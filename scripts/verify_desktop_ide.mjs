// Exercises the actual packaged desktop host and embedded workbench, using
// isolated data/ports and the application's explicit diagnostics endpoint.
import assert from 'node:assert/strict'
import {spawn} from 'node:child_process'
import {mkdir,readFile,writeFile} from 'node:fs/promises'
import {join,resolve} from 'node:path'
import net from 'node:net'
const root=resolve(process.argv[2]), out=resolve(process.argv[3])
await mkdir(out,{recursive:true})
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const {chromium}=pw.default||pw
const sleep=ms=>new Promise(done=>setTimeout(done,ms))
async function port(){return new Promise((done,reject)=>{const server=net.createServer();server.on('error',reject);server.listen(0,'127.0.0.1',()=>{const value=server.address().port;server.close(()=>done(value))})})}
async function wait(fn,label,timeout=90000){const deadline=Date.now()+timeout;let error;while(Date.now()<deadline){try{const value=await fn();if(value)return value}catch(e){error=e}await sleep(250)}throw new Error(`${label}: ${error?.message||'timed out'}`)}
const backend=await port(),frontend=await port(),debug=await port(), diagnostics=join(out,'native.json')
const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|npm_)/i.test(key)))
Object.assign(env,{PATH:[join(process.env.SystemRoot,'System32'),process.env.SystemRoot,join(process.env.SystemRoot,'System32/WindowsPowerShell/v1.0')].join(';'),CODE_AGENT_HOME:join(out,'user-data'),CODE_AGENT_PROVIDER:'offline',CODE_AGENT_AUTH_ENABLED:'false',CODE_AGENT_LAUNCHER_BACKEND_PORT:String(backend),CODE_AGENT_LAUNCHER_FRONTEND_PORT:String(frontend)})
const report={passed:false,root,backend,frontend,checks:[]};let browser,app,page
try{
  app=spawn(join(root,'Code-agent.exe'),['--diagnostics-file',diagnostics,'--diagnostics-port',String(debug),'--diagnostics-exit-after-ms','120000'],{env,cwd:out,windowsHide:true,stdio:'ignore'})
  const ready=await wait(async()=>{const value=JSON.parse((await readFile(diagnostics,'utf8')).replace(/^\uFEFF/,''));if(value.state==='failed')throw Error(value.error);return value.state==='ready'?value:null},'native workbench readiness')
  assert.equal(ready.integratedWorkbench,true);assert.equal(new URL(ready.mainViewUrl).port,String(frontend));assert.equal(ready.workspaceWindows,0)
  report.checks.push('single primary window navigates directly to the workbench')
  browser=await chromium.connectOverCDP(`http://127.0.0.1:${debug}`)
  page=await wait(()=>browser.contexts().flatMap(c=>c.pages()).find(p=>new URL(p.url()).port===String(frontend)),'embedded page')
  await page.getByRole('button',{name:/应用状态/}).first().waitFor({timeout:30000})
  const menuBar=page.getByRole('menubar',{name:'IDE 菜单'}),activity=page.getByRole('navigation',{name:'工具窗口'}),toolStrip=page.getByRole('toolbar',{name:'底部工具窗口'})
  await menuBar.waitFor({timeout:30000});await activity.waitFor();await toolStrip.waitFor()
  for(const label of ['文件','编辑','导航','视图','运行','帮助'])assert.equal(await menuBar.getByRole('menuitem',{name:label,exact:true}).isVisible(),true)
  for(const label of ['项目文件','搜索文件或代码','源代码管理','运行与调试','AI 助手'])assert.equal(await activity.getByRole('button',{name:label,exact:true}).isVisible(),true)
  for(const label of ['问题','终端','运行','调试','输出'])assert.equal(await toolStrip.getByRole('button',{name:new RegExp('^'+label)}).isVisible(),true)
  const emptyWorkspace=await page.locator('.editor-tabs .tab-select').count()===0
  assert.equal(emptyWorkspace,true,'isolated application home should open an empty workspace without files')
  await menuBar.getByRole('menuitem',{name:'文件',exact:true}).click()
  assert.equal(await page.getByRole('menu',{name:'文件',exact:true}).getByRole('menuitem',{name:/^保存(?:\s|$)/}).isDisabled(),true)
  await page.keyboard.press('Escape')
  report.emptyWorkspace=true;report.checks.push('installed IDE renders menu/activity/tool-window chrome and disables file save in an empty workspace')
  await page.keyboard.press('Control+Shift+P')
  const palette=page.getByRole('dialog',{name:'命令面板',exact:true})
  await palette.waitFor();await palette.getByRole('combobox',{name:'搜索命令',exact:true}).fill('专注')
  await palette.getByRole('option').filter({hasText:'专注模式'}).first().waitFor()
  await page.screenshot({path:join(out,'ide-command-palette.png')})
  await page.keyboard.press('Escape');await palette.waitFor({state:'detached'})
  report.checks.push('native embedded workbench opens and searches the global command palette with Ctrl+Shift+P and closes it with Escape')
  const left=page.locator('.layout > .left'),right=page.locator('.layout > .right'),center=page.locator('.layout > .center')
  const priorPanes={left:await left.isVisible(),right:await right.isVisible()}
  const focusMenu=async()=>{await menuBar.getByRole('menuitem',{name:'视图',exact:true}).click();await page.getByRole('menu',{name:'视图',exact:true}).getByRole('menuitemcheckbox',{name:/专注模式/}).click()}
  await focusMenu();await page.waitForFunction(()=>document.querySelector('.code')?.classList.contains('focus-mode'))
  assert.equal(await left.isVisible(),false);assert.equal(await right.isVisible(),false)
  const focusedBounds=await center.boundingBox();assert.ok(focusedBounds.width>250&&focusedBounds.height>200,'focus mode must retain a usable center pane without a project')
  await page.screenshot({path:join(out,'ide-focus.png')})
  await focusMenu();await page.waitForFunction(()=>!document.querySelector('.code')?.classList.contains('focus-mode'))
  assert.equal(await left.isVisible(),priorPanes.left);assert.equal(await right.isVisible(),priorPanes.right)
  report.checks.push('native focus mode works without open files and restores the previous side-pane visibility')
  await page.screenshot({path:join(out,'workbench.png')})
  await page.getByRole('button',{name:/应用状态/}).first().click()
  await page.getByRole('dialog',{name:/应用状态/}).waitFor()
  await wait(async()=>!(await page.getByLabel('运行日志内容').textContent()).includes('正在读取日志'),'maintenance log rendering',20000)
  await page.screenshot({path:join(out,'application-status.png')})
  await page.evaluate(()=>{window.testDesktopRpc=(method,params={})=>new Promise((resolve,reject)=>{const id=`verification-${Date.now()}-${Math.random()}`;const receive=event=>{if(event.data?.id!==id)return;window.chrome.webview.removeEventListener('message',receive);event.data.ok?resolve(event.data.result):reject(Error(event.data.error))};window.chrome.webview.addEventListener('message',receive);window.chrome.webview.postMessage({channel:'code-agent-desktop',id,method,params})})})
  const before=await page.evaluate(()=>window.testDesktopRpc('status'))
  assert.ok(before.services.every(service=>service.state==='healthy'))
  assert.deepEqual([...before.desktop.managedServices].sort(),['backend','frontend'])
  const logs=await page.evaluate(()=>window.testDesktopRpc('logs',{service:'backend',lines:50}))
  assert.equal(typeof logs.text,'string');report.checks.push('embedded maintenance UI and native authenticated status/log bridge')
  const after=await page.evaluate(()=>window.testDesktopRpc('action',{action:'restart',target:'backend'}))
  assert.equal(after.operation.state,'succeeded');assert.notEqual(after.services[0].pid,before.services[0].pid)
  assert.equal(after.services[1].pid,before.services[1].pid);report.checks.push('backend restarts inside the workbench without replacing its frontend')
  report.ready=ready;report.services=after.services.map(({id,pid,state})=>({id,pid,state}))
  await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'verification-exit',method:'exit',params:{}}))
  await wait(()=>app.exitCode!==null,'normal desktop close',45000)
  for(const servicePort of [backend,frontend])await wait(async()=>{try{await fetch(`http://127.0.0.1:${servicePort}/healthz`,{signal:AbortSignal.timeout(500)});return false}catch{return true}},'owned services stop on application exit',15000)
  report.checks.push('normal app exit closes its own frontend/backend after the frontend leave guard')
  report.passed=true
}catch(error){report.error=error.stack||String(error);if(page)await page.evaluate(()=>window.chrome.webview.postMessage({channel:'code-agent-desktop',id:'verification-error-exit',method:'exit',params:{}})).catch(()=>{});if(app)await wait(()=>app.exitCode!==null,'failed verification cleanup',45000).catch(()=>{});throw error}
finally{await writeFile(join(out,'report.json'),JSON.stringify(report,null,2));if(browser)await browser.close().catch(()=>{});console.log(JSON.stringify(report))}
