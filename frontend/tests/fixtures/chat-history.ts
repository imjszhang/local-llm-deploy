import type { Page } from '@playwright/test'
import type { Session } from '../../src/features/chat/domain/types'

export async function mockChatHistory(page: Page) {
  const state = {
    documents: new Map<string, { session: Session; revision: number; updated_at: number }>(),
    writes: [] as { schema_version: number; session: Session; revision: number }[],
    failWrites: false, failList: false, token: '', issued: 0,
  }
  await page.route('**/console-api/v1/session', route => {
    state.token = `local-console.history-fixture-token-${++state.issued}`
    return route.fulfill({ json: { token: state.token, expires_at: Date.now() + 60000 }, headers: { 'Cache-Control': 'no-store' } })
  })
  await page.route('**/chat-api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const headers = request.headers()
    if (headers.authorization !== `Bearer ${state.token}` || headers['x-local-console'] !== '1') return route.fulfill({ status: 401, json: { error: {} } })
    const id = path.split('/')[4]
    const document = id ? state.documents.get(id) : undefined
    const reply = (json: unknown, status = 200) => route.fulfill({ status, json, headers: { 'Cache-Control': 'no-store' } })
    if (request.method() === 'GET' && !id && state.failList) return reply({ error: {} }, 503)
    if (request.method() === 'GET' && !id) return reply({ schema_version: 1, sessions: [...state.documents.values()].sort((a, b) => b.updated_at - a.updated_at).map(d => ({ id: d.session.id, title: d.session.title, model: d.session.model, revision: d.revision, created_at: d.updated_at, updated_at: d.updated_at })) })
    if (request.method() === 'GET') return document ? reply({ schema_version: 1, ...document }) : reply({ error: {} }, 404)
    const body = request.postDataJSON()
    if (request.method() === 'PUT') {
      if (state.failWrites) return reply({ error: {} }, 503)
      if ((document?.revision ?? 0) !== body.revision) return reply({ error: {} }, 409)
      state.writes.push(body)
      const next = { session: body.session, revision: body.revision + 1, updated_at: Date.now() }
      state.documents.set(id!, next)
      return reply({ schema_version: 1, ...next })
    }
    if (request.method() === 'DELETE') {
      if (!document || document.revision !== body.revision) return reply({ error: {} }, 409)
      state.documents.delete(id!)
      return reply({ deleted: true, id })
    }
    return reply({ error: {} }, 405)
  })
  return state
}
