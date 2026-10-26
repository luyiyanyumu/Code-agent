import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
// 在隔离的 offline 后端上验收，拒绝发送到真实模型服务。
// node verify-workspace.mjs <baseUrl> <screenshotsDir>
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const playwright = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:15175'
const chatBase = process.env.CHAT_BASE_URL || 'http://127.0.0.1:5174'
const out = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/ui-after', import.meta.url)))
const project = fileURLToPath(new URL('../.tmp/ui-demo-project', import.meta.url))
fs.mkdirSync(out, { recursive: true })
fs.mkdirSync(project, { recursive: true })
fs.writeFileSync(`${project}/demo.py`, 'def greet(name):\n    return f"Hello, {name}"\n')
for (const target of new Set([base, chatBase])) {
  const settings = await (await fetch(`${target}/api/v1/settings/model`)).json()
  assert.equal(settings.provider, 'offline', `browser verification must use offline backend: ${target}`)
}
const browser = await playwright.chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const issues = []
const failed = []
const checks = []
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  page.on('pageerror', e => issues.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) failed.push(`${r.status()} ${r.url()}`) })
  const requests = []
  page.on('request', r => requests.push(r.url()))
  const nav = async label => {
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: label === '工具' ? '设置' : label, exact: true }).click()
    if (label === '工具') {
      await page.locator('.settings-nav-item').filter({ hasText: '工具与扩展' }).click()
      await page.locator('.tools-page').waitFor()
    }
  }
  const bounded = async label => {
    const size = await page.evaluate(() => ({ viewport: innerWidth, scroll: document.documentElement.scrollWidth }))
    assert.ok(size.scroll <= size.viewport + 2, `${label}: page horizontal overflow ${JSON.stringify(size)}`)
    checks.push(label)
  }
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.locator('.shell').waitFor()
  assert.equal(await page.locator('input[type="password"]').count(), 0)
  assert.equal(requests.some(url => url.includes('/auth/login')), false)
  assert.equal(await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '对话', exact: true }).count(), 0)
  const initiallyCompact = await page.locator('.shell.nav-collapsed').count()
  await page.locator('.collapse-btn').click()
  assert.equal(await page.locator('.shell.nav-collapsed').count(), initiallyCompact ? 0 : 1)
  await page.locator('.collapse-btn').click()
  await page.goto(chatBase, { waitUntil: 'networkidle' })
  await page.locator('.chat').waitFor()
  await page.locator('.welcome-state').waitFor()
  assert.equal(await page.locator('.session-close').isVisible(), false)
  await page.screenshot({ path: `${out}/chat-desktop.png` })
  await page.locator('.welcome-starters button').first().click()
  await page.locator('.ta:not(:disabled)').waitFor()
  await page.waitForFunction(() => Boolean(document.querySelector('.ta')?.value.trim()))
  await page.locator('.connection-status.connected').waitFor()
  await page.locator('.ta').fill('你好，请回复一条测试消息。')
  await page.locator('.send-btn').click()
  await page.locator('.md-body').first().waitFor({ timeout: 20000 })
  await page.waitForFunction(() => !document.querySelector('.send-btn.stop'))
  await bounded('desktop chat send')
  await page.screenshot({ path: `${out}/chat-conversation.png` })
  await page.goto(base, { waitUntil: 'networkidle' })
  for (const label of ['记忆', '知识库', '工具', '用量', '自动化', '审计', '设置']) {
    await nav(label)
    await page.locator('.content > .page').waitFor()
    await page.waitForTimeout(120)
    await bounded(`desktop ${label}`)
  }
  await nav('代码')
  await page.locator('.code').waitFor()
  await page.getByRole('button', { name: '切换项目', exact: true }).click()
  await page.getByRole('button', { name: '输入工作区路径…', exact: true }).click()
  await page.getByRole('textbox', { name: '工作区路径', exact: true }).fill(project)
  await page.locator('.workspace-entry .path-form button[type="submit"]').click()
  await page.locator('.tree .row').filter({ hasText: 'demo.py' }).click()
  try {
    await page.locator('.monaco-editor').waitFor({ timeout: 30000 })
  } catch (e) {
    console.log(JSON.stringify({ body: (await page.locator('body').innerText()).slice(-5000), issues, failed }, null, 2))
    await page.screenshot({ path: `${out}/code-failure.png` })
    throw e
  }
  await bounded('desktop code editor')
  await page.screenshot({ path: `${out}/code-desktop.png` })
  await page.setViewportSize({ width: 390, height: 844 })
  await bounded('mobile code editor')
  await page.screenshot({ path: `${out}/code-mobile.png` })
  await page.locator('.workspace-switcher button').filter({ hasText: '项目与文件' }).click()
  assert.equal(await page.locator('.code .left').isVisible(), true)
  await page.locator('.workspace-switcher button').filter({ hasText: 'AI 助手' }).click()
  assert.equal(await page.locator('.code .right').isVisible(), true)
  await bounded('mobile code assistant')
  await page.goto(chatBase, { waitUntil: 'networkidle' })
  await page.locator('.welcome-state').waitFor()
  await page.screenshot({ path: `${out}/chat-mobile.png` })
  await page.locator('.history-toggle').click()
  await page.locator('.session-list.mobile-open').waitFor()
  await page.locator('.sess-main').first().click()
  assert.equal(await page.locator('.session-list').isVisible(), false)
  await page.locator('.connection-status.connected').waitFor()
  await bounded('mobile history and conversation')
  await page.screenshot({ path: `${out}/chat-mobile-conversation.png` })
  await page.goto(base, { waitUntil: 'networkidle' })
  for (const label of ['记忆', '知识库', '工具', '用量', '自动化', '审计', '设置']) {
    await nav(label)
    await page.locator('.content > .page').waitFor()
    await page.waitForTimeout(120)
    await bounded(`mobile ${label}`)
  }
  await page.evaluate(() => localStorage.setItem('code-agent-token', 'expired-token'))
  await page.reload({ waitUntil: 'networkidle' })
  await page.locator('.shell').waitFor()
  checks.push('stale token boot')
  assert.deepEqual(issues, [], 'browser runtime errors')
  assert.deepEqual(failed, [], 'API requests failed')
  console.log(JSON.stringify({ checks, runtimeErrors: issues, failedRequests: failed }, null, 2))
} finally {
  await browser.close()
}
