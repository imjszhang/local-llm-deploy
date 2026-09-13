import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useMonitor } from '../../src/composables/useMonitor'
import { MonitorApiError, type MonitorClient } from '../../src/api/client'
import type { ModelDetail, Snapshot } from '../../src/api/types'
import { sampleDetail, sampleSnapshot } from './data-fixtures'
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}
function setup() {
  const visibility = new EventTarget() as EventTarget & { hidden: boolean }
  visibility.hidden = false
  const client = {
    snapshot: vi.fn<MonitorClient['snapshot']>().mockImplementation(async () => sampleSnapshot()),
    detail: vi.fn<MonitorClient['detail']>().mockImplementation(async key => sampleDetail(key)),
    publicOverview: vi.fn<MonitorClient['publicOverview']>().mockResolvedValue({ model_count: 1, lanes: {}, system: null, last_success_at: null }),
  }
  const monitor = useMonitor({ client, document: visibility, requestTimeout: 8000 })
  stops.push(monitor.stop)
  return { monitor, client, visibility }
}
const flush = async () => { await vi.advanceTimersByTimeAsync(0) }
let stops: (() => void)[] = []
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(1_000_000) })
afterEach(() => { stops.forEach(stop => stop()); stops = []; vi.useRealTimers() })

describe('monitor polling lifecycle', () => {
  it('uses explicit lifecycle and single-flight requests under slow and repeated refresh', async () => {
    const { monitor, client } = setup(), slow = deferred<Snapshot>()
    client.snapshot.mockReturnValue(slow.promise)
    expect(client.snapshot).not.toHaveBeenCalled()
    monitor.start(); monitor.start(); void monitor.refresh(); void monitor.refresh()
    await flush()
    expect(client.snapshot).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5000)
    expect(client.snapshot).toHaveBeenCalledTimes(1)
    slow.resolve(sampleSnapshot()); await flush()
    await vi.advanceTimersByTimeAsync(5000)
    expect(client.snapshot).toHaveBeenCalledTimes(2)
    monitor.stop(); expect(monitor.loading.value).toBe(false)
  })
  it('discards an old response that ignores abort after authentication changes', async () => {
    const { monitor, client } = setup(), old = deferred<Snapshot>()
    client.snapshot.mockReturnValueOnce(old.promise)
    monitor.start(); await flush()
    const firstSignal = client.snapshot.mock.calls[0]![0]
    monitor.applyKey('new-key'); await flush()
    expect(firstSignal.aborted).toBe(true)
    expect(monitor.access.value).toBe('authorized')
    const current = monitor.snapshot.value
    const stale = sampleSnapshot(); stale.snapshot_id = 'old-auth-response'
    old.resolve(stale); await flush()
    expect(monitor.snapshot.value).toEqual(current)
    expect(monitor.snapshot.value?.snapshot_id).not.toBe('old-auth-response')
  })
  it('401 stops protected polling until credentials change while public fallback remains live', async () => {
    const { monitor, client } = setup()
    client.snapshot.mockRejectedValue(new MonitorApiError('unauthorized', 401))
    monitor.start(); await flush(); expect(monitor.access.value).toBe('required')
    await vi.advanceTimersByTimeAsync(60000)
    expect(client.snapshot).toHaveBeenCalledTimes(1)
    expect(client.publicOverview.mock.calls.length).toBeGreaterThan(5)
    client.snapshot.mockResolvedValue(sampleSnapshot())
    monitor.applyKey('valid'); await flush(); expect(monitor.access.value).toBe('authorized')
    const publicCount = client.publicOverview.mock.calls.length
    await vi.advanceTimersByTimeAsync(20000)
    expect(client.publicOverview).toHaveBeenCalledTimes(publicCount)
  })
  it('404 v1 sets explicit unsupported state and keeps public fallback', async () => {
    const { monitor, client } = setup()
    client.snapshot.mockRejectedValue(new MonitorApiError('unsupported', 404))
    monitor.start(); await flush(); expect(monitor.access.value).toBe('unsupported')
    await vi.advanceTimersByTimeAsync(20000)
    expect(client.snapshot).toHaveBeenCalledTimes(1)
    expect(client.publicOverview.mock.calls.length).toBeGreaterThan(2)
  })
  it('clearKey purges output synchronously and old completions cannot restore it', async () => {
    const { monitor, client } = setup()
    monitor.applyKey('do-not-expose'); monitor.start(); await flush()
    monitor.selectModel('chat'); await flush(); monitor.setIncludeOutput(true); await flush()
    expect(monitor.detail.value).not.toBeNull()
    const old = deferred<ModelDetail>()
    client.detail.mockReturnValueOnce(old.promise)
    void monitor.refresh(); await flush()
    client.snapshot.mockRejectedValue(new MonitorApiError('unauthorized', 401))
    monitor.clearKey()
    expect(monitor.snapshot.value).toBeNull(); expect(monitor.detail.value).toBeNull()
    expect(monitor.history.value).toEqual([]); expect(monitor.includeOutput.value).toBe(false)
    expect(monitor.selectedKey.value).toBeNull(); expect(monitor.keySet.value).toBe(false)
    old.resolve(sampleDetail()); await flush(); expect(monitor.detail.value).toBeNull()
    expect(JSON.stringify({ error: monitor.error.value, snapshot: monitor.snapshot.value, detail: monitor.detail.value })).not.toContain('do-not-expose')
  })
  it('hiding and pausing abort requests and suppress polling until resumed', async () => {
    const { monitor, client, visibility } = setup(), slow = deferred<Snapshot>()
    client.snapshot.mockReturnValueOnce(slow.promise)
    monitor.start(); await flush()
    visibility.hidden = true; visibility.dispatchEvent(new Event('visibilitychange'))
    expect(client.snapshot.mock.calls[0]![0].aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(60000); expect(client.snapshot).toHaveBeenCalledTimes(1)
    visibility.hidden = false; visibility.dispatchEvent(new Event('visibilitychange')); await flush()
    expect(client.snapshot).toHaveBeenCalledTimes(2)
    monitor.setPaused(true); await vi.advanceTimersByTimeAsync(60000)
    expect(client.snapshot).toHaveBeenCalledTimes(2)
    monitor.setPaused(false); await flush(); expect(client.snapshot).toHaveBeenCalledTimes(3)
    slow.resolve(sampleSnapshot()); await flush()
  })
  it('model selection invalidates old detail and uses explicit output opt-in', async () => {
    const { monitor, client } = setup(), old = deferred<ModelDetail>()
    monitor.start(); await flush(); client.detail.mockReturnValueOnce(old.promise)
    monitor.selectModel('chat'); await flush(); monitor.selectModel('other'); await flush()
    expect(client.detail.mock.calls[0]![1].aborted).toBe(true)
    expect(monitor.detail.value?.key).toBe('other'); expect(client.detail.mock.calls[1]![3]).toBe(false)
    old.resolve(sampleDetail('chat')); await flush(); expect(monitor.detail.value?.key).toBe('other')
    monitor.setIncludeOutput(true); await flush(); expect(client.detail.mock.lastCall?.[3]).toBe(true)
    monitor.selectModel(null); expect(monitor.detail.value).toBeNull(); expect(monitor.includeOutput.value).toBe(false)
  })
  it('keeps detail mounted while output changes and removes text immediately on revocation', async () => {
    const { monitor, client } = setup()
    monitor.start(); await flush(); monitor.selectModel('chat'); await flush()
    const pending = deferred<ModelDetail>()
    client.detail.mockReturnValueOnce(pending.promise)
    const previous = monitor.detail.value
    monitor.setIncludeOutput(true)
    expect(monitor.detail.value).toEqual(previous)
    pending.resolve(sampleDetail()); await flush()
    expect(monitor.detail.value?.slots.items[0]?.generated).toBe('sensitive output')
    client.detail.mockImplementationOnce(() => new Promise(() => {}))
    monitor.setIncludeOutput(false)
    expect(monitor.detail.value).not.toBeNull()
    expect(monitor.detail.value?.slots.items[0]?.generated).toBeUndefined()
    expect(monitor.detail.value?.slots.items[0]?.reasoning).toBeUndefined()
  })
  it('times out hanging requests, applies backoff, and retains last good data', async () => {
    const { monitor, client } = setup()
    monitor.start(); await flush(); const good = monitor.snapshot.value
    client.snapshot.mockRejectedValue(new MonitorApiError('invalid'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(monitor.snapshot.value?.system).toEqual(good?.system)
    expect(monitor.snapshot.value?.sources.system.stale).toBe(true)
    expect(monitor.snapshot.value?.sources.system.last_success_at).toBe(good?.sources.system.last_success_at)
    expect(monitor.error.value).toBe('监控响应格式无效')
    expect(monitor.history.value.at(-1)?.cpu).toBeNull()
    await vi.advanceTimersByTimeAsync(9999); expect(client.snapshot).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1); expect(client.snapshot).toHaveBeenCalledTimes(3)
    client.snapshot.mockImplementation(() => new Promise(() => {}))
    await vi.advanceTimersByTimeAsync(20000 + 8000)
    expect(monitor.error.value).toContain('超时'); expect(monitor.loading.value).toBe(false)
    await vi.advanceTimersByTimeAsync(5 * 60000)
    expect(client.snapshot.mock.calls.length).toBeLessThan(12)
  })
  it('polls busy detail at 3 seconds and idle detail still refreshes with 5-second snapshots', async () => {
    const { monitor, client } = setup()
    monitor.start(); await flush(); monitor.selectModel('chat'); await flush()
    await vi.advanceTimersByTimeAsync(10000); expect(client.detail).toHaveBeenCalledTimes(2)
    const busy = sampleSnapshot(); busy.models[0]!.activity.active = 1; client.snapshot.mockResolvedValue(busy)
    await vi.advanceTimersByTimeAsync(10000); const count = client.detail.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000); expect(client.detail).toHaveBeenCalledTimes(count + 1)
  })
})
