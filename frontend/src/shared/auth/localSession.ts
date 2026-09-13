export interface LocalSession { token: string; expires_at: number }
export function isLocalConsole(hostname: string) {
  return ['localhost', '127.0.0.1', '[::1]', '::1'].includes(hostname.toLowerCase())
}

/** A short-lived console token, never the project's root API key. No persistent storage. */
export async function requestLocalSession(hostname: string, signal: AbortSignal, fetcher: typeof fetch = globalThis.fetch): Promise<LocalSession | null> {
  if (!isLocalConsole(hostname)) return null
  try {
    const response = await fetcher('/console-api/v1/session', {
      method: 'POST', headers: { 'X-Local-Console': '1', Accept: 'application/json' },
      signal, credentials: 'omit', redirect: 'error', cache: 'no-store', referrerPolicy: 'no-referrer',
    })
    if (!response.ok) { await response.body?.cancel(); return null }
    const reader = response.body?.getReader()
    if (!reader) return null
    let text = '', bytes = 0
    const decoder = new TextDecoder('utf-8', { fatal: true })
    try {
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        bytes += chunk.value.byteLength
        if (bytes > 16384) { await reader.cancel(); return null }
        text += decoder.decode(chunk.value, { stream: true })
      }
      text += decoder.decode()
    } finally { reader.releaseLock() }
    const value: unknown = JSON.parse(text)
    if (!value || typeof value !== 'object') return null
    const session = value as Partial<LocalSession>
    if (typeof session.token !== 'string' || !session.token || session.token.length > 8192 || /\s/.test(session.token)) return null
    if (typeof session.expires_at !== 'number' || !Number.isFinite(session.expires_at) || session.expires_at <= Date.now() || session.expires_at > Date.now() + 24 * 60 * 60 * 1000) return null
    return { token: session.token, expires_at: session.expires_at }
  } catch { return null }
}
