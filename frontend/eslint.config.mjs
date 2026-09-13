import js from '@eslint/js'
import ts from 'typescript-eslint'
import vue from 'eslint-plugin-vue'

export default [
  { ignores: ['dist/**', 'node_modules/**', 'playwright-report/**', 'test-results/**'] },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...vue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: ts.parser, extraFileExtensions: ['.vue'] } },
  },
  {
    files: ['**/*.{ts,vue}'],
    languageOptions: { globals: Object.fromEntries([
      'window', 'document', 'location', 'navigator', 'fetch', 'Headers', 'Request', 'Response',
      'AbortController', 'AbortSignal', 'DOMException', 'URL', 'URLSearchParams', 'HTMLElement',
      'HTMLInputElement', 'HTMLDialogElement', 'HTMLButtonElement', 'HTMLDivElement',
      'Event', 'KeyboardEvent', 'MouseEvent', 'FocusEvent', 'ResizeObserver',
      'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
      'cancelAnimationFrame', 'console', 'performance', 'process', 'global',
    ].map(name => [name, 'readonly'])) },
    rules: {
      'vue/no-v-html': 'error',
      'vue/multi-word-component-names': 'off',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
]
