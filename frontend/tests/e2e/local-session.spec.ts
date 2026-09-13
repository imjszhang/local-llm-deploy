import { expect, test } from '@playwright/test'
import { mockMonitor } from '../fixtures/browser'

test('localhost connects automatically, refresh renews session, clear stays disconnected', async ({ page }) => {
  const state = await mockMonitor(page)
  let token = '', issued = 0
  await page.route('**/console-api/v1/session', route => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers()['x-local-console']).toBe('1')
    token = `console-session-fixture-${++issued}`
    return route.fulfill({ json: { token, expires_at: Date.now() + 60000 }, headers: { 'Cache-Control': 'no-store' } })
  })
  await page.route('**/monitor-api/v1/snapshot', route => {
    const headers = route.request().headers()
    if (headers.authorization !== `Bearer ${token}`) return route.fulfill({ status: 401, json: { error: {} } })
    expect(headers['x-local-console']).toBe('1')
    return route.fulfill({ json: state.snapshot })
  })
  await page.route('**/v1/chat/completions', route => {
    expect(route.request().headers().authorization).toBe(`Bearer ${token}`)
    expect(route.request().headers()['x-local-console']).toBe('1')
    return route.fulfill({ json: { choices: [{ message: { content: '自动连接成功' }, finish_reason: 'stop' }] } })
  })
  await page.goto('/monitor.html#/chat')
  const workspace = page.getByRole('region', { name: '模型对话测试台' })
  await expect(workspace.getByRole('button', { name: '本机已连接', exact: true })).toBeVisible()
  await workspace.getByLabel('对话模型', { exact: true }).selectOption('qwen3.8-27b')
  await workspace.getByLabel('消息输入').fill('无需手动复制 Key')
  await workspace.getByRole('button', { name: '发送 ↑' }).click()
  await expect(workspace.getByText('自动连接成功', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))).not.toContain(token)
  await page.reload()
  await expect(workspace.getByRole('button', { name: '本机已连接', exact: true })).toBeVisible()
  expect(issued).toBe(2)
  await workspace.getByRole('button', { name: '本机已连接', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('根目录中的 Key 不会传给浏览器')
  await page.getByRole('button', { name: '清除凭据', exact: true }).click()
  await expect(workspace.getByRole('button', { name: '设置访问凭据', exact: true })).toBeVisible()
  expect(issued).toBe(2)
  await workspace.getByRole('button', { name: '设置访问凭据', exact: true }).click()
  await page.getByRole('button', { name: '自动连接本机', exact: true }).click()
  await expect(workspace.getByRole('button', { name: '本机已连接', exact: true })).toBeVisible()
  expect(issued).toBe(3)
})

test('a delayed automatic connection never overwrites a manual credential choice', async ({ page }) => {
  await mockMonitor(page, { authenticated: true })
  let finish!: () => void
  const gate = new Promise<void>(resolve => { finish = resolve })
  await page.route('**/console-api/v1/session', async route => {
    await gate
    await route.fulfill({ json: { token: 'late-console-token', expires_at: Date.now() + 60000 } })
  })
  await page.goto('/monitor.html#/chat')
  const workspace = page.getByRole('region', { name: '模型对话测试台' })
  await workspace.getByRole('button', { name: '正在连接本机…', exact: true }).click()
  await page.getByLabel('API Key', { exact: true }).fill('fixture-key')
  await page.getByRole('button', { name: '应用凭据', exact: true }).click()
  finish()
  await expect(workspace.getByRole('button', { name: '访问设置 · 已设置', exact: true })).toBeVisible()
  await expect(workspace.locator('#chat-model option[value="qwen3.8-27b"]')).toHaveCount(1)
})
