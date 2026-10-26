// 浏览器侧 LSP 客户端（M18 · P3）：WebSocket ↔ Monaco 的一层薄桥。
//
// 工作区持有客户端及多个文档；这里适配 JSON-RPC、版本、Monaco providers。
// 普通补全与单文件格式化操作编辑器模型；跨文件修改统一提交工作台预览，
// 不写磁盘、不执行语言服务器给出的任意命令。
//
// 协议约定：
// - URI 由**服务端**下发（file API 的 `uri` 字段），前端原样回传，绝不自己拼；
// - 服务端会在连接建立时先发 `code-agent/ready`（含 server/capabilities/已有诊断）；
// - 语言服务器起不来时发 `code-agent/error`，这里向上抛给 UI 显示原因与安装提示。

import * as monaco from './monacoRuntime.js'
import { getToken } from '../api.js'
import { hoverMarkdownStrings, normalizeHoverDocumentation } from './hoverDocumentation.js'
import { getTokenHover, registerTokenHoverSource, rememberSemanticTokens, clearSemanticTokens, notifyHoverDocumentationUpdated } from './tokenHover.js'

const LANG_ID = {
  python: 'python',
  javascript: 'javascript',
  typescript: 'typescript',
  vue: 'vue',
  json: 'json',
  yaml: 'yaml',
  toml: 'toml',
  shell: 'shell',
  go: 'go',
  rust: 'rust',
  c: 'c',
  cpp: 'cpp',
  java: 'java',
  csharp: 'csharp',
  php: 'php',
  ruby: 'ruby',
  kotlin: 'kotlin',
  swift: 'swift',
  css: 'css',
  scss: 'scss',
  less: 'less',
  html: 'html',
  markdown: 'markdown',
  sql: 'sql',
  terraform: 'terraform',
  dockerfile: 'dockerfile',
  zig: 'zig',
  dart: 'dart',
  lua: 'lua',
}

const SEVERITY = {
  1: monaco.MarkerSeverity.Error,
  2: monaco.MarkerSeverity.Warning,
  3: monaco.MarkerSeverity.Info,
  4: monaco.MarkerSeverity.Hint,
}
let clientSequence = 0

export function lspLanguageId(lang) {
  return LANG_ID[lang] || lang || 'plaintext'
}

export class LspClient {
  /**
   * @param {object} opts
   * @param {string} opts.sessionId 代码模式会话 id
   * @param {string} opts.projectId 项目 id
   * @param {string} opts.lang     语言（用后端 detect_language 的取值）
   * @param {(state: string, detail?: string) => void} opts.onStatus
   */
  constructor({ sessionId, projectId, tree = 'main', lang, serverId = '', onStatus, onDiagnostics, onCapabilities, onWorkspaceEdit, onNavigate, canEdit, autoReconnect = true, reconnectDelayMs = 500 }) {
    this.projectId = projectId
    this.tree = tree || 'main'
    this.lang = lang
    this.serverId = serverId
    this.onStatus = onStatus || (() => {})
    this.onDiagnostics = onDiagnostics || (() => {})
    this.onCapabilities = onCapabilities || (() => {})
    this.onWorkspaceEdit = onWorkspaceEdit
    this.onNavigate = onNavigate
    this.canEdit = canEdit || (() => true)
    this.autoReconnect = autoReconnect
    this.reconnectDelayMs = reconnectDelayMs
    this.reconnectTimer = null
    this.reconnectAttempts = 0
    this.failureDetail = ''
    this.ws = null
    this.nextId = 1
    this.pending = new Map()
    this.capabilities = {}
    this.server = ''
    this.environment = {}
    this.initializationMs = null
    this.documents = new Map() // uri → { version, model }
    this.serverDocumentVersions = new Map()
    this.diagnostics = new Map()
    this.diagnosticVersions = new Map()
    this.documentationVersions = new Map()
    this.resultMetadata = new WeakMap()
    this.completionMetadata = new WeakMap()
    this.actionMetadata = new WeakMap()
    this.hierarchyMetadata = new WeakMap()
    this.lensMetadata = new WeakMap()
    this.inlayMetadata = new WeakMap()
    this.featureGeneration = 0
    this.semanticGeneration = 0
    this.semanticTokenListeners = new Set()
    this.codeLensListeners = new Set()
    this.inlayHintListeners = new Set()
    this.disposables = []
    this.disposed = false
    this.ready = false
    this.readyWaiters = new Set()
    this.sessionId = sessionId || ''
    this.providersRequested = false
    this.providerLanguage = ''
    this.commandId = `code-agent.lsp.review.${++clientSequence}`
    this.lensCommandId = `${this.commandId}.navigation`
  }

  connect() {
    if (this.disposed || this.ws) return
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    this.failureDetail = ''
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const token = getToken()
    const url =
      `${proto}://${location.host}/api/v1/code/projects/${encodeURIComponent(this.projectId)}` +
      `/lsp/stream?lang=${encodeURIComponent(this.lang)}&tree=${encodeURIComponent(this.tree)}&token=${encodeURIComponent(token)}` +
      (this.serverId ? `&server=${encodeURIComponent(this.serverId)}` : '')
    this.onStatus('connecting')
    let ws
    try { ws = new WebSocket(url) }
    catch (error) { this.onStatus('error', error.message); this._reconnect(); return }
    this.ws = ws
    ws.onmessage = (event) => {
      if (this.disposed || this.ws !== ws) return
      let message = null
      try {
        message = JSON.parse(event.data)
      } catch {
        return
      }
      this._handle(message)
    }
    ws.onclose = (event = {}) => {
      if (this.ws !== ws) return
      this.ws = null
      this.ready = false
      this.semanticGeneration++
      clearSemanticTokens(this)
      this.documentationVersions.clear()
      this._rejectPending(new Error('LSP 通道已关闭'))
      const fatal = [4400, 4401, 4403, 4404, 4500, 4503].includes(event.code)
      if (!this.disposed) this.onStatus(fatal ? 'error' : 'closed', fatal ? this.failureDetail || event.reason || '语言服务不可用，请检查项目、访问权限或服务器安装。' : '')
      if (!fatal) this._reconnect()
    }
    ws.onerror = () => {
      if (this.disposed || this.ws !== ws) return
      this._rejectPending(new Error('LSP 连接失败'))
      this.onStatus('error')
    }
  }

  _reconnect() {
    if (this.disposed || !this.autoReconnect || this.reconnectTimer) return
    const delay = Math.min(10000, this.reconnectDelayMs * 2 ** this.reconnectAttempts++)
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.connect() }, delay)
  }

  get markerOwner() { return `code-agent-lsp:${this.serverId || this.server || this.lang}` }

  /** ready 通知即代表初始化完成；服务器可以合法返回空 capabilities。 */
  waitForReady(timeoutMs = 20000) {
    if (this.disposed) return Promise.reject(new Error('LSP 客户端已释放'))
    if (this.ready) return Promise.resolve()
    return new Promise((resolve, reject) => {
      const waiter = {
        resolve: () => { clearTimeout(timer); this.readyWaiters.delete(waiter); resolve() },
        reject: (error) => { clearTimeout(timer); this.readyWaiters.delete(waiter); reject(error) },
      }
      const timer = setTimeout(() => waiter.reject(new Error('LSP 初始化超时')), timeoutMs)
      this.readyWaiters.add(waiter)
    })
  }

  _rejectPending(error) {
    for (const waiter of this.readyWaiters) waiter.reject(error)
    for (const request of this.pending.values()) request.reject(error)
    this.pending.clear()
  }

  _handle(message) {
    if (this.disposed || !message || typeof message !== 'object') return
    const method = message.method
    if (method === 'code-agent/ready') {
      this.featureGeneration++
      this.semanticGeneration++
      clearSemanticTokens(this)
      this.server = message.params?.server || ''
      this.capabilities = message.params?.capabilities || {}
      this.environment = message.params?.environment || {}
      this.initializationMs = Number.isFinite(message.params?.initialization_ms) && message.params.initialization_ms >= 0
        ? message.params.initialization_ms : null
      this.serverDocumentVersions = new Map(Object.entries(message.params?.document_versions || {}).filter(([, version]) => Number.isInteger(version) && version >= 0))
      this.ready = true
      this.reconnectAttempts = 0
      for (const [uri, entry] of this.documents) {
        const text = entry.model?.getValue?.() ?? entry.text
        const serverVersion = this.serverDocumentVersions.get(uri) ?? 0
        if (text !== entry.text) { entry.text = text; entry.version = Math.max(entry.version + 1, serverVersion + 1) }
        else if (serverVersion > entry.version) entry.version = serverVersion + 1
        this._didOpen(uri, entry)
      }
      if (this.providersRequested) this.registerProviders(true)
      this.onCapabilities(this.capabilities)
      this.onStatus('ready', this.server)
      for (const waiter of this.readyWaiters) waiter.resolve()
      for (const [uri, items] of Object.entries(message.params?.diagnostics || {})) {
        this._publish(uri, items, message.params?.diagnostic_versions?.[uri])
      }
      return
    }
    if (method === 'code-agent/error') {
      const detail = message.params?.message || 'LSP 启动失败'
      this.failureDetail = detail
      this._rejectPending(new Error(detail))
      this.onStatus('error', detail)
      return
    }
    if (method === 'textDocument/publishDiagnostics') {
      this._publish(message.params?.uri, message.params?.diagnostics || [], message.params?.version)
      return
    }
    if (method === 'code-agent/documentationUpdated') {
      const uri = message.params?.uri, entry = this.documents.get(uri)
      if (typeof uri !== 'string' || !entry || entry.model?.isDisposed?.()) return
      if (Number.isInteger(message.params?.version) && message.params.version !== entry.version) return
      this.documentationVersions.set(uri, (this.documentationVersions.get(uri) || 0) + 1)
      notifyHoverDocumentationUpdated({ projectId: this.projectId, tree: this.tree, uri, version: entry.version, serverId: this.serverId,
        status: message.params.status, prewarm: message.params.prewarm,
        ...(validLspPosition(message.params.position) ? { position: message.params.position } : {}) })
      return
    }
    if (method === 'code-agent/reviewEdit') {
      this.reviewEdit(message.params?.edit, { title: message.params?.label || '语言服务修改', source: 'server', requestId: message.params?.request_id }).catch(() => {})
      return
    }
    if (message.id === undefined && ['workspace/semanticTokens/refresh', 'workspace/codeLens/refresh', 'workspace/inlayHint/refresh'].includes(method)) {
      const listeners = method === 'workspace/codeLens/refresh' ? this.codeLensListeners
        : method === 'workspace/inlayHint/refresh' ? this.inlayHintListeners : this.semanticTokenListeners
      if (method === 'workspace/semanticTokens/refresh') { this.semanticGeneration++; clearSemanticTokens(this) }
      for (const listener of [...listeners]) listener()
      return
    }
    // 服务端主动请求（进度条/注册能力）：按 LSP 要求回一个结果
    if (message.id !== undefined && method) {
      if (['workspace/semanticTokens/refresh', 'workspace/codeLens/refresh', 'workspace/inlayHint/refresh'].includes(method)) {
        const listeners = method === 'workspace/codeLens/refresh' ? this.codeLensListeners
          : method === 'workspace/inlayHint/refresh' ? this.inlayHintListeners : this.semanticTokenListeners
        if (method === 'workspace/semanticTokens/refresh') { this.semanticGeneration++; clearSemanticTokens(this) }
        for (const listener of [...listeners]) listener()
        this._send({ jsonrpc: '2.0', id: message.id, result: null })
        return
      }
      if (method === 'workspace/applyEdit') {
        this._send({ jsonrpc: '2.0', id: message.id, result: { applied: false, failureReason: '修改已提交预览，需人工确认；尚未写入磁盘。' } })
        this.reviewEdit(message.params?.edit, { title: message.params?.label || '语言服务修改', source: 'server' }).catch(() => {})
        return
      }
      const result = method === 'workspace/configuration'
        ? (message.params?.items || []).map(() => ({}))
        : null
      this._send({ jsonrpc: '2.0', id: message.id, result })
      return
    }
    if (message.id !== undefined) {
      const resolver = this.pending.get(message.id)
      if (!resolver) return
      this.pending.delete(message.id)
      if (message.error) resolver.reject(Object.assign(new Error(message.error.message || 'LSP 错误'), { code: message.error.code }))
      else resolver.resolve(message.result)
    }
  }

  _publish(uri, items, version) {
    if (!uri) return
    const entry = this.documents.get(uri)
    if (entry && ((Number.isInteger(version) && version < entry.version) || entry.model?.getValue?.() !== entry.text)) return
    const previous = this.diagnosticVersions.get(uri)
    if (Number.isInteger(version) && Number.isInteger(previous) && version < previous) return
    this.diagnosticVersions.set(uri, version)
    this.diagnostics.set(uri, items || [])
    const model = entry?.model
    if (model && !model.isDisposed?.()) {
      const markers = (items || []).map((item) => ({
        severity: SEVERITY[item.severity] ?? monaco.MarkerSeverity.Info,
        message: item.message || '',
        source: item.source || this.server || 'lsp',
        code: item.code != null ? String(item.code) : undefined,
        tags: item.tags,
        relatedInformation: (item.relatedInformation || []).filter(info => info.location?.uri).map(info => ({
          resource: monaco.Uri.parse(info.location.uri), message: info.message || '',
          startLineNumber: (info.location.range?.start?.line ?? 0) + 1,
          startColumn: (info.location.range?.start?.character ?? 0) + 1,
          endLineNumber: (info.location.range?.end?.line ?? 0) + 1,
          endColumn: (info.location.range?.end?.character ?? 0) + 1,
        })),
        startLineNumber: (item.range?.start?.line ?? 0) + 1,
        startColumn: (item.range?.start?.character ?? 0) + 1,
        endLineNumber: (item.range?.end?.line ?? 0) + 1,
        endColumn: (item.range?.end?.character ?? 0) + 1,
      }))
      monaco.editor.setModelMarkers(model, this.markerOwner, markers)
    }
    this.onDiagnostics(uri, items || [], version)
  }

  _send(payload) {
    if (!this.disposed && this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload))
      return true
    }
    return false
  }

  notify(method, params) {
    this._send({ jsonrpc: '2.0', method, params })
  }

  request(method, params, options) {
    // Hover may include model-backed documentation translation after the LSP
    // reply. Other queries keep their shorter interactive deadline.
    const { timeoutMs = method === 'textDocument/hover' ? 30000 : 15000, token } = typeof options === 'number' ? { timeoutMs: options } : options || {}
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      if (this.disposed || token?.isCancellationRequested) { reject(cancelledError()); return }
      let timer, cancellation
      const cleanup = () => { clearTimeout(timer); cancellation?.dispose?.(); this.pending.delete(id) }
      const cancel = () => {
        if (!this.pending.has(id)) return
        this.notify('$/cancelRequest', { id })
        cleanup(); reject(cancelledError())
      }
      this.pending.set(id, {
        resolve: value => { cleanup(); resolve(value) },
        reject: error => { cleanup(); reject(error) },
      })
      if (!this._send({ jsonrpc: '2.0', id, method, params })) { this.pending.get(id)?.reject(new Error('LSP 通道未连接')); return }
      if (!this.pending.has(id)) return
      timer = setTimeout(() => {
        this.notify('$/cancelRequest', { id })
        this.pending.delete(id)
        cleanup()
        reject(new Error(`LSP 请求超时：${method}`))
      }, timeoutMs)
      cancellation = token?.onCancellationRequested?.(cancel)
      if (!this.pending.has(id)) { clearTimeout(timer); cancellation?.dispose?.() }
      if (token?.isCancellationRequested) cancel()
    })
  }

  // ------------------------------------------------------------------
  // 文档同步
  // ------------------------------------------------------------------
  openDocument({ uri, text, model }) {
    if (!uri || this.disposed) return
    const existing = this.documents.get(uri)
    if (existing) {
      if (existing.model !== model) clearSemanticTokens(this, existing.model)
      existing.model = model; this.changeDocument(uri, model?.getValue?.() ?? text); return
    }
    const entry = { version: Math.max(1, (this.serverDocumentVersions.get(uri) ?? 0) + 1), model, text: model?.getValue?.() ?? text ?? '' }
    this.documents.set(uri, entry)
    if (this.ready) this._didOpen(uri, entry)
    if (this.diagnostics.has(uri)) this._publish(uri, this.diagnostics.get(uri), this.diagnosticVersions.get(uri))
    if (this.providersRequested && this.providerLanguage !== model?.getLanguageId?.()) this.registerProviders(true)
  }

  _didOpen(uri, entry) {
    this.notify('textDocument/didOpen', {
      textDocument: { uri, languageId: lspLanguageId(this.lang), version: entry.version, text: entry.text },
    })
  }

  changeDocument(uri, text) {
    const entry = this.documents.get(uri)
    if (!entry || text === entry.text) return
    clearSemanticTokens(this, entry.model)
    const previous = entry.text
    entry.version += 1
    entry.text = text
    entry.syncedModelVersion = null
    if (!this.ready) return
    const sync = this.capabilities.textDocumentSync
    const kind = typeof sync === 'number' ? sync : sync?.change ?? 1
    if (!kind) return
    const contentChanges = kind === 2 ? [incrementalChange(previous, text)] : [{ text }]
    this.notify('textDocument/didChange', {
      textDocument: { uri, version: entry.version },
      contentChanges,
    })
  }

  saveDocument(uri, text) {
    if (!this.documents.has(uri) || !this.ready) return
    this.syncDocuments()
    const sync = this.capabilities.textDocumentSync
    if (typeof sync === 'object' && sync.save === false) return
    const includeText = typeof sync !== 'object' || sync.save?.includeText === true
    this.notify('textDocument/didSave', { textDocument: { uri }, ...(includeText ? { text: text ?? this.documents.get(uri).text } : {}) })
  }

  closeDocument(uri) {
    const entry = this.documents.get(uri)
    if (entry) clearSemanticTokens(this, entry.model)
    if (entry?.model) monaco.editor.setModelMarkers(entry.model, this.markerOwner, [])
    this.documents.delete(uri)
    this.serverDocumentVersions.delete(uri)
    this.diagnostics.delete(uri)
    this.diagnosticVersions.delete(uri)
    this.documentationVersions.delete(uri)
    if (entry && this.ready) this.notify('textDocument/didClose', { textDocument: { uri } })
  }

  syncDocuments() {
    for (const [uri, entry] of this.documents) {
      if (entry.model?.isDisposed?.()) continue
      const model = entry.model, modelVersion = model?.getVersionId?.()
      // Keep all dirty drafts synchronized for cross-file queries, but avoid
      // allocating every unchanged large file for every completion/hover.
      if (Number.isInteger(modelVersion) && entry.syncedModel === model && entry.syncedModelVersion === modelVersion) continue
      const text = entry.model?.getValue?.() ?? entry.text
      this.changeDocument(uri, text)
      entry.syncedModel = model
      entry.syncedModelVersion = modelVersion
    }
  }

  captureDocuments() {
    return {
      versions: Object.fromEntries([...this.documents].map(([uri, entry]) => [uri, entry.version])),
      snapshots: Object.fromEntries([...this.documents].map(([uri, entry]) => [uri, entry.text])),
    }
  }

  editable(model) { try { return this.canEdit(model) !== false && !this.disposed && !model?.isDisposed?.() } catch { return false } }

  async _documentRequest(uri, method, params = {}, token, { editable = false } = {}) {
    const entry = this.documents.get(uri)
    if (!entry || (editable && !this.editable(entry.model))) throw new Error('当前文档不可编辑或已关闭')
    this.syncDocuments()
    const metadata = { ...this.captureDocuments(), uri, version: entry.version, model: entry.model }
    const result = await this.request(method, { textDocument: { uri }, ...params }, { token })
    if (this.disposed || token?.isCancellationRequested || this.documents.get(uri) !== entry || entry.model !== metadata.model
      || entry.version !== metadata.version || entry.model?.getValue?.() !== metadata.snapshots[uri]) throw cancelledError('文档已变化，请重试')
    if (editable && !this.editable(entry.model)) throw new Error('当前文档已改为只读')
    if (result && typeof result === 'object') {
      this.resultMetadata.set(result, metadata)
      if (Array.isArray(result)) for (const item of result) if (item && typeof item === 'object') {
        this.resultMetadata.set(item, metadata)
        if (item.edit && typeof item.edit === 'object') this.resultMetadata.set(item.edit, metadata)
      }
    }
    return result
  }

  symbols(uri, token) { return this._documentRequest(uri, 'textDocument/documentSymbol', {}, token) }
  definition(uri, position, token) { return this._documentRequest(uri, 'textDocument/definition', { position: fromPosition(position) }, token) }
  async hover(uri, position, token) {
    const entry = this.documents.get(uri), model = entry?.model
    if (model?.getLanguageId?.() === 'java') {
      if (this.disposed || model.isDisposed?.() || token?.isCancellationRequested) throw cancelledError()
      const local = getTokenHover(model, position)
      if (local?.localOnly || !this.capabilities.hoverProvider || !this.ready) return local
      this.syncDocuments()
      const version = entry.version, modelVersion = model.getVersionId?.(), text = entry.text, generation = this.featureGeneration
      const current = () => !this.disposed && !model.isDisposed?.() && !token?.isCancellationRequested
        && this.documents.get(uri) === entry && entry.model === model && entry.version === version
        && model.getVersionId?.() === modelVersion && model.getValue?.() === text && this.featureGeneration === generation
      try {
        const result = await this._documentRequest(uri, 'textDocument/hover', { position: fromPosition(position) }, token)
        if (!current()) throw cancelledError('文档已变化，请重试')
        return getTokenHover(model, position, result)
      } catch (error) {
        if (error?.name === 'CancellationError' || error?.code === -32800 || !current()) throw cancelledError('文档已变化或请求已取消')
        // Server failures can still leave useful lexical help for this exact
        // draft. Never revive an obsolete hover after typing or navigation.
        return getTokenHover(model, position)
      }
    }
    if (!this.capabilities.hoverProvider) return null
    const result = await this._documentRequest(uri, 'textDocument/hover', { position: fromPosition(position) }, token)
    if (!result) return null
    // Background cache growth does not change this document's source. Let the
    // current hover finish; an open details view coalesces a later refresh.
    return getTokenHover(model, position, result)
  }
  async workspaceSymbols(query, token) {
    this.syncDocuments()
    const snapshots = [...this.documents].map(([uri, entry]) => ({ uri, entry, model: entry.model, version: entry.version, text: entry.text }))
    const result = await this.request('workspace/symbol', { query }, { token })
    if (snapshots.some(item => this.documents.get(item.uri) !== item.entry || item.entry.model !== item.model
      || item.entry.version !== item.version || item.model?.getValue?.() !== item.text)) throw cancelledError('工作区文档已变化，请重试')
    return result
  }
  formatDocument(uri, options = { tabSize: 4, insertSpaces: true }, token) { return this._documentRequest(uri, 'textDocument/formatting', { options }, token, { editable: true }) }
  codeActions(uri, range, context = {}, token) { return this._documentRequest(uri, 'textDocument/codeAction', { range: fromRange(range), context }, token, { editable: true }) }
  resolveCodeAction(action, token) { return this._resolve('codeAction/resolve', action, this.resultMetadata.get(action), token) }
  rename(uri, position, newName, token) { return this._documentRequest(uri, 'textDocument/rename', { position: fromPosition(position), newName }, token, { editable: true }) }

  // Monaco exposes CodeLens/inlay providers but no public hierarchy provider.
  // Keep server-issued hierarchy handles opaque (including `data`); only handles
  // produced by this client's current document request can be expanded.
  async prepareHierarchy(uri, position, kind = 'call', token) {
    if (!['call', 'type'].includes(kind) || !this.capabilities[`${kind}HierarchyProvider`]) return []
    const generation = this.featureGeneration
    const result = await this._documentRequest(uri, kind === 'call' ? 'textDocument/prepareCallHierarchy' : 'textDocument/prepareTypeHierarchy', { position: fromPosition(position) }, token)
    const metadata = this.resultMetadata.get(result)
    if (!this._advancedCurrent({ metadata, generation })) throw cancelledError()
    return (Array.isArray(result) ? result : []).filter(validHierarchyItem).map(item => {
      this.hierarchyMetadata.set(item, { metadata, generation, kind })
      return item
    })
  }

  async hierarchyChildren(item, direction, token) {
    const info = this.hierarchyMetadata.get(item)
    const methods = info?.kind === 'call'
      ? { incoming: 'callHierarchy/incomingCalls', outgoing: 'callHierarchy/outgoingCalls' }
      : info?.kind === 'type' ? { incoming: 'typeHierarchy/supertypes', outgoing: 'typeHierarchy/subtypes' } : {}
    if (!methods[direction] || !this.capabilities[`${info?.kind}HierarchyProvider`] || !this._advancedCurrent(info)) throw cancelledError('层级已过期，请重新查询')
    const result = await this.request(methods[direction], { item }, { token })
    if (token?.isCancellationRequested || !this._advancedCurrent(info)) throw cancelledError('文档已变化，请重新查询')
    return (Array.isArray(result) ? result : []).filter(raw => validHierarchyItem(info.kind === 'call' ? raw?.[direction === 'incoming' ? 'from' : 'to'] : raw)).map(raw => {
      const child = info.kind === 'call' ? raw[direction === 'incoming' ? 'from' : 'to'] : raw
      this.hierarchyMetadata.set(child, info)
      return { item: child, ranges: info.kind === 'call' ? (raw.fromRanges || []).filter(validLspRange) : [],
        rangeUri: info.kind === 'call' && direction === 'outgoing' ? item.uri : child.uri }
    })
  }

  _advancedCurrent(info) {
    const metadata = info?.metadata
    if (!metadata?.uri || this.disposed || !this.ready || info.generation !== this.featureGeneration) return false
    this.syncDocuments()
    if (this.documents.get(metadata.uri)?.model !== metadata.model || metadata.model?.isDisposed?.()) return false
    return Object.entries(metadata.versions).every(([uri, version]) => {
      const entry = this.documents.get(uri)
      return entry && entry.version === version && !entry.model?.isDisposed?.() && entry.text === metadata.snapshots[uri]
    })
  }

  async _resolveAdvanced(method, raw, info, token) {
    if (!this._advancedCurrent(info) || token?.isCancellationRequested) throw cancelledError()
    const result = await this.request(method, raw, { token })
    if (!this._advancedCurrent(info) || token?.isCancellationRequested) throw cancelledError()
    return result
  }

  _codeLens(raw, info) {
    const lens = { range: toRange(raw.range) }
    this.lensMetadata.set(lens, { ...info, raw })
    if (raw.command) {
      const navigation = lensNavigation(raw.command)
      // Never forward a server command ID to Monaco's command service. Known
      // reference locations use our document/version-checked local handler.
      lens.command = { title: String(raw.command.title || '代码信息'), id: navigation && typeof this.onNavigate === 'function' ? this.lensCommandId : '', arguments: [lens],
        ...(!navigation ? { tooltip: '语言服务命令尚未接入；此处仅显示信息。' } : {}) }
    }
    return lens
  }

  async activateCodeLens(lens) {
    const info = this.lensMetadata.get(lens)
    if (!this._advancedCurrent(info) || typeof this.onNavigate !== 'function') return false
    const navigation = lensNavigation(info.raw.command)
    if (!navigation) return false
    return (await this.onNavigate({ ...navigation, title: info.raw.command.title || '引用' })) !== false
  }

  _inlayHint(raw, info) {
    const label = typeof raw.label === 'string' ? raw.label : (raw.label || []).filter(part => typeof part?.value === 'string').map(part => ({
      label: part.value, tooltip: documentation(part.tooltip),
      ...(validLocation(part.location) ? { location: { uri: monaco.Uri.parse(part.location.uri), range: toRange(part.location.range) } } : {}),
    }))
    const hint = { label, position: { lineNumber: raw.position.line + 1, column: raw.position.character + 1 },
      tooltip: documentation(raw.tooltip), ...(raw.kind === 1 || raw.kind === 2 ? { kind: raw.kind } : {}),
      paddingLeft: raw.paddingLeft === true, paddingRight: raw.paddingRight === true }
    // Accepting an inlay hint may edit a draft. Expose only well-formed text
    // edits on writable owned models; hint labels never execute commands.
    if (this.editable(info.metadata?.model) && Array.isArray(raw.textEdits)) hint.textEdits = raw.textEdits.filter(edit => validLspRange(edit?.range) && typeof edit.newText === 'string').map(textEdit)
    this.inlayMetadata.set(hint, { ...info, raw })
    return hint
  }

  async reviewEdit(edit, context = {}) {
    if (this.disposed || !edit || typeof this.onWorkspaceEdit !== 'function') return false
    const metadata = context.metadata || this.resultMetadata.get(edit)
    if (!metadata) this.syncDocuments()
    const uri = context.uri || metadata?.uri
    if (uri && (!this.documents.has(uri) || !this.editable(this.documents.get(uri).model))) return false
    const targets = [...Object.keys(edit.changes || {}), ...(edit.documentChanges || []).map(change => change.textDocument?.uri).filter(Boolean)]
    if (targets.some(target => this.documents.has(target) && !this.editable(this.documents.get(target).model))) return false
    if (metadata && uri && (this.documents.get(uri)?.model !== metadata.model || this.documents.get(uri)?.version !== metadata.version
      || metadata.model?.getValue?.() !== metadata.snapshots[uri])) return false
    const { model, ...snapshot } = metadata || this.captureDocuments()
    const { metadata: ignored, ...details } = context
    const accepted = await this.onWorkspaceEdit({ ...snapshot, ...details, edit, uri, source: context.source || 'language' })
    return !this.disposed && accepted !== false
  }

  // ------------------------------------------------------------------
  // Monaco 能力接线
  // ------------------------------------------------------------------
  registerProviders(force = false) {
    this.providersRequested = true
    if (this.disposed || !this.ready || (!force && this.disposables.length)) return
    for (const item of this.disposables.splice(0)) item.dispose?.()
    this.semanticGeneration++
    clearSemanticTokens(this)
    const langId = this.documents.values().next().value?.model?.getLanguageId() || lspLanguageId(this.lang)
    this.providerLanguage = langId
    const caps = this.capabilities
    const register = (name, provider, metadata) => {
      const disposable = monaco.languages[name]?.(langId, provider, metadata)
      if (disposable) this.disposables.push(disposable)
    }
    const provide = async (model, method, params, token, fallback, editable = false) => {
      const uri = this._uriOf(model)
      if (!uri) return fallback
      try { return await this._documentRequest(uri, method, params, token, { editable }) }
      catch { return fallback }
    }

    if (caps.codeLensProvider) {
      const command = monaco.editor.registerCommand?.(this.lensCommandId, (_accessor, lens) => this.activateCodeLens(lens))
      if (command) this.disposables.push(command)
      register('registerCodeLensProvider', {
        onDidChange: listener => { this.codeLensListeners.add(listener); return { dispose: () => this.codeLensListeners.delete(listener) } },
        provideCodeLenses: async (model, token) => {
          const generation = this.featureGeneration
          const result = await provide(model, 'textDocument/codeLens', {}, token, [])
          const info = { metadata: this.resultMetadata.get(result), generation }
          return { lenses: this._advancedCurrent(info) ? (Array.isArray(result) ? result : []).filter(raw => validLspRange(raw?.range)).map(raw => this._codeLens(raw, info)) : [], dispose() {} }
        },
        ...(caps.codeLensProvider.resolveProvider ? { resolveCodeLens: async (model, lens, token) => {
          const info = this.lensMetadata.get(lens)
          if (!info || this._uriOf(model) !== info.metadata?.uri) return { ...lens, command: undefined }
          if (info.raw.command) return this._advancedCurrent(info) ? lens : { ...lens, command: undefined }
          try { return this._codeLens({ ...info.raw, ...await this._resolveAdvanced('codeLens/resolve', info.raw, info, token) }, info) }
          catch { return { ...lens, command: undefined } }
        } } : {}),
      })
      this.disposables.push({ dispose: () => this.codeLensListeners.clear() })
    }
    if (caps.inlayHintProvider) {
      register('registerInlayHintsProvider', {
        displayName: this.server || this.lang,
        onDidChangeInlayHints: listener => { this.inlayHintListeners.add(listener); return { dispose: () => this.inlayHintListeners.delete(listener) } },
        provideInlayHints: async (model, range, token) => {
          const generation = this.featureGeneration
          const result = await provide(model, 'textDocument/inlayHint', { range: fromRange(range) }, token, [])
          const info = { metadata: this.resultMetadata.get(result), generation }
          return { hints: this._advancedCurrent(info) ? (Array.isArray(result) ? result : []).filter(validInlayHint).map(raw => this._inlayHint(raw, info)) : [], dispose() {} }
        },
        ...(caps.inlayHintProvider.resolveProvider ? { resolveInlayHint: async (hint, token) => {
          const info = this.inlayMetadata.get(hint)
          if (!info) return hint
          try { return this._inlayHint({ ...info.raw, ...await this._resolveAdvanced('inlayHint/resolve', info.raw, info, token) }, info) }
          catch { return { ...hint, textEdits: undefined, label: typeof hint.label === 'string' ? hint.label : hint.label.map(part => ({ label: part.label })) } }
        } } : {}),
      })
      this.disposables.push({ dispose: () => this.inlayHintListeners.clear() })
    }

    // Semantic colours refine the lexical grammar only when the actual server
    // advertises a valid legend. Scope requests to this client's own documents;
    // cancelled/stale responses fall back to syntax rather than recolour drafts.
    const semantic = caps.semanticTokensProvider
    const legend = semanticLegend(semantic?.legend)
    if (legend && (semantic.full || semantic.range)) {
      const semanticProvider = {
        getLegend: () => legend,
        onDidChange: listener => {
          this.semanticTokenListeners.add(listener)
          return { dispose: () => this.semanticTokenListeners.delete(listener) }
        },
      }
      const tokens = async (model, method, params, token) => {
        const generation = this.semanticGeneration
        const result = await provide(model, method, params, token, null)
        if (generation !== this.semanticGeneration || this.disposed || !this.ready || !this._uriOf(model)) return null
        const validated = toSemanticTokens(result, legend, model)
        if (validated) rememberSemanticTokens(model, validated, legend, this)
        return validated
      }
      if (semantic.full) register('registerDocumentSemanticTokensProvider', {
        ...semanticProvider,
        provideDocumentSemanticTokens: (model, _lastResultId, token) => tokens(model, 'textDocument/semanticTokens/full', {}, token),
        releaseDocumentSemanticTokens() {},
      })
      if (semantic.range) register('registerDocumentRangeSemanticTokensProvider', {
        ...semanticProvider,
        provideDocumentRangeSemanticTokens: (model, range, token) => tokens(model, 'textDocument/semanticTokens/range', { range: fromRange(range) }, token),
      })
      this.disposables.push({ dispose: () => { this.semanticTokenListeners.clear(); clearSemanticTokens(this) } })
    }

    if (caps.completionProvider) register('registerCompletionItemProvider', {
      triggerCharacters: caps.completionProvider.triggerCharacters || [],
      provideCompletionItems: async (model, position, context, token) => {
        const result = await provide(model, 'textDocument/completion', {
          position: fromPosition(position),
          context: { triggerKind: (context?.triggerKind ?? 0) + 1, ...(context?.triggerCharacter ? { triggerCharacter: context.triggerCharacter } : {}) },
        }, token, null, true)
        const items = Array.isArray(result) ? result : result?.items || []
        const metadata = result && this.resultMetadata.get(result)
        return { incomplete: Boolean(result?.isIncomplete), suggestions: items.map(item => {
          const suggestion = completionItem(item, result?.itemDefaults, model, position)
          const defaults = result?.itemDefaults || {}
          const resolveDefaults = Object.fromEntries(['data', 'commitCharacters', 'insertTextFormat', 'insertTextMode'].filter(key => defaults[key] !== undefined).map(key => [key, defaults[key]]))
          this.completionMetadata.set(suggestion, { raw: { ...resolveDefaults, ...item }, defaults, model, position, metadata })
          return suggestion
        }) }
      },
      ...(caps.completionProvider.resolveProvider ? { resolveCompletionItem: async (item, token) => {
        const info = this.completionMetadata.get(item)
        if (!info) return item
        try {
          const resolved = await this._resolve('completionItem/resolve', info.raw, info.metadata, token)
          const converted = completionItem({ ...info.raw, ...resolved }, info.defaults, info.model, info.position)
          this.completionMetadata.set(converted, { ...info, raw: { ...info.raw, ...resolved } })
          return converted
        } catch { return item }
      } } : {}),
    })

    if (langId === 'java') this.disposables.push(registerTokenHoverSource({
      matches: model => this.ready && !this.disposed && model?.getLanguageId?.() === 'java' && !!this._uriOf(model),
      provide: (model, position, token) => {
        const uri = this._uriOf(model)
        return uri ? this.hover(uri, position, token) : null
      },
    }))
    else if (caps.hoverProvider) register('registerHoverProvider', {
      provideHover: async (model, position, token) => {
        const uri = this._uriOf(model)
        if (!uri) return null
        try { return await this.hover(uri, position, token) }
        catch { return null }
      },
    })
    for (const [capability, registration, method, handler] of [
      ['definitionProvider', 'registerDefinitionProvider', 'textDocument/definition', 'provideDefinition'],
      ['declarationProvider', 'registerDeclarationProvider', 'textDocument/declaration', 'provideDeclaration'],
      ['typeDefinitionProvider', 'registerTypeDefinitionProvider', 'textDocument/typeDefinition', 'provideTypeDefinition'],
      ['implementationProvider', 'registerImplementationProvider', 'textDocument/implementation', 'provideImplementation'],
    ]) if (caps[capability]) register(registration, {
      [handler]: async (model, position, token) => {
        const result = await provide(model, method, { position: fromPosition(position) }, token, null)
        return (Array.isArray(result) ? result : result ? [result] : []).filter(item => item?.uri || item?.targetUri).map(item => ({
          uri: monaco.Uri.parse(item.uri || item.targetUri), range: toRange(item.targetSelectionRange || item.range || item.targetRange),
        }))
      },
    })
    if (caps.referencesProvider) register('registerReferenceProvider', {
      provideReferences: async (model, position, context, token) => {
        const result = await provide(model, 'textDocument/references', { position: fromPosition(position), context: { includeDeclaration: context?.includeDeclaration !== false } }, token, [])
        return (Array.isArray(result) ? result : []).filter(item => item.uri).map(item => ({ uri: monaco.Uri.parse(item.uri), range: toRange(item.range) }))
      },
    })
    if (caps.signatureHelpProvider) register('registerSignatureHelpProvider', {
      signatureHelpTriggerCharacters: caps.signatureHelpProvider.triggerCharacters || [],
      signatureHelpRetriggerCharacters: caps.signatureHelpProvider.retriggerCharacters || [],
      provideSignatureHelp: async (model, position, token, context) => {
        const result = await provide(model, 'textDocument/signatureHelp', { position: fromPosition(position), ...(context ? { context } : {}) }, token, null)
        if (!result) return null
        return { value: { ...result, activeSignature: result.activeSignature ?? 0, activeParameter: result.activeParameter ?? 0,
          signatures: (result.signatures || []).map(signature => ({ ...signature, documentation: documentation(signature.documentation), parameters: (signature.parameters || []).map(parameter => ({ ...parameter, documentation: documentation(parameter.documentation) })) })),
        }, dispose() {} }
      },
    })
    if (caps.documentSymbolProvider) register('registerDocumentSymbolProvider', {
      displayName: this.server || this.lang,
      provideDocumentSymbols: async (model, token) => (await provide(model, 'textDocument/documentSymbol', {}, token, []) || []).map(documentSymbol),
    })
    if (caps.documentFormattingProvider) register('registerDocumentFormattingEditProvider', {
      displayName: this.server || this.lang,
      provideDocumentFormattingEdits: async (model, options, token) => (await provide(model, 'textDocument/formatting', { options }, token, [], true) || []).map(textEdit),
    })
    if (caps.documentRangeFormattingProvider) register('registerDocumentRangeFormattingEditProvider', {
      displayName: this.server || this.lang,
      provideDocumentRangeFormattingEdits: async (model, range, options, token) => (await provide(model, 'textDocument/rangeFormatting', { range: fromRange(range), options }, token, [], true) || []).map(textEdit),
    })
    if (caps.codeActionProvider) {
      const command = monaco.editor.registerCommand?.(this.commandId, (_accessor, action) => this.applyCodeAction(action))
      if (command) this.disposables.push(command)
      register('registerCodeActionProvider', {
        provideCodeActions: async (model, range, context, token) => {
          const result = await provide(model, 'textDocument/codeAction', {
            range: fromRange(range), context: { diagnostics: this.diagnostics.get(this._uriOf(model)) || (context?.markers || []).map(lspDiagnostic), ...(context?.only ? { only: [context.only] } : {}), triggerKind: context?.trigger || 1 },
          }, token, [], true)
          const metadata = result && this.resultMetadata.get(result)
          return { actions: (result || []).map(raw => this._codeAction(raw, model, metadata)), dispose() {} }
        },
        ...(caps.codeActionProvider.resolveProvider ? { resolveCodeAction: async (action, token) => {
          const info = this.actionMetadata.get(action)
          if (!info) return action
          try { return this._codeAction(await this._resolve('codeAction/resolve', info.raw, info.metadata, token), info.model, info.metadata) }
          catch { return { ...action, disabled: '文档已变化或修复不可用，请重新获取。' } }
        } } : {}),
      }, { providedCodeActionKinds: caps.codeActionProvider.codeActionKinds || ['quickfix', 'refactor', 'source'] })
    }
    if (caps.renameProvider) register('registerRenameProvider', {
      resolveRenameLocation: async (model, position, token) => {
        if (!this._uriOf(model) || !this.editable(model)) return { rejectReason: '当前文档只读。', range: wordRange(model, position), text: '' }
        if (!caps.renameProvider.prepareProvider) return { range: wordRange(model, position), text: model.getWordAtPosition?.(position)?.word || '' }
        const result = await provide(model, 'textDocument/prepareRename', { position: fromPosition(position) }, token, null, true)
        if (!result) return { rejectReason: '此位置不能重命名。', range: wordRange(model, position), text: '' }
        if (result.defaultBehavior) return { range: wordRange(model, position), text: model.getWordAtPosition?.(position)?.word || '' }
        const range = toRange(result.range || result)
        return { range, text: result.placeholder || model.getValueInRange?.(range) || '' }
      },
      provideRenameEdits: async (model, position, newName, token) => {
        const edit = await provide(model, 'textDocument/rename', { position: fromPosition(position), newName }, token, null, true)
        if (!edit) return { edits: [], rejectReason: '重命名不可用或文档已变化。' }
        const accepted = await this.reviewEdit(edit, { title: `重命名为 ${newName}`, source: 'rename' })
        return accepted ? { edits: [] } : { edits: [], rejectReason: '无法预览修改，请检查编辑权限。' }
      },
    })
  }

  async _resolve(method, raw, metadata, token) {
    if (!metadata?.uri || this.documents.get(metadata.uri)?.model !== metadata.model || this.documents.get(metadata.uri)?.version !== metadata.version || !this.editable(metadata.model)
      || metadata.model.getValue() !== metadata.snapshots[metadata.uri]) throw cancelledError()
    this.syncDocuments()
    const result = await this.request(method, raw, { token })
    if (this.disposed || token?.isCancellationRequested || this.documents.get(metadata.uri)?.model !== metadata.model || this.documents.get(metadata.uri)?.version !== metadata.version
      || metadata.model.getValue() !== metadata.snapshots[metadata.uri] || !this.editable(metadata.model)) throw cancelledError()
    if (result && typeof result === 'object') {
      this.resultMetadata.set(result, metadata)
      if (result.edit && typeof result.edit === 'object') this.resultMetadata.set(result.edit, metadata)
    }
    return result
  }

  _codeAction(raw, model, metadata) {
    const resolvable = !raw.edit && raw.data !== undefined && this.capabilities.codeActionProvider?.resolveProvider
    const reviewCommand = this._reviewCommand(raw, metadata)
    const action = { title: raw.title || '语言修复', kind: raw.kind, isPreferred: raw.isPreferred,
      ...(raw.disabled ? { disabled: raw.disabled.reason || '此操作不可用。' } : !raw.edit && !resolvable && !reviewCommand ? { disabled: '此操作仅提供服务器命令，当前不执行。' } : {}),
    }
    this.actionMetadata.set(action, { raw, model, metadata })
    action.command = { id: this.commandId, title: action.title, arguments: [action] }
    return action
  }

  _reviewCommand(raw, metadata) {
    const command = typeof raw?.command === 'string' ? raw : raw?.command
    const advertised = this.capabilities.executeCommandProvider?.commands
    if (command?.command !== 'java.edit.organizeImports' || !Array.isArray(advertised) || !advertised.includes(command.command)
      || !metadata?.uri || !Array.isArray(command.arguments) || command.arguments.length !== 1 || command.arguments[0] !== metadata.uri) return null
    return { command: command.command, arguments: [metadata.uri] }
  }

  async applyCodeAction(action, token) {
    const info = this.actionMetadata.get(action)
    if (!info || action.disabled || !this.editable(info.model)) return false
    let raw = info.raw
    if (!raw.edit && this.capabilities.codeActionProvider?.resolveProvider) {
      try { raw = await this._resolve('codeAction/resolve', raw, info.metadata, token) } catch { return false }
    }
    if (!raw.edit) {
      const command = this._reviewCommand(raw, info.metadata)
      const guard = { metadata: info.metadata, generation: this.featureGeneration }
      if (!command || !this._advancedCurrent(guard) || token?.isCancellationRequested) return false
      let edit
      try { edit = await this.request('workspace/executeCommand', command, { token }) } catch { return false }
      if (!this._advancedCurrent(guard) || !this.editable(info.model) || token?.isCancellationRequested || !edit || typeof edit !== 'object' || !(edit.changes || edit.documentChanges)) return false
      return this.reviewEdit(edit, { metadata: info.metadata, title: raw.title || action.title, source: 'organizeImports' })
    }
    return this.reviewEdit(raw.edit, { metadata: info.metadata, title: raw.title || action.title, source: 'codeAction' })
  }

  _uriOf(model) {
    for (const [uri, entry] of this.documents.entries()) {
      if (entry.model === model) return uri
    }
    return null
  }

  dispose() {
    if (this.disposed) return
    this.disposed = true
    this.ready = false
    this.semanticGeneration++
    clearSemanticTokens(this)
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this._rejectPending(new Error('LSP 客户端已释放'))
    for (const item of this.disposables.splice(0)) {
      try {
        item.dispose()
      } catch {
        /* 忽略：组件卸载时 dispose 失败不该影响其它清理 */
      }
    }
    for (const [, entry] of this.documents) {
      if (entry.model && !entry.model.isDisposed?.()) monaco.editor.setModelMarkers(entry.model, this.markerOwner, [])
    }
    this.documents.clear()
    this.serverDocumentVersions.clear()
    this.diagnostics.clear()
    this.diagnosticVersions.clear()
    this.documentationVersions.clear()
    this.semanticTokenListeners.clear()
    this.codeLensListeners.clear()
    this.inlayHintListeners.clear()
    if (this.ws) {
      this.ws.onclose = null
      this.ws.onmessage = null
      this.ws.onerror = null
      this.ws.close()
      this.ws = null
    }
  }
}

function validLspPosition(position) { return Number.isInteger(position?.line) && position.line >= 0 && Number.isInteger(position?.character) && position.character >= 0 }
function validLspRange(range) { return validLspPosition(range?.start) && validLspPosition(range?.end) && (range.end.line > range.start.line || range.end.line === range.start.line && range.end.character >= range.start.character) }
function validLocation(location) { return typeof location?.uri === 'string' && /^file:\/\//i.test(location.uri) && validLspRange(location.range) }
function validHierarchyItem(item) { return typeof item?.uri === 'string' && !!item.uri && typeof item.name === 'string' && validLspRange(item.range) && validLspRange(item.selectionRange) }
function validInlayHint(hint) { return validLspPosition(hint?.position) && (typeof hint.label === 'string' && !!hint.label || Array.isArray(hint.label) && hint.label.some(part => typeof part?.value === 'string' && !!part.value)) }
function lensNavigation(command) {
  if (!command || !['editor.action.showReferences', 'java.show.references', 'java.show.implementations'].includes(command.command) || !Array.isArray(command.arguments)) return null
  const [uri, position, locations] = command.arguments
  if (typeof uri !== 'string' || !/^file:\/\//i.test(uri) || !validLspPosition(position) || !Array.isArray(locations) || locations.length > 5000 || locations.some(location => !validLocation(location))) return null
  return { kind: 'references', uri, position: { lineNumber: position.line + 1, column: position.character + 1 },
    locations: locations.map(location => ({ uri: location.uri, range: toRange(location.range) })) }
}

function semanticLegend(legend) {
  if (!Array.isArray(legend?.tokenTypes) || !legend.tokenTypes.length || legend.tokenTypes.length > 65535
    || !Array.isArray(legend.tokenModifiers) || legend.tokenModifiers.length > 31
    || [...legend.tokenTypes, ...legend.tokenModifiers].some(name => typeof name !== 'string' || !name || name.length > 128)) return null
  return { tokenTypes: [...legend.tokenTypes], tokenModifiers: [...legend.tokenModifiers] }
}

function toSemanticTokens(result, legend, model) {
  if (!result || !Array.isArray(result.data) || result.data.length % 5 || result.data.length > 1000000) return null
  const fallbackLines = model.getLineCount && model.getLineLength ? null : model.getValue().split(/\r\n|\r|\n/)
  const count = model.getLineCount?.() ?? fallbackLines.length
  const modifierMask = 2 ** legend.tokenModifiers.length - 1
  let line = 0, column = 0, previousEnd = 0
  for (let offset = 0; offset < result.data.length; offset += 5) {
    for (let index = offset; index < offset + 5; index++) {
      const value = result.data[index]
      if (!Number.isInteger(value) || value < 0 || value > 0xFFFFFFFF) return null
    }
    const deltaLine = result.data[offset], deltaStart = result.data[offset + 1], length = result.data[offset + 2]
    if (!length || result.data[offset + 3] >= legend.tokenTypes.length || result.data[offset + 4] > modifierMask) return null
    line += deltaLine
    column = deltaLine ? deltaStart : column + deltaStart
    if ((!deltaLine && column < previousEnd) || line >= count || column + length > (model.getLineLength?.(line + 1) ?? fallbackLines[line].length)) return null
    previousEnd = column + length
  }
  return { data: Uint32Array.from(result.data), ...(typeof result.resultId === 'string' ? { resultId: result.resultId } : {}) }
}

function toRange(range) {
  if (!range) return new monaco.Range(1, 1, 1, 1)
  return new monaco.Range(
    (range.start?.line ?? 0) + 1,
    (range.start?.character ?? 0) + 1,
    (range.end?.line ?? 0) + 1,
    (range.end?.character ?? 0) + 1
  )
}

function cancelledError(message = 'LSP 请求已取消') { return Object.assign(new Error(message), { name: 'CancellationError', code: -32800 }) }
function fromPosition(position) {
  return position?.lineNumber != null ? { line: position.lineNumber - 1, character: position.column - 1 } : position
}
function fromRange(range) {
  return range?.start ? range : { start: { line: (range?.startLineNumber ?? 1) - 1, character: (range?.startColumn ?? 1) - 1 }, end: { line: (range?.endLineNumber ?? 1) - 1, character: (range?.endColumn ?? 1) - 1 } }
}
function insideTextUnit(text, offset) {
  if (offset <= 0 || offset >= text.length) return false
  const before = text.charCodeAt(offset - 1), after = text.charCodeAt(offset)
  return (before === 13 && after === 10)
    || (before >= 0xd800 && before <= 0xdbff && after >= 0xdc00 && after <= 0xdfff)
}
function textPosition(text, offset) {
  let line = 0, character = 0
  for (let index = 0; index < offset; index++) {
    const code = text.charCodeAt(index)
    if (code === 13 || code === 10) {
      if (code === 13 && text.charCodeAt(index + 1) === 10) index++
      line++; character = 0
    } else character++ // LSP positions use UTF-16 code units, including surrogate pairs.
  }
  return { line, character }
}
function incrementalChange(previous, text) {
  let start = 0
  const sharedLength = Math.min(previous.length, text.length)
  while (start < sharedLength && previous.charCodeAt(start) === text.charCodeAt(start)) start++
  // Ranges cannot address the middle of a CRLF. Keep both CRLF and Unicode pairs
  // intact when the matching prefix/suffix happens to stop between their units.
  while (insideTextUnit(previous, start) || insideTextUnit(text, start)) start--
  let oldEnd = previous.length, newEnd = text.length
  while (oldEnd > start && newEnd > start && previous.charCodeAt(oldEnd - 1) === text.charCodeAt(newEnd - 1)) {
    oldEnd--; newEnd--
  }
  while (insideTextUnit(previous, oldEnd) || insideTextUnit(text, newEnd)) { oldEnd++; newEnd++ }
  return {
    range: { start: textPosition(previous, start), end: textPosition(previous, oldEnd) },
    rangeLength: oldEnd - start,
    text: text.slice(start, newEnd),
  }
}
function wordRange(model, position) {
  const word = model.getWordUntilPosition(position)
  return new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn)
}
function documentation(value) {
  if (!value) return undefined
  if (typeof value === 'string') return value
  return value.kind === 'plaintext' ? value.value || '' : { value: value.value || '', isTrusted: false }
}
const completionKinds = ['Text', 'Method', 'Function', 'Constructor', 'Field', 'Variable', 'Class', 'Interface', 'Module', 'Property', 'Unit', 'Value', 'Enum', 'Keyword', 'Snippet', 'Color', 'File', 'Reference', 'Folder', 'EnumMember', 'Constant', 'Struct', 'Event', 'Operator', 'TypeParameter']
function completionItem(item, defaults = {}, model, position) {
  defaults ||= {}
  const raw = { ...defaults, ...item }
  const edit = item.textEdit || (defaults.editRange ? { ...((defaults.editRange.insert && defaults.editRange.replace) ? defaults.editRange : { range: defaults.editRange }), newText: item.textEditText ?? item.insertText ?? item.label ?? '' } : null)
  const range = edit?.insert && edit?.replace ? { insert: toRange(edit.insert), replace: toRange(edit.replace) } : edit?.range ? toRange(edit.range) : wordRange(model, position)
  return {
    label: item.labelDetails ? { label: item.label || '', detail: item.labelDetails.detail, description: item.labelDetails.description } : item.label || item.insertText || '',
    kind: monaco.languages.CompletionItemKind[completionKinds[(item.kind || 1) - 1]] ?? monaco.languages.CompletionItemKind.Text,
    detail: item.detail || '', documentation: documentation(item.documentation),
    insertText: edit?.newText ?? item.insertText ?? item.label ?? '', range,
    insertTextRules: (raw.insertTextFormat === 2 ? monaco.languages.CompletionItemInsertTextRule?.InsertAsSnippet ?? 4 : 0) | (raw.insertTextMode === 1 ? monaco.languages.CompletionItemInsertTextRule?.KeepWhitespace ?? 1 : 0),
    sortText: item.sortText, filterText: item.filterText, preselect: item.preselect, commitCharacters: raw.commitCharacters,
    tags: item.tags || (item.deprecated ? [monaco.languages.CompletionItemTag?.Deprecated ?? 1] : undefined),
    additionalTextEdits: (item.additionalTextEdits || []).map(edit => ({ range: toRange(edit.range), text: edit.newText || '' })),
  }
}
function textEdit(edit) { return { range: toRange(edit.range), text: edit.newText ?? '' } }
function documentSymbol(symbol) {
  const range = symbol.range || symbol.location?.range
  return { name: symbol.name || '', detail: symbol.detail || '', kind: Math.max(0, (symbol.kind || 1) - 1), tags: symbol.tags || [],
    containerName: symbol.containerName, range: toRange(range), selectionRange: toRange(symbol.selectionRange || range), children: (symbol.children || []).map(documentSymbol),
  }
}
function lspDiagnostic(marker) {
  return { range: fromRange(marker), message: marker.message || '', severity: Number(Object.keys(SEVERITY).find(key => SEVERITY[key] === marker.severity) || 3), source: marker.source, code: marker.code?.value || marker.code, tags: marker.tags }
}

export default LspClient
