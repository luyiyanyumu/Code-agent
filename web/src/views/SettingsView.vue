<script setup>
import { computed, defineAsyncComponent, nextTick, onMounted, ref, watch } from 'vue'
import { api, getToken } from '../api.js'
import { useDialog } from '../composables/useDialog.js'
import ExecutionSettings from '../components/settings/ExecutionSettings.vue'
import ToolsView from './ToolsView.vue'
import { usePreferences } from '../lib/preferences.js'
import { SETTINGS_SECTIONS, WORKSPACE_SHORTCUTS, searchSettings } from '../lib/settingsCatalog.js'
import EmptyState from '../components/ui/EmptyState.vue'
import UiIcon from '../components/ui/UiIcon.vue'

const MemoryView = defineAsyncComponent(() => import('./MemoryView.vue'))
const KnowledgeView = defineAsyncComponent(() => import('./KnowledgeView.vue'))
const UsageView = defineAsyncComponent(() => import('./UsageView.vue'))
const AutomationsView = defineAsyncComponent(() => import('./AutomationsView.vue'))
const AuditView = defineAsyncComponent(() => import('./AuditView.vue'))
const resourceViews = { memory: MemoryView, knowledge: KnowledgeView, usage: UsageView, automations: AutomationsView, audit: AuditView }
const resourceSections = Object.keys(resourceViews)

const props = defineProps({
  user: { type: Object, default: null },
  visible: { type: Boolean, default: true },
  initialSection: { type: String, default: 'general' },
  initialToolTab: { type: String, default: 'builtin' },
})
const isAdmin = computed(() => props.user?.role === 'admin')
const emit = defineEmits(['close', 'section-change'])
const { preferences, updatePreference, resetPreferences, storageError } = usePreferences()
const sections = SETTINGS_SECTIONS
const activeSection = ref(sections.some(item => item.id === props.initialSection) ? props.initialSection : 'general')
const extensionsVisited = ref(activeSection.value === 'extensions')
// Load resource screens on first visit and keep their instances while settings are open or hidden.
const visitedResources = ref(new Set(resourceSections.includes(activeSection.value) ? [activeSection.value] : []))
const resourcePanels = {}
const resourceReloads = new Set()
const toolsPanel = ref(null)
const searchQuery = ref('')
const contentPane = ref(null)
const preferenceNotice = ref('')
const searchResults = computed(() => searchSettings(searchQuery.value))
const currentSection = computed(() => sections.find(item => item.id === activeSection.value) || sections[0])
const shortcuts = WORKSPACE_SHORTCUTS
const themeOptions = [{ value: 'light', label: '浅色' }, { value: 'dark', label: '深色' }, { value: 'system', label: '跟随系统' }]
const editorPreview = computed(() => `public class Workspace {\n${' '.repeat(preferences.editorTabSize)}// 按你的习惯阅读代码\n${' '.repeat(preferences.editorTabSize)}String message = "Hello, Code-agent";\n}`)

function setPreference(key, value, event) {
  const saved = updatePreference(key, value)
  preferenceNotice.value = saved ? '偏好已保存' : ''
  if (!saved && event?.target) {
    if (event.target.type === 'checkbox') event.target.checked = preferences[key] === true || preferences[key] === 'on'
    else event.target.value = String(preferences[key])
  }
}
function resetSection(keys) {
  preferenceNotice.value = resetPreferences(keys) ? '本组偏好已恢复默认' : ''
}
function refreshResource(panel) {
  if (!panel || resourceReloads.has(panel)) return
  resourceReloads.add(panel)
  nextTick(async () => {
    try {
      if (props.visible !== false && resourcePanels[activeSection.value] === panel) await panel.reload?.()
    } finally { resourceReloads.delete(panel) }
  })
}
async function selectSection(id, anchor = '') {
  if (!sections.some(item => item.id === id)) return
  // A new async screen owns its first load; refresh only an existing instance.
  const resourcePanel = resourcePanels[id]
  activeSection.value = id
  if (id === 'extensions') extensionsVisited.value = true
  if (resourceSections.includes(id)) visitedResources.value.add(id)
  refreshResource(resourcePanel)
  emit('section-change', id)
  searchQuery.value = ''
  preferenceNotice.value = ''
  await nextTick()
  if (contentPane.value) contentPane.value.scrollTop = 0
  if (id === 'extensions' && anchor.startsWith('extensions-')) {
    toolsPanel.value?.openCategory(anchor.slice('extensions-'.length))
    await nextTick()
  }
  if (id === 'system' && !service.value && !serviceLoading.value) checkService()
  if (anchor) {
    const target = contentPane.value?.querySelector(`[id="${anchor}"]`)
    target?.scrollIntoView?.({ block: 'center' })
    if (target?.matches?.('input, select, button, [tabindex]')) target.focus({ preventScroll: true })
    else target?.querySelector?.('input, select, button')?.focus({ preventScroll: true })
  }
}
watch(() => props.initialSection, section => {
  if (section !== activeSection.value) selectSection(section)
})
watch(() => props.initialToolTab, tab => selectSection('extensions', `extensions-${tab}`))
watch(() => props.visible, (visible, previous) => {
  if (visible && previous === false) refreshResource(resourcePanels[activeSection.value])
})
const service = ref(null)
const serviceLoading = ref(false)
const serviceError = ref('')
const serviceCheckedAt = ref('')
async function checkService() {
  if (serviceLoading.value) return
  serviceLoading.value = true
  serviceError.value = ''
  try { service.value = await api.health(); serviceCheckedAt.value = new Date().toLocaleTimeString() }
  catch (e) { service.value = null; serviceError.value = e.message }
  finally { serviceLoading.value = false }
}


const cfg = ref(null)
const error = ref('')
const notice = ref('')
const saving = ref(false)
const loading = ref(false)
const profileError = ref('')
const formSection = ref(null)
const dialog = useDialog()

const teamConfig = ref(null)
const teamForm = ref({ max_depth: 3, max_parallel: 4 })
const teamLoading = ref(false)
const teamSaving = ref(false)
const teamError = ref('')
const teamNotice = ref('')
const teamChanged = computed(() => teamConfig.value && (
  teamForm.value.max_depth !== teamConfig.value.max_depth ||
  teamForm.value.max_parallel !== teamConfig.value.max_parallel
))

async function loadAgentTeam() {
  if (teamLoading.value || teamSaving.value) return
  teamLoading.value = true
  teamError.value = ''
  try {
    teamConfig.value = await api.settings.agentTeam()
    teamForm.value = { ...teamConfig.value }
  } catch (e) {
    teamError.value = e.message
  } finally { teamLoading.value = false }
}

async function saveAgentTeam() {
  if (teamSaving.value || !isAdmin.value || !teamConfig.value) return
  teamError.value = ''
  teamNotice.value = ''
  const limits = [
    ['max_depth', '最大递归深度', 8],
    ['max_parallel', '并行子智能体上限', 16],
  ]
  for (const [field, label, max] of limits) {
    const value = teamForm.value[field]
    if (!Number.isInteger(value) || value < 1 || value > max) {
      teamError.value = `${label}必须是 1 到 ${max} 的整数`
      return
    }
  }
  teamSaving.value = true
  try {
    teamConfig.value = await api.settings.saveAgentTeam({ ...teamForm.value })
    teamForm.value = { ...teamConfig.value }
    teamNotice.value = '协作设置已保存'
  } catch (e) {
    teamError.value = e.message
  } finally { teamSaving.value = false }
}

const teamBudgetConfig = ref(null)
const teamBudgetForm = ref({ task_tokens: 0, task_seconds: 0, team_tokens: 0, team_seconds: 0 })
const teamBudgetLoading = ref(false)
const teamBudgetSaving = ref(false)
const teamBudgetError = ref('')
const teamBudgetNotice = ref('')
const teamBudgetFields = [['task_tokens', '单次执行 Token 上限', 10000000], ['task_seconds', '单次执行时间上限', 86400], ['team_tokens', '团队总 Token 上限', 100000000], ['team_seconds', '团队总时间上限', 604800]]
const teamBudgetChanged = computed(() => teamBudgetConfig.value && teamBudgetFields.some(([field]) => teamBudgetForm.value[field] !== teamBudgetConfig.value[field]))
async function loadAgentTeamBudgets() {
  if (teamBudgetLoading.value || teamBudgetSaving.value) return
  teamBudgetLoading.value = true
  teamBudgetError.value = ''
  try {
    teamBudgetConfig.value = await api.settings.agentTeamBudgets()
    teamBudgetForm.value = { ...teamBudgetConfig.value }
  } catch (e) { teamBudgetError.value = e.message }
  finally { teamBudgetLoading.value = false }
}
async function saveAgentTeamBudgets() {
  if (teamBudgetSaving.value || teamBudgetLoading.value || !isAdmin.value || !teamBudgetConfig.value) return
  teamBudgetError.value = ''
  teamBudgetNotice.value = ''
  for (const [field, label, max] of teamBudgetFields) {
    const value = teamBudgetForm.value[field]
    if (!Number.isSafeInteger(value) || value < 0 || value > max) {
      teamBudgetError.value = `${label}必须是 0 到 ${max.toLocaleString('zh-CN')} 的整数，0 表示不限制`
      return
    }
  }
  teamBudgetSaving.value = true
  try {
    const payload = Object.fromEntries(teamBudgetFields.map(([field]) => [field, teamBudgetForm.value[field]]))
    teamBudgetConfig.value = await api.settings.saveAgentTeamBudgets(payload)
    teamBudgetForm.value = { ...teamBudgetConfig.value }
    teamBudgetNotice.value = '任务预算已保存'
  } catch (e) { teamBudgetError.value = e.message }
  finally { teamBudgetSaving.value = false }
}

const form = ref({
  provider: 'deepseek',
  base_url: '',
  api_key: '',
  model_default: '',
  proxy_mode: 'system',
  context_window: '',
  max_output_tokens: '',
})

const PROVIDERS = [
  { value: 'deepseek', label: 'DeepSeek', hint: '官方 API，默认 https://api.deepseek.com' },
  { value: 'openai', label: 'OpenAI', hint: '官方 API，默认 https://api.openai.com/v1' },
  { value: 'openai_compat', label: 'OpenAI 兼容', hint: '内网网关 / vLLM / Ollama（/v1）' },
  { value: 'offline', label: '离线 Mock', hint: '无 key 演示 / 回归测试' },
]

const BASE_URL_HINT = {
  deepseek: 'https://api.deepseek.com',
  openai: 'https://api.openai.com/v1',
  openai_compat: 'http://10.0.0.9:8000/v1',
  offline: '',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    cfg.value = await api.modelSettings()
    if (!showProfileForm.value) fillForm(cfg.value)
  } catch (e) {
    error.value = e.message
  } finally { loading.value = false }
}

async function submit() {
  if (saving.value || !isAdmin.value) return
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const payload = { ...form.value }
    payload.context_window = parseCapacity(payload.context_window, '上下文窗口')
    payload.max_output_tokens = parseCapacity(payload.max_output_tokens, '最大输出 Token')
    if (payload.context_window && payload.max_output_tokens && payload.max_output_tokens >= payload.context_window) {
      throw new Error('最大输出 Token 必须小于上下文窗口')
    }
    if (!payload.api_key) delete payload.api_key // 留空 = 不修改
    for (const k of Object.keys(payload)) {
      if (typeof payload[k] === 'string') payload[k] = payload[k].trim()
    }
    // 防止浏览器把用户名自动填进服务地址。
    const url = payload.base_url || ''
    if (url && !/^https?:\/\/\S+$/i.test(url)) {
      error.value = '服务地址请填写 http:// 或 https:// 开头的地址'
      return
    }

    // 档案模式：保存到 providers.json（编辑中的档案若正在生效则自动应用）
    if (showProfileForm.value && (editingPid.value || adding.value)) {
      if (adding.value !== 'preset' && !editingPid.value && !editingName.value.trim()) {
        error.value = '档案名称不能为空'
        return
      }
      const body = { ...payload, name: editingName.value.trim() }
      if (editingPid.value) {
        const view = await profileCall('PUT', `/api/v1/model-profiles/${editingPid.value}`, body)
        notice.value = view.active ? '配置已保存并生效' : '配置已保存，启用后生效'
      } else {
        await profileCall('POST', '/api/v1/model-profiles', body)
        notice.value = '配置已添加，启用后生效'
      }
      showProfileForm.value = false
      editingPid.value = ''
      adding.value = ''
      await Promise.all([loadProfiles(), load()])
      return
    }

    await api.saveModelSettings(payload)
    notice.value = '配置已保存并生效'
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

// ---- 模型配置档案（M14）：多 provider 预设，一键切换 ----
const profiles = ref([])
const editingPid = ref('')      // 正在编辑的档案 id（'' = 编辑当前生效配置 / 新增）
const adding = ref('')          // '' | 'preset' | 'custom'
const editingName = ref('')
const showProfileForm = ref(false)

// ---- 本地 / 内网后端（Ollama、LM Studio、vLLM）：免密钥 + 可用模型探测 ----
const probing = ref(false)
const probed = ref([])
const probeError = ref('')
const formLocal = computed(() => isLocalUrl(form.value.base_url))
let probeGeneration = 0

function probeSignature() {
  return JSON.stringify([editingPid.value, adding.value, form.value.provider, form.value.base_url.trim(), form.value.api_key.trim(), form.value.proxy_mode])
}

/** 与后端 is_local_base_url 同口径的轻量判定（只用于提示，不做安全决策）。 */
function isLocalUrl(u) {
  const m = /^https?:\/\/([^/:?#]+)/i.exec(u || '')
  if (!m) return false
  const h = m[1].toLowerCase()
  if (['localhost', '0.0.0.0', '::1', '[::1]', 'host.docker.internal'].includes(h)) return true
  if (h.startsWith('127.') || h.startsWith('10.') || h.startsWith('192.168.') || h.startsWith('169.254.')) return true
  if (h.endsWith('.local') || h.endsWith('.localhost') || h.endsWith('.internal')) return true
  if (h.startsWith('172.')) {
    const seg = h.split('.')
    return seg.length > 1 && Number(seg[1]) >= 16 && Number(seg[1]) <= 31
  }
  return false
}

/** 拉取 {base_url}/models —— 本地模型的模型名不该靠猜。 */
async function probeModels() {
  if (probing.value || saving.value) return
  probeError.value = ''
  probed.value = []
  const base_url = form.value.base_url.trim() || BASE_URL_HINT[form.value.provider] || ''
  if (!base_url) { probeError.value = '请先填写 Base URL'; return }
  const useCurrent = !showProfileForm.value && form.value.provider === cfg.value?.provider
  const storedKey = editingPid.value ? profiles.value.find(profile => profile.id === editingPid.value)?.api_key_set : useCurrent && cfg.value?.api_key_set
  if (!form.value.api_key.trim() && !isLocalUrl(base_url) && !storedKey) {
    probeError.value = '远程服务需要 API Key，请填写密钥或选择已有密钥的档案'
    return
  }
  probing.value = true
  const generation = ++probeGeneration
  const signature = probeSignature()
  try {
    const r = await profileCall('POST', '/api/v1/model-profiles/probe', {
      base_url,
      api_key: form.value.api_key.trim(),
      proxy_mode: form.value.proxy_mode,
      ...(editingPid.value ? { profile_id: editingPid.value } : useCurrent ? { use_current: true } : {}),
    })
    if (generation !== probeGeneration || signature !== probeSignature()) return
    probed.value = r.models || []
    if (!probed.value.length) probeError.value = '服务有响应但没有模型，先在本地后端拉取/加载一个模型'
  } catch (e) {
    if (generation === probeGeneration && signature === probeSignature()) probeError.value = e.message
  } finally {
    if (generation === probeGeneration) probing.value = false
  }
}

const profilesLoaded = ref(false)
const formSource = computed(() => editingPid.value
  ? profiles.value.find(profile => profile.id === editingPid.value)
  : adding.value ? null : cfg.value)
const storedKey = computed(() => !!formSource.value?.api_key_set)
const modelOptions = computed(() => [...new Set([
  form.value.model_default,
  ...(formSource.value?.models || []).map(item => item.id),
  ...probed.value,
].filter(Boolean))])

function parseCapacity(value, label) {
  if (value == null || String(value).trim() === '') return null
  const text = String(value).trim()
  if (!/^\d+$/.test(text) || !Number.isSafeInteger(Number(text)) || Number(text) < 1 || Number(text) > 10000000) {
    throw new Error(`${label}请输入 1–10,000,000 之间的整数`)
  }
  return Number(text)
}

function tokenCount(value) {
  return value ? Number(value).toLocaleString('en-US') : '自动'
}

// api.js 带着他人未提交改动，这里用局部 fetch 保持隔离
async function profileCall(method, path, body) {
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

async function loadProfiles() {
  profileError.value = ''
  try { profiles.value = await profileCall('GET', '/api/v1/model-profiles') }
  catch (e) { profileError.value = e.message }
  finally { profilesLoaded.value = true }
}

function fillForm(src) {
  probeGeneration++
  probing.value = false
  form.value = {
    provider: src.provider || 'deepseek',
    base_url: src.base_url || '',
    api_key: '', // 永远不回填 key，留空=不修改
    model_default: src.model_default || '',
    proxy_mode: src.proxy_mode || 'system',
    context_window: src.context_window ?? '',
    max_output_tokens: src.max_output_tokens ?? '',
  }
  probed.value = []
  probeError.value = ''
}

function beginEditProfile(p) {
  editingPid.value = p.id
  adding.value = ''
  editingName.value = p.name
  showProfileForm.value = true
  fillForm(p)
  error.value = ''
  notice.value = ''
  setTimeout(() => formSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
}

function beginAddPreset(name) {
  const EMPTY = { model_default: '' }
  const pr = {
    DeepSeek: { provider: 'deepseek', base_url: '', model_default: 'deepseek-chat' },
    Kimi: { provider: 'openai_compat', base_url: 'https://api.moonshot.cn/v1', model_default: 'kimi-k2-turbo-preview' },
    '火山方舟': { provider: 'openai_compat', base_url: 'https://ark.cn-beijing.volces.com/api/v3', model_default: 'deepseek-v4-pro' },
    OpenAI: { provider: 'openai', base_url: 'https://api.openai.com/v1', ...EMPTY },
    // 本地 / 内网后端：无需密钥，模型名建议点「探测模型」从本机拉取后一键填入
    '本地 Ollama': { provider: 'openai_compat', base_url: 'http://127.0.0.1:11434/v1', ...EMPTY },
    '本地 LM Studio': { provider: 'openai_compat', base_url: 'http://127.0.0.1:1234/v1', ...EMPTY },
    '本地 vLLM': { provider: 'openai_compat', base_url: 'http://127.0.0.1:8000/v1', ...EMPTY },
  }[name]
  if (!pr) return
  editingPid.value = ''
  adding.value = 'preset'
  editingName.value = name
  showProfileForm.value = true
  fillForm(pr)
  error.value = ''
  notice.value = ''
  setTimeout(() => formSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
}

function beginAddCustom() {
  editingPid.value = ''
  adding.value = 'custom'
  editingName.value = ''
  showProfileForm.value = true
  fillForm({ provider: 'openai_compat' })
  error.value = ''
  notice.value = ''
  setTimeout(() => formSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
}

function cancelProfileEdit() {
  showProfileForm.value = false
  editingPid.value = ''
  adding.value = ''
  load()
}

async function activateProfile(p) {
  error.value = ''
  notice.value = ''
  try {
    await profileCall('POST', `/api/v1/model-profiles/${p.id}/activate`)
    notice.value = `已切换到「${p.name}」，配置已生效`
    await Promise.all([loadProfiles(), load()])
  } catch (e) { error.value = e.message }
}

async function deleteProfile(p) {
  const { id, name } = p
  return dialog.open({
    title: '删除配置档案',
    description: '将移除此保存的模型连接档案。当前已经生效的连接配置仍会保留。',
    details: name,
    tone: 'danger',
    icon: 'server',
    confirmText: '删除档案',
    pendingText: '正在删除…',
    onConfirm: async () => {
      error.value = ''
      notice.value = ''
      await profileCall('DELETE', `/api/v1/model-profiles/${id}`)
      notice.value = `已删除配置档案「${name}」`
      await loadProfiles()
    },
  })
}

onMounted(load)
onMounted(loadProfiles)
onMounted(loadAgentTeam)
onMounted(loadAgentTeamBudgets)
onMounted(() => { if (activeSection.value === 'system') checkService() })
</script>

<template>
  <div class="page settings-page">
    <header class="settings-header"><div><h1>设置</h1><p>管理工作区偏好、资源与服务。</p></div><div class="settings-header-actions"><span class="access-label"><UiIcon :name="isAdmin ? 'shield' : 'key'" :size="14" />{{ isAdmin ? '管理员' : '服务端配置只读' }}</span><button class="back-to-code" aria-label="返回代码工作区" @click="emit('close')"><UiIcon name="arrow-left" :size="16" /><span>返回代码</span></button></div></header>
    <div class="settings-workspace">
      <aside class="settings-sidebar">
        <div class="settings-search"><UiIcon name="search" :size="16" /><input v-model="searchQuery" type="search" aria-label="搜索设置" placeholder="搜索设置…" @keydown.esc="searchQuery = ''" /></div>
        <nav aria-label="设置分类"><button v-for="section in sections" :key="section.id" class="settings-nav-item" :class="{ selected: activeSection === section.id && !searchQuery.trim() }" :aria-current="activeSection === section.id && !searchQuery.trim() ? 'page' : undefined" @click="selectSection(section.id)"><UiIcon :name="section.icon" :size="17" /><span>{{ section.label }}</span></button></nav>
        <div class="settings-sidebar-note"><span>Code-agent</span><span>你的工作区，你的偏好</span></div>
      </aside>
      <section ref="contentPane" class="settings-content" aria-label="设置内容">
        <div class="settings-content-inner">
          <template v-if="searchQuery.trim()">
            <div class="category-heading"><div><h2>搜索设置</h2><p role="status">找到 {{ searchResults.length }} 项与「{{ searchQuery.trim() }}」相关的设置</p></div></div>
            <div v-if="searchResults.length" class="search-results"><button v-for="item in searchResults" :key="item.section + item.label" class="search-result" @click="selectSection(item.section, item.anchor)"><div><span>{{ sections.find(s => s.id === item.section)?.label }}</span><strong>{{ item.label }}</strong></div><UiIcon name="chevron-right" :size="16" /></button></div>
            <EmptyState v-else title="没有找到相关设置" description="试试“主题”“模型”“权限”或“字体”。" icon="search" />
          </template>
          <div v-show="!searchQuery.trim()">
            <div class="category-heading"><div><h2>{{ currentSection.label }}</h2><p>{{ currentSection.description }}</p></div><span class="scope-label">{{ currentSection.scope }}</span></div>
            <p v-if="storageError" class="feedback error" role="alert">{{ storageError }}</p>
            <p v-if="preferenceNotice" class="preference-notice" role="status"><UiIcon name="check" :size="14" />{{ preferenceNotice }}</p>

            <section v-show="activeSection === 'general'" aria-label="通用设置">
              <div class="setting-group"><h3>启动与代码助手</h3>
                <div id="default-view" class="setting-row"><div><span class="setting-label">起始页面</span><p>启动后直接进入代码工作区。资源与管理功能统一从设置打开。</p></div><span class="subtle-badge">代码工作区</span></div>
                <div class="setting-row"><div><label for="send-key">发送消息的按键</label><p>应用于代码助手。Shift + Enter 始终换行。</p></div><select id="send-key" :value="preferences.sendKey" @change="setPreference('sendKey', $event.target.value, $event)"><option value="enter">Enter 发送</option><option value="ctrl-enter">Ctrl / ⌘ + Enter 发送</option></select></div>
              </div>
              <div class="settings-callout"><UiIcon name="folder" :size="18" /><div><strong>个人偏好与服务配置各自保存</strong><p>外观、编辑器和输入习惯保存在此浏览器。模型、权限和协作配置保存在服务端，可供连接同一服务的工作区使用。</p></div></div>
              <div class="group-actions"><button class="ghost" @click="resetSection(['sendKey'])">恢复本组默认值</button></div>
            </section>

            <section v-show="activeSection === 'appearance'" aria-label="外观设置">
              <div class="setting-group"><h3>主题与体验</h3>
                <div id="theme" class="setting-row"><div><span class="setting-label" id="theme-label">主题</span><p>应用于工作区界面与代码编辑器。</p></div><div class="segmented" role="group" aria-labelledby="theme-label"><button v-for="option in themeOptions" :key="option.value" :aria-pressed="preferences.theme === option.value" @click="setPreference('theme', option.value)">{{ option.label }}</button></div></div>
                <div class="setting-row"><div><label for="reduced-motion">减少动态效果</label><p>减少界面动画和过渡，保留必要的状态反馈。</p></div><input id="reduced-motion" class="toggle" type="checkbox" role="switch" :checked="preferences.reducedMotion" @change="setPreference('reducedMotion', $event.target.checked, $event)" /></div>
              </div>
              <div class="appearance-sample"><div class="sample-heading"><UiIcon name="code" :size="16" /><span>界面预览</span><span class="sample-status">已就绪</span></div><div class="sample-content"><strong>专注你的下一步</strong><p>清晰的层级、柔和的边界，以及适合长时间工作的配色。</p><div><span class="sample-tag">工作区</span><span class="sample-tag">Code-agent</span></div></div></div>
              <div class="group-actions"><button class="ghost" @click="resetSection(['theme', 'reducedMotion'])">恢复本组默认值</button></div>
            </section>

            <section v-show="activeSection === 'editor'" aria-label="编辑器设置">
              <div class="setting-group"><h3>文字与排版</h3>
                <div class="setting-row"><div><label for="editor-font-size">代码字号</label><p>调整代码编辑区文字大小，范围 12–24 px。</p></div><select id="editor-font-size" :value="preferences.editorFontSize" @change="setPreference('editorFontSize', Number($event.target.value), $event)"><option v-for="size in 13" :key="size" :value="size + 11">{{ size + 11 }} px</option></select></div>
                <div class="setting-row"><div><label for="editor-tab-size">缩进宽度</label><p>使用空格缩进；改变显示与后续输入，不会重写已有文件。</p></div><select id="editor-tab-size" :value="preferences.editorTabSize" @change="setPreference('editorTabSize', Number($event.target.value), $event)"><option :value="2">2 个空格</option><option :value="4">4 个空格</option><option :value="8">8 个空格</option></select></div>
                <div class="setting-row"><div><label for="editor-word-wrap">自动换行</label><p>让长代码行适应编辑器宽度。</p></div><input id="editor-word-wrap" type="checkbox" class="toggle" role="switch" :checked="preferences.editorWordWrap === 'on'" @change="setPreference('editorWordWrap', $event.target.checked ? 'on' : 'off', $event)" /></div>
              </div>
              <div class="setting-group"><h3>辅助显示</h3>
                <div class="setting-row"><div><label for="editor-line-numbers">行号</label><p>显示绝对行号、相对行号，或隐藏行号。</p></div><select id="editor-line-numbers" :value="preferences.editorLineNumbers" @change="setPreference('editorLineNumbers', $event.target.value, $event)"><option value="on">显示行号</option><option value="relative">相对行号</option><option value="off">隐藏行号</option></select></div>
                <div class="setting-row"><div><label for="editor-minimap">代码缩略图</label><p>在编辑区右侧显示整个文件的概览。</p></div><input id="editor-minimap" type="checkbox" class="toggle" role="switch" :checked="preferences.editorMinimap" @change="setPreference('editorMinimap', $event.target.checked, $event)" /></div>
                <div class="setting-row"><div><label for="editor-whitespace">空白字符</label><p>显示空格和制表符，方便检查缩进。</p></div><select id="editor-whitespace" :value="preferences.editorWhitespace" @change="setPreference('editorWhitespace', $event.target.value, $event)"><option value="none">隐藏</option><option value="boundary">仅边界</option><option value="all">全部显示</option></select></div>
              </div>
              <div class="code-preview"><div><UiIcon name="file-code" :size="15" />Workspace.java <span>字号与缩进预览</span></div><pre :style="{ fontSize: preferences.editorFontSize + 'px', whiteSpace: preferences.editorWordWrap === 'on' ? 'pre-wrap' : 'pre' }">{{ editorPreview }}</pre></div>
              <div class="group-actions"><button class="ghost" @click="resetSection(['editorFontSize', 'editorTabSize', 'editorWordWrap', 'editorMinimap', 'editorLineNumbers', 'editorWhitespace'])">恢复编辑器默认值</button></div>
            </section>

            <section v-show="activeSection === 'models'" aria-label="模型与连接设置">
              <div v-if="error" class="feedback error" role="alert"><UiIcon name="alert" :size="16" /><span>{{ error }}</span><button v-if="!cfg" :disabled="loading" @click="load">重试</button></div>
              <div v-if="notice" class="feedback success" role="status"><UiIcon name="check" :size="16" /><span>{{ notice }}</span></div>
      <section class="panel active-panel" aria-label="当前生效模型">
        <template v-if="cfg">
          <div class="active-model"><span class="section-icon"><UiIcon name="server" :size="18" /></span><div><span class="eyebrow">当前模型</span><strong :title="cfg.active_model || cfg.model_default">{{ cfg.active_model || cfg.model_default || '未设置' }}</strong></div><span class="status-chip">生效中</span></div>
          <div class="active-capacity"><div><span>上下文窗口</span><strong>{{ tokenCount(cfg.effective_context_window ?? cfg.context_window) }}</strong></div><div><span>最大输出 Token</span><strong>{{ tokenCount(cfg.effective_max_output_tokens ?? cfg.max_output_tokens) }}</strong></div></div>
        </template>
        <div v-else class="loading-state" role="status">{{ loading ? '正在加载配置…' : '当前配置暂时不可用' }}</div>
      </section>
      <div class="settings-layout">
        <section id="saved-profiles" class="panel profiles-panel">
          <div class="section-head"><h3>已保存配置 <span class="count">{{ profiles.length }}</span></h3><button v-if="isAdmin" class="mini" :disabled="saving" @click="beginAddCustom"><UiIcon name="plus" :size="14" />添加</button></div>
          <div v-if="profileError" class="profile-error" role="alert"><span>{{ profileError }}</span><button @click="loadProfiles">重试</button></div>
          <div v-if="!profilesLoaded" class="loading-state" role="status">正在加载配置…</div>
          <div v-else-if="profiles.length" class="profile-list">
            <article v-for="p in profiles" :key="p.id" class="profile-card" :class="{ active: p.active, editing: editingPid === p.id }">
              <div class="profile-heading"><span class="provider-icon"><UiIcon name="server" :size="16" /></span><div class="p-main"><div class="p-title"><span class="p-name">{{ p.name }}</span><span v-if="p.active" class="p-badge">生效中</span><span v-if="p.local" class="p-badge local">本地</span></div><span class="p-model" :title="p.model_default || p.provider">{{ p.model_default || p.provider }}</span></div></div>
              <div class="profile-meta"><span><span class="p-dot" :class="(p.api_key_set || p.local) ? 'ok' : 'warn'"></span>{{ p.local ? '本地服务' : p.api_key_set ? '密钥已配置' : '未配置密钥' }}</span><span>{{ p.proxy_mode === 'direct' ? '直接连接' : '系统代理' }}</span></div>
              <div v-if="isAdmin" class="p-actions"><button class="mini" :disabled="saving" @click="beginEditProfile(p)">编辑</button><button v-if="!p.active" class="mini go" :disabled="saving" @click="activateProfile(p)">启用</button><button class="mini del" :disabled="saving" @click="deleteProfile(p)">删除</button></div>
            </article>
          </div>
          <EmptyState v-else-if="!profileError" title="暂无保存的配置" icon="server" />
          <div v-if="isAdmin" class="preset-section"><h4>快速添加</h4><div class="add-row"><button v-for="name in ['DeepSeek', 'Kimi', '火山方舟', 'OpenAI']" :key="name" class="add-btn" :disabled="saving" @click="beginAddPreset(name)">{{ name }}</button></div><div class="add-row"><button v-for="name in ['本地 Ollama', '本地 LM Studio', '本地 vLLM']" :key="name" class="add-btn" :disabled="saving" @click="beginAddPreset(name)">{{ name.replace('本地 ', '') }}</button></div></div>
        </section>
        <section ref="formSection" class="panel connection-panel">
          <div class="section-head"><h3>{{ showProfileForm ? (editingPid ? '编辑配置' : '添加配置') : '当前配置' }}</h3><span class="section-note">{{ isAdmin ? '一个配置 · 一个模型' : '只读 · 仅管理员可修改' }}</span></div>
          <form class="form" novalidate @submit.prevent="submit">
            <fieldset :disabled="saving || !isAdmin" class="form-fields">
              <div v-if="showProfileForm" class="field"><label for="profile-name">配置名称</label><input id="profile-name" v-model="editingName" name="code-agent-profile-name" autocomplete="off" :disabled="adding === 'preset'" placeholder="例如：本地开发" /></div>
              <div class="autofill-decoy" aria-hidden="true"><input type="text" name="decoy-username" autocomplete="username" tabindex="-1" /><input type="password" name="decoy-password" autocomplete="current-password" tabindex="-1" /></div>
              <div class="field"><label for="model-provider">服务提供方</label><select id="model-provider" v-model="form.provider"><option v-for="p in PROVIDERS" :key="p.value" :value="p.value">{{ p.label }}</option></select></div>
              <div class="field"><label for="provider-base-url">服务地址 <span>Base URL</span></label><input id="provider-base-url" v-model="form.base_url" name="code-agent-base-url" autocomplete="off" autocapitalize="off" spellcheck="false" :placeholder="BASE_URL_HINT[form.provider] || '默认地址'" /></div>
              <div class="field"><label for="provider-api-key">API Key <span v-if="storedKey" class="key-status">已配置</span></label><input id="provider-api-key" v-model="form.api_key" type="password" name="code-agent-api-key" autocomplete="new-password" :placeholder="formLocal ? '本地服务可留空' : storedKey ? '留空保留已有密钥' : '输入服务密钥'" /></div>
              <div class="model-section">
                <div class="field"><label for="default-model">模型 <span>Model ID</span></label><div class="model-row"><input id="default-model" v-model="form.model_default" list="available-models" name="code-agent-model-default" autocomplete="off" spellcheck="false" placeholder="输入或选择一个模型" /><button class="mini" type="button" :disabled="probing || form.provider === 'offline'" @click="probeModels"><UiIcon name="refresh" :size="14" />{{ probing ? '获取中…' : '获取模型' }}</button></div><datalist id="available-models"><option v-for="m in modelOptions" :key="m" :value="m" /></datalist><p v-if="probeError" class="err" role="alert">{{ probeError }}</p><p v-else-if="probed.length" class="hint" role="status">已获取 {{ probed.length }} 个模型，可在输入框中选择</p></div>
                <div class="capacity-grid">
                  <div class="field"><label for="context-window">上下文窗口 <span>Token</span></label><input id="context-window" v-model="form.context_window" name="context_window" type="text" inputmode="numeric" autocomplete="off" placeholder="自动" aria-describedby="capacity-hint" /></div>
                  <div class="field"><label for="max-output-tokens">最大输出 Token</label><input id="max-output-tokens" v-model="form.max_output_tokens" name="max_output_tokens" type="text" inputmode="numeric" autocomplete="off" placeholder="自动" aria-describedby="capacity-hint" /></div>
                </div>
                <p id="capacity-hint" class="hint">留空自动设置，输出上限需小于上下文窗口。</p>
              </div>
              <div class="field network-field"><label for="provider-proxy-mode">网络代理</label><select id="provider-proxy-mode" v-model="form.proxy_mode" aria-label="网络代理"><option value="system">跟随系统</option><option value="direct">直接连接</option></select></div>
            </fieldset>
            <div v-if="isAdmin" class="actions"><button v-if="showProfileForm" class="ghost" type="button" :disabled="saving" @click="cancelProfileEdit">取消</button><button class="primary" type="submit" :disabled="saving || probing || (!cfg && !showProfileForm)">{{ saving ? '保存中…' : (showProfileForm ? '保存配置' : '保存并生效') }}</button></div>
          </form>
        </section>
      </div>

            </section>
            <section v-show="activeSection === 'agents'" aria-label="智能体协作设置">
      <section class="panel team-panel" aria-labelledby="agent-team-title">
        <div class="section-head"><h3 id="agent-team-title">智能体协作</h3><span class="section-note">主对话协调 · 后台子任务协作</span></div>
        <div v-if="teamLoading" class="loading-state" role="status">正在加载协作设置…</div>
        <form v-else-if="teamConfig" class="form team-form" novalidate @submit.prevent="saveAgentTeam">
          <fieldset class="form-fields" :disabled="teamSaving || !isAdmin">
            <div class="capacity-grid">
              <div class="field"><label for="agent-team-depth">最大递归深度</label><input id="agent-team-depth" v-model.number="teamForm.max_depth" type="number" min="1" max="8" step="1" inputmode="numeric" aria-describedby="agent-team-depth-hint" @input="teamNotice = ''" /><p id="agent-team-depth-hint" class="hint">Lead 为第 0 层，节点智能体为第 1 层。范围 1–8。</p></div>
              <div class="field"><label for="agent-team-parallel">并行子智能体上限</label><input id="agent-team-parallel" v-model.number="teamForm.max_parallel" type="number" min="1" max="16" step="1" inputmode="numeric" aria-describedby="agent-team-parallel-hint" @input="teamNotice = ''" /><p id="agent-team-parallel-hint" class="hint">同一项目的所有层级共享并发上限。范围 1–16。</p></div>
            </div>
          </fieldset>
          <div class="team-footer"><p v-if="!isAdmin" class="hint">仅管理员可修改协作设置。</p><p v-else class="hint">Lead 负责读取核查、任务委派与验收。新调度采用保存后的限制；降低并发不会中断正在执行的任务。</p><button v-if="isAdmin" class="primary" type="submit" :disabled="teamSaving || !teamChanged">{{ teamSaving ? '保存中…' : '保存协作设置' }}</button></div>
          <p v-if="teamNotice" class="team-success" role="status">{{ teamNotice }}</p>
        </form>
        <div v-if="teamError" class="team-error" role="alert"><span>{{ teamError }}</span><button v-if="!teamConfig" class="mini" :disabled="teamLoading" @click="loadAgentTeam">重试</button></div>
      </section>
      <section class="panel team-panel" aria-labelledby="agent-budget-title">
        <div class="section-head"><h3 id="agent-budget-title">任务与团队预算</h3><span class="section-note">高级设置 · 0 表示不限制</span></div>
        <div v-if="teamBudgetLoading" class="loading-state" role="status">正在加载任务预算…</div>
        <form v-else-if="teamBudgetConfig" class="form team-form" novalidate @submit.prevent="saveAgentTeamBudgets">
          <fieldset class="form-fields" :disabled="teamBudgetSaving || !isAdmin">
            <div class="capacity-grid">
              <div class="field"><label for="agent-budget-task-tokens">单次执行 Token 上限</label><input id="agent-budget-task-tokens" v-model.number="teamBudgetForm.task_tokens" type="number" min="0" max="10000000" step="1" inputmode="numeric" aria-describedby="agent-budget-task-tokens-hint" @input="teamBudgetNotice = ''" /><p id="agent-budget-task-tokens-hint" class="hint">每个智能体的每次执行独立计数；委派或继续派工会开启新的计数。</p></div>
              <div class="field"><label for="agent-budget-task-seconds">单次执行时间上限（秒）</label><input id="agent-budget-task-seconds" v-model.number="teamBudgetForm.task_seconds" type="number" min="0" max="86400" step="1" inputmode="numeric" aria-describedby="agent-budget-task-seconds-hint" @input="teamBudgetNotice = ''" /><p id="agent-budget-task-seconds-hint" class="hint">按实际经过时间计入，包括排队、审批和等待子任务。</p></div>
              <div class="field"><label for="agent-budget-team-tokens">团队总 Token 上限</label><input id="agent-budget-team-tokens" v-model.number="teamBudgetForm.team_tokens" type="number" min="0" max="100000000" step="1" inputmode="numeric" aria-describedby="agent-budget-team-tokens-hint" @input="teamBudgetNotice = ''" /><p id="agent-budget-team-tokens-hint" class="hint">Lead 的本次用户请求、所有递归子任务和自动续跑共享计数。</p></div>
              <div class="field"><label for="agent-budget-team-seconds">团队总时间上限（秒）</label><input id="agent-budget-team-seconds" v-model.number="teamBudgetForm.team_seconds" type="number" min="0" max="604800" step="1" inputmode="numeric" aria-describedby="agent-budget-team-seconds-hint" @input="teamBudgetNotice = ''" /><p id="agent-budget-team-seconds-hint" class="hint">从 Lead 本次用户请求开始计时；下一条明确的用户请求开启新预算。</p></div>
            </div>
            <p class="hint">Token 累计输入与输出。达到上限后不再发起下一次模型请求，正在进行的模型请求可能超过上限。默认均为 0，不限制。</p>
          </fieldset>
          <div class="team-footer"><p class="hint">{{ isAdmin ? '预算单独保存，供后续任务采用；具体委派还可以设置单次执行上限。' : '仅管理员可修改任务预算。' }}</p><button v-if="isAdmin" class="primary" type="submit" :disabled="teamBudgetSaving || !teamBudgetChanged">{{ teamBudgetSaving ? '保存中…' : '保存任务预算' }}</button></div>
          <p v-if="teamBudgetNotice" class="team-success" role="status">{{ teamBudgetNotice }}</p>
        </form>
        <div v-if="teamBudgetError" class="team-error" role="alert"><span>{{ teamBudgetError }}</span><button v-if="!teamBudgetConfig" class="mini" :disabled="teamBudgetLoading" @click="loadAgentTeamBudgets">重试</button></div>
      </section>
              <div class="settings-callout"><UiIcon name="git-branch" :size="18" /><div><strong>让任务规模与机器能力相匹配</strong><p>更高并发会增加模型请求与本机资源占用。深度控制任务可以继续拆分的层数，并不代表同时运行数量。</p></div></div>
            </section>
            <section v-show="activeSection === 'permissions'" id="execution-settings" aria-label="执行与权限设置"><ExecutionSettings :user="user" /></section>
            <section v-show="activeSection === 'extensions'" aria-label="工具与扩展设置">
              <ToolsView v-if="extensionsVisited" ref="toolsPanel" embedded :initial-tab="initialToolTab" />
              <p class="section-footnote">工具是否开放给某个智能体，还取决于角色和会话权限。</p>
            </section>
            <section v-for="resourceSection in resourceSections" :id="`${resourceSection}-settings`" :key="resourceSection" v-show="activeSection === resourceSection" class="settings-resource" :aria-label="sections.find(section => section.id === resourceSection)?.label" tabindex="-1">
              <component :is="resourceViews[resourceSection]" v-if="visitedResources.has(resourceSection)" :ref="panel => { resourcePanels[resourceSection] = panel }" :user="user" />
            </section>
            <section v-show="activeSection === 'shortcuts'" aria-label="快捷键列表">
              <div id="shortcut-list" class="setting-group shortcut-list"><div v-for="shortcut in shortcuts" :key="shortcut.label" class="shortcut-row"><div><strong>{{ shortcut.label }}</strong><span>{{ shortcut.scope }}</span></div><kbd>{{ shortcut.keys }}</kbd></div><div class="shortcut-row"><div><strong>发送消息</strong><span>可在通用设置中修改</span></div><kbd>{{ preferences.sendKey === 'enter' ? 'Enter' : 'Ctrl / ⌘ + Enter' }}</kbd></div></div>
              <p class="section-footnote">以上为 Windows 快捷键。编辑器中的常见 Ctrl 组合键在 macOS 上使用 ⌘；运行和调试需要当前文件与运行环境支持。</p>
            </section>
            <section v-show="activeSection === 'system'" aria-label="关于与诊断设置">
              <div id="service-status" class="setting-group"><h3>服务连接</h3><div class="setting-row"><div><span class="setting-label">后端服务</span><p v-if="serviceError" class="err" role="alert">{{ serviceError }}</p><p v-else>{{ service?.ok ? '服务已连接 · v' + service.version : '尚未检查' }}<span v-if="serviceCheckedAt"> · 最近检查 {{ serviceCheckedAt }}</span></p></div><button :disabled="serviceLoading" @click="checkService"><UiIcon name="refresh" :size="14" />{{ serviceLoading ? '检查中…' : '检查连接' }}</button></div></div>
              <div id="settings-storage" class="setting-group"><h3>设置与数据</h3><div class="setting-row"><div><span class="setting-label">此浏览器的偏好</span><p>主题、输入方式与编辑器显示自动保存。清除浏览器站点数据后，这些偏好会恢复默认。</p></div><span class="subtle-badge">本地存储</span></div><div class="setting-row"><div><span class="setting-label">模型与服务配置</span><p>模型档案、密钥、权限和团队参数由后端保存。页面不会回填已保存的 API 密钥。</p></div><span class="subtle-badge">服务端</span></div></div>
              <div class="diagnostic-links"><button @click="selectSection('usage', 'usage-settings')"><UiIcon name="usage" :size="16" />查看模型用量</button><button v-if="isAdmin" @click="selectSection('audit', 'audit-settings')"><UiIcon name="audit" :size="16" />查看执行审计</button></div>
            </section>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.page { min-width: 0; overflow-y: auto; padding: 24px; background: var(--bg, #f4f6f9); }
.page-content { max-width: 1100px; margin: 0 auto; }
.panel { min-width: 0; border: 1px solid var(--border); border-radius: 10px; background: var(--panel, #fff); overflow: hidden; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
.access-label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; }
.feedback { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border: 1px solid; border-radius: 8px; margin-bottom: 16px; font-size: 12px; }
.feedback svg { flex: none; }.feedback span { flex: 1; overflow-wrap: anywhere; }
.feedback.error { background: var(--err-soft); color: var(--err); border-color: var(--border); }.feedback.success { background: var(--ok-soft); color: var(--ok); border-color: var(--border); }
.err { color: var(--err); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; margin: 0; }
.active-panel { display: flex; align-items: center; gap: 24px; padding: 16px 20px; margin-bottom: 18px; }
.active-model { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
.active-model > div { display: flex; flex-direction: column; gap: 4px; min-width: 0; }.active-model strong { font-size: 14px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.eyebrow { font-size: 11px; color: var(--muted); }
.section-icon { height: 36px; width: 36px; display: grid; place-items: center; color: var(--accent); background: var(--accent-soft); border-radius: 9px; flex: none; }
.status-chip { color: var(--ok); background: var(--ok-soft); font-size: 10px; border-radius: 5px; padding: 4px 7px; flex: none; }
.active-capacity { display: flex; gap: 32px; padding-left: 24px; border-left: 1px solid var(--border); flex: none; }
.active-capacity > div { display: flex; flex-direction: column; gap: 5px; }.active-capacity span { color: var(--muted); font-size: 11px; }.active-capacity strong { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }
.settings-layout { display: grid; grid-template-columns: minmax(260px, .8fr) minmax(0, 1.2fr); gap: 18px; align-items: start; }
.team-panel { margin-top: 18px; }.team-form { max-width: 760px; }.team-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 18px; }.team-footer button { min-height: 32px; font-size: 12px; padding: 6px 12px; flex: none; }.team-error { display: flex; align-items: center; gap: 10px; margin: 0; padding: 0 18px 16px; color: var(--err); font-size: 12px; }.team-error span { flex: 1; overflow-wrap: anywhere; }.team-success { margin: 10px 0 0; color: var(--ok); font-size: 12px; }
.section-head { padding: 15px 18px; display: flex; align-items: center; justify-content: space-between; gap: 10px; border-bottom: 1px solid var(--border); }.section-head h3 { margin: 0; font-size: 13px; line-height: 1.5; font-weight: 600; }.section-note { font-size: 11px; color: var(--muted); }
.count { margin-left: 4px; padding: 2px 6px; font-size: 10px; font-weight: 500; background: var(--panel-soft); color: var(--muted); border-radius: 4px; }
.profile-list { display: flex; flex-direction: column; gap: 10px; padding: 16px; }.profile-card { border: 1px solid var(--border); border-radius: 8px; padding: 12px; min-width: 0; }.profile-card.active { border-color: var(--accent-border); background: var(--panel-soft); }.profile-card.editing { box-shadow: 0 0 0 1px var(--accent-border); }
.profile-heading { display: flex; align-items: center; gap: 9px; }.provider-icon { width: 30px; height: 30px; border: 1px solid var(--border); background: var(--panel); border-radius: 7px; color: var(--muted); display: grid; place-items: center; flex: none; }.p-main { min-width: 0; flex: 1; }.p-title { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }.p-name { font-weight: 600; font-size: 12px; overflow-wrap: anywhere; }.p-model { display: block; font-size: 11px; color: var(--muted); margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.p-badge { padding: 2px 5px; border-radius: 4px; font-size: 10px; background: var(--accent-soft); color: var(--accent); }.p-badge.local { color: var(--ok); background: var(--ok-soft); }
.profile-meta { display: flex; flex-wrap: wrap; gap: 6px 12px; margin: 11px 0 9px; color: var(--muted); font-size: 10px; }.profile-meta > span { display: inline-flex; align-items: center; gap: 5px; }.p-dot { height: 5px; width: 5px; border-radius: 50%; flex: none; }.p-dot.ok { background: #20a16d; }.p-dot.warn { background: #dba43e; }.p-actions { display: flex; gap: 6px; }.p-actions .del { margin-left: auto; }
.mini { min-height: 28px; padding: 4px 9px; font-size: 11px; white-space: nowrap; }.mini.go { color: var(--accent); background: var(--accent-soft); border-color: var(--accent-border); }.mini.del { color: var(--muted); border-color: transparent; background: transparent; }.mini.del:hover { color: var(--err); background: var(--err-soft); }
.preset-section { border-top: 1px solid var(--border); padding: 16px; background: var(--panel-soft); }.preset-section h4 { margin: 0 0 10px; font-size: 11px; font-weight: 500; color: var(--muted); }.add-row { display: flex; flex-wrap: wrap; gap: 6px; }.add-row + .add-row { margin-top: 7px; }.add-btn { min-height: 28px; padding: 4px 8px; font-size: 11px; background: var(--panel); color: var(--text-2); }.add-btn:hover { color: var(--accent); border-color: var(--accent-border); }
.form { padding: 18px; }.form-fields { border: 0; margin: 0; padding: 0; min-width: 0; display: flex; flex-direction: column; gap: 15px; }.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }.field label { color: var(--text-2); font-size: 12px; font-weight: 500; }.field label > span { margin-left: 4px; color: var(--muted); font-size: 10px; font-weight: 400; }.field label > .key-status { color: var(--ok); }
.form input,.form select { width: 100%; min-width: 0; font-size: 12px; min-height: 34px; padding: 7px 10px; }.hint { margin: 0; color: var(--muted); font-size: 10px; line-height: 1.6; overflow-wrap: anywhere; }.model-section { display: flex; flex-direction: column; gap: 13px; padding-top: 15px; border-top: 1px solid var(--border); }.model-row { display: flex; align-items: center; gap: 7px; }.model-row input { flex: 1; }.model-row .mini { flex: none; min-height: 34px; }.capacity-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }.capacity-grid input { font-variant-numeric: tabular-nums; }
.network-field { padding-top: 15px; border-top: 1px solid var(--border); }.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }.actions button { min-height: 32px; font-size: 12px; padding: 6px 12px; }.autofill-decoy { height: 0; overflow: hidden; margin: -7.5px 0; }.autofill-decoy input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }.loading-state { padding: 25px 16px; text-align: center; color: var(--muted); font-size: 12px; }.profile-error { padding: 16px; display: flex; align-items: center; gap: 8px; color: var(--err); font-size: 12px; }.profile-error span { flex: 1; overflow-wrap: anywhere; }
@media (max-width: 900px) { .settings-layout { grid-template-columns: minmax(0, 1fr); }.connection-panel { scroll-margin-top: 16px; }.active-panel { gap: 16px; }.active-capacity { gap: 18px; padding-left: 16px; } }
@media (max-width: 600px) { .page { padding: 18px 14px; }.active-panel { padding: 14px; flex-wrap: wrap; gap: 14px; }.active-model { flex-basis: 100%; }.active-capacity { border-left: 0; border-top: 1px solid var(--border); padding: 12px 0 0; flex: 1; justify-content: space-between; gap: 14px; }.section-head { padding: 14px; }.profile-list,.preset-section,.form { padding: 14px; }.section-note { font-size: 10px; }.access-label { font-size: 10px; } }
@media (max-width: 380px) { .capacity-grid { grid-template-columns: minmax(0, 1fr); }.section-note { display: none; } }

.settings-page { padding: 0; height: 100%; min-height: 0; display: flex; flex-direction: column; overflow: hidden; background: var(--panel); }
.settings-header { flex: none; min-height: 96px; padding: 22px 32px; display: flex; align-items: center; justify-content: space-between; gap: 20px; border-bottom: 1px solid var(--border); }
.settings-header h1 { margin: 0; font-size: 24px; line-height: 1.4; letter-spacing: -.5px; }
.settings-header p { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
.settings-header-actions { display: flex; align-items: center; gap: 18px; }
.back-to-code { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
.settings-workspace { flex: 1; min-height: 0; display: grid; grid-template-columns: 210px minmax(0, 1fr); }
.settings-sidebar { background: var(--panel-soft); border-right: 1px solid var(--border); padding: 22px 14px 18px; display: flex; flex-direction: column; min-height: 0; overflow-y: auto; }
.settings-search { display: flex; align-items: center; gap: 7px; margin: 0 2px 23px; padding: 0 10px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--panel); color: var(--muted); }
.settings-search:focus-within { border-color: var(--accent); box-shadow: var(--focus-ring); }
.settings-search input { min-width: 0; width: 100%; border: 0; padding: 7px 0; background: transparent; font-size: 12px; box-shadow: none; }
.settings-sidebar nav { display: flex; flex-direction: column; gap: 5px; }
.settings-nav-item { justify-content: flex-start; gap: 11px; min-height: 40px; border: 1px solid transparent; border-radius: 7px; background: transparent; color: var(--text-2); font-size: 13px; padding: 0 11px; }
.settings-nav-item.selected { background: var(--panel); color: var(--accent); border-color: var(--border); font-weight: 600; box-shadow: var(--shadow-sm); }
.settings-sidebar-note { margin-top: auto; padding: 32px 11px 0; display: flex; flex-direction: column; gap: 3px; color: var(--faint); font-size: 10px; }
.settings-sidebar-note span:first-child { color: var(--muted); font-size: 12px; font-weight: 600; }
.settings-content { min-width: 0; min-height: 0; overflow-y: auto; padding: 32px 36px 48px; }
.settings-content-inner { max-width: 980px; margin: 0 auto; }
.settings-resource { min-width: 0; outline: none; }
.settings-resource :deep(.page) { padding: 0; margin: 0; min-width: 0; max-width: none; height: auto; flex: initial; overflow: visible; background: transparent; }
.settings-resource :deep(.page-content) { max-width: none; }
.settings-resource :deep(.page-header) { justify-content: flex-end; margin-bottom: 20px; }
.settings-resource :deep(.page-header-copy) { display: none; }
.category-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 29px; }
.category-heading h2 { margin: 0; font-size: 21px; font-weight: 600; letter-spacing: -.3px; }
.category-heading p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.7; }
.scope-label { margin-top: 5px; padding: 3px 8px; border: 1px solid var(--border); border-radius: 5px; font-size: 10px; color: var(--muted); white-space: nowrap; }
.setting-group { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 22px; background: var(--panel); }
.setting-group > h3 { margin: 0; padding: 13px 20px; background: var(--panel-soft); border-bottom: 1px solid var(--border); color: var(--text-2); font-size: 12px; font-weight: 600; }
.setting-row { display: flex; align-items: center; justify-content: space-between; gap: 28px; padding: 21px 20px; min-width: 0; }
.setting-row + .setting-row { border-top: 1px solid var(--border); }
.setting-row > div:first-child { flex: 1; min-width: 0; }
.setting-row label, .setting-label { color: var(--text); font-size: 13px; font-weight: 500; }
.setting-row p { margin: 5px 0 0; color: var(--muted); font-size: 12px; line-height: 1.65; max-width: 520px; }
.setting-row select { width: 186px; max-width: 100%; flex: none; font-size: 12px; }
.toggle { appearance: none; width: 36px; min-width: 36px; height: 21px; min-height: 21px; border-radius: 20px; padding: 2px; border: 0; background: var(--border-strong); cursor: pointer; flex: none; position: relative; }
.toggle::after { content: ''; position: absolute; top: 3px; left: 3px; width: 15px; height: 15px; border-radius: 50%; background: white; box-shadow: 0 1px 3px #0002; transition: transform .15s ease; }
.toggle:checked { background: var(--accent); }.toggle:checked::after { transform: translateX(15px); }.toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.segmented { flex: none; display: flex; border: 1px solid var(--border); background: var(--panel-soft); border-radius: 7px; padding: 3px; gap: 3px; }
.segmented button { min-height: 29px; padding: 0 11px; border: 1px solid transparent; background: transparent; font-size: 12px; color: var(--muted); }
.segmented button[aria-pressed=true] { background: var(--panel); border-color: var(--border); box-shadow: var(--shadow-sm); color: var(--text); }
.settings-callout { display: flex; align-items: flex-start; gap: 12px; padding: 16px 18px; border: 1px solid var(--border); border-radius: 9px; background: var(--panel-soft); color: var(--muted); margin: 22px 0; }
.settings-callout > svg { margin-top: 2px; }.settings-callout strong { font-size: 12px; font-weight: 500; color: var(--text-2); }.settings-callout p { margin: 5px 0 0; font-size: 12px; line-height: 1.8; }
.group-actions { display: flex; justify-content: flex-end; margin-top: 16px; }.group-actions button { font-size: 11px; }
.preference-notice { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ok); margin: -12px 0 18px; }
.appearance-sample { max-width: 540px; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--bg); }
.sample-heading { display: flex; align-items: center; gap: 8px; border-bottom: 1px solid var(--border); padding: 12px 17px; font-size: 12px; color: var(--text-2); background: var(--panel-soft); }.sample-status { margin-left: auto; color: var(--ok); font-size: 10px; }.sample-content { padding: 24px; }.sample-content strong { font-size: 16px; font-weight: 600; }.sample-content p { color: var(--muted); font-size: 12px; margin: 8px 0 18px; }.sample-tag { display: inline-block; padding: 3px 9px; margin-right: 6px; border-radius: 5px; background: var(--accent-soft); color: var(--accent); font-size: 11px; }
.code-preview { border: 1px solid var(--border); border-radius: 9px; overflow: hidden; }.code-preview > div { display: flex; gap: 7px; align-items: center; font-size: 12px; padding: 10px 16px; background: var(--panel-soft); color: var(--text-2); border-bottom: 1px solid var(--border); }.code-preview > div span { color: var(--muted); font-size: 10px; margin-left: auto; }.code-preview pre { margin: 0; padding: 20px; line-height: 1.7; overflow-x: auto; color: var(--text); }
.search-results { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }.search-result { width: 100%; border: 0; border-bottom: 1px solid var(--border); border-radius: 0; background: var(--panel); justify-content: space-between; text-align: left; padding: 16px 20px; }.search-result:last-child { border-bottom: 0; }.search-result div { display: flex; flex-direction: column; gap: 4px; }.search-result span { color: var(--muted); font-size: 11px; }.search-result strong { font-size: 13px; font-weight: 500; }
.extension-row { width: 100%; display: flex; align-items: center; justify-content: flex-start; gap: 16px; padding: 22px 20px; background: var(--panel); border: 0; border-radius: 0; white-space: normal; text-align: left; }.extension-row + .extension-row { border-top: 1px solid var(--border); }.extension-row > svg:first-child { color: var(--muted); }.extension-row div { flex: 1; min-width: 0; }.extension-row strong { font-size: 13px; font-weight: 500; }.extension-row p { margin: 5px 0 0; font-size: 12px; color: var(--muted); line-height: 1.6; }.extension-count { font-size: 11px; color: var(--muted); flex: none; }
.shortcut-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 15px 20px; }.shortcut-row + .shortcut-row { border-top: 1px solid var(--border); }.shortcut-row div { display: flex; flex-direction: column; gap: 3px; }.shortcut-row strong { font-size: 12px; font-weight: 500; }.shortcut-row span { font-size: 10px; color: var(--muted); }.shortcut-row kbd { padding: 3px 7px; border-radius: 5px; border: 1px solid var(--border); background: var(--panel-soft); color: var(--text-2); font-size: 11px; white-space: nowrap; }
.section-footnote { color: var(--muted); font-size: 11px; line-height: 1.8; }.subtle-badge { font-size: 10px; color: var(--muted); background: var(--panel-soft); border: 1px solid var(--border); border-radius: 5px; padding: 3px 8px; white-space: nowrap; }.diagnostic-links { display: flex; flex-wrap: wrap; gap: 10px; }.diagnostic-links button { font-size: 12px; }
.team-panel { margin-top: 0; }.team-form { max-width: none; }.team-footer .hint { max-width: 480px; font-size: 11px; }.settings-layout { grid-template-columns: minmax(220px, .8fr) minmax(0, 1.2fr); gap: 18px; }.active-panel { flex-wrap: wrap; gap: 15px; padding: 16px; }.active-capacity { gap: 20px; padding-left: 14px; }.active-model strong { max-width: 240px; }.feedback { overflow-wrap: anywhere; }
@media (max-width: 1250px) { .settings-content { padding: 28px 24px 40px; }.settings-workspace { grid-template-columns: 188px minmax(0, 1fr); }.scope-label { display: none; }.settings-layout { grid-template-columns: minmax(0, 1fr); }.active-capacity { border-left: 0; padding-left: 0; flex: 1; justify-content: flex-end; }.settings-header { padding: 20px 24px; } }
@media (max-width: 820px) { .settings-workspace { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }.settings-sidebar { padding: 14px 16px 10px; border-right: 0; border-bottom: 1px solid var(--border); overflow: visible; gap: 10px; }.settings-search { max-width: 360px; margin: 0; }.settings-sidebar nav { flex-direction: row; overflow-x: auto; gap: 4px; padding: 1px 0 4px; }.settings-nav-item { flex: none; min-height: 34px; font-size: 12px; padding: 0 10px; gap: 6px; }.settings-sidebar-note { display: none; }.settings-content { padding: 24px 20px 40px; }.settings-header { min-height: 78px; padding: 16px 20px; }.settings-header h1 { font-size: 21px; }.category-heading { margin-bottom: 22px; }.setting-row { gap: 16px; } }
@media (max-width: 520px) { .settings-header p { display: none; }.settings-header-actions .access-label { display: none; }.settings-content { padding: 22px 14px 32px; }.setting-row { align-items: flex-start; flex-wrap: wrap; padding: 16px; gap: 12px; }.setting-row > div:first-child { flex-basis: calc(100% - 70px); }.setting-row select { width: 100%; }.setting-group > h3 { padding: 12px 16px; }.settings-header { min-height: 66px; }.category-heading h2 { font-size: 19px; }.segmented { flex-wrap: wrap; }.active-model strong { max-width: 190px; }.extension-row { padding: 17px 14px; gap: 10px; }.extension-count { display: none; }.shortcut-row { padding: 14px; gap: 8px; }.shortcut-row kbd { font-size: 10px; }.team-footer { align-items: flex-start; flex-direction: column; }.team-footer button { align-self: flex-end; }.group-actions { justify-content: flex-start; } }

</style>
