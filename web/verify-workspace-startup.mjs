import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

// Real App, Vue, Monaco and Chrome. All API/WS traffic is isolated with fixtures.
// No real project, provider, terminal or settings request can reach the backend.
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || '../.tmp/workspace-startup-browser')
await fs.mkdir(output, { recursive: true })
const appSource = await (await fetch(`${base}/src/App.vue`)).text()
const vuePath = appSource.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'A Vite development server is required')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const gates = { lead: deferred(), lsp: deferred(), background: deferred() }
const user = { id: 'startup-fixture-user', username: 'offline-fixture', role: 'admin' }
const project = { id: 'startup-fixture-project', name: 'Startup 工作区', root_path: 'E:/offline-fixture/startup', available: true, allow_direct_write: false, git: { is_repo: false } }
const session = { id: 'startup-fixture-lead', code_project_id: project.id, model: 'offline-fixture', permission_mode: 'read-only', status: 'open', busy: false, web_enabled: false }
const source = Array.from({ length: 220 }, (_, index) => index === 0 ? 'export class StartupExample {' : index === 219 ? '}' : `  method${index}() { return "line ${index}"; }`).join('\n')
const fixtureFile = filename => ({ path: filename, name: filename, text: filename === 'main.js' ? source : `export const name = "${filename}";\n`, sha1: `fixture-${filename}`, lang: 'javascript', uri: `file:///${project.root_path}/${filename}`, editable: true, total_lines: filename === 'main.js' ? 220 : 2, secret: false })
const files = ['helper.js', 'slow.js', 'main.js']
const checks = [], errors = [], unexpected = [], forbidden = [], calls = [], screenshots = [], measurements = {}
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page
const count = suffix => calls.filter(call => call.path === suffix).length
const snapshotCalls = () => ({ list: count('/api/v1/code/projects'), lead: count(`/api/v1/code/projects/${project.id}/session`), lsp: count(`/api/v1/code/projects/${project.id}/lsp`), diagnostics: count(`/api/v1/code/projects/${project.id}/diagnostics`) })
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
  await context.addInitScript(({ projectId, userId, fileNames, sessionId }) => {
    localStorage.setItem('code-agent:view', 'code')
    localStorage.setItem('code-agent:nav-collapsed', 'true')
    localStorage.setItem('code-agent-code-left', '230')
    localStorage.setItem('code-agent-code-right', '310')
    localStorage.setItem(`code-agent-active-project-v2:${userId}`, projectId)
    localStorage.setItem(`code-agent-workbench-v1:${userId}`, JSON.stringify({ version: 1, scopes: [{ projectId, tree: 'main', activeKey: JSON.stringify([projectId, 'main', 'main.js']), documents: fileNames.map(filename => ({ path: filename, dirty: false, sha1: `fixture-${filename}`, file: { path: filename, lang: 'javascript', editable: true } })) }] }))
    // Accelerate only the diagnostics interval to exercise several poll cycles offline.
    const nativeInterval = window.setInterval
    window.setInterval = (fn, milliseconds, ...args) => nativeInterval(fn, milliseconds === 5000 ? 100 : milliseconds, ...args)
    const NativeSocket = window.WebSocket
    window.fixtureSocketFrames = []
    window.WebSocket = class FixtureSocket extends EventTarget {
      static OPEN = 1; static CLOSED = 3; static CONNECTING = 0
      constructor(value, protocols) {
        super()
        const url = new URL(value, location.href)
        if (!url.pathname.startsWith('/api/')) return new NativeSocket(value, protocols)
        this.url = url.href; this.readyState = 0; this.bufferedAmount = 0
        queueMicrotask(() => {
          if (this.readyState === 3) return
          this.readyState = 1
          this.emit('open', new Event('open'))
          this.emit('message', new MessageEvent('message', { data: JSON.stringify({ type: 'session_meta', seq: 1, payload: { id: sessionId, busy: false, model: 'offline-fixture' } }) }))
        })
      }
      emit(name, event) { this.dispatchEvent(event); this[`on${name}`]?.(event) }
      send(raw) {
        const frame = JSON.parse(raw)
        window.fixtureSocketFrames.push({ path: new URL(this.url).pathname, type: frame.type })
        if (frame.type === 'ping') queueMicrotask(() => this.emit('message', new MessageEvent('message', { data: JSON.stringify({ type: 'pong', payload: {} }) })))
      }
      close() { this.readyState = 3 }
    }
  }, { projectId: project.id, userId: user.id, fileNames: files, sessionId: session.id })
  page = await context.newPage()
  page.setDefaultTimeout(20000)
  page.on('pageerror', error => errors.push(error.message))
  page.on('dialog', dialog => { unexpected.push(`dialog:${dialog.type()}`); void dialog.dismiss() })
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname
    if (url.origin !== origin) { unexpected.push(`external:${url.origin}${pathname}`); return route.abort() }
    // Other App pages are intentionally tiny, so this regression isolates workspace retention.
    const otherPage = pathname.match(/^\/src\/views\/(Chat|Memory|Knowledge|Tools|Usage|Automations|Audit|Settings)View\.vue$/)
    if (otherPage) return route.fulfill({ contentType: 'text/javascript', body: `import {h} from ${JSON.stringify(vuePath)};export default {setup(){return()=>h('section',{'data-fixture-page':${JSON.stringify(otherPage[1])},style:'padding:32px'},[h('h1',${JSON.stringify(otherPage[1] + ' fixture')}),h('input',{'aria-label':'其他页面搜索',placeholder:'offline fixture'})])}}` })
    if (!pathname.startsWith('/api/') && pathname !== '/healthz') return route.continue()
    calls.push({ method: request.method(), path: pathname, query: url.search, at: Date.now() })
    const reply = data => route.fulfill({ contentType: 'application/json; charset=utf-8', body: JSON.stringify(data) })
    if (pathname === '/healthz') return reply({ ok: true })
    if (pathname === '/api/v1/auth/me') return reply(user)
    if (pathname === '/api/v1/web') return reply({ available: false, reason: 'Offline fixture' })
    if (pathname === '/api/v1/code/meta') return reply({ git_available: false, native_picker: { available: false }, templates: ['empty'], projects_root: '' })
    if (pathname === '/api/v1/code/projects' && request.method() === 'GET') return reply([project])
    const match = pathname.match(/^\/api\/v1\/code\/projects\/([^/]+)(.*)$/)
    if (match?.[1] === project.id) {
      const suffix = match[2]
      if (!suffix && request.method() === 'GET') return reply(project)
      if (suffix === '/session' && request.method() === 'POST') { await gates.lead.promise; return reply(session) }
      if (request.method() !== 'GET') { forbidden.push(`${request.method()} ${pathname}`); return route.fulfill({ status: 403, body: '{}' }) }
      if (suffix === '/lsp') { await gates.lsp.promise; return reply({ servers: [] }) }
      if (suffix === '/diagnostics') return reply({ seq: 1, counts: { error: 0, warning: 0 }, items: [], files: {}, symbols: {} })
      if (suffix === '/tree') return reply({ dirs: [], files: files.map(name => ({ name, path: name, size: 100 })), truncated: false })
      if (suffix === '/file') {
        const filename = url.searchParams.get('path')
        if (filename !== 'main.js') await gates.background.promise
        return reply(fixtureFile(filename))
      }
      if (suffix === '/agents') return reply({ lead: { session_id: session.id, project_id: project.id, role: 'lead', depth: 0, title: 'Lead', state: 'idle' }, agents: [], limits: { max_depth: 3, max_parallel: 4 }, running: 0, queued: 0 })
      if (suffix === '/files') return reply({ files: files.map(filename => ({ path: filename })), truncated: false })
    }
    const sessionRoute = pathname.match(/^\/api\/v1\/sessions\/([^/]+)(.*)$/)
    if (sessionRoute?.[1] === session.id && request.method() === 'GET') {
      if (!sessionRoute[2]) return reply(session)
      if (sessionRoute[2] === '/events') return reply([])
      if (sessionRoute[2] === '/checkpoints') return reply({ items: [], busy: false })
      if (/\/approvals?/.test(sessionRoute[2])) return reply({})
    }
    unexpected.push(`${request.method()} ${pathname}`)
    return route.fulfill({ status: 501, contentType: 'application/json', body: JSON.stringify({ detail: 'Not covered by offline fixture' }) })
  })
  const started = Date.now()
  await page.goto(base, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '文件 main.js', exact: true }).waitFor()
  await page.locator('.monaco-editor').waitFor()
  await page.waitForFunction(() => !document.querySelector('[aria-label="切换项目"]')?.disabled)
  const initial = await page.evaluate(async () => {
    const mod = await import('/src/lib/monacoSetup.js'), monaco = mod.ensureMonaco()
    const editor = monaco.editor.getEditors().find(editor => editor.getModel()?.uri.toString().endsWith('/main.js'))
    if (!editor) throw new Error('Fixture editor is missing')
    window.fixtureEditor = editor; window.fixtureModel = editor.getModel()
    return { text: editor.getModel().getValue(), readonly: editor.getOption(monaco.editor.EditorOption.readOnly), phase: document.querySelector('.code').__vueParentComponent.setupState.projectAccess.phase.value }
  })
  measurements.firstUsableMs = Date.now() - started
  assert.equal(initial.phase, 'idle')
  assert.equal(initial.readonly, false)
  assert.equal(initial.text, source)
  assert.match(await page.locator('.status').innerText(), /准备会话/)
  assert.equal(count(`/api/v1/code/projects/${project.id}/session`), 1)
  assert.equal(count(`/api/v1/code/projects/${project.id}/lsp`), 1)
  assert.ok(calls.some(call => call.path.endsWith('/file') && call.query.includes('helper.js')))
  checks.push('project selection is idle and active editor/file tree are usable while Lead, LSP and background files are blocked')
  const hiddenTools = calls.filter(call => /\/(patches|tasks|parallel|git)(\/|$)/.test(call.path))
  assert.deepEqual(hiddenTools, [])
  checks.push('unvisited ledger, task, parallel and Git panels make no startup requests')
  await page.screenshot({ path: `${output}/workspace-first-usable.png` }); screenshots.push('workspace-first-usable.png')
  gates.lead.resolve(); gates.lsp.resolve()
  await page.waitForFunction(() => document.querySelector('.team-trigger strong')?.textContent === 'Lead')
  const saved = await page.evaluate(() => {
    const editor = window.fixtureEditor, model = editor.getModel()
    editor.executeEdits('offline-fixture', [{ range: { startLineNumber: 2, startColumn: 1, endLineNumber: 2, endColumn: 1 }, text: '// unsaved browser fixture\n', forceMoveMarkers: true }])
    editor.setPosition({ lineNumber: 100, column: 8 }); editor.setScrollTop(1400); editor.focus()
    return { value: model.getValue(), scrollTop: editor.getScrollTop(), position: editor.getPosition(), uri: model.uri.toString() }
  })
  await page.waitForFunction(() => document.querySelector('.etab.active')?.textContent || document.querySelector('.editor-tabs')?.textContent.includes('main.js'))
  const beforeSwitch = snapshotCalls()
  await page.locator('.sidebar .nav-item[title="设置"]').click()
  await page.locator('[data-fixture-page="Settings"]').waitFor()
  assert.equal(await page.locator('.code').isVisible(), false)
  const hiddenDiagnostics = count(`/api/v1/code/projects/${project.id}/diagnostics`)
  const keyboard = await page.evaluate(() => {
    const event = new KeyboardEvent('keydown', { key: 'p', ctrlKey: true, bubbles: true, cancelable: true })
    window.dispatchEvent(event)
    return { prevented: event.defaultPrevented, quickOpen: !!document.querySelector('[aria-label="快速打开文件"]') }
  })
  assert.deepEqual(keyboard, { prevented: false, quickOpen: false })
  await page.waitForTimeout(650)
  assert.equal(count(`/api/v1/code/projects/${project.id}/diagnostics`), hiddenDiagnostics)
  checks.push('hidden workspace pauses diagnostics polling and does not intercept another page Ctrl+P')
  gates.background.resolve()
  await page.waitForTimeout(100)
  const reentryStart = Date.now()
  await page.locator('.sidebar .nav-item[title="代码"]').click()
  await page.locator('.code').waitFor({ state: 'visible' })
  const retained = await page.evaluate(() => ({ sameEditor: window.fixtureEditor.getDomNode().isConnected, sameModel: window.fixtureEditor.getModel() === window.fixtureModel, value: window.fixtureEditor.getValue(), scrollTop: window.fixtureEditor.getScrollTop(), position: window.fixtureEditor.getPosition() }))
  measurements.reentryMs = Date.now() - reentryStart
  assert.equal(retained.sameEditor, true)
  assert.equal(retained.sameModel, true)
  assert.equal(retained.value, saved.value)
  assert.deepEqual(retained.position, saved.position)
  assert.ok(Math.abs(retained.scrollTop - saved.scrollTop) < 2, `scroll changed ${saved.scrollTop} → ${retained.scrollTop}`)
  const afterSwitch = snapshotCalls()
  assert.equal(afterSwitch.list, beforeSwitch.list)
  assert.equal(afterSwitch.lead, beforeSwitch.lead)
  assert.equal(afterSwitch.lsp, beforeSwitch.lsp)
  checks.push('returning from settings retains the same editor/model, unsaved draft and reading position without reinitializing project/Lead/LSP')
  await page.screenshot({ path: `${output}/workspace-retained-draft.png` }); screenshots.push('workspace-retained-draft.png')
  await page.locator('.sidebar .nav-item[title="用量"]').click()
  await page.locator('[data-fixture-page="Usage"]').waitFor()
  await page.locator('.sidebar .nav-item[title="代码"]').click()
  await page.locator('.code').waitFor({ state: 'visible' })
  assert.equal(await page.evaluate(() => window.fixtureEditor.getModel() === window.fixtureModel && window.fixtureEditor.getValue().includes('unsaved browser fixture')), true)
  assert.equal(snapshotCalls().lead, beforeSwitch.lead)
  assert.equal(snapshotCalls().list, beforeSwitch.list)
  assert.equal(snapshotCalls().lsp, beforeSwitch.lsp)
  checks.push('repeated navigation through another module preserves workspace state without reopening')
  const wsFrames = await page.evaluate(() => window.fixtureSocketFrames)
  assert.equal(wsFrames.every(frame => frame.type === 'ping'), true)
  assert.deepEqual(forbidden, []); assert.deepEqual(unexpected, []); assert.deepEqual(errors, [])
  checks.push('all traffic stays inside fixtures with no writes, model executions, native dialogs or runtime errors')
} finally {
  Object.values(gates).forEach(gate => gate.resolve())
  await fs.writeFile(`${output}/report.json`, JSON.stringify({ checks, measurements, errors, unexpected, forbidden, calls, screenshots }, null, 2))
  await browser.close()
}
console.log(JSON.stringify({ passed: checks.length, measurements, output }, null, 2))
