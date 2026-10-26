// Offline UI fixtures only: every API request is intercepted before it can reach a real service.
// node web/verify-model-settings.mjs http://127.0.0.1:5175 .tmp/model-settings
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:5175'
const output = path.resolve(process.argv[3] || '.tmp/model-settings')
await fs.mkdir(output, { recursive: true })
const source = await (await fetch(`${base}/src/views/SettingsView.vue`)).text()
const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'Vite must be running')

let cfg = {
  provider: 'deepseek', base_url: 'https://api.deepseek.com', model_default: 'deepseek-chat',
  active_model: 'deepseek-chat', proxy_mode: 'direct', api_key_set: true, api_key_hint: 'fixture-only',
  context_window: 131072, max_output_tokens: 8192,
  effective_context_window: 131072, effective_max_output_tokens: 8192,
}
let profiles = [{
  id: 'profile-a', name: 'DeepSeek 测试连接', provider: 'deepseek',
  base_url: 'https://api.deepseek.com', model_default: 'deepseek-chat', proxy_mode: 'direct',
  api_key_set: true, api_key_hint: 'fixture-only', active: true, local: false,
  context_window: 131072, max_output_tokens: 8192,
  // Legacy catalog data is intentionally retained by the fixture, but must not render as editable rows.
  models: [{ id: 'deepseek-chat', name: 'DeepSeek Chat' }, { id: 'legacy-extra', name: '旧模型' }],
}]
const checks = [], errors = [], unexpected = [], mutations = [], nativeDialogs = [], reads = []
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
const page = await context.newPage()
page.setDefaultTimeout(10000)
page.on('pageerror', error => errors.push(error.message))
page.on('dialog', async dialog => { nativeDialogs.push(dialog.type()); await dialog.dismiss() })
await context.route('**/api/**', async route => {
  const req = route.request(), p = new URL(req.url()).pathname, method = req.method()
  let data
  if (method === 'GET') reads.push(p)
  else mutations.push({ method, path: p, body: req.postDataJSON() })
  if (p === '/api/v1/settings/model' && method === 'GET') data = cfg
  else if (p === '/api/v1/model-profiles' && method === 'GET') data = profiles
  else if (p === '/api/v1/settings/model' && method === 'PUT') {
    const body = req.postDataJSON()
    cfg = { ...cfg, ...body, active_model: body.model_default || cfg.active_model,
      effective_context_window: body.context_window ?? 131072,
      effective_max_output_tokens: body.max_output_tokens ?? 8192 }
    data = cfg
  } else if (p === '/api/v1/model-profiles/profile-a' && method === 'PUT') {
    const body = req.postDataJSON()
    profiles[0] = { ...profiles[0], ...body }
    cfg = { ...cfg, ...body, active_model: body.model_default || cfg.active_model,
      effective_context_window: body.context_window ?? 131072,
      effective_max_output_tokens: body.max_output_tokens ?? 8192 }
    data = profiles[0]
  } else if (p === '/api/v1/model-profiles/probe' && method === 'POST') {
    data = { models: ['deepseek-chat', 'deepseek-reasoner', 'fixture-custom-model'] }
  } else {
    unexpected.push(`${method} ${p}`)
    return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Unexpected fixture route' }) })
  }
  return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
})
await context.addInitScript(() => {
  window.__modelSettingsFixtureSockets = []
  const NativeSocket = window.WebSocket
  window.WebSocket = class extends NativeSocket {
    constructor(url, ...args) {
      if (new URL(String(url), location.href).pathname.includes('/api/')) {
        window.__modelSettingsFixtureSockets.push(new URL(String(url), location.href).pathname)
        throw new Error('Unexpected API WebSocket in model-settings fixture')
      }
      super(url, ...args)
    }
  }
})
await page.route('**/__model-settings-test*', route => route.fulfill({
  contentType: 'text/html',
  body: `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module">import {createApp,h,Fragment} from '${vuePath}'; import View from '/src/views/SettingsView.vue'; import DialogHost from '/src/components/ui/DialogHost.vue'; import '/src/style.css'; createApp({render(){return h(Fragment,null,[h(View,{user:{role:'admin'}}),h(DialogHost)])}}).mount('#app');</script></body></html>`,
}))

const connection = page.locator('.connection-panel')
const modelInput = page.locator('#default-model')
const contextInput = page.getByLabel(/^上下文窗口/)
const outputInput = page.getByLabel('最大输出 Token', { exact: true })
const save = () => connection.locator('button[type="submit"]')
async function screenshot(name) { await page.screenshot({ path: path.join(output, name), fullPage: true }) }
async function goto(width = 1440) {
  await page.setViewportSize({ width, height: width === 1440 ? 960 : 844 })
  await page.goto(`${base}/__model-settings-test?width=${width}`)
  await modelInput.waitFor()
  await contextInput.waitFor()
  await outputInput.waitFor()
}
async function settle() {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
}
async function assertNoOverflow(label) {
  const result = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth,
    controls: [...document.querySelectorAll('.connection-panel input:not(.autofill-decoy input), .connection-panel select, .profile-card')]
      .filter(el => el.getBoundingClientRect().width > 2)
      .map(el => { const r = el.getBoundingClientRect(); return { name: el.id || el.className, left: r.left, right: r.right } }) }))
  assert.ok(result.document <= result.viewport + 1, `${label}: document overflow: ${JSON.stringify(result)}`)
  for (const control of result.controls) assert.ok(control.left >= -1 && control.right <= result.viewport + 1,
    `${label}: control outside viewport: ${JSON.stringify(control)}`)
  checks.push(`${label}: no horizontal overflow and every form field fits`)
}
async function submitAndWait() {
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/settings/model' && r.request().method() === 'GET')
  await save().click()
  await response
  await settle()
}
async function invalidBudget(contextValue, outputValue, label) {
  await contextInput.fill(contextValue)
  await outputInput.fill(outputValue)
  const before = mutations.length
  await save().click()
  await settle()
  assert.equal(mutations.length, before, `${label}: invalid budgets must not reach the API`)
  const invalid = await page.evaluate(() => [...document.querySelectorAll('.connection-panel input')].some(el => !el.checkValidity()))
  const alerts = await page.getByRole('alert').count()
  assert.ok(invalid || alerts > 0, `${label}: invalid input must have visible validation feedback`)
  checks.push(`${label}: blocked before any API write`)
}

let failure
try {
  await goto()
  assert.equal(await modelInput.count(), 1)
  assert.equal(await page.locator('.pm-panel, .pm-row').count(), 0)
  assert.equal(await page.getByRole('button', { name: /模型清单/ }).count(), 0)
  assert.equal(await contextInput.inputValue(), '131072')
  assert.equal(await outputInput.inputValue(), '8192')
  assert.equal(await page.locator('#provider-api-key').inputValue(), '')
  const contextBox = await contextInput.boundingBox(), outputBox = await outputInput.boundingBox()
  assert.ok(contextBox && outputBox && Math.abs(contextBox.y - outputBox.y) <= 2 && outputBox.x > contextBox.x,
    'Desktop token settings should share a compact row')
  checks.push('Current configuration shows one model and two stored limits; secret is never populated')
  await assertNoOverflow('Desktop 1440')
  await screenshot('single-model-desktop.png')

  await page.locator('.profile-card').first().getByRole('button', { name: '编辑', exact: true }).click()
  assert.equal(await modelInput.inputValue(), 'deepseek-chat')
  assert.equal(await contextInput.inputValue(), '131072')
  assert.equal(await outputInput.inputValue(), '8192')
  await modelInput.fill('fixture-custom-model')
  await contextInput.fill('196608')
  await outputInput.fill('16384')
  await submitAndWait()
  const profileSave = mutations.findLast(m => m.method === 'PUT' && m.path === '/api/v1/model-profiles/profile-a')
  assert.ok(profileSave, 'Editing a saved profile must update that profile')
  assert.equal(profileSave.body.model_default, 'fixture-custom-model')
  assert.equal(profileSave.body.context_window, 196608)
  assert.equal(profileSave.body.max_output_tokens, 16384)
  assert.equal(profileSave.body.proxy_mode, 'direct')
  assert.equal(Object.hasOwn(profileSave.body, 'api_key'), false, 'Blank key must not replace the stored secret')
  assert.equal(Object.hasOwn(profileSave.body, 'models'), false, 'Saving one model must not overwrite a legacy catalog')
  checks.push('Editing a profile saves one model with integer budgets, retaining secret, proxy, and legacy catalog')
  assert.equal(await contextInput.inputValue(), '196608')
  assert.equal(await outputInput.inputValue(), '16384')

  await page.locator('.profile-card').first().getByRole('button', { name: '编辑', exact: true }).click()
  await connection.getByRole('button', { name: /探测模型|获取模型/ }).click()
  const datalistId = await modelInput.getAttribute('list')
  assert.ok(datalistId, 'Single model input must offer probed suggestions through a datalist')
  await page.locator(`datalist[id="${datalistId}"] option[value="deepseek-reasoner"]`).waitFor({ state: 'attached' })
  const probe = mutations.findLast(m => m.path === '/api/v1/model-profiles/probe')
  assert.equal(probe.body.profile_id, 'profile-a')
  assert.equal(probe.body.proxy_mode, 'direct')
  assert.equal(probe.body.api_key || '', '')
  assert.equal(await modelInput.inputValue(), 'fixture-custom-model', 'Probe must not change the chosen model')
  assert.equal(await contextInput.inputValue(), '196608', 'Probe must not reset limits')
  assert.equal(await outputInput.inputValue(), '16384', 'Probe must not reset limits')
  checks.push('Probe uses the stored profile key and adds datalist suggestions without changing model or limits')

  await contextInput.fill('')
  await outputInput.fill('')
  await submitAndWait()
  const cleared = mutations.findLast(m => m.method === 'PUT' && m.path === '/api/v1/model-profiles/profile-a')
  assert.equal(cleared.body.context_window, null)
  assert.equal(cleared.body.max_output_tokens, null)
  assert.equal(await contextInput.inputValue(), '')
  assert.equal(await outputInput.inputValue(), '')
  checks.push('Clearing limits saves explicit null overrides and restores automatic settings')

  await invalidBudget('0', '8192', 'Zero context window')
  await invalidBudget('131072.5', '8192', 'Fractional context window')
  await invalidBudget('131072', '0', 'Zero output limit')
  await invalidBudget('131072', '8192.5', 'Fractional output limit')
  await invalidBudget('8192', '8192', 'Output equal to context window')
  await invalidBudget('8192', '16384', 'Output larger than context window')
  await contextInput.fill('65536')
  await outputInput.fill('4096')
  await submitAndWait()
  const currentSave = mutations.findLast(m => m.method === 'PUT' && m.path === '/api/v1/settings/model')
  assert.equal(currentSave.body.context_window, 65536)
  assert.equal(currentSave.body.max_output_tokens, 4096)
  assert.equal(Object.hasOwn(currentSave.body, 'api_key'), false)
  checks.push('Valid current settings save and reload both limits without changing the key')

  await goto(390)
  await assertNoOverflow('Mobile 390')
  await screenshot('single-model-mobile-390.png')
  await page.locator('.profile-card').first().getByRole('button', { name: '编辑', exact: true }).click()
  await assertNoOverflow('Mobile profile edit 390')
  await connection.scrollIntoViewIfNeeded()
  await screenshot('single-model-mobile-edit-390.png')
  assert.deepEqual(await page.evaluate(() => window.__modelSettingsFixtureSockets), [])
  assert.deepEqual(errors, [])
  assert.deepEqual(nativeDialogs, [])
  assert.deepEqual(unexpected, [])
  checks.push('Every API request stayed inside fixture routes; no model calls, live writes, sockets, or native dialogs')
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
