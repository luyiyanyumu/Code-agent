// Production coding panels with fixture-only HTTP/WebSocket. No real session,
// project, model, approval, file write or Git operation is accessed.
// With no URL argument this starts and closes a private Vite server on 5194.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

const root = path.dirname(fileURLToPath(import.meta.url))
let vite
const base = process.argv[2] || 'http://127.0.0.1:5194'
const origin = new URL(base).origin
const output = path.resolve(process.argv[3] || path.join(root, 'shots/code-questions'))
await fs.mkdir(output, { recursive: true })
if (!process.argv[2]) {
  vite = await createServer({ configFile: false, root, plugins: [vue()],
    cacheDir: path.join(root, 'node_modules/.vite-code-questions-fixture'),
    server: { host: '127.0.0.1', port: 5194, strictPort: true,
      proxy: { '/api': { target: 'http://127.0.0.1:9', ws: true }, '/healthz': 'http://127.0.0.1:9' } },
    optimizeDeps: { include: ['vue', 'marked'] }, logLevel: 'warn' })
  await vite.listen()
}
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
let browser, failure
const checks = [], errors = [], unexpected = [], mutations = [], socketWrites = []

const options = [{ label: '纳入本次修改（推荐）', description: '把新增测试作为交付的一部分。' }, { label: '暂时保留本地', description: '本次不纳入交付。' }]
const group = (id, sid, questions) => ({ id, question_id: id, session_id: sid, status: 'pending', questions, timeout: 600,
  ts: Date.now() / 1000, expires_at: new Date(Date.now() + 600000).toISOString() })
const main = group('g-main', 'worker', [
  { id: 'single', header: '测试交付', question: '新增测试应如何处理？', options },
  { id: 'multi', header: '验证范围', question: '需要验证哪些内容？', multi_select: true, options: [{ label: '前端交互', description: '验证按钮和问题卡。' }, { label: '后端接口', description: '验证回答确实送达提问执行者。' }, { label: '样式', description: '验证窄屏和暗色。' }] },
  { id: 'custom', header: '补充要求', question: '还有什么补充要求？' },
])
const skip = group('g-skip', 'worker', [{ id: 'skip-one', question: '可以明确跳过这道问题吗？', options: [{ label: '确认执行' }, { label: '等稍后' }] }, { id: 'skip-two', question: '第二道问题如何处理？', options: [{ label: '保留说明' }, { label: '删除说明' }] }])
const dismiss = group('g-dismiss', 'worker', [{ id: 'dismiss-one', question: '是否放弃这一整组问题？', options: [{ label: '继续执行' }, { label: '另行安排' }] }])
const second = group('g-main', 'worker-two', [{ id: 'single', question: '第二个执行者的问题', options: [{ label: '第二个执行者选项' }, { label: '暂缓' }] }])
const inactive = group('g-inactive', 'history', [{ id: 'old', question: '服务重启前留下的问题', options: [{ label: '不应自动回答' }] }])
const timeout = group('g-timeout', 'history', [{ id: 'timeout', question: '已经超时的问题', options: [{ label: '不应继续提交' }] }])
function requestEvent(g, seq) { return { seq, type: 'question_request', ts: Date.now() / 1000, payload: structuredClone(g) } }
const sessions = new Map([
  ['lead', { busy: true, phase: 'waiting_children', pending: [], history: [{ seq: 1, type: 'assistant_message', payload: { text: '执行者正在等待你确认测试交付。' } }] }],
  ['worker', { busy: true, phase: 'waiting_question', pending: [main, skip, dismiss], history: [
    { seq: 1, type: 'assistant_message', payload: { text: '请确认以下问题后继续。', tool_calls: [{ id: 'ask-tool', name: 'ask_user_question', arguments: { questions: main.questions } }] } },
    requestEvent(main, 2), requestEvent(skip, 3), requestEvent(dismiss, 4),
  ] }],
  // Its request fell outside a limited REST history page: the live waiter still
  // restores the complete group through the pending snapshot.
  ['worker-two', { busy: true, phase: 'waiting_question', pending: [second], history: [] }],
  ['history', { busy: false, phase: '', pending: [], history: [requestEvent(inactive, 1), requestEvent(timeout, 2), { seq: 3, type: 'question_answered', payload: { question_id: timeout.id, status: 'timeout', answers: [] } }, { seq: 4, type: 'done', payload: { stop_reason: 'end_turn' } }] }],
])
let failAnswerOnce = true, holdAnswer = 180
const roster = () => ({ lead: { session_id: 'lead', role: 'lead', depth: 0, title: 'Lead', state: 'waiting', phase: 'waiting_children' },
  agents: ['worker', 'worker-two'].map((id, i) => ({ session_id: id, parent_session_id: 'lead', role: 'node', depth: 1, title: i ? '第二执行者' : '测试交付', state: 'waiting', phase: 'waiting_question', tree_id: i ? 'main' : 'task:isolated', pending_questions: structuredClone(sessions.get(id).pending) })),
  running: 0, queued: 0, limits: { max_parallel: 3, max_depth: 3 } })
const workItems = () => ({ project_id: 'fixture', capabilities: { team: true, tasks: true, parallel: true }, items: [
  { id: 'agent:lead', kind: 'agent', session_id: 'lead', tree_id: 'main', title: 'Lead 的待回答工作', state: 'waiting', phase: 'waiting_children', execution_managed: true,
    agent: { role: 'lead', session_id: 'lead' }, agents: roster().agents },
] })

try {
  const source = await (await fetch(`${base}/src/components/code/AiPanel.vue`)).text()
  const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vuePath, 'The isolated Vite fixture must be running')
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1120, height: 970 }, serviceWorkers: 'block' })
  await context.addInitScript(() => {
    const sockets = new Set(), opened = {}, sent = []
    window.fixtureSockets = { opened, sent,
      emit(sid, event) { for (const socket of sockets) if (socket.sid === sid && socket.readyState === 1) socket.onmessage?.({ data: JSON.stringify(event) }) },
      disconnect(sid) { for (const socket of sockets) if (socket.sid === sid && socket.readyState === 1) { socket.readyState = 3; socket.onclose?.({ code: 1006 }) } },
    }
    window.WebSocket = class {
      static OPEN = 1
      constructor(url) {
        this.url = url; this.sid = new URL(url).pathname.match(/^\/api\/v1\/sessions\/([^/]+)\/stream$/)?.[1]; this.readyState = 0
        // Vite's development heartbeat is also offline. It is not a model/session
        // socket and must not be mistaken for an undefined execution session.
        if (!this.sid) { queueMicrotask(() => { this.readyState = 1; this.onopen?.({}) }); return }
        sockets.add(this)
        queueMicrotask(async () => {
          const state = await (await fetch('/__question-fixture/state/' + encodeURIComponent(this.sid))).json()
          if (this.readyState === 3) return
          this.readyState = 1; opened[this.sid] = (opened[this.sid] || 0) + 1; this.onopen?.({})
          this.onmessage?.({ data: JSON.stringify({ type: 'session_meta', payload: { busy: state.busy, phase: state.phase, model: 'fixture-offline', pending_questions: state.pending } }) })
        })
      }
      send(raw) { if (!this.sid) return; const message = JSON.parse(raw); sent.push({ sid: this.sid, ...message }); if (message.type !== 'ping') throw new Error('The question fixture must never send a model request') }
      close() { this.readyState = 3; sockets.delete(this) }
    }
  })
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  page.on('pageerror', error => errors.push(error.stack || error.message))
  page.on('dialog', dialog => { unexpected.push('native dialog'); void dialog.dismiss() })
  const fulfill = (route, value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
  const fixture = `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,#app{height:100%;width:100%;overflow:hidden;margin:0}#fixture{height:100%;width:480px;max-width:100%;margin:auto;display:flex;flex-direction:column;border-inline:1px solid var(--border)}#fixture>nav{flex:none;display:flex;flex-wrap:wrap;gap:5px;padding:7px;border-bottom:1px solid var(--border)}#fixture>nav button{font-size:11px;padding:4px 6px}#fixture>.ai,#fixture>.work-items{flex:1;min-height:0}</style></head><body><div id="app"></div><script type="module">
import{createApp,h,ref}from'${vuePath}';import Panel from'/src/components/code/AiPanel.vue';import Tasks from'/src/components/code/WorkItemsPanel.vue';import'/src/style.css';
const sid=ref('lead'),tab=ref('ai'),team=ref(null),events=ref([]);window.fixture={sid,tab,team,events};async function refresh(){team.value=await(await fetch('/__question-fixture/roster')).json()}function select(id){events.value.push({type:'select',sessionId:id});sid.value=id;tab.value='ai'}await refresh();createApp({render:()=>h('main',{id:'fixture'},[h('nav',[h('button',{id:'lead',onClick:()=>select('lead')},'回主会话'),h('button',{id:'worker-two',onClick:()=>select('worker-two')},'第二会话'),h('button',{id:'history',onClick:()=>select('history')},'历史问题'),h('button',{id:'tasks',onClick:()=>tab.value='tasks'},'任务入口'),h('button',{id:'theme',onClick:()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark'}},'切换主题')]),tab.value==='ai'?h(Panel,{sessionId:sid.value,team:team.value,agentId:sid.value,writable:false,onAgentSelect:select,onAgentLead:()=>select('lead'),onTeamRefresh:refresh}):h(Tasks,{projectId:'fixture',onOpenAgent:event=>{events.value.push({type:'open',...event});select(event.sessionId)}})])}).mount('#app')
</script></body></html>`
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), p = decodeURIComponent(url.pathname), method = req.method()
    if (url.origin !== origin) { unexpected.push(`${method} ${url.origin}${p}`); return route.abort() }
    if (p === '/__code-question-test') return route.fulfill({ contentType: 'text/html', body: fixture })
    if (p === '/__question-fixture/roster') return fulfill(route, roster())
    if (p.startsWith('/__question-fixture/state/')) {
      const state = sessions.get(p.split('/').at(-1))
      if (!state) unexpected.push(`unknown fixture state ${p}`)
      return fulfill(route, state || {}, state ? 200 : 404)
    }
    if (!p.startsWith('/api/')) return route.continue()
    if (p === '/api/v1/web' && method === 'GET') return fulfill(route, { available: false, reason: '全模拟验收', how_to: 'fixture only' })
    if (p === '/api/v1/code/projects/fixture/work-items' && method === 'GET') return fulfill(route, workItems())
    const match = p.match(/^\/api\/v1\/sessions\/([^/]+)\/(events|checkpoints|approvals\/pending|questions\/pending|questions\/([^/]+)\/answer)$/)
    if (match) {
      const state = sessions.get(match[1]), action = match[2]
      if (!state) { unexpected.push(`unknown session ${match[1]}`); return fulfill(route, {}, 404) }
      if (method === 'GET' && action === 'events') return fulfill(route, state.history)
      if (method === 'GET' && action === 'checkpoints') return fulfill(route, { busy: state.busy })
      if (method === 'GET' && action === 'approvals/pending') return fulfill(route, {})
      if (method === 'GET' && action === 'questions/pending') return fulfill(route, { questions: state.pending })
      if (method === 'POST' && match[3]) {
        const body = req.postDataJSON(), id = match[3]
        mutations.push({ sid: match[1], id, body })
        await new Promise(resolve => setTimeout(resolve, holdAnswer))
        if (failAnswerOnce && id === main.id) { failAnswerOnce = false; return fulfill(route, { detail: '回答提交暂时失败，请重试。' }, 503) }
        if (!state.pending.some(value => value.id === id)) return fulfill(route, { detail: '这次提问已结束。' }, 409)
        state.pending = state.pending.filter(value => value.id !== id)
        const event = { seq: Math.max(0, ...state.history.map(value => value.seq || 0)) + 1, type: 'question_answered', payload: { question_id: id, status: body.dismiss ? 'dismissed' : 'answered', answers: body.answers || [] } }
        state.history.push(event)
        await page.evaluate(({ sid, event }) => window.fixtureSockets.emit(sid, event), { sid: match[1], event })
        return fulfill(route, { ok: true })
      }
    }
    unexpected.push(`${method} ${p}`); return fulfill(route, { detail: 'Unexpected fixture API' }, 500)
  })
  const card = id => page.locator(`.question-request[data-question-id="${id}"]`)
  await page.goto(`${base}/__code-question-test`, { waitUntil: 'networkidle' })
  await page.locator('[data-question-session="worker"][data-question-id="g-main"]').click()
  await card(main.id).getByRole('heading', { name: '新增测试应如何处理？', exact: true }).waitFor()
  assert.equal(await page.locator('.question-requests > .question-request').count(), 3)
  assert.equal(await page.getByLabel('向代码助手发送消息').isVisible(), false)
  assert.equal(await page.locator('.tool-head').filter({ hasText: 'ask_user_question' }).count(), 0)
  assert.equal(mutations.length, 0)
  assert.equal(await card(main.id).locator('[aria-pressed="true"]').count(), 0)
  checks.push('team attention opens the actual executor; all groups render with no automatic choice or tool spinner')
  await page.screenshot({ path: path.join(output, 'code-questions-desktop.png'), fullPage: true, animations: 'disabled' })

  await page.reload({ waitUntil: 'networkidle' })
  await page.locator('[data-question-session="worker"][data-question-id="g-main"]').click()
  await card(main.id).waitFor()
  assert.equal(await page.locator('.question-requests > .question-request').count(), 3)
  assert.equal(mutations.length, 0)
  checks.push('history reload and the live pending snapshot restore every unanswered group')
  await card(main.id).getByRole('button', { name: '提交回答', exact: true }).click()
  await card(main.id).getByRole('alert').filter({ hasText: '请选择选项' }).waitFor()
  assert.equal(mutations.length, 0)
  checks.push('an incomplete group stays local until the user answers or explicitly skips each question')
  const singleOption = card(main.id).getByRole('button', { name: /纳入本次修改/ })
  await singleOption.focus(); await page.keyboard.press('Enter')
  assert.equal(await singleOption.getAttribute('aria-pressed'), 'true')
  await card(main.id).getByRole('button', { name: '下一题', exact: true }).click()
  await card(main.id).getByRole('button', { name: /前端交互/ }).click()
  await card(main.id).getByRole('button', { name: /后端接口/ }).click()
  await card(main.id).getByRole('textbox', { name: '回答：需要验证哪些内容？', exact: true }).fill('还需验证重新连接。')
  await card(main.id).getByRole('button', { name: '下一题', exact: true }).click()
  await card(main.id).getByRole('textbox', { name: '回答：还有什么补充要求？', exact: true }).fill('不要自动提交代码。')
  checks.push('keyboard single selection, multiple selection and a custom answer produce an explicit draft')

  await page.locator('#worker-two').click()
  await card(second.id).getByRole('heading', { name: '第二个执行者的问题', exact: true }).waitFor()
  assert.equal(await card(second.id).locator('[aria-pressed="true"]').count(), 0)
  checks.push('a live pending group outside the retained history page still renders its complete question')
  await page.locator('[data-question-session="worker"][data-question-id="g-main"]').click()
  await card(main.id).getByRole('textbox', { name: '回答：还有什么补充要求？', exact: true }).waitFor()
  assert.equal(await card(main.id).getByRole('textbox').inputValue(), '不要自动提交代码。')
  checks.push('switching sessions preserves drafts without leaking them across identical group IDs')
  const beforeReconnect = await page.evaluate(() => window.fixtureSockets.opened.worker)
  await page.evaluate(() => window.fixtureSockets.disconnect('worker'))
  await page.waitForFunction(count => window.fixtureSockets.opened.worker > count, beforeReconnect)
  assert.equal(await card(main.id).getByRole('textbox').inputValue(), '不要自动提交代码。')
  assert.equal(await page.locator('.question-requests > .question-request').count(), 3)
  checks.push('reconnect retains drafts and does not duplicate pending cards')

  await card(main.id).getByRole('button', { name: '提交回答', exact: true }).evaluate(button => { button.click(); button.click() })
  await card(main.id).getByRole('alert').filter({ hasText: '回答提交暂时失败' }).waitFor()
  assert.equal(mutations.length, 1)
  assert.equal(await card(main.id).getByRole('textbox').inputValue(), '不要自动提交代码。')
  await card(main.id).getByRole('button', { name: '提交回答', exact: true }).click()
  await card(main.id).waitFor({ state: 'detached' })
  assert.equal(mutations.length, 2)
  assert.deepEqual(mutations[1], { sid: 'worker', id: main.id, body: { answers: [
    { id: 'single', selected: ['纳入本次修改（推荐）'] },
    { id: 'multi', selected: ['前端交互', '后端接口'], custom: '还需验证重新连接。' },
    { id: 'custom', selected: [], custom: '不要自动提交代码。' },
  ] } })
  checks.push('double submission sends one request; a failed answer retains the draft and retry sends the exact choices')

  await card(skip.id).getByRole('button', { name: '跳过本题', exact: true }).click()
  await card(skip.id).getByRole('heading', { name: '第二道问题如何处理？', exact: true }).waitFor()
  await card(skip.id).getByRole('button', { name: /保留说明/ }).click()
  await card(skip.id).getByRole('button', { name: '提交回答', exact: true }).click()
  await card(skip.id).waitFor({ state: 'detached' })
  assert.deepEqual(mutations.at(-1).body, { answers: [{ id: 'skip-one', selected: [] }, { id: 'skip-two', selected: ['保留说明'] }] })
  checks.push('per-question skip survives pagination and is sent as an empty explicit answer')
  const beforeDismiss = mutations.length
  await card(dismiss.id).getByRole('button', { name: '放弃整组', exact: true }).click()
  await card(dismiss.id).getByRole('button', { name: '继续回答', exact: true }).click()
  assert.equal(mutations.length, beforeDismiss)
  await card(dismiss.id).getByRole('button', { name: '放弃整组', exact: true }).click()
  await card(dismiss.id).getByRole('button', { name: '确认放弃整组', exact: true }).click()
  await card(dismiss.id).waitFor({ state: 'detached' })
  assert.deepEqual(mutations.at(-1).body, { dismiss: true })
  checks.push('whole-group abandonment requires an explicit second action and records dismiss without invented choices')
  await page.locator('.question-history[data-question-id="g-main"] summary').click()
  await page.getByText('不要自动提交代码。', { exact: true }).waitFor()
  assert.equal(await page.locator('.question-history').count(), 3)
  checks.push('answered, skipped and dismissed questions remain readable in the conversation history')

  await page.locator('#history').click()
  await page.locator('.question-history[data-question-id="g-inactive"]').waitFor()
  assert.equal(await page.locator('.question-request').count(), 0)
  await page.getByText('已超时', { exact: true }).waitFor()
  const beforeInactive = mutations.length
  await page.locator('.question-history[data-question-id="g-inactive"] summary').click()
  await page.locator('.question-history[data-question-id="g-inactive"]').getByText(/这次提问已失效.*可以重新发消息继续/).waitFor()
  assert.equal(await page.getByRole('button', { name: '提交回答', exact: true }).count(), 0)
  assert.equal(mutations.length, beforeInactive)
  checks.push('inactive and timed-out history cannot submit an answer or strand the composer')

  await page.locator('#tasks').click()
  await page.getByRole('button', { name: '待处理 1', exact: true }).click()
  await page.getByRole('button', { name: '回答问题', exact: true }).click()
  await card(second.id).getByRole('heading', { name: '第二个执行者的问题', exact: true }).waitFor()
  const opened = await page.evaluate(() => window.fixture.events.value.findLast(value => value.type === 'open'))
  assert.equal(opened.sessionId, 'worker-two')
  assert.equal(opened.tree, 'main')
  assert.equal(opened.question, true)
  checks.push('unified task attention sends the user to the pending question executor rather than the waiting Lead')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('#theme').click()
  await card(second.id).getByRole('button', { name: /第二个执行者选项/ }).focus()
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('question-option')), true)
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  await page.screenshot({ path: path.join(output, 'code-questions-dark-narrow.png'), fullPage: true, animations: 'disabled' })
  checks.push('390px dark mode keeps the card inside the viewport and options remain keyboard focusable')
  // The live waiter can expire between rendering and an explicit click. A 409
  // must retire its actionable card; retaining the draft must never cause retry.
  const beforeExpired = mutations.length
  sessions.get('worker-two').pending = []
  await page.keyboard.press('Enter')
  await card(second.id).getByRole('textbox', { name: '回答：第二个执行者的问题', exact: true }).fill('改用独立自定义答案。')
  assert.equal(await card(second.id).locator('[aria-pressed="true"]').count(), 0)
  checks.push('a custom answer replaces a single choice visibly while multiple choices remain composable')
  await card(second.id).getByRole('button', { name: '提交回答', exact: true }).click()
  await card(second.id).waitFor({ state: 'detached' })
  await page.locator('.question-error-notice').filter({ hasText: '回答未送达' }).waitFor()
  const expiredHistory = page.locator('.question-history[data-question-id="g-main"]')
  await expiredHistory.getByText('未送出的回答草稿：改用独立自定义答案。', { exact: true }).waitFor()
  assert.equal(await expiredHistory.evaluate(node => node.open), true)
  assert.equal(mutations.length, beforeExpired + 1)
  assert.deepEqual(mutations.at(-1).body, { answers: [{ id: 'single', selected: [], custom: '改用独立自定义答案。' }] })
  assert.equal(await page.getByRole('button', { name: '提交回答', exact: true }).count(), 0)
  checks.push('a waiter that expires before answering becomes inactive after 409 without an automatic retry')
  await page.screenshot({ path: path.join(output, 'code-questions-expired-draft.png'), fullPage: true, animations: 'disabled' })
  socketWrites.push(...await page.evaluate(() => window.fixtureSockets.sent))
  assert.equal(socketWrites.some(value => value.type !== 'ping'), false)
  assert.equal(mutations.every(value => value.sid === 'worker' && ['g-main', 'g-skip', 'g-dismiss'].includes(value.id) || value.sid === 'worker-two' && value.id === 'g-main'), true)
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
  checks.push('all requests were intercepted; no prompt, model, approval, commit or real-project request occurred')
  await context.close()
} catch (error) {
  failure = error
  if (browser) for (const context of browser.contexts()) for (const page of context.pages()) await page.screenshot({ path: path.join(output, 'code-questions-failure.png'), fullPage: true, animations: 'disabled' }).catch(() => {})
} finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ checks, mutations, socketWrites, errors, unexpected, failure: failure?.message }, null, 2))
  await browser?.close()
  await vite?.close()
}
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, output }, null, 2))
