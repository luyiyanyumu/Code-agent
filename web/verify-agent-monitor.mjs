// Real production components; all API/session events are isolated local fixtures.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

const root = path.dirname(fileURLToPath(import.meta.url)), port = 5214
const base = `http://127.0.0.1:${port}`, output = path.resolve(process.argv[2] || path.join(root, '../.tmp/agent-monitor-browser'))
const checks = [], errors = [], unexpected = [], mutations = []
const now = new Date().toISOString()
const longScopePath = 'web/src/components/code/文件责任与并行任务协调/very-long-responsibility-path-for-the-current-conversation/AgentTeamMonitor.vue'
const common = { role: 'subagent', tree_id: 'main', activation_id: 1, effective_permission: 'workspace-write', updated_at: now, elapsed_seconds: 43, audit: { decision: 'pending', activation_id: 1 } }
const roster = {
  lead: { ...common, session_id: 'lead', role: 'lead', title: '主智能体', state: 'running', phase: 'model_streaming', depth: 0, activation_id: 8, last_task_message: '修复任务委派、并发调度和结果回传，完成回归验证后统一交付。', scope_mode: 'legacy', write_paths: ['.'], file_ownership: { held: true, waiting_for: [] } },
  agents: [
    { ...common, session_id: 'backend', parent_session_id: 'lead', depth: 1, title: '修复并发调度', state: 'waiting', phase: 'waiting_children', blocked_reason: '等待回归测试返回结果', last_task_message: '统一等待和审批恢复的入场检查，确保失败执行不继续写入。', scope_mode: 'scoped_write', write_paths: ['code_agent/agent/project_team.py'], delegation_reason: '运行时与界面可以分别实现', file_ownership: { held: true, waiting_for: [] } },
    { ...common, session_id: 'tests', parent_session_id: 'backend', depth: 2, title: '验证重入边界', state: 'running', phase: 'tools', current_tool: 'bash', last_task_message: '运行生命周期与并发重入的离线回归测试。', effective_permission: 'read-only', scope_mode: 'read_only', write_paths: [], delegation_reason: '独立核验入场规则', file_ownership: { held: false, waiting_for: [] } },
    { ...common, session_id: 'frontend', parent_session_id: 'lead', depth: 1, title: '实现多智能体协作视图', state: 'running', phase: 'tools', current_tool: 'edit_file', last_task_message: '实现委派关系图、任务状态和本轮执行证据，保持主对话输入框。', scope_mode: 'scoped_write', write_paths: [longScopePath, 'web/src/lib/'], delegation_reason: '界面展示与后端调度可独立推进', file_ownership: { held: true, waiting_for: [] } },
    { ...common, session_id: 'research', parent_session_id: 'lead', depth: 1, title: '核对历史失败用例', state: 'completed', result: '已定位验证命令的误拦截，并核对三条过时的测试断言。', audit: { decision: 'accepted', activation_id: 1 }, last_task_message: '核对原有五项失败用例，保留行为约束和回归覆盖。' },
    { ...common, session_id: 'approval', parent_session_id: 'lead', depth: 1, title: '运行构建验证', state: 'waiting', phase: 'waiting_approval', blocked_reason: '等待你批准本次构建命令', pending_approval: { approval_id: 'approval-build', session_id: 'approval', tool: 'bash', arguments: { command: 'npm run build' }, purpose: '验证前端产物能正常构建', reason: '执行命令需要单次授权', status: 'pending' }, last_task_message: '构建前端并检查产物。' },
    { ...common, session_id: 'queued', parent_session_id: 'lead', depth: 1, title: '检查交付结果', state: 'queued', phase: 'waiting_files', blocked_reason: '等待实现多智能体协作视图释放文件责任', scope_mode: 'scoped_write', write_paths: [longScopePath], delegation_reason: '在界面实现后补充独立用例', file_ownership: { held: false, waiting_for: [{ session_id: 'frontend', title: '实现多智能体协作视图', paths: [longScopePath] }] } },
    { ...common, session_id: 'dependent', parent_session_id: 'lead', depth: 1, title: '汇总回归结论', state: 'queued', phase: 'waiting_dependencies', blocked_reason: '等待验证重入边界第 1 次执行完成', effective_permission: 'read-only', scope_mode: 'read_only', write_paths: [], delegation_reason: '复用前置验证证据', depends_on: [{ session_id: 'tests', activation_id: 1 }], file_ownership: { held: false, waiting_for: [] } },
  ], limits: { max_depth: 4, max_parallel: 16 }, running: 2, queued: 1,
}
const marker = (seq, activation_id, phase) => ({ seq, type: 'session_meta', payload: { agent_activation: { activation_id, phase } } })
const evidence = [marker(1, 1, 'started'), { seq: 2, type: 'tool_use', payload: { call: { name: 'bash', arguments: { command: 'python -m pytest tests/test_team_lifecycle_regressions.py' } } } },
  { seq: 3, type: 'tool_result', payload: { name: 'bash', result: '11 passed in 1.45s\nexit_code=0' } },
  { seq: 4, type: 'done', payload: { text: '本轮回归验证结束。', stop_reason: 'end_turn' } }, marker(5, 1, 'finished'),
  marker(6, 2, 'started'), { seq: 7, type: 'done', payload: { text: '别的轮次，不应显示' } }, marker(8, 2, 'finished')]
let vite, browser, page, failure, delayEvidence = false, rejectEvidence = false, releaseEvidence
await fs.mkdir(output, { recursive: true })
try {
  vite = await createServer({ configFile: false, root, plugins: [vue()], cacheDir: path.join(root, 'node_modules/.vite-agent-monitor-fixture'),
    server: { host: '127.0.0.1', port, strictPort: true, watch: null },
    optimizeDeps: { include: ['vue', 'marked'], noDiscovery: true, entries: [] }, logLevel: 'warn' })
  await vite.listen()
  console.log('Isolated agent monitor browser checks started')
  const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  const { chromium } = loaded.default || loaded
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
  await context.addInitScript(() => {
    window.fixtureSent = []
    window.WebSocket = class {
      static OPEN = 1
      constructor(url) { this.url = url; this.readyState = 1; queueMicrotask(() => { this.onopen?.({}); this.onmessage?.({ data: JSON.stringify({ seq: 1, type: 'assistant_message', payload: { text: '离线验收：后台子任务按各自分工推进，主对话统一交付。' } }) }); this.onmessage?.({ data: JSON.stringify({ type: 'session_meta', payload: { busy: false, model: 'fixture-offline' } }) }) }) }
      send(raw) { const message = JSON.parse(raw); if (message.type !== 'ping') window.fixtureSent.push(message) }
      close() { this.readyState = 3 }
    }
  })
  const source = await (await fetch(`${base}/src/components/code/AgentTeamMonitor.vue`)).text()
  const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vuePath)
  const fixture = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="app"></div><script type="module">
import {createApp,h,ref} from '${vuePath}';import Panel from '/src/components/code/AiPanel.vue';import '/src/style.css';
const team=ref(${JSON.stringify(roster)}),sid=ref('lead'),error=ref(''),loading=ref(false);window.fixtureUpdate=(value)=>{team.value=value};window.fixtureRoot=(value)=>sid.value=value;window.fixtureError=(value)=>error.value=value;window.fixtureLoading=(value)=>loading.value=value;window.fixtureStops=[];
createApp({render:()=>h('main',{id:'fixture'},[h('div',{class:'fixture-label'},'界面离线验收 · 模拟任务数据'),h(Panel,{sessionId:sid.value,agentId:sid.value,team:team.value,teamError:error.value,teamLoading:loading.value,onAgentStop:id=>window.fixtureStops.push(id)})])}).mount('#app');
</script><style>html,body,#app{height:100%;margin:0}#fixture{width:420px;max-width:100%;height:100%;margin:auto;display:flex;flex-direction:column;border-inline:1px solid var(--border);box-sizing:border-box}.fixture-label{padding:12px;color:var(--muted);font-size:12px;background:var(--panel-soft);border-bottom:1px solid var(--border)}.ai{flex:1;min-height:0}.team-monitor .monitor-eyebrow{font-size:0}.team-monitor .monitor-eyebrow::after{content:'离线验收 · 模拟任务数据';font-size:9px}</style></html>`
  page = await context.newPage(); page.setDefaultTimeout(10000)
  page.on('pageerror', error => errors.push(error.stack || error.message))
  page.on('console', message => { if (/duplicate.*key/i.test(message.text())) errors.push(message.text()) })
  page.on('dialog', dialog => { unexpected.push('native-confirm'); void dialog.dismiss() })
  const respond = (route, value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname
    if (url.origin !== base) { unexpected.push(url.href); return route.abort() }
    if (p === '/__agent-monitor-test') return route.fulfill({ contentType: 'text/html', body: fixture })
    if (!p.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') { mutations.push(`${request.method()} ${p}`); return respond(route, {}, 500) }
    if (/\/sessions\/[^/]+\/events$/.test(p)) {
      if (p.includes('/tests/')) {
        if (delayEvidence) await new Promise(resolve => { releaseEvidence = resolve })
        if (rejectEvidence) return respond(route, { detail: '模拟执行记录读取失败' }, 503)
        return respond(route, evidence)
      }
      return respond(route, [])
    }
    if (p === '/api/v1/web') return respond(route, { available: false, reason: '离线验收' })
    if (/\/checkpoints$/.test(p)) return respond(route, { busy: false })
    if (/\/questions\/pending$/.test(p)) return respond(route, { questions: [] })
    if (/\/approvals\/pending$/.test(p)) return respond(route, {})
    unexpected.push(p); return respond(route, { detail: 'Unexpected fixture request' }, 500)
  })
  await page.goto(`${base}/__agent-monitor-test`, { waitUntil: 'networkidle' })
  const composer = page.getByRole('textbox', { name: '向代码助手发送消息' })
  await composer.fill('保留在主对话里的未发送草稿')
  await page.getByRole('button', { name: '协作视图', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '多智能体协作' })
  await dialog.waitFor()
  assert.equal(await dialog.locator('[data-monitor-agent]').count(), 8)
  assert.match(await dialog.locator('.project-limit').textContent(), /16.*4/)
  assert.equal(await page.evaluate(() => window.fixtureSent.length), 0)
  checks.push('existing AiPanel opens a read-only current-conversation tree with shared limits and all task identities')
  assert.match(await dialog.locator('.file-responsibility').textContent(), /未声明/)
  assert.match(await dialog.locator('.file-ownership').textContent(), /已持有文件责任/)
  checks.push('a legacy undeclared scope still shows actual held file ownership')
  await dialog.locator('[data-monitor-agent="backend"]').click()
  assert.match(await dialog.locator('.agent-inspector').textContent(), /等待回归测试返回结果/)
  const statusContrast = await dialog.locator('.state-pill').evaluate(node => {
    const luminance = color => {
      const values = color.match(/[\d.]+/g).slice(0, 3).map(value => {
        const channel = Number(value) / 255
        return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4
      })
      return values[0] * .2126 + values[1] * .7152 + values[2] * .0722
    }
    const foreground = luminance(getComputedStyle(node).color)
    const background = luminance(getComputedStyle(node.closest('.agent-inspector')).backgroundColor)
    return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05)
  })
  assert.ok(statusContrast >= 4.5, `Status text contrast ${statusContrast}`)
  await page.screenshot({ path: path.join(output, 'agent-monitor-desktop.png') })
  await dialog.locator('[data-monitor-agent="queued"]').click()
  assert.match(await dialog.locator('.agent-inspector .state-pill').textContent(), /等待文件释放/)
  assert.match(await dialog.locator('.file-ownership').textContent(), /等待取得文件责任.*等待 实现多智能体协作视图 释放/s)
  assert.match(await dialog.locator('.file-ownership').textContent(), /frontend/)
  assert.equal(await dialog.locator('.file-responsibility code').textContent(), longScopePath)
  await dialog.locator('.file-ownership').scrollIntoViewIfNeeded()
  await page.screenshot({ path: path.join(output, 'agent-monitor-file-ownership.png') })
  await dialog.locator('[data-monitor-agent="dependent"]').click()
  assert.match(await dialog.locator('.agent-inspector .state-pill').textContent(), /等待前置任务/)
  assert.match(await dialog.locator('.dependency-list').textContent(), /验证重入边界 · 第 1 次/)
  await dialog.locator('[data-monitor-agent="tests"]').click()
  assert.match(await dialog.locator('.file-responsibility').textContent(), /只读分析与核验/)
  assert.match(await dialog.locator('.file-ownership').textContent(), /只读，不占用写入责任/)
  assert.match(await dialog.locator('.delegation-reason').textContent(), /独立核验入场规则/)
  await dialog.locator('[data-monitor-agent="research"]').click()
  assert.match(await dialog.locator('.file-responsibility').textContent(), /未声明/)
  checks.push('declared paths, named file owner, exact dependency activation, read-only assignments and undeclared older records remain distinct')
  await dialog.getByRole('button', { name: '列表', exact: true }).click()
  await dialog.getByRole('button', { name: /^需关注/ }).click()
  assert.deepEqual(await dialog.locator('[data-monitor-agent]').evaluateAll(nodes => nodes.map(node => node.dataset.monitorAgent)), ['lead', 'approval'])
  await dialog.getByRole('button', { name: '全部任务', exact: true }).click()
  await dialog.getByRole('textbox', { name: '搜索子任务' }).fill('验证重入')
  assert.deepEqual(await dialog.locator('[data-monitor-agent]').evaluateAll(nodes => nodes.map(node => node.dataset.monitorAgent)), ['lead', 'backend', 'tests'])
  checks.push('graph/list, attention filter and task search preserve actual parent chains')
  await dialog.getByRole('textbox', { name: '搜索子任务' }).fill('VERY-LONG-RESPONSIBILITY')
  assert.deepEqual(await dialog.locator('[data-monitor-agent]').evaluateAll(nodes => nodes.map(node => node.dataset.monitorAgent)), ['lead', 'frontend', 'queued'])
  await dialog.getByRole('textbox', { name: '搜索子任务' }).fill('复用前置验证')
  assert.deepEqual(await dialog.locator('[data-monitor-agent]').evaluateAll(nodes => nodes.map(node => node.dataset.monitorAgent)), ['lead', 'dependent'])
  checks.push('path and delegation-reason searches select matching assignments')
  await dialog.getByRole('textbox', { name: '搜索子任务' }).fill('')
  await dialog.locator('[data-monitor-agent="tests"]').click()
  await dialog.getByRole('button', { name: '执行记录', exact: true }).click()
  await dialog.getByText('11 passed in 1.45s', { exact: false }).waitFor()
  assert.equal(await dialog.getByText('别的轮次，不应显示').count(), 0)
  checks.push('evidence uses exact activation markers, excluding a later activation')
  rejectEvidence = true
  await dialog.getByRole('button', { name: '刷新记录', exact: true }).click()
  await dialog.getByRole('alert').filter({ hasText: '模拟执行记录读取失败' }).waitFor()
  rejectEvidence = false
  await dialog.getByRole('button', { name: '刷新记录', exact: true }).click()
  await dialog.getByText('11 passed in 1.45s', { exact: false }).waitFor()
  checks.push('record failure is visible and can be retried without affecting the task')
  await dialog.getByRole('button', { name: '停止此任务及后代', exact: true }).click()
  assert.deepEqual(await page.evaluate(() => window.fixtureStops), ['tests'])
  const settled = { ...roster, agents: roster.agents.map(agent => agent.session_id === 'tests'
    ? { ...agent, state: 'completed', phase: 'completed', current_tool: '', result: '边界回归已通过。' } : agent) }
  await page.evaluate(value => window.fixtureUpdate(value), settled)
  await dialog.locator('.agent-inspector .state-pill').filter({ hasText: '已完成' }).waitFor()
  assert.equal(await dialog.getByRole('button', { name: '停止此任务及后代', exact: true }).count(), 0)
  assert.match(await dialog.locator('.monitor-statusbar').textContent(), /2 已完成/)
  checks.push('new server snapshots update state, counters and available actions without opening a model turn')
  await dialog.getByRole('button', { name: '任务概览', exact: true }).click()
  await dialog.locator('[data-monitor-agent="approval"]').click()
  await dialog.getByRole('button', { name: '前往处理申请', exact: true }).click()
  await dialog.waitFor({ state: 'hidden' })
  assert.match(await page.evaluate(() => document.activeElement.textContent), /子任务操作申请/)
  assert.equal(await composer.inputValue(), '保留在主对话里的未发送草稿')
  assert.equal(await page.evaluate(() => window.fixtureSent.length), 0)
  checks.push('stop targets the selected worker; pending approval returns to its real card and preserves root draft')
  await page.getByRole('button', { name: '协作视图', exact: true }).click()
  await dialog.waitFor()
  await dialog.getByRole('button', { name: '执行记录', exact: true }).focus()
  await page.keyboard.press('Tab')
  assert.equal(await dialog.locator('.identity-details > summary').evaluate(node => node === document.activeElement), true)
  await page.keyboard.press('Tab')
  assert.equal(await dialog.getByRole('button', { name: '刷新', exact: true }).evaluate(node => node === document.activeElement), true)
  await dialog.getByRole('button', { name: '执行记录', exact: true }).click()
  await dialog.getByText('未找到本轮执行边界', { exact: false }).waitFor()
  await dialog.getByRole('button', { name: '关闭协作视图', exact: true }).click()
  await page.getByRole('button', { name: '协作视图', exact: true }).click()
  assert.equal(await dialog.getByRole('button', { name: '任务概览', exact: true }).getAttribute('aria-pressed'), 'true')
  checks.push('keyboard reaches the native identity disclosure; reopening the same activation restores a nonempty overview')
  await page.keyboard.press('Tab'); assert.equal(await dialog.evaluate(node => node.contains(document.activeElement)), true)
  await page.keyboard.press('Shift+Tab'); assert.equal(await dialog.evaluate(node => node.contains(document.activeElement)), true)
  await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'hidden' })
  assert.equal(await page.getByRole('button', { name: '协作视图', exact: true }).evaluate(node => node === document.activeElement), true)
  checks.push('native modal keeps keyboard focus, Escape closes and restores trigger focus')
  const anonymous = { ...settled, agents: settled.agents.map(agent => agent.session_id === 'queued'
    ? { ...agent, blocked_reason: '等待其他对话中的执行者释放文件责任', file_ownership: { held: false, waiting_for: [
      { session_id: '', title: '其他对话中的执行者', paths: [longScopePath] },
      { session_id: '', title: '其他对话中的执行者', paths: ['web/src/lib/'] },
    ] } } : agent) }
  await page.evaluate(value => window.fixtureUpdate(value), anonymous)
  await page.getByRole('button', { name: '协作视图', exact: true }).click()
  await dialog.locator('[data-monitor-agent="queued"]').click()
  assert.equal(await dialog.locator('.ownership-holders > li').count(), 2)
  assert.equal(await dialog.locator('.ownership-holders > li > small').count(), 0)
  assert.deepEqual(await dialog.locator('.ownership-holders > li > p').allTextContents(), ['等待 其他对话中的执行者 释放', '等待 其他对话中的执行者 释放'])
  assert.deepEqual(await dialog.locator('.ownership-holders code').allTextContents(), [longScopePath, 'web/src/lib/'])
  assert.match(await dialog.locator('.file-ownership').textContent(), /等待取得文件责任/)
  await dialog.locator('.file-ownership').scrollIntoViewIfNeeded()
  await page.screenshot({ path: path.join(output, 'agent-monitor-anonymous-ownership.png') })
  await dialog.getByRole('button', { name: '关闭协作视图', exact: true }).click()
  checks.push('two anonymized cross-conversation owners render distinct rows and paths without identity leakage or duplicate keys')
  for (const width of [768, 390, 320]) {
    await page.setViewportSize({ width, height: width === 768 ? 1024 : 844 })
    await page.getByRole('button', { name: '协作视图', exact: true }).click()
    await dialog.waitFor()
    await dialog.getByRole('button', { name: '关系图', exact: true }).click()
    await dialog.locator('[data-monitor-agent="queued"]').click()
    const fit = await dialog.evaluate(node => ({ left: node.getBoundingClientRect().left, right: node.getBoundingClientRect().right,
      width: innerWidth, document: document.documentElement.scrollWidth, body: node.querySelector('.monitor-body').scrollWidth, client: node.querySelector('.monitor-body').clientWidth }))
    assert.ok(fit.left >= 0 && fit.right <= fit.width + 1 && fit.document <= fit.width + 1 && fit.body <= fit.client + 1, JSON.stringify(fit))
    const scopeFit = await dialog.locator('.file-responsibility').evaluate(node => ({ scroll: node.scrollWidth, client: node.clientWidth }))
    assert.ok(scopeFit.scroll <= scopeFit.client + 1, JSON.stringify(scopeFit))
    assert.equal(await dialog.locator('.ownership-holders > li').count(), 2)
    assert.match(await dialog.locator('.file-ownership').textContent(), /等待 其他对话中的执行者 释放/)
    if (width === 390) { await dialog.locator('.file-ownership').scrollIntoViewIfNeeded(); await page.screenshot({ path: path.join(output, 'agent-monitor-mobile.png') }) }
    await dialog.getByRole('button', { name: '关闭协作视图', exact: true }).click()
  }
  checks.push('768/390/320px fit without page overflow; only the relationship canvas scrolls horizontally')
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark')
  await page.getByRole('button', { name: '协作视图', exact: true }).click()
  await dialog.waitFor()
  await dialog.locator('[data-monitor-agent="research"]').click()
  await page.screenshot({ path: path.join(output, 'agent-monitor-dark.png') })
  await dialog.getByRole('button', { name: '关闭协作视图', exact: true }).click()
  const empty = { ...roster, agents: [] }
  await page.evaluate(value => window.fixtureUpdate(value), empty)
  await page.getByRole('button', { name: '协作视图', exact: true }).click()
  await dialog.getByText('当前由主智能体处理。', { exact: false }).waitFor()
  await dialog.getByRole('button', { name: '关闭协作视图', exact: true }).click()
  await page.evaluate(value => window.fixtureUpdate(value), roster)
  await page.getByRole('button', { name: '协作视图', exact: true }).click()
  await dialog.getByRole('button', { name: '列表', exact: true }).click()
  await dialog.locator('[data-monitor-agent="tests"]').click()
  delayEvidence = true
  await dialog.getByRole('button', { name: '执行记录', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.trace-content')?.textContent.includes('正在读取'))
  for (let attempt = 0; attempt < 100 && !releaseEvidence; attempt++) await new Promise(resolve => setTimeout(resolve, 10))
  assert.ok(releaseEvidence)
  await page.evaluate(() => window.fixtureRoot('replacement'))
  await dialog.waitFor({ state: 'hidden' })
  releaseEvidence()
  await page.waitForTimeout(100)
  assert.equal(await page.locator('.team-monitor[open]').count(), 0)
  checks.push('dark theme and empty states render; changing conversation closes the modal and ignores late evidence')
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); assert.deepEqual(mutations, [])
  checks.push('no runtime errors, unexpected API calls, model requests or live writes')
} catch (cause) {
  failure = cause.stack || String(cause)
  await page?.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {})
} finally {
  releaseEvidence?.()
  await browser?.close(); await vite?.close()
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ passed: !failure, checks, errors, unexpected, mutations, failure }, null, 2))
}
console.log(JSON.stringify({ passed: !failure, checks: checks.length, output, failure }, null, 2))
if (failure) process.exitCode = 1
