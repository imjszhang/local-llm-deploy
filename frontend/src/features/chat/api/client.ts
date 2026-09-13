import { SSEDecoder } from './sse'
import type { Answer, RequestSnapshot } from '../domain/types'
import { capabilities, wireParameters } from '../domain/parameters'

type Update = { content?: string; reasoning?: string; finishReason?: string; usage?: Answer['usage'] }
export class ChatError extends Error {
  constructor(message: string, public readonly status?: number) { super(message) }
}
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const MAX_BYTES = 8 * 1024 * 1024

export async function streamChat(request: RequestSnapshot, token: string, signal: AbortSignal,
  update: (value: Update) => void, fetcher: typeof fetch = globalThis.fetch): Promise<void> {
  const body = JSON.stringify({ model: request.model, messages: request.messages, ...wireParameters(request.parameters, request.backend, request.chat_controls), stream: true,
    ...(capabilities(request.backend).streamUsage ? { stream_options: { include_usage: true } } : {}) })
  if (new TextEncoder().encode(body).length > MAX_BYTES) throw new ChatError('上下文超过页面请求限制，请减少历史或新建会话')
  const headers: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'text/event-stream, application/json', 'X-Local-Console': '1' }
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetcher('/v1/chat/completions', { method: 'POST', headers, body, signal, credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' })
  if (!response.ok) {
    await response.body?.cancel()
    const messages: Record<number, string> = { 401: '访问凭据无效，请更新凭据后重试', 403: '没有访问该模型的权限', 404: '模型或聊天接口不存在，请刷新模型列表', 413: '上下文过大，请减少历史或新建会话', 429: '推理队列已满，请稍后手动重试', 503: '模型暂不可用或上一请求仍在收尾', 504: '请求等待超时，请稍后重试' }
    throw new ChatError(messages[response.status] ?? `请求失败（HTTP ${response.status}），请检查模型状态与上下文长度`, response.status)
  }
  const reader = response.body?.getReader()
  if (!reader) throw new ChatError('浏览器未提供可读取的响应')
  let done = false, finish = false, bytes = 0, json = ''
  const decoder = new TextDecoder('utf-8', { fatal: true })
  const isSSE = response.headers.get('Content-Type')?.includes('text/event-stream')
  function payload(text: string, full = false) {
    if (signal.aborted || done) return
    if (text.trim() === '[DONE]') { done = true; return }
    let parsed: unknown
    try { parsed = JSON.parse(text) } catch { throw new ChatError('模型返回了无效 JSON') }
    const root = object(parsed)
    if (root.error) throw new ChatError('模型在生成过程中返回错误，请检查模型状态、参数或上下文长度')
    const choices = Array.isArray(root.choices) ? root.choices : []
    const choice = object(choices.find(value => object(value).index === 0) ?? choices[0])
    const delta = object(full ? choice.message : choice.delta)
    const value: Update = {}
    if (typeof delta.content === 'string') value.content = delta.content
    if (typeof delta.reasoning_content === 'string') value.reasoning = delta.reasoning_content
    else if (request.backend === 'ollama' && typeof delta.reasoning === 'string') value.reasoning = delta.reasoning
    if (typeof choice.finish_reason === 'string' && choice.finish_reason) { value.finishReason = choice.finish_reason; finish = true }
    if (choice.finish_reason === 'tool_calls' || delta.tool_calls) throw new ChatError('当前文本测试台不支持工具调用，请使用文本回答配置')
    if (root.usage) {
      const usage = object(root.usage)
      value.usage = {}
      for (const key of ['prompt_tokens', 'completion_tokens', 'total_tokens'] as const) {
        const count = usage[key]
        if (typeof count === 'number' && Number.isSafeInteger(count) && count >= 0) value.usage[key] = count
      }
    }
    if (!choices.length && !root.usage) throw new ChatError('模型响应缺少消息数据')
    if (full && !('content' in value) && !('reasoning' in value)) throw new ChatError('模型响应缺少文本消息')
    update(value)
  }
  const sse = new SSEDecoder(text => payload(text))
  try {
    while (!done) {
      const chunk = await reader.read()
      if (signal.aborted) throw new DOMException('Cancelled', 'AbortError')
      if (chunk.done) break
      bytes += chunk.value.byteLength
      if (bytes > MAX_BYTES) throw new ChatError('响应超过 8 MiB 页面限制，已停止接收')
      const text = decoder.decode(chunk.value, { stream: true })
      if (isSSE) sse.push(text)
      else json += text
    }
    if (isSSE) {
      if (!done) { sse.push(decoder.decode()); sse.finish() }
      if (!done && !finish) throw new ChatError('连接在生成完成前中断，已保留部分回答')
    } else payload(json + decoder.decode(), true)
  } finally {
    try { await reader.cancel() } catch { /* The original stream error is authoritative. */ }
    reader.releaseLock()
  }
}
