/** Monitor API v1. All timestamps are Unix milliseconds; absent readings are null. */
export type LaneKey = 'chat' | 'embed' | 'rerank' | 'asr'
export type DataState = 'loading' | 'ready' | 'stale' | 'unauthorized' | 'unsupported' | 'error'
export interface Issue { code: string; message: string }
export interface SourceState {
  last_attempt_at: number | null
  last_success_at: number | null
  stale_after_ms: number
  stale: boolean
  error: Issue | null
}
export interface SystemData {
  cpu: { user: number | null; sys: number | null; idle: number | null }
  memory: { total_gb: number | null; used_gb: number | null; free_gb: number | null; wired_gb: number | null }
  load_avg: (number | null)[]
}
export interface Lane { active: number; waiting: number; max: number; queue_depth: number }
export interface Diagnostic { code: string; severity: 'info' | 'warning' | 'error'; message: string; model_key: string | null }
export interface MonitorModel {
  key: string
  alias: string
  backend: string
  backend_model: string | null
  capabilities: string[]
  registered: boolean
  management: 'process' | 'launchd' | 'external'
  port: number | null
  lifecycle: { state: 'stopped' | 'starting' | 'running' | 'stale' | 'unknown' | 'unmanaged'; reason: string | null }
  availability: { state: 'healthy' | 'unready' | 'unauthorized' | 'unreachable' | 'unknown'; reason: string | null }
  routing: { available: boolean | null; reason: string | null }
  activity: { active: number; waiting: number; uncertain: boolean; scope: 'gateway' }
  monitoring_support: { health: boolean; metrics: boolean; slots: boolean; process_stats: boolean; output: boolean }
  budget: { used: number; total: number } | null
  loaded: boolean | null
  endpoint: string | null
}
export interface Snapshot {
  schema_version: 1
  snapshot_id: string
  generated_at: number
  sources: { system: SourceState; catalog: SourceState; discovery: SourceState }
  system: SystemData | null
  lanes: Record<LaneKey, Lane>
  models: MonitorModel[]
  diagnostics: Diagnostic[]
}
export interface Metric { name: string; value: number | null; unit: string }
export interface Slot {
  id: number | string
  busy: boolean
  decoded: number | null
  limit: number | null
  prompt_tokens: number | null
  generated?: string
  reasoning?: string
}
export interface Section { state: DataState; message: string | null }
export interface ModelDetail {
  schema_version: 1
  key: string
  generated_at: number
  source: SourceState
  health: Section
  metrics: Section & { values: Metric[] }
  slots: Section & { items: Slot[] }
  process: Section & { cpu_pct: number | null; rss_gb: number | null }
  ollama: Section & { version: string | null; loaded: boolean | null; size_gb: number | null; vram_gb: number | null; quantization: string | null; expires_at: string | null }
}
export interface PublicOverview {
  system: SystemData | null
  lanes: Partial<Record<LaneKey, Lane>>
  model_count: number | null
  last_success_at: number | null
}
export interface HistorySample { time: number; cpu: number | null; memory: number | null }
