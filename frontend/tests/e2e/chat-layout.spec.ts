import { expect, test } from '@playwright/test'
import { mockMonitor } from '../fixtures/browser'
import { mockChatHistory } from '../fixtures/chat-history'

for (const width of [375, 768, 1440]) {
  test(`conversation layout is usable at ${width}px with quiet autosave`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await mockMonitor(page)
    await mockChatHistory(page)
    await page.route('**/v1/chat/completions', route => route.fulfill({ json: {
      choices: [{ message: { content: '当然可以。\n\n### 一个简单的开始\n\n把复杂的问题拆成小步骤，逐一验证。\n\n- 明确目标\n- 编写实现\n- 检查结果' }, finish_reason: 'stop' }],
    } }))
    await page.goto('/monitor.html#/chat')
    await expect(page.getByRole('button', { name: '本机已连接', exact: true })).toBeVisible()
    await page.getByLabel('对话模型', { exact: true }).selectOption('qwen3.8-27b')
    await expect(page.getByRole('heading', { name: '今天想聊些什么？' })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('empty.png') })
    await page.getByLabel('消息输入').fill('帮我规划一个简单的学习计划')
    await expect(page.getByLabel('会话保存状态')).toHaveAttribute('data-save-state', 'saved')
    await expect(page.getByLabel('会话保存状态')).toBeHidden()
    await page.getByRole('button', { name: '发送 ↑' }).click()
    await expect(page.locator('.chat-markdown')).toContainText('把复杂的问题拆成小步骤')
    await expect(page.getByLabel('会话保存状态')).toBeHidden()
    await expect(page.locator('.chat-stats')).toBeHidden()
    await page.getByText('生成详情', { exact: true }).click()
    await expect(page.locator('.chat-stats')).toBeVisible()
    await page.getByText('生成详情', { exact: true }).click()
    await page.getByLabel('会话操作', { exact: true }).click()
    await page.getByRole('button', { name: '重命名', exact: true }).click()
    await page.getByLabel('会话名称', { exact: true }).fill('学习计划')
    await page.getByRole('button', { name: '保存', exact: true }).click()
    await expect(page.locator('.chat-session-toolbar')).toContainText('学习计划')
    if (width > 700) {
      await page.getByLabel('搜索对话', { exact: true }).fill('没有这样的对话')
      await expect(page.getByText('没有匹配的对话', { exact: true })).toBeVisible()
      await page.getByLabel('搜索对话', { exact: true }).fill('')
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const composer = await page.getByLabel('消息输入').boundingBox()
    expect(composer!.y + composer!.height).toBeLessThan(900)
    await page.screenshot({ path: testInfo.outputPath('conversation.png') })
  })
}
