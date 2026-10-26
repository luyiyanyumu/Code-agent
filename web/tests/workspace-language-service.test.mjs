import assert from 'node:assert/strict'
import test from 'node:test'
import { WorkspaceLanguageService } from '../src/lib/workspaceLanguageService.js'

const tick = () => new Promise(setImmediate)
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const row = (name, text = name, extra = {}) => ({ key: `p/main/${name}.py`, uri: `file:///E:/project/${name}.py`, lang: 'python', text, model: { name }, ...extra })

function fixture(options = {}) {
  const clients = [], timers = new Map(), statuses = [], edits = [], diagnostics = []
  let timerId = 0
  class Client {
    constructor(opts) {
      this.options = opts; this.calls = []; this.documents = new Map()
      this.capabilities = options.capabilities?.[opts.serverId || 'primary'] || {}
      this.ready = options.pending ? deferred() : null
      clients.push(this)
    }
    connect() { this.calls.push(['connect']) }
    waitForReady() { return this.ready?.promise || Promise.resolve() }
    openDocument(value) { this.documents.set(value.uri, { ...value }); this.calls.push(['open', value.uri, value.text]) }
    changeDocument(uri, text) { this.documents.get(uri).text = text; this.calls.push(['change', uri, text]) }
    saveDocument(uri, text) { this.calls.push(['save', uri, text]) }
    closeDocument(uri) { this.documents.delete(uri); this.calls.push(['close', uri]) }
    registerProviders() { this.calls.push(['providers']) }
    dispose() { this.disposed = true; this.calls.push(['dispose']) }
  }
  const service = new WorkspaceLanguageService({
    clientClass: Client, projectId: 'p', tree: 'main', sessionId: 'session',
    onStatus: value => statuses.push(value), onWorkspaceEdit: value => edits.push(value),
    onDiagnostics: value => diagnostics.push(value), canEdit: model => model?.editable === true,
    setTimer: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId }, clearTimer: id => timers.delete(id),
  })
  const flushTimers = () => { const tasks = [...timers.values()]; timers.clear(); for (const task of tasks) task.fn() }
  return { service, clients, timers, statuses, edits, diagnostics, flushTimers }
}

test('navigation callbacks include workspace context and stop after the client is removed', async () => {
  const { service, clients } = fixture(), navigations = []
  service.onNavigate = payload => { navigations.push(payload); return true }
  await service.syncDocuments([row('a')])
  const navigation = { kind: 'references', uri: row('a').uri, locations: [] }
  assert.equal(clients[0].options.onNavigate(navigation), true)
  assert.deepEqual(navigations[0], { ...navigation, projectId: 'p', tree: 'main', lang: 'python', serverId: '' })
  service.dispose()
  assert.equal(clients[0].options.onNavigate(navigation), false)
  assert.equal(navigations.length, 1)
})

test('switching active models keeps all open tabs and a single workspace connection', async () => {
  const { service, clients } = fixture()
  const a = row('a'), b = row('b')
  await service.syncDocuments([a, b])
  const replacement = { name: 'new active editor model' }
  await service.syncDocuments([a, { ...b, model: replacement }])
  assert.equal(clients.length, 1)
  assert.deepEqual([...clients[0].documents.keys()], [a.uri, b.uri])
  assert.equal(clients[0].documents.get(b.uri).model, replacement)
  assert.equal(clients[0].calls.some(([name]) => name === 'close' || name === 'dispose'), false)
  assert.equal(clients[0].options.tree, 'main')
  assert.equal(clients[0].options.sessionId, 'session')
  service.dispose()
})

test('inactive draft updates debounce once and latest text reaches both semantic and Ruff clients', async () => {
  const { service, clients, timers, flushTimers } = fixture()
  const a = row('a', 'disk', { servers: ['pyright', 'ruff'] })
  await service.syncDocuments([a])
  await service.syncDocuments([{ ...a, text: 'first' }])
  await service.syncDocuments([{ ...a, text: 'latest' }])
  assert.equal(clients.length, 2)
  assert.equal(timers.size, 1)
  assert.equal([...timers.values()][0].delay, 250)
  assert.equal(clients.every(client => !client.calls.some(([name]) => name === 'change')), true)
  flushTimers()
  for (const client of clients) assert.deepEqual(client.calls.filter(([name]) => name === 'change'), [['change', a.uri, 'latest']])
  service.dispose()
})

test('a new tab opens immediately without flushing another tab debounce', async () => {
  const { service, clients, timers } = fixture()
  const a = row('a')
  await service.syncDocuments([a])
  service.changeDocument(a.uri, 'pending a')
  await service.syncDocuments([{ ...a, text: 'pending a' }, row('b')])
  assert.equal(clients[0].documents.has(row('b').uri), true)
  assert.equal(clients[0].calls.some(([name]) => name === 'change'), false)
  assert.equal(timers.size, 1)
  service.dispose()
})

test('closing a tab sends didClose only for that document and keeps the workspace client', async () => {
  const { service, clients } = fixture()
  const a = row('a'), b = row('b')
  await service.syncDocuments([a, b])
  await service.syncDocuments([b])
  assert.deepEqual(clients[0].calls.filter(([name]) => name === 'close'), [['close', a.uri]])
  assert.equal(clients[0].disposed, undefined)
  await service.syncDocuments([])
  await service.syncDocuments([a])
  assert.equal(clients.length, 1)
  service.dispose()
})

test('alias keys share one URI and closing a stale key cannot close the live alias', async () => {
  const { service, clients } = fixture()
  const a = row('a'), alias = { ...a, key: 'p/main/alias.py' }
  await service.syncDocuments([a, alias])
  await service.syncDocuments([alias])
  assert.equal(clients[0].calls.some(([name]) => name === 'close'), false)
  assert.equal(service.closeDocument({ key: a.key, uri: a.uri }), false)
  assert.equal(service.closeDocument(alias), true)
  assert.deepEqual(clients[0].calls.filter(([name]) => name === 'close'), [['close', a.uri]])
  service.dispose()
})

test('only notifySaved emits didSave and it preserves text typed after a save was submitted', async () => {
  const { service, clients } = fixture()
  const a = row('a', 'submitted')
  await service.syncDocuments([a])
  service.changeDocument(a.uri, 'newer unsaved draft')
  service.flushDocument(a.uri)
  assert.equal(clients[0].calls.some(([name]) => name === 'save'), false)
  assert.equal(service.notifySaved({ key: a.key, text: 'submitted' }), true)
  assert.deepEqual(clients[0].calls.filter(([name]) => name === 'save'), [['save', a.uri, 'submitted']])
  assert.equal(clients[0].documents.get(a.uri).text, 'newer unsaved draft')
  assert.equal(service.notifySaved({ key: 'missing', text: 'ignored' }), false)
  service.dispose()
})

test('late ready opens only surviving tabs using their latest draft and queued disk-save notification', async () => {
  const { service, clients } = fixture({ pending: true })
  const a = row('a'), b = row('b')
  const pending = service.syncDocuments([a, b])
  service.syncDocuments([{ ...b, text: 'latest background b' }])
  service.notifySaved({ key: b.key, text: 'saved b' })
  clients[0].ready.resolve()
  await pending
  assert.deepEqual(clients[0].calls.filter(([name]) => name === 'open'), [['open', b.uri, 'latest background b']])
  assert.deepEqual(clients[0].calls.filter(([name]) => name === 'save'), [['save', b.uri, 'saved b']])
  service.dispose()
})

test('ordinary connection failure retains its client and ready resumes latest document sync', async () => {
  const { service, clients, statuses } = fixture({ pending: true })
  const a = row('a')
  const pending = service.syncDocuments([a])
  clients[0].ready.reject(new Error('socket closed'))
  await pending
  assert.equal(clients[0].disposed, undefined)
  service.changeDocument(a.uri, 'offline draft')
  clients[0].options.onStatus('ready', 'reconnected')
  await tick()
  assert.equal(clients[0].documents.get(a.uri).text, 'offline draft')
  assert.equal(statuses.at(-1).state, 'ready')
  service.dispose()
})

test('explicit retry reconnects only failed slots and preserves documents and healthy services', async () => {
  const { service, clients } = fixture({ pending: true })
  const a = row('a', 'draft', { servers: ['pyright', 'ruff'] })
  const pending = service.syncDocuments([a])
  clients[0].ready.reject(new Error('server startup failed'))
  clients[1].ready.resolve()
  await pending
  service.changeDocument(a.uri, 'edited before retry')
  clients[0].ready = deferred()
  const retry = service.reconnect()
  clients[0].ready.resolve()
  await retry
  assert.equal(clients[0].calls.filter(([name]) => name === 'connect').length, 2)
  assert.equal(clients[1].calls.filter(([name]) => name === 'connect').length, 1)
  assert.equal(clients[0].documents.get(a.uri).text, 'edited before retry')
  assert.equal(clients.every(client => !client.disposed), true)
  service.dispose()
})

test('scope disposal cancels timers, closes all documents, and ignores stale service callbacks', async () => {
  const { service, clients, timers, statuses, edits } = fixture()
  const a = row('a')
  await service.syncDocuments([a])
  service.changeDocument(a.uri, 'draft')
  const count = statuses.length
  service.dispose(); service.dispose()
  clients[0].options.onStatus('ready')
  clients[0].options.onWorkspaceEdit({ edit: {} })
  assert.equal(statuses.length, count)
  assert.equal(edits.length, 0)
  assert.equal(timers.size, 0)
  assert.deepEqual(clients[0].calls.slice(-2), [['close', a.uri], ['dispose']])
  assert.equal(service.clients.size, 0)
  assert.equal(service.documents.size, 0)
})

test('dispose before initial ready cannot reopen a closed workspace', async () => {
  const { service, clients } = fixture({ pending: true })
  const pending = service.syncDocuments([row('a')])
  service.dispose()
  clients[0].ready.resolve()
  await pending
  assert.equal(clients[0].calls.some(([name]) => name === 'open'), false)
})

test('server removal closes that server only and capabilities can come from Ruff', async () => {
  const { service, clients } = fixture({ capabilities: { pyright: { renameProvider: true }, ruff: { documentFormattingProvider: true } } })
  const a = row('a', 'draft', { servers: ['pyright', 'ruff'] })
  await service.syncDocuments([a])
  assert.equal(service.clientFor('python', 'documentFormattingProvider'), clients[1])
  assert.equal(service.clientFor('python', 'renameProvider'), clients[0])
  await service.syncDocuments([{ ...a, servers: ['pyright'] }])
  assert.equal(clients[1].disposed, true)
  assert.equal(clients[0].disposed, undefined)
  assert.equal(clients[0].documents.has(a.uri), true)
  assert.equal(service.clientFor('python', 'documentFormattingProvider'), null)
  await service.syncDocuments([{ ...a, servers: [] }])
  assert.equal(clients[0].disposed, true)
  assert.equal(service.clients.size, 0)
  service.dispose()
})

test('edits, diagnostics and edit guards carry workspace context without directly applying changes', async () => {
  const { service, clients, edits, diagnostics } = fixture()
  await service.syncDocuments([row('a', 'draft', { servers: ['pyright'] })])
  const client = clients[0], edit = { changes: {} }
  client.options.onWorkspaceEdit({ edit, title: 'Rename', versions: { uri: 2 } })
  client.options.onDiagnostics('uri', [{ message: 'warning' }], 2)
  assert.equal(client.options.canEdit({ editable: false }), false)
  assert.equal(client.options.canEdit({ editable: true }), true)
  assert.equal(edits[0].edit, edit)
  assert.equal(edits[0].serverId, 'pyright')
  assert.equal(edits[0].projectId, 'p')
  assert.equal(diagnostics[0].version, 2)
  assert.equal(diagnostics[0].tree, 'main')
  assert.equal(client.calls.some(([name]) => name === 'change'), false)
  service.dispose()
})

test('default timer wrappers do not call browser timers with a service receiver', () => {
  const set = globalThis.setTimeout, clear = globalThis.clearTimeout
  const calls = []
  globalThis.setTimeout = function (fn, delay) { assert.equal(this, undefined); calls.push(['set', delay]); return 1 }
  globalThis.clearTimeout = function (id) { assert.equal(this, undefined); calls.push(['clear', id]) }
  try {
    const service = new WorkspaceLanguageService({ projectId: 'p' })
    service.syncDocuments([row('a', 'disk', { servers: [] })])
    service.changeDocument(row('a').uri, 'draft')
    service.dispose()
    assert.deepEqual(calls, [['set', 250], ['clear', 1]])
  } finally { globalThis.setTimeout = set; globalThis.clearTimeout = clear }
})
