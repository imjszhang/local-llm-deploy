from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HAS_FASTMCP = importlib.util.find_spec("fastmcp") is not None
if HAS_FASTMCP:
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import FastMCPTransport
    from fastmcp.exceptions import AuthorizationError
    from fastmcp.server import create_proxy
    from mcp_types import ToolAnnotations
    from local_llm_deploy.mcp_hub.generic import HubServicePolicy, _IsolatedProxyClient, build_generic_server


@unittest.skipUnless(HAS_FASTMCP, "Run with the dedicated MCP Hub environment")
class GenericProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.upstream = FastMCP("fake-upstream")
        self.writes = []

        @self.upstream.tool(annotations=ToolAnnotations(read_only_hint=True))
        def inspect_item() -> str:
            return "read-result"

        @self.upstream.tool()
        def change_item(value: str) -> str:
            self.writes.append(value)
            return value

        @self.upstream.resource("test://record")
        def record() -> str:
            return "resource-result"

        @self.upstream.resource("test://records/{identifier}")
        def record_template(identifier: str) -> str:
            return identifier

        @self.upstream.prompt()
        def greeting() -> str:
            return "hello"

        client = _IsolatedProxyClient(lambda: FastMCPTransport(self.upstream))
        self.proxy = create_proxy(client, name="test-proxy", middleware=[HubServicePolicy("worker")])

    def device(self, services=("worker",), permissions=("read",)):
        request = SimpleNamespace(state=SimpleNamespace(hub_device={
            "id": "test-device", "services": list(services), "permissions": list(permissions)}))
        return patch("local_llm_deploy.mcp_hub.generic.get_http_request", return_value=request)

    async def test_read_only_device_cannot_discover_or_call_mutating_tool(self):
        with self.device():
            self.assertEqual([tool.name for tool in await self.proxy.list_tools()], ["inspect_item"])
            await self.proxy.call_tool("inspect_item", {})
            with self.assertRaises(AuthorizationError):
                await self.proxy.call_tool("change_item", {"value": "forbidden"})
        self.assertEqual(self.writes, [])

    async def test_generate_permission_allows_unannotated_tool(self):
        with self.device(permissions=("read", "generate")):
            self.assertEqual({tool.name for tool in await self.proxy.list_tools()}, {"inspect_item", "change_item"})
            await self.proxy.call_tool("change_item", {"value": "allowed"})
        self.assertEqual(self.writes, ["allowed"])

    async def test_allowed_tools_cannot_be_bypassed_by_direct_call(self):
        self.proxy.add_middleware(HubServicePolicy("worker", ["inspect_item"]))
        with self.device(permissions=("read", "generate")):
            self.assertEqual([tool.name for tool in await self.proxy.list_tools()], ["inspect_item"])
            with self.assertRaises(AuthorizationError):
                await self.proxy.call_tool("change_item", {"value": "forbidden"})
        self.assertEqual(self.writes, [])

    async def test_missing_service_has_empty_catalogs_and_denies_direct_calls(self):
        with self.device(services=("another-service",), permissions=("read", "generate")):
            self.assertEqual(await self.proxy.list_tools(), [])
            self.assertEqual(await self.proxy.list_resources(), [])
            self.assertEqual(await self.proxy.list_resource_templates(), [])
            self.assertEqual(await self.proxy.list_prompts(), [])
            with self.assertRaises(AuthorizationError):
                await self.proxy.call_tool("inspect_item", {})
            with self.assertRaises(AuthorizationError):
                await self.proxy.read_resource("test://record")
            with self.assertRaises(AuthorizationError):
                await self.proxy.render_prompt("greeting", {})

    async def test_resources_and_prompts_require_read(self):
        with self.device():
            self.assertEqual(len(await self.proxy.list_resources()), 1)
            self.assertEqual(len(await self.proxy.list_resource_templates()), 1)
            self.assertEqual(len(await self.proxy.list_prompts()), 1)
            await self.proxy.read_resource("test://record")
            await self.proxy.render_prompt("greeting", {})
        with self.device(permissions=("generate",)):
            self.assertEqual(await self.proxy.list_resources(), [])
            with self.assertRaises(AuthorizationError):
                await self.proxy.read_resource("test://record")

    async def test_aggregate_does_not_reveal_unauthorized_child(self):
        combined = FastMCP("combined")
        combined.mount(self.proxy, namespace="worker")
        with self.device(services=("another-service",)):
            self.assertEqual(await combined.list_tools(), [])
            self.assertEqual(await combined.list_resources(), [])
            self.assertEqual(await combined.list_prompts(), [])
            with self.assertRaises(AuthorizationError):
                await combined.call_tool("worker_inspect_item", {})
        with self.device():
            self.assertEqual([tool.name for tool in await combined.list_tools()], ["worker_inspect_item"])

    async def test_missing_http_identity_fails_closed(self):
        with patch("local_llm_deploy.mcp_hub.generic.get_http_request", side_effect=RuntimeError("no HTTP")):
            self.assertEqual(await self.proxy.list_tools(), [])
            with self.assertRaises(AuthorizationError):
                await self.proxy.call_tool("inspect_item", {})

    async def test_protocol_clients_keep_read_filter_in_both_eras(self):
        for mode in ("legacy", "auto"):
            with self.subTest(mode=mode), self.device():
                async with Client(self.proxy, mode=mode) as client:
                    self.assertEqual([tool.name for tool in await client.list_tools()], ["inspect_item"])
                    result = await client.call_tool("change_item", {"value": "forbidden"}, raise_on_error=False)
                    self.assertTrue(result.is_error)
        self.assertEqual(self.writes, [])

    async def test_builder_never_forwards_device_headers_and_clones_stdio_transport(self):
        with tempfile.TemporaryDirectory(prefix="hub-generic-") as directory:
            spec = {"id": "worker", "kind": "stdio", "name": "Worker", "command": "python",
                    "args": ["-m", "fake_server"], "env": {"OPERATOR_SETTING": "test-value"}, "allowed_tools": []}
            server = build_generic_server(spec, workspace=Path(directory) / "worker")
            left, right = server.client_factory(), server.client_factory()
            self.assertIsNot(left.transport, right.transport)
            self.assertFalse(left._transport_options.forward_incoming_headers)
            self.assertFalse(right._transport_options.forward_incoming_headers)
            self.assertFalse(left.transport.keep_alive)
            self.assertEqual(left.transport.env, {"OPERATOR_SETTING": "test-value"})
            self.assertEqual(left.transport.cwd, str((Path(directory) / "worker").resolve()))
            http = build_generic_server({"id": "remote", "kind": "http", "url": "http://example.test/mcp"})
            remote = http.client_factory()
            self.assertEqual(remote.transport.headers, {})
            self.assertFalse(remote._transport_options.forward_incoming_headers)
            with patch("local_llm_deploy.mcp_hub.generic.httpx2.AsyncClient") as factory:
                remote.transport.httpx_client_factory(headers={}, follow_redirects=True)
                self.assertIs(factory.call_args.kwargs["trust_env"], False)


if __name__ == "__main__":
    unittest.main()
