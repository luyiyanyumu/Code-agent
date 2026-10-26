// Full application visual QA against a disposable offline backend.
// node web/verify-design.mjs http://127.0.0.1:15175 .tmp/ui-redesign/screens
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:15175'
const chatBase = process.env.CHAT_BASE_URL || 'http://127.0.0.1:5174'
const output = path.resolve(process.argv[3] || '../.tmp/ui-redesign/screens')
for (const target of new Set([base, chatBase])) {
  const model = await (await fetch(`${target}/api/v1/settings/model`)).json()
  assert.equal(model.provider, 'offline', `Visual QA requires a disposable offline backend: ${target}`)
}
fs.mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const issues = [], failures = [], checks = [], screenshots = []
const names = { chat: '对话', code: '代码', memory: '记忆', knowledge: '知识库', tools: '工具', usage: '用量', automations: '自动化', audit: '审计', settings: '设置' }
let page, failure
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
  await context.addInitScript(() => localStorage.setItem('code-agent:view', 'code'))
  page = await context.newPage()
  page.setDefaultTimeout(15000)
  page.on('pageerror', error => issues.push(error.message))
  page.on('response', response => { if (response.url().includes('/api/') && response.status() >= 400) failures.push(`${response.status()} ${new URL(response.url()).pathname}`) })
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.locator('.shell').waitFor()
  async function show(key) {
    if (key === 'chat') {
      await page.goto(chatBase, { waitUntil: 'networkidle' })
      await page.locator('.chat').waitFor()
    } else {
      if (new URL(page.url()).origin !== new URL(base).origin) await page.goto(base, { waitUntil: 'networkidle' })
      await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: names[key === 'tools' ? 'settings' : key], exact: true }).click()
      await page.locator(`.content > ${key === 'code' ? '.code' : '.page'}`).waitFor()
      if (key === 'tools') {
        await page.locator('.settings-nav-item').filter({ hasText: '工具与扩展' }).click()
        await page.locator('.tools-page').waitFor()
      }
    }
    if (key === 'code') await page.locator('.project-switcher-trigger:not(:disabled)').waitFor({ state: 'attached' })
    await page.waitForLoadState('networkidle')
    await page.evaluate(() => document.fonts.ready)
  }
  async function shot(key, width) {
    const dimensions = await page.evaluate(() => ({ viewport: innerWidth, doc: document.documentElement.scrollWidth }))
    assert.ok(dimensions.doc <= dimensions.viewport + 1, `${key} @ ${width} horizontal overflow ${JSON.stringify(dimensions)}`)
    const heading = ['chat', 'code', 'tools'].includes(key) ? names[key] : await page.locator('.page-header h1, .settings-header h1').innerText()
    assert.ok(heading.trim())
    const file = `${key}-${width}.png`
    await page.screenshot({ path: path.join(output, file) })
    screenshots.push(file); checks.push(`${names[key]}: ${width}px layout and navigation`)
  }
  for (const [width, height] of [[1440, 960], [390, 844]]) {
    await page.setViewportSize({ width, height })
    for (const key of Object.keys(names)) { await show(key); await shot(key, width) }
  }
  await page.setViewportSize({ width: 1440, height: 960 })
  await show('chat')
  await page.keyboard.press('Tab')
  // Hover-only row controls also expose themselves to keyboard users.
  if (await page.locator('.sess-main').count()) {
    await page.locator('.sess-main').first().focus()
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.sess-row .si-more')).opacity === '1')
    const opacity = await page.locator('.sess-row').first().locator('.si-more').evaluate(node => getComputedStyle(node).opacity)
    assert.equal(opacity, '1')
    checks.push('Conversation row controls are visible on keyboard focus')
  }
  await page.setViewportSize({ width: 1024, height: 768 })
  await show('settings'); await shot('settings', 1024)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const motion = await page.locator('.nav-item').first().evaluate(node => getComputedStyle(node).transitionDuration)
  assert.equal(motion, '0s'); checks.push('Reduced-motion preference honored')
  assert.deepEqual(issues, []); assert.deepEqual(failures, [])
} catch (error) {
  failure = error.stack
  if (page) await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {})
} finally {
  await browser.close()
  const report = { passed: !failure, checks, screenshots, issues, failures, failure: failure || null }
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
}
if (failure) process.exitCode = 1
