import { expect, test, type Locator, type Page } from '@playwright/test'
import { mockMonitor } from '../fixtures/browser'
import type { Session } from '../../src/features/chat/domain/types'

async function setup(page: Page, ollama = false) {
  const state = await mockMonitor(page)
  if (ollama) {
    const model = state.snapshot.models.find(item => item.key === 'qwen3.8-27b')!
    model.backend = 'ollama'
    model.backend_model = 'qwen3.8:27b-mlx'
    model.chat_controls = { thinking: true, reasoning_efforts: ['low', 'medium', 'high'], reasoning_budget: false, default_thinking: true, default_effort: null, source: 'configured' }
  }
  const requests: Record<string, unknown>[] = []
  await page.route('**/v1/chat/completions', async route => {
    requests.push(route.request().postDataJSON())
    await route.fulfill({ contentType: 'text/event-stream', body: `data: ${JSON.stringify({ choices: [{ delta: { content: `回答 ${requests.length}` }, finish_reason: 'stop' }] })}\n\ndata: [DONE]\n\n` })
  })
  await page.goto('/monitor.html#/chat')
  const workspace = page.getByRole('region', { name: '模型对话测试台' })
  await expect(workspace.locator('#chat-model option[value="qwen3.8-27b"]')).toHaveCount(1)
  await workspace.getByLabel('对话模型', { exact: true }).selectOption('qwen3.8-27b')
  await workspace.getByRole('button', { name: '测试设置', exact: true }).click()
  return { workspace, requests, state }
}

async function send(workspace: Locator, number: number) {
  await workspace.getByLabel('消息输入').fill(`问题 ${number}`)
  await workspace.getByRole('button', { name: '发送 ↑' }).click()
  await expect(workspace.getByText(`回答 ${number}`, { exact: true })).toBeVisible()
}

test('llama thinking choices map to explicit template controls and omit overrides for default', async ({ page }) => {
  const { workspace, requests } = await setup(page)
  await expect(workspace.getByText('后端默认关闭。修改从下一次请求生效。')).toBeVisible()
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('on')
  await workspace.getByLabel('推理强度', { exact: true }).selectOption('xhigh')
  await workspace.getByLabel('思考 token 预算', { exact: true }).fill('4096')
  await workspace.getByLabel('最大输出 token', { exact: true }).fill('8192')
  await send(workspace, 1)
  expect(requests[0]).toMatchObject({ chat_template_kwargs: { enable_thinking: true }, reasoning_format: 'deepseek', reasoning_effort: 'xhigh', reasoning_budget_tokens: 4096, max_tokens: 8192 })
  expect(requests[0]).not.toHaveProperty('thinking')
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('off')
  await send(workspace, 2)
  expect(requests[1]).toMatchObject({ chat_template_kwargs: { enable_thinking: false }, max_tokens: 8192 })
  for (const key of ['reasoning_effort', 'reasoning_budget_tokens', 'reasoning_format']) expect(requests[1]).not.toHaveProperty(key)
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('default')
  await send(workspace, 3)
  for (const key of ['thinking', 'chat_template_kwargs', 'reasoning_effort', 'reasoning_budget_tokens']) expect(requests[2]).not.toHaveProperty(key)
  expect(requests[2]?.reasoning_format).toBe('deepseek')
})

test('Ollama exposes only its declared levels and maps default on to medium, off to none', async ({ page }) => {
  const { workspace, requests } = await setup(page, true)
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('on')
  await expect(workspace.getByLabel('推理强度', { exact: true }).locator('option')).toHaveText(['默认（中）', '低', '中', '高'])
  await expect(workspace.getByText('显式开启且未选择强度时，使用中档。', { exact: false })).toBeVisible()
  await expect(workspace.getByLabel('思考 token 预算', { exact: true })).toHaveCount(0)
  await send(workspace, 1)
  expect(requests[0]?.reasoning_effort).toBe('medium')
  await workspace.getByLabel('推理强度', { exact: true }).selectOption('high')
  await send(workspace, 2)
  expect(requests[1]?.reasoning_effort).toBe('high')
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('off')
  await send(workspace, 3)
  expect(requests[2]?.reasoning_effort).toBe('none')
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('default')
  await send(workspace, 4)
  expect(requests[3]).not.toHaveProperty('reasoning_effort')
  for (const request of requests) {
    for (const key of ['think', 'thinking', 'chat_template_kwargs', 'reasoning_budget_tokens']) expect(request).not.toHaveProperty(key)
  }
})

test('request snapshots and JSON export preserve the selected thinking settings after edits', async ({ page }) => {
  const { workspace } = await setup(page)
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('on')
  await workspace.getByLabel('推理强度', { exact: true }).selectOption('xhigh')
  await workspace.getByLabel('思考 token 预算', { exact: true }).fill('2048')
  await send(workspace, 1)
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('off')
  await workspace.locator('.chat-request summary').click()
  const snapshot = workspace.locator('.chat-request pre')
  await expect(snapshot).toContainText('"thinking": true')
  await expect(snapshot).toContainText('"reasoning_effort": "xhigh"')
  await expect(snapshot).toContainText('"reasoning_budget_tokens": 2048')
  const downloadPromise = page.waitForEvent('download')
  await workspace.getByRole('button', { name: '导出 JSON' }).click()
  const stream = await (await downloadPromise).createReadStream()
  let text = ''
  for await (const chunk of stream!) text += chunk.toString()
  const exported = JSON.parse(text) as { session: Session }
  expect(exported.session.parameters).toEqual({ thinking: false })
  expect(exported.session.turns[0]?.answers[0]?.request.parameters).toEqual({ thinking: true, reasoning_effort: 'xhigh', reasoning_budget_tokens: 2048 })
  expect(exported.session.turns[0]?.answers[0]?.request.chat_controls?.reasoning_efforts).toEqual(['low', 'medium', 'xhigh'])
})

test('carrying context to a different model resets thinking settings and toggle-only models expose no levels', async ({ page }) => {
  const { workspace, requests } = await setup(page)
  await workspace.getByLabel('Temperature', { exact: true }).fill('0.5')
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('on')
  await workspace.getByLabel('推理强度', { exact: true }).selectOption('xhigh')
  await workspace.getByLabel('思考 token 预算', { exact: true }).fill('2048')
  await send(workspace, 1)
  await workspace.getByLabel('对话模型', { exact: true }).selectOption('qwen3-8b')
  await page.getByRole('dialog', { name: '切换对话模型' }).getByRole('button', { name: '携带上下文切换' }).click()
  await expect(workspace.getByLabel('深度思考', { exact: true })).toHaveValue('default')
  await expect(workspace.getByLabel('Temperature', { exact: true })).toHaveValue('0.5')
  await send(workspace, 2)
  expect(requests[1]).toMatchObject({ model: 'qwen3-8b', temperature: 0.5, messages: [{ role: 'user', content: '问题 1' }, { role: 'assistant', content: '回答 1' }, { role: 'user', content: '问题 2' }] })
  expect(requests[1]).not.toHaveProperty('reasoning_effort')
  expect(requests[1]).not.toHaveProperty('reasoning_budget_tokens')
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('on')
  await expect(workspace.getByText('此模型未声明支持分级推理强度。')).toBeVisible()
  await expect(workspace.getByLabel('推理强度', { exact: true })).toHaveCount(0)
  await expect(workspace.getByLabel('思考 token 预算', { exact: true })).toHaveCount(0)
  await send(workspace, 3)
  expect(requests[2]?.reasoning_effort).toBe('medium')
})

test('invalid thinking budgets preserve the draft and never dispatch inference', async ({ page }) => {
  const { workspace, requests } = await setup(page)
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('on')
  await workspace.getByLabel('消息输入').fill('保留思考测试草稿')
  for (const value of ['0', '131073', '1.5']) {
    await workspace.getByLabel('思考 token 预算', { exact: true }).fill(value)
    await workspace.getByRole('button', { name: '发送 ↑' }).click()
    await expect(workspace.getByRole('alert')).toContainText('思考预算必须为 1–131072 的整数')
    await expect(workspace.getByLabel('消息输入')).toHaveValue('保留思考测试草稿')
    expect(requests).toHaveLength(0)
  }
  await workspace.getByLabel('思考 token 预算', { exact: true }).fill('2048')
  await workspace.getByLabel('最大输出 token', { exact: true }).fill('2048')
  await workspace.getByRole('button', { name: '发送 ↑' }).click()
  await expect(workspace.getByRole('alert')).toContainText('思考预算需小于最大输出 token')
  await expect(workspace.getByLabel('消息输入')).toHaveValue('保留思考测试草稿')
  expect(requests).toHaveLength(0)
})

test('thinking controls remain usable in the mobile settings drawer', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 950 })
  const { workspace, requests } = await setup(page)
  const dialog = page.getByRole('dialog', { name: '对话参数' })
  await dialog.getByLabel('深度思考', { exact: true }).selectOption('on')
  await dialog.getByLabel('推理强度', { exact: true }).selectOption('low')
  await dialog.getByLabel('思考 token 预算', { exact: true }).fill('1024')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await send(workspace, 1)
  expect(requests[0]).toMatchObject({ reasoning_effort: 'low', reasoning_budget_tokens: 1024 })
})

test('withdrawn thinking capabilities block stale overrides until only those settings are reset', async ({ page }) => {
  const { workspace, requests, state } = await setup(page)
  await workspace.getByLabel('Temperature', { exact: true }).fill('0.5')
  await workspace.getByLabel('深度思考', { exact: true }).selectOption('on')
  await workspace.getByLabel('推理强度', { exact: true }).selectOption('xhigh')
  await workspace.getByLabel('思考 token 预算', { exact: true }).fill('2048')
  state.snapshot.models.find(model => model.key === 'qwen3.8-27b')!.chat_controls = null
  await workspace.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(workspace.getByLabel('深度思考', { exact: true })).toHaveCount(0)
  await workspace.getByLabel('消息输入').fill('保留草稿和采样参数')
  await workspace.getByRole('button', { name: '发送 ↑' }).click()
  await expect(workspace.getByRole('alert')).toContainText('尚未声明可用的思考控制')
  expect(requests).toHaveLength(0)
  await expect(workspace.getByLabel('消息输入')).toHaveValue('保留草稿和采样参数')
  await workspace.getByRole('button', { name: '重置思考设置', exact: true }).click()
  await expect(workspace.getByLabel('Temperature', { exact: true })).toHaveValue('0.5')
  await workspace.getByRole('button', { name: '发送 ↑' }).click()
  await expect(workspace.getByText('回答 1', { exact: true })).toBeVisible()
  expect(requests[0]).toMatchObject({ temperature: 0.5, messages: [{ role: 'user', content: '保留草稿和采样参数' }] })
  for (const key of ['thinking', 'chat_template_kwargs', 'reasoning_effort', 'reasoning_budget_tokens']) expect(requests[0]).not.toHaveProperty(key)
})
