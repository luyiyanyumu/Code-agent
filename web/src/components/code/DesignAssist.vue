<script setup>
import { computed, ref, watch } from 'vue'
import { STYLE_FIELDS, normalizeStyleDeclarations } from '../../lib/frontendDesign.js'
import { DESIGN_INSTRUCTION_TEMPLATES, MAX_DESIGN_PRESETS, MAX_DESIGN_PRESET_NAME, normalizeDesignPresetName, normalizeDesignPresets } from '../../lib/designPresets.js'

const props = defineProps({ selected: { type: Object, default: null }, instruction: { type: String, default: '' }, proposal: { type: Object, default: null }, diff: { type: Array, default: () => [] }, notice: { type: String, default: '' }, busy: { type: Boolean, default: false }, disabled: { type: Boolean, default: false }, canSavePreset: { type: Boolean, default: false }, presets: { type: Array, default: () => [] } })
const emit = defineEmits(['update:instruction', 'request', 'apply', 'dismiss', 'save-preset', 'apply-preset', 'remove-preset'])
const blocked = computed(() => props.disabled || props.busy || !props.selected)
const proposalMatches = computed(() => !props.proposal?.selector || props.proposal.selector === props.selected?.selector)
const labels = new Map(STYLE_FIELDS.map(field => [field.property, field.label]))
const diffRows = computed(() => {
  if (!props.proposal) return []
  const declarations = normalizeStyleDeclarations(props.proposal.declarations), seen = new Set()
  return (Array.isArray(props.diff) ? props.diff : []).flatMap(item => {
    if (!item || typeof item.property !== 'string' || !Object.hasOwn(declarations, item.property) || seen.has(item.property)) return []
    seen.add(item.property)
    return [{ property: item.property, label: labels.get(item.property) || item.property, before: String(item.before ?? '未设置').slice(0, 256), after: declarations[item.property] }]
  })
})
const chosen = ref(new Set()), presetName = ref('')
const safePresets = computed(() => normalizeDesignPresets(props.presets))
const chosenCount = computed(() => diffRows.value.filter(row => chosen.value.has(row.property)).length)
watch([() => props.proposal, () => props.selected?.selector, diffRows], ([proposal, selector, rows], previous) => {
  const reset = !previous || proposal !== previous[0] || selector !== previous[1]
  const existing = new Set((previous?.[2] || []).map(row => row.property))
  chosen.value = new Set(rows.filter(row => reset || chosen.value.has(row.property) || !existing.has(row.property)).map(row => row.property))
}, { immediate: true })
function fillInstruction(template) { if (!blocked.value) emit('update:instruction', template.instruction) }
function updateInstruction(event) { if (!blocked.value) emit('update:instruction', String(event.target?.value || '').slice(0, 2000)) }
function request() { if (!blocked.value && props.instruction.trim()) emit('request') }
function choose(property, checked) {
  if (blocked.value || !proposalMatches.value) return
  const next = new Set(chosen.value)
  if (checked) next.add(property)
  else next.delete(property)
  chosen.value = next
}
function apply() {
  if (blocked.value || !proposalMatches.value || !chosenCount.value) return
  emit('apply', Object.fromEntries(diffRows.value.filter(row => chosen.value.has(row.property)).map(row => [row.property, row.after])))
}
function dismiss() { if (!props.disabled) emit('dismiss') }
function savePreset() {
  const name = normalizeDesignPresetName(presetName.value)
  if (!blocked.value && props.canSavePreset && name && safePresets.value.length < MAX_DESIGN_PRESETS) emit('save-preset', name)
}
function applyPreset(id) { if (!blocked.value && safePresets.value.some(item => item.id === id)) emit('apply-preset', id) }
function removePreset(id) { if (!blocked.value && safePresets.value.some(item => item.id === id)) emit('remove-preset', id) }
</script>

<template>
  <section class="design-assist" aria-label="AI 样式与本项目样式">
    <p v-if="!selected" class="assist-hint">先在画布或图层中选择元素，再描述需要调整的样式。</p>
    <div class="instruction-templates" role="group" aria-label="AI 需求快捷模板"><button v-for="template in DESIGN_INSTRUCTION_TEMPLATES" :key="template.id" type="button" :disabled="blocked" :title="template.instruction" @click="fillInstruction(template)">{{ template.label }}</button></div>
    <label class="instruction-field"><span>设计要求</span><textarea :value="instruction" :disabled="blocked" rows="4" maxlength="2000" aria-label="AI 样式设计要求" placeholder="例如：文字更醒目，增加留白，调整为深色圆角卡片" @input="updateInstruction" /></label>
    <button type="button" class="request-button" :disabled="blocked || !instruction.trim()" @click="request">{{ busy ? '正在生成样式…' : '生成样式建议' }}</button>
    <button v-if="busy" type="button" class="cancel-wait" :disabled="disabled" @click="dismiss">取消等待</button>
    <p class="assist-hint">快捷模板只填写需求。生成后可按属性选择，再应用到预览。</p>
    <p v-if="notice" class="assist-notice" role="status">{{ notice }}</p>
    <section v-if="proposal" class="style-proposal" aria-label="AI 候选样式">
      <header><strong>候选样式</strong><button type="button" :disabled="disabled" @click="dismiss">取消候选</button></header>
      <p v-if="proposal.summary" class="proposal-summary">{{ proposal.summary }}</p>
      <p v-if="!proposalMatches" class="assist-hint">候选对应先前的元素，请重新生成当前元素的样式。</p>
      <label v-for="row in diffRows" :key="row.property" class="style-diff"><input type="checkbox" :checked="chosen.has(row.property)" :disabled="blocked || !proposalMatches" :aria-label="`应用${row.label}`" @change="choose(row.property, $event.target.checked)" /><span><strong>{{ row.label }}<code>{{ row.property }}</code></strong><span class="diff-before">{{ row.before }}</span><span class="diff-after">→ {{ row.after }}</span></span></label>
      <p v-if="!diffRows.length" class="assist-hint">候选与当前样式一致，或没有可应用的属性。</p>
      <button type="button" class="apply-button" :disabled="blocked || !proposalMatches || !chosenCount" @click="apply">应用选中属性（{{ chosenCount }}）</button>
    </section>
    <details class="project-presets" open>
      <summary>本项目样式 <span>{{ safePresets.length }}/{{ MAX_DESIGN_PRESETS }}</span></summary>
      <p class="assist-hint">仅保存当前元素的样式覆盖，便于复用到本项目其他元素；不包含原页面的计算样式。保存在当前浏览器，按项目隔离。</p>
      <p v-if="selected && !canSavePreset" class="assist-hint">先在设计属性中修改当前元素，再保存样式覆盖。</p>
      <div class="save-preset"><input :value="presetName" :maxlength="MAX_DESIGN_PRESET_NAME" :disabled="blocked || !canSavePreset || safePresets.length >= MAX_DESIGN_PRESETS" type="text" aria-label="本项目样式名称" placeholder="例如：主要按钮" @input="presetName = $event.target.value" @keydown.enter.prevent="savePreset" /><button type="button" :disabled="blocked || !canSavePreset || !normalizeDesignPresetName(presetName) || safePresets.length >= MAX_DESIGN_PRESETS" @click="savePreset">保存覆盖</button></div>
      <p v-if="!safePresets.length" class="assist-hint">还没有保存的样式。</p>
      <article v-for="preset in safePresets" :key="preset.id" class="preset-card"><div><strong>{{ preset.name }}</strong><span>{{ Object.keys(preset.declarations).length }} 项属性</span></div><button type="button" :disabled="blocked" :aria-label="`应用样式 ${preset.name}`" @click="applyPreset(preset.id)">应用</button><button type="button" :disabled="blocked" :aria-label="`删除样式 ${preset.name}`" @click="removePreset(preset.id)">删除</button></article>
    </details>
  </section>
</template>

<style scoped>
.design-assist { min-width: 0; font-size: 12px; color: var(--text); }.design-assist button,.design-assist input,.design-assist textarea { font: inherit; }.design-assist button { min-height: 29px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--text-2, var(--text)); cursor: pointer; }.design-assist button:hover:not(:disabled) { color: var(--accent); border-color: var(--accent-border, var(--accent)); }.design-assist button:disabled { opacity: .45; cursor: default; }.design-assist button:focus-visible,.design-assist input:focus-visible,.design-assist textarea:focus-visible { outline: 2px solid var(--accent-border, var(--accent)); outline-offset: 2px; }
.instruction-templates { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 11px; }.instruction-templates button { min-height: 26px; padding: 3px 7px; font-size: 11px; background: var(--panel-soft); }.instruction-field { display: flex; flex-direction: column; gap: 6px; color: var(--text-2); }.instruction-field textarea,.save-preset input { width: 100%; min-width: 0; box-sizing: border-box; padding: 7px 8px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel-soft); color: var(--text); }.instruction-field textarea { resize: vertical; line-height: 1.65; }.design-assist .request-button,.design-assist .apply-button { width: 100%; margin-top: 8px; background: var(--accent); border-color: var(--accent); color: var(--on-accent, #fff); }.design-assist .cancel-wait { width: 100%; margin-top: 5px; background: transparent; }.assist-hint,.assist-notice { margin: 9px 0; font-size: 11px; line-height: 1.65; color: var(--muted); overflow-wrap: anywhere; }.assist-notice { color: var(--accent); }
.style-proposal { margin-top: 14px; padding: 10px; border: 1px solid var(--accent-border, var(--border)); border-radius: 6px; background: var(--accent-soft); }.style-proposal header { display: flex; align-items: center; justify-content: space-between; gap: 6px; }.style-proposal header strong { font-size: 12px; }.style-proposal header button { border: 0; padding: 2px 3px; min-height: 24px; background: transparent; font-size: 11px; }.proposal-summary { font-size: 11px; line-height: 1.7; overflow-wrap: anywhere; }.style-diff { display: flex; align-items: flex-start; gap: 6px; padding: 9px 0; border-top: 1px solid var(--border); cursor: pointer; }.style-diff > input { flex: none; margin: 2px 0 0; accent-color: var(--accent); }.style-diff > span { min-width: 0; display: flex; flex-direction: column; gap: 4px; overflow-wrap: anywhere; }.style-diff strong { display: flex; flex-wrap: wrap; gap: 5px; font-size: 11px; font-weight: 500; }.style-diff code { color: var(--muted); font-size: 10px; }.diff-before { color: var(--muted); text-decoration: line-through; font-size: 11px; }.diff-after { color: var(--accent); font-size: 11px; }
.project-presets { margin-top: 18px; padding-top: 13px; border-top: 1px solid var(--border); }.project-presets summary { cursor: pointer; font-weight: 500; }.project-presets summary span { margin-left: 5px; color: var(--muted); font-size: 11px; }.save-preset { display: flex; gap: 5px; }.save-preset input { flex: 1; padding: 5px 6px; font-size: 11px; }.save-preset button { flex: none; padding: 4px 6px; font-size: 11px; }.preset-card { display: flex; gap: 5px; align-items: center; padding: 9px 0; border-bottom: 1px solid var(--border); }.preset-card > div { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 4px; }.preset-card strong { overflow-wrap: anywhere; font-size: 11px; font-weight: 500; }.preset-card span { color: var(--muted); font-size: 10px; }.preset-card button { min-height: 25px; padding: 3px 5px; font-size: 11px; }
</style>
