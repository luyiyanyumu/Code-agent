<script setup>
defineProps({
  size: { type: Number, default: 28 },
  wordmark: { type: Boolean, default: true },
})
</script>

<template>
  <span class="agent-logo" role="img" aria-label="Code-agent 编码智能体" title="Code-agent · 编码智能体">
    <img class="agent-logo__mark" src="/brand/code-agent-mark.png" alt="" :width="size" :height="size" draggable="false" />
    <span v-if="wordmark" class="agent-logo__wordmark" aria-hidden="true">Code-agent</span>
  </span>
</template>

<style scoped>
.agent-logo { display: inline-flex; align-items: center; justify-content: center; gap: 7px; flex-shrink: 0; vertical-align: middle; color: var(--text); }
.agent-logo__mark { display: block; flex-shrink: 0; object-fit: contain; }
.agent-logo__wordmark { font-size: var(--fs-sm); font-weight: var(--fw-semibold); line-height: 1; letter-spacing: -.025em; white-space: nowrap; }
@media (max-width: 1100px) { .agent-logo__wordmark { display: none; } }
</style>
