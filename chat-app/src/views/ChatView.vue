<script setup>
import { computed, onBeforeUnmount, onMounted, nextTick, ref, watch } from 'vue'
import { marked } from 'marked'
import WelcomeState from '../components/WelcomeState.vue'
import ApprovalRequest from '../components/ApprovalRequest.vue'
import UiIcon from '../components/ui/UiIcon.vue'
import { useDialog } from '../composables/useDialog.js'
import { attachThinking, pushThinking } from '../lib/thinkingSegments.js'
import { createSessionStream } from '../lib/sessionStream.js'
import { sessionPhaseLabel, internalMessagePresentation, isSuccessfulCompletion, incompleteCompletion } from '../lib/sessionPresentation.js'
import {
  currentTodos,
  todoCounts,
  todoHeadline,
  TODO_ICON,
} from '../lib/todos.js'
import {
  buildAnswers,
  draftCompleted,
  optionPrefix,
  parseRecommendedLabel,
  pendingQuestion,
  progressText,
} from '../lib/questions.js'
import { api, openStream, getToken, readFileBase64 } from '../api.js'
import { shouldSendMessage, usePreferences } from '../lib/preferences.js'
import { generalChatSessions } from '../lib/chatSessions.js'

const { preferences } = usePreferences()
const composing = ref(false)
const sendHint = computed(() => preferences.sendKey === 'ctrl-enter' ? 'Ctrl / ⌘ + Enter 发送，Enter 换行' : 'Enter 发送，Shift+Enter 换行')
function onComposerKeydown(event) {
  if (!shouldSendMessage(event, preferences.sendKey, composing.value)) return
  event.preventDefault()
  send()
}

const props = defineProps({ user: { type: Object, required: true } })
const dialog = useDialog()
let viewDisposed = false

const sessions = ref([])
const current = ref(null)
const events = ref([])
const input = ref('')
const busy = ref(false)
const phase = ref('')
const activityLabel = computed(() => sessionPhaseLabel(phase.value))
const sessionListOpen = ref(false)
const starterBusy = ref(false)
const banner = ref('')
const approval = ref(null)      // 待处理审批
const approvalSubmitting = ref('')
const approvalError = ref('')
const streamState = ref('idle') // idle | connecting | open | closed
const streaming = ref('')       // 流式增量缓冲（打字机效果）
const thinking = ref('')        // 推理模型思考流缓冲（渲染用；真正的分段落状态在 thinkSink）
const thinkOpen = ref({})       // 每条消息自己的"思考"折叠状态（key = 事件 seq）
//: 思考分段的真状态（buffer + 起始时间）：抽成纯函数放在 lib 里，便于单测
const thinkSink = { buffer: '', startedTs: 0 }
const modelName = ref('')       // 当前档位实际模型名（来自 session_meta）
const listEl = ref(null)
const listWrapEl = ref(null)    // composer 外层（自动聚焦用）
// 会话级 Token 用量（由 done 事件的 usage 累计，历史回放自动重建）
const usage = ref({ input: 0, output: 0, cached: 0 })

// ---- 输入区状态（harness 风格 composer） ----
const activeModel = ref('')     // 当前生效模型（来自模型设置）
const taEl = ref(null)

// ---- 联网开关（M19） ----
// 与执行权限同一套语义：**会话级**属性，落库 + 热生效；但开关本身在输入框旁边，
// 因为它影响的是"这一轮要不要上网"。关着的时候后端不会把 web_search/web_fetch
// 放进模型可见的工具列表——模型看不到就不会去调，也不会浪费一轮拿"工具不存在"。
const webOn = ref(false)
const webInfo = ref(null)          // { available, reason, model, max_results, how_to }
const webHint = computed(() => {
  const info = webInfo.value
  if (!info) return '联网能力检测中…'
  if (!info.available) return info.reason || '当前不可用'
  return `检索模型 ${info.model || '—'} · 单次最多 ${info.max_results || 8} 条来源；` +
    '结果是外部数据，会以"数据块"形式注入并标注来源 URL'
})
const webAvailable = computed(() => webInfo.value?.available !== false)

function localWebOverride(sid) {
  try {
    return sid ? window.localStorage.getItem(`chat-agent:web:${sid}`) : null
  } catch {
    return null
  }
}

async function loadWeb(sid, token = sessionToken) {
  if (!webInfo.value) {
    try {
      webInfo.value = await api.webCapability()
    } catch {
      webInfo.value = { available: false, reason: '拿不到联网能力信息' }
    }
  }
  if (!isCurrentSession(sid, token)) return
  const local = localWebOverride(sid)
  if (local !== null) webOn.value = local === '1'
  if (!webAvailable.value) webOn.value = false
}

async function toggleWeb() {
  if (!webAvailable.value) {
    showHint(webHint.value)
    return
  }
  const next = !webOn.value
  webOn.value = next
  const sid = current.value?.id
  const mine = sessionToken
  try {
    if (sid) window.localStorage.setItem(`chat-agent:web:${sid}`, next ? '1' : '0')
  } catch {
    /* 隐私模式：不影响后端那份 */
  }
  if (sid) {
    try {
      await api.setSessionWeb(sid, next)
      if (!isCurrentSession(sid, mine)) return
      showHint(next ? '已开启联网：本轮起模型可以检索网页' : '已关闭联网：模型不再拿到联网工具')
    } catch (e) {
      if (isCurrentSession(sid, mine)) showHint(e.message)
    }
  }
}

// ---- 弹层定位（fixed + 视口夹紧） ----
// composer 所在的会话列是 overflow:hidden 的窄容器（790px 窗口下只有 264px），
// absolute 弹层会被它裁掉——真机踩过：权限下拉左边被切 64px，
// 「本会话权限」显示成「会话权限」、「切换立即生效」显示成「立即生效」。
// 统一用 fixed（fixed 不受祖先 overflow 裁剪）按锚点矩形算坐标，并夹紧到视口内。
const popAt = ref({})   // { [key]: { left, bottom, maxHeight? } } —— 直接绑到弹层的 :style
function popStyle(key, ev, width, { align = 'right', gap = 6, maxH = 0 } = {}) {
  const r = ev.currentTarget.getBoundingClientRect()
  width = Math.min(width, Math.max(0, window.innerWidth - 24))
  let left = r.right - width
  if (align === 'left') left = r.left
  else if (align === 'center') left = r.left + r.width / 2 - width / 2
  const above = Math.max(0, r.top - gap - 12)
  const below = Math.max(0, window.innerHeight - r.bottom - gap - 12)
  const upwards = above >= 180 || above >= below
  const available = upwards ? above : below
  const style = {
    width: `${width}px`,
    left: Math.round(Math.max(12, Math.min(left, window.innerWidth - width - 12))) + 'px',
    ...(upwards ? { bottom: Math.max(12, window.innerHeight - r.top + gap) + 'px' } : { top: Math.max(12, r.bottom + gap) + 'px' }),
    maxHeight: Math.floor(Math.min(maxH || available, available)) + 'px',
    overflowY: 'auto',
  }
  popAt.value[key] = style
  return style
}
function closePops() {
  addMenuOpen.value = false
  addExpand.value = ''
  permMenuOpen.value = false
  profMenuOpen.value = false
  statPop.value = ''
  ctxPop.value = false
  menuFor.value = null      // 会话行的 ⋯ 菜单
}

/**
 * 点空白处关掉所有悬浮弹层（用户要求："所有的类似三个点这种悬浮窗，点空白处会自己关闭"）。
 *
 * 判据是**点击目标是否落在弹层/触发按钮内部**：不是就关。所以每个弹层与它的触发按钮
 * 都带 `data-pop` 标记（见模板）——比"点在哪几个类名之外"稳，新增弹层时只需补标记。
 * 用 capture=false 让弹层内部按钮的 @click 先跑（否则点菜单项会先被关掉、动作丢失）。
 *
 * **必须看 `composedPath()` 而不是只看 `ev.target`**：Vue 的 DOM 更新在微任务里刷新，
 * 而微任务检查点会在每个事件回调返回后发生——所以"点一下就把自己 v-if 掉的按钮"
 * （例如搜索框的清空按钮）在事件冒泡到 window 时**已经从文档里摘掉了**，
 * 此时 `target.closest('[data-pop]')` 返回 null，弹层会被误判成"点了外面"而关掉。
 * `composedPath()` 是派发时算好的路径，元素被摘掉也仍然给出原来的祖先，因此可靠。
 */
function onDocClick(ev) {
  const path = typeof ev.composedPath === 'function' ? ev.composedPath() : []
  if (path.some((node) => node instanceof Element && node.hasAttribute('data-pop'))) return
  const el = ev.target
  if (el instanceof Element && el.closest('[data-pop]')) return
  closePops()
}

function onEsc(ev) {
  if (ev.key === 'Escape') closePops()
}

// ---- 权限档位（三档：只读 / 工作区写入 / 完全权限） ----
// 档位是**会话级**属性：新建会话用 newPerm，已有会话改自己的（落库 + 热生效）。
// 可选档位与文案由后端 /settings/permission 下发，避免两边漂移。
const PERM_FALLBACK = [
  { id: 'read-only', label: '逐次审批', desc: '读取附件；生成文件前申请本次授权' },
  { id: 'workspace-write', label: '生成文件', desc: '读取附件、生成文件，操作限于本会话' },
]
const PERM_ICONS = {
  'read-only': 'M8 1.5l5 2v3.7c0 3.2-2.1 5.6-5 6.8-2.9-1.2-5-3.6-5-6.8V3.5l5-2z M5.7 8.1l1.6 1.6 3.1-3.3',
  'workspace-write': 'M8 1.5l5 2v3.7c0 3.2-2.1 5.6-5 6.8-2.9-1.2-5-3.6-5-6.8V3.5l5-2z M5.7 9.6l2.6-2.6 1.7 1.7-2.6 2.6H5.7V9.6z',
  'full-access': 'M8 1.5l5 2v3.7c0 3.2-2.1 5.6-5 6.8-2.9-1.2-5-3.6-5-6.8V3.5l5-2z M8 4.6v3.1 M8 9.9v.6',
}
const permModes = ref(PERM_FALLBACK)
const defaultPerm = ref('workspace-write')  // 全局默认（新建会话用）
const newPerm = ref('workspace-write')      // 新建会话的档位
const permMenuOpen = ref(false)
const permMenuEl = ref(null)
const choiceTriggers = { perm: null }

function closeChoiceMenu(key) {
  if (key === 'perm') permMenuOpen.value = false
  choiceTriggers[key]?.focus({ preventScroll: true })
}

function openChoiceMenu(key, ev) {
  closePops()
  choiceTriggers[key] = ev.currentTarget
  popStyle(key, ev, 208, { align: 'right', gap: 6, maxH: 260 })
  if (key === 'perm') permMenuOpen.value = true
  nextTick(() => {
    const menu = permMenuEl.value
    const target = menu?.querySelector('[aria-checked="true"]') || menu?.querySelector('[role="menuitemradio"]')
    target?.focus({ preventScroll: true })
    target?.scrollIntoView({ block: 'nearest' })
  })
}

function onChoiceKey(ev, key) {
  if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closeChoiceMenu(key); return }
  if (ev.key === 'Tab') { closeChoiceMenu(key); return }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(ev.key)) return
  ev.preventDefault(); ev.stopPropagation()
  const items = [...ev.currentTarget.querySelectorAll('[role="menuitemradio"]')]
  if (!items.length) return
  const index = items.indexOf(document.activeElement)
  const next = ev.key === 'Home' ? 0 : ev.key === 'End' ? items.length - 1
    : (index + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
  items[next].focus({ preventScroll: true })
  items[next].scrollIntoView({ block: 'nearest' })
}

// 当前生效档位：有会话看会话自己的，没会话看"即将用于新建"的
const curPerm = computed(() => current.value?.permission_mode || newPerm.value)
const curPermInfo = computed(
  () => permModes.value.find((m) => m.id === curPerm.value) || PERM_FALLBACK[1])
function permLabelOf(mode) {
  return (permModes.value.find((m) => m.id === mode) || {}).label || mode
}

async function loadPermissionSettings() {
  try {
    const body = await api.permissionSettings()
    if (Array.isArray(body.modes) && body.modes.length) permModes.value = body.modes
    if (body.mode) {
      defaultPerm.value = body.mode
      newPerm.value = body.mode
    }
  } catch {
    /* 拉不到就用本地兜底清单，不影响使用 */
  }
}

async function pickPerm(mode) {
  closeChoiceMenu('perm')
  if (!current.value?.id) {
    newPerm.value = mode
    return
  }
  const id = current.value.id
  const mine = sessionToken
  try {
    const meta = await api.setSessionPermission(id, mode)
    if (isCurrentSession(id, mine)) current.value = { ...current.value, ...meta }
    const i = sessions.value.findIndex((s) => s.id === meta.id)
    if (i >= 0) sessions.value[i] = { ...sessions.value[i], ...meta }
  } catch (e) {
    if (isCurrentSession(id, mine)) banner.value = e.message
  }
}

let lastSeq = 0
let sessionToken = 0
let sendToken = 0
let approvalVersion = 0
let refreshApprovalOnMeta = false
let approvalSnapshotReceived = false
let runtimeMetaReceived = false
const isCurrentSession = (id, token) => current.value?.id === id && sessionToken === token
const connection = createSessionStream({
  open: openStream,
  afterSeq: () => lastSeq,
  onEvent: pushEvent,
  onState: (state) => { streamState.value = state; if (state === 'open') refreshApprovalOnMeta = true },
  onError: (message) => { banner.value = message },
})

const MAX_EVENTS = 400

// ---- 交付文件预览面板（按扩展名类型化渲染） ----
// 图片 → object URL（<img> 不能带 Authorization，只能先 fetch 成 blob）
// HTML  → iframe + srcdoc（sandbox 禁脚本，防交付物里的 JS 执行）
// md    → 轻量 Markdown 渲染（先转义再套标签）
// 其余  → 纯文本 <pre>
const preview = ref(null) // { filename, kind, content?, html?, url? } | null
const previewBusy = ref(false)
let previewToken = 0

const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'avif']
const HTML_EXT = ['html', 'htm']
const MD_EXT = ['md', 'markdown']

function artifactKind(filename) {
  const ext = String(filename || '').split('.').pop()?.toLowerCase() || ''
  if (IMAGE_EXT.includes(ext)) return 'image'
  if (HTML_EXT.includes(ext)) return 'html'
  if (MD_EXT.includes(ext)) return 'markdown'
  return 'text'
}

const KIND_LABEL = { image: '图片', html: '网页', markdown: 'Markdown', text: '文本' }

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

/** 聊天回答的 Markdown 渲染（marked，本地依赖；输出经 sanitizeHtml 清洗）。 */
function renderMarkdown(md) {
  // 用 marked（本地依赖，不引 CDN）：模型回答里表格/有序列表/嵌套列表/代码块都有，
  // 手写正则那条路在"表格里的 | 和行内 code"上会翻车。gfm 打开 → 表格与任务列表可用。
  // 注意：marked 输出的是 HTML，必须自己防注入——模型回答里可能出现 <script>。
  // 这里用 DOMPurify 会更稳，但为了不引第二个依赖，采取"只允许安全标签"的策略：
  // 先交给 marked，再把危险标签与 on* 属性剥掉（回答是文本，不需要 iframe/svg/script）。
  const raw = marked.parse(md ?? '', { gfm: true, breaks: true, async: false })
  return sanitizeHtml(raw)
}

/** 只允许聊天回答需要的标签与属性；其余一律剥掉（回答是模型输出，等同外部输入）。 */
function sanitizeHtml(html) {
  const tpl = document.createElement('template')
  tpl.innerHTML = html
  const DROP = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'LINK', 'META', 'FORM', 'INPUT', 'BASE'])
  const KEEP_ATTR = new Set(['class', 'href', 'src', 'alt', 'title', 'colspan', 'rowspan', 'start'])
  const walk = (node) => {
    for (const el of [...node.children]) {
      if (DROP.has(el.tagName)) {
        el.remove()
        continue
      }
      for (const attr of [...el.attributes]) {
        const name = attr.name.toLowerCase()
        const unsafeHref = name === 'href' && /^\s*(javascript|data):/i.test(attr.value)
        if (!KEEP_ATTR.has(name) || unsafeHref) el.removeAttribute(attr.name)
      }
      if (el.tagName === 'A') {
        el.setAttribute('target', '_blank')
        el.setAttribute('rel', 'noopener')
      }
      walk(el)
    }
  }
  walk(tpl.content)
  return tpl.innerHTML
}

function revokePreviewUrl() {
  if (preview.value?.url) {
    try {
      URL.revokeObjectURL(preview.value.url)
    } catch {
      /* 忽略 */
    }
  }
}

async function openArtifact(filename) {
  if (!current.value || !filename) return
  const sid = current.value.id
  const session = sessionToken
  const mine = ++previewToken
  const stillOpen = () => mine === previewToken && isCurrentSession(sid, session)
  revokePreviewUrl()
  const kind = artifactKind(filename)
  preview.value = { filename, kind, content: '', html: '' }
  previewBusy.value = true
  try {
    if (kind === 'image') {
      const url = await api.artifactBlobUrl(sid, filename)
      if (!stillOpen()) { URL.revokeObjectURL(url); return }
      preview.value = { filename, kind, url }
    } else {
      const text = await api.artifactText(sid, filename)
      if (!stillOpen()) return
      preview.value =
        kind === 'html'
          ? { filename, kind, html: text }
          : kind === 'markdown'
            ? { filename, kind, html: renderMarkdown(text) }
            : { filename, kind, content: text }
    }
  } catch (e) {
    if (stillOpen()) preview.value = { filename, kind: 'text', content: `（读取失败：${e.message}）` }
  } finally {
    if (stillOpen()) previewBusy.value = false
  }
}

function closePreview() {
  previewToken += 1
  revokePreviewUrl()
  preview.value = null
  previewBusy.value = false
}

async function copyPreview() {
  const p = preview.value
  if (!p) return
  const text = p.content ?? p.html ?? ''
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    /* 剪贴板不可用时忽略 */
  }
}

function downloadPreview() {
  const p = preview.value
  if (!p) return
  const a = document.createElement('a')
  a.download = p.filename
  if (p.kind === 'image' && p.url) {
    a.href = p.url
  } else {
    const blob = new Blob([p.content ?? p.html ?? ''], { type: 'text/plain;charset=utf-8' })
    a.href = URL.createObjectURL(blob)
    setTimeout(() => URL.revokeObjectURL(a.href), 2000)
  }
  a.click()
}

/** 交付类工具渲染成「文件卡片」，而不是普通工具卡。 */
function isDeliver(node) {
  return node?.call?.name === 'deliver_file'
}

function deliverFilename(node) {
  return node?.call?.arguments?.filename || '交付文件'
}

function deliverSize(node) {
  const c = String(node?.call?.arguments?.content || '')
  if (!c) return ''
  const n = new TextEncoder().encode(c).length
  return n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`
}

const timeline = computed(() => events.value.filter((e) => e.seq > 0))

// ---- 工具调用配对（M15）：tool_use + tool_result 合成一张卡 ----
// 旧版把两者分开渲染：tool_use 只是一个灰色名字 chip（看不到参数），
// tool_result 是一条默认折叠的 details（看不到结果）。这里按 call_id 配对，
// 一张卡同时呈现「调用了什么（参数）+ 结果如何（状态/预览）」。
//
// 形态参考 deepseek harness：**一行摘要 + 点击展开**。摘要行是
// `图标 · 动词 · 目标（文件/命令/查询）+ 增删行数`，状态与耗时也在这一行——
// 之前的常驻卡片（参数块 + 结果块）在小屏上会把对话刷得看不见回答，
// 而"默认最小化"正是 DSH 的做法：没展开时一行，展开才看参数与输出。
const openTools = ref({}) // { [call_id]: true }

const TOOL_ARG_KEYS = ['command', 'path', 'query', 'url', 'text', 'content', 'name']

/**
 * 工具 → (中文动词, 图标)。动词用中文（界面语言），图标是内联 SVG path，
 * 一个 `d` 一条路径，风格与页面其它图标一致（stroke 1.3、圆头圆角）。
 */
const TOOL_META = {
  read_file: { verb: '读取', icon: 'M5.5 3.2h5.2l3.3 3.3v6.3H5.5z M10.7 3.2v3.3h3.3' },
  write_file: { verb: '写入', icon: 'M3.4 12.8l.8-2.9 7-7 2.1 2.1-7 7-2.9.8z' },
  edit_file: { verb: '编辑', icon: 'M3.4 12.8l.8-2.9 7-7 2.1 2.1-7 7-2.9.8z' },
  str_replace_editor: { verb: '编辑', icon: 'M3.4 12.8l.8-2.9 7-7 2.1 2.1-7 7-2.9.8z' },
  grep: { verb: '搜索', icon: 'M7.2 11.4a4.2 4.2 0 100-8.4 4.2 4.2 0 000 8.4z M10.3 10.3l3 3' },
  glob: { verb: '查找', icon: 'M7.2 11.4a4.2 4.2 0 100-8.4 4.2 4.2 0 000 8.4z M10.3 10.3l3 3' },
  search_files: { verb: '查找', icon: 'M7.2 11.4a4.2 4.2 0 100-8.4 4.2 4.2 0 000 8.4z M10.3 10.3l3 3' },
  list_dir: { verb: '列出', icon: 'M1.8 4.4a1 1 0 011-1h3l1.2 1.4h6.2a1 1 0 011 1v6a1 1 0 01-1 1H2.8a1 1 0 01-1-1z' },
  list_files: { verb: '列出', icon: 'M1.8 4.4a1 1 0 011-1h3l1.2 1.4h6.2a1 1 0 011 1v6a1 1 0 01-1 1H2.8a1 1 0 01-1-1z' },
  get_file_info: { verb: '查看', icon: 'M5.5 3.2h5.2l3.3 3.3v6.3H5.5z M10.7 3.2v3.3h3.3' },
  bash: { verb: '运行', icon: 'M3 4.4l3.6 3.6L3 11.6 M8 11.6h5' },
  pwsh: { verb: '运行', icon: 'M3 4.4l3.6 3.6L3 11.6 M8 11.6h5' },
  code_map: { verb: '看地图', icon: 'M8 2.6l4.4 2.6v5.6L8 13.4 3.6 10.8V5.2z M8 8.2v5.2 M3.6 5.2L8 8.2l4.4-3' },
  web_search: { verb: '联网检索', icon: 'M8 13.4a5.4 5.4 0 100-10.8 5.4 5.4 0 000 10.8z M2.6 8h10.8 M8 2.6c2.6 3 2.6 7.8 0 10.8M8 2.6c-2.6 3-2.6 7.8 0 10.8' },
  web_fetch: { verb: '抓取网页', icon: 'M8 13.4a5.4 5.4 0 100-10.8 5.4 5.4 0 000 10.8z M2.6 8h10.8 M8 2.6c2.6 3 2.6 7.8 0 10.8M8 2.6c-2.6 3-2.6 7.8 0 10.8' },
  deliver_file: { verb: '交付', icon: 'M8 10.6V3.2 M4.8 6.4L8 3.2l3.2 3.2 M3.4 12.8h9.2' },
  save_memory: { verb: '记住', icon: 'M8 3.4a4.6 4.6 0 014.6 4.6c0 3-4.6 5.6-4.6 5.6S3.4 11 3.4 8A4.6 4.6 0 018 3.4z' },
  load_pack: { verb: '加载', icon: 'M8 3.2l4.6 2.4-4.6 2.4L3.4 5.6z M3.4 8.4L8 10.8l4.6-2.4 M3.4 11.4L8 13.8l4.6-2.4' },
  kb_search: { verb: '查知识库', icon: 'M3.2 3.4h9.6v9.2H3.2z M6 3.4v9.2' },
  kb_write: { verb: '写知识库', icon: 'M3.2 3.4h9.6v9.2H3.2z M6 3.4v9.2 M9.6 7.6v3M8.1 9.1h3' },
}

const DEFAULT_TOOL_ICON = 'M6.4 2.6h3.2v3.2H6.4z M4.4 9.4h3.2v3.2H4.4z M8.4 9.4h3.2v3.2H8.4z'

function toolMeta(node) {
  const name = String(node?.call?.name || '')
  return TOOL_META[name] || { verb: name || '工具', icon: DEFAULT_TOOL_ICON }
}

function toolIconPath(node) {
  return toolMeta(node).icon
}

function toolVerb(node) {
  return toolMeta(node).verb
}

/** 摘要行里的"目标"：文件路径 / 命令 / 查询词 / URL，尽量短。 */
function toolTarget(node) {
  const args = node?.call?.arguments || {}
  const pick = (list) => list.find((k) => args[k] !== undefined && args[k] !== '')
  const name = String(node?.call?.name || '')
  if (name === 'web_search' && Array.isArray(args.queries)) {
    return args.queries.join(' / ').slice(0, 90)
  }
  const key = pick(['path', 'url', 'pattern', 'query', 'command', 'text', 'name'])
  if (!key) {
    // 认不出常见字段就把参数原样压一行（工具的自定义参数兜底）
    const raw = JSON.stringify(args || {})
    if (raw === '{}' || raw === 'null') return ''
    return raw.length > 90 ? `${raw.slice(0, 90)}…` : raw
  }
  const value = String(args[key]).replace(/\s+/g, ' ').trim()
  return value.length > 90 ? `${value.slice(0, 90)}…` : value
}

/** 目标是不是一个文件路径（决定要不要用"可点开"的下划线样式）。 */
function toolTargetIsPath(node) {
  const args = node?.call?.arguments || {}
  return typeof args.path === 'string' && args.path.length > 0
}

/**
 * 增删行数。只在**能算准**的时候给：
 * - `edit_file`：替换的 old/new 文本行数就是这次改动的增删（事实，不是估算）；
 * - `write_file`：只知道写入内容有多少行，**不知道旧文件**，所以只给 `+N`，
 *   另一半留空并在 title 里说明"这不是 diff"。
 */
function toolDiff(node) {
  const args = node?.call?.arguments || {}
  const name = String(node?.call?.name || '')
  const lines = (s) => (typeof s === 'string' && s.length ? s.split('\n').length : 0)
  if (name === 'edit_file' || name === 'str_replace_editor') {
    const added = lines(args.new_string)
    const removed = lines(args.old_string)
    if (!added && !removed) return null
    return { added, removed, exact: true }
  }
  if (name === 'write_file') {
    const added = lines(args.content)
    return added ? { added, removed: null, exact: false } : null
  }
  return null
}

function toolDiffTitle(node) {
  const d = toolDiff(node)
  if (!d) return ''
  return d.exact
    ? `本次替换：新增 ${d.added} 行 / 删除 ${d.removed} 行`
    : `写入内容 ${d.added} 行（不知道原文件，这不是 diff）`
}

function toolArgs(call) {
  const args = call?.arguments || {}
  for (const k of TOOL_ARG_KEYS) {
    if (args[k] !== undefined && args[k] !== '') {
      const v = String(args[k])
      return v.length > 500 ? v.slice(0, 500) + '…' : v
    }
  }
  const s = JSON.stringify(args)
  if (s === '{}' || s === 'null') return ''
  return s.length > 500 ? s.slice(0, 500) + '…' : s
}

function toolState(node) {
  if (!node.result) return 'running'
  const r = String(node.result.result || '')
  if (r.startsWith('[permission denied]')) return 'denied'
  if (r.startsWith('[tool error]')) return 'error'
  return 'ok'
}

const TOOL_STATE_LABEL = { running: '执行中', ok: '完成', denied: '被拒绝', error: '失败' }

function toolStateLabel(node) {
  return TOOL_STATE_LABEL[toolState(node)]
}

function toggleTool(id) {
  openTools.value = { ...openTools.value, [id]: !openTools.value[id] }
}

/** 审批参数美化：优先给原始命令/路径正文，避免 JSON 转义（\" 很难看）。
 *  loop 落的事件 arguments 是对象；runtime 广播的是 str(arguments)（Python dict 的字符串表示）。 */
function formatArgs(a) {
  if (a === undefined || a === null || a === '') return ''
  if (typeof a === 'object') {
    for (const k of TOOL_ARG_KEYS) {
      if (a[k] !== undefined && a[k] !== '') return String(a[k])
    }
    try {
      return JSON.stringify(a, null, 2)
    } catch {
      return String(a)
    }
  }
  const s = String(a)
  const m = s.match(/^\{?\s*['"][A-Za-z_]+['"]\s*:\s*([\s\S]*?)\s*\}?$/)
  if (m) {
    let v = m[1].trim().replace(/,$/, '').trim()
    if (v.length >= 2 && v[0] === v[v.length - 1] && (v[0] === "'" || v[0] === '"')) {
      v = v.slice(1, -1)
    }
    return v || s
  }
  return s
}

/** 把事件流重组为「工具卡 / 普通事件」两种节点（tool_result 合并进工具卡）。 */
const renderedTimeline = computed(() => {
  const byCall = new Map()
  for (const ev of timeline.value) {
    if (ev.type === 'tool_result' && ev.payload?.call_id) {
      byCall.set(ev.payload.call_id, ev.payload)
    }
  }
  const out = []
  for (const ev of timeline.value) {
    if (ev.type === 'tool_result') continue // 已并入对应工具卡
    // 任务清单写入（M30）不铺成工具行：输入框上方的面板就是它的渲染
    // （轨迹里仍在，可审计；界面上再铺一行只是重复）。
    if (ev.type === 'tool_use' && ev.payload?.call?.name === 'todo_write') continue
    // 检查点事件（code=segment_boundary）只是调度上的换气：轨迹里留着可审计，
    // 界面上不必显示——用户真正需要看到的是紧接着那条"已自动继续"提示。
    // 不跳过的话，一个检查点会冒两行淡字，等于把噪音做小而不是去掉。
    if (ev.type === 'error' && ev.payload?.code === 'segment_boundary') continue
    if (ev.type === 'tool_use') {
      const call = ev.payload?.call || {}
      out.push({
        kind: 'tool',
        key: 't' + (call.id || ev.seq),
        call,
        result: byCall.get(call.id) || null,
        ts: ev.ts,
        // 这一步的思考（在 pushEvent 里按轮挂到 tool_use 上）：渲染在这条工具行上方
        thinking: ev.payload?.thinking || '',
        thinking_ms: ev.payload?.thinking_ms || 0,
      })
      continue
    }
    out.push({ kind: 'event', key: 'e' + ev.seq, ev, internal: internalMessagePresentation(ev), incomplete: ev.type === 'done' ? incompleteCompletion(ev.payload) : null })
  }
  return out
})

// 底部统计行：全部由真实事件推导，不造假数据
const stats = computed(() => {
  const turns = timeline.value.filter((e) => e.type === 'done').length
  const tools = timeline.value.reduce(
    (n, e) => n + (Array.isArray(e.payload?.tool_calls) ? e.payload.tool_calls.length : 0), 0)
  const prompts = timeline.value.filter((e) => e.type === 'user_message' && !internalMessagePresentation(e)).length
  return {
    prompts,
    turns,
    tools,
    total: timeline.value.length,
    input: usage.value.input,
    output: usage.value.output,
    cached: usage.value.cached,
  }
})

function fmtTokens(n) {
  if (!n) return '0'
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}

// 缓存命中率（占输入 token 的百分比；0 时不显示）
const cachePct = computed(() => {
  if (!stats.value.input || !stats.value.cached) return 0
  return Math.min(99, Math.max(1, Math.round((stats.value.cached / stats.value.input) * 100)))
})

function autoGrow() {
  const ta = taEl.value
  if (!ta) return
  ta.style.height = 'auto'
  ta.style.height = Math.min(ta.scrollHeight, 190) + 'px'
}

let modelInfo = null // 模型设置（M36：只有一个默认模型），onMounted 时拉取

// ---- [+] 引用菜单（添加文件 / 引用会话内文件 / 技能 / 插件 / 连接器） ----
const addMenuOpen = ref(false)
const addExpand = ref('')       // 当前展开的子列表：files|skills|plugins|mcp
const catalog = ref(null)       // /api/v1/tools 的能力目录（首次打开菜单时懒加载）
const convoFiles = ref([])      // 本会话已添加的文本文件 [{name, chars}]
const fileEl = ref(null)        // 隐藏文件选择器
const composerHint = ref('')    // 输入区上方的轻提示（替代弹窗打扰）
let hintTimer = null
const statPop = ref('')         // 统计弹窗：'' | 'session' | 'tokens'
const lastStopReason = ref('end_turn')  // 最近一次 done 的收口原因（max_turns 时给「继续」按钮）

// M29：成对检查点（回退入口）与有序验收清单
//   checkpoints 按 anchor_seq 挂在对应那一行上；acceptance 是"做到哪儿了"的显性展示
const checkpoints = ref([])
const acceptance = ref(null)
const rewindBusy = ref(false)
//: 时间线重建的并发令牌 + "正在回放"标记。
//: 为什么要令牌：回退时**两处**都会触发重建（回退请求的返回 + WS 推来的回退事件），
//: 两次重建并发跑，晚清空的那次会把早推入的那条报告清掉、而它的 forEach 又因为
//: lastSeq 已经被推进而把同一条事件跳过——于是"回退报告凭空消失"（真机踩到过）。
//: `replaying` 则防止"回放历史时又看到 rewind 事件 → 再触发一次重建"的死循环。
let rebuildToken = 0
let replaying = false

/** 整条时间线从服务端重建（回退后轨迹被截断，本地的增量视图已经不可信）。 */
async function rebuildTimeline() {
  const id = current.value?.id
  if (!id) return false
  const session = sessionToken
  const mine = ++rebuildToken
  let history
  try {
    history = await api.events(id, 0)
  } catch {
    return false
  }
  if (mine !== rebuildToken || !isCurrentSession(id, session)) return false
  replaying = true
  try {
    events.value = []
    lastSeq = 0
    usage.value = { input: 0, output: 0, cached: 0 }
    runStats.value = []
    runStartTs = firstDeltaTs = 0
    busy.value = false
    phase.value = ''
    approvalVersion += 1
    clearApproval()
    streaming.value = thinking.value = ''
    thinkSink.buffer = ''
    thinkSink.startedTs = 0
    thinkOpen.value = {}
    lastStopReason.value = 'end_turn'
    history.forEach((ev) => pushEvent(ev))
  } finally {
    replaying = false
  }
  return true
}

/** 回退到某条检查点之前的对话，已生成文件保留。 */
async function rewindTo(ck) {
  if (!current.value || !ck || rewindBusy.value) return
  const id = current.value.id
  const token = sessionToken
  const checkpointId = ck.id
  const label = ck.kind === 'prompt' ? '这条消息之前' : '这一步'
  return dialog.open({
    title: '回退到检查点',
    description: `回退到${label}，撤回该步及之后的对话记录。`,
    details: '已上传的附件和已生成的文件仍保留，不随对话回退删除。',
    tone: 'warning',
    icon: 'history',
    confirmText: '确认回退',
    pendingText: '正在回退…',
    onConfirm: async () => {
      if (!isCurrentSession(id, token)) throw new Error('当前会话已切换，请关闭弹窗后重新选择检查点。')
      rewindBusy.value = true
      try {
        const out = await api.rewind(id, checkpointId)
        if (!isCurrentSession(id, token)) return out
        await rebuildTimeline()
        if (!isCurrentSession(id, token)) return out
        loadCheckpoints().catch(() => {})
        loadAcceptance().catch(() => {})
        showHint('已回退对话；附件和已生成文件保留')
        return out
      } finally { rewindBusy.value = false }
    },
  })
}

/** 某个事件（按 seq）上挂着的检查点；没有就返回 null。 */
function checkpointAt(seq) {
  return checkpoints.value.find((c) => c.anchor_seq === seq && c.kind === 'prompt') || null
}

async function loadCheckpoints() {
  if (!current.value) { checkpoints.value = []; return }
  const id = current.value.id
  const mine = sessionToken
  const seq = lastSeq
  try {
    const result = await api.checkpoints(id)
    if (isCurrentSession(id, mine)) {
      checkpoints.value = result.checkpoints || []
      if (!runtimeMetaReceived && lastSeq === seq && typeof result.busy === 'boolean') busy.value = result.busy
    }
  } catch { if (isCurrentSession(id, mine)) checkpoints.value = [] }
}

async function loadAcceptance() {
  if (!current.value) { acceptance.value = null; return }
  const id = current.value.id
  const mine = sessionToken
  try {
    const a = await api.acceptance(id)
    if (isCurrentSession(id, mine)) acceptance.value = a?.declared ? a : null
  } catch { if (isCurrentSession(id, mine)) acceptance.value = null }
}

/** 验收清单的一行摘要（只有声明了才显示，不占平时的注意力）。 */
const acceptanceSummary = computed(() => {
  const a = acceptance.value
  if (!a || !a.total) return ''
  return `验收 ${a.done}/${a.total}`
})

// ---- M30 任务清单：输入框上方的进度面板 ----
// 数据来源是模型自己写的 todo_write 快照（见 lib/todos.js 的三条规则），
// 不是界面猜的。面板默认展开（它就是要让人一眼看到进度），点标题可收起。
const taskOpen = ref(true)
const todos = computed(() => currentTodos(timeline.value))
const todoStat = computed(() => todoCounts(todos.value))
const todoHasItems = computed(() => !!todos.value && todos.value.length > 0)

// ---- M31 提问卡：模型挂起等答案时，它**替代**输入框（截图里就是这种形态） ----
// 一屏一题、可翻页；选中单选会自动跳到下一题；"跳过本题"把该题标成没答并直接提交。
const question = computed(() => pendingQuestion(timeline.value))
const qIndex = ref(0)
const qDrafts = ref({})          // { [questionId]: { selected: [], custom: '', skipped: false } }
const qBusy = ref(false)
const qError = ref('')
const qMinimized = ref(false)

const qCurrent = computed(() => {
  const list = question.value?.questions || []
  return list[Math.min(qIndex.value, Math.max(0, list.length - 1))] || null
})

function qDraft(qid) {
  return qDrafts.value[qid] || { selected: [], custom: '', skipped: false }
}

function setDraft(qid, patch) {
  qDrafts.value = { ...qDrafts.value, [qid]: { ...qDraft(qid), ...patch } }
}

/** 单选：点一下选中，并自动翻到下一题（多选要保持停留，让人继续勾）。 */
function pickOption(opt) {
  const q = qCurrent.value
  if (!q) return
  qError.value = ''
  if (q.multiSelect) {
    const selected = qDraft(q.id).selected
    const next = selected.includes(opt.label)
      ? selected.filter((s) => s !== opt.label)
      : [...selected, opt.label]
    setDraft(q.id, { selected: next, skipped: false })
    return
  }
  setDraft(q.id, { selected: [opt.label], skipped: false })
  if (qIndex.value < (question.value?.questions.length || 1) - 1) qIndex.value += 1
}

function goQuestion(step) {
  const total = question.value?.questions.length || 1
  qIndex.value = Math.min(Math.max(0, qIndex.value + step), total - 1)
  qError.value = ''
}

/** "下一题 / 提交"：没答就不许前进（照 DSH 的 error.unanswered 口径）。 */
async function advanceQuestion() {
  const list = question.value?.questions || []
  const q = qCurrent.value
  if (!q) return
  if (!draftCompleted(qDraft(q.id))) {
    qError.value = '请选择一个选项或填写自定义答案。'
    return
  }
  qError.value = ''
  if (qIndex.value < list.length - 1) {
    qIndex.value += 1
    return
  }
  await submitQuestions()
}

/** "跳过本题"：标成 skipped 并**直接提交整组**（其余没答的题会被拦下）。 */
async function skipQuestion() {
  const q = qCurrent.value
  if (!q) return
  setDraft(q.id, { selected: [], custom: '', skipped: true })
  await submitQuestions()
}

async function submitQuestions() {
  const pending = question.value
  if (!pending || qBusy.value) return
  const answers = buildAnswers(pending.questions, qDrafts.value)
  if (answers === null) {
    // 还有题没答也没跳过：跳到那一题并提示（不静默丢掉这组答案）
    const missing = pending.questions.findIndex((q) => !draftCompleted(qDrafts.value[q.id]))
    qIndex.value = Math.max(0, missing)
    qError.value = '请先完成这道问题。'
    return
  }
  qBusy.value = true
  qError.value = ''
  try {
    await api.answerQuestion(current.value.id, pending.id, { answers })
    // 回答事件由服务端落轨迹并推回来，卡片的消失交给 pendingQuestion 推导
  } catch (e) {
    qError.value = e.message || '提交失败'
  } finally {
    qBusy.value = false
  }
}

async function dismissQuestions() {
  const pending = question.value
  const id = current.value?.id
  if (!pending || !id || qBusy.value) return
  const token = sessionToken
  const questionId = pending.id
  return dialog.open({
    title: '放弃这组提问',
    description: '模型会收到你放弃了整组提问的通知，不会再追问。',
    details: `这组提问包含 ${pending.questions.length} 道问题。已选择的答案不会提交。`,
    tone: 'warning',
    icon: 'chat',
    confirmText: '放弃提问',
    pendingText: '正在关闭…',
    onConfirm: async () => {
      if (!isCurrentSession(id, token) || question.value?.id !== questionId) {
        throw new Error('这组提问已更新或当前会话已切换，请关闭弹窗后重新确认。')
      }
      qBusy.value = true
      qError.value = ''
      try { return await api.answerQuestion(id, questionId, { dismiss: true }) }
      finally {
        if (isCurrentSession(id, token) && question.value?.id === questionId) qBusy.value = false
      }
    },
  })
}

// 新的一组提问（或换会话）→ 答题状态从零开始：否则上一组的勾选会串到这一组上
watch(
  () => question.value?.id || '',
  (id) => {
    qIndex.value = 0
    qDrafts.value = {}
    qError.value = ''
    qBusy.value = false
    qMinimized.value = false
  },
)

// 模型配置档案切换（composer 右下角）
const profMenuOpen = ref(false)
const profiles = ref([])
const isAdmin = computed(() => props.user?.role === 'admin')

function showHint(text) {
  composerHint.value = text
  clearTimeout(hintTimer)
  hintTimer = setTimeout(() => { composerHint.value = '' }, 4000)
}

async function toggleAddMenu(ev) {
  if (addMenuOpen.value) { addMenuOpen.value = false; addExpand.value = ''; return }
  closePops()
  if (ev) popStyle('add', ev, 320, { align: 'left', gap: 8, maxH: 360 })
  addMenuOpen.value = true
  if (!catalog.value) {
    try { catalog.value = await api.tools() } catch { /* 目录拉不到只影响技能/插件项 */ }
  }
}
function togglePermMenu(ev) {
  if (permMenuOpen.value) { closeChoiceMenu('perm'); return }
  openChoiceMenu('perm', ev)
}
function toggleStatPop(kind, ev) {
  if (statPop.value === kind) { statPop.value = ''; return }
  closePops()
  popStyle('stat', ev, 320, { align: 'center' })
  statPop.value = kind
}
function toggleExpand(key) {
  addExpand.value = addExpand.value === key ? '' : key
}

function insertAtCursor(text) {
  input.value = (input.value ? input.value.replace(/\s+$/, '') + '\n' : '') + text
  addMenuOpen.value = false
  nextTick(() => { taEl.value?.focus(); autoGrow() })
}

const TEXT_EXT = /\.(txt|md|markdown|json|csv|tsv|py|js|ts|vue|jsx|tsx|java|html?|css|ya?ml|log|sql|xml|ini|toml|sh|bat|c|cpp|h|go|rs)$/i
const MAX_FILE_BYTES = 200 * 1024
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024
const pendingFiles = ref([])    // 待随消息发送的附件（任意格式，已上传到工作区）

function isInlineText(f) {
  return f.size <= MAX_FILE_BYTES && (f.type.startsWith('text/') || TEXT_EXT.test(f.name))
}

function fmtSize(n) {
  if (!Number.isFinite(n) || n < 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// 统一入口：小文本直接插入输入框，其余任意格式上传到会话工作区随消息发送
async function handlePickedFile(f) {
  if (!current.value?.id) {
    showHint('先选择或新建一个会话，再添加文件')
    return
  }
  const sid = current.value.id
  const mine = sessionToken
  if (isInlineText(f)) {
    try {
      const text = await f.text()
      if (!isCurrentSession(sid, mine)) return
      insertAtCursor('```\n// 文件: ' + f.name + '\n' + text + '\n```')
      convoFiles.value.push({ name: f.name, chars: text.length })
    } catch { if (isCurrentSession(sid, mine)) showHint(`「${f.name}」读取失败`) }
    return
  }
  if (f.size > MAX_UPLOAD_BYTES) {
    showHint(`「${f.name}」超过 10MB 上限，请缩小文件后上传`)
    return
  }
  if (pendingFiles.value.some((x) => x.name === f.name && x.size === f.size)) {
    showHint(`「${f.name}」已在上传列表里`)
    return
  }
  try {
    const b64 = await readFileBase64(f)
    if (!isCurrentSession(sid, mine)) return
    const meta = await api.uploadSessionFile(sid, f.name, b64)
    if (!isCurrentSession(sid, mine)) return
    pendingFiles.value.push(meta)
    showHint(`已上传「${meta.name}」（${fmtSize(meta.size)}），将随下一条消息一起发送`)
  } catch (e) {
    if (isCurrentSession(sid, mine)) showHint(`「${f.name}」上传失败：${e.message}`)
  }
}

async function onFilePicked(e) {
  const picked = [...(e.target.files || [])]
  e.target.value = ''
  addMenuOpen.value = false
  for (const f of picked) await handlePickedFile(f)
}

// 直接粘贴文件（截图 / 从资源管理器复制的文件）→ 与"添加文件"同一条链路
async function onPasteFiles(e) {
  const files = [...(e.clipboardData?.files || [])]
  if (!files.length) return        // 纯文本粘贴走默认行为
  e.preventDefault()
  addMenuOpen.value = false
  for (const f of files) await handlePickedFile(f)
}

const skills = computed(() => (catalog.value?.packs || []).filter((p) => p.kind === 'skill'))
const pluginsAvail = computed(() => catalog.value?.plugins || [])
const mcpServers = computed(() => catalog.value?.mcp || [])
function truncate(text, n) { return (text || '').length > n ? text.slice(0, n) + '…' : text || '' }

// ---- 会话统计（全部由事件时间戳/usage 推导，不造假数据） ----
const runStats = ref([])        // 每次对话 {dur, ttft}；历史回放无 delta → ttft=0
let runStartTs = 0
let firstDeltaTs = 0

const sessionStats = computed(() => {
  const runs = runStats.value
  const totalDur = runs.reduce((s, r) => s + r.dur, 0)
  const ttfts = runs.filter((r) => r.ttft > 0).map((r) => r.ttft)
  return {
    runs: runs.length,
    totalDur,
    avgDur: runs.length ? totalDur / runs.length : 0,
    ttftAvg: ttfts.length ? ttfts.reduce((s, t) => s + t, 0) / ttfts.length : 0,
    avgTps: totalDur > 0 ? usage.value.output / totalDur : 0,
  }
})

function fmtDur(sec) {
  if (!sec || sec <= 0) return '—'
  const m = Math.floor(sec / 60), s = Math.round(sec % 60)
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`
}

async function toggleModelMenu(ev) {
  if (profMenuOpen.value) { profMenuOpen.value = false; return }
  closePops()
  if (ev) popStyle('model', ev, 300, { align: 'right', gap: 8, maxH: 320 })
  profMenuOpen.value = true
  if (!profiles.value.length) {
    try { profiles.value = await sessionCall('GET', '/api/v1/model-profiles') } catch { /* 拉不到就显示空菜单 */ }
  }
}

async function activateModel(p) {
  profMenuOpen.value = false
  if (!isAdmin.value) { showHint('仅管理员可切换模型配置'); return }
  if (p.active) return
  try {
    const view = await sessionCall('POST', `/api/v1/model-profiles/${p.id}/activate`)
    activeModel.value = view.active_model || ''
    modelName.value = view.active_model || modelName.value
    await Promise.all([loadSessions(), (async () => { try { profiles.value = await sessionCall('GET', '/api/v1/model-profiles') } catch {} })()])
    showHint(`已切换到「${p.name}」，新会话即用新模型`)
  } catch (e) { showHint(e.message) }
}

function pushEvent(ev) {
  if (ev.type === 'session_meta') {
    // WS 首帧带 {session_id, model, model_override, web_enabled}；ping 的 pong 也走这个类型
    if (ev.payload?.model) modelName.value = ev.payload.model
    if (ev.payload?.title && current.value) {
      current.value = { ...current.value, title: ev.payload.title }
      const i = sessions.value.findIndex(s => s.id === current.value.id)
      if (i >= 0) sessions.value[i] = { ...sessions.value[i], title: ev.payload.title }
    }
    if (typeof ev.payload?.busy === 'boolean') {
      runtimeMetaReceived = true
      busy.value = ev.payload.busy
    }
    if (typeof ev.payload?.phase === 'string') phase.value = ev.payload.phase
    if (!busy.value) phase.value = ''
    const pendingApproval = ev.payload?.pending_approval
    const hasApprovalSnapshot = !replaying && ev.seq === 0 && Object.hasOwn(ev.payload || {}, 'pending_approval')
      && (pendingApproval === null || (pendingApproval && typeof pendingApproval === 'object' && !Array.isArray(pendingApproval)
        && typeof pendingApproval.approval_id === 'string' && pendingApproval.approval_id.trim()))
    if (hasApprovalSnapshot) {
      approvalVersion += 1
      approvalSnapshotReceived = true
      refreshApprovalOnMeta = false
      approvalError.value = ''
      if (pendingApproval) {
        if (approval.value?.approval_id !== pendingApproval.approval_id) approvalSubmitting.value = ''
        approval.value = pendingApproval
        busy.value = true
        phase.value = 'waiting_approval'
      } else clearApproval()
    } else if (refreshApprovalOnMeta && !replaying && ev.seq === 0) {
      refreshApprovalOnMeta = false
      void restorePendingApproval()
    }
    // 联网开关（M19）：本地没有显式覆盖时采用会话行里的状态
    if (typeof ev.payload?.web_enabled === 'boolean' && localWebOverride(current.value?.id) === null) {
      webOn.value = !!ev.payload.web_enabled && webInfo.value?.available !== false
    }
    return
  }
  // 流式增量不落轨迹（seq=0），只累积到打字机缓冲；ts 用于 TTFT 统计
  if (ev.type === 'thinking_delta') {
    // 思考缓冲按**轮**分段：一轮 = 一次模型响应（可能带工具调用）。
    // 以前是一路 += 到同一条字符串里，25 轮的工具循环就把 25 段思考粘成一大坨，
    // 相邻两段常常在重复同一套计划（用户原话："思考时老是有重复的部分"）。
    pushThinking(thinkSink, ev.payload?.text || '', ev.ts)
    thinking.value = thinkSink.buffer
    scrollDown()
    return
  }
  if (ev.type === 'assistant_delta') {
    if (!firstDeltaTs && ev.ts) firstDeltaTs = ev.ts
    streaming.value += ev.payload?.text || ''
    scrollDown()
    return
  }
  // 回退会重置服务端 seq；先识别事实变更，再做正常增量去重。
  if (ev.payload?.rewind && current.value && !replaying) {
    rebuildTimeline()
    return
  }
  if (ev.seq && ev.seq <= lastSeq) return
  if (ev.seq) lastSeq = ev.seq
  // 这一轮的思考挂到"轮到它收口的那条事件"上，保证每段思考只出现一次：
  //   带工具调用的轮 → 挂在 tool_use 上（渲染在那次调用上方）
  //   收口轮        → 挂在 assistant_message / done 上（渲染在回答上方）
  attachThinking(ev, thinkSink)
  thinking.value = thinkSink.buffer
  events.value.push(ev)
  if (events.value.length > MAX_EVENTS) events.value.splice(0, events.value.length - MAX_EVENTS)
  // 段末检查点/熔断的用量落在 error；与 done 一样只累计已去重的持久事件。
  const eventUsage = ev.payload?.usage
  if (ev.seq > 0 && (ev.type === 'done' || ev.type === 'error') && eventUsage) {
    usage.value.input += Number(eventUsage.input_tokens) || 0
    usage.value.output += Number(eventUsage.output_tokens) || 0
    usage.value.cached += Number(eventUsage.cached_tokens) || 0
  }
  if (ev.type === 'approval_request' && ev.payload?.approval_id) {
    approvalVersion += 1
    approvalError.value = ''
    if (approval.value?.approval_id !== ev.payload.approval_id) approvalSubmitting.value = ''
    approval.value = ev.payload
    busy.value = true
    phase.value = 'waiting_approval'
  }
  if (ev.type === 'approval_decided' && ev.payload?.approval_id) {
    approvalVersion += 1
    if (approval.value?.approval_id === ev.payload.approval_id) clearApproval()
  }
  if (ev.type === 'user_message') {
    const internal = internalMessagePresentation(ev)
    if (!internal) { phase.value = ''; banner.value = '' }
    busy.value = true
    runStartTs = ev.ts; firstDeltaTs = 0

    if (internal) {
      // 系统自动续跑：上次达到最大轮数被截断。恢复 busy（done 已把它清掉）
      busy.value = true
      streaming.value = ''
    }
  }
  if (ev.type === 'error') {
    if (ev.payload?.code === 'busy') {
      // 后端拒收第二条消息：任务其实还在跑，不能把 busy 清掉（曾因此状态脱节）
      showHint(ev.payload?.message || '会话正在执行中')
      busy.value = true
      return
    }
    // 熔断（M29）：这不是系统故障，是**主动停下等人**——所以不走顶部红条，
    // 只在时间线里留一张说明卡（完整原因在里面），把状态正确地标成"不在跑"。
    if (ev.payload?.code === 'repeated_failure') {
      busy.value = false
      phase.value = ''
      banner.value = ''
      streaming.value = ''
      return
    }
    // quiet 错误（code=segment_boundary：本轮到检查点、马上续跑）不是故障：
    // 不弹红条、不清 busy——否则每个检查点都像任务炸了一次。
    if (!ev.payload?.quiet) {
      banner.value = ev.payload?.message || '发生错误'
      busy.value = false
      phase.value = ''
    }
  }
  if (ev.type === 'done' || ev.type === 'assistant_message') {
    // 收口：思考已在上面按轮挂到 ev 上了（这里只负责清缓冲，让界面不再显示"流式中"那一行）。
    // 以前是"思考留在页面底部当独立一屏"，和回答分家、还排在最后一条回答之后
    // （用户原话："思考过程不要放在一起"）。现在思考属于它自己那一步。
    attachThinking(ev, thinkSink)
    thinking.value = ''
    streaming.value = ''
    if (ev.type === 'done') {
      approvalVersion += 1
      clearApproval()
      busy.value = false
      phase.value = ''
      if (isSuccessfulCompletion(ev.payload)) banner.value = ''
      lastStopReason.value = ev.payload?.stop_reason || 'end_turn'
      // 单轮计时（user_message → done，含工具步）
      if (runStartTs && ev.ts >= runStartTs) {
        runStats.value.push({ dur: ev.ts - runStartTs, ttft: firstDeltaTs ? firstDeltaTs - runStartTs : 0 })
      }
      runStartTs = 0
      firstDeltaTs = 0
      // 收口后刷新验收进度与检查点：回退入口要以"现在"为最新（M29）
      if (!replaying) {
        loadAcceptance().catch(() => {})
        loadCheckpoints().catch(() => {})
      }
    }
  }
  scrollDown()
}

//: "粘在底部"：只有当用户本来就在底部附近时才自动跟随。
//: 以前无条件 scrollTop = scrollHeight，于是流式输出期间用户往上翻会被反复拽回底部
//: （用户原话："无法上下拉动，会自动回到最下面"）。DSH 也是这个策略：用户手动上滑即停止跟随，
//: 并给一个「回到最新」的入口。
const stickBottom = ref(true)
const BOTTOM_SLACK = 48

function onStreamScroll() {
  const el = listEl.value
  if (!el) return
  stickBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK
}

function scrollDown(force = false) {
  if (!force && !stickBottom.value) return
  nextTick(() => {
    const el = listEl.value
    if (!el) return
    el.scrollTop = el.scrollHeight
    stickBottom.value = true
  })
}

async function loadSessions() {
  sessions.value = generalChatSessions(await api.listSessions())
}

// ---- 会话操作：重命名 / 分叉 / 归档（⋯ 菜单） ----
const showArchived = ref(false)
const menuFor = ref(null)       // 打开 ⋯ 菜单的会话：{ id, x, y }（fixed 定位坐标）

// 菜单用 fixed 定位贴着按钮弹出：会话列表是 overflow-y:auto 的滚动容器，
// absolute 定位的菜单会被容器裁掉（真机踩过：底部会话只露出半截菜单）。
const MENU_W = 150
const MENU_H = 160  // 四项 + padding 的估算高度，用于判断要不要向上弹

function toggleMenu(s, ev) {
  if (menuFor.value?.id === s.id) { menuFor.value = null; return }
  const rect = ev.currentTarget.getBoundingClientRect()
  const x = Math.max(6, Math.min(rect.right - MENU_W + 10, window.innerWidth - MENU_W - 6))
  const y = rect.bottom + MENU_H > window.innerHeight - 8
    ? Math.max(6, rect.top - MENU_H - 4)   // 下方放不下 → 向上弹
    : rect.bottom + 4
  menuFor.value = { id: s.id, x, y }
}

const visibleSessions = computed(() =>
  sessions.value.filter((s) => (showArchived.value ? s.status === 'archived' : s.status !== 'archived')))

// ---- 侧栏按工作区分组（folder 为空 = 默认工作区，永远排第一） ----
// 分组名册单独落库，否则空工作区没有会话承载就会"新建完就消失"。
const DEFAULT_FOLDER = ''
const workspaces = ref([])          // [{ name, count }]，来自 /api/v1/workspaces
const collapsedGroups = ref({})     // folder → true 表示折叠
//: 会话搜索（参考 DSH：列表头有个搜索图标，点开是一个过滤框）
const sessionSearch = ref(false)
const sessionQuery = ref('')
const searchInputEl = ref(null)

function toggleSessionSearch() {
  sessionSearch.value = !sessionSearch.value
  if (!sessionSearch.value) sessionQuery.value = ''
  else nextTick(() => searchInputEl.value?.focus())
}

/** 相对时间（对齐 DSH 的紧凑写法）：刚刚 / N分钟 / N小时 / N天；更久给日期。 */
function relTime(ts) {
  if (!ts) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return ''
  const diff = Date.now() - d.getTime()
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min}分钟`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour}小时`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day}天`
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

async function loadWorkspaces() {
  try {
    workspaces.value = await api.listWorkspaces()
  } catch {
    workspaces.value = []   // 拿不到就退化成分组只由会话推导
  }
}

const groupedSessions = computed(() => {
  const q = sessionQuery.value.trim().toLowerCase()
  const source = q
    ? visibleSessions.value.filter((s) => (s.title || '').toLowerCase().includes(q))
    : visibleSessions.value
  const map = new Map()
  for (const s of source) {
    const key = (s.folder || '').trim()
    if (!map.has(key)) map.set(key, [])
    map.get(key).push(s)
  }
  // 先按名册建组（含刚建好还没会话的空组），再把会话挂进去
  const groups = []
  const seen = new Set()
  for (const w of workspaces.value) {
    if (seen.has(w.name)) continue
    seen.add(w.name)
    groups.push({ folder: w.name, items: map.get(w.name) || [] })
  }
  for (const [folder, items] of map) {
    if (seen.has(folder)) continue
    groups.push({ folder, items })
  }
  // sessions 本身按 updated_at 倒序，所以 items[0] 就是该组最近活跃的会话
  groups.sort((a, b) => {
    if (a.folder === DEFAULT_FOLDER) return -1
    if (b.folder === DEFAULT_FOLDER) return 1
    return String(b.items[0]?.updated_at || '').localeCompare(String(a.items[0]?.updated_at || ''))
  })
  if (showArchived.value) return groups.filter((g) => g.items.length)
  // 搜索时把命中的会话留在列表里，空组不显示（否则搜一个词还留一堆空工作区）
  if (q) return groups.filter((g) => g.items.length)
  return groups
})

function toggleGroup(folder) {
  collapsedGroups.value = { ...collapsedGroups.value, [folder]: !collapsedGroups.value[folder] }
}

async function createWorkspace() {
  return dialog.open({
    title: '新建工作区',
    description: '用工作区分组管理相关会话。',
    icon: 'folder',
    confirmText: '创建工作区',
    pendingText: '正在创建…',
    input: { label: '工作区名称', value: '', placeholder: '例如：产品研发', required: true },
    onConfirm: async (name) => {
      const ws = await api.createWorkspace(name)
      if (viewDisposed) return ws
      await loadWorkspaces()
      if (viewDisposed) return ws
      if (!workspaces.value.some(item => item.name === ws.name)) workspaces.value.push(ws)
      collapsedGroups.value = { ...collapsedGroups.value, [ws.name]: false }
      return ws
    },
  })
}

async function deleteWorkspace(name) {
  const target = String(name)
  const count = workspaces.value.find(ws => ws.name === target)?.count
    ?? sessions.value.filter(session => (session.folder || '') === target).length
  return dialog.open({
    title: '删除工作区',
    description: count ? `其中 ${count} 个会话将移回默认工作区，会话本身会保留。` : '删除这个空的会话分组。',
    details: target,
    tone: 'danger',
    icon: 'folder',
    confirmText: '删除工作区',
    pendingText: '正在删除…',
    onConfirm: async () => {
      const result = await api.deleteWorkspace(target)
      if (viewDisposed) return result
      workspaces.value = workspaces.value.filter(ws => ws.name !== target)
      sessions.value = sessions.value.map(session => session.folder === target ? { ...session, folder: '' } : session)
      if (current.value?.folder === target) current.value = { ...current.value, folder: '' }
      await refreshSessionsAfterChange()
      return result
    },
  })
}

async function moveSession(s) {
  menuFor.value = null
  const id = s.id
  const folder = s.folder || ''
  const title = s.title || '未命名会话'
  return dialog.open({
    title: '移动会话',
    description: '选择已有工作区，或输入新名称创建分组。',
    details: title,
    icon: 'folder',
    confirmText: '移动会话',
    pendingText: '正在移动…',
    input: {
      label: '目标工作区', value: folder, placeholder: '留空移回默认工作区',
      suggestions: [...new Set(groupedSessions.value.map(group => group.folder).filter(Boolean))],
      hint: '留空表示默认工作区。',
    },
    onConfirm: async (destination) => {
      if (destination === folder) return
      const meta = await api.moveSessionFolder(id, destination)
      if (viewDisposed) return meta
      const i = sessions.value.findIndex(session => session.id === id)
      if (i >= 0) sessions.value[i] = { ...sessions.value[i], ...meta }
      if (current.value?.id === id) current.value = { ...current.value, ...meta }
      await loadWorkspaces()
      if (!viewDisposed && destination) collapsedGroups.value = { ...collapsedGroups.value, [destination]: false }
      return meta
    },
  })
}

// A completed mutation must not be repeated just because the subsequent list read failed.
async function refreshSessionsAfterChange() {
  try {
    const list = await api.listSessions()
    if (!viewDisposed) sessions.value = generalChatSessions(list)
  } catch (error) {
    if (!viewDisposed) banner.value = `操作已完成，但会话列表刷新失败：${error.message}`
  }
}

// 会话管理请求使用本对话应用的独立访问令牌。
async function sessionCall(method, path, body) {
  const headers = { Authorization: `Bearer ${getToken()}` }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const resp = await fetch(path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail
    throw new Error(detail || `HTTP ${resp.status}`)
  }
  return resp.json()
}

async function renameSession(s) {
  menuFor.value = null
  const id = s.id
  const previousTitle = s.title || ''
  return dialog.open({
    title: '重命名会话',
    description: '为会话设置一个容易识别的名称。',
    icon: 'edit',
    confirmText: '保存名称',
    pendingText: '正在保存…',
    input: { label: '会话名称', value: previousTitle, placeholder: '输入会话名称', required: true },
    onConfirm: async (title) => {
      if (title === previousTitle) return
      const meta = await sessionCall('POST', `/api/v1/sessions/${id}/rename`, { title })
      if (viewDisposed) return meta
      const i = sessions.value.findIndex(session => session.id === id)
      if (i >= 0) sessions.value[i] = { ...sessions.value[i], ...meta }
      if (current.value?.id === id) current.value = { ...current.value, ...meta }
      await refreshSessionsAfterChange()
      return meta
    },
  })
}

async function forkSession(s) {
  menuFor.value = null
  try {
    const meta = await sessionCall('POST', `/api/v1/sessions/${s.id}/fork`)
    await loadSessions()
    await selectSession(meta.id)
  } catch (e) { banner.value = e.message }
}

async function archiveSession(s) {
  menuFor.value = null
  const id = s.id
  const title = s.title || '未命名会话'
  return dialog.open({
    title: '归档会话',
    description: '归档后无法继续发送消息，历史记录可在“已归档”中查看。',
    details: title,
    icon: 'archive',
    confirmText: '归档会话',
    pendingText: '正在归档…',
    onConfirm: async () => {
      const result = await sessionCall('POST', `/api/v1/sessions/${id}/archive`)
      if (viewDisposed) return result
      sessions.value = sessions.value.map(session => session.id === id ? { ...session, status: 'archived' } : session)
      if (current.value?.id === id) { current.value = null; closeStream() }
      await refreshSessionsAfterChange()
      return result
    },
  })
}

async function deleteSession(s) {
  menuFor.value = null
  const id = s.id
  const title = s.title || '未命名会话'
  return dialog.open({
    title: '删除会话',
    description: '对话记录与用量统计将被永久清除。需要保留记录时，可以选择归档。',
    details: title,
    tone: 'danger',
    icon: 'trash',
    confirmText: '删除会话',
    pendingText: '正在删除…',
    onConfirm: async () => {
      const result = await sessionCall('DELETE', `/api/v1/sessions/${id}`)
      if (viewDisposed) return result
      sessions.value = sessions.value.filter(session => session.id !== id)
      if (current.value?.id === id) { current.value = null; closeStream() }
      await refreshSessionsAfterChange()
      return result
    },
  })
}

// ---- 上下文占用（系统提示词 / 工具定义 / 对话消息 三段估算）----
const ctxPop = ref(false)
const ctxInfo = ref(null)
const ctxLoading = ref(false)

async function toggleCtxPop(ev) {
  if (ctxPop.value) { ctxPop.value = false; return }
  closePops()
  if (ev) popStyle('ctx', ev, 340, { align: 'center' })
  ctxPop.value = true
  ctxLoading.value = true
  ctxInfo.value = null
  try {
    ctxInfo.value = await sessionCall('GET', `/api/v1/sessions/${current.value.id}/context`)
  } catch (e) { ctxInfo.value = { error: e.message } }
  finally { ctxLoading.value = false }
}

function fmtK(n) {
  if (n == null) return '-'
  if (n >= 1_000_000) return `~${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `~${(n / 1_000).toFixed(1)}K`
  return `~${n}`
}

function fmtWindow(n) {
  if (n >= 1_000_000) return `${Math.round(n / 1_000_000)}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`
  return String(n)
}

function ctxSeg(item) {
  // 弹窗进度条的分段宽度（相对窗口，保证三段加起来 = used_pct）
  if (!ctxInfo.value || !ctxInfo.value.window) return 0
  return (item / ctxInfo.value.window) * 100
}

async function newSessionIn(folder = '') {
  try {
    const meta = await api.createSession({
      folder,
      permissionMode: newPerm.value,
    })
    // 新建的会话要可见：展开它所属的工作区分组
    collapsedGroups.value = { ...collapsedGroups.value, [folder]: false }
    await loadSessions()
    await loadWorkspaces()
    await selectSession(meta.id)
  } catch (e) {
    banner.value = e.message || '创建会话失败，请重试'
  }
}

async function newSession() {
  await newSessionIn('')
}

async function startConversation(prompt = '') {
  if (starterBusy.value) return
  starterBusy.value = true
  try {
    if (!current.value) await newSession()
    if (!current.value) return
    if (prompt) input.value = prompt
    await nextTick()
    taEl.value?.focus()
    autoGrow()
  } catch (e) {
    banner.value = e.message || '会话创建失败，请重试'
  } finally {
    starterBusy.value = false
  }
}

async function selectSession(id) {
  sessionListOpen.value = false
  closeStream()
  closePreview()
  const mine = sessionToken
  const meta = sessions.value.find((s) => s.id === id) || { id }
  runtimeMetaReceived = false
  current.value = meta
  events.value = []
  lastSeq = 0
  banner.value = ''
  clearApproval()
  refreshApprovalOnMeta = false
  approvalSnapshotReceived = false
  busy.value = false
  phase.value = ''
  streaming.value = ''
  thinking.value = ''
  thinkSink.buffer = ''
  thinkSink.startedTs = 0
  thinkOpen.value = {}
  modelName.value = ''
  usage.value = { input: 0, output: 0, cached: 0 }
  lastStopReason.value = 'end_turn'
  webOn.value = !!meta.web_enabled
  runStats.value = []
  runStartTs = 0
  firstDeltaTs = 0
  convoFiles.value = []
  pendingFiles.value = []   // 附件跟着会话走：切会话不带过去
  addMenuOpen.value = false
  addExpand.value = ''
  statPop.value = ''
  ctxPop.value = false
  checkpoints.value = []
  acceptance.value = null
  loadWeb(id, mine).catch(() => {})   // 联网能力 + 本会话的开关状态（M19）
  // 先回放已有事件，再用 after_seq 接实时流。
  // 令牌自增：切换会话时让任何"在飞的"重建作废（否则它会把新会话的时间线写坏）。
  rebuildToken += 1
  let history = []
  try {
    history = await api.events(id, 0) || []
  } catch (e) {
    if (!isCurrentSession(id, mine)) return
    banner.value = e.message || '历史记录加载失败，正在恢复连接'
  }
  if (!isCurrentSession(id, mine)) return
  replaying = true
  try {
    history.forEach(pushEvent)
  } finally {
    replaying = false
  }
  connect(id)
  // M29：回退入口与验收进度（拉不到就当没有，不阻塞对话）
  loadCheckpoints().catch(() => {})
  loadAcceptance().catch(() => {})
  // 恢复挂起中的审批：审批弹窗走的是不落盘的实时事件，刷新后必须
  // 以 approvals 表兜底，否则后端在等批准、用户却永远看不到弹窗
  if (!approvalSnapshotReceived) await restorePendingApproval()
}

function connect(id) {
  connection.connect(id)
}

function closeStream() {
  sessionToken += 1
  rebuildToken += 1
  connection.close()
}

async function send() {
  const text = input.value.trim()
  if ((!text && !pendingFiles.value.length) || !current.value || busy.value) return
  const id = current.value.id
  const mine = sessionToken
  const sending = ++sendToken
  const files = pendingFiles.value
  let submitted = false
  const attachments = pendingFiles.value.map((f) => ({ name: f.name, path: f.path, size: f.size }))
  input.value = ''
  pendingFiles.value = []
  busy.value = true
  phase.value = ''
  banner.value = ''
  addMenuOpen.value = false
  permMenuOpen.value = false
  statPop.value = ''
  scrollDown(true)      // 自己刚发出去：强制跟随（即使用户之前翻到了上面）
  try {
    // 优先走 WS（事件会从流里回来）；WS 不可用时退回 REST
    const ws = connection.socket
    if (ws && ws.readyState === WebSocket.OPEN) {
      // 联网开关跟着这一轮走（M19）：后端据此决定是否把联网工具暴露给模型
      ws.send(JSON.stringify({ type: 'prompt', text, attachments, web: webOn.value }))
    } else {
      await api.prompt(id, text, attachments, { web: webOn.value })
      submitted = true
      if (!isCurrentSession(id, mine) || sending !== sendToken) return
      // 与 WS 共用 seq 去重和 done 用量累计，避免重连期间重复计费统计。
      const history = await api.events(id, lastSeq)
      if (!isCurrentSession(id, mine) || sending !== sendToken) return
      history.forEach(pushEvent)
    }
  } catch (e) {
    if (!isCurrentSession(id, mine) || sending !== sendToken) return
    banner.value = e.message
    busy.value = !submitted && e.status === 409 && /正在|上一个请求|busy/i.test(e.message)
    if (!busy.value) phase.value = ''
    // 提交失败时保留原稿；期间已输入的新内容和附件也要保留。
    if (!submitted) {
      if (!input.value) input.value = text
      const paths = new Set(pendingFiles.value.map((f) => f.path))
      pendingFiles.value = [...files.filter((f) => !paths.has(f.path)), ...pendingFiles.value]
    }
  }
}

function stopRun() {
  // 主动中断：后端会杀任务并把挂起中的审批一并拒绝（fail closed）
  const ws = connection.socket
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    showHint('会话连接尚未恢复，请连接后再停止任务')
    return
  }
  try {
    ws.send(JSON.stringify({ type: 'cancel' }))
  } catch (e) {
    showHint(e.message || '停止指令未发送，请重试')
    return
  }
  busy.value = false
  phase.value = ''
  approvalVersion += 1
  clearApproval()
  streaming.value = thinking.value = ''
  thinkSink.buffer = ''
  thinkSink.startedTs = 0
  showHint('已发送停止指令，正在中断当前任务…')
}

async function continueRun() {
  // 上次因最大轮数截断且配额耗尽时，用户手动续
  if (busy.value || !current.value) return
  input.value = '继续完成上面的任务；如果已经完成，直接给出结论。'
  await send()
}

async function decide(approved) {
  if (!approval.value || approvalSubmitting.value) return
  const pending = approval.value
  const id = current.value?.id
  const mine = sessionToken
  approvalVersion += 1
  approvalSubmitting.value = pending.approval_id
  try {
    const result = await api.decideApproval(pending.approval_id, approved)
    if (isCurrentSession(id, mine) && approval.value?.approval_id === pending.approval_id) {
      approvalVersion += 1
      clearApproval()
    }
    if (isCurrentSession(id, mine) && result?.delivered === false) {
      banner.value = '申请已结束，请重新发起操作'
      await restorePendingApproval()
    }
  } catch (e) {
    if (isCurrentSession(id, mine) && approval.value?.approval_id === pending.approval_id) banner.value = e.message
  } finally {
    if (isCurrentSession(id, mine) && approvalSubmitting.value === pending.approval_id) approvalSubmitting.value = ''
  }
}

function clearApproval() {
  approval.value = null
  approvalSubmitting.value = ''
  approvalError.value = ''
  if (phase.value === 'waiting_approval') phase.value = ''
}

async function restorePendingApproval() {
  const id = current.value?.id, mine = sessionToken, version = approvalVersion
  if (!id) return false
  try {
    const pending = await api.pendingApproval(id)
    if (!isCurrentSession(id, mine) || version !== approvalVersion) return false
    approvalVersion += 1
    approvalError.value = ''
    if (pending?.approval_id) {
      if (approval.value?.approval_id !== pending.approval_id) approvalSubmitting.value = ''
      approval.value = pending
      busy.value = true
      phase.value = 'waiting_approval'
    } else clearApproval()
    return true
  } catch (cause) {
    if (isCurrentSession(id, mine) && version === approvalVersion) {
      approvalError.value = cause?.message ? `无法核对审批状态：${cause.message}` : '无法核对审批状态，请重试'
    }
    return false
  }
}

function fmtTime(ts) {
  if (!ts) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  return isNaN(d.getTime()) ? '' : d.toLocaleTimeString('zh-CN', { hour12: false })
}

async function refreshModelSettings() {
  profiles.value = []
  try {
    const ms = await api.modelSettings()
    if (viewDisposed) return
    modelInfo = ms
    activeModel.value = ms?.active_model || ''
  } catch (e) { if (!viewDisposed) showHint(e.message) }
}

onMounted(async () => {
  window.addEventListener('resize', closePops)   // 弹层是 fixed 坐标，视口一变就失效
  window.addEventListener('click', onDocClick)  // 点空白处关掉所有悬浮弹层
  window.addEventListener('keydown', onEsc)
  window.addEventListener('chat-agent:model-settings-changed', refreshModelSettings)
  try { await loadSessions() } catch (e) { banner.value = e.message }
  await loadWorkspaces()
  try {
    const ms = await api.modelSettings()
    modelInfo = ms
    activeModel.value = ms?.active_model || ''
  } catch { /* 设置接口不可用不阻塞聊天 */ }
  await loadPermissionSettings()
})
onBeforeUnmount(() => {
  viewDisposed = true
  window.removeEventListener('resize', closePops)
  window.removeEventListener('click', onDocClick)
  window.removeEventListener('keydown', onEsc)
  window.removeEventListener('chat-agent:model-settings-changed', refreshModelSettings)
  clearTimeout(hintTimer)
  closePreview()
  closeStream()
})
</script>

<template>
  <div class="chat" :class="{ 'with-preview': !!preview }">
    <!-- 会话列表 -->
    <section id="session-list" class="session-list card" :class="{ 'mobile-open': sessionListOpen }" aria-label="会话列表">
      <div class="list-head">
        <div class="session-heading"><span class="section-caption">对话</span><strong>{{ showArchived ? '已归档' : '会话工作区' }}</strong></div>
        <div class="head-actions">
          <button class="icon-btn session-close" title="关闭会话列表" aria-label="关闭会话列表" @click="sessionListOpen = false">×</button>
          <!-- 三个图标按钮**同尺寸同风格**（都是 28×28 幽灵按钮，不搞一个蓝底特例） -->
          <button class="icon-btn" title="搜索会话" @click="toggleSessionSearch">
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden>
              <circle cx="7" cy="7" r="4.6" fill="none" stroke="currentColor" stroke-width="1.5" />
              <path d="M10.4 10.4L14 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
          </button>
          <button class="icon-btn" title="新建工作区" @click="createWorkspace">
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden>
              <path d="M1.8 4.2a1 1 0 011-1h3l1.2 1.4h6.2a1 1 0 011 1v6.2a1 1 0 01-1 1H2.8a1 1 0 01-1-1V4.2z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
              <path d="M8 7.4v3M6.5 8.9h3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
          </button>
          <button v-if="!showArchived" class="icon-btn" title="新建会话" @click="newSession">
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden>
              <path d="M8 3.2v9.6M3.2 8h9.6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
            </svg>
          </button>
        </div>
      </div>

      <div v-if="!showArchived" class="session-create"><button @click="newSession"><UiIcon name="plus" :size="16" />新建对话<span>开始新任务</span></button></div>
      <!-- 搜索：只在点开时占一行，平时不占位 -->
      <div v-if="sessionSearch" class="search-row">
        <input
          ref="searchInputEl"
          v-model="sessionQuery"
          class="search-input"
          type="search"
          placeholder="搜索会话标题…"
          aria-label="搜索会话"
        />
      </div>

      <div class="list-body" @scroll="menuFor = null">
        <div v-for="g in groupedSessions" :key="g.folder || '__default__'" class="ws-group">
          <!-- 工作区行：34px，一个 16px 图标位（只放展开箭头，不放文件夹图标——
               用户明确不要文件夹图标；箭头本身就是"可展开"的提示） -->
          <div class="ws-row">
            <button class="ws-toggle" :title="g.folder || '默认工作区'" @click="toggleGroup(g.folder)">
              <span class="slot">
                <svg class="ws-caret" :class="{ open: !collapsedGroups[g.folder] }" viewBox="0 0 16 16" width="14" height="14" aria-hidden>
                  <path d="M6 3.5L10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </span>
              <span class="ws-name">{{ g.folder || '默认工作区' }}</span>
            </button>
            <div class="row-actions">
              <button class="icon-btn sm" title="在此工作区新建会话" @click.stop="newSessionIn(g.folder)">
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><path d="M8 3.4v9.2M3.4 8h9.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" /></svg>
              </button>
              <button
                v-if="g.folder"
                class="icon-btn sm"
                title="删除工作区（会话退回默认工作区，不会删会话）"
                @click.stop="deleteWorkspace(g.folder)"
              >
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" /></svg>
              </button>
            </div>
          </div>

          <div v-show="!collapsedGroups[g.folder]" class="ws-items">
            <p v-if="!g.items.length" class="muted ws-empty">空工作区 · 点 + 在此新建会话</p>
            <div
              v-for="s in g.items"
              :key="s.id"
              class="sess-row"
              :class="{ active: current?.id === s.id }"
              data-pop
            >
              <button class="sess-main" @click="selectSession(s.id)">
                <!-- 行内只留标题与时间：权限档位这些小标签按用户要求去掉
                     （它们不是选会话时要看的东西，反而把标题挤窄了） -->
                <span class="sess-title">{{ s.title || '(未命名会话)' }}</span>
                <span v-if="showArchived && s.status === 'archived'" class="sess-tag">已归档</span>
                <span class="sess-time">{{ relTime(s.updated_at) }}</span>
              </button>
              <button class="si-more" title="会话操作" @click.stop="toggleMenu(s, $event)">
                <span class="dots">⋯</span>
              </button>
              <div
                v-if="menuFor?.id === s.id"
                class="si-menu"
                :style="{ left: menuFor.x + 'px', top: menuFor.y + 'px' }"
                @mouseleave="menuFor = null"
              >
                <button class="si-menu-item" @click="renameSession(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden><path d="M11.3 2.2l2.5 2.5L5.5 13H3v-2.5l8.3-8.3zM2 14.5h12" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" /></svg>
                  重命名
                </button>
                <button class="si-menu-item" @click="moveSession(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden><path d="M1.8 4.2a1 1 0 011-1h3l1.2 1.4h6.2a1 1 0 011 1v6.2a1 1 0 01-1 1H2.8a1 1 0 01-1-1V4.2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" /></svg>
                  移到工作区
                </button>
                <button class="si-menu-item" @click="forkSession(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden><circle cx="4" cy="3.5" r="1.8" fill="none" stroke="currentColor" stroke-width="1.2" /><circle cx="4" cy="12.5" r="1.8" fill="none" stroke="currentColor" stroke-width="1.2" /><circle cx="12" cy="8" r="1.8" fill="none" stroke="currentColor" stroke-width="1.2" /><path d="M4 5.3v5.4M5.6 4.2C8 5 10 6.2 10.3 7.1M5.6 11.8C8 11 10 9.8 10.3 8.9" fill="none" stroke="currentColor" stroke-width="1.2" /></svg>
                  分叉会话
                </button>
                <button v-if="s.status !== 'archived'" class="si-menu-item" @click="archiveSession(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden><rect x="2" y="2.5" width="12" height="3.5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2" /><path d="M3.2 6v6.5a1 1 0 001 1h7.6a1 1 0 001-1V6M6.3 8.5h3.4" fill="none" stroke="currentColor" stroke-width="1.2" /></svg>
                  归档会话
                </button>
                <button class="si-menu-item danger" @click="deleteSession(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden><path d="M3 4.5h10M6.5 4.5V3a1 1 0 011-1h1a1 1 0 011 1v1.5M4.5 4.5l.7 8.6a1 1 0 001 .9h3.6a1 1 0 001-.9l.7-8.6" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" /></svg>
                  删除会话
                </button>
              </div>
            </div>
          </div>
        </div>
        <p v-if="!groupedSessions.length" class="muted empty">
          {{ sessionQuery ? '没有匹配的会话' : (showArchived ? '还没有归档的会话' : '还没有会话，点右上角「+」开始') }}
        </p>
        <button class="arch-toggle" @click="showArchived = !showArchived; menuFor = null">
          {{ showArchived ? '← 返回活跃会话' : '查看已归档' }}
        </button>
      </div>
    </section>

    <!-- 对话区 -->
    <section class="conversation">
      <header class="conv-head card">
        <button class="history-toggle small ghost" :aria-expanded="sessionListOpen" aria-controls="session-list" @click="sessionListOpen = !sessionListOpen"><UiIcon name="panel-left" :size="17" />会话</button>
        <div>
          <span class="conversation-caption">{{ current ? '当前会话' : '工作空间' }}</span><strong>{{ current ? (current.title || '新对话') : '对话工作区' }}</strong>
          <!-- 验收进度（M29）：只在声明过清单的会话里出现，一行摘要 + 悬停看细节。
               清单是"做到哪儿了"的显性证据，不声明就不占位、不制造噪音。 -->
          <button
            v-if="acceptance"
            class="acc-chip"
            :class="{ done: acceptance.passed }"
            :title="`验收清单（按顺序）\n` + (acceptance.items || []).map((i, n) => `${i.ok ? '✅' : '❌'} ${n + 1}. ${i.label}`).join('\n') + (acceptance.passed ? '\n\n全部满足。' : `\n\n第一个未满足项：${acceptance.first_unsatisfied}`)"
          >
            {{ acceptanceSummary }} · ρ {{ (acceptance.rho ?? 0).toFixed(2) }}
          </button>
        </div>
        <span v-if="current" class="connection-status" :class="{ connected: streamState === 'open' }" :title="modelName || '会话连接状态'" role="status">
          <i aria-hidden="true"></i>{{ streamState === 'open' ? '已连接' : streamState === 'closed' ? '重连中' : streamState === 'connecting' ? '连接中' : '未连接' }}
        </span>
        <!-- 这里曾有一个「退出当前会话（不删除）」按钮：换会话点左边列表即可，
             想清空对话内容也有别的入口，留一个红色按钮只会让人误以为要删东西。 -->
      </header>

      <div v-if="banner" class="banner">
        <span>{{ banner }}</span>
        <button
          v-if="banner.includes('最大轮数') && !busy"
          class="banner-act"
          title="从中断处继续执行"
          @click="continueRun"
        >继续任务</button>
      </div>
      <div v-if="approvalError && !approval" class="banner approval-status" role="status">
        <span>{{ approvalError }}</span>
        <button class="banner-act" @click="restorePendingApproval">核对审批</button>
      </div>

      <div class="stream-wrap">
        <div ref="listEl" class="stream" @scroll.passive="onStreamScroll">
          <WelcomeState v-if="!renderedTimeline.length && !busy && !streaming" :has-session="!!current" :busy="starterBusy" @start="startConversation" />
          <template v-for="node in renderedTimeline" :key="node.key">
          <!-- 交付文件：渲染成文件卡片，点击打开右侧预览 -->
          <div v-if="node.kind === 'tool' && isDeliver(node)" class="row agent">
            <button class="file-card" @click="openArtifact(deliverFilename(node))">
              <span class="file-ico">
                <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden>
                  <path d="M5.5 2.6h5.4l3.6 3.6v11.2H5.5z M10.9 2.6v3.6h3.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </span>
              <span class="file-meta">
                <span class="file-name">{{ deliverFilename(node) }}</span>
                <span class="file-sub">{{ deliverSize(node) }} · 交付文件</span>
              </span>
              <span class="file-open">预览</span>
            </button>
          </div>

          <!-- 工具调用：**一行摘要 + 点击展开**（参考 deepseek harness 的形式）。
               默认最小化：没展开时只有一行（图标 · 动词 · 目标 · 增删行数 · 状态 · 时间），
               展开才看参数与输出。以前是常驻卡片，一次读文件就能把回答挤出屏幕。 -->
          <div v-else-if="node.kind === 'tool'" class="row tool">
            <!-- 这一步的思考（模型为什么发起这次调用）挂在它自己上方，默认折叠 -->
            <details v-if="node.thinking" class="think think-step" :open="!!thinkOpen[node.key]">
              <summary @click.prevent="thinkOpen[node.key] = !thinkOpen[node.key]">
                <span class="think-arrow" :class="{ open: !!thinkOpen[node.key] }">▸</span>
                <span>思考{{ node.thinking_ms >= 200 ? ` ${(node.thinking_ms / 1000).toFixed(1)} 秒` : '' }}</span>
                <span class="muted">· {{ node.thinking.length }} 字</span>
              </summary>
              <div v-if="thinkOpen[node.key]" class="think-body">{{ node.thinking }}</div>
            </details>
            <button
              class="tool-row"
              :class="'st-' + toolState(node)"
              :title="openTools[node.call.id] ? '收起详情' : '展开参数与输出'"
              @click="toggleTool(node.call.id)"
            >
              <span class="tr-caret" :class="{ open: !!openTools[node.call.id] }">▸</span>
              <svg class="tr-ico" viewBox="0 0 16 16" width="14" height="14" aria-hidden>
                <path :d="toolIconPath(node)" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span class="tr-verb">{{ toolVerb(node) }}</span>
              <span class="tr-sep">·</span>
              <span
                class="tr-target mono"
                :class="{ path: toolTargetIsPath(node) }"
                :title="toolTarget(node)"
              >{{ toolTarget(node) }}</span>
              <span v-if="toolDiff(node)" class="tr-diff" :title="toolDiffTitle(node)">
                <span class="add">+{{ toolDiff(node).added }}</span>
                <span v-if="toolDiff(node).removed !== null" class="del">-{{ toolDiff(node).removed }}</span>
              </span>
              <span class="spacer"></span>
              <span v-if="toolState(node) === 'running'" class="tr-spin" aria-hidden></span>
              <span class="tr-state" :class="'st-' + toolState(node)">{{ toolStateLabel(node) }}</span>
              <span class="muted tr-time">{{ fmtTime(node.ts) }}</span>
            </button>
            <div v-if="openTools[node.call.id]" class="tool-detail">
              <pre v-if="toolArgs(node.call)" class="tool-args mono">{{ toolArgs(node.call) }}</pre>
              <pre v-if="node.result" class="tool-output mono">{{ node.result.result }}</pre>
              <div v-else class="tool-running">执行中…</div>
            </div>
          </div>

          <!-- 普通事件 -->
          <template v-else>
            <div v-if="node.internal" class="row system">
              <details class="internal-message" aria-label="系统过程">
                <summary><span>{{ node.internal.label }}</span><span v-if="node.internal.summary !== node.internal.label"> · {{ node.internal.summary }}</span></summary>
                <pre>{{ node.internal.text }}</pre>
              </details>
            </div>

            <div v-else-if="node.ev.type === 'user_message'" class="row user">
              <div class="bubble">
                <div v-if="node.ev.payload.attachments?.length" class="bubble-atts">
                  <span v-for="a in node.ev.payload.attachments" :key="a.path" class="attach-chip static">
                    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden><path d="M13 7.2l-4.9 4.9a3 3 0 01-4.2-4.2l5-5a2 2 0 012.8 2.8l-5 5a1 1 0 01-1.4-1.4L9.7 4.9" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" /></svg>
                    {{ a.name }}
                  </span>
                </div>
                {{ node.ev.payload.text }}
                <!-- 回退入口（M29）：挂在这条消息上，回退 = **这条消息之前**的状态
                     （对话 + 该步及其之后的文件改动一起退回去）。 -->
                <button
                  v-if="checkpointAt(node.ev.seq)"
                  class="rewind-btn"
                  :class="{ busy: rewindBusy }"
                  :title="`回退到这条消息之前（撤回该步及其之后的改动与对话记录）· 检查点 ${checkpointAt(node.ev.seq).id}`"
                  :disabled="rewindBusy || busy"
                  @click="rewindTo(checkpointAt(node.ev.seq))"
                >
                  <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden>
                    <path d="M6.4 3.2L3 6.6l3.4 3.4M3 6.6h6.1a3.7 3.7 0 010 7.4H6.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                  回退到这里
                </button>
              </div>
            </div>

            <div
              v-else-if="(node.ev.type === 'assistant_message' || node.ev.type === 'done') && (node.ev.payload.text || node.incomplete)"
              class="row agent"
            >
              <!-- 回答不带头像、不带外框：正文直接排在页面上（DSH 的排版） -->
              <div class="answer">
                <div v-if="node.ev.payload.rewind" class="rewind-tag">已回退</div>
                <details v-if="node.ev.payload.thinking" class="think" :open="!!thinkOpen[node.key]">
                  <summary @click.prevent="thinkOpen[node.key] = !thinkOpen[node.key]">
                    <span class="think-arrow" :class="{ open: !!thinkOpen[node.key] }">▸</span>
                    <span>思考{{ node.ev.payload.thinking_ms >= 200 ? ` ${(node.ev.payload.thinking_ms / 1000).toFixed(1)} 秒` : '' }}</span>
                    <span class="muted">· {{ node.ev.payload.thinking.length }} 字</span>
                  </summary>
                  <div v-if="thinkOpen[node.key]" class="think-body">{{ node.ev.payload.thinking }}</div>
                </details>
                <div v-if="node.ev.payload.text" class="md-body" v-html="renderMarkdown(node.ev.payload.text)"></div>
                <p v-if="node.incomplete" class="completion-warning" role="status" :title="node.ev.payload.stop_reason || ''">{{ node.incomplete.label }}</p>
                <div class="ts muted">{{ fmtTime(node.ev.ts) }}</div>
              </div>
            </div>

            <div v-else-if="node.ev.type === 'approval_request'" class="row approve">
              <div class="approve-card">
                <div class="approve-title">
                  <span class="approve-ico">⚠</span> 需要人工审批：<code>{{ node.ev.payload.tool }}</code>
                </div>
                <div class="muted approve-reason">{{ node.ev.payload.reason }}</div>
                <pre
                  v-if="formatArgs(node.ev.payload.arguments)"
                  class="approve-args mono"
                >{{ formatArgs(node.ev.payload.arguments) }}</pre>
              </div>
            </div>

            <div v-else-if="node.ev.type === 'error'" class="row" :class="node.ev.payload?.quiet ? 'system' : 'err'">
              <!-- 检查点事件（quiet）→ 一行淡字；真故障仍是红色气泡 -->
              <div v-if="node.ev.payload?.quiet" class="auto-note" :title="node.ev.payload.message">
                {{ node.ev.payload.message }}
              </div>
              <!-- 熔断（M29）：不是故障，是**主动停下等人**——用中性的"已停下"卡片，
                   并把完整原因（卡在哪个调用、试了几次、建议怎么办）留在里面。 -->
              <div v-else-if="node.ev.payload?.code === 'repeated_failure'" class="stop-card">
                <div class="stop-head">
                  <span class="stop-ico">■</span>
                  已停下：{{ node.ev.payload.streak }} 次同样的失败
                </div>
                <div class="stop-body">{{ node.ev.payload.message }}</div>
              </div>
              <div v-else class="bubble">❌ {{ node.ev.payload.message }}</div>
            </div>
          </template>
        </template>

        <!-- 思考过程：推理模型（qwen3 等）的 reasoning 流。
             流式期间独立一行（还没有"属于哪条消息"），收口时会挂到那条 assistant 消息上；
             样式与回答刻意不同：小字 + 淡色 + 默认折叠。 -->
        <div v-if="thinking" class="row agent">
          <div class="answer">
            <button class="think-toggle" @click="thinkOpen.live = !thinkOpen.live">
              <span class="think-arrow" :class="{ open: !!thinkOpen.live }">▸</span>
              {{ busy ? '思考中…' : '思考过程' }}（{{ thinking.length }} 字）
            </button>
            <div v-if="thinkOpen.live" class="think-body">{{ thinking }}</div>
          </div>
        </div>

        <!-- 打字机：正在生成的增量文本 -->
        <div v-if="streaming" class="row agent">
          <div class="answer">
            <div class="md-body streaming" v-html="renderMarkdown(streaming)"></div>
            <span class="caret"></span>
          </div>
        </div>
        <div v-else-if="busy && !thinking && !approval && !question" class="row agent">
          <div class="answer muted" role="status">{{ activityLabel }}…</div>
        </div>
        </div>

        <!-- 用户往上翻时不抢滚动条：给一个回到底部的入口（DSH 同款） -->
        <button v-if="current && !stickBottom" class="to-bottom" title="回到最新" @click="scrollDown(true)">
          ↓ 回到最新
        </button>
      </div>

      <footer class="composer-wrap">
        <!-- 提问卡（M31）：模型挂起等答案时**替代**输入框。
             一屏一题 + 翻页 + 推荐徽标 + 自定义答案 + 跳过；答案按 id 组装后回给模型。
             X（放弃）是刻意的出口：万一这组提问在后端已经失效，用户还能拿回输入框。 -->
        <section v-if="question && qCurrent" class="ask-card card">
          <button class="ask-head" :title="qMinimized ? '展开问题卡片' : '收起问题卡片'" @click="qMinimized = !qMinimized">
            <span class="ask-eyebrow">{{ qCurrent.header || '需要你确认' }}</span>
            <span class="ask-head-actions">
              <span class="ask-chev" :class="{ open: !qMinimized }">▾</span>
            </span>
          </button>
          <button class="ask-close" title="放弃整组提问" @click.stop="dismissQuestions">×</button>

          <div v-if="!qMinimized" class="ask-body">
            <h3 class="ask-question">{{ qCurrent.question }}</h3>
            <p v-if="qCurrent.multiSelect" class="ask-hint">可多选</p>

            <div v-if="qCurrent.options.length" class="ask-options">
              <button
                v-for="(opt, i) in qCurrent.options"
                :key="opt.label"
                class="ask-opt"
                :class="{
                  on: qDraft(qCurrent.id).selected.includes(opt.label),
                  multi: qCurrent.multiSelect,
                }"
                @click="pickOption(opt)"
              >
                <span class="ask-num">{{ optionPrefix(i) }}</span>
                <span class="ask-opt-copy">
                  <span class="ask-opt-label">
                    {{ parseRecommendedLabel(opt.label).label }}
                    <span v-if="parseRecommendedLabel(opt.label).recommended" class="ask-rec">推荐</span>
                  </span>
                  <span v-if="opt.description" class="ask-opt-desc">{{ opt.description }}</span>
                </span>
              </button>
            </div>

            <label class="ask-custom">
              <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden>
                <path d="M11.6 2.6l1.8 1.8-7.2 7.2-2.4.6.6-2.4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
              </svg>
              <textarea
                :value="qDraft(qCurrent.id).custom"
                rows="1"
                placeholder="输入你的答案"
                @input="(e) => setDraft(qCurrent.id, { custom: e.target.value, skipped: false })"
              ></textarea>
            </label>

            <div v-if="qError" class="ask-error">{{ qError }}</div>
          </div>

          <div class="ask-foot">
            <span class="ask-pager">
              <button class="ask-page" :disabled="qIndex <= 0" title="上一题" @click="goQuestion(-1)">‹</button>
              <span class="ask-page-num">{{ progressText(qIndex, question.questions.length) }}</span>
              <button
                class="ask-page"
                :disabled="qIndex >= question.questions.length - 1"
                title="下一题"
                @click="goQuestion(1)"
              >›</button>
            </span>
            <div class="ask-actions">
              <button class="ask-skip" :disabled="qBusy" @click="skipQuestion">跳过本题</button>
              <button class="ask-next" :disabled="qBusy" @click="advanceQuestion">
                {{ qBusy ? '提交中…' : (qIndex === question.questions.length - 1 ? '提交' : '下一题') }}
              </button>
            </div>
          </div>
        </section>

        <!-- 任务清单（M30）：模型自己写的整份快照，回答"这一轮做到第几步"。
             内容与顺序都照模型给的那份渲染（不重排），只把零项从头部摘掉。 -->
        <section v-if="!question && todoHasItems" class="task-panel card">
          <button class="task-head" :title="taskOpen ? '收起任务清单' : '展开任务清单'" @click="taskOpen = !taskOpen">
            <svg class="task-ico" viewBox="0 0 16 16" width="14" height="14" aria-hidden>
              <path d="M2.5 4.2h2.2M2.5 8h2.2M2.5 11.8h2.2M7 4.2h6.5M7 8h6.5M7 11.8h6.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
            </svg>
            <span class="task-title">任务</span>
            <span class="task-counts">{{ todoHeadline(todoStat) }}</span>
            <span class="task-caret" :class="{ open: taskOpen }">▾</span>
          </button>
          <div v-if="taskOpen" class="task-body">
            <div v-for="(item, i) in todos" :key="i" class="task-row" :class="'ts-' + item.status">
              <span class="task-mark">{{ TODO_ICON[item.status] }}</span>
              <span class="task-text">{{ item.content }}</span>
            </div>
          </div>
        </section>

        <div v-if="!question" class="composer card">
          <div v-if="pendingFiles.length" class="attach-row">
            <span v-for="(f, i) in pendingFiles" :key="f.path" class="attach-chip">
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden><path d="M13 7.2l-4.9 4.9a3 3 0 01-4.2-4.2l5-5a2 2 0 012.8 2.8l-5 5a1 1 0 01-1.4-1.4L9.7 4.9" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" /></svg>
              <span class="attach-name" :title="f.path">{{ f.name }}</span>
              <span class="attach-size">{{ fmtSize(f.size) }}</span>
              <button class="attach-x" title="移除" @click="pendingFiles.splice(i, 1)">×</button>
            </span>
          </div>
          <textarea
            ref="taEl"
            v-model="input"
            rows="1"
            class="ta"
            :placeholder="current ? '描述你想完成的任务，/ 调用指令，@ 引用文件' : '点击「新建对话」，开始输入任务'"
            aria-label="消息内容"
            :disabled="!current"
            :title="sendHint"
            @keydown="onComposerKeydown"
            @compositionstart="composing = true"
            @compositionend="composing = false"
            @input="autoGrow"
            @paste="onPasteFiles"
          ></textarea>

          <div class="tools-row">
            <div class="tools-left">
              <!-- [+] 引用菜单：添加文件 / 会话内文件 / 技能 / 插件 / 连接器 -->
              <div class="add-select" data-pop>
                <button
                  class="tool-btn"
                  :class="{ open: addMenuOpen }"
                  title="添加文件或引用对话中的文件"
                  :disabled="!current || busy"
                  @click="toggleAddMenu($event)"
                >
                  <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
                    <path d="M8 2.5v11M2.5 8h11" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
                  </svg>
                </button>
                <div v-if="addMenuOpen" class="add-menu" :style="popAt.add" @mouseleave="addMenuOpen = false; addExpand = ''">
                  <button class="add-item" @click="fileEl?.click()">
                    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><path d="M13.2 7.3l-4.9 4.9a3.4 3.4 0 01-4.8-4.8l5.6-5.6a2.3 2.3 0 013.2 3.2L6.7 10.6a1.1 1.1 0 01-1.6-1.6l4.6-4.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
                    <span>添加文件</span>
                    <span class="add-note">任意格式 · 文本插入，其他上传工作区（也可直接粘贴）</span>
                  </button>
                  <input ref="fileEl" type="file" multiple hidden @change="onFilePicked" />

                  <button class="add-item" @click="toggleExpand('files')">
                    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><circle cx="8" cy="8" r="2.2" fill="none" stroke="currentColor" stroke-width="1.3" /><path d="M2.5 8a5.5 5.5 0 0111 0" fill="none" stroke="currentColor" stroke-width="1.3" /></svg>
                    <span>引用对话中的文件</span>
                    <span class="add-count" v-if="convoFiles.length">{{ convoFiles.length }}</span>
                  </button>
                  <div v-if="addExpand === 'files'" class="add-sublist">
                    <p v-if="!convoFiles.length" class="add-empty">本会话还没有添加过文件</p>
                    <button v-for="f in convoFiles" :key="f.name" class="add-item sub" @click="insertAtCursor('@' + f.name)">
                      <span>@ {{ f.name }}</span><span class="add-note">{{ f.chars }} 字符</span>
                    </button>
                  </div>

                  <button class="add-item" v-if="skills.length" @click="toggleExpand('skills')">
                    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><path d="M8 1.8l1.7 3.6 3.9.5-2.9 2.7.8 3.9L8 10.6l-3.5 1.9.8-3.9L2.4 5.9l3.9-.5L8 1.8z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" /></svg>
                    <span>技能</span><span class="add-count" v-if="skills.length">{{ skills.length }}</span>
                  </button>
                  <div v-if="addExpand === 'skills'" class="add-sublist">
                    <p v-if="!skills.length" class="add-empty">暂无可用技能（可在「工具」页上传）</p>
                    <button v-for="s in skills" :key="s.name" class="add-item sub" :title="s.description" @click="insertAtCursor('@' + s.name + ' ')">
                      <span>@ {{ s.name }}</span><span class="add-note">{{ truncate(s.description, 24) }}</span>
                    </button>
                  </div>

                  <button class="add-item" v-if="pluginsAvail.length" @click="toggleExpand('plugins')">
                    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><rect x="2.5" y="5.5" width="11" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3" /><path d="M5.5 5.5v-2a1.5 1.5 0 013 0v2M10.5 5.5v-2a1.5 1.5 0 00-3 0" fill="none" stroke="currentColor" stroke-width="1.3" /></svg>
                    <span>插件</span><span class="add-count" v-if="pluginsAvail.length">{{ pluginsAvail.length }}</span>
                  </button>
                  <div v-if="addExpand === 'plugins'" class="add-sublist">
                    <p v-if="!pluginsAvail.length" class="add-empty">暂无插件包（可在「工具」页上传）</p>
                    <button v-for="p in pluginsAvail" :key="p.name" class="add-item sub" :title="p.description" @click="insertAtCursor('@' + p.name + ' ')">
                      <span>@ {{ p.name }}</span>
                      <span class="add-note">{{ p.enabled ? '已启用' : '未启用' }} · {{ truncate(p.description, 18) }}</span>
                    </button>
                  </div>

                  <button class="add-item" v-if="mcpServers.length" @click="toggleExpand('mcp')">
                    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><path d="M5.5 3.5A3.5 3.5 0 005.5 10.5M10.5 3.5v7M8 13.5v1" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /><rect x="4" y="10.5" width="8" height="3" rx="1" fill="none" stroke="currentColor" stroke-width="1.3" /></svg>
                    <span>连接器（MCP）</span><span class="add-count" v-if="mcpServers.length">{{ mcpServers.length }}</span>
                  </button>
                  <div v-if="addExpand === 'mcp'" class="add-sublist">
                    <p v-if="!mcpServers.length" class="add-empty">暂无 MCP 连接器（可在「工具」页配置）</p>
                    <button v-for="m in mcpServers" :key="m.name" class="add-item sub" @click="insertAtCursor('@' + m.name + ' ')">
                      <span>@ {{ m.name }}</span><span class="add-note">{{ m.connected ? '已连接' : '未连接' }}</span>
                    </button>
                  </div>
                </div>
              </div>

              <!-- 联网（M19）：会话级开关，位于输入区工具栏。不可用时点它会给出原因 -->
              <div v-if="current && webAvailable" class="web-select">
                <button
                  class="tool-btn web-btn"
                  :class="{ on: webOn, off: !webAvailable }"
                  :title="webHint"
                  :aria-pressed="webOn"
                  @click="toggleWeb"
                >
                  <UiIcon name="globe" :size="14" />
                  联网{{ webOn ? '已开' : '关闭' }}
                </button>
              </div>

              <div class="perm-select" data-pop>
                <button
                  class="perm-btn"
                  :class="curPerm"
                  :title="curPermInfo.desc"
                  aria-label="会话权限" aria-haspopup="menu" :aria-expanded="permMenuOpen" aria-controls="chat-permission-menu"
                  @click="togglePermMenu($event)"
                  @keydown.down.prevent="openChoiceMenu('perm', $event)" @keydown.up.prevent="openChoiceMenu('perm', $event)"
                >
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden>
                    <path :d="PERM_ICONS[curPerm] || PERM_ICONS['workspace-write']" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round" />
                  </svg>
                  {{ curPermInfo.label }}
                  <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden>
                    <path d="M3.5 6l4.5 4.5L12.5 6" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" />
                  </svg>
                </button>
                <div v-if="permMenuOpen" id="chat-permission-menu" ref="permMenuEl" class="perm-menu composer-menu" :style="popAt.perm" role="menu" aria-label="会话权限" @keydown="onChoiceKey($event, 'perm')">
                  <button
                    v-for="m in permModes"
                    :key="m.id"
                    class="perm-item choice-item"
                    :class="{ active: curPerm === m.id }"
                    role="menuitemradio" :aria-checked="curPerm === m.id" :tabindex="curPerm === m.id ? 0 : -1"
                    @click="pickPerm(m.id)"
                  >
                    <span class="choice-icon" :class="m.id"><svg viewBox="0 0 16 16" width="17" height="17" aria-hidden>
                      <path :d="PERM_ICONS[m.id] || PERM_ICONS['workspace-write']" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round" />
                    </svg></span>
                    <span class="perm-text">
                      <b>{{ m.label }}</b>
                    </span>
                    <span class="choice-check" :class="{ checked: curPerm === m.id }" aria-hidden="true"><UiIcon v-if="curPerm === m.id" name="check" :size="12" /></span>
                  </button>
                </div>
              </div>
            </div>

            <div class="tools-right">
              <div class="model-select" data-pop>
                <button
                  class="model-btn"
                  :title="isAdmin ? '切换模型（选配置档案）' : '当前模型'"
                  @click="toggleModelMenu($event)"
                >
                  <span class="model-name">{{ modelName || activeModel || '模型未配置' }}</span>
                  <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden><path d="M3.5 6l4.5 4.5L12.5 6" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" /></svg>
                </button>
                <div v-if="profMenuOpen" class="model-menu" :style="popAt.model" @mouseleave="profMenuOpen = false">
                  <!-- M36：菜单里只留**配置档案**——具体模型就是在这里选的。
                       档案 = provider + 密钥 + 默认模型 三件套，切档案即切模型；
                       原来那套"默认模型 / 全部模型 / 刷新列表"整块已删（用户要求）：
                       同一件事有两个入口（选模型 vs 选档案）只会让人犹豫用哪个。 -->
                  <p class="mm-title">切换配置档案<span class="muted">（选一个即可换模型）</span></p>
                  <button
                    v-for="pr in profiles"
                    :key="pr.id"
                    class="mm-item"
                    :class="{ active: pr.active }"
                    @click="activateModel(pr)"
                  >
                    <span
                      class="p-dot"
                      :class="(pr.api_key_set || pr.local) ? 'ok' : 'warn'"
                      :title="pr.local ? '本地 / 内网后端，无需密钥' : (pr.api_key_set ? '已配置密钥' : '未配置密钥')"
                    ></span>
                    <span class="mm-name">{{ pr.name }}</span>
                    <span class="mm-model">{{ pr.model_default || pr.provider }}</span>
                    <span v-if="pr.active" class="mm-badge">✓</span>
                  </button>
                  <p v-if="!profiles.length" class="mm-note">还没有配置：点击顶部「模型连接」添加</p>
                  <p class="mm-note">切换会写入配置并热生效（下一条消息起用新模型）</p>
                </div>
              </div>
              <span v-if="busy" class="spinner" title="执行中"></span>
              <button
                v-if="busy"
                class="send-btn stop"
                title="停止当前任务"
                @click="stopRun"
              >
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor" /></svg>
              </button>
              <button
                v-else
                class="send-btn"
                :disabled="!current || (!input.trim() && !pendingFiles.length)"
                :title="sendHint"
                @click="send"
              >
                <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden>
                  <path d="M8 13.5V3M3.6 7.2L8 2.8l4.4 4.4" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </button>
            </div>
          </div>
        </div>

        <!-- 统计行：两个可点击 pill，弹窗数据全部来自真实事件 -->
        <div class="stats-wrap" v-if="current" data-pop>
          <div v-if="composerHint" class="composer-hint">{{ composerHint }}</div>
          <div class="stats-line">
            <button class="stat-pill" @click="toggleStatPop('session', $event)">
              <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden><circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.3" /><path d="M8 4.5V8l2.3 1.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
              {{ stats.prompts }} 次对话 · {{ stats.tools }} 步
              <template v-if="sessionStats.avgTps"> · ~{{ sessionStats.avgTps.toFixed(0) }} tok/s</template>
            </button>
            <button class="stat-pill" @click="toggleStatPop('tokens', $event)">
              <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden><ellipse cx="8" cy="4.2" rx="5.5" ry="2.2" fill="none" stroke="currentColor" stroke-width="1.2" /><path d="M2.5 4.2v7.6c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2V4.2M2.5 8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2" fill="none" stroke="currentColor" stroke-width="1.2" /></svg>
              {{ fmtTokens(stats.input + stats.output) }} tok · 输入 {{ fmtTokens(stats.input) }} / 输出 {{ fmtTokens(stats.output) }}
              <template v-if="cachePct"> · 缓存 {{ cachePct }}%</template>
            </button>
            <button class="stat-pill" @click="toggleCtxPop($event)">
              <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden><path d="M2.5 8a5.5 5.5 0 111.6 3.9" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /><path d="M2.5 12.5V9.5h3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" /></svg>
              对话历史
            </button>
          </div>

          <!-- 会话统计弹窗 -->
          <div v-if="statPop === 'session'" class="stat-pop" :style="popAt.stat">
            <div class="pop-title">会话统计</div>
            <div class="pop-row"><span>对话次数</span><b>{{ stats.prompts }}</b></div>
            <div class="pop-row"><span>轮数 / 工具步</span><b>{{ stats.turns }} 轮 · {{ stats.tools }} 步</b></div>
            <div class="pop-row"><span>模型用时（合计）</span><b>{{ fmtDur(sessionStats.totalDur) }}</b></div>
            <div class="pop-row"><span>单轮平均</span><b>{{ fmtDur(sessionStats.avgDur) }}</b></div>
            <div class="pop-row"><span>首 token 平均（TTFT）</span><b>{{ sessionStats.ttftAvg ? sessionStats.ttftAvg.toFixed(1) + ' 秒' : '—' }}</b></div>
            <div class="pop-row"><span>输出速度（全程均值）</span><b>{{ sessionStats.avgTps ? '~' + sessionStats.avgTps.toFixed(0) + ' tok/s' : '—' }}</b></div>
            <p class="pop-note">由事件时间戳推导；TTFT 仅统计本浏览器在线期间收到的流式轮次</p>
          </div>

          <!-- Token 用量弹窗 -->
          <div v-if="statPop === 'tokens'" class="stat-pop" :style="popAt.stat">
            <div class="pop-title">Token 用量（本会话）</div>
            <div class="pop-row"><span>输入</span><b>{{ stats.input.toLocaleString() }} tok</b></div>
            <div class="pop-row"><span>其中：缓存命中</span><b>{{ stats.cached.toLocaleString() }} tok<template v-if="cachePct"> · {{ cachePct }}%</template></b></div>
            <div class="pop-row"><span>其中：未缓存输入</span><b>{{ (stats.input - stats.cached).toLocaleString() }} tok</b></div>
            <div class="pop-row"><span>输出</span><b>{{ stats.output.toLocaleString() }} tok</b></div>
            <div class="pop-row"><span>合计</span><b>{{ (stats.input + stats.output).toLocaleString() }} tok</b></div>
            <p class="pop-note">按会话事件累计用量，包含分段执行；缓存命中由模型服务上报。</p>
          </div>

          <!-- 上下文占用弹窗 -->
          <div v-if="ctxPop" class="stat-pop ctx-pop" :style="popAt.ctx" @mouseleave="ctxPop = false">
            <template v-if="ctxInfo && !ctxInfo.error">
              <div class="pop-title">对话历史</div>
              <div class="pop-row"><span>历史消息</span><b>{{ ctxInfo.messages }} 条</b></div>
              <div class="pop-row"><span>事件</span><b>{{ ctxInfo.events }} 条</b></div>
              <p class="pop-note">{{ ctxInfo.model }} · {{ ctxInfo.note }}</p>
            </template>
            <template v-else-if="ctxLoading"><p class="pop-note">读取中…</p></template>
            <template v-else><p class="pop-note">{{ ctxInfo?.error || '无法获取' }}</p></template>
          </div>
        </div>
      </footer>
    </section>

    <!-- 文件预览面板（第四栏，可关闭） -->
    <aside v-if="preview" class="preview-panel card">
      <div class="preview-head">
        <svg class="ph-icon" viewBox="0 0 20 20" width="15" height="15" aria-hidden>
          <path d="M5.5 2.6h5.4l3.6 3.6v11.2H5.5z M10.9 2.6v3.6h3.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <strong>文件预览</strong>
        <span class="spacer"></span>
        <button class="icon-btn" title="复制内容" @click="copyPreview">
          <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden>
            <path d="M7.4 7.4h8.2v9H7.4z M4.4 12.6V3.6h8.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
          </svg>
        </button>
        <button class="icon-btn" title="下载" @click="downloadPreview">
          <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden>
            <path d="M10 3.4v9 M6.4 9l3.6 3.6L13.6 9 M4 15.6h12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <button class="icon-btn" title="关闭" @click="closePreview">
          <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden>
            <path d="M5.4 5.4l9.2 9.2 M14.6 5.4l-9.2 9.2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
        </button>
      </div>
      <div class="preview-file">
        {{ preview.filename }}
        <span class="preview-kind">{{ KIND_LABEL[preview.kind] || '文件' }}</span>
      </div>
      <div class="preview-body">
        <div v-if="previewBusy" class="preview-loading">读取中…</div>

        <!-- 图片：走 object URL -->
        <img
          v-else-if="preview.kind === 'image'"
          :src="preview.url"
          :alt="preview.filename"
          class="preview-image"
        />

        <!-- HTML：sandbox 禁脚本，防止交付物里的 JS 执行 -->
        <iframe
          v-else-if="preview.kind === 'html'"
          class="preview-frame"
          sandbox="allow-same-origin"
          :srcdoc="preview.html"
        ></iframe>

        <!-- Markdown 渲染 -->
        <div v-else-if="preview.kind === 'markdown'" class="preview-md" v-html="preview.html"></div>

        <!-- 纯文本 -->
        <pre v-else class="preview-content mono">{{ preview.content }}</pre>
      </div>
    </aside>

    <!-- 审批弹窗 -->
    <div v-if="approval" class="modal-mask">
      <div class="approval-dialog">
        <div v-if="approvalError" class="banner approval-status" role="status">
          <span>{{ approvalError }}</span>
          <button class="banner-act" :disabled="!!approvalSubmitting" @click="restorePendingApproval">核对审批</button>
        </div>
        <ApprovalRequest :approval="approval" :submitting="!!approvalSubmitting" :readonly="curPerm === 'read-only'" @decide="decide" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.connection-status { display: inline-flex; flex: none; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-xs); color: var(--muted); }
.connection-status i { width: 6px; height: 6px; border-radius: 50%; background: var(--warn); }
.connection-status.connected i { background: var(--ok); }
.conv-head > div { flex: 1; }
.chat { display: grid; grid-template-columns: 252px minmax(0, 1fr); gap: 0; padding: 0; height: 100%; min-height: 0; overflow: hidden; position: relative; background: var(--panel); }
/* 打开文件预览时变四栏：会话列表 / 对话 / 预览面板 */
.chat.with-preview { grid-template-columns: 232px minmax(280px, 1fr) 360px; }

/* 助手头像（CH 方块） */
.msg-avatar {
  flex: none; width: 28px; height: 28px; border-radius: var(--r-sm);
  background: var(--accent); color: var(--on-accent);
  display: grid; place-items: center;
  font-size: var(--fs-xs); font-weight: var(--fw-bold); letter-spacing: .5px;
  margin-right: var(--sp-2); margin-top: var(--sp-hair);
}

/* 交付文件卡片：点击打开右侧预览 */
.file-card {
  display: flex; align-items: center; gap: var(--sp-2-5);
  width: 100%; max-width: 420px;
  padding: var(--sp-2-5) var(--sp-3); border-radius: var(--r-md);
  border: 1px solid var(--border); background: var(--panel);
  cursor: pointer; text-align: left; font: inherit; color: var(--text);
  transition: border-color .15s, background .15s;
}
.file-card:hover { border-color: var(--accent-border); background: #f8fbff; }
.file-ico {
  flex: none; width: 34px; height: 34px; border-radius: var(--r-sm);
  background: var(--accent-soft); color: var(--accent);
  display: grid; place-items: center;
}
.file-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-0-5); }
.file-name {
  font-size: var(--fs-sm); font-weight: var(--fw-semibold);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.file-sub { font-size: var(--fs-xs); color: var(--muted); }
.file-open { flex: none; font-size: var(--fs-xs); color: var(--accent); }

/* 文件预览面板（第四栏） */
.preview-panel { display: flex; flex-direction: column; overflow: hidden; min-width: 0; min-height: 0; border-radius: 0; border: 0; border-left: 1px solid var(--border); box-shadow: none; }
.preview-head {
  display: flex; align-items: center; gap: var(--sp-1-5);
  padding: var(--sp-3) var(--sp-3-5); border-bottom: 1px solid var(--border);
}
.preview-head .ph-icon { color: var(--accent); flex: none; }
.preview-head strong { font-size: var(--fs-md); }
.icon-btn {
  flex: none; border: none; background: transparent; padding: var(--sp-1-5);
  border-radius: var(--r-sm); color: var(--muted); cursor: pointer;
  display: grid; place-items: center;
}
.icon-btn:hover { background: var(--hover); color: var(--text); }
.preview-file {
  padding: var(--sp-3) var(--sp-3-5) var(--sp-2); font-size: var(--fs-lg); font-weight: var(--fw-semibold);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.preview-kind {
  margin-left: var(--sp-1-5); font-size: var(--fs-xs); font-weight: var(--fw-medium);
  padding: var(--sp-hair) 7px; border-radius: 999px;
  background: var(--hover); color: var(--muted); vertical-align: middle;
}
.preview-body { flex: 1; overflow: auto; padding: 0 var(--sp-3-5) var(--sp-3-5); }
.preview-loading { padding: var(--sp-6) 0; text-align: center; color: var(--muted); font-size: var(--fs-sm); }
.preview-content {
  margin: 0; padding: var(--sp-3); background: var(--panel-soft);
  border: 1px solid var(--border); border-radius: var(--r-sm);
  font-size: var(--fs-sm); line-height: var(--lh-relaxed); white-space: pre-wrap; word-break: break-word;
}
.preview-image {
  display: block; max-width: 100%; border-radius: var(--r-sm);
  border: 1px solid var(--border); background: var(--panel);
}
.preview-frame {
  width: 100%; height: 100%; min-height: 460px;
  border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--panel);
}
/* Markdown 渲染排版 */
.preview-md { font-size: var(--fs-sm); line-height: var(--lh-relaxed); color: var(--text); word-break: break-word; }
.preview-md :deep(h1){ font-size: var(--fs-xl); margin: 0 0 var(--sp-2-5); }
.preview-md :deep(h2){ font-size: var(--fs-lg); margin: var(--sp-4) 0 var(--sp-2); }
.preview-md :deep(h3){ font-size: var(--fs-lg); margin: var(--sp-3-5) 0 var(--sp-1-5); }
.preview-md :deep(h4), .preview-md :deep(h5), .preview-md :deep(h6){ font-size: var(--fs-sm); margin: var(--sp-3) 0 var(--sp-1-5); }
.preview-md :deep(p){ margin: var(--sp-2) 0; }
.preview-md :deep(ul){ margin: var(--sp-2) 0; padding-left: var(--sp-5); }
.preview-md :deep(li){ margin: 3px 0; }
.preview-md :deep(blockquote){
  margin: var(--sp-2) 0; padding: var(--sp-1-5) var(--sp-3);
  border-left: 3px solid var(--border); color: var(--muted);
}
.preview-md :deep(code){ background: var(--hover); padding: var(--sp-hair) 5px; border-radius: var(--r-sm); font-size: var(--fs-sm); }
.preview-md :deep(.md-code){
  display: block; margin: var(--sp-2) 0; padding: var(--sp-2-5); background: var(--panel-soft);
  border: 1px solid var(--border); border-radius: var(--r-sm); overflow: auto; white-space: pre-wrap;
}
.preview-md :deep(a){ color: var(--accent); }

.session-list { display: flex; flex-direction: column; overflow: hidden; min-height: 0; border: 0; border-right: 1px solid var(--border); border-radius: 0; background: #fafbfe; box-shadow: none; }
/* ---- 会话列表（M24：对齐 deepseek harness 的行式列表）----
   参考 DSH 的 rows.module.css 实测值：
     · 行高：工作区 34px / 会话 32px
     · 行内边距 0 8px、gap 6px、圆角 8px
     · 悬停与**选中**用同一个淡灰底（interactive-bg-hover），不用蓝底——列表要安静
     · 一个 16px 的图标位：hover 时文件夹图标换成箭头（展开态）
     · 标题 14px/20px 单行省略；时间 12px/20px 右对齐、三级灰
   会话不再有第二行 "default · active"（那是噪音），权限状态改成行内小标签。 */
.list-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 20px 16px 14px; }
.session-heading { display: flex; flex-direction: column; gap: 2px; }
.section-caption, .conversation-caption { display: block; font-size: var(--fs-2xs); color: var(--muted); font-weight: 400; }
.session-create { padding: 0 12px 14px; border-bottom: 1px solid var(--border); }
.session-create button { justify-content: flex-start; width: 100%; min-height: 36px; gap: 8px; border-color: var(--accent-border); background: var(--panel); color: var(--accent); font-weight: 500; }
.session-create button span { margin-left: auto; font-size: 11px; color: var(--muted); font-weight: 400; }
.list-head strong { font-size: var(--fs-sm); font-weight: var(--fw-semibold); }
.head-actions { display: flex; align-items: center; gap: var(--sp-0-5); }
/* 列表头三个图标按钮**同尺寸同风格**：28×28 幽灵按钮（新建会话不再是蓝底特例） */
.head-actions .icon-btn {
  width: 28px; min-height: 28px; padding: 0;
  display: grid; place-items: center; border: none; background: transparent;
  color: var(--text-2); border-radius: var(--r-sm);
}
.head-actions .icon-btn:hover { background: var(--hover); color: var(--text); }
.search-row { padding: var(--sp-2) var(--sp-3) 0; }
.search-input { width: 100%; height: 30px; min-height: 30px; font-size: var(--fs-sm); }
.list-body { overflow-y: auto; padding: 12px 10px; display: flex; flex-direction: column; gap: var(--sp-hair); }

/* 工作区行 */
.ws-row { display: flex; align-items: center; min-height: 34px; border-radius: var(--r-sm); padding: 0 var(--sp-1-5) 0 var(--sp-1); }
.ws-row:hover { background: var(--hover); }
.ws-toggle {
  flex: 1; min-width: 0; display: flex; align-items: center; gap: var(--sp-1-5);
  justify-content: flex-start;                 /* 全局 button 居中规则要覆盖掉 */
  min-height: 32px; padding: 0; border: none; background: transparent; color: var(--text);
  border-radius: var(--r-sm); text-align: left;
}
.slot { flex: none; width: 16px; height: 18px; display: inline-flex; align-items: center; justify-content: center; color: var(--faint); }
.ws-caret { transition: transform .15s ease; }
.ws-caret.open { transform: rotate(90deg); }
.row-actions { flex: none; display: flex; align-items: center; gap: var(--sp-0-5); opacity: 0; transition: opacity .12s ease; }
.ws-row:hover .row-actions { opacity: 1; }
.icon-btn.sm { width: 24px; min-height: 24px; padding: 0; border: none; background: transparent; color: var(--faint); border-radius: var(--r-sm); display: grid; place-items: center; }
.icon-btn.sm:hover { background: #e6eaf0; color: var(--text); }
/* 侧栏字体统一：同字族、同字号、同行高，只用字重分主次——工作区 700，会话 500 */
.ws-name {
  font-family: inherit; font-size: var(--fs-sm); font-weight: var(--fw-bold);   /* 工作区标题加粗（用户要求） */
  line-height: var(--h-line); letter-spacing: 0; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 会话行：单行 = 标题 + 时间，**左对齐**（不再比工作区标题更往里缩） */
.ws-items { display: flex; flex-direction: column; gap: var(--sp-hair); }
.sess-row { position: relative; display: flex; align-items: center; min-height: 32px; border-radius: var(--r-sm); }
.sess-row:hover, .sess-row.active { background: var(--hover); }
.sess-main {
  flex: 1; min-width: 0; display: flex; align-items: center; gap: var(--sp-1-5);
  justify-content: flex-start;
  min-height: 32px; padding: 0 var(--sp-1-5) 0 26px;      /* 26px = 工作区名左边距（4+16+6），两列左对齐 */
  border: none; background: transparent;
  color: inherit; border-radius: var(--r-sm); cursor: pointer; text-align: left;
}
.sess-title {
  font-family: inherit; font-size: var(--fs-sm); font-weight: var(--fw-medium);
  line-height: var(--h-line); letter-spacing: 0; color: var(--text);
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sess-time { flex: none; font-size: var(--fs-xs); line-height: var(--h-line); color: var(--faint); }
.sess-tag {
  flex: none; height: var(--h-chip); padding: 0 var(--sp-1-5); border-radius: var(--r-sm);
  font-size: var(--fs-xs); line-height: var(--h-chip);
  background: var(--hover); color: var(--muted);
}
.sess-tag.read-only { background: #ecfdf5; color: #047857; }
.sess-tag.full-access { background: #fffbeb; color: #b45309; }
.si-more {
  flex: none; width: 26px; height: 26px; border: none; background: transparent;
  border-radius: var(--r-sm); color: var(--faint); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  opacity: 0; transition: opacity .12s ease;
}
.si-more .dots { font-size: var(--fs-lg); line-height: var(--lh-solid); letter-spacing: -1px; margin-top: -6px; }
.sess-row:hover .si-more, .sess-row.active .si-more { opacity: 1; }
.si-more:hover { background: #e6eaf0; color: var(--text); }
.ws-empty { font-size: var(--fs-xs); padding: var(--sp-1) var(--sp-1-5) var(--sp-1-5); }
.si-menu {
  position: fixed; z-index: 60; min-width: 150px;
  background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-md);
  box-shadow: var(--shadow-pop); padding: var(--sp-1);
}
.si-menu-item {
  display: flex; align-items: center; gap: var(--sp-2); width: 100%;
  text-align: left; padding: 7px var(--sp-2-5); border-radius: var(--r-sm);
  border: none; background: transparent; font-size: var(--fs-sm); color: var(--text); cursor: pointer;
}
.si-menu-item:hover { background: var(--hover); }
.si-menu-item svg { color: var(--muted); flex: none; }
.si-menu-item.danger { color: #dc2626; }
.si-menu-item.danger svg { color: #dc2626; }
.si-menu-item.danger:hover { background: #fef2f2; }
.arch-toggle {
  width: 100%; text-align: center; padding: 7px; border: none; background: transparent;
  font-size: var(--fs-xs); color: var(--accent); cursor: pointer; border-radius: var(--r-sm);
}
.arch-toggle:hover { background: var(--accent-soft); }
.empty { text-align: center; padding: var(--sp-5) var(--sp-2); font-size: var(--fs-sm); }

.conversation { display: flex; flex-direction: column; gap: 16px; overflow: hidden; min-width: 0; min-height: 0; background: var(--panel); padding-bottom: 18px; }
.conv-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 68px; padding: 12px 28px; border: 0; border-bottom: 1px solid var(--border); border-radius: 0; box-shadow: none; }
.conv-head > div { min-width: 0; }
.conv-head strong { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: middle; font-size: var(--fs-md); font-weight: 600; }
.history-toggle, .head-actions .session-close { display: none; }
.banner { margin: 0 24px; background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412; padding: var(--sp-2) var(--sp-3); border-radius: var(--r-sm); font-size: var(--fs-sm); }

/* 消息流容器：包一层相对定位，给"回到最新"按钮当锚点 */
.stream-wrap { position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column; }
.to-bottom {
  position: absolute; left: 50%; transform: translateX(-50%); bottom: 10px;
  padding: 5px var(--sp-3-5); border-radius: 999px; font-size: var(--fs-xs); cursor: pointer;
  border: 1px solid var(--border); background: var(--panel); color: #475569;
  box-shadow: var(--shadow-pop);
}
.to-bottom:hover { border-color: var(--accent-border); color: var(--accent); }
/* 阅读宽度：整屏铺开的对话行太长（一行 1000+ px），眼睛回行很累。
   用 padding 居中收成 ~840px 的阅读列，不改 DOM 结构。 */
.stream {
  flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: var(--sp-2-5);
  padding: 16px max(24px, calc((100% - 840px) / 2));
}
.center { text-align: center; margin-top: 60px; }
/* 助手回答：**不带头像、不带外框**，正文直接排在页面上（对齐 deepseek harness 的排版）。
   用户消息保留蓝色气泡（一眼分得清谁说的），错误消息保留红色气泡。 */
.answer { flex: 1; min-width: 0; max-width: 100%; }

/* 正文排版：标题/列表/表格/代码块/引用都给出明确的层次与留白 */
.md-body { font-size: var(--fs-md); line-height: var(--lh-relaxed); color: var(--text); word-break: break-word; }
.md-body :deep(> :first-child){ margin-top: 0; }
.md-body :deep(> :last-child){ margin-bottom: 0; }
.md-body :deep(p){ margin: 0 0 var(--sp-2-5); }
.md-body :deep(h1), .md-body :deep(h2), .md-body :deep(h3), .md-body :deep(h4), .md-body :deep(h5), .md-body :deep(h6){
  margin: 18px 0 var(--sp-2); line-height: var(--lh-tight); font-weight: var(--fw-semibold);
}
.md-body :deep(h1){ font-size: var(--fs-xl); }
.md-body :deep(h2){ font-size: var(--fs-lg); }
/* h3 原来是 15.5px（阶梯里没这一档，且与 16px 只差 0.5px 看不出层次）：
   四级标题现在从令牌里取，20 → 16 → 14 → 13，每级都看得出差别 */
.md-body :deep(h3){ font-size: var(--fs-md); }
.md-body :deep(h4), .md-body :deep(h5), .md-body :deep(h6){ font-size: var(--fs-sm); }
.md-body :deep(ul), .md-body :deep(ol){ margin: 0 0 var(--sp-2-5); padding-left: 22px; }
.md-body :deep(li){ margin: var(--sp-0-5) 0; }
.md-body :deep(li > ul), .md-body :deep(li > ol){ margin: var(--sp-0-5) 0; }
.md-body :deep(strong){ font-weight: var(--fw-semibold); }
.md-body :deep(a){ color: var(--accent); text-decoration: none; border-bottom: 1px solid rgba(37, 99, 235, .3); }
.md-body :deep(a:hover){ border-bottom-color: var(--accent); }
.md-body :deep(code){
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--fs-sm); padding: var(--sp-hair) 5px; border-radius: var(--r-sm); background: var(--hover); color: #0f172a;
}
.md-body :deep(pre){
  margin: 0 0 var(--sp-3); padding: 11px 13px; border-radius: var(--r-md);
  background: var(--panel-soft); border: 1px solid #e8edf3; overflow-x: auto;
}
.md-body :deep(pre code){ padding: 0; background: none; font-size: var(--fs-sm); line-height: var(--lh-normal); }
.md-body :deep(blockquote){
  margin: 0 0 var(--sp-3); padding: var(--sp-1-5) var(--sp-3); border-left: 3px solid #dbe3ec;
  color: #4b5563; background: var(--panel-soft);
}
.md-body :deep(hr){ margin: var(--sp-4) 0; border: 0; border-top: 1px solid var(--border); }
.md-body :deep(table){ margin: 0 0 var(--sp-3); border-collapse: collapse; font-size: var(--fs-sm); }
.md-body :deep(th), .md-body :deep(td){ padding: var(--sp-1-5) var(--sp-2-5); border: 1px solid var(--border); text-align: left; }
.md-body :deep(th){ background: #f7f9fc; font-weight: var(--fw-semibold); }
.md-body :deep(img){ max-width: 100%; border-radius: var(--r-sm); }

/* 工具卡片（保留边框：它本来就不是"回答"，是要能一眼区分的执行记录） */
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.agent { justify-content: flex-start; }
.row.system { justify-content: center; }
.internal-message { width: 100%; min-width: 0; color: var(--faint); font-size: var(--fs-xs); }
.internal-message summary { cursor: pointer; line-height: 1.7; }
.internal-message pre { margin: 8px 0 0; padding: 10px; background: var(--panel); border-radius: var(--r-sm); white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; max-height: 240px; overflow-y: auto; }
.completion-warning { margin: 8px 0 0; color: var(--warn); font-size: var(--fs-xs); line-height: 1.6; }
.auto-note {
  max-width: 88%; font-size: var(--fs-xs); color: var(--faint);
  background: none; border: none; padding: var(--sp-0-5) 0; text-align: center;
  cursor: help;   /* 完整文本在 title 里（那段话是给模型看的"重新锚定"提示） */
}
.send-btn.stop { color: #fff; background: #dc2626; border-color: #dc2626; }
.send-btn.stop:hover { background: #b91c1c; }
.banner { display: flex; align-items: center; gap: var(--sp-2-5); }
.banner-act {
  flex: none; margin-left: auto; padding: 3px var(--sp-2-5); font-size: var(--fs-xs);
  border-radius: var(--r-sm); border: 1px solid #9a3412;
  background: var(--panel); color: #9a3412; cursor: pointer;
}
.banner-act:hover { background: #fff7ed; }
.approval-dialog { width: min(560px, 100%); max-height: 100%; min-height: 0; display: flex; flex-direction: column; gap: 8px; }
.approval-dialog .approval-status { margin: 0; flex: none; }
.approval-status > span { min-width: 0; overflow-wrap: anywhere; }
.bubble { max-width: 84%; padding: 12px 16px; border-radius: var(--r-md); background: var(--panel); border: 1px solid var(--border); white-space: pre-wrap; word-break: break-word; }
.row.user .bubble { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.row.err .bubble { background: #fef2f2; border-color: #fecaca; color: #991b1b; }

/* M29 回退入口：默认不出现，鼠标移到这条消息上才出现——它是"重来一次"的
   破坏性操作，不该在正常阅读时抢注意力。 */
.rewind-btn {
  display: none; align-items: center; gap: var(--sp-1); margin-top: var(--sp-1-5);
  padding: var(--sp-0-5) var(--sp-2); font-size: var(--fs-xs); cursor: pointer;
  border-radius: var(--r-sm); border: 1px solid var(--accent-border);
  background: var(--panel); color: var(--accent);
}
.row.user .bubble:hover .rewind-btn { display: inline-flex; }
.rewind-btn:hover { background: var(--panel); opacity: .85; }
.rewind-btn:disabled { cursor: not-allowed; opacity: .5; }

/* M29 熔断说明：中性的"已停下"卡片。刻意不用报错红——它是主动停下来等人，
   不是系统故障（红条会让人以为任务炸了，进而忽略里面真正有用的那几句）。 */
.stop-card {
  max-width: 86%; padding: var(--sp-2-5) 13px; border-radius: var(--r-md);
  border: 1px solid var(--border); background: var(--panel);
}
.stop-head { font-size: var(--fs-sm); font-weight: var(--fw-semibold); display: flex; align-items: center; gap: var(--sp-1-5); margin-bottom: var(--sp-1-5); }
.stop-ico { font-size: var(--fs-2xs); color: #b45309; }
.stop-body { font-size: var(--fs-sm); color: var(--text); white-space: pre-wrap; line-height: var(--lh-relaxed); }

/* M29 验收进度：标题右侧的一行摘要，悬停看清单细节 */
.acc-chip {
  margin-left: var(--sp-2-5); padding: var(--sp-hair) var(--sp-2); font-size: var(--fs-xs);
  border-radius: 999px; border: 1px solid var(--border);
  background: var(--hover); color: var(--text); cursor: help;
  font-variant-numeric: tabular-nums;
}
.acc-chip.done { color: #15803d; border-color: #bbf7d0; background: #f0fdf4; }

/* M29 回退报告的小标签：让"这一段是被撤回过的"一眼可辨 */
.rewind-tag {
  display: inline-block; margin-bottom: var(--sp-1); padding: var(--sp-hair) var(--sp-2);
  font-size: var(--fs-xs); border-radius: 999px;
  border: 1px solid var(--bd, var(--border)); background: var(--hover); color: var(--faint);
}

/* M30 任务清单：贴在输入框上方的一张卡（模型自己写的整份快照，见 lib/todos.js） */
.task-panel { margin-bottom: var(--sp-2); padding: 0; overflow: hidden; }
.task-head {
  display: flex; align-items: center; gap: var(--sp-2); width: 100%;
  padding: 9px var(--sp-3-5); background: none; border: none; cursor: pointer;
  font-size: var(--fs-sm); color: var(--text); text-align: left;
}
.task-head:hover { background: var(--hover); }
.task-ico { flex: none; color: var(--faint); }
.task-title { flex: none; font-weight: var(--fw-semibold); }
.task-counts { flex: 1; color: var(--faint); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
.task-caret { flex: none; color: var(--faint); font-size: var(--fs-2xs); transition: transform .12s ease; }
.task-caret.open { transform: rotate(180deg); }
.task-body { max-height: 208px; overflow: auto; padding: 0 var(--sp-3-5) var(--sp-2-5); }

/* M31 提问卡：形态对齐设计稿（eyebrow + 题干 + 编号选项 + 自定义答案 + 翻页 + 跳过/提交） */
.ask-card { position: relative; margin-bottom: var(--sp-2); padding: 0; }
.ask-head {
  display: flex; align-items: center; width: 100%; padding: var(--sp-3) var(--sp-3-5) var(--sp-0-5);
  background: none; border: none; cursor: pointer; text-align: left;
}
.ask-eyebrow { flex: 1; font-size: var(--fs-xs); color: var(--faint); }
.ask-chev { color: var(--faint); font-size: var(--fs-2xs); transition: transform .12s ease; }
.ask-chev.open { transform: rotate(180deg); }
.ask-close {
  position: absolute; top: 8px; right: 8px; width: 26px; height: 26px;
  border: none; background: none; cursor: pointer; color: var(--faint);
  font-size: var(--fs-lg); line-height: var(--lh-solid); border-radius: var(--r-sm);
}
.ask-close:hover { background: var(--hover); color: var(--text); }
.ask-body { padding: 0 var(--sp-3-5); }
.ask-question { margin: var(--sp-1) 0 var(--sp-2-5); font-size: var(--fs-md); font-weight: var(--fw-semibold); line-height: var(--lh-snug); }
.ask-hint { margin: 0 0 var(--sp-2); font-size: var(--fs-xs); color: var(--faint); }
.ask-options { display: flex; flex-direction: column; gap: var(--sp-0-5); }
.ask-opt {
  display: flex; gap: var(--sp-2); align-items: flex-start; width: 100%; text-align: left;
  padding: var(--sp-1-5) var(--sp-2); border: 1px solid transparent; border-radius: var(--r-sm);
  background: none; cursor: pointer; font-size: var(--fs-sm); color: var(--text);
}
.ask-opt:hover { background: var(--hover); }
.ask-opt.on { border-color: var(--accent-border); background: var(--hover); }
.ask-num {
  flex: none; width: 18px; height: 18px; margin-top: var(--sp-0-5);
  display: inline-flex; align-items: center; justify-content: center;
  font-size: var(--fs-xs); color: var(--faint); background: var(--hover); border-radius: 4px;
}
.ask-opt.on .ask-num { background: var(--accent); color: var(--on-accent); }
.ask-opt-copy { flex: 1; min-width: 0; }
.ask-opt-label { font-weight: var(--fw-semibold); }
.ask-rec {
  margin-left: var(--sp-1-5); padding: 0 var(--sp-1-5); font-size: var(--fs-xs); font-weight: var(--fw-regular);
  color: var(--accent); background: var(--accent-soft, var(--hover));
  border: 1px solid var(--accent-border); border-radius: 999px;
}
.ask-opt-desc { margin-left: var(--sp-2); color: var(--faint); font-weight: var(--fw-regular); }
.ask-custom {
  display: flex; gap: var(--sp-2); align-items: center; margin: var(--sp-2-5) 0 var(--sp-1);
  padding: 0 var(--sp-2); border-radius: var(--r-sm); border: 1px solid transparent;
}
.ask-custom:focus-within { border-color: var(--accent-border); background: var(--hover); }
.ask-custom svg { flex: none; color: var(--faint); }
.ask-custom textarea {
  flex: 1; min-height: 28px; max-height: 120px; resize: none; padding: 5px 0;
  border: none; outline: none; background: none; color: var(--text);
  font: inherit; font-size: var(--fs-sm); line-height: var(--lh-snug);
}
.ask-custom textarea::placeholder { color: var(--faint); }
.ask-error { margin: var(--sp-1-5) 0 0; font-size: var(--fs-xs); color: #b91c1c; }
.ask-foot { display: flex; align-items: center; gap: var(--sp-2-5); padding: var(--sp-2-5) var(--sp-3-5) var(--sp-3); }
.ask-pager { display: flex; align-items: center; gap: var(--sp-0-5); color: var(--faint); }
.ask-page {
  width: 24px; height: 24px; border: none; background: none; cursor: pointer;
  color: var(--faint); font-size: var(--fs-lg); line-height: var(--lh-solid); border-radius: var(--r-sm);
}
.ask-page:hover:not(:disabled) { background: var(--hover); color: var(--text); }
.ask-page:disabled { opacity: .35; cursor: default; }
.ask-page-num { font-size: var(--fs-xs); font-variant-numeric: tabular-nums; min-width: 34px; text-align: center; }
.ask-actions { margin-left: auto; display: flex; align-items: center; gap: var(--sp-2); }
.ask-skip {
  padding: 7px var(--sp-3-5); font-size: var(--fs-sm); cursor: pointer;
  border-radius: var(--r-sm); border: 1px solid var(--border); background: var(--panel); color: var(--text);
}
.ask-skip:hover { background: var(--hover); }
.ask-next {
  padding: 7px 18px; font-size: var(--fs-sm); cursor: pointer;
  border-radius: var(--r-sm); border: 1px solid var(--accent); background: var(--accent); color: var(--on-accent);
}
.ask-next:hover:not(:disabled) { opacity: .9; }
.ask-next:disabled, .ask-skip:disabled { opacity: .5; cursor: default; }

.task-row { display: flex; gap: var(--sp-2); align-items: flex-start; padding: 3px 0; font-size: var(--fs-sm); line-height: var(--lh-normal); }
.task-mark { flex: none; width: 14px; text-align: center; color: var(--faint); }
.task-row.ts-completed .task-mark { color: #15803d; }
.task-row.ts-completed .task-text { color: var(--faint); }
.task-row.ts-in_progress .task-mark { color: var(--accent); }
.task-text { flex: 1; word-break: break-word; }
.text { white-space: pre-wrap; }
.tools { margin-top: var(--sp-1-5); display: flex; flex-wrap: wrap; gap: var(--sp-1-5); }
.chip { font-size: var(--fs-xs); background: var(--hover); border: 1px solid var(--border); border-radius: 999px; padding: var(--sp-hair) var(--sp-2); }
.ts { font-size: var(--fs-xs); margin-top: var(--sp-1); }

/* 打字机光标 */
.streaming { border-color: var(--accent-border); }
/* 思考过程：小字 + 淡色 + 默认折叠。视觉上明确"这不是回答"，
   而且它是**那一条消息自己的**，渲染在回答上方（用户要求：思考不要和回答混在一起） */
.think { margin: 0 0 var(--sp-2); }
/* 工具步上的思考：更小、更靠左，明显属于"这一步"而不是整条回答 */
.think-step { margin-left: var(--sp-1-5); }
.think > summary {
  display: inline-flex; align-items: center; gap: var(--sp-1-5); cursor: pointer;
  list-style: none; font-size: var(--fs-sm); color: var(--muted);
}
.think > summary::-webkit-details-marker { display: none; }
.think > summary:hover { color: var(--accent); }
.think-body {
  margin-top: var(--sp-1-5); padding: 9px 11px; border-radius: var(--r-md);
  background: var(--panel-soft); border: 1px dashed #e2e8f0;
  color: var(--muted); font-size: var(--fs-sm); line-height: var(--lh-relaxed);
  white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow-y: auto;
}
.think-toggle {
  display: inline-flex; align-items: center; gap: 5px;
  border: 0; background: none; padding: 0; cursor: pointer;
  font-size: var(--fs-sm); color: var(--muted);
}
.think-toggle:hover { color: var(--accent); }
.think-arrow { display: inline-block; transition: transform .15s; }
.think-arrow.open { transform: rotate(90deg); }
.caret {
  display: inline-block; width: 7px; height: 14px; margin-left: var(--sp-0-5);
  background: var(--accent); vertical-align: text-bottom;
  animation: blink 1s steps(2, start) infinite;
}
@keyframes blink { to { visibility: hidden; } }

/* ---- 工具调用：一行摘要 + 点击展开（参考 deepseek harness 的形式） ----
   默认最小化：没有外框、没有常驻参数块，只有一行；hover 才浮现底色。
   状态色只放在状态文字上（失败/被拒一眼可见），不给整行上色——否则一屏全是色块。 */
.row.tool { justify-content: flex-start; flex-direction: column; width: 100%; }
.tool-row {
  display: flex; align-items: center; gap: var(--sp-1-5); width: 100%;
  padding: 3px var(--sp-1-5); border: none; border-radius: var(--r-sm); background: transparent;
  font: inherit; font-size: var(--fs-sm); color: var(--text-2); cursor: pointer; text-align: left;
}
.tool-row:hover { background: #f4f6f9; }
.tr-caret { flex: none; font-size: var(--fs-xs); color: var(--faint); transition: transform .14s ease; }
.tr-caret.open { transform: rotate(90deg); }
.tr-ico { flex: none; color: #8a93a1; }
.tr-verb { flex: none; color: #475569; }
.tr-sep { flex: none; color: #b9c0cb; }
.tr-target {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: var(--fs-xs); color: #64748b;
}
/* 文件路径用虚线下划线（DSH 里是可点开的链接样式）：一眼看出"这次动的是哪个文件" */
.tr-target.path { border-bottom: 1px dotted #c3cbd6; }
.tr-diff { flex: none; display: inline-flex; gap: 5px; font-size: var(--fs-xs); }
.tr-diff .add { color: #16a34a; }
.tr-diff .del { color: #dc2626; }
.tr-state { flex: none; font-size: var(--fs-xs); }
.tr-state.st-ok { color: #047857; }
.tr-state.st-running { color: var(--accent-hover); }
.tr-state.st-denied { color: #b45309; }
.tr-state.st-error { color: #b91c1c; }
.tr-time { flex: none; font-size: var(--fs-xs); }
.tr-spin {
  flex: none; width: 9px; height: 9px; border-radius: 50%;
  border: 1.6px solid var(--accent-border); border-top-color: var(--accent);
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.tool-detail { width: 100%; padding: 0 var(--sp-1-5) var(--sp-0-5) 26px; }
.tool-args {
  margin: var(--sp-1) 0 0; padding: var(--sp-2) var(--sp-2-5); background: var(--hover); color: #334155;
  border-radius: var(--r-sm); font-size: var(--fs-xs); line-height: var(--lh-normal);
  white-space: pre-wrap; word-break: break-word;
  max-height: 160px; overflow: auto;
}
.tool-output {
  margin: var(--sp-1-5) 0 0; padding: 9px var(--sp-2-5); background: var(--panel);
  border: 1px solid var(--border); border-radius: var(--r-sm);
  font-size: var(--fs-xs); line-height: var(--lh-normal);
  white-space: pre-wrap; word-break: break-word;
  max-height: 380px; overflow: auto;
}
.tool-running { margin-top: var(--sp-1-5); font-size: var(--fs-xs); color: var(--muted); }
.link-btn {
  border: none; background: transparent; color: var(--accent);
  font-size: var(--fs-xs); cursor: pointer; padding: 0 var(--sp-1);
}
.link-btn:hover { text-decoration: underline; }

.row.approve { justify-content: flex-start; }
.approve-card {
  width: 100%; background: var(--warn-soft);
  border: 1px solid #fed7aa; border-left: 3px solid #d97706;
  border-radius: var(--r-md); padding: var(--sp-2-5) var(--sp-3); font-size: var(--fs-sm);
}
.approve-title { display: flex; align-items: center; gap: var(--sp-1); }
.approve-ico { color: #d97706; }
.approve-reason { font-size: var(--fs-xs); margin-top: var(--sp-1); }
.approve-args {
  margin: var(--sp-2) 0 0; padding: var(--sp-2) var(--sp-2-5); background: var(--panel);
  border: 1px solid var(--border); border-radius: var(--r-sm); font-size: var(--fs-xs);
  white-space: pre-wrap; word-break: break-word;
  max-height: 150px; overflow: auto;
}

/* ---- composer（参考 deepseek harness 输入栏：胶囊卡片 + 工具行 + 圆形发送键） ---- */
/* 输入区与统计行收成同一列宽，视觉上是一条"对话列" */
.composer-wrap { display: flex; flex-direction: column; gap: 8px; width: calc(100% - 48px); max-width: 840px; margin: 0 auto; }
.composer {
  display: flex; flex-direction: column; gap: var(--sp-2);
  border-radius: var(--r-lg); padding: 14px; border-color: var(--border-strong); background: var(--panel);
  box-shadow: 0 4px 18px #24334d08;
}
.ta {
  border: none; outline: none; resize: none; background: transparent;
  font: inherit; line-height: var(--lh-normal); color: var(--text, #1f2937);
  min-height: 70px; max-height: 190px; padding: var(--sp-0-5) var(--sp-1);
}
.composer:focus-within { border-color: var(--accent-border); box-shadow: 0 0 0 3px #355dfb08; }
.ta:focus { box-shadow: none; }
.ta::placeholder { color: var(--faint); }
.ta:disabled { color: var(--placeholder); min-height: 44px; background: transparent; }

.tools-row { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2-5); flex-wrap: wrap; }
.tools-left { display: flex; align-items: center; gap: var(--sp-1-5); flex-wrap: wrap; }
.tools-right { display: flex; align-items: center; gap: var(--sp-2-5); min-width: 0; }

/* The composer uses one compact control rhythm; state is carried by icon and tint. */
.tool-btn, .perm-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  min-height: 30px; height: 30px; padding: 0 9px; border-radius: 8px;
  border: 1px solid transparent; background: var(--panel-soft);
  color: var(--text-2); font-size: 12px; font-weight: 500; cursor: pointer;
  white-space: nowrap; flex: none;
}
.tool-btn:hover:not(:disabled), .perm-btn:hover { background: var(--hover); border-color: var(--border); }
.tool-btn:disabled { opacity: .45; cursor: not-allowed; }
.perm-btn[aria-expanded="true"] { color: var(--accent); background: var(--accent-soft); border-color: var(--accent-border); }
.perm-btn > svg:last-child { color: var(--muted); transition: transform .15s; }
.perm-btn[aria-expanded="true"] > svg:last-child { transform: rotate(180deg); }
.perm-btn.read-only { color: #0f766e; background: #effaf8; }
.perm-btn.full-access { color: #a35a0a; background: #fff8ec; }
.perm-select, .web-select { position: relative; display: inline-flex; align-items: center; }
.web-btn.on { color: var(--accent); background: var(--accent-soft); }
.web-btn.off { color: var(--muted); }
.dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.dot.lite { background: #10b981; }.dot.default { background: var(--accent); }.dot.craft { background: #8b5cf6; }

.composer-menu {
  position: fixed; z-index: 45; width: 208px; max-width: calc(100vw - 24px); padding: 5px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: 0 8px 28px #1e2b471a, 0 2px 6px #1e2b470a;
  overscroll-behavior: contain; scrollbar-width: thin; text-align: left;
}
.choice-item {
  display: grid; grid-template-columns: 18px minmax(0, 1fr) 16px; align-items: center; gap: 9px;
  width: 100%; min-width: 0; min-height: 34px; margin: 1px 0; padding: 6px 8px; border: 1px solid transparent;
  border-radius: 6px; text-align: left; color: var(--text); background: transparent;
  white-space: normal; overflow-wrap: anywhere; cursor: pointer;
}
.choice-item:hover:not(:disabled) { background: var(--panel-soft); }
.choice-item.active, .choice-item.active:hover:not(:disabled) { background: #f1f4ff; color: var(--accent); }
.choice-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; box-shadow: none; }
.choice-icon { width: 18px; height: 18px; display: grid; place-items: center; color: var(--muted); }
.choice-icon.read-only { color: #0f766e; }
.choice-icon.workspace-write { color: var(--accent); }
.choice-icon.full-access { color: #a35a0a; }
.perm-text { min-width: 0; }
.perm-text b { font-size: 13px; font-weight: 500; line-height: 1.5; }
.choice-check { width: 16px; height: 16px; display: grid; place-items: center; }
.choice-check.checked { color: var(--accent); }

/* 侧栏工作区分组：组容器 + 空态（行样式见上面 M24 那一段，这里只留容器级规则） */
.ws-group { display: flex; flex-direction: column; }
.ws-empty { font-size: var(--fs-xs); padding: var(--sp-1) var(--sp-1-5) var(--sp-1-5); color: var(--placeholder); }

.meta-perm {
  display: inline-block; margin-left: var(--sp-1); padding: 0 5px;
  border-radius: 999px; font-size: var(--fs-xs); border: 1px solid transparent;
}
.meta-perm.read-only { color: #0f766e; background: #effaf8; border-color: #cdeee9; }
.meta-perm.full-access { color: #b45309; background: #fff8ec; border-color: #fbe3bd; }

.model-name {
  font-size: var(--fs-sm); color: var(--text-2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px;
}

/* 模型选择（右下角）：档位 + 具体模型 + 配置档案三段共用这一个菜单（M36） */
.model-select { position: relative; }
.model-btn {
  display: inline-flex; align-items: center; gap: var(--sp-1); max-width: 240px;
  border: 1px solid transparent; background: transparent; border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-1-5); cursor: pointer;
}
.model-btn:hover { background: var(--hover); }
.model-btn svg { color: var(--faint); flex: none; }
/* 模型菜单（M36）：现在只有一段——配置档案。选档案即换模型。
   原来的"默认模型 / 全部模型 / 搜索 / 分组 / 刷新"整块已删（用户要求），
   对应的 .mm-search / .mm-group / .mm-refresh / .model-btn.pinned 样式一并删掉。 */
.model-menu {
  position: fixed; z-index: 45;
  width: 300px; max-height: 320px; overflow-y: auto;
  background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-md);
  box-shadow: var(--shadow-pop); padding: 5px;
}
.mm-title { font-size: var(--fs-xs); font-weight: var(--fw-semibold); color: var(--text); margin: var(--sp-1) var(--sp-2) var(--sp-1-5); }
.mm-title .muted { font-weight: var(--fw-regular); margin-left: var(--sp-hair); }
.mm-item {
  display: flex; align-items: center; gap: 7px; width: 100%;
  text-align: left; padding: 7px 9px; border-radius: var(--r-sm);
  border: none; background: transparent; font-size: var(--fs-sm); color: var(--text); cursor: pointer;
}
.mm-item:hover { background: var(--hover); }
.mm-item.active { background: var(--accent-soft); }
.mm-name { font-weight: var(--fw-medium); }
.mm-model { margin-left: auto; font-size: var(--fs-xs); color: var(--faint); white-space: nowrap; }
.mm-badge { color: var(--accent); font-size: var(--fs-xs); flex: none; }
.mm-note { font-size: var(--fs-xs); color: var(--faint); margin: var(--sp-1-5) var(--sp-2) var(--sp-1); }
.p-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; display: inline-block; }
.p-dot.ok { background: #22c55e; }
.p-dot.warn { background: #f59e0b; }

.spinner {
  width: 15px; height: 15px; border-radius: 50%;
  border: 2px solid var(--border-strong); border-top-color: var(--accent);
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.send-btn {
  width: 36px; height: 36px; border-radius: 50%; border: none;
  background: var(--accent, var(--accent)); color: var(--on-accent); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; flex: none;
  transition: opacity .15s ease, transform .1s ease;
}
.send-btn:hover:not(:disabled) { opacity: .9; }
.send-btn:active:not(:disabled) { transform: scale(.95); }
.send-btn:disabled { background: var(--border-strong); color: #fff; cursor: not-allowed; }

.stats-line { font-size: var(--fs-xs); display: flex; gap: var(--sp-2); justify-content: center; flex-wrap: wrap; }

/* [+] 引用菜单 */
.add-select { position: relative; }
.tool-btn.open { background: #eef1f5; color: var(--text); }
.add-menu {
  position: fixed; z-index: 45;
  width: 320px; max-height: 360px; overflow-y: auto;
  background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-md);
  box-shadow: var(--shadow-pop); padding: 5px;
}
.add-item {
  display: flex; align-items: center; gap: var(--sp-2); width: 100%;
  text-align: left; padding: var(--sp-2) var(--sp-2-5); border-radius: var(--r-sm);
  border: none; background: transparent; font-size: var(--fs-sm); color: var(--text);
  cursor: pointer; white-space: nowrap;
}
.add-item:hover { background: var(--hover); }
.add-item.sub { padding-left: 30px; font-size: var(--fs-sm); }
.add-item svg { flex: none; color: var(--muted); }
.add-note { margin-left: auto; font-size: var(--fs-xs); color: var(--faint); overflow: hidden; text-overflow: ellipsis; }
.add-count {
  margin-left: auto; min-width: 18px; height: 18px; border-radius: 999px;
  background: var(--accent-soft); color: var(--accent); font-size: var(--fs-xs);
  display: inline-flex; align-items: center; justify-content: center; padding: 0 5px;
}
.add-sublist { border-top: 1px dashed var(--border); margin: var(--sp-0-5) var(--sp-1-5); padding: 3px 0; }
.add-empty { font-size: var(--fs-xs); color: var(--faint); padding: var(--sp-1-5) var(--sp-2-5); margin: 0; }

/* 统计 pill 与弹窗 */
.stats-wrap { position: relative; width: 100%; max-width: 880px; margin: 0 auto; }
.stat-pill {
  display: inline-flex; align-items: center; gap: var(--sp-1);
  font-size: var(--fs-xs); color: var(--muted); padding: 3px var(--sp-2-5);
  border-radius: 999px; border: 1px solid transparent; background: transparent;
  cursor: pointer;
}
.stat-pill:hover { background: #eef1f5; color: var(--text); }
.stat-pop {
  position: fixed; z-index: 45;
  width: 320px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-md);
  box-shadow: var(--shadow-pop); padding: var(--sp-3) var(--sp-3-5);
}
.pop-title { font-size: var(--fs-sm); font-weight: var(--fw-semibold); padding-bottom: var(--sp-2); margin-bottom: var(--sp-1-5); border-bottom: 1px solid var(--border); }
.pop-row { display: flex; align-items: center; justify-content: space-between; font-size: var(--fs-sm); padding: var(--sp-1) 0; color: #4b5563; }
.pop-row b { font-weight: var(--fw-semibold); color: var(--text); }
.pop-note { font-size: var(--fs-xs); color: var(--faint); margin: var(--sp-2) 0 0; line-height: var(--lh-snug); }
/* 上下文占用弹窗：分段进度条 + 三行图例 */
.ctx-pop { width: 340px; }
.ctx-head { display: flex; align-items: baseline; justify-content: space-between; font-size: var(--fs-sm); margin-bottom: var(--sp-2); }
.ctx-pct { font-size: var(--fs-lg); }
.ctx-total { color: var(--muted); font-size: var(--fs-xs); }
.ctx-bar {
  display: flex; height: 8px; border-radius: 999px; overflow: hidden;
  background: #eef1f5; margin-bottom: var(--sp-2-5);
}
.ctx-bar .seg { height: 100%; transition: width .25s; }
.ctx-bar .seg.sys { background: var(--faint); }
.ctx-bar .seg.tool { background: #a78bfa; }
.ctx-bar .seg.msg { background: var(--accent); }
.ctx-legend { display: flex; flex-direction: column; gap: var(--sp-1-5); }
.ctx-row { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-sm); color: #4b5563; }
.ctx-row .dot { width: 9px; height: 9px; border-radius: 3px; flex: none; }
.ctx-row .dot.sys { background: var(--faint); }
.ctx-row .dot.tool { background: #a78bfa; }
.ctx-row .dot.msg { background: var(--accent); }
.ctx-row .v { margin-left: auto; font-weight: var(--fw-semibold); color: var(--text); }
.composer-hint {
  text-align: center; font-size: var(--fs-xs); color: #9a6b12;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2-5); margin-bottom: var(--sp-0-5);
}

/* 附件（任意格式上传）chip 行 */
.attach-row { display: flex; flex-wrap: wrap; gap: var(--sp-1-5); }
.attach-chip {
  display: inline-flex; align-items: center; gap: 5px;
  max-width: 260px; padding: var(--sp-1) var(--sp-2); font-size: var(--fs-xs);
  background: #f4f6f8; border: 1px solid var(--border); border-radius: 999px;
  color: #4b5563;
}
.attach-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.attach-size { color: var(--faint); font-size: var(--fs-xs); flex: none; }
.attach-x {
  flex: none; width: 16px; height: 16px; line-height: var(--lh-solid); padding: 0;
  border: none; background: transparent; color: var(--faint);
  font-size: var(--fs-md); cursor: pointer; border-radius: 50%;
}
.attach-x:hover { background: #e5e9ef; color: #dc2626; }
.bubble-atts { display: flex; flex-wrap: wrap; gap: var(--sp-1); margin-bottom: var(--sp-1); }
.row.user .bubble .attach-chip {
  background: rgba(255, 255, 255, .18); border-color: rgba(255, 255, 255, .35); color: #fff;
}

.modal-mask { position: fixed; inset: 0; box-sizing: border-box; padding: 12px; overflow: auto; background: var(--overlay); display: grid; place-items: center; z-index: 50; }
.modal { width: 460px; padding: var(--sp-5); }
.modal h3 { margin: 0 0 var(--sp-2-5); }
.modal p { margin: var(--sp-1-5) 0; }
.args { background: var(--panel-soft); border: 1px solid var(--border); border-radius: var(--r-sm); padding: var(--sp-2-5); font-size: var(--fs-xs); max-height: 160px; overflow: auto; white-space: pre-wrap; }
.actions { display: flex; justify-content: flex-end; gap: var(--sp-2-5); margin-top: var(--sp-3-5); }

.sess-row.active { background: var(--accent-soft); }
.sess-row.active .sess-title { color: var(--accent); }
.sess-row:focus-within .si-more, .ws-row:focus-within .row-actions { opacity: 1; }
.stat-pop, .ctx-pop { max-width: calc(100vw - 24px); }
@media (max-width: 1250px) {
  .chat.with-preview { grid-template-columns: 232px minmax(0, 1fr); }
  .preview-panel { position: absolute; top: 10px; bottom: 10px; right: 10px; width: min(420px, calc(100% - 20px)); z-index: 25; border: 1px solid var(--border); border-radius: var(--r-lg); box-shadow: var(--shadow-pop); }
}
@media (max-width: 960px) {
  .chat, .chat.with-preview { grid-template-columns: minmax(0, 1fr); }
  .history-toggle, .head-actions .session-close { display: inline-flex; flex: none; }
  .session-list { display: none; }
  .session-list.mobile-open { display: flex; position: absolute; left: 8px; top: 8px; bottom: 8px; width: min(300px, calc(100% - 16px)); z-index: 30; border: 1px solid var(--border); border-radius: var(--r-lg); box-shadow: var(--shadow-pop); }
  .conversation { grid-column: 1; }
}
@media (max-width: 760px) {
  .conv-head { min-height: 60px; padding: 10px 16px; gap: 8px; }
  .conversation { padding-bottom: 12px; gap: 10px; }
  .stream { padding: 12px 16px; }
  .composer-wrap { width: calc(100% - 24px); }
  .composer { padding: 10px; }
  .banner { margin: 0 12px; }
  .tools-row { gap: 6px; }.tools-right { margin-left: auto; }.tool-btn { font-size: var(--fs-xs); padding: 0 6px; }
  .connection-status { font-size: 11px; }
}
</style>
