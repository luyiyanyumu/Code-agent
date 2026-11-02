<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import UiIcon from '../ui/UiIcon.vue'

const props = defineProps({ menus: { type: Array, default: () => [] } })
const emit = defineEmits(['command'])
const root = ref(null), popup = ref(null), activeId = ref(''), focusIndex = ref(0), popupStyle = ref({ visibility: 'hidden' })
const instanceId = `ide-menu-${useId()}`
const buttons = new Map(), itemButtons = new Map()
const menus = computed(() => props.menus.filter(menu => menu?.id && menu?.label))
const activeMenu = computed(() => menus.value.find(menu => menu.id === activeId.value))
const items = computed(() => (activeMenu.value?.items || []).filter(item => item && !item.hidden))
const enabledItems = computed(() => items.value.filter(item => item.id && item.label && !item.separator && !item.disabled))
let typeahead = '', typedAt = 0, placement = 0

function bind(map, key, element) { if (element) map.set(key, element); else map.delete(key) }
function trigger(id = activeId.value) { return buttons.get(id) }
function close(restoreFocus = false) {
  const button = trigger()
  activeId.value = ''; placement++; typeahead = ''
  if (restoreFocus) button?.focus({ preventScroll: true })
}
function focusItem(index) {
  const rows = enabledItems.value
  if (!rows.length) { popup.value?.focus({ preventScroll: true }); return }
  const row = rows[(index + rows.length) % rows.length]
  itemButtons.get(row.id)?.focus({ preventScroll: true })
  itemButtons.get(row.id)?.scrollIntoView({ block: 'nearest' })
}
async function place() {
  const token = ++placement
  await nextTick()
  if (token !== placement || !activeId.value || !popup.value || !trigger()) return
  const rect = trigger().getBoundingClientRect(), width = Math.min(288, window.innerWidth - 16)
  const height = popup.value.scrollHeight, below = window.innerHeight - rect.bottom - 8, above = rect.top - 8
  const upwards = height > below && above > below, available = Math.max(48, upwards ? above - 3 : below - 3)
  popupStyle.value = {
    left: `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`,
    top: `${Math.max(8, upwards ? rect.top - Math.min(height, available) - 3 : rect.bottom + 3)}px`,
    width: `${width}px`, maxHeight: `${available}px`, visibility: 'visible',
  }
}
async function open(menu, focus = '') {
  if (!menu) return
  activeId.value = menu.id; focusIndex.value = menus.value.indexOf(menu)
  popupStyle.value = { visibility: 'hidden' }; typeahead = ''
  await place()
  await nextTick()
  if (activeId.value !== menu.id) return
  if (focus) focusItem(focus === 'last' ? enabledItems.value.length - 1 : 0)
}
function toggle(menu) { if (activeId.value === menu.id) close(true); else void open(menu, 'first') }
function switchMenu(step, focus = true) {
  const index = (focusIndex.value + step + menus.value.length) % menus.value.length
  const menu = menus.value[index]
  if (!menu) return
  focusIndex.value = index
  if (activeId.value) void open(menu, focus ? 'first' : '')
  else buttons.get(menu.id)?.focus({ preventScroll: true })
}
function onTriggerKey(event, menu) {
  if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
    event.preventDefault(); void open(menu, event.key === 'ArrowUp' ? 'last' : 'first')
  } else if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') { event.preventDefault(); switchMenu(event.key === 'ArrowRight' ? 1 : -1) }
  else if (event.key === 'Home' || event.key === 'End') {
    event.preventDefault(); focusIndex.value = event.key === 'Home' ? 0 : menus.value.length - 1
    buttons.get(menus.value[focusIndex.value]?.id)?.focus({ preventScroll: true })
  } else if (event.key === 'Escape') { event.preventDefault(); close(true) }
  else if (event.key === 'Tab') close()
}
function activate(item) {
  if (!item?.id || item.disabled || item.separator) return
  close(true); emit('command', item.id)
}
function onMenuKey(event) {
  const rows = enabledItems.value, index = rows.findIndex(item => itemButtons.get(item.id) === document.activeElement)
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); focusItem(index + (event.key === 'ArrowDown' ? 1 : -1)) }
  else if (event.key === 'Home' || event.key === 'End') { event.preventDefault(); focusItem(event.key === 'Home' ? 0 : rows.length - 1) }
  else if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') { event.preventDefault(); switchMenu(event.key === 'ArrowRight' ? 1 : -1) }
  else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true) }
  else if (event.key === 'Tab') { trigger()?.focus({ preventScroll: true }); close() }
  else if ((event.key === 'Enter' || event.key === ' ') && index >= 0) { event.preventDefault(); activate(rows[index]) }
  else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && event.key !== ' ') {
    const now = Date.now(); typeahead = now - typedAt > 700 ? event.key : typeahead + event.key; typedAt = now
    const found = rows.findIndex((item, offset) => offset > index && String(item.label).toLocaleLowerCase().startsWith(typeahead.toLocaleLowerCase()))
    const wrapped = found >= 0 ? found : rows.findIndex(item => String(item.label).toLocaleLowerCase().startsWith(typeahead.toLocaleLowerCase()))
    if (wrapped >= 0) { event.preventDefault(); focusItem(wrapped) }
  }
}
function outside(event) { if (activeId.value && !root.value?.contains(event.target) && !popup.value?.contains(event.target)) close() }
function windowBlur() { close() }
function hover(menu) { if (activeId.value && activeId.value !== menu.id) void open(menu, 'first') }
watch(menus, () => { if (!activeMenu.value) close(); focusIndex.value = Math.min(focusIndex.value, Math.max(0, menus.value.length - 1)) })
watch(items, () => { if (activeId.value) void place() })
onMounted(() => {
  document.addEventListener('pointerdown', outside, true)
  document.addEventListener('focusin', outside)
  window.addEventListener('resize', place)
  window.addEventListener('scroll', place, true)
  window.addEventListener('blur', windowBlur)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', outside, true); document.removeEventListener('focusin', outside)
  window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); window.removeEventListener('blur', windowBlur)
})
</script>

<template>
  <div ref="root" class="ide-menubar" role="menubar" aria-label="IDE 菜单">
    <button v-for="(menu, index) in menus" :key="menu.id" :ref="element => bind(buttons, menu.id, element)"
      type="button" class="ide-menu-trigger" role="menuitem" aria-haspopup="menu"
      :aria-expanded="activeId === menu.id" :aria-controls="`${instanceId}-${menu.id}`"
      :tabindex="focusIndex === index ? 0 : -1" :class="{ open: activeId === menu.id }"
      @focus="focusIndex = index" @click="toggle(menu)" @mouseenter="hover(menu)" @keydown="onTriggerKey($event, menu)">{{ menu.label }}</button>
    <Teleport to="body">
      <div v-if="activeMenu" :id="`${instanceId}-${activeMenu.id}`" :key="activeMenu.id" ref="popup"
        class="ide-menu-popup" role="menu" :aria-label="activeMenu.label" :style="popupStyle" tabindex="-1" @keydown="onMenuKey">
        <template v-for="(item, index) in items" :key="item.id || `separator-${index}`">
          <div v-if="item.separator" role="separator" class="ide-menu-separator" />
          <button v-else-if="item.id && item.label" :ref="element => bind(itemButtons, item.id, element)"
            type="button" class="ide-menu-item" :role="typeof item.checked === 'boolean' ? 'menuitemcheckbox' : 'menuitem'"
            :aria-checked="typeof item.checked === 'boolean' ? item.checked : undefined" :disabled="item.disabled" :aria-disabled="item.disabled || undefined"
            tabindex="-1" :title="item.label" @click="activate(item)">
            <span class="ide-menu-check" aria-hidden="true"><UiIcon v-if="item.checked" name="check" :size="14" /></span>
            <span class="ide-menu-label">{{ item.label }}</span><kbd v-if="item.shortcut">{{ item.shortcut }}</kbd>
          </button>
        </template>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ide-menubar { display: flex; align-items: center; flex: 0 1 auto; min-width: 0; overflow-x: auto; scrollbar-width: none; gap: 1px; }
.ide-menubar::-webkit-scrollbar { display: none; }
.ide-menu-trigger { flex: none; min-width: 38px; min-height: 27px; padding: 3px 8px; border: 0; border-radius: 4px; box-shadow: none; background: transparent; color: var(--text-2, #475569); font: inherit; font-size: 13px; white-space: nowrap; }
.ide-menu-trigger:hover, .ide-menu-trigger.open { color: var(--text, #1f2937); background: var(--hover, #e9edf5); }
.ide-menu-trigger:focus-visible { outline: 2px solid var(--accent, #365dff); outline-offset: -2px; }
.ide-menu-popup { position: fixed; z-index: 1500; box-sizing: border-box; padding: 5px; overflow: auto; overscroll-behavior: contain; border: 1px solid var(--border-strong, #d5dce8); border-radius: 7px; color: var(--text, #1f2937); background: var(--panel, #fff); box-shadow: var(--shadow-lg, 0 8px 28px #17233b26); scrollbar-width: thin; outline: none; }
.ide-menu-item { display: grid; grid-template-columns: 16px minmax(0, 1fr) auto; align-items: center; gap: 7px; width: 100%; min-height: 30px; padding: 4px 8px; border: 0; border-radius: 4px; box-shadow: none; text-align: left; color: inherit; background: transparent; font: inherit; font-size: 13px; }
.ide-menu-item:hover:not(:disabled), .ide-menu-item:focus-visible { background: var(--accent-soft, #edf1ff); color: var(--accent, #365dff); outline: none; }
.ide-menu-item:focus-visible { box-shadow: inset 0 0 0 1px var(--accent, #365dff); }
.ide-menu-item:disabled { color: var(--faint, #8c96a8); cursor: default; }
.ide-menu-check { display: inline-flex; width: 16px; align-items: center; justify-content: center; color: var(--accent, #365dff); }
.ide-menu-label { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.ide-menu-item kbd { border: 0; padding: 0; background: transparent; color: var(--muted, #65748b); font-family: var(--font-mono, Consolas, monospace); font-size: 11px; white-space: nowrap; }
.ide-menu-item:disabled kbd { color: inherit; }
.ide-menu-separator { height: 1px; margin: 5px 8px; background: var(--border, #e4e8f0); }
@media (max-width: 760px) { .ide-menu-trigger { min-width: 36px; padding: 3px 7px; }.ide-menu-item { min-height: 36px; } }
</style>
