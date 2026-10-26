import assert from 'node:assert/strict'
import test from 'node:test'
import {
  NODE_TYPES, WORKFLOW_LIMITS, createSopWorkflow, createWorkflowNode, validateWorkflow,
  canConnect, connectNodes, removeNode, setNodeStatus, readyNodeIds,
  arrangeWorkflow, workflowPrompt, workflowNodeOrder, edgePath,
} from '../src/lib/sopWorkflow.js'

function graph(ids, pairs = []) {
  return {
    ...createSopWorkflow(),
    nodes: ids.map((id, index) => ({ ...createWorkflowNode('custom', 50 + index * 280, 60), id, title: `步骤 ${id}` })),
    edges: pairs.map(([source, target], index) => ({ id: `e${index}`, source, target })),
  }
}

function complete(workflow, id, output = `产物 ${id}`) {
  const draft = structuredClone(workflow)
  draft.nodes.find((node) => node.id === id).output = output
  return setNodeStatus(draft, id, 'done')
}

function samplePath(path) {
  const commands = [...path.matchAll(/([MCL])\s+([^MCL]+)/g)]
  const points = []
  let position = null
  for (const [, command, raw] of commands) {
    const values = raw.match(/-?\d+(?:\.\d+)?/g).map(Number)
    if (command === 'M') { position = values; points.push(position); continue }
    const start = position
    for (let sample = 1; sample <= 250; sample += 1) {
      const t = sample / 250, u = 1 - t
      if (command === 'L') points.push([start[0] * u + values[0] * t, start[1] * u + values[1] * t])
      else points.push([u ** 3 * start[0] + 3 * u ** 2 * t * values[0] + 3 * u * t ** 2 * values[2] + t ** 3 * values[4], u ** 3 * start[1] + 3 * u ** 2 * t * values[1] + 3 * u * t ** 2 * values[3] + t ** 3 * values[5]])
    }
    position = values.slice(-2)
  }
  return points
}

function assertRoutesAvoidOtherCards(workflow) {
  for (const edge of workflow.edges) {
    const source = workflow.nodes.find((node) => node.id === edge.source)
    const target = workflow.nodes.find((node) => node.id === edge.target)
    for (const [x, y] of samplePath(edgePath(source, target, workflow.nodes))) {
      for (const card of workflow.nodes) {
        if (card.id === edge.source || card.id === edge.target) continue
        const inside = x > card.x + 0.001 && x < card.x + 240 - 0.001 && y > card.y + 0.001 && y < card.y + 190 - 0.001
        assert.equal(inside, false, `${edge.source} → ${edge.target} crosses ${card.id} at ${x},${y}`)
      }
    }
  }
}

test('default SOP has eight named stages and a continuous left-to-right chain', () => {
  const workflow = createSopWorkflow()
  assert.equal(validateWorkflow(workflow).valid, true)
  assert.equal(workflow.nodes.length, 8)
  assert.equal(workflow.edges.length, 7)
  assert.deepEqual(workflow.nodes.map((node) => node.title), ['需求确认', 'UI与流程', '技术方案', '任务拆分', '开发实现', '测试验收', '发布交付', '维护迭代'])
  assert.deepEqual(workflow.nodes.map(({ x, y }) => [x, y]), Array.from({ length: 8 }, (_, index) => [40 + index * 344, 60]))
  assert.deepEqual(workflow.nodes.map((node) => node.id), ['requirements', 'ui_design', 'architecture', 'planning', 'implementation', 'verification', 'release', 'maintenance'])
  assert.deepEqual(workflow.edges.map((edge) => edge.id), ['edge_1', 'edge_2', 'edge_3', 'edge_4', 'edge_5', 'edge_6', 'edge_7'])
  assert.equal(workflow.updated_at, '')
  assert.ok(workflow.nodes.every((node) => node.status === 'pending' && node.output === ''))
  assert.deepEqual(readyNodeIds(workflow), [workflow.nodes[0].id])
})

test('metadata supplies real stage objectives, deliverables and acceptance criteria', () => {
  assert.equal(NODE_TYPES.length, 9)
  for (const type of NODE_TYPES.filter((entry) => entry.type !== 'custom')) {
    const node = createWorkflowNode(type.type, 8, 12)
    assert.equal(node.type, type.type)
    assert.ok(node.objective && node.deliverable && node.acceptance)
    assert.equal(node.x, 8)
    assert.equal(node.y, 12)
  }
  assert.notEqual(createWorkflowNode().id, createWorkflowNode().id)
  assert.throws(() => createWorkflowNode('invalid'), /类型/)
  assert.throws(() => createWorkflowNode('custom', Infinity, 0), /位置/)
})

test('connections reject self links, duplicates and missing endpoints', () => {
  const workflow = graph(['a', 'b'], [['a', 'b']])
  assert.equal(canConnect(workflow, 'a', 'a').valid, false)
  assert.equal(canConnect(workflow, 'a', 'b').valid, false)
  assert.equal(canConnect(workflow, 'a', 'missing').valid, false)
  assert.throws(() => connectNodes(workflow, 'a', 'b'), /已经连接/)
})

test('cycle prevention catches an indirect path through a branching graph', () => {
  const workflow = graph(['a', 'b', 'c', 'd'], [['a', 'b'], ['a', 'c'], ['b', 'd'], ['c', 'd']])
  assert.equal(canConnect(workflow, 'd', 'a').valid, false)
  assert.throws(() => connectNodes(workflow, 'd', 'a'), /循环/)
  const connected = connectNodes(workflow, 'b', 'c')
  assert.equal(connected.edges.length, 5)
  assert.equal(workflow.edges.length, 4)
})

test('a joining step waits until every predecessor is done', () => {
  let workflow = graph(['a', 'b', 'join'], [['a', 'join'], ['b', 'join']])
  assert.deepEqual(readyNodeIds(workflow), ['a', 'b'])
  workflow = complete(workflow, 'a')
  assert.deepEqual(readyNodeIds(workflow), ['b'])
  assert.throws(() => setNodeStatus(workflow, 'join', 'active'), /步骤 b/)
  assert.throws(() => complete(workflow, 'join'), /前置步骤/)
  workflow = complete(workflow, 'b')
  assert.deepEqual(readyNodeIds(workflow), ['join'])
  assert.equal(setNodeStatus(workflow, 'join', 'active').nodes.at(-1).status, 'active')
})

test('completion requires nonblank evidence and never creates automatic output', () => {
  const workflow = graph(['a'])
  assert.throws(() => setNodeStatus(workflow, 'a', 'done'), /产物或验收记录/)
  workflow.nodes[0].output = '  \n '
  assert.throws(() => setNodeStatus(workflow, 'a', 'done'), /产物或验收记录/)
  workflow.nodes[0].output = '测试通过的记录'
  const next = setNodeStatus(workflow, 'a', 'done')
  assert.equal(next.nodes[0].status, 'done')
  assert.equal(next.nodes[0].output, '测试通过的记录')
  assert.equal(workflow.nodes[0].status, 'pending')
})

test('reopening a completed stage resets its completed and active descendants, retaining evidence', () => {
  let workflow = graph(['a', 'b', 'c', 'unrelated'], [['a', 'b'], ['b', 'c']])
  workflow = complete(workflow, 'a')
  workflow = complete(workflow, 'b')
  workflow = setNodeStatus(workflow, 'c', 'active')
  workflow = complete(workflow, 'unrelated')
  workflow.nodes[2].output = '待复核的草稿'
  const next = setNodeStatus(workflow, 'a', 'active')
  assert.deepEqual(next.nodes.map((node) => node.status), ['active', 'pending', 'pending', 'done'])
  assert.deepEqual(next.nodes.map((node) => node.output), ['产物 a', '产物 b', '待复核的草稿', '产物 unrelated'])
  assert.equal(validateWorkflow(next).valid, true)
  assert.deepEqual(workflow.nodes.map((node) => node.status), ['done', 'done', 'active', 'done'])
})

test('a new prerequisite invalidates downstream status even when it was already completed', () => {
  let workflow = graph(['a', 'b', 'c', 'new'], [['a', 'b'], ['b', 'c']])
  for (const id of ['a', 'b', 'c']) workflow = complete(workflow, id)
  const next = connectNodes(workflow, 'new', 'b')
  assert.deepEqual(next.nodes.map((node) => node.status), ['done', 'pending', 'pending', 'pending'])
  assert.equal(next.nodes[1].output, '产物 b')
  assert.equal(validateWorkflow(next).valid, true)
})

test('removing a node removes incident edges and resets only affected branch statuses', () => {
  let workflow = graph(['a', 'b', 'c', 'other'], [['a', 'b'], ['b', 'c']])
  for (const id of ['a', 'b', 'c', 'other']) workflow = complete(workflow, id)
  workflow.name = '保留工作流名称'
  workflow.nodes[2].acceptance = '保留验收条件'
  const before = structuredClone(workflow)
  const next = removeNode(workflow, 'b')
  assert.deepEqual(next.nodes.map((node) => node.id), ['a', 'c', 'other'])
  assert.deepEqual(next.edges, [])
  assert.equal(next.nodes[1].status, 'pending')
  assert.equal(next.nodes[1].output, '产物 c')
  assert.equal(next.nodes[1].acceptance, '保留验收条件')
  assert.equal(next.nodes[2].status, 'done')
  assert.equal(next.name, before.name)
  assert.deepEqual(workflow, before)
})

test('arrange is deterministic across input ordering and preserves all non-position data', () => {
  const workflow = graph(['root', 'left', 'right', 'join', 'alone'], [['root', 'left'], ['root', 'right'], ['left', 'join'], ['right', 'join']])
  workflow.nodes[0].objective = '保留目标描述'
  workflow.nodes[0].deliverable = '保留交付物描述'
  workflow.nodes[0].output = '保留草稿产物'
  workflow.viewport = { x: 80, y: -35, zoom: 0.75 }
  workflow.revision = 18
  const before = structuredClone(workflow)
  const arranged = arrangeWorkflow(workflow)
  const reversed = arrangeWorkflow({ ...workflow, nodes: [...workflow.nodes].reverse(), edges: [...workflow.edges].reverse() })
  const geometry = (doc) => Object.fromEntries(doc.nodes.map(({ id, x, y }) => [id, [x, y]]).sort(([a], [b]) => a.localeCompare(b)))
  assert.deepEqual(geometry(arranged), geometry(reversed))
  assert.deepEqual(arranged.edges, before.edges)
  assert.deepEqual(arranged.viewport, before.viewport)
  assert.equal(arranged.revision, 18)
  assert.equal(arranged.updated_at, before.updated_at)
  arranged.nodes.forEach((node, index) => {
    const { x, y, ...rest } = node
    const { x: oldX, y: oldY, ...oldRest } = before.nodes[index]
    assert.deepEqual(rest, oldRest)
    assert.ok(Number.isFinite(x) && Number.isFinite(y))
  })
  assert.deepEqual(workflow, before)
  for (const edge of arranged.edges) {
    const source = arranged.nodes.find((node) => node.id === edge.source)
    const target = arranged.nodes.find((node) => node.id === edge.target)
    assert.ok(target.x > source.x)
  }
})

test('arranged cards do not overlap, including many independent roots and branch layers', () => {
  const workflow = graph(['a', 'b', 'c', 'd', 'e', 'f'], [['a', 'd'], ['b', 'd'], ['d', 'e'], ['d', 'f']])
  const arranged = arrangeWorkflow(workflow)
  for (let a = 0; a < arranged.nodes.length; a += 1) for (let b = a + 1; b < arranged.nodes.length; b += 1) {
    const first = arranged.nodes[a], second = arranged.nodes[b]
    assert.equal(first.x < second.x + 240 && second.x < first.x + 240 && first.y < second.y + 190 && second.y < first.y + 190, false)
  }
})

test('canvas width and legacy columns never fold a chain back across previous stages', () => {
  const workflow = createSopWorkflow()
  assert.deepEqual(arrangeWorkflow(workflow, { columns: 4 }), workflow)
  const compact = arrangeWorkflow(workflow, { columns: 2 })
  assert.deepEqual(compact, workflow)
  const reversed = arrangeWorkflow({ ...workflow, nodes: [...workflow.nodes].reverse(), edges: [...workflow.edges].reverse() }, { columns: 3 })
  const geometry = (doc) => Object.fromEntries(doc.nodes.map(({ id, x, y }) => [id, [x, y]]))
  assert.deepEqual(geometry(reversed), geometry(compact))
  assertRoutesAvoidOtherCards(compact)
})

test('an inserted technical-selection stage follows graph order rather than insertion order', () => {
  const workflow = createSopWorkflow()
  const technical = { ...createWorkflowNode('custom'), id: 'technical_selection', title: '技术选型' }
  workflow.nodes.push(technical)
  workflow.edges = workflow.edges.filter((edge) => edge.id !== 'edge_1')
  workflow.edges.push({ id: 'selection_input', source: 'requirements', target: technical.id }, { id: 'selection_output', source: technical.id, target: 'ui_design' })
  const order = ['requirements', 'technical_selection', 'ui_design', 'architecture', 'planning', 'implementation', 'verification', 'release', 'maintenance']
  assert.deepEqual(workflowNodeOrder(workflow), order)
  const arranged = arrangeWorkflow(workflow, { columns: 2 })
  for (const [index, id] of order.entries()) {
    const node = arranged.nodes.find((entry) => entry.id === id)
    assert.deepEqual([node.x, node.y], [40 + index * 344, 60])
  }
  assert.deepEqual(arranged.nodes.map((node) => node.id), workflow.nodes.map((node) => node.id))
  assertRoutesAvoidOtherCards(arranged)
  for (const edge of arranged.edges) assert.equal((edgePath(arranged.nodes.find((node) => node.id === edge.source), arranged.nodes.find((node) => node.id === edge.target), arranged.nodes).match(/ C /g) || []).length, 1)
})

test('presentation numbering tolerates unfinished text fields and safely falls back on malformed graphs', () => {
  const workflow = graph(['c', 'a', 'b'], [['a', 'b'], ['b', 'c']])
  workflow.nodes[0].title = ''
  workflow.nodes[1].status = 'done'
  workflow.nodes[1].output = ''
  assert.deepEqual(workflowNodeOrder(workflow), ['a', 'b', 'c'])
  workflow.edges.push({ id: 'bad', source: 'c', target: 'a' })
  assert.deepEqual(workflowNodeOrder(workflow), ['c', 'a', 'b'])
  workflow.edges[2].target = 'missing'
  assert.deepEqual(workflowNodeOrder(workflow), ['c', 'a', 'b'])
  assert.deepEqual(workflowNodeOrder(null), [])
})

test('detached nodes follow the main SOP sequence until connected between existing stages', () => {
  const workflow = createSopWorkflow()
  workflow.nodes.push({ ...createWorkflowNode('custom'), id: 'node_aaa', title: '技术选型' })
  const main = ['requirements', 'ui_design', 'architecture', 'planning', 'implementation', 'verification', 'release', 'maintenance']
  assert.deepEqual(workflowNodeOrder(workflow), [...main, 'node_aaa'])
  assert.deepEqual(workflowNodeOrder({ ...workflow, nodes: [...workflow.nodes].reverse(), edges: [...workflow.edges].reverse() }), [...main, 'node_aaa'])
  workflow.edges = workflow.edges.filter((edge) => edge.id !== 'edge_1')
  workflow.edges.push({ id: 'custom_input', source: 'requirements', target: 'node_aaa' }, { id: 'custom_output', source: 'node_aaa', target: 'ui_design' })
  assert.deepEqual(workflowNodeOrder(workflow), [main[0], 'node_aaa', ...main.slice(1)])
})

test('component numbering matches layout grouping with stable ties between independent flows', () => {
  const workflow = graph(['z2', 'z1', 'a2', 'a1', 'alone'], [['z1', 'z2'], ['a1', 'a2']])
  assert.deepEqual(workflowNodeOrder(workflow), ['a1', 'a2', 'z1', 'z2', 'alone'])
  const arranged = arrangeWorkflow(workflow)
  const byId = Object.fromEntries(arranged.nodes.map((node) => [node.id, node]))
  assert.ok(byId.a1.y < byId.z1.y && byId.z1.y < byId.alone.y)
  assert.deepEqual(workflowNodeOrder({ ...workflow, nodes: [...workflow.nodes].reverse(), edges: [...workflow.edges].reverse() }), workflowNodeOrder(workflow))
})

test('split branches are evenly spaced and their parent and merge share the midpoint', () => {
  const workflow = arrangeWorkflow(graph(['start', 'branch_a', 'branch_b', 'branch_c', 'merge', 'end'], [
    ['start', 'branch_a'], ['start', 'branch_b'], ['start', 'branch_c'],
    ['branch_a', 'merge'], ['branch_b', 'merge'], ['branch_c', 'merge'], ['merge', 'end'],
  ]))
  const byId = Object.fromEntries(workflow.nodes.map((node) => [node.id, node]))
  assert.deepEqual(['branch_a', 'branch_b', 'branch_c'].map((id) => byId[id].y), [60, 298, 536])
  for (const id of ['start', 'merge', 'end']) assert.equal(byId[id].y, 298)
  assertRoutesAvoidOtherCards(workflow)
})

test('independent workflows occupy separate compact bands instead of mixing ranks', () => {
  const workflow = arrangeWorkflow(graph(['main_1', 'main_2', 'main_3', 'aux_1', 'aux_2', 'alone'], [
    ['main_1', 'main_2'], ['main_2', 'main_3'], ['aux_1', 'aux_2'],
  ]))
  const byId = Object.fromEntries(workflow.nodes.map((node) => [node.id, node]))
  assert.deepEqual(['main_1', 'main_2', 'main_3'].map((id) => byId[id].y), [60, 60, 60])
  assert.deepEqual(['aux_1', 'aux_2'].map((id) => byId[id].y), [346, 346])
  assert.equal(byId.alone.y, 632)
  assertRoutesAvoidOtherCards(workflow)
})

test('branched and disconnected graphs retain ranked DAG layout when columns are requested', () => {
  const branch = graph(['a', 'b', 'c', 'd'], [['a', 'b'], ['a', 'c'], ['b', 'd'], ['c', 'd']])
  assert.deepEqual(arrangeWorkflow(branch, { columns: 2 }), arrangeWorkflow(branch))
  const disconnected = graph(['a', 'b', 'c'], [['a', 'b']])
  assert.deepEqual(arrangeWorkflow(disconnected, { columns: 2 }), arrangeWorkflow(disconnected))
  assert.deepEqual(arrangeWorkflow(branch, { columns: NaN }), arrangeWorkflow(branch))
})

test('prompt includes stage criteria and direct upstream evidence without claiming automatic execution', () => {
  let workflow = graph(['spec', 'build', 'unrelated'], [['spec', 'build']])
  workflow.nodes[1].objective = '实现文件保存'
  workflow.nodes[1].deliverable = '保存接口和界面'
  workflow.nodes[1].acceptance = '磁盘内容与编辑器一致'
  workflow = complete(workflow, 'spec', '已确认使用 SHA 校验防止覆盖外部修改')
  workflow.nodes[2].output = '无关秘密不应进入提示'
  const prompt = workflowPrompt(workflow, 'build')
  for (const expected of ['实现文件保存', '保存接口和界面', '磁盘内容与编辑器一致', 'SHA 校验', '用户验收后确认', '不要声称执行了未执行的操作']) assert.ok(prompt.includes(expected), expected)
  assert.equal(prompt.includes('无关秘密不应进入提示'), false)
  assert.equal(workflow.nodes[1].status, 'pending')
})

test('prompt labels unfinished predecessor evidence honestly', () => {
  const workflow = graph(['a', 'b'], [['a', 'b']])
  assert.match(workflowPrompt(workflow, 'b'), /待处理/)
  assert.match(workflowPrompt(workflow, 'b'), /尚未填写产物/)
  assert.throws(() => workflowPrompt(workflow, 'missing'), /不存在/)
})

test('validation rejects malformed imports without throwing', () => {
  const mutations = [
    (doc) => { doc.schema_version = 2 },
    (doc) => { doc.nodes = null },
    (doc) => { doc.edges = {} },
    (doc) => { doc.nodes.push(null) },
    (doc) => { doc.edges.push(null) },
    (doc) => { doc.nodes[0].id = '' },
    (doc) => { doc.nodes[1].id = doc.nodes[0].id },
    (doc) => { doc.nodes[0].type = 'shell' },
    (doc) => { doc.nodes[0].x = Infinity },
    (doc) => { doc.nodes[0].objective = {} },
    (doc) => { doc.nodes[0].status = 'executed' },
    (doc) => { doc.nodes[0].status = 'done' },
    (doc) => { doc.edges[0].target = 'missing' },
    (doc) => { doc.edges[0].target = doc.edges[0].source },
    (doc) => { doc.edges.push({ ...doc.edges[0], id: 'duplicate-pair' }) },
    (doc) => { doc.edges.push({ id: 'back', source: doc.nodes[1].id, target: doc.nodes[0].id }) },
    (doc) => { doc.viewport.zoom = 0 },
    (doc) => { doc.updated_at = 'not-a-date' },
    (doc) => { doc.revision = -1 },
  ]
  for (const mutate of mutations) {
    const workflow = graph(['a', 'b'], [['a', 'b']])
    mutate(workflow)
    const checked = validateWorkflow(workflow)
    assert.equal(checked.valid, false)
    assert.ok(checked.errors.length)
  }
  for (const input of [null, undefined, [], 3, 'json']) assert.equal(validateWorkflow(input).valid, false)
})

test('validation rejects imported active or completed nodes with unfinished prerequisites', () => {
  const workflow = graph(['a', 'b'], [['a', 'b']])
  workflow.nodes[1].status = 'active'
  assert.match(validateWorkflow(workflow).errors.join(' '), /前置步骤/)
  workflow.nodes[1].status = 'done'
  workflow.nodes[1].output = '不充分的完成记录'
  assert.match(validateWorkflow(workflow).errors.join(' '), /前置步骤/)
})

test('an empty graph is a valid editable workflow', () => {
  const workflow = graph([])
  assert.equal(validateWorkflow(workflow).valid, true)
  assert.deepEqual(readyNodeIds(workflow), [])
  assert.deepEqual(arrangeWorkflow(workflow), workflow)
})

test('server field limits allow the exact boundary and reject larger values', () => {
  const workflow = graph(['a'])
  workflow.name = 'n'.repeat(120)
  const node = workflow.nodes[0]
  node.id = 'a'.repeat(64)
  node.title = 't'.repeat(120)
  node.objective = 'o'.repeat(4000)
  node.deliverable = 'd'.repeat(4000)
  node.acceptance = 'a'.repeat(4000)
  node.output = 'x'.repeat(40000)
  node.x = -100000
  node.y = 100000
  workflow.viewport = { x: -100000, y: 100000, zoom: 0.05 }
  assert.equal(validateWorkflow(workflow).valid, true)
  workflow.viewport.zoom = 2.5
  assert.equal(validateWorkflow(workflow).valid, true)
  const mutations = [
    (doc) => { doc.name += 'n' },
    (doc) => { doc.nodes[0].id += 'a' },
    (doc) => { doc.nodes[0].title += 't' },
    (doc) => { doc.nodes[0].objective += 'o' },
    (doc) => { doc.nodes[0].deliverable += 'd' },
    (doc) => { doc.nodes[0].acceptance += 'a' },
    (doc) => { doc.nodes[0].output += 'x' },
    (doc) => { doc.nodes[0].x = -100000.01 },
    (doc) => { doc.nodes[0].y = 100000.01 },
    (doc) => { doc.viewport.x = -100001 },
    (doc) => { doc.viewport.y = 100001 },
    (doc) => { doc.viewport.zoom = 0.0499 },
    (doc) => { doc.viewport.zoom = 2.5001 },
    (doc) => { doc.revision = Number.MAX_SAFE_INTEGER + 1 },
  ]
  for (const mutate of mutations) {
    const copy = structuredClone(workflow)
    mutate(copy)
    assert.equal(validateWorkflow(copy).valid, false)
  }
  assert.throws(() => createWorkflowNode('custom', 100001, 0), /位置/)
})

test('import validation forbids unknown fields and unsafe backend identifiers', () => {
  const workflow = graph(['a', 'b'], [['a', 'b']])
  const mutations = [
    (doc) => { doc.extra = true },
    (doc) => { doc.nodes[0].extra = true },
    (doc) => { doc.edges[0].extra = true },
    (doc) => { doc.viewport.extra = true },
    (doc) => { doc.nodes[0].id = 'file:path' },
    (doc) => { doc.nodes[0].id = '中文' },
    (doc) => { doc.edges[0].id = 'edge/path' },
  ]
  for (const mutate of mutations) {
    const copy = structuredClone(workflow)
    mutate(copy)
    assert.equal(validateWorkflow(copy).valid, false)
  }
})

test('server graph count limits are enforced before saving or adding another edge', () => {
  const workflow = graph(Array.from({ length: 128 }, (_, index) => `n${index}`))
  assert.equal(validateWorkflow(workflow).valid, true)
  workflow.nodes.push({ ...createWorkflowNode(), id: 'overflow' })
  assert.equal(validateWorkflow(workflow).valid, false)
  const ids = Array.from({ length: 33 }, (_, index) => `n${index}`)
  const pairs = []
  for (let a = 0; a < ids.length; a += 1) for (let b = a + 1; b < ids.length; b += 1) pairs.push([ids[a], ids[b]])
  const dense = graph(ids, pairs.slice(0, 512))
  assert.equal(validateWorkflow(dense).valid, true)
  assert.equal(canConnect(dense, ...pairs[512]).valid, false)
  dense.edges.push({ id: 'overflow_edge', source: pairs[512][0], target: pairs[512][1] })
  assert.equal(validateWorkflow(dense).valid, false)
})

test('UTF-8 request size is bounded even when each individual field is valid', () => {
  const workflow = graph(Array.from({ length: 20 }, (_, index) => `n${index}`))
  for (const node of workflow.nodes) node.output = '中'.repeat(WORKFLOW_LIMITS.output)
  assert.match(validateWorkflow(workflow).errors.join(' '), /内容过大/)
})

test('all graph mutators reject invalid states and invalid node or status requests', () => {
  const workflow = graph(['a'])
  assert.throws(() => setNodeStatus(workflow, 'missing', 'pending'), /不存在/)
  assert.throws(() => setNodeStatus(workflow, 'a', 'invalid'), /状态/)
  assert.deepEqual(removeNode(workflow, 'missing'), workflow)
  const invalid = { ...workflow, schema_version: 9 }
  assert.throws(() => arrangeWorkflow(invalid), /版本/)
  assert.throws(() => readyNodeIds(invalid), /版本/)
  assert.throws(() => removeNode(invalid, 'a'), /版本/)
  assert.equal(canConnect(invalid, 'a', 'b').valid, false)
})

test('curved links use agreed ports and reverse links have rounded obstacle-aware bends', () => {
  const workflow = createSopWorkflow()
  for (const edge of workflow.edges) {
    const source = workflow.nodes.find((node) => node.id === edge.source)
    const target = workflow.nodes.find((node) => node.id === edge.target)
    const path = edgePath(source, target, workflow.nodes)
    assert.ok(path.startsWith(`M ${source.x + 240} ${source.y + 92} C `))
    assert.ok(path.endsWith(`${target.x} ${target.y + 92}`))
    assert.equal(/NaN|Infinity/.test(path), false)
  }
  const backward = edgePath(workflow.nodes[7], workflow.nodes[6], workflow.nodes)
  assert.ok((backward.match(/ C /g) || []).length >= 3)
  assert.equal(backward.includes(' L '), false)
  for (const [x, y] of samplePath(backward)) for (const node of workflow.nodes) {
    assert.equal(x > node.x + 0.001 && x < node.x + 239.999 && y > node.y + 0.001 && y < node.y + 189.999, false)
  }
  assert.equal(edgePath(null, workflow.nodes[0]), '')
})

test('every default SOP curve avoids all non-endpoint card interiors', () => {
  assertRoutesAvoidOtherCards(createSopWorkflow())
})

test('skip links route around an intermediate card instead of cutting through its contents', () => {
  const workflow = arrangeWorkflow(graph(['a', 'b', 'c'], [['a', 'b'], ['b', 'c'], ['a', 'c']]))
  const path = edgePath(workflow.nodes[0], workflow.nodes[2], workflow.nodes)
  assert.ok((path.match(/ C /g) || []).length > 1)
  assert.equal(path.includes(' L '), false)
  assertRoutesAvoidOtherCards(workflow)
})

test('routes avoid cards moved into a diagonal curve and handle reverse links between rows', () => {
  const workflow = graph(['source', 'target', 'blocker', 'upper_wall', 'lower_wall'], [['source', 'target']])
  Object.assign(workflow.nodes[0], { x: 700, y: 320 })
  Object.assign(workflow.nodes[1], { x: 40, y: 60 })
  Object.assign(workflow.nodes[2], { x: 390, y: 210 })
  Object.assign(workflow.nodes[3], { x: 700, y: 60 })
  Object.assign(workflow.nodes[4], { x: 40, y: 320 })
  assertRoutesAvoidOtherCards(workflow)
  Object.assign(workflow.nodes[0], { x: 40, y: 60 })
  Object.assign(workflow.nodes[1], { x: 700, y: 320 })
  Object.assign(workflow.nodes[2], { x: 390, y: 210 })
  Object.assign(workflow.nodes[3], { x: 1100, y: 60 })
  Object.assign(workflow.nodes[4], { x: 1100, y: 320 })
  const direct = edgePath(workflow.nodes[0], workflow.nodes[1])
  const avoiding = edgePath(workflow.nodes[0], workflow.nodes[1], workflow.nodes)
  assert.notEqual(avoiding, direct)
  assertRoutesAvoidOtherCards(workflow)
})

test('visibility routing can leave a blocked departure corridor before going around an obstacle', () => {
  const workflow = graph(['source', 'target', 'upper_wall', 'lower_wall', 'blocker'], [['source', 'target']])
  Object.assign(workflow.nodes[0], { x: 40, y: 300 })
  Object.assign(workflow.nodes[1], { x: 1000, y: 300 })
  Object.assign(workflow.nodes[2], { x: 280, y: 170 })
  Object.assign(workflow.nodes[3], { x: 280, y: 450 })
  Object.assign(workflow.nodes[4], { x: 600, y: 300 })
  assertRoutesAvoidOtherCards(workflow)
  const path = edgePath(workflow.nodes[0], workflow.nodes[1], workflow.nodes)
  assert.ok((path.match(/ C /g) || []).length >= 5)
  assert.equal(path.includes(' L '), false)
})

test('reverse connections remain clear in previously saved layouts with a narrow 40px card gap', () => {
  const workflow = graph(['left', 'middle', 'right'], [['middle', 'left']])
  Object.assign(workflow.nodes[0], { x: 40, y: 60 })
  Object.assign(workflow.nodes[1], { x: 320, y: 60 })
  Object.assign(workflow.nodes[2], { x: 600, y: 60 })
  assertRoutesAvoidOtherCards(workflow)
  const path = edgePath(workflow.nodes[1], workflow.nodes[0], workflow.nodes)
  for (const [x, y] of samplePath(path)) for (const node of workflow.nodes) {
    assert.equal(x > node.x + 0.001 && x < node.x + 239.999 && y > node.y + 0.001 && y < node.y + 189.999, false)
  }
})

test('routing at coordinate limits stays finite and never mutates saved geometry', () => {
  const workflow = graph(['a', 'b', 'blocker'], [['a', 'b']])
  Object.assign(workflow.nodes[0], { x: 100000, y: 100000 })
  Object.assign(workflow.nodes[1], { x: -100000, y: -100000 })
  Object.assign(workflow.nodes[2], { x: 40, y: 60 })
  const before = structuredClone(workflow)
  const path = edgePath(workflow.nodes[0], workflow.nodes[1], workflow.nodes)
  assert.equal(/NaN|Infinity/.test(path), false)
  assert.deepEqual(workflow, before)
  assertRoutesAvoidOtherCards(workflow)
})
