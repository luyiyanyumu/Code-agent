import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { buildToolCards, summarizeToolArgs } from '../src/composables/useAgentSession.js'
import { eventTime, sessionPresentation, internalMessagePresentation, incompleteCompletion } from '../src/lib/sessionPresentation.js'
import { teamNoticePresentation } from '../src/lib/agentTeam.js'
import { PREFERENCE_DEFAULTS, shouldSendMessage } from '../src/lib/preferences.js'

const group = id => ({ id, session_id: 'worker', status: 'pending', questions: [{ id: 'one', question: `问题 ${id}`, options: [{ label: '继续' }] }] })
async function panel(t) {
  const props = reactive({ sessionId: 'worker', writable: true, pendingPrompt: '' }), calls = [], events = [], scope = effectScope()
  const session = { timeline: ref([]), questions: ref([]), busy: ref(true), phase: ref('tools'), streamState: ref('open'), historyLoading: ref(false), historyError: ref(''), banner: ref(''), streaming: ref(''), web: ref(false), usage: ref({}), questionSubmitting: ref(''), questionError: ref(''),
    load: async () => { session.questionError.value = '' }, send: async () => true, answerQuestions: async (...args) => calls.push(['answer', ...args]), dismissQuestions: async (...args) => calls.push(['dismiss', ...args]) }
  session.pendingQuestions = computed(() => session.questions.value.filter(value => value.status === 'pending'))
  const source = (await readFile(new URL('../src/components/code/AiPanel.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const ctx = vm.createContext({ computed, nextTick, ref, watch, onBeforeUnmount() {}, useAgentSession: () => session, buildToolCards, summarizeToolArgs, eventTime, sessionPresentation, internalMessagePresentation, incompleteCompletion, teamNoticePresentation,
    usePreferences: () => ({ preferences: PREFERENCE_DEFAULTS }), shouldSendMessage, defineProps: () => props, defineEmits: () => (...args) => events.push(args) })
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject={pendingQuestions,questionGroups,archivedQuestions,currentPhase,questionDraft,saveQuestionDraft,questionError,questionNotices,questionStateError,answerQuestion,dismissQuestion,refreshApprovals,messages,input,canSend}`, ctx))
  t.after(() => scope.stop())
  return { props, session, calls, events, s: ctx.subject }
}

test('coding panel shows both real pending question groups and removes duplicate ordinary ask tool cards', async t => {
  const { s, session } = await panel(t)
  session.questions.value = [group('first'), group('second')]
  session.timeline.value = [{ type: 'assistant_message', seq: 1, payload: { text: '需要你确认', tool_calls: [{ id: 'ask', name: 'ask_user_question' }, { id: 'read', name: 'read_file' }] } },
    { type: 'question_request', seq: 2, payload: { question_id: 'first' } }, { type: 'question_request', seq: 3, payload: { question_id: 'second' } }]
  assert.equal(s.pendingQuestions.value.length, 2)
  assert.equal(s.currentPhase.value, 'waiting_question')
  assert.deepEqual(Array.from(s.messages.value[0].calls), ['read'])
  assert.equal(s.messages.value.filter(value => value.kind === 'question').length, 0, 'pending cards are displayed in the dedicated action area')
  assert.equal(s.archivedQuestions.value.length, 0, 'live waiters never become archived history')
  session.questions.value[0].status = 'answered'
  assert.equal(s.pendingQuestions.value.length, 1)
  assert.equal(s.messages.value.find(value => value.kind === 'question').group.id, 'first')
  session.questions.value[1].status = 'timeout'
  assert.equal(s.currentPhase.value, 'tools')
  assert.equal(s.messages.value.filter(value => value.kind === 'question').length, 2)
  assert.equal(s.archivedQuestions.value.length, 0, 'completed questions with request events stay in their original conversation positions')
})

test('completed requests render once at their first timeline position and orphan history is deduplicated in the archive', async t => {
  const { s, session } = await panel(t)
  const inline = { ...group('inline'), status: 'answered' }, orphan = { ...group('orphan'), status: 'timeout' }
  session.questions.value = [inline, { ...inline }, orphan, { ...orphan }, group('live'), { ...orphan, id: '' }]
  session.timeline.value = [
    { type: 'user_message', seq: 1, payload: { text: '上一轮' } },
    { type: 'question_request', seq: 2, payload: { question_id: 'inline' } },
    { type: 'assistant_message', seq: 3, payload: { text: '继续执行' } },
    { type: 'question_request', seq: 4, payload: { question_id: 'inline' } },
    { type: 'done', seq: 5, payload: { text: '本轮完成' } },
  ]
  assert.deepEqual(Array.from(s.messages.value, value => [value.kind, value.key]), [['user', 'u1'], ['question', 'q2'], ['assistant', 'a3'], ['done', 'd5']])
  assert.deepEqual(Array.from(s.archivedQuestions.value, value => value.id), ['orphan'])
  assert.deepEqual(Array.from(s.pendingQuestions.value, value => value.id), ['live'])
})

test('new rounds leave three expired orphan questions in the archive and replayed request events restore only their original position', async t => {
  const { s, session } = await panel(t)
  session.questions.value = ['old-1', 'old-2', 'old-3'].map(id => ({ ...group(id), status: 'timeout' }))
  session.timeline.value = [{ type: 'user_message', seq: 50, payload: { text: '新的任务' } }, { type: 'done', seq: 51, payload: { text: '新的回答' } }]
  assert.equal(s.archivedQuestions.value.length, 3)
  assert.deepEqual(Array.from(s.messages.value, value => value.kind), ['user', 'done'])
  session.timeline.value.push({ type: 'user_message', seq: 52, payload: { text: '下一轮' } }, { type: 'done', seq: 53, payload: { text: '下一轮回答' } })
  assert.deepEqual(Array.from(s.messages.value, value => value.kind), ['user', 'done', 'user', 'done'], 'a new reply never receives old timeout cards at its tail')
  assert.equal(s.archivedQuestions.value.length, 3)
  session.timeline.value.unshift({ type: 'question_request', seq: 2, payload: { question_id: 'old-2' } })
  assert.equal(s.messages.value[0].group.id, 'old-2')
  assert.deepEqual(Array.from(s.archivedQuestions.value, value => value.id), ['old-1', 'old-3'], 'recovered timeline history is not duplicated in the archive')
})

test('coding question drafts survive executor switches without leaking a same-id draft to another session', async t => {
  const { s, props } = await panel(t), first = group('same-id')
  s.saveQuestionDraft(first, { index: 0, answers: { one: { custom: 'worker草稿' } } })
  props.sessionId = 'other'; await nextTick()
  assert.equal(s.questionDraft(first).answers, undefined)
  s.saveQuestionDraft(first, { index: 0, answers: { one: { custom: 'other草稿' } } })
  props.sessionId = 'worker'; await nextTick()
  assert.equal(s.questionDraft(first).answers.one.custom, 'worker草稿')
})

test('question actions send the selected group explicitly while pending groups block ordinary message submission', async t => {
  const { s, session, calls } = await panel(t), first = group('first'), second = group('second')
  session.questions.value = [first, second]; session.busy.value = false; s.input.value = '继续'
  assert.equal(s.canSend.value, false)
  const answers = [{ id: 'one', selected: ['继续'] }]
  await s.answerQuestion(second, answers); await s.dismissQuestion(first)
  assert.deepEqual(calls, [['answer', 'second', answers], ['dismiss', 'first']])
  assert.equal(s.questionDraft(second).answers, undefined, 'sending does not synthesize default drafts')
  session.questions.value = []
  assert.equal(s.canSend.value, true)
})

test('an expired answer failure remains visible after the pending card disappears without leaking to another group or session', async t => {
  const { s, session, props } = await panel(t), first = group('first'), second = group('second')
  session.questions.value = [first, second]
  s.saveQuestionDraft(first, { answers: { one: { custom: '保留草稿' } } })
  session.answerQuestions = async id => { session.questionError.value = '这次提问已结束'; session.questions.value = session.questions.value.map(value => value.id === id ? { ...value, status: 'inactive' } : value); return false }
  await s.answerQuestion(first, [{ id: 'one', selected: [], custom: '保留草稿' }])
  assert.equal(s.pendingQuestions.value.length, 1); assert.equal(s.questionNotices.value[0].group.id, 'first')
  assert.equal(s.questionError(second), ''); assert.equal(s.questionError(first), '这次提问已结束')
  assert.equal(s.messages.value.some(value => value.kind === 'question'), false, 'an evicted request is not appended to the current reply')
  assert.equal(s.archivedQuestions.value.find(value => value.id === 'first').status, 'inactive', 'its ended state and failure remain available in the archive')
  props.sessionId = 'other'; await nextTick()
  assert.equal(s.questionNotices.value.length, 0); assert.equal(s.questionStateError.value, '')
  props.sessionId = 'worker'; await nextTick()
  assert.equal(s.questionNotices.value[0].group.id, 'first'); assert.equal(s.questionDraft(first).answers.one.custom, '保留草稿')
})

test('a stale waiting-question roster cannot claim a timeout history is still awaiting an answer', async t => {
  const { s, session, props } = await panel(t)
  props.team = { agents: [{ session_id: 'worker', state: 'waiting', phase: 'waiting_question', pending_questions: [group('old')] }] }
  session.questions.value = [{ ...group('old'), status: 'timeout' }]
  session.phase.value = 'waiting_question'
  assert.equal(s.pendingQuestions.value.length, 0); assert.equal(s.currentPhase.value, 'tools')
})

test('approval retry refreshes the active session and team without deciding or selecting an executor', async t => {
  const { s, session, calls, events } = await panel(t)
  session.refreshApproval = async () => calls.push('refresh-approval')
  s.refreshApprovals()
  assert.deepEqual(calls, ['refresh-approval'])
  assert.deepEqual(events.map(([name]) => name), ['team-refresh'])
  const { descriptor, errors } = parse(await readFile(new URL('../src/components/code/AiPanel.vue', import.meta.url), 'utf8'))
  assert.deepEqual(errors, [])
  const all = node => [node, ...(node.children || []).flatMap(all)]
  const nodes = all(descriptor.template.ast)
  const retryNotice = nodes.find(node => node.props?.some(prop => prop.type === 7 && prop.name === 'if' && prop.exp?.content === 's.approvalError?.value'))
  assert.ok(retryNotice, 'failed recovery has a visible error notice rather than silently omitting a card')
  assert.equal(retryNotice.props.find(prop => prop.type === 6 && prop.name === 'role')?.value?.content, 'alert')
  const retry = all(retryNotice).find(node => node.tag === 'button')
  assert.equal(retry.props.find(prop => prop.type === 7 && prop.name === 'on' && prop.arg?.content === 'click')?.exp?.content, 'refreshApprovals')
  const team = nodes.find(node => node.tag === 'BackgroundAgentTasks')
  assert.equal(team.props.find(prop => prop.type === 7 && prop.name === 'on' && prop.arg?.content === 'refresh')?.exp?.content, 'refreshApprovals', 'the team check refreshes both the aggregate and active approval card')
})
