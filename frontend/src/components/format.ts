import type { MonitorModel, SourceState, SystemData } from "../api/types";

export function isReading(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function sourceStale(
  source: SourceState | null | undefined,
  now: number,
): boolean {
  return Boolean(
    source &&
      (source.stale ||
        source.last_success_at === null ||
        now - source.last_success_at > source.stale_after_ms),
  );
}

export function number(value: unknown, digits = 0): string {
  return isReading(value)
    ? value.toLocaleString("zh-CN", {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      })
    : "—";
}

export function cpuUsage(system: SystemData | null | undefined): number | null {
  if (!system) return null;
  const { user, sys, idle } = system.cpu;
  if (isReading(user) && isReading(sys))
    return Math.min(100, Math.max(0, user + sys));
  return isReading(idle) ? Math.min(100, Math.max(0, 100 - idle)) : null;
}

export function time(value: number | null | undefined): string {
  return isReading(value)
    ? new Date(value).toLocaleTimeString("zh-CN", { hour12: false })
    : "尚未成功采集";
}

export const capabilityLabels: Record<string, string> = {
  chat: "对话",
  embed: "向量",
  embedding: "向量",
  rerank: "重排",
  asr: "语音",
  transcription: "语音",
};
export const backendLabels: Record<string, string> = {
  llama_cpp: "llama.cpp",
  "llama.cpp": "llama.cpp",
  "llama-server": "llama.cpp",
  ollama: "Ollama",
  embedding: "Embedding",
  embed: "Embedding",
  rerank: "Rerank",
  whisper: "Whisper",
  external: "外部服务",
  external_http: "HTTP API",
  transformers_embedding: "Transformers",
  mlx_rerank: "MLX Rerank",
  mlx_whisper: "MLX Whisper",
};
export const lifecycleLabels: Record<string, string> = {
  stopped: "未运行",
  starting: "启动中",
  running: "运行中",
  stale: "记录异常",
  unknown: "生命周期未知",
  unmanaged: "外部管理",
};
export const availabilityLabels: Record<string, string> = {
  healthy: "可用",
  unready: "未就绪",
  unauthorized: "需要认证",
  unreachable: "不可达",
  unknown: "待确认",
};

export function modelStatus(model: MonitorModel): {
  label: string;
  tone: "good" | "warning" | "danger" | "muted";
} {
  if (model.activity.uncertain) return { label: "结束待确认", tone: "warning" };
  if (
    model.backend === "ollama" &&
    model.loaded === false &&
    model.availability.state !== "unreachable"
  )
    return { label: "未加载", tone: "muted" };
  if (model.availability.state === "healthy")
    return {
      label: model.activity.active > 0 ? "处理中" : "可用",
      tone: "good",
    };
  if (model.availability.state === "unauthorized")
    return { label: "需要认证", tone: "warning" };
  if (
    model.lifecycle.state === "starting" ||
    model.availability.state === "unready"
  )
    return { label: "未就绪", tone: "warning" };
  if (model.lifecycle.state === "stopped")
    return { label: "未运行", tone: "muted" };
  return {
    label: availabilityLabels[model.availability.state] || "待确认",
    tone: model.availability.state === "unreachable" ? "danger" : "muted",
  };
}

export function percent(used: unknown, total: unknown): number {
  return isReading(used) && isReading(total) && total > 0
    ? Math.min(100, Math.max(0, (used / total) * 100))
    : 0;
}
