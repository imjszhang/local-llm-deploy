import { describe, expect, it, vi } from 'vitest'
import { createHubClient, HubApiError } from '../../src/features/mcp/api/client'
import type { HubSnapshot } from '../../src/features/mcp/api/types'
export const hubSnapshot = (): HubSnapshot => ({
  config: { schema_version: 1, revision: 4, listen: { host: '0.0.0.0', port: 8899, public_url: '' }, allowed_origins: [], services: [{ id: 'comfyui', name: '局域网 ComfyUI', kind: 'comfyui', enabled: true, command: '/tools/comfy-mcp', args: [], env: { COMFY_BIN: '/tools/comfy' }, url: 'http://192.0.2.10:8188', allowed_tools: [] }] },
  runtime: { status: 'stopped', pid: null, available: false, port: 8899 }, devices: [], tasks: [],
  connections: [{ service_id: 'comfyui', url: 'http://192.0.2.20:8899/mcp/comfyui', transport: 'streamable-http' }],
})
const signal = () => new AbortController().signal

describe('MCP admin boundary', () => {
  it('rejects automatic console grants before making an admin request', async () => {
    const fetcher = vi.fn<typeof fetch>()
    const client = createHubClient(fetcher)
    await expect(client.snapshot('local-console.session', signal())).rejects.toMatchObject({ status: 401 })
    await expect(client.snapshot('', signal())).rejects.toMatchObject({ status: 401 })
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('keeps root credentials on same-origin admin requests and uses optimistic configuration revisions', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ config: hubSnapshot().config }))
    await createHubClient(fetcher).save(hubSnapshot().config, 4, 'root-private', signal())
    const [url, options] = fetcher.mock.calls[0]!
    expect(url).toBe('/mcp-admin-api/v1/config')
    expect(options).toMatchObject({ method: 'PUT', redirect: 'error', credentials: 'omit', cache: 'no-store', headers: { Authorization: 'Bearer root-private' } })
    expect(JSON.parse(String(options?.body))).toMatchObject({ expected_revision: 4 })
    expect(String(options?.body)).not.toContain('root-private')
  })
  it('rejects path traversal for device or service targets', async () => {
    const fetcher = vi.fn<typeof fetch>()
    const client = createHubClient(fetcher)
    expect(() => client.revokeDevice('../config', 'root', signal())).toThrow(HubApiError)
    expect(() => client.tools('comfyui/../../config', 'root', signal())).toThrow(HubApiError)
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('reports conflicts and authentication failures without echoing the root key', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ error: { message: 'root-private' } }, { status: 409 }))
    const client = createHubClient(fetcher)
    await expect(client.action('start', 'root-private', signal())).rejects.toThrow('配置或运行状态已变化')
    fetcher.mockResolvedValue(Response.json({ error: { message: 'root-private' } }, { status: 500 }))
    await expect(client.action('start', 'root-private', signal())).rejects.toThrow('[已隐藏]')
    fetcher.mockResolvedValue(new Response('root-private', { status: 401 }))
    await expect(client.snapshot('root-private', signal())).rejects.toMatchObject({ status: 401 })
  })
  it('fails malformed or oversized responses instead of treating them as connected', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ config: {} }))
    const client = createHubClient(fetcher)
    await expect(client.snapshot('root', signal())).rejects.toThrow('快照格式无效')
    fetcher.mockResolvedValue(new Response('{}', { headers: { 'Content-Length': String(3 * 1024 * 1024) } }))
    await expect(client.snapshot('root', signal())).rejects.toThrow('返回内容过大')
  })
})
