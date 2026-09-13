<script setup lang="ts">
import { defineAsyncComponent, onBeforeUnmount, onMounted, provide, ref } from 'vue'
import MonitorWorkspace from './features/monitor/MonitorWorkspace.vue'
import { useMonitor } from './features/monitor/composables/useMonitor'
import { consoleContext } from './app/context'
import { requestLocalSession } from './shared/auth/localSession'
const ChatWorkspace = defineAsyncComponent(() => import('./features/chat/ChatWorkspace.vue'))
const credential = ref(''), credentialGeneration = ref(0)
const credentialSource = ref<'none' | 'local' | 'manual'>('none'), localConnecting = ref(false)
let applyingLocal = false, disposed = false, localRequest: AbortController | null = null
const monitor = useMonitor({ onCredentialChange(token) {
  credential.value = token; credentialSource.value = token ? applyingLocal ? 'local' : 'manual' : 'none'; credentialGeneration.value++
} })
async function connectLocal() {
  if (localConnecting.value) return
  const generation = credentialGeneration.value, controller = new AbortController()
  localRequest = controller; localConnecting.value = true
  const timeout = setTimeout(() => controller.abort(), 5000)
  try {
    const session = await requestLocalSession(location.hostname, controller.signal)
    if (session && !disposed && !controller.signal.aborted && generation === credentialGeneration.value) {
      applyingLocal = true
      try { monitor.applyKey(session.token) } finally { applyingLocal = false }
    }
  } finally { clearTimeout(timeout); localRequest = null; localConnecting.value = false }
}
provide(consoleContext, { monitor, credential, credentialGeneration, credentialSource, localConnecting, connectLocal })
const chat = ref(location.hash.startsWith('#/chat'))
const visitedChat = ref(chat.value)
function navigate() { chat.value = location.hash.startsWith('#/chat'); visitedChat.value ||= chat.value }
onMounted(async () => { window.addEventListener('hashchange', navigate); await connectLocal(); if (!disposed) monitor.start() })
onBeforeUnmount(() => { disposed = true; localRequest?.abort(); window.removeEventListener('hashchange', navigate); monitor.stop() })
</script>
<template>
  <nav class="workspace-nav" :class="{ 'workspace-nav--chat': chat }" aria-label="工作区">
    <a href="#/monitor" :aria-current="!chat ? 'page' : undefined">运行监控</a>
    <a href="#/chat" :aria-current="chat ? 'page' : undefined">模型对话</a>
    <span>LOCAL LLM · 本地工作台</span>
  </nav>
  <div v-show="!chat"><MonitorWorkspace /></div>
  <ChatWorkspace v-if="visitedChat" v-show="chat" />
</template>
<style>
.workspace-nav{display:flex;align-items:center;gap:8px;padding:10px 24px;border-bottom:1px solid #29343d;background:#10171c;color:#a9bac5}
.workspace-nav a{padding:8px 16px;border-radius:7px;color:inherit;text-decoration:none;font-size:14px}
.workspace-nav a[aria-current=page]{background:#253847;color:#e2efff}
.workspace-nav span{margin-left:auto;font-size:11px;letter-spacing:.12em}
.workspace-nav--chat{background:#f7f8fc;border-color:#e4e7ef;color:#626b80}
.workspace-nav--chat a[aria-current=page]{background:#eaeafd;color:#4338ca;font-weight:600}
.workspace-nav--chat a:focus-visible{outline:2px solid #6366f1;outline-offset:2px}
@media(max-width:600px){.workspace-nav{padding:8px}.workspace-nav span{display:none}}
</style>
