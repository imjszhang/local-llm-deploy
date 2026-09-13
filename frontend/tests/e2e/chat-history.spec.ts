import { expect, test, type Page } from '@playwright/test'
import { mockMonitor } from '../fixtures/browser'
import { mockChatHistory } from '../fixtures/chat-history'

async function setup(page: Page, failList = false) {
  await mockMonitor(page)
  const state = await mockChatHistory(page)
  state.failList = failList
  let requests = 0
  await page.route('**/v1/chat/completions', route => route.fulfill({ json: {
    choices: [{ message: { content: `已保存的答案 ${++requests}`, reasoning_content: '用于保存测试的思考内容' }, finish_reason: 'stop' }], usage: { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20 },
  } }))
  await page.goto('/monitor.html#/chat')
  const workspace = page.getByRole('region', { name: '模型对话测试台' })
  await expect(workspace.getByRole('button', { name: '本机已连接', exact: true })).toBeVisible()
  await workspace.getByLabel('对话模型', { exact: true }).selectOption('qwen3.8-27b')
  return { workspace, state }
}
const saved = (page: Page) => expect(page.getByLabel('会话保存状态')).toContainText('已自动保存')

test('autosaves drafts, parameters, reasoning, answer versions and restores after reload', async ({ page }) => {
  const { workspace, state } = await setup(page)
  await workspace.getByRole('button', { name: '测试设置', exact: true }).click()
  await page.getByLabel('System Prompt', { exact: true }).fill('简短回答')
  await page.getByLabel('深度思考', { exact: true }).selectOption('on')
  await page.getByLabel('推理强度', { exact: true }).selectOption('medium')
  await page.getByLabel('思考 token 预算', { exact: true }).fill('48')
  await workspace.getByLabel('消息输入').fill('需要保存的问题')
  await workspace.getByRole('button', { name: '发送 ↑' }).click()
  await expect(workspace.getByText('已保存的答案 1', { exact: true })).toBeVisible()
  await workspace.getByRole('button', { name: '重新生成', exact: true }).click()
  await expect(workspace.getByText('已保存的答案 2', { exact: true })).toBeVisible()
  await workspace.getByLabel('消息输入').fill('还没发送的草稿')
  await saved(page)
  expect(state.documents.size).toBe(1)
  const stored = [...state.documents.values()][0]!.session
  expect(stored.turns[0]!.answers).toHaveLength(2)
  expect(stored.turns[0]!.answers[0]!.reasoning).toBe('用于保存测试的思考内容')
  expect(stored.turns[0]!.answers[0]!.request.parameters).toMatchObject({ thinking: true, reasoning_effort: 'medium', reasoning_budget_tokens: 48 })
  expect(JSON.stringify(stored)).not.toContain(state.token)
  await page.reload()
  await expect(workspace.getByLabel('消息输入')).toHaveValue('还没发送的草稿')
  await expect(workspace.getByText('已保存的答案 2', { exact: true })).toBeVisible()
  await expect(workspace.getByLabel('答案版本')).toHaveValue('1')
  await workspace.getByRole('button', { name: '测试设置', exact: true }).click()
  await expect(page.getByLabel('System Prompt', { exact: true })).toHaveValue('简短回答')
  await expect(page.getByLabel('推理强度', { exact: true })).toHaveValue('medium')
  await saved(page)
})

test('retrying initial history load never discards an unsaved local draft', async ({ page }) => {
  const { workspace, state } = await setup(page, true)
  await expect(page.getByLabel('会话保存状态')).toContainText('保存失败')
  await workspace.getByLabel('消息输入').fill('列表失败期间输入的草稿')
  state.failList = false
  await page.getByRole('button', { name: '重试保存', exact: true }).click()
  await expect(workspace.getByLabel('消息输入')).toHaveValue('列表失败期间输入的草稿')
  await saved(page)
  expect([...state.documents.values()].map(d => d.session.draft)).toContain('列表失败期间输入的草稿')
})

test('deleting a saved session persists across refresh', async ({ page }) => {
  const { workspace, state } = await setup(page)
  await workspace.getByLabel('消息输入').fill('稍后删除的会话')
  await saved(page)
  await workspace.getByRole('button', { name: '删除', exact: true }).click()
  await page.getByRole('button', { name: '删除会话', exact: true }).click()
  await expect.poll(() => state.documents.size).toBe(0)
  await page.reload()
  await expect(workspace.getByLabel('消息输入')).toHaveValue('')
  await expect(workspace.locator('.chat-session')).toHaveCount(1)
})

test('credential clear hides history without deleting it and reconnect restores it', async ({ page }) => {
  const { workspace, state } = await setup(page)
  await workspace.getByLabel('消息输入').fill('凭据清空也要保留')
  await saved(page)
  await workspace.getByRole('button', { name: '本机已连接', exact: true }).click()
  await page.getByRole('button', { name: '清除凭据', exact: true }).click()
  await expect(workspace.getByLabel('消息输入')).toHaveValue('')
  expect(state.documents.size).toBe(1)
  await workspace.getByRole('button', { name: '设置访问凭据', exact: true }).click()
  await page.getByRole('button', { name: '自动连接本机', exact: true }).click()
  await expect(workspace.getByLabel('消息输入')).toHaveValue('凭据清空也要保留')
  expect(state.documents.size).toBe(1)
})

test('failed saves preserve edits; retry persists them; conflicts can be saved as a new session', async ({ page }) => {
  const { workspace, state } = await setup(page)
  await workspace.getByLabel('消息输入').fill('初始草稿')
  await saved(page)
  state.failWrites = true
  await workspace.getByLabel('消息输入').fill('失败后保留的草稿')
  await expect(page.getByLabel('会话保存状态')).toContainText('保存失败')
  await expect(workspace.getByLabel('消息输入')).toHaveValue('失败后保留的草稿')
  state.failWrites = false
  await page.getByRole('button', { name: '重试保存', exact: true }).click()
  await saved(page)
  const other = [...state.documents.values()][0]!
  other.revision++
  other.session = { ...other.session, draft: '另一个窗口的版本' }
  await workspace.getByLabel('消息输入').fill('本窗口要另存的版本')
  await expect(page.getByLabel('会话保存状态')).toContainText('会话存在其他版本')
  await page.getByRole('button', { name: '保存为新会话', exact: true }).click()
  await saved(page)
  await expect.poll(() => state.documents.size).toBe(2)
  expect([...state.documents.values()].map(d => d.session.draft)).toContain('另一个窗口的版本')
  expect([...state.documents.values()].map(d => d.session.draft)).toContain('本窗口要另存的版本')
})
