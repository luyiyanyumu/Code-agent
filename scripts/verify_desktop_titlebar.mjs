// Real staged EXE/WebView2 acceptance, isolated from the user's installed window.
// node scripts/verify_desktop_titlebar.mjs <stage-root> <out> [--pause-before-close] [--manual-only]
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import net from 'node:net'

assert.ok(process.argv[2] && process.argv[3], 'Provide a staged app root and evidence directory')
const root = resolve(process.argv[2]), out = resolve(process.argv[3]), manualOnly = process.argv.includes('--manual-only'), inspect = manualOnly || process.argv.includes('--pause-before-close')
await mkdir(out, { recursive: true })
assert.ok((await readFile(join(root, 'web/dist/index.html'), 'utf8')).includes('<html'), 'Build the staged production frontend first')
const pause = ms => new Promise(done => setTimeout(done, ms)), started = performance.now()
async function wait(check, label, timeout = 15000) {
  const until = performance.now() + timeout; let last
  while (performance.now() < until) {
    try { const result = await check(); if (result) return result } catch (error) { last = error }
    await pause(120)
  }
  throw Error(`${label}: ${last?.message || 'timed out'}`)
}
async function port() {
  return new Promise((done, reject) => { const server = net.createServer(); server.once('error', reject); server.listen(0, '127.0.0.1', () => { const value = server.address().port; server.close(() => done(value)) }) })
}
async function listening(value) {
  return new Promise(done => { const socket = net.connect({ host: '127.0.0.1', port: value }); let settled = false; const finish = result => { if (settled) return; settled = true; socket.destroy(); done(result) }; socket.once('connect', () => finish(true)); socket.once('error', () => finish(false)); socket.setTimeout(500, () => finish(false)) })
}
function gone(pid) { if (!pid) return true; try { process.kill(pid, 0); return false } catch (error) { if (error.code === 'ESRCH') return true; throw error } }
const backend = await port(), frontend = await port(), debug = await port(), diagnostics = join(out, 'native.json')
const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|ANTHROPIC_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|JAVA_HOME|JDK_HOME|JDK_JAVA_OPTIONS|JAVA_TOOL_OPTIONS|_JAVA_OPTIONS|npm_)/i.test(name)))
Object.assign(env, { PATH: [join(process.env.SystemRoot, 'System32'), process.env.SystemRoot, join(process.env.SystemRoot, 'System32/WindowsPowerShell/v1.0')].join(';'), CODE_AGENT_HOME: join(out, 'user-data'), CODE_AGENT_PROVIDER: 'offline', CODE_AGENT_AUTH_ENABLED: 'false', PYTHONDONTWRITEBYTECODE: '1', CODE_AGENT_LAUNCHER_BACKEND_PORT: String(backend), CODE_AGENT_LAUNCHER_FRONTEND_PORT: String(frontend) })
const report = { passed: false, mode: manualOnly ? 'manual-only' : 'automatic', root, out, ports: { backend, frontend, debug }, checks: [], errors: [], sentPrompts: [], screenshots: [], startedAt: new Date().toISOString() }
let app, exited, browser, page, secondary, closeRequested = false
const readDiagnostics = async () => JSON.parse((await readFile(diagnostics, 'utf8')).replace(/^\uFEFF/, ''))
async function stage(name) { report.stage = name; await writeFile(join(out, 'progress.json'), JSON.stringify({ stage: name, nativePid: report.nativePid, controllerPid: report.controllerPid, checks: report.checks, at: new Date().toISOString() }, null, 2)) }
async function rpc(method, params = {}) {
  return page.evaluate(({ method, params }) => new Promise((done, reject) => {
    const id = `titlebar-${Date.now()}-${Math.random()}`
    const timer = setTimeout(() => { window.chrome.webview.removeEventListener('message', receive); reject(Error(`Native ${method} timed out`)) }, 8000)
    function receive(event) { const value = event.data; if (value?.channel !== 'code-agent-desktop' || value.id !== id) return; clearTimeout(timer); window.chrome.webview.removeEventListener('message', receive); value.ok ? done(value.result) : reject(Error(value.error)) }
    window.chrome.webview.addEventListener('message', receive)
    window.chrome.webview.postMessage({ channel: 'code-agent-desktop', id, method, params })
  }), { method, params })
}
async function screenshot(name) { const path = join(out, name); await page.screenshot({ path }); report.screenshots.push(path) }
async function geometry(label) {
  const state = await rpc('window-state')
  const dom = await page.evaluate(() => {
    const rect = element => { const r = element.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom } }
    const header = document.querySelector('.ide-titlebar'), controls = document.querySelector('.desktop-window-controls')
    return { dpr: window.devicePixelRatio, width: window.innerWidth, height: window.innerHeight, header: header && header.getBoundingClientRect().height > 0 ? rect(header) : null, controls: controls ? rect(controls) : null, buttons: controls ? [...controls.querySelectorAll('button')].map(button => ({ label: button.getAttribute('aria-label'), ...rect(button) })) : [] }
  })
  assert.equal(state.integratedTitlebar, true, `${label}: native titlebar capability`)
  assert.equal(state.integratedFrame, true, `${label}: native caption should be removed`)
  assert.equal(state.nativeDragRegions, true, `${label}: native drag regions`)
  assert.ok(dom.controls && dom.buttons.length === 3, `${label}: exactly three global controls`)
  assert.equal(Math.round(dom.controls.y), 0); assert.equal(Math.round(dom.controls.height), 35)
  assert.ok(dom.controls.right <= dom.width + 1 && dom.controls.x >= 0, `${label}: controls visible`)
  if (dom.header) { assert.equal(Math.round(dom.header.height), 35); assert.equal(Math.round(dom.header.y), 0) }
  for (const button of dom.buttons) assert.ok(button.width >= 40 && button.right <= dom.width + 1, `${label}: accessible caption target`)
  const captionGap = state.bounds.height - state.clientSize.height
  assert.ok(captionGap >= 0 && captionGap <= Math.ceil(24 * dom.dpr), `${label}: native frame gap ${captionGap} must not include another caption`)
  return { native: state, dom }
}
async function inspectPause() {
  // A root agent may inspect this owned fixture through Sky and finish via stdin
  // ("finish\n") or a finish-now marker; never wait beyond the EXE's 120s budget.
  const timeout = Math.min(manualOnly ? 65000 : 30000, Math.max(0, 100000 - (performance.now() - started)))
  report.inspectionBudgetMs = Math.round(timeout)
  await writeFile(join(out, 'pause-ready.json'), JSON.stringify({ nativePid: app.pid, controllerPid: report.controllerPid, root, diagnostics, debug, timeoutMs: Math.round(timeout), state: await rpc('window-state') }, null, 2))
  console.log(`TITLEBAR_PAUSE_READY ${out}`)
  if (!timeout) return
  await new Promise(done => {
    let timer, poll, complete = false
    const finish = () => { if (complete) return; complete = true; clearTimeout(timer); clearInterval(poll); process.stdin.off('data', input); process.stdin.pause(); done() }
    const input = value => { if (String(value).includes('finish')) finish() }
    process.stdin.on('data', input); process.stdin.resume()
    timer = setTimeout(finish, timeout)
    poll = setInterval(() => readFile(join(out, 'finish-now')).then(finish).catch(() => {}), 250)
  })
}
async function normalClose() {
  if (!app || app.exitCode !== null) return
  if (page && !page.isClosed()) {
    closeRequested = true
    await page.getByRole('button', { name: '关闭窗口', exact: true }).click().catch(error => { if (!page.isClosed()) throw error })
  }
  await wait(() => app.exitCode !== null, 'window-close normal native exit', 18000)
  await exited
}

try {
  await stage('Starting isolated native application')
  // Manual-only is explicitly an on-screen inspection. Background services keep
  // their own hidden-process policy; only this owned GUI must be shown for Sky.
  app = spawn(join(root, 'Code-agent.exe'), ['--diagnostics-file', diagnostics, '--diagnostics-port', String(debug), '--diagnostics-exit-after-ms', '120000'], { cwd: out, env, windowsHide: !manualOnly, stdio: 'ignore' })
  exited = new Promise((done, reject) => { app.once('exit', (code, signal) => done({ code, signal })); app.once('error', reject) })
  const ready = await wait(async () => { if (app.exitCode !== null) throw Error(`Native exited: ${app.exitCode}`); const value = await readDiagnostics(); if (value.state === 'failed') throw Error(value.error); return value.state === 'ready' ? value : null }, 'native startup', 45000)
  assert.equal(ready.bundledRuntime, true); assert.equal(ready.integratedWorkbench, true); assert.equal(ready.windowEnabled, true)
  report.nativePid = app.pid; report.controllerPid = ready.controllerPid; report.windowHandle = ready.windowHandle
  const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  browser = await (pw.default || pw).chromium.connectOverCDP(`http://127.0.0.1:${debug}`)
  page = await wait(() => browser.contexts().flatMap(context => context.pages()).find(value => value.url().startsWith(`http://127.0.0.1:${frontend}/`)), 'embedded production workbench')
  page.setDefaultTimeout(15000)
  page.on('pageerror', error => report.errors.push(error.message))
  page.on('request', request => { const path = new URL(request.url()).pathname; if (request.method() === 'POST' && path.endsWith('/prompt')) report.sentPrompts.push({ transport: 'http', path }) })
  page.on('websocket', socket => socket.on('framesent', frame => { try { if (JSON.parse(String(frame.payload)).type === 'prompt') report.sentPrompts.push({ transport: 'ws' }) } catch {} }))
  await page.getByRole('group', { name: '窗口控制', exact: true }).waitFor()
  await page.getByRole('menubar', { name: 'IDE 菜单', exact: true }).waitFor()
  await wait(async () => (await rpc('window-state')).integratedFrame, 'native frame handshakes with production controls')
  report.normal = await geometry('normal')
  report.checks.push('Real production web and native host render one 35px title row with exactly three controls and no system caption')
  await screenshot('native-titlebar-normal.png'); await stage('Integrated native titlebar ready')
  if (!manualOnly) {
  await page.getByRole('button', { name: '最大化窗口', exact: true }).click()
  await wait(async () => (await rpc('window-state')).maximized, 'native maximized state')
  await page.getByRole('button', { name: '还原窗口', exact: true }).waitFor()
  report.maximized = await geometry('maximized')
  const max = report.maximized.native.bounds, work = report.maximized.native.workingArea, tolerance = Math.ceil(16 * report.maximized.dom.dpr)
  assert.ok(Math.abs(max.x - work.x) <= tolerance && Math.abs(max.y - work.y) <= tolerance, 'Maximized origin follows the monitor work area')
  assert.ok(Math.abs(max.width - work.width) <= tolerance && Math.abs(max.height - work.height) <= tolerance, 'Maximized bounds preserve the taskbar work area')
  await screenshot('native-titlebar-maximized.png')
  await page.getByRole('button', { name: '还原窗口', exact: true }).click()
  await wait(async () => !(await rpc('window-state')).maximized, 'native restore state')
  await page.getByRole('button', { name: '最大化窗口', exact: true }).waitFor()
  report.restored = await geometry('restored')
  for (const key of ['x', 'y', 'width', 'height']) assert.ok(Math.abs(report.restored.native.bounds[key] - report.normal.native.bounds[key]) <= 2, `Restored ${key}`)
  report.checks.push('Maximize/restore buttons and native state agree, preserve restore bounds and fit the taskbar working area')
  await stage('Maximize and restore verified')
  await page.getByRole('button', { name: '最小化窗口', exact: true }).click()
  await wait(async () => (await rpc('window-state')).minimized, 'native minimized state')
  secondary = spawn(join(root, 'Code-agent.exe'), [], { cwd: out, env, windowsHide: true, stdio: 'ignore' })
  secondary.on('error', error => report.errors.push('Secondary launch: ' + error.message))
  report.secondaryPid = secondary.pid
  await wait(() => secondary.exitCode !== null, 'second EXE exits after activating existing window', 8000)
  assert.equal(secondary.exitCode, 0)
  await wait(async () => !(await rpc('window-state')).minimized, 'existing native window restored by second launch')
  assert.equal((await readDiagnostics()).pid, app.pid); assert.equal((await readDiagnostics()).controllerPid, report.controllerPid)
  report.checks.push('Minimize works and a second launch restores the same native window/controller without starting duplicate services')
  await stage('Minimize and single-instance activation verified')
  await page.getByRole('button', { name: '打开设置', exact: true }).click()
  await page.getByRole('heading', { name: '设置', exact: true }).waitFor()
  assert.equal(await page.getByRole('group', { name: '窗口控制', exact: true }).count(), 1)
  assert.equal(await page.getByRole('group', { name: '窗口控制', exact: true }).getByRole('button').count(), 3)
  report.settings = await geometry('settings')
  const back = await page.getByRole('button', { name: '返回代码工作区', exact: true }).boundingBox()
  assert.ok(back.x + back.width <= report.settings.dom.controls.x || back.y >= report.settings.dom.controls.bottom, 'Settings navigation stays clear of window controls')
  await screenshot('native-titlebar-settings.png')
  await page.getByRole('button', { name: '返回代码工作区', exact: true }).click()
  await page.getByRole('menubar', { name: 'IDE 菜单', exact: true }).waitFor()
  report.checks.push('Settings retains the same global caption controls without obscuring navigation')
  await stage('Settings titlebar verified')
  } else {
    await stage('Manual-only inspection: automatic maximize/minimize/settings matrix already verified separately')
  }
  const desktop = await rpc('status'); report.services = desktop.services.map(({ id, pid, state }) => ({ id, pid, state }))
  assert.ok(report.services.every(value => value.state === 'healthy')); assert.deepEqual(report.sentPrompts, []); assert.deepEqual(report.errors, [])
  if (inspect) { await stage('Paused for root Sky inspection'); await inspectPause(); report.afterInspection = await rpc('window-state') }
  assert.ok(performance.now() - started < 112000, 'Close must be explicitly requested before the diagnostics auto-exit deadline')
  await stage('Closing through the real titlebar button')
  const closingAt = performance.now(); await normalClose(); report.closeMs = Math.round(performance.now() - closingAt)
  assert.equal(app.exitCode, 0); assert.equal(closeRequested, true)
  await wait(() => gone(app.pid) && gone(report.controllerPid) && report.services.every(service => gone(service.pid)), 'owned native processes exit', 15000)
  await wait(async () => !(await listening(backend)) && !(await listening(frontend)), 'owned application ports released', 10000)
  report.checks.push('The integrated close button uses normal native shutdown and releases EXE/controller/service processes and ports')
  report.passed = true
} catch (error) {
  report.failure = error.stack || String(error); console.error(report.failure)
  if (page && !page.isClosed()) { await screenshot('native-titlebar-failure.png').catch(() => {}); await writeFile(join(out, 'failure.html'), await page.content()).catch(() => {}) }
  process.exitCode = 1
} finally {
  if (app?.exitCode === null) {
    await normalClose().catch(error => report.errors.push('Normal cleanup: ' + error.message))
    if (app.exitCode === null) {
      // Only the process handle created above is targeted; never touch a user app.
      if (process.platform === 'win32') await new Promise(done => { const cleanup = spawn('taskkill.exe', ['/PID', String(app.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); cleanup.once('exit', done); cleanup.once('error', done) })
      else app.kill('SIGKILL')
      report.forcedCleanup = true
    }
  }
  if (browser) await browser.close().catch(() => {})
  // WebView2 may release its CDP listener a moment after the native/controller
  // processes exit. Observe that owned port briefly; never kill by port number.
  await wait(async () => !(await listening(debug)), 'owned WebView diagnostics listener released', 5000).catch(() => {})
  report.remainingListeners = { backend: await listening(backend), frontend: await listening(frontend), diagnostics: await listening(debug) }
  if (Object.values(report.remainingListeners).some(Boolean)) report.passed = false
  report.finishedAt = new Date().toISOString(); report.elapsedMs = Math.round(performance.now() - started)
  await writeFile(join(out, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: report.passed, out, checks: report.checks.length, failure: report.failure, remainingListeners: report.remainingListeners, elapsedMs: report.elapsedMs }, null, 2))
  if (!report.passed) process.exitCode = 1
}
