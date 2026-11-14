import { afterEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import McpWorkspace from '../../src/features/mcp/McpWorkspace.vue'
import { consoleContext, type ConsoleContext } from '../../src/app/context'
import type { HubSnapshot } from '../../src/features/mcp/api/types'

const sample = (): HubSnapshot => ({
  config: { schema_version: 1, revision: 7, listen: { host: '0.0.0.0', port: 8899, public_url: '' }, allowed_origins: [], services: [{ id: 'comfyui', name: '局域网 ComfyUI', kind: 'comfyui', enabled: true, command: '/tools/comfy-mcp', args: [], env: { PRIVATE_TOKEN: 'upstream-private' }, url: 'http://192.0.2.10:8188', allowed_tools: [] }] },
  runtime: { status: 'stopped', available: false, port: 8899 }, devices: [], tasks: [],
  connections: [{ service_id: 'comfyui', url: 'http://192.0.2.20:8899/mcp/comfyui', transport: 'streamable-http' }],
})
const wrappers: VueWrapper[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.unstubAllGlobals(); vi.useRealTimers() })
function setup(source: 'local' | 'manual' = 'manual', fetcher = vi.fn<typeof fetch>().mockImplementation(async () => Response.json(sample()))) {
  vi.stubGlobal('fetch', fetcher)
  const context = { credential: ref(source === 'local' ? 'local-console.auto' : 'root-private'), credentialSource: ref(source) } as unknown as ConsoleContext
  const wrapper = mount(McpWorkspace, { global: { provide: { [consoleContext as symbol]: context } } })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}
function button(wrapper: VueWrapper, name: string) {
  const found = wrapper.findAll('button').find(item => item.text() === name)
  if (!found) throw new Error('Missing button: ' + name)
  return found
}

describe('MCP Hub management workflow', () => {
  it('keeps automatic local browsing separate from administrator access', async () => {
    const { wrapper, fetcher } = setup('local')
    await flushPromises()
    expect(wrapper.text()).toContain('连接管理控制台')
    expect(fetcher).not.toHaveBeenCalled()
    await wrapper.get('#mcp-admin-key').setValue('explicit-admin')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Hub 运行管理')
    expect(fetcher.mock.calls[0]?.[1]?.headers).toMatchObject({ Authorization: 'Bearer explicit-admin' })
    expect(wrapper.text()).not.toContain('explicit-admin')
  })
  it('allows editing configuration while Hub is stopped, preserving hidden upstream credentials and the loaded revision', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, options) => options?.method === 'PUT' ? Response.json({ config: sample().config }) : Response.json(sample()))
    const { wrapper } = setup('manual', fetcher)
    await flushPromises()
    await button(wrapper, '编辑').trigger('click')
    const password = wrapper.get('input[aria-label="环境变量值"]')
    expect(password.attributes('type')).toBe('password')
    await wrapper.get('input[placeholder="局域网 ComfyUI"]').setValue('渲染服务')
    await wrapper.get('form.mcp-editor').trigger('submit')
    await flushPromises()
    const write = fetcher.mock.calls.find(([, options]) => options?.method === 'PUT')!
    const payload = JSON.parse(String(write[1]?.body))
    expect(payload.expected_revision).toBe(7)
    expect(payload.config.services[0]).toMatchObject({ name: '渲染服务', env: { PRIVATE_TOKEN: 'upstream-private' } })
    expect(wrapper.text()).toContain('配置已保存，将在下次启动时应用')
  })
  it('surfaces configuration conflicts without closing or losing the editor draft', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, options) => options?.method === 'PUT' ? Response.json({}, { status: 409 }) : Response.json(sample()))
    const { wrapper } = setup('manual', fetcher)
    await flushPromises(); await button(wrapper, '编辑').trigger('click')
    await wrapper.get('input[placeholder="局域网 ComfyUI"]').setValue('未保存的名字')
    await wrapper.get('form.mcp-editor').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('配置或运行状态已变化')
    expect(wrapper.get<HTMLInputElement>('input[placeholder="局域网 ComfyUI"]').element.value).toBe('未保存的名字')
  })
  it('issues a scoped device token and includes only that token in client configuration', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith('/devices') && options?.method === 'POST') return Response.json({ device: { id: 'device-1', name: 'video-remake', services: ['comfyui'], permissions: ['read', 'generate'] }, token: 'device-one-time' })
      return Response.json(sample())
    })
    const { wrapper } = setup('manual', fetcher)
    await flushPromises(); await button(wrapper, '设备与连接').trigger('click')
    await wrapper.get('input[placeholder="video-remake 或笔记本"]').setValue('video-remake')
    await wrapper.get('input[type="checkbox"][value="comfyui"]').setValue(true)
    await wrapper.get('form.mcp-device-form').trigger('submit'); await flushPromises()
    const configuration = wrapper.get('pre[aria-label="MCP 客户端配置"]').text()
    expect(configuration).toContain('device-one-time')
    expect(configuration).not.toContain('root-private')
    expect(wrapper.text()).toContain('只在这次创建后显示')
    await button(wrapper, '已保存，关闭').trigger('click')
    expect(wrapper.find('textarea[aria-label="设备访问令牌"]').exists()).toBe(false)
    expect(wrapper.get('pre[aria-label="MCP 客户端配置"]').text()).not.toContain('device-one-time')
  })
  it('distinguishes saved settings from the revision running in Hub', async () => {
    const value = sample()
    value.runtime = { ...value.runtime, status: 'running', available: true, applied_revision: 5 }
    const { wrapper } = setup('manual', vi.fn<typeof fetch>().mockImplementation(async () => Response.json(value)))
    await flushPromises()
    expect(wrapper.text()).toContain('配置 r7')
    expect(wrapper.text()).toContain('运行 r5')
    expect(wrapper.text()).toContain('新配置尚未应用，请重启 Hub')
  })

  it('clears management state and one-time credentials when locked', async () => {
    const { wrapper } = setup()
    await flushPromises(); await button(wrapper, '锁定管理').trigger('click')
    expect(wrapper.text()).toContain('连接管理控制台')
    expect(wrapper.text()).not.toContain('局域网 ComfyUI')
  })
})
