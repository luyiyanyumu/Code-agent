import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'

// Real Vue, Monaco and Chrome. Every API is intercepted; no project files, scan or model are touched.
// node web/verify-ide.mjs [baseUrl] [screenshotsDir]
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const playwright = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:5175'
const origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/ide-ui', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], screenshots = [], runtimeErrors = [], consoleErrors = [], failedRequests = []
const unexpectedRequests = [], forbiddenRequests = [], externalRequests = [], sessionRequests = [], saves = []
const findings = []
const mockedWebSockets = []
let interceptedApiRequests = 0, nextSaveGate = null, failure = null, page
let socketTransport = 'playwright-route'
const redact = value => String(value).replace(/([?&]token=)[^&\s"']+/gi, '$1[redacted]')
  .replace(/Bearer\s+[^\s"']+/gi, 'Bearer [redacted]').replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[redacted]')
const requestLabel = request => `${request.method()} ${new URL(request.url()).pathname}`
const normalizedText = value => String(value).replace(/\r\n/g, '\n')
const hash = text => createHash('sha1').update(text).digest('hex')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const lineFile = (prefix, count) => Array.from({ length: count }, (_, index) => `// ${prefix} line ${String(index + 1).padStart(3, '0')}`).join('\n')
const alphaMain = lineFile('alpha main', 180).split('\n')
alphaMain[7] = 'const literal = "[a+b].*";'
const originals = {
  alphaMain: alphaMain.join('\n'), alphaHelper: lineFile('alpha helper', 30),
  betaMain: lineFile('beta main', 90), betaHelper: lineFile('beta helper', 24),
}
const projects = [
  { id: 'fixture-alpha', name: 'Alpha 工作区', root_path: 'E:/ide-fixture/alpha', available: true, allow_direct_write: false, git: { is_repo: false } },
  { id: 'fixture-beta', name: 'Beta 工作区', root_path: 'E:/ide-fixture/beta', available: true, allow_direct_write: false, git: { is_repo: false } },
]
const sessions = new Map(projects.map(project => [`session-${project.id}`, {
  id: `session-${project.id}`, code_project_id: project.id, permission_mode: 'read-only',
  model: 'fixture-model', status: 'open', web_enabled: false, busy: false,
}]))
const disks = new Map(projects.map((project, index) => [project.id, new Map([
  ['main.js', { text: index ? originals.betaMain : originals.alphaMain }],
  ['helper.js', { text: index ? originals.betaHelper : originals.alphaHelper }],
  ['src/util.js', { text: `export const workspace = "${index ? 'beta' : 'alpha'}";\n// utility file` }],
])]))
for (const files of disks.values()) for (const file of files.values()) file.sha1 = hash(file.text)
const diskFile = (pid, filename) => {
  const project = projects.find(item => item.id === pid), file = disks.get(pid)?.get(filename)
  if (!file) return null
  return { ...file, path: filename, name: path.posix.basename(filename), lang: 'javascript',
    start_line: 1, end_line: file.text.split('\n').length, total_lines: file.text.split('\n').length,
    uri: `file:///${project.root_path}/${filename}`, editable: true, size: Buffer.byteLength(file.text), secret: false }
}
const inventory = pid => [...disks.get(pid).keys()].map(filename => ({ path: filename, name: path.posix.basename(filename), size: Buffer.byteLength(disks.get(pid).get(filename).text) }))
const directory = (pid, prefix) => {
  const dirs = new Map(), files = []
  for (const file of inventory(pid)) {
    if (prefix && !file.path.startsWith(`${prefix}/`)) continue
    const rest = file.path.slice(prefix ? prefix.length + 1 : 0), parts = rest.split('/')
    if (parts.length === 1) files.push(file)
    else dirs.set(parts[0], { name: parts[0], path: `${prefix ? `${prefix}/` : ''}${parts[0]}` })
  }
  return { dirs: [...dirs.values()], files, truncated: false }
}
const mapFixture = pid => ({
  project: projects.find(item => item.id === pid), change_seq: 1,
  structure: { id: 'dir:', kind: 'dir', name: 'fixture', path: '', children: inventory(pid).map(file => ({ ...file, id: `file:${file.path}`, kind: 'file', lang: 'javascript', loc: 20, children: [] })) },
  features: [], edges: { imports: [], calls: [], routes: [] }, stats: { files: 3, symbols: 0 },
})
const diagnostics = pid => ({ seq: 1, counts: { error: 1, warning: 1 }, files: {}, symbols: {}, items: [
  { path: 'helper.js', line: 12, column: 4, severity: 1, message: `${pid === 'fixture-alpha' ? 'Alpha' : 'Beta'} fixture diagnostic`, source: 'fixture-lsp' },
  { path: 'main.js', line: 8, column: 1, severity: 2, message: 'Fixture warning', source: 'fixture-lsp' },
] })

const browser = await playwright.chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block', permissions: ['clipboard-read', 'clipboard-write'] })
  await context.addInitScript(() => {
    if (sessionStorage.getItem('ide-fixture-initialized')) return
    sessionStorage.setItem('ide-fixture-initialized', '1')
    localStorage.setItem('code-agent:nav-collapsed', 'true')
    localStorage.setItem('code-agent-code-left', '232')
    localStorage.setItem('code-agent-code-right', '310')
    // Do not force the code view: fresh users should get the default workbench.
  })
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname, method = request.method()
    if (url.origin !== origin) { externalRequests.push(requestLabel(request)); return route.abort('blockedbyclient') }
    if (!pathname.startsWith('/api/') && pathname !== '/healthz') return route.continue()
    interceptedApiRequests++
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
    if (/\/scan$|\/prompt$|\/feature-briefs\/fill$|\/reindex$|\/settings$/.test(pathname)) {
      forbiddenRequests.push(requestLabel(request)); return reply({ detail: 'Blocked by fixture verification' }, 403)
    }
    if (pathname === '/healthz') return reply({ ok: true, status: 'ok' })
    if (pathname === '/api/v1/auth/me') return reply({ id: 'ide-fixture-user', username: 'fixture', role: 'admin' })
    if (pathname === '/api/v1/web' && method === 'GET') return reply({ available: false, reason: 'Fixture network search is disabled', how_to: 'Browser verification does not call external services' })
    if (pathname === '/api/v1/code/meta') return reply({ templates: ['empty'], browse_enabled: false, features: {}, projects_root: '', git_available: false })
    if (pathname === '/api/v1/code/projects' && method === 'GET') return reply(projects)
    const sessionMatch = pathname.match(/^\/api\/v1\/sessions\/([^/]+)(.*)$/)
    if (sessionMatch && sessions.has(sessionMatch[1]) && method === 'GET') {
      const session = sessions.get(sessionMatch[1]), suffix = sessionMatch[2]
      if (!suffix) return reply(session)
      if (suffix === '/events') return reply([])
      if (suffix === '/checkpoints') return reply({ items: [], busy: false })
      if (suffix === '/approvals/pending') return reply({})
    }
    const match = pathname.match(/^\/api\/v1\/code\/projects\/([^/]+)(.*)$/)
    const pid = match?.[1], suffix = match?.[2]
    if (pid && disks.has(pid)) {
      if (!suffix && method === 'GET') return reply(projects.find(project => project.id === pid))
      if (suffix === '/session' && method === 'POST') {
        const body = request.postDataJSON() || {}
        sessionRequests.push({ projectId: pid, permission_mode: body.permission_mode || 'read-only' })
        if (body.permission_mode === 'workspace-write') forbiddenRequests.push(`${requestLabel(request)} workspace-write`)
        return reply(sessions.get(`session-${pid}`))
      }
      if (suffix === '/file' && method === 'PUT') {
        const body = request.postDataJSON(), file = disks.get(pid).get(body.path)
        if (!file) return reply({ detail: 'Fixture file missing' }, 404)
        const entry = { projectId: pid, path: body.path, content: body.content, expected_sha1: body.expected_sha1, conflict: body.expected_sha1 !== file.sha1 }
        saves.push(entry)
        const gate = nextSaveGate
        if (gate) {
          nextSaveGate = null
          gate.started.resolve(entry)
          await Promise.race([gate.release.promise, new Promise(resolve => setTimeout(resolve, 5000))])
        }
        if (body.expected_sha1 !== file.sha1) return reply({ detail: '文件已由其他操作更新，请重新读取后再保存。' }, 409)
        const changed = body.content !== file.text
        file.text = body.content; file.sha1 = hash(file.text)
        return reply({ changed, sha1: file.sha1, lines: file.text.split('\n').length, version: saves.length + 1 })
      }
      if (method === 'GET') {
        if (suffix === '/agents') return reply({ lead: { session_id: `session-${pid}`, role: 'lead', state: 'idle', depth: 0 }, agents: [], limits: { max_depth: 3, max_parallel: 2 }, running: 0, queued: 0 })
        if (suffix === '/file') {
          const file = diskFile(pid, url.searchParams.get('path'))
          return file ? reply(file) : reply({ detail: 'Fixture file missing' }, 404)
        }
        if (suffix === '/tree') return reply(directory(pid, url.searchParams.get('path') || ''))
        if (suffix === '/files') return reply({ files: inventory(pid), truncated: false, inventory_truncated: false })
        if (suffix === '/search') {
          const query = url.searchParams.get('query') || '', caseSensitive = url.searchParams.get('case_sensitive') === 'true'
          assert.equal(url.searchParams.get('regex'), 'false', 'IDE default global search must be literal')
          const matches = []
          for (const [filename, file] of disks.get(pid)) file.text.split('\n').forEach((text, index) => {
            const haystack = caseSensitive ? text : text.toLowerCase(), needle = caseSensitive ? query : query.toLowerCase(), column = haystack.indexOf(needle)
            if (column >= 0) matches.push({ path: filename, line: index + 1, column: column + 1, text })
          })
          return reply({ matches, files_scanned: disks.get(pid).size, truncated: false, inventory_truncated: false })
        }
        if (suffix === '/map') return reply(mapFixture(pid))
        if (suffix === '/index') return reply({ change_seq: 1, scanning: false, briefs: { running: false } })
        if (suffix === '/map/delta') return reply({ to_seq: 1, changes: [], added: [], updated: [], removed: [] })
        if (suffix === '/feature-briefs') return reply({ missing: { total: 0 }, items: [] })
        if (suffix === '/lsp') return reply({ servers: [] })
        if (suffix === '/diagnostics') return reply(diagnostics(pid))
        if (suffix === '/patches') return reply({ patches: [], counts: { total: 0, active: 0 } })
        if (suffix === '/tasks' || suffix === '/batches') return reply([])
        if (suffix === '/architecture') return reply({ rules: {}, errors: [] })
        if (suffix === '/git') return reply({ is_repo: false, changes: [] })
        if (suffix === '/doc') return reply({ markdown: '# IDE fixture', lines: 1, chars: 13, available_sections: ['overview'] })
      }
    }
    unexpectedRequests.push(requestLabel(request))
    return reply({ detail: 'API is not covered by IDE fixtures' }, 501)
  })
  if (typeof context.routeWebSocket === 'function') await context.routeWebSocket('**/api/**', socket => {
    const pathname = new URL(socket.url()).pathname
    const match = pathname.match(/^\/api\/v1\/sessions\/([^/]+)\/stream$/)
    const session = match && sessions.get(match[1])
    if (!session) { forbiddenRequests.push(`WS ${pathname}`); socket.close(); return }
    mockedWebSockets.push(pathname)
    // This is an in-browser fixture socket. Never call connectToServer().
    socket.onMessage(message => {
      let frame
      try { frame = JSON.parse(String(message)) } catch { forbiddenRequests.push(`WS ${pathname} unrecognized client frame`); return }
      if (frame.type === 'ping') socket.send(JSON.stringify({ type: 'pong', seq: 0, payload: {} }))
      else forbiddenRequests.push(`WS ${pathname} client ${frame.type || 'unknown'} frame`)
    })
    socket.send(JSON.stringify({ type: 'session_meta', seq: 0, payload: session }))
  })
  else {
    socketTransport = 'browser-api-fixture'
    // Older bundled Playwright has no socket routing. Replace only API sockets before app code runs;
    // Vite's development socket remains native, and no API socket can reach any server.
    await context.addInitScript(({ sessionEntries }) => {
      const NativeSocket = window.WebSocket, fixtureSessions = new Map(sessionEntries)
      const audit = record => {
        const records = JSON.parse(sessionStorage.getItem('ide-fixture-ws-audit') || '[]')
        records.push(record); sessionStorage.setItem('ide-fixture-ws-audit', JSON.stringify(records))
      }
      class FixtureSocket extends EventTarget {
        static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
        constructor(url, protocols) {
          super()
          const target = new URL(url, location.href)
          if (!target.pathname.startsWith('/api/')) return new NativeSocket(url, protocols)
          this.url = target.href; this.readyState = 0; this.bufferedAmount = 0
          this.protocol = ''; this.extensions = ''; this.binaryType = 'blob'
          this.CONNECTING = 0; this.OPEN = 1; this.CLOSING = 2; this.CLOSED = 3
          const match = target.pathname.match(/^\/api\/v1\/sessions\/([^/]+)\/stream$/)
          const session = match && fixtureSessions.get(match[1])
          audit({ pathname: target.pathname, kind: session ? 'connected' : 'blocked' })
          queueMicrotask(() => {
            if (this.readyState !== 0) return
            if (!session) { this.close(4404, 'No fixture socket'); return }
            this.readyState = 1
            this.emit('open', new Event('open'))
            this.emit('message', new MessageEvent('message', { data: JSON.stringify({ type: 'session_meta', seq: 0, payload: session }) }))
          })
        }
        emit(type, event) { this.dispatchEvent(event); this[`on${type}`]?.(event) }
        send(message) {
          if (this.readyState !== 1) throw new DOMException('Fixture socket is closed', 'InvalidStateError')
          let frame
          try { frame = JSON.parse(String(message)) } catch { frame = {} }
          if (frame.type === 'ping') queueMicrotask(() => this.emit('message', new MessageEvent('message', { data: JSON.stringify({ type: 'pong', seq: 0, payload: {} }) })))
          else audit({ pathname: new URL(this.url).pathname, kind: 'blocked-frame', frameType: frame.type || 'unknown' })
        }
        close(code = 1000, reason = '') {
          if (this.readyState === 3) return
          this.readyState = 3
          queueMicrotask(() => this.emit('close', new CloseEvent('close', { code, reason, wasClean: true })))
        }
      }
      window.WebSocket = FixtureSocket
    }, { sessionEntries: [...sessions] })
  }
  page = await context.newPage()
  page.setDefaultTimeout(15_000)
  page.on('dialog', dialog => { if (dialog.type() === 'beforeunload') dialog.accept(); else { forbiddenRequests.push(`Unexpected native dialog: ${dialog.type()}`); dialog.dismiss() } })
  page.on('pageerror', error => runtimeErrors.push(redact(error.message)))
  page.on('console', message => { if (message.type() === 'error' && !/status of 409/.test(message.text())) consoleErrors.push(redact(message.text())) })
  page.on('requestfailed', request => failedRequests.push(`${requestLabel(request)} ${request.failure()?.errorText || ''}`))
  page.on('response', response => {
    const pathname = new URL(response.url()).pathname
    if (response.status() >= 400 && !(response.status() === 409 && response.request().method() === 'PUT' && pathname.endsWith('/file'))) failedRequests.push(`${response.status()} ${pathname}`)
  })
  const editor = () => page.locator('.pane .monaco-editor').filter({ visible: true })
  // Monaco 0.56 uses Chrome's native EditContext instead of its legacy textarea.
  const input = () => editor().locator('[role="textbox"]')
  const tabs = () => page.locator('.editor-tabs .editor-tab')
  const tab = filename => page.locator('.editor-tabs .tab-select').filter({ has: page.locator(`.tab-label:text-is("${filename}")`) })
  const activePath = () => page.locator('.pane .bar .path')
  const dirty = filename => tab(filename).locator('.dirty-dot')
  const waitActive = async filename => {
    await activePath().filter({ hasText: new RegExp(`^${filename.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`) }).waitFor()
    // The EditContext input has zero area until focused; rendered lines verify editor readiness.
    await input().waitFor({ state: 'attached' })
    await page.waitForFunction(() => !!document.querySelector('.pane .monaco-editor .view-lines .view-line'))
  }
  const treeOpen = async filename => {
    await page.locator(`.tree .name[title="${filename}"]`).click()
    await waitActive(filename)
  }
  const activate = async filename => { await tab(filename).click(); await waitActive(filename) }
  const readEditor = async () => {
    await input().focus()
    await page.keyboard.press('Control+A'); await page.keyboard.press('Control+C')
    return normalizedText(await page.evaluate(() => navigator.clipboard.readText()))
  }
  const append = async text => { await input().focus(); await page.keyboard.press('Control+End'); await page.keyboard.insertText(text) }
  const waitDirty = async filename => dirty(filename).waitFor()
  const waitClean = async filename => dirty(filename).waitFor({ state: 'detached' })
  const screenshot = async name => {
    const filename = `${name}.png`; await page.screenshot({ path: path.join(output, filename), fullPage: true }); screenshots.push(filename)
  }
  const boundedPage = async label => {
    const geometry = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }))
    assert.ok(geometry.scrollWidth <= geometry.width + 2, `${label}: horizontal overflow ${JSON.stringify(geometry)}`)
    checks.push(label)
  }
  const firstVisibleLine = () => editor().locator('.margin-view-overlays .line-numbers').evaluateAll(elements => {
    const frame = elements[0]?.closest('.monaco-editor')?.getBoundingClientRect()
    const visible = elements.filter(element => { const r = element.getBoundingClientRect(); return frame && r.bottom > frame.top + 8 && r.top < frame.bottom - 8 })
    visible.sort((left, right) => left.getBoundingClientRect().top - right.getBoundingClientRect().top)
    return Number(visible[0]?.textContent || 0)
  })
  const settledLine = async () => {
    let previous = -1, stable = 0
    for (let i = 0; i < 12; i++) {
      const line = await firstVisibleLine()
      if (line === previous) stable++
      else stable = 0
      if (stable >= 2) return line
      previous = line
      await page.waitForTimeout(100)
    }
    return firstVisibleLine()
  }
  const closeTab = async filename => page.getByRole('button', { name: `关闭 ${filename}`, exact: true }).click()
  const closeDialog = () => page.getByRole('dialog', { name: '保存未完成的更改', exact: true })
  const conflictDialog = () => page.getByRole('dialog', { name: '文件保存冲突', exact: true })
  const selectProject = async pid => {
    await page.getByRole('button', { name: '切换项目', exact: true }).click()
    await page.locator(`.project-menu .project-select[data-project-id="${pid}"]`).click()
    await page.waitForFunction(expected => document.querySelector('.project-switcher-trigger')?.dataset.projectId === expected && !document.querySelector('.status')?.textContent.includes('准备会话'), pid)
  }

  await page.goto(base, { waitUntil: 'networkidle' })
  await page.locator('.code .workbench-bar').waitFor()
  assert.equal(await page.locator('.code .tabs button.active').innerText(), '编辑器')
  assert.equal(await page.locator('.right-tabs .perm').innerText(), '执行只读')
  await page.locator('.ai .head .chip').filter({ hasText: 'fixture-model' }).waitFor()
  assert.equal(await page.locator('.ai .composer textarea').isVisible(), true)
  checks.push('fresh user enters the editor workbench; manual editor is writable and AI remains read-only')
  await treeOpen('main.js')
  assert.equal(await readEditor(), originals.alphaMain)
  assert.equal(await page.locator('.pane .chip.lock').count(), 0)
  await append('\n// draft-A')
  await waitDirty('main.js')
  const mainDraft = `${originals.alphaMain}\n// draft-A`
  assert.equal(await readEditor(), mainDraft)
  await input().focus(); await page.keyboard.press('Control+Home')
  for (let i = 0; i < 5; i++) await page.keyboard.press('PageDown')
  const firstLineBefore = await settledLine()
  assert.ok(firstLineBefore > 20, `scroll test did not scroll (${firstLineBefore})`)
  await treeOpen('helper.js')
  await append('\n// draft-B')
  await waitDirty('helper.js')
  await activate('main.js')
  const firstLineAfter = await settledLine()
  assert.ok(Math.abs(firstLineAfter - firstLineBefore) <= 2, `switching tabs restores main.js viewport (${firstLineBefore} → ${firstLineAfter})`)
  assert.equal(await tabs().count(), 2)
  assert.equal(await readEditor(), mainDraft)
  // Native EditContext may split a pasted newline/comment into more than one undo edit.
  let undoSteps = 0, undone = mainDraft
  while (undone !== originals.alphaMain && undoSteps < 4) {
    await input().focus(); await page.keyboard.press('Control+Z')
    undone = await readEditor(); undoSteps++
  }
  assert.equal(undone, originals.alphaMain, 'tab switching preserves the Monaco undo stack')
  await waitClean('main.js')
  for (let i = 0; i < undoSteps; i++) { await input().focus(); await page.keyboard.press('Control+Y') }
  assert.equal(await readEditor(), mainDraft)
  await waitDirty('main.js')
  checks.push('two file tabs preserve drafts, scroll position and undo/redo across switches')
  await screenshot('01-tabs-unsaved')

  const gate = { started: deferred(), release: deferred() }
  nextSaveGate = gate
  await input().focus(); await page.keyboard.press('Control+End'); await page.keyboard.press('Control+S')
  const submitted = await Promise.race([gate.started.promise, new Promise((_, reject) => setTimeout(() => reject(new Error('Ctrl+S did not reach the fixture save endpoint')), 6000))])
  assert.equal(submitted.content, mainDraft)
  await append('\n// typed-during-save')
  const continued = `${mainDraft}\n// typed-during-save`
  await activate('helper.js')
  assert.equal(await readEditor(), `${originals.alphaHelper}\n// draft-B`)
  await activate('main.js')
  assert.equal(await readEditor(), continued)
  gate.release.resolve()
  await page.locator('.tab-note').filter({ hasText: '已保存 main.js' }).waitFor()
  assert.equal(await readEditor(), continued)
  await waitDirty('main.js')
  assert.equal(disks.get('fixture-alpha').get('main.js').text, mainDraft)
  let continuedUndoSteps = 0, continuedUndone = continued
  while (continuedUndone !== mainDraft && continuedUndoSteps < 4) {
    await input().focus(); await page.keyboard.press('Control+Z')
    continuedUndone = await readEditor(); continuedUndoSteps++
  }
  assert.equal(continuedUndone, mainDraft, 'saving and returning to a tab preserves later edits in its undo stack')
  await waitClean('main.js')
  await page.locator('.pane .chip.dirty').waitFor({ state: 'detached' })
  assert.equal(await page.locator('.pane .bar button').filter({ hasText: /^保存$/ }).isDisabled(), true)
  for (let i = 0; i < continuedUndoSteps; i++) { await input().focus(); await page.keyboard.press('Control+Y') }
  assert.equal(await readEditor(), continued)
  await waitDirty('main.js')
  await input().focus(); await page.keyboard.press('Control+S')
  await waitClean('main.js')
  assert.equal(disks.get('fixture-alpha').get('main.js').text, continued)
  checks.push('delayed save survives typing and A→B→A tab switching; undo reaches the submitted baseline and second save clears the remaining draft')

  await closeTab('helper.js')
  await closeDialog().waitFor()
  assert.match(await closeDialog().innerText(), /helper.js/)
  await page.waitForFunction(() => document.activeElement?.textContent?.trim() === '取消')
  await page.keyboard.press('Shift+Tab')
  assert.equal(await closeDialog().getByRole('button', { name: '保存并关闭', exact: true }).evaluate(element => element === document.activeElement), true)
  await page.keyboard.press('Tab')
  assert.equal(await closeDialog().getByRole('button', { name: '取消', exact: true }).evaluate(element => element === document.activeElement), true)
  await page.keyboard.press('Escape')
  await closeDialog().waitFor({ state: 'detached' })
  assert.equal(await tabs().count(), 2)
  await closeTab('helper.js')
  await closeDialog().getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(await tabs().count(), 2)
  await activate('helper.js')
  assert.equal(await readEditor(), `${originals.alphaHelper}\n// draft-B`)
  await closeTab('helper.js')
  await closeDialog().getByRole('button', { name: '丢弃更改', exact: true }).click()
  await tab('helper.js').waitFor({ state: 'detached' })
  await treeOpen('helper.js')
  assert.equal(await readEditor(), originals.alphaHelper)
  await waitClean('helper.js')
  await append('\n// close-save')
  await waitDirty('helper.js')
  await closeTab('helper.js')
  await closeDialog().getByRole('button', { name: '保存并关闭', exact: true }).click()
  await tab('helper.js').waitFor({ state: 'detached' })
  await treeOpen('helper.js')
  assert.equal(await readEditor(), `${originals.alphaHelper}\n// close-save`)
  await waitClean('helper.js')
  checks.push('closing dirty tabs supports cancel, discard and save; reopened content follows the selected choice')
  checks.push('dirty close dialog focuses a button, traps Tab/Shift+Tab and Escape cancels without discarding text')

  await activate('main.js')
  await append('\n// recovery-A')
  const recoveryA = `${continued}\n// recovery-A`
  await waitDirty('main.js')
  await selectProject('fixture-beta')
  await treeOpen('main.js')
  assert.equal(await readEditor(), originals.betaMain)
  await append('\n// second-project-draft')
  const recoveryB = `${originals.betaMain}\n// second-project-draft`
  await waitDirty('main.js')
  await selectProject('fixture-alpha')
  await waitActive('main.js')
  assert.equal(await readEditor(), recoveryA)
  await input().focus(); await page.keyboard.press('Control+Home')
  for (let i = 0; i < 3; i++) await page.keyboard.press('PageDown')
  const readingLineBeforeReload = await settledLine()
  const readingGeometry = async () => editor().evaluate(element => {
    const rect = element.getBoundingClientRect(), slider = element.querySelector('.scrollbar.vertical .slider')
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, sliderTop: slider?.style.top, sliderHeight: slider?.style.height }
  })
  const readingBefore = { firstVisibleLine: readingLineBeforeReload, geometry: await readingGeometry() }
  await activate('helper.js')
  await page.waitForFunction(() => {
    const raw = localStorage.getItem('code-agent-workbench-v1:ide-fixture-user')
    return raw && JSON.parse(raw).scopes.find(scope => scope.projectId === 'fixture-alpha')?.activeKey === '["fixture-alpha","main","helper.js"]'
  })
  const snapshotBeforeReload = await page.evaluate(() => JSON.parse(localStorage.getItem('code-agent-workbench-v1:ide-fixture-user')))
  // The workbench polls diagnostics/maps; readiness comes from rendered UI, not network idleness.
  await page.reload({ waitUntil: 'domcontentloaded' })
  await waitActive('helper.js')
  assert.equal(await tabs().count(), 2)
  await waitClean('helper.js')
  assert.equal(await readEditor(), `${originals.alphaHelper}\n// close-save`)
  await activate('main.js')
  const readingLineAfterReload = await settledLine()
  const readingAfter = { firstVisibleLine: readingLineAfterReload, geometry: await readingGeometry() }
  fs.writeFileSync(path.join(output, 'reading-position.json'), JSON.stringify({ before: readingBefore, after: readingAfter, snapshotBeforeReload }, null, 2), 'utf8')
  if (Math.abs(readingLineAfterReload - readingLineBeforeReload) > 2) findings.push(`reload reading position changed ${readingLineBeforeReload} → ${readingLineAfterReload}`)
  assert.equal(await readEditor(), recoveryA)
  await waitDirty('main.js')
  await selectProject('fixture-beta')
  await waitActive('main.js')
  assert.equal(await readEditor(), recoveryB)
  await waitDirty('main.js')
  await selectProject('fixture-alpha')
  await waitActive('main.js')
  checks.push('same-path files are isolated by project; reload restores dirty drafts, clean tabs, reading positions and each project active file')
  await screenshot('02-recovered-drafts')

  const external = disks.get('fixture-alpha').get('main.js')
  external.text = `${external.text}\n// external disk update`; external.sha1 = hash(external.text)
  const externalHash = external.sha1
  await input().focus(); await page.keyboard.press('Control+S')
  await conflictDialog().waitFor()
  const compare = conflictDialog().locator('textarea')
  assert.equal(normalizedText(await compare.nth(0).inputValue()), recoveryA)
  assert.equal(normalizedText(await compare.nth(1).inputValue()), external.text)
  await screenshot('03-conflict-compare')
  await page.waitForFunction(() => document.activeElement?.textContent?.trim() === '保留草稿')
  await page.keyboard.press('Tab'); await page.keyboard.press('Tab'); await page.keyboard.press('Tab')
  assert.equal(await compare.nth(0).evaluate(element => element === document.activeElement), true)
  await page.keyboard.press('Shift+Tab')
  assert.equal(await conflictDialog().getByRole('button', { name: '覆盖保存', exact: true }).evaluate(element => element === document.activeElement), true)
  await page.keyboard.press('Escape')
  await conflictDialog().waitFor({ state: 'detached' })
  assert.equal(await readEditor(), recoveryA)
  await input().focus(); await page.keyboard.press('Control+S')
  await conflictDialog().waitFor()
  await conflictDialog().getByRole('button', { name: '保留草稿', exact: true }).click()
  assert.equal(await readEditor(), recoveryA)
  await waitDirty('main.js')
  await input().focus(); await page.keyboard.press('Control+S')
  await conflictDialog().waitFor()
  await conflictDialog().getByRole('button', { name: '覆盖保存', exact: true }).click()
  await conflictDialog().waitFor({ state: 'detached' })
  await waitClean('main.js')
  assert.equal(external.text, recoveryA)
  assert.equal(saves.at(-1).expected_sha1, externalHash)
  assert.equal(await readEditor(), recoveryA)
  assert.ok(saves.filter(save => save.conflict).length >= 2)
  checks.push('409 conflict comparison preserves the original draft/hash; explicit overwrite uses the freshly reviewed disk hash')
  checks.push('conflict dialog traps keyboard focus including comparison textareas; Escape keeps the draft')

  await input().focus(); await page.keyboard.press('Control+P')
  const quick = page.getByRole('dialog', { name: '快速打开文件', exact: true })
  await quick.waitFor()
  const quickInput = quick.getByRole('textbox', { name: '快速打开搜索', exact: true })
  await quickInput.fill('util')
  await quick.getByRole('option').filter({ hasText: 'src/util.js' }).waitFor()
  await quickInput.press('Enter')
  await waitActive('src/util.js')
  assert.equal(await readEditor(), disks.get('fixture-alpha').get('src/util.js').text)
  checks.push('Ctrl+P fuzzy file lookup opens a nested file through keyboard selection')
  await input().focus(); await page.keyboard.press('Control+Shift+F')
  const search = page.getByRole('region', { name: '项目搜索', exact: true })
  const searchInput = page.getByRole('textbox', { name: '全局搜索文本', exact: true })
  await searchInput.fill('[a+b].*'); await searchInput.press('Enter')
  await search.locator('.search-results button').filter({ hasText: 'const literal' }).click()
  await waitActive('main.js')
  await page.locator('.pane[data-highlight-line="8"]').waitFor()
  checks.push('Ctrl+Shift+F searches regex-like text literally and a result opens/highlights its line')
  await page.locator('.workbench-bar .workbench-action').filter({ hasText: /^问题/ }).click()
  await page.locator('.problem-row').filter({ hasText: 'Alpha fixture diagnostic' }).click()
  await waitActive('helper.js')
  await page.locator('.pane[data-highlight-line="12"]').waitFor()
  checks.push('problem list opens the diagnostic file and highlights line 12')
  await screenshot('04-search-and-problems')

  await activate('main.js')
  await append('\n// selection-position-save')
  const savedPositionText = `${recoveryA}\n// selection-position-save`
  await waitDirty('main.js')
  await input().focus(); await page.keyboard.press('Control+Home')
  for (let i = 0; i < 3; i++) await page.keyboard.press('PageDown')
  await page.keyboard.press('Home')
  const cursorStatus = page.locator('.code .status > .mono').filter({ hasText: /Ln \d+, Col \d+/ })
  const selectedStart = Number((await cursorStatus.innerText()).match(/Ln (\d+)/)[1])
  assert.ok(selectedStart > 30, 'selection test uses a scrolled, later portion of the file')
  const selectedContext = `main.js:${selectedStart}-${selectedStart + 2}`
  await page.keyboard.press('Shift+ArrowDown'); await page.keyboard.press('Shift+ArrowDown')
  await page.waitForFunction(expected => document.querySelector('.ai .ctx-label .mono')?.textContent.trim() === expected, selectedContext)
  const positionBeforeSave = await settledLine()
  const cursorBeforeSave = await cursorStatus.innerText()
  await page.keyboard.press('Control+C')
  const selectionBeforeSave = normalizedText(await page.evaluate(() => navigator.clipboard.readText()))
  assert.equal(selectionBeforeSave, `${savedPositionText.split('\n')[selectedStart - 1]}\n${savedPositionText.split('\n')[selectedStart]}\n`)
  await page.keyboard.press('Control+S')
  await waitClean('main.js')
  await page.locator('.pane .chip.dirty').waitFor({ state: 'detached' })
  assert.equal(await cursorStatus.innerText(), cursorBeforeSave, 'saving the active file retains its cursor')
  assert.ok(Math.abs(await settledLine() - positionBeforeSave) <= 2, 'saving the active file retains its viewport')
  assert.equal(await page.locator('.ai .ctx-label .mono').innerText(), selectedContext, 'saving keeps the selected AI context')
  await input().focus(); await page.keyboard.press('Control+C')
  assert.equal(normalizedText(await page.evaluate(() => navigator.clipboard.readText())), selectionBeforeSave, 'saving preserves the complete selection')
  assert.equal(disks.get('fixture-alpha').get('main.js').text, savedPositionText)
  checks.push('saving the current tab retains cursor, viewport, complete selection and AI selection context')
  await screenshot('10-ai-selection-context')
  await page.locator('.ai .ctx').getByRole('button', { name: '清除', exact: true }).click()
  await page.locator('.ai .ctx').waitFor({ state: 'detached' })
  assert.equal(await activePath().innerText(), 'main.js')
  assert.equal(await editor().isVisible(), true)
  assert.equal(await tab('main.js').getAttribute('aria-selected'), 'true')
  assert.equal(await readEditor(), savedPositionText)
  checks.push('a real project AI session displays the selected line range; clearing AI context keeps the document/editor open')

  const beforeReadonly = await readEditor()
  await page.locator('.workbench-bar .workbench-action').filter({ hasText: /^允许编辑$/ }).click()
  await page.locator('.pane .chip.lock').waitFor()
  await append('\n// blocked readonly typing')
  assert.equal(await readEditor(), beforeReadonly)
  assert.equal(await page.locator('.pane .bar button').filter({ hasText: /^保存$/ }).isDisabled(), true)
  await page.locator('.workbench-bar .workbench-action').filter({ hasText: /^编辑只读$/ }).click()
  await page.locator('.pane .chip.lock').waitFor({ state: 'detached' })
  assert.equal(await page.locator('.right-tabs .perm').innerText(), '执行只读')
  assert.equal(sessionRequests.some(request => request.permission_mode === 'workspace-write'), false)
  checks.push('manual read-only toggle blocks edits independently and never requests AI workspace-write permission')

  await page.getByRole('button', { name: '关闭问题面板', exact: true }).click()
  const beforeWidth = (await page.locator('.code .center').boundingBox()).width
  await page.locator('.workbench-bar .layout-toggle').click()
  assert.equal(await page.locator('.code .left').isVisible(), false)
  await page.getByRole('button', { name: 'AI 面板', exact: true }).click()
  assert.equal(await page.locator('.code .right').isVisible(), false)
  const collapsedWidth = (await page.locator('.code .center').boundingBox()).width
  assert.ok(collapsedWidth > beforeWidth + 400, `collapsing both panels releases editor width (${beforeWidth} → ${collapsedWidth})`)
  await boundedPage('desktop workbench has no page horizontal overflow')
  await screenshot('05-collapsed-workbench')
  await page.locator('.workbench-bar .layout-toggle').click()
  await page.getByRole('button', { name: 'AI 面板', exact: true }).click()
  checks.push('both sidebars can collapse and release editor width')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.workspace-switcher button').filter({ hasText: /^代码工作区$/ }).click()
  assert.equal(await page.locator('.code .center').isVisible(), true)
  await boundedPage('390px mobile editor has no page horizontal overflow')
  await screenshot('06-mobile-editor')
  await page.locator('.workspace-switcher button').filter({ hasText: /^项目与文件$/ }).click()
  await boundedPage('390px mobile explorer/search has no page horizontal overflow')
  await screenshot('07-mobile-search')
  await page.locator('.workspace-switcher button').filter({ hasText: /^AI 助手$/ }).click()
  await boundedPage('390px mobile AI panel has no page horizontal overflow')
  const mobileComposer = page.locator('.ai .composer textarea')
  await mobileComposer.fill('仅验证 AI 输入草稿，不发送')
  assert.equal(await mobileComposer.inputValue(), '仅验证 AI 输入草稿，不发送')
  const composerBox = await mobileComposer.boundingBox()
  assert.ok(composerBox && composerBox.y >= 0 && composerBox.y + composerBox.height <= 844, `mobile AI input is clipped ${JSON.stringify(composerBox)}`)
  assert.equal(await page.locator('.ai .send-row').getByRole('button', { name: '发送', exact: true }).isEnabled(), true)
  checks.push('390px mobile AI composer stays visible and accepts a draft without sending a prompt')
  await screenshot('08-mobile-ai')
  await page.locator('.workspace-switcher button').filter({ hasText: /^代码工作区$/ }).click()
  await page.getByRole('button', { name: /搜索文件/ }).click()
  await quick.waitFor()
  await boundedPage('390px mobile quick-open dialog has no page horizontal overflow')
  await screenshot('09-mobile-quick-open')
  await quickInput.press('Escape')

  if (socketTransport === 'browser-api-fixture') {
    const audit = await page.evaluate(() => JSON.parse(sessionStorage.getItem('ide-fixture-ws-audit') || '[]'))
    for (const event of audit) {
      if (event.kind === 'connected') mockedWebSockets.push(event.pathname)
      else forbiddenRequests.push(`WS ${event.pathname} ${event.kind} ${event.frameType || ''}`.trim())
    }
  }

  assert.deepEqual(runtimeErrors, [], 'browser runtime errors')
  assert.deepEqual(consoleErrors, [], 'browser console errors, excluding deliberate 409 fixtures')
  assert.deepEqual(failedRequests, [], 'failed browser requests, excluding deliberate 409 fixtures')
  assert.deepEqual(unexpectedRequests, [], 'fixture routes must cover every API')
  assert.deepEqual(forbiddenRequests, [], 'no model, scan, project settings, native permission dialog or unmocked API websocket')
  assert.ok(mockedWebSockets.length >= 2, 'project AI sessions connect only to the browser fixture sockets')
  assert.deepEqual(externalRequests, [], 'no external requests')
  assert.deepEqual(findings, [], 'IDE verification findings')
} catch (error) {
  failure = redact(error.stack || error.message)
  if (page) {
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    const body = await page.locator('body').innerText().catch(() => '')
    fs.writeFileSync(path.join(output, 'failure.txt'), `${failure}\n\n${redact(body)}`, 'utf8')
    const inputs = await page.locator('.monaco-editor [tabindex], .monaco-editor textarea').evaluateAll(elements => elements.map(element => ({ tag: element.tagName, class: element.className, role: element.getAttribute('role'), ariaLabel: element.getAttribute('aria-label') }))).catch(() => [])
    fs.writeFileSync(path.join(output, 'monaco-inputs.json'), JSON.stringify(inputs, null, 2), 'utf8')
    const snapshot = await page.evaluate(() => localStorage.getItem('code-agent-workbench-v1:ide-fixture-user')).catch(() => '')
    fs.writeFileSync(path.join(output, 'recovery-snapshot.json'), snapshot || 'null', 'utf8')
  }
} finally {
  nextSaveGate?.release.resolve()
  if (socketTransport === 'browser-api-fixture' && page && !mockedWebSockets.length) {
    const audit = await page.evaluate(() => JSON.parse(sessionStorage.getItem('ide-fixture-ws-audit') || '[]')).catch(() => [])
    for (const event of audit) {
      if (event.kind === 'connected') mockedWebSockets.push(event.pathname)
      else forbiddenRequests.push(`WS ${event.pathname} ${event.kind} ${event.frameType || ''}`.trim())
    }
  }
  const report = { passed: !failure, checks, screenshots, findings, runtimeErrors, consoleErrors, failedRequests,
    unexpectedRequests, forbiddenRequests, externalRequests, interceptedApiRequests, sessionRequests, mockedWebSockets, socketTransport,
    saves: saves.map(({ content, ...save }) => ({ ...save, contentChars: content.length })), failure }
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2), 'utf8')
  console.log(JSON.stringify(report, null, 2))
  await browser.close()
  if (failure) process.exitCode = 1
}
