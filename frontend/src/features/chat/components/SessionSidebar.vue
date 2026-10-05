<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Session } from '../domain/types'
const props = defineProps<{ sessions: Session[]; selectedId: string; busySession: string | null; disabled?: boolean; isLoaded?: (id: string) => boolean }>()
const search = ref('')
const filteredSessions = computed(() => props.sessions.filter(session => session.title.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase())))
const emit = defineEmits<{ create: []; select: [id: string]; remove: [id: string] }>()
</script>
<template>
  <aside class="chat-sessions" aria-label="会话列表">
    <button class="button button--primary" type="button" @click="emit('create')">＋ 新建对话</button>
    <input v-model="search" class="chat-session-search" aria-label="搜索对话" placeholder="搜索对话" type="search" /><p class="chat-sidebar-label">最近对话</p>
    <div class="chat-session-list">
      <p v-if="!filteredSessions.length" class="chat-search-empty">没有匹配的对话</p>
      <div v-for="session in filteredSessions" :key="session.id" class="chat-session" :aria-current="selectedId === session.id ? 'true' : undefined">
        <button type="button" class="chat-session-open" @click="emit('select', session.id)">
          <strong>{{ session.title }}</strong>
          <span>{{ busySession === session.id ? '● 生成中' : isLoaded && !isLoaded(session.id) ? '已保存的对话' : `${session.turns.length} 轮对话` }}</span>
        </button>
        <button type="button" class="chat-session-delete" :aria-label="`删除「${session.title || '未命名会话'}」`" :disabled="disabled" @click="emit('remove', session.id)">删除</button>
      </div>
    </div>
    <p class="chat-memory-note">本地模型 · 对话记录保存在此服务器</p>
  </aside>
</template>
