import type { ModelDetail, MonitorModel, Snapshot, SourceState } from '../../src/api/types'

export function source(now = Date.now()): SourceState {
  return { last_attempt_at: now, last_success_at: now, stale_after_ms: 15000, stale: false, error: null }
}

function model(key: string, alias: string, backend: string, capabilities: string[], extra: Partial<MonitorModel> = {}): MonitorModel {
  return {
    key, alias, backend, capabilities, backend_model: null, registered: true,
    management: 'process', port: 8002,
    lifecycle: { state: 'running', reason: null }, availability: { state: 'healthy', reason: null },
    routing: { available: true, reason: null },
    activity: { active: 0, waiting: 0, uncertain: false, scope: 'gateway' },
    monitoring_support: { health: true, metrics: backend === 'llama_cpp', slots: backend === 'llama_cpp', process_stats: true, output: backend === 'llama_cpp' },
    budget: null, loaded: true, endpoint: `/api/${encodeURIComponent(key)}/v1`, ...extra,
  }
}

export function createSnapshot(now = Date.now()): Snapshot {
  return {
    schema_version: 1, snapshot_id: `fixture-${now}`, generated_at: now,
    sources: { system: source(now), catalog: source(now), discovery: source(now) },
    system: {
      cpu: { user: 28.4, sys: 6.8, idle: 64.8 },
      memory: { total_gb: 128, used_gb: 76.8, free_gb: 51.2, wired_gb: 10.2 },
      load_avg: [4.21, 3.86, 3.42],
    },
    lanes: {
      chat: { active: 1, max: 1, waiting: 2, queue_depth: 3 },
      embed: { active: 1, max: 2, waiting: 0, queue_depth: 1 },
      rerank: { active: 0, max: 1, waiting: 0, queue_depth: 0 },
      asr: { active: 0, max: 1, waiting: 0, queue_depth: 0 },
    },
    models: [
      model('qwen3.8-27b', 'Qwen 3.8 · 27B', 'llama_cpp', ['chat'], {
        activity: { active: 1, waiting: 2, uncertain: false, scope: 'gateway' }, budget: { used: 65536, total: 188744 },
      }),
      model('jina-embed', 'Jina Embeddings v3', 'transformers_embedding', ['embedding'], {
        management: 'launchd', port: 8004, activity: { active: 1, waiting: 0, uncertain: false, scope: 'gateway' },
      }),
      model('jina-rerank', 'Jina Reranker v2', 'mlx_rerank', ['rerank'], { management: 'launchd', port: 8006 }),
      model('whisper-large-v3', 'Whisper Large v3', 'mlx_whisper', ['asr'], {
        management: 'launchd', port: 8007, loaded: false,
        lifecycle: { state: 'stopped', reason: '服务尚未启动' },
        availability: { state: 'unknown', reason: '尚无就绪实例' },
        routing: { available: false, reason: '未发现就绪实例' },
      }),
      model('qwen3-8b', 'Qwen 3 · 8B', 'ollama', ['chat'], {
        backend_model: 'qwen3:8b', management: 'external', port: 11434, loaded: false,
        lifecycle: { state: 'unmanaged', reason: '由 Ollama 管理，按需加载' },
        monitoring_support: { health: true, metrics: false, slots: false, process_stats: false, output: false },
      }),
      model('deepseek-v4', 'DeepSeek V4 Flash', 'external_http', ['chat'], {
        management: 'external', port: 8005, loaded: null,
        lifecycle: { state: 'unmanaged', reason: '外部服务' },
        availability: { state: 'unreachable', reason: '健康检查未通过' },
        routing: { available: false, reason: '后端不可达' },
        monitoring_support: { health: true, metrics: false, slots: false, process_stats: false, output: false },
      }),
      model('nomic-embed-text', 'Nomic Embed Text', 'ollama', ['embedding'], {
        backend_model: 'nomic-embed-text:latest', management: 'external', port: 11434, registered: false, loaded: false,
        lifecycle: { state: 'unmanaged', reason: 'Ollama 自动发现' },
        monitoring_support: { health: true, metrics: false, slots: false, process_stats: false, output: false },
      }),
    ],
    diagnostics: [{ code: 'backend_unreachable', severity: 'warning', model_key: 'deepseek-v4', message: 'DeepSeek V4 Flash 暂不可用，其他模型可继续调用。' }],
  }
}

export function createDetail(key: string, output = false, now = Date.now()): ModelDetail {
  const m = createSnapshot(now).models.find(item => item.key === key)!
  const supported = m?.backend === 'llama_cpp'
  const unavailable = m?.routing.available === false
  return {
    schema_version: 1, key, generated_at: now, source: source(now),
    health: { state: unavailable ? 'error' : 'ready', message: unavailable ? '未发现健康实例' : '健康检查通过' },
    metrics: { state: supported ? 'ready' : 'unsupported', message: supported ? null : '此后端未提供运行指标', values: supported ? [
      { name: 'llamacpp:prompt_tokens_seconds', value: 1246.8, unit: 'tokens/s' },
      { name: 'llamacpp:predicted_tokens_seconds', value: 42.6, unit: 'tokens/s' },
      { name: 'llamacpp:tokens_predicted_total', value: 283640, unit: 'tokens' },
    ] : [] },
    slots: { state: supported ? 'ready' : 'unsupported', message: supported ? null : '此后端不提供槽位', items: supported ? [
      { id: 0, busy: true, decoded: 1842, limit: 8192, prompt_tokens: 4608, ...(output ? { generated: '这是一段演示输出。控制台保留阅读位置，只有位于底部时才自动跟随新内容。', reasoning: '演示数据，不对应真实请求。' } : {}) },
    ] : [] },
    process: { state: m?.management === 'external' ? 'unsupported' : unavailable ? 'error' : 'ready', message: null, cpu_pct: unavailable ? null : 136.4, rss_gb: unavailable ? null : 24.6 },
    ollama: { state: m?.backend === 'ollama' ? 'ready' : 'unsupported', message: null, version: m?.backend === 'ollama' ? 'fixture-version' : null, loaded: m?.backend === 'ollama' ? m.loaded : null, size_gb: m?.backend === 'ollama' ? 5.2 : null, vram_gb: m?.loaded && m?.backend === 'ollama' ? 5.2 : null, quantization: m?.backend === 'ollama' ? 'Q4_K_M' : null, expires_at: null },
  }
}
