// Real packaged host + real FastAPI + real Windows Shell picker. After the
// isolated desktop is ready, use the supported Windows computer-use tool to
// click Open Folder and Cancel. No picker response or API is mocked.
import assert from 'node:assert/strict'
import {spawn} from 'node:child_process'
import {readFile,mkdir,writeFile} from 'node:fs/promises'
import {resolve,join} from 'node:path'
import net from 'node:net'
const root=resolve(process.argv[2]),out=resolve(process.argv[3])
await mkdir(out,{recursive:true})
const fixture=join(out,'chooser-fixture');await mkdir(fixture,{recursive:true})
await writeFile(join(fixture,'README.md'),'# Isolated DPI picker verification\n')
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const {chromium}=pw.default||pw
const sleep=ms=>new Promise(done=>setTimeout(done,ms))
async function port(){return new Promise((done,reject)=>{const s=net.createServer();s.on('error',reject);s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>done(p))})})}
async function wait(fn,label,timeout=90000){const end=Date.now()+timeout;while(Date.now()<end){const value=await fn();if(value)return value;await sleep(200)}throw Error(`Timed out: ${label}`)}
const backend=await port(),frontend=await port(),debug=await port(),diagnostics=join(out,'native.json')
const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|npm_)/i.test(key)))
Object.assign(env,{PATH:[join(process.env.SystemRoot,'System32'),process.env.SystemRoot,join(process.env.SystemRoot,'System32/WindowsPowerShell/v1.0')].join(';'),CODE_AGENT_HOME:join(out,'user-data'),CODE_AGENT_PROVIDER:'offline',CODE_AGENT_AUTH_ENABLED:'false',CODE_AGENT_LAUNCHER_BACKEND_PORT:String(backend),CODE_AGENT_LAUNCHER_FRONTEND_PORT:String(frontend)})
const report={passed:false,root,backend,frontend,checks:[]};let app,browser,page
try{
  app=spawn(join(root,'Code-agent.exe'),['--diagnostics-file',diagnostics,'--diagnostics-port',String(debug),'--diagnostics-exit-after-ms','120000'],{cwd:out,env,windowsHide:false,stdio:'ignore'})
  const ready=await wait(async()=>{try{const data=JSON.parse((await readFile(diagnostics,'utf8')).replace(/^\uFEFF/,''));if(data.state==='failed')throw Error(data.error);return data.state==='ready'?data:null}catch(e){if(e.code==='ENOENT'||e instanceof SyntaxError)return null;throw e}},'desktop ready')
  report.ready=ready;assert.equal(ready.integratedWorkbench,true)
  browser=await chromium.connectOverCDP(`http://127.0.0.1:${debug}`)
  page=await wait(()=>browser.contexts().flatMap(c=>c.pages()).find(p=>p.url().startsWith(`http://127.0.0.1:${frontend}/`)),'embedded workbench')
  await page.getByRole('menubar',{name:'IDE 菜单'}).waitFor()
  // Set only the starting directory to a generated fixture. The actual local
  // request, Python process, COM dialog and Cancel response all remain real.
  await page.route('**/api/v1/code/fs/pick-folder',route=>route.continue({postData:JSON.stringify({start:fixture})}))
  let result=null,pickerRequests=0
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/v1/code/fs/pick-folder')pickerRequests++})
  page.on('response',async response=>{if(new URL(response.url()).pathname==='/api/v1/code/fs/pick-folder'){
    try { const text=await response.text();try{result={status:response.status(),body:JSON.parse(text)}}catch{result={status:response.status(),body:{error:text.slice(0,300)}}} }
    catch(error){result={status:response.status(),body:{error:error.message}}}
  }})
  await writeFile(join(out,'ui-ready.json'),JSON.stringify({phase:'ready-for-native-folder-button',root,pid:ready.pid,windowHandle:ready.windowHandle,fixture}))
  if(process.argv.includes('--auto-open'))await page.getByRole('button',{name:'打开文件夹',exact:true}).first().click()
  console.log('READY_FOR_NATIVE_UI: Click Open Folder in this isolated Code-agent, then Cancel in its Windows dialog.')
  await wait(()=>result,'real Windows picker cancellation',90000)
  assert.equal(pickerRequests,1);assert.equal(result.status,200);assert.deepEqual(result.body,{path:null,cancelled:true})
  await page.getByRole('button',{name:'打开文件夹',exact:true}).first().waitFor()
  const projects=await page.evaluate(async()=>{const r=await fetch('/api/v1/code/projects');return r.json()})
  assert.deepEqual(projects,[])
  report.checks.push('native desktop button opens exactly one real Python/Shell picker; Cancel succeeds and leaves the isolated workspace unchanged')
  await page.screenshot({path:join(out,'workbench-after-cancel.png')})
  await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'picker-verification-exit',method:'exit',params:{}}))
  await wait(()=>app.exitCode!==null,'normal app exit',45000)
  for(const p of [backend,frontend])await wait(async()=>{try{await fetch(`http://127.0.0.1:${p}/healthz`,{signal:AbortSignal.timeout(500)});return false}catch{return true}},'owned service stop',15000)
  report.checks.push('normal exit stops only the isolated desktop services')
  report.pickerRequests=pickerRequests;report.passed=true
}catch(error){report.error=error.stack||String(error);if(page)await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'picker-verification-error-exit',method:'exit',params:{}})).catch(()=>{});if(app)await wait(()=>app.exitCode!==null,'failure cleanup',45000).catch(()=>{});throw error}
finally{await writeFile(join(out,'report.json'),JSON.stringify(report,null,2));if(browser)await browser.close().catch(()=>{});console.log(JSON.stringify(report))}
