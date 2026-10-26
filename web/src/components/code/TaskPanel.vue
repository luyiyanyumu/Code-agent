<script setup>
import UiIcon from '../ui/UiIcon.vue'
// 任务面板（M18 · P4）：需求 → 计划 → 批准 → 隔离执行 → 审查 → 合并/放弃。
//
// 界面的核心是让三件事一眼可见：
// 1. **这次要动什么**（计划里的文件/符号/风险/验证命令），批准之前先说清楚；
// 2. **实际动了什么**（符号级差异：新增/修改/删除了哪些类与函数，越界改动单独标红）；
// 3. **怎么落地**（整体合并 / 只应用某些文件 / 跑验证 / 放弃）。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../api.js'
import { useDialog } from '../../composables/useDialog.js'
import { workItemActive, workItemLegacy } from '../../lib/workItems.js'

const props = defineProps({
  projectId: { type: String, required: true },
  writable: { type: Boolean, default: false },   // 主干是否允许直改（决定"任务"入口的提示口径）
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

const tasks = ref([])
const selected = ref(null)      // 任务详情（含 plan / review）
const loading = ref(false)
const busy = ref('')
const note = ref('')
const creating = ref(false)
const editingPlan = ref(false), planDraft = ref(''), planError = ref('')
const form = ref({ requirement: '', title: '', planFirst: true, gitInit: false, manualPlan: '' })

const STATUS_META = {
  planning: { label: '计划中', cls: 'muted' },
  planned: { label: '待批准', cls: 'warn' },
  plan_failed: { label: '计划需手改', cls: 'warn' },
  running: { label: '执行中', cls: 'busy' },
  review: { label: '待审查', cls: 'accent' },
  merged: { label: '已合并', cls: 'ok' },
  discarded: { label: '已放弃', cls: 'muted' },
  failed: { label: '失败', cls: 'err' },
}

const plan = computed(() => selected.value?.plan || {})
const review = computed(() => selected.value?.review || {})
const symbols = computed(() => review.value?.symbols || {})
const counts = computed(() => symbols.value?.counts || {})
const gate = computed(() => review.value?.gate || null)
const risk = computed(() => review.value?.impact?.risk || null)
const gateErrors = computed(() => (gate.value?.violations || []).filter((v) => v.severity === 'error'))
const executionActive = computed(() => workItemActive(selected.value))
const canApprove = computed(() =>
  !!selected.value && !executionActive.value && ['planned', 'plan_failed'].includes(selected.value.status))
const canMerge = computed(() =>
  !!selected.value && !executionActive.value && ['review', 'planned', 'plan_failed'].includes(selected.value.status))
const workspaceAvailable = computed(() => selected.value?.workspace_available !== false && !['merged', 'discarded'].includes(selected.value?.status))

// ---- P6：架构规则（存在平台侧，不写进用户仓库） ----
const showRules = ref(false)
const rulesText = ref('')
const rulesInfo = ref(null)
const rulesNote = ref('')

async function loadRules() {
  try {
    const data = await api.code.architecture(props.projectId)
    rulesInfo.value = data
    rulesText.value = JSON.stringify(data.rules || {}, null, 2)
    rulesNote.value = (data.errors || []).join('；')
  } catch (e) {
    emit('error', e.message)
  }
}

async function saveRules() {
  let parsed = {}
  try {
    parsed = rulesText.value.trim() ? JSON.parse(rulesText.value) : {}
  } catch {
    rulesNote.value = '规则不是合法 JSON'
    return
  }
  try {
    const data = await api.code.saveArchitecture(props.projectId, parsed)
    rulesInfo.value = data
    rulesNote.value = '已保存（下次审查/合并立即生效）'
    await load()
  } catch (e) {
    rulesNote.value = e.message
  }
}

function toggleRules() {
  showRules.value = !showRules.value
  if (showRules.value) loadRules()
}

async function load() {
  const pid = props.projectId, epoch = projectEpoch, request = ++loadRequest
  const selectedId = selected.value?.id, selectionRequest = detailRequest
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && loadRequest === request
  loading.value = true
  try {
    if (props.detailOnly) {
      if (!selectedId) return
      const task = await api.code.task(pid, selectedId)
      if (current() && selected.value?.id === selectedId && selectionRequest === detailRequest) selected.value = task
      return
    }
    const list = await api.code.tasks(pid)
    if (!current()) return
    tasks.value = list
    if (selectedId && selected.value?.id === selectedId && selectionRequest === detailRequest) {
      const still = list.find(t => t.id === selectedId)
      if (still) {
        const task = await api.code.task(pid, still.id)
        if (current() && selected.value?.id === selectedId && selectionRequest === detailRequest) selected.value = task
      }
      else selected.value = null
    }
  } catch (e) {
    if (current()) emit('error', e.message)
  } finally {
    if (current()) loading.value = false
  }
}

async function open(taskId) {
  const pid = props.projectId, epoch = projectEpoch, request = ++detailRequest
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && detailRequest === request
  loading.value = true
  try {
    const task = await api.code.task(pid, taskId)
    if (!current()) return
    selected.value = task
    if (workspaceAvailable.value) emit('open-tree', { taskId, tree: `task:${taskId}` })
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
  busy.value = 'create'
  note.value = ''
  try {
    let manual = null
    const raw = form.value.manualPlan.trim()
    if (raw) {
      try {
        manual = JSON.parse(raw)
      } catch {
        throw new Error('手动计划不是合法 JSON')
      }
    }
    const task = await api.code.createTask(props.projectId, {
      requirement,
      title: form.value.title.trim(),
      plan_first: manual ? false : form.value.planFirst,
      git_init: form.value.gitInit,
      ...(manual ? { plan: manual } : {}),
    })
    creating.value = false
    form.value = { requirement: '', title: '', planFirst: true, gitInit: false, manualPlan: '' }
    await load()
    await open(task.id)
    if (task.status === 'plan_failed') {
      note.value = '模型没能产出可解析的计划：请在上方"手动计划"里粘贴/修改 JSON 后再批准'
    }
    emit('changed', task)
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
  note.value = '正在隔离工作树里执行（改动不会碰主干）…'
  try {
    const task = await api.code.approveTask(pid, id)
    if (!current()) return
    note.value = task.error || (['running', 'queued', 'planning'].includes(task.status) ? '已批准，团队正在隔离工作树中执行。' : '执行完成，请审查下方差异')
    await load()
    if (current()) emit('changed', task)
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
    await api.code.updateTaskPlan(pid, id, value)
    if (!current()) return
    editingPlan.value = false; await load()
    if (current()) emit('changed', selected.value)
  } catch (cause) { if (current()) planError.value = cause.message }
  finally { if (current()) busy.value = '' }
}

async function doReview() {
  if (!selected.value || busy.value) return
  const pid = props.projectId, epoch = projectEpoch, id = selected.value.id
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && selected.value?.id === id
  busy.value = 'review'
  try {
    await api.code.taskReview(pid, id)
    if (!current()) return
    await load()
    if (!current()) return
    note.value = '审查快照已更新'
    emit('changed', selected.value)
  } catch (e) {
    if (current()) emit('error', e.message)
  } finally {
    if (current()) busy.value = ''
  }
}

async function verify() {
  if (!selected.value || busy.value) return
  const pid = props.projectId, epoch = projectEpoch, id = selected.value.id
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && selected.value?.id === id
  busy.value = 'verify'
  note.value = '正在工作树里跑计划里的验证命令…'
  try {
    const result = await api.code.verifyTask(pid, id)
    if (!current()) return
    note.value = result.ok
      ? `验证通过（${(result.results || []).length} 条命令）`
      : `验证未通过：${(result.results || []).map((r) => r.command).join(' / ') || result.note || ''}`
    await load()
    if (current()) emit('changed', selected.value)
  } catch (e) {
    if (current()) emit('error', e.message)
  } finally {
    if (current()) busy.value = ''
  }
}

async function confirmTaskAction(action, options, perform) {
  if (!selected.value || busy.value || disposed) return null
  const pid = props.projectId, epoch = projectEpoch
  const task = { ...selected.value }
  const scopeCurrent = () => !disposed && props.projectId === pid && projectEpoch === epoch
  const targetCurrent = () => scopeCurrent() && selected.value?.id === task.id
  busy.value = `confirm:${action}`
  try {
    return await dialog.open({
      ...options,
      details: [`任务：${task.title || task.id}`, options.details].filter(Boolean).join('\n'),
      onConfirm: async value => {
        if (!targetCurrent()) throw new Error('当前项目或任务已变化，请关闭弹窗后重新操作。')
        busy.value = action
        try { return await perform({ pid, task, value, scopeCurrent, targetCurrent }) }
        finally { if (scopeCurrent()) busy.value = `confirm:${action}` }
      },
    })
  } finally { if (scopeCurrent()) busy.value = '' }
}

function forceReasonOptions(errors) {
  if (!errors.length) return {}
  return {
    input: { label: '强制应用理由', value: '', placeholder: '说明本次接受这些门禁问题的原因', required: true, maxLength: 2000, multiline: true, hint: '理由会记录在被改文件的决策线程中。' },
    validate: value => String(value || '').trim() ? '' : '请填写强制应用理由。',
  }
}

async function mergeAll() {
  const errors = gateErrors.value.map(row => row.message)
  const force = errors.length > 0
  return confirmTaskAction('merge', {
    title: force ? '强制合并任务？' : '合并任务到主干？',
    description: force ? `有 ${errors.length} 条架构门禁问题。填写理由后才能继续合并。` : '将任务改动合并到当前项目，生成合并提交，并清理隔离工作树与分支。',
    details: force ? errors.map(message => `· ${message}`).join('\n') : '',
    tone: force ? 'danger' : 'warning', icon: 'git-branch',
    confirmText: force ? '强制合并' : '合并到主干', pendingText: '正在合并…',
    ...forceReasonOptions(errors),
  }, async ({ pid, task, value, targetCurrent }) => {
    const result = await api.code.mergeTask(pid, task.id, force ? { force: true, reason: value.trim() } : undefined)
    if (!targetCurrent()) return result
    note.value = force ? `已强制落地 ${result.files?.length || 0} 个文件（理由已记入决策线程）` : `已合并 ${result.files?.length || 0} 个文件到主干`
    await load()
    if (targetCurrent()) { emit('changed', result); emit('open-tree', { tree: 'main' }) }
    return result
  })
}

async function applyFile(path) {
  const errors = gateErrors.value.map(row => row.message)
  const force = errors.length > 0
  return confirmTaskAction(`file:${path}`, {
    title: '应用文件并结束任务？', tone: 'warning', icon: 'file-code',
    description: '仅所选文件进入主干，暂不生成提交。任务的隔离工作树与分支会被清理，其余任务改动将被放弃。',
    details: [`文件：${path}`, ...errors.map(message => `· ${message}`)].join('\n'),
    confirmText: '应用并结束任务', pendingText: '正在应用…', ...forceReasonOptions(errors),
  }, async ({ pid, task, value, targetCurrent }) => {
    const result = await api.code.mergeTask(pid, task.id, { files: [path], ...(force ? { force: true, reason: value.trim() } : {}) })
    if (!targetCurrent()) return result
    note.value = `已把 ${path} 应用到主干（未提交，请在「源代码管理」里审阅后提交）`
    await load()
    if (targetCurrent()) { emit('changed', result); emit('open-tree', { tree: 'main' }) }
    return result
  })
}

async function discard() {
  return confirmTaskAction('discard', {
    title: '放弃这个任务？', tone: 'danger', icon: 'warning',
    description: '将删除任务的隔离工作树与分支，尚未合并的改动会丢失。主干文件保持不变。',
    confirmText: '放弃任务', pendingText: '正在放弃…',
  }, async ({ pid, task, scopeCurrent, targetCurrent }) => {
    const result = await api.code.discardTask(pid, task.id)
    if (!targetCurrent()) return result
    note.value = '已放弃该任务'
    selected.value = null
    await load()
    if (scopeCurrent()) { emit('open-tree', { tree: 'main' }); emit('changed', {}) }
    return result
  })
}

function fmtTime(ts) {
  if (!ts) return ''
  const date = new Date(ts)
  return isNaN(date.getTime()) ? ts : date.toLocaleString('zh-CN', { hour12: false })
}

watch([() => props.projectId, () => props.selectedId], () => {
  projectEpoch++; detailRequest++; selected.value = null; busy.value = ''; note.value = ''
  editingPlan.value = false; planError.value = ''
  if (props.detailOnly) { if (props.selectedId) void open(props.selectedId) }
  else void load()
}, { immediate: true })
defineExpose({ load, open })
</script>

<template>
  <div class="tasks">
    <div v-if="!detailOnly" class="head">
      <UiIcon name="layers" :size="15" class="panel-icon" /><span class="title">任务</span>
      <span class="muted">{{ tasks.length }} 个</span>
      <span class="spacer" />
      <button class="ghost" title="新建任务" @click="creating = !creating">＋</button>
      <button class="ghost" title="刷新" aria-label="刷新面板" @click="load"><UiIcon name="refresh" :size="14" /></button>
    </div>

    <div v-if="!detailOnly && creating" class="create">
      <textarea v-model="form.requirement" rows="3" placeholder="描述需求，例如：把 SessionRuntime 的审批等待抽成独立方法，并补一个测试" />
      <input v-model="form.title" placeholder="标题（可选，用于分支名）" />
      <label class="chk"><input v-model="form.planFirst" type="checkbox" /> 先生成计划让我批准（推荐）</label>
      <label class="chk"><input v-model="form.gitInit" type="checkbox" /> 若不是 git 仓库，自动初始化并提交当前代码</label>
      <details class="manual">
        <summary>手动计划（可选：粘贴/修改 Plan JSON）</summary>
        <textarea v-model="form.manualPlan" rows="5" placeholder='{"goal":"…","subtasks":[{"id":"s1","files":["pkg/a.py"],"verify":["python -m pytest"]}]}' />
      </details>
      <div class="row">
        <button class="primary" :disabled="busy === 'create'" @click="create">
          {{ busy === 'create' ? '创建中…' : '创建任务' }}
        </button>
        <button class="ghost" @click="creating = false">取消</button>
      </div>
      <p class="hint muted">
        AI 会在独立的 git 工作树与分支里改代码；主干在你点「合并」之前不会被触碰。
      </p>
    </div>

    <div v-if="note" class="note">{{ note }}</div>
    <div v-if="detailOnly && !selected" class="muted pad">{{ loading ? '读取任务详情…' : '暂时无法读取任务详情。' }}<button v-if="!loading" class="ghost small" @click="open(selectedId)">重试</button></div>
    <div v-if="!detailOnly && loading && !tasks.length" class="muted pad">读取中…</div>
    <div v-else-if="!detailOnly && !tasks.length && !creating" class="muted pad">
      还没有任务。点右上角 ＋ 描述一个需求：AI 会先给计划，你批准后才在隔离工作树里动手。
    </div>

    <ul v-else-if="!detailOnly" class="list">
      <li
        v-for="t in tasks"
        :key="t.id"
        :class="{ selected: selected?.id === t.id }"
        @click="open(t.id)"
      >
        <div class="row1">
          <span class="status" :class="STATUS_META[t.status]?.cls">{{ STATUS_META[t.status]?.label || t.status }}</span>
          <span class="tname">{{ t.title || t.requirement.slice(0, 40) }}</span>
        </div>
        <div class="row2">
          <span class="muted mono">{{ t.branch }}</span>
          <span class="spacer" />
          <span v-if="t.changed_files" class="muted">{{ t.changed_files }} 文件</span>
          <span v-if="t.symbol_counts && (t.symbol_counts.added || t.symbol_counts.modified || t.symbol_counts.removed)"
                class="muted">
            +{{ t.symbol_counts.added || 0 }} ~{{ t.symbol_counts.modified || 0 }} -{{ t.symbol_counts.removed || 0 }}
          </span>
        </div>
      </li>
    </ul>

    <div v-if="selected" class="detail">
      <div class="detail-head">
        <span class="status" :class="STATUS_META[selected.status]?.cls">
          {{ STATUS_META[selected.status]?.label || selected.status }}
        </span>
        <span class="mono ellipsis" :title="selected.branch">{{ selected.branch }}</span>
        <span class="spacer" />
        <button v-if="canApprove" class="ghost small" :disabled="!!busy" @click="editPlan">调整计划</button>
        <button class="ghost small" :disabled="!!busy || !workspaceAvailable || executionActive" @click="doReview">审查</button>
        <button class="ghost small" :disabled="!!busy || !workspaceAvailable || executionActive" @click="verify">跑验证</button>
      </div>

      <div v-if="selected.error" class="err-note">{{ selected.error }}</div>
      <div v-if="workItemLegacy({ ...selected, kind: 'task' })" class="note">历史任务会话，下一轮执行后接入团队。</div>
      <div v-if="executionActive" class="note">团队仍在执行此任务及其子任务，完成后可验证与合并。</div>
      <div v-if="editingPlan" class="plan-editor">
        <label>计划 JSON<textarea v-model="planDraft" rows="8" spellcheck="false" :disabled="!!busy" /></label>
        <p v-if="planError" class="err-note" role="alert">{{ planError }}</p>
        <button class="primary small" :disabled="!!busy" @click="savePlan">{{ busy === 'plan' ? '保存中…' : '保存计划' }}</button>
      </div>

      <details class="rules" @toggle="toggleRules">
        <summary>架构约束（分层 / 禁环 / 公共面 / 保护路径）</summary>
        <div class="rules-body">
          <p class="muted tiny">
            规则存在平台侧（不写进你的仓库）。合并前会按这些规则判定：error 级会被拦下，
            强制落地必须写明理由。
          </p>
          <textarea v-model="rulesText" rows="6" spellcheck="false" />
          <div class="row">
            <button class="ghost small" @click="saveRules">保存规则</button>
            <span v-if="rulesInfo?.layers?.length" class="muted tiny">已定义层：{{ rulesInfo.layers.join('、') }}</span>
            <span v-if="rulesNote" class="muted tiny">{{ rulesNote }}</span>
          </div>
        </div>
      </details>

      <div class="body">
        <section v-if="plan.goal || (plan.subtasks || []).length">
          <div class="sub">计划</div>
          <p v-if="plan.goal" class="goal">{{ plan.goal }}</p>
          <div v-for="s in plan.subtasks || []" :key="s.id" class="subtask">
            <div class="st-head">
              <b>{{ s.title || s.id }}</b>
              <span class="risk" :class="s.risk">{{ s.risk }}</span>
            </div>
            <div class="mono files">{{ (s.files || []).join('  ') }}</div>
            <div v-if="(s.verify || []).length" class="mono verify">验证：{{ (s.verify || []).join(' · ') }}</div>
            <div v-if="s.notes" class="muted notes">{{ s.notes }}</div>
          </div>
        </section>

        <section v-if="gate && (gate.violations || []).length">
          <div class="sub">
            架构门禁
            <span v-if="gate.ok" class="ok-badge">通过（{{ gate.counts.warn }} 条提醒）</span>
            <span v-else class="warn-badge">{{ gateErrors.length }} 条会被拦下</span>
          </div>
          <ul class="violations">
            <li v-for="(v, i) in gate.violations" :key="i" :class="v.severity">
              <span class="v-rule">{{ v.rule }}</span>
              <span>{{ v.message }}</span>
            </li>
          </ul>
        </section>

        <section v-if="risk">
          <div class="sub">静态可达与风险</div>
          <div class="symline">
            <span class="risk-badge" :class="risk.level">{{ risk.level }} · {{ risk.score }}</span>
            静态可达 {{ risk.signals.fan_in }} · 跨 {{ risk.signals.files }} 文件
            <template v-if="review.impact?.tests?.length"> · 关联测试 {{ review.impact.tests.length }}</template>
          </div>
          <ul class="reasons">
            <li v-for="(r, i) in risk.reasons" :key="i">{{ r }}</li>
          </ul>
          <div v-if="review.impact?.basis" class="basis">依据：{{ review.impact.basis }}</div>
        </section>

        <section v-if="review.changed_files">
          <div class="sub">
            改动
            <span v-if="review.plan_scope_ok === false" class="warn-badge">有越界改动</span>
          </div>
          <div v-if="(review.outside_plan || []).length" class="outside">
            计划外：{{ (review.outside_plan || []).join('、') }}
          </div>
          <div class="symline">
            符号：新增 {{ counts.added || 0 }} · 修改 {{ counts.modified || 0 }} · 删除 {{ counts.removed || 0 }}
          </div>
          <ul class="changes">
            <li v-for="c in review.changes || []" :key="c.path">
              <span class="cstatus">{{ c.status }}</span>
              <span class="mono ellipsis" :title="c.path">{{ c.path }}</span>
              <span class="spacer" />
              <button
                class="ghost small"
                :disabled="!!busy || !workspaceAvailable || executionActive"
                @click.stop="applyFile(c.path)"
              >{{ busy === `file:${c.path}` ? '应用中…' : '应用' }}</button>
            </li>
          </ul>
          <div class="symbols">
            <div v-if="(symbols.added || []).length" class="sym-group">
              <span class="sym-title add">新增</span>
              <span v-for="s in symbols.added.slice(0, 30)" :key="s.id" class="mono sym">{{ s.kind }} {{ s.name }}</span>
            </div>
            <div v-if="(symbols.modified || []).length" class="sym-group">
              <span class="sym-title mod">修改</span>
              <span v-for="s in symbols.modified.slice(0, 30)" :key="s.id" class="mono sym">{{ s.kind }} {{ s.name }}</span>
            </div>
            <div v-if="(symbols.removed || []).length" class="sym-group">
              <span class="sym-title del">删除</span>
              <span v-for="s in symbols.removed.slice(0, 30)" :key="s.id" class="mono sym">{{ s.kind }} {{ s.name }}</span>
            </div>
          </div>
          <div v-if="(review.diagnostics && (review.diagnostics.error || review.diagnostics.warning))"
               class="symline">
            工作树诊断：{{ review.diagnostics.error || 0 }} 错误 · {{ review.diagnostics.warning || 0 }} 警告
          </div>
          <div v-if="(review.verification || []).length" class="verify-result">
            <div v-for="(v, i) in review.verification" :key="i" class="mono" :class="v.ok ? 'ok' : 'bad'">
              {{ v.ok ? '✓' : '✗' }} {{ v.command }}
            </div>
          </div>
        </section>
      </div>

      <footer>
        <button v-if="canApprove" class="primary" :disabled="!!busy" @click="approve">
          {{ busy === 'approve' ? '执行中…' : '批准并执行' }}
        </button>
        <button v-if="canMerge" class="primary" :disabled="!!busy || !review.changed_files" @click="mergeAll">
          {{
            busy === 'merge'
              ? '合并中…'
              : gateErrors.length
                ? `强制落地（${gateErrors.length} 条未过）`
                : '整体合并到主干'
          }}
        </button>
        <button class="danger" :disabled="!!busy || !workspaceAvailable || executionActive" @click="discard">放弃</button>
        <span class="spacer" />
        <span class="muted tiny">{{ fmtTime(selected.updated_at) }}</span>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.tasks { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; }
.plan-editor { padding: 10px; overflow: auto; max-height: 45%; flex: none; }
.plan-editor label { display: flex; flex-direction: column; gap: 6px; font-size: var(--fs-xs); }
.plan-editor textarea { width: 100%; font-family: var(--font-code); margin-bottom: 8px; resize: vertical; }
.head { display: flex; align-items: center; gap: 8px; min-height: 42px; flex: none; padding: 7px 14px; border-bottom: 1px solid var(--border); font-size: var(--fs-sm); }
.title { font-weight: var(--fw-semibold); }.panel-icon { flex: none; color: var(--muted); }
.spacer { flex: 1; }
.ghost { display: inline-flex; align-items: center; justify-content: center; min-height: 26px; border: none; background: transparent; color: var(--muted); padding: 3px 6px; }
.ghost:hover { background: var(--hover); }
.ghost.small { font-size: var(--fs-xs); }

.create { display: flex; flex-direction: column; gap: 10px; padding: 14px; background: var(--panel-soft); border-bottom: 1px solid var(--border); }
.create textarea, .create input { width: 100%; font-size: var(--fs-sm); }
.chk { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-xs); color: var(--muted); }
.manual summary { font-size: var(--fs-xs); color: var(--muted); cursor: pointer; }
.row { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; }
.hint { font-size: var(--fs-xs); margin: var(--sp-0-5) 0 0; }

.note { padding: var(--sp-1-5) var(--sp-2-5); font-size: var(--fs-xs); color: var(--accent); background: var(--accent-soft); }
.err-note { padding: var(--sp-1-5) var(--sp-2-5); font-size: var(--fs-xs); color: var(--err); background: var(--err-soft); }

.rules { border-bottom: 1px solid var(--border); padding: var(--sp-1-5) var(--sp-2-5); }
.rules summary { font-size: var(--fs-xs); color: var(--muted); cursor: pointer; }
.rules-body { display: flex; flex-direction: column; gap: var(--sp-1); margin-top: var(--sp-1-5); }
.rules-body textarea { width: 100%; font-size: var(--fs-xs); font-family: ui-monospace, monospace; }
.ok-badge { font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); background: var(--ok-soft); color: var(--ok); }
.violations { list-style: none; margin: 0 0 var(--sp-1-5); padding: 0; }
.violations li { display: flex; gap: var(--sp-1-5); font-size: var(--fs-xs); padding: 3px var(--sp-1-5); border-radius: var(--r-sm); margin-bottom: 3px; background: var(--warn-soft); color: var(--warn); }
.violations li.error { background: var(--err-soft); color: var(--err); }
.v-rule { flex: none; font-size: var(--fs-xs); font-weight: var(--fw-bold); text-transform: uppercase; }
.risk-badge { font-size: var(--fs-xs); font-weight: var(--fw-bold); padding: 0 var(--sp-1-5); border-radius: var(--r-sm); background: var(--hover); margin-right: var(--sp-1); }
.risk-badge.low { background: var(--ok-soft); color: var(--ok); }
.risk-badge.medium { background: var(--warn-soft); color: var(--warn); }
.risk-badge.high { background: #fff7ed; color: #c2410c; }
.risk-badge.critical { background: var(--err-soft); color: var(--err); }
.reasons { margin: var(--sp-0-5) 0 var(--sp-1-5); padding-left: var(--sp-4); font-size: var(--fs-xs); color: var(--text-2); }
.basis { font-size: var(--fs-xs); color: var(--muted); margin-bottom: var(--sp-1-5); }
.pad { padding: 24px 18px; font-size: var(--fs-xs); line-height: 1.8; text-align: center; color: var(--muted); }

.list { list-style: none; margin: 0; padding: 8px; overflow-y: auto; max-height: 40%; }
.list li { padding: 10px; border: 1px solid transparent; border-radius: 6px; cursor: pointer; margin-bottom: 4px; }
.list li:hover { background: var(--bg); }
.list li.selected { background: var(--accent-soft); border-color: var(--accent-border); }
.row1 { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-sm); }
.row2 { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-xs); margin-top: var(--sp-0-5); }
.tname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status { flex: none; font-size: var(--fs-xs); padding: var(--sp-hair) 5px; border-radius: var(--r-sm); background: var(--hover); }
.status.warn { background: var(--warn-soft); color: var(--warn); }
.status.accent { background: var(--accent-soft); color: var(--accent); }
.status.ok { background: var(--ok-soft); color: var(--ok); }
.status.err { background: var(--err-soft); color: var(--err); }
.status.busy { background: #eff6ff; color: var(--accent-hover); }

.detail { border-top: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; flex: 1; }
.detail-head { display: flex; align-items: center; gap: var(--sp-1-5); padding: 5px var(--sp-2-5); border-bottom: 1px solid var(--border); font-size: var(--fs-xs); }
.body { flex: 1; overflow-y: auto; padding: var(--sp-2) var(--sp-2-5); font-size: var(--fs-sm); }
.sub { font-size: var(--fs-xs); font-weight: var(--fw-semibold); color: var(--muted); margin: var(--sp-2) 0 var(--sp-1); display: flex; gap: var(--sp-1-5); align-items: center; }
.goal { margin: 0 0 var(--sp-1-5); }
.subtask { padding: var(--sp-1-5) var(--sp-2); border: 1px solid var(--border); border-radius: var(--r-sm); margin-bottom: var(--sp-1-5); }
.st-head { display: flex; align-items: center; gap: var(--sp-1-5); }
.risk { font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); background: var(--hover); color: var(--muted); }
.risk.high { background: var(--err-soft); color: var(--err); }
.risk.medium { background: var(--warn-soft); color: var(--warn); }
.files { font-size: var(--fs-xs); color: var(--text-2); word-break: break-all; }
.verify { font-size: var(--fs-xs); color: var(--accent); }
.notes { font-size: var(--fs-xs); margin-top: var(--sp-0-5); }
.warn-badge { font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); background: var(--err-soft); color: var(--err); }
.outside { font-size: var(--fs-xs); color: var(--err); margin-bottom: var(--sp-1); }
.symline { font-size: var(--fs-xs); color: var(--text-2); margin-bottom: var(--sp-1); }
.changes { list-style: none; margin: 0 0 var(--sp-1-5); padding: 0; }
.changes li { display: flex; align-items: center; gap: var(--sp-1-5); padding: 3px var(--sp-1); border-radius: var(--r-sm); }
.changes li:hover { background: var(--bg); }
.cstatus { font-size: var(--fs-xs); color: var(--warn); flex: none; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.symbols { display: flex; flex-direction: column; gap: var(--sp-1); }
.sym-group { display: flex; flex-wrap: wrap; gap: var(--sp-1); align-items: baseline; }
.sym-title { font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); }
.sym-title.add { background: var(--ok-soft); color: var(--ok); }
.sym-title.mod { background: var(--warn-soft); color: var(--warn); }
.sym-title.del { background: var(--err-soft); color: var(--err); }
.sym { font-size: var(--fs-xs); color: var(--text-2); }
.verify-result { margin-top: var(--sp-1-5); font-size: var(--fs-xs); }
.verify-result .ok { color: var(--ok); }
.verify-result .bad { color: var(--err); }

footer { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; padding: var(--sp-2) var(--sp-2-5); border-top: 1px solid var(--border); }
.tiny { font-size: var(--fs-xs); }
</style>
