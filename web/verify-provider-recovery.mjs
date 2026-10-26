// Real AiPanel and useAgentSession; every execution, REST response and stream is
// synthetic. A private Vite server never forwards traffic to a real backend.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

const root = path.dirname(fileURLToPath(import.meta.url))
const output = path.resolve(process.argv[2] || path.join(root, 'shots/provider-recovery'))
const port = 5328, base = `http://127.0.0.1:${port}`
await fs.mkdir(output, { recursive: true })
const checks = [], errors = [], unexpected = [], requests = [], socketWrites = []
const providerFailure = 'provider 错误: HTTP 402: {"error":{"message":"Insufficient Balance (request_id: fixture-offline)","code":"invalid_request_error"}}'
const submitFailure = 'HTTP 503: 模拟消息提交失败，请手动重试。'
const event = (seq, type, payload = {}) => ({ seq, type, payload, ts: 1800000000 + seq })
const history = [event(1, 'user_message', { text: '离线回归：验证模型错误恢复。' }),
  event(2, 'error', { code: 'provider_error', message: providerFailure }),
  event(3, 'done', { stop_reason: 'provider_error' })]
let fixtureLoaded = false, browser, vite, failure

try {
  vite = await createServer({ configFile: false, root,
    plugins: [
      { name: 'provider-recovery-fixture-capture', enforce: 'pre',
        transform(source, id) {
          if (id.split('?')[0].replaceAll('\\', '/').endsWith('/src/composables/useAgentSession.js')) {
            assert.ok(source.includes('export function useAgentSession('), 'The fixture must wrap the actual session factory')
            fixtureLoaded = true
            return source.replace('export function useAgentSession(', 'function fixtureOriginalSession(')
              + '\nexport function useAgentSession(...args) { const session = fixtureOriginalSession(...args); window.fixtureSession = session; return session }\n'
          }
        } }, vue(),
    ], cacheDir: path.join(root, 'node_modules/.vite-provider-recovery-fixture'),
    server: { host: '127.0.0.1', port, strictPort: true,
      proxy: { '/api': { target: 'http://127.0.0.1:9', ws: true } } },
    optimizeDeps: { include: ['vue', 'marked'] }, logLevel: 'warn' })
  await vite.listen()
  const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  const { chromium } = loaded.default || loaded
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 980, height: 860 }, serviceWorkers: 'block' })
  await context.addInitScript(() => {
    const sockets = new Set(), sent = []
    window.fixtureSockets = { sent,
      emit(value) { for (const socket of sockets) if (socket.sid === 'fixture' && socket.readyState === 1) socket.onmessage?.({ data: JSON.stringify(value) }) },
      emitAll(values) { for (const value of values) this.emit(value) },
    }
    window.WebSocket = class {
      static OPEN = 1
      constructor(url) {
        this.url = url; this.readyState = 0
        this.sid = new URL(url).pathname.match(/^\/api\/v1\/sessions\/([^/]+)\/stream$/)?.[1]
        sockets.add(this)
        queueMicrotask(async () => {
          const initialHistory = this.sid ? await (await fetch('/__provider-recovery-state')).json() : []
          if (this.readyState === 3) return
          this.readyState = 1; this.onopen?.({})
          if (this.sid) {
            for (const value of initialHistory) this.onmessage?.({ data: JSON.stringify(value) })
            this.onmessage?.({ data: JSON.stringify({ type: 'session_meta', payload: { busy: false, phase: '', model: 'offline-fixture', pending_questions: [] } }) })
          }
        })
      }
      send(raw) {
        if (!this.sid) return
        const message = JSON.parse(raw); sent.push(message)
        if (message.type !== 'ping') throw new Error('The recovery fixture never sends model requests through WebSocket')
      }
      close() { this.readyState = 3; sockets.delete(this) }
    }
  })
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  page.on('pageerror', error => errors.push(error.message))
  page.on('dialog', dialog => { unexpected.push('native dialog'); void dialog.dismiss() })
  const source = await (await fetch(`${base}/src/components/code/AiPanel.vue`)).text()
  const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vuePath, 'The private Vue fixture must be compiled')
  const fixture = `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,#app{height:100%;margin:0;overflow:hidden}#fixture{height:100%;width:520px;max-width:100%;margin:auto;display:flex;border-inline:1px solid var(--border)}#fixture>.ai{flex:1;min-width:0}body{background:var(--bg)}</style></head><body><div id="app"></div><script type="module">import{createApp,h}from'${vuePath}';import Panel from'/src/components/code/AiPanel.vue';import'/src/style.css';createApp({render:()=>h('main',{id:'fixture'},[h(Panel,{sessionId:'fixture',writable:false})])}).mount('#app')</script></body></html>`
  const fulfill = (route, value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), p = url.pathname
    if (url.origin !== base) { unexpected.push(`${method} ${url.origin}${p}`); return route.abort() }
    if (p === '/__provider-recovery-fixture') return route.fulfill({ contentType: 'text/html', body: fixture })
    if (p === '/__provider-recovery-state' && method === 'GET') return fulfill(route, history)
    if (!p.startsWith('/api/')) return route.continue()
    requests.push({ method, path: p })
    if (method === 'GET' && p === '/api/v1/web') return fulfill(route, { available: false, reason: '离线浏览器回归', how_to: 'fixture only' })
    if (method === 'GET' && p === '/api/v1/sessions/fixture/events') return fulfill(route, history.filter(value => value.seq > Number(url.searchParams.get('after_seq') || 0)))
    if (method === 'GET' && p === '/api/v1/sessions/fixture/checkpoints') return fulfill(route, { busy: false })
    if (method === 'GET' && p === '/api/v1/sessions/fixture/approvals/pending') return fulfill(route, {})
    if (method === 'GET' && p === '/api/v1/sessions/fixture/questions/pending') return fulfill(route, { questions: [] })
    // This is a deliberately intercepted failed transport submission, never a
    // model call. No forwarding is permitted for any other mutation.
    if (method === 'POST' && p === '/api/v1/sessions/fixture/prompt') return fulfill(route, { detail: submitFailure }, 503)
    unexpected.push(`${method} ${p}`)
    return fulfill(route, { detail: 'Unexpected fixture request' }, 500)
  })
  const banner = page.locator('.ai > .banner')
  const badge = page.locator('.connection-status > span').nth(1)
  const emit = async value => {
    if (value.seq > 0 && !history.some(existing => existing.seq === value.seq)) history.push(value)
    await page.evaluate(value => window.fixtureSockets.emit(value), value)
  }
  const hiddenBanner = () => banner.waitFor({ state: 'detached' })
  const snapshot = () => page.evaluate(() => ({
    banner: window.fixtureSession.banner.value,
    submissionError: window.fixtureSession.submissionError.value,
    dismissed: window.fixtureSession.bannerDismissed.value,
    recoveredThroughSeq: window.fixtureSession.recoveredThroughSeq.value,
    length: window.fixtureSession.timeline.value.length,
  }))

  await page.goto(`${base}/__provider-recovery-fixture`, { waitUntil: 'networkidle' })
  await page.waitForFunction(() => window.fixtureSession && !window.fixtureSession.historyLoading.value)
  await banner.filter({ hasText: 'Insufficient Balance' }).waitFor()
  assert.equal(await badge.innerText(), '模型请求失败')
  assert.equal(await page.locator('.current-error').count(), 1)
  checks.push('persisted HTTP402 renders a current failure banner and accurate failed status')
  await page.screenshot({ path: path.join(output, 'provider-failure.png'), fullPage: true, animations: 'disabled' })

  await emit({ type: 'session_meta', payload: { busy: true, phase: 'model' } })
  await emit({ type: 'assistant_delta', payload: { text: '' } })
  assert.match(await banner.innerText(), /Insufficient Balance/)
  checks.push('busy metadata and an empty model delta do not falsely resolve the failure')
  await emit({ type: 'thinking_delta', payload: { text: '离线模拟：模型已经开始响应。' } })
  await hiddenBanner()
  assert.equal(await badge.innerText(), '模型响应中')
  assert.equal(await page.locator('details.historical-error').count(), 1)
  assert.equal(await page.locator('details.historical-error').evaluate(node => node.open), false)
  checks.push('a nonempty live model response clears the active banner and folds the historical error')
  await emit(event(4, 'assistant_message', { text: '离线模拟成功：服务恢复，错误已归入历史。' }))
  await emit(event(5, 'done', { stop_reason: 'end_turn' }))
  assert.equal(await badge.innerText(), '模型待命')
  await hiddenBanner()
  await page.screenshot({ path: path.join(output, 'provider-recovered.png'), fullPage: true, animations: 'disabled' })
  checks.push('successful terminal completion keeps the panel idle while retaining inspectable history')
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForFunction(() => window.fixtureSession && !window.fixtureSession.historyLoading.value)
  await hiddenBanner()
  assert.equal(await badge.innerText(), '模型待命')
  assert.equal(await page.locator('details.historical-error').count(), 1)
  checks.push('reloading a recovered persisted transcript does not restore its obsolete HTTP402 banner')
  await emit(event(2, 'error', { code: 'provider_error', message: providerFailure }))
  await hiddenBanner()
  assert.equal(await badge.innerText(), '模型待命')
  assert.equal(await page.locator('.current-error').count(), 0)
  checks.push('a replayed old WebSocket error cannot reactivate an already recovered failure')

  await emit(event(6, 'error', { code: 'provider_error', message: providerFailure }))
  await emit(event(7, 'done', { stop_reason: 'provider_error' }))
  await banner.waitFor()
  assert.equal(await badge.innerText(), '模型请求失败')
  assert.equal(await page.locator('.current-error').count(), 1)
  assert.equal(await page.locator('details.historical-error').count(), 1)
  checks.push('a new failure with the identical text reappears independently of the historical failure')
  await emit({ type: 'assistant_delta', payload: { text: '再次收到离线模拟响应。' } })
  await hiddenBanner()
  assert.equal(await page.locator('details.historical-error').count(), 2)
  checks.push('ordinary assistant streaming also resolves the new failure without deleting either receipt')
  await emit(event(8, 'assistant_message', { text: '再次恢复成功。' }))
  await emit(event(9, 'done', { stop_reason: 'end_turn' }))
  const noise = Array.from({ length: 420 }, (_, index) => event(10 + index, 'tool_result', { tool_call_id: `fixture-${index}`, result: 'offline fixture' }))
  history.push(...noise)
  await page.evaluate(values => window.fixtureSockets.emitAll(values), noise)
  await page.waitForFunction(() => window.fixtureSession.timeline.value.length === 400)
  assert.equal(await badge.innerText(), '模型待命')
  assert.equal((await snapshot()).recoveredThroughSeq >= 8, true)
  await hiddenBanner()
  await emit(event(6, 'error', { code: 'provider_error', message: providerFailure }))
  await hiddenBanner()
  assert.equal(await badge.innerText(), '模型待命')
  checks.push('the bounded 400-event timeline cannot resurrect an error after recovery evidence is trimmed')

  await page.evaluate(() => window.fixtureSession.closeStream())
  await page.getByLabel('向代码助手发送消息').fill('离线回归：模拟失败的 HTTP 提交。')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await banner.filter({ hasText: submitFailure }).waitFor()
  assert.equal(await badge.innerText(), '消息发送失败')
  const beforeDismiss = await snapshot()
  assert.equal(beforeDismiss.submissionError, submitFailure)
  await banner.getByRole('button', { name: '收起错误提示', exact: true }).click()
  await hiddenBanner()
  const afterDismiss = await snapshot()
  assert.equal(afterDismiss.submissionError, submitFailure)
  assert.equal(afterDismiss.dismissed, true)
  assert.equal(await badge.innerText(), '消息发送失败')
  assert.equal(afterDismiss.recoveredThroughSeq, beforeDismiss.recoveredThroughSeq)
  checks.push('manual dismissal hides only the notice; the failed HTTP submission and status remain truthful')
  await page.screenshot({ path: path.join(output, 'submission-dismissed.png'), fullPage: true, animations: 'disabled' })

  await page.getByRole('button', { name: '发送', exact: true }).click()
  await banner.filter({ hasText: submitFailure }).waitFor()
  assert.equal((await snapshot()).dismissed, false)
  assert.equal(await badge.innerText(), '消息发送失败')
  checks.push('an explicit retry that fails with the same message resets dismissal and displays the new error')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  await banner.getByRole('button', { name: '收起错误提示', exact: true }).focus()
  assert.equal(await page.evaluate(() => document.activeElement?.tagName), 'BUTTON')
  await page.screenshot({ path: path.join(output, 'provider-narrow-dark.png'), fullPage: true, animations: 'disabled' })
  checks.push('narrow dark-mode recovery UI remains inside the viewport and dismissal is keyboard accessible')
  socketWrites.push(...await page.evaluate(() => window.fixtureSockets.sent))
  assert.ok(fixtureLoaded)
  assert.equal(requests.filter(value => value.method === 'POST').length, 2)
  assert.equal(requests.filter(value => value.method !== 'GET').every(value => value.method === 'POST' && value.path === '/api/v1/sessions/fixture/prompt'), true)
  assert.equal(socketWrites.every(value => value.type === 'ping'), true)
  assert.deepEqual(errors, [])
  assert.deepEqual(unexpected, [])
  checks.push('all API/stream traffic is synthetic; no real backend, credentials, project data, approval or model request is accessed')
  await context.close()
} catch (error) {
  failure = error
  if (browser) for (const context of browser.contexts()) for (const page of context.pages()) await page.screenshot({ path: path.join(output, 'provider-recovery-failure.png'), fullPage: true, animations: 'disabled' }).catch(() => {})
} finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ checks, requests, socketWrites, errors, unexpected, failure: failure?.message }, null, 2))
  await browser?.close()
  await vite?.close()
}
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, errors: errors.length, unexpected: unexpected.length, output }, null, 2))
