<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../api.js'
import UiIcon from '../ui/UiIcon.vue'
import { createRunPoller, outputLocations } from '../../lib/runtimeClient.js'
import { INTEGRATION_METHODS, candidateService, cloneIntegration, createIntegrationObserver, integrationActive, integrationDeadline, integrationForm, integrationPayload, integrationStatus, jsonObject, responseText, sameIntegrationForm } from '../../lib/integrationClient.js'
import { createIntegrationPreviewDocument } from '../../lib/integrationPreview.js'

const props = defineProps({ projectId: { type: String, default: '' }, visible: { type: Boolean, default: true } })
const emit = defineEmits(['open-file', 'error', 'expand'])
const tabs = [{ id: 'preview', label: '页面预览', icon: 'globe' }, { id: 'request', label: '接口调试', icon: 'code' }, { id: 'logs', label: '日志', icon: 'terminal' }, { id: 'config', label: '配置', icon: 'settings' }]
const kinds = [{ id: 'npm', label: 'npm script' }, { id: 'maven', label: 'Maven' }, { id: 'gradle', label: 'Gradle' }, { id: 'python', label: 'Python 文件' }, { id: 'node', label: 'Node.js 文件' }, { id: 'command', label: '自定义命令' }]
const roleNames = { frontend: '前端', backend: '后端' }
const activeTab = ref('config'), session = ref(null), form = ref(integrationForm({})), savedForm = ref(integrationForm({}))
const candidates = ref({ frontend: [], backend: [] }), selectedCandidate = ref({ frontend: '', backend: '' })
const loading = ref(false), saving = ref(false), loaded = ref(false), busy = ref(''), error = ref(''), connectionError = ref(''), notice = ref('')
const preview = ref(null), previewDocument = ref(''), previewLoading = ref(false), previewError = ref(''), previewReady = ref(false), frame = ref(null)
const requestMethod = ref('GET'), requestPath = ref('/'), requestHeaders = ref('{}'), requestBody = ref(''), requestTimeout = ref(15)
const requesting = ref(false), requestError = ref(''), response = ref(null), responsePane = ref('body'), history = ref([]), historyLoading = ref(false), selectedRequest = ref('')
const logs = ref([]), logRole = ref('all'), follow = ref(true), logsTruncated = ref(false), logsHost = ref(null), startButton = ref(null)
const tabElements = new Map(), drafts = new Map(), pendingReads = new Set(), cursors = new Map()
let generation = 0, operation = 0, disposed = false, configRequest = null, previewVersion = 0, requestVersion = 0, detailVersion = 0, responseVersion = 0
let channel = '', previewTimer = null, historyTimer = null, logOrder = 0, autoPreviewSession = ''
const roles = ['backend', 'frontend']
const runTargets = { backend: { id: '', observing: false }, frontend: { id: '', observing: false } }
const dirty = computed(() => loaded.value && !sameIntegrationForm(form.value, savedForm.value))
const active = computed(() => integrationActive(session.value))
const canRequest = computed(() => !!session.value?.id && !['stopped', 'failed', 'error'].includes(session.value.status))
const filteredLogs = computed(() => logs.value.filter(row => logRole.value === 'all' || row.role === logRole.value))
const locations = computed(() => outputLocations(filteredLogs.value.map(row => row.text).join('')))
const shownResponse = computed(() => {
  if (!response.value) return ''
  if (responsePane.value === 'headers') return responseText(response.value.headers || {})
  if (responsePane.value === 'request') return responseText({ method: response.value.method, path: response.value.path, headers: response.value.request_headers || {}, body: response.value.request_body || '' })
  return responseText(response.value.body ?? '')
})
const serviceState = role => session.value?.services?.[role] || null
const stateLabel = role => integrationStatus(serviceState(role)?.status)
const current = (mine, pid) => !disposed && generation === mine && props.projectId === pid
const message = error => error?.message || String(error || '操作失败')
function report(value, mine = generation, pid = props.projectId) { if (current(mine, pid)) { error.value = message(value); emit('error', error.value) } }
function clearPreview() {
  previewVersion++; channel = ''; preview.value = null; previewDocument.value = ''; previewReady.value = false; previewLoading.value = false; previewError.value = ''
  if (previewTimer != null) clearTimeout(previewTimer)
  previewTimer = null
}
function applySession(value) {
  const previous = session.value?.id
  session.value = value?.session ?? null
  if (previous && previous !== session.value?.id || ['stopped', 'failed'].includes(session.value?.status)) clearPreview()
  observeRuns()
}
async function read(action, timeout = 15000) {
  const controller = new AbortController(); pendingReads.add(controller)
  try { return await integrationDeadline(() => action({ signal: controller.signal }), { timeoutMs: timeout, signal: controller.signal }) }
  finally { controller.abort(); pendingReads.delete(controller) }
}
const observer = createIntegrationObserver({
  load: (pid, options) => integrationDeadline(() => api.code.integration(pid, options), { timeoutMs: 12000, signal: options.signal, message: '联调状态读取超时，可以刷新重试。' }),
  onState: value => { connectionError.value = ''; applySession(value) },
  onError: value => { connectionError.value = message(value) },
})
function appendOutput(role, id, text, event) {
  if (runTargets[role].id !== id) return
  cursors.set(id, event.seq)
  const nearBottom = !logsHost.value || logsHost.value.scrollTop + logsHost.value.clientHeight >= logsHost.value.scrollHeight - 50
  logs.value.push({ role, id, seq: event.seq, text, order: ++logOrder })
  let size = logs.value.reduce((sum, row) => sum + row.text.length, 0)
  while (logs.value.length > 2000 || size > 1_048_576) { const removed = logs.value.shift(); size -= removed.text.length; logsTruncated.value = true }
  if (follow.value && nearBottom) nextTick(() => { if (logsHost.value) logsHost.value.scrollTop = logsHost.value.scrollHeight })
}
const runPollers = Object.fromEntries(roles.map(role => [role, createRunPoller({
  load: (pid, id, seq) => api.code.run(pid, id, seq),
  onOutput: (text, event, identity) => { if (identity.projectId === props.projectId) appendOutput(role, identity.id, text, event) },
  onExit: (_code, event, identity) => { if (identity.projectId === props.projectId) cursors.set(identity.id, event.seq) },
  onGap: () => { logsTruncated.value = true },
  onError: value => { connectionError.value = `${roleNames[role]}日志：${message(value)}` },
})]))
function observeRuns() {
  for (const role of roles) {
    const id = serviceState(role)?.run_id || ''
    if (props.visible === false || !props.projectId) { runPollers[role].stop(); runTargets[role].observing = false; continue }
    if (runTargets[role].id !== id || id && !runTargets[role].observing) {
      runTargets[role] = { id, observing: !!id }
      runPollers[role].select(props.projectId, id, { afterSeq: cursors.get(id) || 0 })
    }
  }
}
async function loadConfig({ replaceDraft = false } = {}) {
  const mine = generation, pid = props.projectId
  if (!pid) return false
  if (configRequest?.generation === mine) return configRequest.promise
  loading.value = true
  const ticket = { generation: mine, promise: null }; configRequest = ticket
  ticket.promise = (async () => {
    try {
      const value = await read(options => api.code.integrationConfig(pid, options))
      if (!current(mine, pid)) return false
      const next = integrationForm(value.config || {})
      candidates.value = { frontend: value.candidates?.frontend || [], backend: value.candidates?.backend || [] }
      if (!loaded.value || replaceDraft) {
        const draft = drafts.get(pid)
        savedForm.value = cloneIntegration(next)
        form.value = !replaceDraft && draft ? cloneIntegration(draft) : cloneIntegration(next)
      }
      loaded.value = true
      return true
    } catch (value) { if (value?.name !== 'AbortError') report(value, mine, pid); return false }
    finally { if (current(mine, pid)) { loading.value = false; if (configRequest === ticket) configRequest = null } }
  })()
  return ticket.promise
}
async function save() {
  if (!props.projectId || saving.value || busy.value) return false
  const mine = generation, pid = props.projectId
  let payload
  try { payload = integrationPayload(form.value) } catch (value) { report(value); return false }
  const submitted = cloneIntegration(form.value)
  saving.value = true; error.value = ''; notice.value = ''
  try {
    const value = await read(options => api.code.saveIntegrationConfig(pid, payload, options))
    if (!current(mine, pid)) return false
    const next = integrationForm(value.config || payload)
    if (sameIntegrationForm(form.value, submitted)) form.value = cloneIntegration(next)
    savedForm.value = cloneIntegration(next); drafts.delete(pid)
    if (value.candidates) candidates.value = value.candidates
    notice.value = dirty.value ? '配置已保存，保存期间的新增修改仍保留在草稿中。' : active.value ? '配置已保存，整体重启后生效。' : '配置已保存，可以启动联调。'
    return true
  } catch (value) { if (value?.name !== 'AbortError') report(value, mine, pid); return false }
  finally { if (current(mine, pid)) saving.value = false }
}
async function action(kind, role = 'all') {
  if (!props.projectId || saving.value || busy.value && kind !== 'stop') return false
  if (kind === 'start' && (!loaded.value || dirty.value)) { activeTab.value = 'config'; notice.value = '请先保存联调配置，再启动服务。'; return false }
  if (kind === 'start') { try { integrationPayload(form.value) } catch (value) { activeTab.value = 'config'; report(value); return false } }
  const mine = generation, pid = props.projectId, token = ++operation
  busy.value = kind; error.value = ''; notice.value = ''
  try {
    const method = { start: 'startIntegration', stop: 'stopIntegration', restart: 'restartIntegration' }[kind]
    const value = await integrationDeadline(() => kind === 'start' ? api.code[method](pid) : api.code[method](pid, { role }), {
      timeoutMs: kind === 'stop' ? 30000 : (Number(form.value.startup_timeout) || 60) * 1000 + 15000,
      message: '操作结果尚未确认，请刷新联调状态；启动请求不会自动重复发送。',
    })
    if (!current(mine, pid) || token !== operation) return false
    applySession(value)
    if (kind === 'stop') notice.value = role === 'all' ? '已停止本次联调启动的服务；连接的已有服务保留。' : `${roleNames[role]}服务已停止。`
    if (kind === 'start' || kind === 'restart') { activeTab.value = 'preview'; if (session.value?.status === 'running') await loadPreview() }
    return true
  } catch (value) { if (current(mine, pid) && token === operation) report(value); return false }
  finally { if (current(mine, pid) && token === operation) { busy.value = ''; if (props.visible) observer.refresh() } }
}
function choose(role) {
  const item = (candidates.value[role] || []).find(candidate => candidate.id === selectedCandidate.value[role])
  if (item) form.value[role] = candidateService(item, form.value[role])
}
async function loadPreview() {
  if (!props.projectId || !session.value?.id || previewLoading.value) return
  const mine = generation, pid = props.projectId, version = ++previewVersion
  previewLoading.value = true; previewError.value = ''; previewReady.value = false
  try {
    const value = await read(options => api.code.integrationPreview(pid, undefined, options), 25000)
    if (!current(mine, pid) || version !== previewVersion) return
    channel = `integration-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
    preview.value = value; previewDocument.value = createIntegrationPreviewDocument(value, { channel })
    if (previewTimer != null) clearTimeout(previewTimer)
    previewTimer = setTimeout(() => { if (current(mine, pid) && version === previewVersion && !previewReady.value) previewError.value = '页面尚未完成加载，请查看前端日志或刷新预览。' }, 20000)
  } catch (value) { if (current(mine, pid) && version === previewVersion && value?.name !== 'AbortError') previewError.value = message(value) }
  finally { if (current(mine, pid) && version === previewVersion) previewLoading.value = false }
}
function receive(event) {
  const value = event.data
  if (!frame.value || event.source !== frame.value.contentWindow || !value || value.channel !== channel || !channel) return
  if (value.type === 'ready') { previewReady.value = true; if (previewTimer != null) clearTimeout(previewTimer); previewTimer = null }
  if (value.type === 'error') previewError.value = String(value.message || '页面运行失败，请查看日志。').slice(0, 1000)
  if (value.type === 'request' && activeTab.value === 'request' && props.visible && historyTimer == null) historyTimer = setTimeout(() => { historyTimer = null; loadHistory() }, 300)
}
async function loadHistory() {
  if (!props.projectId || historyLoading.value) return
  const mine = generation, pid = props.projectId
  historyLoading.value = true
  try { const value = await read(options => api.code.integrationRequests(pid, options)); if (current(mine, pid)) history.value = Array.isArray(value.requests) ? value.requests : [] }
  catch (value) { if (current(mine, pid) && value?.name !== 'AbortError') requestError.value = message(value) }
  finally { if (current(mine, pid)) historyLoading.value = false }
}
async function sendRequest() {
  if (!props.projectId || requesting.value || !canRequest.value) return
  const mine = generation, pid = props.projectId, version = ++requestVersion, responseTurn = ++responseVersion
  let headers
  try { headers = jsonObject(requestHeaders.value, '请求头') } catch (value) { requestError.value = message(value); return }
  const path = requestPath.value.trim()
  if (!path) { requestError.value = '请填写项目接口路径。'; return }
  const timeout = Number(requestTimeout.value)
  if (!Number.isFinite(timeout) || timeout < 0.1 || timeout > 30) { requestError.value = '请求等待时间需要在 0.1–30 秒之间。'; return }
  requesting.value = true; requestError.value = ''; selectedRequest.value = ''
  try {
    const value = await read(options => api.code.integrationRequest(pid, { method: requestMethod.value, path, headers,
      ...(!['GET', 'HEAD'].includes(requestMethod.value) ? { body: requestBody.value } : {}), timeout }, options), timeout * 1000 + 5000)
    if (!current(mine, pid) || version !== requestVersion) return
    if (responseTurn === responseVersion) { response.value = value; selectedRequest.value = value.id || ''; responsePane.value = 'body' }
    await loadHistory()
  } catch (value) { if (current(mine, pid) && version === requestVersion && value?.name !== 'AbortError') requestError.value = message(value) }
  finally { if (current(mine, pid) && version === requestVersion) requesting.value = false }
}
async function showRequest(item) {
  const mine = generation, pid = props.projectId, version = ++detailVersion, responseTurn = ++responseVersion
  selectedRequest.value = item.id
  try { const value = await read(options => api.code.integrationRequestDetail(pid, item.id, options)); if (current(mine, pid) && version === detailVersion && responseTurn === responseVersion) { response.value = value; responsePane.value = 'body' } }
  catch (value) { if (current(mine, pid) && version === detailVersion && value?.name !== 'AbortError') requestError.value = message(value) }
}
async function copyResponse() {
  try { await navigator.clipboard.writeText(shownResponse.value); notice.value = '已复制当前响应。' } catch { notice.value = '复制不可用，可以在响应区域选择文本复制。' }
}
const requestTime = value => { const number = Number(value); const date = new Date(Number.isFinite(number) ? number < 1e12 ? number * 1000 : number : value); return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour12: false }) }
function selectTab(id) { activeTab.value = id; if (id === 'request') loadHistory(); if (id === 'logs') nextTick(() => { if (follow.value && logsHost.value) logsHost.value.scrollTop = logsHost.value.scrollHeight }) }
function tabKey(event, index) {
  let next = index
  if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
  else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = tabs.length - 1
  else return
  event.preventDefault(); selectTab(tabs[next].id); nextTick(() => tabElements.get(tabs[next].id)?.focus())
}
function release() {
  operation++; configRequest = null; observer.stop()
  for (const controller of pendingReads) controller.abort()
  pendingReads.clear()
  for (const role of roles) { runPollers[role].stop(); runTargets[role] = { id: '', observing: false } }
  if (historyTimer != null) clearTimeout(historyTimer)
  historyTimer = null; clearPreview()
}
async function refresh() { await loadConfig(); if (props.visible) observer.refresh() }
function focus() { nextTick(() => startButton.value?.focus()) }
watch([() => props.projectId, () => props.visible, activeTab, () => session.value?.id, () => session.value?.status, previewDocument, previewLoading, previewError], ([pid, visible, tab, id, status, document, loading, error]) => {
  if (pid && visible && tab === 'preview' && id && status === 'running' && !document && !loading && !error && autoPreviewSession !== id) {
    autoPreviewSession = id
    loadPreview()
  }
})
watch(() => props.projectId, async (pid, previous) => {
  if (previous && loaded.value) { drafts.set(previous, cloneIntegration(form.value)); if (drafts.size > 20) drafts.delete(drafts.keys().next().value) }
  const mine = ++generation; release(); autoPreviewSession = ''; loaded.value = false; loading.value = false; saving.value = false; busy.value = ''
  session.value = null; form.value = integrationForm({}); savedForm.value = integrationForm({}); candidates.value = { frontend: [], backend: [] }; selectedCandidate.value = { frontend: '', backend: '' }
  error.value = connectionError.value = notice.value = ''; activeTab.value = 'config'; logs.value = []; cursors.clear(); logsTruncated.value = false
  history.value = []; historyLoading.value = false; response.value = null; requestError.value = ''; requesting.value = false; requestVersion++; detailVersion++; responseVersion++
  requestMethod.value = 'GET'; requestPath.value = '/'; requestHeaders.value = '{}'; requestBody.value = ''; requestTimeout.value = 15
  if (pid) { await loadConfig(); if (current(mine, pid) && props.visible) observer.select(pid) }
}, { immediate: true, flush: 'sync' })
watch(() => props.visible, value => {
  if (value === false) { observer.stop(); for (const role of roles) { runPollers[role].stop(); runTargets[role].observing = false } }
  else if (props.projectId) { observer.select(props.projectId); observeRuns(); if (activeTab.value === 'request') loadHistory() }
})
watch(requestMethod, value => { if (!['GET', 'HEAD'].includes(value) && requestHeaders.value.trim() === '{}') requestHeaders.value = '{"Content-Type":"application/json"}' })
window.addEventListener('message', receive)
onBeforeUnmount(() => { disposed = true; generation++; release(); window.removeEventListener('message', receive) })
defineExpose({ focus, refresh, start: () => action('start') })
</script>

<template>
  <section class="integration-panel" aria-label="前后端联调">
    <header class="integration-toolbar">
      <div class="service-statuses">
        <span v-for="role in ['backend', 'frontend']" :key="role" class="service-status" :class="serviceState(role)?.status" :title="serviceState(role)?.error || serviceState(role)?.url || ''"><i /><b>{{ roleNames[role] }}</b>{{ stateLabel(role) }}<small v-if="serviceState(role)?.mode === 'attach'">已有服务</small></span>
      </div>
      <div class="session-actions">
        <button ref="startButton" class="primary" :disabled="!projectId || !loaded || saving || !!busy || active" @click="action('start')"><UiIcon name="play" :size="13" />{{ busy === 'start' ? '启动中…' : '启动联调' }}</button>
        <button :disabled="!projectId || (!active && !busy) || busy === 'stop' || saving" @click="action('stop')"><UiIcon name="close" :size="13" />{{ busy === 'stop' ? '停止中…' : '停止' }}</button>
        <button :disabled="!projectId || !session || !!busy || saving" title="使用已保存配置整体重启" @click="action('restart')"><UiIcon name="refresh" :size="13" />{{ busy === 'restart' ? '重启中…' : '整体重启' }}</button>
        <button class="icon-only" aria-label="展开联调面板" title="展开联调面板" @click="emit('expand')"><UiIcon name="arrow-up" :size="14" /></button>
      </div>
    </header>
    <div v-if="error || connectionError || session?.error" class="integration-alert" role="alert"><UiIcon name="warning" :size="14" /><span>{{ error || connectionError || session.error }}</span><button v-if="error" class="icon-only" aria-label="关闭联调错误" @click="error = ''">×</button><button v-if="connectionError" @click="observer.refresh()">刷新状态</button></div>
    <div v-else-if="notice || session?.message" class="integration-notice" role="status">{{ notice || session.message }}</div>
    <nav class="integration-tabs" role="tablist" aria-label="联调工具">
      <button v-for="(tab, index) in tabs" :id="`integration-tab-${tab.id}`" :key="tab.id" :ref="element => element ? tabElements.set(tab.id, element) : tabElements.delete(tab.id)" role="tab" :aria-label="tab.label" :aria-description="tab.id === 'config' && dirty ? '有未保存配置' : undefined" :aria-selected="activeTab === tab.id" :tabindex="activeTab === tab.id ? 0 : -1" :aria-controls="`integration-pane-${tab.id}`" :class="{ selected: activeTab === tab.id }" @click="selectTab(tab.id)" @keydown="tabKey($event, index)"><UiIcon :name="tab.icon" :size="14" />{{ tab.label }}<span v-if="tab.id === 'config' && dirty" class="dirty-dot" aria-label="有未保存配置" /></button>
      <span class="tab-spacer" /><span v-if="session" class="session-phase">{{ integrationStatus(session.status) }}</span>
    </nav>
    <div v-if="!projectId" class="integration-empty"><UiIcon name="server" :size="28" /><strong>打开项目，开始前后端联调</strong><p>在同一处启动两个服务、预览页面和调试接口。</p></div>
    <template v-else>
      <div v-show="activeTab === 'preview'" id="integration-pane-preview" role="tabpanel" aria-labelledby="integration-tab-preview" class="preview-pane">
        <div class="preview-bar"><span :title="preview?.frontend_url || form.frontend_url">{{ preview?.frontend_url || form.frontend_url || '前端服务地址尚未配置' }}</span><small v-if="previewDocument">{{ previewReady ? '页面已连接' : '页面加载中' }}</small><button :disabled="!session?.id || previewLoading" @click="loadPreview"><UiIcon name="refresh" :size="13" />{{ previewLoading ? '载入中…' : '刷新预览' }}</button></div>
        <p v-if="previewError" class="preview-error" role="alert">{{ previewError }} <button @click="selectTab('logs')">查看日志</button></p>
        <iframe v-if="previewDocument" :key="channel" ref="frame" :srcdoc="previewDocument" title="前后端联调页面预览" sandbox="allow-scripts" referrerpolicy="no-referrer" />
        <div v-else class="integration-empty"><UiIcon name="globe" :size="30" /><strong>{{ previewLoading ? '正在连接页面…' : '预览真实项目页面' }}</strong><p>{{ active ? '服务就绪后点击刷新预览。' : '配置并启动前后端，或连接已有的本机服务。' }}</p><button v-if="!active" @click="selectTab('config')">配置服务</button></div>
        <div class="preview-footnote">页面使用独立临时存储；接口会话由联调代理保存。前端修改后点击刷新预览。</div>
      </div>
      <div v-show="activeTab === 'request'" id="integration-pane-request" role="tabpanel" aria-labelledby="integration-tab-request" class="request-pane">
        <div class="request-workspace">
          <div class="request-main">
            <form class="request-url" @submit.prevent="sendRequest"><select v-model="requestMethod" aria-label="请求方法"><option v-for="method in INTEGRATION_METHODS" :key="method">{{ method }}</option></select><label class="request-path"><span class="sr-label">接口路径</span><input v-model="requestPath" aria-label="接口路径" placeholder="/users · 相对后端地址" /></label><button class="primary" :disabled="requesting || !canRequest" :title="!canRequest ? '启动联调后发送请求' : '发送一次项目接口请求'">{{ requesting ? '发送中…' : '发送请求' }}</button></form>
            <div class="request-editors" @keydown.ctrl.enter.prevent="sendRequest">
              <label><span>请求头 · JSON</span><textarea v-model="requestHeaders" aria-label="请求头 JSON" spellcheck="false" rows="3" placeholder='{"Authorization":"Bearer …"}' /></label>
              <label v-if="!['GET', 'HEAD'].includes(requestMethod)"><span>请求体</span><textarea v-model="requestBody" aria-label="请求体" spellcheck="false" rows="3" placeholder='{"name":"示例"}' /></label>
              <label class="request-timeout"><span>等待 / 秒</span><input v-model.number="requestTimeout" type="number" min="0.1" max="30" step="0.1" aria-label="请求等待秒数" /></label>
            </div>
            <p v-if="requestError" class="request-error" role="alert">{{ requestError }}</p>
            <div class="response-heading"><strong>响应</strong><template v-if="response"><b class="http-status" :class="{ failed: (response.status_code ?? response.status) >= 400 || response.error }">{{ response.status_code ?? response.status ?? '失败' }}</b><span>{{ Number(response.duration_ms || 0).toFixed(0) }} ms</span><span v-if="response.truncated">内容已截断</span></template><span class="tab-spacer" /><button v-if="response" @click="copyResponse">复制响应</button></div>
            <p v-if="response?.error" class="request-error">{{ response.error }}</p>
            <div v-if="response" class="response-tabs"><button v-for="item in [{id:'body',label:'内容'},{id:'headers',label:'响应头'},{id:'request',label:'请求详情'}]" :key="item.id" :class="{ selected: responsePane === item.id }" @click="responsePane = item.id">{{ item.label }}</button></div>
            <pre v-if="response" class="response-text" tabindex="0" aria-label="接口响应内容">{{ shownResponse }}</pre><div v-else class="response-empty">发送请求后，在这里查看状态、耗时和响应内容。<span>Ctrl+Enter 发送</span></div>
          </div>
          <aside class="request-history" aria-label="接口请求历史"><div><strong>请求历史</strong><button :disabled="historyLoading" aria-label="刷新请求历史" @click="loadHistory"><UiIcon name="refresh" :size="13" /></button></div><p v-if="!history.length">{{ historyLoading ? '读取中…' : '还没有项目接口请求' }}</p><button v-for="item in history" :key="item.id" :class="{ selected: selectedRequest === item.id }" :title="item.path || item.url" @click="showRequest(item)"><span><b>{{ item.method }}</b><em :class="{ failed: item.error || item.status_code >= 400 }">{{ item.status_code || '失败' }}</em></span><code>{{ item.path || item.url }}</code><small>{{ requestTime(item.created_at) }} · {{ Number(item.duration_ms || 0).toFixed(0) }} ms</small></button></aside>
        </div>
      </div>
      <div v-show="activeTab === 'logs'" id="integration-pane-logs" role="tabpanel" aria-labelledby="integration-tab-logs" class="logs-pane">
        <div class="logs-toolbar"><label>来源 <select v-model="logRole" aria-label="日志来源"><option value="all">全部服务</option><option value="backend">后端</option><option value="frontend">前端</option></select></label><label><input v-model="follow" type="checkbox" />跟随输出</label><button @click="logs = []; logsTruncated = false">清空显示</button><span class="tab-spacer" /><template v-for="role in roles" :key="role"><button :disabled="!serviceState(role) || !!busy" @click="action('restart', role)">{{ roleNames[role] }}重启</button><button :disabled="!serviceState(role) || !!busy" @click="action('stop', role)">{{ roleNames[role] }}停止</button></template></div>
        <p v-if="logsTruncated" class="logs-warning">较早的输出已超出缓存，仅显示最近日志。</p>
        <div v-if="locations.length" class="log-locations"><span>定位代码</span><button v-for="point in locations" :key="`${point.path}:${point.line}`" @click="emit('open-file', point)">{{ point.path }}:{{ point.line }}</button></div>
        <div ref="logsHost" class="logs-output" tabindex="0" aria-label="前后端运行日志"><p v-if="!filteredLogs.length">{{ active ? '服务正在运行，等待输出…' : '启动联调后，在这里查看前后端日志。' }}</p><div v-for="row in filteredLogs" :key="`${row.role}:${row.id}:${row.seq}`" class="log-row"><span :class="row.role">[{{ roleNames[row.role] }}]</span><pre>{{ row.text }}</pre></div></div>
      </div>
      <div v-show="activeTab === 'config'" id="integration-pane-config" role="tabpanel" aria-labelledby="integration-tab-config" class="config-pane">
        <div class="config-heading"><div><strong>前后端服务配置</strong><span>{{ loading ? '正在读取项目配置…' : dirty ? '有未保存修改' : '保存后启动，配置修改在整体重启后生效' }}</span></div><button :disabled="loading || saving || !!busy" @click="loadConfig({replaceDraft:true})">重新读取</button><button class="primary" :disabled="loading || saving || !!busy" @click="save">{{ saving ? '保存中…' : '保存配置' }}</button></div>
        <div class="config-scroll"><div class="config-general"><label>配置名称<input v-model="form.name" aria-label="联调配置名称" /></label><label>启动等待 / 秒<input v-model.number="form.startup_timeout" type="number" min="1" max="300" aria-label="启动等待秒数" /></label></div>
          <div class="service-configs"><section v-for="role in ['frontend', 'backend']" :key="role" :aria-label="`${roleNames[role]}服务配置`"><h3><UiIcon :name="role === 'frontend' ? 'globe' : 'server'" :size="15" />{{ roleNames[role] }}服务</h3><label>运行方式<select v-model="form[role].mode" :aria-label="`${roleNames[role]}运行方式`"><option value="launch">启动项目服务</option><option value="attach">连接已有服务</option></select></label><template v-if="form[role].mode === 'launch'"><label>发现的运行配置<select v-model="selectedCandidate[role]" :aria-label="`${roleNames[role]}运行配置`" @change="choose(role)"><option value="">自定义 / 当前配置</option><option v-for="item in candidates[role] || []" :key="item.id" :value="item.id" :disabled="item.available === false">{{ item.name || item.script || item.path || item.kind }}{{ item.available === false ? ' · 环境未就绪' : '' }}</option></select></label><label>运行类型<select v-model="form[role].kind" :aria-label="`${roleNames[role]}运行类型`"><option v-for="item in kinds" :key="item.id" :value="item.id">{{ item.label }}</option></select></label><label>工作目录<input v-model="form[role].cwd" :aria-label="`${roleNames[role]}工作目录`" placeholder="相对项目目录，例如 frontend" /></label><label v-if="['npm','maven','gradle'].includes(form[role].kind)">脚本 / 构建目标<input v-model="form[role].script" :aria-label="`${roleNames[role]}运行脚本`" placeholder="dev / spring-boot:run / bootRun" /></label><label v-else-if="form[role].kind === 'command'">启动命令<input v-model="form[role].command" :aria-label="`${roleNames[role]}启动命令`" placeholder="在工作目录中执行的命令" /></label><label v-else>入口文件<input v-model="form[role].path" :aria-label="`${roleNames[role]}入口文件`" placeholder="项目内相对路径" /></label></template><label>服务地址<input v-model="form[`${role}_url`]" :aria-label="`${roleNames[role]}服务地址`" :placeholder="role === 'frontend' ? 'http://127.0.0.1:5173/' : 'http://127.0.0.1:8080/'" /></label><label v-if="role === 'backend'">健康检查地址（可选）<input v-model="form.health_url" aria-label="后端健康检查地址" placeholder="http://127.0.0.1:8080/health" /></label><details v-if="form[role].mode === 'launch'"><summary>参数与环境变量</summary><label>参数 · JSON 数组<textarea v-model="form[role].args_text" :aria-label="`${roleNames[role]}启动参数 JSON`" rows="2" spellcheck="false" /></label><label>环境变量 · JSON 对象<textarea v-model="form[role].env_text" :aria-label="`${roleNames[role]}环境变量 JSON`" rows="3" spellcheck="false" /></label></details><p v-else class="attach-hint">停止联调时保留已有服务。</p></section></div>
          <div class="proxy-config"><label>前端接口前缀<input v-model="form.api_prefix" aria-label="前端接口前缀" placeholder="/api" /></label><label class="checkbox"><input v-model="form.strip_api_prefix" type="checkbox" />转发到后端时去掉此前缀</label></div><p class="config-footnote">先等待后端就绪，再启动前端。使用项目现有环境；页面预览支持接口写入，请使用开发数据。</p>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.integration-panel{height:100%;min-height:0;min-width:0;display:flex;flex:1;flex-direction:column;background:var(--panel);color:var(--text);font-size:var(--fs-xs,12px);overflow:hidden}
.integration-panel button,.integration-panel input,.integration-panel select,.integration-panel textarea{font-size:inherit;min-width:0;box-sizing:border-box}.integration-panel button{min-height:27px;padding:4px 8px;display:inline-flex;align-items:center;justify-content:center;gap:5px}.integration-panel input,.integration-panel select,.integration-panel textarea{border:1px solid var(--border-strong);border-radius:4px;background:var(--panel);color:var(--text);padding:5px 7px}.integration-panel :is(button,input,select,textarea,summary,pre,[tabindex]):focus-visible{outline:2px solid var(--accent);outline-offset:2px}.integration-panel button:disabled{cursor:not-allowed}.integration-panel .icon-only{padding:3px 6px;min-width:27px}.integration-toolbar{display:flex;align-items:center;flex-wrap:wrap;justify-content:space-between;gap:8px;padding:7px 10px;border-bottom:1px solid var(--border);flex:none}.service-statuses,.session-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.service-status{display:inline-flex;align-items:center;gap:5px;white-space:nowrap;color:var(--muted)}.service-status b{color:var(--text);font-weight:550}.service-status i{width:6px;height:6px;border-radius:50%;background:var(--faint)}.service-status.ready i,.service-status.running i{background:var(--ok)}.service-status.starting i,.service-status.pending i{background:var(--warn)}.service-status.failed i,.service-status.unhealthy i{background:var(--err)}.service-status small{font-size:10px;padding:1px 4px;background:var(--panel-soft);border-radius:3px}.integration-alert{display:flex;align-items:center;gap:7px;padding:5px 10px;background:var(--err-soft);color:var(--err);flex:none;max-height:72px;overflow:auto}.integration-alert>span{flex:1;overflow-wrap:anywhere}.integration-notice{padding:4px 10px;color:var(--muted);border-bottom:1px solid var(--border);flex:none;overflow-wrap:anywhere;max-height:50px;overflow:auto}.integration-tabs{display:flex;align-items:center;gap:1px;padding:0 8px;flex:none;border-bottom:1px solid var(--border)}.integration-tabs>button{border:0;border-radius:0;border-bottom:2px solid transparent;background:transparent;min-height:32px;color:var(--muted);padding:6px 10px}.integration-tabs>button.selected{color:var(--accent);border-bottom-color:var(--accent);font-weight:550}.tab-spacer{flex:1}.dirty-dot{width:5px;height:5px;background:var(--warn);border-radius:50%}.session-phase{color:var(--muted);font-size:11px;white-space:nowrap}.integration-empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;padding:24px;text-align:center;min-height:80px;color:var(--muted)}.integration-empty strong{color:var(--text);font-size:13px}.integration-empty p{margin:0;line-height:1.7;max-width:420px}.integration-empty>.ui-icon{color:var(--accent)}.preview-pane,.request-pane,.logs-pane,.config-pane{flex:1;min-height:0;min-width:0;display:flex;flex-direction:column;overflow:hidden}.preview-bar{display:flex;align-items:center;gap:8px;padding:5px 10px;flex:none;background:var(--panel-soft);border-bottom:1px solid var(--border)}.preview-bar>span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--font-mono,Consolas,monospace)}.preview-bar small{color:var(--muted);white-space:nowrap}.preview-pane iframe{border:0;width:100%;min-height:80px;flex:1;background:white}.preview-footnote{padding:4px 10px;font-size:11px;color:var(--muted);flex:none;border-top:1px solid var(--border)}.preview-error,.request-error{color:var(--err);background:var(--err-soft);padding:5px 8px;margin:0;line-height:1.6;overflow-wrap:anywhere;flex:none}.request-workspace{flex:1;display:flex;min-height:0;min-width:0}.request-main{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column}.request-url{display:flex;gap:6px;padding:7px 9px;flex:none}.request-url>select{width:84px}.request-path{flex:1;min-width:0}.request-path input{width:100%;height:29px}.sr-label{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}.request-editors{display:flex;gap:8px;padding:0 9px 6px;flex:none}.request-editors label{display:flex;flex:1;min-width:0;flex-direction:column;gap:4px;color:var(--muted);font-size:11px}.request-editors textarea{resize:vertical;min-height:45px;max-height:130px;font-family:var(--font-mono,Consolas,monospace);font-size:11px}.request-editors .request-timeout{flex:none;width:70px}.response-heading{display:flex;align-items:center;gap:8px;min-height:31px;padding:3px 9px;border-top:1px solid var(--border);flex:none;color:var(--muted)}.response-heading strong{color:var(--text)}.http-status{color:var(--ok)}.http-status.failed,.request-history em.failed{color:var(--err)}.response-tabs{display:flex;gap:4px;padding:0 9px;border-bottom:1px solid var(--border);flex:none}.response-tabs button{border:0;border-radius:0;background:transparent;color:var(--muted);min-height:25px}.response-tabs button.selected{color:var(--accent);background:var(--accent-soft)}.response-text{margin:0;padding:9px;flex:1;min-height:40px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;font-size:11px;font-family:var(--font-mono,Consolas,monospace);background:var(--panel-soft)}.response-empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--muted);padding:14px;text-align:center;line-height:1.7}.response-empty span{font-size:11px}.request-history{width:182px;flex:none;overflow:auto;border-left:1px solid var(--border);background:var(--panel-soft)}.request-history>div{display:flex;align-items:center;justify-content:space-between;padding:6px 8px;border-bottom:1px solid var(--border)}.request-history>div button{border:0;background:transparent}.request-history>p{padding:10px 8px;color:var(--muted);font-size:11px;line-height:1.7}.request-history>button{display:flex;flex-direction:column;align-items:stretch;justify-content:flex-start;gap:4px;width:100%;border:0;border-bottom:1px solid var(--border);border-radius:0;background:transparent;text-align:left;padding:8px}.request-history>button.selected{background:var(--accent-soft);box-shadow:inset 2px 0 var(--accent)}.request-history>button>span{display:flex;justify-content:space-between;gap:6px}.request-history b{font-weight:550;font-size:11px}.request-history em{font-style:normal;color:var(--ok);font-size:11px}.request-history code{font-size:11px;overflow-wrap:anywhere;white-space:normal}.request-history small{font-size:10px;color:var(--muted)}.logs-toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:7px;padding:6px 9px;border-bottom:1px solid var(--border);flex:none}.logs-toolbar label{display:inline-flex;align-items:center;gap:5px;color:var(--muted)}.logs-toolbar select{padding:3px 5px}.logs-toolbar button{font-size:11px}.logs-warning{margin:0;padding:4px 9px;background:var(--warn-soft);color:var(--warn);font-size:11px}.logs-output{flex:1;overflow:auto;min-height:50px;background:var(--panel-soft);padding:8px 9px;font-family:var(--font-mono,Consolas,monospace);font-size:11px}.logs-output>p{color:var(--muted)}.log-row{display:flex;gap:8px;align-items:baseline;line-height:1.7}.log-row>span{flex:none;user-select:none;color:var(--muted)}.log-row>span.frontend{color:var(--accent)}.log-row>pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;min-width:0;font:inherit}.log-locations{display:flex;align-items:center;gap:5px;flex-wrap:wrap;padding:4px 9px;max-height:50px;overflow:auto;flex:none;color:var(--muted)}.log-locations button{font-size:11px;padding:2px 5px;min-height:23px;max-width:100%;overflow-wrap:anywhere}.config-heading{display:flex;align-items:center;gap:7px;padding:7px 10px;border-bottom:1px solid var(--border);flex:none}.config-heading>div{display:flex;flex:1;flex-direction:column;gap:3px;min-width:0}.config-heading strong{font-weight:550}.config-heading span{font-size:11px;color:var(--muted)}.config-scroll{flex:1;min-height:0;overflow:auto;padding:10px}.config-general{display:grid;grid-template-columns:minmax(0,1fr) 130px;gap:10px;margin-bottom:12px}.config-pane label{display:flex;flex-direction:column;gap:5px;min-width:0;color:var(--text-2);font-size:11px}.service-configs{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px}.service-configs>section{min-width:0;display:flex;flex-direction:column;gap:8px}.service-configs h3{display:flex;align-items:center;gap:6px;margin:0 0 3px;font-size:12px;font-weight:550;color:var(--text)}.service-configs>section+section{border-left:1px solid var(--border);padding-left:18px}.service-configs details{font-size:11px}.service-configs summary{color:var(--muted);cursor:pointer;margin:2px 0 7px}.service-configs details label{margin:7px 0}.service-configs textarea{resize:vertical;font-family:var(--font-mono,Consolas,monospace);font-size:11px;max-height:180px}.attach-hint{margin:0;color:var(--muted);font-size:11px}.proxy-config{display:flex;align-items:end;gap:15px;margin-top:15px;padding-top:12px;border-top:1px solid var(--border)}.proxy-config>label:first-child{max-width:220px;flex:1}.config-pane .checkbox{display:flex;flex-direction:row;align-items:center;gap:6px;min-height:29px}.config-footnote{margin:9px 0 0;color:var(--muted);font-size:11px;line-height:1.7}
.integration-panel{container-type:inline-size;container-name:integration}
@container integration (max-width:760px){.integration-toolbar{padding:5px 7px;gap:6px}.service-statuses{gap:12px}.session-actions{gap:4px}.integration-tabs{padding:0 4px}.integration-tabs>button{padding:6px 8px}.request-history{width:145px}.request-editors{flex-wrap:wrap}.request-editors .request-timeout{width:60px}.config-heading{padding:6px 8px}.service-configs{gap:12px}.service-configs>section+section{padding-left:12px}.preview-footnote{font-size:10px}}
@container integration (max-width:500px){.integration-toolbar{align-items:stretch}.service-statuses,.session-actions{width:100%}.session-actions>button:not(.icon-only){flex:1}.session-actions .icon-only{margin-left:auto}.session-phase{display:none}.integration-tabs>button{flex:1;padding:6px 4px;white-space:nowrap}.request-workspace{flex-direction:column;overflow:auto}.request-main{flex:none;min-height:320px}.request-url{padding:6px;gap:4px}.request-url>select{width:76px}.request-url>button{padding:4px 6px}.request-editors{padding:0 6px 6px;gap:6px}.request-editors label{flex-basis:calc(100% - 74px)}.request-editors label:has(textarea[aria-label="请求体"]){flex-basis:100%;order:3}.request-history{width:auto;max-height:180px;border-left:0;border-top:1px solid var(--border);flex:none}.response-text{min-height:100px;max-height:260px;flex:none}.config-heading{flex-wrap:wrap}.config-heading>div{flex-basis:100%}.config-heading>button{flex:1}.service-configs{grid-template-columns:minmax(0,1fr);gap:14px}.service-configs>section+section{border-left:0;border-top:1px solid var(--border);padding:12px 0 0}.config-general{grid-template-columns:minmax(0,1fr) 115px}.proxy-config{align-items:stretch;flex-direction:column;gap:7px}.proxy-config>label:first-child{max-width:none}.logs-toolbar .tab-spacer{display:none}.logs-toolbar>button{padding:3px 5px}.preview-bar small{display:none}.preview-footnote{line-height:1.6}}
</style>
