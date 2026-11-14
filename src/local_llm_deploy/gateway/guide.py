"""Visitor-facing description of the current gateway. No upstreams or credentials."""
from __future__ import annotations

import html


CAPABILITY_TEXT = {
    'chat': '对话与文本补全',
    'embedding': '文本向量',
    'rerank': '文档重排序',
    'asr': '音频转写',
    'tts': '语音合成',
}
APP_SUMMARY = {
    'knowledge': '外部知识库。请求沿这个入口转发，认证用知识库自己的凭据。',
    'video': '本机视频学习库。浏览不用网关密钥；入库和重试使用视频库自己的令牌。',
    'monitor': '运行监控和模型对话。本机打开页面时会领取控制台会话。',
    'agents': '本机 AI 探员。本机打开页面时会领取控制台会话。',
    'http': '自行登记的 HTTP 应用。网关按该路径转发，并带上访问者自己的 Authorization。',
}
ENDPOINT_TEXT = {
    '/v1/chat/completions': ('POST', '对话补全，JSON 或 SSE。'),
    '/v1/completions': ('POST', '文本补全。'),
    '/v1/responses': ('POST', 'OpenAI Responses。只有模型显式声明这个路径时才开放。'),
    '/v1/messages': ('POST', 'Anthropic Messages。只有模型显式声明这个路径时才开放。'),
    '/v1/embeddings': ('POST', '文本向量。'),
    '/v1/rerank': ('POST', '文档重排序，Jina 格式。'),
    '/v1/audio/transcriptions': ('POST', '音频转写，multipart 上传。'),
    '/v1/audio/speech': ('POST', '语音合成。JSON 输入，WAV 输出。'),
}
CAPABILITY_ROUTES = {
    'chat': ('/v1/chat/completions', '/v1/completions'),
    'embedding': ('/v1/embeddings',),
    'rerank': ('/v1/rerank',),
    'asr': ('/v1/audio/transcriptions',),
    'tts': ('/v1/audio/speech',),
}


def wants_html(accept) -> bool:
    header = (accept or '').lower()
    html_at = header.find('text/html')
    if html_at < 0:
        return False
    json_at = header.find('application/json')
    return json_at < 0 or html_at < json_at


def build_guide(apps, proxies, specs, discovered, *, mcp_hub=None) -> dict:
    applications = [_app(spec) for spec in (apps or {}).values()]
    services = [_service(spec) for spec in (proxies or {}).values()]
    models = _models(specs or {}, discovered or {})
    return {
        'title': '本地网关说明',
        'summary': '这里是当前登记的应用、外部服务、MCP 服务和模型接口。模型接口使用网关密钥，MCP 使用独立设备令牌。本说明可以直接打开。',
        'apps': sorted(applications, key=lambda item: (item['alias'], item['key'])),
        'services': sorted(services, key=lambda item: (item['alias'], item['key'])),
        'models': models,
        'mcp_hub': mcp_hub,
        'interfaces': _interfaces(models),
    }


def render_guide(payload: dict) -> bytes:
    sections = [
        _section('应用', payload['apps'], '当前没有登记应用。', lambda item: (item['alias'], item['path'], item['summary'])),
        _section('外部服务', payload['services'], '当前没有登记外部服务。', lambda item: (item['alias'], item['path'], item['summary'])),
        _mcp_section(payload.get('mcp_hub')),
        _section('模型', payload['models'], '当前没有登记模型。', _model_line),
        _section('模型接口', payload['interfaces'], '当前没有可调用的模型接口。', _interface_line),
    ]
    page = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(payload["title"])}</title>
  <style>
    :root {{ color-scheme: light; --ink:#1d2433; --muted:#667085; --line:#e4e7ef; --bg:#f6f7fb; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.6 "Segoe UI", "PingFang SC", sans-serif; }}
    main {{ max-width:820px; margin:0 auto; padding:40px 24px 72px; }}
    h1 {{ margin:0 0 8px; font-size:32px; letter-spacing:-.04em; }}
    h2 {{ margin:32px 0 12px; font-size:13px; letter-spacing:.12em; color:var(--muted); font-weight:650; }}
    h3 {{ margin:24px 0 10px; font-size:18px; }}
    p {{ margin:0 0 12px; }}
    .lead {{ color:var(--muted); }}
    article {{ padding:14px 0; border-top:1px solid var(--line); }}
    strong {{ display:block; }}
    code {{ color:var(--muted); font-family:ui-monospace, monospace; font-size:13px; overflow-wrap:anywhere; }}
    pre {{ margin:10px 0 18px; padding:16px; border:1px solid var(--line); border-radius:8px; background:white; overflow-x:auto; }}
    pre code {{ color:var(--ink); overflow-wrap:normal; white-space:pre; }}
    details {{ padding:10px 0; border-top:1px solid var(--line); }}
    summary {{ cursor:pointer; font-weight:600; }}
    details p {{ margin:8px 0; }}
    li {{ margin:10px 0; }}
    a {{ color:#4338ca; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(payload["title"])}</h1>
    <p class="lead">{html.escape(payload["summary"])}</p>
    <p><a href="/">返回首页</a></p>
    {''.join(sections)}
  </main>
</body>
</html>
'''
    return page.encode('utf-8')


def _app(spec) -> dict:
    return {
        'key': spec.key,
        'alias': spec.alias,
        'kind': spec.kind,
        'path': spec.endpoint,
        'summary': APP_SUMMARY.get(spec.kind, '已登记的应用。'),
    }


def _service(spec) -> dict:
    summary = '外部 HTTP 服务。从该路径进入，调用需要网关密钥。'
    if getattr(spec, 'websocket', False):
        summary += '支持 WebSocket。'
    return {
        'key': spec.key,
        'alias': spec.alias,
        'path': spec.prefix.rstrip('/') + '/',
        'summary': summary,
    }


def _models(specs, discovered) -> list:
    rows = []
    for key in sorted(set(specs) | set(discovered)):
        spec = specs.get(key)
        live = discovered.get(key)
        capabilities = tuple(live.capabilities if live is not None else spec.capabilities)
        explicit = live.endpoints if live is not None and live.endpoints is not None else None
        if explicit is None and spec is not None:
            explicit = spec.endpoints
        names = '、'.join(CAPABILITY_TEXT.get(item, item) for item in capabilities) or '未声明能力'
        if live is None:
            summary = f'已登记，当前不可路由。能力：{names}。'
        else:
            summary = f'当前可路由。能力：{names}。'
        defaults = tuple((spec.raw.get('default_for') if spec is not None else None) or ())
        if defaults:
            summary += '默认用于' + '、'.join(CAPABILITY_TEXT.get(item, item) for item in defaults) + '。'
        ollama = bool(getattr(live, 'ollama', False) or (spec is not None and spec.backend == 'ollama'))
        if ollama:
            summary += '也可以走 Ollama 原生接口 /api/ollama/。'
        rows.append({
            'key': key,
            'alias': live.alias if live is not None else spec.alias,
            'routable': live is not None,
            'ollama': ollama,
            'capabilities': list(capabilities),
            'summary': summary,
            'endpoints': _endpoint_rows(capabilities, explicit),
        })
    return rows


def _endpoint_rows(capabilities, explicit) -> list:
    if explicit is None:
        paths = tuple(path for capability in capabilities for path in CAPABILITY_ROUTES.get(capability, ()))
    else:
        paths = tuple(explicit)
    rows = []
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        method, summary = ENDPOINT_TEXT.get(path, ('POST', '该模型声明的接口。'))
        rows.append({'method': method, 'path': path, 'summary': summary})
    return rows


def _interfaces(models) -> list:
    rows = [
        {'method': 'GET', 'path': '/api/guide', 'summary': '本说明。浏览器打开是页面，带 application/json 时返回同一份内容。', 'auth': '无需密钥', 'models': []},
        {'method': 'GET', 'path': '/v1/models', 'summary': '当前可路由模型的键和别名。', 'auth': '网关密钥', 'models': [item['key'] for item in models if item['routable']]},
        {'method': 'POST', 'path': '/api/<模型键>/', 'summary': '转到对应模型的后端。标准路径也可以写成 /api/<模型键>/v1/...。', 'auth': '网关密钥', 'models': [item['key'] for item in models]},
    ]
    grouped = {}
    for model in models:
        for endpoint in model['endpoints']:
            grouped.setdefault((endpoint['method'], endpoint['path']), []).append(model['key'])
    for (method, path), keys in sorted(grouped.items(), key=lambda item: item[0][1]):
        summary = ENDPOINT_TEXT.get(path, ('POST', '该模型声明的接口。'))[1]
        rows.append({'method': method, 'path': path, 'summary': summary, 'auth': '网关密钥', 'models': keys})
    ollama = [item['key'] for item in models if item['ollama']]
    if ollama:
        rows.append({
            'method': 'POST', 'path': '/api/ollama/',
            'summary': 'Ollama 原生接口，例如 /api/ollama/chat 和 /api/ollama/generate。经网关调度。',
            'auth': '网关密钥', 'models': ollama,
        })
    return rows


def _section(title, items, empty, line) -> str:
    body = f'<p>{html.escape(empty)}</p>' if not items else ''.join(_article(*line(item)) for item in items)
    return f'<h2>{html.escape(title)}</h2>{body}'


def _model_line(item):
    paths = '、'.join(endpoint['path'] for endpoint in item['endpoints']) or '无'
    return item['alias'], item['key'], item['summary'] + '接口：' + paths


def _interface_line(item):
    models = '、'.join(item['models'])
    detail = item['summary'] + '认证：' + item['auth'] + ('。模型：' + models + '。' if models else '。')
    return item['method'] + ' ' + item['path'], '', detail


def _mcp_section(hub):
    title = '<h2>MCP Hub 服务</h2>'
    if not hub:
        return title + '<p>当前没有登记 MCP 服务。</p>'
    states = {'disabled': '未启用', 'pending_restart': '等待应用配置', 'upstream_unavailable': '远端不可达',
              'unavailable': '当前不可用', 'unknown': '状态未确认', 'starting': '启动中',
              'error': '连接异常', 'stopped': '已停止', 'stale': '状态已过期', 'unhealthy': '连接异常'}
    rows = []
    for item in hub['services']:
        state = '当前可用' if item['available'] else states.get(item['status'], '状态未确认')
        detail = state + ' · ' + item['kind'] + ' · Streamable HTTP。'
        if item['tool_count'] is not None:
            detail += f'服务目录 {item["tool_count"]} 个工具，实际可用工具由设备权限决定。'
        rows.append(_article(item['name'] + ' · ' + item['id'], item['url'] or '连接地址暂不可用', detail))
    if not rows:
        rows.append('<p>当前没有登记 MCP 服务。</p>')
    if hub['aggregate']:
        rows.append(_article('多个服务共用一个连接', hub['aggregate']['url'] or '', hub['aggregate']['summary']))
    details = '<p>' + html.escape(hub['authentication']['summary']) + '</p>'
    details += '<p>' + html.escape(' '.join(item['name'] + '：' + item['summary'] for item in hub['permissions'])) + '</p>'
    return (title + '<p>' + html.escape(hub['summary']) + '</p>' + details
            + '<p><a href="/mcp-hub/">打开 Hub 管理页面</a></p>' + ''.join(rows)
            + _mcp_setup(hub))


def _mcp_setup(hub):
    setup = hub.get('token_setup')
    if not setup:
        return ''
    sections = ['<h3>' + html.escape(setup['title']) + '</h3><ol>']
    for step in setup['steps']:
        sections.append('<li><strong>' + html.escape(step['title']) + '</strong>'
                        + html.escape(step['detail']) + '</li>')
    sections.append('</ol><h3>MCP 连接配置示例</h3>')
    for note in hub.get('connection_notes', []):
        sections.append('<p>' + html.escape(note) + '</p>')
    examples = hub.get('connection_examples', [])
    if not examples:
        sections.append('<p>当前没有可生成配置示例的服务地址；请先由管理员配置并启用服务，再从可访问 Hub 的地址打开本说明。</p>')
    for example in examples:
        sections.append('<h3>' + html.escape(example['name'] + ' · ' + example['service_id']) + '</h3>')
        if not example['available']:
            sections.append('<p>此入口当前未就绪；配置示例可提前保存，连接前请先确认上方服务状态。</p>')
        for config in example['configs']:
            sections.append('<details><summary>' + html.escape(config['client']) + ' · <code>'
                            + html.escape(config['config_file']) + '</code></summary>'
                            + '<p>选择代码文本即可复制；合并到现有配置后替换令牌占位符。</p><pre><code>'
                            + html.escape(config['content']) + '</code></pre></details>')
    sections.append('<h3>确认接入成功</h3><ol>')
    for step in hub.get('verification', []):
        sections.append('<li>' + html.escape(step) + '</li>')
    sections.append('</ol>')
    return ''.join(sections)


def _article(title, path, summary) -> str:
    path_html = f'<code>{html.escape(path)}</code>' if path else ''
    return (
        '<article>'
        f'<strong>{html.escape(title)}</strong>'
        f'{path_html}'
        f'<p>{html.escape(summary)}</p>'
        '</article>'
    )
