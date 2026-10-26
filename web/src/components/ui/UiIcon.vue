<script setup>
defineProps({ name: { type: String, default: 'code' }, size: { type: [Number, String], default: 18 } })
const icons = {
  edit: 'm16 3 5 5-12 12-6 1 1-6L16 3Zm-2 2 5 5',
  trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  code: 'm8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18',
  chat: 'M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 4V6a2 2 0 0 1 2-2Z',
  memory: 'M6 3h12v18l-6-4-6 4V3Z',
  knowledge: 'M12 5c-3-2-6-2-10-1v15c4-1 7-1 10 1m0-15c3-2 6-2 10-1v15c-4-1-7-1-10 1V5Z',
  tools: 'M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z',
  usage: 'M4 20V10m5 10V4m6 16v-7m5 7V7M2 22h20',
  automations: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4v5l3 2',
  audit: 'm12 3 8 3v6c0 5-8 10-8 10S4 17 4 12V6l8-3Zm-4 9 3 3 5-6',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0-6v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2',
  folder: 'M3 5h6l2 3h10v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5Z',
  'folder-open': 'M3 9V5h6l2 3h9v3M3 9h18l-3 11H5L3 9Z',
  file: 'M5 3h9l5 5v13H5V3Zm9 0v6h5',
  'file-code': 'M5 3h9l5 5v13H5V3Zm9 0v6h5m-9 3-2 3 2 3m4-6 2 3-2 3',
  search: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 6 6',
  plus: 'M12 5v14M5 12h14', close: 'm6 6 12 12M6 18 18 6',
  'chevron-down': 'm6 9 6 6 6-6', 'chevron-right': 'm9 6 6 6-6 6',
  'arrow-left': 'M20 12H4m6-6-6 6 6 6', 'arrow-right': 'M4 12h16m-6-6 6 6-6 6',
  play: 'm7 4 14 8-14 8V4Z', terminal: 'm4 6 6 6-6 6m9 0h7',
  'git-branch': 'M6 7v10m0-14a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm0 14a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM18 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm0 4v3a5 5 0 0 1-5 5H6',
  sparkles: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Zm7 0v4m-2-2h4',
  refresh: 'M20 9a8 8 0 0 0-14-4L3 8m0-5v5h5m-4 7a8 8 0 0 0 14 4l3-3m0 5v-5h-5',
  check: 'm4 12 5 5L20 6', warning: 'm12 3 10 18H2L12 3Zm0 6v5m0 3v.1',
  layers: 'm12 3 10 6-10 6L2 9l10-6Zm-9 11 9 5 9-5M3 19l9 5 9-5',
  history: 'M3 11a9 9 0 1 1 3 8M3 5v6h6m3-4v6l4 2',
  undo: 'M3 10h10a7 7 0 0 1 7 7M3 10l5-5M3 10l5 5',
  redo: 'M21 10H11a7 7 0 0 0-7 7m17-7-5-5m5 5-5 5',
  filter: 'M3 5h18M6 12h12m-8 7h4', download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
  upload: 'M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5',
  archive: 'M3 3h18v5H3V3Zm2 5v13h14V8m-10 5h6',
  globe: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 0c5 5 5 13 0 18-5-5-5-13 0-18ZM3 12h18',
  inbox: 'M5 4h14l3 10v6H2v-6L5 4ZM2 14h6l2 3h4l2-3h6',
  menu: 'M4 6h16M4 12h16M4 18h16',
  'panel-left': 'M3 4h18v16H3V4Zm6 0v16',
  bug: 'M8 8h8v9a4 4 0 0 1-8 0V8Zm0 3H4m16 0h-4M8 16H3m18 0h-5M9 4l2 4m4-4-2 4M7 20l-3 2m13-2 3 2',
  server: 'M3 3h18v7H3V3Zm0 11h18v7H3v-7Zm4-8h.1M7 17h.1m4-11h6m-6 11h6',
  key: 'M8 3a5 5 0 1 0 0 10A5 5 0 0 0 8 3Zm4 9 9 9m-5-5 3-3m-1 5 3-3',
  'arrow-down': 'M12 3v18m-6-6 6 6 6-6', 'arrow-up': 'M12 21V3m-6 6 6-6 6 6',
}
Object.assign(icons, { chart: icons.usage, alert: icons.warning, clock: icons.automations, shield: icons.audit, book: icons.knowledge })
</script>

<template><svg class="ui-icon" :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path :d="icons[name] || icons.code" /></svg></template>

<style scoped>.ui-icon { display: inline-block; flex-shrink: 0; vertical-align: middle; }</style>
