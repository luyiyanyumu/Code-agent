<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../../api.js'
import { businessEntityId, BUSINESS_SOURCE_LABEL, BUSINESS_STATUS_LABEL, displayBusiness, matchesBusinessMeaningChange } from '../../lib/businessMeaningDisplay.js'

const props = defineProps({
  projectId: { type: String, default: '' },
  entity: { type: Object, required: true },
})
const emit = defineEmits(['saved'])
const record = ref(null)
const loading = ref(false)
const saving = ref(false)
const editing = ref(false)
const error = ref('')
const notice = ref('')
const draft = ref(emptyDraft())
const business = computed(() => displayBusiness(record.value?.business))
const entityId = computed(() => businessEntityId(props.entity))
const statusLabel = computed(() => BUSINESS_STATUS_LABEL[business.value.status])
const sourceLabel = computed(() => BUSINESS_SOURCE_LABEL[business.value.source] || '')
const ready = computed(() => !!record.value?.business?.version && record.value?.entity?.id === entityId.value)
const rowGroups = ['inputs', 'outputs']
let generation = 0
let loadSeq = 0
let disposed = false

function emptyDraft() {
  return { business_meaning: '', detail: '', inputs: [], outputs: [], rules: '', effects: '', feature_refs: '' }
}
function key() { return JSON.stringify([props.projectId, entityId.value, props.entity.tree || 'main']) }
function capture() { return { generation, key: key(), projectId: props.projectId, id: entityId.value, tree: props.entity.tree || 'main' } }
function current(target) { return !disposed && target.generation === generation && target.key === key() }

function edit() {
  const value = business.value
  draft.value = {
    business_meaning: value.business_meaning || '', detail: value.detail || '',
    inputs: (value.inputs || []).map(row => ({ name: row.name || '', meaning: row.meaning || '' })),
    outputs: (value.outputs || []).map(row => ({ name: row.name || '', meaning: row.meaning || '' })),
    rules: (value.rules || []).join('\n'), effects: (value.effects || []).join('\n'), feature_refs: (value.feature_refs || []).join('\n'),
  }
  editing.value = true
  error.value = notice.value = ''
}
function cancel() { if (!saving.value) { editing.value = false; draft.value = emptyDraft(); error.value = '' } }
const lines = value => String(value || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean)
function payload(target) {
  const value = draft.value
  return {
    id: target.id, tree: target.tree,
    version: record.value?.business?.version || '',
    business_meaning: value.business_meaning.trim(), detail: value.detail.trim(),
    inputs: value.inputs.map(row => ({ name: row.name.trim(), meaning: row.meaning.trim() })).filter(row => row.name || row.meaning),
    outputs: value.outputs.map(row => ({ name: row.name.trim(), meaning: row.meaning.trim() })).filter(row => row.name || row.meaning),
    rules: lines(value.rules), effects: lines(value.effects), feature_refs: lines(value.feature_refs),
  }
}
async function load({ reset = false } = {}) {
  const target = capture()
  const mine = ++loadSeq
  if (reset) { record.value = null; error.value = notice.value = ''; editing.value = saving.value = false; draft.value = emptyDraft() }
  if (!target.projectId || !target.id || disposed) { loading.value = false; return }
  error.value = ''
  loading.value = true
  try {
    const result = await api.code.symbolBusiness(target.projectId, target.id, target.tree)
    if (current(target) && mine === loadSeq) record.value = result
  } catch (cause) {
    if (current(target) && mine === loadSeq) error.value = cause.message
  } finally { if (current(target) && mine === loadSeq) loading.value = false }
}
async function save() {
  if (saving.value) return
  const target = capture()
  if (!target.projectId || !target.id) return
  if (!ready.value) { error.value = '请先成功读取当前代码的功能记录，再编辑保存。'; return }
  const data = payload(target)
  if (!data.business_meaning) { error.value = '请填写一句话功能描述。'; return }
  loadSeq++
  loading.value = false
  saving.value = true
  error.value = notice.value = ''
  try {
    const result = await api.code.saveSymbolBusiness(target.projectId, data)
    if (!current(target)) return
    record.value = result
    editing.value = false
    notice.value = '已保存，功能导图、结构导图与编辑器共用这份功能描述。'
    emit('saved', result)
  } catch (cause) {
    if (current(target)) error.value = cause.message
  } finally { if (current(target)) saving.value = false }
}
function onBusinessChanged(event) {
  const detail = event.detail || {}
  if (!matchesBusinessMeaningChange(detail, { projectId: props.projectId, tree: props.entity.tree, id: entityId.value }) || saving.value) return
  generation++
  void load()
  if (editing.value) notice.value = '共享功能描述已更新；当前编辑内容保留，保存会应用你的说明。'
}
watch(key, () => { generation++; void load({ reset: true }) }, { immediate: true })
onMounted(() => window.addEventListener('code-agent:business-meaning-changed', onBusinessChanged))
onBeforeUnmount(() => { disposed = true; generation++; window.removeEventListener('code-agent:business-meaning-changed', onBusinessChanged) })
</script>

<template>
  <section v-if="entityId" class="business-meaning" aria-label="功能描述" :aria-busy="loading || saving">
    <header class="business-head">
      <b>功能描述</b><span v-if="record" class="business-status" :class="business.status">{{ statusLabel }}</span>
      <span class="spacer" />
      <button v-if="!editing" class="business-link" :disabled="loading || !ready" @click="edit">{{ business.status === 'unreviewed' ? '确认或修改' : '编辑' }}</button>
    </header>
    <p v-if="loading && !record" class="muted" role="status">读取功能描述…</p>
    <template v-else-if="!editing && record">
      <p v-if="business.business_meaning" class="business-summary">{{ business.business_meaning }}</p>
      <p v-else class="muted">{{ business.status === 'stale' ? '代码已变化，功能描述等待自动更新。' : '功能描述待自动补充。' }}</p>
      <p v-if="sourceLabel" class="business-source">{{ sourceLabel }}<template v-if="business.status === 'unreviewed'"> · 尚未人工确认</template></p>
      <p v-if="business.detail" class="business-detail">{{ business.detail }}</p>
      <dl v-for="group in rowGroups" :key="group" v-show="business[group]?.length" class="business-rows">
        <dt>{{ group === 'inputs' ? '输入与参数' : '输出与返回值' }}</dt>
        <dd v-for="(row, i) in business[group] || []" :key="i"><code>{{ row.name }}</code><span>{{ row.meaning }}</span></dd>
      </dl>
      <div v-for="group in ['rules', 'effects', 'feature_refs']" :key="group" v-show="business[group]?.length" class="business-list">
        <b>{{ { rules: '功能规则', effects: '副作用', feature_refs: '关联功能' }[group] }}</b>
        <ul><li v-for="(item, i) in business[group] || []" :key="i">{{ item }}</li></ul>
      </div>
      <details v-if="business.evidence?.length" class="business-evidence"><summary>依据（{{ business.evidence.length }}）</summary><ul><li v-for="(item, i) in business.evidence" :key="i">{{ typeof item === 'string' ? item : [item.path, item.line ? `:${item.line}` : '', item.text || item.reason || ''].filter(Boolean).join(' ') }}</li></ul></details>
    </template>
    <form v-else-if="editing" class="business-form" @submit.prevent="save">
      <label>一句话功能描述 <textarea v-model="draft.business_meaning" rows="2" maxlength="200" required placeholder="例如：查询当前用户有权查看的文档列表。" :disabled="saving" /></label>
      <details class="business-fields">
        <summary>更多功能信息（可选）</summary>
        <label>详细说明 <textarea v-model="draft.detail" rows="3" maxlength="6000" :disabled="saving" /></label>
        <fieldset v-for="group in rowGroups" :key="group" :disabled="saving">
          <legend>{{ group === 'inputs' ? '输入与参数' : '输出与返回值' }}</legend>
          <div v-for="(row, i) in draft[group]" :key="i" class="business-row-edit"><input v-model="row.name" aria-label="名称" placeholder="名称" maxlength="160" /><input v-model="row.meaning" aria-label="含义" placeholder="参数含义、单位或空值约定" maxlength="2000" /><button type="button" class="business-link" :aria-label="`删除${group === 'inputs' ? '输入' : '输出'} ${i + 1}`" @click="draft[group].splice(i, 1)">×</button></div>
          <button type="button" class="business-link" :disabled="draft[group].length >= 80" @click="draft[group].push({ name: '', meaning: '' })">＋ 添加{{ group === 'inputs' ? '输入' : '输出' }}</button>
        </fieldset>
        <label>功能规则（每行一条） <textarea v-model="draft.rules" rows="2" :disabled="saving" placeholder="取值范围、单位、默认值、状态变化等" /></label>
        <label>副作用（每行一条） <textarea v-model="draft.effects" rows="2" :disabled="saving" placeholder="修改数据、发送通知等" /></label>
        <label>关联功能（每行一项） <textarea v-model="draft.feature_refs" rows="2" :disabled="saving" placeholder="功能名称或节点标识" /></label>
      </details>
      <div class="business-actions"><button type="submit" class="primary" :disabled="saving || !ready">{{ saving ? '保存中…' : '保存为人工说明' }}</button><button type="button" :disabled="saving" @click="cancel">取消</button></div>
    </form>
    <p v-if="error" class="business-error" role="alert">{{ error }} <button v-if="!editing" class="business-link" :disabled="loading" @click="load()">重试</button></p>
    <p v-if="notice" class="muted" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.business-meaning { margin: 10px 0; padding: 10px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.business-head { display: flex; align-items: center; gap: 8px; }.spacer { flex: 1; }.business-status { padding: 2px 5px; border-radius: 4px; color: var(--muted); background: var(--panel-soft); font-size: var(--fs-2xs); }.business-status.current { color: var(--ok); background: var(--ok-soft); }.business-status.unreviewed, .business-status.stale { color: var(--warning, #b7791f); }
.business-summary { margin: 10px 0 5px; line-height: 1.7; }.business-source, .muted { color: var(--muted); font-size: var(--fs-2xs); line-height: 1.6; }.business-detail { white-space: pre-wrap; line-height: 1.7; }.business-link { padding: 3px 5px; border: 0; color: var(--accent); background: transparent; font-size: var(--fs-xs); }.business-link:hover { background: var(--accent-soft); }.business-rows { margin: 10px 0; }.business-rows dt, .business-list > b { color: var(--muted); font-size: var(--fs-2xs); font-weight: var(--fw-medium); }.business-rows dd { display: flex; gap: 8px; margin: 5px 0; }.business-rows code { flex-shrink: 0; max-width: 40%; }.business-rows dd span { white-space: pre-wrap; }.business-list { margin: 8px 0; }.business-list ul, .business-evidence ul { margin: 5px 0; padding-left: 18px; line-height: 1.7; }.business-evidence { color: var(--muted); font-size: var(--fs-2xs); }.business-evidence summary { cursor: pointer; }
.business-form { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }.business-form label { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-2xs); color: var(--muted); }.business-form textarea, .business-form input { width: 100%; min-width: 0; box-sizing: border-box; padding: 6px; font-size: var(--fs-xs); color: var(--text); background: var(--panel-soft); border: 1px solid var(--border); border-radius: 4px; }.business-form textarea { resize: vertical; }.business-fields > summary { cursor: pointer; color: var(--accent); margin-bottom: 8px; }.business-fields label { margin: 8px 0; }.business-form fieldset { min-width: 0; border: 1px solid var(--border); padding: 7px; margin: 8px 0; border-radius: 4px; }.business-form legend { color: var(--muted); font-size: var(--fs-2xs); }.business-row-edit { display: grid; grid-template-columns: minmax(45px, .7fr) minmax(60px, 1.3fr) 24px; gap: 4px; margin-bottom: 5px; }.business-actions { display: flex; flex-wrap: wrap; gap: 7px; }.business-actions button { font-size: var(--fs-xs); }.business-error { color: var(--error); font-size: var(--fs-xs); }
</style>
