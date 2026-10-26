// One language-service lifetime per project/worktree, independent of the editor tab.
// Client injection keeps document/version lifecycle testable without Monaco or a socket.
const slotKey = (lang, serverId = '') => `${lang}\0${serverId}`

export class WorkspaceLanguageService {
  constructor({ clientClass, projectId, tree = 'main', sessionId = '', onStatus = () => {},
    onCapabilities = () => {}, onWorkspaceEdit = () => {}, onDiagnostics = () => {}, onNavigate = () => false,
    canEdit = () => false, delay = 250, setTimer = (fn, milliseconds) => setTimeout(fn, milliseconds), clearTimer = id => clearTimeout(id) } = {}) {
    this.clientClass = clientClass
    this.projectId = projectId
    this.tree = tree || 'main'
    this.sessionId = sessionId
    this.onStatus = onStatus
    this.onCapabilities = onCapabilities
    this.onWorkspaceEdit = onWorkspaceEdit
    this.onDiagnostics = onDiagnostics
    this.onNavigate = onNavigate
    this.canEdit = canEdit
    this.delay = delay
    this.setTimer = setTimer
    this.clearTimer = clearTimer
    this.documents = new Map()
    this.keys = new Map()
    this.clients = new Map()
    this.disposed = false
  }

  _ensureClient(lang, serverId = '') {
    const key = slotKey(lang, serverId)
    if (this.clients.has(key)) return this.clients.get(key)
    const slot = { lang, serverId, state: 'connecting', detail: '', client: null, ready: null }
    const current = () => !this.disposed && this.clients.get(key) === slot
    const context = { projectId: this.projectId, tree: this.tree, lang, serverId }
    const client = new this.clientClass({
      ...context, sessionId: this.sessionId, canEdit: model => current() && this.canEdit(model),
      onStatus: (state, detail = '') => {
        if (!current()) return
        slot.state = state; slot.detail = detail
        this.onStatus({ ...context, state, detail, client })
        if (state === 'ready') Promise.resolve().then(() => this._flushSlot(slot))
      },
      onCapabilities: capabilities => { if (current()) this.onCapabilities({ ...context, capabilities, client }) },
      onDiagnostics: (uri, diagnostics, version) => { if (current()) this.onDiagnostics({ ...context, uri, diagnostics, version }) },
      onWorkspaceEdit: payload => current() ? this.onWorkspaceEdit({ ...payload, ...context }) : false,
      onNavigate: payload => current() ? this.onNavigate({ ...payload, ...context }) : false,
    })
    slot.client = client
    this.clients.set(key, slot)
    this._connectSlot(slot)
    return slot
  }

  _connectSlot(slot) {
    const key = slotKey(slot.lang, slot.serverId), client = slot.client
    const current = () => !this.disposed && this.clients.get(key) === slot
    const context = { projectId: this.projectId, tree: this.tree, lang: slot.lang, serverId: slot.serverId }
    slot.state = 'connecting'; slot.detail = ''
    try {
      client.connect()
      slot.ready = Promise.resolve(client.waitForReady()).then(() => {
        if (!current()) return null
        slot.state = 'ready'
        this._flushSlot(slot)
        this.onStatus({ ...context, state: 'ready', detail: slot.detail, client })
        this.onCapabilities({ ...context, capabilities: client.capabilities || {}, client })
        return client
      }).catch(error => {
        if (current()) {
          slot.state = 'error'; slot.detail = error.message
          this.onStatus({ ...context, state: 'error', detail: error.message, client })
        }
        return null
      })
    } catch (error) {
      slot.state = 'error'; slot.detail = error.message
      slot.ready = Promise.resolve(null)
      this.onStatus({ ...context, state: 'error', detail: error.message, client })
    }
    return slot.ready
  }

  _flushSlot(slot, uri, openOnly = false) {
    if (this.disposed || slot.state !== 'ready' || slot.client.disposed) return
    const key = slotKey(slot.lang, slot.serverId)
    for (const record of this.documents.values()) {
      if ((uri && uri !== record.uri) || record.lang !== slot.lang || !record.servers.has(slot.serverId)) continue
      const opened = slot.client.documents.has(record.uri)
      if (!opened) {
        slot.client.openDocument({ uri: record.uri, text: record.text, model: record.model })
        record.sent.set(key, record.text)
      } else {
        const entry = slot.client.documents.get(record.uri)
        if (entry && record.model && entry.model !== record.model) entry.model = record.model
        if (!openOnly && record.sent.get(key) !== record.text) {
          slot.client.changeDocument(record.uri, record.text)
          record.sent.set(key, record.text)
        }
      }
      if (!openOnly && record.saveSeq && record.savedSent.get(key) !== record.saveSeq) {
        slot.client.saveDocument(record.uri, record.savedText)
        record.savedSent.set(key, record.saveSeq)
      }
    }
    if (slot.client.documents.size) slot.client.registerProviders()
  }

  // Rows contain the current drafts, including inactive tabs. Switching the
  // focused editor only changes the model reference; it never sends didClose.
  syncDocuments(rows = []) {
    if (this.disposed) return Promise.resolve([])
    const seen = new Set(), pending = new Set()
    for (const row of rows) {
      if (!row?.key || !row.uri || !row.lang || typeof row.text !== 'string') continue
      seen.add(row.key)
      const previous = this.keys.get(row.key)
      if (previous && previous !== row.uri) this.closeDocument(row.key)
      let record = this.documents.get(row.uri)
      if (record && record.lang !== row.lang) {
        for (const key of [...record.keys]) this.closeDocument(key)
        record = null
      }
      if (!record) {
        record = { uri: row.uri, lang: row.lang, text: row.text, model: row.model,
          keys: new Set(), servers: new Set(row.servers || ['']), sent: new Map(), timer: null,
          saveSeq: 0, savedText: null, savedSent: new Map() }
        this.documents.set(row.uri, record)
      }
      record.keys.add(row.key); this.keys.set(row.key, row.uri)
      if (row.model) record.model = row.model
      const desiredServers = new Set(row.servers || [''])
      for (const serverId of record.servers) if (!desiredServers.has(serverId)) {
        const key = slotKey(record.lang, serverId), slot = this.clients.get(key)
        if (slot?.client.documents.has(record.uri)) slot.client.closeDocument(record.uri)
        record.sent.delete(key); record.savedSent.delete(key)
      }
      record.servers = desiredServers
      for (const serverId of desiredServers) {
        pending.add(this._ensureClient(row.lang, serverId).ready)
      }
      this.changeDocument(row.uri, row.text)
    }
    for (const key of [...this.keys.keys()]) if (!seen.has(key)) this.closeDocument(key)
    for (const [key, slot] of this.clients) {
      const matching = [...this.documents.values()].filter(record => record.lang === slot.lang)
      // An explicit catalog disable removes its providers and connection; closing
      // the last tab keeps the workspace service ready for the next file.
      if (matching.length && !matching.some(record => record.servers.has(slot.serverId))) {
        this.clients.delete(key)
        slot.client.dispose()
      }
    }
    for (const slot of this.clients.values()) this._flushSlot(slot, undefined, true)
    return Promise.allSettled([...pending])
  }

  changeDocument(uri, text) {
    const record = this.documents.get(uri)
    if (this.disposed || !record || typeof text !== 'string' || record.text === text) return
    record.text = text
    if (record.timer != null) this.clearTimer(record.timer)
    record.timer = this.setTimer(() => { record.timer = null; this.flushDocument(uri) }, this.delay)
  }

  flushDocument(uri) {
    const record = this.documents.get(uri)
    if (this.disposed || !record) return false
    if (record.timer != null) { this.clearTimer(record.timer); record.timer = null }
    for (const slot of this.clients.values()) this._flushSlot(slot, uri)
    return true
  }

  notifySaved({ key, uri, text }) {
    uri = uri || this.keys.get(key)
    const record = this.documents.get(uri)
    if (this.disposed || !record || typeof text !== 'string') return false
    // didSave describes the submitted disk revision, while didChange continues
    // to describe any newer draft typed during the save request.
    record.savedText = text; record.saveSeq += 1
    this.flushDocument(uri)
    return true
  }

  closeDocument(document) {
    const key = typeof document === 'string' ? document : document?.key
    const uri = this.keys.get(key) || document?.uri || document?.file?.uri
    const record = uri && this.documents.get(uri)
    if (!record) return false
    if (key && !record.keys.has(key)) return false
    if (key) { record.keys.delete(key); this.keys.delete(key) }
    else { for (const item of record.keys) this.keys.delete(item); record.keys.clear() }
    if (record.keys.size) return true
    if (record.timer != null) this.clearTimer(record.timer)
    for (const slot of this.clients.values()) if (slot.client.documents.has(uri)) slot.client.closeDocument(uri)
    this.documents.delete(uri)
    return true
  }

  clientsFor(lang) { return [...this.clients.values()].filter(slot => slot.lang === lang && slot.state === 'ready').map(slot => slot.client) }
  statesFor(lang) { return [...this.clients.values()].filter(slot => slot.lang === lang) }
  clientFor(lang, capability) {
    return this.clientsFor(lang).find(client => !capability || client.capabilities?.[capability]) || null
  }

  reconnect() {
    if (this.disposed) return Promise.resolve([])
    return Promise.allSettled([...this.clients.values()].filter(slot => ['error', 'closed'].includes(slot.state))
      .map(slot => this._connectSlot(slot)))
  }

  dispose() {
    if (this.disposed) return
    this.disposed = true
    for (const record of this.documents.values()) if (record.timer != null) this.clearTimer(record.timer)
    for (const slot of this.clients.values()) {
      for (const uri of [...slot.client.documents.keys()]) slot.client.closeDocument(uri)
      slot.client.dispose()
    }
    this.documents.clear(); this.keys.clear(); this.clients.clear()
  }
}
