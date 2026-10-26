// Read-only browser fixtures: no model requests or user workspace mutations are allowed.
// node web/verify-composer-menus.mjs [chatBaseUrl] .tmp/composer-menus
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = pw.default || pw
const base = process.env.CHAT_BASE_URL || process.argv[2] || 'http://127.0.0.1:5174'
const output = path.resolve(process.argv[3] || '.tmp/composer-menus')
await fs.mkdir(output, { recursive: true })
const source = await (await fetch(`${base}/src/views/ChatView.vue`)).text()
const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'Vite must be running')

const permissions = [
  { id: 'read-only', label: '只读', desc: '仅允许读取代码、搜索文件和分析项目；需要修改文件或执行命令时，会保留明确的操作边界。' },
  { id: 'workspace-write', label: '工作区写入', desc: '允许在当前工作区内修改代码和创建文件；访问工作区之外的目录或执行额外命令时需要审批。' },
  { id: 'full-access', label: '完全权限', desc: '允许直接修改文件并执行命令，操作不会逐项请求确认；请仅在信任项目与任务内容时使用此权限。' },
]
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
const page = await context.newPage()
page.setDefaultTimeout(10000)
const checks = [], errors = [], unexpected = [], mutations = [], nativeDialogs = [], reads = []
page.on('pageerror', error => errors.push(error.message))
page.on('dialog', async dialog => { nativeDialogs.push(dialog.type()); await dialog.dismiss() })

await context.route('**/api/**', async route => {
  const req = route.request(), p = new URL(req.url()).pathname, method = req.method()
  if (method !== 'GET') {
    mutations.push({ method, path: p })
    return route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ detail: 'This fixture does not allow mutations.' }) })
  }
  reads.push(p)
  let data
  if (p === '/api/v1/auth/me') data = { id: 'composer-fixture-user', role: 'admin' }
  else if (p === '/api/v1/sessions') data = []
  else if (p === '/api/v1/workspaces') data = []
  else if (p === '/api/v1/settings/model') data = { provider: 'offline', active_model: 'offline', model_default: 'offline' }
  else if (p === '/api/v1/model-profiles') data = []
  else if (p === '/api/v1/settings/permission') data = { mode: 'workspace-write', modes: permissions }
  else if (p === '/api/v1/web') data = { available: false, reason: '浏览器验收环境不发起联网请求' }
  else {
    unexpected.push(`${method} ${p}`)
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  }
  return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
})
await context.addInitScript(() => {
  window.__composerFixtureSockets = []
  const NativeSocket = window.WebSocket
  window.WebSocket = class extends NativeSocket {
    constructor(url, ...args) {
      if (new URL(String(url), location.href).pathname.includes('/api/')) {
        window.__composerFixtureSockets.push(new URL(String(url), location.href).pathname)
        throw new Error('Unexpected API WebSocket in composer fixture')
      }
      super(url, ...args)
    }
  }
})
await page.route('**/__composer-menu-test*', route => route.fulfill({
  contentType: 'text/html',
  body: `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module">import { createApp } from '${vuePath}'; import App from '/src/App.vue'; import '/src/style.css'; createApp(App).mount('#app');</script></body></html>`,
}))

const permissionButton = page.locator('.perm-btn')
const permissionMenu = page.locator('.perm-menu')
async function screenshot(name) { await page.screenshot({ path: path.join(output, name) }) }
async function focused(locator, message) {
  assert.equal(await locator.evaluate(element => element === document.activeElement), true, message)
}
async function menuGeometry(menu, modes, width, label) {
  const box = await menu.boundingBox()
  const height = page.viewportSize().height
  assert.ok(box && box.x >= -1 && box.y >= -1 && box.x + box.width <= width + 1 && box.y + box.height <= height + 1, `${label}: menu must stay inside ${width}×${height}: ${JSON.stringify(box)}`)
  assert.ok(box.width <= 209, `${label}: compact menu must be at most 208px wide: ${box.width}`)
  assert.ok(box.height <= 140, `${label}: compact menu must be at most 140px high: ${box.height}`)
  assert.equal(await menu.locator('.mode-description, .composer-menu-heading, .composer-menu-title, .composer-menu-subtitle, .composer-menu-note, .choice-badge, .perm-menu-title, .perm-menu-note').count(), 0, `${label}: descriptions, headings, badges and footnotes must not render`)
  const result = await menu.evaluate(element => {
    const items = [...element.querySelectorAll('[role="menuitemradio"]')]
    return {
      menuOverflow: element.scrollWidth > element.clientWidth + 1,
      items: items.map(item => ({ text: item.innerText.replace(/✓/g, '').trim(), height: item.getBoundingClientRect().height, overflow: item.scrollWidth > item.clientWidth + 1 })),
    }
  })
  assert.equal(result.menuOverflow, false, `${label}: menu has horizontal overflow`)
  assert.equal(result.items.length, modes.length)
  for (const [index, item] of result.items.entries()) {
    assert.equal(item.text, modes[index].label, `${label}: options should contain only their labels`)
    assert.ok(item.height >= 28 && item.height <= 42, `${label}: option height must remain compact: ${item.height}`)
    assert.equal(item.overflow, false, `${label}: option has horizontal overflow`)
  }
  const pageOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)
  assert.equal(pageOverflow, false, `${label}: document has horizontal overflow`)
  checks.push(`${label}: compact menu fits the viewport and shows only icons, names and selection marks`)
}
async function keyboardNavigation(menu, trigger, label) {
  const items = menu.getByRole('menuitemradio')
  const count = await items.count()
  await page.keyboard.press('Home'); await focused(items.first(), `${label}: Home focuses first item`)
  await page.keyboard.press('ArrowDown'); await focused(items.nth(1), `${label}: ArrowDown advances`)
  await page.keyboard.press('ArrowUp'); await focused(items.first(), `${label}: ArrowUp goes back`)
  await page.keyboard.press('End'); await focused(items.last(), `${label}: End focuses last item`)
  await page.keyboard.press('ArrowDown'); await focused(items.first(), `${label}: ArrowDown wraps`)
  await page.keyboard.press('ArrowUp'); await focused(items.nth(count - 1), `${label}: ArrowUp wraps`)
  await page.keyboard.press('Escape'); await menu.waitFor({ state: 'hidden' })
  await focused(trigger, `${label}: Escape restores trigger focus`)
  checks.push(`${label}: Home/End/arrows wrap correctly and Escape restores focus`)
}

let failure
try {
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: width === 1440 ? 960 : 844 })
    await page.goto(`${base}/__composer-menu-test?width=${width}`)
    await permissionButton.waitFor()
    assert.equal(await page.locator('.loop-btn, .loop-menu').count(), 0, 'removed execution modes have no composer entry')
    assert.equal(await permissionButton.getAttribute('aria-label'), '会话权限')
    // The fixture remains at the new-session composer; selecting permissions must never create a session.
    assert.equal(await page.locator('.sess-row').count(), 0)

    await permissionButton.click()
    await permissionMenu.getByRole('menuitemradio').nth(2).waitFor()
    assert.equal(await permissionMenu.getAttribute('role'), 'menu')
    assert.equal(await permissionMenu.getByRole('menuitemradio').nth(1).getAttribute('aria-checked'), 'true')
    assert.equal(await permissionMenu.locator('[aria-checked="true"]').count(), 1)
    await menuGeometry(permissionMenu, permissions, width, `Permissions ${width}`)
    await screenshot(`permissions-compact-${width}.png`)
    await keyboardNavigation(permissionMenu, permissionButton, `Permissions ${width}`)

    await permissionButton.click()
    await page.keyboard.press('End'); await page.keyboard.press('Enter')
    await permissionMenu.waitFor({ state: 'hidden' })
    assert.match(await permissionButton.innerText(), /完全权限/)
    await permissionButton.click()
    assert.equal(await permissionMenu.getByRole('menuitemradio').last().getAttribute('aria-checked'), 'true')
    assert.equal(await permissionMenu.locator('[aria-checked="true"]').count(), 1)
    checks.push(`Permissions ${width}: keyboard selection updates the checked item without a session write`)

    await page.keyboard.press('Escape')
    await permissionMenu.waitFor({ state: 'hidden' })
    checks.push(`Permissions ${width}: removed execution modes have no composer entry`)
    assert.deepEqual(await page.evaluate(() => window.__composerFixtureSockets), [])
    assert.deepEqual(mutations, [])
  }
  assert.deepEqual(mutations, [])
  assert.deepEqual(errors, [])
  assert.deepEqual(nativeDialogs, [])
  assert.deepEqual(unexpected, [])
  checks.push('All API traffic is read-only fixture data; no session creation, model calls, or native dialogs')
} catch (error) {
  failure = error.stack
  await screenshot('failure.png').catch(() => {})
} finally {
  await browser.close()
  const report = { passed: !failure, checks, errors, nativeDialogs, unexpected, mutations, reads, failure: failure || null }
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
}
if (failure) process.exitCode = 1
