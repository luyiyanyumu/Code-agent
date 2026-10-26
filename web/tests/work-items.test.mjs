import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick, reactive } from 'vue'
import { EXECUTION_MODES, filterWorkItems, hasWorkApproval, useWorkItems, workItemActive, workItemApprovals, workItemCanOpen, workItemGroup, workItemLegacy, workItemMode, workItemProgress, workItemSession, workItemState, workItemTree, workItemVerification } from '../src/lib/workItems.js'

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = async () => { await nextTick(); for (let n = 0; n < 10; n++) await Promise.resolve() }
const items = () => [
  { id: 'agent:s1', kind: 'agent', session_id: 's1', state: 'running', phase: 'waiting_approval', title: '后端接口', pending_approval: { approval_id: 'a1', status: 'pending' }, agent: { role: 'subagent' } },
  { id: 'task:t1', kind: 'task', task_id: 't1', title: '登录页面', state: 'planned' },
  { id: 'batch:b1', kind: 'batch', batch_id: 'b1', title: '搜索功能', state: 'running', progress: { total: 3, merged: 1, running: 2 } },
  { id: 'agent:s2', kind: 'agent', session_id: 's2', title: '验证', state: 'completed', result: '编译通过', audit: { decision: 'accepted' }, verification: { ok: true, results: [{ command: 'build' }] } },
]
function setup(t, overrides = {}) {
  const props = reactive({ pid: 'p1', visible: true }), calls = [], timers = new Map()
  const originalSet = globalThis.setTimeout, originalClear = globalThis.clearTimeout
  let count = 0
  globalThis.setTimeout = (fn, ms) => { timers.set(++count, { fn, ms }); return count }
  globalThis.clearTimeout = id => timers.delete(id)
  const raw = { workItems: async pid => ({ project_id: pid, items: items() }), createWorkItem: async (_pid, payload) => ({ item: { id: 'agent:new', kind: 'agent', state: 'queued', title: payload.requirement } }), ...overrides }
  const client = new Proxy(raw, { get: (target, key) => (...args) => { calls.push([key, ...args]); return target[key](...args) } })
  const scope = effectScope(), state = scope.run(() => useWorkItems({ projectId: () => props.pid, visible: () => props.visible, client }))
  t.after(() => { scope.stop(); globalThis.setTimeout = originalSet; globalThis.clearTimeout = originalClear })
  return { props, state, calls, timers, scope }
}

test('unified modes distinguish project execution from isolated worktrees and serial integration', () => {
  assert.deepEqual(EXECUTION_MODES.map(mode => mode.id), ['team', 'isolated', 'parallel'])
  assert.match(EXECUTION_MODES[2].description, /按顺序合并/)
  assert.equal(workItemMode({ kind: 'agent', agent: { role: 'lead' } }), 'Lead')
  assert.equal(workItemMode({ kind: 'task' }), '隔离执行')
  assert.equal(workItemMode({ kind: 'batch' }), '隔离并行')
})
test('pending permissions outrank running state without treating completed approval as pending', () => {
  const agent = items()[0]
  assert.equal(workItemGroup(agent), 'attention')
  assert.equal(workItemState(agent), '等待权限批准')
  agent.pending_approval.status = 'approved'
  assert.equal(hasWorkApproval(agent), false)
  assert.equal(workItemGroup(agent), 'active')
})
test('recursive worker approvals open their actual executor rather than the parent task session', () => {
  assert.equal(workItemSession({ session_id: 'parent', pending_approval: { approval_id: 'a1', session_id: 'child', status: 'pending' } }), 'child')
  assert.equal(workItemSession({ session_id: 'parent', pending_approval: { approval_id: 'a1', session_id: 'child', status: 'approved' } }), 'parent')
  assert.equal(workItemSession({ agent: { session_id: 'worker', pending_approval: { approval_id: 'a2', session_id: 'grandchild' } } }), 'grandchild')
})
test('parallel approval selection excludes resolved requests and closed worktrees cannot open sessions', () => {
  const item = { kind: 'task', state: 'review', session_id: 'parent', pending_approvals: [{ approval_id: 'old', session_id: 'old', status: 'approved' }, { approval_id: 'new', session_id: 'child', status: 'pending' }] }
  assert.equal(workItemApprovals(item).length, 1); assert.equal(workItemSession(item), 'child')
  assert.equal(workItemCanOpen(item), true)
  item.state = 'merged'; assert.equal(workItemCanOpen(item), false)
  item.state = 'review'; item.workspace_available = false; assert.equal(workItemCanOpen(item), false)
  assert.equal(workItemCanOpen({ kind: 'agent', state: 'completed', session_id: 'historical-child', workspace_available: false }), false)
})
test('legacy task sessions remain visible but cannot route through the managed-team selector', () => {
  const legacy = { kind: 'task', state: 'review', task_id: 'old', session_id: 'old-session', agent: null, execution_managed: false, workspace_available: true }
  assert.equal(workItemLegacy(legacy), true); assert.equal(workItemCanOpen(legacy), false)
  assert.equal(workItemLegacy({ ...legacy, execution_managed: true }), false)
  assert.equal(workItemCanOpen({ ...legacy, execution_managed: true }), true)
  assert.equal(workItemLegacy({ kind: 'task', session_id: '', execution_managed: false }), false, 'unexecuted plans are not legacy sessions')
  assert.equal(workItemLegacy({ task_id: 'old-lane', session_id: 'old-lane-session', agent: null }), true)
  assert.equal(workItemCanOpen({ task_id: 'old-lane', session_id: 'old-lane-session', agent: null }), false)
})
test('a completed parent with active recursive workers remains in progress rather than ready for review', () => {
  const item = { kind: 'task', state: 'review', active: true, phase: 'review_pending', agent: { state: 'completed' }, agents: [{ session_id: 'child', state: 'running', current_tool: 'build', elapsed_seconds: 5 }] }
  assert.equal(workItemActive(item), true); assert.equal(workItemGroup(item), 'active'); assert.equal(workItemState(item), '等待子任务')
  assert.equal(workItemProgress(item), 'build · 已用 5秒')
  assert.equal(workItemActive({ status: 'review', agents: [{ state: 'queued' }] }), true)
  assert.equal(workItemActive({ status: 'deferred' }), false, 'deferred dependency lanes must allow completed predecessors to merge')
})
test('a batch-wide approval opens the correct lane workspace for the selected recursive executor', () => {
  const item = { kind: 'batch', tree_id: 'main', pending_approvals: [{ approval_id: 'a1', session_id: 'child' }], children: [{ tree_id: 'task:lane-one', pending_approvals: [{ approval_id: 'a1', session_id: 'child' }] }] }
  assert.equal(workItemTree(item, 'child'), 'task:lane-one')
  assert.equal(workItemTree({ tree_id: 'main', batch: { lanes: [{ task_id: 'lane-two', pending_approval: { approval_id: 'a2', session_id: 'other' } }] } }, 'other'), 'task:lane-two')
})
test('execution completion, Lead acceptance and Git integration remain separate states', () => {
  const worker = { kind: 'agent', state: 'completed', audit: { decision: 'none' }, agent: { role: 'subagent' } }
  assert.equal(workItemState(worker), '完成待审查'); assert.equal(workItemGroup(worker), 'attention')
  worker.audit.decision = 'accepted'
  assert.equal(workItemState(worker), '已验收'); assert.equal(workItemGroup(worker), 'completed')
  worker.audit.decision = 'rejected'
  assert.equal(workItemState(worker), '需返工'); assert.equal(workItemGroup(worker), 'attention')
  assert.equal(workItemState({ kind: 'task', state: 'merged' }), '已合并')
})
test('unified filters combine execution state with title, result and errors', () => {
  assert.deepEqual(filterWorkItems(items(), 'attention').map(item => item.id), ['agent:s1', 'task:t1'])
  assert.deepEqual(filterWorkItems(items(), 'completed', '编译').map(item => item.id), ['agent:s2'])
  assert.equal(filterWorkItems(items(), 'active', '登录').length, 0)
})
test('progress and validation use reported results rather than inferring success from completion', () => {
  assert.equal(workItemProgress(items()[2]), '1/3 项已合并 · 2 项执行中')
  assert.equal(workItemVerification(items()[3]), '验证通过 · 1 条命令')
  assert.equal(workItemVerification({ state: 'completed' }), '')
  assert.equal(workItemVerification({ task: { verification: { ok: false, results: [{ command: 'test' }, { command: 'build' }] } } }), '验证未通过 · 2 条命令')
  assert.equal(workItemVerification({ verification: { ok: null, stale: true } }), '代码已变化，需重新验证')
  assert.equal(workItemVerification({ kind: 'batch', verification: { ok: null, total: 3, verified: 1, failed: 0 } }), '已验证 1/3 项')
})
test('one project-wide read supplies all execution modes without posting or selecting a worker', async t => {
  const f = setup(t); await settle()
  assert.deepEqual(f.calls.map(call => call[0]), ['workItems'])
  assert.deepEqual(f.state.counts.value, { total: 4, active: 1, attention: 2, completed: 1 })
  assert.equal(f.state.selected.value, null)
  assert.equal(f.state.select('unknown'), false)
})
test('late reads from the old project cannot replace the new list or selected task', async t => {
  const pending = deferred(), f = setup(t, { workItems: pid => pid === 'p1' ? pending.promise : Promise.resolve({ project_id: pid, items: [{ id: 'task:p2', kind: 'task', title: '项目二', state: 'planned' }] }) })
  f.props.pid = 'p2'; await settle(); f.state.select('task:p2')
  pending.resolve({ project_id: 'p1', items: items() }); await settle()
  assert.equal(f.state.data.value.project_id, 'p2'); assert.equal(f.state.selected.value.id, 'task:p2')
})
test('hidden lists stop polling and discard in-flight responses until shown again', async t => {
  const pending = deferred(), f = setup(t, { workItems: () => pending.promise })
  f.props.visible = false; await settle()
  pending.resolve({ project_id: 'p1', items: items() }); await settle()
  assert.equal(f.state.data.value, null); assert.equal(f.timers.size, 0)
  assert.equal(f.state.notify({ project_id: 'p1' }), false)
  f.props.visible = true; await settle()
  assert.equal(f.state.items.value.length, 4)
})
test('tool event bursts coalesce and do not starve a slow project read', async t => {
  const pending = deferred(), f = setup(t, { workItems: () => pending.promise })
  for (let n = 0; n < 10; n++) f.state.notify({ project_id: 'p1', tree_id: `task:${n}` })
  assert.equal([...f.timers.values()].filter(timer => timer.ms === 200).length, 1)
  await f.state.refresh()
  assert.equal(f.calls.length, 1)
  pending.resolve({ project_id: 'p1', items: items() }); await settle()
  assert.equal(f.state.items.value.length, 4)
  assert.equal(f.state.notify({ project_id: 'other' }), false)
})
test('read failures keep existing tasks visible and retry clears the error', async t => {
  let fail = false
  const f = setup(t, { workItems: async () => { if (fail) throw new Error('读取失败'); return { items: items() } } }); await settle()
  fail = true; await f.state.refresh()
  assert.equal(f.state.error.value, '读取失败'); assert.equal(f.state.items.value.length, 4)
  fail = false; await f.state.refresh()
  assert.equal(f.state.error.value, '')
})
test('creation passes its explicit execution mode and selects the returned item once', async t => {
  const f = setup(t); await settle()
  const payload = { requirement: '开发搜索', execution_mode: 'parallel', plan_first: true, git_init: false, tree: 'main' }
  await f.state.create(payload)
  assert.deepEqual(f.calls.at(-1), ['createWorkItem', 'p1', payload])
  assert.equal(f.state.selected.value.id, 'agent:new'); assert.equal(f.state.creating.value, false)
})
test('a creation completing after project switch cannot open an old session', async t => {
  const pending = deferred(), f = setup(t, { createWorkItem: () => pending.promise }); await settle()
  const creation = f.state.create({ requirement: '旧项目需求', execution_mode: 'team' })
  f.props.pid = 'p2'; await settle()
  pending.resolve({ item: { id: 'agent:old', kind: 'agent', session_id: 'old' } })
  assert.equal(await creation, null)
  assert.equal(f.state.selectedId.value, ''); assert.equal(f.state.items.value.some(item => item.id === 'agent:old'), false)
})
