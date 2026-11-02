// Real SettingsView + ModelProviders + DialogHost; every API is an offline fixture.
import assert from 'node:assert/strict'
import {mkdir,writeFile} from 'node:fs/promises'
import {join,resolve} from 'node:path'
const base=process.argv[2]||'http://127.0.0.1:62485',origin=new URL(base).origin,out=resolve(process.argv[3]||'../.tmp/model-providers-browser')
await mkdir(out,{recursive:true})
const source=await(await fetch(`${base}/src/views/SettingsView.vue`)).text(),vuePath=source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1];assert.ok(vuePath,'Vite server required')
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js'),{chromium}=pw.default||pw
const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
const report={passed:false,checks:[],screenshots:[],errors:[],unexpected:[],mutations:[]},reads=[]
let cfg={provider:'deepseek',base_url:'https://api.deepseek.com',model_default:'flash',active_model:'flash',api_key_set:true,proxy_mode:'direct',context_window:131072,max_output_tokens:8192}
let profiles=[{id:'ds',name:'DeepSeek 工作连接',provider:'deepseek',base_url:cfg.base_url,model_default:'flash',models:[{id:'flash',name:'Flash'},{id:'pro',name:'自己的 Pro'}],api_key_set:true,active:true,proxy_mode:'direct',context_window:131072,max_output_tokens:8192},
{id:'local',name:'本地开发',provider:'openai_compat',base_url:'http://127.0.0.1:11434/v1',model_default:'qwen-local',models:[{id:'qwen-local',name:'本机模型'}],api_key_set:false,local:true,active:false,proxy_mode:'system'}]
let failNextSave=false,failNextActivate=false,serial=0,page
const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'})
const clone=value=>JSON.parse(JSON.stringify(value))
await context.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url()),path=url.pathname,method=req.method()
  if(url.origin!==origin){report.unexpected.push(`external:${url.origin}`);return route.abort()}
  if(path==='/model-providers-fixture')return route.fulfill({contentType:'text/html; charset=utf-8',body:`<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module">import {createApp,h,Fragment} from ${JSON.stringify(vuePath)};import Settings from '/src/views/SettingsView.vue';import DialogHost from '/src/components/ui/DialogHost.vue';import '/src/style.css';createApp({render:()=>h(Fragment,null,[h(Settings,{user:{id:'fixture',role:'admin'},initialSection:'general'}),h(DialogHost)])}).mount('#app');</script></body></html>`})
  if(!path.startsWith('/api/'))return route.continue()
  const body=method==='GET'?null:req.postDataJSON();let data
  if(method==='GET')reads.push(path);else report.mutations.push({method,path,body:{...body,...(body&&Object.hasOwn(body,'api_key')?{api_key:'[fixture-key-redacted]'}:{})}})
  const reply=(value,status=200)=>route.fulfill({status,contentType:'application/json; charset=utf-8',body:JSON.stringify(value)})
  if(path==='/api/v1/settings/model'&&method==='GET')data=cfg
  else if(path==='/api/v1/model-profiles'&&method==='GET')data=profiles
  else if(path==='/api/v1/settings/agent-team')data={max_depth:3,max_parallel:4}
  else if(path==='/api/v1/settings/agent-team/budgets')data={task_tokens:0,task_seconds:0,team_tokens:0,team_seconds:0}
  else if(path==='/api/v1/settings/permission')data={mode:'read-only',modes:[{id:'read-only',label:'只读',desc:'离线测试'}]}
  else if(path==='/api/v1/model-profiles/probe')data={models:['flash','pro','gateway/alpha','gateway/beta']}
  else if(path==='/api/v1/model-profiles'&&method==='POST'){
    const row={...(body.use_current?cfg:{}),...body,id:`created-${++serial}`,api_key_set:!!(body.api_key||body.use_current&&cfg.api_key_set),active:false};delete row.api_key;delete row.use_current;profiles.push(row);data=row
  }else{
    const m=path.match(/^\/api\/v1\/model-profiles\/([^/]+)(\/activate)?$/),row=m&&profiles.find(p=>p.id===m[1])
    if(row&&method==='PUT'){
      if(failNextSave){failNextSave=false;return reply({detail:'测试保存失败，草稿应保留'},503)}
      Object.assign(row,body,{api_key_set:row.api_key_set||!!body.api_key});delete row.api_key
      if(row.active)cfg={...cfg,...row,active_model:row.model_default};data=row
    }else if(row&&m[2]&&method==='POST'){
      if(failNextActivate){failNextActivate=false;return reply({detail:'测试默认连接应用失败'},503)}
      profiles.forEach(p=>p.active=p.id===row.id);cfg={...cfg,...row,active_model:row.model_default};data=cfg
    }else if(row&&method==='DELETE'){profiles=profiles.filter(p=>p.id!==row.id);data={ok:true}}
    else{report.unexpected.push(`${method} ${path}`);return reply({detail:'Unexpected fixture API'},500)}
  }
  return reply(clone(data))
})
const mutationCount=()=>report.mutations.filter(m=>m.method==='PUT'||m.method==='DELETE'||m.path==='/api/v1/model-profiles'||m.path.endsWith('/activate')).length
async function screenshot(name){const path=join(out,name);await page.screenshot({path,fullPage:true});report.screenshots.push(path)}
const row=id=>page.locator(`[data-provider-id="${id}"]`)
const form=()=>page.locator('.provider-editor')
const advanced=()=>page.locator('.custom-provider-settings > summary')
try{
  page=await context.newPage();page.setDefaultTimeout(15000);page.on('pageerror',error=>report.errors.push(error.message));page.on('dialog',dialog=>{report.unexpected.push(`native-dialog:${dialog.type()}`);void dialog.dismiss()})
  await page.goto(`${base}/model-providers-fixture`);await page.getByRole('heading',{name:'工作区与外观',exact:true}).waitFor()
  assert.equal(reads.includes('/api/v1/settings/model'),false);assert.equal(reads.includes('/api/v1/model-profiles'),false)
  await page.getByRole('button',{name:'模型与连接',exact:true}).click();await row('ds').locator('.provider-row-trigger').waitFor()
  await page.locator('#provider-api-key').waitFor();assert.equal(await page.locator('#provider-api-key').inputValue(),'');assert.equal(await page.locator('#provider-api-key').getAttribute('autocomplete'),'new-password')
  assert.equal(await page.locator('.provider-editor').count(),1);assert.equal(await page.locator('.custom-provider-settings').getAttribute('open'),null)
  assert.equal(reads.filter(p=>p==='/api/v1/settings/model').length,1);assert.equal(reads.filter(p=>p==='/api/v1/model-profiles').length,1)
  await screenshot('providers-desktop.png');report.checks.push('providers load only on first model-section visit and show one key-first editor with no stored key echo')
  await advanced().click();assert.equal(await page.locator('.editable-model-row').count(),2)
  await page.getByRole('button',{name:'获取可用模型',exact:true}).click();await page.getByRole('region',{name:'可用模型'}).waitFor()
  const probe=report.mutations.find(m=>m.path.endsWith('/probe'));assert.equal(probe.body.profile_id,'ds');assert.equal(probe.body.proxy_mode,'direct');assert.equal(Object.hasOwn(probe.body,'api_key'),false)
  await page.getByRole('searchbox',{name:'搜索可用模型'}).fill('alpha');await page.getByRole('checkbox',{name:'gateway/alpha'}).check()
  await page.getByRole('searchbox',{name:'搜索可用模型'}).fill('beta');await page.getByRole('checkbox',{name:'gateway/beta'}).check()
  assert.equal(await page.locator('.editable-model-row').count(),2);assert.equal(mutationCount(),0)
  await page.getByRole('region',{name:'可用模型'}).scrollIntoViewIfNeeded();await screenshot('providers-model-discovery.png')
  await page.getByRole('button',{name:'添加所选（2）',exact:true}).click();assert.equal(await page.locator('.editable-model-row').count(),4)
  await page.getByRole('textbox',{name:'模型显示名称 2',exact:true}).fill('保留自己的 Pro 名称');await page.locator('#default-model').selectOption('gateway/beta')
  await page.locator('#context-window').fill('262144');await page.locator('#max-output-tokens').fill('16384');await form().getByRole('button',{name:'应用',exact:true}).click()
  await page.locator('.model-feedback.success').waitFor();const saved=report.mutations.find(m=>m.method==='PUT'&&m.path.endsWith('/ds'))
  assert.equal(saved.body.models.length,4);assert.equal(saved.body.models[1].name,'保留自己的 Pro 名称');assert.equal(saved.body.model_default,'gateway/beta');assert.equal(Object.hasOwn(saved.body,'api_key'),false);assert.equal(saved.body.context_window,262144)
  report.checks.push('discovery opens a searchable multi-selector, retains hidden selections, and saves adopted models atomically with IDs, names and capacities')
  await row('ds').locator('.provider-row-trigger').click();await page.locator('#max-output-tokens').fill('262144');const beforeInvalid=mutationCount();await form().getByRole('button',{name:'应用',exact:true}).click();await page.getByRole('alert').filter({hasText:'最大输出'}).waitFor();assert.equal(mutationCount(),beforeInvalid)
  await page.locator('#max-output-tokens').fill('8192');failNextSave=true;await form().getByRole('button',{name:'应用',exact:true}).click();await page.getByRole('alert').filter({hasText:'测试保存失败'}).waitFor();assert.equal(await page.locator('#context-window').inputValue(),'262144')
  await form().getByRole('button',{name:'应用',exact:true}).click();await page.locator('.provider-editor').waitFor({state:'detached'})
  report.checks.push('invalid capacities never submit and failed saves keep an editable draft for successful retry')
  await page.getByRole('button',{name:'添加模型提供商',exact:true}).click();await page.getByRole('tab',{name:'自定义模型 API',exact:true}).click()
  await page.locator('#provider-name').fill('团队自定义 API');await page.locator('#provider-base-url').fill('http://[::1]:1234/v1');await page.getByRole('textbox',{name:'模型 ID 1',exact:true}).fill('custom/local');await page.getByRole('textbox',{name:'模型显示名称 1',exact:true}).fill('本地自定义')
  await page.getByRole('tab',{name:'第三方模型提供商',exact:true}).click();await page.getByRole('tab',{name:'自定义模型 API',exact:true}).click();assert.equal(await page.locator('#provider-name').inputValue(),'团队自定义 API')
  await form().getByRole('button',{name:'应用',exact:true}).click();await row('created-1').waitFor()
  assert.equal(cfg.model_default,'gateway/beta','saving a provider does not change the global default')
  const creation=report.mutations.find(m=>m.method==='POST'&&m.path==='/api/v1/model-profiles');assert.equal(creation.body.provider,'openai_compat');assert.equal(creation.body.models[0].id,'custom/local');assert.equal(Object.hasOwn(creation.body,'api_key'),false)
  await row('created-1').getByRole('button',{name:'设为全局默认',exact:true}).click();await page.locator('.model-default-summary').filter({hasText:'团队自定义 API'}).waitFor();assert.equal(cfg.model_default,'custom/local')
  report.checks.push('one add-provider entry preserves tab drafts, supports IPv6 local APIs, and changes global default only on an explicit action')
  await row('created-1').locator('.provider-row-trigger').click();await form().getByRole('button',{name:'删除提供商',exact:true}).click();await page.getByRole('dialog').waitFor();assert.match(await page.getByRole('dialog').innerText(),/团队自定义 API/)
  await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).click();assert.equal(report.mutations.some(m=>m.method==='DELETE'),false)
  await form().getByRole('button',{name:'删除提供商',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'删除提供商',exact:true}).click();await row('created-1').waitFor({state:'detached'})
  assert.equal(report.mutations.find(m=>m.method==='DELETE').path,'/api/v1/model-profiles/created-1')
  report.checks.push('deletion names the provider, supports safe cancellation, and removes only the confirmed target')
  await row('__current').locator('.provider-row-trigger').click();failNextActivate=true
  await form().getByRole('button',{name:'应用',exact:true}).click();await page.getByRole('alert').filter({hasText:'连接已保存，设为全局默认失败'}).waitFor()
  assert.equal(await page.locator('#provider-api-key').inputValue(),'');assert.equal(await row('created-2').locator('.provider-editor-host').isVisible(),true)
  await form().getByRole('button',{name:'应用',exact:true}).click();await page.locator('.provider-editor').waitFor({state:'detached'})
  assert.equal(report.mutations.filter(m=>m.method==='POST'&&m.path==='/api/v1/model-profiles'&&m.body.use_current).length,1)
  assert.equal(report.mutations.filter(m=>m.path==='/api/v1/model-profiles/created-2/activate').length,2)
  assert.equal(report.mutations.filter(m=>m.path==='/api/v1/model-profiles/created-2'&&m.method==='PUT').length,1)
  report.checks.push('saved-current migration retains its new provider ID and safely retries activation after a real HTTP failure')
  await row('ds').locator('.provider-row-trigger').click();await page.locator('#provider-api-key').fill('fixture-only-unsaved-key');await page.getByRole('button',{name:'工作区与外观',exact:true}).click();await page.getByRole('button',{name:'模型与连接',exact:true}).click();assert.equal(await page.locator('#provider-api-key').inputValue(),'fixture-only-unsaved-key')
  await page.getByRole('searchbox',{name:'搜索设置',exact:true}).fill('base URL');await page.locator('.search-result').filter({hasText:'服务地址'}).click();await page.locator('#provider-base-url').waitFor();assert.equal(await page.locator(':focus').getAttribute('id'),'provider-base-url')
  await screenshot('providers-advanced.png')
  for(const width of [768,390,320]){await page.setViewportSize({width,height:900});await page.waitForTimeout(100);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`overflow at${width}`);await screenshot(`providers-${width}.png`)}
  report.checks.push('section navigation preserves drafts and settings search opens the actual nested field;768/390/320px layouts remain bounded')
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.unexpected,[]);report.passed=true
}catch(error){report.error=error.stack||String(error);if(page)await page.screenshot({path:join(out,'failure.png'),fullPage:true}).catch(()=>{});process.exitCode=1}
finally{await writeFile(join(out,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify({passed:report.passed,checks:report.checks,screenshots:report.screenshots,errors:report.errors,unexpected:report.unexpected,error:report.error}))}
