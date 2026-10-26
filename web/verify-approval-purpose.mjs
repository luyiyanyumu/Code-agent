// Actual coding AiPanel and independent-chat ApprovalRequest, with all HTTP and
// WebSocket execution traffic mocked. No real approval, model, shell or project.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'

const root = path.dirname(fileURLToPath(import.meta.url))
const base = 'http://127.0.0.1:5196', origin = new URL(base).origin
const output = path.resolve(process.argv[2] || path.join(root, 'shots/approval-purpose'))
const checks = [], errors = [], unexpected = [], decisions = [], measurements = []
await fs.mkdir(output, { recursive: true })
let vite, browser, page, releaseDecision, failure

const command = Array.from({ length: 28 }, (_, index) => `printf "离线检查 ${index}"; ls fixture/src/components/very-long-directory-${index}/node_modules/@vue/compiler-sfc/ 2>/dev/null | wc -l;`).join('\n') + '\nprintf "COMMAND_END"'
const makeApproval = id => ({
  approval_id: id, session_id: 'fixture-worker', tool: 'bash',
  purpose: '定位浏览器验收超时的原因，延长离线脚本等待时间并核对本地模块是否能加载。',
  purpose_source: 'agent',
  effects: Array.from({ length: 22 }, (_, index) => `影响 ${index + 1}：只操作独立临时验证脚本与本地测试页面，不运行真实模型，不修改用户项目。`),
  reason: '当前会话为只读模式，此操作需要单次授权。批准仅允许本次操作，会话保持只读。',
  arguments: { command, timeout: 60, marker: 'ARGS_END' },
})
const initialHistory = [{ seq: 1, type: 'assistant_message', payload: { text: '请检查这次操作的目的、影响和命令后再决定。' } }]
let pending = null, busy = false
const team = () => ({ lead: { session_id: 'fixture-lead', role: 'lead', title: 'Lead', state: 'idle', effective_permission: 'read-only' }, agents: [{ session_id: 'fixture-worker', parent_session_id: 'fixture-lead', title: '离线界面检查', role: 'node', depth: 1, state: pending ? 'waiting' : 'idle', phase: pending ? 'waiting_approval' : '', effective_permission: 'read-only', ...(pending ? { pending_approval: pending } : {}) }], limits: { max_depth: 3, max_parallel: 2 }, running: 0, queued: 0 })

async function visibleActions(label, panelSelector = '.ai') {
  const result = await page.evaluate(panelSelector => {
    const panel = document.querySelector(panelSelector), card = document.querySelector('.approval-request'), body = card.querySelector('.approval-body')
    const rect = node => { const r = node.getBoundingClientRect(); return { top: r.top, right: r.right, bottom: r.bottom, left: r.left, width: r.width, height: r.height } }
    const p = rect(panel), c = rect(card)
    return { viewport: { width: innerWidth, height: innerHeight }, panel: p, card: c,
      body: { ...rect(body), scrollTop: body.scrollTop, scrollHeight: body.scrollHeight, clientHeight: body.clientHeight },
      overflow: document.documentElement.scrollWidth > innerWidth,
      buttons: [...card.querySelectorAll('.approval-actions button')].map(button => {
        const r = rect(button), target = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return { text: button.textContent, ...r, hit: target === button || button.contains(target),
          visible: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth,
          inPanel: r.top >= p.top - 1 && r.bottom <= p.bottom + 1 && r.left >= p.left - 1 && r.right <= p.right + 1,
          inCard: r.top >= c.top - 1 && r.bottom <= c.bottom + 1 }
      }) }
  }, panelSelector)
  assert.equal(result.buttons.length, 2, `${label}: both approval actions exist`)
  for (const button of result.buttons) assert.ok(button.visible && button.hit && button.inPanel && button.inCard, `${label}: ${button.text} is visible and reachable: ${JSON.stringify(result)}`)
  assert.equal(result.overflow, false, `${label}: no horizontal page overflow`)
  assert.ok(result.body.clientHeight > 30, `${label}: body remains usable`)
  measurements.push({ label, ...result })
  return result
}

async function emitApproval(approval) {
  pending = approval; busy = true
  await page.evaluate(({ approval, team }) => { window.fixtureTeam.value = team; window.fixtureSockets.emit('fixture-worker', { type: 'approval_request', payload: approval }) }, { approval, team: team() })
  await page.getByRole('region', { name: '操作权限申请', exact: true }).waitFor()
}

try {
  vite = await createServer({ configFile: false, root, plugins: [vue()], cacheDir: path.join(root, 'node_modules/.vite-approval-purpose-fixture'),
    server: { host: '127.0.0.1', port: 5196, strictPort: true, proxy: { '/api': { target: 'http://127.0.0.1:9', ws: true }, '/healthz': 'http://127.0.0.1:9' } },
    optimizeDeps: { include: ['vue', 'marked'] }, logLevel: 'warn' })
  await vite.listen()
  const component = await (await fetch(`${base}/src/components/code/AiPanel.vue`)).text()
  const vuePath = component.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vuePath, 'Private Vite supplies the real coding component')
  const chatSource = await fs.readFile(new URL('../chat-app/src/components/ApprovalRequest.vue', import.meta.url), 'utf8')
  const chatView = await fs.readFile(new URL('../chat-app/src/views/ChatView.vue', import.meta.url), 'utf8')
  const descriptor = parse(chatSource).descriptor, scopeId = 'data-v-approval-purpose-fixture'
  const chatScript = compileScript(descriptor, { id: scopeId, inlineTemplate: true }).content.replace(/from (["'])vue\1/g, `from '${vuePath}'`)
  const chatStyle = compileStyle({ source: descriptor.styles[0].content, filename: 'ApprovalRequest.vue', id: scopeId, scoped: true }).code
  const modalRule = chatView.match(/\.modal-mask\s*\{[^}]*\}/)?.[0]
  assert.ok(modalRule, 'Use the actual independent-chat modal style')
  const fixture = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="fixture"></div><script type="module">
import {createApp,h,ref} from '${vuePath}';import AiPanel from '/src/components/code/AiPanel.vue';import '/src/style.css';
const roster=await(await fetch('/__approval-purpose/team')).json();const team=ref(roster);window.fixtureTeam=team;createApp({render:()=>h('main',{class:'fixture-main'},[h('nav',{class:'fixture-tabs'},'AI　修改　台账　任务　Git'),h(AiPanel,{sessionId:'fixture-worker',agentId:'fixture-worker',team:team.value,writable:false})])}).mount('#fixture');
</script><style>html,body,#fixture{height:100%;width:100%;margin:0;overflow:hidden}.fixture-main{height:100%;width:568px;max-width:100%;margin:auto;display:flex;flex-direction:column;border-inline:1px solid var(--border);box-sizing:border-box}.fixture-tabs{height:36px;flex:none;padding:10px 12px;box-sizing:border-box;font-size:12px;border-bottom:1px solid var(--border)}.fixture-main>.ai{flex:1;min-height:0}</style></html>`
  const chatFixture = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="fixture"></div><script type="module">
import{createApp,h,ref}from'${vuePath}';import Component from '/fixture-chat-purpose.js';import '/src/style.css';Component.__scopeId='${scopeId}';const approval=ref(${JSON.stringify(makeApproval('fixture-chat'))}),submitting=ref(false);window.chatFixture={approval,submitting,decisions:[]};createApp({render:()=>h('div',{class:'modal-mask'},[h(Component,{approval:approval.value,submitting:submitting.value,readonly:true,onDecide:value=>{if(submitting.value)return;submitting.value=true;window.chatFixture.decisions.push(value)}})])}).mount('#fixture');
</script><style>html,body,#fixture{height:100%;width:100%;margin:0;overflow:hidden}${modalRule}${chatStyle}</style></html>`
  const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  const { chromium } = loaded.default || loaded
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1400, height: 960 }, serviceWorkers: 'block' })
  await context.addInitScript(() => {
    const sockets = new Set(), sent = [], opened = {}
    window.fixtureSockets = { sent, opened,
      emit(sid, event) { for (const socket of sockets) if (socket.sid === sid && socket.readyState === 1) socket.onmessage?.({ data: JSON.stringify(event) }) },
      disconnect(sid) { for (const socket of sockets) if (socket.sid === sid && socket.readyState === 1) { socket.readyState = 3; socket.onclose?.({ code: 1006 }) } },
    }
    window.WebSocket = class {
      static OPEN = 1
      constructor(url) {
        this.url = url; this.readyState = 0; this.sid = new URL(url).pathname.match(/^\/api\/v1\/sessions\/([^/]+)\/stream$/)?.[1]
        if (!this.sid) { queueMicrotask(() => { this.readyState = 1; this.onopen?.({}) }); return }
        sockets.add(this)
        queueMicrotask(async () => {
          const state = await (await fetch('/__approval-purpose/state')).json()
          if (this.readyState === 3) return
          this.readyState = 1; opened[this.sid] = (opened[this.sid] || 0) + 1; this.onopen?.({}); this.onmessage?.({ data: JSON.stringify({ type: 'session_meta', payload: { busy: state.busy, phase: state.pending ? 'waiting_approval' : '', model: 'fixture-offline', pending_questions: [] } }) })
        })
      }
      send(raw) { if (!this.sid) return; const message = JSON.parse(raw); sent.push({ sid: this.sid, ...message }); if (message.type !== 'ping') throw new Error('Fixture must never send an execution/model request') }
      close() { this.readyState = 3; sockets.delete(this) }
    }
  })
  page = await context.newPage(); page.setDefaultTimeout(10000)
  page.on('pageerror', error => errors.push(error.stack || error.message))
  page.on('dialog', dialog => { unexpected.push('native dialog'); void dialog.dismiss() })
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), p = decodeURIComponent(url.pathname)
    const respond = value => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) })
    if (url.origin !== origin) { unexpected.push(`${method} ${url.origin}${p}`); return route.abort() }
    if (p === '/__approval-purpose/code') return route.fulfill({ contentType: 'text/html', body: fixture })
    if (p === '/__approval-purpose/chat') return route.fulfill({ contentType: 'text/html', body: chatFixture })
    if (p === '/fixture-chat-purpose.js') return route.fulfill({ contentType: 'text/javascript', body: chatScript })
    if (p === '/__approval-purpose/team') return respond(team())
    if (p === '/__approval-purpose/state') return respond({ busy, pending })
    if (!p.startsWith('/api/')) return route.continue()
    if (method === 'GET' && p === '/api/v1/web') return respond({ available: false, reason: '全模拟审批验收', how_to: 'fixture only' })
    if (method === 'GET' && p === '/api/v1/sessions/fixture-worker/events') return respond(initialHistory)
    if (method === 'GET' && p === '/api/v1/sessions/fixture-worker/checkpoints') return respond({ busy })
    if (method === 'GET' && p === '/api/v1/sessions/fixture-worker/questions/pending') return respond({ questions: [] })
    if (method === 'GET' && p === '/api/v1/sessions/fixture-worker/approvals/pending') return respond(pending)
    if (method === 'POST' && /^\/api\/v1\/approvals\/fixture-[\w-]+\/decide$/.test(p)) {
      decisions.push({ path: p, body: request.postDataJSON() })
      await new Promise(resolve => { releaseDecision = resolve })
      pending = null; busy = false
      return respond({ ok: true, delivered: true })
    }
    unexpected.push(`${method} ${p}`); return route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
  })
  await page.goto(`${base}/__approval-purpose/code`, { waitUntil: 'networkidle' })
  const composer = page.getByRole('textbox', { name: '向代码助手发送消息', includeHidden: true })
  await composer.fill('这份未发送的草稿应在审批结束后保留。')
  const approval = makeApproval('fixture-approve')
  await emitApproval(approval)
  assert.equal(await page.locator('.approval-purpose p').textContent(), approval.purpose)
  assert.equal(await page.locator('.approval-reason').textContent(), approval.reason)
  assert.equal(await page.locator('.approval-purpose .approval-reason').count(), 0)
  assert.equal(await page.locator('[aria-label="操作影响"] li').count(), approval.effects.length)
  assert.equal(await composer.isVisible(), false)
  assert.equal(decisions.length, 0)
  checks.push('actual AiPanel displays purpose before a separate impact list, policy reason and complete command; no automatic approval')
  await page.screenshot({ path: path.join(output, 'approval-purpose-desktop.png'), animations: 'disabled' })

  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('region', { name: '操作权限申请', exact: true }).waitFor()
  assert.equal(await page.locator('.approval-purpose p').textContent(), approval.purpose)
  assert.deepEqual(await page.locator('[aria-label="操作影响"] li').allTextContents(), approval.effects)
  assert.equal(await page.locator('.approval-reason').textContent(), approval.reason)
  assert.equal(decisions.length, 0)
  checks.push('refresh restores the exact persisted purpose, effects and reason from the mocked pending API')
  await page.evaluate(() => window.fixtureSockets.disconnect('fixture-worker'))
  await page.waitForFunction(() => window.fixtureSockets.opened['fixture-worker'] >= 2)
  assert.equal(await page.locator('.approval-purpose p').textContent(), approval.purpose)
  assert.deepEqual(await page.locator('[aria-label="操作影响"] li').allTextContents(), approval.effects)
  assert.equal(decisions.length, 0)
  checks.push('WebSocket reconnect and pending-GET reconciliation preserve the exact approval purpose and effects without submitting it')

  for (const viewport of [{ width: 1400, height: 960 }, { width: 1440, height: 520 }, { width: 390, height: 600 }, { width: 320, height: 760 }]) {
    await page.setViewportSize(viewport)
    await page.locator('.approval-body').evaluate(node => { node.scrollTop = 0 })
    const label = `coding ${viewport.width}x${viewport.height}`
    const before = await visibleActions(label)
    await page.locator('.approval-body').evaluate(node => { node.scrollTop = node.scrollHeight })
    const after = await visibleActions(`${label} scrolled`)
    assert.ok(after.body.scrollTop > 0)
    assert.ok(Math.abs(after.body.scrollTop + after.body.clientHeight - after.body.scrollHeight) < 3)
    const fullCommand = await page.locator('.approval-details pre').first().textContent()
    assert.equal(fullCommand, command)
    assert.match(await page.locator('.approval-details pre').last().textContent(), /ARGS_END/)
    for (let index = 0; index < 2; index++) assert.ok(Math.abs(before.buttons[index].top - after.buttons[index].top) < 2)
    checks.push(`${label}: long effects and full command remain scrollable while both actions stay reachable`)
  }
  await page.setViewportSize({ width: 390, height: 600 })
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
  await page.locator('.approval-body').evaluate(node => { node.scrollTop = 0 })
  await visibleActions('coding dark 390x600')
  await page.locator('.approval-body').focus()
  await page.keyboard.press('Tab')
  assert.ok(await page.getByRole('button', { name: '仅批准本次', exact: true }).evaluate(node => document.activeElement === node))
  await page.getByRole('button', { name: '仅批准本次', exact: true }).evaluate(node => node.blur())
  await page.waitForFunction(() => !document.querySelector('.approval-actions button')?.matches(':focus'))
  await page.locator('.approval-body').evaluate(node => { node.scrollTop = 0 })
  await page.waitForFunction(() => document.querySelector('.approval-body').scrollTop === 0)
  await page.screenshot({ path: path.join(output, 'approval-purpose-mobile-dark.png'), animations: 'disabled' })
  checks.push('coding dark narrow viewport supports keyboard focus and readable purpose without displacing actions')

  const hostile = { ...approval, purpose: '<img src=x onerror="window.fixtureInjected=true">请仅按文本展示', effects: ['<script>window.fixtureInjected=true</script>', '<b>仍是纯文本</b>'] }
  await emitApproval(hostile)
  assert.equal(await page.locator('.approval-purpose p').textContent(), hostile.purpose)
  assert.equal(await page.locator('.approval-request img,.approval-request script,.approval-request b').count(), 0)
  assert.equal(await page.evaluate(() => window.fixtureInjected), undefined)
  checks.push('purpose and effects containing HTML remain literal text and cannot execute markup')
  const missing = { ...approval, purpose: '', purpose_source: 'missing', effects: [] }
  await emitApproval(missing)
  assert.match(await page.locator('.approval-purpose p').textContent(), /尚未提供操作目的/)
  assert.match(await page.locator('[aria-label="操作影响"] p').textContent(), /尚未说明/)
  assert.notEqual(await page.locator('.approval-purpose p').textContent(), missing.reason)
  await visibleActions('coding missing purpose')
  await page.screenshot({ path: path.join(output, 'approval-purpose-missing.png'), animations: 'disabled' })
  checks.push('legacy/missing purpose is explicitly marked without inventing a goal or copying permission policy as purpose')

  await emitApproval(approval)
  await page.getByRole('button', { name: '仅批准本次', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.approval-actions button')?.disabled)
  await page.locator('.approval-actions button').evaluateAll(buttons => buttons.forEach(button => button.click()))
  assert.equal(decisions.length, 1)
  assert.deepEqual(decisions[0], { path: '/api/v1/approvals/fixture-approve/decide', body: { approved: true } })
  assert.equal(await page.locator('.approval-actions button:disabled').count(), 2)
  releaseDecision(); releaseDecision = null
  await composer.waitFor({ state: 'visible' })
  // Reload necessarily loses an unsent in-memory draft, so verify approval-only
  // transitions preserve a new draft separately from the recovery assertion.
  await composer.fill('审批期间保留这份当前草稿。')
  checks.push('approve sends only this fixture ID once; pending submission blocks both duplicate approve and reject clicks')
  await emitApproval(makeApproval('fixture-reject'))
  assert.equal(await composer.inputValue(), '审批期间保留这份当前草稿。')
  assert.match(await page.locator('.approval-scope').textContent(), /会话仍保持只读；批准只授权本次操作/)
  await page.getByRole('button', { name: '拒绝', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.approval-actions button')?.disabled)
  assert.deepEqual(decisions[1], { path: '/api/v1/approvals/fixture-reject/decide', body: { approved: false } })
  releaseDecision(); releaseDecision = null
  await composer.waitFor({ state: 'visible' })
  assert.equal(await composer.inputValue(), '审批期间保留这份当前草稿。')
  assert.deepEqual(await page.evaluate(() => window.fixtureSockets.sent), [])
  checks.push('reject sends the current fixture ID, retains composer draft and read-only scope, and sends no permission/model command')

  for (const viewport of [{ width: 1440, height: 520 }, { width: 390, height: 420 }]) {
    await page.setViewportSize(viewport)
    await page.goto(`${base}/__approval-purpose/chat`, { waitUntil: 'networkidle' })
    await page.getByRole('region', { name: '操作权限申请', exact: true }).waitFor()
    assert.equal(await page.locator('.approval-purpose p').textContent(), makeApproval('fixture-chat').purpose)
    assert.equal(await page.locator('[aria-label="操作影响"] li').count(), 22)
    assert.equal(await page.locator('.approval-reason').textContent(), makeApproval('fixture-chat').reason)
    const label = `chat ${viewport.width}x${viewport.height}`
    const before = await visibleActions(label, '.modal-mask')
    await page.locator('.approval-body').evaluate(node => { node.scrollTop = node.scrollHeight })
    const after = await visibleActions(`${label} scrolled`, '.modal-mask')
    assert.ok(after.body.scrollTop > 0)
    for (let index = 0; index < 2; index++) assert.ok(Math.abs(before.buttons[index].top - after.buttons[index].top) < 2)
    assert.equal(await page.locator('.approval-details pre').first().textContent(), command)
    checks.push(`${label}: actual independent-chat approval component separates purpose/reason and retains reachable actions with long content`)
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; window.chatFixture.approval.value = { ...window.chatFixture.approval.value, purpose: '<b>这是纯文本目的</b>', effects: ['<img src=x onerror="window.fixtureInjected=true">'] } })
  await page.locator('.approval-body').evaluate(node => { node.scrollTop = 0 })
  assert.equal(await page.locator('.approval-purpose p').textContent(), '<b>这是纯文本目的</b>')
  assert.equal(await page.locator('.approval-request img,.approval-request b').count(), 0)
  await visibleActions('chat narrow dark', '.modal-mask')
  await page.screenshot({ path: path.join(output, 'approval-purpose-chat-dark.png'), animations: 'disabled' })
  await page.getByRole('button', { name: '拒绝', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.approval-actions button')?.disabled)
  await page.locator('.approval-actions button').evaluateAll(buttons => buttons.forEach(button => button.click()))
  assert.deepEqual(await page.evaluate(() => window.chatFixture.decisions), [false])
  assert.match(await page.locator('.approval-scope').textContent(), /会话仍保持只读/)
  checks.push('independent-chat dark narrow view treats HTML as text and disabled actions prevent duplicate emitted decisions')
  await page.evaluate(() => { window.chatFixture.submitting.value = false; window.chatFixture.approval.value = { ...window.chatFixture.approval.value, purpose: '', purpose_source: 'missing', effects: [] } })
  assert.match(await page.locator('.approval-purpose p').textContent(), /尚未提供操作目的/)
  assert.match(await page.locator('[aria-label="操作影响"] p').textContent(), /尚未说明/)
  checks.push('independent-chat legacy approval receives the same honest missing-purpose and missing-effects fallback')
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
  assert.equal(decisions.length, 2)
  checks.push('all session/approval HTTP and WebSocket activity is fixture-only; no runtime errors, live backend, model or permission change')
} catch (error) {
  failure = error
  if (page) await page.screenshot({ path: path.join(output, 'failure.png'), animations: 'disabled' }).catch(() => {})
} finally {
  releaseDecision?.()
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ passed: checks.length, checks, errors, unexpected, decisions, measurements, ...(failure ? { failure: failure.stack || failure.message } : {}) }, null, 2))
  await browser?.close()
  await vite?.close()
}
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, output }, null, 2))
