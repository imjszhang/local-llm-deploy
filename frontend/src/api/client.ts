import type { ModelDetail, PublicOverview, Snapshot } from './types'
import { InvalidPayload, parseDetail, parsePublicModels, parsePublicSystem, parseSnapshot } from '../domain/validation'

export class MonitorApiError extends Error {
  constructor(public readonly kind: 'unauthorized' | 'unsupported' | 'http' | 'network' | 'invalid', public readonly status: number | null = null) {
    super(({ unauthorized: '需要有效的访问凭据', unsupported: '当前网关尚未提供监控 API v1', http: '监控接口暂时不可用', network: '无法连接监控接口', invalid: '监控响应格式无效' })[kind])
    this.name = 'MonitorApiError'
  }
}
export interface MonitorClient {
  snapshot(signal: AbortSignal, token: string): Promise<Snapshot>
  detail(key: string, signal: AbortSignal, token: string, includeOutput: boolean): Promise<ModelDetail>
  publicOverview(signal: AbortSignal): Promise<PublicOverview>
}
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024
const API = '/monitor-api/v1/'

/** No caller-supplied URL and no ambient cookie credentials. The token is never returned. */
export function createMonitorClient(fetcher: typeof fetch = (...args) => globalThis.fetch(...args)): MonitorClient {
  async function request(path: string, signal: AbortSignal, token = ''): Promise<unknown> {
    if (!(path === '/api/models' || path === '/api/system' || path === API + 'snapshot' ||
      /^\/monitor-api\/v1\/models\/[^/?#]+(?:\?include_output=1)?$/.test(path))) {
      throw new MonitorApiError('invalid')
    }
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (token && path.startsWith(API)) headers.Authorization = `Bearer ${token}`
    try {
      const response = await fetcher(path, { method: 'GET', signal, headers, credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', redirect: 'error' })
      if (response.status === 401) throw new MonitorApiError('unauthorized', 401)
      if (response.status === 404 && path === API + 'snapshot') throw new MonitorApiError('unsupported', 404)
      if (!response.ok) throw new MonitorApiError('http', response.status)
      if (Number(response.headers.get('Content-Length')) > MAX_RESPONSE_BYTES) throw new MonitorApiError('invalid')
      const reader = response.body?.getReader()
      let text = ''
      if (reader) {
        const decoder = new TextDecoder(); let bytes = 0
        try {
          while (true) {
            const chunk = await reader.read()
            if (chunk.done) break
            bytes += chunk.value.byteLength
            if (bytes > MAX_RESPONSE_BYTES) { await reader.cancel(); throw new MonitorApiError('invalid') }
            text += decoder.decode(chunk.value, { stream: true })
          }
          text += decoder.decode()
        } finally { reader.releaseLock() }
      } else text = await response.text()
      if (text.length > MAX_RESPONSE_BYTES) throw new MonitorApiError('invalid')
      try { return JSON.parse(text) as unknown } catch { throw new MonitorApiError('invalid') }
    } catch (error) {
      if (signal.aborted) throw new DOMException('Request cancelled', 'AbortError')
      if (error instanceof MonitorApiError) throw error
      throw new MonitorApiError('network')
    }
  }
  const validated = async <T>(work: Promise<unknown>, parse: (value: unknown) => T): Promise<T> => {
    try { return parse(await work) } catch (error) {
      if (error instanceof InvalidPayload) throw new MonitorApiError('invalid')
      throw error
    }
  }
  return {
    snapshot: (signal, token) => validated(request(API + 'snapshot', signal, token), parseSnapshot),
    detail: (key, signal, token, includeOutput) => {
      if (!key || key === '.' || key === '..' || key.length > 512) return Promise.reject(new MonitorApiError('invalid'))
      const path = API + 'models/' + encodeURIComponent(key) + (includeOutput ? '?include_output=1' : '')
      return validated(request(path, signal, token), value => parseDetail(value, key, includeOutput))
    },
    publicOverview: async signal => {
      const results = await Promise.allSettled([
        validated(request('/api/models', signal), parsePublicModels),
        validated(request('/api/system', signal), parsePublicSystem),
      ])
      if (signal.aborted) throw new DOMException('Request cancelled', 'AbortError')
      const [models, system] = results
      if (models.status === 'rejected' && system.status === 'rejected') throw models.reason
      return {
        lanes: models.status === 'fulfilled' ? models.value.lanes : {},
        model_count: models.status === 'fulfilled' ? models.value.model_count : null,
        system: system.status === 'fulfilled' ? system.value.system : null,
        last_success_at: system.status === 'fulfilled' ? system.value.last_success_at : null,
      }
    },
  }
}
