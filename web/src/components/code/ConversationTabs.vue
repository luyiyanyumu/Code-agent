<script setup>
import UiIcon from '../ui/UiIcon.vue'
import { sessionPhaseLabel } from '../../lib/sessionPresentation.js'

defineProps({ conversations: { type: Array, default: () => [] }, selectedId: { type: String, default: '' }, creating: Boolean, deleting: { type: String, default: '' }, loading: Boolean, disabled: Boolean, error: { type: String, default: '' } })
const emit = defineEmits(['select', 'create', 'delete', 'refresh'])
function move(event, index, items) {
  const offset = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
  if (!offset && event.key !== 'Home' && event.key !== 'End') return
  event.preventDefault()
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + offset + items.length) % items.length
  emit('select', items[next].id)
  event.currentTarget?.closest('[role="tablist"]')?.querySelectorAll('[role="tab"]')[next]?.focus()
}
</script>

<template>
  <div class="conversation-navigation">
    <div class="conversation-tabs" role="tablist" aria-label="项目对话">
      <div v-for="(item, index) in conversations" :key="item.id" class="conversation-tab" :class="{ selected: selectedId === item.id }">
      <button role="tab" :aria-selected="selectedId === item.id" :tabindex="selectedId === item.id ? 0 : -1" :title="`${item.title || '项目对话'}${item.busy ? ` · ${sessionPhaseLabel(item.phase) || '执行中'}` : ''}`" :disabled="disabled" @click="emit('select', item.id)" @keydown="move($event, index, conversations)">
        <span v-if="item.busy" class="running-dot" aria-label="后台执行中" /><span class="tab-title">{{ item.title || '项目对话' }}</span>
      </button>
      <button type="button" class="delete-conversation" :aria-label="`删除对话 ${item.title || '项目对话'}`" :title="`删除对话 ${item.title || '项目对话'}`" :disabled="disabled || creating || !!deleting" @click.stop="emit('delete', item.id)"><UiIcon name="close" :size="11" /></button>
      </div>
    </div>
    <button type="button" class="new-conversation" :disabled="disabled || creating || !!deleting" :aria-busy="creating" title="新建独立项目对话，当前任务会继续执行" @click="emit('create')"><UiIcon name="plus" :size="13" />{{ creating ? '新建中…' : '新建对话' }}</button>
    <div v-if="error" class="conversation-error" role="alert"><span>{{ error }}</span><button type="button" :disabled="loading" @click="emit('refresh')">刷新列表</button></div>
  </div>
</template>

<style scoped>
.conversation-navigation { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; padding: 0 12px 8px; border-bottom: 1px solid var(--border); min-width: 0; }
.conversation-tabs { display: flex; gap: 3px; min-width: 0; flex: 1 1 100px; overflow-x: auto; scrollbar-width: thin; }
.conversation-tab { display: flex; align-items: center; flex: none; min-width: 0; border: 1px solid transparent; border-radius: 5px; }.conversation-tab.selected { color: var(--accent); background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 25%, transparent); }
.conversation-tabs button { display: flex; align-items: center; gap: 5px; flex: none; max-width: 145px; padding: 5px 7px; color: var(--muted); background: transparent; border: 0; border-radius: 4px; font-size: var(--fs-2xs); cursor: pointer; }.conversation-tab.selected button { color: var(--accent); }.conversation-tabs .delete-conversation { padding: 5px 3px; margin-right: 2px; }.delete-conversation:hover { background: var(--panel); color: var(--danger); }
.tab-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.running-dot { width: 5px; height: 5px; flex: none; border-radius: 50%; background: var(--ok); }
.new-conversation { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; padding: 5px 7px; color: var(--accent); border: 1px solid var(--border); border-radius: 5px; background: var(--panel); font-size: var(--fs-2xs); cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.conversation-error { display: flex; align-items: center; gap: 8px; width: 100%; color: var(--danger); font-size: var(--fs-2xs); }.conversation-error span { flex: 1; }.conversation-error button { background: transparent; color: inherit; border: 0; text-decoration: underline; cursor: pointer; }
</style>
