"""Runtime-only adapters for administrator-registered stdio and HTTP MCP servers.

Generic stdio servers receive a fresh process per request. This intentionally
does not promise browser/session state across calls. Paths refer to the Hub's
filesystem, and the managed cwd is organization, not a filesystem sandbox.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import httpx2
from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import AuthorizationError
from fastmcp.server import create_proxy
from fastmcp.server.dependencies import get_context, get_http_request
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp.utilities.versions import VersionSpec
from mcp_types.version import MODERN_PROTOCOL_VERSIONS


def _device():
    try:
        device = getattr(get_http_request().state, "hub_device", None)
    except RuntimeError:
        return None
    return device if isinstance(device, dict) else None


def _read_only(tool):
    annotations = getattr(tool, "annotations", None)
    if isinstance(annotations, dict):
        return annotations.get("readOnlyHint", annotations.get("read_only_hint")) is True
    if hasattr(annotations, "read_only_hint"):
        return annotations.read_only_hint is True
    return getattr(annotations, "readOnlyHint", None) is True


def _version(message):
    meta = getattr(message, "meta", None)
    value = meta.get("fastmcp", {}).get("version") if isinstance(meta, dict) and isinstance(meta.get("fastmcp"), dict) else None
    if isinstance(value, str):
        return VersionSpec(eq=value)
    if isinstance(value, dict):
        fields = {key: value.get(key) for key in ("eq", "gte", "lt")}
        if all(item is None or isinstance(item, str) for item in fields.values()):
            return VersionSpec(**fields)
    return None


class HubServicePolicy(Middleware):
    """Enforce the current HTTP device identity on every child-server operation."""

    def __init__(self, service_id, allowed_tools=()):
        self.service_id = service_id
        self.allowed_tools = frozenset(allowed_tools)

    def _permissions(self):
        device = _device()
        if not device or self.service_id not in device.get("services", ()):
            return frozenset()
        return frozenset(device.get("permissions", ()))

    def _may_tool(self, tool, permissions):
        return (bool(tool) and (not self.allowed_tools or tool.name in self.allowed_tools) and
                ("read" if _read_only(tool) else "generate") in permissions)

    @staticmethod
    def _deny():
        raise AuthorizationError("该设备无权使用此 MCP 服务或操作")

    async def on_request(self, context, call_next):
        # Lists return empty for unauthorized mounted child servers so aggregate
        # discovery remains usable for a device with access to other children.
        listing = ("tools/list", "resources/list", "resources/templates/list", "prompts/list")
        permissions = self._permissions()
        if context.method not in listing and not permissions:
            self._deny()
        if context.method == "completion/complete" and "read" not in permissions:
            self._deny()
        return await call_next(context)

    async def on_list_tools(self, context, call_next):
        permissions = self._permissions()
        if not permissions:
            return []
        return [tool for tool in await call_next(context) if self._may_tool(tool, permissions)]

    async def on_call_tool(self, context, call_next):
        permissions = self._permissions()
        if not permissions or context.fastmcp_context is None:
            self._deny()
        name = context.message.name
        if self.allowed_tools and name not in self.allowed_tools:
            self._deny()
        # Resolve exactly the version the request will invoke. An older version
        # may have different readOnlyHint semantics from the latest listing.
        tool = await context.fastmcp_context.fastmcp.get_tool(name, version=_version(context.message))
        if not self._may_tool(tool, permissions):
            self._deny()
        return await call_next(context)

    async def on_list_resources(self, context, call_next):
        return await call_next(context) if "read" in self._permissions() else []

    async def on_list_resource_templates(self, context, call_next):
        return await call_next(context) if "read" in self._permissions() else []

    async def on_list_prompts(self, context, call_next):
        return await call_next(context) if "read" in self._permissions() else []

    async def check_resource_access(self, context):
        if "read" not in self._permissions():
            self._deny()

    async def on_get_prompt(self, context, call_next):
        if "read" not in self._permissions():
            self._deny()
        return await call_next(context)


class _IsolatedProxyClient(ProxyClient):
    """Clone both client and transport; a shared StdioTransport shares a session."""

    def __init__(self, transport_factory, *, mode="legacy"):
        self._hub_transport_factory = transport_factory
        super().__init__(transport_factory(), forward_incoming_headers=False, mode=mode)

    def new(self):
        mode = self.mode
        try:
            request_context = get_context().request_context
            if request_context is not None:
                version = request_context.protocol_version
                mode = version if version in MODERN_PROTOCOL_VERSIONS else "legacy"
        except RuntimeError:
            pass
        return type(self)(self._hub_transport_factory, mode=mode)


def _http_client(**kwargs):
    # Preserve MCP's long response window; a plain custom httpx client would
    # otherwise impose its five-second default on long-running tool calls.
    kwargs.setdefault("timeout", httpx2.Timeout(30.0, read=300.0))
    return httpx2.AsyncClient(trust_env=False, **kwargs)


def build_generic_server(spec, *, workspace=None) -> FastMCP:
    """Build a proxy without connecting; only operator-configured env reaches stdio.

    HTTP backends currently accept an endpoint URL only. Inbound device tokens,
    cookies and other request headers are never repurposed as upstream auth.
    """
    spec = deepcopy(spec)
    if spec["kind"] == "stdio":
        directory = Path(workspace) if workspace is not None else Path.cwd() / "data" / "mcp-hub" / "workspaces" / spec["id"]
        directory = directory.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

        def transport_factory():
            return StdioTransport(command=spec["command"], args=list(spec.get("args", [])),
                                  env=dict(spec.get("env", {})), cwd=str(directory), keep_alive=False)
    elif spec["kind"] == "http":
        def transport_factory():
            return StreamableHttpTransport(
                spec["url"], headers={},
                httpx_client_factory=_http_client,
            )
    else:
        raise ValueError("通用 MCP 代理仅支持 stdio 或 http 服务")
    client = _IsolatedProxyClient(transport_factory)
    return create_proxy(client, name=spec.get("name") or spec["id"],
                        middleware=[HubServicePolicy(spec["id"], spec.get("allowed_tools", []))],
                        provider_error_strategy="raise", mask_error_details=True)
