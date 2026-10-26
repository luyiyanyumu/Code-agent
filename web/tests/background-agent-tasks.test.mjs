import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'

const settle = async () => { await Vue.nextTick(); await new Promise(setImmediate); await Vue.nextTick() }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const all = node => [node, ...node.children.flatMap(all)]
const text = node => node.type === '#comment' ? '' : node.text + node.children.map(text).join('')
const roster = (root = 'root') => ({ lead: { session_id: root, role: 'lead', pending_approval: { approval_id: `${root}-own`, tool: 'bash' }, pending_questions: [{ id: 'own-question', status: 'pending' }] }, agents: [{ session_id: `${root}-child`, parent_session_id: root, role: 'subagent', title: '验证实现', state: 'running', phase: 'waiting_approval', effective_permission: 'read-only', pending_approval: { approval_id: `${root}-approval`, tool: 'bash', arguments: { command: 'npm test' }, purpose: '验证当前改动', effects: ['只运行测试'] }, pending_questions: [{ id: 'child-question', session_id: `${root}-child`, status: 'pending', questions: [{ id: 'one', question: '使用哪种测试？' }] }] }] })

async function setup(t, overrides = {}) {
  const calls = [], events = [], warnings = []
  const client = { events: async () => [{ seq: 1, type: 'done', payload: { text: '只读执行证据' } }], decideApproval: async () => ({ delivered: true }), answerQuestion: async () => ({ delivered: true }), ...overrides }
  const api = Object.fromEntries(Object.entries(client).map(([name, fn]) => [name, (...args) => { calls.push([name, ...args]); return fn(...args) }]))
  const url = new URL('../src/components/code/BackgroundAgentTasks.vue', import.meta.url)
  const source = (await readFile(url, 'utf8')).replace('</script>', '\ndefineExpose({ testState: () => ({workers,approvals,questions,decide,answer,readRecords,records,errors,submitting,open}) })\n</script>')
  const { descriptor, errors } = parse(source); assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: 'background-tasks-test', inlineTemplate: true })
  const approval = Vue.defineComponent({ props: ['approval', 'submitting', 'readonly'], emits: ['decide'], setup: (props, { emit }) => () => Vue.h('article', { 'data-approval': props.approval.approval_id, 'data-submitting': props.submitting, 'data-readonly': props.readonly }, [Vue.h('pre', JSON.stringify(props.approval)), Vue.h('button', { onClick: () => emit('decide', true) }, '仅批准本次')]) })
  const question = Vue.defineComponent({ props: ['group'], emits: ['answer'], setup: (props, { emit }) => () => Vue.h('button', { 'data-question': props.group.id, onClick: () => emit('answer', [{ id: 'one', selected: ['单元测试'] }]) }, '回答子任务') })
  const bindings = {}, imports = []
  let script = compiled.content.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?\r?$/gm, (_, clause, specifier) => { imports.push({ clause: clause.trim(), specifier }); return '' })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? Vue : specifier.endsWith('/api.js') ? { api } : specifier.endsWith('/ApprovalRequest.vue') ? { default: approval } : specifier.endsWith('/QuestionRequest.vue') ? { default: question } : specifier.endsWith('.vue') ? { default: Vue.defineComponent({ render: () => Vue.h('span') }) } : await import(new URL(specifier, url))
    if (clause.startsWith('{')) for (const entry of clause.slice(1, -1).split(',').map(value => value.trim()).filter(Boolean)) { const [key, local = key] = entry.split(/\s+as\s+/); bindings[local] = namespace[key] }
    else bindings[clause] = namespace.default
  }
  const Subject = new Function(...Object.keys(bindings), script.replace('export default', 'return'))(...Object.values(bindings))
  const node = (type, value = '') => Vue.markRaw({ type, text: value, props: {}, children: [], parent: null })
  const remove = child => { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null }
  const insert = (child, parent, anchor) => { remove(child); const at = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(at < 0 ? parent.children.length : at, 0, child); child.parent = parent }
  const renderer = Vue.createRenderer({ createElement: type => node(type), createText: value => node('#text', value), createComment: value => node('#comment', value), insert, remove, parentNode: child => child.parent, nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] || null, patchProp: (el, key, old, value) => { el.props[key] = value }, setText: (el, value) => { el.text = value }, setElementText: (el, value) => { el.text = value; el.children = [] }, setScopeId() {}, insertStaticContent(value, parent, anchor) { const el = node('#static', value); insert(el, parent, anchor); return [el, el] } })
  const root = node('root'), props = Vue.reactive({ selectedId: 'root', team: roster(), visible: true })
  let state
  const app = renderer.createApp({ render: () => Vue.h(Subject, { ...props, ref: instance => { if (instance) state = instance.testState() }, onRefresh: () => events.push('refresh'), onStop: id => events.push(['stop', id]) }) })
  app.config.warnHandler = message => warnings.push(message); app.mount(root)
  t.after(() => { app.unmount(); assert.deepEqual(warnings, []) })
  await settle()
  return { root, props, state, calls, events, app }
}

test('background tasks are collapsed read-only records with no worker selector and child approvals stay in the root window', async t => {
  const f = await setup(t)
  assert.equal(all(f.root).find(node => node.type === 'details').props.open, false)
  assert.equal(all(f.root).some(node => node.props['aria-label'] === '选择项目智能体' || text(node) === '回 Lead'), false)
  assert.equal(f.state.approvals.value.length, 1); assert.equal(f.state.questions.value.length, 1, 'root own waiters remain exclusively in the root session UI')
  const card = all(f.root).find(node => node.props['data-approval'] === 'root-approval')
  assert.ok(card); assert.match(text(card), /npm test/)
  assert.ok(all(f.root).some(node => node.props['data-approval-session'] === 'root-child'))
  await all(card).find(node => node.type === 'button').props.onClick(); await settle()
  assert.deepEqual(f.calls, [['decideApproval', 'root-approval', true]])
  assert.equal(f.props.selectedId, 'root'); assert.equal(f.state.approvals.value.length, 0)
  assert.deepEqual(f.events, ['refresh'])
})

test('child questions use exact child/group target without switching composer or granting permissions', async t => {
  const f = await setup(t)
  await all(f.root).find(node => node.props['data-question'] === 'child-question').props.onClick(); await settle()
  assert.deepEqual(f.calls, [['answerQuestion', 'root-child', 'child-question', { answers: [{ id: 'one', selected: ['单元测试'] }] }]])
  assert.equal(f.props.selectedId, 'root'); assert.equal(f.state.questions.value.length, 0)
  f.props.team.agents[0].pending_questions.push({ id: 'forged-target', status: 'pending', session_id: 'another-root-child', questions: [] })
  await settle(); assert.equal(f.state.questions.value.some(group => group.id === 'forged-target'), false)
})

test('failed approval remains actionable, is single-flight, and late root response cannot clear another root waiter', async t => {
  const result = deferred(), f = await setup(t, { decideApproval: () => result.promise })
  const agent = f.state.approvals.value[0], pending = f.state.decide(agent, false)
  assert.equal(await f.state.decide(agent, true), false); assert.equal(f.calls.length, 1)
  result.reject(Error('审批未送达')); assert.equal(await pending, false); await settle()
  assert.equal(f.state.approvals.value.length, 1); assert.match(text(f.root), /审批未送达/)
  const late = deferred(), g = await setup(t, { decideApproval: () => late.promise })
  const old = g.state.decide(g.state.approvals.value[0], true)
  g.props.selectedId = 'replacement'; g.props.team = roster('replacement'); await settle()
  late.resolve({ delivered: true }); await old; await settle()
  assert.equal(g.state.approvals.value[0].pending_approval.approval_id, 'replacement-approval')
  assert.deepEqual(g.events, [])
})

test('worker execution records load on demand and stale reads do not populate another root', async t => {
  const f = await setup(t)
  assert.deepEqual(f.calls, [])
  await f.state.readRecords(f.state.workers.value[0]); await settle()
  assert.deepEqual(f.calls, [['events', 'root-child']]); assert.match(text(f.root), /只读执行证据/)
  const result = deferred(), g = await setup(t, { events: () => result.promise })
  const pending = g.state.readRecords(g.state.workers.value[0])
  g.props.selectedId = 'new-root'; g.props.team = roster('new-root'); await settle()
  result.resolve([{ seq: 1, type: 'done', payload: { text: 'old evidence' } }]); await pending
  assert.deepEqual(g.state.records.value, {})
})
