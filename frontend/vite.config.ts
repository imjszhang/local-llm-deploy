import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: {
    outDir: 'dist',
    assetsDir: 'monitor-assets',
    sourcemap: false,
    rolldownOptions: { input: 'monitor.html' },
  },
  server: {
    proxy: Object.fromEntries(['/monitor-api', '/api', '/v1', '/knowledge'].map(path => [path, {
      target: 'http://127.0.0.1:8888', changeOrigin: false,
    }])),
  },
  test: {
    environment: 'jsdom',
    include: ['tests/unit/**/*.test.ts', 'tests/components/**/*.test.ts'],
    restoreMocks: true,
  },
})
