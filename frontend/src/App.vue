<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useMonitor } from "./composables/useMonitor";
import type { LaneKey } from "./api/types";
import AccessDialog from "./components/AccessDialog.vue";
import Sparkline from "./components/Sparkline.vue";
import StatusBadge from "./components/StatusBadge.vue";
import ModelTable from "./features/ModelTable.vue";
import ModelDrawer from "./features/ModelDrawer.vue";
import {
  cpuUsage,
  isReading,
  number,
  percent,
  sourceStale,
  time,
} from "./components/format";
import "./styles/console.css";

const monitor = useMonitor();
const {
  snapshot,
  detail,
  publicOverview,
  history,
  connection,
  access,
  paused,
  loading,
  lastSuccessAt,
  error,
  keySet,
  selectedKey,
  includeOutput,
} = monitor;
const accessOpen = ref(false);
const diagnosticsExpanded = ref(false);
const now = ref(Date.now());
let clockTimer: ReturnType<typeof setInterval> | undefined;
const isDemo = document.body.dataset.demo === "true";
const system = computed(() =>
  snapshot.value ? snapshot.value.system : publicOverview.value?.system || null,
);
const models = computed(() => snapshot.value?.models || []);
const selectedModel = computed(
  () => models.value.find((model) => model.key === selectedKey.value) || null,
);
const lanes = computed(() =>
  snapshot.value ? snapshot.value.lanes : publicOverview.value?.lanes || {},
);
const cpu = computed(() => cpuUsage(system.value));
const memory = computed(() => system.value?.memory);
const online = computed(() =>
  snapshot.value
    ? models.value.filter((model) => model.availability.state === "healthy")
        .length
    : null,
);
const activities = computed(() =>
  Object.values(lanes.value).reduce(
    (sum, lane) => sum + (lane?.active || 0),
    0,
  ),
);
const waiting = computed(() =>
  Object.values(lanes.value).reduce(
    (sum, lane) => sum + (lane?.waiting || 0),
    0,
  ),
);
const hasLanes = computed(() => Object.keys(lanes.value).length > 0);
const diagnostics = computed(() => snapshot.value?.diagnostics || []);
const shownDiagnostics = computed(() =>
  diagnosticsExpanded.value ? diagnostics.value : diagnostics.value.slice(0, 3),
);
const systemStale = computed(() =>
  sourceStale(snapshot.value?.sources.system, now.value),
);
const catalogStale = computed(
  () =>
    sourceStale(snapshot.value?.sources.catalog, now.value) ||
    sourceStale(snapshot.value?.sources.discovery, now.value),
);
const anyStale = computed(() => systemStale.value || catalogStale.value);
const laneMeta: {
  key: LaneKey;
  label: string;
  english: string;
  symbol: string;
}[] = [
  { key: "chat", label: "对话生成", english: "CHAT", symbol: "C" },
  { key: "embed", label: "文本向量", english: "EMBED", symbol: "E" },
  { key: "rerank", label: "结果重排", english: "RERANK", symbol: "R" },
  { key: "asr", label: "语音转写", english: "ASR", symbol: "S" },
];
const statusLabel = computed(() =>
  paused.value
    ? "刷新已暂停"
    : connection.value === "connecting"
      ? "正在连接网关"
      : connection.value === "disconnected"
        ? "网关连接中断"
        : anyStale.value
          ? "部分数据已过期"
          : "网关已连接",
);
const statusTone = computed<"good" | "warning" | "danger" | "muted">(() =>
  paused.value
    ? "muted"
    : connection.value === "disconnected"
      ? "danger"
      : anyStale.value
        ? "warning"
        : connection.value === "connected"
          ? "good"
          : "muted",
);
function refresh() {
  void monitor.refresh();
}
function applyKey(key: string) {
  void monitor.applyKey(key);
}
function clearKey() {
  void monitor.clearKey();
}
function selectModel(key: string | null, event?: Event) {
  (event?.currentTarget as HTMLElement | null)?.focus();
  void monitor.selectModel(key);
}
function openAccess(event?: Event) {
  (event?.currentTarget as HTMLElement | null)?.focus();
  accessOpen.value = true;
}
onMounted(() => {
  void monitor.start();
  clockTimer = setInterval(() => {
    now.value = Date.now();
  }, 1000);
});
onBeforeUnmount(() => {
  clearInterval(clockTimer);
  monitor.stop();
});
</script>

<template>
  <a class="skip-link" href="#main-content">跳转到主要内容</a>
  <div class="console-shell">
    <header class="topbar">
      <a
        class="brand"
        href="/monitor.html"
        aria-label="Local LLM 模型控制台首页"
        ><span class="brand-mark" aria-hidden="true"><i /><i /><i /><i /></span
        ><span class="brand-name"
          >LOCAL<span class="brand-divider">/</span>LLM<span
            class="brand-subtitle"
            >模型控制台</span
          ></span
        ></a
      >
      <nav class="topbar-actions" aria-label="控制台导航">
        <a class="knowledge-link" href="/knowledge/"
          >知识库 <span aria-hidden="true">↗</span></a
        ><span class="nav-divider" /><button
          class="button button--quiet access-button"
          @click="openAccess"
        >
          <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <circle
              cx="6.5"
              cy="7"
              r="3.5"
              stroke="currentColor"
              stroke-width="1.4"
            />
            <path
              d="m9 9.5 7 7m-2-2 2-2m-4 0 2-2"
              stroke="currentColor"
              stroke-width="1.4"
            /></svg
          ><span>访问设置</span
          ><span
            v-if="keySet"
            class="credential-indicator"
            aria-label="已设置凭据"
          />
        </button>
      </nav>
    </header>

    <main id="main-content" class="main-content">
      <div class="page-heading">
        <div>
          <div class="eyebrow">
            <span class="eyebrow-line" /> LOCAL INFERENCE · OBSERVABILITY
          </div>
          <h1>模型运行概览<span class="heading-period">.</span></h1>
          <p class="page-description">
            系统资源、模型服务与请求活动，在一个视图中掌握。
          </p>
        </div>
        <span class="environment-tag"
          ><span class="status-dot" aria-hidden="true" /> 本地工作空间</span
        >
      </div>

      <div class="connection-bar" role="status" aria-live="polite">
        <div class="connection-info">
          <StatusBadge :tone="statusTone">{{ statusLabel }}</StatusBadge
          ><span class="connection-divider" /><span class="last-update"
            >最后成功更新 <time>{{ time(lastSuccessAt) }}</time></span
          ><span v-if="access === 'required'" class="public-badge"
            >公开概览</span
          >
        </div>
        <div class="refresh-actions">
          <button
            class="text-button pause-button"
            :aria-pressed="paused"
            @click="monitor.setPaused(!paused)"
          >
            <svg
              v-if="!paused"
              viewBox="0 0 16 16"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M5 3v10M11 3v10"
                stroke="currentColor"
                stroke-width="2"
              /></svg
            ><svg v-else viewBox="0 0 16 16" aria-hidden="true">
              <path d="m5 3 8 5-8 5z" fill="currentColor" /></svg
            >{{ paused ? "恢复刷新" : "暂停刷新" }}</button
          ><button
            class="refresh-button"
            :disabled="loading"
            :aria-busy="loading"
            @click="refresh"
          >
            <svg
              viewBox="0 0 20 20"
              fill="none"
              aria-hidden="true"
              :class="{ spinning: loading }"
            >
              <path
                d="M16.4 7A7 7 0 1 0 17 11M16.5 2v5h-5"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              /></svg
            ><span>{{ loading ? "刷新中" : "刷新" }}</span>
          </button>
        </div>
      </div>

      <div v-if="isDemo" class="demo-banner">
        <span>演示数据</span> 当前为模拟模型与系统读数，用于预览界面。
      </div>
      <div v-if="!snapshot && publicOverview" class="public-disclaimer">
        当前显示公开摘要，采集有效性由旧网关提供。完整状态需要监控 API v1
        及相应访问权限。
      </div>
      <div
        v-if="error && access !== 'required' && access !== 'unsupported'"
        class="notice notice--warning global-notice"
      >
        <div>
          <strong>{{
            connection === "disconnected"
              ? "暂时无法连接网关"
              : "状态更新暂不可用"
          }}</strong>
          <p>
            {{ error
            }}<span v-if="lastSuccessAt">
              当前保留上次成功数据，请留意更新时间。</span
            >
          </p>
        </div>
        <button class="text-button" @click="refresh">重新尝试 ↻</button>
      </div>

      <section class="overview-grid" aria-label="系统概览">
        <article class="metric-card panel">
          <div class="metric-heading">
            <span>处理器使用率</span
            ><StatusBadge v-if="systemStale" tone="warning" :dot="false"
              >已过期</StatusBadge
            ><span v-else class="metric-kicker">CPU</span>
          </div>
          <div class="metric-value">
            {{ number(cpu, 1) }}<span v-if="isReading(cpu)">%</span
            ><span v-else class="unavailable-label">未提供</span>
          </div>
          <Sparkline
            :values="history.map((sample) => sample.cpu)"
            :times="history.map((sample) => sample.time)"
            :now="now"
            :max="100"
            label="CPU 使用率：本页近五分钟趋势"
          />
          <div class="metric-footnote">
            <span
              >用户 {{ number(system?.cpu.user, 1) }}% · 系统
              {{ number(system?.cpu.sys, 1) }}%</span
            ><span>近 5 分钟</span>
          </div>
        </article>
        <article class="metric-card panel">
          <div class="metric-heading">
            <span>系统内存</span
            ><StatusBadge v-if="systemStale" tone="warning" :dot="false"
              >已过期</StatusBadge
            ><span v-else class="metric-kicker">MEMORY</span>
          </div>
          <div class="metric-value">
            {{ number(memory?.used_gb, 1)
            }}<span v-if="isReading(memory?.used_gb)"
              >GB
              <span class="metric-total"
                >/ {{ number(memory?.total_gb, 1) }}</span
              ></span
            ><span v-else class="unavailable-label">未提供</span>
          </div>
          <Sparkline
            :values="history.map((sample) => sample.memory)"
            :times="history.map((sample) => sample.time)"
            :now="now"
            :max="memory?.total_gb || undefined"
            color="blue"
            label="系统内存使用量：本页近五分钟趋势"
          />
          <div class="metric-footnote">
            <span>空闲 {{ number(memory?.free_gb, 1) }} GB</span
            ><span>系统物理内存</span>
          </div>
        </article>
        <article class="metric-card metric-card--services panel">
          <div class="metric-heading">
            <span>可用模型</span><span class="metric-kicker">SERVICES</span>
          </div>
          <div class="metric-value">
            {{ number(online)
            }}<span
              >/
              {{
                snapshot ? models.length : number(publicOverview?.model_count)
              }}</span
            >
          </div>
          <div class="service-segments" aria-hidden="true">
            <span
              v-for="index in Math.max(Math.min(models.length, 20), 8)"
              :key="index"
              :class="{
                'segment--healthy': online !== null && index <= online,
                'segment--offline':
                  snapshot && index > (online || 0) && index <= models.length,
              }"
            />
          </div>
          <div class="metric-footnote">
            <span>{{
              snapshot
                ? `${models.filter((model) => model.registered).length} 个已注册`
                : "完整清单需要认证"
            }}</span
            ><span>{{
              snapshot
                ? `${models.filter((model) => !model.registered).length} 个自动发现`
                : "只读监控"
            }}</span>
          </div>
        </article>
        <article class="metric-card metric-card--requests panel">
          <div class="metric-heading">
            <span>请求活动</span><span class="metric-kicker">TRAFFIC</span>
          </div>
          <div class="traffic-values">
            <div>
              <strong :class="{ 'active-number': activities > 0 }">{{
                hasLanes ? number(activities) : "—"
              }}</strong
              ><span>活动请求</span>
            </div>
            <span class="traffic-divider" />
            <div>
              <strong>{{ hasLanes ? number(waiting) : "—" }}</strong
              ><span>等待请求</span>
            </div>
          </div>
          <div class="load-average">
            <span>系统负载</span
            ><span>{{
              system?.load_avg?.length
                ? system.load_avg.map((value) => number(value, 2)).join(" / ")
                : "未提供"
            }}</span>
          </div>
          <div class="metric-footnote">
            <span>负载：1 / 5 / 15 分钟</span><span>当前网关</span>
          </div>
        </article>
      </section>

      <section class="lanes-grid" aria-label="各能力请求通道">
        <article v-for="lane in laneMeta" :key="lane.key" class="lane-card">
          <span
            class="lane-symbol"
            :class="`lane-symbol--${lane.key}`"
            aria-hidden="true"
            >{{ lane.symbol }}</span
          >
          <div class="lane-title">
            <h2>{{ lane.label }}</h2>
            <span>{{ lane.english }}</span>
          </div>
          <div class="lane-counts">
            <span
              ><strong
                :class="{ 'active-number': (lanes[lane.key]?.active || 0) > 0 }"
                >{{ number(lanes[lane.key]?.active) }}</strong
              >
              活动</span
            ><span
              ><strong>{{ number(lanes[lane.key]?.waiting) }}</strong>
              等待</span
            >
          </div>
          <div
            class="lane-capacity"
            :title="`通道活动容量 ${number(lanes[lane.key]?.max)}`"
          >
            <span
              :style="{
                width: `${percent(lanes[lane.key]?.active, lanes[lane.key]?.max)}%`,
              }"
            />
          </div>
        </article>
      </section>

      <section
        v-if="diagnostics.length"
        class="diagnostics panel"
        aria-labelledby="diagnostics-title"
      >
        <div class="diagnostics-heading">
          <div>
            <span class="diagnostic-symbol" aria-hidden="true">!</span>
            <h2 id="diagnostics-title">需要关注</h2>
            <span class="count-badge">{{ diagnostics.length }}</span>
          </div>
          <span class="section-note">先了解原因，再处理异常</span>
        </div>
        <ul>
          <li
            v-for="(item, index) in shownDiagnostics"
            :key="`${item.code}-${item.model_key}-${index}`"
            :class="`diagnostic--${item.severity}`"
          >
            <span class="status-dot" aria-hidden="true" /><span>{{
              item.message
            }}</span
            ><button
              v-if="
                item.model_key &&
                models.some((model) => model.key === item.model_key)
              "
              class="text-button"
              @click="selectModel(item.model_key, $event)"
            >
              查看模型 ↗
            </button>
          </li>
        </ul>
        <button
          v-if="diagnostics.length > 3"
          class="text-button diagnostics-toggle"
          :aria-expanded="diagnosticsExpanded"
          @click="diagnosticsExpanded = !diagnosticsExpanded"
        >
          {{
            diagnosticsExpanded
              ? "收起"
              : `查看其余 ${diagnostics.length - 3} 项`
          }}
          <span aria-hidden="true">{{ diagnosticsExpanded ? "↑" : "↓" }}</span>
        </button>
      </section>

      <ModelTable
        :models="models"
        :loading="loading"
        :restricted="access === 'required'"
        :unsupported="access === 'unsupported'"
        :unavailable="Boolean(error)"
        :stale="catalogStale"
        @select="selectModel"
        @access="openAccess"
      />
      <footer class="page-footer">
        <span
          ><span class="footer-mark" aria-hidden="true">◇</span> LOCAL LLM
          CONSOLE<span class="footer-divider">/</span>只读运行视图</span
        ><span>趋势仅保留在本页 · 缺失数据以 — 表示</span>
      </footer>
    </main>
  </div>
  <ModelDrawer
    v-if="selectedKey"
    :model="selectedModel"
    :detail="detail"
    :include-output="includeOutput"
    :now="now"
    :error="error"
    @close="selectModel(null)"
    @output="monitor.setIncludeOutput"
    @refresh="refresh"
  />
  <AccessDialog
    v-if="accessOpen"
    :has-credential="keySet"
    @close="accessOpen = false"
    @apply="applyKey"
    @clear="clearKey"
  />
</template>
