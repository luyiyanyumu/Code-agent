import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'

// Real Chrome/Vue/Monaco/xterm. All API/PTY/debug/model transport is fixture-only.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const playwright = pw.default || pw, base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/ide-runtime-ui', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], screenshots = [], errors = [], unexpected = [], forbidden = [], writes = [], runRequests = [], debugRequests = [], decisions = []
let page, failure = null, apiCount = 0, errorCount = 0
const hash = text => createHash('sha1').update(text).digest('hex')
const redact = text => String(text).replace(/([?&]token=)[^&\s"']+/gi, '$1[redacted]').replace(/Bearer\s+[^\s"']+/gi, 'Bearer [redacted]').replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[redacted]')
const project = { id: 'fixture-runtime', name: 'Runtime 验收', root_path: 'E:/ide-runtime-fixture', available: true, allow_direct_write: false, git: { is_repo: false } }
const session = { id: 'session-runtime-fixture', code_project_id: project.id, permission_mode: 'read-only', model: 'fixture-model', busy: false, status: 'open', web_enabled: false }
const original = Array.from({ length: 100 }, (_, i) => i === 0 ? 'value = 1' : i === 19 ? 'other = 2' : `# runtime fixture line ${i + 1}`).join('\n')
const javaPath = 'backend/src/main/java/demo/AgentService.java'
const files = new Map([['main.py', { text: original, lang: 'python' }], ['helper.py', { text: Array.from({ length: 30 }, (_, i) => `# helper line ${i + 1}`).join('\n'), lang: 'python' }], ['notes.js', { text: 'console.log("fixture")\n', lang: 'javascript' }], [javaPath, { text: 'package demo;\n\npublic class AgentService {\n  public String greeting() { return "fixture"; }\n}\n', lang: 'java' }]])
for (const file of files.values()) file.sha1 = hash(file.text)
const fileData = filename => { const file = files.get(filename); return file ? { ...file, path: filename, name: filename, uri: `file:///${project.root_path}/${filename}`, editable: true, secret: false, start_line: 1, total_lines: file.text.split('\n').length, end_line: file.text.split('\n').length, size: Buffer.byteLength(file.text) } : null }
const inventory = () => [...files].map(([filename, file]) => ({ path: filename, name: filename, size: Buffer.byteLength(file.text) }))
const terminals = new Map(), runs = new Map(); let debug = null
const modelSettings = { provider: 'deepseek', base_url: 'https://api.deepseek.com', model_default: 'deepseek-flash', active_model: 'deepseek-flash', proxy_mode: 'system', api_key_set: true, api_key_hint: '[fixture]' }
const modelProfile = { id: 'proxy-fixture', name: 'DeepSeek fixture', provider: 'deepseek', base_url: 'https://api.deepseek.com', model_default: 'deepseek-flash', proxy_mode: 'system', api_key_set: true, api_key_hint: '[fixture]', active: true }
const proxyRequests = [], modelProbes = []
const runtimeConfigs = [
  { id: 'pytest', name: 'Fixture pytest suite', kind: 'pytest', path: 'tests', cwd: '.', available: true },
  { id: 'maven-boot', name: 'Maven · backend · spring-boot:run', kind: 'maven', script: 'spring-boot:run', cwd: 'backend', source: 'project', available: true },
  { id: 'maven-test', name: 'Maven · backend · test', kind: 'maven', script: 'test', cwd: 'backend', source: 'project', available: true },
  { id: 'maven-compile', name: 'Maven · backend · compile', kind: 'maven', script: 'compile', cwd: 'backend', source: 'project', available: true },
  { id: 'gradle-missing', name: 'Gradle · other · bootRun', kind: 'gradle', script: 'bootRun', cwd: 'other', source: 'project', available: false, reason: '未找到 Gradle，也没有项目 wrapper。' },
]
let releaseDiscovery, pendingDiscovery = 0
const discoveryGate = new Promise(resolve => { releaseDiscovery = resolve })
const proposal = { id: 'proposal-fixture', title: 'Fixture: two independent edits', path: 'main.py', new_file: false, state: 'pending', hunks: [
  { id: 'h1', status: 'pending', lines: [{ kind: 'remove', old_line: 1, text: 'value = 1\n' }, { kind: 'add', new_line: 1, text: 'value = 10\n' }] },
  { id: 'h2', status: 'pending', lines: [{ kind: 'remove', old_line: 20, text: 'other = 2\n' }, { kind: 'add', new_line: 20, text: 'other = 20\n' }] },
], counts: { pending: 2, applied: 0, rejected: 0 } }
const clone = value => JSON.parse(JSON.stringify(value))
function updateProposal() {
  const count = status => proposal.hunks.filter(h => h.status === status).length
  proposal.counts = { pending: count('pending'), applied: count('applied'), rejected: count('rejected') }
  proposal.state = proposal.counts.pending === 2 ? 'pending' : proposal.counts.pending ? 'partial' : proposal.counts.applied ? 'applied' : 'rejected'
}
function debugState(line = 3, reason = 'entry') {
  return { id: 'debug-fixture', project_id: project.id, path: 'main.py', status: 'paused', reason, seq: (debug?.seq || 0) + 1, pause_seq: (debug?.pause_seq || 0) + 1, thread_id: 1, frame_id: 100,
    stack: [{ id: 100, name: '<module>', path: 'main.py', line, column: 1 }, { id: 101, name: 'fixture_helper', path: 'helper.py', line: 6, column: 3 }],
    variables: [{ name: 'value', value: String(line), type: 'int', variablesReference: 0 }, { name: 'items', value: '[1, 2]', type: 'list', variablesReference: 201 }],
    scopes: [{ name: 'Locals', variablesReference: 200, expensive: false }], console_seq: 1, console: [{ seq: 1, category: 'stdout', output: 'fixture debug console\n' }], breakpoints: debug?.breakpoints || [], exit_code: null }
}
const browser = await playwright.chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block', permissions: ['clipboard-read', 'clipboard-write'] })
  await context.addInitScript(({ session }) => {
    localStorage.setItem('code-agent:nav-collapsed', 'true'); localStorage.setItem('code-agent-code-left', '232'); localStorage.setItem('code-agent-code-right', '350')
    const Native = window.WebSocket, sockets = [], histories = new Map(), audit = []
    const emit = (ws, type, event) => { ws.dispatchEvent(event); ws[`on${type}`]?.(event) }
    const frame = (ws, value) => { if (ws.readyState === 1) emit(ws, 'message', new MessageEvent('message', { data: JSON.stringify(value) })) }
    class FixtureSocket extends EventTarget {
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
      constructor(url, protocols) {
        super(); const u = new URL(url, location.href)
        if (!u.pathname.startsWith('/api/')) return new Native(url, protocols)
        this.url = u.href; this.readyState = 0; this.bufferedAmount = 0; this.protocol = ''; this.extensions = ''; this.binaryType = 'blob'
        this.CONNECTING = 0; this.OPEN = 1; this.CLOSING = 2; this.CLOSED = 3; this.inputBuffer = ''
        this.terminal = /\/terminals\/([^/]+)\/stream$/.exec(u.pathname)?.[1]
        this.isLsp = u.pathname.endsWith('/lsp/stream')
        const isSession = u.pathname === `/api/v1/sessions/${session.id}/stream`
        audit.push({ kind: this.terminal || isSession || this.isLsp ? 'connect' : 'blocked', pathname: u.pathname, afterSeq: Number(u.searchParams.get('after_seq') || 0) }); sockets.push(this)
        queueMicrotask(() => {
          if (this.readyState !== 0) return
          if (!this.terminal && !isSession && !this.isLsp) return this.close(4404, 'unmocked API websocket')
          this.readyState = 1; emit(this, 'open', new Event('open'))
          if (this.isLsp) return frame(this, { method: 'code-agent/ready', params: { server: 'fixture-analysis', capabilities: {}, diagnostics: {}, diagnostic_versions: {}, environment: u.searchParams.get('lang') === 'java' ? { lombok: { enabled: true, status: 'enabled', version: '1.18.46', reason: '已为 JDTLS 启用本地 Lombok JVM agent' }, workspace_data: { isolated: true } } : {} } })
          if (isSession) return frame(this, { type: 'session_meta', seq: 0, payload: session })
          if (!histories.has(this.terminal)) histories.set(this.terminal, [{ type: 'output', seq: 1, data: 'fixture terminal ready\r\n' }])
          const events = histories.get(this.terminal), after = Number(u.searchParams.get('after_seq') || 0)
          frame(this, { type: 'ready', id: this.terminal, shell: 'powershell', cwd: '.', status: 'running', first_seq: 1, next_seq: events.at(-1)?.seq || 0 })
          if (after) frame(this, events.find(event => event.seq === after)) // deliberate replay duplicate
          events.filter(event => event.seq > after).forEach(event => frame(this, event))
        })
      }
      send(text) {
        if (this.readyState !== 1) throw new DOMException('closed', 'InvalidStateError')
        const data = JSON.parse(text); audit.push({ kind: 'send', pathname: new URL(this.url).pathname, frame: data })
        if (this.isLsp) { if (data.id != null) queueMicrotask(() => frame(this, { jsonrpc: '2.0', id: data.id, result: null })); return }
        if (!this.terminal) { if (data.type === 'ping') queueMicrotask(() => frame(this, { type: 'pong', seq: 0, payload: {} })); else audit.push({ kind: 'blocked-frame' }); return }
        if (data.type === 'resize') return
        if (data.type !== 'input') { audit.push({ kind: 'blocked-frame' }); return }
        this.inputBuffer += data.data
        if (!/[\r\n]/.test(this.inputBuffer)) return
        const history = histories.get(this.terminal), event = { type: 'output', seq: history.at(-1).seq + 1, data: `${this.inputBuffer.replace(/[\r\n]/g, '')}\r\nterminal fixture output\r\n` }
        this.inputBuffer = ''; history.push(event); queueMicrotask(() => frame(this, event))
      }
      close(code = 1000, reason = '') { if (this.readyState === 3) return; this.readyState = 3; queueMicrotask(() => emit(this, 'close', new CloseEvent('close', { code, reason, wasClean: code === 1000 }))) }
    }
    window.WebSocket = FixtureSocket
    window.__runtimeTransportFixture = {
      audit,
      disconnectTerminal() { sockets.filter(ws => ws.terminal && ws.readyState === 1).forEach(ws => ws.close(1006, 'fixture network interruption')) },
      providerError() { sockets.filter(ws => !ws.terminal && !ws.isLsp && ws.readyState === 1).forEach(ws => frame(ws, { type: 'error', seq: 1, payload: { message: 'provider fixture: 模型连接失败' } })) },
      answer(text) { sockets.filter(ws => !ws.terminal && !ws.isLsp && ws.readyState === 1).forEach(ws => { frame(ws, { type: 'user_message', seq: 2, payload: { text: '展示分析结果' } }); frame(ws, { type: 'assistant_message', seq: 3, payload: { text } }); frame(ws, { type: 'done', seq: 4, payload: { stop_reason: 'end_turn' } }) }) },
    }
  }, { session })
  await context.route('**/*', async route => {
    const req = route.request(), u = new URL(req.url()), pathname = u.pathname, method = req.method(), label = `${method} ${pathname}`
    if (u.origin !== origin) { forbidden.push(label); return route.abort('blockedbyclient') }
    if (!pathname.startsWith('/api/') && pathname !== '/healthz') return route.continue()
    apiCount++
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
    if (/\/scan$|\/prompt$|\/feature-briefs\/fill$|\/reindex$/.test(pathname)) { forbidden.push(label); return reply({ detail: 'blocked in fixture' }, 403) }
    if (pathname === '/healthz') return reply({ ok: true })
    if (pathname === '/api/v1/auth/me') return reply({ id: 'runtime-fixture-user', username: 'fixture', role: 'admin' })
    if (pathname === '/api/v1/web') return reply({ available: false, reason: 'fixture' })
    if (pathname === '/api/v1/code/meta') return reply({ templates: ['empty'], browse_enabled: false, features: {}, git_available: false })
    if (pathname === '/api/v1/code/projects') return reply([project])
    if (pathname === '/api/v1/settings/model') {
      if (method === 'PUT') { const body = req.postDataJSON(); proxyRequests.push(body); Object.assign(modelSettings, body) }
      return reply(modelSettings)
    }
    if (pathname === '/api/v1/model-profiles' && method === 'GET') return reply([modelProfile])
    if (pathname === '/api/v1/model-profiles/proxy-fixture' && method === 'PUT') {
      const body = req.postDataJSON(); proxyRequests.push(body); Object.assign(modelProfile, body); Object.assign(modelSettings, body); return reply(modelProfile)
    }
    if (pathname === '/api/v1/model-profiles/probe' && method === 'POST') { modelProbes.push(req.postDataJSON()); return reply({ models: ['deepseek-flash', 'deepseek-v4-pro'], total: 2 }) }
    if (pathname.startsWith(`/api/v1/sessions/${session.id}`) && method === 'GET') {
      const suffix = pathname.slice(`/api/v1/sessions/${session.id}`.length)
      if (!suffix) return reply(session)
      if (suffix === '/events') return reply([])
      if (suffix === '/checkpoints') return reply({ items: [], busy: false })
      if (suffix === '/approvals/pending') return reply({})
    }
    const prefix = `/api/v1/code/projects/${project.id}`
    if (pathname.startsWith(prefix)) {
      const suffix = pathname.slice(prefix.length), body = method === 'POST' || method === 'PUT' ? req.postDataJSON() || {} : {}
      if (!suffix && method === 'GET') return reply(project)
      if (suffix === '/session' && method === 'POST') { if (body.permission_mode === 'workspace-write') forbidden.push(`${label} workspace-write`); return reply(session) }
      if (suffix === '/file') {
        if (method === 'GET') return reply(fileData(u.searchParams.get('path')) || { detail: 'missing' }, files.has(u.searchParams.get('path')) ? 200 : 404)
        if (method === 'PUT') { const file = files.get(body.path); writes.push({ path: body.path, content: body.content }); if (body.expected_sha1 !== file.sha1) return reply({ detail: 'fixture conflict' }, 409); file.text = body.content; file.sha1 = hash(file.text); return reply({ changed: true, sha1: file.sha1, lines: file.text.split('\n').length, version: writes.length + 1 }) }
      }
      if (suffix === '/terminals') {
        if (method === 'GET') { pendingDiscovery++; await discoveryGate; return reply({ available: true, backend: 'fixture-conpty', shells: [{ id: 'powershell', label: 'PowerShell', available: true }], terminals: [...terminals.values()] }) }
        if (method === 'POST') { const meta = { id: `term-${terminals.size + 1}`, project_id: project.id, status: 'running', exit_code: null, ...body, first_seq: 1, next_seq: 0 }; terminals.set(meta.id, meta); return reply(meta) }
      }
      const terminal = suffix.match(/^\/terminals\/([^/]+)$/)
      if (terminal && method === 'DELETE') { const meta = terminals.get(terminal[1]); meta.status = 'closed'; meta.exit_code = 0; return reply(meta) }
      if (suffix === '/run-configs') return reply({ configs: runtimeConfigs, limits: {} })
      if (suffix === '/runs') {
        if (method === 'GET') { pendingDiscovery++; await discoveryGate; return reply({ runs: [...runs.values()].map(({ events, ...meta }) => meta) }) }
        if (method === 'POST') { runRequests.push(body); const id = `run-${runs.size + 1}`, testing = body.kind === 'pytest' || body.kind === 'maven' && body.script === 'test', java = body.kind === 'maven', meta = { id, ...body, command: java ? ['mvn', body.script] : testing ? ['python', '-m', 'pytest', body.path || ''] : [body.kind, body.path || body.script || body.command || ''], status: body.kind === 'command' ? 'running' : 'exited', exit_code: body.kind === 'command' ? null : testing || java ? 0 : 1,
          events: [{ type: 'output', seq: 1, data: java ? testing ? '[INFO] Tests run: 2, Failures: 0\n[INFO] BUILD SUCCESS\n' : 'Started Java project fixture\n' : testing ? 'tests/test_main.py ..\n2 passed in 0.02s\n' : 'File "helper.py", line 12\nfixture runtime output\n' }, { type: 'exit', seq: 2, code: testing || java ? 0 : 1 }], first_seq: 1, next_seq: 2 }; if (body.kind === 'command') { meta.events.pop(); meta.next_seq = 1 }; runs.set(id, meta); const { events, ...summary } = meta; return reply(summary) }
      }
      const run = suffix.match(/^\/runs\/([^/]+)(\/stop)?$/)
      if (run && runs.has(run[1])) { const meta = runs.get(run[1]); if (method === 'POST') { meta.status = 'stopped'; meta.exit_code = 1; meta.events.push({ type: 'exit', seq: ++meta.next_seq, code: 1 }); return reply(meta) }; return reply({ ...meta, events: meta.events.filter(event => event.seq > Number(u.searchParams.get('after_seq') || 0)) }) }
      if (suffix === '/debug/meta') return reply({ available: true, backend: 'debugpy', install_hint: '' })
      if (suffix === '/debug/sessions') {
        if (method === 'GET') return reply({ sessions: debug ? [debug] : [] })
        if (method === 'POST') { debugRequests.push(body); debug = debugState(); debug.breakpoints = body.breakpoints.map(bp => ({ ...bp, requested_line: bp.line, verified: true })); return reply(debug) }
      }
      if (suffix === '/debug/sessions/debug-fixture' && method === 'GET') {
        const frameId = Number(u.searchParams.get('frame_id'))
        if (frameId) debug.frame_id = frameId
        return reply(debug)
      }
      if (suffix === '/debug/sessions/debug-fixture/variables') return reply({ variables: [{ name: '0', value: '1', type: 'int', variablesReference: 0 }, { name: '1', value: '2', type: 'int', variablesReference: 0 }] })
      if (suffix === '/debug/sessions/debug-fixture/breakpoints' && method === 'POST') { debug.breakpoints = body.breakpoints.map(bp => ({ ...bp, requested_line: bp.line, verified: true })); return reply(debug) }
      if (suffix === '/debug/sessions/debug-fixture/action' && method === 'POST') {
        debugRequests.push(body)
        if (body.action === 'stop') debug = { ...debug, status: 'stopped', stack: [], variables: [], exit_code: 0, seq: debug.seq + 1 }
        else if (body.action === 'continue') debug = { ...debug, status: 'running', stack: [], variables: [], seq: debug.seq + 1 }
        else debug = debugState(body.action === 'pause' ? 5 : (debug.stack[0]?.line || 3) + 1, body.action === 'pause' ? 'pause' : 'step')
        return reply(debug)
      }
      if (suffix === '/change-proposals' && method === 'GET') return reply({ proposals: [clone(proposal)] })
      if (suffix === '/change-proposals/proposal-fixture' && method === 'GET') return reply(clone(proposal))
      const decision = suffix.match(/^\/change-proposals\/proposal-fixture\/(apply|reject|rollback)$/)
      if (decision && method === 'POST') {
        const action = decision[1], ids = body.hunk_ids; decisions.push({ action, ids: ids || null })
        let changed = false
        for (const h of proposal.hunks) if ((!ids || ids.includes(h.id)) && (action === 'rollback' ? h.status === 'applied' : h.status === 'pending')) {
          if (action === 'reject') h.status = 'rejected'
          else { const file = files.get('main.py'); const oldText = h.lines[0].text.trim(), newText = h.lines[1].text.trim(); file.text = action === 'apply' ? file.text.replace(oldText, newText) : file.text.replace(newText, oldText); file.sha1 = hash(file.text); h.status = action === 'apply' ? 'applied' : 'reverted'; changed = true }
        }
        updateProposal(); if (action === 'rollback') proposal.state = 'reverted'
        return reply({ changed, path: 'main.py', proposal: clone(proposal) })
      }
      if (method === 'GET') {
        if (suffix === '/tree') return reply({ dirs: [], files: inventory(), truncated: false })
        if (suffix === '/files') return reply({ files: inventory(), truncated: false, inventory_truncated: false })
        if (suffix === '/map') return reply({ project, change_seq: 1, structure: { id: 'dir:', kind: 'dir', path: '', name: 'fixture', children: inventory().map(file => ({ ...file, id: `file:${file.path}`, kind: 'file', loc: 20, children: [] })) }, features: [], edges: { imports: [], calls: [], routes: [] }, stats: { files: 3, symbols: 0 } })
        if (suffix === '/index') return reply({ change_seq: 1, scanning: false, briefs: { running: false } })
        if (suffix === '/map/delta') return reply({ to_seq: 1, changes: [], added: [], updated: [], removed: [] })
        if (suffix === '/feature-briefs') return reply({ missing: { total: 0 }, items: [] })
        if (suffix === '/lsp') return reply({ servers: [{ id: 'jdtls', name: 'Runtime fixture: Java analysis', languages: ['java'], state: 'installed', enabled: true, detected: true }] })
        if (suffix === '/diagnostics') return reply({ seq: 1, counts: { error: 0, warning: 0 }, files: {}, symbols: {}, items: [] })
        if (suffix === '/patches') return reply({ patches: [], counts: { total: 0, active: 0 } })
        if (suffix === '/tasks' || suffix === '/batches') return reply([])
        if (suffix === '/architecture') return reply({ rules: {}, errors: [] })
        if (suffix === '/git') return reply({ is_repo: false, changes: [] })
      }
    }
    unexpected.push(label); return reply({ detail: 'Uncovered fixture API' }, 501)
  })
  page = await context.newPage(); page.setDefaultTimeout(15000)
  page.on('pageerror', e => { errorCount++; if (errors.length < 8) errors.push({ after: checks.at(-1), stack: redact(e.stack || e.message) }) })
  page.on('console', message => { if (message.type() === 'error') { errorCount++; if (errors.length < 8) errors.push({ after: checks.at(-1), stack: redact(message.text()) }) } })
  page.on('dialog', dialog => dialog.type() === 'beforeunload' ? dialog.accept() : dialog.dismiss())
  const editor = () => page.locator('.pane .monaco-editor').filter({ visible: true })
  const editorInput = () => editor().locator('[role="textbox"]')
  const waitFile = async filename => { await page.waitForFunction(name => document.querySelector('.pane .bar .path')?.textContent.trim() === name, filename); await editorInput().waitFor({ state: 'attached' }); await page.waitForFunction(() => !!document.querySelector('.pane .monaco-editor .view-line')) }
  const open = async filename => { await page.keyboard.press('Control+p'); await page.getByRole('textbox', { name: '快速打开搜索', exact: true }).fill(filename); await page.getByRole('option').filter({ has: page.locator(`small:text-is("${filename}")`) }).click(); await waitFile(filename) }
  const position = async (line, column) => { await editorInput().focus(); await page.keyboard.press('Control+Home'); for (let i = 1; i < line; i++) await page.keyboard.press('ArrowDown'); await page.keyboard.press('Home'); for (let i = 1; i < column; i++) await page.keyboard.press('ArrowRight'); await waitPosition(line, column) }
  const waitPosition = (line, column) => page.waitForFunction(({ line, column }) => document.querySelector('.status')?.textContent.includes(`Ln ${line}, Col ${column}`), { line, column })
  const screenshot = async name => { const file = `${name}.png`; await page.screenshot({ path: path.join(output, file), fullPage: true }); screenshots.push(file) }
  const bottom = async label => page.locator('.bottom-tabs').getByRole('button', { name: label, exact: true }).click()
  const checkBounds = async label => { const box = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth })); assert.ok(box.scroll <= box.width + 2, `${label} overflow: ${JSON.stringify(box)}`); checks.push(label) }
  const readEditor = async () => { await editorInput().focus(); await page.keyboard.press('Control+a'); await page.keyboard.press('Control+c'); return (await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n') }
  const dirtyTab = name => page.locator('.editor-tabs .editor-tab').filter({ has: page.locator(`.tab-label:text-is("${name}")`) }).locator('.dirty-dot')

  await page.goto(base, { waitUntil: 'networkidle' }); await page.locator('.code .workbench-bar').waitFor()
  assert.equal(runRequests.length, 0); assert.equal(debugRequests.length, 0); assert.equal(terminals.size, 0)
  checks.push('default workbench never auto-starts terminal, run, or debugger')
  await open(javaPath)
  await page.locator('.language-environment').filter({ hasText: 'Lombok：已启用' }).waitFor()
  await page.locator('.pane .lsp.as-button').click()
  await page.locator('.lsp-foot').filter({ hasText: 'Lombok 已启用' }).waitFor()
  assert.match(await page.locator('.lsp-panel').innerText(), /1\.18\.46/)
  assert.match(await page.locator('.lsp-panel').innerText(), /Java 分析缓存已按工作区隔离/)
  await page.locator('.lsp-head').getByRole('button', { name: '关闭代码分析设置', exact: true }).click()
  checks.push('Java analysis settings show Lombok version/state and isolated workspace cache')
  await page.locator('.workbench-bar').getByRole('button', { name: '运行', exact: true }).click()
  await page.locator('.runtime-guidance').filter({ hasText: 'spring-boot:run' }).waitFor()
  assert.equal(pendingDiscovery, 2); assert.equal(runRequests.length, 0)
  assert.equal(await page.getByRole('combobox', { name: '运行配置', exact: true }).inputValue(), 'maven-boot')
  assert.equal(await page.getByRole('textbox', { name: 'Maven 目标', exact: true }).inputValue(), 'spring-boot:run')
  assert.equal(await page.getByRole('textbox', { name: '运行工作目录', exact: true }).inputValue(), 'backend')
  assert.equal(await page.locator('.runtime-error').count(), 0)
  checks.push('Java business class prepares nearest Spring Boot module without running; configuration loads while terminal/history requests remain pending'); await screenshot('00-java-project-config')
  releaseDiscovery()
  await open('main.py'); await position(8, 6); await open('helper.py'); await position(12, 4)
  await page.getByRole('button', { name: '导航后退', exact: true }).click(); await waitFile('main.py'); await waitPosition(8, 6)
  await page.getByRole('button', { name: '导航前进', exact: true }).click(); await waitFile('helper.py'); await waitPosition(12, 4)
  checks.push('navigation back/forward restores file, line and column'); await screenshot('01-navigation')

  await page.locator('.workbench-bar').getByRole('button', { name: '终端', exact: true }).click()
  await page.locator('.runtime-panel').getByRole('button', { name: '+ 终端', exact: true }).click()
  await page.locator('.terminal-host:visible .xterm').waitFor(); await page.waitForFunction(() => document.querySelector('.terminal-host .xterm-rows')?.textContent.includes('fixture terminal ready'))
  await page.locator('.terminal-host:visible .xterm-helper-textarea').focus(); await page.keyboard.type('echo fixture'); await page.keyboard.press('Enter')
  await page.waitForFunction(() => document.querySelector('.terminal-host .xterm-rows')?.textContent.includes('terminal fixture output'))
  const before = await page.locator('.terminal-host:visible .xterm-rows').innerText()
  await page.evaluate(() => window.__runtimeTransportFixture.disconnectTerminal())
  await page.waitForFunction(() => window.__runtimeTransportFixture.audit.filter(item => item.kind === 'connect' && item.pathname.includes('/terminals/')).length >= 2)
  await page.waitForFunction(() => document.querySelector('.runtime-state')?.textContent === '已连接')
  assert.equal(await page.locator('.terminal-host:visible .xterm-rows').innerText(), before)
  const socketAudit = await page.evaluate(() => window.__runtimeTransportFixture.audit)
  assert.ok(socketAudit.some(item => item.kind === 'send' && item.frame.type === 'resize')); assert.ok(socketAudit.some(item => item.kind === 'connect' && item.afterSeq === 2))
  checks.push('real xterm receives PTY output/input/resize, reconnects from seq without duplicate output'); await screenshot('02-terminal')
  await page.locator('.runtime-panel').getByRole('button', { name: '关闭', exact: true }).click(); await page.waitForFunction(() => !document.querySelector('.terminal-host'))
  assert.equal([...terminals.values()][0].status, 'closed'); checks.push('manual terminal close stops fixture process and releases terminal host')

  await open('main.py'); await page.locator('.workbench-bar').getByRole('button', { name: '运行', exact: true }).click()
  await page.locator('.run-output').filter({ hasText: 'fixture runtime output' }).waitFor(); assert.equal(runRequests.length, 1); assert.equal(runRequests[0].kind, 'python'); assert.equal(runRequests[0].path, 'main.py')
  assert.match(await page.locator('.runtime-source').innerText(), /python main.py/); assert.match(await page.locator('.run-selection').innerText(), /退出码 1/)
  await page.locator('.output-locations').getByRole('button', { name: 'helper.py:12', exact: true }).click(); await waitFile('helper.py'); await waitPosition(12, 1)
  checks.push('current Python runs only on click, displays source/exit code and navigates relative output location'); await screenshot('03-run-output')
  await bottom('测试'); await page.getByRole('combobox', { name: '运行配置', exact: true }).selectOption('pytest')
  assert.equal(runRequests.length, 1); assert.equal(await page.getByRole('textbox', { name: '运行文件路径', exact: true }).inputValue(), 'tests')
  await page.locator('.run-form').getByRole('button', { name: '▶ 运行', exact: true }).click(); await page.locator('.run-output').filter({ hasText: '2 passed' }).waitFor(); assert.equal(runRequests.at(-1).kind, 'pytest')
  checks.push('test configuration prepares only; explicit Run displays pytest result'); await screenshot('04-tests')
  await bottom('运行'); await page.getByRole('combobox', { name: '运行类型', exact: true }).selectOption('command'); await page.getByRole('textbox', { name: '人工运行命令', exact: true }).fill('echo fixture command')
  await page.locator('.run-form').getByRole('button', { name: '▶ 运行', exact: true }).click(); await page.locator('.run-selection').getByRole('button', { name: '■ 停止', exact: true }).click()
  await page.locator('.run-selection').filter({ hasText: '已停止' }).waitFor(); assert.equal([...runs.values()].at(-1).status, 'stopped'); checks.push('manual command starts only on submit and Stop ends the selected run')

  await open('main.py'); await position(5, 1); await page.keyboard.press('F9')
  await bottom('调试'); await page.locator('.debug-panel').getByRole('button', { name: '▶ 启动', exact: true }).click()
  await page.locator('.debug-status').filter({ hasText: '已暂停' }).waitFor(); await editor().locator('.ide-debug-line').waitFor()
  assert.equal(debugRequests[0].path, 'main.py'); assert.ok(debugRequests[0].breakpoints.some(bp => bp.path === 'main.py' && bp.line === 5))
  await page.getByRole('button', { name: '展开变量 items', exact: true }).click(); await page.locator('.debug-variable-name').filter({ hasText: /^0$/ }).waitFor()
  await page.getByRole('button', { name: '↷ 跳过', exact: true }).click(); await page.locator('.debug-frame.selected').filter({ hasText: 'main.py:4' }).waitFor()
  await page.locator('.debug-frame.selected').click(); await waitPosition(4, 1)
  await page.locator('.debug-stack').getByRole('button').filter({ hasText: 'fixture_helper' }).click(); await waitFile('helper.py'); await waitPosition(6, 3)
  checks.push('Python debugger entry pauses, F9 breakpoint is passed, variable children/step/frame navigation work'); await screenshot('05-debug')
  await editor().locator('.ide-debug-line').waitFor()
  const continueBefore = debugRequests.filter(request => request.action === 'continue').length
  await bottom('输出'); await page.keyboard.press('F5')
  await page.locator('.bottom-tabs button.active').filter({ hasText: /^调试$/ }).waitFor()
  await page.locator('.debug-status').filter({ hasText: '运行中' }).waitFor()
  await editor().locator('.ide-debug-line').waitFor({ state: 'detached' })
  assert.equal(debugRequests.filter(request => request.action === 'continue').length, continueBefore + 1)
  assert.equal(debugRequests.filter(request => Object.hasOwn(request, 'path')).length, 1)
  checks.push('F5 from Output while paused returns to Debug, continues exactly once and clears paused highlight'); await screenshot('05b-debug-f5')
  await page.getByRole('button', { name: '■ 停止', exact: true }).click(); await page.locator('.debug-status').filter({ hasText: '已结束' }).waitFor()
  checks.push('manual debugger stop ends session')

  await open('main.py'); await position(1, 1); await page.keyboard.press('Shift+End')
  await page.locator('.ai .code-actions').filter({ hasText: '已选中代码' }).waitFor()
  await page.locator('.ai').getByRole('button', { name: '解释代码', exact: true }).click()
  assert.match(await page.locator('.ai .composer textarea').inputValue(), /value = 1/)
  await page.locator('.ai').getByRole('button', { name: '提出修改', exact: true }).click()
  assert.match(await page.locator('.ai .composer textarea').inputValue(), /propose_edit/)
  await page.locator('.ai').getByRole('button', { name: '生成测试', exact: true }).click()
  assert.match(await page.locator('.ai .composer textarea').inputValue(), /有意义的测试/)
  assert.match(await page.locator('.ai .composer textarea').inputValue(), /propose_edit/)
  assert.equal(forbidden.length, 0); checks.push('AI explain/edit/test actions prepare selected-code prompts without model calls'); await screenshot('06-ai-actions')
  await page.evaluate(() => window.__runtimeTransportFixture.providerError())
  await page.locator('.ai .connection-status .error').filter({ hasText: '模型请求失败' }).waitFor()
  assert.match(await page.locator('.ai .connection-status').innerText(), /会话已连接/)
  checks.push('connected session websocket and failed model request show independent status'); await screenshot('06b-ai-model-status')
  await page.evaluate(() => window.__runtimeTransportFixture.answer('### 改进建议\n\n**先保存文件**，再后台更新索引。\n\n1. 保留当前草稿\n2. 更新项目地图\n\n| 步骤 | 状态 |\n| --- | --- |\n| 文件写入 | 完成 |\n\n```python\nprint("<script>literal</script>")\n```\n\n<script>window.__markdownAttack = true</script>\n\n![外部图片](https://fixture.invalid/do-not-load.png)\n\n[危险链接](javascript:alert(1))'))
  await page.locator('.ai .markdown-body h3').filter({ hasText: '改进建议' }).waitFor()
  assert.equal(await page.locator('.ai .markdown-body table').count(), 1)
  assert.equal(await page.locator('.ai .markdown-body strong').innerText(), '先保存文件')
  assert.equal(await page.locator('.ai .markdown-body pre code').innerText(), 'print("<script>literal</script>")\n')
  assert.equal(await page.locator('.ai .markdown-body script, .ai .markdown-body img, .ai .markdown-body [href^="javascript:"]').count(), 0)
  assert.equal(await page.evaluate(() => window.__markdownAttack), undefined)
  await page.locator('.ai .historical-error:not([open])').waitFor()
  checks.push('AI answers render Markdown headings, lists, code and tables safely without script execution or remote image loads; a later answer archives the old error'); await screenshot('06c-ai-markdown')
  await page.locator('.ai').getByRole('button', { name: '查看修改', exact: true }).click(); await page.locator('.changes .hunk').first().waitFor()
  assert.equal(await page.locator('.changes .hunk').count(), 2)
  await editorInput().focus(); await page.keyboard.press('Control+End'); await page.keyboard.insertText('\n# unsaved guard'); await dirtyTab('main.py').waitFor()
  await page.locator('.changes .draft-note').filter({ hasText: '未保存草稿' }).waitFor(); assert.equal(await page.locator('.changes .review-actions').getByRole('button', { name: '应用全部', exact: true }).isDisabled(), true); assert.equal(decisions.length, 0)
  checks.push('dirty draft blocks AI proposal apply before any API write')
  await editorInput().focus(); await page.keyboard.press('Control+a'); await page.keyboard.insertText(original); await dirtyTab('main.py').waitFor({ state: 'detached' })
  await page.locator('.changes .hunk').nth(0).getByRole('button', { name: '应用', exact: true }).click(); await page.waitForFunction(() => document.querySelector('.changes .hunk header')?.textContent.includes('已应用'))
  assert.deepEqual(decisions[0], { action: 'apply', ids: ['h1'] }); assert.ok(files.get('main.py').text.startsWith('value = 10'))
  assert.ok((await readEditor()).startsWith('value = 10')); checks.push('one selected hunk applies and clean open editor refreshes')
  await page.locator('.changes .hunk').nth(1).getByRole('button', { name: '拒绝', exact: true }).click(); await page.waitForFunction(() => document.querySelectorAll('.changes .hunk header')[1]?.textContent.includes('rejected') || document.querySelectorAll('.changes .hunk header')[1]?.textContent.includes('已拒绝'))
  assert.deepEqual(decisions[1], { action: 'reject', ids: ['h2'] }); assert.ok(files.get('main.py').text.includes('other = 2'))
  await screenshot('07-change-review')
  await page.locator('.changes .review-actions').getByRole('button', { name: '回滚已应用', exact: true }).click(); await page.waitForFunction(() => document.querySelector('.changes .proposal-list')?.textContent.includes('已回滚'))
  assert.equal(files.get('main.py').text, original); assert.equal(await readEditor(), original); checks.push('remaining hunk rejects without write; rollback restores only applied fixture change')

  await open(javaPath); const javaRunsBefore = runRequests.length
  await page.locator('.workbench-bar').getByRole('button', { name: '运行', exact: true }).click()
  await page.locator('.runtime-guidance').filter({ hasText: 'spring-boot:run' }).waitFor()
  assert.equal(runRequests.length, javaRunsBefore)
  await page.locator('.run-form').getByRole('button', { name: '▶ 运行', exact: true }).click()
  await page.locator('.run-output').filter({ hasText: 'Started Java project fixture' }).waitFor()
  assert.deepEqual(runRequests.at(-1), { kind: 'maven', script: 'spring-boot:run', cwd: 'backend', name: runtimeConfigs[1].name })
  assert.match(await page.locator('.runtime-source').innerText(), /mvn spring-boot:run/)
  assert.match(await page.locator('.runtime-source').innerText(), /backend/)
  checks.push('explicit Java project Run sends Maven goal and module cwd, displays actual command/output'); await screenshot('07b-java-run')
  await bottom('测试')
  assert.equal(await page.getByRole('combobox', { name: '运行配置', exact: true }).inputValue(), 'maven-test')
  await page.locator('.run-form').getByRole('button', { name: '▶ 运行', exact: true }).click()
  await page.locator('.run-output').filter({ hasText: 'BUILD SUCCESS' }).waitFor()
  assert.equal(runRequests.at(-1).script, 'test'); assert.equal(runRequests.at(-1).cwd, 'backend')
  checks.push('mixed Java/Python project selects module Maven test for active Java document')
  await bottom('运行'); await page.getByRole('combobox', { name: '运行配置', exact: true }).selectOption('gradle-missing')
  await page.locator('.runtime-notice').filter({ hasText: '未找到 Gradle' }).waitFor()
  assert.equal(await page.locator('.run-form').getByRole('button', { name: '▶ 运行', exact: true }).isDisabled(), true)
  assert.equal(await page.locator('.runtime-error').count(), 0)
  checks.push('missing Gradle environment is explained inline and unavailable configuration cannot execute')

  await page.setViewportSize({ width: 375, height: 860 }); await page.locator('.workspace-switcher').getByRole('button', { name: '代码工作区', exact: true }).click()
  await bottom('终端'); await page.locator('.runtime-panel').getByRole('button', { name: '+ 终端', exact: true }).click(); await page.locator('.terminal-host:visible .xterm').waitFor(); await checkBounds('375px terminal controls and xterm stay within viewport'); await screenshot('08-mobile-terminal')
  await bottom('测试'); await page.locator('.runtime-panel .run-form').getByRole('button', { name: '▶ 运行', exact: true }).waitFor(); await checkBounds('375px tests/run form stays within viewport'); await screenshot('09-mobile-tests')
  await bottom('调试'); await checkBounds('375px debugger panel stays within viewport'); await screenshot('10-mobile-debug')
  await page.locator('.workspace-switcher').getByRole('button', { name: 'AI 助手', exact: true }).click(); await page.locator('.right-tabs').getByRole('button', { name: '修改', exact: true }).click(); await page.locator('.changes').waitFor(); await checkBounds('375px change review remains usable without page overflow'); await screenshot('11-mobile-review')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('button', { name: '设置', exact: true }).click()
  await page.getByRole('combobox', { name: '网络代理', exact: true }).waitFor()
  await page.getByRole('combobox', { name: '网络代理', exact: true }).selectOption('direct')
  await page.getByRole('button', { name: '保存并生效', exact: true }).click()
  await page.locator('.summary .row').filter({ hasText: '网络代理' }).filter({ hasText: '直接连接' }).waitFor()
  assert.equal(proxyRequests.at(-1).proxy_mode, 'direct'); assert.ok(!proxyRequests.at(-1).api_key)
  assert.equal(proxyRequests.at(-1).model_default, 'deepseek-flash')
  await page.getByRole('button', { name: '探测模型', exact: true }).click()
  await page.locator('.probe-chip').filter({ hasText: /^deepseek-flash$/ }).waitFor()
  assert.equal(modelProbes.at(-1).proxy_mode, 'direct')
  assert.equal(modelProbes.at(-1).use_current, true)
  checks.push('provider-scoped direct mode saves without exposing/replacing credentials; model probe uses the selected mode'); await screenshot('12-proxy-settings')
  await page.locator('.profile-card').getByRole('button', { name: '编辑', exact: true }).click()
  assert.equal(await page.getByRole('combobox', { name: '网络代理', exact: true }).inputValue(), 'system')
  await page.getByRole('combobox', { name: '网络代理', exact: true }).selectOption('direct')
  await page.getByRole('button', { name: '保存档案', exact: true }).click()
  assert.equal(proxyRequests.at(-1).proxy_mode, 'direct'); assert.ok(!proxyRequests.at(-1).api_key)
  checks.push('editing an active model profile persists its own network mode')
  await page.setViewportSize({ width: 375, height: 860 }); await checkBounds('375px proxy settings stay within viewport'); await screenshot('13-mobile-proxy')
  const audit = await page.evaluate(() => window.__runtimeTransportFixture.audit)
  assert.deepEqual(audit.filter(item => item.kind.startsWith('blocked')), [])
  assert.deepEqual(unexpected, []); assert.deepEqual(forbidden, []); assert.equal(errorCount, 0, `runtime errors: ${errors[0]?.stack || ''}`)
  checks.push('all API/WS requests stay in fixtures; zero model/scan/workspace-write calls and runtime errors')
} catch (e) { failure = redact(e.stack || e.message); if (page) { try { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }); screenshots.push('failure.png') } catch {} } }
finally {
  await browser.close()
  const report = { passed: !failure, checks, screenshots, apiCount, runRequests, debugRequests, decisions, proxyRequests, modelProbes, writes: writes.map(({ path }) => path), unexpected, forbidden, errorCount, errors, failure }
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2)); if (failure) process.exitCode = 1
}
