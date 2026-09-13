<script setup lang="ts">
import { computed, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import python from 'highlight.js/lib/languages/python'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
const props = defineProps<{ content: string }>()
hljs.registerLanguage('javascript', javascript); hljs.registerLanguage('python', python)
hljs.registerLanguage('json', json); hljs.registerLanguage('bash', bash)
const copied = ref('')
const markdown = new MarkdownIt({ html: false, linkify: false, breaks: true, highlight(code, language) {
  if (language && hljs.getLanguage(language) && code.length < 50000) return hljs.highlight(code, { language, ignoreIllegals: true }).value
  return ''
} })
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(`[图片：${tokens[index]?.content ?? ''}，未加载]`)
const validateLink = markdown.validateLink.bind(markdown)
markdown.validateLink = value => {
  if (!validateLink(value)) return false
  try { return ['http:', 'https:', 'mailto:'].includes(new URL(value, window.location.origin).protocol) }
  catch { return false }
}
const linkOpen = markdown.renderer.rules.link_open
const fence = markdown.renderer.rules.fence!
markdown.renderer.rules.fence = (tokens, index, options, env, renderer) => `<div class="chat-code-block"><button type="button" data-copy-code>复制代码</button>${fence(tokens, index, options, env, renderer)}</div>`
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index]!.attrSet('target', '_blank'); tokens[index]!.attrSet('rel', 'noopener noreferrer'); tokens[index]!.attrSet('referrerpolicy', 'no-referrer')
  return linkOpen ? linkOpen(tokens, index, options, env, renderer) : renderer.renderToken(tokens, index, options)
}
const html = computed(() => markdown.render(props.content))
async function copyCode(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (!target.closest('[data-copy-code]')) return
  const pre = target.closest('.chat-code-block')?.querySelector('pre')
  if (!pre) return
  try { await navigator.clipboard.writeText(pre.textContent ?? ''); copied.value = '代码已复制' }
  catch { copied.value = '复制失败，请手动选择代码' }
}
</script>
<template>
  <!-- markdown-it raw HTML is disabled; its URL validator and escaped highlight output are retained. -->
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div class="chat-markdown" @click="copyCode" v-html="html" />
  <span class="chat-notice" role="status">{{ copied }}</span>
</template>
