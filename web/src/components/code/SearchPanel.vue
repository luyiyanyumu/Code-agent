<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../api.js'
const props = defineProps({ projectId: { type: String, default: '' } })
const emit = defineEmits(['open-file'])
const query = ref(''), include = ref(''), caseSensitive = ref(false), data = ref(null), busy = ref(false), error = ref('')
let generation = 0
const groups = computed(() => {
  const out = new Map()
  for (const match of data.value?.matches || []) { if (!out.has(match.path)) out.set(match.path, []); out.get(match.path).push(match) }
  return [...out].map(([path, matches]) => ({ path, matches }))
})
async function search() {
  if (!props.projectId || !query.value.trim()) return
  const mine = ++generation, pid = props.projectId
  busy.value = true; error.value = ''; data.value = null
  try {
    const result = await api.code.search(pid, { query: query.value, include: include.value, case_sensitive: caseSensitive.value, limit: 300 })
    if (mine === generation && pid === props.projectId) data.value = result
  } catch (e) { if (mine === generation) error.value = e.message }
  finally { if (mine === generation) busy.value = false }
}
watch(() => props.projectId, () => { generation++; data.value = null; error.value = ''; busy.value = false })
onBeforeUnmount(() => { generation++ })
</script>
<template>
  <section class="search-panel" aria-label="项目搜索">
    <div class="search-head"><b>项目搜索</b><kbd>Ctrl+Shift+F</kbd></div>
    <form @submit.prevent="search"><div class="search-field"><input v-model="query" placeholder="搜索项目中的文本" aria-label="全局搜索文本" maxlength="512" /><button :disabled="busy || !query.trim()">{{ busy ? '…' : '搜索' }}</button></div><input v-model="include" placeholder="文件范围，例如 frontend/**" aria-label="搜索文件范围" /><label><input v-model="caseSensitive" type="checkbox" />区分大小写</label></form>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-else-if="data" class="summary">{{ data.matches?.length || 0 }} 个匹配 · {{ data.files_scanned || 0 }} 个文件<span v-if="data.truncated || data.inventory_truncated">（达到上限，请缩小范围）</span></p>
    <p v-else class="summary">搜索已保存的文本，忽略生成目录、二进制和过大文件。</p>
    <div class="search-results"><section v-for="group in groups" :key="group.path"><h4 :title="group.path">{{ group.path }}</h4><button v-for="(match, index) in group.matches" :key="index" @click="emit('open-file', { path: match.path, line: match.line, column: match.column })"><small>{{ match.line }}</small><code>{{ match.text }}</code></button></section><p v-if="data && !groups.length" class="summary">没有找到匹配内容</p></div>
  </section>
</template>
<style scoped>
.search-panel { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }.search-head { display: flex; justify-content: space-between; align-items: center; min-height: 40px; flex: none; padding: 9px 14px; font-size: var(--fs-xs); }.search-head kbd { font-size: var(--fs-2xs); color: var(--muted); }
form { padding: 0 12px 4px; display: flex; flex-direction: column; gap: 8px; flex: none; }.search-field { display: flex; gap: 5px; }.search-field input { flex: 1; min-width: 0; }input:not([type='checkbox']) { min-height: 30px; padding: 6px 8px; font-size: var(--fs-xs); width: 100%; background: var(--panel); }.search-field button { flex: none; font-size: var(--fs-xs); padding: 4px 7px; }label { display: flex; align-items: center; gap: 6px; font-size: var(--fs-2xs); color: var(--muted); }
.summary, .error { margin: 12px 14px; font-size: var(--fs-xs); line-height: 1.7; color: var(--muted); flex: none; }.error { color: var(--err); }.search-results { overflow: auto; min-height: 0; flex: 1; }h4 { background: var(--hover); padding: 8px 14px; margin: 4px 0 0; font-size: var(--fs-xs); font-weight: var(--fw-medium); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-results button { width: 100%; display: flex; gap: 10px; min-height: 30px; padding: 6px 14px; border: 0; border-radius: 0; text-align: left; background: transparent; }.search-results button:hover { background: var(--accent-soft); }small { color: var(--muted); font-size: var(--fs-2xs); flex: none; }code { min-width: 0; font-size: var(--fs-xs); white-space: pre; overflow: hidden; text-overflow: ellipsis; }
</style>
