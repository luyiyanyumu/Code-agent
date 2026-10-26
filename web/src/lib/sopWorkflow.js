// Project SOP data and graph rules. No model requests or filesystem writes happen here.
export const WORKFLOW_NODE_WIDTH = 240
export const WORKFLOW_NODE_HEIGHT = 190
export const WORKFLOW_PORT_Y = 92
export const WORKFLOW_ROUTE_SIDE_GUTTER = 28
export const WORKFLOW_ROUTE_BOTTOM_GUTTER = 44
export const WORKFLOW_COLUMN_GAP = 104
export const WORKFLOW_ROW_GAP = 48
export const WORKFLOW_LIMITS = Object.freeze({ nodes: 128, edges: 512, title: 120, description: 4000, output: 40000, id: 64, coordinate: 100000, minZoom: 0.05, maxZoom: 2.5, bytes: 2 * 1024 * 1024 })

export const NODE_TYPES = Object.freeze([
  { type: 'requirements', title: '需求确认', color: '#6d73f5', objective: '明确使用者、问题和核心需求。', deliverable: '需求清单与优先级', acceptance: '范围清晰，每项需求都有完成标准。' },
  { type: 'ui_design', title: 'UI与流程', color: '#a56af0', objective: '设计核心操作流程和界面状态。', deliverable: '流程图、页面设计与组件规范', acceptance: '核心场景及加载、空数据、失败状态齐全。' },
  { type: 'architecture', title: '技术方案', color: '#419be0', objective: '明确模块、接口、数据与部署方式。', deliverable: '技术方案和关键验证结果', acceptance: '关键风险已验证，接口与模块职责明确。' },
  { type: 'planning', title: '任务拆分', color: '#d4a139', objective: '将需求拆成可以独立验收的任务。', deliverable: '任务列表与完成条件', acceptance: '每项任务具备输入、输出和验收标准。' },
  { type: 'implementation', title: '开发实现', color: '#32a89d', objective: '先跑通核心场景，再补齐功能。', deliverable: '可运行的实现与代码变更', acceptance: '核心流程完整，代码可审查。' },
  { type: 'verification', title: '测试验收', color: '#dc8b54', objective: '验证功能、异常、性能和界面。', deliverable: '测试结果与验收记录', acceptance: '需求验收通过，已有功能无回归。' },
  { type: 'release', title: '发布交付', color: '#4caa69', objective: '准备构建、配置、变更说明和回退方式。', deliverable: '交付版本与发布记录', acceptance: '发布条件满足，回退方式可用。' },
  { type: 'maintenance', title: '维护迭代', color: '#8090aa', objective: '根据使用反馈和运行数据确定改进。', deliverable: '问题记录与下一轮计划', acceptance: '重要问题有结论，迭代任务已排序。' },
  { type: 'custom', title: '自定义步骤', color: '#8a81a6', objective: '', deliverable: '', acceptance: '' },
].map((metadata) => Object.freeze(metadata)))

const TYPES = new Map(NODE_TYPES.map((metadata) => [metadata.type, metadata]))
const STATUSES = new Set(['pending', 'active', 'done'])
const TEXT_FIELDS = ['title', 'objective', 'deliverable', 'acceptance', 'output']
const NODE_FIELDS = new Set(['id', 'type', 'title', 'x', 'y', 'objective', 'deliverable', 'acceptance', 'status', 'output'])
const EDGE_FIELDS = new Set(['id', 'source', 'target'])
const DOCUMENT_FIELDS = new Set(['schema_version', 'name', 'nodes', 'edges', 'viewport', 'revision', 'updated_at'])
const VIEWPORT_FIELDS = new Set(['x', 'y', 'zoom'])
const validIdentifier = (id) => typeof id === 'string' && id.length <= WORKFLOW_LIMITS.id && /^[A-Za-z0-9_-]+$/.test(id)
const validCoordinate = (value) => Number.isFinite(value) && Math.abs(value) <= WORKFLOW_LIMITS.coordinate
const hasUnknownFields = (object, allowed) => Object.keys(object).some((field) => !allowed.has(field))

function newId(prefix) {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 11)}`
  return `${prefix}-${suffix}`
}

function copyWorkflow(workflow) {
  return { ...workflow, nodes: workflow.nodes.map((node) => ({ ...node })), edges: workflow.edges.map((edge) => ({ ...edge })), viewport: { ...workflow.viewport } }
}

function nodeMap(workflow) {
  return new Map(workflow.nodes.map((node) => [node.id, node]))
}

function requireWorkflow(workflow) {
  const result = validateWorkflow(workflow)
  if (!result.valid) throw new Error(result.errors[0])
}

function predecessors(workflow, id) {
  return workflow.edges.filter((edge) => edge.target === id).map((edge) => edge.source)
}

function descendants(workflow, id) {
  const seen = new Set()
  const queue = [id]
  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    for (const edge of workflow.edges) {
      if (edge.source !== queue[cursor] || seen.has(edge.target)) continue
      seen.add(edge.target)
      queue.push(edge.target)
    }
  }
  return seen
}

function resetStatuses(workflow, ids) {
  for (const node of workflow.nodes) if (ids.has(node.id)) node.status = 'pending'
}

function topologicalOrder(workflow) {
  const indegree = new Map(workflow.nodes.map((node) => [node.id, 0]))
  const children = new Map(workflow.nodes.map((node) => [node.id, []]))
  for (const edge of workflow.edges) {
    indegree.set(edge.target, indegree.get(edge.target) + 1)
    children.get(edge.source).push(edge.target)
  }
  for (const list of children.values()) list.sort()
  const available = [...indegree].filter(([, count]) => count === 0).map(([id]) => id).sort()
  const order = []
  while (available.length) {
    const id = available.shift()
    order.push(id)
    for (const child of children.get(id)) {
      indegree.set(child, indegree.get(child) - 1)
      if (indegree.get(child) === 0) { available.push(child); available.sort() }
    }
  }
  return order
}

function workflowComponents(ids, parents, children) {
  const unseen = new Set(ids), components = []
  for (const root of [...ids].sort()) {
    if (!unseen.has(root)) continue
    const members = [], queue = [root]
    unseen.delete(root)
    for (let cursor = 0; cursor < queue.length; cursor += 1) {
      const id = queue[cursor]
      members.push(id)
      for (const neighbor of [...parents.get(id), ...children.get(id)]) {
        if (!unseen.delete(neighbor)) continue
        queue.push(neighbor)
      }
    }
    members.sort()
    components.push(members)
  }
  return components.sort((a, b) => b.length - a.length || a[0].localeCompare(b[0]))
}

export function workflowNodeOrder(workflow) {
  // Presentation order also works during incomplete text edits. Graph structure
  // alone determines numbering; a malformed graph retains the saved array order.
  const nodes = Array.isArray(workflow?.nodes) ? workflow.nodes : []
  const original = nodes.map((node) => node?.id)
  if (!Array.isArray(workflow?.edges) || original.some((id) => typeof id !== 'string') || new Set(original).size !== original.length) return original
  const ids = new Set(original)
  if (workflow.edges.some((edge) => !edge || !ids.has(edge.source) || !ids.has(edge.target) || edge.source === edge.target)) return original
  const sorted = topologicalOrder(workflow)
  if (sorted.length !== nodes.length) return original
  const parents = new Map(original.map((id) => [id, []])), children = new Map(original.map((id) => [id, []]))
  for (const { source, target } of workflow.edges) { parents.get(target).push(source); children.get(source).push(target) }
  // Number the main flow continuously before detached steps, matching layout.
  return workflowComponents(original, parents, children).flatMap((members) => {
    const component = new Set(members)
    return sorted.filter((id) => component.has(id))
  })
}

export function createWorkflowNode(type = 'custom', x = 40, y = 60) {
  const metadata = TYPES.get(type)
  if (!metadata) throw new Error('请选择有效的步骤类型')
  if (![x, y].every(validCoordinate)) throw new Error('节点位置必须在-100000到100000之间')
  return { id: newId('node'), type, title: metadata.title, x, y, objective: metadata.objective, deliverable: metadata.deliverable, acceptance: metadata.acceptance, status: 'pending', output: '' }
}

export function createSopWorkflow() {
  const nodes = NODE_TYPES.filter((metadata) => metadata.type !== 'custom').map((metadata, index) => {
    return { ...createWorkflowNode(metadata.type, 40 + index * (WORKFLOW_NODE_WIDTH + WORKFLOW_COLUMN_GAP), 60), id: metadata.type }
  })
  const edges = nodes.slice(1).map((node, index) => ({ id: `edge_${index + 1}`, source: nodes[index].id, target: node.id }))
  return { schema_version: 1, name: '项目开发 SOP', nodes, edges, viewport: { x: 0, y: 0, zoom: 1 }, revision: 0, updated_at: '' }
}

export function validateWorkflow(workflow) {
  const errors = []
  if (!workflow || typeof workflow !== 'object' || Array.isArray(workflow)) return { valid: false, errors: ['工作流必须是一个对象'] }
  if (hasUnknownFields(workflow, DOCUMENT_FIELDS)) errors.push('工作流包含不支持的字段')
  if (workflow.schema_version !== 1) errors.push('不支持此工作流版本')
  if (typeof workflow.name !== 'string' || !workflow.name.trim() || workflow.name.length > 120) errors.push('工作流名称不能为空，且不能超过120个字符')
  if (!Number.isSafeInteger(workflow.revision) || workflow.revision < 0) errors.push('工作流版本号必须是非负安全整数')
  if (typeof workflow.updated_at !== 'string' || (workflow.updated_at !== '' && !Number.isFinite(Date.parse(workflow.updated_at)))) errors.push('工作流更新时间无效')
  const viewport = workflow.viewport
  if (!viewport || typeof viewport !== 'object' || Array.isArray(viewport) || ![viewport.x, viewport.y].every(validCoordinate) || !Number.isFinite(viewport.zoom) || viewport.zoom < WORKFLOW_LIMITS.minZoom || viewport.zoom > WORKFLOW_LIMITS.maxZoom) errors.push('画布位置或缩放无效')
  else if (hasUnknownFields(viewport, VIEWPORT_FIELDS)) errors.push('画布包含不支持的字段')
  if (!Array.isArray(workflow.nodes) || !Array.isArray(workflow.edges)) return { valid: false, errors: [...errors, '节点和连线必须是数组'] }
  if (workflow.nodes.length > WORKFLOW_LIMITS.nodes) errors.push('工作流最多支持128个步骤')
  if (workflow.edges.length > WORKFLOW_LIMITS.edges) errors.push('工作流最多支持512条连线')
  const ids = new Set()
  let graphSafe = true
  for (const node of workflow.nodes) {
    if (!node || typeof node !== 'object' || Array.isArray(node)) { errors.push('节点格式无效'); graphSafe = false; continue }
    if (hasUnknownFields(node, NODE_FIELDS)) errors.push('节点包含不支持的字段')
    if (!validIdentifier(node.id) || ids.has(node.id)) { errors.push('节点标识须为1到64位字母、数字、下划线或连字符，且不能重复'); graphSafe = false }
    ids.add(node.id)
    if (!TYPES.has(node.type)) errors.push('节点类型无效')
    if (![node.x, node.y].every(validCoordinate)) errors.push('节点位置必须在-100000到100000之间')
    if (!STATUSES.has(node.status)) errors.push('节点状态无效')
    for (const field of TEXT_FIELDS) if (typeof node[field] !== 'string') errors.push(`节点${field}必须是文本`)
    if (typeof node.title === 'string' && (!node.title.trim() || node.title.length > 120)) errors.push('步骤名称不能为空，且不能超过120个字符')
    for (const field of ['objective', 'deliverable', 'acceptance']) if (typeof node[field] === 'string' && node[field].length > WORKFLOW_LIMITS.description) errors.push('步骤目标、交付物或验收条件不能超过4000个字符')
    if (typeof node.output === 'string' && node.output.length > WORKFLOW_LIMITS.output) errors.push('产物或验收记录不能超过40000个字符')
    if (node.status === 'done' && (typeof node.output !== 'string' || !node.output.trim())) errors.push(`“${node.title || '步骤'}”完成前需要填写产物或验收记录`)
  }
  const edgeIds = new Set()
  const pairs = new Set()
  for (const edge of workflow.edges) {
    if (!edge || typeof edge !== 'object' || Array.isArray(edge)) { errors.push('连线格式无效'); graphSafe = false; continue }
    if (hasUnknownFields(edge, EDGE_FIELDS)) errors.push('连线包含不支持的字段')
    if (!validIdentifier(edge.id) || edgeIds.has(edge.id)) errors.push('连线标识须为1到64位字母、数字、下划线或连字符，且不能重复')
    edgeIds.add(edge.id)
    if (!ids.has(edge.source) || !ids.has(edge.target)) { errors.push('连线引用了不存在的节点'); graphSafe = false }
    if (edge.source === edge.target) { errors.push('节点不能连接自身'); graphSafe = false }
    // JSON tuple encoding avoids collisions in imported IDs containing separators.
    const pair = JSON.stringify([edge.source, edge.target])
    if (pairs.has(pair)) errors.push('同一对节点不能重复连线')
    pairs.add(pair)
  }
  if (graphSafe) {
    if (topologicalOrder(workflow).length !== workflow.nodes.length) errors.push('工作流不能包含循环依赖')
    const byId = nodeMap(workflow)
    for (const node of workflow.nodes) {
      if (node.status !== 'active' && node.status !== 'done') continue
      if (predecessors(workflow, node.id).some((id) => byId.get(id).status !== 'done')) errors.push(`“${node.title}”的前置步骤尚未完成`)
    }
  }
  if (!errors.length) {
    // Match the endpoint's UTF-8 request-size guard, including its envelope.
    const { revision, updated_at, ...document } = workflow
    const bytes = new TextEncoder().encode(JSON.stringify({ expected_revision: revision, workflow: document })).length
    if (bytes > WORKFLOW_LIMITS.bytes) errors.push('工作流内容过大，请精简节点记录')
  }
  return { valid: errors.length === 0, errors: [...new Set(errors)] }
}

export function canConnect(workflow, source, target) {
  const checked = validateWorkflow(workflow)
  if (!checked.valid) return { valid: false, error: checked.errors[0] }
  const byId = nodeMap(workflow)
  if (!byId.has(source) || !byId.has(target)) return { valid: false, error: '请选择存在的两个步骤' }
  if (source === target) return { valid: false, error: '节点不能连接自身' }
  if (workflow.edges.some((edge) => edge.source === source && edge.target === target)) return { valid: false, error: '这两个步骤已经连接' }
  if (descendants(workflow, target).has(source)) return { valid: false, error: '这条连线会形成循环，请调整步骤顺序' }
  if (workflow.edges.length >= WORKFLOW_LIMITS.edges) return { valid: false, error: '工作流最多支持512条连线' }
  return { valid: true, error: '' }
}

export function connectNodes(workflow, source, target) {
  const checked = canConnect(workflow, source, target)
  if (!checked.valid) throw new Error(checked.error)
  const next = copyWorkflow(workflow)
  next.edges.push({ id: newId('edge'), source, target })
  // A newly added prerequisite changes the evidence needed for this branch.
  resetStatuses(next, new Set([target, ...descendants(next, target)]))
  return next
}

export function removeNode(workflow, id) {
  requireWorkflow(workflow)
  const next = copyWorkflow(workflow)
  if (!next.nodes.some((node) => node.id === id)) return next
  const affected = descendants(next, id)
  next.nodes = next.nodes.filter((node) => node.id !== id)
  next.edges = next.edges.filter((edge) => edge.source !== id && edge.target !== id)
  resetStatuses(next, affected)
  return next
}

export function setNodeStatus(workflow, id, status) {
  requireWorkflow(workflow)
  if (!STATUSES.has(status)) throw new Error('步骤状态无效')
  const byId = nodeMap(workflow)
  const node = byId.get(id)
  if (!node) throw new Error('步骤不存在')
  if (status === 'active' || status === 'done') {
    const blocked = predecessors(workflow, id).filter((parentId) => byId.get(parentId).status !== 'done')
    if (blocked.length) throw new Error(`请先完成前置步骤：${blocked.map((parentId) => byId.get(parentId).title).join('、')}`)
  }
  if (status === 'done' && !node.output.trim()) throw new Error('请先填写产物或验收记录，再确认完成')
  const next = copyWorkflow(workflow)
  next.nodes.find((entry) => entry.id === id).status = status
  if (node.status === 'done' && status !== 'done') resetStatuses(next, descendants(next, id))
  return next
}

export function readyNodeIds(workflow) {
  requireWorkflow(workflow)
  const byId = nodeMap(workflow)
  return workflow.nodes.filter((node) => node.status === 'pending' && predecessors(workflow, node.id).every((id) => byId.get(id).status === 'done')).map((node) => node.id)
}

export function arrangeWorkflow(workflow, _options = {}) {
  requireWorkflow(workflow)
  const next = copyWorkflow(workflow)
  if (!workflow.nodes.length) return next
  const order = topologicalOrder(workflow)
  const parents = new Map(order.map((id) => [id, []]))
  const children = new Map(order.map((id) => [id, []]))
  for (const { source, target } of workflow.edges) {
    parents.get(target).push(source)
    children.get(source).push(target)
  }
  for (const list of [...parents.values(), ...children.values()]) list.sort()
  // Keep disconnected workflows together instead of mixing all their roots into
  // one tall rank. Main connected flows precede small independent groups.
  const components = workflowComponents(order, parents, children)
  const layerOf = new Map()
  for (const id of order) {
    layerOf.set(id, Math.max(0, ...parents.get(id).map((parent) => layerOf.get(parent) + 1)))
  }
  const positions = new Map()
  const rowStep = WORKFLOW_NODE_HEIGHT + WORKFLOW_ROW_GAP
  let groupTop = 60
  for (const members of components) {
    const ranks = []
    for (const id of members) (ranks[layerOf.get(id)] ||= []).push(id)
    const rowOf = new Map()
    const maxRows = Math.max(...ranks.map((rank) => rank.length))
    ranks.forEach((rank) => rank.forEach((id, index) => rowOf.set(id, (index + (maxRows - rank.length) / 2) * rowStep)))
    const mean = (ids) => ids.reduce((sum, id) => sum + rowOf.get(id), 0) / ids.length
    const alignRank = (rank, neighbors) => {
      // Barycentric sweeps place a split around its parent and a join between
      // its inputs. Stable ID ties make import ordering irrelevant.
      const desired = new Map(rank.map((id) => [id, neighbors.get(id).length ? mean(neighbors.get(id)) : rowOf.get(id)]))
      rank.sort((a, b) => desired.get(a) - desired.get(b) || a.localeCompare(b))
      const slots = rank.map((id, index) => desired.get(id) - index * rowStep)
      // Isotonic regression enforces the card gap with the smallest movement,
      // so adjacent branches stay compact without pushing a merge off-center.
      const blocks = []
      for (let index = 0; index < slots.length; index += 1) {
        blocks.push({ start: index, end: index, sum: slots[index], count: 1 })
        while (blocks.length > 1) {
          const last = blocks.at(-1), previous = blocks.at(-2)
          if (previous.sum / previous.count <= last.sum / last.count) break
          blocks.splice(-2, 2, { start: previous.start, end: last.end, sum: previous.sum + last.sum, count: previous.count + last.count })
        }
      }
      for (const block of blocks) for (let index = block.start; index <= block.end; index += 1) rowOf.set(rank[index], block.sum / block.count + index * rowStep)
    }
    for (let sweep = 0; sweep < 8; sweep += 1) {
      for (let layer = 1; layer < ranks.length; layer += 1) alignRank(ranks[layer], parents)
      for (let layer = ranks.length - 2; layer >= 0; layer -= 1) alignRank(ranks[layer], children)
    }
    const minY = Math.min(...members.map((id) => rowOf.get(id)))
    const maxY = Math.max(...members.map((id) => rowOf.get(id)))
    for (const id of members) positions.set(id, {
      x: 40 + layerOf.get(id) * (WORKFLOW_NODE_WIDTH + WORKFLOW_COLUMN_GAP),
      y: Math.round((groupTop + rowOf.get(id) - minY) * 1000) / 1000,
    })
    groupTop += maxY - minY + WORKFLOW_NODE_HEIGHT + 96
  }
  next.nodes = next.nodes.map((node) => ({ ...node, ...positions.get(node.id) }))
  return next
}

export function workflowPrompt(workflow, id) {
  requireWorkflow(workflow)
  const byId = nodeMap(workflow)
  const node = byId.get(id)
  if (!node) throw new Error('步骤不存在')
  const input = predecessors(workflow, id).map((parentId) => {
    const parent = byId.get(parentId)
    return `### ${parent.title}\n状态：${parent.status === 'done' ? '已完成' : parent.status === 'active' ? '进行中' : '待处理'}\n${parent.output.trim() || '尚未填写产物'}`
  }).join('\n\n')
  return [
    `项目工作流：${workflow.name}`,
    `当前步骤：${node.title}`,
    `目标：${node.objective.trim() || '待补充'}`,
    `交付物：${node.deliverable.trim() || '待补充'}`,
    `验收条件：${node.acceptance.trim() || '待补充'}`,
    input ? `前置步骤的产物：\n${input}` : '前置步骤：无',
    '请根据当前项目和以上信息协助完成此步骤，给出可检查的产物，并明确尚未验证的内容。不要声称执行了未执行的操作；步骤状态由用户验收后确认。',
  ].join('\n\n')
}

const ROUTE_CLEARANCE = 18
const ROUTE_RADIUS = 16

function cardBounds(node, clearance = 0) {
  return { left: node.x - clearance, right: node.x + WORKFLOW_NODE_WIDTH + clearance, top: node.y - clearance, bottom: node.y + WORKFLOW_NODE_HEIGHT + clearance }
}

function segmentCrossesCard(a, b, card) {
  // Clip against the open interior; traveling along a clearance boundary is safe.
  let low = 0, high = 1
  for (const [start, delta, min, max] of [[a[0], b[0] - a[0], card.left + 0.001, card.right - 0.001], [a[1], b[1] - a[1], card.top + 0.001, card.bottom - 0.001]]) {
    if (Math.abs(delta) < 0.000001) { if (start < min || start > max) return false; continue }
    const enter = (min - start) / delta, leave = (max - start) / delta
    low = Math.max(low, Math.min(enter, leave))
    high = Math.min(high, Math.max(enter, leave))
    if (low > high) return false
  }
  return low <= high
}

function curveIsClear(start, controlA, controlB, end, cards) {
  const left = Math.min(start[0], controlA[0], controlB[0], end[0]), right = Math.max(start[0], controlA[0], controlB[0], end[0])
  const top = Math.min(start[1], controlA[1], controlB[1], end[1]), bottom = Math.max(start[1], controlA[1], controlB[1], end[1])
  const nearby = cards.filter((card) => card.left < right && card.right > left && card.top < bottom + 0.001 && card.bottom > top - 0.001)
  if (!nearby.length) return true
  if (start[1] === end[1] && controlA[1] === end[1] && controlB[1] === end[1]) return !nearby.some((card) => segmentCrossesCard(start, end, card))
  let previous = start
  for (let index = 1; index <= 80; index += 1) {
    const t = index / 80, u = 1 - t
    const point = [0, 1].map((axis) => u ** 3 * start[axis] + 3 * u ** 2 * t * controlA[axis] + 3 * u * t ** 2 * controlB[axis] + t ** 3 * end[axis])
    if (nearby.some((card) => segmentCrossesCard(previous, point, card))) return false
    previous = point
  }
  return true
}

function roundedRoute(points, cornerRadius = ROUTE_RADIUS) {
  const compact = []
  for (const point of points) {
    if (compact.length && point[0] === compact.at(-1)[0] && point[1] === compact.at(-1)[1]) continue
    while (compact.length > 1) {
      const a = compact.at(-2), b = compact.at(-1)
      if ((a[0] !== b[0] || b[0] !== point[0]) && (a[1] !== b[1] || b[1] !== point[1])) break
      compact.pop()
    }
    compact.push(point)
  }
  const number = (value) => Math.round(value * 1000) / 1000
  const pair = (point) => point.map(number).join(' ')
  let path = `M ${pair(compact[0])}`, current = compact[0]
  const line = (end) => {
    if (end[0] === current[0] && end[1] === current[1]) return
    const a = [0, 1].map((axis) => current[axis] + (end[axis] - current[axis]) / 3)
    const b = [0, 1].map((axis) => current[axis] + (end[axis] - current[axis]) * 2 / 3)
    path += ` C ${pair(a)}, ${pair(b)}, ${pair(end)}`
    current = end
  }
  for (let index = 1; index < compact.length - 1; index += 1) {
    const previous = compact[index - 1], corner = compact[index], next = compact[index + 1]
    const beforeLength = Math.hypot(corner[0] - previous[0], corner[1] - previous[1])
    const afterLength = Math.hypot(next[0] - corner[0], next[1] - corner[1])
    const radius = Math.min(cornerRadius, beforeLength / 2, afterLength / 2)
    const before = [0, 1].map((axis) => corner[axis] + (previous[axis] - corner[axis]) * radius / beforeLength)
    const after = [0, 1].map((axis) => corner[axis] + (next[axis] - corner[axis]) * radius / afterLength)
    line(before)
    const a = [0, 1].map((axis) => before[axis] + (corner[axis] - before[axis]) * 0.552285)
    const b = [0, 1].map((axis) => after[axis] + (corner[axis] - after[axis]) * 0.552285)
    path += ` C ${pair(a)}, ${pair(b)}, ${pair(after)}`
    current = after
  }
  line(compact.at(-1))
  return path
}

function obstacleRoute(start, end, cards) {
  // Most skipped stages and backward links need one clear horizontal lane.
  // Try those short candidates first; the visibility search handles cards that
  // also block the departure or arrival corridor after manual dragging.
  const laneYs = [...new Set([start[1], end[1], ...cards.flatMap((card) => [card.top, card.bottom])])]
  laneYs.sort((a, b) => Math.abs(start[1] - a) + Math.abs(end[1] - a) - Math.abs(start[1] - b) - Math.abs(end[1] - b) || a - b)
  for (const y of laneYs) {
    const route = [start, [start[0], y], [end[0], y], end]
    if (route.slice(1).every((point, index) => !cards.some((card) => segmentCrossesCard(route[index], point, card)))) return route
  }
  // A rectilinear visibility grid uses only card boundaries and port coordinates.
  // A* favors short paths and adds a bend cost, avoiding unnecessary zigzags.
  const xs = [...new Set([start[0], end[0], ...cards.flatMap((card) => [card.left, card.right])])].sort((a, b) => a - b)
  const ys = [...new Set([start[1], end[1], ...cards.flatMap((card) => [card.top, card.bottom])])].sort((a, b) => a - b)
  const width = xs.length
  const startIndex = ys.indexOf(start[1]) * width + xs.indexOf(start[0])
  const endIndex = ys.indexOf(end[1]) * width + xs.indexOf(end[0])
  const point = (index) => [xs[index % width], ys[Math.floor(index / width)]]
  const heuristic = (index) => { const [x, y] = point(index); return Math.abs(x - end[0]) + Math.abs(y - end[1]) }
  const queue = []
  const push = (item) => {
    queue.push(item)
    let index = queue.length - 1
    while (index > 0) {
      const parent = (index - 1) >> 1
      if (queue[parent].score <= item.score) break
      queue[index] = queue[parent]; index = parent
    }
    queue[index] = item
  }
  const pop = () => {
    const first = queue[0], last = queue.pop()
    if (queue.length) {
      let index = 0
      while (index * 2 + 1 < queue.length) {
        let child = index * 2 + 1
        if (child + 1 < queue.length && queue[child + 1].score < queue[child].score) child += 1
        if (last.score <= queue[child].score) break
        queue[index] = queue[child]; index = child
      }
      queue[index] = last
    }
    return first
  }
  const costs = new Map(), previous = new Map(), clearance = new Map()
  const initialKey = startIndex * 2
  costs.set(initialKey, 0)
  push({ index: startIndex, direction: 0, key: initialKey, cost: 0, score: heuristic(startIndex) })
  let finalKey = null
  while (queue.length) {
    const current = pop()
    if (current.cost !== costs.get(current.key)) continue
    if (current.index === endIndex) { finalKey = current.key; break }
    const x = current.index % width, y = Math.floor(current.index / width)
    const neighbors = []
    if (x > 0) neighbors.push([current.index - 1, 0])
    if (x < width - 1) neighbors.push([current.index + 1, 0])
    if (y > 0) neighbors.push([current.index - width, 1])
    if (y < ys.length - 1) neighbors.push([current.index + width, 1])
    const a = point(current.index)
    for (const [index, direction] of neighbors) {
      const b = point(index), segmentKey = `${Math.min(current.index, index)}:${Math.max(current.index, index)}`
      if (!clearance.has(segmentKey)) clearance.set(segmentKey, !cards.some((card) => segmentCrossesCard(a, b, card)))
      if (!clearance.get(segmentKey)) continue
      const cost = current.cost + Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + (direction === current.direction ? 0 : 32)
      const key = index * 2 + direction
      if (cost >= (costs.get(key) ?? Infinity)) continue
      costs.set(key, cost); previous.set(key, current.key)
      push({ index, direction, key, cost, score: cost + heuristic(index) })
    }
  }
  if (finalKey === null) return null
  const route = []
  for (let key = finalKey; key !== undefined; key = previous.get(key)) route.push(point(Math.floor(key / 2)))
  return route.reverse()
}

export function edgePath(sourceNode, targetNode, nodes = []) {
  if (!sourceNode || !targetNode || ![sourceNode.x, sourceNode.y, targetNode.x, targetNode.y].every(Number.isFinite)) return ''
  const start = [sourceNode.x + WORKFLOW_NODE_WIDTH, sourceNode.y + WORKFLOW_PORT_Y]
  const end = [targetNode.x, targetNode.y + WORKFLOW_PORT_Y]
  const isEndpoint = (node) => node === sourceNode || node === targetNode || (node.id && (node.id === sourceNode.id || node.id === targetNode.id))
  const otherNodes = (Array.isArray(nodes) ? nodes : []).filter((node) => node && [node.x, node.y].every(Number.isFinite) && !isEndpoint(node))
  const bend = Math.max(32, Math.min(160, (end[0] - start[0]) / 2))
  const controlA = [start[0] + bend, start[1]], controlB = [end[0] - bend, end[1]]
  if (end[0] >= start[0] + 24 && curveIsClear(start, controlA, controlB, end, otherNodes.map((node) => cardBounds(node, ROUTE_CLEARANCE)))) {
    return `M ${start.join(' ')} C ${controlA.join(' ')}, ${controlB.join(' ')}, ${end.join(' ')}`
  }
  const allNodes = [sourceNode, targetNode, ...otherNodes]
  // Old saved layouts can have only a 40px gap. Keep each port's escape within
  // half the available gap so routing still works before the user reorganizes.
  const rightGaps = allNodes.filter((node) => node !== sourceNode && node.x >= start[0] && start[1] > node.y && start[1] < node.y + WORKFLOW_NODE_HEIGHT).map((node) => (node.x - start[0]) / 2)
  const leftGaps = allNodes.filter((node) => node !== targetNode && node.x + WORKFLOW_NODE_WIDTH <= end[0] && end[1] > node.y && end[1] < node.y + WORKFLOW_NODE_HEIGHT).map((node) => (end[0] - node.x - WORKFLOW_NODE_WIDTH) / 2)
  const outGap = Math.min(WORKFLOW_ROUTE_SIDE_GUTTER, ...rightGaps), inGap = Math.min(WORKFLOW_ROUTE_SIDE_GUTTER, ...leftGaps)
  const clearance = Math.max(0.1, Math.min(ROUTE_CLEARANCE, outGap - 1, inGap - 1))
  const cards = allNodes.map((node) => cardBounds(node, clearance))
  const escapeStart = [start[0] + outGap, start[1]], escapeEnd = [end[0] - inGap, end[1]]
  const route = obstacleRoute(escapeStart, escapeEnd, cards)
  const radius = Math.min(ROUTE_RADIUS, clearance * 0.8)
  if (route) return roundedRoute([start, ...route, end], radius)
  // Overlapping cards can cover a port completely. Keep a stable rounded path
  // while the user moves them apart; valid non-overlapping layouts use A* above.
  const routeY = Math.max(...cards.map((card) => card.bottom)) + WORKFLOW_ROUTE_BOTTOM_GUTTER
  return roundedRoute([start, escapeStart, [escapeStart[0], routeY], [escapeEnd[0], routeY], escapeEnd, end], radius)
}
