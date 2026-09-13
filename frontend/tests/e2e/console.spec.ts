import { expect, test } from '@playwright/test'
import { mockMonitor } from '../fixtures/browser'

test('overview, backend-aware model filtering and read-only requests', async ({ page }) => {
  const state = await mockMonitor(page)
  await page.goto('/monitor.html')
  await expect(page.getByRole('button', { name: 'Qwen 3.8 · 27B', exact: true })).toBeVisible()
  await expect(page.locator('[data-model-key]')).toHaveCount(7)
  await page.getByLabel('按能力筛选').selectOption('embedding')
  await expect(page.locator('[data-model-key]')).toHaveCount(2)
  await page.getByLabel('按能力筛选').selectOption('all')
  await page.getByLabel('按后端筛选').selectOption('mlx_rerank')
  await expect(page.locator('[data-model-key]')).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Jina Reranker v2', exact: true })).toBeVisible()
  expect(state.details).toEqual([])
  expect(state.methods.every(method => method === 'GET')).toBe(true)
  expect(state.errors).toEqual([])
})

test('selected detail supports keyboard dismissal and explicit output loading', async ({ page }) => {
  const state = await mockMonitor(page)
  await page.goto('/monitor.html')
  const trigger = page.getByRole('button', { name: 'Qwen 3.8 · 27B', exact: true })
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: 'Qwen 3.8 · 27B' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('网关预留 token 预算', { exact: true })).toBeVisible()
  await expect(dialog.getByText('进程 RSS', { exact: true })).toBeVisible()
  expect(state.details.every(url => !url.includes('include_output=1'))).toBe(true)
  await dialog.getByLabel('显示高级输出').check()
  await expect(dialog.getByText('这是一段演示输出。', { exact: false })).toBeVisible()
  expect(state.details.some(url => url.includes('include_output=1'))).toBe(true)
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(trigger).toBeFocused()
  expect(state.errors).toEqual([])
})

test('unsupported service never pretends to provide llama metrics', async ({ page }) => {
  const state = await mockMonitor(page)
  await page.goto('/monitor.html')
  await page.getByRole('button', { name: 'Jina Embeddings v3', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('此服务提供基础监控')).toBeVisible()
  await expect(dialog.getByLabel('显示高级输出')).toHaveCount(0)
  expect(state.details.every(url => url.endsWith('/jina-embed'))).toBe(true)
})

test('credential flow preserves public overview and clears protected content', async ({ page }) => {
  const state = await mockMonitor(page, { authenticated: true })
  await page.goto('/monitor.html')
  await expect(page.getByText('连接你的模型服务', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: /设置访问凭据/ }).click()
  await page.getByLabel('API Key', { exact: true }).fill('fixture-key')
  await page.getByRole('button', { name: '应用凭据' }).click()
  await expect(page.getByRole('button', { name: 'Qwen 3.8 · 27B', exact: true })).toBeVisible()
  expect(await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))).not.toContain('fixture-key')
  expect(page.url()).not.toContain('fixture-key')
  await page.getByRole('button', { name: /访问设置/ }).click()
  await page.getByRole('button', { name: '清除凭据' }).click()
  await expect(page.locator('[data-model-key]')).toHaveCount(0)
  await expect(page.getByText('连接你的模型服务', { exact: true })).toBeVisible()
  expect(state.publicRequests).toBeGreaterThan(0)
  expect(state.errors).toEqual([])
})

test('older gateway shows upgrade guidance instead of an empty model list', async ({ page }) => {
  await mockMonitor(page, { unsupported: true })
  await page.goto('/monitor.html')
  await expect(page.getByText('网关需要升级', { exact: true })).toBeVisible()
  await expect(page.getByText('暂无模型', { exact: true })).toHaveCount(0)
})

test('dynamic model names render as text and long names do not overflow', async ({ page }) => {
  const state = await mockMonitor(page)
  const unsafe = '<img src=x onerror="window.monitorInjected=1"> 模型 \' " ' + '很长的名称'.repeat(10)
  state.snapshot.models[0]!.alias = unsafe
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('/monitor.html')
  await expect(page.getByRole('button', { name: unsafe, exact: true })).toBeVisible()
  expect(await page.evaluate(() => Reflect.get(window, 'monitorInjected'))).toBeUndefined()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(state.errors).toEqual([])
})

for (const width of [375, 768, 1440]) {
  test(`responsive layout and drawer at ${width}px`, async ({ page }) => {
    const state = await mockMonitor(page)
    await page.setViewportSize({ width, height: 960 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.goto('/monitor.html')
    await expect(page.getByRole('button', { name: 'Qwen 3.8 · 27B', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.getByRole('button', { name: 'Qwen 3.8 · 27B', exact: true }).click()
    await expect(page.getByRole('dialog')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.keyboard.press('Tab')
    expect(await page.getByRole('dialog').evaluate(dialog => dialog.contains(document.activeElement))).toBe(true)
    expect(state.errors).toEqual([])
  })
}

test('pause cancels polling and resume preserves the selected filters', async ({ page }) => {
  await page.clock.install()
  const state = await mockMonitor(page)
  await page.goto('/monitor.html')
  await expect(page.locator('[data-model-key]')).toHaveCount(7)
  await page.getByLabel('搜索模型').fill('Jina')
  await expect(page.locator('[data-model-key]')).toHaveCount(2)
  await page.getByRole('button', { name: '暂停刷新' }).click()
  const before = state.snapshots
  await page.clock.fastForward(30000)
  expect(state.snapshots).toBe(before)
  await page.getByRole('button', { name: '恢复刷新' }).click()
  await expect.poll(() => state.snapshots).toBeGreaterThan(before)
  await expect(page.getByLabel('搜索模型')).toHaveValue('Jina')
  await expect(page.locator('[data-model-key]')).toHaveCount(2)
})

test('failed refresh retains the catalog and marks the last readings stale', async ({ page }) => {
  const state = await mockMonitor(page)
  await page.goto('/monitor.html')
  await expect(page.locator('[data-model-key]')).toHaveCount(7)
  state.snapshotStatus = 503
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.getByText('部分数据已过期', { exact: true })).toBeVisible()
  await expect(page.locator('[data-model-key]')).toHaveCount(7)
  await expect(page.getByLabel('系统概览').getByText('35.2', { exact: false })).toBeVisible()
  expect(state.errors).toEqual([])
})

test('first collection failure shows missing readings instead of public zero values', async ({ page }) => {
  const state = await mockMonitor(page)
  state.snapshot.system = null
  state.snapshot.sources.system = { last_attempt_at: Date.now(), last_success_at: null, stale_after_ms: 15000, stale: true, error: { code: 'collection_failed', message: 'Unavailable' } }
  await page.goto('/monitor.html')
  const overview = page.getByLabel('系统概览')
  await expect(overview.getByText('未提供', { exact: true })).toHaveCount(3)
  await expect(page.locator('[data-model-key]')).toHaveCount(7)
  expect(state.errors).toEqual([])
})

test('polling preserves search focus and reading position', async ({ page }) => {
  await page.clock.install()
  const state = await mockMonitor(page)
  await page.setViewportSize({ width: 375, height: 800 })
  await page.goto('/monitor.html')
  const search = page.getByLabel('搜索模型')
  await search.fill('Qwen')
  await expect(page.locator('[data-model-key]')).toHaveCount(2)
  const before = await page.evaluate(() => scrollY)
  const requests = state.snapshots
  state.snapshot.generated_at += 5000
  await page.clock.fastForward(5001)
  await expect.poll(() => state.snapshots).toBeGreaterThan(requests)
  await expect(search).toBeFocused()
  await expect(search).toHaveValue('Qwen')
  expect(Math.abs(await page.evaluate(() => scrollY) - before)).toBeLessThan(3)
})

test('production build cannot enable fixture data with the demo query', async ({ page }) => {
  await mockMonitor(page, { authenticated: true })
  await page.goto('/monitor.html?demo=1')
  await expect(page.getByText('连接你的模型服务', { exact: true })).toBeVisible()
  await expect(page.locator('.demo-banner')).toHaveCount(0)
  await expect(page.locator('[data-model-key]')).toHaveCount(0)
})
