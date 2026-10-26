<script setup>
import { computed } from 'vue'

const props = defineProps({
  explanation: { type: Object, default: null },
  expanded: { type: Boolean, default: false },
})
// Only the explicit public explanation fields belong in this card. Model
// thinking/reasoning payloads are never a fallback for missing explanations.
function publicText(value) {
  if (typeof value === 'string') return value.trim()
  if (Array.isArray(value)) return value.filter(item => typeof item === 'string').map(item => item.trim()).filter(Boolean).join('\n')
  return ''
}
const purpose = computed(() => publicText(props.explanation?.purpose))
const basis = computed(() => publicText(props.explanation?.basis))
const effects = computed(() => publicText(props.explanation?.effects))
const provided = computed(() => !!(purpose.value || basis.value || effects.value))
</script>

<template>
  <div class="tool-explanation" :class="{ 'is-expanded': expanded, missing: !provided }">
    <p v-if="!provided" class="explanation-missing">此记录未提供执行说明</p>
    <p v-else-if="!expanded" class="explanation-summary"><strong>执行说明：</strong>{{ purpose || '已提供依据或预期影响，展开查看。' }}</p>
    <template v-else>
      <div class="explanation-title"><strong>思路摘要</strong><small>智能体说明</small></div>
      <dl>
        <div><dt>调用目的</dt><dd>{{ purpose || '未提供调用目的。' }}</dd></div>
        <div><dt>依据的已知事实</dt><dd>{{ basis || '未提供依据。' }}</dd></div>
        <div><dt>预期影响</dt><dd>{{ effects || '未提供预期影响。' }}</dd></div>
      </dl>
      <p class="explanation-note">预期影响用于说明计划；实际执行请核对工具状态和结果。</p>
    </template>
  </div>
</template>

<style scoped>
.tool-explanation { padding: 7px 11px; border-top: 1px solid var(--border); color: var(--text-2); background: var(--panel); font-size: var(--fs-2xs); line-height: 1.65; min-width: 0; }
.explanation-summary, .explanation-missing { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
.explanation-summary { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.explanation-summary strong { color: var(--text); font-weight: var(--fw-medium); }
.explanation-missing { color: var(--muted); }
.is-expanded { padding: 10px 12px; }
.explanation-title { display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px; color: var(--text); margin-bottom: 8px; }
.explanation-title strong { font-weight: var(--fw-medium); }.explanation-title small { font-size: inherit; color: var(--muted); }
dl { margin: 0; display: grid; gap: 8px; } dl > div { min-width: 0; } dt { margin: 0 0 2px; color: var(--text); font-weight: var(--fw-medium); } dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
.explanation-note { margin: 10px 0 0; color: var(--muted); }
</style>
