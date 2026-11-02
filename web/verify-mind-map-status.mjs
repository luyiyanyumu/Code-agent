import assert from 'node:assert/strict'
import {mkdir, writeFile} from 'node:fs/promises'
import {resolve, join} from 'node:path'

// Mount the real component in an IDE-sized flex frame. Every data method is
// isolated, including explicit retry; no backend, project or model is touched.
const base=process.argv[2]||'http://127.0.0.1:62485',origin=new URL(base).origin
const output=resolve(process.argv[3]||'../.tmp/mind-map-status-browser')
await mkdir(output,{recursive:true})
const appSource=await(await fetch(`${base}/src/App.vue`)).text()
const vuePath=appSource.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath,'Vite server required')
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const {chromium}=pw.default||pw
const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
const report={passed:false,checks:[],screenshots:[],errors:[],forbidden:[],measurements:{}}
const missing={state:'waiting',running:false,revision:1,coverage_known:true,coverage_total:646,described:24,missing:622,total:24,done:0,written:0,failed:0,
  block_reason:'model_not_configured',action:'configure_model',note:'请在设置 → 模型与连接中配置可用模型。'}
const map={version:191,change_seq:1,stats:{files:94,symbols:568,edges:82},scan:{finished_at:'2026-10-30T03:31:55Z',externals:360},modules:[],edges:{imports:[]},warnings:[],
  structure:{id:'root',kind:'dir',name:'sky-take-out',path:'',children:Array.from({length:8},(_,d)=>({id:`dir:${d}`,kind:'dir',name:`sky-module-${d}`,path:`sky-module-${d}`,
    children:Array.from({length:12},(_,f)=>({id:`file:${d}-${f}`,kind:'file',name:`EmployeeController${f}.java`,path:`sky-module-${d}/EmployeeController${f}.java`,symbols:[],children:[]}))}))}}
const html=`<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module">
import {createApp,h,ref} from ${JSON.stringify(vuePath)};
import MindMap from '/src/components/code/MindMap.vue'; import {api} from '/src/api.js'; import '/src/style.css';
const map=${JSON.stringify(map)},initial=${JSON.stringify(missing)};let status={...initial};window.fixtureCalls={retry:0,settings:[]};
api.code={map:async()=>map,indexStatus:async()=>({change_seq:1,scanning:false,briefs:status}),features:async()=>({domains:[]}),
symbolPurpose:async()=>({headline:'离线测试节点',facts:[]}),
fillFeatureBriefs:async(pid,options)=>{window.fixtureCalls.retry++;window.fixtureCalls.retryOptions=options;status={...initial,state:'running',running:true,action:'',block_reason:'',note:'',revision:2};return status}};
createApp({setup(){const hidden=ref(true);window.revealMap=()=>{hidden.value=false};window.hideMap=()=>{hidden.value=true};window.setMapJob=value=>{status=value};
return()=>h('div',{class:'fixture-ide'},[h('header',{class:'fixture-header'},'Code-agent · 思维导图布局测试'),h('div',{class:'fixture-workspace'},[h('aside',{class:'fixture-left'},'项目文件'),
h('main',{class:'fixture-center'},[h(MindMap,{projectId:'offline-map-fixture',tree:'task:test',style:{display:hidden.value?'none':''},onOpenSettings:section=>window.fixtureCalls.settings.push(section)})]),h('aside',{class:'fixture-right'},'代码助手')])])}}).mount('#app');
</script><style>body{overflow:hidden}.fixture-ide{height:100dvh;display:flex;flex-direction:column}.fixture-header{height:44px;flex:none;padding:10px 16px;border-bottom:1px solid var(--border);font-weight:600}.fixture-workspace{display:flex;flex:1;min-height:0}.fixture-left{width:240px;flex:none;background:var(--panel-soft);padding:20px;border-right:1px solid var(--border)}.fixture-right{width:330px;flex:none;padding:20px;border-left:1px solid var(--border)}.fixture-center{display:flex;flex:1;min-width:0;min-height:0;flex-direction:column}@media(max-width:1000px){.fixture-left,.fixture-right{display:none}}</style></body></html>`
let page
try{
  const context=await browser.newContext({viewport:{width:1440,height:960},serviceWorkers:'block'})
  await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin!==origin||url.pathname.startsWith('/api/')){report.forbidden.push(url.pathname);return route.abort()}
    if(url.pathname==='/mind-map-fixture')return route.fulfill({contentType:'text/html; charset=utf-8',body:html});return route.continue()})
  page=await context.newPage();page.setDefaultTimeout(20000);page.on('pageerror',error=>report.errors.push(error.message))
  await page.goto(`${base}/mind-map-fixture`)
  await page.waitForFunction(()=>typeof window.revealMap==='function')
  await page.locator('.map .node').first().waitFor({state:'attached'})
  await page.evaluate(()=>window.revealMap())
  const visibleRoot=async()=>page.locator('.map').evaluate(el=>{const svg=el.querySelector('svg').getBoundingClientRect(),root=el.querySelector('.node[data-depth="0"]').getBoundingClientRect();return root.width>0&&root.height>0&&root.left>=svg.left-1&&root.right<=svg.right+1&&root.top>=svg.top-1&&root.bottom<=svg.bottom+1})
  await page.waitForFunction(()=>{const svg=document.querySelector('.map svg')?.getBoundingClientRect(),node=document.querySelector('.map .node[data-depth="0"]')?.getBoundingClientRect();return svg&&node&&node.height>0&&node.top>=svg.top-1&&node.bottom<=svg.bottom+1})
  assert.equal(await visibleRoot(),true,'map loaded while hidden fits when revealed')
  report.checks.push('hidden initial load fits the root after the retained map becomes visible')
  const dimensions=()=>page.locator('.map').evaluate(el=>{const box=node=>{const b=node.getBoundingClientRect();return{x:b.x,y:b.y,width:b.width,height:b.height,bottom:b.bottom}};return{map:box(el),status:box(el.querySelector('.description-status')),actions:box(el.querySelector('.map-actions')),canvas:box(el.querySelector('.canvas')),meta:box(el.querySelector('.meta')),bodyOverflow:document.documentElement.scrollWidth>innerWidth+1}})
  const wide=await dimensions();report.measurements.desktop=wide
  assert.ok(wide.status.height<=34);assert.ok(wide.canvas.height>600);assert.ok(wide.meta.y>=wide.canvas.bottom-1);assert.ok(wide.actions.bottom<=wide.status.y+1);assert.equal(wide.bodyOverflow,false)
  assert.equal(await page.locator('.description-scope').count(),0)
  await page.screenshot({path:join(output,'map-desktop.png')});report.screenshots.push(join(output,'map-desktop.png'))
  await page.getByRole('button',{name:/功能描述等待模型配置.*查看详情/}).click()
  await page.getByRole('region',{name:'功能描述详情'}).waitFor()
  assert.match(await page.getByRole('region',{name:'功能描述详情'}).innerText(),/646|622/)
  await page.screenshot({path:join(output,'map-status-details.png')});report.screenshots.push(join(output,'map-status-details.png'))
  await page.keyboard.press('Escape');assert.equal(await page.getByRole('region',{name:'功能描述详情'}).count(),0)
  assert.match(await page.locator(':focus').getAttribute('aria-label'),/查看详情/)
  await page.getByRole('button',{name:'模型设置',exact:true}).click();assert.deepEqual(await page.evaluate(()=>window.fixtureCalls.settings),['models'])
  assert.equal(await page.evaluate(()=>window.fixtureCalls.retry),0,'opening settings does not generate descriptions')
  report.checks.push('compact configuration status exposes complete details, Escape/focus restoration and model settings without hidden generation')
  await page.evaluate(()=>window.setMapJob({state:'waiting',running:false,revision:1,coverage_known:true,coverage_total:646,described:24,missing:622,total:24,done:0,written:0,failed:24,error_code:'model_request_failed',action:'retry',error:'',note:'模型请求失败，请检查连接后重试。'}))
  await page.getByRole('button',{name:'重试',exact:true}).waitFor();await page.getByRole('button',{name:'重试',exact:true}).click()
  assert.equal(await page.evaluate(()=>window.fixtureCalls.retry),1);assert.deepEqual(await page.evaluate(()=>window.fixtureCalls.retryOptions),{tree:'task:test'})
  await page.locator('.description-label').filter({hasText:'自动补功能描述'}).waitFor()
  report.checks.push('only an explicit retry starts one description request for the selected worktree')
  for(const width of [768,390,320]){
    await page.setViewportSize({width,height:844});await page.waitForTimeout(100)
    const size=await dimensions();report.measurements[width]=size;assert.ok(size.status.height<=34);assert.ok(size.canvas.height>350);assert.equal(size.bodyOverflow,false)
    assert.equal(await visibleRoot(),true)
    await page.screenshot({path:join(output,`map-${width}.png`)});report.screenshots.push(join(output,`map-${width}.png`))
  }
  report.checks.push('768,390 and320px layouts keep actions scrollable, status one line and root visible in the remaining canvas')
  const transform=()=>page.locator('.map svg > g').getAttribute('transform')
  const beforeZoom=await transform()
  await page.locator('.map svg').dispatchEvent('wheel',{deltaY:-100,clientX:120,clientY:500})
  await page.waitForFunction(before => document.querySelector('.map svg > g')?.getAttribute('transform') !== before, beforeZoom)
  const manuallyZoomed=await transform();assert.notEqual(manuallyZoomed,beforeZoom)
  await page.evaluate(()=>window.hideMap());await page.waitForTimeout(50);await page.evaluate(()=>window.revealMap());await page.waitForTimeout(100)
  assert.equal(await transform(),manuallyZoomed)
  await page.setViewportSize({width:768,height:900});await page.waitForTimeout(100)
  assert.equal(await transform(),manuallyZoomed)
  report.checks.push('manual zoom and pan survive retained-map visibility changes and viewport resizing')
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.forbidden,[]);report.passed=true
}catch(error){report.error=error.stack||String(error);if(page)await page.screenshot({path:join(output,'failure.png')}).catch(()=>{});process.exitCode=1}
finally{await writeFile(join(output,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify(report))}
