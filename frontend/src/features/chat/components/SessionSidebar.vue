<script setup lang="ts">
import type { Session } from '../domain/types'
defineProps<{ sessions: Session[]; selectedId: string; busySession: string | null; disabled?: boolean; isLoaded?: (id: string) => boolean }>()
const emit = defineEmits<{ create: []; select: [id: string]; remove: [id: string] }>()
</script>
<template>
  <aside class="chat-sessions" aria-label="会话列表">
    <button class="button button--primary" :disabled="disabled" @click="emit('create')">＋ 新建对话</button>
    <div class="chat-session-list">
      <div v-for="session in sessions" :key="session.id" class="chat-session" :aria-current="selectedId === session.id ? 'true' : undefined">
        <button type="button" class="chat-session-open" @click="emit('select', session.id)">
          <strong>{{ session.title }}</strong>
          <span>{{ busySession === session.id ? '● 生成中' : isLoaded && !isLoaded(session.id) ? '已保存的对话' : `${session.turns.length} 轮对话` }}</span>
        </button>
        <button type="button" class="chat-session-delete" :aria-label="`删除「${session.title || '未命名会话'}」`" :disabled="disabled" @click="emit('remove', session.id)">删除</button>
      </div>
    </div>
    <p class="chat-memory-note">对话自动保存在本机。每条会话可单独删除；确认显示“已自动保存”后，刷新或重新打开页面可继续。</p>
  </aside>
</template>
