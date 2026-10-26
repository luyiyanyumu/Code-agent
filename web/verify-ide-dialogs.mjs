import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Real production Vue panels and shared host; every API request is intercepted.
const module = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const playwright = module.default || module
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/ide-dialogs', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], mutations = [], errors = [], unexpected = [], nativeDialogs = [], screenshots = []
const git = { is_repo: true, branch: 'main', remote: 'origin', dirty: false, changes: [] }
const review = { changed_files: 1, changes: [{ path: 'main.py', status: 'M' }], gate: { ok: true, violations: [], counts: { warn: 0 } } }
let tasks = [
  { id: 'discard-fixture', title: '待放弃的隔离任务', status: 'review', branch: 'task/discard', plan: { goal: '验证确认与重试', subtasks: [] }, review },
  { id: 'force-fixture', title: '需要理由的审查任务', status: 'review', branch: 'task/force', plan: { goal: '验证强制理由', subtasks: [] }, review: { ...review, gate: { ok: false, violations: [{ severity: 'error', rule: 'layer', message: '检测到跨层依赖' }], counts: { warn: 0 } } } },
]
const batch = { id: 'batch-fixture', title: '并行批次示例', status: 'review', plan: { goal: '检查手机端批次确认' }, progress: { total: 1, merged: 0 }, lanes: [{ lane: 'L1', id: 'lane-fixture', title: '示例车道', status: 'review', files: ['main.py'] }], queue: ['L1'], conflicts: [], waves: [] }
let discardCalls = 0, pushCalls = 0, failure = null, page
const componentSource = await (await fetch(`${base}/src/components/code/TaskPanel.vue`)).text()
const vueUrl = componentSource.match(/from "([^"\n]*\/vue\.js[^"\n]*)"/)[1]
const fixture = `<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>IDE dialog fixture</title><div id="fixture"></div><script type="module">
import { createApp, ref, h } from '${vueUrl}';
import TaskPanel from '/src/components/code/TaskPanel.vue';
import ParallelPanel from '/src/components/code/ParallelPanel.vue';
import SourceControl from '/src/components/code/SourceControl.vue';
import DialogHost from '/src/components/ui/DialogHost.vue';
import '/src/style.css';
createApp({ setup(){ const active = ref('task'); return () => h('div',{class:'fixture'},[h('nav',[['task','任务视图'],['git','Git 视图'],['parallel','并行视图']].map(([key,label])=>h('button',{onClick:()=>active.value=key},label))),h('main',[h(active.value==='task'?TaskPanel:active.value==='git'?SourceControl:ParallelPanel,{projectId:'fixture-project'})]),h(DialogHost)]) } }).mount('#fixture');
</script><style>html,body,#fixture{margin:0;width:100%;height:100%;}.fixture{max-width:650px;margin:24px auto;display:flex;flex-direction:column;height:calc(100vh - 48px);background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden;}nav{display:flex;gap:8px;padding:12px;border-bottom:1px solid var(--border);}main{min-height:0;flex:1;overflow:hidden;}@media(max-width:500px){.fixture{margin:0;border-radius:0;height:100vh;}}</style></html>`
const browser = await playwright.chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
try {
  const context = await browser.newContext({ viewport: { width: 1160, height: 850 }, serviceWorkers: 'block' })
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname, method = request.method()
    const label = `${method} ${pathname}`
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
    if (url.origin !== origin) { unexpected.push(label); return route.abort('blockedbyclient') }
    if (pathname === '/__ide-dialog-fixture') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: fixture })
    if (!pathname.startsWith('/api/')) return route.continue()
    const suffix = pathname.replace('/api/v1/code/projects/fixture-project', '')
    if (method === 'GET') {
      if (suffix === '/tasks') return reply(tasks)
      if (suffix.startsWith('/tasks/')) { const task = tasks.find(item => item.id === suffix.split('/')[2]); if (task) return reply(task) }
      if (suffix === '/batches') return reply([batch])
      if (suffix === '/batches/batch-fixture') return reply(batch)
      if (suffix === '/git') return reply(git)
      if (suffix === '/git/branches') return reply({ branches: [{ name: 'main' }, { name: 'feature' }] })
    }
    if (method === 'POST' && suffix === '/tasks/discard-fixture/discard') {
      mutations.push({ path: suffix, body: request.postDataJSON() }); discardCalls++
      if (discardCalls === 1) return reply({ detail: 'fixture：暂时无法清理工作树，请重试' }, 409)
      tasks = tasks.filter(item => item.id !== 'discard-fixture'); return reply({ status: 'discarded' })
    }
    if (method === 'POST' && suffix === '/tasks/force-fixture/merge') {
      mutations.push({ path: suffix, body: request.postDataJSON() }); return reply({ ok: true, commit: 'fixture-commit', task: { ...tasks[0], status: 'merged' } })
    }
    if (method === 'POST' && suffix === '/git/action') {
      mutations.push({ path: suffix, body: request.postDataJSON() }); pushCalls++
      return reply(pushCalls === 1 ? { ok: false, output: 'fixture：远端拒绝推送，请重试' } : { ok: true, output: 'fixture：推送成功', status: git })
    }
    unexpected.push(label); return reply({ detail: 'Uncovered fixture route' }, 501)
  })
  page = await context.newPage(); page.setDefaultTimeout(10000)
  page.on('pageerror', error => errors.push(error.message))
  page.on('dialog', async dialog => { nativeDialogs.push(dialog.type()); await dialog.dismiss() })
  await page.goto(`${base}/__ide-dialog-fixture`, { waitUntil: 'networkidle' })
  const modal = page.locator('.app-dialog[open]')
  const snapshot = async name => { const file = `${name}.png`; await page.screenshot({ path: path.join(output, file), fullPage: true }); screenshots.push(file) }
  await page.locator('.tasks .list li').filter({ hasText: '待放弃的隔离任务' }).click()
  await page.locator('.tasks footer').getByRole('button', { name: '放弃', exact: true }).click()
  await modal.waitFor(); assert.match(await modal.innerText(), /待放弃的隔离任务/)
  assert.equal(mutations.length, 0); await modal.getByRole('button', { name: '取消', exact: true }).click()
  await modal.waitFor({ state: 'hidden' }); assert.equal(mutations.length, 0)
  checks.push('task discard cancellation makes no API mutation and shows the target task')
  await page.locator('.tasks footer').getByRole('button', { name: '放弃', exact: true }).click()
  await modal.getByRole('button', { name: '放弃任务', exact: true }).click()
  await modal.locator('.dialog-error').waitFor(); assert.match(await modal.innerText(), /暂时无法清理工作树/)
  assert.equal(discardCalls, 1); await snapshot('01-task-discard-retry')
  checks.push('task discard API failure remains in the modal with an actionable error')
  await modal.getByRole('button', { name: '放弃任务', exact: true }).click()
  await modal.waitFor({ state: 'hidden' }); assert.equal(discardCalls, 2)
  await page.locator('.tasks .list li').filter({ hasText: '待放弃的隔离任务' }).waitFor({ state: 'hidden' })
  checks.push('retry succeeds, closes the modal and refreshes the task list')
  await page.locator('.tasks .list li').filter({ hasText: '需要理由的审查任务' }).click()
  await page.locator('.tasks footer button').filter({ hasText: '强制落地' }).click()
  await modal.waitFor(); await modal.getByRole('button', { name: '强制合并', exact: true }).click()
  assert.equal(mutations.filter(item => item.path.endsWith('/merge')).length, 0)
  await modal.locator('#app-dialog-input').fill('  已核对依赖范围并获批准  ')
  await snapshot('02-force-merge-reason')
  await modal.getByRole('button', { name: '强制合并', exact: true }).click(); await modal.waitFor({ state: 'hidden' })
  assert.deepEqual(mutations.find(item => item.path.endsWith('/merge')).body, { force: true, reason: '已核对依赖范围并获批准' })
  checks.push('force merge requires a reason and sends only the explicitly entered, trimmed reason')
  await page.getByRole('button', { name: 'Git 视图', exact: true }).click()
  const push = page.locator('.scm .actions').getByRole('button', { name: '推送', exact: true })
  await push.click(); await modal.waitFor(); assert.match(await modal.innerText(), /main/)
  await modal.getByRole('button', { name: '取消', exact: true }).click(); await modal.waitFor({ state: 'hidden' }); assert.equal(pushCalls, 0)
  checks.push('Git push cancellation makes no request and identifies the captured branch')
  await push.click(); await modal.getByRole('button', { name: '推送分支', exact: true }).click()
  await modal.locator('.dialog-error').waitFor(); assert.match(await modal.innerText(), /远端拒绝推送/); assert.equal(pushCalls, 1)
  await modal.getByRole('button', { name: '推送分支', exact: true }).click(); await modal.waitFor({ state: 'hidden' })
  assert.equal(pushCalls, 2); assert.deepEqual(mutations.filter(item => item.path === '/git/action').map(item => item.body), [{ action: 'push', branch: 'main' }, { action: 'push', branch: 'main' }])
  checks.push('Git ok:false results stay in the modal and successful retry uses the original branch')
  await page.getByRole('button', { name: '并行视图', exact: true }).click()
  await page.locator('.par .list li').filter({ hasText: '并行批次示例' }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.par footer').getByRole('button', { name: '放弃整批', exact: true }).click()
  await modal.waitFor(); assert.match(await modal.innerText(), /已合并|已落地/)
  const bounds = await modal.boundingBox(); assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 391)
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  await snapshot('03-mobile-batch-discard')
  await modal.getByRole('button', { name: '取消', exact: true }).click(); await modal.waitFor({ state: 'hidden' })
  assert.equal(mutations.some(item => item.path.startsWith('/batches/')), false)
  checks.push('390px batch discard modal fits, explains partial merge consequences and cancels without mutation')
  assert.deepEqual(nativeDialogs, []); assert.deepEqual(unexpected, []); assert.deepEqual(errors, [])
  checks.push('all interactions use app dialogs with no native dialogs, runtime errors or unhandled API calls')
} catch (error) {
  failure = error.stack || String(error)
  if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
} finally {
  await browser.close()
  const report = { ok: !failure, checks, screenshots, mutations, nativeDialogs, unexpected, errors, failure }
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
  if (failure) process.exitCode = 1
}
