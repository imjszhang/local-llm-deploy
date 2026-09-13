<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { consoleContext } from '../../app/context'
import AccessDialog from '../../shared/components/AccessDialog.vue'
import DialogShell from '../../shared/components/DialogShell.vue'
import MessageContent from './components/MessageContent.vue'
import { useChat } from './composables/useChat'
import { exportSession } from './domain/export'
import ParameterPanel from './components/ParameterPanel.vue'
import SessionSidebar from './components/SessionSidebar.vue'
import RequestStats from './components/RequestStats.vue'
import ChatComposer from './components/ChatComposer.vue'
import HistoryStatus from './components/HistoryStatus.vue'
import './chat.css'
const context = inject(consoleContext)
if (!context) throw new Error('Console context is required')
const { monitor } = context
const chat = useChat(context)
const { sessions, selectedId, current, busySession, models, error } = chat
const { history } = chat
const { loading: historyLoading, status: historyStatus, message: historyMessage, hasUnsaved, issueSessionId } = history
const editable = computed(() => !historyLoading.value && !!current.value && history.isLoaded(current.value.id))
const narrow = ref(window.innerWidth <= 700), compact = ref(window.innerWidth <= 1100)
function resize() { narrow.value = window.innerWidth <= 700; compact.value = window.innerWidth <= 1100 }
function focusTrigger(event: Event) { (event.currentTarget as HTMLElement)?.focus() }
const accessOpen = ref(false), sidebarOpen = ref(false), settingsOpen = ref(false)
const pendingModel = ref(''), rename = ref(''), renameOpen = ref(false), deleteOpen = ref(false)
const reloadOpen = ref(false)
const list = ref<HTMLElement>(), composer = ref<InstanceType<typeof ChatComposer>>(), atBottom = ref(true), notice = ref('')
const model = computed(() => models.value.find(m => m.key === current.value?.model))
const canSend = computed(() => editable.value && !!current.value?.draft.trim() && !busySession.value && model.value?.routing.available === true)
const lastAnswer = computed(() => { const turn = current.value?.turns.at(-1); return turn?.answers[turn.selected] })
const runningTitle = computed(() => sessions.value.find(s => s.id === busySession.value)?.title)
const uncertain = computed(() => models.value.some(item => item.activity.uncertain))
const statusNames = { waiting: '等待首内容 · 可能正在排队或加载', streaming: '正在生成', complete: '已完成', stopped: '已停止接收 · 后端可能仍在收尾', error: '生成失败' }
function selectModel(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  if (current.value?.turns.length && value !== current.value.model) pendingModel.value = value
  else chat.switchModel(value, false)
  ;(event.target as HTMLSelectElement).value = current.value?.model ?? ''
}
function changeModel(carry: boolean) { chat.switchModel(pendingModel.value, carry); pendingModel.value = '' }
async function bottom() { await nextTick(); if (list.value) list.value.scrollTop = list.value.scrollHeight; atBottom.value = true }
function scroll() { const el = list.value; if (el) atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }
watch(() => [lastAnswer.value?.content, lastAnswer.value?.reasoning, current.value?.turns.length], () => { if (atBottom.value) void bottom() })
watch(selectedId, () => { sidebarOpen.value = false; void bottom() })
async function copy(text: string) { try { await navigator.clipboard.writeText(text); notice.value = '已复制' } catch { notice.value = '复制失败，请手动选择文本' } }
function hashModel() {
  if (!history.ready.value) return
  if (!location.hash.startsWith('#/chat?')) return
  const key = new URLSearchParams(location.hash.split('?')[1]).get('model')
  if (key && current.value?.model !== key) chat.create(key)
}
watch(history.ready, ready => { if (ready) hashModel() })
function beforeUnload(event: BeforeUnloadEvent) { if (busySession.value || hasUnsaved.value) event.preventDefault() }
onMounted(() => { hashModel(); window.addEventListener('resize', resize); window.addEventListener('hashchange', hashModel); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { window.removeEventListener('resize', resize); window.removeEventListener('hashchange', hashModel); window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <section class="chat-workspace" aria-label="模型对话测试台">
    <header class="chat-topbar">
      <div><p class="eyebrow">MODEL PLAYGROUND</p><h1>模型对话</h1><p>在本地验证回答，观察每次生成。</p></div>
      <div class="chat-actions">
        <button class="button button--quiet chat-mobile" @click="focusTrigger($event); sidebarOpen = !sidebarOpen">会话</button>
        <button class="button button--quiet" :disabled="!editable" @click="focusTrigger($event); settingsOpen = !settingsOpen">测试设置</button>
        <button class="button button--quiet" @click="focusTrigger($event); accessOpen = true">{{ context.credentialSource?.value === 'local' ? '本机已连接' : context.localConnecting?.value ? '正在连接本机…' : monitor.keySet.value ? '访问设置 · 已设置' : '设置访问凭据' }}</button>
      </div>
    </header>
    <div class="chat-layout" :class="{ 'settings-visible': settingsOpen }">
      <SessionSidebar v-if="!narrow" :sessions="sessions" :selected-id="selectedId" :busy-session="busySession" :disabled="historyLoading" :is-loaded="history.isLoaded" @create="chat.create()" @select="selectedId = $event" />
      <main v-if="editable && current" class="chat-main">
        <HistoryStatus :status="historyStatus" :message="historyMessage" :pending="hasUnsaved" :elsewhere="!!issueSessionId && issueSessionId !== selectedId" @open-issue="selectedId = issueSessionId" @retry="history.retry()" @reload="reloadOpen = true" @copy="history.copyCurrent()" />
        <div class="chat-model-bar">
          <label for="chat-model">对话模型</label>
          <select id="chat-model" :value="current.model" :disabled="!!busySession" @change="selectModel">
            <option value="">选择本地模型</option>
            <option v-if="current.model && !model" :value="current.model" disabled>{{ current.model }} · 当前不可用</option>
            <option v-for="item in models" :key="item.key" :value="item.key" :disabled="item.routing.available !== true">{{ item.alias }} · {{ item.backend }} · {{ item.routing.available !== true ? '不可用' : item.loaded === false ? '未加载' : '可用' }}</option>
          </select>
          <button class="button button--quiet" @click="monitor.refresh()">刷新</button>
        </div>
        <p v-if="model" class="chat-model-note">{{ model.key }} <span v-if="model.loaded === false">· 首次请求可能需要加载模型</span><span v-if="model.routing.available !== true"> · {{ model.routing.reason || '暂不可路由' }}</span></p>
        <p v-if="!models.length" class="chat-banner">{{ monitor.access.value === 'required' ? '设置有效访问凭据后获取模型列表。' : '暂无可用的 Chat 模型；请检查模型注册与网关连接。' }}</p>
        <p v-if="uncertain" class="chat-banner">网关有结束状态待确认的请求，可能继续占用对话通道。请在运行监控中检查；确认后端空闲后由管理员恢复网关。</p>
        <div class="chat-session-toolbar">
          <strong>{{ current.title }}</strong>
          <button @click="rename = current.title; renameOpen = true">重命名</button>
          <button @click="deleteOpen = true">删除</button>
          <button @click="exportSession(current, 'markdown')">导出 Markdown</button>
          <button @click="exportSession(current, 'json')">导出 JSON</button>
        </div>
        <div ref="list" class="chat-messages" role="log" aria-label="对话消息" aria-live="off" @scroll="scroll">
          <div v-if="!current.turns.length" class="chat-empty">
            <div class="chat-empty-icon">↗</div><h2>从一个问题开始测试</h2><p>选择模型，发送提示词。每轮回答保留模型与参数快照。</p>
            <div class="chat-examples"><button v-for="example in ['用 Python 编写一个带边界检查的二分查找。', '用三句话解释大语言模型的上下文窗口。', '只返回 JSON，包含 name、purpose 两个字段，介绍你自己。']" :key="example" @click="current.draft = example; composer?.focus()">{{ example }}</button></div>
          </div>
          <article v-for="(turn, index) in current.turns" :key="turn.id" class="chat-turn">
            <div class="chat-user"><div class="chat-message-heading"><strong>你</strong><button @click="copy(turn.user)">复制</button><button v-if="index === current.turns.length - 1" :disabled="!!busySession" @click="chat.editLast(); composer?.focus()">编辑并重新发送</button></div><p>{{ turn.user }}</p></div>
            <template v-for="answer in [turn.answers[turn.selected]]" :key="answer?.id">
              <div v-if="answer" class="chat-assistant">
                <div class="chat-message-heading"><strong>{{ answer.request.model }}</strong><span :class="{ 'chat-active': answer.status === 'streaming' || answer.status === 'waiting' }">{{ statusNames[answer.status] }}</span></div>
                <details v-if="answer.reasoning" class="chat-reasoning"><summary>推理内容</summary><pre>{{ answer.reasoning }}</pre></details>
                <MessageContent v-if="answer.content" :content="answer.content" />
                <p v-else-if="answer.status === 'waiting'" class="chat-waiting">等待模型返回内容<span>…</span></p>
                <p v-if="answer.error" class="chat-error" role="alert">{{ answer.error }}</p>
                <RequestStats :answer="answer" />
                <div class="chat-message-actions">
                  <button :disabled="!answer.content" @click="copy(answer.content)">复制回答</button>
                  <button v-if="index === current.turns.length - 1" :disabled="!!busySession" @click="chat.regenerate()">{{ answer.status === 'error' || answer.status === 'stopped' ? '重试' : '重新生成' }}</button>
                  <label v-if="turn.answers.length > 1">答案版本 <select v-model="turn.selected" :disabled="!!busySession || index !== current.turns.length - 1"><option v-for="(_, version) in turn.answers" :key="version" :value="version">{{ version + 1 }} / {{ turn.answers.length }}</option></select></label>
                  <label v-if="['stopped', 'error'].includes(answer.status) && answer.content"><input v-model="answer.adopted" type="checkbox" :disabled="!!busySession" />将部分回答加入上下文</label>
                </div>
                <details class="chat-request"><summary>请求快照</summary><p>后端：{{ answer.request.backend }} · 包含 {{ answer.request.messages.length }} 条上下文消息</p><pre>{{ JSON.stringify(answer.request.parameters, null, 2) }}</pre><p>未设置参数使用后端默认值。首内容延迟包含排队与模型处理。</p></details>
              </div>
            </template>
          </article>
        </div>
        <button v-if="!atBottom" class="chat-latest button" @click="bottom">↓ 回到最新</button>
        <ChatComposer ref="composer" v-model="current.draft" :busy-session="busySession" :selected-id="selectedId" :running-title="runningTitle" :error="error" :notice="notice" :can-send="canSend" @send="canSend && chat.send()" @stop="chat.stop()" @select-running="selectedId = busySession!" />
      </main>
      <main v-else class="chat-main chat-history-loading"><HistoryStatus :status="historyStatus" :message="historyMessage" :pending="hasUnsaved" :elsewhere="!!issueSessionId && issueSessionId !== selectedId" @open-issue="selectedId = issueSessionId" @retry="history.retry()" @reload="reloadOpen = true" @copy="history.copyCurrent()" /><p>{{ historyLoading ? '正在读取会话记录…' : '会话尚未载入，请重试读取或选择其他会话。' }}</p></main>
      <ParameterPanel v-if="settingsOpen && editable && current && !compact" :model-value="current" :backend="model?.backend" :controls="model?.chat_controls" @close="settingsOpen = false" />
    </div>
    <DialogShell v-if="settingsOpen && editable && current && compact" label-id="chat-settings-title" kind="drawer" @close="settingsOpen = false"><div class="chat-workspace chat-dialog-content"><h2 id="chat-settings-title">对话参数</h2><ParameterPanel :model-value="current" :backend="model?.backend" :controls="model?.chat_controls" @close="settingsOpen = false" /></div></DialogShell>
    <DialogShell v-if="sidebarOpen && narrow" label-id="chat-sessions-title" kind="drawer" @close="sidebarOpen = false"><div class="chat-workspace chat-dialog-content"><div class="chat-message-heading"><h2 id="chat-sessions-title">会话列表</h2><button @click="sidebarOpen = false">关闭</button></div><SessionSidebar :sessions="sessions" :selected-id="selectedId" :busy-session="busySession" :disabled="historyLoading" :is-loaded="history.isLoaded" @create="chat.create(); sidebarOpen = false" @select="selectedId = $event; sidebarOpen = false" /></div></DialogShell>
    <AccessDialog v-if="accessOpen" :has-credential="monitor.keySet.value" @close="accessOpen = false" @apply="monitor.applyKey" @clear="monitor.clearKey" />
    <DialogShell v-if="pendingModel" label-id="switch-model-title" @close="pendingModel = ''"><h2 id="switch-model-title">切换对话模型</h2><p>选择是否将当前有效上下文交给新模型。</p><div class="dialog-actions"><button class="button" @click="pendingModel = ''">取消</button><button class="button" @click="changeModel(true)">携带上下文切换</button><button class="button button--primary" @click="changeModel(false)">新建会话</button></div></DialogShell>
    <DialogShell v-if="renameOpen" label-id="rename-title" @close="renameOpen = false"><h2 id="rename-title">重命名会话</h2><form @submit.prevent="current!.title = rename.trim() || '新对话'; renameOpen = false"><input v-model="rename" aria-label="会话名称" maxlength="100" /><button class="button button--primary" type="submit">保存</button></form></DialogShell>
    <DialogShell v-if="deleteOpen" label-id="delete-title" @close="deleteOpen = false"><h2 id="delete-title">删除当前会话？</h2><p>该会话将从本机历史记录中删除。{{ busySession === selectedId ? '同时停止接收当前生成，后端可能仍在收尾。' : '' }}</p><div class="dialog-actions"><button class="button" @click="deleteOpen = false">取消</button><button class="button button--primary" @click="deleteOpen = false; chat.remove(selectedId)">删除会话</button></div></DialogShell>
    <DialogShell v-if="reloadOpen" label-id="reload-chat-title" @close="reloadOpen = false"><h2 id="reload-chat-title">载入已保存版本？</h2><p>当前页面未保存的修改将被放弃。需要保留时，可以取消并选择“保存为新会话”。</p><div class="dialog-actions"><button class="button" @click="reloadOpen = false">取消</button><button class="button button--primary" @click="reloadOpen = false; history.reload()">确认载入</button></div></DialogShell>
  </section>
</template>
