import { describe, expect, it, vi } from 'vitest'
import { createMonitorClient, MonitorApiError } from '../../src/api/client'
import { parseDetail, parseSnapshot } from '../../src/domain/validation'
import { sampleDetail, sampleSnapshot } from './data-fixtures'
const signal = () => new AbortController().signal

describe('monitor API boundary', () => {
  it('sends credentials only to same-origin allowlisted protected endpoints', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async input => {
      const path = String(input)
      if (path === '/api/models') return Response.json({ models: [], lanes: {} })
      if (path === '/api/system') return Response.json({ ...sampleSnapshot().system, cached_at: Date.now() / 1000 })
      return Response.json(path.endsWith('snapshot') ? sampleSnapshot() : sampleDetail('chat'))
    })
    const client = createMonitorClient(fetcher)
    await client.snapshot(signal(), 'private-key')
    await client.detail('chat', signal(), 'private-key', false)
    await client.publicOverview(signal())
    for (const [url, init] of fetcher.mock.calls) {
      expect(String(url)).not.toContain('private-key')
      expect(String(url)).toMatch(/^\/(monitor-api\/v1|api)\//)
      expect(init?.credentials).toBe('omit')
      expect(init?.redirect).toBe('error')
      expect((init?.headers as Record<string, string>).Authorization).toBe(String(url).startsWith('/monitor-api/') ? 'Bearer private-key' : undefined)
    }
  })
  it('rejects ambiguous keys without fetching and encodes a model as one URL segment', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(sampleDetail('a/b')))
    const client = createMonitorClient(fetcher)
    await expect(client.detail('..', signal(), '', false)).rejects.toThrow(MonitorApiError)
    expect(fetcher).not.toHaveBeenCalled()
    await client.detail('a/b', signal(), '', false)
    expect(fetcher.mock.calls[0]?.[0]).toBe('/monitor-api/v1/models/a%2Fb')
  })
  it('does not copy backend output without explicit per-request consent', () => {
    expect(parseDetail(sampleDetail(), 'chat').slots.items[0]?.generated).toBeUndefined()
    expect(parseDetail(sampleDetail(), 'chat', true).slots.items[0]?.generated).toBe('sensitive output')
  })
  it('validates nested numbers and preserves null readings', () => {
    const data = sampleSnapshot()
    data.system!.cpu.user = null
    expect(parseSnapshot(data).system?.cpu.user).toBeNull()
    data.lanes.chat.active = Number.NaN
    expect(() => parseSnapshot(data)).toThrow('监控响应格式无效')
    expect(() => parseSnapshot({ ...sampleSnapshot(), models: [{}] })).toThrow()
    expect(() => parseSnapshot({ ...sampleSnapshot(), schema_version: 2 })).toThrow()
    expect(() => parseDetail(sampleDetail('other'), 'chat')).toThrow()
  })
  it('sanitizes network and server errors without echoing secret data', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error('Authorization: private-key; https://private-host'))
    const client = createMonitorClient(fetcher)
    await expect(client.snapshot(signal(), 'private-key')).rejects.toThrow('无法连接监控接口')
    fetcher.mockResolvedValue(new Response('private-key server-debug-data', { status: 503 }))
    try { await client.snapshot(signal(), 'private-key') } catch (error) {
      expect(String(error)).not.toContain('private-key')
      expect(String(error)).not.toContain('server-debug-data')
    }
  })
  it('reports invalid JSON and oversized bodies as payload failures', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('<html>error</html>'))
    const client = createMonitorClient(fetcher)
    await expect(client.snapshot(signal(), '')).rejects.toMatchObject({ kind: 'invalid' })
    fetcher.mockResolvedValue(new Response('{}', { headers: { 'Content-Length': String(3 * 1024 * 1024) } }))
    await expect(client.snapshot(signal(), '')).rejects.toMatchObject({ kind: 'invalid' })
  })
  it('keeps partially available public readings null instead of inventing data', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async input => String(input) === '/api/models'
      ? Response.json({ models: [{}], lanes: {} }) : new Response('unavailable', { status: 503 }))
    expect(await createMonitorClient(fetcher).publicOverview(signal())).toEqual({ model_count: 1, lanes: {}, system: null, last_success_at: null })
  })
})
