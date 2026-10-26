<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { changeReviews } from '../../lib/changeReviewClient.js'
const props = defineProps({ projectId: { type: String, default: '' }, visible: Boolean, refreshKey: Number, dirtyPaths: { type: Array, default: () => [] }, readonly: Boolean, caseInsensitive: Boolean })
const emit = defineEmits(['open-file', 'changed', 'error'])
const proposals = ref([]), selected = ref(null), busy = ref(false), error = ref('')
const batches = ref([]), selectedBatch = ref(null)
let generation = 0, listRequest = 0, detailRequest = 0, disposed = false, timer = null
const pathKey = value => { const path = String(value || '').replace(/\\/g, '/'); return props.caseInsensitive ? path.toLowerCase() : path }
const hasDraft = path => props.dirtyPaths.some(dirty => pathKey(dirty) === pathKey(path))
const blocked = computed(() => props.readonly || hasDraft(selected.value?.path))
const batchBlocked = computed(() => props.readonly || selectedBatch.value?.paths.some(hasDraft))
const labels = { pending: '待审查', partial: '部分已处理', applied: '已应用', rejected: '已拒绝', reviewed: '审查完成', reverted: '已回滚' }
async function load() {
  if (!props.projectId || !props.visible) return
  const pid = props.projectId, version = generation, request = ++listRequest
  try {
    const [result, batchResult] = await Promise.all([changeReviews.list(pid), changeReviews.listBatches ? changeReviews.listBatches(pid) : { batches: [] }])
    if (disposed || pid !== props.projectId || version !== generation || request !== listRequest) return
    proposals.value = result.proposals || []
    batches.value = batchResult.batches || []
    if (selected.value) {
      const next = proposals.value.find(p => p.id === selected.value.id)
      if (next && (next.state !== selected.value.state || JSON.stringify(next.counts) !== JSON.stringify(selected.value.counts))) await choose(next)
    } else if (!selectedBatch.value && proposals.value.length) await choose(proposals.value[0])
    if (selectedBatch.value) {
      const next = batches.value.find(batch => batch.id === selectedBatch.value.id)
      if (next && next.state !== selectedBatch.value.state) await chooseBatch(next)
    } else if (!selected.value && batches.value.length) await chooseBatch(batches.value[0])
  } catch (e) { if (!disposed && version === generation && request === listRequest) error.value = e.message }
}
async function choose(proposal) {
  const pid = props.projectId, version = generation, request = ++detailRequest
  error.value = ''
  try {
    const result = await changeReviews.detail(pid, proposal.id)
    if (!disposed && version === generation && request === detailRequest) { selected.value = result; selectedBatch.value = null }
  } catch (e) { if (!disposed && version === generation && request === detailRequest) error.value = e.message }
}
async function chooseBatch(batch) {
  const pid = props.projectId, version = generation, request = ++detailRequest
  error.value = ''
  try {
    const result = await changeReviews.detailBatch(pid, batch.id)
    if (!disposed && version === generation && request === detailRequest) { selectedBatch.value = result; selected.value = null }
  } catch (e) { if (!disposed && version === generation && request === detailRequest) error.value = e.message }
}
async function decideBatch(action) {
  const batch = selectedBatch.value
  if (!batch || busy.value || (action !== 'reject' && batchBlocked.value) || batch.state !== (action === 'rollback' ? 'applied' : 'pending')) return
  const pid = props.projectId, version = generation, id = batch.id
  busy.value = true; error.value = ''
  try {
    const result = await changeReviews.decideBatch(pid, id, action)
    if (result.changed) emit('changed', { projectId: pid, paths: result.paths, batch: result.batch, index_refresh: result.index_refresh })
    if (disposed || version !== generation) return
    if (selectedBatch.value?.id === id) selectedBatch.value = result.batch
    if (result.warning) error.value = result.warning
    await load()
  } catch (e) { if (!disposed && version === generation) error.value = e.message }
  finally { if (!disposed && version === generation) busy.value = false }
}
async function decide(action, ids) {
  if (!selected.value || selected.value.state === 'reverted' || busy.value || (action !== 'reject' && blocked.value)) return
  const pid = props.projectId, version = generation, id = selected.value.id
  busy.value = true; error.value = ''
  try {
    const result = await changeReviews.decide(pid, id, action, ids)
    // 提案请求发出后即使切走面板，已完成的写入也要刷新工作区；父级按项目身份过滤。
    if (result.changed) emit('changed', { projectId: pid, path: result.path, proposal: result.proposal })
    if (disposed || version !== generation) return
    if (selected.value?.id === id) selected.value = result.proposal
    if (result.warning) error.value = result.warning
    await load()
  } catch (e) { if (!disposed && version === generation) error.value = e.message }
  finally { if (!disposed && version === generation) busy.value = false }
}
watch(() => props.projectId, () => { generation++; detailRequest++; proposals.value = []; batches.value = []; selected.value = selectedBatch.value = null; error.value = ''; busy.value = false; load() })
watch(() => [props.visible, props.refreshKey], () => { if (timer) clearInterval(timer); if (props.visible) { load(); timer = setInterval(() => { if (!busy.value && document.visibilityState === 'visible') load() }, 3000) } }, { immediate: true })
onBeforeUnmount(() => { disposed = true; generation++; if (timer) clearInterval(timer) })
</script>
<template>
  <section class="changes" aria-label="修改审查">
    <div class="review-head"><b>修改审查</b><span class="muted">应用后才写入文件</span><button :disabled="busy" @click="load">刷新</button></div>
    <p v-if="!proposals.length && !batches.length" class="empty muted">代码修改与项目重构会显示在这里。审查后应用，也可拒绝或回滚。</p>
    <div v-else class="proposal-list"><button v-for="p in proposals" :key="p.id" :class="{ active: p.id === selected?.id }" @click="choose(p)"><strong>{{ p.title }}</strong><span>{{ p.new_file ? '新增 · ' : '' }}{{ p.path }}</span><small>{{ labels[p.state] }} · {{ p.counts.pending }} 块待审</small></button><button v-for="batch in batches" :key="batch.id" :class="{ active: batch.id === selectedBatch?.id }" @click="chooseBatch(batch)"><strong>{{ batch.title }}</strong><span>项目重构 · {{ batch.file_count }} 个文件</span><small>{{ labels[batch.state] }} · 整批审查</small></button></div>
    <div v-if="error" class="review-error" role="alert">{{ error }}</div>
    <template v-if="selected">
      <div class="review-actions"><button @click="emit('open-file', { path: selected.path })" :disabled="selected.new_file && (!selected.counts.applied || selected.state === 'reverted')">打开文件</button><button class="primary" :disabled="busy || blocked || !selected.counts.pending || selected.state === 'reverted'" @click="decide('apply')">应用全部</button><button :disabled="busy || !selected.counts.pending || selected.state === 'reverted'" @click="decide('reject')">拒绝全部</button><button :disabled="busy || blocked || !selected.counts.applied || selected.state === 'reverted'" @click="decide('rollback')">回滚已应用</button></div>
      <p v-if="blocked" class="draft-note">{{ readonly ? '编辑器为只读，开启人工编辑后可应用修改。' : '此文件有未保存草稿，请先保存或关闭草稿再应用。' }}</p>
      <div class="hunks"><article v-for="h in selected.hunks" :key="h.id" class="hunk"><header><span>{{ h.id }} · {{ labels[h.status] || (h.status === 'applied' ? '已应用' : h.status) }}</span><span class="spacer" /><button :disabled="busy || blocked || h.status !== 'pending'" @click="decide('apply', [h.id])">应用</button><button :disabled="busy || h.status !== 'pending'" @click="decide('reject', [h.id])">拒绝</button></header><div class="diff-lines"><div v-for="(line, index) in h.lines" :key="index" :class="line.kind"><span class="line-no">{{ line.old_line || '' }}</span><span class="line-no">{{ line.new_line || '' }}</span><span class="sign">{{ line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : ' ' }}</span><code>{{ line.text.replace(/\r?\n$/, '') }}</code></div></div></article></div>
    </template>
    <template v-if="selectedBatch">
      <div class="review-actions"><button class="primary" :disabled="busy || batchBlocked || selectedBatch.state !== 'pending'" @click="decideBatch('apply')">应用整批</button><button :disabled="busy || selectedBatch.state !== 'pending'" @click="decideBatch('reject')">拒绝整批</button><button :disabled="busy || batchBlocked || selectedBatch.state !== 'applied'" @click="decideBatch('rollback')">回滚整批</button></div>
      <p v-if="batchBlocked" class="draft-note">{{ readonly ? '开启人工编辑后可应用重构。' : '涉及文件有未保存草稿，请先保存或关闭草稿。' }}</p>
      <div class="hunks"><article v-for="item in selectedBatch.files" :key="item.path" class="hunk"><header><b class="batch-kind">{{ item.kind === 'create' ? '新增' : item.kind === 'delete' ? '删除' : '修改' }}</b><code class="batch-path">{{ item.path }}</code></header><div v-if="!item.hunks.length" class="batch-empty">{{ item.kind === 'create' ? '创建空文件' : '移除空文件' }}</div><div v-for="h in item.hunks" :key="h.id" class="diff-lines"><div v-for="(line, index) in h.lines" :key="index" :class="line.kind"><span class="line-no">{{ line.old_line || '' }}</span><span class="line-no">{{ line.new_line || '' }}</span><span class="sign">{{ line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : ' ' }}</span><code>{{ line.text.replace(/\r?\n$/, '') }}</code></div></div></article></div>
    </template>
  </section>
</template>
<style scoped>
.changes { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; overflow: auto; font-size: var(--fs-xs); }
.review-head { display: flex; align-items: center; gap: 8px; min-height: 42px; flex: none; padding: 8px 14px; border-bottom: 1px solid var(--border); font-size: var(--fs-sm); }.review-head button { margin-left: auto; }
.empty { padding: 26px 18px; line-height: 1.8; color: var(--muted); text-align: center; }.proposal-list { display: flex; flex-direction: column; gap: 5px; max-height: 180px; overflow: auto; padding: 10px; flex: none; }
.proposal-list button { display: flex; flex-direction: column; gap: 5px; text-align: left; font-size: var(--fs-xs); overflow-wrap: anywhere; background: var(--panel-soft); border-color: transparent; padding: 10px; }.proposal-list button.active { border-color: var(--accent-border); background: var(--accent-soft); }.proposal-list small { color: var(--muted); font-size: var(--fs-2xs); }
.review-actions { display: flex; flex-wrap: wrap; gap: 6px; padding: 9px 12px; border-block: 1px solid var(--border); background: var(--panel-soft); }.changes button { font-size: var(--fs-xs); min-height: 28px; padding: 4px 8px; }
.review-error, .draft-note { margin: 10px 12px; padding: 10px 12px; border-radius: 5px; background: var(--warn-soft); color: var(--warn); font-size: var(--fs-xs); line-height: 1.7; overflow-wrap: anywhere; }
.hunks { padding: 12px; }.hunk { border: 1px solid var(--border); margin-bottom: 12px; border-radius: 6px; overflow: hidden; }.hunk header { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; padding: 8px; background: var(--panel-soft); font-size: var(--fs-2xs); }.spacer { flex: 1; }
.diff-lines { overflow-x: auto; }.diff-lines > div { display: flex; min-width: max-content; line-height: 22px; font-family: var(--font-mono, monospace); font-size: var(--fs-xs); }.line-no { width: 32px; padding-right: 7px; text-align: right; color: var(--muted); flex: none; }.sign { width: 16px; flex: none; }.diff-lines code { white-space: pre; padding-right: 12px; }.add { background: #edf8f1; color: #23653b; }.remove { background: #fff0f0; color: #9e3030; }
.batch-kind { color: var(--accent); font-size: var(--fs-2xs); }.batch-path { overflow-wrap: anywhere; }.batch-empty { padding: 12px; color: var(--muted); }
</style>
