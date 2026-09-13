import type { HistorySample, SystemData } from '../api/types'
export const HISTORY_WINDOW = 5 * 60 * 1000
const MAX_SAMPLES = 180
export function addHistory(samples: readonly HistorySample[], system: SystemData | null, time: number | null, now: number, valid = true): HistorySample[] {
  const kept = samples.filter(sample => sample.time >= now - HISTORY_WINDOW).slice(-MAX_SAMPLES)
  if (time === null || !Number.isFinite(time) || time < now - HISTORY_WINDOW || time > now + 1000 || kept.some(sample => sample.time === time)) return kept
  const cpu = valid && system?.cpu.user !== null && system?.cpu.sys !== null && system ? system.cpu.user + system.cpu.sys : null
  const memory = valid && system ? system.memory.used_gb : null
  const latest = kept.at(-1)
  if (latest && time < latest.time) return kept
  // A missed interval stays visibly disconnected; do not interpolate missing history.
  if (latest && time - latest.time > 15_000) kept.push({ time: latest.time + 1, cpu: null, memory: null })
  kept.push({ time, cpu, memory })
  return kept.slice(-MAX_SAMPLES)
}
