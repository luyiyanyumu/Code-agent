import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

// Real production Vue component; native bridge responses are isolated fixtures.
// No user service, model, native action, file or update is changed by this run.
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || '.tmp/desktop-panel-browser')
await fs.mkdir(output, { recursive: true })
const appSource = await (await fetch(`${base}/src/App.vue`)).text()
const vuePath = appSource.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'A Vite development server is required')
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const checks = [], errors = [], network = []
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
  await context.addInitScript(() => {
    const listeners = new Set()
    const state = { version: '0.10.2', mode: 'bundled', services: [{ id: 'backend', state: 'healthy' }, { id: 'frontend', state: 'healthy' }], operation: null, update: { current: '0.10.2', checkedAt: null }, desktop: { managedServices: ['backend', 'frontend'], reusedServices: [] } }
    window.fixtureDesktop = { requests: [], failRestart: true, dirty: false, state }
    window.__CODE_AGENT_DESKTOP__ = { integrated: true }
    window.chrome.webview = {
      addEventListener: (_, listener) => listeners.add(listener), removeEventListener: (_, listener) => listeners.delete(listener),
      emit: message => { for (const listener of listeners) listener({ data: message }) },
      postMessage(message) {
        window.fixtureDesktop.requests.push(message)
        if (message.method === 'close-response') return
        setTimeout(() => {
          let result, error
          if (message.method === 'status') result = state
          else if (message.method === 'logs') result = { text: `INFO [${message.params.service}] 桌面界面验证日志\nINFO 原生桥测试数据，未连接用户服务。` }
          else if (message.method === 'action') {
            if (message.params.action === 'restart' && window.fixtureDesktop.failRestart) error = '仍有任务正在执行，请稍后重启。'
            else {
              if (message.params.action === 'check-update') state.update = { current: '0.10.2', latest: '0.10.3', checkedAt: new Date().toISOString(), available: true, message: '验证示例版本，请使用新版安装包更新。' }
              state.operation = { state: 'succeeded', message: message.params.action === 'open' ? '已打开日志目录。' : '验证操作已完成。' }
              result = state
            }
          }
          window.chrome.webview.emit({ channel: message.channel, id: message.id, ok: !error, result: result ? JSON.parse(JSON.stringify(result)) : result, error })
        }, message.method === 'action' ? 240 : 30)
      },
    }
  })
  await context.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin || url.pathname.startsWith('/api/') || url.pathname === '/healthz') { network.push(url.href); return route.abort() }
    if (url.pathname === '/__desktop_component_fixture__') return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>桌面维护组件验证</title><div id="app"></div><script type="module">import{createApp,h,ref}from ${JSON.stringify(vuePath)};import Panel from'/src/components/DesktopSystemPanel.vue';import{desktopRuntime}from'/src/lib/desktopRuntime.js';import'/src/style.css';desktopRuntime.registerLeaveGuard(()=>!window.fixtureDesktop.dirty||window.confirm('未保存文件，仍要关闭吗？'));const open=ref(false);createApp({setup(){return()=>h('main',{style:'padding:40px'},[h('h1','桌面模式验证'),h('p','原生通信使用隔离测试数据'),h('button',{id:'open-status',onClick:()=>open.value=true},'应用状态'),h(Panel,{open:open.value,onClose:()=>open.value=false})])}}).mount('#app');</script></html>` })
    return route.continue()
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${base}/__desktop_component_fixture__`)
  await page.getByRole('button', { name: '应用状态', exact: true }).click()
  const panel = page.getByRole('dialog', { name: '应用状态' })
  await panel.waitFor()
  await panel.getByText('运行正常', { exact: true }).first().waitFor()
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), '关闭应用状态')
  await page.waitForFunction(() => document.querySelector('.system-log')?.textContent.includes('[backend]'))
  assert.match(await panel.getByLabel('运行日志内容').innerText(), /backend/)
  await page.screenshot({ path: path.join(output, 'desktop-1440.png') })
  checks.push('真实组件显示服务、版本、日志，并将焦点移到关闭按钮')
  await panel.getByRole('button', { name: '重启', exact: true }).first().click()
  assert.equal(await panel.getByRole('button', { name: '重启', exact: true }).first().isDisabled(), true)
  await panel.getByRole('alert').filter({ hasText: '仍有任务正在执行' }).waitFor()
  checks.push('后台重启错误明确可见，操作期间禁用重复提交')
  await page.evaluate(() => { window.fixtureDesktop.failRestart = false })
  await panel.getByRole('button', { name: '重启', exact: true }).first().click()
  await panel.getByRole('status').filter({ hasText: '验证操作已完成' }).waitFor()
  await panel.getByRole('button', { name: '检查更新' }).click()
  await panel.getByText(/发现更新 0.10.3/).waitFor()
  checks.push('重试真实桥动作、版本检查结果正确显示')
  await panel.getByLabel('日志来源').selectOption('frontend')
  await page.waitForFunction(() => document.querySelector('.system-log')?.textContent.includes('[frontend]'))
  await panel.getByRole('button', { name: '打开日志目录' }).click()
  await page.waitForFunction(() => window.fixtureDesktop.requests.some(request => request.method === 'action' && request.params.action === 'open' && request.params.target === 'logs'))
  await page.waitForFunction(() => !document.querySelector('.system-update button')?.disabled)
  await page.keyboard.press('Escape')
  assert.equal(await panel.isVisible(), false)
  assert.equal(await page.evaluate(() => document.activeElement?.id), 'open-status')
  checks.push('日志切换、打开目录契约、Escape关闭和焦点恢复正常')
  await page.evaluate(() => { window.fixtureDesktop.dirty = true })
  page.once('dialog', dialog => dialog.dismiss())
  await page.evaluate(() => { window.chrome.webview.emit({ channel: 'code-agent-desktop', event: 'before-close', id: 'dirty-close' }) })
  await page.waitForFunction(() => window.fixtureDesktop.requests.some(request => request.id === 'dirty-close' && request.params.allow === false))
  await page.evaluate(() => { window.fixtureDesktop.dirty = false; window.chrome.webview.emit({ channel: 'code-agent-desktop', event: 'before-close', id: 'clean-close' }) })
  await page.waitForFunction(() => window.fixtureDesktop.requests.some(request => request.id === 'clean-close' && request.params.allow === true))
  checks.push('原生关闭请求遵守未保存取消，干净状态直接允许关闭')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '应用状态', exact: true }).click()
  await panel.waitFor()
  const bounds = await panel.boundingBox()
  assert.ok(bounds.width <= 390 && bounds.x >= 0)
  assert.ok(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1), 'mobile dialog has no horizontal overflow')
  await page.screenshot({ path: path.join(output, 'desktop-390.png') })
  checks.push('390px视口弹窗与主要操作无横向溢出')
  assert.deepEqual(errors, [])
  assert.deepEqual(network, [])
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ passed: true, fixtureNativeBridge: true, checks, errors, network }, null, 2))
  console.log(JSON.stringify({ passed: true, checks, output }))
} finally { await browser.close() }
