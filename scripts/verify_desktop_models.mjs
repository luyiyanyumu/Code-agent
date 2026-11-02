// Real packaged desktop + FastAPI acceptance for model settings and the map.
// All project files, profiles, data and ports are isolated. No prompts are sent.
// node scripts/verify_desktop_models.mjs <staged-app-root> <evidence-directory>
import assert from 'node:assert/strict'
import {spawn} from 'node:child_process'
import {mkdir,readFile,writeFile} from 'node:fs/promises'
import {join,resolve} from 'node:path'
import net from 'node:net'

assert.ok(process.argv[2]&&process.argv[3],'Provide an app root and evidence directory')
const root=resolve(process.argv[2]),out=resolve(process.argv[3]),fixture=join(out,'model-project'),verifyJava=process.argv.includes('--java')
await mkdir(fixture,{recursive:true})
await writeFile(join(fixture,'README.md'),'# Model selection acceptance\n\nIsolated fixture.\n')
await writeFile(join(fixture,'demo.py'),'def multiply(value):\n    return value * 3\n')
if(verifyJava){
  await writeFile(join(fixture,'Main.java'),'public class Main {\n    public static void main(String[] args) {\n        String message = "hello";\n        System.out.println(message.substring(1));\n    }\n}\n')
  await writeFile(join(fixture,'.project'),'<?xml version="1.0"?><projectDescription><name>bundled_java_desktop</name><buildSpec><buildCommand><name>org.eclipse.jdt.core.javabuilder</name><arguments/></buildCommand></buildSpec><natures><nature>org.eclipse.jdt.core.javanature</nature></natures></projectDescription>')
  await writeFile(join(fixture,'.classpath'),'<?xml version="1.0"?><classpath><classpathentry kind="src" path=""/><classpathentry kind="con" path="org.eclipse.jdt.launching.JRE_CONTAINER"/><classpathentry kind="output" path="bin"/></classpath>')
}
assert.ok((await readFile(join(root,'web/dist/index.html'),'utf8')).includes('<html'),'Staged frontend must be built before this acceptance starts')
await readFile(join(root,'code_agent/api/model_selection_api.py'),'utf8')
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const {chromium}=pw.default||pw,sleep=ms=>new Promise(done=>setTimeout(done,ms))
async function port(){return new Promise((done,reject)=>{const s=net.createServer();s.once('error',reject);s.listen(0,'127.0.0.1',()=>{const value=s.address().port;s.close(()=>done(value))})})}
async function wait(fn,label,timeout=60000){const until=performance.now()+timeout;let last;while(performance.now()<until){try{const result=await fn();if(result)return result}catch(error){last=error}await sleep(150)}throw Error(`${label}: ${last?.message||'timed out'}`)}
async function bounded(promise,ms,label){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(`Timed out: ${label}`)),ms)})])}finally{clearTimeout(timer)}}
function gone(pid){try{process.kill(pid,0);return false}catch(error){if(error.code==='ESRCH')return true;throw error}}
async function listening(value){return new Promise(done=>{const s=net.connect({host:'127.0.0.1',port:value});let complete=false;const finish=result=>{if(complete)return;complete=true;s.destroy();done(result)};s.once('connect',()=>finish(true));s.once('error',()=>finish(false));s.setTimeout(500,()=>finish(true))})}
const backend=await port(),frontend=await port(),debug=await port(),diagnostics=join(out,'native.json')
const env=Object.fromEntries(Object.entries(process.env).filter(([name])=>!/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|JAVA_HOME|JDK_HOME|JDK_JAVA_OPTIONS|JAVA_TOOL_OPTIONS|_JAVA_OPTIONS|npm_)/i.test(name)))
Object.assign(env,{PATH:[join(process.env.SystemRoot,'System32'),process.env.SystemRoot,join(process.env.SystemRoot,'System32/WindowsPowerShell/v1.0')].join(';'),CODE_AGENT_HOME:join(out,'user-data'),CODE_AGENT_PROVIDER:'offline',CODE_AGENT_AUTH_ENABLED:'false',PYTHONDONTWRITEBYTECODE:'1',CODE_AGENT_LAUNCHER_BACKEND_PORT:String(backend),CODE_AGENT_LAUNCHER_FRONTEND_PORT:String(frontend)})
const report={passed:false,root,backend,frontend,checks:[],errors:[],sentPrompts:[]}
let app,exited,browser,page,observedSession=''
const readState=async()=>JSON.parse((await readFile(diagnostics,'utf8')).replace(/^\uFEFF/,''))
async function api(method,path,body){
  const response=await fetch(`http://127.0.0.1:${backend}${path}`,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})})
  const result=await response.json()
  assert.equal(response.status,200,`${method} ${path}: ${JSON.stringify(result)}`)
  return result
}
async function screenshot(name){await page.screenshot({path:join(out,name)})}
async function desktopRpc(method,params={}){return page.evaluate(({method,params})=>new Promise((done,reject)=>{const id=`model-acceptance-${Date.now()}-${Math.random()}`,timer=setTimeout(()=>reject(Error('Desktop RPC timed out')),30000);const receive=event=>{if(event.data?.id!==id)return;clearTimeout(timer);window.chrome.webview.removeEventListener('message',receive);event.data.ok?done(event.data.result):reject(Error(event.data.error))};window.chrome.webview.addEventListener('message',receive);window.chrome.webview.postMessage({channel:'code-agent-desktop',id,method,params})}),{method,params})}
async function modelPopup(models=false){
  await page.getByRole('button',{name:/^选择会话模型：/}).click()
  const dialog=page.getByRole('dialog',{name:'会话模型选择',exact:true});await dialog.waitFor()
  if(models)await dialog.locator('.model-root-row[data-pane="models"]').click()
  return dialog
}
async function mapVisible(label){
  await page.locator('.map .canvas').waitFor()
  const geometry=await wait(()=>page.evaluate(()=>{
    const map=document.querySelector('.map'),canvas=map?.querySelector('.canvas'),status=map?.querySelector('.description-progress'),meta=map?.querySelector('.meta'),actions=map?.querySelector('.map-actions')
    if(!canvas||!status||!meta)return null
    const c=canvas.getBoundingClientRect(),s=status.getBoundingClientRect(),m=meta.getBoundingClientRect(),a=actions?.getBoundingClientRect()
    const visible=[...map.querySelectorAll('svg .node')].filter(node=>{const n=node.getBoundingClientRect();return n.width>0&&n.height>0&&n.right>c.left&&n.left<c.right&&n.bottom>c.top&&n.top<c.bottom})
    return c.height>200&&visible.length?{canvas:{x:c.x,y:c.y,width:c.width,height:c.height},statusHeight:s.height,statusTop:s.top,metaTop:m.top,actionsBottom:a?.bottom,visibleNodes:visible.length}:null
  }),label,30000)
  assert.ok(geometry.statusHeight<=36,`${label}: status should remain one compact row`)
  assert.ok(geometry.metaTop>=geometry.canvas.y+geometry.canvas.height-2,`${label}: index metadata belongs below the canvas`)
  if(geometry.actionsBottom)assert.ok(geometry.actionsBottom<=geometry.statusTop+2,`${label}: toolbar must not be covered by the description status`)
  return geometry
}

try{
  app=spawn(join(root,'Code-agent.exe'),['--diagnostics-file',diagnostics,'--diagnostics-port',String(debug),'--diagnostics-exit-after-ms','120000'],{cwd:out,env,windowsHide:true,stdio:'ignore'})
  exited=new Promise((done,reject)=>{app.once('exit',(code,signal)=>done({code,signal,at:performance.now()}));app.once('error',reject)})
  const ready=await wait(async()=>{if(app.exitCode!==null)return {state:'process-exited',exitCode:app.exitCode};const state=await readState();if(state.state==='failed')return state;return state.state==='ready'?state:null},'native ready',90000)
  assert.equal(ready.state,'ready',`Native startup failed: ${ready.error||ready.exitCode||ready.state}`)
  assert.equal(ready.integratedWorkbench,true);assert.equal(ready.windowEnabled,true)
  report.nativePid=app.pid;report.controllerPid=ready.controllerPid
  browser=await chromium.connectOverCDP(`http://127.0.0.1:${debug}`)
  page=await wait(()=>browser.contexts().flatMap(context=>context.pages()).find(candidate=>candidate.url().startsWith(`http://127.0.0.1:${frontend}/`)),'embedded workbench')
  page.setDefaultTimeout(20000)
  page.on('pageerror',error=>report.errors.push(error.message))
  page.on('request',request=>{const path=new URL(request.url()).pathname,match=path.match(/^\/api\/v1\/sessions\/([^/]+)\/models$/);if(match)observedSession=match[1];if(request.method()==='POST'&&path.endsWith('/prompt'))report.sentPrompts.push({transport:'http',path})})
  page.on('websocket',socket=>socket.on('framesent',frame=>{try{const data=JSON.parse(String(frame.payload));if(data.type==='prompt')report.sentPrompts.push({transport:'websocket'})}catch{}}))
  await page.getByRole('menubar',{name:'IDE 菜单'}).waitFor()
  const cfg=await api('GET','/api/v1/settings/model');assert.equal(cfg.provider,'offline')
  const offline=await api('POST','/api/v1/model-profiles',{name:'离线模型验证',provider:'offline',model_default:'offline-one',models:[{id:'offline-one',name:'离线一'},{id:'offline-two',name:'离线二'}]})
  const credential=await api('POST','/api/v1/model-profiles',{name:'密钥界面验证',provider:'deepseek',base_url:'https://fixture.invalid/v1',api_key:'fixture-only-not-a-real-key',model_default:'fixture-a',models:[{id:'fixture-a',name:'Fixture A'},{id:'fixture-b',name:'Fixture B'},{id:'fixture-c',name:'Fixture C'},{id:'deepseek-flash',name:'DeepSeek Flash'}]})
  const pathInput=page.getByRole('textbox',{name:'工作区路径',exact:true})
  if(!await pathInput.isVisible())await page.getByRole('button',{name:'输入工作区路径',exact:true}).click()
  await pathInput.fill(fixture)
  await page.getByRole('button',{name:'打开工作区',exact:true}).click()
  // A freshly installed runtime can still be warming its editor/index files.
  // Wait for the project session itself rather than assuming a warm 20s disk cache.
  await page.getByRole('button',{name:/^选择会话模型：/}).waitFor({timeout:60000})
  await wait(()=>observedSession,'project session catalog request')
  const project=await wait(async()=>{const rows=await api('GET','/api/v1/code/projects');return rows.find(row=>resolve(row.root_path).toLowerCase()===fixture.toLowerCase())},'registered fixture project')
  const sessionId=observedSession;report.projectId=project.id;report.sessionId=sessionId
  const sessionMeta=await api('GET',`/api/v1/sessions/${sessionId}`)
  assert.equal(sessionMeta.project_id,project.id)
  let popup=await modelPopup(true)
  await popup.getByRole('searchbox',{name:'搜索模型',exact:true}).fill('离线二')
  assert.equal(await popup.getByRole('option').count(),1)
  await screenshot('native-model-picker.png')
  await popup.getByRole('option',{name:'离线二',exact:true}).click()
  await popup.waitFor({state:'detached'})
  const selected=await api('GET',`/api/v1/sessions/${sessionId}/models`)
  assert.deepEqual(selected.current,{provider:offline.id,model:'offline-two'})
  assert.deepEqual(await api('GET','/api/v1/settings/model'),cfg)
  assert.deepEqual(JSON.parse((await api('GET',`/api/v1/sessions/${sessionId}`)).model_selection),selected.current)
  report.checks.push('actual native project composer selects a grouped model through the real session API; global configuration remains unchanged')
  await page.reload();await page.getByRole('button',{name:/^选择会话模型：离线二/}).waitFor()
  assert.deepEqual((await api('GET',`/api/v1/sessions/${sessionId}/models`)).current,selected.current)
  report.checks.push('saved session selection survives embedded-page reload and is persisted in session metadata')
  const beforeEvents=await api('GET',`/api/v1/sessions/${sessionId}/events`)
  const composer=page.getByRole('textbox',{name:'向代码助手发送消息',exact:true})
  await composer.fill('/model');await composer.press('Enter')
  popup=page.getByRole('dialog',{name:'会话模型选择',exact:true});await popup.waitFor()
  await popup.getByRole('listbox',{name:'可用模型',exact:true}).waitFor()
  assert.ok(await popup.getByRole('option',{name:'离线二',exact:true}).count())
  await popup.getByRole('button',{name:'关闭模型选择',exact:true}).click()
  assert.equal((await api('GET',`/api/v1/sessions/${sessionId}/events`)).length,beforeEvents.length)
  assert.equal(report.sentPrompts.length,0)
  report.checks.push('/model opens the same grouped catalog locally without submitting prompts or tools')
  popup=await modelPopup(true)
  await popup.getByRole('searchbox',{name:'搜索模型',exact:true}).fill('DeepSeek Flash')
  await popup.getByRole('option',{name:'DeepSeek Flash',exact:true}).click()
  await page.getByRole('button',{name:/^选择会话模型：DeepSeek Flash，推理强度：高/}).waitFor()
  popup=await modelPopup()
  await popup.getByRole('button',{name:/^推理强度/}).click()
  const effortList=popup.getByRole('listbox',{name:'推理强度',exact:true})
  assert.equal(await effortList.getByRole('option').count(),4)
  await screenshot('native-reasoning-strengths.png')
  await effortList.getByRole('option',{name:/^低(?:\s|$)/}).click()
  const reasoningSelected=await api('GET',`/api/v1/sessions/${sessionId}/models`)
  assert.deepEqual(reasoningSelected.current,{provider:credential.id,model:'deepseek-flash',reasoningEffort:'low'})
  assert.deepEqual(await api('GET','/api/v1/settings/model'),cfg)
  await page.reload();await page.getByRole('button',{name:/^选择会话模型：DeepSeek Flash，推理强度：低/}).waitFor()
  assert.deepEqual(JSON.parse((await api('GET',`/api/v1/sessions/${sessionId}`)).model_selection),reasoningSelected.current)
  popup=await modelPopup();await popup.getByRole('button',{name:/^推理强度/}).click()
  await popup.getByRole('option',{name:/^关闭(?:\s|$)/}).click()
  assert.equal((await api('GET',`/api/v1/sessions/${sessionId}/models`)).current.reasoningEffort,'off')
  report.checks.push('native reasoning menu exposes exact DeepSeek levels, persists low/off in the real session database across reload, and leaves global settings unchanged')
  popup=await modelPopup(true)
  await popup.getByRole('searchbox',{name:'搜索模型',exact:true}).fill('离线二')
  await popup.getByRole('option',{name:'离线二',exact:true}).click()
  popup=await modelPopup();assert.equal(await popup.getByText('推理强度',{exact:true}).count(),0)
  await page.keyboard.press('Escape')
  assert.deepEqual((await api('GET',`/api/v1/sessions/${sessionId}/models`)).current,selected.current)
  report.checks.push('switching to a model without reasoning capability removes the control and the saved effort')
  popup=await modelPopup()
  await popup.getByRole('button',{name:/管理模型提供方/}).click()
  const row=page.locator(`.provider-row[data-provider-id="${credential.id}"]`)
  await row.waitFor()
  assert.equal(await page.getByRole('navigation',{name:'设置分类'}).getByRole('button').count(),8)
  assert.equal(await row.locator('.provider-row-trigger').getAttribute('aria-expanded'),'false')
  await row.locator('.provider-row-trigger').click()
  const keyInput=row.locator('#provider-api-key'),advanced=row.locator('details.custom-provider-settings')
  await keyInput.waitFor();assert.equal(await keyInput.inputValue(),'')
  assert.equal(await advanced.evaluate(node=>node.open),false)
  const keyBox=await keyInput.boundingBox(),advancedBox=await advanced.boundingBox();assert.ok(keyBox.y<advancedBox.y)
  await screenshot('native-provider-key-first.png')
  await advanced.locator('summary').click()
  assert.equal(await row.locator('.editable-model-row').count(),4)
  await screenshot('native-provider-models.png')
  report.checks.push('provider settings use folded rows, a blank write-only credential field first, and multi-model editing under custom settings')
  await page.getByRole('button',{name:'返回代码工作区',exact:true}).click()
  await page.getByRole('button',{name:'思维导图',exact:true}).click()
  report.mapInitial=await mapVisible('initial real map geometry')
  const brief=await wait(async()=>{const state=await api('GET',`/api/v1/code/projects/${project.id}/feature-briefs/fill`);return state.running?null:state},'offline description preflight settles')
  assert.equal(brief.failed||0,0);assert.notEqual(brief.state,'error')
  await screenshot('native-map-status.png')
  await page.getByRole('button',{name:'编辑器',exact:true}).click()
  await page.getByRole('button',{name:'思维导图',exact:true}).click()
  report.mapRestored=await mapVisible('retained map reappears without an empty canvas')
  report.checks.push('real project map keeps visible nodes after mode switching; compact status is at most 36px and index metadata stays below the canvas')
  if(verifyJava){
    await page.getByRole('button',{name:'编辑器',exact:true}).click()
    await page.getByRole('button',{name:'文件 Main.java',exact:true}).click()
    await page.getByRole('button',{name:'代码分析就绪',exact:true}).waitFor({timeout:90000})
    const lsp=await api('GET',`/api/v1/code/projects/${project.id}/lsp?refresh=1`)
    const java=lsp.servers.find(server=>server.id==='jdtls')
    assert.equal(java.installed,true)
    assert.ok(resolve(java.resolved_command).toLowerCase().startsWith(join(root,'runtimes/jdtls').toLowerCase()))
    const javaArg=java.args.indexOf('--java-executable')
    assert.ok(javaArg>=0)
    assert.equal(resolve(java.args[javaArg+1]).toLowerCase(),join(root,'runtimes/java/bin/java.exe').toLowerCase())
    const runningJava=lsp.sessions.find(session=>session.server==='jdtls'&&session.alive)
    assert.ok(runningJava)
    const javaConfiguration=runningJava.environment?.configuration_data
    assert.equal(javaConfiguration?.isolated,true)
    assert.ok(resolve(javaConfiguration.path).toLowerCase().startsWith(join(out,'user-data').toLowerCase()))
    await page.getByRole('button',{name:'代码分析就绪',exact:true}).click()
    const lspPanel=page.getByRole('dialog',{name:'代码分析设置',exact:true})
    await lspPanel.waitFor()
    await screenshot('native-java-analysis-ready.png')
    assert.ok((await lspPanel.innerText()).includes('Eclipse JDT'))
    const overflow=await lspPanel.evaluate(node=>node.scrollWidth>node.clientWidth+1)
    assert.equal(overflow,false)
    await lspPanel.getByRole('button',{name:'关闭代码分析设置',exact:true}).click()
    report.java={installed:true,server:'jdtls',source:'bundled',javaVersion:'21.0.12.1+1'}
    report.checks.push('actual desktop opens Java with no Java/JDT on PATH, connects the bundled JDT/JDK, and renders readable analysis status')
  }
  const status=await desktopRpc('status');report.services=status.services.map(({id,pid,state})=>({id,pid,state}))
  assert.ok(report.services.every(service=>service.state==='healthy'))
  assert.equal(report.sentPrompts.length,0);assert.deepEqual(report.errors,[])
  const started=performance.now()
  await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'models-acceptance-exit',method:'exit',params:{}}))
  const ended=await bounded(exited,90000,'normal native close');report.elapsedCloseMilliseconds=Math.round(ended.at-started)
  assert.equal(ended.code,0)
  const verificationStarted=performance.now()
  await wait(()=>gone(app.pid)&&gone(report.controllerPid)&&report.services.every(service=>gone(service.pid)),'all owned processes exit',15000)
  await wait(async()=>!await listening(backend)&&!await listening(frontend),'owned service ports close',15000)
  assert.equal((await readState()).state,'closed')
  report.verificationMilliseconds=Math.round(performance.now()-verificationStarted)
  report.checks.push('normal app close stops only its owned controller/backend/frontend, with close and verifier timings recorded separately')
  report.passed=true
}catch(error){
  report.error=error.stack||String(error)
  if(page)await screenshot('failure.png').catch(()=>{})
  if(page&&app&&app.exitCode===null)await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'models-acceptance-cleanup',method:'exit',params:{}})).catch(()=>{})
  if(app&&app.exitCode===null&&exited)await bounded(exited,45000,'normal failure cleanup').catch(()=>{})
  process.exitCode=1
}finally{
  await writeFile(join(out,'report.json'),JSON.stringify(report,null,2))
  if(browser)await browser.close().catch(()=>{})
  console.log(JSON.stringify(report))
}
