import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'

// Real Vue/Monaco/Chrome, with all API and language sockets replaced by fixtures.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/ide-language-ui', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], screenshots = [], errors = [], unexpected = [], saves = []
const project = { id: 'language-fixture', name: 'Python 工作区', root_path: 'E:/language-fixture', available: true, git: { is_repo: false } }
const session = { id: 'language-session', code_project_id: project.id, permission_mode: 'read-only', model: 'fixture-model', status: 'open', web_enabled: false, busy: false }
const original = 'def greet(name):\n    return name\n\nprint(greet("World"))\n'
const helper = 'from main import greet\nprint(greet("Helper"))\n'
const disks = new Map([['main.py', { text: original }], ['helper.py', { text: helper }]])
const hash = text => createHash('sha1').update(text).digest('hex')
for (const file of disks.values()) file.sha1 = hash(file.text)
const inventory = () => [...disks].map(([filename, file]) => ({ path: filename, name: filename, size: file.text.length }))
const file = filename => disks.has(filename) ? { ...disks.get(filename), path: filename, lang: 'python', uri: `file:///${project.root_path}/${filename}`, editable: true, secret: false, start_line: 1, end_line: disks.get(filename).text.split('\n').length, total_lines: disks.get(filename).text.split('\n').length } : null
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page, failure
try {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 }, serviceWorkers: 'block', permissions: ['clipboard-read', 'clipboard-write'] })
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname, method = request.method()
    if (url.origin !== origin) { unexpected.push(`${method} external`); return route.abort() }
    if (!pathname.startsWith('/api/') && pathname !== '/healthz') return route.continue()
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
    if (pathname === '/healthz') return reply({ ok: true })
    if (pathname === '/api/v1/auth/me') return reply({ id: 'language-user', username: 'fixture', role: 'admin' })
    if (pathname === '/api/v1/web') return reply({ available: false, reason: 'Fixture only' })
    if (pathname === '/api/v1/code/meta') return reply({ templates: ['empty'], browse_enabled: false, features: {}, projects_root: '', git_available: false })
    if (pathname === '/api/v1/code/projects' && method === 'GET') return reply([project])
    if (pathname.startsWith(`/api/v1/sessions/${session.id}`) && method === 'GET') {
      const suffix = pathname.slice(`/api/v1/sessions/${session.id}`.length)
      if (!suffix) return reply(session)
      if (suffix === '/events') return reply([])
      if (suffix === '/checkpoints') return reply({ items: [], busy: false })
      if (suffix === '/approvals/pending') return reply({})
    }
    const prefix = `/api/v1/code/projects/${project.id}`
    if (pathname.startsWith(prefix)) {
      const suffix = pathname.slice(prefix.length)
      if (!suffix && method === 'GET') return reply(project)
      if (suffix === '/session' && method === 'POST') return reply(session)
      if (suffix === '/file' && method === 'GET') return reply(file(url.searchParams.get('path')))
      if (suffix === '/file' && method === 'PUT') {
        const body = request.postDataJSON(), target = disks.get(body.path)
        saves.push(body)
        if (body.expected_sha1 !== target.sha1) return reply({ detail: 'Fixture conflict' }, 409)
        target.text = body.content; target.sha1 = hash(target.text)
        return reply({ changed: true, sha1: target.sha1, lines: target.text.split('\n').length, version: saves.length })
      }
      if (method === 'GET') {
        if (suffix === '/agents') return reply({ lead: { session_id: session.id, project_id: project.id, role: 'lead', depth: 0, title: 'Lead', state: 'idle' }, agents: [], limits: { max_depth: 3, max_parallel: 4 }, running: 0, queued: 0 })
        if (suffix === '/tree') return reply({ dirs: [], files: inventory(), truncated: false })
        if (suffix === '/files') return reply({ files: inventory(), truncated: false })
        if (suffix === '/lsp') return reply({ servers: [
          { id: 'pyright', name: 'Pyright', languages: ['python'], installed: true, enabled: true, state: 'installed' },
          { id: 'ruff', name: 'Ruff', languages: ['python'], installed: true, enabled: true, state: 'installed' },
        ], environment: { python: 'E:/language-fixture/.venv/Scripts/python.exe', interpreter_source: 'project-venv' } })
        if (suffix === '/diagnostics') return reply({ seq: 1, counts: { error: 0, warning: 0 }, items: [], files: {}, symbols: {} })
        if (suffix === '/index') return reply({ change_seq: 1, scanning: false })
        if (suffix === '/map/delta') return reply({ to_seq: 1, changes: [] })
        if (suffix === '/patches') return reply({ patches: [], counts: { total: 0, active: 0 } })
        if (suffix === '/tasks' || suffix === '/batches') return reply([])
        if (suffix === '/git') return reply({ is_repo: false, changes: [] })
        if (suffix === '/architecture') return reply({ rules: {}, errors: [] })
      }
    }
    unexpected.push(`${method} ${pathname}`)
    return reply({ detail: 'Fixture rejects unrecognized API' }, 501)
  })
  await context.addInitScript(({ project, session }) => {
    localStorage.setItem('code-agent:nav-collapsed', 'true')
    localStorage.setItem('code-agent-code-left', '240')
    localStorage.setItem('code-agent-code-right', '310')
    const NativeSocket = window.WebSocket
    const state = window.__languageFixture = { sockets: [], audit: [] }
    const range = (line, start, end) => ({ start: { line, character: start }, end: { line, character: end } })
    class FixtureSocket extends EventTarget {
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
      constructor(url, protocols) {
        super()
        const target = new URL(url, location.href)
        if (!target.pathname.startsWith('/api/')) return new NativeSocket(url, protocols)
        this.url = target.href; this.readyState = 0; this.protocol = ''; this.extensions = ''; this.bufferedAmount = 0
        this.server = target.searchParams.get('server') || 'pyright'; this.docs = new Map()
        this.isLsp = target.pathname.endsWith('/lsp/stream'); state.sockets.push(this)
        state.audit.push({ kind: 'connect', lsp: this.isLsp, server: this.server })
        queueMicrotask(() => {
          this.readyState = 1; this.emit('open', new Event('open'))
          if (this.isLsp) this.message({ method: 'code-agent/ready', params: {
            server: this.server, environment: { python: 'E:/language-fixture/.venv/Scripts/python.exe', interpreter_source: 'project-venv' }, diagnostics: {}, diagnostic_versions: {},
            capabilities: this.server === 'ruff' ? { textDocumentSync: { change: 1, save: { includeText: true } }, documentFormattingProvider: true, codeActionProvider: true } : {
              textDocumentSync: { change: 1, save: { includeText: true } }, completionProvider: { triggerCharacters: ['.'], resolveProvider: true }, hoverProvider: true, definitionProvider: true, referencesProvider: true,
              signatureHelpProvider: { triggerCharacters: ['(', ','] }, documentSymbolProvider: true, workspaceSymbolProvider: true, renameProvider: { prepareProvider: true }, codeActionProvider: true,
            },
          } })
          else if (target.pathname === `/api/v1/sessions/${session.id}/stream`) this.message({ type: 'session_meta', seq: 0, payload: session })
          else { state.audit.push({ kind: 'blocked', pathname: target.pathname }); this.close() }
        })
      }
      emit(type, event) { this.dispatchEvent(event); this[`on${type}`]?.(event) }
      message(frame) { if (this.readyState === 1) this.emit('message', new MessageEvent('message', { data: JSON.stringify(frame) })) }
      send(raw) {
        const frame = JSON.parse(raw)
        if (!this.isLsp) { if (frame.type === 'ping') this.message({ type: 'pong', seq: 0, payload: {} }); else state.audit.push({ kind: 'blocked-frame' }); return }
        state.audit.push({ kind: 'frame', server: this.server, method: frame.method, params: frame.params })
        const uri = frame.params?.textDocument?.uri
        if (frame.method === 'textDocument/didOpen') this.docs.set(uri, { ...frame.params.textDocument })
        if (frame.method === 'textDocument/didChange') { const doc = this.docs.get(uri); if (doc) { doc.text = frame.params.contentChanges[0].text; doc.version = frame.params.textDocument.version } }
        if (frame.method === 'textDocument/didClose') this.docs.delete(uri)
        if (frame.id == null) return
        let result = null
        const text = this.docs.get(uri)?.text || ''
        if (frame.method === 'textDocument/completion') {
          const position = frame.params.position
          result = { isIncomplete: false, items: [{ label: 'Path', kind: 7, sortText: '000', filterText: 'Path', insertTextFormat: 2,
            textEdit: { range: range(position.line, Math.max(0, position.character - 2), position.character), newText: 'Path(${1:filename})' },
            additionalTextEdits: [{ range: range(0, 0, 0), newText: 'from pathlib import Path\n' }], detail: 'pathlib.Path · 自动导入' }] }
        }
        if (frame.method === 'completionItem/resolve') result = { ...frame.params, documentation: { kind: 'markdown', value: 'Fixture path completion' } }
        if (frame.method === 'textDocument/signatureHelp') result = { activeSignature: 0, activeParameter: 0, signatures: [{ label: 'greet(name: str)', parameters: [{ label: 'name: str' }] }] }
        if (frame.method === 'textDocument/formatting') result = [{ range: range(0, 0, 0), newText: '# formatted\n' }]
        if (frame.method === 'textDocument/codeAction') result = [{ title: '添加文件说明', kind: 'quickfix', edit: { changes: { [uri]: [{ range: range(0, 0, 0), newText: '# fixed\n' }] } } }]
        if (frame.method === 'textDocument/documentSymbol') result = [{ name: 'greet', kind: 12, range: range(0, 0, 16), selectionRange: range(0, 4, 9), children: [] }]
        if (frame.method === 'workspace/symbol') result = [{ name: 'helper_entry', kind: 12, location: { uri: `file:///${project.root_path}/helper.py`, range: range(1, 0, 5) } }]
        if (frame.method === 'textDocument/prepareRename') {
          const p = frame.params.position, line = text.split('\n')[p.line] || '', match = [...line.matchAll(/\bgreet\b/g)].find(m => p.character >= m.index && p.character <= m.index + 5)
          result = { range: range(p.line, match?.index ?? 4, (match?.index ?? 4) + 5), placeholder: 'greet' }
        }
        if (frame.method === 'textDocument/rename') {
          const changes = {}
          for (const [docUri, doc] of this.docs) {
            const edits = []
            doc.text.split('\n').forEach((line, index) => { for (const m of line.matchAll(/\bgreet\b/g)) edits.push({ range: range(index, m.index, m.index + 5), newText: frame.params.newName }) })
            if (edits.length) changes[docUri] = edits
          }
          result = { changes }
        }
        queueMicrotask(() => this.message({ jsonrpc: '2.0', id: frame.id, result }))
      }
      close(code = 1000, reason = '') {
        if (this.readyState === 3) return
        this.readyState = 3; state.audit.push({ kind: 'close', lsp: this.isLsp, server: this.server })
        queueMicrotask(() => this.emit('close', new CloseEvent('close', { code, reason, wasClean: true })))
      }
    }
    window.WebSocket = FixtureSocket
  }, { project, session })
  page = await context.newPage(); page.setDefaultTimeout(15000)
  page.on('pageerror', error => errors.push(error.stack || error.message))
  page.on('dialog', dialog => { unexpected.push(`native-dialog ${dialog.type()}`); dialog.dismiss() })
  const editor = () => page.locator('.pane .monaco-editor').filter({ visible: true })
  const input = () => editor().locator('[role="textbox"]')
  const tab = filename => page.locator('.editor-tabs .tab-select').filter({ has: page.locator(`.tab-label:text-is("${filename}")`) })
  const active = async filename => { await page.locator('.pane .bar .path').filter({ hasText: filename }).waitFor(); await input().waitFor({ state: 'attached' }); await page.waitForFunction(() => !!document.querySelector('.pane .view-lines .view-line')) }
  const treeOpen = async filename => { await page.locator(`.tree .name[title="${filename}"]`).click(); await active(filename) }
  const activate = async filename => { await tab(filename).click(); await active(filename) }
  const read = async () => { await input().focus(); await page.keyboard.press('Control+A'); await page.keyboard.press('Control+C'); return (await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n') }
  const append = async text => { await input().focus(); await page.keyboard.press('Control+End'); await page.keyboard.insertText(text) }
  const audit = () => page.evaluate(() => window.__languageFixture.audit)
  const shot = async name => { await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true }); screenshots.push(`${name}.png`) }
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.locator('.code .workbench-bar').waitFor()
  await treeOpen('main.py')
  await page.getByRole('button', { name: '格式化', exact: true }).waitFor()
  await page.waitForFunction(() => window.__languageFixture.sockets.filter(s => s.isLsp && s.readyState === 1).length === 2)
  checks.push('Python primary analyzer and installed Ruff share the workspace environment')
  await append('# unsaved\n')
  await treeOpen('helper.py'); await activate('main.py')
  assert.equal(await read(), original + '# unsaved\n')
  const initialAudit = await audit()
  assert.equal(initialAudit.filter(r => r.kind === 'connect' && r.lsp).length, 2)
  assert.equal(initialAudit.filter(r => r.method === 'textDocument/didClose').length, 0)
  await page.waitForFunction(() => window.__languageFixture.sockets.filter(s => s.isLsp).every(s => s.docs.size === 2 && [...s.docs.values()].find(d => d.uri.endsWith('/main.py')).text.includes('# unsaved')))
  checks.push('tab switches retain both language channels and synchronize background drafts')
  const publish = (server, severity, stale = false, clear = false) => page.evaluate(({ server, severity, stale, clear }) => {
    const socket = window.__languageFixture.sockets.find(s => s.isLsp && s.server === server && s.readyState === 1)
    const doc = [...socket.docs.values()].find(d => d.uri.endsWith('/main.py'))
    socket.message({ method: 'textDocument/publishDiagnostics', params: { uri: doc.uri, version: doc.version - (stale ? 1 : 0), diagnostics: clear ? [] : [{ severity, message: stale ? 'stale fixture' : `${server} fixture`, source: server, range: { start: { line: 0, character: 4 }, end: { line: 0, character: 9 } } }] } })
  }, { server, severity, stale, clear })
  await publish('pyright', 1, true)
  assert.equal(await editor().locator('.squiggly-error').count(), 0)
  await publish('pyright', 1); await editor().locator('.squiggly-error').first().waitFor()
  await publish('ruff', 2); await editor().locator('.squiggly-warning').first().waitFor()
  assert.ok(await editor().locator('.squiggly-error').count())
  await publish('pyright', 1, false, true)
  await editor().locator('.squiggly-error').first().waitFor({ state: 'detached' })
  assert.ok(await editor().locator('.squiggly-warning').count())
  await publish('ruff', 2, false, true)
  checks.push('old diagnostics are dropped and primary/Ruff markers coexist and clear independently')
  await page.getByRole('button', { name: '格式化', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.pane .view-lines')?.textContent.replace(/\u00a0/g, ' ').includes('# formatted'))
  assert.equal(await read(), '# formatted\n' + original + '# unsaved\n')
  await input().focus(); await page.keyboard.press('Control+Z')
  assert.equal(await read(), original + '# unsaved\n')
  checks.push('Ruff formatting applies to the current draft in one undo step without saving')
  await append('Pa')
  await page.keyboard.press('Control+Space')
  await page.locator('.suggest-widget.visible').waitFor()
  await page.locator('.suggest-widget .monaco-list-row').filter({ hasText: 'Path' }).first().click()
  await page.waitForFunction(() => document.querySelector('.pane .view-lines')?.textContent.replace(/\u00a0/g, ' ').includes('from pathlib import Path'))
  assert.ok((await read()).includes('Path(filename)'))
  checks.push('completion applies replacement, snippet placeholders and additional auto-import edits')
  await shot('01-completion-import')
  await page.keyboard.press('Escape')
  // Restore draft through ordinary editor input before rename fixture.
  await page.evaluate(text => navigator.clipboard.writeText(text), original + '# unsaved\n')
  await input().focus(); await page.keyboard.press('Control+A'); await page.keyboard.press('Control+V')
  assert.equal(await read(), original + '# unsaved\n')
  await append('greet'); await page.keyboard.insertText('(')
  await editor().locator('.parameter-hints-widget.visible').waitFor()
  assert.match(await editor().locator('.parameter-hints-widget').innerText(), /name: str/)
  checks.push('typing a call opens the language-server parameter hint')
  await page.keyboard.press('Escape')
  await page.evaluate(text => navigator.clipboard.writeText(text), original + '# unsaved\n')
  await input().focus(); await page.keyboard.press('Control+A'); await page.keyboard.press('Control+V')
  await input().focus(); await page.keyboard.press('Control+Home'); await page.keyboard.press('ArrowDown'); await page.keyboard.press('End')
  await page.getByRole('button', { name: '修复', exact: true }).click()
  await page.getByText('添加文件说明', { exact: true }).first().waitFor()
  // Monaco intentionally ignores the opening click until the pointer moves.
  await page.mouse.move(780, 300)
  await page.getByText('添加文件说明', { exact: true }).first().click()
  const preview = page.getByRole('dialog', { name: '代码修改预览', exact: true })
  await preview.waitFor()
  assert.equal(saves.length, 0)
  await shot('02-quick-fix-preview')
  await preview.getByRole('button', { name: '应用到草稿', exact: true }).click()
  assert.equal(await read(), '# fixed\n' + original + '# unsaved\n')
  await page.getByRole('button', { name: '撤销代码修改', exact: true }).click()
  assert.equal(await read(), original + '# unsaved\n')
  checks.push('quick fix requires a review and supports batch undo without disk writes')
  await input().focus(); await page.keyboard.press('Control+Home')
  for (let i = 0; i < 6; i++) await page.keyboard.press('ArrowRight')
  await page.getByRole('button', { name: '重命名', exact: true }).click()
  const renameInput = page.locator('.rename-box input')
  await renameInput.waitFor(); await renameInput.fill('welcome'); await renameInput.press('Enter')
  await preview.waitFor()
  assert.equal(await preview.locator('.language-edit-files button').count(), 2)
  assert.ok(await preview.evaluate(node => node.getBoundingClientRect().width > 800))
  await shot('03-cross-file-rename')
  await page.setViewportSize({ width: 390, height: 844 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2))
  assert.equal(await preview.locator('.conflict-compare').evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length), 1)
  await shot('05-mobile-edit-preview')
  await page.setViewportSize({ width: 1600, height: 1050 })
  checks.push('edit preview stacks comparisons and remains bounded on a phone viewport')
  await preview.getByRole('button', { name: '应用到草稿', exact: true }).click()
  assert.equal(await read(), (original + '# unsaved\n').replaceAll('greet', 'welcome'))
  await activate('helper.py'); assert.equal(await read(), helper.replaceAll('greet', 'welcome'))
  assert.equal(saves.length, 0)
  await page.getByRole('button', { name: '撤销代码修改', exact: true }).click()
  assert.equal(await read(), helper)
  await activate('main.py'); assert.equal(await read(), original + '# unsaved\n')
  checks.push('cross-file rename reviews both drafts and restores the entire batch')
  await page.getByRole('button', { name: '符号', exact: true }).click()
  await page.locator('.language-symbol-item').filter({ hasText: 'greet' }).click()
  checks.push('file symbol navigation uses language-server locations')
  await page.getByRole('button', { name: '符号', exact: true }).click()
  await page.getByRole('button', { name: '工作区符号', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索符号', exact: true }).fill('helper')
  await page.locator('.language-symbol-panel').getByRole('button', { name: '搜索', exact: true }).click()
  await page.locator('.language-symbol-item').filter({ hasText: 'helper_entry' }).click()
  await active('helper.py')
  await page.waitForFunction(() => document.querySelector('.pane')?.dataset.highlightLine === '2')
  await activate('main.py')
  checks.push('workspace symbol search opens another file at its exact line')
  await page.locator('.workbench-bar .workbench-action').filter({ hasText: /^允许编辑$/ }).click()
  for (const name of ['格式化', '修复', '重命名']) assert.equal(await page.getByRole('button', { name, exact: true }).isDisabled(), true)
  assert.equal(await page.getByRole('button', { name: '符号', exact: true }).isDisabled(), false)
  await page.locator('.workbench-bar .workbench-action').filter({ hasText: /^编辑只读$/ }).click()
  checks.push('manual readonly disables modifying tools while retaining symbol navigation')
  // Reconnection must replay exact draft versions, without losing background documents.
  await page.evaluate(() => window.__languageFixture.sockets.find(s => s.isLsp && s.server === 'pyright' && s.readyState === 1).close(1006, 'fixture reconnect'))
  await page.waitForFunction(() => window.__languageFixture.sockets.filter(s => s.isLsp && s.server === 'pyright' && s.readyState === 1).some(s => s.docs.size === 2 && [...s.docs.values()].find(d => d.uri.endsWith('/main.py')).text.includes('# unsaved')))
  checks.push('language reconnect replays both current draft snapshots')
  await input().focus(); await page.keyboard.press('Control+S')
  await page.waitForFunction(() => window.__languageFixture.audit.filter(r => r.method === 'textDocument/didSave').length === 2)
  assert.equal(saves.length, 1)
  assert.equal(saves[0].content, original + '# unsaved\n')
  checks.push('didSave is sent to both analyzers after the explicit save succeeds')
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
  checks.push('no unhandled browser errors, model calls, native dialogs or real project mutations')
  await shot('04-language-workspace')
} catch (error) { failure = error; if (page) { try { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }) } catch {} } }
finally {
  const wsAudit = page ? await page.evaluate(() => window.__languageFixture?.audit || []).catch(() => []) : []
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ checks, screenshots, errors, unexpected, saves: saves.map(s => ({ path: s.path, chars: s.content.length })), wsAudit, failure: failure?.stack || null }, null, 2))
  await browser.close()
}
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, screenshots, output }))
