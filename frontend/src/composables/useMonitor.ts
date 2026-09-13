import { ref } from 'vue'
import { createMonitorClient, MonitorApiError, type MonitorClient } from '../api/client'
import type { HistorySample, ModelDetail, PublicOverview, Snapshot } from '../api/types'
import { addHistory } from '../domain/history'

type Resource = 'public' | 'snapshot' | 'detail'
type Job = { generation: number; timer: ReturnType<typeof setTimeout> | null; controller: AbortController | null; failures: number }
export interface MonitorOptions {
  client?: MonitorClient
  fetch?: typeof fetch
  now?: () => number
  document?: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>
  requestTimeout?: number
}

export function useMonitor(options: MonitorOptions = {}) {
  const snapshot = ref<Snapshot | null>(null), detail = ref<ModelDetail | null>(null)
  const publicOverview = ref<PublicOverview | null>(null), history = ref<HistorySample[]>([])
  const connection = ref<'connecting' | 'connected' | 'disconnected'>('connecting')
  const access = ref<'unknown' | 'open' | 'authorized' | 'required' | 'unsupported'>('unknown')
  const paused = ref(false), loading = ref(false), lastSuccessAt = ref<number | null>(null)
  const error = ref<string | null>(null), keySet = ref(false), selectedKey = ref<string | null>(null), includeOutput = ref(false)
  const client = options.client ?? createMonitorClient(options.fetch)
  const now = options.now ?? Date.now
  const visibility = options.document ?? (typeof document === 'undefined' ? undefined : document)
  const resources: Resource[] = ['public', 'snapshot', 'detail']
  const jobs: Record<Resource, Job> = Object.fromEntries(resources.map(r => [r, { generation: 0, timer: null, controller: null, failures: 0 }])) as Record<Resource, Job>
  const errors: Partial<Record<Resource, string>> = {}
  let token = '', started = false

  const updateLoading = () => { loading.value = resources.some(r => jobs[r].controller !== null) }
  const updateError = () => { error.value = errors.snapshot ?? errors.detail ?? errors.public ?? null }
  function eligible(resource: Resource) {
    if (!started || paused.value || visibility?.hidden) return false
    if (resource === 'public') return access.value !== 'authorized' && access.value !== 'open'
    if (access.value === 'required' || access.value === 'unsupported') return false
    return resource !== 'detail' || selectedKey.value !== null
  }
  function interval(resource: Resource) {
    if (resource === 'public') return 5000
    if (resource === 'snapshot') return 5000
    const model = snapshot.value?.models.find(m => m.key === selectedKey.value)
    return model && (model.activity.active > 0 || model.activity.waiting > 0 || model.activity.uncertain) ? 3000 : 10000
  }
  function cancel(resource: Resource) {
    const job = jobs[resource]
    job.generation++
    if (job.timer !== null) clearTimeout(job.timer)
    job.timer = null
    job.controller?.abort()
    job.controller = null
    updateLoading()
  }
  const cancelAll = () => resources.forEach(cancel)
  function schedule(resource: Resource) {
    const job = jobs[resource]
    if (!eligible(resource) || job.controller || job.timer !== null) return
    const delay = Math.min(60000, interval(resource) * 2 ** Math.min(job.failures, 5))
    job.timer = setTimeout(() => { job.timer = null; void run(resource) }, delay)
  }
  function purgeProtected() {
    snapshot.value = null
    detail.value = null
    history.value = []
    includeOutput.value = false
    delete errors.snapshot; delete errors.detail
    updateError()
  }
  function unauthorized() {
    access.value = 'required'
    cancel('snapshot'); cancel('detail')
    purgeProtected()
    errors.snapshot = '需要有效的访问凭据'
    updateError()
    void run('public')
  }
  function recordHistory(value: Snapshot) {
    const state = value.sources.system
    const valid = !state.stale && state.error === null
    // Stale snapshots add a gap at observation time, never a new healthy sample.
    const time = valid ? state.last_success_at : value.generated_at
    history.value = addHistory(history.value, value.system, time, now(), valid)
  }
  function markStale(resource: Resource, message: string) {
    const issue = { code: 'client_refresh_failed', message }
    if (resource === 'snapshot' && snapshot.value) {
      const previous = snapshot.value
      snapshot.value = { ...previous, sources: {
        system: { ...previous.sources.system, stale: true, error: previous.sources.system.error ?? issue },
        catalog: { ...previous.sources.catalog, stale: true, error: previous.sources.catalog.error ?? issue },
        discovery: { ...previous.sources.discovery, stale: true, error: previous.sources.discovery.error ?? issue },
      } }
    }
    if (resource === 'detail' && detail.value) {
      const previous = detail.value
      const stale = <T extends { state: string; message: string | null }>(section: T): T =>
        section.state === 'ready' ? { ...section, state: 'stale', message } : section
      detail.value = { ...previous, source: { ...previous.source, stale: true, error: previous.source.error ?? issue },
        health: stale(previous.health), metrics: stale(previous.metrics), slots: stale(previous.slots),
        process: stale(previous.process), ollama: stale(previous.ollama) }
    }
  }
  async function run(resource: Resource): Promise<void> {
    const job = jobs[resource]
    if (!eligible(resource) || job.controller) return
    if (job.timer !== null) clearTimeout(job.timer)
    job.timer = null
    const controller = new AbortController(), generation = job.generation
    const requestedKey = selectedKey.value, requestedOutput = includeOutput.value
    job.controller = controller
    updateLoading()
    let timedOut = false
    const timeout = setTimeout(() => { timedOut = true; controller.abort() }, options.requestTimeout ?? 8000)
    let abortListener: (() => void) | undefined
    const aborted = new Promise<never>((_, reject) => {
      abortListener = () => reject(new DOMException('Request cancelled', 'AbortError'))
      controller.signal.addEventListener('abort', abortListener, { once: true })
    })
    try {
      const work = resource === 'public' ? client.publicOverview(controller.signal)
        : resource === 'snapshot' ? client.snapshot(controller.signal, token)
          : client.detail(requestedKey!, controller.signal, token, requestedOutput)
      const value = await Promise.race([work, aborted])
      if (generation !== job.generation || !eligible(resource)) return
      connection.value = 'connected'
      lastSuccessAt.value = now()
      job.failures = 0
      delete errors[resource]
      if (resource === 'public') {
        publicOverview.value = value as PublicOverview
        if (snapshot.value === null) history.value = addHistory(history.value, publicOverview.value.system, publicOverview.value.last_success_at, now())
      } else if (resource === 'snapshot') {
        snapshot.value = value as Snapshot
        access.value = token ? 'authorized' : 'open'
        cancel('public')
        publicOverview.value = null
        delete errors.public
        recordHistory(snapshot.value)
        // Detail cadence adapts after refreshed activity; it never overlaps an in-flight read.
        if (selectedKey.value && !jobs.detail.controller) {
          schedule('detail')
        }
      } else detail.value = value as ModelDetail
      updateError()
    } catch (cause) {
      if (generation !== job.generation || !eligible(resource)) return
      if (controller.signal.aborted && !timedOut) return
      const failure = cause instanceof MonitorApiError ? cause : null
      if (failure?.kind === 'unauthorized' && resource !== 'public') {
        connection.value = 'connected'
        unauthorized()
      } else if (failure?.kind === 'unsupported' && resource === 'snapshot') {
        connection.value = 'connected'
        access.value = 'unsupported'
        cancel('snapshot'); cancel('detail')
        purgeProtected()
        errors.snapshot = failure.message
        void run('public')
      } else {
        if (failure?.kind === 'network' || timedOut || !failure) connection.value = 'disconnected'
        else connection.value = 'connected'
        job.failures++
        errors[resource] = timedOut ? '监控请求超时，稍后重试' : failure?.message ?? '无法读取监控数据'
        markStale(resource, errors[resource]!)
        if (resource === 'snapshot' || (resource === 'public' && !snapshot.value)) history.value = addHistory(history.value, null, now(), now(), false)
      }
      updateError()
    } finally {
      clearTimeout(timeout)
      if (abortListener) controller.signal.removeEventListener('abort', abortListener)
      if (generation === job.generation && job.controller === controller) {
        job.controller = null
        updateLoading()
        schedule(resource)
      }
    }
  }
  function refresh() { return Promise.all(resources.map(run)).then(() => undefined) }
  function onVisibility() {
    cancelAll()
    if (!visibility?.hidden) void refresh()
  }
  function start() {
    if (started) return
    started = true
    visibility?.addEventListener('visibilitychange', onVisibility)
    void refresh()
  }
  function stop() {
    started = false
    visibility?.removeEventListener('visibilitychange', onVisibility)
    cancelAll()
  }
  function setPaused(value: boolean) {
    if (paused.value === value) return
    paused.value = value
    cancelAll()
    if (!value) void refresh()
  }
  function applyKey(value: string) {
    if (value.length > 8192 || /[\r\n]/.test(value)) { error.value = '访问凭据格式无效'; return }
    cancelAll()
    token = value.trim()
    keySet.value = token.length > 0
    purgeProtected()
    access.value = 'unknown'
    resources.forEach(r => { jobs[r].failures = 0 })
    void refresh()
  }
  function clearKey() {
    cancelAll()
    token = ''
    keySet.value = false
    selectedKey.value = null
    purgeProtected()
    access.value = 'unknown'
    resources.forEach(r => { jobs[r].failures = 0 })
    void refresh()
  }
  function selectModel(key: string | null) {
    if (selectedKey.value === key) return
    cancel('detail')
    selectedKey.value = key
    detail.value = null
    includeOutput.value = false
    delete errors.detail; updateError()
    jobs.detail.failures = 0
    if (key !== null) void run('detail')
  }
  function setIncludeOutput(value: boolean) {
    if (includeOutput.value === value) return
    cancel('detail')
    includeOutput.value = value
    // Keep the detail subtree mounted so its checkbox and keyboard focus survive.
    // Revoking output consent removes text synchronously, before the next read.
    if (!value && detail.value) {
      detail.value = { ...detail.value, slots: { ...detail.value.slots, items: detail.value.slots.items.map(slot => {
        const clean = { ...slot }
        delete clean.generated
        delete clean.reasoning
        return clean
      }) } }
    }
    void run('detail')
  }
  return { snapshot, detail, publicOverview, history, connection, access, paused, loading, lastSuccessAt,
    error, keySet, selectedKey, includeOutput, start, stop, refresh, setPaused, applyKey, clearKey, selectModel, setIncludeOutput }
}
