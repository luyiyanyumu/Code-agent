<script setup>
import UiIcon from './UiIcon.vue'
defineProps({ title: String, description: String, eyebrow: String, icon: String })
</script>

<template>
  <header class="page-header">
    <div class="page-header-copy">
      <span v-if="icon" class="page-header-icon"><UiIcon :name="icon" :size="22" /></span>
      <div class="page-header-text">
        <p v-if="eyebrow" class="page-eyebrow">{{ eyebrow }}</p>
        <h1>{{ title }}</h1>
        <p v-if="description" class="page-header-description">{{ description }}</p>
        <slot />
      </div>
    </div>
    <div v-if="$slots.actions" class="page-header-actions"><slot name="actions" /></div>
  </header>
</template>

<style scoped>
.page-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 28px; }
.page-header-copy { display: flex; align-items: center; gap: 14px; min-width: 0; }
.page-header-icon { display: grid; place-items: center; flex: none; width: 46px; height: 46px; border: 1px solid var(--border); border-radius: 13px; background: var(--panel); color: var(--accent); box-shadow: var(--shadow-sm); }
.page-header-text { min-width: 0; }
.page-eyebrow { margin: 0 0 5px; color: var(--muted); font-size: var(--fs-xs); font-weight: 500; }
h1 { margin: 0; font-size: var(--fs-2xl); font-weight: 650; line-height: 1.3; letter-spacing: -.025em; color: var(--text); }
.page-header-description { margin: 6px 0 0; font-size: var(--fs-sm); color: var(--muted); line-height: 1.65; text-wrap: pretty; }
.page-header-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; flex-shrink: 0; }
@media (max-width: 760px) { .page-header { align-items: flex-start; flex-direction: column; gap: 16px; margin-bottom: 20px; }.page-header-copy { gap: 12px; }.page-header-icon { width: 40px; height: 40px; border-radius: 11px; } h1 { font-size: 22px; }.page-header-actions { width: 100%; } }
</style>
