/** Manually entered gateway key for this origin. Console session tokens are not stored. */
const STORAGE_KEY = 'local-llm-deploy.gateway-api-key'

function acceptable(value: string) {
  const token = value.trim()
  return token.length > 0 && token.length <= 8192 && !/[\r\n]/.test(token)
}

export function readStoredKey(storage: Storage | null = globalThis.localStorage): string {
  try {
    const value = storage?.getItem(STORAGE_KEY) ?? ''
    return acceptable(value) ? value.trim() : ''
  } catch {
    return ''
  }
}

export function writeStoredKey(value: string, storage: Storage | null = globalThis.localStorage) {
  try {
    if (!storage) return
    if (!value) storage.removeItem(STORAGE_KEY)
    else if (acceptable(value)) storage.setItem(STORAGE_KEY, value.trim())
  } catch {
    // Private mode or a blocked storage API still leaves the key in page memory.
  }
}
