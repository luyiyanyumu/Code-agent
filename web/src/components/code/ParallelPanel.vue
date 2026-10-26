<script setup>
import UiIcon from '../ui/UiIcon.vue'
// 并行面板（M18 · P5）：一个需求拆成 N 条车道，各自一棵隔离工作树，同时推进、串行落地。
//
// 界面要回答的问题只有三个（并行最容易出事的就是这三个）：
// 1. **谁在改哪个文件**：每条车道列出它独占的文件（写租约）；同一个文件被两条车道
//    声明时必须显式标出"谁先谁后"，而不是让用户自己算。
// 2. **真的同时在跑吗**：车道状态 + 波次（同一波 = 同时跑），deferred 的车道说明它
//    在等谁——它要等前面那条落地后才从新 HEAD 出发，这不是慢，是唯一正确的顺序。
// 3. **落地顺序**：合并队列按计划顺序一次一条；踩到冲突时主干会被恢复原状并停下。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../api.js'
import { useDialog } from '../../composables/useDialog.js'
import { agentStateLabel, agentProgress } from '../../lib/agentTeam.js'
import { hasWorkApproval, hasWorkQuestion, workItemQuestions, workItemActive, workItemApprovals, workItemCanOpen, workItemLegacy, workItemSession } from '../../lib/workItems.js'

const props = defineProps({
  projectId: { type: String, required: true },
  detailOnly: { type: Boolean, default: false },
  selectedId: { type: String, default: '' },
})
const emit = defineEmits(['error', 'changed', 'open-tree', 'open-agent'])
const dialog = useDialog()
let disposed = false
let projectEpoch = 0
let loadRequest = 0
let detailRequest = 0
onBeforeUnmount(() => { disposed = true })

const batches = ref([])
const selected = ref(null)
const loading = ref(false)
const busy = ref('')
const note = ref('')
const creating = ref(false)
const editingPlan = ref(false), planDraft = ref(''), planError = ref(''), laneVerification = ref({})
const laneApprovalSessions = ref({})
const manualPlan = ref('')
const form = ref({ requirement: '', title: '', planFirst: true, gitInit: false, workers: 3 })

const STATUS_META = {
  planning: { label: '计划中', cls: 'muted' },
  plan_failed: { label: '计划需手改', cls: 'warn' },
  planned: { label: '待批准', cls: 'warn' },
  running: { label: '执行中', cls: 'busy' },
  review: { label: '待审查', cls: 'accent' },
  merging: { label: '落地中', cls: 'busy' },
  merged: { label: '已全部落地', cls: 'ok' },
  partial: { label: '部分落地', cls: 'warn' },
  discarded: { label: '已放弃', cls: 'muted' },
  failed: { label: '有车道失败', cls: 'err' },
}

const LANE_META = {
  planning: { label: '计划中', cls: 'muted' },
  planned: { label: '待执行', cls: 'warn' },
  deferred: { label: '排队等前置', cls: 'wait' },
  running: { label: '执行中', cls: 'busy' },
  review: { label: '待审查', cls: 'accent' },
  merged: { label: '已落地', cls: 'ok' },
  conflict: { label: '冲突', cls: 'err' },
  failed: { label: '失败', cls: 'err' },
  discarded: { label: '已放弃', cls: 'muted' },
}

const plan = computed(() => selected.value?.plan || {})
const lanes = computed(() => selected.value?.lanes || [])
const conflicts = computed(() => selected.value?.conflicts || [])
const progress = computed(() => selected.value?.progress || { total: 0, merged: 0, running: 0, review: 0 })
const executionActive = computed(() => workItemActive(selected.value) || lanes.value.some(lane => workItemActive(lane)))
const canApprove = computed(() => !!selected.value && !executionActive.value && ['planned', 'plan_failed'].includes(selected.value.status))
const canMerge = computed(() =>
  !!selected.value && !executionActive.value && ['review', 'planned', 'plan_failed', 'partial'].includes(selected.value.status))

async function load() {
  const pid = props.projectId, epoch = projectEpoch, request = ++loadRequest
  const selectedId = selected.value?.id, selectionRequest = detailRequest
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && loadRequest === request
  loading.value = true
  try {
    if (props.detailOnly) {
      if (!selectedId) return
      const batch = await api.code.batch(pid, selectedId)
      if (current() && selected.value?.id === selectedId && selectionRequest === detailRequest) selected.value = batch
      return
    }
    const list = await api.code.batches(pid)
    if (!current()) return
    batches.value = list
    if (selectedId && selected.value?.id === selectedId && selectionRequest === detailRequest) {
      try {
        const batch = await api.code.batch(pid, selectedId)
        if (current() && selected.value?.id === selectedId && selectionRequest === detailRequest) selected.value = batch
      } catch {
        if (current() && selected.value?.id === selectedId && selectionRequest === detailRequest) selected.value = null
      }
    }
  } catch (e) {
    if (current()) emit('error', e.message)
  } finally {
    if (current()) loading.value = false
  }
}

async function open(batchId) {
  const pid = props.projectId, epoch = projectEpoch, request = ++detailRequest
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && detailRequest === request
  loading.value = true
  try {
    const batch = await api.code.batch(pid, batchId)
    if (current()) selected.value = batch
  } catch (e) {
    if (current()) emit('error', e.message)
  } finally { if (current()) loading.value = false }
}

async function create() {
  const requirement = form.value.requirement.trim()
  if (!requirement) {
    note.value = '请先写清需求'
    return
  }
  let manual = null
  const raw = manualPlan.value.trim()
  if (raw) {
    try {
      manual = JSON.parse(raw)
    } catch {
      note.value = '手动车道计划不是合法 JSON'
      return
    }
  }
  busy.value = 'create'
  note.value = ''
  try {
    const batch = await api.code.createBatch(props.projectId, {
      requirement,
      title: form.value.title.trim(),
      plan_first: manual ? false : form.value.planFirst,
      git_init: form.value.gitInit,
      ...(manual ? { plan: manual } : {}),
    })
    creating.value = false
    manualPlan.value = ''
    form.value = { requirement: '', title: '', planFirst: true, gitInit: false, workers: 3 }
    await load()
    await open(batch.id)
    if (batch.conflicts?.length) {
      note.value = `有 ${batch.conflicts.length} 处文件被多条车道声明：靠后的那条会在前置落地后再执行（队列里可见）`
    }
    if (batch.status === 'plan_failed') {
      note.value = '模型没能给出可解析的车道计划：请粘贴/修改「手动车道计划」后再批准'
    }
    emit('changed', batch)
  } catch (e) {
    emit('error', e.message)
  } finally {
    busy.value = ''
  }
}

async function approve() {
  if (!selected.value || busy.value) return
  const pid = props.projectId, epoch = projectEpoch, id = selected.value.id
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && selected.value?.id === id
  busy.value = 'approve'
  note.value = '各车道在各自的隔离工作树里同时执行（主干不受影响）…'
  try {
    const result = await api.code.approveBatch(pid, id, {
      max_workers: Number(form.value.workers) || 3,
    })
    if (!current()) return
    note.value = result.ok
      ? `执行完成：并发峰值 ${result.max_concurrent} 条车道同时进行${result.deferred?.length ? `（${result.deferred.length} 条在等前置落地）` : ''}`
      : '有车道执行失败：看各车道状态与错误'
    await load()
    if (current()) emit('changed', result.batch)
  } catch (e) {
    if (current()) emit('error', e.message)
  } finally {
    if (current()) busy.value = ''
  }
}

async function refreshReview() {
  if (!selected.value || busy.value) return
  const pid = props.projectId, epoch = projectEpoch, id = selected.value.id
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && selected.value?.id === id
  busy.value = 'review'
  try {
    const result = await api.code.reviewBatch(pid, id)
    if (!current()) return
    selected.value = result.batch
    note.value = `整批符号差异：新增 ${result.total.added} · 修改 ${result.total.modified} · 删除 ${result.total.removed}`
    emit('changed', result.batch)
  } catch (e) {
    if (current()) emit('error', e.message)
  } finally {
    if (current()) busy.value = ''
  }
}

function editPlan() {
  planDraft.value = JSON.stringify(plan.value, null, 2)
  planError.value = ''; editingPlan.value = !editingPlan.value
}
async function savePlan() {
  if (!selected.value || busy.value) return
  let value
  try { value = JSON.parse(planDraft.value) } catch { planError.value = '计划需要是有效的 JSON。'; return }
  if (!value || typeof value !== 'object' || Array.isArray(value)) { planError.value = '计划需要是 JSON 对象。'; return }
  const pid = props.projectId, epoch = projectEpoch, id = selected.value.id
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && selected.value?.id === id
  busy.value = 'plan'; planError.value = ''
  try {
    await api.code.updateBatchPlan(pid, id, value)
    if (!current()) return
    editingPlan.value = false; await load()
    if (current()) emit('changed', selected.value)
  } catch (cause) { if (current()) planError.value = cause.message }
  finally { if (current()) busy.value = '' }
}
async function verifyLane(lane) {
  if (!lane.task_id || busy.value) return
  const pid = props.projectId, epoch = projectEpoch, id = selected.value?.id
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && selected.value?.id === id
  busy.value = `verify:${lane.task_id}`
  try {
    const result = await api.code.verifyTask(pid, lane.task_id)
    if (!current()) return
    laneVerification.value = { ...laneVerification.value, [lane.task_id]: result }
    emit('changed', selected.value)
  } catch (cause) { if (current()) emit('error', cause.message) }
  finally { if (current()) busy.value = '' }
}

async function confirmBatchAction(action, options, perform) {
  if (!selected.value || busy.value || disposed) return null
  const pid = props.projectId, epoch = projectEpoch, batch = { ...selected.value }
  const scopeCurrent = () => !disposed && props.projectId === pid && projectEpoch === epoch
  const targetCurrent = () => scopeCurrent() && selected.value?.id === batch.id
  busy.value = `confirm:${action}`
  try {
    return await dialog.open({
      ...options, details: `批次：${batch.title || batch.id}\n车道：${lanes.value.length} 条`,
      onConfirm: async () => {
        if (!targetCurrent()) throw new Error('当前项目或批次已变化，请关闭弹窗后重新操作。')
        busy.value = action
        try { return await perform({ pid, batch, scopeCurrent, targetCurrent }) }
        finally { if (scopeCurrent()) busy.value = `confirm:${action}` }
      },
    })
  } finally { if (scopeCurrent()) busy.value = '' }
}

async function mergeQueue() {
  return confirmBatchAction('merge', {
    title: '按队列合并到主干？', tone: 'warning', icon: 'git-branch',
    description: '各车道将按计划顺序逐条合并。遇到冲突会中止当前合并并停止队列，已经完成的车道会保留。',
    confirmText: '按队列合并', pendingText: '正在合并队列…',
  }, async ({ pid, batch, targetCurrent }) => {
    const result = await api.code.mergeBatch(pid, batch.id, {})
    const failure = !result.ok ? `合并到 ${result.log?.find(row => row.error)?.lane || '某条车道'} 时停止：${result.log?.find(row => row.error)?.error || '请处理车道冲突后重试。'}` : ''
    if (targetCurrent()) {
      note.value = result.ok ? `已按顺序落地 ${result.merged.length} 条车道：${result.merged.join(' → ')}` : failure
      await load()
      if (targetCurrent()) { emit('changed', result.batch); emit('open-tree', { tree: 'main' }) }
    }
    if (failure) throw new Error(failure)
    return result
  })
}

async function discard() {
  return confirmBatchAction('discard', {
    title: '放弃整个批次？', tone: 'danger', icon: 'trash',
    description: '将删除所有车道的隔离工作树与分支，尚未合并的改动会丢失。已落地的主干文件保持不变。',
    confirmText: '放弃整个批次', pendingText: '正在放弃批次…',
  }, async ({ pid, batch, scopeCurrent, targetCurrent }) => {
    const result = await api.code.discardBatch(pid, batch.id)
    if (!targetCurrent()) return result
    note.value = '已放弃整批'
    selected.value = null
    await load()
    if (scopeCurrent()) { emit('changed', {}); emit('open-tree', { tree: 'main' }) }
    return result
  })
}

/** 点车道：地图切到这条车道的工作树（看它自己的符号与差异）。 */
function openLane(lane) {
  if (lane.workspace_available === false || ['merged', 'discarded'].includes(lane.status)) return
  if (lane.task_id) emit('open-tree', { taskId: lane.task_id, tree: `task:${lane.task_id}` })
}
function openLaneAgent(lane, question = null) {
  if (!workItemCanOpen(lane)) return
  const chosen = workItemApprovals(lane).find(value => value.session_id === laneApprovalSessions.value[lane.task_id])
  emit('open-agent', { sessionId: question?.session_id || chosen?.session_id || workItemSession(lane), tree: `task:${lane.task_id}`, approval: !question && hasWorkApproval(lane), ...(question || hasWorkQuestion(lane) ? { question: true } : {}) })
}

function fmtTime(ts) {
  if (!ts) return ''
  const date = new Date(ts)
  return isNaN(date.getTime()) ? ts : date.toLocaleString('zh-CN', { hour12: false })
}
function verificationLabel(value) {
  if (value?.stale) return '代码已变化，需重新验证'
  if (value?.ok == null) return '尚未验证'
  return value.ok ? '验证通过' : '验证未通过'
}

watch([() => props.projectId, () => props.selectedId], () => {
  projectEpoch++; detailRequest++; selected.value = null; busy.value = ''; note.value = ''
  editingPlan.value = false; planError.value = ''; laneVerification.value = {}
  laneApprovalSessions.value = {}
  if (props.detailOnly) { if (props.selectedId) void open(props.selectedId) }
  else void load()
}, { immediate: true })
defineExpose({ load, open })
</script>

<template>
  <div class="par">
    <div v-if="!detailOnly" class="head">
      <UiIcon name="layers" :size="15" class="panel-icon" /><span class="title">并行</span>
      <span class="muted">{{ batches.length }} 个批次</span>
      <span class="spacer" />
      <button class="ghost" title="新建并行批次" @click="creating = !creating">＋</button>
      <button class="ghost" title="刷新" aria-label="刷新面板" @click="load"><UiIcon name="refresh" :size="14" /></button>
    </div>

    <div v-if="!detailOnly && creating" class="create">
      <textarea
        v-model="form.requirement"
        rows="3"
        placeholder="描述一个可以并行拆分的需求，例如：给三个模块分别补上类型注解与单测"
      />
      <input v-model="form.title" placeholder="标题（可选）" />
      <label class="chk">
        <input v-model="form.planFirst" type="checkbox" /> 先让模型拆车道，我再批准（推荐）
      </label>
      <label class="chk">
        <input v-model="form.gitInit" type="checkbox" /> 若不是 git 仓库，自动初始化并提交当前代码
      </label>
      <details>
        <summary>手动车道计划（可选：粘贴/修改 JSON）</summary>
        <textarea
          v-model="manualPlan"
          rows="5"
          placeholder='{"goal":"…","lanes":[{"id":"L1","files":["pkg/a.py"],"verify":["python -m pytest"]},{"id":"L2","files":["pkg/b.py"]}]}'
        />
      </details>
      <div class="row">
        <button class="primary" :disabled="busy === 'create'" @click="create">
          {{ busy === 'create' ? '创建中…' : '创建批次' }}
        </button>
        <button class="ghost" @click="creating = false">取消</button>
        <span class="spacer" />
        <span class="muted tiny">并发</span>
        <input v-model.number="form.workers" class="num" type="number" min="1" max="6" />
      </div>
      <p class="hint muted">
        同一个文件只能属于一条车道：撞车的车道会在前置落地之后（从最新 HEAD）自动重做，
        所以"并行执行"不会变成"互相覆盖"。
      </p>
    </div>

    <div v-if="note" class="note">{{ note }}</div>
    <div v-if="detailOnly && !selected" class="muted pad">{{ loading ? '读取并行任务详情…' : '暂时无法读取并行任务详情。' }}<button v-if="!loading" class="ghost small" @click="open(selectedId)">重试</button></div>
    <div v-if="!detailOnly && loading && !batches.length" class="muted pad">读取中…</div>
    <div v-else-if="!detailOnly && !batches.length && !creating" class="muted pad">
      还没有并行批次。点 ＋ 描述需求：模型会把它拆成互不重叠的车道，各自在隔离工作树里同时改。
    </div>

    <ul v-else-if="!detailOnly" class="list">
      <li
        v-for="b in batches"
        :key="b.id"
        :class="{ selected: selected?.id === b.id }"
        @click="open(b.id)"
      >
        <div class="row1">
          <span class="status" :class="STATUS_META[b.status]?.cls">{{ STATUS_META[b.status]?.label || b.status }}</span>
          <span class="tname">{{ b.title || b.requirement.slice(0, 40) }}</span>
        </div>
        <div class="row2">
          <span class="muted">{{ b.progress.merged }}/{{ b.progress.total }} 条已落地</span>
          <span v-if="b.conflicts" class="muted">· {{ b.conflicts }} 处文件重叠</span>
          <span class="spacer" />
          <span class="muted mono">{{ (b.queue || []).join(' → ') }}</span>
        </div>
      </li>
    </ul>

    <div v-if="selected" class="detail">
      <div class="detail-head">
        <span class="status" :class="STATUS_META[selected.status]?.cls">
          {{ STATUS_META[selected.status]?.label || selected.status }}
        </span>
        <span class="mono ellipsis" :title="plan.goal">{{ plan.goal || selected.title }}</span>
        <span class="spacer" />
        <button v-if="canApprove" class="ghost small" :disabled="!!busy" @click="editPlan">调整计划</button>
        <button class="ghost small" :disabled="!!busy || executionActive || ['merged','discarded'].includes(selected.status)" @click="refreshReview">审查</button>
      </div>

      <div v-if="selected.error" class="err-note">{{ selected.error }}</div>
      <div v-if="executionActive" class="note">团队仍在执行车道及其子任务，全部结束后可审查与合并。</div>
      <div v-if="editingPlan" class="plan-editor">
        <label>并行计划 JSON<textarea v-model="planDraft" rows="8" spellcheck="false" :disabled="!!busy" /></label>
        <p v-if="planError" class="err-note" role="alert">{{ planError }}</p>
        <button class="primary small" :disabled="!!busy" @click="savePlan">{{ busy === 'plan' ? '保存中…' : '保存计划' }}</button>
      </div>

      <div class="body">
        <section v-if="conflicts.length">
          <div class="sub">文件重叠（写租约）</div>
          <div v-for="c in conflicts" :key="c.path" class="conflict">
            <span class="mono">{{ c.path }}</span>
            <span class="muted tiny">{{ c.lanes.join(' → ') }}（后者等前者落地）</span>
          </div>
        </section>

        <section>
          <div class="sub">
            车道
            <span class="muted tiny">已落地 {{ progress.merged }}/{{ progress.total }}</span>
          </div>
          <div v-for="lane in lanes" :key="lane.lane" class="lane" @click="openLane(lane)">
            <div class="l-head">
              <span class="status" :class="LANE_META[lane.status]?.cls">
                {{ LANE_META[lane.status]?.label || lane.status }}
              </span>
              <b>{{ lane.lane }} · {{ lane.title }}</b>
              <span class="spacer" />
              <span v-if="lane.symbol_counts && (lane.symbol_counts.added || lane.symbol_counts.modified || lane.symbol_counts.removed)"
                    class="muted tiny">
                +{{ lane.symbol_counts.added || 0 }} ~{{ lane.symbol_counts.modified || 0 }} -{{ lane.symbol_counts.removed || 0 }}
              </span>
            </div>
            <div class="mono files">{{ (lane.leased?.length ? lane.leased : lane.files).join('  ') || '（未声明文件）' }}</div>
            <div v-if="lane.wait_for?.length" class="muted tiny">
              等 {{ lane.wait_for.join('、') }} 落地后再执行（要改同一个文件）
            </div>
            <div v-if="lane.error" class="lane-err">{{ lane.error }}</div>
            <div v-if="lane.agent" class="muted tiny">{{ agentStateLabel(lane.agent) }}<template v-if="agentProgress(lane.agent).detail"> · {{ agentProgress(lane.agent).detail }}</template></div>
            <div v-if="workItemLegacy(lane)" class="muted tiny">历史任务会话，下一轮执行后接入团队。</div>
            <div v-if="laneVerification[lane.task_id] || lane.verification" class="tiny" :class="(laneVerification[lane.task_id] || lane.verification).ok === false ? 'lane-err' : 'muted'">{{ verificationLabel(laneVerification[lane.task_id] || lane.verification) }}</div>
            <div class="lane-actions">
              <select v-if="workItemApprovals(lane).length > 1" v-model="laneApprovalSessions[lane.task_id]" aria-label="选择车道审批执行者" @click.stop><option v-for="(approval,index) in workItemApprovals(lane)" :key="approval.approval_id" :value="approval.session_id">{{ approval.executor_title || approval.title || `执行智能体 ${index + 1}` }}</option></select>
              <button v-if="lane.task_id && ['review', 'failed'].includes(lane.status)" class="ghost small" :disabled="!!busy || workItemActive(lane) || lane.workspace_available === false" @click.stop="verifyLane(lane)">{{ busy === `verify:${lane.task_id}` ? '验证中…' : '跑验证' }}</button>
              <button v-if="workItemSession(lane)" class="ghost small" :disabled="!workItemCanOpen(lane)" @click.stop="openLaneAgent(lane)">{{ hasWorkApproval(lane) ? '处理申请' : hasWorkQuestion(lane) ? '回答问题' : '执行会话' }}</button>
            </div>
            <div v-if="hasWorkQuestion(lane)" class="lane-questions" aria-label="车道待回答问题"><button v-for="question in workItemQuestions(lane)" :key="`${question.session_id}:${question.id}`" class="ghost small" :disabled="!workItemCanOpen(lane)" :data-question-session="question.session_id" :data-question-id="question.id" @click.stop="openLaneAgent(lane, question)">{{ question.executor_title }} · {{ question.questions?.[0]?.question || '需要你的回答' }} →</button></div>
            <div v-if="lane.workspace_available === false" class="muted tiny">工作树已清理，可以查看历史结果。</div>
          </div>
        </section>

        <section v-if="(selected.waves || []).length">
          <div class="sub">执行波次（同一波同时跑）</div>
          <div v-for="(wave, i) in selected.waves" :key="i" class="wave mono">
            第 {{ i + 1 }} 波：{{ wave.join(' + ') }}
          </div>
        </section>

        <section v-if="(selected.queue || []).length">
          <div class="sub">合并队列（串行，一次一条）</div>
          <div class="mono queue">{{ (selected.queue || []).join(' → ') }}</div>
        </section>
      </div>

      <footer>
        <label v-if="canApprove" class="muted tiny">最多同时执行 <input v-model.number="form.workers" type="number" min="1" max="16" class="workers" /> 项</label>
        <button v-if="canApprove" class="primary" :disabled="!!busy" @click="approve">
          {{ busy === 'approve' ? '执行中…' : '批准并执行全部车道' }}
        </button>
        <button v-if="canMerge" class="primary" :disabled="!!busy" @click="mergeQueue">
          {{ busy === 'merge' ? '落地中…' : '按队列落地到主干' }}
        </button>
        <button class="danger" :disabled="!!busy || executionActive || ['merged','discarded'].includes(selected.status)" @click="discard">放弃整批</button>
        <span class="spacer" />
        <span class="muted tiny">{{ fmtTime(selected.updated_at) }}</span>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.par { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; }
.plan-editor { padding: 10px; overflow: auto; max-height: 45%; flex: none; }
.plan-editor label { display: flex; flex-direction: column; gap: 6px; font-size: var(--fs-xs); }
.plan-editor textarea { width: 100%; font-family: var(--font-code); margin-bottom: 8px; resize: vertical; }
.workers { width: 44px; font-size: var(--fs-xs); }
.lane-actions { display: flex; justify-content: flex-end; margin-top: 6px; }
.head { display: flex; align-items: center; gap: 8px; min-height: 42px; flex: none; padding: 7px 14px; border-bottom: 1px solid var(--border); font-size: var(--fs-sm); }
.title { font-weight: var(--fw-semibold); }.panel-icon { flex: none; color: var(--muted); }
.spacer { flex: 1; }
.ghost { display: inline-flex; align-items: center; justify-content: center; min-height: 26px; border: none; background: transparent; color: var(--muted); padding: 3px 6px; }
.ghost:hover { background: var(--hover); }
.ghost.small { font-size: var(--fs-xs); }

.create { display: flex; flex-direction: column; gap: 10px; padding: 14px; background: var(--panel-soft); border-bottom: 1px solid var(--border); }
.create textarea, .create input { width: 100%; font-size: var(--fs-sm); }
.create .num { width: 52px; }
.create details summary { font-size: var(--fs-xs); color: var(--muted); cursor: pointer; }
.chk { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-xs); color: var(--muted); }
.row { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; }
.hint { font-size: var(--fs-xs); margin: var(--sp-0-5) 0 0; }

.note { padding: var(--sp-1-5) var(--sp-2-5); font-size: var(--fs-xs); color: var(--accent); background: var(--accent-soft); }
.err-note { padding: var(--sp-1-5) var(--sp-2-5); font-size: var(--fs-xs); color: var(--err); background: var(--err-soft); }
.pad { padding: 24px 18px; font-size: var(--fs-xs); line-height: 1.8; text-align: center; color: var(--muted); }

.list { list-style: none; margin: 0; padding: 8px; overflow-y: auto; max-height: 34%; }
.list li { padding: 10px; border: 1px solid transparent; border-radius: 6px; cursor: pointer; margin-bottom: 4px; }
.list li:hover { background: var(--bg); }
.list li.selected { background: var(--accent-soft); border-color: var(--accent-border); }
.row1 { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-sm); }
.row2 { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-xs); margin-top: var(--sp-0-5); }
.tname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status { flex: none; font-size: var(--fs-xs); padding: var(--sp-hair) 5px; border-radius: var(--r-sm); background: var(--hover); }
.status.warn { background: var(--warn-soft); color: var(--warn); }
.status.wait { background: #f5f3ff; color: #6d28d9; }
.status.accent { background: var(--accent-soft); color: var(--accent); }
.status.ok { background: var(--ok-soft); color: var(--ok); }
.status.err { background: var(--err-soft); color: var(--err); }
.status.busy { background: #eff6ff; color: var(--accent-hover); }

.detail { border-top: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; flex: 1; }
.detail-head { display: flex; align-items: center; gap: var(--sp-1-5); padding: 5px var(--sp-2-5); border-bottom: 1px solid var(--border); font-size: var(--fs-xs); }
.body { flex: 1; overflow-y: auto; padding: var(--sp-2) var(--sp-2-5); font-size: var(--fs-sm); }
.sub { font-size: var(--fs-xs); font-weight: var(--fw-semibold); color: var(--muted); margin: var(--sp-2) 0 var(--sp-1); display: flex; gap: var(--sp-1-5); align-items: center; }
.conflict { font-size: var(--fs-xs); color: var(--warn); background: var(--warn-soft); border-radius: var(--r-sm); padding: 3px var(--sp-1-5); margin-bottom: var(--sp-1); display: flex; gap: var(--sp-1-5); align-items: baseline; }
.lane { border: 1px solid var(--border); border-radius: var(--r-sm); padding: var(--sp-1-5) var(--sp-2); margin-bottom: var(--sp-1-5); cursor: pointer; }
.lane:hover { background: var(--panel-soft); }
.l-head { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-xs); }
.files { font-size: var(--fs-xs); color: var(--text-2); word-break: break-all; }
.lane-err { font-size: var(--fs-xs); color: var(--err); }
.wave, .queue { font-size: var(--fs-xs); color: var(--text-2); }

footer { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; padding: var(--sp-2) var(--sp-2-5); border-top: 1px solid var(--border); }
.tiny { font-size: var(--fs-xs); }
</style>
