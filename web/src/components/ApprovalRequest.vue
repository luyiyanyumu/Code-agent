<script setup>
import { computed } from 'vue'

const props = defineProps({
  approval: { type: Object, required: true },
  submitting: { type: Boolean, default: false },
  readonly: { type: Boolean, default: false },
})
const emit = defineEmits(['decide'])
const purpose = computed(() => typeof props.approval.purpose === 'string' ? props.approval.purpose.trim() : '')
const effects = computed(() => {
  const value = props.approval.effects
  return (Array.isArray(value) ? value : [value])
    .filter(effect => typeof effect === 'string' && effect.trim())
    .map(effect => effect.trim())
})
const argumentsValue = computed(() => {
  const value = props.approval.arguments
  if (typeof value !== 'string') return value
  try {
    const parsed = JSON.parse(value)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed
  } catch { /* Legacy argument descriptions remain readable verbatim. */ }
  return value
})
const command = computed(() => typeof argumentsValue.value?.command === 'string' ? argumentsValue.value.command : '')
const argumentsText = computed(() => {
  const args = argumentsValue.value
  if (typeof args === 'string') return args
  if (!args) return ''
  const rest = command.value ? Object.fromEntries(Object.entries(args).filter(([key]) => key !== 'command')) : args
  return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : ''
})
</script>

<template>
  <section class="approval-request" aria-label="操作权限申请" :aria-busy="submitting">
    <header><strong>等待你批准</strong><code>{{ approval.tool }}</code></header>
    <div class="approval-body" tabindex="0" aria-label="本次操作的目的、影响、申请原因及完整命令和参数">
      <section class="approval-purpose" :class="{ missing: !purpose }" aria-label="操作目的">
        <h3>操作目的<small v-if="purpose">智能体说明</small></h3>
        <p>{{ purpose || '尚未提供操作目的，请先让智能体说明后再决定是否批准。' }}</p>
      </section>
      <section class="approval-context" aria-label="操作影响">
        <h3>操作影响<small v-if="effects.length">智能体说明</small></h3>
        <ul v-if="effects.length"><li v-for="(effect, index) in effects" :key="index">{{ effect }}</li></ul>
        <p v-else class="approval-unexplained">尚未说明，按下方命令与参数核对。</p>
      </section>
      <section class="approval-context" aria-label="申请原因">
        <h3>申请原因</h3>
        <p class="approval-reason">{{ approval.reason || '这次操作需要你的授权。' }}</p>
      </section>
      <div v-if="command || argumentsText" class="approval-details">
        <template v-if="command"><span>命令</span><pre>{{ command }}</pre></template>
        <template v-if="argumentsText"><span>参数</span><pre>{{ argumentsText }}</pre></template>
      </div>
    </div>
    <footer class="approval-footer">
      <p class="approval-scope">{{ readonly ? '会话仍保持只读；批准只授权本次操作。' : '批准只授权本次操作，不改变会话权限。' }}</p>
      <div class="approval-actions">
        <button class="primary" :disabled="submitting" @click="emit('decide', true)">{{ submitting ? '正在提交…' : '仅批准本次' }}</button>
        <button class="danger" :disabled="submitting" @click="emit('decide', false)">拒绝</button>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.approval-request { flex: 0 1 auto; box-sizing: border-box; min-width: 0; min-height: 0; width: 100%; max-width: 560px; max-height: min(60vh, 100%); display: flex; flex-direction: column; gap: 10px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); font-size: var(--fs-sm); }
.approval-request > header, .approval-footer { flex: none; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: var(--warn); } header code { color: var(--text); font-size: var(--fs-xs); overflow-wrap: anywhere; }
p { margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; }.approval-scope { font-size: var(--fs-xs); color: var(--muted); }
h3 { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; margin: 0 0 5px; font-size: var(--fs-sm); color: var(--text); } h3 small, .approval-unexplained { font-size: var(--fs-xs); color: var(--muted); font-weight: normal; }.approval-purpose { padding: 10px; margin-bottom: 10px; border: 1px solid var(--border); border-radius: 5px; background: var(--accent-soft, var(--panel-soft)); }.approval-purpose.missing { color: var(--warn); }.approval-purpose.missing h3 { color: inherit; }.approval-context { margin-bottom: 10px; }.approval-context ul { margin: 0; padding-left: 20px; }.approval-context li { margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
.approval-body { flex: 1 1 auto; min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.approval-body:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.approval-details { padding: 10px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 5px; }.approval-details span { color: var(--muted); font-size: var(--fs-xs); } pre { margin: 5px 0 10px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: var(--fs-xs); } pre:last-child { margin-bottom: 0; }
.approval-footer { display: flex; flex-direction: column; gap: 8px; padding-top: 8px; border-top: 1px solid var(--border); }
.approval-actions { display: flex; flex-wrap: wrap; gap: 8px; }.approval-actions button { min-height: 32px; font-size: var(--fs-xs); }
</style>
