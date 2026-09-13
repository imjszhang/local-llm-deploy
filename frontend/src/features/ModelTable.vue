<script setup lang="ts">
import { computed, ref } from "vue";
import type { MonitorModel } from "../api/types";
import StatusBadge from "../components/StatusBadge.vue";
import {
  backendLabels,
  capabilityLabels,
  lifecycleLabels,
  modelStatus,
  number,
  percent,
} from "../components/format";
const props = defineProps<{
  models: MonitorModel[];
  loading: boolean;
  restricted: boolean;
  unsupported: boolean;
  unavailable: boolean;
  stale: boolean;
}>();
const emit = defineEmits<{ select: [key: string]; access: [] }>();
function openModel(key: string, event: Event) {
  (event.currentTarget as HTMLElement | null)?.focus();
  emit("select", key);
}
function openAccess(event: Event) {
  (event.currentTarget as HTMLElement | null)?.focus();
  emit("access");
}
const capabilitySymbols: Record<string, string> = {
  chat: "C",
  embed: "E",
  embedding: "E",
  rerank: "R",
  asr: "S",
};
const search = ref("");
const capability = ref("all");
const backend = ref("all");
const status = ref("all");
const sort = ref("name");
const backends = computed(() =>
  Array.from(new Set(props.models.map((model) => model.backend))).sort(),
);
const capabilities = computed(() =>
  Array.from(
    new Set(props.models.flatMap((model) => model.capabilities)),
  ).sort(),
);
const filtered = computed(() => {
  const query = search.value.toLocaleLowerCase().trim();
  const models = props.models.filter((model) => {
    if (
      query &&
      !`${model.alias} ${model.backend_model || ""} ${model.backend} ${model.key}`
        .toLocaleLowerCase()
        .includes(query)
    )
      return false;
    if (
      capability.value !== "all" &&
      !model.capabilities.includes(capability.value)
    )
      return false;
    if (backend.value !== "all" && model.backend !== backend.value)
      return false;
    if (status.value === "healthy" && model.availability.state !== "healthy")
      return false;
    if (
      status.value === "busy" &&
      model.activity.active === 0 &&
      model.activity.waiting === 0
    )
      return false;
    if (
      status.value === "attention" &&
      model.availability.state === "healthy" &&
      !model.activity.uncertain
    )
      return false;
    return true;
  });
  return models.sort((a, b) => {
    if (sort.value === "activity") {
      const delta =
        b.activity.active +
        b.activity.waiting -
        a.activity.active -
        a.activity.waiting;
      if (delta) return delta;
    }
    if (sort.value === "status") {
      const delta =
        Number(a.availability.state === "healthy" && !a.activity.uncertain) -
        Number(b.availability.state === "healthy" && !b.activity.uncertain);
      if (delta) return delta;
    }
    return a.alias.localeCompare(b.alias, "zh-CN");
  });
});
function reset() {
  search.value = "";
  capability.value = "all";
  backend.value = "all";
  status.value = "all";
}
</script>

<template>
  <section class="models-section panel" aria-labelledby="models-title">
    <div class="section-heading">
      <div class="section-heading-title">
        <h2 id="models-title">模型服务</h2>
        <span class="count-badge">{{ models.length }}</span
        ><StatusBadge v-if="stale" tone="warning">数据已过期</StatusBadge>
      </div>
      <span class="section-note">注册模型与 Ollama 发现模型</span>
    </div>
    <div class="model-toolbar">
      <label class="search-field"
        ><svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <circle
            cx="8.5"
            cy="8.5"
            r="5.5"
            stroke="currentColor"
            stroke-width="1.5"
          />
          <path d="m13 13 4 4" stroke="currentColor" stroke-width="1.5" /></svg
        ><input
          v-model="search"
          type="search"
          aria-label="搜索模型"
          placeholder="搜索模型名称或后端…"
      /></label>
      <div class="model-filters">
        <select v-model="capability" aria-label="按能力筛选">
          <option value="all">全部能力</option>
          <option v-for="item in capabilities" :key="item" :value="item">
            {{ capabilityLabels[item] || item }}
          </option></select
        ><select v-model="backend" aria-label="按后端筛选">
          <option value="all">全部后端</option>
          <option v-for="item in backends" :key="item" :value="item">
            {{ backendLabels[item] || item }}
          </option></select
        ><select v-model="status" aria-label="按状态筛选">
          <option value="all">全部状态</option>
          <option value="healthy">可用</option>
          <option value="busy">处理中 / 等待</option>
          <option value="attention">需要关注</option></select
        ><select v-model="sort" aria-label="模型排序">
          <option value="name">名称排序</option>
          <option value="activity">活动优先</option>
          <option value="status">异常优先</option>
        </select>
      </div>
    </div>
    <div v-if="restricted" class="empty-state">
      <div class="empty-symbol" aria-hidden="true">⌑</div>
      <h3>连接你的模型服务</h3>
      <p>
        系统公开概览仍可查看。应用网关 API Key 后，解锁完整模型清单与运行详情。
      </p>
      <button class="button button--primary" @click="openAccess">
        设置访问凭据 <span aria-hidden="true">↗</span>
      </button>
    </div>
    <div v-else-if="unsupported" class="empty-state">
      <div class="empty-symbol" aria-hidden="true">↗</div>
      <h3>网关需要升级</h3>
      <p>
        当前网关尚未提供监控 API
        v1。请更新网关版本后再刷新页面；模型推理仍使用原有接口。
      </p>
    </div>
    <div
      v-else-if="loading && !models.length"
      class="table-loading"
      role="status"
    >
      <span class="loading-dot" /> 正在读取模型状态…
    </div>
    <div v-else-if="unavailable && !models.length" class="empty-state">
      <div class="empty-symbol" aria-hidden="true">↻</div>
      <h3>模型清单暂时不可用</h3>
      <p>尚未成功获取模型状态。请检查上方的更新提示后重试。</p>
    </div>
    <div v-else-if="!models.length" class="empty-state">
      <div class="empty-symbol" aria-hidden="true">◇</div>
      <h3>暂无模型</h3>
      <p>模型注册或 Ollama 发现成功后会显示在这里。</p>
    </div>
    <div v-else-if="!filtered.length" class="empty-state">
      <h3>没有匹配的模型</h3>
      <p>试试其他关键词，或重置筛选条件。</p>
      <button class="button button--quiet" @click="reset">重置筛选</button>
    </div>
    <div v-else class="model-table-wrap">
      <table class="model-table">
        <thead>
          <tr>
            <th scope="col">模型 / 能力</th>
            <th scope="col">后端</th>
            <th scope="col">服务状态</th>
            <th scope="col">
              网关活动
              <span
                class="help-text"
                title="仅统计通过当前网关的请求；等待不包含活动请求"
                >ⓘ</span
              >
            </th>
            <th scope="col">预留 token 预算</th>
            <th scope="col"><span class="sr-only">查看详情</span></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="model in filtered"
            :key="model.key"
            :data-model-key="model.key"
          >
            <td class="model-identity">
              <span
                class="model-monogram"
                :class="`model-monogram--${model.capabilities[0] || 'chat'}`"
                aria-hidden="true"
                >{{
                  capabilitySymbols[model.capabilities[0] || ""] || "M"
                }}</span
              >
              <div>
                <button
                  class="model-name"
                  @click="openModel(model.key, $event)"
                >
                  {{ model.alias }}
                </button>
                <div class="capability-list">
                  <span v-for="item in model.capabilities" :key="item">{{
                    capabilityLabels[item] || item
                  }}</span
                  ><span v-if="!model.registered" class="discovered-tag"
                    >自动发现</span
                  >
                </div>
              </div>
            </td>
            <td class="model-backend" data-label="后端">
              <span>{{ backendLabels[model.backend] || model.backend }}</span
              ><span class="table-secondary">{{
                model.management === "external"
                  ? "外部服务"
                  : model.management === "launchd"
                    ? "launchd 托管"
                    : "本地进程"
              }}</span>
            </td>
            <td class="model-state" data-label="服务状态">
              <StatusBadge :tone="modelStatus(model).tone">{{
                modelStatus(model).label
              }}</StatusBadge
              ><span class="table-secondary">{{
                lifecycleLabels[model.lifecycle.state]
              }}</span>
            </td>
            <td class="model-activity" data-label="网关活动">
              <span
                ><strong
                  :class="{ 'active-number': model.activity.active > 0 }"
                  >{{ number(model.activity.active) }}</strong
                ><span class="muted"> 活动</span
                ><span class="activity-divider">/</span
                ><strong>{{ number(model.activity.waiting) }}</strong
                ><span class="muted"> 等待</span></span
              ><span
                v-if="model.activity.uncertain"
                class="table-secondary text-warning"
                >请求结束待确认</span
              ><span v-else class="table-secondary">{{
                model.routing.available === true
                  ? "可通过网关调用"
                  : model.routing.available === false
                    ? "网关路由不可用"
                    : "路由状态待确认"
              }}</span>
            </td>
            <td class="model-budget" data-label="预留 token 预算">
              <template v-if="model.budget"
                ><div class="budget-numbers">
                  <strong>{{ number(model.budget.used) }}</strong
                  ><span>/ {{ number(model.budget.total) }}</span>
                </div>
                <div
                  class="meter"
                  role="img"
                  :aria-label="`预留 ${number(model.budget.used)}，总预算 ${number(model.budget.total)} token`"
                >
                  <span
                    :style="{
                      width: `${percent(model.budget.used, model.budget.total)}%`,
                    }"
                  /></div></template
              ><span v-else class="table-secondary">未提供</span>
            </td>
            <td class="model-open">
              <button
                class="icon-button"
                :aria-label="`查看 ${model.alias} 详情`"
                @click="openModel(model.key, $event)"
              >
                <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
                  <path
                    d="m7 4 6 6-6 6"
                    stroke="currentColor"
                    stroke-width="1.5"
                  />
                </svg>
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="models.length && !restricted" class="table-footer">
      <span>显示 {{ filtered.length }} / {{ models.length }} 个模型</span
      ><span>活动与等待仅统计当前网关</span>
    </div>
  </section>
</template>
