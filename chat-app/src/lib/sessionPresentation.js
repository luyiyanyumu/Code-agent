// 当前请求状态只由本轮事件决定，历史错误仍留在时间线中供展开查看。
const MODEL_ERROR = /provider|模型|connection|connect|连接|API|key|认证|余额|额度|\b429\b|\b401\b/i

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

// Keep this standalone build's attribution contract aligned with the code UI.
// Explicit source metadata and the legacy auto flag identify runtime messages;
// text content and unknown source kinds never turn a human input into a notice.
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
  if (phase === 'model') return '模型响应中'
  if (phase === 'waiting_approval') return '等待你批准'
  return '执行中'
}

export function sessionPresentation(events, { busy = false, phase = '', banner = '', submissionError = '' } = {}) {
  const history = events || []
  const historicalErrors = new Set()
  let latestUser = -1, latestDone = -1, previousDone = -1, latestModelError = -1
  for (let i = 0; i < history.length; i += 1) {
    const event = history[i]
    if (event.type === 'user_message' && !internalMessagePresentation(event)) latestUser = i
    if (event.type === 'done') {
      previousDone = latestDone
      latestDone = i
    }
    if (isModelError(event)) latestModelError = i
  }
  // 新一轮已开始或后续有非失败收口，旧错误不再代表当前请求。
  let resolved = -1
  for (let i = history.length - 1; i >= 0; i -= 1) {
    const event = history[i]
    if ((event.type === 'user_message' && !internalMessagePresentation(event)) || (event.type === 'done' && isSuccessfulCompletion(event.payload))) {
      resolved = Math.max(resolved, i)
    }
    if (event.type === 'error' && i < resolved) historicalErrors.add(event.seq)
  }

  const stop = latestDone >= 0 ? history[latestDone].payload?.stop_reason : ''
  const failedDone = latestDone >= latestUser && (stop === 'provider_error'
    || (stop === 'error' && latestModelError > Math.max(previousDone, latestUser) && latestModelError < latestDone))
  const currentModelError = latestModelError > Math.max(latestUser, latestDone)
  const currentFailure = failedDone || currentModelError || isModelError({ type: 'error', payload: { message: submissionError } })
  const incomplete = latestDone > resolved ? incompleteCompletion(history[latestDone].payload) : null
  const duplicateHistoryBanner = history.some(event => event.type === 'error'
    && historicalErrors.has(event.seq) && event.payload?.message === banner)
  const currentBannerEvent = history.some(event => event.type === 'error'
    && !historicalErrors.has(event.seq) && event.payload?.message === banner)
  const visibleBanner = submissionError || (!duplicateHistoryBanner || currentBannerEvent ? banner : '')

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
