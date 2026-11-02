import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

// Mount the real menu component with isolated command records. No project,
// model, service, editor or settings API request is permitted by this fixture.
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || '.tmp/ide-menu-browser')
await fs.mkdir(output, { recursive: true })
const source = await (await fetch(`${base}/src/components/code/IdeMenuBar.vue`)).text()
const vue = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vue, 'A Vite development server is required')
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const checks = [], errors = [], forbidden = []
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin || url.pathname.startsWith('/api/') || url.pathname === '/healthz') { forbidden.push(url.href); return route.abort() }
    if (url.pathname === '/__ide_menu_fixture__') return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>IDE 菜单验证</title><div id="app"></div><script type="module">import{createApp,h,reactive}from ${JSON.stringify(vue)};import Menu from'/src/components/code/IdeMenuBar.vue';import'/src/style.css';const menus=reactive([{id:'file',label:'文件',items:[{id:'open',label:'打开项目',shortcut:'Ctrl+O'},{id:'disabled',label:'不可用操作',disabled:true},{separator:true},{id:'save',label:'保存文件',shortcut:'Ctrl+S'},{id:'autosave',label:'自动保存',checked:false}]},{id:'edit',label:'编辑',items:[{id:'undo',label:'撤销',disabled:true},{id:'format',label:'整理格式',shortcut:'Shift+Alt+F'}]},{id:'view',label:'视图',items:[{id:'sidebar',label:'文件侧栏',checked:true}]},{id:'navigate',label:'导航',items:[{id:'symbol',label:'代码导航'}]},{id:'run',label:'运行',items:[{id:'run',label:'运行当前文件'}]},{id:'help',label:'帮助',items:Array.from({length:24},(_,i)=>({id:'help-'+i,label:'帮助条目 '+i}))}]);window.fixtureMenu={commands:[],menus};createApp({setup(){return()=>h('main',{style:'height:100%;padding:0 8px'},[h('header',{style:'display:flex;justify-content:flex-end;align-items:center;height:38px;border-bottom:1px solid var(--border)'},[h(Menu,{menus,onCommand:id=>{window.fixtureMenu.commands.push(id);if(id==='autosave')menus[0].items[4].checked=!menus[0].items[4].checked}}),h('button',{id:'after-menu'},'搜索')]),h('section',{style:'padding:28px'},[h('h1','IDE 菜单交互验证'),h('p','动作只记录到隔离测试数据。')])])}}).mount('#app');</script></html>` })
    return route.continue()
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${base}/__ide_menu_fixture__`)
  const bar = page.getByRole('menubar', { name: 'IDE 菜单' })
  const file = bar.getByRole('menuitem', { name: '文件', exact: true })
  await file.click()
  await page.getByRole('menu', { name: '文件', exact: true }).waitFor()
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), '打开项目Ctrl+O')
  assert.equal(await page.getByRole('menuitem', { name: '不可用操作', exact: true }).isDisabled(), true)
  await page.keyboard.press('ArrowDown')
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), '保存文件Ctrl+S')
  await page.keyboard.press('Enter')
  assert.deepEqual(await page.evaluate(() => window.fixtureMenu.commands), ['save'])
  assert.equal(await file.getAttribute('aria-expanded'), 'false')
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), '文件')
  checks.push('鼠标打开与键盘执行真实命令，跳过禁用项与分隔线并恢复焦点')

  await page.keyboard.press('ArrowUp')
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), '自动保存')
  await page.keyboard.press('Space')
  await file.click()
  assert.equal(await page.getByRole('menuitemcheckbox', { name: '自动保存' }).getAttribute('aria-checked'), 'true')
  await page.keyboard.press('ArrowRight')
  await page.getByRole('menu', { name: '编辑', exact: true }).waitFor()
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), '整理格式Shift+Alt+F')
  await page.keyboard.press('ArrowRight')
  await page.getByRole('menu', { name: '视图', exact: true }).waitFor()
  await page.keyboard.press('ArrowLeft')
  await page.keyboard.press('Escape')
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), '编辑')
  checks.push('勾选状态同步，左右切换菜单与 Escape 返回对应触发器')

  await file.click()
  await page.keyboard.press('Tab')
  assert.equal(await page.evaluate(() => document.activeElement.id), 'after-menu')
  assert.equal(await page.getByRole('menu').count(), 0)
  await file.click(); await page.locator('#after-menu').click()
  assert.equal(await page.getByRole('menu').count(), 0)
  checks.push('Tab 正常离开菜单，外部点击关闭且不产生动作')

  await page.setViewportSize({ width: 390, height: 380 })
  await bar.getByRole('menuitem', { name: '帮助', exact: true }).click()
  const help = page.getByRole('menu', { name: '帮助', exact: true })
  const rect = await help.boundingBox()
  assert.ok(rect.x >= 7 && rect.x + rect.width <= 383 && rect.y >= 7 && rect.y + rect.height <= 373, JSON.stringify(rect))
  await page.keyboard.press('End')
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), '帮助条目 23')
  await page.screenshot({ path: path.join(output, 'menu-390.png') })
  await page.keyboard.press('Home')
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), '帮助条目 0')
  await page.keyboard.press('Escape')
  await page.setViewportSize({ width: 1440, height: 900 })
  await file.click(); await page.screenshot({ path: path.join(output, 'menu-1440.png') })
  checks.push('390px 右侧与短视口浮层不溢出，长菜单 Home/End 与滚动可达')
  assert.deepEqual(errors, []); assert.deepEqual(forbidden, [])
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ passed: true, fixtureCommands: true, checks, errors, forbidden }, null, 2))
  console.log(JSON.stringify({ passed: true, checks }))
} finally { await browser.close() }
