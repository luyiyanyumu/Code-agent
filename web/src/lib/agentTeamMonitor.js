import { agentCanStop, agentTreeRows } from './agentTeam.js'

const resourceWait = agent => agentCanStop(agent) && ['waiting_files', 'waiting_dependencies'].includes(agent.phase)
const pathsOf = value => Array.isArray(value) ? value.filter(path => typeof path === 'string' && path) : []

export function agentResponsibility(agent, rows = []) {
  const declared = agent?.scope_mode !== 'legacy' && (['read_only', 'scoped_write', 'workspace_write'].includes(agent?.scope_mode) || Array.isArray(agent?.write_paths))
  const paths = declared ? pathsOf(agent.write_paths) : []
  const readOnly = declared && (agent.scope_mode === 'read_only' || agent.scope_mode !== 'workspace_write' && !paths.length)
  const workspace = declared && !readOnly && (agent.scope_mode === 'workspace_write' || paths.includes('.'))
  const ownership = agent?.file_ownership
  const blockers = (Array.isArray(ownership?.waiting_for) ? ownership.waiting_for : [])
    .filter(owner => owner && typeof owner.session_id === 'string')
    .map(owner => ({ session_id: owner.session_id, title: owner.title || owner.session_id || '其他对话中的执行者', paths: pathsOf(owner.paths) }))
  const dependencies = (Array.isArray(agent?.depends_on) ? agent.depends_on : [])
    .filter(target => target && typeof target.session_id === 'string' && target.session_id && Number.isInteger(target.activation_id) && target.activation_id > 0)
    .map(target => ({ ...target, title: rows.find(row => row.session_id === target.session_id)?.title || target.session_id }))
  return { declared, readOnly, paths: workspace ? ['.'] : paths, blockers, dependencies,
    reason: typeof agent?.delegation_reason === 'string' ? agent.delegation_reason : '',
    scopeLabel: !declared ? '未声明' : readOnly ? '只读' : workspace ? '整个工作区' : '限定文件与目录',
    ownershipLabel: readOnly ? '只读，不占用写入责任' : ownership?.held === true ? '已持有文件责任'
      : blockers.length ? '等待取得文件责任' : ownership && typeof ownership.held === 'boolean' ? '未持有文件责任'
        : !declared ? '未声明' : '暂无持有记录' }
}

export function agentTone(agent) {
  if (agent?.state === 'failed') return 'failed'
  if (agent?.pending_approval?.approval_id || agent?.pending_questions?.some(question => question.status === 'pending')
    || ['waiting_approval', 'waiting_question'].includes(agent?.phase)) return 'attention'
  if (agent?.state === 'completed') return 'completed'
  if (resourceWait(agent)) return 'waiting'
  if (agent?.state === 'queued') return 'queued'
  if (agent?.state === 'waiting') return 'waiting'
  if (agent?.state === 'running') return 'running'
  return 'idle'
}

export function teamSummary(team) {
  const workers = agentTreeRows(team).filter(agent => agent.role !== 'lead')
  return { total: workers.length,
    running: workers.filter(agent => agent.state === 'running' && agentTone(agent) !== 'attention').length,
    queued: workers.filter(agent => agent.state === 'queued' && !resourceWait(agent)).length,
    waiting: workers.filter(agent => agent.state === 'waiting' || resourceWait(agent) || agentTone(agent) === 'attention').length,
    completed: workers.filter(agent => agent.state === 'completed').length,
    attention: workers.filter(agent => ['attention', 'failed'].includes(agentTone(agent))).length,
    active: workers.filter(agentCanStop).length }
}

// Include ancestors of matches: filtering must not invent a new delegation edge.
export function monitorRows(team, query = '', filter = 'all') {
  const rows = agentTreeRows(team), byId = new Map(rows.map(row => [row.session_id, row]))
  const text = query.trim().toLocaleLowerCase()
  const matches = row => (!text || [row.title, row.last_task_message, row.current_tool, row.session_id, row.delegation_reason, ...pathsOf(row.write_paths)]
    .some(value => String(value || '').toLocaleLowerCase().includes(text)))
    && (filter === 'all' || filter === 'active' && agentCanStop(row)
      || filter === 'attention' && ['attention', 'failed'].includes(agentTone(row)))
  const matched = new Set(rows.filter(row => row.role !== 'lead' && matches(row)).map(row => row.session_id))
  const included = new Set(matched)
  if (team?.lead?.session_id) included.add(team.lead.session_id)
  for (const id of matched) {
    let parent = byId.get(id)?.parent_session_id
    const seen = new Set([id])
    while (parent && byId.has(parent) && !seen.has(parent)) {
      seen.add(parent); included.add(parent); parent = byId.get(parent)?.parent_session_id
    }
  }
  return { rows: rows.filter(row => included.has(row.session_id)).map(row => ({ ...row,
    contextOnly: row.role !== 'lead' && !matched.has(row.session_id) })), matched: matched.size }
}

export function layoutAgentTree(rows) {
  const width = 216, height = 92, stepX = 280, stepY = 124, pad = 28
  const byId = new Map(rows.map(row => [row.session_id, row])), children = new Map(), nodes = [], edges = []
  for (const row of rows) {
    if (row.parent_session_id !== row.session_id && byId.has(row.parent_session_id)) {
      const siblings = children.get(row.parent_session_id) || []
      siblings.push(row); children.set(row.parent_session_id, siblings)
    }
  }
  const seen = new Set()
  let leaf = 0
  function place(row, depth) {
    if (seen.has(row.session_id)) return null
    seen.add(row.session_id)
    const node = { ...row, x: pad + depth * stepX, y: pad, width, height }
    nodes.push(node)
    const descendants = (children.get(row.session_id) || []).map(child => place(child, depth + 1)).filter(Boolean)
    node.y = descendants.length ? (descendants[0].y + descendants.at(-1).y) / 2 : pad + leaf++ * stepY
    for (const child of descendants) {
      const x1 = node.x + width, y1 = node.y + height / 2, x2 = child.x, y2 = child.y + height / 2
      edges.push({ id: `${node.session_id}:${child.session_id}`, source: node.session_id, target: child.session_id,
        path: `M${x1},${y1} C${x1 + 32},${y1} ${x2 - 32},${y2} ${x2},${y2}` })
    }
    return node
  }
  for (const row of rows) if (!byId.has(row.parent_session_id)) place(row, 0)
  for (const row of rows) if (!seen.has(row.session_id)) place(row, 0)
  return { nodes, edges, width: Math.max(400, ...nodes.map(node => node.x + width + pad)),
    height: Math.max(260, ...nodes.map(node => node.y + height + pad)) }
}

// Evidence belongs to one activation. Missing markers never fall back to a
// different turn's history, even when the agent has been followed up again.
export function activationRecords(events, activationId) {
  const start = events.findIndex(event => event.type === 'session_meta'
    && event.payload?.agent_activation?.phase === 'started'
    && event.payload.agent_activation.activation_id === activationId)
  if (start < 0) return { records: [], boundary: 'missing', omitted: 0 }
  let end = events.length, boundary = 'open'
  for (let index = start + 1; index < events.length; index++) {
    const marker = events[index].type === 'session_meta' && events[index].payload?.agent_activation
    if (marker?.phase === 'started' || marker?.phase === 'finished' && marker.activation_id === activationId) {
      end = index; boundary = marker.phase === 'finished' ? 'complete' : 'interrupted'; break
    }
  }
  const records = events.slice(start + 1, end).filter(event => ['tool_use', 'tool_result', 'done', 'error'].includes(event.type))
  return { records: records.slice(-150), boundary, omitted: Math.max(0, records.length - 150) }
}

export function recordPresentation(event) {
  const payload = event.payload || {}
  if (event.type === 'tool_use') {
    const args = payload.call?.arguments || {}
    return { label: '调用工具', title: payload.call?.name || '工具', text: args.command || args.path || JSON.stringify(args, null, 2) }
  }
  if (event.type === 'tool_result') return { label: '工具返回', title: payload.name || '执行结果', text: String(payload.result ?? '') }
  if (event.type === 'error') return { label: '执行提示', title: payload.code || '提示', text: payload.message || '' }
  return { label: '本轮结束', title: payload.stop_reason || '结束', text: payload.text || '' }
}
