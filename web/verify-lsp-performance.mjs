import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Real EditorPane, Vue, Monaco and Chrome; all HTTP APIs and language sockets
// are isolated fixtures. This never opens a user tab or writes a project file.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/lsp-performance-browser', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], errors = [], unexpected = [], screenshots = [], measurements = {}
const root = 'E:/lsp-performance-fixture', projectId = 'lsp-performance-fixture'
const source = 'package demo;\n\npublic class WorkspaceService {\n    public String resolve(String input) {\n        return input.trim();\n    }\n}\n' +
  Array.from({ length: 2800 }, (_, i) => `// persistent large Java draft line ${i} : ORIGINAL_${i} ; source snapshot and caret stay intact`).join('\n')
const helper = 'package demo;\n\npublic class HelperType {\n    public String resolve(String input) { return input; }\n}\n'
const servers = [
  { id: 'jdtls', name: 'Eclipse JDT (Java)', languages: ['java'], installed: true, enabled: true, state: 'installed', detected: true,
    resolved_command: 'C:/Users/fixture/LanguageServers/java/this-is-a-long-distribution-path/jdtls/bin/jdtls.bat' },
  { id: 'pyright', name: 'Pyright', languages: ['python'], installed: true, enabled: true, state: 'installed', detected: false, resolved_command: 'C:/fixture/bin/pyright-langserver.cmd' },
  { id: 'rust', name: 'rust-analyzer', languages: ['rust'], installed: true, enabled: true, state: 'installed', detected: false, resolved_command: 'C:/fixture/bin/rust-analyzer.exe' },
  { id: 'swift', name: 'SourceKit-LSP', languages: ['swift'], installed: false, enabled: true, state: 'missing', detected: false, install_hint: 'Install the Swift toolchain' },
]
const fixture = { source, helper, root, projectId, servers }
const component = await (await fetch(`${origin}/src/components/code/EditorPane.vue`)).text()
const vueModule = component.match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vueModule && new URL(vueModule, origin).origin === origin, 'Cannot locate Vite Vue runtime')
const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{margin:0;min-width:0;height:100%;}body{background:#f4f6f9;font:14px system-ui;}#app{height:100%;display:flex;flex-direction:column;min-width:0;}.fixture-head{padding:10px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:white;border-bottom:1px solid #e3e7ef;}.fixture-head b{font-size:14px;}.fixture-shell{flex:1;min-height:0;min-width:0;padding:12px;}.fixture-shell>.pane{border:1px solid #e3e7ef;border-radius:8px;overflow:hidden;}
</style></head><body><div id="app"></div><script type="module">
import {createApp,h,ref,computed,nextTick} from '${vueModule}';
import EditorPane from '/src/components/code/EditorPane.vue';
import {ensureMonaco} from '/src/lib/monacoSetup.js';
import '/src/style.css';
const fixture=${JSON.stringify(fixture).replaceAll('<', '\u003c')};
const state=window.__lspPerformanceFixture={frames:[],sockets:[],changes:[],saves:[],openFiles:[],navigationMode:'normal',socketErrors:[]};
const first={path:'src/demo/WorkspaceService.java',text:fixture.source,lang:'java',sha1:'fixture-a',uri:'file:///'+fixture.root+'/src/demo/WorkspaceService.java',total_lines:fixture.source.split('\n').length,editable:true,size:fixture.source.length};
const second={path:'src/demo/HelperType.java',text:fixture.helper,lang:'java',sha1:'fixture-b',uri:'file:///'+fixture.root+'/src/demo/HelperType.java',total_lines:fixture.helper.split('\n').length,editable:true,size:fixture.helper.length};
function offset(text,point){const lines=text.split('\n');return lines.slice(0,point.line).reduce((sum,line)=>sum+line.length+1,0)+point.character;}
function applyChange(text,change){if(!change.range)return change.text;const start=offset(text,change.range.start),end=offset(text,change.range.end);if(change.rangeLength!==undefined&&change.rangeLength!==end-start)state.socketErrors.push('Invalid replacement UTF16 length');return text.slice(0,start)+change.text+text.slice(end);}
const capabilities={textDocumentSync:{change:2,save:{includeText:false}},completionProvider:{triggerCharacters:['.']},hoverProvider:true,signatureHelpProvider:{triggerCharacters:['(']},definitionProvider:true,declarationProvider:true,typeDefinitionProvider:true,implementationProvider:true,referencesProvider:true,documentSymbolProvider:true,workspaceSymbolProvider:true,renameProvider:{prepareProvider:true},codeActionProvider:true,callHierarchyProvider:true,typeHierarchyProvider:true,codeLensProvider:{resolveProvider:true},inlayHintProvider:true};
class FixtureSocket extends EventTarget{static CONNECTING=0;static OPEN=1;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;this.docs=new Map();const target=new URL(url,location.href);this.isLsp=target.pathname.endsWith('/lsp/stream');if(target.host!==location.host||(!this.isLsp&&target.pathname!=='/'))state.socketErrors.push('Unexpected socket '+target.pathname);state.sockets.push(this);queueMicrotask(()=>{this.readyState=1;this.emit('open',new Event('open'));setTimeout(()=>{if(this.isLsp)this.message({method:'code-agent/ready',params:{server:'jdtls',initialization_ms:1370,capabilities,environment:{workspace_data:{isolated:true}}}})},250)})}
 emit(type,event){this.dispatchEvent(event);this['on'+type]?.(event)}
 message(frame){if(this.readyState===1)this.emit('message',new MessageEvent('message',{data:JSON.stringify(frame)}))}
 send(raw){if(!this.isLsp)return;const frame=JSON.parse(raw);state.frames.push(frame);const uri=frame.params?.textDocument?.uri;
  if(frame.method==='textDocument/didOpen')this.docs.set(uri,{...frame.params.textDocument});
  if(frame.method==='textDocument/didChange'){const doc=this.docs.get(uri);if(!doc)state.socketErrors.push('didChange before didOpen');else{for(const change of frame.params.contentChanges)doc.text=applyChange(doc.text,change);doc.version=frame.params.textDocument.version;}}
  if(frame.method==='textDocument/didClose')this.docs.delete(uri);
  if(frame.id===undefined)return;let result=null;
  const range=(line,start,end)=>({start:{line,character:start},end:{line,character:end}});
  const item=(name,target)=>({name,kind:6,uri:target,range:range(3,18,25),selectionRange:range(3,18,25),data:{opaque:name}});
  if(frame.method==='textDocument/declaration')result={uri:first.uri,range:range(2,13,29)};
  if(frame.method==='textDocument/typeDefinition')result=[{targetUri:second.uri,targetRange:range(2,0,25),targetSelectionRange:range(2,13,23),originSelectionRange:range(3,18,25)}];
  if(frame.method==='textDocument/implementation')result=[{uri:second.uri,range:range(3,18,25)}];
  if(frame.method==='textDocument/definition')result={uri:second.uri,range:range(2,13,23)};
  if(frame.method==='textDocument/hover')result={contents:{kind:'markdown',value:'Fixture Java documentation'}};
  if(frame.method==='textDocument/completion')result={items:[],isIncomplete:false};
  if(frame.method==='textDocument/documentSymbol')result=[];
  if(frame.method==='textDocument/prepareCallHierarchy'||frame.method==='textDocument/prepareTypeHierarchy')result=[item('resolve',uri)];
  if(frame.method==='callHierarchy/incomingCalls')result=frame.params.item.name==='resolve'?[{from:item('caller',second.uri),fromRanges:[range(3,18,25)]}]:[];
  if(frame.method==='callHierarchy/outgoingCalls')result=[{to:item('helper',second.uri),fromRanges:[range(4,8,13)]}];
  if(frame.method==='typeHierarchy/supertypes')result=[item('BaseType',second.uri)];
  if(frame.method==='typeHierarchy/subtypes')result=[];
  if(frame.method==='textDocument/codeLens')result=[{range:range(3,18,25),data:{uri}}];
  if(frame.method==='codeLens/resolve')result={...frame.params,command:{title:'1 reference',command:'java.show.references',arguments:[frame.params.data.uri,{line:3,character:18},[{uri:second.uri,range:range(3,18,25)}]]}};
  if(frame.method==='textDocument/inlayHint')result=[{position:{line:4,character:15},label:'fixture hint:',kind:2,paddingRight:true}];
  queueMicrotask(()=>this.message({id:frame.id,result}));
 }
 close(){this.readyState=3;this.emit('close',new CloseEvent('close'))}
}
window.WebSocket=FixtureSocket;
const files=new Map([[first.path,first],[second.path,second]]),current=ref(first),pane=ref(null),highlight=ref(null);
const docsRows=computed(()=>[...files.values()].map(file=>({key:fixture.projectId+'/main/'+file.path,file,uri:file.uri,path:file.path,lang:file.lang,text:file.text})));
const navigate=event=>{state.openFiles.push(event);const file=files.get(event.path);if(!file){state.socketErrors.push('Unexpected file navigation '+event.path);return;}current.value=file;highlight.value={start:event.line,column:event.column,token:(highlight.value?.token||0)+1};};
createApp({setup(){return()=>h('div',{id:'app'},[h('header',{class:'fixture-head'},[h('b','代码服务 · 隔离验收'),h('button',{id:'first',onClick:()=>current.value=files.get(first.path)},'当前文件'),h('button',{id:'second',onClick:()=>current.value=files.get(second.path)},'另一文件')]),h('main',{class:'fixture-shell'},[h(EditorPane,{ref:pane,file:current.value,documentKey:fixture.projectId+'/main/'+current.value.path,documents:docsRows.value,projectId:fixture.projectId,sessionId:'performance-session',readonly:false,highlight:highlight.value,languageServers:fixture.servers,lspServer:fixture.servers[0],onOpenFile:navigate,onLanguageSymbol:navigate,onChange:change=>{state.changes.push(change);const file=files.get(change.path);if(file)file.text=change.text;},onSave:event=>state.saves.push(event)})])])}}).mount('#app');
Object.assign(state,{pane,current,highlight,nextTick,files,monaco:ensureMonaco()});
Object.defineProperty(state,'editor',{get(){return state.monaco.editor.getEditors()[0]}});
</script></body></html>`
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page, failure
try {
  const context = await browser.newContext({ viewport: { width: 1400, height: 940 }, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) { unexpected.push(url.href); return route.abort() }
    if (url.pathname === `/api/v1/code/projects/${projectId}/lsp`) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ servers, sessions: [], failures: {}, environment: {} }) })
    if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return route.abort() }
    if (url.pathname === '/__lsp-performance-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    return route.continue()
  })
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/__lsp-performance-fixture`, { waitUntil: 'networkidle' })
  await page.waitForFunction(() => !!window.__lspPerformanceFixture?.editor && document.querySelector('.lsp.as-button.ready'))
  await page.getByRole('button', { name: '调用层级', exact: true }).click()
  await page.locator('.hierarchy-panel').getByText('caller', { exact: true }).waitFor()
  assert.ok(await page.evaluate(() => window.__lspPerformanceFixture.frames.some(frame => frame.method === 'callHierarchy/incomingCalls')))
  await page.locator('.hierarchy-panel').getByRole('button', { name: /caller/, exact: false }).filter({ has: page.locator('.hierarchy-name') }).click()
  await page.waitForFunction(() => window.__lspPerformanceFixture.current.value.path.endsWith('HelperType.java'))
  assert.equal((await page.evaluate(() => window.__lspPerformanceFixture.openFiles.at(-1))).line, 4)
  checks.push('Integrated EditorPane call hierarchy navigates a lazy caller to another project file')
  await page.locator('#first').click()
  await page.waitForFunction(() => window.__lspPerformanceFixture.current.value.path.endsWith('WorkspaceService.java'))
  await page.getByRole('button', { name: '类型层级', exact: true }).click()
  await page.locator('.hierarchy-panel').getByText('BaseType', { exact: true }).waitFor()
  await page.locator('.hierarchy-panel').getByRole('button', { name: '关闭层级' }).click()
  checks.push('Integrated EditorPane type hierarchy queries and closes without changing the draft')
  await page.evaluate(() => { const e = window.__lspPerformanceFixture.editor; e.setPosition({ lineNumber: 4, column: 24 }); e.revealLineInCenter(4) })
  await page.getByRole('button', { name: '1 reference', exact: true }).first().waitFor()
  await page.getByRole('button', { name: '1 reference', exact: true }).first().click()
  await page.locator('.lens-references').getByText('src/demo/HelperType.java', { exact: true }).waitFor()
  await page.locator('.lens-references').getByRole('button', { name: /src\/demo\/HelperType\.java/ }).click()
  await page.waitForFunction(() => window.__lspPerformanceFixture.current.value.path.endsWith('HelperType.java'))
  checks.push('Integrated CodeLens opens a bounded local reference picker and navigates its selected location')
  await page.locator('#first').click(); await page.waitForFunction(() => window.__lspPerformanceFixture.current.value.path.endsWith('WorkspaceService.java'))
  await page.evaluate(() => window.__lspPerformanceFixture.editor.revealLineInCenter(5))
  await page.waitForFunction(() => window.__lspPerformanceFixture.frames.some(frame => frame.method === 'textDocument/inlayHint'))
  assert.ok(await page.locator('.monaco-editor').getByText('fixture hint:', { exact: true }).count())
  checks.push('Integrated editor viewport displays real LSP inlay hints')
  const panel = () => page.locator('.lsp-panel')
  const openPanel = async () => { await page.locator('.lsp.as-button').click(); await panel().waitFor(); await page.waitForFunction(() => document.querySelector('.lsp-catalog-summary')?.textContent.includes('目录 4')) }
  const shot = async name => { const filename = path.join(output, name); await page.screenshot({ path: filename, fullPage: true }); screenshots.push(filename) }
  const snapshot = () => page.evaluate(() => { const editor = window.__lspPerformanceFixture.editor; return { text: editor.getValue(), position: editor.getPosition(), top: editor.getScrollTop(), left: editor.getScrollLeft(), uri: editor.getModel().uri.toString() } })
  await openPanel()
  assert.match(await panel().locator('.lsp-runtime-row').innerText(), /Eclipse JDT \(Java\)[\s\S]*已连接[\s\S]*初始化 1\.37s/)
  checks.push('Runtime panel displays the actual connected server and reported 1.37s initialization')
  const features = await panel().locator('.lsp-features span').evaluateAll(els => els.map(el => ({ label: el.textContent, available: el.classList.contains('available') })))
  for (const label of ['声明', '类型', '实现', '补全', '说明']) assert.equal(features.find(feature => feature.label === label)?.available, true)
  for (const label of ['格式化', '语义高亮']) assert.equal(features.find(feature => feature.label === label)?.available, false)
  measurements.features = features; checks.push('Capability chips follow real negotiated capabilities, including unsupported features')
  assert.match(await panel().locator('.lsp-catalog-summary').innerText(), /目录 4 · 命令已找到 3 · 未找到 1/)
  assert.equal(await panel().locator('.lsp-list li').count(), 1)
  assert.match(await panel().locator('.lsp-list').innerText(), /命令已找到[\s\S]*Eclipse JDT/)
  assert.doesNotMatch(await panel().locator('.lsp-list').innerText(), /Pyright|rust-analyzer|SourceKit/)
  checks.push('Command catalog is distinguished from runtime health and defaults to relevant Java only')
  await panel().getByRole('button', { name: '显示全部', exact: true }).click()
  assert.equal(await panel().locator('.lsp-list li').count(), 4)
  assert.match(await panel().locator('.lsp-list').innerText(), /Pyright[\s\S]*rust-analyzer[\s\S]*SourceKit-LSP/)
  checks.push('Show all reveals installed and missing presets outside the project language')
  await shot('lsp-panel-desktop-all.png')
  await panel().getByRole('button', { name: '只看相关', exact: true }).click()
  await panel().getByRole('button', { name: '关闭代码分析设置' }).click()
  await page.evaluate(() => { const state = window.__lspPerformanceFixture, e = state.editor, text = e.getValue(), needle = 'ORIGINAL_1700', index = text.indexOf(needle), start = e.getModel().getPositionAt(index + 'ORIGINAL_'.length); state.frames.length = 0; e.executeEdits('fixture', [{ range: new state.monaco.Range(start.lineNumber, start.column, start.lineNumber, start.column + 1), text: '9' }]); e.setPosition({ lineNumber: start.lineNumber, column: start.column + 1 }); e.revealLineInCenter(start.lineNumber); })
  await page.waitForFunction(() => window.__lspPerformanceFixture.frames.some(frame => frame.method === 'textDocument/didChange'))
  measurements.incremental = await page.evaluate(() => { const state = window.__lspPerformanceFixture, frame = state.frames.find(frame => frame.method === 'textDocument/didChange'), uri = frame.params.textDocument.uri, doc = state.sockets.find(socket => socket.isLsp && socket.docs.has(uri)).docs.get(uri); return { draftLength: state.editor.getValue().length, frameLength: JSON.stringify(frame).length, changes: frame.params.contentChanges, appliedExactly: doc.text === state.editor.getValue() } })
  assert.ok(measurements.incremental.draftLength > 200000)
  assert.equal(measurements.incremental.changes.length, 1)
  assert.equal(measurements.incremental.changes[0].text, '9')
  assert.equal(measurements.incremental.changes[0].rangeLength, 1)
  assert.ok(measurements.incremental.frameLength < 500)
  assert.equal(measurements.incremental.appliedExactly, true)
  checks.push('A single-character edit in a 200KB+ real Monaco draft sends one tiny UTF16 range and reconstructs exactly')
  const draft = await snapshot(); await openPanel(); await panel().getByRole('button', { name: '关闭代码分析设置' }).click()
  assert.deepEqual(await snapshot(), draft); checks.push('Opening and closing settings preserves the unsaved draft, caret and scrolling')
  await page.setViewportSize({ width: 390, height: 844 }); await openPanel()
  await panel().getByRole('button', { name: '显示全部', exact: true }).click()
  measurements.mobile = await panel().evaluate(el => { const rect = el.getBoundingClientRect(); return { page: document.documentElement.scrollWidth, viewport: innerWidth, panel: { x: rect.x, width: rect.width, right: rect.right }, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth } })
  assert.ok(measurements.mobile.page <= 392, JSON.stringify(measurements.mobile))
  assert.ok(measurements.mobile.panel.x >= 0 && measurements.mobile.panel.right <= 391, JSON.stringify(measurements.mobile))
  assert.ok(measurements.mobile.scrollWidth <= measurements.mobile.clientWidth + 2, JSON.stringify(measurements.mobile))
  checks.push('390px settings panel and long executable paths do not overflow horizontally')
  await shot('lsp-panel-mobile-all.png')
  await panel().getByRole('button', { name: '关闭代码分析设置' }).click(); await page.setViewportSize({ width: 1400, height: 940 })
  const cursor = async () => { await page.evaluate(() => { const e = window.__lspPerformanceFixture.editor; e.setPosition({ lineNumber: 4, column: 24 }); e.revealLineInCenter(4); e.focus() }); await page.waitForTimeout(60) }
  await cursor()
  await page.evaluate(() => window.__lspPerformanceFixture.editor.trigger('fixture', 'editor.action.revealDeclaration', {}))
  await page.waitForFunction(() => window.__lspPerformanceFixture.frames.some(frame => frame.method === 'textDocument/declaration'))
  await page.waitForFunction(() => window.__lspPerformanceFixture.editor.getPosition().lineNumber === 3)
  assert.match((await snapshot()).uri, /WorkspaceService\.java$/); checks.push('Real Monaco declaration action uses the new LSP provider and navigates within the current file')
  await cursor()
  await page.evaluate(() => window.__lspPerformanceFixture.editor.trigger('fixture', 'editor.action.goToTypeDefinition', {}))
  await page.waitForFunction(() => window.__lspPerformanceFixture.current.value.path.endsWith('HelperType.java'))
  await page.waitForFunction(() => window.__lspPerformanceFixture.editor.getPosition().lineNumber === 3)
  assert.ok(await page.evaluate(() => window.__lspPerformanceFixture.frames.some(frame => frame.method === 'textDocument/typeDefinition')))
  assert.equal((await snapshot()).position.column, 14)
  checks.push('Real Monaco type-definition action handles LocationLink and opens another project file at its selected range')
  await page.locator('#first').click(); await page.waitForFunction(() => window.__lspPerformanceFixture.current.value.path.endsWith('WorkspaceService.java')); await cursor()
  await page.evaluate(() => window.__lspPerformanceFixture.editor.trigger('fixture', 'editor.action.goToImplementation', {}))
  await page.waitForFunction(() => window.__lspPerformanceFixture.current.value.path.endsWith('HelperType.java'))
  await page.waitForFunction(() => window.__lspPerformanceFixture.editor.getPosition().lineNumber === 4)
  assert.equal((await snapshot()).position.column, 19)
  assert.ok(await page.evaluate(() => window.__lspPerformanceFixture.frames.some(frame => frame.method === 'textDocument/implementation')))
  checks.push('Real Monaco implementation action handles Location arrays and opens the implementing method')
  await page.locator('#first').click(); await page.waitForFunction(() => window.__lspPerformanceFixture.editor.getValue().includes('ORIGINAL_9700'))
  assert.equal((await snapshot()).text, draft.text)
  checks.push('Cross-file navigation preserves all unsaved large-document bytes')
  await openPanel(); await panel().getByRole('button', { name: '只看相关', exact: true }).click()
  assert.equal(await panel().locator('.lsp-list li').count(), 1)
  await shot('lsp-panel-desktop-related.png')
  assert.equal(await page.evaluate(() => window.__lspPerformanceFixture.saves.length), 0)
  assert.deepEqual(await page.evaluate(() => window.__lspPerformanceFixture.socketErrors), [])
  assert.deepEqual(unexpected, []); assert.deepEqual(errors, [])
  checks.push('All APIs and language sockets remain fixture-only; no save, real model or project file operation occurs')
} catch (error) { failure = error; errors.push(error.message); if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}) }
finally { await browser.close(); fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ checks, errors, unexpected, measurements, screenshots }, null, 2)) }
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, report: path.join(output, 'report.json') }))
