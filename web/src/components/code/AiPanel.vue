<script setup>
import UiIcon from '../ui/UiIcon.vue'
import BackgroundAgentTasks from './BackgroundAgentTasks.vue'
import ConversationTabs from './ConversationTabs.vue'
import ApprovalRequest from '../ApprovalRequest.vue'
import QuestionRequest from '../QuestionRequest.vue'
import ToolExplanation from '../ToolExplanation.vue'
// Each root dialogue owns its composer; workers remain background operations.
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useAgentSession, buildToolCards, summarizeToolArgs } from '../../composables/useAgentSession.js'
import { eventTime, sessionPresentation, internalMessagePresentation, incompleteCompletion } from '../../lib/sessionPresentation.js'
import { teamNoticePresentation } from '../../lib/agentTeam.js'
import { renderMarkdown } from '../../lib/markdown.js'
import { shouldSendMessage, usePreferences } from '../../lib/preferences.js'

const { preferences } = usePreferences()
const composing = ref(false)
const sendHint = computed(() => preferences.sendKey === 'ctrl-enter' ? 'Ctrl / ⌘ + Enter 发送 · Enter 换行' : 'Enter 发送 · Shift+Enter 换行')
function onComposerKeydown(event) {
  if (!shouldSendMessage(event, preferences.sendKey, composing.value)) return
  event.preventDefault()
  send()
}

const props = defineProps({
  sessionId: { type: String, default: '' },
  fileContext: { type: Object, default: null },   // { path, start_line, end_line }
  codeContext: { type: Object, default: null },   // 结构坐标（服务端据此渲染事实块）
  pendingPrompt: { type: String, default: '' },   // 导图节点「问 AI」塞进来的问题
  writable: { type: Boolean, default: false },    // 会话是否可写（决定空态怎么说明能力）
  selectionText: { type: String, default: '' },
  team: { type: Object, default: null },
  agentId: { type: String, default: '' },
  teamLoading: { type: Boolean, default: false },
  teamError: { type: String, default: '' },
  teamActing: { type: String, default: '' },
  visible: { type: Boolean, default: true },
  projectChat: { type: Boolean, default: false },
  draftScope: { type: String, default: '' },
  conversations: { type: Array, default: () => [] },
  conversationId: { type: String, default: '' },
  conversationsEnabled: { type: Boolean, default: false },
  conversationCreating: { type: Boolean, default: false },
  conversationDeleting: { type: String, default: '' },
  conversationLoading: { type: Boolean, default: false },
  conversationError: { type: String, default: '' },
})
const emit = defineEmits(['clear-context', 'consume-prompt', 'open-review', 'review-refresh', 'team-refresh', 'agent-stop', 'sent', 'assistant-complete', 'conversation-select', 'conversation-create', 'conversation-delete', 'conversation-refresh'])

const s = useAgentSession()
const input = ref('')
const listEl = ref(null)
const toolOpen = ref({})
const includeContext = ref(true)
const drafts = new Map()
const dialogueViews = new Map()
const draftStorageError = ref('')
function draftKey(id, scope = props.draftScope) { return `code-agent-composer-v1:${encodeURIComponent(scope || 'local')}:${encodeURIComponent(id || '')}` }
function readDraft(id, scope = props.draftScope) {
  if (!id) return ''
  const key = draftKey(id, scope)
  if (drafts.has(key)) return drafts.get(key)
  try {
    const value = typeof localStorage === 'undefined' ? '' : localStorage.getItem(key) || ''
    drafts.set(key, value)
    return value
  } catch { return '' }
}
function saveDraft(id, value, scope = props.draftScope) {
  if (!id) return
  drafts.set(draftKey(id, scope), value)
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(draftKey(id, scope), value)
    draftStorageError.value = ''
  } catch { draftStorageError.value = '浏览器无法保存草稿，当前页面内仍会保留。' }
}
input.value = readDraft(props.sessionId)
// Synchronous storage keeps the latest keystroke on refresh; editor recovery has
// a separate key and is never read or modified by dialogue navigation.
watch(input, value => saveDraft(props.sessionId, value), { flush: 'sync' })
const questionDrafts = ref({})
const activeQuestionId = ref('')
const questionErrors = ref({})
const questionGroups = computed(() => s.questions?.value || [])
const pendingQuestions = computed(() => s.pendingQuestions?.value || questionGroups.value.filter(group => group.status === 'pending'))
function questionKey(group) { return `${props.sessionId}:${group.id || group.question_id}` }
function questionDraft(group) { return questionDrafts.value[questionKey(group)] || {} }
function saveQuestionDraft(group, draft) { questionDrafts.value = { ...questionDrafts.value, [questionKey(group)]: draft } }
function questionError(group) { return questionErrors.value[questionKey(group)] || '' }
const questionNotices = computed(() => questionGroups.value.filter(group => questionError(group)).map(group => ({ group, key: questionKey(group), text: questionError(group) })))
const questionStateError = computed(() => s.questionError?.value && !questionNotices.value.some(notice => notice.text === s.questionError.value) ? s.questionError.value : '')
async function submitQuestion(group, answers, dismiss = false) {
  if (s.questionSubmitting?.value || group.status !== 'pending') return false
  const sessionId = props.sessionId, key = questionKey(group)
  activeQuestionId.value = group.id || group.question_id
  questionErrors.value = { ...questionErrors.value, [key]: '' }
  const result = dismiss ? await s.dismissQuestions?.(activeQuestionId.value) : await s.answerQuestions?.(activeQuestionId.value, answers)
  if (props.sessionId === sessionId && result === false && s.questionError?.value) questionErrors.value = { ...questionErrors.value, [key]: s.questionError.value }
  return result
}
function answerQuestion(group, answers) { return submitQuestion(group, answers) }
function dismissQuestion(group) { return submitQuestion(group, null, true) }
watch(() => s.teamRevision?.value, () => emit('team-refresh', s.teamChange?.value || {}))
watch(() => s.streamState?.value, state => { if (state === 'open') emit('team-refresh', {}) })
function chooseAgent(id) {
  emit('team-refresh', {})
  if (id === props.sessionId) { void s.refreshApproval?.(); void s.refreshQuestions?.() }
}
function refreshApprovals() { void s.refreshApproval?.(); emit('team-refresh', {}) }
watch(() => questionGroups.value.map(group => `${group.id || group.question_id}:${group.status}`).join(','), () => emit('team-refresh', {}))
const usageLabel = computed(() => {
  const usage = s.usage?.value
  if (!usage || !(usage.input || usage.output)) return ''
  const fmt = value => Number(value || 0).toLocaleString('zh-CN')
  return `会话累计输入 ${fmt(usage.input)}${usage.cached ? `（缓存 ${fmt(usage.cached)}）` : ''} · 输出 ${fmt(usage.output)}`
})
const usageTitle = computed(() => {
  const usage = s.usage?.value || {}
  const input = Number(usage.input) || 0
  const cached = Math.min(input, Number(usage.cached) || 0)
  const rate = input ? (cached / input * 100).toFixed(1) : '0.0'
  const execution = s.lastExecutionUsage?.value
  const detail = execution?.usage
    ? ` 最近执行：输入 ${Number(execution.usage.input_tokens || 0).toLocaleString()}，输出 ${Number(execution.usage.output_tokens || 0).toLocaleString()}，${Number(execution.model_responses || 0)} 轮模型响应，${Number(execution.duration_seconds || 0).toFixed(1)} 秒。`
    : ''
  return `当前会话已记录轮次的累计用量，含已结算的失败与取消；当前任务预算单独计算。未缓存输入 ${Math.max(0, input - cached).toLocaleString()}，缓存命中率 ${rate}%。缓存已包含在输入中，不额外相加；此处不是费用金额。${detail}`
})

watch(
  () => [props.sessionId, props.draftScope],
  async ([id, scope], [previous, previousScope]) => {
    if (previous) {
      saveDraft(previous, input.value, previousScope)
      dialogueViews.set(draftKey(previous, previousScope), { tools: toolOpen.value, stick: stickBottom.value, scroll: listEl.value?.scrollTop || 0, context: includeContext.value })
    }
    input.value = readDraft(id, scope)
    const view = dialogueViews.get(draftKey(id, scope))
    activeQuestionId.value = ''
    toolOpen.value = view?.tools || {}
    includeContext.value = view?.context ?? true
    stickBottom.value = view?.stick ?? true
    await s.load(id || '')
    if (props.sessionId !== id || props.draftScope !== scope || !view || view.stick) return
    await nextTick()
    if (props.sessionId === id && props.draftScope === scope && listEl.value) listEl.value.scrollTop = view.scroll
  },
  { immediate: false }
)
// Do not start requests before all state used by session changes exists.
onBeforeUnmount(() => { saveDraft(props.sessionId, input.value); drafts.clear(); dialogueViews.clear(); questionDrafts.value = {}; questionErrors.value = {} })

// 导图节点提问：直接填进输入框（不自动发送）——让用户先看一眼带了什么上下文再决定
watch(
  () => props.pendingPrompt,
  (text) => {
    if (!text) return
    input.value = text
    emit('consume-prompt')
  }
)

const cards = computed(() => buildToolCards(s.timeline.value))
const currentAgent = computed(() => [props.team?.lead, ...(props.team?.agents || [])].find(agent => agent?.session_id === props.sessionId))
const approvalReadonly = computed(() => currentAgent.value?.effective_permission
  ? currentAgent.value.effective_permission === 'read-only' : !props.writable)
const currentPhase = computed(() => {
  if (pendingQuestions.value.length) return 'waiting_question'
  const phase = s.phase.value === 'waiting_question' ? 'tools' : s.phase.value
  const agent = currentAgent.value
  if (phase === 'waiting_approval' || phase === 'preparing_context' || phase === 'model' || phase === 'model_streaming') return phase
  if (agent?.state === 'queued') return 'queued'
  if (agent?.state === 'waiting') return agent.pending_approval ? 'waiting_approval' : agent.phase === 'waiting_question' ? phase || 'tools' : 'waiting_children'
  return phase || (s.busy.value ? agent?.phase === 'waiting_question' ? 'tools' : agent?.phase || '' : '')
})
const modelStatus = computed(() => sessionPresentation(s.timeline.value, {
  busy: s.busy.value,
  phase: currentPhase.value,
  banner: s.banner.value,
  submissionError: s.submissionError?.value || '',
  recoveredThroughSeq: s.recoveredThroughSeq?.value || 0,
  bannerDismissed: s.bannerDismissed?.value || false,
}))
let seenProposals = new Set()
watch(cards, list => {
  for (const card of list) {
    if (card.name === 'propose_edit' && card.state === 'ok' && !seenProposals.has(card.id)) {
      seenProposals.add(card.id)
      emit('review-refresh')
    }
  }
})
watch(() => props.sessionId, () => { seenProposals = new Set() })
function prepareAction(action) {
  if (!props.fileContext) return
  const location = `${props.fileContext.path}:${props.codeContext?.start || props.fileContext.start_line || 1}-${props.codeContext?.end || props.fileContext.end_line || 1}`
  const selected = props.selectionText ? `\n当前选中片段：\n${props.selectionText}\n` : ''
  const request = action === 'explain' ? `解释 ${location} 的逻辑、调用关系和潜在问题，引用项目内的文件与行号。` : action === 'tests' ? `为 ${location} 生成有意义的测试，沿用项目已有测试框架。` : `优化 ${location} 的逻辑，先说明问题与改法。`
  input.value = request + selected + (action === 'explain' ? '' : '\n先读取磁盘文件确认最新版本，再用 propose_edit 提交待审差异。已有文件用精确替换，新建测试文件用 content；提交提案后提示我到修改面板审查。')
  nextTick(() => listEl.value?.parentElement?.querySelector('textarea')?.focus())
}
const toolCardsById = computed(() => {
  const map = new Map()
  for (const c of cards.value) map.set(c.id, c)
  return map
})
function fullToolArgs(value) {
  if (typeof value === 'string') {
    try { value = JSON.parse(value) } catch { return value || '（无参数）' }
  }
  if (value == null) return '（无参数）'
  if (typeof value === 'object' && !Array.isArray(value) && typeof value.command === 'string') {
    const { command, ...rest } = value
    return command + (Object.keys(rest).length ? `\n\n其他参数\n${JSON.stringify(rest, null, 2)}` : '')
  }
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
}

// The short timeline may evict an old question_request while question history
// retains its terminal record. Keep those records in a collapsed archive rather
// than appending them after every new response or replay.
const archivedQuestions = computed(() => {
  const requests = new Set(s.timeline.value.filter(event => event.type === 'question_request').map(event => event.payload?.question_id))
  const seen = new Set()
  return questionGroups.value.filter(group => {
    const id = group.id || group.question_id
    if (!id || group.status === 'pending' || requests.has(id) || seen.has(id)) return false
    seen.add(id)
    return true
  })
})

/** 渲染顺序：user_message / assistant_message（含工具卡）/ done / error。 */
const messages = computed(() => {
  const out = [], seenQuestions = new Set()
  for (const ev of s.timeline.value) {
    if (ev.type === 'user_message') {
      const internal = internalMessagePresentation(ev)
      out.push(internal ? { kind: 'internal', key: `u${ev.seq}`, ...internal } : { kind: 'user', key: `u${ev.seq}`, text: ev.payload?.text || '' })
    } else if (ev.type === 'assistant_message') {
      const calls = (ev.payload?.tool_calls || []).filter(c => c.name !== 'ask_user_question').map((c) => c.id)
      out.push({ kind: 'assistant', key: `a${ev.seq}`, text: ev.payload?.text || '', calls })
    } else if (ev.type === 'done') {
      out.push({ kind: 'done', key: `d${ev.seq}`, text: ev.payload?.text || '', stop: ev.payload?.stop_reason, incomplete: incompleteCompletion(ev.payload) })
    } else if (ev.type === 'error') {
      const internal = internalMessagePresentation(ev)
      out.push(internal ? { kind: 'internal', key: `e${ev.seq}`, ...internal } : { kind: 'error', key: `e${ev.seq}`, text: ev.payload?.message || '发生错误',
        historical: modelStatus.value.historicalErrors.has(ev.seq), when: eventTime(ev.ts) })
    } else if (ev.type === 'session_meta' && ev.payload?.team_notice) {
      const receipt = teamNoticePresentation(ev.payload.team_notice)
      if (receipt) out.push({ kind: 'team-notice', key: `n${ev.seq}`, ...receipt })
    } else if (ev.type === 'question_request') {
      const group = questionGroups.value.find(value => (value.id || value.question_id) === ev.payload?.question_id)
      if (group && group.status !== 'pending' && !seenQuestions.has(group.id || group.question_id)) { seenQuestions.add(group.id || group.question_id); out.push({ kind: 'question', key: `q${ev.seq}`, group }) }
    }
  }
  return out
})

// Completed style replies reuse this session's event stream. Streaming fragments
// and ordinary coding replies never become design commands; the canvas validates
// request/selector identity and waits for a human to apply the proposed styles.
let seenStyleReplies = new Set()
watch(() => props.sessionId, () => { seenStyleReplies = new Set() })
watch(messages, list => {
  for (const message of list) {
    if (!['assistant', 'done'].includes(message.kind) || (message.calls?.length) ||
        (message.kind === 'done' && message.incomplete)) continue
    const text = message.text
    if (!text || text.length > 24000 || !/"kind"\s*:\s*"frontend-style"/.test(text)) continue
    const key = `${props.sessionId}:${text}`
    if (seenStyleReplies.has(key)) continue
    seenStyleReplies.add(key)
    if (seenStyleReplies.size > 100) seenStyleReplies.delete(seenStyleReplies.values().next().value)
    emit('assistant-complete', { sessionId: props.sessionId, text, seq: message.key })
  }
})

//: 只有"用户本来就在底部"时才自动跟随（同 ChatView）：
//: 否则流式输出期间往上翻会被反复拽回底部。
const stickBottom = ref(true)
void s.load(props.sessionId || '')

function onListScroll() {
  const el = listEl.value
  if (!el) return
  stickBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight <= 48
}

function scrollDown(force = false) {
  if (!force && !stickBottom.value) return
  nextTick(() => {
    const el = listEl.value
    if (!el) return
    el.scrollTop = el.scrollHeight
    stickBottom.value = true
  })
}
watch(() => [messages.value.length, s.streaming.value], () => scrollDown())

const canSend = computed(() => !!props.sessionId && !!input.value.trim() && !s.busy.value && !pendingQuestions.value.length && !s.historyLoading.value && !s.historyError.value)

async function send() {
  if (!canSend.value) return
  scrollDown(true)
  const text = input.value
  const sessionId = props.sessionId
  const scope = props.draftScope
  input.value = ''
  // 结构坐标随消息发给服务端：由它从符号索引渲染成事实块（只进模型 prompt，
  // 展示文本仍是用户原文）。这样模型看到的是"文件:行 + 签名 + 被谁引用"，
  // 而不是让模型自己去猜用户在指什么。
  const ctx = includeContext.value ? { ...props.codeContext } : {}
  // 联网开关也跟着走（M19）：关着时后端不会把 web_search/web_fetch 暴露给模型
  const sent = await s.send(text, { codeContext: ctx, web: s.web.value })
  if (sent && props.sessionId === sessionId) emit('sent')
  if (!sent && !readDraft(sessionId, scope)) {
    saveDraft(sessionId, text, scope)
    if (!input.value && props.sessionId === sessionId && props.draftScope === scope) input.value = text
  }
}

/** 联网开关：后端说不可用（没配检索 key）时点不开，并把原因写在 title 上。 */
const webAvailable = computed(() => s.webInfo.value?.available !== false)
const webTitle = computed(
  () => s.webHint.value + (webAvailable.value ? '' : `（${s.webInfo.value?.how_to || ''}）`)
)
async function toggleWeb() {
  if (!webAvailable.value) return
  await s.setWeb(!s.web.value)
}

function toggleTool(id) {
  toolOpen.value = { ...toolOpen.value, [id]: !toolOpen.value[id] }
}

const STATE_LABEL = { running: '执行中', ok: '完成', denied: '被拒绝', error: '失败' }
</script>

<template>
  <div class="ai" :class="{ 'has-approval': !!s.approval.value, 'has-questions': !!pendingQuestions.length }">
    <div class="ai-meta">
    <div class="head">
      <span class="assistant-icon"><UiIcon name="sparkles" :size="15" /></span><span class="title">{{ projectChat ? '项目对话' : '代码助手' }}</span>
      <span v-if="s.modelName.value" class="chip">{{ s.modelName.value }}</span>
      <span class="spacer" />
      <span class="dot" :class="s.streamState.value" :title="`WS: ${s.streamState.value}`" />
      <button v-if="s.busy.value" class="ghost danger-text" @click="s.stop()">停止</button>
    </div>
    <ConversationTabs v-if="conversationsEnabled" :conversations="conversations" :selected-id="conversationId" :creating="conversationCreating" :deleting="conversationDeleting" :loading="conversationLoading" :disabled="!sessionId" :error="conversationError" @select="emit('conversation-select', $event)" @create="emit('conversation-create')" @delete="emit('conversation-delete', $event)" @refresh="emit('conversation-refresh')" />
    <BackgroundAgentTasks v-if="sessionId" :key="sessionId" :team="team" :selected-id="sessionId" :loading="teamLoading" :error="teamError" :acting="teamActing" :visible="visible" @refresh="refreshApprovals" @stop="emit('agent-stop', $event)" />
    <div v-if="sessionId" class="connection-status"><span :class="{ failed: s.streamState.value === 'closed' }">会话{{ s.streamState.value === 'open' ? '已连接' : s.streamState.value === 'closed' ? '已断开' : '连接中' }}</span><span :class="modelStatus.state">{{ modelStatus.label }}</span><button class="ghost" @click="emit('open-review')">查看修改</button></div>
    <div v-if="usageLabel" class="session-usage" :title="usageTitle">{{ usageLabel }}</div>
    </div>

    <div v-if="!sessionId" class="muted empty no-session"><span class="welcome-icon"><UiIcon name="sparkles" :size="26" /></span><strong>与你一起理解代码</strong><p>打开项目后，可以解释代码、梳理依赖或提出修改。</p></div>

    <template v-else>
      <div class="conversation-body">
      <div ref="listEl" class="list" @scroll.passive="onListScroll">
        <div v-if="s.historyLoading.value" class="muted thinking" role="status">正在读取会话记录…</div>
        <div v-if="s.historyError.value" class="history-load-error" role="alert"><span>读取会话记录失败：{{ s.historyError.value }}</span><button class="ghost" :disabled="s.historyLoading.value" @click="s.load(sessionId)">重新读取</button></div>
        <details v-if="archivedQuestions.length" :key="sessionId" class="archived-question-history" aria-label="历史提问">
          <summary>历史提问（{{ archivedQuestions.length }}）</summary>
          <div class="archived-question-items"><QuestionRequest v-for="group in archivedQuestions" :key="questionKey(group)" :group="group" :draft="questionDraft(group)" :error="questionError(group)" /></div>
        </details>
        <div v-if="!messages.length && !s.busy.value && !s.historyLoading.value && !s.historyError.value" class="muted empty">
          <div>
            <b>{{ projectChat ? '项目对话' : 'Lead' }}</b>可以直接读改代码、运行测试并交付；适合并行时按需要调用后台子任务，在当前主对话汇总结果。
          </div>
          <div class="empty-sub">
            修改建议进入右侧「修改」面板，可逐块应用、拒绝或回滚；直接修改受当前执行权限控制。
          </div>
          <div class="empty-sub">
            {{ writable ? '当前执行可写。' : '当前执行只读，可提交待审修改。' }}需要授权的操作仍会请求批准，并保留操作与审批日志。
          </div>
        </div>

        <div v-for="m in messages" :key="m.key" class="msg" :class="m.kind">
          <details v-if="m.kind === 'internal'" class="internal-message" aria-label="系统过程">
            <summary><span>{{ m.label }}</span><span v-if="m.summary !== m.label"> · {{ m.summary }}</span></summary>
            <pre>{{ m.text }}</pre>
          </details>
          <template v-else-if="m.kind === 'user'">
            <div class="bubble user">{{ m.text }}</div>
          </template>

          <template v-else-if="m.kind === 'assistant'">
            <div v-if="m.text" class="bubble assistant markdown-body" v-html="renderMarkdown(m.text)" />
            <div v-for="cid in m.calls" :key="cid" class="tool">
              <button class="tool-head" type="button" :aria-expanded="!!toolOpen[cid]" :aria-label="`${toolOpen[cid] ? '收起' : '展开'} ${toolCardsById.get(cid)?.name || 'tool'} 执行说明和结果`" @click="toggleTool(cid)">
                <span class="caret" :class="{ open: toolOpen[cid] }">▸</span>
                <span class="tool-name mono">{{ toolCardsById.get(cid)?.name || 'tool' }}</span>
                <span class="tool-args mono">{{ summarizeToolArgs(toolCardsById.get(cid)?.args) }}</span>
                <span class="tool-state" :class="toolCardsById.get(cid)?.state">
                  {{ STATE_LABEL[toolCardsById.get(cid)?.state] || '' }}
                </span>
              </button>
              <ToolExplanation :explanation="toolCardsById.get(cid)?.explanation" :expanded="!!toolOpen[cid]" />
              <div v-if="toolOpen[cid]" class="tool-details">
                <div class="tool-section"><strong>完整命令与参数</strong><pre class="tool-parameters">{{ fullToolArgs(toolCardsById.get(cid)?.args) }}</pre></div>
                <div class="tool-section"><strong>实际执行结果<span v-if="toolCardsById.get(cid)?.state"> · {{ STATE_LABEL[toolCardsById.get(cid)?.state] || '' }}</span></strong><pre class="tool-result">{{ toolCardsById.get(cid)?.result || (toolCardsById.get(cid)?.state === 'running' ? '（等待结果）' : '（工具未返回内容）') }}</pre></div>
              </div>
              <button v-if="toolCardsById.get(cid)?.name === 'propose_edit' && toolCardsById.get(cid)?.state === 'ok'" class="review-link" @click="emit('open-review')">审查这次修改 →</button>
            </div>
          </template>

          <template v-else-if="m.kind === 'done'">
            <div v-if="m.text" class="bubble assistant markdown-body" v-html="renderMarkdown(m.text)" />
            <div v-if="m.incomplete" class="tag warn" role="status" :title="m.stop || ''">{{ m.incomplete.label }}</div>
          </template>

          <div v-else-if="m.kind === 'team-notice'" class="team-receipt"><header><strong>{{ m.title }}</strong><span>{{ m.state }}</span></header><details v-if="m.text"><summary>查看子任务回执</summary><p>{{ m.text }}</p></details><small>子任务记录可在上方‘后台子任务’只读展开，输入始终留在当前主对话。</small></div>
          <QuestionRequest v-else-if="m.kind === 'question'" :group="m.group" :draft="questionDraft(m.group)" :error="questionError(m.group)" />
          <details v-else-if="m.historical" class="historical-error">
            <summary>历史错误<span v-if="m.when"> · {{ m.when }}</span><span class="history-action">展开详情</span></summary>
            <div class="bubble error">{{ m.text }}</div>
          </details>
          <template v-else>
            <div class="tag current-error">本次请求错误<span v-if="m.when"> · {{ m.when }}</span></div>
            <div class="bubble error">{{ m.text }}</div>
          </template>
        </div>

        <!-- 打字机缓冲：delta 不落轨迹，只能实时显示 -->
        <div v-if="s.streaming.value" class="bubble assistant markdown-body streaming" v-html="renderMarkdown(s.streaming.value)" />
        <div v-else-if="s.busy.value && !pendingQuestions.length" class="muted thinking" role="status">{{ modelStatus.label }}…</div>
      </div>

      <div v-if="pendingQuestions.length" class="question-requests" aria-label="待回答问题">
        <div class="questions-heading" role="status"><strong>{{ pendingQuestions.length > 1 ? `${pendingQuestions.length} 组问题等待你回答` : '请回答下方问题后继续' }}</strong><button class="ghost" @click="s.refreshQuestions?.()">刷新问题</button></div>
        <QuestionRequest v-for="group in pendingQuestions" :key="questionKey(group)" :group="group" :draft="questionDraft(group)" :submitting="s.questionSubmitting?.value === (group.id || group.question_id)" :error="questionError(group)" @update:draft="saveQuestionDraft(group, $event)" @answer="answerQuestion(group, $event)" @dismiss="dismissQuestion(group)" />
      </div>
      <ApprovalRequest v-if="s.approval.value" :approval="s.approval.value" :submitting="!!s.approvalSubmitting.value" :readonly="approvalReadonly" @decide="s.decide" />
      </div>

      <div v-if="modelStatus.banner" class="banner" role="alert"><span>{{ modelStatus.banner }}</span><button class="ghost" aria-label="收起错误提示" title="收起当前提示，失败记录仍保留" @click="s.dismissBanner()"><UiIcon name="close" :size="14" /></button></div>
      <div v-for="notice in questionNotices" :key="notice.key" class="question-error-notice" role="alert"><strong>回答未送达</strong><span>{{ notice.group.questions?.[0]?.question }}</span><p>{{ notice.text }}</p></div>
      <div v-if="questionStateError" class="question-error-notice" role="alert">{{ questionStateError }}<button class="ghost" @click="s.refreshQuestions?.()">重新核对问题</button></div>
      <div v-if="s.approvalError?.value" class="question-error-notice" role="alert">{{ s.approvalError.value }}<button class="ghost" @click="refreshApprovals">重新核对审批</button></div>

      <div v-show="!s.approval.value && !pendingQuestions.length" class="composer">
        <div v-if="fileContext" class="code-actions"><button :disabled="s.busy.value" @click="prepareAction('explain')">解释代码</button><button :disabled="s.busy.value" @click="prepareAction('modify')">提出修改</button><button :disabled="s.busy.value" @click="prepareAction('tests')">生成测试</button><span v-if="selectionText" class="muted">已选中代码</span></div>
        <div v-if="fileContext" class="ctx">
          <label class="ctx-label">
            <input v-model="includeContext" type="checkbox" />
            <span class="mono">
              {{ codeContext?.path || fileContext.path }}:{{ codeContext?.start || fileContext.start_line || 1 }}-{{ codeContext?.end || fileContext.end_line || fileContext.start_line || 1 }}
            </span>
          </label>
          <span v-if="codeContext?.symbol_id" class="muted ctx-sym mono" :title="codeContext.symbol_id">
            {{ String(codeContext.symbol_id).split('#').pop() }}
          </span>
          <button class="ghost" @click="emit('clear-context')">清除</button>
        </div>
        <textarea
          v-model="input"
          rows="3"
          aria-label="向代码助手发送消息"
          placeholder="提问或描述想要的修改…"
          @keydown="onComposerKeydown"
          @compositionstart="composing = true"
          @compositionend="composing = false"
        />
        <small v-if="draftStorageError" role="status" class="muted">{{ draftStorageError }}</small>
        <div class="send-row">
          <!-- 联网开关（M19）：和"发送"同一行，发消息时随这一轮一起带过去。
               不可用时（没配检索 key）置灰，title 里写清怎么配。 -->
          <button
            class="web-toggle"
            :class="{ on: s.web.value, off: !webAvailable }"
            :disabled="!webAvailable"
            :title="webTitle"
            @click="toggleWeb"
          >
            <span class="web-dot" />
            联网{{ s.web.value ? '已开' : '关闭' }}
          </button>
          <span class="muted tiny">{{ sendHint }}</span>
          <span class="spacer" />
          <button class="primary" :disabled="!canSend" @click="send">发送</button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ai { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; height: 100%; background: var(--panel); }
.ai-meta { flex: none; min-width: 0; min-height: 0; }
.conversation-body { display: flex; flex: 1 1 0%; flex-direction: column; min-width: 0; min-height: 0; }
.has-questions .ai-meta { flex: 0 1 auto; max-height: 32%; overflow-y: auto; }.has-questions .list { flex: 0 1 26%; min-height: 48px; }.question-requests { flex: 1 1 0%; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 10px 12px; display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--border); }.question-requests > :deep(.question-request) { flex: none; }.questions-heading { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; color: var(--muted); font-size: var(--fs-2xs); }.questions-heading strong { font-weight: var(--fw-medium); }.questions-heading button { margin-left: auto; font-size: var(--fs-2xs); }.has-questions.has-approval .question-requests { flex: 1 1 45%; }.has-questions.has-approval .conversation-body > :deep(.approval-request) { flex: 1 1 40%; }.has-questions.has-approval .list { flex: 0 1 10%; min-height: 0; }
.has-approval .ai-meta { flex: 0 1 auto; max-height: 40%; overflow-y: auto; overscroll-behavior: contain; }
.has-approval .conversation-body { padding: 8px; gap: 8px; }
.has-approval .list { flex: 0 1 20%; max-height: 20%; padding: 4px 8px; }
.has-approval .conversation-body > :deep(.approval-request) { flex: 1 1 0%; min-height: 0; max-height: none; max-width: none; }
.has-approval .banner { flex: 0 1 auto; min-height: 0; max-height: 15%; overflow: auto; }
.session-usage { flex: none; padding: 5px 14px; color: var(--muted); font-size: var(--fs-2xs); border-bottom: 1px solid var(--border); }.team-receipt { border: 1px solid var(--border); border-radius: 6px; background: var(--panel-soft); padding: 9px; font-size: var(--fs-xs); }.team-receipt header { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; color: var(--muted); }.team-receipt header strong { color: var(--text); }.team-receipt header button { margin-left: auto; color: var(--accent); font-size: var(--fs-2xs); }.team-receipt summary { margin-top: 6px; cursor: pointer; color: var(--accent); }.team-receipt p { margin: 7px 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }.team-receipt small { display: block; margin-top: 6px; color: var(--muted); font-size: var(--fs-2xs); }
.head { display: flex; align-items: center; gap: 8px; min-height: 43px; flex: none; padding: 7px 14px; font-size: var(--fs-sm); }
.head .assistant-icon { display: grid; place-items: center; width: 25px; height: 25px; color: var(--accent); background: var(--accent-soft); border-radius: 6px; flex: none; }
.title { font-weight: var(--fw-semibold); white-space: nowrap; }.spacer { flex: 1; min-width: 0; }
.chip { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-2xs); padding: 2px 6px; border-radius: 4px; color: var(--muted); background: var(--panel-soft); }
.connection-status { display: flex; flex: none; gap: 10px; align-items: center; min-height: 27px; padding: 0 14px 6px; font-size: var(--fs-2xs); color: var(--muted); border-bottom: 1px solid var(--border); }
.connection-status button { margin-left: auto; font-size: var(--fs-2xs); white-space: nowrap; }.connection-status .failed, .connection-status .error { color: var(--err); }.connection-status .busy { color: var(--accent); }
.connection-status .incomplete { color: var(--warn); }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--border-strong); flex: none; }.dot.open { background: var(--ok); }.dot.closed { background: var(--err); }
.ghost { min-height: 24px; border: none; background: transparent; color: var(--muted); padding: 2px 5px; }.ghost:hover { background: var(--hover); color: var(--text); }.danger-text { color: var(--err); }
.list { flex: 1; min-height: 0; overflow-y: auto; padding: 18px 16px; display: flex; flex-direction: column; gap: 20px; scroll-behavior: auto; }
.archived-question-history { flex: none; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-soft); font-size: var(--fs-2xs); color: var(--muted); }
.archived-question-history > summary { padding: 8px 10px; cursor: pointer; }
.archived-question-items { display: flex; flex-direction: column; gap: 8px; padding: 0 10px 10px; max-height: 300px; overflow-y: auto; overscroll-behavior: contain; }
.empty { display: flex; flex-direction: column; gap: 12px; font-size: var(--fs-sm); line-height: 1.8; padding: 22px 4px; }.empty-sub { font-size: var(--fs-xs); color: var(--muted); }
.no-session { align-items: center; text-align: center; max-width: 300px; margin: auto; padding: 30px 24px; }.no-session strong { color: var(--text); font-size: var(--fs-sm); font-weight: var(--fw-semibold); }.no-session p { margin: 0; color: var(--muted); font-size: var(--fs-xs); }.welcome-icon { display: grid; place-items: center; width: 54px; height: 54px; margin-bottom: 4px; border-radius: 12px; background: var(--accent-soft); color: var(--accent); }
.msg { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.internal-message { color: var(--muted); font-size: var(--fs-xs); min-width: 0; }
.internal-message summary { cursor: pointer; line-height: 1.7; }
.internal-message pre { margin: 8px 0 0; padding: 10px; background: var(--panel-soft); border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; max-height: 240px; overflow-y: auto; }
.bubble { min-width: 0; padding: 11px 13px; border-radius: 9px; font-size: var(--fs-sm); white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; line-height: 1.85; }
.bubble.user { background: var(--accent-soft); align-self: flex-end; max-width: 94%; color: var(--text); border: 1px solid color-mix(in srgb, var(--accent) 10%, transparent); }
.bubble.assistant { background: transparent; padding: 0 1px; color: var(--text-2); }
.markdown-body { white-space: normal; }
.markdown-body :deep(> :first-child) { margin-top: 0; }.markdown-body :deep(> :last-child) { margin-bottom: 0; }
.markdown-body :deep(p) { margin: 0 0 12px; }.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3), .markdown-body :deep(h4), .markdown-body :deep(h5), .markdown-body :deep(h6) { color: var(--text); font-size: var(--fs-sm); font-weight: var(--fw-semibold); line-height: 1.65; margin: 20px 0 9px; }
.markdown-body :deep(h1), .markdown-body :deep(h2) { font-size: var(--fs-lg); }.markdown-body :deep(strong) { color: var(--text); font-weight: var(--fw-semibold); }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 22px; margin: 8px 0 14px; }.markdown-body :deep(li) { margin: 4px 0; }.markdown-body :deep(li > p) { margin-bottom: 5px; }
.markdown-body :deep(a) { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; overflow-wrap: anywhere; }
.markdown-body :deep(code) { padding: 2px 4px; border-radius: 4px; background: var(--panel-soft); color: var(--text); font: var(--fs-xs)/1.7 var(--font-mono, monospace); }
.markdown-body :deep(pre) { margin: 12px 0; width: 100%; min-width: 0; max-width: 100%; padding: 12px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; word-break: normal; overflow: auto; line-height: 1.7; }
.markdown-body :deep(pre code) { display: block; min-width: 0; max-width: 100%; padding: 0; white-space: inherit; overflow-wrap: inherit; word-break: inherit; background: transparent; }
.markdown-body :deep(blockquote) { margin: 12px 0; padding: 4px 12px; border-left: 3px solid var(--accent-border); color: var(--muted); }.markdown-body :deep(blockquote p:last-child) { margin-bottom: 0; }
.markdown-body :deep(table) { display: block; width: max-content; max-width: 100%; border-collapse: collapse; margin: 12px 0; overflow-x: auto; font-size: var(--fs-xs); }.markdown-body :deep(th), .markdown-body :deep(td) { padding: 7px 10px; border: 1px solid var(--border); text-align: left; }.markdown-body :deep(th) { color: var(--text); background: var(--panel-soft); font-weight: var(--fw-semibold); }.markdown-body :deep(hr) { margin: 18px 0; border: 0; border-top: 1px solid var(--border); }
.bubble.error { background: var(--err-soft); color: var(--err); font-size: var(--fs-xs); line-height: 1.7; border-left: 2px solid var(--err); border-radius: 0 6px 6px 0; }
.current-error { color: var(--err); }.historical-error { border: 1px solid var(--border); background: var(--panel-soft); border-radius: 6px; font-size: var(--fs-2xs); color: var(--muted); }
.historical-error summary { cursor: pointer; padding: 9px 10px; line-height: 1.7; }.historical-error summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }.historical-error .history-action { float: right; margin-left: 8px; }.historical-error[open] .history-action { display: none; }.historical-error .bubble { margin: 0 8px 8px; }
.bubble.streaming { opacity: .85; }.tag { font-size: var(--fs-2xs); color: var(--muted); }.tag.warn { color: var(--warn); }.thinking { font-size: var(--fs-xs); padding: 3px 0; }
.history-load-error { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: var(--fs-xs); color: var(--err); overflow-wrap: anywhere; }
.tool { border: 1px solid var(--border); border-radius: 6px; overflow: hidden; background: var(--panel); }
.tool-head { width: 100%; display: flex; align-items: center; gap: 6px; min-height: 33px; padding: 7px 9px; border: 0; border-radius: 0; text-align: left; cursor: pointer; font-size: var(--fs-2xs); background: var(--panel-soft); }
.tool-head:hover { background: var(--hover); }.caret { color: var(--muted); transition: transform .12s; }.caret.open { transform: rotate(90deg); }.tool-name { font-weight: var(--fw-medium); flex: none; }.tool-args { min-width: 0; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.tool-state { margin-left: auto; flex: none; font-size: var(--fs-2xs); color: var(--muted); }.tool-state.ok { color: var(--ok); }.tool-state.denied { color: var(--warn); }.tool-state.error { color: var(--err); }
.tool-head:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.tool-details { border-top: 1px solid var(--border); padding: 10px 12px; display: grid; gap: 11px; min-width: 0; }.tool-section { min-width: 0; }.tool-section > strong { display: block; margin-bottom: 5px; font-size: var(--fs-2xs); color: var(--text); font-weight: var(--fw-medium); }
.tool-result, .tool-parameters { margin: 0; padding: 9px 10px; font-size: var(--fs-xs); background: var(--panel-soft); max-height: 220px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; border: 1px solid var(--border); border-radius: 4px; }
.review-link { display: block; width: 100%; min-height: 30px; border: 0; border-radius: 0; font-size: var(--fs-xs); background: var(--accent-soft); color: var(--accent); text-align: left; padding: 6px 10px; }
.approval { flex: none; border-top: 1px solid var(--border); padding: 14px; background: var(--warn-soft); }.approval-title { font-size: var(--fs-sm); font-weight: var(--fw-semibold); color: var(--warn); margin-bottom: 8px; }.approval-args { font-size: var(--fs-xs); max-height: 90px; overflow: auto; white-space: pre-wrap; margin-bottom: 10px; }.approval-actions { display: flex; gap: 8px; }
.banner { display: flex; align-items: flex-start; gap: 8px; flex: none; margin: 0 12px 10px; padding: 9px 11px; border-radius: 6px; font-size: var(--fs-xs); background: var(--err-soft); color: var(--err); overflow-wrap: anywhere; }.banner > span { flex: 1; min-width: 0; }.banner > button { flex: none; color: inherit; }
.question-error-notice { flex: 0 1 auto; min-height: 0; max-height: 15%; overflow: auto; margin: 0 12px 8px; padding: 8px 10px; border-radius: 6px; background: var(--err-soft); color: var(--err); font-size: var(--fs-2xs); line-height: 1.7; overflow-wrap: anywhere; }.question-error-notice strong { margin-right: 7px; }.question-error-notice p { margin: 5px 0 0; }.question-error-notice button { margin-left: 6px; }
.composer { flex: none; border-top: 1px solid var(--border); background: var(--panel); padding: 12px; }
.code-actions { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; align-items: center; }.code-actions button { font-size: var(--fs-2xs); padding: 4px 8px; min-height: 27px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 5px; color: var(--text-2); }.code-actions button:hover { color: var(--accent); border-color: var(--accent-border); background: var(--accent-soft); }.code-actions > span { font-size: var(--fs-2xs); }
.ctx { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 0 8px; padding: 5px 7px; border-radius: 5px; background: var(--panel-soft); font-size: var(--fs-2xs); }
.ctx-label { display: flex; align-items: center; gap: 6px; min-width: 0; }.ctx-label span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.ctx-label input { flex: none; }.ctx-sym { font-size: var(--fs-2xs); max-width: 90px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.ctx .ghost { font-size: var(--fs-2xs); flex: none; }
.composer textarea { width: 100%; min-height: 92px; max-height: 35vh; resize: vertical; padding: 11px 12px; border: 1px solid var(--border-strong); border-radius: 8px; font-size: var(--fs-sm); line-height: 1.7; background: var(--panel); }
.send-row { display: flex; align-items: center; gap: 7px; margin-top: 9px; }.send-row .spacer { flex: 1; }.send-row .primary { min-height: 30px; padding: 5px 13px; font-size: var(--fs-xs); }
.web-toggle { display: inline-flex; align-items: center; flex: none; gap: 5px; min-height: 27px; font-size: var(--fs-2xs); padding: 3px 7px; border-radius: 5px; border: 1px solid transparent; background: transparent; color: var(--muted); cursor: pointer; }.web-toggle .web-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--border-strong); flex: none; }.web-toggle.on { color: var(--accent); background: var(--accent-soft); }.web-toggle.on .web-dot { background: var(--accent); }.web-toggle.off { cursor: not-allowed; opacity: .6; }.tiny { font-size: var(--fs-2xs); }
@media (max-width: 1400px) and (min-width: 1101px) { .send-row .tiny { display: none; }.connection-status { gap: 7px; } }
@media (max-width: 440px) { .send-row .tiny { display: none; }.list { padding: 14px; }.composer { padding: 10px; }.head { padding: 7px 12px; }.connection-status { padding-left: 12px; padding-right: 12px; gap: 8px; }.ctx-sym { display: none; } }
@media (max-height: 600px) { .has-approval .list { display: none; }.has-approval .conversation-body { padding: 6px; } }
@media (prefers-reduced-motion: reduce) { .caret { transition: none; } }
</style>
