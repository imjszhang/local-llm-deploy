"""Versioned MCP Hub configuration. No MCP SDK or process imports."""
from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from ..storage import atomic_json, file_lock


class HubConfigError(ValueError):
    """A configuration error whose message never includes a supplied secret."""


class HubConfigConflict(HubConfigError):
    """The configuration changed after the editor loaded it."""


def default_config():
    return {"schema_version": 1, "revision": 0,
            "listen": {"host": "0.0.0.0", "port": 8899, "public_url": ""},
            "allowed_origins": [], "allowed_hosts": [], "services": []}


def _text(value, field, *, empty=False, limit=8192):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise HubConfigError(f"{field} 必须是{'非空' if not empty else ''}字符串")
    if len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HubConfigError(f"{field} 过长或包含控制字符")
    return value


def _http_url(value, field, *, origin=False, empty=False):
    _text(value, field, empty=empty)
    if empty and not value:
        return value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or "\\" in value or
                any(char.isspace() for char in value) or
                parsed.username is not None or parsed.password is not None or parsed.fragment or
                any(char.isspace() for char in parsed.netloc) or parsed.port == 0):
            raise ValueError
        if origin and (parsed.path not in ("", "/") or parsed.query):
            raise ValueError
    except ValueError:
        raise HubConfigError(f"{field} 必须是无用户信息及片段的 HTTP(S) {'origin' if origin else 'URL'}") from None
    return value.rstrip("/") if origin else value


def _host(value):
    _text(value, "listen.host", limit=253)
    # The 8888 management API reaches the Hub over loopback. Binding solely to
    # a LAN interface would make the internal management endpoint unreachable.
    if value not in ("0.0.0.0", "127.0.0.1", "::", "::1"):
        raise HubConfigError("listen.host 必须是 0.0.0.0、127.0.0.1、:: 或 ::1")
    return value


def validate_config(raw):
    """Return a normalized independent copy; errors identify fields, never values."""
    if not isinstance(raw, dict):
        raise HubConfigError("Hub 配置必须是对象")
    if set(raw) - set(default_config()):
        raise HubConfigError("Hub 配置包含未知字段")
    result = default_config()
    result.update(copy.deepcopy(raw))
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise HubConfigError("schema_version 必须为 1")
    if type(result["revision"]) is not int or result["revision"] < 0:
        raise HubConfigError("revision 必须是非负整数")
    listen = result["listen"]
    if not isinstance(listen, dict) or set(listen) - {"host", "port", "public_url"}:
        raise HubConfigError("listen 必须包含 host、port 和可选 public_url")
    listen = {**default_config()["listen"], **listen}
    listen["host"] = _host(listen["host"])
    if type(listen["port"]) is not int or not 1 <= listen["port"] <= 65535:
        raise HubConfigError("listen.port 必须是 1–65535 的整数")
    _http_url(listen["public_url"], "listen.public_url", empty=True)
    result["listen"] = listen
    hosts = result["allowed_hosts"]
    if not isinstance(hosts, list) or len(hosts) > 100:
        raise HubConfigError("allowed_hosts 必须是最多 100 项的 IP 地址列表")
    try:
        result["allowed_hosts"] = list(dict.fromkeys(str(ipaddress.ip_address(value))
            for value in hosts if isinstance(value, str)))
        if any(not isinstance(value, str) for value in hosts):
            raise ValueError
    except ValueError:
        raise HubConfigError("allowed_hosts 必须只包含完整的 IP 地址，不支持通配符或网段") from None
    origins = result["allowed_origins"]
    if not isinstance(origins, list) or len(origins) > 100:
        raise HubConfigError("allowed_origins 必须是最多 100 项的列表")
    result["allowed_origins"] = list(dict.fromkeys(
        _http_url(value, "allowed_origins", origin=True) for value in origins))
    services = result["services"]
    if not isinstance(services, list) or len(services) > 100:
        raise HubConfigError("services 必须是最多 100 项的列表")
    normalized, identifiers = [], set()
    for entry in services:
        if not isinstance(entry, dict) or set(entry) - {
                "id", "name", "kind", "enabled", "command", "args", "env", "url", "allowed_tools"}:
            raise HubConfigError("service 必须是对象且不能包含未知字段")
        item = {"name": "", "enabled": True, "command": "", "args": [], "env": {},
                "url": "", "allowed_tools": [], **entry}
        identifier = item.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", identifier) or len(identifier) > 64:
            raise HubConfigError("service.id 必须是至多 64 字符的小写 slug")
        if identifier == "default":
            raise HubConfigError("service.id 不能使用聚合入口保留名 default")
        if identifier in identifiers:
            raise HubConfigError("service.id 不能重复")
        identifiers.add(identifier)
        if item["name"] == "":
            item["name"] = identifier
        _text(item["name"], "service.name", limit=120)
        if item.get("kind") not in ("comfyui", "stdio", "http"):
            raise HubConfigError("service.kind 必须是 comfyui、stdio 或 http")
        if type(item["enabled"]) is not bool:
            raise HubConfigError("service.enabled 必须是布尔值")
        command = _text(item["command"], "service.command", empty=True, limit=4096)
        if command and (command.startswith("-") or (not os.path.isabs(command) and
                not re.fullmatch(r"[A-Za-z0-9_.+-]+", command))):
            raise HubConfigError("service.command 必须是绝对可执行文件路径或单个 PATH 命令名")
        if item["kind"] != "http" and item["enabled"] and not command:
            raise HubConfigError("启用的 stdio/comfyui 服务必须声明 command")
        args = item["args"]
        if not isinstance(args, list) or len(args) > 256:
            raise HubConfigError("service.args 必须是最多 256 项的字符串列表")
        for arg in args:
            _text(arg, "service.args", empty=True)
        env = item["env"]
        if not isinstance(env, dict) or len(env) > 128:
            raise HubConfigError("service.env 必须是最多 128 项的对象")
        for key, value in env.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise HubConfigError("service.env 包含无效环境变量名")
            _text(value, "service.env 值", empty=True)
        _http_url(item["url"], "service.url", empty=item["kind"] == "stdio" or not item["enabled"])
        if item["kind"] == "comfyui" and item["url"]:
            parsed = urlsplit(item["url"])
            if parsed.scheme != "http" or parsed.path not in ("", "/") or parsed.query:
                raise HubConfigError("ComfyUI url 必须是仅含主机和端口的普通 HTTP 地址")
        if item["kind"] == "http" and (command or args or env):
            raise HubConfigError("HTTP 服务不使用 command、args 或 env")
        allowed = item["allowed_tools"]
        if not isinstance(allowed, list) or len(allowed) > 1000:
            raise HubConfigError("service.allowed_tools 必须是工具名列表")
        for tool in allowed:
            _text(tool, "service.allowed_tools", limit=128)
            if not re.fullmatch(r"[A-Za-z0-9_.:/-]+", tool):
                raise HubConfigError("service.allowed_tools 包含无效工具名")
        item["allowed_tools"] = list(dict.fromkeys(allowed))
        normalized.append(item)
    result["services"] = normalized
    return result


class HubConfigStore:
    def __init__(self, paths):
        self.paths = paths
        self.path = Path(paths.root) / "data" / "mcp-hub" / "config.json"
        self.lock_path = self.path.with_suffix(".lock")

    def load(self):
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default_config()
        except UnicodeError:
            raise HubConfigError("Hub 配置必须是 UTF-8 JSON") from None
        try:
            raw = json.loads(content)
        except (ValueError, UnicodeError):
            raise HubConfigError("Hub 配置不是有效 JSON") from None
        return validate_config(raw)

    def save(self, config, expected_revision=None):
        candidate = validate_config(config)
        expected = candidate["revision"] if expected_revision is None else expected_revision
        if type(expected) is not int or expected < 0:
            raise HubConfigError("expected_revision 必须是非负整数")
        with file_lock(self.lock_path):
            current = self.load()
            if expected != current["revision"]:
                raise HubConfigConflict("Hub 配置已更新，请重新加载后保存")
            candidate["revision"] = current["revision"] + 1
            atomic_json(self.path, candidate)
            self.path.chmod(0o600)
        return copy.deepcopy(candidate)
