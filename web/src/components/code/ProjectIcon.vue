<script setup>
defineProps({ name: { type: String, default: 'folder' } })
</script>

<template>
  <svg class="project-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
    <template v-if="name === 'workspace'"><path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="m9 11-3 3 3 3m6-6 3 3-3 3m-2-7-2 8"/></template>
    <path v-else-if="name === 'folder'" d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>
    <template v-else-if="name === 'folder-open'"><path d="M3 16V7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v2"/><path d="M5 11h16l-3 9H3Z"/></template>
    <path v-else-if="name === 'plus'" d="M12 5v14M5 12h14"/>
    <path v-else-if="name === 'chevron'" d="m7 10 5 5 5-5"/>
    <path v-else-if="name === 'check'" d="m5 12 4 4L19 6"/>
    <template v-else-if="name === 'search'"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></template>
    <template v-else-if="name === 'clock'"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></template>
    <path v-else-if="name === 'back'" d="m10 6-6 6 6 6M4 12h16"/>
    <path v-else-if="name === 'close'" d="m6 6 12 12M6 18 18 6"/>
    <template v-else-if="name === 'branch'"><circle cx="7" cy="5" r="2"/><circle cx="7" cy="19" r="2"/><circle cx="17" cy="6" r="2"/><path d="M7 7v10m10-9a5 5 0 0 1-5 5H7"/></template>
  </svg>
</template>

<style scoped>
.project-icon { display: block; width: 18px; height: 18px; flex: none; }
</style>
