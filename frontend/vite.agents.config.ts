import { defineConfig } from 'vite'

export default defineConfig({
  base: '/',
  build: {
    outDir: 'dist-agents',
    assetsDir: 'agent-assets',
    sourcemap: false,
    rolldownOptions: { input: 'agents.html' },
  },
  server: {
    proxy: {
      '/agents-api': {
        target: 'http://127.0.0.1:8888',
        changeOrigin: false,
      },
    },
  },
})
