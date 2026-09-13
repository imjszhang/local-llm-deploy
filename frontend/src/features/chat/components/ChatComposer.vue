<script setup lang="ts">
import { ref } from 'vue'
defineProps<{ busySession: string | null; selectedId: string; runningTitle?: string; error: string; notice: string; canSend: boolean }>()
const draft = defineModel<string>({ required: true })
const emit = defineEmits<{ send: []; stop: []; selectRunning: [] }>()
const composer = ref<HTMLTextAreaElement>()
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); emit('send') }
}
defineExpose({ focus: () => composer.value?.focus() })
</script>
<template>
        <div class="chat-composer">
          <p v-if="busySession && busySession !== selectedId" class="chat-banner">“{{ runningTitle }}”正在生成。<button @click="emit('selectRunning')">查看</button><button @click="emit('stop')">停止接收</button></p>
          <p v-if="error" class="chat-error" role="alert">{{ error }}</p>
          <textarea ref="composer" v-model="draft" aria-label="消息输入" placeholder="输入测试提示词…" rows="3" @keydown="keydown" />
          <div class="chat-compose-footer"><small>Enter 发送 · Shift+Enter 换行</small><span role="status">{{ notice }}</span><button v-if="busySession === selectedId" class="button button--primary" @click="emit('stop')">停止接收</button><button v-else class="button button--primary" :disabled="!canSend" @click="emit('send')">发送 ↑</button></div>
        </div>
</template>
