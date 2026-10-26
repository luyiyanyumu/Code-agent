<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { dialogService } from '../../composables/useDialog.js'
import UiIcon from './UiIcon.vue'

const modal = ref(null), input = ref(null), cancelButton = ref(null)
const request = dialogService.active
const options = computed(() => request.value?.options || {})
const dangerous = computed(() => options.value.tone === 'danger')
const caution = computed(() => dangerous.value || options.value.tone === 'warning')
const icon = computed(() => options.value.icon || (caution.value ? 'warning' : options.value.input ? 'edit' : 'check'))
let previousFocus = null, backdropDown = false

watch(() => request.value?.id, async id => {
  if (!id) {
    modal.value?.close()
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
    previousFocus = null
    return
  }
  if (!modal.value?.open) previousFocus = document.activeElement
  await nextTick()
  if (request.value?.id !== id || !modal.value) return
  if (!modal.value.open) modal.value.showModal()
  await nextTick()
  if (request.value?.id !== id) return
  const target = options.value.input ? input.value : cancelButton.value
  target?.focus({ preventScroll: true })
  input.value?.select()
}, { flush: 'post', immediate: true })

watch(() => request.value?.busy, async busy => {
  const id = request.value?.id
  if (!id) return
  await nextTick()
  if (request.value?.id !== id || !modal.value?.open) return
  // Disabled controls lose focus in browsers; keep pending dialogs keyboard-isolated.
  if (busy) modal.value.focus({ preventScroll: true })
  else if (document.activeElement === modal.value) (input.value || cancelButton.value)?.focus({ preventScroll: true })
}, { flush: 'post' })

function outside(event) {
  if (event.target !== modal.value) return false
  const rect = modal.value.getBoundingClientRect()
  return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom
}
function backdropClick(event) {
  if (backdropDown && outside(event)) dialogService.cancel()
  backdropDown = false
}
function keepFocus(event) {
  if (event.key !== 'Tab') return
  const controls = [...modal.value.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]')]
    .filter(element => element.getClientRects().length)
  if (!controls.length) { event.preventDefault(); return }
  const first = controls[0], last = controls.at(-1)
  if (event.shiftKey && (document.activeElement === first || !modal.value.contains(document.activeElement))) {
    event.preventDefault(); last.focus()
  } else if (!event.shiftKey && (document.activeElement === last || !modal.value.contains(document.activeElement))) {
    event.preventDefault(); first.focus()
  }
}
onBeforeUnmount(() => { modal.value?.close() })
</script>

<template>
  <Teleport to="body">
    <dialog ref="modal" class="app-dialog" tabindex="-1" :class="{ 'is-danger': dangerous, 'is-warning': options.tone === 'warning' }"
      aria-labelledby="app-dialog-title" :aria-describedby="[options.description ? 'app-dialog-description' : '', options.details ? 'app-dialog-details' : ''].filter(Boolean).join(' ') || undefined" :aria-busy="request?.busy || undefined"
      @cancel.prevent="dialogService.cancel()" @keydown.stop="keepFocus" @pointerdown="backdropDown = outside($event)" @click="backdropClick">
      <form v-if="request" class="dialog-form" novalidate @submit.prevent="dialogService.submit()">
        <header class="dialog-heading">
          <span class="dialog-symbol"><UiIcon :name="icon" :size="23" /></span>
          <button type="button" class="dialog-close ghost" aria-label="关闭弹窗" :disabled="request.busy" @click="dialogService.cancel()"><UiIcon name="close" :size="19" /></button>
          <div class="dialog-heading-text">
            <h2 id="app-dialog-title">{{ options.title }}</h2>
            <p id="app-dialog-description">{{ options.description }}</p>
          </div>
        </header>
        <div class="dialog-body">
          <div v-if="options.details" id="app-dialog-details" class="dialog-details">{{ options.details }}</div>
          <div v-if="options.input" class="dialog-field">
            <label for="app-dialog-input">{{ options.input.label || '内容' }}<span v-if="options.input.required" class="field-required">必填</span></label>
            <component :is="options.input.multiline ? 'textarea' : 'input'" id="app-dialog-input" ref="input"
              :value="request.value" :placeholder="options.input.placeholder" :maxlength="options.input.maxLength" :rows="options.input.multiline ? 3 : undefined"
              :list="options.input.suggestions?.length ? 'app-dialog-suggestions' : undefined" :disabled="request.busy" :required="options.input.required"
              :aria-invalid="request.error ? 'true' : undefined" :aria-describedby="[options.input.hint ? 'app-dialog-hint' : '', request.error ? 'app-dialog-error' : ''].filter(Boolean).join(' ') || undefined"
              autocomplete="off" @input="dialogService.setValue($event.target.value)" />
            <datalist v-if="options.input.suggestions?.length" id="app-dialog-suggestions"><option v-for="item in options.input.suggestions" :key="item" :value="item" /></datalist>
            <p v-if="options.input.hint" id="app-dialog-hint" class="dialog-hint">{{ options.input.hint }}</p>
          </div>
          <div v-if="request.error" id="app-dialog-error" class="dialog-error" role="alert"><UiIcon name="warning" :size="17" /><span>{{ request.error }}</span></div>
        </div>
        <footer class="dialog-footer">
          <span class="dialog-pending" role="status">{{ request.busy ? (options.pendingText || '正在处理…') : '' }}</span>
          <button ref="cancelButton" type="button" :disabled="request.busy" @click="dialogService.cancel()">{{ options.cancelText || '取消' }}</button>
          <button type="submit" class="primary dialog-confirm" :disabled="request.busy">
            <span v-if="request.busy" class="dialog-spinner" aria-hidden="true"></span>
            {{ request.busy ? (options.pendingText || '正在处理…') : (options.confirmText || '确认') }}
          </button>
        </footer>
      </form>
    </dialog>
  </Teleport>
</template>

<style scoped>
.app-dialog { padding: 0; border: 1px solid var(--border); border-radius: 18px; color: var(--text); background: var(--panel); width: min(480px, calc(100vw - 32px)); max-height: calc(100dvh - 40px); box-shadow: 0 24px 80px #17233c30, 0 4px 16px #17233c12; overflow: auto; }
.app-dialog::backdrop { background: #17233c66; backdrop-filter: blur(3px); }
.dialog-form { margin: 0; }
.dialog-heading { position: relative; padding: 28px 28px 0; }
.dialog-symbol { display: grid; place-items: center; width: 46px; height: 46px; border: 1px solid var(--accent-border); border-radius: 13px; color: var(--accent); background: var(--accent-soft); margin-bottom: 18px; }
.is-danger .dialog-symbol { color: var(--err); border-color: #fecaca; background: var(--err-soft); }
.is-warning .dialog-symbol { color: #a45d08; border-color: #fde3b4; background: var(--warn-soft); }
.dialog-close { position: absolute; right: 17px; top: 17px; width: 32px; min-height: 32px; padding: 0; }
.dialog-heading h2 { margin: 0; font-size: 20px; line-height: 1.4; font-weight: 650; letter-spacing: -.02em; overflow-wrap: anywhere; }
.dialog-heading p { margin: 8px 0 0; color: var(--muted); font-size: 13px; line-height: 1.7; white-space: pre-line; overflow-wrap: anywhere; }
.dialog-heading p:empty { display: none; }
.dialog-body { padding: 20px 28px 26px; display: grid; gap: 18px; }
.dialog-details { padding: 12px 14px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 9px; font-size: 13px; line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 180px; overflow-y: auto; }
.dialog-field { display: grid; gap: 8px; }
.dialog-field label { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
.field-required { color: var(--muted); font-size: 11px; font-weight: 400; }
.dialog-field input, .dialog-field textarea { width: 100%; min-width: 0; padding: 10px 12px; font-size: 14px; }
.dialog-field textarea { resize: vertical; min-height: 84px; max-height: 180px; }
.dialog-field [aria-invalid="true"] { border-color: var(--err); }
.dialog-hint { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.dialog-error { display: flex; align-items: flex-start; gap: 8px; padding: 10px 12px; border-radius: 8px; color: #b42318; background: var(--err-soft); font-size: 13px; overflow-wrap: anywhere; }
.dialog-error svg { flex: none; margin-top: 2px; }
.dialog-error span { min-width: 0; }
.dialog-footer { padding: 16px 28px; display: flex; justify-content: flex-end; align-items: center; gap: 10px; border-top: 1px solid var(--border); background: var(--panel-soft); }
.dialog-footer button { min-height: 38px; padding-inline: 18px; }
.dialog-pending { margin-right: auto; color: var(--muted); font-size: 12px; }
.dialog-confirm { gap: 7px; }
.is-danger .dialog-confirm { background: #cf3038; border-color: #cf3038; }
.is-danger .dialog-confirm:hover:not(:disabled) { background: #b5232b; border-color: #b5232b; }
.dialog-spinner { display: inline-block; width: 13px; height: 13px; border: 2px solid #ffffff60; border-top-color: white; border-radius: 50%; animation: dialog-spin .7s linear infinite; }
@keyframes dialog-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .dialog-spinner { animation: none; } }
@media (max-width: 480px) { .dialog-heading { padding: 24px 20px 0; } .dialog-body { padding: 18px 20px 24px; } .dialog-footer { padding: 14px 20px; flex-wrap: wrap; } .dialog-pending:not(:empty) { width: 100%; } }
</style>
