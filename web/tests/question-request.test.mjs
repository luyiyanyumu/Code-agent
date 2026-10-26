import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { pendingTeamQuestions } from '../src/lib/agentTeam.js'
import { hasWorkQuestion, workItemGroup, workItemQuestions, workItemSession, workItemState, workItemTree } from '../src/lib/workItems.js'

const group = () => ({ id: 'group-1', session_id: 'worker', status: 'pending', questions: [
  { id: 'one', header: '处理方式', question: '如何处理？', options: [{ label: '保留 (Recommended)', description: '保留已有行为' }, { label: '调整' }] },
  { id: 'two', question: '验证哪些内容？', multi_select: true, options: [{ label: '前端' }, { label: '后端' }] },
] })
async function card(t, value = group()) {
  const props = reactive({ group: value, draft: {}, error: '', submitting: false }), events = [], scope = effectScope()
  const source = (await readFile(new URL('../src/components/QuestionRequest.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const ctx = vm.createContext({ computed, ref, watch, defineProps: () => props, defineEmits: () => (name, payload) => {
    events.push([name, payload]); if (name === 'update:draft') props.draft = payload
  } })
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject={pick,skip,submit,advance,go,patchDraft,setCustom,current,index,pending,localError,confirmDismiss,recommended,historyAnswer,unsentDraft,endExplanation}`, ctx))
  t.after(() => scope.stop())
  return { props, events, s: ctx.subject }
}
const plain = value => JSON.parse(JSON.stringify(value))

test('question card starts unanswered and never accepts the recommended option automatically', async t => {
  const { props, s, events } = await card(t)
  assert.deepEqual(plain(props.draft), {})
  assert.deepEqual(plain(s.recommended('保留 (Recommended)')), { label: '保留', recommended: true })
  s.submit()
  assert.match(s.localError.value, /明确跳过/)
  assert.equal(events.some(([name]) => name === 'answer'), false)
})

test('single and multiple choices submit exact original labels and explicit custom answers', async t => {
  const { s, props, events } = await card(t)
  s.pick(props.group.questions[0].options[0]); s.advance()
  assert.equal(s.index.value, 1)
  s.pick(props.group.questions[1].options[0]); s.pick(props.group.questions[1].options[1])
  s.patchDraft('two', { custom: '兼容旧接口', skipped: false }); s.submit()
  assert.deepEqual(plain(events.find(([name]) => name === 'answer')[1]), [
    { id: 'one', selected: ['保留 (Recommended)'] }, { id: 'two', selected: ['前端', '后端'], custom: '兼容旧接口' },
  ])
})

test('a single custom answer replaces the selected option rather than requesting both', async t => {
  const { s, props, events } = await card(t, { ...group(), questions: [group().questions[0]] })
  s.pick(props.group.questions[0].options[0]); s.patchDraft('one', { custom: '先保存草稿' }); s.submit()
  assert.deepEqual(plain(events.find(([name]) => name === 'answer')[1]), [{ id: 'one', selected: [], custom: '先保存草稿' }])
})

test('custom input clears the visible single selection and preserves multi selections', async t => {
  const { s, props } = await card(t)
  s.pick(props.group.questions[0].options[0]); s.setCustom('单选自定义')
  assert.deepEqual(plain(props.draft.answers.one.selected), [])
  s.go(1); s.pick(props.group.questions[1].options[0]); s.setCustom('多选补充')
  assert.deepEqual(plain(props.draft.answers.two.selected), ['前端'])
})

test('skipping preserves an explicit empty answer when it advances to the next question', async t => {
  const { s, props, events } = await card(t)
  s.skip()
  assert.equal(s.index.value, 1); assert.equal(props.draft.answers.one.skipped, true)
  assert.equal(events.filter(([name]) => name === 'update:draft').length, 1, 'one update cannot overwrite the skipped answer')
  s.pick(props.group.questions[1].options[0]); s.submit()
  assert.deepEqual(plain(events.find(([name]) => name === 'answer')[1]), [{ id: 'one', selected: [] }, { id: 'two', selected: ['前端'] }])
})

test('unanswered earlier questions remain actionable when submitting from the final page', async t => {
  const { s, props, events } = await card(t)
  s.go(1); s.pick(props.group.questions[1].options[0]); s.submit()
  assert.equal(s.index.value, 0)
  assert.equal(events.some(([name]) => name === 'answer'), false)
  assert.equal(props.draft.answers.two.selected[0], '前端')
})

test('request errors preserve drafts and ended question groups cannot send an answer', async t => {
  const { s, props, events } = await card(t, { ...group(), questions: [group().questions[0]] })
  s.patchDraft('one', { custom: '保留这个答案' }); props.error = '网络中断'
  assert.equal(props.draft.answers.one.custom, '保留这个答案')
  props.submitting = true; s.submit(); assert.equal(events.some(([name]) => name === 'answer'), false)
  props.submitting = false; props.group.status = 'inactive'; await nextTick()
  s.submit(); s.pick(props.group.questions[0].options[0]); s.skip()
  assert.equal(events.some(([name]) => name === 'answer'), false)
  assert.equal(props.draft.answers.one.custom, '保留这个答案')
})

test('historical answers distinguish a skipped question from an unanswered terminal group', async t => {
  const { s, props } = await card(t)
  props.group.status = 'answered'; props.group.answers = [{ id: 'one', selected: [], custom: '' }]
  assert.equal(s.historyAnswer(props.group.questions[0]), '已跳过')
  assert.equal(s.historyAnswer(props.group.questions[1]), '')
})

test('expired and inactive histories retain an unsent draft and explain why submission ended', async t => {
  const { s, props } = await card(t)
  s.setCustom('保留待提交的答案')
  props.group.status = 'timeout'
  assert.match(s.endExplanation.value, /时限已结束/); assert.equal(s.unsentDraft(props.group.questions[0]), '保留待提交的答案')
  props.group.status = 'inactive'
  assert.match(s.endExplanation.value, /回答没有送达/)
  props.group.status = 'answered'
  assert.equal(s.unsentDraft(props.group.questions[0]), '')
})

test('team pending question navigation keeps every group and routes to the actual executor', () => {
  const team = { lead: { session_id: 'lead', role: 'lead' }, agents: [{ session_id: 'worker', title: '后端', role: 'subagent', pending_questions: [group(), { ...group(), id: 'group-2' }, { ...group(), id: 'old', status: 'answered' }] }] }
  const pending = pendingTeamQuestions(team)
  assert.equal(pending.length, 2); assert.equal(pending[0].session_id, 'worker'); assert.equal(pending[1].id, 'group-2')
  assert.equal(pending[0].executor_title, '后端')
})

test('work item questions require attention and select a recursive lane executor and its worktree', () => {
  const pending = { ...group(), session_id: 'grandchild' }
  const item = { kind: 'batch', tree_id: 'main', state: 'running', pending_questions: [pending], children: [{ kind: 'task', task_id: 'lane-one', tree_id: 'task:lane-one', session_id: 'parent', agents: [{ session_id: 'grandchild', pending_questions: [pending] }] }] }
  assert.equal(workItemQuestions(item).length, 1)
  assert.equal(hasWorkQuestion(item), true); assert.equal(workItemGroup(item), 'attention'); assert.equal(workItemState(item), '等待你回答')
  assert.equal(workItemSession(item), 'grandchild'); assert.equal(workItemTree(item), 'task:lane-one')
  item.pending_questions[0].status = 'answered'
  assert.equal(hasWorkQuestion(item), false)
})
