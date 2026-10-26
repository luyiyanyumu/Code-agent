// All APIs are intercepted. This check never reads or changes real user memories.
// node web/verify-memory.mjs http://127.0.0.1:5175 web/shots/memory
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:5175'
const output = path.resolve(process.argv[3] || 'web/shots/memory')
await fs.mkdir(output, { recursive: true })
const source = await (await fetch(`${base}/src/views/MemoryView.vue`)).text()
const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'Vite must be running')
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const context = await browser.newContext({ viewport: { width: 1360, height: 1040 }, serviceWorkers: 'block' })
const page = await context.newPage()
page.setDefaultTimeout(10000)
const errors = [], unexpected = [], checks = [], mutations = []
page.on('pageerror', error => errors.push(error.message))
let failRead = false
const scopes = [
  { id: 'user', kind: 'user', title: '个人记忆', revision: 1, records: [
    { id: 'u1', content: '回答先给结论，再解释依据。', category: 'preference', source: 'manual', enabled: true, pinned: false, created_at: '2026-10-11T08:00:00Z', updated_at: '2026-10-11T08:00:00Z' },
    { id: 'u2', content: '优先使用中文，必要时保留技术术语。', category: 'preference', source: 'agent', enabled: true, pinned: true, created_at: '2026-10-11T09:00:00Z', updated_at: '2026-10-11T09:00:00Z' },
  ] },
  { id: 'project:p1', kind: 'project', title: '学习平台', project_id: 'p1', revision: 1, records: [
    { id: 'p1', content: '统一使用 Java 21。\n提交前运行后端编译与前端构建。', category: 'convention', source: 'manual', enabled: false, pinned: false, created_at: '2026-10-11T10:00:00Z', updated_at: '2026-10-11T10:00:00Z' },
  ] },
  { id: 'session:s1', kind: 'session', title: '登录功能调试', session_id: 's1', revision: 0, records: [] },
]
await context.route('**/api/**', async route => {
  const req = route.request(), url = new URL(req.url()), p = decodeURIComponent(url.pathname), method = req.method()
  if (!p.startsWith('/api/v1/memory')) { unexpected.push(`${method} ${p}`); return route.fulfill({ status: 404, body: '{}' }) }
  if (method === 'GET') return route.fulfill({ status: failRead ? 503 : 200, contentType: 'application/json', body: JSON.stringify(failRead ? { detail: '记忆读取暂时失败，请重试。' } : { scopes, user: { entries: [] }, sessions: [] }) })
  const body = req.postDataJSON() || {}
  mutations.push({ method, body, query: Object.fromEntries(url.searchParams) })
  const scope = scopes.find(item => item.id === (body.scope_id || url.searchParams.get('scope_id')))
  if (!scope) return route.fulfill({ status: 404, body: '{}' })
  const revision = body.revision ?? Number(url.searchParams.get('revision'))
  if (Number(revision) !== scope.revision) return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'scope updated' }) })
  if (method === 'POST') scope.records.push({ id: `new-${scope.revision}`, content: body.content, category: body.category, source: 'manual', enabled: true, pinned: !!body.pinned, updated_at: '2026-10-12T08:00:00Z' })
  else if (method === 'PATCH') Object.assign(scope.records.find(item => item.id === p.split('/').at(-1)), body, { updated_at: '2026-10-12T08:00:00Z' })
  else if (method === 'DELETE') scope.records = scope.records.filter(item => item.id !== url.searchParams.get('entry_id'))
  scope.revision++
  return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, revision: scope.revision, records: scope.records }) })
})
await page.route('**/__memory-test*', route => route.fulfill({ contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,#app{height:auto!important;overflow:auto!important}#fixture{max-width:1050px;margin:0 auto;padding:28px}.memory-page{padding:0!important;overflow:visible!important}.page-header{display:none!important}@media(max-width:700px){#fixture{padding:12px}}</style></head><body><div id="app"></div><script type="module">import{createApp,h,ref}from'${vuePath}';import Memory from'/src/views/MemoryView.vue';import Host from'/src/components/ui/DialogHost.vue';import'/src/style.css';const visible=ref(true),panel=ref();createApp({render:()=>h('main',{id:'fixture'},[h('button',{id:'toggle',onClick:async()=>{visible.value=!visible.value;if(visible.value)await panel.value.reload()}},visible.value?'切换设置':'返回记忆'),h('section',{style:visible.value?'':'display:none'},[h(Memory,{ref:panel})]),h(Host)])}).mount('#app')</script></body></html>` }))
const modal = page.getByRole('dialog')
let failure
try {
  await page.goto(`${base}/__memory-test`)
  await page.getByText('回答先给结论，再解释依据。', { exact: true }).waitFor()
  assert.equal(await page.locator('.facts li').count(), 3)
  assert.equal(await page.locator('.memory-stats strong').first().innerText(), '2')
  checks.push('scoped counts and metadata render')
  await page.getByRole('button', { name: '项目 1', exact: true }).click()
  assert.equal(await page.locator('.facts li').count(), 1)
  await page.getByRole('searchbox', { name: '搜索记忆' }).fill('Java')
  assert.equal(await page.locator('.facts li').count(), 1)
  await page.getByRole('combobox', { name: '筛选启用状态' }).selectOption('enabled')
  await page.getByText('没有匹配的记忆', { exact: true }).waitFor()
  await page.getByRole('button', { name: '清除筛选' }).click()
  checks.push('scope, keyword, enabled filters compose and reset')
  await page.getByRole('button', { name: '添加记忆', exact: true }).click()
  await page.getByLabel('使用范围', { exact: true }).selectOption('project:p1')
  await page.getByLabel('分类', { exact: true }).selectOption('decision')
  await page.getByLabel('记忆内容', { exact: true }).fill('登录使用短期访问令牌。\n刷新令牌仅保存到安全 Cookie。')
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  await page.getByText('登录使用短期访问令牌。\n刷新令牌仅保存到安全 Cookie。', { exact: true }).waitFor()
  checks.push('multiline project add saves with category')
  const p1 = scopes[1].records[0]
  await page.getByRole('button', { name: `启用记忆：${p1.content}`, exact: true }).click()
  await page.getByRole('button', { name: `停用记忆：${p1.content}`, exact: true }).waitFor()
  await page.getByRole('button', { name: `置顶记忆：${p1.content}`, exact: true }).click()
  await page.getByRole('button', { name: `取消置顶记忆：${p1.content}`, exact: true }).waitFor()
  checks.push('enable and pin operations immediately reflect')
  await page.getByRole('button', { name: `编辑记忆：${p1.content}`, exact: true }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('统一使用 Java 21，提交前运行编译与构建。')
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  await page.getByText('统一使用 Java 21，提交前运行编译与构建。', { exact: true }).waitFor()
  checks.push('record edit retains scope identity')
  await page.screenshot({ path: path.join(output, 'memory-desktop.png'), fullPage: true })
  await page.getByRole('button', { name: '编辑记忆：回答先给结论，再解释依据。', exact: true }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('先给可执行结论，再解释依据。')
  scopes[0].revision++; scopes[0].records[0].content = '先说明执行结果。'
  await page.getByRole('button', { name: '刷新记忆', exact: true }).click()
  await page.locator('.memory-editor .feedback').filter({ hasText: '保存前请核对最新版本' }).waitFor()
  assert.equal(await page.getByRole('button', { name: '保存记忆', exact: true }).isDisabled(), true)
  await page.getByRole('button', { name: '核对最新版本', exact: true }).click()
  await modal.getByRole('button', { name: '以此版本继续编辑' }).click()
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  await page.getByText('先给可执行结论，再解释依据。', { exact: true }).waitFor()
  checks.push('concurrent change preserves draft and requires review')
  await page.getByRole('button', { name: '删除记忆：先给可执行结论，再解释依据。', exact: true }).click()
  const beforeCancel = mutations.length
  await modal.getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(mutations.length, beforeCancel)
  await page.getByRole('button', { name: '删除记忆：先给可执行结论，再解释依据。', exact: true }).click()
  await modal.getByRole('button', { name: '删除记忆', exact: true }).click()
  await page.getByLabel('记忆列表').getByText('先给可执行结论，再解释依据。', { exact: true }).waitFor({ state: 'hidden' })
  checks.push('delete cancellation is safe and confirmation uses stable ID')
  await page.getByRole('button', { name: '添加记忆', exact: true }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('离开设置后仍保留这段草稿。')
  await page.getByRole('button', { name: '切换设置', exact: true }).click()
  await page.getByRole('button', { name: '返回记忆', exact: true }).click()
  assert.equal(await page.getByLabel('记忆内容', { exact: true }).inputValue(), '离开设置后仍保留这段草稿。')
  failRead = true
  await page.getByRole('button', { name: '刷新记忆', exact: true }).click()
  await page.getByRole('alert').waitFor()
  assert.equal(await page.getByLabel('记忆内容', { exact: true }).inputValue(), '离开设置后仍保留这段草稿。')
  failRead = false; await page.getByRole('button', { name: '重试读取', exact: true }).click()
  await page.getByRole('alert').waitFor({ state: 'hidden' })
  checks.push('settings re-entry and failed reload preserve drafts')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: path.join(output, 'memory-narrow.png'), fullPage: true })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true)
  checks.push('390px layout does not overflow horizontally')
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
} catch (error) { failure = error; await page.screenshot({ path: path.join(output, 'memory-failure.png'), fullPage: true }) }
finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ checks, mutations, errors, unexpected, failure: failure?.message }, null, 2))
  await browser.close()
}
if (failure) throw failure
console.log(JSON.stringify({ checks: checks.length, screenshots: output }))
