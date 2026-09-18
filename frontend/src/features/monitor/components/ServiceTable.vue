<script setup lang="ts">
import { computed } from "vue";
import type { MonitorService } from "../api/types";
import StatusBadge from "../../../shared/components/StatusBadge.vue";
import { availabilityLabels } from "../../../shared/components/format";

const props = defineProps<{
  services: MonitorService[];
}>();

interface ServiceCard {
  key: string;
  alias: string;
  note: string;
  href: string;
  external: boolean;
  endpoint: string;
  symbol: string;
  availability: MonitorService["availability"] | null;
}

const cards = computed<ServiceCard[]>(() => [
  {
    key: "knowledge",
    alias: "知识库",
    note: "独立知识库反代",
    href: "/knowledge/",
    external: false,
    endpoint: "/knowledge/",
    symbol: "K",
    availability: null,
  },
  ...props.services.map((service) => ({
    key: service.key,
    alias: service.alias,
    note: `${service.host}:${service.port}`,
    href: service.upstream,
    external: true,
    endpoint: service.endpoint,
    symbol: service.alias.slice(0, 1).toUpperCase(),
    availability: service.availability,
  })),
]);

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
      <span class="section-note">点击卡片打开页面；不进入对话模型列表</span>
    </div>
    <div class="service-cards">
      <a
        v-for="card in cards"
        :key="card.key"
        class="service-card"
        :href="card.href"
        :target="card.external ? '_blank' : undefined"
        :rel="card.external ? 'noopener noreferrer' : undefined"
      >
        <span class="lane-symbol" aria-hidden="true">{{ card.symbol }}</span>
        <div class="service-card-copy">
          <h3>{{ card.alias }}</h3>
          <span>{{ card.note }}</span>
          <code class="service-endpoint">{{ card.endpoint }}</code>
        </div>
        <div class="service-card-meta">
          <StatusBadge
            v-if="card.availability"
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
