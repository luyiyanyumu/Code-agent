<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
import PageHeader from '../components/ui/PageHeader.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import UiIcon from '../components/ui/UiIcon.vue'

const usage = ref(null)
const error = ref('')
const loading = ref(false)
const updatedAt = ref('')

function fmt(n) { return (n ?? 0).toLocaleString('zh-CN') }
async function load() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    usage.value = await api.usage()
    updatedAt.value = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  } catch (e) { error.value = e.message }
  finally { loading.value = false }
}
onMounted(load)
defineExpose({ reload: load })
</script>

<template>
  <div class="page usage-page">
    <div class="page-content">
      <PageHeader title="用量概览" description="查看对话轮次与模型消耗，了解工作区的使用情况。" eyebrow="工作区管理" icon="chart">
        <template #actions><button :disabled="loading" @click="load"><UiIcon name="refresh" :size="15" />{{ loading ? '刷新中…' : '刷新数据' }}</button></template>
      </PageHeader>
      <div v-if="error" class="feedback error" role="alert"><UiIcon name="alert" :size="17" /><span>{{ error }}</span><button :disabled="loading" @click="load">重试</button></div>
      <div v-if="loading && !usage" class="loading-state" role="status">正在加载用量数据…</div>
      <template v-if="usage">
        <div class="stats">
          <article class="stat"><div class="stat-label">对话轮次 <UiIcon name="chat" :size="17" /></div><strong>{{ fmt(usage.prompts) }}</strong><span class="stat-caption">累计发起的对话请求</span></article>
          <article class="stat"><div class="stat-label">输入 Tokens <UiIcon name="arrow-down" :size="17" /></div><strong>{{ fmt(usage.input_tokens) }}</strong><span class="stat-caption">提示词与上下文</span></article>
          <article class="stat"><div class="stat-label">输出 Tokens <UiIcon name="arrow-up" :size="17" /></div><strong>{{ fmt(usage.output_tokens) }}</strong><span class="stat-caption">模型生成的内容</span></article>
          <article class="stat total"><div class="stat-label">合计 Tokens <UiIcon name="chart" :size="17" /></div><strong>{{ fmt((usage.input_tokens || 0) + (usage.output_tokens || 0)) }}</strong><span class="stat-caption">输入与输出总量</span></article>
        </div>
        <section class="data-panel">
          <div class="section-head"><div><h3>模型使用明细</h3><p>按模型汇总请求与 Token 消耗</p></div><span class="count">{{ (usage.by_model || []).length }} 个模型</span></div>
          <div v-if="(usage.by_model || []).length" class="table-scroll" tabindex="0" aria-label="模型使用明细">
            <table class="table"><thead><tr><th>模型</th><th class="numeric">对话轮次</th><th class="numeric">输入 Tokens</th><th class="numeric">输出 Tokens</th></tr></thead>
              <tbody><tr v-for="m in usage.by_model || []" :key="m.model || '(默认)'"><td><span class="model-name">{{ m.model || '(默认模型)' }}</span></td><td class="numeric">{{ fmt(m.prompts) }}</td><td class="numeric">{{ fmt(m.input_tokens) }}</td><td class="numeric">{{ fmt(m.output_tokens) }}</td></tr></tbody>
            </table>
          </div>
          <EmptyState v-else title="还没有用量记录" description="开始一次对话后，模型的使用情况会显示在这里。" icon="chart" />
          <div class="panel-footer"><span>统计包含当前可见的累计用量</span><span v-if="updatedAt">更新于 {{ updatedAt }}</span></div>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.page { min-width: 0; overflow-y: auto; padding: 28px; background: var(--bg, #f4f6f9); }
.page-content { width: 100%; max-width: 1440px; margin: 0 auto; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; }
.feedback { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 8px; margin-bottom: 18px; font-size: 13px; }
.feedback span { flex: 1; overflow-wrap: anywhere; }.feedback.error { background: var(--err-soft, #fff1f2); color: var(--err, #b42336); border: 1px solid #f4cfd5; }
.loading-state { padding: 64px 20px; text-align: center; color: var(--muted); }
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-bottom: 24px; }
.stat { min-width: 0; padding: 22px; border: 1px solid var(--border, #e3e7ef); border-radius: 12px; background: var(--panel, #fff); }
.stat-label { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; color: var(--muted); }
.stat-label svg { color: #8b96ab; flex-shrink: 0; }.stat strong { display: block; margin: 14px 0 7px; font-size: 30px; line-height: 1.2; font-weight: 650; letter-spacing: -.7px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.stat-caption { color: var(--muted); font-size: 12px; }.total { background: #eef2ff; border-color: #dce3ff; }.total strong, .total .stat-label svg { color: var(--accent, #355dfb); }
.data-panel { min-width: 0; background: var(--panel, #fff); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
.section-head { padding: 20px 22px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }.section-head h3 { margin: 0 0 5px; font-size: 15px; }.section-head p { margin: 0; color: var(--muted); font-size: 12px; }.count { color: var(--muted); font-size: 12px; white-space: nowrap; }
.table-scroll { width: 100%; overflow: auto; }.table { width: 100%; min-width: 560px; border-collapse: collapse; }.table th,.table td { padding: 15px 22px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }.table th { background: #f8f9fc; color: var(--muted); font-size: 12px; font-weight: 500; }.table .numeric { text-align: right; font-variant-numeric: tabular-nums; }.table tr:last-child td { border: 0; }.model-name { font-weight: 550; }.table tbody tr:hover { background: #fafbfe; }
.panel-footer { border-top: 1px solid var(--border); padding: 13px 22px; display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; color: var(--muted); font-size: 12px; }
@media (max-width: 1000px) { .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 600px) { .page { padding: 20px 16px; }.stats { gap: 10px; margin-bottom: 16px; }.stat { padding: 16px; }.stat strong { font-size: 25px; }.stat-caption { font-size: 11px; }.section-head { padding: 17px 16px; }.panel-footer { padding: 12px 16px; } }
</style>
