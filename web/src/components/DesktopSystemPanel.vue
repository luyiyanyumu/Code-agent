<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import UiIcon from './ui/UiIcon.vue'
import AgentLogo from './ui/AgentLogo.vue'
import { desktopRuntime } from '../lib/desktopRuntime.js'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['close', 'status'])
const modal = ref(null), closeButton = ref(null), state = ref(null), error = ref(''), notice = ref('')
const loading = ref(false), actionBusy = ref(false), logBusy = ref(false), log = ref(null), logService = ref('backend')
let previousFocus = null, poll = null, disposed = false, logSequence = 0, statusFlight = null, statusCheckedAt = 0
const busy = computed(() => actionBusy.value || state.value?.operation?.state === 'running')
const services = computed(() => state.value?.services || [])
const healthy = computed(() => services.value.length > 0 && services.value.every(service => service.state === 'healthy'))
const statusNames = { healthy: '运行正常', stopped: '未运行', starting: '正在启动', stopping: '正在停止', unhealthy: '连接异常', foreign: '其他应用占用' }
const update = computed(() => state.value?.update)
const updateText = computed(() => {
  if (!update.value?.checkedAt) return '检查是否有可安装的新版本。'
  if (update.value.error) return update.value.error
  if (update.value.available) return `发现更新${update.value.latest ? ` ${update.value.latest}` : ''}。${update.value.message || '请使用新版安装包更新应用。'}`
  return update.value.message || '当前没有可用更新。'
})

function acceptStatus(value) { if (!disposed) { state.value = value; statusCheckedAt = Date.now(); emit('status', value) } }
async function refresh(showLoading = false) {
  if (statusFlight) return statusFlight
  if (showLoading) loading.value = true
  statusFlight = desktopRuntime.request('status').then(acceptStatus).catch(failure => { if (!disposed) error.value = failure.message }).finally(() => { loading.value = false; statusFlight = null })
  return statusFlight
}
async function readLogs() {
  const sequence = ++logSequence
  logBusy.value = true
  try { const result = await desktopRuntime.request('logs', { service: logService.value, lines: 200 }); if (!disposed && sequence === logSequence) log.value = result }
  catch (failure) { if (!disposed && sequence === logSequence) error.value = failure.message }
  finally { if (sequence === logSequence) logBusy.value = false }
}
async function act(action, target = 'all') {
  if (busy.value) return
  if (action === 'restart' && (target === 'frontend' || target === 'all') && !await desktopRuntime.prepareLeave('restart')) return
  actionBusy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await desktopRuntime.request('action', { action, target })
    if (result?.services) acceptStatus(result)
    notice.value = result?.operation?.message || (action === 'open' ? '已打开日志目录。' : '操作已完成。')
    await refresh()
    if (action === 'restart' && props.open) await readLogs()
  } catch (failure) { error.value = failure.message }
  finally { actionBusy.value = false }
}
async function openPanel() {
  previousFocus = document.activeElement
  await nextTick()
  if (!modal.value?.open) modal.value?.showModal()
  closeButton.value?.focus({ preventScroll: true })
  error.value = ''; notice.value = ''
  await Promise.all([refresh(true), readLogs()])
}
function closePanel() { emit('close') }
watch(() => props.open, open => {
  if (open) void openPanel()
  else { modal.value?.close(); if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true }); previousFocus = null }
})
watch(logService, () => { if (props.open) void readLogs() })
onMounted(() => {
  if (!desktopRuntime.available()) return
  void refresh()
  if (props.open) void openPanel()
  poll = setInterval(() => { if (document.visibilityState !== 'hidden' && (props.open || Date.now() - statusCheckedAt >= 15000)) void refresh() }, 3000)
})
onBeforeUnmount(() => { disposed = true; logSequence++; clearInterval(poll); modal.value?.close() })
</script>

<template>
  <Teleport to="body">
    <dialog ref="modal" class="desktop-system" aria-labelledby="desktop-system-title" @cancel.prevent="closePanel">
      <header class="desktop-system-header">
        <AgentLogo :wordmark="false" :size="30" />
        <div><h2 id="desktop-system-title">应用状态</h2><p>Code-agent <span v-if="state?.version">{{ state.version }}</span> · 桌面工作台</p></div>
        <button ref="closeButton" type="button" class="ghost close-system" aria-label="关闭应用状态" @click="closePanel"><UiIcon name="close" :size="18" /></button>
      </header>
      <div class="desktop-system-body">
        <div v-if="error" class="system-feedback failure" role="alert">{{ error }}</div>
        <div v-if="notice" class="system-feedback" role="status">{{ notice }}</div>
        <section aria-labelledby="desktop-runtime-title">
          <div class="system-section-heading"><h3 id="desktop-runtime-title">运行环境</h3><span v-if="loading" class="muted">正在连接…</span><span v-else-if="state" class="runtime-summary" :class="{ healthy }">{{ healthy ? '运行正常' : '需要检查' }}</span><button type="button" class="ghost" :disabled="loading" @click="error = ''; refresh(true)"><UiIcon name="refresh" :size="14" />刷新</button></div>
          <div class="runtime-services">
            <div v-for="service in services" :key="service.id" class="runtime-service">
              <span class="service-mark" :class="service.state"><UiIcon :name="service.id === 'backend' ? 'sparkles' : 'code'" :size="18" /></span>
              <div class="runtime-service-copy"><strong>{{ service.id === 'backend' ? '智能体服务' : '工作台界面' }}</strong><span :title="service.detail">{{ statusNames[service.state] || service.state }}</span></div>
              <button type="button" :disabled="busy || service.state === 'foreign'" @click="act(service.state === 'stopped' ? 'start' : 'restart', service.id)"><UiIcon name="refresh" :size="14" />{{ service.state === 'stopped' ? '启动' : '重启' }}</button>
            </div>
            <p v-if="!services.length && !loading" class="muted">尚未取得运行状态。可刷新重试或查看日志。</p>
          </div>
          <p v-if="busy" class="system-operation" role="status"><span class="system-spinner" />{{ state?.operation?.state === 'running' ? state.operation.message : '正在执行应用操作…' }}</p>
          <p v-if="state?.desktop?.reusedServices?.length" class="system-hint">已连接先前运行的服务，退出本窗口时会保留这些服务。</p>
        </section>
        <section aria-labelledby="desktop-update-title" class="system-update">
          <div><h3 id="desktop-update-title">软件更新</h3><p>{{ updateText }}</p><small v-if="update?.checkedAt">上次检查 {{ new Date(update.checkedAt).toLocaleString() }}</small></div>
          <button type="button" :disabled="busy" @click="act('check-update')"><UiIcon name="download" :size="15" />检查更新</button>
        </section>
        <section aria-labelledby="desktop-logs-title">
          <div class="system-section-heading"><h3 id="desktop-logs-title">运行日志</h3><button type="button" class="ghost" :disabled="busy" @click="act('open', 'logs')"><UiIcon name="folder-open" :size="14" />打开日志目录</button></div>
          <div class="system-log-toolbar"><label for="desktop-log-service">日志来源</label><select id="desktop-log-service" v-model="logService"><option value="backend">智能体服务</option><option value="frontend">工作台界面</option><option value="launcher">桌面应用</option><option value="update">软件更新</option></select><button type="button" class="ghost" :disabled="logBusy" @click="readLogs"><UiIcon name="refresh" :size="14" />{{ logBusy ? '读取中…' : '刷新日志' }}</button></div>
          <pre class="system-log" tabindex="0" aria-label="运行日志内容">{{ logBusy && !log ? '正在读取日志…' : (typeof log === 'string' ? log : log?.text || log?.content || '暂无日志内容。') }}</pre>
          <p class="system-hint">显示最近 200 行，完整内容可从日志目录查看。</p>
        </section>
      </div>
      <footer class="desktop-system-footer"><span>启动与运行环境由应用自动管理</span><button type="button" class="primary" @click="closePanel">返回工作台</button></footer>
    </dialog>
  </Teleport>
</template>

<style scoped>
.desktop-system { padding: 0; width: min(720px, calc(100vw - 32px)); max-height: min(840px, calc(100dvh - 40px)); border: 1px solid var(--border); border-radius: var(--r-lg); background: var(--panel); color: var(--text); box-shadow: var(--shadow-pop); overflow: hidden; }
.desktop-system[open] { display: flex; flex-direction: column; }
.desktop-system::backdrop { background: var(--overlay); }
.desktop-system-header { display: flex; align-items: center; gap: 12px; padding: 20px 24px; border-bottom: 1px solid var(--border); }
.desktop-system-header h2 { font-size: 17px; margin: 0; line-height: 1.4; }
.desktop-system-header p { color: var(--muted); font-size: 12px; margin: 2px 0 0; }
.close-system { margin-left: auto; width: 32px; height: 32px; padding: 0; display: grid; place-items: center; }
.desktop-system-body { overflow: auto; padding: 20px 24px; min-height: 0; display: grid; gap: 24px; }
.desktop-system h3 { font-size: 13px; font-weight: 650; margin: 0; }
.system-section-heading { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; min-height: 24px; }
.system-section-heading > button:last-child { margin-left: auto; }
.system-section-heading button, .system-log-toolbar button { min-height: 27px; font-size: 12px; padding: 3px 7px; }
.runtime-summary { color: var(--muted); font-size: 11px; }
.runtime-summary.healthy { color: var(--ok); }
.runtime-services { display: grid; gap: 8px; }
.runtime-service { display: flex; align-items: center; gap: 11px; padding: 11px 13px; border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--panel-soft); }
.service-mark { width: 34px; height: 34px; border-radius: 8px; display: grid; place-items: center; background: var(--hover); color: var(--muted); }
.service-mark.healthy { background: var(--ok-soft); color: var(--ok); }
.service-mark.unhealthy, .service-mark.foreign { color: var(--err); background: var(--err-soft); }
.runtime-service-copy { flex: 1; display: grid; gap: 2px; min-width: 0; }
.runtime-service-copy strong { font-size: 13px; font-weight: 550; }
.runtime-service-copy > span { color: var(--muted); font-size: 12px; }
.runtime-service > button { font-size: 12px; min-height: 29px; padding: 4px 9px; }
.system-update { display: flex; align-items: center; gap: 20px; padding-block: 18px; border-block: 1px solid var(--border); }
.system-update > div { flex: 1; min-width: 0; }
.system-update p { font-size: 12px; color: var(--text-2); margin: 5px 0 0; overflow-wrap: anywhere; }
.system-update small { color: var(--muted); display: block; margin-top: 5px; font-size: 11px; }
.system-update > button { flex-shrink: 0; font-size: 12px; }
.system-log-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.system-log-toolbar label { color: var(--muted); font-size: 12px; }
.system-log-toolbar select { width: 150px; height: 29px; padding-block: 3px; font-size: 12px; }
.system-log-toolbar button { margin-left: auto; }
.system-log { min-height: 160px; max-height: 240px; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--panel-soft); border: 1px solid var(--border); border-radius: var(--r-sm); color: var(--text-2); font: 11px/1.65 var(--font-mono, Consolas, monospace); }
.system-hint { font-size: 11px; line-height: 1.6; color: var(--muted); margin: 8px 0 0; }
.system-operation { display: flex; align-items: center; gap: 7px; color: var(--accent); font-size: 12px; margin: 10px 0 0; }
.system-spinner { width: 12px; height: 12px; border: 2px solid var(--accent-border); border-top-color: var(--accent); border-radius: 50%; animation: system-spin .7s linear infinite; }
.system-feedback { padding: 10px 12px; font-size: 12px; border-radius: 7px; background: var(--accent-soft); color: var(--text); overflow-wrap: anywhere; }
.system-feedback.failure { background: var(--err-soft); color: var(--err); }
.desktop-system-footer { border-top: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 12px; background: var(--panel-soft); }
.desktop-system-footer > span { flex: 1; font-size: 11px; color: var(--muted); }
.desktop-system-footer button { font-size: 12px; }
@keyframes system-spin { to { transform: rotate(360deg); } }
@media (max-width: 520px) { .desktop-system-header, .desktop-system-body { padding: 16px; } .desktop-system-footer { padding: 12px 16px; } .desktop-system-footer > span { display: none; } .desktop-system-footer > button { margin-left: auto; } .system-update { align-items: flex-start; gap: 12px; } }
@media (prefers-reduced-motion: reduce) { .system-spinner { animation: none; } }
</style>
