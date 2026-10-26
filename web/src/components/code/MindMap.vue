<script setup>
// 结构树与功能导航共用索引。
//
// 三个刻意的取舍：
// 1. **语义缩放而不是图形缩放**：深度滑块换的是"画到哪一层语义"
//    （模块 → 文件 → 符号 → 成员），不是把一张巨图缩小；
// 2. **按需展开**：2352 个符号全画出来既慢又没用，默认只画到文件层，
//    展开哪个文件才画它的符号；
// 3. **实时性靠"变化信号 + 局部补丁"**：轮询索引 change_seq（很便宜），
//    有变化就取 delta——能就地贴就贴（顺带把变更节点标黄），
//    不能贴（新增/删除文件、边变了、被截断）才整取。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../../api.js'
import { applyDelta, evidenceTokens, kindMeta, layoutFeatureTree, layoutStructure, SOURCE_LABEL } from '../../lib/mapLayout.js'
import NodeDetail from './NodeDetail.vue'
import { businessEntityId, displayBusiness, BUSINESS_SOURCE_LABEL, BUSINESS_STATUS_LABEL } from '../../lib/businessMeaningDisplay.js'
import { functionDescriptionPresentation, publishFunctionDescriptions } from '../../lib/functionDescriptions.js'

const props = defineProps({
  projectId: { type: String, required: true },
  tree: { type: String, default: 'main' },
  // 诊断概览（来自 /diagnostics）：{ seq, counts, files: {path: {...}}, symbols: {id: {...}} }
  // 这是 P3 的关键融合：语言服务器说"哪行有问题"，地图说"那行属于哪个类/函数"。
  diagnostics: { type: Object, default: null },
  // 任务符号级差异（P4）：{ added:[symbol], modified:[...], removed:[...] }
  // 地图叠加层：一眼看清"这次任务动了哪些类/函数"。
  overlay: { type: Object, default: null },
})
const emit = defineEmits(['open-file', 'select', 'ask', 'error'])

// ---- P7：覆盖率上地图（三态：直接 / 文件级 / 无）----
const showCoverage = ref(false)
const coverage = ref(null)
const coverageBusy = ref(false)

const coverageIndex = computed(() => {
  const data = coverage.value
  const index = { direct: new Set(), file: new Map(), none: new Set() }
  if (!data) return index
  for (const sid of Object.keys(data.direct_symbols || {})) index.direct.add(sid)
  for (const item of data.files || []) {
    index.file.set(item.path, item.state)
    if (item.state === 'none') index.none.add(item.path)
  }
  return index
})

/** 覆盖率三态：只有"直接覆盖"才敢说这条符号被测试盯着。 */
function coverageOf(node) {
  if (!showCoverage.value || !coverage.value) return ''
  const index = coverageIndex.value
  if (node.kind === 'file') return index.file.get(node.path) || 'none'
  if (node.kind === 'dir') return ''
  return index.direct.has(node.id) ? 'direct' : ''
}

async function loadCoverage() {
  const context = captureContext()
  coverageBusy.value = true
  try {
    const data = await api.code.coverage(context.projectId, context.tree)
    if (contextCurrent(context)) coverage.value = data
  } catch (e) {
    if (contextCurrent(context)) emit('error', e.message)
  } finally {
    if (contextCurrent(context)) coverageBusy.value = false
  }
}

async function toggleCoverage() {
  showCoverage.value = !showCoverage.value
  if (showCoverage.value && !coverage.value) await loadCoverage()
}

// ---- P7：证据角标（SRC n）——archify 的 Verified Source Beacon 思路：
// 只标"平台自己记下来的证据条数"（未回滚的台账 + 决策），只读、不进导出物。
const showEvidence = ref(false)
const evidence = ref(null)
const evidenceBusy = ref(false)

const evidenceTotal = computed(() => evidence.value?.totals?.items || 0)
function evidenceOf(node) {
  if (!showEvidence.value || !evidence.value) return 0
  const data = evidence.value
  if (node.kind === 'file') {
    const item = data.paths?.[node.path]
    return item ? item.patches + item.decisions : 0
  }
  if (node.kind === 'dir') return data.modules?.[node.path] || 0
  const sid = String(node.id || '').replace(/^sym:/, '')
  return data.symbols?.[sid] || 0
}

async function toggleEvidence() {
  showEvidence.value = !showEvidence.value
  if (showEvidence.value && !evidence.value) {
    const context = captureContext()
    evidenceBusy.value = true
    try {
      const data = await api.code.evidence(context.projectId, context.tree)
      if (contextCurrent(context)) evidence.value = data
    } catch (e) {
      if (contextCurrent(context)) emit('error', e.message)
    } finally {
      if (contextCurrent(context)) evidenceBusy.value = false
    }
  }
}

function coverageSummary() {
  const totals = coverage.value?.totals
  if (!totals) return ''
  return `直接覆盖 ${totals.direct} · 文件级 ${totals.file_level} · 未覆盖 ${totals.uncovered}`
}

// ---- 聚焦高亮：选中一个节点后，只让它的邻域保持清晰（archify 的 reachability 做法：
// 上游/下游分别着色，并给一条"节点/边/最大跳数"的回执，不喊"影响面"）----
const focus = computed(() => {
  const node = selected.value
  if (!node) return { active: false, up: new Set(), down: new Set(), edges: 0 }
  const up = new Set([node.id])
  const down = new Set([node.id])
  let edges = 0
  for (const link of layout.value.links) {
    if (link.from === node.id) {
      down.add(link.to)
      edges += 1
    } else if (link.to === node.id) {
      up.add(link.from)
      edges += 1
    }
  }
  // 结构树里 from=父（上游），to=子（下游）。
  return { active: true, up, down, edges }
})

function focusRole(node) {
  if (!focus.value.active) return ''
  if (node.id === selected.value?.id) return 'self'
  if (focus.value.down.has(node.id)) return 'down'
  if (focus.value.up.has(node.id)) return 'up'
  return 'dim'
}

/** 节点在任务差异里的角色：added / modified / removed（用于叠加着色）。 */
const overlayIndex = computed(() => {
  const index = { added: new Set(), modified: new Set(), removed: new Set() }
  const data = props.overlay
  if (!data) return index
  for (const key of ['added', 'modified', 'removed']) {
    for (const item of data[key] || []) index[key].add(item.id)
  }
  return index
})

function overlayOf(node) {
  const index = overlayIndex.value
  if (index.added.has(node.id)) return 'added'
  if (index.modified.has(node.id)) return 'modified'
  if (index.removed.has(node.id)) return 'removed'
  return ''
}

/** 节点 → 诊断计数。文件按 path 匹配，符号按 symbol_id 匹配（服务端已归好类）。 */
function diagOf(node) {
  const d = props.diagnostics
  if (!d) return null
  if (node.kind === 'file') {
    const hit = d.files?.[node.path]
    return hit && (hit.error || hit.warning) ? hit : null
  }
  if (node.kind === 'dir') return null
  const hit = d.symbols?.[node.id]
  return hit && (hit.error || hit.warning) ? hit : null
}

/**
 * 一行右侧的角标。**布局要按它们的宽度排版**（宽度算进节点宽度、从右往左摆），
 * 所以这里只负责"有哪些角标、什么文字"，位置由 mapLayout 统一算——
 * 旧版把这些角标写死在 `width-46` / `width-34` 上，两个角标一撞就盖住文件名。
 */
function badgesOf(node) {
  const out = []
  const cov = coverageOf(node)
  if (cov) {
    out.push({
      dot: true,
      cls: `tok-cov-${cov}`,
      title: { direct: '有测试直接盯着', file: '文件级覆盖', none: '没有测试覆盖' }[cov],
    })
  }
  if (node.entrypoint) {
    out.push({ text: '入口', cls: 'tok-entry', title: '入口文件（服务端按 __main__ / main() / 启动脚本识别）' })
  }
  if (node.skipped) {
    out.push({ text: '未索引', cls: 'tok-skip', title: `未参与解析：${node.skipped}` })
  }
  if (node.kind === 'dir' && node.skippedCount) {
    out.push({
      text: `未索引 ${node.skippedCount}`,
      cls: 'tok-skip',
      title: '这个目录里有密钥/二进制/超大文件未参与解析（勾选「未索引」可以画出来）',
    })
  }
  if (node.kind === 'dir' && !node.open && node.childCount) {
    out.push({ text: `${node.childCount} 项`, cls: 'tok-count', title: '目录还是折叠的：点一下展开' })
  } else if (node.kind === 'file' && !node.open && node.symbolCount) {
    out.push({ text: `${node.symbolCount}`, cls: 'tok-count', title: `${node.symbolCount} 个符号（点开看）` })
  }
  const ev = evidenceOf(node)
  if (ev) {
    out.push({ text: `SRC ${ev}`, cls: 'tok-src', bg: 'tok-bg-src', title: `已记录证据 ${ev} 条（改动台账 + 决策）` })
  }
  const d = diagOf(node)
  if (d) {
    // `diag` 这个类名是给冒烟脚本当选择器用的（P3 用 `.map svg .diag` 数诊断角标），
    // 角标从"节点外的圆圈"换成"行内的文字药丸"后仍然保留这个钩子。
    if (d.error) out.push({ text: `${d.error} 错`, cls: 'tok-err diag', bg: 'tok-bg-err', title: `${d.error} 个错误（来自语言服务器）` })
    if (d.warning) out.push({ text: `${d.warning} 警`, cls: 'tok-warn diag', bg: 'tok-bg-warn', title: `${d.warning} 个警告（来自语言服务器）` })
  }
  return out
}

// ---- P12：悬停看"这是干嘛的"（文件 / 目录 / 符号都能问）----
//
// 悬停时给一句话结论 + 几条事实：文件取作者写的模块文档串/文件头注释，目录取
// `__init__.py` 的包说明，符号取 P10 那套"作用"。都来自 `/symbol/purpose`
// （id 前缀 `file:` / `mod:` / `sym:` 由地图节点直接给出），按 id 缓存，滑过不重复请求。
const hover = ref(null)
const purposeCache = new Map()
let purposeSeq = 0
let hoverTimer = null
let hoverCloseTimer = null

function tipPos(ev) {
  const host = svgEl.value?.closest('.canvas')
  const box = host?.getBoundingClientRect()
  if (!box) return { x: 8, y: 8, maxHeight: 360 }
  const pointerY = Math.max(8, Math.min(ev.clientY - box.top, box.height - 8))
  const below = Math.max(0, box.height - pointerY - 26)
  const above = Math.max(0, pointerY - 26)
  const openBelow = below >= Math.min(260, box.height / 2) || below >= above
  const maxHeight = Math.max(40, Math.min(420, openBelow ? below : above))
  return {
    x: Math.max(8, Math.min(ev.clientX - box.left + 14, box.width - Math.min(420, box.width - 24) - 8)),
    y: openBelow ? pointerY + 18 : Math.max(8, pointerY - 18 - maxHeight),
    maxHeight,
  }
}

async function onNodeHover(node, ev) {
  holdNodeTip()
  const cached = purposeCache.get(node.id)
  hover.value = {
    id: node.id,
    node,
    name: node.label,
    kindLabel: kindMeta(node.kind).label,
    ...tipPos(ev),
    loading: !cached,
    headline: cached?.headline || '',
    facts: cached?.facts || [],
    documentation: cached?.documentation || null,
    business: cached?.business || displayBusiness(node.business),
    error: cached?.error || '',
  }
  if (cached || !props.projectId) return
  // 能力域节点既不是符号也不是文件/目录：没有"作用"接口可查，
  // 直接用它自带的事实（功能数 / 说明来自哪），别打一个必然失败的请求。
  if (node.kind === 'domain') {
    const payload = {
      headline: node.doc || '能力域（跟着包的层级走的一组功能）',
      business: displayBusiness(node.business),
      facts: [
        { label: '功能', text: `共 ${node.count || node.childCount || 0} 条` },
        ...(node.own ? [{ label: '直属', text: `${node.own} 条（这个目录下的文件）` }] : []),
        ...(node.doc_path ? [{ label: '说明来自', text: node.doc_path }] : []),
      ],
      error: '',
    }
    purposeCache.set(node.id, payload)
    if (hover.value?.id === node.id) hover.value = { ...hover.value, loading: false, ...payload }
    return
  }
  if (hoverTimer) clearTimeout(hoverTimer)
  const id = node.id
  const context = captureContext()
  const mine = mapSeq
  const purposeGeneration = purposeSeq
  // 结构事实是**本地产出**的（不再需要点一下才知道）：有没有下一层、有几条
  const structural = []
  if (node.hasKids) {
    structural.push({
      label: '细分功能',
      text: node.childCount ? `${node.childCount} 条（点 ▸ 展开）` : '有下一层（点 ▸ 展开）',
    })
  } else if (node.marker === '·') {
    structural.push({ label: '结构', text: '原子步骤，没有下一层' })
  } else if (node.marker === '≡') {
    structural.push({ label: '结构', text: '在别处已经拆开过' })
  } else if (node.marker === '↺') {
    structural.push({ label: '结构', text: '调用图里有环，不再展开' })
  }
  // 去抖：滑过地图不该打出一串请求
  hoverTimer = setTimeout(async () => {
    if (!contextCurrent(context) || mine !== mapSeq || purposeGeneration !== purposeSeq) return
    let payload
    try {
      const data = await api.code.symbolPurpose(context.projectId, id, context.tree)
      payload = {
        headline: data.headline || '',
        // 自动补的描述统一显示在“功能”；详细说明只保留源码文档。
        documentation: data.documentation || null,
        business: displayBusiness(data.business),
        facts: [...(data.facts || []), ...structural],
        error: '',
      }
    } catch (e) {
      payload = { headline: '', facts: structural, error: e.message }
    }
    if (!contextCurrent(context) || mine !== mapSeq || purposeGeneration !== purposeSeq) return
    if (!payload.error) purposeCache.set(id, payload)
    if (hover.value?.id === id) hover.value = { ...hover.value, loading: false, ...payload }
  }, 140)
}

function holdNodeTip() {
  if (hoverCloseTimer) clearTimeout(hoverCloseTimer)
  hoverCloseTimer = null
}

function onNodeLeave(immediate = false) {
  if (hoverTimer) clearTimeout(hoverTimer)
  hoverTimer = null
  holdNodeTip()
  if (immediate === true) hover.value = null
  else hoverCloseTimer = setTimeout(() => { hover.value = null; hoverCloseTimer = null }, 180)
}

async function openHoverDetail() {
  const node = hover.value?.node
  if (!node) return
  onNodeLeave(true)
  selected.value = node
  emit('select', node)
  await loadDetail(node)
}

const map = ref(null)
const layout = ref({ nodes: [], links: [], width: 800, height: 400 })
const loading = ref(false)
const error = ref('')
const scanning = ref(false)
const notice = ref('')

const view = ref('structure')          // structure | features
const depth = ref(2)                   // 1 模块骨架 / 2 顶层符号 / 3 含成员
/**
 * 视角预设：把"编程到底要关心哪几类东西"变成一键可达。
 *
 * 为什么要有它：真实仓库 3517 个符号里，函数 1474、方法 659、变量 386、钩子 357、
 * 常量 312、类 150、路由 139……"只看类/函数/变量"这种诉求用单个下拉表达不了
 * （下拉是单选，看三类要切三次），而 `kinds` 本来就是服务端过滤、支持逗号列表。
 */
const FOCUS_PRESETS = [
  { id: 'all', label: '全部', kinds: [] },
  { id: 'code', label: '代码骨架', kinds: ['class', 'interface', 'type', 'enum', 'record', 'annotation', 'component', 'function', 'method', 'constructor', 'field', 'property', 'record_component'] },
  { id: 'data', label: '数据', kinds: ['variable', 'constant', 'property', 'field', 'parameter', 'enum_member', 'record_component'] },
  { id: 'flow', label: '行为入口', kinds: ['route', 'hook', 'function', 'method', 'constructor'] },
]
const kindFilter = ref([])             // [] = 全部；否则是 kind 列表（服务端过滤）
const kindPanel = ref(false)           // 类型多选的浮层
const query = ref('')
const showHeuristic = ref(true)
//: 默认不画被跳过的文件（密钥/二进制/超大文件）：本仓 319 个文件里有 92 个是这类，
//: 全画出来等于用一堆"未索引"角标淹掉真正的代码结构。数量会在目录行上如实标注。
const showSkipped = ref(false)
const onlyChanged = ref(false)
const expanded = ref({})
const selected = ref(null)             // 选中节点（符号时右侧显示详情）
const detail = ref(null)

// ---- 功能导航（M33）----
// 数据来自 /features（能力域 → 功能）与 /features/children（点开才取的下一层）。
// 为什么按需取而不是一次拉全：功能树是**逐层拆到原子步骤**的，一次拉全会把
// 整个调用图展开成一棵树（同一段代码会被复制很多次）；点一层取一层才是"导航"。
// 开合状态单独存一份：和结构图的 expanded 混用会让切来切去把用户展开过的目录全折叠掉。
const featureMap = ref(null)           // 第一屏：能力域 + 功能
const featureKids = ref({})            // id → { children, leaf, note, summary, total }
const featureBusy = ref(false)
const featureBusyIds = ref(new Set())  // 正在取下一层的节点（界面上要看得见"在读取"）
const featureError = ref('')
const featureOpen = ref({})
const featureParent = ref({})          // 子 id → 父 id（拼祖先链用，环检测要交给服务端）
//: 默认只画"这一行能不能往下走 + 有没有测试盯着"两件事；
//: 来源与"无说明"是另外两个问题，想集中看某一维时再打开，免得每行挂四颗药丸。
const showSource = ref(false)
const showNoDoc = ref(false)

const changedIds = ref(new Set())      // 本轮增量变更的节点（临时高亮）
const changedFiles = ref([])
const zoom = ref(1)
const pan = ref({ x: 0, y: 0 })
//: 用户自己缩放过就不再自动适配（别抢他的视野）
const userZoomed = ref(false)
const svgEl = ref(null)

let pollTimer = null
let lastSeq = 0
let dragging = null
let disposed = false
let contextSeq = 0
let mapSeq = 0
let detailSeq = 0
let briefSeq = 0
let polling = false
let changedTimer = null

function captureContext() {
  return { seq: contextSeq, projectId: props.projectId, tree: props.tree }
}

function contextCurrent(context) {
  return !disposed && context.seq === contextSeq &&
    context.projectId === props.projectId && context.tree === props.tree
}

// 关闭详情/取消选择的入口调用此函数，保证旧请求不能重新打开抽屉。
function invalidateDetail() {
  detailSeq += 1
}

// ---------------------------------------------------------------------
// 取数
// ---------------------------------------------------------------------
/** 默认展开两级目录：一进来就只看到一个根节点等于没有地图。 */
function defaultExpanded(structure) {
  const out = {}
  const walk = (node, level) => {
    if (!node || node.kind !== 'dir') return
    if (level <= 1) out[node.id] = true
    for (const child of node.children || []) walk(child, level + 1)
  }
  walk(structure, 0)
  return out
}

async function loadMap({ ensure = false, keepExpand = false, syncFeatures = true } = {}) {
  const context = captureContext()
  const mine = ++mapSeq
  if (!context.projectId || disposed) return
  const requestedDepth = depth.value
  const requestedKinds = kindFilter.value.join(',')
  const currentRequest = () => contextCurrent(context) && mine === mapSeq
  loading.value = true
  error.value = ''
  try {
    const data = await api.code.map(context.projectId, {
      tree: context.tree,
      depth: requestedDepth,
      kinds: requestedKinds,
      ensure,
    })
    if (!currentRequest()) return
    const indexChanged = lastSeq > 0 && data.change_seq !== lastSeq
    map.value = data
    lastSeq = map.value.change_seq || 0
    if (!keepExpand) expanded.value = defaultExpanded(map.value.structure)
    purposeCache.clear()      // 地图整取 = 内容可能变了，悬停说明的缓存一起失效
    relayout()
    if (indexChanged && syncFeatures) await invalidateFeatureData()
  } catch (e) {
    if (!currentRequest()) return
    error.value = e.message
    map.value = null
  } finally {
    if (currentRequest()) loading.value = false
  }
}

/** 展开所有**能力域**（含子域）；更深的层要按需取，展开全部会拉整张调用图。 */
function expandDirs() {
  if (view.value === 'features') {
    const out = { 'feature-root': true }
    const walk = (domain) => {
      out[domain.id] = true
      for (const child of domain.children || []) walk(child)
    }
    for (const d of featureMap.value?.domains || []) walk(d)
    featureOpen.value = out
    return
  }
  const out = { ...expanded.value }
  const walk = (node) => {
    if (node.kind === 'dir') out[node.id] = true
    for (const child of node.children || []) walk(child)
  }
  if (map.value?.structure) walk(map.value.structure)
  expanded.value = out
}

function collapseAll() {
  if (view.value === 'features') {
    featureOpen.value = { 'feature-root': true }
    return
  }
  expanded.value = map.value?.structure ? { [map.value.structure.id]: true } : {}
}

/**
 * 取功能导航的第一屏（能力域 → 功能）。
 *
 * 为什么不复用 `/map`：功能导航不是"代码的另一种画法"，它回答的是**另一个问题**
 * （能干什么、这件事怎么拆），数据来自"作者的 docstring + 符号树 + 调用图"。
 *
 * 每次调用都带一个序号：切项目时旧请求可能还在路上，回来晚了**必须丢掉**——
 * 否则会把上一个项目的功能树画到当前项目的标题下面（真机踩到过：切项目地图没变）。
 */
let featureSeq = 0

async function loadFeatures() {
  if (!props.projectId) return
  const context = captureContext()
  const my = ++featureSeq
  featureBusy.value = true
  featureError.value = ''
  try {
    const data = await api.code.features(context.projectId, { tree: context.tree })
    if (!contextCurrent(context) || my !== featureSeq) return
    featureMap.value = data
    featureKids.value = {}
    featureParent.value = {}
    featureBusyIds.value = new Set()
    const open = { 'feature-root': true }
    for (const d of data.domains || []) {
      // 小域默认展开：一进来全是折叠行等于没画；大域先收起来，用户点开才画。
      // 用户自己开过的域保持原样（切项目时会被 resetFeatureView 清掉）。
      if (d.id in featureOpen.value) continue
      open[d.id] = (d.count || 0) <= DOMAIN_AUTO_OPEN
    }
    featureOpen.value = { ...featureOpen.value, ...open }
    relayout()
    await restoreExpandedFeatures(context, my)
  } catch (e) {
    if (!contextCurrent(context) || my !== featureSeq) return
    featureError.value = e.message
    featureMap.value = null
    emit('error', `功能导航读取失败：${e.message}`)
  } finally {
    if (contextCurrent(context) && my === featureSeq) featureBusy.value = false
  }
}

/** Restore only still-reachable branches the user had explicitly expanded. */
async function restoreExpandedFeatures(context, mine) {
  const queued = []
  const domains = [...(featureMap.value?.domains || [])]
  while (domains.length) {
    const domain = domains.shift()
    queued.push(...(domain.features || []))
    domains.push(...(domain.children || []))
  }
  const seen = new Set()
  while (queued.length && contextCurrent(context) && mine === featureSeq) {
    const batch = queued.splice(0).filter(node => {
      if (!node?.id || seen.has(node.id)) return false
      seen.add(node.id)
      return featureOpenOf(node) && node.has_children && !node.cyclic
    })
    await Promise.all(batch.map(node => loadFeatureChildren(node)))
    if (!contextCurrent(context) || mine !== featureSeq) return
    for (const node of batch) queued.push(...(featureKids.value[node.id]?.children || []))
  }
}

async function invalidateFeatureData({ keepDetail = false } = {}) {
  if (!featureMap.value && !Object.keys(featureKids.value).length && view.value !== 'features') return
  const keepOpen = { ...featureOpen.value }
  resetFeatureView({ keepDetail })
  featureOpen.value = keepOpen
  if (view.value === 'features') await loadFeatures()
}

/** Shared descriptions change prose, not the expanded code hierarchy. */
async function onBusinessChanged(event) {
  const change = event.detail || {}
  if (change.projectId !== props.projectId || (change.tree || 'main') !== props.tree || change.origin === 'mind-map') return
  if (Number.isInteger(change.revision)) lastBriefRevision = briefRevisionToken(change)
  if (!change.id) {
    await refreshFunctionViews()
    return
  }
  purposeSeq++
  purposeCache.clear()
  onNodeLeave(true)
  if (!featureMap.value && view.value !== 'features') return
  const context = captureContext()
  const mine = ++featureSeq
  const affected = new Set([...featureBusyIds.value])
  for (const [id, payload] of Object.entries(featureKids.value)) {
    if (id === change.id || (payload.children || []).some(node => businessEntityId(node) === change.id)) affected.add(id)
  }
  featureBusy.value = true
  featureError.value = ''
  try {
    const [navigation, children] = await Promise.all([
      api.code.features(context.projectId, { tree: context.tree }),
      Promise.all([...affected].map(async id => [id, await api.code.featureChildren(context.projectId, id, { tree: context.tree, visited: ancestorsOf(id) })])),
    ])
    if (!contextCurrent(context) || mine !== featureSeq) return
    featureMap.value = navigation
    featureKids.value = { ...featureKids.value, ...Object.fromEntries(children) }
    relayout()
    void loadBriefState()
  } catch (cause) {
    if (!contextCurrent(context) || mine !== featureSeq) return
    featureMap.value = null
    featureKids.value = {}
    featureError.value = `功能描述已保存，功能导航刷新失败：${cause.message}`
  } finally {
    if (contextCurrent(context) && mine === featureSeq) { featureBusy.value = false; featureBusyIds.value = new Set() }
  }
}

// 自动补功能描述由服务端调度；已有索引轮询同时读取进度，不另开定时器。
// 每个批次落库都会推进 revision，三处说明共用同一条变更通知。
const briefMissing = ref(null)
const briefLoading = ref(false)
const briefJob = ref(null)
const briefStatusError = ref('')
let lastBriefRevision = null
function briefRevisionToken(job) {
  return Number.isInteger(job?.revision) ? `${job.revision_epoch || ''}\0${job.revision}` : null
}
const briefPresentation = computed(() => functionDescriptionPresentation(briefJob.value, {
  loading: briefLoading.value, error: briefStatusError.value,
}))

async function refreshFunctionViews() {
  purposeSeq += 1
  purposeCache.clear()
  onNodeLeave(true)
  // 保持当前详情抽屉与展开路径，详情自身监听同一条共享通知刷新。
  await invalidateFeatureData({ keepDetail: true })
}

async function applyBriefState(job, context) {
  if (!job || !contextCurrent(context)) return
  const revision = briefRevisionToken(job)
  const changed = revision !== null && lastBriefRevision !== null && revision !== lastBriefRevision
  if (revision !== null) lastBriefRevision = revision
  briefJob.value = { ...job }
  briefStatusError.value = ''
  briefMissing.value = job.coverage_known && Number.isFinite(job.missing) ? job.missing : null
  if (changed) await refreshFunctionViews()
  if (!contextCurrent(context)) return
  publishFunctionDescriptions(context.projectId, context.tree, job, { origin: 'mind-map' })
}

async function loadBriefState() {
  if (!props.projectId) return
  const context = captureContext()
  const mine = ++briefSeq
  briefLoading.value = true
  try {
    const body = await api.code.indexStatus(context.projectId, context.tree)
    if (!contextCurrent(context) || mine !== briefSeq) return
    if (!body.briefs) {
      briefStatusError.value = '暂时无法读取自动补描述状态'
      return
    }
    await applyBriefState(body.briefs, context)
  } catch (cause) {
    if (contextCurrent(context) && mine === briefSeq) briefStatusError.value = cause.message || '读取失败'
  } finally {
    if (contextCurrent(context) && mine === briefSeq) briefLoading.value = false
  }
}

/** 切项目/切工作树/索引变了：功能导航的缓存全部作废。 */
function resetFeatureView({ keepDetail = false } = {}) {
  featureSeq += 1                          // 让在途请求的结果作废
  purposeSeq += 1
  featureMap.value = null
  featureKids.value = {}
  featureParent.value = {}
  featureOpen.value = {}
  featureBusyIds.value = new Set()
  featureError.value = ''
  hover.value = null
  purposeCache.clear()
  if (detail.value && !keepDetail) closeDetail()
  if (view.value === 'features') relayout()
}

/** 一个功能节点的下一层（细分功能），点开才取。 */
async function loadFeatureChildren(node) {
  if (!props.projectId || node.kind === 'domain') return
  if (featureKids.value[node.id]) return
  const context = captureContext()
  const mine = featureSeq
  const busy = new Set(featureBusyIds.value)
  busy.add(node.id)
  featureBusyIds.value = busy
  try {
    const payload = await api.code.featureChildren(context.projectId, node.id, {
      tree: context.tree,
      visited: ancestorsOf(node.id),
    })
    if (!contextCurrent(context) || mine !== featureSeq) return
    featureKids.value = { ...featureKids.value, [node.id]: payload }
    const parents = { ...featureParent.value }
    for (const child of payload.children || []) {
      if (!parents[child.id]) parents[child.id] = node.id
    }
    featureParent.value = parents
    relayout()
  } catch (e) {
    if (!contextCurrent(context) || mine !== featureSeq) return
    featureKids.value = {
      ...featureKids.value,
      [node.id]: { id: node.id, children: [], leaf: true, note: e.message },
    }
  } finally {
    if (contextCurrent(context) && mine === featureSeq) {
      const rest = new Set(featureBusyIds.value)
      rest.delete(node.id)
      featureBusyIds.value = rest
    }
  }
}

/** 当前节点在树上的祖先链（环检测交给服务端：出现在链上的节点会被标 cyclic）。 */
function ancestorsOf(id) {
  const out = []
  let cur = featureParent.value[id]
  const guard = new Set([id])
  while (cur && !guard.has(cur) && out.length < 40) {
    guard.add(cur)
    out.push(cur)
    cur = featureParent.value[cur]
  }
  return out
}

//: 一个能力域小到什么程度就默认展开：太大先收起来，点开才画。
//: 能力域现在**跟着包的层级嵌套**，所以 `code_agent`（整棵 400+）默认收起，
//: 而 `code_agent/agent`（16）这种子包直接摊开——一进来就能看到该看的东西。
const DOMAIN_AUTO_OPEN = 20

/** 这个节点是展开的还是折叠的：能力域看规模默认，功能默认折叠（要一层层点下去）。 */
function featureOpenOf(node) {
  const id = node.id
  if (id in featureOpen.value) return !!featureOpen.value[id]
  if (id === 'feature-root') return true
  if (node.kind === 'domain') return (node.count || 0) <= DOMAIN_AUTO_OPEN
  return false
}

/**
 * 一个能力域节点：它的**子域**（同一个包下的子包）排在前面，然后才是直接挂在这个
 * 目录下的功能。为什么要子域在前：读的时候先看"这一包里还能分成哪几块"，
 * 再看"这个目录自己还有什么"，比反过来更接近人理解代码结构的方式。
 */
function buildDomainNode(domain, depth, placed) {
  const id = domain.id
  const open = featureOpenOf({ id, kind: 'domain', count: domain.count })
  const tokens = [
    {
      text: `${domain.count || 0} 项`,
      cls: 'tok-count',
      title: '这一支下的功能数（含子域）',
    },
  ]
  const kids = domain.children || []
  if (kids.length && domain.own) {
    tokens.push({ text: `直属 ${domain.own}`, cls: 'tok-count', title: '直接挂在这个目录下的功能' })
  }
  // 说明、截断这类"看一次就够"的事实进悬停；常驻的只留数字
  if (domain.truncated) {
    tokens.push({
      text: '截断',
      cls: 'tok-skip',
      title: `这个目录下还有 ${Math.max(0, (domain.own || 0) - (domain.shown || 0))} 条没列出`,
    })
  }
  if (domain.note) tokens.push({ text: '到此为止', cls: 'tok-skip', title: domain.note })

  const built = {
    id,
    kind: 'domain',
    name: domain.name,
    label: domain.label || domain.name,
    doc: domain.doc || '',
    doc_path: domain.doc_path || '',
    business: displayBusiness(domain.business),
    count: domain.count || 0,
    own: domain.own || 0,
    node: domain,
    open,
    hasKids: (domain.count || 0) > 0,
    childCount: domain.count || 0,
    loading: false,
    tokens,
    children: [],
  }
  if (!open) return built
  const nextPlaced = new Set(placed)
  nextPlaced.add(id)
  built.children = [
    ...kids.map((child) => buildDomainNode(child, depth + 1, nextPlaced)),
    ...(domain.features || []).map((f) => buildFeatureNode({ ...f, kind: 'feature' }, depth + 1, nextPlaced)),
  ]
  return built
}

/**
 * 把「能力域 + 已取回的下一层 + 开合状态」装配成布局要的树。
 *
 * 同时做三件界面上的事：
 * - `shared`：同一个符号在别处已经拆过（两条功能都调用同一个工具），再拆一遍会把
 *   导航变成森林——标出来，默认不再展开；
 * - `loading`：正在取下一层的节点要看得见"读取中"，否则点了没反应像卡住；
 * - 角标：证据三态 / 无说明 / 来源 / 环 / 原子，都是"这行能判断什么"的信息。
 */
function buildFeatureNode(node, depth, placed) {
  const id = node.id
  const shared = placed.has(id)
  const open = shared ? false : featureOpenOf(node)
  const loading = featureBusyIds.value.has(id)
  // 一行只留三样：类型块（模板里那个 chip）、证据点、caret 位的结构记号。
  // 其余（来源、无说明）默认收进悬停，想集中看某一维时再用上面的开关打开。
  const tokens = [...evidenceTokens(node)]
  if (loading) tokens.push({ text: '读取中…', cls: 'tok-count' })
  // 模型补的功能名：**必须一直标出来**，不能和作者写的 docstring 混在一起
  if (node.label_from === 'brief') {
    const brief = node.brief || {}
    tokens.push({
      text: 'AI补',
      cls: 'tok-skip',
      title: `这条功能名是模型补的（作者没写 docstring）：${brief.text || ''}` +
        (brief.model ? `\n模型：${brief.model}` : '') +
        '\n按符号内容哈希缓存：代码一改自动失效',
    })
  }
  if (showNoDoc.value && !node.has_doc && !node.brief) {
    tokens.push({ text: '无说明', cls: 'tok-skip', title: '这个符号没有 docstring：功能名只能用符号名' })
  }
  if (showSource.value) {
    const src = SOURCE_LABEL[node.source] || node.source
    if (src) tokens.push({ text: src, cls: 'tok-count', title: `来源：${node.source_label || src}` })
  }

  let marker = ''
  let markerTitle = ''
  //: `has_children` 现在是**事先算准**的（后端复用同一份步骤规则），不再是"函数大概能拆"的
  //: 猜测：所以 `·`（原子）在第一屏就看得见，不用点一下才知道；反过来，`▸` 承诺的下一层
  //: 一定存在。取回下一层后若发现和承诺不一致，以实际结果为准（顺手纠正）。
  const leaf = featureKids.value[id]?.leaf === true
  if (node.cyclic) {
    marker = '↺'
    markerTitle = '静态调用图里有环：它在本条路径上已经出现过，不再展开'
  } else if (shared) {
    marker = '≡'
    markerTitle = '这块功能在别处已经拆开过，这里不再重复展开'
  } else if (!node.has_children || leaf) {
    marker = '·'
    markerTitle =
      featureKids.value[id]?.note ||
      '到这一层没有可再拆的东西了（原子步骤）：点它看「实现」页里每一步做了什么'
  }

  const built = {
    id,
    kind: 'feature',
    name: node.name,
    label: node.label || node.name || id,
    path: node.path,
    line: node.line,
    end_line: node.end_line,
    business: displayBusiness(node.business),
    node,
    open,
    hasKids: !!node.has_children && !node.cyclic && !shared && !leaf,
    marker,
    markerTitle,
    childCount: node.child_count || 0,    loading,
    tokens,
    children: [],
  }

  if (!open) return built
  const nextPlaced = new Set(placed)
  nextPlaced.add(id)
  built.children = (featureKids.value[id]?.children || []).map((child) =>
    buildFeatureNode(child, depth + 1, nextPlaced)
  )
  return built
}

/** 功能导航树（根 → 能力域 → 子域 → 功能 → …），随开合状态与已取回的层重建。 */
const featureTree = computed(() => {
  const nav = featureMap.value
  if (!nav) return null
  const domains = (nav.domains || []).filter((d) => (d.count || 0) > 0)
  const root = {
    id: 'feature-root',
    kind: 'domain',
    name: '全部功能',
    label: `功能（${nav.totals?.features || 0}）`,
    count: nav.totals?.found || nav.totals?.features || 0,
    doc: nav.note || '',
  }
  const built = buildFeatureNode(root, 0, new Set())
  built.kind = 'domain'
  built.label = root.label
  built.hasKids = domains.length > 0
  built.childCount = root.count
  built.tokens = [
    { text: `${nav.totals?.domains || domains.length} 个能力域`, cls: 'tok-count' },
    { text: `${nav.totals?.features || 0} 条功能`, cls: 'tok-count' },
  ]
  const placed = new Set(['feature-root'])
  built.children = domains.map((d) => buildDomainNode(d, 1, placed))
  return built
})

async function refresh(full = true) {
  const context = captureContext()
  if (!context.projectId || disposed) return
  scanning.value = true
  notice.value = ''
  try {
    await api.code.scan(context.projectId, { full, tree: context.tree })
    if (!contextCurrent(context)) return
    await loadMap({ syncFeatures: false })
    if (!contextCurrent(context)) return
    await invalidateFeatureData()
    if (!contextCurrent(context)) return
    notice.value = full ? '已全量重建索引' : '已增量更新索引'
  } catch (e) {
    if (contextCurrent(context)) error.value = e.message
  } finally {
    if (contextCurrent(context)) scanning.value = false
  }
}

/** 轮询变更信号：change_seq 变了才做事（比定时整取地图便宜得多）。 */
async function poll() {
  if (!props.projectId || disposed || loading.value || polling) return
  const context = captureContext()
  const mine = mapSeq
  const briefRequest = ++briefSeq
  const currentPoll = () => contextCurrent(context) && mine === mapSeq
  let statusRead = false
  polling = true
  try {
    const status = await api.code.indexStatus(context.projectId, context.tree)
    if (!currentPoll()) return
    statusRead = true
    if (briefRequest === briefSeq) {
      if (status.briefs) {
        await applyBriefState(status.briefs, context)
        if (!currentPoll()) return
      } else briefStatusError.value = '暂时无法读取自动补描述状态'
    }
    if (status.scanning) {
      scanning.value = true
      return
    }
    scanning.value = false
    if (!status.change_seq || status.change_seq === lastSeq) return
    const delta = await api.code.mapDelta(context.projectId, lastSeq, context.tree)
    if (!currentPoll()) return
    lastSeq = delta.to_seq
    const patched = applyDelta(map.value, delta)
    // 连线或文件集合改变时整取地图，保留改动文件供临时高亮。
    changedFiles.value = [...new Set([
      ...(delta.changes || []).map(change => change.path),
      ...(delta.added || []).map(node => node.path),
      ...(delta.updated || []).map(node => node.path),
      ...(delta.removed || []).map(id => String(id).replace(/^sym:/, '').split('#')[0]),
      ...patched.changedFiles,
    ].filter(Boolean))]
    changedIds.value = new Set([
      ...patched.added, ...patched.updated, ...patched.removed,
      ...changedFiles.value.map(path => `file:${path}`),
    ])
    if (changedTimer !== null) clearTimeout(changedTimer)
    changedTimer = setTimeout(() => {
      changedTimer = null
      if (contextCurrent(context)) changedIds.value = new Set()
    }, 3000)
    if (patched.patched) {
      notice.value = `已同步 ${changedFiles.value.length} 个文件的改动` +
        (patched.added.size ? `（新增 ${patched.added.size}）` : '')
      relayout()
    } else {
      const rebuilding = loadMap({ keepExpand: true, syncFeatures: false })
      const rebuildSeq = mapSeq
      await rebuilding
      if (!contextCurrent(context) || rebuildSeq !== mapSeq) return
      notice.value = '地图已随代码更新'
    }
    // 代码变了，功能导航也跟着过期：丢掉缓存，正在看这一页就重取
    // （服务端按 change_seq 缓存，所以这里重取的代价很小）
    if (featureMap.value || Object.keys(featureKids.value).length) {
      await invalidateFeatureData()
    }
  } catch (cause) {
    // 状态读失败不能显示为“描述已齐”；地图仍可浏览，下次轮询再检查。
    if (currentPoll() && briefRequest === briefSeq && !statusRead) briefStatusError.value = cause.message || '读取失败'
  } finally {
    if (contextCurrent(context) && briefRequest === briefSeq) briefLoading.value = false
    polling = false
  }
}

onMounted(async () => {
  window.addEventListener('code-agent:business-meaning-changed', onBusinessChanged)
  await loadMap({ ensure: true })
  if (disposed) return
  loadBriefState()
  pollTimer = setInterval(poll, 2500)

})
onBeforeUnmount(() => {
  window.removeEventListener('code-agent:business-meaning-changed', onBusinessChanged)
  disposed = true
  contextSeq += 1
  mapSeq += 1
  briefSeq += 1
  featureSeq += 1
  invalidateDetail()
  if (pollTimer !== null) clearInterval(pollTimer)
  if (changedTimer !== null) clearTimeout(changedTimer)
  onNodeLeave(true)
  onPointerUp()
})
// 切项目 / 切工作树：功能导航必须跟着换。旧版只在"缓存为空"时取一次，
// 于是切了项目这张图还是上一个项目的（真机反馈：功能思维导图没有变）。
watch([() => props.projectId, () => props.tree], () => {
  contextSeq += 1
  invalidateDetail()
  selected.value = detail.value = null
  onlyChanged.value = false
  userZoomed.value = false
  map.value = null
  relayout()
  lastSeq = 0
  loading.value = false
  scanning.value = false
  featureBusy.value = false
  briefLoading.value = false
  briefMissing.value = null
  coverage.value = evidence.value = null
  coverageBusy.value = evidenceBusy.value = false
  changedIds.value = new Set()
  changedFiles.value = []
  lastBriefRevision = null
  briefJob.value = null
  briefStatusError.value = ''
  onNodeLeave(true)
  if (changedTimer !== null) clearTimeout(changedTimer)
  changedTimer = null
  resetFeatureView()
  loadMap({ ensure: true })
  loadBriefState()
  if (view.value === 'features') loadFeatures()
})

// ---------------------------------------------------------------------
// 布局
// ---------------------------------------------------------------------
function relayout() {
  if (view.value === 'features') {
    layout.value = featureTree.value
      ? layoutFeatureTree(featureTree.value)
      : { nodes: [], links: [], width: 800, height: 400, clusters: [] }
    if (!userZoomed.value) nextTick(() => fitView())
    return
  }
  if (!map.value) {
    layout.value = { nodes: [], links: [], width: 800, height: 400, clusters: [] }
    return
  }
  layout.value = layoutStructure(map.value, {
          expanded: expanded.value,
          kinds: kindFilter.value.length ? kindFilter.value : null,
          query: query.value,
          showHeuristic: showHeuristic.value,
          showSkipped: showSkipped.value,
          autoExpand: kindFilter.value.length > 0,
          badgeOf: badgesOf,
        })
  // 布局换了就自动适配一次——否则切 depth/投影后会停在上一张图的缩放上，
  // 看到的是"一堆挤在一起的框"（用户反馈的"不够清晰"有一半来自这里）。
  // 用户自己缩放/平移过就不再抢他的视野，只在他点「适配」时回到默认。
  if (!userZoomed.value) {
    nextTick(() => fitView())
  }
}
// 只影响本地产出的开关：重排即可
watch([view, query, showHeuristic, showSkipped, onlyChanged], () => {
  userZoomed.value = false
  relayout()
})
watch(view, (v) => {
  closeDetail()
  onlyChanged.value = false
  kindPanel.value = false
  onNodeLeave(true)
})
// 切到功能导航才取第一屏（另一份数据集，不在 /map 里）
watch(view, (v) => { if (v === 'features' && !featureMap.value) loadFeatures() })
// 功能导航：开合状态、已取回的下一层、正在读取的节点，任何一项变了都要重排
watch([featureOpen, featureKids, featureBusyIds], relayout, { deep: true })
// 这两个开关只改"画什么角标"，重排即可
watch([showSource, showNoDoc], relayout)
// 角标宽度参与节点宽度，所以这些角标一变就必须重排——否则角标会压到文件名上。
// **只盯内容标识，不盯对象身份**：诊断是父组件每 5 秒轮询后新赋的对象，
// 盯对象等于每 5 秒白重排一次整张图（3000 个节点），还会把"用户没动过就自动适配"的
// 视野重置一遍——和编辑器那个"自动跳"是同一类毛病。
watch(
  [
    showCoverage,
    showEvidence,
    () => props.diagnostics?.seq || 0,
    () => {
      const o = props.overlay
      if (!o) return ''
      return [
        (o.added || []).length,
        (o.modified || []).length,
        (o.removed || []).length,
        (o.changes || []).length,
      ].join(':')
    },
  ],
  () => relayout(),
)
// depth / kinds 是**服务端过滤**（决定下发的符号集合），必须重新取数——
// 只重排会出现"切到 depth=1 却还画着 depth=3 的符号"这种自相矛盾的界面
watch([depth, kindFilter], () => loadMap({ keepExpand: true }))
watch(expanded, relayout, { deep: true })

const stats = computed(() => map.value?.stats || {})
const warnings = computed(() => map.value?.warnings || [])
const scanInfo = computed(() => map.value?.scan || {})
/** 被跳过的文件总数（secret/too_large/binary）：结构树默认不画，但必须如实显示数量。 */
const skippedTotal = computed(() =>
  Object.values(scanInfo.value.skipped || {}).reduce((sum, n) => sum + Number(n || 0), 0),
)
const visibleNodes = computed(() =>
  onlyChanged.value
    ? layout.value.nodes.filter((n) => changedIds.value.has(n.id))
    : layout.value.nodes
)
const visibleLinks = computed(() => {
  const ids = new Set(visibleNodes.value.map(node => node.id))
  return layout.value.links.filter(link => ids.has(link.from) && ids.has(link.to))
})
const changedNode = node => changedIds.value.has(node.id)
watch(changedIds, ids => { if (!ids.size && onlyChanged.value) onlyChanged.value = false })
const nodeById = computed(() => {
  const out = new Map()
  for (const node of layout.value.nodes) out.set(node.id, node)
  return out
})

const allKinds = computed(() => {
  const kinds = map.value?.stats?.kinds || {}
  return Object.entries(kinds)
    .sort((a, b) => b[1] - a[1])
    .map(([kind, count]) => ({ kind, count, meta: kindMeta(kind) }))
})

/** 当前类型过滤命中哪个预设（用于高亮）；不命中就都不亮。 */
const focusedPreset = computed(() => {
  const current = kindFilter.value
  const same = (list) => list.length === current.length && list.every((k, i) => k === current[i])
  return FOCUS_PRESETS.find((p) => same(p.kinds))?.id || ''
})

const kindLabel = computed(() => kindFilter.value.map((k) => kindMeta(k).label).join('、'))

function applyPreset(preset) {
  kindFilter.value = [...preset.kinds]
  kindPanel.value = false
}

/** 多选：勾选顺序无所谓，但按"符号多的排前面"稳定下来，避免每次过滤条件字符串都不一样。 */
function toggleKind(kind) {
  const set = new Set(kindFilter.value)
  if (set.has(kind)) set.delete(kind)
  else set.add(kind)
  kindFilter.value = allKinds.value.map((k) => k.kind).filter((k) => set.has(k))
}

// ---------------------------------------------------------------------
// 交互
// ---------------------------------------------------------------------
function toggleNode(node) {
  if (!node.hasKids) return
  // 功能导航的开合状态单独一份：切回结构图时用户展开过的目录不该被顺手改掉
  if (view.value === 'features') {
    const id = node.id
    const next = !nodeOpen(node)
    featureOpen.value = { ...featureOpen.value, [id]: next }
    // 展开 = 取下一层（点一层取一层，才能一直拆到原子步骤）
    if (next) loadFeatureChildren(node.node || node)
    return
  }
  // 类型过滤下文件/目录是"默认展开"的，所以不能简单取反——要按同一个判定函数翻转
  expanded.value = { ...expanded.value, [node.id]: !nodeOpen(node) }
}

/** 与布局同一套判定：类型过滤生效时默认展开，只有显式折叠过（false）才收起。 */
function nodeOpen(node) {
  if (!node.hasKids) return false
  if (view.value === 'features') {
    // 布局按"根展开、小域展开、功能默认折叠"决定，口径必须一致，
    // 否则 caret 的朝向和实际画出来的子节点会相反（踩过：域显示 ▾ 却没有一条功能）
    const id = node.id
    if (id in featureOpen.value) return !!featureOpen.value[id]
    if (id === 'feature-root') return true
    if (node.kind === 'domain') return (node.childCount || 0) <= DOMAIN_AUTO_OPEN
    return false
  }
  const flag = expanded.value[node.id]
  if (kindFilter.value.length && (node.kind === 'file' || node.kind === 'dir')) return flag !== false
  return !!flag
}

async function onNodeClick(node) {
  // 名称打开详情；右侧箭头独立展开，目录与能力域也有共享功能描述。
  selected.value = node
  emit('select', node)
  // 文件与符号都先看说明；详情中的「打开源码」才进入编辑器。
  onNodeLeave(true)
  await loadDetail(node)
}

async function loadDetail(node) {
  const context = captureContext()
  const mine = ++detailSeq
  if (disposed) return
  detail.value = null
  const id = typeof node === 'string' ? node : (node && node.id) || ''
  if (!id) return
  // **只有符号节点**才走符号级接口。模块节点（`mod:...`，按路径前缀聚合）与目录/文件节点
  // 在索引里不是"符号"，调 `api.code.symbol` 会拿到 422「符号不存在」——抽屉只能把它当错误
  // 显示成一行红字（真机踩到：点一个模块节点，抽屉头顶是"符号不存在: mod:code_agent/web"）。
  // 这类节点直接用**节点自身的数据**（它的规模、成员数本来就在节点上），
  // 其余内容（作用 / 契约 / 可达 / 决策 / 溯源）由抽屉各自按需拉，都能吃模块 id。
  if (!String(id).startsWith('sym:')) {
    const data = typeof node === 'object' && node ? { ...node } : { id }
    if (String(id).startsWith('file:')) {
      const pending = [map.value?.structure]
      let file
      while (pending.length) {
        const candidate = pending.pop()
        if (!candidate) continue
        if (candidate.id === id) { file = candidate; break }
        pending.push(...(candidate.children || []))
      }
      const symbols = file?.symbols || []
      const ids = new Set(symbols.map(symbol => symbol.id))
      data.kind = 'file'
      data.path ||= String(id).slice(5)
      data.name ||= data.label || data.path.split('/').at(-1)
      data.members = symbols.filter(symbol => !symbol.parent || !ids.has(symbol.parent))
    }
    detail.value = { ...data, id, tree: context.tree }
    return
  }
  try {
    const result = await api.code.symbol(context.projectId, id, { tree: context.tree })
    if (contextCurrent(context) && mine === detailSeq) detail.value = { ...result, tree: context.tree }
  } catch (e) {
    if (contextCurrent(context) && mine === detailSeq) detail.value = { id, tree: context.tree, error: e.message }
  }
}

function closeDetail() {
  invalidateDetail()
  detail.value = null
  selected.value = null
}

/** 搜索定位：展开命中路径并居中到第一个可见命中节点。 */
async function locate() {
  const q = query.value.trim().toLowerCase()
  if (!q) return
  const hit = layout.value.nodes.find((n) => (n.label || '').toLowerCase().includes(q))
  if (!hit) {
    notice.value = '没有匹配的节点（试试切换深度或清空类型过滤）'
    return
  }
  userZoomed.value = true
  centerOn(hit)
}

function centerOn(node) {
  const el = svgEl.value
  if (!el) return
  const box = el.getBoundingClientRect()
  pan.value = {
    x: box.width / 2 - (node.x + node.width / 2) * zoom.value,
    y: box.height / 2 - (node.y + node.height / 2) * zoom.value,
  }
}

function onWheel(ev) {
  ev.preventDefault()
  const el = svgEl.value
  if (!el) return
  const box = el.getBoundingClientRect()
  const mx = ev.clientX - box.left
  const my = ev.clientY - box.top
  const next = Math.min(2.5, Math.max(0.06, zoom.value * (ev.deltaY > 0 ? 0.9 : 1.1)))
  // 以光标为锚点缩放：否则一放大就"跑飞"，用户得反复找位置
  pan.value = {
    x: mx - ((mx - pan.value.x) / zoom.value) * next,
    y: my - ((my - pan.value.y) / zoom.value) * next,
  }
  zoom.value = next
  userZoomed.value = true          // 用户接管了视野，之后不再自动适配
}

function onPointerDown(ev) {
  if (ev.button !== 0) return
  dragging = { x: ev.clientX, y: ev.clientY, px: pan.value.x, py: pan.value.y }
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', onPointerUp)
}
function onPointerMove(ev) {
  if (!dragging) return
  if (Math.abs(ev.clientX - dragging.x) + Math.abs(ev.clientY - dragging.y) < 3) return
  userZoomed.value = true
  pan.value = { x: dragging.px + (ev.clientX - dragging.x), y: dragging.py + (ev.clientY - dragging.y) }
}
function onPointerUp() {
  dragging = null
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
}

function fitView(mode = 'width') {
  const el = svgEl.value
  if (!el || !layout.value.nodes.length) return
  const box = el.getBoundingClientRect()
  const widthScale = box.width / (layout.value.width + 40)
  const heightScale = box.height / (layout.value.height + 40)
  // 默认保留可读字号；全览允许继续缩小，保证大图也能完整进入视口。
  let scale = mode === 'all' ? Math.min(widthScale, heightScale) : widthScale
  scale = Math.min(1.2, Math.max(mode === 'all' ? Number.EPSILON : 0.5, scale))
  zoom.value = scale
  const contentW = layout.value.width * scale
  const contentH = layout.value.height * scale
  // 横向：装得下就居中，装不下就从左边留一点余量
  const x = contentW + 24 < box.width ? Math.round((box.width - contentW) / 2) : 12
  // 纵向：**以根节点为中心**。思维导图的根在整棵树的垂直中线上，
  // 如果像以前那样把视野钉在左上角，打开地图看到的是一堆中间层节点，
  // 会让人以为根节点和图对不上。
  const rootY = (layout.value.rootY ?? layout.value.height / 2) * scale
  const y =
    contentH + 24 < box.height
      ? Math.round((box.height - contentH) / 2)
      : Math.round(Math.min(12, Math.max(box.height - contentH - 12, box.height / 2 - rootY)))
  pan.value = { x, y }
  userZoomed.value = false
  notice.value = mode === 'all' ? '已全览（字会变小，可点「适应宽度」）' : ''
}

async function copyDigest() {
  try {
    const body = await api.code.digest(props.projectId, 6000, props.tree)
    if (navigator.clipboard) await navigator.clipboard.writeText(body.text)
    notice.value = `已复制地图摘要（${body.chars} 字符）`
  } catch (e) {
    error.value = e.message
  }
}

async function verify() {
  notice.value = '保真度校验中…'
  try {
    const body = await api.code.reindex(props.projectId, props.tree)
    notice.value = body.ok
      ? `保真度 100%（校验 ${body.checked} 个文件，索引与磁盘完全一致）`
      : `保真度 ${(body.fidelity * 100).toFixed(1)}%：${body.drift_count} 处漂移（前 3 处：${
          body.drift.slice(0, 3).map((d) => `${d.path} — ${d.reason}`).join('；')
        }）`
  } catch (e) {
    notice.value = ''
    error.value = e.message
  }
}

defineExpose({ refresh, verify, loadMap, poll })
</script>

<template>
  <div class="map">
    <div class="bar">
      <div class="seg">
        <button :class="{ active: view === 'structure' }" @click="view = 'structure'">结构</button>
        <button
          :class="{ active: view === 'features' }"
          title="功能视图：这个项目能干什么、干这件事的代码在哪"
          @click="view = 'features'"
        >功能</button>
      </div>

      <label v-if="view === 'structure'" class="ctl">
        深度
        <select v-model.number="depth">
          <option :value="1">1 · 模块</option>
          <option :value="2">2 · 符号</option>
          <option :value="3">3 · 成员</option>
        </select>
      </label>
      <div v-if="view === 'structure'" class="ctl kinds">
        <span>视角</span>
        <button
          v-for="p in FOCUS_PRESETS"
          :key="p.id"
          class="ghost tiny"
          :class="{ on: focusedPreset === p.id }"
          :title="p.kinds.length ? `只看：${p.kinds.map((k) => kindMeta(k).label).join('、')}` : '不过滤类型'"
          @click="applyPreset(p)"
        >{{ p.label }}</button>
        <button
          class="ghost tiny"
          :class="{ on: kindPanel || kindFilter.length }"
          title="按符号类型多选过滤（服务端过滤，会重新取数）"
          @click="kindPanel = !kindPanel"
        >类型{{ kindFilter.length ? ` ${kindFilter.length}` : '' }} ▾</button>
      </div>
      <label v-if="view === 'structure'" class="ctl">
        <input v-model="showHeuristic" type="checkbox" />
        启发式节点
      </label>
      <label v-if="view === 'structure'" class="ctl" title="密钥/二进制/超大文件默认不画进结构树（数量会在目录行上标注）">
        <input v-model="showSkipped" type="checkbox" />
        未索引
      </label>
      <label v-if="view === 'features'" class="ctl" title="标出还没有有效功能描述的符号">
        <input v-model="showNoDoc" type="checkbox" />
        无说明
      </label>
      <label v-if="view === 'features'" class="ctl" title="显示每条功能的来源（HTTP/命令/工具/钩子/成员/调用）">
        <input v-model="showSource" type="checkbox" />
        来源
      </label>
      <label class="ctl">
        <input v-model="onlyChanged" type="checkbox" :disabled="!changedIds.size" />
        只看改动
      </label>

      <div class="search">
        <input v-model="query" placeholder="搜索符号 / 路径" aria-label="搜索地图" @keyup.enter="locate" />
        <button class="ghost" title="定位到首个匹配" @click="locate">↵</button>
      </div>

      <div class="map-actions">
      <span class="spacer" />
      <button class="ghost" title="展开所有目录（不含文件）" @click="expandDirs">展开目录</button>
      <button class="ghost" title="回到只看模块骨架" @click="collapseAll">折叠</button>
      <button class="ghost" title="适应宽度（保持文字可读，纵向滚动看更多）" @click="fitView('width')">适应宽度</button>
      <button class="ghost" title="全览（把整张图塞进屏幕，字会变小）" @click="fitView('all')">全览</button>
      <button class="ghost" :disabled="scanning" @click="refresh(false)">
        {{ scanning ? '扫描中…' : '扫描' }}
      </button>
      <button class="ghost" :disabled="scanning" title="清空并全量重建" @click="refresh(true)">重建</button>
      <button class="ghost" title="校验索引与磁盘一致（保真度）" @click="verify">校验</button>
      <button
        class="ghost"
        :class="{ on: showCoverage }"
        :disabled="coverageBusy"
        title="覆盖率上地图：实线=有测试直接盯着，虚线=文件级覆盖，灰=没有测试"
        @click="toggleCoverage"
      >覆盖率</button>
      <button
        class="ghost"
        :class="{ on: showEvidence }"
        :disabled="evidenceBusy"
        title="证据角标 SRC n：该节点上有多少条已记录的证据（未回滚的改动台账 + 决策线程）"
        @click="toggleEvidence"
      >证据</button>
      <button class="ghost" title="复制给模型的紧凑地图" @click="copyDigest">摘要</button>
      </div>
    </div>
    <div class="description-progress" :class="briefPresentation.tone" role="status" :title="briefPresentation.title">
      <span class="description-dot" aria-hidden="true" />
      <span>{{ briefPresentation.label }}</span>
      <span v-if="briefJob?.running && briefJob.total" class="muted tiny">已写 {{ briefJob.written || 0 }} · 失败 {{ briefJob.failed || 0 }}</span>
      <span v-else-if="briefJob?.error || briefJob?.note" class="description-reason muted tiny">{{ briefJob.error || briefJob.note }}</span>
      <span class="description-scope muted tiny">共用于功能导图、结构详情和编辑器词说明</span>
    </div>
    <div v-if="kindPanel" class="kindpick card">
      <div class="kp-head">
        <b>只看这些类型的符号</b>
        <span class="spacer" />
        <button class="ghost tiny" @click="kindFilter = []">清空</button>
        <button class="ghost tiny" @click="kindPanel = false">×</button>
      </div>
      <label v-for="k in allKinds" :key="k.kind" class="kp-row">
        <input type="checkbox" :checked="kindFilter.includes(k.kind)" @change="toggleKind(k.kind)" />
        <span class="kp-chip" :style="{ color: k.meta.color }">{{ k.meta.short }}</span>
        <span class="kp-name">{{ k.meta.label }}</span>
        <span class="spacer" />
        <span class="muted tiny">{{ k.count }}</span>
      </label>
      <div class="kp-foot muted tiny">
        树上的 <b>DIR</b> = 目录、<b>F</b> = 文件：它们是树的骨架，任何视角下都在，
        <b>不受上面这些勾选影响</b>（勾选过滤的是符号）；其余标记（<b>ƒ</b> 函数、
        <b>m</b> 方法、<b>v</b> 变量…）就是符号类型，在服务端过滤。
        左边的数字是全仓构成，不是过滤后的数量。
      </div>
    </div>

    <div v-if="featureBusy && view === 'features'" class="covbar">
      <span class="muted tiny">读取功能清单…</span>
    </div>

    <div v-if="showEvidence && evidence" class="covbar">
      <span class="cov-chip direct">SRC n</span>
      <span class="muted tiny">
        台账 {{ evidence.totals.patches }} · 决策 {{ evidence.totals.decisions }} ·
        标在 {{ evidence.totals.nodes }} 个节点上
      </span>
      <span class="spacer" />
      <span class="muted tiny">{{ evidence.basis }}</span>
    </div>

    <div v-if="showCoverage && coverage" class="covbar">
      <span class="cov-chip direct">直接覆盖</span>
      <span class="cov-chip file">文件级</span>
      <span class="cov-chip none">未覆盖</span>
      <span class="muted tiny">{{ coverageSummary() }}</span>
      <span class="spacer" />
      <span class="muted tiny">未覆盖的公共符号 {{ coverage.uncovered?.length || 0 }} 条（详情见节点抽屉）</span>
    </div>

    <div class="meta">
      <span>v{{ map?.version || 0 }}</span>
      <span>{{ stats.files || 0 }} 文件</span>
      <span>{{ stats.symbols || 0 }} 符号</span>
      <span>{{ stats.edges || 0 }} 关系</span>
      <span v-if="scanInfo.finished_at">上次扫描 {{ String(scanInfo.finished_at).slice(11, 19) }}</span>
      <span v-if="scanInfo.reused">复用 {{ scanInfo.reused }}</span>
      <span v-if="scanInfo.externals">外部依赖 {{ scanInfo.externals }}</span>
      <span v-if="!showSkipped && skippedTotal" title="密钥/二进制/超大文件：不在结构树里画，但数量如实标出（勾选「未索引」可展开）">
        未索引文件 {{ skippedTotal }}（已折叠）
      </span>
      <span class="spacer" />
      <span v-if="notice" class="notice" role="status" :title="notice">{{ notice }}</span>
    </div>

    <div v-if="focus.active" class="focusbar">
      <span class="up">上游（谁用到它）{{ focus.up.size - 1 }}</span>
      <span class="down">下游（它用到谁）{{ focus.down.size - 1 }}</span>
      <span class="muted tiny">{{ focus.edges }} 条直连边 · 只高亮一跳邻域</span>
      <span class="spacer" />
      <span class="muted tiny">依据：结构树与功能导航的父子关系</span>
      <button class="ghost tiny" @click="closeDetail">取消聚焦</button>
    </div>

    <div v-if="error" class="error">
      {{ error }}
      <button class="ghost" @click="refresh(false)">重试</button>
    </div>
    <div v-else-if="warnings.length" class="warn" :title="warnings.join('\n')">
      ⚠ {{ warnings[0] }}<span v-if="warnings.length > 1"> 等 {{ warnings.length }} 条</span>
    </div>

    <div class="canvas">
      <div v-if="loading && !map" class="muted overlay">加载地图…</div>
      <div v-else-if="!map" class="muted overlay">暂无索引数据</div>
      <div class="graph-viewport">
      <svg
        ref="svgEl"
        class="svg"
        @wheel="onWheel"
        @pointerdown="onPointerDown"
        @dblclick="fitView('width')"
      >
        <g :transform="`translate(${pan.x}, ${pan.y}) scale(${zoom})`">
          <!-- 结构投影的连接线：父右 → 子左的三次贝塞尔（思维导图的曲线），
               不是肘形折线——折线在几十行的时候会连成一片网格 -->
            <path
              v-for="(link, i) in visibleLinks"
              :key="`tree-${i}`"
              class="tree-link"
              :class="{
                hot: focus.active && (link.from === selected?.id || link.to === selected?.id),
                dim: focus.active && link.from !== selected?.id && link.to !== selected?.id,
              }"
              :d="(() => {
                const a = nodeById.get(link.from)
                const b = nodeById.get(link.to)
                if (!a || !b) return ''
                const ax = a.x + a.width
                const ay = a.y + a.height / 2
                const bx = b.x
                const by = b.y + b.height / 2
                const dx = Math.max(14, (bx - ax) * 0.55)
                return `M ${ax} ${ay} C ${ax + dx} ${ay}, ${bx - dx} ${by}, ${bx} ${by}`
              })()"
            />
          <!-- 节点 -->
          <g
            v-for="node in visibleNodes"
            :key="node.id"
            class="node"
            :data-id="node.id"
            :data-kind="node.kind"
            :data-depth="node.depth"
            :class="{
              selected: selected?.id === node.id,
              changed: changedNode(node),
              dim: node.unmatched,
              heuristic: view === 'structure' && (node.confidence === 'low' || node.confidence === 'medium'),
              'ov-added': overlayOf(node) === 'added',
              'ov-modified': overlayOf(node) === 'modified',
              'ov-removed': overlayOf(node) === 'removed',
              'cov-direct': coverageOf(node) === 'direct',
              'cov-file': coverageOf(node) === 'file',
              'cov-none': coverageOf(node) === 'none',
              'fx-up': focusRole(node) === 'up',
              'fx-down': focusRole(node) === 'down',
              'fx-self': focusRole(node) === 'self',
              'fx-dim': focusRole(node) === 'dim',
            }"
            :transform="`translate(${node.x}, ${node.y})`"
            tabindex="0"
            role="button"
            :aria-label="node.fullLabel || node.label"
            @pointerdown.stop
            @click.stop="onNodeClick(node)"
            @keyup.enter.stop="onNodeClick(node)"
            @keyup.space.prevent.stop="onNodeClick(node)"
            @mouseenter="onNodeHover(node, $event)"
            @mouseleave="onNodeLeave()"
          >
              <rect
                class="box"
                :width="node.width"
                :height="node.height"
                rx="6"
                :style="{ '--node-color': kindMeta(node.kind).color }"
              />
              <!-- 类型标记：`DIR`/`F`/`C`/`ƒ`… 都是缩写，悬停在上面给出中文全称
                   （用户问过"dir 是什么意思"——图例不该只存在于代码里） -->
              <text class="badge" :x="node.chipX" :y="node.midY + 4">
                <title>{{ kindMeta(node.kind).label }}（{{ kindMeta(node.kind).short }}）</title>
                {{ kindMeta(node.kind).short }}
              </text>
              <text class="label" :x="node.labelX" :y="node.midY + 4">{{ node.label }}</text>
              <!-- 角标的位置由布局算好（宽度算进节点宽度）：一个一个排在 caret 左边，
                   同类角标落在同一条竖线上，也不会压住文件名 -->
              <template v-for="(tok, ti) in node.tokens" :key="`tok-${ti}`">
                <rect
                  v-if="tok.bg"
                  class="tok-bg"
                  :class="tok.bg"
                  :x="tok.x"
                  :y="node.midY - 7"
                  :width="tok.w"
                  height="14"
                  rx="7"
                />
                <circle
                  v-if="tok.dot"
                  class="tok-dot"
                  :class="tok.cls"
                  :cx="tok.x + 6"
                  :cy="node.midY"
                  r="4"
                />
                <text
                  v-else
                  class="token"
                  :class="tok.cls"
                  :x="tok.x + 5"
                  :y="node.midY + 3.5"
                >
                  <title>{{ tok.title || tok.text }}</title>{{ tok.text }}
                </text>
              </template>
              <text
                v-if="node.hasKids"
                class="caret"
                data-role="caret"
                :x="node.caretX"
                :y="node.midY + 4"
                @click.stop="toggleNode(node)"
              >{{ nodeOpen(node) ? '▾' : '▸' }}</text>
              <!-- 到头了：`·` 原子 · `≡` 共用 · `↺` 回到上层。
                   放在 caret 这一位，是因为"能不能继续往下走"本来就是箭头要回答的问题；
                   旧版把它做成文字药丸，和证据、来源挤在同一行，才显得乱。 -->
              <text
                v-else-if="node.marker"
                class="caret ended"
                data-role="ended"
                :x="node.caretX"
                :y="node.midY + 4"
              >
                <title>{{ node.markerTitle || '' }}</title>
                {{ node.marker }}
              </text>
          </g>
        </g>
      </svg>

      <!-- 说明允许停留与滚动；延迟关闭让鼠标能够从节点进入卡片。 -->
      <div
        v-if="hover"
        class="nodetip card"
        :style="{ left: `${hover.x}px`, top: `${hover.y}px`, maxHeight: `${hover.maxHeight}px` }"
        role="region"
        aria-label="节点说明"
        tabindex="0"
        @mouseenter="holdNodeTip"
        @mouseleave="onNodeLeave"
        @focusin="holdNodeTip"
        @focusout="onNodeLeave"
        @wheel.stop
      >
        <div class="nt-head">
          <span class="nt-kind">{{ hover.kindLabel }}</span>
          <span class="nt-name mono">{{ hover.name }}</span>
        </div>
        <div v-if="hover.loading" class="muted tiny">读取说明…</div>
        <template v-else>
          <div v-if="hover.error" class="hint-err">{{ hover.error }}</div>
          <div v-else-if="hover.headline" class="nt-line">{{ hover.headline }}</div>
          <div v-if="hover.documentation?.text" class="nt-documentation"><span class="nt-source">{{ hover.documentation.source || '作者说明' }}</span><p>{{ hover.documentation.text }}</p></div>
          <div v-if="hover.business?.business_meaning" class="nt-documentation"><b>功能</b><span class="nt-source">{{ BUSINESS_STATUS_LABEL[hover.business.status] }} · {{ BUSINESS_SOURCE_LABEL[hover.business.source] }}</span><p>{{ hover.business.business_meaning }}</p></div>
          <div v-else-if="hover.business" class="muted tiny">{{ hover.business.status === 'stale' ? '功能描述已过期，正在等待自动更新。' : '功能描述待自动补充。' }}</div>
          <ul v-if="hover.facts.length" class="nt-facts">
            <li v-for="(f, i) in hover.facts" :key="i">
              <span class="nt-la">{{ f.label }}</span>{{ f.text }}
            </li>
          </ul>
        </template>
        <div class="nt-foot"><button class="link" @click.stop="openHoverDetail">查看完整说明 <span aria-hidden="true">→</span></button><span class="muted tiny">作用 · 功能 · 实现</span></div>
      </div>

      </div>
      <NodeDetail
        v-if="detail"
        :detail="detail"
        :project-id="projectId"
        @close="closeDetail"
        @open-file="(payload) => emit('open-file', payload)"
        @ask="(text) => emit('ask', text)"
        @error="(message) => emit('error', message)"
      />
    </div>
  </div>
</template>

<style scoped>
.map { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; height: 100%; position: relative; }
.bar {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  padding: 10px 12px 0; border-bottom: 1px solid var(--border); font-size: var(--fs-xs); background: var(--panel); flex: none;
}
.seg { display: flex; flex: none; gap: 2px; padding: 3px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 6px; }
.map-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 2px; flex-basis: calc(100% + 24px); margin: 2px -12px 0; padding: 5px 9px; border-top: 1px solid var(--border); background: var(--panel-soft); }.map-actions > .spacer { display: none; }
.seg button { font-size: var(--fs-xs); min-height: 26px; padding: 3px 10px; background: transparent; border: 1px solid transparent; border-radius: 4px; color: var(--muted); }
.seg button.active { background: var(--panel); color: var(--accent); border-color: var(--border); font-weight: var(--fw-semibold); box-shadow: 0 1px 3px #20273508; }
.ctl { display: flex; align-items: center; gap: var(--sp-1); color: var(--muted); }
.ctl select, .ctl input[type='checkbox'] { font-size: var(--fs-xs); padding: var(--sp-0-5) var(--sp-1); }
.search { display: flex; align-items: center; gap: 4px; min-width: 0; flex: 1 1 170px; max-width: 280px; }.search input { min-width: 0; flex: 1; }
.search input { width: 150px; min-height: 29px; padding: 4px 8px; font-size: var(--fs-xs); }
.ghost { border: none; background: transparent; color: var(--muted); padding: 3px 7px; font-size: var(--fs-xs); }
.ghost:hover { background: var(--hover); }
.spacer { flex: 1; }
/* 视角预设：一排小按钮，比单选下拉更贴近"我这次只想看哪几类" */
.ctl.kinds { gap: var(--sp-0-5); }
.ctl.kinds .ghost.tiny { font-size: var(--fs-xs); padding: var(--sp-0-5) var(--sp-1-5); border-radius: var(--r-sm); }
/* 类型多选浮层 */
.kindpick {
  position: absolute; right: 10px; top: 40px; z-index: 20; width: 232px;
  max-height: 60%; overflow: auto; padding: var(--sp-2) var(--sp-2-5); font-size: var(--fs-xs);
  box-shadow: var(--shadow-pop);
}
.kindpick .kp-head { display: flex; align-items: center; gap: var(--sp-1-5); margin-bottom: var(--sp-1-5); }
.kindpick .kp-row { display: flex; align-items: center; gap: var(--sp-1-5); padding: 3px var(--sp-0-5); cursor: pointer; }
.kindpick .kp-row:hover { background: var(--hover); }
.kindpick .kp-name { min-width: 0; }
/* 这个浮层同时是**图例**：左边缩写（DIR/F/C/ƒ…）对应地图上的类型标记，右边中文全称 */
.kindpick .kp-chip { flex: none; width: 24px; text-align: left; font-size: var(--fs-2xs); font-weight: var(--fw-bold); }
.kindpick .kp-foot { margin-top: var(--sp-1-5); line-height: var(--lh-snug); }

.meta {
  display: flex; align-items: center; gap: 14px; flex: none; padding: 6px 12px; overflow-x: auto; white-space: nowrap; background: var(--panel-soft);
  border-bottom: 1px solid var(--border); font-size: var(--fs-xs); color: var(--muted);
}
.notice { color: var(--accent); }
.description-progress { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; flex: none; padding: 7px 12px; border-bottom: 1px solid var(--border); background: var(--panel); font-size: var(--fs-xs); color: var(--muted); }
.description-dot { width: 6px; height: 6px; flex: none; border-radius: 50%; background: currentColor; }
.description-progress.running { color: var(--accent); }.description-progress.complete { color: var(--ok); }.description-progress.error { color: var(--err); }.description-progress.missing { color: var(--warn); }
.description-scope { margin-left: auto; }
.description-reason { max-width: min(480px, 100%); overflow-wrap: anywhere; }
.error {
  display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1-5) var(--sp-2-5);
  font-size: var(--fs-xs); color: var(--err); background: var(--err-soft);
}
.warn {
  padding: 5px var(--sp-2-5); font-size: var(--fs-xs); color: var(--warn); background: var(--warn-soft);
  border-bottom: 1px solid var(--border);
}
.canvas { position: relative; flex: 1; min-height: 0; display: flex; }
.graph-viewport { position: relative; flex: 1; min-width: 0; min-height: 0; display: flex; overflow: hidden; }
.svg { flex: 1; min-width: 0; width: 100%; height: 100%; background: var(--panel-soft); cursor: grab; touch-action: none; }
.svg:active { cursor: grabbing; }
.overlay { position: absolute; inset: 0; display: grid; place-items: center; font-size: var(--fs-sm); }

/* 作者说明保留完整文本，长内容可滚动，文件点击继续在地图中查看详情。 */
.nodetip {
  position: absolute; z-index: 7; width: min(420px, calc(100% - 24px)); overflow-y: auto;
  padding: 12px 14px; font-size: var(--fs-xs); line-height: var(--lh-normal);
  box-shadow: var(--shadow-pop);
}
.nodetip .nt-head { display: flex; align-items: baseline; gap: var(--sp-1-5); margin-bottom: var(--sp-1); }
.nodetip .nt-kind {
  flex: none; font-size: var(--fs-xs); font-weight: var(--fw-bold); color: var(--accent);
  background: var(--accent-soft); border-radius: var(--r-sm); padding: 0 5px;
}
.nodetip .nt-name { font-weight: var(--fw-semibold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nodetip .nt-line { color: #0f172a; }
.nodetip .nt-documentation { margin: 10px 0; padding: 10px; border-radius: 6px; background: var(--panel-soft); }
.nodetip .nt-source { font-size: 10px; color: var(--muted); }.nodetip .nt-documentation p { margin: 5px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.nodetip .nt-facts { list-style: none; margin: var(--sp-1) 0 0; padding: 0; }
.nodetip .nt-facts li { display: flex; gap: var(--sp-1-5); font-size: var(--fs-xs); color: var(--text-2); overflow-wrap: anywhere; }
.nodetip .nt-la { flex: none; width: 58px; color: var(--muted); }
.nodetip .hint-err { color: var(--err); font-size: var(--fs-xs); }
.nodetip .nt-foot { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px; margin-top: 10px; border-top: 1px solid var(--border); padding-top: 9px; }.nodetip .nt-foot button { padding: 0; border: 0; background: transparent; color: var(--accent); font-size: 11px; }

.tree-link { fill: none; stroke: var(--border-strong); stroke-width: 1.2; }
/* 树状（思维导图）节点：目录是药丸，文件/符号只写字——层级靠缩进与连线，不靠每个节点一个框。
   状态色一律落在 .box 的 fill 上（叶子默认透明），所以选中/变更/叠加层都能看出来。 */
.node { cursor: pointer; }
.node .box { fill: transparent; stroke: none; }
.node[data-kind='dir'] .box { fill: #fff; stroke: var(--border); stroke-width: 1; }
.node[data-depth='0'] .box { fill: #0f172a; stroke: none; }
.node[data-depth='0'] .label { fill: #fff; font-weight: var(--fw-semibold); }
.node[data-depth='0'] .badge { fill: #94a3b8; }
.node[data-depth='0'] .caret { fill: var(--border-strong); }
.node[data-depth='0']:hover .box { fill: #0f172a; }

.node.selected .box { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 1.6; }
.node.changed .box { fill: #fffbeb; stroke: #f59e0b; stroke-width: 1.6; }
.node.heuristic .label { font-style: italic; fill: #64748b; }
/* 任务叠加层：新增绿 / 修改黄 / 删除红（删除的符号在任务树里已经不存在，
   这里保留样式以便用主干的树看差异时着色） */
.node.ov-added .box { fill: #f0fdf4; stroke: #16a34a; stroke-width: 1.6; }
.node.ov-modified .box { fill: #fffbeb; stroke: #d97706; stroke-width: 1.6; }
.node.ov-removed .box { fill: #fef2f2; stroke: #dc2626; stroke-width: 1.6; }

/* 注意：原来这里有个 9.5px 的 `.badge`，但模板里没有任何元素用它（节点用的是
   `.label`/`.caret`/`.token`）——死样式，已删。字号统一后不该留下这种东西当"参考"。 */
.label { font-size: var(--fs-xs); fill: var(--text); }
.caret { font-size: var(--fs-xs); fill: #94a3b8; }
.caret:hover { fill: var(--accent); }
/* 「到头了」的记号（· 原子 / ≡ 共用 / ↺ 回到上层）：和箭头同一个位置，但要更安静 */
.caret.ended { fill: #cbd5e1; cursor: default; }
.caret.ended:hover { fill: #cbd5e1; }
/* 角标：位置来自布局，样式只管颜色与底 */
.token { font-size: var(--fs-xs); fill: var(--muted); }
.tok-bg { opacity: .14; }
.token.tok-err { fill: #b91c1c; font-weight: var(--fw-semibold); }
.token.tok-warn { fill: #b45309; font-weight: var(--fw-semibold); }
.tok-bg-err { fill: #dc2626; opacity: .16; }
.tok-bg-warn { fill: #d97706; opacity: .16; }
.token.tok-entry { fill: var(--accent); font-weight: var(--fw-semibold); }
.token.tok-skip { fill: #b45309; }
.token.tok-count { fill: #94a3b8; }
.tok-bg-src { fill: #0f172a; opacity: .92; }
.token.tok-src { fill: #fff; font-weight: var(--fw-bold); letter-spacing: .02em; }
.tok-dot.tok-cov-direct { fill: #059669; }
.tok-dot.tok-cov-file { fill: #fff; stroke: #64748b; stroke-width: 1.4; stroke-dasharray: 2 1.5; }
.tok-dot.tok-cov-none { fill: #e5e7eb; }

/* 覆盖率三态用左边的小圆点表达（见 .tok-dot），文件夹本身没有覆盖率概念 */
.node.cov-none .label { fill: #94a3b8; }
.ghost.on { background: var(--accent-soft); color: var(--accent); }
.covbar {
  display: flex; align-items: center; gap: var(--sp-1-5); padding: var(--sp-1) var(--sp-2-5);
  border-bottom: 1px solid var(--border); font-size: var(--fs-xs);
}
.tree-link.hot { stroke: var(--accent); stroke-opacity: .8; stroke-width: 1.6; }
.tree-link.dim { stroke-opacity: .2; }
/* 聚焦三态：上游=紫（谁用到它）、下游=绿（它用到谁）、其余退到背景。
   压暗不能太狠：曾经用 .28，结果整张图看起来是空的（真机截图确认过）。 */
.node.fx-up .box { fill: #f5f3ff; stroke: #7c3aed; stroke-width: 1.6; }
.node.fx-down .box { fill: #ecfdf5; stroke: #059669; stroke-width: 1.6; }
.node.fx-self .box { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 2; }
.node.fx-dim { opacity: .45; }
/* 悬停给一点反馈：一眼知道"这里可以点" */
.node:hover .box { fill: #f1f5f9; }
.node:focus-visible { outline: none; }.node:focus-visible .box { stroke: var(--accent); stroke-width: 2; }
@media (max-width: 760px) { .meta { flex-wrap: wrap; gap: 5px 9px; } .search input { width: 155px; } }
.focusbar {
  display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1) var(--sp-2-5);
  border-bottom: 1px solid var(--border); font-size: var(--fs-xs); background: #fbfdff;
}
.focusbar .up { color: #6d28d9; font-weight: var(--fw-semibold); }
.focusbar .down { color: var(--ok); font-weight: var(--fw-semibold); }
.cov-chip { font-size: var(--fs-xs); padding: 0 var(--sp-1-5); border-radius: var(--r-sm); border: 1px solid var(--border); }
.cov-chip.direct { border-color: #059669; color: #065f46; }
.cov-chip.file { border-style: dashed; color: var(--muted); }
.cov-chip.none { color: var(--muted); }
</style>
