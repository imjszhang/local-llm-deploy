<script setup lang="ts">
import { computed } from 'vue'
import type { Answer } from '../domain/types'
const { answer } = defineProps<{ answer: Answer }>()
const rate = computed(() => answer.status === 'complete' && answer.usage?.completion_tokens !== undefined && answer.durationMs && answer.durationMs > 0 ? answer.usage.completion_tokens / (answer.durationMs / 1000) : null)
</script>
<template>
                <div class="chat-stats">
                  <span>首内容 {{ answer.firstContentMs === undefined ? '—' : `${(answer.firstContentMs / 1000).toFixed(2)}s` }}</span>
                  <span>总耗时 {{ answer.durationMs === undefined ? '—' : `${(answer.durationMs / 1000).toFixed(2)}s` }}</span>
                  <span>输入 {{ answer.usage?.prompt_tokens ?? '未提供' }} / 输出 {{ answer.usage?.completion_tokens ?? '未提供' }} token</span>
                  <span v-if="answer.finishReason">结束：{{ answer.finishReason === 'length' ? '达到输出限制' : answer.finishReason }}</span>
                <span v-if="rate !== null" title="后端输出 token ÷ 完整请求耗时，包含排队、加载与网络；不是模型解码速度">端到端 {{ rate.toFixed(1) }} token/s</span></div>
</template>
