import { computed, onScopeDispose, ref, watch } from 'vue'
import { agentProgress, agentStateLabel } from './agentTeam.js'

export const EXECUTION_MODES = Object.freeze([
  { id: 'team', label: '团队执行', description: '由 Lead 管理，执行智能体按需拆分工作，遵守项目权限。适合日常修改。' },
  { id: 'isolated', label: '隔离执行', description: '在独立分支中修改，先批准计划，审查并合并后进入主干。' },
  { id: 'parallel', label: '隔离并行', description: '拆成多个独立分支同时执行，控制文件依赖，最后按顺序合并。' },
])
const STATE_LABELS = Object.freeze({ planning: '计划中', planned: '待批准计划', plan_failed: '计划需要调整', running: '执行中', queued: '排队中', waiting: '等待子任务', deferred: '等待前置任务', review: '待审查', merging: '合并中', merged: '已合并', partial: '部分合并', completed: '已完成', discarded: '已放弃', failed: '失败', cancelled: '已停止', idle: '待命' })
const ACTIVE = new Set(['planning', 'running', 'queued', 'waiting', 'deferred', 'merging'])
const ATTENTION = new Set(['planned', 'plan_failed', 'review', 'failed', 'partial', 'conflict'])
const COMPLETED = new Set(['completed', 'merged', 'discarded', 'cancelled'])

export function hasWorkApproval(item) {
  return workItemApprovals(item).length > 0
}
export function hasWorkQuestion(item) { return workItemQuestions(item).length > 0 }
export function workItemQuestions(item) {
  const seen = new Map(), groups = [], visited = new Set()
  function visit(value, fallback = {}) {
    if (!value || typeof value !== 'object' || visited.has(value)) return
    visited.add(value)
    const sessionId = value.session_id || fallback.session_id, treeId = value.tree_id || fallback.tree_id || (value.task_id ? `task:${value.task_id}` : '')
    for (const group of value.pending_questions || []) {
      const id = group.id || group.question_id, executor = group.session_id || sessionId
      if (!id || !executor || group.status !== 'pending') continue
      const key = `${executor}:${id}`, existing = seen.get(key)
      if (existing) {
        if (value.session_id === executor) { existing.executor_title = group.executor_title || value.title || existing.executor_title; if (group.tree_id || treeId) existing.tree_id = group.tree_id || treeId }
        continue
      }
      const record = { ...group, id, session_id: executor, ...(group.tree_id || treeId ? { tree_id: group.tree_id || treeId } : {}), executor_title: group.executor_title || value.title || fallback.title || '执行智能体' }
      seen.set(key, record); groups.push(record)
    }
    if (value.agent) visit(value.agent, { session_id: sessionId, tree_id: treeId, title: value.title })
    for (const agent of value.agents || []) visit(agent, { tree_id: treeId })
    for (const child of value.children || []) visit(child, { tree_id: child.task_id ? `task:${child.task_id}` : treeId })
    if (value.task) visit(value.task, { session_id: sessionId, tree_id: treeId })
    if (value.batch) visit(value.batch, { tree_id: treeId })
    for (const lane of value.lanes || []) visit(lane, { tree_id: lane.task_id ? `task:${lane.task_id}` : treeId })
  }
  visit(item)
  return groups
}
export function workItemSession(item) {
  const approval = workItemApprovals(item)[0]
  return approval?.session_id || workItemQuestions(item)[0]?.session_id || item?.session_id || item?.agent?.session_id || ''
}
export function workItemCanOpen(item) {
  if (!item || item.workspace_available === false || item.execution_managed === false || workItemLegacy(item)) return false
  if ((item.kind === 'task' || item.task_id) && ['merged', 'discarded'].includes(item.state || item.status)) return false
  return !!workItemSession(item)
}
export function workItemLegacy(item) {
  if (!item?.session_id || item.execution_managed === true) return false
  return item.execution_managed === false || ((item.kind === 'task' || item.task_id) && item.agent === null)
}
export function workItemTree(item, sessionId = workItemSession(item)) {
  const approval = workItemApprovals(item).find(value => value.session_id === sessionId)
  if (approval?.tree_id) return approval.tree_id
  const question = workItemQuestions(item).find(value => value.session_id === sessionId)
  for (const child of item?.children || []) {
    if (workItemSession(child) === sessionId || child.agents?.some(agent => agent.session_id === sessionId)
      || workItemApprovals(child).some(value => value.session_id === sessionId)) return child.tree_id || `task:${child.task_id}`
  }
  for (const lane of item?.batch?.lanes || []) {
    if (workItemSession(lane) === sessionId || workItemApprovals(lane).some(value => value.session_id === sessionId)) return `task:${lane.task_id}`
  }
  if (question?.tree_id) return question.tree_id
  return item?.tree_id || 'main'
}
export function workItemActive(item) {
  if (item?.active === true || item?.task?.active === true || item?.batch?.active === true) return true
  if (ACTIVE.has(item?.state || item?.status) && (item?.state || item?.status) !== 'deferred') return true
  if (['queued', 'running', 'waiting'].includes(item?.agent?.state)) return true
  if ((item?.agents || item?.task?.agents || []).some(agent => ['queued', 'running', 'waiting'].includes(agent.state))) return true
  return (item?.children || []).some(child => child.active === true)
}
export function workItemApprovals(item) {
  const approval = item?.pending_approval || item?.agent?.pending_approval
  const candidates = item?.pending_approvals?.length ? item.pending_approvals : approval ? [approval] : []
  return candidates.filter(value => value?.approval_id && (!value.status || value.status === 'pending'))
}
export function workItemState(item) {
  if (hasWorkApproval(item)) return '等待权限批准'
  if (hasWorkQuestion(item)) return '等待你回答'
  if (item.kind !== 'agent' && workItemActive(item) && ['review', 'review_pending', 'failed', 'partial'].includes(item.state)) {
    return item.phase === 'waiting_children' || item.phase === 'review_pending' || item.agent?.state === 'completed' ? '等待子任务' : '执行中'
  }
  if (item.kind === 'agent' && item.state === 'completed' && item.audit && (item.agent?.role || item.role) !== 'lead') {
    if (item.audit.decision === 'accepted') return '已验收'
    if (item.audit.decision === 'rejected') return '需返工'
    return '完成待审查'
  }
  if (item.kind === 'agent') return agentStateLabel({ ...(item.agent || item), state: item.state, phase: item.phase || item.agent?.phase })
  return STATE_LABELS[item.state] || item.state || '待命'
}
export function workItemGroup(item) {
  if (hasWorkApproval(item) || hasWorkQuestion(item)) return 'attention'
  if (workItemActive(item)) return 'active'
  if (ATTENTION.has(item.state) || (item.state === 'completed' && item.audit && (item.agent?.role || item.role) !== 'lead' && item.audit.decision !== 'accepted')) return 'attention'
  if (ACTIVE.has(item.state)) return 'active'
  return COMPLETED.has(item.state) ? 'completed' : 'other'
}
export function workItemMode(item) {
  if (item.kind === 'batch') return '隔离并行'
  if (item.kind === 'task') return '隔离执行'
  return item.agent?.role === 'lead' || item.role === 'lead' ? 'Lead' : '团队任务'
}
export function workItemProgress(item) {
  if (item.kind === 'batch') {
    const progress = item.progress || item.batch?.progress
    return progress ? `${progress.merged || 0}/${progress.total || 0} 项已合并${progress.running ? ` · ${progress.running} 项执行中` : ''}` : ''
  }
  const activeWorker = (item.agents || item.task?.agents || []).find(agent => ['queued', 'running', 'waiting'].includes(agent.state))
  const descriptor = { ...(item.agent || item), ...(item.progress && !Array.isArray(item.progress) ? item.progress : {}), ...(workItemActive(item) && activeWorker ? activeWorker : {}) }
  return agentProgress(descriptor).detail || (item.kind === 'task' && item.task?.changed_files ? `${item.task.changed_files} 个文件已修改` : '')
}
export function workItemVerification(item) {
  const verification = item?.verification || item?.task?.verification || item?.batch?.verification
  if (!verification) return ''
  if (verification.stale) return '代码已变化，需重新验证'
  if (item.kind === 'batch' && verification.total && verification.verified < verification.total) return `已验证 ${verification.verified || 0}/${verification.total} 项${verification.failed ? ` · ${verification.failed} 项未通过` : ''}`
  if (verification.ok === undefined || verification.ok === null) return ''
  return `${verification.ok ? '验证通过' : '验证未通过'}${verification.results?.length ? ` · ${verification.results.length} 条命令` : ''}`
}
export function filterWorkItems(items, filter = 'all', query = '') {
  const needle = String(query || '').trim().toLocaleLowerCase()
  return items.filter(item => (filter === 'all' || workItemGroup(item) === filter)
    && (!needle || [item.title, item.result, item.error, workItemMode(item)].some(value => String(value || '').toLocaleLowerCase().includes(needle))))
}

// One project-wide read supplies all execution modes. Events coalesce; hidden
// views stop polling, and late requests from an old project cannot replace state.
export function useWorkItems({ projectId, visible, client, pollMs = 15000 }) {
  const data = ref(null), loading = ref(false), creating = ref(false), error = ref(''), selectedId = ref('')
  const items = computed(() => data.value?.items || [])
  const selected = computed(() => items.value.find(item => item.id === selectedId.value) || null)
  const counts = computed(() => ({ total: items.value.length, active: items.value.filter(item => workItemGroup(item) === 'active').length,
    attention: items.value.filter(item => workItemGroup(item) === 'attention').length, completed: items.value.filter(item => workItemGroup(item) === 'completed').length }))
  let generation = 0, requestId = 0, disposed = false, timer = null, eventTimer = null, inflightVersion = null, refreshPending = false
  const current = (pid, version) => !disposed && projectId() === pid && generation === version
  function stopTimers() { clearTimeout(timer); clearTimeout(eventTimer); timer = eventTimer = null }
  function schedule() {
    clearTimeout(timer); timer = null
    if (!disposed && projectId() && visible()) timer = setTimeout(() => { timer = null; void refresh() }, pollMs)
  }
  async function refresh() {
    if (disposed || !projectId() || !visible()) return false
    if (inflightVersion === generation) { refreshPending = true; return false }
    clearTimeout(timer); timer = null
    const pid = projectId(), version = generation, request = ++requestId
    inflightVersion = version; loading.value = !data.value
    try {
      const result = await client.workItems(pid)
      if (!current(pid, version) || requestId !== request) return false
      data.value = result; error.value = ''
      if (selectedId.value && !result.items?.some(item => item.id === selectedId.value)) selectedId.value = ''
      return true
    } catch (cause) {
      if (current(pid, version) && requestId === request) error.value = cause.message || '读取任务失败，请重试。'
      return false
    } finally {
      if (current(pid, version) && requestId === request) {
        inflightVersion = null; loading.value = false
        if (refreshPending) { refreshPending = false; notify() }
        schedule()
      }
    }
  }
  function notify(change = {}) {
    if (disposed || !visible() || !projectId() || (change.project_id && change.project_id !== projectId())) return false
    if (eventTimer === null) eventTimer = setTimeout(() => { eventTimer = null; void refresh() }, 200)
    return true
  }
  async function create(payload) {
    if (disposed || !projectId() || creating.value) return null
    const pid = projectId(), version = generation
    creating.value = true; error.value = ''
    try {
      const response = await client.createWorkItem(pid, payload)
      if (!current(pid, version)) return null
      const item = response.item
      if (item?.id) {
        data.value = { ...(data.value || {}), items: [...items.value.filter(row => row.id !== item.id), item] }
        selectedId.value = item.id
      }
      notify()
      return response
    } catch (cause) {
      if (current(pid, version)) error.value = cause.message || '创建任务失败，请重试。'
      return null
    } finally { if (current(pid, version)) creating.value = false }
  }
  function select(id) {
    if (!items.value.some(item => item.id === id)) return false
    selectedId.value = id; return true
  }
  watch(projectId, () => {
    generation++; requestId++; stopTimers(); inflightVersion = null; refreshPending = false
    data.value = null; error.value = ''; selectedId.value = ''; loading.value = creating.value = false
    void refresh()
  }, { immediate: true })
  watch(visible, value => {
    stopTimers(); requestId++; inflightVersion = null; refreshPending = false; loading.value = false
    if (value) void refresh()
  })
  onScopeDispose(() => { disposed = true; generation++; requestId++; stopTimers() })
  return { data, items, selected, selectedId, counts, loading, creating, error, refresh, notify, create, select }
}
