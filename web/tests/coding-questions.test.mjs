import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope } from 'vue'
import { api } from '../src/api.js'
import { useAgentSession } from '../src/composables/useAgentSession.js'
import { normalizeQuestionGroup, reconcileQuestionSnapshot, trimQuestionHistory } from '../src/lib/questions.js'

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const group = (id, extra = {}) => ({ question_id: id, questions: [{ id: 'choice', question: `请选择 ${id}`, options: [{ label: '继续（推荐）' }, { label: '暂停' }] }], ...extra })
const frame = (seq, type, payload = {}) => ({ seq, type, payload, ts: Date.now() / 1000 })
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }

class Socket {
  static OPEN = 1
  static sockets = []
  constructor() { this.readyState = 0; this.sent = []; Socket.sockets.push(this) }
  open() { this.readyState = 1; this.onopen?.() }
  emit(value) { this.onmessage?.({ data: JSON.stringify(value) }) }
  send(value) { this.sent.push(JSON.parse(value)) }
  close() { this.readyState = 3; this.onclose?.({ code: 1000 }) }
}
function setup(t) {
  const originalApi = { ...api }, globals = Object.fromEntries(['WebSocket', 'location', 'localStorage', 'window'].map(key => [key, globalThis[key]]))
  globalThis.WebSocket = Socket
  globalThis.location = { protocol: 'http:', host: 'offline-fixture' }
  globalThis.localStorage = { getItem: () => null, setItem() {} }
  globalThis.window = { localStorage: globalThis.localStorage }
  Socket.sockets = []
  Object.assign(api, { events: async () => [], pendingApproval: async () => null, pendingQuestions: async () => ({ questions: [] }),
    checkpoints: async () => ({ busy: false }), webCapability: async () => ({ available: false }) })
  const calls = []
  api.answerQuestion = async (...args) => { calls.push(args); return { delivered: true } }
  const scope = effectScope(), s = scope.run(useAgentSession)
  t.after(() => { scope.stop(); Object.assign(api, originalApi); Object.assign(globalThis, globals) })
  return { s, calls, socket: () => Socket.sockets.at(-1) }
}

test('coding: two simultaneous question groups never submit defaults and answer the exact executor/group', async t => {
  const { s, socket, calls } = setup(t)
  await s.load('worker/2'); socket().open()
  socket().emit(frame(1, 'question_request', group('first')))
  socket().emit(frame(2, 'question_request', group('second')))
  assert.equal(s.pendingQuestions.value.length, 2)
  assert.equal(s.phase.value, 'waiting_question')
  assert.equal(await s.send('继续'), false)
  assert.equal(calls.length, 0)
  const answers = [{ id: 'choice', selected: ['暂停'], custom: '' }]
  assert.equal(await s.answerQuestions('second', answers), true)
  assert.deepEqual(calls, [['worker/2', 'second', { answers }]])
  assert.deepEqual(s.pendingQuestions.value.map(q => q.id), ['first'])
  assert.equal(s.questions.value.find(q => q.id === 'second').status, 'answered')
  assert.equal(await s.dismissQuestions('first'), true)
  assert.deepEqual(calls[1], ['worker/2', 'first', { dismiss: true }])
  assert.equal(s.questions.value[0].status, 'dismissed')
})

test('coding: pending groups survive transcript truncation and are restored without a request in the history page', async t => {
  const { s, socket } = setup(t)
  api.pendingQuestions = async () => ({ questions: [group('live')] })
  await s.load('worker')
  assert.equal(s.pendingQuestions.value[0].id, 'live')
  assert.equal(s.timeline.value.length, 0)
  socket().open()
  for (let seq = 1; seq <= 410; seq++) socket().emit(frame(seq, 'tool_use', { call: { id: `read-${seq}`, name: 'read_file' } }))
  assert.equal(s.timeline.value.length, 400)
  assert.equal(s.pendingQuestions.value[0].id, 'live')
})

test('coding: historical requests with no live waiter become inactive rather than accepting an answer after restart', async t => {
  const { s, calls } = setup(t)
  api.events = async () => [frame(1, 'question_request', group('old'))]
  await s.load('worker')
  assert.equal(s.questions.value[0].status, 'inactive')
  assert.equal(s.pendingQuestions.value.length, 0)
  assert.equal(await s.answerQuestions('old', []), false)
  assert.equal(calls.length, 0)
})

test('coding: live websocket snapshot reconciles replay and clears requests that ended while disconnected', async t => {
  const { s, socket } = setup(t)
  api.pendingQuestions = async () => ({ questions: [group('live')] })
  await s.load('worker'); socket().open()
  socket().emit(frame(0, 'session_meta', { busy: true, phase: 'waiting_question', pending_questions: [group('live'), group('new')] }))
  assert.deepEqual(s.pendingQuestions.value.map(q => q.id), ['live', 'new'])
  api.pendingQuestions = async () => ({ questions: [] })
  socket().emit(frame(0, 'session_meta', { busy: false, phase: '', pending_questions: [] }))
  assert.equal(s.pendingQuestions.value.length, 0)
  assert.equal(s.busy.value, false)
})

test('coding: stale pending lookup cannot erase a question arriving after the lookup began', async t => {
  const { s, socket } = setup(t), lookup = deferred()
  await s.load('worker'); socket().open()
  api.pendingQuestions = () => lookup.promise
  const restoring = s.refreshQuestions()
  socket().emit(frame(1, 'question_request', group('new')))
  lookup.resolve({ questions: [] }); await restoring
  assert.deepEqual(s.pendingQuestions.value.map(q => q.id), ['new'])
})

test('coding: submission failure preserves the pending request and blocks duplicate submissions until response', async t => {
  const { s, socket } = setup(t), answer = deferred()
  await s.load('worker'); socket().open(); socket().emit(frame(1, 'question_request', group('q')))
  api.answerQuestion = () => answer.promise
  const sending = s.answerQuestions('q', [{ id: 'choice', custom: '我的答案' }])
  assert.equal(s.questionSubmitting.value, 'q')
  assert.equal(await s.dismissQuestions('q'), false)
  answer.reject(new Error('网络断开')); assert.equal(await sending, false)
  assert.equal(s.pendingQuestions.value[0].id, 'q')
  assert.equal(s.questionSubmitting.value, '')
  assert.equal(s.questionError.value, '网络断开')
})

test('coding: old submission and lookup results cannot mutate a newly selected session with the same question id', async t => {
  const { s, socket } = setup(t), answer = deferred()
  await s.load('first'); socket().open(); socket().emit(frame(1, 'question_request', group('same')))
  api.answerQuestion = () => answer.promise
  const sending = s.answerQuestions('same', [{ id: 'choice', selected: ['暂停'] }])
  api.pendingQuestions = async () => ({ questions: [group('same', { session_id: 'second' })] })
  await s.load('second'); answer.reject(new Error('旧会话失败')); await sending
  assert.equal(s.sessionId.value, 'second')
  assert.equal(s.questionError.value, '')
  assert.equal(s.questionSubmitting.value, '')
  assert.equal(s.pendingQuestions.value[0].session_id, 'second')
})

test('coding: a 409 ends only the rejected group and never retries or submits another live group', async t => {
  const { s, socket, calls } = setup(t)
  await s.load('worker'); socket().open()
  socket().emit(frame(1, 'question_request', group('old'))); socket().emit(frame(2, 'question_request', group('new')))
  api.answerQuestion = async (...args) => { calls.push(args); throw Object.assign(new Error('问题已经结束'), { status: 409 }) }
  api.pendingQuestions = async () => ({ questions: [group('new')] })
  assert.equal(await s.answerQuestions('old', []), false)
  assert.equal(calls.length, 1)
  assert.equal(s.questions.value[0].status, 'inactive')
  assert.deepEqual(s.pendingQuestions.value.map(q => q.id), ['new'])
})

test('coding: another delivered answer is preserved when an in-flight duplicate receives 409', async t => {
  const { s, socket } = setup(t), answer = deferred()
  await s.load('worker'); socket().open(); socket().emit(frame(1, 'question_request', group('q')))
  api.answerQuestion = () => answer.promise
  const sending = s.answerQuestions('q', [])
  const answers = [{ id: 'choice', custom: '另一个页面的答案' }]
  socket().emit(frame(2, 'question_answered', { question_id: 'q', status: 'answered', answers }))
  answer.reject(Object.assign(new Error('已回答'), { status: 409 })); await sending
  assert.equal(s.questions.value[0].status, 'answered')
  assert.deepEqual(s.questions.value[0].answers, answers)
})

test('coding: expiry and explicit stop retain truthful history and disable stale actions', async t => {
  const { s, socket } = setup(t)
  await s.load('worker'); socket().open()
  socket().emit(frame(1, 'question_request', group('expired', { expires_at: new Date(Date.now() - 100).toISOString() })))
  assert.equal(s.questions.value[0].status, 'timeout')
  assert.equal(s.pendingQuestions.value.length, 0)
  assert.notEqual(s.phase.value, 'waiting_question')
  socket().emit(frame(2, 'question_request', group('active')))
  assert.equal(s.stop(), true)
  assert.equal(s.questions.value[1].status, 'cancelled')
  assert.deepEqual(socket().sent, [{ type: 'cancel' }])
  await settle()
})

test('coding: normalization preserves original option labels and live snapshots cannot revive answered or explicitly unavailable groups', () => {
  assert.equal(normalizeQuestionGroup(group('q', { session_id: 'other' }), 'worker'), null)
  const original = normalizeQuestionGroup(group('q'), 'worker')
  assert.equal(original.questions[0].options[0].label, '继续（推荐）')
  assert.equal(reconcileQuestionSnapshot([{ ...original, status: 'answered' }], [group('q')], 'worker')[0].status, 'answered')
  assert.equal(reconcileQuestionSnapshot([{ ...original, status: 'inactive', unavailable: true }], [group('q')], 'worker')[0].status, 'inactive')
  assert.equal(reconcileQuestionSnapshot([{ ...original, status: 'inactive' }], [group('q')], 'worker')[0].status, 'pending')
  const history = Array.from({ length: 60 }, (_, i) => ({ ...original, id: `old-${i}`, status: 'answered' }))
  assert.equal(trimQuestionHistory([original, ...history]).length, 51)
})
