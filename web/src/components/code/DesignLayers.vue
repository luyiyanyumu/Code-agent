<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import UiIcon from '../ui/UiIcon.vue'

const props = defineProps({ layers: { type: Array, default: () => [] }, selectedSelector: { type: String, default: '' }, loading: { type: Boolean, default: false }, truncated: { type: Boolean, default: false }, disabled: { type: Boolean, default: false } })
const emit = defineEmits(['select', 'refresh'])
const query = ref(''), collapsed = ref(new Set()), focusedSelector = ref('')
const rowElements = new Map()
let initializedLargeTree = false, manualExpansion = false
const index = computed(() => {
  const bySelector = new Map(), nodes = []
  for (const source of (props.layers || []).slice(0, 800)) {
    if (!source || typeof source.selector !== 'string' || !source.selector || bySelector.has(source.selector)) continue
    const node = { ...source, name: String(source.name || source.tag || source.selector), children: [] }
    bySelector.set(node.selector, node); nodes.push(node)
  }
  const roots = []
  for (const node of nodes) {
    const parent = node.parentSelector !== node.selector && bySelector.get(node.parentSelector)
    if (parent) parent.children.push(node)
    else roots.push(node)
  }
  return { nodes, roots, bySelector }
})
function ancestorSelectors(selector) {
  const ancestors = [], seen = new Set([selector])
  let node = index.value.bySelector.get(selector)
  while (node?.parentSelector && !seen.has(node.parentSelector)) {
    const parent = index.value.bySelector.get(node.parentSelector)
    if (!parent) break
    ancestors.push(parent.selector); seen.add(parent.selector); node = parent
  }
  return ancestors
}
const matching = computed(() => {
  const search = query.value.trim().toLocaleLowerCase()
  if (!search) return null
  const included = new Set()
  for (const node of index.value.nodes) if (`${node.name} ${node.tag || ''} ${node.selector}`.toLocaleLowerCase().includes(search)) {
    included.add(node.selector)
    for (const ancestor of ancestorSelectors(node.selector)) included.add(ancestor)
  }
  return included
})
const rows = computed(() => {
  const output = [], seen = new Set(), included = matching.value
  function walk(start, depth = 0) {
    const pending = [{ node: start, depth }]
    while (pending.length) {
      const { node, depth: level } = pending.pop()
      if (seen.has(node.selector)) continue
      seen.add(node.selector)
      if (included && !included.has(node.selector)) continue
      const children = node.children.filter(child => !included || included.has(child.selector))
      output.push({ ...node, depth: level, expandable: node.children.length > 0, expanded: !!included || !collapsed.value.has(node.selector), shownChildren: children.length })
      if (included || !collapsed.value.has(node.selector)) for (let i = children.length - 1; i >= 0; i--) pending.push({ node: children[i], depth: level + 1 })
      else {
        const hidden = [...node.children]
        while (hidden.length) { const child = hidden.pop(); if (seen.has(child.selector)) continue; seen.add(child.selector); hidden.push(...child.children) }
      }
    }
  }
  for (const root of index.value.roots) walk(root)
  // A malformed/cyclic incoming graph cannot hang the panel or hide all nodes.
  for (const node of index.value.nodes) if (!seen.has(node.selector)) walk(node)
  return output
})
const tabStop = computed(() => rows.value.some(row => row.selector === focusedSelector.value) ? focusedSelector.value
  : rows.value.some(row => row.selector === props.selectedSelector) ? props.selectedSelector : rows.value[0]?.selector || '')
function revealSelected() {
  const next = new Set(collapsed.value)
  for (const selector of ancestorSelectors(props.selectedSelector)) next.delete(selector)
  collapsed.value = next
}
function scrollSelectionIntoView(selector) {
  nextTick(() => {
    if (selector && props.selectedSelector === selector) rowElements.get(selector)?.scrollIntoView?.({ block: 'nearest' })
  })
}
watch([() => props.selectedSelector, () => props.layers], ([selector], previous) => {
  if (previous && selector !== previous[0] && index.value.bySelector.has(selector) && matching.value && !matching.value.has(selector)) query.value = ''
  collapsed.value = new Set([...collapsed.value].filter(selector => index.value.bySelector.has(selector)))
  if (!index.value.nodes.length) { initializedLargeTree = false; manualExpansion = false }
  if (!initializedLargeTree && !manualExpansion && index.value.nodes.length > 150) {
    initializedLargeTree = true
    const next = new Set(collapsed.value)
    for (const node of index.value.nodes) if (node.children.length && ancestorSelectors(node.selector).length >= 2) next.add(node.selector)
    collapsed.value = next
  }
  revealSelected()
  if (index.value.bySelector.has(props.selectedSelector)) focusedSelector.value = props.selectedSelector
  if (selector && previous?.length && selector !== previous[0] && index.value.bySelector.has(selector)) scrollSelectionIntoView(selector)
}, { immediate: true })
onMounted(() => scrollSelectionIntoView(props.selectedSelector))
function toggle(row) {
  if (props.disabled || !row.expandable || matching.value) return
  manualExpansion = true
  const next = new Set(collapsed.value)
  if (next.has(row.selector)) next.delete(row.selector)
  else next.add(row.selector)
  collapsed.value = next
}
function select(row) {
  if (props.disabled) return
  focusedSelector.value = row.selector
  emit('select', row.selector)
}
function focusRow(selector) {
  if (!selector) return
  focusedSelector.value = selector
  nextTick(() => { const element = rowElements.get(selector); element?.focus?.({ preventScroll: true }); element?.scrollIntoView?.({ block: 'nearest' }) })
}
function treeKey(event, row) {
  if (props.disabled || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return
  const position = rows.value.findIndex(item => item.selector === row.selector)
  if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' '].includes(event.key)) return
  event.preventDefault(); event.stopPropagation()
  if (event.key === 'ArrowDown') focusRow(rows.value[Math.min(rows.value.length - 1, position + 1)]?.selector)
  else if (event.key === 'ArrowUp') focusRow(rows.value[Math.max(0, position - 1)]?.selector)
  else if (event.key === 'Home') focusRow(rows.value[0]?.selector)
  else if (event.key === 'End') focusRow(rows.value.at(-1)?.selector)
  else if (event.key === 'ArrowRight') {
    if (row.expandable && !row.expanded) toggle(row)
    else if (rows.value[position + 1]?.depth > row.depth) focusRow(rows.value[position + 1].selector)
  } else if (event.key === 'ArrowLeft') {
    if (row.expandable && row.expanded && !matching.value) toggle(row)
    else if (rows.value.some(item => item.selector === row.parentSelector)) focusRow(row.parentSelector)
  } else select(row)
}
function collapseAll() {
  if (props.disabled) return
  manualExpansion = true
  collapsed.value = new Set(index.value.nodes.filter(node => node.children.length).map(node => node.selector))
  revealSelected()
}
function expandAll() { if (!props.disabled) { manualExpansion = true; collapsed.value = new Set() } }
function setRowRef(selector, element) { if (element) rowElements.set(selector, element); else rowElements.delete(selector) }
onBeforeUnmount(() => rowElements.clear())
</script>

<template>
  <section class="design-layers" aria-label="页面图层">
    <header class="layers-heading"><strong>图层</strong><span>{{ rows.length }} / {{ index.nodes.length }}</span><button type="button" class="refresh-layers" :disabled="loading || disabled" aria-label="刷新图层" title="重新读取当前页面图层" @click="emit('refresh')"><UiIcon name="refresh" :size="14" /></button></header>
    <label class="layer-search"><UiIcon name="search" :size="13" /><input v-model="query" type="search" placeholder="搜索图层" aria-label="搜索图层" :disabled="disabled" /></label>
    <div class="layer-actions"><button type="button" :disabled="disabled || !rows.length || !!matching" @click="expandAll">展开全部</button><button type="button" :disabled="disabled || !rows.length || !!matching" @click="collapseAll">折叠全部</button></div>
    <p v-if="loading" class="layers-notice" role="status">正在读取页面图层…</p>
    <div class="layer-tree" role="tree" aria-label="页面元素" :aria-busy="loading">
      <div v-for="row in rows" :key="row.selector" :ref="element => setRowRef(row.selector, element)" class="layer-row" :class="{ selected: row.selector === selectedSelector, hidden: row.hidden }" role="treeitem" :tabindex="!disabled && row.selector === tabStop ? 0 : -1" :aria-level="row.depth + 1" :aria-selected="row.selector === selectedSelector" :aria-expanded="row.expandable ? row.expanded : undefined" :aria-disabled="disabled || undefined" :style="{ paddingLeft: `${8 + Math.min(row.depth, 12) * 13}px` }" :title="row.selector" :data-selector="row.selector" @click="select(row)" @focus="focusedSelector = row.selector" @keydown="treeKey($event, row)">
        <button v-if="row.expandable" type="button" class="layer-chevron" :class="{ expanded: row.expanded }" tabindex="-1" :disabled="disabled || !!matching" :aria-label="`${row.expanded ? '折叠' : '展开'} ${row.name}`" @click.stop="toggle(row)"><UiIcon name="chevron-right" :size="12" /></button><span v-else class="chevron-space" />
        <UiIcon :name="row.expandable ? 'layers' : 'file-code'" :size="13" /><span class="layer-name">{{ row.name }}</span><small v-if="row.hidden" class="hidden-label">隐藏</small>
      </div>
    </div>
    <p v-if="!rows.length && !loading" class="layers-empty">{{ query.trim() ? '没有匹配的图层' : '载入页面后显示真实页面图层。' }}</p>
    <p v-if="truncated || layers.length > 800" class="layers-notice" role="status">仅显示前 800 个图层，可在画布直接选择其他元素。</p>
  </section>
</template>

<style scoped>
.design-layers { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; background: var(--panel); color: var(--text); font-size: var(--fs-xs, 12px); }.layers-heading { display: flex; align-items: center; gap: 7px; padding: 13px 11px 10px; flex: none; }.layers-heading strong { font-size: 12px; font-weight: 600; }.layers-heading > span { flex: 1; color: var(--muted); font-size: 10px; }.refresh-layers { display: grid; place-items: center; padding: 3px; min-width: 23px; min-height: 23px; border: 0; background: transparent; color: var(--muted); }
.layer-search { margin: 0 10px; display: flex; align-items: center; gap: 5px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel-soft); color: var(--muted); padding: 6px 7px; }.layer-search input { width: 100%; min-width: 0; padding: 0; border: 0; background: transparent; font-size: 11px; color: var(--text); outline: none; }.layer-search:focus-within { border-color: var(--accent-border); box-shadow: var(--focus-ring); }.layer-actions { display: flex; gap: 8px; padding: 7px 10px 9px; border-bottom: 1px solid var(--border); }.layer-actions button { padding: 2px 0; background: transparent; border: 0; color: var(--muted); font-size: 10px; }
.layer-tree { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 5px 0; }.layer-row { display: flex; align-items: center; gap: 5px; min-height: 30px; padding: 4px 9px; box-sizing: border-box; cursor: pointer; white-space: nowrap; font-size: 11px; border: 1px solid transparent; }.layer-row:hover { background: var(--panel-soft); }.layer-row.selected { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-border); }.layer-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }.layer-row[aria-disabled="true"] { cursor: default; }.layer-row > .ui-icon { color: var(--muted); }.layer-row.selected > .ui-icon { color: var(--accent); }.layer-row.hidden .layer-name { color: var(--muted); }.layer-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }.hidden-label { color: var(--muted); font-size: 9px; }.layer-chevron { display: grid; place-items: center; flex: none; width: 14px; height: 18px; min-height: 0; padding: 0; border: 0; background: transparent; color: inherit; }.layer-chevron.expanded > svg { transform: rotate(90deg); }.chevron-space { width: 14px; flex: none; }
.layers-notice, .layers-empty { font-size: 10px; line-height: 1.7; color: var(--muted); padding: 9px 12px; margin: 0; }.layers-empty { text-align: center; padding-top: 28px; }.layers-notice { flex: none; }.layer-actions button:disabled, .refresh-layers:disabled { opacity: .45; cursor: default; }
</style>
