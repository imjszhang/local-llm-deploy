import { afterEach, describe, expect, it } from 'vitest'
import { reactive } from 'vue'
import { mount, type VueWrapper } from '@vue/test-utils'
import ParameterPanel from '../../src/features/chat/components/ParameterPanel.vue'
import type { ChatControls } from '../../src/features/monitor/api/types'
import type { Session } from '../../src/features/chat/domain/types'

const wrappers: VueWrapper[] = []
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
const controls: ChatControls = {
  thinking: true, reasoning_efforts: ['low', 'medium', 'xhigh'], reasoning_budget: true,
  default_thinking: true, default_effort: 'xhigh', source: 'configured',
}
function setup(declared: ChatControls | null = controls) {
  const session = reactive<Session>({ id: 's', title: 'test', model: 'model', system: 'system', parameters: { temperature: 0.5 }, turns: [], draft: 'draft' })
  const wrapper = mount(ParameterPanel, { props: { modelValue: session, backend: 'llama', controls: declared } })
  wrappers.push(wrapper)
  return { session, wrapper }
}

describe('chat thinking controls', () => {
  it('offers only declared levels after thinking is explicitly enabled', async () => {
    const { session, wrapper } = setup()
    expect(wrapper.get<HTMLSelectElement>('#chat-thinking').element.value).toBe('default')
    expect(wrapper.text()).toContain('后端默认开启')
    expect(wrapper.find('#chat-reasoning-effort').exists()).toBe(false)
    expect(wrapper.find('#chat-reasoning-budget').exists()).toBe(false)
    await wrapper.get('#chat-thinking').setValue('on')
    expect(session.parameters.thinking).toBe(true)
    expect(wrapper.findAll('#chat-reasoning-effort option').map(option => option.attributes('value'))).toEqual(['', 'low', 'medium', 'xhigh'])
    expect(wrapper.text()).toContain('不保证固定 token 数')
    expect(wrapper.text()).toContain('后端默认：极高')
    await wrapper.get('#chat-reasoning-effort').setValue('xhigh')
    expect(session.parameters.reasoning_effort).toBe('xhigh')
    await wrapper.get('#chat-reasoning-effort').setValue('')
    expect(session.parameters).not.toHaveProperty('reasoning_effort')
  })

  it('clears effort and budget on off or default without clearing other parameters', async () => {
    const { session, wrapper } = setup()
    for (const mode of ['off', 'default']) {
      await wrapper.get('#chat-thinking').setValue('on')
      await wrapper.get('#chat-reasoning-effort').setValue('low')
      await wrapper.get('#chat-reasoning-budget').setValue('4096')
      await wrapper.get('#chat-thinking').setValue(mode)
      expect(session.parameters).toEqual(mode === 'off' ? { temperature: 0.5, thinking: false } : { temperature: 0.5 })
      expect(wrapper.find('#chat-reasoning-effort').exists()).toBe(false)
      expect(wrapper.find('#chat-reasoning-budget').exists()).toBe(false)
      expect(session.system).toBe('system')
      expect(session.draft).toBe('draft')
    }
  })

  it('keeps the optional budget numeric and removes an empty budget', async () => {
    const { session, wrapper } = setup()
    await wrapper.get('#chat-thinking').setValue('on')
    const budget = wrapper.get('#chat-reasoning-budget')
    expect(budget.attributes()).toMatchObject({ type: 'number', min: '1', max: '131072', step: '1' })
    await budget.setValue('4096')
    expect(session.parameters.reasoning_budget_tokens).toBe(4096)
    expect(wrapper.text()).toContain('最大输出 token 包含思考和正文')
    await budget.setValue('')
    expect(session.parameters).not.toHaveProperty('reasoning_budget_tokens')
  })

  it('does not advertise undeclared controls or an unsupported budget', async () => {
    const { wrapper } = setup(null)
    expect(wrapper.text()).toContain('尚未声明支持思考设置')
    expect(wrapper.find('#chat-thinking').exists()).toBe(false)
    await wrapper.setProps({ controls: { ...controls, reasoning_efforts: ['low', 'medium', 'high'], reasoning_budget: false } })
    await wrapper.get('#chat-thinking').setValue('on')
    expect(wrapper.findAll('#chat-reasoning-effort option').map(option => option.attributes('value'))).toEqual(['', 'low', 'medium', 'high'])
    expect(wrapper.find('#chat-reasoning-budget').exists()).toBe(false)
    await wrapper.setProps({ controls: { ...controls, reasoning_efforts: [], reasoning_budget: false } })
    expect(wrapper.find('#chat-reasoning-effort').exists()).toBe(false)
    expect(wrapper.text()).toContain('未声明支持分级推理强度')
    await wrapper.setProps({ controls: { ...controls, thinking: false } })
    expect(wrapper.find('#chat-thinking').exists()).toBe(false)
  })

  it('allows resetting stale thinking overrides after model capabilities are withdrawn', async () => {
    const { session, wrapper } = setup()
    await wrapper.get('#chat-thinking').setValue('on')
    await wrapper.get('#chat-reasoning-effort').setValue('xhigh')
    await wrapper.get('#chat-reasoning-budget').setValue('4096')
    await wrapper.setProps({ controls: null })
    const reset = wrapper.findAll('button').find(button => button.text() === '重置思考设置')!
    expect(reset.exists()).toBe(true)
    await reset.trigger('click')
    expect(session.parameters).toEqual({ temperature: 0.5 })
    expect(wrapper.text()).not.toContain('重置思考设置')
  })
})
