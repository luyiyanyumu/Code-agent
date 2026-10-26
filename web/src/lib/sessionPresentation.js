// 当前请求状态只由本轮事件决定，历史错误仍留在时间线中供展开查看。
const MODEL_ERROR = /provider|模型|connection|connect|连接|API|key|认证|余额|额度|\b429\b|\b401\b/i

// A terminal event is not proof of success. Unknown provider reasons must stay
// unfinished; missing reasons remain compatible with older recorded sessions.
export function isSuccessfulCompletion(payload) {
  const reason = payload?.stop_reason
  return (reason == null || reason === '' || reason === 'end_turn' || reason === 'stop')
    && !(Array.isArray(payload?.tool_calls) && payload.tool_calls.length)
}
export function incompleteCompletion(payload) {
  if (isSuccessfulCompletion(payload)) return null
  const reason = typeof payload?.stop_reason === 'string' ? payload.stop_reason : String(payload?.stop_reason ?? '')
  // 旧停止原因仅供历史记录展示，不能启用任何执行模式。
  const labels = {
    max_tokens: '输出达到上限，任务未完成', length: '输出达到上限，任务未完成',
    content_filter: '输出被内容过滤，任务未完成', max_turns: '执行达到轮数上限，任务未完成',
    cancelled: '任务已停止，尚未完成', canceled: '任务已停止，尚未完成', user_cancel: '任务已停止，尚未完成',
    token_budget: '执行预算已用尽，任务未完成', cage_budget: '执行预算已用尽，任务未完成', budget_exceeded: '执行预算已用尽，任务未完成',
    error: '执行异常，任务未完成', provider_error: '模型请求失败，任务未完成',
    tool_use: '工具调用尚未完成，任务未完成',
  }
  return { reason, label: Object.hasOwn(labels, reason) ? labels[reason] : Array.isArray(payload?.tool_calls) && payload.tool_calls.length ? '工具调用尚未完成，任务未完成' : `任务未完成 · 停止原因：${reason}` }
}

// Model-facing user-role messages may be authored by the runtime or another
// agent. Only explicit attribution (or the legacy automatic flag) changes
// their presentation; user text and unknown source kinds are never guessed.
const INTERNAL_SOURCES = Object.freeze({
  'runtime-continuation': '系统过程',
  'runtime-context': '运行上下文',
  'job-notice': '后台任务通知',
  'tool-jobs': '后台任务通知',
  'agent-message': '智能体消息',
  'subagent-settled': '子任务结果',
})
export function internalMessagePresentation(event) {
  if (event?.type === 'error' && event.payload?.quiet === true) {
    const code = event.payload.code
    const summary = code === 'repeat_tool_reminder' ? '工具执行提示' : code === 'segment_boundary' ? '执行检查点' : code === 'job_wake_budget_exhausted' ? '后台任务通知' : '执行提示'
    return { label: '系统过程', summary, text: typeof event.payload.message === 'string' ? event.payload.message : '' }
  }
  if (event?.type !== 'user_message') return null
  const payload = event.payload || {}, source = payload.source
  const kind = source && typeof source === 'object' && !Array.isArray(source) && typeof source.kind === 'string' ? source.kind : ''
  if (payload.auto !== true && !Object.hasOwn(INTERNAL_SOURCES, kind)) return null
  const label = INTERNAL_SOURCES[kind] || '系统过程'
  const supplied = typeof source?.summary === 'string' ? source.summary.trim().slice(0, 160) : ''
  let summary = supplied
  if (!summary) {
    if (payload.kind === 'job_notice') summary = '收到后台任务通知'
    else if (payload.kind === 'repeat_tool_notice') summary = '工具执行提示'
    else if (payload.kind === 'image') summary = '图片已加入模型上下文'
    else if (Number.isSafeInteger(payload.closures) && payload.closures > 0) summary = '验证检查'
    else if (Number.isSafeInteger(payload.continuation) && payload.continuation > 0) summary = `自动继续 · 第 ${payload.continuation} 次`
    else summary = label
  }
  return { label, summary, text: typeof payload.text === 'string' ? payload.text : '' }
}

export function isModelError(event) {
  return event?.type === 'error' && !event.payload?.quiet && event.payload?.code !== 'busy'
    && (event.payload?.code === 'provider_error' || MODEL_ERROR.test(event.payload?.message || ''))
}

export function sessionPhaseLabel(phase) {
  if (phase === 'preparing_context') return '准备项目上下文'
  if (phase === 'model' || phase === 'model_streaming') return '模型响应中'
  if (phase === 'preparing') return '准备项目上下文'
  if (phase === 'queued') return '排队等待执行'
  if (phase === 'waiting_files') return '等待文件释放'
  if (phase === 'waiting_dependencies') return '等待前置任务'
  if (phase === 'tools') return '工具执行中'
  if (phase === 'waiting_children' || phase === 'waiting') return '等待子任务'
  if (phase === 'waiting_approval') return '等待你批准'
  if (phase === 'waiting_question') return '等待你回答'
  return '执行中'
}

export function sessionPresentation(events, { busy = false, phase = '', banner = '', submissionError = '', recoveredThroughSeq = 0, bannerDismissed = false } = {}) {
  const history = events || []
  const historicalErrors = new Set()
  let latestDone = -1, latestModelError = -1, resolved = -1
  const recovered = Number.isSafeInteger(recoveredThroughSeq) && recoveredThroughSeq > 0 ? recoveredThroughSeq : 0
  for (let i = 0; i < history.length; i += 1) {
    const event = history[i]
    if (event.type === 'done') latestDone = i
    if (isModelError(event)) latestModelError = i
    const response = event.type === 'assistant_message' && (isSuccessfulCompletion(event.payload)
      || ['end_turn', 'stop', 'tool_use', undefined, null, ''].includes(event.payload?.stop_reason) && Array.isArray(event.payload?.tool_calls) && event.payload.tool_calls.length > 0)
      && (Boolean(event.payload?.text?.trim?.()) || (Array.isArray(event.payload?.tool_calls) && event.payload.tool_calls.length > 0))
    if ((event.type === 'user_message' && !internalMessagePresentation(event)) || response || (event.type === 'done' && isSuccessfulCompletion(event.payload))
      || (recovered && event.seq > 0 && event.seq <= recovered)) resolved = i
  }
  // 实时响应游标独立于400条历史上限；历史回放本身也能恢复已完成请求。
  for (let i = 0; i < history.length; i += 1) if (history[i].type === 'error'
    && (i < resolved || (recovered && history[i].seq > 0 && history[i].seq <= recovered))) historicalErrors.add(history[i].seq)

  const stop = latestDone >= 0 ? history[latestDone].payload?.stop_reason : ''
  const failedDone = latestDone > resolved && stop === 'provider_error'
  const currentModelError = latestModelError > resolved
  const currentFailure = failedDone || currentModelError || isModelError({ type: 'error', payload: { message: submissionError } })
  const incomplete = latestDone > resolved ? incompleteCompletion(history[latestDone].payload) : null
  const duplicateHistoryBanner = history.some(event => event.type === 'error'
    && historicalErrors.has(event.seq) && event.payload?.message === banner)
  const currentBannerEvent = history.some(event => event.type === 'error'
    && !historicalErrors.has(event.seq) && event.payload?.message === banner)
  const visibleBanner = bannerDismissed ? '' : submissionError || (!duplicateHistoryBanner || currentBannerEvent ? banner : '')

  return {
    label: busy ? sessionPhaseLabel(phase) : currentFailure ? '模型请求失败' : submissionError ? '消息发送失败' : incomplete?.label || '模型待命',
    state: busy ? 'busy' : currentFailure || submissionError ? 'error' : incomplete ? 'incomplete' : 'idle',
    historicalErrors,
    banner: visibleBanner,
  }
}

export function eventTime(value) {
  if (value === undefined || value === null || value === '') return ''
  const parsed = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value)
  if (!Number.isFinite(parsed.getTime())) return ''
  return parsed.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}
