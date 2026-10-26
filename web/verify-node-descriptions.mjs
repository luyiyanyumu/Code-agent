import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Mount the production components; every API and external request is intercepted.
const module = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const playwright = module.default || module
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || fileURLToPath(new URL('../.tmp/node-descriptions', import.meta.url)))
fs.mkdirSync(output, { recursive: true })
const checks = [], errors = [], unexpected = [], mutations = [], sourceEvents = [], requests = []
const file = 'backend/src/main/java/demo/AgentService.java', id = `file:${file}`
const symbol = { id: `sym:${file}#AgentService`, kind: 'class', name: 'AgentService', qname: 'demo.AgentService', path: file, line: 64, end_line: 3101, parent: '', confidence: 'heuristic', doc: '处理用户需求并调用工具。' }
const doc = '处理用户需求并调用工具。\n\n职责：\n- 读取会话上下文\n- 校验工具权限\n- 处理模型返回的工具调用\n- 保存执行证据与最终回复\n\n调用约定：工具执行前检查当前会话的权限。\n\n' + Array.from({ length: 8 }, (_, n) => `- 执行步骤 ${n + 1}：保留输入与结果，方便后续追踪。`).join('\n')
const purpose = {
  available: true, target: id, kind: 'file', role: '文件', name: 'AgentService.java', path: file, line: 1,
  headline: '处理用户需求并调用工具。', documentation: { text: doc, line: 55, source: '类文档注释' },
  facts: [
    { label: '基本情况', text: 'java · 3101 行 · 1 个符号', line: 1 },
    { label: '符号构成', text: '类 1', line: 64 },
    { label: '主要职责', text: '处理模型返回的工具调用', line: 55 },
    { label: '来源', text: 'AgentService 的类文档', line: 55 },
    { label: '权限约定', text: '调用前检查当前会话权限', line: 61 },
    { label: '执行证据', text: '保存执行证据与最终回复', line: 60 },
  ], basis: '作者说明 + 文件内容 + 符号索引', notes: [],
}
const map = {
  change_seq: 1, structure: { id: 'mod:', kind: 'dir', name: '说明示例项目', path: '', children: [{ id, kind: 'file', name: 'AgentService.java', path: file, lang: 'java', loc: 3101, symbols: [symbol] }] },
  modules: [], edges: { imports: [] }, stats: { files: 1, symbols: 1, edges: 0 }, scan: {}, warnings: [],
}
const componentSource = await (await fetch(`${base}/src/components/code/MindMap.vue`)).text()
const vueUrl = componentSource.match(/from "([^"\n]*\/vue\.js[^"\n]*)"/)[1]
const fixture = `<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Node descriptions fixture</title><div id="fixture"></div><script type="module">
import { createApp, h } from '${vueUrl}';
import MindMap from '/src/components/code/MindMap.vue';
import '/src/style.css';
window.sourceEvents=[];
createApp({render(){return h(MindMap,{projectId:'fixture-node-descriptions',onOpenFile:event=>window.sourceEvents.push(event)})}}).mount('#fixture');
</script><style>html,body,#fixture{margin:0;width:100%;height:100%;}#fixture{display:flex;overflow:hidden;}</style></html>`
const browser = await playwright.chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: width === 1440 ? 920 : 844 }, serviceWorkers: 'block' })
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), pathname = url.pathname, method = request.method()
      const label = `${method} ${pathname}`
      const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
      if (url.origin !== origin) { unexpected.push(label); return route.abort('blockedbyclient') }
      if (pathname === '/__node-descriptions-fixture') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: fixture })
      if (!pathname.startsWith('/api/')) return route.continue()
      requests.push(label)
      const suffix = pathname.replace('/api/v1/code/projects/fixture-node-descriptions', '')
      if (method === 'GET') {
        if (suffix === '/map') return reply(map)
        if (suffix === '/index') return reply({ change_seq: 1, scanning: false })
        if (suffix === '/feature-briefs') return reply({ missing: { total: 0 } })
        if (suffix === '/symbol/purpose') return reply(purpose)
        if (suffix === '/spec/behaviors') return reply({ cards: [] })
        if (suffix === '/contracts') return reply({ has_contract: false })
        if (suffix === '/decisions') return reply({ items: [], kinds: [] })
        if (suffix === '/provenance') return reply({ items: [] })
        if (suffix === '/symbol') return reply(symbol)
        if (suffix === '/symbol/logic') return reply({ available: true, steps: [], calls: [] })
      }
      // Impact is a read-only query despite using POST.
      if (method === 'POST' && suffix === '/impact') {
        const body = request.postDataJSON()
        assert.deepEqual(body.files, [file]); assert.deepEqual(body.symbol_ids, [])
        return reply({ risk: { level: 'low', score: 0, reasons: [] }, tests: [] })
      }
      if (method !== 'GET') mutations.push(label)
      unexpected.push(label); return reply({ detail: 'Unstubbed fixture route' }, 501)
    })
    const page = await context.newPage(); page.setDefaultTimeout(10000)
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(`${base}/__node-descriptions-fixture`, { waitUntil: 'networkidle' })
    const fileNode = page.locator(`.map .node[data-id="${id}"]`)
    await fileNode.waitFor()
    const nodeBounds = await fileNode.boundingBox()
    await page.mouse.move(nodeBounds.x + nodeBounds.width / 2, nodeBounds.y + nodeBounds.height / 2)
    await page.locator('.nodetip .nt-line').getByText(purpose.headline, { exact: true }).waitFor()
    const tip = page.locator('.nodetip'), tipText = await tip.innerText()
    assert.ok(tipText.includes('权限约定') && tipText.includes('执行证据'), 'hover keeps facts beyond the old four-item limit')
    const bounds = await tip.boundingBox()
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 2, `hover is bounded at ${width}px`)
    await tip.hover()
    await page.waitForTimeout(240)
    assert.equal(await tip.isVisible(), true, 'pointer can enter the tooltip without its close timer hiding it')
    assert.equal(await tip.evaluate(element => element.scrollHeight > element.clientHeight), true, 'long explanations have a scrollable tooltip')
    await page.mouse.wheel(0, 250)
    await page.waitForTimeout(100)
    assert.ok(await tip.evaluate(element => element.scrollTop) > 0, 'tooltip responds to wheel scrolling')
    await tip.evaluate(element => { element.scrollTop = 0 })
    await page.screenshot({ path: path.join(output, `hover-${width}.png`) })
    checks.push(`hover ${width}: author purpose, all facts, pointer entry and scrolling`)
    await fileNode.click()
    await page.locator('.detail .purpose-documentation').waitFor()
    const text = await page.locator('.detail .purpose-documentation').innerText()
    for (const line of doc.split('\n').filter(Boolean)) assert.ok(text.includes(line), `full documentation keeps ${line}`)
    assert.equal((await page.evaluate(() => window.sourceEvents)).length, 0, 'opening explanation stays on the map')
    assert.equal(await page.locator('.map svg').isVisible(), true)
    assert.ok((await page.locator('.detail').innerText()).includes('文件'))
    assert.equal((await page.locator('.detail').innerText()).includes('这是模块节点'), false)
    const implementationBounds = await page.locator('.detail .panel').boundingBox()
    assert.ok(implementationBounds.height <= 100, `file implementation note is compact at ${width}px: ${implementationBounds.height}`)
    if (width === 390) {
      const bodyBounds = await page.locator('.detail .body').boundingBox()
      const headingBounds = await page.locator('.detail .documentation-head b').boundingBox()
      assert.ok(headingBounds.y >= bodyBounds.y && headingBounds.y + headingBounds.height <= bodyBounds.y + bodyBounds.height, 'the detailed explanation title is visible before scrolling on mobile')
    }
    const layout = await page.evaluate(() => ({ viewport: innerWidth, scroll: document.documentElement.scrollWidth }))
    assert.ok(layout.scroll <= layout.viewport + 2)
    await page.screenshot({ path: path.join(output, `detail-${width}.png`) })
    checks.push(`detail ${width}: full author documentation and file-specific handling`)
    const sourceButton = page.locator('.detail .purpose-documentation').getByRole('button', { name: /^查看注释/ })
    await sourceButton.click()
    const events = await page.evaluate(() => window.sourceEvents)
    assert.equal(events.at(-1).path, file)
    assert.equal(events.at(-1).line, 55)
    sourceEvents.push(...events)
    checks.push(`source ${width}: opens author documentation line 55`)
    await page.locator('.detail header').getByRole('button', { name: '打开源码', exact: true }).click()
    const fileEvents = await page.evaluate(() => window.sourceEvents)
    assert.equal(fileEvents.at(-1).path, file)
    assert.equal(fileEvents.at(-1).line, 1)
    checks.push(`file source ${width}: explicit action opens the file`)
    await context.close()
  }
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); assert.deepEqual(mutations, [])
  console.log(JSON.stringify({ checks, sourceEvents, apiRequests: requests.length, mutations, errors, unexpected, screenshots: output }, null, 2))
} finally { await browser.close() }
