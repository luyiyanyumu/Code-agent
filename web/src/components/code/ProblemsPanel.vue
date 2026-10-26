<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../api.js'
const props = defineProps({ projectId: { type: String, default: '' }, tree: { type: String, default: 'main' }, seq: { type: Number, default: 0 }, visible: Boolean })
const emit = defineEmits(['open-file'])
const rows = ref([]), severity = ref(''), query = ref(''), error = ref(''), busy = ref(false)
const total = ref(0), truncated = ref(false)
let generation = 0
async function load() {
  if (!props.visible || !props.projectId) return
  const mine = ++generation, pid = props.projectId, tree = props.tree
  busy.value = true; error.value = ''
  try {
    const data = await api.code.diagnostics(pid, { tree, details: true, severity: severity.value, limit: 1000 })
    if (mine !== generation || pid !== props.projectId || tree !== props.tree) return
    rows.value = data.items || []
    total.value = data.total || rows.value.length; truncated.value = !!data.truncated
  } catch (e) { if (mine === generation) error.value = e.message }
  finally { if (mine === generation) busy.value = false }
}
const filtered = computed(() => rows.value.filter(row => (!severity.value || Number(row.severity) === Number(severity.value)) && `${row.path} ${row.message}`.toLowerCase().includes(query.value.toLowerCase())).sort((a, b) => a.severity - b.severity || a.path.localeCompare(b.path) || a.line - b.line))
watch([() => props.projectId, () => props.tree], () => { generation++; rows.value = []; total.value = 0; truncated.value = false; busy.value = false; error.value = ''; load() })
watch([() => props.visible, () => props.seq], load, { immediate: true })
watch(severity, load)
onBeforeUnmount(() => { generation++ })
defineExpose({ load })
</script>
<template>
  <div class="problems-panel">
    <div class="problem-tools"><input v-model="query" placeholder="筛选文件或问题…" aria-label="筛选诊断" /><select v-model="severity" aria-label="诊断级别"><option value="">全部级别</option><option value="1">错误</option><option value="2">警告</option><option value="3">信息</option><option value="4">提示</option></select><span>{{ filtered.length }} / {{ total }} 条</span><button title="刷新诊断" @click="load">↻</button></div>
    <p v-if="truncated" class="empty">当前仅显示前 1000 条，可按级别筛选缩小范围。</p>
    <p v-if="error" class="error">{{ error }}</p><p v-else-if="busy" class="empty">读取问题…</p><p v-else-if="!filtered.length" class="empty">{{ query || severity ? '没有匹配的问题' : '当前没有语言服务器诊断' }}</p>
    <div class="problem-list"><button v-for="(row, index) in filtered" :key="`${row.path}:${row.line}:${index}`" class="problem-row" @click="emit('open-file', { path: row.path, line: row.line, column: row.column || 1 })"><span class="severity" :class="{ err: Number(row.severity) === 1, warning: Number(row.severity) === 2 }">{{ Number(row.severity) === 1 ? '●' : Number(row.severity) === 2 ? '▲' : 'ⓘ' }}</span><span class="message">{{ row.message }}</span><small>{{ row.path }}:{{ row.line }} · {{ row.source || 'LSP' }}</small></button></div>
  </div>
</template>
<style scoped>
.problems-panel { display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
.problem-tools { display: flex; flex: none; align-items: center; gap: 8px; padding: 8px 12px; font-size: var(--fs-2xs); border-bottom: 1px solid var(--border); }.problem-tools input { flex: 1; min-width: 0; max-width: 500px; padding: 5px 8px; font-size: var(--fs-xs); }.problem-tools select, .problem-tools button { font-size: var(--fs-xs); min-height: 28px; padding: 4px 8px; }.problem-tools > span { flex: none; color: var(--muted); }
.problem-list { overflow: auto; flex: 1; min-height: 0; }.problem-row { display: flex; align-items: baseline; gap: 10px; width: 100%; min-height: 31px; border: 0; border-bottom: 1px solid var(--panel-soft); border-radius: 0; background: transparent; text-align: left; padding: 6px 14px; font-size: var(--fs-xs); }.problem-row:hover { background: var(--hover); }.message { flex: 1; min-width: 0; overflow-wrap: anywhere; }.problem-row small { color: var(--muted); font-size: var(--fs-2xs); flex: 0 1 auto; max-width: 48%; overflow-wrap: anywhere; }.severity { color: var(--muted); }.severity.err { color: var(--err); }.severity.warning { color: var(--warn); }.empty, .error { font-size: var(--fs-xs); margin: 24px 18px; color: var(--muted); text-align: center; }.error { color: var(--err); }
@media (max-width: 760px) { .problem-row { flex-wrap: wrap; gap: 4px 7px; }.problem-row small { width: 100%; max-width: none; padding-left: 19px; }.problem-tools > span { display: none; }.problem-tools { gap: 5px; }.problem-tools select { max-width: 104px; } }
</style>
