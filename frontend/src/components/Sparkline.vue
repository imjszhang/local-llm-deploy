<script setup lang="ts">
import { computed } from "vue";
import { isReading } from "./format";
const props = withDefaults(
  defineProps<{
    values: (number | null)[];
    times: number[];
    now: number;
    label: string;
    max?: number;
    color?: "green" | "blue";
  }>(),
  { color: "green" },
);
const points = computed(() => {
  const top =
    props.max || Math.max(...props.values.filter(isReading), 1) * 1.15;
  const start = props.now - 300000;
  return props.values.map((value, index) => {
    const time = props.times[index];
    if (!isReading(value) || !isReading(time) || time < start) return null;
    const x = Math.min(1, (time - start) / 300000) * 280;
    const y = 44 - Math.min(1, Math.max(0, value / top)) * 36;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
});
const segments = computed(() => {
  const chunks: string[][] = [[]];
  points.value.forEach((point) => {
    if (point) chunks[chunks.length - 1]!.push(point);
    else chunks.push([]);
  });
  return chunks.filter((chunk) => chunk.length);
});
</script>

<template>
  <svg
    class="sparkline"
    :class="`sparkline--${color}`"
    viewBox="0 0 280 52"
    preserveAspectRatio="none"
    role="img"
    :aria-label="label"
  >
    <path class="sparkline-grid" d="M0 44H280 M0 26H280 M0 8H280" />
    <template v-for="(segment, index) in segments" :key="index">
      <polyline
        v-if="segment.length > 1"
        :points="segment.join(' ')"
        class="sparkline-line"
        vector-effect="non-scaling-stroke"
      />
      <circle
        v-else
        :cx="segment[0]?.split(',')[0]"
        :cy="segment[0]?.split(',')[1]"
        r="2"
        class="sparkline-dot"
      />
    </template>
  </svg>
</template>
