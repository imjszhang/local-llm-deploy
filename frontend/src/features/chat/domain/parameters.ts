import type { ChatControls } from '../../monitor/api/types'
import type { Parameters } from './types'
import { validateParameters } from './context'

/** Verified against this project's llama.cpp and Ollama deployments. Unknown providers opt out. */
export function capabilities(backend: string) {
  const verified = backend === 'llama_cpp' || backend === 'ollama'
  return { seed: verified, streamUsage: verified }
}

/** Canonical UI choices become backend-specific request fields only after capability validation. */
export function wireParameters(parameters: Parameters, backend: string, controls?: ChatControls | null): Record<string, unknown> {
  validateParameters(parameters)
  const result: Record<string, unknown> = {}
  for (const key of ['temperature', 'top_p', 'max_tokens', 'seed'] as const) {
    if (parameters[key] !== undefined && (key !== 'seed' || capabilities(backend).seed)) result[key] = parameters[key]
  }
  const { thinking, reasoning_effort: effort, reasoning_budget_tokens: budget } = parameters
  if (thinking === undefined) {
    if (effort !== undefined || budget !== undefined) throw new Error('请先开启深度思考，再设置推理强度或思考预算')
    // Keep default thinking behavior, while separating any reasoning from the final answer.
    if (backend === 'llama_cpp' && controls?.thinking) result.reasoning_format = 'deepseek'
    return result
  }
  if (!controls?.thinking || !['llama_cpp', 'ollama'].includes(backend)) throw new Error('该模型尚未声明可用的思考控制，请刷新模型列表或使用后端默认')
  if (!thinking && (effort !== undefined || budget !== undefined)) throw new Error('关闭深度思考时不能指定推理强度或思考预算')
  if (effort !== undefined && !controls.reasoning_efforts.includes(effort)) throw new Error('该模型不支持所选推理强度，请重新选择')
  if (budget !== undefined && (!controls.reasoning_budget || backend !== 'llama_cpp')) throw new Error('该模型不支持思考 token 预算')
  if (backend === 'llama_cpp') {
    result.chat_template_kwargs = { enable_thinking: thinking }
    if (thinking) {
      result.reasoning_format = 'deepseek'
      if (effort !== undefined) result.reasoning_effort = effort
      if (budget !== undefined) result.reasoning_budget_tokens = budget
    }
  } else {
    // Ollama's OpenAI adapter maps medium to enabled thinking; omission cannot force thinking on.
    result.reasoning_effort = thinking ? effort ?? 'medium' : 'none'
  }
  return result
}
