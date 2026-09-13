import type { Parameters, Session, WireMessage } from './types'

export function contextMessages(session: Session, through = session.turns.length): WireMessage[] {
  const messages: WireMessage[] = []
  if (session.system.trim()) messages.push({ role: 'system', content: session.system })
  for (const turn of session.turns.slice(0, through)) {
    messages.push({ role: 'user', content: turn.user })
    const answer = turn.answers[turn.selected]
    if (answer && (answer.status === 'complete' || answer.adopted) && answer.content) {
      messages.push({ role: 'assistant', content: answer.content })
    }
  }
  return messages
}

export function validateParameters(parameters: Parameters) {
  for (const key of ['temperature', 'top_p', 'max_tokens', 'seed', 'reasoning_budget_tokens'] as const) {
    const value = parameters[key]
    if (value === undefined) continue
    if (!Number.isFinite(value)) throw new Error(`${key} 必须为有限数值`)
    if (key === 'temperature' && (value < 0 || value > 2)) throw new Error('temperature 范围为 0–2')
    if (key === 'top_p' && (value <= 0 || value > 1)) throw new Error('top_p 范围为大于 0、至多 1')
    if (key === 'max_tokens' && (!Number.isInteger(value) || value < 1 || value > 131072)) throw new Error('最大输出 token 必须为 1–131072 的整数')
    if (key === 'seed' && (!Number.isSafeInteger(value) || value < 0)) throw new Error('seed 必须为非负安全整数')
    if (key === 'reasoning_budget_tokens' && (!Number.isInteger(value) || value < 1 || value > 131072)) throw new Error('思考预算必须为 1–131072 的整数')
  }
  if (parameters.thinking !== undefined && typeof parameters.thinking !== 'boolean') throw new Error('深度思考必须为开或关')
  if (parameters.reasoning_effort !== undefined && (typeof parameters.reasoning_effort !== 'string' || !/^[a-z][a-z0-9_-]{0,23}$/.test(parameters.reasoning_effort))) throw new Error('推理强度无效')
  if (parameters.reasoning_budget_tokens !== undefined && parameters.max_tokens !== undefined && parameters.reasoning_budget_tokens >= parameters.max_tokens) throw new Error('思考预算需小于最大输出 token，为最终回答留出空间')
}
