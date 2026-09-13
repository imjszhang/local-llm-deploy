import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ConsoleContext } from '../../../app/context'
import { copySession, createHistoryClient, HistoryApiError, type HistoryClient, type SessionSummary } from '../api/history'
import type { Session } from '../domain/types'

export type HistoryStatus = 'loading' | 'saved' | 'saving' | 'pending' | 'error' | 'conflict' | 'unavailable' | 'unauthorized'
interface Entry {
  loaded: boolean; revision: number; saved: string; dirty: boolean; changedAt: number; dirtySince: number
  saving?: Promise<void>; loading?: Promise<void>; timer?: ReturnType<typeof setTimeout>; error?: HistoryApiError
  deleting?: boolean; attempted?: boolean
  validationError?: boolean; errorText?: string
}
const blank = (model = '', id: string = crypto.randomUUID(), title = '新对话'): Session => ({ id, title, model, system: '', parameters: {}, turns: [], draft: '' })
const meaningful = (session: Session) => !!(session.model || session.draft || session.system || session.turns.length || Object.keys(session.parameters).length || session.title !== '新对话')
const serialize = (session: Session) => JSON.stringify(copySession(session))
function restoreInterrupted(session: Session) {
  let changed = false
  for (const turn of session.turns) for (const answer of turn.answers) {
    if (answer.status === 'waiting' || answer.status === 'streaming') {
      answer.status = 'stopped'; answer.adopted = false
      answer.error = '本页未连接这次生成，已恢复最近保存的部分内容。后端或其他页面可能仍在生成；重试将发起新请求。'
      changed = true
    }
  }
  return changed
}

export function useChatHistory(context: Pick<ConsoleContext, 'credential' | 'credentialGeneration'>,
  options: { enabled?: boolean; onReset?: () => void; client?: HistoryClient } = {}) {
  const enabled = options.enabled !== false, client = options.client ?? createHistoryClient()
  const sessions = ref<Session[]>([]), selectedId = ref(''), loading = ref(false), ready = ref(!enabled)
  const issueSessionId = ref('')
  const status = ref<HistoryStatus>(enabled ? 'loading' : 'saved'), message = ref(''), unsaved = ref(false)
  const current = computed(() => sessions.value.find(session => session.id === selectedId.value))
  const hasUnsaved = computed(() => unsaved.value)
  const entries = new Map<string, Entry>(), controllers = new Set<AbortController>()
  let epoch = 0, disposed = false, listLoading = false, listFailed = false, topError: HistoryApiError | undefined, restoredNotice = ''

  function active(own: number) { return !disposed && own === epoch }
  function isLoaded(id: string) { return entries.get(id)?.loaded === true }
  function updateState() {
    const all = [...entries.values()]
    loading.value = listLoading || !!entries.get(selectedId.value)?.loading
    unsaved.value = enabled && all.some(entry => entry.loaded && (entry.dirty || !!entry.saving))
    if (!enabled) { status.value = 'saved'; message.value = ''; return }
    const issue = entries.get(selectedId.value)?.error ? selectedId.value : [...entries].find(([, entry]) => entry.error)?.[0]
    issueSessionId.value = topError ? '' : issue ?? ''
    const error = topError ?? (issue ? entries.get(issue)?.error : undefined)
    if (loading.value) { status.value = 'loading'; message.value = '正在载入本机保存的对话'; return }
    if (error) {
      status.value = error.kind === 'conflict' || error.kind === 'missing' ? 'conflict' : error.kind === 'unauthorized' ? 'unauthorized' : error.kind === 'unavailable' ? 'unavailable' : 'error'
      message.value = error.message; return
    }
    if (!context.credential.value) { status.value = 'unauthorized'; message.value = '连接本机或设置访问凭据后自动保存'; return }
    if (all.some(entry => entry.saving)) { status.value = 'saving'; message.value = '正在保存对话'; return }
    if (unsaved.value) { status.value = 'pending'; message.value = '有修改等待保存'; return }
    status.value = 'saved'; message.value = restoredNotice || '对话已保存到本机'
  }
  function clearTimers() { for (const entry of entries.values()) { clearTimeout(entry.timer); entry.timer = undefined } }
  function invalidate() {
    epoch++; clearTimers()
    for (const controller of controllers) controller.abort()
    controllers.clear()
    return epoch
  }
  function controller() { const value = new AbortController(); controllers.add(value); return value }
  function errorOf(cause: unknown) { return cause instanceof HistoryApiError ? cause : new HistoryApiError('network') }
  function schedule(id: string) {
    const entry = entries.get(id)
    if (!enabled || !entry?.loaded || !entry.dirty || entry.saving || entry.deleting || entry.error || topError || listLoading || !context.credential.value) return
    clearTimeout(entry.timer)
    const delay = Math.max(0, Math.min(entry.changedAt + 500, entry.dirtySince + 1000) - Date.now())
    entry.timer = setTimeout(() => { entry.timer = undefined; void save(id) }, delay)
  }
  function inspect() {
    for (const session of sessions.value) {
      const entry = entries.get(session.id)
      if (!entry?.loaded || entry.deleting) continue
      try {
        const text = serialize(session), dirty = text !== entry.saved && (entry.revision > 0 || meaningful(session))
        if (entry.error?.kind === 'invalid' && (entry.validationError || text !== entry.errorText)) { entry.error = undefined; entry.validationError = false; entry.errorText = undefined }
        if (dirty) {
          if (!entry.dirty) entry.dirtySince = Date.now()
          entry.changedAt = Date.now()
        }
        entry.dirty = dirty
        if (!dirty) { clearTimeout(entry.timer); entry.timer = undefined }
        else schedule(session.id)
      } catch (cause) { entry.dirty = true; entry.error = errorOf(cause); entry.validationError = true }
    }
    updateState()
  }
  function create(model = current.value?.model ?? ''): Session {
    const session = blank(model)
    entries.set(session.id, { loaded: true, revision: 0, saved: '', dirty: false, changedAt: 0, dirtySince: 0 })
    sessions.value.unshift(session); selectedId.value = session.id
    inspect()
    return current.value!
  }
  async function save(id: string): Promise<void> {
    const entry = entries.get(id), session = sessions.value.find(item => item.id === id)
    if (!enabled || !entry?.loaded || !session || entry.deleting || entry.error || topError || !context.credential.value) return
    if (entry.saving) {
      await entry.saving
      if (entries.get(id) === entry && entry.dirty && !entry.error) await save(id)
      return
    }
    let snapshot: Session, text: string
    try { snapshot = copySession(session); text = JSON.stringify(snapshot) } catch (cause) { entry.error = errorOf(cause); updateState(); return }
    if (text === entry.saved || (entry.revision === 0 && !meaningful(snapshot))) { entry.dirty = false; updateState(); return }
    clearTimeout(entry.timer); entry.timer = undefined
    const own = epoch, abort = controller(), token = context.credential.value, revision = entry.revision
    entry.attempted = true
    const work = Promise.resolve().then(async () => {
      try {
        const response = await client.put(snapshot, revision, token, abort.signal)
        if (!active(own) || entries.get(id) !== entry) return
        if (response.revision <= revision) throw new HistoryApiError('invalid')
        entry.revision = response.revision; entry.saved = text
        entry.dirty = serialize(session) !== text
        if (entry.dirty) { entry.dirtySince = Date.now(); entry.changedAt = Date.now() }
      } catch (cause) {
        if (active(own) && entries.get(id) === entry && !abort.signal.aborted) { entry.error = errorOf(cause); entry.errorText = text; entry.dirty = true }
      } finally {
        controllers.delete(abort)
        if (active(own) && entries.get(id) === entry) { entry.saving = undefined; schedule(id); updateState() }
      }
    })
    entry.saving = work; updateState()
    await work
  }
  async function ensureLoaded(id: string): Promise<void> {
    const entry = entries.get(id)
    if (!enabled || !entry || entry.loaded || !context.credential.value) return
    if (entry.loading) return entry.loading
    const own = epoch, abort = controller(), token = context.credential.value
    entry.error = undefined
    const work = Promise.resolve().then(async () => {
      try {
        const response = await client.get(id, token, abort.signal)
        if (!active(own) || entries.get(id) !== entry) return
        entry.revision = response.revision; entry.loaded = true
        if (restoreInterrupted(response.session)) restoredNotice = '已恢复最近保存的内容，本页不会接续其他连接的生成'
        // Restoring a display state must not write over another window's active checkpoint.
        entry.saved = serialize(response.session)
        const index = sessions.value.findIndex(session => session.id === id)
        if (index >= 0) sessions.value[index] = response.session
      } catch (cause) {
        if (active(own) && entries.get(id) === entry && !abort.signal.aborted) entry.error = errorOf(cause)
      } finally {
        controllers.delete(abort)
        if (active(own) && entries.get(id) === entry) { entry.loading = undefined; inspect() }
      }
    })
    entry.loading = work; updateState()
    await work
  }
  function applyList(items: SessionSummary[], preserve: boolean) {
    const previousId = selectedId.value
    const local = new Map(sessions.value.flatMap(session => {
      const entry = entries.get(session.id)
      return preserve && entry?.loaded && (entry.dirty || (entry.revision === 0 && meaningful(session))) ? [[session.id, { session, entry }] as const] : []
    }))
    clearTimers(); entries.clear()
    const restored = items.map(item => {
      const pending = local.get(item.id)
      if (pending) {
        local.delete(item.id)
        if (pending.entry.revision !== item.revision) pending.entry.error = new HistoryApiError('conflict', 409)
        entries.set(item.id, pending.entry)
        return pending.session
      }
      entries.set(item.id, { loaded: false, revision: item.revision, saved: '', dirty: false, changedAt: 0, dirtySince: 0 })
      return blank(item.model, item.id, item.title)
    })
    for (const [id, pending] of local) {
      if (pending.entry.revision > 0) pending.entry.error = new HistoryApiError('missing', 404)
      entries.set(id, pending.entry); restored.unshift(pending.session)
    }
    sessions.value = restored
    selectedId.value = preserve && restored.some(session => session.id === previousId) ? previousId : restored[0]?.id ?? ''
    if (!restored.length) create('')
  }
  async function loadList(own = epoch, preserve = true): Promise<void> {
    if (!enabled || !context.credential.value || !active(own)) { ready.value = true; updateState(); return }
    listLoading = true; ready.value = false; topError = undefined; updateState()
    const abort = controller(), token = context.credential.value
    try {
      const items = await client.list(token, abort.signal)
      if (!active(own)) return
      listFailed = false; applyList(items, preserve)
      await ensureLoaded(selectedId.value)
    } catch (cause) {
      if (active(own) && !abort.signal.aborted) { topError = errorOf(cause); listFailed = true }
    } finally {
      controllers.delete(abort)
      if (active(own)) { listLoading = false; ready.value = true; inspect() }
    }
  }
  async function flush(): Promise<void> {
    if (!enabled) return
    inspect(); clearTimers()
    await Promise.all([...entries].filter(([, entry]) => entry.loaded && (entry.dirty || entry.saving)).map(([id]) => save(id)))
    updateState()
  }
  async function retry(): Promise<void> {
    if (!enabled) return
    topError = undefined
    for (const entry of entries.values()) if (entry.error?.kind !== 'conflict' && entry.error?.kind !== 'missing') entry.error = undefined
    if (listFailed) { await loadList(); await flush(); return }
    if (current.value && !isLoaded(current.value.id)) await ensureLoaded(current.value.id)
    await flush()
  }
  async function reload(): Promise<void> {
    options.onReset?.()
    const own = invalidate()
    for (const entry of entries.values()) { entry.saving = undefined; entry.loading = undefined; entry.deleting = false }
    restoredNotice = ''
    await loadList(own, false)
  }
  function removeMemory(id: string) {
    clearTimeout(entries.get(id)?.timer); entries.delete(id)
    sessions.value = sessions.value.filter(session => session.id !== id)
    if (selectedId.value === id) selectedId.value = sessions.value[0]?.id ?? ''
    if (!sessions.value.length) create('')
    inspect()
  }
  async function remove(id: string): Promise<boolean> {
    const entry = entries.get(id)
    if (!entry) return false
    if (!enabled) { removeMemory(id); return true }
    const own = epoch
    entry.deleting = true; clearTimeout(entry.timer)
    await entry.saving
    if (!active(own) || entries.get(id) !== entry) return false
    if (entry.revision === 0 && !entry.attempted) { removeMemory(id); return true }
    const abort = controller()
    try {
      if (!context.credential.value) throw new HistoryApiError('unauthorized')
      await client.remove(id, entry.revision, context.credential.value, abort.signal)
      if (!active(own) || entries.get(id) !== entry) return false
      removeMemory(id); return true
    } catch (cause) {
      if (active(own) && entries.get(id) === entry && !abort.signal.aborted) { entry.error = errorOf(cause); entry.deleting = false; updateState() }
      return false
    } finally { controllers.delete(abort) }
  }
  function copyCurrent(): Session {
    const original = current.value && isLoaded(current.value.id) ? copySession(current.value) : undefined
    if (!original) return create('')
    const session = create(original.model)
    Object.assign(session, original, { id: session.id, title: `${Array.from(original.title).slice(0, 94).join('')} · 副本` })
    restoreInterrupted(session)
    // The new session owns every unsaved edit; the old sidebar entry can reload its stored version.
    const previous = entries.get(original.id)
    if (previous?.error?.kind === 'missing') {
      entries.delete(original.id); sessions.value = sessions.value.filter(item => item.id !== original.id)
    } else if (previous?.error?.kind === 'conflict') {
      previous.loaded = false; previous.dirty = false; previous.error = undefined; previous.saved = ''
      const index = sessions.value.findIndex(item => item.id === original.id)
      if (index >= 0) sessions.value[index] = blank(original.model, original.id, original.title)
    }
    inspect()
    return session
  }
  function resetAuth() {
    options.onReset?.()
    const own = invalidate()
    entries.clear(); sessions.value = []; selectedId.value = ''; listLoading = false; listFailed = false; topError = undefined; restoredNotice = ''
    ready.value = !enabled || !context.credential.value
    create(''); updateState()
    if (enabled && context.credential.value) queueMicrotask(() => { if (active(own)) void loadList(own) })
  }
  watch(sessions, inspect, { deep: true })
  watch(selectedId, id => { if (id) void ensureLoaded(id); updateState() })
  watch([context.credential, context.credentialGeneration], resetAuth, { immediate: true, flush: 'sync' })
  onBeforeUnmount(() => { disposed = true; invalidate() })
  return { sessions, selectedId, current, create, remove, flush, retry, reload, copyCurrent, loading, ready, status, message, hasUnsaved, isLoaded, issueSessionId }
}
