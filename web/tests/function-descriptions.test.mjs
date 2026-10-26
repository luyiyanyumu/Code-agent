import assert from 'node:assert/strict'
import test from 'node:test'
import { functionDescriptionPresentation, publishFunctionDescriptions } from '../src/lib/functionDescriptions.js'
import { subscribeBusinessMeaning } from '../src/lib/businessMeaning.js'
import { useFunctionDescriptions } from '../src/composables/useFunctionDescriptions.js'

const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const progress = extra => ({ state: 'running', running: true, revision: 1, coverage_known: true, coverage_total: 8, described: 3, missing: 5, total: 5, done: 2, written: 2, failed: 0, ...extra })

test('unknown coverage and empty projects cannot claim all descriptions are ready', () => {
  assert.equal(functionDescriptionPresentation(null).tone, 'unknown')
  assert.equal(functionDescriptionPresentation(progress({ running: false, state: 'idle', coverage_known: false })).tone, 'unknown')
  assert.equal(functionDescriptionPresentation(progress({ running: false, state: 'complete', missing: 0, coverage_total: 0, described: 0 })).tone, 'unknown')
  assert.equal(functionDescriptionPresentation(progress({ running: false, state: 'complete', missing: 0, described: 8 })).label, '功能描述已齐 8/8')
})

test('automatic progress reports missing descriptions and failures without hiding an unavailable provider', () => {
  assert.equal(functionDescriptionPresentation(progress()).label, '自动补功能描述：本轮 2/5')
  const waiting = functionDescriptionPresentation(progress({ state: 'waiting', running: false, note: '没有可用的模型' }))
  assert.match(waiting.label, /等待補全|等待补全/)
  assert.match(waiting.title, /没有可用的模型/)
  const failed = functionDescriptionPresentation(progress({ state: 'error', running: false, failed: 2, error: '请求超时' }))
  assert.equal(failed.tone, 'error')
  assert.match(failed.label, /失败 2 条.*待补 5/)
})

test('the current fill batch is separate from project coverage and explains entity counting and reuse', () => {
  const view = functionDescriptionPresentation(progress({ total: 45, done: 12, written: 10, failed: 1, skipped: 1,
    added: 30, stale: 15, reused: 28560, coverage_total: 28605, described: 28570, missing: 35,
    target_counts: { method: 20, field: 15, parameter: 10, file: 0 } }))
  assert.equal(view.label, '自动补功能描述：本轮 12/45')
  assert.doesNotMatch(view.label, /28605|28560/)
  assert.match(view.title, /本轮待补 45 个代码实体，已处理 12 个/)
  assert.match(view.title, /本轮生成 10 条，失败 1 条，跳过 1 条/)
  assert.match(view.title, /新增 30、过期 15、复用 28560/)
  assert.match(view.title, /本轮类型：方法 20、字段 15、参数 10/)
  assert.match(view.title, /项目有效描述 28570\/28605 条，待补 35 条/)
  assert.match(view.title, /分别计数.*有效描述会复用，只补缺失或过期/)
  assert.doesNotMatch(view.title, /文件 0/)
})

test('older status responses do not invent the unknown added, stale, reused or skipped counts', () => {
  const view = functionDescriptionPresentation(progress({ total: 28605, done: 576 }))
  assert.equal(view.label, '自动补功能描述：本轮 576/28605')
  assert.doesNotMatch(view.title, /新增 \d|过期 \d|复用 \d|跳过 \d/)
  assert.match(view.title, /代码实体/)
  const zero = functionDescriptionPresentation(progress({ added: 0, stale: 0, reused: 0, skipped: 0 }))
  assert.match(zero.title, /新增 0、过期 0、复用 0/)
  assert.match(zero.title, /跳过 0 条/)
})

test('checking an unknown inventory does not present an uncollected zero as the pending batch size', () => {
  const view = functionDescriptionPresentation({ state: 'queued', running: true, total: 0, coverage_known: false })
  assert.equal(view.label, '功能描述自动检查中…')
  assert.match(view.title, /本轮待补数量检查中/)
  assert.doesNotMatch(view.title, /本轮待补 0 个/)
})

test('map and editor polling publish a shared revision once per project and worktree', t => {
  const events = []
  t.after(subscribeBusinessMeaning(change => events.push(change)))
  assert.equal(publishFunctionDescriptions('dedup-project', 'main', { revision: 0 }), false)
  assert.equal(publishFunctionDescriptions('dedup-project', 'main', { revision: 1 }, { origin: 'mind-map' }), true)
  assert.equal(publishFunctionDescriptions('dedup-project', 'main', { revision: 1 }, { origin: 'code-view' }), false)
  assert.equal(publishFunctionDescriptions('dedup-project', 'main', { revision: 0 }), false)
  assert.equal(publishFunctionDescriptions('dedup-project', 'task:1', { revision: 1 }), true)
  assert.equal(publishFunctionDescriptions('dedup-other-project', 'main', { revision: 1 }), true)
  assert.equal(events.length, 3)
  assert.equal(events[0].id, '')
  assert.equal(events[0].automatic, true)
  assert.equal(events[0].origin, 'mind-map')
})

test('restarting the backend publishes new descriptions even when its revision restarts lower', t => {
  const events = []
  t.after(subscribeBusinessMeaning(change => events.push(change)))
  assert.equal(publishFunctionDescriptions('restarted-server', 'main', { revision_epoch: 'before', revision: 9 }), true)
  assert.equal(publishFunctionDescriptions('restarted-server', 'main', { revision_epoch: 'after', revision: 1 }), true)
  assert.equal(publishFunctionDescriptions('restarted-server', 'main', { revision_epoch: 'after', revision: 1 }), false)
  assert.equal(publishFunctionDescriptions('restarted-server', 'main', { revision_epoch: 'after', revision: 2 }), true)
  assert.equal(events.length, 3)
  assert.equal(events[1].revision_epoch, 'after')
})

test('project switching cancels an optional description read and discards its late result', async t => {
  const old = deferred(), requests = []
  const observer = useFunctionDescriptions({ client: { indexStatus: (pid, tree, options) => {
    requests.push({ pid, tree, signal: options.signal })
    return pid === 'old' ? old.promise : Promise.resolve({ briefs: progress({ state: 'complete', running: false, missing: 0, described: 8, revision: 0 }) })
  } } })
  t.after(observer.reset)
  const first = observer.refresh('old')
  await Promise.resolve()
  await observer.refresh('new')
  await first
  assert.equal(requests[0].signal.aborted, true)
  old.resolve({ briefs: progress({ described: 999, revision: 99 }) })
  await Promise.resolve()
  assert.equal(observer.state.value.described, 8)
  assert.equal(observer.error.value, '')
})

test('a stalled description status read times out and retries while duplicate reads share one request', async t => {
  const old = deferred(), timers = new Map()
  let reads = 0, timerSeq = 0, signal
  const observer = useFunctionDescriptions({ client: { indexStatus: (_pid, _tree, options) => {
    signal = options.signal
    return ++reads === 1 ? old.promise : Promise.resolve({ briefs: progress({ revision: 0 }) })
  } }, setTimer: callback => { timers.set(++timerSeq, callback); return timerSeq }, clearTimer: key => timers.delete(key) })
  t.after(observer.reset)
  const first = observer.refresh('timeout-project')
  assert.equal(observer.refresh('timeout-project'), first)
  await Promise.resolve()
  assert.equal(reads, 1)
  timers.values().next().value()
  await first
  assert.equal(signal.aborted, true)
  assert.match(observer.error.value, /超时/)
  assert.equal(observer.loading.value, false)
  await observer.refresh('timeout-project')
  assert.equal(reads, 2)
  assert.equal(observer.presentation.value.tone, 'running')
  old.resolve({ briefs: progress({ revision: 999, described: 999 }) })
  await Promise.resolve()
  assert.equal(observer.state.value.described, 3)
})
