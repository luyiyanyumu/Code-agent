<script setup>
import { computed, nextTick, reactive, ref, useId, watch } from 'vue'
import UiIcon from '../ui/UiIcon.vue'
import { ACCEPTANCE_LIMIT, ACCEPTANCE_TEMPLATES, acceptanceSummary, filterAcceptanceChecks, acceptanceTemplateTitles } from '../../lib/designAcceptance.js'

const props = defineProps({ checks: { type: Array, default: () => [] }, disabled: { type: Boolean, default: false } })
const emit = defineEmits(['add', 'add-batch', 'update', 'state', 'remove', 'export-report'])
const instanceId = useId()
const title = ref(''), filter = ref('all'), query = ref(''), notice = ref(''), adding = ref(false)
const drafts = reactive(Object.create(null)), editing = new Set()
const summary = computed(() => acceptanceSummary(props.checks))
const remaining = computed(() => Math.max(0, ACCEPTANCE_LIMIT - summary.value.total))
const filtered = computed(() => filterAcceptanceChecks(props.checks, { state: filter.value, query: query.value }))
const filters = [{ id: 'all', label: '全部', count: 'total' }, { id: 'pending', label: '待验收', count: 'pending' }, { id: 'failed', label: '失败', count: 'failed' }, { id: 'passed', label: '通过', count: 'passed' }]
const editKey = (id, field) => `${id}\u0000${field}`
watch(() => props.checks, checks => {
  const ids = new Set()
  for (const check of Array.isArray(checks) ? checks : []) {
    if (!check || typeof check.id !== 'string' || typeof check.title !== 'string') continue
    ids.add(check.id)
    if (!drafts[check.id]) drafts[check.id] = { title: '', note: '' }
    for (const field of ['title', 'note']) if (!editing.has(editKey(check.id, field))) drafts[check.id][field] = String(check[field] || '').slice(0, field === 'title' ? 240 : 600)
  }
  for (const id of Object.keys(drafts)) if (!ids.has(id)) { delete drafts[id]; editing.delete(editKey(id, 'title')); editing.delete(editKey(id, 'note')) }
}, { immediate: true, deep: true })
function submitAdd() {
  if (props.disabled || adding.value || !remaining.value) return
  const value = title.value.trim().slice(0, 240)
  if (!value) return
  adding.value = true; notice.value = ''; title.value = ''
  emit('add', value)
  nextTick(() => { adding.value = false })
}
function addTemplate(templateId) {
  if (props.disabled || adding.value) return
  const titles = acceptanceTemplateTitles(templateId, props.checks)
  if (!titles.length) return
  adding.value = true
  notice.value = `已添加 ${titles.length} 项验收模板，请按当前页面调整。`
  emit('add-batch', titles)
  nextTick(() => { adding.value = false })
}
function updateCheck(check, field) {
  const key = editKey(check.id, field)
  editing.delete(key)
  if (props.disabled || !drafts[check.id]) return
  const value = field === 'title' ? drafts[check.id].title.trim().slice(0, 240) : drafts[check.id].note.slice(0, 600)
  if (field === 'title' && !value) { drafts[check.id].title = check.title; notice.value = '验收项标题不能为空。'; return }
  drafts[check.id][field] = value
  if (value === String(check[field] || '')) return
  emit('update', { id: check.id, [field]: value })
  nextTick(() => {
    const current = props.checks.find(item => item.id === check.id)
    if (current && drafts[check.id] && !editing.has(key) && drafts[check.id][field] === value) drafts[check.id][field] = String(current[field] || '').slice(0, field === 'title' ? 240 : 600)
  })
}
function inputUndo(event) { if ((event.ctrlKey || event.metaKey) && ['z', 'y'].includes(String(event.key).toLowerCase())) event.stopPropagation() }
function editKeydown(event, check, field) {
  inputUndo(event)
  if (event.key === 'Enter' && field === 'title' && !event.ctrlKey && !event.metaKey && !event.altKey) { event.preventDefault(); event.stopPropagation(); updateCheck(check, field) }
  else if (event.key === 'Escape') { drafts[check.id][field] = String(check[field] || ''); editing.delete(editKey(check.id, field)); event.stopPropagation(); event.target?.blur?.() }
}
function setState(check, state) {
  if (!props.disabled && ['pending', 'passed', 'failed'].includes(state) && state !== check.state) emit('state', { id: check.id, state })
}
function removeCheck(id) { if (!props.disabled) emit('remove', id) }
function exportReport() { if (!props.disabled && summary.value.total) emit('export-report') }
</script>

<template>
  <section class="design-acceptance" aria-label="功能验收">
    <header class="acceptance-heading"><strong>功能验收</strong><button type="button" class="export-report" :disabled="disabled || !summary.total" aria-label="导出 Markdown 验收报告" title="导出 Markdown 验收报告" @click="exportReport"><UiIcon name="download" :size="13" />导出</button></header>
    <div class="acceptance-overview"><div><strong>{{ summary.passed }}<small> / {{ summary.total }} 通过</small></strong><span>{{ summary.progress }}%</span></div><progress :value="summary.passed" :max="summary.total || 1" aria-label="验收通过进度" /><p>已验收 {{ summary.completed }} 项<span>失败 {{ summary.failed }} · 待验收 {{ summary.pending }}</span></p></div>
    <form class="acceptance-add" @submit.prevent="submitAdd"><label class="sr-only" :for="`${instanceId}-new-check`">新增验收项</label><input :id="`${instanceId}-new-check`" v-model="title" type="text" maxlength="240" aria-label="新增验收项" placeholder="添加操作与预期结果" :disabled="disabled || !remaining" @keydown="inputUndo" /><button type="submit" :disabled="disabled || adding || !remaining || !title.trim()" aria-label="添加验收项"><UiIcon name="plus" :size="15" /></button></form>
    <details class="acceptance-templates"><summary>常用验收模板<span>{{ remaining }} 个空位</span></summary><div><button v-for="template in ACCEPTANCE_TEMPLATES" :key="template.id" type="button" :disabled="disabled || adding || !acceptanceTemplateTitles(template.id, checks).length" :title="template.titles.join('\n')" @click="addTemplate(template.id)">{{ template.label }}<span>+{{ acceptanceTemplateTitles(template.id, checks).length }}</span></button></div></details>
    <p v-if="notice" class="acceptance-notice" role="status">{{ notice }}</p><p v-if="!remaining" class="acceptance-notice" role="status">已达到 100 项上限，删除后可继续添加。</p>
    <div class="acceptance-filter" role="group" aria-label="筛选验收项"><button v-for="item in filters" :key="item.id" type="button" :class="{ active: filter === item.id }" :aria-pressed="filter === item.id" @click="filter = item.id">{{ item.label }}<span>{{ summary[item.count] }}</span></button></div>
    <label class="acceptance-search"><UiIcon name="search" :size="12" /><input v-model="query" type="search" placeholder="搜索标题或备注" aria-label="搜索验收项" @keydown="inputUndo" /></label>
    <div class="acceptance-list"><article v-for="check in filtered" :key="check.id" class="acceptance-item" :class="check.state"><div class="check-title"><span class="check-dot" aria-hidden="true" /><input v-model="drafts[check.id].title" type="text" maxlength="240" :disabled="disabled" :aria-label="`验收项标题：${check.title}`" @focus="editing.add(editKey(check.id, 'title'))" @input="editing.add(editKey(check.id, 'title'))" @blur="updateCheck(check, 'title')" @keydown="editKeydown($event, check, 'title')" /><button type="button" class="remove-check" :disabled="disabled" :aria-label="`删除验收项：${check.title}`" @click="removeCheck(check.id)"><UiIcon name="trash" :size="12" /></button></div><div class="check-states" role="group" :aria-label="`${check.title}的验收状态`"><button v-for="state in [{ id: 'passed', label: '通过' }, { id: 'failed', label: '失败' }, { id: 'pending', label: '待验收' }]" :key="state.id" type="button" :class="{ active: check.state === state.id }" :aria-pressed="check.state === state.id" :disabled="disabled" @click="setState(check, state.id)">{{ state.label }}</button></div><textarea v-model="drafts[check.id].note" rows="2" maxlength="600" :disabled="disabled" :aria-label="`${check.title}的验收备注`" placeholder="实际结果 / 复现步骤" @focus="editing.add(editKey(check.id, 'note'))" @input="editing.add(editKey(check.id, 'note'))" @blur="updateCheck(check, 'note')" @keydown="editKeydown($event, check, 'note')" /></article><p v-if="!filtered.length" class="acceptance-empty">{{ summary.total ? '没有匹配的验收项。' : '添加验收项，或从常用模板开始。' }}</p></div>
    <footer>验收记录保存在当前浏览器，按项目隔离。</footer>
  </section>
</template>

<style scoped>
.design-acceptance { display: flex; flex-direction: column; height: 100%; min-width: 0; min-height: 0; color: var(--text); background: var(--panel); font-size: 11px; }.acceptance-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 13px 11px 9px; flex: none; }.acceptance-heading strong { font-size: 12px; font-weight: 600; }.export-report { display: inline-flex; gap: 4px; align-items: center; padding: 3px; border: 0; background: transparent; color: var(--muted); font-size: 10px; }
.acceptance-overview { padding: 0 12px 10px; flex: none; }.acceptance-overview > div { display: flex; align-items: center; justify-content: space-between; }.acceptance-overview strong { font-size: 18px; font-weight: 600; }.acceptance-overview small { color: var(--muted); font-size: 10px; font-weight: 400; }.acceptance-overview > div > span { color: var(--ok); font-size: 11px; }.acceptance-overview progress { display: block; width: 100%; height: 5px; margin: 9px 0; border: 0; border-radius: 3px; overflow: hidden; accent-color: var(--ok); background: var(--border); }.acceptance-overview progress::-webkit-progress-bar { background: var(--border); }.acceptance-overview progress::-webkit-progress-value { background: var(--ok); }.acceptance-overview p { display: flex; justify-content: space-between; gap: 4px; margin: 0; color: var(--muted); font-size: 9px; }
.acceptance-add { display: flex; gap: 5px; padding: 0 10px 8px; flex: none; }.acceptance-add input, .acceptance-search input { flex: 1; min-width: 0; font: inherit; color: var(--text); }.acceptance-add input { padding: 6px 7px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel-soft); }.acceptance-add > button { display: grid; place-items: center; width: 29px; flex: none; padding: 3px; border: 1px solid var(--accent-border); border-radius: 5px; color: var(--accent); background: var(--accent-soft); }
.acceptance-templates { flex: none; padding: 0 11px 9px; border-bottom: 1px solid var(--border); }.acceptance-templates summary { cursor: pointer; padding: 3px 0 5px; color: var(--text-2); font-size: 10px; }.acceptance-templates summary > span { float: right; color: var(--muted); font-size: 9px; }.acceptance-templates > div { display: flex; gap: 5px; padding-top: 4px; }.acceptance-templates button { flex: 1; min-width: 0; padding: 5px 3px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel-soft); color: var(--text-2); font-size: 10px; }.acceptance-templates button > span { padding-left: 3px; color: var(--muted); font-size: 9px; }.acceptance-notice { padding: 7px 11px 0; margin: 0; color: var(--muted); font-size: 10px; line-height: 1.6; }
.acceptance-filter { display: flex; gap: 2px; padding: 9px 9px 7px; flex: none; }.acceptance-filter button { flex: 1; min-width: 0; display: inline-flex; justify-content: center; gap: 4px; padding: 5px 2px; border: 1px solid transparent; border-radius: 4px; background: transparent; color: var(--muted); font-size: 10px; white-space: nowrap; }.acceptance-filter button.active { color: var(--accent); background: var(--accent-soft); border-color: var(--accent-border); }.acceptance-filter button > span { font-size: 9px; opacity: .8; }.acceptance-search { display: flex; align-items: center; gap: 5px; margin: 0 10px 8px; padding: 5px 6px; color: var(--muted); border: 1px solid var(--border); border-radius: 5px; background: var(--panel-soft); flex: none; }.acceptance-search input { padding: 0; border: 0; background: transparent; outline: none; font-size: 10px; }
.acceptance-list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 0 10px 9px; }.acceptance-item { padding: 8px; margin-bottom: 7px; border: 1px solid var(--border); border-radius: 6px; }.acceptance-item.failed { border-color: color-mix(in srgb, var(--err) 40%, var(--border)); }.acceptance-item.passed { border-color: color-mix(in srgb, var(--ok) 35%, var(--border)); }.check-title { display: flex; align-items: center; gap: 4px; }.check-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--muted); flex: none; }.passed .check-dot { background: var(--ok); }.failed .check-dot { background: var(--err); }.check-title input { flex: 1; min-width: 0; padding: 3px; border: 1px solid transparent; background: transparent; color: var(--text); font: inherit; border-radius: 3px; }.remove-check { display: grid; place-items: center; width: 20px; min-height: 20px; padding: 2px; border: 0; background: transparent; color: var(--muted); flex: none; }.remove-check:hover { color: var(--err); }.check-states { display: flex; gap: 4px; padding: 7px 0; }.check-states button { flex: 1; padding: 3px 4px; border: 1px solid var(--border); border-radius: 4px; background: transparent; color: var(--muted); font-size: 9px; }.check-states button.active { color: var(--accent); background: var(--accent-soft); border-color: var(--accent-border); }.acceptance-item textarea { box-sizing: border-box; width: 100%; min-width: 0; padding: 5px 6px; border: 1px solid var(--border); border-radius: 4px; background: var(--panel-soft); color: var(--text); resize: vertical; font: inherit; font-size: 10px; line-height: 1.6; }.acceptance-empty { text-align: center; color: var(--muted); padding: 24px 8px; font-size: 10px; line-height: 1.7; }.design-acceptance footer { flex: none; padding: 9px 11px; border-top: 1px solid var(--border); color: var(--muted); font-size: 9px; line-height: 1.6; }
button { min-height: 24px; }.check-title input { min-height: 24px; }button:disabled, input:disabled, textarea:disabled { opacity: .45; cursor: default; }button:focus-visible, input:focus-visible, textarea:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }.acceptance-search:focus-within { border-color: var(--accent-border); box-shadow: var(--focus-ring); }.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style>
