import { expect, test } from '@playwright/test'
import { mockMonitor } from '../fixtures/browser'
import type { HubSnapshot } from '../../src/features/mcp/api/types'

const sample = (): HubSnapshot => ({
  config: { schema_version: 1, revision: 3, listen: { host: '0.0.0.0', port: 8899, public_url: '' }, allowed_origins: [], services: [{ id: 'comfyui', name: '局域网 ComfyUI', kind: 'comfyui', enabled: true, command: '/tools/comfy-mcp', args: [], env: {}, url: 'http://192.0.2.10:8188', allowed_tools: [] }] },
  runtime: { status: 'stopped', available: false, port: 8899 }, devices: [], tasks: [], connections: [{ service_id: 'comfyui', url: 'http://192.0.2.20:8899/mcp/comfyui', transport: 'streamable-http' }],
})

test('manage a stopped Hub, create a device and export a scoped connection', async ({ page }) => {
  const monitor = await mockMonitor(page)
  await page.route('**/console-api/v1/session', route => route.fulfill({ json: { token: 'local-console.preview', expires_at: Date.now() + 60000 } }))
  await page.route('**/mcp-hub/', route => route.fulfill({ path: 'dist/monitor.html', contentType: 'text/html' }))
  const snapshot = sample()
  await page.route('**/mcp-admin-api/v1/**', async route => {
    expect(route.request().headers().authorization).toBe('Bearer admin-fixture')
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/snapshot')) return route.fulfill({ json: snapshot })
    if (path.endsWith('/actions')) {
      expect(route.request().postDataJSON()).toEqual({ action: 'start' })
      snapshot.runtime = { ...snapshot.runtime, status: 'running', available: true, healthy: true, pid: 1234 }
      return route.fulfill({ status: 202, json: { operation: { id: 'operation-1', action: 'start', status: 'succeeded' } } })
    }
    if (path.endsWith('/devices')) {
      expect(route.request().postDataJSON()).toEqual({ name: 'video-remake', services: ['comfyui'], permissions: ['read', 'generate'] })
      const device = { id: 'device-1', name: 'video-remake', services: ['comfyui'], permissions: ['read', 'generate'], created_at: 1700000000 }
      snapshot.devices.push(device)
      return route.fulfill({ status: 201, json: { device, token: 'device-fixture-only' } })
    }
    return route.fulfill({ status: 404, json: { error: { message: 'Not found' } } })
  })
  await page.goto('/mcp-hub/')
  const workspace = page.getByRole('main', { name: 'MCP Hub 管理' })
  await expect(workspace.getByText('连接管理控制台')).toBeVisible()
  await workspace.getByLabel('管理员 API Key', { exact: true }).fill('admin-fixture')
  await workspace.getByRole('button', { name: '连接管理', exact: true }).click()
  await expect(workspace.getByText('已停止', { exact: true })).toBeVisible()
  await workspace.getByRole('button', { name: '启动 Hub', exact: true }).click()
  await expect(workspace.getByText('运行中', { exact: true })).toBeVisible()
  await workspace.getByRole('button', { name: '设备与连接', exact: true }).click()
  await workspace.getByPlaceholder('video-remake 或笔记本').fill('video-remake')
  await workspace.getByRole('checkbox', { name: '局域网 ComfyUI', exact: true }).check()
  await workspace.getByRole('button', { name: '创建设备令牌', exact: true }).click()
  await expect(workspace.getByLabel('设备访问令牌', { exact: true })).toHaveValue('device-fixture-only')
  await expect(workspace.getByLabel('MCP 客户端配置', { exact: true })).toContainText('device-fixture-only')
  await expect(workspace.getByLabel('MCP 客户端配置', { exact: true })).not.toContainText('admin-fixture')
  expect(await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))).not.toContain('device-fixture-only')
  await workspace.getByRole('button', { name: '已保存，关闭', exact: true }).click()
  await expect(workspace.getByLabel('MCP 客户端配置', { exact: true })).toContainText('<设备令牌>')
  await page.screenshot({ path: 'test-results/mcp-desktop.png', fullPage: true })
  expect(monitor.errors).toEqual([])
})

test('mobile Hub layout keeps controls inside the viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = await mockMonitor(page)
  await page.route('**/console-api/v1/session', route => route.fulfill({ status: 401, json: { error: {} } }))
  await page.route('**/mcp-admin-api/v1/snapshot', route => route.fulfill({ json: sample() }))
  await page.goto('/monitor.html#/mcp')
  const workspace = page.getByRole('main', { name: 'MCP Hub 管理' })
  await workspace.getByLabel('管理员 API Key', { exact: true }).fill('admin-fixture')
  await workspace.getByRole('button', { name: '连接管理', exact: true }).click()
  await expect(workspace.getByRole('heading', { name: '服务目录', exact: true })).toBeVisible()
  await workspace.getByRole('button', { name: '编辑', exact: true }).click()
  await expect(workspace.getByRole('heading', { name: '编辑服务', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.screenshot({ path: 'test-results/mcp-mobile.png', fullPage: true })
  expect(monitor.errors).toEqual([])
})
