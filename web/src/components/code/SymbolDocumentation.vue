<script setup>
import { computed } from 'vue'
import { renderMarkdown } from '../../lib/markdown.js'
import { hoverCodeBlock } from '../../lib/hoverDocumentation.js'

const props = defineProps({
  document: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['close'])
const title = computed(() => props.document?.title || '完整说明')
const location = computed(() => {
  const path = typeof props.document?.path === 'string' ? props.document.path : ''
  const line = Number(props.document?.line)
  return path ? `${path}${Number.isInteger(line) && line > 0 ? `:${line}` : ''}` : ''
})
const sections = computed(() => {
  const items = Array.isArray(props.document?.sections) ? props.document.sections : []
  const safe = items.filter(item => item && ['code', 'markdown', 'plaintext'].includes(item.kind)
    && typeof item.value === 'string' && item.value.length)
  return safe.map((item, index) => ({
    ...item,
    heading: index === 0 || (item.kind === 'code') !== (safe[index - 1].kind === 'code')
      ? item.kind === 'code' ? '签名' : '说明' : '',
  }))
})
function sectionHtml(section) {
  return renderMarkdown(section.kind === 'code'
    ? hoverCodeBlock(section.value, typeof section.language === 'string' ? section.language : 'plaintext')
    : section.value)
}
</script>

<template>
  <aside class="symbol-documentation" aria-label="代码完整说明" tabindex="-1" @keydown.esc.stop="emit('close')">
    <header class="documentation-header">
      <span class="documentation-title">{{ title }}</span>
      <button class="documentation-close" type="button" aria-label="关闭完整说明" title="关闭" @click="emit('close')">×</button>
    </header>
    <div class="documentation-context">
      <span class="documentation-source">{{ document?.source || '语言服务' }}</span>
      <span v-if="location" class="documentation-location" :title="location">查询位置 <span class="mono">{{ location }}</span></span>
    </div>
    <div class="documentation-body" :aria-busy="!!document?.loading">
      <div v-if="document?.loading" class="documentation-state" role="status">读取说明…</div>
      <div v-else-if="document?.error" class="documentation-state documentation-error" role="alert">{{ document.error }}</div>
      <template v-else-if="sections.length">
        <section v-for="(section, index) in sections" :key="index" class="documentation-section" :data-section-kind="section.kind">
          <h2 v-if="section.heading" class="documentation-section-title">{{ section.heading }}</h2>
          <div v-if="section.kind === 'plaintext'" class="documentation-plaintext">{{ section.value }}</div>
          <div v-else class="documentation-markdown" v-html="sectionHtml(section)" />
        </section>
      </template>
      <div v-else class="documentation-state">未返回说明</div>
    </div>
  </aside>
</template>

<style scoped>
.symbol-documentation {
  position: absolute; top: 8px; right: 8px; bottom: 8px; z-index: 30;
  width: min(540px, calc(100% - 16px)); min-width: 0; display: flex; flex-direction: column;
  overflow: hidden; background: var(--panel, #fff); color: var(--text, #202735);
  border: 1px solid var(--border, #e3e7ef); border-radius: 10px; box-shadow: var(--shadow-pop);
  font-size: 13px; line-height: 1.65;
}
.documentation-header { display: flex; align-items: flex-start; gap: 12px; padding: 13px 14px 8px; flex: none; }
.documentation-title { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 14px; line-height: 1.5; font-weight: 600; }
.documentation-close { display: grid; place-items: center; flex: none; min-height: 24px; width: 24px; padding: 0; border: 0; border-radius: 5px; background: transparent; color: var(--muted); font-size: 20px; line-height: 1; }
.documentation-close:hover { background: var(--hover); color: var(--text); }
.documentation-context { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 10px; padding: 0 14px 11px; border-bottom: 1px solid var(--border); color: var(--muted); font-size: 11px; flex: none; }
.documentation-source { flex: none; border-radius: 4px; padding: 1px 5px; background: var(--panel-soft); }
.documentation-location { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.documentation-location .mono { margin-left: 5px; }
.documentation-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 14px; scrollbar-width: thin; }
.documentation-section + .documentation-section { margin-top: 12px; }
.documentation-section-title { margin: 0 0 7px; color: var(--muted); font-size: 11px; line-height: 1.4; font-weight: 600; }
.documentation-plaintext { white-space: pre-wrap; overflow-wrap: anywhere; }
.documentation-state { padding: 8px 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.documentation-error { color: var(--err); }
.documentation-markdown { overflow-wrap: anywhere; min-width: 0; }
.documentation-markdown :deep(> :first-child) { margin-top: 0; }
.documentation-markdown :deep(> :last-child) { margin-bottom: 0; }
.documentation-markdown :deep(p) { margin: 0 0 9px; }
.documentation-markdown :deep(h1), .documentation-markdown :deep(h2), .documentation-markdown :deep(h3),
.documentation-markdown :deep(h4), .documentation-markdown :deep(h5), .documentation-markdown :deep(h6) { margin: 15px 0 7px; font-size: 14px; line-height: 1.5; font-weight: 600; }
.documentation-markdown :deep(strong) { font-weight: 600; }
.documentation-markdown :deep(ul), .documentation-markdown :deep(ol) { margin: 8px 0; padding-left: 21px; }
.documentation-markdown :deep(li + li) { margin-top: 4px; }
.documentation-markdown :deep(code) { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; padding: 1px 4px; border-radius: 4px; background: var(--panel-soft); }
.documentation-markdown :deep(pre) { margin: 8px 0; padding: 10px 11px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-soft); overflow-x: auto; max-width: 100%; line-height: 1.6; }
.documentation-markdown :deep(pre code) { padding: 0; background: transparent; white-space: pre; }
.documentation-markdown :deep(blockquote) { margin: 10px 0; padding: 0 10px; border-left: 2px solid var(--border-strong); color: var(--text-2); }
.documentation-markdown :deep(a) { color: var(--accent); }
.documentation-markdown :deep(table) { display: block; overflow-x: auto; border-collapse: collapse; max-width: 100%; font-size: 12px; margin: 10px 0; }
.documentation-markdown :deep(th), .documentation-markdown :deep(td) { border: 1px solid var(--border); padding: 5px 7px; text-align: left; }
.documentation-markdown :deep(hr) { margin: 12px 0; height: 1px; border: 0; background: var(--border); }
@media (max-width: 600px) { .documentation-header { padding: 11px 12px 7px; }.documentation-context { padding: 0 12px 10px; }.documentation-body { padding: 12px; } }
</style>
