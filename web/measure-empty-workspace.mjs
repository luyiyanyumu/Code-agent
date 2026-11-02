// Production frontend lab measurement. API data is empty and isolated; no model,
// filesystem registration, project mutation or external service is contacted.
import assert from 'node:assert/strict'
import {createServer} from 'node:http'
import {readFile, mkdir, writeFile, stat} from 'node:fs/promises'
import {resolve, extname, sep, join} from 'node:path'

const dist=resolve(process.argv[2]),out=resolve(process.argv[3])
const sampleCount=Number(process.argv[4]||3)
await mkdir(out,{recursive:true})
const mime={'.html':'text/html; charset=utf-8','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.woff2':'font/woff2'}
const unknown=[]
const api={
  '/api/v1/auth/me':{id:'performance-fixture',username:'Performance fixture',role:'admin'},
  '/api/v1/code/meta':{templates:['empty'],browse_enabled:false,features:{},git_available:false,native_picker:{available:false}},
  '/api/v1/code/projects':[],
  '/api/v1/web':{available:false,reason:'Isolated performance fixture'},
}
const server=createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://localhost'),pathname=decodeURIComponent(url.pathname)
    res.setHeader('Cache-Control','no-store')
    if(pathname.startsWith('/api/')){
      if(req.method!=='GET'||!Object.hasOwn(api,pathname)){unknown.push(`${req.method} ${pathname}`);res.writeHead(404);return res.end('{}')}
      res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(api[pathname]))
    }
    const file=resolve(dist,pathname==='/'?'index.html':`.${pathname}`)
    if(!file.startsWith(dist+sep)){res.writeHead(403);return res.end()}
    if(!(await stat(file)).isFile()){res.writeHead(404);return res.end()}
    res.setHeader('Content-Type',mime[extname(file)]||'application/octet-stream')
    res.end(await readFile(file))
  }catch{res.writeHead(404);res.end()}
})
await new Promise(done=>server.listen(0,'127.0.0.1',done))
const base=`http://127.0.0.1:${server.address().port}`
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const {chromium}=pw.default||pw
const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
const samples=[],errors=[]
try{
  for(let index=0;index<sampleCount;index++){
    const context=await browser.newContext({viewport:{width:1440,height:960},deviceScaleFactor:1.5,serviceWorkers:'block'})
    const page=await context.newPage(),session=await context.newCDPSession(page)
    await session.send('Emulation.setCPUThrottlingRate',{rate:4})
    page.on('pageerror',error=>errors.push(error.message))
    await page.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort())
    await page.addInitScript(()=>{
      window.__labLongTasks=[]
      new PerformanceObserver(list=>window.__labLongTasks.push(...list.getEntries().map(e=>({start:e.startTime,duration:e.duration})))).observe({type:'longtask',buffered:true})
    })
    await page.goto(base,{waitUntil:'domcontentloaded'})
    await page.getByRole('menubar',{name:'IDE 菜单'}).waitFor()
    const readyMs=await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(()=>done(performance.now())))))
    await page.waitForTimeout(1000)
    const result=await page.evaluate(()=>({
      fcpMs:performance.getEntriesByName('first-contentful-paint')[0]?.startTime??null,
      resources:performance.getEntriesByType('resource').map(e=>({path:new URL(e.name).pathname,bytes:e.encodedBodySize,decodedBytes:e.decodedBodySize,type:e.initiatorType})),
      longTasks:window.__labLongTasks,devicePixelRatio,
      logo:{source:document.querySelector('.agent-logo__mark')?.getAttribute('src'),width:document.querySelector('.agent-logo__mark')?.getBoundingClientRect().width},
    }))
    result.readyMs=readyMs
    result.monacoRequests=result.resources.filter(e=>/monaco-|\.worker-/.test(e.path)).map(e=>e.path)
    result.resourceBytes=result.resources.reduce((sum,e)=>sum+e.bytes,0)
    samples.push(result)
    if(index===0)await page.screenshot({path:join(out,'workspace-dpr-1.5.png')})
    await context.close()
  }
  assert.deepEqual(errors,[]);assert.deepEqual(unknown,[])
  const median=key=>samples.map(e=>e[key]).sort((a,b)=>a-b)[Math.floor(samples.length/2)]
  const report={passed:true,fixture:'empty API data, production assets, local HTTP, fresh browser contexts',browser:browser.version(),cpuThrottle:4,deviceScaleFactor:1.5,samples,
    median:{readyMs:median('readyMs'),fcpMs:median('fcpMs'),resourceBytes:median('resourceBytes')},unknownRequests:unknown,errors}
  await writeFile(join(out,'report.json'),JSON.stringify(report,null,2))
  console.log(JSON.stringify({passed:true,dist,samples:sampleCount,median:report.median,monacoRequests:samples.map(e=>e.monacoRequests.length)}))
}finally{await browser.close();await new Promise(done=>server.close(done))}
