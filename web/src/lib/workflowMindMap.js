// Visual root/details are projections, never executable SOP nodes or edges.
import { NODE_TYPES, workflowNodeOrder } from './sopWorkflow.js'

export const MINDMAP_STAGE_WIDTH = 240
export const MINDMAP_STAGE_HEIGHT = 84
export const MINDMAP_PORT_Y = 42
const ROOT_WIDTH = 220, ROOT_HEIGHT = 96, GAP = 80
const DETAIL_WIDTH = 250, DETAIL_HEIGHT = 40, DETAIL_GAP = 8, ROW_GAP = 26
const fields = [
  ['objective', '目标', '待填写阶段目标'],
  ['deliverable', '交付物', '待填写交付物'],
  ['acceptance', '验收标准', '待填写验收标准'],
  ['output', '已记录产出', '尚未记录产出'],
]
const colors = new Map(NODE_TYPES.map(type => [type.type, type.color]))
const finite = value => Number.isFinite(value) ? value : 0
const detailSpan = fields.length * DETAIL_HEIGHT + (fields.length - 1) * DETAIL_GAP

export function isLegacyWorkflowChain(workflow) {
  const nodes = workflow?.nodes || []
  if (!nodes.length) return false
  if (nodes.length === 1) return nodes[0].x === 40 && nodes[0].y === 60
  const ordered = [...nodes].sort((a, b) => a.x - b.x)
  return ordered.every(node => Number.isFinite(node.x) && Number.isFinite(node.y) && Math.abs(node.y - ordered[0].y) < 1)
    && ordered.slice(1).every((node, index) => node.x - ordered[index].x >= MINDMAP_STAGE_WIDTH)
}

function excerpt(value, fallback) {
  const text = typeof value === 'string' ? value.trim().replace(/\s+/g, ' ') : ''
  return text.length > 80 ? `${text.slice(0, 79)}…` : text || fallback
}

function curve(start, end) {
  const dx = Math.abs(end.x - start.x)
  const direction = end.x >= start.x ? 1 : -1
  const bend = Math.max(28, dx * .48)
  return `M ${start.x} ${start.y} C ${start.x + direction * bend} ${start.y}, ${end.x - direction * bend} ${end.y}, ${end.x} ${end.y}`
}

function automaticPositions(nodes, expanded) {
  const split = nodes.length > 3 ? Math.ceil(nodes.length / 2) : 0
  const left = nodes.slice(0, split), right = nodes.slice(split)
  const span = list => list.reduce((total, node) => total + (expanded.has(node.id) ? detailSpan : MINDMAP_STAGE_HEIGHT) + ROW_GAP, 0) - (list.length ? ROW_GAP : 0)
  const height = Math.max(ROOT_HEIGHT, span(left), span(right))
  const root = { x: 460, y: 32 + (height - ROOT_HEIGHT) / 2, width: ROOT_WIDTH, height: ROOT_HEIGHT }
  const positions = new Map()
  for (const [side, list] of [['left', left], ['right', right]]) {
    let top = 32 + (height - span(list)) / 2
    const x = side === 'left' ? root.x - GAP - MINDMAP_STAGE_WIDTH : root.x + ROOT_WIDTH + GAP
    for (const node of list) {
      const block = expanded.has(node.id) ? detailSpan : MINDMAP_STAGE_HEIGHT
      positions.set(node.id, { x, y: top + (block - MINDMAP_STAGE_HEIGHT) / 2, side })
      top += block + ROW_GAP
    }
  }
  return { root, positions }
}

function savedPositions(nodes) {
  const left = Math.min(...nodes.map(node => finite(node.x)))
  const top = Math.min(...nodes.map(node => finite(node.y)))
  const bottom = Math.max(...nodes.map(node => finite(node.y) + MINDMAP_STAGE_HEIGHT))
  const columns = [...new Set(nodes.map(node => finite(node.x)))].sort((a, b) => a - b)
  let rootX = left - ROOT_WIDTH - GAP, bestGap = ROOT_WIDTH + 32
  for (let index = 1; index < columns.length; index++) {
    const end = columns[index - 1] + MINDMAP_STAGE_WIDTH
    const gap = columns[index] - end
    if (gap >= bestGap) { bestGap = gap; rootX = end + (gap - ROOT_WIDTH) / 2 }
  }
  const root = { x: rootX, y: (top + bottom - ROOT_HEIGHT) / 2, width: ROOT_WIDTH, height: ROOT_HEIGHT }
  const positions = new Map(nodes.map(node => [node.id, {
    x: finite(node.x), y: finite(node.y),
    side: finite(node.x) + MINDMAP_STAGE_WIDTH / 2 < rootX + ROOT_WIDTH / 2 ? 'left' : 'right',
  }]))
  return { root, positions }
}

export function projectWorkflowMindMap(workflow, { expandedIds = new Set(), forceLayout = false } = {}) {
  const raw = Array.isArray(workflow?.nodes) ? workflow.nodes : []
  const byId = new Map(raw.map(node => [node.id, node]))
  const ordered = workflowNodeOrder(workflow).map(id => byId.get(id)).filter(Boolean)
  const expanded = expandedIds instanceof Set ? expandedIds : new Set(expandedIds || [])
  const projected = forceLayout || isLegacyWorkflowChain(workflow)
  const layout = !ordered.length || projected ? automaticPositions(ordered, expanded) : savedPositions(ordered)
  const root = { ...layout.root, id: 'workflow-visual-root', title: workflow?.name || '项目工作流' }
  const stages = new Map()
  const branches = []
  for (const [index, node] of ordered.entries()) {
    const position = layout.positions.get(node.id)
    const stage = { id: node.id, ...position, width: MINDMAP_STAGE_WIDTH, height: MINDMAP_STAGE_HEIGHT,
      storedX: node.x, storedY: node.y, order: index + 1, color: colors.get(node.type) || '#8a81a6', details: [],
      input: { x: position.x, y: position.y + MINDMAP_PORT_Y },
      output: { x: position.x + MINDMAP_STAGE_WIDTH, y: position.y + MINDMAP_PORT_Y },
    }
    const inward = { x: position.side === 'left' ? stage.x + stage.width : stage.x, y: stage.y + stage.height / 2 }
    const start = { x: position.side === 'left' ? root.x : root.x + root.width, y: root.y + root.height / 2 }
    branches.push({ id: `branch:${node.id}`, target: node.id, path: curve(start, inward), color: stage.color })
    stages.set(node.id, stage)
  }
  // Details may expand beside a saved custom layout. Pack each outer column
  // without moving a real stage or writing presentation state into its schema.
  for (const side of ['left', 'right']) {
    const column = [...stages.values()].filter(stage => stage.side === side)
    const visible = column.filter(stage => expanded.has(stage.id)).sort((a, b) => a.y - b.y || a.order - b.order)
    const detailX = side === 'left' ? Math.min(...column.map(stage => stage.x)) - 50 - DETAIL_WIDTH
      : Math.max(...column.map(stage => stage.x + stage.width)) + 50
    let bottom = -Infinity
    for (const stage of visible) {
      const node = byId.get(stage.id)
      const detailTop = Math.max(stage.y + stage.height / 2 - detailSpan / 2, bottom + ROW_GAP)
      stage.details = fields.map(([key, label, fallback], index) => {
        const fullText = typeof node[key] === 'string' ? node[key] : ''
        const detail = { id: `detail:${stage.id}:${key}`, nodeId: stage.id, key, label,
          text: excerpt(fullText, fallback), fullText, x: detailX, y: detailTop + index * (DETAIL_HEIGHT + DETAIL_GAP), width: DETAIL_WIDTH, height: DETAIL_HEIGHT }
        detail.path = curve({ x: side === 'left' ? stage.x : stage.x + stage.width, y: stage.y + stage.height / 2 },
          { x: side === 'left' ? detail.x + detail.width : detail.x, y: detail.y + detail.height / 2 })
        return detail
      })
      bottom = detailTop + detailSpan
    }
  }
  const rectangles = [root, ...[...stages.values()].flatMap(stage => [stage, ...stage.details])]
  const left = Math.min(...rectangles.map(rect => rect.x)), top = Math.min(...rectangles.map(rect => rect.y))
  const right = Math.max(...rectangles.map(rect => rect.x + rect.width)), bottom = Math.max(...rectangles.map(rect => rect.y + rect.height))
  return { root, stages, branches, bounds: { x: left, y: top, left, top, right, bottom, width: right - left, height: bottom - top }, projected }
}

export function arrangeWorkflowMindMap(workflow, options = {}) {
  const view = projectWorkflowMindMap(workflow, { ...options, forceLayout: true })
  return { ...workflow, nodes: workflow.nodes.map(node => ({ ...node, x: view.stages.get(node.id).x, y: view.stages.get(node.id).y })),
    edges: workflow.edges.map(edge => ({ ...edge })), viewport: { ...workflow.viewport } }
}

export function workflowDependencyPath(source, target, index = 0, { root, bounds, stages } = {}) {
  if (!source || !target) return ''
  const start = source.output || { x: source.x + source.width, y: source.y + source.height / 2 }
  const end = target.input || { x: target.x, y: target.y + target.height / 2 }
  const margin = 24 + Math.abs(index % 3) * 2
  const boxes = [root, ...[...(stages?.values?.() || [])].flatMap(stage => [stage, ...stage.details])].filter(Boolean)
  const startMargin = Math.min(margin, ...boxes.filter(box => box.x >= start.x && start.y > box.y && start.y < box.y + box.height).map(box => (box.x - start.x) / 2))
  const endMargin = Math.min(margin, ...boxes.filter(box => box.x + box.width <= end.x && end.y > box.y && end.y < box.y + box.height).map(box => (end.x - box.x - box.width) / 2))
  const pointsAt = y => [start, { x: start.x + startMargin, y: start.y }, { x: start.x + startMargin, y },
    { x: end.x - endMargin, y }, { x: end.x - endMargin, y: end.y }, end]
  const blocked = points => points.slice(1).some((point, offset) => {
    const previous = points[offset]
    return boxes.some(box => {
      if (point.x === previous.x) return point.x > box.x && point.x < box.x + box.width
        && Math.max(point.y, previous.y) > box.y && Math.min(point.y, previous.y) < box.y + box.height
      return point.y > box.y && point.y < box.y + box.height
        && Math.max(point.x, previous.x) > box.x && Math.min(point.x, previous.x) < box.x + box.width
    })
  })
  // Keep adjacent dependencies short; skip/cross-column routes use a clear
  // corridor so a visual root or an intervening phase never hides the arrow.
  const middle = start.y === end.y ? Math.max(source.y + source.height, target.y + target.height) + margin : (start.y + end.y) / 2
  const candidates = [middle]
  if (root) candidates.push(root.y - margin, root.y + root.height + margin)
  const top = bounds?.top ?? Math.min(source.y, target.y)
  const bottom = bounds?.bottom ?? Math.max(source.y + source.height, target.y + target.height)
  candidates.push(top - margin, bottom + margin)
  const clear = candidates.map(y => ({ points: pointsAt(y), distance: Math.abs(start.y - y) + Math.abs(end.y - y) }))
    .filter(route => !blocked(route.points)).sort((a, b) => a.distance - b.distance)
  const points = clear[0]?.points || pointsAt(top - margin)
  return `M ${start.x} ${start.y} H ${points[1].x} V ${points[2].y} H ${points[3].x} V ${end.y} H ${end.x}`
}
