import { afterEach, describe, expect, it, vi } from 'vitest'
import { copySession, createHistoryClient } from '../../src/features/chat/api/history'
import type { Session } from '../../src/features/chat/domain/types'

const session = (): Session => ({ id: 'test-session', title: '测试', model: 'model', system: '', parameters: { thinking: true, reasoning_effort: 'low' }, draft: '草稿', turns: [{ id: 'turn-1', user: '问题', selected: 0, answers: [{ id: 'answer-1', content: '回答', reasoning: '思考', status: 'complete', adopted: false, startedAt: 10, request: { model: 'model', backend: 'llama_cpp', parameters: {}, messages: [{ role: 'user', content: '问题' }] } }] }] })
const envelope = (value = session(), revision = 1) => ({ schema_version: 1, session: value, revision, updated_at: 100 })
afterEach(() => vi.useRealTimers())

describe('bounded private chat history API', () => {
  it('writes an explicit session schema with revision and header-only credentials', async () => {
    const input = session()
    Object.assign(input, { credential: 'must-not-persist', headers: { Authorization: 'Bearer secret' } })
    Object.assign(input.turns[0]!.answers[0]!.request, { api_key: 'nested-secret' })
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(envelope()))
    const result = await createHistoryClient(fetcher).put(input, 0, 'header-secret', new AbortController().signal)
    const [url, options] = fetcher.mock.calls[0]!
    expect(url).toBe('/chat-api/v1/sessions/test-session')
    expect(options).toMatchObject({ method: 'PUT', credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', headers: { Authorization: 'Bearer header-secret', 'X-Local-Console': '1' } })
    expect(JSON.parse(options!.body as string)).toEqual({ schema_version: 1, session: session(), revision: 0 })
    expect(options!.body).not.toContain('secret')
    expect(result.session).toEqual(session())
  })
  it('validates list entries and rejects duplicate IDs or foreign response identity', async () => {
    const summary = { id: 'test-session', title: '测试', model: 'model', revision: 2, created_at: 1, updated_at: 2 }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(Response.json({ schema_version: 1, sessions: [summary] }))
      .mockResolvedValueOnce(Response.json({ schema_version: 1, sessions: [summary, summary] }))
      .mockResolvedValueOnce(Response.json(envelope({ ...session(), id: 'different-session' })))
    const client = createHistoryClient(fetcher), signal = new AbortController().signal
    expect(await client.list('key', signal)).toEqual([summary])
    await expect(client.list('key', signal)).rejects.toMatchObject({ kind: 'invalid' })
    await expect(client.get('test-session', 'key', signal)).rejects.toMatchObject({ kind: 'invalid' })
    await expect(client.get('../escape', 'key', signal)).rejects.toMatchObject({ kind: 'invalid' })
    expect(fetcher).toHaveBeenCalledTimes(3)
  })
  it('classifies failures without exposing server error text', async () => {
    for (const [status, kind] of [[401, 'unauthorized'], [403, 'unauthorized'], [404, 'unavailable'], [409, 'conflict'], [413, 'too_large'], [400, 'invalid'], [507, 'capacity'], [500, 'http']] as const) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('server-secret', { status }))
      await expect(createHistoryClient(fetcher).list('key', new AbortController().signal)).rejects.toMatchObject({ kind })
      const second = createHistoryClient(vi.fn<typeof fetch>().mockResolvedValue(new Response('server-secret', { status })))
      await expect(second.list('key', new AbortController().signal)).rejects.not.toHaveProperty('message', expect.stringContaining('server-secret'))
    }
    await expect(createHistoryClient(vi.fn<typeof fetch>().mockResolvedValue(new Response('', { status: 404 }))).get('missing', 'key', new AbortController().signal)).rejects.toMatchObject({ kind: 'missing' })
  })
  it('rejects oversized requests before fetch and cancels oversized response streams', async () => {
    const fetcher = vi.fn<typeof fetch>()
    await expect(createHistoryClient(fetcher).put({ ...session(), draft: 'x'.repeat(8 * 1024 * 1024) }, 0, 'key', new AbortController().signal)).rejects.toMatchObject({ kind: 'too_large' })
    expect(fetcher).not.toHaveBeenCalled()
    const cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array(8 * 1024 * 1024 + 1025)) }, cancel })
    await expect(createHistoryClient(vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: { 'Content-Type': 'application/json' } }))).list('key', new AbortController().signal)).rejects.toMatchObject({ kind: 'too_large' })
    expect(cancel).toHaveBeenCalledOnce()
  })
  it('times out a hung request and preserves caller cancellation separately', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn<typeof fetch>((_url, options) => new Promise((_resolve, reject) => options!.signal!.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))))
    const client = createHistoryClient(fetcher)
    const request = client.list('key', new AbortController().signal)
    const timed = expect(request).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(15000); await timed
    const abort = new AbortController(), request2 = client.list('key', abort.signal)
    const stopped = expect(request2).rejects.toMatchObject({ name: 'AbortError' })
    abort.abort(); await stopped
  })
  it('validates delete confirmation and editable data before persistence', async () => {
    const client = createHistoryClient(vi.fn<typeof fetch>().mockResolvedValue(Response.json({ deleted: true, id: 'other-session' })))
    await expect(client.remove('test-session', 3, 'key', new AbortController().signal)).rejects.toMatchObject({ kind: 'invalid' })
    expect(() => copySession({ ...session(), parameters: { temperature: 3 } })).toThrow('会话数据格式无效')
    expect(() => copySession({ ...session(), turns: [{ id: 't', user: '', answers: [], selected: 1 }] })).toThrow('会话数据格式无效')
  })
})
