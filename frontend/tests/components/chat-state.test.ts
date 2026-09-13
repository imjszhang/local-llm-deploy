import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { useChat } from '../../src/features/chat/composables/useChat'
import { useMonitor } from '../../src/features/monitor/composables/useMonitor'
import { createSnapshot } from '../fixtures/monitor'

afterEach(() => vi.unstubAllGlobals())
function setup() {
  const monitor = useMonitor(), credential = ref('secret'), credentialGeneration = ref(0)
  monitor.snapshot.value = createSnapshot()
  let chat!: ReturnType<typeof useChat>, stream!: ReadableStreamDefaultController<Uint8Array>
  vi.stubGlobal('fetch', vi.fn(async (_url, options) => new Response(new ReadableStream({ start(controller) {
    stream = controller
    options.signal.addEventListener('abort', () => controller.error(new DOMException('Cancelled', 'AbortError')))
  } }), { headers: { 'Content-Type': 'text/event-stream' } })))
  const wrapper = mount(defineComponent({ setup() { chat = useChat({ monitor, credential, credentialGeneration }, { persistence: false }); return () => null } }))
  chat.current.value!.model = 'qwen3.8-27b'; chat.current.value!.draft = 'question'
  const push = (text: string) => stream.enqueue(new TextEncoder().encode(text))
  return { chat, wrapper, credentialGeneration, push }
}
describe('chat request ownership', () => {
  it('keeps request with its original session and blocks duplicate dispatch', async () => {
    const { chat, wrapper, push } = setup()
    const original = chat.current.value!
    const sending = chat.send()
    await Promise.resolve()
    chat.create('qwen3-8b'); chat.current.value!.draft = 'other'
    await chat.send()
    expect(fetch).toHaveBeenCalledTimes(1)
    push('data: {"choices":[{"delta":{"content":"original answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    await sending
    expect(original.turns[0]!.answers[0]!.content).toBe('original answer')
    expect(chat.current.value!.turns).toHaveLength(0)
    expect(chat.busySession.value).toBeNull()
    wrapper.unmount()
  })
  it('stop preserves partial text and does not adopt it', async () => {
    const { chat, wrapper, push } = setup()
    const sending = chat.send(); await Promise.resolve()
    push('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
    await new Promise(resolve => setTimeout(resolve, 0))
    chat.stop(); await sending
    const answer = chat.current.value!.turns[0]!.answers[0]!
    expect(answer.status).toBe('stopped'); expect(answer.content).toBe('partial'); expect(answer.adopted).toBe(false)
    wrapper.unmount()
  })
  it('credential change synchronously clears sessions and invalidates pending writes', async () => {
    const { chat, wrapper, credentialGeneration } = setup()
    const sending = chat.send(); await Promise.resolve()
    credentialGeneration.value++
    expect(chat.current.value!.turns).toHaveLength(0)
    expect(chat.busySession.value).toBeNull()
    await sending
    expect(chat.current.value!.turns).toHaveLength(0)
    wrapper.unmount()
  })
  it('deleting the active session stops it and leaves a fresh session usable', async () => {
    const { chat, wrapper } = setup()
    const id = chat.current.value!.id
    const sending = chat.send(); await Promise.resolve()
    await chat.remove(id)
    await sending
    expect(chat.sessions.value.some(session => session.id === id)).toBe(false)
    expect(chat.current.value!.turns).toHaveLength(0)
    expect(chat.busySession.value).toBeNull()
    wrapper.unmount()
  })
  it('preserves a reasoning snapshot while later settings and model choices change', async () => {
    const { chat, wrapper, push } = setup()
    const original = chat.current.value!
    const controls = { thinking: true, reasoning_efforts: ['low', 'medium', 'xhigh'], reasoning_budget: true, default_thinking: false, default_effort: 'xhigh', source: 'configured' as const }
    chat.models.value[0]!.chat_controls = controls
    original.parameters = { thinking: true, reasoning_effort: 'xhigh', reasoning_budget_tokens: 32, temperature: 0.4 }
    const sending = chat.send(); await Promise.resolve()
    original.parameters.reasoning_effort = 'low'
    controls.reasoning_efforts.push('custom')
    push('data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    await sending
    const request = original.turns[0]!.answers[0]!.request
    expect(request.parameters.reasoning_effort).toBe('xhigh')
    expect(request.chat_controls?.reasoning_efforts).toEqual(['low', 'medium', 'xhigh'])
    chat.switchModel('qwen3-8b', true)
    expect(original.parameters).toEqual({ temperature: 0.4 })
    expect(request.parameters.thinking).toBe(true)
    expect(request.model).toBe('qwen3.8-27b')
    wrapper.unmount()
  })
  it('keeps the draft when stale thinking controls are rejected', async () => {
    const { chat, wrapper } = setup()
    chat.models.value[0]!.chat_controls = null
    chat.current.value!.parameters = { thinking: true }
    await chat.send()
    expect(fetch).not.toHaveBeenCalled()
    expect(chat.current.value!.draft).toBe('question')
    expect(chat.current.value!.turns).toHaveLength(0)
    expect(chat.error.value).toContain('尚未声明')
    wrapper.unmount()
  })
  it('stopping while the initial save is pending never dispatches inference or leaves a waiting answer', async () => {
    const { chat, wrapper } = setup()
    let finish!: () => void
    const saving = new Promise<void>(resolve => { finish = resolve })
    vi.spyOn(chat.history, 'flush').mockReturnValueOnce(saving).mockResolvedValue(undefined)
    const sending = chat.send()
    chat.stop(); finish()
    await sending
    expect(fetch).not.toHaveBeenCalled()
    expect(chat.current.value!.turns[0]!.answers[0]!.status).toBe('stopped')
    expect(chat.busySession.value).toBeNull()
    wrapper.unmount()
  })
})
