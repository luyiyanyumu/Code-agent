// 后端 API 封装：REST + WebSocket。
// 本地工作区默认免登录；保留显式认证部署的 token 支持。

const TOKEN_KEY = 'chat-agent-token'

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch { return '' }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch { /* 禁止本地存储时，免登录工作区仍可正常调用。 */ }
}

export async function request(method, path, body, options = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const resp = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    ...(options?.signal ? { signal: options.signal } : {}),
  })

  if (resp.status === 401) {
    throw Object.assign(new Error('服务开启了访问认证，请输入此后端的访问令牌后重新连接。'), { status: 401 })
  }

  const text = await resp.text()
  let data = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = { detail: text } }
  }
  if (!resp.ok) {
    let msg = (data && (data.detail || data.message)) || `HTTP ${resp.status}`
    throw Object.assign(new Error(typeof msg === 'string' ? msg : JSON.stringify(msg)), { status: resp.status })
  }
  return data
}

export const api = {
  me: () => request('GET', '/api/v1/auth/me'),
  listSessions: () => request('GET', '/api/v1/sessions'),
  createSession: (opts = {}) =>
    request('POST', '/api/v1/sessions', {
      folder: opts.folder || '',
      ...(opts.permissionMode ? { permission_mode: opts.permissionMode } : {}),
    }),
  moveSessionFolder: (id, folder) => request('POST', `/api/v1/sessions/${id}/folder`, { folder }),
  setSessionPermission: (id, mode) =>
    request('POST', `/api/v1/sessions/${id}/permission`, { mode }),
  permissionSettings: () => request('GET', '/api/v1/settings/permission'),
  listWorkspaces: () => request('GET', '/api/v1/workspaces'),
  createWorkspace: (name) => request('POST', '/api/v1/workspaces', { name }),
  deleteWorkspace: (name) => request('DELETE', `/api/v1/workspaces/${encodeURIComponent(name)}`),
  events: (id, afterSeq = 0, options = {}) => request('GET', `/api/v1/sessions/${id}/events?after_seq=${afterSeq}`, undefined, options),
  prompt: (id, text, attachments = [], { web = null } = {}) =>
    request('POST', `/api/v1/sessions/${id}/prompt`, {
      text,
      ...(attachments.length ? { attachments } : {}),
      // 联网开关（M19）：只在明确给了布尔值时下发，其余情况沿用会话设置
      ...(typeof web === 'boolean' ? { web } : {}),
    }),
  webCapability: () => request('GET', '/api/v1/web'),
  setSessionWeb: (id, enabled) =>
    request('POST', `/api/v1/sessions/${id}/web`, { enabled }),
  uploadSessionFile: (id, name, contentB64) =>
    request('POST', `/api/v1/sessions/${id}/files`, { name, content_b64: contentB64 }),
  checkpoints: (id) => request('GET', `/api/v1/sessions/${id}/checkpoints`),
  rewind: (id, checkpointId) =>
    request('POST', `/api/v1/sessions/${id}/rewind`, { checkpoint_id: checkpointId }),
  acceptance: (id) => request('GET', `/api/v1/sessions/${id}/acceptance`),
  answerQuestion: (id, questionId, payload) =>
    request('POST', `/api/v1/sessions/${id}/questions/${questionId}/answer`, payload),
  decideApproval: (id, approved) => request('POST', `/api/v1/approvals/${id}/decide`, { approved }),
  pendingApproval: (sessionId) =>
    request('GET', `/api/v1/sessions/${sessionId}/approvals/pending`),
  tools: () => request('GET', '/api/v1/tools'),
  async artifactText(sid, filename) {
    const t = getToken()
    const resp = await fetch(
      `/api/v1/sessions/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(filename)}`,
      { headers: t ? { Authorization: `Bearer ${t}` } : {} }
    )
    if (!resp.ok) throw new Error(`读取文件失败：HTTP ${resp.status}`)
    return resp.text()
  },
  async artifactBlobUrl(sid, filename) {
    const t = getToken()
    const resp = await fetch(
      `/api/v1/sessions/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(filename)}`,
      { headers: t ? { Authorization: `Bearer ${t}` } : {} }
    )
    if (!resp.ok) throw new Error(`读取文件失败：HTTP ${resp.status}`)
    return URL.createObjectURL(await resp.blob())
  },
  modelSettings: () => request('GET', '/api/v1/settings/model'),
  modelProfiles: () => request('GET', '/api/v1/model-profiles'),
  saveModelProfile: (profile) => request('POST', '/api/v1/model-profiles', profile),
  activateModelProfile: (id) => request('POST', `/api/v1/model-profiles/${encodeURIComponent(id)}/activate`),
  deleteModelProfile: (id) => request('DELETE', `/api/v1/model-profiles/${encodeURIComponent(id)}`),
}

export function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result || '')
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : result)
    }
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'))
    reader.readAsDataURL(file)
  })
}

export function openStream(sessionId, afterSeq = 0) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${location.host}/api/v1/sessions/${sessionId}/stream?token=${encodeURIComponent(getToken())}&after_seq=${afterSeq}`
  return new WebSocket(url)
}
