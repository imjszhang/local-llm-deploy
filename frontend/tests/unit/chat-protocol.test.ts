import { describe, expect, it } from 'vitest'
import { SSEDecoder } from '../../src/features/chat/api/sse'
import { streamChat } from '../../src/features/chat/api/client'
import { contextMessages, validateParameters } from '../../src/features/chat/domain/context'
import type { Session, RequestSnapshot } from '../../src/features/chat/domain/types'

const request: RequestSnapshot = { model: 'test', backend: 'llama', messages: [{ role: 'user', content: '你好' }], parameters: {} }
function fetchStream(text: string, type = 'text/event-stream') {
  const bytes = new TextEncoder().encode(text)
  return (async () => new Response(new ReadableStream({ start(controller) {
    for (const byte of bytes) controller.enqueue(new Uint8Array([byte]))
    controller.close()
  } }), { headers: { 'Content-Type': type } })) as typeof fetch
}
describe('chat protocol', () => {
  it('decodes CRLF split across chunks, comments and multiline data', () => {
    const values: string[] = [], parser = new SSEDecoder(value => values.push(value))
    for (const char of ': beat\r\n\r\ndata: one\r\ndata: two\r\n\r\n') parser.push(char)
    parser.finish()
    expect(values).toEqual(['one\ntwo'])
  })
  it('rejects truncated and oversized events', () => {
    const parser = new SSEDecoder(() => {}, 5)
    expect(() => parser.push('data: longer\n')).toThrow()
    const truncated = new SSEDecoder(() => {})
    truncated.push('data: {}\n')
    expect(() => truncated.finish()).toThrow()
  })
  it('handles multibyte content and usage after finish reason', async () => {
    const updates: unknown[] = []
    await streamChat(request, '', new AbortController().signal, value => updates.push(value), fetchStream(
      ': heartbeat\n\ndata: {"choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}\n\n' +
      'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n' +
      'data: {"choices":[],"usage":{"completion_tokens":2}}\n\ndata: [DONE]\n\n'))
    expect(updates).toEqual([{ content: '你好' }, { finishReason: 'stop' }, { usage: { completion_tokens: 2 } }])
  })
  it('rejects stream errors and incomplete termination', async () => {
    await expect(streamChat(request, '', new AbortController().signal, () => {}, fetchStream('data: {"error":{"message":"private"}}\n\n'))).rejects.toThrow('生成过程中')
    await expect(streamChat(request, '', new AbortController().signal, () => {}, fetchStream('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'))).rejects.toThrow('中断')
  })
  it('accepts JSON fallback', async () => {
    const updates: unknown[] = []
    await streamChat(request, '', new AbortController().signal, value => updates.push(value), fetchStream('{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}', 'application/json'))
    expect(updates).toEqual([{ content: 'ok', finishReason: 'stop' }])
  })
  it('requests usage only from verified providers and maps Ollama reasoning', async () => {
    let body = ''
    const fetcher = (async (_url: unknown, options: RequestInit) => {
      body = String(options.body)
      return new Response('data: {"choices":[{"delta":{"reasoning":"thinking","content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n', { headers: { 'Content-Type': 'text/event-stream' } })
    }) as typeof fetch
    const updates: unknown[] = []
    await streamChat({ ...request, backend: 'ollama' }, '', new AbortController().signal, v => updates.push(v), fetcher)
    expect(JSON.parse(body).stream_options).toEqual({ include_usage: true })
    expect(updates).toEqual([{ content: 'answer', reasoning: 'thinking', finishReason: 'stop' }])
    await streamChat({ ...request, backend: 'external_http' }, '', new AbortController().signal, () => {}, fetcher)
    expect(JSON.parse(body).stream_options).toBeUndefined()
  })
})
describe('chat context', () => {
  it('includes only the selected complete or explicitly adopted answer', () => {
    const session: Session = { id: 's', title: '', model: 'test', system: 'system', parameters: {}, draft: '', turns: [{ id: 't', user: 'question', selected: 1, answers: [
      { id: 'a', content: 'old', reasoning: '', status: 'complete', adopted: false, startedAt: 0, request },
      { id: 'b', content: 'partial', reasoning: '', status: 'stopped', adopted: false, startedAt: 0, request },
    ] }] }
    expect(contextMessages(session).map(m => m.content)).toEqual(['system', 'question'])
    session.turns[0]!.answers[1]!.adopted = true
    expect(contextMessages(session).map(m => m.content)).toEqual(['system', 'question', 'partial'])
  })
  it('rejects invalid parameter values', () => {
    expect(() => validateParameters({ max_tokens: 0 })).toThrow()
    expect(() => validateParameters({ temperature: NaN })).toThrow()
    expect(() => validateParameters({ top_p: 1, seed: 0 })).not.toThrow()
  })
})
