<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../../api.js'
import UiIcon from '../ui/UiIcon.vue'
import ApprovalRequest from '../ApprovalRequest.vue'
import QuestionRequest from '../QuestionRequest.vue'
import AgentTeamMonitor from './AgentTeamMonitor.vue'
import { agentCanStop, agentProgress, agentStateLabel, agentTreeRows, pendingTeamApprovals, pendingTeamQuestions } from '../../lib/agentTeam.js'

const props = defineProps({ team: { type: Object, default: null }, selectedId: { type: String, default: '' }, loading: Boolean, error: { type: String, default: '' }, acting: { type: String, default: '' }, visible: { type: Boolean, default: true } })
const emit = defineEmits(['refresh', 'stop'])
const open = ref(false), now = ref(Date.now()), approvalChecked = ref(false)
const taskRoot = ref(null)
const records = ref({}), recordLoading = ref({}), recordErrors = ref({}), errors = ref({}), submitting = ref({}), resolved = ref(new Set()), questionDrafts = ref({})
let generation = 0, disposed = false, clock = null
const matchingTeam = computed(() => props.team?.lead?.session_id === props.selectedId ? props.team : null)
const workers = computed(() => agentTreeRows(matchingTeam.value).filter(agent => agent.session_id !== props.selectedId))
const approvals = computed(() => pendingTeamApprovals(matchingTeam.value).filter(agent => agent.session_id !== props.selectedId && !resolved.value.has(`approval:${agent.session_id}:${agent.pending_approval.approval_id}`)))
const questions = computed(() => pendingTeamQuestions(matchingTeam.value).filter(group => group.session_id !== props.selectedId && !resolved.value.has(`question:${group.session_id}:${group.id}`)))
const running = computed(() => workers.value.filter(agentCanStop).length)
const keyFor = (kind, sid, id) => `${kind}:${sid}:${id}`
const current = (root, version) => !disposed && props.selectedId === root && generation === version
function checkApprovals() { approvalChecked.value = true; emit('refresh') }
async function focusAttention(id) {
  open.value = true; approvalChecked.value = true; emit('refresh')
  await nextTick()
  const card = [...(taskRoot.value?.querySelectorAll('[data-approval-session], [data-question-session]') || [])]
    .find(node => node.dataset.approvalSession === id || node.dataset.questionSession === id)
  const heading = card?.querySelector('h4')
  heading?.scrollIntoView({ block: 'nearest' }); heading?.focus({ preventScroll: true })
}
function toggleRecords(event, agent) { if (event.target.open && !records.value[agent.session_id]) void readRecords(agent) }
async function readRecords(agent) {
  if (!workers.value.some(value => value.session_id === agent.session_id) || recordLoading.value[agent.session_id]) return
  const root = props.selectedId, version = generation, id = agent.session_id
  recordLoading.value = { ...recordLoading.value, [id]: true }
  recordErrors.value = { ...recordErrors.value, [id]: '' }
  try {
    const result = await api.events(id)
    if (current(root, version)) records.value = { ...records.value, [id]: (Array.isArray(result) ? result : []).filter(event => event.seq > 0).slice(-200) }
  } catch (cause) { if (current(root, version)) recordErrors.value = { ...recordErrors.value, [id]: cause.message || '读取执行记录失败' } }
  finally { if (current(root, version)) recordLoading.value = { ...recordLoading.value, [id]: false } }
}
function recordText(event) {
  const payload = event.payload || {}
  return payload.text || payload.message || JSON.stringify(payload, null, 2)
}
async function decide(agent, approved) {
  const id = agent.session_id, approvalId = agent.pending_approval?.approval_id, key = keyFor('approval', id, approvalId)
  if (!approvalId || submitting.value[key] || !approvals.value.some(value => value.session_id === id && value.pending_approval.approval_id === approvalId)) return false
  const root = props.selectedId, version = generation
  submitting.value = { ...submitting.value, [key]: true }; errors.value = { ...errors.value, [key]: '' }
  try {
    const result = await api.decideApproval(approvalId, approved)
    if (!current(root, version)) return true
    if (result?.delivered === false) throw new Error('这项申请已结束，请核对最新审批状态')
    resolved.value = new Set([...resolved.value, key]); emit('refresh'); return true
  } catch (cause) {
    if (current(root, version)) { errors.value = { ...errors.value, [key]: cause.message || '审批未送达，请重试' }; emit('refresh') }
    return false
  } finally { if (current(root, version)) submitting.value = { ...submitting.value, [key]: false } }
}
async function answer(group, answers, dismiss = false) {
  const id = group.session_id, questionId = group.id, key = keyFor('question', id, questionId)
  if (submitting.value[key] || !questions.value.some(value => value.session_id === id && value.id === questionId)) return false
  const root = props.selectedId, version = generation
  submitting.value = { ...submitting.value, [key]: true }; errors.value = { ...errors.value, [key]: '' }
  try {
    const result = await api.answerQuestion(id, questionId, dismiss ? { dismiss: true } : { answers })
    if (!current(root, version)) return true
    if (result?.delivered === false) throw new Error('这组提问已结束，请核对最新状态')
    resolved.value = new Set([...resolved.value, key]); emit('refresh'); return true
  } catch (cause) {
    if (current(root, version)) { errors.value = { ...errors.value, [key]: cause.message || '回答未送达，请重试' }; emit('refresh') }
    return false
  } finally { if (current(root, version)) submitting.value = { ...submitting.value, [key]: false } }
}
watch(() => props.selectedId, () => {
  generation++; open.value = false; approvalChecked.value = false
  records.value = {}; recordLoading.value = {}; recordErrors.value = {}; errors.value = {}; submitting.value = {}; resolved.value = new Set(); questionDrafts.value = {}
}, { flush: 'sync' })
function updateClock() {
  clearInterval(clock); clock = null; now.value = Date.now()
  if (props.visible && open.value && running.value) clock = setInterval(() => { now.value = Date.now() }, 1000)
}
watch(() => [props.visible, open.value, running.value], updateClock)
onMounted(updateClock)
onBeforeUnmount(() => { disposed = true; generation++; clearInterval(clock) })
</script>

<template>
  <section v-if="matchingTeam || loading || error" ref="taskRoot" class="background-agent-tasks" aria-label="后台子任务">
    <div class="background-visualization"><AgentTeamMonitor :team="team" :selected-id="selectedId" :loading="loading" :error="error" :acting="acting" :visible="visible" @refresh="emit('refresh')" @stop="emit('stop', $event)" @attention="focusAttention" /></div>
    <details :open="open" @toggle="open = $event.target.open">
      <summary><UiIcon name="git-branch" :size="12" /><strong>后台子任务</strong><span>{{ loading ? '读取中…' : `${workers.length} 项${running ? ` · ${running} 项执行中` : ''}` }}</span></summary>
      <div class="background-task-list">
        <article v-for="agent in workers" :key="agent.session_id" :data-agent-session="agent.session_id">
          <header><strong>{{ agent.title || '子任务' }}</strong><span>{{ agentStateLabel(agent) }}</span><button v-if="agentCanStop(agent)" :disabled="!!acting" :aria-label="`停止子任务 ${agent.title || agent.session_id}`" @click="emit('stop', agent.session_id)">停止</button></header>
          <small v-if="agentProgress(agent, now).detail">{{ agentProgress(agent, now).detail }}</small><p v-if="agent.blocked_reason">{{ agent.blocked_reason }}</p>
          <details class="task-records" @toggle="toggleRecords($event, agent)"><summary>只读执行记录</summary>
            <button :disabled="!!recordLoading[agent.session_id]" @click="readRecords(agent)">刷新记录</button>
            <p v-if="recordLoading[agent.session_id]" role="status">正在读取…</p><p v-if="recordErrors[agent.session_id]" role="alert">{{ recordErrors[agent.session_id] }}</p>
            <ol v-if="records[agent.session_id]?.length"><li v-for="event in records[agent.session_id]" :key="event.seq"><small>{{ event.type }}</small><pre>{{ recordText(event) }}</pre></li></ol>
            <p v-else-if="records[agent.session_id]">暂无执行记录。</p>
          </details>
        </article>
        <p v-if="!workers.length && !loading">当前没有后台子任务。主智能体会按需要创建。</p>
        <button class="check-approvals" :disabled="loading" @click="checkApprovals">核对审批</button>
        <p v-if="approvalChecked && matchingTeam && !error && !loading && !approvals.length" role="status">当前没有待审批的子任务操作。</p>
      </div>
    </details>
    <p v-if="error" class="task-error" role="alert">{{ error }} <button :disabled="loading" @click="emit('refresh')">重试</button></p>
    <div v-if="approvals.length || questions.length" class="background-task-actions">
      <section v-for="agent in approvals" :key="keyFor('approval', agent.session_id, agent.pending_approval.approval_id)" :data-approval-session="agent.session_id" :data-approval-id="agent.pending_approval.approval_id">
        <h4 :title="agent.session_id" tabindex="-1">子任务操作申请 · {{ agent.title || '子任务' }} <small>{{ agent.session_id }}</small></h4>
        <ApprovalRequest :approval="agent.pending_approval" :readonly="agent.effective_permission !== 'workspace-write' && agent.effective_permission !== 'full-access'" :submitting="!!submitting[keyFor('approval', agent.session_id, agent.pending_approval.approval_id)]" @decide="decide(agent, $event)" />
        <p v-if="errors[keyFor('approval', agent.session_id, agent.pending_approval.approval_id)]" class="task-error" role="alert">{{ errors[keyFor('approval', agent.session_id, agent.pending_approval.approval_id)] }}</p>
      </section>
      <section v-for="group in questions" :key="keyFor('question', group.session_id, group.id)" :data-question-session="group.session_id">
        <h4 :title="group.session_id" tabindex="-1">子任务提问 · {{ group.executor_title }} <small>{{ group.session_id }}</small></h4>
        <QuestionRequest :group="group" :draft="questionDrafts[keyFor('question', group.session_id, group.id)] || {}" :error="errors[keyFor('question', group.session_id, group.id)] || ''" :submitting="!!submitting[keyFor('question', group.session_id, group.id)]" @update:draft="questionDrafts[keyFor('question', group.session_id, group.id)] = $event" @answer="answer(group, $event)" @dismiss="answer(group, null, true)" />
      </section>
    </div>
  </section>
</template>

<style scoped>
.background-visualization{float:right;margin-top:-2px;margin-left:8px}.background-agent-tasks>details>summary{min-height:26px;display:flex;align-items:center}.background-agent-tasks>details>summary::before{content:'›';display:inline-block;margin-right:3px;font-size:16px}.background-agent-tasks>details[open]>summary::before{transform:rotate(90deg)}
.background-agent-tasks { padding: 6px 12px; border-bottom: 1px solid var(--border); color: var(--muted); font-size: var(--fs-2xs); min-width: 0; }summary { cursor: pointer; }summary > strong,summary > span { margin-left: 6px; }summary > strong { color: var(--text); font-weight: var(--fw-medium); }.background-task-list { max-height: 230px; overflow: auto; padding-top: 7px; }.background-task-list article { padding: 8px; margin-bottom: 5px; border: 1px solid var(--border); border-radius: 5px; }header { display: flex; gap: 6px; align-items: center; }header strong { color: var(--text); flex: 1; min-width: 0; overflow-wrap: anywhere; }.background-agent-tasks button { color: var(--accent); background: var(--panel); border: 1px solid var(--border); border-radius: 4px; padding: 3px 6px; font-size: var(--fs-2xs); cursor: pointer; }button:disabled { opacity: .5; cursor: default; }p { margin: 5px 0; overflow-wrap: anywhere; }.task-records { margin-top: 6px; }.task-records ol { padding-left: 17px; }.task-records pre { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); font-size: var(--fs-2xs); }.background-task-actions { max-height: 43vh; overflow-y: auto; overscroll-behavior: contain; padding-top: 6px; }.background-task-actions > section { margin-bottom: 8px; }.background-task-actions :deep(.approval-request) { max-height: 35vh; }h4 { margin: 5px 0; color: var(--text); font-weight: var(--fw-medium); overflow-wrap: anywhere; }h4 small { display: block; color: var(--muted); font-size: 9px; font-weight: normal; }.task-error { color: var(--err); }
</style>
