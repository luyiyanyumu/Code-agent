<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { desktopRuntime } from '../lib/desktopRuntime.js'

const available = desktopRuntime.integratedTitlebar()
const state = ref({ maximized: false, minimized: false, active: true })
const busy = ref(''), error = ref('')
let release = null, errorTimer = null
async function act(method) {
  if (busy.value) return
  busy.value = method; error.value = ''
  try { await desktopRuntime.request(method, {}, { timeout: 10000 }) }
  catch (failure) {
    error.value = failure?.message || '窗口操作暂未完成，请重试。'
    clearTimeout(errorTimer); errorTimer = setTimeout(() => { error.value = '' }, 6000)
  } finally { busy.value = '' }
}
onMounted(() => {
  if (!available) return
  release = desktopRuntime.subscribeWindowState(value => { state.value = value })
  // State updates also arrive when Windows shortcuts or native snapping change
  // the window, so the restore icon follows the real host state.
  void desktopRuntime.request('window-state', {}, { timeout: 5000 }).catch(() => {})
})
onBeforeUnmount(() => { release?.(); clearTimeout(errorTimer) })
</script>

<template>
  <div v-if="available" class="desktop-window-controls" :class="{ inactive: !state.active }" role="group" aria-label="窗口控制">
    <button type="button" :disabled="!!busy" aria-label="最小化窗口" title="最小化" @click="act('window-minimize')"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M1 6.5h10" /></svg></button>
    <button type="button" :disabled="!!busy" :aria-label="state.maximized ? '还原窗口' : '最大化窗口'" :title="state.maximized ? '还原' : '最大化'" @click="act('window-toggle-maximize')"><svg viewBox="0 0 12 12" aria-hidden="true"><path v-if="state.maximized" d="M3.5 3.5V1.5h7v7h-2m-7-5h7v7h-7z" /><path v-else d="M1.5 1.5h9v9h-9z" /></svg></button>
    <button type="button" class="window-close" :disabled="!!busy" aria-label="关闭窗口" title="关闭" @click="act('window-close')"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="m1.5 1.5 9 9m-9 0 9-9" /></svg></button>
    <span v-if="error" class="window-error" role="status">{{ error }}</span>
  </div>
</template>

<style scoped>
.desktop-window-controls { position: fixed; top: 0; right: 0; z-index: 2000; display: flex; height: 35px; width: 138px; border-bottom: 1px solid var(--border); background: var(--panel-soft); color: var(--text); user-select: none; -webkit-app-region: no-drag; app-region: no-drag; }
.desktop-window-controls > button { display: inline-flex; justify-content: center; align-items: center; width: 46px; height: 34px; flex: none; min-height: 0; padding: 0; margin: 0; border: 0; border-radius: 0; box-shadow: none; color: inherit; background: transparent; cursor: default; -webkit-app-region: no-drag; app-region: no-drag; }
.desktop-window-controls > button:hover:not(:disabled) { background: var(--hover); }
.desktop-window-controls > button:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
.desktop-window-controls > button.window-close:hover:not(:disabled) { color: #fff; background: #c42b1c; }
.desktop-window-controls > button:disabled { opacity: .65; }
.desktop-window-controls svg { display: block; width: 11px; height: 11px; stroke: currentColor; stroke-width: 1; fill: none; }
.desktop-window-controls.inactive { color: var(--muted); }
.window-error { position: absolute; top: 40px; right: 8px; width: max-content; max-width: min(320px, calc(100vw - 16px)); padding: 8px 10px; color: var(--err); background: var(--panel); border: 1px solid var(--border-strong); border-radius: 5px; box-shadow: var(--shadow-pop); font-size: 12px; white-space: normal; overflow-wrap: anywhere; }
</style>
