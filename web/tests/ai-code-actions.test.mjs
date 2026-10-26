import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { buildToolCards, summarizeToolArgs } from '../src/composables/useAgentSession.js'
import { eventTime, sessionPresentation, internalMessagePresentation, incompleteCompletion } from '../src/lib/sessionPresentation.js'
import { teamNoticePresentation } from '../src/lib/agentTeam.js'
import { PREFERENCE_DEFAULTS, shouldSendMessage } from '../src/lib/preferences.js'
async function setup(t) {
  const props = reactive({ sessionId: 's', fileContext: { path: 'a.py', start_line: 3, end_line: 4 }, codeContext: { path: 'a.py', start: 3, end: 4 }, selectionText: 'return value + 1', pendingPrompt: '', writable: false, workflowNodeId: '', workflowTree: 'main' })
  const events = [], calls = [], scope = effectScope()
  const session = { timeline: ref([]), busy: ref(false), phase: ref(''), streamState: ref('open'), historyLoading: ref(false), historyError: ref(''), lastStopReason: ref('end_turn'), banner: ref(''), bannerDismissed: ref(false), recoveredThroughSeq: ref(0), submissionError: ref(''), streaming: ref(''), web: ref(false), usage: ref({ input: 0, output: 0, cached: 0 }), teamRevision: ref(0), teamChange: ref(null), refreshApproval: async () => calls.push(['refreshApproval']), load: async () => {}, send: async (...args) => { calls.push(args); return true } }
  const source = (await readFile(new URL('../src/components/code/AiPanel.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const preferences = reactive({ ...PREFERENCE_DEFAULTS })
  const ctx = vm.createContext({ computed, nextTick, ref, usePreferences: () => ({ preferences }), shouldSendMessage, onBeforeUnmount: () => {}, watch: (...args) => scope.run(() => watch(...args)), useAgentSession: () => session, buildToolCards, summarizeToolArgs, eventTime, sessionPresentation, internalMessagePresentation, incompleteCompletion, teamNoticePresentation, defineProps: () => props, defineEmits: () => (name, payload) => events.push([name, payload]) })
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject = { input, prepareAction, modelStatus, messages, send, canSend, includeContext, onComposerKeydown, composing, usageLabel, usageTitle, chooseAgent, approvalReadonly, fullToolArgs }`, ctx))
  await nextTick(); t.after(() => scope.stop())
  return { s: ctx.subject, props, session, calls, events, preferences }
}
test('code actions fill selected context and stage instructions without sending', async t => {
  const { s, calls } = await setup(t)
  s.prepareAction('modify')
  assert.match(s.input.value, /a\.py:3-4/)
  assert.match(s.input.value, /return value \+ 1/)
  assert.match(s.input.value, /propose_edit/)
  s.prepareAction('tests'); assert.match(s.input.value, /生成.*测试/)
  assert.equal(calls.length, 0)
})

test('coding conversation distinguishes attributed internal notices from matching user-authored text', async t => {
  const { s, session } = await setup(t)
  const text = '[收口自查] 证据够了吗？'
  const records = [
    { type: 'user_message', seq: 1, payload: { text } },
    { type: 'user_message', seq: 2, payload: { text, auto: true, quiet: true, closures: 1 } },
    { type: 'user_message', seq: 3, payload: { text, source: { kind: 'runtime-continuation', form: 'notice', summary: '继续执行未完成的任务' } } },
    { type: 'user_message', seq: 4, payload: { text, source: { kind: 'unknown-user-source', form: 'notice' } } },
  ]
  session.timeline.value = records
  assert.deepEqual(Array.from(s.messages.value, value => value.kind), ['user', 'internal', 'internal', 'user'])
  assert.equal(s.messages.value[1].summary, '验证检查')
  assert.equal(s.messages.value[2].summary, '继续执行未完成的任务')
  assert.equal(s.messages.value[1].text, text)
  assert.equal(session.timeline.value.length, 4)
  assert.equal(session.timeline.value[0].payload.auto, undefined)
})

test('completed style replies reuse the live session, emit once and never apply or request a model', async t => {
  const { session, events, calls } = await setup(t)
  const text = '```json\n' + JSON.stringify({ kind: 'frontend-style', request_id: 'r1', selector: '#title', declarations: { color: '#123456' } }) + '\n```'
  session.streaming.value = text
  await nextTick()
  assert.equal(events.filter(([name]) => name === 'assistant-complete').length, 0)
  session.timeline.value = [{ type: 'assistant_message', seq: 10, payload: { text } }]
  await nextTick()
  const replies = events.filter(([name]) => name === 'assistant-complete')
  assert.equal(replies.length, 1)
  assert.equal(replies[0][1].sessionId, 's')
  assert.equal(replies[0][1].text, text)
  session.timeline.value.push({ type: 'done', seq: 11, payload: { text, stop_reason: 'end_turn' } })
  await nextTick()
  assert.equal(events.filter(([name]) => name === 'assistant-complete').length, 1)
  assert.equal(calls.length, 0)
})

test('ordinary coding, tool-bearing and failed style completions do not become canvas commands', async t => {
  const { session, events } = await setup(t)
  const text = JSON.stringify({ kind: 'frontend-style', request_id: 'r1', selector: '#title', declarations: { color: 'red' } })
  session.timeline.value = [
    { type: 'assistant_message', seq: 1, payload: { text: '普通代码分析' } },
    { type: 'assistant_message', seq: 2, payload: { text, tool_calls: [{ id: 'tool1', name: 'read_file' }] } },
    { type: 'done', seq: 3, payload: { text, stop_reason: 'error' } },
  ]
  await nextTick()
  assert.equal(events.filter(([name]) => name === 'assistant-complete').length, 0)
})

test('expanded coding tool parameters preserve complete multiline commands and additional arguments', async t => {
  const { s } = await setup(t)
  const command = `echo first\n${'x'.repeat(12000)}\necho final`
  const args = { command, cwd: 'temporary folder', timeout: 90 }
  const complete = s.fullToolArgs(args)
  assert.ok(complete.startsWith(command))
  assert.ok(complete.endsWith(JSON.stringify({ cwd: 'temporary folder', timeout: 90 }, null, 2)))
  assert.equal(s.fullToolArgs(JSON.stringify(args)), complete)
  assert.equal(s.fullToolArgs('legacy command\nnext command'), 'legacy command\nnext command')
  assert.equal(s.fullToolArgs(null), '（无参数）')
  assert.equal(s.fullToolArgs({ path: 'src/module.py', content: 'full content\nlast line' }), JSON.stringify({ path: 'src/module.py', content: 'full content\nlast line' }, null, 2))
})

test('team receipts show executor navigation, cached usage and reconnect refresh without sending', async t => {
  const { s, session, events, calls } = await setup(t)
  session.timeline.value = [{ type: 'session_meta', seq: 9, payload: { team_notice: { session_id: 'worker', state: 'completed', title: '构建验证', result: '完成' } } }]
  assert.equal(s.messages.value[0].kind, 'team-notice')
  assert.equal(s.messages.value[0].sessionId, 'worker')
  assert.equal(s.messages.value[0].state, '已完成')
  s.chooseAgent('worker')
  assert.equal(events.at(-1)[0], 'team-refresh')
  assert.equal(calls.length, 0)
  s.chooseAgent('s'); await nextTick()
  assert.deepEqual(calls.at(-1), ['refreshApproval'])
  session.usage.value = { input: 1200, output: 300, cached: 100 }
  assert.match(s.usageLabel.value, /会话累计输入 1,200.*缓存 100.*输出 300/)
  assert.match(s.usageTitle.value, /未缓存输入 1,100/)
  assert.match(s.usageTitle.value, /缓存已包含在输入中，不额外相加/)
  session.lastExecutionUsage = ref({ usage: { input_tokens: 900348, output_tokens: 7000, cached_tokens: 880000 }, delta: {}, model_responses: 10, duration_seconds: 87.784 })
  session.usage.value = { input: 3012728, output: 44103, cached: 2889216 }
  assert.match(s.usageTitle.value, /最近执行：输入 900,348，输出 7,000，10 轮模型响应，87.8 秒/)
  assert.match(s.usageLabel.value, /会话累计输入 3,012,728/)
  session.teamChange.value = { project_id: 'p' }; session.teamRevision.value++
  await nextTick()
  assert.deepEqual(events.at(-1), ['team-refresh', session.teamChange.value])
  session.streamState.value = 'closed'; await nextTick()
  session.streamState.value = 'open'; await nextTick()
  assert.equal(events.at(-1)[0], 'team-refresh')
})

test('selected scheduler state distinguishes waiting children and re-admission from tool work', async t => {
  const { s, session, props } = await setup(t)
  props.team = { lead: { session_id: 's', state: 'waiting', phase: 'waiting_children' }, agents: [] }
  session.busy.value = true; session.phase.value = 'tools'
  assert.equal(s.modelStatus.value.label, '等待子任务')
  props.team.lead.state = 'queued'; props.team.lead.phase = 'queued'
  assert.equal(s.modelStatus.value.label, '排队等待执行')
  session.phase.value = 'preparing_context'
  assert.equal(s.modelStatus.value.label, '准备项目上下文', 'new live execution metadata supersedes a stale roster')
  session.phase.value = 'waiting_approval'
  assert.equal(s.modelStatus.value.label, '等待你批准')
})

test('approval scope follows selected executor effective permission rather than project toggle', async t => {
  const { s, props } = await setup(t)
  props.writable = true
  props.team = { agents: [{ session_id: 's', effective_permission: 'read-only' }] }
  assert.equal(s.approvalReadonly.value, true)
  props.team.agents[0].effective_permission = 'workspace-write'
  assert.equal(s.approvalReadonly.value, false)
  props.team = null; props.writable = false
  assert.equal(s.approvalReadonly.value, true)
})

test('assistant composer applies send preference and leaves composition and newline keys alone', async t => {
  const { s, calls, preferences } = await setup(t)
  let prevented = 0
  const enter = extras => ({ key: 'Enter', preventDefault() { prevented++ }, ...extras })
  s.input.value = 'first task'
  s.composing.value = true
  s.onComposerKeydown(enter())
  s.composing.value = false
  s.onComposerKeydown(enter({ isComposing: true }))
  s.onComposerKeydown(enter({ keyCode: 229 }))
  s.onComposerKeydown(enter({ shiftKey: true }))
  assert.equal(calls.length, 0)
  assert.equal(prevented, 0)
  preferences.sendKey = 'ctrl-enter'
  s.onComposerKeydown(enter())
  assert.equal(calls.length, 0)
  s.onComposerKeydown(enter({ ctrlKey: true }))
  await nextTick()
  assert.equal(calls.length, 1)
  assert.equal(prevented, 1)
  preferences.sendKey = 'enter'
  s.input.value = 'second task'
  s.onComposerKeydown(enter())
  await nextTick()
  assert.equal(calls.length, 2)
})

test('history loading and failed reads show explicit state and prevent sending into an unknown session', async t => {
  const { s, session } = await setup(t)
  s.input.value = 'continue'
  session.historyLoading.value = true
  assert.equal(s.canSend.value, false)
  session.historyLoading.value = false
  session.historyError.value = 'read failed'
  assert.equal(s.canSend.value, false)
  session.historyError.value = ''
  assert.equal(s.canSend.value, true)
  const source = await readFile(new URL('../src/components/code/AiPanel.vue', import.meta.url), 'utf8')
  assert.match(source, /正在读取会话记录/)
  assert.match(source, /读取会话记录失败/)
  assert.match(source, /@click="s\.load\(sessionId\)">重新读取/)
})
test('provider failures stay separate from websocket transport state and successful next turn clears them', async t => {
  const { s, session } = await setup(t)
  session.timeline.value = [{ type: 'error', seq: 2, payload: { message: 'provider: All connection attempts failed' } }, { type: 'done', seq: 3, payload: { stop_reason: 'error' } }]
  session.banner.value = 'provider: All connection attempts failed'
  session.lastStopReason.value = 'provider_error'
  assert.equal(s.modelStatus.value.label, '模型请求失败')
  assert.equal(session.streamState.value, 'open')
  session.busy.value = true; session.phase.value = 'model'; assert.equal(s.modelStatus.value.label, '模型响应中')
  session.timeline.value.push({ type: 'done', seq: 4, payload: { stop_reason: 'end_turn' } }); session.busy.value = false
  assert.equal(s.modelStatus.value.label, '模型待命')
  assert.equal(s.modelStatus.value.banner, '')
  assert.equal(s.messages.value.find(message => message.kind === 'error').historical, true)
})

test('a real streaming recovery archives the older failure before a terminal event, and the same new error remains visible', async t => {
  const { s, session } = await setup(t)
  const message = 'provider 错误: HTTP 402: Insufficient Balance'
  session.timeline.value = [{ type: 'error', seq: 2, payload: { code: 'provider_error', message } }, { type: 'done', seq: 3, payload: { stop_reason: 'provider_error' } }]
  session.recoveredThroughSeq.value = 3
  session.streaming.value = '已收到真实模型输出'
  assert.equal(s.modelStatus.value.state, 'idle')
  assert.equal(s.messages.value.find(item => item.key === 'e2').historical, true)
  session.timeline.value.push({ type: 'error', seq: 4, payload: { code: 'provider_error', message } })
  session.banner.value = message
  assert.equal(s.modelStatus.value.state, 'error')
  assert.equal(s.modelStatus.value.banner, message)
  assert.equal(s.messages.value.find(item => item.key === 'e4').historical, false)
})

test('a successful assistant turn resolves prior model failure without requiring another user message or done', async t => {
  const { s, session } = await setup(t)
  session.timeline.value = [{ type: 'error', seq: 1, payload: { code: 'provider_error', message: 'provider: HTTP402' } }, { type: 'done', seq: 2, payload: { stop_reason: 'provider_error' } }, { type: 'assistant_message', seq: 3, payload: { text: '继续执行', tool_calls: [] } }]
  assert.equal(s.modelStatus.value.state, 'idle')
  assert.equal(s.messages.value.find(item => item.key === 'e1').historical, true)
})

test('cancellation, budget termination, empty assistant messages and idle snapshots do not claim a failed model recovered', async t => {
  const { s, session } = await setup(t)
  const failure = { type: 'error', seq: 1, payload: { code: 'provider_error', message: 'provider: HTTP402' } }
  for (const stop of ['cancelled', 'cage_budget', 'repeated_failure']) {
    session.timeline.value = [failure, { type: 'assistant_message', seq: 2, payload: { text: ' ', tool_calls: [] } }, { type: 'done', seq: 3, payload: { stop_reason: stop } }]
    session.banner.value = failure.payload.message
    session.busy.value = false
    assert.equal(s.modelStatus.value.state, 'error')
    assert.equal(s.modelStatus.value.banner, failure.payload.message)
    assert.equal(s.messages.value.find(item => item.key === 'e1').historical, false)
  }
})

test('explicitly hiding an HTTP failure banner preserves its failed request status', async t => {
  const { s, session } = await setup(t)
  session.submissionError.value = 'provider: HTTP402'
  session.bannerDismissed.value = true
  assert.equal(s.modelStatus.value.banner, '')
  assert.equal(s.modelStatus.value.state, 'error')
  session.bannerDismissed.value = false
  session.banner.value = session.submissionError.value
  assert.equal(s.modelStatus.value.banner, 'provider: HTTP402')
  const source = await readFile(new URL('../src/components/code/AiPanel.vue', import.meta.url), 'utf8')
  assert.match(source, /aria-label="收起错误提示"[^>]*@click="s\.dismissBanner\(\)"/)
})

test('code assistant labels context preparation separately from model work and unknown execution', async t => {
  const { s, session } = await setup(t)
  session.busy.value = true
  for (const [phase, label] of [['', '执行中'], ['preparing_context', '准备项目上下文'], ['model', '模型响应中'], ['future_phase', '执行中']]) {
    session.phase.value = phase
    assert.equal(s.modelStatus.value.label, label)
    assert.equal(s.modelStatus.value.state, 'busy')
  }
  session.busy.value = false
  assert.equal(s.modelStatus.value.label, '模型待命')
  const source = await readFile(new URL('../src/components/code/AiPanel.vue', import.meta.url), 'utf8')
  assert.match(source, /v-else-if="s\.busy\.value && !pendingQuestions\.length"[^>]*role="status">\{\{ modelStatus\.label \}\}…/)
})
for (const stop of ['end_turn', 'cancelled', 'user_cancel', 'max_turns', 'repeated_failure', 'needs_input', 'approval', 'budget_exceeded', 'error']) {
  test(`later ${stop} completion cannot revive an older provider failure`, async t => {
    const { s, session } = await setup(t)
    const message = 'provider: All connection attempts failed'
    session.banner.value = message
    session.timeline.value = [
      { type: 'error', seq: 2, ts: '2026-09-29T02:12:00Z', payload: { message } },
      { type: 'done', seq: 3, payload: { stop_reason: 'error' } },
      { type: 'user_message', seq: 60, payload: { text: 'later request' } },
      { type: 'done', seq: 64, payload: { stop_reason: stop, text: 'later response' } },
    ]
    assert.equal(s.modelStatus.value.state, stop === 'end_turn' ? 'idle' : 'incomplete')
    assert.equal(s.modelStatus.value.banner, '')
    const oldError = s.messages.value.find(message => message.key === 'e2')
    assert.equal(oldError.text, message)
    assert.equal(oldError.historical, true)
    assert.match(oldError.when, /2026/)
    assert.equal(s.messages.value.at(-1).stop, stop)
  })
}
test('a new provider error remains current after earlier successful turns', async t => {
  const { s, session } = await setup(t)
  session.timeline.value = [
    { type: 'error', seq: 2, payload: { message: 'provider: old failure' } },
    { type: 'done', seq: 64, payload: { stop_reason: 'end_turn' } },
    { type: 'user_message', seq: 65, payload: { text: 'new request' } },
    { type: 'error', seq: 66, payload: { code: 'provider_error', message: 'current API key invalid' } },
    { type: 'done', seq: 67, payload: { stop_reason: 'error' } },
  ]
  assert.equal(s.modelStatus.value.label, '模型请求失败')
  assert.equal(s.messages.value.find(message => message.key === 'e2').historical, true)
  assert.equal(s.messages.value.find(message => message.key === 'e66').historical, false)
})
test('a generic error in a newer turn cannot borrow the preceding turn provider error', async t => {
  const { s, session } = await setup(t)
  session.timeline.value = [
    { type: 'error', seq: 2, payload: { message: 'provider: old failure' } },
    { type: 'user_message', seq: 3, payload: { text: 'new request' } },
    { type: 'done', seq: 4, payload: { stop_reason: 'error' } },
  ]
  assert.equal(s.modelStatus.value.state, 'incomplete')
  assert.equal(s.messages.value[0].historical, true)
  assert.equal(s.messages.value.at(-1).stop, 'error')
})
test('current HTTP submission failure is visible even when its text matches a resolved historical error', async t => {
  const { s, session } = await setup(t)
  const message = 'provider: All connection attempts failed'
  session.timeline.value = [{ type: 'error', seq: 2, payload: { message } }, { type: 'done', seq: 64, payload: { stop_reason: 'end_turn' } }]
  session.banner.value = session.submissionError.value = message
  assert.equal(s.modelStatus.value.state, 'error')
  assert.equal(s.modelStatus.value.banner, message)
  assert.equal(s.messages.value[0].historical, true)
})
test('historical errors use native disclosure while the latest error stays expanded', async () => {
  const source = await readFile(new URL('../src/components/code/AiPanel.vue', import.meta.url), 'utf8')
  assert.match(source, /<details v-else-if="m\.historical"/)
  assert.match(source, /<summary>历史错误/)
  assert.match(source, /本次请求错误/)
  assert.doesNotMatch(source, /<details[^>]*\sopen(?:=|\s|>)/)
})
test('a successful proposal refreshes review once and sessions reset deduplication', async t => {
  const { session, props, events } = await setup(t)
  session.timeline.value = [{ type: 'tool_use', seq: 1, payload: { call: { id: 'call-1', name: 'propose_edit', arguments: {} } } }, { type: 'tool_result', seq: 2, payload: { call_id: 'call-1', result: '{"proposal_id":"r1"}' } }]
  await nextTick()
  session.timeline.value = [...session.timeline.value]; await nextTick()
  assert.equal(events.filter(([name]) => name === 'review-refresh').length, 1)
  props.sessionId = 'another-session'; await nextTick()
  session.timeline.value = [...session.timeline.value]; await nextTick()
  assert.equal(events.filter(([name]) => name === 'review-refresh').length, 2)
})

test('switching dialogue retains independent composer drafts and workflow handoff stays on the new dialogue', async t => {
  const { s, props } = await setup(t)
  s.input.value = 'Lead 草稿'
  props.sessionId = 'child'; await nextTick(); assert.equal(s.input.value, '')
  s.input.value = '子任务草稿'
  props.sessionId = 's'; await nextTick(); assert.equal(s.input.value, 'Lead 草稿')
  props.sessionId = 'child'; props.pendingPrompt = '节点待分配'; await nextTick()
  assert.equal(s.input.value, '节点待分配')
})

test('node routing identity survives disabled file context and is consumed only after explicit submission', async t => {
  const { s, props, calls, events } = await setup(t)
  props.workflowNodeId = 'verification'; props.workflowTree = 'task:42'
  s.includeContext.value = false; s.input.value = '安排验收'
  assert.equal(calls.length, 0)
  await s.send()
  assert.equal(calls[0][1].codeContext.workflow_node_id, 'verification')
  assert.equal(calls[0][1].codeContext.workflow_tree_id, 'task:42')
  assert.equal(calls[0][1].codeContext.path, undefined)
  assert.equal(events.some(([name]) => name === 'sent'), true)
})
