import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'

// Real Vue/Monaco/Chrome, with all API and language sockets replaced by fixtures.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const base = process.argv[2] || 'http://127.0.0.1:5186', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/intellij-ide', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], screenshots = [], errors = [], unexpected = [], saves = []
const project = { id: 'language-fixture', name: 'Python 工作区', root_path: 'E:/language-fixture', available: true, git: { is_repo: false } }
const session = { id: 'language-session', code_project_id: project.id, permission_mode: 'workspace-write', model: 'fixture-model', status: 'open', web_enabled: false, busy: false }
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
      if (suffix === '/questions/pending') return reply({questions:[]})
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
        if (suffix === '/conversations') return reply({conversations:[session],default_session_id:session.id})
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
              textDocumentSync: { change: 1, save: { includeText: true } }, completionProvider: { triggerCharacters: ['.'], resolveProvider: true }, hoverProvider: true, definitionProvider: true, referencesProvider: true, implementationProvider:true, typeDefinitionProvider:true,
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
        if (frame.method === 'textDocument/hover') result = {
          contents:[{language:'python',value:'def greet(name: str) -> str:'},{kind:'markdown',value:'根据姓名生成问候内容。\n\n**业务信息：** 用户问候。\n\n**返回类型：** `str`\n\n识别依据：语言服务。\n\n[Python 参考文档](https://docs.python.org/3/library/stdtypes.html)'}],
          documentation_translation:{status:'ready',language:'zh-CN',source:'configured-model'},
          _code_agent_original:{contents:[{language:'python',value:'def greet(name: str) -> str:'},{kind:'markdown',value:'Returns a greeting for the supplied user name.'}]}
        }
        if (['textDocument/definition','textDocument/implementation','textDocument/typeDefinition'].includes(frame.method)) result=[{uri:`file:///${project.root_path}/helper.py`,range:range(1,0,5)}]
        if(frame.method==='textDocument/references') result=[{uri:`file:///${project.root_path}/main.py`,range:range(3,6,11)},{uri:`file:///${project.root_path}/helper.py`,range:range(1,6,11)}]
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
  await page.getByRole('button', { name: '整理格式', exact: true }).waitFor()
  await page.waitForFunction(() => window.__languageFixture.sockets.filter(s => s.isLsp && s.readyState === 1).length === 2)
  await page.evaluate(async () => {
    window.__ideMonaco = (await import('/src/lib/monacoSetup.js')).ensureMonaco()
    window.__ideEditor = () => window.__ideMonaco.editor.getEditors().find(editor => editor.getDomNode()?.closest('.pane'))
    window.__ideKeys = []
    window.addEventListener('keydown', event => { if (event.ctrlKey || event.altKey || /^F\d+$/.test(event.key)) setTimeout(()=>window.__ideKeys.push({key:event.key,prevented:event.defaultPrevented}),0) },true)
  })
  const cursor = async (lineNumber, column) => page.evaluate(({lineNumber,column}) => { const editor = window.__ideEditor(); editor.setPosition({lineNumber,column}); editor.revealLineInCenter(lineNumber); editor.focus() }, {lineNumber,column})
  const text = () => page.evaluate(() => window.__ideEditor().getValue())
  const restore = async value => page.evaluate(value => { const editor=window.__ideEditor(); editor.executeEdits('fixture-reset',[{range:editor.getModel().getFullModelRange(),text:value}]);editor.focus() }, value)
  const menu = async label => { await page.locator('.editor-command-menu').getByRole('button', {name:label,exact:true}).click(); return page.getByRole('menu', {name:label,exact:true}) }
  const preview = page.getByRole('dialog', { name: '代码修改预览', exact: true })
  const drawer = () => page.locator('.symbol-documentation')

  await append('Pa')
  await page.keyboard.press('Control+Space')
  await page.locator('.suggest-widget.visible').waitFor()
  await page.locator('.suggest-widget .monaco-list-row').filter({hasText:'Path'}).first().click()
  await page.waitForFunction(() => window.__ideEditor().getValue().includes('from pathlib import Path'))
  assert.match(await text(), /Path\(filename\)/)
  assert.match(await text(), /^from pathlib import Path\n/)
  checks.push('Ctrl+Space applies the language completion, snippet placeholder and additional import to the draft')
  await shot('01-completion-import')
  await page.keyboard.press('Escape')
  await restore(original)

  await cursor(4,13)
  await page.keyboard.press('Control+p')
  await page.locator('.parameter-hints-widget.visible').waitFor()
  assert.match(await page.locator('.parameter-hints-widget.visible').innerText(),/name: str/)
  assert.equal(await page.locator('.quick-open').count(),0)
  assert.ok((await page.evaluate(()=>window.__ideKeys)).some(row=>row.key==='p'&&row.prevented))
  checks.push('Ctrl+P opens parameter hints and the workbench does not also open global file search')
  await page.keyboard.press('Escape')
  await page.getByRole('button',{name:'切换文件侧栏',exact:true}).focus()
  await page.keyboard.press('Control+p')
  await page.locator('.quick-open').waitFor()
  await page.keyboard.press('Escape')
  await page.locator('.quick-open').waitFor({state:'detached'})
  checks.push('Outside editor focus Ctrl+P still opens the existing global project search')

  await treeOpen('helper.py'); await activate('main.py')
  await cursor(4,9)
  const sidebarBefore = await page.getByRole('button',{name:'切换文件侧栏',exact:true}).getAttribute('aria-pressed')
  await page.keyboard.press('Control+b')
  await active('helper.py')
  assert.equal(await page.getByRole('button',{name:'切换文件侧栏',exact:true}).getAttribute('aria-pressed'),sidebarBefore)
  assert.ok((await audit()).some(row=>row.method==='textDocument/definition'))
  checks.push('Ctrl+B follows the language-server definition to another file without toggling the project sidebar')
  await activate('main.py'); await cursor(1,7)
  await page.keyboard.press('Alt+F7')
  await page.waitForFunction(()=>window.__languageFixture.audit.some(row=>row.method==='textDocument/references'))
  await page.locator('.zone-widget .peekview-widget').first().waitFor()
  checks.push('Alt+F7 requests references and opens the real Monaco references view')
  await page.keyboard.press('Escape')

  await cursor(1,7)
  await page.keyboard.press('Shift+F6')
  const rename = page.locator('.rename-box input')
  await rename.waitFor(); await rename.fill('welcome'); await rename.press('Enter')
  await preview.waitFor()
  assert.equal(await preview.locator('.language-edit-files button').count(),2)
  assert.equal(saves.length,0)
  assert.equal(await text(),original)
  await shot('02-rename-preview')
  await preview.getByRole('button',{name:'应用到草稿',exact:true}).click()
  assert.equal(await text(),original.replaceAll('greet','welcome'))
  await activate('helper.py'); assert.equal(await text(),helper.replaceAll('greet','welcome'))
  await page.getByRole('button',{name:'撤销代码修改',exact:true}).click()
  assert.equal(await text(),helper)
  await activate('main.py'); assert.equal(await text(),original)
  checks.push('Shift+F6 previews both affected files, applies only to drafts, and one undo restores the entire rename')

  await cursor(1,7)
  await page.keyboard.press('Control+Alt+l')
  await page.waitForFunction(()=>window.__ideEditor().getValue().startsWith('# formatted\n'))
  assert.equal(saves.length,0)
  await page.keyboard.press('Control+z')
  assert.equal(await text(),original)
  checks.push('Ctrl+Alt+L formats through the language service and supports ordinary undo without saving')

  await cursor(1,7)
  let navigation = await menu('代码导航')
  await navigation.getByRole('menuitem',{name:'快速文档',exact:false}).click()
  await drawer().waitFor()
  await page.waitForFunction(()=>document.querySelector('.documentation-body')?.getAttribute('aria-busy')==='false')
  assert.match(await drawer().innerText(),/业务信息[\s\S]*用户问候/)
  assert.match(await drawer().innerText(),/返回类型/)
  assert.match(await drawer().innerText(),/识别依据/)
  assert.match(await drawer().innerText(),/文档中文翻译/)
  assert.doesNotMatch(await drawer().locator('.documentation-body').innerText(),/Returns a greeting/)
  await drawer().getByRole('button',{name:'原文',exact:true}).click()
  assert.match(await drawer().locator('.documentation-body').innerText(),/Returns a greeting/)
  await drawer().getByRole('button',{name:'中文说明',exact:true}).click()
  await drawer().getByRole('button',{name:'复制签名',exact:true}).click()
  assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'def greet(name: str) -> str:')
  await drawer().getByRole('button',{name:'固定说明',exact:true}).click()
  await activate('helper.py')
  assert.match(await drawer().innerText(),/main\.py:1/)
  assert.match(await drawer().innerText(),/用户问候/)
  checks.push('Quick documentation preserves Chinese purpose, business/type/evidence/translation, offers opt-in original, copies declaration and stays pinned across files')
  await shot('03-chinese-pinned-documentation')
  await drawer().getByRole('button',{name:'关闭完整说明'}).click()
  await cursor(2,9)
  await page.keyboard.press('Control+q')
  await drawer().waitFor()
  await page.waitForFunction(()=>document.querySelector('.documentation-body')?.getAttribute('aria-busy')==='false')
  assert.match(await drawer().innerText(),/helper\.py:2/)
  await page.keyboard.press('Escape')
  await drawer().waitFor({state:'detached'})
  checks.push('Ctrl+Q opens documentation for the current file and Escape closes the focused reading pane')

  await activate('main.py')
  await page.locator('.workbench-bar .workbench-action').filter({hasText:/^允许编辑$/}).click()
  const readonlyText=await text(), beforeRename=(await audit()).filter(row=>row.method==='textDocument/rename').length
  const refactor=await menu('重构')
  assert.equal(await refactor.getByRole('menuitem',{name:/重命名符号/}).isDisabled(),true)
  await page.keyboard.press('Escape')
  await cursor(1,7); await page.keyboard.press('Shift+F6')
  assert.equal((await audit()).filter(row=>row.method==='textDocument/rename').length,beforeRename)
  assert.equal(await text(),readonlyText)
  navigation=await menu('代码导航')
  assert.equal(await navigation.getByRole('menuitem',{name:/跳转到定义/}).isDisabled(),false)
  await navigation.getByRole('menuitem',{name:/跳转到定义/}).click(); await active('helper.py')
  checks.push('Read-only mode blocks rename both from menus and keyboard while preserving actual definition navigation')

  await page.locator('.workbench-bar .workbench-action').filter({hasText:/^编辑只读$/}).click()
  await page.setViewportSize({width:390,height:844})
  const trigger=page.locator('.editor-command-menu').getByRole('button',{name:'代码导航',exact:true})
  await trigger.focus(); await trigger.press('ArrowDown')
  navigation=page.getByRole('menu',{name:'代码导航',exact:true})
  await navigation.waitFor()
  const geometry=await navigation.boundingBox()
  assert.ok(geometry.x>=0&&geometry.x+geometry.width<=391)
  assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('role')),'menuitem')
  await page.keyboard.press('End')
  assert.equal(await page.evaluate(()=>document.activeElement?.disabled),false)
  await shot('04-mobile-keyboard-menu')
  await page.keyboard.press('Escape')
  assert.equal(await navigation.count(),0)
  assert.equal(await trigger.evaluate(el=>el===document.activeElement),true)
  checks.push('390px command menu stays within viewport, arrow/Home/End keys skip disabled items and Escape restores trigger focus')
  assert.equal(saves.length,0)
  assert.deepEqual(errors,[])
  assert.deepEqual(unexpected,[])
  checks.push('All tested IDE operations stay in isolated browser fixtures with no model calls, disk writes or browser errors')
} catch(error) { failure=error; if(page) await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{}) }
finally {
  const wsAudit=page?await page.evaluate(()=>window.__languageFixture?.audit||[]).catch(()=>[]):[]
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({checks,screenshots,errors,unexpected,saves:saves.map(row=>({path:row.path,chars:row.content.length})),wsAudit,failure:failure?.stack||null},null,2))
  await browser.close()
}
if(failure)throw failure
console.log(JSON.stringify({passed:checks.length,screenshots,output}))
