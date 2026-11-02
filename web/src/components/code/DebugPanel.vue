<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { debugAdapterMeta, debugClient, debugLanguageForPath, javaDebugOptions } from '../../lib/debugClient.js'

const props = defineProps({ projectId: String, file: [Object, String], breakpoints: { type: Array, default: () => [] }, visible: { type: Boolean, default: true } })
const emit = defineEmits(['open-file', 'breakpoints', 'error', 'location', 'start-request'])
const meta = ref(null), session = ref(null), error = ref(''), loading = ref(false), acting = ref(false)
const path = ref(''), argsText = ref('[]'), cwd = ref('')
const language = ref('auto'), javaRequest = ref('launch'), mainClass = ref(''), projectName = ref('')
const attachHost = ref('127.0.0.1'), attachPort = ref('5005'), stopOnEntry = ref(true)
const expanded = ref({}), variableLoading = ref({}), consoleHost = ref(null)
const expression = ref(''), evaluation = ref(null), evaluationError = ref(''), evaluating = ref(false), watches = ref([])
let evaluationEpoch = 0, watchSequence = 0
let generation = 0, timer = null, refreshing = false, disposed = false
let breakpointVersion = 0, updatingBreakpoints = false
let projectReady = Promise.resolve()
const currentPath = computed(() => typeof props.file === 'string' ? props.file : props.file?.path || '')
const active = computed(() => ['starting', 'running', 'paused'].includes(session.value?.status))
const paused = computed(() => session.value?.status === 'paused')
const running = computed(() => session.value?.status === 'running')
const selectedLanguage = computed(() => active.value ? session.value?.language || debugLanguageForPath(session.value?.path) || 'python'
  : language.value === 'auto' ? debugLanguageForPath(path.value.trim()) || debugLanguageForPath(currentPath.value) || 'python' : language.value)
const adapter = computed(() => debugAdapterMeta(meta.value, selectedLanguage.value))
const languageLabel = computed(() => selectedLanguage.value === 'java' ? 'Java' : 'Python')
const attach = computed(() => selectedLanguage.value === 'java' && javaRequest.value === 'attach')
const languageBreakpoints = computed(() => props.breakpoints.filter(item => debugLanguageForPath(item.path) === (active.value ? session.value?.language || debugLanguageForPath(session.value?.path) || selectedLanguage.value : selectedLanguage.value)))
const canStart = computed(() => Boolean(props.projectId && adapter.value.available && debugLanguageForPath(path.value.trim()) === selectedLanguage.value && !active.value && !loading.value && !acting.value))
const statusText = computed(() => acting.value && !active.value ? attach.value ? '连接中' : '启动中'
  : ({ starting: '启动中', running: '运行中', paused: '已暂停', stopped: '已结束', error: '调试失败' })[session.value?.status] || '未启动')
const reasonText = computed(() => ({ entry: '程序入口', breakpoint: '断点', step: '逐步', pause: '手动暂停', exception: '异常' })[session.value?.reason] || '')
const canEvaluate = computed(() => paused.value && Number.isInteger(session.value?.frame_id) && !acting.value && !loading.value)
function variableRows(items, parent, depth = 0) {
  const result = []
  function visit(values, depth, parent) {
    values.forEach((item, index) => {
      const key = `${parent}/${index}:${item.name}`
      result.push({ ...item, depth, key, open: Object.hasOwn(expanded.value, key) })
      if (Object.hasOwn(expanded.value, key)) visit(expanded.value[key], depth + 1, key)
    })
  }
  visit(items, depth, parent)
  return result
}
const rows = computed(() => variableRows(session.value?.variables || [], 'locals'))
const evaluationRows = computed(() => evaluation.value ? variableRows([{ ...evaluation.value, name: evaluation.value.expression, value: evaluation.value.result }], 'evaluation') : [])
const watchRows = computed(() => watches.value.flatMap(item => variableRows([{
  ...item.result, name: item.expression, value: item.result?.result ?? (item.loading ? '求值中…' : paused.value ? '等待求值' : '暂停后求值'),
  watchId: item.id, evaluationError: item.error, isWatch: true,
}], `watch:${item.id}`)))

function valid(seq, projectId = props.projectId) { return !disposed && generation === seq && props.projectId === projectId }
function report(exc) {
  error.value = exc?.message || String(exc)
  emit('error', error.value)
}
function applyState(next) {
  if (!next) return
  const previous = session.value
  if (previous?.id === next.id && Number(next.seq) < Number(previous.seq)) return
  const contextChanged = !previous || previous.id !== next.id || previous.pause_seq !== next.pause_seq || previous.frame_id !== next.frame_id || previous.status !== next.status
  if (contextChanged || next.status !== 'paused') {
    expanded.value = {}
    variableLoading.value = {}
  }
  if (contextChanged) invalidateEvaluation()
  const consoleRows = previous?.id === next.id ? previous.console || [] : []
  const existing = new Set(consoleRows.map(item => item.seq))
  next.console = [...consoleRows, ...(next.console || []).filter(item => !existing.has(item.seq))].slice(-500)
  session.value = next
  if (contextChanged && next.status === 'paused' && Number.isInteger(next.frame_id)) void refreshWatches()
  if (next.error) error.value = next.error
  if (next.console_seq !== previous?.console_seq) nextTick(() => { if (consoleHost.value) consoleHost.value.scrollTop = consoleHost.value.scrollHeight })
}
function schedule() {
  clearTimeout(timer)
  timer = null
  if (!disposed && props.visible !== false && active.value) timer = setTimeout(async () => { timer = null; await refresh(); schedule() }, running.value ? 450 : 800)
}
async function refresh({ frameId } = {}) {
  if (!props.projectId || !session.value?.id || (refreshing && frameId == null) || loading.value || acting.value) return
  const seq = generation, projectId = props.projectId, id = session.value.id
  refreshing = true
  try {
    const next = await debugClient.state(projectId, id, { frameId, afterConsole: session.value.console_seq || 0 })
    if (valid(seq, projectId) && session.value?.id === id) applyState(next)
  } catch (exc) { if (valid(seq, projectId)) report(exc) }
  finally { if (valid(seq, projectId)) refreshing = false }
}
async function loadProject() {
  const seq = ++generation, projectId = props.projectId
  let finishReady
  projectReady = new Promise(resolve => { finishReady = resolve })
  clearTimeout(timer)
  meta.value = null; session.value = null; expanded.value = {}; error.value = ''; loading.value = false; acting.value = false
  invalidateEvaluation(); watches.value = []; expression.value = ''
  refreshing = false; updatingBreakpoints = false; breakpointVersion++
  path.value = currentPath.value; cwd.value = ''; argsText.value = '[]'; language.value = 'auto'
  javaRequest.value = 'launch'; mainClass.value = ''; projectName.value = ''; attachHost.value = '127.0.0.1'; attachPort.value = '5005'; stopOnEntry.value = true
  if (!projectId) { finishReady(); return }
  loading.value = true
  try {
    const [capabilities, history] = await Promise.all([debugClient.meta(projectId), debugClient.sessions(projectId)])
    if (!valid(seq, projectId)) return
    meta.value = capabilities
    const existing = (history.sessions || []).find(item => ['starting', 'running', 'paused'].includes(item.status))
    if (existing) { path.value = existing.path; language.value = existing.language || debugLanguageForPath(existing.path) || 'auto'; javaRequest.value = existing.request || 'launch'; mainClass.value = existing.main_class || ''; applyState(existing) }
  } catch (exc) { if (valid(seq, projectId)) report(exc) }
  finally { if (valid(seq, projectId)) { loading.value = false; schedule() }; finishReady() }
}
async function start(options = {}) {
  if (!props.projectId || active.value || acting.value) return
  const seq = generation, projectId = props.projectId
  // F5 can mount this panel and immediately call start before its capability request finishes.
  await projectReady
  if (!valid(seq, projectId) || active.value || acting.value) return
  error.value = ''; acting.value = true
  try {
    if (!meta.value) meta.value = await debugClient.meta(projectId)
    if (!valid(seq, projectId)) return
    const payload = launchOptions(options)
    const capabilities = debugAdapterMeta(meta.value, payload.language || 'python')
    if (!capabilities.available) throw new Error(capabilities.install_hint || `${payload.language === 'java' ? 'Java' : 'Python'} 调试器不可用`)
    if (currentPath.value === payload.path && (props.file?.dirty || props.file?.isDirty) && payload.java?.request !== 'attach') throw new Error('文件有新的未保存更改，请保存后再启动调试')
    const next = await debugClient.start(projectId, { ...payload, breakpoints: props.breakpoints.filter(item => debugLanguageForPath(item.path) === (payload.language || 'python')) })
    if (valid(seq, projectId)) { path.value = payload.path; applyState(next) }
  } catch (exc) { if (valid(seq, projectId)) report(exc) }
  finally { if (valid(seq, projectId)) { acting.value = false; schedule(); if (active.value) syncBreakpoints() } }
}
function requestStart() {
  if (!canStart.value) return
  try {
    emit('start-request', launchOptions())
  } catch (exc) { report(exc) }
}
function launchOptions(options = {}) {
  const target = typeof options.path === 'string' ? options.path.trim() : path.value.trim() || currentPath.value
  const lang = options.language || (language.value === 'auto' ? debugLanguageForPath(target) || selectedLanguage.value : language.value)
  if (!['python', 'java'].includes(lang) || debugLanguageForPath(target) !== lang) {
    const unavailable = debugAdapterMeta(meta.value, lang)
    if (!unavailable.available && unavailable.install_hint) throw new Error(unavailable.install_hint)
    throw new Error('请选择与调试语言一致的项目内 .py 或 .java 文件')
  }
  const java = lang === 'java' ? javaDebugOptions(options.java || { request: javaRequest.value, main_class: mainClass.value, project_name: projectName.value, stop_on_entry: stopOnEntry.value, host: attachHost.value, port: attachPort.value }) : null
  const args = options.args ?? (java?.request === 'attach' ? [] : JSON.parse(argsText.value || '[]'))
  if (!Array.isArray(args) || args.length > 100 || args.some(arg => typeof arg !== 'string' || arg.length > 8000 || arg.includes('\0'))) throw new Error('参数请填写 JSON 字符串数组，例如 ["--port", "8000"]')
  return { path: target, args, cwd: options.cwd ?? cwd.value.trim(), ...(java ? { language: 'java', java } : {}) }
}
async function refreshEnvironment() {
  if (loading.value || acting.value) return
  const seq = generation, projectId = props.projectId
  if (!projectId) return
  loading.value = true; error.value = ''
  try { const data = await debugClient.meta(projectId); if (valid(seq, projectId)) meta.value = data }
  catch (exc) { if (valid(seq, projectId)) report(exc) }
  finally { if (valid(seq, projectId)) loading.value = false }
}
async function action(command) {
  if (!session.value?.id || acting.value) return
  const seq = generation, projectId = props.projectId, id = session.value.id
  acting.value = true; error.value = ''
  clearTimeout(timer)
  try {
    const next = await debugClient.action(projectId, id, command)
    if (valid(seq, projectId) && session.value?.id === id) applyState(next)
  } catch (exc) { if (valid(seq, projectId)) report(exc) }
  finally { if (valid(seq, projectId)) { acting.value = false; schedule() } }
}
async function stop() { return action('stop') }
async function selectFrame(frame) {
  if (frame.path) emit('open-file', { path: frame.path, line: frame.line, column: frame.column || 1 })
  if (session.value?.frame_id === frame.id) return
  await refresh({ frameId: frame.id })
}
function invalidateEvaluation() {
  evaluationEpoch++
  evaluation.value = null; evaluationError.value = ''; evaluating.value = false
  for (const item of watches.value) { item.result = null; item.error = ''; item.loading = false }
}
function evaluationContext() {
  if (!paused.value || !Number.isInteger(session.value?.frame_id)) return null
  const current = session.value
  return { epoch: evaluationEpoch, generation, projectId: props.projectId, id: current.id, frameId: current.frame_id, pauseSeq: current.pause_seq }
}
function evaluationCurrent(context) {
  return context && valid(context.generation, context.projectId) && context.epoch === evaluationEpoch && paused.value
    && session.value?.id === context.id && session.value.frame_id === context.frameId && session.value.pause_seq === context.pauseSeq
}
function expressionValue() {
  const value = expression.value.trim()
  if (!value || value.length > 8192 || value.includes('\0')) { evaluationError.value = '请输入不超过 8192 个字符的表达式'; return null }
  return value
}
async function evaluateExpression() {
  if (!canEvaluate.value || evaluating.value) return
  const value = expressionValue(), context = evaluationContext()
  if (!value || !context) return
  evaluating.value = true; evaluationError.value = ''; evaluation.value = null
  expanded.value = Object.fromEntries(Object.entries(expanded.value).filter(([key]) => !key.startsWith('evaluation/')))
  try {
    const result = await debugClient.evaluate(context.projectId, context.id, { expression: value, frame_id: context.frameId, pause_seq: context.pauseSeq, context: 'repl' })
    if (evaluationCurrent(context)) evaluation.value = result
  } catch (exc) { if (evaluationCurrent(context)) evaluationError.value = exc?.message || String(exc) }
  finally { if (evaluationCurrent(context)) evaluating.value = false }
}
async function evaluateWatch(item) {
  const context = evaluationContext()
  if (!context || item.loading) return
  item.loading = true; item.error = ''
  expanded.value = Object.fromEntries(Object.entries(expanded.value).filter(([key]) => !key.startsWith(`watch:${item.id}/`)))
  const current = () => evaluationCurrent(context) && watches.value.some(watch => watch.id === item.id)
  try {
    const result = await debugClient.evaluate(context.projectId, context.id, { expression: item.expression, frame_id: context.frameId, pause_seq: context.pauseSeq, context: 'watch' })
    if (current()) item.result = result
  } catch (exc) { if (current()) { item.result = null; item.error = exc?.message || String(exc) } }
  finally { if (current()) item.loading = false }
}
async function refreshWatches() {
  // A normal state poll must never repeat a watch that may execute user code.
  const context = evaluationContext()
  for (const item of [...watches.value]) {
    if (!evaluationCurrent(context)) return
    await evaluateWatch(item)
  }
}
async function addWatch() {
  const value = expressionValue()
  if (!value || !props.projectId) return
  if (watches.value.some(item => item.expression === value)) { evaluationError.value = '该表达式已在监视列表中'; return }
  if (watches.value.length >= 24) { evaluationError.value = '最多添加 24 个监视表达式'; return }
  evaluationError.value = ''
  const id = ++watchSequence
  watches.value.push({ id, expression: value, result: null, error: '', loading: false })
  await evaluateWatch(watches.value.find(item => item.id === id))
}
function removeWatch(id) {
  watches.value = watches.value.filter(item => item.id !== id)
  expanded.value = Object.fromEntries(Object.entries(expanded.value).filter(([key]) => !key.startsWith(`watch:${id}/`)))
}
async function toggleVariable(row) {
  if (!row.variablesReference || !paused.value || variableLoading.value[row.key]) return
  if (Object.hasOwn(expanded.value, row.key)) { const copy = { ...expanded.value }; delete copy[row.key]; expanded.value = copy; return }
  const seq = generation, projectId = props.projectId, id = session.value.id, pauseSeq = session.value.pause_seq, frameId = session.value.frame_id
  variableLoading.value = { ...variableLoading.value, [row.key]: true }
  try {
    const data = await debugClient.variables(projectId, id, row.variablesReference, pauseSeq)
    if (valid(seq, projectId) && session.value?.id === id && paused.value && session.value.pause_seq === pauseSeq && session.value.frame_id === frameId) expanded.value = { ...expanded.value, [row.key]: data.variables || [] }
  } catch (exc) { if (valid(seq, projectId) && session.value?.pause_seq === pauseSeq) report(exc) }
  finally { if (valid(seq, projectId)) { const copy = { ...variableLoading.value }; delete copy[row.key]; variableLoading.value = copy } }
}
async function syncBreakpoints() {
  breakpointVersion++
  if (updatingBreakpoints || !active.value || session.value?.status === 'starting') return
  const seq = generation, projectId = props.projectId, id = session.value.id
  updatingBreakpoints = true
  try {
    let version
    do {
      version = breakpointVersion
      const next = await debugClient.breakpoints(projectId, id, languageBreakpoints.value)
      if (!valid(seq, projectId) || session.value?.id !== id) return
      // A breakpoint response may predate a pause/continue response; only merge its own field.
      session.value = { ...session.value, seq: Math.max(session.value.seq || 0, next.seq || 0), breakpoints: next.breakpoints || [] }
    } while (version !== breakpointVersion && active.value)
  } catch (exc) { if (valid(seq, projectId)) report(exc) }
  finally { if (valid(seq, projectId)) updatingBreakpoints = false }
}
function removeBreakpoint(bp) { emit('breakpoints', props.breakpoints.filter(item => !(item.path === bp.path && item.line === (bp.requested_line || bp.line)))) }
watch(() => props.projectId, loadProject, { immediate: true })
watch(() => props.visible, visible => {
  clearTimeout(timer); timer = null
  if (visible !== false) void refresh().finally(schedule)
})
watch(currentPath, value => { if (!active.value) path.value = value })
watch(() => props.breakpoints, syncBreakpoints, { deep: true })
watch([
  () => session.value?.status,
  () => session.value?.frame_id,
  () => session.value?.stack?.find(frame => frame.id === session.value.frame_id)?.path,
  () => session.value?.stack?.find(frame => frame.id === session.value.frame_id)?.line,
], () => {
  const frame = paused.value ? session.value?.stack?.find(item => item.id === session.value.frame_id) : null
  emit('location', frame?.path ? { path: frame.path, line: frame.line, column: frame.column || 1 } : null)
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; generation++; clearTimeout(timer); emit('location', null) })
defineExpose({ start, stop, refresh, isPaused: () => paused.value, continue: () => action('continue') })
</script>

<template>
  <section class="debug-panel" :aria-label="`${languageLabel} 调试`">
    <header class="debug-toolbar">
      <span class="debug-title">调试</span>
      <select v-model="language" class="debug-language" aria-label="调试语言" :disabled="active || loading || acting"><option value="auto">自动 · {{ languageLabel }}</option><option value="python">Python</option><option value="java">Java</option></select>
      <div v-if="selectedLanguage === 'java' && !active" class="debug-mode" aria-label="Java 调试方式"><button :class="{ selected: javaRequest === 'launch' }" :disabled="loading || acting" @click="javaRequest = 'launch'">启动</button><button :class="{ selected: javaRequest === 'attach' }" :disabled="loading || acting" @click="javaRequest = 'attach'">附加</button></div>
      <span :class="['debug-status', session?.status]">{{ statusText }}{{ paused && reasonText ? ` · ${reasonText}` : '' }}</span>
      <div class="debug-actions">
        <button v-if="!active" :disabled="!canStart" :title="attach ? '连接本机正在运行的 Java 调试进程' : `保存文件后，启动 ${languageLabel} 调试`" @click="requestStart">{{ attach ? '▷ 连接' : '▶ 启动' }}</button>
        <button v-if="paused" :disabled="acting" title="继续" @click="action('continue')">▶ 继续</button>
        <button v-if="running" :disabled="acting" title="暂停" @click="action('pause')">Ⅱ 暂停</button>
        <button :disabled="!paused || acting" title="单步跳过" @click="action('next')">↷ 跳过</button>
        <button :disabled="!paused || acting" title="单步进入" @click="action('stepIn')">↓ 进入</button>
        <button :disabled="!paused || acting" title="单步跳出" @click="action('stepOut')">↑ 跳出</button>
        <button class="debug-stop" :disabled="!active || acting" :title="session?.request === 'attach' ? '断开调试连接，保留 Java 进程' : '停止调试进程'" @click="stop">{{ session?.request === 'attach' ? '■ 断开' : '■ 停止' }}</button>
      </div>
    </header>
    <div v-if="!active" class="debug-launch">
      <label class="debug-entry"><span>{{ attach ? '源码' : '入口' }}</span><input v-model="path" :disabled="loading || acting" :placeholder="selectedLanguage === 'java' ? '项目内 Java 文件，例如 src/Main.java' : '项目内 Python 文件，例如 main.py'" aria-label="调试入口" /></label>
      <template v-if="attach">
        <label class="debug-attach-host"><span>地址</span><select v-model="attachHost" :disabled="loading || acting" aria-label="Java 附加地址"><option value="127.0.0.1">127.0.0.1</option><option value="::1">::1</option></select></label>
        <label class="debug-attach-port"><span>端口</span><input v-model="attachPort" inputmode="numeric" :disabled="loading || acting" placeholder="5005" aria-label="Java 附加端口" /></label>
      </template>
      <template v-else>
        <label><span>参数</span><input v-model="argsText" :disabled="loading || acting" placeholder='["--port", "8000"]' aria-label="调试参数" /></label>
        <label><span>目录</span><input v-model="cwd" :disabled="loading || acting" placeholder="项目根目录" aria-label="调试工作目录" /></label>
      </template>
    </div>
    <div v-if="selectedLanguage === 'java' && !active && !attach" class="debug-java-options">
      <label><span>主类</span><input v-model="mainClass" :disabled="loading || acting" placeholder="自动识别，例如 demo.Main" aria-label="Java 主类" /></label>
      <label><span>项目</span><input v-model="projectName" :disabled="loading || acting" placeholder="自动识别" aria-label="Java 项目名" /></label>
      <label class="debug-entry-pause"><input v-model="stopOnEntry" type="checkbox" :disabled="loading || acting" />入口暂停</label>
    </div>
    <p v-if="loading" class="debug-note">正在检查调试环境…</p>
    <p v-else-if="meta && !adapter.available" class="debug-note debug-prerequisite">{{ adapter.install_hint || `${languageLabel} 调试器不可用` }} <button type="button" :disabled="acting" @click="refreshEnvironment">重新检测</button></p>
    <p v-else-if="!active && !session" class="debug-note">{{ selectedLanguage === 'java' ? attach ? '连接本机 JDWP 端口；断开后保留 Java 进程。' : '由 JDT 解析主类和类路径，请先构建工程。' : '保存后启动，在程序入口暂停。' }}</p>
    <p v-if="error" class="debug-error" role="alert">{{ error }}</p>
    <p v-if="selectedLanguage === 'python' && (session?.python || meta?.python)" class="debug-interpreter" :title="session?.python || meta?.python">{{ (session?.interpreter_source || meta?.interpreter_source) === 'project-venv' ? '项目 .venv' : '后端 Python' }} · {{ session?.python || meta?.python }}</p>
    <p v-if="selectedLanguage === 'java' && session?.main_class" class="debug-interpreter" :title="session.main_class">Java Debug · {{ session.main_class }}</p>
    <div class="debug-body">
      <div class="debug-inspect">
        <section class="debug-section debug-stack">
          <h4>调用栈 <span v-if="session?.thread_id">线程 {{ session.thread_id }}</span></h4>
          <button v-for="frame in session?.stack || []" :key="frame.id" :class="['debug-frame', { selected: frame.id === session.frame_id }]" :title="frame.path || frame.source_name" @click="selectFrame(frame)">
            <span>{{ frame.name }}</span><small>{{ frame.path || frame.source_name }}:{{ frame.line }}</small>
          </button>
          <p v-if="!session?.stack?.length" class="debug-empty">{{ running ? '运行中，暂停后查看调用栈' : '暂停后显示调用栈' }}</p>
        </section>
        <section class="debug-section debug-variables">
          <h4>局部变量</h4>
          <div v-for="row in rows" :key="row.key" class="debug-variable" :style="{ paddingLeft: `${8 + row.depth * 14}px` }">
            <button class="debug-expand" :disabled="!row.variablesReference || variableLoading[row.key]" :aria-label="`${row.open ? '收起' : '展开'}变量 ${row.name}`" @click="toggleVariable(row)">{{ row.variablesReference ? variableLoading[row.key] ? '…' : row.open ? '▾' : '▸' : '·' }}</button>
            <span class="debug-variable-name" :title="row.type">{{ row.name }}</span><code :title="row.value">{{ row.value }}</code>
          </div>
          <p v-if="!rows.length" class="debug-empty">{{ paused ? '当前帧没有局部变量' : '暂停后显示变量' }}</p>
          <details v-if="session?.scopes?.some(scope => scope.expensive || scope.name?.toLowerCase() === 'globals')" class="debug-global">
            <summary>其他作用域</summary>
            <button v-for="scope in session.scopes.filter(item => item.expensive || item.name?.toLowerCase() === 'globals')" :key="scope.variablesReference" @click="toggleVariable({ key: `scope:${scope.variablesReference}`, name: scope.name, variablesReference: scope.variablesReference })">{{ scope.name }}</button>
            <template v-for="scope in session.scopes" :key="scope.variablesReference"><div v-for="item in expanded[`scope:${scope.variablesReference}`] || []" :key="item.name" class="debug-variable"><span>{{ item.name }}</span><code>{{ item.value }}</code></div></template>
          </details>
        </section>
        <section class="debug-section debug-watches">
          <h4>监视表达式 <span>{{ watches.length }}/24</span><button type="button" :disabled="!canEvaluate || !watches.length || watches.some(item => item.loading)" title="重新计算所有监视表达式" @click="refreshWatches">刷新</button></h4>
          <div v-for="row in watchRows" :key="row.key" class="debug-variable debug-watch" :style="{ paddingLeft: `${8 + row.depth * 14}px` }">
            <button class="debug-expand" :disabled="!row.variablesReference || variableLoading[row.key]" :aria-label="`${row.open ? '收起' : '展开'}监视 ${row.name}`" @click="toggleVariable(row)">{{ row.variablesReference ? variableLoading[row.key] ? '…' : row.open ? '▾' : '▸' : '·' }}</button>
            <span class="debug-watch-name" :title="row.name">{{ row.name }}</span><code :class="{ 'evaluation-error': row.evaluationError }" :title="row.evaluationError || row.type || row.value">{{ row.evaluationError || row.value }}</code>
            <button v-if="row.isWatch" class="debug-watch-remove" type="button" :aria-label="`移除监视 ${row.name}`" @click="removeWatch(row.watchId)">×</button>
          </div>
          <p v-if="!watches.length" class="debug-empty">在右侧输入表达式，点击“添加监视”</p>
        </section>
        <section v-if="languageBreakpoints.length" class="debug-section debug-breakpoints">
          <h4>断点</h4>
          <div v-for="bp in languageBreakpoints" :key="`${bp.path}:${bp.line}`" class="debug-breakpoint">
            <button class="debug-location" :title="bp.path" @click="emit('open-file', { path: bp.path, line: bp.line, column: 1 })"><span :class="{ verified: session?.breakpoints?.some(item => item.path === bp.path && item.requested_line === bp.line && item.verified) }">●</span> {{ bp.path }}:{{ bp.line }}</button>
            <button title="移除断点" @click="removeBreakpoint(bp)">×</button>
          </div>
        </section>
      </div>
      <section class="debug-section debug-console">
        <h4>调试控制台 <span v-if="session?.exit_code != null">退出码 {{ session.exit_code }}</span></h4>
        <div class="debug-evaluator">
          <form class="debug-expression-form" @submit.prevent="evaluateExpression">
            <label for="debug-expression">表达式</label>
            <input id="debug-expression" v-model="expression" maxlength="8192" autocomplete="off" spellcheck="false" aria-label="调试表达式" :placeholder="paused ? '在当前栈帧求值，例如 number + 1' : '输入表达式，暂停后求值'" />
            <button type="submit" :disabled="!canEvaluate || evaluating || !expression.trim()">{{ evaluating ? '求值中…' : '求值' }}</button>
            <button type="button" :disabled="!props.projectId || !expression.trim() || watches.length >= 24" title="每次暂停或切换栈帧求值一次；函数调用可能改变程序状态" @click="addWatch">添加监视</button>
          </form>
          <p v-if="evaluationError" class="debug-evaluation-error" role="alert">{{ evaluationError }}</p>
          <div v-if="evaluationRows.length" class="debug-evaluation-result" aria-live="polite">
            <div v-for="row in evaluationRows" :key="row.key" class="debug-variable" :style="{ paddingLeft: `${8 + row.depth * 14}px` }">
              <button class="debug-expand" :disabled="!row.variablesReference || variableLoading[row.key]" :aria-label="`${row.open ? '收起' : '展开'}求值 ${row.name}`" @click="toggleVariable(row)">{{ row.variablesReference ? variableLoading[row.key] ? '…' : row.open ? '▾' : '▸' : '·' }}</button>
              <span class="debug-watch-name" :title="row.name">{{ row.name }}</span><code :title="row.value">{{ row.value }}</code><small v-if="row.type" class="debug-value-type">{{ row.type }}</small>
            </div>
          </div>
        </div>
        <div ref="consoleHost" class="debug-output" role="log" aria-live="off"><pre v-for="item in session?.console || []" :key="item.seq" :class="item.category">{{ item.output }}</pre><p v-if="!session?.console?.length" class="debug-empty">程序输出显示在这里</p></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.debug-panel { display: flex; flex-direction: column; height: 100%; min-height: 0; color: var(--text); font-size: 12px; overflow: hidden; }
.debug-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; min-height: 39px; flex: none; padding: 7px 12px; background: var(--panel-soft); border-bottom: 1px solid var(--border); }
.debug-title { font-weight: 650; }.debug-status { color: var(--muted); font-size: 11px; }.debug-status.paused { color: var(--warn); }.debug-status.running { color: var(--ok); }
.debug-language { padding: 3px 6px; min-height: 27px; max-width: 110px; font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 5px; }
.debug-mode { display: flex; padding: 2px; gap: 2px; border-radius: 5px; background: var(--panel); border: 1px solid var(--border); }.debug-mode button { padding: 2px 7px !important; }.debug-mode button.selected { color: var(--accent); background: var(--accent-soft, #eef3ff); }
.debug-actions { display: flex; gap: 4px; margin-left: auto; flex-wrap: wrap; }.debug-panel button { border: 1px solid transparent; background: transparent; color: inherit; border-radius: 4px; padding: 4px 6px; cursor: pointer; font: inherit; }.debug-panel button:hover:enabled { background: var(--hover); border-color: var(--border); }.debug-panel button:disabled { opacity: .38; cursor: default; }.debug-panel .debug-stop { color: var(--err); }
.debug-launch { display: flex; gap: 10px; flex-wrap: wrap; padding: 8px 12px; border-bottom: 1px solid var(--border); }.debug-launch label { display: flex; align-items: center; gap: 6px; flex: 1 1 180px; color: var(--muted); }.debug-launch input { width: 100%; min-width: 100px; color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 5px; min-height: 29px; padding: 5px 7px; font-size: 12px; }
.debug-note,.debug-error { margin: 0; padding: 8px 12px; line-height: 1.5; }.debug-note { color: var(--muted); }.debug-error { color: var(--err); background: var(--err-soft); }
.debug-interpreter { margin: 0; padding: 4px 12px; color: var(--muted); font-size: var(--fs-2xs); white-space: nowrap; text-overflow: ellipsis; overflow: hidden; }
.debug-launch label { min-width: 0; }.debug-launch label > span { flex: 0 0 auto; white-space: nowrap; }.debug-launch input { flex: 1 1 0; min-width: 0; width: 0; }
.debug-launch .debug-entry { flex-grow: 2; }.debug-launch .debug-attach-host { flex: 0 1 165px; }.debug-launch .debug-attach-port { flex: 0 1 105px; }.debug-attach-host select { min-width: 0; width: 100%; border: 1px solid var(--border); border-radius: 5px; padding: 5px 7px; font: inherit; color: var(--text); background: var(--panel); }
.debug-java-options { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; padding: 6px 12px; border-bottom: 1px solid var(--border); }.debug-java-options label { display: flex; align-items: center; min-width: 0; flex: 1 1 180px; gap: 6px; color: var(--muted); }.debug-java-options label span { white-space: nowrap; }.debug-java-options input:not([type=checkbox]) { flex: 1; width: 0; min-width: 0; border: 1px solid var(--border); background: var(--panel); color: var(--text); border-radius: 5px; padding: 5px 7px; min-height: 29px; font-size: 12px; }.debug-java-options .debug-entry-pause { flex: 0 0 auto; white-space: nowrap; }.debug-entry-pause input { margin: 0; accent-color: var(--accent); }
.debug-prerequisite { overflow-wrap: anywhere; }.debug-prerequisite button { margin-left: 6px; color: var(--accent); white-space: nowrap; }
.debug-body { display: grid; grid-template-columns: minmax(240px, 42%) 1fr; flex: 1; min-height: 0; }.debug-inspect { overflow: auto; border-right: 1px solid var(--border); }.debug-section h4 { margin: 0; padding: 7px 10px; font-size: 11px; font-weight: 600; color: var(--muted); border-bottom: 1px solid var(--border); display: flex; gap: 8px; }.debug-section h4 span { margin-left: auto; font-weight: 400; }.debug-section + .debug-section { border-top: 1px solid var(--border); }
.debug-frame { display: flex; flex-direction: column; align-items: flex-start; justify-content: flex-start; width: 100%; text-align: left; padding: 6px 10px !important; }.debug-frame > span { width: 100%; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.debug-frame small { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%; }.debug-frame.selected { background: var(--hover); color: var(--accent); }.debug-empty { color: var(--muted); padding: 9px 10px; margin: 0; font-size: 11px; }
.debug-variable { display: flex; align-items: baseline; gap: 5px; padding: 3px 8px; min-width: 0; }.debug-expand { padding: 0 !important; flex: 0 0 12px; }.debug-variable-name { color: var(--accent); flex-shrink: 0; }.debug-variable code { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }.debug-global { padding: 6px 10px; color: var(--muted); }.debug-global summary { cursor: pointer; }
.debug-breakpoint { display: flex; align-items: center; padding: 2px 5px; }.debug-location { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; text-align: left; }.debug-location span { color: var(--err); opacity: .45; }.debug-location span.verified { opacity: 1; }
.debug-console { display: flex; flex-direction: column; min-width: 0; min-height: 0; }.debug-output { flex: 1; overflow: auto; padding: 8px 10px; }.debug-output pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.6 var(--font-mono, monospace); }.debug-output pre.stderr { color: var(--err); }
.debug-evaluator { flex: none; padding: 8px; border-bottom: 1px solid var(--border); background: var(--panel-soft); }
.debug-expression-form { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; }
.debug-expression-form label { color: var(--muted); font-size: 11px; }
.debug-expression-form input { flex: 1 1 150px; min-width: 0; width: 0; min-height: 28px; border: 1px solid var(--border); border-radius: 4px; padding: 4px 6px; color: var(--text); background: var(--panel); font: 12px var(--font-mono, monospace); }
.debug-expression-form button { flex: none; border-color: var(--border); background: var(--panel); }
.debug-evaluation-result { max-height: 160px; overflow: auto; padding-top: 6px; }
.debug-evaluation-error { margin: 5px 0 0; color: var(--err); overflow-wrap: anywhere; }
.debug-watch-name { min-width: 0; max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--accent); }
.debug-watch-remove { margin-left: auto; flex: none; }.debug-watch code { flex: 1; }.debug-watch .evaluation-error { color: var(--err); }
.debug-value-type { color: var(--muted); flex: none; }.debug-watches h4 { align-items: center; }.debug-watches h4 button { padding: 0 4px; font-size: 11px; color: var(--accent); }
.debug-panel :is(button, input):focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
@media (max-width: 720px) { .debug-body { grid-template-columns: minmax(150px, 45%) 1fr; }.debug-toolbar { gap: 6px; padding: 6px 8px; }.debug-actions { width: 100%; margin-left: 0; }.debug-launch { gap: 6px; }.debug-launch label { flex-basis: 100%; } }
</style>
