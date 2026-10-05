import { describe, expect, it } from 'vitest'
import { readStoredKey, writeStoredKey } from '../../src/shared/auth/storedKey'

function memoryStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => { values.delete(key) },
    setItem: (key, value) => { values.set(key, value) },
  }
}

describe('stored gateway key', () => {
  it('round-trips a manual key and drops it when cleared', () => {
    const storage = memoryStorage()
    writeStoredKey('  gateway-key  ', storage)
    expect(readStoredKey(storage)).toBe('gateway-key')
    writeStoredKey('', storage)
    expect(readStoredKey(storage)).toBe('')
  })

  it('ignores blank, oversized, or multiline values', () => {
    const storage = memoryStorage()
    writeStoredKey('   ', storage)
    writeStoredKey('a\nb', storage)
    writeStoredKey('x'.repeat(8193), storage)
    expect(readStoredKey(storage)).toBe('')
    storage.setItem('local-llm-deploy.gateway-api-key', 'bad\nkey')
    expect(readStoredKey(storage)).toBe('')
  })
})
