// Real native/WebView2 edge and unsaved-close acceptance. Only the provided
// candidate EXE, isolated profile, fixture file and owned service ports are used.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import net from 'node:net'

const root = resolve(process.argv[2]), executable = resolve(process.argv[3]), out = resolve(process.argv[4])
await mkdir(out, { recursive: true })
const fixture = join(out, 'project'), original = 'native edge fixture\n'
await mkdir(fixture, { recursive: true }); await writeFile(join(fixture, 'fixture.txt'), original)
const delay = ms => new Promise(done => setTimeout(done, ms))
async function wait(test, label, timeout = 20000) {
  const until = performance.now() + timeout; let last
  while (performance.now() < until) { try { const result = await test(); if (result) return result } catch (error) { last = error }; await delay(100) }
  throw Error(`${label}: ${last?.message || 'timed out'}`)
}
async function port() { return new Promise((done, reject) => { const server = net.createServer(); server.once('error', reject); server.listen(0, '127.0.0.1', () => { const p = server.address().port; server.close(() => done(p)) }) }) }
async function listening(value) { return new Promise(done => { const socket = net.connect({ host: '127.0.0.1', port: value }); let settled = false; const finish = result => { if (settled) return; settled = true; socket.destroy(); done(result) }; socket.once('connect', () => finish(true)); socket.once('error', () => finish(false)); socket.setTimeout(500, () => finish(false)) }) }
const backend = await port(), frontend = await port(), debug = await port(), diagnostics = join(out, 'native.json')
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|ANTHROPIC_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|JAVA_HOME|JDK_HOME|JDK_JAVA_OPTIONS|JAVA_TOOL_OPTIONS|_JAVA_OPTIONS|npm_)/i.test(key)))
Object.assign(env, { PATH: [join(process.env.SystemRoot, 'System32'), process.env.SystemRoot, join(process.env.SystemRoot, 'System32/WindowsPowerShell/v1.0')].join(';'), CODE_AGENT_HOME: join(out, 'user-data'), CODE_AGENT_PROVIDER: 'offline', CODE_AGENT_AUTH_ENABLED: 'false', PYTHONDONTWRITEBYTECODE: '1', CODE_AGENT_LAUNCHER_BACKEND_PORT: String(backend), CODE_AGENT_LAUNCHER_FRONTEND_PORT: String(frontend) })
const report = { passed: false, root, executable, checks: [], errors: [], prompts: [], ports: { backend, frontend, debug }, screenshots: [] }
let app, browser, page
const native = async () => JSON.parse((await readFile(diagnostics, 'utf8')).replace(/^\uFEFF/, ''))
async function rpc(method) {
  return page.evaluate(method => new Promise((done, reject) => {
    const id = `edge-${Date.now()}-${Math.random()}`, timer = setTimeout(() => { window.chrome.webview.removeEventListener('message', receive); reject(Error(method + ' timed out')) }, 8000)
    function receive(event) { const value = event.data; if (value?.channel !== 'code-agent-desktop' || value.id !== id) return; clearTimeout(timer); window.chrome.webview.removeEventListener('message', receive); value.ok ? done(value.result) : reject(Error(value.error)) }
    window.chrome.webview.addEventListener('message', receive); window.chrome.webview.postMessage({ channel: 'code-agent-desktop', id, method, params: {} })
  }), method)
}
async function screenshot(name) { const path = join(out, name); await page.screenshot({ path }); report.screenshots.push(path) }
try {
  app = spawn(executable, ['--root', root, '--diagnostics-file', diagnostics, '--diagnostics-port', String(debug), '--diagnostics-exit-after-ms', '120000'], { cwd: out, env, windowsHide: true, stdio: 'ignore' })
  const ready = await wait(async () => { const state = await native(); if (state.state === 'failed') throw Error(state.error); return state.state === 'ready' ? state : null }, 'isolated workbench', 50000)
  report.pid = app.pid; report.controllerPid = ready.controllerPid
  const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  browser = await (pw.default || pw).chromium.connectOverCDP(`http://127.0.0.1:${debug}`)
  page = await wait(() => browser.contexts().flatMap(context => context.pages()).find(page => page.url().startsWith(`http://127.0.0.1:${frontend}/`)), 'embedded page')
  page.setDefaultTimeout(15000)
  page.on('pageerror', error => report.errors.push(error.message))
  page.on('request', request => { if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/prompt')) report.prompts.push(request.url()) })
  await page.getByRole('menubar', { name: 'IDE 菜单' }).waitFor()
  report.normal = await rpc('window-state')
  assert.equal(report.normal.integratedFrame, true)
  assert.equal(report.normal.clientBounds.y, report.normal.bounds.y, 'no native top strip above the IDE titlebar')
  const normalBounds = report.normal.bounds
  await page.getByRole('button', { name: '最大化窗口', exact: true }).click()
  await wait(async () => (await rpc('window-state')).maximized, 'maximize')
  report.maximized = await rpc('window-state')
  assert.deepEqual(report.maximized.clientBounds, report.maximized.workingArea)
  await page.getByRole('button', { name: '还原窗口', exact: true }).click()
  await wait(async () => !(await rpc('window-state')).maximized, 'restore')
  report.restored = await rpc('window-state'); assert.deepEqual(report.restored.bounds, normalBounds)
  assert.equal(report.restored.clientBounds.y, report.restored.bounds.y)
  report.checks.push('Normal and restored WebView client starts at window top; maximize fits the work area and restore preserves bounds')
  await screenshot('edge-normal.png')
  await page.getByRole('button', { name: '切换项目', exact: true }).click()
  await page.getByRole('button', { name: '输入工作区路径…', exact: true }).click()
  await page.getByRole('textbox', { name: '工作区路径', exact: true }).fill(fixture)
  await page.locator('.workspace-entry .path-form button[type="submit"]').click()
  await page.locator('.tree .row').filter({ hasText: 'fixture.txt' }).click()
  await page.locator('.monaco-editor [role="textbox"]').focus(); await page.keyboard.press('Control+End'); await page.keyboard.type('unsaved native edge verification')
  await page.locator('.chip.dirty').waitFor()
  const before = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }))
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click()
  await page.getByRole('button', { name: '继续编辑', exact: true }).waitFor()
  await wait(async () => (await native()).closePending === true, 'native acknowledgement of the web confirmation')
  await delay(500)
  report.pending = await native()
  assert.equal(report.pending.closeStatusVisible, false)
  assert.equal(report.pending.windowEnabled, true)
  assert.deepEqual(await page.evaluate(() => ({ width: innerWidth, height: innerHeight })), before, 'confirmation adds no native bottom strip or layout jump')
  assert.equal(report.pending.clientBounds.y, report.pending.bounds.y)
  await screenshot('edge-unsaved-confirmation.png')
  report.checks.push('Real unsaved-file web confirmation has no native top/bottom band and does not change viewport size or disable the window')
  await page.getByRole('button', { name: '继续编辑', exact: true }).click()
  await wait(async () => (await native()).state === 'ready', 'cancel returns the workbench')
  await page.locator('.chip.dirty').waitFor(); assert.equal(await readFile(join(fixture, 'fixture.txt'), 'utf8'), original)
  report.checks.push('Cancelling close keeps the dirty fixture draft and leaves its disk file unchanged')
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click()
  await page.getByRole('button', { name: '仍然离开', exact: true }).click()
  await wait(() => app.exitCode !== null, 'confirmed native shutdown', 18000)
  assert.equal(app.exitCode, 0); assert.equal(await readFile(join(fixture, 'fixture.txt'), 'utf8'), original)
  assert.deepEqual(report.errors, []); assert.deepEqual(report.prompts, [])
  report.checks.push('Confirming the owned draft closes only the isolated app and does not save/discard any user file')
  report.passed = true
} catch (error) {
  report.failure = error.stack || String(error); process.exitCode = 1
  if (page && !page.isClosed()) { await screenshot('failure.png').catch(() => {}); await writeFile(join(out, 'failure.html'), await page.content()).catch(() => {}) }
} finally {
  if (app?.exitCode === null && page && !page.isClosed()) {
    await page.getByRole('button', { name: '继续编辑', exact: true }).click({ timeout: 500 }).catch(() => {})
    await page.getByRole('button', { name: '关闭窗口', exact: true }).click({ timeout: 500 }).catch(() => {})
    await page.getByRole('button', { name: '仍然离开', exact: true }).click({ timeout: 1000 }).catch(() => {})
    await wait(() => app.exitCode !== null, 'cleanup normal close', 18000).catch(() => {})
  }
  if (app?.exitCode === null) {
    await new Promise(done => { const cleanup = spawn('taskkill.exe', ['/PID', String(app.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); cleanup.once('exit', done); cleanup.once('error', done) }); report.forcedCleanup = true
  }
  if (browser) await browser.close().catch(() => {})
  await wait(async () => !(await listening(backend)) && !(await listening(frontend)) && !(await listening(debug)), 'owned port release', 8000).catch(() => {})
  report.remainingListeners = { backend: await listening(backend), frontend: await listening(frontend), debug: await listening(debug) }
  if (Object.values(report.remainingListeners).some(Boolean)) report.passed = false
  await writeFile(join(out, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: report.passed, checks: report.checks.length, out, failure: report.failure, remainingListeners: report.remainingListeners }))
}
