<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from "vue";
const props = defineProps<{ text: string; label: string }>();
const viewport = ref<HTMLElement | null>(null);
const following = ref(true);
function onScroll() {
  const element = viewport.value;
  if (element)
    following.value =
      element.scrollHeight - element.scrollTop - element.clientHeight < 32;
}
function latest() {
  const element = viewport.value;
  if (element) element.scrollTop = element.scrollHeight;
  following.value = true;
}
watch(
  () => props.text,
  async () => {
    if (following.value) {
      await nextTick();
      latest();
    }
  },
);
onMounted(latest);
</script>

<template>
  <div class="slot-output">
    <div class="slot-output-label">
      <span>{{ label }}</span
      ><button
        v-if="!following"
        type="button"
        class="text-button"
        @click="latest"
      >
        回到最新 ↓</button
      ><span v-else class="muted">跟随最新</span>
    </div>
    <pre ref="viewport" tabindex="0" :aria-label="label" @scroll="onScroll">{{
      text || "暂无输出"
    }}</pre>
  </div>
</template>
