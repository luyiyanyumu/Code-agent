<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../../api.js'
import UiIcon from '../ui/UiIcon.vue'
import DesignLayers from './DesignLayers.vue'
import DesignInspector from './DesignInspector.vue'
import DesignAcceptance from './DesignAcceptance.vue'
import DesignAssist from './DesignAssist.vue'
import DesignChanges from './DesignChanges.vue'
import DesignViewport from './DesignViewport.vue'
import { formatAcceptanceReport } from '../../lib/designAcceptance.js'
import { normalizeDesignPresets, normalizeDesignPresetName, createDesignPreset, MAX_DESIGN_PRESETS } from '../../lib/designPresets.js'
import { buildDesignChanges } from '../../lib/designChanges.js'
import { normalizeDesignViewport } from '../../lib/designViewport.js'
import { downloadDesignText } from '../../lib/designDownloads.js'
import { clampZoom, stepZoom, wheelZoom, fitCanvasZoom, zoomScrollPosition, isDesignTypingTarget } from '../../lib/designCanvas.js'
import { STYLE_FIELDS, normalizeStyleDeclarations, serializeDesignCss, parseDesignCss, isManagedDesignCss, createDesignDocument, createStyleRequest, parseStyleResult, validateBridgeMessage } from '../../lib/frontendDesign.js'

const props = defineProps({ projectId: { type: String, required: true }, readonly: { type: Boolean, default: false }, visible: { type: Boolean, default: true }, aiResult: { type: Object, default: null }, dirtyPaths: { type: Array, default: () => [] }, caseInsensitive: { type: Boolean, default: false } })
const emit = defineEmits(['ask', 'saved', 'error'])
const iframeEl = ref(null)
const active = ref(null)
const contexts = new Map()
const group = ref('appearance')
const panelOpen = ref(true)
const layersOpen = ref(true)
const canvasViewport = ref(null)
const workspaceEl = ref(null)
const viewportSize = ref({ width: 960, height: 640 })
const spaceHeld = ref(false)
const panning = ref(false)
const panShieldEl = ref(null)
const narrowViewport = ref(false)
let canvasObserver = null
let panSession = null
let disposed = false
let requestSequence = 0
let zoomSequence = 0
let wheelFrame = null
let pendingWheel = null
let wheelPersistence = null
let wheelEpoch = 0
const timers = new Set()
const readControllers = new Set()
const STORAGE_PREFIX = 'code-agent-frontend-design-v1:'
const clone = value => JSON.parse(JSON.stringify(value))
const identifier = () => globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
const current = state => !disposed && active.value === state && props.projectId === state.projectId
const groups = [
  { key: 'appearance', label: '颜色与文字', icon: 'edit', names: ['color', 'typography', 'appearance', 'text', '颜色', '文字'] },
  { key: 'layout', label: '尺寸与布局', icon: 'layers', names: ['layout', 'spacing', 'size', 'dimensions', '布局', '尺寸', '间距'] },
  { key: 'border', label: '边框与效果', icon: 'settings', names: ['border', 'effects', 'effect', '边框', '效果'] },
]
const fieldsForGroup = computed(() => STYLE_FIELDS.filter(field => {
  const property = field.property || ''
  if (group.value === 'layout') return /^(width|height|min-|max-|margin|padding|display|flex|align-|justify-|gap|position|top|left|right|bottom|grid|overflow|box-sizing)/.test(property)
  if (group.value === 'border') return /^(border|box-shadow|opacity|outline)/.test(property)
  return !/^(width|height|min-|max-|margin|padding|display|flex|align-|justify-|gap|position|top|left|right|bottom|grid|overflow|box-sizing|border|box-shadow|opacity|outline)/.test(property)
}))
const selected = computed(() => active.value?.selected || null)
const rules = computed(() => active.value?.rules || [])
const dirty = computed(() => !!active.value && serializeDesignCss(rules.value) !== active.value.savedCss)
const undoAvailable = computed(() => (active.value?.historyIndex || 0) > 0)
const redoAvailable = computed(() => !!active.value && active.value.historyIndex < active.value.history.length - 1)
const dirtySource = computed(() => {
  const state = active.value, normalize = path => {
    const normalized = String(path || '').replaceAll('\\', '/').replace(/^\.\//, '')
    return props.caseInsensitive ? normalized.toLowerCase() : normalized
  }
  const paths = new Set((props.dirtyPaths || []).map(normalize))
  return !!state && [state.loadedEntryPath, state.stylesheetPath].filter(Boolean).some(path => paths.has(normalize(path)))
})
const passCount = computed(() => (active.value?.checks || []).filter(item => item.state === 'passed').length)
const failureCount = computed(() => (active.value?.checks || []).filter(item => item.state === 'failed').length)
const zoomLevel = computed(() => clampZoom(active.value?.zoom ?? 100))
const frameSize = computed(() => normalizeDesignViewport(active.value?.customViewport) || ({ width: active.value?.device === 'phone' ? 390 : active.value?.device === 'tablet' ? 768 : Math.max(320, viewportSize.value.width - 48), height: Math.max(360, viewportSize.value.height - 48) }))
const canvasStyle = computed(() => ({ width: `${frameSize.value.width}px`, height: `${frameSize.value.height}px`, transform: `scale(${zoomLevel.value / 100})`, transformOrigin: '0 0' }))
const canvasStageStyle = computed(() => ({ width: `${frameSize.value.width * zoomLevel.value / 100}px`, height: `${frameSize.value.height * zoomLevel.value / 100}px` }))
const panToolActive = computed(() => active.value?.mode === 'edit' && (active.value?.canvasTool === 'pan' || spaceHeld.value))
const selectedOverrides = computed(() => rules.value.find(rule => rule.selector === selected.value?.selector)?.declarations || {})
const selectionAncestors = computed(() => selected.value?.ancestors || [])
const styleChanges = computed(() => buildDesignChanges(rules.value, active.value?.savedCss || ''))
const aiBusy = computed(() => active.value?.aiPhase === 'waiting')
const transformInProgress = computed(() => !!active.value?.transformGesture)
const zoomPresets = [25, 50, 75, 100, 125, 150, 200]
const aiDiff = computed(() => {
  const state = active.value
  if (!state?.aiProposal || !state.selected) return []
  const own = state.rules.find(rule => rule.selector === state.selected.selector)?.declarations || {}
  return Object.entries(state.aiProposal.declarations).map(([property, after]) => ({ property, before: own[property] || state.selected.styles?.[property] || '未设置', after })).filter(item => item.before !== item.after)
})
const sourceLabel = computed(() => ['url', 'dev-server'].includes(active.value?.preview?.source_kind) ? '开发页面' : '项目页面')
const entryRuntime = computed(() => {
  const state = active.value
  if (state?.runtimeEntry === state?.entryPath && state?.runtime) return state.runtime
  return state?.entries.find(entry => entry.path === state.entryPath)?.runtime || null
})
const suggestedService = computed(() => {
  try {
    const url = new URL(entryRuntime.value?.suggested_url || '')
    return url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) && !url.username && !url.password && !['8765', '5175'].includes(url.port) && !url.search && !url.hash ? url.href : ''
  } catch { return '' }
})
function localApiAddress(value) {
  try {
    const url = new URL(value)
    return url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) && !url.username && !url.password && !['8765', '5175'].includes(url.port) && !url.search && !url.hash ? url.href : ''
  } catch { return '' }
}
const suggestedApi = computed(() => {
  const state = active.value
  const suggestion = state?.entries.find(entry => entry.path === state.entryPath)?.api_suggested_url || (state?.runtimeEntry === state?.entryPath ? state?.apiSuggestedUrl : '') || ''
  return localApiAddress(suggestion)
})
const statusLabel = computed(() => {
  const state = active.value
  if (!state) return '未载入'
  if (state.loading) return '正在读取页面'
  if (state.saving) return '正在保存'
  if (state.transformGesture) return '正在调整元素'
  if (state.conflict) return '外部改动 · 草稿已保留'
  if (state.loadError) return '载入失败'
  if (state.serviceRequired) return '需要前端开发服务'
  if (state.preview && state.pageError) return '页面运行错误'
  if (state.preview && !state.bridgeReady) return state.bridgeError ? '画布连接失败' : '正在连接画布'
  return dirty.value ? '样式预览 · 尚未保存' : state.preview ? '已就绪' : '选择页面'
})

function readLocal(projectId) {
  try { return JSON.parse(localStorage.getItem(STORAGE_PREFIX + projectId) || 'null') } catch { return null }
}
function persistLocal(state) {
  try {
    localStorage.setItem(STORAGE_PREFIX + state.projectId, JSON.stringify({ version: 1, entryPath: state.entryPath, url: state.url, apiUrl: localApiAddress(state.apiUrl), device: state.device, customViewport: normalizeDesignViewport(state.customViewport), stylePresets: normalizeDesignPresets(state.stylePresets), zoom: clampZoom(state.zoom ?? 100), checks: state.checks.slice(0, 100), draft: state.preview && serializeDesignCss(state.rules) !== state.savedCss ? { css: serializeDesignCss(state.rules), baseSha1: state.sha1, entrySha1: state.entrySha1, entryPath: state.loadedEntryPath, url: state.loadedUrl } : null, pendingAi: state.pendingAi }))
  } catch { if (current(state)) state.notice = '浏览器存储不可用，本次预览仍保留在页面中。' }
}
function createContext(projectId) {
  const stored = readLocal(projectId)
  const checks = Array.isArray(stored?.checks) ? stored.checks.filter(item => typeof item?.id === 'string' && typeof item?.title === 'string').slice(0, 100).map(item => ({ id: item.id, title: item.title.slice(0, 240), state: ['passed', 'failed'].includes(item.state) ? item.state : 'pending', note: String(item.note || '').slice(0, 600) })) : []
  return reactive({ projectId, mode: 'acceptance', device: ['phone', 'tablet', 'fluid'].includes(stored?.device) ? stored.device : 'fluid', entries: [], entryPath: String(stored?.entryPath || ''), url: String(stored?.url || ''), apiUrl: typeof stored?.apiUrl === 'string' ? stored.apiUrl.slice(0, 4096) : '', apiSuggestedUrl: '', loadedEntryPath: '', loadedUrl: '', loaded: false, loading: false, loadError: '', runtime: null, runtimeEntry: '', serviceRequired: false, request: 0, preview: null, document: '', channel: '', bridgeReady: false, bridgeError: '', pageError: '', pageErrors: [], bridgeTimer: null, selected: null, styleDraft: {}, rules: [], savedCss: serializeDesignCss([]), sha1: '', entrySha1: '', stylesheetPath: '', stylesheetUnsupported: false, saving: false, conflict: false, notice: '', checks, checkDraft: '', aiInstruction: '', pendingAi: stored?.pendingAi?.projectId === projectId ? stored.pendingAi : null, aiProposal: null, aiNotice: '', history: [[]], historyIndex: 0, recoveredDraft: stored?.draft || null })
}
function prepareCanvasState(state) {
  if (state.zoom === undefined) state.zoom = clampZoom(readLocal(state.projectId)?.zoom ?? 100)
  if (!state.canvasTool) state.canvasTool = 'select'
  if (!Array.isArray(state.layers)) state.layers = []
  if (state.layersTruncated === undefined) state.layersTruncated = false
  if (state.layersLoading === undefined) state.layersLoading = false
  if (state.customViewport === undefined) {
    state.customViewport = normalizeDesignViewport(readLocal(state.projectId)?.customViewport)
    if (state.customViewport) state.device = 'custom'
  }
  if (!Array.isArray(state.stylePresets)) state.stylePresets = normalizeDesignPresets(readLocal(state.projectId)?.stylePresets)
  if (!state.aiPhase) state.aiPhase = 'idle'
  if (state.aiTimer === undefined) state.aiTimer = null
  if (state.transformGesture === undefined) state.transformGesture = null
}
async function boundedRead(operation, timeoutMessage) {
  let timer
  const controller = new AbortController()
  readControllers.add(controller)
  try {
    return await Promise.race([operation(controller.signal), new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new Error(timeoutMessage)) }, 12000); timers.add(timer) })])
  } finally { clearTimeout(timer); timers.delete(timer); readControllers.delete(controller) }
}
async function activate(projectId) {
  cancelWheelZoom()
  cancelTransform(active.value)
  endPan(); spaceHeld.value = false
  if (!projectId || disposed) { active.value = null; return }
  let state = contexts.get(projectId)
  if (!state) { state = createContext(projectId); contexts.set(projectId, state) }
  prepareCanvasState(state)
  active.value = state
  if (props.visible && !state.loaded && !state.loading) await loadInfo(state)
}
async function loadInfo(state = active.value) {
  if (!state || state.loading || disposed) return
  const token = ++requestSequence
  state.request = token; state.loading = true; state.loadError = ''; state.notice = ''
  try {
    const info = await boundedRead(signal => api.code.designInfo(state.projectId, { signal }), '读取项目页面入口超时，请重试；若持续超时，请检查平台后端与项目目录是否可访问。')
    if (disposed || state.request !== token) return
    state.entries = Array.isArray(info.entries) ? info.entries.filter(entry => typeof entry?.path === 'string') : []
    state.loaded = true
    if (!state.entryPath || !state.entries.some(entry => entry.path === state.entryPath)) state.entryPath = state.entries[0]?.path || ''
    state.loading = false
    if (state.entryPath) await loadPreview(state)
  } catch (error) {
    if (disposed || state.request !== token) return
    state.loadError = error.message || '无法读取项目页面。'; state.loaded = false
    if (current(state)) emit('error', state.loadError)
  } finally { if (state.request === token) state.loading = false }
}
async function loadPreview(state = active.value, { discardDraft = false } = {}) {
  if (!state || state.loading || disposed) return
  if (active.value === state) { cancelWheelZoom(); endPan() }
  cancelTransform(state)
  if (!state.entryPath) { state.notice = '请先选择当前项目的 HTML 入口。若项目使用 Vue 或 React，再填写已运行的前端开发服务地址。'; return }
  if (state.preview && serializeDesignCss(state.rules) !== state.savedCss && !discardDraft) { state.notice = '当前有未保存样式。请先保存，或使用“放弃草稿并重载”。'; return }
  const token = ++requestSequence, entryPath = state.entryPath, url = state.url.trim(), apiUrl = state.apiUrl.trim()
  clearBridgeTimeout(state)
  clearAiTimeout(state); state.aiPhase = 'idle'
  state.request = token; state.loading = true; state.loadError = ''; state.notice = ''; state.bridgeError = ''; state.bridgeReady = false
  state.layers = []; state.layersLoading = false; state.layersTruncated = false
  try {
    const response = await boundedRead(signal => api.code.designPreview(state.projectId, { entry_path: entryPath, url, ...(apiUrl ? { api_url: apiUrl } : {}) }, { signal }), url ? '连接前端页面超时，请确认当前项目的页面服务地址正确且可访问后重试。' : '载入项目页面超时，请重试；若持续超时，请检查入口文件及其资源。')
    if (disposed || state.request !== token) return
    const entry = state.entries.find(entry => entry.path === entryPath)
    state.runtime = response.runtime || entry?.runtime || null
    if (entry && response.runtime) entry.runtime = response.runtime
    state.runtimeEntry = entryPath
    state.apiSuggestedUrl = response.api_suggested_url || ''
    state.serviceRequired = !url && state.runtime?.requires_dev_server === true
    if (state.serviceRequired) {
      state.preview = null; state.document = ''; state.channel = ''; state.selected = null; state.styleDraft = {}; state.aiProposal = null
      state.pageError = ''; state.pageErrors = []; state.loadedEntryPath = response.entry_path || entryPath; state.loadedUrl = ''; state.stylesheetPath = response.stylesheet?.path || ''
      state.rules = []; state.savedCss = serializeDesignCss([]); state.history = [[]]; state.historyIndex = 0; state.conflict = false; state.stylesheetUnsupported = false
      return
    }
    if (typeof response.html !== 'string') throw new Error('预览响应缺少页面内容。')
    const text = typeof response.stylesheet?.text === 'string' ? response.stylesheet.text : ''
    state.stylesheetUnsupported = !!text.trim() && !isManagedDesignCss(text)
    let nextRules = parseDesignCss(text)
    state.savedCss = serializeDesignCss(nextRules)
    state.sha1 = response.stylesheet?.sha1 || ''; state.entrySha1 = response.entry_sha1 || ''; state.stylesheetPath = response.stylesheet?.path || ''; state.loadedEntryPath = response.entry_path || entryPath; state.loadedUrl = url
    state.conflict = false
    const draft = discardDraft ? null : state.recoveredDraft
    if (draft?.entryPath === state.loadedEntryPath && (draft.url || '') === url && isManagedDesignCss(draft.css)) {
      nextRules = parseDesignCss(draft.css)
      state.conflict = draft.baseSha1 !== state.sha1 || draft.entrySha1 !== state.entrySha1
      state.notice = state.conflict ? '已恢复本地草稿；页面或样式有外部改动。请对照后重新载入。' : '已恢复尚未保存的本地样式草稿。'
    }
    state.recoveredDraft = null; state.rules = nextRules; state.history = [clone(nextRules)]; state.historyIndex = 0; state.selected = null; state.styleDraft = {}; state.aiProposal = null
    state.channel = `frontend-design-${identifier()}`; state.preview = response
    let initialRoute = '/'
    if (url) { const address = new URL(url); initialRoute = address.hash.startsWith('#/') ? address.hash.slice(1) : address.pathname + address.search + address.hash }
    let resourceRootUrl = response.base_url || ''
    if (resourceRootUrl) {
      const base = new URL(resourceRootUrl)
      const capabilityPath = base.pathname.match(/^\/api\/v1\/code\/frontend-design-assets\/[A-Za-z0-9_-]+\//)?.[0]
      const entryDirectory = state.loadedEntryPath.split('/').slice(0, -1).join('/')
      const standalone = /(?:^|\/)(?:dist|build)$/i.test(entryDirectory) || (state.runtime?.command && state.runtime?.working_dir === (entryDirectory || '.'))
      if (capabilityPath && (['url', 'dev-server'].includes(response.source_kind) || !standalone)) resourceRootUrl = new URL(capabilityPath, base.origin).href
    }
    state.document = createDesignDocument(response.html, { channel: state.channel, baseUrl: response.base_url || '', css: serializeDesignCss(nextRules), mode: state.mode, initialRoute, resourceRootUrl, apiUrl: response.api_url || '', apiProxyUrl: response.api_proxy_url || '' })
    if (response.api_url) state.apiUrl = response.api_url
    state.pageError = ''; state.pageErrors = []
    persistLocal(state)
  } catch (error) {
    if (disposed || state.request !== token) return
    state.loadError = error.message || '无法载入页面。'
    if (current(state)) emit('error', state.loadError)
  } finally { if (state.request === token) state.loading = false }
}
function retry() { const state = active.value; if (!state) return; return state.loaded ? loadPreview(state, { discardDraft: !dirty.value }) : loadInfo(state) }
function connectSuggestedService() {
  const state = active.value, url = suggestedService.value
  if (!state || !url || state.loading || state.saving) return
  state.url = url
  return loadPreview(state)
}
function connectSuggestedApi() {
  const state = active.value, url = suggestedApi.value
  if (!state || !url || state.loading || state.saving) return
  state.apiUrl = url
  return loadPreview(state)
}
function disconnectApi() {
  const state = active.value
  if (!state || state.loading || state.saving) return
  state.apiUrl = ''
  return loadPreview(state)
}
function post(type, payload = {}) {
  const state = active.value
  if (!state?.channel || !iframeEl.value?.contentWindow || disposed) return
  iframeEl.value.contentWindow.postMessage({ channel: state.channel, type, ...payload }, '*')
}
function pushStyles() { post('styles', { rules: clone(rules.value), css: serializeDesignCss(rules.value) }) }
function syncInteraction() {
  const state = active.value
  if (!state) return
  const enabled = current(state) && props.visible && state.mode === 'edit' && !state.loading && !state.saving && state.canvasTool !== 'pan' && !spaceHeld.value && !panning.value
  if (!enabled) cancelTransform(state)
  const panEnabled = current(state) && props.visible && state.bridgeReady && state.mode === 'edit' && !state.loading && !state.saving
  post('interaction', { enabled, panEnabled, zoom: zoomLevel.value })
}
function cancelTransform(state = active.value) {
  const gesture = state?.transformGesture
  if (!gesture) return
  state.transformGesture = null
  if (state.selected?.selector === gesture.selector && gesture.startSelection) state.selected = clone(gesture.startSelection)
  if (active.value === state) { post('transform-result', { gestureId: gesture.id, accepted: false }); refreshDraft() }
}
function receiveTransform(message) {
  const state = active.value, gesture = state?.transformGesture
  if (!state) return
  if (message.phase === 'cancel') {
    if (gesture?.id === message.gestureId && gesture.selector === message.selector) { cancelTransform(state); pushStyles() }
    return
  }
  const permitted = current(state) && props.visible && state.bridgeReady && state.mode === 'edit' && !state.loading && !state.saving && state.canvasTool !== 'pan' && !spaceHeld.value && !panning.value && state.selected?.selector === message.selector
  if (message.phase === 'start') {
    if (!permitted) { if (gesture?.id === message.gestureId) cancelTransform(state); post('transform-result', { gestureId: message.gestureId, accepted: false }); return }
    if (permitted && !gesture) {
      state.transformGesture = { id: message.gestureId, selector: message.selector, channel: state.channel, baseCss: serializeDesignCss(state.rules), startSelection: clone(state.selected) }
      group.value = 'appearance'
    } else if (gesture?.id !== message.gestureId) post('transform-result', { gestureId: message.gestureId, accepted: false })
    return
  }
  const matches = gesture?.id === message.gestureId && gesture.selector === message.selector && gesture.channel === state.channel
  if (!permitted || !matches || gesture.baseCss !== serializeDesignCss(state.rules)) {
    if (matches) cancelTransform(state)
    if (!state.transformGesture) pushStyles()
    post('transform-result', { gestureId: message.gestureId, accepted: false })
    return
  }
  state.transformGesture = null
  const previous = state.rules.find(rule => rule.selector === message.selector)?.declarations || {}
  const unchanged = Object.entries(message.declarations).every(([property, value]) => previous[property] === value)
  const accepted = applyDeclarations(message.declarations) || unchanged
  pushStyles()
  post('transform-result', { gestureId: message.gestureId, accepted })
  if (!accepted) refreshDraft()
}
function setMode(mode) {
  const state = active.value
  if (!state || !['edit', 'acceptance'].includes(mode)) return
  cancelWheelZoom()
  cancelTransform(state)
  state.mode = mode; state.aiProposal = null
  if (mode === 'acceptance') { clearAiTimeout(state); state.aiPhase = 'idle' }
  endPan(); spaceHeld.value = false
  post('mode', { mode })
  if (mode === 'edit') refreshLayers()
  if (mode === 'edit' && state.selected) post('inspect', { selector: state.selected.selector })
}
function setDevice(device) { if (active.value && ['phone', 'tablet', 'fluid'].includes(device)) { cancelWheelZoom(); endPan(); cancelTransform(); active.value.device = device; active.value.customViewport = null; persistLocal(active.value) } }
function setViewport(size) {
  const state = active.value, normalized = normalizeDesignViewport(size)
  if (!state || !normalized || state.loading || state.saving) return false
  cancelWheelZoom(); endPan()
  cancelTransform(state)
  state.customViewport = normalized; state.device = 'custom'; persistLocal(state)
  return true
}
function refreshLayers() {
  const state = active.value
  if (!state?.preview || !state.bridgeReady || state.mode !== 'edit' || !props.visible) return
  state.layersLoading = true
  post('get-layers')
}
function selectLayer(selector) {
  const state = active.value
  if (!state?.bridgeReady || state.mode !== 'edit' || !props.visible || typeof selector !== 'string') return
  cancelTransform(state)
  state.canvasTool = 'select'
  post('inspect', { selector, scrollIntoView: true })
}
function clearCanvasSelection() {
  if (active.value?.mode !== 'edit') return
  cancelTransform()
  post('inspect', { selector: '' })
  active.value.selected = null; active.value.styleDraft = {}; active.value.aiProposal = null
}
function measureCanvas() {
  const element = canvasViewport.value
  if (element?.clientWidth > 0 && element.clientHeight > 0) viewportSize.value = { width: element.offsetWidth || element.clientWidth, height: element.offsetHeight || element.clientHeight }
}
function updateViewportMode() {
  const narrow = (workspaceEl.value?.clientWidth || window.innerWidth) <= 900
  if (narrow && !narrowViewport.value) { layersOpen.value = false; panelOpen.value = false }
  narrowViewport.value = narrow
}
function toggleLayers() { layersOpen.value = !layersOpen.value; if (narrowViewport.value && layersOpen.value) panelOpen.value = false }
function toggleProperties() { panelOpen.value = !panelOpen.value; if (narrowViewport.value && panelOpen.value) layersOpen.value = false }
function clearWheelPersistence(save = true) {
  const pending = wheelPersistence
  wheelPersistence = null
  if (!pending) return
  clearTimeout(pending.timer); timers.delete(pending.timer)
  if (save && contexts.get(pending.state.projectId) === pending.state) persistLocal(pending.state)
}
function cancelWheelZoom() {
  wheelEpoch += 1
  zoomSequence += 1
  if (wheelFrame !== null) window.cancelAnimationFrame(wheelFrame)
  wheelFrame = null; pendingWheel = null
  clearWheelPersistence()
}
function wheelContextIsCurrent(pending) {
  return current(pending.state) && props.visible && pending.state.channel === pending.channel && pending.state.mode === pending.mode
    && !pending.state.loading && !pending.state.saving && canvasViewport.value === pending.element && pending.epoch === wheelEpoch
}
function queueWheelZoom(deltaY, deltaMode, anchor) {
  const state = active.value, element = canvasViewport.value
  if (!state?.preview || !current(state) || !props.visible || state.loading || state.saving || !element) return
  if (pendingWheel && !wheelContextIsCurrent(pendingWheel)) cancelWheelZoom()
  const previous = pendingWheel?.zoom ?? zoomLevel.value
  const next = wheelZoom(previous, { deltaY, deltaMode, pageHeight: element.clientHeight })
  if (next === previous) return
  cancelTransform(state); endPan()
  if (wheelPersistence) { clearTimeout(wheelPersistence.timer); timers.delete(wheelPersistence.timer); wheelPersistence.timer = null }
  pendingWheel = { state, channel: state.channel, mode: state.mode, element, epoch: wheelEpoch, zoom: next, anchor }
  if (wheelFrame !== null) return
  const epoch = wheelEpoch
  const frame = window.requestAnimationFrame(() => {
    if (wheelEpoch !== epoch || wheelFrame !== frame) return
    wheelFrame = null
    const pending = pendingWheel
    pendingWheel = null
    if (!pending || !wheelContextIsCurrent(pending)) return
    void setZoom(pending.zoom, pending.anchor, { persist: false, wheel: true })
    const timer = setTimeout(() => {
      timers.delete(timer)
      if (wheelPersistence?.timer !== timer) return
      wheelPersistence = null
      if (wheelContextIsCurrent(pending)) persistLocal(pending.state)
    }, 250)
    timers.add(timer)
    wheelPersistence = { state: pending.state, timer }
  })
  wheelFrame = frame
}
async function setZoom(value, anchor = null, { persist = true, wheel = false } = {}) {
  const state = active.value, element = canvasViewport.value
  if (!state) return
  if (!wheel) cancelWheelZoom()
  endPan()
  cancelTransform(state)
  const token = ++zoomSequence, channel = state.channel
  const previousZoom = zoomLevel.value, nextZoom = clampZoom(value)
  const anchorX = anchor?.x ?? element?.clientWidth / 2, anchorY = anchor?.y ?? element?.clientHeight / 2
  const position = element ? zoomScrollPosition({ scrollLeft: element.scrollLeft, scrollTop: element.scrollTop, anchorX, anchorY, previousZoom, nextZoom }) : null
  const stage = element?.querySelector?.('.canvas-stage'), before = stage?.getBoundingClientRect(), viewport = element?.getBoundingClientRect?.()
  const point = before && viewport ? { x: (anchorX - before.left + viewport.left) * 100 / previousZoom, y: (anchorY - before.top + viewport.top) * 100 / previousZoom } : null
  state.zoom = nextZoom
  if (persist) persistLocal(state)
  await nextTick()
  if (current(state) && token === zoomSequence && state.channel === channel && position && canvasViewport.value === element) {
    const after = stage?.getBoundingClientRect()
    element.scrollLeft = point && after ? Math.max(0, element.scrollLeft + after.left - viewport.left + point.x * nextZoom / 100 - anchorX) : position.scrollLeft
    element.scrollTop = point && after ? Math.max(0, element.scrollTop + after.top - viewport.top + point.y * nextZoom / 100 - anchorY) : position.scrollTop
  }
}
function fitCanvas() { measureCanvas(); return setZoom(fitCanvasZoom({ viewportWidth: viewportSize.value.width, viewportHeight: viewportSize.value.height, frameWidth: frameSize.value.width, frameHeight: frameSize.value.height, padding: 24 })) }
async function focusSelection() {
  const state = active.value, element = canvasViewport.value, rect = selected.value?.rect
  if (!state || !element || !rect || rect.width <= 0 || rect.height <= 0) return
  const channel = state.channel, selector = selected.value.selector
  await setZoom(fitCanvasZoom({ viewportWidth: element.clientWidth, viewportHeight: element.clientHeight, frameWidth: rect.width, frameHeight: rect.height, padding: 64 }))
  if (!current(state) || state.channel !== channel || selected.value?.selector !== selector || canvasViewport.value !== element) return
  const ratio = zoomLevel.value / 100
  const frame = iframeEl.value?.getBoundingClientRect?.(), viewport = element.getBoundingClientRect?.()
  const originX = frame && viewport ? element.scrollLeft + frame.left - viewport.left : 24
  const originY = frame && viewport ? element.scrollTop + frame.top - viewport.top : 24 + 30 * ratio
  element.scrollLeft = Math.max(0, originX + rect.left * ratio - (element.clientWidth - rect.width * ratio) / 2)
  element.scrollTop = Math.max(0, originY + rect.top * ratio - (element.clientHeight - rect.height * ratio) / 2)
}
function onCanvasWheel(event) {
  if (!(event.ctrlKey || event.metaKey) || !active.value?.preview || !Number.isFinite(event.deltaY) || !event.deltaY) return
  event.preventDefault()
  const rect = canvasViewport.value?.getBoundingClientRect()
  if (rect) queueWheelZoom(event.deltaY, event.deltaMode ?? 0, { x: event.clientX - rect.left, y: event.clientY - rect.top })
}
function beginPan(event, source = 'parent') {
  const element = canvasViewport.value
  const state = active.value
  if (!state || !props.visible || !current(state) || state.mode !== 'edit' || state.loading || state.saving || !element || panSession
    || (!panToolActive.value && event.button !== 1) || ![0, 1].includes(event.button)
    || !Number.isInteger(event.pointerId) || ![event.clientX, event.clientY].every(Number.isFinite)) return
  cancelWheelZoom()
  cancelTransform()
  event.preventDefault()
  panSession = { state, channel: state.channel, source, pointerId: event.pointerId, x: event.clientX, y: event.clientY, left: element.scrollLeft, top: element.scrollTop, element, capture: event.currentTarget, parentInput: false }
  panning.value = true
  try { event.currentTarget?.setPointerCapture?.(event.pointerId) } catch { /* Window listeners still finish a released pointer. */ }
}
function movePan(event, source = 'parent') {
  if (!panSession || event.pointerId !== panSession.pointerId || ![event.clientX, event.clientY].every(Number.isFinite)) return
  if (!current(panSession.state) || !props.visible || panSession.channel !== panSession.state.channel || panSession.state.mode !== 'edit' || panSession.state.loading || panSession.state.saving || canvasViewport.value !== panSession.element) { endPan(); return }
  if (source === 'frame' && panSession.parentInput) return
  if (source === 'parent') panSession.parentInput = true
  if (typeof event.buttons === 'number' && event.buttons === 0) { endPan(event); return }
  event.preventDefault()
  canvasViewport.value.scrollLeft = panSession.left - event.clientX + panSession.x
  canvasViewport.value.scrollTop = panSession.top - event.clientY + panSession.y
}
function endPan(event = null) {
  if (event && Number.isInteger(event.pointerId) && panSession && event.pointerId !== panSession.pointerId) return
  const previous = panSession
  panSession = null; panning.value = false
  if (!previous) return
  if (previous.source === 'frame' && active.value === previous.state && previous.channel === previous.state.channel) post('canvas-pan-cancel', { pointerId: previous.pointerId })
  try { if (previous.capture?.hasPointerCapture?.(previous.pointerId)) previous.capture.releasePointerCapture(previous.pointerId) } catch { /* A removed shield has already released capture. */ }
}
function receiveCanvasPan(message) {
  const state = active.value
  if (message.phase === 'cancel' || message.phase === 'end') {
    if (panSession?.source === 'frame' && panSession.pointerId === message.pointerId) {
      if (message.phase === 'end') {
        const frame = iframeEl.value?.getBoundingClientRect?.()
        if (frame) movePan({ pointerId: message.pointerId, clientX: frame.left + message.clientX * zoomLevel.value / 100, clientY: frame.top + message.clientY * zoomLevel.value / 100, preventDefault() {} }, 'frame')
      }
      endPan(message)
    }
    return
  }
  if (!state || !current(state) || !props.visible || !state.bridgeReady || state.mode !== 'edit' || state.loading || state.saving) return
  const frame = iframeEl.value?.getBoundingClientRect?.()
  if (!frame) return
  const event = { pointerId: message.pointerId, button: 1, clientX: frame.left + message.clientX * zoomLevel.value / 100, clientY: frame.top + message.clientY * zoomLevel.value / 100, preventDefault() {} }
  if (message.phase === 'move') { movePan(event, 'frame'); return }
  if (message.phase !== 'start' || panSession) return
  beginPan(event, 'frame')
  const started = panSession
  if (!started) return
  nextTick(() => {
    if (panSession !== started || !current(state) || state.channel !== started.channel) return
    const shield = panShieldEl.value
    try { shield?.setPointerCapture?.(started.pointerId); if (shield) started.capture = shield } catch { /* Frame/window pointer events remain the fallback. */ }
  })
}
function onDesignKey(event) {
  if (!props.visible || active.value?.mode !== 'edit' || event.isComposing || isDesignTypingTarget(event.target)) return
  const key = String(event.key || '').toLowerCase()
  if (key === ' ') {
    if (event.target?.closest?.('button, summary, a[href], [role="button"], [role="treeitem"]')) return
    event.preventDefault(); spaceHeld.value = true; return
  }
  const modifier = event.ctrlKey || event.metaKey
  if (modifier && key === 'z') { event.preventDefault(); event.shiftKey ? redo() : undo(); return }
  if (modifier && key === 'y') { event.preventDefault(); redo(); return }
  if (modifier || event.altKey) return
  if (key === 'escape' && panning.value) { event.preventDefault(); endPan(); return }
  if (key === 'escape' && transformInProgress.value) { event.preventDefault(); cancelTransform(); return }
  if (key === 'escape') { event.preventDefault(); clearCanvasSelection() }
  else if (key === 'v') active.value.canvasTool = 'select'
  else if (key === 'h') active.value.canvasTool = 'pan'
  else if (event.shiftKey && (event.code === 'Digit1' || key === '1' || key === '!')) { event.preventDefault(); void fitCanvas() }
  else if (event.shiftKey && (event.code === 'Digit2' || key === '2' || key === '@')) { event.preventDefault(); void focusSelection() }
}
function releaseSpace(event) { if (event.type === 'blur') cancelWheelZoom(); if (event.type === 'blur' || event.key === ' ') { spaceHeld.value = false; endPan() } }
function refreshDraft() {
  const state = active.value
  if (!state?.selected) return
  const own = state.transformGesture ? {} : state.rules.find(rule => rule.selector === state.selected.selector)?.declarations || {}
  state.styleDraft = Object.fromEntries(STYLE_FIELDS.map(field => [field.property, own[field.property] || state.selected.styles?.[field.property] || '']))
}
function clearBridgeTimeout(state) {
  if (state?.bridgeTimer == null) return
  clearTimeout(state.bridgeTimer); timers.delete(state.bridgeTimer); state.bridgeTimer = null
}
function frameLoaded(event) {
  const state = active.value
  if (!state?.preview || !iframeEl.value || (event?.target && event.target !== iframeEl.value)) return
  cancelWheelZoom(); endPan()
  cancelTransform(state)
  clearBridgeTimeout(state)
  state.bridgeReady = false; state.bridgeError = ''; state.selected = null; state.styleDraft = {}; state.aiProposal = null
  state.layers = []; state.layersLoading = false; state.layersTruncated = false
  const channel = state.channel
  post('ping')
  state.bridgeTimer = setTimeout(() => {
    timers.delete(state.bridgeTimer); state.bridgeTimer = null
    if (current(state) && state.channel === channel && !state.bridgeReady) state.bridgeError = '画布选择工具未连接，页面可能已跳转或资源未加载。请重新载入页面后继续编辑。'
  }, 8000)
  timers.add(state.bridgeTimer)
}
async function navigatePreview(href) {
  const state = active.value
  if (!state?.preview || state.mode !== 'acceptance' || !props.visible) return
  if (state.loading || state.saving) { state.notice = '页面正在载入或保存，请稍后再跳转。'; return }
  if (serializeDesignCss(state.rules) !== state.savedCss) { state.notice = '页面有未保存的样式，请先保存后再跳转。'; return }
  try {
    const base = new URL(state.preview.base_url)
    const ticketPrefix = base.pathname.match(/^\/api\/v1\/code\/frontend-design-assets\/[A-Za-z0-9_-]+\//)?.[0]
    const target = new URL(href, base)
    if (!ticketPrefix || target.origin !== base.origin || !target.pathname.startsWith(ticketPrefix) || !['http:', 'https:'].includes(target.protocol)) throw new Error('只支持当前项目预览内的页面跳转。')
    const path = decodeURIComponent(target.pathname.slice(ticketPrefix.length))
    if (!path || path.startsWith('/') || /[\\\u0000-\u001f\u007f]/.test(path) || path.split('/').some(part => !part || ['.', '..'].includes(part))) throw new Error('页面跳转路径无效，请重新载入预览。')
    if (state.preview.source_kind === 'dev-server' || state.preview.source_kind === 'url') {
      if (!state.loadedUrl) throw new Error('当前开发页面缺少服务地址，请重新载入。')
      const remote = new URL(state.loadedUrl)
      const next = new URL('/' + target.pathname.slice(ticketPrefix.length), remote.origin)
      next.search = target.search; next.hash = target.hash
      state.entryPath = state.loadedEntryPath; state.url = next.href
    } else {
      if (!/\.html?$/i.test(path)) throw new Error('静态预览只支持跳转到项目内 HTML 页面；应用路由请使用前端开发服务。')
      state.entryPath = path; state.url = ''
      if (!state.entries.some(entry => entry.path === path)) state.entries.push({ path, label: path })
    }
    await loadPreview(state)
  } catch (error) { state.notice = error.message || '无法跳转到该页面。' }
}
function receive(event) {
  const state = active.value
  if (!state) return
  const message = validateBridgeMessage(event, { iframeWindow: iframeEl.value?.contentWindow, channel: state.channel })
  if (!message) return
  if (message.type === 'ready') { clearBridgeTimeout(state); state.bridgeReady = true; state.bridgeError = ''; syncInteraction(); post('mode', { mode: state.mode }); pushStyles(); refreshLayers() }
  else if (message.type === 'transform') receiveTransform(message)
  else if (message.type === 'canvas-pan') receiveCanvasPan(message)
  else if (message.type === 'selected-position' && state.mode === 'edit' && props.visible && state.selected?.selector === message.selector) {
    state.selected.rect = message.rect
  }
  else if (message.type === 'layers' && state.mode === 'edit' && props.visible) { state.layers = message.nodes; state.layersTruncated = message.truncated; state.layersLoading = false }
  else if (message.type === 'canvas-wheel' && state.mode === 'edit' && props.visible) {
    const frame = iframeEl.value?.getBoundingClientRect?.(), viewport = canvasViewport.value?.getBoundingClientRect?.()
    if (frame && viewport) queueWheelZoom(message.deltaY, message.deltaMode ?? 0, { x: frame.left - viewport.left + message.clientX * zoomLevel.value / 100, y: frame.top - viewport.top + message.clientY * zoomLevel.value / 100 })
  }
  else if (message.type === 'shortcut' && state.mode === 'edit' && props.visible) {
    if (message.action === 'undo') undo()
    else if (message.action === 'redo') redo()
    else if (message.action === 'clear') clearCanvasSelection()
    else if (message.action === 'pan-start') spaceHeld.value = true
    else if (message.action === 'pan-end') { spaceHeld.value = false; endPan() }
    else if (message.action === 'select-tool') state.canvasTool = 'select'
    else if (message.action === 'pan-tool') state.canvasTool = 'pan'
    else if (message.action === 'fit') void fitCanvas()
    else if (message.action === 'focus') void focusSelection()
  }
  else if (message.type === 'selected' && state.mode === 'edit' && props.visible) {
    const changed = state.selected?.selector !== message.element?.selector
    if (changed) cancelTransform(state)
    state.selected = message.element || null
    if (changed) { clearAiTimeout(state); state.aiPhase = 'idle'; state.aiProposal = null; state.aiNotice = ''; state.aiInstruction = '' }
    refreshDraft()
  } else if (message.type === 'navigate' && props.visible) { void navigatePreview(message.href) }
  else if (message.type === 'error') {
    const text = String(message.message || '页面运行遇到问题。').slice(0, 600)
    const path = message.resource_path || message.filename || ''
    const location = path ? `${path}${message.line ? `:${message.line}${message.column ? `:${message.column}` : ''}` : ''}` : ''
    const key = `${message.error_kind || ''}:${location}:${text}`
    if (!state.pageErrors.some(item => item.key === key)) state.pageErrors = [...state.pageErrors, { key, message: text, kind: message.error_kind || '', path, location }].slice(-5)
    const first = state.pageErrors[0]
    state.pageError = first ? `${first.message}${first.kind === 'runtime' && first.location ? `（${first.location}）` : ''}` : text
  }
}
function recordRules(next) {
  const state = active.value
  cancelTransform(state)
  if (!state || JSON.stringify(next) === JSON.stringify(state.rules)) return false
  state.rules = clone(next)
  state.history = [...state.history.slice(0, state.historyIndex + 1), clone(next)].slice(-50)
  state.historyIndex = state.history.length - 1
  state.aiProposal = null; state.notice = '样式已应用到预览，保存后才会写入项目。'
  persistLocal(state); pushStyles(); refreshDraft()
  return true
}
function applyDeclarations(declarations) {
  const state = active.value
  if (!state?.selected || state.mode !== 'edit') return false
  try {
    const normalized = normalizeStyleDeclarations(declarations)
    const requested = Object.entries(declarations || {}).filter(([, value]) => String(value || '').trim())
    if (!requested.length || requested.some(([property, value]) => normalized[property] !== String(value).trim())) {
      state.notice = '样式包含不支持的属性或值，请使用有效的颜色、字号、尺寸或布局值。'
      return false
    }
    const next = clone(state.rules), index = next.findIndex(rule => rule.selector === state.selected.selector)
    if (index < 0 && next.length >= 256) { state.notice = '已达到 256 组元素样式，请先重置不需要的元素。'; return false }
    const merged = { ...(index >= 0 ? next[index].declarations : {}), ...normalized }
    if (index >= 0) next[index].declarations = merged
    else next.push({ selector: state.selected.selector, declarations: merged })
    return recordRules(next)
  } catch (error) { state.notice = error.message || '样式包含不支持的值，请调整后重试。'; return false }
}
function applyStyleDraft() {
  const state = active.value
  if (!state?.selected) return
  const own = state.rules.find(rule => rule.selector === state.selected.selector)?.declarations || {}
  const declarations = Object.fromEntries(STYLE_FIELDS.map(field => field.property).filter(property => String(state.styleDraft[property] || '').trim() && (state.styleDraft[property] !== state.selected.styles?.[property] || own[property])).map(property => [property, state.styleDraft[property]]))
  if (!Object.keys(declarations).length) { state.notice = '请先修改左侧的样式属性。'; return }
  applyDeclarations(declarations)
}
function resetSelected() { if (active.value?.mode === 'edit' && selected.value) recordRules(rules.value.filter(rule => rule.selector !== selected.value.selector)) }
function resetProperty(property) {
  if (active.value?.mode !== 'edit' || !selected.value || !STYLE_FIELDS.some(field => field.property === property)) return false
  const next = clone(rules.value), index = next.findIndex(rule => rule.selector === selected.value.selector)
  if (index < 0 || !Object.hasOwn(next[index].declarations, property)) return false
  delete next[index].declarations[property]
  if (!Object.keys(next[index].declarations).length) next.splice(index, 1)
  return recordRules(next)
}
function resetRule(selector) {
  if (active.value?.mode !== 'edit' || typeof selector !== 'string' || active.value.saving) return false
  return recordRules(rules.value.filter(rule => rule.selector !== selector))
}
function changeProperty({ property, value } = {}) { return String(value ?? '').trim() ? applyDeclarations({ [property]: value }) : resetProperty(property) }
function undo() { const state = active.value; cancelTransform(state); if (!state || state.historyIndex <= 0) return; state.rules = clone(state.history[--state.historyIndex]); state.aiProposal = null; state.notice = serializeDesignCss(state.rules) === state.savedCss ? '已恢复已保存样式。' : '已撤销上一步样式，当前预览尚未保存。'; persistLocal(state); pushStyles(); refreshDraft() }
function redo() { const state = active.value; cancelTransform(state); if (!state || state.historyIndex >= state.history.length - 1) return; state.rules = clone(state.history[++state.historyIndex]); state.aiProposal = null; state.notice = serializeDesignCss(state.rules) === state.savedCss ? '已恢复已保存样式。' : '已重做样式，当前预览尚未保存。'; persistLocal(state); pushStyles(); refreshDraft() }
async function saveStyles() {
  const state = active.value
  if (!state || !state.preview || state.saving || state.transformGesture || props.readonly || dirtySource.value || state.stylesheetUnsupported || state.conflict || !dirty.value) return
  const css = serializeDesignCss(state.rules), projectId = state.projectId, entryPath = state.loadedEntryPath, channel = state.channel
  state.saving = true; state.notice = ''
  try {
    const result = await api.code.saveDesignStyles(projectId, { entry_path: entryPath, css, expected_sha1: state.sha1, expected_entry_sha1: state.entrySha1 })
    if (disposed || state.channel !== channel) return
    state.savedCss = css; state.sha1 = result.sha1; state.entrySha1 = result.entry_sha1 || state.entrySha1; state.stylesheetPath = result.path || state.stylesheetPath
    const warning = typeof result.warning === 'string' ? result.warning.trim().slice(0, 1000) : ''
    state.notice = (serializeDesignCss(state.rules) === css ? '样式已保存到项目。' : '上一版样式已保存，当前预览还有新修改。') + (warning ? ` ${warning}` : '')
    persistLocal(state)
    if (current(state)) emit('saved', { path: state.stylesheetPath, entry_path: state.loadedEntryPath, ...(warning ? { warning } : {}) })
  } catch (error) {
    if (disposed || state.channel !== channel) return
    const conflict = error.status === 409 || error.statusCode === 409 || /409|conflict|外部|冲突/i.test(error.message || '')
    state.conflict = conflict
    state.notice = conflict ? '保存冲突：页面或样式已被外部修改。草稿已保留，请重新载入最新页面后调整。' : error.message || '保存失败，草稿已保留。'
    persistLocal(state)
    if (current(state)) emit('error', state.notice)
  } finally { state.saving = false }
}
function requestAi() {
  const state = active.value
  if (!state?.selected || state.mode !== 'edit' || !state.aiInstruction.trim() || state.aiPhase === 'waiting' || state.loading || state.saving) return
  clearAiTimeout(state)
  const requestId = identifier()
  state.pendingAi = { requestId, projectId: state.projectId, selector: state.selected.selector, channel: state.channel, instruction: state.aiInstruction.trim().slice(0, 2000) }
  state.aiProposal = null; state.aiPhase = 'waiting'; state.aiNotice = '已将设计要求交给 AI 助手，结果返回后可对照并应用。'; persistLocal(state)
  const timerId = setTimeout(() => {
    timers.delete(timerId)
    if (state.aiTimer !== timerId) return
    state.aiTimer = null
    if (!disposed && state.pendingAi?.requestId === requestId && state.aiPhase === 'waiting') { state.aiPhase = 'idle'; state.aiNotice = '暂未收到样式候选，可重试，或继续手动调整样式。' }
  }, 180000)
  state.aiTimer = timerId; timers.add(timerId)
  emit('ask', createStyleRequest({ projectId: state.projectId, requestId, element: clone(state.selected), instruction: state.pendingAi.instruction }))
}
function clearAiTimeout(state) { if (state?.aiTimer != null) { clearTimeout(state.aiTimer); timers.delete(state.aiTimer); state.aiTimer = null } }
function dismissAi() {
  const state = active.value
  if (!state) return
  clearAiTimeout(state); state.pendingAi = null; state.aiProposal = null; state.aiPhase = 'idle'; state.aiNotice = '已取消本次样式候选。'; persistLocal(state)
}
function receiveAi(result) {
  const state = active.value
  if (!state?.pendingAi || !result || (result.projectId || result.project_id) !== state.projectId) return
  const text = typeof result.text === 'string' ? result.text : typeof result.content === 'string' ? result.content : ''
  const parsed = parseStyleResult(text)
  if (!parsed) {
    if (text.slice(0, 20000).includes(state.pendingAi.requestId)) { clearAiTimeout(state); state.aiPhase = 'idle'; state.aiNotice = '返回的样式格式或属性不受支持，请在 AI 助手重新生成。' }
    return
  }
  if (parsed.request_id !== state.pendingAi.requestId || parsed.selector !== state.pendingAi.selector) return
  clearAiTimeout(state)
  if (state.mode !== 'edit' || state.selected?.selector !== parsed.selector || state.pendingAi.channel !== state.channel) { state.aiPhase = 'idle'; state.aiNotice = 'AI 结果对应先前选中的元素，请重新选中后发起设计。'; return }
  state.aiProposal = parsed; state.aiPhase = 'ready'; state.aiNotice = ''
}
function applyAi(chosen) {
  const state = active.value
  if (!state?.aiProposal || !state.pendingAi || state.selected?.selector !== state.aiProposal.selector || state.pendingAi.channel !== state.channel || state.pendingAi.projectId !== state.projectId || state.loading || state.saving) return
  const declarations = chosen === undefined ? state.aiProposal.declarations : chosen
  if (!declarations || typeof declarations !== 'object' || Array.isArray(declarations) || !Object.keys(declarations).length || Object.entries(declarations).some(([property, value]) => !Object.hasOwn(state.aiProposal.declarations, property) || state.aiProposal.declarations[property] !== value)) { state.aiNotice = '请选择候选中需要应用的属性。'; return }
  if (applyDeclarations(declarations)) { clearAiTimeout(state); state.pendingAi = null; state.aiPhase = 'idle'; state.aiNotice = 'AI 样式已进入预览，请验收后保存。'; persistLocal(state) }
}
function savePreset(name) {
  const state = active.value, normalizedName = normalizeDesignPresetName(name)
  if (!state?.selected || state.mode !== 'edit' || state.loading || state.saving || !normalizedName) return false
  if (state.stylePresets.length >= MAX_DESIGN_PRESETS) { state.notice = `最多保存 ${MAX_DESIGN_PRESETS} 个项目样式，请先删除不需要的样式。`; return false }
  if (state.stylePresets.some(item => item.name.toLocaleLowerCase() === normalizedName.toLocaleLowerCase())) { state.notice = '已有同名项目样式，请换一个名称。'; return false }
  const preset = createDesignPreset({ id: identifier(), name: normalizedName, declarations: selectedOverrides.value })
  if (!preset) { state.notice = '请先修改当前元素的属性，再保存为项目样式。'; return false }
  state.stylePresets.push(preset); persistLocal(state); state.notice = '项目样式已保存在当前浏览器，可应用到其他元素。'
  return true
}
function applyPreset(id) {
  const state = active.value, preset = state?.stylePresets.find(item => item.id === id)
  if (!preset || !state.selected || state.mode !== 'edit' || state.loading || state.saving) return false
  return applyDeclarations(preset.declarations)
}
function removePreset(id) { const state = active.value; if (!state || state.loading || state.saving) return; state.stylePresets = state.stylePresets.filter(item => item.id !== id); persistLocal(state) }
function exportCss() {
  const state = active.value
  if (!state || !styleChanges.value.exportable) return false
  try { downloadDesignText({ filename: 'code-agent-design.css', text: styleChanges.value.currentCss, mime: 'text/css' }); state.notice = '已导出当前预览的 CSS 样式。'; return true }
  catch (error) { state.notice = error.message || '导出失败，请重试。'; return false }
}
async function copyCss() {
  const state = active.value, channel = state?.channel
  if (!state || !styleChanges.value.exportable) return false
  try {
    if (!window.navigator?.clipboard?.writeText) throw new Error('当前浏览器无法直接复制，请使用「导出 CSS」。')
    await window.navigator.clipboard.writeText(styleChanges.value.currentCss)
    if (current(state) && state.channel === channel) state.notice = '已复制当前预览的 CSS 样式。'
    return true
  } catch (error) { if (current(state) && state.channel === channel) state.notice = error.message || '复制失败，请使用「导出 CSS」。'; return false }
}
function exportChecks() {
  const state = active.value
  if (!state) return false
  const device = state.customViewport ? `${state.customViewport.width} × ${state.customViewport.height}` : ({ fluid: '自适应', tablet: '平板', phone: '手机' }[state.device] || '自适应')
  try { downloadDesignText({ filename: 'code-agent-acceptance.md', text: formatAcceptanceReport({ checks: state.checks, entryPath: state.loadedEntryPath || state.entryPath, device }), mime: 'text/markdown' }); state.notice = '已导出当前项目的验收报告。'; return true }
  catch (error) { state.notice = error.message || '导出验收报告失败，请重试。'; return false }
}
function addCheck(value = active.value?.checkDraft) {
  const state = active.value, title = typeof value === 'string' ? value.trim().slice(0, 240) : ''
  if (!state || !title || state.checks.length >= 100) return false
  if (state.checks.some(item => item.title.toLocaleLowerCase() === title.toLocaleLowerCase())) { state.notice = '已有相同验收项。'; return false }
  state.checks.push({ id: identifier(), title, state: 'pending', note: '' }); state.checkDraft = ''; persistLocal(state)
  return true
}
function addChecks(titles) {
  const state = active.value
  if (!state || !Array.isArray(titles)) return 0
  const existing = new Set(state.checks.map(item => item.title.toLocaleLowerCase())), next = []
  for (const value of titles.slice(0, 100)) {
    const title = typeof value === 'string' ? value.trim().slice(0, 240) : '', key = title.toLocaleLowerCase()
    if (!title || existing.has(key) || state.checks.length + next.length >= 100) continue
    next.push({ id: identifier(), title, state: 'pending', note: '' }); existing.add(key)
  }
  if (next.length) { state.checks.push(...next); persistLocal(state) }
  return next.length
}
function updateCheck(patch) {
  const state = active.value, item = state?.checks.find(item => item.id === patch?.id)
  if (!item || !patch || typeof patch !== 'object') return false
  if (Object.hasOwn(patch, 'title')) { if (typeof patch.title !== 'string' || !patch.title.trim()) return false; item.title = patch.title.trim().slice(0, 240) }
  if (typeof patch.note === 'string') item.note = patch.note.slice(0, 600)
  persistLocal(state); return true
}
function changeCheckState({ id, state: status } = {}) {
  const item = active.value?.checks.find(item => item.id === id)
  if (item) setCheckState(item, status)
}
function setCheckState(item, status) { if (!active.value || !['pending', 'passed', 'failed'].includes(status)) return; item.state = status; persistLocal(active.value) }
function removeCheck(id) { const state = active.value; if (!state) return; state.checks = state.checks.filter(item => item.id !== id); persistLocal(state) }
function discardAndReload() {
  const state = active.value
  if (!state || state.loading || state.saving) return
  state.recoveredDraft = null
  return loadPreview(state, { discardDraft: true })
}
watch(() => props.projectId, projectId => activate(projectId), { immediate: true })
watch(() => props.visible, visible => {
  if (!visible) { cancelTransform(); syncInteraction(); return }
  if (!active.value) return
  if (!active.value.loaded && !active.value.loading) loadInfo(active.value)
  else if (active.value.preview && !active.value.bridgeReady) frameLoaded()
  else if (active.value.bridgeReady) { post('mode', { mode: active.value.mode }); pushStyles() }
})
watch(() => props.aiResult, receiveAi)
watch(() => [props.visible, props.projectId, active.value?.projectId, active.value?.mode, active.value?.loading, active.value?.saving, active.value?.bridgeReady, active.value?.canvasTool, spaceHeld.value, panning.value, zoomLevel.value], syncInteraction)
watch(() => [props.visible, props.projectId, active.value?.channel, active.value?.mode, active.value?.loading, active.value?.saving, active.value?.device, active.value?.customViewport], () => { cancelWheelZoom(); endPan() })
watch(canvasViewport, element => {
  cancelWheelZoom(); endPan()
  canvasObserver?.disconnect(); canvasObserver = null
  if (!element) return
  measureCanvas()
  if (typeof globalThis.ResizeObserver === 'function') { canvasObserver = new globalThis.ResizeObserver(() => { updateViewportMode(); measureCanvas() }); canvasObserver.observe(element); if (workspaceEl.value) canvasObserver.observe(workspaceEl.value) }
})
watch(() => props.visible, visible => { if (!visible) { spaceHeld.value = false; endPan() } else measureCanvas() })
onMounted(() => { updateViewportMode(); window.addEventListener('resize', updateViewportMode); window.addEventListener('message', receive); window.addEventListener('keydown', onDesignKey); window.addEventListener('keyup', releaseSpace); window.addEventListener('blur', releaseSpace); window.addEventListener('pointermove', movePan); window.addEventListener('pointerup', endPan); window.addEventListener('pointercancel', endPan) })
onBeforeUnmount(() => { cancelWheelZoom(); cancelTransform(); endPan(); disposed = true; canvasObserver?.disconnect(); window.removeEventListener('resize', updateViewportMode); window.removeEventListener('message', receive); window.removeEventListener('keydown', onDesignKey); window.removeEventListener('keyup', releaseSpace); window.removeEventListener('blur', releaseSpace); window.removeEventListener('pointermove', movePan); window.removeEventListener('pointerup', endPan); window.removeEventListener('pointercancel', endPan); for (const controller of readControllers) controller.abort(); readControllers.clear(); for (const timer of timers) clearTimeout(timer); timers.clear(); for (const state of contexts.values()) persistLocal(state) })
</script>

<template>
  <section ref="workspaceEl" class="frontend-design" aria-label="前端设计" :class="{ 'panel-hidden': !panelOpen }">
    <div class="source-bar" v-if="active">
      <label class="entry-label"><UiIcon name="file-code" :size="15" /><select v-model="active.entryPath" aria-label="项目页面入口" :disabled="active.loading || active.saving || transformInProgress"><option value="">选择页面入口</option><option v-for="entry in active.entries" :key="entry.path" :value="entry.path">{{ entry.label || entry.path }}{{ entry.runtime?.requires_dev_server ? ' · 源码入口' : '' }}</option></select></label>
      <label class="url-label"><UiIcon name="globe" :size="15" /><input v-model="active.url" aria-label="前端开发服务地址" placeholder="开发服务地址，例如 http://127.0.0.1:5173" :disabled="active.loading || active.saving || transformInProgress" @keydown.enter.prevent="loadPreview()" /></label>
      <button type="button" :disabled="active.loading || active.saving || !active.entryPath" @click="loadPreview()"><UiIcon name="refresh" :size="14" />载入</button>
      <div class="device-switch" role="group" aria-label="预览宽度"><button type="button" :class="{ active: active.device === 'fluid' }" :aria-pressed="active.device === 'fluid'" @click="setDevice('fluid')">自适应</button><button type="button" :class="{ active: active.device === 'tablet' }" :aria-pressed="active.device === 'tablet'" @click="setDevice('tablet')">平板</button><button type="button" :class="{ active: active.device === 'phone' }" :aria-pressed="active.device === 'phone'" @click="setDevice('phone')">手机</button><DesignViewport :key="active.projectId" :width="frameSize.width" :height="frameSize.height" :custom="active.device === 'custom'" :disabled="active.loading || active.saving || transformInProgress" @change="setViewport" /></div>
      <div v-show="visible" class="design-toolbar" role="group" aria-label="页面操作">
        <span class="design-status" role="status" :title="statusLabel" :class="{ dirty, conflict: active?.conflict }">{{ statusLabel }}</span>
        <div class="header-actions">
          <div class="mode-switch" role="group" aria-label="页面模式"><button type="button" :class="{ active: active?.mode === 'acceptance' }" :aria-pressed="active?.mode === 'acceptance'" @click="setMode('acceptance')"><UiIcon name="audit" :size="15" />验收</button><button type="button" :class="{ active: active?.mode === 'edit' }" :aria-pressed="active?.mode === 'edit'" @click="setMode('edit')"><UiIcon name="edit" :size="15" />编辑</button></div>
          <button type="button" class="icon-button" title="撤销样式" aria-label="撤销样式" :disabled="!undoAvailable || active?.saving" @click="undo"><UiIcon name="undo" :size="17" /></button><button type="button" class="icon-button" title="重做样式" aria-label="重做样式" :disabled="!redoAvailable || active?.saving" @click="redo"><UiIcon name="redo" :size="17" /></button>
          <button type="button" class="save-button" :disabled="!dirty || readonly || dirtySource || !active?.preview || active?.saving || transformInProgress || active?.conflict || active?.stylesheetUnsupported" @click="saveStyles"><UiIcon name="check" :size="15" />{{ active?.saving ? '保存中…' : '保存样式' }}</button>
        </div>
      </div>
    </div>
    <div class="api-bar" v-if="active">
      <label class="api-url-label"><span>项目接口</span><input v-model="active.apiUrl" aria-label="项目接口服务地址" placeholder="可选，例如 http://127.0.0.1:8080/api/" :disabled="active.loading || active.saving || transformInProgress" @keydown.enter.prevent="loadPreview()" /></label>
      <button type="button" :disabled="active.loading || active.saving || !active.entryPath || !active.apiUrl.trim()" @click="loadPreview()">连接接口</button>
      <button type="button" v-if="suggestedApi && !active.apiUrl" :disabled="active.loading || active.saving || transformInProgress" :title="suggestedApi" @click="connectSuggestedApi">使用建议接口</button>
      <button type="button" v-if="active.preview?.api_proxy_url" :disabled="active.loading || active.saving || transformInProgress" @click="disconnectApi">断开接口</button>
      <span class="api-help">{{ active.preview?.api_proxy_url ? '已配置项目接口 · 只读 GET/HEAD' : '页面数据需要项目接口；连接后只读 GET/HEAD' }}</span>
    </div>
    <div class="design-body" v-if="active" :class="{ 'editing-workspace': active.mode === 'edit' }">
      <DesignLayers v-if="active.mode === 'edit' && layersOpen" class="layer-tree-panel" :layers="active.layers || []" :selected-selector="selected?.selector || ''" :loading="active.layersLoading" :truncated="active.layersTruncated" :disabled="!active.bridgeReady || active.loading || active.saving || transformInProgress" @select="selectLayer" @refresh="refreshLayers" />
      <nav v-if="active.mode === 'acceptance'" class="tool-rail" aria-label="设计工具">
        <button type="button" class="rail-toggle" title="显示或收起属性面板" aria-label="显示或收起属性面板" :aria-expanded="panelOpen" @click="panelOpen = !panelOpen"><UiIcon name="panel-left" :size="19" /></button>
        <button type="button" class="active" title="功能验收" aria-label="功能验收" @click="panelOpen = true"><UiIcon name="audit" :size="20" /></button>
      </nav>
      <aside class="properties-panel" :class="{ 'acceptance-panel': active.mode === 'acceptance' }" v-show="panelOpen" :aria-label="active.mode === 'edit' ? '元素属性' : '功能验收'">
        <template v-if="active.mode === 'acceptance'">
          <DesignAcceptance :key="active.projectId" :checks="active.checks" :disabled="active.loading || active.saving || transformInProgress" @add="addCheck" @add-batch="addChecks" @update="updateCheck" @state="changeCheckState" @remove="removeCheck" @export-report="exportChecks" />
        </template>
        <template v-else>
          <div class="inspector-tabs" role="group" aria-label="属性面板"><button type="button" :aria-pressed="!['ai', 'changes'].includes(group)" :class="{ active: !['ai', 'changes'].includes(group) }" @click="group = 'appearance'">设计</button><button type="button" :aria-pressed="group === 'ai'" :class="{ active: group === 'ai' }" @click="group = 'ai'"><UiIcon name="sparkles" :size="13" />AI 样式</button><button type="button" :aria-pressed="group === 'changes'" :class="{ active: group === 'changes' }" @click="group = 'changes'">改动{{ styleChanges.elementCount ? ` ${styleChanges.elementCount}` : '' }}</button><button type="button" class="inspector-close" title="收起属性面板" aria-label="收起属性面板" @click="panelOpen = false"><UiIcon name="close" :size="13" /></button></div>
          <div v-if="group !== 'changes'" class="panel-heading"><strong>{{ group === 'ai' ? 'AI 设计' : '元素属性' }}</strong></div>
          <div v-if="selected && group !== 'changes'" class="selection-info"><code :title="selected.selector">{{ selected.selector }}</code><p v-if="selected.text">{{ selected.text }}</p></div>
          <nav v-if="selectionAncestors.length && group !== 'changes'" class="selection-breadcrumbs" aria-label="元素层级"><button v-for="ancestor in selectionAncestors.slice(-4)" :key="ancestor.selector" type="button" :title="ancestor.selector" @click="selectLayer(ancestor.selector)">{{ ancestor.tag }}</button><span>{{ selected?.tag }}</span></nav>
          <template v-if="!['ai', 'changes'].includes(group)">
            <DesignInspector class="element-inspector" :selected="selected" :draft="active.styleDraft" :overrides="selectedOverrides" :disabled="!active.bridgeReady || active.loading || active.saving || transformInProgress" @change="changeProperty" @reset="resetProperty" @reset-all="resetSelected" />
          </template>
          <template v-else-if="group === 'ai'">
            <DesignAssist :key="active.projectId" :selected="selected" :instruction="active.aiInstruction" :proposal="active.aiProposal" :diff="aiDiff" :notice="active.aiNotice" :busy="aiBusy" :disabled="active.loading || active.saving || transformInProgress" :presets="active.stylePresets" :can-save-preset="!!selected && Object.keys(selectedOverrides).length > 0" @update:instruction="active.aiInstruction = $event" @request="requestAi" @apply="applyAi" @dismiss="dismissAi" @save-preset="savePreset" @apply-preset="applyPreset" @remove-preset="removePreset" />
          </template>
          <DesignChanges v-else class="element-changes" :rules="rules" :saved-css="active.savedCss" :disabled="active.loading || active.saving || transformInProgress" @select="selector => { selectLayer(selector); group = 'appearance' }" @reset="resetRule" @export-css="exportCss" @copy-css="copyCss" />
          <p class="panel-footnote">修改先作用于预览，保存样式后写入项目。元素结构或路由变化后，可重新选择。</p>
        </template>
      </aside>
      <main class="design-canvas" aria-label="前端页面画布">
        <div class="canvas-caption">
          <div v-if="active.mode === 'edit'" class="canvas-selection-tools" role="group" aria-label="画布工具"><button type="button" :aria-pressed="layersOpen" title="显示或收起图层" @click="toggleLayers"><UiIcon name="layers" :size="14" />图层</button><button type="button" :aria-pressed="active.canvasTool === 'select'" :class="{ active: active.canvasTool === 'select' }" title="选择工具 · V" @click="active.canvasTool = 'select'">选择</button><button type="button" :aria-pressed="active.canvasTool === 'pan'" :class="{ active: active.canvasTool === 'pan' }" title="平移工具 · H，或按住空格拖动" @click="active.canvasTool = 'pan'">平移</button><button type="button" :aria-pressed="panelOpen" @click="toggleProperties">属性</button></div>
          <span v-else>验收模式 · 页面可交互</span>
          <div v-if="active.preview" class="canvas-zoom" role="group" aria-label="画布缩放"><button type="button" :disabled="zoomLevel <= 25" aria-label="缩小画布" @click="setZoom(stepZoom(zoomLevel, -1))">缩小</button><select :value="zoomLevel" aria-label="画布缩放比例" @change="setZoom(Number($event.target.value))"><option v-if="!zoomPresets.includes(zoomLevel)" :value="zoomLevel">{{ Math.round(zoomLevel) }}%</option><option v-for="value in zoomPresets" :key="value" :value="value">{{ value }}%</option></select><button type="button" :disabled="zoomLevel >= 200" aria-label="放大画布" @click="setZoom(stepZoom(zoomLevel, 1))">放大</button><button type="button" title="适应画布 · Shift+1" @click="fitCanvas">适应</button><button type="button" title="实际尺寸" @click="setZoom(100)">1:1</button><button v-if="active.mode === 'edit'" type="button" :disabled="!selected" title="聚焦选中元素 · Shift+2" @click="focusSelection">选中</button></div>
          <details v-if="active.mode === 'edit'" class="canvas-help" @keydown.esc.prevent.stop="$event.currentTarget.open = false; endPan(); cancelTransform()">
            <summary>快捷键</summary>
            <div class="canvas-help-popover" aria-label="画布快捷键说明">
              <strong>画布操作</strong>
              <dl><dt>V / H</dt><dd>选择 / 平移工具</dd><dt>空格 + 拖动 / 中键拖动</dt><dd>平移画布</dd><dt>Ctrl / ⌘ + 滚轮</dt><dd>以指针为中心缩放</dd><dt>Shift + 1 / 2</dt><dd>适应画布 / 聚焦选中元素</dd><dt>Ctrl / ⌘ + Z</dt><dd>撤销样式</dd><dt>Ctrl / ⌘ + Shift + Z</dt><dd>重做样式</dd><dt>Esc</dt><dd>取消调整、结束平移或清除选择</dd></dl>
              <p>数字属性输入框内：↑ / ↓ 微调，Shift 加速，Alt 精调。</p>
            </div>
          </details>
        </div>
        <div v-if="active.notice || active.conflict || active.stylesheetUnsupported || readonly || dirtySource" class="design-notice" role="status"><span>{{ dirtySource ? '入口或设计样式文件在编辑器中有未保存修改，请先保存编辑器草稿。' : active.stylesheetUnsupported ? '现有设计样式文件含非受控内容，为保护原样式暂不能覆盖保存。' : active.notice || (readonly ? '当前只读，可预览样式；保存需要可写权限。' : '') }}</span><button v-if="dirty || active.conflict" type="button" :disabled="active.loading || active.saving || transformInProgress" @click="discardAndReload">放弃草稿并重载</button></div>
        <div v-if="active.loadError" class="canvas-state error-state" role="alert"><UiIcon name="warning" :size="30" /><strong>页面载入失败</strong><p>{{ active.loadError }}</p><button type="button" :disabled="active.loading" @click="retry">重试</button></div>
        <div v-else-if="active.serviceRequired" class="canvas-state service-state" role="status"><UiIcon name="globe" :size="30" /><strong>此页面需要前端开发服务</strong><p>{{ entryRuntime?.reason || '所选页面引用未编译源码，需要由项目的前端服务编译后预览。' }}</p><dl v-if="entryRuntime?.source_entry || entryRuntime?.command"><template v-if="entryRuntime?.source_entry"><dt>源码入口</dt><dd><code>{{ entryRuntime.source_entry }}</code></dd></template><template v-if="entryRuntime?.command"><dt>运行目录</dt><dd><code>{{ entryRuntime.working_dir || '.' }}</code></dd><dt>启动命令</dt><dd><code>{{ entryRuntime.command }}</code></dd></template></dl><p>在项目终端启动服务，填写上方地址并载入；也可选择已构建的 HTML 页面。</p><button v-if="suggestedService" type="button" class="primary" :disabled="active.loading || active.saving || transformInProgress" @click="connectSuggestedService">连接开发服务</button><code v-if="suggestedService" class="service-address">{{ suggestedService }}</code><p v-if="suggestedService" class="service-footnote">地址来自项目配置，连接时会检查服务是否可用。</p></div>
        <div v-else-if="active.loading && !active.preview" class="canvas-state" role="status"><span class="design-spinner" /><strong>正在读取项目页面…</strong></div>
        <div v-else-if="!active.preview" class="canvas-state"><UiIcon name="code" :size="32" /><strong>{{ active.loaded && !active.entries.length ? '当前项目没有 HTML 入口' : '载入你的前端页面' }}</strong><p>{{ active.loaded && !active.entries.length ? '请先在项目中创建或加入前端 index.html，再重新读取入口。' : '选择项目 HTML 入口；Vue、React 等项目还需填写已运行的前端服务地址。' }}</p><p>画布展示实际页面，保存样式会关联到所选 HTML 入口。</p><button type="button" @click="loadInfo()">重新读取页面入口</button></div>
        <div v-else class="canvas-viewport" @wheel="onCanvasWheel"><div ref="canvasViewport" class="canvas-scroll" :class="{ 'hand-tool': panToolActive, 'is-panning': panning }" tabindex="0" aria-label="可缩放画布" @pointerdown="beginPan" @pointermove="movePan" @pointerup="endPan" @pointercancel="endPan" @lostpointercapture="endPan"><div class="canvas-stage" :style="canvasStageStyle"><div class="page-frame" :class="active.device" :style="canvasStyle"><div class="page-frame-heading"><span class="window-dots"><i /><i /><i /></span><span>{{ sourceLabel }} · {{ active.loadedEntryPath || active.loadedUrl }}</span><span>{{ active.mode === 'edit' ? '选择元素' : '交互测试' }}</span></div><iframe ref="iframeEl" :key="active.channel" :srcdoc="active.document" sandbox="allow-scripts allow-forms allow-modals" referrerpolicy="no-referrer" title="前端设计预览" @load="frameLoaded" /></div></div></div><div v-if="panToolActive || panning" ref="panShieldEl" class="canvas-pan-shield" :class="{ dragging: panning }" aria-label="拖动画布平移" @pointerdown="beginPan" @pointermove="movePan" @pointerup="endPan" @pointercancel="endPan" @lostpointercapture="endPan" /></div>
        <div v-if="active.loading && active.preview" class="canvas-loading" role="status"><span class="design-spinner" />正在重新载入页面…</div>
        <div v-if="active.pageError || active.bridgeError" class="bridge-notice" role="alert"><span>{{ active.pageError || active.bridgeError }}</span><details v-if="active.pageErrors.length > 1"><summary>查看 {{ active.pageErrors.length }} 项页面错误</summary><ul><li v-for="item in active.pageErrors" :key="item.key">{{ item.message }}<code v-if="item.kind === 'runtime' && item.location">（{{ item.location }}）</code></li></ul></details></div>
      </main>
    </div>
    <div v-else class="canvas-state"><p>打开项目后可使用前端设计。</p></div>
    <footer class="design-footer"><span><i :class="{ connected: active?.bridgeReady }" />{{ active?.bridgeReady ? '画布已连接' : '画布未连接' }}</span><span>{{ rules.length }} 组样式修改</span><span v-if="active?.mode === 'edit'">{{ (active.layers || []).length }} 图层 · {{ frameSize.width }} × {{ frameSize.height }} · {{ Math.round(zoomLevel) }}%</span><span v-if="active?.mode === 'edit'" class="canvas-key-hint" title="V 选择 · H/空格平移 · Esc 取消调整">拖动移动 · 边角缩放 · Esc 取消</span><code v-if="active?.stylesheetPath" :title="active.stylesheetPath">{{ active.stylesheetPath }}</code></footer>
  </section>
</template>

<style scoped>
.frontend-design { height: 100%; min-height: 0; min-width: 0; display: flex; flex-direction: column; background: var(--panel); color: var(--text); font-size: var(--fs-xs, 12px); overflow: hidden; }
.frontend-design button { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 29px; padding: 5px 9px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--text-2, var(--text)); font-size: var(--fs-xs, 12px); cursor: pointer; white-space: nowrap; }
.frontend-design button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }.frontend-design button:disabled { opacity: .45; cursor: default; }.frontend-design button:focus-visible,.frontend-design input:focus-visible,.frontend-design select:focus-visible,.frontend-design textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.design-status { color: var(--muted); font-size: var(--fs-2xs, 11px); }.design-status.dirty { color: var(--accent); }.design-status.conflict { color: var(--warn, #b78338); }.header-actions { display: flex; align-items: center; gap: 4px; flex: none; }.mode-switch,.device-switch { display: inline-flex; padding: 3px; gap: 2px; border: 1px solid var(--border); background: var(--panel-soft); border-radius: 6px; }.mode-switch button,.device-switch button { border-color: transparent; background: transparent; }.mode-switch button.active,.device-switch button.active { background: var(--panel); color: var(--accent); border-color: var(--border); }.frontend-design .icon-button { padding: 5px; min-width: 28px; }.frontend-design .save-button,.frontend-design .primary { background: var(--accent); color: var(--on-accent, #fff); border-color: var(--accent); }
.source-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--border); flex: none; background: var(--panel-soft); }.source-bar label { display: flex; align-items: center; gap: 6px; min-width: 0; }.source-bar label > svg { flex: none; color: var(--muted); }.entry-label { flex: 0 1 200px; max-width: 220px; }.url-label { flex: 0 1 320px; max-width: 360px; }.source-bar input,.source-bar select { width: 100%; min-width: 0; padding: 6px 8px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--text); font-size: var(--fs-xs, 12px); }.device-switch { flex: none; margin-left: 0; }.device-switch button { font-size: var(--fs-2xs, 11px); min-height: 25px; padding: 3px 7px; }
.design-body { display: flex; min-height: 0; min-width: 0; flex: 1; }.tool-rail { width: 46px; flex: none; display: flex; flex-direction: column; align-items: center; gap: 7px; padding: 9px 5px; border-right: 1px solid var(--border); background: var(--panel-soft); }.tool-rail button { width: 34px; height: 35px; border: 0; background: transparent; padding: 7px; }.tool-rail button.active { background: var(--accent-soft, var(--panel)); color: var(--accent); border: 1px solid var(--accent-border, var(--border)); }.rail-divider { width: 24px; border-top: 1px solid var(--border); margin: 4px; }.properties-panel { width: 254px; flex: none; overflow-y: auto; overscroll-behavior: contain; min-height: 0; padding: 13px 12px 16px; border-right: 1px solid var(--border); background: var(--panel); }.panel-heading { display: flex; justify-content: space-between; align-items: center; gap: 7px; margin-bottom: 13px; }.panel-heading span { color: var(--muted); font-size: var(--fs-2xs, 11px); }.panel-hint,.panel-empty,.panel-footnote { color: var(--muted); font-size: var(--fs-2xs, 11px); line-height: 1.65; margin: 8px 0 13px; }.panel-footnote { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border); }.selection-info { border: 1px solid var(--border); background: var(--panel-soft); padding: 9px; border-radius: 6px; margin-bottom: 15px; display: flex; flex-direction: column; gap: 5px; overflow-wrap: anywhere; }.selection-info strong { font-family: var(--font-mono, monospace); font-size: var(--fs-xs, 12px); color: var(--accent); }.selection-info code { font-size: var(--fs-2xs, 11px); max-height: 50px; overflow: auto; }.selection-info p { margin: 0; max-height: 55px; overflow: auto; font-size: var(--fs-2xs, 11px); }.selection-info > span { color: var(--muted); font-size: var(--fs-2xs, 11px); }.selection-empty { padding: 18px 10px; margin-bottom: 12px; border: 1px dashed var(--border-strong); border-radius: 6px; color: var(--muted); text-align: center; }.selection-empty svg { margin-bottom: 8px; color: var(--accent); }.selection-empty strong { display: block; color: var(--text); font-weight: 500; }.selection-empty p { line-height: 1.65; font-size: var(--fs-2xs, 11px); margin: 8px 0 0; }
.style-fields { display: flex; flex-direction: column; gap: 10px; }.style-fields label { display: grid; grid-template-columns: 76px minmax(0,1fr); align-items: center; gap: 8px; }.style-fields label > span { color: var(--muted); font-size: var(--fs-2xs, 11px); }.style-fields input,.check-form input,.check-item textarea,.ai-instruction textarea { width: 100%; min-width: 0; padding: 6px 7px; border: 1px solid var(--border); background: var(--panel); color: var(--text); border-radius: 4px; font-size: var(--fs-xs, 12px); box-sizing: border-box; }.style-fields input { font-family: var(--font-mono, monospace); font-size: var(--fs-2xs, 11px); }.style-fields input:disabled { opacity: .5; }.property-actions { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 16px; }.check-form { display: flex; gap: 5px; margin-bottom: 12px; }.check-form input { flex: 1; }.check-item { padding: 10px 9px; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 10px; }.check-item.passed { border-color: var(--success, #2f977a); }.check-item.failed { border-color: var(--error); }.check-title { display: flex; align-items: start; gap: 5px; line-height: 1.65; overflow-wrap: anywhere; }.check-title > span { flex: 1; }.check-title .icon-button { min-width: 20px; min-height: 20px; border: 0; padding: 3px; color: var(--muted); }.check-state { display: flex; flex-wrap: wrap; gap: 4px; margin: 8px 0; }.check-state button { padding: 3px 6px; min-height: 25px; font-size: var(--fs-2xs, 11px); }.check-state button.active { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-border, var(--accent)); }.check-item textarea { resize: vertical; font-size: var(--fs-2xs, 11px); line-height: 1.6; }.acceptance-summary { display: flex; flex-wrap: wrap; gap: 10px; padding-top: 6px; color: var(--muted); font-size: var(--fs-2xs, 11px); }.ai-instruction { display: flex; flex-direction: column; gap: 8px; color: var(--muted); }.ai-instruction textarea { resize: vertical; line-height: 1.65; }.ai-request { width: 100%; margin-top: 10px; }.ai-notice { font-size: var(--fs-2xs, 11px); line-height: 1.7; color: var(--accent); }.ai-proposal { padding: 10px; border: 1px solid var(--accent-border, var(--border)); border-radius: 6px; margin-top: 12px; background: var(--accent-soft); }.ai-proposal > p { margin: 7px 0 10px; font-size: var(--fs-2xs, 11px); overflow-wrap: anywhere; line-height: 1.65; }.ai-diff { display: flex; flex-direction: column; gap: 4px; padding: 8px 0; border-top: 1px solid var(--border); font-size: var(--fs-2xs, 11px); overflow-wrap: anywhere; }.ai-diff > span { color: var(--muted); text-decoration: line-through; }.ai-diff > b { color: var(--accent); font-weight: 500; }.ai-proposal button { width: 100%; margin-top: 10px; }
.design-canvas { position: relative; flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; background-color: var(--workspace-bg, var(--panel-soft)); background-image: radial-gradient(var(--border-strong) .7px, transparent .7px); background-size: 14px 14px; }.canvas-caption { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; padding: 10px 15px; flex: none; font-size: var(--fs-2xs, 11px); color: var(--muted); }.canvas-scroll { flex: 1; min-height: 0; overflow: auto; padding: 4px 18px 20px; display: flex; align-items: stretch; justify-content: safe center; }.page-frame { min-height: 300px; height: 100%; display: flex; flex-direction: column; flex: none; border: 1px solid var(--border-strong); border-radius: 8px 8px 3px 3px; background: #fff; box-shadow: 0 8px 24px #17233c14; overflow: hidden; }.page-frame.fluid { min-width: 240px; }.page-frame-heading { display: flex; align-items: center; gap: 10px; flex: none; height: 30px; padding: 0 10px; border-bottom: 1px solid var(--border); background: var(--panel); color: var(--muted); font-size: var(--fs-2xs, 11px); }.page-frame-heading > span:nth-child(2) { flex: 1; min-width: 0; text-overflow: ellipsis; white-space: nowrap; overflow: hidden; }.page-frame-heading > span:last-child { white-space: nowrap; }.window-dots { display: flex; align-items: center; gap: 4px; }.window-dots i { width: 6px; height: 6px; border-radius: 50%; background: var(--border-strong); }.page-frame iframe { width: 100%; border: 0; background: white; flex: 1; min-height: 270px; }.canvas-state { display: flex; flex: 1; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 30px 20px; min-height: 160px; text-align: center; color: var(--muted); }.canvas-state > strong { color: var(--text); font-size: var(--fs-sm, 13px); }.canvas-state > p { max-width: 470px; margin: 0; line-height: 1.7; overflow-wrap: anywhere; }.canvas-state > svg { color: var(--accent); }.error-state > svg { color: var(--error); }.design-notice { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 12px; margin: 0 12px 8px; border: 1px solid var(--accent-border, var(--border)); border-radius: 5px; background: var(--panel); color: var(--muted); font-size: var(--fs-2xs, 11px); line-height: 1.7; }.design-notice > span { flex: 1; min-width: 160px; }.design-notice button { font-size: var(--fs-2xs, 11px); }.bridge-notice { position: absolute; left: 12px; right: 12px; bottom: 10px; padding: 8px 12px; border: 1px solid var(--warn, var(--border)); border-radius: 5px; color: var(--warn); background: var(--panel); }.canvas-loading { position: absolute; top: 45px; left: 50%; transform: translateX(-50%); display: flex; gap: 8px; align-items: center; padding: 9px 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: var(--muted); box-shadow: 0 4px 12px #17233c15; }.design-spinner { display: inline-block; width: 17px; height: 17px; border: 2px solid var(--border-strong); border-top-color: var(--accent); border-radius: 50%; animation: design-spin .8s linear infinite; }.design-footer { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; min-height: 27px; padding: 5px 12px; border-top: 1px solid var(--border); background: var(--panel-soft); color: var(--muted); font-size: var(--fs-2xs, 11px); flex: none; }.design-footer > span { display: inline-flex; align-items: center; gap: 6px; }.design-footer i { width: 6px; height: 6px; border-radius: 50%; background: var(--border-strong); }.design-footer i.connected { background: var(--success, #2f977a); }.design-footer code { margin-left: auto; max-width: 45%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: inherit; }
.service-state { overflow: auto; }.service-state dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 16px; max-width: 480px; margin: 0; padding: 14px 18px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); text-align: left; }.service-state dt { color: var(--muted); }.service-state dd { min-width: 0; margin: 0; color: var(--text); overflow-wrap: anywhere; }.service-state code { font-size: inherit; }.service-state .service-footnote { font-size: var(--fs-2xs, 11px); }.service-address { color: var(--accent); overflow-wrap: anywhere; }.bridge-notice { overflow-wrap: anywhere; max-height: 35%; overflow: auto; font-size: var(--fs-xs, 12px); }.bridge-notice details { margin-top: 5px; }.bridge-notice summary { cursor: pointer; }.bridge-notice ul { padding-left: 20px; margin: 6px 0; }
.api-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 7px 12px; border-bottom: 1px solid var(--border); background: var(--panel-soft); flex: none; font-size: var(--fs-xs, 12px); }
.api-url-label { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1 1 300px; }
.api-url-label > span { color: var(--muted); flex: none; }
.api-url-label input { min-width: 0; width: 100%; border: 1px solid var(--border); border-radius: 5px; padding: 6px 8px; background: var(--panel); color: var(--text); font: inherit; }
.api-help { color: var(--muted); overflow-wrap: anywhere; font-size: var(--fs-2xs, 11px); }
@keyframes design-spin { to { transform: rotate(360deg); } }
.design-toolbar { display: flex; align-items: center; gap: 10px; flex: 0 1 auto; min-width: 0; max-width: 100%; margin-left: auto; padding-left: 10px; border-left: 1px solid var(--border); }
.design-toolbar .design-status { flex: none; max-width: 140px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.design-toolbar button { min-height: 28px; line-height: 18px; padding: 4px 8px; }
.design-toolbar .mode-switch { padding: 2px; }
.design-toolbar .icon-button { min-width: 28px; padding: 4px; }
@media (max-width: 850px) { .properties-panel { width: 220px; padding: 12px 10px; }.header-actions { margin-left: 0; }.design-header { gap: 8px 15px; }.design-status { font-size: 10px; }.device-switch { margin-left: 0; }.style-fields label { grid-template-columns: 68px minmax(0,1fr); gap: 5px; }.canvas-scroll { padding-left: 10px; padding-right: 10px; } }
@media (max-width: 560px) { .design-body { position: relative; }.properties-panel { position: absolute; z-index: 3; left: 46px; top: 0; bottom: 0; width: min(245px, calc(100% - 58px)); box-shadow: 8px 0 18px #17233c1c; }.frontend-design.panel-hidden .properties-panel { display: none; }.design-header { padding: 8px; }.source-bar { padding: 7px 8px; gap: 6px; }.entry-label,.url-label { flex: 1 1 calc(100% - 55px); }.source-bar .device-switch { margin-left: 0; }.design-status { max-width: 170px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.header-actions { gap: 4px; }.design-title { flex-wrap: wrap; }.canvas-caption { padding: 9px; }.canvas-scroll { padding: 2px 8px 12px; justify-content: flex-start; }.page-frame.fluid { min-width: 210px; }.design-footer { gap: 8px; padding: 5px 8px; }.design-footer code { display: none; }.page-frame-heading > span:last-child { display: none; }.mode-switch button { padding: 4px 7px; }.frontend-design .save-button { padding: 5px 7px; } }
@media (prefers-reduced-motion: reduce) { .design-spinner { animation: none; } }
.frontend-design { container: designer / inline-size; }
.design-body.editing-workspace { position: relative; }
.editing-workspace > .layer-tree-panel { order: 1; width: 210px; flex: none; min-width: 0; height: 100%; border-right: 1px solid var(--border); }
.editing-workspace > .design-canvas { order: 2; }
.editing-workspace > .properties-panel { order: 3; width: 272px; flex: none; border-right: 0; border-left: 1px solid var(--border); padding: 0 12px 16px; }
.inspector-tabs { display: flex; gap: 4px; align-items: center; padding: 10px 0; margin-bottom: 10px; border-bottom: 1px solid var(--border); }
.inspector-tabs button { min-height: 28px; padding: 4px 10px; border-color: transparent; background: transparent; color: var(--muted); }
.inspector-tabs button.active { background: var(--accent-soft); color: var(--accent); }
.inspector-tabs .inspector-close { margin-left: auto; padding: 4px; min-width: 24px; }
.properties-panel.acceptance-panel { padding: 0; overflow: hidden; }
.element-changes { height: auto; }
.element-changes :deep(.changes-list) { overflow: visible; }
.device-switch { display: inline-flex; align-items: center; gap: 2px; }
.element-inspector { height: auto; }
.element-inspector :deep(.inspector-groups) { overflow: visible; }
.editing-workspace .selection-info { margin-bottom: 10px; }
.selection-breadcrumbs { display: flex; flex-wrap: wrap; gap: 3px; align-items: center; margin: -5px 0 12px; color: var(--muted); font-size: 10px; }
.selection-breadcrumbs button { min-height: 24px; padding: 2px 5px; background: var(--panel-soft); font: inherit; }
.selection-breadcrumbs button::after { content: '›'; margin-left: 4px; color: var(--muted); }
.selection-breadcrumbs > span { padding: 2px 4px; color: var(--accent); }
.canvas-caption { align-items: center; padding: 7px 12px; min-height: 43px; border-bottom: 1px solid var(--border); background: var(--panel); }
.canvas-selection-tools,.canvas-zoom { display: flex; flex-wrap: wrap; gap: 3px; align-items: center; }
.canvas-zoom { margin-left: auto; }
.canvas-help { position: relative; flex: none; font-size: 11px; }.canvas-help > summary { cursor: pointer; padding: 5px 6px; border-radius: 4px; list-style: none; }.canvas-help > summary::-webkit-details-marker { display: none; }.canvas-help[open] > summary,.canvas-help > summary:hover { color: var(--accent); background: var(--accent-soft); }.canvas-help > summary:focus-visible { outline: 2px solid var(--accent-border); }.canvas-help-popover { position: absolute; right: 0; top: calc(100% + 7px); z-index: 10; width: 310px; max-width: calc(100vw - 40px); padding: 12px; border: 1px solid var(--border-strong); border-radius: 7px; background: var(--panel); color: var(--text); box-shadow: 0 8px 25px #17233c25; }.canvas-help-popover > strong { font-size: 12px; }.canvas-help-popover dl { display: grid; grid-template-columns: auto minmax(0,1fr); gap: 7px 10px; margin: 12px 0; }.canvas-help-popover dt { color: var(--text); font-family: var(--font-mono, monospace); }.canvas-help-popover dd { margin: 0; color: var(--muted); }.canvas-help-popover p { margin: 10px 0 0; padding-top: 9px; border-top: 1px solid var(--border); line-height: 1.7; color: var(--muted); }
.canvas-caption button { min-height: 27px; padding: 3px 6px; font-size: 11px; background: transparent; border-color: transparent; }
.canvas-caption button:hover:not(:disabled) { background: var(--panel-soft); }
.canvas-caption button.active,.canvas-caption button[aria-pressed="true"] { color: var(--accent); background: var(--accent-soft); }
.canvas-zoom select { height: 27px; width: 66px; padding: 2px; border: 1px solid var(--border); border-radius: 4px; background: var(--panel); color: var(--text); font: inherit; }
.canvas-viewport { position: relative; flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
.canvas-scroll { position: absolute; inset: 0; display: block; padding: 24px; overflow: auto; overscroll-behavior: contain; }
.canvas-stage { position: relative; min-height: 0; margin: 0 auto; }
.canvas-stage .page-frame { position: absolute; left: 0; top: 0; min-height: 0; min-width: 0; margin: 0; box-sizing: border-box; }
.canvas-stage .page-frame iframe { min-height: 0; }
.canvas-scroll.hand-tool,.canvas-pan-shield { cursor: grab; }
.canvas-scroll.is-panning,.canvas-pan-shield.dragging { cursor: grabbing; }
.canvas-pan-shield { position: absolute; inset: 0; z-index: 2; touch-action: none; }
.canvas-key-hint { margin-left: auto; }
.editing-workspace + .design-footer code { max-width: 25%; }
@container designer (max-width: 900px) {
  .editing-workspace > .layer-tree-panel { position: absolute; z-index: 4; left: 0; top: 43px; bottom: 0; height: auto; width: 230px; max-width: calc(100% - 32px); box-shadow: 6px 0 20px #17233c1c; }
  .editing-workspace > .properties-panel { position: absolute; z-index: 4; left: auto; right: 0; top: 43px; bottom: 0; width: 272px; max-width: calc(100% - 32px); box-shadow: -6px 0 20px #17233c1c; }
  .canvas-key-hint { display: none !important; }
  .design-footer code { display: none; }
}
@container designer (max-width: 600px) {
  .canvas-caption { padding: 6px; }
  .canvas-zoom { margin-left: 0; }
  .source-bar .design-toolbar { margin-left: 0; padding-left: 0; border-left: 0; flex-wrap: wrap; }
  .api-help { display: none; }
}
</style>
