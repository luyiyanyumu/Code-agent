import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'

// Real Chrome/Vue/Monaco; every API HTTP/WS request is intercepted before navigation.
// No actual project files, indexing, terminal processes, prompts or model requests.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const playwright = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/editor-save-ui', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], screenshots = [], errors = [], unexpected = [], forbidden = [], writes = [], refreshRequests = [], expectedConsole = []
let page, failure = null, apiCount = 0, pendingIndexRequests = 0, indexSeq = 1, stage = 'launch'
const phase = label => { stage = label; console.log(`[fixture] ${label}`) }
const hash = text => createHash('sha1').update(text).digest('hex')
const redact = text => String(text).replace(/([?&]token=)[^&\s"']+/gi, '$1[redacted]').replace(/Bearer\s+[^\s"']+/gi, 'Bearer [redacted]')
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const waitFixture = async (predicate, label, timeout = 5000) => {
  const deadline = Date.now() + timeout
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20))
  assert.ok(predicate(), label)
}
const project = { id: 'fixture-editor-save', name: '保存与历史记录验收', root_path: 'E:/editor-save-fixture', available: true, allow_direct_write: false, git: { is_repo: false } }
const session = { id: 'session-editor-save-fixture', code_project_id: project.id, permission_mode: 'read-only', model: 'fixture-model', busy: false, status: 'open', web_enabled: false }
const firstEdit = '# first fixture edit\n'
const initialText = ('# fixture 174KB editor save\n' + '# safe fixture comment padding abcdefghijklmnopqrstuvwxyz\n'.repeat(4000)).slice(0, 174 * 1024 - firstEdit.length - 1) + '\n'
const filename = 'main.py', file = { text: initialText, lang: 'python' }
file.sha1 = hash(file.text)
const fileData = () => ({ ...file, path: filename, name: filename, uri: `file:///${project.root_path}/${filename}`, editable: true, secret: false, start_line: 1, total_lines: file.text.split('\n').length, end_line: file.text.split('\n').length, size: Buffer.byteLength(file.text) })
const inventory = () => [{ path: filename, name: filename, lang: file.lang, size: Buffer.byteLength(file.text) }]
const oldError = 'provider fixture: All connection attempts failed'
const history = [
  { type: 'user_message', seq: 1, ts: '2026-09-29T02:11:00Z', payload: { text: 'historical fixture request' } },
  { type: 'error', seq: 2, ts: '2026-09-29T02:12:00Z', payload: { message: oldError } },
  { type: 'done', seq: 3, ts: '2026-09-29T02:12:00Z', payload: { stop_reason: 'error' } },
  { type: 'user_message', seq: 60, ts: '2026-10-07T02:12:00Z', payload: { text: 'later fixture request' } },
  { type: 'done', seq: 64, ts: '2026-10-07T02:13:00Z', payload: { stop_reason: 'max_turns', text: 'later fixture answer' } },
]
let refresh = { state: 'idle', pending: false, requested_generation: 0, completed_generation: 0, error: '' }
let indexGate = deferred(), saveGate = null, nextRefreshFailure = false
const refreshSnapshot = () => ({ ...refresh })
const indexSnapshot = () => ({ version: indexSeq, change_seq: indexSeq, indexed: true, scanning: refresh.pending, files: 1, symbols: 0, edges: 0, briefs: { running: false }, index_refresh: refreshSnapshot() })
function requestRefresh() {
  refresh = { ...refresh, state: 'running', pending: true, requested_generation: refresh.requested_generation + 1, error: '' }
  return refreshSnapshot()
}
function completeRefresh() {
  refresh = { ...refresh, state: 'done', pending: false, completed_generation: refresh.requested_generation, error: '' }
  indexSeq += 1
  indexGate.resolve()
}
const browser = await playwright.chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const watchdog = setTimeout(() => {
  failure ||= `Fixture exceeded 90 seconds during ${stage}`
  console.error(failure)
  saveGate?.resolve(); indexGate.resolve()
  browser.close().catch(() => {})
}, 90000)
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block', permissions: ['clipboard-read', 'clipboard-write'] })
  await context.addInitScript(({ session }) => {
    localStorage.setItem('code-agent:view', 'code')
    localStorage.setItem('code-agent:nav-collapsed', 'true')
    localStorage.setItem('code-agent-code-left', '225')
    localStorage.setItem('code-agent-code-right', '365')
    const Native = window.WebSocket, sockets = [], audit = []
    const emit = (socket, type, event) => { socket.dispatchEvent(event); socket[`on${type}`]?.(event) }
    const frame = (socket, value) => { if (socket.readyState === 1) emit(socket, 'message', new MessageEvent('message', { data: JSON.stringify(value) })) }
    class FixtureSocket extends EventTarget {
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
      constructor(url, protocols) {
        super()
        const parsed = new URL(url, location.href)
        if (!parsed.pathname.startsWith('/api/') && parsed.host === location.host) return new Native(url, protocols)
        this.url = parsed.href; this.readyState = 0; this.bufferedAmount = 0; this.protocol = ''; this.extensions = ''; this.binaryType = 'blob'
        this.CONNECTING = 0; this.OPEN = 1; this.CLOSING = 2; this.CLOSED = 3
        this.isSession = parsed.host === location.host && parsed.pathname === `/api/v1/sessions/${session.id}/stream`
        audit.push({ kind: this.isSession ? 'connect' : 'blocked', path: parsed.pathname }); sockets.push(this)
        queueMicrotask(() => {
          if (this.readyState !== 0) return
          if (!this.isSession) return this.close(4404, 'unmocked websocket blocked')
          this.readyState = 1; emit(this, 'open', new Event('open'))
          frame(this, { type: 'session_meta', seq: 0, payload: session })
        })
      }
      send(text) {
        const data = JSON.parse(text)
        audit.push({ kind: data.type === 'ping' ? 'ping' : 'blocked-frame', path: new URL(this.url).pathname, type: data.type })
        if (data.type === 'ping') queueMicrotask(() => frame(this, { type: 'pong', seq: 0, payload: {} }))
      }
      close(code = 1000, reason = '') {
        if (this.readyState === 3) return
        this.readyState = 3
        queueMicrotask(() => emit(this, 'close', new CloseEvent('close', { code, reason, wasClean: code === 1000 })))
      }
    }
    window.WebSocket = FixtureSocket
    window.__editorSaveFixture = {
      audit,
      emitSession(events) { for (const event of events) sockets.filter(socket => socket.isSession && socket.readyState === 1).forEach(socket => frame(socket, event)) },
    }
  }, { session })
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname, method = request.method(), label = `${method} ${pathname}`
    if (url.origin !== origin) { forbidden.push(label); return route.abort('blockedbyclient') }
    if (!pathname.startsWith('/api/') && pathname !== '/healthz') return route.continue()
    apiCount += 1
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
    if (/\/prompt$|\/runs(?:\/|$)|\/terminals(?:\/|$)|\/debug\/|\/reindex$|\/feature-briefs\/fill$/.test(pathname)) {
      forbidden.push(label); return reply({ detail: 'fixture forbids execution and model requests' }, 403)
    }
    if (pathname === '/healthz') return reply({ ok: true })
    if (pathname === '/api/v1/auth/me') return reply({ id: 'editor-save-fixture-user', username: 'fixture', role: 'admin' })
    if (pathname === '/api/v1/web') return reply({ available: false, reason: 'fixture' })
    if (pathname === '/api/v1/settings/model') return reply({ provider: 'offline', model_default: 'fixture-model', active_model: 'fixture-model' })
    if (pathname === '/api/v1/model-profiles') return reply([])
    if (pathname === '/api/v1/code/meta') return reply({ templates: ['empty'], browse_enabled: false, features: {}, git_available: false })
    if (pathname === '/api/v1/code/projects') return reply([project])
    if (pathname.startsWith(`/api/v1/sessions/${session.id}`) && method === 'GET') {
      const suffix = pathname.slice(`/api/v1/sessions/${session.id}`.length)
      if (!suffix) return reply(session)
      if (suffix === '/events') return reply(history.filter(event => event.seq > Number(url.searchParams.get('after_seq') || 0)))
      if (suffix === '/checkpoints') return reply({ items: [], busy: false })
      if (suffix === '/approvals/pending') return reply({})
    }
    const prefix = `/api/v1/code/projects/${project.id}`
    if (pathname.startsWith(prefix)) {
      const suffix = pathname.slice(prefix.length)
      if (!suffix && method === 'GET') return reply(project)
      if (suffix === '/session' && method === 'POST') {
        const body = request.postDataJSON()
        if (body?.permission_mode === 'workspace-write') forbidden.push(`${label} workspace-write`)
        return reply(session)
      }
      if (suffix === '/file') {
        if (method === 'GET') return reply(fileData(), url.searchParams.get('path') === filename ? 200 : 404)
        if (method === 'PUT') {
          const body = request.postDataJSON()
          writes.push({ path: body.path, bytes: Buffer.byteLength(body.content), sha1: hash(body.content), expected_sha1: body.expected_sha1 })
          if (body.path !== filename) { unexpected.push(label); return reply({ detail: 'unknown fixture file' }, 422) }
          if (body.expected_sha1 !== file.sha1) return reply({ detail: { message: 'fixture conflict', current_sha1: file.sha1, expected_sha1: body.expected_sha1 } }, 409)
          const gate = saveGate
          if (gate) await gate.promise
          file.text = body.content; file.sha1 = hash(file.text)
          return reply({ ok: true, changed: true, path: filename, sha1: file.sha1, lines: file.text.split('\n').length, patch_id: `fixture-patch-${writes.length}`, index_refresh: requestRefresh() })
        }
      }
      if (suffix === '/scan' && method === 'POST') {
        assert.equal(url.searchParams.get('full'), '0', 'index retry must stay incremental')
        assert.equal(url.searchParams.get('tree'), 'main')
        refreshRequests.push(label)
        return reply({ status: 'queued', scanning: true, index_refresh: requestRefresh(), ...refreshSnapshot() })
      }
      if (suffix === '/index' && method === 'GET') {
        if (refresh.pending) {
          pendingIndexRequests += 1
          if (nextRefreshFailure) {
            nextRefreshFailure = false
            refresh = { ...refresh, state: 'error', pending: false, error: 'fixture index unavailable' }
          } else await indexGate.promise
        }
        return reply(indexSnapshot())
      }
      if (method === 'GET') {
        if (suffix === '/agents') return reply({ lead: { session_id: session.id, project_id: project.id, role: 'lead', depth: 0, title: 'Lead', state: 'idle' }, agents: [], limits: { max_depth: 3, max_parallel: 4 }, running: 0, queued: 0 })
        if (suffix === '/tree') return reply({ dirs: [], files: inventory(), truncated: false })
        if (suffix === '/files') return reply({ files: inventory(), truncated: false, inventory_truncated: false })
        if (suffix === '/map') return reply({ project, change_seq: indexSeq, structure: { id: 'dir:', kind: 'dir', path: '', name: 'fixture', children: inventory().map(item => ({ ...item, id: `file:${item.path}`, kind: 'file', loc: 20, children: [] })) }, features: [], edges: { imports: [], calls: [], routes: [] }, stats: { files: 1, symbols: 0 } })
        if (suffix === '/map/delta') return reply({ to_seq: indexSeq, changes: [], added: [], updated: [], removed: [] })
        if (suffix === '/feature-briefs') return reply({ missing: { total: 0 }, items: [] })
        if (suffix === '/lsp') return reply({ servers: [] })
        if (suffix === '/diagnostics') return reply({ seq: indexSeq, counts: { error: 0, warning: 0 }, files: {}, symbols: {}, items: [] })
        if (suffix === '/patches') return reply({ patches: [], counts: { total: 0, active: 0 } })
        if (suffix === '/change-proposals') return reply({ proposals: [] })
        if (suffix === '/tasks' || suffix === '/batches') return reply([])
        if (suffix === '/architecture') return reply({ rules: {}, errors: [] })
        if (suffix === '/git') return reply({ is_repo: false, changes: [] })
      }
    }
    unexpected.push(label)
    return reply({ detail: 'uncovered fixture API' }, 501)
  })
  page = await context.newPage()
  page.setDefaultTimeout(15000)
  page.on('pageerror', error => errors.push(redact(error.stack || error.message)))
  page.on('console', message => {
    if (message.type() !== 'error') return
    if (/Failed to load resource:.*409/.test(message.text())) expectedConsole.push(message.text())
    else errors.push(redact(message.text()))
  })
  page.on('dialog', dialog => dialog.type() === 'beforeunload' ? dialog.accept() : dialog.dismiss())
  const editor = () => page.locator('.pane .monaco-editor').filter({ visible: true })
  const editorInput = () => editor().locator('[role="textbox"]')
  const saveButton = () => page.locator('.pane .bar').getByRole('button', { name: /^保存(?:中…)?$/ })
  const dirtyTab = () => page.locator('.editor-tabs .editor-tab').filter({ has: page.locator(`.tab-label:text-is("${filename}")`) }).locator('.dirty-dot')
  const readEditor = async () => {
    await editorInput().focus(); await page.keyboard.press('Control+a'); await page.keyboard.press('Control+c')
    return (await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n')
  }
  const append = async text => { await editorInput().focus(); await page.keyboard.press('Control+End'); await page.keyboard.insertText(text) }
  const screenshot = async name => { const filename = `${name}.png`; await page.screenshot({ path: path.join(output, filename), fullPage: true }); screenshots.push(filename) }
  const pendingStatus = () => page.locator('.index-refresh').filter({ hasText: /索引.*更新中|更新.*索引/ })
  const waitIndexFinished = () => page.waitForFunction(() => {
    const status = document.querySelector('.index-refresh')
    return !status || !/更新中|更新失败/.test(status.textContent)
  })

  phase('open fixture workbench and existing 174KB file')
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.locator('.code .workbench-bar').waitFor()
  await page.keyboard.press('Control+p')
  await page.getByRole('textbox', { name: '快速打开搜索', exact: true }).fill(filename)
  await page.getByRole('option').filter({ has: page.locator(`small:text-is("${filename}")`) }).click()
  await editorInput().waitFor({ state: 'attached' })
  await page.waitForFunction(() => !!document.querySelector('.pane .monaco-editor .view-line'))
  phase('historical error presentation')
  await page.locator('.ai .connection-status .idle').filter({ hasText: '模型待命' }).waitFor()
  const historical = page.locator('.ai details.historical-error')
  assert.equal(await historical.count(), 1)
  assert.equal(await historical.evaluate(element => element.open), false)
  assert.equal(await historical.locator('.bubble.error').isVisible(), false)
  assert.match(await historical.locator('summary').innerText(), /历史错误.*2026/)
  assert.equal(await page.locator('.ai .banner').count(), 0)
  assert.match(await page.locator('.ai .msg.done .tag.warn').last().innerText(), /max_turns/)
  await historical.locator('summary').click()
  assert.equal(await historical.locator('.bubble.error').innerText(), oldError)
  await historical.locator('summary').click()
  checks.push('historical seq 2 provider error stays readable but collapsed; later seq 64 max_turns clears current failure and duplicate banner')
  await screenshot('01-historical-error')

  phase('save 174KB file while index HTTP remains pending')
  const content = initialText + firstEdit
  await append(firstEdit)
  await dirtyTab().waitFor()
  await page.keyboard.press('Control+s')
  await page.locator('.tabs .tab-note').filter({ hasText: `已保存 ${filename}` }).waitFor()
  await dirtyTab().waitFor({ state: 'detached' })
  assert.equal(await saveButton().innerText(), '保存')
  assert.equal(file.text, content)
  await pendingStatus().waitFor()
  assert.equal(await pendingStatus().getAttribute('role'), 'status')
  await page.waitForFunction(() => !document.querySelector('.pane .bar .primary')?.textContent.includes('保存中'))
  await waitFixture(() => pendingIndexRequests >= 1, 'save must poll index status independently after its receipt')
  checks.push('174KB file save acknowledges disk success and clears saving/dirty before delayed index HTTP completes')
  phase('typing during pending index refresh')
  await append('# typed while index request is pending\n')
  await dirtyTab().waitFor()
  const indexDraft = await readEditor()
  assert.ok(indexDraft.endsWith('# typed while index request is pending\n'))
  assert.equal(await saveButton().isEnabled(), true)
  assert.equal(file.text, content)
  checks.push('editor stays writable and newer typing remains an unsaved draft while index refresh is pending')
  await screenshot('02-saved-index-pending')
  completeRefresh()
  await waitIndexFinished()
  assert.equal(await readEditor(), indexDraft)
  await dirtyTab().waitFor()
  checks.push('finishing background indexing preserves newer draft text and dirty state')

  phase('typing during pending file PUT and index failure')
  saveGate = deferred(); indexGate = deferred(); nextRefreshFailure = true
  const submittedDraft = await readEditor(), beforeWrites = writes.length
  await editorInput().focus(); await page.keyboard.press('Control+s')
  await saveButton().filter({ hasText: '保存中' }).waitFor()
  assert.equal(writes.length, beforeWrites + 1)
  await append('# typed while file PUT is pending\n')
  const newerDraft = await readEditor()
  saveGate.resolve(); saveGate = null
  await page.locator('.tabs .tab-note').filter({ hasText: `已保存 ${filename}` }).waitFor()
  await saveButton().filter({ hasText: /^保存$/ }).waitFor()
  assert.equal(file.text, submittedDraft)
  assert.equal(await readEditor(), newerDraft)
  await dirtyTab().waitFor()
  checks.push('save receipt applies only submitted text/hash and keeps input entered during pending PUT')
  const failedIndex = page.locator('.index-refresh').filter({ hasText: /索引.*更新失败/ })
  await failedIndex.waitFor()
  assert.equal(await failedIndex.getAttribute('role'), 'status')
  assert.equal(await page.locator('.center > .banner').count(), 0)
  assert.match(await page.locator('.tabs .tab-note').innerText(), /已保存/)
  assert.doesNotMatch(await page.locator('.tabs .tab-note').innerText(), /已更新到|保存失败/)
  checks.push('failed index refresh is a separate status and does not turn successful disk save into a file error')
  await screenshot('03-index-error-newer-draft')
  phase('explicit index retry')
  await failedIndex.getByRole('button', { name: /重试/ }).click()
  await pendingStatus().waitFor()
  assert.equal(refreshRequests.length, 1)
  completeRefresh()
  await waitIndexFinished()
  assert.equal(await readEditor(), newerDraft)
  await dirtyTab().waitFor()
  checks.push('explicit fixture index retry clears the failure without writing or replacing newer draft text')

  phase('409 conflict retains the newer draft')
  file.text = '# external fixture edit\nvalue = 999\n'; file.sha1 = hash(file.text)
  await editorInput().focus(); await page.keyboard.press('Control+s')
  const conflict = page.getByRole('dialog', { name: '文件保存冲突', exact: true })
  await conflict.waitFor()
  assert.equal(await conflict.locator('.conflict-compare textarea').nth(0).inputValue(), newerDraft)
  assert.equal(await conflict.locator('.conflict-compare textarea').nth(1).inputValue(), file.text)
  await conflict.getByRole('button', { name: '保留草稿', exact: true }).click()
  assert.equal(await readEditor(), newerDraft)
  await dirtyTab().waitFor()
  assert.equal(file.text, '# external fixture edit\nvalue = 999\n')
  checks.push('409 conflict keeps both external fixture contents and current unsaved draft until user chooses')
  await screenshot('04-conflict-keeps-draft')

  phase('latest provider error remains current')
  await page.evaluate(() => window.__editorSaveFixture.emitSession([
    { type: 'user_message', seq: 65, payload: { text: 'current fixture request' } },
    { type: 'error', seq: 66, ts: '2026-10-08T02:13:00Z', payload: { code: 'provider_error', message: 'provider fixture: current request failed' } },
    { type: 'done', seq: 67, payload: { stop_reason: 'error' } },
  ]))
  await page.locator('.ai .connection-status .error').filter({ hasText: '模型请求失败' }).waitFor()
  assert.match(await page.locator('.ai .connection-status').innerText(), /会话已连接/)
  const currentError = page.locator('.ai .msg.error').filter({ hasText: 'provider fixture: current request failed' })
  assert.equal(await currentError.locator('details').count(), 0)
  assert.equal(await currentError.locator('.bubble.error').isVisible(), true)
  assert.match(await currentError.innerText(), /本次请求错误/)
  assert.equal(await historical.evaluate(element => element.open), false)
  checks.push('latest provider error remains expanded and failed while the connected websocket and old history retain separate states')
  await screenshot('05-current-provider-error')
  const audit = await page.evaluate(() => window.__editorSaveFixture.audit)
  assert.deepEqual(audit.filter(item => item.kind.startsWith('blocked')), [])
  assert.deepEqual(unexpected, []); assert.deepEqual(forbidden, []); assert.deepEqual(errors, [])
  assert.equal(writes.length, 3)
  checks.push('all API and websocket transport is fixture-only; no real disk/index/terminal/model requests and zero runtime errors')
} catch (error) {
  failure = redact(error.stack || error.message)
  if (page) { try { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }); screenshots.push('failure.png') } catch {} }
} finally {
  clearTimeout(watchdog)
  saveGate?.resolve(); indexGate.resolve()
  await browser.close()
  const report = { passed: !failure, stage, checks, screenshots, apiCount, writes, refreshRequests, pendingIndexRequests, unexpected, forbidden, errors, expectedConsole, failure }
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
  if (failure) process.exitCode = 1
}
