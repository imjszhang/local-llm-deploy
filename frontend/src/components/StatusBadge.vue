<script setup lang="ts">
withDefaults(
  defineProps<{
    tone?: "good" | "warning" | "danger" | "muted";
    dot?: boolean;
  }>(),
  { tone: "muted", dot: true },
);
</script>

<template>
  <span class="status-badge" :class="`status-badge--${tone}`"
    ><span v-if="dot" class="status-dot" aria-hidden="true" /><slot
  /></span>
</template>
