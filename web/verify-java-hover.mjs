import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Real EditorPane + Monaco in Chrome, with an in-memory LSP only. The fixture
// cannot read a user project, write files, start language servers, or call AI.
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const origin = new URL(process.argv[2] || 'http://127.0.0.1:5175').origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/java-hover-browser', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], errors = [], unexpected = [], screenshots = [], observations = []
const source = `package demo;

import org.springframework.web.bind.annotation.RequestParam;

public class DocumentService implements Repository {
    private final String name = "documents";

    public Long list(@RequestParam(required = false) Long categoryId) {
        Long localId = categoryId;
        return unknownResult;
    }
}

interface Repository {
    Long list(Long categoryId);
}
`
const otherSource = `package other;

public class OtherService {
    public Long read(Long categoryId) {
        return categoryId;
    }
}

interface Long {}
`
const legend = { tokenTypes: ['class', 'interface', 'method', 'property', 'parameter', 'variable', 'decorator'], tokenModifiers: ['declaration', 'readonly', 'static'] }
const fixture = { source, otherSource, legend }
const componentResponse = await fetch(`${origin}/src/components/code/EditorPane.vue`)
assert.ok(componentResponse.ok, 'Vite must serve EditorPane before this verification runs')
const componentSource = await componentResponse.text()
const vueModule = componentSource.match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vueModule && new URL(vueModule, origin).origin === origin, 'Cannot locate the component Vue runtime')

const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{margin:0;height:100%;}body{background:#f4f6f9;font:14px system-ui;}#app{height:100%;display:flex;flex-direction:column;min-width:0;}.fixture-head{padding:12px 18px;display:flex;gap:12px;align-items:center;background:white;border-bottom:1px solid #e3e7ef}.fixture-head b{font-size:15px}.fixture-head small{color:#64748b}.fixture-shell{flex:1;min-height:0;padding:16px}.fixture-shell>.pane{border:1px solid #e3e7ef;border-radius:10px;overflow:hidden}
</style></head><body><div id="app"></div><script type="module">
import {createApp,h,ref,computed,nextTick} from '${vueModule}';
import EditorPane from '/src/components/code/EditorPane.vue';
import {ensureMonaco} from '/src/lib/monacoSetup.js';
import '/src/style.css';
const fixture=${JSON.stringify(fixture).replaceAll('<', '\\u003c')};
const state=window.__javaHoverFixture={frames:[],sockets:[],pending:[],holdHover:false,holdSemantic:false,saves:[],changes:[],mode:'online'};
function semantic(text,uri){
 const other=uri.endsWith('/OtherService.java'),tokens=[];
 for(const [line,lineText]of text.split('\n').entries())for(const match of lineText.matchAll(/\b[A-Za-z_$][\w$]*\b/g)){
  const word=match[0];let type=null,bits=0;
  if(word==='DocumentService'||word==='OtherService')type='class';
  else if(word==='Repository')type='interface';
  else if(word==='Long')type=other?'interface':'class';
  else if(word==='list'||word==='read')type='method';
  else if(word==='name'){type='property';bits=2}
  else if(word==='categoryId')type='parameter';
  else if(word==='localId')type='variable';
  else if(word==='RequestParam'&&lineText.includes('@'))type='decorator';
  else if(word==='required')type='property';
  if(type)tokens.push({line,column:match.index,length:word.length,type:fixture.legend.tokenTypes.indexOf(type),bits});
 }
 let previousLine=0,previousColumn=0;const data=[];
 for(const token of tokens){const delta=token.line-previousLine;data.push(delta,delta?token.column:token.column-previousColumn,token.length,token.type,token.bits);previousLine=token.line;previousColumn=token.column}
 return {data};
}
class FixtureSocket extends EventTarget{
 static CONNECTING=0;static OPEN=1;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;this.docs=new Map();this.mode=state.mode;this.isLsp=new URL(url,location.href).pathname.endsWith('/lsp/stream');state.sockets.push(this);queueMicrotask(()=>{this.readyState=1;this.emit('open',new Event('open'));if(this.isLsp)this.message({method:'code-agent/ready',params:{server:'Fixture Java',capabilities:{hoverProvider:this.mode!=='no-hover',textDocumentSync:{change:1,save:{includeText:true}},semanticTokensProvider:{legend:fixture.legend,full:true}}}})})}
 emit(type,event){this.dispatchEvent(event);this['on'+type]?.(event)}
 message(frame){if(this.readyState===1)this.emit('message',new MessageEvent('message',{data:JSON.stringify(frame)}))}
 send(raw){if(!this.isLsp)return;const frame=JSON.parse(raw);state.frames.push({...frame,fixtureMode:this.mode});const uri=frame.params?.textDocument?.uri;
  if(frame.method==='textDocument/didOpen')this.docs.set(uri,{...frame.params.textDocument});
  if(frame.method==='textDocument/didChange'){const doc=this.docs.get(uri);if(doc){doc.text=frame.params.contentChanges[0].text;doc.version=frame.params.textDocument.version}}
  if(frame.method==='textDocument/didClose')this.docs.delete(uri);
  if(frame.id===undefined)return;
  const text=this.docs.get(uri)?.text||'',position=frame.params?.position,line=text.split('\n')[position?.line]||'';
  let result=null;
  if(frame.method==='textDocument/semanticTokens/full')result=semantic(text,uri);
  if(frame.method==='textDocument/hover'&&this.mode!=='no-hover'){
   const match=[...line.matchAll(/\b[A-Za-z_$][\w$]*\b/g)].find(item=>item.index<=position.character&&position.character<item.index+item[0].length);
   if(match?.[0]==='RequestParam')result={contents:[{language:'java',value:'org.springframework.web.bind.annotation.RequestParam'},{kind:'markdown',value:'Source: *spring-web-6.2.19.jar*'}],range:{start:{line:position.line,character:match.index},end:{line:position.line,character:match.index+match[0].length}}};
  }
  const deliver=()=>this.message({id:frame.id,result});
  if((state.holdHover&&frame.method==='textDocument/hover')||(state.holdSemantic&&frame.method==='textDocument/semanticTokens/full'))state.pending.push({method:frame.method,deliver});else queueMicrotask(deliver);
 }
 close(){this.readyState=3;this.emit('close',new CloseEvent('close'))}
}
window.WebSocket=FixtureSocket;
const mode=ref('online'),active=ref('DocumentService.java'),pane=ref(null);
const files=new Map([
 ['DocumentService.java',{path:'src/demo/DocumentService.java',text:fixture.source,lang:'java',sha1:'fixture-a',uri:'file:///E:/java-hover-fixture/src/demo/DocumentService.java',total_lines:fixture.source.split('\n').length,editable:true,size:fixture.source.length}],
 ['OtherService.java',{path:'src/other/OtherService.java',text:fixture.otherSource,lang:'java',sha1:'fixture-b',uri:'file:///E:/java-hover-fixture/src/other/OtherService.java',total_lines:fixture.otherSource.split('\n').length,editable:true,size:fixture.otherSource.length}],
]);
const current=computed(()=>files.get(active.value));
const rows=computed(()=>[...files.values()].map(file=>({key:'java-hover-fixture/'+mode.value+'/main/'+file.path,file,uri:file.uri,path:file.path,lang:file.lang,text:file.text})));
const servers=computed(()=>[{id:'fixture-java',name:'Fixture Java',languages:['java'],state:mode.value==='offline'?'missing':'installed',available:mode.value!=='offline'}]);
function setMode(value){state.mode=value;mode.value=value;active.value='DocumentService.java'}
createApp({setup(){return()=>h('div',{id:'app'},[
 h('header',{class:'fixture-head'},[h('b','Java 逐词说明'),h('small','本地隔离验收 · 真实 Monaco'),h('button',{id:'first',onClick:()=>active.value='DocumentService.java'},'DocumentService.java'),h('button',{id:'second',onClick:()=>active.value='OtherService.java'},'OtherService.java'),h('button',{id:'no-hover',onClick:()=>setMode('no-hover')},'无 Hover 服务'),h('button',{id:'offline',onClick:()=>setMode('offline')},'离线模式')]),
 h('main',{class:'fixture-shell'},[h(EditorPane,{key:mode.value,ref:pane,file:current.value,documentKey:'java-hover-fixture/'+mode.value+'/main/'+current.value.path,documents:rows.value,projectId:'java-hover-fixture-'+mode.value,sessionId:'java-hover-session',readonly:false,languageServers:servers.value,lspServer:servers.value[0],onChange:change=>{state.changes.push(change);const file=[...files.values()].find(item=>item.path===change.path);if(file)file.text=change.text},onSave:event=>state.saves.push(event)})])
])}}).mount('#app');
Object.assign(state,{pane,active,modeRef:mode,nextTick,files,monaco:ensureMonaco(),release(){state.holdHover=false;state.holdSemantic=false;for(const item of state.pending.splice(0))item.deliver()}});
Object.defineProperty(state,'editor',{get(){return state.monaco.editor.getEditors().find(editor=>!!editor.getModel())}});
</script></body></html>`

const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let failure, page
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 930 }, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin || url.pathname.startsWith('/api/')) { unexpected.push(url.href); return route.abort() }
    if (url.pathname === '/__java-hover-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    return route.continue()
  })
  page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/__java-hover-fixture`, { waitUntil: 'networkidle' })
  await page.waitForFunction(() => !!window.__javaHoverFixture?.editor)
  await page.waitForFunction(() => window.__javaHoverFixture.frames.some(frame => frame.method === 'textDocument/semanticTokens/full'))
  // Allow Monaco to consume the in-memory semantic response, not just send it.
  await page.waitForTimeout(150)
  const native = () => page.locator('.monaco-resizable-hover:visible').last()
  const shot = async name => { const filename = path.join(output, name); await page.waitForTimeout(200); await page.screenshot({ path: filename, fullPage: true }); screenshots.push(filename) }
  const position = async (needle, word, occurrence = 0) => page.evaluate(({ needle, word, occurrence }) => {
    const editor = window.__javaHoverFixture.editor, model = editor.getModel()
    const lines = model.getValue().split('\n'), line = lines.findIndex(text => text.includes(needle))
    if (line < 0) throw new Error('Missing fixture line: ' + needle)
    let offset = -1
    for (let i = 0; i <= occurrence; i++) offset = lines[line].indexOf(word, offset + 1)
    if (offset < 0) throw new Error('Missing fixture token: ' + word)
    const target = { lineNumber: line + 1, column: offset + Math.max(1, Math.floor(word.length / 2)) + 1 }
    editor.setPosition(target); editor.revealLineInCenter(line + 1); editor.focus()
    return target
  }, { needle, word, occurrence })
  const inspectHover = async ({ needle, word, expected, rejected, label, occurrence = 0 }) => {
    await page.keyboard.press('Escape')
    await position(needle, word, occurrence)
    await page.evaluate(() => window.__javaHoverFixture.editor.getAction('editor.action.showHover').run())
    await native().waitFor()
    await page.waitForFunction(({ word }) => [...document.querySelectorAll('.monaco-resizable-hover')].some(el => el.getBoundingClientRect().height && el.textContent.includes(word)), { word })
    const text = await native().innerText()
    assert.match(text, expected, `${label}: ${text}`)
    if (rejected) assert.doesNotMatch(text, rejected, `${label}: ${text}`)
    observations.push({ label, word, text }); checks.push(label)
    return text
  }

  const cases = [
    { needle: 'public class', word: 'public', expected: /修饰符/, label: 'public is explained as an access modifier' },
    { needle: 'public class', word: 'class', expected: /关键字/, label: 'class has a keyword explanation' },
    { needle: 'public class', word: 'DocumentService', expected: /DocumentService\s*·\s*类(?:\s|$)/, label: 'A class declaration has its class attribute' },
    { needle: 'implements Repository', word: 'Repository', expected: /接口/, label: 'An implemented interface is classified as an interface' },
    { needle: 'interface Repository', word: 'Repository', expected: /接口/, label: 'An interface declaration has its interface attribute' },
    { needle: 'public Long list', word: 'list', expected: /方法/, label: 'A method declaration is classified as a method' },
    { needle: 'private final String', word: 'name', expected: /字段|成员变量/, label: 'A field is distinguished from a local variable' },
    { needle: 'public Long list', word: 'categoryId', expected: /参数/, label: 'A formal parameter is classified as a parameter' },
    { needle: 'Long localId', word: 'localId', expected: /局部变量|变量/, label: 'A local declaration is classified as a variable' },
    { needle: 'Long localId', word: 'categoryId', expected: /参数/, label: 'A parameter reference keeps the parameter attribute' },
    { needle: 'public Long list', word: 'RequestParam', expected: /注解/, label: 'An annotation name has an annotation explanation' },
    { needle: 'public Long list', word: 'required', expected: /注解.*(?:属性|元素|参数)|(?:属性|元素).*注解/s, label: 'An annotation attribute is distinguished from an ordinary field' },
    { needle: 'public Long list', word: 'false', expected: /布尔.*字面量|字面量.*布尔/s, label: 'false is explained as a Boolean literal' },
    { needle: 'public Long list', word: 'Long', expected: /Long\s*·\s*类(?:\s|$)[\s\S]*语言服务器语义信息/, label: 'A semantic class token enriches a type reference' },
  ]
  for (const item of cases) await inspectHover(item)

  const annotation = await inspectHover({ needle: 'public Long list', word: 'RequestParam', expected: /注解/, label: 'Enhanced annotation hover retains original server documentation' })
  assert.match(annotation, /org\.springframework\.web\.bind\.annotation\.RequestParam/)
  assert.match(annotation, /Source:\s*spring-web-6\.2\.19\.jar/)
  await shot('java-hover-annotation.png')
  await inspectHover({ needle: 'public class', word: 'public', expected: /修饰符/, label: 'Modifier hover renders through the native Monaco surface' })
  await shot('java-hover-modifier.png')

  await page.keyboard.press('Escape'); await position('public Long list', 'RequestParam')
  const openFullDocumentation = () => page.evaluate(() => {
    const action = window.__javaHoverFixture.editor.getAction('code-agent.symbol-documentation')
    if (!action) throw new Error('The complete documentation context-menu action is missing')
    return action.run()
  })
  assert.equal(await page.locator('.language-tool').filter({ hasText: /^说明$/ }).count(), 0)
  await openFullDocumentation()
  const drawer = page.getByRole('complementary', { name: '代码完整说明' })
  await drawer.waitFor()
  await page.waitForFunction(() => document.querySelector('.documentation-body')?.getAttribute('aria-busy') === 'false')
  assert.match(await drawer.innerText(), /注解/)
  assert.match(await drawer.innerText(), /org\.springframework\.web\.bind\.annotation\.RequestParam/)
  assert.match(await drawer.innerText(), /spring-web-6\.2\.19\.jar/)
  checks.push('Full documentation uses the same enriched explanation and preserves server sections')
  await drawer.getByRole('button', { name: '关闭完整说明' }).click()

  // An old class token must not survive a model revision. Hold the new semantic
  // response so that a passing result cannot be explained by a refreshed cache.
  await page.evaluate(() => {
    const state = window.__javaHoverFixture, editor = state.editor, model = editor.getModel()
    state.holdSemantic = true
    const line = model.getValue().split('\n').findIndex(text => text.includes('public Long list')) + 1
    const column = model.getLineContent(line).indexOf('Long') + 1
    editor.executeEdits('fixture', [{ range: new state.monaco.Range(line, column, line, column + 4), text: 'Myst' }])
  })
  await inspectHover({ needle: 'public Myst list', word: 'Myst', expected: /类型|标识符/, rejected: /Myst\s*·\s*类(?:\s|$)|语言服务器语义信息/, label: 'Edited text cannot reuse a class classification from an older model revision' })
  await page.evaluate(() => { const state = window.__javaHoverFixture; state.editor.setValue(state.files.get('DocumentService.java').text.replace('public Myst list', 'public Long list')); state.release() })

  await page.keyboard.press('Escape')
  await page.evaluate(() => { window.__javaHoverFixture.holdHover = true })
  await position('public Long list', 'RequestParam')
  await page.evaluate(() => { window.__javaHoverFixture.editor.getAction('editor.action.showHover').run() })
  await page.waitForFunction(() => window.__javaHoverFixture.pending.some(item => item.method === 'textDocument/hover'))
  await page.locator('#second').click()
  await page.waitForFunction(() => window.__javaHoverFixture.editor.getModel().uri.toString().endsWith('/OtherService.java'))
  await page.evaluate(() => window.__javaHoverFixture.release())
  await page.waitForFunction(() => window.__javaHoverFixture.frames.some(frame => frame.method === 'textDocument/semanticTokens/full' && frame.params.textDocument.uri.endsWith('/OtherService.java')))
  await page.waitForTimeout(150)
  await inspectHover({ needle: 'public Long read', word: 'Long', expected: /接口/, rejected: /RequestParam|spring-web/, label: 'Another document uses its own interface tokens and rejects a late prior hover' })
  assert.ok(await page.evaluate(() => window.__javaHoverFixture.frames.some(frame => frame.method === '$/cancelRequest')))
  checks.push('Switching files cancels the outstanding LSP hover request')

  await page.locator('#no-hover').click()
  await page.waitForFunction(() => window.__javaHoverFixture.editor?.getValue().includes('DocumentService') && window.__javaHoverFixture.sockets.some(socket => socket.mode === 'no-hover' && socket.readyState === 1))
  await inspectHover({ needle: 'public class', word: 'public', expected: /修饰符/, label: 'A server without hover capability still explains Java keywords' })
  await inspectHover({ needle: 'return unknownResult', word: 'unknownResult', expected: /标识符|未解析|待确认|无法确定/, label: 'A server without hover capability still explains unknown identifiers honestly' })
  assert.equal(await page.evaluate(() => window.__javaHoverFixture.frames.filter(frame => frame.fixtureMode === 'no-hover' && frame.method === 'textDocument/hover').length), 0)
  checks.push('No hover request is sent when the server does not advertise hover support')

  const socketCount = await page.evaluate(() => window.__javaHoverFixture.sockets.length)
  await page.locator('#offline').click()
  await page.waitForFunction(() => window.__javaHoverFixture.mode === 'offline' && !!window.__javaHoverFixture.editor)
  await inspectHover({ needle: 'public class', word: 'class', expected: /关键字/, label: 'Java keyword explanations work with no language server' })
  await inspectHover({ needle: 'return unknownResult', word: 'unknownResult', expected: /标识符|未解析|待确认|无法确定/, label: 'Unknown identifier explanations remain available offline' })
  assert.equal(await page.evaluate(() => window.__javaHoverFixture.sockets.length), socketCount)
  checks.push('Offline inspection opens no WebSocket or backend connection')
  await shot('java-hover-offline.png')
  await page.keyboard.press('Escape'); await position('public class', 'public')
  await openFullDocumentation(); await drawer.waitFor()
  await page.waitForFunction(() => document.querySelector('.documentation-body')?.getAttribute('aria-busy') === 'false')
  assert.match(await drawer.innerText(), /修饰符/)
  checks.push('The full documentation context-menu action also works offline for Java tokens')

  assert.equal(await page.evaluate(() => window.__javaHoverFixture.saves.length), 0)
  checks.push('Inspection never saves a file')
  if (unexpected.length) errors.push(`Blocked unexpected requests: ${unexpected.join(', ')}`)
  assert.deepEqual(errors, [])
} catch (error) {
  failure = error; errors.push(error.message)
  if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
} finally {
  await browser.close()
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ checks, errors, unexpected, observations, screenshots }, null, 2))
}
if (failure) throw failure
console.log(JSON.stringify({ passed: checks.length, report: path.join(output, 'report.json') }))
