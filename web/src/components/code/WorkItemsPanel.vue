<script setup>
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import UiIcon from '../ui/UiIcon.vue'
import { api } from '../../api.js'
import { EXECUTION_MODES, filterWorkItems, hasWorkApproval, hasWorkQuestion, useWorkItems, workItemApprovals, workItemQuestions, workItemCanOpen, workItemGroup, workItemLegacy, workItemMode, workItemProgress, workItemSession, workItemState, workItemTree, workItemVerification } from '../../lib/workItems.js'

const TaskDetail = defineAsyncComponent(() => import('./TaskPanel.vue'))
const BatchDetail = defineAsyncComponent(() => import('./ParallelPanel.vue'))
const props = defineProps({ projectId: { type: String, required: true }, visible: { type: Boolean, default: true }, writable: { type: Boolean, default: false }, activeTree: { type: String, default: 'main' }, refreshKey: { type: Number, default: 0 } })
const emit = defineEmits(['error', 'changed', 'open-tree', 'open-agent'])
const work = useWorkItems({ projectId: () => props.projectId, visible: () => props.visible, client: api.code })
const filter = ref('all'), query = ref(''), creating = ref(false), formError = ref(''), detailRef = ref(null)
const defaults = () => ({ requirement: '', title: '', executionMode: 'team', planFirst: true, gitInit: false, manualPlan: '' })
const form = ref(defaults())
const shown = computed(() => filterWorkItems(work.items.value, filter.value, query.value))
const selected = work.selected
const approvalSession = ref('')
const approvals = computed(() => workItemApprovals(selected.value))
const questions = computed(() => workItemQuestions(selected.value))
const mode = computed(() => EXECUTION_MODES.find(value => value.id === form.value.executionMode))
const planPlaceholder = computed(() => JSON.stringify(form.value.executionMode === 'parallel'
  ? { goal: '…', lanes: [{ id: 'L1', files: ['src/a.js'] }] }
  : { goal: '…', subtasks: [{ id: 's1', files: ['src/a.js'] }] }))
const supported = id => work.data.value?.capabilities?.[id === 'team' ? 'team' : id === 'isolated' ? 'tasks' : 'parallel'] !== false

function openAgent(item = selected.value, question = null) {
  if (!workItemCanOpen(item)) return
  const chosen = item === selected.value && approvals.value.find(value => value.session_id === approvalSession.value)
  const sessionId = question?.session_id || chosen?.session_id || workItemSession(item)
  if (sessionId) emit('open-agent', { sessionId, tree: workItemTree(item, sessionId), approval: !question && hasWorkApproval(item), ...(question || hasWorkQuestion(item) ? { question: true } : {}) })
}
function pick(item) { work.select(item.id) }
function approvalTitle(value, index) {
  return value.executor_title || value.title || selected.value?.agents?.find(agent => agent.session_id === value.session_id)?.title || `执行智能体 ${index + 1}`
}
async function create() {
  formError.value = ''
  const requirement = form.value.requirement.trim()
  if (!requirement) { formError.value = '请先描述需求。'; return }
  let plan
  if (form.value.executionMode !== 'team' && form.value.manualPlan.trim()) {
    try { plan = JSON.parse(form.value.manualPlan) } catch { formError.value = '手动计划需要是有效的 JSON。'; return }
    if (!plan || typeof plan !== 'object' || Array.isArray(plan)) { formError.value = '手动计划需要是 JSON 对象。'; return }
  }
  const response = await work.create({ requirement, title: form.value.title.trim(), execution_mode: form.value.executionMode,
    tree: 'main', plan_first: plan ? false : form.value.planFirst, git_init: form.value.executionMode !== 'team' && form.value.gitInit, ...(plan ? { plan } : {}) })
  if (!response) return
  creating.value = false; form.value = defaults()
  emit('changed', response.item)
  if (response.item?.kind === 'agent') openAgent(response.item)
}
function changed(payload) { work.notify(); emit('changed', payload) }
function notify(payload) { work.notify(payload) }
function refresh() { void work.refresh(); detailRef.value?.load?.() }
watch(() => props.refreshKey, () => notify())
watch(() => [selected.value?.id, selected.value?.state, selected.value?.updated_at, selected.value?.active,
  selected.value?.kind === 'batch' ? JSON.stringify(selected.value?.progress || {}) : '',
  workItemApprovals(selected.value).map(value => value.approval_id).join(','),
  workItemQuestions(selected.value).map(value => value.id).join(',')], () => {
  if (props.visible && selected.value?.kind !== 'agent') detailRef.value?.load?.()
}, { flush: 'post' })
watch(() => props.visible, visible => { if (visible) detailRef.value?.load?.() })
watch(() => props.projectId, () => { creating.value = false; form.value = defaults(); formError.value = ''; query.value = ''; filter.value = 'all' })
watch(() => selected.value?.id, () => { approvalSession.value = '' })
defineExpose({ load: refresh, notify })
</script>

<template>
  <section class="work-items" aria-label="项目任务">
    <header class="head">
      <UiIcon name="layers" :size="16" /><strong>任务</strong><span class="count">{{ work.counts.value.total }}</span>
      <span class="spacer" />
      <button class="ghost small" :disabled="work.creating.value" @click="creating = !creating; work.selectedId.value = ''">{{ creating ? '收起' : '＋ 新建' }}</button>
      <button class="ghost icon" aria-label="刷新任务" @click="refresh"><UiIcon name="refresh" :size="15" /></button>
    </header>
    <div v-if="work.error.value" class="error" role="alert">{{ work.error.value }} <button class="ghost small" @click="refresh">重试</button></div>
    <form v-if="creating" class="create-form" @submit.prevent="create">
      <label>需求<textarea v-model="form.requirement" :disabled="work.creating.value" rows="3" placeholder="描述要实现的功能或要解决的问题" /></label>
      <label>标题（可选）<input v-model="form.title" :disabled="work.creating.value" placeholder="便于后续查找" /></label>
      <fieldset :disabled="work.creating.value">
        <legend>执行方式</legend>
        <label v-for="option in EXECUTION_MODES" :key="option.id" class="mode" :class="{ chosen: form.executionMode === option.id, disabled: !supported(option.id) }">
          <input v-model="form.executionMode" type="radio" name="execution-mode" :value="option.id" :disabled="!supported(option.id)" />
          <span><strong>{{ option.label }}</strong><small>{{ option.description }}{{ !supported(option.id) ? '当前服务未启用。' : '' }}</small></span>
        </label>
      </fieldset>
      <template v-if="form.executionMode !== 'team'">
        <label class="check"><input v-model="form.planFirst" type="checkbox" :disabled="work.creating.value" />先生成计划，批准后执行</label>
        <details class="advanced"><summary>高级选项</summary>
          <label class="check"><input v-model="form.gitInit" type="checkbox" :disabled="work.creating.value" />非 Git 项目：初始化仓库并提交当前代码</label>
          <label>手动计划 JSON（可选）<textarea v-model="form.manualPlan" :disabled="work.creating.value" rows="4" :placeholder="planPlaceholder" /></label>
        </details>
      </template>
      <p v-if="formError" class="error" role="alert">{{ formError }}</p>
      <button class="primary" :disabled="work.creating.value || !supported(form.executionMode)" type="submit">{{ work.creating.value ? '创建中…' : `创建任务 · ${mode.label}` }}</button>
    </form>
    <template v-else-if="selected">
      <div class="detail-heading"><button class="ghost small" @click="work.selectedId.value = ''">← 所有任务</button><span class="kind">{{ workItemMode(selected) }}</span></div>
      <div class="selected-title"><strong>{{ selected.title }}</strong><span class="state" :class="workItemGroup(selected)">{{ workItemState(selected) }}</span></div>
      <label v-if="approvals.length > 1" class="approval-executor">审批执行者<select v-model="approvalSession" aria-label="选择审批执行者"><option value="">{{ approvalTitle(approvals[0], 0) }}（默认）</option><option v-for="(value,index) in approvals" :key="value.approval_id" :value="value.session_id">{{ approvalTitle(value,index) }}</option></select></label>
      <div v-if="questions.length" class="work-questions" aria-label="任务待回答问题"><strong>{{ questions.length }} 组问题等待你回答</strong><button v-for="question in questions" :key="`${question.session_id}:${question.id}`" class="ghost small" :disabled="!workItemCanOpen(selected)" :data-question-session="question.session_id" :data-question-id="question.id" @click="openAgent(selected, question)"><span>{{ question.executor_title }}</span><span class="question-preview">{{ question.questions?.[0]?.question || '需要你的回答' }}</span><span>回答问题 →</span></button></div>
      <div v-if="selected.kind !== 'agent' && workItemSession(selected)" class="detail-session"><button class="ghost small" :disabled="!workItemCanOpen(selected)" :title="workItemLegacy(selected) ? '历史任务会话，下一轮执行后接入团队' : ''" @click="openAgent()">{{ hasWorkApproval(selected) ? '查看并处理权限申请' : hasWorkQuestion(selected) ? '打开会话回答问题' : '打开执行会话' }}</button></div>
      <p v-if="!workItemCanOpen(selected) && selected.workspace_available === false" class="history-note">工作树已清理，可以查看历史结果。</p>
      <TaskDetail v-if="selected.kind === 'task'" :key="selected.id" ref="detailRef" :project-id="projectId" :selected-id="selected.task_id || selected.task?.id" detail-only :writable="writable" @open-tree="emit('open-tree', $event)" @open-agent="emit('open-agent', $event)" @changed="changed" @error="emit('error', $event)" />
      <BatchDetail v-else-if="selected.kind === 'batch'" :key="selected.id" ref="detailRef" :project-id="projectId" :selected-id="selected.batch_id || selected.batch?.id" detail-only @open-tree="emit('open-tree', $event)" @open-agent="emit('open-agent', $event)" @changed="changed" @error="emit('error', $event)" />
      <div v-else class="agent-detail">
        <p v-if="workItemLegacy(selected)" class="muted">历史任务会话，下一轮执行后接入团队。</p>
        <p v-if="workItemProgress(selected)" class="muted">{{ workItemProgress(selected) }}</p>
        <p v-if="selected.phase === 'waiting_children'" class="muted">正在等待子任务返回，完成后 Lead 会继续核查。</p>
        <div v-if="hasWorkApproval(selected)" class="approval-note">有操作需要你批准。打开此智能体会话查看具体命令与理由。</div>
        <p v-if="selected.error" class="error">{{ selected.error }}</p>
        <p v-if="workItemVerification(selected)" class="verification" :class="{ 'verification-failed': workItemVerification(selected).includes('未通过'), 'verification-pending': workItemVerification(selected).includes('需重新') || workItemVerification(selected).startsWith('已验证') }">{{ workItemVerification(selected) }}</p>
        <section v-if="selected.result"><h4>结果</h4><p class="result">{{ selected.result }}</p></section>
        <section v-if="selected.audit?.reason"><h4>Lead 审查</h4><p class="result">{{ selected.audit.reason }}</p></section>
        <button class="primary" :disabled="!workItemCanOpen(selected)" @click="openAgent()">{{ hasWorkApproval(selected) ? '查看并处理权限申请' : hasWorkQuestion(selected) ? '打开会话回答问题' : '打开智能体会话' }}</button>
      </div>
    </template>
    <template v-else>
      <div class="filters" aria-label="筛选任务">
        <button v-for="option in [{ id:'all', label:'全部', count:work.counts.value.total }, { id:'active', label:'进行中', count:work.counts.value.active }, { id:'attention', label:'待处理', count:work.counts.value.attention }, { id:'completed', label:'已结束', count:work.counts.value.completed }]" :key="option.id" class="ghost small" :class="{ active:filter === option.id }" @click="filter = option.id">{{ option.label }} {{ option.count }}</button>
      </div>
      <div v-if="work.items.value.length" class="search"><input v-model="query" type="search" aria-label="搜索任务" placeholder="搜索任务、结果或错误" /></div>
      <div v-if="work.loading.value" class="empty">正在读取项目任务…</div>
      <div v-else-if="!work.items.value.length && !work.error.value" class="empty"><strong>还没有任务</strong><p>描述一个需求，Lead 会协调团队执行。需要先审查代码时，可以选择隔离执行。</p><button class="primary" @click="creating = true">新建任务</button></div>
      <div v-else-if="!shown.length && !work.error.value" class="empty">没有匹配的任务。<button class="ghost small" @click="filter = 'all'; query = ''">清除筛选</button></div>
      <ul v-else class="items">
        <li v-for="item in shown" :key="item.id" class="item" :class="workItemGroup(item)">
          <button class="item-main" @click="pick(item)">
            <div class="item-head"><span class="kind">{{ workItemMode(item) }}</span><span class="state" :class="workItemGroup(item)">{{ workItemState(item) }}</span></div>
            <strong class="item-title">{{ item.title || '未命名任务' }}</strong>
            <small v-if="workItemLegacy(item)" class="muted">历史任务会话，下一轮执行后接入团队。</small>
            <small v-if="workItemProgress(item)" class="muted">{{ workItemProgress(item) }}</small>
            <small v-if="workItemVerification(item)" class="verification" :class="{ 'verification-failed': workItemVerification(item).includes('未通过'), 'verification-pending': workItemVerification(item).includes('需重新') || workItemVerification(item).startsWith('已验证') }">{{ workItemVerification(item) }}</small>
            <small v-if="item.error" class="item-error">{{ item.error }}</small>
            <small v-else-if="item.result" class="result-preview">{{ item.result }}</small>
          </button>
          <div v-if="item.kind === 'agent' && workItemSession(item)" class="item-actions"><button class="ghost small" :disabled="!workItemCanOpen(item)" @click="openAgent(item)">{{ hasWorkApproval(item) ? '处理申请' : hasWorkQuestion(item) ? '回答问题' : '打开会话' }}</button></div>
        </li>
      </ul>
    </template>
  </section>
</template>

<style scoped>
.work-items { display:flex; flex-direction:column; height:100%; min-height:0; min-width:0; overflow:hidden; }
.work-questions { flex: none; padding: 9px 12px; background: var(--accent-soft); color: var(--accent); font-size: var(--fs-xs); }.work-questions button { display: flex; width: 100%; align-items: center; gap: 7px; text-align: left; padding: 7px 0; }.work-questions .question-preview { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--muted); }.work-questions button > span:last-child { white-space: nowrap; }
.head { display:flex; align-items:center; gap:8px; padding:10px 12px; border-bottom:1px solid var(--border); flex:none; }
.head > :first-child { flex:none; }
.count,.kind,.muted { color:var(--muted); font-size:var(--fs-xs); }
.spacer { flex:1; }.small { font-size:var(--fs-xs); }.icon { padding:4px; display:flex; }
.create-form { padding:14px; overflow:auto; display:flex; flex-direction:column; gap:12px; }
.create-form label { display:flex; flex-direction:column; gap:6px; font-size:var(--fs-xs); }
.create-form input:not([type=radio]):not([type=checkbox]),textarea,.search input { width:100%; min-width:0; font:inherit; }
textarea { resize:vertical; }.create-form fieldset { border:0; padding:0; margin:0; }.create-form legend { font-size:var(--fs-xs); margin-bottom:8px; }
.create-form .mode { flex-direction:row; align-items:flex-start; gap:9px; padding:10px; margin-bottom:6px; border:1px solid var(--border); border-radius:8px; cursor:pointer; }
.mode input { margin-top:3px; }.mode span { min-width:0; }.mode small { display:block; color:var(--muted); line-height:1.6; margin-top:3px; }.mode.chosen { border-color:var(--accent); background:var(--accent-soft); }.mode.disabled { opacity:.6; }
.create-form .check { flex-direction:row; align-items:flex-start; line-height:1.6; }.advanced summary { cursor:pointer; color:var(--muted); font-size:var(--fs-xs); }.advanced label { margin-top:10px; }
.filters { display:flex; flex-wrap:wrap; gap:4px; padding:10px 12px 6px; }.filters .active { color:var(--accent); background:var(--accent-soft); }.search { padding:4px 12px 10px; }.search input { font-size:var(--fs-xs); }
.items { overflow:auto; list-style:none; padding:4px 12px 12px; margin:0; display:flex; flex-direction:column; gap:9px; }.item { border:1px solid var(--border); border-radius:8px; overflow:hidden; flex:none; }.item.attention { border-color:var(--warn); }
.item-main { width:100%; text-align:left; display:flex; flex-direction:column; align-items:stretch; gap:7px; background:transparent; border:0; padding:12px; color:inherit; cursor:pointer; }.item-main:hover { background:var(--panel-soft); }.item-head { display:flex; gap:8px; align-items:center; justify-content:space-between; }.item-title { line-height:1.5; overflow-wrap:anywhere; font-size:var(--fs-sm); }
.state { font-size:var(--fs-xs); color:var(--muted); flex:none; }.state.active { color:var(--accent); }.state.attention { color:var(--warn); }.state.completed,.verification { color:var(--ok); }.item-actions { padding:0 10px 8px; text-align:right; }.item-error,.error { color:var(--err); }.item-error,.result-preview { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; line-height:1.6; overflow-wrap:anywhere; }
.verification-failed { color:var(--err); }
.verification-pending { color:var(--muted); }
.detail-session { padding:0 12px 8px; }
.approval-executor { margin:0 12px 8px; display:flex; gap:8px; align-items:center; font-size:var(--fs-xs); }.approval-executor select { flex:1; min-width:0; }.history-note { padding:0 12px 8px; margin:0; color:var(--muted); font-size:var(--fs-xs); }
.error { font-size:var(--fs-xs); padding:9px 12px; background:var(--err-soft); margin:0; overflow-wrap:anywhere; }.empty { padding:28px 20px; text-align:center; color:var(--muted); font-size:var(--fs-sm); line-height:1.8; }.empty strong { color:var(--text); }.empty p { margin:10px 0 18px; }
.detail-heading { display:flex; align-items:center; justify-content:space-between; padding:7px 12px; border-bottom:1px solid var(--border); }.selected-title { display:flex; justify-content:space-between; align-items:flex-start; padding:10px 12px; gap:10px; font-size:var(--fs-sm); }.selected-title strong { min-width:0; overflow-wrap:anywhere; }.agent-detail { padding:4px 14px 18px; overflow:auto; font-size:var(--fs-sm); }.agent-detail section { margin:16px 0; }.agent-detail h4 { font-size:var(--fs-xs); color:var(--muted); margin:0 0 8px; }.result { white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.7; }.approval-note { padding:10px; background:var(--warn-soft); color:var(--warn); border-radius:6px; line-height:1.7; font-size:var(--fs-xs); }.agent-detail .primary { margin-top:14px; }
</style>
