import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Actual Vue/Monaco/client UI, with every API and language socket isolated.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const origin = new URL(process.argv[2] || 'http://127.0.0.1:5175').origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/advanced-lsp-browser', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], errors = [], unexpected = []
const component = await (await fetch(`${origin}/src/components/code/CallHierarchyPanel.vue`)).text()
const vueModule = component.match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vueModule)
const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{margin:0;min-width:0;height:100%;}body{font:13px system-ui;background:#f5f7fa;}#app{height:100%;}.toolbar{height:40px;display:flex;gap:8px;align-items:center;padding:0 12px;background:#fff;border-bottom:1px solid #e3e7ef;}.surface{height:calc(100% - 41px);position:relative;}#editor{height:100%;width:100%;}
</style></head><body><div id="app"></div><script type="module">
import {createApp,h,ref,markRaw,nextTick} from '${vueModule}';
import CallHierarchyPanel from '/src/components/code/CallHierarchyPanel.vue';
import LspClient from '/src/lib/lspClient.js';
import {ensureMonaco} from '/src/lib/monacoSetup.js';
import '/src/style.css';
const state=window.__advanced={frames:[],navigations:[],errors:[],client:null,closed:0};
const uri='file:///E:/advanced-fixture/Root.java',other='file:///E:/advanced-fixture/Caller.java';
const range=(l=0,c=0,e=c+3)=>({start:{line:l,character:c},end:{line:l,character:e}});
const item=(name='resolve',target=uri)=>({name,kind:6,uri:target,range:range(2,4,11),selectionRange:range(2,4,11),detail:'demo.Root.'+name+'()',data:{opaque:name}});
const caps={textDocumentSync:{change:2},callHierarchyProvider:true,typeHierarchyProvider:true,codeLensProvider:{resolveProvider:true},inlayHintProvider:{resolveProvider:true}};
class Socket{static OPEN=1;constructor(url){this.readyState=1;this.url=url;queueMicrotask(()=>this.receive({method:'code-agent/ready',params:{server:'jdtls',capabilities:caps}}))}receive(value){this.onmessage?.({data:JSON.stringify(value)})}close(){this.readyState=3;this.onclose?.()}send(text){const frame=JSON.parse(text);state.frames.push(frame);if(frame.id===undefined)return;let result=null;
 if(frame.method==='textDocument/prepareCallHierarchy'||frame.method==='textDocument/prepareTypeHierarchy')result=[item()];
 if(frame.method==='callHierarchy/incomingCalls')result=frame.params.item.name==='resolve'?[{from:item('caller',other),fromRanges:[range(3,4,11)]}]:[{from:item(),fromRanges:[range(4,4,11)]}];
 if(frame.method==='callHierarchy/outgoingCalls')result=[{to:item('helper',other),fromRanges:[range(4,4,11)]}];
 if(frame.method==='typeHierarchy/supertypes')result=[item('BaseType',other)];
 if(frame.method==='typeHierarchy/subtypes')result=[];
 if(frame.method==='textDocument/codeLens')result=[{range:range(2,4,11),data:{opaque:'lens'}},{range:range(0,0,5),command:{title:'Unavailable command',command:'fixture.exec'}}];
 if(frame.method==='codeLens/resolve')result={...frame.params,command:{title:'1 reference',command:'java.show.references',arguments:[uri,{line:2,character:4},[{uri:other,range:range(3,4,11)}]]}};
 if(frame.method==='textDocument/inlayHint')result=[{position:{line:3,character:17},label:[{value:'input:',tooltip:{kind:'markdown',value:'**Parameter**'}}],kind:2,paddingRight:true,data:{opaque:'hint'}}];
 if(frame.method==='inlayHint/resolve')result={...frame.params,tooltip:{kind:'plaintext',value:'String parameter'}};
 queueMicrotask(()=>this.receive({id:frame.id,result}));
}}
window.WebSocket=Socket;
const monaco=ensureMonaco(),client=markRaw(new LspClient({projectId:'advanced-fixture',lang:'java',autoReconnect:false,onNavigate:payload=>{state.navigations.push(payload);return true}}));
state.client=client;client.connect();await client.waitForReady();
const model=monaco.editor.createModel('class Root {\n\n    String resolve(String input) {\n        return trim(input);\n    }\n}\n','java',monaco.Uri.parse(uri));
client.openDocument({uri,model});client.registerProviders();
const kind=ref('call'),open=ref(true);
createApp({setup(){return()=>h('div',{id:'app'},[h('div',{class:'toolbar'},[h('button',{id:'call',onClick:()=>{kind.value='call';open.value=true}},'调用'),h('button',{id:'type',onClick:()=>{kind.value='type';open.value=true}},'类型')]),h('main',{class:'surface'},[h('div',{id:'editor'}),open.value?h(CallHierarchyPanel,{client,uri,position:{lineNumber:3,column:8},kind:kind.value,onNavigate:payload=>state.navigations.push(payload),onClose:()=>{state.closed++;open.value=false}}):null])])}}).mount('#app');
state.editor=monaco.editor.create(document.querySelector('#editor'),{model,theme:'vs',automaticLayout:true,codeLens:true,inlayHints:{enabled:'on'},minimap:{enabled:false}});
state.monaco=monaco;state.nextTick=nextTick;
</script></body></html>`
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page, failure
try {
  const context = await browser.newContext({ viewport: { width: 1240, height: 820 }, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin || url.pathname.startsWith('/api/')) { unexpected.push(url.href); return route.abort() }
    if (url.pathname === '/__advanced-lsp-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    return route.continue()
  })
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/__advanced-lsp-fixture`, { waitUntil: 'networkidle' })
  const panel = () => page.locator('.hierarchy-panel')
  await panel().getByText('caller', { exact: true }).waitFor()
  assert.ok(await page.evaluate(() => window.__advanced.frames.some(frame => frame.method === 'callHierarchy/incomingCalls' && frame.params.item.data.opaque === 'resolve')))
  checks.push('Call hierarchy mounts and auto-expands actual client-issued opaque handles')
  await panel().getByRole('button', { name: '展开 caller', exact: true }).click()
  await panel().getByText('循环', { exact: true }).waitFor()
  assert.equal(await panel().locator('[role=treeitem]').count(), 3)
  checks.push('Recursive caller is marked as a cycle and cannot expand indefinitely')
  await panel().getByRole('button', { name: '它调用谁', exact: true }).click()
  await panel().getByText('helper', { exact: true }).waitFor()
  await panel().getByRole('button', { name: '展开 helper', exact: true }).click()
  await panel().getByRole('button', { name: '第 5 行', exact: true }).first().click()
  const location = await page.evaluate(() => window.__advanced.navigations.at(-1))
  assert.equal(location.uri, 'file:///E:/advanced-fixture/Root.java'); assert.equal(location.range.start.line, 4)
  checks.push('Outgoing call ranges navigate to the parent caller file, rather than the callee file')
  await panel().getByRole('button', { name: '关闭层级' }).click()
  await page.getByRole('button', { name: '1 reference', exact: true }).waitFor()
  await page.getByRole('button', { name: '1 reference', exact: true }).click()
  await page.waitForFunction(() => window.__advanced.navigations.some(item => item.kind === 'references'))
  const navigation = await page.evaluate(() => window.__advanced.navigations.find(item => item.kind === 'references'))
  assert.equal(navigation.locations[0].uri, 'file:///E:/advanced-fixture/Caller.java'); assert.equal(navigation.locations[0].range.startLineNumber, 4)
  assert.equal(await page.getByRole('button', { name: 'Unavailable command', exact: true }).count(), 0)
  checks.push('Real Monaco CodeLens resolves and routes a safe reference locally; unknown commands stay inert')
  await page.waitForFunction(() => window.__advanced.frames.some(frame => frame.method === 'textDocument/inlayHint'))
  assert.ok(await page.locator('.monaco-editor').getByText('input:', { exact: true }).count())
  checks.push('Actual Monaco viewport requests and renders LSP parameter inlay hints')
  await page.locator('#type').click(); await panel().getByText('BaseType', { exact: true }).waitFor()
  assert.ok(await page.evaluate(() => window.__advanced.frames.some(frame => frame.method === 'typeHierarchy/supertypes')))
  await panel().getByRole('button', { name: '子类型', exact: true }).click()
  await panel().getByText('没有关联类型', { exact: true }).waitFor()
  checks.push('Type hierarchy direction switches call supertypes/subtypes and render an honest empty state')
  await page.setViewportSize({ width: 390, height: 844 })
  const metrics = await panel().evaluate(element => ({ width: element.getBoundingClientRect().width, right: element.getBoundingClientRect().right, document: document.documentElement.scrollWidth, viewport: innerWidth }))
  assert.ok(metrics.width <= 375 && metrics.right <= 390 && metrics.document <= 392, JSON.stringify(metrics))
  await page.screenshot({ path: path.join(output, 'hierarchy-mobile.png'), fullPage: true })
  checks.push('Hierarchy panel remains contained at 390px without horizontal page overflow')
  await page.setViewportSize({ width: 1240, height: 820 }); await page.locator('#call').click(); await panel().getByRole('button', { name: '谁调用它', exact: true }).click(); await panel().getByText('caller', { exact: true }).waitFor()
  await page.screenshot({ path: path.join(output, 'hierarchy-desktop.png'), fullPage: true })
  assert.equal(await page.evaluate(() => window.__advanced.frames.some(frame => frame.method === 'workspace/executeCommand')), false)
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
  checks.push('Fixture-only execution performs no arbitrary language command or real project request')
} catch (error) { failure = error; errors.push(error.message); if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}) }
finally { await browser.close(); fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ checks, errors, unexpected }, null, 2)) }
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, report: path.join(output, 'report.json') }))
