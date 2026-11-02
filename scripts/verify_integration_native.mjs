// Short installed-product smoke. Reuse the real project created by
// verify_integration_delivery.mjs; its full CRUD/restart/reflow checks stay there.
// node scripts/verify_integration_native.mjs <installed-app-root> <out> <delivery-out>
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import net from 'node:net'

assert.ok(process.argv[2] && process.argv[3] && process.argv[4], 'Provide installed app root, evidence output and successful delivery output')
const root = resolve(process.argv[2]), out = resolve(process.argv[3]), delivery = resolve(process.argv[4]), fixture = join(delivery, 'project')
const config = JSON.parse(await readFile(join(delivery, 'saved-config.json'), 'utf8'))
await readFile(join(fixture, 'frontend/dev.mjs'), 'utf8'); await readFile(join(fixture, 'backend/server.py'), 'utf8')
assert.ok((await readFile(join(root, 'web/dist/index.html'), 'utf8')).includes('<html'), 'Installed frontend must exist')
await mkdir(out, { recursive: true })
const pause = ms => new Promise(done => setTimeout(done, ms))
async function wait(check, label, timeout = 30000) {
  const end = performance.now() + timeout; let last
  while (performance.now() < end) { try { const value = await check(); if (value) return value } catch (error) { last = error }; await pause(120) }
  throw Error(`${label}: ${last?.message || 'timed out'}`)
}
async function port() { return new Promise((done, reject) => { const server = net.createServer(); server.once('error', reject); server.listen(0, '127.0.0.1', () => { const value = server.address().port; server.close(() => done(value)) }) }) }
async function listening(value) { return new Promise(done => { const socket = net.connect({ host: '127.0.0.1', port: value }); let settled = false; const finish = value => { if (settled) return; settled = true; socket.destroy(); done(value) }; socket.once('connect', () => finish(true)); socket.once('error', () => finish(false)); socket.setTimeout(600, () => finish(true)) }) }
const gone = pid => { if (!pid) return true; try { process.kill(pid, 0); return false } catch (error) { if (error.code === 'ESRCH') return true; throw error } }
const servicePorts = [Number(new URL(config.frontend_url).port || 80), Number(new URL(config.backend_url).port || 80)]
for (const value of servicePorts) assert.equal(await listening(value), false, `Fixture port ${value} must be released by the delivery verifier first`)
const backend = await port(), frontend = await port(), debug = await port(), diagnostics = join(out, 'native.json')
const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|ANTHROPIC_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|JAVA_HOME|JDK_HOME|JDK_JAVA_OPTIONS|JAVA_TOOL_OPTIONS|_JAVA_OPTIONS|npm_)/i.test(name)))
Object.assign(env, { PATH: [join(process.env.SystemRoot, 'System32'), process.env.SystemRoot, join(process.env.SystemRoot, 'System32/WindowsPowerShell/v1.0')].join(';'), CODE_AGENT_HOME: join(out, 'user-data'), CODE_AGENT_PROVIDER: 'offline', CODE_AGENT_AUTH_ENABLED: 'false', PYTHONDONTWRITEBYTECODE: '1', CODE_AGENT_LAUNCHER_BACKEND_PORT: String(backend), CODE_AGENT_LAUNCHER_FRONTEND_PORT: String(frontend) })
const report = { success: false, root, out, fixture, ports: { backend, frontend, debug, project: servicePorts }, checks: [], errors: [], sentPrompts: [], screenshots: [], startedAt: new Date().toISOString() }
let app, exited, browser, page, projectId, nativeClosed = false
const prefix = () => `/api/v1/code/projects/${projectId}/integration`
async function api(method, path, body, expected = 200) {
  const response = await fetch(`http://127.0.0.1:${backend}${path}`, { method, headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(15000) })
  const text = await response.text(); let value; try { value = JSON.parse(text) } catch { value = text }
  assert.equal(response.status, expected, `${method} ${path}: ${text.slice(0, 800)}`); return value
}
async function desktopRpc(method, params = {}) { return page.evaluate(({ method, params }) => new Promise((done, reject) => { const id = `integration-native-${Date.now()}-${Math.random()}`, timer = setTimeout(() => { window.chrome.webview.removeEventListener('message', receive); reject(Error('Desktop RPC timed out')) }, 15000); const receive = event => { if (event.data?.id !== id) return; clearTimeout(timer); window.chrome.webview.removeEventListener('message', receive); event.data.ok ? done(event.data.result) : reject(Error(event.data.error)) }; window.chrome.webview.addEventListener('message', receive); window.chrome.webview.postMessage({ channel: 'code-agent-desktop', id, method, params }) }), { method, params }) }
async function screenshot(name) { const path = join(out, name); await page.screenshot({ path }); report.screenshots.push(path) }
async function quit() {
  if (!app || nativeClosed || app.exitCode !== null) return
  if (page) await page.evaluate(() => window.chrome.webview.postMessage({ channel: 'code-agent-desktop', id: 'integration-native-exit', method: 'exit', params: {} })).catch(() => {})
  await wait(() => app.exitCode !== null, 'normal native close', 18000); await exited; nativeClosed = true
}

try {
  app = spawn(join(root, 'Code-agent.exe'), ['--diagnostics-file', diagnostics, '--diagnostics-port', String(debug), '--diagnostics-exit-after-ms', '120000'], { cwd: out, env, windowsHide: true, stdio: 'ignore' })
  exited = new Promise((done, reject) => { app.once('exit', (code, signal) => done({ code, signal })); app.once('error', reject) })
  const ready = await wait(async () => { if (app.exitCode !== null) throw Error(`Native exited with ${app.exitCode}`); const state = JSON.parse((await readFile(diagnostics, 'utf8')).replace(/^\uFEFF/, '')); if (state.state === 'failed') throw Error(state.error); return state.state === 'ready' ? state : null }, 'installed native ready', 45000)
  assert.equal(ready.integratedWorkbench, true); assert.equal(ready.windowEnabled, true)
  report.nativePid = app.pid; report.controllerPid = ready.controllerPid
  const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  browser = await (pw.default || pw).chromium.connectOverCDP(`http://127.0.0.1:${debug}`)
  page = await wait(() => browser.contexts().flatMap(context => context.pages()).find(value => value.url().startsWith(`http://127.0.0.1:${frontend}/`)), 'embedded installed workbench')
  page.setDefaultTimeout(20000)
  page.on('pageerror', error => report.errors.push(error.message))
  page.on('request', request => { const path = new URL(request.url()).pathname; if (request.method() === 'POST' && path.endsWith('/prompt')) report.sentPrompts.push({ transport: 'http', path }) })
  page.on('websocket', socket => socket.on('framesent', frame => { try { if (JSON.parse(String(frame.payload)).type === 'prompt') report.sentPrompts.push({ transport: 'ws' }) } catch {} }))
  await page.getByRole('menubar', { name: 'IDE 菜单' }).waitFor()
  const entry = page.locator('.workspace-entry[aria-busy="false"]'), integrationButton = page.getByRole('button', { name: '前后端联调', exact: true })
  await wait(async () => await entry.isVisible() || await integrationButton.isEnabled(), 'native project entry or already opened workspace')
  if (!await integrationButton.isEnabled()) {
    const path = page.getByRole('textbox', { name: '工作区路径', exact: true })
    if (!await path.isVisible()) await page.getByRole('button', { name: '输入工作区路径', exact: true }).click()
    await path.fill(fixture); await page.getByRole('button', { name: '打开工作区', exact: true }).click()
  }
  const project = await wait(async () => (await api('GET', '/api/v1/code/projects')).find(value => resolve(value.root_path).toLowerCase() === fixture.toLowerCase()), 'native opened fixture project')
  projectId = project.id; report.projectId = projectId
  await api('PUT', prefix() + '/config', config)
  await page.getByRole('button', { name: '前后端联调', exact: true }).click()
  const panel = page.getByRole('region', { name: '前后端联调', exact: true }); await panel.waitFor()
  await wait(async () => await panel.getByLabel('前端服务地址', { exact: true }).inputValue() === config.frontend_url, 'persisted configuration in installed panel')
  await panel.getByRole('button', { name: '展开联调面板', exact: true }).click()
  report.checks.push('Installed EXE opens its embedded workbench and real project with an isolated user profile')
  await panel.getByRole('button', { name: '启动联调', exact: true }).click()
  const running = await wait(async () => { const value = (await api('GET', prefix())).session; if (value?.status === 'failed') throw Error(JSON.stringify(value)); return value?.status === 'running' ? value : null }, 'native starts both actual project services', 45000)
  report.projectServices = Object.fromEntries(Object.entries(running.services).map(([role, value]) => [role, { pid: value.pid, status: value.status, run_id: value.run_id }]))
  await panel.getByRole('tab', { name: '页面预览', exact: true }).click()
  const frame = page.frameLocator('iframe[title="前后端联调页面预览"]')
  await frame.getByRole('heading', { name: /联调示例 · v[12]/ }).waitFor()
  async function action(name, expected) { await frame.getByRole('button', { name, exact: true }).click(); return wait(async () => { const value = await frame.getByTestId('fixture-status').textContent(); return value.includes(expected) ? JSON.parse(value) : null }, `installed preview ${name}`) }
  await action('读取任务', '初始任务'); await action('新增任务', '画布创建')
  const auth = await action('登录并验证', 'authenticated'); assert.equal(auth.authenticated, true); assert.equal(auth.authorization_seen, true); assert.equal(auth.cookie_seen, true)
  assert.equal(await frame.locator('body').evaluate(() => document.cookie), '')
  await screenshot('native-integration-preview.png')
  const requests = (await api('GET', prefix() + '/requests')).requests
  assert.ok(requests.some(value => value.method === 'GET') && requests.some(value => value.method === 'POST'))
  report.checks.push('Actual installed iframe automatically loads and completes GET, POST and bearer plus isolated HttpOnly cookie authentication')
  await panel.getByRole('tab', { name: '接口调试', exact: true }).click(); await panel.getByRole('button', { name: '刷新请求历史', exact: true }).click()
  await panel.getByRole('complementary', { name: '接口请求历史' }).getByRole('button', { name: /GET.*200/ }).first().waitFor()
  await screenshot('native-integration-requests.png')
  await panel.getByRole('button', { name: '停止', exact: true }).click()
  await wait(async () => (await api('GET', prefix())).session?.status === 'stopped', 'native group stop', 20000)
  await wait(async () => (await Promise.all(servicePorts.map(listening))).every(value => !value), 'project ports released', 15000)
  await wait(async () => await panel.locator('iframe').count() === 0, 'installed panel observes stopped state and releases iframe', 8000)
  report.checks.push('Installed Stop releases the two owned project ports and removes the preview')
  const desktop = await desktopRpc('status'); report.services = desktop.services.map(({ id, pid, state }) => ({ id, pid, state }))
  assert.ok(report.services.every(value => value.state === 'healthy')); assert.deepEqual(report.sentPrompts, []); assert.deepEqual(report.errors, [])
  const closeStarted = performance.now(); await quit(); report.closeMs = Math.round(performance.now() - closeStarted)
  assert.equal(app.exitCode, 0)
  await wait(() => gone(app.pid) && gone(report.controllerPid) && report.services.every(value => gone(value.pid)), 'owned native services exit', 15000)
  await wait(async () => !(await listening(backend)) && !(await listening(frontend)), 'installed application ports released', 10000)
  report.checks.push('Normal native close exits the EXE, controller and owned application services')
  report.success = true
} catch (error) {
  report.failure = error.stack || String(error)
  if (page) { await screenshot('native-integration-failure.png').catch(() => {}); await writeFile(join(out, 'failure.html'), await page.content()).catch(() => {}) }
  console.error(report.failure); process.exitCode = 1
} finally {
  if (projectId && app?.exitCode === null) {
    try { await api('POST', prefix() + '/stop', { role: 'all' }, 202); await wait(async () => (await Promise.all(servicePorts.map(listening))).every(value => !value), 'cleanup project stop', 12000) } catch (error) { report.errors.push('Project cleanup: ' + error.message) }
  }
  await quit().catch(error => report.errors.push('Native cleanup: ' + error.message))
  if (browser) await browser.close().catch(() => {})
  report.remainingListeners = { applicationBackend: await listening(backend), applicationFrontend: await listening(frontend), project: await Promise.all(servicePorts.map(listening)) }
  if (report.remainingListeners.applicationBackend || report.remainingListeners.applicationFrontend || report.remainingListeners.project.some(Boolean)) report.success = false
  report.finishedAt = new Date().toISOString(); await writeFile(join(out, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ success: report.success, out, checks: report.checks.length, failure: report.failure, remainingListeners: report.remainingListeners }, null, 2))
  if (!report.success) process.exitCode = 1
}
