<script setup>
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'
import UiIcon from '../ui/UiIcon.vue'
import { searchIdeCommands } from '../../lib/ideCommands.js'

const props = defineProps({ open: Boolean, menus: { type: Array, default: () => [] } })
const emit = defineEmits(['close', 'command'])
const modal = ref(null), input = ref(null), list = ref(null), query = ref(''), active = ref(0), composing = ref(false)
const listId = `ide-commands-${useId()}`
const results = computed(() => searchIdeCommands(props.menus, query.value, { limit: 200 }))
const selectedId = computed(() => results.value.length ? `${listId}-${active.value}` : undefined)
const groups = computed(() => {
  const commands = results.value.map((command, index) => ({ ...command, index }))
  if (query.value.replace(/^\s*>\s*/, '').trim()) return [{ id: 'results', label: '', commands }]
  const grouped = new Map()
  for (const command of commands) {
    if (!grouped.has(command.menuId)) grouped.set(command.menuId, { id: command.menuId, label: command.menuLabel, commands: [] })
    grouped.get(command.menuId).commands.push(command)
  }
  return [...grouped.values()]
})
let previousFocus = null, closing = false, disposed = false

function restoreFocus() {
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
  previousFocus = null
}
function close() {
  if (closing) return
  closing = true; modal.value?.close(); restoreFocus(); emit('close')
}
function choose(command) {
  if (composing.value || !command) return
  // Recheck live availability immediately before emitting an action.
  const current = searchIdeCommands(props.menus, query.value, { limit: 200 }).find(item => item.id === command.id)
  if (!current) return
  close(); emit('command', current.id)
}
function scrollActive() { if (props.open) nextTick(() => list.value?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' })) }
function onInputKey(event) {
  if (event.isComposing || event.keyCode === 229 || composing.value) { event.stopPropagation(); return }
  if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault(); event.stopPropagation()
    if (!results.value.length) return
    if (event.key === 'Home') active.value = 0
    else if (event.key === 'End') active.value = results.value.length - 1
    else active.value = (active.value + (event.key === 'ArrowDown' ? 1 : -1) + results.value.length) % results.value.length
    scrollActive()
  } else if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); choose(results.value[active.value]) }
}
function dialogKey(event) {
  if (event.isComposing || event.keyCode === 229 || composing.value) { if (event.key === 'Escape') event.stopPropagation(); return }
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
  if (event.key !== 'Tab') return
  const focusable = [...(modal.value?.querySelectorAll('input:not([disabled]), button:not([disabled]):not([tabindex="-1"]), [tabindex="0"]') || [])]
  const first = focusable[0], last = focusable.at(-1)
  if (event.shiftKey && (document.activeElement === first || !modal.value.contains(document.activeElement))) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
function cancel(event) { event.preventDefault(); if (!composing.value) close() }
function backdrop(event) {
  if (event.target !== modal.value) return
  const bounds = modal.value.getBoundingClientRect()
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close()
}
watch(() => props.open, async open => {
  if (!open) { modal.value?.close(); restoreFocus(); return }
  closing = false; composing.value = false; query.value = ''; active.value = 0
  previousFocus = document.activeElement
  await nextTick()
  if (!props.open || disposed || !modal.value) return
  if (!modal.value.open) modal.value.showModal()
  input.value?.focus({ preventScroll: true })
}, { immediate: true })
watch(query, () => { active.value = 0; scrollActive() })
watch(results, rows => { active.value = Math.min(active.value, Math.max(0, rows.length - 1)); scrollActive() })
onBeforeUnmount(() => { disposed = true; modal.value?.close(); restoreFocus() })
</script>

<template>
  <Teleport to="body">
    <dialog ref="modal" class="ide-command-palette" aria-label="命令面板" @cancel="cancel" @keydown="dialogKey" @click="backdrop">
      <div class="command-search">
        <UiIcon name="chevron-right" :size="18" class="command-prompt" aria-hidden="true" />
        <input ref="input" v-model="query" type="text" role="combobox" aria-label="搜索命令" aria-autocomplete="list"
          :aria-expanded="open" :aria-controls="listId" :aria-activedescendant="selectedId"
          autocomplete="off" spellcheck="false" placeholder="搜索命令，例如：保存、终端、专注模式"
          @keydown="onInputKey" @compositionstart="composing = true" @compositionend="composing = false" />
        <button type="button" class="command-close" aria-label="关闭命令面板" @click="close">Esc</button>
      </div>
      <div :id="listId" ref="list" class="command-list" role="listbox" aria-label="可用命令">
        <div v-for="group in groups" :key="group.id" role="group" :aria-label="group.label || '搜索结果'">
          <div v-if="group.label" class="command-group-title" role="presentation">{{ group.label }}</div>
          <button v-for="command in group.commands" :id="`${listId}-${command.index}`" :key="command.id"
            type="button" role="option" class="command-option" :class="{ selected: active === command.index }"
            :aria-selected="active === command.index" tabindex="-1" @mouseenter="active = command.index" @click="choose(command)">
            <span class="command-name">{{ command.label }}<small v-if="query.trim()">{{ command.menuLabel }}</small></span>
            <kbd v-if="command.shortcut">{{ command.shortcut }}</kbd>
            <UiIcon v-else-if="command.checked" name="check" :size="14" aria-hidden="true" />
          </button>
        </div>
        <div v-if="!results.length" class="command-empty" role="status">{{ query.trim() ? '没有匹配的可用命令，试试“保存”或“终端”。' : '当前没有可用命令。' }}</div>
      </div>
      <footer class="command-footer"><span><kbd>↑ ↓</kbd> 选择 <kbd>Enter</kbd> 执行</span><span><kbd>Ctrl+Shift+P</kbd> 命令面板</span></footer>
    </dialog>
  </Teleport>
</template>

<style scoped>
.ide-command-palette { position: fixed; inset: min(12vh, 96px) 0 auto; box-sizing: border-box; width: min(620px, calc(100vw - 24px)); max-height: calc(100dvh - min(12vh, 96px) - 24px); padding: 0; margin: 0 auto; border: 1px solid var(--border-strong, #d5dce8); border-radius: 9px; background: var(--panel, #fff); color: var(--text, #1f2937); box-shadow: var(--shadow-lg, 0 14px 50px #17233b33); overflow: hidden; }
.ide-command-palette[open] { display: flex; flex-direction: column; }
.ide-command-palette::backdrop { background: #17233b38; }
.command-search { display: flex; align-items: center; gap: 8px; min-height: 48px; flex: none; padding: 0 12px; border-bottom: 1px solid var(--border, #e4e8f0); }
.command-prompt { color: var(--accent, #365dff); flex: none; }
.command-search input { flex: 1; min-width: 0; width: 100%; min-height: 44px; border: 0; border-radius: 0; padding: 8px 0; color: inherit; background: transparent; font: inherit; font-size: 14px; outline: none; box-shadow: none; }
.command-search:focus-within { box-shadow: inset 0 -2px var(--accent, #365dff); }
.command-search input::placeholder { color: var(--muted, #65748b); }
.command-close { flex: none; min-height: 26px; padding: 2px 6px; border: 1px solid var(--border, #e4e8f0); border-radius: 4px; background: var(--panel-soft, #f4f6fa); color: var(--muted, #65748b); font-size: 11px; }
.command-close:hover { background: var(--hover, #e9edf5); color: var(--text, #1f2937); }
.command-close:focus-visible { outline: 2px solid var(--accent, #365dff); outline-offset: 2px; }
.command-list { min-height: 0; max-height: 460px; flex: 1; padding: 5px; overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin; }
.command-group-title { padding: 10px 10px 4px; color: var(--muted, #65748b); font-size: 11px; font-weight: 600; }
.command-list > div:first-child .command-group-title { padding-top: 5px; }
.command-option { display: flex; align-items: center; gap: 10px; width: 100%; min-height: 34px; padding: 6px 10px; border: 0; border-radius: 4px; box-shadow: none; background: transparent; color: inherit; font: inherit; font-size: 13px; text-align: left; }
.command-option.selected { color: var(--accent, #365dff); background: var(--accent-soft, #edf1ff); }
.command-option:focus-visible { outline: 1px solid var(--accent, #365dff); outline-offset: -1px; }
.command-name { display: flex; gap: 9px; align-items: baseline; flex: 1; min-width: 0; overflow-wrap: anywhere; }
.command-name small { color: var(--muted, #65748b); font-size: 11px; }
.command-option kbd, .command-footer kbd { flex: none; color: var(--muted, #65748b); background: transparent; border: 0; padding: 0; font-family: var(--font-mono, Consolas, monospace); font-size: 11px; white-space: nowrap; }
.command-empty { padding: 28px 14px; text-align: center; color: var(--muted, #65748b); font-size: 13px; }
.command-footer { display: flex; align-items: center; justify-content: space-between; flex: none; gap: 12px; padding: 8px 12px; border-top: 1px solid var(--border, #e4e8f0); background: var(--panel-soft, #f4f6fa); color: var(--muted, #65748b); font-size: 11px; }
.command-footer span { display: flex; align-items: center; gap: 5px; }
@media(max-width: 480px) { .ide-command-palette { inset-block-start: 32px; max-height: calc(100dvh - 48px); }.command-search { gap: 5px; padding: 0 9px; }.command-search input { font-size: 13px; }.command-footer { flex-wrap: wrap; gap: 5px; padding: 7px 10px; }.command-option { min-height: 38px; } }
</style>
