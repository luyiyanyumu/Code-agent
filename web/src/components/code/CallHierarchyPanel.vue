<script setup>
import { computed, markRaw, onBeforeUnmount, ref, toRaw, watch } from 'vue'
import { attachHierarchyChildren, flattenHierarchy, hierarchyCanExpand, hierarchyCancellation, hierarchyNode, HIERARCHY_MAX_DEPTH, HIERARCHY_MAX_VISIBLE } from '../../lib/hierarchyTree.js'

const props = defineProps({ client: { type: Object, default: null }, uri: { type: String, default: '' },
  position: { type: Object, default: () => ({ lineNumber: 1, column: 1 }) }, kind: { type: String, default: 'call' } })
const emit = defineEmits(['close', 'navigate'])
const direction = ref('incoming'), roots = ref([]), loading = ref(false), error = ref('')
const tokens = new Set()
let generation = 0, disposed = false
const isType = computed(() => props.kind === 'type')
const rows = computed(() => flattenHierarchy(roots.value))
const capability = computed(() => !!props.client?.capabilities?.[isType.value ? 'typeHierarchyProvider' : 'callHierarchyProvider'])
const labels = computed(() => isType.value ? ['父类型', '子类型'] : ['谁调用它', '它调用谁'])

function token() { const value = hierarchyCancellation(); tokens.add(value); return value }
function cancel() { for (const value of tokens) value.cancel(); tokens.clear() }
function close() { generation++; cancel(); emit('close') }
function path(item) {
  try { const parts = decodeURIComponent(new URL(item.uri).pathname).split('/').filter(Boolean); return parts.slice(-2).join('/') }
  catch { return item.uri || '' }
}
function navigate(node, range = node.item.selectionRange) {
  if (!/^file:\/\//i.test(node.item.uri)) return
  emit('navigate', { uri: node.item.uri, range, name: node.item.name })
}
async function expand(node) {
  if (!hierarchyCanExpand(node) || node.loading) return
  node.expanded = !node.expanded
  if (!node.expanded || node.loaded) return
  const current = generation, requestToken = token(), client = toRaw(props.client)
  node.loading = true; node.error = ''
  try {
    const result = await client.hierarchyChildren(toRaw(node.item), direction.value, requestToken)
    if (disposed || current !== generation || requestToken.isCancellationRequested) return
    const children = result.map(row => ({ ...row, item: markRaw(row.item) }))
    attachHierarchyChildren(node, children)
  } catch (err) {
    if (current === generation && !requestToken.isCancellationRequested) { node.error = err.message || '读取失败'; node.expanded = false }
  } finally { tokens.delete(requestToken); if (current === generation) node.loading = false }
}
async function refresh() {
  const current = ++generation
  cancel(); roots.value = []; loading.value = false; error.value = ''
  if (!props.client || !props.uri || !capability.value) return
  const requestToken = token(), client = toRaw(props.client)
  loading.value = true
  try {
    const items = await client.prepareHierarchy(props.uri, props.position, isType.value ? 'type' : 'call', requestToken)
    if (disposed || current !== generation || requestToken.isCancellationRequested) return
    roots.value = items.map(item => hierarchyNode(markRaw(item)))
    if (roots.value.length === 1) await expand(roots.value[0])
  } catch (err) {
    if (current === generation && !requestToken.isCancellationRequested) error.value = err.message || '层级查询失败'
  } finally { tokens.delete(requestToken); if (current === generation) loading.value = false }
}
watch(() => [props.client, props.uri, props.kind, props.position?.lineNumber, props.position?.column, direction.value], refresh, { immediate: true })
onBeforeUnmount(() => { disposed = true; generation++; cancel() })
</script>

<template>
  <aside class="hierarchy-panel" tabindex="-1" :aria-label="isType ? '类型层级' : '调用层级'" @keydown.esc.stop="close">
    <header class="hierarchy-header">
      <strong>{{ isType ? '类型层级' : '调用层级' }}</strong>
      <button type="button" class="hierarchy-refresh" :disabled="loading" aria-label="刷新层级" @click="refresh">↻</button>
      <button type="button" class="hierarchy-close" aria-label="关闭层级" @click="close">×</button>
    </header>
    <nav class="hierarchy-direction" aria-label="层级方向">
      <button type="button" :class="{ active: direction === 'incoming' }" :aria-pressed="direction === 'incoming'" @click="direction = 'incoming'">{{ labels[0] }}</button>
      <button type="button" :class="{ active: direction === 'outgoing' }" :aria-pressed="direction === 'outgoing'" @click="direction = 'outgoing'">{{ labels[1] }}</button>
    </nav>
    <div class="hierarchy-body" :aria-busy="loading">
      <p v-if="!capability" class="hierarchy-state">当前语言服务未提供此能力</p>
      <p v-else-if="error" class="hierarchy-state error" role="alert">{{ error }}</p>
      <p v-else-if="loading && !rows.length" class="hierarchy-state" role="status">查询层级…</p>
      <p v-else-if="!rows.length" class="hierarchy-state">当前位置没有可查询的符号</p>
      <div v-else role="tree" :aria-label="labels[direction === 'incoming' ? 0 : 1]">
        <div v-for="node in rows" :key="node.id" class="hierarchy-row-group">
          <div class="hierarchy-row" role="treeitem" :aria-level="node.depth + 1" :aria-expanded="hierarchyCanExpand(node) ? node.expanded : undefined" :style="{ paddingLeft: `${10 + Math.min(node.depth, 16) * 14}px` }">
            <button type="button" class="hierarchy-toggle" :disabled="!hierarchyCanExpand(node) || node.loading" :aria-label="`${node.expanded ? '折叠' : '展开'} ${node.item.name}`" @click="expand(node)">
              {{ node.loading ? '·' : hierarchyCanExpand(node) ? node.expanded ? '▾' : '▸' : '·' }}
            </button>
            <button type="button" class="hierarchy-symbol" :title="`${node.item.detail || node.item.name}\n${node.item.uri}`" :disabled="!/^file:\/\//i.test(node.item.uri)" @click="navigate(node)">
              <span class="hierarchy-name">{{ node.item.name }}</span><span v-if="node.item.detail" class="hierarchy-detail">{{ node.item.detail }}</span>
              <span class="hierarchy-path">{{ path(node.item) }}:{{ node.item.selectionRange.start.line + 1 }}</span>
            </button>
            <span v-if="node.cycle" class="hierarchy-note">循环</span>
            <span v-else-if="node.depth >= HIERARCHY_MAX_DEPTH" class="hierarchy-note">深度上限</span>
            <span v-else-if="node.ranges.length" class="hierarchy-note">{{ node.ranges.length }} 处</span>
          </div>
          <div v-if="node.expanded && node.ranges.length" class="hierarchy-calls" :style="{ paddingLeft: `${36 + Math.min(node.depth, 16) * 14}px` }">
            <button v-for="(range, index) in node.ranges.slice(0, 8)" :key="index" type="button" :disabled="!/^file:\/\//i.test(node.rangeUri)" @click="emit('navigate', { uri: node.rangeUri, range })">第 {{ range.start.line + 1 }} 行</button>
          </div>
          <p v-if="node.error" class="hierarchy-row-state error" role="alert">{{ node.error }} <button type="button" @click="expand(node)">重试</button></p>
          <p v-else-if="node.expanded && node.loaded && !node.children.length" class="hierarchy-row-state">{{ isType ? '没有关联类型' : '没有关联调用' }}</p>
          <p v-if="node.expanded && node.omitted" class="hierarchy-row-state">还有 {{ node.omitted }} 项，缩小查询范围后查看</p>
        </div>
      </div>
    </div>
    <footer class="hierarchy-footer">{{ rows.length >= HIERARCHY_MAX_VISIBLE ? '显示上限已达，折叠分支后继续查看' : '按需展开 · 点击符号定位' }}</footer>
  </aside>
</template>

<style scoped>
.hierarchy-panel { position: absolute; z-index: 31; top: 8px; right: 8px; bottom: 8px; width: min(440px, calc(100% - 16px)); display: flex; flex-direction: column; min-width: 0; overflow: hidden; background: var(--panel, #fff); color: var(--text); border: 1px solid var(--border); border-radius: 9px; box-shadow: var(--shadow-pop); font-size: 12px; }
.hierarchy-header { display: flex; align-items: center; gap: 6px; padding: 10px 12px 8px; }
.hierarchy-header strong { flex: 1; font-size: 13px; font-weight: 600; }
.hierarchy-header button { height: 25px; width: 25px; padding: 0; border: 0; border-radius: 5px; background: transparent; color: var(--muted); font-size: 19px; }
.hierarchy-header button:hover { background: var(--hover); color: var(--text); }
.hierarchy-direction { display: flex; gap: 3px; padding: 0 12px 9px; border-bottom: 1px solid var(--border); }
.hierarchy-direction button { flex: 1; border: 0; border-radius: 5px; background: var(--panel-soft); color: var(--muted); padding: 6px 8px; font-size: 12px; }
.hierarchy-direction button.active { color: var(--accent); background: var(--accent-soft, #eef3ff); font-weight: 600; }
.hierarchy-body { overflow: auto; flex: 1; min-height: 0; padding: 6px 0; overscroll-behavior: contain; scrollbar-width: thin; }
.hierarchy-state { padding: 8px 14px; margin: 0; line-height: 1.7; color: var(--muted); overflow-wrap: anywhere; }
.hierarchy-row { display: flex; align-items: flex-start; gap: 3px; min-width: 0; padding: 5px 10px; }
.hierarchy-row:hover { background: var(--hover); }
.hierarchy-toggle { flex: none; width: 19px; height: 22px; padding: 0; border: 0; background: transparent; color: var(--muted); font-size: 14px; }
.hierarchy-toggle:disabled { opacity: .5; }
.hierarchy-symbol { flex: 1; min-width: 0; display: flex; flex-wrap: wrap; align-items: baseline; justify-content: flex-start; gap: 0 6px; padding: 2px 0; background: transparent; border: 0; color: var(--text); text-align: left; font-size: 12px; }
.hierarchy-symbol:disabled { cursor: default; opacity: .75; }
.hierarchy-name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.hierarchy-detail { font-size: 11px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.hierarchy-path { color: var(--muted); font-size: 10px; flex-basis: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hierarchy-note { flex: none; padding-top: 4px; color: var(--muted); font-size: 10px; }
.hierarchy-row-state { margin: 0; padding: 2px 14px 7px 36px; color: var(--muted); font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.hierarchy-row-state button { border: 0; background: transparent; padding: 0 2px; color: var(--accent); font-size: inherit; }
.hierarchy-calls { display: flex; flex-wrap: wrap; gap: 4px; padding: 0 10px 5px; }
.hierarchy-calls button { border: 0; border-radius: 4px; background: var(--panel-soft); color: var(--muted); font-size: 10px; padding: 2px 5px; }
.hierarchy-calls button:hover { color: var(--accent); }
.hierarchy-footer { padding: 8px 12px; border-top: 1px solid var(--border); color: var(--muted); font-size: 10px; }
.error { color: var(--err); }
</style>
