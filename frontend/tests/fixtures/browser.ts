import type { Page } from '@playwright/test'
import type { Snapshot } from '../../src/api/types'
import { createDetail, createSnapshot } from './monitor'

export async function mockMonitor(page: Page, options: { authenticated?: boolean; unsupported?: boolean } = {}) {
  const state = { snapshot: createSnapshot(), snapshotStatus: 200, snapshots: 0, details: [] as string[], publicRequests: 0, methods: [] as string[], errors: [] as string[] }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/monitor-api/**', async route => {
    const request = route.request()
    state.methods.push(request.method())
    if (options.unsupported) return route.fulfill({ status: 404, json: { error: { message: 'Not found' } } })
    if (options.authenticated && request.headers().authorization !== 'Bearer fixture-key') return route.fulfill({ status: 401, json: { error: { message: 'Unauthorized' } } })
    const url = new URL(request.url())
    if (url.pathname === '/monitor-api/v1/snapshot') {
      state.snapshots++
      if (state.snapshotStatus !== 200) return route.fulfill({ status: state.snapshotStatus, json: { error: { message: 'Fixture failure' } } })
      return route.fulfill({ json: state.snapshot })
    }
    state.details.push(url.pathname + url.search)
    const key = decodeURIComponent(url.pathname.split('/').pop()!)
    return route.fulfill({ json: createDetail(key, url.searchParams.get('include_output') === '1') })
  })
  await page.route('**/api/system', async route => {
    state.publicRequests++
    return route.fulfill({ json: { ...state.snapshot.system, cached_at: state.snapshot.generated_at / 1000 } })
  })
  await page.route('**/api/models', async route => {
    state.publicRequests++
    return route.fulfill({ json: { models: state.snapshot.models.filter(m => m.routing.available), lanes: state.snapshot.lanes } })
  })
  return state as typeof state & { snapshot: Snapshot }
}
