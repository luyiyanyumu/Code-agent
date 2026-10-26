<script setup>
import UiIcon from '../ui/UiIcon.vue'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../api.js'
const props = defineProps({ projectId: { type: String, default: '' }, open: Boolean, initialMode: { type: String, default: 'files' } })
const emit = defineEmits(['close', 'open-file', 'mode-change'])
const mode = ref('files'), query = ref(''), include = ref(''), caseSensitive = ref(false)
const files = ref([]), fileData = ref(null), data = ref(null), busy = ref(false), error = ref('')
const active = ref(0), navigating = ref(false), input = ref(null), list = ref(null), dialog = ref(null), fileTab = ref(null), contentTab = ref(null)
let generation = 0, timer = null, previousFocus = null, resetting = false, closing = false, catalogLoaded = false, remoteCatalog = false
const normalizeMode = value => value === 'content' ? 'content' : 'files'
function score(path, value) {
  const text = path.toLowerCase(), q = value.toLowerCase().trim()
  if (!q) return 1
  const basename = text.split('/').pop()
  if (basename === q) return 1000
  if (basename.startsWith(q)) return 800 - path.length / 100
  if (text.includes(q)) return 500 - text.indexOf(q)
  let cursor = 0, hits = 0
  for (const letter of q) { cursor = text.indexOf(letter, cursor); if (cursor < 0) return 0; hits += cursor; cursor++ }
  return Math.max(1, 100 - hits / 100)
}
const rankedFiles = computed(() => files.value.map(file => ({ ...file, score: score(file.path, query.value) })).filter(file => file.score).sort((a, b) => b.score - a.score || a.path.localeCompare(b.path)))
const matches = computed(() => rankedFiles.value.slice(0, 100))
const groups = computed(() => {
  const out = new Map()
  for (const match of data.value?.matches || []) { if (!out.has(match.path)) out.set(match.path, []); out.get(match.path).push(match) }
  let index = 0
  return [...out].map(([path, rows]) => ({ path, matches: rows.map(match => ({ ...match, resultIndex: index++ })) }))
})
const results = computed(() => mode.value === 'files' ? matches.value : groups.value.flatMap(group => group.matches))
const selectedId = computed(() => !busy.value && !error.value && results.value.length && (mode.value === 'files' || navigating.value) ? `project-search-result-${active.value}` : undefined)
const fileLimitNote = computed(() => {
  if (fileData.value?.inventory_truncated) return `项目文件扫描达到${fileData.value.inventory_limit ? ` ${fileData.value.inventory_limit} 项` : ''}上限，请缩小项目范围。`
  if (fileData.value?.truncated) return `文件清单达到${fileData.value.limit ? ` ${fileData.value.limit} 项` : ''}上限，输入更具体的路径继续查找。`
  if (rankedFiles.value.length > matches.value.length) return `显示前 ${matches.value.length} 项，共 ${rankedFiles.value.length} 个匹配，请缩小范围。`
  return ''
})
const truncationNote = computed(() => {
  if (!data.value?.truncated && !data.value?.inventory_truncated) return ''
  const result = data.value
  const reasons = { matches: `匹配达到 ${result.limit || 300} 项上限`, files: `扫描达到 ${result.limits?.files || 5000} 个文件上限`, bytes: result.limits?.bytes ? `扫描达到 ${Math.round(result.limits.bytes / 1024 / 1024)} MB 上限` : '扫描达到字节上限', inventory: '项目文件清单达到扫描上限' }
  return `${reasons[result.truncation_reason] || '搜索达到上限'}，请缩小文件范围。`
})
function invalidate() { generation++; clearTimeout(timer); busy.value = false; error.value = ''; active.value = 0; navigating.value = false }
function resetResults() { files.value = []; fileData.value = null; data.value = null; catalogLoaded = false; remoteCatalog = false }
function isCurrent(mine, pid, searchMode, value) { return mine === generation && props.open && !closing && pid === props.projectId && mode.value === searchMode && query.value === value }
async function loadFiles(filter = '') {
  if (!props.open || !props.projectId || mode.value !== 'files' || closing) return
  const mine = ++generation, pid = props.projectId, value = query.value
  busy.value = true; error.value = ''
  try {
    const result = await api.code.files(pid, { query: filter, limit: 2000 })
    if (!isCurrent(mine, pid, 'files', value)) return
    files.value = result.files || []; fileData.value = result; catalogLoaded = true
    remoteCatalog = remoteCatalog || !!(filter || result.truncated || result.inventory_truncated)
    if (!filter && value.trim() && remoteCatalog) timer = setTimeout(() => loadFiles(query.value.trim()), 180)
  } catch (e) { if (isCurrent(mine, pid, 'files', value)) error.value = e.message || '读取文件失败，请重试。' }
  finally { if (mine === generation) busy.value = false }
}
async function search() {
  if (!props.open || !props.projectId || mode.value !== 'content' || !query.value.trim() || closing || busy.value) return
  const mine = ++generation, pid = props.projectId, value = query.value
  busy.value = true; error.value = ''; data.value = null; active.value = 0; navigating.value = false
  try {
    const result = await api.code.search(pid, { query: value, include: include.value, case_sensitive: caseSensitive.value, limit: 300 })
    if (isCurrent(mine, pid, 'content', value)) data.value = result
  } catch (e) { if (isCurrent(mine, pid, 'content', value)) error.value = e.message || '搜索失败，请重试。' }
  finally { if (mine === generation) busy.value = false }
}
function choose(file) {
  if (!file || busy.value) return
  emit('open-file', { path: file.path, ...(mode.value === 'content' ? { line: file.line, column: file.column } : {}) }); close()
}
function close() { closing = true; invalidate(); emit('close') }
function move(delta) {
  if (busy.value || !results.value.length) return
  active.value = navigating.value || mode.value === 'files' ? (active.value + delta + results.value.length) % results.value.length : delta < 0 ? results.value.length - 1 : 0
  navigating.value = true
  nextTick(() => list.value?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }))
}
function enter() { if (mode.value === 'files' || navigating.value) choose(results.value[active.value]); else search() }
function inputEnter(event) { if (event.isComposing || event.keyCode === 229) return; event.preventDefault(); enter() }
function setMode(value) {
  const next = normalizeMode(value)
  if (mode.value === next) return
  invalidate(); mode.value = next; emit('mode-change', next); resetResults()
  if (props.open && next === 'files') loadFiles()
  nextTick(() => input.value?.focus())
}
function tabKey(event) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const next = event.key === 'Home' ? 'files' : event.key === 'End' ? 'content' : mode.value === 'files' ? 'content' : 'files'
  setMode(next); nextTick(() => (next === 'files' ? fileTab.value : contentTab.value)?.focus())
}
function dialogKey(event) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
  if (event.key !== 'Tab') return
  const focusable = [...(dialog.value?.querySelectorAll('button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), [tabindex="0"]') || [])]
  if (!focusable.length) return
  const first = focusable[0], last = focusable[focusable.length - 1]
  if (event.shiftKey && (document.activeElement === first || !dialog.value.contains(document.activeElement))) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && (document.activeElement === last || !dialog.value.contains(document.activeElement))) { event.preventDefault(); first.focus() }
}
watch([() => props.open, () => props.projectId], async ([open], previous) => {
  resetting = true; invalidate(); resetResults()
  if (!open) {
    resetting = false
    if (previous?.[0]) { await nextTick(); if (previousFocus?.isConnected) previousFocus.focus(); previousFocus = null }
    return
  }
  if (!previous?.[0]) previousFocus = document.activeElement
  closing = false; query.value = ''; include.value = ''; caseSensitive.value = false; mode.value = normalizeMode(props.initialMode)
  resetting = false
  await nextTick(); input.value?.focus()
  if (mode.value === 'files') loadFiles()
}, { immediate: true })
watch(() => props.initialMode, value => { if (props.open) setMode(value) })
watch(query, () => {
  if (resetting) return
  invalidate(); data.value = null
  if (props.open && mode.value === 'files' && (remoteCatalog || !catalogLoaded)) timer = setTimeout(() => loadFiles(query.value.trim()), 180)
}, { flush: 'sync' })
watch([include, caseSensitive], () => { if (!resetting && mode.value === 'content') { invalidate(); data.value = null } }, { flush: 'sync' })
onBeforeUnmount(() => { invalidate(); if (props.open && previousFocus?.isConnected) previousFocus.focus() })
</script>

<template>
  <div v-if="open" class="quick-shade" @click.self="close">
    <section ref="dialog" class="quick-open" role="dialog" aria-modal="true" aria-labelledby="project-search-title" @keydown="dialogKey">
      <header class="quick-head"><b id="project-search-title">搜索项目</b><button class="close-button" type="button" aria-label="关闭搜索" title="关闭（Esc）" @click="close"><UiIcon name="close" :size="17" /></button></header>
      <div class="search-tabs" role="tablist" aria-label="搜索类型" @keydown="tabKey">
        <button id="project-search-files-tab" ref="fileTab" type="button" role="tab" :aria-selected="mode === 'files'" aria-controls="project-search-panel" :tabindex="mode === 'files' ? 0 : -1" :class="{ selected: mode === 'files' }" @click="setMode('files')"><UiIcon name="file" :size="16" />文件<kbd>Ctrl+P</kbd></button>
        <button id="project-search-content-tab" ref="contentTab" type="button" role="tab" :aria-selected="mode === 'content'" aria-controls="project-search-panel" :tabindex="mode === 'content' ? 0 : -1" :class="{ selected: mode === 'content' }" @click="setMode('content')"><UiIcon name="search" :size="16" />全文<kbd>Ctrl+Shift+F</kbd></button>
      </div>
      <section id="project-search-panel" class="search-body" role="tabpanel" :aria-labelledby="mode === 'files' ? 'project-search-files-tab' : 'project-search-content-tab'">
        <form class="search-form" @submit.prevent="mode === 'content' ? search() : enter()">
          <div class="quick-input"><UiIcon name="search" :size="18" /><input ref="input" v-model="query" :placeholder="mode === 'files' ? '输入文件名或路径…' : '搜索项目中的文本…'" :aria-label="mode === 'files' ? '搜索文件名或路径' : '搜索文件内容'" role="combobox" aria-expanded="true" aria-controls="project-search-results" :aria-autocomplete="mode === 'files' ? 'list' : 'none'" :aria-activedescendant="selectedId" maxlength="512" autocomplete="off" spellcheck="false" @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)" @keydown.enter="inputEnter" /><button v-if="mode === 'content'" class="search-button" type="submit" :disabled="busy || !projectId || !query.trim()">{{ busy ? '搜索中' : '搜索' }}</button><kbd v-else>Esc</kbd></div>
          <div v-if="mode === 'content'" class="search-options"><input v-model="include" class="include-input" aria-label="搜索文件范围" placeholder="文件范围，例如 web/**/*.vue" maxlength="512" /><label><input v-model="caseSensitive" type="checkbox" />区分大小写</label></div>
        </form>
        <p v-if="!projectId" class="empty" role="status">请先打开项目。</p>
        <p v-else-if="error" class="error" role="alert">{{ error }}<button type="button" @click="mode === 'files' ? loadFiles(query.trim()) : search()">重试</button></p>
        <p v-else-if="busy" class="empty" role="status">{{ mode === 'files' ? '正在读取项目文件…' : '正在搜索项目内容…' }}</p>
        <template v-else>
          <p v-if="mode === 'content' && !data" class="search-hint" role="status">输入文本并按 Enter 搜索。搜索已保存的文件，忽略生成目录、二进制和过大文件。</p>
          <p v-if="mode === 'content' && data" class="result-summary" role="status">{{ data.matches?.length || 0 }} 个匹配 · 扫描 {{ data.files_scanned || 0 }} 个文件<span v-if="truncationNote" class="limit-note">{{ truncationNote }}</span></p>
        </template>
        <div id="project-search-results" ref="list" class="quick-results" role="listbox" :aria-label="mode === 'files' ? '匹配文件' : '全文搜索结果'" :aria-busy="busy">
          <template v-if="!busy && !error && projectId">
            <template v-if="mode === 'files'">
              <button v-for="(file, index) in matches" :id="`project-search-result-${index}`" :key="file.path" type="button" role="option" tabindex="-1" :aria-selected="active === index" :class="{ active: active === index }" :title="file.path" @mousemove="active = index; navigating = true" @click="choose(file)"><UiIcon name="file" :size="16" /><span class="file-name">{{ file.path.split('/').pop() }}</span><small class="file-path">{{ file.path }}</small></button>
            </template>
            <template v-else>
              <section v-for="group in groups" :key="group.path" role="group" :aria-label="group.path" class="content-group"><h4 :title="group.path"><UiIcon name="file-code" :size="15" /><span>{{ group.path }}</span><small>{{ group.matches.length }}</small></h4><button v-for="match in group.matches" :id="`project-search-result-${match.resultIndex}`" :key="`${match.line}:${match.column}:${match.resultIndex}`" type="button" role="option" tabindex="-1" :aria-selected="navigating && active === match.resultIndex" :class="{ active: navigating && active === match.resultIndex }" :title="`${match.path}:${match.line}:${match.column}`" @mousemove="active = match.resultIndex; navigating = true" @click="choose(match)"><small class="line-number">{{ match.line }}</small><code>{{ match.text_start_column > 1 ? '… ' : '' }}{{ match.text }}</code></button></section>
            </template>
          </template>
        </div>
        <p v-if="projectId && !busy && !error && !results.length && (mode === 'files' || data)" class="empty" role="status">{{ mode === 'files' ? '没有匹配的文件' : '没有找到匹配内容' }}</p>
      </section>
      <footer><span v-if="mode === 'files' && fileLimitNote">{{ fileLimitNote }}</span><span v-else>{{ mode === 'files' ? '↑ ↓ 选择 · Enter 打开' : 'Enter 搜索 · ↑ ↓ 选择后 Enter 打开' }}</span><span class="escape-hint">Esc 关闭</span></footer>
    </section>
  </div>
</template>

<style scoped>
.quick-shade { position: absolute; inset: 0; z-index: 50; background: #20273545; backdrop-filter: blur(2px); display: flex; justify-content: center; align-items: flex-start; padding: 64px 16px 16px; overflow-y: auto; }
.quick-open { width: 720px; max-width: 100%; max-height: 100%; min-width: 0; display: flex; flex-direction: column; background: var(--panel); border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 18px 60px #20273530; overflow: hidden; }
.quick-head { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px 4px; flex: none; font-size: var(--fs-sm); }.close-button { display: grid; place-items: center; min-width: 30px; min-height: 30px; padding: 4px; background: transparent; border: 0; color: var(--muted); }.close-button:hover { background: var(--hover); }
.search-tabs { display: flex; gap: 4px; margin: 4px 12px 0; padding: 3px; border-radius: 7px; background: var(--panel-soft); flex: none; }.search-tabs button { display: flex; align-items: center; justify-content: center; gap: 7px; flex: 1; min-width: 0; min-height: 36px; padding: 6px 10px; border: 1px solid transparent; background: transparent; color: var(--muted); font-size: var(--fs-sm); }.search-tabs button.selected { background: var(--panel); border-color: var(--border); color: var(--accent); box-shadow: 0 1px 3px #2027350a; }.search-tabs kbd { margin-left: auto; font-size: var(--fs-2xs); }
.search-body { display: flex; flex-direction: column; min-height: 0; overflow: auto; }.search-form { flex: none; }.quick-input { display: flex; align-items: center; gap: 10px; margin: 12px; border: 1px solid var(--accent-border); background: var(--panel-soft); border-radius: 7px; padding: 6px 10px; }.quick-input > svg { color: var(--muted); flex: none; }.quick-input:focus-within { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }.quick-input input { width: 100%; min-width: 0; border: none; outline: none; box-shadow: none; padding: 5px 0; font-size: var(--fs-sm); background: transparent; }.search-button { flex: none; min-height: 32px; padding: 4px 12px; background: var(--accent); color: #fff; border-color: var(--accent); font-size: var(--fs-xs); }.search-button:disabled { opacity: .55; }kbd { flex: none; font-family: inherit; font-size: var(--fs-2xs); color: var(--muted); border: 1px solid var(--border-strong); border-radius: 4px; padding: 1px 5px; white-space: nowrap; }
.search-options { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; margin: -3px 12px 12px; }.include-input { flex: 1 1 240px; min-width: 0; max-width: 100%; padding: 7px 10px; min-height: 34px; font-size: var(--fs-xs); background: var(--panel); }.search-options label { display: flex; align-items: center; gap: 6px; white-space: nowrap; color: var(--muted); font-size: var(--fs-xs); }.search-options input[type='checkbox'] { margin: 0; }
.quick-results { max-height: min(420px, 50vh); overflow-y: auto; min-height: 0; padding: 0 8px; }.quick-results button { width: 100%; min-width: 0; display: flex; align-items: center; gap: 10px; min-height: 38px; border: 0; text-align: left; border-radius: 5px; padding: 8px 12px; font-size: var(--fs-sm); background: transparent; }.quick-results button.active, .quick-results button:hover { background: var(--accent-soft); color: var(--accent); }.file-name { flex: 0 1 auto; min-width: 0; max-width: 55%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.file-path { min-width: 0; margin-left: auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: var(--fs-2xs); }.content-group h4 { display: flex; align-items: center; gap: 7px; min-width: 0; margin: 5px 0 2px; padding: 9px 12px; background: var(--panel-soft); border-radius: 4px; font-size: var(--fs-xs); font-weight: var(--fw-medium); }.content-group h4 span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.content-group h4 small { margin-left: auto; color: var(--muted); }.line-number { flex: none; width: 38px; text-align: right; color: var(--muted); font-size: var(--fs-2xs); }.content-group code { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: pre; font-size: var(--fs-xs); }
.empty, .error { margin: 25px 20px; font-size: var(--fs-sm); line-height: 1.6; color: var(--muted); text-align: center; flex: none; }.error { color: var(--err); overflow-wrap: anywhere; }.error button { margin-left: 10px; font-size: var(--fs-xs); }.search-hint, .result-summary { margin: 0 16px 12px; color: var(--muted); line-height: 1.7; font-size: var(--fs-xs); flex: none; }.limit-note { display: block; color: var(--muted); }footer { padding: 10px 16px; margin-top: 10px; display: flex; justify-content: space-between; gap: 12px; border-top: 1px solid var(--border); font-size: var(--fs-2xs); line-height: 1.6; color: var(--muted); background: var(--panel-soft); flex: none; }footer > span { min-width: 0; overflow-wrap: anywhere; }.escape-hint { flex: none; }
button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }.quick-input input:focus-visible { outline: none; }
@media (max-width: 600px) { .quick-shade { padding: 24px 10px; }.search-tabs kbd { display: none; }.file-name { max-width: 100%; }.quick-results button:has(.file-name) { display: grid; grid-template-columns: 16px minmax(0, 1fr); gap: 3px 9px; }.file-path { max-width: 100%; width: 100%; grid-column: 2; margin: 0; }.search-tabs button { min-height: 40px; }.include-input { flex-basis: 100%; }.search-options { gap: 8px; }footer { gap: 8px; padding: 9px 12px; }.escape-hint { display: none; } }
</style>
