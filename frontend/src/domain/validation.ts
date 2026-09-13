import type {
  DataState, Diagnostic, Lane, LaneKey, ModelDetail, MonitorModel, PublicOverview,
  Section, Snapshot, SourceState, SystemData,
} from '../api/types'

/** A malformed reading is never silently converted into a healthy zero. */
export class InvalidPayload extends Error {
  constructor() { super('监控响应格式无效'); this.name = 'InvalidPayload' }
}
const fail = (): never => { throw new InvalidPayload() }
const object = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : fail()
const string = (v: unknown): string => typeof v === 'string' && v.length <= 65536 ? v : fail()
const nullableString = (v: unknown): string | null => v === null ? null : string(v)
const number = (v: unknown): number => typeof v === 'number' && Number.isFinite(v) ? v : fail()
const count = (v: unknown): number => { const n = number(v); return Number.isInteger(n) && n >= 0 ? n : fail() }
const nullableNumber = (v: unknown): number | null => v === null ? null : number(v)
const boolean = (v: unknown): boolean => typeof v === 'boolean' ? v : fail()
const nullableBoolean = (v: unknown): boolean | null => v === null ? null : boolean(v)
const array = <T>(v: unknown, parse: (item: unknown) => T, limit = 5000): T[] =>
  Array.isArray(v) && v.length <= limit ? v.map(parse) : fail()
const oneOf = <T extends string>(v: unknown, choices: readonly T[]): T =>
  typeof v === 'string' && choices.includes(v as T) ? v as T : fail()
const lanes = ['chat', 'embed', 'rerank', 'asr'] as const
const source = (v: unknown): SourceState => {
  const o = object(v)
  const error = o.error === null ? null : object(o.error)
  return {
    last_attempt_at: nullableNumber(o.last_attempt_at), last_success_at: nullableNumber(o.last_success_at),
    stale_after_ms: count(o.stale_after_ms), stale: boolean(o.stale),
    error: error ? { code: string(error.code), message: string(error.message) } : null,
  }
}
export const parseSystem = (v: unknown): SystemData => {
  const o = object(v), cpu = object(o.cpu), mem = object(o.memory)
  return {
    cpu: { user: nullableNumber(cpu.user), sys: nullableNumber(cpu.sys), idle: nullableNumber(cpu.idle) },
    memory: { total_gb: nullableNumber(mem.total_gb), used_gb: nullableNumber(mem.used_gb),
      free_gb: nullableNumber(mem.free_gb), wired_gb: nullableNumber(mem.wired_gb) },
    load_avg: array(o.load_avg, nullableNumber, 3),
  }
}
const lane = (v: unknown): Lane => {
  const o = object(v)
  return { active: count(o.active), waiting: count(o.waiting), max: count(o.max), queue_depth: count(o.queue_depth) }
}
const model = (v: unknown): MonitorModel => {
  const o = object(v), lifecycle = object(o.lifecycle), availability = object(o.availability)
  const routing = object(o.routing), activity = object(o.activity), support = object(o.monitoring_support)
  const budget = o.budget === null ? null : object(o.budget)
  return {
    key: string(o.key), alias: string(o.alias), backend: string(o.backend), backend_model: nullableString(o.backend_model),
    capabilities: array(o.capabilities, string, 30), registered: boolean(o.registered),
    management: oneOf(o.management, ['process', 'launchd', 'external']), port: nullableNumber(o.port),
    lifecycle: { state: oneOf(lifecycle.state, ['stopped', 'starting', 'running', 'stale', 'unknown', 'unmanaged']), reason: nullableString(lifecycle.reason) },
    availability: { state: oneOf(availability.state, ['healthy', 'unready', 'unauthorized', 'unreachable', 'unknown']), reason: nullableString(availability.reason) },
    routing: { available: nullableBoolean(routing.available), reason: nullableString(routing.reason) },
    activity: { active: count(activity.active), waiting: count(activity.waiting), uncertain: boolean(activity.uncertain), scope: oneOf(activity.scope, ['gateway']) },
    monitoring_support: { health: boolean(support.health), metrics: boolean(support.metrics), slots: boolean(support.slots),
      process_stats: boolean(support.process_stats), output: boolean(support.output) },
    budget: budget ? { used: number(budget.used), total: number(budget.total) } : null,
    loaded: nullableBoolean(o.loaded), endpoint: nullableString(o.endpoint),
  }
}
export function parseSnapshot(value: unknown): Snapshot {
  const o = object(value), sources = object(o.sources), laneValues = object(o.lanes)
  if (o.schema_version !== 1) return fail()
  const models = array(o.models, model)
  if (new Set(models.map(m => m.key)).size !== models.length) return fail()
  return {
    schema_version: 1, snapshot_id: string(o.snapshot_id), generated_at: number(o.generated_at),
    sources: { system: source(sources.system), catalog: source(sources.catalog), discovery: source(sources.discovery) },
    system: o.system === null ? null : parseSystem(o.system),
    lanes: Object.fromEntries(lanes.map(key => [key, lane(laneValues[key])])) as Record<LaneKey, Lane>,
    models,
    diagnostics: array(o.diagnostics, (v): Diagnostic => {
      const d = object(v)
      return { code: string(d.code), severity: oneOf(d.severity, ['info', 'warning', 'error']), message: string(d.message), model_key: nullableString(d.model_key) }
    }),
  }
}
const states: readonly DataState[] = ['loading', 'ready', 'stale', 'unauthorized', 'unsupported', 'error']
const section = (v: unknown): Section => {
  const o = object(v)
  return { state: oneOf(o.state, states), message: nullableString(o.message) }
}
export function parseDetail(value: unknown, requestedKey: string, includeOutput = false): ModelDetail {
  const o = object(value), metrics = object(o.metrics), slots = object(o.slots), process = object(o.process), ollama = object(o.ollama)
  if (o.schema_version !== 1 || o.key !== requestedKey) return fail()
  return {
    schema_version: 1, key: string(o.key), generated_at: number(o.generated_at), source: source(o.source),
    health: section(o.health), metrics: { ...section(metrics), values: array(metrics.values, v => {
      const m = object(v); return { name: string(m.name), value: nullableNumber(m.value), unit: string(m.unit) }
    }, 1000) },
    slots: { ...section(slots), items: array(slots.items, v => {
      const s = object(v)
      const item = { id: typeof s.id === 'number' ? number(s.id) : string(s.id), busy: boolean(s.busy),
        decoded: nullableNumber(s.decoded), limit: nullableNumber(s.limit), prompt_tokens: nullableNumber(s.prompt_tokens) }
      // Even an over-eager backend cannot expose output unless this request opted in.
      return includeOutput ? { ...item,
        ...(s.generated === undefined ? {} : { generated: string(s.generated) }),
        ...(s.reasoning === undefined ? {} : { reasoning: string(s.reasoning) }) } : item
    }, 256) },
    process: { ...section(process), cpu_pct: nullableNumber(process.cpu_pct), rss_gb: nullableNumber(process.rss_gb) },
    ollama: { ...section(ollama), version: nullableString(ollama.version ?? null), loaded: nullableBoolean(ollama.loaded), size_gb: nullableNumber(ollama.size_gb),
      vram_gb: nullableNumber(ollama.vram_gb), quantization: nullableString(ollama.quantization), expires_at: nullableString(ollama.expires_at) },
  }
}
export function parsePublicModels(value: unknown): Pick<PublicOverview, 'lanes' | 'model_count'> {
  const o = object(value)
  if (!Array.isArray(o.models) || o.models.length > 5000) return fail()
  const values = o.lanes === undefined ? {} : object(o.lanes)
  const result: Partial<Record<LaneKey, Lane>> = {}
  for (const key of lanes) if (values[key] !== undefined) result[key] = lane(values[key])
  return { lanes: result, model_count: o.models.length }
}
export function parsePublicSystem(value: unknown): Pick<PublicOverview, 'system' | 'last_success_at'> {
  const o = object(value)
  return { system: parseSystem(o), last_success_at: o.cached_at === undefined ? null : number(o.cached_at) * 1000 }
}
