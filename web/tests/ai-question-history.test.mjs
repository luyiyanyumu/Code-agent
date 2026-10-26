import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { api } from '../src/api.js'
import * as agentSession from '../src/composables/useAgentSession.js'
import { PREFERENCE_DEFAULTS } from '../src/lib/preferences.js'

const settle = async () => { await Vue.nextTick(); await new Promise(setImmediate); await Vue.nextTick() }
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const group = (id, sessionId = 'worker', status = 'timeout') => ({ id, question_id: id, session_id: sessionId, status, questions: [{ id: 'one', question: `问题 ${id}`, options: [{ label: '继续' }] }] })
const frame = (seq, type, payload = {}) => ({ seq, type, payload, ts: Date.now() / 1000 })
const history = sessionId => ['old-1', 'old-2', 'old-3'].map(id => group(id, sessionId))
const descendants = node => [node, ...node.children.flatMap(descendants)]
const hasClass = (node, name) => String(node.props.class || '').split(/\s+/).includes(name)
const findClass = (root, name) => descendants(root).find(node => hasClass(node, name))
const archivedHistory = root => descendants(root).find(node => node.type === 'details' && node.props['aria-label'] === '历史提问')
const cards = root => descendants(root).filter(node => node.props['data-question-id'])
const messages = root => descendants(root).filter(node => hasClass(node, 'msg'))
const nodeText = node => node.text + node.children.map(nodeText).join('')
function findAst(node, className) {
  if (node.props?.some(prop => prop.type === 6 && prop.name === 'class' && prop.value?.content.split(/\s+/).includes(className))) return node
  for (const child of node.children || []) { const found = findAst(child, className); if (found) return found }
}

// The real panel script, conversation template, and session state run together.
// All HTTP methods and websocket frames are local fixtures; no backend or model.
async function setup(t) {
  const originalApi = { ...api }, globals = Object.fromEntries(['WebSocket', 'location', 'localStorage', 'window', 'fetch'].map(key => [key, globalThis[key]]))
  const sockets = [], warnings = [], unexpected = []
  class Socket {
    static OPEN = 1
    constructor() { this.readyState = 0; sockets.push(this) }
    open() { this.readyState = 1; this.onopen?.() }
    emit(value) { this.onmessage?.({ data: JSON.stringify(value) }) }
    send() { assert.fail('history tests must not send websocket commands') }
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }) }
  }
  const fail = name => async () => { unexpected.push(name); throw new Error(`Unexpected live operation: ${name}`) }
  for (const [name, value] of Object.entries(api)) if (typeof value === 'function') api[name] = fail(name)
  Object.assign(api, { events: async () => [], pendingApproval: async () => null, pendingQuestions: async () => ({ questions: [] }), checkpoints: async () => ({ busy: false }), webCapability: async () => ({ available: false }), answerQuestion: async () => ({ delivered: true }) })
  Object.assign(globalThis, { WebSocket: Socket, location: { protocol: 'http:', host: 'offline-fixture' }, localStorage: { getItem: () => null, setItem() {} }, fetch: fail('fetch') })
  globalThis.window = { localStorage: globalThis.localStorage }
  let app, session
  t.after(() => {
    app?.unmount()
    Object.assign(api, originalApi); Object.assign(globalThis, globals)
    assert.deepEqual(unexpected, []); assert.deepEqual(warnings, [])
  })
  const url = new URL('../src/components/code/AiPanel.vue', import.meta.url)
  const { descriptor, errors } = parse(await readFile(url, 'utf8'))
  assert.deepEqual(errors, [])
  const conversation = findAst(descriptor.template.ast, 'conversation-body')
  assert.ok(conversation, 'the real conversation includes both historical and pending question areas')
  const compiled = compileScript({ ...descriptor, template: { ...descriptor.template, content: conversation.loc.source, ast: undefined } }, { id: 'ai-question-history', inlineTemplate: true })
  const Question = Vue.defineComponent({
    props: ['group', 'draft', 'error', 'submitting'], emits: ['update:draft', 'answer', 'dismiss'],
    setup: props => () => Vue.h('article', { 'data-question-id': props.group.id, 'data-session-id': props.group.session_id, 'data-status': props.group.status }, props.group.questions[0].question),
  })
  const bindings = {}, imports = []
  let source = compiled.content.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?\r?$/gm, (_, clause, specifier) => { imports.push({ clause: clause.trim(), specifier }); return '' })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? Vue
      : specifier.endsWith('/useAgentSession.js') ? { ...agentSession, useAgentSession: () => (session = agentSession.useAgentSession()) }
        : specifier.endsWith('/preferences.js') ? { ...(await import(new URL(specifier, url))), usePreferences: () => ({ preferences: PREFERENCE_DEFAULTS }) }
          : specifier.endsWith('.vue') ? { default: specifier.endsWith('/QuestionRequest.vue') ? Question : Vue.defineComponent({ render: () => Vue.h('span') }) }
            : await import(new URL(specifier, url))
    if (clause.startsWith('{')) for (const entry of clause.slice(1, -1).split(',').map(value => value.trim()).filter(Boolean)) {
      const [name, local = name] = entry.split(/\s+as\s+/); bindings[local] = namespace[name]
    }
    else bindings[clause] = namespace.default
  }
  source = source.replace('export default', 'return')
  const Subject = new Function(...Object.keys(bindings), source)(...Object.values(bindings))
  const node = (type, text = '') => Vue.markRaw({ type, text, props: {}, children: [], parent: null })
  const remove = child => { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null }
  const insert = (child, parent, anchor) => { remove(child); const index = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent }
  const renderer = Vue.createRenderer({
    createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text), insert, remove,
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp: (node, key, previous, value) => { node.props[key] = value }, setText: (node, text) => { node.text = text },
    setElementText: (node, text) => { node.text = text; node.children = [] }, setScopeId() {},
    insertStaticContent(text, parent, anchor) { const child = node('#static', text); insert(child, parent, anchor); return [child, child] },
  })
  const props = Vue.reactive({ sessionId: 'worker', writable: true }), root = node('root')
  app = renderer.createApp({ render: () => Vue.h(Subject, props) })
  app.config.warnHandler = message => warnings.push(message)
  app.mount(root)
  return { root, props, session, socket: () => sockets.at(-1) }
}

test('three orphan timeouts stay in a default-collapsed archive above the conversation across refresh and new rounds', async t => {
  const { root, session, socket } = await setup(t)
  api.events = async () => [frame(1, 'user_message', { text: '本次任务' }), frame(2, 'done', { text: '本次完成', stop_reason: 'end_turn' })]
  api.pendingQuestions = async () => ({ questions: history('worker') })
  await session.load('worker'); await settle(); socket().open()
  const list = findClass(root, 'list'), archive = archivedHistory(root)
  assert.equal(archive.type, 'details'); assert.equal(archive.props.open, undefined, 'the native details element defaults to collapsed')
  assert.equal(archive.props['aria-label'], '历史提问')
  assert.match(nodeText(archive.children.find(node => node.type === 'summary')), /历史提问.*3/)
  assert.deepEqual(cards(archive).map(node => node.props['data-status']), ['timeout', 'timeout', 'timeout'])
  assert.ok(list.children.indexOf(archive) < list.children.indexOf(messages(root)[0]), 'history appears before the first conversation message')
  assert.deepEqual(messages(root).map(node => node.props.class), ['msg user', 'msg done'])
  archive.props.open = true // Native user expansion should survive an ordinary refresh.
  await session.refreshQuestions(); await settle()
  socket().emit(frame(3, 'user_message', { text: '下一轮' })); socket().emit(frame(4, 'done', { text: '下一轮完成', stop_reason: 'end_turn' }))
  await settle()
  assert.equal(archivedHistory(root), archive); assert.equal(archive.props.open, true)
  assert.equal(cards(root).length, 3, 'refresh never stacks another copy of the same three cards')
  assert.equal(cards(findClass(root, 'list')).every(card => descendants(archive).includes(card)), true, 'all old cards remain inside the archive')
  assert.deepEqual(messages(root).map(node => node.props.class), ['msg user', 'msg done', 'msg user', 'msg done'])
})

test('attributed internal controls render as collapsed process records while identical human text stays a user bubble', async t => {
  const { root, session } = await setup(t)
  const text = '[收口自查] 证据够了吗？\n完整内部说明'
  const records = [
    frame(1, 'user_message', { text }),
    frame(2, 'user_message', { text, auto: true, quiet: true, closures: 1 }),
    frame(3, 'user_message', { text, source: { kind: 'runtime-continuation', form: 'notice', summary: '继续执行未完成的任务' } }),
    frame(4, 'user_message', { text, source: { kind: 'unknown-user-source', form: 'notice' } }),
  ]
  api.events = async () => records
  await session.load('worker'); await settle()
  const controls = descendants(root).filter(node => node.type === 'details' && node.props['aria-label'] === '系统过程')
  assert.equal(controls.length, 2)
  assert.equal(controls.every(node => node.props.open === undefined), true, 'native process details default to collapsed')
  for (const control of controls) {
    assert.equal(control.parent.props.class, 'msg internal')
    assert.equal(nodeText(control.children.find(node => node.type === 'pre')), text, 'expanding still reveals the complete recorded control text')
  }
  const human = messages(root).filter(node => hasClass(node, 'user'))
  assert.equal(human.length, 2)
  assert.equal(human.every(node => nodeText(node) === text), true)
  assert.equal(session.events.value.length, 4)
  await session.load('worker'); await settle()
  assert.equal(descendants(root).filter(node => node.type === 'details' && node.props['aria-label'] === '系统过程').length, 2, 'refresh does not duplicate or revert process records into human bubbles')
})

test('truncated, filtered and unknown completions keep partial text and display truthful unfinished status', async t => {
  const { root, session } = await setup(t)
  for (const [stop, label, text] of [['max_tokens', '输出达到上限，任务未完成', '保留这段未完成输出'], ['content_filter', '输出被内容过滤，任务未完成', ''], ['future_provider_stop', '任务未完成 · 停止原因：future_provider_stop', '其他终止的部分输出']]) {
    api.events = async () => [frame(1, 'user_message', { text: '任务' }), frame(2, 'done', { text, stop_reason: stop })]
    await session.load('worker'); await settle()
    const done = messages(root).find(node => hasClass(node, 'done'))
    const warning = descendants(done).find(node => node.props.role === 'status')
    assert.equal(nodeText(warning), label)
    assert.equal(warning.props.title, stop)
    if (text) assert.ok(descendants(done).some(node => String(node.props.innerHTML || '').includes(text)), 'partial model output remains visible')
    assert.equal(session.events.value.at(-1).payload.stop_reason, stop)
  }
})

test('historical and live quiet repetition advisories stay folded process records without red error bubbles', async t => {
  const { root, session, socket } = await setup(t)
  const text = '重复操作，请复用已有结果'
  api.events = async () => [frame(1, 'user_message', { text: '继续检查' }), frame(2, 'error', { code: 'repeat_tool_reminder', quiet: true, message: text })]
  await session.load('worker'); await settle(); socket().open()
  assert.equal(messages(root).filter(node => hasClass(node, 'error')).length, 0)
  socket().emit(frame(3, 'user_message', { text: '执行任务' }))
  socket().emit(frame(4, 'tool_use', { call: { id: 'read', name: 'read_file', arguments: { path: 'a.py' } } }))
  socket().emit(frame(5, 'error', { code: 'repeat_tool_reminder', quiet: true, message: text }))
  await settle()
  assert.equal(session.busy.value, true, 'an advisory does not terminate the active execution')
  assert.equal(session.phase.value, 'tools')
  assert.equal(session.banner.value, '')
  assert.equal(session.timeline.value.filter(event => event.type === 'error' && event.payload.quiet).length, 2, 'quiet return occurs after the original records are retained')
  const notices = descendants(root).filter(node => node.type === 'details' && node.props['aria-label'] === '系统过程')
  assert.equal(notices.length, 2)
  assert.equal(notices.every(node => node.props.open === undefined), true)
  assert.equal(notices.every(node => nodeText(node.children.find(child => child.type === 'pre')) === text), true)
  assert.equal(messages(root).filter(node => hasClass(node, 'error')).length, 0)
  socket().emit(frame(6, 'error', { code: 'provider_error', message: text }))
  await settle()
  assert.equal(messages(root).filter(node => hasClass(node, 'error')).length, 1, 'an actual error with the same text is still visibly an error')
  assert.equal(session.banner.value, text)
  assert.equal(session.timeline.value.length, 6)
})

test('real pending questions use the action area, then completed questions occupy either their first request position or the archive', async t => {
  const { root, session } = await setup(t)
  api.events = async () => [frame(1, 'user_message', { text: '请处理' }), frame(2, 'question_request', group('inline', 'worker', 'pending')), frame(3, 'assistant_message', { text: '等待确认' }), frame(4, 'question_request', group('inline', 'worker', 'pending')), frame(5, 'done', { text: '处理中' })]
  api.pendingQuestions = async () => ({ questions: [group('inline', 'worker', 'pending'), group('orphan', 'worker', 'pending')] })
  await session.load('worker'); await settle()
  assert.equal(archivedHistory(root), undefined)
  assert.deepEqual(cards(findClass(root, 'question-requests')).map(node => node.props['data-question-id']), ['inline', 'orphan'])
  assert.equal(cards(findClass(root, 'list')).length, 0, 'pending requests are never duplicated in the transcript or archive')
  await session.answerQuestions('inline', [{ id: 'one', selected: ['继续'] }]); await settle()
  assert.deepEqual(messages(root).map(node => node.props.class), ['msg user', 'msg question', 'msg assistant', 'msg done'])
  assert.equal(cards(findClass(root, 'list'))[0].props['data-question-id'], 'inline')
  assert.deepEqual(cards(findClass(root, 'question-requests')).map(node => node.props['data-question-id']), ['orphan'])
  await session.dismissQuestions('orphan'); await settle()
  assert.equal(findClass(root, 'question-requests'), undefined)
  assert.deepEqual(cards(archivedHistory(root)).map(node => [node.props['data-question-id'], node.props['data-status']]), [['orphan', 'dismissed']])
  assert.equal(cards(root).filter(node => node.props['data-question-id'] === 'inline').length, 1, 'duplicate request events retain only the first inline card')
})

test('session switches reset archive expansion and late snapshots or old stream frames cannot restore old cards', async t => {
  const { root, props, session, socket } = await setup(t)
  api.events = async id => [frame(1, 'user_message', { text: `${id}任务` }), frame(2, 'done', { text: `${id}完成` })]
  api.pendingQuestions = async () => ({ questions: history('worker') })
  await session.load('worker'); await settle(); socket().open()
  const previousArchive = archivedHistory(root), previousFrame = socket().onmessage, late = deferred()
  previousArchive.props.open = true
  api.pendingQuestions = id => id === 'worker' ? late.promise : Promise.resolve({ questions: [group('old-1', 'other', 'answered')] })
  const refreshing = session.refreshQuestions()
  props.sessionId = 'other'; await settle()
  const currentArchive = archivedHistory(root)
  assert.equal(session.sessionId.value, 'other'); assert.notEqual(currentArchive, previousArchive)
  assert.equal(currentArchive.props.open, undefined, 'switching executors starts with its own collapsed history')
  late.resolve({ questions: history('worker') }); assert.equal(await refreshing, false)
  previousFrame({ data: JSON.stringify(frame(50, 'session_meta', { busy: true, pending_questions: [group('late-worker', 'worker', 'pending')] })) })
  await settle()
  assert.deepEqual(cards(root).map(node => [node.props['data-question-id'], node.props['data-session-id']]), [['old-1', 'other']])
  assert.equal(findClass(root, 'question-requests'), undefined)
  const staleCurrent = deferred()
  api.pendingQuestions = () => staleCurrent.promise
  const olderLookup = session.refreshQuestions()
  socket().emit(frame(3, 'question_request', group('new-round', 'other', 'pending')))
  staleCurrent.resolve({ questions: [] }); assert.equal(await olderLookup, false); await settle()
  assert.deepEqual(cards(findClass(root, 'question-requests')).map(node => node.props['data-question-id']), ['new-round'], 'a snapshot begun before the new request cannot erase its live action')
  assert.equal(cards(archivedHistory(root)).length, 1)
  assert.equal(messages(root).some(node => hasClass(node, 'question')), false, 'no old timeout card is attached to the new conversation tail')
})
