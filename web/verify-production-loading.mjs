import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { createServer } from 'node:http'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { inspectProductionBundle, assertDeferredEditor } from './verify-production-bundle.mjs'

// Production Vue/Monaco/Chrome, without exposing a real project or backend.
// node web/verify-production-loading.mjs [dist directory] [evidence directory]
const directory = path.resolve(process.argv[2] || fileURLToPath(new URL('./dist', import.meta.url)))
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/production-loading-browser', import.meta.url)))
await fs.mkdir(output, { recursive: true })
const bundle = await inspectProductionBundle(directory)
assertDeferredEditor(bundle)
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.ttf': 'font/ttf' }
const server = createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, 'http://fixture').pathname)
    const filename = path.resolve(directory, `.${pathname === '/' ? '/index.html' : pathname}`)
    const relative = path.relative(directory, filename)
    if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('Outside fixture directory')
    const content = await fs.readFile(filename)
    response.writeHead(200, { 'Content-Type': mime[path.extname(filename)] || 'application/octet-stream', 'Cache-Control': 'no-store' })
    response.end(content)
  } catch { if (!response.headersSent) response.writeHead(404); response.end('Not found') }
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const user = { id: 'lazy-editor-fixture-user', username: 'offline-fixture', role: 'admin' }
const project = { id: 'lazy-editor-fixture', name: '编辑器按需加载验收', root_path: 'E:/offline-fixture/lazy-editor', available: true, allow_direct_write: false, git: { is_repo: false } }
const session = { id: 'lazy-editor-session', code_project_id: project.id, model: 'fixture-model', permission_mode: 'read-only', status: 'open', busy: false, web_enabled: false }
const files = new Map([
  ['main.js', { text: 'export function greet(name) {\n  return `Hello ${name}`;\n}\n' }],
  ['helper.js', { text: 'export const helper = 42;\n' }],
])
const sha1 = value => createHash('sha1').update(value).digest('hex')
const fileData = filename => {
  const file = files.get(filename)
  return file ? { path: filename, name: filename, text: file.text, sha1: sha1(file.text), lang: 'javascript', editable: true, secret: false, uri: `file:///${project.root_path}/${filename}`, total_lines: file.text.split('\n').length } : null
}
const inventory = () => [...files].map(([name, file]) => ({ path: name, name, size: Buffer.byteLength(file.text) }))
const checks = [], errors = [], unexpected = [], forbidden = [], resources = [], calls = [], saves = [], screenshots = [], timings = {}
const editorResources = () => resources.filter(item => /\/(?:monaco(?:Setup)?-|(?:editor|json|css|html|ts)\.worker-)/.test(item.path))
let page, stage = 'launch', failure = null
const phase = value => { stage = value; console.log(`[production] ${stage}`) }
const watchdog = setTimeout(() => browser.close().catch(() => {}), 90_000)
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 1.5, serviceWorkers: 'block' })
  await context.addInitScript(({ userId, session }) => {
    // An explicit empty preference means no selected project, even with recent records.
    localStorage.setItem(`code-agent-active-project-v2:${userId}`, '')
    const NativeSocket = window.WebSocket
    window.fixtureSocketAudit = []
    window.WebSocket = class FixtureSocket extends EventTarget {
      static OPEN = 1; static CLOSED = 3; static CONNECTING = 0
      constructor(value, protocols) {
        super()
        const target = new URL(value, location.href)
        if (!target.pathname.startsWith('/api/')) return new NativeSocket(value, protocols)
        this.url = target.href; this.readyState = 0; this.bufferedAmount = 0
        const allowed = target.host === location.host && target.pathname === `/api/v1/sessions/${session.id}/stream`
        window.fixtureSocketAudit.push({ path: target.pathname, allowed })
        queueMicrotask(() => {
          if (!allowed || this.readyState === 3) { this.close(); return }
          this.readyState = 1; this.emit('open', new Event('open'))
          this.emit('message', new MessageEvent('message', { data: JSON.stringify({ type: 'session_meta', seq: 0, payload: session }) }))
        })
      }
      emit(type, event) { this.dispatchEvent(event); this[`on${type}`]?.(event) }
      send(text) {
        const frame = JSON.parse(text)
        if (frame.type === 'ping') queueMicrotask(() => this.emit('message', new MessageEvent('message', { data: JSON.stringify({ type: 'pong', payload: {} }) })))
        else window.fixtureSocketAudit.push({ path: new URL(this.url).pathname, allowed: false, type: frame.type })
      }
      close() { this.readyState = 3 }
    }
  }, { userId: user.id, session })
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname, method = request.method(), label = `${method} ${pathname}`
    if (url.origin !== origin) { forbidden.push(label); return route.abort() }
    if (!pathname.startsWith('/api/') && pathname !== '/healthz') return route.continue()
    calls.push({ method, path: pathname, query: url.search, stage })
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
    if (pathname === '/healthz') return reply({ ok: true })
    if (pathname === '/api/v1/auth/me') return reply(user)
    if (pathname === '/api/v1/web') return reply({ available: false, reason: 'Offline fixture' })
    if (pathname === '/api/v1/settings/model') return reply({ provider: 'offline', active_model: session.model, model_default: session.model })
    if (pathname === '/api/v1/model-profiles') return reply([])
    if (pathname === '/api/v1/code/meta') return reply({ templates: ['empty'], projects_root: '', native_picker: { available: false }, git_available: false })
    if (pathname === '/api/v1/code/projects' && method === 'GET') return reply([project])
    const prefix = `/api/v1/code/projects/${project.id}`
    if (pathname.startsWith(prefix)) {
      const suffix = pathname.slice(prefix.length)
      if (!suffix && method === 'GET') return reply(project)
      if (suffix === '/session' && method === 'POST') {
        assert.notEqual(request.postDataJSON()?.permission_mode, 'workspace-write')
        return reply(session)
      }
      if (suffix === '/file' && method === 'PUT') {
        const body = request.postDataJSON(), current = files.get(body.path)
        if (!current || body.expected_sha1 !== sha1(current.text)) return reply({ detail: 'Fixture file conflict' }, 409)
        saves.push(body); current.text = body.content
        return reply({ changed: true, path: body.path, sha1: sha1(current.text), lines: current.text.split('\n').length })
      }
      if (method === 'GET') {
        if (suffix === '/file') return reply(fileData(url.searchParams.get('path')))
        if (suffix === '/tree') return reply({ dirs: [], files: inventory(), truncated: false })
        if (suffix === '/files') return reply({ files: inventory(), truncated: false })
        if (suffix === '/lsp') return reply({ servers: [] })
        if (suffix === '/diagnostics') return reply({ seq: 1, counts: { error: 0, warning: 0 }, items: [], files: {}, symbols: {} })
        if (suffix === '/index') return reply({ change_seq: 1, scanning: false, briefs: { running: false } })
        if (suffix === '/map/delta') return reply({ to_seq: 1, changes: [], added: [], updated: [], removed: [] })
        if (suffix === '/feature-briefs') return reply({ items: [], missing: { total: 0 } })
        if (suffix === '/conversations') return reply({ conversations: [{ id: session.id, kind: 'main', title: '项目对话', busy: false }], default_session_id: session.id })
        if (suffix === '/agents') return reply({ lead: { session_id: session.id, role: 'lead', state: 'idle', depth: 0 }, agents: [], limits: { max_depth: 3, max_parallel: 2 }, running: 0, queued: 0 })
      }
    }
    const sessionPrefix = `/api/v1/sessions/${session.id}`
    if (pathname.startsWith(sessionPrefix) && method === 'GET') {
      const suffix = pathname.slice(sessionPrefix.length)
      if (!suffix) return reply(session)
      if (suffix === '/events') return reply([])
      if (suffix === '/checkpoints') return reply({ items: [], busy: false })
      if (suffix === '/approvals/pending' || suffix === '/questions/pending') return reply({})
    }
    if (method !== 'GET') forbidden.push(label)
    unexpected.push(label)
    return reply({ detail: 'Not covered by offline production fixture' }, 501)
  })
  page = await context.newPage()
  page.setDefaultTimeout(20_000)
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    const url = new URL(request.url())
    if (url.origin === origin && !url.pathname.startsWith('/api/') && url.pathname !== '/healthz') resources.push({ path: url.pathname, stage })
  })
  phase('empty workspace cold start')
  const started = Date.now()
  await page.goto(origin, { waitUntil: 'networkidle' })
  await page.getByRole('menubar', { name: 'IDE 菜单' }).waitFor()
  timings.emptyWorkspaceReadyMs = Date.now() - started
  assert.deepEqual(editorResources(), [], 'An empty workbench must not request Monaco, its CSS or workers')
  assert.equal(calls.some(call => call.path.startsWith(`/api/v1/code/projects/${project.id}/`)), false)
  const logo = await page.locator('.ide-brand .agent-logo__mark').evaluate(image => ({ src: new URL(image.src).pathname, loaded: image.complete && image.naturalWidth > 0, width: image.width, height: image.height }))
  assert.equal(logo.src, '/brand/code-agent-mark.svg'); assert.equal(logo.loaded, true); assert.equal(logo.width, logo.height)
  assert.equal(await page.locator('link[rel="icon"]').getAttribute('type'), 'image/svg+xml')
  assert.ok(resources.some(resource => resource.path.endsWith('.svg')))
  assert.equal(resources.some(resource => resource.path.endsWith('code-agent-mark.png')), false)
  checks.push('Production entry/workbench static dependency closures and an empty workspace request zero Monaco/editor assets')
  checks.push('The real titlebar and favicon use the same small, decoded vector mark with explicit square dimensions')
  await page.screenshot({ path: path.join(output, '01-empty-workspace.png') }); screenshots.push('01-empty-workspace.png')

  phase('open a workspace without opening a file')
  await page.getByRole('button', { name: '切换项目', exact: true }).click()
  await page.getByRole('button', { name: `切换到 ${project.name}`, exact: true }).click()
  await page.getByRole('button', { name: '文件 main.js', exact: true }).waitFor()
  await page.waitForLoadState('networkidle')
  assert.deepEqual(editorResources(), [], 'Selecting a project alone must not load the editor')
  checks.push('Project tree/session initialization without an open file still makes zero Monaco requests')

  phase('first file mounts the real editor and accepts input/save')
  const firstFileStart = Date.now()
  await page.getByRole('button', { name: '文件 main.js', exact: true }).click()
  const editor = page.locator('.pane .monaco-editor').first(), input = editor.getByRole('textbox')
  await input.waitFor({ state: 'attached' })
  await editor.locator('.view-line').first().waitFor()
  timings.firstFileReadyMs = Date.now() - firstFileStart
  assert.ok(editorResources().some(resource => /\/monaco-[^/]+\.js$/.test(resource.path)))
  assert.ok(editorResources().some(resource => /\/monaco-[^/]+\.css$/.test(resource.path)))
  const original = files.get('main.js').text, edit = '// production lazy editor verified\n'
  await input.focus(); await page.keyboard.press('Control+End'); await page.keyboard.insertText(edit)
  await page.locator('.editor-tab.active .dirty-dot').waitFor()
  await page.keyboard.press('Control+s')
  await page.waitForFunction(() => !document.querySelector('.editor-tab.active .dirty-dot'))
  assert.equal(saves.length, 1); assert.equal(saves[0].path, 'main.js')
  assert.equal(saves[0].expected_sha1, sha1(original))
  assert.equal(files.get('main.js').text, original + edit)
  assert.match(await page.locator('.workspace-notice-bar').innerText(), /已保存 main\.js/)
  checks.push('First file dynamically loads Monaco and local styles; real keyboard input and Ctrl+S save exact edited bytes with the expected original hash')
  await page.screenshot({ path: path.join(output, '02-first-file-saved.png') }); screenshots.push('02-first-file-saved.png')

  phase('real file navigation after lazy editor initialization')
  await page.getByRole('button', { name: '文件 helper.js', exact: true }).click()
  await page.getByRole('tab', { name: 'helper.js', exact: true }).filter({ has: page.locator('.tab-label') }).waitFor()
  await page.waitForFunction(() => document.querySelector('.editor-tab.active .tab-label')?.textContent === 'helper.js')
  await page.getByRole('button', { name: '导航后退', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.editor-tab.active .tab-label')?.textContent === 'main.js')
  assert.match(await editor.innerText(), /production\s+lazy\s+editor\s+verified/)
  await page.getByRole('button', { name: '导航前进', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.editor-tab.active .tab-label')?.textContent === 'helper.js')
  assert.equal(editorResources().filter(resource => /\/monaco-[^/]+\.js$/.test(resource.path)).length, 1)
  assert.equal(saves.length, 1)
  checks.push('Opening another file and navigating back/forward retains saved contents without re-downloading Monaco or extra file writes')
  assert.deepEqual(await page.evaluate(() => window.fixtureSocketAudit.filter(item => !item.allowed)), [])
  assert.deepEqual(unexpected, []); assert.deepEqual(forbidden, []); assert.deepEqual(errors, [])
  checks.push('All API/WebSocket traffic is isolated; zero real project, model, shell or service operations and zero browser runtime errors')
} catch (error) {
  failure = error.stack || String(error)
  if (page) try { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }); screenshots.push('failure.png') } catch {}
} finally {
  clearTimeout(watchdog)
  await browser.close()
  await new Promise(resolve => server.close(resolve))
  const report = { passed: !failure, stage, bundle, checks, timings, resources, calls, saves, errors, unexpected, forbidden, screenshots, failure }
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
  if (failure) process.exitCode = 1
}
