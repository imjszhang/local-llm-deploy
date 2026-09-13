<script setup lang="ts">
import { computed, inject, ref } from "vue";
import { consoleContext } from "../../app/context";
import { isLocalConsole } from "../auth/localSession";
import DialogShell from "./DialogShell.vue";
defineProps<{ hasCredential: boolean }>();
const emit = defineEmits<{ close: []; apply: [key: string]; clear: [] }>();
const credential = ref("");
const context = inject(consoleContext, null);
const local = computed(() => context?.credentialSource?.value === 'local');
const canConnectLocal = isLocalConsole(location.hostname) && !!context?.connectLocal;
async function connectLocal() {
  await context?.connectLocal?.();
  if (local.value) emit('close');
}
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
      {{ local ? '已自动连接本机网关。网关在服务端读取项目的 .api-key，浏览器使用临时会话凭据。' : '本机页面可自动连接网关；通过局域网或远程地址访问时，请输入网关 API Key。' }}
    </p>
    <p v-if="local" class="credential-note">本机会话有效 8 小时，刷新页面自动重新连接。根目录中的 Key 不会传给浏览器。</p>
    <button v-if="canConnectLocal && !local" class="button button--primary" type="button" :disabled="context?.localConnecting?.value" @click="connectLocal">{{ context?.localConnecting?.value ? '正在连接本机…' : '自动连接本机' }}</button>
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
        <p>凭据只保存在当前页面内存中。本机刷新后自动重连，手动 Key 刷新后需重新输入。更换或清除凭据会停止接收生成并清空页面显示，已保存的对话仍保留在本机。请先确认保存完成。</p>
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
