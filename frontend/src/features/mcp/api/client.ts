import type { HubConfig, HubDevice, HubOperation, HubSnapshot, HubTool } from './types'

const BASE = '/mcp-admin-api/v1'
const LIMIT = 2 * 1024 * 1024
const segment = (value: string) => {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(value)) throw new HubApiError('请求标识无效', 0)
  return encodeURIComponent(value)
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
export class HubApiError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = 'HubApiError' }
}
export function createHubClient(fetcher: typeof fetch = (...args) => globalThis.fetch(...args)) {
  async function request<T>(path: string, key: string, signal: AbortSignal, method = 'GET', body?: unknown): Promise<T> {
    if (!key || key.startsWith('local-console.') || /[\r\n]/.test(key)) throw new HubApiError('请输入网关管理员 API Key；本机浏览会话没有管理权限。', 401)
    try {
      const response = await fetcher(BASE + path, {
        method, signal, credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
        headers: { Accept: 'application/json', Authorization: `Bearer ${key}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      if (response.status === 401 || response.status === 403) throw new HubApiError('管理员凭据无效或来源未获准，请重新连接。', response.status)
      if (Number(response.headers.get('Content-Length')) > LIMIT) throw new HubApiError('管理接口返回内容过大', 0)
      let text = ''
      const reader = response.body?.getReader()
      if (reader) {
        const decoder = new TextDecoder(); let size = 0
        try {
          while (true) {
            const chunk = await reader.read()
            if (chunk.done) break
            size += chunk.value.byteLength
            if (size > LIMIT) { await reader.cancel(); throw new HubApiError('管理接口返回内容过大', 0) }
            text += decoder.decode(chunk.value, { stream: true })
          }
          text += decoder.decode()
        } finally { reader.releaseLock() }
      } else text = await response.text()
      if (text.length > LIMIT) throw new HubApiError('管理接口返回内容过大', 0)
      let parsed: unknown
      try { parsed = text ? JSON.parse(text) : {} } catch { throw new HubApiError('管理接口响应格式无效', response.status) }
      if (!response.ok) {
        const detail = record(parsed) && record(parsed.error) && typeof parsed.error.message === 'string' ? parsed.error.message : ''
        const message = response.status === 409 ? '配置或运行状态已变化，请刷新后重新操作。' : detail || '操作未完成，请检查服务状态。'
        throw new HubApiError(message.split(key).join('[已隐藏]').slice(0, 500), response.status)
      }
      return parsed as T
    } catch (error) {
      if (signal.aborted) throw new DOMException('请求已取消', 'AbortError')
      if (error instanceof HubApiError) throw error
      throw new HubApiError('无法连接管理接口，请检查 8888 网关。', 0)
    }
  }
  return {
    snapshot: async (key: string, signal: AbortSignal) => {
      const value = await request<HubSnapshot>('/snapshot', key, signal)
      if (!record(value) || !record(value.config) || !record(value.config.listen) || !Array.isArray(value.config.services) || !record(value.runtime) || !Array.isArray(value.devices) || !Array.isArray(value.tasks) || !Array.isArray(value.connections)) throw new HubApiError('管理快照格式无效', 0)
      return value
    },
    save: (config: HubConfig, expected_revision: number, key: string, signal: AbortSignal) => request<{ config: HubConfig }>('/config', key, signal, 'PUT', { config, expected_revision }),
    action: (action: 'start' | 'stop' | 'restart', key: string, signal: AbortSignal) => request<{ operation: HubOperation }>('/actions', key, signal, 'POST', { action }),
    operation: (id: string, key: string, signal: AbortSignal) => request<{ operation: HubOperation }>(`/operations/${segment(id)}`, key, signal),
    logs: (key: string, signal: AbortSignal) => request<{ logs: string }>('/logs', key, signal),
    createDevice: (name: string, services: string[], permissions: string[], key: string, signal: AbortSignal) => request<{ device: HubDevice; token: string }>('/devices', key, signal, 'POST', { name, services, permissions }),
    revokeDevice: (id: string, key: string, signal: AbortSignal) => request(`/devices/${segment(id)}`, key, signal, 'DELETE'),
    tools: (id: string, key: string, signal: AbortSignal) => request<{ tools: HubTool[] }>(`/services/${segment(id)}/tools`, key, signal),
    probe: (id: string, key: string, signal: AbortSignal) => request<Record<string, unknown>>(`/services/${segment(id)}/probe`, key, signal, 'POST', {}),
  }
}
