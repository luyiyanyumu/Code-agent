import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick, reactive } from 'vue'
import { agentTreeRows, agentCanStop, agentStateLabel, agentProgress, pendingTeamApprovals, teamNoticePresentation, useProjectAgentTeam } from '../src/lib/agentTeam.js'

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = async () => { await nextTick(); for (let n = 0; n < 10; n++) await Promise.resolve() }
const roster = (pid = 'a', tree = 'main') => ({ lead: { session_id: `${pid}-lead`, project_id: pid, role: 'lead', state: 'waiting', depth: 0 }, agents: [
  { session_id: `${pid}-${tree}-child`, parent_session_id: `${pid}-${tree}-node`, role: 'subagent', depth: 2, title: '测试子任务', state: 'queued' },
  { session_id: `${pid}-${tree}-node`, parent_session_id: `${pid}-lead`, node_id: 'verification', role: 'node', depth: 1, title: '测试验收', state: 'running', activation_id: 'activation-1' },
], limits: { max_depth: 3, max_parallel: 2 }, running: 1, queued: 1 })
function setup(t, overrides = {}) {
  const props = reactive({ pid: 'a', tree: 'main', root: '', visible: true }), calls = [], timers = new Map()
  const originalSet = globalThis.setTimeout, originalClear = globalThis.clearTimeout
  let counter = 0
  globalThis.setTimeout = (fn, ms) => { timers.set(++counter, { fn, ms }); return counter }
  globalThis.clearTimeout = id => timers.delete(id)
  const client = { agentTeam: async (pid, tree) => roster(pid, tree), stopAgent: async () => {}, auditAgent: async () => {}, ...overrides }
  const observed = new Proxy(client, { get: (target, key) => (...args) => { calls.push([key, ...args]); return target[key](...args) } })
  const scope = effectScope(), state = scope.run(() => useProjectAgentTeam({ projectId: () => props.pid, tree: () => props.tree, visible: () => props.visible, leadSessionId: () => `${props.pid}-lead`, conversationId: () => props.root || `${props.pid}-lead`, client: observed }))
  t.after(() => { scope.stop(); globalThis.setTimeout = originalSet; globalThis.clearTimeout = originalClear })
  return { state, props, calls, timers, scope }
}

test('agent tree follows parent identities and skips retired workers without hiding orphaned children', () => {
  const team = roster()
  team.agents.push({ session_id: 'retired', parent_session_id: 'a-lead', role: 'node', node_id: 'old', state: 'retired' }, { session_id: 'orphan', parent_session_id: 'unknown', role: 'subagent', state: 'idle' })
  assert.deepEqual(agentTreeRows(team).map(a => [a.session_id, a.indent]), [['a-lead', 0], ['a-main-node', 1], ['a-main-child', 2], ['orphan', 1]])
  assert.equal(agentCanStop(team.lead), true)
  assert.equal(agentCanStop({ state: 'completed' }), false)
})

test('root dialogue stays selected and worker navigation cannot replace its composer', async t => {
  const f = setup(t); await settle()
  assert.equal(f.state.sessionId.value, 'a-lead')
  assert.equal(f.state.select('a-main-child'), false)
  assert.equal(f.state.sessionId.value, 'a-lead')
  f.state.returnLead(); assert.equal(f.state.sessionId.value, 'a-lead')
  assert.deepEqual(f.calls.map(call => call[0]), ['agentTeam'])
})

test('root-scoped roster discards late prior conversation and accepts same-root task notifications only', async t => {
  const old = deferred(), f = setup(t, { agentTeam: (pid, tree, root) => root === 'a-lead' ? old.promise : Promise.resolve({ ...roster(pid, tree), lead: { session_id: root, role: 'lead' } }) })
  f.props.root = 'a-chat'; await settle()
  assert.equal(f.state.sessionId.value, 'a-chat')
  assert.equal(f.state.team.value.lead.session_id, 'a-chat')
  old.resolve(roster()); await settle()
  assert.equal(f.state.team.value.lead.session_id, 'a-chat')
  assert.equal(f.state.select('a-main-child'), false)
  assert.equal(f.state.notify({ project_id: 'a', lead_session_id: 'another-root', tree_id: 'main' }), false)
  assert.equal(f.state.notify({ project_id: 'a', lead_session_id: 'a-chat', tree_id: 'task:child' }), true)
  assert.deepEqual(f.calls[1], ['agentTeam', 'a', 'main', 'a-chat'])
})

test('late old project responses cannot replace new project Lead or worker roster', async t => {
  const pending = deferred(), f = setup(t, { agentTeam: (pid, tree) => pid === 'a' ? pending.promise : Promise.resolve(roster(pid, tree)) })
  f.props.pid = 'b'; await settle()
  assert.equal(f.state.sessionId.value, 'b-lead')
  pending.resolve(roster('a')); await settle()
  assert.equal(f.state.team.value.lead.session_id, 'b-lead')
  assert.equal(f.state.select('a-main-node'), false)
})

test('tree selection retains the root dialogue and displays legacy node records as background tasks', async t => {
  const f = setup(t); await settle(); f.state.select('a-main-node')
  f.props.tree = 'task:42'; await settle()
  assert.equal(f.state.sessionId.value, 'a-lead')
  assert.equal(f.state.rows.value.some(agent => agent.session_id === 'a-task:42-node'), true)
  assert.equal(Object.hasOwn(f.state, 'assignments'), false)
  assert.equal(f.state.select('a-main-node'), false)
})

test('hidden UI pauses polling, ignores in-flight response and resumes visibly', async t => {
  const pending = deferred(), f = setup(t, { agentTeam: () => pending.promise })
  f.props.visible = false; await settle(); pending.resolve(roster()); await settle()
  assert.equal(f.state.team.value, null)
  assert.equal(f.timers.size, 0)
  f.props.visible = true; await settle()
  assert.equal(f.state.team.value.lead.session_id, 'a-lead')
  assert.equal(f.timers.size, 1)
  f.props.visible = false; await settle(); assert.equal(f.timers.size, 0)
})

test('manual audit sends displayed activation identity and refreshes only current project', async t => {
  const f = setup(t); await settle()
  await f.state.action('a-main-node', 'accepted', '已核对测试', 'activation-1')
  assert.deepEqual(f.calls.find(call => call[0] === 'auditAgent'), ['auditAgent', 'a', 'a-main-node', 'accepted', '已核对测试', 'activation-1'])
  assert.equal(f.calls.filter(call => call[0] === 'agentTeam').length, 2)
  assert.equal(f.state.team.value.agents[1].state, 'running', 'UI does not synthesize completed task states')
})

test('stopping worker cannot mutate selection or error state after project switch', async t => {
  const stopping = deferred(), f = setup(t, { stopAgent: () => stopping.promise }); await settle()
  const action = f.state.action('a-main-node', 'stop')
  f.props.pid = 'b'; await settle(); stopping.reject(new Error('旧项目失败'))
  assert.equal(await action, false)
  assert.equal(f.state.error.value, '')
  assert.equal(f.state.sessionId.value, 'b-lead')
})

test('effect disposal removes poll timers and ignores pending requests', async t => {
  const pending = deferred(), f = setup(t, { agentTeam: () => pending.promise }); f.scope.stop()
  pending.resolve(roster()); await settle()
  assert.equal(f.state.team.value, null)
  assert.equal(f.timers.size, 0)
})

test('runtime phases and elapsed progress distinguish approvals from actual execution', () => {
  const worker = { session_id: 'w', state: 'running', phase: 'waiting_approval', started_at: '2026-10-12T01:00:00Z', last_progress_at: '2026-10-12T01:00:50Z', current_tool: 'bash', blocked_reason: '本次命令需要授权' }
  assert.equal(agentStateLabel(worker), '等待你批准')
  const progress = agentProgress(worker, Date.parse('2026-10-12T01:01:05Z'))
  assert.equal(progress.detail, 'bash · 已用 1分5秒')
  assert.equal(progress.reason, '本次命令需要授权')
  assert.equal(progress.recent, '最近进展 15秒前')
  assert.equal(agentStateLabel({ state: 'waiting', phase: 'waiting_children' }), '等待子任务')
  assert.equal(agentStateLabel({ state: 'running', phase: 'tools' }), '工具执行中')
  assert.equal(agentStateLabel({ state: 'queued', phase: 'queued' }), '排队等待执行')
  assert.equal(agentStateLabel({ state: 'queued', phase: 'tools' }), '排队等待执行')
  assert.equal(agentStateLabel({ state: 'completed', phase: 'tools' }), '已完成', 'terminal state takes precedence over stale runtime phase')
  assert.equal(agentProgress({ state: 'completed', elapsed_seconds: 90 }).elapsed, '1分30秒')
  assert.equal(agentProgress({ state: 'idle' }).detail, '')
  assert.equal(agentProgress({ state: 'idle', started_at: '', elapsed_seconds: 0 }).detail, '')
})

test('team approvals select real executor identities and omit finished requests', () => {
  const team = roster()
  team.agents[0].pending_approval = { approval_id: 'approval-child', status: 'pending', tool: 'bash' }
  team.agents[1].pending_approval = { approval_id: 'approval-ended', status: 'approved', tool: 'write_file' }
  assert.deepEqual(pendingTeamApprovals(team).map(agent => agent.session_id), ['a-main-child'])
  assert.deepEqual(teamNoticePresentation({ session_id: 'w', title: '验证', state: 'failed', result: 'old', error: '构建失败', activation_id: 2 }), { sessionId: 'w', title: '验证', state: '失败', text: '构建失败', activationId: 2 })
  assert.equal(teamNoticePresentation({ result: 'missing identity' }), null)
})

test('team change events coalesce refreshes and preserve a slow polling recovery', async t => {
  const f = setup(t); await settle()
  assert.equal([...f.timers.values()][0].ms, 15000)
  assert.equal(f.state.notify({ project_id: 'other' }), false)
  assert.equal(f.state.notify({ project_id: 'a', tree_id: 'other' }), false)
  assert.equal(f.state.notify({ project_id: 'a', session_id: 'a-main-child' }), true)
  assert.equal(f.state.notify({ project_id: 'a' }), true)
  const changes = [...f.timers.entries()].filter(([, timer]) => timer.ms === 200)
  assert.equal(changes.length, 1)
  const [id, timer] = changes[0]; f.timers.delete(id); timer.fn(); await settle()
  assert.equal(f.calls.filter(call => call[0] === 'agentTeam').length, 2)
  f.state.notify({ project_id: 'a' })
  f.props.visible = false; await settle()
  assert.equal(f.timers.size, 0, 'hidden panels cancel pending event refreshes too')
})

test('continuous change events cannot replace a slow roster read before it renders', async t => {
  const pending = deferred(), f = setup(t, { agentTeam: () => pending.promise })
  const flushNotification = async () => {
    const entry = [...f.timers.entries()].find(([, timer]) => timer.ms === 200)
    assert.ok(entry)
    f.timers.delete(entry[0]); entry[1].fn(); await settle()
  }
  for (let i = 0; i < 3; i++) { f.state.notify({ project_id: 'a' }); await flushNotification() }
  assert.equal(f.calls.length, 1, 'only one roster request runs at a time')
  pending.resolve(roster()); await settle()
  assert.equal(f.state.team.value.lead.session_id, 'a-lead', 'first result renders despite intervening notifications')
  await flushNotification()
  assert.equal(f.calls.length, 2, 'one trailing refresh obtains changes received during the read')
})
