<script setup>
import UiIcon from '../ui/UiIcon.vue'
// 文件树：**按层懒加载**的扁平渲染（不是递归组件）。
//
// 用扁平列表 + 缩进而不是递归组件，是因为树的展开态需要与"已加载的子目录"
// 缓存同处一个容器里维护；递归组件会让这份状态分散到每层实例上，展开/折叠
// 与重新拉取时的行为会随层级变复杂。这里状态只有三个 map，行为好推理。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../../api.js'

const props = defineProps({
  projectId: { type: String, required: true },
  activePath: { type: String, default: null },
})
const emit = defineEmits(['open'])

const children = ref({})       // { [dirPath]: { dirs: [], files: [] } }
const expanded = ref({})       // { [dirPath]: true }
const loading = ref({})        // { [dirPath]: true }
const error = ref('')
const selected = ref('')
let generation = 0
let disposed = false
const requests = new Map()

function loadDir(path) {
  const key = path || ''
  if (disposed || !props.projectId) return Promise.resolve()
  if (requests.has(key)) return requests.get(key)
  const version = generation, projectId = props.projectId
  const current = () => !disposed && version === generation && projectId === props.projectId
  loading.value = { ...loading.value, [key]: true }
  error.value = ''
  const pending = Promise.resolve().then(() => api.code.tree(projectId, key)).then(body => {
    if (current()) {
      children.value = {
        ...children.value,
        [key]: { dirs: body.dirs || [], files: body.files || [], truncated: !!body.truncated },
      }
    }
  }).catch(e => { if (current()) error.value = e.message }).finally(() => {
    if (requests.get(key) === pending) requests.delete(key)
    if (current()) loading.value = { ...loading.value, [key]: false }
  })
  requests.set(key, pending)
  return pending
}

async function toggle(dirPath) {
  const next = { ...expanded.value, [dirPath]: !expanded.value[dirPath] }
  expanded.value = next
  if (next[dirPath] && !children.value[dirPath]) await loadDir(dirPath)
}

function openFile(file) {
  selected.value = file.path
  emit('open', file)
}

/** 把"哪些目录当前可见"算出来：只有父链全部展开的目录才参与渲染。 */
const rows = computed(() => {
  const out = []
  const walk = (dirPath, depth) => {
    const node = children.value[dirPath]
    if (!node) return
    for (const d of node.dirs) {
      out.push({ ...d, depth, dir: true })
      if (expanded.value[d.path]) walk(d.path, depth + 1)
    }
    for (const f of node.files) out.push({ ...f, depth, dir: false })
  }
  walk('', 0)
  return out
})

async function refresh() {
  generation += 1
  requests.clear()
  children.value = {}
  expanded.value = {}
  loading.value = {}
  selected.value = ''
  await loadDir('')
}

defineExpose({ refresh })

watch(() => props.projectId, refresh)
onMounted(refresh)
onBeforeUnmount(() => { disposed = true; generation += 1; requests.clear() })

function fmtSize(n) {
  if (!n) return ''
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${Math.round(n / 1024)}KB`
  return `${(n / 1024 / 1024).toFixed(1)}MB`
}

</script>

<template>
  <div class="tree">
    <div class="head">
      <span class="title">文件</span>
      <button class="ghost" title="重新加载" aria-label="重新加载文件树" @click="refresh"><UiIcon name="refresh" :size="14" /></button>
    </div>
    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="loading[''] && !children['']" class="muted loading">读取中…</div>
    <ul class="rows">
      <li
        v-for="row in rows"
        :key="row.path"
        class="row" role="button" tabindex="0"
        :aria-label="`${row.dir ? '文件夹' : '文件'} ${row.name}`" :aria-expanded="row.dir ? !!expanded[row.path] : undefined"
        @keydown.enter.prevent="row.dir ? toggle(row.path) : openFile(row)"
        @keydown.space.prevent="row.dir ? toggle(row.path) : openFile(row)"
        :class="{ selected: !row.dir && (activePath === null ? selected : activePath) === row.path }"
        :style="{ paddingLeft: `${6 + row.depth * 12}px` }"
        @click="row.dir ? toggle(row.path) : openFile(row)"
      >
        <span class="caret" :class="{ open: expanded[row.path] }"><UiIcon v-if="row.dir" name="chevron-right" :size="11" /></span>
        <UiIcon :name="row.dir ? (expanded[row.path] ? 'folder-open' : 'folder') : 'file'" :size="15" class="file-icon" :class="{ folder: row.dir }" />
        <span class="name" :title="row.path">{{ row.name }}</span>
        <span v-if="row.secret" class="badge secret" title="疑似含密钥：不会被索引，也不会随上下文发给模型">key</span>
        <span v-else-if="!row.dir" class="size muted">{{ fmtSize(row.size) }}</span>
      </li>
    </ul>
    <div v-if="!rows.length && !loading['']" class="muted loading">（空目录）</div>
  </div>
</template>

<style scoped>
.tree { display: flex; flex-direction: column; min-height: 0; flex: 1; min-width: 0; }
.head { display: flex; align-items: center; justify-content: space-between; min-height: 37px; padding: 0 14px; font-size: var(--fs-2xs); color: var(--muted); }
.title { font-weight: var(--fw-semibold); letter-spacing: .04em; }
.ghost { display: grid; place-items: center; border: none; background: transparent; width: 25px; height: 25px; min-height: 0; padding: 0; color: var(--muted); }
.ghost:hover { background: var(--hover); color: var(--text); }
.rows { list-style: none; margin: 0; padding: 0 6px 12px; overflow: auto; flex: 1; min-height: 0; }
.row { display: flex; align-items: center; gap: 5px; min-height: 29px; padding: 3px 8px; border-radius: 4px; font-size: var(--fs-sm); cursor: pointer; white-space: nowrap; color: var(--text-2); }
.row:hover { background: var(--hover); }.row.selected { background: var(--accent-soft); color: var(--accent); box-shadow: inset 2px 0 var(--accent); }
.row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.caret { width: 11px; display: flex; flex: none; color: var(--muted); transition: transform .12s; }.caret.open { transform: rotate(90deg); }
.file-icon { flex: none; color: var(--faint); }.file-icon.folder { color: #8b9bb2; }.row.selected .file-icon { color: var(--accent); }
.name { overflow: hidden; text-overflow: ellipsis; }.size { font-size: var(--fs-2xs); flex: none; margin-left: auto; color: var(--faint); padding-left: 6px; }
.badge.secret { flex: none; margin-left: auto; font-size: var(--fs-2xs); padding: 0 5px; border-radius: 3px; background: var(--warn-soft); color: var(--warn); }
.error { margin: 4px 10px; padding: 8px; border-radius: 5px; background: var(--err-soft); font-size: var(--fs-xs); color: var(--err); overflow-wrap: anywhere; }
.loading { padding: 16px; font-size: var(--fs-xs); text-align: center; }
@media (prefers-reduced-motion: reduce) { .caret { transition: none; } }
</style>
