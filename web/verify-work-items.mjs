// A standalone fixture with every API mocked. No real project, agent or model is used.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:5192'
const output = path.resolve(process.argv[3] || 'web/shots/work-items')
await fs.mkdir(output, { recursive: true })
const source = await (await fetch(`${base}/src/components/code/WorkItemsPanel.vue`)).text()
const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'The isolated Vite fixture must be running')
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const context = await browser.newContext({ viewport: { width: 1150, height: 970 }, serviceWorkers: 'block' })
const page = await context.newPage()
page.setDefaultTimeout(10000)
const errors = [], unexpected = [], mutations = [], checks = []
page.on('pageerror', error => errors.push(error.message))
const task = { id: 't1', title: '隔离登录页面', requirement: '实现登录', status: 'review', branch: 'ai/login', session_id: 's-task', plan: { goal: '实现登录', subtasks: [{ id: 's1', title: '页面', files: ['src/login.js'], verify: ['npm test'] }] }, review: { changed_files: 1, changes: [{ path: 'src/login.js', status: 'M' }], symbols: { counts: { added: 1, modified: 0, removed: 0 } }, gate: { ok: true, violations: [] } } }
const batch = { id: 'b1', title: '隔离并行搜索', status: 'review', plan: { goal: '前后端搜索', lanes: [{ id: 'front', files: ['src/search.js'] }, { id: 'back', files: ['api/search.py'] }] }, progress: { total: 2, merged: 0, running: 0, review: 2 }, conflicts: [], lanes: [
  { lane: 'front', task_id: 'lane-front', title: '搜索界面', status: 'review', files: ['src/search.js'], session_id: 's-front', verification: { ok: null, stale: false }, agent: { session_id: 's-front', state: 'completed', phase: 'completed' } },
  { lane: 'back', task_id: 'lane-back', title: '搜索接口', status: 'review', files: ['api/search.py'], session_id: 's-back', pending_approval: { approval_id: 'approval-back', status: 'pending', session_id: 's-back-child' }, pending_approvals: [{ approval_id: 'approval-back', status: 'pending', session_id: 's-back-child', executor_title: '接口校验' }, { approval_id: 'approval-back-2', status: 'pending', session_id: 's-back-child-2', executor_title: '编译验收' }], agent: { session_id: 's-back', state: 'waiting', phase: 'waiting_approval' } },
], waves: [['front', 'back']], queue: ['front', 'back'] }
const tasks = new Map([['t1', task]])
let failRead = false, counter = 0
const items = [
  { id: 'agent:lead', kind: 'agent', title: 'Lead 协调开发', state: 'waiting', phase: 'waiting_children', session_id: 'lead', tree_id: 'main', agent: { role: 'lead' } },
  { id: 'agent:pending', kind: 'agent', title: '后端校验', state: 'waiting', phase: 'waiting_approval', session_id: 'pending', tree_id: 'main', pending_approval: { approval_id: 'approval-1', status: 'pending' } },
  { id: 'task:t1', kind: 'task', title: task.title, state: 'review', task_id: 't1', tree_id: 'task:t1', session_id: 's-task', task, verification: { ok: true, results: [{ command: 'npm test' }] } },
  { id: 'batch:b1', kind: 'batch', title: batch.title, state: 'review', batch_id: 'b1', tree_id: 'main', batch, progress: batch.progress, verification: { ok: null, total: 2, verified: 0, failed: 0 } },
]
function synchronize() {
  for (const item of items) {
    if (item.task_id && tasks.has(item.task_id)) { item.state = tasks.get(item.task_id).status; item.task = tasks.get(item.task_id) }
    if (item.batch_id === batch.id) { item.state = batch.status; item.batch = batch }
  }
}
await context.route('**/api/**', async route => {
  const req = route.request(), url = new URL(req.url()), method = req.method(), p = decodeURIComponent(url.pathname)
  const fulfill = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
  const body = method === 'GET' ? null : req.postDataJSON() || {}
  if (method !== 'GET') mutations.push({ path: p, method, body })
  if (/\/work-items$/.test(p) && method === 'GET') {
    synchronize()
    return fulfill(failRead ? { detail: '任务读取暂时失败' } : { project_id: p.includes('/p2/') ? 'p2' : 'p1', items: p.includes('/p2/') ? [] : items, capabilities: { team: true, tasks: true, parallel: true } }, failRead ? 503 : 200)
  }
  if (/\/work-items$/.test(p) && method === 'POST') {
    const id = `new-${++counter}`
    let item
    if (body.execution_mode === 'team') item = { id: `agent:${id}`, kind: 'agent', session_id: id, title: body.requirement, tree_id: 'main', state: 'queued' }
    else if (body.execution_mode === 'isolated') {
      const value = { ...structuredClone(task), id, title: body.requirement, requirement: body.requirement, status: 'planned', branch: `ai/${id}`, plan: body.plan || task.plan }
      tasks.set(id, value)
      item = { id: `task:${id}`, kind: 'task', task_id: id, title: body.requirement, tree_id: `task:${id}`, state: 'planned', task: value }
    } else item = { id: `batch:${id}`, kind: 'batch', batch_id: 'b1', title: body.requirement, tree_id: 'main', state: 'review', batch }
    items.push(item); return fulfill({ item })
  }
  const match = p.match(/\/tasks\/([^/]+)(?:\/(plan|approve|review|verify|merge|discard))?$/)
  if (match) {
    const id = match[1], action = match[2], value = tasks.get(id) || task
    if (action === 'plan') { value.plan = body.plan; value.status = 'planned'; return fulfill(value) }
    if (action === 'approve') { value.status = 'review'; return fulfill(value) }
    if (action === 'review') return fulfill(value.review)
    if (action === 'verify') {
      const result = { ok: true, results: [{ command: 'npm test', ok: true }] }
      const lane = batch.lanes.find(l => l.task_id === id)
      if (lane) lane.verification = result
      value.review.verification = result.results
      return fulfill(result)
    }
    if (action === 'merge' || action === 'discard') { value.status = action === 'merge' ? 'merged' : 'discarded'; return fulfill({ ok: true, files: ['src/login.js'], status: value.status }) }
    return fulfill(value)
  }
  if (/\/batches\/b1$/.test(p)) return fulfill(batch)
  if (/\/batches\/b1\/plan$/.test(p)) { batch.plan = body.plan; batch.status = 'planned'; return fulfill(batch) }
  if (/\/batches\/b1\/review$/.test(p)) return fulfill({ batch, total: { added: 1, modified: 1, removed: 0 } })
  if (/\/batches\/b1\/approve$/.test(p)) { batch.status = 'review'; return fulfill({ ok: true, batch, max_concurrent: 2, deferred: [] }) }
  if (/\/batches\/b1\/merge$/.test(p)) { batch.status = 'merged'; return fulfill({ ok: true, batch, merged: ['front', 'back'], log: [] }) }
  if (/\/batches\/b1\/discard$/.test(p)) { batch.status = 'discarded'; return fulfill({ ok: true, batch }) }
  if (/\/architecture$/.test(p)) return fulfill({ rules: {}, errors: [] })
  unexpected.push(`${method} ${p}`); return fulfill({ detail: 'Unexpected fixture API' }, 404)
})
await page.route('**/__work-items-test*', route => route.fulfill({ contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,#app{height:100%!important;overflow:hidden!important}#fixture{height:100%;display:flex;flex-direction:column;width:460px;max-width:100%;margin:0 auto;border:1px solid #dde3ed}#fixture>nav{padding:8px;display:flex;gap:12px}#fixture>.work-items{flex:1}#events{display:none}</style></head><body><div id="app"></div><script type="module">import{createApp,h,ref}from'${vuePath}';import Panel from'/src/components/code/WorkItemsPanel.vue';import Host from'/src/components/ui/DialogHost.vue';import'/src/style.css';const pid=ref('p1'),visible=ref(true),events=ref([]);window.fixture={pid,visible,events};createApp({render:()=>h('main',{id:'fixture'},[h('nav',[h('button',{id:'project',onClick:()=>{pid.value=pid.value==='p1'?'p2':'p1'}},'切换项目'),h('button',{id:'visibility',onClick:()=>{visible.value=!visible.value}},'切换可见')]),h(Panel,{projectId:pid.value,visible:visible.value,onOpenTree:event=>events.value.push({type:'tree',...event}),onOpenAgent:event=>events.value.push({type:'agent',...event})}),h('pre',{id:'events'},JSON.stringify(events.value)),h(Host)])}).mount('#app')</script></body></html>` }))
let failure
try {
  await page.goto(`${base}/__work-items-test`)
  await page.getByText('Lead 协调开发', { exact: true }).waitFor()
  assert.equal(await page.locator('.items>.item').count(), 4)
  assert.equal(await page.locator('.create-form').count(), 0)
  await page.getByText('已验证 0/2 项', { exact: true }).waitFor()
  await page.screenshot({ path: path.join(output, 'work-items-desktop.png'), fullPage: true })
  checks.push('one list includes Lead, worker, isolated task and historical batch')
  await page.getByRole('button', { name: '待处理 3', exact: true }).click()
  assert.equal(await page.locator('.items>.item').count(), 3)
  await page.getByRole('searchbox', { name: '搜索任务' }).fill('后端')
  assert.equal(await page.locator('.items>.item').count(), 1)
  await page.getByRole('button', { name: '处理申请', exact: true }).click()
  assert.deepEqual(await page.evaluate(() => window.fixture.events.value.at(-1)), { type: 'agent', sessionId: 'pending', tree: 'main', approval: true })
  checks.push('approval action selects its real worker and filters compose')
  await page.getByRole('searchbox', { name: '搜索任务' }).fill('')
  await page.getByRole('button', { name: '全部 4', exact: true }).click()
  await page.getByRole('button', { name: '＋ 新建', exact: true }).click()
  await page.getByLabel('需求', { exact: true }).fill('统一需求入口')
  assert.equal(await page.getByRole('radio').count(), 3)
  await page.getByRole('button', { name: '创建任务 · 团队执行', exact: true }).click()
  assert.equal(mutations.at(-1).path.endsWith('/work-items'), true)
  assert.equal(mutations.at(-1).body.execution_mode, 'team')
  assert.equal((await page.evaluate(() => window.fixture.events.value.at(-1))).type, 'agent')
  checks.push('ordinary work creates through the single endpoint and opens the assigned session')
  await page.getByRole('button', { name: '← 所有任务', exact: true }).click()
  await page.getByRole('button', { name: '＋ 新建', exact: true }).click()
  await page.getByLabel('需求', { exact: true }).fill('隔离需求入口')
  await page.locator('input[type=radio][value=isolated]').check()
  await page.getByRole('button', { name: '创建任务 · 隔离执行', exact: true }).click()
  await page.getByRole('button', { name: '批准并执行', exact: true }).waitFor()
  assert.equal(await page.locator('.tasks>.head').count(), 0)
  assert.equal(await page.locator('.tasks>.list').count(), 0)
  await page.getByRole('button', { name: '调整计划', exact: true }).click()
  await page.getByLabel('计划 JSON', { exact: true }).fill(JSON.stringify({ goal: '调整后的登录功能', subtasks: [{ id: 's1', files: ['src/login.js'], verify: ['npm test'] }] }))
  await page.getByRole('button', { name: '保存计划', exact: true }).click()
  await page.getByText('调整后的登录功能', { exact: true }).waitFor()
  await page.getByRole('button', { name: '批准并执行', exact: true }).click()
  await page.getByRole('button', { name: '跑验证', exact: true }).click()
  await page.getByText('验证通过（1 条命令）', { exact: true }).waitFor()
  await page.getByRole('button', { name: '整体合并到主干', exact: true }).click()
  const modal = page.getByRole('dialog')
  await modal.getByRole('button', { name: '合并到主干', exact: true }).click()
  await modal.waitFor({ state: 'hidden' })
  await page.getByText('已合并', { exact: true }).first().waitFor()
  assert.equal(await page.getByRole('button', { name: '跑验证', exact: true }).isDisabled(), true)
  checks.push('isolated task plan edit, approval, verification and confirmed merge retain the full lifecycle')
  await page.getByRole('button', { name: '← 所有任务', exact: true }).click()
  await page.getByRole('button', { name: /隔离并行搜索/ }).click()
  await page.getByText('搜索界面', { exact: false }).first().waitFor()
  await page.getByRole('button', { name: '跑验证', exact: true }).first().click()
  await page.getByText('验证通过', { exact: true }).waitFor()
  await page.getByRole('combobox', { name: '选择车道审批执行者' }).selectOption('s-back-child-2')
  await page.getByRole('button', { name: '处理申请', exact: true }).click()
  assert.deepEqual(await page.evaluate(() => window.fixture.events.value.at(-1)), { type: 'agent', sessionId: 's-back-child-2', tree: 'task:lane-back', approval: true })
  await page.screenshot({ path: path.join(output, 'work-items-parallel-detail.png'), fullPage: true })
  checks.push('historical parallel detail retains waves and per-lane validation plus the matching approval session')
  await page.getByRole('button', { name: '← 所有任务', exact: true }).click()
  await page.getByRole('button', { name: '＋ 新建', exact: true }).click()
  await page.getByLabel('需求', { exact: true }).fill('并行需求入口')
  await page.locator('input[type=radio][value=parallel]').check()
  await page.getByRole('button', { name: '创建任务 · 隔离并行', exact: true }).click()
  assert.equal(mutations.at(-1).body.execution_mode, 'parallel')
  checks.push('parallel execution shares the same creation form and endpoint')
  await page.getByRole('button', { name: '← 所有任务', exact: true }).click()
  task.execution_managed = false; task.agent = null
  const legacy = items.find(item => item.id === 'task:t1')
  legacy.execution_managed = false; legacy.agent = null
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await page.getByText('历史任务会话，下一轮执行后接入团队。', { exact: true }).waitFor()
  await page.getByRole('button', { name: /隔离登录页面/ }).click()
  await page.getByText('历史任务会话，下一轮执行后接入团队。', { exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: '打开执行会话', exact: true }).isDisabled(), true)
  await page.getByRole('button', { name: '← 所有任务', exact: true }).click()
  checks.push('legacy task history stays visible and its unregistered session cannot route into Lead')
  failRead = true
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await page.getByRole('alert').waitFor()
  assert.ok(await page.locator('.items>.item').count() > 0)
  failRead = false
  await page.getByRole('button', { name: '重试', exact: true }).click()
  await page.getByRole('alert').waitFor({ state: 'hidden' })
  checks.push('a failed refresh preserves existing work and retry clears the error')
  await page.locator('#project').click()
  await page.getByText('还没有任务', { exact: true }).waitFor()
  assert.equal(await page.locator('.items>.item').count(), 0)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '新建任务', exact: true }).click()
  await page.screenshot({ path: path.join(output, 'work-items-narrow.png'), fullPage: true })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  checks.push('project changes clear old selections; the shared creation form fits 390px')
  assert.deepEqual(unexpected, [])
  assert.deepEqual(errors, [])
} catch (error) { failure = error; await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }) }
await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ ok: !failure, checks, errors, unexpected, mutations: mutations.map(item => ({ method: item.method, path: item.path, executionMode: item.body.execution_mode })) }, null, 2))
await browser.close()
if (failure) throw failure
console.log(JSON.stringify({ ok: true, checks: checks.length, output }))
