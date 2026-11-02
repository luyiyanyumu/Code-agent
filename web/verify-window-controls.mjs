// Component DOM checks only; native moving/resizing is verified by the desktop host.
import assert from 'node:assert/strict'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'

const web = dirname(fileURLToPath(import.meta.url)), out = resolve(process.argv[2] || join(web, '../.tmp/integrated-titlebar-component'))
await mkdir(out, { recursive: true })
const codeSource = await readFile(join(web, 'src/views/CodeView.vue'), 'utf8')
const header = codeSource.match(/<header class="ide-titlebar"[\s\S]*?<\/header>/)?.[0]
assert.ok(header, 'Use the real workbench header markup')
const css = []
const fixture = `<script setup>
import {ref} from 'vue';import AgentLogo from './src/components/ui/AgentLogo.vue';import UiIcon from './src/components/ui/UiIcon.vue';import IdeMenuBar from './src/components/code/IdeMenuBar.vue';import DesktopWindowControls from './src/components/DesktopWindowControls.vue';import {desktopRuntime} from './src/lib/desktopRuntime.js';
const integratedTitlebar=desktopRuntime.integratedTitlebar();const project=ref({name:'long-workspace-project-name',root_path:'C:/fixtures/project'});const ideMenus=['文件','编辑','导航','视图','运行','帮助'].map((label,i)=>({id:String(i),label,items:[{id:'example',label:'示例操作'}]}));function onIdeCommand(value){window.__commands.push(value)}function openWorkspaceSearch(){window.__searched=true}
<\/script><template><main class="code">${header}<p class="fixture-body">原生窗口控件组件验证</p><DesktopWindowControls/></main></template><style scoped>${parse(codeSource).descriptor.styles.map(style=>style.content).join('\n')}</style>`
const bundled = await build({ write: false, bundle: true, format: 'iife', platform: 'browser', absWorkingDir: web,
  stdin: { resolveDir: web, sourcefile: 'window-controls-entry.js', contents: "import {createApp} from 'vue';import Fixture from 'virtual:titlebar';window.__app=createApp(Fixture).mount('#app');" },
  define: { __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
  plugins: [{ name: 'titlebar-components', setup(tool) {
    tool.onResolve({ filter: /^virtual:titlebar$/ }, () => ({ path: 'virtual-titlebar.vue', namespace: 'fixture' }))
    async function compile(source, path) {
      const { descriptor, errors } = parse(source, { filename: path }); assert.deepEqual(errors, [])
      const script = compileScript(descriptor, { id: 'titlebar-dom', inlineTemplate: true })
      for (const style of descriptor.styles) { const result = compileStyle({ source: style.content, filename: path, id: 'data-v-titlebar-dom', scoped: true }); assert.deepEqual(result.errors, []); css.push(result.code) }
      return { contents: script.content.replace('export default', 'const __component__ =') + "\n__component__.__scopeId='data-v-titlebar-dom';export default __component__;", loader: 'js', resolveDir: web }
    }
    tool.onLoad({ filter: /.*/, namespace: 'fixture' }, args => compile(fixture, args.path))
    tool.onLoad({ filter: /\.vue$/ }, async args => ({ ...(await compile(await readFile(args.path, 'utf8'), args.path)), resolveDir: dirname(args.path) }))
  } }],
})
const sharedCss = await readFile(join(web, 'src/style.css'), 'utf8')
const server = createServer(async (request, response) => {
  const path = new URL(request.url, 'http://localhost').pathname
  if (path === '/app.js') { response.setHeader('Content-Type','text/javascript'); response.end(bundled.outputFiles[0].text); return }
  if (path === '/style.css') { response.setHeader('Content-Type','text/css'); response.end(sharedCss + css.join('\n') + '\nhtml,body,#app{height:100%;margin:0}.fixture-body{padding:20px;color:var(--muted)}'); return }
  if (path === '/brand/code-agent-mark.svg') { response.setHeader('Content-Type','image/svg+xml'); response.end(await readFile(join(web,'public/brand/code-agent-mark.svg'))); return }
  response.setHeader('Content-Type','text/html'); response.end('<html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"></head><body><div id="app"></div><script src="/app.js"></script></body></html>')
})
await new Promise(done=>server.listen(0,'127.0.0.1',done))
const base=`http://127.0.0.1:${server.address().port}`
const pw=await import(process.env.PLAYWRIGHT_CORE_PATH||'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const browser=await (pw.default||pw).chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true})
const context=await browser.newContext(), page=await context.newPage(), report={passed:false,checks:[],errors:[],screenshots:[]}
page.on('pageerror',error=>report.errors.push(error.message))
await context.addInitScript(()=>{
  window.__commands=[];window.__calls=[];window.__searched=false;window.__nativeState={maximized:false,minimized:false,active:true,integratedTitlebar:true}
  window.__CODE_AGENT_DESKTOP__={integrated:true,integratedTitlebar:true}
  const listeners=new Set();window.__sendNative=value=>{for(const listener of listeners)listener({data:{channel:'code-agent-desktop',...value}})}
  window.chrome={webview:{addEventListener:(_,listener)=>listeners.add(listener),removeEventListener:(_,listener)=>listeners.delete(listener),postMessage:message=>{
    window.__calls.push(message)
    if(message.method==='window-toggle-maximize')window.__nativeState.maximized=!window.__nativeState.maximized
    queueMicrotask(()=>window.__sendNative({id:message.id,ok:true,result:message.method==='window-close'?{accepted:true}:window.__nativeState}))
  }}}
})
try{
  await page.setViewportSize({width:1440,height:900});await page.goto(base);await page.getByRole('group',{name:'窗口控制'}).waitFor()
  await page.getByRole('button',{name:'最大化窗口',exact:true}).click();await page.getByRole('button',{name:'还原窗口',exact:true}).waitFor()
  await page.evaluate(()=>window.__sendNative({event:'window-state',state:{maximized:false,minimized:false,active:false,integratedTitlebar:true}}));await page.getByRole('button',{name:'最大化窗口',exact:true}).waitFor()
  await page.getByRole('button',{name:'最小化窗口',exact:true}).click();await page.getByRole('button',{name:'关闭窗口',exact:true}).click()
  assert.deepEqual(await page.evaluate(()=>window.__calls.map(call=>call.method)),['window-state','window-toggle-maximize','window-minimize','window-close'])
  report.checks.push('Window buttons call only their native methods and track native maximize/restore updates')
  await page.getByRole('menuitem',{name:'文件',exact:true}).click();await page.getByRole('menuitem',{name:'示例操作',exact:true}).waitFor();await page.keyboard.press('Escape')
  await page.getByRole('button',{name:'搜索文件或代码',exact:true}).click();assert.equal(await page.evaluate(()=>window.__searched),true)
  assert.equal(await page.evaluate(()=>window.__calls.some(call=>call.method==='window-drag')),false)
  report.checks.push('Menus, keyboard Escape and search retain normal interaction and never invoke manual window dragging')
  for(const width of [1440,960,768,390]){
    await page.setViewportSize({width,height:900})
    const geometry=await page.evaluate(()=>{const c=document.querySelector('.desktop-window-controls').getBoundingClientRect(),h=document.querySelector('.ide-titlebar').getBoundingClientRect(),search=document.querySelector('.quick-trigger').getBoundingClientRect();return{controls:{left:c.left,right:c.right,top:c.top,height:c.height},header:{right:h.right,height:h.height},searchRight:search.right,scrollWidth:document.documentElement.scrollWidth}})
    assert.ok(geometry.controls.left>=0&&geometry.controls.right<=width+.5);assert.equal(geometry.header.height,35);assert.ok(geometry.searchRight<=geometry.controls.left,'Search must not overlap caption controls');assert.ok(geometry.scrollWidth<=width)
    const path=join(out,`titlebar-component-${width}.png`);await page.screenshot({path});report.screenshots.push(path)
  }
  report.checks.push('Single 35px header and all three window actions remain visible without overlap at 1440, 960, 768 and 390 CSS px')
  await context.addInitScript(()=>{delete window.__CODE_AGENT_DESKTOP__;delete window.chrome})
  await page.reload();assert.equal(await page.getByRole('group',{name:'窗口控制'}).count(),0);assert.equal(await page.locator('.ide-titlebar.integrated-titlebar').count(),0)
  report.checks.push('Ordinary browser mode retains its header and does not render desktop caption controls')
  assert.deepEqual(report.errors,[]);report.passed=true
}catch(error){report.error=error.stack||String(error);throw error}
finally{await writeFile(join(out,'report.json'),JSON.stringify(report,null,2));await browser.close();await new Promise(done=>server.close(done));console.log(JSON.stringify(report,null,2))}
