// Real CodeView + EditorPane, isolated HTTP/session streams and disposable
// browser storage. Nothing is read from or written to a user project.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import { createServer } from 'vite'

const root=path.dirname(fileURLToPath(import.meta.url)),port=5206,base=`http://127.0.0.1:${port}`
const output=path.resolve(process.argv[2]||path.join(root,'../.tmp/file-path-state-20261101/browser'))
await fs.mkdir(output,{recursive:true})
const hash=text=>createHash('sha1').update(text).digest('hex'),key=(pid,p)=>JSON.stringify([pid,'main',p])
const report={passed:false,checks:[],requests:[],errors:[],unexpected:[],screenshots:[],observations:{},fixtureSaveAttempts:0,successfulWrites:0,userDataWrites:0,owned:{pid:process.pid,port,browsersClosed:false,serverStopped:false},scope:'Actual CodeView/EditorPane, in-memory API and session stream fixtures, fresh isolated browser localStorage. No actual user files, credentials, models or windows.'}
const projects=[{id:'file-path-p1',name:'Path fixture one',root_path:'E:/file-path-fixture/one',available:true,allow_direct_write:false,git:{is_repo:false}},{id:'file-path-p2',name:'Path fixture two',root_path:'E:/file-path-fixture/two',available:true,allow_direct_write:false,git:{is_repo:false}}]
const A='SkyApplications.java',B='SkyApplication.java',flaky='Flaky.java',draft='Draft.java',late='Delayed.java',locked='ReadOnly.java'
const texts={ [A]:'public class SkyApplications {}\n',[B]:'public class SkyApplication {}\n',[flaky]:'public class Flaky {}\n',[draft]:'public class Draft { int disk = 1; }\n',[late]:'public class Delayed {}\n',[locked]:'public class ReadOnly {}\n' }
let aExists=false,draftExists=false,flakyStatus=200,saveGate=null,readGate=null,html='',vite,browser,context,page,failure
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return{promise,resolve}}
const data=(pid,name)=>{const text=pid==='file-path-p2'&&name===B?'package projectTwo;\npublic class SkyApplication {}\n':texts[name];return{path:name,name,text,sha1:hash(text),uri:`file:///${projects.find(p=>p.id===pid).root_path}/${name}`,lang:'java',editable:name!==locked,secret:false,start_line:1,total_lines:text.split('\n').length,end_line:text.split('\n').length,size:text.length}}
const inventory=()=>Object.keys(texts).map(name=>({path:name,name,lang:'java',size:texts[name].length}))
const session=pid=>({id:'session-'+pid,code_project_id:pid,permission_mode:'read-only',model:'offline-fixture',busy:false,status:'open',web_enabled:false})
try{
  vite=await createServer({root,server:{host:'127.0.0.1',port,strictPort:true},plugins:[{name:'file-path-state-fixture',configureServer(server){server.middlewares.use((request,response,next)=>{if(request.url?.split('?')[0]!=='/__file-path-state')return next();response.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'});response.end(html)})}}],logLevel:'warn'})
  await vite.listen()
  const vue=(await(await fetch(`${base}/src/views/CodeView.vue`)).text()).match(/from ["']([^"']*\/vue\.js[^"']*)["']/)?.[1];assert.ok(vue)
  html=`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body,#app{height:100%;margin:0;overflow:hidden}#app{display:flex}#app>.code{flex:1;min-width:0}</style></head><body><div id="app"></div><script type="module">import{createApp,h}from'${vue}';import CodeView from'/src/views/CodeView.vue';import'/src/style.css';createApp({render:()=>h(CodeView,{user:{id:'file-path-fixture',username:'offline-fixture'},visible:true})}).mount('#app');setTimeout(async()=>{const runtime=await import('/src/lib/monacoRuntime.js');window.__filePathFixture={runtime,get editor(){return runtime.editor.getEditors().find(editor=>editor.getModel()&&!editor.getDomNode()?.closest('.monaco-diff-editor'))}}},0)</script></body></html>`
  const loaded=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  browser=await(loaded.default||loaded).chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
  const fresh=async(snapshot=null)=>{
    await context?.close();context=await browser.newContext({viewport:{width:1440,height:960},serviceWorkers:'block'})
    await context.addInitScript(({snapshot})=>{
      localStorage.setItem('code-agent-active-project-v2:file-path-fixture','file-path-p1');localStorage.setItem('code-agent-ide-layout-v1',JSON.stringify({left:true,right:false,bottom:false}));localStorage.setItem('code-agent-code-left','260');localStorage.setItem('code-agent-code-right','340')
      if(snapshot)localStorage.setItem('code-agent-workbench-v1:file-path-fixture',JSON.stringify(snapshot))
      const NativeSocket=window.WebSocket
      window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSED=3;constructor(url,...args){super();const parsed=new URL(url,location.href);if(!parsed.pathname.startsWith('/api/'))return new NativeSocket(url,...args);this.url=String(url);this.readyState=0;queueMicrotask(()=>{this.readyState=1;this.onopen?.({});this.onmessage?.({data:JSON.stringify({type:'session_meta',payload:{busy:false,phase:'',model:'offline-fixture',pending_questions:[],web_enabled:false}})})})}send(raw){if(JSON.parse(raw).type!=='ping')throw new Error('Fixture forbids execution/model messages')}close(){this.readyState=3}}
    },{snapshot})
    await context.route('**/*',async route=>{
      const request=route.request(),url=new URL(request.url()),p=url.pathname,method=request.method()
      if(url.origin!==base){report.unexpected.push(`${method} ${url.href}`);return route.abort()}
      if(!p.startsWith('/api/'))return route.continue()
      const body=request.postDataJSON();report.requests.push({method,path:p,query:url.search,body})
      const json=(value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)})
      if(/\/prompt$|\/runs(?:\/|$)|\/terminals(?:\/|$)|\/debug\/|\/scan$|\/reindex$|\/feature-briefs\/fill$/.test(p)){report.unexpected.push(`${method} ${p}`);return json({detail:'Fixture forbids execution and model requests'},403)}
      if(p==='/api/v1/code/meta')return json({templates:['empty'],browse_enabled:false,features:{},git_available:false})
      if(p==='/api/v1/code/projects')return json(projects)
      if(p==='/api/v1/web')return json({available:false,reason:'离线文件状态验收'})
      if(p==='/api/v1/settings/model')return json({provider:'offline',active_model:'offline-fixture',model_default:'offline-fixture'})
      if(p==='/api/v1/model-profiles')return json([])
      const sm=p.match(/^\/api\/v1\/sessions\/session-(file-path-p[12])(?:\/(.*))?$/)
      if(sm&&method==='GET'){if(!sm[2])return json(session(sm[1]));if(sm[2]==='events')return json([]);if(sm[2]==='checkpoints')return json({items:[],busy:false});if(sm[2]==='approvals/pending')return json({});if(sm[2]==='questions/pending')return json({questions:[]});if(sm[2]==='models')return json({available:false,groups:[],current:{provider:'offline',model:'offline-fixture'}})}
      const pm=p.match(/^\/api\/v1\/code\/projects\/(file-path-p[12])(?:\/(.*))?$/)
      if(pm){const pid=pm[1],suffix=pm[2]||'',project=projects.find(p=>p.id===pid)
        if(!suffix&&method==='GET')return json(project)
        if(suffix==='session'&&method==='POST')return json(session(pid))
        if(suffix==='file'&&method==='GET'){const name=url.searchParams.get('path');if(name===late&&pid===projects[0].id&&readGate)await readGate.promise;if(name===A&&!aExists||name===draft&&!draftExists)return json({detail:'文件不存在：'+name},404);if(name===flaky&&flakyStatus!==200)return json({detail:'临时读取失败：'+name},flakyStatus);if(!texts[name])return json({detail:'文件不存在'},404);return json(data(pid,name))}
        if(suffix==='file'&&method==='PUT'){report.fixtureSaveAttempts++;if(saveGate)await saveGate.promise;return json({detail:'保存 SkyApplications.java 失败：离线模拟500'},500)}
        if(method==='GET'){
          if(suffix==='tree')return json({dirs:[],files:inventory(),truncated:false})
          if(suffix==='files')return json({files:inventory(),truncated:false,inventory_truncated:false})
          if(suffix==='index')return json({version:1,change_seq:1,indexed:true,scanning:false,files:6,symbols:0,edges:0,briefs:{running:false}})
          if(suffix==='diagnostics')return json({seq:1,counts:{error:0,warning:0},files:{},symbols:{},items:[]})
          if(suffix==='lsp')return json({servers:[]})
          if(suffix==='feature-briefs')return json({missing:{total:0},items:[]})
          if(suffix==='conversations')return json({default_session_id:session(pid).id,conversations:[{...session(pid),kind:'main',title:'Lead'}]})
          if(suffix==='agents')return json({lead:{session_id:session(pid).id,project_id:pid,role:'lead',depth:0,title:'Lead',state:'idle'},agents:[],limits:{max_depth:3,max_parallel:4},running:0,queued:0})
          if(suffix==='patches')return json({patches:[],counts:{total:0,active:0}})
          if(suffix==='change-proposals')return json({proposals:[]})
          if(suffix==='git')return json({is_repo:false,changes:[]})
        }
      }
      report.unexpected.push(`${method} ${p}`);return json({detail:'Unexpected fixture API'},501)
    })
    page=await context.newPage();page.setDefaultTimeout(15000);page.on('pageerror',error=>report.errors.push(error.message));page.on('dialog',dialog=>dialog.type()==='beforeunload'?dialog.accept():dialog.dismiss())
    await page.goto(`${base}/__file-path-state`,{waitUntil:'domcontentloaded'});await page.locator('.tree .row').filter({hasText:B}).waitFor({timeout:60000})
  }
  const open=async name=>{await page.locator('.tree .row').filter({hasText:name}).click()}
  const waitEditor=async name=>{await page.waitForFunction(name=>window.__filePathFixture?.editor?.getModel()?.uri.toString().endsWith('/'+name),name,{timeout:60000});await page.waitForTimeout(50)}
  const state=()=>page.evaluate(()=>{const s=window.__filePathFixture,e=s?.editor;return{uri:e?.getModel()?.uri.toString(),text:e?.getValue(),readonly:e?.getOption(s.runtime.editor.EditorOption.readOnly),activeTab:document.querySelector('.editor-tab.active')?.textContent,errors:[...document.querySelectorAll('.center > .banner,.file-error')].map(el=>el.textContent),draftWarning:[...document.querySelectorAll('.draft-warning')].map(el=>el.textContent)}})
  const edit=async suffix=>page.evaluate(suffix=>{const e=window.__filePathFixture.editor,m=e.getModel(),position=m.getPositionAt(m.getValueLength());e.executeEdits('fixture-only',[{range:{startLineNumber:position.lineNumber,endLineNumber:position.lineNumber,startColumn:position.column,endColumn:position.column},text:suffix}])},suffix)
  const tab=async name=>{await page.locator('.editor-tab').filter({has:page.locator('.tab-label').filter({hasText:new RegExp('^'+name.replace('.','\\.')+'$')})}).locator('.tab-select').click()}
  const retry=()=>page.getByRole('button',{name:/重新读取/}).filter({visible:true}).first().click()
  const save=()=>page.locator('.pane .bar').getByRole('button',{name:/^保存(?:中…)?$/}).click()
  const fileReadCount=name=>report.requests.filter(r=>r.method==='GET'&&r.path.endsWith('/file')&&new URLSearchParams(r.query).get('path')===name).length
  const waitNode=async(predicate,label)=>{const until=Date.now()+5000;while(!predicate()&&Date.now()<until)await new Promise(resolve=>setTimeout(resolve,10));assert.ok(predicate(),label)}
  const snapshot=(name,text,sha1)=>({version:1,scopes:[{projectId:projects[0].id,tree:'main',activeKey:key(projects[0].id,name),documents:[{path:name,sha1,dirty:true,text,file:{path:name,lang:'java',uri:data(projects[0].id,name).uri,editable:true,secret:false}}]}]})
  const shot=async name=>{const filename=path.join(output,name);await page.screenshot({path:filename,fullPage:true,animations:'disabled',timeout:30000});report.screenshots.push(filename)}

  console.log('[fixture] failed read A → healthy B')
  await fresh();await open(B);await waitEditor(B);await open(A);await page.waitForFunction(()=>document.querySelector('.center > .banner')?.textContent.includes('SkyApplications.java'));await tab(B);await waitEditor(B);let current=await state();assert.equal(current.readonly,false);assert.ok(current.errors.every(text=>!text.includes(A)));report.observations.readErrorSwitch=current
  report.checks.push('A missing plural filename read error is not shown on the existing singular filename tab; B remains editable')

  console.log('[fixture] delayed save A → tab B')
  aExists=true;await open(A);await waitEditor(A);await edit('// unsaved fixture A\n');saveGate=deferred();await save();await waitNode(()=>report.fixtureSaveAttempts===1,'A save must be in flight');await open(B);await waitEditor(B);saveGate.resolve();saveGate=null;await page.waitForTimeout(120);current=await state();assert.ok(current.errors.every(text=>!text.includes(A)));assert.equal(current.readonly,false);await tab(A);await waitEditor(A);await page.waitForFunction(()=>document.querySelector('.center')?.textContent.includes('保存 SkyApplications.java 失败'));assert.match((await state()).text,/unsaved fixture A/);await tab(B);await waitEditor(B);assert.ok((await state()).errors.every(text=>!text.includes(A)));await shot('healthy-file-no-stale-path.png');report.checks.push('A late failed save remains associated with A, preserves its draft, and never pollutes active B')

  console.log('[fixture] transient restored 500 → retry')
  flakyStatus=500;const flakyDraft=texts[flaky]+'// restored transient draft\n';await fresh(snapshot(flaky,flakyDraft,hash(texts[flaky])));await waitEditor(flaky);await page.getByRole('button',{name:/重新读取/}).filter({visible:true}).first().waitFor();current=await state();assert.match(current.text,/restored transient draft/);assert.doesNotMatch(current.errors.join('\n'),/文件不存在|已被删除/);const transientReads=fileReadCount(flaky);flakyStatus=200;await retry();await page.waitForFunction(()=>window.__filePathFixture.editor.getOption(window.__filePathFixture.runtime.editor.EditorOption.readOnly)===false);assert.ok(fileReadCount(flaky)>transientReads);assert.equal((await state()).text,flakyDraft);report.observations.transientRecovered=await state();report.checks.push('A transient restore HTTP500 can be retried successfully and does not discard its recovered dirty text')

  console.log('[fixture] restored missing draft reappears → reread')
  draftExists=false;const dirtyDraft='public class Draft { int unsaved = 42; }\n';await fresh(snapshot(draft,dirtyDraft,'old-draft-baseline'));await waitEditor(draft);await page.getByRole('button',{name:/重新读取/}).filter({visible:true}).first().waitFor();const missingReads=fileReadCount(draft);draftExists=true;await retry();await page.waitForFunction(()=>window.__filePathFixture.editor.getOption(window.__filePathFixture.runtime.editor.EditorOption.readOnly)===false);current=await state();assert.equal(current.text,dirtyDraft);assert.ok(fileReadCount(draft)>missingReads);assert.ok(current.draftWarning.some(text=>/磁盘内容已变化|草稿/.test(text)));assert.ok(current.errors.every(text=>!/不存在/.test(text)));report.observations.missingReappeared=current;await shot('missing-reappeared-preserves-draft.png');report.checks.push('A restored missing file is reread when it reappears, clears missing/read-only state and preserves its dirty draft with a disk-conflict warning')

  console.log('[fixture] project switch rejects late file response')
  await fresh();readGate=deferred();await open(late);await waitNode(()=>fileReadCount(late)>0,'p1 read must be pending');await page.getByRole('button',{name:'切换项目',exact:true}).click();await page.getByRole('button',{name:'切换到 Path fixture two',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.project-switcher-trigger')?.getAttribute('data-project-id')==='file-path-p2');await open(B);await waitEditor(B);readGate.resolve();readGate=null;await page.waitForTimeout(100);current=await state();assert.match(current.text,/projectTwo/);assert.match(current.uri,/\/two\//);assert.equal(current.readonly,false);report.observations.crossProject=current;report.checks.push('An old project file response cannot replace a newly selected project file or its editor state')

  console.log('[fixture] true disk readonly respected')
  await open(locked);await waitEditor(locked);current=await state();assert.equal(current.readonly,true);const lockedText=current.text;await page.evaluate(()=>{const e=window.__filePathFixture.editor;e.focus();e.trigger('fixture','type',{text:'SHOULD_NOT_WRITE'})});assert.equal((await state()).text,lockedText);assert.equal(report.successfulWrites,0);report.checks.push('A genuinely noneditable file remains read-only, rejects typing, and receives no successful write')
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpected,[]);report.passed=true
}catch(error){failure=error;report.failure=error.stack||String(error);console.log('UI fixture failure:',error.message);if(page){try{report.observations.failureDom=await page.locator('body').innerText();report.observations.failureState=await page.evaluate(()=>{const s=window.__filePathFixture,e=s?.editor;return{uri:e?.getModel()?.uri.toString(),text:e?.getValue()}})}catch{}}}
finally{saveGate?.resolve();readGate?.resolve();await browser?.close();report.owned.browsersClosed=true;await vite?.close();report.owned.serverStopped=true;await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,checks:report.checks.length,report:path.join(output,'report.json'),failure:report.failure?.split('\n')[0]},null,2))}
if(failure)throw failure
