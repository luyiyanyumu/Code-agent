import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { marked } from 'marked'
import { api, getToken, openStream, readFileBase64 } from '../src/api.js'
import { useAgentSession } from '../src/composables/useAgentSession.js'
import { createSessionStream } from '../src/lib/sessionStream.js'
import { sessionPhaseLabel, sessionPresentation } from '../src/lib/sessionPresentation.js'
import { internalMessagePresentation, isSuccessfulCompletion, incompleteCompletion } from '../../chat-app/src/lib/sessionPresentation.js'
import { attachThinking, pushThinking } from '../src/lib/thinkingSegments.js'
import * as todos from '../../chat-app/src/lib/todos.js'
import * as questions from '../../chat-app/src/lib/questions.js'
import { generalChatSessions } from '../../chat-app/src/lib/chatSessions.js'
import { PREFERENCE_DEFAULTS, shouldSendMessage } from '../src/lib/preferences.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const event = (seq, type, payload = {}) => ({ seq, type, payload, ts: seq })

class FakeSocket {
  static OPEN = 1
  static sockets = []
  constructor(url) { this.url = url; this.readyState = 0; this.sent = []; FakeSocket.sockets.push(this) }
  send(data) { this.sent.push(JSON.parse(data)) }
  close() { this.readyState = 3; this.onclose?.({ code: 1000 }) }
  open() { this.readyState = 1; this.onopen?.() }
  emit(value) { this.onmessage?.({ data: JSON.stringify(value) }) }
}

function setup(t, kind = 'panel') {
  const savedApi = { ...api }
  const savedGlobals = { WebSocket: globalThis.WebSocket, location: globalThis.location, localStorage: globalThis.localStorage, window: globalThis.window, fetch: globalThis.fetch }
  globalThis.WebSocket = FakeSocket
  globalThis.location = { protocol: 'http:', host: 'localhost' }
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.window = { localStorage: globalThis.localStorage, removeEventListener() {} }
  FakeSocket.sockets = []
  api.events = async () => []
  api.pendingApproval = async () => null
  api.pendingQuestions = async () => ({ questions: [] })
  api.checkpoints = async () => ({ checkpoints: [], busy: false })
  api.acceptance = async () => null
  api.webCapability = async () => ({ available: true })
  const dialogs = []
  const useDialog = () => ({ open: async options => { dialogs.push(options); return null } })
  const scope = effectScope()
  const preferences = reactive({ ...PREFERENCE_DEFAULTS })
  let unmount
  let state
  if (kind === 'panel') state = scope.run(useAgentSession)
  else {
    // 直接执行真实 SFC 脚本，只替换生命周期与导入，不复制一份状态机。
    const source = readFileSync(new URL('../../chat-app/src/views/ChatView.vue', import.meta.url), 'utf8')
    const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
      .replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"]\s*$/gm, '')
    const bindings = {
      computed, ref, watch, nextTick, marked, api, getToken, openStream, readFileBase64, usePreferences: () => ({ preferences }), shouldSendMessage,
      createSessionStream, sessionPhaseLabel, internalMessagePresentation, isSuccessfulCompletion, incompleteCompletion, attachThinking, pushThinking, useDialog, generalChatSessions, ...todos, ...questions,
      defineProps: () => ({ user: { role: 'admin' } }),
      onMounted() {}, onBeforeUnmount: (fn) => { unmount = fn },
    }
    const expose = '({ selectSession, newSessionIn, closeStream, current, events, timeline, renderedTimeline, stats, usage, busy, phase, activityLabel, banner, approval, approvalError, approvalSubmitting, input, pendingFiles, send, onComposerKeydown, composing, stopRun, decide, rebuildTimeline, thinking, thinkSink, pushEvent, preview, openArtifact, closePreview, handlePickedFile, createWorkspace, deleteWorkspace, moveSession, renameSession, archiveSession, deleteSession, rewindTo, dismissQuestions, sessions, workspaces, collapsedGroups, question, qBusy, rewindBusy, lastStopReason })'
    state = scope.run(() => new Function(...Object.keys(bindings), `${script}\nreturn ${expose}`)(...Object.values(bindings)))
  }
  t.after(() => {
    unmount?.()
    scope.stop()
    Object.assign(api, savedApi)
    Object.assign(globalThis, savedGlobals)
  })
  return { state, scope, dialogs, preferences, socket: () => FakeSocket.sockets.at(-1) }
}

test('panel: completion metadata survives replay once and project changes remain transient', async t => {
  const { state: s, socket } = setup(t)
  const receipt = event(2, 'session_meta', { team_notice: { session_id: 'worker', activation_id: 1, title: '测试', state: 'completed', result: '验证已完成' } })
  api.events = async () => [event(1, 'user_message'), receipt]
  await s.load('lead')
  assert.equal(s.timeline.value.filter(e => e.payload?.team_notice).length, 1)
  socket().open()
  socket().emit(receipt)
  assert.equal(s.timeline.value.filter(e => e.payload?.team_notice).length, 1)
  const change = { project_id: 'p', session_id: 'worker', activation_id: 1 }
  socket().emit(event(0, 'session_meta', { team_changed: change }))
  assert.equal(s.teamRevision.value, 1)
  assert.deepEqual(s.teamChange.value, change)
  assert.equal(s.timeline.value.length, 2, 'change notifications are not timeline messages')
  const next = event(3, 'assistant_message', { text: '继续检查' })
  socket().emit(next)
  assert.equal(s.timeline.value.length, 3)
})

test('panel: tool frames update execution phase immediately without overriding live approvals', async t => {
  const { state: s, socket } = setup(t)
  await s.load('lead'); socket().open()
  socket().emit(event(0, 'session_meta', { busy: true, phase: 'model' }))
  socket().emit(event(1, 'tool_use', { call: { id: 'read', name: 'read_file', arguments: { path: 'a.py' } } }))
  assert.equal(s.phase.value, 'tools')
  socket().emit(event(0, 'approval_request', { approval_id: 'a', tool: 'bash' }))
  socket().emit(event(2, 'tool_use', { call: { id: 'other', name: 'read_file', arguments: { path: 'b.py' } } }))
  assert.equal(s.phase.value, 'waiting_approval')
  socket().emit(event(0, 'approval_decided', { approval_id: 'a' }))
  socket().emit(event(3, 'tool_use', { call: { id: 'next', name: 'read_file', arguments: { path: 'b.py' } } }))
  assert.equal(s.phase.value, 'tools')
})

test('chat composer follows changed send shortcut and protects IME candidate confirmation', async t => {
  const { state: s, socket, preferences } = setup(t, 'chat')
  await s.selectSession('a')
  socket().open()
  let prevented = 0
  const key = extras => ({ key: 'Enter', preventDefault() { prevented++ }, ...extras })
  const prompts = () => socket().sent.filter(message => message.type === 'prompt')
  s.input.value = '中文内容'
  s.composing.value = true
  s.onComposerKeydown(key())
  s.composing.value = false
  s.onComposerKeydown(key({ isComposing: true }))
  s.onComposerKeydown(key({ keyCode: 229 }))
  assert.equal(prompts().length, 0)
  assert.equal(prevented, 0)
  preferences.sendKey = 'ctrl-enter'
  s.onComposerKeydown(key())
  assert.equal(prompts().length, 0)
  s.onComposerKeydown(key({ metaKey: true }))
  assert.equal(prompts().length, 1)
  assert.equal(prompts()[0].text, '中文内容')
  assert.equal(prevented, 1)
  socket().emit(event(1, 'done'))
  preferences.sendKey = 'enter'
  s.input.value = 'next message'
  s.onComposerKeydown(key({ shiftKey: true }))
  assert.equal(prompts().length, 1)
  s.onComposerKeydown(key())
  assert.equal(prompts().length, 2)
})

test('stream ignores old socket frames and clears reconnect/heartbeat on close', () => {
  const timers = new Map()
  const sockets = []
  const seen = []
  let n = 0, seq = 5
  const stream = createSessionStream({
    open: (id, after) => { const s = new FakeSocket(`${id}:${after}`); sockets.push(s); return s },
    afterSeq: () => seq, onState() {}, onError() {}, onEvent: (e) => seen.push(e),
    schedule: (fn, delay) => { const id = ++n; timers.set(id, { fn, delay }); return id },
    unschedule: (id) => timers.delete(id),
  })
  stream.connect('a')
  const old = sockets[0]
  const oldMessage = old.onmessage
  old.open()
  assert.equal([...timers.values()][0].delay, 30000)
  old.close()
  assert.equal([...timers.values()][0].delay, 2000)
  seq = 9
  const pendingReconnect = [...timers.values()][0].fn
  stream.connect('b')
  oldMessage({ data: JSON.stringify(event(6, 'user_message')) })
  pendingReconnect()
  assert.equal(sockets.length, 2)
  assert.equal(sockets[1].url, 'b:9')
  assert.equal(seen.length, 0)
  stream.close()
  assert.equal(timers.size, 0)
})

test('terminal server close does not retry; heartbeat sends ping; malformed frames are ignored', () => {
  const timers = new Map()
  const errors = [], seen = []
  let n = 0, socket
  const stream = createSessionStream({
    open: () => (socket = new FakeSocket('a')), afterSeq: () => 0,
    onState() {}, onError: (e) => errors.push(e), onEvent: (e) => seen.push(e),
    schedule: (fn) => { timers.set(++n, fn); return n }, unschedule: (id) => timers.delete(id),
  })
  stream.connect('a')
  socket.open()
  const tick = timers.values().next().value
  timers.clear(); tick()
  assert.deepEqual(socket.sent, [{ type: 'ping' }])
  socket.onmessage({ data: 'invalid json' })
  socket.emit(null)
  socket.emit(event(1, 'done'))
  assert.equal(seen.length, 1)
  socket.onclose({ code: 4404 })
  assert.match(errors[0], /不存在/)
  assert.equal(timers.size, 0)
  stream.close()
})

for (const kind of ['panel', 'chat']) {
  const load = (s, id) => kind === 'panel' ? s.load(id) : s.selectSession(id)

  test(`${kind}: runtime attribution survives replay and live delivery without rewriting real user input`, async t => {
    const { state: s, socket } = setup(t, kind)
    const text = '[收口自查] 证据够了吗？'
    const records = [
      event(1, 'user_message', { text }),
      event(2, 'user_message', { text, auto: true, quiet: true, closures: 1, closure_kind: 'evidence' }),
      event(3, 'user_message', { text, source: { kind: 'runtime-continuation', form: 'notice', summary: '继续执行未完成的任务' } }),
      event(4, 'user_message', { text, source: { kind: 'unknown-user-source', form: 'notice' } }),
    ]
    const original = JSON.stringify(records)
    api.events = async () => records
    await load(s, 'a'); socket().open()
    assert.equal(s.events.value.length, 4)
    assert.equal(s.events.value[0].payload.auto, undefined)
    assert.equal(s.events.value[2].payload.source.kind, 'runtime-continuation')
    socket().emit(records[2])
    const notice = event(5, 'user_message', { text: '后台任务已完成', auto: true, quiet: true, source: { kind: 'job-notice', form: 'notice', summary: '收到任务结果' } })
    socket().emit(notice)
    assert.equal(s.events.value.length, 5, 'replay duplication is ignored, but the raw internal event is retained')
    assert.equal(s.events.value[4].payload.text, notice.payload.text)
    assert.deepEqual(s.events.value[4].payload.source, notice.payload.source)
    assert.equal(JSON.stringify(records), original)
    if (kind === 'chat') {
      assert.equal(s.stats.value.prompts, 2, 'internal model-role messages do not count as new human prompts')
      assert.deepEqual(s.renderedTimeline.value.filter(node => node.internal).map(node => node.ev.seq), [2, 3, 5])
      assert.deepEqual(s.renderedTimeline.value.filter(node => !node.internal).map(node => node.ev.seq), [1, 4])
      assert.equal(s.timeline.value.length, 5, 'presentation does not delete events from the execution log')
    }
  })

  test(`${kind}: interrupted and unknown terminal reasons retain partial output without automatically sending another prompt`, async t => {
    for (const stop of ['max_tokens', 'content_filter', 'future_provider_stop', 'tool_use']) await t.test(stop, async sub => {
      const { state: s, socket } = setup(sub, kind)
      await load(s, 'a'); socket().open()
      socket().emit(event(1, 'user_message', { text: '当前任务' }))
      const done = event(2, 'done', { text: '尚未完成的部分输出', stop_reason: stop })
      socket().emit(done)
      assert.equal(s.busy.value, false)
      assert.equal(s.lastStopReason.value, stop)
      assert.equal(s.events.value.at(-1).payload.text, done.payload.text)
      if (kind === 'panel') assert.equal(panelPresentation(s).state, 'incomplete')
      else assert.equal(s.renderedTimeline.value.at(-1).incomplete.reason, stop)
      assert.deepEqual(socket().sent, [], 'a stopped provider response does not synthesize a human continuation')
      api.events = async () => [event(1, 'user_message', { text: '当前任务' }), done]
      await load(s, 'a')
      assert.equal(s.lastStopReason.value, stop)
      assert.equal(s.events.value.at(-1).payload.text, done.payload.text)
      if (kind === 'panel') assert.equal(panelPresentation(s).state, 'incomplete')
      else assert.equal(s.renderedTimeline.value.at(-1).incomplete.reason, stop)
    })
  })

  test(`${kind}: an internal notice followed by a truncated completion cannot erase an actual model failure`, async t => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a'); socket().open()
    socket().emit(event(1, 'error', { code: 'provider_error', message: 'provider: actual failure' }))
    socket().emit(event(2, 'user_message', { text: '内部恢复提示', auto: true, quiet: true, source: { kind: 'runtime-continuation', form: 'notice' } }))
    socket().emit(event(3, 'done', { text: '部分输出', stop_reason: 'max_tokens' }))
    assert.equal(s.banner.value, 'provider: actual failure')
    assert.equal(s.busy.value, false)
    assert.deepEqual(socket().sent, [])
    socket().emit(event(4, 'user_message', { text: '[收口自查] 证据够了吗？' }))
    socket().emit(event(5, 'done', { text: '新任务的部分输出', stop_reason: 'content_filter' }))
    assert.equal(s.banner.value, '', 'a genuine new human request must not inherit an older task failure')
    if (kind === 'panel') assert.equal(panelPresentation(s).state, 'incomplete')
    else assert.equal(s.renderedTimeline.value.at(-1).incomplete.reason, 'content_filter')
  })

  test(`${kind}: runtime phases survive heartbeat, change with real work, and clear at request boundaries`, async (t) => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a')
    socket().open()
    const status = () => kind === 'panel' ? panelPresentation(s).label : s.activityLabel.value
    const metadata = phase => socket().emit(event(0, 'session_meta', { busy: true, phase }))
    metadata('preparing_context')
    assert.equal(s.busy.value, true)
    assert.equal(status(), '准备项目上下文')
    socket().emit(event(0, 'session_meta', { pong: true }))
    assert.equal(s.phase.value, 'preparing_context')
    metadata('model')
    assert.equal(status(), '模型响应中')
    metadata('')
    assert.equal(status(), '执行中')
    metadata('future_phase')
    assert.equal(status(), '执行中')

    metadata('preparing_context')
    socket().emit(event(1, 'error', { quiet: true, code: 'segment_boundary' }))
    assert.equal(s.phase.value, 'preparing_context')
    socket().emit(event(2, 'error', { message: 'context preparation failed' }))
    assert.equal(s.busy.value, false)
    assert.equal(s.phase.value, '')

    metadata('model')
    socket().emit(event(3, 'done'))
    assert.equal(s.busy.value, false)
    assert.equal(s.phase.value, '')
    metadata('model')
    socket().emit(event(4, 'user_message'))
    assert.equal(s.phase.value, '')
    metadata('preparing_context')
    ;(kind === 'panel' ? s.stop : s.stopRun)()
    assert.equal(s.busy.value, false)
    assert.equal(s.phase.value, '')
    metadata('model')
    socket().emit(event(0, 'session_meta', { busy: false, phase: 'model' }))
    assert.equal(s.phase.value, '')
    metadata('preparing_context')
    await load(s, 'b')
    assert.equal(s.busy.value, false)
    assert.equal(s.phase.value, '')
    // A reconnect's runtime snapshot restores the current preparation stage.
    metadata('preparing_context')
    assert.equal(status(), '准备项目上下文')
  })

  test(`${kind}: stale history from an earlier selection of the same session is ignored`, async (t) => {
    const { state: s } = setup(t, kind)
    const old = deferred()
    let calls = 0
    api.events = () => ++calls === 1 ? old.promise : Promise.resolve([event(2, 'assistant_message', { text: 'new' })])
    const first = load(s, 'a')
    await load(s, 'a')
    old.resolve([event(3, 'assistant_message', { text: 'old' })])
    await first
    assert.deepEqual(s.events.value.map((e) => e.payload.text), ['new'])
    assert.equal(FakeSocket.sockets.length, kind === 'panel' ? 2 : 1)
  })

  test(`${kind}: pending approval from the old session cannot appear in the new session`, async (t) => {
    const { state: s } = setup(t, kind)
    const old = deferred()
    api.pendingApproval = (id) => id === 'a' ? old.promise : Promise.resolve(null)
    const first = load(s, 'a')
    await Promise.resolve(); await Promise.resolve()
    await load(s, 'b')
    old.resolve({ approval_id: 'old' })
    await first
    assert.equal(s.approval.value, null)
    assert.equal(s.busy.value, false)
  })

  test(`${kind}: runtime pending-approval snapshot restores the full card even when REST recovery fails`, async t => {
    const { state: s, socket } = setup(t, kind)
    api.pendingApproval = async () => { throw new Error('approval lookup unavailable') }
    await load(s, 'a'); socket().open()
    assert.match(s.approvalError.value, /approval lookup unavailable/, 'lookup failure is available for an actionable UI error')
    const pending = { approval_id: 'snapshot-approval', tool: 'bash', arguments: { command: 'npm run build' }, reason: '需要本次执行授权' }
    socket().emit(event(0, 'session_meta', { busy: true, phase: 'waiting_approval', pending_approval: pending }))
    assert.deepEqual(s.approval.value, pending, 'the websocket snapshot supplies an immediately usable approval card')
    assert.equal(s.busy.value, true)
    assert.equal(s.phase.value, 'waiting_approval')
    assert.equal(s.approvalError.value, '', 'an authoritative snapshot resolves the stale lookup error')
    await Promise.resolve(); await Promise.resolve()
    assert.deepEqual(s.approval.value, pending, 'a failed fallback cannot hide the snapshot card')
    assert.equal(s.approvalError.value, '')
    assert.equal(s.banner.value, '')
  })

  // The coding panel connects while REST history loads. Chat connects after its
  // history rebuild, so this startup race exists only in the panel lifecycle.
  if (kind === 'panel') test(`${kind}: a pending-approval snapshot received during history loading survives initialization fallback`, async t => {
    const { state: s, socket } = setup(t, kind), history = deferred()
    api.events = () => history.promise
    api.pendingApproval = async () => null
    const loading = load(s, 'a')
    socket().open()
    const pending = { approval_id: 'snapshot-during-load', tool: 'bash', arguments: { command: 'npm run build' } }
    socket().emit(event(0, 'session_meta', { busy: true, phase: 'waiting_approval', pending_approval: pending }))
    assert.deepEqual(s.approval.value, pending)
    history.resolve([]); await loading
    assert.deepEqual(s.approval.value, pending, 'post-history initialization must retain the already-authoritative snapshot')
    assert.equal(s.phase.value, 'waiting_approval')
  })

  test(`${kind}: explicit null pending-approval snapshot immediately clears the old card and submitting state`, async t => {
    const { state: s, socket } = setup(t, kind)
    api.pendingApproval = async () => ({ approval_id: 'old' })
    await load(s, 'a'); socket().open()
    s.approvalSubmitting.value = 'old'
    api.pendingApproval = async () => { throw new Error('approval lookup unavailable') }
    socket().emit(event(0, 'session_meta', { busy: false, phase: '', pending_approval: null }))
    assert.equal(s.approval.value, null, 'null is authoritative, rather than an omitted legacy field')
    assert.equal(s.approvalSubmitting.value, '')
    assert.equal(s.busy.value, false)
    assert.equal(s.phase.value, '')
    await Promise.resolve(); await Promise.resolve()
    assert.equal(s.approval.value, null)
  })

  test(`${kind}: a legacy runtime snapshot without pending-approval still restores the card through REST`, async t => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a'); socket().open()
    const pending = { approval_id: 'legacy-rest', tool: 'write_file', arguments: { path: 'src/main.js' } }, calls = []
    api.pendingApproval = async id => { calls.push(id); return pending }
    socket().emit(event(0, 'session_meta', { busy: true, phase: 'waiting_approval' }))
    await Promise.resolve(); await Promise.resolve()
    assert.deepEqual(calls, ['a'])
    assert.deepEqual(s.approval.value, pending)
    assert.equal(s.busy.value, true)
    assert.equal(s.phase.value, 'waiting_approval')
  })

  for (const snapshot of [{ approval_id: 'newer-snapshot', tool: 'bash' }, null]) {
    test(`${kind}: late REST approval cannot overwrite a newer ${snapshot ? 'card' : 'null'} snapshot`, async t => {
      const { state: s, socket } = setup(t, kind), requested = deferred(), pending = deferred()
      api.pendingApproval = () => { requested.resolve(); return pending.promise }
      const loading = load(s, 'a')
      await requested.promise
      socket().open()
      socket().emit(event(0, 'session_meta', { busy: !!snapshot, phase: snapshot ? 'waiting_approval' : '', pending_approval: snapshot }))
      assert.deepEqual(s.approval.value, snapshot)
      pending.resolve({ approval_id: 'stale-rest', tool: 'write_file' })
      await loading
      assert.deepEqual(s.approval.value, snapshot, 'the newer websocket state wins over the already-running lookup')
      assert.equal(s.busy.value, !!snapshot)
      assert.equal(s.phase.value, snapshot ? 'waiting_approval' : '')
    })
  }

  test(`${kind}: old approval REST results and snapshot frames cannot mutate a newly selected session`, async t => {
    const { state: s, socket } = setup(t, kind), requested = deferred(), pending = deferred()
    api.pendingApproval = id => {
      if (id === 'a') { requested.resolve(); return pending.promise }
      return Promise.resolve(null)
    }
    const loading = load(s, 'a')
    await requested.promise
    socket().open()
    const oldMessage = socket().onmessage
    socket().emit(event(0, 'session_meta', { busy: true, phase: 'waiting_approval', pending_approval: { approval_id: 'old-snapshot' } }))
    assert.equal(s.approval.value.approval_id, 'old-snapshot')
    await load(s, 'b'); socket().open()
    const current = { approval_id: 'current-snapshot', tool: 'bash', arguments: { command: 'npm test' } }
    socket().emit(event(0, 'session_meta', { busy: true, phase: 'waiting_approval', pending_approval: current }))
    pending.resolve({ approval_id: 'old-rest' }); await loading
    oldMessage({ data: JSON.stringify(event(0, 'session_meta', { busy: false, phase: '', pending_approval: null })) })
    oldMessage({ data: JSON.stringify(event(0, 'session_meta', { busy: true, phase: 'waiting_approval', pending_approval: { approval_id: 'late-old-snapshot' } })) })
    assert.deepEqual(s.approval.value, current)
    assert.equal(s.busy.value, true)
    assert.equal(s.phase.value, 'waiting_approval')
  })

  test(`${kind}: permission requests require a matching decision and restore after refresh or reconnect`, async t => {
    const { state: s, socket } = setup(t, kind)
    const first = { approval_id: 'first', tool: 'bash', arguments: { command: 'python build.py' }, reason: '只读权限需要本次授权' }
    api.pendingApproval = async () => first
    await load(s, 'a')
    assert.equal(s.approval.value.approval_id, 'first')
    assert.equal(s.busy.value, true)
    assert.equal(sessionPhaseLabel(s.phase.value), '等待你批准')
    socket().emit(event(0, 'approval_decided', {}))
    socket().emit(event(0, 'approval_decided', { approval_id: 'unrelated' }))
    assert.equal(s.approval.value.approval_id, 'first')
    socket().emit(event(0, 'approval_decided', { approval_id: 'first', reason: 'timeout' }))
    assert.equal(s.approval.value, null)
    assert.equal(s.phase.value, '')
    api.pendingApproval = async () => ({ ...first, approval_id: 'after-reconnect' })
    socket().open()
    socket().emit(event(0, 'session_meta', { busy: true, phase: 'waiting_approval' }))
    await Promise.resolve(); await Promise.resolve()
    assert.equal(s.approval.value.approval_id, 'after-reconnect')
  })

  test(`${kind}: reconnect removes a stale card when the server has no pending approval`, async t => {
    const { state: s, socket } = setup(t, kind)
    api.pendingApproval = async () => ({ approval_id: 'stale' })
    await load(s, 'a')
    assert.equal(s.approval.value.approval_id, 'stale')
    api.pendingApproval = async () => null
    socket().open()
    socket().emit(event(0, 'session_meta', { busy: false, phase: '' }))
    await Promise.resolve(); await Promise.resolve()
    assert.equal(s.approval.value, null)
  })

  test(`${kind}: approval submit is single flight, retryable, and cannot unlock a newer request`, async t => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a')
    socket().emit(event(0, 'approval_request', { approval_id: 'first' }))
    const request = deferred(), calls = []
    api.decideApproval = (id, approved) => { calls.push([id, approved]); return request.promise }
    const submitting = s.decide(true)
    await s.decide(false)
    assert.deepEqual(calls, [['first', true]])
    assert.equal(s.approvalSubmitting.value, 'first')
    request.reject(new Error('network unavailable'))
    await submitting
    assert.equal(s.approval.value.approval_id, 'first')
    assert.equal(s.approvalSubmitting.value, '')
    const old = deferred(), next = deferred()
    api.decideApproval = id => id === 'first' ? old.promise : next.promise
    const retry = s.decide(true)
    socket().emit(event(0, 'approval_request', { approval_id: 'next' }))
    const second = s.decide(false)
    old.resolve({ ok: true, delivered: true })
    await retry
    assert.equal(s.approval.value.approval_id, 'next')
    assert.equal(s.approvalSubmitting.value, 'next')
    next.resolve({ ok: true, delivered: true })
    await second
    assert.equal(s.approval.value, null)
    assert.equal(s.approvalSubmitting.value, '')
  })

  test(`${kind}: expired approval reports expiry and reconciles the next pending request`, async t => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a')
    socket().emit(event(0, 'approval_request', { approval_id: 'expired' }))
    api.decideApproval = async () => ({ ok: true, delivered: false })
    api.pendingApproval = async () => ({ approval_id: 'current', tool: 'write_file' })
    await s.decide(true)
    assert.match(s.banner.value, /申请已结束，请重新发起操作/)
    assert.equal(s.approval.value.approval_id, 'current')
    assert.equal(s.approvalSubmitting.value, '')
    assert.equal(s.phase.value, 'waiting_approval')
  })

  test(`${kind}: failed approval keeps its card and decision response cannot clear a newer approval`, async (t) => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a')
    socket().emit(event(0, 'approval_request', { approval_id: 'first' }))
    api.decideApproval = async () => { throw new Error('temporary failure') }
    await s.decide(true)
    assert.equal(s.approval.value.approval_id, 'first')
    const decision = deferred()
    api.decideApproval = () => decision.promise
    const pending = s.decide(true)
    socket().emit(event(0, 'approval_request', { approval_id: 'second' }))
    decision.resolve({})
    await pending
    assert.equal(s.approval.value.approval_id, 'second')
  })

  test(`${kind}: REST completion and concurrent WS done count usage once`, async (t) => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a')
    const result = deferred()
    api.prompt = () => result.promise
    const done = event(2, 'done', { usage: { input_tokens: 12, output_tokens: 4 } })
    api.events = async () => [event(1, 'user_message'), done]
    if (kind === 'chat') s.input.value = 'hello'
    const sending = kind === 'panel' ? s.send('hello') : s.send()
    socket().emit(event(1, 'user_message'))
    socket().emit(done)
    result.resolve({ usage: { input_tokens: 12, output_tokens: 4 } })
    await sending
    assert.equal(s.usage.value.input, 12)
    assert.equal(s.usage.value.output, 4)
    assert.equal(s.events.value.length, 2)
    assert.equal(s.busy.value, false)
  })

  test(`${kind}: old REST failure cannot change the newly selected session`, async (t) => {
    const { state: s } = setup(t, kind)
    await load(s, 'a')
    const request = deferred()
    api.prompt = () => request.promise
    if (kind === 'chat') s.input.value = 'hello'
    const sending = kind === 'panel' ? s.send('hello') : s.send()
    await load(s, 'b')
    request.reject(new Error('old error'))
    await sending
    assert.equal(s.banner?.value || '', '')
    assert.equal(s.busy.value, false)
  })

  test(`${kind}: runtime metadata restores busy state and switching clears thinking`, async (t) => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a')
    socket().emit(event(0, 'session_meta', { busy: true }))
    assert.equal(s.busy.value, true)
    socket().emit(event(0, 'thinking_delta', { text: 'old thought' }))
    await load(s, 'b')
    socket().emit(event(3, 'assistant_message', { text: 'new answer' }))
    assert.equal(s.events.value[0].payload.thinking, undefined)
    assert.equal(s.thinking.value, '')
  })

  test(`${kind}: rewind report with a lower seq replaces stale history`, async (t) => {
    const { state: s, socket } = setup(t, kind)
    api.events = async () => [event(9, 'assistant_message', { text: 'removed' }), event(10, 'done', { usage: { input_tokens: 20 } })]
    await load(s, 'a')
    const report = event(3, 'assistant_message', { text: 'rewound', rewind: true })
    api.events = async () => [event(1, 'user_message'), event(2, 'done', { usage: { input_tokens: 5 } }), report]
    socket().emit(report)
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    assert.deepEqual(s.events.value.map((e) => e.seq), [1, 2, 3])
    assert.equal(s.usage.value.input, 5)
    assert.equal(s.events.value.at(-1).payload.text, 'rewound')
  })

  test(`${kind}: checkpoint and breaker usage count once alongside final done`, async (t) => {
    const { state: s, socket } = setup(t, kind)
    await load(s, 'a')
    const checkpoint = event(2, 'error', { code: 'segment_boundary', quiet: true, usage: { input_tokens: 10, output_tokens: 2, cached_tokens: 3 } })
    const done = event(4, 'done', { usage: { input_tokens: 20, output_tokens: 4, cached_tokens: 5 } })
    socket().emit(event(1, 'user_message'))
    socket().emit(checkpoint)
    socket().emit(checkpoint)
    assert.equal(s.busy.value, true)
    socket().emit(event(3, 'user_message', { auto: true }))
    socket().emit(done)
    socket().emit(done)
    socket().emit(event(5, 'error', { code: 'repeated_failure', usage: { input_tokens: 7, output_tokens: 1, cached_tokens: 2 } }))
    assert.deepEqual({ ...s.usage.value }, { input: 37, output: 7, cached: 10 })
  })
}

test('chat: create failure is shown without an unhandled rejection', async (t) => {
  const { state: s } = setup(t, 'chat')
  api.createSession = async () => { throw new Error('create failed') }
  await s.newSessionIn('workspace')
  assert.equal(s.banner.value, 'create failed')
  assert.equal(s.current.value, null)
})

test('panel: cancelled and failed usage reconciliation adds only unpublished delta, with replay deduplication', async (t) => {
  const { state: s, socket } = setup(t, 'panel')
  await s.load('a')
  socket().emit(event(1, 'user_message'))
  socket().emit(event(2, 'error', { quiet: true, code: 'segment_boundary', usage: { input_tokens: 10, output_tokens: 2, cached_tokens: 8 } }))
  socket().emit(event(3, 'error', { message: 'budget reached' }))
  const settled = event(4, 'session_meta', { usage_reconciliation: {
    prompt_id: 'p1', outcome: 'error',
    model_responses: 3, duration_seconds: 2.4,
    usage: { input_tokens: 30, output_tokens: 5, cached_tokens: 26 },
    delta: { input_tokens: 20, output_tokens: 3, cached_tokens: 18 },
  } })
  socket().emit(settled)
  socket().emit(settled)
  assert.deepEqual({ ...s.usage.value }, { input: 30, output: 5, cached: 26 })
  assert.equal(s.lastExecutionUsage.value.model_responses, 3)
  assert.equal(s.lastExecutionUsage.value.duration_seconds, 2.4)
  const done = event(5, 'done', { usage: { input_tokens: 7, output_tokens: 1, cached_tokens: 4 } })
  const zero = event(6, 'session_meta', { usage_reconciliation: { prompt_id: 'p2', outcome: 'completed', usage: done.payload.usage, delta: {} } })
  socket().emit(done); socket().emit(zero)
  assert.deepEqual({ ...s.usage.value }, { input: 37, output: 6, cached: 30 })
  api.events = async () => [event(1, 'user_message'), event(2, 'error', { usage: { input_tokens: 10, output_tokens: 2, cached_tokens: 8 } }), event(3, 'error'), settled, done, zero]
  await s.load('a')
  assert.deepEqual({ ...s.usage.value }, { input: 37, output: 6, cached: 30 })
  socket().emit(settled)
  assert.deepEqual({ ...s.usage.value }, { input: 37, output: 6, cached: 30 })
  await s.load('b')
  // This fixture replays the same totals for b; a new seq only belongs to b.
  socket().emit(event(7, 'session_meta', { usage_reconciliation: { prompt_id: 'p3', outcome: 'cancelled', delta: { input_tokens: 3, output_tokens: 1, cached_tokens: 2 } } }))
  assert.deepEqual({ ...s.usage.value }, { input: 40, output: 7, cached: 32 })
})

test('chat: rebuilding the timeline resets usage and failed submission preserves text and attachments', async (t) => {
  const { state: s } = setup(t, 'chat')
  const history = [event(1, 'user_message'), event(2, 'done', { usage: { input_tokens: 8, output_tokens: 3 } })]
  api.events = async () => history
  await s.selectSession('a')
  await s.rebuildTimeline()
  await s.rebuildTimeline()
  assert.equal(s.usage.value.input, 8)
  assert.equal(s.usage.value.output, 3)
  s.input.value = 'retry me'
  s.pendingFiles.value = [{ name: 'report.pdf', path: '/report.pdf', size: 100 }]
  api.prompt = async () => { throw new Error('network failure') }
  await s.send()
  assert.equal(s.input.value, 'retry me')
  assert.equal(s.pendingFiles.value[0].name, 'report.pdf')
})

test('chat: a stale image preview is revoked and cannot reopen after being closed', async (t) => {
  const { state: s } = setup(t, 'chat')
  await s.selectSession('a')
  const pending = deferred(), revoked = []
  const revoke = URL.revokeObjectURL
  URL.revokeObjectURL = (url) => revoked.push(url)
  t.after(() => { URL.revokeObjectURL = revoke })
  api.artifactBlobUrl = () => pending.promise
  const opening = s.openArtifact('chart.png')
  s.closePreview()
  pending.resolve('blob:old')
  await opening
  assert.equal(s.preview.value, null)
  assert.deepEqual(revoked, ['blob:old'])
})

const panelPresentation = state => sessionPresentation(state.timeline.value, {
  busy: state.busy.value, phase: state.phase.value, banner: state.banner.value, submissionError: state.submissionError.value,
  recoveredThroughSeq: state.recoveredThroughSeq.value, bannerDismissed: state.bannerDismissed.value,
})

test('panel: starts the socket immediately and merges REST history before higher live sequences without gaps', async t => {
  const { state: s, socket } = setup(t)
  const history = deferred()
  api.events = () => history.promise
  const loading = s.load('a')
  assert.equal(FakeSocket.sockets.length, 1)
  assert.equal(s.streamState.value, 'connecting')
  assert.equal(s.historyLoading.value, true)
  socket().emit(event(3, 'user_message', { text: 'current request' }))
  socket().emit(event(4, 'assistant_message', { text: 'live answer' }))
  const done = event(2, 'done', { usage: { input_tokens: 10, output_tokens: 4 } })
  history.resolve([event(1, 'user_message', { text: 'earlier request' }), done, event(3, 'user_message', { text: 'current request' })])
  await loading
  assert.deepEqual(s.timeline.value.map(ev => ev.seq), [1, 2, 3, 4])
  assert.equal(s.timeline.value[0].payload.text, 'earlier request')
  assert.equal(s.usage.value.input, 10)
  assert.equal(s.usage.value.output, 4)
  assert.equal(s.historyLoading.value, false)
  socket().emit(done)
  assert.equal(s.usage.value.input, 10)
})

test('panel: a completed stream replay displays history and cancels a stalled REST read', async t => {
  const { state: s, socket } = setup(t)
  let signal, checkpointCalls = 0
  api.events = (id, after, options) => new Promise((resolve, reject) => {
    signal = options.signal
    signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true })
  })
  api.checkpoints = async () => { checkpointCalls++; return { busy: false } }
  const loading = s.load('a')
  socket().open()
  socket().emit(event(1, 'user_message', { text: 'request' }))
  socket().emit(event(2, 'done', { text: 'finished', usage: { input_tokens: 3 } }))
  socket().emit(event(0, 'session_meta', { model: 'fixture', busy: false, phase: '' }))
  await loading
  assert.equal(signal.aborted, true)
  assert.deepEqual(s.timeline.value.map(ev => ev.seq), [1, 2])
  assert.equal(s.historyLoading.value, false)
  assert.equal(s.historyError.value, '')
  assert.equal(s.modelName.value, 'fixture')
  assert.equal(s.busy.value, false)
  assert.equal(s.usage.value.input, 3)
  assert.equal(checkpointCalls, 0, 'the authoritative stream snapshot avoids a redundant checkpoint lookup')
})

test('panel: switching nodes aborts the obsolete history request instead of keeping it queued', async t => {
  const { state: s } = setup(t)
  let signal
  api.events = (id, after, options) => id === 'a' ? new Promise((resolve, reject) => {
    signal = options.signal
    signal.addEventListener('abort', () => reject(new Error('old read aborted')), { once: true })
  }) : Promise.resolve([event(1, 'assistant_message', { text: 'new node' })])
  const old = s.load('a')
  const oldSocket = FakeSocket.sockets[0]
  await s.load('b')
  await old
  assert.equal(signal.aborted, true)
  assert.equal(oldSocket.readyState, 3)
  assert.equal(s.sessionId.value, 'b')
  assert.equal(s.timeline.value[0].payload.text, 'new node')
  assert.equal(s.historyError.value, '')
})

test('panel: failed history can be retried explicitly or recovered by the stream snapshot', async t => {
  const { state: s, socket } = setup(t)
  api.events = async () => { throw new Error('history temporarily unavailable') }
  await s.load('a')
  assert.equal(s.historyError.value, 'history temporarily unavailable')
  assert.equal(s.historyLoading.value, false)
  api.events = async () => [event(1, 'done', { text: 'recovered history' })]
  await s.load('a')
  assert.equal(s.historyError.value, '')
  assert.equal(s.timeline.value[0].payload.text, 'recovered history')

  api.events = async () => { throw new Error('another temporary failure') }
  await s.load('b')
  socket().emit(event(1, 'assistant_message', { text: 'stream history' }))
  socket().emit(event(0, 'session_meta', { busy: false }))
  assert.equal(s.historyError.value, '')
  assert.equal(s.timeline.value[0].payload.text, 'stream history')
})

test('panel: replayed provider error followed by successful done clears its transient banner and retains history', async t => {
  const { state: s } = setup(t)
  const message = 'provider: All connection attempts failed'
  api.events = async () => [event(2, 'error', { message }), event(3, 'done', { stop_reason: 'error' }), event(60, 'user_message'), event(64, 'done', { stop_reason: 'end_turn' })]
  await s.load('a')
  assert.equal(s.banner.value, '')
  assert.equal(s.recoveredThroughSeq.value, 64)
  assert.equal(s.submissionError.value, '')
  assert.equal(panelPresentation(s).state, 'idle')
  assert.equal(panelPresentation(s).banner, '')
  assert.equal(panelPresentation(s).historicalErrors.has(2), true)
})

test('panel: current HTTP rejection stays visible and a later recorded completion clears it', async t => {
  const { state: s, socket } = setup(t)
  const message = 'provider: All connection attempts failed'
  api.events = async () => [event(2, 'error', { message }), event(64, 'done', { stop_reason: 'end_turn' })]
  await s.load('a')
  api.prompt = async () => { throw new Error(message) }
  assert.equal(await s.send('retry'), false)
  assert.equal(s.submissionError.value, message)
  assert.equal(panelPresentation(s).state, 'error')
  assert.equal(panelPresentation(s).banner, message)
  socket().emit(event(65, 'user_message'))
  socket().emit(event(66, 'done', { stop_reason: 'end_turn' }))
  assert.equal(s.submissionError.value, '')
  assert.equal(panelPresentation(s).state, 'idle')
  assert.equal(panelPresentation(s).banner, '')
})

test('panel: delayed HTTP error after a recorded completion is not a new model failure', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  const request = deferred()
  api.prompt = () => request.promise
  const sending = s.send('request')
  socket().emit(event(1, 'user_message'))
  socket().emit(event(2, 'done', { stop_reason: 'end_turn' }))
  request.reject(new Error('provider: delayed HTTP failure'))
  await sending
  assert.equal(s.banner.value, '')
  assert.equal(s.submissionError.value, '')
  assert.equal(panelPresentation(s).state, 'idle')
})

test('panel: switching sessions clears a submission failure and ignores old REST and socket failures', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  api.prompt = async () => { throw new Error('provider: initial HTTP failure') }
  await s.send('first request')
  assert.equal(panelPresentation(s).state, 'error')
  const staleMessage = socket().onmessage
  const request = deferred()
  api.prompt = () => request.promise
  const sending = s.send('second request')
  await s.load('b')
  staleMessage({ data: JSON.stringify(event(100, 'error', { message: 'provider: old socket failure' })) })
  request.reject(new Error('provider: old REST failure'))
  await sending
  assert.equal(s.submissionError.value, '')
  assert.equal(panelPresentation(s).state, 'idle')
  assert.equal(panelPresentation(s).banner, '')
  assert.equal(s.timeline.value.length, 0)
})

test('panel: reconnect replay cannot reactivate an old model error and a new live error still surfaces', async t => {
  const schedule = globalThis.setTimeout, unschedule = globalThis.clearTimeout
  const timers = new Map()
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay !== 2000 && delay !== 30000) return schedule(callback, delay, ...args)
    const timer = { callback, delay }
    timers.set(timer, timer)
    return timer
  }
  globalThis.clearTimeout = timer => timers.delete(timer) || unschedule(timer)
  t.after(() => { globalThis.setTimeout = schedule; globalThis.clearTimeout = unschedule })
  const { state: s, socket } = setup(t)
  api.events = async () => [event(2, 'error', { message: 'provider: historical failure' }), event(64, 'done', { stop_reason: 'end_turn' })]
  await s.load('a')
  socket().open()
  socket().close()
  assert.equal(s.streamState.value, 'closed')
  assert.equal(panelPresentation(s).state, 'idle')
  const reconnect = [...timers.values()].find(timer => timer.delay === 2000)
  assert.ok(reconnect)
  timers.delete(reconnect)
  reconnect.callback()
  socket().open()
  socket().emit(event(2, 'error', { message: 'provider: historical failure' }))
  assert.equal(panelPresentation(s).state, 'idle')
  socket().emit(event(65, 'user_message'))
  socket().emit(event(66, 'error', { code: 'provider_error', message: 'current connection failed' }))
  assert.equal(panelPresentation(s).state, 'error')
  assert.equal(panelPresentation(s).historicalErrors.has(2), true)
  assert.equal(panelPresentation(s).historicalErrors.has(66), false)
  socket().emit(event(67, 'done', { stop_reason: 'end_turn' }))
  assert.equal(panelPresentation(s).state, 'idle')
  assert.equal(panelPresentation(s).banner, '')
})

test('panel: recovery survives event trimming and reconnect replay without erasing the historical error', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  const failure = event(1, 'error', { message: 'provider 错误: HTTP 402 Insufficient Balance' })
  socket().emit(failure)
  socket().emit(event(2, 'done', { stop_reason: 'end_turn' }))
  assert.equal(s.banner.value, '')
  assert.equal(s.timeline.value.some(ev => ev.seq === 1), true)
  for (let seq = 3; seq <= 405; seq++) socket().emit(event(seq, 'tool_result', { call_id: 'historical', result: 'ok' }))
  assert.equal(s.events.value.length, 400)
  assert.equal(s.timeline.value.some(ev => ev.seq === 1), false)
  socket().emit(failure)
  assert.equal(s.banner.value, '')
  assert.equal(s.submissionError.value, '')
  assert.equal(panelPresentation(s).state, 'idle')
})

test('panel: accepted request clears old errors while replayed successful frames cannot hide a newer failure', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  api.prompt = async () => { throw new Error('provider: HTTP rejected') }
  await s.send('request')
  assert.equal(s.submissionError.value, 'provider: HTTP rejected')
  socket().emit(event(1, 'user_message', { text: 'accepted retry' }))
  assert.equal(s.banner.value, '')
  assert.equal(s.submissionError.value, '')
  assert.equal(s.recoveredThroughSeq.value, 1)
  socket().emit(event(2, 'error', { message: 'provider: current failure' }))
  socket().emit(event(1, 'assistant_message', { text: 'old reply' }))
  socket().emit(event(1, 'assistant_delta', { text: 'old delta' }))
  socket().emit(event(1, 'thinking_delta', { text: 'old thinking' }))
  socket().emit(event(1, 'user_message'))
  assert.equal(s.banner.value, 'provider: current failure')
  assert.equal(s.recoveredThroughSeq.value, 1)
})

for (const type of ['assistant_delta', 'thinking_delta']) {
  test(`panel: nonempty ${type} clears the current banner and a later error reappears`, async t => {
    const { state: s, socket } = setup(t)
    await s.load('a')
    socket().emit(event(1, 'error', { message: 'provider: HTTP 402 Insufficient Balance' }))
    socket().open()
    socket().emit(event(0, 'session_meta', { busy: false, phase: '' }))
    socket().emit(event(0, 'session_meta', { busy: true, phase: 'model' }))
    socket().emit(event(0, type, { text: '' }))
    socket().emit(event(0, type, { text: '   ' }))
    socket().emit(event(0, type, { text: '', tool_calls: [{ id: 'not-response-text' }] }))
    assert.equal(s.banner.value, 'provider: HTTP 402 Insufficient Balance', 'connection and empty output do not prove recovery')
    assert.equal(s.recoveredThroughSeq.value, 0)
    socket().emit(event(0, type, { text: 'model response' }))
    assert.equal(s.banner.value, '')
    assert.equal(s.recoveredThroughSeq.value, 1)
    socket().emit(event(2, 'error', { code: 'provider_error', message: 'provider: new failure' }))
    assert.equal(s.banner.value, 'provider: new failure')
    assert.equal(s.busy.value, false)
    assert.equal(panelPresentation(s).state, 'error')
  })
}

test('panel: empty assistant records and failed or cancelled completions do not clear a current failure', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  const message = 'provider: HTTP 402 Insufficient Balance'
  socket().emit(event(1, 'error', { message }))
  socket().emit(event(2, 'assistant_message', { text: '' }))
  socket().emit(event(3, 'assistant_message', { text: 'failure note', stop_reason: 'provider_error' }))
  for (const [index, stop_reason] of ['error', 'provider_error', 'cancelled', 'cage_budget'].entries()) {
    socket().emit(event(index + 4, 'done', { stop_reason }))
    assert.equal(s.banner.value, message)
  }
  assert.equal(s.recoveredThroughSeq.value, 0)
  socket().emit(event(8, 'assistant_message', { text: '', tool_calls: [{ id: 'read', name: 'read_file' }] }))
  assert.equal(s.banner.value, '')
  assert.equal(s.recoveredThroughSeq.value, 8)
})

test('panel: buffered old deltas cannot clear a newer persisted failure during history replay', async t => {
  const { state: s, socket } = setup(t)
  const history = deferred()
  api.events = () => history.promise
  const loading = s.load('a')
  socket().emit(event(1, 'user_message'))
  socket().emit(event(0, 'assistant_delta', { text: 'earlier model output' }))
  socket().emit(event(0, 'thinking_delta', { text: 'earlier model thinking' }))
  socket().emit(event(2, 'error', { message: 'provider: latest failure' }))
  history.resolve([])
  await loading
  assert.equal(s.banner.value, 'provider: latest failure')
  assert.equal(s.recoveredThroughSeq.value, 1)
  assert.equal(panelPresentation(s).state, 'error')
})

test('panel: delayed REST failure cannot overwrite a completed request after its done event is trimmed', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  const request = deferred()
  api.prompt = () => request.promise
  const sending = s.send('request')
  socket().emit(event(1, 'user_message'))
  socket().emit(event(2, 'done', { stop_reason: 'end_turn' }))
  for (let seq = 3; seq <= 405; seq++) socket().emit(event(seq, 'tool_result', { call_id: 'late', result: 'ok' }))
  assert.equal(s.timeline.value.some(ev => ev.type === 'done'), false)
  request.reject(new Error('provider: delayed HTTP failure'))
  assert.equal(await sending, true)
  assert.equal(s.banner.value, '')
  assert.equal(s.submissionError.value, '')
  assert.equal(s.busy.value, false)
})

test('panel: delayed REST failure preserves a live model response and any newer real stream failure', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  let request = deferred()
  api.prompt = () => request.promise
  let sending = s.send('request')
  socket().emit(event(1, 'user_message'))
  socket().emit(event(0, 'thinking_delta', { text: 'working response' }))
  request.reject(new Error('provider: stale HTTP failure'))
  assert.equal(await sending, true)
  assert.equal(s.banner.value, '')
  assert.equal(s.submissionError.value, '')
  assert.equal(s.busy.value, true)

  socket().emit(event(2, 'done'))
  request = deferred()
  sending = s.send('second request')
  socket().emit(event(3, 'user_message'))
  socket().emit(event(0, 'assistant_delta', { text: 'valid response' }))
  socket().emit(event(4, 'error', { message: 'provider: actual new failure' }))
  request.reject(new Error('provider: delayed transport failure'))
  await sending
  assert.equal(s.banner.value, 'provider: actual new failure')
  assert.equal(s.submissionError.value, '')
  assert.equal(s.busy.value, false)
})

test('panel: failed done retains its current streamed failure when the REST rejection arrives later', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  const request = deferred()
  api.prompt = () => request.promise
  const sending = s.send('request')
  socket().emit(event(1, 'user_message'))
  socket().emit(event(2, 'error', { message: 'provider: actual failure' }))
  socket().emit(event(3, 'done', { stop_reason: 'error' }))
  request.reject(new Error('provider: delayed REST failure'))
  await sending
  assert.equal(s.banner.value, 'provider: actual failure')
  assert.equal(s.recoveredThroughSeq.value, 1)
  assert.equal(panelPresentation(s).state, 'error')
})

test('panel: HTTP busy rejection remains actionable and dismissing a banner preserves failure truth', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  api.prompt = async () => { throw Object.assign(new Error('上一个请求正在执行中'), { status: 409 }) }
  assert.equal(await s.send('request'), false)
  assert.equal(s.busy.value, true)
  assert.equal(s.submissionError.value, '')
  assert.equal(s.banner.value, '上一个请求正在执行中')
  socket().emit(event(1, 'error', { message: 'provider: current failure' }))
  s.dismissBanner()
  assert.equal(s.banner.value, '')
  assert.equal(s.bannerDismissed.value, true)
  assert.equal(s.recoveredThroughSeq.value, 0)
  assert.equal(s.timeline.value[0].payload.message, 'provider: current failure')
  assert.equal(panelPresentation(s).state, 'error')
  socket().emit(event(2, 'error', { message: 'provider: next failure' }))
  assert.equal(s.bannerDismissed.value, false)
  assert.equal(s.banner.value, 'provider: next failure')
})

test('panel: dismissing a submission banner keeps its real failure state until a new request is accepted', async t => {
  const { state: s, socket } = setup(t)
  await s.load('a')
  api.prompt = async () => { throw new Error('provider: submission failed') }
  await s.send('request')
  s.dismissBanner()
  assert.equal(s.banner.value, '')
  assert.equal(s.bannerDismissed.value, true)
  assert.equal(s.submissionError.value, 'provider: submission failed')
  assert.equal(s.recoveredThroughSeq.value, 0)
  assert.equal(panelPresentation(s).state, 'error')
  socket().emit(event(1, 'user_message'))
  assert.equal(s.bannerDismissed.value, false)
  assert.equal(s.submissionError.value, '')
})


test('chat: management and destructive actions wait for shared dialog confirmation', async t => {
  const { state: s, dialogs } = setup(t, 'chat')
  const calls = []
  for (const key of ['createWorkspace', 'deleteWorkspace', 'moveSessionFolder', 'rewind', 'answerQuestion']) {
    api[key] = async (...args) => { calls.push({ key, args }); return {} }
  }
  globalThis.fetch = async () => { throw new Error('Unexpected session mutation before confirmation') }
  s.sessions.value = [{ id: 'a', title: 'Project notes', folder: 'Research' }]
  s.workspaces.value = [{ name: 'Research', count: 1 }]
  await s.selectSession('a')
  s.pushEvent(event(1, 'question_request', { question_id: 'q1', questions: [{ id: 'one', question: 'Choose?', options: ['A'] }] }))
  await nextTick()

  await s.createWorkspace()
  await s.deleteWorkspace('Research')
  await s.moveSession(s.sessions.value[0])
  await s.renameSession(s.sessions.value[0])
  await s.archiveSession(s.sessions.value[0])
  await s.deleteSession(s.sessions.value[0])
  await s.rewindTo({ id: 'checkpoint-a', kind: 'prompt' })
  await s.dismissQuestions()

  assert.equal(dialogs.length, 8)
  assert.deepEqual(calls, [])
  assert.equal(s.current.value.id, 'a')
  assert.equal(s.sessions.value.length, 1)
  assert.equal(dialogs[0].input.required, true)
  assert.equal(dialogs[1].tone, 'danger')
  assert.equal(dialogs[1].details, 'Research')
  assert.deepEqual(dialogs[2].input.suggestions, ['Research'])
  assert.equal(dialogs[2].input.required, undefined)
  assert.equal(dialogs[5].confirmText, '删除会话')
})

test('chat: workspace dialog updates groups and move uses its captured session target', async t => {
  const { state: s, dialogs } = setup(t, 'chat')
  api.createWorkspace = async name => ({ name, count: 0 })
  api.listWorkspaces = async () => []
  await s.createWorkspace()
  await dialogs.at(-1).onConfirm('Research')
  assert.equal(s.workspaces.value[0].name, 'Research')
  assert.equal(s.collapsedGroups.value.Research, false)

  const source = { id: 'a', title: 'Original', folder: 'Research' }
  s.sessions.value = [source, { id: 'b', title: 'Other', folder: '' }]
  await s.selectSession('b')
  const moves = []
  api.moveSessionFolder = async (id, folder) => { moves.push({ id, folder }); return { id, folder } }
  await s.moveSession(source)
  source.id = 'changed-after-dialog-open'
  await dialogs.at(-1).onConfirm('')
  assert.deepEqual(moves, [{ id: 'a', folder: '' }])
  assert.equal(s.current.value.id, 'b')
})

test('chat: rename snapshots its id and preserves a different active session', async t => {
  const { state: s, dialogs } = setup(t, 'chat')
  const source = { id: 'a', title: 'Original' }
  s.sessions.value = [source, { id: 'b', title: 'Other' }]
  await s.selectSession('b')
  const requests = []
  globalThis.fetch = async (path, options) => {
    requests.push({ path, body: JSON.parse(options.body) })
    return { ok: true, json: async () => ({ id: 'a', title: 'Renamed' }) }
  }
  api.listSessions = async () => [{ id: 'a', title: 'Renamed' }, { id: 'b', title: 'Other' }]
  await s.renameSession(source)
  source.id = 'changed-after-dialog-open'
  await dialogs.at(-1).onConfirm('Renamed')
  assert.deepEqual(requests, [{ path: '/api/v1/sessions/a/rename', body: { title: 'Renamed' } }])
  assert.equal(s.current.value.id, 'b')
  assert.equal(s.current.value.title, 'Other')
  assert.equal(s.sessions.value[0].title, 'Renamed')
})

for (const action of ['archiveSession', 'deleteSession']) {
  test(`chat: ${action} retains errors for retry and does not repeat a successful write on refresh failure`, async t => {
    const { state: s, dialogs } = setup(t, 'chat')
    const source = { id: 'a', title: 'Original', status: 'active' }
    s.sessions.value = [source]
    await s.selectSession('a')
    let attempts = 0
    const paths = []
    globalThis.fetch = async path => {
      paths.push(path)
      attempts++
      return attempts === 1
        ? { ok: false, status: 503, json: async () => ({ detail: 'Temporarily unavailable' }) }
        : { ok: true, json: async () => ({ ok: true }) }
    }
    api.listSessions = async () => { throw new Error('List unavailable') }
    await s[action](source)
    const confirmation = dialogs.at(-1)
    await assert.rejects(confirmation.onConfirm(), /Temporarily unavailable/)
    assert.equal(s.current.value.id, 'a')
    assert.equal(s.sessions.value[0].status, 'active')
    await confirmation.onConfirm()
    assert.equal(attempts, 2)
    assert.ok(paths.every(path => path === (action === 'archiveSession' ? '/api/v1/sessions/a/archive' : '/api/v1/sessions/a')))
    assert.equal(s.current.value, null)
    assert.match(s.banner.value, /操作已完成.*列表刷新失败/)
    if (action === 'archiveSession') assert.equal(s.sessions.value[0].status, 'archived')
    else assert.equal(s.sessions.value.length, 0)
  })
}

test('chat: rewind confirmation refuses a switched session and late completion cannot rebuild its replacement', async t => {
  const { state: s, dialogs } = setup(t, 'chat')
  const rewinds = []
  const request = deferred()
  api.rewind = (id, checkpoint) => { rewinds.push({ id, checkpoint }); return request.promise }
  await s.selectSession('a')
  await s.rewindTo({ id: 'checkpoint-a', kind: 'prompt' })
  await s.selectSession('b')
  await assert.rejects(dialogs.at(-1).onConfirm(), /当前会话已切换/)
  assert.deepEqual(rewinds, [])

  await s.selectSession('a')
  await s.rewindTo({ id: 'checkpoint-a', kind: 'prompt' })
  const confirmation = dialogs.at(-1).onConfirm()
  assert.equal(s.rewindBusy.value, true)
  await s.selectSession('b')
  api.events = async () => { throw new Error('Must not rebuild the replacement session') }
  request.resolve({ failed: [] })
  await confirmation
  assert.deepEqual(rewinds, [{ id: 'a', checkpoint: 'checkpoint-a' }])
  assert.equal(s.current.value.id, 'b')
  assert.equal(s.rewindBusy.value, false)
})

test('chat: dismiss confirmation guards the question group and preserves a replacement group busy state', async t => {
  const { state: s, dialogs } = setup(t, 'chat')
  const question = id => ({ question_id: id, questions: [{ id: 'one', question: 'Choose?', options: ['A'] }] })
  const answers = []
  const request = deferred()
  api.answerQuestion = (id, questionId, value) => { answers.push({ id, questionId, value }); return request.promise }
  await s.selectSession('a')
  s.pushEvent(event(1, 'question_request', question('old-group')))
  await nextTick()
  await s.dismissQuestions()
  s.pushEvent(event(2, 'question_request', question('new-group')))
  await nextTick()
  await assert.rejects(dialogs.at(-1).onConfirm(), /这组提问已更新/)
  assert.deepEqual(answers, [])

  await s.dismissQuestions()
  const confirmation = dialogs.at(-1).onConfirm()
  assert.equal(s.qBusy.value, true)
  await s.selectSession('b')
  s.pushEvent(event(1, 'question_request', question('replacement')))
  await nextTick()
  s.qBusy.value = true
  request.resolve({ ok: true })
  await confirmation
  assert.deepEqual(answers, [{ id: 'a', questionId: 'new-group', value: { dismiss: true } }])
  assert.equal(s.question.value.id, 'replacement')
  assert.equal(s.qBusy.value, true)
})
