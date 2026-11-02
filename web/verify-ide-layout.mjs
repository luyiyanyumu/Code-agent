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
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/ide-layout-browser', import.meta.url)))
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
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block', permissions: [] })
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
      if (suffix === '/approvals/pending' || suffix === '/questions/pending') return reply({})
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
        if (suffix === '/conversations') return reply({ conversations: [{ id: `session-${pid}`, kind: 'main', title: '项目对话', busy: false }], default_session_id: `session-${pid}` })
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
        if (suffix === '/terminals') return reply({ available: false, reason: '隔离验证不启动真实终端', terminals: [], shells: [] })
        if (suffix === '/run-configs') return reply({ configs: [] })
        if (suffix === '/runs') return reply({ runs: [] })
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

  const baselineOnly = process.argv.includes('--baseline')
  const responsiveOnly = process.argv.includes('--responsive-only')
  const snapshot = async name => {
    await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true }); screenshots.push(name + '.png')
  }
  const bounded = async label => {
    const geometry = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth}))
    assert.ok(geometry.scroll <= geometry.width + 2, label + ' horizontal overflow: ' + JSON.stringify(geometry))
    checks.push(label + ': no page overflow')
  }
  await page.goto(base, { waitUntil: 'domcontentloaded' })
  await page.locator('.code .workbench-bar').waitFor()
  await page.getByRole('button', { name: '切换项目', exact: true }).waitFor()
  await page.waitForFunction(() => !document.querySelector('[aria-label="切换项目"]')?.disabled)
  await page.locator('.tree .name[title="main.js"]').click()
  await page.locator('.monaco-editor .view-lines').waitFor()
  await page.waitForFunction(() => [...document.querySelectorAll('.monaco-editor .view-line')].some(line => line.textContent.replace(/\s/g, ' ').includes('alpha main')))
  await bounded('1440px')
  await snapshot(baselineOnly ? 'baseline-1440' : 'ide-1440')
  if (!baselineOnly && !responsiveOnly) {
    const menus = page.getByRole('menubar', { name: 'IDE 菜单' })
    const menuTrigger = label => menus.getByRole('menuitem', { name: label, exact: true })
    const command = async (menu, label, role = 'menuitem') => {
      await menuTrigger(menu).click()
      const item = page.getByRole('menu', { name: menu, exact: true }).getByRole(role, { name: label })
      await item.click()
    }
    const nav = page.getByRole('navigation', { name: '工具窗口' })
    const left = page.locator('.layout > .left'), right = page.locator('.layout > .right')
    const strip = page.locator('.tool-window-strip')
    const panel = page.locator('.bottom-panel')
    const inspectModel = async (edit, filename = 'main.js') => page.evaluate(async ({ shouldEdit, filename }) => {
      const monaco = (await import('/src/lib/monacoSetup.js')).ensureMonaco()
      const editor = monaco.editor.getEditors().find(item => item.getModel()?.uri.toString().endsWith('/' + filename))
      if (!editor) throw Error('Live Monaco fixture editor not found')
      if (shouldEdit) {
        window.ideLayoutModels ||= {}
        window.ideLayoutModels[filename] ||= editor.getModel()
        const model = editor.getModel(), line = model.getLineCount(), column = model.getLineMaxColumn(line)
        editor.executeEdits('ide-layout-fixture', [{ range: new monaco.Range(line, column, line, column), text: '\n// isolated IDE menu save', forceMoveMarkers: true }])
      }
      editor.focus()
      return { text: editor.getValue(), sameModel: !window.ideLayoutModels?.[filename] || window.ideLayoutModels[filename] === editor.getModel() }
    }, { shouldEdit: edit, filename })
    const waitModel = filename => page.waitForFunction(async target => {
      const monaco = (await import('/src/lib/monacoSetup.js')).ensureMonaco()
      return monaco.editor.getEditors().some(editor => editor.getModel()?.uri.toString().endsWith('/' + target))
    }, filename)
    await menus.waitFor()
    await menuTrigger('文件').focus(); await page.keyboard.press('ArrowDown')
    await page.getByRole('menu', { name: '文件', exact: true }).waitFor()
    await page.keyboard.press('ArrowRight')
    await page.getByRole('menu', { name: '编辑', exact: true }).waitFor()
    await page.keyboard.press('Escape')
    assert.equal(await menuTrigger('编辑').evaluate(element => element === document.activeElement), true)
    checks.push('menu keyboard opens, switches groups and restores trigger focus on Escape')

    const edited = await inspectModel(true)
    await page.locator('.editor-tabs .dirty-dot').waitFor()
    const savesBefore = saves.length
    await command('文件', /^保存(?:当前)?(?:文件)?(?:\s|$)/)
    await page.waitForFunction(() => !document.querySelector('.editor-tabs .dirty-dot'))
    assert.equal(saves.length, savesBefore + 1)
    assert.equal(disks.get('fixture-alpha').get('main.js').text, edited.text)
    assert.equal((await inspectModel(false)).sameModel, true)
    checks.push('File menu saves the active Monaco document into the memory-only API fixture')

    await page.locator('.tree .name[title="helper.js"]').click()
    await waitModel('helper.js')
    const helperDraft = await inspectModel(true, 'helper.js')
    await page.locator('.tree .name[title="main.js"]').click()
    await waitModel('main.js')
    const mainDraft = await inspectModel(true)
    const allSavesBefore = saves.length
    await command('文件', /全部保存/)
    await page.waitForFunction(() => !document.querySelector('.editor-tabs .dirty-dot'))
    assert.equal(saves.length, allSavesBefore + 2)
    assert.equal(disks.get('fixture-alpha').get('helper.js').text, helperDraft.text)
    assert.equal(disks.get('fixture-alpha').get('main.js').text, mainDraft.text)
    checks.push('Save All persists both dirty tabs through the real workbench save pipeline without disk or provider access')

    await nav.getByRole('button', { name: '项目文件', exact: true }).click()
    assert.equal(await left.isVisible(), false)
    await nav.getByRole('button', { name: '项目文件', exact: true }).click()
    assert.equal(await left.isVisible(), true)
    await nav.getByRole('button', { name: '搜索文件或代码', exact: true }).click()
    await page.getByRole('dialog', { name: '搜索项目', exact: true }).waitFor()
    await page.keyboard.press('Escape')
    assert.equal(await page.getByRole('dialog', { name: '搜索项目', exact: true }).count(), 0)
    await nav.getByRole('button', { name: 'AI 助手', exact: true }).click()
    assert.equal(await right.isVisible(), false)
    await nav.getByRole('button', { name: 'AI 助手', exact: true }).click()
    assert.equal(await right.isVisible(), true)
    assert.equal((await inspectModel(false)).sameModel, true)
    checks.push('activity bar controls project files, unified search and AI without remounting the editor')

    await strip.getByRole('button', { name: /^问题/ }).click()
    await page.getByLabel('筛选诊断').waitFor()
    await panel.locator('.bottom-tabs').getByRole('button', { name: '终端', exact: true }).click()
    await page.getByText('隔离验证不启动真实终端', { exact: true }).waitFor()
    const separator = page.getByRole('separator', { name: '调整底部面板高度', exact: true })
    const oldHeight = (await panel.boundingBox()).height
    await separator.focus(); await page.keyboard.press('ArrowUp')
    const keyboardHeight = (await panel.boundingBox()).height
    assert.ok(keyboardHeight > oldHeight + 4, `keyboard resize did not increase the panel (${oldHeight} → ${keyboardHeight})`)
    const handle = await separator.boundingBox()
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2)
    await page.mouse.down(); await page.mouse.move(handle.x + handle.width / 2, handle.y - 35, { steps: 5 }); await page.mouse.up()
    assert.ok((await panel.boundingBox()).height > keyboardHeight + 4)
    await snapshot('ide-tools-1440')
    checks.push('bottom tool windows expose real diagnostics and safe terminal availability; separator supports keys and drag')

    await command('视图', /专注模式/, 'menuitemcheckbox')
    assert.equal(await left.isVisible(), false); assert.equal(await right.isVisible(), false)
    await page.getByRole('button', { name: '退出专注模式', exact: true }).first().waitFor()
    await snapshot('ide-focus-1440')
    await page.keyboard.press('Control+Shift+F12')
    assert.equal(await left.isVisible(), true); assert.equal(await right.isVisible(), true)
    await nav.getByRole('button', { name: 'AI 助手', exact: true }).click()
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('.monaco-editor .view-lines').waitFor()
    await page.waitForFunction(() => !document.querySelector('[aria-label="切换项目"]')?.disabled)
    assert.equal(await right.isVisible(), false)
    await command('视图', /恢复默认布局/)
    assert.equal(await left.isVisible(), true); assert.equal(await right.isVisible(), true)
    checks.push('focus mode restores prior panes, explicit pane visibility persists across reload and default layout can be restored')

    await inspectModel(false)
    await page.keyboard.press('Control+Shift+P')
    const palette = page.getByRole('dialog', { name: '命令面板', exact: true })
    await palette.waitFor()
    await palette.getByRole('combobox', { name: '搜索命令', exact: true }).fill('终端')
    await palette.getByRole('option').filter({ hasText: '终端' }).first().waitFor()
    await page.keyboard.press('Enter')
    await palette.waitFor({ state: 'detached' })
    await page.getByText('隔离验证不启动真实终端', { exact: true }).waitFor()
    checks.push('global command palette opens while Monaco is focused, filters commands and executes a tool-window action')
    await command('视图', /恢复默认布局/)
    await bounded('1440px final')
    await snapshot('ide-1440')
    await page.setViewportSize({ width: 768, height: 1024 }); await bounded('768px'); await snapshot('ide-768')
  }
  if (responsiveOnly) {
    await page.setViewportSize({ width: 768, height: 1024 })
    await bounded('768px')
    assert.equal(await page.locator('.workspace-switcher').isVisible(), false)
    await snapshot('ide-768')
  }
  await page.setViewportSize({ width: 390, height: 844 })
  await bounded('390px')
  await snapshot(baselineOnly ? 'baseline-390' : 'ide-390')
  if (!baselineOnly) {
    const nav = page.getByRole('navigation', { name: '工具窗口' })
    await nav.getByRole('button', { name: '项目文件', exact: true }).click()
    assert.equal(await page.locator('.layout > .left').isVisible(), true)
    await page.locator('.tree .name[title="main.js"]').click()
    assert.equal(await page.locator('.layout > .center').isVisible(), true)
    await nav.getByRole('button', { name: 'AI 助手', exact: true }).click()
    assert.equal(await page.locator('.layout > .right').isVisible(), true)
    await nav.getByRole('button', { name: 'AI 助手', exact: true }).click()
    assert.equal(await page.locator('.layout > .right').isVisible(), false)
    assert.equal(await page.locator('.layout > .center').isVisible(), true)
    await page.keyboard.press('Control+Shift+F12')
    await page.waitForFunction(() => document.querySelector('.code')?.classList.contains('focus-mode'))
    assert.equal(await page.locator('.layout > .center').isVisible(), true)
    const focusedBounds = await page.locator('.layout > .center').boundingBox()
    assert.ok(focusedBounds.width > 250 && focusedBounds.height > 200, `mobile focus mode collapsed editor: ${JSON.stringify(focusedBounds)}`)
    await snapshot('ide-focus-390')
    await page.keyboard.press('Control+Shift+F12')
    await page.waitForFunction(() => !document.querySelector('.code')?.classList.contains('focus-mode'))
    assert.equal(await page.locator('.layout > .center').isVisible(), true)
    await nav.getByRole('button', { name: '项目文件', exact: true }).click()
    await page.locator('.tree .name[title="main.js"]').click()
    await page.keyboard.press('Control+Shift+P')
    const palette = page.getByRole('dialog', { name: '命令面板', exact: true })
    await palette.waitFor()
    const bounds = await palette.boundingBox()
    assert.ok(bounds.x >= -1 && bounds.x + bounds.width <= 391, `command palette exceeds narrow viewport: ${JSON.stringify(bounds)}`)
    await snapshot('ide-command-390')
    await page.keyboard.press('Escape')
    await palette.waitFor({ state: 'detached' })
    checks.push('390px activity rail selects file and AI panes, repeated AI toggle returns to editor, focus mode retains editor bounds and restores correctly')
    checks.push('390px command palette stays within viewport and closes with Escape')
  }
  assert.deepEqual(runtimeErrors, [])
  assert.deepEqual(consoleErrors, [])
  assert.deepEqual(unexpectedRequests, [])
  assert.deepEqual(forbiddenRequests, [])
  assert.deepEqual(externalRequests, [])
  assert.deepEqual(failedRequests.filter(value => !value.endsWith('net::ERR_ABORTED')), [])
  checks.push('all project, API, WebSocket and editor data remain isolated fixtures')
} catch (error) {
  failure = redact(error.stack || error.message || error)
  throw error
} finally {
  await fs.promises.writeFile(path.join(output, 'report.json'), JSON.stringify({ passed: !failure, isolatedApiAndWebSocketFixtures: true, failure, checks, screenshots, runtimeErrors, consoleErrors, unexpectedRequests, forbiddenRequests, externalRequests, failedRequests, interceptedApiRequests }, null, 2))
  await browser.close()
}
console.log(JSON.stringify({ passed: !failure, checks, output }, null, 2))
