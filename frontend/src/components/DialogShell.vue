<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
const props = defineProps<{ labelId: string; kind?: "drawer" | "dialog" }>();
const emit = defineEmits<{ close: [] }>();
const panel = ref<HTMLElement | null>(null);
let returnFocus: HTMLElement | null = null;
let originalOverflow = "";
function isTopDialog() {
  const dialogs = document.querySelectorAll(
    '[role="dialog"][aria-modal="true"]',
  );
  return dialogs[dialogs.length - 1] === panel.value;
}
function focusin() {
  if (isTopDialog() && !panel.value?.contains(document.activeElement))
    panel.value?.focus();
}
function focusable(): HTMLElement[] {
  return Array.from(
    panel.value?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
    ) || [],
  ).filter((element) => element.getClientRects().length > 0);
}
function keydown(event: KeyboardEvent) {
  if (!isTopDialog()) return;
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopImmediatePropagation();
    emit("close");
  }
  if (event.key !== "Tab") return;
  const elements = focusable();
  const first = elements[0];
  const last = elements[elements.length - 1];
  if (!first) {
    event.preventDefault();
    panel.value?.focus();
    return;
  }
  if (!panel.value?.contains(document.activeElement)) {
    event.preventDefault();
    (event.shiftKey ? last : first)?.focus();
    return;
  }
  if (
    event.shiftKey &&
    (document.activeElement === first || document.activeElement === panel.value)
  ) {
    event.preventDefault();
    last?.focus();
  } else if (
    !event.shiftKey &&
    (document.activeElement === last || document.activeElement === panel.value)
  ) {
    event.preventDefault();
    first.focus();
  }
}
onMounted(async () => {
  returnFocus =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  originalOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  document.addEventListener("keydown", keydown);
  document.addEventListener("focusin", focusin);
  await nextTick();
  panel.value?.focus();
});
onBeforeUnmount(() => {
  document.removeEventListener("keydown", keydown);
  document.removeEventListener("focusin", focusin);
  document.body.style.overflow = originalOverflow;
  if (returnFocus?.isConnected) returnFocus.focus();
});
</script>

<template>
  <Teleport to="body">
    <div
      class="modal-backdrop"
      :class="`modal-backdrop--${props.kind || 'dialog'}`"
      @click.self="emit('close')"
    >
      <section
        ref="panel"
        :class="[
          'modal-panel',
          props.kind === 'drawer' ? 'detail-drawer' : 'access-dialog',
        ]"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="labelId"
        tabindex="-1"
      >
        <slot />
      </section>
    </div>
  </Teleport>
</template>
