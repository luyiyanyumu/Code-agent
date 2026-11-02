// Actual packaged/installed Windows desktop shutdown benchmark. All homes,
// ports and files are isolated; no user's workspace or model API is used.
// node scripts/verify_desktop_shutdown.mjs <app-root> <evidence-dir> [runs=3]
import assert from 'node:assert/strict'
import {spawn} from 'node:child_process'
import {mkdir,readFile,writeFile} from 'node:fs/promises'
import {join,resolve} from 'node:path'
import net from 'node:net'

assert.ok(process.argv[2]&&process.argv[3],'Provide an app root and evidence directory')
const root=resolve(process.argv[2]),out=resolve(process.argv[3]),runs=Number(process.argv[4]||3)
assert.ok(Number.isInteger(runs)&&runs>=1&&runs<=10,'Runs must be between 1 and 10')
await mkdir(out,{recursive:true})
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const {chromium}=pw.default||pw,delay=ms=>new Promise(done=>setTimeout(done,ms))
async function wait(fn,label,timeout=90000){const end=performance.now()+timeout;while(performance.now()<end){const value=await fn();if(value)return value;await delay(100)}throw Error(`Timed out: ${label}`)}
async function bounded(promise,milliseconds,label){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(`Timed out: ${label}`)),milliseconds)})])}finally{clearTimeout(timer)}}
async function port(){return new Promise((done,reject)=>{const socket=net.createServer();socket.once('error',reject);socket.listen(0,'127.0.0.1',()=>{const value=socket.address().port;socket.close(()=>done(value))})})}
async function listening(port){return new Promise(done=>{const socket=net.connect({port,host:'127.0.0.1'});let settled=false;const finish=value=>{if(settled)return;settled=true;socket.destroy();done(value)};socket.once('connect',()=>finish(true));socket.once('error',()=>finish(false));socket.setTimeout(500,()=>finish(true))})}
function gone(pid){try{process.kill(pid,0);return false}catch(error){if(error.code==='ESRCH')return true;throw error}}
function environment(home,backend,frontend){
  const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|npm_)/i.test(key)))
  return Object.assign(env,{PATH:[join(process.env.SystemRoot,'System32'),process.env.SystemRoot,join(process.env.SystemRoot,'System32/WindowsPowerShell/v1.0')].join(';'),CODE_AGENT_HOME:home,CODE_AGENT_PROVIDER:'offline',CODE_AGENT_AUTH_ENABLED:'false',CODE_AGENT_LAUNCHER_BACKEND_PORT:String(backend),CODE_AGENT_LAUNCHER_FRONTEND_PORT:String(frontend)})
}
const report={passed:false,root,runs,measurement:'Native process exit event minus native exit request; post-exit verification measured separately',samples:[]}
try{
  for(let index=1;index<=runs;index++){
    const directory=join(out,`run-${index}`);await mkdir(directory,{recursive:true})
    const backend=await port(),frontend=await port(),debug=await port(),diagnostics=join(directory,'native.json')
    const readState=async()=>JSON.parse((await readFile(diagnostics,'utf8')).replace(/^\uFEFF/,''))
    const sample={run:index,backend,frontend,passed:false};let app,browser,page,exited
    report.samples.push(sample)
    try{
      app=spawn(join(root,'Code-agent.exe'),['--diagnostics-file',diagnostics,'--diagnostics-port',String(debug),'--diagnostics-exit-after-ms','120000'],{cwd:directory,env:environment(join(directory,'user-data'),backend,frontend),windowsHide:true,stdio:'ignore'})
      exited=new Promise((done,reject)=>{app.once('exit',(code,signal)=>done({code,signal,at:performance.now()}));app.once('error',reject)})
      const ready=await wait(async()=>{if(app.exitCode!==null)throw Error('Native process exited before ready; another instance may own this app root');try{const state=await readState();if(state.state==='failed')throw Error(state.error);return state.state==='ready'?state:null}catch(error){if(error.code==='ENOENT'||error instanceof SyntaxError)return null;throw error}},'native ready')
      assert.equal(ready.pid,app.pid);assert.equal(ready.windowEnabled,true);assert.equal(ready.integratedWorkbench,true)
      browser=await chromium.connectOverCDP(`http://127.0.0.1:${debug}`)
      page=await wait(()=>browser.contexts().flatMap(context=>context.pages()).find(candidate=>candidate.url().startsWith(`http://127.0.0.1:${frontend}/`)),'embedded workbench')
      await page.getByRole('menubar',{name:'IDE 菜单'}).waitFor()
      const status=await page.evaluate(()=>new Promise((done,reject)=>{const id='shutdown-benchmark-status';const timer=setTimeout(()=>reject(Error('Native status timed out')),30000);const receive=event=>{if(event.data?.id!==id)return;clearTimeout(timer);window.chrome.webview.removeEventListener('message',receive);event.data.ok?done(event.data.result):reject(Error(event.data.error))};window.chrome.webview.addEventListener('message',receive);window.chrome.webview.postMessage({channel:'code-agent-desktop',id,method:'status',params:{}})}))
      const services=['backend','frontend'].map(id=>{const service=status.services.find(candidate=>candidate.id===id);assert.ok(service&&service.pid>0,`Missing live ${id}`);return {id,pid:service.pid}})
      sample.nativePid=app.pid;sample.controllerPid=ready.controllerPid;sample.services=services
      assert.ok(sample.controllerPid>0)
      const started=performance.now()
      sample.closePhases=[]
      let monitor=true
      const phases=(async()=>{while(monitor){try{const state=await readState();if(sample.closePhases.at(-1)?.state!==state.state)sample.closePhases.push({state:state.state,elapsedMilliseconds:Math.round(performance.now()-started)})}catch{}await delay(25)}})()
      const send=page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'shutdown-benchmark-exit',method:'exit',params:{}}))
      // This timestamp comes from ChildProcess's exit event, without 100 ms
      // polling, PowerShell/CIM startup or port checks inflating the result.
      let ended
      try{ended=await bounded(exited,110000,'normal native process exit')}
      finally{monitor=false;await phases}
      await send.catch(()=>{})
      sample.elapsedCloseMilliseconds=Math.round(ended.at-started)
      assert.equal(ended.code,0);assert.equal(ended.signal,null)
      const verificationStarted=performance.now()
      const closed=await readState();assert.equal(closed.state,'closed');assert.equal(closed.closeStartedEnabled,true)
      await wait(()=>gone(sample.nativePid)&&gone(sample.controllerPid)&&services.every(service=>gone(service.pid)),'native, controller and owned service processes ended',15000)
      await wait(async()=>!(await listening(backend))&&!(await listening(frontend)),'owned ports are no longer listening',15000)
      sample.verificationMilliseconds=Math.round(performance.now()-verificationStarted)
      sample.totalCloseAndVerificationMilliseconds=Math.round(performance.now()-started)
      sample.passed=true
      console.log(JSON.stringify({run:index,elapsedCloseMilliseconds:sample.elapsedCloseMilliseconds,verificationMilliseconds:sample.verificationMilliseconds,passed:true}))
    }catch(error){
      sample.error=error.stack||String(error)
      if(page&&app&&app.exitCode===null)await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'shutdown-benchmark-cleanup',method:'exit',params:{}})).catch(()=>{})
      if(app&&app.exitCode===null&&exited)await bounded(exited,45000,'normal failure cleanup').catch(()=>{})
      throw error
    }finally{
      if(browser)await browser.close().catch(()=>{})
      await writeFile(join(directory,'report.json'),JSON.stringify(sample,null,2))
    }
  }
  const times=report.samples.map(sample=>sample.elapsedCloseMilliseconds).sort((a,b)=>a-b)
  report.medianCloseMilliseconds=times.length%2?times[(times.length-1)/2]:(times[times.length/2-1]+times[times.length/2])/2
  report.minimumCloseMilliseconds=times[0];report.maximumCloseMilliseconds=times.at(-1);report.passed=true
}catch(error){report.error=error.stack||String(error);process.exitCode=1}
finally{await writeFile(join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report))}
