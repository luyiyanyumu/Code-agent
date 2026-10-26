import { computed, onScopeDispose, ref, watch } from 'vue'
import { sessionPhaseLabel } from './sessionPresentation.js'

export const AGENT_STATE_LABELS = Object.freeze({ idle: '待命', queued: '排队', running: '执行中', waiting: '等待子任务', completed: '已完成', failed: '失败', cancelled: '已停止', retired: '已移除' })
export const agentStateLabel = agent => agent?.state === 'queued' ? sessionPhaseLabel(['waiting_files', 'waiting_dependencies'].includes(agent.phase) ? agent.phase : 'queued')
  : ['running', 'waiting'].includes(agent?.state) && agent?.phase
  ? sessionPhaseLabel(agent.phase) : AGENT_STATE_LABELS[agent?.state] || '待命'
export const agentCanStop = agent => ['queued', 'running', 'waiting'].includes(agent?.state)

function timestamp(value) {
  if (value === undefined || value === null || value === '') return NaN
  return typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : Date.parse(value)
}
export function durationLabel(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0))
  if (value < 60) return `${value}秒`
  if (value < 3600) return `${Math.floor(value / 60)}分${value % 60}秒`
  return `${Math.floor(value / 3600)}小时${Math.floor(value % 3600 / 60)}分`
}
export function agentProgress(agent, now = Date.now()) {
  if (!agent) return { detail: '', elapsed: '', recent: '', reason: '' }
  const active = agentCanStop(agent), started = timestamp(agent.started_at), progress = timestamp(agent.last_progress_at)
  const elapsedSeconds = active && Number.isFinite(started) ? (now - started) / 1000 : agent.elapsed_seconds
  const elapsed = elapsedSeconds !== undefined && elapsedSeconds !== null
    && (active || Number.isFinite(started) || Number(elapsedSeconds) > 0) ? durationLabel(elapsedSeconds) : ''
  const tool = typeof agent.current_tool === 'string' ? agent.current_tool : agent.current_tool?.name || ''
  const detail = [tool, elapsed && `已用 ${elapsed}`].filter(Boolean).join(' · ')
  return { detail, elapsed, recent: active && Number.isFinite(progress) ? `最近进展 ${durationLabel((now - progress) / 1000)}前` : '', reason: agent.blocked_reason || '' }
}
export function pendingTeamApprovals(team) {
  return agentTreeRows(team).filter(agent => agent.pending_approval?.approval_id
    && (!agent.pending_approval.status || agent.pending_approval.status === 'pending'))
}
export function pendingTeamQuestions(team) {
  const seen = new Set(), questions = []
  for (const agent of agentTreeRows(team)) {
    for (const group of agent.pending_questions || []) {
      const id = group.id || group.question_id, sessionId = agent.session_id
      if (group.session_id && group.session_id !== sessionId) continue
      if (!id || group.status !== 'pending' || !sessionId || seen.has(`${sessionId}:${id}`)) continue
      seen.add(`${sessionId}:${id}`)
      questions.push({ ...group, id, session_id: sessionId, executor_title: agent.role === 'lead' ? 'Lead' : agent.title || '执行智能体' })
    }
  }
  return questions
}

export function teamNoticePresentation(notice) {
  if (!notice || typeof notice !== 'object' || !notice.session_id) return null
  const state = AGENT_STATE_LABELS[notice.state] || '有新进展'
  return { sessionId: notice.session_id, title: notice.title || '子智能体', state,
    text: notice.error || notice.result || '', activationId: notice.activation_id }
}

// Server descriptors establish identity and parentage. UI depth never grants permissions.
export function agentTreeRows(team) {
  const lead = team?.lead
  const seen = new Set(), rows = [], byParent = new Map()
  const agents = (team?.agents || []).filter(agent => agent?.session_id && agent.role !== 'lead' && agent.state !== 'retired')
  for (const agent of agents) {
    const parent = agent.parent_session_id || lead?.session_id || ''
    if (!byParent.has(parent)) byParent.set(parent, [])
    byParent.get(parent).push(agent)
  }
  function visit(agent, indent) {
    if (!agent?.session_id || seen.has(agent.session_id)) return
    seen.add(agent.session_id)
    rows.push({ ...agent, indent })
    for (const child of byParent.get(agent.session_id) || []) visit(child, indent + 1)
  }
  if (lead?.session_id) visit(lead, 0)
  for (const agent of agents) if (!seen.has(agent.session_id)) visit(agent, 1)
  return rows
}

// Each project/tree starts on Lead. Polling pauses on hidden panels and discards
// late requests from a previous project without interrupting server-side jobs.
export function useProjectAgentTeam({ projectId, tree, visible, leadSessionId, conversationId = leadSessionId, client, pollMs = 15000 }) {
  const team = ref(null), loading = ref(false), error = ref(''), acting = ref('')
  let generation = 0, requestId = 0, timer = null, eventTimer = null, disposed = false
  let refreshingVersion = null, refreshPending = false
  const rows = computed(() => agentTreeRows(team.value))
  const selectedId = computed(() => conversationId() || leadSessionId() || '')
  const selected = computed(() => team.value?.lead?.session_id === selectedId.value ? team.value.lead : null)
  const sessionId = selectedId
  const current = (pid, tid, version) => !disposed && projectId() === pid && tree() === tid && version === generation
  function stopTimer() { clearTimeout(timer); timer = null }
  function stopEventTimer() { clearTimeout(eventTimer); eventTimer = null }
  function schedule() {
    stopTimer()
    if (!disposed && projectId() && visible()) timer = setTimeout(() => { timer = null; void refresh() }, pollMs)
  }
  async function refresh() {
    if (disposed || !projectId() || !selectedId.value || !visible()) return false
    // A slow roster read must still be allowed to finish while tool events keep
    // arriving. Request replacement on every event would hide all its results.
    if (refreshingVersion === generation) { refreshPending = true; return false }
    stopTimer()
    const pid = projectId(), tid = tree(), version = generation, request = ++requestId
    refreshingVersion = version
    loading.value = !team.value
    try {
      const result = await client.agentTeam(pid, tid, selectedId.value)
      if (!current(pid, tid, version) || request !== requestId) return false
      if (result?.lead?.session_id !== selectedId.value) throw new Error('后台子任务不属于当前主对话，请重新读取')
      team.value = result
      error.value = ''
      return true
    } catch (cause) {
      if (current(pid, tid, version) && request === requestId) error.value = cause.message || '读取智能体失败'
      return false
    } finally {
      if (current(pid, tid, version) && request === requestId) {
        refreshingVersion = null
        loading.value = false
        if (refreshPending) { refreshPending = false; notify() }
        schedule()
      }
    }
  }
  // Project-wide change notifications arrive on the selected session's socket.
  // Coalesce bursts so streamed tool progress does not start a request per frame.
  function notify(change = {}) {
    if (disposed || !projectId() || !visible() || (change.project_id && change.project_id !== projectId()) ||
        (change.lead_session_id && change.lead_session_id !== selectedId.value) ||
        (!change.lead_session_id && change.tree_id && change.tree_id !== tree())) return false
    if (eventTimer === null) eventTimer = setTimeout(() => { eventTimer = null; void refresh() }, 200)
    return true
  }
  // A worker is a background operation, never a human-facing composer.
  function select(id) { return !!id && id === selectedId.value }
  function returnLead() {}
  async function action(id, decision, reason = '', activationId = '') {
    if (disposed || !projectId() || acting.value || !rows.value.some(agent => agent.session_id === id)) return false
    const pid = projectId(), tid = tree(), version = generation
    acting.value = id
    try {
      if (decision === 'stop') await client.stopAgent(pid, id, selectedId.value)
      else await client.auditAgent(pid, id, decision, reason, activationId)
      if (!current(pid, tid, version)) return false
      await refresh()
      return true
    } catch (cause) {
      if (current(pid, tid, version)) error.value = cause.message || '智能体操作失败'
      return false
    } finally { if (current(pid, tid, version)) acting.value = '' }
  }
  watch([projectId, tree, conversationId, visible], (next, previous) => {
    const identityChanged = !previous || next.slice(0, 3).some((value, index) => value !== previous[index])
    stopTimer(); stopEventTimer(); refreshingVersion = null; refreshPending = false
    if (identityChanged) {
      generation++; requestId++; team.value = null; loading.value = false; error.value = ''; acting.value = ''
    }
    if (next[3]) void refresh()
    else { requestId++; loading.value = false }
  }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => { disposed = true; generation += 1; requestId += 1; stopTimer(); stopEventTimer() })
  return { team, rows, selected, sessionId, selectedId, loading, error, acting, refresh, notify, select, returnLead, action }
}
