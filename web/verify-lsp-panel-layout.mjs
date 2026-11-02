// Real EditorPane and Monaco; offline catalog/clipboard fixtures only.
import assert from 'node:assert/strict'
import {mkdir,writeFile} from 'node:fs/promises'
import {join,resolve} from 'node:path'
const base=process.argv[2]||'http://127.0.0.1:5179',origin=new URL(base).origin,out=resolve(process.argv[3]||'.tmp/lsp-panel-layout-browser')
await mkdir(out,{recursive:true})
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const source=await(await fetch(base+'/src/components/code/EditorPane.vue')).text(),vue=source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vue,'Start the Vite source server')
const hint='未找到 Java 语言服务。请重新安装包含 Java 分析组件的 Code-agent 安装包。服务需要 Java 21 或更高版本；安装后点击“重新探测”即可连接。参考说明：https://example.invalid/very-long-distribution-name/installation-guide/windows-x64/java-language-server-and-runtime'
const command='C:\\Users\\fixture\\AppData\\Local\\Programs\\Code-agent\\runtimes\\language-servers\\yaml-language-server\\distribution-with-a-very-long-name-and-no-breakpoints\\bin\\yaml-language-server.CMD'
const servers=[{id:'jdtls',name:'Eclipse JDT (Java)',languages:['java'],enabled:true,state:'missing',detected:true,install_hint:hint},
 {id:'yaml',name:'YAML',languages:['yaml'],enabled:true,state:'installed',detected:true,resolved_command:command},
 {id:'other',name:'AnExtremelyLongLanguageServerNameWithoutAnyNaturalBreakpointsForNarrowViewports',languages:['other'],state:'missing',enabled:true,detected:false,install_hint:'安装其他语言的服务组件。'}]
const report={passed:false,checks:[],viewports:[],errors:[],unexpected:[],requests:[]}
const browser=await(pw.default||pw).chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
const page=await browser.newPage({viewport:{width:1280,height:860}})
page.setDefaultTimeout(20000);page.on('pageerror',e=>report.errors.push(e.message))
await page.addInitScript(()=>{Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{if(window.__copyFailure)throw Error('Fixture denied');window.__copiedText=text}},configurable:true})})
await page.route('**/*',route=>{
 const url=new URL(route.request().url())
 if(url.origin!==origin){report.unexpected.push(url.href);return route.abort()}
 if(url.pathname==='/api/v1/code/projects/lsp-layout/lsp'){report.requests.push(url.search);return route.fulfill({contentType:'application/json',body:JSON.stringify({servers,sessions:[],failures:{},environment:{}})})}
 if(url.pathname.startsWith('/api/')){report.unexpected.push(url.pathname);return route.abort()}
 if(url.pathname==='/__lsp-layout-fixture')return route.fulfill({contentType:'text/html',body:`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body{margin:0;height:100%;min-width:0}#app{height:100%;display:flex;padding:12px;box-sizing:border-box;min-width:0}#app>.pane{border:1px solid #dfe3ec;overflow:hidden}</style><div id="app"></div><script type="module">import{createApp,h}from'${vue}';import EditorPane from'/src/components/code/EditorPane.vue';import'/src/style.css';createApp({render(){return h(EditorPane,{file:{path:'src/Example.java',text:'public class Example {}',lang:'java',uri:'file:///E:/layout-fixture/src/Example.java',sha1:'fixture'},projectId:'lsp-layout',sessionId:'layout-session',readonly:true,lspServer:{id:'jdtls',name:'Eclipse JDT (Java)',available:false,hint:${JSON.stringify(hint)}},languageServers:${JSON.stringify(servers)}})}}).mount('#app')</script></html>`})
 return route.continue()
})
const dialog=page.getByRole('dialog',{name:'代码分析设置',exact:true}),trigger=page.getByRole('button',{name:'代码分析未启用，查看配置',exact:true})
try{
 await page.goto(base+'/__lsp-layout-fixture');await trigger.waitFor();await trigger.click();await dialog.waitFor();await dialog.locator('.lsp-list li').nth(1).waitFor()
 const java=dialog.locator('[data-server-id="jdtls"]'),yaml=dialog.locator('[data-server-id="yaml"]')
 assert.equal(await java.locator('.lsp-install-hint').innerText(),hint);assert.equal(await yaml.locator('.lsp-command').innerText(),command)
 await java.getByRole('button',{name:'复制 Eclipse JDT (Java) 安装说明',exact:true}).click();assert.equal(await page.evaluate(()=>window.__copiedText),hint);assert.equal(await java.getByRole('button',{name:'复制 Eclipse JDT (Java) 安装说明',exact:true}).innerText(),'已复制')
 await yaml.getByRole('button',{name:'复制 YAML 命令路径',exact:true}).click();assert.equal(await page.evaluate(()=>window.__copiedText),command)
 report.checks.push('installation guidance and executable path are complete readable text with distinct copy actions')
 await page.evaluate(()=>window.__copyFailure=true);await java.getByRole('button',{name:'复制 Eclipse JDT (Java) 安装说明',exact:true}).click();await dialog.getByRole('alert').filter({hasText:'复制失败'}).waitFor();await page.evaluate(()=>window.__copyFailure=false)
 report.checks.push('clipboard failures remain visible without losing the manual-copy text')
 await dialog.getByRole('button',{name:'重新探测',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.lsp-catalog-summary')?.textContent.includes('目录 3'))
 assert.ok(report.requests.some(query=>query.includes('refresh=1')))
 await dialog.getByRole('button',{name:'显示全部',exact:true}).click();assert.equal(await dialog.locator('.lsp-list li').count(),3)
 report.checks.push('reprobe calls the refresh endpoint and show-all retains the complete catalog')
 for(const width of [1280,768,390,320]){
  await page.setViewportSize({width,height:860})
  const geometry=await dialog.evaluate(el=>{const box=el.getBoundingClientRect();return{x:box.x,y:box.y,width:box.width,height:box.height,scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,texts:[...el.querySelectorAll('.lsp-command,.lsp-install-hint,.nm')].map(node=>({text:node.textContent,scroll:node.scrollWidth,client:node.clientWidth,whiteSpace:getComputedStyle(node).whiteSpace}))}})
  assert.ok(geometry.x>=0&&geometry.x+geometry.width<=width+1,JSON.stringify(geometry));assert.ok(geometry.scrollWidth<=geometry.clientWidth+1,JSON.stringify(geometry))
  for(const text of geometry.texts)assert.ok(text.scroll<=text.client+1,JSON.stringify(text))
  await page.screenshot({path:join(out,`lsp-panel-${width}.png`)});report.viewports.push({width,geometry})
 }
 report.checks.push('1280/768/390/320 viewports wrap long names, paths and guidance without horizontal clipping')
 await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});assert.equal(await trigger.evaluate(el=>el===document.activeElement),true)
 await page.keyboard.press('Enter');await dialog.waitFor();await dialog.getByRole('button',{name:'关闭代码分析设置',exact:true}).click();await dialog.waitFor({state:'detached'});assert.equal(await trigger.evaluate(el=>el===document.activeElement),true)
 report.checks.push('Escape and the close button restore keyboard focus to the analysis-status trigger')
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpected,[]);report.passed=true
}catch(e){report.error=e.stack;await page.screenshot({path:join(out,'failure.png')}).catch(()=>{});process.exitCode=1}
finally{await writeFile(join(out,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify(report))}
