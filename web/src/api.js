// 后端 API 封装：REST + WebSocket。
// 本地工作区默认免登录；保留显式认证部署的 token 支持。

const TOKEN_KEY = 'code-agent-token'
import { notifyBusinessMeaningChanged } from './lib/businessMeaning.js'
//: 改名（CH-agent → Code-agent）之前的键：浏览器里还留着旧 token 的用户不该被登出，
//: 读到旧值就顺手迁到新键（只做一次）。写成常量拼接是为了让"新名字"这件事只有一处真源。
const LEGACY_TOKEN_KEY = ['ch', 'agent'].join('-') + '-token'

export function getToken() {
  try {
    const token = localStorage.getItem(TOKEN_KEY)
    if (token) return token
    const legacy = localStorage.getItem(LEGACY_TOKEN_KEY)
    if (legacy) {
      localStorage.setItem(TOKEN_KEY, legacy)
      localStorage.removeItem(LEGACY_TOKEN_KEY)
      return legacy
    }
    return ''
  } catch { return '' }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(LEGACY_TOKEN_KEY)
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
    throw Object.assign(new Error('服务开启了访问认证。直接进入工作区需将 CODE_AGENT_AUTH_ENABLED 设为 false 并重启后端。'), { status: 401 })
  }

  const text = await resp.text()
  let data = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = { detail: text } }
  }
  if (!resp.ok) {
    let msg = (data && (data.detail || data.message)) || `HTTP ${resp.status}`
    // 代码模式的路由是后加的：如果后端进程还是旧版本，这里会得到一句干巴巴的
    // "Not Found"，用户只会看到"项目加载失败"却不知道要重启服务。把原因说清楚。
    //
    // 但**只认路由级的 Not Found**：以前只要是 /api/v1/code/ 下的 404 就一律改写，
    // 于是"文件不存在/符号不存在/目录不存在"这类**资源级 404** 也被说成"后端是旧版本",
    // 把人引去重启服务（真机踩到：点模块节点看到"请重启后端"却重启无效）。
    // 路由级的特征很确定：FastAPI 的默认 detail 就是字符串 "Not Found"。
    if (resp.status === 404 && path.startsWith('/api/v1/code/')) {
      const detail = typeof (data && data.detail) === 'string' ? data.detail.trim() : String(text || '').trim()
      if (detail === 'Not Found' || detail === '{"detail":"Not Found"}') {
        msg =
          '后端没有这个 /api/v1/code 路由：当前服务进程可能是旧版本（代码模式是后加的）。' +
          '请重启后端进程后刷新页面。'
      }
    }
    throw Object.assign(new Error(typeof msg === 'string' ? msg : JSON.stringify(msg)), { status: resp.status })
  }
  return data
}

export const api = {
  health: () => request('GET', '/healthz'),
  me: () => request('GET', '/api/v1/auth/me'),

  listSessions: () => request('GET', '/api/v1/sessions'),
  // opts.folder：把新会话直接建在某个工作区分组下；opts.permissionMode：三档权限
  // 模型不用传：用什么模型由"当前生效的配置档案"决定
  createSession: (opts = {}) =>
    request('POST', '/api/v1/sessions', {
      folder: opts.folder || '',
      ...(opts.permissionMode ? { permission_mode: opts.permissionMode } : {}),
    }),
  // 每条档案自己的模型清单（M40：模型目录从全局搬到了档案下面）
  saveProfileModels: (pid, items) =>
    request('PUT', `/api/v1/model-profiles/${pid}/models`, { items }),
  resetProfileModels: (pid) =>
    request('POST', `/api/v1/model-profiles/${pid}/models/reset`),
  moveSessionFolder: (id, folder) => request('POST', `/api/v1/sessions/${id}/folder`, { folder }),
  // 工具一键启用/禁用（M21）：禁用后模型看不到该工具，也调不动
  setToolEnabled: (name, enabled) =>
    request('POST', `/api/v1/tools/${encodeURIComponent(name)}/enabled`, { enabled }),
  setToolsEnabled: (enabled, tools) =>
    request('POST', '/api/v1/tools/enabled', { enabled, ...(tools ? { tools } : {}) }),
  // 技能/专家：停用启用 + 删除（目录移入 .trash 回收站）
  setPackEnabled: (kind, name, enabled) =>
    request('POST', `/api/v1/packs/${kind}/${encodeURIComponent(name)}/enabled`, { enabled }),
  deletePack: (kind, name) =>
    request('DELETE', `/api/v1/packs/${kind}/${encodeURIComponent(name)}`),
  // MCP server：停用启用 + 删除（改 mcp.json，需重启服务才断开/连上）
  setMcpEnabled: (name, enabled) =>
    request('POST', `/api/v1/mcp/${encodeURIComponent(name)}/enabled`, { enabled }),
  deleteMcp: (name) => request('DELETE', `/api/v1/mcp/${encodeURIComponent(name)}`),
  setSessionPermission: (id, mode) =>
    request('POST', `/api/v1/sessions/${id}/permission`, { mode }),
  permissionSettings: () => request('GET', '/api/v1/settings/permission'),
  savePermissionSettings: (mode) =>
    request('PUT', '/api/v1/settings/permission', { mode }),

  // 工作区（侧栏分组）
  listWorkspaces: () => request('GET', '/api/v1/workspaces'),
  createWorkspace: (name) => request('POST', '/api/v1/workspaces', { name }),
  renameWorkspace: (name, next) =>
    request('PUT', `/api/v1/workspaces/${encodeURIComponent(name)}`, { name: next }),
  deleteWorkspace: (name) => request('DELETE', `/api/v1/workspaces/${encodeURIComponent(name)}`),
  closeSession: (id) => request('POST', `/api/v1/sessions/${id}/close`, {}),
  events: (id, afterSeq = 0, options = {}) => request('GET', `/api/v1/sessions/${id}/events?after_seq=${afterSeq}`, undefined, options),
  prompt: (id, text, attachments = [], { web = null, codeContext = null } = {}) =>
    request('POST', `/api/v1/sessions/${id}/prompt`, {
      text,
      ...(attachments.length ? { attachments } : {}),
      // 联网开关（M19）：只在明确给了布尔值时下发，其余情况沿用会话设置
      ...(typeof web === 'boolean' ? { web } : {}),
      ...(codeContext ? { code_context: codeContext } : {}),
    }),
  // 联网能力探测 + 会话开关（M19）
  webCapability: () => request('GET', '/api/v1/web'),
  setSessionWeb: (id, enabled) =>
    request('POST', `/api/v1/sessions/${id}/web`, { enabled }),
  uploadSessionFile: (id, name, contentB64) =>
    request('POST', `/api/v1/sessions/${id}/files`, { name, content_b64: contentB64 }),
  sessionFiles: (id) => request('GET', `/api/v1/sessions/${id}/files`),

  // 成对检查点 / 回退（M29）：检查点 = 对话位置 + 这一步的文件改动。
  // 回退会真的截断轨迹并撤回文件改动，所以这是一个写操作，不是"视图"操作。
  checkpoints: (id) => request('GET', `/api/v1/sessions/${id}/checkpoints`),
  rewind: (id, checkpointId) =>
    request('POST', `/api/v1/sessions/${id}/rewind`, { checkpoint_id: checkpointId }),
  // 有序验收清单（M29）：模型用 acceptance_set 声明，用户也可以直接改
  acceptance: (id) => request('GET', `/api/v1/sessions/${id}/acceptance`),
  setAcceptance: (id, items, note = '') =>
    request('PUT', `/api/v1/sessions/${id}/acceptance`, { items, note }),
  clearAcceptance: (id) => request('DELETE', `/api/v1/sessions/${id}/acceptance`),
  // 提问（M31）：把答案送达模型挂起中的问题；dismiss=true 表示放弃整组
  answerQuestion: (id, questionId, payload) =>
    request('POST', `/api/v1/sessions/${encodeURIComponent(id)}/questions/${encodeURIComponent(questionId)}/answer`, payload),
  pendingQuestions: (id) =>
    request('GET', `/api/v1/sessions/${encodeURIComponent(id)}/questions/pending`),

  decideApproval: (id, approved) => request('POST', `/api/v1/approvals/${id}/decide`, { approved }),
  /** 挂起中的审批（刷新/重连后恢复弹窗用）；无挂起返回 {}。 */
  pendingApproval: (sessionId) =>
    request('GET', `/api/v1/sessions/${sessionId}/approvals/pending`),

  usage: () => request('GET', '/api/v1/usage/me'),

  automations: () => request('GET', '/api/v1/automations'),
  createAutomation: (payload) => request('POST', '/api/v1/automations', payload),
  setAutomationStatus: (id, status) => request('POST', `/api/v1/automations/${id}/status`, { status }),

  audit: (limit = 100) => request('GET', `/api/v1/admin/audit?limit=${limit}`),
  auditVerify: () => request('GET', '/api/v1/admin/audit/verify'),

  tools: () => request('GET', '/api/v1/tools'),
  tool: (name) => request('GET', `/api/v1/tools/${encodeURIComponent(name)}`),
  permissions: () => request('GET', '/api/v1/permissions'),
  setPermission: (tool, mode) =>
    request('PUT', `/api/v1/permissions/${encodeURIComponent(tool)}`, { mode }),

  plugins: () => request('GET', '/api/v1/plugins'),
  togglePlugin: (name, enabled) => request('POST', `/api/v1/plugins/${encodeURIComponent(name)}/toggle`, { enabled }),
  reloadPlugins: () => request('POST', '/api/v1/plugins/reload', {}),
  uploadPlugin: (filename, contentB64, overwrite = false) =>
    request('POST', '/api/v1/plugins/upload', { filename, content_b64: contentB64, overwrite }),
  deletePlugin: (name) => request('DELETE', `/api/v1/plugins/${encodeURIComponent(name)}`),

  uploadSkill: (name, description, content, resources) =>
    request('POST', '/api/v1/skills/upload', { name, description, content, resources }),
  importSkillZip: (filename, contentB64, overwrite = false) =>
    request('POST', '/api/v1/skills/import', { filename, content_b64: contentB64, overwrite }),

  mcpConfig: () => request('GET', '/api/v1/mcp/config'),
  saveMcpConfig: (servers) => request('PUT', '/api/v1/mcp/config', { servers }),

  // 知识库（M12/M14 · LLM wiki）
  knowledge: (origin) =>
    request('GET', `/api/v1/knowledge${origin ? `?origin=${encodeURIComponent(origin)}` : ''}`),
  knowledgeStats: () => request('GET', '/api/v1/knowledge/stats'),
  knowledgeGet: (id, origin) =>
    request(
      'GET',
      `/api/v1/knowledge/${encodeURIComponent(id)}${origin ? `?origin=${encodeURIComponent(origin)}` : ''}`
    ),
  knowledgeSearch: (q, topK = 5, origin) =>
    request(
      'GET',
      `/api/v1/knowledge/search?q=${encodeURIComponent(q)}&top_k=${topK}${origin ? `&origin=${encodeURIComponent(origin)}` : ''}`
    ),
  knowledgeCreate: (payload) => request('POST', '/api/v1/knowledge', payload),
  knowledgeUpdate: (id, payload) =>
    request('PUT', `/api/v1/knowledge/${encodeURIComponent(id)}`, payload),
  knowledgeDelete: (id, origin) =>
    request(
      'DELETE',
      `/api/v1/knowledge/${encodeURIComponent(id)}${origin ? `?origin=${encodeURIComponent(origin)}` : ''}`
    ),
  knowledgePromote: (id) =>
    request('POST', `/api/v1/knowledge/${encodeURIComponent(id)}/promote`, {}),
  knowledgeCompile: (id) =>
    request('POST', `/api/v1/knowledge/${encodeURIComponent(id)}/compile`, {}),
  knowledgeImportDir: (dir) => request('POST', '/api/v1/knowledge/import', { dir }),
  knowledgeImportFiles: (files) => request('POST', '/api/v1/knowledge/import', { files }),

  // 交付文件（deliver_file 产物）
  artifacts: (sid) => request('GET', `/api/v1/sessions/${encodeURIComponent(sid)}/artifacts`),
  /** 取交付文件的原始文本（FileResponse，不走 JSON 包装）。 */
  async artifactText(sid, filename) {
    const t = getToken()
    const resp = await fetch(
      `/api/v1/sessions/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(filename)}`,
      { headers: t ? { Authorization: `Bearer ${t}` } : {} }
    )
    if (!resp.ok) throw new Error(`读取文件失败：HTTP ${resp.status}`)
    return resp.text()
  },
  /** 取交付文件的 object URL（图片等二进制预览用）；调用方负责 revoke。 */
  async artifactBlobUrl(sid, filename) {
    const t = getToken()
    const resp = await fetch(
      `/api/v1/sessions/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(filename)}`,
      { headers: t ? { Authorization: `Bearer ${t}` } : {} }
    )
    if (!resp.ok) throw new Error(`读取文件失败：HTTP ${resp.status}`)
    return URL.createObjectURL(await resp.blob())
  },

  // 代码模式（M18）：项目登记 / 目录树 / 文件文本 / 项目会话
  // 项目 = 用户自己的一个真实目录；会话工作区跟着项目根走（见后端 manager.workspace_for）
  code: {
    agentTeam: (pid, tree = 'main', conversationId = '') =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/agents?tree=${encodeURIComponent(tree)}${conversationId ? `&conversation_id=${encodeURIComponent(conversationId)}` : ''}`),
    stopAgent: (pid, sessionId, conversationId = '') =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/agents/${encodeURIComponent(sessionId)}/stop`, conversationId ? { conversation_id: conversationId } : {}),
    sendAgentMessage: (pid, sessionId, message) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/agents/${encodeURIComponent(sessionId)}/message`, { message }),
    followupAgent: (pid, sessionId, prompt, contextStrategy = 'task_brief') =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/agents/${encodeURIComponent(sessionId)}/followup`, { prompt, context_strategy: contextStrategy }),
    interruptAgent: (pid, sessionId) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/agents/${encodeURIComponent(sessionId)}/interrupt`, {}),
    auditAgent: (pid, sessionId, decision, reason = '', activationId = '') =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/agents/${encodeURIComponent(sessionId)}/audit`, { decision, reason, activation_id: activationId }),
    workflow: (pid, tree = 'main', options = {}) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/workflow?tree=${encodeURIComponent(tree)}`, undefined, options),
    saveWorkflow: (pid, workflow, expectedRevision, tree = 'main') =>
      request('PUT', `/api/v1/code/projects/${encodeURIComponent(pid)}/workflow?tree=${encodeURIComponent(tree)}`, {
        expected_revision: expectedRevision,
        workflow: {
          schema_version: workflow.schema_version,
          name: workflow.name,
          nodes: workflow.nodes,
          edges: workflow.edges,
          viewport: workflow.viewport,
        },
      }),
    meta: () => request('GET', '/api/v1/code/meta'),
    listProjects: () => request('GET', '/api/v1/code/projects'),
    openProject: (path) => request('POST', '/api/v1/code/projects', { mode: 'open', path }),
    createProject: (name, template = 'empty', gitInit = true, location) => {
      const parent = String(location || '').trim()
      return request('POST', '/api/v1/code/projects', { mode: 'create', name, template, git_init: gitInit, ...(parent ? { location: parent } : {}) })
    },
    project: (pid) => request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}`),
    removeProject: (pid, deleteFiles = false) =>
      request(
        'DELETE',
        `/api/v1/code/projects/${encodeURIComponent(pid)}${deleteFiles ? '?delete_files=true' : ''}`
      ),
    projectSession: (pid, opts = {}) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/session`, opts),
    conversations: (pid) => request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/conversations`),
    createConversation: (pid, payload = {}) => request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/conversations`, payload),
    deleteConversation: (pid, sid) => request('DELETE', `/api/v1/code/projects/${encodeURIComponent(pid)}/conversations/${encodeURIComponent(sid)}`),
    // IDE 人工终端与运行：发现配置只读，执行由界面按钮显式触发。
    terminals: (pid, options) => request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/terminals`, undefined, options),
    createTerminal: (pid, payload = {}) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/terminals`, payload),
    closeTerminal: (pid, id) =>
      request('DELETE', `/api/v1/code/projects/${encodeURIComponent(pid)}/terminals/${encodeURIComponent(id)}`),
    runConfigs: (pid, options) => request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/run-configs`, undefined, options),
    designInfo: (pid, options) => request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/frontend-design`, undefined, options),
    designPreview: (pid, payload, options) => request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/frontend-design/preview`, payload, options),
    saveDesignStyles: (pid, payload, options) => request('PUT', `/api/v1/code/projects/${encodeURIComponent(pid)}/frontend-design/styles`, payload, options),
    runs: (pid, options) => request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/runs`, undefined, options),
    createRun: (pid, payload) => request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/runs`, payload),
    run: (pid, id, afterSeq = 0) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/runs/${encodeURIComponent(id)}?after_seq=${Math.max(0, Math.trunc(Number(afterSeq) || 0))}`),
    stopRun: (pid, id) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/runs/${encodeURIComponent(id)}/stop`, {}),
    tree: (pid, path = '') =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/tree?path=${encodeURIComponent(path)}`
      ),
    file: (pid, path, start, end) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/file?path=${encodeURIComponent(path)}` +
          (start ? `&start=${start}` : '') +
          (end ? `&end=${end}` : '')
      ),
    files: (pid, { query = '', limit = 500 } = {}) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/files?` +
        new URLSearchParams({ query, limit })),
    search: (pid, { query = '', case_sensitive = false, regex = false, include = '', limit = 200 } = {}) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/search?` +
        new URLSearchParams({ query, case_sensitive, regex, include, limit })),
    /** 编辑器保存（乐观锁）：sha1 不匹配服务端返回 409。 */
    saveFile: (pid, path, content, expectedSha1, sessionId = '') =>
      request('PUT', `/api/v1/code/projects/${encodeURIComponent(pid)}/file`, {
        path,
        content,
        expected_sha1: expectedSha1 || '',
        session_id: sessionId,
      }),
    /** 项目设置：allow_direct_write 控制主会话能否直改主干（默认关）。 */
    updateSettings: (pid, patch) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/settings`, patch),
    patches: (pid, { limit = 60, path = '' } = {}) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/patches?limit=${limit}` +
          (path ? `&path=${encodeURIComponent(path)}` : '')
      ),
    patchDetail: (pid, patchId) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/patches/${encodeURIComponent(patchId)}`
      ),
    revertPatch: (pid, patchId) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/patches/${encodeURIComponent(patchId)}/revert`,
        {}
      ),
    fsList: (path = '', { hidden = false } = {}) =>
      request(
        'GET',
        `/api/v1/code/fs/list?path=${encodeURIComponent(path)}${hidden ? '&hidden=1' : ''}`
      ),
    pickFolder: (start = '') => request('POST', '/api/v1/code/fs/pick-folder', { start }),

    // 索引与地图（P1）：ensure=1 时服务端会先保证索引新鲜（首次打开项目用）
    indexStatus: (pid, tree = 'main', options = {}) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/index?tree=${tree}`, undefined, options),
    scan: (pid, { full = false, tree = 'main', signal } = {}) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/scan?full=${full ? 1 : 0}&tree=${tree}`,
        {}, { signal }
      ),
    map: (pid, { tree = 'main', depth = 2, module = '', kinds = '', edges = true, ensure = false } = {}) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/map?tree=${tree}&depth=${depth}` +
          `&module=${encodeURIComponent(module)}&kinds=${encodeURIComponent(kinds)}` +
          `&edges=${edges ? 1 : 0}&ensure=${ensure ? 1 : 0}`
      ),
    mapDelta: (pid, after, tree = 'main') =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/map/delta?after=${after}&tree=${tree}`
      ),
    symbol: (pid, id, { tree = 'main', withSource = true } = {}) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/symbol?id=${encodeURIComponent(id)}` +
          `&tree=${tree}&with_source=${withSource ? 1 : 0}`
      ),
    reindex: (pid, tree = 'main') =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/reindex?tree=${tree}`, {}),
    digest: (pid, maxChars = 6000, tree = 'main') =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/digest?tree=${tree}&max_chars=${maxChars}`
      ),

    // LSP（P3）：目录/探测状态与诊断概览
    lsp: (pid = '') =>
      request('GET', `/api/v1/code/lsp${pid ? `?project_id=${encodeURIComponent(pid)}` : ''}`),
    projectLsp: (pid, tree = 'main', refresh = false) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/lsp?tree=${tree}${refresh ? '&refresh=1' : ''}`
      ),
    diagnostics: (pid, { tree = 'main', path = '', severity = '', limit = 200, details = false } = {}) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/diagnostics?tree=${tree}&limit=${limit}` +
          (path ? `&path=${encodeURIComponent(path)}` : '') +
          (severity ? `&severity=${severity}` : '') +
          (details ? '&details=1' : '')
      ),

    // 统一工作项：团队派工、隔离单任务、隔离并行任务。
    workItems: (pid) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/work-items`),
    createWorkItem: (pid, payload) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/work-items`, payload),

    // 任务（P4）：隔离工作树 + 计划 + 审查 + 合并
    tasks: (pid, limit = 30) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks?limit=${limit}`),
    task: (pid, tid) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}`),
    createTask: (pid, payload) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks`, payload),
    updateTaskPlan: (pid, tid, plan) =>
      request(
        'PUT',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/plan`,
        { plan }
      ),
    approveTask: (pid, tid) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/approve`,
        {}
      ),
    taskReview: (pid, tid) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/review`
      ),
    taskDiff: (pid, tid, path = '') =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/diff` +
          (path ? `?path=${encodeURIComponent(path)}` : '')
      ),
    verifyTask: (pid, tid) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/verify`,
        {}
      ),
    mergeTask: (pid, tid, payload = {}) => {
      const body = Array.isArray(payload) ? (payload.length ? { files: payload } : {}) : payload || {}
      return request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/merge`,
        body
      )
    },
    discardTask: (pid, tid) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/discard`,
        {}
      ),

    // 源代码管理（P4）
    git: (pid) => request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/git`),
    gitBranches: (pid) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/git/branches`),
    gitCommit: (pid, message, files = null) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/git/commit`, {
        message,
        ...(files && files.length ? { files } : {}),
      }),
    gitAction: (pid, action, extra = {}) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/git/action`, {
        action,
        ...extra,
      }),
    gitCheckout: (pid, branch) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/git/checkout`, { branch }),

    // 并行批次（P5）：一个需求 → N 条互不重叠的车道 → 串行合并队列
    batches: (pid, limit = 30) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/batches?limit=${limit}`),
    batch: (pid, bid) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/batches/${encodeURIComponent(bid)}`
      ),
    createBatch: (pid, payload) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/batches`, payload),
    updateBatchPlan: (pid, bid, plan) =>
      request(
        'PUT',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/batches/${encodeURIComponent(bid)}/plan`,
        { plan }
      ),
    approveBatch: (pid, bid, payload = {}) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/batches/${encodeURIComponent(bid)}/approve`,
        payload
      ),
    reviewBatch: (pid, bid) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/batches/${encodeURIComponent(bid)}/review`
      ),
    mergeBatch: (pid, bid, payload = {}) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/batches/${encodeURIComponent(bid)}/merge`,
        payload
      ),
    discardBatch: (pid, bid) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/batches/${encodeURIComponent(bid)}/discard`,
        {}
      ),

    // P6：影响面 / 架构规则与门禁 / 决策线程 / 改动溯源
    impact: (pid, payload) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/impact`, payload),
    architecture: (pid) =>
      request('GET', `/api/v1/code/projects/${encodeURIComponent(pid)}/architecture`),
    saveArchitecture: (pid, rules) =>
      request('PUT', `/api/v1/code/projects/${encodeURIComponent(pid)}/architecture`, { rules }),
    gate: (pid, payload) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/gate`, payload),
    decisions: (pid, { target = '', limit = 50, kinds = '', prefix = false } = {}) => {
      const query = new URLSearchParams()
      if (target) query.set('target', target)
      query.set('limit', String(limit))
      if (kinds) query.set('kinds', kinds)
      if (prefix) query.set('prefix', 'true')
      return request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/decisions?${query.toString()}`
      )
    },
    addDecision: (pid, payload) =>
      request('POST', `/api/v1/code/projects/${encodeURIComponent(pid)}/decisions`, payload),
    deleteDecision: (pid, id) =>
      request(
        'DELETE',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/decisions/${encodeURIComponent(id)}`
      ),
    provenance: (pid, { path = '', symbolId = '', tree = 'main', limit = 80 } = {}) => {
      const query = new URLSearchParams()
      if (path) query.set('path', path)
      if (symbolId) query.set('symbol_id', symbolId)
      if (tree) query.set('tree', tree)
      query.set('limit', String(limit))
      return request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/provenance?${query.toString()}`
      )
    },

    // P7：测试即规格（行为卡片 / 覆盖率）与图即文档
    behaviors: (pid, { path = '', module = '', target = '', tree = 'main', limit = 500 } = {}) => {
      const query = new URLSearchParams()
      if (path) query.set('path', path)
      if (module) query.set('module', module)
      if (target) query.set('target', target)
      if (tree) query.set('tree', tree)
      query.set('limit', String(limit))
      return request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/spec/behaviors?${query.toString()}`
      )
    },
    coverage: (pid, tree = 'main') =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/spec/coverage?tree=${encodeURIComponent(tree)}`
      ),
    // 契约层（M32）：不带 path = 列项目里所有契约；带 path = 查那一个实现文件的契约对照
    contracts: (pid, { path = '' } = {}) => {
      const query = new URLSearchParams()
      if (path) query.set('path', path)
      const tail = query.toString()
      return request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/contracts${tail ? '?' + tail : ''}`
      )
    },
    evidence: (pid, tree = 'main') =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/evidence?tree=${encodeURIComponent(tree)}`
      ),
    // 功能导航（M33）：第一屏 = 能力域 → 功能；更深的层按需取（点开才请求）
    features: (pid, { tree = 'main' } = {}) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/features?tree=${encodeURIComponent(tree)}`
      ),
    featureChildren: (pid, id, { tree = 'main', visited = [] } = {}) => {
      const query = new URLSearchParams()
      query.set('id', id)
      if (tree) query.set('tree', tree)
      // visited = 当前路径上的祖先：静态调用图里有环，服务端据此标 cyclic 并停止展开
      if (visited.length) query.set('visited', visited.filter(Boolean).join(','))
      return request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/features/children?${query.toString()}`
      )
    },
    // 功能描述（M33）：模型补的"它是干什么的"（按内容哈希缓存，代码改了自动失效）
    featureBriefs: (pid, { missing = 0 } = {}) =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/feature-briefs${missing ? `?missing=${missing}` : ''}`
      ),
    // 补描述：后台跑（批量问模型 + 服务端落库，不带任何工具），进度走 indexStatus.briefs
    fillFeatureBriefs: (pid, { limit = 0 } = {}) =>
      request(
        'POST',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/feature-briefs/fill${limit ? `?limit=${limit}` : ''}`
      ),
    symbolLogic: (pid, id, tree = 'main') =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/symbol/logic?id=${encodeURIComponent(id)}&tree=${encodeURIComponent(tree)}`
      ),
    // P10：符号的"意义与作用"（它是什么、拿来干什么、谁真的在用）
    symbolPurpose: (pid, id, tree = 'main') =>
      request(
        'GET',
        `/api/v1/code/projects/${encodeURIComponent(pid)}/symbol/purpose?id=${encodeURIComponent(id)}&tree=${encodeURIComponent(tree)}`
      ),
    symbolBusiness: (pid, id, tree = 'main') => request('GET',
      `/api/v1/code/projects/${encodeURIComponent(pid)}/symbol/business?id=${encodeURIComponent(id)}&tree=${encodeURIComponent(tree)}`),
    resolveSymbolBusiness: (pid, payload, options = {}) => request('POST',
      `/api/v1/code/projects/${encodeURIComponent(pid)}/symbol/business/resolve`, payload, options),
    saveSymbolBusiness: async (pid, payload) => {
      const record = await request('PUT', `/api/v1/code/projects/${encodeURIComponent(pid)}/symbol/business`, payload)
      notifyBusinessMeaningChanged({ projectId: pid, tree: payload.tree || 'main', id: payload.id })
      return record
    },
  },

  // 记忆：个人、项目及独立会话；结构化修改使用稳定 ID 和范围版本。
  memory: () => request('GET', '/api/v1/memory'),  addMemory: (payload) => request('POST', '/api/v1/memory', payload),
  updateMemory: (entryId, payload) => request('PATCH', `/api/v1/memory/${encodeURIComponent(entryId)}`, payload),
  deleteMemoryRecord: (scopeId, entryId, revision) => request('DELETE', `/api/v1/memory?scope_id=${encodeURIComponent(scopeId)}&entry_id=${encodeURIComponent(entryId)}&revision=${encodeURIComponent(revision)}`),
  deleteMemory: (scope, index, sessionId = '') =>
    request(
      'DELETE',
      `/api/v1/memory?scope=${encodeURIComponent(scope)}&index=${index}${
        sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ''
      }`
    ),

  // 设置（M13 大模型配置）
  modelSettings: () => request('GET', '/api/v1/settings/model'),
  saveModelSettings: (payload) => request('PUT', '/api/v1/settings/model', payload),
  settings: {
    agentTeam: () => request('GET', '/api/v1/settings/agent-team'),
    saveAgentTeam: (payload) => request('PUT', '/api/v1/settings/agent-team', payload),
    agentTeamBudgets: () => request('GET', '/api/v1/settings/agent-team/budgets'),
    saveAgentTeamBudgets: payload => request('PUT', '/api/v1/settings/agent-team/budgets', payload),
  },
}

/** 读本地文件为 base64（插件 zip 上传用，免 multipart 依赖）。 */
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

/** 打开会话的 WebSocket 事件流（断线补齐 after_seq）。 */
export function openStream(sessionId, afterSeq = 0) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${location.host}/api/v1/sessions/${sessionId}/stream?token=${encodeURIComponent(getToken())}&after_seq=${afterSeq}`
  return new WebSocket(url)
}

/** PTY 流：after_seq 是最后已见序号，重连只补输出，不重发人工输入。 */
export function openTerminalStream(projectId, terminalId, afterSeq = 0) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const query = new URLSearchParams({ after_seq: String(Math.max(0, Math.trunc(Number(afterSeq) || 0))) })
  const token = getToken()
  if (token) query.set('token', token)
  return new WebSocket(`${proto}://${location.host}/api/v1/code/projects/${encodeURIComponent(projectId)}/terminals/${encodeURIComponent(terminalId)}/stream?${query}`)
}
