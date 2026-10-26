<script setup>
import UiIcon from '../ui/UiIcon.vue'
import SymbolDocumentation from './SymbolDocumentation.vue'
import CallHierarchyPanel from './CallHierarchyPanel.vue'
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
//: 「无语言服务器」这个提示要能点开：否则用户只知道"没有"，不知道"装什么、怎么装"
const lspPanel = ref(false)
const lspList = ref([])
const lspListLoading = ref(false)
const lspListLoaded = ref(false)
const lspCopied = ref('')
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
  lspPanel.value = refresh ? true : !lspPanel.value
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

async function copyInstall(command) {
  try {
    await navigator.clipboard.writeText(command)
    if (disposed) return
    lspCopied.value = command
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { lspCopied.value = '' }, 1500)
  } catch {
    lspCopied.value = ''
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
const languageCapabilities = computed(() => {
  languageRevision.value
  const capabilities = {}
  for (const client of workspaceLanguage?.clientsFor(props.file?.lang) || []) {
    for (const [key, value] of Object.entries(client.capabilities || {})) if (value) capabilities[key] = value
  }
  return capabilities
})
const hasLanguageTools = computed(() => !!(
  languageCapabilities.value.documentFormattingProvider || languageCapabilities.value.documentSymbolProvider
  || languageCapabilities.value.workspaceSymbolProvider))
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
    editorDisposables.push({ dispose: subscribeHoverDocumentationUpdates(change => { void refreshLanguageDocumentation(change) }) })
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
    if (themeSubscription) editorDisposables.push(themeSubscription)
    model.updateOptions?.({ tabSize: preferences.editorTabSize, insertSpaces: true })
    editor.onDidChangeModelContent(() => {
      if (symbolDocumentation.value) closeDocumentation(false)
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
    editorDisposables.push(editor.addAction({
      id: 'code-agent.symbol-documentation',
      label: '查看完整说明',
      contextMenuGroupId: 'navigation',
      contextMenuOrder: 3,
      run: () => showDocumentation(),
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
  symbolDocumentation.value = { ...symbolDocumentation.value, sections: result.sections || target.base.sections || [] }
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
  if (symbolDocumentation.value.loading) { target.languageRefreshNeeded = true; return false }
  return showDocumentation({ refreshTarget: target })
}

async function showDocumentation({ refreshTarget = null } = {}) {
  const client = languageClient('hoverProvider'), instance = editor, model = editor?.getModel(), context = activeDocument
  if ((!client && props.file?.lang !== 'java') || !model || !context?.uri || context.key !== documentKey()) return false
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
  const target = { model, context, position: { ...position }, current, base: null, lookup: null, entityId: '', refreshNeeded: false, languageRefreshNeeded: false }
  documentationTarget = target
  symbolDocumentation.value = { title, path: context.path, line: position.lineNumber, sections: [], loading: true, error: '' }
  if (!refreshTarget) void nextTick(() => { if (current()) documentationRef.value?.$el?.focus?.() })
  try {
    workspaceLanguage?.flushDocument(context.uri)
    const languageResult = client ? await client.hover(context.uri, position, token) : getTokenHover(model, position)
    if (!current()) return false
    target.base = languageResult
    const result = await withTokenBusiness(model, position, languageResult, token)
    if (!current()) return false
    target.lookup = result?.businessLookup || null
    target.entityId = result?.business?.entity?.id || ''
    symbolDocumentation.value = { ...symbolDocumentation.value, sections: result?.sections || [], source: result?.source, loading: false,
      ...(result?.documentation_translation ? { documentation_translation: result.documentation_translation } : {}),
      ...(result && Object.hasOwn(result, '_code_agent_original') ? { _code_agent_original: result._code_agent_original } : {}) }
    if (target.refreshNeeded) void refreshBusinessDocumentation({ projectId: context.projectId, tree: context.tree })
    if (target.languageRefreshNeeded) void refreshLanguageDocumentation({ projectId: context.projectId, tree: context.tree, uri: context.uri })
    return true
  } catch (cause) {
    if (current()) {
      symbolDocumentation.value = { ...symbolDocumentation.value, loading: false, error: cause.message || '暂时无法读取说明' }
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
  if (!client || !model || !context || context.key !== documentKey() || context.path !== props.file?.path || effectiveReadonly.value || languageActionBusy.value) return false
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

function languageEditorAction(action, capability) {
  if (activeDocument?.key !== documentKey() || effectiveReadonly.value || !languageCapabilities.value[capability] || languageActionBusy.value) return false
  editor?.focus()
  const command = editor?.getAction?.(action)
  if (!command && !editor?.trigger) return false
  const result = command ? command.run() : editor.trigger('code-agent.language', action, {})
  result?.catch?.(e => { if (!disposed) error.value = e.message })
  return true
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
  formatDocument, showDocumentation, showHierarchy, focus: () => editor?.focus() })

// 分别比较身份值：保存会替换 file 元数据对象，不能因此重建编辑器或回退到陈旧的视图状态。
watch(
  [() => props.projectId, () => props.tree, () => props.documentKey, () => props.file?.uri || props.file?.path],
  async () => {
    closeDocumentation(false)
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
  <div class="pane" :data-highlight-line="highlightLine || ''">
    <div v-if="!file" class="empty muted">
      <span class="empty-icon"><UiIcon name="file-code" :size="30" /></span>
      <h3>从一个文件开始</h3>
      <p>选择左侧文件，或在思维导图中浏览项目结构。</p>
      <div class="editor-shortcuts"><span><kbd>Ctrl</kbd> <kbd>P</kbd> 快速打开</span><span><kbd>Ctrl</kbd> <kbd>S</kbd> 保存文件</span></div>
      <div class="empty-sub">文件保存后，项目索引与地图会在后台更新。</div>
    </div>

    <template v-else>
      <div class="bar">
        <span class="path mono" :title="file.path">{{ file.path }}</span>
        <span class="chip">{{ file.lang }}</span>
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
          :title="lspDetail || '点开看这台机器上装了哪些语言服务器、缺什么、怎么装'"
          @click="toggleLspPanel()"
        >
          <template v-if="lspState === 'ready'">代码分析就绪</template>
          <template v-else-if="lspState === 'connecting'">代码分析连接中…</template>
          <template v-else-if="lspState === 'unavailable'">代码分析未启用 · 配置</template>
          <template v-else-if="lspState === 'error'">代码分析连接失败 · 详情</template>
          <template v-else-if="lspState === 'closed'">代码分析重连中…</template>
          <template v-else>代码分析未连接</template>
        </button>
        <span class="muted meta">
          {{ file.total_lines ?? '—' }} 行 · {{ Math.max(1, Math.round((file.size || 0) / 1024)) }}KB
        </span>
        <button class="primary sm" :disabled="!canSave" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </div>

      <Teleport :to="toolbarTarget" :disabled="!toolbarTarget">
        <div v-if="!plainFile && hasLanguageTools" class="language-tools" :class="{ 'in-workbench': !!toolbarTarget }" aria-label="代码工具">
          <button v-if="languageCapabilities.documentFormattingProvider" class="language-tool" :disabled="effectiveReadonly || languageActionBusy || symbolBusy"
            :title="fileReadonly ? '当前文件仅支持只读预览' : readonly ? '开启人工编辑后可整理格式' : '整理当前文件格式，可一次撤销'"
            @click="formatDocument">{{ languageActionBusy ? '整理中…' : '整理格式' }}</button>
          <button v-if="languageCapabilities.documentSymbolProvider || languageCapabilities.workspaceSymbolProvider"
            class="language-tool" :class="{ active: symbolPanel }" :aria-expanded="symbolPanel" aria-controls="language-symbol-panel"
            :disabled="languageActionBusy"
            title="查找当前文件或工作区的函数、类和变量" @click="toggleSymbolPanel">代码导航</button>
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

      <!-- 语言服务器面板：如实说明"这台机器上有没有装、缺什么、装完怎么生效"。
           只显示探测结果与安装命令，不在应用里替用户下载安装。 -->
      <div v-if="lspPanel" class="lsp-panel card">
        <div class="lsp-head">
          <b>代码分析设置</b>
          <span class="lsp-language muted">{{ file.lang }}</span>
          <span class="spacer" />
          <button
            v-if="lspList.length"
            class="ghost tiny"
            :title="showAllLsp ? '只看与当前项目/文件相关的服务器' : '列出全部预设（含当前项目用不到的）'"
            @click="showAllLsp = !showAllLsp"
          >{{ showAllLsp ? '只看相关' : '显示全部' }}</button>
          <button class="ghost tiny" title="重新探测（刚装完服务器时用，不必重启后端）" @click="toggleLspPanel(true)">重新探测</button>
          <button class="ghost tiny" aria-label="关闭代码分析设置" @click="lspPanel = false">×</button>
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
          <li v-for="s in shownServers" :key="s.id">
            <span class="st" :class="s.state">
              {{ s.state === 'installed' ? '命令已找到' : s.state === 'missing' ? '未找到' : '已禁用' }}
            </span>
            <span class="nm">{{ s.name }}</span>
            <span class="langs muted tiny">{{ (s.languages || []).join('/') }}</span>
            <span class="spacer" />
            <button
              v-if="s.state !== 'installed' && s.install_hint"
              class="ghost tiny mono hintbtn"
              :title="`复制安装命令：${s.install_hint}`"
              @click="copyInstall(s.install_hint)"
            >{{ lspCopied === s.install_hint ? '已复制' : s.install_hint }}</button>
            <span v-else-if="s.resolved_command" class="lsp-command muted tiny mono" :title="s.resolved_command">
              {{ s.resolved_command }}
            </span>
          </li>
        </ul>
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
      <SymbolDocumentation v-if="symbolDocumentation" ref="documentationRef" :document="symbolDocumentation" @close="closeDocumentation()" />
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
.editor { --ide-hover-width: 560px; }
/* Keep Monaco's placement, focus, resizing and diagnostic actions; constrain only its reading surface. */
.editor :deep(.monaco-resizable-hover) {
  border-radius: 9px;
  border-color: var(--vscode-editorHoverWidget-border, var(--border));
  box-shadow: 0 8px 26px #14213d1a, 0 2px 5px #14213d08;
}
.editor :deep(.monaco-hover) {
  --vscode-hover-maxWidth: min(var(--ide-hover-width), calc(100vw - 40px)) !important;
  background: var(--panel);
  color: var(--text);
  border-color: var(--border);
  border-radius: 9px;
  max-width: min(var(--ide-hover-width), calc(100vw - 40px)) !important;
}
.editor :deep(.monaco-hover-content) {
  font-size: 13px !important;
  line-height: 1.65 !important;
  max-width: min(var(--ide-hover-width), calc(100vw - 40px)) !important;
}
.editor :deep(.monaco-hover .hover-row .hover-row-contents) { flex: 1; }
.editor :deep(.monaco-hover .markdown-hover) { padding: 10px 14px; overflow-wrap: anywhere; }
.editor :deep(.monaco-hover .markdown-hover h1),
.editor :deep(.monaco-hover .markdown-hover h2),
.editor :deep(.monaco-hover .markdown-hover h3),
.editor :deep(.monaco-hover .markdown-hover h4),
.editor :deep(.monaco-hover .markdown-hover h5),
.editor :deep(.monaco-hover .markdown-hover h6) { font-size: 13px; line-height: 1.6; margin: 12px 0 5px; font-weight: 600; }
.editor :deep(.monaco-hover .markdown-hover p) { margin: 5px 0; line-height: 1.7; }
.editor :deep(.monaco-hover .markdown-hover ul),
.editor :deep(.monaco-hover .markdown-hover ol) { margin: 6px 0; padding-left: 20px; }
.editor :deep(.monaco-hover .markdown-hover li) { margin: 3px 0; line-height: 1.7; }
.editor :deep(.monaco-hover .markdown-hover hr) { margin: 10px 0; border-color: var(--border); }
.editor :deep(.monaco-hover .markdown-hover code) { font-size: 12px; border-radius: 4px; padding: 1px 4px; }
.editor :deep(.monaco-hover .markdown-hover pre) { margin: 0; padding: 8px 10px; border-radius: 5px; background: var(--panel-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.editor :deep(.monaco-hover .markdown-hover pre code) { padding: 0; line-height: 1.65; }
.editor :deep(.monaco-hover .hover-row:first-child .markdown-hover > :first-child) { margin-top: 0; }
.editor :deep(.monaco-hover .hover-row:not(:first-child)) { border-top: 1px solid var(--vscode-editorHoverWidget-border, var(--border)); }
.editor :deep(.monaco-hover .hover-row .actions) { font-size: 11px; padding: 5px 12px; }
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
  overflow: auto; padding: var(--sp-2) var(--sp-2-5); font-size: var(--fs-xs); box-shadow: var(--shadow-pop);
}
.lsp-head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-1-5); flex-wrap: wrap; row-gap: var(--sp-1); }
.lsp-head .spacer { flex: 1; }
.lsp-language { font-size: 11px; padding: 1px 5px; border-radius: 4px; background: var(--panel-soft); }
.lsp-runtime { padding: 9px 0 11px; border-bottom: 1px solid var(--border); }
.lsp-runtime-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 5px; font-size: 12px; }
.lsp-runtime-row b { font-weight: 600; }
.lsp-runtime-state { color: var(--muted); }.lsp-runtime-state.ready { color: var(--ok); }.lsp-runtime-state.connecting { color: var(--accent); }.lsp-runtime-state.error { color: var(--err); }
.lsp-features { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 9px; }
.lsp-features span { padding: 2px 6px; border: 1px solid var(--border); border-radius: 4px; color: var(--muted); font-size: 11px; }
.lsp-features .available { color: var(--text-2); background: var(--panel-soft); border-color: transparent; }
.lsp-catalog-summary { padding: 9px 0 4px; font-size: 11px; }
.lsp-command { flex-basis: 100%; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
.lsp-list { list-style: none; margin: 0; padding: 0; }
.lsp-list li {
  display: flex; align-items: center; gap: var(--sp-1-5); padding: var(--sp-1) var(--sp-0-5); border-bottom: 1px solid var(--border); min-width: 0; flex-wrap: wrap;
}
.lsp-list .st { flex: none; font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); background: var(--hover); color: #475569; }
.lsp-list .st.installed { background: #ecfdf5; color: #047857; }
.lsp-list .st.missing { background: #fffbeb; color: #92400e; }
.lsp-list .st.unavailable { background: var(--hover); color: #94a3b8; }
.lsp-list .nm { flex: none; font-weight: var(--fw-semibold); }
.lsp-list .spacer { flex: 1; }
.hintbtn { font-size: var(--fs-xs); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
