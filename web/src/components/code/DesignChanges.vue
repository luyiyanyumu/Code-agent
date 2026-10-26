<script setup>
import { computed } from 'vue'
import UiIcon from '../ui/UiIcon.vue'
import { buildDesignChanges } from '../../lib/designChanges.js'

const props = defineProps({ rules: { type: Array, default: () => [] }, savedCss: { type: String, default: '' }, disabled: { type: Boolean, default: false } })
const emit = defineEmits(['select', 'reset', 'export-css', 'copy-css'])
const model = computed(() => buildDesignChanges(props.rules, props.savedCss))
const statusLabels = { added: '新增', modified: '修改', removed: '已移除' }
function selectChange(selector) {
  if (!props.disabled && model.value.changes.some(change => change.selector === selector)) emit('select', selector)
}
function resetChange(selector) {
  if (!props.disabled && model.value.changes.some(change => change.selector === selector && change.hasCurrent)) emit('reset', selector)
}
function exportCss() { if (!props.disabled && model.value.exportable) emit('export-css') }
function copyCss() { if (!props.disabled && model.value.exportable) emit('copy-css') }
</script>

<template>
  <section class="design-changes" aria-label="样式改动管理">
    <header class="changes-heading"><UiIcon name="history" :size="16" /><div><strong>样式改动</strong><span>{{ model.elementCount }} 个元素 · {{ model.propertyCount }} 项属性改动</span></div></header>
    <div class="changes-actions"><button type="button" :disabled="disabled || !model.exportable" @click="copyCss"><UiIcon name="file-code" :size="13" />复制 CSS</button><button type="button" :disabled="disabled || !model.exportable" @click="exportCss"><UiIcon name="download" :size="13" />导出 CSS</button></div>
    <p v-if="!model.exportable" class="changes-notice" role="alert">当前样式过大，请减少改动后再导出。</p>
    <div v-if="model.exportable && !model.changes.length" class="changes-empty" role="status"><UiIcon name="check" :size="25" /><strong>没有尚未保存的改动</strong><p>当前样式与已保存版本一致，仍可复制或导出完整设计 CSS。</p></div>
    <div v-else-if="model.changes.length" class="changes-list">
      <article v-for="change in model.changes" :key="change.selector" class="change-item" :class="change.status">
        <header class="change-heading"><span class="change-status">{{ statusLabels[change.status] }}</span><span>{{ change.properties.length }} 项属性</span><button v-if="change.hasCurrent" type="button" :disabled="disabled" title="移除此元素当前的全部样式覆盖" :aria-label="`重置 ${change.selector} 的样式覆盖`" @click="resetChange(change.selector)"><UiIcon name="undo" :size="12" />重置</button></header>
        <button type="button" class="change-selector" :disabled="disabled" :title="change.selector" :aria-label="`选中元素 ${change.selector}`" @click="selectChange(change.selector)"><UiIcon name="layers" :size="12" /><code>{{ change.selector }}</code></button>
        <dl class="change-properties"><div v-for="property in change.properties" :key="property.property" class="change-property"><dt><span>{{ property.label }}</span><small>{{ statusLabels[property.status] }}</small></dt><dd><span class="value-caption">已保存</span><code :class="{ absent: property.before === null }">{{ property.before === null ? '未设置' : property.before }}</code></dd><dd><span class="value-caption">当前</span><code :class="{ absent: property.after === null }">{{ property.after === null ? '已移除覆盖' : property.after }}</code></dd></div></dl>
      </article>
    </div>
    <p class="changes-footnote">对比已保存的设计样式。复制和导出包含当前全部样式覆盖，不会写入项目文件。</p>
  </section>
</template>

<style scoped>
.design-changes { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; background: var(--panel); color: var(--text); font-size: var(--fs-xs, 12px); }
.changes-heading { display: flex; align-items: flex-start; gap: 7px; padding: 14px 12px 11px; flex: none; }.changes-heading > svg { color: var(--accent); margin-top: 1px; }.changes-heading > div { display: flex; flex-direction: column; min-width: 0; gap: 5px; }.changes-heading strong { font-weight: 600; }.changes-heading span { color: var(--muted); font-size: 10px; }
.changes-actions { display: flex; gap: 7px; padding: 0 12px 12px; border-bottom: 1px solid var(--border); flex: none; }.design-changes button { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-width: 0; border: 1px solid var(--border); border-radius: 5px; padding: 6px 7px; background: var(--panel-soft); color: var(--text-2, var(--text)); font-size: 10px; cursor: pointer; }.changes-actions button { flex: 1; white-space: nowrap; }.design-changes button:hover:not(:disabled) { color: var(--accent); border-color: var(--accent-border, var(--accent)); }.design-changes button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }.design-changes button:disabled { opacity: .45; cursor: default; }
.changes-list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 0 12px; }.change-item { padding: 12px 0; border-bottom: 1px solid var(--border); min-width: 0; }.change-heading { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 10px; }.change-status { color: var(--accent); font-weight: 500; }.removed .change-status { color: var(--warn, #b78338); }.change-heading > span:nth-child(2) { flex: 1; }.change-heading button { border: 0; background: transparent; padding: 3px; }
.design-changes .change-selector { justify-content: flex-start; width: 100%; text-align: left; margin-top: 6px; padding: 6px; background: var(--panel-soft); }.change-selector code { min-width: 0; overflow-wrap: anywhere; white-space: normal; font-family: var(--font-mono, monospace); font-size: 10px; line-height: 1.5; }.change-selector > svg { flex: none; }
.change-properties { margin: 0; }.change-property { margin-top: 11px; }.change-property dt { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; font-size: 10px; }.change-property dt > span { flex: 1; }.change-property small { color: var(--muted); font-size: 9px; }.change-property dd { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 5px; margin: 4px 0 0; align-items: baseline; line-height: 1.5; }.value-caption { color: var(--muted); font-size: 9px; }.change-property code { font-size: 10px; font-family: var(--font-mono, monospace); overflow-wrap: anywhere; white-space: pre-wrap; }.change-property code.absent { color: var(--muted); font-family: inherit; }
.changes-empty { display: flex; flex-direction: column; align-items: center; gap: 10px; text-align: center; padding: 34px 16px; color: var(--muted); }.changes-empty > svg { color: var(--success, var(--accent)); }.changes-empty strong { color: var(--text-2, var(--text)); font-size: 11px; font-weight: 500; }.changes-empty p, .changes-footnote, .changes-notice { font-size: 10px; line-height: 1.7; margin: 0; }.changes-footnote { color: var(--muted); padding: 12px; flex: none; }.changes-notice { color: var(--warn, var(--muted)); padding: 10px 12px 0; }
</style>
