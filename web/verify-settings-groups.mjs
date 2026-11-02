// Real SettingsView and resource components; all API traffic stays in offline fixtures.
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'

const base = process.argv[2] || 'http://127.0.0.1:5178'
const origin = new URL(base).origin
const out = resolve(process.argv[3] || '../.tmp/settings-groups-browser')
await mkdir(out, { recursive: true })
const source = await (await fetch(`${base}/src/views/SettingsView.vue`)).text()
const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'Vite server required')
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = pw.default || pw
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' })
const report = { passed: false, checks: [], screenshots: [], errors: [], unexpected: [], requests: [] }
await context.route('**/*', async route => {
  const req = route.request(), url = new URL(req.url()), path = url.pathname
  if (url.origin !== origin) { report.unexpected.push(`external:${url.origin}`); return route.abort() }
  if (path === '/settings-groups-fixture') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module">import {createApp,h,Fragment} from ${JSON.stringify(vuePath)};import Settings from '/src/views/SettingsView.vue';import DialogHost from '/src/components/ui/DialogHost.vue';import '/src/style.css';createApp({render:()=>h(Fragment,null,[h(Settings,{user:{id:'fixture',role:'admin'},initialSection:new URLSearchParams(location.search).get('section')||'general'}),h(DialogHost)])}).mount('#app');</script></body></html>` })
  if (!path.startsWith('/api/')) return route.continue()
  report.requests.push(`${req.method()} ${path}`)
  const fixtures = {
    '/api/v1/settings/agent-team': { max_depth: 3, max_parallel: 4 },
    '/api/v1/settings/agent-team/budgets': { task_tokens: 0, task_seconds: 0, team_tokens: 0, team_seconds: 0 },
    '/api/v1/settings/permission': { mode: 'read-only', modes: [{ id: 'read-only', label: '只读', desc: '离线验证' }, { id: 'workspace-write', label: '工作区可写', desc: '离线验证' }] },
    '/api/v1/memory': { user: { entries: ['统一使用中文回复'] }, sessions: [] },
    '/api/v1/knowledge': [], '/api/v1/knowledge/stats': { docs: 0, chunks: 0 },
    '/api/v1/usage/me': { prompts: 3, input_tokens: 1280, output_tokens: 480, by_model: [] },
    '/api/v1/automations': [], '/api/v1/admin/audit': [], '/api/v1/admin/audit/verify': { ok: true },
    '/api/health': { ok: true, version: 'fixture' }, '/api/v1/health': { ok: true, version: 'fixture' },
  }
  if (req.method() !== 'GET' || !Object.hasOwn(fixtures, path)) { report.unexpected.push(`${req.method()} ${path}`); return route.fulfill({ status: 500, json: { detail: 'Unexpected fixture API' } }) }
  return route.fulfill({ json: fixtures[path] })
})
let page
const count = path => report.requests.filter(req => req === `GET ${path}`).length
async function shot(name) { const path = join(out, name); await page.screenshot({ path, fullPage: true }); report.screenshots.push(path) }
try {
  page = await context.newPage()
  page.on('pageerror', error => report.errors.push(error.message))
  await page.goto(`${base}/settings-groups-fixture`)
  await page.getByRole('heading', { name: '工作区与外观', exact: true }).waitFor()
  assert.equal(await page.getByRole('navigation', { name: '设置分类' }).getByRole('button').count(), 8)
  for (const path of ['/api/v1/memory', '/api/v1/knowledge', '/api/v1/usage/me', '/api/v1/automations', '/api/v1/admin/audit', '/api/v1/model-profiles']) assert.equal(count(path), 0)
  await page.getByRole('tab', { name: '外观', exact: true }).click()
  await page.getByRole('tab', { name: '外观', exact: true }).press('End')
  await page.locator('#shortcut-list').waitFor()
  assert.equal(await page.locator(':focus').getAttribute('id'), 'settings-tab-shortcuts')
  await page.getByRole('tab', { name: '快捷键', exact: true }).press('Home')
  await page.locator('#send-key').selectOption('ctrl-enter')
  await page.getByRole('tab', { name: '外观', exact: true }).click()
  await page.getByRole('button', { name: '智能体与权限', exact: true }).click()
  await page.locator('#agent-team-parallel').fill('7')
  await page.getByRole('tab', { name: '执行与权限', exact: true }).click()
  await page.locator('#execution-permission').selectOption('workspace-write')
  await page.getByRole('button', { name: '工作区与外观', exact: true }).click()
  assert.equal(await page.getByRole('tab', { name: '外观', exact: true }).getAttribute('aria-selected'), 'true')
  await page.getByRole('tab', { name: '通用', exact: true }).click()
  assert.equal(await page.locator('#send-key').inputValue(), 'ctrl-enter')
  await page.getByRole('button', { name: '智能体与权限', exact: true }).click()
  assert.equal(await page.locator('#execution-permission').inputValue(), 'workspace-write')
  await page.getByRole('tab', { name: '智能体协作', exact: true }).click()
  assert.equal(await page.locator('#agent-team-parallel').inputValue(), '7')
  report.checks.push('8 groups, keyboard subsection navigation, remembered subsection and retained collaboration/permission drafts')
  await page.getByRole('searchbox', { name: '搜索设置', exact: true }).fill('减少动态效果')
  await page.locator('.search-result').click()
  assert.equal(await page.getByRole('tab', { name: '外观', exact: true }).getAttribute('aria-selected'), 'true')
  assert.equal(await page.locator(':focus').getAttribute('id'), 'reduced-motion')
  await shot('settings-groups-1280.png')
  await page.getByRole('button', { name: '知识与记忆', exact: true }).click()
  await page.locator('#knowledge-settings').waitFor()
  assert.equal(count('/api/v1/memory'), 0, 'knowledge group must not load memory until selected')
  await page.getByRole('tab', { name: '记忆', exact: true }).click()
  await page.getByRole('button', { name: '添加记忆', exact: true }).click()
  await page.locator('#memory-fact').fill('未保存的记忆草稿')
  await page.getByRole('button', { name: '运行与记录', exact: true }).click()
  await page.getByRole('button', { name: '刷新数据', exact: true }).waitFor()
  assert.equal(count('/api/v1/automations'), 0); assert.equal(count('/api/v1/admin/audit'), 0)
  await page.getByRole('tab', { name: '自动化', exact: true }).click()
  await page.getByRole('textbox', { name: '任务名称', exact: true }).fill('保留任务草稿')
  await page.getByRole('tab', { name: '审计', exact: true }).click()
  await page.getByRole('searchbox', { name: '搜索审计记录' }).fill('read_file')
  await page.getByRole('tab', { name: '自动化', exact: true }).click()
  assert.equal(await page.getByRole('textbox', { name: '任务名称', exact: true }).inputValue(), '保留任务草稿')
  await page.getByRole('button', { name: '知识与记忆', exact: true }).click()
  assert.equal(await page.locator('#memory-fact').inputValue(), '未保存的记忆草稿')
  await page.getByRole('button', { name: '运行与记录', exact: true }).click()
  assert.equal(await page.getByRole('tab', { name: '自动化', exact: true }).getAttribute('aria-selected'), 'true')
  await page.getByRole('tab', { name: '审计', exact: true }).click()
  assert.equal(await page.getByRole('searchbox', { name: '搜索审计记录' }).inputValue(), 'read_file')
  report.checks.push('resource siblings stay lazy and actual memory, automation and audit drafts survive subsection/group changes')
  for (const width of [768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    await page.getByRole('button', { name: '工作区与外观', exact: true }).click()
    await page.getByRole('tab', { name: '快捷键', exact: true }).click()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `page overflow at ${width}`)
    await shot(`settings-groups-${width}.png`)
    await page.getByRole('button', { name: '运行与记录', exact: true }).click()
    await page.getByRole('tab', { name: '自动化', exact: true }).click()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `resource overflow at ${width}`)
  }
  await page.goto(`${base}/settings-groups-fixture?section=memory`)
  await page.locator('#memory-settings').waitFor()
  assert.equal(await page.getByRole('tab', { name: '记忆', exact: true }).getAttribute('aria-selected'), 'true')
  report.checks.push('legacy resource URL opens correct group/subsection; 1280/768/390/320 layouts remain bounded')
  assert.deepEqual(report.errors, []); assert.deepEqual(report.unexpected, [])
  report.passed = true
} catch (error) {
  report.error = error.stack || String(error)
  if (page) await page.screenshot({ path: join(out, 'failure.png'), fullPage: true }).catch(() => {})
  process.exitCode = 1
} finally {
  await writeFile(join(out, 'report.json'), JSON.stringify(report, null, 2))
  await browser.close()
  console.log(JSON.stringify(report))
}
