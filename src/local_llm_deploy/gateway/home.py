"""Default gateway homepage. Lists mounted apps without upstream addresses."""
from __future__ import annotations

import html


KIND_LABELS = {
    'knowledge': '知识库',
    'video': '本机视频库',
    'monitor': '运行监控与模型对话',
    'http': '自行注册的应用',
}


def render_home(apps) -> bytes:
    cards = []
    for spec in sorted((apps or {}).values(), key=lambda item: (item.alias, item.key)):
        label = html.escape(KIND_LABELS.get(spec.kind, spec.kind))
        alias = html.escape(spec.alias)
        endpoint = html.escape(spec.endpoint, quote=True)
        cards.append(
            f'<a class="card" href="{endpoint}">'
            f'<span class="mark">{html.escape(spec.alias[:1] or "A")}</span>'
            f'<span><strong>{alias}</strong><em>{label}</em>'
            f'<code>{html.escape(spec.endpoint)}</code></span>'
            f'<span class="open">打开</span></a>'
        )
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
  </style>
</head>
<body>
  <header>
    <div><h1>本地网关</h1><p>从这里进入已登记的应用，或打开监控和对话。</p></div>
  </header>
  <main>
    <h2>应用</h2>
    <div class="grid">{catalog}</div>
  </main>
</body>
</html>
'''
    return page.encode('utf-8')
