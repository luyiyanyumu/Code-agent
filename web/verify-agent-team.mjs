import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { createSopWorkflow } from './src/lib/sopWorkflow.js'

// Production components, fixture-only HTTP and WebSocket. Never execute a real model or project write.
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || '../.tmp/project-agent-team-browser')
await fs.mkdir(output, { recursive: true })
const component = await (await fetch(`${base}/src/components/code/AiPanel.vue`)).text()
const vuePath = component.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'Vite is required')
const fixture = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="fixture"></div><script type="module">
import {createApp,h,ref} from '${vuePath}'; import AiPanel from '/src/components/code/AiPanel.vue'; import SopWorkflow from '/src/components/code/SopWorkflow.vue'; import {useProjectAgentTeam} from '/src/lib/agentTeam.js'; import {api} from '/src/api.js'; import '/src/style.css';
createApp({setup(){const pid=ref('fixture-a'),tree=ref('main'),visible=ref(true),prompt=ref(''),node=ref('');const team=useProjectAgentTeam({projectId:()=>pid.value,tree:()=>tree.value,visible:()=>visible.value,leadSessionId:()=>pid.value+'-lead',client:api.code,pollMs:60000});window.changeProject=id=>pid.value=id;window.changeTree=id=>tree.value=id;window.hideTeam=()=>visible.value=false;return()=>h('div',{class:'team-fixture'},[h('main',[h(SopWorkflow,{projectId:pid.value,tree:tree.value,agents:team.team.value?.agents||[],onAsk:(text,intent)=>{team.returnLead();node.value=intent.node_id;prompt.value=text},onAgent:agent=>team.select(agent.session_id),onTeamRefresh:()=>team.refresh()})]),h('aside',[h(AiPanel,{sessionId:team.sessionId.value,team:team.team.value,agentId:team.selected.value?.session_id||'',teamLoading:team.loading.value,teamError:team.error.value,teamActing:team.acting.value,pendingPrompt:prompt.value,workflowNodeId:node.value,workflowTree:tree.value,onConsumePrompt:()=>prompt.value='',onAgentSelect:id=>{team.select(id);node.value=''},onAgentLead:()=>{team.returnLead();node.value=''},onTeamRefresh:()=>team.refresh(),onAgentStop:id=>team.action(id,'stop'),onAgentAudit:item=>team.action(item.id,item.decision,item.reason,item.activation_id),onSent:()=>node.value=''})])])}}).mount('#fixture');
</script><style>html,body,#fixture{height:100%;width:100%;margin:0;overflow:hidden}.team-fixture{display:flex;height:100%;width:100%}.team-fixture>main{flex:1;min-width:0}.team-fixture>aside{width:390px;flex:none;border-left:1px solid var(--border)}@media(max-width:650px){.team-fixture>main{display:none}.team-fixture>aside{width:100%;border:0}}</style></html>`
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const checks = [], errors = [], unexpected = [], writes = [], workflow = createSopWorkflow()
workflow.revision = 1; workflow.viewport = { x: 30, y: 50, zoom: 1 }
const roster = (pid, tree) => ({ lead: { session_id: `${pid}-lead`, project_id: pid, role: 'lead', depth: 0, title: 'Lead', state: 'waiting' }, agents: [
  { session_id: `${pid}-${tree}-child`, parent_session_id: `${pid}-${tree}-node`, lead_session_id: `${pid}-lead`, role: 'subagent', depth: 2, title: '接口验证', state: 'queued' },
  { session_id: `${pid}-${tree}-node`, parent_session_id: `${pid}-lead`, lead_session_id: `${pid}-lead`, node_id: 'requirements', role: 'node', depth: 1, title: '需求确认', state: 'completed', result: '需求清单已整理', activation_id: 1, audit: null },
], limits: { max_depth: 3, max_parallel: 2 }, running: 1, queued: 1 })
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await context.addInitScript(() => {
    window.sentPrompts = []
    window.WebSocket = class {
      static OPEN = 1
      constructor(url) {
        this.url = url; this.readyState = 1
        queueMicrotask(() => {
          this.onopen?.({})
          const sid = new URL(this.url).pathname.split('/')[4]
          this.onmessage?.({ data: JSON.stringify({ seq: 1, type: 'assistant_message', payload: { text: '对话：' + sid } }) })
          this.onmessage?.({ data: JSON.stringify({ type: 'session_meta', payload: { busy: false, model: 'fixture-offline' } }) })
        })
      }
      send(raw) { const msg = JSON.parse(raw); if (msg.type === 'prompt') window.sentPrompts.push({ url: this.url, ...msg }); queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: 'done', seq: 20 + window.sentPrompts.length, payload: { stop_reason: 'end_turn', text: '离线测试完成' } }) })) }
      close() { this.readyState = 3 }
    }
  })
  const page = await context.newPage()
  page.on('pageerror', cause => errors.push(cause.message))
  page.on('dialog', dialog => { unexpected.push('native dialog'); void dialog.dismiss() })
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== origin) { unexpected.push(url.origin + url.pathname); return route.abort() }
    if (url.pathname === '/agent-team-fixture') return route.fulfill({ contentType: 'text/html', body: fixture })
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const respond = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
    const team = url.pathname.match(/^\/api\/v1\/code\/projects\/([^/]+)\/agents$/)
    if (team && request.method() === 'GET') return respond(roster(decodeURIComponent(team[1]), url.searchParams.get('tree') || 'main'))
    if (/\/agents\/[^/]+\/(stop|audit)$/.test(url.pathname) && request.method() === 'POST') { writes.push({ path: url.pathname, body: request.postDataJSON() }); return respond({ ok: true }) }
    if (/\/workflow$/.test(url.pathname) && request.method() === 'GET') return respond(workflow)
    if (/\/events$/.test(url.pathname)) { const sid = url.pathname.split('/')[4]; return respond([{ seq: 1, type: 'assistant_message', payload: { text: '对话：' + sid } }]) }
    if (url.pathname === '/api/v1/web') return respond({ available: true, reason: '', model: 'fixture' })
    if (/\/checkpoints$/.test(url.pathname)) return respond({ busy: false })
    if (/\/approval$/.test(url.pathname) || /\/approvals\/pending$/.test(url.pathname)) return respond(null)
    unexpected.push(request.method() + ' ' + url.pathname); return route.fulfill({ status: 500, body: '{}' })
  })
  await page.goto(`${base}/agent-team-fixture`, { waitUntil: 'networkidle' })
  await page.locator('.team-trigger strong').filter({ hasText: /^Lead$/ }).waitFor()
  assert.match(await page.locator('.list').innerText(), /fixture-a-lead/)
  checks.push('project dialogue defaults to Lead')
  await page.getByRole('button', { name: '选择项目智能体' }).click()
  const ids = await page.locator('[data-agent-session]').evaluateAll(rows => rows.map(row => row.dataset.agentSession))
  assert.deepEqual(ids, ['fixture-a-lead', 'fixture-a-main-node', 'fixture-a-main-child'])
  assert.ok(await page.locator('[data-agent-session$="child"]').evaluate(node => parseInt(node.style.paddingLeft)) > await page.locator('[data-agent-session$="node"]').evaluate(node => parseInt(node.style.paddingLeft)))
  await page.screenshot({ path: `${output}/agent-team-desktop-tree.png` })
  checks.push('compact selector orders recursive worker hierarchy and displays limits/status')
  await page.locator('[data-agent-session="fixture-a-main-node"]').click()
  await page.getByRole('button', { name: '回 Lead', exact: true }).waitFor()
  await page.waitForFunction(() => document.querySelector('.list')?.textContent.includes('fixture-a-main-node'))
  assert.equal(await page.evaluate(() => window.sentPrompts.length), 0)
  checks.push('viewing worker dialogue never runs model')
  await page.getByRole('button', { name: '接受', exact: true }).click()
  assert.equal(writes[0].body.activation_id, 1)
  assert.equal(writes[0].body.decision, 'accepted')
  assert.equal(await page.locator('[data-workflow-node="requirements"]').getAttribute('class').then(c => c.includes('done')), false)
  checks.push('audit carries displayed activation and does not mark workflow phase done')
  await page.getByRole('button', { name: '回 Lead', exact: true }).click()
  await page.locator('[data-workflow-node="requirements"] .node-header').click()
  await page.getByRole('button', { name: '交给 Lead', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('textarea[aria-label="向代码助手发送消息"]')?.value.includes('需求确认'))
  assert.match(await page.locator('.team-trigger').innerText(), /Lead/)
  assert.equal(await page.evaluate(() => window.sentPrompts.length), 0)
  await page.getByRole('button', { name: '发送', exact: true }).click()
  const intent = await page.evaluate(() => window.sentPrompts[0])
  assert.match(intent.url, /fixture-a-lead\/stream/)
  assert.equal(intent.code_context.workflow_node_id, 'requirements')
  assert.equal(intent.code_context.workflow_tree_id, 'main')
  checks.push('workflow explicit submit routes validated node intent to Lead')
  await page.getByRole('button', { name: '选择项目智能体' }).click(); await page.locator('[data-agent-session="fixture-a-main-child"]').click()
  await page.getByRole('button', { name: '停止当前智能体' }).click()
  assert.ok(writes.some(write => write.path.endsWith('/fixture-a-main-child/stop')))
  checks.push('selected queued child can be stopped independently')
  await page.evaluate(() => window.changeProject('fixture-b'))
  await page.waitForFunction(() => document.querySelector('.list')?.textContent.includes('fixture-b-lead'))
  assert.match(await page.locator('.team-trigger').innerText(), /Lead/)
  assert.equal(await page.getByRole('button', { name: '回 Lead', exact: true }).count(), 0)
  checks.push('project switch clears old worker selection and reconnects new Lead')
  await page.getByRole('button', { name: '选择项目智能体' }).click(); await page.locator('[data-agent-session="fixture-b-main-node"]').click()
  await page.evaluate(() => window.changeTree('task:42'))
  await page.waitForFunction(() => document.querySelector('.team-trigger strong')?.textContent === 'Lead')
  await page.getByRole('button', { name: '选择项目智能体' }).click(); await page.locator('[data-agent-session="fixture-b-task:42-child"]').waitFor()
  checks.push('task tree uses independent node agents and returns to Lead')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: `${output}/agent-team-mobile-tree.png` })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
  checks.push('390px compact agent selector has no horizontal overflow')
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
  checks.push('no runtime errors, native dialogs or unstubbed/external requests')
} finally {
  await fs.writeFile(`${output}/report.json`, JSON.stringify({ checks, errors, unexpected, writes }, null, 2)); await browser.close()
}
console.log(JSON.stringify({ passed: checks.length, output }, null, 2))
