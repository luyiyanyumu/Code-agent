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
  let timerId = 0, mount, unmount
  const bindings = {
    defineProps: () => props,
    defineEmits: () => (...args) => calls.push(args),
    defineExpose() {},
    setInterval: (fn, delay) => { timers.set(++timerId, { fn, delay, interval: true }); return timerId },
    clearInterval: (id) => timers.delete(id),
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay, interval: false }); return timerId },
    clearTimeout: (id) => timers.delete(id),
    window: { addEventListener() {}, removeEventListener() {} },
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
  const expose = '({ map, loading, error, detail, loadMap, loadDetail, invalidateDetail, closeDetail, poll, refresh, loadFeatureChildren, featureKids, featureBusyIds, loadBriefState, briefMissing, briefLoading, briefJob, briefPresentation, view, workflowOpened, onlyChanged, changedFiles, changedIds, visibleNodes, visibleLinks, featureMap, featureOpen, featureParent, purposeCache, onBusinessChanged, loadFeatures, hover, onNodeHover })'
  const scope = vue.effectScope()
  const state = scope.run(() => new Function(...Object.keys(bindings), `${script}\nreturn ${expose}`)(...Object.values(bindings)))
  t.after(() => {
    unmount?.()
    scope.stop()
    api.code = originalCodeApi
  })
  return { state, props, stubs, timers, calls, mount: () => mount(), unmount: () => unmount() }
}

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

test('workflow activation keeps the canvas mounted and pauses code index polling', async (t) => {
  const { state: s, stubs } = await setup(t)
  await s.loadMap()
  let calls = 0
  stubs.indexStatus = async () => { calls++; return { change_seq: 1 } }
  s.view.value = 'workflow'
  await vue.nextTick()
  assert.equal(s.workflowOpened.value, true)
  await s.poll()
  assert.equal(calls, 0)
  s.view.value = 'structure'
  await vue.nextTick()
  assert.equal(s.workflowOpened.value, true)
  await s.poll()
  assert.equal(calls, 1)
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

test('automatic description status shares the index poll and never invokes the removed manual fill', async t => {
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
  assert.doesNotMatch(source, /fillBriefs|fillFeatureBriefs|briefButtonLabel|具体业务含义|业务含义/)
  assert.match(source, /class="description-progress"[\s\S]*?role="status"/)
  assert.match(source, /<template v-if="view !== 'workflow'">\s*<div class="description-progress"/,
    'automatic progress is visible in structure and feature views')
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
