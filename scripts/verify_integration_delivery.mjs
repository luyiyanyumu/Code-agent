/** Real local, offline-provider delivery acceptance. No mocked APIs or project services. */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer as netServer, connect } from 'node:net'
import { mkdir, readFile, writeFile, copyFile, unlink } from 'node:fs/promises'
import { createWriteStream } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const args = process.argv.slice(2)
const option = name => args.includes(name) ? args[args.indexOf(name) + 1] : undefined
const appRoot = resolve(option('--app-root') || repo)
const out = resolve(option('--out') || join(repo, '.tmp', `integration-delivery-${Date.now()}`))
const fixture = join(out, 'project'), fixtures = join(repo, 'tests', 'fixtures')
await mkdir(out, { recursive: true }); await mkdir(fixture)
await Promise.all(['frontend/src', 'frontend/vendor', 'backend'].map(path => mkdir(join(fixture, path), { recursive: true })))
const report = { out, fixture, checks: [], errors: [], sentPrompts: [], screenshots: [], startedAt: new Date().toISOString() }
async function mark(stage) {
  report.stage = stage
  console.log(stage)
  await writeFile(join(out, 'progress.json'), JSON.stringify({ stage, at: new Date().toISOString(), projectId: report.projectId, checks: report.checks }, null, 2))
}
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function wait(check, label, timeout = 30000) {
  const end = Date.now() + timeout
  let last
  while (Date.now() < end) {
    try { const result = await check(); if (result) return result } catch (error) { last = error }
    await pause(150)
  }
  throw Error(`${label} timed out${last ? ': ' + last.message : ''}`)
}
async function freePort() { const server = netServer(); await new Promise(done => server.listen(0, '127.0.0.1', done)); const port = server.address().port; await new Promise(done => server.close(done)); return port }
async function listening(port) { return new Promise(done => { const socket = connect({ host: '127.0.0.1', port }); socket.once('connect', () => { socket.destroy(); done(true) }); socket.once('error', () => done(false)); socket.setTimeout(700, () => { socket.destroy(); done(false) }) }) }
const ports = { api: await freePort(), ide: await freePort(), frontend: await freePort(), backend: await freePort() }
const installedWeb = args.includes('--installed-web')
const base = `http://127.0.0.1:${ports.api}`, ide = installedWeb ? base : `http://127.0.0.1:${ports.ide}`
const frontend = `http://127.0.0.1:${ports.frontend}/`, backend = `http://127.0.0.1:${ports.backend}/`
const viteModule = pathToFileURL(join(repo, 'web', 'node_modules', 'vite', 'dist', 'node', 'index.js')).href
const python = option('--python') || join(appRoot, appRoot === repo ? '.venv/Scripts/python.exe' : 'runtimes/python/python.exe')
const node = option('--node') || process.execPath
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^(CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|ANTHROPIC_|PYTHON|VIRTUAL_ENV$|NODE_OPTIONS$)/i.test(name)))
const env = { ...cleanEnv, CODE_AGENT_HOME: join(out, 'user-data'), CODE_AGENT_AUTH_ENABLED: 'false', CODE_AGENT_PROVIDER: 'offline',
  ACCEPTANCE_API_PORT: String(ports.api), ACCEPTANCE_DUMP_STACKS: '1', CODE_AGENT_API_TARGET: base, PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' }
if (installedWeb) env.CODE_AGENT_WEB_DIST = join(appRoot, 'web/dist')
if (appRoot !== repo) env.PATH = [join(appRoot, 'runtimes/node'), join(appRoot, 'runtimes/python'), join(appRoot, 'runtimes/git/cmd'), env.PATH].join(';')
const config = { name: '真实 Vue Python 联调验收', frontend: { mode: 'launch', kind: 'node', cwd: 'frontend', path: 'frontend/dev.mjs', args: [], env: { PORT: String(ports.frontend), BACKEND_URL: backend, VITE_MODULE: viteModule } },
  backend: { mode: 'launch', kind: 'python', cwd: 'backend', path: 'backend/server.py', args: [], env: { PORT: String(ports.backend), PYTHONUNBUFFERED: '1' } },
  frontend_url: frontend, backend_url: backend, health_url: backend + 'health', api_prefix: '/api', strip_api_prefix: false, startup_timeout: 45 }
const healthSwitch = join(fixture, 'backend/unhealthy.flag')
if (args.includes('--health-recovery-only')) config.backend.env.HEALTH_SWITCH_FILE = healthSwitch
await Promise.all([
  copyFile(join(fixtures, 'integration_live_backend.py'), join(fixture, 'backend/server.py')),
  copyFile(join(fixtures, 'integration_live_frontend.mjs'), join(fixture, 'frontend/dev.mjs')),
  copyFile(join(fixtures, 'integration_live_main.ts'), join(fixture, 'frontend/src/main.ts')),
  copyFile(join(repo, 'web/node_modules/vue/dist/vue.esm-browser.prod.js'), join(fixture, 'frontend/vendor/vue.js')),
  writeFile(join(fixture, 'frontend/package.json'), JSON.stringify({ name: 'integration-live-fixture', private: true, type: 'module', scripts: { dev: 'node dev.mjs' } }, null, 2)),
  writeFile(join(fixture, 'frontend/index.html'), `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>真实联调项目</title><style>body{margin:0;background:#f6f8fc;color:#1c2840;font:14px system-ui,sans-serif}main{padding:24px;max-width:960px}h1{font-size:23px;margin:8px 0}p{line-height:1.7;color:#60718b}.eyebrow{font-size:11px;letter-spacing:.08em;color:#4664f4}.actions{display:flex;flex-wrap:wrap;gap:8px;margin:17px 0}button{background:white;border:1px solid #cfd7eb;border-radius:5px;padding:8px 12px;color:#294169;cursor:pointer}button:hover{border-color:#4965ff}output{display:block;background:white;border:1px solid #dae2f2;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;border-radius:5px;min-height:30px;font:12px Consolas,monospace}</style><script>window.__BACKEND_ORIGIN__=__BACKEND_JSON__</script></head><body><div id="app"></div><script type="module" src="/src/main.ts"></script></body></html>`),
  writeFile(join(out, 'endpoints.json'), JSON.stringify({ ports, base, ide, frontend, backend, fixture }, null, 2)),
  writeFile(join(out, 'saved-config.json'), JSON.stringify(config, null, 2)),
])
const children = [], logStreams = []
function child(command, argv, label, cwd) {
  const log = createWriteStream(join(out, label + '.log')); logStreams.push(log)
  const proc = spawn(command, argv, { cwd, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
  proc.stdout.pipe(log); proc.stderr.pipe(log); children.push(proc)
  proc.on('error', error => report.errors.push(`${label}: ${error.message}`))
  return proc
}
async function api(method, path, body, expected = 200) {
  let response
  try { response = await fetch(base + path, { method, headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000) }) }
  catch (error) { throw Error(`${method} ${path}: ${error.message}`, { cause: error }) }
  const text = await response.text(); let value; try { value = JSON.parse(text) } catch { value = text }
  assert.equal(response.status, expected, `${method} ${path}: ${text.slice(0, 1000)}`)
  return value
}
let browser, page, platform, host, projectId, panel
const prefix = () => `/api/v1/code/projects/${projectId}/integration`
async function state() { return (await api('GET', prefix())).session }
async function sessionStatus(status, timeout = 60000) { return wait(async () => { const value = await state(); if (value?.status === 'failed' && status !== 'failed') throw Error(JSON.stringify(value)); return value?.status === status ? value : null }, `integration ${status}`, timeout) }
async function openPanel() {
  await page.getByRole('button', { name: '前后端联调', exact: true }).click()
  panel = page.getByRole('region', { name: '前后端联调', exact: true }); await panel.waitFor()
  const expand = panel.getByRole('button', { name: '展开联调面板', exact: true })
  if (await expand.isVisible()) await expand.click()
}
async function history() { return (await api('GET', prefix() + '/requests')).requests }
async function screenshot(name) { const path = join(out, name); await page.screenshot({ path }); report.screenshots.push(path) }
async function frameAction(label, expected) {
  const frame = page.frameLocator('iframe[title="前后端联调页面预览"]')
  await frame.getByRole('button', { name: label, exact: true }).click()
  try { await wait(async () => (await frame.getByTestId('fixture-status').textContent()).includes(expected), `business action ${label}`, 10000) }
  catch (error) { throw Error(`${error.message}: ${await frame.getByTestId('fixture-status').textContent()}`) }
  return frame.getByTestId('fixture-status').textContent()
}
try {
  // Keep this throwaway project independent of the source repository's ignored
  // build archives and Git ancestry; it remains a real project, not a mock.
  const git = child('git', ['init', '--quiet', fixture], 'fixture-git-init', fixture)
  await wait(() => git.exitCode !== null, 'fixture Git initialization', 10000)
  assert.equal(git.exitCode, 0)
  platform = child(python, ['-B', '-X', 'utf8', '-c', 'import runpy,sys; runpy.run_path(sys.argv[1],run_name="__main__")', join(fixtures, 'integration_live_platform.py')], 'platform', appRoot)
  await wait(async () => { const response = await fetch(base + '/healthz', { signal: AbortSignal.timeout(1000) }); return response.ok }, 'real offline application', 60000)
  await mark('Real API ready: ' + base)
  const registered = await api('POST', '/api/v1/code/projects', { mode: 'open', path: fixture, name: '联调验收项目', git_init: false })
  projectId = registered.id || registered.project?.id
  assert.ok(projectId, JSON.stringify(registered)); report.projectId = projectId
  await mark('Real fixture registered: ' + projectId)
  await api('GET', prefix() + '/config')
  await mark('Configuration discovery ready')
  await api('PUT', prefix() + '/config', config)
  await mark('Real integration configuration persisted')
  if (args.includes('--probe-only')) { report.success = true; report.checks.push('Real project registration and integration config GET/PUT complete'); }
  else if (args.includes('--health-recovery-only')) {
    await api('POST', prefix() + '/start', undefined, 202)
    const initial = await sessionStatus('running')
    report.initialSession = initial
    const capability = (await api('POST', prefix() + '/preview', {})).api_proxy_url
    await writeFile(healthSwitch, 'Created by this acceptance task only.\n')
    const degraded = await sessionStatus('degraded', 15000)
    assert.equal(degraded.services.backend.status, 'unhealthy')
    assert.equal(degraded.services.backend.pid, initial.services.backend.pid)
    report.degradedSession = degraded
    await mark('Real /health 503 produces degraded session while owned backend remains running')
    const preflightOptions = { method: 'OPTIONS', headers: { Origin: 'null', 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,x-code-agent-project-headers' } }
    const preflight = await fetch(capability + 'api/items', preflightOptions)
    report.degradedPreflight = { status: preflight.status, allowOrigin: preflight.headers.get('access-control-allow-origin') }
    assert.equal(preflight.status, 204); assert.equal(report.degradedPreflight.allowOrigin, '*')
    const read = await fetch(capability + 'api/items', { headers: { Origin: 'null' } })
    assert.equal(read.status, 200); assert.equal((await read.json()).items[0].title, '初始任务')
    const write = await fetch(capability + 'api/items', { method: 'POST', headers: { Origin: 'null', 'Content-Type': 'application/json' }, body: '{"title":"健康退化中仍能编辑"}' })
    assert.equal(write.status, 201); assert.equal((await write.json()).title, '健康退化中仍能编辑')
    const manual = await api('POST', prefix() + '/request', { method: 'POST', path: '/api/items', body: { title: '手动联调继续可用' } })
    assert.equal(manual.status_code, 201)
    report.checks.push('Real degraded/unhealthy backend still permits capability OPTIONS 204, GET 200, POST 201 and manual POST 201')
    await unlink(healthSwitch)
    const restored = await sessionStatus('running', 15000)
    assert.equal(restored.services.backend.status, 'ready'); assert.equal(restored.services.backend.pid, initial.services.backend.pid)
    assert.equal((await fetch(capability + 'api/items')).status, 200)
    report.checks.push('Removing the controlled health failure restores ready/running without replacing process or capability')
    const beforeStop = (await history()).length
    await api('POST', prefix() + '/stop', { role: 'all' }, 202)
    await sessionStatus('stopped')
    await wait(async () => !await listening(ports.frontend) && !await listening(ports.backend), 'health task owned ports released')
    assert.equal((await fetch(capability + 'api/items', preflightOptions)).status, 410)
    assert.equal((await fetch(capability + 'api/items')).status, 410)
    assert.equal((await history()).length, beforeStop)
    report.checks.push('Stop revokes OPTIONS and GET with 410, releases both ports and forwards no extra business request')
    report.success = true
  }
  else {
  if (!installedWeb) {
  const ideRunner = join(out, 'ide-host.mjs')
  await writeFile(ideRunner, `import {createServer} from ${JSON.stringify(viteModule)}; const server=await createServer({root:${JSON.stringify(join(repo, 'web'))},server:{host:'127.0.0.1',port:${ports.ide},strictPort:true}}); await server.listen(); process.stdin.setEncoding('utf8');process.stdin.on('data',async value=>{if(value.includes('stop')){await server.close();process.exit(0)}});`)
  host = child(node, [ideRunner], 'ide', repo)
  await wait(async () => (await fetch(ide + '/__code_agent_health', { signal: AbortSignal.timeout(1000) })).ok, 'real IDE Vite', 60000)
  }
  const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  page = await context.newPage(); page.setDefaultTimeout(30000)
  report.httpErrors = []; report.requestFailures = []
  page.on('pageerror', error => report.errors.push(error.message))
  page.on('console', message => {
    if (message.type() !== 'error') return
    report.errors.push('console: ' + message.text())
    if (message.text().includes('CORS') && !report.corsState) state().then(value => { report.corsState = value }).catch(() => {})
  })
  page.on('response', async response => {
    if (response.status() < 400) return
    const item = { method: response.request().method(), status: response.status(), url: response.url() }; report.httpErrors.push(item)
    try { item.body = (await response.text()).slice(0, 1500) } catch {}
  })
  page.on('requestfailed', request => report.requestFailures.push({ method: request.method(), url: request.url(), error: request.failure()?.errorText }))
  const network = await context.newCDPSession(page)
  await network.send('Network.enable')
  network.on('Network.responseReceived', async event => {
    if (event.type !== 'Preflight') return
    const item = { method: 'OPTIONS', status: event.response.status, url: event.response.url, type: 'Preflight' }; report.httpErrors.push(item)
    try { item.body = (await network.send('Network.getResponseBody', { requestId: event.requestId })).body.slice(0, 1500) } catch {}
    if (item.status >= 400) try { report.preflightFailureState = await state() } catch {}
  })
  page.on('request', request => { const path = new URL(request.url()).pathname; if (request.method() === 'POST' && path.endsWith('/prompt')) report.sentPrompts.push({ transport: 'http', path }) })
  page.on('websocket', socket => socket.on('framesent', frame => { try { if (JSON.parse(String(frame.payload)).type === 'prompt') report.sentPrompts.push({ transport: 'ws' }) } catch {} }))
  await page.goto(ide + '/', { waitUntil: 'domcontentloaded', timeout: 90000 })
  await mark('Workbench document received')
  await page.getByRole('menubar', { name: 'IDE 菜单' }).waitFor({ timeout: 90000 })
  await mark('Workbench menu ready')
  // The real workbench restores the only registered project during init. Wait
  // for its enabled action instead of reopening a hidden project picker.
  await wait(() => page.getByRole('button', { name: '前后端联调', exact: true }).isEnabled(), 'registered fixture active in workbench', 60000)
  await mark('Real fixture active in workbench')
  const project = await wait(async () => (await api('GET', '/api/v1/code/projects')).find(row => resolve(row.root_path).toLowerCase() === fixture.toLowerCase()), 'UI registered real fixture', 60000)
  projectId = project.id; report.projectId = projectId
  await openPanel()
  await mark('Integration panel open')
  await wait(() => panel.getByRole('textbox', { name: '前端服务地址', exact: true }).inputValue().then(value => value === frontend), 'saved config in UI')
  assert.equal((await state()), null); assert.equal(await listening(ports.backend), false); assert.equal(await listening(ports.frontend), false)
  report.checks.push('Opening/configuring the real IDE does not start project processes')
  await panel.getByRole('button', { name: '启动联调', exact: true }).click()
  await mark('GUI start submitted')
  let running = await sessionStatus('running'); report.initialSession = running
  await mark('Both production runtime services running')
  const order = JSON.parse(await readFile(join(fixture, 'frontend/startup-order.json'), 'utf8'))
  assert.equal(order.backendReady, true); assert.ok(order.at >= Date.parse(running.created_at))
  report.checks.push('Production runtime starts real Python backend, waits for HTTP health, then starts real Vite frontend')
  await panel.getByRole('tab', { name: '页面预览', exact: true }).click()
  if (!await panel.locator('iframe').count()) await panel.getByRole('button', { name: '刷新预览', exact: true }).click()
  const frame = page.frameLocator('iframe[title="前后端联调页面预览"]')
  await frame.getByRole('heading', { name: '联调示例 · v1', exact: true }).waitFor({ timeout: 45000 })
  await mark('Compiled Vue page rendered through real preview')
  await frameAction('读取任务', '初始任务')
  await frameAction('新增任务', '画布创建')
  await frameAction('修改任务', '画布已更新')
  await frameAction('删除任务', 'deleted')
  const authenticated = JSON.parse(await frameAction('登录并验证', 'authenticated'))
  assert.equal(authenticated.authenticated, true); assert.equal(authenticated.cookie_seen, true); assert.equal(authenticated.authorization_seen, true)
  assert.equal(await frame.locator('body').evaluate(() => document.cookie), '')
  await frameAction('发送 XHR', 'xhr')
  await frameAction('触发后端错误', '500')
  await frameAction('读取任务', '初始任务')
  report.checks.push('Sandboxed Vue page completes real GET/POST/PATCH/DELETE, explicit bearer + HttpOnly session cookie, XHR, 500 and recovery')
  await mark('Business CRUD authentication XHR and error recovery complete')
  await screenshot('integration-preview-1440.png')
  const initialRequests = await history()
  assert.ok(['GET', 'POST', 'PATCH', 'DELETE'].every(method => initialRequests.some(item => item.method === method)))
  assert.ok(initialRequests.some(item => item.status_code === 500))
  assert.ok(initialRequests.every(item => item.duration_ms >= 0 && item.created_at && item.source === 'preview'))
  const previewCapability = (await api('POST', prefix() + '/preview', {})).api_proxy_url
  await panel.getByRole('tab', { name: '接口调试', exact: true }).click()
  await panel.getByRole('button', { name: '刷新请求历史', exact: true }).click()
  const requestHistory = panel.getByRole('complementary', { name: '接口请求历史' })
  await wait(async () => (await requestHistory.getByRole('button').count()) >= initialRequests.length + 1, 'visible real request history')
  await requestHistory.getByRole('button', { name: /GET.*500/ }).click()
  await wait(async () => (await panel.getByLabel('接口响应内容').textContent()).includes('可恢复的示例错误'), 'history detail')
  await panel.getByRole('button', { name: '请求详情', exact: true }).click()
  await wait(async () => (await panel.getByLabel('接口响应内容').textContent()).includes('/api/error'), 'history request detail')
  await panel.getByLabel('请求方法', { exact: true }).selectOption('POST')
  await panel.getByLabel('接口路径', { exact: true }).fill('/api/items')
  await panel.getByLabel('请求头 JSON', { exact: true }).fill('{"Content-Type":"application/json"}')
  await panel.getByLabel('请求体', { exact: true }).fill('{"title":"手动接口创建"}')
  await panel.getByRole('button', { name: '发送请求', exact: true }).click()
  await wait(async () => (await panel.getByLabel('接口响应内容').textContent()).includes('手动接口创建'), 'manual POST response')
  const manual = (await history()).find(item => item.source === 'manual' && item.method === 'POST')
  assert.equal(manual.status_code, 201)
  report.checks.push('Real request history exposes status, duration and details; manual POST writes to the same backend')
  await mark('History and manual request verified')
  await screenshot('integration-requests-1440.png')
  await panel.getByRole('tab', { name: '日志', exact: true }).click()
  const logs = panel.getByLabel('前后端运行日志')
  await wait(async () => { const text = await logs.textContent(); return text.includes('backend/server.py') && text.includes('frontend/dev.mjs') }, 'both actual process logs')
  const sourceButton = panel.getByRole('button', { name: 'backend/server.py:22', exact: true })
  await sourceButton.waitFor(); await sourceButton.click()
  await wait(async () => (await page.locator('.monaco-editor').count()) > 0, 'source location opened in editor')
  report.checks.push('Both owned process logs arrive through incremental run cursors and backend source link opens Monaco')
  await screenshot('integration-logs-1440.png')
  const backendPid = running.services.backend.pid, frontendPid = running.services.frontend.pid
  await panel.getByRole('button', { name: '前端重启', exact: true }).click()
  running = await wait(async () => { const value = await state(); return value.status === 'running' && value.services.frontend.pid !== frontendPid ? value : null }, 'frontend-only restart')
  assert.equal(running.services.backend.pid, backendPid)
  report.checks.push('Frontend-only restart replaces its owned process and preserves backend PID')
  await mark('Single-service restart verified')
  await panel.getByRole('tab', { name: '页面预览', exact: true }).click()
  const sourcePath = join(fixture, 'frontend/src/main.ts')
  await writeFile(sourcePath, (await readFile(sourcePath, 'utf8')).replace('联调示例 · v1', '联调示例 · v2'))
  await panel.getByRole('button', { name: '刷新预览', exact: true }).click()
  await page.frameLocator('iframe[title="前后端联调页面预览"]').getByRole('heading', { name: '联调示例 · v2', exact: true }).waitFor()
  report.checks.push('Changing real TypeScript source and explicitly refreshing displays the updated compiled Vue page')
  const beforeRestart = running
  await panel.getByRole('button', { name: '整体重启', exact: true }).click()
  running = await wait(async () => { const value = await state(); return value.status === 'running' && value.id !== beforeRestart.id ? value : null }, 'whole group restart', 60000)
  assert.notEqual(running.services.backend.pid, beforeRestart.services.backend.pid)
  assert.notEqual(running.services.frontend.pid, beforeRestart.services.frontend.pid)
  report.checks.push('Overall restart creates a new session and replaces both owned project processes')
  await mark('Group restart verified')
  await panel.getByRole('tab', { name: '页面预览', exact: true }).click()
  await page.frameLocator('iframe[title="前后端联调页面预览"]').getByRole('heading', { name: '联调示例 · v2', exact: true }).waitFor()
  for (const width of [768, 390]) {
    await page.setViewportSize({ width, height: 900 }); await panel.getByRole('tab', { name: '接口调试', exact: true }).click(); await pause(200)
    await screenshot(`integration-requests-${width}.png`)
    report['geometry' + width] = await panel.evaluate(element => { const r = element.getBoundingClientRect(); return { x: r.x, width: r.width, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth } })
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  report.requests = await history()
  const afterRestartCapability = (await api('POST', prefix() + '/preview', {})).api_proxy_url
  assert.equal((await fetch(previewCapability + 'api/items')).status, 410, 'whole restart revokes the prior session capability')
  assert.equal((await fetch(afterRestartCapability + 'api/items')).status, 200, 'current preview capability is live before stop')
  await panel.getByRole('button', { name: '停止', exact: true }).click()
  await sessionStatus('stopped')
  await wait(async () => !await listening(ports.frontend) && !await listening(ports.backend), 'both project ports released')
  const stale = await fetch(afterRestartCapability + 'api/items'); assert.equal(stale.status, 410)
  await wait(async () => (await panel.locator('iframe').count()) === 0, 'UI clears preview after the stopped snapshot', 4000)
  report.checks.push('Overall stop releases both owned ports, removes iframe and immediately invalidates old capability')
  await page.reload(); await openPanel(); await panel.getByRole('tab', { name: '配置', exact: true }).click()
  await wait(async () => (await panel.getByLabel('前端服务地址', { exact: true }).inputValue()) === frontend, 'configuration retained after reload')
  assert.deepEqual((await api('GET', prefix() + '/config')).config.frontend.env, config.frontend.env)
  assert.equal((await state()).status, 'stopped')
  report.checks.push('Persisted project configuration survives full IDE reload without auto-starting processes')
  assert.deepEqual(report.sentPrompts, [])
  report.success = true
  }
} catch (error) {
  report.success = false; report.failure = error.stack || String(error)
  if (projectId) { try { report.failureState = await state(); report.failureHistory = await history() } catch {} }
  if (page) { try { await screenshot('failure.png'); await writeFile(join(out, 'failure.html'), await page.content()) } catch {} }
  console.error(report.failure)
} finally {
  if (projectId) { try { const value = await api('POST', prefix() + '/stop', { role: 'all' }, 202); if (value.session) await sessionStatus('stopped', 15000) } catch (error) { report.errors.push('cleanup integration: ' + error.message) } }
  if (browser) await browser.close().catch(() => {})
  for (const proc of children.slice().reverse()) {
    if (!proc || proc.exitCode !== null) continue
    proc.stdin.write('stop\n'); await wait(() => proc.exitCode !== null, 'owned host graceful exit', 15000).catch(async () => {
      // The PIDs below were created and recorded by this script only.
      if (process.platform === 'win32') await new Promise(done => { const killer = spawn('taskkill.exe', ['/PID', String(proc.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); killer.once('exit', done) })
      else proc.kill('SIGKILL')
    })
  }
  for (const log of logStreams) log.end()
  report.remainingListeners = {}
  for (const [name, port] of Object.entries(ports)) report.remainingListeners[name] = await listening(port)
  if (Object.values(report.remainingListeners).some(Boolean)) report.success = false
  report.finishedAt = new Date().toISOString()
  await writeFile(join(out, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ success: report.success, out, checks: report.checks.length, failure: report.failure, remainingListeners: report.remainingListeners }, null, 2))
  if (!report.success) process.exitCode = 1
}
