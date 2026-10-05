import { afterEach, describe, expect, it, vi } from 'vitest'
import { createId } from '../../src/features/chat/domain/id'

afterEach(() => vi.unstubAllGlobals())
describe('chat record IDs on LAN HTTP', () => {
  it('uses random bytes without depending on secure-context randomUUID', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => bytes.fill(255))
    vi.stubGlobal('crypto', { getRandomValues })
    expect(createId()).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff')
    expect(getRandomValues).toHaveBeenCalledOnce()
  })
  it('produces distinct, valid UUID v4 records for sessions, turns and answers', () => {
    const ids = Array.from({ length: 128 }, createId)
    expect(new Set(ids).size).toBe(ids.length)
    for (const id of ids) expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })
})
