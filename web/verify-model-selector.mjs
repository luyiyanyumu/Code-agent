// Model selection UI with isolated HTTP fixtures; no provider calls or user data.
import assert from 'node:assert/strict'
import {mkdir,writeFile} from 'node:fs/promises'
import {resolve,join} from 'node:path'
const base=process.argv[2]||'http://127.0.0.1:62485',out=resolve(process.argv[3]||'.tmp/model-selector-browser')
await mkdir(out,{recursive:true})
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const {chromium}=pw.default||pw
const source=await(await fetch(base+'/src/components/code/ModelSelector.vue')).text(),vue=source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vue,'Run the source Vite server')
const groups=[{id:'default',name:'Default provider',configured:true,models:[{id:'alpha-chat',name:'Alpha Chat'},{id:'alpha-code',name:'Alpha Code'},{id:'alpha-long',name:'Alpha Long'}]},
 {id:'profile-b',name:'Beta provider',configured:true,models:[{id:'beta-one',name:'Beta One'},{id:'beta-two',name:'Beta Two'},
 {id:'beta-thinking',name:'Beta Thinking',reasoning:{efforts:[{id:'off',name:'关闭'},{id:'low',name:'低'},{id:'high',name:'高'},{id:'max',name:'最高'}],defaultEffort:'high'}},
 {id:'beta-provider-default',name:'Provider Default',reasoning:{efforts:[{id:'custom',name:'自定强度'}]}}]}]
const current={a:{provider:'default',model:'alpha-chat'},b:{provider:'profile-b',model:'beta-one'}},calls=[],checks=[],errors=[],unexpected=[]
const directory=id=>({groups,current:current[id],default:{provider:'default',model:'alpha-chat'},available:true,inherited:false})
let rejectSelection=false,held=null,holdA=false,heldWrite=null,holdWrite=false
const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
const page=await browser.newPage({viewport:{width:1024,height:760}})
page.on('pageerror',error=>errors.push(error.message))
await page.route('**/api/**',async route=>{
 const req=route.request(),path=new URL(req.url()).pathname,match=path.match(/^\/api\/v1\/sessions\/([ab])\/(models|model)$/)
 if(!match){unexpected.push(path);return route.fulfill({status:500,contentType:'application/json',body:'{}'})}
 const id=match[1]
 if(req.method()==='GET'&&holdA&&id==='a'){held=route;return}
 if(req.method()==='PUT'){
  const body=req.postDataJSON();calls.push({id,body})
  if(rejectSelection)return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'Fixture selection refused'})})
  current[id]=body.provider?body:{provider:'default',model:'alpha-chat'}
  if(holdWrite){heldWrite=route;return}
 }
 await route.fulfill({contentType:'application/json',body:JSON.stringify(directory(id))})
})
await page.route('**/__selector-fixture',route=>route.fulfill({contentType:'text/html',body:`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="app"></div><script type="module">import{createApp,h,ref}from'${vue}';import View from'/src/components/code/ModelSelector.vue';import'/src/style.css';createApp({setup(){const id=ref('a'),visible=ref(true);window.switchModelSession=value=>id.value=value;window.toggleModelVisible=value=>visible.value=value;return()=>h('div',{style:'position:fixed;bottom:24px;right:24px;max-width:calc(100% - 48px)'},[h(View,{sessionId:id.value,visible:visible.value,onOpenSettings:section=>window.settingsSection=section})])}}).mount('#app')</script></html>`}))
const trigger=page.getByRole('button',{name:/选择会话模型/}),dialog=page.getByRole('dialog',{name:'会话模型选择'})
async function modelList(){await trigger.click();await dialog.getByRole('button',{name:/^模型/}).click();await page.getByRole('listbox',{name:'可用模型'}).waitFor()}
async function bounds(label){const box=await dialog.boundingBox(),size=page.viewportSize();assert.ok(box.x>=0&&box.x+box.width<=size.width+1&&box.y>=0&&box.y+box.height<=size.height+1,JSON.stringify(box));checks.push(label)}
const report={passed:false,checks}
try{
 await page.goto(base+'/__selector-fixture');await page.getByRole('button',{name:'选择会话模型：Alpha Chat'}).waitFor()
 await trigger.click();assert.equal(await dialog.getByText('推理强度',{exact:true}).count(),0);await bounds('root menu stays anchored and shows only supported controls')
 await page.screenshot({path:join(out,'model-menu-root.png')});await dialog.getByRole('button',{name:/^模型/}).click()
 await page.getByRole('searchbox',{name:'搜索模型'}).fill('btw');await page.getByRole('option',{name:'Beta Two',exact:true}).waitFor();assert.equal(await page.getByRole('option').count(),1)
 await page.keyboard.press('Enter');await dialog.waitFor({state:'detached'});assert.deepEqual(calls.at(-1),{id:'a',body:{provider:'profile-b',model:'beta-two'}})
 await page.getByRole('button',{name:'选择会话模型：Beta Two'}).waitFor();checks.push('grouped fuzzy search and Enter save only the addressed session selection')
 await modelList();await page.keyboard.press('Escape');assert.equal(await dialog.getByRole('button',{name:/^模型/}).count(),1);await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});assert.equal(await trigger.evaluate(el=>el===document.activeElement),true);checks.push('Escape returns through the menu and restores trigger focus')
 rejectSelection=true;await modelList();await page.getByRole('option',{name:'Alpha Code',exact:true}).click();await page.getByRole('alert').filter({hasText:'Fixture selection refused'}).waitFor();assert.equal(await trigger.innerText(),'Beta Two\n⌄');await page.screenshot({path:join(out,'model-selection-error.png')});checks.push('rejected selection leaves the previous model and a recoverable error')
 rejectSelection=false;await dialog.getByRole('button',{name:'返回模型菜单'}).click();await dialog.getByRole('button',{name:/管理模型提供方/}).click();assert.equal(await page.evaluate(()=>window.settingsSection),'models');checks.push('manage providers opens the actual model settings section')
 await modelList();await page.getByRole('searchbox',{name:'搜索模型'}).fill('Beta Thinking');await page.getByRole('option',{name:'Beta Thinking',exact:true}).click()
 assert.deepEqual(calls.at(-1).body,{provider:'profile-b',model:'beta-thinking'})
 await page.getByRole('button',{name:'选择会话模型：Beta Thinking，推理强度：高',exact:true}).waitFor()
 await trigger.click();await dialog.getByRole('button',{name:/^推理强度/}).click()
 const efforts=page.getByRole('listbox',{name:'推理强度',exact:true})
 assert.equal(await efforts.getByRole('option').count(),4);assert.equal(await efforts.getByRole('option',{name:'高',exact:true}).getAttribute('aria-selected'),'true')
 await page.screenshot({path:join(out,'reasoning-levels.png')})
 await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');await dialog.waitFor({state:'detached'})
 assert.deepEqual(calls.at(-1).body,{provider:'profile-b',model:'beta-thinking',reasoningEffort:'max'})
 await page.getByRole('button',{name:'选择会话模型：Beta Thinking，推理强度：最高',exact:true}).waitFor()
 checks.push('model default appears in the trigger and keyboard effort selection saves the complete addressed session choice')
 await trigger.click();await dialog.getByRole('button',{name:/^推理强度/}).click();await page.keyboard.press('Escape')
 assert.equal(await dialog.getByRole('button',{name:/^推理强度/}).evaluate(el=>el===document.activeElement),true)
 await page.keyboard.press('Enter');await efforts.waitFor();await dialog.getByRole('button',{name:'返回模型菜单',exact:true}).focus();await page.keyboard.press('Enter')
 assert.equal(await efforts.count(),0);await dialog.getByRole('button',{name:/^推理强度/}).click();await efforts.getByRole('option',{name:'关闭',exact:true}).click()
 assert.equal(calls.at(-1).body.reasoningEffort,'off');checks.push('effort Escape restores the correct root row; header Enter returns instead of submitting a model; off persists')
 await modelList();await page.getByRole('searchbox',{name:'搜索模型'}).fill('Provider Default');await page.getByRole('option',{name:'Provider Default',exact:true}).click()
 await trigger.click();await dialog.getByRole('button',{name:/^推理强度/}).click();await efforts.getByRole('option',{name:'自定强度',exact:true}).click()
 await trigger.click();await dialog.getByRole('button',{name:/^推理强度/}).click();await efforts.getByRole('option',{name:'服务商默认',exact:true}).click()
 assert.deepEqual(calls.at(-1).body,{provider:'profile-b',model:'beta-provider-default'});checks.push('provider-default effort remains available only when the catalog has no default; reset omits the old effort')
 holdWrite=true;await modelList();await page.getByRole('searchbox',{name:'搜索模型'}).fill('Beta Two');await page.getByRole('option',{name:'Beta Two',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('.model-trigger')?.disabled)
 await page.evaluate(()=>window.toggleModelVisible(false));await page.evaluate(()=>window.toggleModelVisible(true));assert.ok(heldWrite)
 holdWrite=false;await heldWrite.fulfill({contentType:'application/json',body:JSON.stringify(directory('a'))})
 await page.getByRole('button',{name:'选择会话模型：Beta Two',exact:true}).waitFor();assert.equal(await trigger.isEnabled(),true)
 await trigger.click();assert.equal(await dialog.getByText('推理强度',{exact:true}).count(),0);await page.keyboard.press('Escape');checks.push('switching to unsupported models clears effort; visibility refresh cannot strand an in-flight selection in saving state')
 await page.setViewportSize({width:320,height:700});await modelList();await bounds('320px model list remains within the viewport');await page.screenshot({path:join(out,'model-list-320.png')});await page.keyboard.press('Escape');await page.keyboard.press('Escape')
 holdA=true;await trigger.click();await page.waitForFunction(()=>document.querySelector('.model-trigger')?.innerText.includes('…'))
 await page.evaluate(()=>window.switchModelSession('b'));await page.getByRole('button',{name:'选择会话模型：Beta One'}).waitFor();assert.ok(held);holdA=false;await held.fulfill({contentType:'application/json',body:JSON.stringify(directory('a'))});await new Promise(done=>setTimeout(done,100));assert.equal(await page.getByRole('button',{name:'选择会话模型：Beta One'}).count(),1);checks.push('late catalog response from another session cannot overwrite current selection')
 assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);report.passed=true;report.mutations=calls
}catch(error){report.error=error.stack;throw error}
finally{await writeFile(join(out,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify(report))}
