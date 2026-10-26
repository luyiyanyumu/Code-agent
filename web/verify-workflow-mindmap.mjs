// Production SOP renderer against private, fully mocked workflow HTTP.
// Virtual branches are presentation only: no agent/model/task dispatch occurs.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSopWorkflow, createWorkflowNode, validateWorkflow } from './src/lib/sopWorkflow.js'

const root = path.dirname(fileURLToPath(import.meta.url))
const base = 'http://127.0.0.1:5198', origin = new URL(base).origin
const output = path.resolve(process.argv[2] || path.join(root, 'shots/workflow-mindmap'))
const checks = [], errors = [], unexpected = [], mutations = [], stores = new Map(), conflicts = new Set(), delayedReads = new Map()
const clone = value => JSON.parse(JSON.stringify(value))
function fixtureWorkflow(key) {
  const document = createSopWorkflow()
  document.name = `模拟工作流 ${key}`
  if (key.startsWith('dag:')) {
    document.nodes = [
      ['dag_start', 'requirements', '需求入口', 'done', 'fixture/requirements.md'],
      ['dag_ui', 'ui_design', '界面分支', 'done', 'fixture/ui.md'],
      ['dag_api', 'architecture', '接口分支', 'pending', ''],
      ['dag_join', 'implementation', '合流开发', 'pending', ''],
      ['dag_release', 'release', '发布交付', 'pending', ''],
    ].map(([id, type, title, status, result], index) => ({ ...createWorkflowNode(type, 40 + index * 344, 60), id, title, status, output: result }))
    document.edges = [['dag_start', 'dag_ui'], ['dag_start', 'dag_api'], ['dag_ui', 'dag_join'], ['dag_api', 'dag_join'], ['dag_join', 'dag_release']]
      .map(([source, target], index) => ({ id: `dag_edge_${index}`, source, target }))
    document.revision = 1
  }
  document.updated_at = document.revision ? '2026-10-13T00:00:00Z' : ''
  assert.equal(validateWorkflow(document).valid, true)
  return document
}
const get = key => { if (!stores.has(key)) stores.set(key, fixtureWorkflow(key)); return stores.get(key) }
const shape = document => ({ nodes: clone(document.nodes), edges: clone(document.edges) })
let vite, browser, failure
await fs.mkdir(output, { recursive: true })
try {
  vite = await createServer({ configFile: false, root, plugins: [vue()], cacheDir: path.join(root, 'node_modules/.vite-workflow-mindmap-fixture'),
    server: { host: '127.0.0.1', port: 5198, strictPort: true, proxy: { '/api': { target: 'http://127.0.0.1:9', ws: true }, '/healthz': 'http://127.0.0.1:9' } },
    optimizeDeps: { include: ['vue'] }, logLevel: 'warn' })
  await vite.listen()
  const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
  const { chromium } = loaded.default || loaded
  const source = await (await fetch(`${base}/src/components/code/SopWorkflow.vue`)).text()
  const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
  assert.ok(vuePath, 'private fixture Vite must serve Vue')
  const fixture = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body,#fixture{height:100%;width:100%;overflow:hidden;margin:0}</style></head><body><div id="fixture"></div><script type="module">
import{createApp,h,ref}from'${vuePath}';import Workflow from'/src/components/code/SopWorkflow.vue';import DialogHost from'/src/components/ui/DialogHost.vue';import'/src/style.css';
const projectId=ref(new URL(location.href).searchParams.get('project')||'default'),tree=ref('main');window.fixtureEvents={ask:[],agent:[],refresh:[]};window.changeProject=id=>projectId.value=id;window.changeTree=id=>tree.value=id;
createApp({render:()=>h('main',{style:'display:flex;flex-direction:column;height:100%;min-height:0'},[h(Workflow,{projectId:projectId.value,tree:tree.value,agents:[{session_id:'fixture-agent',role:'node',node_id:'requirements',state:'idle'}],onAsk:value=>window.fixtureEvents.ask.push(value),onAgent:value=>window.fixtureEvents.agent.push(value),onTeamRefresh:value=>window.fixtureEvents.refresh.push(value)}),h(DialogHost)])}).mount('#fixture');
</script></body></html>`
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' })
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), p = url.pathname, method = req.method()
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (url.origin !== origin) { unexpected.push(`${method} ${url.href}`); return route.abort() }
    if (p === '/__workflow-mindmap-fixture') return route.fulfill({ contentType: 'text/html', body: fixture })
    if (!p.startsWith('/api/')) return route.continue()
    const match = p.match(/^\/api\/v1\/code\/projects\/([^/]+)\/workflow$/)
    if (!match) { unexpected.push(`${method} ${p}`); return reply({ detail: 'Only mock workflow GET/PUT is permitted' }, 501) }
    const key = `${decodeURIComponent(match[1])}:${url.searchParams.get('tree') || 'main'}`
    if (method === 'GET') {
      if (delayedReads.has(key)) await delayedReads.get(key)
      return reply(clone(get(key)))
    }
    if (method !== 'PUT') { unexpected.push(`${method} ${p}`); return reply({ detail: 'No dispatch is permitted' }, 501) }
    const body = req.postDataJSON(), previous = get(key)
    const fields = ['edges', 'name', 'nodes', 'schema_version', 'viewport']
    assert.deepEqual(Object.keys(body.workflow).sort(), fields)
    assert.equal(validateWorkflow({ ...body.workflow, revision: body.expected_revision, updated_at: previous.updated_at }).valid, true)
    assert.ok(body.workflow.nodes.every(node => !node.id.includes('visual-root') && !node.id.includes('detail:')))
    assert.ok(body.workflow.edges.every(edge => body.workflow.nodes.some(node => node.id === edge.source) && body.workflow.nodes.some(node => node.id === edge.target)))
    mutations.push({ method, key, expected_revision: body.expected_revision, nodes: body.workflow.nodes.length, edges: body.workflow.edges.length, conflicted: conflicts.has(key) })
    if (conflicts.delete(key) || body.expected_revision !== previous.revision) {
      if (previous.revision === body.expected_revision) stores.set(key, { ...clone(previous), revision: previous.revision + 1 })
      return reply({ detail: { message: '模拟版本已变化', current_revision: get(key).revision } }, 409)
    }
    const saved = { ...clone(body.workflow), revision: previous.revision + 1, updated_at: '2026-10-13T00:00:01Z' }
    stores.set(key, saved)
    return reply(saved)
  })
  const page = await context.newPage()
  page.setDefaultTimeout(12000)
  page.on('pageerror', error => errors.push(error.stack || error.message))
  page.on('dialog', dialog => { unexpected.push('native dialog'); void dialog.dismiss() })
  const card = id => page.locator(`[data-workflow-node="${id}"]`)
  const positions = () => page.locator('.workflow-node').evaluateAll(nodes => Object.fromEntries(nodes.map(node => [node.dataset.workflowNode, { x: parseFloat(node.style.left), y: parseFloat(node.style.top) }])))
  const rootCard = page.locator('[data-workflow-root]')
  const waitLoaded = () => page.waitForFunction(() => document.querySelector('.save-indicator')?.textContent && !document.querySelector('.canvas-loading'))
  const waitSaved = () => page.waitForFunction(() => document.querySelector('.save-indicator')?.textContent.includes('已保存'))
  const switchProject = async (id, tree = 'main') => { await page.evaluate(({ id, tree }) => { window.changeProject(id); window.changeTree(tree) }, { id, tree }); await page.waitForFunction(label => document.querySelector('[data-workflow-root] strong')?.textContent === label && !document.querySelector('.canvas-loading'), `模拟工作流 ${id}:${tree}`) }
  const closeInspector = async () => { const button = page.getByRole('button', { name: '关闭阶段详情', exact: true }); if (await button.count()) await button.click() }
  const fit = async () => { await closeInspector(); await page.getByRole('button', { name: '全览', exact: true }).click() }
  const saveNow = async () => { await page.locator('.workflow-canvas').focus(); await page.keyboard.press('Control+s'); await waitSaved() }
  const assertArrowsAvoidRoot = async () => {
    const blocked = await page.evaluate(() => {
      const root = document.querySelector('[data-workflow-root]')
      const box = { x: parseFloat(root.style.left), y: parseFloat(root.style.top), width: parseFloat(root.style.width), height: parseFloat(root.style.height) }
      return [...document.querySelectorAll('.workflow-edge .edge-line')].filter(path => {
        const length = path.getTotalLength()
        for (let position = 0; position <= length; position += 2) {
          const point = path.getPointAtLength(position)
          if (point.x > box.x + .5 && point.x < box.x + box.width - .5 && point.y > box.y + .5 && point.y < box.y + box.height - .5) return true
        }
        return false
      }).map(path => path.parentElement.dataset.workflowEdge)
    })
    assert.deepEqual(blocked, [], 'execution arrows must route around the central topic')
  }
  const drag = async (id, dx, dy, cancel = false) => {
    const box = await card(id).locator('.node-header').boundingBox()
    assert.ok(box, 'stage header is available')
    const x = box.x + box.width * .45, y = box.y + box.height * .5
    await page.mouse.move(x, y); await page.mouse.down(); await page.mouse.move(x + dx, y + dy, { steps: 8 })
    if (cancel) await page.keyboard.press('Escape')
    await page.mouse.up()
  }

  await page.goto(`${base}/__workflow-mindmap-fixture`, { waitUntil: 'networkidle' })
  await waitLoaded()
  assert.equal(await rootCard.count(), 1)
  assert.equal(await page.locator('.workflow-node').count(), 8)
  assert.equal(await page.locator('.mindmap-branch').count(), 8)
  assert.equal(await page.locator('.workflow-edge').count(), 7)
  assert.equal(await page.locator('.workflow-edge:visible').count(), 0)
  const initialPositions = await positions(), rootPosition = await rootCard.evaluate(node => ({ x: parseFloat(node.style.left), y: parseFloat(node.style.top) }))
  assert.equal(Object.values(initialPositions).filter(value => value.x < rootPosition.x).length, 4)
  assert.equal(Object.values(initialPositions).filter(value => value.x > rootPosition.x).length, 4)
  await page.waitForTimeout(1000)
  assert.equal(mutations.length, 0, 'read-only legacy projection must not save transformed coordinates')
  checks.push('legacy workflow opens as one central topic and eight balanced stage branches, with seven original dependencies retained and no initial writes')
  await page.screenshot({ path: path.join(output, 'workflow-mindmap-desktop.png') })

  const beforeExpand = shape(get('default:main'))
  await card('requirements').getByRole('button', { name: '展开需求确认分支', exact: true }).click()
  assert.equal(await page.locator('[data-workflow-detail^="requirements:"]').count(), 4)
  assert.deepEqual(await page.locator('.detail-label').allTextContents(), ['目标', '交付物', '验收标准', '已记录产出'])
  assert.equal(await page.locator('.mindmap-detail-branch').count(), 4)
  await page.waitForTimeout(950)
  assert.equal(mutations.length, 0)
  assert.deepEqual(shape(get('default:main')), beforeExpand)
  checks.push('expanding objective, deliverable, acceptance and output creates virtual leaves only, without modifying executable nodes, dependencies or server state')
  await fit(); await saveNow()
  await page.screenshot({ path: path.join(output, 'workflow-mindmap-expanded.png') })
  await page.locator('[data-workflow-detail="requirements:objective"]').focus()
  await page.keyboard.press('Enter')
  await page.waitForFunction(() => document.activeElement?.dataset.workflowField === 'objective')
  assert.equal(await page.locator('[data-workflow-field="objective"]').inputValue(), get('default:main').nodes[0].objective)
  await closeInspector()
  await card('requirements').getByRole('button', { name: '收起需求确认分支', exact: true }).focus()
  await page.keyboard.press('Enter')
  assert.equal(await page.locator('.mindmap-detail').count(), 0)
  await card('requirements').locator('.node-header').focus(); await page.keyboard.press('Space')
  assert.equal(await page.locator('.workflow-inspector').count(), 1)
  checks.push('keyboard expansion and leaf activation open the matching editable field; Enter and Space stage actions remain accessible')

  await page.getByRole('button', { name: '节点智能体', exact: false }).click()
  assert.deepEqual(await page.evaluate(() => window.fixtureEvents.agent.at(-1)), { session_id: 'fixture-agent', role: 'node', node_id: 'requirements', state: 'idle' })
  await page.getByRole('button', { name: '交给 Lead', exact: true }).click()
  assert.ok((await page.evaluate(() => window.fixtureEvents.ask.at(-1))).includes('需求确认'))
  assert.ok(mutations.every(item => item.method === 'PUT'), 'handoff emits an event without API dispatch')
  checks.push('stage agent entry and Lead handoff emit their original events only, without task/model/team POST requests')
  await page.locator('.inspector-actions').getByRole('button', { name: '开始', exact: true }).click()
  await page.getByRole('button', { name: '完成验收', exact: true }).click()
  assert.ok((await page.locator('.workflow-banner.error').innerText()).includes('产物'))
  assert.equal(await card('requirements').evaluate(node => node.classList.contains('done')), false)
  await page.locator('[data-output-field]').fill('fixture/requirements.md：模拟需求确认与验收记录。')
  await page.getByRole('button', { name: '完成验收', exact: true }).click()
  assert.equal(await card('requirements').evaluate(node => node.classList.contains('done')), true)
  assert.equal(await card('ui_design').evaluate(node => node.classList.contains('ready')), true)
  await saveNow()
  assert.equal(get('default:main').nodes.length, 8)
  assert.equal(get('default:main').edges.length, 7)
  checks.push('acceptance still requires recorded output, and only accepted predecessors unlock the next phase; persisted schema excludes visual root and leaves')

  await switchProject('dag')
  await card('dag_join').locator('.node-header').click()
  assert.equal(await page.locator('.inspector-actions').getByRole('button', { name: '开始', exact: true }).isDisabled(), true)
  assert.equal(await page.getByRole('button', { name: '交给 Lead', exact: true }).isDisabled(), true)
  assert.ok((await page.locator('.dependency-section').innerText()).includes('界面分支'))
  assert.ok((await page.locator('.dependency-section').innerText()).includes('接口分支'))
  const originalDagEdges = clone(get('dag:main').edges)
  await closeInspector()
  await card('dag_api').locator('.node-header').click()
  await page.locator('.inspector-actions').getByRole('button', { name: '开始', exact: true }).click()
  await page.locator('[data-output-field]').fill('fixture/api.md：模拟接口约定已完成。')
  await page.getByRole('button', { name: '完成验收', exact: true }).click()
  assert.equal(await card('dag_join').evaluate(node => node.classList.contains('ready')), true)
  await saveNow()
  assert.deepEqual(get('dag:main').edges, originalDagEdges)
  checks.push('DAG merge phase remains gated until both branches are accepted, with all original branch and merge dependencies preserved')
  await closeInspector()
  const beforeDependencyToggle = mutations.length
  await page.getByRole('button', { name: '显示全部执行依赖', exact: true }).click()
  assert.equal(await page.locator('.workflow-edge:visible').count(), 5)
  assert.equal(await page.locator('.mindmap-branch').count(), 5)
  await assertArrowsAvoidRoot()
  await page.getByRole('button', { name: '显示全部执行依赖', exact: true }).click()
  await page.waitForTimeout(950)
  assert.equal(mutations.length, beforeDependencyToggle)
  checks.push('execution dependency toggle reveals arrows routed around the central topic, without hiding topic branches or writing presentation flags to workflow storage')
  const dagState = get('dag:main').nodes.map(node => [node.id, node.status, node.output])
  await page.getByRole('button', { name: '整理', exact: true }).click(); await saveNow()
  assert.deepEqual(get('dag:main').edges, originalDagEdges)
  assert.deepEqual(get('dag:main').nodes.map(node => [node.id, node.status, node.output]), dagState)
  await assertArrowsAvoidRoot()
  checks.push('explicit mind-map arrangement saves positions while retaining DAG structure, stage progress and acceptance evidence')
  await fit(); await page.screenshot({ path: path.join(output, 'workflow-mindmap-dag.png') })

  await switchProject('drag')
  const beforeDrag = await positions(), storedBeforeDrag = shape(get('drag:main')), writesBeforeDrag = mutations.length
  await drag('requirements', 70, 55, true)
  await page.waitForTimeout(950)
  assert.deepEqual(await positions(), beforeDrag)
  assert.deepEqual(shape(get('drag:main')), storedBeforeDrag)
  assert.equal(mutations.length, writesBeforeDrag)
  checks.push('Escape cancels a projected-node drag and restores every original coordinate without saving or adding graph history')
  await drag('requirements', 75, 45); await saveNow()
  const draggedPositions = await positions()
  assert.ok(draggedPositions.requirements.x > beforeDrag.requirements.x && draggedPositions.requirements.y > beforeDrag.requirements.y)
  assert.deepEqual(Object.fromEntries(get('drag:main').nodes.map(node => [node.id, { x: node.x, y: node.y }])), draggedPositions)
  await page.getByRole('button', { name: '撤销工作流操作', exact: true }).click(); await saveNow()
  assert.deepEqual(await positions(), beforeDrag)
  assert.deepEqual(shape(get('drag:main')), storedBeforeDrag)
  checks.push('completed drag materializes the whole visual projection consistently, and undo restores the exact legacy graph and projected layout')
  await page.getByRole('button', { name: '重做工作流操作', exact: true }).click(); await saveNow()
  assert.deepEqual(await positions(), draggedPositions)
  await page.reload({ waitUntil: 'networkidle' }); await switchProject('drag')
  assert.deepEqual(await positions(), draggedPositions)
  checks.push('redo and refresh preserve saved custom stage positions without applying automatic projection a second time')

  await card('requirements').getByRole('button', { name: '展开需求确认分支', exact: true }).click()
  await switchProject('drag', 'other')
  assert.equal(await page.locator('.mindmap-detail').count(), 0)
  assert.notDeepEqual(await positions(), draggedPositions)
  await switchProject('elsewhere')
  assert.equal(await card('requirements').evaluate(node => node.classList.contains('done')), false)
  await switchProject('drag')
  assert.equal(await page.locator('.mindmap-detail').count(), 4)
  assert.deepEqual(await positions(), draggedPositions)
  checks.push('project and tree contexts isolate progress, saved positions and expanded branches, while returning restores the original context')

  await switchProject('conflict')
  conflicts.add('conflict:main')
  await card('requirements').locator('.node-header').click()
  await page.locator('.inspector-field').filter({ hasText: '阶段名称' }).locator('input').fill('模拟保留草稿阶段')
  await page.locator('.workflow-canvas').focus(); await page.keyboard.press('Control+s')
  await page.locator('.workflow-banner.warning').waitFor()
  const draft = await page.evaluate(() => JSON.parse(localStorage.getItem('code-agent-sop-draft-v1:conflict:main')))
  assert.equal(draft.document.nodes.find(node => node.id === 'requirements').title, '模拟保留草稿阶段')
  assert.equal(draft.document.nodes.length, 8)
  await page.reload({ waitUntil: 'networkidle' }); await switchProject('conflict')
  await page.locator('.workflow-banner.warning').waitFor()
  assert.equal(await card('requirements').locator('.node-header strong').textContent(), '模拟保留草稿阶段')
  checks.push('409 conflicts preserve a valid local draft without adding virtual entities, and refresh retains the draft and explicit conflict state')

  let releaseRead
  delayedReads.set('late:main', new Promise(resolve => { releaseRead = resolve }))
  await page.evaluate(() => window.changeProject('late'))
  await page.locator('.canvas-loading').waitFor()
  await switchProject('fresh')
  releaseRead(); delayedReads.delete('late:main')
  await page.waitForTimeout(250)
  assert.equal(await rootCard.locator('strong').textContent(), '模拟工作流 fresh:main')
  checks.push('late workflow reads from a previous project cannot overwrite the current mind-map context')

  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 })
    await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
    await switchProject(`narrow-${width}`)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    assert.ok(await page.locator('.workflow-toolbar').evaluate(node => node.scrollWidth <= node.clientWidth + 1))
    if (width === 390) {
      const beforeExpansion = await page.locator('.canvas-world').getAttribute('style'), beforeWrites = mutations.length
      await card('requirements').getByRole('button', { name: '展开需求确认分支', exact: true }).click()
      await page.waitForFunction(previous => document.querySelector('.canvas-world')?.getAttribute('style') !== previous, beforeExpansion)
      const afterExpansion = await page.locator('.canvas-world').getAttribute('style')
      const canvas = await page.locator('.workflow-canvas').boundingBox()
      const boxes = await page.locator('.workflow-node,[data-workflow-root],.mindmap-detail').evaluateAll(elements => elements.map(node => { const box = node.getBoundingClientRect(); return { left: box.left, right: box.right, top: box.top, bottom: box.bottom } }))
      assert.ok(boxes.every(box => box.left >= canvas.x - 1 && box.right <= canvas.x + canvas.width + 1 && box.top >= canvas.y - 1 && box.bottom <= canvas.y + canvas.height + 1))
      await card('requirements').getByRole('button', { name: '收起需求确认分支', exact: true }).click()
      assert.equal(await page.locator('.canvas-world').getAttribute('style'), afterExpansion)
      await page.waitForTimeout(950)
      assert.equal(mutations.length, beforeWrites)
      checks.push('narrow-canvas expansion automatically fits all virtual leaves, collapse preserves the viewport, and neither presentation action sends PUT')
    }
    const header = card('requirements').locator('.node-header')
    await header.focus(); await page.keyboard.press('Enter')
    assert.equal(await page.locator('.workflow-inspector').count(), 1)
    assert.ok(await page.locator('.workflow-inspector').evaluate(node => { const rect = node.getBoundingClientRect(); return rect.left >= 0 && rect.right <= innerWidth + 1 }))
    await page.screenshot({ path: path.join(output, `workflow-mindmap-dark-${width}-details.png`) })
    await fit()
    const canvas = await page.locator('.workflow-canvas').boundingBox()
    const nodes = await page.locator('.workflow-node,[data-workflow-root]').evaluateAll(elements => elements.map(node => { const box = node.getBoundingClientRect(); return { left: box.left, right: box.right, top: box.top, bottom: box.bottom } }))
    assert.ok(nodes.every(box => box.left >= canvas.x - 1 && box.right <= canvas.x + canvas.width + 1 && box.top >= canvas.y - 1 && box.bottom <= canvas.y + canvas.height + 1))
    await page.screenshot({ path: path.join(output, `workflow-mindmap-dark-${width}.png`) })
    checks.push(`${width}px dark view has no page or toolbar overflow and stage details remain keyboard-accessible within the viewport`)
  }
  assert.deepEqual(errors, [])
  assert.deepEqual(unexpected, [])
  assert.ok(mutations.every(item => item.method === 'PUT' && item.nodes > 0), 'all writes belong solely to legal in-memory workflow fixtures')
  await context.close()
} catch (error) {
  failure = { message: error.message, stack: error.stack }
} finally {
  await browser?.close()
  await vite?.close()
  const report = { ok: !failure, checks, errors, unexpected, mutations, ...(failure ? { failure } : {}) }
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
}
if (failure) process.exitCode = 1
