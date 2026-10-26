<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { useDialog } from '../composables/useDialog.js'
import PageHeader from '../components/ui/PageHeader.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import UiIcon from '../components/ui/UiIcon.vue'

const data = ref({ user: { entries: [] }, sessions: [] })
const error = ref('')
const notice = ref('')
const newFact = ref('')
const busy = ref('')
const loading = ref(false)
const loaded = ref(false)
const expandedSession = ref('')
const query = ref('')
const scopeFilter = ref('all')
const statusFilter = ref('all')
const draftOpen = ref(false)
const draftScope = ref('user')
const draftCategory = ref('general')
const draftPinned = ref(false)
const editing = ref(null)
const dialog = useDialog()
let reloadRequest = null

const kinds = [
  { id: 'all', label: '全部' }, { id: 'user', label: '个人' },
  { id: 'project', label: '项目' }, { id: 'session', label: '会话' },
]
const categories = [
  { id: 'general', label: '其他' }, { id: 'preference', label: '偏好' },
  { id: 'convention', label: '编码约定' }, { id: 'context', label: '背景' }, { id: 'decision', label: '项目决策' },
]
const scopeLabel = kind => ({ user: '个人', project: '项目', session: '会话' }[kind] || '会话')
const categoryLabel = value => categories.find(item => item.id === value)?.label || value || '通用'
const sourceLabel = value => ({ user: '手动保存', manual: '手动保存', agent: '智能体保存', tool: '智能体保存', legacy: '历史记忆', imported: '导入' }[value] || value || '历史记忆')
const scopeDescription = kind => ({
  user: '跨项目、跨会话使用，适合个人偏好与长期工作习惯。',
  project: '同一项目的 Lead、节点和子智能体共享，适合项目背景、约定与决策。',
  session: '只供所属会话使用，适合本次任务的背景和进展。',
}[kind] || '个人、项目与会话按各自范围检索，停用的记忆不会参与检索。')

// Older servers expose strings only. Keep their deletion flow safe while making
// record controls available only when stable IDs and scope revisions exist.
function memoryScopes(payload) {
  if (Array.isArray(payload?.scopes)) return payload.scopes.map(scope => ({ ...scope, records: scope.records || [] }))
  const legacy = (entries = []) => entries.map((content, index) => ({ id: '', content, enabled: true, pinned: false, category: 'general', source: 'legacy', index }))
  return [
    { id: 'user', kind: 'user', title: '个人记忆', records: legacy(payload?.user?.entries) },
    ...(payload?.sessions || []).map(session => ({ id: `session:${session.session_id}`, kind: 'session', title: session.title, session_id: session.session_id, records: legacy(session.entries) })),
  ]
}
const scopes = computed(() => memoryScopes(data.value))
const records = computed(() => scopes.value.flatMap(scope => scope.records.map((record, index) => ({ ...record, index: record.index ?? index, scope }))))
const total = computed(() => records.value.length)
const enabledCount = computed(() => records.value.filter(record => record.enabled !== false).length)
const pinnedCount = computed(() => records.value.filter(record => record.pinned).length)
const draftTarget = computed(() => scopes.value.find(scope => scope.id === draftScope.value))
const latestEditRecord = computed(() => editing.value && draftTarget.value?.records.find(record => record.id === editing.value.id))
const draftStale = computed(() => editing.value && (!latestEditRecord.value || draftTarget.value?.revision !== editing.value.revision))
const draftCategories = computed(() => draftCategory.value && !categories.some(item => item.id === draftCategory.value)
  ? [...categories, { id: draftCategory.value, label: draftCategory.value }] : categories)
const visibleGroups = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase()
  return scopes.value.filter(scope => scopeFilter.value === 'all' || scope.kind === scopeFilter.value).map(scope => ({
    ...scope,
    records: scope.records.map((record, index) => ({ ...record, index: record.index ?? index, scope }))
      .filter(record => (statusFilter.value === 'all' || (statusFilter.value === 'enabled' ? record.enabled !== false : record.enabled === false))
        && (!needle || [record.content, scope.title, categoryLabel(record.category)].join(' ').toLocaleLowerCase().includes(needle)))
      .sort((a, b) => Number(b.pinned) - Number(a.pinned) || String(b.updated_at || '').localeCompare(String(a.updated_at || '')) || a.index - b.index),
  })).filter(scope => scope.records.length)
})
const matchingCount = computed(() => visibleGroups.value.reduce((count, scope) => count + scope.records.length, 0))
const canSave = computed(() => newFact.value.trim().length > 0 && newFact.value.trim().length <= 1000 && !!draftTarget.value && !draftStale.value && !busy.value && !loading.value)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.memory()
    loaded.value = true
  } catch (e) {
    error.value = e.message || '读取记忆失败，请重试。'
  } finally {
    loading.value = false
  }
}

// Settings keeps this screen mounted; refresh data without replacing its draft.
function reload() {
  if (busy.value) return Promise.resolve()
  if (reloadRequest) return reloadRequest
  reloadRequest = load().finally(() => { reloadRequest = null })
  return reloadRequest
}

function openAdd(scope = '') {
  // Reopening a draft must never erase text entered before leaving settings.
  if (!draftOpen.value) {
    editing.value = null
    draftScope.value = scope || scopes.value.find(item => item.kind === scopeFilter.value)?.id || 'user'
  }
  draftOpen.value = true
  focusDraft()
}
function startEdit(record) {
  if (draftOpen.value && (newFact.value.trim() || editing.value)) {
    notice.value = '请先保存或取消当前编辑，再编辑另一条记忆。'
    return
  }
  editing.value = { id: record.id, scope_id: record.scope.id, revision: record.scope.revision, content: record.content }
  draftScope.value = record.scope.id
  draftCategory.value = record.category || 'general'
  draftPinned.value = !!record.pinned
  newFact.value = record.content
  draftOpen.value = true
  error.value = ''
  notice.value = ''
  focusDraft()
}
function focusDraft() {
  if (typeof document !== 'undefined') Promise.resolve().then(() => document.getElementById('memory-fact')?.focus())
}
function resetDraft() {
  draftOpen.value = false
  editing.value = null
  newFact.value = ''
  draftScope.value = 'user'
  draftCategory.value = 'general'
  draftPinned.value = false
}
async function cancelDraft() {
  if (!newFact.value.trim()) return resetDraft()
  return dialog.open({
    title: '放弃未保存的记忆？', description: '已保存的记忆不会变化，当前编辑内容会被清除。',
    confirmText: '放弃编辑', tone: 'danger', onConfirm: resetDraft,
  })
}
async function reviewLatest() {
  const current = latestEditRecord.value
  if (!current || busy.value) return
  const revision = draftTarget.value.revision
  return dialog.open({
    title: '核对最新记忆', description: '继续编辑将以此版本为基础。保存后会用你的草稿替换该条内容。',
    details: `当前保存内容：\n${current.content}\n\n你的草稿：\n${newFact.value}`,
    confirmText: '以此版本继续编辑',
    onConfirm: () => { editing.value = { ...editing.value, revision, content: current.content }; error.value = ''; notice.value = '已保留草稿，请核对后保存。' },
  })
}
function scopePayload(scope) {
  const kind = scope.kind === 'session' && scope.revision === undefined ? 'workspace' : scope.kind
  return { scope: kind, scope_id: scope.id, ...(scope.project_id ? { project_id: scope.project_id } : {}), ...(scope.session_id ? { session_id: scope.session_id } : {}) }
}
async function saveDraft() {
  if (!canSave.value) return
  const scope = draftTarget.value
  const text = newFact.value.trim()
  const edit = editing.value && { ...editing.value }
  busy.value = edit ? `edit:${edit.id}` : 'add'
  error.value = ''; notice.value = ''
  try {
    const payload = { ...scopePayload(scope), content: text, category: draftCategory.value, pinned: draftPinned.value }
    if (edit) await api.updateMemory(edit.id, { ...payload, revision: edit.revision })
    else await api.addMemory({ ...payload, ...(scope.revision !== undefined ? { revision: scope.revision } : {}) })
    resetDraft()
    notice.value = edit ? '记忆已更新。' : '记忆已保存，相关任务会按使用范围检索。'
    await load()
  } catch (e) {
    error.value = e.status === 409 ? '该范围的记忆已被其他操作更新。已保留你的草稿，请刷新并核对最新内容后重新编辑。' : e.message
  } finally { busy.value = '' }
}
async function addUserFact() { return saveDraft() }
async function updateRecord(record, changes) {
  if (busy.value || !record.id) return
  busy.value = `update:${record.id}`; error.value = ''; notice.value = ''
  try {
    await api.updateMemory(record.id, { ...scopePayload(record.scope), revision: record.scope.revision, ...changes })
    notice.value = 'enabled' in changes ? (changes.enabled ? '已启用，相关任务可检索这条记忆。' : '已停用，保留内容但不参与检索。') : (changes.pinned ? '已置顶。' : '已取消置顶。')
    await load()
  } catch (e) {
    error.value = e.status === 409 ? '记忆已被其他操作更新，请刷新后重试。' : e.message
  } finally { busy.value = '' }
}
async function removeUserFact(i) { return removeFact('user', i) }
async function removeSessionFact(sid, i) { return removeFact('workspace', i, sid) }
async function removeFact(scope, index, sessionId = '') {
  const record = records.value.find(item => item.index === index && (scope === 'user' ? item.scope.kind === 'user' : item.scope.session_id === sessionId))
  if (record) return removeRecord(record)
}
async function removeRecord(record) {
  const captured = { ...record, scope: { ...record.scope } }
  const label = captured.scope.kind === 'user' ? '个人记忆' : captured.scope.kind === 'project' ? '项目记忆' : '会话记忆'
  return dialog.open({
    title: `删除${label}`,
    description: captured.scope.kind === 'user' ? '这条记忆将从个人记忆中移除，之后的相关任务不再检索它。' : `这条记忆将从「${captured.scope.title || '当前会话'}」中移除。`,
    details: captured.content, tone: 'danger', icon: 'memory', confirmText: '删除记忆', pendingText: '正在删除…',
    onConfirm: async () => {
      busy.value = `delete:${captured.id || captured.index}`; error.value = ''; notice.value = ''
      try {
        if (captured.id && captured.scope.revision !== undefined) {
          await api.deleteMemoryRecord(captured.scope.id, captured.id, captured.scope.revision)
        } else {
          const latest = await api.memory()
          const current = memoryScopes(latest).find(item => item.id === captured.scope.id)?.records
          if (current?.[captured.index]?.content !== captured.content) {
            data.value = latest
            throw new Error('记忆列表已变化，请关闭弹窗后重新选择要删除的条目。')
          }
          await api.deleteMemory(captured.scope.kind === 'user' ? 'user' : 'workspace', captured.index, captured.scope.session_id || '')
        }
        notice.value = '记忆已删除。'
        await load()
      } catch (e) {
        if (e.status === 409) throw new Error('记忆已被其他操作更新，请关闭弹窗，刷新后重新选择。')
        throw e
      } finally { busy.value = '' }
    },
  })
}
function fmt(ts) {
  if (!ts) return '时间未记录'
  const value = new Date(ts)
  return Number.isNaN(value.getTime()) ? '时间未记录' : value.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}
function clearFilters() { query.value = ''; scopeFilter.value = 'all'; statusFilter.value = 'all' }
onMounted(reload)
defineExpose({ reload })
</script>

<template>
  <div class="page memory-page">
    <PageHeader title="记忆" description="管理编码助手使用的偏好、项目约定与任务背景。" eyebrow="资源" icon="memory" />
    <section class="memory-overview card" aria-label="记忆概况">
      <div class="overview-copy"><span class="overview-icon"><UiIcon name="memory" :size="20" /></span><div><h2>让助手记住重要的事</h2><p>保存稳定的偏好与项目约定。记忆按范围检索，可随时编辑、停用或删除。</p></div></div>
      <div class="memory-stats"><div><strong>{{ enabledCount }}</strong><span>已启用</span></div><div><strong>{{ total }}</strong><span>全部记忆</span></div><div><strong>{{ pinnedCount }}</strong><span>已置顶</span></div></div>
    </section>
    <p v-if="error" class="feedback err" role="alert">{{ error }} <button :disabled="loading || !!busy" @click="reload">重试读取</button></p>
    <p v-if="notice" class="feedback ok" role="status">{{ notice }}</p>
    <form v-if="draftOpen" class="card memory-editor" aria-label="记忆编辑" @submit.prevent="saveDraft">
      <div class="editor-heading"><h3>{{ editing ? '编辑记忆' : '添加记忆' }}</h3><span class="muted">保存后按所选范围生效</span></div>
      <div class="editor-options"><label>使用范围<select aria-label="使用范围" v-model="draftScope" :disabled="!!editing || !!busy"><option v-for="scope in scopes" :key="scope.id" :value="scope.id">{{ scopeLabel(scope.kind) }} · {{ scope.title }}</option></select></label><label>分类<select aria-label="分类" v-model="draftCategory" :disabled="!!busy"><option v-for="category in draftCategories" :key="category.id" :value="category.id">{{ category.label }}</option></select></label></div>
      <p class="scope-help">{{ scopeDescription(draftTarget?.kind) }}</p>
      <p v-if="draftStale" class="feedback err" role="status">{{ latestEditRecord ? '记忆已更新，草稿已保留。保存前请核对最新版本。' : '这条记忆已被移除，草稿已保留。可复制内容后取消编辑，再添加为新记忆。' }}<button v-if="latestEditRecord" type="button" :disabled="!!busy" @click="reviewLatest">核对最新版本</button></p>
      <label class="sr-only" for="memory-fact">记忆内容</label><textarea id="memory-fact" v-model="newFact" rows="4" :disabled="!!busy" placeholder="例如：项目统一使用 Java 21；回答先给结论，再给依据。" @keydown.ctrl.enter.prevent="saveDraft" @keydown.meta.enter.prevent="saveDraft" />
      <div class="editor-footer"><label class="pin-option"><input v-model="draftPinned" type="checkbox" :disabled="!!busy" />置顶，优先加入上下文（受容量限制）</label><span class="character-count" :class="{ 'too-long': newFact.trim().length > 1000 }">{{ newFact.trim().length }} / 1000</span><div class="editor-actions"><button type="button" :disabled="!!busy" @click="cancelDraft">取消</button><button type="submit" class="primary" :disabled="!canSave">{{ busy === 'add' || busy.startsWith('edit:') ? '保存中…' : '保存记忆' }}</button></div></div>
    </form>
    <section class="card memory-library" aria-label="记忆列表" :aria-busy="loading">
      <div class="library-toolbar"><div class="scope-tabs" aria-label="筛选记忆范围"><button v-for="kind in kinds" :key="kind.id" :aria-pressed="scopeFilter === kind.id" :class="{ active: scopeFilter === kind.id }" @click="scopeFilter = kind.id">{{ kind.label }}<span>{{ kind.id === 'all' ? total : records.filter(record => record.scope.kind === kind.id).length }}</span></button></div><div class="library-actions"><button :disabled="loading || !!busy" title="刷新记忆" aria-label="刷新记忆" @click="reload"><UiIcon name="refresh" :size="15" /></button><button class="primary" :disabled="!loaded || !!busy" @click="openAdd()"><UiIcon name="plus" :size="15" />添加记忆</button></div></div>
      <div class="search-row"><label class="search-input"><UiIcon name="search" :size="16" /><input v-model="query" type="search" aria-label="搜索记忆" placeholder="搜索内容、项目或分类…" /></label><select v-model="statusFilter" aria-label="筛选启用状态"><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select><span class="match-count">{{ matchingCount }} 条</span></div>
      <p class="scope-help list-help">{{ scopeDescription(scopeFilter) }}</p>
      <div v-if="loading && !loaded" class="loading-state" role="status"><UiIcon name="refresh" :size="18" />正在读取记忆…</div>
      <div v-else-if="!loaded && error" class="loading-state">记忆暂时无法读取，可点击“重试读取”。</div>
      <template v-else-if="visibleGroups.length"><section v-for="scope in visibleGroups" :key="scope.id" class="memory-group"><div class="group-heading"><UiIcon :name="scope.kind === 'project' ? 'folder' : scope.kind === 'session' ? 'chat' : 'memory'" :size="16" /><h3>{{ scope.title }}</h3><span class="scope-badge">{{ scopeLabel(scope.kind) }}</span><span class="group-count">{{ scope.records.length }} 条</span></div><ul class="facts"><li v-for="record in scope.records" :key="record.id || record.index" :class="{ disabled: record.enabled === false }"><div class="fact-main"><div class="record-labels"><span v-if="record.pinned" class="pinned-label"><UiIcon name="memory" :size="12" />置顶</span><span>{{ categoryLabel(record.category) }}</span><span v-if="record.enabled === false" class="disabled-label">已停用</span></div><p class="fact-text">{{ record.content }}</p><div class="record-meta"><span>{{ sourceLabel(record.source) }}</span><span>更新于 {{ fmt(record.updated_at) }}</span></div></div><div class="record-actions"><template v-if="record.id && scope.revision !== undefined"><button class="small" :disabled="!!busy || loading" :aria-label="`${record.pinned ? '取消置顶' : '置顶'}记忆：${record.content}`" @click="updateRecord(record, { pinned: !record.pinned })">{{ record.pinned ? '取消置顶' : '置顶' }}</button><button class="small" :disabled="!!busy || loading" :aria-label="`${record.enabled === false ? '启用' : '停用'}记忆：${record.content}`" @click="updateRecord(record, { enabled: record.enabled === false })">{{ record.enabled === false ? '启用' : '停用' }}</button><button class="small" :disabled="!!busy || loading" :aria-label="`编辑记忆：${record.content}`" @click="startEdit(record)">编辑</button></template><button class="small danger" :disabled="!!busy || loading" :aria-label="`删除记忆：${record.content}`" @click="removeRecord(record)">删除</button></div></li></ul></section></template>
      <EmptyState v-else-if="total" icon="search" title="没有匹配的记忆" description="试试其他关键词、使用范围或启用状态。"><button @click="clearFilters">清除筛选</button></EmptyState>
      <EmptyState v-else icon="memory" title="从一条重要约定开始" description="添加回答偏好、项目规范或任务背景，相关编码任务会按范围使用。"><button class="primary" @click="openAdd()"><UiIcon name="plus" :size="15" />添加记忆</button></EmptyState>
    </section>
  </div>
</template>

<style scoped>
.page { padding: 28px; overflow-y: auto; background: var(--bg); }
.page > * { max-width: 1440px; margin-left: auto; margin-right: auto; }
.memory-overview { display: flex; justify-content: space-between; align-items: center; gap: 24px; padding: 20px; margin-bottom: 16px; }
.overview-copy { display: flex; gap: 13px; align-items: center; min-width: 0; }
.overview-icon { display: grid; place-items: center; flex: none; width: 40px; height: 40px; border-radius: 9px; background: var(--accent-soft); color: var(--accent); }
h2 { margin: 0; font-size: 15px; font-weight: 650; }
h3 { margin: 0; font-size: 13px; font-weight: 600; }
.overview-copy p { margin: 6px 0 0; font-size: 12px; color: var(--muted); line-height: 1.7; }
.memory-stats { display: flex; flex: none; gap: 28px; }
.memory-stats > div { display: grid; gap: 5px; text-align: center; }
.memory-stats strong { font-size: 22px; font-weight: 650; }
.memory-stats span { color: var(--muted); font-size: 11px; }
.memory-library { overflow: hidden; }
.library-toolbar { display: flex; justify-content: space-between; gap: 16px; padding: 15px 18px; align-items: center; }
.scope-tabs { display: flex; flex-wrap: wrap; gap: 4px; }
.scope-tabs button { border: 0; color: var(--muted); background: transparent; font-size: 12px; padding: 7px 10px; }
.scope-tabs button.active { color: var(--accent); background: var(--accent-soft); }
.scope-tabs button span { margin-left: 5px; opacity: .8; font-size: 11px; }
.library-actions { display: flex; flex: none; gap: 7px; }
.search-row { display: flex; gap: 10px; align-items: center; padding: 0 18px; }
.search-input { flex: 1; display: flex; align-items: center; gap: 8px; min-width: 0; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 7px; padding: 0 11px; color: var(--muted); }
.search-input:focus-within { border-color: var(--accent); }
.search-input input { min-width: 0; width: 100%; border: 0; background: transparent; outline: none; box-shadow: none; padding: 9px 0; }
.search-row select { flex: none; font-size: 12px; }
.match-count { flex: none; font-size: 11px; color: var(--muted); }
.scope-help { margin: 9px 0 14px; font-size: 12px; line-height: 1.7; color: var(--muted); }
.list-help { margin: 10px 18px 16px; }
.memory-group { border-top: 1px solid var(--border); }
.group-heading { display: flex; align-items: center; gap: 8px; padding: 13px 20px; background: var(--panel-soft); color: var(--muted); }
.group-heading h3 { color: var(--text); overflow-wrap: anywhere; }
.scope-badge { font-size: 10px; border: 1px solid var(--border); padding: 2px 6px; border-radius: 4px; flex: none; }
.group-count { font-size: 11px; margin-left: auto; flex: none; }
.facts { list-style: none; margin: 0; padding: 0 20px; }
.facts li { display: flex; align-items: center; gap: 18px; padding: 17px 0; border-bottom: 1px solid var(--border); }
.facts li:last-child { border-bottom: none; }
.fact-main { min-width: 0; flex: 1; }
.record-labels { display: flex; gap: 8px; align-items: center; font-size: 10px; color: var(--muted); margin-bottom: 5px; }
.pinned-label { display: inline-flex; gap: 4px; align-items: center; color: var(--accent); }
.disabled-label { color: var(--muted); padding: 1px 5px; background: var(--panel-soft); border-radius: 3px; }
.fact-text { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.75; font-size: 13px; }
.disabled .fact-text { color: var(--muted); }
.record-meta { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 7px; color: var(--muted); font-size: 10px; }
.record-actions { display: flex; flex: none; gap: 5px; flex-wrap: wrap; }
button.small { min-height: 28px; font-size: 11px; padding: 3px 8px; }
.memory-editor { padding: 20px; margin-bottom: 16px; }
.editor-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 15px; }
.editor-heading .muted { font-size: 11px; }
.editor-options { display: grid; grid-template-columns: minmax(0, 1fr) 180px; gap: 14px; }
.editor-options label { display: grid; gap: 6px; font-size: 11px; color: var(--muted); }
.editor-options select { width: 100%; min-width: 0; font-size: 12px; }
.memory-editor textarea { resize: vertical; min-height: 108px; width: 100%; display: block; box-sizing: border-box; font-size: 13px; line-height: 1.75; }
.editor-footer { display: flex; gap: 15px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
.pin-option { display: flex; gap: 6px; align-items: center; font-size: 11px; color: var(--muted); }
.pin-option input { accent-color: var(--accent); }
.character-count { color: var(--muted); font-size: 11px; margin-left: auto; }
.too-long { color: var(--err); }
.editor-actions { display: flex; gap: 7px; }
.loading-state { display: flex; align-items: center; justify-content: center; gap: 9px; padding: 58px 20px; font-size: 12px; color: var(--muted); }
.feedback { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 10px 13px; margin-bottom: 16px; border-radius: 7px; font-size: 12px; line-height: 1.7; }
.feedback button { padding: 3px 8px; min-height: 26px; font-size: 11px; }
.err { color: var(--err); background: var(--err-soft, #fff1f2); }
.ok { color: var(--ok); background: var(--ok-soft, #edf9f3); }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
@media (max-width: 1000px) { .facts li { align-items: flex-start; flex-direction: column; gap: 10px; } .record-actions { align-self: flex-end; } }
@media (max-width: 700px) { .page { padding: 18px 14px; } .memory-overview { align-items: flex-start; flex-direction: column; gap: 18px; } .memory-stats { width: 100%; justify-content: space-around; gap: 12px; } .library-toolbar { flex-wrap: wrap; padding: 13px; gap: 10px; } .library-actions { margin-left: auto; } .scope-tabs button { padding: 6px 8px; } .search-row { padding: 0 13px; flex-wrap: wrap; } .search-input { flex-basis: 100%; } .match-count { margin-left: auto; } .list-help { margin: 10px 13px 14px; } .group-heading { padding: 12px 14px; flex-wrap: wrap; } .facts { padding: 0 14px; } .memory-editor { padding: 16px; } .editor-options { grid-template-columns: minmax(0, 1fr); gap: 10px; } .editor-footer { gap: 10px; } .editor-actions { width: 100%; justify-content: flex-end; } .editor-heading { align-items: flex-start; flex-direction: column; gap: 5px; } }
</style>
