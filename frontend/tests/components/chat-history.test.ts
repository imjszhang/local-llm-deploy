import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick, ref } from 'vue'
import { mount, type VueWrapper } from '@vue/test-utils'
import { useChatHistory } from '../../src/features/chat/composables/useChatHistory'
import { copySession, HistoryApiError, type HistoryClient, type StoredSession } from '../../src/features/chat/api/history'
import type { Session } from '../../src/features/chat/domain/types'
import HistoryStatus from '../../src/features/chat/components/HistoryStatus.vue'

const wrappers: VueWrapper[] = []
beforeEach(() => vi.useFakeTimers())
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers() })
const session = (id = 'stored-1'): Session => ({ id, title: id, model: 'model', system: '', parameters: {}, turns: [], draft: 'saved draft' })
const stored = (value: Session, revision = 1): StoredSession => ({ schema_version: 1, session: copySession(value), revision, updated_at: Date.now() })
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
async function settle() { for (let i = 0; i < 16; i++) { await Promise.resolve(); await nextTick() } }
function setup(initial: Session[] = [], enabled = true, token = 'credential') {
  const data = new Map(initial.map(value => [value.id, stored(value)]))
  const client = {
    list: vi.fn<HistoryClient['list']>(async () => [...data.values()].map(item => ({ id: item.session.id, title: item.session.title, model: item.session.model, revision: item.revision, created_at: 1, updated_at: item.updated_at }))),
    get: vi.fn<HistoryClient['get']>(async id => { const item = data.get(id); if (!item) throw new HistoryApiError('missing', 404); return structuredClone(item) }),
    put: vi.fn<HistoryClient['put']>(async (value, revision) => {
      if ((data.get(value.id)?.revision ?? 0) !== revision) throw new HistoryApiError('conflict', 409)
      const item = stored(value, revision + 1); data.set(value.id, item); return structuredClone(item)
    }),
    remove: vi.fn<HistoryClient['remove']>(async (id, revision) => {
      if (data.get(id)?.revision !== revision) throw new HistoryApiError('conflict', 409)
      data.delete(id)
    }),
  }
  const credential = ref(token), credentialGeneration = ref(0), onReset = vi.fn()
  let history!: ReturnType<typeof useChatHistory>
  const wrapper = mount(defineComponent({ setup() { history = useChatHistory({ credential, credentialGeneration }, { client, enabled, onReset }); return () => null } }))
  wrappers.push(wrapper)
  return { history, client, data, credential, credentialGeneration, onReset, wrapper }
}

describe('automatic chat history persistence', () => {
  it('routes another session error to that session instead of offering destructive actions here', async () => {
    const wrapper = mount(HistoryStatus, { props: { status: 'conflict', message: 'conflict', pending: true, elsewhere: true } })
    wrappers.push(wrapper)
    expect(wrapper.text()).not.toContain('保存为新会话')
    expect(wrapper.text()).not.toContain('载入已保存版本')
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('openIssue')).toHaveLength(1)
  })
  it('identifies the conflicted session when another session is selected', async () => {
    const { history, data } = setup([session('first'), session('second')])
    await settle()
    data.get('first')!.revision++
    history.current.value!.draft = 'local conflict'
    await history.flush()
    history.selectedId.value = 'second'; await settle()
    expect(history.status.value).toBe('conflict')
    expect(history.issueSessionId.value).toBe('first')
    expect(history.current.value!.id).toBe('second')
    history.selectedId.value = history.issueSessionId.value; await settle()
    expect(history.current.value!.draft).toBe('local conflict')
  })
  it('loads only the selected detail and never saves sidebar placeholders', async () => {
    const { history, client } = setup([session('first'), session('second')])
    await settle()
    expect(history.ready.value).toBe(true)
    expect(history.current.value!.draft).toBe('saved draft')
    expect(client.get.mock.calls.map(args => args[0])).toEqual(['first'])
    expect(history.isLoaded('second')).toBe(false)
    await vi.advanceTimersByTimeAsync(1500)
    expect(client.put).not.toHaveBeenCalled()
    history.selectedId.value = 'second'; await settle()
    expect(history.current.value!.draft).toBe('saved draft')
    expect(client.get.mock.calls.map(args => args[0])).toEqual(['first', 'second'])
    expect(client.put).not.toHaveBeenCalled()
  })
  it('does not persist an untouched empty session and coalesces draft edits', async () => {
    const { history, client } = setup()
    await settle(); await vi.advanceTimersByTimeAsync(1500)
    expect(client.put).not.toHaveBeenCalled()
    history.current.value!.draft = 'first'; await settle()
    await vi.advanceTimersByTimeAsync(300)
    history.current.value!.draft = 'latest'; await settle()
    await vi.advanceTimersByTimeAsync(499)
    expect(client.put).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1); await settle()
    expect(client.put).toHaveBeenCalledOnce()
    expect(client.put.mock.calls[0]![0].draft).toBe('latest')
    expect(client.put.mock.calls[0]![1]).toBe(0)
    expect(history.status.value).toBe('saved')
    expect(history.hasUnsaved.value).toBe(false)
  })
  it('checkpoints continuous streaming edits within one second instead of waiting for silence', async () => {
    const { history, client } = setup()
    await settle()
    for (let i = 0; i < 7; i++) {
      history.current.value!.draft += `${i}`
      await settle(); await vi.advanceTimersByTimeAsync(200)
    }
    expect(client.put.mock.calls.length).toBeGreaterThanOrEqual(1)
    expect(client.put.mock.calls[0]![0].draft).toBe('01234')
    await history.flush()
    expect(client.put.mock.calls.at(-1)![0].draft).toBe('0123456')
  })
  it('serializes writes and applies returned revisions while preserving edits made during a save', async () => {
    const { history, client } = setup()
    await settle()
    const first = deferred<StoredSession>()
    client.put.mockImplementationOnce(() => first.promise).mockImplementation(async (value, revision) => stored(value, revision + 1))
    history.current.value!.draft = 'first'; await settle()
    const flush1 = history.flush(); await settle()
    history.current.value!.draft = 'second'; await settle()
    const flush2 = history.flush(); await settle()
    expect(client.put).toHaveBeenCalledOnce()
    first.resolve(stored(client.put.mock.calls[0]![0], 1))
    await flush1; await flush2; await settle()
    expect(client.put.mock.calls.map(args => [args[0].draft, args[1]])).toEqual([['first', 0], ['second', 1]])
    expect(history.current.value!.draft).toBe('second')
    expect(history.hasUnsaved.value).toBe(false)
  })
  it('keeps failed saves in memory and pauses automatic retries until requested', async () => {
    const { history, client } = setup()
    await settle()
    client.put.mockRejectedValueOnce(new HistoryApiError('network'))
    history.current.value!.draft = 'unsaved'; await history.flush()
    expect(history.status.value).toBe('error')
    expect(history.current.value!.draft).toBe('unsaved')
    history.current.value!.draft = 'still unsaved'; await settle(); await vi.advanceTimersByTimeAsync(5000)
    expect(client.put).toHaveBeenCalledOnce()
    await history.retry()
    expect(client.put).toHaveBeenCalledTimes(2)
    expect(history.status.value).toBe('saved')
    expect(history.current.value!.draft).toBe('still unsaved')
  })
  it('preserves local drafts when retrying a formerly unavailable history list', async () => {
    const { history, client } = setup()
    client.list.mockRejectedValueOnce(new HistoryApiError('unavailable', 404))
    await settle()
    history.current.value!.draft = 'local after failed list'; await settle()
    const id = history.current.value!.id
    await history.retry()
    expect(history.current.value!.id).toBe(id)
    expect(history.current.value!.draft).toBe('local after failed list')
    expect(client.put.mock.calls[0]![0].draft).toBe('local after failed list')
    expect(history.status.value).toBe('saved')
  })
  it('does not overwrite conflicts; a new ID owns the copied edits and the original reloads saved content', async () => {
    const { history, client, data } = setup([session()])
    await settle()
    history.current.value!.draft = 'my local changes'; await settle()
    data.set('stored-1', stored({ ...session(), draft: 'other page changes' }, 2))
    await history.flush()
    expect(history.status.value).toBe('conflict')
    await history.retry()
    expect(client.put).toHaveBeenCalledOnce()
    const copy = history.copyCurrent()
    expect(copy.id).not.toBe('stored-1')
    expect(copy.draft).toBe('my local changes')
    await history.flush()
    expect(history.status.value).toBe('saved')
    expect(history.hasUnsaved.value).toBe(false)
    expect(data.get(copy.id)?.session.draft).toBe('my local changes')
    history.selectedId.value = 'stored-1'; await settle()
    expect(history.current.value!.draft).toBe('other page changes')
  })
  it('restores interrupted answers as stopped without silently continuing inference', async () => {
    const value = session()
    value.turns = [{ id: 'turn', user: 'question', selected: 0, answers: [{ id: 'answer', content: 'partial', reasoning: 'partial reasoning', status: 'streaming', adopted: true, startedAt: 10, request: { model: 'model', backend: 'ollama', parameters: {}, messages: [{ role: 'user', content: 'question' }] } }] }]
    const { history, client } = setup([value])
    await settle()
    const answer = history.current.value!.turns[0]!.answers[0]!
    expect(answer.status).toBe('stopped'); expect(answer.adopted).toBe(false)
    expect(answer.content).toBe('partial'); expect(answer.error).toContain('本页未连接这次生成')
    await history.flush()
    await vi.advanceTimersByTimeAsync(1500)
    expect(client.put).not.toHaveBeenCalled()
    history.current.value!.draft = '下一问'; await history.flush()
    expect(client.put.mock.calls[0]![0].turns[0]!.answers[0]!.status).toBe('stopped')
  })
  it('deletes only after an in-flight save returns and retains sessions on delete failure', async () => {
    const { history, client } = setup()
    await settle()
    const first = deferred<StoredSession>()
    client.put.mockImplementationOnce(() => first.promise)
    history.current.value!.draft = 'pending'; await settle()
    const id = history.current.value!.id, saving = history.flush(); await settle()
    const deleting = history.remove(id); await settle()
    expect(client.remove).not.toHaveBeenCalled()
    client.remove.mockRejectedValueOnce(new HistoryApiError('network'))
    first.resolve(stored(client.put.mock.calls[0]![0], 1))
    await saving; expect(await deleting).toBe(false)
    expect(client.remove.mock.calls[0]!.slice(0, 2)).toEqual([id, 1])
    expect(history.sessions.value.some(item => item.id === id)).toBe(true)
    client.remove.mockResolvedValueOnce()
    expect(await history.remove(id)).toBe(true)
    expect(history.sessions.value.some(item => item.id === id)).toBe(false)
  })
  it('clears immediately on credential changes and rejects late old-generation responses', async () => {
    const { history, client, credential, credentialGeneration, onReset } = setup()
    const old = deferred<ReturnType<HistoryClient['list']> extends Promise<infer T> ? T : never>()
    client.list.mockImplementationOnce(() => old.promise)
    await settle()
    const oldSignal = client.list.mock.calls[0]![1]
    credential.value = 'new-key'; credentialGeneration.value++
    expect(onReset).toHaveBeenCalledTimes(3)
    expect(history.current.value!.draft).toBe('')
    expect(oldSignal.aborted).toBe(true)
    await settle()
    old.resolve([{ id: 'old-private', title: 'Old', model: 'model', revision: 1, created_at: 1, updated_at: 2 }])
    await settle()
    expect(history.sessions.value.some(item => item.id === 'old-private')).toBe(false)
    expect(client.list.mock.calls.map(args => args[0])).toEqual(['credential', 'new-key'])
    expect(client.remove).not.toHaveBeenCalled()
    credential.value = ''; await settle()
    expect(history.ready.value).toBe(true); expect(history.status.value).toBe('unauthorized')
    expect(client.list).toHaveBeenCalledTimes(2)
  })
  it('recovers from locally invalid parameter edits without a network retry loop', async () => {
    const { history, client } = setup()
    await settle()
    history.current.value!.parameters.temperature = 3
    await settle(); await vi.advanceTimersByTimeAsync(1200)
    expect(history.status.value).toBe('error'); expect(client.put).not.toHaveBeenCalled()
    history.current.value!.parameters.temperature = 0.5
    await settle(); await vi.advanceTimersByTimeAsync(1000)
    expect(client.put).toHaveBeenCalledOnce(); expect(history.status.value).toBe('saved')
  })
  it('disabled persistence retains immediate memory behavior without requests', async () => {
    const { history, client } = setup([], false)
    const id = history.current.value!.id
    history.current.value!.draft = 'local'
    const deleting = history.remove(id)
    expect(history.sessions.value.some(item => item.id === id)).toBe(false)
    expect(await deleting).toBe(true)
    await history.flush(); await settle(); await vi.advanceTimersByTimeAsync(2000)
    expect(client.list).not.toHaveBeenCalled(); expect(client.put).not.toHaveBeenCalled()
    expect(history.ready.value).toBe(true); expect(history.hasUnsaved.value).toBe(false)
  })
})
