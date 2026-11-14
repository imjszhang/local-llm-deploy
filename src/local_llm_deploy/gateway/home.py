"""Default gateway homepage. Lists mounted apps without upstream addresses."""
from __future__ import annotations

import html


KIND_LABELS = {
    'knowledge': '知识库',
    'video': '本机视频库',
    'monitor': '运行监控与模型对话',
    'agents': '本机 AI 探员',
    'http': '自行注册的应用',
}


def _card(alias: str, href: str, label: str, shown: str) -> str:
    safe_alias = html.escape(alias)
    return (
        f'<a class="card" href="{html.escape(href, quote=True)}">'
        f'<span class="mark">{html.escape(alias[:1] or "A")}</span>'
        f'<span><strong>{safe_alias}</strong><em>{html.escape(label)}</em>'
        f'<code>{html.escape(shown)}</code></span>'
        f'<span class="open">打开</span></a>'
    )


def render_home(apps, proxies=None) -> bytes:
    entries = []
    for spec in (apps or {}).values():
        entries.append((spec.alias, spec.key, spec.endpoint, spec.endpoint, KIND_LABELS.get(spec.kind, spec.kind)))
    for spec in (proxies or {}).values():
        shown = spec.prefix + '/'
        query = spec.raw.get('home_query') if isinstance(getattr(spec, 'raw', None), dict) else None
        href = shown + (('?' + query) if query else '')
        entries.append((spec.alias, spec.key, href, shown, '外部服务'))
    cards = [_card(alias, href, label, shown) for alias, _key, href, shown, label in sorted(entries)]
    catalog = ''.join(cards) or '<p class="empty">还没有登记应用。知识库、视频库写在本机 models.json；其他 HTTP 应用可自行注册入口。</p>'
    page = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>本地网关</title>
  <style>
    :root {{ color-scheme: light; --ink:#1d2433; --muted:#667085; --line:#e4e7ef; --accent:#4338ca; --bg:#f6f7fb; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "Segoe UI", "PingFang SC", sans-serif; }}
    header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:24px; padding:36px 8vw 8px; }}
    h1 {{ margin:0 0 6px; font-size:32px; letter-spacing:-.04em; }}
    header p, .empty {{ margin:0; color:var(--muted); }}
    main {{ padding:28px 8vw 64px; }}
    h2 {{ margin:28px 0 12px; font-size:13px; letter-spacing:.12em; color:var(--muted); font-weight:650; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:12px; }}
    a.card {{ display:flex; align-items:center; gap:14px; min-height:92px; padding:16px 18px; border:1px solid var(--line); border-radius:14px; background:#fff; color:inherit; text-decoration:none; }}
    a.card:hover {{ border-color:#c7c2f3; }}
    .mark {{ display:grid; width:42px; height:42px; flex:none; place-items:center; border-radius:12px; background:#eeedfc; color:var(--accent); font-weight:700; }}
    strong {{ display:block; font-size:16px; }}
    em, code {{ display:block; color:var(--muted); font-style:normal; font-size:12px; }}
    code {{ margin-top:2px; font-family:ui-monospace, monospace; }}
    .open {{ margin-left:auto; color:var(--accent); font-size:13px; white-space:nowrap; }}
    a.guide {{ color:var(--accent); text-decoration:none; white-space:nowrap; }}
  </style>
</head>
<body>
  <header>
    <div><h1>本地网关</h1><p>从这里进入已登记的应用，或打开监控和对话。</p></div>
    <a class="guide" href="/api/guide">接口说明</a>
  </header>
  <main>
    <h2>应用</h2>
    <div class="grid">{catalog}</div>
  </main>
  <script>
    const storageKey = 'local-llm-deploy.gateway-api-key'
    document.querySelectorAll('a.card[href^="/services/"]').forEach(link => {{
      link.addEventListener('click', event => {{
        let key = ''
        try {{ key = (localStorage.getItem(storageKey) || '').trim() }} catch (error) {{ return }}
        if (!key || key.length > 8192 || /[\\r\\n]/.test(key)) return
        event.preventDefault()
        const href = link.getAttribute('href')
        fetch(href, {{ headers: {{ Authorization: 'Bearer ' + key }}, credentials: 'same-origin' }})
          .finally(() => {{ location.href = href }})
      }})
    }})
  </script>
</body>
</html>
'''
    return page.encode('utf-8')
