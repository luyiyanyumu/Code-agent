<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'
import PageHeader from '../components/ui/PageHeader.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import UiIcon from '../components/ui/UiIcon.vue'

const props = defineProps({ user: { type: Object, required: true } })

const entries = ref([])
const verdict = ref(null)
const error = ref('')
const loading = ref(false)
const query = ref('')
const outcome = ref('all')
const visibleEntries = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return entries.value.filter(entry => (outcome.value === 'all' || outcomeClass(entry.outcome) === outcome.value) && (!needle || [entry.seq, entry.user_id, entry.tool_name, entry.arguments, entry.outcome].some(value => String(value ?? '').toLowerCase().includes(needle))))
})

const isAdmin = computed(() => props.user?.role === 'admin')

async function load() {
  if (!isAdmin.value || loading.value) return
  loading.value = true
  error.value = ''
  try {
    const [list, verify] = await Promise.all([api.audit(200), api.auditVerify()])
    entries.value = list
    verdict.value = verify
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function fmt(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return isNaN(d.getTime()) ? ts : d.toLocaleString('zh-CN', { hour12: false })
}

function outcomeClass(o) {
  if (o === 'ok') return 'ok'
  if (o?.startsWith('error')) return 'err'
  return 'warn'
}

onMounted(load)
defineExpose({ reload: load })
</script>

<template>
  <div class="page audit-page">
    <div class="page-content">
      <PageHeader title="审计日志" description="追踪工具操作与执行结果，校验操作记录的完整性。" eyebrow="工作区管理" icon="shield">
        <template #actions><button v-if="isAdmin" :disabled="loading" @click="load"><UiIcon name="refresh" :size="15" />{{ loading ? '校验中…' : '重新校验' }}</button></template>
      </PageHeader>
      <section v-if="!isAdmin" class="panel"><EmptyState title="需要管理员权限" :description="`审计链仅管理员可见。当前账号角色：${user.role}`" icon="shield" /></section>
      <template v-else>
        <div v-if="error" class="feedback error" role="alert"><UiIcon name="alert" :size="17" /><span>{{ error }}</span><button :disabled="loading" @click="load">重试</button></div>
        <div v-if="verdict" class="verdict" :class="verdict.ok ? 'good' : 'bad'" role="status">
          <div class="verdict-icon"><UiIcon :name="verdict.ok ? 'shield' : 'alert'" :size="21" /></div>
          <div><strong>{{ verdict.ok ? '哈希链完整' : '审计链校验异常' }}</strong><p>{{ verdict.ok ? '已完成完整性校验，未检测到记录篡改。' : `链在 seq=${verdict.broken_at} 处断裂，可能存在篡改或删除。` }}</p></div>
          <span class="record-count">{{ entries.length }} 条已载入记录</span>
        </div>
        <section class="panel">
          <div class="section-head"><div><h3>操作记录</h3><p>显示最近 200 条审计记录</p></div><span class="count">{{ visibleEntries.length }} 条结果</span></div>
          <div class="filters"><div class="search-input"><UiIcon name="search" :size="16" /><input v-model="query" type="search" aria-label="搜索审计记录" placeholder="搜索工具、用户或参数" /></div><select v-model="outcome" aria-label="筛选执行结果"><option value="all">全部结果</option><option value="ok">执行成功</option><option value="err">执行失败</option><option value="warn">其他结果</option></select></div>
          <div v-if="loading && !entries.length" class="loading-state" role="status">正在加载记录并校验哈希链…</div>
          <div v-else-if="visibleEntries.length" class="table-scroll" tabindex="0" aria-label="审计记录表格">
            <table class="table"><thead><tr><th>序号</th><th>操作时间</th><th>用户</th><th>工具</th><th>参数</th><th>执行结果</th><th>记录哈希</th></tr></thead>
              <tbody><tr v-for="e in visibleEntries" :key="e.seq"><td class="mono sequence">{{ e.seq }}</td><td class="timestamp">{{ fmt(e.ts) }}</td><td class="mono user-id" :title="e.user_id">{{ e.user_id }}</td><td><code class="tool-name">{{ e.tool_name }}</code></td><td class="mono args" :title="e.arguments">{{ e.arguments }}</td><td><span class="badge" :class="outcomeClass(e.outcome)">{{ e.outcome }}</span></td><td class="mono hash" :title="e.entry_hash">{{ (e.entry_hash || '').slice(0, 10) }}…</td></tr></tbody>
            </table>
          </div>
          <EmptyState v-else :title="entries.length ? '没有匹配的记录' : '暂无审计记录'" :description="entries.length ? '尝试其他关键词，或切换执行结果筛选。' : '工具执行后，对应的操作记录会显示在这里。'" icon="shield" />
          <div class="panel-footer"><UiIcon name="shield" :size="14" /><span>记录按哈希链关联，便于追踪与验证。</span></div>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.page { min-width: 0; overflow-y: auto; padding: 28px; background: var(--bg, #f4f6f9); }.page-content { max-width: 1440px; margin: 0 auto; }button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; }
.feedback { display: flex; align-items: center; gap: 10px; padding: 12px 14px; margin-bottom: 18px; font-size: 13px; border-radius: 8px; border: 1px solid #f4cfd5; }.feedback span { flex: 1; overflow-wrap: anywhere; }.feedback.error { background: #fff1f2; color: #b42336; }
.verdict { display: flex; align-items: center; gap: 13px; padding: 20px; border: 1px solid; border-radius: 10px; margin-bottom: 22px; }.verdict.good { background: #f0faf5; border-color: #d2eadc; color: #18734a; }.verdict.bad { background: #fff1f2; border-color: #f4cfd5; color: #b42336; }.verdict-icon { display: grid; place-items: center; width: 38px; height: 38px; background: #ffffffa8; border-radius: 10px; flex: none; }.verdict strong { font-size: 14px; }.verdict p { font-size: 12px; line-height: 1.6; margin: 5px 0 0; }.record-count { margin-left: auto; font-size: 12px; white-space: nowrap; }
.panel { min-width: 0; border: 1px solid var(--border); border-radius: 12px; background: var(--panel, #fff); overflow: hidden; }.section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 20px 22px 16px; }.section-head h3 { margin: 0 0 5px; font-size: 15px; }.section-head p,.count { margin: 0; font-size: 12px; color: var(--muted); }.filters { padding: 0 22px 20px; display: flex; align-items: center; gap: 10px; }.search-input { position: relative; flex: 1; max-width: 360px; min-width: 0; }.search-input svg { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); color: #8894a7; }.search-input input { width: 100%; min-width: 0; padding-left: 34px; font-size: 13px; }.filters select { flex: none; font-size: 13px; }
.table-scroll { width: 100%; overflow: auto; }.table { width: 100%; min-width: 920px; border-collapse: collapse; }.table th,.table td { padding: 14px 16px; text-align: left; border-bottom: 1px solid var(--border); font-size: 12px; }.table th { background: #f8f9fc; color: var(--muted); font-weight: 500; white-space: nowrap; }.table tr:last-child td { border: 0; }.table tbody tr:hover { background: #fafbfe; }.sequence { color: #8a94a7; }.timestamp { white-space: nowrap; color: #576175; }.user-id { max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.tool-name { font-size: 12px; padding: 4px 7px; border-radius: 4px; background: #f3f5f9; white-space: nowrap; }.args { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }.hash { color: #8a94a7; }.badge { display: inline-block; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; padding: 4px 8px; border-radius: 5px; }.badge.ok { background: #eaf7f0; color: #18764d; }.badge.warn { background: #fff5e2; color: #a46612; }.badge.err { background: #fff0f0; color: #b42336; }
.panel-footer { display: flex; align-items: center; gap: 7px; padding: 13px 22px; border-top: 1px solid var(--border); color: var(--muted); font-size: 12px; }.loading-state { padding: 64px 20px; text-align: center; color: var(--muted); font-size: 13px; }
@media (max-width: 700px) { .record-count { display: none; }.verdict { align-items: flex-start; } }
@media (max-width: 600px) { .page { padding: 20px 16px; }.verdict { padding: 16px; }.section-head { padding: 17px 16px 14px; }.filters { padding: 0 16px 16px; flex-wrap: wrap; }.search-input { flex-basis: 100%; max-width: none; }.filters select { width: 100%; }.panel-footer { padding: 12px 16px; } }
</style>
