<script setup>
import UiIcon from './UiIcon.vue'
defineProps({ title: { type: String, default: '暂无内容' }, description: String, icon: { type: String, default: 'inbox' } })
</script>

<template>
  <div class="empty-state">
    <span class="empty-state-icon"><UiIcon :name="icon" :size="25" /></span>
    <h3>{{ title }}</h3>
    <p v-if="description">{{ description }}</p>
    <div v-if="$slots.default" class="empty-state-actions"><slot /></div>
  </div>
</template>

<style scoped>
.empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 44px 24px; text-align: center; min-width: 0; color: var(--muted); }
.empty-state-icon { display: grid; place-items: center; width: 58px; height: 58px; margin-bottom: 17px; border: 1px solid var(--border); border-radius: 18px; background: var(--panel-soft); color: var(--faint); }
h3 { margin: 0; color: var(--text); font-size: var(--fs-md); font-weight: 600; }
p { max-width: 420px; margin: 8px 0 0; font-size: var(--fs-sm); line-height: 1.75; overflow-wrap: anywhere; }
.empty-state-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-top: 20px; }
</style>
