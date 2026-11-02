// Frontend/backend integration uses the existing run IDs and output cursors.
export const INTEGRATION_ROLES = ['backend', 'frontend']
export const INTEGRATION_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
export const integrationActive = value => ['starting', 'running', 'ready', 'degraded', 'stopping', 'restarting'].includes(value?.status)
export const integrationStatus = value => ({ pending: '等待中', starting: '启动中', running: '运行中', ready: '已就绪', unhealthy: '连接异常', degraded: '部分就绪', stopping: '停止中', restarting: '重启中', stopped: '已停止', exited: '已结束', failed: '失败', error: '失败', attached: '已连接', idle: '未启动', unavailable: '不可用' })[value] || value || '未启动'

const copy = value => JSON.parse(JSON.stringify(value))
const service = value => ({ mode: 'launch', kind: 'command', cwd: '', path: '', script: '', command: '', args: [], env: {}, ...(value || {}) })
export function integrationDraft(config = {}) {
  return {
    name: String(config.name || '前后端联调'), frontend: service(config.frontend), backend: service(config.backend),
    frontend_url: String(config.frontend_url || ''), backend_url: String(config.backend_url || ''), health_url: String(config.health_url || ''),
    api_prefix: String(config.api_prefix || '/api'), strip_api_prefix: config.strip_api_prefix === true,
    startup_timeout: Number(config.startup_timeout) || 60,
  }
}
export function integrationServiceDraft(value) {
  const normalized = service(value)
  return { ...normalized, command: Array.isArray(normalized.command) ? normalized.command.join(' ') : String(normalized.command || ''),
    args_text: JSON.stringify(normalized.args || []), env_text: JSON.stringify(normalized.env || {}, null, 2) }
}
export function integrationForm(config) {
  const value = integrationDraft(config)
  return { ...value, frontend: integrationServiceDraft(value.frontend), backend: integrationServiceDraft(value.backend) }
}
export function jsonObject(text, label) {
  let value
  try { value = JSON.parse(String(text || '{}')) } catch { throw new Error(`${label}需要有效的 JSON 对象。`) }
  if (!value || Array.isArray(value) || typeof value !== 'object' || Object.values(value).some(item => typeof item !== 'string')) throw new Error(`${label}需要字符串键值组成的 JSON 对象。`)
  return value
}
export function integrationPayload(form) {
  const payload = integrationDraft(form)
  for (const role of INTEGRATION_ROLES) {
    const item = form[role] || {}
    let args
    try { args = JSON.parse(item.args_text ?? JSON.stringify(item.args || [])) } catch { throw new Error(`${role === 'frontend' ? '前端' : '后端'}启动参数需要有效的 JSON 数组。`) }
    if (!Array.isArray(args) || args.some(arg => typeof arg !== 'string')) throw new Error('启动参数需要字符串组成的 JSON 数组。')
    const command = String(item.command || '')
    const env = jsonObject(item.env_text ?? JSON.stringify(item.env || {}), '环境变量')
    payload[role] = { mode: item.mode || 'launch', kind: item.kind || 'command', cwd: String(item.cwd || '').trim(),
      path: String(item.path || '').trim(), script: String(item.script || '').trim(), command, args, env }
  }
  if (!Number.isFinite(payload.startup_timeout) || payload.startup_timeout < 1 || payload.startup_timeout > 300) throw new Error('启动等待时间需要在 1–300 秒之间。')
  return payload
}
export function candidateService(candidate, previous = {}) {
  const value = service(previous)
  if (!candidate) return integrationServiceDraft(value)
  return integrationServiceDraft({ ...value, mode: 'launch', kind: candidate.kind || 'command', cwd: candidate.cwd || '',
    path: candidate.path || '', script: candidate.script || '', command: candidate.kind === 'command' ? candidate.command || '' : '',
    args: candidate.args || [], env: candidate.env || {} })
}
export function sameIntegrationForm(a, b) { return JSON.stringify(a) === JSON.stringify(b) }
export function responseText(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? '', null, 2)
  try { return JSON.stringify(JSON.parse(text), null, 2) } catch { return text }
}
export function safeIntegrationUrl(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : '' } catch { return '' }
}

// Observation never starts/stops services. A stale response cannot select a new project.
export function createIntegrationObserver({ load, onState = () => {}, onError = () => {}, intervalMs = 1000, schedule = setTimeout, unschedule = clearTimeout } = {}) {
  let generation = 0, projectId = '', pending = null, timer = null
  function stop() {
    generation++; projectId = ''
    if (timer != null) unschedule(timer)
    timer = null; pending?.controller?.abort(); pending = null
  }
  async function refresh() {
    if (!projectId) return
    if (pending?.generation === generation) return pending.promise
    if (timer != null) unschedule(timer)
    timer = null
    const mine = generation, pid = projectId, controller = typeof AbortController === 'function' ? new AbortController() : null
    const request = { generation: mine, controller, promise: null }
    pending = request
    request.promise = (async () => {
      try {
        const value = await load(pid, controller ? { signal: controller.signal } : {})
        if (mine === generation && projectId === pid) onState(value, pid)
      } catch (error) {
        if (mine === generation && projectId === pid && error?.name !== 'AbortError') onError(error)
      } finally {
        if (pending === request) pending = null
        if (mine === generation && projectId === pid) timer = schedule(() => { timer = null; refresh() }, intervalMs)
      }
    })()
    return request.promise
  }
  function select(pid) { stop(); projectId = pid || ''; return refresh() }
  return { select, refresh, stop, dispose: stop }
}

// A timeout does not replay a write operation; callers refresh its resulting state.
export function integrationDeadline(action, { timeoutMs = 15000, message = '联调请求超时，请刷新状态后重试。', signal } = {}) {
  if (signal?.aborted) return Promise.reject(Object.assign(new Error('请求已取消'), { name: 'AbortError' }))
  let timer, listener
  const limit = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs)
    if (signal) {
      listener = () => reject(Object.assign(new Error('请求已取消'), { name: 'AbortError' }))
      if (signal.aborted) listener()
      else signal.addEventListener('abort', listener, { once: true })
    }
  })
  const pending = Promise.resolve().then(() => {
    if (signal?.aborted) throw Object.assign(new Error('请求已取消'), { name: 'AbortError' })
    return action()
  })
  return Promise.race([pending, limit]).finally(() => { clearTimeout(timer); if (listener) signal.removeEventListener('abort', listener) })
}

export function cloneIntegration(value) { return copy(value) }
