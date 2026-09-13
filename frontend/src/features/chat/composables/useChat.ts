import { computed, onBeforeUnmount, ref } from 'vue'
import type { ConsoleContext } from '../../../app/context'
import type { Answer, Session, Turn } from '../domain/types'
import { contextMessages } from '../domain/context'
import { ChatError, streamChat } from '../api/client'
import { capabilities, wireParameters } from '../domain/parameters'
import { useChatHistory } from './useChatHistory'

export function useChat(context: ConsoleContext, options: { persistence?: boolean } = {}) {
  const busySession = ref<string | null>(null), error = ref('')
  let controller: AbortController | null = null, generation = 0
  function stop() { controller?.abort(); controller = null }
  const history = useChatHistory(context, { enabled: options.persistence !== false, onReset() {
    generation++; stop(); busySession.value = null; error.value = ''
  } })
  const { sessions, selectedId, current } = history
  const models = computed(() => context.monitor.snapshot.value?.models.filter(m => m.capabilities.includes('chat')) ?? [])
  function create(model = current.value?.model ?? '') {
    error.value = ''
    return history.create(model)
  }
  async function remove(id: string) {
    if (busySession.value === id) stop()
    return history.remove(id)
  }
  async function generate(session: Session, turn: Turn) {
    if (busySession.value || history.loading.value || !history.isLoaded(session.id)) return
    error.value = ''
    const model = models.value.find(m => m.key === session.model)
    if (!model || model.routing.available !== true) { error.value = '请选择当前可路由的对话模型'; return }
    try { wireParameters(session.parameters, model.backend, model.chat_controls) } catch (cause) { error.value = (cause as Error).message; return }
    const request = { model: model.key, backend: model.backend, parameters: { ...session.parameters },
      chat_controls: model.chat_controls ? { ...model.chat_controls, reasoning_efforts: [...model.chat_controls.reasoning_efforts] } : null,
      messages: contextMessages(session, session.turns.indexOf(turn)) }
    request.messages.push({ role: 'user', content: turn.user })
    const answer: Answer = { id: crypto.randomUUID(), content: '', reasoning: '', status: 'waiting', adopted: false, startedAt: Date.now(), request }
    turn.answers.push(answer); turn.selected = turn.answers.length - 1
    // Work through the reactive proxy so streaming changes render immediately.
    const live = turn.answers[turn.selected]!
    const own = ++generation, abort = new AbortController()
    let start: number | undefined
    controller = abort; busySession.value = session.id
    let timedOut = false
    const deadline = setTimeout(() => { timedOut = true; abort.abort() }, 15 * 60 * 1000)
    let pendingContent = '', pendingReasoning = '', timer: ReturnType<typeof setTimeout> | undefined
    function flush() {
      clearTimeout(timer); timer = undefined
      if (own !== generation) return
      live.content += pendingContent; live.reasoning += pendingReasoning
      pendingContent = ''; pendingReasoning = ''
    }
    try {
      await history.flush()
      if (own !== generation) return
      if (abort.signal.aborted) { live.status = 'stopped'; return }
      start = performance.now(); live.startedAt = Date.now()
      await streamChat(request, context.credential.value, abort.signal, update => {
        if (own !== generation || abort.signal.aborted) return
        if (update.content || update.reasoning) {
          live.firstContentMs ??= performance.now() - start!
          live.status = 'streaming'
          pendingContent += update.content ?? ''; pendingReasoning += update.reasoning ?? ''
          timer ??= setTimeout(flush, 50)
        }
        if (update.usage) live.usage = { ...live.usage, ...update.usage }
        if (update.finishReason) live.finishReason = update.finishReason
      })
      if (own === generation) live.status = abort.signal.aborted ? 'stopped' : 'complete'
    } catch (cause) {
      if (own === generation) {
        live.status = abort.signal.aborted && !timedOut ? 'stopped' : 'error'
        if (timedOut) live.error = '请求超过 15 分钟页面时限，已停止接收；后端可能仍在收尾'
        else if (!abort.signal.aborted) live.error = cause instanceof Error ? cause.message : '连接失败，请手动重试'
        if (!session.draft && cause instanceof ChatError && (cause.status === 413 || cause.message.includes('上下文超过'))) session.draft = turn.user
      }
    } finally {
      clearTimeout(deadline)
      flush()
      if (own === generation) {
        live.durationMs = start === undefined ? 0 : performance.now() - start; busySession.value = null; controller = null
        await history.flush()
      }
    }
  }
  async function send() {
    const session = current.value
    if (!session || !session.draft.trim() || busySession.value || history.loading.value || !history.isLoaded(session.id)) return
    const model = models.value.find(m => m.key === session.model)
    if (!model || model.routing.available !== true) { error.value = '请选择当前可路由的对话模型'; return }
    try { wireParameters(session.parameters, model.backend, model.chat_controls) } catch (cause) { error.value = (cause as Error).message; return }
    const text = session.draft.trim()
    session.turns.push({ id: crypto.randomUUID(), user: text, answers: [], selected: 0 })
    if (session.turns.length === 1 && session.title === '新对话') session.title = [...text].slice(0, 32).join('')
    session.draft = ''
    await generate(session, session.turns[session.turns.length - 1]!)
  }
  async function regenerate() {
    const session = current.value, turn = session?.turns.at(-1)
    if (session && turn) await generate(session, turn)
  }
  function editLast() {
    const session = current.value
    if (!session || busySession.value) return
    const turn = session.turns.pop()
    if (turn) session.draft = turn.user
  }
  function switchModel(model: string, carry: boolean) {
    if (busySession.value || history.loading.value) return
    if (current.value && (!current.value.turns.length || carry)) {
      if (current.value.model !== model) {
        delete current.value.parameters.thinking
        delete current.value.parameters.reasoning_effort
        delete current.value.parameters.reasoning_budget_tokens
      }
      current.value.model = model
      if (!capabilities(models.value.find(m => m.key === model)?.backend ?? '').seed) delete current.value.parameters.seed
    }
    else create(model)
  }
  onBeforeUnmount(() => { generation++; stop() })
  return { history, sessions, selectedId, current, busySession, models, error, create, remove, stop, send, regenerate, editLast, switchModel }
}
