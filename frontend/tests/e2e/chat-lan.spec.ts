import { expect, test } from '@playwright/test'
import { mockMonitor } from '../fixtures/browser'
import { mockChatHistory } from '../fixtures/chat-history'

test('plain HTTP LAN can create sessions, send, save and restore without randomUUID', async ({ page }) => {
  // Keep a genuinely non-trustworthy browser origin; only transport static assets locally.
  const origin = 'http://192.0.2.1:4174'
  await page.route(`${origin}/**`, async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/monitor.html' || path.startsWith('/monitor-assets/')) {
      const response = await route.fetch({ url: `http://127.0.0.1:4174${path}` })
      return route.fulfill({ response })
    }
    return route.fulfill({ status: 404, json: { error: {} } })
  })
  const monitor = await mockMonitor(page, { authenticated: true })
  const history = await mockChatHistory(page)
  history.token = 'fixture-key'
  const errors: string[] = []
  page.on('console', entry => { if (entry.type() === 'error' && entry.text().includes('randomUUID')) errors.push(entry.text()) })
  await page.route('**/v1/chat/completions', route => {
    expect(route.request().headers().authorization).toBe('Bearer fixture-key')
    return route.fulfill({ json: { choices: [{ message: { content: '局域网对话成功' }, finish_reason: 'stop' }] } })
  })
  await page.goto(`${origin}/monitor.html#/chat`)
  expect(await page.evaluate(() => ({ secure: isSecureContext, uuid: typeof crypto.randomUUID }))).toEqual({ secure: false, uuid: 'undefined' })
  const workspace = page.getByRole('region', { name: '模型对话测试台' })
  await workspace.locator('.chat-topbar').getByRole('button', { name: '新建对话', exact: true }).click()
  await expect(workspace.getByLabel('消息输入')).toBeVisible({ timeout: 3000 })
  await workspace.getByRole('button', { name: '设置访问凭据', exact: true }).click()
  await page.getByLabel('API Key', { exact: true }).fill('fixture-key')
  await page.getByRole('button', { name: '应用凭据', exact: true }).click()
  await expect(workspace.locator('#chat-model option[value="qwen3.8-27b"]')).toHaveCount(1)
  await workspace.getByLabel('对话模型', { exact: true }).selectOption('qwen3.8-27b')
  const before = await workspace.locator('.chat-session').count()
  await workspace.getByRole('button', { name: '＋ 新建对话', exact: true }).click()
  await expect(workspace.locator('.chat-session')).toHaveCount(before + 1)
  await workspace.getByLabel('消息输入').fill('局域网测试')
  await workspace.getByRole('button', { name: '发送 ↑' }).click()
  await expect(workspace.getByText('局域网对话成功', { exact: true })).toBeVisible()
  await expect(workspace.getByLabel('会话保存状态')).toHaveAttribute('data-save-state', 'saved')
  const saved = [...history.documents.values()].find(item => item.session.turns.length)!.session
  const ids = [saved.id, saved.turns[0]!.id, saved.turns[0]!.answers[0]!.id]
  expect(new Set(ids).size).toBe(3)
  for (const id of ids) expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  await page.reload()
  await expect(workspace.getByText('局域网对话成功', { exact: true })).toBeVisible()
  expect(history.issued).toBe(0)
  expect(monitor.errors).toEqual([])
  expect(errors).toEqual([])
})
