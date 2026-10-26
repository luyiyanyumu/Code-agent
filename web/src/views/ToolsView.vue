<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, readFileBase64 } from '../api.js'
import { useDialog } from '../composables/useDialog.js'
import PageHeader from '../components/ui/PageHeader.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import UiIcon from '../components/ui/UiIcon.vue'

// ============================================================ 目录数据
const catalog = ref(null)
const error = ref('')
const notice = ref('')
const busy = ref('')
const props = defineProps({
  initialTab: { type: String, default: 'builtin' },
  embedded: { type: Boolean, default: false },
})
const categories = ['builtin', 'mcp', 'plugin', 'off', 'skill', 'expert']
const filter = ref(categories.includes(props.initialTab) ? props.initialTab : 'builtin')
const expanded = ref('')
const catalogQuery = ref('')
const dialog = useDialog()

// 设置搜索定位分类时仅重置目录筛选，保留尚未保存的插件、技能和 MCP 表单。
function openCategory(tab) {
  if (!categories.includes(tab)) return false
  filter.value = tab
  catalogQuery.value = ''
  expanded.value = ''
  return true
}

defineExpose({ openCategory })

function matchesCatalog(item) {
  const query = catalogQuery.value.trim().toLocaleLowerCase()
  return !query || [item.name, item.description_zh, item.description].some((value) => String(value || '').toLocaleLowerCase().includes(query))
}

// 来源筛选：按来源分开看，**不再有「全部」**（用户要求取消全部显示：
// 一次铺 18+ 个工具太挤，也分不清谁属于谁）。后两个筛技能/专家（它们不是工具，是提示词能力）。
const FILTERS = [
  { key: 'builtin', label: '内置工具' },
  { key: 'mcp', label: 'MCP' },
  { key: 'plugin', label: '插件工具' },
  { key: 'off', label: '已禁用' },
  { key: 'skill', label: '技能' },
  { key: 'expert', label: '专家' },
]

const summary = computed(() => catalog.value?.summary || {})
const bySource = computed(() => summary.value.by_source || {})
const packsByKind = computed(() => summary.value.packs_by_kind || {})

const STATS = computed(() => [
  { label: '工具总数', value: summary.value.tools_total ?? 0, hint: `模型可调用 ${(catalog.value?.tools || []).filter((t) => t.enabled !== false).length} 个` },
  { label: '已禁用', value: summary.value.disabled ?? 0, hint: '助手暂不可使用' },
  { label: '内置', value: bySource.value.builtin ?? 0, hint: '平台自带' },
  { label: 'MCP', value: bySource.value.mcp ?? 0, hint: `${summary.value.mcp?.connected ?? 0}/${summary.value.mcp?.configured ?? 0} 已连接` },
  { label: '插件工具', value: bySource.value.plugin ?? 0, hint: '来自插件包' },
  { label: '技能', value: packsByKind.value.skill ?? 0, hint: '可复用的工作方法' },
  { label: '专家', value: packsByKind.value.expert ?? 0, hint: '按需启用的专业能力' },
])

// 筛选与内容严格对应：点哪个 chip，正文与底部面板都只看它（不再有「全部」）
const TOOL_HEADINGS = {
  builtin: '内置工具',
  mcp: 'MCP 工具',
  plugin: '插件工具',
  off: '已禁用的工具',
  skill: '技能',
  expert: '专家',
}
const showTools = computed(() => filter.value !== 'skill' && filter.value !== 'expert')
const showPlugins = computed(() => filter.value === 'plugin')
const showMcp = computed(() => filter.value === 'mcp')

const tools = computed(() => {
  const list = (catalog.value?.tools || []).filter(matchesCatalog)
  if (filter.value === 'off') return list.filter((t) => t.enabled === false)
  if (filter.value === 'skill' || filter.value === 'expert') return []
  return list.filter((t) => t.source === filter.value)
})

const packs = computed(() => {
  const list = (catalog.value?.packs || []).filter(matchesCatalog)
  if (filter.value === 'skill' || filter.value === 'expert') {
    return list.filter((p) => p.kind === filter.value)
  }
  return []
})

const plugins = computed(() => catalog.value?.plugins || [])
const mcpServers = computed(() => catalog.value?.mcp || [])
const codeTools = computed(() => catalog.value?.code_tools || { status: {}, errors: [] })

const EMPTY_HINTS = {
  all: '没有任何工具',
  builtin: '没有内置工具（不应出现，平台至少自带 bash / read_file / write_file）',
  mcp: '还没有 MCP 工具——在下方配置并连接服务器后出现在这里',
  plugin: '没有插件代码工具——这是默认安全状态（代码工具需双重开关才加载）',
}

async function load() {
  error.value = ''
  try {
    catalog.value = await api.tools()
  } catch (e) {
    error.value = e.message
  }
}

async function refresh() {
  busy.value = 'reload'
  error.value = ''
  notice.value = ''
  try {
    const r = await api.reloadPlugins()
    notice.value = `已重新扫描：${r.plugins} 个插件，${r.enabled.length} 个启用`
    if (r.mcp_restart_required) notice.value += '；MCP 声明有变化，需重启服务才生效'
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

// ============================================================ 一键启用 / 禁用（M21）
// 禁用 = 从**模型可见的工具列表**里摘掉（后端 FilteredToolRegistry）+ 注册表层拒调，
// 下一次模型响应就生效（不用重启）。与"审批策略"互补：策略管能不能自动放行，
// 禁用管这个工具存不存在。

const disabledCount = computed(() => (catalog.value?.tools || []).filter((t) => t.enabled === false).length)

async function toggleTool(t) {
  if (!catalog.value?.can_edit) return
  busy.value = 'tool:' + t.name
  error.value = ''
  notice.value = ''
  try {
    const r = await api.setToolEnabled(t.name, !t.enabled)
    t.enabled = r.enabled
    catalog.value.disabled_tools = r.disabled_tools
    notice.value = r.enabled
      ? `已启用 ${t.name}`
      : `已禁用 ${t.name}：它不再出现在模型可见的工具里`
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

/** 工具的"改/删"入口：MCP 工具去 MCP 配置弹窗改命令行或删行；插件工具去插件区删插件。 */
function gotoSource(t) {
  if (t.source === 'mcp') {
    openMcpModal()
  } else {
    filter.value = 'plugin'
    notice.value = `${t.name} 来自插件：在下方「插件」区停用或删除整个插件即可`
  }
}

/** 技能/专家：一键停用/启用（停用后不在技能目录、load_skill 读不到）。 */
async function togglePack(p) {
  const label = p.kind === 'skill' ? '技能' : '专家'
  const next = p.enabled === false
  busy.value = `pack:${p.kind}:${p.name}`
  error.value = ''
  notice.value = ''
  try {
    const r = await api.setPackEnabled(p.kind, p.name, next)
    p.enabled = r.enabled
    notice.value = r.enabled
      ? `已启用${label} ${p.name}`
      : `已停用${label} ${p.name}：模型看不到它了，load_skill 也读不出来`
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

/** 技能/专家：删除（目录移入 .trash 回收站，可手动找回）。 */
async function removePack(p) {
  const { kind, name } = p
  const label = kind === 'skill' ? '技能' : '专家'
  return dialog.open({
    title: `删除${label}`,
    description: '整个目录将移入 .trash 回收站，可手动找回。',
    details: name,
    tone: 'danger',
    icon: 'tools',
    confirmText: `删除${label}`,
    pendingText: '正在删除…',
    onConfirm: async () => {
      busy.value = `pack:${kind}:${name}`
      error.value = ''
      notice.value = ''
      try {
        const r = await api.deletePack(kind, name)
        notice.value = `已删除${label} ${name} → ${r.moved_to}`
        await load()
      } finally {
        busy.value = ''
      }
    },
  })
}

/** MCP server：停用/启用（写 mcp.json，需重启服务才断开/连上）。 */
async function toggleMcp(s) {
  busy.value = 'mcp:' + s.name
  error.value = ''
  notice.value = ''
  try {
    const r = await api.setMcpEnabled(s.name, s.enabled === false)
    s.enabled = r.enabled
    notice.value = `${r.enabled ? '已启用' : '已停用'} MCP ${s.name}；${r.note}`
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

/** MCP server：删除（整行移出 mcp.json）。 */
async function removeMcp(s) {
  const name = s.name
  return dialog.open({
    title: '删除 MCP 连接器',
    description: '连接器配置将从 mcp.json 移除，重启服务后断开连接。',
    details: name,
    tone: 'danger',
    icon: 'tools',
    confirmText: '删除连接器',
    pendingText: '正在删除…',
    onConfirm: async () => {
      busy.value = 'mcp:' + name
      error.value = ''
      notice.value = ''
      try {
        await api.deleteMcp(name)
        notice.value = `已删除 MCP ${name}（重启服务后生效）`
        await load()
      } finally {
        busy.value = ''
      }
    },
  })
}

async function setAllTools(enabled) {
  if (!catalog.value?.can_edit) return
  const total = (catalog.value.tools || []).length
  const apply = async () => {
    busy.value = 'tools-all'
    error.value = ''
    notice.value = ''
    try {
      const r = await api.setToolsEnabled(enabled)
      await load()
      notice.value = enabled
        ? '已启用全部工具（禁用清单已清空）'
        : `已禁用 ${r.count} 个工具：模型现在调不动任何工具`
    } finally {
      busy.value = ''
    }
  }
  if (!enabled) return dialog.open({
    title: '禁用全部工具',
    description: '助手将无法调用工具，只能进行对话。之后可通过「全部启用」恢复。',
    details: `当前共 ${total} 个工具；此操作对所有工具分类生效。`,
    tone: 'warning',
    icon: 'tools',
    confirmText: '禁用全部工具',
    pendingText: '正在禁用…',
    onConfirm: apply,
  })
  try { await apply() } catch (e) { error.value = e.message }
}

async function togglePlugin(p) {
  busy.value = p.name
  error.value = ''
  notice.value = ''
  try {
    const r = await api.togglePlugin(p.name, !p.enabled)
    notice.value = `插件 ${p.name} 已${p.enabled ? '停用' : '启用'}（影响 ${r.active_sessions_updated} 个活跃会话）`
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

async function removePlugin(p) {
  const name = p.name
  return dialog.open({
    title: '删除插件',
    description: '插件目录将移入 plugins/.trash 回收站，可手动找回。',
    details: name,
    tone: 'danger',
    icon: 'tools',
    confirmText: '删除插件',
    pendingText: '正在删除…',
    onConfirm: async () => {
      busy.value = name
      error.value = ''
      notice.value = ''
      try {
        await api.deletePlugin(name)
        notice.value = `插件 ${name} 已删除`
        await load()
      } finally {
        busy.value = ''
      }
    },
  })
}

function toggleExpand(name) {
  expanded.value = expanded.value === name ? '' : name
}

// 来源 → 徽章样式类
const SOURCE_CLASS = { builtin: 's-builtin', mcp: 's-mcp', plugin: 's-plugin' }

function policyText(t) {
  if (t.policy === 'allow') return '免审批'
  if (t.policy === 'policy') return '按参数判定'
  return '需审批'
}

// ---- 工具审批策略（M15）：管理员可在列表直接改 ----
const POLICY_LABELS = { allow: '免审批', approval: '需审批', policy: '按参数判定' }

async function setPolicy(t, mode) {
  error.value = ''
  notice.value = ''
  try {
    const r = await api.setPermission(t.name, mode)
    notice.value = `工具 ${t.name} 审批策略已改为「${POLICY_LABELS[mode] || mode}」（热更新到 ${r.active_sessions_updated} 个活跃会话）`
    await load()
  } catch (e) {
    error.value = e.message
    await load() // 失败回滚显示
  }
}

// 自定义下拉：原生 select 的 option 会继承字色、面板样式也不可控，换成自绘菜单。
// 用 fixed 定位（列表容器有滚动/裁剪，absolute 会被切掉）。
const policyMenu = ref(null) // 当前展开的工具对象（null=关闭）
const policyMenuStyle = ref({})

function togglePolicyMenu(t, ev) {
  if (policyMenu.value?.name === t.name) {
    policyMenu.value = null
    return
  }
  const r = ev.currentTarget.getBoundingClientRect()
  const width = 132
  const left = Math.round(Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8)))
  const below = window.innerHeight - r.bottom
  policyMenuStyle.value =
    below > 140
      ? { left: left + 'px', top: Math.round(r.bottom + 6) + 'px', width: width + 'px' }
      : {
          left: left + 'px',
          bottom: Math.round(window.innerHeight - r.top + 6) + 'px',
          width: width + 'px',
        }
  policyMenu.value = t
}

function pickPolicy(t, mode) {
  policyMenu.value = null
  if (!t || mode === t.policy) return
  setPolicy(t, mode)
}

function originText(t) {
  if (t.origin === 'platform') return '平台内置'
  if (t.origin.startsWith('plugin:')) return `插件 ${t.origin.slice(7)}`
  if (t.origin === 'mcp.json') return 'mcp.json'
  return t.origin
}

// ============================================================ 制作规范（页面直接展示）
const guideOpen = ref(false)
const guideTab = ref('plugin')

const GUIDE = {
  plugin: {
    label: '插件包',
    intro: '一个插件 = plugins/ 下的一个目录 + 一份 plugin.json 清单。可内嵌技能、声明 MCP 服务器、放行名单与 prompt 片段。打包成 zip 后在「上传插件」里提交即可。',
    rules: [
      '目录里 plugin.json 是唯一必需文件；其余（skills/、prompt.md、tools.py）都可选',
      '目录名 / 清单 name：字母或数字开头，仅含字母数字点下划线连字符，≤64 字符',
      'zip 结构支持两种：清单在根目录，或所有文件在同一个顶层文件夹下（自动剥掉）',
      '安全：声明式能力只走既有信任门禁；allowTools 是显式放行，插件不能给自己发权限',
      'tools.py（代码式工具）默认不加载——需要 CODE_AGENT_PLUGIN_CODE=1 且清单 codeTools=true 双重开关',
      '上传后立即生效（技能 / prompt / 放行名单热更新到活跃会话）；声明的 MCP 服务器需重启服务才连接',
    ],
    template: `{
  "name": "fin-pack",
  "version": "1.0.0",
  "description": "金融数据能力包",
  "author": "…",
  "enabled": true,
  "skills": ["a-share"],          // 只启用这些内嵌技能；省略 = 全部
  "mcpServers": {
    "yahoo": { "command": "npx", "args": ["-y", "yahoo-mcp"], "trusted": false }
  },
  "allowTools": ["mcp__yahoo__quote"],   // 免审批放行的工具名
  "prompt": "回答先给结论，再给依据。",     // 追加到 system prompt
  "codeTools": false                       // 是否允许加载 tools.py（默认 false）
}

目录结构：
fin-pack/
├── plugin.json        # 清单（唯一必需）
├── skills/            # 内嵌技能：skills/<skill-name>/SKILL.md
├── prompt.md          # 附加 system prompt 片段（可选，json 里的 prompt 优先）
└── tools.py           # 代码式工具（可选，默认不加载）`,
  },
  skill: {
    label: '技能',
    intro: '技能 = skills/<name>/ 目录，**至少含一个 SKILL.md**（frontmatter 写 name 与 description，正文是给模型的指令）。system prompt 只常驻「目录」（name + 一句描述），模型用 load_skill 按名加载正文——description 是唯一的触发开关，要写清「何时加载」。复杂技能可在目录里附 scripts/（脚本）、references/（参考资料）、assets/（资源文件），正文按相对路径引用，模型用 read_file 读取。',
    rules: [
      'name：字母或数字开头，仅含字母数字点下划线连字符，≤64 字符；与目录名一致最稳，避免同名覆盖时混乱',
      'description 是进 system prompt 的那句话，是模型判断「何时加载」的唯一信号——写触发场景，不写「是什么」',
      '正文写给模型看：用祈使句写「流程 / 格式 / 约束」，不写给人看的背景铺垫',
      '正文建议固定四段：## 何时用（触发条件）→ ## 步骤（顺序固定）→ ## 输出格式 → ## 边界（什么不答）',
      '正文建议 ≤ 400 行：目录常驻、正文按需加载，写太长会挤占上下文窗口',
      '技能是目录：至少含 SKILL.md；复杂技能附 scripts/、references/、assets/ 等任意子文件，正文按相对路径引用，模型用 read_file 读取',
      '选型：让模型「会做一件事」用技能；让模型「查事实/查文档」用知识库（检索自动注入，无需主动加载）',
      '可在「新建技能」里表单填写并逐项添加资源；也可**整体上传技能包 zip**（目录打包：根含 SKILL.md + 可选 scripts/、references/、assets/，同名勾选覆盖）；本地技能与插件内嵌技能同名时本地优先',
    ],
    template: `# 目录结构（skills/weekly-report/）
weekly-report/
├── SKILL.md            # 必需：frontmatter + 指令正文
├── scripts/            # 可选：脚本（正文里按相对路径引用）
│   └── gen_report.py
├── references/         # 可选：参考资料
│   └── template.md
└── assets/             # 可选：资源文件

# SKILL.md 内容如下
---
name: weekly-report
description: 用户要求「写周报 / 本周总结 / 周报邮件」时加载，按四段式整理。
---

# 写周报

## 何时用
用户提到「周报」「本周总结」「weekly report」时加载。

## 步骤（顺序固定）
1. 本周进展 —— 只写完成态的事，动词开头，每条一行
2. 数据与证据 —— 提交号 / 指标变化 / 文档链接，可溯源
3. 风险与阻塞 —— 卡在哪、需要谁做什么决策
4. 下周计划 —— 只写能交付的产出物，不写愿望

## 输出格式
Markdown 分四段，每段最多 5 条；数字必须可溯源。

## 边界
- 非周报场景不要加载
- 没有数据时如实标注「暂无数据」，不编造`,
  },
  mcp: {
    label: 'MCP 连接器',
    intro: 'MCP 服务器在服务启动时连接。配置写进 ~/.code-agent/mcp.json（页面「编辑 MCP」可视化改），插件也可用清单里的 mcpServers 字段声明。',
    rules: [
      '每个 server 必填 command（启动命令）；args 是字符串数组，env 是 字符串->字符串 对象',
      'server 名：字母或数字开头，仅含字母数字点下划线连字符——工具名会变成 mcp__<server>__<tool>',
      'trusted: true 表示信任该 server 的工具免审批放行；不确定就别开，默认走人工审批',
      '字段白名单：command / args / env / trusted，拼错的字段会被拒绝（静默生效比报错更糟）',
      '保存后需重启服务才连接（连接在启动时建立）；连不上的 server 会显示在目录里并带错误原因',
      '插件声明的 server 在「编辑 MCP」里只读——要改就改插件的 plugin.json',
    ],
    template: `{
  "mcpServers": {
    "fetch": {
      "command": "npx",
      "args": ["-y", "mcp-server-fetch"]
    },
    "files": {
      "command": "uvx",
      "args": ["mcp-server-filesystem", "/data"],
      "trusted": false
    }
  }
}`,
  },
}

function copyGuide() {
  navigator.clipboard?.writeText(GUIDE[guideTab.value].template)
}

// ============================================================ 上传插件
const pluginModal = ref(false)
const pluginFile = ref(null)
const pluginOverwrite = ref(false)
const pluginUploading = ref(false)

function onPluginFile(e) {
  pluginFile.value = e.target.files?.[0] || null
}

async function submitPluginUpload() {
  if (!pluginFile.value) {
    error.value = '请先选择一个 .zip 插件包'
    return
  }
  pluginUploading.value = true
  error.value = ''
  notice.value = ''
  try {
    const b64 = await readFileBase64(pluginFile.value)
    const r = await api.uploadPlugin(pluginFile.value.name, b64, pluginOverwrite.value)
    notice.value = `插件 ${r.plugin?.name || ''} 已上传（${r.files} 个文件），能力已热更新`
    pluginModal.value = false
    pluginFile.value = null
    pluginOverwrite.value = false
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    pluginUploading.value = false
  }
}

// ============================================================ 新建技能
const skillModal = ref(false)
const skillForm = ref({ name: '', description: '', content: '', resources: [] })
const skillUploading = ref(false)

function openSkillModal() {
  skillForm.value = { name: '', description: '', content: '', resources: [] }
  skillModal.value = true
}

function addResource() {
  skillForm.value.resources.push({ path: '', content: '' })
}

function removeResource(i) {
  skillForm.value.resources.splice(i, 1)
}

async function onSkillFile(e) {
  const file = e.target.files?.[0]
  if (!file) return
  const text = await file.text()
  // 兼容直接导出的 SKILL.md：frontmatter 里的 name/description 顺手填进表单
  const m = text.match(/^\s*---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/)
  if (m) {
    const meta = {}
    for (const line of m[1].split('\n')) {
      const i = line.indexOf(':')
      if (i > 0) meta[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim()
    }
    skillForm.value.name = skillForm.value.name || meta.name || ''
    skillForm.value.description = skillForm.value.description || meta.description || ''
    skillForm.value.content = m[2].trim()
  } else {
    skillForm.value.content = text
  }
  skillForm.value.name = skillForm.value.name || file.name.replace(/\.md$/i, '')
  e.target.value = ''
}

async function onResourceFile(e, i) {
  const file = e.target.files?.[0]
  if (!file) return
  // 资源文件统一 base64 上传（支持二进制：脚本 / 图片 / 数据文件）
  skillForm.value.resources[i].content_b64 = await readFileBase64(file)
  skillForm.value.resources[i].path =
    skillForm.value.resources[i].path || `assets/${file.name}`
  e.target.value = ''
}

async function submitSkill() {
  skillUploading.value = true
  error.value = ''
  notice.value = ''
  try {
    const f = skillForm.value
    const resources = (f.resources || [])
      .map((r) => {
        const p = (r.path || '').trim()
        if (!p) return null
        if ((r.content || '').trim()) return { path: p, content: r.content.trim() }
        if (r.content_b64) return { path: p, content_b64: r.content_b64 }
        return null
      })
      .filter(Boolean)
    const r = await api.uploadSkill(f.name, f.description, f.content, resources)
    notice.value = `技能 ${r.skill.name} 已保存（${r.skill.files?.length || 1} 个文件），目录已热更新`
    skillModal.value = false
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    skillUploading.value = false
  }
}

// ============================================================ 上传技能包（zip 整体）
const skillZipFile = ref(null)
const skillZipOverwrite = ref(false)
const skillZipUploading = ref(false)

function onSkillZipFile(e) {
  skillZipFile.value = e.target.files?.[0] || null
}

async function submitSkillZip() {
  if (!skillZipFile.value) {
    error.value = '请先选择一个技能包 zip'
    return
  }
  skillZipUploading.value = true
  error.value = ''
  notice.value = ''
  try {
    const b64 = await readFileBase64(skillZipFile.value)
    const r = await api.importSkillZip(skillZipFile.value.name, b64, skillZipOverwrite.value)
    notice.value = `技能包 ${r.skill.name} 已导入（${r.skill.files} 个文件），目录已热更新`
    skillModal.value = false
    skillZipFile.value = null
    skillZipOverwrite.value = false
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    skillZipUploading.value = false
  }
}

// ============================================================ 编辑 MCP
const mcpModal = ref(false)
const mcpRows = ref([])
const mcpFromPlugins = ref([])
const mcpSaving = ref(false)
const mcpRestart = ref(false)

async function openMcpModal() {
  error.value = ''
  try {
    const cfg = await api.mcpConfig()
    mcpRows.value = Object.entries(cfg.servers || {}).map(([name, c]) => ({
      name,
      command: c.command || '',
      argsText: (c.args || []).join(' '),
      trusted: !!c.trusted,
    }))
    mcpFromPlugins.value = cfg.from_plugins || []
    mcpRestart.value = false
    mcpModal.value = true
  } catch (e) {
    error.value = e.message
  }
}

function addMcpRow() {
  mcpRows.value.push({ name: '', command: '', argsText: '', trusted: false })
}

function removeMcpRow(i) {
  mcpRows.value.splice(i, 1)
}

async function submitMcp() {
  mcpSaving.value = true
  error.value = ''
  try {
    const servers = {}
    for (const r of mcpRows.value) {
      const name = (r.name || '').trim()
      if (!name) continue
      servers[name] = {
        command: (r.command || '').trim(),
        args: r.argsText.trim() ? r.argsText.trim().split(/\s+/) : [],
        ...(r.trusted ? { trusted: true } : {}),
      }
    }
    const res = await api.saveMcpConfig(servers)
    mcpRestart.value = !!res.restart_required
    notice.value = 'mcp.json 已保存' + (res.restart_required ? '；重启服务后新配置才会连接' : '')
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    mcpSaving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page tools-page" :class="{ 'tools-embedded': embedded }">
    <div class="tools-header">
      <PageHeader v-if="!embedded" title="工具与扩展" description="管理助手的能力、连接与权限，让工具各尽其用。" eyebrow="资源" icon="tools" />
      <div class="tools-header-actions" role="group" aria-label="工具与扩展操作">
        <!-- 层级：一个主操作（新建技能）+ 两个次操作 + 两个"工具型"动作（规范/重扫）。
             之前 5 个按钮长得一样、主次不分；「编辑 MCP」还和下面 MCP 区里那颗重复，
             这里删掉（改在 MCP 区那颗，就近操作更自然）。 -->
        <button class="ghost" :title="guideOpen ? '收起制作规范' : '插件 / 技能 / MCP 的制作规范'" @click="guideOpen = !guideOpen">
          {{ guideOpen ? '收起规范' : '制作规范' }}
        </button>
        <button
          class="ghost icon-only"
          :disabled="busy === 'reload'"
          :title="busy === 'reload' ? '扫描中…' : '重新扫描插件目录'"
          @click="refresh"
        >
          <UiIcon name="refresh" :size="15" />
          <span class="sr">{{ busy === 'reload' ? '扫描中' : '重新扫描' }}</span>
        </button>
        <span class="head-sep"></span>
        <button @click="pluginModal = true"><UiIcon name="upload" :size="15" />上传插件</button>
        <button class="primary" @click="openSkillModal"><UiIcon name="plus" :size="15" />新建技能</button>
      </div>
    </div>

    <p v-if="error" class="feedback err" role="alert">{{ error }}</p>
    <p v-if="notice" class="feedback ok" role="status">{{ notice }}</p>

    <!-- ============ 制作规范 ============ -->
    <section v-if="guideOpen" class="card block guide">
      <div class="guide-tabs">
        <button
          v-for="(g, key) in GUIDE"
          :key="key"
          class="chip"
          :class="{ active: guideTab === key }"
          @click="guideTab = key"
        >
          {{ g.label }}
        </button>
        <span class="spacer"></span>
        <button class="small" @click="copyGuide">复制模板</button>
      </div>
      <p class="muted sub">{{ GUIDE[guideTab].intro }}</p>
      <ul class="guide-rules">
        <li v-for="r in GUIDE[guideTab].rules" :key="r">{{ r }}</li>
      </ul>
      <pre class="mono guide-template">{{ GUIDE[guideTab].template }}</pre>
    </section>

    <!-- ============ 统计 ============ -->
    <div class="stats card" aria-label="能力概览">
      <div v-for="s in STATS" :key="s.label" class="stat" :title="s.hint">
        <div class="stat-value">{{ s.value }}</div>
        <div class="stat-label">{{ s.label }}</div>
        <div class="muted stat-hint">{{ s.hint }}</div>
      </div>
    </div>

    <!-- ============ 筛选 ============ -->
    <div class="page-toolbar catalog-toolbar">
    <div class="filters" aria-label="能力分类">
      <button
        v-for="f in FILTERS"
        :key="f.key"
        :id="'extensions-' + f.key"
        class="chip"
        :class="{ active: filter === f.key }"
        :aria-pressed="filter === f.key"
        @click="filter = f.key"
      >
        {{ f.label }}<template v-if="f.key === 'off' && disabledCount">（{{ disabledCount }}）</template>
      </button>
    </div>
    <label class="catalog-search"><UiIcon name="search" :size="16" /><input v-model="catalogQuery" aria-label="搜索工具、技能或专家" placeholder="搜索名称或描述…" /></label>
    </div>

    <!-- ============ 工具 / 技能 / 专家 ============ -->
    <section class="card block catalog-panel">
      <div class="catalog-heading">
      <div><h3 v-if="!showTools">{{ filter === 'skill' ? '技能' : '专家' }} <span class="count">{{ packs.length }}</span></h3>
      <h3 v-else>{{ TOOL_HEADINGS[filter] }} <span class="count">{{ tools.length }}</span></h3>
      <p class="muted hint">{{ showTools ? '查看工具用途、启用状态与审批方式。' : '按需加载的工作方法与专业能力。' }}</p></div>
      <span v-if="catalog?.can_edit && showTools" class="filters-actions">
        <button class="small" :disabled="busy === 'tools-all' || !disabledCount" @click="setAllTools(true)">
          全部启用
        </button>
        <button
          class="small danger"
          :disabled="busy === 'tools-all'"
          title="禁用后模型调不动任何工具（可随时一键恢复）"
          @click="setAllTools(false)"
        >
          一键禁用全部
        </button>
      </span>
      </div>

      <div v-if="showTools" class="items">
        <div v-for="t in tools" :key="t.name" class="item" :class="{ 'is-off': !t.enabled }">
          <div class="item-head">
            <button class="tool-name" :aria-expanded="expanded === t.name" @click="toggleExpand(t.name)"><span class="tool-icon"><UiIcon name="tools" :size="16" /></span><code class="mono name">{{ t.name }}</code></button>
            <span class="badge" :class="SOURCE_CLASS[t.source]">{{ t.source === 'builtin' ? '内置' : t.source === 'mcp' ? 'MCP' : '插件' }}</span>
            <span v-if="!t.enabled" class="tag-off" title="已禁用：模型看不到这个工具，也调不动">已禁用</span>
            <span class="muted origin">{{ originText(t) }}</span>
            <span class="spacer"></span>
            <!-- 一键启用/禁用：禁用 = 从模型可见的工具列表里摘掉（比改策略更彻底） -->
            <button
              v-if="catalog?.can_edit"
              class="tgl"
              :class="{ on: t.enabled }"
              :disabled="busy === 'tool:' + t.name"
              :title="t.enabled ? '点击禁用（模型将看不到它）' : '点击启用'"
              @click.stop="toggleTool(t)"
            >
              <span class="knob"></span>
              <span class="sw-text">{{ t.enabled ? '已启用' : '已禁用' }}</span>
            </button>
            <span v-else class="tag-off" v-if="!t.enabled">已禁用</span>
            <!-- 审批策略：管理员点开切换（免审批 / 需审批 / 按参数判定） -->
            <button
              v-if="catalog?.can_edit"
              class="policy-btn"
              :class="'p-' + t.policy"
              title="点击切换审批策略（立即热生效）"
              @click.stop="togglePolicyMenu(t, $event)"
            >
              {{ policyText(t) }}
              <svg class="caret" viewBox="0 0 16 16" width="9" height="9" aria-hidden>
                <path d="M3.5 6l4.5 4.5L12.5 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
              </svg>
            </button>
            <span v-else class="badge policy" :class="t.policy">{{ policyText(t) }}</span>
            <!-- 可改/可删的（MCP 行、插件带来的工具）：给一个跳转过去的入口，
                 内置工具是代码自带的，只能禁用——如实说明，不放假的"删除"按钮 -->
            <button
              v-if="catalog?.can_edit && t.deletable"
              class="small"
              :title="t.edit_hint"
              @click.stop="gotoSource(t)"
            >
              {{ t.source === 'mcp' ? '改/删 MCP' : '改/删插件' }}
            </button>
            <button class="details-toggle" :aria-expanded="expanded === t.name" :aria-label="`${expanded === t.name ? '收起' : '展开'} ${t.name} 详情`" @click="toggleExpand(t.name)"><UiIcon name="chevron-down" :size="16" :class="{ expanded: expanded === t.name }" /></button>
          </div>
          <div class="muted desc">{{ t.description_zh || t.description }}</div>
          <div v-if="expanded === t.name" class="detail">
            <div class="muted" style="font-size:var(--fs-xs)">权限说明：{{ t.policy_label }}</div>
            <div class="muted" style="font-size:var(--fs-xs)">
              状态：{{ t.enabled ? '已启用' : '已禁用（不进模型可见的工具列表，调它会被拒）' }}
              · 来源可改/可删：{{ t.edit_hint }}
            </div>
            <div class="muted" style="font-size:var(--fs-xs); margin:var(--sp-1-5) 0 4px">参数 schema</div> <pre class="mono schema">{{ JSON.stringify(t.parameters, null, 2) }}</pre>
          </div>
        </div>
        <EmptyState v-if="!tools.length" icon="tools" :title="catalogQuery.trim() ? '没有匹配的工具' : '暂时没有工具'" :description="catalogQuery.trim() ? '尝试其他名称或关键词，或切换上方分类。' : (EMPTY_HINTS[filter] || '该来源下暂无工具')" />
      </div>

      <div v-else class="items">
        <div v-for="p in packs" :key="p.kind + p.name" class="item" :class="{ 'is-off': p.enabled === false }">
          <div class="item-head">
            <span class="badge" :class="p.kind === 'skill' ? 's-skill' : 's-expert'">{{ p.kind === 'skill' ? '技能' : '专家' }}</span>
            <code class="mono name">{{ p.name }}</code>
            <span v-if="p.enabled === false" class="tag-off" title="已停用：不在技能目录里，load_skill 也读不到">已停用</span>
            <span class="muted origin">{{ p.origin === 'local' ? '本地目录' : p.origin.replace('plugin:', '插件 ') }}</span>
            <span class="spacer"></span>
            <!-- 技能/专家与工具同一套操作：一键停用/启用 + 删除（M22）。
                 停用 = 不在技能目录里、load_skill 也读不到；删除 = 整个目录移入 .trash（可找回）。 -->
            <button
              v-if="catalog?.can_edit"
              class="tgl"
              :class="{ on: p.enabled !== false }"
              :disabled="busy === 'pack:' + p.kind + ':' + p.name"
              :title="p.enabled === false ? '点击启用' : '点击停用（模型将看不到这个技能）'"
              @click="togglePack(p)"
            >
              <span class="knob"></span>
              <span>{{ p.enabled === false ? '已停用' : '已启用' }}</span>
            </button>
            <span v-if="!p.deletable" class="muted" style="font-size:var(--fs-xs)">由插件带入</span>
            <button
              v-else-if="catalog?.can_edit"
              class="small danger"
              :disabled="busy === 'pack:' + p.kind + ':' + p.name"
              title="整个目录移入 .trash 回收站（可手动找回）"
              @click="removePack(p)"
            >
              删除
            </button>
          </div>
          <div class="muted desc">{{ p.description || '(无描述)' }}</div>
        </div>
        <EmptyState v-if="!packs.length" icon="tools" :title="catalogQuery.trim() ? '没有匹配的能力' : `还没有${filter === 'skill' ? '技能' : '专家'}`" :description="catalogQuery.trim() ? '尝试其他名称或关键词。' : '创建自己的技能，或上传插件包来扩展助手。'"><button v-if="!catalogQuery.trim() && filter === 'skill'" @click="openSkillModal">新建技能</button></EmptyState>
      </div>
    </section>

    <!-- ============ 底部面板：与筛选对应 ============ -->
    <div v-if="showPlugins || showMcp" class="cols">
      <section v-if="showPlugins" class="card block">
        <h3>插件包（{{ plugins.length }}）</h3>
        <p class="muted hint">
          一个插件可同时带来内嵌技能、MCP 服务器、权限放行名单与 prompt 片段。制作规则见页首「制作规范」。
        </p>
        <div class="items">
          <div v-for="p in plugins" :key="p.name" class="item">
            <div class="item-head">
              <code class="mono name">{{ p.name }}</code>
              <span v-if="p.version" class="muted origin">v{{ p.version }}</span>
              <span class="spacer"></span>
              <span v-if="p.error" class="badge s-error">清单异常</span>
              <button class="small danger" :disabled="busy === p.name" @click="removePlugin(p)">删除</button>
              <label class="switch" @click.stop>
                <input
                  type="checkbox"
                  :checked="p.enabled"
                  :disabled="!!p.error || busy === p.name"
                  @change="togglePlugin(p)"
                />
                <span>{{ p.enabled ? '已启用' : '已停用' }}</span>
              </label>
            </div>
            <div class="muted desc">{{ p.description || '(无描述)' }}</div>
            <div v-if="p.error" class="err" style="font-size:var(--fs-xs)">{{ p.error }}</div>
            <div class="tags">
              <span v-if="p.skills_found.length" class="tag">技能 {{ p.skills_found.length }}</span>
              <span v-if="p.mcp_servers.length" class="tag">MCP {{ p.mcp_servers.length }}</span>
              <span v-if="p.allow_tools.length" class="tag">放行 {{ p.allow_tools.length }}</span>
              <span v-if="p.has_prompt" class="tag">prompt</span>
              <span v-if="p.code_tools" class="tag warn">代码工具</span>
            </div>
            <div v-if="p.enabled && !p.error" class="muted" style="font-size:var(--fs-xs)">
              {{ p.skills_found.join('、') || '未包含技能' }}
            </div>
          </div>
          <EmptyState v-if="!plugins.length" icon="tools" title="还没有安装插件" description="上传插件包，为助手添加工具、技能与外部连接。"><button @click="pluginModal = true">上传插件</button></EmptyState>
        </div>

        <div v-if="Object.keys(codeTools.status).length" class="code-tools">
          <div class="muted" style="font-size:var(--fs-xs)">代码式工具（插件 tools.py）</div>
          <div v-for="(state, name) in codeTools.status" :key="name" class="muted" style="font-size:var(--fs-xs)">
            · {{ name }}：{{ state }}
          </div>
        </div>
      </section>

      <section v-if="showMcp" class="card block">
        <div class="block-head">
          <h3>MCP 连接器（{{ mcpServers.length }}）</h3>
          <button class="small" @click="openMcpModal">编辑 MCP</button>
        </div>
        <p class="muted hint">
          外部工具服务器，启动时连接。未标记 trusted 的服务器，其工具一律走人工审批。
        </p>
        <div class="items">
          <div v-for="s in mcpServers" :key="s.name" class="item" :class="{ 'is-off': s.enabled === false }">
            <div class="item-head">
              <span class="badge" :class="s.enabled === false ? '' : (s.connected ? 's-ok' : 's-error')">
                {{ s.enabled === false ? '已停用' : (s.connected ? '已连接' : '未连接') }}
              </span>
              <code class="mono name">{{ s.name }}</code>
              <span class="muted origin">{{ s.origin === 'mcp.json' ? 'mcp.json' : s.origin.replace('plugin:', '插件 ') }}</span>
              <span class="spacer"></span>
              <span class="muted" style="font-size:var(--fs-xs)">{{ s.tools }} 个工具</span>
              <!-- MCP 与工具同一套操作：一键停用/启用 + 删除（改 mcp.json，重连要重启服务） -->
              <button
                v-if="catalog?.can_edit"
                class="tgl"
                :class="{ on: s.enabled !== false }"
                :disabled="busy === 'mcp:' + s.name"
                :title="s.enabled === false ? '点击启用（重启服务后连接）' : '点击停用（重启服务后断开）'"
                @click="toggleMcp(s)"
              >
                <span class="knob"></span>
                <span>{{ s.enabled === false ? '已停用' : '已启用' }}</span>
              </button>
              <span v-if="!s.deletable" class="muted" style="font-size:var(--fs-xs)">由插件声明</span>
              <button
                v-else-if="catalog?.can_edit"
                class="small danger"
                :disabled="busy === 'mcp:' + s.name"
                title="从 mcp.json 移除这一行（重启服务后断开）"
                @click="removeMcp(s)"
              >
                删除
              </button>
            </div>
            <div v-if="s.error" class="err" style="font-size:var(--fs-xs)">{{ s.error }}</div>
          </div>
          <EmptyState v-if="!mcpServers.length" icon="tools" title="连接你的外部工具" description="配置 MCP 服务器，让助手访问所需的工具与数据。"><button @click="openMcpModal">配置连接器</button></EmptyState>
        </div>
      </section>
    </div>

    <!-- ============ 弹窗：上传插件 ============ -->
    <div v-if="pluginModal" class="modal-mask" @click.self="pluginModal = false">
      <div class="card modal" role="dialog" aria-modal="true" aria-label="上传插件包">
        <h3>上传插件包</h3>
        <p class="muted" style="font-size:var(--fs-sm)">
          选择插件 zip（清单 plugin.json 必须在根目录，或所有文件在同一个顶层文件夹下）。
          打包规则见「制作规范 → 插件包」。
        </p>
        <input type="file" aria-label="选择插件包" accept=".zip" @change="onPluginFile" />
        <label class="switch" style="margin-top:10px">
          <input v-model="pluginOverwrite" type="checkbox" />
          <span>覆盖同名插件</span>
        </label>
        <div class="actions">
          <button @click="pluginModal = false">取消</button>
          <button class="primary" :disabled="pluginUploading" @click="submitPluginUpload">
            {{ pluginUploading ? '上传中…' : '上传并启用' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 弹窗：新建技能 ============ -->
    <div v-if="skillModal" class="modal-mask" @click.self="skillModal = false">
      <div class="card modal wide" role="dialog" aria-modal="true" aria-label="新建或覆盖技能">
        <h3>新建 / 覆盖技能</h3>
        <p class="muted" style="font-size:var(--fs-sm)">
          frontmatter 会按下面两个字段自动生成；也可以直接导入一个 .md 文件。
          格式说明见「制作规范 → 技能」。
        </p>
        <div class="form-grid">
          <label for="skill-name">名称</label>
          <input id="skill-name" v-model="skillForm.name" placeholder="如 weekly-report" />
          <label for="skill-description">简介</label>
          <input id="skill-description" v-model="skillForm.description" placeholder="一句话说明适合在什么场景使用" />
          <label for="skill-import">导入 Markdown</label>
          <input id="skill-import" type="file" accept=".md,.markdown,.txt" @change="onSkillFile" />
          <label for="skill-content">正文</label>
          <textarea id="skill-content" v-model="skillForm.content" rows="14" placeholder="写给模型的指令（流程 / 格式 / 约束）"></textarea>
        </div>

        <div class="resources">
          <div class="res-head">
            <span class="muted" style="font-size:var(--fs-sm)">
              附带资源（可选）：脚本 / 参考资料 / 资源文件。正文里按相对路径引用，模型可用 read_file 读取。
            </span>
            <button class="small" @click="addResource">+ 添加资源</button>
          </div>
          <div v-for="(r, i) in skillForm.resources" :key="i" class="res-row">
            <input v-model="r.path" class="mono" :aria-label="`资源 ${i + 1} 路径`" placeholder="相对路径，如 scripts/run.py" />
            <textarea v-model="r.content" :aria-label="`资源 ${i + 1} 内容`" rows="3" placeholder="文本内容（也可通过下方选择文件导入）"></textarea>
            <div class="res-actions">
              <input type="file" class="res-file" :aria-label="`导入资源 ${i + 1}`" @change="onResourceFile($event, i)" />
              <button class="small danger" @click="removeResource(i)">删除</button>
            </div>
          </div>
        </div>

        <div class="zip-import">
          <div class="zip-head">
            <span class="muted" style="font-size:var(--fs-sm)">
              或整体上传技能包：把整套技能目录打成 zip（根含 SKILL.md，可带 scripts/、references/、assets/），一次导入。
            </span>
          </div>
          <div class="zip-row">
            <input type="file" accept=".zip" aria-label="选择技能包" @change="onSkillZipFile" />
            <label class="switch">
              <input v-model="skillZipOverwrite" type="checkbox" />
              <span>覆盖同名技能</span>
            </label>
            <button :disabled="skillZipUploading" @click="submitSkillZip">
              {{ skillZipUploading ? '导入中…' : '导入技能包' }}
            </button>
          </div>
        </div>

        <div class="actions">
          <button @click="skillModal = false">取消</button>
          <button class="primary" :disabled="skillUploading" @click="submitSkill">
            {{ skillUploading ? '保存中…' : '保存技能' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 弹窗：编辑 MCP ============ -->
    <div v-if="mcpModal" class="modal-mask" @click.self="mcpModal = false">
      <div class="card modal wide" role="dialog" aria-modal="true" aria-label="编辑 MCP 连接器">
        <h3>编辑 MCP 连接器</h3>
        <p class="muted" style="font-size:var(--fs-sm)">
          写入 <code class="mono">~/.code-agent/mcp.json</code>。多个参数用空格分隔；
          保存后需重启服务才连接。插件声明的 server
          <span v-if="mcpFromPlugins.length" class="mono">{{ mcpFromPlugins.join('、') }}</span>
          <span v-else>（当前无）</span> 只读，改对应插件的清单。
        </p>
        <div v-for="(row, i) in mcpRows" :key="i" class="mcp-row">
          <input v-model="row.name" class="mono" :aria-label="`连接器 ${i + 1} 名称`" placeholder="名称" />
          <input v-model="row.command" class="mono" :aria-label="`连接器 ${i + 1} 命令`" placeholder="命令，如 npx / uvx / node" />
          <input v-model="row.argsText" class="mono" :aria-label="`连接器 ${i + 1} 参数`" placeholder="参数（空格分隔）" />
          <label class="switch">
            <input v-model="row.trusted" type="checkbox" />
            <span>trusted</span>
          </label>
          <button class="small danger" @click="removeMcpRow(i)">删除</button>
        </div>
        <p v-if="!mcpRows.length" class="muted empty">还没有本地 MCP 服务器。</p>
        <button class="small" @click="addMcpRow">+ 添加 server</button>
        <p v-if="mcpRestart" class="ok" style="font-size:var(--fs-sm)">已保存——重启服务后新配置才会连接。</p>
        <div class="actions">
          <button @click="mcpModal = false">关闭</button>
          <button class="primary" :disabled="mcpSaving" @click="submitMcp">
            {{ mcpSaving ? '保存中…' : '保存 mcp.json' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 审批策略下拉菜单（fixed 定位，避免被列表容器裁剪/滚动带走） -->
    <div v-if="policyMenu" class="policy-mask" @click="policyMenu = null"></div>
    <div v-if="policyMenu" class="policy-menu" :style="policyMenuStyle">
      <button
        v-for="(label, mode) in POLICY_LABELS"
        :key="mode"
        class="policy-item"
        :class="{ active: policyMenu.policy === mode }"
        @click="pickPolicy(policyMenu, mode)"
      >
        <span class="pi-label">{{ label }}</span>
        <span v-if="policyMenu.policy === mode" class="pi-check">✓</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 28px; overflow-y: auto; background: var(--bg); }
.page > * { max-width: 1440px; margin-left: auto; margin-right: auto; }
.tools-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px 24px; margin-bottom: 28px; }
.tools-header :deep(.page-header) { margin-bottom: 0; }
.tools-header-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
/* 嵌入设置后由设置内容区统一滚动和留白，避免嵌套滚动条及重复页面标题。 */
.page.tools-embedded { padding: 0; margin: 0; max-width: none; flex: none; overflow: visible; min-width: 0; background: transparent; }
.tools-embedded > * { max-width: none; }
.tools-embedded .tools-header { margin-bottom: 20px; }
h3 { margin: 0 0 15px; font-size: 15px; font-weight: 650; }
.head-sep { width: 1px; height: 20px; background: var(--border); margin: 0 3px; }
.ghost { color: var(--muted); }
.icon-only { padding: 0 10px; }
button.small { font-size: 11px; padding: 5px 9px; }
.sub { margin: 0 0 16px; font-size: 13px; line-height: 1.75; }
.feedback { padding: 10px 13px; margin-bottom: 16px; border-radius: 7px; font-size: 12px; }
.err { color: var(--err); }
.ok { color: var(--ok); }
.feedback.err { background: var(--err-soft, #fff1f2); }
.feedback.ok { background: var(--ok-soft, #edf9f3); }
.stats { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); margin-bottom: 24px; padding: 20px 0; }
.stat { min-width: 0; padding: 0 20px; }
.stat + .stat { border-left: 1px solid var(--border); }
.stat-value { font-size: 25px; font-weight: 650; line-height: 1.3; letter-spacing: -.7px; margin-bottom: 7px; }
.stat-label { font-size: 12px; font-weight: 550; }
.stat-hint { font-size: 10px; margin-top: 6px; line-height: 1.5; overflow-wrap: anywhere; }
.catalog-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.filters { display: flex; flex-wrap: wrap; gap: 4px; min-width: 0; }
.chip { border: 1px solid transparent; background: transparent; border-radius: 6px; padding: 7px 12px; font-size: 12px; color: var(--muted); cursor: pointer; }
.chip:hover { background: var(--hover); color: var(--text); }
.chip.active { background: var(--panel); border-color: var(--border); color: var(--accent); font-weight: 600; box-shadow: 0 1px 2px #20273506; }
.catalog-search { display: flex; align-items: center; gap: 8px; width: 232px; flex: none; padding-left: 10px; background: var(--panel); border: 1px solid var(--border); border-radius: 7px; color: var(--muted); }
.catalog-search:focus-within { border-color: var(--accent); }
.catalog-search input { width: 100%; min-width: 0; border: 0; box-shadow: none; background: transparent; font-size: 12px; padding-left: 0; }
.catalog-search input:focus { box-shadow: none; }
.block { padding: 22px; margin-bottom: 20px; }
.catalog-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
.catalog-heading h3 { margin: 0; }
.hint { font-size: 12px; margin: 8px 0 16px; line-height: 1.7; }
.catalog-heading .hint { margin-bottom: 0; }
.count { margin-left: 5px; padding: 2px 7px; font-size: 11px; font-weight: 500; color: var(--muted); background: var(--panel-soft); border-radius: 5px; }
.filters-actions { display: inline-flex; gap: 6px; flex-wrap: wrap; flex: none; }
.block-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.block-head h3 { margin: 0; }
.items { display: flex; flex-direction: column; }
.item { padding: 17px 0; border-bottom: 1px solid var(--border); }
.item:last-child { border-bottom: 0; }
.item-head { display: flex; align-items: center; gap: 9px; min-width: 0; flex-wrap: wrap; }
.item-head .name { font-size: 12px; font-weight: 600; overflow-wrap: anywhere; }
.tool-name { display: inline-flex; align-items: center; justify-content: flex-start; gap: 9px; min-width: 0; max-width: 100%; padding: 0; border: 0; background: transparent; text-align: left; min-height: 30px; }
.tool-name:hover { color: var(--accent); }
.tool-icon { display: inline-flex; padding: 7px; background: var(--panel-soft); border-radius: 7px; color: var(--muted); flex: none; }
.origin { font-size: 10px; overflow-wrap: anywhere; }
.spacer { flex: 1; }
.desc { font-size: 12px; margin: 9px 0 0; line-height: 1.75; overflow-wrap: anywhere; }
.details-toggle { border: 0; background: transparent; color: var(--muted); padding: 5px; min-height: 28px; }
.details-toggle svg { transition: transform .15s; }
.details-toggle .expanded { transform: rotate(180deg); }
.detail { margin-top: 14px; padding: 14px 16px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 7px; line-height: 1.8; overflow-wrap: anywhere; }
.schema { margin: 6px 0 0; padding: 12px; font-size: 11px; max-height: 240px; overflow: auto; white-space: pre-wrap; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; }
.empty { font-size: 12px; line-height: 1.7; }
.tgl { display: inline-flex; align-items: center; gap: 6px; padding: 5px 9px; border-radius: 6px; border: 1px solid var(--border); background: var(--panel); min-height: 28px; font-size: 10px; color: var(--muted); cursor: pointer; }
.tgl .knob { width: 7px; height: 7px; border-radius: 50%; background: var(--border-strong); transition: background .12s; }
.tgl.on { color: var(--ok); background: var(--ok-soft); border-color: color-mix(in srgb, var(--ok) 25%, var(--border)); }
.tgl.on .knob { background: var(--ok); }
.tgl:disabled { opacity: .5; cursor: not-allowed; }
.item.is-off .desc, .item.is-off .item-head .name { opacity: .6; }
.tag-off { flex: none; font-size: 10px; padding: 3px 6px; border-radius: 5px; background: var(--panel-soft); color: var(--muted); border: 1px solid var(--border); }
.badge { font-size: 10px; padding: 3px 7px; border-radius: 5px; border: 1px solid var(--border); white-space: nowrap; }
.s-builtin { background: var(--panel-soft); color: var(--muted); }
.s-mcp, .s-plugin { background: var(--accent-soft); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 25%, var(--border)); }
.s-skill, .s-ok, .badge.policy.allow { background: var(--ok-soft); color: var(--ok); border-color: color-mix(in srgb, var(--ok) 25%, var(--border)); }
.s-expert, .badge.policy.approval { background: var(--warn-soft); color: var(--warn); border-color: color-mix(in srgb, var(--warn) 25%, var(--border)); }
.s-error { background: var(--err-soft); color: var(--err); border-color: color-mix(in srgb, var(--err) 25%, var(--border)); }
.badge.policy.policy { background: var(--panel-soft); color: var(--muted); }
.policy-btn { display: inline-flex; align-items: center; gap: 5px; font-family: inherit; font-size: 10px; min-height: 28px; line-height: 1.2; padding: 5px 8px; border-radius: 6px; border: 1px solid var(--border); background: var(--panel); color: var(--text); cursor: pointer; white-space: nowrap; }
.policy-btn .caret { opacity: .65; }
.policy-btn.p-allow { color: var(--ok); background: var(--ok-soft); border-color: color-mix(in srgb, var(--ok) 25%, var(--border)); }
.policy-btn.p-approval { color: var(--warn); background: var(--warn-soft); border-color: color-mix(in srgb, var(--warn) 25%, var(--border)); }
.policy-btn.p-policy { background: var(--panel-soft); color: var(--muted); }
.policy-mask { position: fixed; inset: 0; z-index: 60; max-width: none; }
.policy-menu { position: fixed; z-index: 61; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; box-shadow: var(--shadow-pop); padding: 5px; }
.policy-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; text-align: left; padding: 8px 10px; border-radius: 5px; border: 0; background: transparent; color: var(--text); font-size: 12px; cursor: pointer; }
.policy-item:hover { background: var(--hover); }
.policy-item.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.pi-check { font-size: 11px; }
.guide-tabs { display: flex; gap: 5px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.guide-rules { margin: 0 0 16px; padding-left: 19px; font-size: 12px; line-height: 1.85; color: var(--text); }
.guide-template { margin: 0; padding: 16px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 7px; font-size: 11px; line-height: 1.75; max-height: 340px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.cols { display: flex; flex-direction: column; gap: 0; align-items: stretch; }
.switch { display: flex; align-items: center; gap: 7px; font-size: 12px; cursor: pointer; }
.switch input { cursor: pointer; flex: none; }
.tags { display: flex; gap: 5px; margin-top: 10px; flex-wrap: wrap; }
.tag { font-size: 10px; padding: 3px 7px; border-radius: 5px; background: var(--panel-soft); color: var(--muted); }
.tag.warn { background: var(--warn-soft); color: var(--warn); }
.code-tools { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); }
.modal-mask { position: fixed; inset: 0; background: var(--overlay); display: grid; place-items: center; z-index: 50; padding: 16px; max-width: none; }
.modal { width: min(100%, 500px); padding: 26px; max-height: 90vh; overflow-y: auto; }
.modal.wide { width: min(100%, 800px); }
.modal h3 { margin: 0 0 16px; font-size: 19px; }
.modal p { line-height: 1.7; }
.modal input, .modal textarea { max-width: 100%; min-width: 0; }
.modal > input[type=file] { width: 100%; }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--border); }
.form-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
.form-grid label { font-size: 12px; color: var(--text); margin-top: 8px; }
.form-grid textarea { font-family: ui-monospace, Consolas, monospace; font-size: 12px; resize: vertical; }
.resources, .zip-import { margin-top: 22px; border-top: 1px solid var(--border); padding-top: 18px; }
.res-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.res-head button { flex: none; }
.res-row { display: flex; flex-direction: column; gap: 8px; padding: 12px; border: 1px solid var(--border); border-radius: 7px; margin-bottom: 10px; background: var(--panel-soft); }
.res-row input, .res-row textarea { font-size: 12px; }
.res-row textarea { font-family: ui-monospace, Consolas, monospace; }
.res-actions { display: flex; align-items: center; gap: 8px; }
.res-file { min-width: 0; flex: 1; font-size: 11px; }
.zip-head { margin-bottom: 12px; }
.zip-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.zip-row input[type=file] { width: 100%; font-size: 12px; }
.mcp-row { display: grid; grid-template-columns: 120px minmax(0, 1fr) minmax(0, 1fr) auto auto; align-items: center; gap: 8px; margin-bottom: 10px; padding: 12px; border: 1px solid var(--border); border-radius: 7px; background: var(--panel-soft); }
.mcp-row input { width: 100%; font-size: 12px; padding: 6px 8px; }
.mcp-row input[type=checkbox] { width: auto; }
@media (max-width: 1280px) { .stat { padding: 0 13px; } .stat-hint { display: none; } .catalog-toolbar { flex-wrap: wrap; } .catalog-search { flex: 1; max-width: 300px; } }
@media (max-width: 800px) {
  .stats { grid-template-columns: repeat(4, minmax(0, 1fr)); row-gap: 20px; }
  .stat:nth-child(5) { border-left: 0; }
  .catalog-heading { align-items: flex-start; flex-direction: column; }
  .mcp-row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .mcp-row > input { grid-column: 1 / -1; }
}
@media (max-width: 600px) {
  .page { padding: 18px 14px; }
  .block { padding: 16px; }
  .stats { margin-bottom: 18px; }
  .stat { padding: 0 10px; }
  .stat-value { font-size: 23px; }
  .stat-label { font-size: 10px; }
  .catalog-toolbar { gap: 12px; }
  .catalog-search { max-width: none; width: 100%; }
  .filters { width: 100%; }
  .chip { padding: 6px 10px; }
  .item-head { gap: 8px; }
  .item-head .spacer { display: none; }
  .tool-name { flex-basis: 100%; }
  .tool-name .name { font-size: 12px; }
  .details-toggle { margin-left: auto; }
  .origin { max-width: 100%; }
  .modal { padding: 20px; }
  .res-head { align-items: flex-start; flex-direction: column; }
  .head-sep { display: none; }
  .guide-tabs .spacer { display: none; }
}
</style>
