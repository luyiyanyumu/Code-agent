// Real coding AiPanel with fixture-only HTTP and WebSocket. No live project,
// model, command, approval or Git operation is accessed. The private Vite server
// has a separate cache and a denied proxy, and is always closed in finally.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

const root = path.dirname(fileURLToPath(import.meta.url))
const base = 'http://127.0.0.1:5197', origin = new URL(base).origin
const output = path.resolve(process.argv[2] || path.join(root, 'shots/tool-explanations'))
const checks = [], errors = [], unexpected = [], mutations = []
const rawSecret = 'SYNTHETIC_PRIVATE_THINKING_NEVER_IN_TOOL_EXPLANATION'
const bigResult = 'fixture-only-result\n'.repeat(12000) + 'LARGE_RESULT_END'
const call = (id, command, extra = {}) => ({ id, name: 'bash', arguments: { command, ...extra } })
const explain = (purpose, basis, effects) => ({ purpose, basis, effects, source: 'agent' })
const alpha = call('parallel-a', 'node fixture-ui.mjs --timeout=90000 --name="UI rendering"')
const beta = call('parallel-b', 'node fixture-api.mjs --offline --read-only')
const definitions = [
  [alpha, explain('验证窄屏下工具说明的显示。', '已确认面板使用当前组件；只使用模拟数据。', ['运行临时浏览器脚本。', '预期生成截图供核对。']), 'fixture UI checks passed'],
  [beta, explain('核对接口响应的字段。', '响应来自全模拟接口，不需要访问真实服务。', ['预期读取模拟响应。', '不发送模型请求。']), bigResult],
  [call('legacy', 'printf "historical fixture"'), null, 'old result without an explanation'],
  [call('legacy-approval', 'node old-fixture.mjs', { _approval: { purpose: '复用旧记录保存的操作目的。', effects: ['预期读取旧测试输出。'] } }), null, 'legacy metadata reused'],
  [call('denied', 'node fixture-denied.mjs'), explain('确认拒绝时仍可查看计划目的。', '模拟审批已经拒绝本次调用。', ['原计划执行临时验证。']), '[permission denied] fixture denial'],
  [call('failed', 'node fixture-error.mjs'), explain('验证失败记录的说明。', '此处模拟脚本错误，不是成功结果。', ['原计划产生验证报告。']), '[tool error] fixture failure'],
  [call('html', 'printf "<literal>"'), explain('<img src=x onerror="window.fixtureInjected=true">按文本显示目的。', '<script>window.fixtureInjected=true</script>按文本显示依据。', ['<b>此处是说明文本。</b>']), '<svg onload="window.fixtureInjected=true">literal output'],
  [call('running', 'node fixture-running.mjs --report=result.json'), explain('等待模拟验证完成。', '已启动全模拟执行，当前尚无结果。', ['预期写入临时模拟报告。']), undefined],
]
let seq = 0
const history = [
  { seq: ++seq, type: 'user_message', payload: { text: '请检查这些工具的公开执行说明。' } },
  { seq: 0, type: 'thinking_delta', payload: { text: rawSecret } },
  { seq: ++seq, type: 'assistant_message', payload: { text: '分别验证界面和接口字段。', tool_calls: [alpha, beta] } },
  ...definitions.slice(0, 2).map(([c, e]) => ({ seq: ++seq, type: 'tool_use', payload: { call: c, explanation: e } })),
  // Reverse completion order must not associate one purpose with the other call.
  ...definitions.slice(0, 2).toReversed().map(([c, , result]) => ({ seq: ++seq, type: 'tool_result', payload: { call_id: c.id, result } })),
]
for (const [c, e, result] of definitions.slice(2)) {
  history.push({ seq: ++seq, type: 'assistant_message', payload: { text: '', tool_calls: [c] } })
  history.push({ seq: ++seq, type: 'tool_use', payload: { call: c, ...(e ? { explanation: { ...e, reasoning: rawSecret, thinking: rawSecret } } : {}) } })
  if (result !== undefined) history.push({ seq: ++seq, type: 'tool_result', payload: { call_id: c.id, result } })
}
const sessions = new Map([
  ['main', { busy: true, phase: 'tools', history }],
  ['other', { busy: false, phase: '', history: [
    { seq: 1, type: 'assistant_message', payload: { text: '另一个会话使用相同调用编号。', tool_calls: [alpha] } },
    { seq: 2, type: 'tool_use', payload: { call: alpha, explanation: explain('核对另一个会话的独立目的。', '此记录属于另一会话。', ['预期保留会话边界。']) } },
    { seq: 3, type: 'tool_result', payload: { call_id: alpha.id, result: 'OTHER_SESSION_RESULT' } },
    { seq: 4, type: 'done', payload: { stop_reason: 'end_turn' } },
  ] }],
])
let vite, browser, failure
await fs.mkdir(output, { recursive: true })
try {
  vite = await createServer({ configFile: false, root, plugins: [vue()], cacheDir: path.join(root, 'node_modules/.vite-tool-explanations-fixture'),
    server: { host: '127.0.0.1', port: 5197, strictPort: true, proxy: { '/api': { target: 'http://127.0.0.1:9', ws: true }, '/healthz': 'http://127.0.0.1:9' } },
    optimizeDeps: { include: ['vue', 'marked'] }, logLevel: 'warn' })
  await vite.listen()
  const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  const { chromium } = loaded.default || loaded
  const source = await (await fetch(`${base}/src/components/code/AiPanel.vue`)).text()
  const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vuePath, 'The private Vite fixture must be running')
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1100, height: 1000 }, serviceWorkers: 'block' })
  await context.addInitScript(() => {
    const sockets = new Set(), opened = {}, sent = [], handlers = new Map()
    window.fixtureSockets = { opened, sent,
      emit(sid, event) { for (const socket of sockets) if (socket.sid === sid && socket.readyState === 1) { handlers.set(sid, socket.onmessage); socket.onmessage?.({ data: JSON.stringify(event) }) } },
      stale(sid, event) { handlers.get(sid)?.({ data: JSON.stringify(event) }) },
      disconnect(sid) { for (const socket of sockets) if (socket.sid === sid && socket.readyState === 1) { socket.readyState = 3; socket.onclose?.({ code: 1006 }) } },
    }
    window.WebSocket = class {
      static OPEN = 1
      constructor(url) {
        this.url = url; this.sid = new URL(url).pathname.match(/^\/api\/v1\/sessions\/([^/]+)\/stream$/)?.[1]; this.readyState = 0
        if (!this.sid) { queueMicrotask(() => { this.readyState = 1; this.onopen?.({}) }); return }
        sockets.add(this)
        queueMicrotask(async () => {
          const state = await (await fetch('/__tool-fixture/state/' + encodeURIComponent(this.sid))).json()
          if (this.readyState === 3) return
          this.readyState = 1; opened[this.sid] = (opened[this.sid] || 0) + 1; this.onopen?.({})
          const after = Number(new URL(url).searchParams.get('after') || new URL(url).searchParams.get('after_seq') || 0)
          for (const event of state.history) if (!(event.seq > 0) || event.seq > after) this.onmessage?.({ data: JSON.stringify(event) })
          this.onmessage?.({ data: JSON.stringify({ type: 'session_meta', payload: { busy: state.busy, phase: state.phase, model: 'fixture-offline', pending_questions: [] } }) })
        })
      }
      send(raw) { if (!this.sid) return; const message = JSON.parse(raw); sent.push({ sid: this.sid, ...message }); if (message.type !== 'ping') throw new Error('No real model request is permitted in this fixture') }
      close() { this.readyState = 3; sockets.delete(this) }
    }
  })
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  page.on('pageerror', error => errors.push(error.stack || error.message))
  page.on('dialog', dialog => { unexpected.push('native dialog'); void dialog.dismiss() })
  const fulfill = (route, value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
  const fixture = `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,#app{height:100%;width:100%;overflow:hidden;margin:0}#fixture{height:100%;width:560px;max-width:100%;margin:auto;display:flex;flex-direction:column;border-inline:1px solid var(--border)}#fixture>nav{flex:none;display:flex;flex-wrap:wrap;gap:6px;padding:7px;border-bottom:1px solid var(--border)}#fixture>nav button{font-size:12px;padding:4px 6px}#fixture>.ai{flex:1;min-height:0}</style></head><body><div id="app"></div><script type="module">
import{createApp,h,ref}from'${vuePath}';import Panel from'/src/components/code/AiPanel.vue';import'/src/style.css';const sid=ref('main');window.fixture={sid};createApp({render:()=>h('main',{id:'fixture'},[h('nav',[h('button',{id:'main',onClick:()=>sid.value='main'},'主会话'),h('button',{id:'other',onClick:()=>sid.value='other'},'另一会话'),h('button',{id:'theme',onClick:()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark'},'切换主题')]),h(Panel,{sessionId:sid.value,agentId:sid.value,writable:false})])}).mount('#app')
</script></body></html>`
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), p = decodeURIComponent(url.pathname), method = req.method()
    if (url.origin !== origin) { unexpected.push(`${method} ${url.origin}${p}`); return route.abort() }
    if (p === '/__tool-explanation-test') return route.fulfill({ contentType: 'text/html', body: fixture })
    if (p.startsWith('/__tool-fixture/state/')) return fulfill(route, sessions.get(p.split('/').at(-1)) || {}, 200)
    if (!p.startsWith('/api/')) return route.continue()
    if (method !== 'GET') { mutations.push(`${method} ${p}`); return fulfill(route, { detail: 'No mutation is allowed' }, 500) }
    if (p === '/api/v1/web') return fulfill(route, { available: false, reason: '全模拟验收' })
    const match = p.match(/^\/api\/v1\/sessions\/([^/]+)\/(events|checkpoints|approvals\/pending|questions\/pending)$/)
    if (match && sessions.has(match[1])) {
      const state = sessions.get(match[1])
      if (match[2] === 'events') return fulfill(route, state.history)
      if (match[2] === 'checkpoints') return fulfill(route, { busy: state.busy })
      if (match[2] === 'approvals/pending') return fulfill(route, {})
      if (match[2] === 'questions/pending') return fulfill(route, { questions: [] })
    }
    unexpected.push(`${method} ${p}`); return fulfill(route, { detail: 'Unexpected fixture API' }, 500)
  })
  const card = id => page.locator('.tool').filter({ has: page.locator('.tool-args').filter({ hasText: definitions.find(([c]) => c.id === id)?.[0].arguments.command || '' }) })
  await page.goto(`${base}/__tool-explanation-test`, { waitUntil: 'networkidle' })
  await page.locator('.tool-explanation').first().waitFor()
  assert.equal(await page.locator('.tool').count(), definitions.length)
  assert.equal(await page.locator('.tool-details').count(), 0)
  for (const [c, e] of definitions) if (e) assert.equal(await card(c.id).locator('.explanation-summary').textContent(), `执行说明：${e.purpose}`)
  assert.equal(await card('legacy').locator('.explanation-missing').textContent(), '此记录未提供执行说明')
  checks.push('all collapsed calls show their own concise public purpose, while old unexplained calls are labelled honestly')

  assert.equal(await card(alpha.id).locator('.tool-state').textContent().then(v => v.trim()), '完成')
  assert.equal(await card(beta.id).locator('.tool-state').textContent().then(v => v.trim()), '完成')
  await card(alpha.id).locator('.tool-head').click()
  await card(alpha.id).getByText('依据的已知事实', { exact: true }).waitFor()
  assert.equal(await card(alpha.id).locator('.tool-explanation').textContent().then(v => v.includes(definitions[0][1].basis)), true)
  assert.equal(await card(alpha.id).locator('.tool-parameters').textContent().then(v => v.includes(alpha.arguments.command)), true)
  assert.equal(await card(alpha.id).locator('.tool-result').textContent(), definitions[0][2])
  assert.equal(await card(alpha.id).locator('.tool-explanation').textContent().then(v => v.includes('预期影响用于说明计划')), true)
  checks.push('expanding a card shows public basis and expected effects separately from complete arguments and the actual result')
  await card(alpha.id).locator('.tool-head').click()
  await card(beta.id).locator('.tool-head').click()
  assert.equal(await card(beta.id).locator('.tool-result').textContent(), bigResult)
  assert.equal(await card(beta.id).locator('.tool-explanation').textContent().then(v => v.includes(definitions[1][1].basis)), true)
  assert.equal(await card(beta.id).locator('.tool-explanation').textContent().then(v => v.includes(definitions[0][1].basis)), false)
  assert.equal(await card(beta.id).locator('.tool-result').evaluate(node => node.scrollHeight > node.clientHeight && node.clientHeight <= 240), true)
  await card(beta.id).locator('.tool-head').click()
  assert.equal(await card(beta.id).locator('.tool-result').count(), 0)
  checks.push('reverse-order parallel completions keep purposes and results paired by call ID, and a large result stays collapsed by default')

  assert.equal(await card('legacy-approval').locator('.explanation-summary').textContent().then(v => v.includes('复用旧记录保存的操作目的')), true)
  await card('legacy-approval').locator('.tool-head').click()
  assert.equal(await card('legacy-approval').locator('.tool-parameters').textContent().then(v => v.includes('_approval')), false)
  checks.push('previous saved approval-purpose metadata is reused without exposing internal metadata as executable arguments')
  await card('legacy-approval').locator('.tool-head').click()

  assert.equal(await card('denied').locator('.tool-state').textContent().then(v => v.trim()), '被拒绝')
  assert.equal(await card('failed').locator('.tool-state').textContent().then(v => v.trim()), '失败')
  assert.equal(await card('running').locator('.tool-state').textContent().then(v => v.trim()), '执行中')
  await card('denied').locator('.tool-head').click()
  assert.equal(await card('denied').locator('.tool-result').textContent(), definitions[4][2])
  assert.equal(await card('denied').locator('.tool-explanation').textContent().then(v => v.includes('原计划执行临时验证')), true)
  await card('failed').locator('.tool-head').click()
  assert.equal(await card('failed').locator('.tool-result').textContent(), definitions[5][2])
  checks.push('denial, error and running labels remain based on actual tool results, and anticipated effects are not presented as completed work')
  await card('denied').locator('.tool-head').click(); await card('failed').locator('.tool-head').click()

  await card('html').locator('.tool-head').click()
  assert.equal(await card('html').locator('.tool-explanation img,.tool-explanation script,.tool-explanation b').count(), 0)
  assert.equal(await card('html').locator('.tool-explanation').textContent().then(v => v.includes('<script>')), true)
  assert.equal(await page.evaluate(() => !!window.fixtureInjected), false)
  assert.equal(await page.locator('.tool-explanation').allTextContents().then(v => v.some(t => t.includes(rawSecret))), false)
  checks.push('HTML-looking explanation fields render as plain text, and raw thinking or unknown reasoning fields never become tool explanations')
  await card('html').locator('.tool-head').click()

  await card('running').locator('.tool-head').click()
  assert.equal(await card('running').locator('.tool-result').textContent(), '（等待结果）')
  const completion = { seq: ++seq, type: 'tool_result', payload: { call_id: 'running', result: 'fixture live completion' } }
  sessions.get('main').history.push(completion)
  await page.evaluate(event => window.fixtureSockets.emit('main', event), completion)
  await card('running').locator('.tool-state').filter({ hasText: '完成' }).waitFor()
  assert.equal(await card('running').locator('.tool-result').textContent(), completion.payload.result)
  assert.equal(await card('running').locator('.tool-explanation').textContent().then(v => v.includes(definitions[7][1].purpose)), true)
  checks.push('a live result updates the true state without losing the public explanation registered before execution')
  await card('running').locator('.tool-head').click()

  await page.reload({ waitUntil: 'networkidle' })
  assert.equal(await page.locator('.tool').count(), definitions.length)
  assert.equal(await card('running').locator('.explanation-summary').textContent().then(v => v.includes(definitions[7][1].purpose)), true)
  assert.equal(await page.locator('.tool-details').count(), 0)
  checks.push('refresh replays saved explanations and preserves collapsed results without duplicate cards')
  await page.evaluate(() => window.fixtureSockets.disconnect('main'))
  await page.waitForFunction(() => window.fixtureSockets.opened.main >= 2)
  assert.equal(await page.locator('.tool').count(), definitions.length)
  checks.push('WebSocket reconnect keeps the same explanations and does not duplicate executions')

  await page.evaluate(event => window.fixtureSockets.emit('main', event), { seq: ++seq, type: 'session_meta', payload: { busy: true, phase: 'tools' } })
  await page.locator('#other').click()
  await page.getByText('核对另一个会话的独立目的。', { exact: false }).waitFor()
  assert.equal(await page.locator('.tool').count(), 1)
  await page.evaluate(event => window.fixtureSockets.stale('main', event), { seq: ++seq, type: 'tool_result', payload: { call_id: alpha.id, result: 'STALE_OLD_SESSION_RESULT' } })
  await page.locator('.tool-head').click()
  assert.equal(await page.locator('.tool-result').textContent(), 'OTHER_SESSION_RESULT')
  assert.equal(await page.locator('.tool-explanation').textContent().then(v => v.includes(definitions[0][1].purpose)), false)
  await page.locator('#main').click()
  await card(alpha.id).locator('.explanation-summary').waitFor()
  assert.equal(await page.locator('.tool').count(), definitions.length)
  assert.equal(await page.locator('.tool-details').count(), 0)
  checks.push('identical call IDs in different sessions stay independent; delayed old-session frames cannot overwrite current results')

  await page.locator('.list').evaluate(node => { node.scrollTop = 0 })
  await page.screenshot({ path: path.join(output, 'tool-explanations-desktop.png'), fullPage: true, animations: 'disabled' })
  await card(alpha.id).locator('.tool-head').focus()
  await page.keyboard.press('Enter')
  assert.equal(await card(alpha.id).locator('.tool-head').getAttribute('aria-expanded'), 'true')
  await page.screenshot({ path: path.join(output, 'tool-explanations-expanded.png'), fullPage: true, animations: 'disabled' })
  checks.push('the existing tool button opens its description and full output through keyboard activation with aria-expanded')

  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 })
    if (width === 390) await page.locator('#theme').click()
    await card(alpha.id).locator('.tool-head').scrollIntoViewIfNeeded()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
    assert.equal(await card(alpha.id).locator('.tool-explanation').evaluate(node => node.getBoundingClientRect().right <= innerWidth + 1 && node.getBoundingClientRect().left >= -1), true)
    assert.equal(await card(alpha.id).locator('.tool-parameters').evaluate(node => node.clientWidth <= innerWidth && node.scrollWidth <= node.clientWidth + 1), true)
    await card(alpha.id).locator('.tool-head').focus()
    assert.equal(await card(alpha.id).locator('.tool-head').evaluate(node => document.activeElement === node), true)
    await page.screenshot({ path: path.join(output, `tool-explanations-dark-${width}.png`), fullPage: true, animations: 'disabled' })
    checks.push(`${width}px dark mode retains complete explanation text, wrapped commands and keyboard access inside the viewport`)
  }
  assert.deepEqual(mutations, [])
  assert.equal(await page.evaluate(() => window.fixtureSockets.sent.every(message => message.type === 'ping')), true)
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
  checks.push('all API and session sockets were mocked; no real model, command, approval, project or Git operation occurred')
  await context.close()
} catch (error) {
  failure = error
  if (browser) for (const context of browser.contexts()) for (const page of context.pages()) await page.screenshot({ path: path.join(output, 'tool-explanations-failure.png'), fullPage: true, animations: 'disabled' }).catch(() => {})
} finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ checks, errors, unexpected, mutations, failure: failure?.message }, null, 2))
  await browser?.close()
  await vite?.close()
}
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, output }, null, 2))
