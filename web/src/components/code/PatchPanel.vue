<script setup>
import UiIcon from '../ui/UiIcon.vue'
// 改动台账面板（M18 · P2）：AI/编辑器改了什么，一条条列出来，可看 diff、可回滚。
//
// 这是"AI 改代码可审查、可撤销"的界面落点。两个刻意的设计：
// 1. 回滚按钮对"之后又被改过"的记录直接显示平台拒绝原因（409），
//    而不是先让用户点、再弹一个莫名其妙的错误；
// 2. diff 用 Monaco 的 diff 编辑器（与主编辑器同一套渲染），
//    避免"两处 diff 长得不一样"让人怀疑到底改了没有。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../api.js'
import { diffLanguageForPath, reviewDiffOptions } from '../../lib/diffPresentation.js'
import { usePreferences } from '../../lib/preferences.js'

const props = defineProps({
  projectId: { type: String, required: true },
  refreshKey: { type: Number, default: 0 },   // 由父组件在"扫描/保存/AI 改动"后自增
})
const emit = defineEmits(['open-file', 'changed', 'error'])
const { preferences } = usePreferences()

const patches = ref([])
const counts = ref({ total: 0, active: 0 })
const loading = ref(false)
const selected = ref(null)      // { ...meta, diff }
const detailLoading = ref(false)
const detailError = ref('')
const sideBySide = ref(false)
const wrap = ref(true)
const busyId = ref('')
const note = ref('')

const diffHost = ref(null)
let diffEditor = null
let diffModels = []
let themeSubscription = null
let disposed = false
let projectEpoch = 0
let listRequest = 0
let detailRequest = 0
let renderRequest = 0

const activePatches = computed(() => patches.value)
const diffOptions = computed(() => reviewDiffOptions({ sideBySide: sideBySide.value, wrap: wrap.value, fontSize: preferences.editorFontSize }))

async function load() {
  const projectId = props.projectId
  const epoch = projectEpoch
  const request = ++listRequest
  loading.value = true
  try {
    const body = await api.code.patches(projectId, { limit: 60 })
    if (disposed || epoch !== projectEpoch || request !== listRequest) return
    patches.value = body.patches || []
    counts.value = body.counts || { total: 0, active: 0 }
  } catch (e) {
    if (!disposed && epoch === projectEpoch && request === listRequest) emit('error', e.message)
  } finally {
    if (!disposed && epoch === projectEpoch && request === listRequest) loading.value = false
  }
}

async function openDetail(patch) {
  const projectId = props.projectId
  const epoch = projectEpoch
  const request = ++detailRequest
  ++renderRequest
  disposeModels()
  selected.value = { ...patch }
  detailLoading.value = true
  detailError.value = ''
  try {
    const detail = await api.code.patchDetail(projectId, patch.id)
    if (disposed || epoch !== projectEpoch || request !== detailRequest) return
    selected.value = detail
    await nextTick()
    if (disposed || epoch !== projectEpoch || request !== detailRequest) return
    await renderDiff(detail, request)
  } catch (e) {
    if (!disposed && epoch === projectEpoch && request === detailRequest) {
      detailError.value = e.message
      emit('error', e.message)
    }
  } finally {
    if (!disposed && epoch === projectEpoch && request === detailRequest) detailLoading.value = false
  }
}

function disposeModels() {
  diffEditor?.setModel(null)
  for (const model of diffModels) model.dispose()
  diffModels = []
}

function disposeDiff() {
  themeSubscription?.dispose()
  themeSubscription = null
  disposeModels()
  diffEditor?.dispose()
  diffEditor = null
}

function closeDetail() {
  ++detailRequest
  ++renderRequest
  selected.value = null
  detailLoading.value = false
  detailError.value = ''
  disposeDiff()
}

async function renderDiff(detail, detailToken) {
  const host = diffHost.value
  const epoch = projectEpoch
  const request = ++renderRequest
  if (!host || !selected.value) return
  // Monaco 按需加载：只看台账列表不该付 4MB 编辑器的代价
  const mod = await import('../../lib/monacoSetup.js')
  if (disposed || epoch !== projectEpoch || request !== renderRequest || detailToken !== detailRequest || diffHost.value !== host) return
  const monaco = mod.ensureMonaco()

  // 服务端直接给改前/改后全文 → 用原生 diff 渲染。
  // 若文件过大没存全文，则退化为"只显示改动片段"的只读对照（不假装是完整 diff）。
  const hasInline = !!detail.inline
  const leftText = hasInline ? detail.before_text : detail.old_string || ''
  const rightText = hasInline ? detail.after_text : detail.new_string || ''
  const language = diffLanguageForPath(detail.path)
  const original = monaco.editor.createModel(leftText || '', language)
  diffModels.push(original)
  try {
    const modified = monaco.editor.createModel(rightText || '', language)
    diffModels.push(modified)
    if (!diffEditor) {
      diffEditor = monaco.editor.createDiffEditor(host, { ...diffOptions.value, theme: mod.currentEditorTheme?.() || 'code-agent-light' })
      themeSubscription = mod.followEditorTheme?.(monaco) || null
    }
    diffEditor.setModel({ original, modified })
  } catch (e) {
    disposeDiff()
    throw e
  }
}

async function revert(patch) {
  const projectId = props.projectId
  const epoch = projectEpoch
  const detailToken = detailRequest
  busyId.value = patch.id
  note.value = ''
  try {
    const result = await api.code.revertPatch(projectId, patch.id)
    emit('changed', { ...result, projectId })
    if (disposed || epoch !== projectEpoch) return
    note.value = `已回滚 ${result.path}（地图已同步到 v${result.version}）`
    await load()
    if (!disposed && epoch === projectEpoch && detailToken === detailRequest) closeDetail()
  } catch (e) {
    // 平台会明确拒绝"之后又被改过"的回滚：原样展示原因，不做二次包装
    if (!disposed && epoch === projectEpoch) note.value = e.message
  } finally {
    if (!disposed && epoch === projectEpoch) busyId.value = ''
  }
}

function fmtTime(ts) {
  if (!ts) return ''
  const date = new Date(ts)
  return isNaN(date.getTime()) ? ts : date.toLocaleString('zh-CN', { hour12: false })
}

function deltaLabel(patch) {
  const before = patch.before_len || 0
  const after = patch.after_len || 0
  if (!before && !after) return '（未存全文）'
  const diff = after - before
  return `${diff > 0 ? '+' : ''}${diff} 字节`
}

watch(() => props.projectId, () => {
  ++projectEpoch
  closeDetail()
  patches.value = []
  counts.value = { total: 0, active: 0 }
  note.value = ''
  busyId.value = ''
  load()
}, { flush: 'sync' })
watch(() => props.refreshKey, load)
watch(diffOptions, options => diffEditor?.updateOptions(options))
load()

onBeforeUnmount(() => {
  disposed = true
  ++listRequest
  ++detailRequest
  ++renderRequest
  disposeDiff()
})
</script>

<template>
  <div class="patches" :class="{ 'has-detail': !!selected }">
    <div class="head">
      <UiIcon name="history" :size="15" class="panel-icon" /><span class="title">改动台账</span>
      <span class="muted">{{ counts.total }} 条 · 可回滚 {{ counts.active }}</span>
      <span class="spacer" />
      <button class="ghost" title="刷新" aria-label="刷新面板" @click="load"><UiIcon name="refresh" :size="14" /></button>
    </div>

    <div v-if="note" class="note">{{ note }}</div>
    <div v-if="loading && !patches.length" class="muted pad">读取中…</div>
    <div v-else-if="!patches.length" class="muted pad">
      AI 与编辑器的每一次写入都会出现在这里（含改前内容与 sha1，可一键回滚）。
    </div>

    <ul v-else class="list">
      <li
        v-for="p in activePatches"
        :key="p.id"
        :class="{ selected: selected?.id === p.id, reverted: !!p.reverted_at }"
        @click="openDetail(p)"
      >
        <div class="row1">
          <span class="tool" :class="p.tool">{{ p.tool === 'edit_file' ? 'AI' : p.tool === 'editor_save' ? '编辑器' : p.tool === 'frontend_design_save' ? '前端设计' : p.tool }}</span>
          <span class="path mono" :title="p.path">{{ p.path }}</span>
          <span class="muted delta">{{ deltaLabel(p) }}</span>
        </div>
        <div class="row2">
          <span class="muted">{{ fmtTime(p.ts) }}</span>
          <span v-if="p.reverted_at" class="muted">已回滚</span>
          <span class="spacer" />
          <button
            v-if="!p.reverted_at"
            class="ghost small"
            :disabled="busyId === p.id"
            @click.stop="revert(p)"
          >{{ busyId === p.id ? '回滚中…' : '回滚' }}</button>
        </div>
      </li>
    </ul>

    <div v-if="selected" class="detail">
      <div class="detail-head">
        <span class="mono detail-path" :title="selected.path">{{ selected.path }}</span>
        <button class="ghost small" @click="emit('open-file', { path: selected.path, line: 1 })">打开文件</button>
        <button class="ghost small" title="关闭差异详情" aria-label="关闭差异详情" @click="closeDetail">×</button>
      </div>
      <div class="diff-options">
        <div class="diff-modes" role="group" aria-label="差异显示方式">
          <button class="ghost small" :class="{ active: !sideBySide }" :aria-pressed="!sideBySide" @click="sideBySide = false">行内对照</button>
          <button class="ghost small" :class="{ active: sideBySide }" :aria-pressed="sideBySide" @click="sideBySide = true">并排对照</button>
        </div>
        <label class="wrap-option"><input v-model="wrap" type="checkbox">自动换行</label>
      </div>
      <div class="diff-legend" :class="{ 'side-by-side': sideBySide }">
        <span class="before">− 修改前</span><span class="after">+ 修改后</span>
        <span v-if="!detailLoading && !selected.inline" class="fragment muted">仅显示改动片段</span>
      </div>
      <div v-if="detailLoading" class="detail-status muted" role="status">读取差异中…</div>
      <div v-else-if="detailError" class="detail-status" role="alert">{{ detailError }}</div>
      <div ref="diffHost" class="diff" />
    </div>
  </div>
</template>

<style scoped>
.patches { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; }
.head {
  display: flex; align-items: center; gap: 8px; min-height: 42px; flex: none; padding: 7px 14px;
  border-bottom: 1px solid var(--border); font-size: var(--fs-sm);
}
.title { font-weight: var(--fw-semibold); }.panel-icon { flex: none; color: var(--muted); }
.patches > .head > .panel-icon { flex: 0 0 auto; }
.spacer { flex: 1; }
.ghost { display: inline-flex; align-items: center; justify-content: center; min-height: 26px; border: none; background: transparent; color: var(--muted); padding: 3px 6px; }
.ghost:hover { background: var(--hover); }
.ghost.small { font-size: var(--fs-xs); }

.note { padding: var(--sp-1-5) var(--sp-2-5); font-size: var(--fs-xs); color: var(--accent); background: var(--accent-soft); }
.pad { padding: 24px 18px; font-size: var(--fs-xs); line-height: 1.8; text-align: center; color: var(--muted); }

.list { list-style: none; margin: 0; padding: 8px; overflow-y: auto; flex: 1; min-height: 0; }
.has-detail .list { flex: 1 1 32%; min-height: 72px; }
.list li { padding: 10px; border: 1px solid transparent; border-radius: 6px; cursor: pointer; margin-bottom: 4px; }
.list li:hover { background: var(--bg); }
.list li.selected { background: var(--accent-soft); border-color: var(--accent-border); }
.list li.reverted .path { text-decoration: line-through; color: var(--muted); }
.row1 { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-sm); }
.row2 { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-xs); margin-top: var(--sp-0-5); }
.tool {
  flex: none; font-size: var(--fs-xs); padding: var(--sp-hair) 5px; border-radius: var(--r-sm);
  background: #eef2ff; color: #4338ca;
}
.tool.edit_file { background: var(--ok-soft); color: var(--ok); }
.path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.delta { flex: none; font-size: var(--fs-xs); }

.detail { border-top: 1px solid var(--border); display: flex; flex-direction: column; min-height: 220px; min-width: 0; flex: 2 1 60%; overflow: hidden; position: relative; }
.detail-head {
  display: flex; align-items: center; gap: var(--sp-1-5); padding: 5px var(--sp-2-5); font-size: var(--fs-xs); flex: none;
  border-bottom: 1px solid var(--border);
}
.detail-path { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.detail-head button { flex: none; }
.diff-options { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px 12px; padding: 5px var(--sp-2-5); flex: none; font-size: var(--fs-xs); }
.diff-modes { display: flex; gap: 2px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--r-sm); padding: 2px; }
.diff-modes .active { background: var(--accent-soft); color: var(--accent); }
.wrap-option { display: inline-flex; gap: 5px; align-items: center; color: var(--muted); white-space: nowrap; cursor: pointer; }
.wrap-option input { margin: 0; accent-color: var(--accent); }
.diff-legend { display: flex; align-items: center; flex-wrap: wrap; gap: 5px 20px; font-size: var(--fs-xs); padding: 4px var(--sp-2-5); border-bottom: 1px solid var(--border); flex: none; }
.diff-legend .before { color: var(--err); }
.diff-legend .after { color: var(--ok); }
.diff-legend.side-by-side .before, .diff-legend.side-by-side .after { flex: 1; }
.fragment { width: 100%; }
.detail-status { position: absolute; inset: auto 12px 18px; z-index: 1; padding: 12px; text-align: center; background: var(--surface, var(--bg)); border: 1px solid var(--border); border-radius: var(--r-sm); font-size: var(--fs-xs); }
.detail-status[role="alert"] { color: var(--err); }
.diff { flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
</style>
