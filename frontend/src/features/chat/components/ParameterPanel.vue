<script setup lang="ts">
import { computed } from 'vue'
import type { Parameters, Session } from '../domain/types'
import { capabilities } from '../domain/parameters'
import type { ChatControls } from '../../monitor/api/types'
const props = defineProps<{ backend?: string; controls?: ChatControls | null }>()
const session = defineModel<Session>({ required: true })
const emit = defineEmits<{ close: [] }>()
type NumericParameter = keyof Pick<Parameters, 'temperature' | 'top_p' | 'max_tokens' | 'seed' | 'reasoning_budget_tokens'>
const thinkingMode = computed(() => session.value.parameters.thinking === undefined ? 'default' : session.value.parameters.thinking ? 'on' : 'off')
const hasThinkingOverrides = computed(() => ['thinking', 'reasoning_effort', 'reasoning_budget_tokens'].some(key => key in session.value.parameters))
const effortLabels: Record<string, string> = { low: '低', medium: '中', high: '高', xhigh: '极高', max: '最高' }
const thinkingDefault = computed(() => {
  if (props.controls?.default_thinking === true) return '后端默认开启'
  if (props.controls?.default_thinking === false) return '后端默认关闭'
  return '跟随后端默认设置'
})
function thinking(event: Event) {
  const mode = (event.target as HTMLSelectElement).value
  if (mode === 'default') delete session.value.parameters.thinking
  else session.value.parameters.thinking = mode === 'on'
  if (mode !== 'on') {
    delete session.value.parameters.reasoning_effort
    delete session.value.parameters.reasoning_budget_tokens
  }
}
function effort(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  if (value && props.controls?.reasoning_efforts.includes(value)) session.value.parameters.reasoning_effort = value
  else delete session.value.parameters.reasoning_effort
}
function resetThinking() {
  delete session.value.parameters.thinking
  delete session.value.parameters.reasoning_effort
  delete session.value.parameters.reasoning_budget_tokens
}
function parameter(key: NumericParameter, event: Event) {
  const value = (event.target as HTMLInputElement).value
  if (value === '') delete session.value.parameters[key]
  else session.value.parameters[key] = Number(value)
}
</script>
<template>
      <aside class="chat-settings" aria-label="测试设置">
        <div class="chat-message-heading"><h2>测试设置</h2><button @click="emit('close')">关闭</button></div>
        <label for="chat-system">System Prompt</label><textarea id="chat-system" v-model="session.system" rows="7" placeholder="设置模型的角色、语气和回答约束" />
        <p>修改从下一次请求生效。历史请求保留原始快照。</p>
        <section v-if="controls?.thinking" class="chat-thinking-settings" aria-label="思考设置">
          <label for="chat-thinking">深度思考</label>
          <select id="chat-thinking" :value="thinkingMode" aria-describedby="chat-thinking-default" @change="thinking">
            <option value="default">默认</option><option value="on">开启</option><option value="off">关闭</option>
          </select>
          <p id="chat-thinking-default">{{ thinkingDefault }}。修改从下一次请求生效。</p>
          <template v-if="session.parameters.thinking === true">
            <template v-if="controls.reasoning_efforts.length">
              <label for="chat-reasoning-effort">推理强度</label>
              <select id="chat-reasoning-effort" :value="session.parameters.reasoning_effort ?? ''" aria-describedby="chat-effort-note" @change="effort">
                <option value="">{{ backend === 'ollama' ? '默认（中）' : '默认' }}</option>
                <option v-for="level in controls.reasoning_efforts" :key="level" :value="level">{{ effortLabels[level] ?? level }}</option>
              </select>
              <p id="chat-effort-note">控制模型思考的详略，不保证固定 token 数或回答质量。<template v-if="backend === 'ollama'">显式开启且未选择强度时，使用中档。</template><template v-else-if="controls.default_effort">后端默认：{{ effortLabels[controls.default_effort] ?? controls.default_effort }}。</template></p>
            </template>
            <p v-else>此模型未声明支持分级推理强度。</p>
            <template v-if="controls.reasoning_budget">
              <label for="chat-reasoning-budget">思考 token 预算</label>
              <input id="chat-reasoning-budget" type="number" min="1" max="131072" step="1" :value="session.parameters.reasoning_budget_tokens ?? ''" placeholder="后端默认" aria-describedby="chat-budget-note" @input="parameter('reasoning_budget_tokens', $event)" />
              <p id="chat-budget-note">可选，范围 1–131072，留空使用后端默认。最大输出 token 包含思考和正文。</p>
            </template>
          </template>
        </section>
        <div v-else class="chat-thinking-unavailable"><p>此模型尚未声明支持思考设置。</p><button v-if="hasThinkingOverrides" class="button button--quiet" @click="resetThinking">重置思考设置</button></div>
        <label v-for="field in [{ key: 'temperature', title: 'Temperature', min: 0, max: 2, step: 0.1 }, { key: 'top_p', title: 'Top P', min: 0.01, max: 1, step: 0.05 }, { key: 'max_tokens', title: '最大输出 token', min: 1, max: 131072, step: 1 }] as const" :key="field.key">{{ field.title }}<input type="number" :value="session.parameters[field.key] ?? ''" :min="field.min" :max="field.max" :step="field.step" placeholder="后端默认" @input="parameter(field.key, $event)" /></label>
        <details v-if="capabilities(backend ?? '').seed"><summary>高级参数</summary><label>Seed<input type="number" min="0" step="1" :value="session.parameters.seed ?? ''" placeholder="不设置" @input="parameter('seed', $event)" /></label><p>仅在后端支持时设置；相同 seed 不保证跨后端结果一致。</p></details>
        <button class="button button--quiet" @click="session.parameters = {}">恢复后端默认参数</button>
        <p>不会自动截断历史。上下文超限时，请减少历史或新建会话。</p>
      </aside>
</template>
