import { getToken } from '../api.js'

async function request(projectId, suffix, { method = 'GET', body, timeoutMs = 45000 } = {}) {
  const token = getToken()
  const headers = { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) }
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`/api/v1/code/projects/${encodeURIComponent(projectId)}/debug${suffix}`, {
      method, headers, signal: controller.signal, ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    })
    const text = await response.text()
    let data
    try { data = text ? JSON.parse(text) : {} } catch { data = { detail: text } }
    if (!response.ok) {
      const detail = data.detail || `HTTP ${response.status}`
      throw Object.assign(new Error(response.status === 404 && detail === 'Not Found' ? '后端尚未加载调试接口，请重启后端。' : typeof detail === 'string' ? detail : JSON.stringify(detail)), { status: response.status })
    }
    return data
  } finally { clearTimeout(timer) }
}

const sessionPath = id => `/sessions/${encodeURIComponent(id)}`

export function debugLanguageForPath(path) {
  return /\.java$/i.test(path || '') ? 'java' : /\.py$/i.test(path || '') ? 'python' : ''
}

export function debugAdapterMeta(meta, language) {
  if (!meta || !['python', 'java'].includes(language)) return { available: false }
  const adapter = meta.adapters?.[language]
  if (adapter) return { ...adapter, available: adapter.available === true && (!Array.isArray(meta.languages) || meta.languages.includes(language)) }
  // Older Python-only backends expose no adapters field. Never infer Java
  // availability from the shared top-level available flag.
  const available = Array.isArray(meta.languages) ? meta.languages.includes(language) && meta.available !== false : language === 'python' && meta.available === true
  return { ...(language === 'python' ? meta : {}), available,
    ...(!available && language === 'java' ? { install_hint: 'Java 调试器未就绪，请安装 Java Debug 插件并重新连接 Java 代码分析服务。' } : {}) }
}

export function javaDebugOptions({ request = 'launch', main_class = '', project_name = '', stop_on_entry = true, host = '127.0.0.1', port = 5005 } = {}) {
  if (!['launch', 'attach'].includes(request)) throw new Error('Java 调试支持启动或附加')
  if (typeof main_class !== 'string' || typeof project_name !== 'string' || main_class.length > 512 || project_name.length > 512 || /[\u0000-\u001f]/.test(main_class + project_name)) throw new Error('主类和项目名须为有效文本')
  if (typeof stop_on_entry !== 'boolean') throw new Error('入口暂停设置无效')
  const result = { request, main_class: main_class.trim(), project_name: project_name.trim(), stop_on_entry }
  if (request === 'attach') {
    if (!['127.0.0.1', 'localhost', '::1'].includes(host)) throw new Error('Java 附加调试仅支持本机地址')
    const parsed = typeof port === 'number' ? port : typeof port === 'string' && /^\d+$/.test(port.trim()) ? Number(port.trim()) : NaN
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) throw new Error('附加端口须为 1 到 65535 的整数')
    result.host = host === 'localhost' ? '127.0.0.1' : host
    result.port = parsed
  }
  return result
}

export const debugClient = {
  meta: projectId => request(projectId, '/meta'),
  sessions: projectId => request(projectId, '/sessions'),
  start: (projectId, body) => request(projectId, '/sessions', { method: 'POST', body, timeoutMs: body?.language === 'java' || debugLanguageForPath(body?.path) === 'java' ? 90000 : 45000 }),
  state: (projectId, id, { frameId, afterConsole = 0 } = {}) => request(projectId, `${sessionPath(id)}?after_console=${afterConsole}${frameId == null ? '' : `&frame_id=${frameId}`}`),
  action: (projectId, id, action) => request(projectId, `${sessionPath(id)}/action`, { method: 'POST', body: { action } }),
  breakpoints: (projectId, id, breakpoints) => request(projectId, `${sessionPath(id)}/breakpoints`, { method: 'POST', body: { breakpoints } }),
  variables: (projectId, id, reference, pauseSeq) => request(projectId, `${sessionPath(id)}/variables?reference=${reference}&pause_seq=${pauseSeq}`),
}
