import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Real Chrome/Vue; every API (including native picker) is intercepted before reaching backend.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const playwright = pw.default || pw, base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/project-picker-ui', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], screenshots = [], scenarios = [], errors = [], forbidden = [], unexpected = []
let page, failure = null, apiCount = 0
const redact = value => String(value).replace(/([?&]token=)[^&\s"']+/gi, '$1[redacted]').replace(/Bearer\s+[^\s"']+/gi, 'Bearer [redacted]')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const browser = await playwright.chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
try {
  async function scenario(name, { nativeAvailable = true, pick = async () => ({ path: 'E:/picker-fixture/selected', cancelled: false }), initialProjects = [], remembered } = {}) {
    const context = await browser.newContext({ viewport: { width: 1320, height: 860 }, serviceWorkers: 'block' })
    const state = { name, picks: [], opens: [], removals: [], projects: structuredClone(initialProjects), socketRequests: [] }; scenarios.push(state)
    const session = { id: `session-${name}`, permission_mode: 'read-only', model: 'fixture-model', status: 'open', busy: false }
    await context.addInitScript(({ session, remembered }) => {
      localStorage.setItem('code-agent:nav-collapsed', 'true')
      if (remembered !== undefined && localStorage.getItem('code-agent-active-project-v2:picker-fixture') === null) localStorage.setItem('code-agent-active-project-v2:picker-fixture', remembered)
      const Native = window.WebSocket
      class FixtureSocket extends EventTarget {
        static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3
        constructor(url, protocols) {
          super(); const target = new URL(url, location.href)
          if (!target.pathname.startsWith('/api/')) return new Native(url, protocols)
          this.url = target.href; this.readyState = 0; this.protocol = ''; this.extensions = ''; this.bufferedAmount = 0; this.binaryType = 'blob'
          this.CONNECTING = 0; this.OPEN = 1; this.CLOSING = 2; this.CLOSED = 3
          window.__pickerFixtureSockets ||= []; window.__pickerFixtureSockets.push(target.pathname)
          queueMicrotask(() => {
            if (this.readyState !== 0) return
            if (target.pathname !== `/api/v1/sessions/${session.id}/stream`) return this.close(4404, 'uncovered fixture websocket')
            this.readyState = 1; this.emit('open', new Event('open')); this.frame({ type: 'session_meta', seq: 0, payload: session })
          })
        }
        emit(type, event) { this.dispatchEvent(event); this[`on${type}`]?.(event) }
        frame(data) { if (this.readyState === 1) this.emit('message', new MessageEvent('message', { data: JSON.stringify(data) })) }
        send(text) { const frame = JSON.parse(text); if (frame.type === 'ping') queueMicrotask(() => this.frame({ type: 'pong', seq: 0, payload: {} })); else { window.__pickerFixtureBadFrames ||= []; window.__pickerFixtureBadFrames.push(frame.type || 'unknown') } }
        close(code = 1000, reason = '') { if (this.readyState === 3) return; this.readyState = 3; queueMicrotask(() => this.emit('close', new CloseEvent('close', { code, reason, wasClean: true }))) }
      }
      window.WebSocket = FixtureSocket
    }, { session, remembered })
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url()), pathname = url.pathname, method = req.method(), label = `${method} ${pathname}`
      if (url.origin !== origin) { forbidden.push(label); return route.abort('blockedbyclient') }
      if (!pathname.startsWith('/api/') && pathname !== '/healthz') return route.continue()
      apiCount++
      const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
      if (/\/fs\/list$|\/scan$|\/prompt$|\/feature-briefs\/fill$|\/reindex$/.test(pathname)) { forbidden.push(label); return reply({ detail: 'Blocked in picker verification' }, 403) }
      if (pathname === '/healthz') return reply({ ok: true })
      if (pathname === '/api/v1/auth/me') return reply({ id: 'picker-fixture', username: 'fixture', role: 'admin' })
      if (pathname === '/api/v1/web') return reply({ available: false, reason: 'fixture' })
      if (pathname === '/api/v1/code/meta') return reply({ templates: ['empty', 'python', 'node'], projects_root: 'E:/picker-fixture', git_available: false, browse_enabled: true, native_picker: { available: nativeAvailable, platform: nativeAvailable ? 'win32' : 'remote', reason: nativeAvailable ? '' : '远程服务无法打开本机文件夹窗口，请手动输入工作区路径。' } })
      if (pathname === '/api/v1/code/fs/pick-folder' && method === 'POST') { const body = req.postDataJSON(); state.picks.push(body); return reply(await pick(body)) }
      if (pathname === '/api/v1/code/projects') {
        if (method === 'GET') return reply(state.projects)
        if (method === 'POST') { const body = req.postDataJSON(); state.opens.push(body); const parent = body.location || 'E:/picker-fixture', separator = parent.includes('\\') ? '\\' : '/'; const project = { id: `project-${name}`, name: body.name || `Opened ${name}`, root_path: body.mode === 'create' ? `${parent.replace(/[\\/]+$/, '')}${separator}${body.name}` : body.path, available: true, allow_direct_write: false, git: { is_repo: false } }; state.projects.push(project); return reply(project) }
      }
      if (pathname.startsWith(`/api/v1/sessions/${session.id}`) && method === 'GET') {
        const suffix = pathname.slice(`/api/v1/sessions/${session.id}`.length)
        if (!suffix) return reply(session)
        if (suffix === '/events') return reply([])
        if (suffix === '/checkpoints') return reply({ items: [], busy: false })
        if (suffix === '/approvals/pending') return reply({})
      }
      const project = state.projects.find(item => pathname === `/api/v1/code/projects/${item.id}` || pathname.startsWith(`/api/v1/code/projects/${item.id}/`)), prefix = project && `/api/v1/code/projects/${project.id}`
      if (prefix && pathname.startsWith(prefix)) {
        const suffix = pathname.slice(prefix.length)
        if (!suffix && method === 'GET') return reply(project)
        if (!suffix && method === 'DELETE') { state.removals.push(project.id); state.projects = state.projects.filter(item => item.id !== project.id); return reply({ deleted: true, files_deleted: false }) }
        if (suffix === '/session' && method === 'POST') { const body = req.postDataJSON() || {}; if (body.permission_mode === 'workspace-write') forbidden.push(`${label} workspace-write`); return reply(session) }
        if (method === 'GET') {
          if (suffix === '/tree') return reply({ dirs: [], files: [{ name: 'main.js', path: 'main.js', size: 56 }], truncated: false })
          if (suffix === '/files') return reply({ files: [{ name: 'main.js', path: 'main.js', size: 56 }], truncated: false, inventory_truncated: false })
          if (suffix === '/file') return reply({ name: 'main.js', path: 'main.js', text: `// ${project.name}\nconst greeting = "workspace";\n`, sha1: `hash-${project.id}`, lang: 'javascript', uri: `file:///${project.root_path}/main.js`, editable: true, secret: false, start_line: 1, end_line: 3, total_lines: 3, size: 56 })
          if (suffix === '/lsp') return reply({ servers: [] })
          if (suffix === '/diagnostics') return reply({ seq: 1, counts: { error: 0, warning: 0 }, files: {}, symbols: {}, items: [] })
          if (suffix === '/patches') return reply({ patches: [], counts: { total: 0, active: 0 } })
          if (suffix === '/tasks' || suffix === '/batches') return reply([])
          if (suffix === '/git') return reply({ is_repo: false, changes: [] })
          if (suffix === '/index') return reply({ change_seq: 0, scanning: false, briefs: { running: false } })
          if (suffix === '/map/delta') return reply({ to_seq: 0, changes: [], added: [], updated: [], removed: [] })
          if (suffix === '/architecture') return reply({ rules: {}, errors: [] })
        }
      }
      unexpected.push(label); return reply({ detail: 'Uncovered picker fixture API' }, 501)
    })
    const current = await context.newPage(); page = current; current.setDefaultTimeout(15000)
    current.on('pageerror', e => errors.push(redact(e.stack || e.message)))
    current.on('console', message => { if (message.type() === 'error') errors.push(redact(message.text())) })
    await current.goto(base, { waitUntil: 'networkidle' }); await current.locator('.project-switcher-trigger').waitFor()
    const snapshot = async name => { const filename = `${name}.png`; await current.screenshot({ path: path.join(output, filename), fullPage: true }); screenshots.push(filename) }
    const audit = async () => { state.socketRequests = await current.evaluate(() => window.__pickerFixtureSockets || []); assert.ok(state.socketRequests.every(item => item === `/api/v1/sessions/${session.id}/stream`)); assert.equal(await current.evaluate(() => (window.__pickerFixtureBadFrames || []).length), 0) }
    return { context, page: current, state, picker: current.locator('.workspace-entry'), snapshot, audit }
  }

  const chosen = deferred(), started = deferred()
  const selected = await scenario('native', { pick: body => { started.resolve(body); return chosen.promise } })
  await selected.picker.locator('.open-folder:not(:disabled)').waitFor()
  await selected.snapshot('01-workspace-welcome')
  await selected.picker.getByRole('button', { name: '新建项目', exact: true }).click(); await selected.picker.getByRole('button', { name: '返回打开工作区', exact: true }).click()
  assert.equal(selected.state.picks.length, 0); assert.equal(selected.state.opens.length, 0); checks.push('welcome actions are explicit; changing create/open views makes no API mutation')
  await selected.picker.locator('.open-folder').click(); await started.promise
  assert.equal(await selected.picker.locator('.open-folder').isDisabled(), true)
  await selected.picker.locator('.open-folder').dispatchEvent('click')
  assert.equal(selected.state.picks.length, 1); assert.deepEqual(selected.state.picks[0], { start: '' }); assert.equal(selected.state.opens.length, 0)
  checks.push('system folder selection sends one POST and blocks duplicate actions while pending'); await selected.snapshot('02-native-selecting')
  chosen.resolve({ path: 'E:/picker-fixture/selected', cancelled: false })
  await selected.picker.waitFor({ state: 'detached' }); await selected.page.waitForFunction(id => document.querySelector('.project-switcher-trigger')?.dataset.projectId === id && !document.querySelector('.project-switcher-trigger')?.disabled, 'project-native')
  assert.deepEqual(selected.state.opens, [{ mode: 'open', path: 'E:/picker-fixture/selected' }]); checks.push('native selection registers once and enters immediately without reloading the project list'); await selected.snapshot('03-native-opened')
  await selected.audit(); await selected.context.close()

  const cancelled = await scenario('cancel', { pick: async () => ({ path: '', cancelled: true }) })
  await cancelled.picker.locator('.manual-toggle:not(:disabled)').click()
  await cancelled.page.getByRole('textbox', { name: '工作区路径', exact: true }).fill('E:/keep-manual-path')
  await cancelled.picker.locator('.open-folder').click()
  await cancelled.page.waitForFunction(() => !document.querySelector('.workspace-entry .open-folder')?.disabled)
  assert.equal(await cancelled.page.getByRole('textbox', { name: '工作区路径', exact: true }).inputValue(), 'E:/keep-manual-path')
  assert.deepEqual(cancelled.state.picks, [{ start: 'E:/keep-manual-path' }]); assert.equal(cancelled.state.opens.length, 0); assert.equal(await cancelled.picker.locator('.entry-error').count(), 0)
  checks.push('cancel preserves the manual path and creates no project or error'); await cancelled.snapshot('04-native-cancelled'); await cancelled.audit(); await cancelled.context.close()

  const remote = await scenario('remote', { nativeAvailable: false })
  await remote.picker.locator('.path-form').waitFor()
  assert.equal(await remote.picker.locator('.open-folder').count(), 0); assert.equal(await remote.picker.locator('.path-form button[type="submit"]').isDisabled(), true)
  assert.match(await remote.picker.innerText(), /请手动输入工作区路径/); assert.equal(await remote.page.locator('.dir-picker, .dir-shade, .browse-list, .browse-head').count(), 0)
  checks.push('unavailable native picker uses a clear manual-path entry'); await remote.snapshot('05-remote-manual')
  await remote.page.getByRole('textbox', { name: '工作区路径', exact: true }).fill('E:/picker-fixture/manual')
  await remote.picker.locator('.path-form button[type="submit"]').click(); await remote.picker.waitFor({ state: 'detached' })
  assert.equal(remote.state.picks.length, 0); assert.deepEqual(remote.state.opens, [{ mode: 'open', path: 'E:/picker-fixture/manual' }])
  checks.push('manual path registers directly when native picker is unavailable'); await remote.snapshot('06-manual-opened'); await remote.audit(); await remote.context.close()

  const recentProjects = [
    { id: 'recent-alpha', name: 'Alpha 项目', root_path: 'E:/workspace/alpha', available: true, git: { is_repo: true, branch: 'main' } },
    { id: 'recent-beta', name: 'Beta 组件工作区', root_path: 'E:/workspace/beta', available: true, git: { is_repo: true, branch: 'develop' } },
  ]
  const existingCancel = deferred(), existingStarted = deferred()
  const existing = await scenario('existing', { initialProjects: recentProjects, remembered: 'recent-beta', pick: body => { existingStarted.resolve(body); return existingCancel.promise } })
  const trigger = existing.page.getByRole('button', { name: '切换项目', exact: true })
  const menu = () => existing.page.getByRole('dialog', { name: '项目和工作区', exact: true })
  await existing.page.waitForFunction(() => document.querySelector('.project-switcher-trigger')?.dataset.projectId === 'recent-beta' && !document.querySelector('.project-switcher-trigger')?.disabled)
  assert.match(await trigger.innerText(), /Beta 组件工作区/); assert.match(await trigger.innerText(), /develop/)
  checks.push('reopening restores the last successful project with visible path and branch')
  await trigger.click(); await menu().waitFor(); await existing.snapshot('07-project-menu')
  await existing.page.getByRole('textbox', { name: '搜索最近项目', exact: true }).fill('alpha')
  assert.equal(await menu().locator('.project-select').count(), 1)
  await existing.page.keyboard.press('Escape'); await menu().waitFor({ state: 'detached' })
  assert.equal(await trigger.evaluate(element => element === document.activeElement), true)
  checks.push('recent-project search filters by name and Escape restores trigger focus')
  await existing.page.locator('.tree .name[title="main.js"]').click()
  const input = existing.page.locator('.pane .monaco-editor .inputarea, .pane .monaco-editor .native-edit-context').first()
  await input.waitFor({ state: 'attached', timeout: 30000 }); await input.focus(); await existing.page.keyboard.press('Control+End'); await existing.page.keyboard.insertText('// keep unsaved work')
  await existing.page.locator('.editor-tabs .dirty-dot').waitFor()
  await trigger.click(); await menu().getByRole('button', { name: '打开文件夹…', exact: true }).click(); await existingStarted.promise
  assert.equal(await trigger.getAttribute('data-project-id'), 'recent-beta')
  assert.equal(await existing.page.locator('.editor-tabs .dirty-dot').count(), 1)
  existingCancel.resolve({ path: null, cancelled: true })
  await existing.page.waitForFunction(() => !document.querySelector('.project-switcher-trigger')?.disabled)
  assert.equal(await trigger.getAttribute('data-project-id'), 'recent-beta'); assert.equal(existing.state.opens.length, 0)
  assert.match((await existing.page.locator('.pane .monaco-editor .view-lines').innerText()).replace(/\u00a0/g, ' '), /keep unsaved work/)
  checks.push('opening from an active workspace and cancelling preserve the editor, project and dirty draft'); await existing.snapshot('08-cancel-preserves-editor')
  await trigger.click(); await menu().getByRole('button', { name: '输入工作区路径…', exact: true }).click()
  const modal = existing.page.getByRole('dialog', { name: '打开项目', exact: true })
  await modal.getByRole('textbox', { name: '工作区路径', exact: true }).waitFor()
  assert.equal(await trigger.getAttribute('data-project-id'), 'recent-beta')
  await existing.page.keyboard.press('Escape'); await modal.waitFor({ state: 'detached' })
  checks.push('manual-open modal focuses the path directly and dismissal preserves the workspace')
  await trigger.click(); await menu().getByRole('button', { name: '从最近记录移除 Alpha 项目', exact: true }).click()
  assert.equal(existing.state.removals.length, 0); await menu().getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(existing.state.removals.length, 0)
  await menu().getByRole('button', { name: '从最近记录移除 Alpha 项目', exact: true }).click()
  await menu().getByRole('button', { name: '移除记录', exact: true }).click()
  await existing.page.waitForFunction(() => !document.querySelector('.project-switcher-trigger')?.disabled)
  assert.deepEqual(existing.state.removals, ['recent-alpha']); assert.equal(await trigger.getAttribute('data-project-id'), 'recent-beta')
  checks.push('removing a recent project requires an explicit second step and preserves the current workspace')
  await menu().getByRole('button', { name: '关闭工作区', exact: true }).click()
  await existing.picker.waitFor(); await existing.page.reload({ waitUntil: 'networkidle' }); await existing.picker.waitFor()
  assert.equal(await trigger.getAttribute('data-project-id'), null)
  assert.equal(existing.state.projects.length, 1)
  checks.push('explicit workspace close survives refresh and retains recent projects and draft recovery')
  await existing.snapshot('09-closed-workspace-recents')
  await existing.page.setViewportSize({ width: 390, height: 844 })
  await existing.page.locator('.workspace-switcher button').filter({ hasText: '项目与文件' }).click()
  await trigger.click(); await menu().waitFor()
  const mobileBounds = await menu().boundingBox()
  assert.ok(mobileBounds.x >= 0 && mobileBounds.x + mobileBounds.width <= 391)
  assert.equal(await existing.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  checks.push('project menu fits a narrow viewport without sidebar clipping or horizontal overflow'); await existing.snapshot('10-mobile-project-menu')
  await existing.page.keyboard.press('Escape'); await existing.audit(); await existing.context.close()

  const created = await scenario('create')
  await created.picker.locator('.new-project:not(:disabled)').click()
  await created.page.getByRole('textbox', { name: '项目名称', exact: true }).fill('fresh-project')
  await created.page.getByRole('checkbox', { name: /初始化 Git 仓库/ }).uncheck()
  assert.equal(await created.page.getByRole('textbox', { name: '项目位置', exact: true }).inputValue(), 'E:/picker-fixture')
  await created.snapshot('11-new-project')
  await created.picker.locator('.create-button').click(); await created.picker.waitFor({ state: 'detached' })
  assert.deepEqual(created.state.opens, [{ mode: 'create', name: 'fresh-project', template: 'python', git_init: false, location: 'E:/picker-fixture' }])
  checks.push('create uses the unified controller and preserves template and boolean Git options'); await created.audit(); await created.context.close()

  const locationChosen = deferred(), locationStarted = deferred()
  let locationPick = 0
  const locationProject = await scenario('custom-location', { initialProjects: recentProjects, remembered: 'recent-beta', pick: body => {
    locationPick++
    if (locationPick === 1) { locationStarted.resolve(body); return locationChosen.promise }
    if (locationPick === 2) return { path: null, cancelled: true }
    return { path: 'E:\\开发项目\\个人目录', cancelled: false }
  } })
  await locationProject.page.waitForFunction(() => document.querySelector('.project-switcher-trigger')?.dataset.projectId === 'recent-beta' && !document.querySelector('.project-switcher-trigger')?.disabled)
  await locationProject.page.locator('.tree .name[title="main.js"]').click()
  const locationEditor = locationProject.page.locator('.pane .monaco-editor .inputarea, .pane .monaco-editor .native-edit-context').first()
  await locationEditor.waitFor({ state: 'attached', timeout: 30000 }); await locationEditor.focus(); await locationProject.page.keyboard.press('Control+End'); await locationProject.page.keyboard.insertText('// location draft remains')
  await locationProject.page.locator('.editor-tabs .dirty-dot').waitFor()
  await locationProject.page.getByRole('button', { name: '切换项目', exact: true }).click()
  await locationProject.page.getByRole('dialog', { name: '项目和工作区', exact: true }).getByRole('button', { name: '新建项目', exact: true }).click()
  const locationModal = locationProject.page.getByRole('dialog', { name: '打开项目', exact: true }), locationInput = locationModal.getByRole('textbox', { name: '项目位置', exact: true })
  await locationModal.getByRole('textbox', { name: '项目名称', exact: true }).fill('位置演示')
  await locationModal.getByRole('combobox', { name: '项目模板', exact: true }).selectOption('node')
  await locationModal.getByRole('checkbox', { name: /初始化 Git 仓库/ }).uncheck()
  await locationInput.fill('E:\\手动父目录')
  await locationModal.getByRole('button', { name: '浏览…', exact: true }).click(); await locationStarted.promise
  assert.equal(await locationModal.locator('.create-button').isDisabled(), true)
  await locationModal.getByRole('button', { name: '浏览…', exact: true }).dispatchEvent('click')
  assert.deepEqual(locationProject.state.picks, [{ start: 'E:\\手动父目录' }]); assert.equal(locationProject.state.opens.length, 0)
  assert.equal(await locationProject.page.locator('.project-switcher-trigger').getAttribute('data-project-id'), 'recent-beta')
  locationChosen.resolve({ path: 'E:\\开发项目\\个人目录', cancelled: false })
  await locationProject.page.waitForFunction(() => !document.querySelector('.workspace-entry .create-button')?.disabled)
  assert.equal(await locationInput.inputValue(), 'E:\\开发项目\\个人目录')
  assert.equal(await locationModal.getByRole('textbox', { name: '项目名称', exact: true }).inputValue(), '位置演示')
  assert.equal(await locationModal.getByRole('combobox', { name: '项目模板', exact: true }).inputValue(), 'node')
  assert.equal(await locationModal.getByRole('checkbox', { name: /初始化 Git 仓库/ }).isChecked(), false)
  assert.match(await locationModal.innerText(), /E:\\开发项目\\个人目录\\位置演示/)
  checks.push('creation location picker uses the current parent, blocks duplicates and never registers or changes the active draft')
  await locationInput.fill('E:\\临时输入')
  await locationModal.getByRole('button', { name: '浏览…', exact: true }).click()
  await locationProject.page.waitForFunction(() => !document.querySelector('.workspace-entry .create-button')?.disabled)
  assert.equal(await locationInput.inputValue(), 'E:\\临时输入'); assert.equal(locationProject.state.opens.length, 0)
  checks.push('cancelling creation location keeps all existing form values')
  await locationModal.getByRole('button', { name: '浏览…', exact: true }).click()
  await locationProject.page.waitForFunction(() => !document.querySelector('.workspace-entry .create-button')?.disabled)
  assert.equal(await locationInput.inputValue(), 'E:\\开发项目\\个人目录')
  assert.equal(await locationProject.page.locator('.editor-tabs .dirty-dot').count(), 1)
  assert.match((await locationProject.page.locator('.pane .monaco-editor .view-lines').innerText()).replace(/\u00a0/g, ' '), /location draft remains/)
  checks.push('selecting the same folder again replaces a later manual edit and keeps the current editor')
  await locationProject.snapshot('12-custom-location-modal')
  await locationModal.locator('.create-button').click(); await locationModal.waitFor({ state: 'detached' })
  assert.deepEqual(locationProject.state.opens, [{ mode: 'create', name: '位置演示', template: 'node', git_init: false, location: 'E:\\开发项目\\个人目录' }])
  await locationProject.page.waitForFunction(() => document.querySelector('.project-switcher-trigger')?.dataset.projectId === 'project-custom-location' && !document.querySelector('.project-switcher-trigger')?.disabled)
  assert.match(await locationProject.page.locator('.project-switcher-trigger').innerText(), /E:\\开发项目\\个人目录\\位置演示/)
  checks.push('custom Windows Unicode parent is sent with creation and becomes the final workspace root')
  await locationProject.audit(); await locationProject.context.close()

  const remoteCreate = await scenario('remote-create', { nativeAvailable: false })
  await remoteCreate.picker.locator('.new-project:not(:disabled)').click()
  await remoteCreate.page.getByRole('textbox', { name: '项目名称', exact: true }).fill('remote-project')
  const remoteLocation = remoteCreate.page.getByRole('textbox', { name: '项目位置', exact: true })
  await remoteLocation.fill('')
  assert.equal(await remoteCreate.picker.locator('.create-button').isDisabled(), true)
  assert.equal(await remoteCreate.picker.getByRole('button', { name: '浏览…', exact: true }).count(), 0)
  await remoteLocation.fill('/home/developer/workspaces/')
  assert.match(await remoteCreate.picker.innerText(), /\/home\/developer\/workspaces\/remote-project/)
  await remoteCreate.page.setViewportSize({ width: 390, height: 844 })
  assert.equal(await remoteCreate.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  await remoteCreate.snapshot('13-mobile-manual-location')
  await remoteCreate.picker.locator('.create-button').click(); await remoteCreate.picker.waitFor({ state: 'detached' })
  assert.deepEqual(remoteCreate.state.opens, [{ mode: 'create', name: 'remote-project', template: 'python', git_init: true, location: '/home/developer/workspaces/' }])
  assert.equal(remoteCreate.state.picks.length, 0)
  checks.push('remote creation accepts a manual parent and the editable location form fits a narrow viewport')
  await remoteCreate.audit(); await remoteCreate.context.close()

  assert.deepEqual(errors, []); assert.deepEqual(forbidden, []); assert.deepEqual(unexpected, [])
  checks.push('all API/WS traffic uses fixtures; zero fs/list, scan, native Show, model or workspace-write requests')
} catch (e) {
  failure = redact(e.stack || e.message)
  if (page) try { const filename = 'failure.png'; await page.screenshot({ path: path.join(output, filename), fullPage: true }); screenshots.push(filename) } catch {}
} finally {
  await browser.close()
  const report = { passed: !failure, checks, screenshots, apiCount, scenarios, errors, forbidden, unexpected, failure }
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2)); if (failure) process.exitCode = 1
}
