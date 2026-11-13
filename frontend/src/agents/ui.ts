import type { Session } from './types'

export interface UIController {
  showPanel(session: Session | null): void
  updateStatus(status: 'connected' | 'disconnected' | 'reconnecting'): void
}

export function createUI(container: HTMLElement): UIController {
  // ── Title bar ──────────────────────────────────────────────────────────
  const titleBar = document.createElement('div')
  titleBar.style.cssText = [
    'position:absolute', 'top:16px', 'left:20px', 'z-index:10',
    'user-select:none',
  ].join(';')
  titleBar.innerHTML = `
    <div style="font-family:system-ui,sans-serif;color:#e2e8f0;font-size:18px;font-weight:700;letter-spacing:.04em;">
      AI 探员总部
    </div>
    <a href="/" style="display:inline-block;margin-top:4px;font-family:system-ui,sans-serif;color:#64748b;font-size:12px;letter-spacing:.06em;text-decoration:none;">
      应用首页
    </a>
  `
  container.appendChild(titleBar)

  // ── Status indicator ───────────────────────────────────────────────────
  const statusEl = document.createElement('div')
  statusEl.style.cssText = [
    'position:absolute', 'top:16px', 'right:20px', 'z-index:10',
    'display:flex', 'align-items:center', 'gap:7px',
    'font-family:system-ui,sans-serif', 'font-size:12px', 'color:#94a3b8',
    'pointer-events:none', 'user-select:none',
  ].join(';')

  const dot = document.createElement('span')
  dot.style.cssText = 'display:inline-block;width:8px;height:8px;border-radius:50%;background:#4ade80;'
  const statusText = document.createElement('span')
  statusText.textContent = '已连接'
  statusEl.appendChild(dot)
  statusEl.appendChild(statusText)
  container.appendChild(statusEl)

  // pulsing keyframes for reconnecting
  const style = document.createElement('style')
  style.textContent = `
    @keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:.3} }
    @keyframes pulse-overlay { 0%,100%{opacity:.7} 50%{opacity:.3} }
  `
  document.head.appendChild(style)

  // ── Idle overlay ───────────────────────────────────────────────────────
  const idleOverlay = document.createElement('div')
  idleOverlay.style.cssText = [
    'position:absolute', 'inset:0', 'z-index:5',
    'display:flex', 'align-items:center', 'justify-content:center',
    'pointer-events:none',
  ].join(';')
  idleOverlay.innerHTML = `
    <div style="
      font-family:system-ui,sans-serif;color:#334155;font-size:22px;
      font-weight:600;letter-spacing:.05em;
      animation:pulse-overlay 2.5s ease-in-out infinite;
    ">暂无活跃会话</div>
  `
  container.appendChild(idleOverlay)

  // ── Detail panel ───────────────────────────────────────────────────────
  const panel = document.createElement('div')
  panel.style.cssText = [
    'position:absolute', 'top:0', 'right:0', 'bottom:0', 'z-index:20',
    'width:280px',
    'background:rgba(15,21,30,.92)',
    'backdrop-filter:blur(8px)',
    'border-left:1px solid rgba(255,255,255,.07)',
    'transform:translateX(100%)',
    'transition:transform .25s cubic-bezier(.4,0,.2,1)',
    'display:flex', 'flex-direction:column',
    'font-family:system-ui,sans-serif',
  ].join(';')

  const panelHeader = document.createElement('div')
  panelHeader.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:16px 16px 12px;border-bottom:1px solid rgba(255,255,255,.07);'

  const panelTitle = document.createElement('span')
  panelTitle.style.cssText = 'color:#e2e8f0;font-size:14px;font-weight:600;'
  panelTitle.textContent = '会话详情'

  const closeBtn = document.createElement('button')
  closeBtn.textContent = '×'
  closeBtn.style.cssText = [
    'background:none', 'border:none', 'color:#64748b',
    'font-size:20px', 'cursor:pointer', 'line-height:1',
    'padding:0 4px', 'transition:color .15s',
  ].join(';')
  closeBtn.onmouseenter = () => { closeBtn.style.color = '#e2e8f0' }
  closeBtn.onmouseleave = () => { closeBtn.style.color = '#64748b' }
  closeBtn.onclick = () => showPanel(null)

  panelHeader.appendChild(panelTitle)
  panelHeader.appendChild(closeBtn)

  const panelBody = document.createElement('div')
  panelBody.style.cssText = 'padding:16px;flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:14px;'

  panel.appendChild(panelHeader)
  panel.appendChild(panelBody)
  container.appendChild(panel)

  // ── Helpers ────────────────────────────────────────────────────────────
  function row(label: string, value: string, mono = false): HTMLElement {
    const wrap = document.createElement('div')
    const lbl = document.createElement('div')
    lbl.style.cssText = 'color:#475569;font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px;'
    lbl.textContent = label
    const val = document.createElement('div')
    val.style.cssText = `color:#cbd5e1;font-size:13px;word-break:break-all;${mono ? 'font-family:monospace;font-size:12px;' : ''}`
    val.textContent = value || '—'
    wrap.appendChild(lbl)
    wrap.appendChild(val)
    return wrap
  }

  function agentLabel(agentType: string): string {
    const map: Record<string, string> = {
      'claude-code': '🟠 Claude Code',
      'codex': '🟣 Codex',
    }
    return map[agentType] ?? agentType
  }

  function toolLabel(tool: string | null): string {
    if (!tool) return '—'
    const map: Record<string, string> = {
      Bash: '⚡ Bash',
      Read: '📖 Read',
      Edit: '✏️ Edit',
      Write: '📝 Write',
      WebFetch: '🌐 WebFetch',
      Agent: '🤖 Agent',
    }
    return map[tool] ?? tool
  }

  // ── Public API ─────────────────────────────────────────────────────────
  function showPanel(session: Session | null): void {
    if (!session) {
      panel.style.transform = 'translateX(100%)'
      return
    }
    panelBody.innerHTML = ''

    // figure out agent type from session data if available
    const agentType = (session as Session & { agentType?: string }).agentType ?? ''
    if (agentType) panelBody.appendChild(row('探员类型', agentLabel(agentType)))

    const projectName = session.project.replace(/^-Users-[^-]+-/, '').replace(/-/g, '/')
    panelBody.appendChild(row('项目', projectName))
    panelBody.appendChild(row('当前任务', session.task || '—'))
    panelBody.appendChild(row('工具', toolLabel(session.tool)))
    if (session.target) panelBody.appendChild(row('目标', session.target, true))
    panelBody.appendChild(row('状态', session.state === 'active' ? '🟢 活跃' : '⚪ 归档'))
    panelBody.appendChild(row('会话 ID', session.session.slice(0, 8) + '…', true))

    const ts = session.ts ? new Date(session.ts).toLocaleTimeString('zh-CN') : '—'
    panelBody.appendChild(row('最后活动', ts))

    panel.style.transform = 'translateX(0)'
  }

  function updateStatus(status: 'connected' | 'disconnected' | 'reconnecting'): void {
    if (status === 'connected') {
      dot.style.cssText = 'display:inline-block;width:8px;height:8px;border-radius:50%;background:#4ade80;animation:none;'
      statusText.textContent = '已连接'
    } else if (status === 'disconnected') {
      dot.style.cssText = 'display:inline-block;width:8px;height:8px;border-radius:50%;background:#f87171;animation:none;'
      statusText.textContent = '已断开'
    } else {
      dot.style.cssText = 'display:inline-block;width:8px;height:8px;border-radius:50%;background:#fbbf24;animation:pulse-dot 1s ease-in-out infinite;'
      statusText.textContent = '重新连接中'
    }
  }

  // expose idle overlay control via a custom event so scene.ts can drive it
  // (scene will dispatch 'agents-session-count' on container)
  container.addEventListener('agents-session-count', (e) => {
    const count = (e as CustomEvent<number>).detail
    idleOverlay.style.display = count === 0 ? 'flex' : 'none'
  })

  return { showPanel, updateStatus }
}
