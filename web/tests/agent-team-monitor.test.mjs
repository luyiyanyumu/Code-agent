import assert from 'node:assert/strict'
import test from 'node:test'
import { activationRecords, agentResponsibility, agentTone, layoutAgentTree, monitorRows, recordPresentation, teamSummary } from '../src/lib/agentTeamMonitor.js'
import { agentStateLabel } from '../src/lib/agentTeam.js'
import { sessionPhaseLabel } from '../src/lib/sessionPresentation.js'

const team = () => ({ lead: { session_id: 'lead', role: 'lead', depth: 0, state: 'running' }, agents: [
  { session_id: 'parent', parent_session_id: 'lead', role: 'subagent', title: '实现接口', state: 'waiting', depth: 1 },
  { session_id: 'child', parent_session_id: 'parent', role: 'subagent', title: '验证登录', current_tool: 'pytest', state: 'running', depth: 2 },
  { session_id: 'approval', parent_session_id: 'lead', role: 'subagent', title: '构建检查', state: 'waiting', phase: 'waiting_approval', pending_approval: { approval_id: 'a1' }, depth: 1 },
  { session_id: 'done', parent_session_id: 'lead', role: 'subagent', title: '定位问题', state: 'completed', depth: 1 },
  { session_id: 'queue', parent_session_id: 'lead', role: 'subagent', title: '补充用例', state: 'queued', depth: 1 },
] })

test('summary describes current-conversation task states without counting Lead or waiting approvals as active execution', () => {
  assert.deepEqual(teamSummary(team()), { total: 5, running: 1, queued: 1, waiting: 2, completed: 1, attention: 1, active: 4 })
  assert.equal(agentTone({ state: 'failed' }), 'failed')
  assert.equal(agentTone({ state: 'running', phase: 'waiting_question' }), 'attention')
  assert.equal(agentTone({ state: 'cancelled' }), 'idle')
})

test('search and attention filters retain actual ancestors and never invent direct Lead edges', () => {
  const matched = monitorRows(team(), 'PYTEST')
  assert.deepEqual(matched.rows.map(row => row.session_id), ['lead', 'parent', 'child'])
  assert.equal(matched.matched, 1)
  assert.equal(matched.rows[1].contextOnly, true)
  assert.deepEqual(layoutAgentTree(matched.rows).edges.map(edge => [edge.source, edge.target]), [['parent', 'child'], ['lead', 'parent']])
  assert.deepEqual(monitorRows(team(), '', 'attention').rows.map(row => row.session_id), ['lead', 'approval'])
  assert.equal(monitorRows(team(), 'missing').matched, 0)
  assert.deepEqual(monitorRows(team(), 'missing').rows.map(row => row.session_id), ['lead'])
})

test('layout has non-overlapping nodes with complete identities, handles orphaned and cyclic snapshots', () => {
  const value = team()
  value.agents.push({ session_id: 'orphan', parent_session_id: 'missing', role: 'subagent' },
    { session_id: 'cycle-a', parent_session_id: 'cycle-b', role: 'subagent' },
    { session_id: 'cycle-b', parent_session_id: 'cycle-a', role: 'subagent' })
  const graph = layoutAgentTree(monitorRows(value).rows)
  assert.equal(graph.nodes.length, 9)
  assert.equal(new Set(graph.nodes.map(node => node.session_id)).size, 9)
  assert.equal(graph.edges.some(edge => edge.target === 'orphan'), false)
  for (const first of graph.nodes) for (const second of graph.nodes) {
    if (first === second) continue
    assert.ok(first.x + first.width <= second.x || second.x + second.width <= first.x
      || first.y + first.height <= second.y || second.y + second.height <= first.y)
  }
})

const marker = (seq, activation_id, phase) => ({ seq, type: 'session_meta', payload: { agent_activation: { activation_id, phase } } })
test('evidence is restricted to exact activation boundaries, including missing and interrupted executions', () => {
  const events = [marker(1, 1, 'started'), { seq: 2, type: 'done', payload: { text: 'old' } }, marker(3, 1, 'finished'),
    marker(4, 2, 'started'), { seq: 5, type: 'tool_result', payload: { name: 'bash', result: 'current' } },
    marker(6, 3, 'started'), { seq: 7, type: 'done', payload: { text: 'newer' } }, marker(8, 3, 'finished')]
  assert.deepEqual(activationRecords(events, 2), { records: [events[4]], boundary: 'interrupted', omitted: 0 })
  assert.deepEqual(activationRecords(events, 9), { records: [], boundary: 'missing', omitted: 0 })
  assert.deepEqual(activationRecords(events.slice(0, 5), 2), { records: [events[4]], boundary: 'open', omitted: 0 })
  assert.deepEqual(recordPresentation(events[4]), { label: '工具返回', title: 'bash', text: 'current' })
})

test('long execution displays a bounded tail with an explicit omitted count', () => {
  const events = [marker(1, 1, 'started'), ...Array.from({ length: 200 }, (_, n) => ({ seq: n + 2, type: 'tool_result', payload: { result: n } })), marker(202, 1, 'finished')]
  const result = activationRecords(events, 1)
  assert.equal(result.records.length, 150)
  assert.equal(result.records[0].seq, 52)
  assert.equal(result.omitted, 50)
  assert.equal(result.boundary, 'complete')
})

test('responsibility distinguishes explicit read-only, scoped writes, whole workspace and undeclared history', () => {
  const readOnly = agentResponsibility({ scope_mode: 'read_only', write_paths: [], file_ownership: { held: false } })
  assert.equal(readOnly.scopeLabel, '只读')
  assert.equal(readOnly.ownershipLabel, '只读，不占用写入责任')
  const scoped = agentResponsibility({ scope_mode: 'scoped_write', write_paths: ['src/api.js', 'tests/'], delegation_reason: '接口与测试可独立推进', file_ownership: { held: true } })
  assert.deepEqual(scoped.paths, ['src/api.js', 'tests/'])
  assert.equal(scoped.ownershipLabel, '已持有文件责任')
  assert.equal(scoped.reason, '接口与测试可独立推进')
  const workspace = agentResponsibility({ scope_mode: 'workspace_write', write_paths: ['.'], file_ownership: { held: false } })
  assert.equal(workspace.scopeLabel, '整个工作区')
  assert.deepEqual(workspace.paths, ['.'])
  for (const legacy of [{}, { scope_mode: 'legacy', write_paths: [] }, null]) {
    assert.equal(agentResponsibility(legacy).scopeLabel, '未声明')
    assert.equal(agentResponsibility(legacy).readOnly, false)
  }
  assert.equal(agentResponsibility({ write_paths: [] }).readOnly, true)
})

test('ownership names actual blockers and dependencies retain the declared activation', () => {
  const value = agentResponsibility({ scope_mode: 'scoped_write', write_paths: ['src/'],
    depends_on: [{ session_id: 'owner', activation_id: 2 }],
    file_ownership: { held: false, waiting_for: [{ session_id: 'owner', title: '接口负责人', paths: ['src/api.js'] }] } },
  [{ session_id: 'owner', title: '接口实现', activation_id: 9 }])
  assert.equal(value.ownershipLabel, '等待取得文件责任')
  assert.deepEqual(value.blockers, [{ session_id: 'owner', title: '接口负责人', paths: ['src/api.js'] }])
  assert.deepEqual(value.dependencies, [{ session_id: 'owner', activation_id: 2, title: '接口实现' }])
  assert.equal(agentResponsibility({ scope_mode: 'scoped_write', write_paths: ['src/'] }).ownershipLabel, '暂无持有记录')
})

test('anonymous cross-conversation blockers and actual legacy ownership stay visible without inventing identities', () => {
  const value = agentResponsibility({ scope_mode: 'scoped_write', write_paths: ['src/'], file_ownership: { held: false,
    waiting_for: [{ session_id: '', title: '其他对话中的执行者', paths: ['E:/project/src/'] },
      { session_id: '', title: '其他对话中的执行者', paths: ['E:/project/src/a.js'] }] } })
  assert.equal(value.ownershipLabel, '等待取得文件责任')
  assert.equal(value.blockers.length, 2)
  assert.deepEqual(value.blockers.map(owner => owner.session_id), ['', ''])
  assert.equal(value.blockers[0].title, '其他对话中的执行者')
  assert.deepEqual(value.blockers[0].paths, ['E:/project/src/'])
  const legacy = agentResponsibility({ scope_mode: 'legacy', write_paths: ['.'], file_ownership: { held: true, waiting_for: [] } })
  assert.equal(legacy.scopeLabel, '未声明')
  assert.equal(legacy.ownershipLabel, '已持有文件责任')
  assert.equal(agentResponsibility({}).ownershipLabel, '未声明')
})

test('file and dependency waits are waiting states without using running or ordinary queue counters', () => {
  const value = team()
  value.agents.push({ session_id: 'files', parent_session_id: 'lead', role: 'subagent', state: 'queued', phase: 'waiting_files' },
    { session_id: 'dependencies', parent_session_id: 'lead', role: 'subagent', state: 'queued', phase: 'waiting_dependencies' })
  assert.deepEqual(teamSummary(value), { total: 7, running: 1, queued: 1, waiting: 4, completed: 1, attention: 1, active: 6 })
  assert.equal(agentTone(value.agents[5]), 'waiting')
  assert.equal(agentTone(value.agents[6]), 'waiting')
  assert.equal(agentStateLabel(value.agents[5]), '等待文件释放')
  assert.equal(agentStateLabel(value.agents[6]), '等待前置任务')
  assert.equal(sessionPhaseLabel('waiting_files'), '等待文件释放')
  assert.equal(agentStateLabel({ state: 'queued' }), '排队等待执行')
})

test('file and delegation-reason searches preserve the actual parent chain', () => {
  const value = team()
  value.agents[1].write_paths = ['src/auth/very-long-path/session.js']
  value.agents[1].delegation_reason = '核验分工可独立推进'
  for (const query of ['AUTH/VERY-LONG-PATH', '核验分工']) {
    const matched = monitorRows(value, query)
    assert.equal(matched.matched, 1)
    assert.deepEqual(matched.rows.map(row => row.session_id), ['lead', 'parent', 'child'])
  }
})
