import type { Session } from './types'
export function exportSession(session: Session, format: 'json' | 'markdown') {
  // Only the explicit session schema is exported; credentials and HTTP headers never enter it.
  const text = format === 'json' ? JSON.stringify({ schema_version: 1, exported_at: new Date().toISOString(), session }, null, 2)
    : [`# ${session.title}`, `模型：${session.model}`, session.system ? `## System Prompt\n\n${session.system}` : '',
      ...session.turns.flatMap(turn => {
        const answer = turn.answers[turn.selected]
        return [`## 用户\n\n${turn.user}`, answer ? `## 模型（${answer.status}）\n\n${answer.content}` : '']
      })].filter(Boolean).join('\n\n')
  const url = URL.createObjectURL(new Blob([text], { type: format === 'json' ? 'application/json' : 'text/markdown;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = `conversation-${session.id}.${format === 'json' ? 'json' : 'md'}`
  anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
