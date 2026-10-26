<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'
import PageHeader from '../components/ui/PageHeader.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import UiIcon from '../components/ui/UiIcon.vue'

const items = ref([])
const error = ref('')
const notice = ref('')
const creating = ref(false)
const loading = ref(false)
const toggling = ref('')
const statusFilter = ref('all')
const customSchedule = ref(false)
const activeCount = computed(() => items.value.filter(item => item.status === 'active').length)
const visibleItems = computed(() => statusFilter.value === 'all' ? items.value : items.value.filter(item => item.status === statusFilter.value))

const form = ref({
  name: '',
  prompt: '',
  schedule_type: 'recurring',
  rrule: 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
  scheduled_at: '',
})

// 常用模板，降低 rrule 记忆成本
const PRESETS = [
  { label: '每天 09:00', rrule: 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0' },
  { label: '每工作日 09:00', rrule: 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0' },
  { label: '每周一 10:00', rrule: 'FREQ=WEEKLY;BYDAY=MO;BYHOUR=10;BYMINUTE=0' },
  { label: '每 6 小时', rrule: 'FREQ=HOURLY;INTERVAL=6' },
  { label: '每月 1 日 09:00', rrule: 'FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0' },
]

const canSubmit = computed(() => {
  if (!form.value.name.trim() || !form.value.prompt.trim()) return false
  if (form.value.schedule_type === 'once') return !!form.value.scheduled_at
  return !!form.value.rrule.trim()
})

async function load({ afterMutation = false } = {}) {
  if (loading.value || (!afterMutation && (creating.value || toggling.value))) return
  loading.value = true
  error.value = ''
  try { items.value = await api.automations() } catch (e) { error.value = e.message }
  finally { loading.value = false }
}

async function create() {
  if (!canSubmit.value || creating.value || loading.value || toggling.value) return
  error.value = ''
  notice.value = ''
  creating.value = true
  try {
    const payload = {
      name: form.value.name.trim(),
      prompt: form.value.prompt.trim(),
      schedule_type: form.value.schedule_type,
    }
    if (form.value.schedule_type === 'once') {
      // datetime-local 无时区，补成 ISO（本地时区）
      payload.scheduled_at = new Date(form.value.scheduled_at).toISOString()
    } else {
      payload.rrule = form.value.rrule.trim()
    }
    await api.createAutomation(payload)
    notice.value = '已创建'
    form.value.name = ''
    form.value.prompt = ''
    await load({ afterMutation: true })
  } catch (e) {
    error.value = e.message
  } finally {
    creating.value = false
  }
}

async function toggle(item) {
  if (toggling.value || creating.value || loading.value) return
  toggling.value = item.id
  error.value = ''
  try {
    await api.setAutomationStatus(item.id, item.status === 'active' ? 'paused' : 'active')
    await load({ afterMutation: true })
  } catch (e) {
    error.value = e.message
  } finally { toggling.value = '' }
}

function scheduleLabel(item) {
  if (item.schedule_type === 'once') return `一次性 · ${fmt(item.scheduled_at)}`
  return PRESETS.find(preset => preset.rrule === item.rrule)?.label || '自定义周期'
}

function selectFrequency(event) {
  customSchedule.value = event.target.value === 'custom'
  if (!customSchedule.value) form.value.rrule = event.target.value
}

function fmt(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  return isNaN(d.getTime()) ? ts : d.toLocaleString('zh-CN', { hour12: false })
}

onMounted(load)
defineExpose({ reload: load })
</script>

<template>
  <div class="page automations-page">
    <div class="page-content">
      <PageHeader title="自动化任务" description="安排重复工作，让任务按计划执行。" eyebrow="工作区管理" icon="clock">
        <template #actions><button :disabled="loading || creating || !!toggling" @click="load"><UiIcon name="refresh" :size="15" />{{ loading ? '刷新中…' : '刷新任务' }}</button></template>
      </PageHeader>
      <div v-if="error" class="feedback error" role="alert"><UiIcon name="alert" :size="17" /><span>{{ error }}</span></div>
      <div v-if="notice" class="feedback success" role="status"><UiIcon name="check" :size="17" /><span>{{ notice }}</span></div>
      <div class="overview"><div><span class="live-dot"></span><strong>{{ activeCount }}</strong> 个任务已启用</div><span>{{ items.length }} 个已配置任务</span></div>
      <div class="layout">
        <section class="list panel">
          <div class="section-head"><div><h3>任务列表</h3><p>查看计划、状态与下次执行时间</p></div><select v-model="statusFilter" aria-label="筛选任务状态"><option value="all">全部状态</option><option value="active">已启用</option><option value="paused">已暂停</option><option value="completed">已完成</option></select></div>
          <div v-if="loading && !items.length" class="loading-state" role="status">正在加载任务…</div>
          <div v-else-if="visibleItems.length" class="task-list">
            <article v-for="a in visibleItems" :key="a.id" class="task-card">
              <div class="task-top"><div class="task-icon"><UiIcon name="clock" :size="18" /></div><div class="task-name"><h4>{{ a.name }}</h4><span>{{ scheduleLabel(a) }}</span></div><span class="badge" :class="a.status">{{ { active: '已启用', paused: '已暂停', completed: '已完成' }[a.status] || a.status }}</span></div>
              <p class="task-prompt">{{ a.prompt }}</p>
              <details v-if="a.schedule_type !== 'once' && !PRESETS.some(p => p.rrule === a.rrule)" class="schedule-detail"><summary>查看调度规则</summary><code>{{ a.rrule }}</code></details>
              <div class="task-bottom"><span>下次执行 <strong>{{ fmt(a.next_run_at) }}</strong></span><button v-if="a.status !== 'completed'" :disabled="!!toggling || creating || loading" @click="toggle(a)">{{ toggling === a.id ? '更新中…' : a.status === 'active' ? '暂停' : '恢复' }}</button></div>
            </article>
          </div>
          <EmptyState v-else :title="items.length ? '没有符合条件的任务' : '还没有自动化任务'" :description="items.length ? '切换筛选条件，查看其他状态的任务。' : '填写任务内容和执行时间，创建你的第一个计划。'" icon="clock" />
        </section>
        <section class="panel create-panel">
          <div class="section-head"><div><h3>新建任务</h3><p>一次性执行，或按固定周期重复</p></div><span class="create-icon"><UiIcon name="plus" :size="18" /></span></div>
          <form class="form" @submit.prevent="create">
            <label><span>任务名称</span><input v-model="form.name" placeholder="例如：每日晨报" required /></label>
            <label><span>执行内容</span><textarea v-model="form.prompt" rows="4" placeholder="描述需要完成的工作，例如：汇总昨日销售数据并生成简报" required></textarea></label>
            <label><span>执行方式</span><select v-model="form.schedule_type"><option value="recurring">周期执行</option><option value="once">一次性执行</option></select></label>
            <template v-if="form.schedule_type === 'recurring'">
              <label><span>执行频率</span><select @change="selectFrequency"><option v-for="p in PRESETS" :key="p.rrule" :value="p.rrule" :selected="p.rrule === form.rrule">{{ p.label }}</option><option value="custom" :selected="!PRESETS.some(p => p.rrule === form.rrule)">自定义</option></select></label>
              <details class="advanced" :open="customSchedule || !PRESETS.some(p => p.rrule === form.rrule)"><summary>自定义调度规则</summary><label><span>RRULE</span><input v-model="form.rrule" class="mono" aria-label="RRULE 调度规则" /></label><p>使用 RRULE 格式设置更精细的重复计划。</p></details>
            </template>
            <label v-else><span>执行时间</span><input v-model="form.scheduled_at" type="datetime-local" required /></label>
            <p v-if="form.schedule_type === 'once'" class="timezone">执行时间使用本地时区</p>
            <button class="primary submit-button" :disabled="!canSubmit || creating || loading || !!toggling" type="submit"><UiIcon name="plus" :size="16" />{{ creating ? '创建中…' : '创建任务' }}</button>
            <p class="tip"><UiIcon name="shield" :size="15" /><span>任务以严格模式运行，需要人工审批的操作会自动拒绝。</span></p>
          </form>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { min-width: 0; overflow-y: auto; padding: 28px; background: var(--bg, #f4f6f9); }.page-content { max-width: 1440px; margin: 0 auto; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; }.feedback { display: flex; align-items: center; gap: 10px; border: 1px solid; padding: 12px 14px; border-radius: 8px; margin-bottom: 18px; font-size: 13px; overflow-wrap: anywhere; }.feedback.error { background: #fff1f2; color: #b42336; border-color: #f4cfd5; }.feedback.success { background: #eefaf4; color: #18764d; border-color: #cdeada; }
.overview { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; padding: 0 2px; margin-bottom: 18px; color: var(--muted); font-size: 13px; }.overview > div { display: flex; align-items: center; gap: 7px; }.overview strong { color: var(--text); font-weight: 650; }.live-dot { width: 7px; height: 7px; border-radius: 50%; background: #24a474; margin-right: 3px; }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 22px; align-items: start; }.panel { min-width: 0; border: 1px solid var(--border); border-radius: 12px; background: var(--panel, #fff); overflow: hidden; }.section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 20px; border-bottom: 1px solid var(--border); }.section-head h3 { font-size: 15px; margin: 0 0 5px; }.section-head p { font-size: 12px; color: var(--muted); margin: 0; }.section-head select { max-width: 130px; font-size: 12px; }.create-icon { width: 30px; height: 30px; display: grid; place-items: center; color: var(--accent); border-radius: 8px; background: #eef2ff; flex: none; }
.task-list { padding: 0 20px; }.task-card { padding: 22px 0; border-bottom: 1px solid var(--border); }.task-card:last-child { border: 0; }.task-top { display: flex; align-items: center; gap: 11px; }.task-icon { width: 36px; height: 36px; display: grid; place-items: center; color: #647087; background: #f4f6fa; border-radius: 9px; flex: none; }.task-name { min-width: 0; flex: 1; }.task-name h4 { margin: 0 0 4px; font-size: 14px; overflow-wrap: anywhere; }.task-name > span { color: var(--muted); font-size: 12px; }.badge { flex: none; font-size: 11px; padding: 4px 8px; border-radius: 5px; background: #f0f2f6; color: var(--muted); }.badge.active { background: #eaf7f0; color: #18764d; }.badge.paused { background: #fff5e2; color: #a46612; }.task-prompt { margin: 15px 0; color: #5d6879; font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; white-space: pre-wrap; max-height: 110px; overflow-y: auto; }.task-bottom { display: flex; align-items: center; justify-content: space-between; gap: 12px; }.task-bottom > span { font-size: 12px; color: var(--muted); }.task-bottom strong { display: inline-block; color: #4e596b; margin-left: 6px; font-weight: 450; }.task-bottom button { min-width: 60px; }.schedule-detail { color: var(--muted); font-size: 12px; margin: 0 0 15px; }.schedule-detail summary { cursor: pointer; }.schedule-detail code { display: block; margin-top: 8px; overflow-wrap: anywhere; }
.form { display: flex; flex-direction: column; gap: 17px; padding: 20px; }.form label { display: flex; flex-direction: column; gap: 7px; min-width: 0; }.form label > span { color: #465166; font-size: 12px; font-weight: 550; }.form input,.form select,.form textarea { width: 100%; min-width: 0; font-size: 13px; }.form textarea { resize: vertical; line-height: 1.6; }.advanced { padding: 11px 12px; border: 1px solid var(--border); background: #fafbfe; border-radius: 7px; font-size: 12px; }.advanced summary { cursor: pointer; color: var(--muted); }.advanced label { margin-top: 12px; }.advanced p { color: var(--muted); font-size: 11px; margin: 9px 0 0; }.timezone { margin: -6px 0 0; font-size: 11px; color: var(--muted); }.submit-button { width: 100%; }.tip { display: flex; align-items: flex-start; gap: 7px; font-size: 11px; color: var(--muted); line-height: 1.7; margin: 0; }.tip svg { flex: none; margin-top: 2px; }.loading-state { padding: 64px 20px; text-align: center; color: var(--muted); font-size: 13px; }
@media (max-width: 1100px) { .layout { grid-template-columns: minmax(0, 1fr) 310px; gap: 16px; } }
@media (max-width: 860px) { .layout { grid-template-columns: minmax(0, 1fr); }.create-panel { max-width: none; } }
@media (max-width: 600px) { .page { padding: 20px 16px; }.section-head { padding: 17px 16px; }.task-list { padding: 0 16px; }.form { padding: 16px; }.task-bottom strong { display: block; margin: 4px 0 0; }.section-head p { max-width: 190px; line-height: 1.6; } }
</style>
