<script setup lang="ts">
import { computed } from "vue";
import type { MonitorService } from "../api/types";
import StatusBadge from "../../../shared/components/StatusBadge.vue";
import { availabilityLabels } from "../../../shared/components/format";

const props = defineProps<{
  services: MonitorService[];
}>();

const cards = computed(() =>
  props.services.map((service) => ({
    key: service.key,
    alias: service.alias,
    note: `${service.host}:${service.port}`,
    href: service.upstream,
    endpoint: service.endpoint,
    symbol: service.alias.slice(0, 1).toUpperCase(),
    availability: service.availability,
  })),
);

function tone(state: MonitorService["availability"]["state"]) {
  return state === "healthy"
    ? "good"
    : state === "unready" || state === "unknown"
      ? "warning"
      : "danger";
}
</script>

<template>
  <section class="models-section panel" aria-labelledby="services-title">
    <div class="section-heading">
      <div class="section-heading-title">
        <h2 id="services-title">关联服务</h2>
        <span class="count-badge">{{ cards.length }}</span>
      </div>
      <span class="section-note">点击卡片打开上游页面；不进入对话模型列表</span>
    </div>
    <div class="service-cards">
      <a
        v-for="card in cards"
        :key="card.key"
        class="service-card"
        :href="card.href"
        target="_blank"
        rel="noopener noreferrer"
      >
        <span class="lane-symbol" aria-hidden="true">{{ card.symbol }}</span>
        <div class="service-card-copy">
          <h3>{{ card.alias }}</h3>
          <span>{{ card.note }}</span>
          <code class="service-endpoint">{{ card.endpoint }}</code>
        </div>
        <div class="service-card-meta">
          <StatusBadge
            :tone="tone(card.availability.state)"
            >{{
              availabilityLabels[card.availability.state] ||
              card.availability.state
            }}</StatusBadge
          >
          <span class="service-open">打开 ↗</span>
        </div>
      </a>
    </div>
  </section>
</template>
