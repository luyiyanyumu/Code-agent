<script setup>
// Compatibility entry point: all project agents are background tasks.
import BackgroundAgentTasks from './BackgroundAgentTasks.vue'
defineOptions({ inheritAttrs: false })
</script>

<template>
  <BackgroundAgentTasks v-bind="$attrs" />
</template>
