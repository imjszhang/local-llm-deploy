<script setup lang="ts">
import type { MonitorApp } from "../api/types";
import StatusBadge from "../../../shared/components/StatusBadge.vue";
import { availabilityLabels } from "../../../shared/components/format";

const props = defineProps<{
  apps: MonitorApp[];
}>();

const notes: Record<string, string> = {
  knowledge: "独立知识库反代",
  video: "本机视频库",
};

function tone(state: MonitorApp["availability"]["state"]) {
  return state === "healthy"
    ? "good"
    : state === "unready" || state === "unknown"
      ? "warning"
      : "danger";
}
</script>

<template>
  <section class="models-section panel" aria-labelledby="apps-title">
    <div class="section-heading">
      <div class="section-heading-title">
        <h2 id="apps-title">应用入口</h2>
        <span class="count-badge">{{ props.apps.length }}</span>
      </div>
      <span class="section-note">点击卡片打开页面；不进入对话模型或关联服务</span>
    </div>
    <div class="service-cards">
      <a
        v-for="app in props.apps"
        :key="app.key"
        class="service-card"
        :href="app.href"
      >
        <span class="lane-symbol" aria-hidden="true">{{
          app.alias.slice(0, 1)
        }}</span>
        <div class="service-card-copy">
          <h3>{{ app.alias }}</h3>
          <span>{{ notes[app.kind] || app.kind }}</span>
          <code class="service-endpoint">{{ app.endpoint }}</code>
        </div>
        <div class="service-card-meta">
          <StatusBadge :tone="tone(app.availability.state)">{{
            availabilityLabels[app.availability.state] ||
            app.availability.state
          }}</StatusBadge>
          <span class="service-open">打开 ↗</span>
        </div>
      </a>
    </div>
  </section>
</template>
