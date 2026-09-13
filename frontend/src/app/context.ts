import type { InjectionKey, Ref } from 'vue'
import type { useMonitor } from '../features/monitor/composables/useMonitor'
export interface ConsoleContext {
  monitor: ReturnType<typeof useMonitor>
  credential: Ref<string>
  credentialGeneration: Ref<number>
  credentialSource?: Ref<'none' | 'local' | 'manual'>
  localConnecting?: Ref<boolean>
  connectLocal?: () => Promise<void>
}
export const consoleContext: InjectionKey<ConsoleContext> = Symbol('console')
