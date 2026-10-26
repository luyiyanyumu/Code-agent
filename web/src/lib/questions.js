// Coding questions are scoped to one execution session. The runtime snapshot
// determines which waiters are alive; transcript events retain their history.
const terminal = new Set(['answered', 'dismissed', 'timeout', 'cancelled', 'inactive'])
export function questionTime(value) {
  if (typeof value === 'number') return value < 1e12 ? value * 1000 : value
  return typeof value === 'string' ? Date.parse(value) : NaN
}
export function normalizeQuestionGroup(payload, sessionId, ts) {
  if (!payload || typeof payload !== 'object') return null
  const id = String(payload.question_id || payload.id || '')
  if (!id || (payload.session_id && payload.session_id !== sessionId)) return null
  const seen = new Set()
  const questions = (Array.isArray(payload.questions) ? payload.questions : []).filter(q => q && typeof q.id === 'string' && typeof q.question === 'string' && q.question.trim() && !seen.has(q.id) && seen.add(q.id)).map(q => ({
    ...q, options: (Array.isArray(q.options) ? q.options : []).map(option => typeof option === 'string' ? { label: option } : option).filter(option => option && typeof option.label === 'string' && option.label.trim()),
  }))
  if (!questions.length) return null
  const started = payload.ts ?? ts
  const expires = payload.expires_at || (Number(payload.timeout) > 0 && Number.isFinite(questionTime(started)) ? questionTime(started) + Number(payload.timeout) * 1000 : undefined)
  return { ...payload, id, question_id: id, session_id: sessionId, questions, status: terminal.has(payload.status) ? payload.status : 'pending', answers: payload.answers || [], ts: started, expires_at: expires }
}
export function updateQuestionEvent(groups, event, sessionId) {
  const payload = event.payload || {}, id = String(payload.question_id || '')
  if (event.type === 'question_request') {
    const group = normalizeQuestionGroup(payload, sessionId, event.ts)
    if (!group) return groups
    const old = groups.find(value => value.id === group.id)
    if (old && old.status !== 'pending') return groups
    return old ? groups.map(value => value.id === group.id ? { ...value, ...group } : value) : [...groups, group]
  }
  if (event.type === 'question_answered' && id) return finishQuestionGroup(groups, id, terminal.has(payload.status) ? payload.status : 'answered', payload.answers || [])
  return groups
}
export function finishQuestionGroup(groups, id, status, answers = []) {
  return groups.map(group => group.id === id ? { ...group, status, answers } : group)
}
export function reconcileQuestionSnapshot(groups, live, sessionId) {
  const pending = (Array.isArray(live) ? live : []).map(value => normalizeQuestionGroup(value, sessionId, value.ts)).filter(Boolean)
  const ids = new Set(pending.map(group => group.id))
  const result = groups.map(group => group.status === 'pending' && !ids.has(group.id) ? { ...group, status: 'inactive' } : group)
  for (const group of pending) {
    const index = result.findIndex(value => value.id === group.id)
    if (index < 0) result.push(group)
    else if (!terminal.has(result[index].status) || (result[index].status === 'inactive' && !result[index].unavailable)) result[index] = { ...result[index], ...group }
  }
  return result
}
export function questionPending(group, now = Date.now()) {
  const expires = questionTime(group.expires_at)
  return group.status === 'pending' && (!Number.isFinite(expires) || expires > now)
}
export function trimQuestionHistory(groups) {
  const history = groups.filter(group => group.status !== 'pending').slice(-50)
  const keep = new Set(history.map(group => group.id))
  return groups.filter(group => group.status === 'pending' || keep.has(group.id))
}
