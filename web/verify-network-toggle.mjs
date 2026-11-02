// Actual AiPanel/useAgentSession/ModelSelector; offline HTTP and stream fixtures.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

const root = path.dirname(fileURLToPath(import.meta.url)), port = 5204, base = `http://127.0.0.1:${port}`
const output = path.resolve(process.argv[2] || path.join(root, '../.tmp/network-toggle-20261101/browser'))
await fs.mkdir(output, { recursive: true })
const report = { passed: false, checks: [], errors: [], unexpected: [], requests: [], screenshots: [], observations: {}, owned: { port, pid: process.pid, browsersClosed: false, serverStopped: false }, scope: 'Real AiPanel, session composable and ModelSelector; only isolated local API/stream responses are mocked. No real backend, model, credentials or user windows.' }
let vite, browser, page, fixtureHtml = '', failure, gateRelease
let available = false, webEnabled = false, failPost = false, holdPost = false, failCapability = false
let currentModel = 'deepseek-flash'
const capability = () => available ? { available: true, model: 'deepseek-web-fixture', max_results: 8 }
  : { available: false, reason: '未配置联网检索密钥', how_to: '请在模型设置中配置可用于联网检索的 DeepSeek 提供方。' }
const directory = () => ({ available: true, inherited: false, current: { provider: 'deepseek', model: currentModel }, groups: [{ id: 'deepseek', name: 'DeepSeek', configured: true, models: [{ id: 'deepseek-flash', name: 'DeepSeek Flash' }, { id: 'deepseek-pro', name: 'DeepSeek Pro' }] }] })
try {
  vite = await createServer({ configFile: false, root, plugins: [vue(), { name: 'network-toggle-offline-fixture', configureServer(server) { server.middlewares.use((request, response, next) => { if (request.url?.split('?')[0] !== '/__network-toggle') return next(); response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' }); response.end(fixtureHtml) }) } }],
    cacheDir: path.join(root, 'node_modules/.vite-network-toggle-fixture'), server: { host: '127.0.0.1', port, strictPort: true, proxy: { '/api': { target: 'http://127.0.0.1:9', ws: true } } }, optimizeDeps: { include: ['vue', 'marked'] }, logLevel: 'warn' })
  await vite.listen()
  const source = await (await fetch(`${base}/src/components/code/AiPanel.vue`)).text(), vueModule = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vueModule)
  fixtureHtml = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body,#app{height:100%;margin:0;overflow:hidden}body{background:var(--bg);font:14px system-ui}#fixture{height:100%;width:520px;max-width:100%;margin:auto;display:flex;flex-direction:column;border-inline:1px solid var(--border);box-sizing:border-box}#fixture>nav{flex:none;display:flex;gap:6px;padding:8px;background:var(--panel-soft);border-bottom:1px solid var(--border)}#fixture>nav button{padding:4px 8px;font-size:12px}#fixture>.ai{flex:1;min-height:0}</style></head><body><div id="app"></div><script type="module">import{createApp,h,ref}from'${vueModule}';import AiPanel from'/src/components/code/AiPanel.vue';import'/src/style.css';const visible=ref(true),settings=[];window.__networkFixture={visible,settings};createApp({render:()=>h('main',{id:'fixture'},[h('nav',[h('button',{id:'hide',onClick:()=>visible.value=false},'隐藏面板'),h('button',{id:'show',onClick:()=>visible.value=true},'显示面板')]),h(AiPanel,{sessionId:'network-fixture',visible:visible.value,writable:false,onOpenSettings:section=>settings.push(section)})])}).mount('#app')</script></body></html>`
  const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  browser = await (loaded.default || loaded).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1024, height: 820 }, serviceWorkers: 'block' })
  await context.addInitScript(() => {
    const NativeSocket=window.WebSocket
    window.WebSocket = class extends EventTarget {
      static OPEN=1;static CONNECTING=0;static CLOSED=3
      constructor(url,...args) { super();this.session=/\/sessions\/network-fixture\/stream/.test(String(url));if(!this.session)return new NativeSocket(url,...args);this.url=url;this.readyState=0;queueMicrotask(()=>{this.readyState=1;this.onopen?.({});this.onmessage?.({data:JSON.stringify({type:'session_meta',payload:{busy:false,phase:'',model:'deepseek-flash',pending_questions:[],web_enabled:false}})})}) }
      send(raw) { if(this.session&&JSON.parse(raw).type!=='ping')throw new Error('Fixture forbids model or execution requests') }
      close(){this.readyState=3}
    }
  })
  page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',error=>report.errors.push(error.message))
  const json=(route,value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)})
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),p=url.pathname,method=request.method()
    if(url.origin!==base){report.unexpected.push(`${method} ${url.href}`);return route.abort()}
    if(!p.startsWith('/api/'))return route.continue()
    const body=request.postDataJSON();report.requests.push({method,path:p,body})
    if(method==='GET'&&p==='/api/v1/web')return json(route,failCapability?{detail:'OFFLINE_CAPABILITY_ERROR'}:capability(),failCapability?503:200)
    if(method==='POST'&&p==='/api/v1/sessions/network-fixture/web'){
      if(holdPost)await new Promise(resolve=>{gateRelease=resolve})
      if(failPost)return json(route,{detail:'联网设置保存失败，请重试'},503)
      assert.equal(typeof body?.enabled,'boolean');webEnabled=body.enabled;return json(route,{web_enabled:webEnabled})
    }
    if(method==='GET'&&p==='/api/v1/sessions/network-fixture/models')return json(route,directory())
    if(method==='PUT'&&p==='/api/v1/sessions/network-fixture/model'){currentModel=body.model;return json(route,directory())}
    if(method==='GET'&&p==='/api/v1/sessions/network-fixture/events')return json(route,[])
    if(method==='GET'&&p==='/api/v1/sessions/network-fixture/checkpoints')return json(route,{busy:false})
    if(method==='GET'&&p==='/api/v1/sessions/network-fixture/approvals/pending')return json(route,{})
    if(method==='GET'&&p==='/api/v1/sessions/network-fixture/questions/pending')return json(route,{questions:[]})
    report.unexpected.push(`${method} ${p}`);return json(route,{detail:'Unexpected fixture API'},500)
  })
  const toggle=()=>page.locator('.web-toggle'),notice=()=>page.locator('.web-feedback')
  const count=(method,p)=>report.requests.filter(request=>request.method===method&&request.path===p).length
  const wait=predicate=>page.waitForFunction(predicate)
  const on=()=>wait(()=>document.querySelector('.web-toggle')?.classList.contains('on'))
  const off=()=>wait(()=>!document.querySelector('.web-toggle')?.classList.contains('on'))
  const idle=()=>wait(()=>document.querySelector('.web-toggle')&&!document.querySelector('.web-toggle').disabled)
  const shot=async name=>{const file=path.join(output,name);await page.screenshot({path:file,fullPage:true});report.screenshots.push(file);const composer=page.locator('.composer');if(await composer.count()){const card=file.replace(/\.png$/,'-composer.png');await composer.screenshot({path:card});report.screenshots.push(card)}}
  await page.goto(`${base}/__network-toggle`,{waitUntil:'domcontentloaded'});await idle();await page.waitForFunction(()=>document.querySelector('.model-trigger')?.textContent.includes('DeepSeek Flash'))
  await toggle().click();await idle();assert.match(await notice().innerText(),/未配置联网检索密钥/);assert.ok(await notice().getByRole('button',{name:/重新检测/}).count());assert.ok(await notice().getByRole('button',{name:/模型设置/}).count());assert.equal(count('POST','/api/v1/sessions/network-fixture/web'),0)
  await notice().getByRole('button',{name:/模型设置/}).click();assert.deepEqual(await page.evaluate(()=>window.__networkFixture.settings),['models']);await shot('network-unavailable.png')
  report.checks.push('Unavailable network remains clickable and reveals an inline reason, retry detection and a working model-settings entry without a fake save')

  available=true;await notice().getByRole('button',{name:/重新检测/}).click();await idle();await toggle().click();await on();assert.equal(webEnabled,true);assert.equal(await page.evaluate(()=>localStorage.getItem('code-agent:web:network-fixture')),'1');await toggle().click();await off();await idle();assert.equal(webEnabled,false)
  report.checks.push('After capability configuration changes, re-detection and a click enable only after POST confirmation, and a second click disables')

  failPost=true;await toggle().click();await idle();await off();assert.equal(webEnabled,false);assert.equal(await page.evaluate(()=>localStorage.getItem('code-agent:web:network-fixture')),'0');assert.match(await notice().innerText(),/联网设置保存失败/);await shot('network-save-failed.png')
  failPost=false;holdPost=true;const before=count('POST','/api/v1/sessions/network-fixture/web');await toggle().click();await wait(()=>document.querySelector('.web-toggle')?.disabled);for(let i=0;i<3;i++)await toggle().dispatchEvent('click');await page.waitForTimeout(60);assert.equal(count('POST','/api/v1/sessions/network-fixture/web'),before+1);await off();holdPost=false;gateRelease();gateRelease=null;await on();await idle()
  report.checks.push('POST failure preserves the previous confirmed setting; while save is pending repeat clicks are disabled and create only one POST')

  let capabilitiesBefore=count('GET','/api/v1/web');await page.locator('#hide').click();await page.locator('#show').click();await page.waitForTimeout(100);assert.ok(count('GET','/api/v1/web')>capabilitiesBefore)
  capabilitiesBefore=count('GET','/api/v1/web');await page.getByRole('button',{name:/选择会话模型/}).click();await page.locator('.model-root-row[data-pane="models"]').click();await page.getByRole('option',{name:'DeepSeek Pro'}).click();await page.waitForTimeout(100);assert.ok(count('GET','/api/v1/web')>capabilitiesBefore);assert.equal(currentModel,'deepseek-pro')
  report.checks.push('Showing the panel again and successfully selecting another model each refresh current network capability')

  await page.setViewportSize({width:390,height:720});await toggle().focus();await page.keyboard.press('Space');await off();await idle();await toggle().focus();await page.keyboard.press('Enter');await on();await idle();const layout=await page.locator('.send-row').evaluate(el=>{const box=el.getBoundingClientRect();return{x:box.x,right:box.right,bottom:box.bottom,width:innerWidth,height:innerHeight,bodyOverflow:document.documentElement.scrollWidth>innerWidth+1}});assert.ok(layout.x>=0&&layout.right<=layout.width+1&&layout.bottom<=layout.height+1,JSON.stringify(layout));assert.equal(layout.bodyOverflow,false);report.observations.narrow=layout;await shot('network-enabled-390.png')
  report.checks.push('At 390px, Space and Enter operate the actual toggle and the composer stays within the viewport')

  await toggle().click();await off();available=false;failCapability=true;await toggle().click();await idle();assert.match(await notice().innerText(),/检测失败|无法|不可用/);failCapability=false;available=true;await toggle().click();await on();await idle()
  report.checks.push('A failed capability request is recoverable on a later click instead of remaining permanently cached')
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpected,[]);report.observations.webEnabled=webEnabled;report.observations.localValue=await page.evaluate(()=>localStorage.getItem('code-agent:web:network-fixture'));report.passed=true
}catch(error){failure=error;report.failure=error.stack||String(error);if(page){try{const file=path.join(output,'failure.png');await page.screenshot({path:file,fullPage:true});report.screenshots.push(file);report.observations.failureDom=await page.locator('body').innerText()}catch{}}}
finally{gateRelease?.();await browser?.close();report.owned.browsersClosed=true;await vite?.close();report.owned.serverStopped=true;await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,checks:report.checks.length,report:path.join(output,'report.json'),failure:report.failure?.split('\n')[0]},null,2))}
if(failure)throw failure
