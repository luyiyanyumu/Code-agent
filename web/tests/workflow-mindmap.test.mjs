import assert from 'node:assert/strict'
import test from 'node:test'
import { createSopWorkflow, createWorkflowNode, validateWorkflow, readyNodeIds } from '../src/lib/sopWorkflow.js'
import { projectWorkflowMindMap, arrangeWorkflowMindMap, workflowDependencyPath, isLegacyWorkflowChain } from '../src/lib/workflowMindMap.js'

const overlaps = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height
const inside = (bounds, rect) => rect.x >= bounds.left && rect.y >= bounds.top && rect.x + rect.width <= bounds.right && rect.y + rect.height <= bounds.bottom

test('default SOP is projected around one root without changing its execution document', () => {
  const document = createSopWorkflow(), original = structuredClone(document)
  const view = projectWorkflowMindMap(document)
  assert.equal(view.projected, true)
  assert.equal(view.stages.size, 8)
  assert.equal(view.branches.length, 8)
  assert.equal(view.root.title, document.name)
  assert.equal([...view.stages.values()].filter(stage => stage.side === 'left').length, 4)
  assert.equal([...view.stages.values()].filter(stage => stage.side === 'right').length, 4)
  assert.deepEqual(document, original)
  assert.deepEqual(readyNodeIds(document), ['requirements'])
  assert.ok(view.branches.every(branch => branch.target && branch.id.startsWith('branch:')))
  const boxes = [view.root, ...view.stages.values()]
  for (const box of boxes) assert.ok(inside(view.bounds, box))
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) assert.equal(overlaps(boxes[i], boxes[j]), false)
})

test('expanded fields are visual leaves with bounded preview and complete source text', () => {
  const document = createSopWorkflow()
  document.nodes[0].output = 'complete original report\n' + 'details '.repeat(1000)
  const view = projectWorkflowMindMap(document, { expandedIds: new Set(['requirements', 'ui_design']) })
  const stage = view.stages.get('requirements')
  assert.deepEqual(stage.details.map(item => item.key), ['objective', 'deliverable', 'acceptance', 'output'])
  assert.equal(stage.details[3].fullText, document.nodes[0].output)
  assert.ok(stage.details[3].text.length <= 80)
  assert.ok(stage.details[3].text.endsWith('…'))
  assert.equal(document.nodes.length, 8)
  assert.equal(document.edges.length, 7)
  const boxes = [view.root, ...[...view.stages.values()].flatMap(item => [item, ...item.details])]
  for (const box of boxes) assert.ok(inside(view.bounds, box))
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) assert.equal(overlaps(boxes[i], boxes[j]), false, `${i} overlaps ${j}`)
})

test('mindmap arrangement changes only persistent stage coordinates, preserving statuses and DAG dependencies', () => {
  const document = createSopWorkflow(), original = structuredClone(document)
  document.nodes[0].status = 'done'; document.nodes[0].output = 'accepted requirement list'
  const beforeReady = readyNodeIds(document)
  const arranged = arrangeWorkflowMindMap(document)
  assert.equal(validateWorkflow(arranged).valid, true)
  assert.deepEqual(arranged.edges, original.edges)
  assert.deepEqual(arranged.viewport, original.viewport)
  assert.deepEqual(readyNodeIds(arranged), beforeReady)
  for (const node of arranged.nodes) {
    const old = document.nodes.find(item => item.id === node.id)
    assert.deepEqual(Object.fromEntries(Object.entries(node).filter(([key]) => !['x', 'y'].includes(key))), Object.fromEntries(Object.entries(old).filter(([key]) => !['x', 'y'].includes(key))))
  }
  assert.equal(isLegacyWorkflowChain(arranged), false)
  assert.deepEqual(document.nodes.map(node => [node.x, node.y]), original.nodes.map(node => [node.x, node.y]))
})

test('saved free placement survives refresh and detail expansion, with leaves packed separately', () => {
  const document = arrangeWorkflowMindMap(createSopWorkflow())
  document.nodes[0].x -= 37; document.nodes[0].y += 51
  const view = projectWorkflowMindMap(document, { expandedIds: new Set(document.nodes.map(node => node.id)) })
  assert.equal(view.projected, false)
  for (const node of document.nodes) assert.deepEqual([view.stages.get(node.id).x, view.stages.get(node.id).y], [node.x, node.y])
  const detailBoxes = [...view.stages.values()].flatMap(stage => stage.details)
  for (let i = 0; i < detailBoxes.length; i++) for (let j = i + 1; j < detailBoxes.length; j++) assert.equal(overlaps(detailBoxes[i], detailBoxes[j]), false)
})

test('expanded details stay outside all real stages in a saved multi-column free layout', () => {
  const document = { ...createSopWorkflow(), nodes: [
    { ...createWorkflowNode('custom', 1000, 0), id: 'a' },
    { ...createWorkflowNode('custom', 1400, 100), id: 'b' },
  ], edges: [] }
  const view = projectWorkflowMindMap(document, { expandedIds: new Set(['a']) })
  assert.equal(view.projected, false)
  for (const detail of view.stages.get('a').details) for (const stage of view.stages.values()) assert.equal(overlaps(detail, stage), false)
  assert.deepEqual([view.stages.get('a').x, view.stages.get('a').y], [1000, 0])
  assert.deepEqual([view.stages.get('b').x, view.stages.get('b').y], [1400, 100])
})

test('single custom stage keeps its manually placed position, while empty graphs are safe', () => {
  const empty = { ...createSopWorkflow(), nodes: [], edges: [] }
  assert.equal(projectWorkflowMindMap(null).stages.size, 0)
  assert.equal(projectWorkflowMindMap(empty).branches.length, 0)
  assert.ok(projectWorkflowMindMap(null).bounds.width > 0)
  const node = { ...createWorkflowNode('custom', 560, -240), id: 'manual' }
  const document = { ...empty, nodes: [node] }
  assert.equal(projectWorkflowMindMap(document).projected, false)
  assert.equal(projectWorkflowMindMap(document).stages.get('manual').y, -240)
})

test('branch and merge remain execution dependencies rather than being replaced by root branches', () => {
  const document = createSopWorkflow()
  document.edges = [
    { id: 'a', source: 'requirements', target: 'ui_design' },
    { id: 'b', source: 'requirements', target: 'architecture' },
    { id: 'c', source: 'ui_design', target: 'implementation' },
    { id: 'd', source: 'architecture', target: 'implementation' },
  ]
  const original = structuredClone(document)
  const arranged = arrangeWorkflowMindMap(document)
  const view = projectWorkflowMindMap(arranged)
  for (const [index, edge] of document.edges.entries()) {
    const source = view.stages.get(edge.source), target = view.stages.get(edge.target)
    const path = workflowDependencyPath(source, target, index)
    assert.ok(path.startsWith(`M ${source.output.x} ${source.output.y}`))
    assert.ok(path.includes(`${target.input.x} ${target.input.y}`) || path.endsWith(`H ${target.input.x}`))
    assert.ok(!/NaN|undefined|Infinity/.test(path))
  }
  assert.deepEqual(document, original)
  assert.deepEqual(arranged.edges, original.edges)
  assert.equal(workflowDependencyPath(null, null), '')
})

test('full 128-stage graph remains finite and every stage and detail is reachable in bounds', () => {
  const document = { ...createSopWorkflow(), nodes: Array.from({ length: 128 }, (_, index) => ({ ...createWorkflowNode('custom', index * 344, 60), id: `stage_${index}` })), edges: [] }
  const view = projectWorkflowMindMap(document, { expandedIds: new Set(document.nodes.map(node => node.id)) })
  assert.equal(view.stages.size, 128)
  assert.equal(view.branches.length, 128)
  assert.ok(Object.values(view.bounds).every(Number.isFinite))
  for (const stage of view.stages.values()) { assert.ok(inside(view.bounds, stage)); for (const detail of stage.details) assert.ok(inside(view.bounds, detail)) }
  const arranged = arrangeWorkflowMindMap(document)
  assert.equal(validateWorkflow(arranged).valid, true)
  assert.ok(arranged.nodes.every(node => Math.abs(node.x) <= 100000 && Math.abs(node.y) <= 100000))
})

test('dependency arrows avoid the visual root, intervening phases and expanded leaves', () => {
  const document = createSopWorkflow()
  document.edges.push({ id: 'skip', source: 'requirements', target: 'architecture' })
  const view = projectWorkflowMindMap(document, { expandedIds: new Set(['requirements', 'ui_design', 'implementation']) })
  const boxes = [view.root, ...[...view.stages.values()].flatMap(stage => [stage, ...stage.details])]
  for (const [index, edge] of document.edges.entries()) {
    const source = view.stages.get(edge.source), target = view.stages.get(edge.target)
    const path = workflowDependencyPath(source, target, index, view)
    const commands = [...path.matchAll(/([MHV]) ([\d.-]+)(?: ([\d.-]+))?/g)]
    let previous = { x: Number(commands[0][2]), y: Number(commands[0][3]) }
    assert.deepEqual(previous, source.output)
    for (const [command, axis, first, second] of commands.slice(1)) {
      const point = axis === 'H' ? { x: Number(first), y: previous.y } : { x: previous.x, y: Number(first) }
      for (const box of boxes) {
        const intersects = point.x === previous.x
          ? point.x > box.x && point.x < box.x + box.width && Math.max(point.y, previous.y) > box.y && Math.min(point.y, previous.y) < box.y + box.height
          : point.y > box.y && point.y < box.y + box.height && Math.max(point.x, previous.x) > box.x && Math.min(point.x, previous.x) < box.x + box.width
        assert.equal(intersects, false, `${edge.id} ${command} enters ${box.id}`)
      }
      previous = point
    }
    assert.deepEqual(previous, target.input)
  }
})

test('a dependency leaving a manually placed stage fits a ten-pixel corridor', () => {
  const document = { ...createSopWorkflow(), nodes: [
    { ...createWorkflowNode('custom', 0, 0), id: 'a' },
    { ...createWorkflowNode('custom', 250, 0), id: 'obstacle' },
    { ...createWorkflowNode('custom', 1000, 300), id: 'b' },
  ], edges: [{ id: 'a_b', source: 'a', target: 'b' }] }
  const view = projectWorkflowMindMap(document)
  const path = workflowDependencyPath(view.stages.get('a'), view.stages.get('b'), 0, view)
  const startLane = Number(path.match(/ H ([\d.-]+)/)[1])
  assert.ok(startLane > 240 && startLane < 250, 'the outlet bends before reaching the nearby obstacle')
  assert.ok(!/NaN|Infinity|undefined/.test(path))
  assert.ok(path.endsWith('V 342 H 1000'))
})
