<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import UiIcon from '../ui/UiIcon.vue'
defineProps({ label: String, items: { type: Array, default: () => [] } })
const emit = defineEmits(['select'])
const root = ref(null), open = ref(false), popupStyle = ref({})
function position() {
  if (!open.value || !root.value) return
  const rect = root.value.getBoundingClientRect(), width = Math.min(280, window.innerWidth - 16)
  const top = rect.bottom + 5
  popupStyle.value = { width: `${width}px`, left: `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`, top: `${top}px`, maxHeight: `${Math.max(100, window.innerHeight - top - 8)}px` }
}
function close(focus = false) {
  open.value = false
  if (focus) root.value?.querySelector('.command-trigger')?.focus()
}
async function toggle() {
  open.value = !open.value
  if (open.value) { position(); await nextTick(); root.value?.querySelector('[role=menuitem]:not(:disabled)')?.focus() }
}
function keydown(event) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); return }
  if (event.key === 'Tab') { close(); return }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault(); event.stopPropagation()
  const items = [...root.value.querySelectorAll('[role=menuitem]:not(:disabled)')]
  if (!items.length) return
  const index = items.indexOf(document.activeElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
  items[next]?.focus()
}
function outside(event) { if (!event.composedPath().includes(root.value)) close() }
function select(item) { if (item.disabled) return; close(); emit('select', item.id) }
onMounted(() => { document.addEventListener('pointerdown', outside); window.addEventListener('resize', position) })
onBeforeUnmount(() => { document.removeEventListener('pointerdown', outside); window.removeEventListener('resize', position) })
</script>
<template>
  <div ref="root" class="editor-command-menu" @keydown="keydown" @focusout="event => { if (!root?.contains(event.relatedTarget)) close() }">
    <button type="button" class="command-trigger" :class="{ active: open }" :aria-expanded="open" aria-haspopup="menu" @click="toggle" @keydown.down.prevent="toggle">{{ label }}<UiIcon name="chevron-down" :size="12" /></button>
    <div v-if="open" class="command-popup" :style="popupStyle" role="menu" :aria-label="label">
      <button v-for="item in items" :key="item.id" type="button" role="menuitem" :disabled="item.disabled" :title="item.reason || item.label" @click="select(item)">
        <span>{{ item.label }}</span><kbd v-if="item.shortcut">{{ item.shortcut }}</kbd>
      </button>
    </div>
  </div>
</template>
<style scoped>
.editor-command-menu { position: relative; flex: none; }
.command-trigger { display: flex; align-items: center; gap: 5px; border: 0; border-radius: 5px; padding: 5px 8px; background: transparent; color: var(--text-2); font-size: var(--fs-xs); }
.command-trigger:hover, .command-trigger.active { color: var(--accent); background: var(--hover); }
.command-popup { position: fixed; z-index: 2000; box-sizing: border-box; overflow-y: auto; padding: 5px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); box-shadow: var(--shadow-pop); }
.command-popup button { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 7px 9px; border: 0; border-radius: 4px; background: transparent; color: var(--text); white-space: nowrap; font-size: 12px; text-align: start; }
.command-popup button:hover:not(:disabled), .command-popup button:focus-visible { background: var(--accent-soft); color: var(--accent); outline: 1px solid var(--accent); }
.command-popup button:disabled { opacity: .45; }
kbd { font: 10px ui-monospace, Consolas, monospace; color: var(--muted); }
</style>
