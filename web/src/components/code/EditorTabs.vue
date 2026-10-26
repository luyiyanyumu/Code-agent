<script setup>
import UiIcon from '../ui/UiIcon.vue'
import { computed, nextTick, watch } from 'vue'

const props = defineProps({
  documents: { type: Array, default: () => [] },
  activeKey: { type: String, default: '' },
})
const emit = defineEmits(['select', 'close'])
const tabButtons = new Map()
const basename = (path) => String(path || '').split('/').pop()
const names = computed(() => {
  const counts = new Map()
  for (const document of props.documents) {
    const name = basename(document.path)
    counts.set(name, (counts.get(name) || 0) + 1)
  }
  return counts
})
const parentPath = (path) => String(path || '').split('/').slice(0, -1).join('/') || '根目录'
const tabTitle = (document) => `${document.path}${document.dirty ? ' · 未保存' : ''}${document.conflict ? ' · 磁盘版本已变化' : ''}`

function setButton(key, element) {
  if (element) tabButtons.set(key, element)
  else tabButtons.delete(key)
}

function onKeydown(event, document) {
  const index = props.documents.findIndex((item) => item.key === document.key)
  let nextIndex = index
  if (event.key === 'ArrowRight') nextIndex = (index + 1) % props.documents.length
  else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + props.documents.length) % props.documents.length
  else if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = props.documents.length - 1
  else if (event.key === 'Delete') {
    event.preventDefault()
    emit('close', document.key)
    return
  } else return
  event.preventDefault()
  const next = props.documents[nextIndex]
  if (next) {
    emit('select', next.key)
    tabButtons.get(next.key)?.focus()
  }
}

watch(() => props.activeKey, async (key) => {
  await nextTick()
  tabButtons.get(key)?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}, { immediate: true })
</script>

<template>
  <div v-if="documents.length" class="editor-tabs" role="tablist" aria-label="打开的文件">
    <div
      v-for="document in documents" :key="document.key"
      class="editor-tab" :class="{ active: document.key === activeKey, conflict: !!document.conflict }"
    >
      <button
        :ref="(element) => setButton(document.key, element)"
        type="button" class="tab-select" role="tab"
        :aria-selected="document.key === activeKey"
        :tabindex="document.key === activeKey ? 0 : -1"
        :title="tabTitle(document)" @click="emit('select', document.key)"
        @keydown="onKeydown($event, document)"
      >
        <UiIcon name="file-code" :size="14" class="tab-icon" /><span class="tab-label">{{ basename(document.path) }}</span>
        <span v-if="names.get(basename(document.path)) > 1" class="tab-path">{{ parentPath(document.path) }}</span>
        <span v-if="document.dirty" class="dirty-dot" aria-label="未保存"></span>
        <span v-if="document.conflict" class="conflict-mark" aria-label="磁盘版本已变化">!</span>
      </button>
      <button
        type="button" class="tab-close" :aria-label="`关闭 ${document.path}`"
        :title="`关闭 ${document.path}`" @click.stop="emit('close', document.key)"
      >×</button>
    </div>
  </div>
</template>

<style scoped>
.editor-tabs {
  display: flex; flex: none; min-width: 0; overflow-x: auto; overflow-y: hidden;
  background: var(--panel-soft); border-bottom: 1px solid var(--border); scrollbar-width: thin;
}
.editor-tab {
  display: flex; align-items: center; flex: none; position: relative; min-height: 36px;
  padding-right: 5px; border-right: 1px solid var(--border); color: var(--muted); background: var(--panel-soft);
}
.editor-tab.active { color: var(--text); background: var(--panel); }
.editor-tab.active::before {
  content: ''; position: absolute; inset: 0 0 auto; height: 2px; background: var(--accent);
}
.tab-select, .tab-close { border: 0; border-radius: 0; box-shadow: none; background: transparent; color: inherit; }
.tab-select {
  display: flex; align-items: center; gap: 7px; min-width: 0; max-width: 300px; min-height: 36px;
  padding: 6px 9px 6px 12px; font-size: var(--fs-sm); text-align: left;
}
.tab-label, .tab-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tab-label { min-width: 0; }.tab-icon { color: var(--faint); flex: none; }.editor-tab.active .tab-icon { color: var(--accent); }
.tab-path { max-width: 120px; font-size: var(--fs-2xs); color: var(--muted); }
.dirty-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
.conflict-mark { color: var(--warn); font-size: var(--fs-xs); font-weight: var(--fw-semibold); }
.tab-close { flex: none; padding: 0; width: 24px; height: 24px; border-radius: var(--r-sm); font-size: var(--fs-lg); }
.tab-select:hover, .tab-close:hover { background: var(--hover); }
.tab-select:focus-visible, .tab-close:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
