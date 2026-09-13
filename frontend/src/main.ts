import { createApp } from 'vue'
import App from './App.vue'

async function boot() {
  if (import.meta.env.DEV && new URLSearchParams(window.location.search).get('demo') === '1') {
    const { enableDemo } = await import('../tests/fixtures/demo')
    enableDemo()
    document.body.dataset.demo = 'true'
  }
  createApp(App).mount('#app')
}

void boot()
