<script setup>
import UiIcon from '../ui/UiIcon.vue'
import SymbolDocumentation from './SymbolDocumentation.vue'
import CallHierarchyPanel from './CallHierarchyPanel.vue'
import EditorActionMenu from './EditorActionMenu.vue'
import { ideActions, ideActionState, ideCapability, ideKeybinding } from '../../lib/ideActions.js'
// 编辑器面板（M18 · P2）：Monaco + 乐观锁保存 + 只读守卫。
//
// 界面上必须让"能不能写"一眼可见：只读时显示锁标记并禁用保存——
// 让用户以为能存、存下去才报错，是最差的交互。
import { computed, markRaw, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../../api.js'
import { WorkspaceLanguageService } from '../../lib/workspaceLanguageService.js'
import { getTokenHover, getJavaTokenTarget, registerTokenBusinessSource, withTokenBusiness, subscribeHoverDocumentationUpdates } from '../../lib/tokenHover.js'
import { createBusinessMeaningSource, subscribeBusinessMeaning } from '../../lib/businessMeaning.js'
import { editorPreferenceOptions, usePreferences } from '../../lib/preferences.js'
import { createHoverDocumentationRefresh } from '../../lib/hoverDocumentationRefresh.js'

const { preferences } = usePreferences()
const editorOptions = computed(() => editorPreferenceOptions(preferences))

const props = defineProps({
  file: { type: Object, default: null },      // { path, text, lang, sha1, start_line, total_lines, uri }
  documentKey: { type: String, default: '' },
  documents: { type: Array, default: () => [] },
  languageServers: { type: Array, default: () => [] },
  restoreViewState: { type: Object, default: null },
  highlight: { type: Object, default: null }, // { start, end, token }
  breakpoints: { type: Array, default: () => [] },
  debugLocation: { type: Object, default: null }, // { path, line }, current paused frame
  toolbarTarget: { type: Object, default: null },
  compactToolbar: { type: Boolean, default: false },
  //: token 是"显式跳转序号"：父组件每次**用户主动**要跳转（点文件 / 点「打开实现」）就自增，
  //: 这样"父组件重渲染"不会造成跳转，而"用户要跳"仍然生效。
  readonly: { type: Boolean, default: true },
  saving: { type: Boolean, default: false },
  projectId: { type: String, default: '' },
  sessionId: { type: String, default: '' },
  tree: { type: String, default: 'main' },
  // 语言服务器可用性（由父组件按项目语言探测后下发）：{ available, name, hint }
  lspServer: { type: Object, default: null },
})
const emit = defineEmits(['change', 'view-state', 'selection', 'save', 'open-file', 'breakpoint', 'language-edit', 'language-symbol'])

const hostEl = ref(null)
const dirty = ref(false)
const error = ref('')
const lspState = ref('idle')     // idle | connecting | ready | closed | error | unavailable
const lspDetail = ref('')
const lspStatusLabel = computed(() => ({ ready: '代码分析就绪', connecting: '代码分析连接中', unavailable: '代码分析未启用，查看配置', error: '代码分析连接失败，查看详情', closed: '代码分析重连中' })[lspState.value] || '代码分析未连接')
const compactLspLabel = computed(() => ({ ready: '分析就绪', connecting: '连接中', unavailable: '分析未启用', error: '分析异常', closed: '重连中' })[lspState.value] || '分析未连接')
//: 「无语言服务器」这个提示要能点开：否则用户只知道"没有"，不知道"装什么、怎么装"
const lspPanel = ref(false)
const lspList = ref([])
const lspListLoading = ref(false)
const lspListLoaded = ref(false)
const lspCopied = ref('')
const lspCopyError = ref('')
const lspPanelEl = ref(null)
const lspTrigger = ref(null)
const showAllLsp = ref(false)
const refreshedLanguageCatalog = ref(null)
let lspListRequest = 0
let copyTimer = null

/** 打开面板时拉一次探测结果（含每个服务器的安装命令与是否装了）。
 *
 * 先用父组件已经拿到的探测结果**立即渲染**（项目一打开就拉过，不该让用户看"探测中…"），
 * 再用项目级接口补齐 install_hint / 状态；服务端有缓存，第二次基本是瞬时的。
 */
async function toggleLspPanel(refresh = false) {
  const opening = !lspPanel.value
  lspPanel.value = refresh ? true : !lspPanel.value
  if (opening && lspPanel.value) nextTick(() => lspPanelEl.value?.querySelector('[aria-label="关闭代码分析设置"]')?.focus())
  if (!lspPanel.value || !props.projectId) return
  if (refresh) {
    lspListLoaded.value = false
    lspList.value = []
  }
  if (!lspList.value.length && props.lspServer?.name) {
    lspList.value = [
      {
        id: props.lspServer.id || 'current',
        name: props.lspServer.name,
        languages: [props.file?.lang || ''],
        state: props.lspServer.available === false ? 'missing' : 'installed',
        install_hint: props.lspServer.hint || '',
        resolved_command: '',
        detected: true,
      },
    ]
  }
  if (lspListLoaded.value) return
  const request = ++lspListRequest
  const projectId = props.projectId
  const tree = props.tree
  lspListLoading.value = true
  try {
    // 用**项目级**探测：它按这个项目实际存在的语言与根标记给出建议，
    // 并且带 state/installed/install_hint（全局目录接口没有这些字段）
    const body = await api.code.projectLsp(projectId, tree || 'main', refresh)
    if (disposed || request !== lspListRequest || projectId !== props.projectId || tree !== props.tree) return
    lspList.value = body.servers || []
    lspListLoaded.value = true
    if (refresh) {
      refreshedLanguageCatalog.value = lspList.value
      await connectLsp()
      await workspaceLanguage?.reconnect()
      if (!disposed && request === lspListRequest) refreshLanguageState()
    }
  } catch (e) {
    if (!disposed && request === lspListRequest) lspDetail.value = e.message
  } finally {
    if (!disposed && request === lspListRequest) lspListLoading.value = false
  }
}

function closeLspPanel() {
  lspPanel.value = false
  nextTick(() => lspTrigger.value?.focus())
}

async function copyLspText(text) {
  lspCopyError.value = ''
  try {
    await navigator.clipboard.writeText(text)
    if (disposed) return
    lspCopied.value = text
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { lspCopied.value = '' }, 1500)
  } catch {
    lspCopied.value = ''
    lspCopyError.value = '复制失败，请手动选择并复制内容。'
  }
}

/** 相关服务器：当前语言优先，再列本项目探测到的。 */
const relevantServers = computed(() => {
  const lang = props.file?.lang || ''
  const mine = lspList.value.filter((s) => (s.languages || []).includes(lang))
  const detected = lspList.value.filter((s) => s.detected && !mine.includes(s))
  return [...mine, ...detected]
})

/** 面板里到底列什么：默认"与当前项目相关"，`showAllLsp` 时列全部预设。
 *
 * 为什么要有"全部"：相关列表省略当前项目用不到的服务器——
 * 用户刚装完 rust-analyzer/clangd 这类服务器，打开面板却看不到，会以为没生效。
 */
const shownServers = computed(() => (showAllLsp.value ? lspList.value : relevantServers.value))

/** 预设计数：让"装了没装"有个总量口径，而不是只看得到眼前几条。 */
const lspCounts = computed(() => {
  const total = lspList.value.length
  const installed = lspList.value.filter((s) => s.state === 'installed').length
  const missing = lspList.value.filter((s) => s.state === 'missing').length
  const disabled = lspList.value.filter((s) => s.enabled === false || s.state === 'unavailable' || s.state === 'disabled').length
  return { total, installed, missing, disabled }
})
let editor = null
let monacoRef = null
let mapLanguage = (lang) => lang
let suppress = false
let mounting = false
let lsp = null
let workspaceLanguage = null
let workspaceLanguageLoading = null
let workspaceLanguageScope = ''
let modelFactory = null
const languageRevision = ref(0)
const languageActionBusy = ref(false)
let languageActionRequest = 0
const symbolPanel = ref(false), symbolMode = ref('document'), symbolQuery = ref(''), symbolItems = ref([]), symbolBusy = ref(false)
const symbolDocumentation = ref(null)
const hierarchy = ref(null), lensReferences = ref(null)
const documentationRef = ref(null)
let documentationRequest = 0
let cancelDocumentation = null
let documentationTarget = null
let documentationBusinessRequest = 0
let nativeHoverRefresh = null
const languageCapabilities = computed(() => {
  languageRevision.value
  const capabilities = {}
  for (const client of workspaceLanguage?.clientsFor(props.file?.lang) || []) {
    for (const [key, value] of Object.entries(client.capabilities || {})) if (value) capabilities[key] = value
  }
  return capabilities
})
const editorCommands = computed(() => ideActions.map(item => ideActionState(item, {
  capabilities: languageCapabilities.value, language: props.file?.lang,
  readonly: effectiveReadonly.value, busy: languageActionBusy.value || symbolBusy.value, ready: !!props.file,
})))
const commandState = id => editorCommands.value.find(item => item.id === id)
const navigationCommands = computed(() => editorCommands.value.filter(item => item.group === 'navigation'))
const refactorCommands = computed(() => editorCommands.value.filter(item => item.group === 'refactor'))
const hasLanguageTools = computed(() => !!props.file && !plainFile.value)
const languageEnvironment = computed(() => {
  languageRevision.value
  return workspaceLanguage?.clientsFor(props.file?.lang).find(client => client.environment?.python || client.environment?.lombok)?.environment || {}
})
const runtimeServers = computed(() => {
  languageRevision.value
  return (workspaceLanguage?.statesFor(props.file?.lang) || []).map(slot => {
    const id = slot.serverId || slot.client.server
    const definition = lspList.value.find(server => server.id === id)
      || (props.languageServers || []).find(server => server.id === id)
    return { id, name: definition?.name || slot.client.server || id || '语言服务', state: slot.state, detail: slot.detail,
      initializationMs: slot.client.initializationMs }
  })
})
const languageFeatures = computed(() => [
  ['completionProvider', '补全'], ['hoverProvider', '说明'], ['signatureHelpProvider', '参数签名'],
  ['definitionProvider', '定义'], ['declarationProvider', '声明'], ['typeDefinitionProvider', '类型'],
  ['implementationProvider', '实现'], ['referencesProvider', '引用'], ['documentSymbolProvider', '文件符号'],
  ['workspaceSymbolProvider', '项目符号'], ['documentFormattingProvider', '整理格式'], ['codeActionProvider', '修复'],
  ['renameProvider', '重命名'], ['semanticTokensProvider', '语义高亮'],
  ['callHierarchyProvider', '调用层级'], ['typeHierarchyProvider', '类型层级'],
  ['codeLensProvider', 'CodeLens'], ['inlayHintProvider', '内联提示'],
].map(([key, label]) => ({ key, label, available: !!languageCapabilities.value[key] })))
const shownSymbols = computed(() => {
  const query = symbolQuery.value.trim().toLocaleLowerCase()
  return symbolItems.value.filter(item => !query || `${item.name} ${item.path}`.toLocaleLowerCase().includes(query))
})
let changeTimer = null
let disposed = false
let editorVersion = 0
let lspVersion = 0
let savedText = null
let activeDocument = null
let consumedHighlightToken = null
const editorDisposables = []
const ownedModels = new Set()
const documentModels = new Map()
let debugDecorations = null
let debugDecorationIds = []
let debugDecorationSignature = null
let debugDecorationScheduled = false
let handlingContent = false

const fileReadonly = computed(() => props.file?.editable === false)
const encodingReadonly = computed(() => fileReadonly.value && props.file?.readonly_reason === 'encoding')
const tooLarge = computed(() => fileReadonly.value && !encodingReadonly.value)
const canSave = computed(() => !props.readonly && !fileReadonly.value && dirty.value && !props.saving)
const effectiveReadonly = computed(() => props.readonly || fileReadonly.value)
const canEdit = computed(() => !!props.file && !effectiveReadonly.value)
const plainFile = computed(() => ['text', 'plaintext', 'unknown', ''].includes(props.file?.lang || '') || /(?:^|\/)\.(?:gitignore|gitattributes|dockerignore|ignore)$/.test(props.file?.path || ''))

function documentKey() {
  return props.documentKey || `${props.projectId}/${props.tree}/${props.file?.path || ''}`
}

function selectionSnapshot() {
  if (!editor || !activeDocument) return null
  const selection = editor.getSelection?.()
  if (!selection) return null
  return {
    key: activeDocument.key, path: activeDocument.path,
    start_line: selection.startLineNumber, end_line: selection.endLineNumber,
    start_column: selection.startColumn, end_column: selection.endColumn,
  }
}

function snapshot() {
  return {
    key: activeDocument?.key || '', path: activeDocument?.path || '',
    text: editor?.getModel()?.getValue() ?? props.file?.text ?? '',
    viewState: editor?.saveViewState?.() || null,
    selection: selectionSnapshot(),
  }
}

/** 关闭标签后释放其缓存模型；当前模型必须先经 Vue 切换，再由父级在 nextTick 调用。 */
function releaseDocument(document) {
  if (!document || !monacoRef) return false
  const key = document.key
  const tracked = key && documentModels.get(key)
  let candidates
  if (tracked) candidates = [...tracked]
  else {
    const file = document.file || document
    if (!file.path) return false
    try {
      const uri = file.uri
        ? monacoRef.Uri.parse(file.uri)
        : monacoRef.Uri.from({ scheme: 'code-agent', authority: 'project', path: `/${document.projectId || props.projectId}/${document.tree || props.tree}/${file.path}` })
      candidates = [...ownedModels].filter(model => model.uri.toString() === uri.toString())
    } catch { return false }
  }
  let released = false
  for (const model of candidates) {
    if (editor?.getModel() === model) {
      // 两个别名可指向同一个绝对 URI：关闭旧别名时只解绑它，当前标签继续拥有模型。
      if (tracked && key !== activeDocument?.key) { tracked.delete(model); released = true }
      continue
    }
    tracked?.delete(model)
    if (tracked && [...documentModels.values()].some(models => models.has(model))) continue
    if (!model.isDisposed?.()) model.dispose()
    ownedModels.delete(model)
    for (const models of documentModels.values()) models.delete(model)
    released = true
  }
  for (const [documentKey, models] of documentModels) if (!models.size) documentModels.delete(documentKey)
  if (released) workspaceLanguage?.closeDocument(document)
  return released
}

function publishSelection() {
  const selection = selectionSnapshot()
  if (selection) emit('selection', selection)
}

function toggleBreakpoint(line = editor?.getPosition?.()?.lineNumber ?? editor?.getSelection?.()?.startLineNumber) {
  if (!editor || !activeDocument || activeDocument.key !== documentKey() || !/\.py$/i.test(activeDocument.path)) return false
  const number = Number(line)
  if (!Number.isInteger(number) || number < 1 || number > (editor.getModel()?.getLineCount?.() ?? Infinity)) return false
  emit('breakpoint', { path: activeDocument.path, line: number })
  return true
}

function renderDebugDecorations() {
  if (!editor || !monacoRef || !activeDocument) return
  const path = activeDocument.path
  const count = editor.getModel()?.getLineCount?.() ?? Infinity
  const validLine = line => Number.isInteger(line) && line > 0 && line <= count
  const decorations = (props.breakpoints || []).filter(bp => bp.path === path && validLine(bp.line)).map(bp => ({
    range: new monacoRef.Range(bp.line, 1, bp.line, 1),
    options: { glyphMarginClassName: 'ide-breakpoint-glyph', glyphMarginHoverMessage: { value: `断点 · 第 ${bp.line} 行（单击移除）` }, ...(monacoRef.editor.GlyphMarginLane ? { glyphMargin: { position: monacoRef.editor.GlyphMarginLane.Left } } : {}) },
  }))
  const location = props.debugLocation
  if (location?.path === path && validLine(location.line)) decorations.push({
    range: new monacoRef.Range(location.line, 1, location.line, 1),
    options: { isWholeLine: true, className: 'ide-debug-line', glyphMarginClassName: 'ide-debug-glyph', glyphMarginHoverMessage: { value: '程序暂停在这里' }, ...(monacoRef.editor.GlyphMarginLane ? { glyphMargin: { position: monacoRef.editor.GlyphMarginLane.Right } } : {}) },
  })
  const signature = JSON.stringify([activeDocument.key, editor.getModel()?.getVersionId?.(), decorations])
  if (signature === debugDecorationSignature) return
  // Decoration writes can synchronously flush Monaco view/content events. Record
  // this version first, then nested notifications cannot repeat the same write.
  debugDecorationSignature = signature
  if (!debugDecorations) debugDecorations = editor.createDecorationsCollection?.()
  if (debugDecorations) debugDecorations.set(decorations)
  else debugDecorationIds = editor.deltaDecorations?.(debugDecorationIds, decorations) || []
}

function scheduleDebugDecorations() {
  if (debugDecorationScheduled) return
  debugDecorationScheduled = true
  const instance = editor, version = editorVersion
  Promise.resolve().then(() => {
    if (disposed || editor !== instance || version !== editorVersion) return
    debugDecorationScheduled = false
    renderDebugDecorations()
  })
}

function publishContent() {
  if (!editor || !activeDocument) return
  const text = editor.getModel()?.getValue() ?? ''
  if (text === activeDocument.publishedText) return
  activeDocument.publishedText = text
  emit('change', { key: activeDocument.key, path: activeDocument.path, text })
}

function flushDocument() {
  if (!editor || !activeDocument) return
  publishContent()
  if (changeTimer) { clearTimeout(changeTimer); changeTimer = null }
  if (activeDocument.uri) {
    workspaceLanguage?.changeDocument(activeDocument.uri, editor.getModel().getValue())
    workspaceLanguage?.flushDocument(activeDocument.uri)
  }
  const state = editor.saveViewState?.()
  if (state) emit('view-state', { key: activeDocument.key, state })
}

function disposeEditor() {
  debugDecorations?.clear?.()
  debugDecorations = null
  debugDecorationSignature = null
  debugDecorationScheduled = false
  if (debugDecorationIds.length) editor?.deltaDecorations?.(debugDecorationIds, [])
  debugDecorationIds = []
  for (const item of editorDisposables.splice(0)) item?.dispose()
  editor?.dispose()
  editor = null
  activeDocument = null
}

/** URI 只用来映射相对路径；回传 LSP 的文档 URI 仍使用后端原值。 */
function relativeTargetPath(resource, context = activeDocument) {
  if (!context) return null
  try {
    const parse = value => {
      const uri = new URL(String(value))
      if (uri.search || uri.hash) return null
      const path = decodeURIComponent(uri.pathname).replace(/\\/g, '/')
      if (path.includes('\0')) return null
      const parts = []
      for (const part of path.split('/')) {
        if (!part || part === '.') continue
        if (part === '..') parts.pop()
        else parts.push(part)
      }
      return { scheme: uri.protocol, authority: uri.host, path: `/${parts.join('/')}` }
    }
    const target = parse(resource?.toString?.() || resource)
    const current = parse(context.uri || context.modelUri)
    if (!target || !current || target.scheme !== current.scheme || target.authority !== current.authority) return null
    const relative = context.path.replace(/\\/g, '/')
    if (!relative || relative.startsWith('/') || relative.split('/').some(part => !part || part === '.' || part === '..')) return null
    let root
    const windows = current.scheme === 'file:' && /^\/[a-z]:\//i.test(current.path)
    const compare = value => windows ? value.toLowerCase() : value
    if (current.scheme === 'file:') {
      const suffix = `/${relative}`
      if (!compare(current.path).endsWith(compare(suffix))) return null
      root = current.path.slice(0, -suffix.length)
    } else if (current.scheme === 'code-agent:' && current.authority === 'project') {
      root = `/${context.projectId}/${context.tree}`
      if (!current.path.startsWith(`${root}/`)) return null
    } else return null
    if (!compare(target.path).startsWith(compare(`${root}/`))) return null
    const path = target.path.slice(root.length + 1)
    if (!path || path.split('/').some(part => !part || part === '.' || part === '..')) return null
    return path
  } catch { return null }
}

function registerCrossFileOpener() {
  const instance = editor, context = activeDocument
  const opener = monacoRef.editor.registerEditorOpener?.({
    openCodeEditor(source, resource, selectionOrPosition) {
      if (disposed || source !== instance || editor !== instance || activeDocument !== context
        || context.projectId !== props.projectId || context.tree !== props.tree || context.key !== documentKey()) return false
      const path = relativeTargetPath(resource, context)
      if (!path) return false
      const position = selectionOrPosition || {}
      const positive = value => Number.isFinite(Number(value)) ? Math.max(1, Math.floor(Number(value))) : 1
      emit('open-file', {
        path,
        line: positive(position.startLineNumber ?? position.lineNumber),
        column: positive(position.startColumn ?? position.column),
      })
      return true
    },
  })
  if (opener) editorDisposables.push(opener)
}

/** Monaco 是**按需动态加载**的：只看导图不该为一个 4MB 的编辑器买单。 */
async function mount() {
  // file 首次从 null 变为对象时，v-else 的宿主节点要等 Vue 完成渲染才存在。
  await nextTick()
  if (disposed || !hostEl.value || editor || mounting || !props.file) return
  const version = editorVersion
  mounting = true
  try {
    const mod = await import('../../lib/monacoSetup.js')
    if (disposed || version !== editorVersion || !hostEl.value || editor || !props.file) return
    monacoRef = mod.ensureMonaco()
    mapLanguage = mod.monacoLanguage
    modelFactory = mod.getModel
    const model = mod.getModel(
      props.file.path, props.file.text ?? '', props.file.lang,
      props.file.uri, `${props.projectId}/${props.tree}`,
    )
    ownedModels.add(model)
    if (model.getValue() !== (props.file.text ?? '')) model.setValue(props.file.text ?? '')
    const initialText = props.file.text ?? ''
    activeDocument = {
      key: documentKey(), path: props.file.path, uri: props.file.uri || '',
      modelUri: model.uri?.toString() || '', projectId: props.projectId, tree: props.tree,
      publishedText: initialText,
      baseline: props.file.diskText ?? props.file.savedText ?? (props.file.dirty || props.file.isDirty ? null : initialText),
    }
    const businessSource = createBusinessMeaningSource({
      contextForModel(candidate) {
        if (disposed || editor?.getModel() !== candidate || !activeDocument || activeDocument.key !== documentKey()) return null
        const context = activeDocument
        return { projectId: context.projectId, tree: context.tree, path: context.path, uri: context.uri,
          client: languageClient('definitionProvider'), pathForUri: uri => relativeTargetPath(uri, context),
          textForUri(uri) {
            if (uri === context.uri) return candidate.getValue()
            const row = languageDocuments().find(document => document.uri === uri)
            return row?.model?.getValue?.() ?? row?.text
          },
        }
      },
      targetForModel: getJavaTokenTarget,
      resolve: (projectId, payload, options) => api.code.resolveSymbolBusiness(projectId, payload, options),
    })
    editorDisposables.push(businessSource, registerTokenBusinessSource(businessSource))
    // Register after the source's subscription so its cache is invalidated
    // before an already open explanation reads the updated function record.
    editorDisposables.push({ dispose: subscribeBusinessMeaning(change => { void refreshBusinessDocumentation(change) }) })
    editorDisposables.push({ dispose: subscribeHoverDocumentationUpdates(change => {
      void refreshLanguageDocumentation(change)
      nativeHoverRefresh?.refresh(change)
    }) })
    if (!documentModels.has(activeDocument.key)) documentModels.set(activeDocument.key, new Set())
    documentModels.get(activeDocument.key).add(model)
    dirty.value = activeDocument.baseline == null || initialText !== activeDocument.baseline
    editor = monacoRef.editor.create(hostEl.value, {
      model,
      theme: mod.currentEditorTheme?.() || 'code-agent-light',
      'semanticHighlighting.enabled': true,
      bracketPairColorization: { enabled: true, independentColorPoolPerBracketType: true },
      guides: { bracketPairs: 'active', highlightActiveBracketPair: true, indentation: true },
      readOnly: effectiveReadonly.value,
      automaticLayout: true,
      glyphMargin: true,
      scrollBeyondLastLine: false,
      renderLineHighlight: 'all',
      ...editorOptions.value,
      // Monaco/VS Code 默认会把"看起来像 ASCII 但不是"的字符框成小方块
      // （`ambiguousCharacters: true` + `includeStrings: true`）。在这个仓库里
      // 那就是中文全角标点：全仓 1.6 万处 `（ ） ： ； ，`，全在注释与文档串里——
      // 一屏中文说明就全是黄框（用户截图问过"有小方块是什么"）。
      // 这里按字符放行中文标点，但仍然保留两类真问题的提醒：
      //   · 不可见字符（NBSP / 零宽空格 / BOM）：那是会干扰字符串比较与正则的脏字符；
      //   · 真正的同形字（西里尔 а、希腊 ο 这种伪装成 ASCII 的）继续高亮。
      unicodeHighlight: {
        allowedCharacters: {
          '（': true, '）': true, '：': true, '；': true, '，': true, '。': true,
          '？': true, '！': true, '、': true, '·': true,
          '“': true, '”': true, '‘': true, '’': true,
          '—': true, '–': true, '％': true, '／': true,
          // 注意：**表意空格 U+3000 不放行**——它看起来就是普通空格，
          // 出现在代码里是实打实的坑，继续让它被标出来
        },
        invisibleCharacters: true,
        // 中文注释/文档串里的汉字不是"可疑字符"；显式关掉以防在不受信任环境里被默认打开
        nonBasicASCII: false,
      },
      scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
      // Keep hover and suggestion widgets clear of the clipped editor/chat split.
      fixedOverflowWidgets: true,
      hover: { enabled: true, delay: 350, sticky: true },
    })
    const themeSubscription = mod.followEditorTheme?.(monacoRef)
    nativeHoverRefresh = createHoverDocumentationRefresh({ editor, contextForModel(candidate) {
      const context = activeDocument
      if (disposed || candidate !== editor?.getModel() || !context || context.key !== documentKey()) return null
      const client = languageClient('hoverProvider')
      return { projectId: context.projectId, tree: context.tree || 'main', uri: context.uri,
        version: client?.documents?.get(context.uri)?.version }
    } })
    editorDisposables.push(nativeHoverRefresh)
    if (themeSubscription) editorDisposables.push(themeSubscription)
    model.updateOptions?.({ tabSize: preferences.editorTabSize, insertSpaces: true })
    editor.onDidChangeModelContent(() => {
      if (symbolDocumentation.value) retainPinnedDocumentation()
      hierarchy.value = lensReferences.value = null
      if (suppress || handlingContent) return
      handlingContent = true
      try {
        const text = editor.getModel().getValue()
        dirty.value = activeDocument.baseline == null || text !== activeDocument.baseline
        publishContent()
        // Keep model writes outside Monaco's content-event dispatch. Its glyph
        // layout can emit nested view events while a text replacement is flushing.
        scheduleDebugDecorations()
        // 文档同步做去抖：逐字 didChange 会把语言服务器打爆（尤其是每次全量发送）
        if (activeDocument?.uri) workspaceLanguage?.changeDocument(activeDocument.uri, text)
      } finally { handlingContent = false }
    })
    const cursorListener = editor.onDidChangeCursorSelection?.(publishSelection)
    if (cursorListener) editorDisposables.push(cursorListener)
    const gutterListener = editor.onMouseDown?.(event => {
      if (event.target?.type === (monacoRef.editor.MouseTargetType?.GUTTER_GLYPH_MARGIN ?? 2)) toggleBreakpoint(event.target.position?.lineNumber)
    })
    if (gutterListener) editorDisposables.push(gutterListener)
    if (monacoRef.KeyCode.F9 != null) editor.addCommand(monacoRef.KeyCode.F9, () => toggleBreakpoint())
    renderDebugDecorations()
    registerCrossFileOpener()
    editor.addCommand(monacoRef.KeyMod.CtrlCmd | monacoRef.KeyCode.KeyS, () => save())
    editor.addAction({
      id: 'code-agent.save',
      label: '保存（Code-agent）',
      keybindings: [monacoRef.KeyMod.CtrlCmd | monacoRef.KeyCode.KeyS],
      run: () => save(),
    })
    for (const item of ideActions) editorDisposables.push(editor.addAction({
      id: item.id === 'documentation' ? 'code-agent.symbol-documentation' : `code-agent.ide.${item.id}`,
      label: item.id === 'documentation' ? '查看完整说明' : item.label,
      ...(item.keys ? { keybindings: [ideKeybinding(item, monacoRef)], keybindingContext: 'editorTextFocus' } : {}),
      ...(item.id === 'documentation' ? { contextMenuGroupId: 'navigation', contextMenuOrder: 3 } : {}),
      run: () => runIdeCommand(item.id),
    }))
    for (const kind of ['call', 'type']) editorDisposables.push(editor.addAction({
      id: `code-agent.${kind}-hierarchy`, label: kind === 'call' ? '查看调用层级' : '查看类型层级',
      contextMenuGroupId: 'navigation', contextMenuOrder: kind === 'call' ? 4 : 5,
      run: () => showHierarchy(kind),
    }))
    if (props.restoreViewState) editor.restoreViewState?.(props.restoreViewState)
    const token = Number(props.highlight?.token ?? 0)
    const freshJump = props.highlight?.start && (consumedHighlightToken == null ? Number(token) > 0 : token !== consumedHighlightToken)
    if (!props.restoreViewState || freshJump) jumpToHighlight()
    else lastJump.value = { line: props.highlight?.start || 0, token: Number(token ?? 0), column: props.highlight?.column || props.highlight?.start_column || 1 }
    publishSelection()
    // 面板重新可见（v-show 切回来）时，把"隐藏期间没落地的跳转"补上
    editor.onDidLayoutChange(() => flushPendingJump())
    connectLsp()
  } catch (e) {
    if (!disposed && version === editorVersion) error.value = `编辑器加载失败：${e.message}`
  } finally {
    mounting = false
    if (!disposed && version !== editorVersion) mount()
  }
}

function languageServerIds(lang) {
  const servers = (refreshedLanguageCatalog.value || props.languageServers || []).filter(server => (server.languages || []).includes(lang))
  const installed = servers.filter(server => server.enabled !== false && server.state !== 'disabled'
    && (server.state === 'installed' || server.available === true))
  const primary = installed.find(server => server.id !== 'ruff')
  const ids = primary ? [primary.id || ''] : servers.length ? []
    : lang === props.file?.lang && (props.lspServer?.available === false || props.lspServer?.enabled === false
      || props.lspServer?.state === 'disabled') ? [] : ['']
  if (lang === 'python' && installed.some(server => server.id === 'ruff')) ids.push('ruff')
  return ids
}

function canEditLanguageModel(model) {
  if (disposed || props.readonly || !ownedModels.has(model) || model.isDisposed?.()) return false
  if (editor?.getModel() === model) return !fileReadonly.value && activeDocument?.key === documentKey()
  const documents = (props.documents || []).filter(row => documentModels.get(row.key)?.has(model)
    && (!row.projectId || row.projectId === props.projectId) && (!row.tree || row.tree === (props.tree || 'main')))
  return documents.length > 0 && documents.every(document => document.file?.editable !== false && document.editable !== false)
}

function refreshLanguageState() {
  languageRevision.value += 1
  const lang = props.file?.lang
  const slots = workspaceLanguage?.statesFor(lang) || []
  const ready = slots.filter(slot => slot.state === 'ready')
  lsp = (ready.find(slot => slot.serverId !== 'ruff') || ready[0] || slots[0])?.client || null
  if (plainFile.value) { lspState.value = 'idle'; lspDetail.value = ''; return }
  const state = ready.length ? 'ready' : slots.some(slot => slot.state === 'connecting') ? 'connecting'
    : slots.find(slot => ['error', 'closed'].includes(slot.state))?.state
  lspState.value = state || (languageServerIds(lang).length ? 'idle' : 'unavailable')
  lspDetail.value = ready.length ? ready.map(slot => slot.client.server || slot.serverId).filter(Boolean).join(' · ')
    : slots.find(slot => slot.detail)?.detail || props.lspServer?.hint || ''
}

async function ensureWorkspaceLanguage() {
  if (disposed || !props.projectId) return null
  if (workspaceLanguage && (workspaceLanguage.projectId !== props.projectId || workspaceLanguage.tree !== (props.tree || 'main'))) shutdownLsp()
  if (workspaceLanguage) return workspaceLanguage
  if (workspaceLanguageLoading) return workspaceLanguageLoading
  const projectId = props.projectId, tree = props.tree || 'main', version = lspVersion
  workspaceLanguageScope = `${projectId}\0${tree}`
  const task = (async () => {
    const { LspClient } = await import('../../lib/lspClient.js')
    if (disposed || version !== lspVersion || projectId !== props.projectId || tree !== (props.tree || 'main')) return null
    workspaceLanguage = new WorkspaceLanguageService({
      clientClass: LspClient, projectId, tree, sessionId: props.sessionId,
      setTimer: (fn, milliseconds) => setTimeout(fn, milliseconds), clearTimer: id => clearTimeout(id),
      canEdit: canEditLanguageModel,
      onStatus: refreshLanguageState, onCapabilities: refreshLanguageState,
      onWorkspaceEdit: payload => { if (!disposed) emit('language-edit', payload) },
      onNavigate: showLensReferences,
    })
    return workspaceLanguage
  })()
  workspaceLanguageLoading = task
  try { return await task } finally { if (workspaceLanguageLoading === task) workspaceLanguageLoading = null }
}

function updateModelText(model, text) {
  if (model.getValue() === text) return
  if (model.pushEditOperations && model.getFullModelRange) {
    model.pushStackElement?.()
    model.pushEditOperations([], [{ range: model.getFullModelRange(), text }], () => null)
    model.pushStackElement?.()
  } else model.setValue(text)
}

function languageDocuments() {
  const rows = []
  const candidates = [...(props.documents || []).filter(document => !document.needsReload
    && (!document.projectId || document.projectId === props.projectId) && (!document.tree || document.tree === (props.tree || 'main')))]
  const currentDocument = activeDocument || (editor && props.file ? { key: documentKey(), projectId: props.projectId, tree: props.tree } : null)
  if (currentDocument && currentDocument.key === documentKey() && currentDocument.projectId === props.projectId && currentDocument.tree === props.tree) {
    const current = { key: currentDocument.key, file: props.file, text: editor.getModel().getValue() }
    const index = candidates.findIndex(document => document.key === current.key)
    if (index >= 0) candidates.splice(index, 1)
    candidates.push(current) // An active alias wins when two tabs share the same URI.
  }
  for (const document of candidates) {
    const file = document.file || document, uri = file.uri, path = document.path || file.path, lang = file.lang
    if (!uri || !path || !document.key || !lang || ['text', 'plaintext', 'unknown', ''].includes(lang)
      || /(?:^|\/)\.(?:gitignore|gitattributes|dockerignore|ignore)$/.test(path)) continue
    const text = typeof document.text === 'string' ? document.text : file.text
    if (typeof text !== 'string') continue
    let model = [...(documentModels.get(document.key) || [])].find(item => item.uri?.toString() === uri && !item.isDisposed?.())
    if (!model && modelFactory) model = modelFactory(path, text, lang, uri, `${props.projectId}/${props.tree}`)
    if (!model && editor?.getModel() && currentDocument?.key === document.key) model = editor.getModel()
    if (!model) continue
    ownedModels.add(model)
    model.updateOptions?.({ tabSize: preferences.editorTabSize, insertSpaces: true })
    if (!documentModels.has(document.key)) documentModels.set(document.key, new Set())
    documentModels.get(document.key).add(model)
    if (model !== editor?.getModel()) updateModelText(model, text)
    rows.push({ key: document.key, uri, lang, text, model, servers: languageServerIds(lang) })
  }
  return rows
}

async function connectLsp() {
  if (disposed || !editor || !props.projectId) return
  const version = lspVersion
  try {
    const service = await ensureWorkspaceLanguage()
    if (!service || disposed || version !== lspVersion) return
    const ready = service.syncDocuments(languageDocuments())
    refreshLanguageState()
    await ready
    if (!disposed && service === workspaceLanguage) refreshLanguageState()
  } catch (e) {
    if (!disposed && version === lspVersion) { lspState.value = 'error'; lspDetail.value = e.message }
  }
}

function shutdownLsp() {
  lspVersion += 1
  if (changeTimer) { clearTimeout(changeTimer); changeTimer = null }
  workspaceLanguage?.dispose()
  workspaceLanguage = null; workspaceLanguageLoading = null; workspaceLanguageScope = ''; lsp = null
  lspState.value = 'idle'; languageRevision.value += 1
}

function notifySaved(document, text) {
  const saved = typeof document === 'string' ? { key: document, text } : document
  return workspaceLanguage?.notifySaved(saved || {}) || false
}

function notifyResourceChanges(batch, undo = false, rootPath = '') {
  const context = activeDocument
  if (!workspaceLanguage || !Array.isArray(batch?.files)) return false
  let rootUrl
  if (rootPath) {
    const normalized = rootPath.replace(/\\/g, '/').replace(/\/+$/, '')
    if (!/^[a-z]:\//i.test(normalized) && !normalized.startsWith('/') || normalized.startsWith('//')) return false
    rootUrl = new URL('file:///')
    rootUrl.pathname = '/' + normalized.split('/').filter(Boolean).map(encodeURIComponent).join('/') + '/'
  } else {
    if (!context?.uri?.startsWith('file:') || !relativeTargetPath(context.uri, context)) return false
    rootUrl = new URL(context.uri)
    rootUrl.pathname = rootUrl.pathname.split('/').slice(0, -context.path.replace(/\\/g, '/').split('/').length).join('/') + '/'
  }
  const root = rootUrl.href
  const changes = batch.files.map(row => {
    const path = String(row.path || '')
    if (!path || path.split('/').some(part => !part || part === '..' || part === '.')) return null
    const before = undo ? row.after_exists : row.before_exists, after = undo ? row.before_exists : row.after_exists
    const uri = new URL(path.split('/').map(encodeURIComponent).join('/'), root).href
    return { uri, type: !before && after ? 1 : before && !after ? 3 : 2 }
  }).filter(Boolean)
  for (const slot of workspaceLanguage.clients.values()) if (slot.state === 'ready') slot.client.notify('workspace/didChangeWatchedFiles', { changes })
  return true
}

function languageClient(capability) { return workspaceLanguage?.clientFor(props.file?.lang, capability) }

function showHierarchy(kind = 'call') {
  const client = languageClient(kind === 'type' ? 'typeHierarchyProvider' : 'callHierarchyProvider')
  if (!client || !activeDocument?.uri || activeDocument.key !== documentKey()) return false
  closeDocumentation(false); symbolPanel.value = lspPanel.value = false; lensReferences.value = null
  workspaceLanguage.flushDocument(activeDocument.uri)
  hierarchy.value = { client: markRaw(client), uri: activeDocument.uri, position: editor.getPosition(), kind }
  return true
}
function navigateHierarchy(item) {
  const path = relativeTargetPath(item.uri, activeDocument)
  if (!path || !item.range?.start) return false
  openLanguageSymbol({ path, line: item.range.start.line + 1, column: item.range.start.character + 1 })
  return true
}
function showLensReferences(payload) {
  if (disposed || payload.projectId !== props.projectId || payload.tree !== (props.tree || 'main') || !activeDocument) return false
  const items = (payload.locations || []).map(item => {
    const path = relativeTargetPath(item.uri, activeDocument)
    return path && { path, line: item.range.startLineNumber, column: item.range.startColumn }
  }).filter(Boolean)
  closeDocumentation(false); hierarchy.value = null; symbolPanel.value = lspPanel.value = false
  lensReferences.value = { title: payload.title || '引用', items }
  return true
}
function closeLanguageNavigation() { hierarchy.value = lensReferences.value = null; editor?.focus() }

function closeDocumentation(focus = true) {
  documentationRequest += 1
  documentationBusinessRequest += 1
  cancelDocumentation?.()
  cancelDocumentation = null
  documentationTarget = null
  symbolDocumentation.value = null
  if (focus) editor?.focus()
}

function retainPinnedDocumentation() {
  const document = symbolDocumentation.value
  const keep = document?.pinned && !document.loading && document.projectId === props.projectId && document.tree === (props.tree || 'main')
  closeDocumentation(false)
  if (keep) symbolDocumentation.value = { ...document, detached: true }
}
function toggleDocumentationPin() {
  if (symbolDocumentation.value) symbolDocumentation.value = { ...symbolDocumentation.value, pinned: !symbolDocumentation.value.pinned }
}
function documentationKey(event) {
  if (!event.defaultPrevented && symbolDocumentation.value && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'q') {
    event.preventDefault(); event.stopPropagation(); toggleDocumentationPin()
  }
}

// Both the initial query and background refreshes publish the complete reader
// model. Explicit optional values clear facts from an earlier refresh rather
// than leaving stale symbol/business details behind in the retained panel.
function fullDocumentationContent(result) {
  return {
    sections: result?.sections || [],
    originalSections: result?.originalSections || [],
    source: result?.source,
    symbol: result?.symbol,
    tokenSection: result?.tokenSection,
    symbolSection: result?.symbolSection,
    businessBaseSections: result?.businessBaseSections,
    businessSection: result?.businessSection,
    business: result?.business,
    businessReadError: result?.businessReadError,
    untranslated: result?.untranslated,
    documentationTranslation: result?.documentationTranslation,
    documentation_translation: result?.documentation_translation,
    _code_agent_original: result?._code_agent_original,
  }
}

async function refreshBusinessDocumentation(change) {
  const target = documentationTarget
  if (!target || !target.current() || !symbolDocumentation.value || target.model.getLanguageId?.() !== 'java'
    || change?.projectId !== target.context.projectId || (change.tree || 'main') !== (target.context.tree || 'main')
    || (change.id && target.entityId && change.id !== target.entityId)) return false
  target.refreshNeeded = true
  if (!target.base || symbolDocumentation.value.loading) return false
  target.refreshNeeded = false
  const request = ++documentationBusinessRequest
  const current = () => target === documentationTarget && target.current() && request === documentationBusinessRequest
  const token = { get isCancellationRequested() { return !current() }, onCancellationRequested() { return { dispose() {} } } }
  // The pinned language explanation and exact declaration are sufficient. A
  // background description update must not ask the language server again.
  const result = await withTokenBusiness(target.model, target.position,
    { ...target.base, businessLookup: target.lookup, skipBusinessDefinition: true }, token)
  if (!current() || (!result?.business && !result?.businessReadError)) return false
  target.lookup = result.businessLookup || target.lookup
  target.entityId = result.business?.entity?.id || target.entityId
  symbolDocumentation.value = { ...symbolDocumentation.value, ...fullDocumentationContent(result),
    sections: result.sections || target.base.sections || [], originalSections: result.originalSections || target.base.originalSections || [] }
  return true
}

async function refreshLanguageDocumentation(change) {
  const target = documentationTarget
  if (!target || !target.current() || !symbolDocumentation.value
    || change?.projectId !== target.context.projectId || (change.tree || 'main') !== (target.context.tree || 'main')
    || change.uri !== target.context.uri) return false
  const client = languageClient('hoverProvider')
  const version = client?.documents?.get(target.context.uri)?.version ?? target.model.getVersionId?.()
  if (Number.isInteger(change.version) && Number.isInteger(version) && change.version !== version) return false
  if (change.position) {
    if (change.position.line + 1 !== target.position.lineNumber) return false
    const word = target.model.getWordAtPosition?.(target.position), column = change.position.character + 1
    if (word ? column < word.startColumn || column >= word.endColumn : column !== target.position.column) return false
  }
  if (target.queryPending || symbolDocumentation.value.loading) { target.languageRefreshNeeded = true; return false }
  return showDocumentation({ refreshTarget: target })
}

async function retryDocumentationTranslation() {
  const target = documentationTarget
  if (!target || !target.current() || target.queryPending || symbolDocumentation.value?.detached
    || symbolDocumentation.value?.documentation_translation?.status === 'pending') return false
  return showDocumentation({ refreshTarget: target, retryTranslation: true })
}

async function showDocumentation({ refreshTarget = null, retryTranslation = false } = {}) {
  const client = languageClient('hoverProvider'), instance = editor, model = editor?.getModel(), context = activeDocument
  if (!model || !context?.uri || context.key !== documentKey()) return false
  if (!client && props.file?.lang !== 'java') {
    instance.focus()
    await instance.getAction?.('editor.action.showHover')?.run()
    return true
  }
  const previous = refreshTarget ? symbolDocumentation.value : null
  const pinned = !!previous?.pinned
  closeDocumentation(false)
  symbolPanel.value = false
  lspPanel.value = false
  hierarchy.value = lensReferences.value = null
  const request = documentationRequest, version = model.getVersionId?.()
  const selection = instance.getSelection?.()
  const position = refreshTarget?.position || instance.getPosition?.() || { lineNumber: selection?.startLineNumber || 1, column: selection?.startColumn || 1 }
  const title = model.getWordAtPosition?.(position)?.word || props.file?.path?.split('/').pop() || '符号说明'
  const listeners = new Set()
  let cancelled = false
  const token = { get isCancellationRequested() { return cancelled }, onCancellationRequested(callback) {
    if (cancelled) callback()
    else listeners.add(callback)
    return { dispose: () => listeners.delete(callback) }
  } }
  const cancel = () => { cancelled = true; for (const callback of listeners) callback(); listeners.clear() }
  cancelDocumentation = cancel
  const current = () => !disposed && request === documentationRequest && editor === instance && activeDocument === context
    && context.key === documentKey() && context.projectId === props.projectId && context.tree === props.tree
    && model === editor?.getModel() && model.getVersionId?.() === version && !cancelled
  const target = { model, context, position: { ...position }, current, base: null, lookup: null, entityId: '', refreshNeeded: false, languageRefreshNeeded: false, queryPending: true }
  documentationTarget = target
  symbolDocumentation.value = { ...(previous || { sections: [] }), title, path: context.path, line: position.lineNumber, projectId: context.projectId, tree: context.tree || 'main', pinned, loading: !previous, error: '',
    ...(retryTranslation ? { documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'pending' } } : {}) }
  if (!refreshTarget) void nextTick(() => { if (current()) documentationRef.value?.$el?.focus?.() })
  try {
    workspaceLanguage?.flushDocument(context.uri)
    if (retryTranslation && !client) throw new Error('language service unavailable')
    const languageResult = client ? await client.hover(context.uri, position, token, { retryTranslation }) : getTokenHover(model, position)
    if (!current()) return false
    target.base = languageResult
    const result = await withTokenBusiness(model, position, languageResult, token)
    if (!current()) return false
    target.lookup = result?.businessLookup || null
    target.entityId = result?.business?.entity?.id || ''
    target.queryPending = false
    symbolDocumentation.value = { ...symbolDocumentation.value, ...fullDocumentationContent(result), loading: false }
    if (target.refreshNeeded) void refreshBusinessDocumentation({ projectId: context.projectId, tree: context.tree })
    if (target.languageRefreshNeeded) void refreshLanguageDocumentation({ projectId: context.projectId, tree: context.tree, uri: context.uri })
    return true
  } catch (cause) {
    if (current()) {
      target.queryPending = false
      symbolDocumentation.value = previous ? { ...symbolDocumentation.value, loading: false, error: '',
        documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'failed', retryable: true, error_code: client ? 'request_failed' : 'provider_unavailable' } }
        : { ...symbolDocumentation.value, loading: false, error: cause.message || '暂时无法读取说明' }
      if (target.languageRefreshNeeded) void refreshLanguageDocumentation({ projectId: context.projectId, tree: context.tree, uri: context.uri })
    }
    return false
  } finally {
    listeners.clear()
    if (cancelDocumentation === cancel) cancelDocumentation = null
  }
}

async function formatDocument() {
  const client = languageClient('documentFormattingProvider'), instance = editor, model = editor?.getModel(), context = activeDocument
  if (!model || !context || context.key !== documentKey() || context.path !== props.file?.path || effectiveReadonly.value || languageActionBusy.value) return false
  if (!client) return languageEditorAction('editor.action.formatDocument', 'documentFormattingProvider', true)
  const request = ++languageActionRequest, version = model.getVersionId?.()
  languageActionBusy.value = true; error.value = ''
  try {
    workspaceLanguage.flushDocument(context.uri)
    const edits = await client.formatDocument(context.uri, { tabSize: model.getOptions?.()?.tabSize || 4, insertSpaces: model.getOptions?.()?.insertSpaces !== false })
    if (disposed || request !== languageActionRequest || editor !== instance || activeDocument !== context
      || model.getVersionId?.() !== version || effectiveReadonly.value) return false
    if (!Array.isArray(edits) || !edits.length) return true
    const operations = edits.map(edit => {
      const { start, end } = edit.range || {}
      if (![start?.line, start?.character, end?.line, end?.character].every(value => Number.isInteger(value) && value >= 0)
        || typeof edit.newText !== 'string') throw new Error('整理格式时收到了无效修改')
      return { range: new monacoRef.Range(start.line + 1, start.character + 1, end.line + 1, end.character + 1), text: edit.newText }
    })
    instance.pushUndoStop?.()
    instance.executeEdits('code-agent.format', operations)
    instance.pushUndoStop?.()
    publishContent()
    return true
  } catch (e) { if (!disposed && request === languageActionRequest) error.value = e.message; return false }
  finally { if (request === languageActionRequest) languageActionBusy.value = false }
}

function languageEditorAction(action, capability, writes = ideActions.some(item => item.action === action && item.writes)) {
  if (activeDocument?.key !== documentKey() || writes && effectiveReadonly.value || !ideCapability(languageCapabilities.value, props.file?.lang, capability) || languageActionBusy.value) return false
  if (activeDocument?.uri) workspaceLanguage?.flushDocument(activeDocument.uri)
  editor?.focus()
  const command = editor?.getAction?.(action)
  if (!command && !editor?.trigger) return false
  const result = command ? command.run() : editor.trigger('code-agent.language', action, {})
  result?.catch?.(e => { if (!disposed) error.value = e.message })
  return true
}

function runEditorCommand(id) {
  if (!editor || !props.file || activeDocument?.key !== documentKey()) return false
  const actions = { undo: 'undo', redo: 'redo', find: 'actions.find', replace: 'editor.action.startFindReplaceAction', selectAll: 'editor.action.selectAll', gotoLine: 'editor.action.gotoLine' }
  if (!actions[id] || (['undo', 'redo', 'replace'].includes(id) && (props.readonly || fileReadonly.value))) return false
  editor.focus()
  const action = editor.getAction?.(actions[id])
  const result = action ? action.run() : editor.trigger('code-agent.menu', actions[id], {})
  result?.catch?.(cause => { if (!disposed) error.value = cause.message })
  return true
}

function runIdeCommand(id) {
  const item = commandState(id)
  if (!item || item.disabled) { if (item?.reason) error.value = item.reason; return false }
  error.value = ''
  if (id === 'documentation') {
    if (symbolDocumentation.value && !symbolDocumentation.value.detached) { toggleDocumentationPin(); return true }
    return showDocumentation()
  }
  if (id === 'format') return formatDocument()
  if (id === 'symbols') {
    if (languageCapabilities.value.documentSymbolProvider || languageCapabilities.value.workspaceSymbolProvider) return toggleSymbolPanel()
    return languageEditorAction('editor.action.quickOutline', 'documentSymbolProvider')
  }
  if (id === 'callHierarchy' || id === 'typeHierarchy') return showHierarchy(id === 'callHierarchy' ? 'call' : 'type')
  return languageEditorAction(item.action, item.capability, item.writes)
}

async function loadSymbols() {
  const capability = symbolMode.value === 'workspace' ? 'workspaceSymbolProvider' : 'documentSymbolProvider'
  const client = languageClient(capability), context = activeDocument
  if (!client || !context) return
  const request = ++languageActionRequest
  symbolBusy.value = true
  try {
    workspaceLanguage.flushDocument(context.uri)
    const values = symbolMode.value === 'workspace' ? await client.workspaceSymbols(symbolQuery.value.trim()) : await client.symbols(context.uri)
    if (disposed || request !== languageActionRequest || activeDocument !== context || !symbolPanel.value) return
    const result = []
    const visit = (items, depth = 0) => { for (const item of items || []) {
      const uri = item.location?.uri || context.uri, range = item.selectionRange || item.location?.range || item.range
      const path = relativeTargetPath(uri, context)
      if (path && range?.start) result.push({ name: item.name || '未命名符号', path,
        line: range.start.line + 1, column: range.start.character + 1, depth })
      if (item.children) visit(item.children, depth + 1)
    } }
    visit(values)
    symbolItems.value = result
  } catch (e) { if (!disposed && request === languageActionRequest) error.value = e.message }
  finally { if (request === languageActionRequest) symbolBusy.value = false }
}

function toggleSymbolPanel() {
  symbolPanel.value = !symbolPanel.value
  if (symbolPanel.value) { symbolMode.value = languageCapabilities.value.documentSymbolProvider ? 'document' : 'workspace'; symbolQuery.value = ''; symbolItems.value = []; loadSymbols() }
}
function setSymbolMode(mode) { symbolMode.value = mode; symbolQuery.value = ''; symbolItems.value = []; loadSymbols() }
function openLanguageSymbol(item) {
  if (!item || !props.projectId) return
  symbolPanel.value = false
  emit('language-symbol', { ...item, projectId: props.projectId, tree: props.tree || 'main' })
}

const highlightLine = ref(0)
//: 上一次真正执行过的跳转（行号 + 请求序号）。用来挡住"父组件重渲染带来的重复跳转"。
const lastJump = ref({ line: 0, token: -1 })
//: 面板被 v-show 隐藏时（切到地图/文档）视口高度是 0，这时 revealLineInCenter 不生效，
//: 而且 Monaco 的 getLayoutInfo() 会返回**上一次的**高度，光看高度判断不出来。
//: 所以：把跳转记在这里，直到**真的看见那一行**（getVisibleRanges 命中）才算落地。
let pendingJump = null

function applyJump(line, end, column = 1, endColumn = column) {
  editor.revealLineInCenter(line)
  editor.setSelection(new monacoRef.Range(line, column, end, endColumn))
  // 把"当前跳到哪一行"暴露到 DOM：Monaco 的选区/光标只在编辑器获得焦点时才渲染，
  // 外部（冒烟脚本、无障碍工具）没法判断"跳转到底生效没有"。这一行状态本来就是 UI 知道的。
  highlightLine.value = line
}

/** 尝试落地待处理的跳转；只有"那一行真的在可见区间里"才清除。 */
function flushPendingJump() {
  if (!pendingJump || !editor) return
  const { line, end, column, endColumn } = pendingJump
  applyJump(line, end, column, endColumn)
  const ranges = editor.getVisibleRanges?.() || []
  const visible = ranges.some((r) => line >= r.startLineNumber && line <= r.endLineNumber)
  if (visible) pendingJump = null
}

/**
 * 跳到高亮行。
 *
 * 为什么要 `token` 且要记账：父组件（CodeView）传下来的 `highlight` 是**每次渲染都新建的
 * 对象字面量**，`watch(() => props.highlight)` 因此会在任何一次父组件重渲染时触发——
 * 包括每 5 秒一次的诊断轮询。结果是用户正在读第 241 行，第 4 秒就被 `revealLineInCenter`
 * 拽回第 1 行（自检脚本抓到的就是这个）。所以：
 *   · 只有行号或显式跳转序号**变了**才动视口；
 *   · 显式跳转（点「打开实现」/点文件）由父组件自增 token 触发，仍然会跳；
 *   · 跳转后如果那一行没进入可见区间（面板刚切回来还没布局），保留待处理并在下一次
 *     布局变化 / 下一帧再试，而不是"跳了但看不见"。
 */
function jumpToHighlight(force = false) {
  if (!editor || !props.highlight?.start) return
  const line = Math.max(1, props.highlight.start)
  const end = Math.max(line, props.highlight.end || line)
  const column = Math.max(1, props.highlight.column || props.highlight.start_column || 1)
  const endColumn = Math.max(1, props.highlight.end_column || column)
  const token = Number(props.highlight.token ?? 0)
  if (!force && line === lastJump.value.line && column === (lastJump.value.column || 1) && token === lastJump.value.token) return
  consumedHighlightToken = token
  lastJump.value = { line, token, column }
  highlightLine.value = line
  pendingJump = { line, end, column, endColumn }
  flushPendingJump()
  if (pendingJump) requestAnimationFrame(() => flushPendingJump())
}

function save() {
  if (!activeDocument || activeDocument.key !== documentKey() || activeDocument.projectId !== props.projectId
    || activeDocument.tree !== props.tree || activeDocument.path !== props.file?.path || activeDocument.uri !== (props.file?.uri || '')) return
  if (encodingReadonly.value) {
    error.value = props.file.encoding_warning || '此文件使用非 UTF-8 编码，已只读预览以保留原始字节'
    return
  }
  if (props.readonly) {
    error.value = '当前文件只读，请开启人工编辑后保存'
    return
  }
  if (!canSave.value || !editor || !props.file) return
  const text = editor.getValue()
  publishContent()
  if (props.file.uri) {
    workspaceLanguage?.changeDocument(props.file.uri, text)
    workspaceLanguage?.flushDocument(props.file.uri)
  }
  emit('save', { key: activeDocument?.key || documentKey(), path: props.file.path, text, sha1: props.file.sha1 })
}

/** 外部内容变化（AI 改了 / 重新打开文件）：替换模型内容并复位脏标记。 */
function applyExternal(text, lang) {
  if (!editor) return
  const model = editor.getModel()
  if (!model) return
  suppress = true
  if (lang && model.getLanguageId() !== mapLanguage(lang)) {
    monacoRef.editor.setModelLanguage(model, mapLanguage(lang))
  }
  const changed = model.getValue() !== (text ?? '')
  if (changed) updateModelText(model, text ?? '')
  suppress = false
  if (activeDocument) {
    if (!(props.file?.dirty || props.file?.isDirty)) activeDocument.baseline = text ?? ''
    activeDocument.publishedText = text ?? ''
  }
  dirty.value = activeDocument?.baseline == null || model.getValue() !== activeDocument?.baseline
  if (changed) scheduleDebugDecorations()
  if (changed && props.file?.uri) workspaceLanguage?.changeDocument(props.file.uri, model.getValue())
}

function setConflict(message) {
  error.value = message
  dirty.value = true
}

/** 保存期间仍可继续输入；只复位已落盘的那一版，后续输入继续显示未保存。 */
function markSaved(text) {
  savedText = text
  if (activeDocument) activeDocument.baseline = text
  dirty.value = !!editor && editor.getValue() !== text
}

defineExpose({ applyExternal, markSaved, notifySaved, notifyResourceChanges, setConflict, jumpToHighlight, snapshot, releaseDocument, toggleBreakpoint,
    formatDocument, showDocumentation, showHierarchy, runIdeCommand, runEditorCommand, save, canSave, canEdit, commandState, focus: () => editor?.focus() })

// 分别比较身份值：保存会替换 file 元数据对象，不能因此重建编辑器或回退到陈旧的视图状态。
watch(
  [() => props.projectId, () => props.tree, () => props.documentKey, () => props.file?.uri || props.file?.path],
  async () => {
    retainPinnedDocumentation()
    hierarchy.value = lensReferences.value = null
    flushDocument()
    editorVersion += 1
    savedText = null
    pendingJump = null
    lastJump.value = { line: 0, token: -1 }
    highlightLine.value = 0
    error.value = ''
    lspPanel.value = false
    languageActionRequest += 1
    languageActionBusy.value = false; symbolPanel.value = false; symbolBusy.value = false; symbolItems.value = []
    if (workspaceLanguageScope && workspaceLanguageScope !== `${props.projectId}\0${props.tree || 'main'}`) shutdownLsp()
    lsp = null
    disposeEditor()
    await nextTick()
    mount()
  }
)
watch(() => props.file?.text, (text) => {
  if (!editor) {
    mount()
    return
  }
  // props 变化只在"内容确实不同"时覆盖，避免打字过程中被自身回写打断
  const model = editor.getModel()
  if (savedText === text) {
    savedText = null
    dirty.value = model?.getValue() !== text
    return
  }
  if (model && model.getValue() !== (text ?? '')) applyExternal(text, props.file?.lang)
})
// 磁盘基线也可能独立更新（外部保存的内容刚好等于当前草稿），内容相等时仍要复位未保存标记。
watch(() => [props.file?.diskText, props.file?.savedText], ([diskText, savedText]) => {
  if (!editor || !activeDocument || activeDocument.key !== documentKey()) return
  const baseline = diskText ?? savedText
  if (baseline === undefined) return
  activeDocument.baseline = baseline
  dirty.value = baseline == null || editor.getModel().getValue() !== baseline
})
watch(() => props.readonly, () => editor?.updateOptions({ readOnly: effectiveReadonly.value }))
watch(editorOptions, options => {
  editor?.updateOptions(options)
  for (const model of ownedModels) model.updateOptions?.({ tabSize: options.tabSize, insertSpaces: true })
})
watch(fileReadonly, () => editor?.updateOptions({ readOnly: effectiveReadonly.value }))
watch(() => props.breakpoints, renderDebugDecorations, { deep: true })
watch(() => props.debugLocation, renderDebugDecorations, { deep: true })
watch(() => props.lspServer?.available, () => {
  if (!editor) return
  connectLsp()
})
watch(() => props.languageServers, () => { refreshedLanguageCatalog.value = null }, { deep: true })
watch(() => [props.documents, props.languageServers], () => {
  if (editor) connectLsp()
  else if (workspaceLanguage) workspaceLanguage.syncDocuments(languageDocuments())
}, { deep: true })
watch(() => [props.projectId, props.tree], () => {
  lspListRequest += 1
  lspList.value = []
  refreshedLanguageCatalog.value = null
  lspListLoaded.value = false
  lspListLoading.value = false
  lspPanel.value = false
})
// 只盯**值**：对象身份每次渲染都变，盯对象就会在诊断轮询（5s）时把视口拽回去
watch(
  () => [props.highlight?.start || 0, props.highlight?.end || 0, props.highlight?.column || props.highlight?.start_column || 1, props.highlight?.end_column || 1, props.highlight?.token || 0],
  () => jumpToHighlight(),
)

onMounted(mount)
onBeforeUnmount(() => {
  closeDocumentation(false)
  flushDocument()
  disposed = true
  editorVersion += 1
  lspListRequest += 1
  if (copyTimer) clearTimeout(copyTimer)
  shutdownLsp()
  disposeEditor()
  for (const model of ownedModels) model.dispose()
  ownedModels.clear()
  documentModels.clear()
})
</script>

<template>
  <div class="pane" :class="{ 'compact-toolbar': compactToolbar }" :data-highlight-line="highlightLine || ''">
    <div v-if="!file" class="empty muted">
      <span class="empty-icon"><UiIcon name="file-code" :size="30" /></span>
      <h3>从一个文件开始</h3>
      <p>选择左侧文件，或在思维导图中浏览项目结构。</p>
      <div class="editor-shortcuts"><span><kbd>Ctrl</kbd> <kbd>P</kbd> 快速打开</span><span><kbd>Ctrl</kbd> <kbd>S</kbd> 保存文件</span></div>
      <div class="empty-sub">文件保存后，项目索引与地图会在后台更新。</div>
    </div>

    <template v-else>
      <div class="bar" :aria-label="compactToolbar ? '文件信息' : undefined">
        <span class="path mono" :title="file.path"><template v-if="compactToolbar"><template v-for="(part, index) in file.path.split(/[\\/]/)" :key="index"><UiIcon v-if="index" class="breadcrumb-separator" name="chevron-right" :size="11" /><span :class="{ 'breadcrumb-file': index === file.path.split(/[\\/]/).length - 1 }">{{ part }}</span></template></template><template v-else>{{ file.path }}</template></span>
        <span class="chip language-chip">{{ file.lang }}</span>
        <span v-if="file.secret" class="chip warn">疑似密钥</span>
        <span v-if="encodingReadonly" class="chip warn" :title="file.encoding_warning">
          {{ file.encoding === 'utf-8+gb18030' ? '混合编码' : '非 UTF-8' }}·只读
        </span>
        <span v-else-if="tooLarge" class="chip warn" title="超过 2MB：浏览器内编辑会掉帧，请用外部编辑器">过大·只读</span>
        <span v-else-if="readonly" class="chip lock" title="人工编辑未开启">只读</span>
        <span v-else-if="dirty" class="chip dirty">未保存</span>
        <span class="spacer" />
        <button
          v-if="!plainFile"
          class="lsp as-button"
          :class="lspState"
          ref="lspTrigger"
          :aria-label="lspStatusLabel"
          aria-haspopup="dialog"
          :aria-expanded="lspPanel"
          :title="`${lspStatusLabel}。${lspDetail || '点开查看语言服务状态与配置'}`"
          @click="toggleLspPanel()"
        >
          <template v-if="compactToolbar">{{ compactLspLabel }}</template>
          <template v-else-if="lspState === 'ready'">代码分析就绪</template>
          <template v-else-if="lspState === 'connecting'">代码分析连接中…</template>
          <template v-else-if="lspState === 'unavailable'">代码分析未启用 · 配置</template>
          <template v-else-if="lspState === 'error'">代码分析连接失败 · 详情</template>
          <template v-else-if="lspState === 'closed'">代码分析重连中…</template>
          <template v-else>代码分析未连接</template>
        </button>
        <span class="muted meta">
          {{ file.total_lines ?? '—' }} 行 · {{ Math.max(1, Math.round((file.size || 0) / 1024)) }}KB
        </span>
        <button class="primary sm" :class="{ 'compact-save': compactToolbar }" :disabled="!canSave" title="保存 · Ctrl+S" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </div>

      <Teleport :to="toolbarTarget" :disabled="!toolbarTarget">
        <div v-if="!compactToolbar && !plainFile && hasLanguageTools" class="language-tools" :class="{ 'in-workbench': !!toolbarTarget }" aria-label="代码工具">
          <button class="language-tool" :disabled="commandState('complete').disabled" :title="commandState('complete').reason || '代码补全 · Ctrl+Space'" @click="runIdeCommand('complete')">补全</button>
          <button class="language-tool" :disabled="commandState('parameters').disabled" :title="commandState('parameters').reason || '参数信息 · Ctrl+Shift+Space'" @click="runIdeCommand('parameters')">参数</button>
          <EditorActionMenu label="代码导航" :items="navigationCommands" @select="runIdeCommand" />
          <EditorActionMenu label="重构" :items="refactorCommands" @select="runIdeCommand" />
          <button class="language-tool" :disabled="commandState('format').disabled" :title="commandState('format').reason || '整理格式 · Ctrl+Alt+L，可撤销'" @click="runIdeCommand('format')">{{ languageActionBusy ? '整理中…' : '整理格式' }}</button>
        </div>
      </Teleport>

      <div v-if="symbolPanel" id="language-symbol-panel" class="language-symbol-panel card" aria-label="代码导航" @keydown.esc.stop="symbolPanel = false">
        <div class="symbol-head">
          <button :class="{ active: symbolMode === 'document' }" :disabled="!languageCapabilities.documentSymbolProvider" @click="setSymbolMode('document')">文件符号</button>
          <button :class="{ active: symbolMode === 'workspace' }" :disabled="!languageCapabilities.workspaceSymbolProvider" @click="setSymbolMode('workspace')">工作区符号</button>
          <span class="spacer" />
          <button class="ghost" aria-label="关闭代码导航" @click="symbolPanel = false">关闭</button>
        </div>
        <form class="symbol-search" @submit.prevent="loadSymbols">
          <input v-model="symbolQuery" aria-label="搜索符号" placeholder="搜索名称或路径" />
          <button :disabled="symbolBusy">搜索</button>
        </form>
        <div class="symbol-results" :aria-busy="symbolBusy">
          <div v-if="symbolBusy" class="symbol-empty muted">正在查找符号…</div>
          <template v-else>
            <button v-for="(item, index) in shownSymbols" :key="`${item.path}:${item.line}:${item.name}:${index}`"
              class="language-symbol-item" :style="{ paddingLeft: `${12 + item.depth * 12}px` }"
              :title="`${item.name} · ${item.path}:${item.line}`" @click="openLanguageSymbol(item)">
              <span class="symbol-name">{{ item.name }}</span><span class="symbol-location muted">{{ item.path }}:{{ item.line }}</span>
            </button>
            <div v-if="!shownSymbols.length" class="symbol-empty muted">没有找到符号</div>
          </template>
        </div>
      </div>

      <!-- Distinguish service health, installation guidance and executable paths. -->
      <div v-if="lspPanel" ref="lspPanelEl" class="lsp-panel card" role="dialog" aria-label="代码分析设置" @keydown.esc.stop="closeLspPanel">
        <div class="lsp-head">
          <span class="lsp-title"><b>代码分析设置</b><span class="lsp-language muted">{{ file.lang }}</span></span>
          <div class="lsp-head-actions">
          <button
            v-if="lspList.length"
            class="ghost tiny"
            :title="showAllLsp ? '只看与当前项目/文件相关的服务器' : '列出全部预设（含当前项目用不到的）'"
            @click="showAllLsp = !showAllLsp"
          >{{ showAllLsp ? '只看相关' : '显示全部' }}</button>
          <button class="ghost tiny" title="重新探测（刚装完服务器时用，不必重启后端）" @click="toggleLspPanel(true)">重新探测</button>
          </div>
          <button class="ghost tiny lsp-close" aria-label="关闭代码分析设置" @click="closeLspPanel">×</button>
        </div>
        <div class="lsp-runtime" aria-label="当前语言服务状态">
          <div v-for="server in runtimeServers" :key="server.id" class="lsp-runtime-row">
            <b>{{ server.name }}</b>
            <span class="lsp-runtime-state" :class="server.state" :title="server.detail">
              {{ server.state === 'ready' ? '已连接' : server.state === 'connecting' ? '启动中…' : server.state === 'error' ? '连接失败' : '已断开' }}
            </span>
            <span v-if="server.state === 'ready' && Number.isFinite(server.initializationMs)" class="muted">
              初始化 {{ (server.initializationMs / 1000).toFixed(2) }}s
            </span>
          </div>
          <div v-if="!runtimeServers.length" class="muted">当前文件尚未连接语言服务</div>
          <div v-if="lspState === 'ready'" class="lsp-features" aria-label="当前语言服务能力">
            <span v-for="feature in languageFeatures" :key="feature.key" :class="{ available: feature.available }"
              :title="feature.available ? '当前连接已提供' : '当前连接未提供'">{{ feature.label }}</span>
          </div>
        </div>
        <div v-if="lspList.length" class="lsp-catalog-summary muted">
          目录 {{ lspCounts.total }} · 命令已找到 {{ lspCounts.installed }} · 未找到 {{ lspCounts.missing }}
          <template v-if="lspCounts.disabled"> · 已禁用 {{ lspCounts.disabled }}</template>
        </div>
        <div v-if="lspListLoading" class="muted tiny pad">探测中…</div>
        <div v-else-if="!lspList.length" class="muted tiny pad">
          探测结果为空：后端可能还没扫描这个项目，或 LSP 模块未启用。
        </div>
        <ul v-else class="lsp-list">
          <li v-for="s in shownServers" :key="s.id" :data-server-id="s.id">
            <div class="lsp-server-heading">
            <span class="st" :class="s.state">
              {{ s.state === 'installed' ? '命令已找到' : s.state === 'missing' ? '未找到' : '已禁用' }}
            </span>
            <span class="nm">{{ s.name }}</span>
            <span class="langs muted tiny">{{ (s.languages || []).join('/') }}</span>
            </div>
            <div v-if="s.state !== 'installed' && s.install_hint" class="lsp-server-detail">
              <div class="lsp-detail-heading"><span class="muted tiny">安装说明</span><button class="ghost tiny" :aria-label="`复制 ${s.name} 安装说明`" @click="copyLspText(s.install_hint)">{{ lspCopied === s.install_hint ? '已复制' : '复制说明' }}</button></div>
              <p class="lsp-install-hint">{{ s.install_hint }}</p>
            </div>
            <div v-else-if="s.resolved_command" class="lsp-server-detail">
              <div class="lsp-detail-heading"><span class="muted tiny">命令路径</span><button class="ghost tiny" :aria-label="`复制 ${s.name} 命令路径`" @click="copyLspText(s.resolved_command)">{{ lspCopied === s.resolved_command ? '已复制' : '复制路径' }}</button></div>
              <code class="lsp-command muted tiny mono">{{ s.resolved_command }}</code>
            </div>
          </li>
        </ul>
        <p v-if="lspCopyError" class="lsp-copy-error tiny" role="alert">{{ lspCopyError }}</p>
        <div v-if="languageEnvironment.python" class="lsp-foot tiny">
          <b>Python 解释器</b> · {{ languageEnvironment.interpreter_source === 'project-venv' ? '项目 .venv' : '服务环境' }}
          <div class="mono lsp-interpreter-path">{{ languageEnvironment.python }}</div>
        </div>
        <div v-if="languageEnvironment.lombok && languageEnvironment.lombok.status !== 'not-required'" class="lsp-foot tiny" role="status">
          <b>Lombok {{ languageEnvironment.lombok.enabled ? '已启用' : '未启用' }}</b><span v-if="languageEnvironment.lombok.version"> · {{ languageEnvironment.lombok.version }}</span>
          <div>{{ languageEnvironment.lombok.reason }}</div>
        </div>
        <div v-if="languageEnvironment.workspace_data?.isolated" class="lsp-foot muted tiny">Java 分析缓存已按工作区隔离。</div>
        <div class="lsp-foot muted tiny">
          按当前语言连接服务，完整工程检查请运行项目构建或测试。
        </div>
      </div>

      <div v-if="error" class="err">
        {{ error }}
        <button class="ghost" aria-label="关闭编辑器提示" @click="error = ''">×</button>
      </div>

      <div ref="hostEl" class="editor" />
      <SymbolDocumentation v-if="symbolDocumentation" ref="documentationRef" :document="symbolDocumentation" :pinned="!!symbolDocumentation.pinned" @keydown="documentationKey" @toggle-pin="toggleDocumentationPin" @retry-translation="retryDocumentationTranslation" @close="closeDocumentation()" />
      <CallHierarchyPanel v-if="hierarchy" v-bind="hierarchy" @navigate="navigateHierarchy" @close="closeLanguageNavigation" />
      <aside v-if="lensReferences" class="language-symbol-panel card lens-references" aria-label="CodeLens 引用" @keydown.esc.stop="closeLanguageNavigation">
        <div class="symbol-head"><b>{{ lensReferences.title }}</b><span class="spacer" /><button class="ghost" aria-label="关闭引用" @click="closeLanguageNavigation">×</button></div>
        <div class="symbol-results">
          <button v-for="(item, index) in lensReferences.items" :key="index" class="language-symbol-item" @click="openLanguageSymbol(item); lensReferences = null"><span class="symbol-name">{{ item.path }}</span><span class="symbol-location muted">{{ item.line }}:{{ item.column }}</span></button>
          <p v-if="!lensReferences.items.length" class="symbol-empty muted">工作区内没有引用</p>
        </div>
      </aside>
    </template>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; height: 100%; position: relative; }
.editor { --ide-hover-width: 500px; }
/* Keep Monaco's placement, focus, resizing and diagnostic actions; constrain only its reading surface. */
.editor :deep(.monaco-resizable-hover) {
  border-radius: 10px;
  border-color: var(--vscode-editorHoverWidget-border, var(--border));
  box-shadow: var(--shadow-pop);
}
.editor :deep(.monaco-hover) {
  --vscode-hover-maxWidth: min(var(--ide-hover-width), calc(100vw - 40px)) !important;
  background: var(--panel);
  color: var(--text);
  border-color: var(--border);
  border-radius: 10px;
  max-width: min(var(--ide-hover-width), calc(100vw - 40px)) !important;
}
.editor :deep(.monaco-hover-content) {
  font-size: 13px !important;
  line-height: 1.6 !important;
  max-width: min(var(--ide-hover-width), calc(100vw - 40px)) !important;
}
.editor :deep(.monaco-hover .hover-row .hover-row-contents) { flex: 1; min-width: 0; }
.editor :deep(.monaco-hover .markdown-hover) { padding: 11px 14px; overflow-wrap: anywhere; }
.editor :deep(.monaco-hover .markdown-hover h1),
.editor :deep(.monaco-hover .markdown-hover h2),
.editor :deep(.monaco-hover .markdown-hover h3) { font-size: 14px; line-height: 1.5; margin: 0 0 9px; font-weight: 650; color: var(--text); }
.editor :deep(.monaco-hover .markdown-hover h4),
.editor :deep(.monaco-hover .markdown-hover h5),
.editor :deep(.monaco-hover .markdown-hover h6) { font-size: 12px; line-height: 1.5; margin: 11px 0 4px; font-weight: 600; color: var(--text-2); }
.editor :deep(.monaco-hover .markdown-hover p) { margin: 6px 0; line-height: 1.6; }
.editor :deep(.monaco-hover .markdown-hover strong) { font-weight: 600; }
.editor :deep(.monaco-hover .markdown-hover ul),
.editor :deep(.monaco-hover .markdown-hover ol) { margin: 6px 0; padding-left: 18px; }
.editor :deep(.monaco-hover .markdown-hover li) { margin: 3px 0; line-height: 1.6; }
.editor :deep(.monaco-hover .markdown-hover hr) { margin: 10px 0; border: 0; border-top: 1px solid var(--border); }
.editor :deep(.monaco-hover .markdown-hover code) { font-size: 12px; border-radius: 4px; padding: 1px 4px; background: var(--panel-soft); color: var(--text); overflow-wrap: anywhere; white-space: normal; }
.editor :deep(.monaco-hover .markdown-hover h3 code) { padding: 0; background: transparent; font-size: 14px; }
.editor :deep(.monaco-hover .markdown-hover pre) { margin: 8px 0; padding: 9px 11px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.editor :deep(.monaco-hover .markdown-hover pre code) { padding: 0; line-height: 1.6; background: transparent; white-space: pre-wrap; }
.editor :deep(.monaco-hover .markdown-hover table) { width: 100%; margin: 8px 0; border-collapse: collapse; border-spacing: 0; font-size: 12px; line-height: 1.5; }
.editor :deep(.monaco-hover .markdown-hover th),
.editor :deep(.monaco-hover .markdown-hover td) { padding: 5px 7px; border: 1px solid var(--border); text-align: left; vertical-align: top; overflow-wrap: anywhere; }
.editor :deep(.monaco-hover .markdown-hover th) { background: var(--panel-soft); color: var(--text-2); font-weight: 600; }
.editor :deep(.monaco-hover .markdown-hover td:first-child) { width: 66px; color: var(--muted); white-space: normal; }
/* The compact preview uses a label/value table, not a second pair of headings.
   Keep genuine server tables intact when they provide column names. */
.editor :deep(.monaco-hover .markdown-hover h3 ~ table thead:has(th:empty):not(:has(th:not(:empty)))) { display: none; }
.editor :deep(.monaco-hover .markdown-hover h3 ~ table:has(thead th:empty):not(:has(thead th:not(:empty))) td) { border: 0; background: transparent; padding: 3px 0; }
.editor :deep(.monaco-hover .markdown-hover h3 ~ table:has(thead th:empty):not(:has(thead th:not(:empty))) td + td) { padding-left: 9px; }
.editor :deep(.monaco-hover .markdown-hover blockquote) { margin: 10px 0 0; padding: 7px 0 0; border: 0; border-top: 1px solid var(--border); color: var(--muted); font-size: 11px; line-height: 1.5; }
.editor :deep(.monaco-hover .markdown-hover blockquote p) { margin: 0; font-size: inherit; line-height: inherit; }
.editor :deep(.monaco-hover .hover-row:first-child .markdown-hover > :first-child) { margin-top: 0; }
.editor :deep(.monaco-hover .markdown-hover > :last-child) { margin-bottom: 0; }
.editor :deep(.monaco-hover .hover-row:not(:first-child)) { border-top: 1px solid var(--vscode-editorHoverWidget-border, var(--border)); }
.editor :deep(.monaco-hover .hover-row .actions) { font-size: 11px; padding: 5px 14px; color: var(--muted); }
.editor :deep(.suggest-widget), .editor :deep(.suggest-details), .editor :deep(.parameter-hints-widget) { border-radius: 10px; border-color: var(--border); background: var(--panel); color: var(--text); box-shadow: var(--shadow-pop); }
.editor :deep(.suggest-widget) { max-width: calc(100vw - 40px); }
.editor :deep(.suggest-widget .monaco-list-row) { font-size: 13px; }
.editor :deep(.suggest-widget .details-label) { color: var(--muted); font-size: 12px; }
.editor :deep(.suggest-details) { max-width: min(500px, calc(100vw - 40px)); }
.editor :deep(.suggest-details .body) { position: relative; }
/* The same raw detail remains available to Monaco's candidate list. Its
   formatted signature appears once in our documentation card instead. */
.editor :deep(.suggest-details:has(.docs .rendered-markdown > h3:first-child > code) .header .type) { display: none !important; }
.editor :deep(.suggest-details:has(.docs .rendered-markdown > h3:first-child > code) .header) { padding: 0; margin: 0; }
.editor :deep(.suggest-details:has(.docs .rendered-markdown > h3:first-child > code) .header .codicon-close) { position: absolute; top: 7px; right: 7px; z-index: 1; color: var(--muted); }
.editor :deep(.suggest-details .docs) { margin: 0; padding: 11px 14px; font-family: var(--font-sans, system-ui, sans-serif); font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.editor :deep(.suggest-details > .monaco-scrollable-element > .body > .docs.markdown-docs) { padding: 11px 14px; }
.editor :deep(.suggest-details > .monaco-scrollable-element > .body > .docs.markdown-docs > .rendered-markdown) { padding: 0; }
.editor :deep(.suggest-details .docs h3) { padding-right: 22px; margin: 0 0 9px; font-size: 14px; line-height: 1.5; font-weight: 650; }
.editor :deep(.suggest-details .docs h3 code) { padding: 0; background: transparent; font-size: inherit; }
.editor :deep(.suggest-details .docs p) { margin: 6px 0; line-height: 1.6; }
.editor :deep(.suggest-details .docs code) { padding: 1px 4px; border-radius: 4px; background: var(--panel-soft); font-size: 12px; }
.editor :deep(.suggest-details .docs pre), .editor :deep(.suggest-details .docs .monaco-tokenized-source) { margin: 8px 0; padding: 9px 11px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.editor :deep(.suggest-details .docs .monaco-tokenized-source) { font-size: 12px !important; line-height: 1.6 !important; white-space: pre-wrap !important; overflow-wrap: anywhere; max-width: 100%; min-width: 0; box-sizing: border-box; }
.editor :deep(.suggest-details .docs .monaco-tokenized-source span) { white-space: pre-wrap !important; overflow-wrap: anywhere; }
.editor :deep(.suggest-details .docs pre code) { padding: 0; background: transparent; white-space: pre-wrap; }
.editor :deep(.suggest-details .docs ul), .editor :deep(.suggest-details .docs ol) { padding-left: 18px; margin: 6px 0; }
.editor :deep(.suggest-details .docs table) { max-width: 100%; border-collapse: collapse; font-size: 12px; }
.editor :deep(.suggest-details .docs th), .editor :deep(.suggest-details .docs td) { padding: 4px 6px; border: 1px solid var(--border); overflow-wrap: anywhere; }
.editor :deep(.suggest-details .docs blockquote) { margin: 10px 0 0; padding: 7px 0 0; border: 0; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted); }
.editor :deep(.suggest-details .docs blockquote p) { margin: 0; font-size: inherit; }
.editor :deep(.suggest-details .docs .rendered-markdown > :last-child) { margin-bottom: 0; }
@media (max-width: 700px) { .editor { --ide-hover-width: calc(100vw - 44px); } }
.empty { display: flex; flex-direction: column; gap: 12px; align-items: center; justify-content: center; height: 100%; padding: 32px; font-size: var(--fs-sm); text-align: center; background: var(--panel); }
.empty-icon { display: grid; place-items: center; width: 64px; height: 64px; margin-bottom: 5px; color: var(--accent); border-radius: 14px; background: var(--accent-soft); }
.empty h3 { margin: 0; color: var(--text); font-size: var(--fs-lg); font-weight: var(--fw-semibold); }.empty p { margin: 0; line-height: 1.8; }
.empty-sub { font-size: var(--fs-xs); color: var(--muted); margin-top: 12px; }
.editor-shortcuts { display: flex; justify-content: center; flex-wrap: wrap; gap: 20px; margin-top: 10px; font-size: var(--fs-xs); }.editor-shortcuts span { display: inline-flex; align-items: center; gap: 4px; }.editor-shortcuts kbd { background: var(--panel-soft); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; font-size: var(--fs-2xs); }

.bar {
  display: flex; align-items: center; gap: 8px; min-height: 40px; flex: none; padding: 5px 14px;
  border-bottom: 1px solid var(--border); font-size: var(--fs-xs); background: var(--panel);
}
.path { min-width: 50px; flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-2); font-size: var(--fs-xs); }
.spacer { flex: 1; }
.chip { flex: none; font-size: var(--fs-xs); padding: var(--sp-hair) var(--sp-1-5); border-radius: var(--r-sm); background: var(--hover); color: var(--muted); }
.chip.warn { background: #fef3c7; color: #92400e; }
.chip.lock { background: #eef2ff; color: #4338ca; }
.chip.dirty { background: #fef9c3; color: #854d0e; }
.meta { flex: none; font-size: var(--fs-xs); }
.lsp {
  flex: none; font-size: var(--fs-xs); padding: var(--sp-hair) var(--sp-1-5); border-radius: var(--r-sm);
  background: var(--hover); color: var(--muted);
}
.lsp.ready { background: transparent; color: var(--ok); }
.lsp.connecting { background: #eff6ff; color: var(--accent-hover); }
.lsp.error { background: #fef2f2; color: var(--err); }
.lsp.unavailable { background: #fffbeb; color: #92400e; }
/* 可点：光标与悬停反馈要和普通标签区分开，否则没人会想到它能点 */
.lsp.as-button {
  border: 1px solid transparent; cursor: pointer; font: inherit; font-size: var(--fs-xs);
}
.lsp.as-button:hover { border-color: currentColor; }
.compact-toolbar .bar { container-type: inline-size; container-name: code-editor-header; min-height: 30px; height: 30px; gap: 6px; padding: 2px 9px; flex-wrap: nowrap; }
.compact-toolbar .bar .path { display: flex; align-items: center; gap: 3px; flex: 1 1 auto; min-width: 32px; max-width: none; font-size: var(--fs-2xs); }
.compact-toolbar .bar .path > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.compact-toolbar .breadcrumb-file { color: var(--text); }
.compact-toolbar .breadcrumb-separator { flex-shrink: 0; color: var(--faint); }
.compact-toolbar .bar > .spacer { display: none; }
.compact-toolbar .bar .chip { font-size: var(--fs-2xs); padding: 1px 4px; }
.compact-toolbar .bar .lsp { max-width: 112px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 1px 4px; font-size: var(--fs-2xs); }
.compact-toolbar .bar .meta { font-size: var(--fs-2xs); }
.compact-toolbar .bar .compact-save { min-height: 23px; padding: 1px 6px; border-color: transparent; background: transparent; color: var(--accent); font-size: var(--fs-2xs); box-shadow: none; }
.compact-toolbar .bar .compact-save:disabled { color: var(--muted); opacity: .65; }
.compact-toolbar .bar .compact-save:hover:not(:disabled) { background: var(--accent-soft); }
@container code-editor-header (max-width: 520px) {
  .compact-toolbar .bar .meta, .compact-toolbar .bar .language-chip { display: none; }
  .compact-toolbar .bar .lsp { max-width: 90px; }
}
@media (max-width: 600px) {
  .pane.compact-toolbar .bar { min-height: 30px; height: 30px; padding: 2px 7px; gap: 4px; flex-wrap: nowrap; }
  .pane.compact-toolbar .bar .path { flex-basis: auto; max-width: none; }
}
.language-tools { display: flex; align-items: center; gap: var(--sp-1); min-width: 0; min-height: 32px; flex: none; padding: 2px 10px; border-bottom: 1px solid var(--border); background: var(--panel-soft); }
.language-tools.in-workbench { min-height: 28px; padding: 0; border-bottom: 0; background: transparent; }
.language-tool { padding: var(--sp-1) var(--sp-2); font-size: var(--fs-xs); min-height: 26px; background: transparent; border: 1px solid transparent; white-space: nowrap; }
.language-tool:not(:disabled):hover, .language-tool.active { background: var(--hover); border-color: var(--border); color: var(--accent); }
.lsp-interpreter-path { margin-top: 4px; overflow-wrap: anywhere; }
.language-symbol-panel { position: absolute; top: 84px; right: var(--sp-2-5); z-index: 21; width: min(460px, calc(100% - 20px)); padding: var(--sp-2); box-shadow: var(--shadow-pop); font-size: var(--fs-xs); }
.symbol-head { display: flex; align-items: center; gap: var(--sp-1); margin-bottom: var(--sp-2); }
.symbol-head button, .symbol-search button { padding: var(--sp-1) var(--sp-2); font-size: var(--fs-xs); }
.symbol-head button.active { background: var(--hover); color: var(--accent); }
.symbol-search { display: flex; gap: var(--sp-1-5); }
.symbol-search input { flex: 1; min-width: 0; font-size: var(--fs-xs); }
.symbol-results { margin-top: var(--sp-2); max-height: min(360px, 45vh); overflow: auto; }
.language-symbol-item { width: 100%; display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1-5) var(--sp-2); border: none; background: transparent; text-align: left; font-size: var(--fs-xs); }
.language-symbol-item:hover { background: var(--hover); }
.symbol-name { font-weight: var(--fw-semibold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.symbol-location { margin-left: auto; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.symbol-empty { padding: var(--sp-3); text-align: center; }
@media (max-width: 600px) {
  .meta { display: none; }.bar { gap: 6px; padding: 6px 10px; flex-wrap: wrap; }.bar .path { flex-basis: 100%; max-width: 100%; }.bar > .spacer { min-width: 0; }.bar > .chip { font-size: var(--fs-2xs); }.lsp { font-size: var(--fs-2xs); }.language-tools { overflow-x: auto; }.empty { padding: 24px; }.editor-shortcuts { gap: 12px; }
}

/* 语言服务器面板 */
.lsp-panel {
  position: absolute; right: 10px; top: 42px; z-index: 20; width: min(520px, calc(100% - 20px)); max-height: min(640px, calc(100% - 52px));
  box-sizing: border-box; overflow: auto; overscroll-behavior: contain; padding: var(--sp-2) var(--sp-2-5); font-size: var(--fs-xs); box-shadow: var(--shadow-pop);
}
.lsp-head { position: relative; display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-1-5); padding-right: 26px; flex-wrap: wrap; row-gap: var(--sp-1); }
.lsp-title { display: flex; align-items: center; gap: var(--sp-2); min-width: 0; }
.lsp-head-actions { display: flex; align-items: center; gap: var(--sp-1); flex: none; margin-left: auto; }
.lsp-head .lsp-close { position: absolute; top: 0; right: 0; }
.lsp-language { font-size: 11px; padding: 1px 5px; border-radius: 4px; background: var(--panel-soft); }
.lsp-runtime { padding: 9px 0 11px; border-bottom: 1px solid var(--border); }
.lsp-runtime-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 5px; font-size: 12px; }
.lsp-runtime-row b { font-weight: 600; }
.lsp-runtime-state { color: var(--muted); }.lsp-runtime-state.ready { color: var(--ok); }.lsp-runtime-state.connecting { color: var(--accent); }.lsp-runtime-state.error { color: var(--err); }
.lsp-features { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 9px; }
.lsp-features span { padding: 2px 6px; border: 1px solid var(--border); border-radius: 4px; color: var(--muted); font-size: 11px; }
.lsp-features .available { color: var(--text-2); background: var(--panel-soft); border-color: transparent; }
.lsp-catalog-summary { padding: 9px 0 4px; font-size: 11px; }
.lsp-command { display: block; min-width: 0; max-width: 100%; padding: 0; border-radius: 0; background: transparent; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; line-height: 1.6; user-select: text; }
.lsp-list { list-style: none; margin: 0; padding: 0; }
.lsp-list li {
  display: flex; flex-direction: column; gap: var(--sp-1-5); padding: var(--sp-2) var(--sp-0-5); border-bottom: 1px solid var(--border); min-width: 0;
}
.lsp-server-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--sp-1-5); min-width: 0; width: 100%; }
.lsp-server-detail { min-width: 0; width: 100%; }
.lsp-detail-heading { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-1); }
.lsp-detail-heading button { margin-left: auto; flex: none; font-size: var(--fs-2xs); }
.lsp-install-hint { margin: 0; color: var(--text-2); white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; user-select: text; }
.lsp-copy-error { margin: var(--sp-2) 0; color: var(--err); }
.lsp-list .st { flex: none; font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); background: var(--hover); color: #475569; }
.lsp-list .st.installed { background: #ecfdf5; color: #047857; }
.lsp-list .st.missing { background: #fffbeb; color: #92400e; }
.lsp-list .st.unavailable { background: var(--hover); color: #94a3b8; }
.lsp-list .nm { min-width: 0; font-weight: var(--fw-semibold); overflow-wrap: anywhere; }
.lsp-foot { margin-top: var(--sp-1-5); }
button.sm { padding: 3px var(--sp-2-5); font-size: var(--fs-xs); }

.err {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2);
  padding: var(--sp-1-5) var(--sp-2-5); font-size: var(--fs-xs); background: #fef2f2; color: var(--err);
  border-bottom: 1px solid var(--border);
}
.ghost { border: none; background: transparent; color: inherit; padding: 0 var(--sp-1-5); }

.editor { flex: 1; min-height: 0; }
.editor :deep(.ide-breakpoint-glyph) { background: #d95761; border-radius: 50%; width: 11px !important; height: 11px !important; margin-top: 5px; margin-left: 4px; cursor: pointer; }
.editor :deep(.ide-debug-line) { background: #eabf4530; box-shadow: inset 3px 0 #d7a932; }
.editor :deep(.ide-debug-glyph) { color: #c89516; }
.editor :deep(.ide-debug-glyph::before) { content: '➜'; font-size: 16px; line-height: 22px; }
</style>
