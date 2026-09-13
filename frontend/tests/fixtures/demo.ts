import { createDetail, createSnapshot } from './monitor'

/** Dev-only: compiled away from production; no real gateway requests or secrets. */
export function enableDemo() {
  const realFetch = window.fetch.bind(window)
  window.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), location.origin)
    if (url.origin !== location.origin) return realFetch(input, init)
    if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    const snapshot = createSnapshot()
    const json = (data: unknown) => Promise.resolve(new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } }))
    if (url.pathname === '/monitor-api/v1/snapshot') return json(snapshot)
    if (url.pathname.startsWith('/monitor-api/v1/models/')) return json(createDetail(decodeURIComponent(url.pathname.split('/').pop()!), url.searchParams.get('include_output') === '1'))
    if (url.pathname === '/api/system') return json({ ...snapshot.system, cached_at: snapshot.generated_at / 1000 })
    if (url.pathname === '/api/models') return json({ models: snapshot.models.filter(m => m.routing.available), lanes: snapshot.lanes })
    return realFetch(input, init)
  }
}
