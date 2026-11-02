import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

// Exercise the real EditorPane, language-client providers and Monaco renderer.
// All projects, language responses and optional business lookups exist only in
// this page; it has no access to a user workspace or model service.
const origin = new URL(process.argv[2] || 'http://127.0.0.1:5200').origin
const output = path.resolve(process.argv[3] || '../.tmp/hover-polish-20261031/browser')
const baselineOnly = process.argv.includes('--baseline')
fs.mkdirSync(output, { recursive: true })
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const report = { passed: false, mode: baselineOnly ? 'identity-projection-baseline' : 'compact', checks: [], errors: [], unexpected: [], screenshots: [], measurements: {}, observations: {} }
const component = await (await fetch(`${origin}/src/components/code/EditorPane.vue`)).text()
const vue = component.match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1]
assert.ok(vue, 'Vite must expose the real Vue component')
const java = `package demo;
public class SessionService {
    /** 保存创建时间 setter 的方法名。 */
    public static final String SET_CREATE_TIME = "setCreateTime";
    private int currentCount = 0;
    public java.util.Map<String, java.util.List<String>> loadSession(String userToken, String tenant, java.util.Map<String, java.util.List<String>> labels) { return null; }
}`
const python = `def inspect_session(session_id: str, user_id: str) -> dict[str, str]:
    return {"id": session_id, "user": user_id}

inspect_session("demo", "owner")`
const javaSignature = 'public java.util.Map<String, java.util.List<String>> demo.SessionService.loadSession(String userToken, String tenant, java.util.Map<String, java.util.List<String>> labels)'
const pythonSignature = 'def inspect_session(session_id: str, user_id: str) -> dict[str, str]'
const chinese = '读取当前用户的会话信息，并按租户隔离返回可访问的数据。'
const longDoc = chinese + '\n\n' + Array.from({ length: 15 }, (_, i) => `### 使用约束 ${i + 1}\n\n会话编号与用户编号必须匹配；失效会话不会返回其他用户的数据。传入数据保持原样，操作不修改编辑器中的文件。`).join('\n\n') + '\n\n完整文档尾标 FULL_DOCUMENTATION_TAIL_531d'
const fixture = { java, python, javaSignature, pythonSignature, longDoc }
const html = String.raw`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{margin:0;height:100%;overflow:hidden}body{font:14px system-ui;background:#f6f7fb}#app{height:100%;display:flex;flex-direction:column;min-width:0}.fixture-head{height:40px;display:flex;align-items:center;gap:8px;padding:0 12px;background:white;border-bottom:1px solid #dce2ec;flex:none;overflow-x:auto;white-space:nowrap}.fixture-head b{margin-right:auto}.fixture-workspace{display:flex;flex:1;min-height:0;min-width:0}.fixture-main{min-width:0;flex:1;display:flex;padding:12px}.fixture-main>.pane{border:1px solid #dce2ec;overflow:hidden;border-radius:8px}.fixture-ai{width:290px;flex:none;border-left:1px solid #dce2ec;background:white;padding:18px;box-sizing:border-box;color:#64748b}.fixture-ai h2{font-size:14px;color:#1e293b}@media(max-width:900px){.fixture-ai{display:none}.fixture-head b{display:none}.fixture-main{padding:6px}}
</style></head><body><div id="app"></div><script type="module">
import {createApp,h,ref,computed,nextTick} from '${vue}';
import EditorPane from '/src/components/code/EditorPane.vue';
import {ensureMonaco} from '/src/lib/monacoSetup.js';
import '/src/style.css';
const fixture=${JSON.stringify(fixture).replaceAll('<', '\u003c')};
const state=window.__hoverPresentationFixture={frames:[],sockets:[],changes:[],saves:[],translation:'ready'};
const legend={tokenTypes:['class','method','property','function','parameter'],tokenModifiers:['static','readonly','declaration']};
function response(text,uri,position){
 const line=text.split('\n')[position.line]||'',match=[...line.matchAll(/\b[A-Za-z_$][\w$]*\b/g)].find(item=>item.index<=position.character&&position.character<item.index+item[0].length);
 const word=match?.[0];let contents;
 if(uri.endsWith('.py'))contents=[{language:'python',value:fixture.pythonSignature},{kind:'markdown',value:state.translation==='ready'?fixture.longDoc:'Returns the current user session. ENGLISH_ORIGINAL_TAIL_9b1e'},'com',{kind:'markdown',value:'Source: *sky-server*'}];
 else if(word==='loadSession')contents=[{language:'java',value:fixture.javaSignature},{kind:'markdown',value:fixture.longDoc},'com',{kind:'markdown',value:'Source: *sky-server*'}];
 else if(word==='SET_CREATE_TIME')contents=[{language:'java',value:'public static final String demo.SessionService.SET_CREATE_TIME = "setCreateTime"'},{kind:'markdown',value:'保存创建时间 setter 的方法名。'},'com',{kind:'markdown',value:'Source: *sky-server*'}];
 else contents=[];
 if(!contents.length)return null;
 return {contents,documentation_translation:{language:'zh-CN',source:'configured-model',status:state.translation},_code_agent_original:{contents:[{language:uri.endsWith('.py')?'python':'java',value:uri.endsWith('.py')?fixture.pythonSignature:word==='loadSession'?fixture.javaSignature:'public static final String demo.SessionService.SET_CREATE_TIME = "setCreateTime"'},{kind:'markdown',value:'Returns the current user session. ENGLISH_ORIGINAL_TAIL_9b1e'},'com',{kind:'markdown',value:'Source: *sky-server*'}]},range:{start:{line:position.line,character:match?.index||0},end:{line:position.line,character:(match?.index||0)+(word?.length||1)}}};
}
function semantic(text,uri){let prevLine=0,prevCol=0;const data=[];for(const[line,value]of text.split('\n').entries())for(const match of value.matchAll(/\b[A-Za-z_$][\w$]*\b/g)){let type,bits=0;if(match[0]==='SessionService')type='class';else if(match[0]==='loadSession')type='method';else if(match[0]==='inspect_session')type='function';else if(match[0]==='SET_CREATE_TIME'){type='property';bits=3}else if(match[0]==='currentCount')type='property';if(type){const delta=line-prevLine;data.push(delta,delta?match.index:match.index-prevCol,match[0].length,legend.tokenTypes.indexOf(type),bits);prevLine=line;prevCol=match.index}}return {data}}
class FixtureSocket extends EventTarget{
 static CONNECTING=0;static OPEN=1;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.docs=new Map();this.readyState=0;this.isLsp=new URL(url,location.href).pathname.endsWith('/lsp/stream');state.sockets.push(this);queueMicrotask(()=>{this.readyState=1;this.emit('open',new Event('open'));if(this.isLsp)this.message({method:'code-agent/ready',params:{server:'Fixture language service',capabilities:{hoverProvider:true,textDocumentSync:{change:1,save:{includeText:true}},semanticTokensProvider:{legend,full:true}}}})})}
 emit(type,event){this.dispatchEvent(event);this['on'+type]?.(event)}message(frame){if(this.readyState===1)this.emit('message',new MessageEvent('message',{data:JSON.stringify(frame)}))}
 send(raw){if(!this.isLsp)return;const frame=JSON.parse(raw);state.frames.push(frame);const uri=frame.params?.textDocument?.uri;if(frame.method==='textDocument/didOpen')this.docs.set(uri,{...frame.params.textDocument});if(frame.method==='textDocument/didChange'){const d=this.docs.get(uri);if(d){d.text=frame.params.contentChanges[0].text;d.version=frame.params.textDocument.version}}if(frame.method==='textDocument/didClose')this.docs.delete(uri);if(frame.id===undefined)return;const text=this.docs.get(uri)?.text||'';const result=frame.method==='textDocument/hover'?response(text,uri,frame.params.position):frame.method==='textDocument/semanticTokens/full'?semantic(text,uri):null;queueMicrotask(()=>this.message({id:frame.id,result}))}
 close(){this.readyState=3;this.emit('close',new CloseEvent('close'))}
}
window.WebSocket=FixtureSocket;
const lang=ref('java'),generation=ref(0),pane=ref(null);const files={java:{path:'src/demo/SessionService.java',text:fixture.java,lang:'java',sha1:'java-fixture',uri:'file:///E:/hover-presentation-fixture/src/demo/SessionService.java',total_lines:fixture.java.split('\n').length,editable:true,size:fixture.java.length},python:{path:'inspect_session.py',text:fixture.python,lang:'python',sha1:'py-fixture',uri:'file:///E:/hover-presentation-fixture/inspect_session.py',total_lines:fixture.python.split('\n').length,editable:true,size:fixture.python.length}};const file=computed(()=>files[lang.value]),servers=computed(()=>[{id:'fixture-'+lang.value,name:'Fixture '+lang.value,languages:[lang.value],state:'installed',available:true}]);
function choose(language,status='ready'){lang.value=language;state.translation=status;generation.value++}
createApp({setup(){return()=>h('div',{id:'app'},[h('header',{class:'fixture-head'},[h('b','Code-agent · 悬停说明验收'),h('button',{id:'java',onClick:()=>choose('java')},'Java'),h('button',{id:'python',onClick:()=>choose('python')},'Python'),h('button',{id:'pending',onClick:()=>choose('python','pending')},'翻译处理中'),h('button',{id:'failed',onClick:()=>choose('python','failed')},'翻译失败')]),h('div',{class:'fixture-workspace'},[h('main',{class:'fixture-main'},[h(EditorPane,{key:lang.value+'-'+generation.value,ref:pane,file:file.value,documents:[{key:'hover-presentation-fixture/main/'+file.value.path,file:file.value,uri:file.value.uri,path:file.value.path,lang:file.value.lang,text:file.value.text}],documentKey:'hover-presentation-fixture/main/'+file.value.path,projectId:'hover-presentation-fixture',sessionId:'hover-presentation-session',readonly:false,languageServers:servers.value,lspServer:servers.value[0],onChange:event=>state.changes.push(event),onSave:event=>state.saves.push(event)})]),h('aside',{class:'fixture-ai'},[h('h2','AI 代码助手'),h('p','右侧面板验收边界：悬停说明应留在编辑器内，不遮挡此区域。')])])])}}).mount('#app');
Object.assign(state,{pane,lang,generation,nextTick,monaco:ensureMonaco(),files});Object.defineProperty(state,'editor',{get(){return state.monaco.editor.getEditors().find(e=>e.getModel())}});
</script></body></html>`

const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let page
const checks = report.checks
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (baselineOnly && url.origin === origin && url.pathname === '/src/lib/compactSymbolHover.js') return route.fulfill({ contentType: 'application/javascript', body: 'export function compactSymbolHover(document) { return document }' })
    if (url.origin === origin && url.pathname === '/api/v1/code/projects/hover-presentation-fixture/symbol/business/resolve' && route.request().method() === 'POST') {
      const body = route.request().postDataJSON()
      assert.equal(body.path, 'src/demo/SessionService.java')
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ entity: null, business: null }) })
    }
    if (url.origin !== origin || url.pathname.startsWith('/api/')) { report.unexpected.push(url.href); return route.abort() }
    if (url.pathname === '/__hover-presentation-fixture') return route.fulfill({ contentType: 'text/html', body: html })
    return route.continue()
  })
  page = await context.newPage()
  page.on('pageerror', error => report.errors.push(error.message))
  await page.goto(`${origin}/__hover-presentation-fixture`, { waitUntil: 'networkidle' })
  const native = () => page.locator('.monaco-resizable-hover:visible').last()
  const drawer = () => page.getByRole('complementary', { name: '代码完整说明' })
  const waitEditor = async language => {
    await page.waitForFunction(lang => window.__hoverPresentationFixture?.editor?.getModel()?.getLanguageId() === lang, language)
    await page.waitForFunction(lang => window.__hoverPresentationFixture.frames.some(f => f.method === 'textDocument/semanticTokens/full' && f.params.textDocument.uri.endsWith(lang === 'java' ? '.java' : '.py')), language)
    await page.waitForTimeout(120)
  }
  const cursor = async (word, occurrence = 0) => page.evaluate(({ word, occurrence }) => {
    const e = window.__hoverPresentationFixture.editor, model = e.getModel(), matches = [...model.getValue().matchAll(new RegExp('\\b' + word + '\\b', 'g'))], match = matches[occurrence]
    if (!match) throw new Error('Missing fixture symbol ' + word)
    const pos = model.getPositionAt(match.index + Math.floor(word.length / 2)); e.setPosition(pos); e.revealPositionInCenter(pos); e.focus()
    const visible = e.getScrolledVisiblePosition(pos), layout = e.getLayoutInfo()
    if (visible) e.setScrollLeft(Math.max(0, e.getScrollLeft() + visible.left - (layout.contentLeft + layout.contentWidth / 2)))
  }, { word, occurrence })
  const hover = async (word, occurrence = 0) => {
    await page.keyboard.press('Escape'); await cursor(word, occurrence)
    await page.waitForTimeout(80)
    await page.evaluate(() => window.__hoverPresentationFixture.editor.getAction('editor.action.showHover').run())
    await native().waitFor(); await page.waitForFunction(word => [...document.querySelectorAll('.monaco-resizable-hover')].some(el => el.offsetHeight && el.textContent.includes(word)), word)
    await page.waitForTimeout(150)
    return native().innerText()
  }
  const geometry = async label => {
    const result = await native().evaluate(el => {
      const b = el.getBoundingClientRect(), editor = document.querySelector('.editor').getBoundingClientRect()
      return { x: b.x, y: b.y, width: b.width, height: b.height, right: b.right, bottom: b.bottom, editor: { x: editor.x, right: editor.right, width: editor.width }, viewport: { width: innerWidth, height: innerHeight }, rows: el.querySelectorAll('.hover-row').length, paragraphs: el.querySelectorAll('p').length, bodyOverflow: document.documentElement.scrollWidth > innerWidth + 1, markdownHtml: el.querySelector('.markdown-hover')?.innerHTML }
    })
    report.measurements[label] = result
    assert.ok(result.x >= -1 && result.right <= result.viewport.width + 1, JSON.stringify(result))
    assert.ok(result.y >= -1 && result.bottom <= result.viewport.height + 1, JSON.stringify(result))
    assert.ok(result.right <= result.editor.right + 2, `Hover must not cover AI panel: ${JSON.stringify(result)}`)
    assert.equal(result.bodyOverflow, false)
    return result
  }
  const shot = async name => {
    const file = path.join(output, name); await page.screenshot({ path: file, fullPage: true }); report.screenshots.push(file)
    if (await native().count()) { const card = file.replace(/\.png$/, '-card.png'); await native().screenshot({ path: card }); report.screenshots.push(card) }
  }
  const openFull = async () => {
    await page.keyboard.press('Escape')
    await page.evaluate(() => window.__hoverPresentationFixture.editor.getAction('code-agent.symbol-documentation').run())
    await drawer().waitFor(); await page.waitForFunction(() => document.querySelector('.documentation-body')?.getAttribute('aria-busy') === 'false')
  }
  const clean = text => assert.doesNotMatch(text, /文档中文翻译：|模型翻译，|识别依据：|作用依据：|功能依据：|来源：|Source:|sky-server|FULL_DOCUMENTATION_TAIL_531d|^com$/m)
  await waitEditor('java')
  if (baselineOnly) {
    const constant = await hover('SET_CREATE_TIME'); report.observations.constant = constant
    assert.match(constant, /sky-server/); await geometry('classConstantDesktop'); await shot('hover-class-constant-before.png')
    const method = await hover('loadSession'); report.observations.method = method
    assert.match(method, /FULL_DOCUMENTATION_TAIL_531d/); await geometry('methodDesktop'); await shot('hover-method-before.png')
    checks.push('Identity projection baseline uses the same Monaco, layout and CSS while retaining all server rows')
    assert.deepEqual(report.errors, []); assert.deepEqual(report.unexpected, []); report.passed = true
  } else {
  const constant = await hover('SET_CREATE_TIME'); report.observations.constant = constant
  assert.match(constant, /SET_CREATE_TIME\s*·\s*类常量/); assert.match(constant, /保存创建时间 setter 的方法名/); clean(constant)
  assert.doesNotMatch(constant, /中文说明尚未就绪/)
  const constantGeo = await geometry('classConstantDesktop'); assert.ok(constantGeo.height < 300); assert.equal(await native().locator('.markdown-hover').count(), 1)
  await shot('hover-class-constant-desktop.png'); checks.push('Class constant hover is a single compact section with its purpose and no provenance boilerplate')
  const variable = await hover('currentCount'); assert.match(variable, /成员变量/); clean(variable); checks.push('Mutable member remains a member variable rather than a class constant')
  const method = await hover('loadSession'); report.observations.method = method
  assert.match(method, /loadSession\s*·\s*方法/); assert.match(method, /当前用户的会话信息/); assert.match(method, /String userToken/); clean(method)
  await geometry('methodDesktop'); await shot('hover-method-desktop.png'); checks.push('Long method signature wraps inside the editor and complete prose remains out of the quick hover')
  await openFull(); let full = await drawer().innerText()
  assert.match(full, /FULL_DOCUMENTATION_TAIL_531d/); assert.match(full, /java\.util\.Map/)
  assert.doesNotMatch(full, /sky-server|语言服务器文档（模型中文翻译）/)
  assert.equal(await drawer().locator('.documentation-provenance').getAttribute('open'), null)
  await drawer().locator('.documentation-provenance summary').click(); full = await drawer().innerText()
  assert.match(full, /sky-server/); assert.match(full, /语言服务器文档（模型中文翻译）/)
  await drawer().getByRole('button', { name: '原文', exact: true }).click(); assert.match(await drawer().innerText(), /ENGLISH_ORIGINAL_TAIL_9b1e/)
  await page.keyboard.press('Escape'); await drawer().waitFor({ state: 'detached' }); checks.push('Ctrl+Q keeps full signature, source, translation provenance and opt-in original; Escape closes it')
  for (const width of [768, 390]) {
    await page.setViewportSize({ width, height: 844 }); await page.waitForTimeout(100)
    const text = await hover('loadSession'); clean(text); await geometry('method' + width); await shot(`hover-method-${width}.png`)
    if (width === 390) {
      const box = await native().boundingBox(); await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
      for (let attempt = 0; attempt < 12; attempt++) {
        await page.mouse.wheel(0, 240); await page.waitForTimeout(32)
        if (await native().evaluate(el => { const tip = el.getBoundingClientRect(), footer = el.querySelector('blockquote')?.getBoundingClientRect(); return footer && footer.top >= tip.top && footer.bottom <= tip.bottom })) break
      }
      assert.ok(await native().evaluate(el => { const tip = el.getBoundingClientRect(), footer = el.querySelector('blockquote')?.getBoundingClientRect(); return footer && footer.top >= tip.top && footer.bottom <= tip.bottom }), 'Long signature hover must reveal its footer through real wheel input')
      await shot('hover-method-390-scrolled.png')
      checks.push('A long signature on a narrow screen scrolls through real wheel input to the complete-documentation hint')
    }
  }
  checks.push('768px and 390px layouts keep the quick hover bounded and retain its heading, meaning and signature')
  await page.setViewportSize({ width: 1440, height: 960 }); await page.locator('#python').click(); await waitEditor('python')
  const pythonText = await hover('inspect_session'); report.observations.python = pythonText
  assert.match(pythonText, /inspect_session\s*·\s*函数/); assert.match(pythonText, /dict\[str, str\]/); clean(pythonText); await geometry('pythonDesktop'); await shot('hover-python-desktop.png')
  assert.ok(await page.evaluate(() => window.__hoverPresentationFixture.frames.some(f => f.method === 'textDocument/hover' && f.params.textDocument.uri.endsWith('.py'))))
  checks.push('Python uses the real non-Java language provider and the same compact presentation')
  for (const [mode, expected] of [['pending', /中文|翻译/], ['failed', /中文|翻译|原文/]]) {
    await page.locator('#' + mode).click(); await waitEditor('python')
    const text = await hover('inspect_session'); report.observations[mode] = text
    assert.match(text, expected); assert.doesNotMatch(text, /模型翻译，|ENGLISH_ORIGINAL_TAIL_9b1e|Returns the current/)
    await geometry(mode); await shot(`hover-translation-${mode}.png`)
    await openFull(); await drawer().getByRole('button', { name: '原文', exact: true }).click(); assert.match(await drawer().innerText(), /ENGLISH_ORIGINAL_TAIL_9b1e/)
    await drawer().getByRole('button', { name: '关闭完整说明' }).click()
  }
  checks.push('Pending and failed translation show short honest status while complete original text remains available')
  assert.equal(await page.evaluate(() => window.__hoverPresentationFixture.saves.length), 0)
  assert.equal(await page.evaluate(() => window.__hoverPresentationFixture.changes.length), 0)
  assert.deepEqual(report.errors, []); assert.deepEqual(report.unexpected, [])
  checks.push('Reading hover/full documentation never edits or saves a file and opens no unexpected network request')
  report.passed = true
  }
} catch (error) {
  report.error = error.stack || String(error)
  if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
  process.exitCode = 1
} finally {
  await browser.close()
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: report.passed, checks: report.checks.length, error: report.error, report: path.join(output, 'report.json') }))
}
