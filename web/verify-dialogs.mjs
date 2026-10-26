// Browser checks use intercepted APIs only; they never mutate a user's workspace.
// node web/verify-dialogs.mjs http://127.0.0.1:5175 .tmp/dialogs
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:5175'
const chatBase = process.env.CHAT_BASE_URL || 'http://127.0.0.1:5174'
const output = path.resolve(process.argv[3] || '.tmp/dialogs')
await fs.mkdir(output, { recursive: true })
const source = await (await fetch(`${base}/src/components/ui/DialogHost.vue`)).text()
const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'Vite must be running')
const chatSource = await (await fetch(`${chatBase}/src/components/ui/DialogHost.vue`)).text()
const chatVuePath = chatSource.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(chatVuePath, 'The standalone chat Vite server must be running')
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
const page = await context.newPage()
page.setDefaultTimeout(10000)
const errors = [], nativeDialogs = [], unexpected = [], checks = [], mutations = []
page.on('pageerror', error => errors.push(error.message))
page.on('dialog', async dialog => { nativeDialogs.push(dialog.type()); await dialog.dismiss() })
let sessions = [{ id: 'fixture-session', title: '登录流程讨论', folder: '产品迭代', status: 'active' }]
let workspaces = [{ name: '产品迭代' }], failCreate = true, holdDelete, releaseDelete
await context.route('**/api/**', async route => {
  const req = route.request(), p = decodeURIComponent(new URL(req.url()).pathname), method = req.method()
  let data
  if (method !== 'GET') mutations.push({ method, p, body: req.postDataJSON() })
  if (p === '/api/v1/workspaces' && method === 'POST') {
    if (failCreate) { failCreate = false; return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '服务暂时不可用，请重试' }) }) }
    data = { name: req.postDataJSON().name }; workspaces.push(data)
  } else if (p === '/api/v1/workspaces/产品迭代' && method === 'DELETE') {
    if (holdDelete) await new Promise(resolve => { releaseDelete = resolve })
    workspaces = workspaces.filter(w => w.name !== '产品迭代'); sessions = sessions.map(s => ({ ...s, folder: '' })); data = { ok: true }
  } else if (p === '/api/v1/sessions/fixture-session' && method === 'DELETE') { sessions = []; data = { ok: true } }
  else if (p === '/api/v1/auth/me' && method === 'GET') data = { id: 'fixture-user', role: 'admin' }
  else if (p === '/api/v1/sessions' && method === 'GET') data = sessions
  else if (p === '/api/v1/workspaces' && method === 'GET') data = workspaces
  else if (p === '/api/v1/settings/model' && method === 'GET') data = { provider: 'offline', active_model: 'offline' }
  else if (p === '/api/v1/model-profiles' && method === 'GET') data = []
  else if (p === '/api/v1/settings/permission' && method === 'GET') data = { default: 'read-only', modes: [{ id: 'read-only', label: '只读' }] }
  else if (p === '/api/v1/web' && method === 'GET') data = { available: false }
  else { unexpected.push(`${method} ${p}`); return route.fulfill({ status: 404, body: '{}' }) }
  return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
})
await context.addInitScript(() => {
  const NativeSocket = window.WebSocket
  window.WebSocket = class extends NativeSocket {
    constructor(url, ...args) {
      if (String(url).includes('/api/')) throw new Error('Unexpected fixture WebSocket')
      super(url, ...args)
    }
  }
})
await page.route('**/__dialog-test*', async route => {
  const target = new URL(route.request().url())
  const chat = target.searchParams.has('chat')
  const app = target.searchParams.has('app')
  const runtimePath = target.origin === new URL(chatBase).origin ? chatVuePath : vuePath
  await route.fulfill({ contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module">
    import { createApp, h } from '${runtimePath}'; import Host from '/src/components/ui/DialogHost.vue';
    ${chat ? "import Chat from '/src/views/ChatView.vue';" : ''}
    ${app ? "import App from '/src/App.vue';" : ''}
    import { dialogService as service } from '/src/composables/useDialog.js'; import '/src/style.css';
    window.service = service; window.writes = 0;
    const launch = () => service.open({ title: '新建工作区', description: '为相关会话创建一个分组，让讨论更有条理。', icon: 'folder', confirmText: '创建工作区', input: { label: '工作区名称', required: true, placeholder: '例如：产品迭代', hint: '可以稍后移动会话到这个工作区。' }, onConfirm: value => { window.writes++; return value } });
    const remove = () => service.open({ title: '删除会话', description: '删除后无法恢复，请确认要删除的会话。', details: '登录流程讨论', tone: 'danger', confirmText: '删除会话', onConfirm: () => { window.writes++ } });
    createApp({ render: () => ${app ? 'h(App)' : `[${chat ? "h(Chat, { user: { role: 'admin' } })" : "h('main', { style: 'padding:40px' }, [h('button', { id:'create', onClick: launch }, '新建工作区'), h('button', { id:'remove', onClick: remove }, '删除会话')])"}, h(Host)]`} }).mount('#app');
  </script></body></html>` })
})
const modal = page.getByRole('dialog')
async function closed() { await page.locator('dialog[open]').waitFor({ state: 'hidden' }) }
async function screenshot(name) { await page.screenshot({ path: path.join(output, name) }) }
let failure
try {
  await page.goto(`${base}/__dialog-test`)
  await page.locator('#create').click()
  await page.getByLabel('工作区名称').waitFor()
  assert.equal(await page.getByLabel('工作区名称').evaluate(el => el === document.activeElement), true)
  await screenshot('create-desktop.png')
  checks.push('Input dialog automatically focuses its named field')
  await page.getByLabel('工作区名称').fill('   ')
  await modal.getByRole('button', { name: '创建工作区' }).click()
  await modal.getByRole('alert').getByText('请输入工作区名称').waitFor()
  assert.equal(await page.evaluate(() => window.writes), 0)
  await page.getByLabel('工作区名称').fill('测试分组')
  await page.keyboard.press('Enter'); await closed()
  assert.equal(await page.evaluate(() => window.writes), 1)
  assert.equal(await page.locator('#create').evaluate(el => el === document.activeElement), true)
  checks.push('Whitespace blocked, Enter submits once and focus returns to trigger')
  await page.locator('#remove').click()
  assert.equal(await modal.getByRole('button', { name: '取消', exact: true }).evaluate(el => el === document.activeElement), true)
  await screenshot('delete-desktop.png')
  for (let i = 0; i < 7; i++) {
    await page.keyboard.press('Tab')
    assert.equal(await modal.evaluate(el => el.contains(document.activeElement)), true)
  }
  await page.keyboard.press('Escape'); await closed()
  assert.equal(await page.evaluate(() => window.writes), 1)
  checks.push('Destructive dialog starts on Cancel, traps Tab and Escape cancels without writes')
  await page.locator('#remove').click(); await page.mouse.click(8, 8); await closed()
  checks.push('Backdrop click cancels without mutation')
  await page.setViewportSize({ width: 320, height: 640 })
  await page.locator('#remove').click()
  const box = await modal.boundingBox()
  assert.ok(box.x >= 0 && box.x + box.width <= 320)
  await screenshot('delete-mobile.png')
  await page.keyboard.press('Escape'); await closed()
  checks.push('320px dialog remains within viewport')

  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto(`${chatBase}/__dialog-test?chat`)
  await page.getByTitle('新建工作区').click()
  await page.getByLabel('工作区名称').fill('验收工作区')
  await modal.locator('button[type=submit]').click()
  await modal.getByRole('alert').getByText('服务暂时不可用，请重试').waitFor()
  assert.equal(await page.getByLabel('工作区名称').inputValue(), '验收工作区')
  await screenshot('workspace-retry.png')
  await modal.locator('button[type=submit]').click(); await closed()
  await page.getByText('验收工作区', { exact: true }).first().waitFor()
  assert.equal(mutations.filter(m => m.method === 'POST').length, 2)
  checks.push('Real Chat workspace action retains API failure and input, retries successfully')

  await page.locator('.ws-group').filter({ hasText: '产品迭代' }).getByTitle('删除工作区（会话退回默认工作区，不会删会话）').click()
  await modal.getByText(/默认工作区/).waitFor()
  await screenshot('workspace-delete.png')
  await modal.getByRole('button', { name: '取消', exact: true }).click(); await closed()
  assert.equal(mutations.filter(m => m.method === 'DELETE').length, 0)
  checks.push('Workspace deletion shows session preservation and Cancel performs zero DELETEs')
  await page.locator('.ws-group').filter({ hasText: '产品迭代' }).getByTitle('删除工作区（会话退回默认工作区，不会删会话）').click()
  holdDelete = true
  await modal.locator('button[type=submit]').click()
  await page.waitForFunction(() => document.querySelector('dialog').getAttribute('aria-busy') === 'true')
  assert.equal(await modal.getByRole('button', { name: '取消', exact: true }).isDisabled(), true)
  assert.equal(await modal.locator('button[type=submit]').isDisabled(), true)
  assert.equal(await modal.evaluate(el => el === document.activeElement), true)
  await page.keyboard.press('Tab')
  assert.equal(await modal.evaluate(el => el === document.activeElement), true)
  await page.keyboard.press('Escape')
  assert.equal(await modal.isVisible(), true)
  await screenshot('delete-pending.png')
  assert.equal(mutations.filter(m => m.method === 'DELETE').length, 1)
  releaseDelete(); await closed()
  await page.getByText('登录流程讨论', { exact: true }).first().waitFor()
  checks.push('Pending deletion locks duplicate submit and dismissal; sessions remain after workspace removal')
  await page.locator('.sess-row').filter({ hasText: '登录流程讨论' }).locator('.si-more').click()
  await page.getByRole('button', { name: '删除会话', exact: true }).click()
  await modal.getByText(/永久清除|不可恢复|无法恢复/).waitFor()
  await modal.locator('button[type=submit]').click(); await closed()
  assert.equal(mutations.filter(m => m.p === '/api/v1/sessions/fixture-session').length, 1)
  checks.push('Permanent session deletion identifies loss and deletes exactly the selected fixture')
  await page.goto(`${chatBase}/__dialog-test?app`)
  await page.locator('.chat').waitFor()
  await page.getByTitle('新建工作区').click()
  await page.getByLabel('工作区名称').waitFor()
  assert.equal(await page.locator('dialog[open]').count(), 1)
  await screenshot('workspace-create-app.png')
  await page.keyboard.press('Escape'); await closed()
  checks.push('Standalone chat application mounts one shared dialog host')
  assert.deepEqual(errors, []); assert.deepEqual(nativeDialogs, []); assert.deepEqual(unexpected, [])
} catch (error) { failure = error.stack; await screenshot('failure.png').catch(() => {}) }
finally {
  await browser.close()
  const report = { passed: !failure, checks, errors, nativeDialogs, unexpected, mutations, failure: failure || null }
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
}
if (failure) process.exitCode = 1
