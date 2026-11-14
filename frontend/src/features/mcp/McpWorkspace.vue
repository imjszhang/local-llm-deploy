<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { consoleContext } from '../../app/context'
import { createHubClient, HubApiError } from './api/client'
import type { HubConfig, HubDevice, HubOperation, HubService, HubSnapshot, HubTool } from './api/types'
import './mcp.css'

const shared = inject(consoleContext, null)
const api = createHubClient()
const snapshot = ref<HubSnapshot | null>(null)
const credential = ref(''), keyDraft = ref(''), loading = ref(false), busy = ref(false)
const error = ref(''), notice = ref(''), lastUpdated = ref('')
const tab = ref<'services' | 'devices' | 'tasks' | 'logs' | 'settings'>('services')
const operation = ref<HubOperation | null>(null), logs = ref(''), logsLoaded = ref(false)
const serviceDraft = ref<HubService | null>(null), editorBase = ref<HubConfig | null>(null), originalServiceId = ref('')
const argsText = ref(''), toolsText = ref(''), envRows = ref<Array<{ key: string; value: string }>>([])
const settingsDraft = ref<HubConfig | null>(null), originsText = ref('')
const deviceName = ref(''), deviceServices = ref<string[]>([]), canGenerate = ref(true)
const issued = ref<{ device: HubDevice; token: string } | null>(null)
const connectionService = ref(''), removeServiceId = ref(''), revokeDeviceId = ref('')
const inspection = ref<{ id: string; title: string; tools?: HubTool[]; result?: string; summary?: string } | null>(null)
const controllers = new Set<AbortController>()
let refreshTimer: ReturnType<typeof setInterval> | undefined, operationTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false, authGeneration = 0
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const running = computed(() => ['ready', 'running', 'unhealthy', 'starting'].includes(snapshot.value?.runtime.status || ''))
const activeDevices = computed(() => snapshot.value?.devices.filter(d => !d.revoked && !d.revoked_at) || [])
const services = computed(() => snapshot.value?.config.services || [])
const activeServices = computed(() => services.value.filter(service => service.enabled))
const pending = computed(() => operation.value?.status === 'running')
const unapplied = computed(() => typeof snapshot.value?.runtime.applied_revision === 'number' && snapshot.value.runtime.applied_revision !== snapshot.value.config.revision)
const connection = computed(() => snapshot.value?.connections.find(item => item.service_id === connectionService.value))
const clientConfig = computed(() => {
  if (!connection.value) return ''
  const id = connection.value.service_id
  const token = issued.value && issued.value.device.services.includes(id) ? issued.value.token : '<设备令牌>'
  return JSON.stringify({ mcpServers: { [id]: { type: 'http', url: connection.value.url, headers: { Authorization: `Bearer ${token}` } } } }, null, 2)
})
const statusLabels: Record<string, string> = { stopped: '已停止', starting: '启动中', ready: '运行中', running: '运行中', stopping: '停止中', stale: '进程记录失效', unhealthy: '健康检查异常', failed: '失败', error: '异常', queued: '排队中', pending: '等待中', submitted: '已提交', completed: '已完成', complete: '已完成', succeeded: '已完成', cancelled: '已取消', disabled: '未启用', unavailable: '不可用', unknown: '待检查', uncertain: '待确认' }
const label = (status?: string) => statusLabels[status || 'unknown'] || status || '待检查'
function timestamp(value?: number | string | null) {
  if (!value) return '—'
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}
function serviceStatus(id: string) {
  const found = snapshot.value?.runtime.services?.find(row => (row.id || row.service_id) === id)
  return found || null
}
function showError(reason: unknown) {
  if (disposed || (reason instanceof DOMException && reason.name === 'AbortError')) return
  error.value = reason instanceof Error ? reason.message : '操作失败，请稍后重试。'
  if (reason instanceof HubApiError && [401, 403].includes(reason.status)) {
    credential.value = ''; snapshot.value = null; issued.value = null
  }
}
async function request<T>(work: (signal: AbortSignal, key: string) => Promise<T>): Promise<T> {
  const controller = new AbortController(), key = credential.value, generation = authGeneration
  controllers.add(controller)
  const timeout = setTimeout(() => controller.abort(), 20000)
  try {
    const value = await work(controller.signal, key)
    if (disposed || generation !== authGeneration) throw new DOMException('请求已取消', 'AbortError')
    return value
  } catch (reason) {
    if (controller.signal.aborted && !disposed && generation === authGeneration) throw new HubApiError('请求超时；操作可能仍在进行，请刷新状态。', 0)
    throw reason
  } finally { clearTimeout(timeout); controllers.delete(controller) }
}
async function refresh(quiet = false) {
  if (!credential.value || loading.value || disposed) return
  loading.value = true
  if (!quiet) error.value = ''
  try {
    const value = await request((signal, key) => api.snapshot(key, signal))
    snapshot.value = value
    if (!value.connections.some(item => item.service_id === connectionService.value)) connectionService.value = value.connections[0]?.service_id || ''
    lastUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (reason) { showError(reason) }
  finally { loading.value = false }
}
async function signIn(key = keyDraft.value) {
  if (loading.value || !key.trim()) return
  if (key.trim().startsWith('local-console.')) { error.value = '请输入管理员 API Key；本机浏览会话没有管理权限。'; return }
  authGeneration++; credential.value = key.trim(); keyDraft.value = ''; error.value = ''
  await refresh()
}
function signOut() {
  authGeneration++; controllers.forEach(controller => controller.abort()); clearTimeout(operationTimer)
  credential.value = ''; snapshot.value = null; issued.value = null; serviceDraft.value = null
  editorBase.value = null; settingsDraft.value = null; envRows.value = []; logs.value = ''; inspection.value = null
  operation.value = null; notice.value = ''; error.value = ''
}
async function mutate(work: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try { await work() } catch (reason) { showError(reason) } finally { busy.value = false }
}
async function pollOperation(id: string) {
  try {
    const result = await request((signal, key) => api.operation(id, key, signal))
    operation.value = result.operation
    if (operation.value.status === 'running') operationTimer = setTimeout(() => void pollOperation(id), 1200)
    else {
      if (operation.value.status === 'failed') error.value = operation.value.error || '运行操作失败，请查看日志。'
      else notice.value = '运行操作已完成。'
      await refresh(true)
    }
  } catch (reason) { showError(reason); operation.value = null }
}
async function action(name: 'start' | 'stop' | 'restart') {
  if (pending.value) return
  await mutate(async () => {
    const result = await request((signal, key) => api.action(name, key, signal))
    operation.value = result.operation
    if (result.operation.status === 'running') operationTimer = setTimeout(() => void pollOperation(result.operation.id), 800)
    await refresh(true)
  })
}
function openService(service?: HubService) {
  if (!snapshot.value) return
  editorBase.value = clone(snapshot.value.config)
  originalServiceId.value = service?.id || ''
  serviceDraft.value = service ? clone(service) : { id: '', name: '', kind: 'comfyui', enabled: true, command: '', args: [], env: {}, url: '', allowed_tools: [] }
  argsText.value = (service?.args || []).join('\n'); toolsText.value = (service?.allowed_tools || []).join('\n')
  envRows.value = Object.entries(service?.env || {}).map(([key, value]) => ({ key, value }))
}
function closeEditor() { serviceDraft.value = null; editorBase.value = null; envRows.value = []; argsText.value = ''; toolsText.value = '' }
async function persist(config: HubConfig) {
  await request((signal, key) => api.save(config, config.revision, key, signal))
  notice.value = running.value ? '配置已保存。重启 Hub 后应用到运行服务。' : '配置已保存，将在下次启动时应用。'
  await refresh(true)
}
async function saveService() {
  if (!serviceDraft.value || !editorBase.value) return
  await mutate(async () => {
    const service = clone(serviceDraft.value!), config = clone(editorBase.value!)
    service.id = service.id.trim(); service.name = service.name.trim()
    if (!(/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(service.id) && service.id.length <= 64) || !service.name) throw new Error('请填写服务名称和有效标识（小写字母开头，可含数字和短横线）。')
    if (!originalServiceId.value && config.services.some(item => item.id === service.id)) throw new Error('这个服务标识已经存在。')
    service.args = argsText.value.split('\n').map(item => item.trim()).filter(Boolean)
    service.allowed_tools = toolsText.value.split('\n').map(item => item.trim()).filter(Boolean)
    service.env = {}
    for (const row of envRows.value) {
      if (!row.key.trim()) continue
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(row.key.trim())) throw new Error('环境变量名称格式无效。')
      if (Object.hasOwn(service.env, row.key.trim())) throw new Error('环境变量名称不能重复。')
      service.env[row.key.trim()] = row.value
    }
    if (service.kind === 'http') { service.command = ''; service.args = []; service.env = {} }
    const index = config.services.findIndex(item => item.id === originalServiceId.value)
    if (index < 0) config.services.push(service); else config.services[index] = service
    await persist(config); closeEditor()
  })
}
async function toggleService(service: HubService) {
  if (!snapshot.value) return
  await mutate(async () => {
    const config = clone(snapshot.value!.config)
    const target = config.services.find(item => item.id === service.id)
    if (target) target.enabled = !target.enabled
    await persist(config)
  })
}
async function removeService() {
  if (!snapshot.value || !removeServiceId.value) return
  await mutate(async () => {
    const config = clone(snapshot.value!.config)
    config.services = config.services.filter(service => service.id !== removeServiceId.value)
    await persist(config); removeServiceId.value = ''
  })
}
async function inspectService(id: string, kind: 'tools' | 'probe') {
  await mutate(async () => {
    if (kind === 'tools') {
      const result = await request((signal, key) => api.tools(id, key, signal))
      inspection.value = { id, title: '工具目录', tools: result.tools }
    } else {
      const result = await request((signal, key) => api.probe(id, key, signal))
      const remote = result.remote as { online?: boolean; version?: string } | undefined
      inspection.value = { id, title: '连接检查', result: JSON.stringify(result, null, 2), summary: remote ? `桥接${label(String(result.bridge || 'unknown'))} · 远端 ComfyUI ${remote.online ? '在线' : '不可达'}${remote.version ? ' · v' + remote.version : ''}` : '检查已完成，展开查看详情。' }
      await refresh(true)
    }
  })
}
function selectTab(next: typeof tab.value) {
  tab.value = next
  if (next === 'settings' && snapshot.value) { settingsDraft.value = clone(snapshot.value.config); originsText.value = snapshot.value.config.allowed_origins.join('\n') }
  if (next === 'logs') void loadLogs()
}
async function saveSettings() {
  if (!settingsDraft.value) return
  await mutate(async () => {
    const config = clone(settingsDraft.value!)
    config.allowed_origins = originsText.value.split('\n').map(item => item.trim()).filter(Boolean)
    await persist(config)
    if (snapshot.value) settingsDraft.value = clone(snapshot.value.config)
  })
}
async function issueDevice() {
  if (!deviceName.value.trim() || !deviceServices.value.length) { error.value = '请填写设备名称，并选择至少一个服务。'; return }
  await mutate(async () => {
    const result = await request((signal, key) => api.createDevice(deviceName.value.trim(), deviceServices.value, canGenerate.value ? ['read', 'generate'] : ['read'], key, signal))
    issued.value = result; deviceName.value = ''; connectionService.value = result.device.services[0] || connectionService.value
    notice.value = '设备已创建。请保存下方令牌，关闭后无法再次查看。'
    await refresh(true)
  })
}
async function revokeDevice() {
  if (!revokeDeviceId.value) return
  await mutate(async () => {
    await request((signal, key) => api.revokeDevice(revokeDeviceId.value, key, signal))
    if (issued.value?.device.id === revokeDeviceId.value) issued.value = null
    revokeDeviceId.value = ''; notice.value = '设备令牌已撤销。'; await refresh(true)
  })
}
async function loadLogs() {
  await mutate(async () => { const result = await request((signal, key) => api.logs(key, signal)); logs.value = result.logs; logsLoaded.value = true })
}
async function copy(value: string) {
  try { await navigator.clipboard.writeText(value); notice.value = '已复制。' }
  catch { error.value = '浏览器未允许复制，请选择文本手动复制。' }
}
watch(() => shared?.credential.value, value => {
  if (!credential.value && value && shared?.credentialSource?.value === 'manual' && !value.startsWith('local-console.')) void signIn(value)
}, { immediate: true })
onMounted(() => { refreshTimer = setInterval(() => { if (credential.value && !busy.value && !pending.value) void refresh(true) }, 10000) })
onBeforeUnmount(() => { disposed = true; authGeneration++; controllers.forEach(controller => controller.abort()); clearInterval(refreshTimer); clearTimeout(operationTimer); credential.value = ''; issued.value = null })
</script>

<template>
  <main class="mcp-workspace" aria-label="MCP Hub 管理">
    <header class="mcp-header">
      <div class="mcp-brand"><span class="mcp-mark" aria-hidden="true">⌘</span><div><p class="mcp-eyebrow">本机 · 局域网</p><h1>MCP Hub</h1><p>连接工具服务，让每台设备共用一个入口。</p></div></div>
      <div v-if="snapshot" class="mcp-actions"><span class="mcp-muted">{{ lastUpdated }} 更新</span><button :disabled="loading || busy" @click="refresh()">刷新</button><button @click="signOut">锁定管理</button></div>
    </header>
    <p v-if="error" class="mcp-banner mcp-banner--error" role="alert">{{ error }}</p>
    <p v-if="notice" class="mcp-banner mcp-banner--success" role="status">{{ notice }} <button aria-label="关闭提示" @click="notice = ''">×</button></p>
    <section v-if="!snapshot" class="mcp-access mcp-card">
      <span class="mcp-access-icon" aria-hidden="true">↗</span><h2>连接管理控制台</h2><p>使用网关管理员 API Key 管理 Hub、服务和设备。凭据仅用于当前管理页面。</p>
      <form @submit.prevent="signIn()"><label for="mcp-admin-key">管理员 API Key</label><input id="mcp-admin-key" v-model="keyDraft" type="password" autocomplete="off" placeholder="输入管理员凭据" :disabled="loading" /><button class="mcp-primary" :disabled="loading || !keyDraft.trim()">{{ loading ? '正在连接…' : '连接管理' }}</button></form>
      <p class="mcp-muted">Hub 停止时仍可在这里修改配置和重新启动。本机浏览会话与设备令牌不具备管理权限。</p>
    </section>
    <template v-else>
      <section class="mcp-overview" aria-label="运行概况">
        <div class="mcp-card mcp-runtime"><div class="mcp-runtime-heading"><span class="mcp-status" :class="{ 'mcp-status--online': snapshot.runtime.healthy || snapshot.runtime.available }"><i />{{ label(snapshot.runtime.status) }}</span><span class="mcp-muted">独立服务 · {{ snapshot.runtime.port || snapshot.config.listen.port }}</span></div><h2>{{ pending ? '正在' + ({ start: '启动', stop: '停止', restart: '重启' }[operation!.action] || '执行操作') + '…' : 'Hub 运行管理' }}</h2><p class="mcp-muted">{{ snapshot.runtime.pid ? 'PID ' + snapshot.runtime.pid + ' · ' : '' }}配置 r{{ snapshot.config.revision }}<span v-if="snapshot.runtime.applied_revision !== undefined"> · 运行 r{{ snapshot.runtime.applied_revision }}</span></p><div class="mcp-actions"><button class="mcp-primary" :disabled="busy || pending || running" @click="action('start')">启动 Hub</button><button :disabled="busy || pending || !running" @click="action('restart')">重启</button><button :disabled="busy || pending || !running" @click="action('stop')">停止</button></div><p v-if="unapplied" class="mcp-muted">新配置尚未应用，请重启 Hub。</p><p v-if="snapshot.runtime.error || snapshot.runtime.config_error" class="mcp-error">{{ snapshot.runtime.error || snapshot.runtime.config_error }}</p></div>
        <div class="mcp-card mcp-stat"><span>已启用服务</span><strong>{{ activeServices.length }}<small>/ {{ services.length }}</small></strong><p>ComfyUI 与其他 MCP</p></div>
        <div class="mcp-card mcp-stat"><span>可访问设备</span><strong>{{ activeDevices.length }}</strong><p>独立令牌与服务权限</p></div>
      </section>
      <nav class="mcp-tabs" aria-label="Hub 管理栏目"><button v-for="item in [{ id: 'services', name: '服务' }, { id: 'devices', name: '设备与连接' }, { id: 'tasks', name: '任务' }, { id: 'logs', name: '日志' }, { id: 'settings', name: '设置' }]" :key="item.id" :aria-current="tab === item.id ? 'page' : undefined" @click="selectTab(item.id as typeof tab)">{{ item.name }}</button></nav>
      <section v-if="tab === 'services'" aria-label="MCP 服务">
        <div class="mcp-section-heading"><div><h2>服务目录</h2><p>登记连接，检查工具，再分配给设备。</p></div><button class="mcp-primary" :disabled="busy" @click="openService()">＋ 添加服务</button></div>
        <form v-if="serviceDraft" class="mcp-card mcp-editor" @submit.prevent="saveService">
          <div class="mcp-section-heading"><h2>{{ originalServiceId ? '编辑服务' : '添加服务' }}</h2><button type="button" @click="closeEditor">关闭</button></div>
          <div class="mcp-form-grid"><label>服务名称<input v-model="serviceDraft.name" required maxlength="100" placeholder="局域网 ComfyUI" /></label><label>服务标识<input v-model="serviceDraft.id" required :disabled="!!originalServiceId" pattern="[a-z][a-z0-9]*(-[a-z0-9]+)*" maxlength="64" placeholder="comfyui" /></label><label>连接方式<select v-model="serviceDraft.kind"><option value="comfyui">ComfyUI 桥接</option><option value="stdio">本机 MCP 程序（stdio）</option><option value="http">远程 MCP（HTTP）</option></select></label><label class="mcp-checkbox"><input v-model="serviceDraft.enabled" type="checkbox" />启用这个服务</label><label v-if="serviceDraft.kind !== 'stdio'" class="mcp-span-2">{{ serviceDraft.kind === 'comfyui' ? 'ComfyUI 后端地址' : 'MCP 服务地址' }}<input v-model="serviceDraft.url" type="url" required :placeholder="serviceDraft.kind === 'comfyui' ? 'http://服务器地址:8188' : 'https://服务器地址/mcp'" /></label><label v-if="serviceDraft.kind !== 'http'" class="mcp-span-2">{{ serviceDraft.kind === 'comfyui' ? 'comfy-mcp 可执行文件' : 'MCP 可执行文件' }}<input v-model="serviceDraft.command" required placeholder="/绝对路径/bin/comfy-mcp" /></label></div>
          <details class="mcp-advanced"><summary>高级配置</summary><div class="mcp-form-grid"><label v-if="serviceDraft.kind !== 'http'">启动参数 · 每行一个<textarea v-model="argsText" rows="3" spellcheck="false" /></label><label>开放工具 · 每行一个<textarea v-model="toolsText" rows="3" spellcheck="false" placeholder="留空采用服务默认工具范围" /></label></div><div v-if="serviceDraft.kind !== 'http'" class="mcp-section-heading"><h3>环境变量</h3><button type="button" @click="envRows.push({ key: '', value: '' })">添加变量</button></div><div v-for="(row, index) in (serviceDraft.kind === 'http' ? [] : envRows)" :key="index" class="mcp-env-row"><input v-model="row.key" aria-label="环境变量名称" placeholder="COMFY_BIN" /><input v-model="row.value" aria-label="环境变量值" type="password" autocomplete="off" placeholder="值（默认隐藏）" /><button type="button" aria-label="移除环境变量" @click="envRows.splice(index, 1)">×</button></div></details>
          <div class="mcp-form-footer"><p>保存后重启 Hub 生效。停用连接不会停止远端 ComfyUI。</p><button class="mcp-primary" :disabled="busy">{{ busy ? '正在保存…' : '保存服务' }}</button></div>
        </form>
        <div v-if="!services.length && !serviceDraft" class="mcp-card mcp-empty"><h3>还没有登记服务</h3><p>先添加局域网 ComfyUI，或连接一个已有的 MCP 服务。</p><button @click="openService()">添加第一个服务</button></div>
        <div class="mcp-service-grid"><article v-for="service in services" :key="service.id" class="mcp-card mcp-service"><div class="mcp-service-title"><span class="mcp-service-icon" aria-hidden="true">{{ service.kind === 'comfyui' ? '✳' : service.kind === 'http' ? '↗' : '⌘' }}</span><div><h3>{{ service.name }}</h3><code>{{ service.id }}</code></div><span class="mcp-pill">{{ !service.enabled ? '未启用' : label(serviceStatus(service.id)?.status || (serviceStatus(service.id)?.available ? 'ready' : 'unknown')) }}</span></div><p class="mcp-service-target">{{ service.kind === 'comfyui' ? 'ComfyUI · ' : service.kind === 'http' ? 'HTTP · ' : 'stdio · ' }}{{ service.url || service.command }}</p><p class="mcp-muted">{{ serviceStatus(service.id)?.tool_count !== undefined ? serviceStatus(service.id)!.tool_count + ' 个工具' : '启动后可查看工具目录' }}</p><p v-if="service.kind === 'comfyui'" class="mcp-muted">桥接 · {{ label(serviceStatus(service.id)?.bridge) }} · 远端 · {{ serviceStatus(service.id)?.remote?.online === true ? '在线' : serviceStatus(service.id)?.remote?.online === false ? '不可达' : '未检查' }}<span v-if="serviceStatus(service.id)?.remote?.version"> · v{{ serviceStatus(service.id)?.remote?.version }}</span></p><p v-if="serviceStatus(service.id)?.error" class="mcp-error">{{ serviceStatus(service.id)?.error }}</p><div class="mcp-actions"><button :disabled="busy" @click="openService(service)">编辑</button><button :disabled="busy || !service.enabled || !snapshot.runtime.available" @click="inspectService(service.id, 'probe')">检查连接</button><button :disabled="busy || !service.enabled || !snapshot.runtime.available" @click="inspectService(service.id, 'tools')">工具目录</button><button :disabled="busy" @click="toggleService(service)">{{ service.enabled ? '停用' : '启用' }}</button><button class="mcp-danger-text" :disabled="busy" @click="removeServiceId = service.id">移除</button></div></article></div>
        <div v-if="removeServiceId" class="mcp-card mcp-confirm" role="alert"><p>移除「{{ removeServiceId }}」的服务登记？不会删除远端服务或已有任务。</p><button @click="removeServiceId = ''">保留</button><button class="mcp-danger" :disabled="busy" @click="removeService">确认移除</button></div>
        <section v-if="inspection" class="mcp-card mcp-inspection"><div class="mcp-section-heading"><h3>{{ inspection.id }} · {{ inspection.title }}</h3><button @click="inspection = null">收起</button></div><p v-if="inspection.summary">{{ inspection.summary }}</p><details v-if="inspection.result"><summary>检查详情</summary><pre>{{ inspection.result }}</pre></details><div v-for="tool in inspection.tools" :key="tool.name" class="mcp-tool"><strong>{{ tool.name }}</strong><p>{{ tool.description || '没有描述' }}</p><details v-if="tool.inputSchema"><summary>参数</summary><pre>{{ JSON.stringify(tool.inputSchema, null, 2) }}</pre></details></div><p v-if="inspection.tools?.length === 0" class="mcp-muted">这个服务目前没有开放工具。</p></section>
      </section>
      <section v-if="tab === 'devices'" aria-label="设备与连接">
        <div class="mcp-section-heading"><div><h2>设备与连接</h2><p>为本机项目或局域网设备创建独立访问令牌。</p></div></div>
        <div class="mcp-two-column"><form class="mcp-card mcp-device-form" @submit.prevent="issueDevice"><h3>添加设备 / 项目</h3><label>名称<input v-model="deviceName" required maxlength="100" placeholder="video-remake 或笔记本" /></label><fieldset><legend>允许访问的服务</legend><label v-for="service in services" :key="service.id" class="mcp-checkbox"><input v-model="deviceServices" type="checkbox" :value="service.id" />{{ service.name }}<span v-if="!service.enabled" class="mcp-muted">（未启用）</span></label><p v-if="!services.length" class="mcp-muted">请先登记服务。</p></fieldset><label class="mcp-checkbox"><input v-model="canGenerate" type="checkbox" />允许生成与提交任务</label><p class="mcp-muted">读取权限始终包含；设备令牌不授予管理权限。</p><button class="mcp-primary" :disabled="busy || !services.length || !deviceName.trim() || !deviceServices.length">创建设备令牌</button></form>
        <div class="mcp-card mcp-connection"><h3>连接 MCP 客户端</h3><label>服务<select v-model="connectionService"><option v-for="item in snapshot.connections" :key="item.service_id" :value="item.service_id">{{ services.find(service => service.id === item.service_id)?.name || item.service_id }}</option></select></label><template v-if="connection"><label>连接地址<input :value="connection.url" readonly /></label><pre aria-label="MCP 客户端配置">{{ clientConfig }}</pre><button @click="copy(clientConfig)">复制连接配置</button><p class="mcp-muted">{{ issued ? '当前配置已包含刚创建的设备令牌。' : '将占位符替换为设备令牌。不同客户端的配置字段可能不同。' }}</p></template><p v-else class="mcp-muted">登记服务后可生成连接配置。</p></div></div>
        <section v-if="issued" class="mcp-card mcp-issued" aria-label="新设备令牌"><div class="mcp-section-heading"><div><h3>{{ issued.device.name }} · 请保存访问令牌</h3><p>只在这次创建后显示，关闭后无法找回。</p></div><button @click="issued = null">已保存，关闭</button></div><textarea :value="issued.token" readonly rows="2" aria-label="设备访问令牌" spellcheck="false" /><button @click="copy(issued.token)">复制令牌</button></section>
        <div v-if="revokeDeviceId" class="mcp-card mcp-confirm" role="alert"><p>撤销这个设备的访问令牌？之后的请求将被拒绝。</p><button @click="revokeDeviceId = ''">保留</button><button class="mcp-danger" :disabled="busy" @click="revokeDevice">确认撤销</button></div>
        <div class="mcp-card mcp-table-wrap"><table><thead><tr><th>设备 / 项目</th><th>服务</th><th>权限</th><th>最近访问</th><th>操作</th></tr></thead><tbody><tr v-for="device in snapshot.devices" :key="device.id"><td><strong>{{ device.name }}</strong><small v-if="device.revoked || device.revoked_at">已撤销</small></td><td>{{ device.services.join('、') }}</td><td>{{ device.permissions.includes('generate') ? '读取、生成' : '只读' }}</td><td>{{ timestamp(device.last_seen_at) }}</td><td><button :disabled="busy || !!device.revoked || !!device.revoked_at" @click="revokeDeviceId = device.id">撤销</button></td></tr><tr v-if="!snapshot.devices.length"><td colspan="5" class="mcp-empty-cell">暂无设备令牌</td></tr></tbody></table></div>
      </section>
      <section v-if="tab === 'tasks'" aria-label="任务记录"><div class="mcp-section-heading"><div><h2>最近任务</h2><p>任务提交到远端后可能继续运行。等待超时后请先查询，避免重复提交。</p></div><button :disabled="loading" @click="refresh()">刷新任务</button></div><div class="mcp-card mcp-table-wrap"><table><thead><tr><th>任务</th><th>服务 / 设备</th><th>状态</th><th>提交时间</th><th>结果</th></tr></thead><tbody><tr v-for="task in snapshot.tasks" :key="task.id"><td><strong>{{ task.tool || task.id }}</strong><code>{{ task.prompt_id || task.id }}</code></td><td>{{ task.service || '—' }}<small>{{ snapshot.devices.find(device => device.id === (task.owner || task.device_id))?.name || task.owner || task.device_id || '—' }}</small></td><td><span class="mcp-pill">{{ label(task.status) }}</span></td><td>{{ timestamp(task.created_at) }}</td><td><p v-if="task.error" class="mcp-error">{{ task.error }}</p><details v-else-if="task.result"><summary>查看结果</summary><pre>{{ JSON.stringify(task.result, null, 2) }}</pre></details><span v-else>—</span></td></tr><tr v-if="!snapshot.tasks.length"><td colspan="5" class="mcp-empty-cell">尚无任务。通过 MCP 客户端提交后会显示在这里。</td></tr></tbody></table></div></section>
      <section v-if="tab === 'logs'" aria-label="Hub 日志"><div class="mcp-section-heading"><div><h2>运行日志</h2><p>查看 Hub 最近的启动与连接信息。</p></div><button :disabled="busy" @click="loadLogs">刷新日志</button></div><pre class="mcp-log">{{ logs || (logsLoaded ? '暂无日志' : '正在读取日志…') }}</pre></section>
      <form v-if="tab === 'settings' && settingsDraft" class="mcp-card mcp-settings" @submit.prevent="saveSettings"><div class="mcp-section-heading"><div><h2>Hub 连接设置</h2><p>这些设置保存后在下一次启动时生效。</p></div></div><div class="mcp-form-grid"><label>监听地址<input v-model="settingsDraft.listen.host" required placeholder="0.0.0.0" /><small>0.0.0.0 允许局域网连接；127.0.0.1 仅供本机使用。</small></label><label>端口<input v-model.number="settingsDraft.listen.port" type="number" required min="1" max="65535" /></label><label class="mcp-span-2">对外访问地址（可选）<input v-model="settingsDraft.listen.public_url" type="url" placeholder="http://局域网地址:8899" /><small>留空时使用自动生成的地址；局域网客户端应使用服务器可达的地址。</small></label><label class="mcp-span-2">允许的浏览器来源 · 每行一个<textarea v-model="originsText" rows="3" placeholder="https://可信应用地址" /><small>仅影响浏览器请求；设备仍需提供访问令牌。</small></label></div><div class="mcp-form-footer"><p>管理页面始终通过 8888 访问。</p><button class="mcp-primary" :disabled="busy">保存连接设置</button></div></form>
    </template>
  </main>
</template>
