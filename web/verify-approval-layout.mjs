import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'

// Production components with fixture-only HTTP and WebSocket. No real approval,
// model request, shell command, or project write is permitted by this fixture.
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || '../.tmp/approval-layout-browser')
await fs.mkdir(output, { recursive: true })
const component = await (await fetch(`${base}/src/components/code/AiPanel.vue`)).text()
const vuePath = component.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'Vite is required')

const command = Array.from({ length: 26 }, (_, index) => `printf "step ${index}: 检查带空格的路径"; ls frontend/src/components/very-long-directory-${index}/node_modules/@vue/compiler-sfc/ 2>/dev/null | wc -l;`).join('\n') + '\nprintf "COMMAND_END"'
const longApproval = id => ({ approval_id: id, tool: 'bash', reason: '当前会话为只读模式，此操作需要单次授权。批准仅允许本次操作，会话保持只读。' + '\n' + '请核对本次完整命令及参数。'.repeat(45), arguments: { command, timeout: 60, env_description: Array.from({ length: 18 }, (_, index) => `参数 ${index}：仅离线界面测试，不执行命令。`), marker: 'ARGS_END' } })
const initialHistory = [
  { seq: 1, type: 'user_message', payload: { text: '检查代码助手审批界面的底部按钮。' } },
  { seq: 2, type: 'assistant_message', payload: { text: '这是离线测试消息。\n\n' + '已有会话内容保留，可滚动查看。'.repeat(35) } },
  { seq: 3, type: 'done', payload: { text: '离线消息', usage: { input_tokens: 370895, output_tokens: 9197, cached_tokens: 351104 } } },
]

const fixture = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="fixture"></div><script type="module">
import {createApp,h,ref} from '${vuePath}'; import AiPanel from '/src/components/code/AiPanel.vue'; import '/src/style.css';
const history=${JSON.stringify(initialHistory)};
createApp({setup(){const selected=ref('fixture-worker'),team=ref(null);window.fixtureSelect=id=>selected.value=id;window.fixtureSetTeam=value=>team.value=value;return()=>h('div',{class:'fixture-workbench'},[h('header',{class:'fixture-top'},'离线代码工作区'),h('div',{class:'fixture-main'},[h('main',{class:'fixture-editor'},'编辑器占位'),h('aside',{class:'fixture-sidebar'},[h('nav',{class:'fixture-tabs'},'AI　修改　台账　任务　并行　Git'),h(AiPanel,{sessionId:selected.value,agentId:selected.value,team:team.value,writable:false,onAgentSelect:id=>selected.value=id,onAgentLead:()=>selected.value='fixture-lead'})])]),h('footer',{class:'fixture-bottom'},'项目状态栏')])}}).mount('#fixture');
</script><style>html,body,#fixture{height:100%;width:100%;margin:0;overflow:hidden}.fixture-workbench{height:100%;display:flex;flex-direction:column;min-height:0}.fixture-top{height:36px;flex:none;padding:8px 12px;box-sizing:border-box;border-bottom:1px solid var(--border)}.fixture-main{display:flex;flex:1;min-height:0;min-width:0}.fixture-editor{flex:1;min-width:0;background:var(--panel-soft);padding:20px}.fixture-sidebar{width:568px;min-width:0;min-height:0;flex:none;display:flex;flex-direction:column;border-left:1px solid var(--border)}.fixture-tabs{height:40px;flex:none;padding:10px 14px;box-sizing:border-box;border-bottom:1px solid var(--border);font-size:12px}.fixture-bottom{height:22px;flex:none;border-top:1px solid var(--border);font-size:10px;padding:3px 10px;box-sizing:border-box}@media(max-width:650px){.fixture-editor{display:none}.fixture-sidebar{width:100%;border:0}}</style></html>`

// Compile the actual independent-chat approval component and use the actual
// modal-mask rule, without launching a second app or reaching a real backend.
const chatSource = await fs.readFile(new URL('../chat-app/src/components/ApprovalRequest.vue', import.meta.url), 'utf8')
const chatView = await fs.readFile(new URL('../chat-app/src/views/ChatView.vue', import.meta.url), 'utf8')
const chatDescriptor = parse(chatSource).descriptor
const scopeId = 'data-v-approval-fixture'
const chatScript = compileScript(chatDescriptor, { id: scopeId, inlineTemplate: true }).content.replace(/from (["'])vue\1/g, `from '${vuePath}'`)
const chatStyle = compileStyle({ source: chatDescriptor.styles[0].content, filename: 'ApprovalRequest.vue', id: scopeId, scoped: true }).code
const modalRule = chatView.match(/\.modal-mask\s*\{[^}]*\}/)?.[0]
assert.ok(modalRule, 'Independent chat modal-mask style exists')
const overlayFixture = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="fixture"></div><script type="module">import {createApp,h} from '${vuePath}';import Component from '/fixture-chat-approval.js';import '/src/style.css';Component.__scopeId='${scopeId}';createApp({render(){return h('div',{class:'modal-mask'},[h(Component,{approval:${JSON.stringify(longApproval('fixture-chat'))},readonly:true,onDecide:decision=>window.overlayDecisions.push(decision)})])}}).mount('#fixture');</script><style>html,body,#fixture{height:100%;width:100%;margin:0;overflow:hidden}${modalRule}${chatStyle}</style></html>`

const checks = [], errors = [], unexpected = [], writes = [], measurements = []
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
let releaseDecision = null
const teamFor = approval => ({ lead: { session_id: 'fixture-lead', role: 'lead', title: 'Lead', state: 'idle', effective_permission: 'read-only' }, agents: [{ session_id: 'fixture-worker', parent_session_id: 'fixture-lead', lead_session_id: 'fixture-lead', role: 'node', title: '补验三条命令并修正脚本注释路径', depth: 1, state: approval ? 'waiting' : 'idle', phase: approval ? 'waiting_approval' : '', current_tool: 'bash', started_at: Date.now() - 30000, last_progress_at: Date.now() - 22000, effective_permission: 'read-only', blocked_reason: approval?.reason || '', ...(approval ? { pending_approval: approval } : {}) }], limits: { max_depth: 3, max_parallel: 2 }, running: 0, queued: 0 })

async function visibleActions(page, panelSelector, label) {
  const result = await page.evaluate(({ panelSelector }) => {
    const panel = document.querySelector(panelSelector), card = document.querySelector('.approval-request'), body = document.querySelector('.approval-body')
    const panelRect = panel.getBoundingClientRect(), cardRect = card.getBoundingClientRect()
    const rect = node => { const r = node.getBoundingClientRect(); return { left:r.left, top:r.top, right:r.right, bottom:r.bottom, width:r.width, height:r.height } }
    return { viewport: { width:innerWidth,height:innerHeight }, panel:rect(panel), card:rect(card), body: { ...rect(body), scrollHeight:body.scrollHeight, clientHeight:body.clientHeight, scrollTop:body.scrollTop }, buttons: [...card.querySelectorAll('.approval-actions button')].map(button => { const r = button.getBoundingClientRect(), target = document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return { text:button.textContent, ...rect(button), hit:button === target || button.contains(target), inPanel:r.top>=panelRect.top-1&&r.bottom<=panelRect.bottom+1&&r.left>=panelRect.left-1&&r.right<=panelRect.right+1, inCard:r.top>=cardRect.top-1&&r.bottom<=cardRect.bottom+1, inViewport:r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth } }), overflow:document.documentElement.scrollWidth>innerWidth }
  }, { panelSelector })
  assert.equal(result.buttons.length, 2, `${label}: both actions exist`)
  for (const button of result.buttons) assert.ok(button.inPanel && button.inCard && button.inViewport && button.hit, `${label}: ${button.text} visible and clickable: ${JSON.stringify(result)}`)
  assert.equal(result.overflow, false, `${label}: no horizontal overflow`)
  assert.ok(result.body.clientHeight > 30 && result.body.scrollHeight > result.body.clientHeight, `${label}: scrollable approval body has usable height`)
  measurements.push({ label, ...result })
  return result
}

try {
  const context = await browser.newContext({ viewport: { width:1460,height:900 } })
  await context.addInitScript(({ history }) => {
    window.fixtureSockets = []; window.fixtureEvents = []; window.fixtureSeq = 20; window.fixturePrompts = [];window.overlayDecisions = []
    window.emitFixtureEvent = (sessionId,type,payload) => {
      const event = { seq:++window.fixtureSeq,type,payload };window.fixtureEvents.push({sessionId,...event})
      for(const socket of window.fixtureSockets) if(socket.sessionId===sessionId&&socket.readyState===1) socket.onmessage?.({data:JSON.stringify(event)})
    }
    window.WebSocket = class {
      static OPEN = 1
      constructor(url){this.url=url;this.sessionId=new URL(url).pathname.split('/')[4];this.readyState=1;window.fixtureSockets.push(this);queueMicrotask(()=>{this.onopen?.({});for(const event of history)this.onmessage?.({data:JSON.stringify(event)});this.onmessage?.({data:JSON.stringify({type:'session_meta',payload:{busy:false,model:'fixture-offline'}})})})}
      send(raw){window.fixturePrompts.push(JSON.parse(raw))}
      close(){this.readyState=3}
    }
  }, { history:initialHistory })
  const page = await context.newPage()
  page.on('pageerror', cause=>errors.push(cause.message))
  page.on('dialog', dialog=>{unexpected.push('native dialog');void dialog.dismiss()})
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if(url.origin!==origin){unexpected.push(url.origin+url.pathname);return route.abort()}
    if(url.pathname==='/approval-layout-fixture')return route.fulfill({contentType:'text/html',body:fixture})
    if(url.pathname==='/approval-overlay-fixture')return route.fulfill({contentType:'text/html',body:overlayFixture})
    if(url.pathname==='/fixture-chat-approval.js')return route.fulfill({contentType:'text/javascript',body:chatScript})
    if(!url.pathname.startsWith('/api/'))return route.continue()
    const respond = data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)})
    if(/\/events$/.test(url.pathname))return respond(initialHistory)
    if(url.pathname==='/api/v1/web')return respond({available:true,reason:'',model:'fixture'})
    if(/\/checkpoints$/.test(url.pathname))return respond({busy:false})
    if(/\/approvals\/pending$/.test(url.pathname))return respond(null)
    if(/^\/api\/v1\/approvals\/[^/]+\/decide$/.test(url.pathname)&&request.method()==='POST'){
      writes.push({path:url.pathname,body:request.postDataJSON()})
      await new Promise(resolve=>{releaseDecision=resolve})
      return respond({ok:true,delivered:true})
    }
    unexpected.push(request.method()+' '+url.pathname);return route.fulfill({status:500,body:'{}'})
  })
  await page.goto(`${base}/approval-layout-fixture`, {waitUntil:'networkidle'})
  const composer = page.getByRole('textbox',{name:'向代码助手发送消息',includeHidden:true})
  await composer.fill('审批结束后继续检查界面；这份未发送草稿应完整保留。')
  await page.evaluate(team=>window.fixtureSetTeam(team),teamFor(null))
  await page.evaluate(({approval,team})=>{window.fixtureSetTeam(team);window.emitFixtureEvent('fixture-worker','approval_request',approval)}, {approval:longApproval('fixture-approve'),team:teamFor(longApproval('fixture-approve'))})
  await page.getByRole('region',{name:'操作权限申请'}).waitFor()
  assert.equal(await composer.isVisible(),false)
  assert.equal(await composer.inputValue(),'审批结束后继续检查界面；这份未发送草稿应完整保留。')
  checks.push('approval temporarily hides composer while retaining its unsent draft and DOM')

  for(const viewport of [{width:1460,height:900},{width:1440,height:520},{width:390,height:600},{width:320,height:760}]){
    await page.setViewportSize(viewport)
    const label=`AiPanel ${viewport.width}x${viewport.height}`
    await page.locator('.approval-body').evaluate(node=>node.scrollTop=0)
    const before=await visibleActions(page,'.ai',label)
    await page.locator('.approval-body').evaluate(node=>node.scrollTop=node.scrollHeight)
    const after=await visibleActions(page,'.ai',label+' scrolled')
    assert.ok(after.body.scrollTop>0)
    assert.ok(Math.abs(after.body.scrollTop+after.body.clientHeight-after.body.scrollHeight)<=2)
    for(let index=0;index<2;index++)assert.ok(Math.abs(before.buttons[index].top-after.buttons[index].top)<=1,`${label}: body scroll preserves action position`)
    const tail=await page.locator('.approval-body pre').last().evaluate(node=>({rect:node.getBoundingClientRect().toJSON(),text:node.textContent,body:node.closest('.approval-body').getBoundingClientRect().toJSON()}))
    assert.match(tail.text,/ARGS_END/)
    assert.ok(tail.rect.bottom<=tail.body.bottom+2,`${label}: final arguments can be read after scrolling`)
    assert.ok(await page.locator('.ai-meta').evaluate(node=>node.clientHeight<node.scrollHeight),`${label}: long team metadata has independent scrolling`)
    await page.screenshot({path:`${output}/approval-${viewport.width}x${viewport.height}.png`})
    checks.push(`${label}: actions remain within panel/viewport and clickable; full long command/arguments scroll without moving footer`)
  }

  await page.evaluate(()=>window.emitFixtureEvent('fixture-worker','error',{message:'提交失败的详细提示：'.repeat(100)}))
  for(const viewport of [{width:1440,height:520},{width:390,height:600}]){
    await page.setViewportSize(viewport)
    const label=`AiPanel long banner ${viewport.width}x${viewport.height}`
    const before=await visibleActions(page,'.ai',label)
    assert.ok(await page.locator('.banner').evaluate(node=>node.clientHeight<node.scrollHeight),`${label}: error banner is independently scrollable`)
    await page.locator('.ai-meta').evaluate(node=>node.scrollTop=node.scrollHeight)
    const after=await visibleActions(page,'.ai',label+' metadata scrolled')
    for(let index=0;index<2;index++)assert.ok(Math.abs(before.buttons[index].top-after.buttons[index].top)<=1)
    checks.push(`${label}: long error/banner and scrolling team metadata do not displace approval actions`)
  }

  await page.setViewportSize({width:1440,height:520})
  await page.locator('.ai-meta').evaluate(node=>node.scrollTop=0)
  await page.getByRole('button',{name:'选择项目智能体'}).click()
  await page.locator('[data-agent-session="fixture-lead"]').click()
  await composer.waitFor({state:'visible'})
  assert.equal(await composer.inputValue(),'')
  await composer.fill('Lead 的独立草稿。')
  await page.getByRole('button',{name:'选择项目智能体'}).click()
  await page.locator('[data-agent-session="fixture-worker"]').click()
  await page.waitForFunction(()=>window.fixtureSockets.some(socket=>socket.sessionId==='fixture-worker'&&socket.readyState===1))
  await page.waitForFunction(()=>document.querySelector('textarea')?.value==='审批结束后继续检查界面；这份未发送草稿应完整保留。')
  await page.evaluate(({approval,team})=>{window.fixtureSetTeam(team);window.emitFixtureEvent('fixture-worker','approval_request',approval)}, {approval:longApproval('fixture-approve'),team:teamFor(longApproval('fixture-approve'))})
  await page.getByRole('region',{name:'操作权限申请'}).waitFor()
  await visibleActions(page,'.ai','dropdown switch back to worker')
  checks.push('short-viewport agent dropdown switches to Lead and worker without losing each session draft')

  await page.getByRole('button',{name:'仅批准本次',exact:true}).click()
  await page.waitForFunction(()=>document.querySelector('.approval-actions button')?.disabled)
  await page.locator('.approval-actions button').evaluateAll(buttons=>buttons.forEach(button=>button.click()))
  assert.equal(writes.length,1)
  assert.deepEqual(writes[0],{path:'/api/v1/approvals/fixture-approve/decide',body:{approved:true}})
  assert.equal(await page.locator('.approval-actions button:disabled').count(),2)
  releaseDecision();releaseDecision=null
  await composer.waitFor({state:'visible'})
  assert.equal(await composer.inputValue(),'审批结束后继续检查界面；这份未发送草稿应完整保留。')
  checks.push('approve sends only fixture approval ID; delayed submission disables both actions and blocks duplicate decision; draft restores')

  await page.evaluate(({approval,team})=>{window.fixtureSetTeam(team);window.emitFixtureEvent('fixture-worker','approval_request',approval)}, {approval:longApproval('fixture-reject'),team:teamFor(longApproval('fixture-reject'))})
  await page.getByRole('button',{name:'拒绝',exact:true}).click()
  await page.waitForFunction(()=>document.querySelector('.approval-actions button')?.disabled)
  assert.equal(writes.length,2)
  assert.deepEqual(writes[1],{path:'/api/v1/approvals/fixture-reject/decide',body:{approved:false}})
  releaseDecision();releaseDecision=null
  await composer.waitFor({state:'visible'})
  assert.equal(await composer.inputValue(),'审批结束后继续检查界面；这份未发送草稿应完整保留。')
  assert.deepEqual(await page.evaluate(()=>window.fixturePrompts),[])
  checks.push('reject uses current fixture approval ID and restores draft; no prompts or shell commands sent')

  for(const viewport of [{width:1440,height:520},{width:390,height:420}]){
    await page.setViewportSize(viewport)
    await page.goto(`${base}/approval-overlay-fixture`,{waitUntil:'networkidle'})
    const label=`independent chat overlay ${viewport.width}x${viewport.height}`
    const before=await visibleActions(page,'.modal-mask',label)
    await page.locator('.approval-body').evaluate(node=>node.scrollTop=node.scrollHeight)
    const after=await visibleActions(page,'.modal-mask',label+' scrolled')
    assert.ok(after.body.scrollTop>0)
    for(let index=0;index<2;index++)assert.ok(Math.abs(before.buttons[index].top-after.buttons[index].top)<=1)
    await page.getByRole('button',{name:'拒绝',exact:true}).click()
    assert.deepEqual(await page.evaluate(()=>window.overlayDecisions),[false])
    await page.screenshot({path:`${output}/chat-overlay-${viewport.width}x${viewport.height}.png`})
    checks.push(`${label}: actual chat component and modal styling keep scrollable body and usable actions`)
  }
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[])
  checks.push('no runtime errors, native dialogs, external requests or unstubbed API calls')
} finally {
  releaseDecision?.()
  await fs.writeFile(`${output}/report.json`,JSON.stringify({checks,errors,unexpected,writes,measurements},null,2))
  await browser.close()
}
console.log(JSON.stringify({passed:checks.length,output},null,2))
