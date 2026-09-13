<script setup lang="ts">
import type { ModelDetail, MonitorModel, Section } from "../api/types";
import DialogShell from "../components/DialogShell.vue";
import StatusBadge from "../components/StatusBadge.vue";
import SlotOutput from "../components/SlotOutput.vue";
import {
  availabilityLabels,
  backendLabels,
  capabilityLabels,
  lifecycleLabels,
  modelStatus,
  number,
  percent,
  sourceStale,
  time,
} from "../components/format";
defineProps<{
  model: MonitorModel | null;
  detail: ModelDetail | null;
  includeOutput: boolean;
  error: string | null;
  now: number;
}>();
const emit = defineEmits<{
  close: [];
  output: [enabled: boolean];
  refresh: [];
}>();
function sectionMessage(section: Section): string {
  return (
    section.message ||
    {
      unsupported: "此后端未提供该项监控",
      loading: "正在采集…",
      error: "暂时无法采集",
      unauthorized: "后端需要认证",
      stale: "采集失败，显示上次成功数据",
      ready: "暂无数据",
    }[section.state]
  );
}
const metricLabels: Record<string, string> = {
  prompt_tokens_total: "累计输入 token",
  tokens_predicted_total: "累计生成 token",
  tokens_predicted_seconds: "生成速度",
  predicted_tokens_seconds: "生成速度",
  prompt_tokens_seconds: "输入处理速度",
  requests_processing: "正在处理",
  requests_deferred: "已延迟请求",
  kv_cache_usage_ratio: "后端报告 KV 使用率",
  n_tokens_predicted: "已生成 token",
  n_tokens_evaluated: "已处理 token",
};
function metricLabel(name: string) {
  return metricLabels[name.replace(/^llamacpp:/, "")] || name;
}
</script>

<template>
  <DialogShell label-id="detail-title" kind="drawer" @close="emit('close')">
    <div class="drawer-header">
      <div>
        <p class="eyebrow">MODEL INSPECTOR</p>
        <h2 id="detail-title">{{ model?.alias || "模型详情" }}</h2>
        <div v-if="model" class="drawer-subtitle">
          <span>{{ backendLabels[model.backend] || model.backend }}</span
          ><span aria-hidden="true">·</span
          ><span>{{
            model.capabilities
              .map((item) => capabilityLabels[item] || item)
              .join(" / ")
          }}</span>
        </div>
      </div>
      <button
        class="icon-button"
        aria-label="关闭模型详情"
        @click="emit('close')"
      >
        ×
      </button>
    </div>
    <div class="drawer-content">
      <template v-if="model">
        <div class="detail-status-strip">
          <StatusBadge :tone="modelStatus(model).tone">{{
            modelStatus(model).label
          }}</StatusBadge
          ><span>{{ lifecycleLabels[model.lifecycle.state] }}</span
          ><button class="text-button" @click="emit('refresh')">
            刷新详情 ↻
          </button>
        </div>
        <div v-if="model.activity.uncertain" class="notice notice--warning">
          <strong>请求结束状态待确认</strong>
          <p>
            网关仍保留请求占用。监控页面不会重置此状态，请检查后端请求是否已经结束。
          </p>
        </div>
        <div
          v-if="sourceStale(detail?.source, now)"
          class="notice notice--warning"
        >
          <strong>详情数据已过期</strong>
          <p>
            上次成功：{{ time(detail?.source.last_success_at) }}。{{
              detail?.source.error?.message || "正在尝试重新采集。"
            }}
          </p>
        </div>
        <section class="drawer-section">
          <h3>服务信息</h3>
          <dl class="detail-properties">
            <div>
              <dt>服务可用性</dt>
              <dd>{{ availabilityLabels[model.availability.state] }}</dd>
            </div>
            <div v-if="model.availability.reason">
              <dt>探测说明</dt>
              <dd>{{ model.availability.reason }}</dd>
            </div>
            <div>
              <dt>生命周期</dt>
              <dd>{{ lifecycleLabels[model.lifecycle.state] }}</dd>
            </div>
            <div v-if="model.lifecycle.reason">
              <dt>运行记录</dt>
              <dd>{{ model.lifecycle.reason }}</dd>
            </div>
            <div>
              <dt>网关路由</dt>
              <dd>
                {{
                  model.routing.available === true
                    ? "可调用"
                    : model.routing.available === false
                      ? "不可调用"
                      : "待确认"
                }}
              </dd>
            </div>
            <div v-if="model.routing.reason">
              <dt>路由说明</dt>
              <dd>{{ model.routing.reason }}</dd>
            </div>
            <div>
              <dt>管理方式</dt>
              <dd>
                {{
                  model.management === "external"
                    ? "外部管理"
                    : model.management === "launchd"
                      ? "launchd"
                      : "本地进程"
                }}
              </dd>
            </div>
            <div>
              <dt>模型来源</dt>
              <dd>{{ model.registered ? "模型注册表" : "Ollama 自动发现" }}</dd>
            </div>
            <div v-if="model.backend_model">
              <dt>后端模型</dt>
              <dd class="mono">{{ model.backend_model }}</dd>
            </div>
            <div>
              <dt>网关接口</dt>
              <dd class="mono">{{ model.endpoint || "未提供" }}</dd>
            </div>
            <div>
              <dt>服务端口</dt>
              <dd class="mono">
                {{ model.port === null ? "未提供" : model.port }}
              </dd>
            </div>
          </dl>
        </section>
        <section class="drawer-section">
          <h3>网关活动 <span>当前网关观察范围</span></h3>
          <div class="detail-activity-grid">
            <div>
              <strong>{{ number(model.activity.active) }}</strong
              ><span>活动请求</span>
            </div>
            <div>
              <strong>{{ number(model.activity.waiting) }}</strong
              ><span>等待请求</span>
            </div>
            <div>
              <strong>{{
                number(model.activity.active + model.activity.waiting)
              }}</strong
              ><span>在途请求</span>
            </div>
          </div>
          <div v-if="model.budget" class="detail-budget">
            <div>
              <span>网关预留 token 预算</span
              ><span class="mono"
                >{{ number(model.budget.used) }} /
                {{ number(model.budget.total) }}</span
              >
            </div>
            <div class="meter">
              <span
                :style="{
                  width: `${percent(model.budget.used, model.budget.total)}%`,
                }"
              />
            </div>
            <p class="caption">用于调度的估算预算，不代表真实 KV 内存占用。</p>
          </div>
        </section>
        <div v-if="!detail && !error" class="detail-loading" role="status">
          <span class="loading-dot" /> 正在读取运行详情…
        </div>
        <div v-else-if="!detail && error" class="notice notice--warning">
          <strong>运行详情暂时不可用</strong>
          <p>{{ error }}</p>
        </div>
        <template v-if="detail">
          <section class="drawer-section">
            <h3>
              健康探测
              <StatusBadge
                :tone="
                  detail.health.state === 'ready'
                    ? 'good'
                    : detail.health.state === 'error'
                      ? 'danger'
                      : 'muted'
                "
                >{{
                  detail.health.state === "ready" ? "采集正常" : "未就绪"
                }}</StatusBadge
              >
            </h3>
            <p class="caption">
              {{
                detail.health.message ||
                (detail.health.state === "ready"
                  ? "后端已响应健康检查。"
                  : sectionMessage(detail.health))
              }}
            </p>
          </section>
          <section
            v-if="model.monitoring_support.process_stats"
            class="drawer-section"
          >
            <h3>模型进程</h3>
            <div
              v-if="
                detail.process.state === 'ready' ||
                detail.process.state === 'stale'
              "
              class="process-grid"
            >
              <div>
                <span>进程 CPU</span
                ><strong
                  >{{ number(detail.process.cpu_pct, 1)
                  }}<small>%</small></strong
                >
              </div>
              <div>
                <span>进程 RSS</span
                ><strong
                  >{{ number(detail.process.rss_gb, 2)
                  }}<small>GB</small></strong
                >
              </div>
            </div>
            <p v-else class="caption">{{ sectionMessage(detail.process) }}</p>
            <p class="caption">
              RSS 是进程驻留内存，包含共享页；与系统物理内存使用量的口径不同。
            </p>
          </section>
          <section v-if="model.backend === 'ollama'" class="drawer-section">
            <h3>Ollama 运行信息</h3>
            <dl
              v-if="
                detail.ollama.state === 'ready' ||
                detail.ollama.state === 'stale'
              "
              class="detail-properties"
            >
              <div>
                <dt>内存加载</dt>
                <dd>
                  {{
                    detail.ollama.loaded === null
                      ? "待确认"
                      : detail.ollama.loaded
                        ? "已加载"
                        : "未加载"
                  }}
                </dd>
              </div>
              <div>
                <dt>模型大小</dt>
                <dd>{{ number(detail.ollama.size_gb, 2) }} GB</dd>
              </div>
              <div>
                <dt>显存驻留</dt>
                <dd>{{ number(detail.ollama.vram_gb, 2) }} GB</dd>
              </div>
              <div v-if="detail.ollama.version">
                <dt>服务版本</dt>
                <dd>{{ detail.ollama.version }}</dd>
              </div>
              <div>
                <dt>量化</dt>
                <dd>{{ detail.ollama.quantization || "未提供" }}</dd>
              </div>
              <div>
                <dt>卸载时间</dt>
                <dd>{{ detail.ollama.expires_at || "未提供" }}</dd>
              </div>
            </dl>
            <p v-else class="caption">{{ sectionMessage(detail.ollama) }}</p>
          </section>
          <section
            v-if="model.monitoring_support.metrics"
            class="drawer-section"
          >
            <h3>后端指标</h3>
            <dl
              v-if="detail.metrics.values.length"
              class="detail-properties metrics-properties"
            >
              <div
                v-for="(metric, index) in detail.metrics.values"
                :key="`${metric.name}-${index}`"
              >
                <dt :title="metric.name">{{ metricLabel(metric.name) }}</dt>
                <dd class="mono">
                  {{ number(metric.value, 2) }}
                  <span class="muted">{{ metric.unit }}</span>
                </dd>
              </div>
            </dl>
            <p v-else class="caption">{{ sectionMessage(detail.metrics) }}</p>
          </section>
          <section v-if="model.monitoring_support.slots" class="drawer-section">
            <h3>
              推理槽位 <span>{{ detail.slots.items.length }} 个</span>
            </h3>
            <p v-if="!detail.slots.items.length" class="caption">
              {{ sectionMessage(detail.slots) }}
            </p>
            <div
              v-for="slot in detail.slots.items"
              :key="slot.id"
              class="slot-card"
            >
              <div class="slot-heading">
                <span class="mono">SLOT {{ slot.id }}</span
                ><StatusBadge :tone="slot.busy ? 'good' : 'muted'">{{
                  slot.busy ? "处理中" : "空闲"
                }}</StatusBadge>
              </div>
              <div class="slot-readings">
                <span
                  >输入
                  <strong>{{ number(slot.prompt_tokens) }}</strong> token</span
                ><span
                  >已生成
                  <strong>{{ number(slot.decoded) }}</strong> token</span
                >
              </div>
              <p v-if="slot.limit !== null" class="caption">
                生成上限 {{ number(slot.limit) }} token · 不表示完成进度
              </p>
              <template v-if="includeOutput"
                ><SlotOutput
                  v-if="slot.reasoning"
                  :text="slot.reasoning"
                  label="思考片段" /><SlotOutput
                  :text="slot.generated || ''"
                  label="输出片段"
              /></template>
            </div>
          </section>
          <section
            v-if="model.monitoring_support.output"
            class="drawer-section output-setting"
          >
            <label
              ><input
                type="checkbox"
                :checked="includeOutput"
                @change="
                  emit('output', ($event.target as HTMLInputElement).checked)
                "
              /><span>显示高级输出</span></label
            >
            <p class="caption">
              按需读取后端已有的有界文本片段。仅显示在当前页面，不新增正文存储。
            </p>
          </section>
          <div
            v-if="
              !model.monitoring_support.metrics &&
              !model.monitoring_support.slots
            "
            class="notice notice--neutral"
          >
            <strong>此服务提供基础监控</strong>
            <p>
              当前后端未提供 metrics / slots
              接口。服务健康与网关活动仍显示在上方。
            </p>
          </div>
        </template>
      </template>
      <div v-else class="empty-state">
        <h3>模型不在当前清单中</h3>
        <p>该模型可能已从注册表移除，或发现结果发生变化。</p>
      </div>
    </div>
    <div class="drawer-footer">
      <span class="status-dot" aria-hidden="true" /><span
        >最后成功采集 {{ time(detail?.source.last_success_at) }}</span
      ><span class="read-only-label">只读</span>
    </div>
  </DialogShell>
</template>
