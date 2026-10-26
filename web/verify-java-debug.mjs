import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Actual DebugPanel with isolated HTTP state. No JVM, user project or model call.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const origin = new URL(process.argv[2] || 'http://127.0.0.1:5175').origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/java-debug-browser', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const component = await (await fetch(`${origin}/src/components/code/DebugPanel.vue`)).text()
const vueModule = component.match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vueModule)
const checks = [], errors = [], unexpected = [], calls = []
let javaFound = false, session = null, sequence = 1
const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{margin:0;height:100%;min-width:0;}body{font:13px system-ui;background:#f4f6fa;}#app{height:100%;display:flex;flex-direction:column;}.head{padding:12px;font-weight:600;border-bottom:1px solid #e3e7ef;background:#fff;}.main{flex:1;min-height:0;padding:12px;}section.debug-panel{background:#fff;border:1px solid #e3e7ef;border-radius:8px;}
</style></head><body><div id="app"></div><script type="module">
import {createApp,h,ref,nextTick} from '${vueModule}';import DebugPanel from '/src/components/code/DebugPanel.vue';import '/src/style.css';
const state=window.__debugFixture={requests:[],locations:[],opened:[],errors:[]},file=ref({path:'src/Main.java',dirty:false}),pane=ref(null);
createApp({setup(){return()=>h('div',{id:'app'},[h('header',{class:'head'},'Java 调试 · 隔离验收'),h('main',{class:'main'},[h(DebugPanel,{ref:pane,projectId:'java-debug-fixture',file:file.value,breakpoints:[{path:'src/Main.java',line:7},{path:'main.py',line:3}],onStartRequest:payload=>state.requests.push(payload),onLocation:payload=>state.locations.push(payload),onOpenFile:payload=>state.opened.push(payload),onError:message=>state.errors.push(message)})])])}}).mount('#app');
Object.assign(state,{file,pane,nextTick,start:()=>pane.value.start(state.requests.at(-1))});
</script></body></html>`
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page, failure
try {
  const context = await browser.newContext({ viewport: { width: 1240, height: 750 }, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url()), request = route.request()
    if (url.origin !== origin) { unexpected.push(url.href); return route.abort() }
    if (url.pathname === '/__java-debug-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    const respond = value => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) })
    if (url.pathname.startsWith('/api/v1/code/projects/java-debug-fixture/debug')) {
      const suffix = url.pathname.slice('/api/v1/code/projects/java-debug-fixture/debug'.length), body = request.postDataJSON()
      calls.push({ method: request.method(), suffix, body })
      if (suffix === '/meta') return respond({ available: true, languages: javaFound ? ['python', 'java'] : ['python'], python: 'C:/fixture/python.exe', adapters: { python: { available: true }, java: { available: javaFound, bundle_found: javaFound, install_hint: javaFound ? null : 'Install Java Debug plugin fixture', requests: ['launch', 'attach'] } } })
      if (suffix === '/sessions' && request.method() === 'GET') return respond({ sessions: [] })
      if (suffix === '/sessions' && request.method() === 'POST') {
        session = { id: 'debug-fixture', project_id: 'java-debug-fixture', path: body.path, language: body.language || 'python', request: body.java?.request || 'launch', main_class: body.java?.main_class || '',
          status: 'paused', reason: 'entry', seq: ++sequence, pause_seq: 1, frame_id: 10, thread_id: 1, stack: [{ id: 10, name: 'demo.Main.main(String[])', path: body.path, line: 7, column: 1 }], scopes: [], variables: [{ name: 'count', value: '1', variablesReference: 0 }, { name: 'items', value: '[1,2]', variablesReference: 5 }], breakpoints: body.breakpoints.map(bp => ({ ...bp, requested_line: bp.line, verified: true })), console: [{ seq: 1, category: 'stdout', output: 'fixture output\n' }], console_seq: 1 }
        return respond(session)
      }
      if (suffix.endsWith('/action')) {
        session = { ...session, seq: ++sequence, status: body.action === 'stop' ? 'stopped' : body.action === 'continue' ? 'running' : 'paused' }
        return respond(session)
      }
      if (suffix.endsWith('/breakpoints')) { session = { ...session, seq: ++sequence, breakpoints: body.breakpoints.map(bp => ({ ...bp, requested_line: bp.line, verified: true })) }; return respond(session) }
      if (suffix.endsWith('/variables')) return respond({ variables: [{ name: '0', value: '1', variablesReference: 0 }] })
      if (/^\/sessions\/[^/]+$/.test(suffix)) return respond(session)
      unexpected.push(url.pathname); return route.abort()
    }
    if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return route.abort() }
    return route.continue()
  })
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/__java-debug-fixture`, { waitUntil: 'networkidle' })
  await page.getByText('Install Java Debug plugin fixture', { exact: false }).waitFor()
  assert.equal(await page.getByRole('button', { name: '▶ 启动', exact: true }).isDisabled(), true)
  assert.equal(await page.getByLabel('调试语言').inputValue(), 'auto')
  checks.push('Java prerequisites remain unavailable honestly even while Python is installed')
  javaFound = true
  await page.getByRole('button', { name: '重新检测', exact: true }).click()
  await page.getByRole('button', { name: '▶ 启动', exact: true }).waitFor({ state: 'visible' })
  await page.waitForFunction(() => ![...document.querySelectorAll('button')].find(button => button.textContent.trim() === '▶ 启动')?.disabled)
  await page.getByLabel('Java 主类').fill('demo.Main'); await page.getByLabel('Java 项目名').fill('demo')
  await page.getByLabel('入口暂停', { exact: true }).uncheck()
  await page.getByLabel('调试参数').fill('["--test"]')
  await page.getByRole('button', { name: '▶ 启动', exact: true }).click()
  assert.equal(calls.some(call => call.method === 'POST' && call.suffix === '/sessions'), false)
  const requested = await page.evaluate(() => window.__debugFixture.requests[0])
  assert.deepEqual(requested.java, { request: 'launch', main_class: 'demo.Main', project_name: 'demo', stop_on_entry: false })
  await page.evaluate(() => window.__debugFixture.start())
  await page.getByText('demo.Main.main(String[])', { exact: true }).waitFor()
  const launched = calls.find(call => call.method === 'POST' && call.suffix === '/sessions').body
  assert.equal(launched.language, 'java'); assert.deepEqual(launched.breakpoints, [{ path: 'src/Main.java', line: 7 }])
  assert.deepEqual(launched.args, ['--test'])
  checks.push('Java launch requests parent save first, then sends main class/project/options and Java-only breakpoints')
  await page.getByRole('button', { name: '展开变量 items' }).click()
  await page.locator('.debug-variable').filter({ hasText: '0' }).waitFor()
  assert.ok(calls.some(call => call.suffix.endsWith('/variables')))
  await page.getByRole('button', { name: '↷ 跳过', exact: true }).click()
  assert.ok(calls.some(call => call.suffix.endsWith('/action') && call.body.action === 'next'))
  assert.equal((await page.evaluate(() => window.__debugFixture.locations.filter(Boolean).at(-1))).path, 'src/Main.java')
  checks.push('Java paused state exposes call stack, nested variables, step controls and source location')
  await page.screenshot({ path: path.join(output, 'java-debug-paused.png'), fullPage: true })
  await page.getByRole('button', { name: '■ 停止', exact: true }).click()
  await page.getByRole('button', { name: '附加', exact: true }).click()
  await page.getByLabel('Java 附加地址').selectOption('::1'); await page.getByLabel('Java 附加端口').fill('5006')
  await page.evaluate(() => window.__debugFixture.file.value = { path: 'src/Main.java', dirty: true })
  await page.getByRole('button', { name: '▷ 连接', exact: true }).click()
  await page.evaluate(() => window.__debugFixture.start())
  await page.getByRole('button', { name: '■ 断开', exact: true }).waitFor()
  const attached = calls.filter(call => call.method === 'POST' && call.suffix === '/sessions').at(-1).body
  assert.equal(attached.java.request, 'attach'); assert.equal(attached.java.host, '::1'); assert.equal(attached.java.port, 5006)
  assert.deepEqual(attached.args, [])
  assert.match(await page.getByRole('button', { name: '■ 断开', exact: true }).getAttribute('title'), /保留 Java 进程/)
  checks.push('Java attach sends only a local endpoint, accepts unsaved source and labels disconnect semantics')
  await page.getByRole('button', { name: '■ 断开', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  const metrics = await page.locator('.debug-panel').evaluate(element => ({ right: element.getBoundingClientRect().right, viewport: innerWidth, document: document.documentElement.scrollWidth }))
  assert.ok(metrics.right <= 391 && metrics.document <= 392, JSON.stringify(metrics))
  await page.screenshot({ path: path.join(output, 'java-debug-mobile.png'), fullPage: true })
  checks.push('Java launch configuration remains contained at 390px')
  await page.getByLabel('调试语言').selectOption('python'); await page.getByLabel('调试入口').fill('main.py')
  await page.getByRole('button', { name: '▶ 启动', exact: true }).click()
  const pythonRequest = await page.evaluate(() => window.__debugFixture.requests.at(-1))
  assert.equal(pythonRequest.path, 'main.py'); assert.equal(pythonRequest.java, undefined); assert.equal(pythonRequest.language, undefined)
  checks.push('Manual Python selection retains the previous Python-only payload')
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
  assert.deepEqual(await page.evaluate(() => window.__debugFixture.errors), [])
  checks.push('All debugger requests stay within the fixture and no real program launches')
} catch (error) { failure = error; errors.push(error.message); if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}) }
finally { await browser.close(); fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ checks, errors, unexpected, calls }, null, 2)) }
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, report: path.join(output, 'report.json') }))
