<script setup lang="ts">
import { ref } from "vue";
import DialogShell from "./DialogShell.vue";
defineProps<{ hasCredential: boolean }>();
const emit = defineEmits<{ close: []; apply: [key: string]; clear: [] }>();
const credential = ref("");
function apply() {
  if (credential.value.trim()) {
    emit("apply", credential.value.trim());
    credential.value = "";
    emit("close");
  }
}
function clear() {
  credential.value = "";
  emit("clear");
  emit("close");
}
</script>

<template>
  <DialogShell label-id="access-title" @close="emit('close')">
    <div class="dialog-header">
      <div>
        <p class="eyebrow">ACCESS SETTINGS</p>
        <h2 id="access-title">访问设置</h2>
      </div>
      <button
        class="icon-button"
        aria-label="关闭访问设置"
        @click="emit('close')"
      >
        ×
      </button>
    </div>
    <p class="dialog-description">
      使用网关 API Key 查看完整模型状态与运行详情。
    </p>
    <form @submit.prevent="apply">
      <label class="field-label" for="api-key">API Key</label>
      <input
        id="api-key"
        v-model="credential"
        type="password"
        autocomplete="off"
        spellcheck="false"
        placeholder="输入网关 API Key"
        class="credential-input"
      />
      <div class="credential-note">
        <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path
            d="M5 8V6a5 5 0 0 1 10 0v2M4 8h12v10H4z"
            stroke="currentColor"
            stroke-width="1.4"
          />
        </svg>
        <p>凭据只保存在当前页面内存中。刷新或关闭页面后需要重新输入。</p>
      </div>
      <div class="dialog-actions">
        <button
          v-if="hasCredential"
          type="button"
          class="button button--danger-quiet"
          @click="clear"
        >
          清除凭据</button
        ><span class="action-spacer" /><button
          type="button"
          class="button button--quiet"
          @click="emit('close')"
        >
          取消</button
        ><button
          type="submit"
          class="button button--primary"
          :disabled="!credential.trim()"
        >
          应用凭据
        </button>
      </div>
    </form>
  </DialogShell>
</template>
