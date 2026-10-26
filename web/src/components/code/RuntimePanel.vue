<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api, openTerminalStream } from '../../api.js'
import { createRunPoller, createTerminalStream, isRunning, outputLocations } from '../../lib/runtimeClient.js'
import '@xterm/xterm/css/xterm.css'

const props = defineProps({ projectId: { type: String, default: '' }, file: { type: Object, default: null }, activeTab: { type: String, default: 'terminal' }, visible: { type: Boolean, default: true } })
const emit = defineEmits(['open-file', 'error'])
const rootEl = ref(null), commandEl = ref(null), error = ref(''), notice = ref('')
const terminals = ref([]), terminalInfo = ref(null), terminalId = ref(''), shell = ref(''), cwd = ref(''), creatingTerminal = ref(false)
const terminalVersion = ref(0), configs = ref([]), runs = ref([]), runId = ref(''), creatingRun = ref(false), stoppingRun = ref(false)
const kind = ref('python'), target = ref(''), runCwd = ref(''), runName = ref(''), outputVersion = ref(0), outputEl = ref(null)
const configId = ref(''), formTouched = ref(false)
const emptyLoadStates = () => ({ terminals: { loading: false, error: '' }, configs: { loading: false, error: '' }, runs: { loading: false, error: '' } })
const loadStates = ref(emptyLoadStates()), loadRequests = new Map()
const loadLabels = { terminals: '终端', configs: '运行配置', runs: '运行记录' }
const entries = new Map(), hosts = new Map(), outputs = new Map()
let generation = 0, mounted = true, fitTimer = null, observer = null, modulePromise = null
const terminalVisible = computed(() => props.activeTab === 'terminal')
const loading = computed(() => loadStates.value[terminalVisible.value ? 'terminals' : 'configs'].loading)
const selectedTerminal = computed(() => terminals.value.find(item => item.id === terminalId.value))
const terminalState = computed(() => { terminalVersion.value; return entries.get(terminalId.value)?.state || (selectedTerminal.value && !isRunning(selectedTerminal.value) ? 'exited' : 'closed') })
const shellOptions = computed(() => (terminalInfo.value?.shells || []).filter(item => item.available))
const selectedRun = computed(() => runs.value.find(item => item.id === runId.value))
const shownRuns = computed(() => props.activeTab === 'tests' ? runs.value.filter(isTestRun) : runs.value)
const shownConfigs = computed(() => props.activeTab === 'tests' ? configs.value.filter(isTestRun) : configs.value)
const configGroups = computed(() => {
  const groups = new Map()
  for (const config of shownConfigs.value) {
    const label = isProjectConfig(config) ? `${kindNames[config.kind] || config.kind} · ${config.cwd || '项目根目录'}` : config.source === 'file' || ['python', 'node'].includes(config.kind) ? '文件入口' : '其他配置'
    if (!groups.has(label)) groups.set(label, [])
    groups.get(label).push(config)
  }
  return [...groups].map(([label, items]) => ({ label, items }))
    .sort((a, b) => Number(isProjectConfig(b.items[0])) - Number(isProjectConfig(a.items[0])))
})
const selectedConfig = computed(() => configs.value.find(config => config.id === configId.value))
const matchingConfig = computed(() => selectedConfig.value && matchesForm(selectedConfig.value) ? selectedConfig.value : null)
const output = computed(() => { outputVersion.value; return outputs.get(runId.value)?.text || '' })
const outputTruncated = computed(() => { outputVersion.value; return !!outputs.get(runId.value)?.truncated })
const locations = computed(() => outputLocations(output.value))
const hasCurrentRunner = computed(() => !!props.file?.path)
const currentCommand = computed(() => commandText(selectedRun.value?.command || selectedRun.value?.argv || selectedRun.value?.source_command || ''))
const statusNames = { running: '运行中', starting: '启动中', stopping: '停止中', exited: '已结束', stopped: '已停止', failed: '失败', closed: '已关闭', connecting: '连接中', reconnecting: '重连中', open: '已连接' }
const statusText = status => statusNames[status] || status || '待运行'
const commandText = value => Array.isArray(value) ? value.map(item => /\s/.test(String(item)) ? JSON.stringify(String(item)) : String(item)).join(' ') : String(value || '')
const kindNames = { python: 'Python', node: 'Node.js', npm: 'npm', maven: 'Maven', gradle: 'Gradle', pytest: 'pytest', command: '人工命令' }
const isTestRun = item => item.kind === 'pytest' || ['maven', 'gradle'].includes(item.kind) && item.script === 'test' || item.kind === 'npm' && /(?:^|[-:])test(?:$|[-:])/.test(item.script || item.name || '')
const isProjectConfig = item => item.source === 'project' || ['npm', 'maven', 'gradle'].includes(item.kind)
const configTarget = config => ['npm', 'maven', 'gradle'].includes(config.kind) ? config.script || '' : config.kind === 'command' ? commandText(config.command) : config.path || ''
const normalizeDirectory = value => String(value || '').replace(/\\/g, '/').replace(/^\.\//, '').replace(/\/+$/, '').replace(/^\.$/, '')
function matchesForm(config) { return config.kind === kind.value && configTarget(config) === target.value.trim() && normalizeDirectory(config.cwd) === normalizeDirectory(runCwd.value.trim()) }
function relatedProjectConfig(file = props.file, { tests = false } = {}) {
  const path = normalizeDirectory(file?.path), java = /\.(?:java|kt|kts|scala|groovy|xml|properties)$/i.test(path)
  const python = /\.py$/i.test(path), javascript = /\.(?:js|mjs|cjs|ts|tsx|jsx|vue|svelte)$/i.test(path)
  const candidates = configs.value.filter(config => tests ? isTestRun(config) : isProjectConfig(config) && !isTestRun(config))
    .filter(config => !java || ['maven', 'gradle'].includes(config.kind))
    .filter(config => !tests || !python && !javascript || python && config.kind === 'pytest' || javascript && config.kind === 'npm')
    .map(config => {
      const directory = normalizeDirectory(config.cwd)
      const distance = !directory ? 0 : path.startsWith(`${directory}/`) || path === directory ? directory.split('/').length : -1
      const goal = config.script || ''
      const priority = ['spring-boot:run', 'bootRun', 'start', 'dev', 'serve', 'run'].includes(goal) ? 3 : ['build', 'compile'].includes(goal) ? 2 : 1
      return { config, distance, priority }
    }).filter(item => item.distance >= 0)
    .sort((a, b) => b.distance - a.distance || b.priority - a.priority || Number(b.config.available !== false) - Number(a.config.available !== false))
  return candidates[0]?.config || null
}
function currentKind(file) {
  if (!file?.path) return ''
  if (/\.py$/i.test(file.path)) return 'python'
  if (/\.(?:js|mjs|cjs)$/i.test(file.path)) return 'node'
  return ''
}
function report(e, mine = generation) {
  if (!mounted || mine !== generation) return
  error.value = e?.message || String(e || '操作失败'); emit('error', error.value)
}
function patch(items, meta) {
  if (!meta?.id) return
  const index = items.value.findIndex(item => item.id === meta.id)
  if (index < 0) items.value = [meta, ...items.value]
  else items.value = items.value.map(item => item.id === meta.id ? { ...item, ...meta } : item)
}
function appendOutput(id, text, seq) {
  const buffer = outputs.get(id) || { text: '', seq: 0, truncated: false }
  const nearBottom = !outputEl.value || outputEl.value.scrollTop + outputEl.value.clientHeight >= outputEl.value.scrollHeight - 40
  buffer.text += text
  // Backend history is bounded too; keep long-running output from exhausting the browser.
  if (buffer.text.length > 1_048_576) { buffer.text = buffer.text.slice(-1_048_576); buffer.truncated = true }
  buffer.seq = seq; outputs.set(id, buffer); outputVersion.value++
  if (nearBottom) nextTick(() => { if (runId.value === id && outputEl.value) outputEl.value.scrollTop = outputEl.value.scrollHeight })
}
const poller = createRunPoller({
  load: (pid, id, seq) => api.code.run(pid, id, seq),
  onUpdate: (meta, identity) => {
    if (identity.projectId !== props.projectId) return
    const { events, ...summary } = meta; patch(runs, summary)
  },
  onOutput: (text, event, identity) => { if (identity.projectId === props.projectId) appendOutput(identity.id, text, event.seq) },
  onExit: (_code, event, identity) => { if (identity.projectId !== props.projectId) return; const buffer = outputs.get(identity.id) || { text: '', truncated: false }; buffer.seq = event.seq; outputs.set(identity.id, buffer); outputVersion.value++ },
  onGap: (_gap, identity) => { if (identity.projectId !== props.projectId) return; const buffer = outputs.get(identity.id) || { text: '', seq: 0 }; buffer.truncated = true; outputs.set(identity.id, buffer); outputVersion.value++ },
  onError: e => report(e),
})
function selectRunOutput(id = runId.value) {
  // Suspend only the output observer; backend jobs and terminal streams keep running.
  if (props.visible === false) { poller.stop(); return Promise.resolve() }
  return poller.select(props.projectId, id, { afterSeq: outputs.get(id)?.seq || 0 })
}

function queueFit() {
  if (fitTimer != null) clearTimeout(fitTimer)
  fitTimer = setTimeout(() => { fitTimer = null; fitSelected() }, 60)
}
function fitSelected() {
  if (!terminalVisible.value) return
  const entry = entries.get(terminalId.value), host = hosts.get(terminalId.value)
  if (!entry || !host?.clientWidth || !host?.clientHeight) return
  entry.fit.fit()
}
function setHost(id, element) {
  if (element) { hosts.set(id, element); observer?.observe(element); if (id === terminalId.value) ensureTerminal() }
  else { const old = hosts.get(id); if (old) observer?.unobserve(old); hosts.delete(id) }
}
function disconnectEntry(entry) {
  if (!entry) return
  entry.lastSeq = entry.stream.lastSeq; entry.stream.disconnect(); entry.connected = false; entry.term.options.disableStdin = true
}
function disposeEntry(id) {
  const entry = entries.get(id)
  if (!entry) return
  disconnectEntry(entry); entry.disposables.forEach(item => item.dispose()); entry.term.dispose(); entries.delete(id)
}
async function ensureTerminal() {
  const mine = generation, pid = props.projectId, id = terminalId.value
  if (!mounted || !pid || !id || !terminalVisible.value) return
  await nextTick()
  const host = hosts.get(id)
  if (!host?.clientWidth || !host?.clientHeight) return
  let entry = entries.get(id)
  if (!entry) {
    // Lazy-load the terminal only when a terminal host is actually visible.
    if (!modulePromise) modulePromise = Promise.all([import('@xterm/xterm'), import('@xterm/addon-fit')]).catch(e => { modulePromise = null; throw e })
    let modules
    try { modules = await modulePromise } catch (e) { report(e, mine); return }
    if (!mounted || mine !== generation || pid !== props.projectId || id !== terminalId.value || !terminalVisible.value || hosts.get(id) !== host) return
    entry = entries.get(id)
    if (!entry) {
      const term = new modules[0].Terminal({ fontSize: 12, fontFamily: 'Consolas, "Liberation Mono", monospace', scrollback: 5000, disableStdin: true, cursorBlink: true, theme: { background: '#101722', foreground: '#d9e2ef', cursor: '#88b4ff', selectionBackground: '#375578' } })
      const fit = new modules[1].FitAddon()
      entry = { term, fit, state: 'connecting', connected: false, lastSeq: 0, disposables: [], stream: null }
      const ownsEntry = () => mounted && mine === generation && entries.get(id) === entry
      entry.stream = createTerminalStream({
        open: openTerminalStream,
        onOutput: text => { if (ownsEntry()) term.write(text) },
        onReady: meta => { if (ownsEntry()) patch(terminals, { ...meta, id }) },
        onExit: code => { if (ownsEntry()) patch(terminals, { id, status: 'exited', exit_code: code }) },
        onState: state => { if (ownsEntry()) { entry.state = state; term.options.disableStdin = state !== 'open'; terminalVersion.value++ } },
        onGap: () => { if (ownsEntry()) term.write('\r\n\x1b[33m[早期输出已超出缓存，显示最近输出]\x1b[0m\r\n') },
        onError: e => { if (ownsEntry()) report(e, mine) },
      })
      entries.set(id, entry)
      try {
        term.loadAddon(fit); term.open(host)
        entry.disposables.push(term.onData(data => entry.stream.input(data)), term.onResize(size => entry.stream.resize(size.cols, size.rows)))
      } catch (e) { disposeEntry(id); report(e, mine); return }
    }
  }
  if (mine !== generation || id !== terminalId.value || !terminalVisible.value) return
  if (!entry.connected) { entry.connected = true; entry.stream.connect(pid, id, { afterSeq: entry.lastSeq }) }
  fitSelected()
}
function loadSection(section) {
  const mine = generation, pid = props.projectId
  if (!mounted || !pid) return Promise.resolve(false)
  const previous = loadRequests.get(section)
  if (previous && previous.generation === mine && previous.projectId === pid) return previous.promise
  const controller = typeof AbortController !== 'undefined' ? new AbortController() : null
  const request = { generation: mine, projectId: pid, controller, timer: null, cancel: null, promise: null }
  const current = () => mounted && mine === generation && pid === props.projectId && loadRequests.get(section) === request
  loadRequests.set(section, request)
  loadStates.value[section] = { loading: true, error: '' }
  const cancelled = new Promise((_, reject) => {
    request.cancel = () => reject(new Error('读取已取消'))
    request.timer = setTimeout(() => {
      reject(new Error(`${loadLabels[section]}读取超时，请重试`))
      controller?.abort()
    }, 15_000)
  })
  let response
  try {
    const method = section === 'configs' ? 'runConfigs' : section
    response = api.code[method](pid, controller ? { signal: controller.signal } : {})
  } catch (e) { response = Promise.reject(e) }
  request.promise = Promise.race([response, cancelled]).then(body => {
    if (!current()) return false
    const data = body || {}
    if (section === 'terminals') {
      terminalInfo.value = data
      for (const item of data.terminals || []) patch(terminals, item)
      shell.value = shell.value || (data.shells || []).find(item => item.available)?.id || ''
      terminalId.value = terminalId.value || terminals.value.find(isRunning)?.id || terminals.value[0]?.id || ''
    } else if (section === 'configs') {
      configs.value = data.configs || []
      if (props.activeTab === 'tests') showTests()
      else if (!formTouched.value) {
        const config = configs.value.find(item => item.id === configId.value) || relatedProjectConfig() || configs.value.find(item => isProjectConfig(item) && !isTestRun(item))
        if (config) chooseConfig(config, false)
      }
    } else {
      for (const item of [...(data.runs || [])].reverse()) patch(runs, item)
      runId.value = runId.value || shownRuns.value[0]?.id || ''
    }
    return true
  }).catch(e => {
    if (current()) loadStates.value[section].error = e?.message || `${loadLabels[section]}读取失败，请重试`
    return false
  }).finally(() => {
    if (request.timer != null) clearTimeout(request.timer)
    request.timer = null
    if (current()) { loadStates.value[section].loading = false; loadRequests.delete(section) }
  })
  return request.promise
}
function loadProject() { return Promise.allSettled(['terminals', 'configs', 'runs'].map(loadSection)) }
async function createTerminal() {
  const mine = generation, pid = props.projectId
  if (!pid || creatingTerminal.value || terminalInfo.value?.available === false) return false
  error.value = ''; creatingTerminal.value = true
  try {
    const meta = await api.code.createTerminal(pid, { ...(shell.value ? { shell: shell.value } : {}), cwd: cwd.value, cols: 80, rows: 24 })
    if (!mounted || mine !== generation || pid !== props.projectId) return false
    patch(terminals, meta); terminalId.value = meta.id; await ensureTerminal(); return true
  } catch (e) { report(e, mine); return false }
  finally { if (mine === generation) creatingTerminal.value = false }
}
async function closeTerminal() {
  const mine = generation, pid = props.projectId, id = terminalId.value
  if (!pid || !id) return
  try {
    await api.code.closeTerminal(pid, id)
    if (!mounted || mine !== generation || pid !== props.projectId) return
    disposeEntry(id); terminals.value = terminals.value.filter(item => item.id !== id)
    if (terminalId.value === id) terminalId.value = terminals.value.find(isRunning)?.id || terminals.value[0]?.id || ''
  } catch (e) { report(e, mine) }
}
function reconnect() {
  const entry = entries.get(terminalId.value)
  disconnectEntry(entry); ensureTerminal()
}
function chooseConfig(config, touched = true) {
  if (!config) return
  kind.value = config.kind; target.value = configTarget(config); runCwd.value = config.cwd || ''; runName.value = config.name || ''
  configId.value = config.id || ''; formTouched.value = touched; notice.value = ''
}
function selectConfig() {
  formTouched.value = true
  const config = selectedConfig.value
  if (config) chooseConfig(config)
  else { runName.value = ''; notice.value = '' }
}
async function startRun(payload) {
  const mine = generation, pid = props.projectId
  if (!pid || creatingRun.value) return false
  creatingRun.value = true; error.value = ''
  try {
    const meta = await api.code.createRun(pid, payload)
    if (!mounted || mine !== generation || pid !== props.projectId) return false
    patch(runs, meta); runId.value = meta.id; return true
  } catch (e) { report(e, mine); return false }
  finally { if (mine === generation) creatingRun.value = false }
}
function runForm() {
  const value = target.value.trim()
  if (matchingConfig.value?.available === false) { report(new Error(matchingConfig.value.reason || '此运行配置暂不可用，请检查运行环境')); return Promise.resolve(false) }
  if (!value && kind.value !== 'pytest') { report(new Error('请填写文件路径、脚本、构建目标或命令')); return Promise.resolve(false) }
  const key = kind.value === 'command' ? 'command' : ['npm', 'maven', 'gradle'].includes(kind.value) ? 'script' : 'path'
  const workingDirectory = runCwd.value.trim()
  const config = configs.value.find(matchesForm)
  return startRun({ kind: kind.value, [key]: value, cwd: workingDirectory, ...(config?.name ? { name: config.name } : {}) })
}
async function runCurrent() {
  const mine = generation, pid = props.projectId, file = props.file, path = file?.path
  if (file?.dirty) { report(new Error('请先保存当前文件后再运行')); return false }
  const runner = currentKind(props.file)
  if (!runner) {
    if (loadStates.value.configs.loading) await loadSection('configs')
    if (!mounted || mine !== generation || pid !== props.projectId || path !== props.file?.path) return false
    if (props.file?.dirty) { report(new Error('请先保存当前文件后再运行')); return false }
    const config = relatedProjectConfig(props.file)
    if (config) {
      chooseConfig(config)
      notice.value = `已选择「${config.name || kindNames[config.kind]}」${config.cwd && config.cwd !== '.' ? `（${config.cwd}）` : ''}。运行使用已保存的项目文件，请点击「运行」。`
    } else if (loadStates.value.configs.error) notice.value = '运行配置尚未读取成功，请重试后选择项目配置。'
    else if (/\.java$/i.test(file?.path || '')) notice.value = 'Java 类需通过 Maven 或 Gradle 的项目入口运行。请选择模块运行配置；Service 类不能作为单文件主程序。'
    else notice.value = '当前文件需通过项目运行配置启动。请选择配置后点击「运行」。'
    error.value = ''; nextTick(() => commandEl.value?.focus())
    return false
  }
  kind.value = runner; target.value = props.file.path; runName.value = props.file.path; runCwd.value = ''
  configId.value = ''; formTouched.value = true; notice.value = ''
  return startRun({ kind: runner, path: props.file.path, name: props.file.path })
}
function showTests() {
  const config = relatedProjectConfig(props.file, { tests: true }) || configs.value.find(item => isTestRun(item) && item.available !== false)
  if (config) chooseConfig(config)
  else { kind.value = 'pytest'; target.value = ''; runName.value = ''; runCwd.value = ''; configId.value = '' }
  if (props.activeTab === 'tests' && !shownRuns.value.some(item => item.id === runId.value)) runId.value = shownRuns.value[0]?.id || ''
  nextTick(() => commandEl.value?.focus())
}
async function stopRun() {
  const mine = generation, pid = props.projectId, id = runId.value
  if (!pid || !id || stoppingRun.value || !isRunning(selectedRun.value)) return
  stoppingRun.value = true
  try {
    const meta = await api.code.stopRun(pid, id)
    if (!mounted || mine !== generation || pid !== props.projectId) return
    patch(runs, meta); if (runId.value === id) poller.refresh()
  } catch (e) { report(e, mine) }
  finally { if (mine === generation) stoppingRun.value = false }
}
function focus() {
  if (terminalVisible.value) { ensureTerminal().then(() => entries.get(terminalId.value)?.term.focus()) }
  else commandEl.value?.focus()
}
function release() {
  generation++; poller.stop()
  for (const request of loadRequests.values()) { if (request.timer != null) clearTimeout(request.timer); request.controller?.abort(); request.cancel?.() }
  loadRequests.clear()
  if (fitTimer != null) { clearTimeout(fitTimer); fitTimer = null }
  observer?.disconnect(); observer = null
  for (const id of entries.keys()) disposeEntry(id)
  hosts.clear(); outputs.clear()
}
watch(() => props.projectId, () => {
  release(); terminals.value = []; terminalInfo.value = null; terminalId.value = ''; runs.value = []; configs.value = []; runId.value = ''
  shell.value = ''; cwd.value = ''; runCwd.value = ''; runName.value = ''; target.value = ''; kind.value = currentKind(props.file) || 'python'; error.value = ''; notice.value = ''
  configId.value = ''; formTouched.value = false; loadStates.value = emptyLoadStates()
  creatingTerminal.value = creatingRun.value = stoppingRun.value = false; outputVersion.value++
  if (typeof ResizeObserver !== 'undefined') observer = new ResizeObserver(() => { if (terminalVisible.value) { ensureTerminal(); queueFit() } })
  nextTick(() => { if (mounted && rootEl.value) observer?.observe(rootEl.value) }); loadProject()
}, { immediate: true })
watch(terminalId, (next, old) => { if (old && old !== next) disconnectEntry(entries.get(old)); ensureTerminal() })
watch(runId, id => { stoppingRun.value = false; selectRunOutput(id) })
watch(() => props.visible, visible => { if (visible === false) poller.stop(); else selectRunOutput() })
watch(() => props.activeTab, tab => { if (tab === 'terminal') ensureTerminal(); else if (tab === 'tests') showTests(); queueFit() })
onBeforeUnmount(() => { mounted = false; release() })
defineExpose({ focus, createTerminal, runCurrent, showTests, refresh: loadProject })
</script>

<template>
  <section ref="rootEl" class="runtime-panel" :aria-label="terminalVisible ? '人工终端' : activeTab === 'tests' ? '运行测试' : '运行输出'">
    <div v-if="error" class="runtime-error" role="alert"><span>{{ error }}</span><button aria-label="关闭运行错误" @click="error = ''">×</button></div>
    <p v-if="!projectId" class="runtime-empty">选择项目后可打开终端或运行文件。</p>
    <template v-else-if="terminalVisible">
      <div v-if="loadStates.terminals.error" class="runtime-load-error" role="alert"><span>{{ loadStates.terminals.error }}</span><button @click="loadSection('terminals')">重试终端</button></div>
      <div class="runtime-tools">
        <select v-model="terminalId" aria-label="选择终端" :disabled="!terminals.length"><option value="" disabled>终端</option><option v-for="item in terminals" :key="item.id" :value="item.id">{{ item.shell }} · {{ statusText(item.status) }} · {{ item.id.slice(0, 6) }}</option></select>
        <select v-model="shell" aria-label="新终端 Shell" :disabled="!shellOptions.length"><option value="" disabled>Shell</option><option v-for="item in shellOptions" :key="item.id" :value="item.id">{{ item.label || item.id }}</option></select>
        <input v-model="cwd" class="cwd-input" placeholder="项目根目录" aria-label="终端工作目录" title="项目内的相对目录" />
        <button :disabled="creatingTerminal || loading || terminalInfo?.available === false" title="新建人工终端" @click="createTerminal">{{ creatingTerminal ? '启动中…' : '+ 终端' }}</button>
        <button v-if="terminalId" title="重新连接终端" @click="reconnect">重连</button><button v-if="terminalId" title="关闭终端进程" @click="closeTerminal">关闭</button>
        <span class="runtime-state" :class="{ live: terminalState === 'open' }">{{ terminalId ? statusText(terminalState) : '' }}</span>
      </div>
      <p v-if="terminalInfo?.available === false" class="runtime-empty">{{ terminalInfo.reason || '此环境的 PTY 终端不可用' }}</p>
      <p v-else-if="!terminals.length" class="runtime-empty">{{ loading ? '读取终端…' : '点击「+ 终端」启动 Shell。输入会直接发送到当前项目的人工终端。' }}</p>
      <div v-else class="terminal-stack"><div v-for="item in terminals" v-show="item.id === terminalId" :key="item.id" :ref="element => setHost(item.id, element)" class="terminal-host" /></div>
      <div v-if="selectedTerminal" class="runtime-meta"><span>{{ selectedTerminal.cwd || '.' }}</span><span v-if="selectedTerminal.exit_code !== null && selectedTerminal.exit_code !== undefined">退出码 {{ selectedTerminal.exit_code }}</span><span>{{ terminalInfo?.backend || 'PTY' }}</span></div>
    </template>
    <template v-else>
      <div v-if="activeTab !== 'output'" class="runtime-configs">
        <label for="runtime-config-select">{{ activeTab === 'tests' ? '测试配置' : '运行配置' }}</label>
        <select id="runtime-config-select" v-model="configId" aria-label="运行配置" :disabled="loadStates.configs.loading && !configs.length" @change="selectConfig">
          <option value="">{{ loadStates.configs.loading ? '读取运行配置…' : '自定义运行 / 选择配置' }}</option>
          <optgroup v-for="group in configGroups" :key="group.label" :label="group.label">
            <option v-for="config in group.items" :key="config.id" :value="config.id">{{ config.name || configTarget(config) }}{{ config.available === false ? ' · 环境未就绪' : '' }}</option>
          </optgroup>
        </select>
        <button :disabled="loadStates.configs.loading" aria-label="刷新运行配置" @click="loadSection('configs')">{{ loadStates.configs.loading ? '读取中…' : '刷新' }}</button>
        <span class="runtime-config-hint">运行使用已保存的文件</span>
      </div>
      <div v-if="activeTab !== 'output' && loadStates.configs.error" class="runtime-load-error" role="alert"><span>{{ loadStates.configs.error }}</span><button @click="loadSection('configs')">重试运行配置</button></div>
      <p v-if="activeTab !== 'output' && matchingConfig?.reason && matchingConfig.available === false" class="runtime-notice">{{ matchingConfig.reason }}</p>
      <p v-if="activeTab !== 'output' && notice" class="runtime-guidance" role="status">{{ notice }}</p>
      <form v-if="activeTab !== 'output'" class="runtime-tools run-form" @submit.prevent="runForm">
        <select v-model="kind" aria-label="运行类型" @change="formTouched = true"><option value="python">Python</option><option value="node">Node.js</option><option value="npm">npm script</option><option value="maven">Maven</option><option value="gradle">Gradle</option><option value="pytest">pytest</option><option value="command">人工命令</option></select>
        <input ref="commandEl" v-model="target" class="command-input" :placeholder="kind === 'maven' ? 'compile / test / spring-boot:run' : kind === 'gradle' ? 'build / test / bootRun / run' : kind === 'npm' ? '脚本名称，如 test' : kind === 'command' ? '输入人工 Shell 命令' : kind === 'pytest' ? '测试文件或目录，留空运行全部' : '项目内文件路径'" :aria-label="kind === 'maven' ? 'Maven 目标' : kind === 'gradle' ? 'Gradle 目标' : kind === 'command' ? '人工运行命令' : kind === 'npm' ? 'npm 脚本名称' : '运行文件路径'" @input="formTouched = true" />
        <input v-model="runCwd" class="cwd-input" placeholder="项目根目录" aria-label="运行工作目录" title="构建文件所在模块的相对目录" @input="formTouched = true" />
        <button type="submit" :disabled="creatingRun || loadStates.configs.loading || matchingConfig?.available === false">{{ creatingRun ? '启动中…' : '▶ 运行' }}</button>
        <button v-if="hasCurrentRunner && activeTab !== 'tests'" type="button" :disabled="creatingRun" :title="currentKind(file) ? '运行已保存的当前文件' : '为当前文件选择项目运行配置'" @click="runCurrent">{{ currentKind(file) ? '当前文件' : '选择项目入口' }}</button>
      </form>
      <div v-if="loadStates.runs.error" class="runtime-load-error" role="alert"><span>{{ loadStates.runs.error }}</span><button @click="loadSection('runs')">重试运行记录</button></div>
      <div class="runtime-tools run-selection">
        <select v-model="runId" aria-label="选择运行记录" :disabled="!shownRuns.length"><option value="" disabled>{{ loadStates.runs.loading ? '读取运行记录…' : '运行记录' }}</option><option v-for="item in shownRuns" :key="item.id" :value="item.id">{{ item.name || item.path || item.script || item.kind }} · {{ statusText(item.status) }}{{ item.exit_code !== null && item.exit_code !== undefined ? ` · ${item.exit_code}` : '' }}</option></select>
        <button v-if="isRunning(selectedRun)" :disabled="stoppingRun" @click="stopRun">{{ stoppingRun ? '停止中…' : '■ 停止' }}</button>
        <span v-if="selectedRun" class="runtime-state" :class="{ live: isRunning(selectedRun) }">{{ statusText(selectedRun.status) }}<template v-if="selectedRun.exit_code !== null && selectedRun.exit_code !== undefined"> · 退出码 {{ selectedRun.exit_code }}</template></span>
      </div>
      <div v-if="selectedRun" class="runtime-source" :title="currentCommand"><code>{{ currentCommand || selectedRun.path || selectedRun.script || selectedRun.kind }}</code><small>{{ selectedRun.cwd || '.' }}</small></div>
      <p v-if="outputTruncated" class="runtime-notice">早期输出已超出缓存，当前显示最近输出。</p>
      <pre ref="outputEl" class="run-output" aria-label="运行输出">{{ output.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '') || (selectedRun ? isRunning(selectedRun) ? '等待输出…' : '此运行没有输出。' : '点击运行后，命令与输出会显示在这里。') }}</pre>
      <div v-if="locations.length" class="output-locations"><span>输出位置</span><button v-for="item in locations" :key="`${item.path}:${item.line}:${item.column}`" :title="`${item.path}:${item.line}`" @click="emit('open-file', item)">{{ item.path }}:{{ item.line }}</button></div>
    </template>
  </section>
</template>

<style scoped>
.runtime-panel { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; height: 100%; overflow: hidden; background: var(--panel); font-size: var(--fs-xs); }
.runtime-tools { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 8px 12px; min-width: 0; flex: none; }
.runtime-tools input, .runtime-tools select, .runtime-tools button { font-size: var(--fs-xs); min-height: 28px; padding: 4px 8px; min-width: 0; max-width: 100%; }
.runtime-tools select { max-width: 260px; }.runtime-tools .cwd-input { width: 135px; }.runtime-tools .command-input { flex: 1; min-width: 150px; }
.runtime-state { color: var(--muted); margin-left: auto; flex: none; }.runtime-state.live { color: var(--ok, #15803d); }
.runtime-error { display: flex; gap: 8px; align-items: center; padding: 5px 10px; background: #fff1f2; color: var(--err, #b91c1c); overflow-wrap: anywhere; flex: none; }.runtime-error span { flex: 1; }.runtime-error button { font-size: 13px; border: 0; background: transparent; color: inherit; padding: 0 4px; }
.runtime-empty { margin: 22px 16px; color: var(--muted); line-height: 1.7; }.terminal-stack { flex: 1; position: relative; min-height: 90px; min-width: 0; background: #202735; overflow: hidden; }.terminal-host { position: absolute; inset: 0; padding: 5px 6px 0; min-width: 0; overflow: hidden; }
.terminal-host :deep(.xterm) { height: 100%; }.runtime-meta { display: flex; flex-wrap: wrap; gap: 16px; padding: 3px 10px; color: var(--muted); font-size: var(--fs-2xs); overflow-wrap: anywhere; flex: none; }
.runtime-configs { display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-1-5); padding: var(--sp-1-5) var(--sp-2-5) 0; flex: none; min-width: 0; }
.runtime-configs label { color: var(--muted); flex: none; }
.runtime-configs select { flex: 1; min-width: 140px; max-width: 460px; font-size: var(--fs-2xs); padding: var(--sp-1) var(--sp-1-5); }
.runtime-configs button { font-size: var(--fs-2xs); padding: var(--sp-1) var(--sp-1-5); }
.runtime-config-hint { margin-left: auto; color: var(--muted); font-size: var(--fs-2xs); }
.runtime-load-error { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1) var(--sp-2-5); color: var(--err); background: #fff1f2; flex: none; overflow-wrap: anywhere; }
.runtime-load-error span { flex: 1; }
.runtime-load-error button { flex: none; font-size: var(--fs-2xs); padding: var(--sp-0-5) var(--sp-1-5); }
.runtime-guidance { margin: var(--sp-1-5) var(--sp-2-5) 0; color: var(--accent); font-size: var(--fs-2xs); line-height: 1.7; flex: none; }
.run-selection { border-top: 1px solid var(--border); padding-top: 5px; padding-bottom: 5px; }.run-selection select { flex: 1; max-width: 520px; }
.runtime-source { display: flex; align-items: baseline; gap: 10px; padding: 3px 10px; color: var(--muted); min-width: 0; flex: none; }.runtime-source code { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 11px; }.runtime-source small { flex: none; max-width: 35%; overflow-wrap: anywhere; font-size: var(--fs-2xs); }
.run-output { flex: 1; min-height: 50px; min-width: 0; margin: 0; padding: 7px 12px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.7 Consolas, 'Liberation Mono', monospace; background: var(--panel-soft, #f8fafc); color: var(--text); }
.runtime-notice { margin: 3px 10px; color: #a16207; font-size: var(--fs-2xs); flex: none; }.output-locations { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; max-height: 52px; overflow: auto; padding: 4px 10px; font-size: var(--fs-2xs); color: var(--muted); flex: none; }.output-locations button { font-size: var(--fs-2xs); padding: 2px 5px; max-width: 100%; overflow-wrap: anywhere; }
@media (max-width: 760px) { .runtime-tools { gap: 5px; padding: 5px 7px; }.runtime-tools select { max-width: 180px; }.runtime-tools .cwd-input { width: 105px; }.runtime-state { margin-left: 0; }.run-form .command-input { flex-basis: calc(100% - 140px); min-width: 100px; }.runtime-configs { padding-left: 7px; }.runtime-config-hint { margin-left: 0; }.runtime-source { padding-left: 7px; }.terminal-host { padding-left: 3px; padding-right: 3px; } }
</style>
