import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { createHash } from 'node:crypto'

// Laboratory replay of the actual MindMap component. All data/API calls are
// memory-only fixtures. Baseline serves the frozen, pre-change compiled SFC;
// current serves the working component through the same Vite/runtime/browser.
const base = new URL(process.argv[2] || 'http://127.0.0.1:5199').origin
const output = path.resolve(process.argv[3] || '../.tmp/mindmap-performance-20261031/current')
const mode = process.argv[4] || 'current'
const functionalOnly = process.env.MINDMAP_FUNCTIONAL_ONLY === '1'
const runs = functionalOnly ? 1 : 3
assert.ok(['baseline', 'current'].includes(mode))
const snapshot = path.resolve('../.tmp/mindmap-performance-20261031')
await fs.mkdir(output, { recursive: true })
const sourcePath = '/src/components/code/MindMap.vue'
const baselinePath = path.join(snapshot, 'MindMap.baseline.js')
const compiled = mode === 'baseline' ? await fs.readFile(baselinePath, 'utf8') : await (await fetch(base + sourcePath)).text()
const strippedModule = compiled.slice(compiled.indexOf('import { computed'))
assert.ok(strippedModule.startsWith('import { computed'), 'expected Vite SFC output')
const moduleSource = 'import.meta.hot={on(){},accept(){},dispose(){},prune(){},data:{}};\n' + strippedModule
await fs.writeFile(path.join(output, 'component-under-test.js'), moduleSource)
const vuePath = compiled.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath)
const stylePath = compiled.match(/import ["']([^"']*type=style[^"']*)["']/)?.[1]
let styleSource
if (mode === 'baseline') {
  const filename = path.join(snapshot, 'MindMap.baseline.style.js')
  try { styleSource = await fs.readFile(filename, 'utf8') }
  catch { styleSource = await (await fetch(base + stylePath)).text(); await fs.writeFile(filename, styleSource) }
}
const dirs = 20, files = 76
const fixtureMap = { version: 1, change_seq: 1, stats: { files: dirs * files, symbols: 0, edges: dirs * files + dirs, kinds: { dir: dirs + 1, file: dirs * files } },
  scan: { finished_at: '2026-10-31T00:00:00Z', externals: 0 }, modules: [], edges: { imports: [] }, warnings: [],
  structure: { id: 'root', kind: 'dir', name: '大型项目测试夹具', path: '', children: Array.from({ length: dirs }, (_, d) => ({
    id: `dir:${d}`, kind: 'dir', name: `module-${String(d).padStart(2, '0')}`, path: `module-${d}`, children: Array.from({ length: files }, (_, f) => ({
      id: `file:module-${d}/Controller${f}.java`, kind: 'file', name: `Controller${String(f).padStart(2, '0')}.java`, path: `module-${d}/Controller${f}.java`, symbols: [], children: [],
    })),
  })) } }
const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><div id="app"></div><script type="module">
import {createApp,h} from ${JSON.stringify(vuePath)}; import MindMap from '${sourcePath}'; import {api} from '/src/api.js'; import '/src/style.css';
const fixture=window.fixture={updates:0,selected:[],requests:[],map:${JSON.stringify(fixtureMap)}};
const idle={state:'idle',running:false,revision:1,coverage_known:true,coverage_total:1541,described:1541,missing:0,total:1541,done:1541,written:0,failed:0};
const fallback=async(...args)=>{fixture.requests.push(args);return {}};
api.code=new Proxy({map:async()=>fixture.map,indexStatus:async()=>({change_seq:1,scanning:false,briefs:idle}),features:async()=>({domains:[]}),symbolPurpose:async()=>({headline:'隔离夹具中的项目文件',facts:[]})},{get:(target,key)=>key in target?target[key]:fallback});
MindMap.updated=()=>fixture.updates++;
createApp({setup:()=>()=>h('div',{class:'fixture-ide'},[h('header','Code-agent · 思维导图性能测试夹具（1541 节点）'),h('main',[h(MindMap,{projectId:'isolated-performance-fixture',tree:'main',onSelect:node=>fixture.selected.push(node.id)})])])}).mount('#app');
</script><style>body{margin:0;overflow:hidden}.fixture-ide{height:100dvh;display:flex;flex-direction:column}.fixture-ide>header{padding:10px 16px;height:42px;box-sizing:border-box;border-bottom:1px solid var(--border)}.fixture-ide>main{display:flex;flex:1;min-height:0;min-width:0}</style></body></html>`
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const report = { passed: false, mode, functionalOnly, environment: { browser: browser.version(), viewport: { width: 1440, height: 960 }, cpuThrottling: 1,
  compiledComponentSha256: createHash('sha256').update(moduleSource).digest('hex'), data: { directories: dirs, files: dirs * files, expectedNodes: 1541, expectedLinks: 1540 },
  note: 'Headless Chrome local laboratory replay, Vite development runtime, no CPU/network throttling. Per-event script/task duration and RAF intervals are diagnostic, not real-user FPS or INP.' }, samples: [], checks: [], errors: [], forbidden: [] }
let page
function percentile(values, quantile) { const sorted = [...values].sort((a, b) => a - b); return sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * quantile))] : null }
try {
  const context = await browser.newContext({ viewport: report.environment.viewport, serviceWorkers: 'block' })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin !== base || url.pathname.startsWith('/api/')) { report.forbidden.push(url.pathname); return route.abort() }
    if (url.pathname === '/mind-map-performance-fixture') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html })
    if (url.pathname === sourcePath && !url.searchParams.has('type')) return route.fulfill({ contentType: 'application/javascript', body: moduleSource })
    if (mode === 'baseline' && url.pathname === sourcePath && url.searchParams.get('type') === 'style') return route.fulfill({ contentType: 'application/javascript', body: styleSource })
    return route.continue()
  })
  page = await context.newPage(); page.setDefaultTimeout(30000)
  page.on('pageerror', error => report.errors.push(error.message))
  await page.goto(base + '/mind-map-performance-fixture', { waitUntil: 'networkidle' })
  await page.waitForFunction(() => document.querySelectorAll('.map .node').length === 1541)
  await page.waitForTimeout(300)
  const cdp = await context.newCDPSession(page)
  await cdp.send('Performance.enable')
  const scene = page.locator('.map svg > g').first(), svg = page.locator('.map svg').first()
  const readTransform = async () => scene.evaluate(el => { const value = el.transform.baseVal.consolidate().matrix; return { x: value.e, y: value.f, scale: value.a, raw: el.getAttribute('transform') } })
  const counts = () => page.evaluate(() => ({ nodes: document.querySelectorAll('.map .node').length, links: document.querySelectorAll('.map .tree-link').length, selected: [...window.fixture.selected] }))
  assert.deepEqual(await counts(), { nodes: 1541, links: 1540, selected: [] })
  report.checks.push('1541 real SVG nodes and1540 tree links render without truncation')
  await page.evaluate(() => {
    window.perfReplay = { active: false, frames: [], tasks: [], cameraWrites: 0, nodeWrites: 0, updates: 0, start: 0 }
    let previous = 0
    const frame = now => { const p = window.perfReplay; if (p.active) { if (previous) p.frames.push(now - previous); previous = now } else previous = 0; requestAnimationFrame(frame) }
    requestAnimationFrame(frame)
    new PerformanceObserver(list => { if (window.perfReplay.active) window.perfReplay.tasks.push(...list.getEntries().map(entry => entry.duration)) }).observe({ type: 'longtask', buffered: false })
    new MutationObserver(records => { const p = window.perfReplay; if (!p.active) return; for (const record of records) { if (record.target === document.querySelector('.map svg > g')) p.cameraWrites++; else p.nodeWrites++ } }).observe(document.querySelector('.map svg > g'), { attributes: true, childList: true, characterData: true, subtree: true })
  })
  const metrics = async () => Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(item => [item.name, item.value]))
  const begin = async () => { await page.evaluate(() => { const p = window.perfReplay; Object.assign(p, { active: true, frames: [], tasks: [], cameraWrites: 0, nodeWrites: 0, updates: window.fixture.updates, start: performance.now() }) }); return metrics() }
  const end = async (before, events) => {
    await page.waitForTimeout(50)
    const after = await metrics(), browserData = await page.evaluate(() => { const p = window.perfReplay; p.active = false; return { ...p, wallMs: performance.now() - p.start, updates: window.fixture.updates - p.updates } })
    const timing = {}
    for (const key of ['ScriptDuration', 'TaskDuration', 'LayoutDuration', 'RecalcStyleDuration']) timing[key + 'Ms'] = (after[key] - before[key]) * 1000
    return { events, ...timing, scriptMsPerEvent: timing.ScriptDurationMs / events, taskMsPerEvent: timing.TaskDurationMs / events,
      wallMs: browserData.wallMs, vueUpdates: browserData.updates, cameraWrites: browserData.cameraWrites, nodeMutations: browserData.nodeWrites,
      raf: { samples: browserData.frames.length, p50Ms: percentile(browserData.frames, 0.5), p95Ms: percentile(browserData.frames, 0.95), maxMs: Math.max(0, ...browserData.frames) },
      longTasks: { count: browserData.tasks.length, totalMs: browserData.tasks.reduce((a, b) => a + b, 0), maxMs: Math.max(0, ...browserData.tasks) } }
  }
  for (let run = 0; run < runs; run++) {
    await page.getByRole('button', { name: '适应宽度', exact: true }).click()
    await page.waitForTimeout(100)
    const box = await svg.boundingBox(), start = { x: box.x + 5, y: box.y + 8 }, beforePan = await readTransform()
    await page.mouse.move(start.x, start.y); await page.mouse.down()
    const panMetrics = await begin()
    const events = 90, dx = 120, dy = 74
    for (let step = 1; step <= events; step++) await page.mouse.move(start.x + dx * step / events, start.y + dy * step / events)
    await page.mouse.up()
    const pan = await end(panMetrics, events), afterPan = await readTransform()
    assert.ok(Math.abs(afterPan.x - beforePan.x - dx) < 1, JSON.stringify({ beforePan, afterPan }))
    assert.ok(Math.abs(afterPan.y - beforePan.y - dy) < 1)
    assert.equal(afterPan.scale, beforePan.scale)
    assert.deepEqual(await counts(), { nodes: 1541, links: 1540, selected: [] })
    await page.mouse.move(box.x + box.width - 6, box.y + box.height - 8)
    const beforeZoom = await readTransform(), zoomMetrics = await begin()
    for (let step = 0; step < 12; step++) { await page.mouse.wheel(0, step < 6 ? -24 : 24); await page.waitForTimeout(17) }
    const zoom = await end(zoomMetrics, 12), afterZoom = await readTransform()
    assert.notEqual(afterZoom.scale, beforeZoom.scale)
    assert.deepEqual(await counts(), { nodes: 1541, links: 1540, selected: [] })
    report.samples.push({ run: run + 1, pan, zoom, beforePan, afterPan, beforeZoom, afterZoom, counts: await counts() })
  }
  report.checks.push(`${runs} identical90-input native mouse drags finish at the exact120x74px displacement with unchanged scale and no accidental node selection`)
  report.checks.push(`${runs} twelve-event native wheel sequences change zoom without deleting any SVG nodes/links`)
  const manualCamera = await readTransform()
  await page.screenshot({ path: path.join(output, 'map-large.png') })
  const root = page.locator('.map .node[data-id="root"]')
  await root.locator('[data-role="caret"]').click()
  await page.waitForFunction(() => document.querySelectorAll('.map .node').length === 1)
  assert.deepEqual(await readTransform(), manualCamera, 'collapsing after manual camera control cannot clear its transform')
  await root.locator('[data-role="caret"]').dispatchEvent('click')
  await page.waitForFunction(() => document.querySelectorAll('.map .node').length === 1541)
  assert.deepEqual(await readTransform(), manualCamera, 'expanding after manual camera control cannot clear its transform')
  report.checks.push('Collapsing and expanding the root preserves all1541 nodes and1540 links')
  await root.focus(); await page.keyboard.press('Enter')
  await page.waitForFunction(() => window.fixture.selected.includes('root'))
  assert.deepEqual(await readTransform(), manualCamera, 'selection/detail rerender cannot clear manual camera transform')
  report.checks.push('Keyboard activation still selects the root and opens its details')
  report.checks.push('Collapsing, expanding and selecting after manual pan/zoom preserve the exact camera transform across Vue rerenders')
  assert.equal((await counts()).nodes, 1541)
  assert.deepEqual(report.errors, []); assert.deepEqual(report.forbidden, [])
  report.summary = functionalOnly ? { note: 'Interaction regression only; timing intentionally excluded from performance comparison.' } : Object.fromEntries(['pan', 'zoom'].map(kind => [kind, Object.fromEntries(['ScriptDurationMs', 'TaskDurationMs', 'scriptMsPerEvent', 'taskMsPerEvent', 'vueUpdates', 'cameraWrites', 'nodeMutations', 'wallMs'].map(key => [key, percentile(report.samples.map(row => row[kind][key]), 0.5)]))]))
  report.passed = true
} catch (error) {
  report.error = error.stack || String(error); process.exitCode = 1
  if (page) await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {})
} finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  await browser.close()
  console.log(JSON.stringify({ passed: report.passed, mode, summary: report.summary, checks: report.checks, error: report.error, report: path.join(output, 'report.json') }))
}
