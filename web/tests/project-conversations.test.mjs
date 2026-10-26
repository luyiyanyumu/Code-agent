import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { useProjectConversations } from '../src/composables/useProjectConversations.js'
import { api } from '../src/api.js'
import * as agentSession from '../src/composables/useAgentSession.js'
import { PREFERENCE_DEFAULTS } from '../src/lib/preferences.js'

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = async () => { await Vue.nextTick(); await new Promise(setImmediate); await Vue.nextTick() }
const memory = () => { const values = new Map(); return { values, getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, String(value)) } }
const chat = (id, title = '新对话1', permission = 'read-only') => ({ id, kind: 'chat', title, permission_mode: permission, effective_permission: permission, busy: false, phase: '' })
const listing = (pid = 'a', extra = []) => ({ default_session_id: `${pid}-lead`, conversations: [{ id: `${pid}-lead`, kind: 'code', title: '原始Lead' }, ...extra] })

function setup(t, overrides = {}, storage = memory(), onDefaultChanged) {
  const props = Vue.reactive({ pid: 'a', tree: 'main', lead: 'a-lead', user: 'owner', visible: true }), calls = [], scope = Vue.effectScope()
  const client = { conversations: async pid => listing(pid), createConversation: async () => chat('a-chat'), ...overrides }
  const observed = Object.fromEntries(Object.keys(client).map(name => [name, (...args) => { calls.push([name, ...args]); return client[name](...args) }]))
  const state = scope.run(() => useProjectConversations({ projectId: () => props.pid, tree: () => props.tree, leadSessionId: () => props.lead, userId: () => props.user, visible: () => props.visible, storage, client: observed, onDefaultChanged }))
  t.after(() => scope.stop())
  return { state, props, calls, storage, scope }
}

test('independent conversations keep canonical Lead, use current root config, and fast create clicks are single-flight', async t => {
  const created = deferred(), f = setup(t, { conversations: async () => listing('a', [chat('a-old')]), createConversation: () => created.promise })
  await settle()
  assert.deepEqual(f.state.conversations.value.map(item => item.title), ['原始Lead', '新对话1'])
  assert.equal(f.state.select('a-worker'), false, 'workers never become root conversations')
  f.state.select('a-old')
  const pending = f.state.create()
  assert.equal(await f.state.create(), null)
  assert.equal(f.calls.filter(([name]) => name === 'createConversation').length, 1)
  assert.deepEqual(f.calls.at(-1), ['createConversation', 'a', { from_session_id: 'a-old' }])
  assert.equal(f.state.activeRootId.value, 'a-old', 'current window stays available while creation is pending')
  created.resolve(chat('a-new', '新对话2')); await pending
  assert.equal(f.state.activeRootId.value, 'a-new')
  assert.equal(f.state.canonicalId.value, 'a-lead')
  f.state.returnLead(); assert.equal(f.state.activeRootId.value, 'a-lead')
  assert.equal(f.props.lead, 'a-lead')
})

test('failed creation preserves selected history and user navigation wins over a pending creation', async t => {
  const created = deferred(), f = setup(t, { conversations: async () => listing('a', [chat('a-old')]), createConversation: () => created.promise })
  await settle(); f.state.select('a-old')
  const pending = f.state.create(); created.reject(new Error('服务暂不可用')); await pending
  assert.equal(f.state.activeRootId.value, 'a-old'); assert.equal(f.state.error.value, '服务暂不可用')
  const other = deferred()
  const g = setup(t, { conversations: async () => listing('a', [chat('a-old')]), createConversation: () => other.promise })
  await settle(); g.state.select('a-old')
  const next = g.state.create(); g.state.select('a-lead')
  other.resolve(chat('a-new')); await next
  assert.equal(g.state.activeRootId.value, 'a-lead')
  assert.ok(g.state.conversations.value.some(item => item.id === 'a-new'), 'created history remains reachable')
})

test('late list and create responses cannot replace another project or remove a newly created conversation', async t => {
  const oldList = deferred(), oldCreate = deferred()
  const f = setup(t, { conversations: pid => pid === 'a' ? oldList.promise : Promise.resolve(listing(pid)), createConversation: () => oldCreate.promise })
  const pending = f.state.create()
  f.props.pid = 'b'; f.props.lead = 'b-lead'; await settle()
  oldCreate.resolve(chat('a-old')); oldList.resolve(listing('a', [chat('a-stale')])); await pending; await settle()
  assert.equal(f.state.canonicalId.value, 'b-lead'); assert.equal(f.state.activeRootId.value, 'b-lead')
  assert.equal(f.state.conversations.value.some(item => item.id.startsWith('a-')), false)
  const stale = deferred(), g = setup(t, { conversations: () => stale.promise })
  await g.state.create(); stale.resolve(listing()); await settle()
  assert.equal(g.state.activeRootId.value, 'a-chat')
  assert.ok(g.state.conversations.value.some(item => item.id === 'a-chat'))
})

test('reload and project return restore validated selection while user and task trees remain isolated', async t => {
  const storage = memory(), client = { conversations: async pid => listing(pid, [chat(`${pid}-saved`)]) }
  const f = setup(t, client, storage); await settle(); f.state.select('a-saved')
  f.props.tree = 'task:12'; await settle()
  assert.equal(f.state.isChat.value, false); assert.equal(f.state.activeRootId.value, 'a-lead')
  assert.equal(await f.state.create(), null)
  assert.equal(f.state.select('a-saved'), false)
  f.props.tree = 'main'; await settle(); assert.equal(f.state.activeRootId.value, 'a-saved')
  f.props.pid = 'b'; f.props.lead = 'b-lead'; await settle(); assert.equal(f.state.activeRootId.value, 'b-lead')
  f.props.pid = 'a'; f.props.lead = 'a-lead'; await settle(); assert.equal(f.state.activeRootId.value, 'a-saved')
  const reloaded = setup(t, client, storage); await settle(); assert.equal(reloaded.state.activeRootId.value, 'a-saved')
  reloaded.props.user = 'another-owner'; await settle(); assert.equal(reloaded.state.activeRootId.value, 'a-lead')
})

test('missing restored session falls back to Lead and unavailable storage does not discard in-memory navigation', async t => {
  const storage = memory(); storage.setItem('code-agent-conversation-v1:owner:a', 'deleted-or-foreign')
  const f = setup(t, {}, storage); await settle(); assert.equal(f.state.activeRootId.value, 'a-lead')
  const g = setup(t, {}, { getItem() { throw Error('blocked') }, setItem() { throw Error('full') } }); await settle()
  await g.state.create(); assert.equal(g.state.activeRootId.value, 'a-chat'); assert.equal(g.state.error.value, '')
})

test('waiting for canonical identity fetches once identity arrives, without creating or mixing tree identities', async t => {
  const f = setup(t); await settle()
  f.props.pid = 'empty'; f.props.lead = ''; await settle()
  assert.equal(f.state.conversations.value.length, 0)
  const before = f.calls.length
  f.props.lead = 'empty-lead'; await settle()
  assert.equal(f.calls.length, before + 1)
  assert.equal(f.state.canonicalId.value, 'empty-lead')
  assert.equal(f.calls.some(([name]) => name === 'createConversation'), false)
})

test('deleting a nonselected chat preserves current window while deleting selected chat falls back and retains other history', async t => {
  const deleted = [], f = setup(t, {
    conversations: async () => listing('a', [chat('one'), chat('two')]),
    deleteConversation: async (pid, id) => { deleted.push([pid, id]); return { deleted: true, session_id: id, fallback_session_id: 'a-lead' } },
  })
  await settle(); f.state.select('two')
  assert.equal(await f.state.remove('one'), true)
  assert.equal(f.state.activeRootId.value, 'two')
  assert.equal(await f.state.remove('two'), true)
  assert.equal(f.state.activeRootId.value, 'a-lead')
  assert.deepEqual(deleted, [['a', 'one'], ['a', 'two']])
  assert.deepEqual(f.state.conversations.value.map(item => item.id), ['a-lead'])
})

test('deleting the default promotes an existing chat with its title, kind and current permissions intact', async t => {
  const promoted = chat('kept', '接口开发', 'workspace-write'), changes = []
  const f = setup(t, {
    conversations: async () => listing('a', [promoted]),
    deleteConversation: async (pid, id) => ({ deleted: true, session_id: id, fallback_session_id: promoted.id, default_session_id: promoted.id, default_conversation: promoted }),
  }, memory(), change => changes.push(change))
  await settle()
  assert.equal(await f.state.remove('a-lead'), true)
  assert.equal(f.state.activeRootId.value, promoted.id)
  assert.equal(f.state.canonicalId.value, promoted.id)
  assert.deepEqual(f.state.conversations.value.map(item => item.id), [promoted.id])
  assert.equal(f.state.selected.value.title, '接口开发')
  assert.equal(f.state.selected.value.kind, 'chat')
  assert.equal(f.state.selected.value.effective_permission, 'workspace-write')
  assert.equal(f.state.isChat.value, true, 'promoting a chat does not convert its execution/permission contract')
  assert.equal(changes.length, 1)
  assert.deepEqual(changes[0], { projectId: 'a', deletedSessionId: 'a-lead', defaultSessionId: promoted.id, defaultConversation: promoted })
  assert.equal(f.props.lead, 'a-lead', 'stale parent metadata cannot bring an archived Lead back')
  f.state.returnLead(); assert.equal(f.state.activeRootId.value, promoted.id)
  await f.state.refresh()
  assert.deepEqual(f.state.conversations.value.map(item => item.id), [promoted.id], 'a stale list cannot resurrect the archived default')
})

test('deleting the only conversation selects the returned empty main without copying the old Lead title', async t => {
  const replacement = { id: 'fresh', kind: 'main', title: '新对话', permission_mode: 'read-only', busy: false }, changes = []
  const f = setup(t, { deleteConversation: async (pid, id) => ({ deleted: true, session_id: id, fallback_session_id: replacement.id, default_session_id: replacement.id, default_conversation: replacement }) }, memory(), change => changes.push(change))
  await settle(); assert.equal(await f.state.remove('a-lead'), true)
  assert.equal(f.state.activeRootId.value, 'fresh')
  assert.equal(f.state.selected.value.title, '新对话')
  assert.equal(f.state.selected.value.kind, 'main')
  assert.equal(f.state.isChat.value, false)
  assert.deepEqual(f.state.conversations.value.map(item => item.id), ['fresh'])
  assert.equal(changes[0].defaultConversation, replacement)
})

test('deleting an inactive default and switching during deletion preserve the explicitly selected chat', async t => {
  for (const switchWhilePending of [false, true]) {
    const removed = deferred(), promoted = chat('one', '保留原标题'), other = chat('two', '当前窗口')
    const f = setup(t, { conversations: async () => listing('a', [promoted, other]), deleteConversation: () => removed.promise })
    await settle()
    if (!switchWhilePending) f.state.select('two')
    const pending = f.state.remove('a-lead')
    if (switchWhilePending) f.state.select('two')
    removed.resolve({ deleted: true, session_id: 'a-lead', fallback_session_id: 'one', default_session_id: 'one', default_conversation: promoted })
    assert.equal(await pending, true)
    assert.equal(f.state.activeRootId.value, 'two')
    assert.deepEqual(f.state.conversations.value.map(item => item.id), ['one', 'two'])
    assert.equal(f.state.conversations.value[0].title, '保留原标题')
  }
})

test('failed or invalid default deletion retains the root; a late default response cannot change another project', async t => {
  const failed = deferred(), changes = [], f = setup(t, { deleteConversation: () => failed.promise }, memory(), change => changes.push(change))
  await settle(); const pending = f.state.remove('a-lead')
  failed.reject(new Error('暂时无法归档')); assert.equal(await pending, false)
  assert.equal(f.state.activeRootId.value, 'a-lead')
  assert.deepEqual(changes, [])
  const invalid = setup(t, { deleteConversation: async () => ({ deleted: true, session_id: 'a-lead', fallback_session_id: 'fresh', default_session_id: 'fresh' }) })
  await settle(); assert.equal(await invalid.state.remove('a-lead'), false)
  assert.equal(invalid.state.activeRootId.value, 'a-lead')
  const late = deferred(), g = setup(t, { deleteConversation: () => late.promise }, memory(), change => changes.push(change))
  await settle(); const old = g.state.remove('a-lead')
  g.props.pid = 'b'; g.props.lead = 'b-lead'; await settle()
  const promoted = chat('a-promoted')
  late.resolve({ deleted: true, session_id: 'a-lead', fallback_session_id: promoted.id, default_session_id: promoted.id, default_conversation: promoted })
  assert.equal(await old, false)
  assert.equal(g.state.canonicalId.value, 'b-lead')
  assert.equal(g.state.activeRootId.value, 'b-lead')
  assert.deepEqual(changes, [])
})

test('failed and late deletion cannot discard current dialogue or affect another project, and user switches win', async t => {
  const pending = deferred(), f = setup(t, { conversations: async pid => listing(pid, [chat(`${pid}-chat`)]), deleteConversation: () => pending.promise })
  await settle(); f.state.select('a-chat')
  const removed = f.state.remove('a-chat'); assert.equal(await f.state.remove('a-chat'), false)
  pending.reject(new Error('归档失败')); assert.equal(await removed, false)
  assert.equal(f.state.activeRootId.value, 'a-chat'); assert.equal(f.state.error.value, '归档失败')
  const late = deferred(), g = setup(t, { conversations: async pid => listing(pid, [chat(`${pid}-chat`)]), deleteConversation: () => late.promise })
  await settle(); g.state.select('a-chat')
  const old = g.state.remove('a-chat')
  g.props.pid = 'b'; g.props.lead = 'b-lead'; await settle(); g.state.select('b-chat')
  late.resolve({ deleted: true, session_id: 'a-chat', fallback_session_id: 'a-lead' }); await old
  assert.equal(g.state.activeRootId.value, 'b-chat'); assert.equal(g.state.error.value, '')
})

const all = node => [node, ...node.children.flatMap(all)]
const nodeText = node => node.type === '#comment' ? '' : node.text + node.children.map(nodeText).join('')
const byClass = (root, name) => all(root).find(node => String(node.props.class || '').split(/\s+/).includes(name))
const byText = (root, text) => all(root).find(node => node.type === 'button' && nodeText(node) === text)

// Real compiled AiPanel + ConversationTabs + real websocket/session composable.
// Everything that could call HTTP or a model is a local fixture or a hard error.
async function renderPanel(t) {
  const savedApi = { ...api }, globals = Object.fromEntries(['WebSocket', 'location', 'localStorage', 'window', 'fetch'].map(key => [key, globalThis[key]]))
  const storage = memory(), histories = new Map(), sockets = [], sessions = [], warnings = [], unexpected = [], deleteRequests = [], promptDelivery = deferred()
  class Socket {
    static OPEN = 1
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this) }
    open() { this.readyState = 1; this.onopen?.() }
    emit(value) { this.onmessage?.({ data: JSON.stringify(value) }) }
    send(value) { this.sent.push(JSON.parse(value)) }
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }) }
  }
  const fail = name => async () => { unexpected.push(name); throw new Error(`Unexpected real operation ${name}`) }
  for (const [name, value] of Object.entries(api)) if (typeof value === 'function') api[name] = fail(name)
  Object.assign(api, { events: async id => histories.get(id) || [], pendingApproval: async () => null, pendingQuestions: async () => ({ questions: [] }), checkpoints: async () => ({ busy: false }), webCapability: async () => ({ available: false }), prompt: () => promptDelivery.promise })
  Object.assign(globalThis, { WebSocket: Socket, location: { protocol: 'http:', host: 'offline-conversations' }, localStorage: storage, window: { localStorage: storage }, fetch: fail('fetch') })
  const stub = Vue.defineComponent({ render: () => Vue.h('span') })
  const team = Vue.defineComponent({ render: () => Vue.h('div', { 'data-background-tasks': true }) })
  async function compile(file, full = false) {
    const url = new URL(`../src/components/code/${file}`, import.meta.url)
    const { descriptor, errors } = parse(await readFile(url, 'utf8')); assert.deepEqual(errors, [])
    let template = descriptor.template.content
    if (!full) {
      const find = node => node.props?.some(prop => prop.type === 6 && prop.name === 'class' && prop.value?.content === 'ai') ? node : (node.children || []).map(find).find(Boolean)
      assert.ok(find(descriptor.template.ast))
    }
    const compiled = compileScript({ ...descriptor, template: { ...descriptor.template, content: template, ast: undefined } }, { id: file, inlineTemplate: true })
    const bindings = {}, imports = []
    let source = compiled.content.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?\r?$/gm, (_, clause, specifier) => { imports.push({ clause: clause.trim(), specifier }); return '' })
    for (const { clause, specifier } of imports) {
      const namespace = specifier === 'vue' ? { ...Vue, vModelText: { created(el, binding, vnode) { el.value = binding.value; el.input = value => vnode.props['onUpdate:modelValue'](value) }, beforeUpdate(el, binding, vnode) { el.value = binding.value; el.input = value => vnode.props['onUpdate:modelValue'](value) } } }
        : specifier.endsWith('/useAgentSession.js') ? { ...agentSession, useAgentSession: () => { const session = agentSession.useAgentSession(); sessions.push(session); return session } }
          : specifier.endsWith('/preferences.js') ? { ...(await import(new URL(specifier, url))), usePreferences: () => ({ preferences: PREFERENCE_DEFAULTS }) }
            : specifier.endsWith('/ConversationTabs.vue') ? { default: await compile('ConversationTabs.vue', true) }
              : specifier.endsWith('.vue') ? { default: specifier.endsWith('/BackgroundAgentTasks.vue') ? team : stub }
                : await import(new URL(specifier, url))
      if (clause.startsWith('{')) for (const entry of clause.slice(1, -1).split(',').map(value => value.trim()).filter(Boolean)) {
        const [name, local = name] = entry.split(/\s+as\s+/); bindings[local] = namespace[name]
      }
      else bindings[clause] = namespace.default
    }
    return new Function(...Object.keys(bindings), source.replace('export default', 'return'))(...Object.values(bindings))
  }
  const Panel = await compile('AiPanel.vue')
  const node = (type, text = '') => Vue.markRaw({ type, text, props: {}, style: { display: '' }, children: [], parent: null, scrollTop: 0, scrollHeight: 400, clientHeight: 200 })
  const remove = child => { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null }
  const insert = (child, parent, anchor) => { remove(child); const i = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(i < 0 ? parent.children.length : i, 0, child); child.parent = parent }
  const renderer = Vue.createRenderer({ createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text), insert, remove,
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
    patchProp: (node, key, previous, value) => { node.props[key] = value }, setText: (node, text) => { node.text = text }, setElementText: (node, text) => { node.text = text; node.children = [] }, setScopeId() {},
    insertStaticContent(text, parent, anchor) { const child = node('#static', text); insert(child, parent, anchor); return [child, child] },
  })
  const props = Vue.reactive({ sessionId: 'lead', draftScope: 'owner:project-a', conversationsEnabled: true, conversations: [{ id: 'lead', kind: 'lead', title: 'Lead' }, chat('chat')], conversationId: 'lead', team: { lead: { session_id: 'lead' }, agents: [] }, projectChat: false })
  const root = node('root')
  const make = () => {
    const app = renderer.createApp({ render: () => Vue.h(Panel, { ...props, onConversationDelete: id => deleteRequests.push(id), onConversationSelect: id => { props.conversationId = id; props.sessionId = id; props.projectChat = id !== 'lead'; props.team = id === 'lead' ? { lead: { session_id: 'lead' }, agents: [] } : null } }) })
    app.config.warnHandler = message => warnings.push(message)
    app.mount(root); return app
  }
  let app = make()
  t.after(() => { app.unmount(); Object.assign(api, savedApi); Object.assign(globalThis, globals); assert.deepEqual(unexpected, []); assert.deepEqual(warnings, []) })
  await settle()
  return { root, props, storage, histories, sessions, sockets, deleteRequests, promptDelivery, session: () => sessions.at(-1), input: () => all(root).find(node => node.type === 'textarea'), remount: async () => { app.unmount(); app = make(); await settle() } }
}

test('real root dialogue tabs show one selection and keep independent drafts with no worker selector', async t => {
  const f = await renderPanel(t)
  assert.equal(all(f.root).some(node => node.props['aria-label'] === '选择项目智能体'), false)
  assert.ok(byText(f.root, '新建对话'))
  f.input().input('Lead未发出的草稿'); await settle()
  byText(f.root, '新对话1').props.onClick(); await settle()
  assert.equal(f.session().sessionId.value, 'chat')
  assert.equal(f.input().value, '')
  assert.ok(all(f.root).some(node => node.props['data-background-tasks']))
  assert.equal(all(f.root).filter(node => node.props['aria-label']?.startsWith('删除对话 ')).length, 2, 'default and independent chats both expose deletion')
  all(f.root).find(node => node.props['aria-label'] === '删除对话 Lead').props.onClick({ stopPropagation() {} })
  assert.deepEqual(f.deleteRequests, ['lead'], 'the default close button passes the real session identity through AiPanel')
  assert.equal(f.session().sessionId.value, 'chat', 'asking to delete the default does not select it')
  assert.equal(nodeText(byClass(f.root, 'title')), '项目对话')
  f.input().input('项目独立草稿'); await settle()
  byText(f.root, 'Lead').props.onClick(); await settle(); assert.equal(f.input().value, 'Lead未发出的草稿')
  byText(f.root, '新对话1').props.onClick(); await settle(); assert.equal(f.input().value, '项目独立草稿')
  assert.equal(all(f.root).filter(node => node.props.role === 'tab' && node.props['aria-selected']).length, 1)
  await f.remount(); assert.equal(f.input().value, '项目独立草稿')
  f.props.draftScope = 'another-owner:project-a'; await settle(); assert.equal(f.input().value, '')
})

test('switching real session subscriptions never sends cancel, ignores old frames, and recovers backend activity on return', async t => {
  const f = await renderPanel(t), old = f.sockets.at(-1)
  old.open(); old.emit({ seq: 0, type: 'session_meta', payload: { busy: true, phase: 'model_streaming' } }); await settle()
  assert.equal(f.session().busy.value, true)
  byText(f.root, '新对话1').props.onClick(); await settle()
  assert.equal(old.readyState, 3); assert.deepEqual(old.sent, [])
  old.emit({ seq: 9, type: 'done', payload: { text: '旧会话的迟到结果', stop_reason: 'end_turn' } }); await settle()
  assert.equal(f.session().timeline.value.some(event => event.seq === 9), false)
  byText(f.root, 'Lead').props.onClick(); await settle()
  f.sockets.at(-1).open(); f.sockets.at(-1).emit({ seq: 0, type: 'session_meta', payload: { busy: true, phase: 'tools' } }); await settle()
  assert.equal(f.session().busy.value, true); assert.equal(f.session().phase.value, 'tools')
  assert.equal(f.sockets.flatMap(socket => socket.sent).some(command => command.type === 'cancel'), false)
})

test('failed delivery after switching restores the original dialogue draft without overwriting the new one', async t => {
  const f = await renderPanel(t)
  f.input().input('需要恢复到Lead的消息'); await settle()
  const send = byText(f.root, '发送').props.onClick()
  byText(f.root, '新对话1').props.onClick(); await settle()
  f.input().input('新对话自己的输入'); await settle()
  f.promptDelivery.reject(new Error('离线失败')); await send; await settle()
  assert.equal(f.input().value, '新对话自己的输入')
  byText(f.root, 'Lead').props.onClick(); await settle()
  assert.equal(f.input().value, '需要恢复到Lead的消息')
  await f.remount(); assert.equal(f.input().value, '需要恢复到Lead的消息')
})
