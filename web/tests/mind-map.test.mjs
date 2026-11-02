import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as vue from 'vue'
import { api } from '../src/api.js'
import { subscribeBusinessMeaning } from '../src/lib/businessMeaning.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const mapFixture = (name, seq = 1) => ({
  change_seq: seq, fixture: name,
  structure: { id: 'root', kind: 'dir', name, path: '', children: [] },
  modules: [], edges: { imports: [] }, stats: {}, scan: {}, warnings: [],
})

async function setup(t, initialProps = {}) {
  const originalCodeApi = api.code
  const calls = []
  const stubs = {
    map: async (pid, opts) => mapFixture(`${pid}:${opts.tree}`),
    indexStatus: async () => ({ change_seq: 1, scanning: false, briefs: { running: false, state: 'complete', revision: 0, coverage_known: true, coverage_total: 1, described: 1, missing: 0 } }),
    features: async () => ({ domains: [] }),
  }
  // 即使测试中的 watcher 新增取数，也不允许误触真实网络/扫描/模型调用。
  api.code = new Proxy(stubs, {
    get(target, key) {
      return target[key] || (async () => { throw new Error(`Unstubbed API: ${String(key)}`) })
    },
  })
  const props = vue.reactive({ projectId: 'a', tree: 'main', diagnostics: null, overlay: null, ...initialProps })
  const timers = new Map()
  const frames = new Map(), listeners = new Map()
  let timerId = 0, frameId = 0, clock = 0, mount, unmount
  const testWindow = {
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(callback)
    },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback) },
    requestAnimationFrame(callback) { frames.set(++frameId, callback); return frameId },
    cancelAnimationFrame(id) { frames.delete(id) },
  }
  const bindings = {
    defineProps: () => props,
    defineEmits: () => (...args) => calls.push(args),
    defineExpose() {},
    setInterval: (fn, delay) => { timers.set(++timerId, { fn, delay, interval: true }); return timerId },
    clearInterval: (id) => timers.delete(id),
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay, interval: false }); return timerId },
    clearTimeout: (id) => timers.delete(id),
    window: testWindow,
    performance: { now: () => clock },
  }
  const url = new URL('../src/components/code/MindMap.vue', import.meta.url)
  const source = readFileSync(url, 'utf8')
  let script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  const imports = []
  script = script.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*$/gm, (_, clause, specifier) => {
    imports.push({ clause: clause.trim(), specifier })
    return ''
  })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? vue
      : specifier.endsWith('.vue') ? { default: {} }
        : await import(new URL(specifier, url))
    if (clause.startsWith('{')) {
      for (const entry of clause.slice(1, -1).split(',').map((s) => s.trim()).filter(Boolean)) {
        const [key, local = key] = entry.split(/\s+as\s+/)
        bindings[local] = namespace[key]
      }
    } else bindings[clause] = namespace.default
  }
  bindings.onMounted = (fn) => { mount = fn }
  bindings.onBeforeUnmount = (fn) => { unmount = fn }
  const expose = '({ map, loading, error, detail, loadMap, loadDetail, invalidateDetail, closeDetail, poll, refresh, loadFeatureChildren, featureKids, featureBusyIds, loadBriefState, briefMissing, briefLoading, briefJob, briefPresentation, retryBriefs, briefRetrying, openModelSettings, view, onlyChanged, changedFiles, changedIds, visibleNodes, visibleLinks, featureMap, featureOpen, featureParent, purposeCache, onBusinessChanged, loadFeatures, hover, onNodeHover, onNodeLeave, cameraEl, svgEl, pan, zoom, layout, userZoomed, onPointerDown, onPointerMove, onPointerUp, onWheel })'
  const scope = vue.effectScope()
  const state = scope.run(() => new Function(...Object.keys(bindings), `${script}\nreturn ${expose}`)(...Object.values(bindings)))
  t.after(() => {
    unmount?.()
    scope.stop()
    api.code = originalCodeApi
  })
  return { state, props, stubs, timers, calls, frames, listeners,
    dispatch(type, event = {}) { for (const callback of [...(listeners.get(type) || [])]) callback({ ...event, type }) },
    frame() { const pending = [...frames.values()]; frames.clear(); for (const callback of pending) callback(16) },
    advance(milliseconds) { clock += milliseconds },
    mount: () => mount(), unmount: () => unmount() }
}

function pointer(clientX, clientY, more = {}) {
  return { button: 0, pointerId: 7, clientX, clientY, preventDefault() {}, ...more }
}

async function cameraSetup(t) {
  const fixture = await setup(t), s = fixture.state, writes = [], classes = new Set(), captures = new Set()
  s.svgEl.value = {
    getBoundingClientRect: () => ({ left: 30, top: 50, width: 1200, height: 800 }),
    closest: () => ({ getBoundingClientRect: () => ({ left: 30, top: 50, width: 1200, height: 800 }) }),
    classList: {
      add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name),
      toggle(name, force) { const add = force ?? !classes.has(name); if (add) classes.add(name); else classes.delete(name); return add },
    },
    setPointerCapture: id => captures.add(id), releasePointerCapture: id => captures.delete(id),
    hasPointerCapture: id => captures.has(id),
  }
  s.cameraEl.value = { setAttribute: (name, value) => writes.push({ name, value }) }
  await vue.nextTick()
  fixture.frame()
  writes.length = 0
  return { ...fixture, writes, classes, captures }
}

test('one hundred pointer moves paint one camera frame while preserving scene and layout identities', async t => {
  const fixture = await cameraSetup(t), { state: s, writes, frames } = fixture
  const layout = s.layout.value, nodes = s.visibleNodes.value, links = s.visibleLinks.value
  s.onPointerDown(pointer(100, 100))
  s.onPointerMove(pointer(101, 101))
  assert.deepEqual(s.pan.value, { x: 0, y: 0 }, 'a sub-threshold initial movement remains a click')
  assert.equal(frames.size, 0)
  for (let i = 1; i <= 100; i++) s.onPointerMove(pointer(100 + i, 100 + i / 2))
  assert.deepEqual(s.pan.value, { x: 100, y: 50 })
  assert.equal(frames.size, 1, 'all input samples share the next animation frame')
  assert.equal(writes.length, 0, 'event handlers do not repeatedly paint the scene')
  await vue.nextTick()
  assert.equal(s.layout.value, layout)
  assert.equal(s.visibleNodes.value, nodes)
  assert.equal(s.visibleLinks.value, links)
  fixture.frame()
  assert.deepEqual(writes, [{ name: 'transform', value: 'translate(100, 50) scale(1)' }])
  assert.equal(frames.size, 0)
  s.onPointerMove(pointer(101, 101))
  assert.deepEqual(s.pan.value, { x: 1, y: 1 }, 'an active drag still follows movement back inside the initial click threshold')
  fixture.dispatch('pointerup', pointer(101, 101))
  assert.deepEqual(s.pan.value, { x: 1, y: 1 })
  assert.equal(frames.size, 0)
  assert.deepEqual(writes.at(-1), { name: 'transform', value: 'translate(1, 1) scale(1)' })
})

test('camera zoom preserves the graph coordinate beneath the cursor and coalesces pan and scale writes', async t => {
  const fixture = await cameraSetup(t), { state: s, writes, frames } = fixture
  s.pan.value = { x: 80, y: -20 }; s.zoom.value = 0.8
  fixture.frame(); writes.length = 0
  const oldPan = { ...s.pan.value }, oldZoom = s.zoom.value, cursor = { x: 210, y: 150 }
  let prevented = false
  // The viewport starts at (30,50); pass client coordinates for its local cursor.
  fixture.state.onWheel({ clientX: cursor.x + 30, clientY: cursor.y + 50, deltaY: -1,
    preventDefault() { prevented = true } })
  assert.equal(prevented, true)
  assert.equal(frames.size, 1)
  assert.equal(writes.length, 0)
  assert.ok(Math.abs((cursor.x - s.pan.value.x) / s.zoom.value - (cursor.x - oldPan.x) / oldZoom) < 1e-10)
  assert.ok(Math.abs((cursor.y - s.pan.value.y) / s.zoom.value - (cursor.y - oldPan.y) / oldZoom) < 1e-10)
  fixture.frame()
  assert.equal(writes.length, 1)
})

test('pointer release flushes the release position once and removes all transient drag listeners', async t => {
  const fixture = await cameraSetup(t), { state: s, writes, frames, listeners } = fixture
  s.onPointerDown(pointer(100, 100)); s.onPointerMove(pointer(125, 120))
  assert.equal(frames.size, 1)
  fixture.dispatch('pointerup', pointer(140, 130))
  assert.deepEqual(s.pan.value, { x: 40, y: 30 })
  assert.equal(frames.size, 0, 'the pending paint cannot run again after release')
  assert.deepEqual(writes, [{ name: 'transform', value: 'translate(40, 30) scale(1)' }])
  for (const type of ['pointermove', 'pointerup', 'pointercancel', 'blur']) assert.equal(listeners.get(type)?.size || 0, 0, type)
  s.onPointerMove(pointer(200, 200))
  assert.deepEqual(s.pan.value, { x: 40, y: 30 }, 'movement after release no longer pans')
})

test('pointer cancellation and window blur finish the last known pan without accepting later movement', async t => {
  const fixture = await cameraSetup(t), { state: s, frames, listeners } = fixture
  for (const type of ['pointercancel', 'blur']) {
    const before = { ...s.pan.value }
    s.onPointerDown(pointer(100, 100)); s.onPointerMove(pointer(140, 130))
    fixture.dispatch(type, type === 'pointercancel' ? pointer(140, 130) : {})
    assert.deepEqual(s.pan.value, { x: before.x + 40, y: before.y + 30 }, type)
    assert.equal(frames.size, 0, type)
    for (const event of ['pointermove', 'pointerup', 'pointercancel', 'blur']) assert.equal(listeners.get(event)?.size || 0, 0, `${type}/${event}`)
    s.onPointerMove(pointer(200, 200))
    assert.deepEqual(s.pan.value, { x: before.x + 40, y: before.y + 30 }, type)
  }
})

test('unrelated pointers cannot move or end the active pan, and losing capture finishes it safely', async t => {
  const fixture = await cameraSetup(t), { state: s, frames, listeners, captures } = fixture
  s.onPointerDown(pointer(100, 100))
  assert.equal(captures.has(7), true)
  s.onPointerDown(pointer(200, 200, { pointerId: 9 }))
  s.onPointerMove(pointer(150, 140, { pointerId: 9 }))
  fixture.dispatch('pointerup', pointer(150, 140, { pointerId: 9 }))
  assert.deepEqual(s.pan.value, { x: 0, y: 0 })
  assert.equal(listeners.get('pointermove').size, 1)
  s.onPointerMove(pointer(150, 140))
  s.onPointerUp(pointer(150, 140, { type: 'lostpointercapture' }))
  assert.deepEqual(s.pan.value, { x: 50, y: 40 })
  assert.equal(frames.size, 0)
  assert.equal(captures.size, 0)
  assert.equal(listeners.get('pointermove').size, 0)
})

test('dragging dismisses node documentation and suppresses new hover requests until release', async t => {
  const fixture = await cameraSetup(t), { state: s, timers } = fixture
  const node = { id: 'sym:Example.java#run', kind: 'method', label: 'run' }
  await s.onNodeHover(node, pointer(100, 100))
  assert.ok(s.hover.value)
  assert.equal([...timers.values()].filter(timer => timer.delay === 140).length, 1)
  s.onPointerDown(pointer(100, 100)); s.onPointerMove(pointer(120, 120))
  assert.equal(s.hover.value, null)
  await s.onNodeHover(node, pointer(120, 120))
  assert.equal(s.hover.value, null)
  assert.equal([...timers.values()].filter(timer => timer.delay === 140).length, 0)
  s.onPointerUp(pointer(120, 120))
  await s.onNodeHover(node, pointer(120, 120))
  assert.ok(s.hover.value, 'normal node explanations remain available after movement')
  assert.equal([...timers.values()].filter(timer => timer.delay === 140).length, 1)
})

test('wheel zoom suppresses transient node tips without losing normal hover after the interaction settles', async t => {
  const fixture = await cameraSetup(t), { state: s, timers } = fixture
  const node = { id: 'sym:Example.java#run', kind: 'method', label: 'run' }
  s.onWheel({ clientX: 100, clientY: 100, deltaY: -1, preventDefault() {} })
  await s.onNodeHover(node, pointer(100, 100))
  assert.equal(s.hover.value, null)
  assert.equal([...timers.values()].filter(timer => timer.delay === 140).length, 0)
  fixture.advance(161)
  await s.onNodeHover(node, pointer(100, 100))
  assert.ok(s.hover.value)
  assert.equal([...timers.values()].filter(timer => timer.delay === 140).length, 1)
})

test('unmount cancels pending camera work and releases drag listeners without writing after teardown', async t => {
  const fixture = await cameraSetup(t), { state: s, frames, writes, listeners } = fixture
  s.onPointerDown(pointer(100, 100)); s.onPointerMove(pointer(160, 140))
  assert.equal(frames.size, 1)
  fixture.unmount()
  assert.equal(frames.size, 0)
  const writesAfterUnmount = writes.length
  fixture.frame()
  assert.equal(writes.length, writesAfterUnmount)
  for (const type of ['pointermove', 'pointerup', 'pointercancel', 'blur']) assert.equal(listeners.get(type)?.size || 0, 0, type)
  s.onPointerMove(pointer(200, 200))
  assert.equal(frames.size, 0)
})

test('map load keeps the newer request and its loading state when older responses arrive', async (t) => {
  const { state: s, stubs } = await setup(t)
  const first = deferred(), second = deferred()
  let n = 0
  stubs.map = () => ++n === 1 ? first.promise : second.promise
  const oldLoad = s.loadMap()
  const newLoad = s.loadMap()
  first.resolve(mapFixture('old'))
  await oldLoad
  assert.equal(s.map.value, null)
  assert.equal(s.loading.value, true)
  second.resolve(mapFixture('new'))
  await newLoad
  assert.equal(s.map.value.fixture, 'new')
  assert.equal(s.loading.value, false)
})

test('structure and feature maps keep code polling and contain no workflow entry or API', async t => {
  const { state: s, stubs } = await setup(t)
  await s.loadMap()
  let statusReads = 0, featureReads = 0
  stubs.indexStatus = async () => { statusReads++; return { change_seq: 1 } }
  stubs.features = async () => { featureReads++; return { domains: [] } }
  s.view.value = 'features'
  await vue.nextTick()
  await s.poll()
  assert.equal(featureReads, 1)
  assert.equal(statusReads, 1)
  s.view.value = 'structure'
  await vue.nextTick()
  await s.poll()
  assert.equal(statusReads, 2)
  const source = readFileSync(new URL('../src/components/code/MindMap.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /SopWorkflow|workflow|工作流/)
  assert.match(source, /@click="view = 'structure'"/)
  assert.match(source, /@click="view = 'features'"/)
})

test('old map errors cannot clear a newer successful map', async (t) => {
  const { state: s, stubs } = await setup(t)
  const first = deferred()
  let n = 0
  stubs.map = () => ++n === 1 ? first.promise : Promise.resolve(mapFixture('new'))
  const pending = s.loadMap()
  await s.loadMap()
  first.reject(new Error('old failure'))
  await pending
  assert.equal(s.map.value.fixture, 'new')
  assert.equal(s.error.value, '')
})

test('project and tree switches reject old map responses', async (t) => {
  const { state: s, props, stubs } = await setup(t)
  const first = deferred()
  stubs.map = (pid, opts) => pid === 'a' && opts.tree === 'main'
    ? first.promise : Promise.resolve(mapFixture(`${pid}:${opts.tree}`))
  const pending = s.loadMap()
  props.projectId = 'b'
  props.tree = 'work'
  await vue.nextTick()
  await Promise.resolve()
  first.resolve(mapFixture('a:main'))
  await pending
  assert.equal(s.map.value.fixture, 'b:work')
})

test('latest symbol selection wins and all detail results carry the captured tree', async (t) => {
  const { state: s, props, stubs } = await setup(t)
  props.tree = 'work'
  await vue.nextTick()
  const first = deferred(), second = deferred()
  stubs.symbol = (pid, id) => id === 'sym:first' ? first.promise : second.promise
  const oldDetail = s.loadDetail({ id: 'sym:first' })
  const newDetail = s.loadDetail({ id: 'sym:second' })
  second.resolve({ id: 'sym:second', name: 'second' })
  await newDetail
  first.resolve({ id: 'sym:first', name: 'first' })
  await oldDetail
  assert.equal(s.detail.value.id, 'sym:second')
  assert.equal(s.detail.value.tree, 'work')
  await s.loadDetail({ id: 'mod:src', kind: 'module', path: 'src' })
  assert.equal(s.detail.value.tree, 'work')
})

test('invalidating/closing a detail prevents its delayed response from reopening it', async (t) => {
  const { state: s, stubs } = await setup(t)
  const pending = deferred()
  stubs.symbol = () => pending.promise
  const opening = s.loadDetail({ id: 'sym:late' })
  s.invalidateDetail()
  s.closeDetail()
  pending.resolve({ id: 'sym:late' })
  await opening
  assert.equal(s.detail.value, null)
})

test('old symbol failure cannot replace the new project detail', async (t) => {
  const { state: s, props, stubs } = await setup(t)
  const pending = deferred()
  stubs.symbol = () => pending.promise
  const oldDetail = s.loadDetail({ id: 'sym:old' })
  props.projectId = 'b'
  await vue.nextTick()
  await s.loadDetail({ id: 'mod:b', kind: 'module' })
  pending.reject(new Error('old detail error'))
  await oldDetail
  assert.equal(s.detail.value.id, 'mod:b')
  assert.equal(s.detail.value.error, undefined)
})

test('polling is single-flight and cannot apply a delta from the previous project', async (t) => {
  const { state: s, props, stubs } = await setup(t)
  await s.loadMap()
  const status = deferred(), delta = deferred()
  let statusCalls = 0, deltaCalls = 0
  stubs.indexStatus = () => { statusCalls += 1; return status.promise }
  stubs.mapDelta = () => { deltaCalls += 1; return delta.promise }
  const pending = s.poll()
  await s.poll()
  assert.equal(statusCalls, 1)
  status.resolve({ change_seq: 2, scanning: false })
  await Promise.resolve()
  assert.equal(deltaCalls, 1)
  props.projectId = 'b'
  await vue.nextTick()
  delta.resolve({ to_seq: 20, changes: [], added: [], updated: [], removed: [] })
  await pending
  assert.equal(s.map.value.fixture, 'b:main')
})

test('old feature children and brief counts cannot populate a new project', async (t) => {
  const { state: s, props, stubs } = await setup(t)
  const children = deferred(), brief = deferred()
  stubs.featureChildren = () => children.promise
  stubs.indexStatus = (pid) => pid === 'a' ? brief.promise : Promise.resolve({ briefs: { coverage_known: true, missing: 2 } })
  const oldChildren = s.loadFeatureChildren({ id: 'feature:old', kind: 'feature' })
  const oldBrief = s.loadBriefState()
  props.projectId = 'b'
  await vue.nextTick()
  children.resolve({ children: [{ id: 'feature:old-child' }] })
  brief.resolve({ briefs: { coverage_known: true, missing: 999 } })
  await Promise.all([oldChildren, oldBrief])
  assert.equal(s.featureKids.value['feature:old'], undefined)
  assert.equal(s.featureBusyIds.value.size, 0)
  assert.equal(s.briefMissing.value, 2)
})

test('unmount while initial map is loading prevents updates and a late polling timer', async (t) => {
  const { state: s, stubs, mount, unmount, timers } = await setup(t)
  const pending = deferred()
  stubs.map = () => pending.promise
  const mounting = mount()
  unmount()
  pending.resolve(mapFixture('late'))
  await mounting
  assert.equal(s.map.value, null)
  assert.equal(timers.size, 0)
})

test('an old scan completion cannot trigger a reload or notice in the new project', async (t) => {
  const { state: s, props, stubs } = await setup(t)
  const scan = deferred()
  stubs.scan = () => scan.promise
  let mapLoads = 0
  stubs.map = async (pid, opts) => { mapLoads += 1; return mapFixture(`${pid}:${opts.tree}`) }
  const pending = s.refresh(false)
  props.projectId = 'b'
  await vue.nextTick()
  const loadsAfterSwitch = mapLoads
  scan.resolve({})
  await pending
  assert.equal(mapLoads, loadsAfterSwitch)
  assert.equal(s.map.value.fixture, 'b:main')
})

test('a shared business save updates feature summaries without losing expanded children or unrelated cached branches', async t => {
  const { state: s, stubs } = await setup(t)
  s.featureMap.value = { domains: [{ id: 'fdomain:src', count: 1, features: [{ id: 'sym:src/Service.java#list', label: '旧说明' }] }] }
  s.featureKids.value = {
    'sym:src/Service.java#container': { children: [{ id: 'sym:src/Service.java#list', label: '旧说明' }] },
    'sym:src/Other.java#other': { children: [{ id: 'sym:src/Other.java#child', label: '其他分支' }] },
  }
  s.featureOpen.value = { 'fdomain:src': true, 'sym:src/Service.java#container': true }
  s.purposeCache.set('sym:src/Service.java#list', { headline: '旧说明' })
  const requests = []
  stubs.features = async () => ({ domains: [{ id: 'fdomain:src', count: 1, features: [{ id: 'sym:src/Service.java#list', label: '新的人工说明' }] }] })
  stubs.featureChildren = async (_, id) => { requests.push(id); return { children: [{ id: 'sym:src/Service.java#list', label: '新的人工说明' }] } }
  await s.onBusinessChanged({ detail: { projectId: 'a', tree: 'main', id: 'sym:src/Service.java#list' } })
  assert.equal(s.purposeCache.size, 0)
  assert.equal(s.featureMap.value.domains[0].features[0].label, '新的人工说明')
  assert.equal(s.featureKids.value['sym:src/Service.java#container'].children[0].label, '新的人工说明')
  assert.equal(s.featureKids.value['sym:src/Other.java#other'].children[0].label, '其他分支')
  assert.deepEqual(s.featureOpen.value, { 'fdomain:src': true, 'sym:src/Service.java#container': true })
  assert.deepEqual(requests, ['sym:src/Service.java#container'])
})

test('shared business refresh ignores another project/tree and rejects a late response after project switch', async t => {
  const { state: s, props, stubs } = await setup(t)
  s.featureMap.value = { domains: [] }
  const pending = deferred()
  let requests = 0
  stubs.features = () => { requests++; return pending.promise }
  await s.onBusinessChanged({ detail: { projectId: 'b', tree: 'main', id: 'mod:src' } })
  await s.onBusinessChanged({ detail: { projectId: 'a', tree: 'different', id: 'mod:src' } })
  assert.equal(requests, 0)
  const refresh = s.onBusinessChanged({ detail: { projectId: 'a', tree: 'main', id: 'mod:src' } })
  props.projectId = 'b'
  await vue.nextTick()
  pending.resolve({ domains: [{ id: 'fdomain:old-project', count: 999 }] })
  await refresh
  assert.equal(s.featureMap.value, null)
})

const controllerId = 'sym:src/Controller.java#Controller'
const methodId = 'sym:src/Controller.java#Controller.list(long)'
const stepId = 'sym:src/Repository.java#Repository.query(long)'
const freshNavigation = () => ({ domains: [{ id: 'fdomain:src', count: 1, features: [{ id: controllerId, label: '当前源码说明', has_children: true, business: { status: 'stale', business_meaning: '' } }] }] })
const seededFeatures = s => {
  s.featureMap.value = { domains: [{ id: 'fdomain:src', count: 1, features: [{ id: controllerId, label: '旧人工说明', has_children: true }] }] }
  s.featureKids.value = {
    [controllerId]: { children: [{ id: methodId, label: '旧方法业务', has_children: true }] },
    [methodId]: { children: [{ id: stepId, label: '旧底层业务', has_children: false }] },
    'sym:deleted': { children: [{ id: 'sym:deleted-child', label: '已删除符号' }] },
  }
  s.featureOpen.value = { 'fdomain:src': true, [controllerId]: true, [methodId]: true, 'sym:deleted': true }
}
const freshChildren = id => ({ children: id === controllerId ? [{ id: methodId, label: '当前方法源码说明', has_children: true }] : [{ id: stepId, label: '当前查询源码说明', has_children: false }] })

test('manual rebuild invalidates human summaries and restores expanded reachable branches with current business status', async t => {
  const { state: s, stubs } = await setup(t)
  await s.loadMap()
  seededFeatures(s)
  s.view.value = 'features'; await vue.nextTick()
  const requests = []
  stubs.scan = async () => ({ change_seq: 3 })
  stubs.map = async () => mapFixture('rebuilt', 3)
  stubs.features = async () => freshNavigation()
  stubs.featureChildren = async (_, id) => { requests.push(id); return freshChildren(id) }
  await s.refresh(true)
  assert.equal(s.map.value.change_seq, 3)
  assert.equal(s.featureMap.value.domains[0].features[0].label, '当前源码说明')
  assert.equal(s.featureMap.value.domains[0].features[0].business.status, 'stale')
  assert.equal(s.featureKids.value[controllerId].children[0].label, '当前方法源码说明')
  assert.equal(s.featureKids.value[methodId].children[0].label, '当前查询源码说明')
  assert.equal(s.featureKids.value['sym:deleted'], undefined)
  assert.equal(s.featureOpen.value[controllerId], true)
  assert.equal(s.featureOpen.value[methodId], true)
  assert.deepEqual(requests, [controllerId, methodId], 'removed and collapsed nodes never get extra child requests')
})

test('code-index polling restores expanded feature branches after a delta instead of leaving stale labels or empty opened rows', async t => {
  const { state: s, stubs } = await setup(t)
  await s.loadMap()
  seededFeatures(s)
  s.view.value = 'features'; await vue.nextTick()
  stubs.indexStatus = async () => ({ change_seq: 2, scanning: false })
  stubs.mapDelta = async () => ({ to_seq: 2, changes: [], added: [], updated: [], removed: [] })
  stubs.features = async () => freshNavigation()
  stubs.featureChildren = async (_, id) => freshChildren(id)
  await s.poll()
  assert.equal(s.featureMap.value.domains[0].features[0].label, '当前源码说明')
  assert.equal(s.featureKids.value[methodId].children[0].label, '当前查询源码说明')
  assert.equal(s.featureOpen.value[methodId], true)
})

test('a full map read seeing a newer index invalidates hidden feature data and the next view activation restores expanded branches', async t => {
  const { state: s, stubs } = await setup(t)
  await s.loadMap()
  seededFeatures(s)
  stubs.map = async () => mapFixture('new-index', 4)
  stubs.features = async () => freshNavigation()
  stubs.featureChildren = async (_, id) => freshChildren(id)
  await s.loadMap({ keepExpand: true })
  assert.equal(s.featureMap.value, null)
  assert.deepEqual(s.featureKids.value, {})
  assert.equal(s.featureOpen.value[controllerId], true)
  await s.loadFeatures()
  assert.equal(s.featureMap.value.domains[0].features[0].label, '当前源码说明')
  assert.equal(s.featureKids.value[methodId].children[0].label, '当前查询源码说明')
})

test('restoring expanded branches cannot bring old project children back after a project switch', async t => {
  const { state: s, props, stubs } = await setup(t)
  seededFeatures(s)
  s.view.value = 'features'; await vue.nextTick()
  const children = deferred()
  stubs.features = async pid => pid === 'a' ? freshNavigation() : { domains: [] }
  stubs.featureChildren = () => children.promise
  const restoring = s.loadFeatures()
  for (let n = 0; n < 5; n++) await Promise.resolve()
  assert.equal(s.featureBusyIds.value.has(controllerId), true)
  props.projectId = 'b'; await vue.nextTick()
  children.resolve(freshChildren(controllerId))
  await restoring
  assert.deepEqual(s.featureMap.value.domains, [])
  assert.deepEqual(s.featureKids.value, {})
  assert.equal(s.featureBusyIds.value.size, 0)
})

test('automatic description status shares the index poll without starting manual generation', async t => {
  const { state: s, stubs, mount, timers } = await setup(t, { projectId: 'automatic-mount' })
  let manualCalls = 0, obsoleteStatusCalls = 0
  stubs.fillFeatureBriefs = async () => { manualCalls++; throw new Error('Manual generation must not run') }
  stubs.featureBriefs = async () => { obsoleteStatusCalls++; throw new Error('Use the existing index status') }
  stubs.indexStatus = async (_, tree) => ({ briefs: { state: 'queued', running: true, revision: 0, coverage_known: false, total: 0, tree } })
  await mount()
  for (let n = 0; n < 5; n++) await Promise.resolve()
  assert.equal(s.briefJob.value.state, 'queued')
  assert.equal(s.briefPresentation.value.label, '功能描述自动检查中…')
  assert.equal(s.briefMissing.value, null)
  assert.deepEqual([...timers.values()].filter(timer => timer.interval).map(timer => timer.delay), [2500])
  assert.equal(manualCalls, 0)
  assert.equal(obsoleteStatusCalls, 0)
  const source = readFileSync(new URL('../src/components/code/MindMap.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /fillBriefs|briefButtonLabel|具体业务含义|业务含义/)
  assert.match(source, /class="description-progress"[\s\S]*?role="status"/)
  assert.match(source, /<div class="description-progress"/, 'automatic progress stays visible in both map views')
})

test('explicit description retry is single-flight, preserves the selected worktree and offers model settings', async t => {
  const { state: s, stubs, calls } = await setup(t, { tree: 'task:work' })
  await s.loadMap()
  const pending = deferred(), requests = []
  stubs.fillFeatureBriefs = async (pid, options) => { requests.push({ pid, options }); return pending.promise }
  const first = s.retryBriefs()
  await s.retryBriefs()
  assert.equal(requests.length, 1)
  assert.equal(s.briefRetrying.value, true)
  assert.deepEqual(requests[0], { pid: 'a', options: { tree: 'task:work' } })
  pending.resolve({ state: 'waiting', running: false, block_reason: 'model_not_configured', action: 'configure_model', coverage_known: true, missing: 12 })
  await first
  assert.equal(s.briefPresentation.value.action, 'configure_model')
  assert.equal(s.briefRetrying.value, false)
  s.openModelSettings()
  assert.deepEqual(calls.at(-1), ['open-settings', 'models'])
})

test('unknown, unavailable model, failed, and API errors remain visible instead of claiming descriptions are complete', async t => {
  const { state: s, stubs } = await setup(t, { projectId: 'automatic-status' })
  assert.equal(s.briefPresentation.value.tone, 'unknown')
  stubs.indexStatus = async () => ({ briefs: { state: 'idle', revision: 0, coverage_known: false, missing: 0 } })
  await s.loadBriefState()
  assert.equal(s.briefPresentation.value.label, '功能描述待检查')
  stubs.indexStatus = async () => ({ briefs: { state: 'waiting', revision: 0, coverage_known: true,
    coverage_total: 5, described: 1, missing: 4, note: '没有可用的模型配置' } })
  await s.loadBriefState()
  assert.match(s.briefPresentation.value.label, /等待补全.*待补 4/)
  assert.match(s.briefPresentation.value.title, /没有可用的模型配置/)
  stubs.indexStatus = async () => ({ briefs: { state: 'error', revision: 0, coverage_known: true,
    coverage_total: 5, described: 2, missing: 3, total: 4, done: 4, written: 1, failed: 3, error: '模型返回失败' } })
  await s.loadBriefState()
  assert.match(s.briefPresentation.value.label, /失败 3 条.*待补 3/)
  assert.equal(s.briefPresentation.value.tone, 'error')
  stubs.indexStatus = async () => { throw new Error('Network unavailable') }
  await s.loadBriefState()
  assert.equal(s.briefPresentation.value.label, '功能描述状态暂不可用')
  assert.equal(s.briefPresentation.value.tone, 'error')
  assert.doesNotMatch(s.briefPresentation.value.label, /已齐/)
  await s.loadMap()
  stubs.indexStatus = async () => ({ change_seq: 1 })
  await s.poll()
  assert.equal(s.briefPresentation.value.label, '功能描述状态暂不可用')
})

test('each written description revision refreshes feature labels while the job still runs, retaining expansion and the open detail', async t => {
  const pid = 'automatic-batches'
  const { state: s, stubs } = await setup(t, { projectId: pid })
  await s.loadMap()
  await s.loadBriefState()
  seededFeatures(s)
  s.view.value = 'features'; await vue.nextTick()
  const openDetail = { id: controllerId, tree: 'main', name: 'Controller' }
  s.detail.value = openDetail
  s.purposeCache.set(controllerId, { headline: '旧说明' })
  let navigationReads = 0
  const childReads = [], events = []
  const unsubscribe = subscribeBusinessMeaning(change => events.push(change))
  t.after(unsubscribe)
  stubs.features = async () => { navigationReads++; return freshNavigation() }
  stubs.featureChildren = async (_, id) => { childReads.push(id); return freshChildren(id) }
  let revision = 1
  stubs.indexStatus = async () => ({ change_seq: 1, scanning: false, briefs: { state: 'running', running: true,
    revision, total: 10, done: revision * 2, written: revision, failed: 0, coverage_known: true,
    coverage_total: 10, described: revision, missing: 10 - revision } })
  await s.poll()
  assert.equal(navigationReads, 1)
  assert.deepEqual(childReads, [controllerId, methodId])
  assert.equal(s.featureKids.value[methodId].children[0].label, '当前查询源码说明')
  assert.equal(s.featureOpen.value[methodId], true)
  assert.equal(s.detail.value.id, openDetail.id, 'automatic prose refresh keeps the current drawer open')
  assert.equal(s.purposeCache.size, 0)
  assert.equal(s.briefJob.value.running, true)
  assert.match(s.briefPresentation.value.label, /自动补功能描述：本轮 2\/10/)
  assert.deepEqual(events.map(({ projectId, tree, id, revision, origin }) => ({ projectId, tree, id, revision, origin })),
    [{ projectId: pid, tree: 'main', id: '', revision: 1, origin: 'mind-map' }])
  await s.poll()
  assert.equal(navigationReads, 1, 'unchanged progress never re-reads the navigation')
  await s.onBusinessChanged({ detail: events[0] })
  assert.equal(navigationReads, 1, 'the map does not react twice to its own broadcast')
  revision = 2
  await s.poll()
  assert.equal(navigationReads, 2)
  assert.equal(events.length, 2)
  assert.equal(s.briefJob.value.running, true, 'the view need not wait for job completion')
})

test('a batch event from the editor refreshes shared descriptions and keeps expanded reachable branches', async t => {
  const { state: s, stubs } = await setup(t)
  seededFeatures(s)
  s.view.value = 'features'; await vue.nextTick()
  s.detail.value = { id: methodId, tree: 'main' }
  s.purposeCache.set(methodId, { headline: '旧说明' })
  stubs.features = async () => freshNavigation()
  stubs.featureChildren = async (_, id) => freshChildren(id)
  await s.onBusinessChanged({ detail: { projectId: 'a', tree: 'main', id: '', revision: 7, origin: 'code-view' } })
  assert.equal(s.purposeCache.size, 0)
  assert.equal(s.featureMap.value.domains[0].features[0].label, '当前源码说明')
  assert.equal(s.featureKids.value[methodId].children[0].label, '当前查询源码说明')
  assert.equal(s.featureOpen.value[methodId], true)
  assert.equal(s.detail.value.id, methodId)
})

test('a delayed automatic status cannot update or broadcast after a tree switch', async t => {
  const { state: s, props, stubs } = await setup(t, { projectId: 'automatic-tree-switch' })
  await s.loadMap()
  await s.loadBriefState()
  const status = deferred(), events = []
  stubs.indexStatus = (_, tree) => tree === 'main' ? status.promise : Promise.resolve({ briefs: { state: 'waiting', revision: 0,
    coverage_known: true, coverage_total: 3, described: 1, missing: 2 } })
  const unsubscribe = subscribeBusinessMeaning(change => events.push(change))
  t.after(unsubscribe)
  const pending = s.poll()
  props.tree = 'work'; await vue.nextTick()
  for (let n = 0; n < 5; n++) await Promise.resolve()
  status.resolve({ change_seq: 1, briefs: { revision: 100, state: 'complete', coverage_known: true,
    coverage_total: 999, described: 999, missing: 0 } })
  await pending
  assert.equal(s.briefMissing.value, 2)
  assert.equal(s.briefJob.value.state, 'waiting')
  assert.equal(events.length, 0)
})

test('an initial status returning after a newer poll cannot restore old progress', async t => {
  const { state: s, stubs } = await setup(t, { projectId: 'automatic-status-race' })
  await s.loadMap()
  const first = deferred()
  let calls = 0
  stubs.indexStatus = async () => ++calls === 1 ? first.promise : { change_seq: 1, briefs: {
    state: 'running', running: true, revision: 1, total: 10, done: 4, written: 4, coverage_known: true,
    coverage_total: 10, described: 4, missing: 6,
  } }
  const loading = s.loadBriefState()
  await s.poll()
  first.resolve({ change_seq: 1, briefs: { state: 'queued', running: true, revision: 0, total: 0,
    coverage_known: false } })
  await loading
  assert.equal(s.briefJob.value.done, 4)
  assert.equal(s.briefMissing.value, 6)
  assert.equal(s.briefPresentation.value.label, '自动补功能描述：本轮 4/10')
  assert.equal(s.briefLoading.value, false)
})

test('a backend restart at the same numeric revision refreshes expanded function descriptions', async t => {
  const { state: s, stubs } = await setup(t, { projectId: 'automatic-backend-restart' })
  await s.loadMap()
  let epoch = 'server-one'
  stubs.indexStatus = async () => ({ change_seq: 1, briefs: { state: 'complete', running: false,
    revision: 1, revision_epoch: epoch, coverage_known: true, coverage_total: 3, described: 3, missing: 0 } })
  await s.loadBriefState()
  seededFeatures(s)
  s.view.value = 'features'; await vue.nextTick()
  s.detail.value = { id: methodId, tree: 'main' }
  s.purposeCache.set(methodId, { headline: '上一进程缓存的描述' })
  let reads = 0
  stubs.features = async () => { reads++; return freshNavigation() }
  stubs.featureChildren = async (_, id) => freshChildren(id)
  epoch = 'server-two'
  await s.poll()
  assert.equal(reads, 1)
  assert.equal(s.purposeCache.size, 0)
  assert.equal(s.featureKids.value[methodId].children[0].label, '当前查询源码说明')
  assert.equal(s.featureOpen.value[methodId], true)
  assert.equal(s.detail.value.id, methodId)
})

test('a generated description appears once as the shared function, while actual source documentation remains separate', async t => {
  const { state: s, stubs, timers } = await setup(t)
  stubs.symbolPurpose = async () => ({ headline: '控制器方法', brief: { text: '查询可访问的文档列表' },
    business: { status: 'unreviewed', source: 'model', business_meaning: '查询可访问的文档列表' } })
  await s.onNodeHover({ id: methodId, kind: 'method', label: 'list' }, { clientX: 0, clientY: 0 })
  await [...timers.values()].find(timer => timer.delay === 140).fn()
  assert.equal(s.hover.value.documentation, null)
  assert.equal(s.hover.value.business.business_meaning, '查询可访问的文档列表')
  s.purposeCache.clear()
  stubs.symbolPurpose = async () => ({ documentation: { text: '返回当前用户可访问的列表；分页上限为 100。', source: '作者说明' },
    business: { status: 'current', source: 'human', business_meaning: '查询可访问的文档列表' } })
  await s.onNodeHover({ id: methodId, kind: 'method', label: 'list' }, { clientX: 0, clientY: 0 })
  await [...timers.values()].filter(timer => timer.delay === 140).at(-1).fn()
  assert.equal(s.hover.value.documentation.text, '返回当前用户可访问的列表；分页上限为 100。')
  assert.equal(s.hover.value.business.business_meaning, '查询可访问的文档列表')
})
