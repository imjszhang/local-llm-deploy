"""Read-only, allowlisted MCP discovery for the public gateway guide."""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
from urllib.parse import urlsplit

from local_llm_deploy.mcp_hub.config import HubConfigStore
from local_llm_deploy.mcp_hub.controller import HubController


STATES = frozenset(('stopped', 'starting', 'running', 'ready', 'stale', 'unhealthy'))
SERVICE_STATES = STATES | {'disabled', 'error'}


def _hostname(authority):
    try:
        value = urlsplit('http://' + (authority or ''))
        if (not value.hostname or value.username is not None or value.password is not None
                or value.path or value.query or value.fragment):
            return None
        value.port
        hostname = value.hostname
        if ':' in hostname:
            return '[' + str(ipaddress.IPv6Address(hostname)) + ']'
        if re.fullmatch(r'[A-Za-z0-9.-]+', hostname):
            return hostname
    except ValueError:
        pass
    return None


def _base_url(config, runtime, authority):
    public = config['listen'].get('public_url', '')
    if public:
        try:
            value = urlsplit(public)
            # A guide URL must never publish query credentials or user info.
            if (value.scheme in ('http', 'https') and _hostname(value.netloc)
                    and not value.query and not value.fragment and '\\' not in public
                    and not any(char.isspace() for char in public)):
                return public.rstrip('/'), 'public'
        except ValueError:
            pass
    host = runtime.get('host', config['listen']['host'])
    port = runtime.get('port', config['listen']['port'])
    if type(port) is not int or not 1 <= port <= 65535:
        return None, 'unknown'
    local = host in ('127.0.0.1', '::1')
    hostname = ('[::1]' if host == '::1' else host) if local else _hostname(authority)
    return (f'http://{hostname}:{port}' if hostname else None), ('local' if local else 'lan')


def _token_setup():
    return {
        'title': '创建独立设备 / 项目令牌', 'management_path': '/mcp-hub/',
        'steps': [
            {'title': '由管理员连接管理页',
             'detail': '打开本页的“Hub 管理页面”，在“连接管理控制台”填写“管理员 API Key”，点击“连接管理”。本机和局域网管理都需要管理员密钥；普通客户端向管理员领取设备令牌，不需要持有管理员密钥。'},
            {'title': '为设备或项目单独命名',
             'detail': '进入“设备与连接”，在“添加设备 / 项目”填写“名称”，例如 laptop-video-remake；每台设备或项目分别创建，便于单独撤销。'},
            {'title': '选择服务和权限',
             'detail': '在“允许访问的服务”中只勾选该项目需要的服务。读取权限始终包含；“允许生成与提交任务”默认勾选，只读接入请取消勾选。ComfyUI 上传、提交生成和取回输出需要保留此项。'},
            {'title': '创建并保存一次性显示的令牌',
             'detail': '点击“创建设备令牌”，立即“复制令牌”并保存到该客户端的私有配置或仅当前用户可读的文件。原令牌只显示一次，刷新或关闭后无法再次查看；不要发给其他项目共用。'},
            {'title': '复制对应连接配置',
             'detail': '在“连接 MCP 客户端”选择“服务”，可复制连接地址或通用 JSON 配置；仅当刚创建的令牌已获该服务授权时，配置才会包含它。下方提供各客户端对应格式，Codex 使用 TOML；将 <设备令牌> 替换为刚保存的值。剪贴板不可用时选择文本手动复制。保存完成后点击“已保存，关闭”。'},
        ],
    }


def _connection_example(service_id, name, url, available):
    server = 'hub-' + service_id
    entry = {'url': url, 'headers': {'Authorization': 'Bearer <设备令牌>'}}
    return {
        'service_id': service_id, 'name': name, 'url': url, 'available': available,
        'configs': [
            {'client': 'Claude Code', 'config_file': '.mcp.json', 'format': 'json',
             'content': json.dumps({'mcpServers': {server: {'type': 'http', **entry}}}, ensure_ascii=False, indent=2)},
            {'client': 'Cursor', 'config_file': '.cursor/mcp.json', 'format': 'json',
             'content': json.dumps({'mcpServers': {server: entry}}, ensure_ascii=False, indent=2)},
            {'client': 'Codex', 'config_file': '.codex/config.toml', 'format': 'toml',
             'content': (f'[mcp_servers.{server}]\nurl = {json.dumps(url, ensure_ascii=False)}\n'
                         'http_headers = { Authorization = "Bearer <设备令牌>" }\n'
                         'startup_timeout_sec = 30\ntool_timeout_sec = 400\n')},
        ],
    }


class MCPGuide:
    """Do not construct HubState: reading the guide must not create a database/key."""

    def __init__(self, paths, *, store=None, controller=None, status_reader=None):
        self.paths = paths
        self.store = store or HubConfigStore(paths)
        self.controller = controller or HubController(paths)
        self.status_reader = status_reader or self._status

    def _status(self, runtime):
        if runtime.get('status') not in ('starting', 'running', 'ready', 'unhealthy'):
            return None
        port = runtime.get('port')
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        # Read the existing internal credential only, never create or expose it.
        with (self.paths.root / 'data' / 'mcp-hub' / 'internal.key').open('r', encoding='utf-8') as stream:
            key = stream.read(513).strip()
        if not key or len(key) > 512 or any(char.isspace() for char in key):
            return None
        host = '::1' if runtime.get('host') in ('::', '::1') else '127.0.0.1'
        connection = http.client.HTTPConnection(host, port, timeout=1)
        try:
            connection.request('GET', '/internal/status', headers={'X-Hub-Internal-Key': key,
                                                                  'Accept': 'application/json'})
            response = connection.getresponse()
            data = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(data) > 1024 * 1024:
                return None
            result = json.loads(data)
            return result if isinstance(result, dict) else None
        finally:
            connection.close()

    def snapshot(self, authority):
        result = {
            'status': 'unconfigured', 'available': False, 'requires_restart': False,
            'management_path': '/mcp-hub/', 'transport': 'streamable-http',
            'authentication': {
                'type': 'bearer', 'header': 'Authorization', 'scheme': 'Bearer', 'credential': 'device_token',
                'summary': '使用 Hub 管理页为当前设备或项目创建的独立令牌。请求头为 Authorization: Bearer <设备令牌>，不能使用网关管理员密钥或本机控制台会话。',
            },
            'permissions': [
                {'name': 'read', 'summary': '查询已获授权服务的只读工具、资源和提示词。'},
                {'name': 'generate', 'summary': '执行生成及其他写入工具；ComfyUI 还包括上传、修改工作流和取回输出。'},
            ],
            'summary': '可用表示 Hub 已挂载该服务；远端连通性以最近一次探测为参考，不代表生成模型或模板已就绪。服务目录不代表当前设备已获授权；实际工具还受设备服务范围、read/generate 权限和服务工具白名单限制。',
            'services': [], 'aggregate': None,
            'token_setup': _token_setup(), 'connection_examples': [],
            'connection_notes': [
                '选择下方一种客户端配置，将片段合并到项目根目录对应文件，保留已有服务；不要直接覆盖整个配置文件。示例中的 <设备令牌> 必须替换，不能使用网关管理员密钥。',
                '局域网设备使用这里列出的 Hub 地址；127.0.0.1 / localhost 指向客户端自己，不能用于连接另一台 Hub 主机。8888 的 /mcp-hub/ 是管理页面，MCP 客户端应连接服务 URL。',
                '单服务和聚合入口按需选择一个。聚合入口仍受同一设备授权限制，工具名称带 <服务ID>_ 前缀；不会额外授予服务访问权。',
                '把包含令牌的配置文件加入 .gitignore，并限制为仅当前用户可读。丢失令牌时由管理员撤销旧设备再创建；新设备不会继承旧设备的任务和文件。',
                '若客户端使用 HTTP 代理，将 Hub 主机名或 IP 加入 NO_PROXY / no_proxy，或为该地址设置直连，避免局域网请求绕到代理。',
            ],
            'verification': [
                '保存配置后重新打开项目的 Agent 会话，按客户端提示启用或信任项目连接，查看 MCP 工具目录。',
                'ComfyUI 单服务连接先调用 server_info，确认 bridge 为 running 且 remote.online 为 true；聚合入口对应 <服务ID>_server_info，例如 comfyui_server_info。其他服务先查看工具目录，不直接提交生成。',
                '连接拒绝或超时时检查 Hub 服务状态和局域网地址；401 检查令牌是否正确或已撤销；工具缺失时检查设备服务范围及 read / generate 权限。',
            ],
        }
        try:
            config = self.store.load()
        except (OSError, ValueError):
            result.update(status='unavailable', summary='Hub 配置暂不可用，请在管理页面检查。')
            return result
        if not config['services']:
            return result
        runtime = {}
        try:
            runtime = self.controller.status()
            live = self.status_reader(runtime)
        except (OSError, ValueError, http.client.HTTPException):
            live = None
        result['status'] = runtime.get('status') if runtime.get('status') in STATES else 'unavailable'
        result['available'] = isinstance(live, dict) and live.get('available') is True
        applied = live.get('applied_revision') if isinstance(live, dict) else None
        result['requires_restart'] = type(applied) is int and applied != config['revision']
        rows = live.get('services', []) if isinstance(live, dict) else []
        rows = rows if isinstance(rows, list) else []
        observed = {row['id']: row for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)}
        base, scope = _base_url(config, runtime, authority)
        result['scope'] = scope
        for spec in config['services']:
            current = observed.get(spec['id'], {})
            status = current.get('status') if current.get('status') in SERVICE_STATES else 'unknown'
            remote = current.get('remote') if isinstance(current.get('remote'), dict) else {}
            available = (spec['enabled'] and result['available'] and current.get('available') is True
                         and remote.get('online') is not False and not result['requires_restart'])
            if not spec['enabled']:
                status = 'disabled'
            elif result['requires_restart']:
                status = 'pending_restart'
            elif not result['available']:
                status = 'unavailable'
            elif remote.get('online') is False:
                status = 'upstream_unavailable'
            count = current.get('tool_count')
            result['services'].append({
                'id': spec['id'], 'name': spec['name'], 'kind': spec['kind'], 'enabled': spec['enabled'],
                'available': available, 'status': status,
                'url': f'{base}/mcp/{spec["id"]}' if base else None,
                'transport': 'streamable-http',
                'tool_count': count if type(count) is int and count >= 0 else None,
                'remote_online': remote.get('online') if type(remote.get('online')) is bool else None,
            })
        result['services'].sort(key=lambda item: item['id'])
        if any(item['enabled'] for item in result['services']):
            result['aggregate'] = {
                'url': f'{base}/mcp/default' if base else None, 'transport': 'streamable-http',
                'summary': '聚合入口仅显示设备已获授权的服务，工具名增加 <服务ID>_ 前缀；使用同一设备令牌。',
            }
        if scope == 'local':
            result['summary'] += 'Hub 当前只监听本机回环地址；这里的连接地址仅供 Hub 主机本机使用。'
        if result['requires_restart']:
            result['summary'] += '保存配置尚未应用，服务暂不标为可用；请在管理页面重启 Hub 后重新查看。'
        result['connection_examples'] = [
            _connection_example(item['id'], item['name'], item['url'], item['available'])
            for item in result['services'] if item['enabled'] and item['url']
        ]
        if result['aggregate'] and result['aggregate']['url']:
            result['connection_examples'].append(_connection_example(
                'default', '聚合入口（已授权服务）', result['aggregate']['url'],
                any(item['available'] for item in result['services'])))
        return result
