<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { renderMarkdown } from '../../lib/markdown.js'
import { hoverCodeBlock } from '../../lib/hoverDocumentation.js'
import { buildFullSymbolDocumentation } from '../../lib/fullSymbolDocumentation.js'

const props = defineProps({
  document: { type: Object, default: () => ({}) },
  pinned: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'toggle-pin', 'retry-translation'])
const ready = computed(() => !props.document?.loading && !props.document?.error)
const showOriginal = ref(false), bodyRef = ref(null)
const details = computed(() => buildFullSymbolDocumentation(ready.value ? props.document : null, { original: showOriginal.value }))
const title = computed(() => ready.value ? details.value.title || props.document?.title || '完整说明' : props.document?.title || '完整说明')
const hasOriginal = computed(() => !!details.value.originalAvailable)
const sections = computed(() => details.value.sections)
const hasContent = computed(() => sections.value.length || details.value.purpose || details.value.businessMeaning || details.value.metadata?.length)
const copyState = ref('')
watch(() => props.document, () => { copyState.value = '' })
watch(() => JSON.stringify([props.document?.path, props.document?.line, props.document?.title]), () => { showOriginal.value = false })
watch(hasOriginal, value => { if (!value) showOriginal.value = false })
watch(showOriginal, () => { copyState.value = ''; void nextTick(() => { if (bodyRef.value) bodyRef.value.scrollTop = 0 }) })
const location = computed(() => {
  const path = typeof props.document?.path === 'string' ? props.document.path : ''
  const line = Number(props.document?.line)
  return path ? `${path}${Number.isInteger(line) && line > 0 ? `:${line}` : ''}` : ''
})
const provenance = computed(() => {
  if (!ready.value || !hasContent.value) return []
  const supplied = details.value.provenance || [], source = props.document?.source || '语言服务'
  const rows = [...supplied,
    ...(details.value.owner && !supplied.some(row => row.value === details.value.owner) ? [{ label: '所属类型', value: details.value.owner }] : []),
    ...(!supplied.some(row => row.value === source) ? [{ label: '来源', value: source }] : []),
    ...(location.value ? [{ label: '查询位置', value: location.value }] : [])]
  const seen = new Set()
  return rows.filter(row => {
    if (!row?.value) return false
    const key = `${row.label}\0${row.value}`
    if (seen.has(key)) return false
    seen.add(key); return true
  })
})
async function copyDeclaration() {
  const target = props.document, declaration = details.value.declaration
  if (!declaration) return
  try {
    if (!globalThis.navigator?.clipboard?.writeText) throw new Error('clipboard unavailable')
    await globalThis.navigator.clipboard.writeText(declaration)
    if (props.document === target) copyState.value = '签名已复制'
  } catch {
    if (props.document === target) copyState.value = '无法访问剪贴板，请选择签名后复制'
  }
}
function sectionHtml(section) {
  return renderMarkdown(section.kind === 'code'
    ? hoverCodeBlock(section.value, typeof section.language === 'string' ? section.language : 'plaintext')
    : section.value)
}
</script>

<template>
  <aside class="symbol-documentation" :class="{ 'is-pinned': pinned }" aria-label="代码完整说明" tabindex="-1" @keydown.esc.stop="emit('close')">
    <header class="documentation-header">
      <div class="documentation-identity">
        <span class="documentation-title">{{ title }}</span>
        <span v-if="details.category" class="documentation-category">· {{ details.category }}</span>
        <span v-if="pinned" class="documentation-pinned">已固定</span>
      </div>
      <button class="documentation-close" type="button" aria-label="关闭完整说明" title="关闭" @click="emit('close')">×</button>
    </header>
    <div class="documentation-actions" role="toolbar" aria-label="文档操作">
      <button type="button" :disabled="!ready || !hasContent" :aria-pressed="pinned"
        :title="pinned ? '取消固定' : '固定此说明'" @click="emit('toggle-pin')">{{ pinned ? '取消固定' : '固定说明' }}</button>
      <button type="button" :disabled="!details.declaration" title="复制语言服务返回的声明签名" @click="copyDeclaration">复制签名</button>
      <div v-if="hasOriginal && ready" class="documentation-languages" role="group" aria-label="文档语言">
        <button type="button" :aria-pressed="!showOriginal" @click="showOriginal = false">中文说明</button>
        <button type="button" :aria-pressed="showOriginal" @click="showOriginal = true">原文</button>
      </div>
    </div>
    <div ref="bodyRef" class="documentation-body" :aria-busy="!!document?.loading">
      <div v-if="document?.loading" class="documentation-state" role="status">读取说明…</div>
      <div v-else-if="document?.error" class="documentation-state documentation-error" role="alert">{{ document.error }}</div>
      <template v-else>
        <div v-if="details.translationNotice" class="documentation-notice" :class="{ 'is-failed': details.translationState?.status === 'failed' }" role="status" aria-live="polite" :aria-busy="details.translationState?.status === 'pending'">
          <div class="documentation-translation-message"><strong>{{ details.translationState?.label }}</strong><span>{{ details.translationNotice }}</span></div>
          <button v-if="details.translationState?.canRetry && !document?.detached" type="button" class="documentation-translation-retry" @click="emit('retry-translation')">{{ details.translationState.action }}</button>
        </div>
        <template v-if="hasContent">
        <section v-if="!showOriginal && (details.purpose || details.businessMeaning || details.metadata?.length)" class="documentation-overview">
          <p v-if="details.purpose" class="documentation-purpose"><strong>作用</strong><span>{{ details.purpose }}</span></p>
          <p v-if="details.businessMeaning" class="documentation-purpose"><strong>功能<span v-if="details.businessCandidate">（待确认）</span></strong><span>{{ details.businessMeaning }}</span></p>
          <dl v-if="details.metadata?.length" class="documentation-metadata">
            <div v-for="(item, index) in details.metadata" :key="index"><dt>{{ item.label }}</dt><dd><code v-if="item.code">{{ item.value }}</code><span v-else>{{ item.value }}</span></dd></div>
          </dl>
        </section>
        <section v-for="(section, index) in sections" :key="index" class="documentation-section" :class="{ 'documentation-declaration': section.declaration }" :data-section-kind="section.kind">
          <h2 v-if="section.heading" class="documentation-section-title">{{ section.heading }}</h2>
          <div v-if="section.kind === 'plaintext'" class="documentation-plaintext">{{ section.value }}</div>
          <div v-else class="documentation-markdown" v-html="sectionHtml(section)" />
        </section>
        <details v-if="provenance.length || details.links?.length" class="documentation-provenance">
          <summary>来源与参考</summary>
          <dl class="documentation-metadata"><div v-for="(item, index) in provenance" :key="index"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div></dl>
          <ul v-if="details.links?.length" class="documentation-links"><li v-for="link in details.links" :key="link.href"><a :href="link.href" target="_blank" rel="noopener noreferrer">{{ link.label || '参考文档' }} ↗</a></li></ul>
        </details>
        </template>
        <div v-else-if="!details.translationNotice" class="documentation-state">未返回说明</div>
      </template>
    </div>
    <footer class="documentation-footer">
      <span v-if="copyState" role="status">{{ copyState }}</span>
      <span class="documentation-shortcut"><kbd>Esc</kbd> 关闭</span>
    </footer>
  </aside>
</template>

<style scoped>
.symbol-documentation {
  position: absolute; top: 8px; right: 8px; bottom: 8px; z-index: 30;
  width: min(580px, calc(100% - 16px)); min-width: 0; display: flex; flex-direction: column;
  overflow: hidden; background: var(--panel, #fff); color: var(--text, #202735);
  border: 1px solid var(--border, #e3e7ef); border-radius: 10px; box-shadow: var(--shadow-pop);
  font-size: 13px; line-height: 1.6;
}
.documentation-header { display: flex; align-items: flex-start; gap: 12px; padding: 14px 16px 12px; flex: none; }
.documentation-identity { display: flex; flex: 1; flex-wrap: wrap; align-items: baseline; gap: 3px 6px; min-width: 0; }
.documentation-title { min-width: 0; overflow-wrap: anywhere; font: 650 14px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
.documentation-category { color: var(--text-2, var(--text)); font-size: 13px; font-weight: 600; }
.documentation-pinned { padding: 1px 5px; border-radius: 4px; color: var(--accent); background: var(--accent-soft); font-size: 11px; font-weight: 500; }
.documentation-close { display: grid; place-items: center; flex: none; min-height: 24px; width: 24px; padding: 0; border: 0; border-radius: 5px; background: transparent; color: var(--muted); font-size: 20px; line-height: 1; }
.documentation-close:hover { background: var(--hover); color: var(--text); }
.documentation-actions { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; padding: 6px 10px; border-block: 1px solid var(--border); background: var(--panel-soft); }
.documentation-actions button, .documentation-actions a { display: inline-flex; align-items: center; gap: 4px; padding: 4px 7px; min-height: 28px; border: 1px solid transparent; border-radius: 5px; color: var(--text-2, var(--text)); background: transparent; font: inherit; font-size: 12px; text-decoration: none; cursor: pointer; }
.documentation-actions button:hover:not(:disabled), .documentation-actions a:hover { background: var(--panel); border-color: var(--border); color: var(--accent); }
.documentation-actions button[aria-pressed=true] { color: var(--accent); background: var(--panel); border-color: var(--border); }
.documentation-actions button:disabled { opacity: .5; cursor: not-allowed; }
.documentation-languages { display: flex; gap: 2px; margin-left: auto; padding-left: 5px; border-left: 1px solid var(--border); }
.documentation-actions button:focus-visible, .documentation-actions a:focus-visible, .documentation-close:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.documentation-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 16px; scrollbar-width: thin; }
.documentation-overview { margin-bottom: 16px; }
.documentation-purpose { display: flex; align-items: baseline; gap: 10px; margin: 0 0 9px; line-height: 1.6; }
.documentation-purpose > strong { flex: none; font-size: 12px; font-weight: 600; }
.documentation-purpose > span { min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.documentation-metadata { display: grid; gap: 6px; margin: 12px 0 0; font-size: 12px; line-height: 1.5; }
.documentation-metadata > div { display: grid; grid-template-columns: 66px minmax(0, 1fr); gap: 9px; align-items: baseline; }
.documentation-metadata dt { color: var(--muted); }
.documentation-metadata dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }
.documentation-metadata code { padding: 1px 4px; border-radius: 4px; background: var(--panel-soft); font: 12px/1.6 ui-monospace, SFMono-Regular, Consolas, monospace; }
.documentation-notice { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; margin: 0 0 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-soft); color: var(--muted); font-size: 12px; line-height: 1.6; }
.documentation-translation-message { display: grid; gap: 2px; min-width: 0; overflow-wrap: anywhere; }
.documentation-translation-message strong { font-weight: 600; color: var(--text); }
.documentation-notice.is-failed .documentation-translation-message strong { color: var(--err); }
.documentation-translation-retry { flex: none; border: 1px solid var(--border); border-radius: 5px; padding: 5px 9px; background: var(--panel); color: var(--accent); cursor: pointer; font: inherit; }
.documentation-translation-retry:hover { border-color: var(--accent); }
.documentation-translation-retry:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
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
.documentation-declaration .documentation-markdown :deep(pre code) { white-space: pre-wrap; overflow-wrap: anywhere; }
.documentation-markdown :deep(blockquote) { margin: 10px 0; padding: 0 10px; border-left: 2px solid var(--border-strong); color: var(--text-2); }
.documentation-markdown :deep(a) { color: var(--accent); }
.documentation-markdown :deep(table) { display: block; overflow-x: auto; border-collapse: collapse; max-width: 100%; font-size: 12px; margin: 10px 0; }
.documentation-markdown :deep(th), .documentation-markdown :deep(td) { border: 1px solid var(--border); padding: 5px 7px; text-align: left; }
.documentation-markdown :deep(hr) { margin: 12px 0; height: 1px; border: 0; background: var(--border); }
.documentation-provenance { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border); font-size: 12px; }
.documentation-provenance summary { width: fit-content; color: var(--muted); cursor: pointer; }
.documentation-provenance summary:hover { color: var(--text); }
.documentation-provenance summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 2px; }
.documentation-links { display: grid; gap: 5px; padding-left: 16px; margin: 10px 0 0; }
.documentation-links a { color: var(--accent); overflow-wrap: anywhere; }
.documentation-footer { flex: none; display: flex; align-items: baseline; justify-content: space-between; gap: 10px; padding: 8px 14px; border-top: 1px solid var(--border); font-size: 11px; line-height: 1.6; color: var(--muted); }
.documentation-footer > span { min-width: 0; overflow-wrap: anywhere; }
.documentation-shortcut { flex: none; white-space: nowrap; margin-left: auto; }
.documentation-shortcut kbd { padding: 1px 3px; border: 1px solid var(--border); border-radius: 3px; font: inherit; }
@media (max-width: 600px) { .documentation-header { padding: 11px 12px 10px; }.documentation-body { padding: 12px; }.documentation-footer { padding-inline: 12px; }.documentation-metadata > div { grid-template-columns: 58px minmax(0, 1fr); } }
</style>
