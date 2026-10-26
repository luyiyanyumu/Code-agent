<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../../api.js'
import { useDialog } from '../../composables/useDialog.js'
import UiIcon from '../ui/UiIcon.vue'
import { agentStateLabel } from '../../lib/agentTeam.js'
import { createWorkflowHistory, resetWorkflowHistory, recordWorkflowHistory, undoWorkflowHistory, redoWorkflowHistory, canUndoWorkflow, canRedoWorkflow } from '../../lib/workflowHistory.js'
import {
  NODE_TYPES, createSopWorkflow, createWorkflowNode, validateWorkflow, canConnect,
  connectNodes, removeNode, setNodeStatus, readyNodeIds, workflowNodeOrder, workflowPrompt, WORKFLOW_LIMITS,
} from '../../lib/sopWorkflow.js'
import { projectWorkflowMindMap, arrangeWorkflowMindMap, workflowDependencyPath } from '../../lib/workflowMindMap.js'

const props = defineProps({ projectId: { type: String, required: true }, tree: { type: String, default: 'main' }, agents: { type: Array, default: () => [] } })
const emit = defineEmits(['ask', 'error', 'agent', 'team-refresh'])
const dialog = useDialog()
const rootEl = ref(null)
const canvasEl = ref(null)
const canvasSize = ref({ width: 0, height: 0 })
const importEl = ref(null)
const active = ref(null)
const selectedId = ref('')
const selectedEdgeId = ref('')
const inspectorOpen = ref(false)
const addOpen = ref(false)
const addSearch = ref('')
const addPosition = ref(null)
const addAnchor = ref(null)
const addMenuEl = ref(null)
const nodeSearchEl = ref(null)
const searchIndex = ref(0)
const spaceHeld = ref(false)
const notice = ref('')
const noticeTone = ref('')
const gesture = ref(null)
const connectionPoint = ref(null)
const hoveredInput = ref('')
const contexts = new Map()
let disposed = false
let resizeObserver = null
let noticeTimer = null
let lastDraggedNode = ''
let ignoreNodeClickUntil = 0
let ignoreGraphClickUntil = 0
const WIDTH = 240, HEIGHT = 84
const SAVE_DELAY = 850
const READ_TIMEOUT = 8000
const pendingReads = new Map()
const DRAFT_PREFIX = 'code-agent-sop-draft-v1:'
const arrowId = `workflow-arrow-${Math.random().toString(36).slice(2, 10)}`
const clone = value => JSON.parse(JSON.stringify(value))
const workflow = computed(() => active.value?.document || null)
const nodes = computed(() => {
  if (!workflow.value) return []
  const byId = new Map(workflow.value.nodes.map(node => [node.id, node]))
  return workflowNodeOrder(workflow.value).map(id => byId.get(id)).filter(Boolean)
})
const edges = computed(() => workflow.value?.edges || [])
const viewport = computed(() => workflow.value?.viewport || { x: 32, y: 32, zoom: 1 })
const nodeMap = computed(() => new Map(nodes.value.map(node => [node.id, node])))
const expandedIds = computed(() => new Set(active.value?.expandedIds || []))
const mindMap = computed(() => projectWorkflowMindMap(workflow.value, { expandedIds: expandedIds.value }))
const showDependencies = computed(() => !!active.value?.showDependencies)
const visualStage = node => mindMap.value.stages.get(typeof node === 'string' ? node : node?.id)
const assignedAgents = computed(() => new Map((props.agents || []).filter(agent => agent.role === 'node' && agent.node_id && agent.state !== 'retired').map(agent => [agent.node_id, agent])))
const nodeAgent = node => assignedAgents.value.get(node?.id)
function openNodeAgent(node) { emit('agent', nodeAgent(node) || { node_id: node.id }) }
const edgeRoutes = computed(() => new Map(edges.value.map((edge, index) => [edge.id, workflowDependencyPath(visualStage(edge.source), visualStage(edge.target), index, { root: mindMap.value.root, bounds: mindMap.value.bounds, stages: mindMap.value.stages })])))
const selectedNode = computed(() => nodeMap.value.get(selectedId.value) || null)
const undoAvailable = computed(() => canUndoWorkflow(active.value?.history))
const redoAvailable = computed(() => canRedoWorkflow(active.value?.history))
const compactView = computed(() => viewport.value.zoom < .55)
const adjacentEdges = computed(() => new Set(edges.value.filter(edge => edge.source === selectedId.value || edge.target === selectedId.value).map(edge => edge.id)))
const visibleNodeTypes = computed(() => {
  const query = addSearch.value.trim().toLowerCase()
  return NODE_TYPES.filter(type => !query || `${type.title} ${type.type}`.toLowerCase().includes(query))
})
const addMenuStyle = computed(() => {
  const root = rootEl.value?.getBoundingClientRect()
  if (!root) return {}
  const anchor = addAnchor.value || rootEl.value.querySelector('.add-node-wrap')?.getBoundingClientRect()
  const x = addAnchor.value ? anchor.x : (anchor?.right || root.right) - 220
  const y = addAnchor.value ? anchor.y : (anchor?.bottom || root.top + 50) + 8
  const height = Math.max(140, Math.min(408, root.height - 24))
  return { left: `${Math.max(8, Math.min(root.width - 228, x - root.left))}px`, top: `${Math.max(8, Math.min(root.height - height - 8, y - root.top))}px`, maxHeight: `${height}px` }
})
watch(addSearch, () => { searchIndex.value = 0 })
const ready = computed(() => {
  try { return new Set(workflow.value ? readyNodeIds(workflow.value) : []) }
  catch { return new Set() }
})
const completed = computed(() => nodes.value.filter(node => node.status === 'done').length)
const nextNode = computed(() => nodes.value.find(node => node.status === 'active') || nodes.value.find(node => ready.value.has(node.id)))
const predecessors = computed(() => selectedNode.value ? edges.value.filter(edge => edge.target === selectedId.value).map(edge => nodeMap.value.get(edge.source)).filter(Boolean) : [])
const successors = computed(() => selectedNode.value ? edges.value.filter(edge => edge.source === selectedId.value).map(edge => nodeMap.value.get(edge.target)).filter(Boolean) : [])
const statusText = computed(() => {
  const state = active.value
  if (!state) return ''
  if (state.loading) return '读取中'
  if (state.conflict) return '草稿已保留'
  if (state.loadError || state.saveError) return '待重试'
  if (state.saving) return '保存中'
  return state.editSerial > state.savedSerial ? '待保存' : state.document.revision === 0 ? '默认模板' : '已保存'
})
const statusClass = computed(() => active.value?.conflict || active.value?.loadError || active.value?.saveError ? 'warning' : active.value?.saving ? 'saving' : 'saved')
const previewPath = computed(() => {
  if (gesture.value?.kind !== 'connect' || !connectionPoint.value) return ''
  const source = visualStage(gesture.value.source)
  if (!source) return ''
  const { x, y } = source.output, point = connectionPoint.value
  const bend = Math.max(54, Math.abs(point.x - x) * .45)
  return `M ${x} ${y} C ${x + bend} ${y}, ${point.x - bend} ${point.y}, ${point.x} ${point.y}`
})
const dotStyle = computed(() => ({
  backgroundSize: `${Math.max(18, 24 * viewport.value.zoom)}px ${Math.max(18, 24 * viewport.value.zoom)}px`,
  backgroundPosition: `${viewport.value.x}px ${viewport.value.y}px`,
}))
const worldStyle = computed(() => ({ transform: `translate(${viewport.value.x}px, ${viewport.value.y}px) scale(${viewport.value.zoom})` }))
const minimap = computed(() => {
  if (!nodes.value.length) return null
  const { left: graphLeft, top: graphTop, right: graphRight, bottom: graphBottom } = mindMap.value.bounds
  const left = graphLeft - 40, top = graphTop - 40, right = graphRight + 40, bottom = graphBottom + 40
  const scale = Math.min(144 / (right - left), 38 / (bottom - top))
  const x = (160 - (right - left) * scale) / 2 - left * scale
  const y = (54 - (bottom - top) * scale) / 2 - top * scale
  const box = canvasSize.value.width ? canvasSize.value : null
  return {
    left, top, scale, x, y,
    viewport: box ? {
      x: x - viewport.value.x / viewport.value.zoom * scale,
      y: y - viewport.value.y / viewport.value.zoom * scale,
      width: box.width / viewport.value.zoom * scale,
      height: box.height / viewport.value.zoom * scale,
    } : null,
  }
})

function keyOf(projectId, tree) { return `${encodeURIComponent(projectId)}:${encodeURIComponent(tree || 'main')}` }
function unwrap(data) { return data?.workflow || data }
function validDocument(data) {
  const document = unwrap(data)
  const validation = validateWorkflow(document)
  if (!validation.valid) throw new Error(validation.errors[0] || '工作流数据格式不正确')
  return clone(document)
}
function reopenEditableStage(document, id) {
  // Editor drafts may contain a temporarily blank title or output. Reopening
  // must still work without asserting that incomplete evidence is accepted.
  const next = clone(document), affected = new Set([id]), queue = [id]
  for (let cursor = 0; cursor < queue.length; cursor++) {
    for (const edge of next.edges) if (edge.source === queue[cursor] && !affected.has(edge.target)) {
      affected.add(edge.target)
      queue.push(edge.target)
    }
  }
  for (const node of next.nodes) if (affected.has(node.id)) node.status = 'pending'
  return next
}
function hasTransientGesture(state) {
  return active.value === state && ['node', 'pan'].includes(gesture.value?.kind)
}
function documentForPersistence(state) {
  const document = clone(state.document)
  if (!hasTransientGesture(state)) return document
  const current = gesture.value
  if (current.kind === 'pan') { document.viewport.x = current.startX; document.viewport.y = current.startY }
  else {
    const node = document.nodes.find(node => node.id === current.nodeId)
    if (current.originalPositions) {
      for (const item of document.nodes) {
        const position = current.originalPositions.get(item.id)
        if (position) { item.x = position.x; item.y = position.y }
      }
    } else if (node) { node.x = current.startX; node.y = current.startY }
  }
  return document
}
function readDraft(key) {
  try {
    const draft = JSON.parse(localStorage.getItem(DRAFT_PREFIX + key) || 'null')
    if (!draft || !draft.document || !Number.isInteger(draft.baseRevision)) return null
    // Empty editable fields are valid drafts, even while they temporarily fail
    // the server's completion/title rules. Verify the surrounding document.
    const checked = clone(draft.document)
    checked.nodes = checked.nodes.map(node => ({ ...node,
      title: typeof node.title === 'string' && !node.title.trim() ? '未命名阶段' : node.title,
      output: node.status === 'done' && typeof node.output === 'string' && !node.output.trim() ? '待补充产出' : node.output,
    }))
    const result = validateWorkflow(checked)
    if (!result.valid) return null
    for (const node of draft.document.nodes) if (node.status === 'done' && !node.output.trim()) draft.document = reopenEditableStage(draft.document, node.id)
    return draft
  } catch { return null }
}
function persistDraft(state) {
  if (!state) return
  try {
    if (state.editSerial > state.savedSerial || state.conflict) localStorage.setItem(DRAFT_PREFIX + state.key, JSON.stringify({ document: documentForPersistence(state), baseRevision: state.baseRevision }))
    else localStorage.removeItem(DRAFT_PREFIX + state.key)
  } catch { /* The live document remains editable when local storage is unavailable. */ }
}
function createContext(projectId, tree) {
  const key = keyOf(projectId, tree), draft = readDraft(key)
  const document = draft ? draft.document : createSopWorkflow()
  return reactive({
    key, projectId, tree: tree || 'main', document, history: createWorkflowHistory(document),
    baseRevision: draft?.baseRevision || 0, editSerial: draft ? 1 : 0, savedSerial: 0,
    loaded: false, loading: false, saving: false, conflict: false, loadError: '', saveError: '',
    saveTimer: null, localTimer: null, saveQueued: false, loadGeneration: 0, loadController: null,
    previewViewport: null, initialViewport: !draft,
    expandedIds: [], showDependencies: false, projectionViewed: !!draft,
  })
}
function showNotice(message, tone = '') {
  notice.value = message
  noticeTone.value = tone
  clearTimeout(noticeTimer)
  if (!tone) noticeTimer = setTimeout(() => { notice.value = '' }, 5000)
}
function recordChange(state = active.value, group = null, captureHistory = true) {
  if (!state) return
  if (captureHistory) recordWorkflowHistory(state.history, documentForPersistence(state), { group })
  state.editSerial++
  state.saveError = ''
  clearTimeout(state.localTimer)
  state.localTimer = setTimeout(() => persistDraft(state), 90)
  scheduleSave(state)
}
function recordViewChange(state = active.value) {
  if (!state) return
  // Browsing a preview before its server revision arrives is not a document edit.
  if (!state.loaded || state.loading) { state.previewViewport = clone(state.document.viewport); return }
  recordChange(state, null, false)
}
async function readWorkflow(state, controller = new AbortController()) {
  const signal = controller.signal
  let timer, onAbort
  pendingReads.set(controller, state)
  try {
    const cancelled = new Promise((_, reject) => {
      onAbort = () => reject(signal.reason || Object.assign(new Error('读取工作流已取消'), { name: 'AbortError' }))
      if (signal.aborted) onAbort()
      else signal.addEventListener('abort', onAbort, { once: true })
    })
    timer = setTimeout(() => controller.abort(Object.assign(new Error('读取工作流超时，请重试。当前草稿已保留。'), { name: 'TimeoutError' })), READ_TIMEOUT)
    return await Promise.race([api.code.workflow(state.projectId, state.tree, { signal }), cancelled])
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', onAbort)
    pendingReads.delete(controller)
  }
}
function cancelLoad(state) {
  if (!state) return
  for (const [controller, owner] of pendingReads) if (owner === state) controller.abort()
  if (!state.loading) return
  state.loadGeneration++
  state.loadController?.abort()
  state.loadController = null
  state.loading = false
}
function scheduleSave(state) {
  clearTimeout(state.saveTimer)
  if (!state.loaded || state.conflict || disposed || state.editSerial <= state.savedSerial) return
  state.saveTimer = setTimeout(() => { state.saveTimer = null; void saveState(state) }, SAVE_DELAY)
}
async function loadContext(state) {
  if (state.loading || disposed) return
  const generation = ++state.loadGeneration, startSerial = state.editSerial
  const controller = new AbortController()
  state.loadController = controller
  state.loading = true
  state.loadError = ''
  try {
    const document = validDocument(await readWorkflow(state, controller))
    if (disposed || generation !== state.loadGeneration) return
    if (active.value === state && gesture.value?.kind === 'pan') {
      state.previewViewport = clone(state.document.viewport)
      cancelGesture()
    }
    const hasDraft = state.editSerial > state.savedSerial
    if (hasDraft && state.baseRevision !== document.revision) {
      state.conflict = true
      state.loaded = true
      persistDraft(state)
    } else if (hasDraft || state.editSerial !== startSerial) {
      state.baseRevision = document.revision
      state.document.revision = document.revision
      state.loaded = true
      scheduleSave(state)
    } else {
      state.document = document
      resetWorkflowHistory(state.history, document)
      state.baseRevision = document.revision
      state.loaded = true
      state.savedSerial = state.editSerial
      if (document.revision > 0) state.initialViewport = false
    }
    if (state.previewViewport) {
      state.document.viewport = state.previewViewport
      state.previewViewport = null
      state.initialViewport = false
      state.projectionViewed = true
    }
    const firstProjection = active.value === state && mindMap.value.projected && !state.projectionViewed
    if (firstProjection) state.initialViewport = true
    if (!disposed && active.value === state && (state.initialViewport || firstProjection)) {
      await nextTick()
      if (disposed || active.value !== state || generation !== state.loadGeneration) return
      // A user may browse while the next frame is being prepared. That view
      // takes precedence over the automatic first projection.
      if (state.previewViewport) {
        state.document.viewport = state.previewViewport
        state.previewViewport = null
        state.initialViewport = false
        state.projectionViewed = true
        return
      }
      const freshProjection = mindMap.value.projected && !state.projectionViewed
      if (state.initialViewport && fitCanvas(!freshProjection, false)) { state.initialViewport = false; state.projectionViewed = true }
    }
  } catch (error) {
    if (!disposed && generation === state.loadGeneration) state.loadError = error.message || '读取工作流失败'
  } finally {
    if (generation === state.loadGeneration) { state.loading = false; state.loadController = null }
  }
}
async function saveState(state = active.value, explicit = false) {
  if (!state || !state.loaded || state.loading || state.conflict) return false
  clearTimeout(state.saveTimer)
  state.saveTimer = null
  if (state.saving) { state.saveQueued = true; return false }
  if (state.editSerial <= state.savedSerial) return true
  if (hasTransientGesture(state)) {
    state.saveQueued = true
    persistDraft(state)
    return false
  }
  const validation = validateWorkflow(state.document)
  if (!validation.valid) {
    state.saveError = validation.errors[0] || '请检查工作流配置'
    persistDraft(state)
    return false
  }
  const snapshot = clone(state.document), serial = state.editSerial, revision = state.baseRevision
  state.saving = true
  state.saveError = ''
  state.saveQueued = false
  persistDraft(state)
  try {
    const saved = validDocument(await api.code.saveWorkflow(state.projectId, snapshot, revision, state.tree))
    state.baseRevision = saved.revision
    state.savedSerial = serial
    if (state.editSerial === serial && !hasTransientGesture(state)) state.document = saved
    else { state.document.revision = saved.revision; state.document.updated_at = saved.updated_at }
    persistDraft(state)
    if (!disposed && active.value === state) emit('team-refresh', { project_id: state.projectId, tree_id: state.tree })
    return true
  } catch (error) {
    if (error.status === 409) state.conflict = true
    else state.saveError = error.message || '保存工作流失败'
    persistDraft(state)
    if (explicit && !disposed && active.value === state && !state.conflict) showNotice(state.saveError, 'error')
    return false
  } finally {
    state.saving = false
    if (!state.conflict && !state.saveError && (state.saveQueued || state.editSerial > state.savedSerial)) scheduleSave(state)
  }
}
function retry() {
  const state = active.value
  if (!state) return
  if (state.loadError || !state.loaded) void loadContext(state)
  else void saveState(state, true)
}
async function reloadRemote() {
  const state = active.value
  if (!state) return
  const serial = state.editSerial
  await dialog.open({
    title: '载入服务端版本？', description: '当前未保存的草稿将被替换。', icon: 'refresh', tone: 'warning',
    confirmText: '载入新版本', pendingText: '正在读取…',
    onConfirm: async () => {
      if (disposed || active.value !== state || state.editSerial !== serial) throw new Error('工作流已变化，请重新操作')
      const document = validDocument(await readWorkflow(state))
      if (disposed || active.value !== state || state.editSerial !== serial) throw new Error('工作流已变化，草稿已保留')
      state.document = document
      resetWorkflowHistory(state.history, document)
      state.baseRevision = document.revision
      state.savedSerial = state.editSerial
      state.conflict = false
      state.loadError = ''
      state.saveError = ''
      state.loaded = true
      selectedId.value = ''
      selectedEdgeId.value = ''
      persistDraft(state)
    },
  })
}
async function saveDraftOverLatest() {
  const state = active.value
  if (!state) return
  await dialog.open({
    title: '用当前草稿保存？', description: '将替换服务端的新版本。', icon: 'file', tone: 'warning',
    confirmText: '保存当前草稿', pendingText: '正在保存…',
    onConfirm: async () => {
      if (disposed || active.value !== state) throw new Error('当前项目已变化，请重新操作')
      const latest = validDocument(await readWorkflow(state))
      if (disposed || active.value !== state) throw new Error('当前项目已变化，草稿已保留')
      state.baseRevision = latest.revision
      state.document.revision = latest.revision
      state.conflict = false
      state.loaded = true
      const saved = await saveState(state, true)
      if (!saved) throw new Error(state.conflict ? '服务端再次发生变化，草稿已保留，请重试' : state.saveError || '保存未完成')
    },
  })
}
watch(() => [props.projectId, props.tree], ([projectId, tree]) => {
  const previous = active.value
  cancelGesture()
  if (previous) { cancelLoad(previous); persistDraft(previous); clearTimeout(previous.saveTimer); if (previous.loaded) void saveState(previous) }
  selectedId.value = ''
  selectedEdgeId.value = ''
  inspectorOpen.value = false
  addOpen.value = false
  spaceHeld.value = false
  notice.value = ''
  if (!projectId) { active.value = null; return }
  const key = keyOf(projectId, tree)
  if (!contexts.has(key)) contexts.set(key, createContext(projectId, tree))
  active.value = contexts.get(key)
  if (!active.value.loaded) void loadContext(active.value)
  else if (active.value.editSerial > active.value.savedSerial) scheduleSave(active.value)
}, { immediate: true })

function metaFor(node) { return NODE_TYPES.find(type => type.type === node?.type) || NODE_TYPES[0] || { title: node?.type, color: '#6e7890' } }
function rectStyle(rect) { return rect ? { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px` } : {} }
function cardStyle(node) { return { ...rectStyle(visualStage(node)), '--stage-color': metaFor(node).color || '#6678ef' } }
function detailStyle(node, detail) { return { ...rectStyle(detail), '--stage-color': metaFor(node).color || '#6678ef' } }
function dependencyVisible(edge) { return showDependencies.value || selectedEdgeId.value === edge.id || adjacentEdges.value.has(edge.id) || gesture.value?.kind === 'connect' }
function toggleDetails(id) {
  if (!active.value || gesture.value) return
  const state = active.value, values = new Set(state.expandedIds), opening = !values.has(id)
  if (!opening) values.delete(id)
  else values.add(id)
  state.expandedIds = [...values]
  if (opening) nextTick(() => {
    if (disposed || active.value !== state || !expandedIds.value.has(id)) return
    const stage = visualStage(id), box = canvasEl.value?.getBoundingClientRect()
    if (!stage || !box || !stage.details.length) return
    const rectangles = [stage, ...stage.details], view = viewport.value
    const left = Math.min(...rectangles.map(rect => rect.x)) * view.zoom + view.x
    const right = Math.max(...rectangles.map(rect => rect.x + rect.width)) * view.zoom + view.x
    const top = Math.min(...rectangles.map(rect => rect.y)) * view.zoom + view.y
    const bottom = Math.max(...rectangles.map(rect => rect.y + rect.height)) * view.zoom + view.y
    if (left < 16 || right > box.width - 16 || top < 16 || bottom > box.height - 16) fitCanvas(false, false)
  })
}
function toggleDependencies() {
  if (!active.value) return
  active.value.showDependencies = !active.value.showDependencies
}
function openDetail(id, field) {
  selectNode(id)
  const state = active.value
  nextTick(() => {
    if (active.value === state && selectedId.value === id) rootEl.value?.querySelector(`[data-workflow-field="${field}"]`)?.focus({ preventScroll: true })
  })
}
function statusLabel(node) {
  if (node.status === 'done') return '已验收'
  if (node.status === 'active') return '进行中'
  return ready.value.has(node.id) ? '可以开始' : '等待前置阶段'
}
function selectNode(id, open = true) {
  selectedId.value = id
  selectedEdgeId.value = ''
  inspectorOpen.value = open
  addOpen.value = false
}
function clickNode(id) {
  if (spaceHeld.value || Date.now() < ignoreGraphClickUntil) return
  if (id === lastDraggedNode && Date.now() < ignoreNodeClickUntil) return
  selectNode(id)
}
function focusNode(id, readable = false) {
  const state = active.value, node = visualStage(id), rect = canvasEl.value?.getBoundingClientRect()
  if (!node || !rect) return
  selectNode(id)
  nextTick(() => {
    if (disposed || active.value !== state || !nodeMap.value.has(id)) return
    const box = canvasEl.value?.getBoundingClientRect()
    if (!box) return
    if (readable) viewport.value.zoom = Math.max(.9, viewport.value.zoom)
    viewport.value.x = box.width / 2 - (node.x + WIDTH / 2) * viewport.value.zoom
    viewport.value.y = box.height / 2 - (node.y + HEIGHT / 2) * viewport.value.zoom
    recordViewChange()
  })
}
function updateNode(field, value) {
  if (!selectedNode.value || active.value?.loading) return
  const id = selectedNode.value.id
  try {
    if (selectedNode.value.status === 'done' && ['objective', 'deliverable', 'acceptance', 'output'].includes(field)) {
      active.value.document = reopenEditableStage(workflow.value, id)
      showNotice('阶段内容已改变，请重新验收')
    }
    const maximum = field === 'output' ? WORKFLOW_LIMITS.output : field === 'title' ? WORKFLOW_LIMITS.title : WORKFLOW_LIMITS.description
    selectedNode.value[field] = String(value ?? '').slice(0, maximum)
    recordChange(active.value, `${id}:${field}`)
  } catch (error) {
    showNotice(error.message || '阶段内容未更新，请重试', 'error')
  }
}
function replaceDocument(document, captureHistory = true) {
  if (!active.value) return
  active.value.document = document
  active.value.document.revision = active.value.baseRevision
  recordChange(active.value, null, captureHistory)
}
function restoreHistory(redo = false) {
  const state = active.value
  if (!state || state.loading || state.conflict) return
  cancelGesture()
  const document = redo ? redoWorkflowHistory(state.history, state.document) : undoWorkflowHistory(state.history, state.document)
  if (!document) return
  replaceDocument(document, false)
  if (!nodeMap.value.has(selectedId.value)) { selectedId.value = ''; inspectorOpen.value = false }
  if (!edges.value.some(edge => edge.id === selectedEdgeId.value)) selectedEdgeId.value = ''
  addOpen.value = false
  showNotice(redo ? '已重做' : '已撤销')
}
function requestStatus(id, status) {
  if (!workflow.value || active.value?.loading || spaceHeld.value || Date.now() < ignoreGraphClickUntil) return
  try {
    replaceDocument(status === 'pending' ? reopenEditableStage(workflow.value, id) : setNodeStatus(workflow.value, id, status))
    selectNode(id)
    notice.value = ''
  } catch (error) {
    selectNode(id)
    showNotice(error.message, 'error')
    if (status === 'done' && !nodeMap.value.get(id)?.output?.trim()) nextTick(() => rootEl.value?.querySelector('[data-output-field]')?.focus())
  }
}
async function resetNode(id) {
  const state = active.value, node = nodeMap.value.get(id)
  if (!state || !node) return
  await dialog.open({
    title: `重新处理「${node.title}」？`, description: '后续阶段将回到待处理，已有产出会保留。', icon: 'refresh', tone: 'warning', confirmText: '退回待处理',
    onConfirm: () => {
      if (disposed || active.value !== state || !nodeMap.value.has(id)) throw new Error('工作流已变化，请重新操作')
      requestStatus(id, 'pending')
    },
  })
}
function handoff() {
  if (!selectedNode.value || !workflow.value) return
  const node = selectedNode.value
  if (node.status === 'pending' && !ready.value.has(node.id)) { showNotice('先完成前置阶段，再交给 AI 处理', 'error'); return }
  try { emit('ask', workflowPrompt(workflow.value, node.id), { node_id: node.id, tree_id: props.tree }); showNotice('阶段内容已填入 Lead 输入框') }
  catch (error) { showNotice(error.message, 'error') }
}
function addNode(type) {
  if (!workflow.value || active.value?.loading) return
  if (nodes.value.length >= WORKFLOW_LIMITS.nodes) { showNotice(`最多添加 ${WORKFLOW_LIMITS.nodes} 个节点`, 'error'); return }
  const box = canvasEl.value?.getBoundingClientRect()
  const x = addPosition.value?.x ?? (box ? (box.width / 2 - viewport.value.x) / viewport.value.zoom - WIDTH / 2 : 32)
  const y = addPosition.value?.y ?? (box ? (box.height / 2 - viewport.value.y) / viewport.value.zoom - HEIGHT / 2 : 32)
  const node = createWorkflowNode(type, Math.round(x), Math.round(y))
  workflow.value.nodes.push(node)
  recordChange()
  selectNode(node.id)
}
function openAddMenu(event = null) {
  if (!workflow.value || active.value?.loading || spaceHeld.value) return
  addPosition.value = event ? canvasPoint(event) : null
  addAnchor.value = event ? { x: event.clientX, y: event.clientY } : null
  addSearch.value = ''
  searchIndex.value = 0
  addOpen.value = true
  nextTick(() => nodeSearchEl.value?.focus())
}
function toggleAddMenu() { if (addOpen.value) addOpen.value = false; else openAddMenu() }
function onCanvasDoubleClick(event) {
  if (event.target.closest('.workflow-node,.workflow-root,.mindmap-detail,.workflow-edge,.canvas-controls,.workflow-minimap,.next-stage,.canvas-empty')) return
  cancelGesture()
  openAddMenu(event)
}
function searchNodeKey(event) {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    const length = visibleNodeTypes.value.length
    if (length) searchIndex.value = (searchIndex.value + (event.key === 'ArrowDown' ? 1 : -1) + length) % length
  } else if (event.key === 'Enter') {
    event.preventDefault()
    const type = visibleNodeTypes.value[searchIndex.value]
    if (type) addNode(type.type)
  }
}
async function deleteSelection() {
  const state = active.value
  if (!state || state.loading) return
  if (selectedEdgeId.value) {
    removeDependency(selectedEdgeId.value)
    return
  }
  const node = selectedNode.value
  if (!node) return
  const id = node.id
  await dialog.open({
    title: `移除「${node.title}」？`, description: '同时移除这个节点的输入与输出连线。', icon: 'trash', tone: 'danger', confirmText: '移除节点',
    onConfirm: () => {
      if (disposed || active.value !== state || !nodeMap.value.has(id)) throw new Error('工作流已变化，请重新操作')
      if (validateWorkflow(workflow.value).valid) replaceDocument(removeNode(workflow.value, id))
      else {
        const document = reopenEditableStage(workflow.value, id)
        document.nodes = document.nodes.filter(node => node.id !== id)
        document.edges = document.edges.filter(edge => edge.source !== id && edge.target !== id)
        replaceDocument(document)
      }
      selectedId.value = ''
      inspectorOpen.value = false
    },
  })
}
function removeDependency(id) {
  if (!workflow.value || active.value?.loading) return
  const target = edges.value.find(edge => edge.id === id)?.target
  if (!target) return
  try {
    const document = { ...clone(workflow.value), edges: edges.value.filter(edge => edge.id !== id).map(edge => ({ ...edge })) }
    replaceDocument(reopenEditableStage(document, target))
    if (selectedEdgeId.value === id) selectedEdgeId.value = ''
    showNotice('执行依赖已移除，相关阶段需重新验收')
  } catch (error) { showNotice(error.message, 'error') }
}
function dependencyId(source, target) { return edges.value.find(edge => edge.source === source && edge.target === target)?.id }
function arrange() {
  if (!workflow.value || active.value?.loading) return
  try {
    cancelGesture()
    replaceDocument(arrangeWorkflowMindMap(workflow.value, { expandedIds: expandedIds.value }))
    inspectorOpen.value = false
    nextTick(() => fitCanvas())
  } catch (error) { showNotice(error.message, 'error') }
}
function fitCanvas(readable = false, persist = true) {
  const box = canvasEl.value?.getBoundingClientRect()
  if (!box || box.width < 20 || box.height < 20 || !nodes.value.length) return
  canvasSize.value = { width: box.width, height: box.height }
  const { left: graphLeft, top: graphTop, right: graphRight, bottom: graphBottom } = mindMap.value.bounds
  const left = graphLeft - 32, top = graphTop - 32, right = graphRight + 32, bottom = graphBottom + 32
  const zoom = Math.min(1, Math.max(readable ? .9 : WORKFLOW_LIMITS.minZoom, Math.min((box.width - 64) / (right - left), (box.height - 72) / (bottom - top))))
  viewport.value.zoom = zoom
  if (readable && (right - left) * zoom > box.width - 64) {
    const first = visualStage(nextNode.value?.id || nodes.value[0]?.id)
    viewport.value.x = box.width / 2 - (first.x + WIDTH / 2) * zoom
    viewport.value.y = box.height / 2 - (first.y + HEIGHT / 2) * zoom
  } else {
    viewport.value.x = (box.width - (right - left) * zoom) / 2 - left * zoom
    viewport.value.y = (box.height - (bottom - top) * zoom) / 2 - top * zoom
  }
  if (persist) recordViewChange()
  return true
}
function zoomAt(nextZoom, clientX, clientY) {
  const box = canvasEl.value?.getBoundingClientRect()
  if (!box || !workflow.value) return
  const previous = viewport.value.zoom
  const next = Math.min(WORKFLOW_LIMITS.maxZoom, Math.max(WORKFLOW_LIMITS.minZoom, nextZoom))
  const px = clientX === undefined ? box.width / 2 : clientX - box.left
  const py = clientY === undefined ? box.height / 2 : clientY - box.top
  viewport.value.x = px - (px - viewport.value.x) * next / previous
  viewport.value.y = py - (py - viewport.value.y) * next / previous
  viewport.value.zoom = next
  recordViewChange()
}
function onWheel(event) {
  if (gesture.value || event.target.closest('.canvas-controls,.workflow-minimap')) return
  if (event.shiftKey || Math.abs(event.deltaX || 0) > Math.abs(event.deltaY || 0)) {
    viewport.value.x -= Math.max(-180, Math.min(180, event.deltaX || event.deltaY || 0))
    recordViewChange()
    return
  }
  zoomAt(viewport.value.zoom * Math.exp(-Math.max(-100, Math.min(100, event.deltaY)) * .0018), event.clientX, event.clientY)
}
function navigateMinimap(event) {
  const map = minimap.value
  const canvas = canvasEl.value?.getBoundingClientRect()
  if (!map || !canvas) return
  if (event.detail === 0) { fitCanvas(); return }
  const svg = event.currentTarget.querySelector('svg'), matrix = svg?.getScreenCTM()
  if (!matrix) return
  const point = svg.createSVGPoint()
  point.x = event.clientX
  point.y = event.clientY
  const local = point.matrixTransform(matrix.inverse())
  const worldX = (local.x - map.x) / map.scale
  const worldY = (local.y - map.y) / map.scale
  viewport.value.x = canvas.width / 2 - worldX * viewport.value.zoom
  viewport.value.y = canvas.height / 2 - worldY * viewport.value.zoom
  recordViewChange()
}
function canvasPoint(event) {
  const box = canvasEl.value?.getBoundingClientRect()
  if (!box) return { x: 0, y: 0 }
  return { x: (event.clientX - box.left - viewport.value.x) / viewport.value.zoom, y: (event.clientY - box.top - viewport.value.y) / viewport.value.zoom }
}
function beginPan(event, force = false) {
  if (!workflow.value || ![0, 1].includes(event.button) || (!force && event.target.closest('.workflow-node,.workflow-root,.mindmap-detail,.workflow-edge,.canvas-controls,.canvas-empty,.workflow-minimap,.next-stage'))) return
  event.preventDefault()
  if (force) event.stopPropagation()
  canvasEl.value?.focus({ preventScroll: true })
  addOpen.value = false
  gesture.value = { kind: 'pan', pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY, startX: viewport.value.x, startY: viewport.value.y, moved: false }
}
function beginForcedPan(event) {
  if (event.button !== 1 && !spaceHeld.value) return
  if (event.target.closest('.canvas-controls,.workflow-minimap,.next-stage,.canvas-empty,input,textarea,select,[contenteditable="true"]')) return
  beginPan(event, true)
}
function beginDrag(event, node) {
  if (active.value?.loading || event.button !== 0 || event.target.closest('button,input,textarea')) return
  event.preventDefault()
  event.stopPropagation()
  selectNode(node.id, inspectorOpen.value)
  canvasEl.value?.focus({ preventScroll: true })
  const projectedStages = mindMap.value.stages
  const originalPositions = mindMap.value.projected ? new Map(nodes.value.map(item => [item.id, { x: item.x, y: item.y }])) : null
  const position = projectedStages.get(node.id)
  // Freeze the visible layout for the gesture. Reading and opening branches
  // do not change the saved graph; only a completed drag materializes it.
  if (originalPositions) for (const item of nodes.value) {
    const visual = projectedStages.get(item.id)
    if (visual) { item.x = visual.x; item.y = visual.y }
  }
  gesture.value = { kind: 'node', pointerId: event.pointerId, nodeId: node.id, clientX: event.clientX, clientY: event.clientY, startX: position.x, startY: position.y, originalPositions, moved: false }
}
function beginConnect(event, node) {
  if (active.value?.loading || event.button !== 0) return
  event.preventDefault()
  event.stopPropagation()
  gesture.value = { kind: 'connect', pointerId: event.pointerId, source: node.id }
  connectionPoint.value = canvasPoint(event)
  selectedEdgeId.value = ''
  addOpen.value = false
}
function movePointer(event) {
  const current = gesture.value
  if (!current || current.pointerId !== event.pointerId || !workflow.value) return
  if (current.kind === 'connect') {
    connectionPoint.value = canvasPoint(event)
    hoveredInput.value = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-workflow-input]')?.dataset.workflowInput || ''
    return
  }
  const dx = event.clientX - current.clientX, dy = event.clientY - current.clientY
  if (Math.abs(dx) + Math.abs(dy) > 3) current.moved = true
  if (current.kind === 'pan') { viewport.value.x = current.startX + dx; viewport.value.y = current.startY + dy }
  else {
    const node = nodeMap.value.get(current.nodeId)
    if (node) { node.x = Math.round(current.startX + dx / viewport.value.zoom); node.y = Math.round(current.startY + dy / viewport.value.zoom) }
  }
}
function endPointer(event) {
  const current = gesture.value
  if (!current || current.pointerId !== event.pointerId) return
  if (current.kind === 'connect') {
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-workflow-input]')?.dataset.workflowInput
    if (target) {
      const result = canConnect(workflow.value, current.source, target)
      if (result.valid) { replaceDocument(connectNodes(workflow.value, current.source, target)); showNotice('阶段已连接') }
      else showNotice(result.error, 'error')
    }
  } else if (current.moved) {
    if (current.kind === 'node') { lastDraggedNode = current.nodeId; ignoreNodeClickUntil = Date.now() + 300 }
    else ignoreGraphClickUntil = Date.now() + 180
  }
  else if (current.kind === 'pan') { selectedId.value = ''; selectedEdgeId.value = ''; inspectorOpen.value = false }
  else if (current.kind === 'node') {
    if (current.originalPositions) for (const node of nodes.value) {
      const position = current.originalPositions.get(node.id)
      if (position) { node.x = position.x; node.y = position.y }
    }
    selectNode(current.nodeId)
  }
  gesture.value = null
  connectionPoint.value = null
  hoveredInput.value = ''
  if (current.moved && current.kind === 'pan') recordViewChange()
  else if (current.moved && current.kind === 'node') recordChange(active.value)
  if (active.value?.saveQueued) scheduleSave(active.value)
}
function cancelGesture() {
  const current = gesture.value
  if (current?.kind === 'node') {
    if (current.originalPositions) for (const node of nodes.value) {
      const position = current.originalPositions.get(node.id)
      if (position) { node.x = position.x; node.y = position.y }
    }
    else {
      const node = nodeMap.value.get(current.nodeId)
      if (node) { node.x = current.startX; node.y = current.startY }
    }
  } else if (current?.kind === 'pan' && workflow.value) { viewport.value.x = current.startX; viewport.value.y = current.startY }
  gesture.value = null
  connectionPoint.value = null
  hoveredInput.value = ''
  if (active.value?.saveQueued) scheduleSave(active.value)
  if (current && active.value) persistDraft(active.value)
}
function selectEdge(event, id) {
  if (active.value?.loading) return
  event.stopPropagation()
  selectedEdgeId.value = id
  selectedId.value = ''
  inspectorOpen.value = false
  canvasEl.value?.focus({ preventScroll: true })
}
function onKeydown(event) {
  if (event.key === 'Escape') { addOpen.value = false; rootEl.value?.querySelector('.workflow-more')?.removeAttribute('open'); cancelGesture(); return }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    event.stopPropagation?.()
    void saveState(active.value, true)
    return
  }
  if (event.target.closest('input,textarea,select,[contenteditable="true"]')) return
  if ((event.code === 'Space' || event.key === ' ') && !event.target.closest('button,summary,a')) {
    event.preventDefault()
    spaceHeld.value = true
    return
  }
  if ((event.ctrlKey || event.metaKey) && ['z', 'y'].includes(event.key.toLowerCase())) {
    event.preventDefault()
    event.stopPropagation?.()
    restoreHistory(event.key.toLowerCase() === 'y' || event.shiftKey)
    return
  }
  if (event.key === '.' && !(event.ctrlKey || event.metaKey || event.altKey)) {
    event.preventDefault()
    if (selectedNode.value) focusNode(selectedId.value, true)
    else fitCanvas()
    return
  }
  if (event.key === 'Delete' || event.key === 'Backspace') {
    if (selectedId.value || selectedEdgeId.value) { event.preventDefault(); void deleteSelection() }
  }
}
function onKeyup(event) { if (event.code === 'Space' || event.key === ' ') spaceHeld.value = false }
function onWindowBlur() { spaceHeld.value = false; cancelGesture() }
function onOutsidePointer(event) {
  if (!rootEl.value?.querySelector('.add-node-wrap')?.contains(event.target) && !addMenuEl.value?.contains(event.target)) addOpen.value = false
  const more = rootEl.value?.querySelector('.workflow-more')
  if (more && !more.contains(event.target)) more.removeAttribute('open')
}
function preserveDrafts() {
  for (const state of contexts.values()) persistDraft(state)
}
function openImport() { rootEl.value?.querySelector('.workflow-more')?.removeAttribute('open'); importEl.value?.click() }
function exportWorkflow() {
  if (!workflow.value) return
  rootEl.value?.querySelector('.workflow-more')?.removeAttribute('open')
  const blob = new Blob([JSON.stringify(workflow.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob), link = document.createElement('a')
  link.href = url
  link.download = `${(workflow.value.name || '项目工作流').replace(/[<>:"/\\|?*]/g, '-')}.json`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function importWorkflow(event) {
  const file = event.target.files?.[0], state = active.value
  event.target.value = ''
  if (!file || !state) return
  try {
    if (file.size > 2 * 1024 * 1024) throw new Error('工作流文件不能超过 2 MB')
    const document = validDocument(JSON.parse(await file.text()))
    if (disposed || active.value !== state) return
    await dialog.open({
      title: '导入工作流？', description: `用「${document.name || '未命名工作流'}」替换当前的 ${nodes.value.length} 个节点。`, icon: 'upload', tone: 'warning', confirmText: '导入工作流',
      onConfirm: () => {
        if (disposed || active.value !== state) throw new Error('当前项目已变化，请重新操作')
        replaceDocument(document)
        selectedId.value = ''
        selectedEdgeId.value = ''
        inspectorOpen.value = false
        nextTick(() => fitCanvas(true))
      },
    })
  } catch (error) { if (!disposed && active.value === state) showNotice(error.message || '导入文件格式不正确', 'error') }
}
onMounted(() => {
  window.addEventListener('pointermove', movePointer)
  window.addEventListener('pointerup', endPointer)
  window.addEventListener('pointercancel', cancelGesture)
  window.addEventListener('pagehide', preserveDrafts)
  window.addEventListener('keyup', onKeyup)
  window.addEventListener('blur', onWindowBlur)
  document.addEventListener('pointerdown', onOutsidePointer, true)
  resizeObserver = new ResizeObserver(() => {
    const state = active.value, box = canvasEl.value?.getBoundingClientRect()
    if (box) canvasSize.value = { width: box.width, height: box.height }
    if (disposed || !state || !box || box.width < 20 || !state.initialViewport || !state.loaded || state.loading) return
    if (state.previewViewport) {
      state.document.viewport = state.previewViewport
      state.previewViewport = null
      state.initialViewport = false
      state.projectionViewed = true
      return
    }
    const freshProjection = mindMap.value.projected && !state.projectionViewed
    if (fitCanvas(!freshProjection, false)) { state.initialViewport = false; state.projectionViewed = true }
  })
  if (canvasEl.value) resizeObserver.observe(canvasEl.value)
})
onBeforeUnmount(() => {
  disposed = true
  cancelGesture()
  clearTimeout(noticeTimer)
  resizeObserver?.disconnect()
  window.removeEventListener('pointermove', movePointer)
  window.removeEventListener('pointerup', endPointer)
  window.removeEventListener('pointercancel', cancelGesture)
  window.removeEventListener('pagehide', preserveDrafts)
  window.removeEventListener('keyup', onKeyup)
  window.removeEventListener('blur', onWindowBlur)
  document.removeEventListener('pointerdown', onOutsidePointer, true)
  for (const state of contexts.values()) {
    cancelLoad(state)
    clearTimeout(state.localTimer)
    clearTimeout(state.saveTimer)
    persistDraft(state)
    if (state.loaded && state.editSerial > state.savedSerial) void saveState(state)
  }
  for (const controller of pendingReads.keys()) controller.abort()
})
</script>

<template>
  <section ref="rootEl" class="sop-workflow" aria-label="项目 SOP 工作流思维导图" @keydown="onKeydown">
    <header class="workflow-toolbar">
      <div class="workflow-heading">
        <span class="heading-icon"><UiIcon name="git-branch" :size="17" /></span>
        <strong>{{ workflow?.name || '项目工作流' }}</strong>
        <span class="workflow-progress">{{ completed }}/{{ nodes.length }}</span>
      </div>
      <div class="toolbar-actions">
        <div class="workflow-history"><button class="icon-button" aria-label="撤销工作流操作" title="撤销 · Ctrl+Z" :disabled="!undoAvailable || active?.loading || active?.conflict" @click="restoreHistory()"><UiIcon name="undo" :size="15" /></button><button class="icon-button" aria-label="重做工作流操作" title="重做 · Ctrl+Shift+Z / Ctrl+Y" :disabled="!redoAvailable || active?.loading || active?.conflict" @click="restoreHistory(true)"><UiIcon name="redo" :size="15" /></button></div>
        <div class="add-node-wrap">
          <button class="workflow-button primary-soft" :disabled="!workflow || active?.loading" aria-label="添加工作流节点" :aria-expanded="addOpen" @click="toggleAddMenu"><UiIcon name="plus" :size="15" /><span>节点</span></button>
        </div>
        <button class="workflow-button" :disabled="!workflow || active?.loading" aria-label="整理" title="将阶段排列为中心主题两侧的思维导图，保留执行依赖" @click="arrange"><UiIcon name="layers" :size="15" /><span>整理</span></button>
        <button class="workflow-button dependency-toggle" :class="{ selected: showDependencies }" :disabled="!workflow" :aria-pressed="showDependencies" aria-label="显示全部执行依赖" title="查看或编辑阶段间的执行依赖" @click="toggleDependencies"><UiIcon name="git-branch" :size="15" /><span>依赖</span></button>
        <details class="workflow-more"><summary class="icon-button" aria-label="工作流更多操作" title="更多操作"><UiIcon name="menu" :size="17" /></summary><div class="more-menu"><button @click="exportWorkflow"><UiIcon name="download" :size="15" />导出 JSON</button><button :disabled="active?.loading" @click="openImport"><UiIcon name="upload" :size="15" />导入 JSON</button></div></details>
        <span class="toolbar-divider"></span>
        <button class="save-indicator" :class="statusClass" :disabled="active?.loading || active?.saving || active?.conflict" :title="active?.saveError || '自动保存 · Ctrl+S'" @click="retry"><span class="save-dot"></span>{{ statusText }}</button>
        <button class="icon-button inspector-toggle" :class="{ selected: inspectorOpen }" :disabled="!selectedNode" title="阶段详情" aria-label="切换阶段详情" @click="inspectorOpen = !inspectorOpen"><UiIcon name="panel-left" :size="17" /></button>
      </div>
    </header>
    <div v-if="addOpen" ref="addMenuEl" class="add-node-menu workflow-node-menu" :style="addMenuStyle" role="menu" aria-label="选择阶段类型" @pointerdown.stop>
      <label class="node-search"><UiIcon name="search" :size="14" /><input ref="nodeSearchEl" v-model="addSearch" aria-label="搜索工作流节点" placeholder="搜索节点…" autocomplete="off" @keydown="searchNodeKey" /></label>
      <button v-for="(type, index) in visibleNodeTypes" :key="type.type" role="menuitem" :class="{ 'search-active': searchIndex === index }" @pointerenter="searchIndex = index" @click="addNode(type.type)"><span class="type-dot" :style="{ background: type.color }"></span>{{ type.title }}<UiIcon name="plus" :size="13" /></button>
      <p v-if="!visibleNodeTypes.length" class="search-empty">没有匹配的节点</p>
    </div>
    <div v-if="active?.conflict" class="workflow-banner warning" role="alert"><UiIcon name="warning" :size="15" /><span>服务端版本已变化，当前草稿已保留。</span><div class="banner-actions"><button @click="reloadRemote">载入新版本</button><button @click="saveDraftOverLatest">用草稿保存</button></div></div>
    <div v-else-if="active?.loadError || active?.saveError" class="workflow-banner error" role="alert"><UiIcon name="warning" :size="15" /><span>{{ active.loadError || active.saveError }}</span><button @click="retry">重试</button></div>
    <div v-if="notice" class="workflow-banner" :class="noticeTone" :role="noticeTone ? 'alert' : 'status'"><UiIcon :name="noticeTone ? 'warning' : 'check'" :size="15" /><span>{{ notice }}</span><button class="dismiss-notice" aria-label="关闭提示" @click="notice = ''"><UiIcon name="close" :size="14" /></button></div>
    <div class="workflow-body" :class="{ 'inspector-open': inspectorOpen && selectedNode }">
      <div ref="canvasEl" class="workflow-canvas" :class="{ panning: gesture?.kind === 'pan', 'pan-ready': spaceHeld, connecting: gesture?.kind === 'connect', 'compact-view': compactView, 'dependencies-visible': showDependencies }" :style="dotStyle" tabindex="0" aria-label="工作流思维导图，可展开阶段、拖动节点并编辑执行依赖" @pointerdown.capture="beginForcedPan" @pointerdown="beginPan" @dblclick="onCanvasDoubleClick" @wheel.prevent="onWheel">
        <div v-if="workflow" class="canvas-world" :style="worldStyle">
          <svg class="workflow-connections" width="1" height="1" aria-label="思维导图分支与执行依赖">
            <defs><marker :id="arrowId" viewBox="0 0 10 10" refX="13" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M 1 1 L 9 5 L 1 9" fill="none" stroke="context-stroke" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></marker></defs>
            <path v-for="branch in mindMap.branches" :key="branch.id" class="mindmap-branch" :class="{ selected: selectedId === branch.target }" :d="branch.path" :style="{ '--stage-color': metaFor(nodeMap.get(branch.target)).color }" aria-hidden="true" />
            <template v-for="node in nodes" :key="`details-${node.id}`"><path v-for="detail in visualStage(node)?.details || []" :key="detail.key" class="mindmap-detail-branch" :d="detail.path" :style="{ '--stage-color': metaFor(node).color }" aria-hidden="true" /></template>
            <g v-for="edge in edges" v-show="dependencyVisible(edge)" :key="edge.id" class="workflow-edge" :class="{ selected: selectedEdgeId === edge.id, complete: nodeMap.get(edge.source)?.status === 'done', related: selectedId && adjacentEdges.has(edge.id), muted: selectedId && !adjacentEdges.has(edge.id) }" :style="{ '--link-color': metaFor(nodeMap.get(edge.source)).color }" :data-workflow-edge="edge.id">
              <path v-if="edgeRoutes.get(edge.id)" class="edge-line" :d="edgeRoutes.get(edge.id)" :marker-end="`url(#${arrowId})`" />
              <path v-if="edgeRoutes.get(edge.id)" class="edge-hit" :d="edgeRoutes.get(edge.id)" @pointerdown="selectEdge($event, edge.id)"><title>{{ nodeMap.get(edge.source)?.title }} → {{ nodeMap.get(edge.target)?.title }}</title></path>
            </g>
            <path v-if="previewPath" class="connection-preview" :d="previewPath" />
          </svg>
          <div class="workflow-root" :style="rectStyle(mindMap.root)" :title="mindMap.projected ? '原有节点位置已保留；拖动阶段或点击整理，可保存导图布局。阶段执行依赖保持原规则。' : '实线展示工作流主题分支。虚线箭头表示阶段执行依赖。'" data-workflow-root @pointerdown.stop @dblclick.stop="fitCanvas()"><span class="root-caption"><UiIcon name="git-branch" :size="14" />项目工作流<span v-if="mindMap.projected"> · 导图预览</span></span><strong :title="workflow.name">{{ workflow.name || '项目开发 SOP' }}</strong><span class="root-progress">{{ completed }} / {{ nodes.length }} 阶段已验收</span></div>
          <article v-for="(node, index) in nodes" :key="node.id" class="workflow-node" :class="{ selected: selectedId === node.id, done: node.status === 'done', active: node.status === 'active', ready: ready.has(node.id), expanded: expandedIds.has(node.id), 'branch-left': visualStage(node)?.side === 'left', dragging: gesture?.kind === 'node' && gesture?.nodeId === node.id }" :style="cardStyle(node)" :data-workflow-node="node.id" @click.stop="clickNode(node.id)" @dblclick.stop="focusNode(node.id, true)">
            <header class="node-header" tabindex="0" role="button" :aria-label="`查看${node.title}阶段详情`" @keydown.enter.stop.prevent="selectNode(node.id)" @keydown.space.stop.prevent="selectNode(node.id)" @pointerdown="beginDrag($event, node)"><span class="node-index">{{ String(index + 1).padStart(2, '0') }}</span><strong :title="node.title">{{ node.title }}</strong><UiIcon v-if="node.status === 'done'" name="check" :size="16" /><span v-else class="node-grip">⠿</span></header>
            <button class="workflow-port input" :class="{ 'input-hover': hoveredInput === node.id, 'input-valid': hoveredInput === node.id && gesture?.source && canConnect(workflow, gesture.source, node.id).valid }" :data-workflow-input="node.id" :aria-label="`${node.title}输入端口`" title="拖动其他节点的输出端口到这里" @pointerdown.stop></button>
            <button class="workflow-port output" :aria-label="`${node.title}输出端口`" title="拖动到下一个阶段的输入端口" @pointerdown="beginConnect($event, node)" @click.stop></button>
            <footer class="node-footer"><span class="node-status"><span class="status-dot"></span>{{ statusLabel(node) }}</span><button class="node-agent" :class="nodeAgent(node)?.state" :aria-label="`查看${node.title}智能体`" title="查看阶段智能体" @pointerdown.stop @click.stop="openNodeAgent(node)"><UiIcon name="git-branch" :size="12" /><span v-if="nodeAgent(node) && !compactView">{{ agentStateLabel(nodeAgent(node)) }}</span></button><button v-if="node.status === 'pending' && ready.has(node.id)" class="node-action" :aria-label="`开始${node.title}`" title="开始阶段" :disabled="active?.loading" @click.stop="requestStatus(node.id, 'active')"><UiIcon name="play" :size="14" /></button><button v-else-if="node.status === 'active'" class="node-action" :aria-label="`验收${node.title}`" title="填写产出并完成验收" :disabled="active?.loading" @click.stop="requestStatus(node.id, 'done')"><UiIcon name="check" :size="16" /></button><button class="node-expand" :aria-label="`${expandedIds.has(node.id) ? '收起' : '展开'}${node.title}分支`" :aria-expanded="expandedIds.has(node.id)" title="目标、产出与验收说明" @pointerdown.stop @click.stop="toggleDetails(node.id)"><span v-if="expandedIds.has(node.id)" aria-hidden="true">−</span><UiIcon v-else name="plus" :size="13" /></button></footer>
          </article>
          <template v-for="node in nodes" :key="`leaves-${node.id}`"><button v-for="detail in visualStage(node)?.details || []" :key="detail.key" class="mindmap-detail node-content" :class="{ missing: !detail.fullText?.trim() }" :style="detailStyle(node, detail)" :title="`${detail.label}：${detail.fullText || '待填写'}`" :aria-label="`${node.title}${detail.label}：${detail.text || '待填写'}`" :data-workflow-detail="`${node.id}:${detail.key}`" @pointerdown.stop @click.stop="openDetail(node.id, detail.key)"><span class="detail-label">{{ detail.label }}</span><span class="detail-text">{{ detail.text || '待填写' }}</span><UiIcon name="chevron-right" :size="12" /></button></template>
        </div>
        <div v-if="active?.loading" class="canvas-loading" role="status"><span class="loading-spinner"></span>读取工作流，可先浏览…</div>
        <div v-else-if="workflow && !nodes.length" class="canvas-empty"><span class="empty-icon"><UiIcon name="git-branch" :size="28" /></span><strong>从一个阶段开始</strong><button class="workflow-button primary-soft" @click="addOpen = true"><UiIcon name="plus" :size="15" />添加节点</button></div>
        <div class="canvas-hint"><span v-if="gesture?.kind === 'connect'">拖到输入端口以添加执行依赖</span><span v-else-if="selectedEdgeId">已选择执行依赖 · Delete 移除</span><span v-else>实线：主题分支 · 虚线箭头：执行依赖<br />Space + 拖动平移 · 双击空白添加</span></div>
        <button v-if="minimap && nodes.length > 1" class="workflow-minimap" aria-label="工作流缩略图导航" title="点击缩略图定位 · Enter 全览" @pointerdown.stop @click.stop="navigateMinimap">
          <svg viewBox="0 0 160 54" aria-hidden="true">
            <rect :x="minimap.x + mindMap.root.x * minimap.scale" :y="minimap.y + mindMap.root.y * minimap.scale" :width="mindMap.root.width * minimap.scale" :height="mindMap.root.height * minimap.scale" rx="1.5" fill="var(--wf-blue)" opacity=".8" />
            <rect v-for="node in nodes" :key="node.id" :x="minimap.x + visualStage(node).x * minimap.scale" :y="minimap.y + visualStage(node).y * minimap.scale" :width="WIDTH * minimap.scale" :height="HEIGHT * minimap.scale" rx="1.5" :fill="metaFor(node).color" :opacity="selectedId === node.id ? 1 : .45" />
            <rect v-if="minimap.viewport" class="minimap-viewport" v-bind="minimap.viewport" rx="3" />
          </svg>
        </button>
        <nav class="canvas-controls" aria-label="工作流视图控制" @pointerdown.stop><button class="icon-button" aria-label="缩小工作流" @click="zoomAt(viewport.zoom / 1.15)">−</button><button class="zoom-value" title="恢复 100%" @click="zoomAt(1)">{{ Math.round(viewport.zoom * 100) }}%</button><button class="icon-button" aria-label="放大工作流" @click="zoomAt(viewport.zoom * 1.15)">+</button><span></span><button class="fit-button" title="保持文字可读，从首个节点开始" @click="fitCanvas(true)">阅读</button><button class="fit-button" title="显示全部节点" @click="fitCanvas()">全览</button></nav>
        <button v-if="nextNode && !inspectorOpen" class="next-stage" @pointerdown.stop @click="focusNode(nextNode.id)"><span class="next-dot"></span><span>{{ nextNode.status === 'active' ? '进行中' : '下一步' }}</span><strong>{{ nextNode.title }}</strong><UiIcon name="arrow-right" :size="14" /></button>
      </div>
      <aside v-if="inspectorOpen && selectedNode" class="workflow-inspector" aria-label="阶段详情" @pointerdown.stop>
        <header class="inspector-header"><span class="type-dot" :style="{ background: metaFor(selectedNode).color }"></span><strong>{{ metaFor(selectedNode).title }}</strong><button class="icon-button" aria-label="关闭阶段详情" @click="inspectorOpen = false"><UiIcon name="close" :size="16" /></button></header>
        <div class="inspector-fields">
          <label class="inspector-field"><span>阶段名称</span><input :value="selectedNode.title" maxlength="120" :disabled="active?.loading" @input="updateNode('title', $event.target.value)" /></label>
          <div class="inspector-status" :class="selectedNode.status"><span class="status-dot"></span>{{ statusLabel(selectedNode) }}<span class="type-caption">{{ metaFor(selectedNode).title }}</span></div>
          <button class="assigned-agent" @click="openNodeAgent(selectedNode)"><UiIcon name="git-branch" :size="13" /><span>节点智能体</span><small>{{ nodeAgent(selectedNode) ? agentStateLabel(nodeAgent(selectedNode)) : '同步中' }}</small><UiIcon name="chevron-right" :size="12" /></button>
          <label class="inspector-field"><span>目标</span><textarea data-workflow-field="objective" :value="selectedNode.objective" maxlength="4000" rows="2" placeholder="这个阶段要解决什么？" :disabled="active?.loading" @input="updateNode('objective', $event.target.value)"></textarea></label>
          <label class="inspector-field"><span>交付物</span><textarea data-workflow-field="deliverable" :value="selectedNode.deliverable" maxlength="4000" rows="2" placeholder="阶段结束时交付什么？" :disabled="active?.loading" @input="updateNode('deliverable', $event.target.value)"></textarea></label>
          <label class="inspector-field"><span>验收标准</span><textarea data-workflow-field="acceptance" :value="selectedNode.acceptance" maxlength="4000" rows="3" placeholder="怎样判断这个阶段完成？" :disabled="active?.loading" @input="updateNode('acceptance', $event.target.value)"></textarea></label>
          <section class="stage-inputs dependency-section" aria-label="阶段执行依赖"><span class="field-caption">执行依赖 <small>决定阶段何时可以开始</small></span><p v-if="!predecessors.length && !successors.length" class="dependency-empty">当前阶段没有执行依赖。可从阶段输出端口连到其他阶段的输入端口。</p><span v-if="predecessors.length" class="dependency-caption">前置阶段</span><div v-for="before in predecessors" :key="before.id" class="dependency-row"><button :title="before.output || '尚未产出'" @click="focusNode(before.id)"><UiIcon :name="before.status === 'done' ? 'check' : 'clock'" :size="13" /><span>{{ before.title }}</span><UiIcon name="chevron-right" :size="13" /></button><button class="dependency-remove" :aria-label="`移除${before.title}到${selectedNode.title}的执行依赖`" :disabled="active?.loading" title="移除此执行依赖" @click="removeDependency(dependencyId(before.id, selectedNode.id))"><UiIcon name="close" :size="13" /></button></div><span v-if="successors.length" class="dependency-caption">后续阶段</span><div v-for="after in successors" :key="after.id" class="dependency-row"><button @click="focusNode(after.id)"><span>{{ after.title }}</span><UiIcon name="chevron-right" :size="13" /></button><button class="dependency-remove" :aria-label="`移除${selectedNode.title}到${after.title}的执行依赖`" :disabled="active?.loading" title="移除此执行依赖" @click="removeDependency(dependencyId(selectedNode.id, after.id))"><UiIcon name="close" :size="13" /></button></div></section>
          <label class="inspector-field output-field"><span>阶段产出 <small>验收必填</small></span><textarea data-output-field data-workflow-field="output" :value="selectedNode.output" maxlength="40000" rows="4" placeholder="填写结果、文档位置或验证记录…" :disabled="active?.loading" @input="updateNode('output', $event.target.value)"></textarea></label>
        </div>
        <footer class="inspector-actions">
          <button class="workflow-button ai-button" :disabled="active?.loading || (selectedNode.status === 'pending' && !ready.has(selectedNode.id))" title="交由 Lead 分配节点任务" @click="handoff"><UiIcon name="sparkles" :size="15" />交给 Lead</button>
          <button v-if="selectedNode.status === 'pending'" class="workflow-button solid" :disabled="active?.loading || !ready.has(selectedNode.id)" @click="requestStatus(selectedNode.id, 'active')"><UiIcon name="play" :size="14" />开始</button>
          <button v-else-if="selectedNode.status === 'active'" class="workflow-button solid" :disabled="active?.loading" @click="requestStatus(selectedNode.id, 'done')"><UiIcon name="check" :size="15" />完成验收</button>
          <button v-else class="workflow-button" :disabled="active?.loading" @click="resetNode(selectedNode.id)"><UiIcon name="refresh" :size="14" />重新处理</button>
          <div class="inspector-secondary"><button v-if="selectedNode.status === 'active'" :disabled="active?.loading" @click="resetNode(selectedNode.id)">退回待处理</button><button class="delete-button" :disabled="active?.loading" @click="deleteSelection"><UiIcon name="trash" :size="13" />移除节点</button></div>
        </footer>
      </aside>
    </div>
    <input ref="importEl" class="hidden-file-input" type="file" accept="application/json,.json" aria-label="导入工作流 JSON" @change="importWorkflow" />
  </section>
</template>

<style scoped>
.sop-workflow { --wf-border: var(--border, #e1e6ef); --wf-muted: var(--muted, #8590a3); --wf-text: var(--text, #283348); --wf-blue: var(--accent, #5972ee); position: relative; display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; height: 100%; background: var(--panel-soft, #f7f9fc); color: var(--wf-text); font-family: inherit; }
.sop-workflow button,.sop-workflow input,.sop-workflow textarea { font: inherit; }
.sop-workflow button { cursor: pointer; }
.sop-workflow button:disabled { cursor: default; opacity: .45; }
.sop-workflow button:focus-visible,.sop-workflow summary:focus-visible { outline: 2px solid #6e83fb; outline-offset: 2px; }
.workflow-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 16px; min-height: 54px; box-sizing: border-box; border-bottom: 1px solid var(--wf-border); background: #fff; z-index: 5; }
.workflow-heading { display: flex; align-items: center; gap: 9px; min-width: 0; }
.heading-icon { display: inline-flex; align-items: center; justify-content: center; width: 29px; height: 29px; border: 1px solid #e6ebfc; border-radius: 8px; background: #f3f6ff; color: #667be5; }
.workflow-heading strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 650; }
.workflow-progress { font-size: 11px; color: #7b879c; font-variant-numeric: tabular-nums; background: #f2f4f8; padding: 3px 6px; border-radius: 5px; }
.toolbar-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.workflow-button,.icon-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; border: 1px solid var(--wf-border); color: #536177; background: #fff; border-radius: 7px; height: 31px; padding: 0 10px; font-size: 12px !important; white-space: nowrap; }
.workflow-button:hover,.icon-button:hover { background: #f5f7fc; border-color: #cbd4e8; }
.icon-button { width: 30px; padding: 0; }
.workflow-history { display: flex; gap: 2px; }.workflow-history .icon-button { width: 27px; border-color: transparent; }
.workflow-button.primary-soft { background: #eff3ff; border-color: #d8e1ff; color: #5872e5; }
.workflow-button.solid { background: #6178ed; border-color: #6178ed; color: #fff; }
.workflow-button.solid:hover { background: #5068db; }
.toolbar-divider { height: 18px; width: 1px; background: #e8ecf3; margin: 0 3px; }
.save-indicator { display: inline-flex; align-items: center; justify-content: center; gap: 5px; border: 0; padding: 5px 2px; background: transparent; color: #8290a1; font-size: 11px !important; white-space: nowrap; }
.save-dot,.status-dot,.next-dot { display: inline-block; flex-shrink: 0; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.save-indicator.saved { color: #8795a4; }.save-indicator.saving { color: #6980df; }.save-indicator.warning { color: #b37a29; }
.save-indicator.saving .save-dot { animation: pulse 1s infinite; }
.inspector-toggle { border-color: transparent; color: #8190a3; }.inspector-toggle.selected { color: #647bea; background: #eff3ff; }
.add-node-wrap,.workflow-more { position: relative; }.add-node-menu,.more-menu { position: absolute; z-index: 12; top: calc(100% + 8px); right: 0; min-width: 176px; padding: 6px; border: 1px solid var(--wf-border); border-radius: 10px; background: #fff; box-shadow: 0 12px 28px #26334b16, 0 3px 8px #26334b08; }
.menu-caption { display: block; font-size: 10px; color: #95a0b0; padding: 5px 8px 7px; }
.add-node-menu button,.more-menu button { display: flex; align-items: center; gap: 9px; width: 100%; border: 0; border-radius: 6px; padding: 8px; background: #fff; text-align: left; color: #46536a; font-size: 12px; }
.add-node-menu button:hover,.more-menu button:hover { background: #f2f5fc; }.add-node-menu button .ui-icon { margin-left: auto; color: #a2aec0; }
.type-dot { display: inline-block; flex-shrink: 0; width: 7px; height: 7px; border-radius: 3px; }
.workflow-node-menu { top: auto; right: auto; width: 220px; max-width: calc(100% - 16px); z-index: 24; overflow-y: auto; box-sizing: border-box; }
.node-search { display: flex; align-items: center; gap: 8px; margin: 2px 2px 7px; padding: 7px 8px; border: 1px solid #e3e8f1; border-radius: 6px; background: #f8fafd; color: #8996ab; }.node-search:focus-within { border-color: #9aaaf0; }.node-search input { width: 100%; min-width: 0; border: 0; outline: none; box-shadow: none; padding: 0; min-height: 0; font-size: 12px; background: transparent; }.add-node-menu button.search-active { background: #f0f4ff; color: #5570d7; }.search-empty { margin: 12px 8px; color: #8c97a8; font-size: 12px; }
.workflow-more summary { list-style: none; cursor: pointer; border-color: transparent; }.workflow-more summary::-webkit-details-marker { display: none; }.more-menu { min-width: 142px; }
.workflow-banner { display: flex; align-items: center; gap: 8px; min-height: 33px; padding: 7px 15px; border-bottom: 1px solid #e3eaf5; background: #eff4fc; color: #657d9e; font-size: 12px; box-sizing: border-box; }
.workflow-banner>span { min-width: 0; flex: 1; overflow-wrap: anywhere; }.workflow-banner.warning { background: #fffaee; border-color: #f2e5c8; color: #a17b37; }.workflow-banner.error { background: #fff4f4; border-color: #f4dddd; color: #bf6262; }
.workflow-banner button { flex-shrink: 0; border: 1px solid currentColor; border-radius: 5px; background: #ffffffa8; color: inherit; padding: 3px 7px; font-size: 11px; }.banner-actions { display: flex; gap: 5px; }.workflow-banner .dismiss-notice { display: flex; border: 0; padding: 0; background: transparent; }
.workflow-body { display: flex; flex: 1; min-width: 0; min-height: 0; position: relative; }
.workflow-canvas { flex: 1; min-width: 0; min-height: 0; overflow: hidden; position: relative; container-type: inline-size; background-color: #f8fafc; background-image: radial-gradient(circle, #dbe2ed .8px, transparent .8px); outline: none; touch-action: none; }
.workflow-canvas.panning { cursor: grabbing; }.workflow-canvas.connecting { cursor: crosshair; }.workflow-canvas:focus-visible { box-shadow: inset 0 0 0 2px #8493ff55; }
.workflow-canvas.pan-ready,.workflow-canvas.pan-ready * { cursor: grab !important; }.workflow-canvas.pan-ready.panning,.workflow-canvas.pan-ready.panning * { cursor: grabbing !important; }
.canvas-world { position: absolute; top: 0; left: 0; width: 0; height: 0; transform-origin: 0 0; }
.workflow-connections { position: absolute; top: 0; left: 0; overflow: visible; pointer-events: none; }
.edge-line { fill: none; stroke: var(--link-color, #9baecb); stroke-opacity: .65; stroke-width: 1.8; stroke-dasharray: 5 5; stroke-linecap: round; stroke-linejoin: round; }.workflow-edge.complete .edge-line { stroke: #77b6a2; }.workflow-edge.related .edge-line { stroke-opacity: 1; stroke-width: 2.4; }.workflow-edge.muted .edge-line { stroke-opacity: .26; }.workflow-edge.selected .edge-line { stroke: var(--wf-blue); stroke-opacity: 1; stroke-width: 2.6; }
.edge-hit { fill: none; stroke: transparent; stroke-width: 14; pointer-events: stroke; cursor: pointer; }.connection-preview { fill: none; stroke: #758aed; stroke-width: 2.4; stroke-dasharray: 6 4; }
.workflow-node { position: absolute; width: 240px; height: 84px; box-sizing: border-box; display: flex; flex-direction: column; border: 1px solid var(--wf-border); border-radius: 9px; background: var(--panel, #fff); box-shadow: var(--shadow-sm, 0 3px 10px #293c5907); cursor: pointer; user-select: none; }
.workflow-node.selected { border-color: var(--stage-color); box-shadow: 0 0 0 2px color-mix(in srgb, var(--stage-color) 15%, transparent), 0 7px 18px #2d426810; }.workflow-node.active { box-shadow: 0 0 0 1px color-mix(in srgb, var(--stage-color) 17%, transparent), 0 3px 12px #293c5910; }.workflow-node.done { background: #fcfefd; }.workflow-node.dragging { z-index: 3; box-shadow: 0 8px 24px #26334b25; cursor: grabbing; }
.node-header { display: flex; align-items: center; gap: 8px; padding: 10px 12px 6px; min-height: 44px; box-sizing: border-box; cursor: grab; }
.node-header strong { font-size: 15px; font-weight: 650; line-height: 20px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }.node-header>.ui-icon { color: #6eae97; }.node-index { color: var(--stage-color); font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; }.node-grip { font-size: 16px; line-height: 16px; color: #c3ccdb; }
.node-content { flex: 1; min-height: 0; padding: 13px 15px 10px; display: flex; flex-direction: column; justify-content: space-between; gap: 8px; }
.compact-view .node-header strong { font-size: 18px; line-height: 22px; }.compact-view .node-index { font-size: 13px; }.compact-view .node-grip { display: none; }.compact-view .node-status { font-size: 12px; }
.node-objective { margin: 0; font-size: 13.5px; color: #556277; line-height: 1.55; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.node-deliverable { display: flex; align-items: center; gap: 6px; color: #8a96a8; background: #f5f7fb; padding: 5px 7px; border-radius: 5px; font-size: 12px; line-height: 17px; min-width: 0; }.node-deliverable>span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.node-footer { flex: 1; display: flex; align-items: center; gap: 4px; min-height: 34px; padding: 0 8px 3px 12px; }
.node-status { display: flex; align-items: center; gap: 6px; color: #94a0b3; font-size: 11px; line-height: 17px; }.workflow-node.ready .node-status { color: #7889c4; }.workflow-node.active .node-status { color: #6c83db; }.workflow-node.done .node-status { color: #6aa68f; }
.node-action { display: flex; align-items: center; justify-content: center; width: 25px; height: 24px; border: 0; border-radius: 5px; background: #f0f4ff; color: #6e82d9; }.node-action:hover { background: #e5ecff; }
.workflow-port { position: absolute; z-index: 3; width: 13px; height: 13px; min-width: 0; min-height: 0; box-sizing: border-box; border: 2px solid var(--panel, #fff); outline: 1.5px solid var(--border-strong, #aab8d0); border-radius: 50%; background: var(--panel-soft, #edf2fa); top: 34.5px; padding: 0; touch-action: none; opacity: 0; }.workflow-port.input { left: -7.5px; }.workflow-port.output { right: -7.5px; background: var(--stage-color); outline-color: var(--stage-color); cursor: crosshair; }.workflow-node:hover .workflow-port,.workflow-node:focus-within .workflow-port,.workflow-node.selected .workflow-port,.dependencies-visible .workflow-port,.connecting .workflow-port { opacity: 1; }.workflow-port:hover { transform: scale(1.25); }.workflow-port.input-hover { transform: scale(1.4); background: #f3a39b; outline-color: #d9867e; }.workflow-port.input-valid { background: #77bdad; outline-color: #77bdad; }
.canvas-hint { position: absolute; bottom: 79px; left: 18px; pointer-events: none; font-size: 10px; color: #98a4b7; line-height: 16px; }
.workflow-minimap { position: absolute; left: 14px; bottom: 12px; display: block; width: 160px; height: 54px; min-height: 0; padding: 0; overflow: hidden; border: 1px solid #e2e7f0; border-radius: 8px; background: #ffffffed; box-shadow: 0 2px 8px #26334b07; cursor: crosshair; }
.workflow-minimap:hover { border-color: #c4cfea; }.workflow-minimap svg { display: block; width: 100%; height: 100%; pointer-events: none; }.minimap-viewport { fill: #657de916; stroke: #7c91df; stroke-width: 1; }
@container (max-width: 420px) { .workflow-minimap { width: 132px; height: 45px; } }
@container (max-width: 350px) { .workflow-minimap { display: none; } }
.canvas-controls { position: absolute; bottom: 12px; right: 14px; display: flex; align-items: center; padding: 3px; gap: 1px; background: #ffffffed; border: 1px solid #e2e7f0; border-radius: 8px; box-shadow: 0 2px 8px #26334b07; }.canvas-controls .icon-button { border: 0; font-size: 19px !important; width: 28px; height: 27px; color: #7b889e; }.canvas-controls>span { height: 14px; width: 1px; background: #e8edf4; margin: 0 4px; }.zoom-value,.fit-button { border: 0; border-radius: 5px; background: transparent; color: #76839a; height: 27px; padding: 0 7px; font-size: 11px !important; }.zoom-value { width: 47px; padding: 0; font-variant-numeric: tabular-nums; }.zoom-value:hover,.fit-button:hover { background: #f2f5fb; }
.next-stage { position: absolute; top: 14px; left: 16px; display: flex; align-items: center; gap: 7px; max-width: calc(100% - 32px); border: 1px solid #e3e8f2; background: #ffffffef; color: #8b98ac; padding: 7px 10px; border-radius: 7px; font-size: 11px !important; box-shadow: 0 2px 8px #26334b05; }.next-stage strong { font-size: 11px; font-weight: 600; color: #657899; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }.next-dot { background: #7b91e6; }
.canvas-loading { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); display: flex; align-items: center; gap: 9px; color: #8090a7; font-size: 12px; padding: 12px 16px; background: #ffffffdd; border: 1px solid var(--wf-border); border-radius: 8px; }.loading-spinner { width: 13px; height: 13px; border: 2px solid #e1e7f7; border-top-color: #8a9ce5; border-radius: 50%; animation: spin .8s linear infinite; }
.canvas-empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 15px; color: #8995a9; }.canvas-empty strong { font-size: 13px; font-weight: 500; }.empty-icon { display: inline-flex; align-items: center; justify-content: center; width: 58px; height: 58px; border-radius: 15px; border: 1px solid #e1e7f3; background: #ffffffaa; color: #95a7d1; }
.workflow-inspector { flex-shrink: 0; width: 278px; display: flex; flex-direction: column; border-left: 1px solid var(--wf-border); background: #fff; min-height: 0; z-index: 4; }
.node-agent { display: inline-flex; align-items: center; gap: 3px; border: 0; background: transparent; color: #91a0b3; padding: 0; margin-left: auto; margin-right: 5px; font-size: 9px; }.node-agent.running,.node-agent.waiting { color: #7382d6; }.node-agent.failed { color: #c57777; }.node-agent.completed { color: #6aa58f; }.assigned-agent { display: flex; align-items: center; gap: 6px; width: 100%; padding: 7px 8px; border: 1px solid #e7ecf4; border-radius: 6px; background: #f8faff; color: #6e7c96; font-size: 11px; }.assigned-agent small { margin-left: auto; font-size: 10px; color: #98a4b8; }
.inspector-header { display: flex; align-items: center; gap: 8px; padding: 10px 13px; min-height: 46px; border-bottom: 1px solid #edf0f5; box-sizing: border-box; }.inspector-header strong { font-size: 12px; font-weight: 600; flex: 1; }.inspector-header .icon-button { border-color: transparent; width: 25px; height: 25px; color: #96a0b1; }
.inspector-fields { flex: 1; min-height: 0; overflow: auto; padding: 13px; display: flex; flex-direction: column; gap: 13px; }
.inspector-field { display: flex; flex-direction: column; gap: 6px; }.inspector-field>span,.field-caption { color: #8490a3; font-size: 11px; line-height: 16px; }.inspector-field input,.inspector-field textarea { width: 100%; box-sizing: border-box; padding: 8px 9px; border: 1px solid #e2e7f0; border-radius: 6px; color: #4c596f; background: #fcfdff; font-size: 12px; line-height: 1.55; outline: none; }.inspector-field textarea { resize: vertical; min-height: 53px; }.inspector-field input:focus,.inspector-field textarea:focus { border-color: #98a9f5; box-shadow: 0 0 0 2px #eff2ff; background: #fff; }.inspector-field textarea::placeholder { color: #a9b3c2; }.inspector-field small { font-size: 10px; color: #a4adbd; font-weight: 400; margin-left: 5px; }
.inspector-status { display: flex; align-items: center; gap: 6px; color: #8996ab; font-size: 11px; margin-top: -3px; }.inspector-status.active { color: #6f85d7; }.inspector-status.done { color: #6aa58f; }.type-caption { margin-left: auto; color: #a6afbd; font-size: 10px; }
.stage-inputs { display: flex; flex-direction: column; gap: 4px; }.stage-inputs button { display: flex; align-items: center; gap: 6px; min-width: 0; padding: 5px 7px; border: 1px solid #eef1f6; border-radius: 5px; background: #f8fafd; color: #8390a5; font-size: 11px; text-align: left; }.stage-inputs button>span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }.stage-inputs button>.ui-icon:last-child { margin-left: auto; }.stage-inputs button:hover { border-color: #dce4f3; color: #697da5; }
.output-field textarea { background: #fbfcff; }.output-field>span { color: #7182a3; }
.inspector-actions { display: flex; flex-wrap: wrap; gap: 7px; padding: 11px 13px 9px; border-top: 1px solid #edf0f5; background: #fff; }.inspector-actions>.workflow-button { flex: 1; padding: 0 7px; }.ai-button { color: #7c74c8; border-color: #e5e0f8; background: #faf8ff; }.ai-button:hover { background: #f3f0ff; border-color: #d8cef2; }.inspector-secondary { display: flex; align-items: center; justify-content: space-between; width: 100%; gap: 5px; }.inspector-secondary button { display: inline-flex; align-items: center; gap: 4px; border: 0; color: #9ca5b4; background: transparent; padding: 5px 0; font-size: 10px; }.inspector-secondary .delete-button { margin-left: auto; }.inspector-secondary .delete-button:hover { color: #c47878; }
.hidden-file-input { display: none; }
.mindmap-branch,.mindmap-detail-branch { fill: none; stroke: var(--stage-color); stroke-linecap: round; stroke-linejoin: round; pointer-events: none; }
.mindmap-branch { stroke-width: 2.3; opacity: .66; }.mindmap-branch.selected { opacity: 1; stroke-width: 3; }.mindmap-detail-branch { stroke-width: 1.5; opacity: .5; }
.workflow-root { position: absolute; display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 4px; padding: 11px 17px; border: 1px solid var(--accent-border, #c8d3ff); border-radius: 13px; background: var(--accent-soft, #edf1ff); box-shadow: var(--shadow-sm); color: var(--wf-text); user-select: none; }
.workflow-root strong { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 16px; line-height: 24px; font-weight: 650; }.root-caption { display: flex; align-items: center; gap: 5px; color: var(--wf-blue); font-size: 11px; }.root-progress { color: var(--wf-muted); font-size: 11px; }
.workflow-node::before { content: ""; position: absolute; left: -1px; top: 9px; bottom: 9px; width: 3px; border-radius: 2px; background: var(--stage-color); pointer-events: none; }.workflow-node.branch-left::before { left: auto; right: -1px; }.workflow-node.done { background: var(--panel, #fff); }.node-header:focus-visible { outline: 2px solid var(--wf-blue); outline-offset: 2px; border-radius: 7px; }
.node-status { min-width: 0; margin-right: auto; white-space: nowrap; }.node-agent { flex-shrink: 0; max-width: 70px; margin-left: 0; margin-right: 0; color: var(--wf-muted); }.node-agent span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.node-action,.node-expand { flex-shrink: 0; min-height: 24px; }.node-expand { display: flex; align-items: center; justify-content: center; width: 23px; height: 23px; padding: 0; border: 1px solid var(--wf-border); border-radius: 50%; background: var(--panel, #fff); color: var(--stage-color); }.node-expand:hover { background: var(--hover, #f2f5fc); }.node-expand[aria-expanded="true"] { background: color-mix(in srgb, var(--stage-color) 10%, var(--panel, #fff)); border-color: color-mix(in srgb, var(--stage-color) 40%, var(--wf-border)); }
.mindmap-detail.node-content { position: absolute; display: flex; flex-direction: row; align-items: center; justify-content: flex-start; gap: 7px; min-height: 0; margin: 0; padding: 0 9px; border: 1px solid var(--wf-border); border-left: 2px solid var(--stage-color); border-radius: 6px; background: var(--panel, #fff); text-align: left; white-space: nowrap; box-shadow: var(--shadow-sm); }.mindmap-detail:hover { border-color: var(--stage-color); background: var(--panel-soft, #f7f9fc); }.detail-label { flex-shrink: 0; font-size: 10px; color: var(--stage-color); }.detail-text { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; font-size: 11px; color: var(--text-2, #556277); }.mindmap-detail>.ui-icon { flex-shrink: 0; color: var(--wf-muted); }.mindmap-detail.missing .detail-text { color: var(--faint, #8590a3); }
.dependency-toggle.selected { color: var(--wf-blue); border-color: var(--accent-border, #c8d3ff); background: var(--accent-soft, #edf1ff); }.dependency-section { padding-top: 10px; border-top: 1px solid var(--wf-border); }.dependency-section .field-caption { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }.field-caption small { font-size: 10px; color: var(--wf-muted); }.dependency-caption { color: var(--wf-muted); font-size: 10px; margin-top: 4px; }.dependency-empty { margin: 0; font-size: 11px; line-height: 1.6; color: var(--wf-muted); }.dependency-row { display: flex; gap: 4px; }.dependency-row>button:first-child { flex: 1; }.dependency-row>.dependency-remove { flex-shrink: 0; width: 25px; padding: 0; justify-content: center; }.dependency-remove:hover { color: var(--err, #c47878); }
.workflow-toolbar,.workflow-inspector,.inspector-actions,.add-node-menu,.more-menu { background: var(--panel, #fff); }.workflow-button,.icon-button { background: var(--panel, #fff); color: var(--text-2, #536177); }.workflow-button:hover,.icon-button:hover,.add-node-menu button:hover,.more-menu button:hover { background: var(--hover, #f5f7fc); border-color: var(--border-strong, #cbd4e8); }.workflow-button.primary-soft,.inspector-toggle.selected { background: var(--accent-soft, #eff3ff); border-color: var(--accent-border, #d8e1ff); color: var(--wf-blue); }.workflow-button.solid { color: var(--on-accent, #fff); background: var(--wf-blue); border-color: var(--wf-blue); }.workflow-button.solid:hover { background: var(--accent-hover, #5068db); }.heading-icon { border-color: var(--accent-border, #e6ebfc); background: var(--accent-soft, #f3f6ff); color: var(--wf-blue); }.workflow-progress { color: var(--wf-muted); background: var(--panel-soft, #f2f4f8); }.toolbar-divider { background: var(--wf-border); }.save-indicator.saved,.inspector-toggle,.canvas-hint { color: var(--wf-muted); }.save-indicator.warning { color: var(--warn, #b37a29); }.save-indicator.saving { color: var(--wf-blue); }
.workflow-canvas { background-color: var(--panel-soft, #f8fafc); background-image: radial-gradient(circle, var(--border-strong, #dbe2ed) .8px, transparent .8px); }.add-node-menu button,.more-menu button { background: var(--panel, #fff); color: var(--text-2, #46536a); }.node-search { border-color: var(--wf-border); background: var(--panel-soft, #f8fafd); color: var(--wf-muted); }.node-search input { color: var(--wf-text); }.add-node-menu button.search-active { color: var(--wf-blue); background: var(--accent-soft, #f0f4ff); }
.workflow-minimap,.canvas-controls,.next-stage,.canvas-loading { background: var(--panel, #fff); border-color: var(--wf-border); }.next-stage,.next-stage strong,.canvas-loading,.canvas-controls .icon-button,.zoom-value,.fit-button { color: var(--wf-muted); }.zoom-value:hover,.fit-button:hover { background: var(--hover, #f2f5fb); }.canvas-controls>span { background: var(--wf-border); }.workflow-banner { background: var(--accent-soft, #eff4fc); border-color: var(--accent-border, #e3eaf5); color: var(--text-2, #657d9e); }.workflow-banner.warning { background: var(--warn-soft, #fffaee); border-color: var(--wf-border); color: var(--warn, #a17b37); }.workflow-banner.error { background: var(--err-soft, #fff4f4); border-color: var(--wf-border); color: var(--err, #bf6262); }.workflow-banner button { background: var(--panel, #fff); }.workflow-banner .dismiss-notice { background: transparent; }
.inspector-header,.inspector-actions { border-color: var(--wf-border); }.inspector-field>span,.field-caption,.type-caption,.inspector-secondary button,.inspector-status,.assigned-agent small { color: var(--wf-muted); }.inspector-field input,.inspector-field textarea,.output-field textarea { border-color: var(--wf-border); background: var(--panel-soft, #fcfdff); color: var(--text-2, #4c596f); }.inspector-field input:focus,.inspector-field textarea:focus { border-color: var(--accent-border, #98a9f5); box-shadow: var(--focus-ring); background: var(--panel, #fff); }.inspector-field textarea::placeholder { color: var(--placeholder, #a9b3c2); }.assigned-agent,.stage-inputs button { border-color: var(--wf-border); background: var(--panel-soft, #f8fafd); color: var(--text-2, #8390a5); }.ai-button { background: var(--panel, #faf8ff); color: var(--wf-blue); border-color: var(--accent-border, #e5e0f8); }.ai-button:hover { background: var(--accent-soft, #f3f0ff); border-color: var(--accent-border, #d8cef2); }.node-action { background: var(--accent-soft, #f0f4ff); color: var(--wf-blue); }.node-action:hover { background: var(--hover, #e5ecff); }
@keyframes spin { to { transform: rotate(360deg); } } @keyframes pulse { 50% { opacity: .35; } }
@media (max-width: 760px) { .workflow-inspector { position: absolute; top: 0; right: 0; bottom: 0; width: min(278px, calc(100% - 28px)); box-shadow: -5px 0 20px #28394b0a; }.workflow-toolbar { padding: 9px 10px; gap: 7px; }.workflow-heading { gap: 6px; }.heading-icon { width: 25px; height: 25px; }.workflow-heading strong { max-width: 140px; font-size: 12px; }.toolbar-actions { gap: 3px; }.toolbar-divider { margin: 0 1px; }.workflow-button { padding: 0 7px; }.canvas-hint { display: none; } }
@media (max-width: 480px) { .workflow-heading strong { max-width: 68px; }.heading-icon { display: none; }.workflow-history { gap: 0; }.workflow-history .icon-button { width: 24px; }.workflow-progress { padding: 3px 4px; font-size: 10px; }.toolbar-actions>.workflow-button span { display: none; }.toolbar-actions>.workflow-button { width: 29px; padding: 0; }.save-indicator { font-size: 10px !important; }.inspector-toggle { display: none; }.workflow-banner { padding: 7px 10px; flex-wrap: wrap; font-size: 11px; }.workflow-banner>span { flex: 1 1 calc(100% - 24px); }.banner-actions { padding-left: 23px; }.workflow-heading { gap: 5px; }.canvas-controls { right: 10px; bottom: 10px; }.workflow-minimap { left: 10px; bottom: 10px; width: 132px; height: 45px; }.next-stage { top: 10px; left: 10px; } }
@media (max-width: 480px) { .workflow-toolbar { flex-wrap: wrap; gap: 5px; }.workflow-heading { width: 100%; }.workflow-heading strong { max-width: none; flex: 1; }.toolbar-actions { flex: 1; min-width: 0; width: 100%; justify-content: flex-end; gap: 4px; }.toolbar-actions .workflow-history { margin-right: auto; }.save-indicator { max-width: 72px; overflow: hidden; }.mindmap-detail.node-content { padding: 0 7px; }.workflow-progress { margin-left: auto; } }
@media (prefers-reduced-motion: reduce) { .loading-spinner,.save-indicator.saving .save-dot { animation: none; } }
</style>
