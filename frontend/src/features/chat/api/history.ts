import type { ChatControls } from '../../monitor/api/types'
import type { Answer, Parameters, RequestSnapshot, Session, Turn, WireMessage } from '../domain/types'

export interface SessionSummary { id: string; title: string; model: string; revision: number; created_at: number; updated_at: number }
export interface StoredSession { schema_version: 1; session: Session; revision: number; updated_at: number }
export type HistoryErrorKind = 'unauthorized' | 'unavailable' | 'missing' | 'conflict' | 'invalid' | 'too_large' | 'capacity' | 'network' | 'timeout' | 'http'
export class HistoryApiError extends Error {
  constructor(public readonly kind: HistoryErrorKind, public readonly status?: number) {
    super({ unauthorized: '访问凭据失效，未保存的修改仍保留在当前页面', unavailable: '当前网关尚未提供对话保存接口，修改暂存当前页面', missing: '保存的会话已不存在，请另存为新会话', conflict: '会话已被其他页面更新，请另存或载入已保存版本', invalid: '会话数据格式无效，修改仍保留在当前页面', too_large: '会话超过 8 MiB 保存限制，请导出后新建会话', capacity: '本机历史已达 1000 个会话上限，请导出并删除不需要的会话', network: '无法连接对话保存服务，请手动重试', timeout: '保存服务响应超时，请手动重试', http: '对话保存失败，修改仍保留在当前页面' }[kind])
    this.name = 'HistoryApiError'
  }
}
export interface HistoryClient {
  list(token: string, signal: AbortSignal): Promise<SessionSummary[]>
  get(id: string, token: string, signal: AbortSignal): Promise<StoredSession>
  put(session: Session, revision: number, token: string, signal: AbortSignal): Promise<StoredSession>
  remove(id: string, revision: number, token: string, signal: AbortSignal): Promise<void>
}
const MAX_BYTES = 8 * 1024 * 1024
const MAX_RESPONSE_BYTES = MAX_BYTES + 1024
const API = '/chat-api/v1/sessions'
const invalid = (): never => { throw new HistoryApiError('invalid') }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid()
  return value as Record<string, unknown>
}
function string(value: unknown): string { return typeof value === 'string' ? value : invalid() }
function number(value: unknown): number { return typeof value === 'number' && Number.isFinite(value) ? value : invalid() }
function integer(value: unknown, minimum = 0): number { const result = number(value); return Number.isSafeInteger(result) && result >= minimum ? result : invalid() }
function boolean(value: unknown): boolean { return typeof value === 'boolean' ? value : invalid() }
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : invalid() }
function identifier(value: unknown): string { const result = string(value); return /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/.test(result) ? result : invalid() }
function parameters(value: unknown): Parameters {
  const input = object(value), result: Parameters = {}
  for (const key of ['temperature', 'top_p', 'max_tokens', 'seed', 'reasoning_budget_tokens'] as const) if (input[key] !== undefined) result[key] = number(input[key])
  if (result.temperature !== undefined && (result.temperature < 0 || result.temperature > 2)) return invalid()
  if (result.top_p !== undefined && (result.top_p <= 0 || result.top_p > 1)) return invalid()
  for (const key of ['max_tokens', 'reasoning_budget_tokens'] as const) if (result[key] !== undefined && (!Number.isSafeInteger(result[key]) || result[key]! < 1 || result[key]! > 131072)) return invalid()
  if (result.seed !== undefined && (!Number.isSafeInteger(result.seed) || result.seed < 0)) return invalid()
  if (input.thinking !== undefined) result.thinking = boolean(input.thinking)
  if (input.reasoning_effort !== undefined) {
    result.reasoning_effort = string(input.reasoning_effort)
    if (!/^[a-z][a-z0-9_-]{0,23}$/.test(result.reasoning_effort) || result.reasoning_effort === 'none') return invalid()
  }
  return result
}
function controls(value: unknown): ChatControls | null {
  if (value === null) return null
  const input = object(value)
  if (input.source !== 'configured') return invalid()
  return { thinking: boolean(input.thinking), reasoning_efforts: array(input.reasoning_efforts).map(string), reasoning_budget: boolean(input.reasoning_budget), default_thinking: input.default_thinking === null ? null : boolean(input.default_thinking), default_effort: input.default_effort === null ? null : string(input.default_effort), source: 'configured' }
}
function message(value: unknown): WireMessage {
  const input = object(value)
  if (!['system', 'user', 'assistant'].includes(string(input.role))) return invalid()
  return { role: input.role as WireMessage['role'], content: string(input.content) }
}
function request(value: unknown): RequestSnapshot {
  const input = object(value)
  return { model: string(input.model), backend: string(input.backend), parameters: parameters(input.parameters), messages: array(input.messages).map(message), ...(input.chat_controls !== undefined ? { chat_controls: controls(input.chat_controls) } : {}) }
}
function answer(value: unknown): Answer {
  const input = object(value)
  if (!['waiting', 'streaming', 'complete', 'stopped', 'error'].includes(string(input.status))) return invalid()
  const result: Answer = { id: identifier(input.id), content: string(input.content), reasoning: string(input.reasoning), status: input.status as Answer['status'], adopted: boolean(input.adopted), startedAt: number(input.startedAt), request: request(input.request) }
  for (const key of ['error', 'finishReason'] as const) if (input[key] !== undefined) result[key] = string(input[key])
  for (const key of ['firstContentMs', 'durationMs'] as const) if (input[key] !== undefined) result[key] = number(input[key])
  if (input.usage !== undefined) {
    const usage = object(input.usage)
    result.usage = {}
    for (const key of ['prompt_tokens', 'completion_tokens', 'total_tokens'] as const) if (usage[key] !== undefined) result.usage[key] = integer(usage[key])
  }
  return result
}
function turn(value: unknown): Turn {
  const input = object(value), answers = array(input.answers).map(answer), selected = integer(input.selected)
  if (selected >= Math.max(1, answers.length)) return invalid()
  return { id: identifier(input.id), user: string(input.user), answers, selected }
}
/** Explicit schema copying excludes credentials, headers and arbitrary object properties. */
export function copySession(value: unknown): Session {
  const input = object(value)
  return { id: identifier(input.id), title: string(input.title), model: string(input.model), system: string(input.system), parameters: parameters(input.parameters), turns: array(input.turns).map(turn), draft: string(input.draft) }
}
function stored(value: unknown, id: string): StoredSession {
  const input = object(value), session = copySession(input.session)
  if (input.schema_version !== 1 || session.id !== id) return invalid()
  return { schema_version: 1, session, revision: integer(input.revision, 1), updated_at: number(input.updated_at) }
}
export function createHistoryClient(fetcher: typeof fetch = (...args) => globalThis.fetch(...args)): HistoryClient {
  async function json(path: string, method: string, token: string, signal: AbortSignal, body?: unknown): Promise<unknown> {
    const controller = new AbortController()
    let timedOut = false
    const abort = () => controller.abort()
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    const timeout = setTimeout(() => { timedOut = true; controller.abort() }, 15000)
    try {
      const textBody = body === undefined ? undefined : JSON.stringify(body)
      if (textBody && new TextEncoder().encode(textBody).byteLength > MAX_BYTES) throw new HistoryApiError('too_large')
      const headers: Record<string, string> = { Accept: 'application/json', 'X-Local-Console': '1' }
      if (token) headers.Authorization = `Bearer ${token}`
      if (textBody !== undefined) headers['Content-Type'] = 'application/json'
      const response = await fetcher(path, { method, body: textBody, signal: controller.signal, headers, credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' })
      if (!response.ok) {
        await response.body?.cancel()
        const kind: HistoryErrorKind = response.status === 401 || response.status === 403 ? 'unauthorized' : response.status === 409 ? 'conflict' : response.status === 404 ? path === API ? 'unavailable' : 'missing' : response.status === 413 ? 'too_large' : response.status === 507 ? 'capacity' : response.status === 400 || response.status === 422 ? 'invalid' : 'http'
        throw new HistoryApiError(kind, response.status)
      }
      if (!response.headers.get('Content-Type')?.includes('application/json')) { await response.body?.cancel(); throw new HistoryApiError(path === API ? 'unavailable' : 'invalid') }
      if (Number(response.headers.get('Content-Length')) > MAX_RESPONSE_BYTES) { await response.body?.cancel(); throw new HistoryApiError('too_large') }
      const reader = response.body?.getReader()
      if (!reader) throw new HistoryApiError('invalid')
      let text = '', bytes = 0
      const decoder = new TextDecoder('utf-8', { fatal: true })
      try {
        while (true) {
          const chunk = await reader.read()
          if (controller.signal.aborted) throw new DOMException('Cancelled', 'AbortError')
          if (chunk.done) break
          bytes += chunk.value.byteLength
          if (bytes > MAX_RESPONSE_BYTES) throw new HistoryApiError('too_large')
          text += decoder.decode(chunk.value, { stream: true })
        }
        text += decoder.decode()
      } finally { try { await reader.cancel() } catch { /* Preserve the original read error. */ } reader.releaseLock() }
      try { return JSON.parse(text) as unknown } catch { throw new HistoryApiError('invalid') }
    } catch (cause) {
      if (signal.aborted) throw new DOMException('Cancelled', 'AbortError')
      if (timedOut) throw new HistoryApiError('timeout')
      if (cause instanceof HistoryApiError) throw cause
      throw new HistoryApiError('network')
    } finally { clearTimeout(timeout); signal.removeEventListener('abort', abort) }
  }
  return {
    async list(token, signal) {
      const input = object(await json(API, 'GET', token, signal))
      if (input.schema_version !== 1) return invalid()
      const entries = array(input.sessions)
      if (entries.length > 1000) return invalid()
      const seen = new Set<string>()
      return entries.map(entry => {
        const item = object(entry), id = identifier(item.id)
        if (seen.has(id)) return invalid()
        seen.add(id)
        return { id, title: string(item.title), model: string(item.model), revision: integer(item.revision, 1), created_at: number(item.created_at), updated_at: number(item.updated_at) }
      })
    },
    async get(id, token, signal) { return stored(await json(`${API}/${encodeURIComponent(identifier(id))}`, 'GET', token, signal), id) },
    async put(session, revision, token, signal) {
      const clean = copySession(session)
      return stored(await json(`${API}/${encodeURIComponent(clean.id)}`, 'PUT', token, signal, { schema_version: 1, session: clean, revision: integer(revision) }), clean.id)
    },
    async remove(id, revision, token, signal) {
      const result = object(await json(`${API}/${encodeURIComponent(identifier(id))}`, 'DELETE', token, signal, { revision: integer(revision) }))
      if (result.deleted !== true || result.id !== id) return invalid()
    },
  }
}
