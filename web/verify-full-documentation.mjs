import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

// Real EditorPane, Monaco and LSP providers. Every symbol, response and optional
// project record is an in-memory fixture. No workspace/model/write API exists.
const origin = new URL(process.argv[2] || 'http://127.0.0.1:5201').origin
const output = path.resolve(process.argv[3] || '../.tmp/full-documentation-20261031/browser')
fs.mkdirSync(output, { recursive: true })
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const vueModule = (await (await fetch(`${origin}/src/components/code/EditorPane.vue`)).text()).match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vueModule, 'Vite must serve the actual component')
const report = { passed: false, checks: [], errors: [], unexpected: [], screenshots: [], observations: {}, measurements: {}, baseline: { available: false, note: 'No earlier SymbolDocumentation source was preserved; snapshot-start.vue is the current in-progress source, not an old-product baseline.' } }
const summary = '读取当前用户的会话信息，并按租户隔离返回可访问的数据。'
const signature = 'public java.util.Map<String, java.util.List<String>> demo.SessionService.loadSession(String userToken, String tenant, java.util.Map<String, java.util.List<String>> labels) throws InvalidSessionException'
const pySignature = 'def inspect_session(session_id: str, user_id: str) -> dict[str, str]'
const java = 'package demo;\npublic class SessionService {\n    public java.util.Map<String, java.util.List<String>> loadSession(String userToken, String tenant, java.util.Map<String, java.util.List<String>> labels) { return null; }\n}'
const python = 'def inspect_session(session_id: str, user_id: str) -> dict[str, str]:\n    return {"id": session_id, "user": user_id}\n\ninspect_session("demo", "owner")'
const details = '\n\n## 参数\n\n| 参数 | 说明 |\n| --- | --- |\n| `userToken` | 会话令牌，必须属于当前用户。 |\n| `tenant` | 用于隔离查询的租户编号。 |\n| `labels` | 用于过滤会话的标签集合。 |\n\n## 返回值\n\n返回当前租户内可见的会话映射；不会返回其他租户的数据。\n\n## 异常\n\n`InvalidSessionException`：令牌已过期或不属于当前用户。\n\n## 使用示例\n\n```java\nvar sessions = service.loadSession("user-token", "tenant-a", labels);\nSystem.out.println(sessions.size());\n```\n\n' + Array.from({ length: 24 }, (_, i) => `### 行为说明 ${i + 1}\n\n保留当前用户和租户的权限边界。每条会话记录包含可见标签、失效时间和读取状态；该调用不会保存或修改编辑器中的文件。`).join('\n\n') + '\n\n完整说明末尾 FULL_READER_TAIL_67e2\n\n[会话接口参考](https://docs.fixture.invalid/session)'
const rawOriginal = 'Returns the current user session scoped to the requested tenant.\n\nParameters: userToken, tenant and labels.\n\nReturns a mapping of visible sessions.\n\nThrows InvalidSessionException when the token expires.\n\n```java\nvar sessions = service.loadSession("user-token", "tenant-a", labels);\n```\n\nORIGINAL_READER_TAIL_3f51'
const pySummary = '读取指定用户的会话信息，返回当前用户可见的会话字典。'
const pyDetails = details.replace('| `userToken` | 会话令牌，必须属于当前用户。 |', '| `session_id` | 要读取的会话编号。 |')
  .replace('| `tenant` | 用于隔离查询的租户编号。 |', '| `user_id` | 允许读取此会话的用户编号。 |')
  .replace('| `labels` | 用于过滤会话的标签集合。 |\n', '')
  .replace('返回当前租户内可见的会话映射；不会返回其他租户的数据。', '返回当前用户可见的会话字典，包含会话编号与用户编号。')
  .replace('`InvalidSessionException`：令牌已过期或不属于当前用户。', '`ValueError`：会话已过期或不属于当前用户。')
  .replace('```java\nvar sessions = service.loadSession("user-token", "tenant-a", labels);\nSystem.out.println(sessions.size());\n```', '```python\nsession = inspect_session("session-123", "user-456")\nprint(session["id"])\n```')
const pyOriginal = 'Returns a session dictionary visible to the requested user.\n\nParameters: session_id and user_id.\n\nThrows ValueError when the session expires.\n\n```python\nsession = inspect_session("session-123", "user-456")\n```\n\nORIGINAL_READER_TAIL_3f51'
const fixture = { summary, pySummary, signature, pySignature, java, python, details, pyDetails, rawOriginal, pyOriginal }
const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{margin:0;height:100%;overflow:hidden}body{font:14px system-ui;background:#f6f7fb}#app{height:100%;display:flex;flex-direction:column;min-width:0}.fixture-head{height:40px;display:flex;align-items:center;gap:8px;padding:0 12px;background:white;border-bottom:1px solid #dce2ec;flex:none;overflow-x:auto;white-space:nowrap}.fixture-head b{margin-right:auto}.fixture-workspace{display:flex;flex:1;min-height:0;min-width:0}.fixture-main{min-width:0;flex:1;display:flex;padding:12px}.fixture-main>.pane{border:1px solid #dce2ec;overflow:hidden;border-radius:8px}.fixture-ai{width:290px;flex:none;border-left:1px solid #dce2ec;background:white;padding:18px;box-sizing:border-box;color:#64748b}.fixture-ai h2{font-size:14px;color:#1e293b}@media(max-width:900px){.fixture-ai{display:none}.fixture-head b{display:none}.fixture-main{padding:6px}}
</style></head><body><div id="app"></div><script type="module">
import {createApp,h,ref,computed,nextTick} from '${vueModule}';
import EditorPane from '/src/components/code/EditorPane.vue';
import {ensureMonaco} from '/src/lib/monacoSetup.js';
import '/src/style.css';
const fixture=${JSON.stringify(fixture).replaceAll('<', '\u003c')};
const state=window.__fullDocumentationFixture={frames:[],sockets:[],changes:[],saves:[],pending:[],hold:false,failNextHover:false,resultVersion:0};
const legend={tokenTypes:['class','method','function'],tokenModifiers:['declaration']};
function response(uri,position){const py=uri.endsWith('.py'),signature=py?fixture.pySignature:fixture.signature;const summary=state.resultVersion?'重试后读取会话，返回当前用户可见的数据。':py?fixture.pySummary:fixture.summary;return {contents:[{language:py?'python':'java',value:signature},{language:py?'python':'java',value:signature},{kind:'markdown',value:summary+(py?fixture.pyDetails:fixture.details)},'com',{kind:'markdown',value:'Source: *sky-server*'},{kind:'plaintext',value:'原始文本行：**不解析成粗体**\n纯文本末尾 PLAIN_FULL_TAIL_7d6b'}],documentation_translation:{language:'zh-CN',source:'configured-model',status:'ready'},_code_agent_original:{contents:[{language:py?'python':'java',value:signature},{kind:'markdown',value:py?fixture.pyOriginal:fixture.rawOriginal},'com',{kind:'markdown',value:'Source: *sky-server*'}]},range:{start:{line:position.line,character:py?4:59},end:{line:position.line,character:py?19:70}}}}
function semantic(text){let previousLine=0,previousColumn=0;const data=[];for(const[line,value]of text.split('\n').entries())for(const match of value.matchAll(/\b[A-Za-z_$][\w$]*\b/g)){const type=match[0]==='SessionService'?'class':match[0]==='loadSession'?'method':match[0]==='inspect_session'?'function':null;if(type){const delta=line-previousLine;data.push(delta,delta?match.index:match.index-previousColumn,match[0].length,legend.tokenTypes.indexOf(type),1);previousLine=line;previousColumn=match.index}}return{data}}
class FixtureSocket extends EventTarget{
static CONNECTING=0;static OPEN=1;static CLOSING=2;static CLOSED=3;
constructor(url){super();this.url=String(url);this.docs=new Map();this.readyState=0;this.isLsp=new URL(url,location.href).pathname.endsWith('/lsp/stream');state.sockets.push(this);queueMicrotask(()=>{this.readyState=1;this.emit('open',new Event('open'));if(this.isLsp)this.message({method:'code-agent/ready',params:{server:'Fixture language service',capabilities:{hoverProvider:true,textDocumentSync:{change:1,save:{includeText:true}},semanticTokensProvider:{legend,full:true}}}})})}
emit(type,event){this.dispatchEvent(event);this['on'+type]?.(event)}message(frame){if(this.readyState===1)this.emit('message',new MessageEvent('message',{data:JSON.stringify(frame)}))}
send(raw){if(!this.isLsp)return;const frame=JSON.parse(raw);state.frames.push(frame);const uri=frame.params?.textDocument?.uri;if(frame.method==='textDocument/didOpen')this.docs.set(uri,{...frame.params.textDocument});if(frame.method==='textDocument/didChange'){const d=this.docs.get(uri);if(d){d.text=frame.params.contentChanges[0].text;d.version=frame.params.textDocument.version}}if(frame.method==='textDocument/didClose')this.docs.delete(uri);if(frame.id===undefined)return;const text=this.docs.get(uri)?.text||'';const fail=frame.method==='textDocument/hover'&&state.failNextHover;if(fail)state.failNextHover=false;const result=frame.method==='textDocument/hover'?response(uri,frame.params.position):frame.method==='textDocument/semanticTokens/full'?semantic(text):null;const deliver=()=>this.message(fail?{id:frame.id,error:{code:-32603,message:'FIXTURE_FULL_DOC_FAILURE'}}:{id:frame.id,result});if(state.hold&&frame.method==='textDocument/hover')state.pending.push(deliver);else queueMicrotask(deliver)}
close(){this.readyState=3;this.emit('close',new CloseEvent('close'))}}
window.WebSocket=FixtureSocket;
const lang=ref('java'),pane=ref(null),files={java:{path:'src/demo/SessionService.java',text:fixture.java,lang:'java',sha1:'java-fixture',uri:'file:///E:/full-documentation-fixture/src/demo/SessionService.java',total_lines:fixture.java.split('\n').length,editable:true,size:fixture.java.length},python:{path:'inspect_session.py',text:fixture.python,lang:'python',sha1:'py-fixture',uri:'file:///E:/full-documentation-fixture/inspect_session.py',total_lines:fixture.python.split('\n').length,editable:true,size:fixture.python.length}};const file=computed(()=>files[lang.value]),servers=[{id:'fixture-java',name:'Fixture Java',languages:['java'],state:'installed',available:true},{id:'fixture-python',name:'Fixture Python',languages:['python'],state:'installed',available:true}];
createApp({setup(){return()=>h('div',{id:'app'},[h('header',{class:'fixture-head'},[h('b','Code-agent · 完整说明验收'),h('button',{id:'java',onClick:()=>lang.value='java'},'Java'),h('button',{id:'python',onClick:()=>lang.value='python'},'Python')]),h('div',{class:'fixture-workspace'},[h('main',{class:'fixture-main'},[h(EditorPane,{ref:pane,file:file.value,documents:Object.values(files).map(item=>({key:'full-documentation-fixture/main/'+item.path,file:item,uri:item.uri,path:item.path,lang:item.lang,text:item.text})),documentKey:'full-documentation-fixture/main/'+file.value.path,projectId:'full-documentation-fixture',sessionId:'full-documentation-session',readonly:false,languageServers:servers,lspServer:servers.find(s=>s.languages.includes(lang.value)),onChange:event=>state.changes.push(event),onSave:event=>state.saves.push(event)})]),h('aside',{class:'fixture-ai'},[h('h2','AI 代码助手'),h('p','此区域用于验证完整说明的边界。')])])])}}).mount('#app');
Object.assign(state,{pane,lang,nextTick,monaco:ensureMonaco(),files,release(){state.hold=false;for(const deliver of state.pending.splice(0))deliver()}});Object.defineProperty(state,'editor',{get(){return state.monaco.editor.getEditors().find(e=>e.getModel())}});
</script></body></html>`
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
  await context.addInitScript(() => { window.__fixtureClipboard = []; Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { window.__fixtureClipboard.push(value) } } }) })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin === origin && url.pathname === '/api/v1/code/projects/full-documentation-fixture/symbol/business/resolve' && route.request().method() === 'POST') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ entity: { id: 'fixture-session' }, business: { status: 'current', source: 'human', business_meaning: '按权限校验当前用户可见的会话。', detail: '保留当前租户的数据隔离与已有访问授权。' } }) })
    if (url.origin !== origin || url.pathname.startsWith('/api/')) { report.unexpected.push(url.href); return route.abort() }
    if (url.pathname === '/__full-documentation-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    return route.continue()
  })
  page = await context.newPage(); page.on('pageerror', error => report.errors.push(error.message))
  await page.goto(`${origin}/__full-documentation-fixture`, { waitUntil: 'networkidle' })
  const drawer = () => page.getByRole('complementary', { name: '代码完整说明' })
  const body = () => drawer().locator('.documentation-body')
  const waitEditor = async lang => { await page.waitForFunction(lang => window.__fullDocumentationFixture?.editor?.getModel()?.getLanguageId() === lang, lang); await page.waitForTimeout(100) }
  const cursor = async word => page.evaluate(word => { const state = window.__fullDocumentationFixture, e = state.editor, m = e.getModel(), at = m.getValue().indexOf(word);if(at<0)throw new Error('Missing '+word);const pos=m.getPositionAt(at+Math.floor(word.length/2));e.setPosition(pos);e.revealPositionInCenter(pos);e.focus() }, word)
  const open = async word => { await cursor(word); await page.keyboard.press('Control+q'); await drawer().waitFor() }
  const ready = async () => page.waitForFunction(() => document.querySelector('.documentation-body')?.getAttribute('aria-busy') === 'false' && !document.querySelector('.documentation-error'))
  const shot = async name => { const file = path.join(output, name); await page.screenshot({ path: file, fullPage: true }); report.screenshots.push(file);const card=file.replace(/\.png$/,'-panel.png');await drawer().screenshot({path:card});report.screenshots.push(card) }
  const geometry = async name => { const value=await drawer().evaluate(el=>{const b=el.getBoundingClientRect(),e=document.querySelector('.editor').getBoundingClientRect(),body=el.querySelector('.documentation-body'),a=el.querySelector('.documentation-actions').getBoundingClientRect(),f=el.querySelector('.documentation-footer').getBoundingClientRect();return{x:b.x,y:b.y,width:b.width,height:b.height,right:b.right,bottom:b.bottom,editor:{x:e.x,right:e.right},viewport:{width:innerWidth,height:innerHeight},body:{clientHeight:body.clientHeight,scrollHeight:body.scrollHeight},actions:{top:a.top,bottom:a.bottom},footer:{top:f.top,bottom:f.bottom},overflow:document.documentElement.scrollWidth>innerWidth+1}});report.measurements[name]=value;assert.ok(value.x>=value.editor.x-2&&value.right<=value.editor.right+2,JSON.stringify(value));assert.ok(value.y>=0&&value.bottom<=value.viewport.height+1,JSON.stringify(value));assert.ok(value.body.clientHeight>50&&value.actions.bottom<=value.footer.top,JSON.stringify(value));assert.equal(value.overflow,false);return value }
  const wheelToTail = async () => { await body().hover();for(let i=0;i<45;i++){await page.mouse.wheel(0,600);await page.waitForTimeout(20);if(await body().evaluate(el=>el.scrollTop>=el.scrollHeight-el.clientHeight-2))return}throw new Error('Real wheel could not reach full documentation tail') }
  await waitEditor('java'); await open('loadSession'); await ready()
  assert.match(await drawer().locator('.documentation-title').innerText(), /^loadSession$/)
  assert.match(await drawer().locator('.documentation-category').innerText(), /方法/)
  const text = await body().innerText(); report.observations.java = text
  assert.equal(text.split(summary).length - 1, 1, 'Summary must appear exactly once')
  assert.doesNotMatch(text, /^loadSession\s*·\s*方法$/m)
  for (const content of ['会话令牌，必须属于当前用户。', '用于隔离查询的租户编号。', '返回当前租户内可见的会话映射', '令牌已过期或不属于当前用户。', 'FULL_READER_TAIL_67e2', 'PLAIN_FULL_TAIL_7d6b', '按权限校验当前用户可见的会话。', '保留当前租户的数据隔离与已有访问授权。']) assert.ok(text.includes(content), content)
  assert.equal(await drawer().locator('.documentation-declaration').count(), 1)
  const signatureCopies = await drawer().locator('pre code').evaluateAll((els, expected) => els.filter(el => el.textContent.trim() === expected).length, signature)
  assert.equal(signatureCopies, 1, 'Repeated server declaration must render once')
  assert.ok(await drawer().locator('pre code').filter({ hasText: 'System.out.println' }).count())
  assert.match(await drawer().locator('.documentation-plaintext').innerText(), /\*\*不解析成粗体\*\*/)
  report.checks.push('Default full view deduplicates identity, summary and declaration while retaining all contracts, examples, long text and business detail')
  const provenance = drawer().locator('.documentation-provenance'); assert.equal(await provenance.getAttribute('open'), null)
  assert.doesNotMatch(await body().innerText(), /sky-server|识别依据：|作用依据：|文档中文翻译：/)
  await provenance.locator('summary').click();assert.match(await provenance.innerText(),/sky-server|SessionService/);assert.ok(await provenance.locator('a[href="https://docs.fixture.invalid/session"]').count());await provenance.locator('summary').click()
  report.checks.push('Source and reference context remains available in a collapsed disclosure instead of the first reading layer')
  await drawer().getByRole('button',{name:'复制签名',exact:true}).click();assert.equal(await page.evaluate(()=>window.__fixtureClipboard.at(-1)),signature)
  assert.match(await drawer().locator('.documentation-footer').innerText(),/签名已复制/)
  report.checks.push('Copy writes the exact complete server signature to the isolated clipboard')
  await body().evaluate(el=>el.scrollTop=0);await geometry('desktop');await shot('full-documentation-java-desktop.png');await wheelToTail();await shot('full-documentation-java-tail.png')
  report.checks.push('Real wheel input reaches the complete long-body and plaintext tail')
  await drawer().getByRole('button',{name:'原文',exact:true}).click();assert.equal(await drawer().locator('.documentation-overview').count(),0)
  const original=await body().innerText();assert.match(original,/ORIGINAL_READER_TAIL_3f51/);assert.doesNotMatch(original,new RegExp(summary));assert.doesNotMatch(original,/按权限校验当前用户可见的会话/)
  await body().evaluate(el=>el.scrollTop=0);await shot('full-documentation-original.png')
  await drawer().getByRole('button',{name:'中文说明',exact:true}).click();assert.equal((await body().innerText()).split(summary).length-1,1)
  report.checks.push('Original language view retains every server receipt and does not mix application summaries into it')
  await drawer().getByRole('button',{name:'固定说明',exact:true}).click();assert.equal(await drawer().getByRole('button',{name:'取消固定',exact:true}).getAttribute('aria-pressed'),'true')
  await page.locator('#python').click();await waitEditor('python');assert.equal(await drawer().locator('.documentation-title').innerText(),'loadSession')
  await drawer().getByRole('button',{name:'取消固定',exact:true}).click();await page.keyboard.press('Escape');await drawer().waitFor({state:'detached'})
  await open('inspect_session');await ready();assert.equal(await drawer().locator('.documentation-title').innerText(),'inspect_session');assert.match(await drawer().locator('.documentation-category').innerText(),/函数/)
  report.checks.push('A pinned Java explanation survives file navigation, unpins normally, and a new Python lookup gets its own correct identity')
  for(const viewport of [{width:768,height:844},{width:390,height:844},{width:768,height:420}]){await page.setViewportSize(viewport);await page.waitForTimeout(100);await body().evaluate(el=>el.scrollTop=0);await geometry(`${viewport.width}x${viewport.height}`);await shot(`full-documentation-${viewport.width}x${viewport.height}-top.png`);await wheelToTail();assert.match(await body().innerText(),/FULL_READER_TAIL_67e2/);await shot(`full-documentation-${viewport.width}x${viewport.height}-tail.png`)}
  report.checks.push('768px, 390px and short-window reading layouts remain bounded, scrollable and keep actions accessible')
  await page.keyboard.press('Escape');await drawer().waitFor({state:'detached'});await page.waitForFunction(()=>window.__fullDocumentationFixture.editor.hasTextFocus())
  report.checks.push('Escape closes documentation and returns keyboard focus to the exact editor')
  await page.setViewportSize({width:1440,height:960});await page.evaluate(()=>{const s=window.__fullDocumentationFixture;s.hold=true;s.failNextHover=true})
  await open('inspect_session');await page.waitForFunction(()=>window.__fullDocumentationFixture.pending.length>0)
  assert.equal(await body().getAttribute('aria-busy'),'true');assert.equal(await drawer().locator('.documentation-title').innerText(),'inspect_session');assert.doesNotMatch(await drawer().innerText(),/loadSession|FULL_READER_TAIL_67e2|按权限校验当前用户可见的会话/);await shot('full-documentation-loading.png')
  await page.evaluate(()=>window.__fullDocumentationFixture.release());await drawer().getByRole('alert').waitFor();assert.match(await drawer().getByRole('alert').innerText(),/FIXTURE_FULL_DOC_FAILURE/);assert.equal(await drawer().locator('.documentation-overview,.documentation-section').count(),0)
  await page.evaluate(()=>{window.__fullDocumentationFixture.resultVersion=1});await page.keyboard.press('Escape');await drawer().waitFor({state:'detached'});await open('inspect_session');await ready()
  assert.equal(await drawer().locator('.documentation-title').innerText(),'inspect_session');assert.match(await body().innerText(),/重试后读取会话/);assert.doesNotMatch(await body().innerText(),/FIXTURE_FULL_DOC_FAILURE/)
  report.checks.push('Delayed loading, failure and explicit retry cannot retain an old identity, body or error')
  assert.equal(await page.evaluate(()=>window.__fullDocumentationFixture.saves.length),0);assert.equal(await page.evaluate(()=>window.__fullDocumentationFixture.changes.length),0)
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpected,[]);report.checks.push('All inspection runs leave editor bytes and saves untouched and perform no unexpected request');report.passed=true
}catch(error){report.error=error.stack||String(error);if(page)await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});process.exitCode=1}
finally{await browser.close();fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,checks:report.checks.length,error:report.error,report:path.join(output,'report.json')}))}
