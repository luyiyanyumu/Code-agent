<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { DESIGN_VIEWPORT_PRESETS, normalizeDesignViewport } from '../../lib/designViewport.js'

const props = defineProps({ width: { type: Number, default: 1280 }, height: { type: Number, default: 800 }, custom: { type: Boolean, default: false }, disabled: { type: Boolean, default: false } })
const emit = defineEmits(['change'])
const open = ref(false), draftWidth = ref(1280), draftHeight = ref(800), error = ref('')
const rootEl = ref(null), popoverStyle = ref({})
watch(open, value => {
  if (!value) return
  draftWidth.value = props.width; draftHeight.value = props.height; error.value = ''
  const anchor = rootEl.value?.getBoundingClientRect()
  if (anchor) {
    const bounds = rootEl.value.closest('.frontend-design')?.getBoundingClientRect() || { left: 0, right: window.innerWidth }
    const width = Math.min(264, Math.max(0, bounds.right - bounds.left - 24))
    const left = Math.max(bounds.left + 12 - anchor.left, Math.min(0, bounds.right - 12 - anchor.left - width))
    popoverStyle.value = { left: `${left}px`, width: `${width}px`, maxWidth: `${width}px` }
  }
})
function apply(width = Number(draftWidth.value), height = Number(draftHeight.value)) {
  if (props.disabled) return
  const size = normalizeDesignViewport({ width, height })
  if (!size) { error.value = '宽度须为 240–3840，高度须为 240–2160 的整数。'; return }
  emit('change', size); open.value = false
}
function close(event) { if (event.key === 'Escape') { open.value = false; event.stopPropagation() } }
const closeOnResize = () => { open.value = false }
onMounted(() => window.addEventListener('resize', closeOnResize))
onBeforeUnmount(() => window.removeEventListener('resize', closeOnResize))
</script>

<template>
  <div ref="rootEl" class="design-viewport-controls" @keydown="close">
    <button type="button" class="viewport-toggle" :class="{ active: custom }" aria-label="设置画布尺寸" :aria-expanded="open" :disabled="disabled" @click="open = !open">{{ custom ? `${width}×${height}` : '尺寸' }}</button>
    <section v-if="open" class="viewport-popover" :style="popoverStyle" aria-label="画布尺寸设置">
      <header><strong>画布尺寸</strong><button type="button" aria-label="关闭尺寸设置" @click="open = false">×</button></header>
      <div class="viewport-presets"><button v-for="preset in DESIGN_VIEWPORT_PRESETS" :key="preset.id" type="button" :disabled="disabled" @click="apply(preset.width, preset.height)"><span>{{ preset.label }}</span><small>{{ preset.width }} × {{ preset.height }}</small></button></div>
      <form @submit.prevent="apply()"><div class="viewport-dimensions"><label>宽度<input v-model="draftWidth" type="number" min="240" max="3840" step="1" aria-label="画布宽度" :disabled="disabled" /></label><span>×</span><label>高度<input v-model="draftHeight" type="number" min="240" max="2160" step="1" aria-label="画布高度" :disabled="disabled" /></label><span>px</span></div><p v-if="error" role="alert">{{ error }}</p><div class="viewport-actions"><button type="button" :disabled="disabled" @click="[draftWidth, draftHeight] = [draftHeight, draftWidth]">交换宽高</button><button type="submit" :disabled="disabled" class="apply-size">应用尺寸</button></div></form>
      <p class="viewport-note">只改变预览视口；可用「适应」查看完整页面。</p>
    </section>
  </div>
</template>

<style scoped>
.design-viewport-controls { position: relative; display: inline-flex; font-size: 11px; }
.viewport-toggle { white-space: nowrap; min-height: 25px; padding: 3px 7px; font-size: 11px; line-height: 18px; }.viewport-popover { position: absolute; z-index: 20; left: 0; top: calc(100% + 7px); width: 264px; max-width: calc(100vw - 30px); padding: 12px; border: 1px solid var(--border-strong); border-radius: 8px; box-shadow: 0 8px 28px #17233c24; background: var(--panel); color: var(--text); }
.viewport-popover header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }.viewport-popover header button { border: 0; background: transparent; padding: 2px 7px; color: var(--muted); font-size: 16px; }
.viewport-presets { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 14px; }.viewport-presets button { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; padding: 7px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel-soft); color: var(--text); }.viewport-presets small { color: var(--muted); font-size: 10px; }
.viewport-dimensions { display: flex; align-items: end; gap: 6px; }.viewport-dimensions label { display: flex; flex-direction: column; gap: 4px; min-width: 0; color: var(--muted); }.viewport-dimensions input { width: 100%; min-width: 0; border: 1px solid var(--border); border-radius: 4px; padding: 6px; background: var(--panel-soft); color: var(--text); }.viewport-dimensions > span { padding-bottom: 6px; color: var(--muted); }
.viewport-actions { display: flex; justify-content: space-between; margin-top: 10px; gap: 8px; }.viewport-actions button { padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--panel); color: var(--text); }.viewport-actions .apply-size { background: var(--accent); color: var(--on-accent, #fff); border-color: var(--accent); }
.viewport-popover p { color: var(--muted); font-size: 10px; line-height: 1.6; margin: 10px 0 0; }.viewport-popover p[role="alert"] { color: var(--error); }
</style>
