<script setup>
// 代码模式主界面：编辑器、思维导图与前端设计共享项目和常驻 AI 会话。
//
// 边界（**截至 P10 的实际状态**，早期这里写的是 P0 的"什么都没有"，已过期）：
// - 地图（P1）、Monaco 编辑器（P2）、LSP（P3，WS 直连语言服务器）、隔离工作树与
//   git 面板（P4）、并行车道（P5）、影响面/决策/溯源（P6）、行为卡片（P7）
//   都已经在右侧面板里真实接后端；`features` 位仍由后端下发，界面上**不放假按钮**。
// - 人工编辑与 AI 权限独立；AI 自动写入仍受项目开关与会话权限控制。
//
// 三栏宽度可拖，比例存 localStorage：IDE 类界面里"调过一次就希望它记住"，
// 每次进来重拖是纯损耗。
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'
import UiIcon from '../components/ui/UiIcon.vue'
import AgentLogo from '../components/ui/AgentLogo.vue'
import FileTree from '../components/code/FileTree.vue'
import EditorPane from '../components/code/EditorPane.vue'
import AiPanel from '../components/code/AiPanel.vue'
import ProjectPicker from '../components/code/ProjectPicker.vue'
import ProjectSwitcher from '../components/code/ProjectSwitcher.vue'
import EditorTabs from '../components/code/EditorTabs.vue'
import QuickOpen from '../components/code/QuickOpen.vue'
import ProblemsPanel from '../components/code/ProblemsPanel.vue'
import RuntimePanel from '../components/code/RuntimePanel.vue'
import DebugPanel from '../components/code/DebugPanel.vue'
import { changeReviews } from '../lib/changeReviewClient.js'
import { documentKey, useWorkbench } from '../composables/useWorkbench.js'
import { useNavigation } from '../composables/useNavigation.js'
import { useProjectAccess } from '../composables/useProjectAccess.js'
import { useFunctionDescriptions } from '../composables/useFunctionDescriptions.js'
import { useIndexRefresh } from '../composables/useIndexRefresh.js'
import { useDialog } from '../composables/useDialog.js'
import { prepareWorkspaceEdit, assertPlanCurrent, findPlanDocument } from '../lib/workspaceEdits.js'
import { useProjectAgentTeam } from '../lib/agentTeam.js'
import { useProjectConversations } from '../composables/useProjectConversations.js'

// Optional IDE tools are fetched on first use, then remain mounted to retain their state.
const MindMap = defineAsyncComponent(() => import('../components/code/MindMap.vue'))
const FrontendDesign = defineAsyncComponent(() => import('../components/code/FrontendDesign.vue'))
const PatchPanel = defineAsyncComponent(() => import('../components/code/PatchPanel.vue'))
const SourceControl = defineAsyncComponent(() => import('../components/code/SourceControl.vue'))
const WorkItemsPanel = defineAsyncComponent(() => import('../components/code/WorkItemsPanel.vue'))
const ChangesPanel = defineAsyncComponent(() => import('../components/code/ChangesPanel.vue'))

const props = defineProps({ user: { type: Object, required: true }, visible: { type: Boolean, default: true } })
const emit = defineEmits(['open-settings'])
const dialog = useDialog()
const workbench = useWorkbench({ storageKey: `code-agent-workbench-v1:${props.user?.id || props.user?.user_id || 'local'}` })
const documents = workbench.documents
const activeDocumentKey = workbench.activeKey
const manualReadonly = ref(false)
const quickOpen = ref(false)
const quickMode = ref('files')
const leftVisible = ref(true)
const rightVisible = ref(true)
const problemsVisible = ref(false)
const closingDocument = ref(null)
const conflictDialog = ref(null)
const languageEditPreview = ref(null)
const languageEditSelected = ref(0)
const languageEditBusy = ref(false)
const languageEditUndo = ref(null)
let languageEditRequest = 0
const languageEditRow = computed(() => languageEditPreview.value?.rows[languageEditSelected.value] || null)
const editorSelection = ref(null)
const contextEnabled = ref(true)
const navigation = useNavigation()
const navigationBusy = ref(false)
const bottomTab = ref('problems')
const runtimeActivated = ref(false)
const debugActivated = ref(false)
const runtimeRef = ref(null)
const debugRef = ref(null)
const debugLocation = ref(null)
const breakpoints = ref([])
const executionBusy = ref(false)
const dirtyPaths = computed(() => documents.value.filter(doc => doc.dirty).map(doc => doc.path))
const workspaceCaseInsensitive = computed(() => /^[a-z]:[\\/]/i.test(project.value?.root_path || '') || /^[/\\]{2}/.test(project.value?.root_path || ''))
const selectedCode = computed(() => {
  const selection = editorSelection.value
  if (!selection || selection.key !== file.value?.key || !contextEnabled.value) return ''
  if (selection.start_line === selection.end_line && selection.start_column === selection.end_column) return ''
  const lines = (file.value?.text || '').split(/\r?\n/).slice(selection.start_line - 1, selection.end_line)
  if (!lines.length) return ''
  lines[lines.length - 1] = lines[lines.length - 1].slice(0, selection.end_column - 1)
  lines[0] = lines[0].slice(selection.start_column - 1)
  return lines.join('\n').slice(0, 12000)
})

const project = ref(null)
const banner = ref('')
const pickerOpen = ref(false)
const pickerMode = ref('open')
const projectLocationSelection = ref(null)
const projectAccess = useProjectAccess({
  client: api.code,
  storageKey: `code-agent-active-project-v2:${props.user?.id || props.user?.user_id || 'local'}`,
  onActivate: activateWorkspace,
  onClose: () => { pickerOpen.value = false; pickerMode.value = 'open'; projectLocationSelection.value = null; clearProjectState(); stopDiagnosticsPolling() },
})
const { projects, meta, loading, phase: projectPhase, error: projectError, busy: projectBusy } = projectAccess

const file = ref(null)
const debugSupported = computed(() => /\.(py|java)$/i.test(file.value?.path || ''))
const fileBusy = ref(false)
const fileLine = ref(0)              // 仅显式打开/跳转时高亮，切标签恢复原视口
const fileColumn = ref(1)
const centerView = ref('file')        // file | map | design
const mapActivated = ref(false)
const designActivated = ref(false)
const designAssistantVisible = ref(false)
const designAiResult = ref(null)
const leftShown = computed(() => leftVisible.value && centerView.value !== 'design')
const rightShown = computed(() => rightVisible.value && (centerView.value !== 'design' || designAssistantVisible.value))
const mobilePanel = ref('center')
const rightTab = ref('ai')           // ai | patches
const changesVisible = computed(() => props.visible !== false && rightShown.value && rightTab.value === 'changes')
const rightActivated = ref(new Set(['ai']))
const session = ref(null)
const sessionBusy = ref(false)
const saving = ref(false)
const editorRef = ref(null)
const editorToolsTarget = ref(null)
const treeRef = ref(null)
const mapRef = ref(null)
const askPrompt = ref('')
const pendingAgentNodeId = ref('')
const pendingWorkItemSession = ref(null)
const notice = ref('')
const indexProgress = useIndexRefresh({ client: api.code, onDone: () => {
  if (activeTree.value === 'main') mapRef.value?.loadMap?.({ keepExpand: true })
} })
const { state: indexRefresh, statusError: indexStatusError, retrying: indexRetrying } = indexProgress
const functionDescriptions = useFunctionDescriptions({ client: api.code })
const { presentation: functionDescriptionStatus } = functionDescriptions
const selectedSymbol = ref(null)
const patchRefresh = ref(0)
//: 显式跳转序号：只有"用户主动打开文件/点名跳某一行"时才自增。
//: 编辑器那边靠它区分"用户要跳"和"父组件又渲染了一次"——后者以前会把视口拽回高亮行。
const fileJump = ref(0)
const lspServers = ref([])          // 项目语言服务器探测结果
const diagnostics = ref(null)       // { seq, counts, files, symbols }
const activeTree = ref('main')      // 地图看哪棵树：main | task:<id>
const conversations = useProjectConversations({
  projectId: () => project.value?.available ? project.value.id : '',
  tree: () => activeTree.value, leadSessionId: () => session.value?.id || '',
  userId: () => props.user?.id || props.user?.user_id || 'local',
  visible: () => props.visible !== false && rightShown.value && rightTab.value === 'ai',
  client: api.code, storage: typeof localStorage === 'undefined' ? null : localStorage,
  onDefaultChanged: change => {
    if (disposed || project.value?.id !== change.projectId || session.value?.id !== change.deletedSessionId) return
    // Only replace conversation metadata. Editor models, dirty buffers and
    // per-conversation drafts retain their own identities and are untouched.
    sessionRequest++
    project.value = { ...project.value, session_id: change.defaultSessionId }
    session.value = change.defaultConversation
    sessionBusy.value = false
  },
})
const aiSessionId = computed(() => conversations.activeRootId.value || session.value?.id || '')
const agentTeam = useProjectAgentTeam({
  projectId: () => project.value?.available ? project.value.id : '', tree: () => 'main',
  visible: () => props.visible !== false && !!aiSessionId.value && ((rightShown.value && rightTab.value === 'ai') || (centerView.value === 'map' && mobilePanel.value !== 'projects')),
  leadSessionId: () => session.value?.id || '', conversationId: () => aiSessionId.value, client: api.code,
})
const conversationDraftScope = computed(() => `${props.user?.id || props.user?.user_id || 'local'}:${project.value?.id || ''}`)
const conversationPermissionBusy = ref('')
const activeTaskId = ref('')
const taskPanel = ref(null)
const scmPanel = ref(null)
const taskDiff = ref(null)          // 当前任务树的符号级差异（地图叠加用）
let diagTimer = null
let disposed = false
let projectVersion = 0
let fileRequest = 0
let sessionRequest = 0
let taskRequest = 0
let diagnosticsPending = ''
const pendingFileReads = new Map()
const pendingDocumentRestores = new Map()
const startedProjectIntel = new Set()
let dragCleanup = null

function isCurrentProject(pid, version) {
  return !disposed && project.value?.id === pid && projectVersion === version
}

function clearProjectState() {
  indexProgress.reset()
  functionDescriptions.reset()
  captureEditor()
  workbench.flush()
  projectVersion += 1
  pendingFileReads.clear()
  pendingDocumentRestores.clear()
  startedProjectIntel.clear()
  fileRequest += 1
  sessionRequest += 1
  taskRequest += 1
  project.value = null
  navigation.activate('')
  debugLocation.value = null
  breakpoints.value = []
  runtimeActivated.value = false
  debugActivated.value = false
  executionBusy.value = false
  file.value = null
  session.value = null
  selectedSymbol.value = null
  lspServers.value = []
  diagnostics.value = null
  activeTree.value = 'main'
  activeTaskId.value = ''
  taskDiff.value = null
  askPrompt.value = ''
  pendingAgentNodeId.value = ''
  pendingWorkItemSession.value = null
  notice.value = ''
  fileBusy.value = false
  fileLine.value = 0
  sessionBusy.value = false
  saving.value = false
  centerView.value = 'file'
  mapActivated.value = false
  designActivated.value = false
  designAssistantVisible.value = false
  designAiResult.value = null
  rightActivated.value = new Set(['ai', rightTab.value])
  closingDocument.value = conflictDialog.value = null
  languageEditRequest += 1
  languageEditPreview.value = languageEditUndo.value = null
  languageEditBusy.value = false
  quickOpen.value = false
  editorSelection.value = null
  workbench.activateProject('')
}

const writable = computed(() => ['workspace-write', 'full-access'].includes(session.value?.permission_mode))
const executionWritable = computed(() => conversations.isChat.value
  ? !!project.value?.allow_direct_write && ['workspace-write', 'full-access'].includes(conversations.selected.value?.effective_permission || conversations.selected.value?.permission_mode)
  : writable.value)

/** 当前文件语言对应的服务器（给编辑器显示 LSP 状态与"该装什么"）。 */
const lspForFile = computed(() => {
  if (!file.value?.lang) return null
  const hit = lspServers.value.find((s) => (s.languages || []).includes(file.value.lang))
  if (!hit) return { available: false, name: '', hint: `没有为 ${file.value.lang} 配置语言服务器` }
  const installed = hit.installed && hit.enabled
  return {
    available: installed,
    name: hit.name,
    hint: installed ? '' : `${hit.name} 未安装${hit.install_hint ? `：${hit.install_hint}` : ''}`,
  }
})

const diagCounts = computed(() => diagnostics.value?.counts || { error: 0, warning: 0 })

const context = computed(() => {
  if (!file.value || !contextEnabled.value) return null
  return { path: file.value.path, start_line: editorSelection.value?.start_line || file.value.start_line || 1, end_line: editorSelection.value?.end_line || file.value.end_line || 1 }
})

/** 发给后端的"结构坐标"：前端只给 id 与行区间，事实由服务端从索引解析。 */
const codeContext = computed(() => {
  if (!file.value || !contextEnabled.value) return null
  const ctx = {
    path: file.value.path,
    start: editorSelection.value?.start_line || file.value.start_line || 1,
    end: editorSelection.value?.end_line || file.value.end_line || file.value.start_line || 1,
  }
  if (selectedSymbol.value) {
    ctx.symbol_id = selectedSymbol.value.id
    ctx.start = selectedSymbol.value.line
    ctx.end = selectedSymbol.value.end_line || selectedSymbol.value.line
  }
  return ctx
})

// ---- 三栏宽度（可拖，记忆到 localStorage） ----
const LEFT_KEY = 'code-agent-code-left'
const RIGHT_KEY = 'code-agent-code-right'
const leftWidth = ref(Number(localStorage.getItem(LEFT_KEY)) || 264)
const rightWidth = ref(Number(localStorage.getItem(RIGHT_KEY)) || 380)

function startDrag(side, ev) {
  dragCleanup?.()
  ev.preventDefault()
  const startX = ev.clientX
  const startLeft = leftWidth.value
  const startRight = rightWidth.value
  const onMove = (e) => {
    const dx = e.clientX - startX
    if (side === 'left') {
      leftWidth.value = Math.min(460, Math.max(180, startLeft + dx))
    } else {
      rightWidth.value = Math.min(680, Math.max(260, startRight - dx))
    }
  }
  const onUp = () => {
    dragCleanup?.()
    localStorage.setItem(LEFT_KEY, String(leftWidth.value))
    localStorage.setItem(RIGHT_KEY, String(rightWidth.value))
  }
  dragCleanup = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    dragCleanup = null
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

const gridStyle = computed(() => ({
  gridTemplateColumns: `${leftShown.value ? leftWidth.value : 0}px minmax(0, 1fr) ${rightShown.value ? rightWidth.value : 0}px`,
}))

function captureEditor() {
  const snapshot = editorRef.value?.snapshot?.()
  const doc = snapshot?.key && documents.value.find(item => item.key === snapshot.key)
  // 子编辑器可能还挂着上一个标签的模型；按模型自身的 key 捕获。
  // 未加载的干净恢复标签是占位符，不能把空文本当成新草稿。
  if (doc && !(doc.needsReload && !doc.dirty)) workbench.update(doc.key, snapshot)
}

function releaseClosedDocument(doc) {
  const pane = editorRef.value
  nextTick(() => pane?.releaseDocument?.(doc))
}

function syncActiveDocument() {
  const doc = workbench.activeDocument.value
  const previous = file.value?.key
  file.value = doc ? { ...doc.file, path: doc.path, key: doc.key, text: doc.text, sha1: doc.sha1, savedText: doc.savedText, dirty: doc.dirty, needsReload: doc.needsReload, conflict: doc.conflict } : null
  if (previous !== file.value?.key) {
    selectedSymbol.value = null
    editorSelection.value = doc?.selection || null
    contextEnabled.value = true
  }
}

function activateDocument(key) {
  captureEditor()
  const previous = currentLocation()
  fileRequest += 1
  fileBusy.value = false
  if (!workbench.activate(key)) return
  fileLine.value = 0
  syncActiveDocument()
  navigation.visit(currentLocation(), previous)
  centerView.value = 'file'
  mobilePanel.value = 'center'
}

function onEditorChange({ key, text }) {
  workbench.update(key, { text })
  if (file.value?.key === key) { file.value.text = text; file.value.dirty = workbench.activeDocument.value?.dirty || false }
}

async function onLanguageEdit(payload) {
  if (!project.value || manualReadonly.value || !project.value.available || (payload?.projectId && payload.projectId !== project.value.id) || (payload?.tree && payload.tree !== 'main')) return false
  const pid = project.value.id, version = projectVersion, request = ++languageEditRequest
  captureEditor()
  languageEditBusy.value = true
  banner.value = ''
  try {
    const rows = await prepareWorkspaceEdit({ edit: payload.edit, rootPath: project.value.root_path, documents: documents.value, versions: payload.versions, snapshots: payload.snapshots, requireVersions: payload.source === 'server',
      readFile: path => api.code.file(pid, path) })
    if (!isCurrentProject(pid, version) || request !== languageEditRequest) return false
    captureEditor()
    assertPlanCurrent(rows, documents.value)
    let batch = null
    if (rows.resourceOperations) {
      batch = await changeReviews.createBatch(pid, { title: payload.title || '项目重构', files: rows.transactionFiles })
      if (!isCurrentProject(pid, version) || request !== languageEditRequest) return false
      captureEditor(); assertPlanCurrent(rows, documents.value)
      patchRefresh.value += 1
    }
    languageEditPreview.value = { title: payload.title || '代码修改', rows, batch, projectId: pid, tree: 'main', version }
    languageEditSelected.value = 0
    return true
  } catch (e) { if (isCurrentProject(pid, version) && request === languageEditRequest) banner.value = e.message; return false }
  finally { if (isCurrentProject(pid, version) && request === languageEditRequest) languageEditBusy.value = false }
}

async function applyLanguageEdit() {
  const preview = languageEditPreview.value
  if (!preview || languageEditBusy.value || manualReadonly.value || !project.value?.available || !isCurrentProject(preview.projectId, preview.version)) return false
  captureEditor()
  languageEditBusy.value = true
  try {
    assertPlanCurrent(preview.rows, documents.value)
    if (preview.batch) {
      const result = await changeReviews.decideBatch(preview.projectId, preview.batch.id, 'apply')
      if (isCurrentProject(preview.projectId, preview.version)) {
        languageEditUndo.value = preview; languageEditPreview.value = null
        await onPatchesChanged({ ...result, projectId: preview.projectId })
        notice.value = result.warning || `已应用项目重构，共 ${result.paths?.length || preview.rows.transactionFiles.length} 个路径`
      }
      return true
    }
    for (const row of preview.rows) {
      let document = findPlanDocument(row, documents.value)
      if (!document) document = workbench.openFile(row.file, { projectId: preview.projectId, tree: preview.tree, activate: false })
      row.key = document.key
      workbench.update(document.key, { text: row.after })
    }
    languageEditUndo.value = preview
    languageEditPreview.value = null
    syncActiveDocument()
    notice.value = `已应用到 ${preview.rows.length} 个文件的草稿，请检查并保存`
    return true
  } catch (e) { if (isCurrentProject(preview.projectId, preview.version)) banner.value = e.message; return false }
  finally { if (isCurrentProject(preview.projectId, preview.version)) languageEditBusy.value = false }
}

async function undoLanguageEdit() {
  const batch = languageEditUndo.value
  if (!batch || languageEditBusy.value || manualReadonly.value || !project.value?.available || !isCurrentProject(batch.projectId, batch.version)) return false
  captureEditor()
  languageEditBusy.value = true
  try {
    assertPlanCurrent(batch.rows, documents.value, 'undo')
    if (batch.batch) {
      const result = await changeReviews.decideBatch(batch.projectId, batch.batch.id, 'rollback')
      if (isCurrentProject(batch.projectId, batch.version)) {
        languageEditUndo.value = null
        await onPatchesChanged({ ...result, projectId: batch.projectId })
        notice.value = result.warning || '已回滚项目重构'
      }
      return true
    }
    for (const row of batch.rows) workbench.update(row.key, { text: row.before })
    languageEditUndo.value = null
    syncActiveDocument()
    notice.value = '已撤销整批代码修改，草稿已恢复'
    return true
  } catch (e) { if (isCurrentProject(batch.projectId, batch.version)) banner.value = e.message; return false }
  finally { if (isCurrentProject(batch.projectId, batch.version)) languageEditBusy.value = false }
}

function onLanguageSymbol(symbol) {
  if (symbol?.path && (!symbol.projectId || symbol.projectId === project.value?.id) && (!symbol.tree || symbol.tree === 'main')) openFile({ path: symbol.path, line: symbol.line || 1, column: symbol.column || 1 })
}

function onEditorViewState({ key, state }) { workbench.update(key, { viewState: state }) }
function onEditorSelection(payload) {
  workbench.update(payload.key, { selection: payload })
  if (file.value?.key === payload.key) editorSelection.value = payload
}

function closeDocument(key) {
  captureEditor()
  const doc = documents.value.find(item => item.key === key)
  if (!doc) return
  if (doc.dirty) { closingDocument.value = doc; return }
  workbench.close(key)
  fileRequest += 1
  fileLine.value = 0
  syncActiveDocument()
  releaseClosedDocument(doc)
}

async function finishClose(action) {
  const doc = closingDocument.value
  if (!doc) return
  if (action === 'cancel') { closingDocument.value = null; return }
  if (action === 'save') {
    const ok = await saveFile({ key: doc.key, path: doc.path, text: doc.text, sha1: doc.sha1 })
    if (!ok || closingDocument.value !== doc || project.value?.id !== doc.projectId) return
    captureEditor()
    if (doc.dirty) return
  }
  workbench.close(doc.key, { discard: action === 'discard' })
  closingDocument.value = null
  fileRequest += 1
  fileLine.value = 0
  syncActiveDocument()
  releaseClosedDocument(doc)
}

function readProjectFile(pid, path, version = projectVersion) {
  const key = `${version}:${documentKey(pid, 'main', path)}`
  if (pendingFileReads.has(key)) return pendingFileReads.get(key)
  const read = Promise.resolve().then(() => api.code.file(pid, path))
  pendingFileReads.set(key, read)
  const release = () => { if (pendingFileReads.get(key) === read) pendingFileReads.delete(key) }
  read.then(release, release)
  return read
}

async function restoreDocument(doc, pid, version) {
  if (!isCurrentProject(pid, version) || !doc.needsReload || !documents.value.includes(doc)) return
  try {
    const fresh = await readProjectFile(pid, doc.path, version)
    if (!isCurrentProject(pid, version) || !documents.value.includes(doc)) return
    // A save or explicit reopen may have already replaced this recovery baseline.
    if (doc.needsReload) workbench.updateExternal(doc.key, fresh)
  } catch (e) {
    if (!isCurrentProject(pid, version) || !documents.value.includes(doc) || !doc.needsReload) return
    doc.file = { ...doc.file, path: doc.path, missing: true }
    doc.needsReload = false
    banner.value = `${doc.path}：${e.message}。恢复的草稿仍保留。`
  }
  if (isCurrentProject(pid, version) && file.value?.key === doc.key) syncActiveDocument()
}

function restoreDocuments(pid, version) {
  const key = `${version}:${pid}`
  if (!isCurrentProject(pid, version) || props.visible === false) return Promise.resolve()
  if (pendingDocumentRestores.has(key)) return pendingDocumentRestores.get(key)
  const restore = (async () => {
    const restoring = documents.value.filter(doc => doc.needsReload)
    const active = restoring.find(doc => doc.key === activeDocumentKey.value)
    // Make the visible editor usable before competing with background tabs for disk reads.
    if (active) await restoreDocument(active, pid, version)
    if (!isCurrentProject(pid, version) || props.visible === false) return
    const pending = restoring.filter(doc => doc !== active)
    let index = 0
    const consume = async () => {
      while (isCurrentProject(pid, version) && props.visible !== false && index < pending.length) await restoreDocument(pending[index++], pid, version)
    }
    await Promise.all(Array.from({ length: Math.min(2, pending.length) }, consume))
  })()
  pendingDocumentRestores.set(key, restore)
  const release = () => { if (pendingDocumentRestores.get(key) === restore) pendingDocumentRestores.delete(key) }
  restore.then(release, release)
  return restore
}

function openWorkspaceSearch(mode = 'files') {
  if (!project.value) return
  quickMode.value = mode
  quickOpen.value = true
}
function currentLocation() {
  const doc = workbench.activeDocument.value
  return doc ? { path: doc.path, line: doc.selection?.start_line || 1, column: doc.selection?.start_column || 1 } : null
}
async function navigateHistory(direction) {
  if (navigationBusy.value) return
  captureEditor()
  const ticket = navigation.prepare(direction, currentLocation())
  if (!ticket) return
  navigationBusy.value = true
  try { if (await openFile(ticket.point, { recordNavigation: false })) navigation.commit(ticket) }
  finally { navigationBusy.value = false }
}
function showBottom(tab) {
  centerView.value = 'file'
  bottomTab.value = tab
  problemsVisible.value = true
  mobilePanel.value = 'center'
  if (['terminal', 'run', 'output', 'tests'].includes(tab)) runtimeActivated.value = true
  if (tab === 'debug') debugActivated.value = true
}
function onBreakpoint(point) {
  const index = breakpoints.value.findIndex(b => b.path === point.path && b.line === point.line)
  breakpoints.value = index < 0 ? [...breakpoints.value, point] : breakpoints.value.filter((_, i) => i !== index)
}
async function prepareExecution(path = file.value?.path) {
  if (!project.value?.available || !path || executionBusy.value) return null
  captureEditor()
  const pid = project.value.id, version = projectVersion
  const doc = documents.value.find(d => d.path === path)
  if (doc?.needsReload || doc?.file?.missing) { banner.value = '文件尚未读取或已不存在，请重新打开后运行'; return null }
  if (doc?.dirty) {
    if (!await saveFile({ key: doc.key, path: doc.path, text: doc.text, sha1: doc.sha1 })) return null
    captureEditor()
    if (doc.dirty) { banner.value = '保存期间内容又有变化，请保存最新草稿后运行'; return null }
  }
  return isCurrentProject(pid, version) ? { pid, version, path } : null
}
async function runCurrent() {
  const context = await prepareExecution()
  if (!context) return
  executionBusy.value = true
  try { showBottom('run'); await nextTick(); if (isCurrentProject(context.pid, context.version) && file.value?.path === context.path) await runtimeRef.value?.runCurrent?.() }
  finally { if (isCurrentProject(context.pid, context.version)) executionBusy.value = false }
}
async function runDebug(options = {}) {
  const targetPath = options.path || file.value?.path
  const attaching = options.java?.request === 'attach'
  const context = attaching && project.value?.available && targetPath && !executionBusy.value
    ? { pid: project.value.id, version: projectVersion, path: targetPath }
    : await prepareExecution(targetPath)
  if (!context) return
  executionBusy.value = true
  try {
    showBottom('debug'); await nextTick()
    captureEditor()
    const target = context.path
    if (!attaching && documents.value.some(doc => doc.path === target && doc.dirty)) { banner.value = '文件又有未保存的变化，请保存后调试'; return }
    if (isCurrentProject(context.pid, context.version)) await debugRef.value?.start?.({ ...options, path: target })
  }
  finally { if (isCurrentProject(context.pid, context.version)) executionBusy.value = false }
}
function onWorkbenchKey(ev) {
  if (props.visible === false) return
  if (ev.defaultPrevented) return
  if (pickerOpen.value) {
    if (ev.key === 'Escape') { ev.preventDefault(); dismissProjectForm() }
    if (ev.key === 'Tab') {
      const modal = document.querySelector?.('[aria-label="打开项目"]')
      const controls = modal?.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled)') || []
      const first = controls[0], last = controls[controls.length - 1]
      if (first && ev.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) { ev.preventDefault(); last.focus() }
      else if (first && !ev.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) { ev.preventDefault(); first.focus() }
    }
    return
  }
  if (closingDocument.value || conflictDialog.value || languageEditPreview.value) {
    if (ev.key === 'Escape') {
      ev.preventDefault()
      if (languageEditPreview.value) { if (!languageEditBusy.value) languageEditPreview.value = null }
      else if (conflictDialog.value) resolveConflict('keep')
      else finishClose('cancel')
    }
    if (ev.key === 'Tab') {
      const modal = document.querySelector?.(languageEditPreview.value ? '[aria-label="代码修改预览"]' : conflictDialog.value ? '[aria-label="文件保存冲突"]' : '[aria-label="保存未完成的更改"]')
      const controls = modal?.querySelectorAll('button:not(:disabled), textarea, input, select') || []
      const first = controls[0], last = controls[controls.length - 1]
      if (first && ev.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) { ev.preventDefault(); last.focus() }
      else if (first && !ev.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) { ev.preventDefault(); first.focus() }
    }
    return
  }
  if (quickOpen.value) {
    if ((ev.ctrlKey || ev.metaKey) && !ev.altKey) {
      if (ev.key.toLowerCase() === 'p' && !ev.shiftKey) { ev.preventDefault(); openWorkspaceSearch('files') }
      if (ev.key.toLowerCase() === 'f' && ev.shiftKey) { ev.preventDefault(); openWorkspaceSearch('content') }
    }
    return
  }
  if (centerView.value === 'file' && ev.altKey && !ev.ctrlKey && !ev.metaKey && ['ArrowLeft', 'ArrowRight'].includes(ev.key)) { ev.preventDefault(); navigateHistory(ev.key === 'ArrowLeft' ? -1 : 1); return }
  if (ev.key === 'F5' && centerView.value === 'file') { ev.preventDefault(); if (ev.ctrlKey || ev.metaKey) runCurrent(); else if (debugRef.value?.isPaused?.()) { showBottom('debug'); debugRef.value?.continue?.() } else runDebug(); return }
  if (ev.key === 'F9' && centerView.value === 'file') { ev.preventDefault(); editorRef.value?.toggleBreakpoint?.(); return }
  if (!(ev.ctrlKey || ev.metaKey) || ev.altKey) return
  if (ev.key.toLowerCase() === 'o' && ev.shiftKey) { ev.preventDefault(); openProjectFolder(); return }
  if (ev.key.toLowerCase() === 'p' && !ev.shiftKey) { ev.preventDefault(); openWorkspaceSearch('files') }
  if (ev.key.toLowerCase() === 'f' && ev.shiftKey) { ev.preventDefault(); openWorkspaceSearch('content') }
  if (ev.key.toLowerCase() === 'b' && !ev.shiftKey) { ev.preventDefault(); toggleFileSidebar() }
}
function beforeLeave(ev) {
  captureEditor(); workbench.flush()
  if (workbench.hasUnsaved.value) { ev.preventDefault(); ev.returnValue = '' }
}

// ---- 数据流 ----
function loadProjects() { return projectAccess.init() }

function selectProject(p) { return projectAccess.activate(p) }

async function activateWorkspace(p) {
  if (disposed || !p?.id) return false
  pickerOpen.value = false
  projectLocationSelection.value = null
  if (project.value?.id === p.id) return true
  clearProjectState()
  project.value = p
  navigation.activate(p.id)
  workbench.activateProject(p.id, 'main')
  syncActiveDocument()
  patchRefresh.value += 1
  if (!p.available) {
    banner.value = `项目目录不可用：${p.root_path}`
    return
  }
  banner.value = ''
  // 访问控制器已核对项目详情或取得登记响应，再交给工作区装配会话和文档。
  const pid = p.id
  const version = projectVersion
  // Session preparation, language probing and recovery must not keep the project
  // picker busy. Each operation already checks the project generation before applying.
  void openSession(p, version)
  void restoreDocuments(pid, version)
  scheduleProjectIntel(pid, version)
  return isCurrentProject(pid, version)
}

async function openSession(p, version = projectVersion) {
  const request = ++sessionRequest
  sessionBusy.value = true
  try {
    const result = await api.code.projectSession(p.id)
    if (isCurrentProject(p.id, version) && request === sessionRequest) session.value = result
  } catch (e) {
    if (!isCurrentProject(p.id, version) || request !== sessionRequest) return
    banner.value = e.message
    session.value = null
  } finally {
    if (isCurrentProject(p.id, version) && request === sessionRequest) sessionBusy.value = false
  }
}

function scheduleProjectIntel(pid, version = projectVersion) {
  const key = `${version}:${pid}`
  if (!isCurrentProject(pid, version) || props.visible === false || startedProjectIntel.has(key)) return
  startedProjectIntel.add(key)
  void nextTick(() => {
    if (!isCurrentProject(pid, version)) return
    if (props.visible === false) { startedProjectIntel.delete(key); return }
    return loadProjectIntel(pid, version)
  })
}

/** Probe languages and diagnostics independently; a slow executable probe cannot hold up counts. */
async function loadProjectIntel(pid, version = projectVersion) {
  void functionDescriptions.refresh(pid)
  await Promise.all([refreshDiagnostics(pid, version), (async () => {
    try {
      const body = await api.code.projectLsp(pid)
      if (isCurrentProject(pid, version)) lspServers.value = body.servers || []
    } catch { if (isCurrentProject(pid, version)) lspServers.value = [] }
  })()])
}

async function refreshDiagnostics(pid = project.value?.id, version = projectVersion) {
  if (!pid || !isCurrentProject(pid, version)) return
  const key = `${version}:${pid}`
  if (diagnosticsPending === key) return
  diagnosticsPending = key
  try {
    const result = await api.code.diagnostics(pid)
    if (isCurrentProject(pid, version)) diagnostics.value = result
  } catch {
    if (isCurrentProject(pid, version)) diagnostics.value = null
  } finally {
    if (diagnosticsPending === key) diagnosticsPending = ''
  }
}

/** 诊断轮询：语言服务器的诊断是服务端异步写进索引的，前端只取很轻的计数。 */
function startDiagnosticsPolling() {
  stopDiagnosticsPolling()
  if (props.visible === false) return
  diagTimer = setInterval(() => {
    if (props.visible !== false && document.visibilityState === 'visible') {
      refreshDiagnostics()
      if (project.value?.available) void functionDescriptions.refresh(project.value.id)
    }
  }, 5000)
}

function stopDiagnosticsPolling() {
  if (diagTimer) clearInterval(diagTimer)
  diagTimer = null
}

async function openFile(f, { recordNavigation = true } = {}) {
  if (!project.value) return false
  captureEditor()
  const previous = currentLocation()
  const pid = project.value.id
  const version = projectVersion
  const request = ++fileRequest
  const key = documentKey(pid, 'main', f.path)
  const existing = documents.value.find(doc => doc.key === key)
  if (existing && !existing.needsReload) {
    workbench.activate(key)
    syncActiveDocument()
    fileLine.value = f.line || 0
    fileColumn.value = f.column || 1
    if (f.line) fileJump.value += 1
    centerView.value = 'file'
    mobilePanel.value = 'center'
    fileBusy.value = false
    if (recordNavigation) navigation.visit({ path: f.path, line: f.line || currentLocation()?.line || 1, column: f.column || currentLocation()?.column || 1 }, previous)
    return true
  }
  fileBusy.value = true
  banner.value = ''
  try {
    const result = await readProjectFile(pid, f.path, version)
    if (!isCurrentProject(pid, version) || request !== fileRequest) return
    if (file.value?.path !== result.path) selectedSymbol.value = null
    workbench.openFile(result, { projectId: pid, tree: 'main' })
    syncActiveDocument()
    fileLine.value = f.line || 1
    fileColumn.value = f.column || 1
    fileJump.value += 1          // 显式跳转：编辑器据此把视口移到这一行
    centerView.value = 'file'
    mobilePanel.value = 'center'
    if (recordNavigation) navigation.visit({ path: result.path, line: f.line || 1, column: f.column || 1 }, previous)
    return true
  } catch (e) {
    if (!isCurrentProject(pid, version) || request !== fileRequest) return
    banner.value = `${f.path}：${e.message}`
    return false
  } finally {
    if (isCurrentProject(pid, version) && request === fileRequest) fileBusy.value = false
  }
}

/** 地图或工作流「交给 AI」：打开助手并准备输入，用户编辑后发送。 */
function onAsk(text, intent = null) {
  pendingAgentNodeId.value = intent?.node_id || ''
  centerView.value = 'map'
  rightVisible.value = true
  rightTab.value = 'ai'
  mobilePanel.value = 'assistant'
  askPrompt.value = text
  notice.value = '已放入当前主对话输入框，编辑后发送；需要时由主智能体调用子任务'
}

function openFrontendDesign() {
  if (!project.value || project.value.available === false) return
  if (centerView.value === 'design') {
    mobilePanel.value = 'center'
    return
  }
  captureEditor()
  centerView.value = 'design'
  designActivated.value = true
  designAssistantVisible.value = false
  mobilePanel.value = 'center'
}
function toggleAssistantPanel() {
  if (centerView.value === 'design') {
    designAssistantVisible.value = !rightShown.value
    if (designAssistantVisible.value) rightVisible.value = true
  } else rightVisible.value = !rightVisible.value
}
function showProjectPanel() {
  if (centerView.value === 'design') centerView.value = 'file'
  leftVisible.value = true
  mobilePanel.value = 'projects'
}
function toggleFileSidebar() {
  if (centerView.value === 'design') { showProjectPanel(); return }
  leftVisible.value = !leftVisible.value
}
function showAssistantPanel() {
  rightVisible.value = true
  if (centerView.value === 'design') designAssistantVisible.value = true
  mobilePanel.value = 'assistant'
}
function onDesignAsk(text) {
  if (!project.value || typeof text !== 'string' || !text.trim()) return
  pendingAgentNodeId.value = ''
  pendingWorkItemSession.value = null
  designAiResult.value = null
  centerView.value = 'design'
  designAssistantVisible.value = true
  rightVisible.value = true
  rightTab.value = 'ai'
  mobilePanel.value = 'assistant'
  askPrompt.value = text
  notice.value = '设计要求已放入 AI 输入框；发送后可在设计画布审核和应用样式'
}
function onDesignResult(reply) {
  if (!project.value || !designActivated.value || reply?.sessionId !== aiSessionId.value || typeof reply.text !== 'string') return
  designAiResult.value = { projectId: project.value.id, text: reply.text, seq: reply.seq }
}
function onDesignSaved(result) {
  treeRef.value?.refresh?.()
  void onPatchesChanged({ paths: [result.path, result.entry_path].filter(Boolean) })
  notice.value = '前端样式已保存到项目'
}

function selectAgent(id) {
  // Legacy workflow links reveal background progress without changing drafts.
  rightVisible.value = true; rightTab.value = 'ai'; mobilePanel.value = 'assistant'
  void agentTeam.refresh()
}
function returnLead() { conversations.returnLead(); pendingWorkItemSession.value = null; pendingAgentNodeId.value = ''; askPrompt.value = '' }
function selectConversation(id) {
  if (!conversations.select(id)) return
  pendingWorkItemSession.value = null; pendingAgentNodeId.value = ''; askPrompt.value = ''
}
async function createConversation() {
  if (activeTree.value !== 'main') return
  const result = await conversations.create()
  if (!result || conversations.activeRootId.value !== result.id) return
  pendingWorkItemSession.value = null; pendingAgentNodeId.value = ''; askPrompt.value = ''
}
async function deleteConversation(id) {
  const item = conversations.conversations.value.find(item => item.id === id)
  if (!item || conversations.deleting.value || conversations.creating.value) return
  const pid = project.value?.id, version = projectVersion
  await dialog.open({
    title: `删除「${item.title || '项目对话'}」？`, tone: 'danger', icon: 'trash',
    description: `${item.id === conversations.canonicalId.value ? '删除默认对话后，会将其他对话设为默认；没有其他对话时会创建一条空白对话。' : ''}只移除这条对话并结束它及后台子任务。项目文件、未保存的编辑器草稿和其他对话不受影响；服务端保留归档历史。`,
    confirmText: '删除对话', pendingText: '正在删除…',
    onConfirm: async () => {
      if (!isCurrentProject(pid, version) || activeTree.value !== 'main') throw new Error('当前项目已变化，请重新操作。')
      if (!await conversations.remove(id)) throw new Error(conversations.error.value || '删除对话失败')
    },
  })
}
async function openWorkItemAgent({ sessionId, tree = 'main' }) {
  if (!project.value || !sessionId) return
  const pid = project.value.id, version = projectVersion
  pendingWorkItemSession.value = { pid, version, sessionId, tree }
  // Session selection follows its workspace; an isolated worker's approval
  // must never be shown against the main-tree Lead.
  taskRequest += 1
  activeTree.value = tree
  activeTaskId.value = tree.startsWith('task:') ? tree.slice(5) : ''
  taskDiff.value = null
  pendingAgentNodeId.value = ''; askPrompt.value = ''
  rightVisible.value = true; rightTab.value = 'ai'; mobilePanel.value = 'assistant'
  await nextTick()
  await agentTeam.refresh()
  if (!isCurrentProject(pid, version) || activeTree.value !== tree) return
  pendingWorkItemSession.value = null
  mapRef.value?.loadMap?.({ keepExpand: true })
}
watch(agentTeam.team, () => {
  const target = pendingWorkItemSession.value
  if (!target || !isCurrentProject(target.pid, target.version) || activeTree.value !== target.tree) return
  pendingWorkItemSession.value = null
})
function onTeamRefresh(change) {
  if (change) agentTeam.notify(change)
  else void agentTeam.refresh()
  taskPanel.value?.notify?.(change || {})
}
async function onWorkflowAgent(agent) {
  if (agent?.session_id) selectAgent(agent.session_id)
  else {
    const pid = project.value?.id, tree = activeTree.value
    await agentTeam.refresh()
    if (disposed || project.value?.id !== pid || activeTree.value !== tree) return
    const assigned = agentTeam.assignments.value.get(agent?.node_id)
    if (assigned) selectAgent(assigned.session_id)
  }
}
function refreshAgentTeam(payload) {
  if (payload?.project_id && (payload.project_id !== project.value?.id || payload.tree_id !== activeTree.value)) return
  void agentTeam.refresh()
}

/** 导图里选中的符号：用于把"当前坐标"随消息发给 AI */
function onSelectNode(node) {
  selectedSymbol.value = node && String(node.id || '').startsWith('sym:') ? node : null
}

/** 切换"AI 能否直接改主干"。项目开关默认关闭，第一次开启要显式确认。 */
async function toggleWritable() {
  if (conversations.isChat.value) return toggleConversationWritable()
  if (!project.value || sessionBusy.value) return
  const pid = project.value.id
  const version = projectVersion
  const request = ++sessionRequest
  sessionBusy.value = true
  try {
    if (writable.value) {
      const result = await api.code.projectSession(pid, { permission_mode: 'read-only' })
      if (!isCurrentProject(pid, version) || request !== sessionRequest) return
      session.value = result
      notice.value = '执行智能体已切回只读，Lead 的读取能力与人工编辑权限保持独立'
      return
    }
    const enableWrite = async () => {
      if (!isCurrentProject(pid, version) || request !== sessionRequest) throw new Error('当前项目已变化，请关闭弹窗后重新操作。')
      if (!project.value.allow_direct_write) {
        const updated = await api.code.updateSettings(pid, { allow_direct_write: true })
        if (!isCurrentProject(pid, version) || request !== sessionRequest) return
        project.value = updated
      }
      const result = await api.code.projectSession(pid, { permission_mode: 'workspace-write' })
      if (!isCurrentProject(pid, version) || request !== sessionRequest) return result
      session.value = result
      notice.value = '已允许 AI 直接改主干：每次改动都会记进台账，可一键回滚'
      return result
    }
    if (!project.value.allow_direct_write) {
      await dialog.open({
        title: '允许 AI 直接修改项目？', tone: 'warning', icon: 'shield',
        description: '执行智能体将能够直接写入当前项目目录。每次写入都会记录到改动台账，可在台账中查看与回滚。Lead 负责读取核查和分配任务；人工编辑权限独立控制。',
        details: `项目：${project.value.name}\n${project.value.root_path}`,
        confirmText: '允许 AI 修改', pendingText: '正在更新权限…', onConfirm: enableWrite,
      })
    } else await enableWrite()
  } catch (e) {
    if (!isCurrentProject(pid, version) || request !== sessionRequest) return
    // 兜底：开关状态可能是过期的（另一个标签页改过），此时服务端会拒绝
    const message = e.message || ''
    if (message.includes('允许直接改主干')) {
      banner.value = '项目开关状态已变化：请刷新页面后重试'
    } else {
      banner.value = message
    }
  } finally {
    if (isCurrentProject(pid, version) && request === sessionRequest) sessionBusy.value = false
  }
}

async function toggleConversationWritable() {
  if (!project.value || conversationPermissionBusy.value || !conversations.isChat.value) return
  const pid = project.value.id, version = projectVersion, id = conversations.activeRootId.value
  const mode = executionWritable.value ? 'read-only' : 'workspace-write'
  conversationPermissionBusy.value = id
  const isTarget = () => isCurrentProject(pid, version) && conversations.isChat.value && conversations.activeRootId.value === id
  const apply = async () => {
    if (!isTarget()) throw new Error('当前对话已变化，请重新操作。')
    if (mode !== 'read-only' && !project.value.allow_direct_write) {
      const updated = await api.code.updateSettings(pid, { allow_direct_write: true })
      if (!isTarget()) return
      project.value = updated
    }
    const result = await api.setSessionPermission(id, mode)
    if (!isCurrentProject(pid, version)) return
    conversations.update(id, { ...result, permission_mode: result?.permission_mode || mode, effective_permission: result?.effective_permission || result?.permission_mode || mode })
    if (isTarget()) notice.value = mode === 'read-only' ? '当前项目对话已切回只读' : '当前项目对话已允许直接修改；操作与审批仍会记录'
    void conversations.refresh()
    return result
  }
  try {
    if (mode !== 'read-only' && !project.value.allow_direct_write) await dialog.open({
      title: '允许当前对话修改项目？', tone: 'warning', icon: 'shield',
      description: '仅更新当前项目对话的执行权限，操作与审批会保留记录。其他对话保持各自的会话权限。',
      details: `项目：${project.value.name}\n对话：${conversations.selected.value?.title || '项目对话'}`,
      confirmText: '允许当前对话修改', pendingText: '正在更新权限…', onConfirm: apply,
    })
    else await apply()
  } catch (cause) { if (isTarget()) banner.value = cause.message || '更新对话权限失败' }
  finally { if (conversationPermissionBusy.value === id) conversationPermissionBusy.value = '' }
}

async function saveFile({ key, path, text, sha1 }) {
  if (!project.value || saving.value || manualReadonly.value) return false
  if (key && !documents.value.some(doc => doc.key === key && doc.path === path)) return false
  const pid = project.value.id
  const version = projectVersion
  const docKey = key || file.value?.key
  const sessionId = session.value?.id || ''
  const isCurrentFile = () => isCurrentProject(pid, version) && (docKey ? file.value?.key === docKey : file.value?.path === path)
  saving.value = true
  banner.value = ''
  notice.value = ''
  try {
    let result
    try {
      result = await api.code.saveFile(pid, path, text, sha1, sessionId)
    } catch (e) {
      if (e.status !== 409 || !isCurrentProject(pid, version)) throw e
      const fresh = await api.code.file(pid, path)
      if (!isCurrentProject(pid, version)) return false
      if (docKey) workbench.updateExternal(docKey, fresh)
      if (isCurrentFile() && docKey) syncActiveDocument()
      conflictDialog.value = { key: docKey, path, text, fresh }
      notice.value = '磁盘内容已变化，你的草稿已保留，请对比后选择'
      return false
    }
    if (docKey) workbench.markSaved(docKey, { text, sha1: result.sha1 || sha1, total_lines: result.lines })
    if (!isCurrentProject(pid, version)) return true
    indexProgress.accept(pid, result)
    if (result.patch_error) banner.value = `文件已保存，但改动台账记录失败：${result.patch_error}`
    editorRef.value?.notifySaved?.({ key: docKey, path, uri: documents.value.find(doc => doc.key === docKey)?.file?.uri, text })
    if (result.changed !== false) patchRefresh.value += 1
    if (!isCurrentFile()) return true
    if (docKey) syncActiveDocument()
    if (result.changed === false) {
      if (!docKey) editorRef.value?.markSaved(text)
      notice.value = '内容无变化，未写入'
    } else {
      if (!docKey) file.value = { ...file.value, text, sha1: result.sha1, total_lines: result.lines }
      if (!docKey) editorRef.value?.markSaved(text)
      notice.value = `已保存 ${path}`
    }
    return true
  } catch (e) {
    if (isCurrentProject(pid, version)) banner.value = e.message
    return false
  } finally {
    if (isCurrentProject(pid, version)) saving.value = false
  }
}

async function resolveConflict(action) {
  const conflict = conflictDialog.value
  if (!conflict || saving.value) return
  if (action === 'keep') { conflictDialog.value = null; notice.value = '已保留草稿，尚未写入磁盘'; return }
  if (action === 'disk') {
    workbench.update(conflict.key, { text: conflict.fresh.text })
    workbench.markSaved(conflict.key, { text: conflict.fresh.text, sha1: conflict.fresh.sha1 })
    workbench.updateExternal(conflict.key, conflict.fresh)
    syncActiveDocument()
    conflictDialog.value = null
    notice.value = '已使用磁盘版本'
    return
  }
  const doc = documents.value.find(item => item.key === conflict.key)
  const ok = await saveFile({ key: conflict.key, path: conflict.path, text: doc?.text ?? conflict.text, sha1: conflict.fresh.sha1 })
  if (ok && conflictDialog.value === conflict) conflictDialog.value = null
}

function promptOpenProject(mode = 'open') {
  if (projectBusy.value) return
  projectAccess.deferRestore()
  projectAccess.clearError()
  projectLocationSelection.value = null
  pickerMode.value = ['create', 'path'].includes(mode) ? mode : 'open'
  pickerOpen.value = !!project.value
  mobilePanel.value = 'center'
  if (pickerOpen.value) nextTick(() => document.querySelector?.('[aria-label="打开项目"] button:not(:disabled)')?.focus())
}

function dismissProjectForm() {
  if (projectBusy.value) return
  pickerOpen.value = false
  pickerMode.value = 'open'
  projectLocationSelection.value = null
  projectAccess.clearError()
  nextTick(() => document.querySelector?.('.project-switcher-trigger')?.focus())
}
function openProjectFolder(start = '') {
  if (projectBusy.value || loading.value) return false
  if (!projectAccess.nativeAvailable.value) { promptOpenProject('path'); return false }
  return projectAccess.openFolder(typeof start === 'string' ? start : '')
}
async function pickProjectLocation(start = '') {
  if (projectBusy.value || loading.value) return false
  const location = await projectAccess.pickLocation(typeof start === 'string' ? start : '')
  if (!location) return false
  // A fresh object also applies repeated selections after the user edits the path field.
  projectLocationSelection.value = { path: location }
  return true
}

/** 回滚之后：地图已由服务端重建，这里刷新台账并提示 */
async function onPatchesChanged(result) {
  if (result?.projectId && result.projectId !== project.value?.id) return
  if (!project.value) return
  const pid = project.value.id, version = projectVersion
  patchRefresh.value += 1
  if (result?.index_refresh) indexProgress.accept(pid, result)
  mapRef.value?.loadMap?.({ keepExpand: true })
  if (result?.batch) treeRef.value?.refresh?.()
  const paths = [...new Set(result?.paths || [result?.path].filter(Boolean))]
  await Promise.all(paths.map(async path => {
    const compare = value => workspaceCaseInsensitive.value ? value.toLowerCase() : value
    const changedDoc = documents.value.find(doc => compare(doc.path) === compare(path))
    if (!changedDoc) return
    const key = changedDoc.key
    try {
      const fresh = await api.code.file(pid, path)
      if (!isCurrentProject(pid, version) || !documents.value.some(doc => doc.key === key)) return
      captureEditor()
      const doc = workbench.updateExternal(key, fresh)
      if (file.value?.key === key) syncActiveDocument()
      if (doc?.dirty) { notice.value = '磁盘更新已检测到，未保存的草稿仍保留'; return }
      editorRef.value?.notifySaved?.({ key, path, uri: fresh.uri, text: fresh.text })
    } catch (cause) {
      if (isCurrentProject(pid, version) && cause?.status === 404 && documents.value.some(doc => doc.key === key)) {
        captureEditor()
        if (!changedDoc.dirty && result?.batch) {
          workbench.close(key); releaseClosedDocument(changedDoc); syncActiveDocument()
          return
        }
        changedDoc.file = { ...changedDoc.file, missing: true }
        if (file.value?.key === key) syncActiveDocument()
      }
    }
  }))
  if (isCurrentProject(pid, version) && result?.batch) editorRef.value?.notifyResourceChanges?.(result.batch, result.batch.state === 'reverted', project.value.root_path)
}

/** 任务面板切换了"看哪棵树"：地图跟着切，并把符号级差异作为叠加层传下去。 */
async function onTaskTree(payload) {
  if (!project.value) return
  const pid = project.value.id
  const version = projectVersion
  const request = ++taskRequest
  activeTree.value = payload?.tree || 'main'
  activeTaskId.value = payload?.taskId || ''
  taskDiff.value = null
  if (activeTaskId.value) {
    try {
      const review = await api.code.taskReview(pid, activeTaskId.value)
      if (!isCurrentProject(pid, version) || request !== taskRequest) return
      taskDiff.value = review.symbols || null
    } catch {
      if (!isCurrentProject(pid, version) || request !== taskRequest) return
      taskDiff.value = null
    }
  }
  mapRef.value?.loadMap?.()
  centerView.value = 'map'
}

/** 任务发生状态变化（执行/合并/放弃）：刷新地图与台账。 */
function onTaskChanged(payload) {
  patchRefresh.value += 1
  mapRef.value?.loadMap?.({ keepExpand: true })
  scmPanel.value?.load?.()
  if (payload?.status === 'merged' || payload?.status === 'discarded') {
    taskRequest += 1
    activeTree.value = 'main'
    activeTaskId.value = ''
    taskDiff.value = null
  }
}

onMounted(() => {
  loadProjects()
  window.addEventListener('keydown', onWorkbenchKey)
  window.addEventListener('beforeunload', beforeLeave)
})
watch(() => props.user?.id, () => { projectAccess.close(); loadProjects() })
watch(() => props.visible, active => {
  if (active === false) { stopDiagnosticsPolling(); functionDescriptions.reset(); return }
  if (!project.value?.available) return
  startDiagnosticsPolling()
  void restoreDocuments(project.value.id, projectVersion)
  scheduleProjectIntel(project.value.id, projectVersion)
  void refreshDiagnostics()
  void functionDescriptions.refresh(project.value.id)
})
watch(activeTree, () => { askPrompt.value = ''; pendingAgentNodeId.value = '' })
watch(rightTab, tab => { rightActivated.value = new Set([...rightActivated.value, tab]) })
watch(centerView, (view) => {
  if (view === 'map') mapActivated.value = true
  if (view === 'design') designActivated.value = true
})
watch(() => project.value?.id, (pid) => {
  if (pid) startDiagnosticsPolling()
  else stopDiagnosticsPolling()
})
watch(() => file.value?.path, (path) => { if (path) mobilePanel.value = 'center' })
watch([closingDocument, conflictDialog, languageEditPreview], () => nextTick(() => {
  const name = languageEditPreview.value ? '代码修改预览' : conflictDialog.value ? '文件保存冲突' : closingDocument.value ? '保存未完成的更改' : ''
  if (name) document.querySelector?.(`[aria-label="${name}"]`)?.querySelector('button:not(:disabled)')?.focus()
}))
watch(pickerOpen, (open) => { if (open) mobilePanel.value = 'center' })
onBeforeUnmount(() => {
  projectAccess.dispose()
  captureEditor()
  workbench.dispose()
  window.removeEventListener('keydown', onWorkbenchKey)
  window.removeEventListener('beforeunload', beforeLeave)
  disposed = true
  indexProgress.reset()
  functionDescriptions.reset()
  stopDiagnosticsPolling()
  dragCleanup?.()
})
</script>

<template>
  <div class="code" :class="`mobile-${mobilePanel}`">
    <div class="workbench-bar">
      <button class="layout-toggle" title="文件侧栏 · Ctrl+B" :aria-pressed="leftShown" aria-label="切换文件侧栏" @click="toggleFileSidebar"><UiIcon name="panel-left" :size="16" /></button>
      <AgentLogo class="workbench-brand" />
      <button class="quick-trigger" :disabled="!project" title="搜索文件或代码 · Ctrl+P / Ctrl+Shift+F" aria-label="搜索文件或代码" @click="openWorkspaceSearch('files')"><span><UiIcon name="search" :size="14" />搜索文件或代码</span><kbd>Ctrl+P</kbd></button>
      <div class="workspace-view-switcher" role="group" aria-label="工作区视图">
        <button type="button" :class="{ active: centerView === 'file' }" :aria-pressed="centerView === 'file'" @click="centerView = 'file'; mobilePanel = 'center'"><UiIcon name="code" :size="15" />编辑器</button>
        <button type="button" :class="{ active: centerView === 'map' }" :aria-pressed="centerView === 'map'" :disabled="!project || project.available === false" @click="captureEditor(); centerView = 'map'; mobilePanel = 'center'"><UiIcon name="layers" :size="15" />思维导图</button>
        <button type="button" :class="{ active: centerView === 'design' }" :aria-pressed="centerView === 'design'" :disabled="!project || project.available === false" @click="openFrontendDesign"><UiIcon name="edit" :size="15" />前端设计</button>
      </div>
      <span class="spacer" />
      <button class="workbench-action assistant-trigger" :class="{ active: rightShown }" :aria-pressed="rightShown" aria-label="AI 面板" @click="toggleAssistantPanel"><UiIcon name="sparkles" :size="14" /><span>AI 面板</span></button>
      <button class="workbench-action editing-trigger" :class="{ active: manualReadonly }" :aria-pressed="manualReadonly" :aria-label="manualReadonly ? '编辑只读' : '允许编辑'" :disabled="!project" :title="manualReadonly ? '允许人工编辑和设计保存，AI权限独立控制' : '将人工编辑和设计保存设为只读，AI权限独立控制'" @click="manualReadonly = !manualReadonly"><UiIcon :name="manualReadonly ? 'shield' : 'edit'" :size="14" /><span>{{ manualReadonly ? '编辑只读' : '允许编辑' }}</span></button>
      <button class="workbench-action settings-trigger" title="设置 · Ctrl+," aria-label="打开设置" @click="emit('open-settings')"><UiIcon name="settings" :size="16" /><span>设置</span></button>
    </div>
    <div class="workspace-switcher" aria-label="代码工作区面板">
      <button :class="{ active: mobilePanel === 'projects' }" :aria-pressed="mobilePanel === 'projects'" @click="showProjectPanel">项目与文件</button>
      <button :class="{ active: mobilePanel === 'center' }" :aria-pressed="mobilePanel === 'center'" @click="mobilePanel = 'center'">代码工作区</button>
      <button :class="{ active: mobilePanel === 'assistant' }" :aria-pressed="mobilePanel === 'assistant'" @click="showAssistantPanel">AI 助手</button>
    </div>
    <div v-if="notice || indexRefresh?.pending || indexRefresh?.state === 'error' || indexStatusError" class="workspace-notice-bar" aria-label="工作区状态">
      <span v-if="notice" class="muted tab-note">{{ notice }}</span>
      <span v-if="indexRefresh?.pending || indexRefresh?.state === 'error' || indexStatusError" class="index-refresh" :class="{ warning: indexRefresh?.state === 'error' || indexStatusError }" role="status" :title="indexStatusError || indexRefresh?.error || '文件已保存，后台更新项目索引与地图'">
        {{ indexStatusError || (indexRefresh?.state === 'error' ? '索引更新失败' : '索引更新中') }}
        <button v-if="indexRefresh?.state === 'error' || indexStatusError" :disabled="indexRetrying" @click="indexProgress.retry()">{{ indexRetrying ? '重试中…' : '重试' }}</button>
      </span>
    </div>
    <div class="layout" :style="gridStyle">
      <!-- 左：项目 + 文件树 -->
      <aside v-show="leftShown" class="left">
        <ProjectSwitcher :project="project" :projects="projects" :loading="loading" :phase="projectPhase" :error="projectError"
          @open-folder="openProjectFolder" @new-project="promptOpenProject('create')" @manual-open="promptOpenProject('path')"
          @select="selectProject" @close-project="projectAccess.close()" @remove="projectAccess.removeRecent" />

        <FileTree v-if="project" :key="project.id" ref="treeRef" :project-id="project.id" :active-path="file?.path || ''" @open="openFile" />
        <div v-else class="muted side-empty"><UiIcon name="folder" :size="28" /><strong>项目文件</strong><span>打开工作区后，在这里浏览和搜索代码。</span></div>
        <!-- 拖拽把手必须绝对定位：它要是参与 grid 布局，就会各占一列，
             把"左 | 中 | 右"挤成"左 | 把手 | 中"（宽度直接错位） -->
        <div class="drag drag-left" @mousedown="startDrag('left', $event)" />
      </aside>

      <!-- 中：地图 / 文件 / 空态 -->
      <main class="center">
        <div v-show="centerView === 'file'" class="editor-workbench-tools" role="toolbar" aria-label="编辑器工具">
          <div class="editor-navigation" role="group" aria-label="代码导航">
            <button class="workbench-action" :disabled="navigationBusy || !navigation.canBack.value" title="后退 · Alt+←" aria-label="导航后退" @click="navigateHistory(-1)"><UiIcon name="arrow-left" :size="15" /></button>
            <button class="workbench-action" :disabled="navigationBusy || !navigation.canForward.value" title="前进 · Alt+→" aria-label="导航前进" @click="navigateHistory(1)"><UiIcon name="arrow-right" :size="15" /></button>
          </div>
          <button class="workbench-action" :class="{ active: problemsVisible && bottomTab === 'problems' }" @click="problemsVisible && bottomTab === 'problems' ? problemsVisible = false : showBottom('problems')">问题 {{ diagCounts.error + diagCounts.warning || '' }}</button>
          <button class="workbench-action" :class="{ active: problemsVisible && bottomTab === 'terminal' }" @click="showBottom('terminal')"><UiIcon name="terminal" :size="14" />终端</button>
          <button class="workbench-action" :disabled="!file || executionBusy || saving" title="保存后运行 · Ctrl+F5" @click="runCurrent"><UiIcon name="play" :size="14" />运行</button>
          <button class="workbench-action" :disabled="!debugSupported || executionBusy || saving" title="保存后调试 Python / Java · F5" @click="runDebug()"><UiIcon name="bug" :size="14" />调试</button>
          <div ref="editorToolsTarget" class="editor-code-tools" />
        </div>
        <EditorTabs v-show="centerView === 'file'" :documents="documents" :active-key="activeDocumentKey" @select="activateDocument" @close="closeDocument" />
        <div v-if="workbench.storageError.value" class="draft-warning">{{ workbench.storageError.value }}，草稿仍在当前工作区，请保存文件。</div>
        <div v-if="file?.conflict && centerView === 'file'" class="draft-warning">磁盘内容已变化，当前显示的是你的草稿。保存时会进行冲突检查。</div>

        <div v-if="banner" class="banner">
          <span>{{ banner }}</span>
          <button class="ghost" @click="banner = ''">×</button>
        </div>
        <div v-if="fileBusy" class="muted center-hint">读取中…</div>
        <ProjectPicker v-else-if="!project" :meta="meta" :projects="projects" :loading="loading" :phase="projectPhase" :error="projectError" :initial-mode="pickerMode" :location-selection="projectLocationSelection"
          @open-folder="openProjectFolder" @pick-location="pickProjectLocation" @open-path="projectAccess.openPath" @create="projectAccess.createProject" @select="selectProject" @close="dismissProjectForm" />
        <!-- 首次打开地图才载入；之后保留组件，切回编辑器也不丢展开与缩放。 -->
        <MindMap
          v-if="project && mapActivated"
          :key="project.id"
          v-show="centerView === 'map'"
          ref="mapRef"
          :project-id="project.id"
          :tree="activeTree"
          :diagnostics="diagnostics"
          :overlay="taskDiff"
          :agents="agentTeam.team.value?.agents || []"
          @open-file="openFile"
          @select="onSelectNode"
          @ask="onAsk"
          @agent="onWorkflowAgent"
          @team-refresh="refreshAgentTeam"
          @error="(m) => (banner = m)"
        />
        <FrontendDesign
          v-if="project && designActivated"
          :key="project.id"
          v-show="centerView === 'design'"
          :project-id="project.id"
          :readonly="manualReadonly || languageEditBusy || !project.available"
          :dirty-paths="dirtyPaths"
          :case-insensitive="workspaceCaseInsensitive"
          :visible="props.visible !== false && centerView === 'design'"
          :ai-result="designAiResult"
          @ask="onDesignAsk"
          @saved="onDesignSaved"
          @error="banner = $event"
        />
        <EditorPane
          v-if="project"
          :key="project.id"
          v-show="centerView === 'file'"
          ref="editorRef"
          :toolbar-target="editorToolsTarget"
          :file="file"
          :highlight="fileLine ? { start: fileLine, end: fileLine, column: fileColumn, token: fileJump } : null"
          :readonly="manualReadonly || languageEditBusy || !project.available || file?.missing || file?.needsReload"
          :document-key="file?.key || ''"
          :restore-view-state="workbench.activeDocument.value?.viewState || null"
          :saving="saving"
          :project-id="project.id"
          :session-id="session?.id || ''"
          :lsp-server="lspForFile"
          :documents="documents"
          :language-servers="lspServers"
          :breakpoints="breakpoints"
          :debug-location="debugLocation"
          tree="main"
          @save="saveFile"
          @change="onEditorChange"
          @view-state="onEditorViewState"
          @selection="onEditorSelection"
          @open-file="openFile"
          @breakpoint="onBreakpoint"
          @language-edit="onLanguageEdit"
          @language-symbol="onLanguageSymbol"
        />
        <div v-if="problemsVisible || runtimeActivated || debugActivated" v-show="centerView === 'file' && problemsVisible" class="bottom-panel">
          <div class="bottom-tabs"><button v-for="tab in [{ id: 'problems', label: '问题' }, { id: 'terminal', label: '终端' }, { id: 'run', label: '运行' }, { id: 'output', label: '输出' }, { id: 'tests', label: '测试' }, { id: 'debug', label: '调试' }]" :key="tab.id" :class="{ active: bottomTab === tab.id }" @click="showBottom(tab.id)">{{ tab.label }}</button><span v-if="bottomTab === 'problems'" class="muted">{{ diagCounts.error }} 错误 · {{ diagCounts.warning }} 警告</span><span class="spacer" /><button class="ghost" aria-label="关闭问题面板" title="关闭底部面板" @click="problemsVisible = false">×</button></div>
          <ProblemsPanel v-show="bottomTab === 'problems'" :project-id="project?.id || ''" tree="main" :seq="diagnostics?.seq || 0" :visible="props.visible !== false && centerView === 'file' && problemsVisible && bottomTab === 'problems'" @open-file="openFile" />
          <RuntimePanel v-if="project && runtimeActivated" v-show="['terminal', 'run', 'output', 'tests'].includes(bottomTab)" ref="runtimeRef" :project-id="project.id" :file="file" :active-tab="bottomTab" :visible="props.visible !== false && centerView === 'file' && problemsVisible && ['terminal', 'run', 'output', 'tests'].includes(bottomTab)" @open-file="openFile" />
          <DebugPanel v-if="project && debugActivated" v-show="bottomTab === 'debug'" ref="debugRef" :project-id="project.id" :file="file" :breakpoints="breakpoints" :visible="props.visible !== false && centerView === 'file' && problemsVisible && bottomTab === 'debug'" @open-file="openFile" @breakpoints="breakpoints = $event" @location="debugLocation = $event" @start-request="runDebug" @error="banner = $event" />
        </div>
      </main>

      <!-- 右：AI / 改动台账（AI 面板必须常驻，否则切标签会断掉 WS 事件流） -->
      <aside v-show="rightShown" class="right">
        <div class="drag drag-right" @mousedown="startDrag('right', $event)" />
        <div class="right-tabs" aria-label="协作工具">
          <button :class="{ active: rightTab === 'ai' }" @click="rightTab = 'ai'"><UiIcon name="sparkles" :size="13" />AI</button>
          <button :class="{ active: rightTab === 'changes' }" @click="rightTab = 'changes'">修改</button>
          <button :class="{ active: rightTab === 'patches' }" @click="rightTab = 'patches'">台账</button>
          <button :class="{ active: rightTab === 'tasks' }" @click="rightTab = 'tasks'">任务</button>
          <button :class="{ active: rightTab === 'git' }" @click="rightTab = 'git'">Git</button>
          <span class="spacer" />
          <button
            class="perm"
            :disabled="!project || sessionBusy || !!conversationPermissionBusy"
            :class="{ writable: executionWritable }"
            :title="
              conversations.isChat.value
                ? '切换当前项目对话的执行权限；其他对话权限独立，人工编辑权限独立控制'
                : executionWritable ? '执行智能体可修改当前项目；Lead 可直接执行并按需委派，人工编辑权限独立控制'
                : '执行智能体只读，可提交待审修改，执行操作需申请批准；人工编辑权限独立控制'
            "
            @click="toggleWritable"
          >{{ executionWritable ? '执行可写' : '执行只读' }}</button>
        </div>
        <AiPanel
          :key="project?.id || 'no-project'"
          v-show="rightTab === 'ai'"
          :visible="props.visible !== false && rightShown && rightTab === 'ai'"
          :session-id="aiSessionId"
          :project-chat="conversations.isChat.value"
          :draft-scope="conversationDraftScope"
          :conversations-enabled="!!session?.id && activeTree === 'main'"
          :conversations="conversations.conversations.value"
          :conversation-id="conversations.selected.value?.id || conversations.canonicalId.value"
          :conversation-creating="conversations.creating.value"
          :conversation-deleting="conversations.deleting.value"
          :conversation-loading="conversations.loading.value"
          :conversation-error="conversations.error.value"
          :file-context="centerView === 'design' ? null : context"
          :code-context="centerView === 'design' ? null : codeContext"
          :pending-prompt="askPrompt"
          :writable="executionWritable"
          :selection-text="centerView === 'design' ? '' : selectedCode"
          :team="agentTeam.team.value"
          :agent-id="agentTeam.selected.value?.session_id || ''"
          :team-loading="agentTeam.loading.value"
          :team-error="agentTeam.error.value"
          :team-acting="agentTeam.acting.value"
          :workflow-node-id="pendingAgentNodeId"
          :workflow-tree="activeTree"
          @conversation-select="selectConversation"
          @conversation-create="createConversation"
          @conversation-delete="deleteConversation"
          @conversation-refresh="conversations.refresh()"
          @team-refresh="onTeamRefresh"
          @agent-stop="id => agentTeam.action(id, 'stop')"
          @clear-node-intent="pendingAgentNodeId = ''"
          @sent="pendingAgentNodeId = ''; agentTeam.refresh(); conversations.refresh()"
          @clear-context="contextEnabled = false"
          @consume-prompt="askPrompt = ''"
          @open-review="rightTab = 'changes'"
          @review-refresh="patchRefresh++"
          @assistant-complete="onDesignResult"
        />
        <ChangesPanel v-if="project && rightTab === 'changes'" :key="project.id" :project-id="project.id" :visible="changesVisible" :refresh-key="patchRefresh" :dirty-paths="dirtyPaths" :case-insensitive="workspaceCaseInsensitive" :readonly="manualReadonly || languageEditBusy" @open-file="openFile" @changed="onPatchesChanged" @error="banner = $event" />
        <PatchPanel
          v-if="project && rightActivated.has('patches')"
          :key="project.id"
          v-show="rightTab === 'patches'"
          :project-id="project.id"
          :refresh-key="patchRefresh"
          @open-file="openFile"
          @changed="onPatchesChanged"
          @error="(m) => (banner = m)"
        />
        <WorkItemsPanel
          v-if="project && rightActivated.has('tasks')"
          :key="project.id"
          v-show="rightTab === 'tasks'"
          ref="taskPanel"
          :project-id="project.id"
          :writable="writable"
          :visible="props.visible !== false && rightShown && rightTab === 'tasks'"
          :active-tree="activeTree"
          :refresh-key="patchRefresh"
          @open-agent="openWorkItemAgent"
          @open-tree="onTaskTree"
          @changed="onTaskChanged"
          @error="(m) => (banner = m)"
        />
        <SourceControl
          v-if="project && rightActivated.has('git')"
          :key="project.id"
          v-show="rightTab === 'git'"
          ref="scmPanel"
          :project-id="project.id"
          @changed="() => mapRef?.loadMap?.({ keepExpand: true })"
          @error="(m) => (banner = m)"
        />
      </aside>
    </div>

    <div class="status">
      <span v-if="project" class="mono ellipsis">{{ project.root_path }}</span>
      <span v-else class="muted status-ready"><i />代码工作区已就绪</span>
      <span class="spacer" />
      <span v-if="project?.available" class="function-description-status" :class="functionDescriptionStatus.tone" :title="functionDescriptionStatus.title" role="status">{{ functionDescriptionStatus.label }}</span>
      <span v-if="file" class="mono">{{ file.lang || 'text' }} · Ln {{ editorSelection?.start_line || 1 }}, Col {{ editorSelection?.start_column || 1 }}</span>
      <span v-if="sessionBusy" class="muted">准备会话…</span>
      <span v-else-if="session" class="muted">
        {{ conversations.isChat.value ? '项目对话' : '执行智能体' }} <b>{{ executionWritable ? '可写' : '只读' }}</b> · 编辑器 {{ manualReadonly ? '只读' : '可写' }}
      </span>
      <button
        v-if="diagnostics && (diagCounts.error || diagCounts.warning)"
        class="diag-sum"
        @click="showBottom('problems')"
        :title="`语言服务器诊断：错误 ${diagCounts.error} · 警告 ${diagCounts.warning}（在导图上按节点标注）`"
      >
        <b class="err">{{ diagCounts.error }} 错误</b>
        <span class="warn">{{ diagCounts.warning }} 警告</span>
      </button>
      <span v-else-if="diagnostics && !diagCounts.error && !diagCounts.warning" class="muted">无诊断问题</span>
      <span v-if="meta && !meta.git_available" class="muted">未检测到 git</span>
    </div>
    <QuickOpen :project-id="project?.id || ''" :open="quickOpen" :initial-mode="quickMode" @mode-change="quickMode = $event" @close="quickOpen = false" @open-file="openFile" />
    <div v-if="languageEditBusy" class="language-edit-loading" role="status">正在准备代码修改预览…</div>
    <button v-if="languageEditUndo && !languageEditPreview" class="language-undo" :disabled="manualReadonly || languageEditBusy" @click="undoLanguageEdit" title="整批撤销最近一次语言服务修改">撤销代码修改</button>
    <div v-if="languageEditPreview" class="workbench-modal language-edit-modal" @keydown.esc="!languageEditBusy && (languageEditPreview = null)">
      <section role="dialog" aria-modal="true" aria-label="代码修改预览">
        <h3>{{ languageEditPreview.title }}</h3><p>{{ languageEditPreview.batch ? `涉及 ${languageEditPreview.rows.transactionFiles.length} 个路径，整批应用到项目文件，可回滚。` : `涉及 ${languageEditPreview.rows.length} 个文件，确认后应用到草稿。` }}</p>
        <div class="language-edit-files"><button v-for="(row, index) in languageEditPreview.rows" :key="`${row.kind}:${row.path}:${index}`" :class="{ active: index === languageEditSelected }" @click="languageEditSelected = index">{{ row.kind === 'create' ? '新增 · ' : row.kind === 'delete' ? '删除 · ' : row.kind === 'rename' ? '移动 · ' : '' }}{{ row.oldPath ? `${row.oldPath} → ` : '' }}{{ row.path }}</button></div>
        <div v-if="languageEditRow" class="conflict-compare"><label>修改前<textarea readonly :value="languageEditRow.before" /></label><label>修改后<textarea readonly :value="languageEditRow.after" /></label></div>
        <div class="modal-actions"><button :disabled="languageEditBusy" @click="languageEditPreview = null">取消</button><button class="primary" :disabled="manualReadonly || languageEditBusy" @click="applyLanguageEdit">{{ languageEditBusy ? '处理中…' : languageEditPreview.batch ? '应用项目重构' : '应用到草稿' }}</button></div>
      </section>
    </div>
    <div v-if="pickerOpen && project" class="workbench-modal project-modal" @click.self="dismissProjectForm">
      <section role="dialog" aria-modal="true" aria-label="打开项目">
        <button class="project-modal-close" aria-label="返回当前工作区" :disabled="projectBusy" @click="dismissProjectForm">×</button>
        <ProjectPicker compact :meta="meta" :projects="projects" :loading="loading" :phase="projectPhase" :error="projectError" :initial-mode="pickerMode" :location-selection="projectLocationSelection"
          @open-folder="openProjectFolder" @pick-location="pickProjectLocation" @open-path="projectAccess.openPath" @create="projectAccess.createProject" @select="selectProject" @close="dismissProjectForm" />
      </section>
    </div>
    <div v-if="closingDocument" class="workbench-modal" @keydown.esc="finishClose('cancel')">
      <section role="dialog" aria-modal="true" aria-label="保存未完成的更改"><h3>保存更改？</h3><p>{{ closingDocument.path }} 有未保存的内容。</p><p class="muted">选择保存后关闭，或丢弃本次草稿。</p><div class="modal-actions"><button @click="finishClose('cancel')">取消</button><button :disabled="saving" @click="finishClose('discard')">丢弃更改</button><button class="primary" :disabled="saving || manualReadonly" @click="finishClose('save')">{{ saving ? '保存中…' : '保存并关闭' }}</button></div></section>
    </div>
    <div v-if="conflictDialog" class="workbench-modal conflict-modal">
      <section role="dialog" aria-modal="true" aria-label="文件保存冲突"><h3>磁盘上的文件已更新</h3><p>{{ conflictDialog.path }} · 选择如何处理，草稿仍保留。</p><div class="conflict-compare"><label>你的草稿<textarea readonly :value="documents.find(doc => doc.key === conflictDialog.key)?.text ?? conflictDialog.text" /></label><label>磁盘版本<textarea readonly :value="conflictDialog.fresh.text" /></label></div><div class="modal-actions"><button :disabled="saving" @click="resolveConflict('keep')">保留草稿</button><button :disabled="saving" @click="resolveConflict('disk')">使用磁盘版本</button><button class="primary" :disabled="saving || manualReadonly" @click="resolveConflict('overwrite')">覆盖保存</button></div></section>
    </div>
  </div>
</template>

<style scoped>
.code { position: relative; display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; height: 100%; background: var(--panel); }
.workbench-bar { display: flex; align-items: center; gap: 10px; height: 44px; min-width: 0; flex: none; padding: 0 12px; border-bottom: 1px solid var(--border); background: var(--panel-soft); }
.layout-toggle, .workbench-action { display: inline-flex; align-items: center; justify-content: center; gap: 5px; flex: none; min-height: 28px; font-size: var(--fs-xs); padding: 4px 7px; background: transparent; border-color: transparent; color: var(--text-2); white-space: nowrap; border-radius: 5px; }
.layout-toggle { width: 28px; padding: 0; }
.workbench-action:hover:not(:disabled), .layout-toggle:hover { background: var(--hover); }
.workbench-action.active { color: var(--accent); background: var(--accent-soft); }
.workbench-brand { margin-right: 5px; padding-right: 13px; border-right: 1px solid var(--border-strong); }
.quick-trigger { display: flex; align-items: center; justify-content: space-between; flex: 0 1 340px; min-width: 120px; gap: 24px; width: min(340px, 28vw); min-height: 29px; padding: 4px 9px; background: var(--panel); border: 1px solid var(--border-strong); color: var(--muted); font-size: var(--fs-xs); border-radius: 6px; }
.quick-trigger > span { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
.quick-trigger kbd { font-size: var(--fs-2xs); color: var(--muted); background: var(--panel-soft); border: 0; padding: 0 4px; }
.workspace-switcher { display: none; }
.workspace-notice-bar { display: flex; align-items: center; gap: 10px; flex: none; min-width: 0; padding: 4px 12px; border-bottom: 1px solid var(--border); background: var(--panel); }
.workspace-view-switcher { display: inline-flex; align-items: center; gap: 3px; flex: none; padding: 3px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel-soft); }
.workspace-view-switcher > button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 28px; padding: 4px 12px; border: 1px solid transparent; border-radius: 5px; background: transparent; color: var(--muted); font-size: var(--fs-xs); line-height: 18px; white-space: nowrap; box-shadow: none; }
.workspace-view-switcher > button:hover:not(:disabled):not(.active) { color: var(--text); background: transparent; }
.workspace-view-switcher > button.active, .workspace-view-switcher > button.active:hover { color: var(--accent); background: var(--panel); border-color: var(--border-strong); font-weight: var(--fw-semibold); box-shadow: var(--shadow-sm); }
.workspace-notice-bar .tab-note { max-width: min(620px, 65vw); padding-left: 0; }
.editor-workbench-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 3px; flex: none; min-width: 0; min-height: 36px; padding: 3px 8px; border-bottom: 1px solid var(--border); background: var(--panel); }
.editor-navigation { display: inline-flex; align-items: center; gap: 2px; margin-right: 3px; padding-right: 5px; border-right: 1px solid var(--border); }
.editor-code-tools { display: flex; align-items: center; min-width: 0; margin-left: 3px; padding-left: 6px; border-left: 1px solid var(--border); }
.editor-code-tools:empty { display: none; }
.layout { position: relative; display: grid; flex: 1; min-width: 0; min-height: 0; grid-template-columns: 264px minmax(0, 1fr) 380px; }
.left { grid-column: 1; position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel-soft); border-right: 1px solid var(--border); }
.right { grid-column: 3; position: relative; min-width: 0; min-height: 0; background: var(--panel); border-left: 1px solid var(--border); display: flex; flex-direction: column; }
.center { grid-column: 2; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; background: var(--panel); }
.right-tabs, .bottom-tabs { display: flex; align-items: center; flex: none; gap: 1px; height: 37px; padding: 0 9px; border-bottom: 1px solid var(--border); background: var(--panel); }
.right-tabs > button, .bottom-tabs > button { position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 100%; flex: none; padding: 0 10px; border: 0; border-radius: 0; color: var(--muted); background: transparent; font-size: var(--fs-xs); font-weight: var(--fw-medium); white-space: nowrap; }
.right-tabs > button:hover, .bottom-tabs > button:hover { color: var(--text); background: var(--panel-soft); }
.right-tabs > button.active, .bottom-tabs > button.active { color: var(--accent); box-shadow: inset 0 -2px var(--accent); }
.right-tabs { padding: 0 7px; gap: 0; }
.right-tabs > button { gap: 4px; padding: 0 8px; }
.right-tabs .perm { height: 24px; padding: 0 7px; margin-left: 6px; border: 1px solid var(--border); border-radius: 4px; background: var(--panel-soft); color: var(--muted); font-size: var(--fs-2xs); box-shadow: none; }
.right-tabs .perm.writable { background: var(--ok-soft); color: var(--ok); border-color: color-mix(in srgb, var(--ok) 22%, transparent); }
.side-empty { display: flex; flex-direction: column; align-items: center; gap: 12px; text-align: center; margin: auto; max-width: 240px; padding: 32px 24px; font-size: var(--fs-xs); line-height: 1.8; }
.side-empty > svg { color: var(--faint); }.side-empty strong { color: var(--text-2); font-weight: var(--fw-medium); }
.tab-note { font-size: var(--fs-2xs); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding-left: 8px; }
.index-refresh { flex-shrink: 0; display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-2xs); color: var(--muted); padding-left: 6px; }
.index-refresh.warning { color: var(--warn); }.index-refresh button { min-height: 22px; padding: 2px 6px; font-size: inherit; }
.draft-warning { font-size: var(--fs-xs); line-height: 1.6; padding: 7px 14px; color: var(--warn); background: var(--warn-soft); border-bottom: 1px solid var(--border); }
.banner { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 9px 14px; font-size: var(--fs-xs); background: var(--err-soft); color: var(--err); border-bottom: 1px solid var(--border); overflow-wrap: anywhere; }
.center-hint { padding: 24px; font-size: var(--fs-sm); }
.bottom-panel { height: 320px; max-height: 48%; min-height: 160px; flex: none; border-top: 1px solid var(--border-strong); background: var(--panel); display: flex; flex-direction: column; overflow: hidden; }
.bottom-tabs { min-height: 35px; height: 35px; font-size: var(--fs-2xs); overflow-x: auto; background: var(--panel-soft); padding: 0 8px; }
.bottom-tabs > .muted { white-space: nowrap; margin-left: 10px; }.bottom-tabs > .ghost { height: 24px; width: 24px; padding: 0; border-radius: 4px; }
.drag { position: absolute; top: 0; bottom: 0; width: 7px; z-index: 5; cursor: col-resize; background: transparent; transition: background .15s; }
.drag:hover { background: color-mix(in srgb, var(--accent) 18%, transparent); }.drag-left { right: -3px; }.drag-right { left: -4px; }
.status { display: flex; align-items: center; gap: 14px; min-height: 26px; flex: none; padding: 3px 13px; border-top: 1px solid var(--border); background: var(--panel-soft); font-size: var(--fs-2xs); color: var(--muted); }
.function-description-status { flex-shrink: 0; }.function-description-status.running { color: var(--accent); }.function-description-status.error, .function-description-status.missing { color: var(--warn); }
.status > .ellipsis { min-width: 0; max-width: 45%; }.status-ready { display: inline-flex; align-items: center; gap: 7px; }.status-ready i { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); }
.spacer { flex: 1; min-width: 0; }.diag-sum { display: flex; gap: 9px; }
.status button.diag-sum { min-height: 20px; padding: 0; border: 0; background: transparent; font-size: inherit; }
.diag-sum .err { color: var(--err); }.diag-sum .warn { color: var(--warn); }.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workbench-modal { position: absolute; inset: 0; z-index: 60; background: #20273555; backdrop-filter: blur(3px); display: flex; align-items: center; justify-content: center; padding: 24px; }
.workbench-modal > section { width: 460px; max-width: 100%; max-height: 100%; overflow: auto; padding: 26px; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 20px 70px #20273530; }
.workbench-modal h3 { margin: 0 0 12px; font-size: var(--fs-lg); letter-spacing: -.015em; }.workbench-modal p { font-size: var(--fs-sm); line-height: 1.7; overflow-wrap: anywhere; }
.modal-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--border); }.modal-actions button { font-size: var(--fs-xs); padding: 7px 12px; }
.project-modal > section { position: relative; width: 680px; padding: 0; max-height: min(780px, 100%); }
.project-modal-close { position: absolute; right: 16px; top: 16px; z-index: 1; border: 0; background: transparent; color: var(--muted); width: 30px; height: 30px; min-height: 0; padding: 0; font-size: var(--fs-xl); line-height: 1; }.project-modal-close:hover { background: var(--hover); }
.conflict-modal { z-index: 65; }.conflict-modal > section { width: 960px; }
.conflict-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 18px; }.conflict-compare label { min-width: 0; font-size: var(--fs-xs); display: flex; flex-direction: column; gap: 8px; color: var(--text-2); }.conflict-compare textarea { width: 100%; height: min(350px, 45vh); resize: none; background: var(--panel-soft); font-family: var(--font-mono, monospace); font-size: var(--fs-xs); line-height: 1.7; white-space: pre; }
.language-edit-modal { z-index: 70; }.workbench-modal.language-edit-modal > section { width: 1000px; }
.language-edit-files { display: flex; gap: 6px; flex-wrap: wrap; margin: 14px 0; }.language-edit-files button { max-width: 100%; overflow-wrap: anywhere; text-align: left; font-size: var(--fs-xs); padding: 5px 9px; }.language-edit-files button.active { color: var(--accent); background: var(--accent-soft); border-color: var(--accent-border); }
.language-undo, .language-edit-loading { position: absolute; right: 18px; bottom: 38px; z-index: 50; max-width: calc(100% - 36px); font-size: var(--fs-xs); padding: 9px 13px; background: var(--panel); border: 1px solid var(--border); border-radius: 7px; box-shadow: var(--shadow-pop); }.language-edit-loading { color: var(--muted); }
@media (max-width: 1350px) { .workbench-action { gap: 4px; padding: 4px 6px; }.quick-trigger { width: min(260px, 24vw); }.right-tabs { overflow-x: auto; scrollbar-width: thin; }.right-tabs > button { padding: 0 7px; } }
@media (max-width: 1100px) {
  .workbench-bar { gap: 8px; padding: 0 8px; }.workbench-brand { margin-right: 0; padding-right: 8px; }.quick-trigger { width: auto; flex: 1 1 180px; min-width: 100px; gap: 8px; }.quick-trigger kbd { display: none; }.layout-toggle { display: none; }
  .workspace-switcher { display: flex; flex: none; gap: 4px; padding: 6px 10px; border-bottom: 1px solid var(--border); background: var(--panel-soft); }
  .workspace-switcher button { flex: 1; min-height: 29px; padding: 4px 6px; font-size: var(--fs-xs); border-color: transparent; background: transparent; color: var(--muted); border-radius: 5px; }
  .workspace-switcher button.active { background: var(--panel); color: var(--accent); box-shadow: 0 1px 3px #20273510; border-color: var(--border); }
  .layout { grid-template-columns: minmax(0, 1fr) !important; }.layout > .left, .layout > .right, .layout > .center { display: none; min-width: 0; }
  .mobile-projects .layout > .left, .mobile-center .layout > .center, .mobile-assistant .layout > .right { display: flex; grid-column: 1; border-right: 0; border-left: 0; }
  .drag { display: none; }.right-tabs { overflow-x: auto; flex: none; }.status { overflow-x: auto; white-space: nowrap; scrollbar-width: thin; }.status > .ellipsis { max-width: 220px; flex-shrink: 0; }.tab-note { max-width: 160px; }
}
@media (max-width: 900px) {
  .workbench-bar { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto auto; height: auto; min-height: 44px; padding: 6px 8px; gap: 6px 8px; }.workbench-brand { grid-column: 1; }.quick-trigger { grid-column: 2; min-width: 0; width: 100%; }.assistant-trigger { grid-column: 3; }.editing-trigger { grid-column: 4; }.settings-trigger { grid-column: 5; }.assistant-trigger > span, .editing-trigger > span, .settings-trigger > span, .workbench-bar > .spacer { display: none; }.workbench-bar .assistant-trigger > svg, .workbench-bar .editing-trigger > svg, .workbench-bar .settings-trigger > svg { display: block; }.workbench-bar .workspace-view-switcher { grid-row: 2; grid-column: 1 / -1; display: flex; }.workspace-view-switcher > button { flex: 1; padding: 4px 9px; }
}
@media (max-width: 600px) {
  .workbench-action { font-size: var(--fs-2xs); }.editor-workbench-tools { gap: 2px; padding: 3px 6px; }.editor-workbench-tools .workbench-action { padding: 4px 5px; }
  .workspace-notice-bar { padding: 4px 8px; flex-wrap: wrap; }.index-refresh { font-size: var(--fs-2xs); }.index-refresh.warning { max-width: 100%; }
  .workbench-modal { padding: 12px; }.workbench-modal > section { padding: 20px; }.workbench-modal.project-modal > section { padding: 0; }
  .conflict-compare { grid-template-columns: 1fr; gap: 12px; }.conflict-compare textarea { height: 120px; }.modal-actions { gap: 6px; }.bottom-tabs > .muted { display: none; }.status { gap: 12px; }
}
@media (prefers-reduced-motion: reduce) { .drag { transition: none; } }
</style>
