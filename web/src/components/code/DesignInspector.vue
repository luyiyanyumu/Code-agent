<script setup>
import { computed, nextTick, reactive, useId, watch } from 'vue'
import UiIcon from '../ui/UiIcon.vue'
import { INSPECTOR_GROUPS, normalizeInspectorInput, stepInspectorInput, hexColorForPicker } from '../../lib/designInspectorFields.js'

const props = defineProps({ selected: { type: Object, default: null }, draft: { type: Object, default: () => ({}) }, overrides: { type: Object, default: () => ({}) }, disabled: { type: Boolean, default: false } })
const emit = defineEmits(['change', 'reset', 'reset-all'])
const instanceId = useId()
const values = reactive({})
const editing = new Set(), pending = new Map()
const fields = INSPECTOR_GROUPS.flatMap(group => group.fields)
const blocked = computed(() => props.disabled || !props.selected)
const hasOverrides = computed(() => Object.keys(props.overrides || {}).length > 0)
const dimensions = computed(() => props.selected?.rect ? `${Math.round(props.selected.rect.width)} × ${Math.round(props.selected.rect.height)} px` : '')
const incoming = property => String(props.draft?.[property] ?? props.selected?.styles?.[property] ?? '')
const overridden = property => Object.hasOwn(props.overrides || {}, property)
watch([() => props.selected?.selector, () => props.draft, () => props.selected?.styles], ([selector], previous) => {
  if (!previous || selector !== previous[0]) { editing.clear(); pending.clear() }
  for (const field of fields) {
    const value = incoming(field.property)
    if (!editing.has(field.property)) values[field.property] = value
    if (pending.get(field.property) === value) pending.delete(field.property)
  }
}, { immediate: true, deep: true })
function commit(property, input = values[property]) {
  editing.delete(property)
  if (blocked.value) return
  const value = normalizeInspectorInput(property, input)
  values[property] = value
  if (!value) {
    if (overridden(property)) resetField(property)
    else values[property] = incoming(property)
    return
  }
  if (value === incoming(property) || pending.get(property) === value) return
  pending.set(property, value)
  const selector = props.selected.selector
  emit('change', { property, value })
  nextTick(() => {
    if (props.selected?.selector === selector && pending.get(property) === value && !editing.has(property)) {
      values[property] = incoming(property)
      pending.delete(property)
    }
  })
}
function onFieldInput(property) { editing.add(property); pending.delete(property) }
function resetField(property) {
  if (blocked.value || !overridden(property)) return
  editing.delete(property); pending.delete(property)
  emit('reset', property)
}
function inputKey(event, property) {
  if (event.isComposing || blocked.value) return
  if ((event.ctrlKey || event.metaKey) && ['z', 'y'].includes(event.key.toLowerCase())) { event.stopPropagation(); return }
  if (!event.ctrlKey && !event.metaKey && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
    const value = stepInspectorInput(property, values[property], event.key === 'ArrowUp' ? 1 : -1, event)
    if (value !== null) { event.preventDefault(); event.stopPropagation(); values[property] = value; commit(property); return }
  }
  if (event.key === 'Enter' && !event.ctrlKey && !event.metaKey && !event.altKey) { event.preventDefault(); event.stopPropagation(); commit(property) }
  else if (event.key === 'Escape') { values[property] = incoming(property); editing.delete(property); pending.delete(property); event.stopPropagation(); event.target?.blur?.() }
}
function enumKnown(field) { return !values[field.property] || field.options.some(option => option.value === values[field.property]) }
</script>

<template>
  <section class="design-inspector" aria-label="元素属性">
    <header class="inspector-heading"><div><strong>{{ selected?.tag || '设计属性' }}</strong><span v-if="dimensions">{{ dimensions }}</span></div><button type="button" class="reset-all" :disabled="blocked || !hasOverrides" title="移除当前元素的所有样式覆盖" @click="emit('reset-all')">全部重置</button></header>
    <div v-if="!selected" class="inspector-empty"><UiIcon name="edit" :size="25" /><strong>选择一个页面元素</strong><p>在画布或图层中选择元素，查看和调整它的属性。</p></div>
    <div v-else class="inspector-groups">
      <p v-if="selected.geometry && !selected.geometry.enabled" class="inspector-footnote" role="status">{{ selected.geometry.hint }}</p>
      <details v-for="group in INSPECTOR_GROUPS" :key="group.id" class="inspector-group" open>
        <summary>{{ group.label }}</summary>
        <div class="inspector-fields" :class="{ paired: ['size', 'layout', 'spacing', 'text', 'effects'].includes(group.id) }">
        <div v-for="field in group.fields" :key="field.property" class="inspector-field" :class="{ overridden: overridden(field.property), wide: ['font-family', 'border', 'box-shadow'].includes(field.property) }">
          <div class="field-heading"><label :for="`${instanceId}-${field.property}`">{{ field.label }}</label><span v-if="overridden(field.property)" class="override-mark" title="已设置预览覆盖">已修改</span><button v-if="overridden(field.property)" type="button" class="field-reset" :disabled="blocked" :aria-label="`重置${field.label}`" @click="resetField(field.property)"><UiIcon name="undo" :size="12" /></button></div>
          <div class="field-control" :class="{ 'color-control': field.control === 'color' }">
            <input v-if="field.control === 'color'" type="color" class="color-picker" :value="hexColorForPicker(values[field.property])" :disabled="blocked" :aria-label="`${field.label}色板`" @change="commit(field.property, $event.target.value)" @keydown="inputKey($event, field.property)" />
            <select v-if="field.control === 'select'" :id="`${instanceId}-${field.property}`" v-model="values[field.property]" :disabled="blocked" :aria-label="field.label" @change="commit(field.property)" @keydown="inputKey($event, field.property)"><option value="">未设置</option><option v-if="!enumKnown(field)" :value="values[field.property]">{{ values[field.property] }}</option><option v-for="option in field.options" :key="option.value" :value="option.value">{{ option.label }}</option></select>
            <input v-else :id="`${instanceId}-${field.property}`" v-model="values[field.property]" type="text" :inputmode="['width', 'height', 'font-size', 'font-weight', 'line-height', 'opacity'].includes(field.property) ? 'decimal' : 'text'" :disabled="blocked" :aria-label="field.label" :placeholder="field.placeholder || (field.control === 'opacity' ? '0–1' : field.property)" spellcheck="false" @focus="editing.add(field.property)" @input="onFieldInput(field.property)" @blur="commit(field.property)" @keydown="inputKey($event, field.property)" />
          </div>
        </div>
        </div>
      </details>
      <p class="inspector-footnote">数字属性用 ↑ / ↓ 微调，Shift 加速、Alt 精调；CSS 表达式与多值间距保留原文。修改即时预览，保存后写入项目。位移使用 X / Y 像素值，移动保留原有布局占位。</p>
    </div>
  </section>
</template>

<style scoped>
.design-inspector { min-width: 0; height: 100%; display: flex; flex-direction: column; color: var(--text); font-size: var(--fs-xs, 12px); background: var(--panel); }
.inspector-fields.paired { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 9px; }.inspector-fields.paired .inspector-field.wide { grid-column: 1 / -1; }.field-heading label { min-width: 0; }.field-heading .override-mark { flex: none; }.inspector-fields.paired .override-mark { font-size: 0; width: 4px; height: 4px; border-radius: 50%; background: var(--accent); }
.inspector-heading { display: flex; align-items: center; gap: 8px; padding: 14px 12px; border-bottom: 1px solid var(--border); flex: none; }.inspector-heading > div { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }.inspector-heading strong { font-weight: 600; overflow: hidden; text-overflow: ellipsis; }.inspector-heading span { color: var(--muted); font-size: 10px; }.reset-all { border: 0; background: transparent; color: var(--muted); padding: 4px; white-space: nowrap; font-size: 10px; }
.inspector-groups { overflow-y: auto; overscroll-behavior: contain; min-height: 0; flex: 1; }.inspector-group { padding: 0 12px 12px; border-bottom: 1px solid var(--border); }.inspector-group summary { cursor: pointer; padding: 13px 0 8px; font-weight: 600; font-size: 11px; }.inspector-group:not([open]) { padding-bottom: 3px; }.inspector-field { margin-top: 10px; min-width: 0; }.field-heading { display: flex; align-items: center; gap: 5px; min-height: 19px; margin-bottom: 4px; }.field-heading label { flex: 1; color: var(--text-2); font-size: 11px; }.override-mark { font-size: 9px; color: var(--accent); }.field-reset { display: grid; place-items: center; min-width: 20px; min-height: 19px; padding: 2px; border: 0; color: var(--muted); background: transparent; }
.field-control { display: flex; align-items: stretch; min-width: 0; gap: 5px; }.field-control input, .field-control select { width: 100%; min-width: 0; min-height: 29px; box-sizing: border-box; padding: 5px 7px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel-soft); color: var(--text); font-size: 11px; font-family: inherit; }.field-control input[type="text"] { font-family: var(--font-mono, monospace); }.field-control .color-picker { width: 30px; flex: none; padding: 3px; cursor: pointer; }.overridden .field-control > input[type="text"], .overridden .field-control select { border-color: var(--accent-border); }.field-control input:focus, .field-control select:focus { outline: 2px solid var(--accent-border); outline-offset: 1px; }.field-control input:disabled, .field-control select:disabled, button:disabled { opacity: .45; cursor: default; }
.inspector-empty { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 10px; padding: 42px 18px; color: var(--muted); }.inspector-empty > svg { color: var(--accent); }.inspector-empty strong { color: var(--text-2); font-weight: 500; }.inspector-empty p, .inspector-footnote { font-size: 10px; line-height: 1.7; margin: 0; }.inspector-footnote { padding: 13px 12px 18px; color: var(--muted); }
</style>
