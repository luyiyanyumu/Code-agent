// Real renderer + production design endpoints, limited to throwaway projects.
// The fixture server never creates a model runtime, session or real database.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { build, createServer } from 'vite'
import { build as bundleHelper } from 'esbuild'
import vue from '@vitejs/plugin-vue'
import { parseDesignCss, serializeDesignCss } from './src/lib/frontendDesign.js'

const webRoot = path.dirname(fileURLToPath(import.meta.url))
const repository = path.dirname(webRoot)
const output = path.resolve(process.argv[2] || path.join(webRoot, 'shots/frontend-design'))
const uiOrigin = 'http://127.0.0.1:5298', backendOrigin = 'http://127.0.0.1:5296', vueOrigin = 'http://127.0.0.1:5199'
const routerModule = process.env.VUE_ROUTER_FIXTURE_PATH || 'C:/Users/dyh/WorkBuddy/2026-09-07-15-46-17/learn-hub/frontend/node_modules/vue-router/dist/vue-router.js'
const axiosModule = process.env.AXIOS_FIXTURE_PATH || 'C:/Users/dyh/WorkBuddy/2026-09-07-15-46-17/learn-hub/frontend/node_modules/axios/dist/esm/axios.js'
const apiOrigin = 'http://127.0.0.1:5337/api/'
const checks = [], errors = [], unexpected = [], calls = [], consoleErrors = [], moduleImports = []
let diagnostics = null
let directory, backend, uiVite, projectVite, browser, failure
let backendLog = ''
let fixtureHtml = ''
let savedPageHtml = ''
let minifiedHelperLoads = 0, minifiedHelperObserved = false, minifiedHelperBytes = 0, vite8PreloadPatchCount = 0, vite8PreloadProbe = null
let apiBoundaryStats = null
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
const redactFixtureCapabilities = value => value.replace(/frontend-design-(assets|api)\/[A-Za-z0-9_-]+/g, 'frontend-design-$1/[fixture-ticket]')

async function waitForServer(url, child = null) {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (child?.exitCode !== null && child?.exitCode !== undefined) throw new Error(`Private fixture server exited: ${backendLog.slice(-1800)}`)
    try { if ((await fetch(url, { signal: AbortSignal.timeout(700) })).ok) return } catch {}
    await delay(200)
  }
  throw new Error(`Private fixture server did not become ready: ${url}`)
}

const staticPage = label => `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${label}</title><link rel="stylesheet" href="./style.css"></head><body>
<main><p class="eyebrow">FRONTEND DESIGN FIXTURE</p><h1>${label}</h1><p>这里是可交互的纯前端测试页面。选中按钮后，可修改颜色、文字和布局。</p><section class="card"><h2>按钮交互</h2><button id="counter-btn" type="button">点击计数</button><span id="count">0</span></section><section class="card"><h2>表单验收</h2><form id="sample-form"><label>测试名称<input id="name-field" required placeholder="输入虚构名称"></label><button id="submit-btn" type="submit">提交表单</button></form><p id="submitted">尚未提交</p></section><p><a id="detail-link" href="./zzz-detail.html">查看详情</a> · <a id="home-link" href="./index.html">返回首页</a> · <a id="anchor-link" href="#sample-form">定位表单</a></p></main><script src="./app.js"></script></body></html>`
const staticCss = `*{box-sizing:border-box}body{margin:0;background:#f8fafc;color:#0f172a;font:15px/1.7 system-ui,sans-serif}main{max-width:850px;margin:auto;padding:42px 34px}.eyebrow{font:11px/1.7 monospace;letter-spacing:2px;color:#64748b}h1{font-size:32px;margin:8px 0 12px}h2{font-size:19px;margin:0 0 15px}.card{padding:24px;margin-top:24px;background:#fff;border:1px solid #e2e8f0;border-radius:12px}button{padding:10px 18px;background:#e2e8f0;border:1px solid #cbd5e1;border-radius:6px;color:#0f172a;font:600 14px system-ui;cursor:pointer}#count{margin-left:20px;font-weight:700}form{display:flex;gap:12px;align-items:end;flex-wrap:wrap}label{display:flex;flex-direction:column;gap:6px}input{padding:9px;border:1px solid #cbd5e1;border-radius:6px}#submitted{color:#047857}@media(max-width:500px){main{padding:22px 18px}h1{font-size:25px}.card{padding:18px}}`
const staticJs = `let count=0;document.getElementById('counter-btn').addEventListener('click',()=>document.getElementById('count').textContent=String(++count));document.getElementById('sample-form').addEventListener('submit',event=>{event.preventDefault();document.getElementById('submitted').textContent='已提交：'+document.getElementById('name-field').value});`

await fs.mkdir(output, { recursive: true })
try {
  await fs.access(routerModule)
  await fs.access(axiosModule)
  const helperPath = path.join(webRoot, 'src/lib/frontendDesign.js')
  const helperBundle = await bundleHelper({ entryPoints: [helperPath], bundle: true, minify: true, write: false, format: 'esm', target: 'es2022', platform: 'browser', logLevel: 'silent' })
  const minifiedHelper = helperBundle.outputFiles[0].text
  minifiedHelperBytes = Buffer.byteLength(minifiedHelper)
  assert.ok(minifiedHelperBytes > 1000 && !/^\s*import(?:\s|["'])/m.test(minifiedHelper), 'the actual parent helper is bundled and compressed before browser use')
  directory = await fs.mkdtemp(path.join(os.tmpdir(), 'code-agent-frontend-design-'))
  const projectA = path.join(directory, 'projects/fixture-a'), projectB = path.join(directory, 'projects/fixture-b')
  const staticA = path.join(projectA, 'static'), staticB = path.join(projectB, 'static'), vueRoot = path.join(projectA, 'vue')
  await Promise.all([fs.mkdir(staticA, { recursive: true }), fs.mkdir(staticB, { recursive: true }), fs.mkdir(path.join(vueRoot, 'src/api'), { recursive: true })])
  await Promise.all([
    fs.writeFile(path.join(staticA, 'index.html'), staticPage('模拟产品 A').replace('</main>', '<p><a id="resource-error-link" href="./zzz-resource-errors.html">查看缺失资源诊断</a></p></main>')),
    fs.writeFile(path.join(staticA, 'zzz-detail.html'), staticPage('模拟产品 A · 详情')),
    fs.writeFile(path.join(staticA, 'zzz-resource-errors.html'), staticPage('模拟产品 A · 资源诊断').replace('</main>', '<img alt="预期缺失的虚构标记" src="./missing-logo.png?access_token=fixture-secret#private-fragment"><img alt="预期缺失的虚构横幅" src="./missing-banner.png?signature=fixture-secret#private-fragment"></main>')),
    fs.writeFile(path.join(staticB, 'index.html'), staticPage('模拟产品 B')),
    ...[staticA, staticB].flatMap(root => [fs.writeFile(path.join(root, 'style.css'), staticCss), fs.writeFile(path.join(root, 'app.js'), staticJs)]),
    fs.writeFile(path.join(vueRoot, 'index.html'), '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>模拟 Vue 产品</title></head><body><div id="app"></div><script type="module" src="/main.js"></script></body></html>'),
    fs.writeFile(path.join(vueRoot, 'package.json'), JSON.stringify({ name: 'private-vue-design-fixture', private: true, type: 'module', scripts: { dev: 'vite' } })),
    fs.writeFile(path.join(vueRoot, 'vite.config.js'), 'export default { server: { port: 5199, strictPort: true, proxy: { "/api": { target: "http://127.0.0.1:5337" } } } }\n'),
    fs.writeFile(path.join(vueRoot, 'main.js'), `import{createApp,h}from'vue';import{createRouter,createWebHistory}from'vue-router';import App from'./App.vue';window.fixtureStorageSnapshot={localBefore:localStorage.getItem('opaque-local'),sessionBefore:sessionStorage.getItem('opaque-session'),parentLocal:localStorage.getItem('parent-storage-sentinel'),parentSession:sessionStorage.getItem('parent-storage-sentinel')};localStorage.setItem('opaque-local','fixture-local');sessionStorage.setItem('opaque-session','fixture-session');window.fixtureStorageSnapshot.localAfter=localStorage.getItem('opaque-local');window.fixtureStorageSnapshot.sessionAfter=sessionStorage.getItem('opaque-session');const router=createRouter({history:createWebHistory(),routes:[{path:'/',component:{render:()=>h('p',{id:'route-home'},'首页路由可用')}},{path:'/details',component:()=>import('./DetailRoute.vue')}]});createApp(App).use(router).mount('#app');`),
    fs.writeFile(path.join(vueRoot, 'DetailRoute.vue'), `<template><p id="route-details">详情路由可用</p></template><style scoped>#route-details{border-top:3px solid #355dfb;padding-top:8px}</style>`),
    fs.writeFile(path.join(vueRoot, 'src/api/demo.js'), `export const demoLabel='公开前端 API 模块已加载';\n`),
    fs.writeFile(path.join(vueRoot, 'App.vue'), `<script setup>import{ref}from'vue';import{RouterLink,RouterView}from'vue-router';import axios from'axios';axios.interceptors.response.use(response=>response,error=>{const originalName=error.name;error.message='接口读取失败：'+error.message;window.fixtureAxiosErrorRewrite={rewritten:error.message.startsWith('接口读取失败：'),namePreserved:error.name===originalName};return Promise.reject(error)});import logo from'./logo.svg';import{demoLabel}from'./src/api/demo.js';const count=ref(0),apiState=ref('idle'),apiCode=ref(0),apiResult=ref(''),apiError=ref('');function goBack(){window.history.back()}function goForward(){window.history.forward()}async function requestApi(kind){window.fixtureApiTrace={kind,phase:"started"};apiState.value='loading';apiError.value='';apiResult.value='';apiCode.value=0;try{let response,data;if(kind==='axios'){response=await axios.get('/api/demo',{params:{tag:'axios',note:'a/b'},withCredentials:true,withXSRFToken:true,headers:{Authorization:'Bearer fixture-test-credential'}});data=response.data}else if(kind==='head'){response=await axios.head('/api/demo?tag=head',{withCredentials:true,withXSRFToken:true});data=response.data}else{const write=kind==='post'||kind==='put';response=await fetch(kind==='status'?'/api/status?tag=status':'/api/demo?tag='+kind+'&note=a%2Fb',{method:write?kind.toUpperCase():'GET',credentials:'include',headers:write?{'Content-Type':'application/json'}:{Authorization:'Bearer fixture-test-credential'},...(write?{body:JSON.stringify({fixture:true})}:{})});if(write&&!response.ok)throw new Error('写入被拒绝 '+response.status);data=await response.json()}apiCode.value=response.status;apiResult.value=JSON.stringify(data);apiState.value='done';window.fixtureApiTrace.phase='done'}catch(error){apiCode.value=error.response?.status||0;apiError.value=String(error.message||'请求失败');apiState.value='failed';window.fixtureApiTrace.phase='failed'}}</script><template><main><p>REAL VUE MODULE FIXTURE</p><img id="vue-logo" :src="logo" width="80" height="60" alt="虚构产品标记"><h1>真实 Vue 模块页面</h1><p>由独立 Vite 编译，并经设计资源代理载入。</p><p id="vue-api-module">{{demoLabel}}</p><button id="vue-counter" @click="count++">Vue 计数 {{count}}</button><p id="vue-value">{{count}}</p><nav><RouterLink id="route-home-link" to="/">首页</RouterLink> · <RouterLink id="route-details-link" to="/details">详情</RouterLink></nav><RouterView/><p><button id="route-back" @click="goBack">路由后退</button> <button id="route-forward" @click="goForward">路由前进</button></p><section><h2>虚构只读接口</h2><p><button id="api-axios" @click="requestApi('axios')">Axios GET</button> <button id="api-fetch" @click="requestApi('fetch')">Fetch GET</button> <button id="api-head" @click="requestApi('head')">Axios HEAD</button> <button id="api-status" @click="requestApi('status')">Fetch 状态</button> <button id="api-post" @click="requestApi('post')">拒绝 POST</button> <button id="api-put" @click="requestApi('put')">拒绝 PUT</button></p><p id="api-state">{{apiState}}</p><p id="api-code">{{apiCode}}</p><pre id="api-result">{{apiResult}}</pre><p id="api-error">{{apiError}}</p></section></main></template><style>body{margin:0;background:#f8fafc;color:#0f172a;font:16px/1.7 system-ui}main{padding:45px 34px}h1{font-size:30px}button{background:#e2e8f0;color:#0f172a;padding:14px 24px;border:1px solid #cbd5e1;border-radius:8px;font:600 16px system-ui;cursor:pointer}#api-result{white-space:pre-wrap}</style>`),
    fs.writeFile(path.join(vueRoot, 'logo.svg'), '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60"><rect width="80" height="60" rx="12" fill="#355dfb"/><path d="M18 30 32 44 62 16" fill="none" stroke="white" stroke-width="6"/></svg>'),
  ])
  const projectAliases = { vue: path.join(webRoot, 'node_modules/vue/dist/vue.runtime.esm-bundler.js'), 'vue-router': routerModule, axios: axiosModule }
  await build({ configFile: false, root: vueRoot, plugins: [vue()], resolve: { alias: projectAliases }, build: { outDir: 'dist', emptyOutDir: true }, logLevel: 'warn' })
  const builtAssets = path.join(vueRoot, 'dist/assets')
  for (const filename of await fs.readdir(builtAssets)) {
    if (!filename.endsWith('.js')) continue
    const assetPath = path.join(builtAssets, filename), source = await fs.readFile(assetPath, 'utf8')
    // Vite 8 resolves preload dependency URLs before assigning link.href. Adapt
    // only this throwaway Vite 6 output to exercise the same-origin absolute URL
    // case that the user's existing production bundle uses.
    let count = 0
    const patched = source.replace(/(\b([A-Za-z_$][\w$]*)\.crossOrigin=(?:""|'')\s*[,;]\s*\2\.href=)([A-Za-z_$][\w$]*)/g, (_, prefix, elementName, value) => { count++; return prefix + `__privateVite8ResolvePreload(${value})` })
    if (!count) continue
    vite8PreloadPatchCount += count
    const resolvePreload = `function __privateVite8ResolvePreload(value){const hasResolve=typeof import.meta.resolve==='function';const resolved=hasResolve?import.meta.resolve(value):new URL(value,import.meta.url).href;const url=new URL(resolved);window.__privateVite8PreloadProbe={calls:(window.__privateVite8PreloadProbe?.calls||0)+1,absolute:url.protocol==='http:'||url.protocol==='https:',outsideCapability:!url.pathname.includes('/api/v1/code/frontend-design-assets/'),assetPath:url.pathname.startsWith('/assets/'),usedImportMetaResolve:hasResolve};return resolved}\n`
    await fs.writeFile(assetPath, resolvePreload + patched)
  }
  assert.ok(vite8PreloadPatchCount > 0, 'the private production bundle contains a Vite 8 style absolute preload resolver')
  const backendScript = path.join(repository, 'tests/fixtures/frontend_design_server.py')
  for (let attempt = 0; attempt < 100; attempt++) {
    try { await fs.access(backendScript); break } catch { if (attempt === 99) throw new Error('Private design server fixture is missing'); await delay(300) }
  }
  backend = spawn(path.join(repository, '.venv/Scripts/python.exe'), [backendScript, '--root', directory, '--port', '5296'], { cwd: repository, env: { ...process.env, PYTHONPATH: repository }, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  backend.stdout.on('data', data => { backendLog = (backendLog + data.toString()).slice(-12000) })
  backend.stderr.on('data', data => { backendLog = (backendLog + data.toString()).slice(-12000) })
  backend.on('error', error => { backendLog += error.message })
  await waitForServer(`${backendOrigin}/healthz`, backend)
  projectVite = await createServer({ configFile: false, root: vueRoot, plugins: [vue()], cacheDir: path.join(vueRoot, 'node_modules/.vite'),
    resolve: { alias: projectAliases },
    server: { host: '127.0.0.1', port: 5199, strictPort: true, hmr: false, fs: { allow: [vueRoot, webRoot, path.dirname(routerModule), path.dirname(axiosModule)] } },
    optimizeDeps: { include: ['vue', 'vue-router', 'axios'] }, logLevel: 'warn' })
  await projectVite.listen()
  uiVite = await createServer({ configFile: false, root: webRoot, plugins: [{ name: 'private-minified-parent-design-helper', enforce: 'pre', load(id) {
    if (path.resolve(id.split('?')[0]) !== path.resolve(helperPath)) return null
    minifiedHelperLoads++
    return { code: minifiedHelper, map: null }
  } }, vue(), { name: 'private-design-fixture-document', configureServer(server) {
    server.middlewares.use((request, response, next) => {
      const routePath = request.url?.split('?')[0]
      if (!['/__frontend-design-fixture', '/__saved-design-page'].includes(routePath)) return next()
      response.statusCode = 200; response.setHeader('Content-Type', 'text/html; charset=utf-8'); response.end(routePath === '/__saved-design-page' ? savedPageHtml : fixtureHtml)
    })
  } }], cacheDir: path.join(directory, 'ui-cache'),
    server: { host: '127.0.0.1', port: 5298, strictPort: true, hmr: false, proxy: { '/api': { target: backendOrigin } } },
    optimizeDeps: { include: ['vue'] }, logLevel: 'warn' })
  await uiVite.listen()
  const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  const { chromium } = loaded.default || loaded
  const source = await (await fetch(`${uiOrigin}/src/components/code/FrontendDesign.vue`)).text()
  const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vuePath, 'isolated UI compiler serves Vue')
  const fixture = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body,#fixture{height:100%;width:100%;margin:0;overflow:hidden}</style></head><body><div id="fixture"></div><script type="module">
import{createApp,h,ref}from'${vuePath}';import Design from'/src/components/code/FrontendDesign.vue';import'/src/style.css';
const project=ref('fixture-a'),readonly=ref(false),visible=ref(true),dirtyPaths=ref([]),aiResult=ref(null);window.fixtureEvents={ask:[],saved:[],error:[]};window.changeProject=id=>project.value=id;window.setReadonly=value=>readonly.value=value;window.setDirtyPaths=value=>dirtyPaths.value=value;window.setVisible=value=>visible.value=value;window.deliverAi=value=>aiResult.value=value;
localStorage.setItem('parent-storage-sentinel','parent-local');sessionStorage.setItem('parent-storage-sentinel','parent-session');localStorage.setItem('opaque-local','parent-local-value');sessionStorage.setItem('opaque-session','parent-session-value');
let bridgeReadyGate=false;window.setBridgeReadyGate=value=>bridgeReadyGate=value;window.addEventListener('message',event=>{if(bridgeReadyGate&&event.data?.type==='ready')event.stopImmediatePropagation()},true);
createApp({render:()=>h(Design,{projectId:project.value,readonly:readonly.value,visible:visible.value,dirtyPaths:dirtyPaths.value,aiResult:aiResult.value,onAsk:value=>window.fixtureEvents.ask.push(value),onSaved:value=>window.fixtureEvents.saved.push(value),onError:value=>window.fixtureEvents.error.push(value)})}).mount('#fixture');
</script></body></html>`
  fixtureHtml = fixture
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' })
  await context.addCookies([{ name: 'frontend-design-fixture-parent', value: 'private-fixture-cookie', url: uiOrigin }])
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), routePath = url.pathname
    if (![uiOrigin, backendOrigin].includes(url.origin)) { unexpected.push(`${method} ${url.origin}${routePath}`); return route.abort() }
    if (routePath === '/__frontend-design-fixture' && url.origin === uiOrigin) return route.continue()
    if (routePath === '/favicon.ico') return route.fulfill({ status: 204 })
    if (!routePath.startsWith('/api/')) return url.origin === uiOrigin ? route.continue() : route.abort()
    const design = routePath.match(/^\/api\/v1\/code\/projects\/(fixture-a|fixture-b)\/frontend-design(?:\/(preview|styles))?$/)
    const asset = routePath.startsWith('/api/v1/code/frontend-design-assets/') && ['GET', 'HEAD'].includes(method)
    const api = routePath.startsWith('/api/v1/code/frontend-design-api/') && ['GET', 'HEAD', 'OPTIONS', 'POST', 'PUT'].includes(method)
    if (!asset && !api && !(design && ((method === 'GET' && !design[2]) || (method === 'POST' && design[2] === 'preview') || (method === 'PUT' && design[2] === 'styles')))) {
      unexpected.push(`${method} ${routePath}`); return route.abort()
    }
    calls.push({ method, kind: api ? 'api' : asset ? 'asset' : design[2] || 'info', project: design?.[1] || '', path: asset || api ? routePath.split('/').slice(6).join('/') : '' })
    return route.continue()
  })
  const page = await context.newPage()
  page.setDefaultTimeout(15000)
  page.on('pageerror', error => errors.push(redactFixtureCapabilities(error.stack || error.message)))
  page.on('console', event => { if (event.type() === 'error') consoleErrors.push(redactFixtureCapabilities(event.text())) })
  page.on('response', async response => {
    const url = new URL(response.url())
    if (!url.pathname.startsWith('/api/v1/code/frontend-design-assets/') || !/javascript/.test(response.headers()['content-type'] || '')) return
    try {
      const source = await response.text()
      const imports = [...source.slice(0, 12000).matchAll(/\b(?:import|export)\s+(?:(?:[\s\S]{0,500}?)\bfrom\s*)?["']([^"']+)["']/g)].map(match => redactFixtureCapabilities(match[0]).slice(0, 650))
      moduleImports.push({ path: url.pathname.split('/').slice(6).join('/'), imports })
    } catch {}
  })
  page.on('dialog', async dialog => { unexpected.push('native dialog'); await dialog.dismiss() })
  const preview = () => page.frameLocator('iframe[title="前端设计预览"]')
  const waitReady = () => page.waitForFunction(() => document.querySelector('.design-footer')?.textContent.includes('画布已连接') && !document.querySelector('.canvas-loading') && !document.querySelector('.canvas-state.error-state'))
  const waitColor = (selector, color) => preview().locator(selector).evaluate((element, expected) => new Promise((resolve, reject) => {
    let attempts = 100
    const inspect = () => { if (getComputedStyle(element).color === expected) resolve(); else if (--attempts) setTimeout(inspect, 30); else reject(new Error(`Expected ${expected}, received ${getComputedStyle(element).color}`)) }
    inspect()
  }), color)
  const selectedSelector = () => page.locator('.selection-info code').textContent()
  const edit = () => page.getByRole('button', { name: '编辑', exact: true }).click()
  const acceptance = () => page.getByRole('button', { name: '验收', exact: true }).click()
  const screenshot = name => page.screenshot({ path: path.join(output, name + '.png') })
  const save = async () => { await page.getByRole('button', { name: '保存样式', exact: true }).click(); await page.waitForFunction(() => document.querySelector('.design-notice')?.textContent.includes('样式已保存到项目')) }
  const applyColor = async color => { await page.getByRole('textbox', { name: '文字颜色', exact: true }).fill(color); await page.getByRole('textbox', { name: '文字颜色', exact: true }).press('Enter') }
  const verifyPrivateStorage = async () => {
    assert.deepEqual(await preview().locator('body').evaluate(() => window.fixtureStorageSnapshot), { localBefore: null, sessionBefore: null, parentLocal: null, parentSession: null, localAfter: 'fixture-local', sessionAfter: 'fixture-session' })
    assert.deepEqual(await page.evaluate(() => ({ local: localStorage.getItem('parent-storage-sentinel'), session: sessionStorage.getItem('parent-storage-sentinel') })), { local: 'parent-local', session: 'parent-session' })
    assert.equal(await page.locator('iframe').evaluate(element => element.contentDocument), null)
  }
  const verifyRouterHistory = async () => {
    const beforePreview = calls.filter(call => call.kind === 'preview').length
    await preview().locator('#route-home').waitFor()
    await preview().locator('#route-details-link').click(); await preview().locator('#route-details').waitFor()
    assert.equal(await preview().locator('#route-details').evaluate(element => getComputedStyle(element).borderTopWidth), '3px', 'the actual lazy route stylesheet loaded as well as its JavaScript')
    await preview().locator('#route-back').click(); await preview().locator('#route-home').waitFor()
    await preview().locator('#route-forward').click(); await preview().locator('#route-details').waitFor()
    await preview().locator('#route-home-link').click(); await preview().locator('#route-home').waitFor()
    assert.equal(page.url(), uiOrigin + '/__frontend-design-fixture')
    assert.equal(calls.filter(call => call.kind === 'preview').length, beforePreview, 'client router history remains in the bridged opaque iframe')
  }
  const readApiStats = async () => {
    const response = await fetch(backendOrigin + '/__fixture_api_stats')
    assert.equal(response.status, 200)
    return response.json()
  }
  const runApiAction = async (kind, state = 'done') => {
    await acceptance()
    assert.match(await page.locator('.canvas-caption').textContent(), /验收模式/)
    await preview().locator('body').evaluate(() => {
      window.fixtureLastClick = null
      if (!window.fixtureClickProbe) {
        window.fixtureClickProbe = true
        window.addEventListener('click', event => { window.fixtureLastClick = { id: event.target?.id || '', tag: event.target?.localName || '', trusted: event.isTrusted } }, true)
      }
    })
    await preview().locator('#api-' + kind).evaluate(element => element.scrollIntoView({ block: 'center', inline: 'nearest' }))
    await preview().locator('#api-' + kind).click()
    try { await preview().locator('#api-state').filter({ hasText: new RegExp('^' + state + '$') }).waitFor() }
    catch (error) {
      diagnostics.apiFailure = await preview().locator('body').evaluate(() => ({ state: document.getElementById('api-state')?.textContent, code: document.getElementById('api-code')?.textContent, error: document.getElementById('api-error')?.textContent, trace: window.fixtureApiTrace || null, click: window.fixtureLastClick || null }))
      diagnostics.apiFailure.mode = await page.locator('.canvas-caption').textContent()
      await screenshot('frontend-design-api-action-failure')
      throw error
    }
  }

  const minifiedResponsePromise = page.waitForResponse(response => new URL(response.url()).pathname === '/src/lib/frontendDesign.js')
  await page.goto(`${uiOrigin}/__frontend-design-fixture`, { waitUntil: 'networkidle' })
  const minifiedResponse = await minifiedResponsePromise
  minifiedHelperObserved = (await minifiedResponse.text()).includes(minifiedHelper.trim())
  assert.equal(minifiedHelperObserved, true, 'the browser imports the compressed parent helper, whose emitted iframe runtime must therefore survive minification')
  assert.ok(minifiedHelperLoads > 0)
  await waitReady()
  await preview().locator('#counter-btn').waitFor()
  diagnostics = await preview().locator('head').evaluate(element => ({ links: [...element.querySelectorAll('link')].map(item => new URL(item.href).pathname.split('/').slice(6).join('/')), scriptCount: document.querySelectorAll('script[src]').length, scriptPaths: [...document.querySelectorAll('script[src]')].map(item => new URL(item.src).pathname.split('/').slice(6).join('/')), csp: element.querySelector('meta[http-equiv="Content-Security-Policy"]')?.content.replace(/frontend-design-assets\/[A-Za-z0-9_-]+/g, 'frontend-design-assets/[fixture-ticket]') }))
  assert.equal(await page.locator('iframe').getAttribute('sandbox'), 'allow-scripts allow-forms allow-modals')
  assert.equal(await page.locator('iframe').evaluate(element => element.contentDocument), null, 'iframe has an opaque origin')
  assert.equal(await preview().locator('body').evaluate(() => { try { return Boolean(parent.document.body) } catch { return false } }), false)
  assert.equal(calls.filter(call => call.kind === 'styles').length, 0)
  checks.push('loads actual temporary project HTML and proxied assets in an opaque iframe, without writes or application-origin access')
  await screenshot('frontend-design-desktop-acceptance')

  await preview().locator('#detail-link').click()
  await page.waitForFunction(() => document.querySelector('.page-frame-heading')?.textContent.includes('zzz-detail.html'))
  await waitReady()
  assert.equal(await preview().locator('h1').textContent(), '模拟产品 A · 详情')
  await edit(); await preview().locator('#counter-btn').click()
  assert.equal(await selectedSelector(), '#counter-btn')
  assert.equal(await preview().locator('#count').textContent(), '0')
  await acceptance(); await preview().locator('#home-link').click()
  await page.waitForFunction(() => document.querySelector('.page-frame-heading')?.textContent.includes('static/index.html'))
  await waitReady()
  assert.equal(await preview().locator('h1').textContent(), '模拟产品 A')
  checks.push('ordinary same-project page links reload through the parent with a fresh bridge and remain editable after two-page acceptance navigation')

  const beforeAnchor = calls.filter(call => call.kind === 'preview').length
  await preview().locator('#anchor-link').click()
  assert.equal(await preview().locator('body').evaluate(() => location.hash), '#sample-form')
  assert.equal(await preview().locator('h1').textContent(), '模拟产品 A')
  await waitReady()
  assert.equal(calls.filter(call => call.kind === 'preview').length, beforeAnchor)
  checks.push('hash-only anchors remain inside srcdoc despite the resource base URL, without losing the bridge or creating a page-preview request')

  await preview().locator('#counter-btn').click()
  assert.equal(await preview().locator('#count').textContent(), '1')
  await preview().locator('#name-field').fill('虚构验收样本')
  await preview().locator('#submit-btn').click()
  assert.equal(await preview().locator('#submitted').textContent(), '已提交：虚构验收样本')
  await page.getByRole('textbox', { name: '新增验收项', exact: true }).fill('计数与表单可交互')
  await page.getByRole('button', { name: '添加验收项', exact: true }).click()
  await page.locator('.acceptance-item').getByRole('button', { name: '通过', exact: true }).click()
  await page.getByRole('textbox', { name: '计数与表单可交互的验收备注', exact: true }).fill('虚构计数和表单结果符合预期')
  await page.getByRole('textbox', { name: '计数与表单可交互的验收备注', exact: true }).blur()
  assert.equal(await page.locator('.acceptance-item.passed').count(), 1)
  checks.push('acceptance preserves normal button and form behavior; pass/fail notes are local project-scoped records')

  await edit()
  await preview().locator('#counter-btn').click()
  await page.locator('.selection-info').waitFor()
  assert.equal(await selectedSelector(), '#counter-btn')
  assert.equal(await preview().locator('#count').textContent(), '1', 'editing captures a button without activating it')
  assert.equal(await preview().locator('[data-code-agent-design="selection"]').count(), 1)
  await applyColor('#16a34a')
  await waitColor('#counter-btn', 'rgb(22, 163, 74)')
  assert.equal(calls.filter(call => call.kind === 'styles').length, 0)
  await page.getByRole('button', { name: '撤销样式', exact: true }).click()
  await waitColor('#counter-btn', 'rgb(15, 23, 42)')
  await page.getByRole('button', { name: '重做样式', exact: true }).click()
  await waitColor('#counter-btn', 'rgb(22, 163, 74)')
  checks.push('editing captures the real element, previews CSS without execution or saving, and supports undo/redo')
  await screenshot('frontend-design-desktop-edit')

  const legitimate = await selectedSelector()
  await page.evaluate(() => {
    const iframe = document.querySelector('iframe'), match = iframe.srcdoc.match(/\{"channel":"([^"\n]+)","mode":/)
    const sample = { selector: '#forged', tag: 'button', text: 'forged', styles: { color: 'red' }, rect: { x: 1, y: 1, width: 10, height: 10, top: 1, left: 1, right: 11, bottom: 11 } }
    window.dispatchEvent(new MessageEvent('message', { source: window, data: { channel: match[1], type: 'selected', element: sample } }))
    window.dispatchEvent(new MessageEvent('message', { source: iframe.contentWindow, data: { channel: 'old-preview-channel', type: 'selected', element: sample } }))
  })
  await delay(100)
  assert.equal(await selectedSelector(), legitimate)
  checks.push('parent rejects spoofed message sources and old preview channel IDs')

  await page.evaluate(() => window.setReadonly(true))
  assert.equal(await page.getByRole('button', { name: '保存样式', exact: true }).isDisabled(), true)
  await applyColor('#15803d')
  await waitColor('#counter-btn', 'rgb(21, 128, 61)')
  assert.equal(calls.filter(call => call.kind === 'styles').length, 0)
  await page.evaluate(() => { window.setReadonly(false); window.setDirtyPaths(['static/index.html']) })
  assert.equal(await page.getByRole('button', { name: '保存样式', exact: true }).isDisabled(), true)
  await page.evaluate(() => window.setDirtyPaths([]))
  await save()
  const cssFile = path.join(staticA, 'code-agent.design.css')
  let storedRules = parseDesignCss(await fs.readFile(cssFile, 'utf8'))
  assert.equal(storedRules.find(rule => rule.selector === '#counter-btn').declarations.color, '#15803d')
  let savedEntry = await fs.readFile(path.join(staticA, 'index.html'), 'utf8')
  assert.equal((savedEntry.match(/code-agent\.design\.css/g) || []).length, 1)
  assert.equal(await page.evaluate(() => window.fixtureEvents.saved.length), 1)
  checks.push('readonly and unsaved editor-source guards block writes; an explicit writable save uses the production CAS endpoint and adds one managed stylesheet link')

  await acceptance()
  assert.equal(await preview().locator('[data-code-agent-design="selection"]').count(), 0)
  await preview().locator('#counter-btn').click()
  assert.equal(await preview().locator('#count').textContent(), '2')
  await page.getByRole('button', { name: '载入', exact: true }).click()
  await waitReady()
  await waitColor('#counter-btn', 'rgb(21, 128, 61)')
  checks.push('returning to acceptance removes selection interception; saved CSS survives a fresh project preview')

  await edit(); await preview().locator('#counter-btn').click()
  await page.getByRole('button', { name: 'AI 样式', exact: true }).click()
  await page.getByRole('textbox', { name: 'AI 样式设计要求', exact: true }).fill('做成蓝色主按钮，字更醒目')
  await page.getByRole('button', { name: '生成样式建议', exact: true }).click()
  const prompt = await page.evaluate(() => window.fixtureEvents.ask.at(-1))
  const request = JSON.parse(prompt.split('设计数据（JSON）：\n')[1])
  const result = { kind: 'frontend-style', request_id: request.request_id, selector: request.selector, declarations: { color: '#ffffff', 'background-color': '#2563eb', 'font-weight': '700' }, summary: '蓝色主按钮与白色文字' }
  await page.evaluate(value => window.deliverAi({ projectId: 'fixture-b', text: JSON.stringify(value) }), result)
  await page.evaluate(value => window.deliverAi({ projectId: 'fixture-a', text: JSON.stringify({ ...value, request_id: 'expired-request' }) }), result)
  await delay(150)
  assert.equal(await page.locator('.style-proposal').count(), 0)
  await page.evaluate(value => window.deliverAi({ projectId: 'fixture-a', text: '```json\n' + JSON.stringify(value) + '\n```' }), result)
  await page.locator('.style-proposal').waitFor()
  await waitColor('#counter-btn', 'rgb(21, 128, 61)')
  await screenshot('frontend-design-ai-review')
  await page.getByRole('button', { name: /^应用选中属性/ }).click()
  await waitColor('#counter-btn', 'rgb(255, 255, 255)')
  assert.equal(await preview().locator('#counter-btn').evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(37, 99, 235)')
  assert.equal(calls.filter(call => call.kind === 'styles').length, 1)
  checks.push('AI uses the existing prompt event only; mismatched project/request results are ignored and matched JSON requires a reviewed Apply before preview changes')

  await acceptance()
  const beforeDirtyNavigation = calls.filter(call => call.kind === 'preview').length
  await preview().locator('#detail-link').click()
  await page.waitForFunction(() => document.querySelector('.design-notice')?.textContent.includes('请先保存后再跳转'))
  assert.equal(await preview().locator('h1').textContent(), '模拟产品 A')
  assert.equal(calls.filter(call => call.kind === 'preview').length, beforeDirtyNavigation)
  await edit(); await preview().locator('#counter-btn').click()
  checks.push('unsaved style changes block acceptance page navigation with a clear save-first notice and preserve the current preview draft')

  await page.evaluate(() => window.changeProject('fixture-b'))
  await waitReady()
  assert.equal(await preview().locator('h1').textContent(), '模拟产品 B')
  assert.equal(await page.locator('.design-footer').textContent().then(text => text.includes('0 组样式修改')), true)
  assert.equal(await page.locator('.acceptance-item').count(), 0)
  await page.evaluate(() => window.changeProject('fixture-a'))
  await waitReady()
  await waitColor('#counter-btn', 'rgb(255, 255, 255)')
  assert.equal(await page.locator('.selection-info').count(), 0, 'a new iframe clears stale DOM selection')
  await preview().locator('#counter-btn').click()
  assert.equal(await selectedSelector(), '#counter-btn')
  checks.push('project switching isolates unsaved design styles and acceptance records, restores the original draft, and requires a fresh DOM selection')

  await applyColor('#ef4444')
  await waitColor('#counter-btn', 'rgb(239, 68, 68)')
  storedRules[0].declarations.color = '#884422'
  await fs.writeFile(cssFile, serializeDesignCss(storedRules))
  await page.getByRole('button', { name: '保存样式', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.design-status')?.textContent.includes('草稿已保留'))
  const draft = await page.evaluate(() => JSON.parse(localStorage.getItem('code-agent-frontend-design-v1:fixture-a')))
  assert.equal(parseDesignCss(draft.draft.css).find(rule => rule.selector === '#counter-btn').declarations.color, '#ef4444')
  assert.equal(parseDesignCss(await fs.readFile(cssFile, 'utf8'))[0].declarations.color, '#884422')
  assert.equal(await page.getByRole('button', { name: '保存样式', exact: true }).isDisabled(), true)
  await page.reload({ waitUntil: 'networkidle' }); await waitReady()
  assert.equal(await page.locator('.design-status').textContent().then(text => text.includes('草稿已保留')), true)
  await waitColor('#counter-btn', 'rgb(239, 68, 68)')
  await page.getByRole('button', { name: '放弃草稿并重载', exact: true }).click(); await waitReady()
  await waitColor('#counter-btn', 'rgb(136, 68, 34)')
  checks.push('external stylesheet changes yield HTTP 409 without overwriting; local draft survives full reload and can be explicitly discarded against fresh source')

  await page.getByRole('combobox', { name: '项目页面入口', exact: true }).selectOption('vue/index.html')
  await page.getByRole('textbox', { name: '前端开发服务地址', exact: true }).fill('')
  const assetsBeforeServiceCard = calls.filter(call => call.kind === 'asset').length
  const requiredResponsePromise = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/code/projects/fixture-a/frontend-design/preview')
  await page.getByRole('button', { name: '载入', exact: true }).click()
  const requiredResponse = await requiredResponsePromise
  assert.equal(requiredResponse.status(), 200)
  const requiredPreview = await requiredResponse.json()
  assert.equal(requiredPreview.runtime.requires_dev_server, true)
  assert.equal(requiredPreview.html, '')
  assert.equal(requiredPreview.base_url, '', 'source-only previews do not issue an asset capability')
  await page.locator('.service-state').waitFor()
  const serviceText = await page.locator('.service-state').textContent()
  assert.ok(serviceText.includes('npm run dev') && serviceText.includes('vue') && serviceText.includes(vueOrigin))
  assert.equal(await page.locator('iframe').count(), 0, 'an uncompiled source entry shows a service requirement instead of a blank iframe')
  assert.equal(calls.filter(call => call.kind === 'asset').length, assetsBeforeServiceCard, 'opening the requirement card neither starts nor connects the suggested project service')
  await screenshot('frontend-design-service-required')
  await page.getByRole('button', { name: '连接开发服务', exact: true }).click(); await waitReady()
  assert.equal(new URL(await page.getByRole('textbox', { name: '前端开发服务地址', exact: true }).inputValue()).origin, vueOrigin)
  checks.push('a source Vue entry without a server address displays its configured dev command and port without a blank iframe or asset ticket, then connects only after the explicit user action')
  await preview().locator('#vue-counter').waitFor({ timeout: 25000 })
  assert.equal(await preview().locator('h1').textContent(), '真实 Vue 模块页面')
  assert.equal(await preview().locator('#vue-api-module').textContent(), '公开前端 API 模块已加载')
  await preview().locator('#vue-logo').evaluate(image => new Promise((resolve, reject) => { if (image.complete && image.naturalWidth === 80) resolve(); else { image.addEventListener('load', resolve, { once: true }); image.addEventListener('error', () => reject(new Error('Imported Vue image failed to load')), { once: true }); setTimeout(() => image.naturalWidth === 80 ? resolve() : reject(new Error('Imported Vue image not loaded')), 8000) } }))
  await preview().locator('#vue-counter').click()
  assert.equal(await preview().locator('#vue-value').textContent(), '1')
  assert.ok(calls.some(call => call.kind === 'asset' && call.path.includes('App.vue')))
  assert.ok(calls.some(call => call.kind === 'asset' && call.path.includes('vue.js')))
  assert.ok(calls.some(call => call.kind === 'asset' && call.path === 'src/api/demo.js'), 'public frontend src/api JavaScript modules are resources, not backend API calls')
  checks.push('a genuine separately compiled Vue page loads through ticket-proxied modules including public src/api JavaScript and performs reactive interactions without a direct project-origin request')
  await verifyPrivateStorage()
  await verifyRouterHistory()
  assert.deepEqual(await page.evaluate(() => ({ local: localStorage.getItem('opaque-local'), session: sessionStorage.getItem('opaque-session') })), { local: 'parent-local-value', session: 'parent-session-value' })
  checks.push('the actual installed VueRouter createWebHistory initializes in the opaque iframe, RouterLink and history back/forward stay within its route, and unguarded local/session storage are isolated from the parent')
  await screenshot('frontend-design-vue-acceptance')

  await edit(); await preview().locator('#vue-counter').click()
  await applyColor('#7c3aed'); await waitColor('#vue-counter', 'rgb(124, 58, 237)')
  await save()
  const vueDesignCss = path.join(vueRoot, 'code-agent.design.css')
  assert.equal(parseDesignCss(await fs.readFile(vueDesignCss, 'utf8'))[0].declarations.color, '#7c3aed')
  const vueEntry = await fs.readFile(path.join(vueRoot, 'index.html'), 'utf8')
  assert.equal((vueEntry.match(/code-agent\.design\.css/g) || []).length, 1)
  // Verify the link in a fresh real Vite response, without the inspector's own
  // preview style block, to prove the saved style actually belongs to the page.
  const directPage = await context.newPage()
  directPage.on('console', event => { if (event.type() === 'error') consoleErrors.push(redactFixtureCapabilities(event.text())) })
  const viteResponse = await fetch(vueOrigin + '/')
  const liveHtml = await viteResponse.text()
  assert.equal(liveHtml.includes('code-agent.design.css'), true)
  const savedPreviewResponse = await fetch(`${backendOrigin}/api/v1/code/projects/fixture-a/frontend-design/preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ entry_path: 'vue/index.html', url: vueOrigin }) })
  assert.equal(savedPreviewResponse.status, 200)
  savedPageHtml = (await savedPreviewResponse.json()).html
  await directPage.goto(uiOrigin + '/__saved-design-page', { waitUntil: 'networkidle' })
  await directPage.locator('#vue-counter').waitFor()
  diagnostics.savedPage = await directPage.evaluate(() => ({ links: [...document.querySelectorAll('link[rel="stylesheet"]')].map(element => ({ href: element.href.replace(/frontend-design-assets\/[A-Za-z0-9_-]+/g, 'frontend-design-assets/[fixture-ticket]'), disabled: element.disabled, sheet: Boolean(element.sheet) })), stylesheetCount: document.styleSheets.length, selector: document.getElementById('vue-counter')?.id }))
  assert.equal(await directPage.locator('#vue-counter').evaluate(element => getComputedStyle(element).color), 'rgb(124, 58, 237)')
  assert.equal(await directPage.locator('[data-code-agent-design="style"]').count(), 0, 'saved page styling works without the inspector preview style block')
  await directPage.locator('#vue-counter').click()
  assert.equal(await directPage.locator('#vue-value').textContent(), '1')
  await directPage.close()
  await acceptance(); await page.getByRole('button', { name: '载入', exact: true }).click(); await waitReady()
  await waitColor('#vue-counter', 'rgb(124, 58, 237)')
  await preview().locator('#vue-counter').click()
  assert.equal(await preview().locator('#vue-value').textContent(), '1')
  checks.push('editing and explicit saving work for genuine Vue elements; the persisted entry link appears in fresh Vite HTML and saved styling survives reload without breaking interaction')

  await preview().locator('body').evaluate(() => { location.href = 'about:blank' })
  await page.waitForFunction(() => document.querySelector('.design-footer')?.textContent.includes('画布未连接'))
  await page.waitForFunction(() => document.querySelector('.bridge-notice')?.textContent.includes('选择工具未连接'), { timeout: 12000 })
  assert.equal(await page.locator('.design-status').textContent(), '画布连接失败')
  await page.getByRole('button', { name: '载入', exact: true }).click(); await waitReady()
  await preview().locator('#vue-counter').waitFor()
  checks.push('script-driven navigation to an unbridged document is detected by the iframe load/ping timeout and honestly reports disconnected until the user reloads the preview')

  const beforeHiddenLoad = calls.filter(call => call.kind === 'preview').length
  await page.evaluate(() => {
    window.setBridgeReadyGate(true)
    document.querySelector('.source-bar > button').click()
    window.setVisible(false)
  })
  await page.waitForFunction(() => !document.querySelector('.canvas-loading') && document.querySelector('.design-footer')?.textContent.includes('画布未连接'))
  await preview().locator('#vue-counter').waitFor()
  assert.equal(calls.filter(call => call.kind === 'preview').length, beforeHiddenLoad + 1)
  await page.evaluate(() => { window.setBridgeReadyGate(false); window.setVisible(true) })
  await waitReady()
  assert.equal(calls.filter(call => call.kind === 'preview').length, beforeHiddenLoad + 1)
  await preview().locator('#vue-counter').click()
  assert.equal(await preview().locator('#vue-value').textContent(), '1')
  checks.push('returning to a design view hidden during preview load re-pings a dropped ready handshake and restores interaction without another API read')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark')
  await page.getByRole('button', { name: '显示或收起属性面板', exact: true }).click()
  const bounds = await page.evaluate(() => ({ body: document.body.scrollWidth, viewport: innerWidth, source: document.querySelector('.source-bar').getBoundingClientRect().right, toolbar: document.querySelector('.tool-rail').getBoundingClientRect().right }))
  assert.ok(bounds.body <= bounds.viewport + 1 && bounds.source <= 391 && bounds.toolbar <= 391)
  await screenshot('frontend-design-dark-390-acceptance')
  await edit()
  await page.getByRole('button', { name: '属性', exact: true }).click()
  const panel = await page.locator('.properties-panel').boundingBox()
  assert.ok(panel.x >= 0 && panel.x + panel.width <= 391)
  await screenshot('frontend-design-dark-390-edit')
  checks.push('390px dark layout keeps controls inside the viewport and exposes collapsible, keyboard-accessible editing properties')

  // These are genuine missing-image requests against throwaway files. Keep them
  // last so expected resource errors cannot affect later interactive checks.
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.evaluate(() => document.documentElement.dataset.theme = 'light')
  await acceptance()
  await page.getByRole('combobox', { name: '项目页面入口', exact: true }).selectOption('vue/dist/index.html')
  await page.getByRole('textbox', { name: '前端开发服务地址', exact: true }).fill('')
  await page.getByRole('button', { name: '载入', exact: true }).click(); await waitReady()
  await preview().locator('#vue-counter').waitFor()
  assert.equal(await page.locator('.page-frame-heading').textContent().then(text => text.includes('项目页面 · vue/dist/index.html')), true)
  assert.equal(await preview().locator('#vue-api-module').textContent(), '公开前端 API 模块已加载')
  assert.equal(await page.locator('.service-state').count(), 0, 'the genuinely built bundle is a static page, not a source-only entry')
  await verifyPrivateStorage()
  await verifyRouterHistory()
  await preview().locator('#vue-counter').click()
  assert.equal(await preview().locator('#vue-value').textContent(), '1')
  await edit(); await preview().locator('#vue-counter').click()
  assert.equal(await selectedSelector(), '#vue-counter')
  await applyColor('#d97706'); await waitColor('#vue-counter', 'rgb(217, 119, 6)')
  await save()
  assert.equal(parseDesignCss(await fs.readFile(path.join(vueRoot, 'dist/code-agent.design.css'), 'utf8'))[0].declarations.color, '#d97706')
  await acceptance(); await page.getByRole('button', { name: '载入', exact: true }).click(); await waitReady()
  await waitColor('#vue-counter', 'rgb(217, 119, 6)')
  await verifyPrivateStorage()
  await verifyRouterHistory()
  vite8PreloadProbe = await preview().locator('body').evaluate(() => window.__privateVite8PreloadProbe)
  assert.ok(vite8PreloadProbe?.calls > 0)
  assert.deepEqual({ absolute: vite8PreloadProbe.absolute, outsideCapability: vite8PreloadProbe.outsideCapability, assetPath: vite8PreloadProbe.assetPath, usedImportMetaResolve: vite8PreloadProbe.usedImportMetaResolve }, { absolute: true, outsideCapability: true, assetPath: true, usedImportMetaResolve: true })
  await screenshot('frontend-design-built-spa-acceptance')
  checks.push('a genuine Vite production dist loads through the compressed parent helper with real VueRouter and unguarded opaque storage; Vite 8 style import.meta.resolve absolute lazy-route JS/CSS preload, route history, editing and saved styling survive a fresh static preview')

  const apiInput = page.getByRole('textbox', { name: '项目接口服务地址', exact: true })
  assert.equal(await apiInput.inputValue(), '', 'project metadata does not silently enable API forwarding')
  assert.equal(await page.getByRole('button', { name: '使用建议接口', exact: true }).count(), 1)
  const beforeMissingConnection = await readApiStats()
  assert.equal(beforeMissingConnection.count, 0)
  await runApiAction('axios', 'failed')
  assert.deepEqual(await preview().locator('body').evaluate(() => window.fixtureAxiosErrorRewrite), { rewritten: true, namePreserved: true }, 'the actual Axios response error interceptor can normalize an API failure without a readonly DOMException message setter')
  assert.equal((await preview().locator('#api-error').textContent()).startsWith('接口读取失败：'), true)
  assert.match(await preview().locator('#api-error').textContent(), /接口|API|连接|配置|only/i)
  assert.equal((await preview().locator('#api-error').textContent()).includes('Invalid URL'), false)
  await runApiAction('fetch', 'failed')
  assert.equal((await readApiStats()).count, 0)
  assert.equal(calls.filter(call => call.kind === 'api').length, 0, 'unconnected relative API reads never create a capability request')
  checks.push('a suggested project API remains disconnected until an explicit action; actual Axios error-interceptor message normalization and fetch relative /api reads fail honestly without Invalid URL or any upstream request')

  const resourceOnlyToken = await preview().locator('body').evaluate(() => new URL(document.baseURI).pathname.split('/')[5])
  await apiInput.fill(apiOrigin)
  const apiPreviewPromise = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/code/projects/fixture-a/frontend-design/preview')
  await page.getByRole('button', { name: '连接接口', exact: true }).click()
  const apiPreviewResponse = await apiPreviewPromise
  assert.equal(apiPreviewResponse.status(), 200)
  const apiPreview = await apiPreviewResponse.json()
  assert.equal(apiPreview.api_url, apiOrigin)
  const apiProxy = new URL(apiPreview.api_proxy_url)
  assert.equal(apiProxy.pathname.startsWith('/api/v1/code/frontend-design-api/'), true)
  const apiToken = apiProxy.pathname.split('/')[5], assetToken = new URL(apiPreview.base_url).pathname.split('/')[5]
  assert.equal(Boolean(apiToken && assetToken && apiToken === assetToken), true, 'the configured preview binds the same nonce to separate resource and read-only API namespaces')
  await waitReady()
  await runApiAction('axios')
  assert.equal(await preview().locator('#api-code').textContent(), '200')
  let apiResult = JSON.parse(await preview().locator('#api-result').textContent())
  assert.equal(apiResult.label, '虚构项目接口')
  assert.deepEqual(apiResult.query, { tag: 'axios', note: 'a/b' })
  await runApiAction('fetch')
  assert.equal(await preview().locator('#api-code').textContent(), '200')
  apiResult = JSON.parse(await preview().locator('#api-result').textContent())
  assert.deepEqual(apiResult.query, { tag: 'fetch', note: 'a/b' })
  await runApiAction('head')
  assert.equal(await preview().locator('#api-code').textContent(), '200')
  await runApiAction('status')
  assert.equal(await preview().locator('#api-code').textContent(), '418')
  const successfulApi = await readApiStats()
  assert.equal(successfulApi.count, 4)
  assert.deepEqual(successfulApi.calls.map(call => call.method), ['GET', 'GET', 'HEAD', 'GET'])
  assert.ok(successfulApi.calls.every(call => !call.cookie && !call.authorization))
  assert.ok(calls.filter(call => call.kind === 'api').every(call => ['GET', 'HEAD', 'OPTIONS'].includes(call.method)))
  await screenshot('frontend-design-readonly-project-api')
  checks.push('actual installed Axios and fetch work from opaque srcdoc through a distinct project API capability; GET/HEAD methods, encoded queries and HTTP 418 survive while parent cookies and explicit authorization are omitted upstream')

  await page.evaluate(() => window.setReadonly(true))
  await runApiAction('post', 'failed')
  await runApiAction('put', 'failed')
  // The UI fixture's Vite has its own generic OPTIONS responder. Verify the
  // production capability handler's CORS/method policy directly, rather than
  // mistaking that development middleware for the backend's read-only policy.
  const backendApiProxy = new URL(apiProxy.pathname, backendOrigin)
  for (const method of ['POST', 'PUT']) {
    const rejected = await fetch(new URL('demo', backendApiProxy), { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fixture: true }) })
    assert.ok([403, 405].includes(rejected.status), 'the capability endpoint independently rejects writes')
  }
  const preflight = await fetch(new URL('demo', backendApiProxy), { method: 'OPTIONS', headers: { Origin: 'null', 'Access-Control-Request-Method': 'GET' } })
  assert.ok([200, 204].includes(preflight.status))
  assert.match(preflight.headers.get('access-control-allow-methods') || '', /GET/)
  assert.match(preflight.headers.get('access-control-allow-methods') || '', /HEAD/)
  assert.equal((preflight.headers.get('access-control-allow-methods') || '').includes('POST'), false)
  const wrongCapability = new URL(backendApiProxy)
  wrongCapability.pathname = wrongCapability.pathname.replace(apiToken, resourceOnlyToken)
  const crossTicket = await fetch(new URL('demo', wrongCapability))
  assert.ok([403, 404].includes(crossTicket.status), 'a resource-only preview ticket cannot grant API access')
  apiBoundaryStats = await readApiStats()
  assert.equal(apiBoundaryStats.count, successfulApi.count, 'blocked writes, preflight and cross-capability attempts make no upstream call')
  assert.ok(apiBoundaryStats.calls.every(call => ['GET', 'HEAD'].includes(call.method) && !call.cookie && !call.authorization))
  await page.evaluate(() => window.setReadonly(false))
  checks.push('readonly project API denies browser POST/PUT and direct capability writes, handles only read preflight, and rejects a resource-only preview ticket without an upstream request')
  await page.getByRole('combobox', { name: '项目页面入口', exact: true }).selectOption('static/index.html')
  await page.getByRole('textbox', { name: '前端开发服务地址', exact: true }).fill('')
  await page.getByRole('button', { name: '载入', exact: true }).click(); await waitReady()
  await preview().locator('#resource-error-link').click()
  await page.waitForFunction(() => document.querySelector('.bridge-notice')?.textContent.includes('static/missing-logo.png') && document.querySelector('.bridge-notice')?.textContent.includes('static/missing-banner.png'))
  const resourceNotice = await page.locator('.bridge-notice').textContent()
  const capability = await preview().locator('body').evaluate(() => new URL(document.baseURI).pathname.match(/frontend-design-assets\/([^/]+)/)?.[1] || '')
  assert.ok(capability, 'the loaded temporary page has a capability-bound resource base')
  for (const secret of [capability, 'frontend-design-assets', 'fixture-secret', 'private-fragment', 'access_token', 'signature', '127.0.0.1', 'http://']) assert.equal(resourceNotice.includes(secret), false)
  assert.equal(resourceNotice.includes('开发服务'), false, 'missing static resources do not receive an unrelated Vue/server suggestion')
  await page.locator('.bridge-notice summary').click()
  assert.equal(await page.locator('.bridge-notice li').count(), 2)
  await screenshot('frontend-design-resource-errors')
  checks.push('real missing image resources display both project-relative paths, group repeated page failures, redact capability and URL data, and omit an unrelated framework-server suggestion')

  assert.deepEqual(errors, [])
  assert.deepEqual(unexpected, [])
  assert.ok(calls.every(call => ['info', 'preview', 'styles', 'asset', 'api'].includes(call.kind)))
  assert.equal(calls.filter(call => call.kind === 'styles').length, 4, 'only explicit fixture save actions write files, including the expected conflict and private production dist')
  await context.close()
} catch (error) {
  failure = { message: redactFixtureCapabilities(error.message), stack: redactFixtureCapabilities(error.stack || '') }
  if (backend?.exitCode !== null) failure.fixtureServer = backendLog.slice(-1800)
} finally {
  await browser?.close()
  await uiVite?.close()
  await projectVite?.close()
  if (backend && backend.exitCode === null) {
    backend.kill()
    await Promise.race([new Promise(resolve => backend.once('exit', resolve)), delay(3000)])
  }
  if (directory) {
    const parent = path.resolve(os.tmpdir()) + path.sep
    assert.ok(path.resolve(directory).startsWith(parent) && path.basename(directory).startsWith('code-agent-frontend-design-'))
    await fs.rm(directory, { recursive: true, force: true })
  }
  const report = { ok: !failure, checks, errors, unexpected, parentHelper: { minified: minifiedHelperObserved, bytes: minifiedHelperBytes, loads: minifiedHelperLoads }, productionPreload: { patchedAssignments: vite8PreloadPatchCount, probe: vite8PreloadProbe }, projectApi: apiBoundaryStats ? { calls: apiBoundaryStats.count, readonly: apiBoundaryStats.calls.every(call => ['GET', 'HEAD'].includes(call.method)), credentialsOmitted: apiBoundaryStats.calls.every(call => !call.cookie && !call.authorization) } : null, requests: { info: calls.filter(call => call.kind === 'info').length, preview: calls.filter(call => call.kind === 'preview').length, styleSaves: calls.filter(call => call.kind === 'styles').length, assets: calls.filter(call => call.kind === 'asset').length, api: calls.filter(call => call.kind === 'api').length }, ...(failure ? { failure, consoleErrors, diagnostics, moduleImports } : {}) }
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
}
if (failure) process.exitCode = 1
