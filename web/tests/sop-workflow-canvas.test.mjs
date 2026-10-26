import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as vue from 'vue'
import { api } from '../src/api.js'
import { createSopWorkflow } from '../src/lib/sopWorkflow.js'

const copy = value => JSON.parse(JSON.stringify(value))
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = async () => { await vue.nextTick(); for (let n = 0; n < 12; n++) await Promise.resolve() }
const document = (name = 'A 工作流', revision = 2) => ({ ...createSopWorkflow(), name, revision, updated_at: revision ? '2026-10-10T00:00:00Z' : '' })
function saved(snapshot, revision) { return { ...copy(snapshot), revision: revision + 1, updated_at: '2026-10-10T00:01:00Z' } }

async function setup(t, overrides = {}, storage = new Map(), uiBindings = {}) {
  const original = api.code, calls = [], events = [], timers = new Map(), dialogs = []
  let timerId = 0, mount, unmount, resize, stopped = false
  const props = vue.reactive({ projectId: 'a', tree: 'main' })
  const stubs = { workflow: async pid => document(`${pid} 工作流`), saveWorkflow: async (_, snapshot, revision) => saved(snapshot, revision), ...overrides }
  api.code = new Proxy(stubs, { get: (target, name) => (...args) => { calls.push({ name, args: copy(args) }); if (!target[name]) throw new Error(`Unstubbed API: ${String(name)}`); return target[name](...args) } })
  const bindings = {
    defineProps: () => props, defineEmits: () => (...args) => events.push(args),
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId }, clearTimeout: id => timers.delete(id),
    window: { addEventListener() {}, removeEventListener() {} },
    document: { addEventListener() {}, removeEventListener() {}, elementFromPoint: () => null },
    ResizeObserver: class { constructor(callback) { resize = callback } observe() {} disconnect() {} },
  }
  const url = new URL('../src/components/code/SopWorkflow.vue', import.meta.url)
  let script = readFileSync(url, 'utf8').match(/<script setup>([\s\S]*?)<\/script>/)[1]
  const imports = []
  script = script.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*$/gm, (_, clause, specifier) => { imports.push({ clause: clause.trim(), specifier }); return '' })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? vue : specifier.endsWith('.vue') ? { default: {} } : await import(new URL(specifier, url))
    if (clause.startsWith('{')) for (const entry of clause.slice(1, -1).split(',').map(s => s.trim()).filter(Boolean)) { const [key, local = key] = entry.split(/\s+as\s+/); bindings[local] = namespace[key] }
    else bindings[clause] = namespace.default
  }
  Object.assign(bindings, uiBindings)
  bindings.onMounted = fn => { mount = fn }
  bindings.onBeforeUnmount = fn => { unmount = fn }
  bindings.useDialog = () => ({ open: async options => { dialogs.push(options); return null } })
  const scope = vue.effectScope()
  const state = scope.run(() => new Function(...Object.keys(bindings), `${script}\nreturn {active,workflow,nodes,viewport,ready,contexts,selectedId,selectedEdgeId,notice,statusText,selectNode,clickNode,focusNode,updateNode,recordChange,saveState,loadContext,retry,reloadRemote,saveDraftOverLatest,requestStatus,handoff,addNode,deleteSelection,arrange,gesture,cancelGesture,zoomAt,fitCanvas,canvasEl,canvasSize,rootEl,onKeydown,onKeyup,onWindowBlur,onWheel,navigateMinimap,minimap,edgeRoutes,inspectorOpen,restoreHistory,undoAvailable,redoAvailable,spaceHeld,beginPan,beginForcedPan,beginDrag,movePointer,endPointer,onCanvasDoubleClick,openAddMenu,visibleNodeTypes,addPosition,addSearch,addOpen,searchNodeKey,compactView,adjacentEdges,mindMap,visualStage,expandedIds,showDependencies,toggleDetails,toggleDependencies,openDetail,openNodeAgent}`)(...Object.values(bindings)))
  function dispose() { if (stopped) return; unmount?.(); scope.stop(); stopped = true }
  t.after(() => { dispose(); api.code = original })
  return { state, props, calls, events, stubs, timers, dialogs, storage, mount: () => mount?.(), resize: () => resize?.(), unmount: dispose, runTimer: async delay => { const entry = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(entry, `timer ${delay} is scheduled`); timers.delete(entry[0]); await entry[1].fn(); await settle() } }
}

test('reading a revision-zero default does not write or claim it was already saved', async t => {
  const fixture = await setup(t, { workflow: async () => document('默认 SOP', 0) })
  await settle()
  assert.equal(fixture.state.statusText.value, '默认模板')
  assert.equal(fixture.state.active.value.loaded, true)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
  assert.equal([...fixture.timers.values()].some(timer => timer.delay === 850), false)
})

test('a slow earlier project load cannot replace the newly selected project', async t => {
  const first = deferred(), second = deferred()
  const fixture = await setup(t, { workflow: pid => pid === 'a' ? first.promise : second.promise })
  fixture.props.projectId = 'b'
  await settle()
  second.resolve(document('B 服务端', 5)); await settle()
  assert.equal(fixture.state.workflow.value.name, 'B 服务端')
  first.resolve(document('A 服务端', 8)); await settle()
  assert.equal(fixture.state.workflow.value.name, 'B 服务端')
  assert.equal(fixture.state.active.value.baseRevision, 5)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
})

test('a stalled workflow read times out, releases loading and ignores its late result after retry', async t => {
  const stalled = deferred(), retried = deferred(), signals = []
  const fixture = await setup(t, { workflow: (_, __, options) => {
    signals.push(options?.signal)
    return signals.length === 1 ? stalled.promise : retried.promise
  } })
  await settle()
  assert.equal(fixture.state.active.value.loading, true)
  assert.ok(signals[0] instanceof AbortSignal, 'the actual API read can be cancelled')
  await fixture.runTimer(8000)
  assert.equal(signals[0].aborted, true)
  assert.equal(fixture.state.active.value.loading, false)
  assert.equal(fixture.state.active.value.loaded, false)
  assert.ok(fixture.state.active.value.loadError, 'a stalled read exposes a retryable error')
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)

  fixture.state.retry()
  await settle()
  assert.equal(signals.length, 2)
  assert.equal(fixture.state.active.value.loading, true)
  retried.resolve(document('重试取得的新工作流', 11))
  await settle()
  assert.equal(fixture.state.workflow.value.name, '重试取得的新工作流')
  assert.equal(fixture.state.active.value.baseRevision, 11)
  assert.equal(fixture.state.active.value.loadError, '')
  stalled.resolve(document('超时请求的旧工作流', 3))
  await settle()
  assert.equal(fixture.state.workflow.value.name, '重试取得的新工作流')
  assert.equal(fixture.state.active.value.baseRevision, 11)
  assert.equal(fixture.state.active.value.loading, false)
})

for (const kind of ['project', 'tree']) test(`switching ${kind} aborts its old read and returning starts a fresh request`, async t => {
  const first = deferred(), signals = []
  const fixture = await setup(t, { workflow: (pid, tree, options) => {
    signals.push({ pid, tree, signal: options?.signal })
    return signals.length === 1 ? first.promise : Promise.resolve(document(`${pid}:${tree} 新版`, 6))
  } })
  const old = fixture.state.active.value
  const change = value => { if (kind === 'project') fixture.props.projectId = value; else fixture.props.tree = value }
  change(kind === 'project' ? 'b' : 'task:42')
  await settle()
  assert.equal(signals[0].signal?.aborted, true)
  assert.equal(old.loading, false, 'a cancelled context does not remain stuck when revisited')
  assert.equal(old.loaded, false)
  const currentName = fixture.state.workflow.value.name
  first.resolve(document('已切走请求的过期工作流', 99))
  await settle()
  assert.equal(fixture.state.workflow.value.name, currentName)
  assert.equal(old.loaded, false, 'the late result cannot populate a cancelled context')

  change(kind === 'project' ? 'a' : 'main')
  await settle()
  assert.equal(signals.length, 3)
  assert.equal(fixture.state.active.value, old)
  assert.equal(fixture.state.workflow.value.name, 'a:main 新版')
  assert.equal(fixture.state.active.value.baseRevision, 6)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
})

test('unmount cancels a pending workflow read and cannot apply its late response or save', async t => {
  const pending = deferred()
  let signal
  const fixture = await setup(t, { workflow: (_, __, options) => { signal = options?.signal; return pending.promise } })
  const original = fixture.state.active.value
  fixture.unmount()
  assert.equal(signal?.aborted, true)
  assert.equal(original.loading, false)
  pending.resolve(document('卸载后返回的工作流', 44))
  await settle()
  assert.equal(original.loaded, false)
  assert.equal(original.document.name, '项目开发 SOP')
  assert.equal(original.baseRevision, 0)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
  assert.equal([...fixture.timers.values()].some(timer => timer.delay === 8000), false)
})

test('viewing a pending workflow supports pan, wheel, minimap and zoom without dirtying unknown server data', async t => {
  const pending = deferred(), fixture = await setup(t, { workflow: () => pending.promise })
  canvas(fixture)
  const original = copy(fixture.state.viewport.value)
  const assertReadOnlyPreview = () => {
    assert.equal(fixture.state.active.value.editSerial, 0)
    assert.equal(fixture.state.active.value.savedSerial, 0)
    assert.equal(fixture.state.active.value.conflict, false)
    assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
    assert.equal([...fixture.timers.values()].some(timer => timer.delay === 850), false)
  }
  fixture.state.zoomAt(.75)
  assert.equal(fixture.state.viewport.value.zoom, .75)
  assertReadOnlyPreview()
  const zoomedX = fixture.state.viewport.value.x
  fixture.state.onWheel({ shiftKey: true, deltaX: 0, deltaY: 55, target: { closest: () => null } })
  assert.equal(fixture.state.viewport.value.x, zoomedX - 55)
  assertReadOnlyPreview()

  const beforePan = copy(fixture.state.viewport.value)
  fixture.state.beginPan(pointer(110, 150))
  assert.equal(fixture.state.gesture.value?.kind, 'pan')
  fixture.state.movePointer(pointer(180, 190))
  fixture.state.endPointer(pointer(180, 190))
  assert.equal(fixture.state.viewport.value.x, beforePan.x + 70)
  assert.equal(fixture.state.viewport.value.y, beforePan.y + 40)
  assertReadOnlyPreview()

  fixture.state.navigateMinimap({ detail: 0 })
  assert.notEqual(fixture.state.viewport.value.zoom, .75, 'keyboard minimap navigation can fit during a read')
  assertReadOnlyPreview()
  fixture.state.fitCanvas(true)
  fixture.state.zoomAt(1.15)
  const preview = copy(fixture.state.viewport.value)
  assert.notDeepEqual(preview, original)
  assert.deepEqual(copy(fixture.state.active.value.previewViewport), preview)
  await fixture.state.saveState()
  assertReadOnlyPreview()

  const remote = document('服务端已有工作流', 7)
  remote.viewport = { x: -300, y: -240, zoom: .2 }
  pending.resolve(remote)
  await settle()
  assert.equal(fixture.state.active.value.loaded, true)
  assert.equal(fixture.state.active.value.baseRevision, 7)
  assert.equal(fixture.state.workflow.value.name, '服务端已有工作流')
  assert.deepEqual(copy(fixture.state.viewport.value), preview, 'the fresh GET retains the view chosen while waiting')
  assertReadOnlyPreview()
})

test('a workflow response received during preview panning preserves the live view and ends the old gesture', async t => {
  const pending = deferred(), fixture = await setup(t, { workflow: () => pending.promise })
  canvas(fixture)
  fixture.state.beginPan(pointer(110, 150))
  fixture.state.movePointer(pointer(280, 240))
  const liveView = copy(fixture.state.viewport.value)
  assert.equal(fixture.state.gesture.value?.kind, 'pan')
  assert.equal(fixture.state.active.value.previewViewport, null, 'the gesture has not committed a preview yet')
  const remote = document('平移期间取得的工作流', 8)
  remote.viewport = { x: -500, y: -300, zoom: .3 }
  pending.resolve(remote)
  await settle()
  assert.equal(fixture.state.workflow.value.name, remote.name)
  assert.deepEqual(copy(fixture.state.viewport.value), liveView)
  assert.equal(fixture.state.gesture.value, null)
  fixture.state.movePointer(pointer(390, 310))
  fixture.state.endPointer(pointer(390, 310))
  assert.deepEqual(copy(fixture.state.viewport.value), liveView, 'late pointer events cannot move the newly loaded document')
  assert.equal(fixture.state.active.value.editSerial, 0)
  assert.equal(fixture.state.active.value.conflict, false)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
})

test('local changes made before a delayed load are preserved and saved against its fresh revision', async t => {
  const pending = deferred(), fixture = await setup(t, { workflow: () => pending.promise })
  fixture.state.workflow.value.nodes[0].objective = '读取期间的新草稿'
  fixture.state.recordChange()
  pending.resolve(document('服务端默认', 0)); await settle()
  assert.equal(fixture.state.workflow.value.nodes[0].objective, '读取期间的新草稿')
  await fixture.runTimer(850)
  const put = fixture.calls.find(call => call.name === 'saveWorkflow')
  assert.equal(put.args[2], 0)
  assert.equal(put.args[1].nodes[0].objective, '读取期间的新草稿')
})

test('an edit during a save is retained and the next save uses the returned revision', async t => {
  const pending = deferred(), fixture = await setup(t, { saveWorkflow: () => pending.promise })
  await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id)
  fixture.state.updateNode('objective', '第一版')
  const saving = fixture.state.saveState()
  fixture.state.updateNode('objective', '保存期间的第二版')
  await fixture.state.saveState()
  const snapshot = fixture.calls.find(call => call.name === 'saveWorkflow').args[1]
  assert.equal(snapshot.nodes[0].objective, '第一版')
  pending.resolve(saved(snapshot, 2)); await saving; await settle()
  assert.equal(fixture.state.nodes.value[0].objective, '保存期间的第二版')
  assert.equal(fixture.state.active.value.baseRevision, 3)
  assert.equal(fixture.state.statusText.value, '待保存')
  fixture.stubs.saveWorkflow = async (_, doc, revision) => saved(doc, revision)
  await fixture.runTimer(850)
  const saves = fixture.calls.filter(call => call.name === 'saveWorkflow')
  assert.equal(saves.length, 2)
  assert.equal(saves[1].args[2], 3)
  assert.equal(saves[1].args[1].nodes[0].objective, '保存期间的第二版')
  assert.equal(fixture.state.statusText.value, '已保存')
})

test('an earlier project save response does not replace the active project or its local edits', async t => {
  const pending = deferred(), fixture = await setup(t, { saveWorkflow: () => pending.promise })
  await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id)
  fixture.state.updateNode('objective', 'A 草稿')
  const saving = fixture.state.saveState()
  fixture.props.projectId = 'b'; await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id)
  fixture.state.updateNode('objective', 'B 草稿')
  const snapshot = fixture.calls.find(call => call.name === 'saveWorkflow').args[1]
  pending.resolve(saved(snapshot, 2)); await saving; await settle()
  assert.equal(fixture.state.workflow.value.name, 'b 工作流')
  assert.equal(fixture.state.nodes.value[0].objective, 'B 草稿')
  assert.equal(fixture.state.active.value.baseRevision, 2)
})

test('409 keeps the complete draft and pauses automatic overwrites', async t => {
  const fixture = await setup(t, { saveWorkflow: async () => { throw Object.assign(new Error('冲突'), { status: 409 }) } })
  await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id)
  fixture.state.updateNode('output', '还未提交的验收结果')
  await fixture.state.saveState()
  assert.equal(fixture.state.active.value.conflict, true)
  assert.equal(fixture.state.nodes.value[0].output, '还未提交的验收结果')
  assert.equal(fixture.state.statusText.value, '草稿已保留')
  assert.equal(fixture.storage.size, 1)
  assert.equal([...fixture.timers.values()].some(timer => timer.delay === 850), false)
  await fixture.state.saveState()
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 1)
})

test('a persisted draft is restored after remount and a newer server revision requires an explicit decision', async t => {
  const storage = new Map(), first = await setup(t, { saveWorkflow: async () => { throw Object.assign(new Error('冲突'), { status: 409 }) } }, storage)
  await settle(); first.state.selectNode(first.state.nodes.value[0].id); first.state.updateNode('objective', '恢复出来的草稿'); await first.state.saveState(); first.unmount()
  const second = await setup(t, { workflow: async () => document('服务端新版', 8) }, storage)
  await settle()
  assert.equal(second.state.nodes.value[0].objective, '恢复出来的草稿')
  assert.equal(second.state.active.value.conflict, true)
  assert.equal(second.calls.filter(call => call.name === 'saveWorkflow').length, 0)
  await second.state.reloadRemote()
  assert.equal(second.dialogs.length, 1)
  assert.equal(second.state.nodes.value[0].objective, '恢复出来的草稿', 'opening the decision does not discard the draft')
  await second.dialogs[0].onConfirm()
  assert.equal(second.state.workflow.value.name, '服务端新版')
  assert.equal(second.state.active.value.baseRevision, 8)
  assert.equal(second.storage.size, 0)
})

test('temporarily blank edited fields retain a local draft without crashing readiness', async t => {
  const storage = new Map(), first = await setup(t, {}, storage)
  await settle(); first.state.selectNode(first.state.nodes.value[0].id); first.state.updateNode('title', '')
  assert.deepEqual([...first.state.ready.value], [])
  await first.state.saveState()
  assert.ok(first.state.active.value.saveError)
  assert.equal(first.calls.filter(call => call.name === 'saveWorkflow').length, 0)
  first.unmount()
  const second = await setup(t, {}, storage); await settle()
  assert.equal(second.state.nodes.value[0].title, '')
  assert.ok(second.state.active.value.editSerial > second.state.active.value.savedSerial)
})

test('a failed initial load never attempts to overwrite the unknown server document', async t => {
  const fixture = await setup(t, { workflow: async () => { throw new Error('读取失败') } })
  await settle()
  fixture.state.workflow.value.nodes[0].objective = '离线草稿'; fixture.state.recordChange()
  await fixture.state.saveState()
  assert.equal(fixture.state.active.value.loaded, false)
  assert.equal(fixture.state.active.value.loadError, '读取失败')
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
  fixture.stubs.workflow = async () => document('新版', 7)
  await fixture.state.loadContext(fixture.state.active.value)
  assert.equal(fixture.state.active.value.conflict, true)
  assert.equal(fixture.state.nodes.value[0].objective, '离线草稿')
})

test('a stale conflict dialog cannot discard the draft of a different project', async t => {
  const fixture = await setup(t)
  await settle(); fixture.state.selectNode(fixture.state.nodes.value[0].id); fixture.state.updateNode('objective', 'A 草稿'); await fixture.state.reloadRemote()
  fixture.props.projectId = 'b'; await settle()
  await assert.rejects(fixture.dialogs[0].onConfirm(), /变化/)
  assert.equal(fixture.state.workflow.value.name, 'b 工作流')
})

test('switching trees during a node drag cancels temporary coordinates before saving', async t => {
  const fixture = await setup(t)
  await settle()
  const node = fixture.state.nodes.value[0], original = { x: node.x, y: node.y }
  fixture.state.gesture.value = { kind: 'node', nodeId: node.id, startX: node.x, startY: node.y }
  node.x = 900; node.y = 700
  fixture.props.tree = 'task_1'; await settle()
  assert.equal(fixture.state.contexts.get('a:main').document.nodes[0].x, original.x)
  assert.equal(fixture.state.contexts.get('a:main').document.nodes[0].y, original.y)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
})

test('AI handoff supplies the phase prompt without marking the phase finished or issuing a model API call', async t => {
  const fixture = await setup(t)
  await settle(); fixture.state.selectNode(fixture.state.nodes.value[0].id); fixture.state.handoff()
  assert.equal(fixture.events.length, 1)
  assert.equal(fixture.events[0][0], 'ask')
  assert.match(fixture.events[0][1], /需求确认/)
  assert.match(fixture.events[0][1], /验收/)
  assert.deepEqual(fixture.events[0][2], { node_id: fixture.state.nodes.value[0].id, tree_id: 'main' })
  assert.equal(fixture.state.nodes.value[0].status, 'pending')
  assert.deepEqual(fixture.calls.map(call => call.name), ['workflow'])
  fixture.state.selectNode(fixture.state.nodes.value[1].id); fixture.state.handoff()
  assert.equal(fixture.events.length, 1, 'blocked downstream phases do not generate handoff')
})

test('workflow persistence refreshes node assignments only after successful current-context save', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id)
  fixture.state.updateNode('objective', '更新节点任务')
  assert.equal(fixture.events.some(([type]) => type === 'team-refresh'), false)
  await fixture.state.saveState()
  assert.deepEqual(fixture.events.find(([type]) => type === 'team-refresh'), ['team-refresh', { project_id: 'a', tree_id: 'main' }])
})

test('completion needs an output and predecessors remain gated until successful acceptance', async t => {
  const fixture = await setup(t)
  await settle()
  const first = fixture.state.nodes.value[0].id, second = fixture.state.nodes.value[1].id
  fixture.state.requestStatus(second, 'active'); assert.equal(fixture.state.nodes.value[1].status, 'pending')
  fixture.state.requestStatus(first, 'active'); fixture.state.requestStatus(first, 'done')
  assert.equal(fixture.state.nodes.value[0].status, 'active')
  fixture.state.updateNode('output', '需求清单与验收条件已确认'); fixture.state.requestStatus(first, 'done')
  assert.equal(fixture.state.nodes.value[0].status, 'done')
  assert.ok(fixture.state.ready.value.has(second))
})

test('Delete and Backspace do not intercept edits inside form inputs', async t => {
  const fixture = await setup(t)
  await settle(); fixture.state.selectNode(fixture.state.nodes.value[0].id)
  let prevented = false
  fixture.state.onKeydown({ key: 'Delete', target: { closest: () => ({}) }, preventDefault: () => { prevented = true } })
  assert.equal(prevented, false)
  assert.equal(fixture.dialogs.length, 0)
  fixture.state.onKeydown({ key: 'Delete', target: { closest: () => null }, preventDefault: () => { prevented = true } })
  assert.equal(prevented, true)
  assert.equal(fixture.dialogs.length, 1)
})

test('changing accepted content reopens the phase and downstream phases before clearing its output', async t => {
  const accepted = document()
  accepted.nodes[0].status = 'done'; accepted.nodes[0].output = '第一阶段产出'
  accepted.nodes[1].status = 'done'; accepted.nodes[1].output = '第二阶段产出'
  accepted.nodes[2].status = 'active'
  const fixture = await setup(t, { workflow: async () => accepted }); await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id)
  fixture.state.updateNode('output', '')
  assert.equal(fixture.state.nodes.value[0].status, 'pending')
  assert.equal(fixture.state.nodes.value[1].status, 'pending')
  assert.equal(fixture.state.nodes.value[2].status, 'pending')
  assert.equal(fixture.state.nodes.value[0].output, '')
  assert.equal(fixture.state.nodes.value[1].output, '第二阶段产出')
  await fixture.state.saveState()
  assert.equal(fixture.state.active.value.saveError, '')
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 1)
})

test('readable initial fit keeps the first phase fully reachable at the left and top on a narrow canvas', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.canvasEl.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 390, height: 400 }) }
  fixture.state.fitCanvas(true, false)
  const first = fixture.state.visualStage(fixture.state.nodes.value[0]), view = fixture.state.viewport.value
  assert.equal(view.zoom, .9)
  assert.ok(first.x * view.zoom + view.x >= 32)
  assert.ok(first.y * view.zoom + view.y >= 64)
  assert.ok((first.x + 240) * view.zoom + view.x <= 390)
  assert.ok((first.y + first.height) * view.zoom + view.y <= 400)
})

test('Ctrl+S saves workflow edits inside textareas and stops the outer editor shortcut', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id); fixture.state.updateNode('objective', '快捷键保存')
  let prevented = false, stopped = false
  fixture.state.onKeydown({ key: 's', ctrlKey: true, target: { closest: () => ({}) }, preventDefault: () => { prevented = true }, stopPropagation: () => { stopped = true } })
  await settle()
  assert.equal(prevented, true)
  assert.equal(stopped, true)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 1)
})

test('a hidden canvas retains its initial-fit request until it has a visible size', async t => {
  const pending = deferred(), fixture = await setup(t, { workflow: () => pending.promise })
  fixture.state.canvasEl.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }) }
  pending.resolve(document('默认 SOP', 0)); await settle()
  assert.equal(fixture.state.active.value.initialViewport, true)
  fixture.state.canvasEl.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 390, height: 500 }) }
  assert.equal(fixture.state.fitCanvas(true, false), true)
})

test('a saved legacy horizontal layout loaded while hidden fits its mindmap when the canvas becomes visible', async t => {
  const pending = deferred(), fixture = await setup(t, { workflow: () => pending.promise })
  let width = 0
  fixture.state.canvasEl.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width, height: width ? 500 : 0 }) }
  fixture.mount()
  pending.resolve(document('保存过的旧横排工作流', 7)); await settle()
  assert.equal(fixture.state.active.value.projectionViewed, false)
  width = 390; fixture.resize(); await settle()
  assert.equal(fixture.state.active.value.projectionViewed, true)
  const view = fixture.state.viewport.value
  for (const rect of [fixture.state.mindMap.value.root, ...fixture.state.mindMap.value.stages.values()]) {
    assert.ok(rect.x * view.zoom + view.x >= 0)
    assert.ok((rect.x + rect.width) * view.zoom + view.x <= 390)
  }
  assert.equal(fixture.state.active.value.editSerial, 0)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
})

test('switching project while a loaded projection waits for nextTick cannot fit the next project', async t => {
  const pending = deferred(), tick = deferred()
  const remote = document('B 自由布局', 6)
  remote.nodes[0].y += 8; remote.viewport = { x: 17, y: 29, zoom: .7 }
  const fixture = await setup(t, { workflow: pid => pid === 'a' ? pending.promise : Promise.resolve(remote) }, new Map(), {
    nextTick: callback => callback ? vue.nextTick(callback) : tick.promise,
  })
  canvas(fixture)
  pending.resolve(document('A 待fit导图', 5)); await settle()
  assert.equal(fixture.state.active.value.loaded, true)
  fixture.props.projectId = 'b'; await settle()
  const original = copy(fixture.state.viewport.value)
  assert.deepEqual(original, remote.viewport)
  tick.resolve(); await settle()
  assert.deepEqual(copy(fixture.state.viewport.value), original)
  assert.equal(fixture.state.active.value.editSerial, 0)
})

test('an earlier autosave defers during dragging and never persists canceled transient coordinates', async t => {
  const pending = deferred(), fixture = await setup(t, { saveWorkflow: () => pending.promise }); await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id); fixture.state.updateNode('objective', '拖动之前已编辑的目标')
  const node = fixture.state.nodes.value[0], original = { x: node.x, y: node.y }
  fixture.state.gesture.value = { kind: 'node', nodeId: node.id, startX: node.x, startY: node.y }
  node.x += 400; node.y += 300
  await fixture.runTimer(850)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
  const draft = JSON.parse([...fixture.storage.values()][0])
  assert.equal(draft.document.nodes[0].x, original.x)
  assert.equal(draft.document.nodes[0].y, original.y)
  fixture.state.cancelGesture()
  await fixture.runTimer(850)
  const put = fixture.calls.find(call => call.name === 'saveWorkflow')
  assert.equal(put.args[1].nodes[0].x, original.x)
  assert.equal(put.args[1].nodes[0].y, original.y)
  pending.resolve(saved(put.args[1], 2)); await settle()
  assert.equal(fixture.state.nodes.value[0].x, original.x)
  assert.equal(fixture.state.nodes.value[0].y, original.y)
  assert.equal(fixture.state.nodes.value[0].objective, '拖动之前已编辑的目标')
  assert.equal(fixture.state.active.value.baseRevision, 3)
})

for (const kind of ['node', 'pan']) test(`a save started before ${kind} movement cannot replace the live gesture or break Escape rollback`, async t => {
  const pending = deferred(), fixture = await setup(t, { saveWorkflow: () => pending.promise }); await settle()
  fixture.state.selectNode(fixture.state.nodes.value[0].id); fixture.state.updateNode('objective', '先保存字段')
  const saving = fixture.state.saveState(), node = fixture.state.nodes.value[0], documentRef = fixture.state.workflow.value
  const original = kind === 'node' ? { x: node.x, y: node.y } : { ...fixture.state.viewport.value }
  fixture.state.gesture.value = { kind, nodeId: node.id, startX: original.x, startY: original.y }
  if (kind === 'node') { node.x += 300; node.y += 200 }
  else { fixture.state.viewport.value.x += 300; fixture.state.viewport.value.y += 200 }
  const snapshot = fixture.calls.find(call => call.name === 'saveWorkflow').args[1]
  pending.resolve(saved(snapshot, 2)); await saving; await settle()
  assert.equal(fixture.state.workflow.value, documentRef, 'in-flight completion must preserve the live document object')
  assert.equal(fixture.state.nodes.value[0], node, 'gesture node references remain live')
  assert.equal(fixture.state.active.value.baseRevision, 3)
  assert.equal(fixture.state.active.value.editSerial, fixture.state.active.value.savedSerial)
  const moving = kind === 'node' ? fixture.state.nodes.value[0] : fixture.state.viewport.value
  assert.equal(moving.x, original.x + 300)
  assert.equal(moving.y, original.y + 200)
  fixture.state.cancelGesture()
  const restored = kind === 'node' ? fixture.state.nodes.value[0] : fixture.state.viewport.value
  assert.equal(restored.x, original.x)
  assert.equal(restored.y, original.y)
  assert.equal(fixture.state.statusText.value, '已保存')
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 1)
})

test('accepted content remains editable while another title is temporarily blank', async t => {
  const accepted = document()
  accepted.nodes[0].status = 'done'; accepted.nodes[0].output = '需求验收记录'
  accepted.nodes[1].status = 'done'; accepted.nodes[1].output = '界面验收记录'
  accepted.nodes[2].status = 'active'
  const fixture = await setup(t, { workflow: async () => accepted }); await settle()
  fixture.state.selectNode(fixture.state.nodes.value[4].id); fixture.state.updateNode('title', '')
  fixture.state.selectNode(fixture.state.nodes.value[0].id)
  assert.doesNotThrow(() => fixture.state.updateNode('output', ''))
  assert.equal(fixture.state.nodes.value[0].output, '')
  assert.equal(fixture.state.nodes.value[0].status, 'pending')
  assert.equal(fixture.state.nodes.value[1].status, 'pending')
  assert.equal(fixture.state.nodes.value[2].status, 'pending')
  assert.equal(fixture.state.nodes.value[1].output, '界面验收记录')
  assert.equal(fixture.state.nodes.value[4].title, '')
  await fixture.state.saveState()
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0, 'server validation stays strict')
  fixture.state.selectNode(fixture.state.nodes.value[4].id); fixture.state.updateNode('title', '开发实现')
  await fixture.state.saveState()
  assert.equal(fixture.state.active.value.saveError, '')
})

test('restoring an accepted node with a blank output reopens it and its descendants while preserving editable fields', async t => {
  const accepted = document()
  accepted.nodes[0].status = 'done'; accepted.nodes[0].output = '服务端需求产出'
  accepted.nodes[1].status = 'done'; accepted.nodes[1].output = '服务端设计产出'
  const draft = copy(accepted); draft.nodes[0].output = ''; draft.nodes[4].title = ''
  const storage = new Map([['code-agent-sop-draft-v1:a:main', JSON.stringify({ document: draft, baseRevision: 2 })]])
  const fixture = await setup(t, { workflow: async () => accepted }, storage); await settle()
  assert.equal(fixture.state.nodes.value[0].status, 'pending')
  assert.equal(fixture.state.nodes.value[1].status, 'pending')
  assert.equal(fixture.state.nodes.value[0].output, '')
  assert.equal(fixture.state.nodes.value[1].output, '服务端设计产出')
  assert.equal(fixture.state.nodes.value[4].title, '')
  fixture.state.selectNode(fixture.state.nodes.value[0].id)
  assert.doesNotThrow(() => fixture.state.updateNode('output', '恢复后补充的需求产出'))
  assert.equal(fixture.state.nodes.value[0].output, '恢复后补充的需求产出')
  fixture.state.selectNode(fixture.state.nodes.value[4].id); fixture.state.updateNode('title', '开发实现')
  await fixture.state.saveState()
  assert.equal(fixture.state.active.value.saveError, '')
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 1)
})

test('an incomplete title can be reset and removed without trapping the editor in an invalid draft', async t => {
  const accepted = document(); accepted.nodes[0].status = 'done'; accepted.nodes[0].output = '原产出'
  const fixture = await setup(t, { workflow: async () => accepted }); await settle()
  const id = fixture.state.nodes.value[0].id
  fixture.state.selectNode(id); fixture.state.updateNode('title', '')
  assert.doesNotThrow(() => fixture.state.requestStatus(id, 'pending'))
  assert.equal(fixture.state.nodes.value[0].status, 'pending')
  await fixture.state.deleteSelection(); await fixture.dialogs[0].onConfirm()
  assert.equal(fixture.state.nodes.value.some(node => node.id === id), false)
  await fixture.state.saveState()
  assert.equal(fixture.state.active.value.saveError, '')
})

test('organizing an inserted phase follows its actual dependencies and keeps stored array order and content', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.canvasEl.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 390, height: 500 }) }
  fixture.state.addNode('custom')
  const inserted = fixture.state.selectedId.value
  fixture.state.updateNode('title', '技术选型')
  const workflow = fixture.state.workflow.value
  workflow.edges = workflow.edges.filter(edge => edge.id !== 'edge_1')
  workflow.edges.push({ id: 'pick', source: 'requirements', target: inserted }, { id: 'picked', source: inserted, target: 'ui_design' })
  const before = copy(workflow)
  fixture.state.arrange(); await settle()
  assert.equal(fixture.state.inspectorOpen.value, false)
  assert.equal(fixture.state.nodes.value[1].id, inserted)
  assert.deepEqual(fixture.state.workflow.value.nodes.map(node => node.id), before.nodes.map(node => node.id))
  assert.deepEqual(fixture.state.workflow.value.edges, before.edges)
  const stages = [...fixture.state.mindMap.value.stages.values()]
  assert.equal(stages.filter(stage => stage.side === 'left').length, 5)
  assert.equal(stages.filter(stage => stage.side === 'right').length, 4)
  assert.ok(stages.every((stage, index) => stage.order === index + 1))
  for (const side of ['left', 'right']) {
    const branch = stages.filter(stage => stage.side === side)
    assert.ok(branch.every((stage, index) => !index || stage.y >= branch[index - 1].y + branch[index - 1].height))
  }
  assert.ok(fixture.state.viewport.value.zoom < .9, 'arrangement shows the whole mindmap on a narrow canvas')
  await fixture.state.saveState()
  const persisted = fixture.calls.find(call => call.name === 'saveWorkflow').args[1]
  assert.equal(persisted.nodes.find(node => node.id === inserted).title, '技术选型')
  assert.deepEqual(persisted.edges, before.edges)
})

test('rendered dependency routes bypass the root and intervening phases', async t => {
  const fixture = await setup(t); await settle()
  const workflow = fixture.state.workflow.value
  workflow.edges.push({ id: 'shortcut', source: 'requirements', target: 'architecture' })
  const source = fixture.state.visualStage('requirements'), target = fixture.state.visualStage('architecture')
  const path = fixture.state.edgeRoutes.value.get('shortcut')
  assert.ok(path.startsWith(`M ${source.output.x} ${source.output.y}`))
  assert.ok(path.endsWith(`V ${target.input.y} H ${target.input.x}`))
  assert.ok(!/NaN|Infinity|undefined/.test(path))
  const lane = Number(path.match(/ V ([\d.-]+)/)[1])
  const intervening = fixture.state.visualStage('ui_design')
  assert.ok(lane <= intervening.y || lane >= intervening.y + intervening.height)
})

test('narrow overview fits the root and all phase branches and reading mode reaches the first phase', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.canvasEl.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 390, height: 500 }) }
  fixture.state.fitCanvas()
  let viewport = fixture.state.viewport.value
  assert.ok(viewport.zoom >= .05 && viewport.zoom < .9)
  for (const node of [fixture.state.mindMap.value.root, ...fixture.state.mindMap.value.stages.values()]) {
    assert.ok(node.x * viewport.zoom + viewport.x >= 0)
    assert.ok((node.x + node.width) * viewport.zoom + viewport.x <= 390)
    assert.ok(node.y * viewport.zoom + viewport.y >= 0)
    assert.ok((node.y + node.height) * viewport.zoom + viewport.y <= 500)
  }
  fixture.state.fitCanvas(true)
  viewport = fixture.state.viewport.value
  const first = fixture.state.visualStage(fixture.state.nodes.value[0])
  assert.equal(viewport.zoom, .9)
  assert.ok(first.x * viewport.zoom + viewport.x >= 32)
  assert.ok((first.x + 240) * viewport.zoom + viewport.x <= 390)
})

test('Shift wheel and trackpad horizontal movement pan without changing zoom and persist the viewport', async t => {
  const fixture = await setup(t); await settle()
  const initial = { ...fixture.state.viewport.value }
  const target = { closest: () => null }
  fixture.state.onWheel({ shiftKey: true, deltaX: 0, deltaY: 96, target })
  fixture.state.onWheel({ shiftKey: false, deltaX: 45, deltaY: 2, target })
  assert.equal(fixture.state.viewport.value.x, initial.x - 141)
  assert.equal(fixture.state.viewport.value.y, initial.y)
  assert.equal(fixture.state.viewport.value.zoom, initial.zoom)
  await fixture.state.saveState()
  assert.equal(fixture.calls.find(call => call.name === 'saveWorkflow').args[1].viewport.x, initial.x - 141)
})

test('minimap uses the SVG transform including border and letterboxing when centering a distant phase', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.canvasEl.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 870, height: 600 }) }
  fixture.state.fitCanvas(true, false)
  const map = fixture.state.minimap.value, last = fixture.state.nodes.value.at(-1)
  const matrix = { a: .962963, d: .962963, e: 17, f: 901, inverse() { return this } }
  const svg = {
    getScreenCTM: () => matrix,
    createSVGPoint: () => ({ x: 0, y: 0, matrixTransform(inverse) { return { x: (this.x - inverse.e) / inverse.a, y: (this.y - inverse.f) / inverse.d } } }),
  }
  const localX = map.x + (last.x + 120) * map.scale, localY = map.y + (last.y + 95) * map.scale
  fixture.state.navigateMinimap({ detail: 1, clientX: localX * matrix.a + matrix.e, clientY: localY * matrix.d + matrix.f, currentTarget: { querySelector: () => svg } })
  const view = fixture.state.viewport.value
  assert.ok(Math.abs((last.x + 120) * view.zoom + view.x - 435) < .001)
  assert.ok(Math.abs((last.y + 95) * view.zoom + view.y - 300) < .001)
  assert.equal(view.zoom, .9)
  await fixture.state.saveState()
  assert.equal(fixture.state.active.value.saveError, '')
})

function pointer(x, y, overrides = {}) {
  return { button: 0, pointerId: 17, clientX: x, clientY: y, target: { closest: () => null }, preventDefault() {}, stopPropagation() {}, ...overrides }
}
function canvas(fixture) {
  fixture.state.canvasEl.value = { focus() {}, getBoundingClientRect: () => ({ left: 20, top: 40, width: 870, height: 600 }) }
}

test('grouped field edits remain undoable after saving without rolling back the current viewport or revision', async t => {
  const fixture = await setup(t); await settle()
  const original = fixture.state.nodes.value[0].objective
  fixture.state.selectNode('requirements')
  fixture.state.updateNode('objective', '第一段')
  fixture.state.updateNode('objective', '第一段和第二段')
  await fixture.state.saveState()
  assert.equal(fixture.state.active.value.history.past.length, 1)
  fixture.state.viewport.value.x = 187
  fixture.state.recordChange(fixture.state.active.value, null, false)
  fixture.state.restoreHistory()
  assert.equal(fixture.state.nodes.value[0].objective, original)
  assert.equal(fixture.state.viewport.value.x, 187)
  assert.equal(fixture.state.workflow.value.revision, 3)
  assert.equal(fixture.state.undoAvailable.value, false)
  assert.equal(fixture.state.redoAvailable.value, true)
  await fixture.state.saveState()
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').at(-1).args[2], 3)
  fixture.state.restoreHistory(true)
  assert.equal(fixture.state.nodes.value[0].objective, '第一段和第二段')
  assert.equal(fixture.state.workflow.value.revision, 4)
  assert.equal(fixture.state.viewport.value.x, 187)
})

test('one committed drag records one history step and a canceled drag leaves no transient coordinates', async t => {
  const fixture = await setup(t); await settle(); canvas(fixture)
  const node = fixture.state.nodes.value[0], start = { x: node.x, y: node.y }
  const projected = fixture.state.visualStage(node), committed = { x: projected.x + 100, y: projected.y + 50 }
  const original = copy(fixture.state.workflow.value.nodes)
  fixture.state.beginDrag(pointer(100, 100), node)
  for (let n = 1; n <= 10; n++) fixture.state.movePointer(pointer(100 + n * 10, 100 + n * 5))
  fixture.state.endPointer(pointer(200, 150))
  assert.equal(fixture.state.active.value.history.past.length, 1)
  assert.deepEqual({ x: node.x, y: node.y }, committed)
  assert.equal(fixture.state.mindMap.value.projected, false)
  fixture.state.restoreHistory()
  assert.deepEqual({ x: fixture.state.nodes.value[0].x, y: fixture.state.nodes.value[0].y }, start)
  assert.deepEqual(copy(fixture.state.workflow.value.nodes), original, 'one undo restores every materialized phase')
  fixture.state.restoreHistory(true)
  const restored = fixture.state.nodes.value[0]
  fixture.state.beginDrag(pointer(100, 100), restored)
  fixture.state.movePointer(pointer(500, 400))
  fixture.state.onWindowBlur()
  assert.deepEqual({ x: restored.x, y: restored.y }, committed)
  assert.equal(fixture.state.active.value.history.past.length, 1)
  await fixture.state.saveState()
  const savedNode = fixture.calls.find(call => call.name === 'saveWorkflow').args[1].nodes[0]
  assert.equal(savedNode.x, committed.x)
  assert.equal(savedNode.y, committed.y)
})

test('Space and middle-button pan preserve graph history and suppress the following node click', async t => {
  const fixture = await setup(t); await settle(); canvas(fixture)
  const before = copy(fixture.state.workflow.value.nodes)
  const key = { key: ' ', code: 'Space', target: { closest: () => null }, preventDefault() {} }
  fixture.state.onKeydown(key)
  fixture.state.beginForcedPan(pointer(100, 100))
  assert.equal(fixture.state.gesture.value.kind, 'pan')
  fixture.state.movePointer(pointer(180, 140)); fixture.state.endPointer(pointer(180, 140))
  fixture.state.onKeyup(key)
  fixture.state.clickNode('requirements')
  fixture.state.requestStatus('requirements', 'active')
  assert.equal(fixture.state.inspectorOpen.value, false)
  assert.deepEqual(copy(fixture.state.workflow.value.nodes), before)
  assert.equal(fixture.state.undoAvailable.value, false)
  const view = copy(fixture.state.viewport.value)
  fixture.state.beginForcedPan(pointer(180, 140, { button: 1 }))
  fixture.state.movePointer(pointer(200, 190)); fixture.state.endPointer(pointer(200, 190))
  assert.equal(fixture.state.viewport.value.x, view.x + 20)
  assert.equal(fixture.state.viewport.value.y, view.y + 50)
  assert.equal(fixture.state.undoAvailable.value, false)
})

test('undo during an in-flight save retains the restored graph and advances the next CAS revision', async t => {
  const pending = deferred(), fixture = await setup(t, { saveWorkflow: () => pending.promise }); await settle()
  const original = fixture.state.nodes.value[0].objective
  fixture.state.selectNode('requirements'); fixture.state.updateNode('objective', '正在提交的新内容')
  const saving = fixture.state.saveState(), snapshot = fixture.calls.find(call => call.name === 'saveWorkflow').args[1]
  fixture.state.restoreHistory()
  pending.resolve(saved(snapshot, 2)); await saving; await settle()
  assert.equal(fixture.state.nodes.value[0].objective, original)
  assert.equal(fixture.state.workflow.value.revision, 3)
  assert.equal(fixture.state.statusText.value, '待保存')
  fixture.stubs.saveWorkflow = async (_, doc, revision) => saved(doc, revision)
  await fixture.runTimer(850)
  const put = fixture.calls.filter(call => call.name === 'saveWorkflow').at(-1)
  assert.equal(put.args[2], 3)
  assert.equal(put.args[1].nodes[0].objective, original)
  fixture.state.restoreHistory(true)
  assert.equal(fixture.state.nodes.value[0].objective, '正在提交的新内容')
})

test('project and task-tree histories are independent and retained when returning to their canvas', async t => {
  const fixture = await setup(t); await settle()
  const original = fixture.state.nodes.value[0].objective
  fixture.state.selectNode('requirements'); fixture.state.updateNode('objective', '主项目草稿')
  fixture.props.tree = 'task:isolated'; await settle()
  assert.equal(fixture.state.undoAvailable.value, false)
  fixture.state.addNode('custom')
  assert.equal(fixture.state.nodes.value.length, 9)
  fixture.props.projectId = 'b'; await settle()
  assert.equal(fixture.state.undoAvailable.value, false)
  fixture.props.projectId = 'a'; await settle()
  fixture.state.restoreHistory()
  assert.equal(fixture.state.nodes.value.length, 8)
  fixture.props.tree = 'main'; await settle()
  assert.equal(fixture.state.nodes.value[0].objective, '主项目草稿')
  fixture.state.restoreHistory()
  assert.equal(fixture.state.nodes.value[0].objective, original)
})

test('native text undo and Space are not intercepted by canvas shortcuts', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.addNode('custom')
  let prevented = 0
  const target = { closest: selector => selector.includes('input') ? {} : null }
  fixture.state.onKeydown({ key: 'z', ctrlKey: true, target, preventDefault() { prevented++ } })
  fixture.state.onKeydown({ key: ' ', code: 'Space', target, preventDefault() { prevented++ } })
  assert.equal(prevented, 0)
  assert.equal(fixture.state.nodes.value.length, 9)
  assert.equal(fixture.state.spaceHeld.value, false)
})

test('blank-canvas search inserts the chosen type at the clicked world position and supports undo', async t => {
  const fixture = await setup(t); await settle(); canvas(fixture)
  Object.assign(fixture.state.viewport.value, { x: 100, y: 80, zoom: .5 })
  fixture.state.onCanvasDoubleClick(pointer(380, 280))
  assert.equal(fixture.state.addOpen.value, true)
  assert.deepEqual(copy(fixture.state.addPosition.value), { x: 520, y: 320 })
  fixture.state.addSearch.value = 'custom'; await settle()
  assert.equal(fixture.state.visibleNodeTypes.value.length, 1)
  fixture.state.searchNodeKey({ key: 'Enter', preventDefault() {} })
  const inserted = fixture.state.workflow.value.nodes.at(-1)
  assert.equal(inserted.type, 'custom')
  assert.equal(inserted.x, 520)
  assert.equal(inserted.y, 320)
  assert.equal(fixture.state.addOpen.value, false)
  fixture.state.restoreHistory()
  assert.equal(fixture.state.nodes.value.length, 8)
})

test('selection and viewport changes do not recalculate routes or mutate geometry at overview scale', async t => {
  const fixture = await setup(t); await settle()
  const geometry = copy(fixture.state.workflow.value.nodes), routes = fixture.state.edgeRoutes.value
  fixture.state.selectNode('ui_design')
  assert.deepEqual([...fixture.state.adjacentEdges.value], ['edge_1', 'edge_2'])
  fixture.state.viewport.value.zoom = .3
  fixture.state.viewport.value.x += 40
  assert.equal(fixture.state.compactView.value, true)
  assert.equal(fixture.state.edgeRoutes.value, routes)
  assert.deepEqual(copy(fixture.state.workflow.value.nodes), geometry)
  fixture.state.viewport.value.zoom = .9
  assert.equal(fixture.state.compactView.value, false)
  assert.equal(fixture.state.edgeRoutes.value, routes)
})

test('a deferred focus from an earlier project cannot move the newly loaded project viewport', async t => {
  const fixture = await setup(t, { workflow: async pid => {
    const result = document(`${pid} 工作流`)
    if (pid === 'b') result.nodes[0].y += 8 // A saved free layout retains its own viewport.
    return result
  } }); await settle(); canvas(fixture)
  fixture.state.focusNode('maintenance', true)
  fixture.props.projectId = 'b'
  await settle()
  assert.equal(fixture.state.workflow.value.name, 'b 工作流')
  assert.deepEqual(copy(fixture.state.viewport.value), document().viewport)
  assert.equal(fixture.state.active.value.editSerial, 0)
})

test('expanding a phase brings its visual leaves into view without saving or dispatching agents', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.canvasEl.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 390, height: 500 }) }
  fixture.state.fitCanvas(false, false)
  const original = copy(fixture.state.workflow.value.nodes), edges = copy(fixture.state.workflow.value.edges)
  fixture.state.toggleDetails('requirements'); fixture.state.toggleDependencies(); await settle()
  assert.equal(fixture.state.expandedIds.value.has('requirements'), true)
  assert.equal(fixture.state.showDependencies.value, true)
  const view = fixture.state.viewport.value
  for (const rect of fixture.state.visualStage('requirements').details) {
    assert.ok(rect.x * view.zoom + view.x >= 0)
    assert.ok((rect.x + rect.width) * view.zoom + view.x <= 390)
    assert.ok(rect.y * view.zoom + view.y >= 0)
    assert.ok((rect.y + rect.height) * view.zoom + view.y <= 500)
  }
  const expandedView = copy(view)
  fixture.state.toggleDetails('requirements'); await settle()
  assert.deepEqual(copy(fixture.state.viewport.value), expandedView, 'collapse keeps the chosen view')
  assert.deepEqual(copy(fixture.state.workflow.value.nodes), original)
  assert.deepEqual(copy(fixture.state.workflow.value.edges), edges)
  assert.equal(fixture.state.active.value.editSerial, 0)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
  assert.equal(fixture.events.length, 0)
})

test('detail navigation focuses the real phase and persistence excludes visual roots, leaves and view flags', async t => {
  const fixture = await setup(t); await settle(); canvas(fixture)
  fixture.state.toggleDetails('requirements'); fixture.state.toggleDependencies(); await settle()
  let focused = ''
  fixture.state.rootEl.value = { querySelector: selector => ({ focus: () => { focused = selector } }) }
  fixture.state.openDetail('requirements', 'acceptance'); await settle()
  assert.equal(fixture.state.selectedId.value, 'requirements')
  assert.equal(focused, '[data-workflow-field="acceptance"]')
  fixture.state.updateNode('acceptance', '确认真实节点的验收标准')
  await fixture.state.saveState()
  const persisted = fixture.calls.find(call => call.name === 'saveWorkflow').args[1]
  assert.equal(persisted.nodes.length, 8)
  assert.equal(persisted.edges.length, 7)
  assert.ok(!JSON.stringify(persisted).includes('workflow-visual-root'))
  assert.ok(!JSON.stringify(persisted).includes('detail:'))
  assert.equal(Object.hasOwn(persisted, 'expandedIds'), false)
  assert.equal(Object.hasOwn(persisted, 'showDependencies'), false)
  assert.equal(persisted.nodes.find(node => node.id === 'requirements').acceptance, '确认真实节点的验收标准')
})

test('a projected drag with no movement or Escape restores every original coordinate without history or writes', async t => {
  const fixture = await setup(t); await settle(); canvas(fixture)
  const original = copy(fixture.state.workflow.value.nodes)
  fixture.state.beginDrag(pointer(100, 100), fixture.state.nodes.value[0])
  fixture.state.endPointer(pointer(100, 100))
  assert.deepEqual(copy(fixture.state.workflow.value.nodes), original)
  fixture.state.beginDrag(pointer(100, 100), fixture.state.nodes.value[0])
  fixture.state.movePointer(pointer(240, 180)); fixture.state.cancelGesture()
  assert.deepEqual(copy(fixture.state.workflow.value.nodes), original)
  assert.equal(fixture.state.active.value.history.past.length, 0)
  assert.equal(fixture.state.active.value.editSerial, 0)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
})

test('phase expansion and dependency visibility are independent for each project and task tree', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.toggleDetails('requirements'); fixture.state.toggleDependencies()
  fixture.props.tree = 'task:42'; await settle()
  assert.equal(fixture.state.expandedIds.value.size, 0)
  assert.equal(fixture.state.showDependencies.value, false)
  fixture.state.toggleDetails('ui_design')
  fixture.props.projectId = 'b'; await settle()
  assert.equal(fixture.state.expandedIds.value.size, 0)
  fixture.props.projectId = 'a'; await settle()
  assert.deepEqual([...fixture.state.expandedIds.value], ['ui_design'])
  fixture.props.tree = 'main'; await settle()
  assert.deepEqual([...fixture.state.expandedIds.value], ['requirements'])
  assert.equal(fixture.state.showDependencies.value, true)
  assert.equal(fixture.calls.filter(call => call.name === 'saveWorkflow').length, 0)
})
