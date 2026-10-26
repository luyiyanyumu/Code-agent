// 会话流式状态（M18 抽取）：WS 事件 → 消息时间线，附带审批与用量。
//
// 与独立 chat-app/src/views/ChatView.vue 的 pushEvent/connect/send/decide 同源：那边的逻辑
// 已经在真机上踩过几个坑（busy 错误码、session_meta 首帧、审批弹窗刷新兜底、
// assistant_delta 不落盘只能实时累积）。这里把这些结论搬成独立模块，供代码
// 模式的面板使用；独立对话应用维护自己的界面状态。
// 修改会话协议或事件语义时，应同时验证两个前端。
//
// 与 ChatView 的差异：不处理滚动、不统计首字响应时间、附件能力收敛到最小。

import { computed, onScopeDispose, ref } from 'vue'
import { api, openStream } from '../api.js'
import { createSessionStream } from '../lib/sessionStream.js'
import { attachThinking, pushThinking } from '../lib/thinkingSegments.js'
import { finishQuestionGroup, questionPending, questionTime, reconcileQuestionSnapshot, trimQuestionHistory, updateQuestionEvent } from '../lib/questions.js'
import { explanationForTool, toolArguments } from '../lib/toolExplanations.js'
import { internalMessagePresentation, isSuccessfulCompletion } from '../lib/sessionPresentation.js'

const MAX_EVENTS = 400

const hasResponseText = payload => typeof payload?.text === 'string' && !!payload.text.trim()
const hasModelResponse = payload => hasResponseText(payload)
  || Array.isArray(payload?.tool_calls) && payload.tool_calls.length > 0
const successfulResponse = payload => isSuccessfulCompletion(payload)
  || ['end_turn', 'stop', 'tool_use', undefined, null, ''].includes(payload?.stop_reason) && Array.isArray(payload?.tool_calls) && payload.tool_calls.length > 0

export function useAgentSession() {
  const sessionId = ref('')
  const events = ref([])
  const busy = ref(false)
  const phase = ref('')
  const banner = ref('')
  const bannerDismissed = ref(false)
  // HTTP 提交失败没有落盘事件；单独记录，避免与历史回放留下的 banner 混淆。
  const submissionError = ref('')
  // Keep recovery independent of the bounded timeline: a recovered error must
  // not become current again when older events are trimmed or replayed.
  const recoveredThroughSeq = ref(0)
  const approval = ref(null)
  const approvalSubmitting = ref('')
  const approvalError = ref('')
  const questions = ref([])
  const pendingQuestions = computed(() => questions.value.filter(group => questionPending(group)))
  const questionSubmitting = ref('')
  const questionError = ref('')
  const streaming = ref('')
  const thinking = ref('')
  const modelName = ref('')
  const streamState = ref('idle') // idle | connecting | open | closed
  const historyLoading = ref(false)
  const historyError = ref('')
  const usage = ref({ input: 0, output: 0, cached: 0 })
  const lastExecutionUsage = ref(null)
  const teamRevision = ref(0)
  const teamChange = ref(null)
  const lastStopReason = ref('end_turn')
  //: 联网开关（M19）。默认关，用户显式打开；`webInfo` 是后端的能力探测结果
  //: （没有检索 key 时开关点不开，并给出"怎么配"）。
  const web = ref(false)
  const webInfo = ref(null)

  let lastSeq = 0
  let loadToken = 0
  let sendToken = 0
  let approvalVersion = 0
  let runtimeMetaReceived = false
  let replaying = false
  let refreshApprovalOnMeta = false
  let approvalSnapshotReceived = false
  let questionVersion = 0
  let questionRefresh = 0
  let questionExpiryTimer = null
  let refreshQuestionsOnMeta = false
  let historyController = null
  let replayFrames = []
  let pendingSend = null
  const thinkSink = { buffer: '', startedTs: 0 }
  const connection = createSessionStream({
    open: openStream,
    afterSeq: () => lastSeq,
    onEvent: receiveStreamEvent,
    onState: (state) => { streamState.value = state; if (state === 'open') { refreshApprovalOnMeta = true; refreshQuestionsOnMeta = true } },
    onError: setBanner,
  })
  const isCurrent = (id, token) => sessionId.value === id && loadToken === token

  /** 已落轨迹的事件（seq>0）；流式增量不在其中。 */
  const timeline = computed(() => events.value.filter((e) => e.seq > 0))

  function receiveStreamEvent(ev) {
    const snapshot = ev.type === 'session_meta' && typeof ev.payload?.busy === 'boolean'
    if (historyLoading.value) {
      // The stream replays history before its first runtime snapshot. Keep its
      // frames ordered until either that replay or the REST history completes.
      if (!snapshot) { replayFrames.push(ev); return }
      finishHistory([])
      historyController?.abort()
    }
    if (snapshot) historyError.value = ''
    pushEvent(ev)
  }

  function finishHistory(history) {
    const buffered = replayFrames
    replayFrames = []
    const persisted = new Map()
    for (const ev of [...(history || []), ...buffered]) if (ev.seq > 0) persisted.set(ev.seq, ev)
    replaying = true
    try {
      for (const ev of [...persisted.values()].sort((a, b) => a.seq - b.seq)) pushEvent(ev)
      for (const ev of buffered) if (!(ev.seq > 0)) pushEvent(ev)
    } finally { replaying = false; historyLoading.value = false }
  }

  function recoverErrorBanner() {
    recoveredThroughSeq.value = Math.max(recoveredThroughSeq.value, lastSeq)
    setBanner('')
    submissionError.value = ''
  }

  function observeSubmission(ev, terminal = false) {
    // A delayed REST failure must not overwrite progress already delivered by
    // the same request's stream, even after that progress leaves MAX_EVENTS.
    if (!pendingSend || replaying || (ev.seq > 0 && ev.seq <= pendingSend.afterSeq)) return
    if (terminal) pendingSend.terminalObserved = true
    else pendingSend.responseObserved = true
  }

  function setBanner(message) {
    banner.value = message
    bannerDismissed.value = false
  }

  function dismissBanner() { banner.value = ''; bannerDismissed.value = true }

  function pushEvent(ev) {
    if (ev.type === 'session_meta') {
      if (ev.payload?.team_changed && !replaying) {
        teamChange.value = ev.payload.team_changed
        teamRevision.value += 1
      }
      if (ev.payload?.model) modelName.value = ev.payload.model
      if (typeof ev.payload?.busy === 'boolean') {
        runtimeMetaReceived = true
        busy.value = ev.payload.busy
      }
      if (typeof ev.payload?.phase === 'string') phase.value = ev.payload.phase
      if (!busy.value) phase.value = ''
      if (Array.isArray(ev.payload?.pending_questions)) {
        questionVersion += 1
        questions.value = trimQuestionHistory(reconcileQuestionSnapshot(questions.value, ev.payload.pending_questions, sessionId.value))
        scheduleQuestionExpiry()
      }
      if (refreshQuestionsOnMeta && !replaying) {
        refreshQuestionsOnMeta = false
        void restorePendingQuestions()
      }
      if (pendingQuestions.value.length) { busy.value = true; phase.value = 'waiting_question' }
      // The live connection snapshot already carries the actionable waiter.
      // Its explicit null is equally authoritative; older servers omit the
      // field and retain the REST fallback below. Never revive transcript text.
      const pendingApproval = ev.payload?.pending_approval
      const hasApprovalSnapshot = !replaying && ev.seq === 0 && Object.hasOwn(ev.payload || {}, 'pending_approval')
        && (pendingApproval === null || (pendingApproval && typeof pendingApproval === 'object' && !Array.isArray(pendingApproval)
          && typeof pendingApproval.approval_id === 'string' && pendingApproval.approval_id.trim()))
      if (hasApprovalSnapshot) {
        approvalVersion += 1
        approvalSnapshotReceived = true
        refreshApprovalOnMeta = false
        approvalError.value = ''
        if (pendingApproval) {
          if (approval.value?.approval_id !== pendingApproval.approval_id) approvalSubmitting.value = ''
          approval.value = pendingApproval
          busy.value = true
          phase.value = 'waiting_approval'
        } else clearApproval()
      } else if (refreshApprovalOnMeta && !replaying && ev.seq === 0) {
        refreshApprovalOnMeta = false
        void restorePendingApproval()
      }
      // 联网开关（M19）：本地没有显式覆盖时，采用后端会话行里的状态
      if (typeof ev.payload?.web_enabled === 'boolean' && localWebOverride(sessionId.value) === null) {
        web.value = !!ev.payload.web_enabled && webInfo.value?.available !== false
      }
      // Completion receipts are persisted metadata, unlike transient runtime
      // snapshots. Keep their sequence so reconnects do not duplicate notices.
      if (!(ev.seq > 0 && (ev.payload?.team_notice || ev.payload?.usage_reconciliation))) return
    }
    if (ev.type === 'thinking_delta') {
      if (ev.seq > 0 && ev.seq <= lastSeq) return
      if (!replaying && hasResponseText(ev.payload)) {
        recoverErrorBanner()
        observeSubmission(ev)
      }
      pushThinking(thinkSink, ev.payload?.text || '', ev.ts)
      thinking.value = thinkSink.buffer
      return
    }
    if (ev.type === 'assistant_delta') {
      if (ev.seq > 0 && ev.seq <= lastSeq) return
      if (!replaying && hasResponseText(ev.payload)) {
        recoverErrorBanner()
        observeSubmission(ev)
      }
      streaming.value += ev.payload?.text || ''
      return
    }
    // 回退会重置服务端 seq；报告必须先于普通去重处理，否则会被当旧帧丢掉。
    if (ev.payload?.rewind && sessionId.value && !replaying) {
      load(sessionId.value)
      return
    }
    if (ev.seq && ev.seq <= lastSeq) return
    if (ev.seq) lastSeq = ev.seq
    if (ev.type === 'question_request' || ev.type === 'question_answered') {
      questionVersion += 1
      questions.value = trimQuestionHistory(updateQuestionEvent(questions.value, ev, sessionId.value))
      scheduleQuestionExpiry()
      if (pendingQuestions.value.length) { busy.value = true; phase.value = 'waiting_question' }
      else if (phase.value === 'waiting_question') phase.value = busy.value ? 'tools' : ''
    }
    attachThinking(ev, thinkSink)
    thinking.value = thinkSink.buffer
    events.value.push(ev)
    if (events.value.length > MAX_EVENTS) {
      events.value.splice(0, events.value.length - MAX_EVENTS)
    }
    // 检查点与熔断以 error 收口，也带本段用量；与 done 一样只在 seq 去重后累计。
    const u = ev.type === 'session_meta'
      ? ev.payload?.usage_reconciliation?.delta
      : (ev.type === 'done' || ev.type === 'error') ? ev.payload?.usage : null
    if (ev.seq > 0 && u) {
      // Reconciliation carries the unpublished difference, not another copy
      // of the entire prompt. Existing terminal events retain their semantics.
      const count = value => Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : 0
      usage.value.input += count(u.input_tokens)
      usage.value.output += count(u.output_tokens)
      usage.value.cached += count(u.cached_tokens)
    }
    if (ev.seq > 0 && ev.type === 'session_meta' && ev.payload?.usage_reconciliation) {
      lastExecutionUsage.value = ev.payload.usage_reconciliation
    }

    if (ev.type === 'approval_request' && ev.payload?.approval_id) {
      approvalVersion += 1
      approvalError.value = ''
      if (approval.value?.approval_id !== ev.payload.approval_id) approvalSubmitting.value = ''
      approval.value = ev.payload
      busy.value = true
      phase.value = 'waiting_approval'
    }
    if (ev.type === 'tool_use' && busy.value) phase.value = approval.value ? 'waiting_approval' : pendingQuestions.value.length ? 'waiting_question' : 'tools'
    if (ev.type === 'approval_decided' && ev.payload?.approval_id) {
      approvalVersion += 1
      if (approval.value?.approval_id === ev.payload.approval_id) clearApproval()
    }
    if (ev.type === 'user_message') {
      const internal = internalMessagePresentation(ev)
      if (!internal) { recoverErrorBanner(); phase.value = '' }
      busy.value = true
      if (internal) {
        // 系统自动续跑：done 刚把 busy 清掉，这里要恢复"仍在执行"的真实状态
        busy.value = true
        streaming.value = ''
      }
    }
    if (ev.type === 'error') {
      if (ev.payload?.code === 'busy') {
        // 后端拒收第二条消息说明任务其实还在跑——不能把 busy 清掉（状态会脱节）
        setBanner(ev.payload?.message || '会话正在执行中')
        busy.value = true
        return
      }
      if (ev.payload?.quiet) return
      observeSubmission(ev, true)
      setBanner(ev.payload?.message || '发生错误')
      busy.value = false
      phase.value = ''
      endPendingQuestions('inactive')
    }
    if (ev.type === 'done' || ev.type === 'assistant_message') {
      if (ev.type === 'done' ? isSuccessfulCompletion(ev.payload) : successfulResponse(ev.payload) && hasModelResponse(ev.payload)) {
        recoverErrorBanner()
        observeSubmission(ev)
      }
      streaming.value = ''
      if (ev.type === 'done') {
        observeSubmission(ev, true)
        submissionError.value = ''
        approvalVersion += 1
        clearApproval()
        busy.value = false
        phase.value = ''
        endPendingQuestions('inactive')
        lastStopReason.value = ev.payload?.stop_reason || 'end_turn'
      }
    }
  }

  function connect(id) {
    connection.connect(id)
  }

  function closeStream() {
    loadToken += 1
    historyController?.abort()
    historyController = null
    replayFrames = []
    historyLoading.value = false
    connection.close()
    if (questionExpiryTimer !== null) { clearTimeout(questionExpiryTimer); questionExpiryTimer = null }
  }
  onScopeDispose(closeStream, true)

  function clearApproval() {
    approval.value = null
    approvalSubmitting.value = ''
    approvalError.value = ''
    if (phase.value === 'waiting_approval') phase.value = ''
  }

  function endPendingQuestions(status) {
    questionVersion += 1
    questions.value = questions.value.map(group => group.status === 'pending' ? { ...group, status } : group)
    scheduleQuestionExpiry()
  }

  function scheduleQuestionExpiry() {
    if (questionExpiryTimer !== null) { clearTimeout(questionExpiryTimer); questionExpiryTimer = null }
    const deadlines = questions.value.filter(group => group.status === 'pending').map(group => questionTime(group.expires_at)).filter(Number.isFinite)
    if (!deadlines.length) return
    const now = Date.now()
    let expired = false
    questions.value = questions.value.map(group => {
      if (group.status !== 'pending' || !Number.isFinite(questionTime(group.expires_at)) || questionTime(group.expires_at) > now) return group
      expired = true
      return { ...group, status: 'timeout' }
    })
    if (expired) {
      questionVersion += 1
      if (!pendingQuestions.value.length && phase.value === 'waiting_question') phase.value = busy.value ? 'tools' : ''
    }
    const future = deadlines.filter(deadline => deadline > now)
    if (!future.length) return
    questionExpiryTimer = setTimeout(() => { questionExpiryTimer = null; scheduleQuestionExpiry() }, Math.min(Math.max(1, Math.min(...future) - now + 5), 2147483647))
    questionExpiryTimer?.unref?.()
  }

  async function restorePendingQuestions() {
    const id = sessionId.value, mine = loadToken, version = questionVersion, request = ++questionRefresh
    if (!id || typeof api.pendingQuestions !== 'function') return false
    try {
      const result = await api.pendingQuestions(id)
      if (!isCurrent(id, mine) || version !== questionVersion || request !== questionRefresh) return false
      if (!Array.isArray(result?.questions)) return false
      questionVersion += 1
      questions.value = trimQuestionHistory(reconcileQuestionSnapshot(questions.value, result.questions, id))
      scheduleQuestionExpiry()
      if (pendingQuestions.value.length) { busy.value = true; phase.value = approval.value ? 'waiting_approval' : 'waiting_question' }
      else if (phase.value === 'waiting_question') phase.value = busy.value ? 'tools' : ''
      return true
    } catch (cause) {
      // Older running backends already support answers. Keep their persisted
      // question cards usable until the new backend can be loaded while idle.
      if (isCurrent(id, mine) && request === questionRefresh && cause.status !== 404) questionError.value = cause.message || '无法核对问题状态，请重试'
      return false
    }
  }

  async function submitQuestions(questionId, payload) {
    const group = pendingQuestions.value.find(value => value.id === questionId)
    if (!group || questionSubmitting.value || !sessionId.value) return false
    const id = sessionId.value, mine = loadToken
    questionSubmitting.value = questionId
    questionError.value = ''
    try {
      const result = await api.answerQuestion(id, questionId, payload)
      if (!isCurrent(id, mine)) return true
      if (result?.delivered === false) throw Object.assign(new Error('这次提问已结束，请刷新问题'), { status: 409 })
      questionVersion += 1
      questions.value = finishQuestionGroup(questions.value, questionId, payload.dismiss ? 'dismissed' : 'answered', payload.answers || [])
      scheduleQuestionExpiry()
      if (!pendingQuestions.value.length && phase.value === 'waiting_question') phase.value = busy.value ? 'tools' : ''
      return true
    } catch (cause) {
      if (isCurrent(id, mine)) {
        questionError.value = cause.message || '回答未送达，请重试'
        if (cause.status === 409) {
          questionVersion += 1
          questions.value = questions.value.map(value => value.id === questionId && value.status === 'pending' ? { ...value, status: 'inactive', unavailable: true } : value)
          scheduleQuestionExpiry()
          await restorePendingQuestions()
        }
      }
      return false
    } finally { if (isCurrent(id, mine) && questionSubmitting.value === questionId) questionSubmitting.value = '' }
  }
  const answerQuestions = (id, answers) => submitQuestions(id, { answers })
  const dismissQuestions = id => submitQuestions(id, { dismiss: true })

  async function restorePendingApproval() {
    const id = sessionId.value, mine = loadToken, version = approvalVersion
    if (!id) return false
    try {
      const pending = await api.pendingApproval(id)
      if (!isCurrent(id, mine) || version !== approvalVersion) return false
      approvalVersion += 1
      approvalError.value = ''
      if (pending?.approval_id) {
        if (approval.value?.approval_id !== pending.approval_id) approvalSubmitting.value = ''
        approval.value = pending
        busy.value = true
        phase.value = 'waiting_approval'
      } else clearApproval()
      return true
    } catch (cause) {
      // Preserve an existing actionable card. The dedicated error lets the UI
      // offer a retry without confusing a network failure with an empty waiter.
      if (isCurrent(id, mine) && version === approvalVersion) {
        approvalError.value = cause?.message ? `无法核对审批状态：${cause.message}` : '无法核对审批状态，请重试'
      }
      return false
    }
  }

  /** 历史与实时连接并行；任一历史回放完成即可显示，切换时取消旧读取。 */
  async function load(id) {
    closeStream()
    const mine = loadToken
    sessionId.value = id
    events.value = []
    lastSeq = 0
    busy.value = false
    phase.value = ''
    setBanner('')
    submissionError.value = ''
    recoveredThroughSeq.value = 0
    pendingSend = null
    clearApproval()
    questions.value = []
    questionVersion += 1
    questionRefresh += 1
    questionSubmitting.value = ''
    questionError.value = ''
    refreshQuestionsOnMeta = false
    refreshApprovalOnMeta = false
    approvalSnapshotReceived = false
    streaming.value = ''
    thinking.value = ''
    thinkSink.buffer = ''
    thinkSink.startedTs = 0
    modelName.value = ''
    usage.value = { input: 0, output: 0, cached: 0 }
    lastExecutionUsage.value = null
    teamChange.value = null
    lastStopReason.value = 'end_turn'
    web.value = false
    runtimeMetaReceived = false
    historyError.value = ''
    if (!id) return
    // 联网能力 + 该会话的开关状态（M19）：一次拿齐，输入框上的开关据此渲染
    loadWebState(id, mine).catch(() => {})
    const controller = new AbortController()
    historyController = controller
    historyLoading.value = true
    connect(id)
    try {
      const history = await api.events(id, 0, { signal: controller.signal })
      if (!isCurrent(id, mine)) return
      if (historyLoading.value) finishHistory(history)
    } catch (e) {
      if (!isCurrent(id, mine)) return
      if (!controller.signal.aborted) {
        if (historyLoading.value) finishHistory([])
        historyError.value = e.message || '无法读取会话记录'
      }
    } finally {
      if (historyController === controller) historyController = null
    }
    if (!isCurrent(id, mine)) return
    const seq = lastSeq
    if (!runtimeMetaReceived) api.checkpoints(id).then((result) => {
      if (isCurrent(id, mine) && !runtimeMetaReceived && lastSeq === seq && typeof result?.busy === 'boolean') busy.value = result.busy
    }).catch(() => {})
    // 审批弹窗走的是不落盘的实时事件，刷新后必须以 approvals 表兜底：
    // 否则后端在等批准、用户却永远看不到弹窗（ChatView 上踩过）
    if (!approvalSnapshotReceived) await restorePendingApproval()
    await restorePendingQuestions()
  }

  /** 联网能力 + 会话开关（M19）。
   *
   * 开关状态优先取浏览器本地（"这个会话我要不要联网"，用户最直接的意图），
   * 本地没有就用后端会话行里的 `web_enabled`——它随 WS 的 `session_meta` 一起来，
   * 所以这里只负责拿能力探测 + 读本地覆盖。
   */
  function localWebOverride(id) {
    try {
      return window.localStorage.getItem(`code-agent:web:${id}`)
    } catch {
      return null
    }
  }

  async function loadWebState(id, token = loadToken) {
    if (!webInfo.value) {
      try {
        webInfo.value = await api.webCapability()
      } catch {
        webInfo.value = { available: false, reason: '拿不到联网能力信息' }
      }
    }
    if (!isCurrent(id, token)) return
    const local = id ? localWebOverride(id) : null
    if (local !== null) web.value = local === '1'
    if (web.value && webInfo.value?.available === false) web.value = false
  }

  /** 切换联网开关：本地记一份 + 同步到会话行（后端下次重建运行时也要是打开的）。 */
  async function setWeb(enabled) {
    const next = !!enabled
    if (next && webInfo.value?.available === false) return
    const id = sessionId.value
    const mine = loadToken
    web.value = next
    try {
      if (sessionId.value) window.localStorage.setItem(`code-agent:web:${sessionId.value}`, next ? '1' : '0')
    } catch {
      /* 隐私模式下写不了 localStorage：不影响后端那份 */
    }
    if (sessionId.value) {
      try {
        await api.setSessionWeb(sessionId.value, next)
      } catch (e) {
        if (isCurrent(id, mine)) setBanner(e.message)
      }
    }
  }

  /** 选中联网开关时给用户的提示（后端说了为什么不能用就直接显示原因）。 */
  const webHint = computed(() => {
    const info = webInfo.value
    if (!info) return '联网能力检测中…'
    if (!info.available) return info.reason || '当前不可用'
    return `检索模型 ${info.model || '—'} · 单次最多 ${info.max_results || 8} 条来源`
  })

  /** 发送一轮 prompt。codeContext（P2 起）随消息带给后端做上下文注入。 */
  async function send(text, { attachments = [], codeContext = null, web: webOverride = null } = {}) {
    const trimmed = (text || '').trim()
    if (!trimmed || !sessionId.value || busy.value) return false
    const id = sessionId.value
    const mine = loadToken
    const sending = ++sendToken
    const submittingAfterSeq = lastSeq
    const receipt = { afterSeq: submittingAfterSeq, responseObserved: false, terminalObserved: false }
    pendingSend = receipt
    const nextWeb = typeof webOverride === 'boolean' ? webOverride : web.value
    let submitted = false
    busy.value = true
    phase.value = ''
    setBanner('')
    submissionError.value = ''
    const payload = { type: 'prompt', text: trimmed }
    if (attachments.length) payload.attachments = attachments
    if (codeContext) payload.code_context = codeContext
    // 联网开关（M19）随消息发：后端据此决定本轮是否把 web_search/web_fetch 暴露给模型。
    // 每次都带（而不是只在点开关时同步一次），免得"刚点开关就发送"时后端还没收到设置。
    payload.web = nextWeb
    try {
      const ws = connection.socket
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(payload))
      } else {
        await api.prompt(id, trimmed, attachments, { web: nextWeb, codeContext })
        submitted = true
        if (!isCurrent(id, mine) || sending !== sendToken) return true
        // 用落盘事件统一更新消息与用量；重连已经收到的 done 会被 seq 去重。
        const history = await api.events(id, lastSeq)
        if (!isCurrent(id, mine) || sending !== sendToken) return true
        ;(history || []).forEach(pushEvent)
      }
      return true
    } catch (e) {
      if (isCurrent(id, mine) && sending === sendToken) {
        if (receipt.responseObserved || receipt.terminalObserved) return submitted || receipt.responseObserved
        setBanner(e.message)
        busy.value = !submitted && e.status === 409 && /正在|上一个请求|busy/i.test(e.message)
        if (!busy.value) phase.value = ''
        if (!submitted && !busy.value) submissionError.value = e.message
      }
      return submitted
    } finally { if (pendingSend === receipt) pendingSend = null }
  }

  function stop() {
    const ws = connection.socket
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setBanner('会话连接尚未恢复，请连接后再停止任务')
      return false
    }
    try {
      ws.send(JSON.stringify({ type: 'cancel' }))
    } catch (e) {
      setBanner(e.message)
      return false
    }
    busy.value = false
    phase.value = ''
    approvalVersion += 1
    clearApproval()
    endPendingQuestions('cancelled')
    streaming.value = ''
    thinking.value = ''
    thinkSink.buffer = ''
    thinkSink.startedTs = 0
    return true
  }

  async function decide(approved) {
    if (!approval.value || approvalSubmitting.value) return
    const pending = approval.value
    const id = sessionId.value
    const mine = loadToken
    approvalVersion += 1
    approvalSubmitting.value = pending.approval_id
    try {
      const result = await api.decideApproval(pending.approval_id, approved)
      if (isCurrent(id, mine) && approval.value?.approval_id === pending.approval_id) {
        approvalVersion += 1
        clearApproval()
      }
      if (isCurrent(id, mine) && result?.delivered === false) {
        setBanner('申请已结束，请重新发起操作')
        await restorePendingApproval()
      }
    } catch (e) {
      if (isCurrent(id, mine) && approval.value?.approval_id === pending.approval_id) setBanner(e.message)
    } finally {
      if (isCurrent(id, mine) && approvalSubmitting.value === pending.approval_id) approvalSubmitting.value = ''
    }
  }

  return {
    sessionId, events, timeline, busy, phase, banner, bannerDismissed, submissionError, recoveredThroughSeq, dismissBanner, approval, approvalSubmitting, approvalError, streaming, thinking,
    modelName, streamState, historyLoading, historyError, usage, lastExecutionUsage, lastStopReason, teamRevision, teamChange,
    web, webInfo, webHint, setWeb,
    questions, pendingQuestions, questionSubmitting, questionError, answerQuestions, dismissQuestions,
    refreshQuestions: restorePendingQuestions,
    load, send, stop, decide, closeStream, refreshApproval: restorePendingApproval,
  }
}

/** 工具调用/结果配对：把事件流折成"一次工具调用"卡片所需的形态。 */
export function buildToolCards(events) {
  const calls = new Map()
  const cards = []
  for (const ev of events) {
    if (ev.type === 'tool_use' && ev.payload?.call) {
      const call = ev.payload.call
      const card = { id: call.id, name: call.name, args: toolArguments(call.arguments), explanation: explanationForTool(ev.payload), result: '', state: 'running' }
      calls.set(call.id, card)
      cards.push(card)
    } else if (ev.type === 'tool_result' && ev.payload?.call_id) {
      const card = calls.get(ev.payload.call_id)
      if (card) {
        card.result = ev.payload.result || ''
        card.state = String(card.result).startsWith('[permission denied]')
          ? 'denied'
          : String(card.result).startsWith('[tool error]')
            ? 'error'
            : 'ok'
      }
    }
  }
  return cards
}

export function summarizeToolArgs(args) {
  if (!args || typeof args !== 'object') return ''
  const keys = ['command', 'path', 'query', 'url', 'pattern', 'text', 'content', 'name']
  for (const k of keys) {
    if (args[k] !== undefined && args[k] !== null && args[k] !== '') {
      const v = String(args[k])
      return v.length > 120 ? `${v.slice(0, 120)}…` : v
    }
  }
  const s = JSON.stringify(args)
  return s.length > 120 ? `${s.slice(0, 120)}…` : s
}
