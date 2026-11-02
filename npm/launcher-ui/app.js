const $ = (selector, root = document) => root.querySelector(selector)
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)]
const token = $('meta[name="launcher-token"]')?.content || ''
const desktop = document.documentElement.dataset.desktop === 'true'
if (desktop) $('#close-hint').textContent = '关闭此窗口后，服务继续运行'
const stateLabels = { healthy: '运行正常', stopped: '已停止', unhealthy: '检查异常', foreign: '端口被占用', starting: '启动中', stopping: '停止中' }
const serviceLabels = { backend: '后端', frontend: '前端', launcher: '启动器', update: '更新' }
let latestStatus = null
let statusPending = false
let logsPending = false
let requestPending = false
let selectedLog = 'backend'
let logText = ''
let logGeneration = 0
let dismissedOperation = null
let transientError = null
let lastOperationId = null
let connectionFault = false

async function api(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(15000), ...options, headers: { ...(options.body ? { 'Content-Type': 'application/json', 'X-Launcher-Token': token } : {}), ...options.headers } })
  const text = await response.text()
  let data
  try { data = text ? JSON.parse(text) : {} } catch { throw new Error(`启动器返回了无法解析的结果（HTTP ${response.status}）`) }
  if (!response.ok) throw new Error(data.error?.message || data.error || data.message || `请求失败（HTTP ${response.status}）`)
  return data
}

function showFeedback(message, state = 'succeeded') {
  const feedback = $('#feedback')
  feedback.hidden = false
  feedback.dataset.state = state
  $('#feedback-text').textContent = String(message)
  $('#dismiss-feedback').hidden = state === 'running'
}

function safeUrl(value) {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null
  } catch { return null }
}

function renderStatus(data) {
  if (connectionFault && !transientError) $('#feedback').hidden = true
  connectionFault = false
  latestStatus = data
  $('#header-version').textContent = `v${data.version || '未知'}`
  $('#workspace-root').textContent = data.root || '项目位置未提供'
  $('#workspace-root').title = data.root || ''
  const services = Array.isArray(data.services) ? data.services : []
  const healthy = services.filter(service => service.state === 'healthy').length
  $('#service-summary').textContent = `${healthy} / ${services.length} 正常运行`
  const connection = $('#connection-state')
  connection.className = 'connection connected'
  connection.replaceChildren(dot(), document.createTextNode('启动器已连接'))
  $('#refresh-time').textContent = `${new Date().toLocaleTimeString('zh-CN', { hour12: false })} 已刷新`
  for (const service of services) {
    const card = $(`#service-${service.id}`)
    if (!card) continue
    const badge = $('[data-field="state"]', card)
    const knownState = Object.hasOwn(stateLabels, service.state) ? service.state : 'unhealthy'
    badge.className = `state-badge state-${knownState}`
    badge.replaceChildren(dot(), document.createTextNode(stateLabels[knownState]))
    const link = $('[data-field="url"]', card)
    const destination = service.id === 'backend' ? `${String(service.url).replace(/\/$/, '')}/docs` : service.url
    const url = safeUrl(destination)
    if (url) link.href = url
    else link.removeAttribute('href')
    link.replaceChildren(document.createTextNode(destination || '尚未提供地址'), icon('open'))
    $('[data-field="pid"]', card).textContent = service.pid || '—'
    $('[data-field="latency"]', card).textContent = Number.isFinite(service.latencyMs) ? `${Math.round(service.latencyMs)} ms` : '—'
    $('[data-field="detail"]', card).textContent = service.detail || (service.state === 'healthy' ? '服务已就绪，可以开始工作。' : service.state === 'stopped' ? '点击启动以运行此服务。' : '等待下一次健康检查。')
  }
  const update = data.update || {}
  const latestLabel = update.latest && /^\d+\.\d+\.\d+/.test(update.latest) ? `v${update.latest}` : update.latest
  $('#update-version').textContent = `当前 v${update.current || data.version || '未知'}`
  $('#update-badge').hidden = !update.available
  $('#update-badge').textContent = latestLabel ? `可更新：${latestLabel}` : '发现新版本'
  $('#install-update').textContent = update.available ? '更新到最新版' : '更新'
  if (update.error) $('#update-detail').textContent = String(update.error)
  else if (update.message) $('#update-detail').textContent = String(update.message)
  else if (update.available) $('#update-detail').textContent = `${latestLabel || '新版本'}已可用。更新过程与结果可在「更新」日志中查看。`
  else if (update.checkedAt) $('#update-detail').textContent = `已检查${latestLabel ? `：最新版本 ${latestLabel}` : ''} · ${formatTime(update.checkedAt)}${data.mode === 'source' ? ' · 源码更新会先检查本地改动' : ''}`
  else $('#update-detail').textContent = data.mode === 'source' ? '源码工作区 · 更新前会检查本地改动，具体结果在更新日志中查看。' : '检查并下载最新发布版本，保留已有配置和数据。'

  const operation = data.operation
  if (operation?.id && operation.id !== lastOperationId) {
    lastOperationId = operation.id
    transientError = null
  }
  if (!transientError && operation && operation.id !== dismissedOperation) {
    showFeedback(operation.message || (operation.state === 'running' ? '正在处理，请稍候…' : operation.state === 'failed' ? '操作失败，请查看启动器日志。' : '操作已完成。'), operation.state || 'running')
  }
  updateDisabled()
}

function dot() { const element = document.createElement('i'); element.className = 'status-dot'; return element }
function icon(name) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  element.classList.add('icon')
  element.setAttribute('aria-hidden', 'true')
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use')
  use.setAttribute('href', `#icon-${name}`)
  element.append(use)
  return element
}
function formatTime(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false })
}

function updateDisabled() {
  const busy = requestPending || latestStatus?.operation?.state === 'running'
  for (const button of $$('[data-action]')) {
    const action = button.dataset.action
    const target = button.dataset.target
    const service = latestStatus?.services?.find(item => item.id === target)
    let disabled = busy || !latestStatus || connectionFault
    if (!disabled && action === 'start' && service?.state === 'healthy') disabled = true
    if (!disabled && ['stop', 'restart'].includes(action) && service?.state === 'foreign') disabled = true
    if (!disabled && action === 'stop' && service?.state === 'stopped') disabled = true
    button.disabled = disabled
  }
  $('#copy-logs').disabled = !logText
  $('#download-logs').disabled = !logText
}

async function refreshStatus() {
  if (statusPending) return
  statusPending = true
  try { renderStatus(await api('/api/status')) }
  catch (error) {
    connectionFault = true
    const connection = $('#connection-state')
    connection.className = 'connection disconnected'
    connection.replaceChildren(dot(), document.createTextNode('启动器连接中断'))
    $('#service-summary').textContent = '状态暂不可用'
    $('#refresh-time').textContent = '连接中断，正在重试'
    for (const badge of $$('.service-card [data-field="state"]')) {
      badge.className = 'state-badge state-unhealthy'
      badge.replaceChildren(dot(), document.createTextNode('状态未知'))
    }
    updateDisabled()
    if (!requestPending) showFeedback(`无法连接启动器：${error.message}。请确认启动器仍在运行；页面会自动重试。`, 'failed')
  } finally { statusPending = false }
}

async function act(action, target) {
  if (requestPending || latestStatus?.operation?.state === 'running') return
  requestPending = true
  transientError = null
  dismissedOperation = null
  showFeedback('正在提交操作…', 'running')
  updateDisabled()
  if (action === 'update' || action === 'check-update') selectLog('update', false)
  try {
    const result = await api('/api/action', { method: 'POST', body: JSON.stringify({ action, target }) })
    const operation = result.operation || (result.state ? result : null)
    if (operation) {
      latestStatus = { ...latestStatus, operation }
      lastOperationId = operation.id
      showFeedback(operation.message || '操作已受理，正在处理…', operation.state || 'running')
    } else showFeedback(result.message || '操作已完成。', 'succeeded')
    await refreshStatus()
    await refreshLogs()
  } catch (error) {
    transientError = error.message
    showFeedback(error.message, 'failed')
  } finally { requestPending = false; updateDisabled() }
}

function selectLog(service, focus = false) {
  if (!Object.hasOwn(serviceLabels, service)) return
  selectedLog = service
  logGeneration += 1
  logText = ''
  for (const tab of $$('.log-tab')) {
    const selected = tab.dataset.log === service
    tab.setAttribute('aria-selected', String(selected))
    tab.tabIndex = selected ? 0 : -1
    if (selected && focus) tab.focus()
  }
  $('#log-output').setAttribute('aria-labelledby', `tab-${service}`)
  $('#log-output').textContent = '正在读取日志…'
  $('#log-path').textContent = `${serviceLabels[service]}日志`
  $('#log-refresh-state').textContent = '最近 300 行'
  updateDisabled()
  refreshLogs()
}

async function refreshLogs() {
  if (logsPending) return
  logsPending = true
  const generation = logGeneration
  const service = selectedLog
  try {
    const result = await api(`/api/logs?service=${encodeURIComponent(service)}&lines=300`)
    if (generation !== logGeneration) return
    const next = typeof result.text === 'string' ? result.text : ''
    const output = $('#log-output')
    if (next !== logText || output.textContent === '正在读取日志…') {
      logText = next
      output.textContent = next || '暂无日志。启动服务或执行操作后，运行记录会显示在这里。'
      output.classList.toggle('is-empty', !next)
      if ($('#follow-logs').checked) output.scrollTop = output.scrollHeight
    }
    $('#log-path').textContent = result.path || `${serviceLabels[service]}日志尚未生成`
    $('#log-path').title = result.path || ''
    $('#log-refresh-state').textContent = `${new Date().toLocaleTimeString('zh-CN', { hour12: false })} · 最近 300 行`
    updateDisabled()
  } catch (error) {
    if (generation === logGeneration) {
      $('#log-refresh-state').textContent = '读取失败'
      if (!logText) $('#log-output').textContent = `暂时无法读取日志：${error.message}`
    }
  } finally {
    logsPending = false
    if (generation !== logGeneration) refreshLogs()
  }
}

document.addEventListener('click', event => {
  const action = event.target.closest('[data-action]')
  if (action && !action.disabled) act(action.dataset.action, action.dataset.target)
  const log = event.target.closest('[data-log]')
  if (log) {
    selectLog(log.dataset.log)
    if (!log.classList.contains('log-tab')) $('#logs-title').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'nearest' })
  }
})
$('.log-tabs').addEventListener('keydown', event => {
  const tabs = $$('.log-tab')
  const index = tabs.indexOf(document.activeElement)
  if (index < 0) return
  let next
  if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
  if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = tabs.length - 1
  if (next !== undefined) { event.preventDefault(); selectLog(tabs[next].dataset.log, true) }
})
$('#dismiss-feedback').addEventListener('click', () => {
  dismissedOperation = latestStatus?.operation?.id
  transientError = null
  $('#feedback').hidden = true
})
$('#refresh-logs').addEventListener('click', refreshLogs)
$('#follow-logs').addEventListener('change', event => { if (event.target.checked) $('#log-output').scrollTop = $('#log-output').scrollHeight })
$('#copy-logs').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(logText); showFeedback('当前日志已复制。') }
  catch { showFeedback(`${desktop ? '当前窗口' : '浏览器'}未允许复制，请点击下载日志，或在日志区域选中文本复制。`, 'failed') }
})
$('#download-logs').addEventListener('click', () => {
  const url = URL.createObjectURL(new Blob([logText], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `code-agent-${selectedLog}-${new Date().toISOString().replace(/[:.]/g, '-')}.log`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
})
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshStatus(); refreshLogs() } })
updateDisabled()
refreshStatus()
refreshLogs()
setInterval(() => { if (!document.hidden) refreshStatus() }, 2500)
setInterval(() => { if (!document.hidden) refreshLogs() }, 3000)
