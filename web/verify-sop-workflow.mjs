import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { createSopWorkflow, createWorkflowNode, validateWorkflow } from './src/lib/sopWorkflow.js'

// Production MindMap + workflow + dialog host. All API traffic is fixture-only.
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
const base = process.argv[2] || 'http://127.0.0.1:5175', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || '../.tmp/sop-workflow-browser')
await fs.mkdir(output, { recursive: true })
const source = await (await fetch(`${base}/src/components/code/MindMap.vue`)).text()
const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'Vite is required')
const fixture = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><div id="fixture"></div><script type="module">
import {createApp,h,ref} from '${vuePath}'; import MindMap from '/src/components/code/MindMap.vue'; import DialogHost from '/src/components/ui/DialogHost.vue'; import '/src/style.css';
const projectId=ref(new URL(location.href).searchParams.get('project')||'fixture-a'),tree=ref('main'); window.changeProject=id=>projectId.value=id; window.changeTree=id=>tree.value=id; window.sopEvents=[];
createApp({render:()=>h('div',{style:'display:flex;flex-direction:column;height:100%;min-height:0'},[h(MindMap,{projectId:projectId.value,tree:tree.value,onAsk:text=>window.sopEvents.push(text)}),h(DialogHost)])}).mount('#fixture');
</script><style>html,body,#fixture{height:100%;width:100%;margin:0;overflow:hidden}</style></html>`
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const checks = [], errors = [], unexpected = [], nativeDialogs = [], writes = []
const clone = value => JSON.parse(JSON.stringify(value))
const nineStageOrder = ['requirements', 'fixture_technology', 'ui_design', 'architecture', 'planning', 'implementation', 'verification', 'release', 'maintenance']
function nineStageWorkflow() {
  // Reproduce the screenshot: a node appended to storage was connected between
  // the first two stages. Arrangement must follow connections, not array order.
  const document = createSopWorkflow()
  const added = { ...createWorkflowNode('custom', 320, 60), id: 'fixture_technology', title: '技术选型', objective: '选择语言、技术框架等', deliverable: '技术选型记录' }
  document.nodes.push(added)
  document.edges = nineStageOrder.slice(1).map((target, index) => ({ id: `fixture_edge_${index}`, source: nineStageOrder[index], target }))
  const byId = new Map(document.nodes.map(node => [node.id, node]))
  nineStageOrder.forEach((id, index) => { byId.get(id).x = 40 + (index % 2) * 280; byId.get(id).y = 60 + Math.floor(index / 2) * 260 })
  document.viewport = { x: 35, y: 32, zoom: .65 }
  document.revision = 1
  document.updated_at = '2026-10-10T00:00:00Z'
  assert.equal(validateWorkflow(document).valid, true)
  return document
}
function branchedWorkflow() {
  const document = createSopWorkflow()
  document.name = '分支协作工作流'
  const stages = [
    ['dag_start', 'requirements', '需求确认'],
    ['dag_ui', 'ui_design', '界面与交互'],
    ['dag_tech', 'architecture', '技术方案'],
    ['dag_quality', 'planning', '测试规划'],
    ['dag_join', 'implementation', '开发整合'],
    ['dag_delivery', 'release', '交付发布'],
    ['dag_notes', 'custom', '独立调研'],
  ]
  // Deliberately scrambled storage and positions; links determine the layout.
  document.nodes = stages.map(([id, type, title], index) => ({ ...createWorkflowNode(type, (index % 2) * 55, index * 37), id, title })).reverse()
  const links = [
    ['dag_start', 'dag_ui'], ['dag_start', 'dag_tech'], ['dag_start', 'dag_quality'],
    ['dag_ui', 'dag_join'], ['dag_tech', 'dag_join'], ['dag_quality', 'dag_join'],
    ['dag_join', 'dag_delivery'], ['dag_start', 'dag_join'],
  ]
  document.edges = links.map(([source, target], index) => ({ id: `dag_edge_${index}`, source, target }))
  document.viewport = { x: 32, y: 32, zoom: .9 }
  document.revision = 1
  document.updated_at = '2026-10-10T00:00:00Z'
  assert.equal(validateWorkflow(document).valid, true)
  return document
}
function assertForwardChain(document, message) {
  const byId = new Map(document.nodes.map(node => [node.id, node]))
  const ordered = nineStageOrder.map(id => byId.get(id))
  assert.ok(ordered.every(Boolean), `${message}: all stages retained`)
  assert.ok(ordered.every(node => node.y === ordered[0].y), `${message}: chain stays on one horizontal lane`)
  for (let index = 1; index < ordered.length; index++) {
    assert.ok(ordered[index].x >= ordered[index - 1].x + 240 + 24, `${message}: stages advance right with clear gaps`)
  }
  assert.deepEqual(document.edges.map(edge => [edge.source, edge.target]), nineStageOrder.slice(1).map((target, index) => [nineStageOrder[index], target]))
}
try {
  for (const width of [1440, 870, 390]) {
    const stores = new Map()
    const get = key => { if (!stores.has(key)) stores.set(key, key.startsWith('fixture-nine:') ? nineStageWorkflow() : key.startsWith('fixture-dag:') ? branchedWorkflow() : createSopWorkflow()); return stores.get(key) }
    const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 960 }, serviceWorkers: 'block' })
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url()), method = req.method(), p = url.pathname
      const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      if (url.origin !== origin) { unexpected.push(`${method} ${url.href}`); return route.abort() }
      if (p === '/__sop-fixture') return route.fulfill({ contentType: 'text/html', body: fixture })
      if (!p.startsWith('/api/')) return route.continue()
      const match = p.match(/^\/api\/v1\/code\/projects\/([^/]+)\/(.*)$/)
      const pid = match?.[1], suffix = match?.[2], key = `${pid}:${url.searchParams.get('tree') || 'main'}`
      if (suffix === 'workflow' && method === 'GET') return reply(get(key))
      if (suffix === 'workflow' && method === 'PUT') {
        const body = req.postDataJSON(), previous = get(key)
        assert.deepEqual(Object.keys(body.workflow).sort(), ['edges', 'name', 'nodes', 'schema_version', 'viewport'])
        const validation = validateWorkflow({ ...body.workflow, revision: body.expected_revision, updated_at: previous.updated_at })
        assert.equal(validation.valid, true, JSON.stringify(validation.errors))
        if (body.expected_revision !== previous.revision) return reply({ detail: { message: '版本已变化', current_revision: previous.revision } }, 409)
        const saved = { ...clone(body.workflow), revision: previous.revision + 1, updated_at: new Date().toISOString() }
        stores.set(key, saved); writes.push({ width, key, revision: saved.revision }); return reply(saved)
      }
      if (method === 'GET' && suffix === 'map') return reply({ change_seq: 1, structure: { id: 'mod:', kind: 'dir', name: pid, path: '', children: [] }, stats: {}, warnings: [], scan: {}, edges: { imports: [] } })
      if (method === 'GET' && suffix === 'feature-briefs') return reply({ missing: { total: 0 } })
      if (method === 'GET' && suffix === 'index') return reply({ change_seq: 1, scanning: false })
      if (method === 'GET' && suffix === 'features') return reply({ domains: [], totals: {} })
      unexpected.push(`${method} ${p}`); return reply({ detail: 'Unstubbed fixture route' }, 501)
    })
    const page = await context.newPage(); page.setDefaultTimeout(12000)
    page.on('pageerror', error => errors.push(error.message))
    page.on('dialog', async dialog => { nativeDialogs.push(dialog.type()); await dialog.dismiss() })
    const workflowTab = () => page.locator('.map > .bar .seg').getByRole('button', { name: '工作流', exact: true })
    const card = id => page.locator(`[data-workflow-node="${id}"]`)
    const waitSaved = () => page.waitForFunction(() => document.querySelector('.save-indicator')?.textContent.includes('已保存'))
    const closeInspector = async () => { const close = page.getByRole('button', { name: '关闭阶段详情' }); if (await close.count()) await close.click() }
    const fit = async () => { await closeInspector(); await page.locator('.canvas-controls').getByRole('button', { name: '全览', exact: true }).click() }
    await page.goto(`${base}/__sop-fixture`, { waitUntil: 'networkidle' })
    await page.locator('.map .node .label').first().waitFor({ state: 'visible' })
    assert.equal(await page.locator('.map > .bar .seg').getByRole('button', { name: '依赖', exact: true }).count(), 0)
    await workflowTab().click(); await card('requirements').waitFor()
    await page.locator('.save-indicator').getByText('默认模板', { exact: true }).waitFor()
    assert.equal(await page.locator('.workflow-node').count(), 8)
    assert.equal(await page.locator('.workflow-edge').count(), 7)
    assert.equal(writes.filter(item => item.width === width).length, 0, 'viewing default template is read-only')
    const bounds = await card('requirements').boundingBox(), canvas = await page.locator('.workflow-canvas').boundingBox()
    assert.ok(bounds.x >= canvas.x && bounds.y >= canvas.y, `first card visible at ${width}`)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: path.join(output, `workflow-${width}-readable.png`) })
    checks.push(`${width}: dependency tab replaced, eight default nodes, readable first step, no initial write/overflow`)
    await fit(); await page.screenshot({ path: path.join(output, `workflow-${width}-overview.png`) })
    const all = await page.locator('.workflow-node').evaluateAll(elements => elements.map(el => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom } }))
    const viewportBox = await page.locator('.workflow-canvas').boundingBox()
    assert.ok(all.every(r => r.left >= viewportBox.x - 1 && r.right <= viewportBox.x + viewportBox.width + 1 && r.top >= viewportBox.y - 1 && r.bottom <= viewportBox.y + viewportBox.height + 1))
    checks.push(`${width}: explicit overview fits all eight cards`)
    await card('requirements').locator('.node-header').click()
    await page.getByRole('button', { name: '交给 Lead', exact: true }).click()
    const prompts = await page.evaluate(() => window.sopEvents)
    assert.equal(prompts.length, 1); assert.ok(prompts[0].includes('验收条件：') && prompts[0].includes('需求确认'))
    assert.equal(await card('requirements').evaluate(el => el.classList.contains('done')), false)
    await page.locator('.inspector-actions').getByRole('button', { name: '开始', exact: true }).click()
    await page.getByRole('button', { name: '完成验收', exact: true }).click()
    assert.equal(await card('requirements').evaluate(el => el.classList.contains('active')), true)
    assert.ok((await page.locator('.workflow-banner.error').innerText()).includes('产物'))
    await page.locator('[data-output-field]').fill('需求清单 docs/requirements.md；核心流程与范围已确认。')
    await page.getByRole('button', { name: '完成验收', exact: true }).click()
    assert.equal(await card('requirements').evaluate(el => el.classList.contains('done')), true)
    assert.equal(await card('ui_design').evaluate(el => el.classList.contains('ready')), true)
    await waitSaved()
    checks.push(`${width}: AI prompt handoff, output gate and successor readiness`)
    await page.reload({ waitUntil: 'networkidle' }); await workflowTab().click()
    await page.waitForFunction(() => document.querySelector('[data-workflow-node="requirements"]')?.classList.contains('done'))
    await fit(); await card('requirements').locator('.node-header').click()
    assert.ok((await page.locator('[data-output-field]').inputValue()).includes('docs/requirements.md'))
    checks.push(`${width}: refresh restores node progress and output`)
    await page.locator('.map > .bar .seg').getByRole('button', { name: '结构', exact: true }).click()
    await workflowTab().click(); assert.equal(await card('requirements').evaluate(el => el.classList.contains('done')), true)
    await page.evaluate(() => window.changeProject('fixture-b'))
    await page.waitForFunction(() => document.querySelector('.save-indicator')?.textContent.includes('默认模板'))
    assert.equal(await card('requirements').evaluate(el => el.classList.contains('done')), false)
    await page.evaluate(() => window.changeProject('fixture-a'))
    await page.waitForFunction(() => document.querySelector('[data-workflow-node="requirements"]')?.classList.contains('done'))
    checks.push(`${width}: tab persistence and project isolation`)
    if (width === 870) {
      await fit()
      const header = card('requirements').locator('.node-header'), before = await header.boundingBox()
      const grabX = before.x + before.width * .4, grabY = before.y + before.height * .5
      await page.mouse.move(grabX, grabY); await page.mouse.down(); await page.mouse.move(grabX + 45, grabY + 100, { steps: 8 }); await page.mouse.up()
      const after = await header.boundingBox(); assert.ok(after.x > before.x + 35 && after.y > before.y + 20)
      await waitSaved(); checks.push('870: pointer dragging persists logical node coordinates')
      const existingIds = new Set(await page.locator('.workflow-node').evaluateAll(elements => elements.map(el => el.getAttribute('data-workflow-node'))))
      await page.getByRole('button', { name: '添加工作流节点' }).click()
      await page.getByRole('menuitem', { name: '自定义步骤' }).click()
      const customId = (await page.locator('.workflow-node').evaluateAll(elements => elements.map(el => el.getAttribute('data-workflow-node')))).find(id => !existingIds.has(id))
      assert.ok(customId, 'new node is found regardless of topological render order')
      assert.equal(await page.locator('.workflow-node').count(), 9)
      await page.locator('.inspector-field').filter({ hasText: '阶段名称' }).locator('input').fill('代码审查')
      await closeInspector(); await fit()
      const out = await card('requirements').locator('.workflow-port.output').boundingBox(), input = await card(customId).locator('.workflow-port.input').boundingBox()
      await page.mouse.move(out.x + out.width / 2, out.y + out.height / 2); await page.mouse.down(); await page.mouse.move(input.x + input.width / 2, input.y + input.height / 2, { steps: 12 }); await page.mouse.up()
      assert.equal(await page.locator('.workflow-edge').count(), 8)
      await card(customId).locator('.node-header').click(); await page.getByRole('button', { name: '交给 Lead', exact: true }).click()
      assert.ok((await page.evaluate(() => window.sopEvents)).at(-1).includes('docs/requirements.md'))
      checks.push('870: add and edit node, pointer connection, upstream artifact in AI handoff')
      await page.getByRole('button', { name: '移除节点', exact: true }).click()
      await page.getByRole('dialog').getByRole('button', { name: '移除节点', exact: true }).click()
      assert.equal(await page.locator('.workflow-node').count(), 8); assert.equal(await page.locator('.workflow-edge').count(), 7)
      await waitSaved(); checks.push('870: shared delete dialog removes node and attached edge')
      await page.getByRole('button', { name: '整理', exact: true }).click(); await fit()
      await page.screenshot({ path: path.join(output, 'workflow-870-arranged.png') })
      checks.push('870: automatic arrangement preserves workflow')

      // ComfyUI-style graph editing: exercise real keyboard/pointer input against
      // an isolated project so history, drafts and CAS remain fixture-only.
      await closeInspector()
      await page.evaluate(() => window.changeProject('fixture-comfy'))
      await page.locator('.save-indicator').getByText('默认模板', { exact: true }).waitFor()
      const undo = page.getByRole('button', { name: '撤销工作流操作', exact: true })
      const redo = page.getByRole('button', { name: '重做工作流操作', exact: true })
      const canvasFocus = () => page.locator('.workflow-canvas').focus()
      const saveAction = async action => {
        await Promise.all([
          page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname.endsWith('/fixture-comfy/workflow') && response.status() === 200),
          action(),
        ])
        await waitSaved()
      }
      const logicalPosition = id => card(id).evaluate(el => ({ x: parseFloat(el.style.left), y: parseFloat(el.style.top) }))
      const nodeIds = () => page.locator('.workflow-node').evaluateAll(elements => elements.map(el => el.getAttribute('data-workflow-node')))
      assert.equal(await undo.isDisabled(), true)
      assert.equal(await redo.isDisabled(), true)
      await page.locator('.canvas-controls').getByRole('button', { name: '阅读', exact: true }).click()
      const panPosition = await logicalPosition('requirements')
      const panHeader = await card('requirements').locator('.node-header').boundingBox()
      await canvasFocus()
      await page.keyboard.down('Space')
      await saveAction(async () => {
        await page.mouse.move(panHeader.x + panHeader.width * .45, panHeader.y + panHeader.height * .5)
        await page.mouse.down()
        await page.mouse.move(panHeader.x + panHeader.width * .45 + 70, panHeader.y + panHeader.height * .5 + 65, { steps: 8 })
        await page.mouse.up()
      })
      await page.keyboard.up('Space')
      assert.deepEqual(await logicalPosition('requirements'), panPosition, 'Space-drag over a node moves the camera rather than the stage')
      assert.equal(await page.locator('.workflow-inspector').count(), 0, 'the click emitted after Space-drag does not open node details')
      assert.equal(await undo.isDisabled(), true, 'camera moves do not add graph history')
      checks.push('870: Space-drag from a node suppresses its ensuing click and keeps the inspector closed')
      const spaceViewport = clone(get('fixture-comfy:main').viewport)
      const middleHeader = await card('requirements').locator('.node-header').boundingBox()
      await saveAction(async () => {
        await page.mouse.move(middleHeader.x + middleHeader.width * .45, middleHeader.y + middleHeader.height * .5)
        await page.mouse.down({ button: 'middle' })
        await page.mouse.move(middleHeader.x + middleHeader.width * .45 + 25, middleHeader.y + middleHeader.height * .5 + 30, { steps: 6 })
        await page.mouse.up({ button: 'middle' })
      })
      assert.deepEqual(await logicalPosition('requirements'), panPosition, 'middle-drag over a node moves the camera rather than the stage')
      assert.ok(get('fixture-comfy:main').viewport.x > spaceViewport.x + 20)
      assert.equal(await undo.isDisabled(), true)
      checks.push('870: Space-left-drag and middle-drag pan from node headers without moving stages or creating undo history')

      const dragStart = await logicalPosition('requirements')
      const dragHeader = await card('requirements').locator('.node-header').boundingBox()
      await saveAction(async () => {
        await page.mouse.move(dragHeader.x + dragHeader.width * .35, dragHeader.y + dragHeader.height * .5)
        await page.mouse.down()
        await page.mouse.move(dragHeader.x + dragHeader.width * .35 + 45, dragHeader.y + dragHeader.height * .5 + 75, { steps: 10 })
        await page.mouse.up()
      })
      const dragEnd = await logicalPosition('requirements'), dragViewport = clone(get('fixture-comfy:main').viewport)
      const dragRevision = get('fixture-comfy:main').revision
      assert.ok(dragEnd.x > dragStart.x && dragEnd.y > dragStart.y)
      await canvasFocus()
      await saveAction(() => page.keyboard.press('Control+z'))
      assert.deepEqual(await logicalPosition('requirements'), dragStart, 'one undo reverses the complete multi-move drag')
      assert.deepEqual(get('fixture-comfy:main').viewport, dragViewport, 'undo keeps the current camera')
      assert.ok(get('fixture-comfy:main').revision > dragRevision, 'undo creates a fresh CAS revision instead of rewinding it')
      assert.equal(await undo.isDisabled(), true, 'one gesture creates exactly one history entry')
      await saveAction(() => page.keyboard.press('Control+Shift+z'))
      assert.deepEqual(await logicalPosition('requirements'), dragEnd)
      await saveAction(() => undo.click())
      await canvasFocus()
      await saveAction(() => page.keyboard.press('Control+y'))
      assert.deepEqual(await logicalPosition('requirements'), dragEnd)
      checks.push('870: a drag is one undo operation; toolbar undo, Ctrl+Z, Ctrl+Shift+Z and Ctrl+Y retain viewport and increasing save revisions')

      const beforeAdding = new Set(await nodeIds())
      await saveAction(async () => {
        await page.getByRole('button', { name: '添加工作流节点' }).click()
        await page.getByRole('menuitem', { name: '自定义步骤', exact: true }).click()
      })
      const historyNode = (await nodeIds()).find(id => !beforeAdding.has(id))
      assert.ok(historyNode)
      await closeInspector()
      await saveAction(() => undo.click())
      assert.equal(await card(historyNode).count(), 0)
      await saveAction(() => redo.click())
      assert.equal(await card(historyNode).count(), 1)
      await fit()
      await card('ui_design').locator('.node-header').click()
      await saveAction(async () => {
        await page.getByRole('button', { name: '移除节点', exact: true }).click()
        await page.getByRole('dialog').getByRole('button', { name: '移除节点', exact: true }).click()
      })
      assert.equal(await card('ui_design').count(), 0)
      assert.equal(await page.locator('.workflow-edge').count(), 5)
      await canvasFocus()
      await saveAction(() => page.keyboard.press('Control+z'))
      assert.equal(await card('ui_design').count(), 1)
      assert.equal(await page.locator('.workflow-edge').count(), 7, 'undo restores both deleted predecessor/successor links')
      const comfyRevision = get('fixture-comfy:main').revision
      assert.equal(await redo.isDisabled(), false)
      await page.evaluate(() => window.changeProject('fixture-comfy-other'))
      await page.locator('.save-indicator').getByText('默认模板', { exact: true }).waitFor()
      assert.equal(await undo.isDisabled(), true)
      assert.equal(await redo.isDisabled(), true)
      await page.evaluate(() => window.changeProject('fixture-comfy'))
      await card(historyNode).waitFor()
      assert.equal(await redo.isDisabled(), false)
      assert.equal(get('fixture-comfy:main').revision, comfyRevision)
      checks.push('870: add/delete undo and redo restore nodes and attached links; project switching isolates and retains graph history')

      await fit()
      await card(historyNode).locator('.node-header').click()
      const titleInput = page.locator('.inspector-field').filter({ hasText: '阶段名称' }).locator('input')
      const oldTitle = await titleInput.inputValue(), formNodeCount = await page.locator('.workflow-node').count()
      await titleInput.focus()
      await titleInput.press('End')
      await page.keyboard.insertText('X')
      assert.equal(await titleInput.inputValue(), `${oldTitle}X`)
      await page.keyboard.press('Control+z')
      assert.equal(await titleInput.inputValue(), oldTitle, 'Ctrl+Z in a text field retains the native text edit undo')
      assert.equal(await page.locator('.workflow-node').count(), formNodeCount)
      assert.equal(await page.locator('.workflow-edge').count(), 7)
      await page.keyboard.press('Space')
      assert.equal(await titleInput.inputValue(), `${oldTitle} `, 'Space in a text field enters a space')
      await titleInput.fill(oldTitle)
      await closeInspector()
      checks.push('870: editable fields retain native Ctrl+Z and Space without undoing graph edits')

      await page.locator('.canvas-controls').getByRole('button', { name: '阅读', exact: true }).click()
      const quickCanvas = await page.locator('.workflow-canvas').boundingBox()
      const quickPoint = { x: quickCanvas.x + quickCanvas.width * .57, y: quickCanvas.y + quickCanvas.height * .63 }
      const beforeQuickIds = new Set(await nodeIds())
      const quickWorldPoint = await page.locator('.canvas-world').evaluate((el, point) => {
        const box = el.parentElement.getBoundingClientRect(), matrix = new DOMMatrix(getComputedStyle(el).transform)
        const value = new DOMPoint(point.x - box.x, point.y - box.y).matrixTransform(matrix.inverse())
        return { x: value.x, y: value.y }
      }, quickPoint)
      await page.mouse.dblclick(quickPoint.x, quickPoint.y)
      const search = page.getByRole('textbox', { name: '搜索工作流节点', exact: true })
      await search.waitFor({ state: 'visible' })
      assert.equal(await search.evaluate(el => document.activeElement === el), true)
      await search.fill('测试')
      assert.equal(await page.locator('.add-node-menu [role="menuitem"]').count(), 1)
      await saveAction(() => page.getByRole('menuitem', { name: '测试验收', exact: true }).click())
      const quickId = (await nodeIds()).find(id => !beforeQuickIds.has(id))
      assert.ok(quickId)
      const quickPosition = await logicalPosition(quickId)
      assert.ok(quickWorldPoint.x >= quickPosition.x - 1 && quickWorldPoint.x <= quickPosition.x + 241 && quickWorldPoint.y >= quickPosition.y - 1 && quickWorldPoint.y <= quickPosition.y + 191, 'quick-add places the node at the double-click position')
      assert.equal(await search.count(), 0)
      checks.push('870: double-click empty canvas focuses searchable node picker and adds the chosen type at its world position')

      await fit()
      const overviewZoom = Number((await page.locator('.zoom-value').innerText()).replace('%', '')) / 100
      assert.ok(overviewZoom < .5)
      assert.equal(await card('requirements').locator('.node-content').isVisible(), false, 'overview omits unreadable body text')
      assert.equal(await card('requirements').locator('.node-header strong').isVisible(), true)
      assert.deepEqual(await card('requirements').evaluate(el => ({ width: el.offsetWidth, height: el.offsetHeight })), { width: 240, height: 190 }, 'LOD keeps logical node and port geometry unchanged')
      await page.screenshot({ path: path.join(output, 'workflow-870-comfy-compact-overview.png') })
      await page.locator('.canvas-controls').getByRole('button', { name: '阅读', exact: true }).click()
      assert.equal(await card('requirements').locator('.node-content').isVisible(), true)
      checks.push('870: low zoom keeps titles and stage geometry while hiding unreadable node body; reading mode restores details')

      await fit()
      const graphBeforeFocus = clone({ nodes: get('fixture-comfy:main').nodes, edges: get('fixture-comfy:main').edges })
      await saveAction(() => card(quickId).locator('.node-header').dblclick())
      assert.equal(await card(quickId).evaluate(el => el.classList.contains('selected')), true)
      assert.equal(await card(quickId).locator('.node-content').isVisible(), true)
      const focusedCard = await card(quickId).boundingBox(), focusCanvas = await page.locator('.workflow-canvas').boundingBox()
      assert.ok(focusedCard.width >= 215.5 && Math.abs(focusedCard.x + focusedCard.width / 2 - (focusCanvas.x + focusCanvas.width / 2)) < 2)
      await fit()
      await canvasFocus()
      await saveAction(() => page.keyboard.press('.'))
      assert.equal(await card(quickId).locator('.node-content').isVisible(), true)
      assert.ok((await card(quickId).boundingBox()).width >= 215.5)
      assert.deepEqual({ nodes: get('fixture-comfy:main').nodes, edges: get('fixture-comfy:main').edges }, graphBeforeFocus, 'selection focusing changes only the camera')
      checks.push('870: double-clicking a node and period-key selection focus restore readable zoom, centered card and detail inspector without changing the graph')
    }
    await closeInspector()
    await page.evaluate(() => { history.replaceState(null, '', '/__sop-fixture?project=fixture-nine'); window.changeProject('fixture-nine') })
    await card('fixture_technology').waitFor()
    assert.equal(await page.locator('.workflow-node').count(), 9)
    assert.equal(await page.locator('.workflow-edge').count(), 8)
    await Promise.all([
      page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname.endsWith('/fixture-nine/workflow') && response.status() === 200),
      page.getByRole('button', { name: '整理', exact: true }).click(),
    ])
    await waitSaved()
    const arranged = clone(get('fixture-nine:main'))
    assertForwardChain(arranged, `${width}: arrangement`)
    const indices = await page.locator('.workflow-node').evaluateAll(elements => Object.fromEntries(elements.map(el => [el.getAttribute('data-workflow-node'), el.querySelector('.node-index')?.textContent.trim()])))
    nineStageOrder.forEach((id, index) => assert.equal(Number(indices[id]), index + 1, `${width}: ${id} displays its actual workflow position`))
    const firstCard = await card('requirements').boundingBox(), arrangedCanvas = await page.locator('.workflow-canvas').boundingBox()
    assert.ok(arranged.viewport.zoom >= .9 && firstCard.width >= 215.5, `${width}: arranging retains readable node size`)
    assert.ok(firstCard.x >= arrangedCanvas.x && firstCard.y >= arrangedCanvas.y, `${width}: arrangement starts at first stage`)
    assert.ok(firstCard.x + firstCard.width <= arrangedCanvas.x + arrangedCanvas.width + 1, `${width}: readable first card fits canvas`)
    await page.screenshot({ path: path.join(output, `workflow-${width}-nine-arranged-readable.png`) })
    checks.push(`${width}: screenshot-style nine-stage chain arranges left to right, keeps readable cards and follows topological numbering`)
    await page.reload({ waitUntil: 'networkidle' }); await workflowTab().click(); await card('fixture_technology').waitFor()
    await page.locator('.save-indicator').getByText('已保存', { exact: true }).waitFor()
    const restoredPositions = await page.locator('.workflow-node').evaluateAll(elements => Object.fromEntries(elements.map(el => [el.getAttribute('data-workflow-node'), { x: parseFloat(el.style.left), y: parseFloat(el.style.top) }])))
    for (const node of arranged.nodes) assert.deepEqual(restoredPositions[node.id], { x: node.x, y: node.y }, `${width}: refresh retains arranged positions`)
    assert.ok((await card('requirements').boundingBox()).width >= 215.5, `${width}: refresh retains readable zoom`)
    checks.push(`${width}: arranged nine-stage layout and readable viewport survive save and refresh`)
    const restoredFirst = await card('requirements').boundingBox(), navigationCanvas = await page.locator('.workflow-canvas').boundingBox()
    await page.mouse.move(navigationCanvas.x + navigationCanvas.width / 2, navigationCanvas.y + navigationCanvas.height / 2)
    await page.keyboard.down('Shift'); await page.mouse.wheel(0, 120); await page.keyboard.up('Shift')
    await page.waitForFunction(x => document.querySelector('[data-workflow-node="requirements"]')?.getBoundingClientRect().x < x - 60, restoredFirst.x)
    const shiftedFirst = await card('requirements').boundingBox()
    assert.ok(Math.abs(shiftedFirst.width - restoredFirst.width) < .2 && Math.abs(shiftedFirst.y - restoredFirst.y) < .2, `${width}: Shift-wheel pans horizontally without shrinking cards`)
    const minimapLast = await page.getByRole('button', { name: '工作流缩略图导航' }).locator('svg rect:not(.minimap-viewport)').last().boundingBox()
    await page.mouse.click(minimapLast.x + minimapLast.width / 2, minimapLast.y + minimapLast.height / 2)
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    const navigatedLast = await card('maintenance').boundingBox()
    const minimapButton = await page.getByRole('button', { name: '工作流缩略图导航' }).boundingBox(), minimapSvg = await page.locator('.workflow-minimap svg').boundingBox()
    assert.ok(Math.abs(navigatedLast.x + navigatedLast.width / 2 - (navigationCanvas.x + navigationCanvas.width / 2)) < 40, `${width}: minimap centers the selected location ${JSON.stringify({ minimapLast, minimapButton, minimapSvg, navigatedLast, navigationCanvas })}`)
    assert.ok((await card('maintenance').boundingBox()).width >= 215.5, `${width}: minimap navigation retains readable scale`)
    await page.screenshot({ path: path.join(output, `workflow-${width}-nine-navigation.png`) })
    await page.locator('.canvas-controls').getByRole('button', { name: '阅读', exact: true }).click()
    const readingFirst = await card('requirements').boundingBox()
    assert.ok(readingFirst.x >= navigationCanvas.x && readingFirst.x + readingFirst.width <= navigationCanvas.x + navigationCanvas.width + 1 && readingFirst.width >= 215.5, `${width}: reading mode returns to a legible first stage`)
    checks.push(`${width}: Shift-wheel navigation, minimap jumps and reading mode preserve readable cards`)
    await fit()
    await page.screenshot({ path: path.join(output, `workflow-${width}-nine-arranged-overview.png`) })
    const overviewCards = await page.locator('.workflow-node').evaluateAll(elements => elements.map(el => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom } }))
    const overviewCanvas = await page.locator('.workflow-canvas').boundingBox()
    assert.ok(overviewCards.every(r => r.left >= overviewCanvas.x - 1 && r.right <= overviewCanvas.x + overviewCanvas.width + 1 && r.top >= overviewCanvas.y - 1 && r.bottom <= overviewCanvas.y + overviewCanvas.height + 1), `${width}: explicit overview fits the whole nine-stage chain`)
    const arrangedPaths = await page.locator('.workflow-edge .edge-line').evaluateAll(elements => elements.map(el => el.getAttribute('d')))
    assert.ok(arrangedPaths.every(d => d && /\bC\b/.test(d) && !/\b[LHV]\b/.test(d)), `${width}: links use soft cubic paths without angular segments`)
    checks.push(`${width}: explicit overview fits the nine-stage chain with soft links and no return folds`)
    if (width === 870) {
      await page.evaluate(() => { history.replaceState(null, '', '/__sop-fixture?project=fixture-dag'); window.changeProject('fixture-dag') })
      await card('dag_start').waitFor()
      await Promise.all([
        page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname.endsWith('/fixture-dag/workflow') && response.status() === 200),
        page.getByRole('button', { name: '整理', exact: true }).click(),
      ])
      await waitSaved()
      const dagDocument = clone(get('fixture-dag:main'))
      const dagPositions = await page.locator('.workflow-node').evaluateAll(elements => Object.fromEntries(elements.map(el => [el.getAttribute('data-workflow-node'), { x: parseFloat(el.style.left), y: parseFloat(el.style.top) }])))
      assert.equal(Object.keys(dagPositions).length, 7)
      assert.equal(await page.locator('.workflow-edge').count(), 8)
      for (const node of dagDocument.nodes) assert.deepEqual(dagPositions[node.id], { x: node.x, y: node.y })
      const branchPositions = ['dag_ui', 'dag_tech', 'dag_quality'].map(id => dagPositions[id]).sort((a, b) => a.y - b.y)
      assert.ok(branchPositions.every(node => node.x === branchPositions[0].x), 'parallel branches share a column')
      const branchGaps = branchPositions.slice(1).map((node, index) => node.y - branchPositions[index].y)
      assert.ok(branchGaps.every(gap => gap >= 238) && Math.abs(branchGaps[0] - branchGaps[1]) < .01, 'parallel branch gaps are equal and leave breathing room')
      const midpoint = (branchPositions[0].y + branchPositions[2].y) / 2
      for (const id of ['dag_start', 'dag_join', 'dag_delivery']) assert.ok(Math.abs(dagPositions[id].y - midpoint) < .01, `${id} aligns with branch midpoint`)
      for (const edge of dagDocument.edges) assert.ok(dagPositions[edge.target].x >= dagPositions[edge.source].x + 240 + 24, `${edge.id} advances left to right`)
      const mainBottom = Math.max(...Object.entries(dagPositions).filter(([id]) => id !== 'dag_notes').map(([, node]) => node.y + 190))
      assert.ok(dagPositions.dag_notes.y >= mainBottom + 95, 'independent node occupies a separate band')
      const dagCards = Object.values(dagPositions)
      for (let index = 0; index < dagCards.length; index++) for (let other = index + 1; other < dagCards.length; other++) {
        const a = dagCards[index], b = dagCards[other]
        assert.ok(a.x + 240 <= b.x || b.x + 240 <= a.x || a.y + 190 <= b.y || b.y + 190 <= a.y, 'DAG cards do not overlap')
      }
      checks.push('870: branched DAG aligns equal branch gaps and centered split/join, with an independent node in a separate band')
      const routeCollisions = await page.locator('.workflow-edge .edge-line').evaluateAll((paths, document) => {
        const collisions = []
        for (const path of paths) {
          const id = path.closest('[data-workflow-edge]').getAttribute('data-workflow-edge'), edge = document.edges.find(edge => edge.id === id)
          const others = document.nodes.filter(node => node.id !== edge.source && node.id !== edge.target), length = path.getTotalLength()
          for (let sample = 0; sample <= 500; sample++) {
            const point = path.getPointAtLength(length * sample / 500)
            for (const node of others) if (point.x > node.x + .5 && point.x < node.x + 239.5 && point.y > node.y + .5 && point.y < node.y + 189.5) { collisions.push({ edge: id, node: node.id }); break }
            if (collisions.some(hit => hit.edge === id)) break
          }
        }
        return collisions
      }, dagDocument)
      assert.deepEqual(routeCollisions, [], 'actual SVG routes avoid unrelated cards, including the skipped layer')
      const skipPath = await page.locator('[data-workflow-edge="dag_edge_7"] .edge-line').getAttribute('d')
      assert.ok((skipPath.match(/\bC\b/g) || []).length > 1 && !/\b[LHV]\b/.test(skipPath), 'skip-layer route bends around the middle branch with soft corners')
      checks.push('870: 500 samples per rendered SVG edge verify skip-layer and branch links avoid unrelated cards')
      await fit()
      await page.screenshot({ path: path.join(output, 'workflow-870-dag-overview.png') })
      const dagOverviewCards = await page.locator('.workflow-node').evaluateAll(elements => elements.map(el => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom } }))
      const dagOverviewCanvas = await page.locator('.workflow-canvas').boundingBox()
      assert.ok(dagOverviewCards.every(r => r.left >= dagOverviewCanvas.x - 1 && r.right <= dagOverviewCanvas.x + dagOverviewCanvas.width + 1 && r.top >= dagOverviewCanvas.y - 1 && r.bottom <= dagOverviewCanvas.y + dagOverviewCanvas.height + 1), 'DAG overview contains branches, merge and detached node')
      checks.push('870: DAG overview includes the branching workflow and its detached research node')
      await card('dag_tech').locator('.node-header').click()
      assert.equal(await page.locator('.workflow-edge.related').count(), 2, 'selection emphasizes only its input and output links')
      assert.equal(await page.locator('.workflow-edge.muted').count(), 6, 'unrelated links fade without removing them')
      await page.screenshot({ path: path.join(output, 'workflow-870-comfy-focused-links.png') })
      checks.push('870: node selection emphasizes connected paths and fades unrelated links')
    }
    await context.close()
  }
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); assert.deepEqual(nativeDialogs, [])
  checks.push('no runtime errors, native dialogs, external calls or unstubbed API requests')
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ passed: true, checks, writes, errors, unexpected, nativeDialogs }, null, 2))
  console.log(JSON.stringify({ passed: true, checks: checks.length, output }))
} catch (error) {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ passed: false, error: error.stack, checks, writes, errors, unexpected, nativeDialogs }, null, 2)); throw error
} finally { await browser.close() }
