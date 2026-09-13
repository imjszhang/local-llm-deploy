import { describe, expect, it } from 'vitest'
import { addHistory, HISTORY_WINDOW } from '../../src/domain/history'
import { sampleSnapshot } from './data-fixtures'

describe('bounded observation history', () => {
  it('deduplicates backend collection times and leaves a gap after missing intervals', () => {
    const now = 1_000_000, system = sampleSnapshot(now).system
    const first = addHistory([], system, now, now)
    expect(addHistory(first, system, now, now + 5000)).toEqual(first)
    const withGap = addHistory(first, system, now + 20000, now + 20000)
    expect(withGap).toHaveLength(3)
    expect(withGap[1]).toEqual({ time: now + 1, cpu: null, memory: null })
    expect(withGap[2]?.cpu).toBe(25)
  })
  it('never interpolates missing, stale, future or non-finite readings', () => {
    const now = 1_000_000, system = sampleSnapshot(now).system
    expect(addHistory([], system, null, now)).toEqual([])
    expect(addHistory([], system, Infinity, now)).toEqual([])
    expect(addHistory([], system, now + 5000, now)).toEqual([])
    expect(addHistory([], system, now, now, false)[0]).toEqual({ time: now, cpu: null, memory: null })
    expect(addHistory([], system, now - HISTORY_WINDOW - 1, now)).toEqual([])
  })
  it('prunes history by time and caps retained samples', () => {
    const now = 1_000_000, system = sampleSnapshot(now).system
    let samples = addHistory([], system, now, now)
    for (let n = 1; n <= 500; n++) samples = addHistory(samples, system, now + n * 1000, now + n * 1000)
    expect(samples.length).toBeLessThanOrEqual(180)
    expect(samples.every(s => s.time >= now + 500000 - HISTORY_WINDOW)).toBe(true)
  })
})
