// Coding AiPanel accounting acceptance using fixture-only HTTP/WebSocket.
// No live service, project, index, model, command or approval is accessed.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

const root = path.dirname(fileURLToPath(import.meta.url))
const base = 'http://127.0.0.1:5208', origin = new URL(base).origin
const output = path.resolve(process.argv[2] || path.join(root, 'shots/agent-efficiency'))
const checks = [], errors = [], unexpected = [], mutations = []
const event = (seq, type, payload = {}) => ({ seq, type, payload })
const failed = event(4, 'session_meta', { usage_reconciliation: {
  prompt_id: 'fixture-failed', outcome: 'error',
  usage: { input_tokens: 3500, output_tokens: 170, cached_tokens: 3000 },
  delta: { input_tokens: 2500, output_tokens: 70, cached_tokens: 2200 },
} })
const cancelled = event(6, 'session_meta', { usage_reconciliation: {
  prompt_id: 'fixture-cancelled', outcome: 'cancelled',
  usage: { input_tokens: 600, output_tokens: 20, cached_tokens: 500 },
  delta: { input_tokens: 600, output_tokens: 20, cached_tokens: 500 },
} })
const done = event(8, 'done', { text: '全模拟正常收口。', stop_reason: 'end_turn',
  usage: { input_tokens: 700, output_tokens: 60, cached_tokens: 400 } })
const zero = event(9, 'session_meta', { usage_reconciliation: {
  prompt_id: 'fixture-completed', outcome: 'completed', usage: done.payload.usage,
  delta: { input_tokens: 0, output_tokens: 0, cached_tokens: 0 },
} })
const sessions = new Map([
  ['main', { busy: false, phase: '', history: [
    event(1, 'user_message', { text: '全模拟失败用量验收。' }),
    event(2, 'error', { quiet: true, code: 'segment_boundary',
      usage: { input_tokens: 1000, output_tokens: 100, cached_tokens: 800 } }),
    event(3, 'error', { message: '模拟任务预算已达到上限', code: 'execution_budget_exceeded' }),
  ] }],
  ['large', { busy: false, phase: '', history: [
    event(1, 'user_message', { text: '检查累计数的显示含义。' }),
    event(2, 'done', { text: '截图数字只作为全模拟显示数据。', stop_reason: 'end_turn',
      usage: { input_tokens: 3012728, output_tokens: 44103, cached_tokens: 2889216 } }),
  ] }],
])
let vite, browser, failure
await fs.mkdir(output, { recursive: true })
try {
  vite = await createServer({ configFile: false, root, plugins: [vue()],
    cacheDir: path.join(root, 'node_modules/.vite-agent-efficiency-fixture'),
    server: { host: '127.0.0.1', port: 5208, strictPort: true,
      proxy: { '/api': { target: 'http://127.0.0.1:9', ws: true }, '/healthz': 'http://127.0.0.1:9' } },
    optimizeDeps: { include: ['vue', 'marked'] }, logLevel: 'warn' })
  await vite.listen()
  const imported = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  const { chromium } = imported.default || imported
  const source = await (await fetch(`${base}/src/components/code/AiPanel.vue`)).text()
  const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vuePath)
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1100, height: 900 }, serviceWorkers: 'block' })
  await context.addInitScript(() => {
    const sockets = new Set(), opened = {}, sent = []
    window.fixtureSockets = { opened, sent,
      emit(sid, frame) { for (const socket of sockets) if (socket.sid === sid && socket.readyState === 1) socket.onmessage?.({ data: JSON.stringify(frame) }) },
      disconnect(sid) { for (const socket of sockets) if (socket.sid === sid && socket.readyState === 1) { socket.readyState = 3; socket.onclose?.({ code: 1006 }) } },
    }
    window.WebSocket = class {
      static OPEN = 1
      constructor(url) {
        this.url = url; this.sid = new URL(url).pathname.match(/^\/api\/v1\/sessions\/([^/]+)\/stream$/)?.[1]; this.readyState = 0
        if (!this.sid) throw new Error('Unexpected fixture WebSocket')
        sockets.add(this)
        queueMicrotask(async () => {
          const state = await (await fetch('/__efficiency-fixture/state/' + encodeURIComponent(this.sid))).json()
          if (this.readyState === 3) return
          this.readyState = 1; opened[this.sid] = (opened[this.sid] || 0) + 1; this.onopen?.({})
          const after = Number(new URL(url).searchParams.get('after') || new URL(url).searchParams.get('after_seq') || 0)
          for (const frame of state.history) if (frame.seq > after) this.onmessage?.({ data: JSON.stringify(frame) })
          this.onmessage?.({ data: JSON.stringify({ type: 'session_meta', payload: { busy: state.busy, phase: state.phase, model: 'fixture-offline', pending_questions: [] } }) })
        })
      }
      send(raw) { const message = JSON.parse(raw); sent.push({ sid: this.sid, ...message }); if (message.type !== 'ping') throw new Error('No model or command request is permitted') }
      close() { this.readyState = 3; sockets.delete(this) }
    }
  })
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  page.on('pageerror', error => errors.push(error.stack || error.message))
  page.on('dialog', dialog => { unexpected.push('native dialog'); void dialog.dismiss() })
  const fulfill = (route, value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
  const fixture = `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,#app{height:100%;width:100%;overflow:hidden;margin:0}#fixture{height:100%;width:560px;max-width:100%;margin:auto;display:flex;flex-direction:column;border-inline:1px solid var(--border);box-sizing:border-box}#fixture>nav{flex:none;display:flex;flex-wrap:wrap;gap:6px;padding:7px;border-bottom:1px solid var(--border)}#fixture>nav button{font-size:12px;padding:4px 6px}#fixture>.ai{flex:1;min-height:0}</style></head><body><div id="app"></div><script type="module">
import{createApp,h,ref}from'${vuePath}';import Panel from'/src/components/code/AiPanel.vue';import'/src/style.css';const sid=ref('main');createApp({render:()=>h('main',{id:'fixture'},[h('nav',[h('button',{id:'main',onClick:()=>sid.value='main'},'模拟结算'),h('button',{id:'large',onClick:()=>sid.value='large'},'累计大数字'),h('button',{id:'theme',onClick:()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark'},'切换主题')]),h(Panel,{sessionId:sid.value,agentId:sid.value,writable:false})])}).mount('#app')
</script></body></html>`
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), p = decodeURIComponent(url.pathname), method = request.method()
    if (url.origin !== origin) { unexpected.push(`${method} ${url.origin}${p}`); return route.abort() }
    if (p === '/__agent-efficiency-test') return route.fulfill({ contentType: 'text/html', body: fixture })
    if (p.startsWith('/__efficiency-fixture/state/')) return fulfill(route, sessions.get(p.split('/').at(-1)) || {})
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
  const usage = page.locator('.session-usage')
  const emit = async frame => { sessions.get('main').history.push(frame); await page.evaluate(e => window.fixtureSockets.emit('main', e), frame) }
  const snapshot = () => page.evaluate(() => window.fixtureSockets.emit('main', { seq: 0, type: 'session_meta', payload: { busy: false, phase: '' } }))
  await page.goto(`${base}/__agent-efficiency-test`, { waitUntil: 'networkidle' })
  await usage.filter({ hasText: '会话累计输入 1,000' }).waitFor()
  await emit(failed)
  await usage.filter({ hasText: '会话累计输入 3,500' }).waitFor()
  await page.evaluate(e => { window.fixtureSockets.emit('main', e); window.fixtureSockets.emit('main', e) }, failed)
  assert.equal(await usage.textContent(), '会话累计输入 3,500（缓存 3,000） · 输出 170')
  checks.push('failed prompt adds only its unpublished reconciliation delta once, after an already counted segment')

  await emit(event(5, 'user_message', { text: '全模拟取消用量验收。' }))
  await emit(cancelled); await snapshot()
  await usage.filter({ hasText: '会话累计输入 4,100' }).waitFor()
  await page.evaluate(e => window.fixtureSockets.emit('main', e), cancelled)
  assert.equal(await usage.textContent(), '会话累计输入 4,100（缓存 3,500） · 输出 190')
  assert.equal(await page.getByRole('button', { name: '停止', exact: true }).count(), 0)
  checks.push('cancelled prompt usage remains visible, is counted once and settles the visible busy state without a model request')

  await emit(event(7, 'user_message', { text: '全模拟正常收口用量验收。' }))
  await emit(done); await emit(zero); await snapshot()
  await usage.filter({ hasText: '会话累计输入 4,800' }).waitFor()
  assert.equal(await usage.textContent(), '会话累计输入 4,800（缓存 3,900） · 输出 250')
  checks.push('normal done usage is counted once; a zero reconciliation delta does not add the entire prompt usage again')

  await page.reload({ waitUntil: 'networkidle' })
  assert.equal(await usage.textContent(), '会话累计输入 4,800（缓存 3,900） · 输出 250')
  assert.equal(await page.locator('.bubble.user').count(), 3)
  await page.evaluate(() => window.fixtureSockets.disconnect('main'))
  await page.waitForFunction(() => window.fixtureSockets.opened.main >= 2)
  await page.evaluate(e => window.fixtureSockets.emit('main', e), failed)
  assert.equal(await usage.textContent(), '会话累计输入 4,800（缓存 3,900） · 输出 250')
  assert.equal(await page.locator('.bubble.user').count(), 3)
  checks.push('REST plus WebSocket replay, refresh, reconnect and repeated old settlement frames do not duplicate usage or messages')

  assert.match(await usage.getAttribute('title'), /未缓存输入 900/)
  assert.match(await usage.getAttribute('title'), /含已结算的失败与取消/)
  assert.match(await usage.getAttribute('title'), /缓存已包含在输入中，不额外相加/)
  assert.match(await usage.getAttribute('title'), /此处不是费用金额/)
  await page.screenshot({ path: path.join(output, 'agent-efficiency-desktop.png'), fullPage: true, animations: 'disabled' })
  checks.push('the cumulative label distinguishes session usage from task budget; its tooltip explains uncached input, cache inclusion and failed/cancelled usage')

  await page.locator('#large').click()
  await usage.filter({ hasText: '会话累计输入 3,012,728' }).waitFor()
  assert.match(await usage.getAttribute('title'), /未缓存输入 123,512/)
  await page.setViewportSize({ width: 320, height: 844 })
  await page.locator('#theme').click()
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  assert.equal(await usage.evaluate(node => node.scrollWidth <= node.clientWidth + 1 && node.getBoundingClientRect().right <= innerWidth + 1), true)
  assert.equal(await usage.textContent(), '会话累计输入 3,012,728（缓存 2,889,216） · 输出 44,103')
  await page.screenshot({ path: path.join(output, 'agent-efficiency-dark-320.png'), fullPage: true, animations: 'disabled' })
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); assert.deepEqual(mutations, [])
  assert.equal(await page.evaluate(() => window.fixtureSockets.sent.every(message => message.type === 'ping')), true)
  checks.push('320px dark mode wraps the large cumulative label within the viewport; every HTTP/WS operation was mocked and no real service or mutation ran')
  await context.close()
} catch (error) {
  failure = error
  if (browser) for (const context of browser.contexts()) for (const page of context.pages()) await page.screenshot({ path: path.join(output, 'agent-efficiency-failure.png'), fullPage: true, animations: 'disabled' }).catch(() => {})
} finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ checks, errors, unexpected, mutations, failure: failure?.message }, null, 2))
  await browser?.close()
  await vite?.close()
}
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, output }, null, 2))
