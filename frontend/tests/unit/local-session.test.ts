import { describe, expect, it, vi } from 'vitest'
import { isLocalConsole, requestLocalSession } from '../../src/shared/auth/localSession'

describe('local console bootstrap', () => {
  it('only bootstraps explicit loopback names', async () => {
    expect(isLocalConsole('localhost')).toBe(true)
    expect(isLocalConsole('[::1]')).toBe(true)
    const fetcher = vi.fn<typeof fetch>()
    expect(await requestLocalSession('192.168.1.2', new AbortController().signal, fetcher)).toBeNull()
    expect(await requestLocalSession('localhost.attacker.example', new AbortController().signal, fetcher)).toBeNull()
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('uses a nonpersistent same-origin POST and accepts an expiring session', async () => {
    const value = { token: 'console-test-session', expires_at: Date.now() + 60000 }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(value))
    expect(await requestLocalSession('127.0.0.1', new AbortController().signal, fetcher)).toEqual(value)
    const [url, init] = fetcher.mock.calls[0]!
    expect(url).toBe('/console-api/v1/session')
    expect(init).toMatchObject({ method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store', headers: { 'X-Local-Console': '1' } })
    expect(init?.body).toBeUndefined()
  })
  it('falls back on old gateways, network failures, malformed or oversized responses', async () => {
    for (const response of [new Response('', { status: 404 }), Response.json({ token: 'bad\nkey', expires_at: Date.now() + 60000 }), Response.json({ token: 'old', expires_at: 0 }), new Response('x'.repeat(16385))]) {
      expect(await requestLocalSession('localhost', new AbortController().signal, vi.fn<typeof fetch>().mockResolvedValue(response))).toBeNull()
    }
    expect(await requestLocalSession('localhost', new AbortController().signal, vi.fn<typeof fetch>().mockRejectedValue(new TypeError('network')))).toBeNull()
  })
})
