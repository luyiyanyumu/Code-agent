// Real packaged desktop + FastAPI + preview proxy + Windows folder helper.
// Uses isolated files, ports and data; never touches the user's installation.
import assert from 'node:assert/strict'
import {spawn,execFile} from 'node:child_process'
import {promisify} from 'node:util'
import {mkdir,readFile,writeFile} from 'node:fs/promises'
import {join,resolve} from 'node:path'
import net from 'node:net'
import http from 'node:http'
const root=resolve(process.argv[2]),out=resolve(process.argv[3]);await mkdir(out,{recursive:true})
const fixture=join(out,'project');await mkdir(fixture,{recursive:true});await writeFile(join(fixture,'README.md'),'# Isolated close regression\n')
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const {chromium}=pw.default||pw,run=promisify(execFile),delay=ms=>new Promise(done=>setTimeout(done,ms))
async function port(){return new Promise(done=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const value=s.address().port;s.close(()=>done(value))})})}
async function wait(fn,label,timeout=90000){const end=Date.now()+timeout;while(Date.now()<end){const value=await fn();if(value)return value;await delay(200)}throw Error(`Timed out: ${label}`)}
const backend=await port(),frontend=await port(),debug=await port(),diagnostics=join(out,'native.json')
const readState=async()=>JSON.parse((await readFile(diagnostics,'utf8')).replace(/^\uFEFF/,''))
const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|npm_)/i.test(key)))
Object.assign(env,{PATH:[join(process.env.SystemRoot,'System32'),process.env.SystemRoot,join(process.env.SystemRoot,'System32/WindowsPowerShell/v1.0')].join(';'),CODE_AGENT_HOME:join(out,'user-data'),CODE_AGENT_PROVIDER:'offline',CODE_AGENT_AUTH_ENABLED:'false',CODE_AGENT_LAUNCHER_BACKEND_PORT:String(backend),CODE_AGENT_LAUNCHER_FRONTEND_PORT:String(frontend)})
async function helpers(parentPid){
  assert.ok(Number.isSafeInteger(parentPid)&&parentPid>0)
  const python=join(root,'runtimes/python/python.exe').replaceAll("'","''")
  const command=`$items = @(Get-CimInstance Win32_Process -Filter 'ParentProcessId=${parentPid}' | Where-Object { $_.ExecutablePath -eq '${python}' -and ($_.CommandLine -like '*native_dialog.py*' -or $_.CommandLine -like '*code_agent.codeintel.native_dialog*') } | Select-Object ProcessId,ParentProcessId,@{Name='WindowHandle';Expression={(Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue).MainWindowHandle.ToInt64()}}); ConvertTo-Json -InputObject $items -Compress`
  const {stdout}=await run('powershell.exe',['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(command,'utf16le').toString('base64')],{windowsHide:true,timeout:15000})
  return JSON.parse(stdout.trim()||'[]')
}
const gone=pid=>{try{process.kill(pid,0);return false}catch(error){return error.code==='ESRCH'}}
const report={passed:false,root,backend,frontend,checks:[]};let app,browser,page,exited
try{
  app=spawn(join(root,'Code-agent.exe'),['--diagnostics-file',diagnostics,'--diagnostics-port',String(debug),'--diagnostics-exit-after-ms','120000'],{cwd:out,env,windowsHide:true,stdio:'ignore'})
  exited=new Promise((resolve,reject)=>{app.once('exit',(code,signal)=>resolve({code,signal,at:performance.now()}));app.once('error',reject)})
  const ready=await wait(async()=>{try{const value=await readState();if(value.state==='failed')throw Error(value.error);return value.state==='ready'?value:null}catch(error){if(error.code==='ENOENT'||error instanceof SyntaxError)return null;throw error}},'native readiness')
  assert.equal(ready.integratedWorkbench,true);assert.equal(ready.windowEnabled,true)
  browser=await chromium.connectOverCDP(`http://127.0.0.1:${debug}`)
  page=await wait(()=>browser.contexts().flatMap(c=>c.pages()).find(p=>p.url().startsWith(`http://127.0.0.1:${frontend}/`)),'embedded page')
  report.pickerRequests=[];report.pickerResponses=[];report.helperSnapshots=[]
  page.on('request',request=>{if(new URL(request.url()).pathname.endsWith('/fs/pick-folder'))report.pickerRequests.push({method:request.method()})})
  page.on('response',async response=>{if(new URL(response.url()).pathname.endsWith('/fs/pick-folder')){
    try{report.pickerResponses.push({status:response.status(),body:await response.text()})}catch{}
  }})
  await page.getByRole('menubar',{name:'IDE 菜单'}).waitFor()
  const status=await page.evaluate(()=>new Promise((resolve,reject)=>{const id='close-regression-status';const receive=event=>{if(event.data?.id!==id)return;window.chrome.webview.removeEventListener('message',receive);event.data.ok?resolve(event.data.result):reject(Error(event.data.error))};window.chrome.webview.addEventListener('message',receive);window.chrome.webview.postMessage({channel:'code-agent-desktop',id,method:'status',params:{}})}))
  const backendPid=status.services.find(s=>s.id==='backend').pid
  const pendingHelper=async()=>{
    const children=await wait(async()=>{const result=await helpers(backendPid);report.helperSnapshots.push(result);return result.length?result:null},'real folder helper',15000)
    assert.equal(children.length,1);return children[0].ProcessId
  }
  // Force a real socket disconnect rather than assuming a browser navigation
  // or rejected fetch promise has physically closed its HTTP connection.
  const caller=http.request(`http://127.0.0.1:${frontend}/api/v1/code/fs/pick-folder`,{method:'POST',headers:{'Content-Type':'application/json'}})
  caller.on('error',()=>{})
  const clientClosed=new Promise(done=>caller.once('close',done))
  caller.end(JSON.stringify({start:fixture}))
  const firstHelper=await pendingHelper()
  caller.destroy();await clientClosed
  await wait(async()=>!(await helpers(backendPid)).some(child=>child.ProcessId===firstHelper),'disconnect removes only the pending helper',10000)
  assert.equal((await fetch(`http://127.0.0.1:${backend}/healthz`)).status,200)
  report.checks.push('a physical HTTP client disconnect propagates through the preview proxy and API and reaps only its helper while the backend stays healthy')
  await page.route('**/api/v1/code/fs/pick-folder',route=>route.continue({postData:JSON.stringify({start:fixture})}))
  await page.getByRole('button',{name:'打开文件夹',exact:true}).first().click()
  const secondHelper=await pendingHelper();assert.notEqual(secondHelper,firstHelper)
  const closeStarted=performance.now()
  await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'close-regression-exit',method:'exit',params:{}}))
  await wait(()=>app.exitCode!==null,'normal desktop close with a visible picker',110000)
  const appExit=await exited;assert.equal(appExit.code,0)
  report.elapsedCloseMilliseconds=Math.round(appExit.at-closeStarted)
  const verificationStarted=performance.now()
  await wait(async()=>!(await helpers(backendPid)).some(child=>child.ProcessId===secondHelper),'helper exits when its backend parent exits',10000)
  const closed=await readState();assert.equal(closed.state,'closed');assert.equal(closed.closeStartedEnabled,true)
  for(const p of [backend,frontend])await wait(async()=>{try{await fetch(`http://127.0.0.1:${p}/healthz`,{signal:AbortSignal.timeout(500)});return false}catch{return true}},'owned service stop',15000)
  report.checks.push('closing begins with an enabled main window, exits normally with the picker still open, and leaves no helper or owned service')
  report.verificationMilliseconds=Math.round(performance.now()-verificationStarted)
  report.totalCloseAndVerificationMilliseconds=Math.round(performance.now()-closeStarted)
  report.helperPids=[firstHelper,secondHelper];report.closed=closed;report.passed=true
}catch(error){report.error=error.stack||String(error);if(page)await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'close-regression-cleanup',method:'exit',params:{}})).catch(()=>{});if(app)await wait(()=>app.exitCode!==null,'normal failure cleanup',45000).catch(()=>{});throw error}
finally{await writeFile(join(out,'report.json'),JSON.stringify(report,null,2));if(browser)await browser.close().catch(()=>{});console.log(JSON.stringify(report))}
