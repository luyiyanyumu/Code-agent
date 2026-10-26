import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createWorkflowHistory, resetWorkflowHistory, recordWorkflowHistory,
  undoWorkflowHistory, redoWorkflowHistory, canUndoWorkflow, canRedoWorkflow,
} from '../src/lib/workflowHistory.js'

function workflow(name = '项目 SOP') {
  return {
    schema_version: 1, name,
    nodes: [{ id: 'a', title: '需求确认', x: 40, y: 60, status: 'pending', output: '' }],
    edges: [], viewport: { x: 12, y: 34, zoom: 0.9 }, revision: 7, updated_at: '2026-10-10T01:00:00Z',
  }
}

function edit(document, update) {
  const next = structuredClone(document)
  update(next)
  return next
}

test('graph operations are complete independent transactions', () => {
  let document = workflow()
  const history = createWorkflowHistory(document)
  const documents = [structuredClone(document)]
  for (const update of [
    (draft) => { draft.nodes[0].x = 450; draft.nodes[0].y = 160 },
    (draft) => { draft.nodes.push({ id: 'b', title: '设计', x: 750, y: 160, status: 'pending', output: '' }) },
    (draft) => { draft.edges.push({ id: 'a_b', source: 'a', target: 'b' }) },
    (draft) => { draft.nodes.splice(1, 1); draft.edges = [] },
  ]) {
    document = edit(document, update)
    assert.equal(recordWorkflowHistory(history, document), true)
    documents.push(structuredClone(document))
  }
  for (let index = documents.length - 2; index >= 0; index -= 1) {
    document = undoWorkflowHistory(history, document)
    assert.deepEqual(document, documents[index])
  }
  assert.equal(canUndoWorkflow(history), false)
  assert.equal(undoWorkflowHistory(history, document), null)
  for (let index = 1; index < documents.length; index += 1) {
    document = redoWorkflowHistory(history, document)
    assert.deepEqual(document, documents[index])
  }
  assert.equal(canRedoWorkflow(history), false)
  assert.equal(redoWorkflowHistory(history, document), null)
})

test('undo and redo retain current viewport and latest server revision', () => {
  const original = workflow()
  const history = createWorkflowHistory(original)
  const changed = edit(original, (draft) => { draft.nodes[0].title = '核心需求' })
  recordWorkflowHistory(history, changed)
  const saved = edit(changed, (draft) => {
    draft.revision = 9; draft.updated_at = '2026-10-10T01:10:00Z'
    draft.viewport = { x: -300, y: 500, zoom: 1.2 }
  })
  assert.equal(recordWorkflowHistory(history, saved), false)
  const undone = undoWorkflowHistory(history, saved)
  assert.equal(undone.nodes[0].title, original.nodes[0].title)
  assert.deepEqual(undone.viewport, saved.viewport)
  assert.equal(undone.revision, 9)
  assert.equal(undone.updated_at, saved.updated_at)
  const latest = edit(undone, (draft) => { draft.revision = 10; draft.viewport.zoom = 0.6 })
  const redone = redoWorkflowHistory(history, latest)
  assert.equal(redone.nodes[0].title, '核心需求')
  assert.equal(redone.revision, 10)
  assert.equal(redone.viewport.zoom, 0.6)
})

test('view navigation and save timestamps never create history entries', () => {
  const original = workflow()
  const history = createWorkflowHistory(original)
  const viewed = edit(original, (draft) => { draft.viewport.x = 1200; draft.revision += 1; draft.updated_at = 'later' })
  assert.equal(recordWorkflowHistory(history, viewed), false)
  assert.equal(canUndoWorkflow(history), false)
})

test('snapshots and returned documents have no shared live references', () => {
  const original = workflow()
  const history = createWorkflowHistory(original)
  original.nodes[0].title = '改变原对象'
  const changed = edit(original, (draft) => { draft.nodes[0].title = '编辑后的标题' })
  recordWorkflowHistory(history, changed)
  changed.nodes[0].title = '调用者继续修改'
  const undone = undoWorkflowHistory(history, edit(changed, (draft) => { draft.nodes[0].title = '编辑后的标题' }))
  assert.equal(undone.nodes[0].title, '需求确认')
  undone.viewport.x = 999
  assert.equal(changed.viewport.x, 12)
  const redone = redoWorkflowHistory(history, edit(undone, (draft) => { draft.nodes[0].title = '需求确认' }))
  assert.equal(redone.nodes[0].title, '编辑后的标题')
  assert.notEqual(undone.nodes, redone.nodes)
  assert.notEqual(undone.viewport, redone.viewport)
})

test('same field text edits within 600 ms merge into one undo step', () => {
  let document = workflow()
  const history = createWorkflowHistory(document)
  for (const [title, time] of [['需', 0], ['需求', 500], ['需求文档', 1100]]) {
    document = edit(document, (draft) => { draft.nodes[0].title = title })
    recordWorkflowHistory(history, document, { group: 'a:title', time })
  }
  assert.equal(history.past.length, 1)
  const undone = undoWorkflowHistory(history, document)
  assert.equal(undone.nodes[0].title, '需求确认')
  assert.equal(redoWorkflowHistory(history, undone).nodes[0].title, '需求文档')
})

test('elapsed time or another field starts a separate text transaction', () => {
  let document = workflow()
  const history = createWorkflowHistory(document)
  for (const [field, value, time] of [['title', 'A', 0], ['title', 'B', 601], ['output', '产物', 700]]) {
    document = edit(document, (draft) => { draft.nodes[0][field] = value })
    recordWorkflowHistory(history, document, { group: `a:${field}`, time })
  }
  document = undoWorkflowHistory(history, document)
  assert.equal(document.nodes[0].title, 'B')
  assert.equal(document.nodes[0].output, '')
  document = undoWorkflowHistory(history, document)
  assert.equal(document.nodes[0].title, 'A')
})

test('explicit null even on an unchanged document interrupts text merging', () => {
  let document = workflow()
  const history = createWorkflowHistory(document)
  document = edit(document, (draft) => { draft.nodes[0].title = 'A' })
  recordWorkflowHistory(history, document, { group: 'a:title', time: 0 })
  assert.equal(recordWorkflowHistory(history, document, { group: null, time: 100 }), false)
  document = edit(document, (draft) => { draft.nodes[0].title = 'B' })
  recordWorkflowHistory(history, document, { group: 'a:title', time: 200 })
  assert.equal(undoWorkflowHistory(history, document).nodes[0].title, 'A')
})

test('clock moving backwards cannot merge unrelated transactions', () => {
  let document = workflow()
  const history = createWorkflowHistory(document)
  document = edit(document, (draft) => { draft.name = 'A' })
  recordWorkflowHistory(history, document, { group: 'name', time: 900 })
  document = edit(document, (draft) => { draft.name = 'B' })
  recordWorkflowHistory(history, document, { group: 'name', time: 500 })
  assert.equal(undoWorkflowHistory(history, document).name, 'A')
})

test('editing after undo creates a branch and clears redo', () => {
  let document = workflow()
  const history = createWorkflowHistory(document)
  for (const name of ['A', 'B']) {
    document = edit(document, (draft) => { draft.name = name })
    recordWorkflowHistory(history, document)
  }
  document = undoWorkflowHistory(history, document)
  assert.equal(document.name, 'A')
  assert.equal(canRedoWorkflow(history), true)
  document = edit(document, (draft) => { draft.name = 'C' })
  recordWorkflowHistory(history, document)
  assert.equal(canRedoWorkflow(history), false)
  assert.equal(undoWorkflowHistory(history, document).name, 'A')
})

test('undo ends a text group before subsequent edits', () => {
  let document = workflow()
  const history = createWorkflowHistory(document)
  for (const [name, time] of [['A', 0], ['B', 900]]) {
    document = edit(document, (draft) => { draft.name = name })
    recordWorkflowHistory(history, document, { group: 'name', time })
  }
  document = undoWorkflowHistory(history, document)
  document = edit(document, (draft) => { draft.name = 'C' })
  recordWorkflowHistory(history, document, { group: 'name', time: 950 })
  assert.equal(undoWorkflowHistory(history, document).name, 'A')
})

test('per-project histories restore only their own graph content', () => {
  const a = workflow('项目 A'), b = workflow('项目 B')
  const histories = new Map([['a', createWorkflowHistory(a)], ['b', createWorkflowHistory(b)]])
  const a2 = edit(a, (draft) => { draft.nodes[0].output = 'A 产物' })
  const b2 = edit(b, (draft) => { draft.nodes[0].output = 'B 产物' })
  recordWorkflowHistory(histories.get('a'), a2)
  recordWorkflowHistory(histories.get('b'), b2)
  assert.equal(undoWorkflowHistory(histories.get('a'), a2).name, '项目 A')
  assert.equal(canUndoWorkflow(histories.get('b')), true)
  assert.equal(undoWorkflowHistory(histories.get('b'), b2).name, '项目 B')
  assert.equal(redoWorkflowHistory(histories.get('a'), a).nodes[0].output, 'A 产物')
})

test('reset discards old graphs and redo without modifying the loaded document', () => {
  const original = workflow(), history = createWorkflowHistory(original)
  const changed = edit(original, (draft) => { draft.name = '编辑内容' })
  recordWorkflowHistory(history, changed)
  undoWorkflowHistory(history, changed)
  const loaded = workflow('重新加载')
  assert.equal(resetWorkflowHistory(history, loaded), true)
  assert.equal(canUndoWorkflow(history), false)
  assert.equal(canRedoWorkflow(history), false)
  assert.equal(history.bytes, 0)
  assert.equal(resetWorkflowHistory(history, loaded), false)
  loaded.name = '外部继续编辑'
  assert.equal(recordWorkflowHistory(history, loaded), true)
  assert.equal(undoWorkflowHistory(history, loaded).name, '重新加载')
})

test('temporary empty title and completed-output drafts are preserved without strict validation', () => {
  const original = workflow()
  original.nodes[0].status = 'done'; original.nodes[0].output = '验收产物'
  const history = createWorkflowHistory(original)
  const incomplete = edit(original, (draft) => { draft.nodes[0].title = ''; draft.nodes[0].output = '' })
  assert.equal(recordWorkflowHistory(history, incomplete), true)
  const undone = undoWorkflowHistory(history, incomplete)
  assert.equal(undone.nodes[0].output, '验收产物')
  assert.deepEqual(redoWorkflowHistory(history, undone).nodes[0], incomplete.nodes[0])
})

test('JSON content is deduplicated regardless of object key insertion order', () => {
  const original = workflow(), history = createWorkflowHistory(original)
  const reordered = { ...original, nodes: [{ output: '', status: 'pending', y: 60, x: 40, title: '需求确认', id: 'a' }] }
  assert.equal(recordWorkflowHistory(history, reordered), false)
  assert.equal(canUndoWorkflow(history), false)
})

test('unrecorded edits are preserved as redo targets when undo is requested', () => {
  const original = workflow(), history = createWorkflowHistory(original)
  const incomplete = edit(original, (draft) => { draft.nodes[0].title = '' })
  const undone = undoWorkflowHistory(history, incomplete)
  assert.equal(undone.nodes[0].title, '需求确认')
  assert.equal(redoWorkflowHistory(history, undone).nodes[0].title, '')
})

test('an unrecorded new edit invalidates existing redo', () => {
  const original = workflow(), history = createWorkflowHistory(original)
  const changed = edit(original, (draft) => { draft.name = 'A' })
  recordWorkflowHistory(history, changed)
  const undone = undoWorkflowHistory(history, changed)
  const branched = edit(undone, (draft) => { draft.name = '新分支' })
  assert.equal(redoWorkflowHistory(history, branched), null)
  assert.equal(canRedoWorkflow(history), false)
  assert.equal(undoWorkflowHistory(history, branched).name, original.name)
})

test('entry limit bounds undo and redo together and retains the nearest steps', () => {
  let document = workflow('0')
  const history = createWorkflowHistory(document, { maxEntries: 3 })
  for (let index = 1; index <= 10; index += 1) {
    document = edit(document, (draft) => { draft.name = `${index}` })
    recordWorkflowHistory(history, document)
    assert.ok(history.past.length + history.future.length <= 3)
  }
  for (const name of ['9', '8', '7']) {
    document = undoWorkflowHistory(history, document)
    assert.equal(document.name, name)
    assert.ok(history.past.length + history.future.length <= 3)
  }
  assert.equal(undoWorkflowHistory(history, document), null)
  assert.equal(redoWorkflowHistory(history, document).name, '8')
})

test('UTF-8 byte limit trims large outputs without breaking the current baseline', () => {
  let document = workflow()
  const history = createWorkflowHistory(document, { maxEntries: 50, maxBytes: 1300 })
  for (let index = 0; index < 12; index += 1) {
    document = edit(document, (draft) => { draft.nodes[0].output = `${index}:` + '验收结果'.repeat(70) })
    recordWorkflowHistory(history, document)
    assert.ok(history.bytes <= 1300)
    assert.equal(history.bytes, [...history.past, ...history.future].reduce((sum, entry) => sum + new TextEncoder().encode(entry.json).byteLength, 0))
  }
  assert.equal(history.past.length, 1)
  const undone = undoWorkflowHistory(history, document)
  assert.ok(undone.nodes[0].output.startsWith('10:'))
  assert.ok(history.bytes <= 1300)
  assert.equal(redoWorkflowHistory(history, undone).nodes[0].output, document.nodes[0].output)
})

test('zero capacity disables retained undo while keeping change detection usable', () => {
  const original = workflow(), history = createWorkflowHistory(original, { maxEntries: 0, maxBytes: 0 })
  const changed = edit(original, (draft) => { draft.name = '编辑' })
  assert.equal(recordWorkflowHistory(history, changed), true)
  assert.equal(recordWorkflowHistory(history, changed), false)
  assert.equal(canUndoWorkflow(history), false)
  assert.equal(history.bytes, 0)
})

test('invalid memory and timing options fail clearly', () => {
  for (const options of [{ maxEntries: -1 }, { maxBytes: Infinity }, { mergeWindowMs: 0.5 }]) {
    assert.throws(() => createWorkflowHistory(workflow(), options), RangeError)
  }
  const history = createWorkflowHistory(workflow())
  assert.throws(() => recordWorkflowHistory(history, workflow(), { time: NaN }), /time/)
  assert.throws(() => createWorkflowHistory(null), /document/)
})
