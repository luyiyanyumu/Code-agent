import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Actual CodeView, Monaco, FileTree and ChangesPanel. Every API and WebSocket
// is an in-memory fixture; no real backend/project/model can be reached.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const origin = new URL(process.argv[2] || 'http://127.0.0.1:5175').origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/workspace-refactor-browser', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], errors = [], unexpected = [], requests = [], screenshots = []
const pid = 'resource-refactor-fixture', root = 'E:/resource-refactor-fixture', sessionId = 'refactor-lead-fixture'
const originals = { 'Old.java': 'package fixture;\npublic class Old {}\n', 'Caller.java': 'package fixture;\nclass Caller { Old item; }\n', 'Obsolete.java': 'package fixture;\nclass Obsolete {}\n' }
const disk = new Map(Object.entries(originals)), batches = new Map(), decisions = []
const hash = text => crypto.createHash('sha1').update(text).digest('hex')
const file = rel => ({ path: rel, text: disk.get(rel), sha1: hash(disk.get(rel)), lang: 'java', editable: true, total_lines: disk.get(rel).split('\n').length, start_line: 1, uri: `file:///${root}/${rel}`, size: disk.get(rel).length })
const project = { id: pid, name: 'Resource refactor fixture', root_path: root, available: true, languages: ['java'], git: { branch: 'fixture', is_repo: true }, settings: { allow_direct_write: false } }
const server = { id: 'jdtls', name: 'Fixture Java', languages: ['java'], state: 'installed', installed: true, enabled: true, detected: true }
const batchView = batch => ({ id: batch.id, ts: '2026-10-10T10:00:00Z', title: batch.title, state: batch.state, source: 'lsp', paths: batch.files.map(item => item.path), file_count: batch.files.length, files: batch.files.map(item => ({ ...item, kind: !item.before_exists ? 'create' : !item.after_exists ? 'delete' : 'edit', hunks: item.before_text === item.after_text ? [] : [{ id: 'h1', lines: [...item.before_text.split('\n').filter(Boolean).map((text, index) => ({ kind: 'remove', old_line: index + 1, new_line: null, text: text + '\n' })), ...item.after_text.split('\n').filter(Boolean).map((text, index) => ({ kind: 'add', old_line: null, new_line: index + 1, text: text + '\n' }))] }] })) })
const component = await (await fetch(`${origin}/src/views/CodeView.vue`)).text()
const vueModule = component.match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vueModule && new URL(vueModule, origin).origin === origin, 'Cannot locate Vite Vue runtime')
const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body{margin:0;height:100%;min-width:0;}#app{height:100%;display:flex;min-width:0;}</style></head><body><div id="app"></div><script type="module">
import {createApp,h,ref,nextTick} from '${vueModule}';
import CodeView from '/src/views/CodeView.vue';
import {ensureMonaco} from '/src/lib/monacoSetup.js';
import '/src/style.css';
const state=window.__refactorFixture={frames:[],sockets:[],nextTick,monaco:ensureMonaco(),socketErrors:[]};
class FixtureSocket extends EventTarget {static CONNECTING=0;static OPEN=1;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;this.isLsp=new URL(url,location.href).pathname.endsWith('/lsp/stream');state.sockets.push(this);queueMicrotask(()=>{this.readyState=1;this.emit('open',new Event('open'));if(this.isLsp)this.message({method:'code-agent/ready',params:{server:'jdtls',initialization_ms:100,capabilities:{textDocumentSync:{change:2},hoverProvider:true,codeActionProvider:true,renameProvider:true}}})})}
 emit(type,event){this.dispatchEvent(event);this['on'+type]?.(event)}
 message(frame){if(this.readyState===1)this.emit('message',new MessageEvent('message',{data:JSON.stringify(frame)}))}
 send(raw){const frame=JSON.parse(raw);state.frames.push(frame);if(frame.id!==undefined)queueMicrotask(()=>this.message({id:frame.id,result:frame.method==='textDocument/codeAction'?[]:null}));if(!this.isLsp&&frame.type==='prompt')state.socketErrors.push('Unexpected model prompt')}
 close(){this.readyState=3;this.emit('close',new CloseEvent('close'))}
}
window.WebSocket=FixtureSocket;
createApp({setup(){return()=>h(CodeView,{user:{id:'refactor-fixture-user'},visible:true})}}).mount('#app');
Object.defineProperty(state,'editor',{get(){return state.monaco.editor.getEditors()[0]}});
state.emitEdit=payload=>{const pane=document.querySelector('.pane')?.__vueParentComponent;if(!pane)throw new Error('EditorPane is unavailable');pane.emit('language-edit',payload)};
</script></body></html>`
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page, failure
try {
  const context = await browser.newContext({ viewport: { width: 1460, height: 960 }, serviceWorkers: 'block' })
  await context.route('**/*', async route => {
    const url = new URL(route.request().url()), method = route.request().method(), pathname = url.pathname
    if (url.origin !== origin) { unexpected.push(url.href); return route.abort() }
    if (pathname === '/__workspace-refactor-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    if (!pathname.startsWith('/api/')) return route.continue()
    requests.push({ path: pathname, method })
    const respond = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    const base = `/api/v1/code/projects/${pid}`
    if (pathname === '/api/v1/code/projects' && method === 'GET') return respond({ projects: [project] })
    if (pathname === '/api/v1/code/meta' && method === 'GET') return respond({ git_available: true, native_picker: { available: false }, features: {} })
    if (pathname === base && method === 'GET') return respond(project)
    if (pathname === base + '/session' && method === 'POST') return respond({ id: sessionId, permission_mode: 'read-only' })
    if (pathname === base + '/agents' && method === 'GET') return respond({ lead: { session_id: sessionId, role: 'lead', state: 'idle', name: 'Lead' }, agents: [], limits: { max_depth: 3, max_parallel: 4 } })
    if (pathname === base + '/lsp' && method === 'GET') return respond({ servers: [server], sessions: [], failures: {} })
    if (pathname === base + '/diagnostics' && method === 'GET') return respond({ seq: 0, counts: { error: 0, warning: 0 }, files: [], items: [] })
    if (pathname === base + '/tree' && method === 'GET') return respond({ dirs: [], files: [...disk.keys()].map(rel => ({ path: rel, name: rel, size: disk.get(rel).length, lang: 'java' })) })
    if (pathname === base + '/file' && method === 'GET') { const rel = url.searchParams.get('path'); return disk.has(rel) ? respond(file(rel)) : respond({ detail: '文件不存在' }, 404) }
    if (pathname === base + '/index' && method === 'GET') return respond({ index_refresh: { pending: false, state: 'done', requested_generation: decisions.length, completed_generation: decisions.length } })
    if (pathname === base + '/change-proposals' && method === 'GET') return respond({ proposals: [] })
    if (pathname === base + '/change-batches' && method === 'GET') return respond({ batches: [...batches.values()].map(batchView) })
    if (pathname === base + '/change-batches' && method === 'POST') {
      const body = route.request().postDataJSON()
      const batch = { id: 'fixture-batch-' + (batches.size + 1), title: body.title, state: 'pending', files: body.files }
      for (const item of body.files) if (disk.has(item.path) !== item.before_exists || (disk.get(item.path) || '') !== item.before_text) return respond({ detail: '创建提案时磁盘已变化' }, 409)
      batches.set(batch.id, batch); return respond(batchView(batch))
    }
    const match = pathname.match(new RegExp('^' + base + '/change-batches/([^/]+)(?:/(apply|rollback|reject))?$'))
    if (match) {
      const batch = batches.get(match[1]); if (!batch) return respond({ detail: '批次不存在' }, 404)
      if (method === 'GET' && !match[2]) return respond(batchView(batch))
      if (method === 'POST' && match[2]) {
        const action = match[2], forward = action !== 'rollback'
        decisions.push({ id: batch.id, action })
        if (batch.state !== (forward ? 'pending' : 'applied')) return respond({ detail: '批次已处理' }, 409)
        if (action !== 'reject') {
          for (const item of batch.files) if (disk.has(item.path) !== item[forward ? 'before_exists' : 'after_exists'] || (disk.get(item.path) || '') !== item[forward ? 'before_text' : 'after_text']) return respond({ detail: '目标已发生其他改动，整个批次未应用' }, 409)
          for (const item of batch.files) if (item[forward ? 'after_exists' : 'before_exists']) disk.set(item.path, item[forward ? 'after_text' : 'before_text']); else disk.delete(item.path)
        }
        batch.state = action === 'apply' ? 'applied' : action === 'rollback' ? 'reverted' : 'rejected'
        return respond({ changed: action !== 'reject', paths: batch.files.map(item => item.path), batch: batchView(batch), warning: '', ...(action !== 'reject' ? { index_refresh: { pending: true, state: 'running', requested_generation: decisions.length, completed_generation: decisions.length - 1 } } : {}) })
      }
    }
    if (pathname === `/api/v1/sessions/${sessionId}/events` && method === 'GET') return respond([])
    if (pathname === `/api/v1/sessions/${sessionId}/checkpoints` && method === 'GET') return respond({ checkpoints: [], busy: false })
    if (pathname === `/api/v1/sessions/${sessionId}/approvals/pending` && method === 'GET') return respond({})
    if (pathname === '/api/v1/web' && method === 'GET') return respond({ available: false, reason: 'fixture offline' })
    unexpected.push(`${method} ${pathname}`); return route.abort()
  })
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/__workspace-refactor-fixture`, { waitUntil: 'networkidle' })
  const treeFile = rel => page.locator('.tree .row').filter({ hasText: rel })
  const open = async rel => { await treeFile(rel).click(); await page.waitForFunction(rel => window.__refactorFixture.editor?.getModel()?.uri.toString().endsWith('/' + rel), rel) }
  const modal = () => page.getByRole('dialog', { name: '代码修改预览' })
  const editPayload = title => ({ title, projectId: pid, tree: 'main', source: 'refactor', edit: { documentChanges: [
    { textDocument: { uri: `file:///${root}/Old.java`, version: null }, edits: [{ range: { start: { line: 1, character: 13 }, end: { line: 1, character: 16 } }, newText: 'New' }] },
    { kind: 'rename', oldUri: `file:///${root}/Old.java`, newUri: `file:///${root}/New.java` },
    { textDocument: { uri: `file:///${root}/Caller.java`, version: null }, edits: [{ range: { start: { line: 1, character: 15 }, end: { line: 1, character: 18 } }, newText: 'New' }] },
    { kind: 'create', uri: `file:///${root}/Empty.java` },
    { kind: 'delete', uri: `file:///${root}/Obsolete.java` },
  ] } })
  const emitEdit = async title => page.evaluate(payload => window.__refactorFixture.emitEdit(payload), editPayload(title))
  const setText = async text => { await page.evaluate(text => window.__refactorFixture.editor.getModel().setValue(text), text); await page.waitForTimeout(40) }
  const shot = async name => { const target = path.join(output, name); await page.screenshot({ path: target, fullPage: true }); screenshots.push(target) }
  await treeFile('Old.java').waitFor(); await open('Old.java'); await open('Caller.java')
  assert.equal(await page.getByTitle('保存后调试 Python / Java · F5', { exact: true }).isEnabled(), true)
  checks.push('The actual workbench debug toolbar enables Java source files')
  await page.waitForFunction(() => !!document.querySelector('.lsp.ready'))
  await setText(originals['Caller.java'] + '// UNSAVED_BEFORE_PREVIEW\n')
  await emitEdit('Dirty draft blocked')
  await page.waitForFunction(() => document.querySelector('.banner')?.textContent.includes('草稿'))
  assert.equal(await modal().count(), 0); assert.equal(batches.size, 0); assert.deepEqual(Object.fromEntries(disk), originals)
  checks.push('Actual CodeView rejects resource refactors over an unsaved Monaco draft before creating a proposal')
  await setText(originals['Caller.java']); await emitEdit('Rename class and update caller')
  await modal().waitFor(); assert.equal(batches.size, 1); assert.deepEqual(Object.fromEntries(disk), originals)
  assert.match(await modal().innerText(), /5 个路径[\s\S]*Old\.java → New\.java[\s\S]*新增 · Empty\.java[\s\S]*删除 · Obsolete\.java/)
  await shot('resource-refactor-preview.png')
  checks.push('Resource preview shows move, creation, deletion and cross-file edit while disk fixtures remain unchanged')
  await setText(originals['Caller.java'] + '// UNSAVED_AFTER_PREVIEW\n')
  await modal().getByRole('button', { name: '应用项目重构', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.banner')?.textContent.includes('草稿'))
  assert.equal(decisions.length, 0); assert.equal(await modal().count(), 1)
  checks.push('A later draft invalidates the whole preview before any apply request is sent')
  await setText(originals['Caller.java']); await modal().getByRole('button', { name: '应用项目重构', exact: true }).click(); await modal().waitFor({ state: 'detached' })
  await treeFile('New.java').waitFor(); await page.waitForFunction(() => window.__refactorFixture.editor?.getValue().includes('New item'))
  assert.equal(disk.has('Old.java'), false); assert.equal(disk.has('Obsolete.java'), false); assert.equal(disk.get('Empty.java'), '')
  assert.equal(disk.get('New.java'), originals['Old.java'].replace('class Old', 'class New'))
  assert.equal(disk.get('Caller.java'), originals['Caller.java'].replace('Old item', 'New item'))
  assert.equal(await page.locator('.editor-tabs').getByText('Old.java', { exact: true }).count(), 0)
  checks.push('Apply writes the whole resource batch, reloads a changed open caller, refreshes FileTree and removes a clean moved tab')
  await page.locator('.index-refresh').waitFor(); await page.locator('.index-refresh').waitFor({ state: 'detached' })
  assert.ok(requests.some(request => request.path === `/api/v1/code/projects/${pid}/index`))
  checks.push('Committed refactoring acknowledges immediately while the real CodeView polls the queued index generation to completion')
  assert.ok(await page.evaluate(() => window.__refactorFixture.frames.some(frame => frame.method === 'workspace/didChangeWatchedFiles')))
  checks.push('Applied resource changes notify the connected language service')
  await page.getByRole('button', { name: '撤销代码修改', exact: true }).click()
  await treeFile('Old.java').waitFor(); await page.waitForFunction(() => window.__refactorFixture.editor?.getValue().includes('Old item'))
  assert.deepEqual(Object.fromEntries(disk), originals)
  assert.equal(batches.get('fixture-batch-1').state, 'reverted')
  checks.push('Toolbar undo rolls back the exact whole batch and restores every original path and caller text')
  await emitEdit('Persistent project refactor'); await modal().waitFor(); await modal().getByRole('button', { name: '取消', exact: true }).click()
  await page.locator('.right-tabs').getByRole('button', { name: '修改', exact: true }).click()
  await page.locator('.changes').waitFor(); await page.locator('.proposal-list button').filter({ hasText: 'Persistent project refactor' }).click()
  await page.locator('.review-actions').getByRole('button', { name: '应用整批', exact: true }).waitFor()
  await page.reload({ waitUntil: 'networkidle' }); await treeFile('Caller.java').waitFor()
  await page.locator('.right-tabs').getByRole('button', { name: '修改', exact: true }).click(); await page.locator('.changes').waitFor()
  await page.locator('.proposal-list button').filter({ hasText: 'Persistent project refactor' }).click()
  assert.equal(await page.locator('.review-actions').getByRole('button', { name: '应用整批', exact: true }).isEnabled(), true)
  assert.match(await page.locator('.changes').innerText(), /New\.java[\s\S]*Caller\.java[\s\S]*Empty\.java[\s\S]*Obsolete\.java/)
  checks.push('Cancelling the immediate preview and reloading preserves the pending resource batch and its complete diff review')
  await open('Caller.java'); await setText(originals['Caller.java'] + '// DIRTY_PERSISTED_REVIEW\n')
  await page.waitForFunction(() => document.querySelector('.review-actions button.primary')?.disabled)
  assert.equal(await page.locator('.review-actions').getByRole('button', { name: '应用整批', exact: true }).isDisabled(), true)
  checks.push('Persistent ChangesPanel blocks applying a batch when any affected open file has a dirty draft')
  await setText(originals['Caller.java']); await page.locator('.review-actions').getByRole('button', { name: '应用整批', exact: true }).click()
  await treeFile('New.java').waitFor(); await page.waitForFunction(() => window.__refactorFixture.editor?.getValue().includes('New item'))
  assert.equal(batches.get('fixture-batch-2').state, 'applied')
  await page.reload({ waitUntil: 'networkidle' }); await treeFile('New.java').waitFor(); await page.locator('.right-tabs').getByRole('button', { name: '修改', exact: true }).click()
  await page.locator('.proposal-list button').filter({ hasText: 'Persistent project refactor' }).click()
  await page.locator('.review-actions').getByRole('button', { name: '回滚整批', exact: true }).click()
  await treeFile('Old.java').waitFor(); assert.deepEqual(Object.fromEntries(disk), originals)
  checks.push('Persisted applied batch remains available for full rollback after a fresh CodeView reload')
  await open('Caller.java'); await emitEdit('External file collision'); await modal().waitFor()
  disk.set('New.java', 'EXTERNAL_CREATED_FILE\n')
  await modal().getByRole('button', { name: '应用项目重构', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.banner')?.textContent.includes('整个批次未应用'))
  assert.equal(await modal().count(), 1); assert.equal(disk.get('Old.java'), originals['Old.java']); assert.equal(disk.get('Caller.java'), originals['Caller.java']); assert.equal(disk.get('New.java'), 'EXTERNAL_CREATED_FILE\n')
  checks.push('An API conflict keeps the review visible and preserves both original project files and an externally created destination')
  await modal().getByRole('button', { name: '取消', exact: true }).click(); disk.delete('New.java')
  await emitEdit('Mobile project refactor'); await modal().waitFor(); await page.setViewportSize({ width: 390, height: 844 })
  const dimensions = await modal().evaluate(el => { const rect = el.getBoundingClientRect(); return { x: rect.x, right: rect.right, width: rect.width, page: document.documentElement.scrollWidth, viewport: innerWidth } })
  assert.ok(dimensions.x >= 0 && dimensions.right <= 391 && dimensions.page <= 392, JSON.stringify(dimensions))
  await shot('resource-refactor-mobile.png'); checks.push('390px resource preview remains within the viewport with readable operation labels')
  assert.equal(requests.some(request => request.method === 'PUT' || request.path.endsWith('/prompt')), false)
  assert.deepEqual(unexpected, []); assert.deepEqual(errors, []); assert.deepEqual(await page.evaluate(() => window.__refactorFixture.socketErrors), [])
  checks.push('Resource refactoring never invokes individual file saves, terminal commands, models or a real API')
} catch (error) { failure = error; errors.push(error.message); if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}) }
finally { await browser.close(); fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ checks, errors, unexpected, decisions, requests, screenshots }, null, 2)) }
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, report: path.join(output, 'report.json') }))
