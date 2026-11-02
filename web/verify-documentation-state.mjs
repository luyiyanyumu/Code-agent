import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

const origin = new URL(process.argv[2] || 'http://127.0.0.1:5203').origin
const backend = new URL(process.argv[3] || 'http://127.0.0.1:19415').origin
const output = path.resolve(process.argv[4] || '../.tmp/documentation-state-20261101/browser')
fs.mkdirSync(output, { recursive: true })
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const vue = (await (await fetch(`${origin}/src/components/code/EditorPane.vue`)).text()).match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vue)
const control = async action => { const r = await fetch(`${backend}/control?action=${action}`, { method: 'POST' }); assert.ok(r.ok); return r.json() }
const backendState = async () => (await fetch(`${backend}/state`)).json()
const definition = await backendState()
assert.ok(definition.sources?.java && definition.sources?.python, 'Backend must expose its isolated documents')
const report = { passed: false, checks: [], errors: [], unexpected: [], screenshots: [], measurements: {}, observations: {}, backend, fixtureRoot: definition.root, saves: 0, edits: 0,
  scope: 'Real EditorPane/Monaco → real WebSocket LspManager/translation pool/SQLite; only the language server and translation model are isolated gated stubs.' }
const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{margin:0;height:100%;overflow:hidden}body{font:14px system-ui;background:#f6f7fb}#app{height:100%;display:flex;flex-direction:column;min-width:0}.fixture-head{height:40px;display:flex;align-items:center;gap:8px;padding:0 12px;background:white;border-bottom:1px solid #dce2ec;flex:none;overflow-x:auto;white-space:nowrap}.fixture-head b{margin-right:auto}.fixture-workspace{display:flex;flex:1;min-height:0;min-width:0}.fixture-main{min-width:0;flex:1;display:flex;padding:12px}.fixture-main>.pane{border:1px solid #dce2ec;overflow:hidden;border-radius:8px}.fixture-ai{width:290px;flex:none;border-left:1px solid #dce2ec;background:white;padding:18px;box-sizing:border-box;color:#64748b}.fixture-ai h2{font-size:14px;color:#1e293b}@media(max-width:900px){.fixture-ai{display:none}.fixture-head b{display:none}.fixture-main{padding:6px}}
</style></head><body><div id="app"></div><script type="module">
import {createApp,h,ref,computed,nextTick} from '${vue}';
import EditorPane from '/src/components/code/EditorPane.vue';
import {ensureMonaco} from '/src/lib/monacoSetup.js';
import {notifyHoverDocumentationUpdated} from '/src/lib/tokenHover.js';
import '/src/style.css';
const sources=${JSON.stringify(definition.sources).replaceAll('<', '\u003c')};
const state=window.__documentationState={frames:[],received:[],terminalEvents:[],sockets:[],socketEvents:[],edits:[],saves:[],notifyHoverDocumentationUpdated};
const NativeSocket=window.WebSocket;
window.WebSocket=class extends NativeSocket{constructor(url,...options){const original=new URL(url,location.href),lsp=original.pathname.endsWith('/lsp/stream');super(lsp?'${backend.replace(/^http/, 'ws')}/ws?lang='+encodeURIComponent(original.searchParams.get('lang')):url,...options);this.isFixtureLsp=lsp;if(lsp){state.sockets.push(this);state.socketEvents.push({type:'created',url:this.url});this.addEventListener('close',event=>state.socketEvents.push({type:'close',url:this.url,code:event.code,reason:event.reason}));this.addEventListener('error',()=>state.socketEvents.push({type:'error',url:this.url}));this.addEventListener('message',event=>{const frame=JSON.parse(event.data);state.received.push(frame);if(frame.method==='code-agent/documentationUpdated')state.terminalEvents.push({status:frame.params?.status,hoverVisible:[...document.querySelectorAll('.monaco-resizable-hover')].some(el=>el.offsetHeight&&getComputedStyle(el).visibility!=='hidden'),time:performance.now()})})}}send(raw){if(this.isFixtureLsp)state.frames.push(JSON.parse(raw));return super.send(raw)}};
const lang=ref('java'),pane=ref(null),files={};for(const[language,s]of Object.entries(sources)){files[language]={...s,lang:language,sha1:'fixture-'+language,total_lines:s.text.split('\n').length,editable:true,size:s.text.length}}const file=computed(()=>files[lang.value]),servers=['java','python'].map(language=>({id:'state-fixture-'+language,name:'Fixture '+language,languages:[language],state:'installed',available:true}));
createApp({setup(){return()=>h('div',{id:'app'},[h('header',{class:'fixture-head'},[h('b','Code-agent · 中文说明状态验收'),h('button',{id:'java',onClick:()=>lang.value='java'},'Java'),h('button',{id:'python',onClick:()=>lang.value='python'},'Python')]),h('div',{class:'fixture-workspace'},[h('main',{class:'fixture-main'},[h(EditorPane,{ref:pane,file:file.value,documents:Object.values(files).map(item=>({key:'documentation-state/main/'+item.path,file:item,uri:item.uri,path:item.path,lang:item.lang,text:item.text})),documentKey:'documentation-state/main/'+file.value.path,projectId:'documentation-state',sessionId:'documentation-state-session',readonly:false,languageServers:servers,lspServer:servers.find(s=>s.languages.includes(lang.value)),onChange:event=>state.edits.push(event),onSave:event=>state.saves.push(event)})]),h('aside',{class:'fixture-ai'},[h('h2','AI 代码助手'),h('p','原生悬停与完整说明保留在编辑区域。')])])])}}).mount('#app');
Object.assign(state,{pane,lang,nextTick,files,monaco:ensureMonaco()});Object.defineProperty(state,'editor',{get(){return state.monaco.editor.getEditors().find(editor=>editor.getModel())}});
</script></body></html>`
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page, context
try {
  const fresh = async () => {
    if (context) await context.close()
    await control('reset')
    context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
    await context.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.origin === origin && url.pathname === '/api/v1/code/projects/documentation-state/symbol/business/resolve') return route.fulfill({ contentType: 'application/json', body: '{"entity":null,"business":null}' })
      if (url.origin !== origin || url.pathname.startsWith('/api/')) { report.unexpected.push(url.href); return route.abort() }
      return route.continue()
    })
    page = await context.newPage(); page.on('pageerror', error => report.errors.push(error.message));page.on('console',message=>{if(message.type()==='error')console.log('BROWSER',message.text())});page.on('websocket',socket=>socket.on('socketerror',error=>console.log('WSERROR',socket.url(),error)))
    // A real loopback navigation retains Chrome's local address-space proof;
    // a synthetic route.fulfill document is classified as public and blocks WS.
    await page.goto(`${origin}/__code_agent_health`, { waitUntil: 'networkidle' })
    await page.setContent(html, { waitUntil: 'networkidle' })
    await waitEditor('java')
  }
  const native = () => page.locator('.monaco-resizable-hover:visible')
  const drawer = () => page.getByRole('complementary', { name: '代码完整说明' })
  const waitEditor = async language => {
    await page.waitForFunction(language => window.__documentationState?.editor?.getModel()?.getLanguageId() === language, language)
    await page.waitForFunction(language => window.__documentationState.received.some(frame => frame.method === 'code-agent/ready') && window.__documentationState.frames.some(frame => frame.method === 'textDocument/didOpen' && frame.params.textDocument.uri.endsWith(language === 'java' ? '.java' : '.py')), language,{timeout:10000})
    await page.evaluate(() => window.__documentationState.editor.updateOptions({ hover: { enabled: 'on', delay: 30, sticky: true, hidingDelay: 0 }, quickSuggestions: false }))
    await page.waitForTimeout(100)
  }
  const locate = async word => page.evaluate(word => {
    const editor = window.__documentationState.editor, model = editor.getModel(), at = model.getValue().indexOf(word)
    if (at < 0) throw new Error('Missing fixture word '+word)
    const position = model.getPositionAt(at + Math.floor(word.length / 2)); return { position, word }
  }, word)
  const setCaret = async word => { const target = await locate(word); await page.evaluate(position => { const e=window.__documentationState.editor;e.setPosition(position);e.revealPositionInCenter(position);e.focus() }, target.position); return target.position }
  const mouseHover = async word => {
    const target = await locate(word)
    const point = await page.evaluate(position => { const e=window.__documentationState.editor;e.revealPositionInCenter(position);const p=e.getScrolledVisiblePosition(position),b=e.getDomNode().getBoundingClientRect();if(!p)throw new Error('Offscreen fixture word');return{x:b.x+p.left+3,y:b.y+p.top+p.height/2} }, target.position)
    await page.mouse.move(point.x, point.y); await native().waitFor(); await page.waitForTimeout(70)
    return native().innerText()
  }
  const openFull = async word => { await setCaret(word); await page.keyboard.press('Control+q'); await drawer().waitFor(); await page.waitForFunction(() => document.querySelector('.documentation-body')?.getAttribute('aria-busy') === 'false') }
  const shot = async name => { const file=path.join(output,name);await page.screenshot({path:file,fullPage:true});report.screenshots.push(file);const widget=await drawer().count()?drawer():native();if(await widget.count()){const card=file.replace(/\.png$/,'-card.png');await widget.screenshot({path:card});report.screenshots.push(card)} }
  const frames = async method => page.evaluate(method => window.__documentationState.frames.filter(frame => frame.method === method), method)
  const waitStatus = async regex => { await page.waitForFunction(source => new RegExp(source).test(document.querySelector('.documentation-notice')?.textContent || ''), regex.source) }
  const geometry = async label => { const widget=await drawer().count()?drawer():native(); const data=await widget.evaluate(el=>{const b=el.getBoundingClientRect(),e=document.querySelector('.editor').getBoundingClientRect();return{x:b.x,y:b.y,right:b.right,bottom:b.bottom,width:b.width,height:b.height,editorRight:e.right,viewportWidth:innerWidth,viewportHeight:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth+1}});report.measurements[label]=data;assert.ok(data.x>=-1&&data.right<=Math.min(data.viewportWidth,data.editorRight)+2&&data.bottom<=data.viewportHeight+2,JSON.stringify(data));assert.equal(data.overflow,false) }

  // The caret intentionally points somewhere else. Only the mouse establishes
  // the native hover anchor; no editor.action.showHover or provider mock runs.
  await fresh()
  const mainWord = definition.sources.java.hoverWord || 'readSession', caretWord = definition.sources.java.caretWord || 'unrelatedCaret'
  const caret = await setCaret(caretWord), before = await page.evaluate(() => ({ model:window.__documentationState.editor.getValue(),version:window.__documentationState.editor.getModel().getVersionId(),active:document.activeElement?.className }))
  const pendingText = await mouseHover(mainWord); assert.match(pendingText,/正在翻译中文说明/)
  report.observations.nativePending=pendingText; await shot('native-pending.png')
  await control('release')
  await page.waitForFunction(() => [...document.querySelectorAll('.monaco-resizable-hover')].some(el=>el.offsetHeight&&el.textContent.includes('中文状态回执完成')))
  assert.doesNotMatch(await native().innerText(),/正在翻译|尚未翻译|翻译失败/)
  const after = await page.evaluate(() => ({model:window.__documentationState.editor.getValue(),version:window.__documentationState.editor.getModel().getVersionId(),position:window.__documentationState.editor.getPosition(),active:document.activeElement?.className,anchor:window.__documentationState.editor.getContribution('editor.contrib.contentHover')._contentWidget._currentResult.options.anchor.range}))
  assert.deepEqual(after.position,caret);assert.equal(after.model,before.model);assert.equal(after.version,before.version);assert.equal(after.active,before.active)
  const target=await locate(mainWord);assert.equal(after.anchor.startLineNumber,target.position.lineNumber);assert.ok(Math.abs(after.anchor.startColumn-target.position.column)<=1)
  report.observations.anchorRefresh={caret,before,after,hoverRequests:await frames('textDocument/hover')};await geometry('desktopNative');await shot('native-ready.png')
  report.checks.push('Real backend pending→ready refreshes the visible native mouse anchor while caret, focus, model content and model revision remain unchanged')

  await page.mouse.move(3,3);await page.keyboard.press('Escape');await native().waitFor({state:'hidden'})
  const hiddenCount=(await frames('textDocument/hover')).length
  await page.evaluate(()=>{const s=window.__documentationState,open=s.frames.findLast(frame=>frame.method==='textDocument/didOpen');s.notifyHoverDocumentationUpdated({projectId:'documentation-state',tree:'main',uri:open.params.textDocument.uri,version:open.params.textDocument.version,status:'ready'})});await page.waitForTimeout(150)
  assert.equal((await frames('textDocument/hover')).length,hiddenCount)
  report.checks.push('A hidden native hover ignores documentation updates and starts no provider query')

  const cachedBefore=await backendState();assert.ok(cachedBefore.cache.entries>0)
  await control('clear_memory');await openFull(mainWord);assert.match(await drawer().innerText(),/中文状态回执完成/);assert.equal(await drawer().locator('.documentation-notice').count(),0)
  const cachedAfter=await backendState();assert.equal(cachedAfter.attempts,cachedBefore.attempts);report.observations.sqliteCache={before:cachedBefore.cache,after:cachedAfter.cache,attemptsBefore:cachedBefore.attempts,attemptsAfter:cachedAfter.attempts}
  report.checks.push('Clearing only translator memory still returns ready Chinese from real SQLite without another model attempt')

  await fresh();await control('release');await page.evaluate(()=>window.__documentationState.editor.updateOptions({hover:{enabled:'on',delay:300,sticky:true,hidingDelay:0}}));await setCaret(caretWord);await mouseHover(mainWord)
  await page.waitForFunction(()=>[...document.querySelectorAll('.monaco-resizable-hover')].some(el=>el.offsetHeight&&el.textContent.includes('中文状态回执完成')))
  const earlyEvents=await page.evaluate(()=>window.__documentationState.terminalEvents);assert.ok(earlyEvents.some(event=>event.status==='ready'&&!event.hoverVisible),'Fast backend terminal update must precede first pending hover DOM for this race proof')
  const earlyResponse=await page.evaluate(()=>window.__documentationState.received.find(frame=>frame.id===1)?.result?.documentation_translation)
  assert.equal(earlyResponse?.status,'pending');assert.equal((await frames('textDocument/hover')).length,2);report.observations.earlyTerminal={events:earlyEvents,initialTranslation:earlyResponse,hoverRequests:await frames('textDocument/hover')};await shot('native-early-terminal-ready.png')
  report.checks.push('A terminal update arriving before the initial pending hover is visible refreshes that same operation once, without a cached-ready loop')

  await fresh();await openFull(mainWord);await waitStatus(/翻译中/)
  assert.equal(await drawer().locator('.documentation-translation-retry').count(),0)
  await drawer().getByRole('button',{name:'原文',exact:true}).click();assert.match(await drawer().innerText(),/ORIGINAL_STATE_TAIL/)
  await drawer().getByRole('button',{name:'固定说明',exact:true}).click();await shot('full-pending-original.png')
  await control('fail');await page.waitForFunction(()=>window.__documentationState.received.some(frame=>frame.method==='code-agent/documentationUpdated'&&frame.params?.status==='failed'))
  const originalAtFailure=await drawer().innerText();assert.match(originalAtFailure,/ORIGINAL_STATE_TAIL/);assert.match(originalAtFailure,/已固定/)
  assert.equal(await drawer().getByRole('button',{name:'原文',exact:true}).getAttribute('aria-pressed'),'true')
  await drawer().getByRole('button',{name:'中文说明',exact:true}).click();await waitStatus(/翻译失败/);await shot('full-failed.png')
  const retryBefore=(await frames('textDocument/hover')).length
  await drawer().locator('.documentation-translation-retry').click();await waitStatus(/翻译中/)
  assert.equal(await drawer().locator('.documentation-translation-retry').count(),0)
  const retries=(await frames('textDocument/hover')).slice(retryBefore);assert.ok(retries.some(frame=>frame.params.codeAgentRetryTranslation===true))
  await drawer().getByRole('button',{name:'原文',exact:true}).click();assert.match(await drawer().innerText(),/ORIGINAL_STATE_TAIL/)
  await control('release');await page.waitForFunction(()=>!document.querySelector('.documentation-notice'))
  assert.match(await drawer().innerText(),/ORIGINAL_STATE_TAIL/);assert.match(await drawer().innerText(),/已固定/)
  await drawer().getByRole('button',{name:'中文说明',exact:true}).click();assert.match(await drawer().innerText(),/中文状态回执完成/)
  report.observations.retryFrames=retries;await shot('full-retry-ready.png')
  report.checks.push('Pending keeps original available; true backend failure exposes Chinese retry; one retry remains pending without duplicate button and ready preserves pin/original state')

  await page.setViewportSize({width:390,height:720});await page.waitForTimeout(120);await geometry('narrowFull');await shot('full-ready-390.png')
  await page.keyboard.press('Escape');await drawer().waitFor({state:'detached'})
  await page.mouse.move(3,3);await setCaret(caretWord);await mouseHover(mainWord);await geometry('narrowNative');await shot('native-ready-390.png')
  report.checks.push('Chinese pending/failed/ready documentation and native hover remain inside the editor at 390px without covering adjacent panels')

  await fresh();await openFull(mainWord);await waitStatus(/翻译中/);await page.keyboard.press('Escape');await drawer().waitFor({state:'detached'});await control('release');await page.waitForTimeout(150)
  assert.equal(await drawer().count(),0)
  await fresh();await setCaret(caretWord);await mouseHover(mainWord);await page.locator('#python').click();await waitEditor('python');const switchedCount=(await frames('textDocument/hover')).length;await control('release');await page.waitForTimeout(200)
  assert.equal((await frames('textDocument/hover')).length,switchedCount);assert.equal(await native().count(),0);assert.equal(await drawer().count(),0)
  report.checks.push('Closing the full panel and switching documents prevent late translated results from reopening or querying the abandoned symbol')

  report.saves=await page.evaluate(()=>window.__documentationState.saves.length);report.edits=await page.evaluate(()=>window.__documentationState.edits.length)
  report.observations.backend=await backendState();assert.equal(report.saves,0);assert.equal(report.edits,0);assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpected,[])
  report.passed=true
} catch(error) {
  report.failure=error.stack||String(error)
  if(page){try{const file=path.join(output,'failure.png');await page.screenshot({path:file,fullPage:true});report.screenshots.push(file);report.observations.failureDom=await page.locator('body').innerText();report.observations.failureFrames=await page.evaluate(()=>window.__documentationState?.frames);report.observations.failureSockets=await page.evaluate(()=>window.__documentationState?.socketEvents);report.observations.failureBackend=await backendState()}catch{}}
  throw error
} finally {
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify({passed:report.passed,checks:report.checks.length,report:path.join(output,'report.json'),failure:report.failure?.split('\n')[0]},null,2))
}
