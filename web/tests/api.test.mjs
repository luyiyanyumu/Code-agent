import test from 'node:test'
import assert from 'node:assert/strict'
import { api, getToken, setToken } from '../src/api.js'

function mock(t, name, value) {
  const previous = globalThis[name]
  globalThis[name] = value
  t.after(() => { globalThis[name] = previous })
}

test('new sessions only send folder and permission settings, including when stale callers supply removed modes', async t => {
  mock(t, 'localStorage', { getItem: () => null })
  const calls = []
  mock(t, 'fetch', async (path, options) => {
    calls.push({ path, method: options.method, body: JSON.parse(options.body) })
    return new Response('{}')
  })
  await api.createSession({ folder: 'project', permissionMode: 'workspace-write', loopMode: 'cage' })
  assert.deepEqual(calls, [{ path: '/api/v1/sessions', method: 'POST', body: { folder: 'project', permission_mode: 'workspace-write' } }])
  for (const removed of ['setSessionLoopMode', 'loopModeSettings', 'saveLoopModeSettings']) assert.equal(removed in api, false)
})

test('question reads and answers encode exact executor/group identities without posting a prompt', async t => {
  const calls = []
  mock(t, 'localStorage', { getItem: () => null })
  mock(t, 'fetch', async (path, options) => { calls.push([path, options.method, options.body && JSON.parse(options.body)]); return new Response('{}') })
  await api.pendingQuestions('worker/2')
  await api.answerQuestion('worker/2', 'question/a b', { answers: [{ id: 'one', custom: '回答' }] })
  assert.deepEqual(calls, [
    ['/api/v1/sessions/worker%2F2/questions/pending', 'GET', undefined],
    ['/api/v1/sessions/worker%2F2/questions/question%2Fa%20b/answer', 'POST', { answers: [{ id: 'one', custom: '回答' }] }],
  ])
})

test('local workspace requests work when browser storage is unavailable', async t => {
  mock(t, 'localStorage', { getItem() { throw new Error('disabled') }, removeItem() { throw new Error('disabled') } })
  mock(t, 'fetch', async (path, options) => {
    assert.equal(path, '/api/v1/auth/me')
    assert.equal(options.headers.Authorization, undefined)
    return new Response(JSON.stringify({ id: 'local-user', role: 'admin' }))
  })
  assert.equal(getToken(), '')
  assert.doesNotThrow(() => setToken(''))
  assert.equal((await api.me()).id, 'local-user')
})

test('clearing legacy credentials cannot restore the old token', t => {
  const values = new Map([['ch-agent-token', 'legacy']])
  mock(t, 'localStorage', { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) })
  assert.equal(getToken(), 'legacy')
  assert.equal(values.has('ch-agent-token'), false)
  values.set('ch-agent-token', 'old')
  setToken('')
  assert.equal(getToken(), '')
})

test('REST fallback preserves selected code context and web setting', async t => {
  mock(t, 'localStorage', { getItem: () => null })
  const codeContext = { project_id: 'project', path: 'demo.py' }
  mock(t, 'fetch', async (path, options) => {
    assert.equal(path, '/api/v1/sessions/session/prompt')
    assert.deepEqual(JSON.parse(options.body), { text: 'review', web: false, code_context: codeContext })
    return new Response(JSON.stringify({ ok: true }))
  })
  await api.prompt('session', 'review', [], { web: false, codeContext })
})

test('route mismatch and missing resource produce distinct errors', async t => {
  mock(t, 'localStorage', { getItem: () => null })
  mock(t, 'fetch', async () => new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404 }))
  await assert.rejects(api.code.meta(), { status: 404, message: /重启后端/ })
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: '文件不存在' }), { status: 404 })
  await assert.rejects(api.code.file('project', 'missing.py'), { status: 404, message: '文件不存在' })
})

test('agent roster and controls preserve encoded identities and audit activation', async t => {
  const calls = []
  mock(t, 'localStorage', { getItem: () => null })
  mock(t, 'fetch', async (path, options) => { calls.push({ path, method: options.method, body: options.body ? JSON.parse(options.body) : null }); return new Response('{}') })
  await api.code.agentTeam('p/1', 'task:a b')
  await api.code.agentTeam('p/1', 'main', 'root/2')
  await api.code.stopAgent('p/1', 'child/2')
  await api.code.stopAgent('p/1', 'child/2', 'root/2')
  await api.code.sendAgentMessage('p/1', 'child/2', '补充约束')
  await api.code.followupAgent('p/1', 'child/2', '继续验证')
  await api.code.followupAgent('p/1', 'child/2', '重新分析', 'none')
  await api.code.interruptAgent('p/1', 'child/2')
  await api.code.auditAgent('p/1', 'child/2', 'accepted', '已复核', 'act/3')
  assert.deepEqual(calls, [
    { path: '/api/v1/code/projects/p%2F1/agents?tree=task%3Aa%20b', method: 'GET', body: null },
    { path: '/api/v1/code/projects/p%2F1/agents?tree=main&conversation_id=root%2F2', method: 'GET', body: null },
    { path: '/api/v1/code/projects/p%2F1/agents/child%2F2/stop', method: 'POST', body: {} },
    { path: '/api/v1/code/projects/p%2F1/agents/child%2F2/stop', method: 'POST', body: { conversation_id: 'root/2' } },
    { path: '/api/v1/code/projects/p%2F1/agents/child%2F2/message', method: 'POST', body: { message: '补充约束' } },
    { path: '/api/v1/code/projects/p%2F1/agents/child%2F2/followup', method: 'POST', body: { prompt: '继续验证', context_strategy: 'task_brief' } },
    { path: '/api/v1/code/projects/p%2F1/agents/child%2F2/followup', method: 'POST', body: { prompt: '重新分析', context_strategy: 'none' } },
    { path: '/api/v1/code/projects/p%2F1/agents/child%2F2/interrupt', method: 'POST', body: {} },
    { path: '/api/v1/code/projects/p%2F1/agents/child%2F2/audit', method: 'POST', body: { decision: 'accepted', reason: '已复核', activation_id: 'act/3' } },
  ])
})

test('removed workflow API is unavailable while map and collaboration methods remain callable', () => {
  assert.equal(Object.hasOwn(api.code, 'workflow'), false)
  assert.equal(Object.hasOwn(api.code, 'saveWorkflow'), false)
  for (const method of ['map', 'features', 'agentTeam', 'stopAgent', 'conversations', 'createConversation']) {
    assert.equal(typeof api.code[method], 'function', method)
  }
})

test('team budgets use their separate settings endpoint without saving collaboration limits', async t => {
  const calls = []
  mock(t, 'localStorage', { getItem: () => null })
  mock(t, 'fetch', async (path, options) => { calls.push({ path, method: options.method, body: options.body ? JSON.parse(options.body) : null }); return new Response('{}') })
  await api.settings.agentTeamBudgets()
  const budget = { task_tokens: 10000, task_seconds: 300, team_tokens: 50000, team_seconds: 1200 }
  await api.settings.saveAgentTeamBudgets(budget)
  assert.deepEqual(calls, [
    { path: '/api/v1/settings/agent-team/budgets', method: 'GET', body: null },
    { path: '/api/v1/settings/agent-team/budgets', method: 'PUT', body: budget },
  ])
})

test('memory updates and deletion encode stable record and scope identities with revision guards', async t => {
  const calls = []
  mock(t, 'localStorage', { getItem: () => null })
  mock(t, 'fetch', async (path, options) => { calls.push({ path, method: options.method, body: options.body ? JSON.parse(options.body) : null }); return new Response('{}') })
  await api.updateMemory('fact/1 &', { scope_id: 'project:p/1', revision: 7, enabled: false })
  await api.deleteMemoryRecord('project:p/1', 'fact/1 &', 7)
  await api.deleteMemory('workspace', 2, 'session/1')
  assert.deepEqual(calls, [
    { path: '/api/v1/memory/fact%2F1%20%26', method: 'PATCH', body: { scope_id: 'project:p/1', revision: 7, enabled: false } },
    { path: '/api/v1/memory?scope_id=project%3Ap%2F1&entry_id=fact%2F1%20%26&revision=7', method: 'DELETE', body: null },
    { path: '/api/v1/memory?scope=workspace&index=2&session_id=session%2F1', method: 'DELETE', body: null },
  ])
})
