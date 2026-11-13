import { isLocalConsole, requestLocalSession } from '../shared/auth/localSession'
import type { Snapshot } from './types'

const SNAPSHOT = '/agents-api/v1/snapshot'

export interface AgentsClientOptions {
  onSnapshot(snapshot: Snapshot): void
  onStatus(status: 'connected' | 'disconnected' | 'reconnecting'): void
}

/** Same local console grant as the monitor and chat pages. No root key, no cookie. */
export function connectAgents(opts: AgentsClientOptions): () => void {
  let dead = false
  let token = ''
  let timer: ReturnType<typeof setTimeout> | null = null
  let resume: (() => void) | null = null
  const controller = new AbortController()

  async function tick() {
    if (dead) return
    try {
      if (!token) {
        if (!isLocalConsole(location.hostname)) {
          opts.onStatus('disconnected')
          schedule(5000)
          return
        }
        const session = await requestLocalSession(location.hostname, controller.signal)
        if (dead) return
        if (!session) {
          opts.onStatus('disconnected')
          schedule(3000)
          return
        }
        token = session.token
      }
      const response = await fetch(SNAPSHOT, {
        method: 'GET',
        signal: controller.signal,
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${token}`,
          'X-Local-Console': '1',
        },
        credentials: 'omit',
        cache: 'no-store',
        referrerPolicy: 'no-referrer',
        redirect: 'error',
      })
      if (response.status === 401) {
        token = ''
        await response.body?.cancel()
        opts.onStatus('reconnecting')
        schedule(500)
        return
      }
      if (!response.ok) {
        await response.body?.cancel()
        opts.onStatus('reconnecting')
        schedule(2000)
        return
      }
      const snapshot = await response.json() as Snapshot
      if (dead) return
      opts.onSnapshot(snapshot)
      opts.onStatus('connected')
      schedule(1000)
    } catch {
      if (dead || controller.signal.aborted) return
      opts.onStatus('reconnecting')
      schedule(2000)
    }
  }

  function schedule(delay: number) {
    if (dead) return
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      if (document.hidden) {
        resume = () => {
          if (resume) document.removeEventListener('visibilitychange', resume)
          resume = null
          void tick()
        }
        document.addEventListener('visibilitychange', resume)
        return
      }
      void tick()
    }, delay)
  }

  void tick()
  return () => {
    dead = true
    controller.abort()
    if (timer !== null) clearTimeout(timer)
    if (resume) document.removeEventListener('visibilitychange', resume)
  }
}
