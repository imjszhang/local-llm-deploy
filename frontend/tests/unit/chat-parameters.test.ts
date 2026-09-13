import { describe, expect, it, vi } from 'vitest'
import type { ChatControls } from '../../src/features/monitor/api/types'
import type { Parameters } from '../../src/features/chat/domain/types'
import { wireParameters } from '../../src/features/chat/domain/parameters'
import { streamChat } from '../../src/features/chat/api/client'
import { parseSnapshot } from '../../src/features/monitor/domain/validation'
import { sampleSnapshot } from './data-fixtures'

const llama: ChatControls = { thinking: true, reasoning_efforts: ['low', 'medium', 'xhigh'], reasoning_budget: true, default_thinking: false, default_effort: 'xhigh', source: 'configured' }
const ollama: ChatControls = { ...llama, reasoning_efforts: ['low', 'medium', 'high'], reasoning_budget: false, default_thinking: null, default_effort: null }

describe('verified thinking controls', () => {
  it('leaves every thinking field absent in default mode, including for old gateways', () => {
    for (const backend of ['llama_cpp', 'ollama', 'external_http']) {
      expect(wireParameters({ temperature: 0.4, max_tokens: 200 }, backend)).toEqual({ temperature: 0.4, max_tokens: 200 })
    }
    expect(wireParameters({}, 'llama_cpp', llama)).toEqual({ reasoning_format: 'deepseek' })
  })
  it('overrides llama defaults per request and accepts the actual template effort enum', () => {
    expect(wireParameters({ thinking: true, reasoning_effort: 'xhigh', reasoning_budget_tokens: 32, max_tokens: 100 }, 'llama_cpp', llama)).toEqual({
      max_tokens: 100, chat_template_kwargs: { enable_thinking: true }, reasoning_format: 'deepseek', reasoning_effort: 'xhigh', reasoning_budget_tokens: 32,
    })
    expect(wireParameters({ thinking: false }, 'llama_cpp', llama)).toEqual({ chat_template_kwargs: { enable_thinking: false } })
    expect(wireParameters({ thinking: true }, 'llama_cpp', llama)).not.toHaveProperty('reasoning_effort')
    expect(() => wireParameters({ thinking: true, reasoning_effort: 'high' }, 'llama_cpp', llama)).toThrow('不支持所选推理强度')
  })
  it('uses Ollama OpenAI reasoning_effort for both the toggle and renderer levels', () => {
    expect(wireParameters({ thinking: true }, 'ollama', ollama)).toEqual({ reasoning_effort: 'medium' })
    expect(wireParameters({ thinking: false }, 'ollama', ollama)).toEqual({ reasoning_effort: 'none' })
    expect(wireParameters({ thinking: true, reasoning_effort: 'high' }, 'ollama', ollama)).toEqual({ reasoning_effort: 'high' })
    expect(() => wireParameters({ thinking: true, reasoning_effort: 'xhigh' }, 'ollama', ollama)).toThrow('不支持所选推理强度')
    expect(() => wireParameters({ thinking: true, reasoning_budget_tokens: 30 }, 'ollama', ollama)).toThrow('不支持思考 token 预算')
  })
  it('rejects undeclared controls, conflicting choices and invalid budgets before fetching', async () => {
    expect(() => wireParameters({ thinking: false }, 'llama_cpp')).toThrow('尚未声明')
    expect(() => wireParameters({ thinking: true }, 'external_http', llama)).toThrow('尚未声明')
    expect(() => wireParameters({ reasoning_effort: 'medium' }, 'llama_cpp', llama)).toThrow('先开启')
    expect(() => wireParameters({ thinking: false, reasoning_effort: 'low' }, 'llama_cpp', llama)).toThrow('关闭')
    for (const budget of [0, -1, 1.5, NaN, Infinity, 131073]) {
      expect(() => wireParameters({ thinking: true, reasoning_budget_tokens: budget }, 'llama_cpp', llama)).toThrow()
    }
    expect(() => wireParameters({ thinking: true, reasoning_budget_tokens: 100, max_tokens: 100 }, 'llama_cpp', llama)).toThrow('最终回答')
    expect(() => wireParameters({ thinking: 'true' } as unknown as Parameters, 'llama_cpp', llama)).toThrow()
    const fetcher = vi.fn<typeof fetch>()
    await expect(streamChat({ model: 'test', backend: 'ollama', chat_controls: ollama, messages: [], parameters: { thinking: true, reasoning_effort: 'xhigh' } }, '', new AbortController().signal, () => {}, fetcher)).rejects.toThrow()
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('parses optional capabilities and rejects malformed declarations without exposing extra fields', () => {
    const data = sampleSnapshot()
    expect(parseSnapshot(data).models[0]?.chat_controls).toBeNull()
    data.models[0]!.chat_controls = { ...llama, private_template: 'private' } as ChatControls
    expect(parseSnapshot(data).models[0]?.chat_controls).toEqual(llama)
    data.models[0]!.chat_controls.reasoning_efforts = ['none']
    expect(() => parseSnapshot(data)).toThrow()
    data.models[0]!.chat_controls = { ...llama, thinking: false }
    expect(() => parseSnapshot(data)).toThrow()
  })
})
