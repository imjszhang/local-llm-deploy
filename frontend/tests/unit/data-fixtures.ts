import type { ModelDetail, MonitorModel, Snapshot } from '../../src/api/types'
export function sampleModel(key = 'chat'): MonitorModel {
  return { key, alias: key, backend: 'llama_cpp', backend_model: null, capabilities: ['chat'], registered: true,
    management: 'process', port: 8001, lifecycle: { state: 'running', reason: null },
    availability: { state: 'healthy', reason: null }, routing: { available: true, reason: null },
    activity: { active: 0, waiting: 0, uncertain: false, scope: 'gateway' },
    monitoring_support: { health: true, metrics: true, slots: true, process_stats: true, output: true },
    budget: { used: 0, total: 8192 }, loaded: true, endpoint: '/v1/chat/completions' }
}
export const sampleSource = (time: number) => ({ last_attempt_at: time, last_success_at: time, stale_after_ms: 15000, stale: false, error: null })
export function sampleSnapshot(time = Date.now()): Snapshot {
  const lane = { active: 0, waiting: 0, max: 1, queue_depth: 0 }
  return { schema_version: 1, snapshot_id: String(time), generated_at: time,
    sources: { system: sampleSource(time), discovery: sampleSource(time), catalog: sampleSource(time) },
    system: { cpu: { user: 20, sys: 5, idle: 75 }, memory: { total_gb: 64, used_gb: 20, free_gb: 44, wired_gb: 3 }, load_avg: [1, 2, 3] },
    lanes: { chat: { ...lane }, embed: { ...lane }, rerank: { ...lane }, asr: { ...lane } },
    models: [sampleModel('chat'), sampleModel('other')], diagnostics: [] }
}
export function sampleDetail(key = 'chat', time = Date.now()): ModelDetail {
  const section = { state: 'ready' as const, message: null }
  return { schema_version: 1, key, generated_at: time, source: sampleSource(time), health: { ...section },
    metrics: { ...section, values: [{ name: 'tokens_per_second', value: 12.5, unit: 'tokens/s' }] },
    slots: { ...section, items: [{ id: 0, busy: false, decoded: 3, limit: 8192, prompt_tokens: 10, generated: 'sensitive output' }] },
    process: { ...section, cpu_pct: 123, rss_gb: 5 },
    ollama: { state: 'unsupported', message: null, version: null, loaded: null, size_gb: null, vram_gb: null, quantization: null, expires_at: null } }
}
