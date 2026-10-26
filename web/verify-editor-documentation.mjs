import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Standalone real EditorPane, Monaco and Chrome. No user project, backend,
// language process, model call or file save can be reached by this fixture.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/editor-documentation-browser', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], errors = [], unexpected = [], screenshots = [], measurements = {}
const source = `package demo;\n\npublic class SessionService {\n    public String projectLead(String input) {\n        return input.trim();\n    }\n}\n` + Array.from({ length: 65 }, (_, i) => `// editor position preserved ${i}`).join('\n')
const otherSource = 'package demo;\nclass OtherService {\n    void otherSymbol() {}\n}\n'
const markdown = '# 为什么要有这一层\n\n原来后端是**完全无状态**的：对话历史由前端带回，超出的直接丢弃。\n\n## 事件 ≠ 上下文\n\n落库的事件是**全量**的，而发给模型的上下文是投影。\n\n' +
  Array.from({ length: 28 }, (_, i) => `### ${i + 1}. 执行步骤\n\n- 读取项目事件日志并校验版本。\n- 上下文保留用户意图与最终结果，审计记录保留工具输出。\n\n`).join('') +
  '\n<h1 id="raw-html-probe">HTML 不执行</h1>\n<img src="https://fixture.invalid/remote.png" onerror="window.__documentationAttack=1">\n\n[执行命令](command:fixture.attack)\n\n![远程架构图片](https://fixture.invalid/diagram.png)\n\n## 完整说明末尾标记\n\nDOCUMENTATION_TAIL_9e52\n'
const signature = 'public java.util.Map<String, java.util.List<ProjectAgent>> projectLead(String input, java.util.function.Consumer<AuditEvent> auditor)'
const docs = [{ language: 'java', value: signature }, { kind: 'markdown', value: markdown }, { kind: 'plaintext', value: '纯文本原样：**不是粗体**\n@param input 输入\nPLAIN_TEXT_TAIL_28d7' }]
const fixture = { source, otherSource, docs }
// Reuse Vite's exact optimized Vue URL; loading it without the version query
// would create a second runtime and invalidate component lifecycle hooks.
const componentSource = await (await fetch(`${origin}/src/components/code/EditorPane.vue`)).text()
const vueModule = componentSource.match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vueModule && new URL(vueModule, origin).origin === origin, 'Cannot locate fixture Vue runtime')
const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{margin:0;min-width:0;height:100%;}body{background:#f4f6f9;font:14px system-ui;}#app{height:100%;display:flex;flex-direction:column;min-width:0;} .fixture-head{padding:10px 16px;display:flex;align-items:center;gap:12px;background:white;border-bottom:1px solid #e3e7ef;}.fixture-head b{font-size:14px;} .fixture-shell{flex:1;min-height:0;min-width:0;padding:12px;} .fixture-shell>.pane{border:1px solid #e3e7ef;border-radius:8px;overflow:hidden;}
</style></head><body><div id="app"></div><script type="module">
import {createApp, h, ref, computed, nextTick} from '${vueModule}';
import EditorPane from '/src/components/code/EditorPane.vue';
import {ensureMonaco} from '/src/lib/monacoSetup.js';
import '/src/style.css';
const fixture=${JSON.stringify(fixture).replaceAll('<', '\\u003c')};
const state=window.__documentationFixture={frames:[],sockets:[],pending:[],hold:false,saves:[],changes:[],attacks:0};
class FixtureSocket extends EventTarget{ static CONNECTING=0;static OPEN=1;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;this.docs=new Map();this.isLsp=new URL(url,location.href).pathname.endsWith('/lsp/stream');state.sockets.push(this);queueMicrotask(()=>{this.readyState=1;this.emit('open',new Event('open'));if(this.isLsp)this.message({method:'code-agent/ready',params:{server:'Fixture Java',capabilities:{hoverProvider:true,textDocumentSync:{change:1,save:{includeText:true}}}}})})}
 emit(type,event){this.dispatchEvent(event);this['on'+type]?.(event)}
 message(frame){if(this.readyState===1)this.emit('message',new MessageEvent('message',{data:JSON.stringify(frame)}))}
 send(raw){if(!this.isLsp)return;const frame=JSON.parse(raw);state.frames.push(frame);const uri=frame.params?.textDocument?.uri;if(frame.method==='textDocument/didOpen')this.docs.set(uri,frame.params.textDocument);if(frame.method==='textDocument/didChange'){const doc=this.docs.get(uri);if(doc){doc.text=frame.params.contentChanges[0].text;doc.version=frame.params.textDocument.version}}if(frame.method==='textDocument/didClose')this.docs.delete(uri);if(frame.id===undefined)return;
 const result=frame.method==='textDocument/hover'?{contents:uri?.endsWith('/OtherService.java')?[{language:'java',value:'void otherSymbol()'},{kind:'markdown',value:'OTHER_SYMBOL_ONLY'}]:fixture.docs,range:{start:{line:3,character:18},end:{line:3,character:29}}}:null;
 if(state.hold&&frame.method==='textDocument/hover')state.pending.push(()=>this.message({id:frame.id,result}));else queueMicrotask(()=>this.message({id:frame.id,result}))}
 close(){this.readyState=3;this.emit('close',new CloseEvent('close'))}
}
window.WebSocket=FixtureSocket;
const first={path:'src/demo/SessionService.java',text:fixture.source,lang:'java',sha1:'fixture-a',uri:'file:///E:/documentation-fixture/src/demo/SessionService.java',total_lines:fixture.source.split('\n').length,editable:true,size:fixture.source.length};
const second={path:'src/demo/OtherService.java',text:fixture.otherSource,lang:'java',sha1:'fixture-b',uri:'file:///E:/documentation-fixture/src/demo/OtherService.java',total_lines:4,editable:true,size:fixture.otherSource.length};
const current=ref(first),readonly=ref(false),pane=ref(null);const files=new Map([[first.path,first],[second.path,second]]);const docsRows=computed(()=>[...files.values()].map(file=>({key:'documentation-fixture/main/'+file.path,file,uri:file.uri,path:file.path,lang:file.lang,text:file.text})));
createApp({setup(){return()=>h('div',{id:'app'},[h('header',{class:'fixture-head'},[h('b','代码说明 · 本地隔离验证'),h('button',{id:'first',onClick:()=>current.value=files.get(first.path)},'当前文件'),h('button',{id:'second',onClick:()=>current.value=files.get(second.path)},'另一文件'),h('button',{id:'readonly',onClick:()=>readonly.value=!readonly.value},readonly.value?'只读':'可编辑')]),h('main',{class:'fixture-shell'},[h(EditorPane,{ref:pane,file:current.value,documentKey:'documentation-fixture/main/'+current.value.path,documents:docsRows.value,projectId:'documentation-fixture',sessionId:'documentation-session',readonly:readonly.value,languageServers:[{id:'fixture-java',name:'Fixture Java',languages:['java'],state:'installed',available:true}],lspServer:{id:'fixture-java',name:'Fixture Java',available:true},onChange:change=>{state.changes.push(change);const file=files.get(change.path);if(file)file.text=change.text},onSave:event=>state.saves.push(event)})])])}}).mount('#app');
Object.assign(state,{pane,current,readonly,nextTick,monaco:ensureMonaco(),files,signature:fixture.docs[0].value,markdown:fixture.docs[1].value,release(){state.hold=false;for(const deliver of state.pending.splice(0))deliver()}});
Object.defineProperty(state,'editor',{get(){return state.monaco.editor.getEditors()[0]}});
</script></body></html>`
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let failure, page
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 930 }, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin || url.pathname.startsWith('/api/')) { unexpected.push(url.href); return route.abort() }
    if (url.pathname === '/__editor-documentation-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    return route.continue()
  })
  page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/__editor-documentation-fixture`, { waitUntil: 'networkidle' })
  const tool = () => page.locator('.language-tool').filter({ hasText: /^说明$/ })
  const drawer = () => page.getByRole('complementary', { name: '代码完整说明' })
  await tool().waitFor(); await page.waitForFunction(() => ![...document.querySelectorAll('.language-tool')].find(el=>el.textContent==='说明')?.disabled)
  const setCursor = async () => {await page.waitForFunction(()=>!!window.__documentationFixture.editor);await page.evaluate(() => {const editor=window.__documentationFixture.editor;editor.setPosition({lineNumber:4,column:23});editor.revealLineInCenter(4);editor.focus()})}
  const shot = async name => {const filename=path.join(output,name);await page.screenshot({path:filename,fullPage:true});screenshots.push(filename)}
  const snapshot = () => page.evaluate(() => {const editor=window.__documentationFixture.editor;return {text:editor.getValue(),selection:editor.getSelection(),top:editor.getScrollTop(),left:editor.getScrollLeft(),uri:editor.getModel().uri.toString()}})
  await setCursor()
  await page.evaluate(() => window.__documentationFixture.editor.getAction('editor.action.showHover').run())
  const native = page.locator('.monaco-resizable-hover').filter({visible:true})
  await native.waitFor()
  await page.waitForTimeout(250)
  measurements.hover = await native.evaluate(el => {const hover=el.querySelector('.monaco-hover'),content=el.querySelector('.monaco-hover-content');const rect=element=>{const b=element.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height}};return {outer:rect(el),hover:rect(hover),content:rect(content),scrollables:[...el.querySelectorAll('.monaco-scrollable-element')].map(item=>({rect:rect(item),height:item.style.height,overflow:getComputedStyle(item).overflow,scroll:item.scrollHeight,client:item.clientHeight})),font:getComputedStyle(content).fontSize,headers:[...content.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(el=>getComputedStyle(el).fontSize),scroll:content.scrollHeight,client:content.clientHeight}})
  const geo=measurements.hover
  if (geo.outer.width > 566 || geo.outer.height > 390 || Math.abs(geo.outer.width - geo.hover.width) > 8 || Math.abs(geo.outer.height - geo.hover.height) > 8) errors.push(`Hover outer geometry ${JSON.stringify(geo)}`)
  else checks.push('Native hover outer shell and reading surface stay within 560 × 380 without empty padding')
  assert.ok(parseFloat(geo.font)<=14);assert.ok(geo.headers.every(value=>parseFloat(value)<=14));checks.push('Native documentation headings and body use compact 13px typography')
  assert.ok(geo.scroll>geo.client);checks.push('Long native documentation uses a scrollable reading surface')
  await shot('editor-hover-long.png')
  await native.evaluate(el=>{el.__wheels=[];el.addEventListener('wheel',event=>el.__wheels.push({y:event.deltaY,target:event.target.className}),true)})
  // Monaco intentionally limits an individual wheel gesture. Deliver real
  // repeated gestures instead of assuming a large delta jumps to the bottom.
  await native.locator('.monaco-hover-content').hover()
  const wheelBox=await native.locator('.monaco-hover-content').boundingBox()
  measurements.wheelTarget=await page.evaluate(({x,y})=>{const target=document.elementFromPoint(x,y);return {x,y,tag:target?.tagName,className:target?.className,insideHover:!!target?.closest('.monaco-resizable-hover')}},{x:wheelBox.x+wheelBox.width/2,y:wheelBox.y+wheelBox.height/2})
  assert.equal(measurements.wheelTarget.insideHover,true)
  for(let i=0;i<90;i++) {
    await page.mouse.wheel(0,1500);await page.waitForTimeout(16)
    if(await native.evaluate(el=>{const content=el.querySelector('.monaco-hover-content');return content.scrollTop>=content.scrollHeight-content.clientHeight-2}))break
  }
  await page.waitForTimeout(150)
  const nativeTail = await native.evaluate(el=>{const surface=el.getBoundingClientRect(),content=el.querySelector('.monaco-hover-content');const match=[...el.querySelectorAll('p')].find(item=>item.textContent.includes('PLAIN_TEXT_TAIL_28d7'));const b=match?.getBoundingClientRect();return b?{top:b.top,bottom:b.bottom,surfaceTop:surface.top,surfaceBottom:surface.bottom,scrollTop:content.scrollTop,scrollHeight:content.scrollHeight,clientHeight:content.clientHeight,wheels:el.__wheels}:null})
  measurements.nativeTail=nativeTail
  assert.ok(nativeTail?.wheels.length>0,'No real wheel event reached native hover')
  if(!nativeTail||nativeTail.top<nativeTail.surfaceTop||nativeTail.bottom>nativeTail.surfaceBottom)errors.push(`Native scroll cannot reveal complete tail ${JSON.stringify(nativeTail)}`)
  else checks.push('Native hover can scroll to the final plaintext tail without clipping')
  await shot('editor-hover-tail.png')
  await page.keyboard.press('Escape'); await tool().click(); await drawer().waitFor()
  await page.waitForFunction(() => document.querySelector('.documentation-body')?.getAttribute('aria-busy')==='false')
  assert.match(await drawer().innerText(),/语言服务[\s\S]*SessionService\.java:4/);checks.push('Full documentation names the language-service source and query file/line')
  assert.deepEqual(await drawer().locator('.documentation-section').evaluateAll(els=>els.map(el=>el.dataset.sectionKind)),['markdown','code','markdown','plaintext']);checks.push('Full documentation adds Java classification and keeps signature, Markdown and literal text in server order')
  assert.equal(await drawer().locator('.documentation-section[data-section-kind=code] code').innerText(),signature+'\n')
  assert.match(await drawer().innerText(),/DOCUMENTATION_TAIL_9e52[\s\S]*PLAIN_TEXT_TAIL_28d7/);checks.push('Full documentation retains complete long content and every tail marker')
  assert.match(await drawer().locator('.documentation-plaintext').innerText(),/\*\*不是粗体\*\*/);checks.push('Plaintext Javadoc markers stay literal')
  assert.equal(await drawer().locator('img,script,iframe,svg,#raw-html-probe').count(),0)
  assert.equal(await drawer().locator('a[href^="command:"]').count(),0)
  assert.equal(await page.evaluate(()=>window.__documentationAttack||0),0);checks.push('Full documentation escapes raw HTML and command links and never renders remote images')
  measurements.drawer=await drawer().evaluate(el=>{const b=el.getBoundingClientRect();return {width:b.width,height:b.height,headers:[...el.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(el=>getComputedStyle(el).fontSize)}})
  assert.ok(measurements.drawer.width<=542);assert.ok(measurements.drawer.headers.every(value=>parseFloat(value)<=14));checks.push('Full drawer has restrained width and 14px maximum headings')
  await drawer().locator('.documentation-body').evaluate(el=>el.scrollTop=el.scrollHeight)
  await drawer().locator('.documentation-plaintext').waitFor();await shot('editor-documentation-tail.png')
  await drawer().locator('.documentation-body').evaluate(el=>el.scrollTop=0);await shot('editor-documentation-full.png')
  const before=await snapshot();await page.keyboard.press('Escape');await page.waitForTimeout(100)
  if(await drawer().count()){errors.push('Escape from the actual toolbar-open focus does not close documentation');await drawer().getByRole('button',{name:'关闭完整说明'}).click()}
  else checks.push('Escape closes the drawer from its actual opening focus')
  assert.deepEqual(await snapshot(),before);checks.push('Closing the drawer preserves editor text, caret and scroll')
  await tool().click();await drawer().waitFor();await drawer().getByRole('button',{name:'关闭完整说明'}).click();assert.deepEqual(await snapshot(),before);checks.push('Close button returns to the exact editor view without saving')
  await page.locator('#readonly').click();await tool().click();await drawer().waitFor();assert.match(await drawer().innerText(),/DOCUMENTATION_TAIL_9e52/);assert.equal(await page.locator('.bar button').filter({hasText:/^保存$/}).isDisabled(),true);checks.push('Read-only files can inspect documentation without enabling Save')
  await drawer().getByRole('button',{name:'关闭完整说明'}).click();await page.locator('#readonly').click()
  await page.evaluate(()=>{const e=window.__documentationFixture.editor;e.executeEdits('fixture',[{range:new window.__documentationFixture.monaco.Range(7,2,7,2),text:' // UNSAVED_DRAFT_2af1'}]);e.setPosition({lineNumber:4,column:23});e.setScrollTop(0)})
  const draft=await snapshot();await tool().click();await drawer().waitFor();await drawer().getByRole('button',{name:'关闭完整说明'}).click();assert.deepEqual(await snapshot(),draft);assert.match((await snapshot()).text,/UNSAVED_DRAFT_2af1/);checks.push('Opening and closing documentation leaves unsaved draft bytes intact')
  await page.evaluate(()=>{window.__documentationFixture.hold=true});await tool().click();await drawer().waitFor();await page.waitForFunction(()=>window.__documentationFixture.pending.length>0)
  await page.locator('#second').click();await drawer().waitFor({state:'detached'});await page.evaluate(()=>window.__documentationFixture.release());await page.waitForTimeout(100)
  assert.equal(await drawer().count(),0);assert.ok(await page.evaluate(()=>window.__documentationFixture.frames.some(frame=>frame.method==='$/cancelRequest')));checks.push('Switching files cancels a pending documentation request and rejects its late response')
  await page.evaluate(()=>{const e=window.__documentationFixture.editor;e.setPosition({lineNumber:3,column:15});e.focus()})
  await tool().click();await drawer().waitFor();await page.waitForFunction(()=>document.querySelector('.documentation-body')?.textContent.includes('OTHER_SYMBOL_ONLY'));assert.doesNotMatch(await drawer().innerText(),/DOCUMENTATION_TAIL_9e52/);checks.push('New document explanation cannot inherit the previous symbol content')
  await drawer().getByRole('button',{name:'关闭完整说明'}).click()
  await page.evaluate(()=>window.__documentationFixture.editor.getAction('code-agent.symbol-documentation').run());await drawer().waitFor();checks.push('Monaco context action opens the same full-documentation drawer')
  await drawer().getByRole('button',{name:'关闭完整说明'}).click();await page.locator('#first').click();await setCursor()
  await page.setViewportSize({width:390,height:844});await tool().click();await drawer().waitFor();await page.waitForFunction(()=>document.querySelector('.documentation-body')?.textContent.includes('DOCUMENTATION_TAIL_9e52'))
  measurements.mobile=await page.evaluate(()=>{const drawer=document.querySelector('.symbol-documentation').getBoundingClientRect();return {page:document.documentElement.scrollWidth,viewport:innerWidth,drawer:{x:drawer.x,width:drawer.width,right:drawer.right},body:document.querySelector('.documentation-body').scrollWidth}})
  if(measurements.mobile.page>392||measurements.mobile.drawer.right>391||measurements.mobile.drawer.x<0)errors.push(`Mobile overflow ${JSON.stringify(measurements.mobile)}`)
  else checks.push('390px viewport contains the complete drawer without page horizontal overflow')
  await shot('editor-documentation-mobile.png')
  await drawer().getByRole('button',{name:'关闭完整说明'}).click();await setCursor()
  await page.evaluate(()=>window.__documentationFixture.editor.getAction('editor.action.showHover').run());await native.waitFor();await page.waitForTimeout(250)
  measurements.mobileHover=await native.evaluate(el=>{const outer=el.getBoundingClientRect(),inner=el.querySelector('.monaco-hover').getBoundingClientRect();return{outer:{x:outer.x,width:outer.width,right:outer.right,height:outer.height},inner:{width:inner.width,height:inner.height},page:document.documentElement.scrollWidth,viewport:innerWidth}})
  if(measurements.mobileHover.outer.x<0||measurements.mobileHover.outer.right>391||measurements.mobileHover.page>392||Math.abs(measurements.mobileHover.outer.width-measurements.mobileHover.inner.width)>8)errors.push(`Mobile native overflow ${JSON.stringify(measurements.mobileHover)}`)
  else checks.push('390px native hover stays inside the viewport without an oversized outer shell')
  await shot('editor-hover-mobile.png')
  assert.equal(await page.evaluate(()=>window.__documentationFixture.saves.length),0);checks.push('Documentation actions never issue Save or modify project files')
  if(unexpected.length)errors.push(`Blocked unexpected requests: ${unexpected.join(', ')}`)
  assert.deepEqual(errors,[])
}catch(error){failure=error;errors.push(error.message);if(page)await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{})}
finally{await browser.close();fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({checks,errors,unexpected,measurements,screenshots},null,2))}
if(failure)throw failure
console.log(JSON.stringify({passed:checks.length,report:path.join(output,'report.json')}))
