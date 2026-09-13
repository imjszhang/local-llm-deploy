<script setup lang="ts">
defineProps<{ status: string; message: string; pending: boolean; elsewhere?: boolean }>()
defineEmits<{ retry: []; reload: []; copy: []; openIssue: [] }>()
const labels: Record<string, string> = {
  loading: '正在恢复对话…', saved: '已自动保存', saving: '正在保存…', pending: '等待保存…',
  error: '保存失败', conflict: '会话存在其他版本', unavailable: '自动保存不可用', unauthorized: '连接后自动保存',
}
</script>
<template>
  <div class="chat-history-status" :class="{ 'chat-history-status--error': ['error', 'conflict', 'unavailable'].includes(status) }" aria-label="会话保存状态">
    <span role="status">{{ labels[status] ?? status }}<span v-if="pending && ['error', 'conflict', 'unavailable', 'unauthorized'].includes(status)"> · 修改尚未保存</span></span>
    <p v-if="message">{{ message }}</p>
    <div v-if="elsewhere" class="chat-history-actions"><button @click="$emit('openIssue')">查看需要处理的会话</button></div>
    <div v-else-if="['error', 'unavailable'].includes(status)" class="chat-history-actions"><button @click="$emit('retry')">重试保存</button></div>
    <div v-else-if="status === 'conflict'" class="chat-history-actions"><button @click="$emit('copy')">保存为新会话</button><button @click="$emit('reload')">载入已保存版本</button></div>
  </div>
</template>
