import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { hoverMarkdownStrings, normalizeHoverDocumentation } from '../src/lib/hoverDocumentation.js'
import { getTokenHover, rememberSemanticTokens, clearSemanticTokens, notifyHoverDocumentationUpdated } from '../src/lib/tokenHover.js'

const clone = value => JSON.parse(JSON.stringify(value))
const tick = () => new Promise(setImmediate)
const range = (sl = 0, sc = 0, el = sl, ec = sc + 1) => ({ start: { line: sl, character: sc }, end: { line: el, character: ec } })
const pos = { lineNumber: 1, column: 4 }
const uriA = 'file:///E:/project/a.py', uriB = 'file:///E:/project/b.py'
const allCapabilities = { completionProvider: { resolveProvider: true, triggerCharacters: ['.', ':'] }, hoverProvider: true, definitionProvider: true, referencesProvider: true, signatureHelpProvider: { triggerCharacters: ['('], retriggerCharacters: [','] }, documentSymbolProvider: true, workspaceSymbolProvider: true, documentFormattingProvider: true, documentRangeFormattingProvider: true, codeActionProvider: { resolveProvider: true }, renameProvider: { prepareProvider: true }, textDocumentSync: { change: 1, save: { includeText: true } } }
function model(text = 'foo', language = 'python') {
  return { text, version: 1, disposed: false,
    getValue() { return this.text }, getLanguageId() { return language }, isDisposed() { return this.disposed },
    getVersionId() { return this.version }, set(value) { this.text = value; this.version++ },
    getWordUntilPosition(p) { return { startColumn: 1, endColumn: p.column, word: this.text.split('\n')[p.lineNumber - 1]?.slice(0, p.column - 1) || '' } },
    getWordAtPosition() { return { word: 'foo', startColumn: 1, endColumn: 4 } }, getValueInRange() { return 'foo' },
  }
}
function cancellation() {
  const listeners = new Set()
  return { isCancellationRequested: false, disposed: 0,
    onCancellationRequested(fn) { listeners.add(fn); return { dispose: () => { this.disposed++; listeners.delete(fn) } } },
    cancel() { this.isCancellationRequested = true; for (const fn of [...listeners]) fn() },
  }
}
function controlledTimers() {
  let now = 0, sequence = 0
  const pending = new Map()
  return {
    setTimeout(fn, delay) { const id = ++sequence; pending.set(id, { fn, deadline: now + delay }); return id },
    clearTimeout(id) { pending.delete(id) },
    advance(milliseconds) {
      now += milliseconds
      for (const [id, item] of [...pending]) if (item.deadline <= now && pending.delete(id)) item.fn()
    },
    get size() { return pending.size },
  }
}
async function setup(t, options = {}, capabilities = allCapabilities, hoverHelpers = {}, timers = { setTimeout, clearTimeout }) {
  const registrations = [], markers = [], commands = new Map(), sockets = [], statuses = [], edits = [], diagnostics = [], environments = []
  const hoverSources = [], semanticRecords = [], semanticClears = [], documentationUpdates = []
  class Range { constructor(startLineNumber, startColumn, endLineNumber, endColumn) { Object.assign(this, { startLineNumber, startColumn, endLineNumber, endColumn }) } }
  const monaco = { Range, Uri: { parse: uri => ({ toString: () => uri }) }, MarkerSeverity: { Error: 8, Warning: 4, Info: 2, Hint: 1 },
    languages: { CompletionItemKind: { Text: 18, Function: 1, Method: 0, Variable: 4 }, CompletionItemInsertTextRule: { InsertAsSnippet: 4, KeepWhitespace: 1 } },
    editor: { setModelMarkers(model, owner, items) { markers.push({ model, owner, items }); (model.markers ||= new Map()).set(owner, items) },
      registerCommand(id, fn) { commands.set(id, fn); return { dispose() { commands.delete(id) } } },
    },
  }
  for (const name of ['CompletionItem', 'Hover', 'Definition', 'Declaration', 'TypeDefinition', 'Implementation', 'Reference', 'SignatureHelp', 'DocumentSymbol', 'DocumentFormattingEdit', 'DocumentRangeFormattingEdit', 'CodeAction', 'Rename', 'DocumentSemanticTokens', 'DocumentRangeSemanticTokens', 'CodeLens', 'InlayHints']) {
    monaco.languages[`register${name}Provider`] = (lang, provider, metadata) => {
      const registration = { name, lang, provider, metadata, disposed: false, dispose() { this.disposed = true } }
      registrations.push(registration); return registration
    }
  }
  class Socket {
    static OPEN = 1
    constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this) }
    send(text) { this.sent.push(JSON.parse(text)) }
    close() { this.readyState = 3; this.onclose?.() }
    receive(message) { this.onmessage?.({ data: JSON.stringify(message) }) }
  }
  const source = (await readFile(new URL('../src/lib/lspClient.js', import.meta.url), 'utf8')).replace(/^import .*\r?\n/gm, '').replace(/^export default .*\r?\n/gm, '').replace(/^export /gm, '')
  const context = vm.createContext({ monaco, hoverMarkdownStrings, normalizeHoverDocumentation,
    getTokenHover: hoverHelpers.getTokenHover || getTokenHover,
    registerTokenHoverSource: source => {
      const registration = { source, disposed: false, dispose() { this.disposed = true } }
      hoverSources.push(registration); return registration
    },
    rememberSemanticTokens: (model, result, legend, owner) => { semanticRecords.push({ model, result, legend, owner }); rememberSemanticTokens(model, result, legend, owner) },
    clearSemanticTokens: (owner, model) => { semanticClears.push({ owner, model }); clearSemanticTokens(owner, model) },
    notifyHoverDocumentationUpdated: change => { documentationUpdates.push(change); notifyHoverDocumentationUpdated(change) },
    WebSocket: Socket, location: { protocol: 'http:', host: 'fixture.invalid' }, getToken: () => '', setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, URL })
  vm.runInContext(`${source}\n;globalThis.Subject = LspClient`, context)
  let client
  client = new context.Subject({ projectId: 'p/one', tree: 'task:one', lang: 'python', autoReconnect: false,
    onStatus: (...args) => statuses.push(args), onDiagnostics: (...args) => diagnostics.push(args),
    onCapabilities: caps => environments.push({ capabilities: clone(caps), environment: clone(client.environment) }),
    onWorkspaceEdit: payload => { edits.push(payload) }, ...options,
  })
  t.after(() => client.dispose())
  client.connect()
  const ready = (caps = capabilities, params = {}) => sockets.at(-1).receive({ method: 'code-agent/ready', params: { server: 'pyright', capabilities: caps, ...params } })
  ready()
  const a = model('foo', options.lang || 'python'), b = model('bar', options.lang || 'python')
  client.openDocument({ uri: uriA, text: a.text, model: a }); client.openDocument({ uri: uriB, text: b.text, model: b })
  client.registerProviders()
  const provider = name => registrations.findLast(entry => entry.name === name && !entry.disposed)?.provider
  const respond = result => { const request = sockets.at(-1).sent.findLast(frame => frame.id !== undefined && frame.method); assert.ok(request); sockets.at(-1).receive({ id: request.id, result }); return request }
  return { client, a, b, sockets, registrations, markers, commands, statuses, edits, diagnostics, environments, hoverSources, semanticRecords, semanticClears, documentationUpdates, provider, respond, ready, sent: () => sockets.at(-1).sent }
}

test('capabilities gate providers, preserve model language, server query/environment and rebuild registrations on reconnect ready', async t => {
  const h = await setup(t, { serverId: 'ruff/test' }, { hoverProvider: true })
  assert.deepEqual(h.registrations.filter(x => !x.disposed).map(x => x.name), ['Hover'])
  assert.equal(new URL(h.sockets[0].url).searchParams.get('server'), 'ruff/test'); assert.equal(new URL(h.sockets[0].url).searchParams.get('tree'), 'task:one')
  h.ready({ completionProvider: { triggerCharacters: ['@'] } }, { environment: { python: 'E:/project/.venv/python.exe' } })
  assert.equal(h.registrations[0].disposed, true); assert.deepEqual(clone(h.provider('CompletionItem').triggerCharacters), ['@']); assert.equal(h.provider('Hover'), undefined)
  assert.equal(h.environments.at(-1).environment.python, 'E:/project/.venv/python.exe'); assert.equal(h.client.markerOwner, 'code-agent-lsp:ruff/test')
})

test('declaration, type and implementation navigation respect capability, document ownership, cancellation and versions', async t => {
  const h = await setup(t, {}, { declarationProvider: {}, typeDefinitionProvider: true, implementationProvider: true })
  const target = { targetUri: uriB, targetRange: range(4, 0, 5, 3), targetSelectionRange: range(4, 1, 4, 3) }
  for (const [name, handler, method] of [
    ['Declaration', 'provideDeclaration', 'textDocument/declaration'],
    ['TypeDefinition', 'provideTypeDefinition', 'textDocument/typeDefinition'],
    ['Implementation', 'provideImplementation', 'textDocument/implementation'],
  ]) {
    const provider = h.provider(name)
    const pending = provider[handler](h.a, pos)
    assert.equal(h.sent().at(-1).method, method)
    assert.deepEqual(h.sent().at(-1).params.position, { line: 0, character: 3 })
    h.respond([target])
    const locations = await pending
    assert.equal(locations[0].uri.toString(), uriB)
    assert.deepEqual(clone(locations[0].range), { startLineNumber: 5, startColumn: 2, endLineNumber: 5, endColumn: 4 })
    const count = h.sent().length
    assert.deepEqual(clone(await provider[handler](model('foreign'), pos)), [])
    assert.equal(h.sent().length, count)
    const token = cancellation(), cancelled = provider[handler](h.a, pos, token)
    token.cancel(); assert.deepEqual(clone(await cancelled), [])
    const stale = provider[handler](h.a, pos)
    h.a.set(h.a.text + 'x'); h.respond([target])
    assert.deepEqual(clone(await stale), [])
  }
  h.ready({ hoverProvider: true })
  assert.equal(h.provider('Declaration'), undefined)
  assert.equal(h.provider('TypeDefinition'), undefined)
  assert.equal(h.provider('Implementation'), undefined)
})

test('unchanged drafts skip text allocations while dirty background drafts still flush before a query', async t => {
  const h = await setup(t)
  let readsA = 0, readsB = 0
  h.a.getValue = function () { readsA++; return this.text }
  h.b.getValue = function () { readsB++; return this.text }
  h.client.syncDocuments()
  h.client.syncDocuments()
  assert.equal(readsA, 1); assert.equal(readsB, 1)
  h.b.set('changed background')
  const request = h.provider('Hover').provideHover(h.a, pos)
  const beforeReply = h.sent().slice(-2)
  assert.equal(beforeReply[0].method, 'textDocument/didChange')
  assert.equal(beforeReply[0].params.textDocument.uri, uriB)
  assert.equal(beforeReply[1].method, 'textDocument/hover')
  assert.equal(readsB, 2)
  h.respond({ contents: 'docs' }); await request
  h.client.syncDocuments(); assert.equal(readsB, 2)
  h.client.changeDocument(uriB, 'stale external draft')
  h.client.syncDocuments()
  assert.equal(h.client.documents.get(uriB).text, 'changed background')
  assert.equal(readsB, 3, 'an external text change invalidates the model-version cache')
  const replacement = model('replacement')
  h.client.documents.get(uriB).model = replacement
  h.client.syncDocuments()
  assert.equal(h.client.documents.get(uriB).text, 'replacement')
})

test('initialization timing reports actual handshake values and clears missing or invalid metadata on reconnect', async t => {
  const h = await setup(t)
  h.ready(allCapabilities, { initialization_ms: 1250 })
  assert.equal(h.client.initializationMs, 1250)
  h.ready(allCapabilities, { initialization_ms: -1 })
  assert.equal(h.client.initializationMs, null)
  h.ready(allCapabilities)
  assert.equal(h.client.initializationMs, null)
})

const hierarchyItem = (name = 'foo', uri = uriA) => ({ name, kind: 6, uri, range: range(0, 0, 0, 3), selectionRange: range(0, 0, 0, 3), detail: 'Project.foo()', data: { handle: `${name}-opaque` } })
test('call and type hierarchy preserve opaque server handles and reject foreign, stale and cancelled expansions', async t => {
  const h = await setup(t, { canEdit: () => false }, { callHierarchyProvider: true, typeHierarchyProvider: true })
  for (const [kind, prepare, directions] of [
    ['call', 'textDocument/prepareCallHierarchy', [['incoming', 'callHierarchy/incomingCalls', 'from'], ['outgoing', 'callHierarchy/outgoingCalls', 'to']]],
    ['type', 'textDocument/prepareTypeHierarchy', [['incoming', 'typeHierarchy/supertypes', null], ['outgoing', 'typeHierarchy/subtypes', null]]],
  ]) {
    const pending = h.client.prepareHierarchy(uriA, pos, kind)
    assert.equal(h.sent().at(-1).method, prepare)
    h.respond([hierarchyItem(), { name: 'invalid' }])
    const [root] = await pending
    assert.deepEqual(clone(root.data), { handle: 'foo-opaque' })
    for (const [direction, method, field] of directions) {
      const children = h.client.hierarchyChildren(root, direction)
      assert.equal(h.sent().at(-1).method, method)
      assert.deepEqual(h.sent().at(-1).params.item, clone(root))
      const child = hierarchyItem('bar', uriB), raw = field ? { [field]: child, fromRanges: [range(1, 2, 1, 5), { broken: true }] } : child
      h.respond([raw])
      const [row] = await children
      assert.equal(row.item.uri, uriB)
      assert.equal(row.ranges.length, field ? 1 : 0)
      const deeper = h.client.hierarchyChildren(row.item, direction)
      assert.equal(h.sent().at(-1).params.item.data.handle, 'bar-opaque'); h.respond([]); await deeper
    }
    const count = h.sent().length
    await assert.rejects(h.client.hierarchyChildren(clone(root), 'incoming'), { name: 'CancellationError' })
    assert.equal(h.sent().length, count, 'only exact handles issued by this client can expand')
    const token = cancellation(), cancelled = h.client.hierarchyChildren(root, 'incoming', token)
    token.cancel(); await assert.rejects(cancelled, { name: 'CancellationError' })
    const stale = h.client.hierarchyChildren(root, 'outgoing')
    h.b.set(`background ${kind}`); h.respond([])
    await assert.rejects(stale, { name: 'CancellationError' })
  }
  const count = h.sent().length
  assert.deepEqual(clone(await h.client.prepareHierarchy(uriA, pos, 'invalid')), [])
  await assert.rejects(h.client.prepareHierarchy('file:///foreign.py', pos, 'call'))
  assert.equal(h.sent().length, count)
  h.ready({})
  assert.deepEqual(clone(await h.client.prepareHierarchy(uriA, pos, 'call')), [])
})

test('CodeLens resolves on readonly models, uses validated navigation and never executes unknown commands', async t => {
  const navigations = [], h = await setup(t, { canEdit: () => false, onNavigate: payload => navigations.push(payload) }, { codeLensProvider: { resolveProvider: true } })
  const provider = h.provider('CodeLens')
  const pending = provider.provideCodeLenses(h.a)
  assert.equal(h.sent().at(-1).method, 'textDocument/codeLens')
  h.respond([{ range: range(), data: { handle: 'lens' } }, { range: range(), command: { title: 'Run shell', command: 'arbitrary.run', arguments: ['rm'] } }, { range: { bad: true } }])
  const result = await pending
  assert.equal(result.lenses.length, 2)
  assert.equal(result.lenses[1].command.id, '')
  assert.equal(await h.client.activateCodeLens(result.lenses[1]), false)
  const resolving = provider.resolveCodeLens(h.a, result.lenses[0])
  assert.equal(h.sent().at(-1).method, 'codeLens/resolve')
  assert.deepEqual(h.sent().at(-1).params.data, { handle: 'lens' })
  h.respond({ range: range(), command: { title: '1 reference', command: 'java.show.references', arguments: [uriA, { line: 0, character: 0 }, [{ uri: uriB, range: range(2, 1, 2, 4) }]] } })
  const lens = await resolving
  assert.equal(lens.command.id, h.client.lensCommandId)
  await h.commands.get(lens.command.id)(null, lens)
  assert.equal(navigations[0].kind, 'references'); assert.equal(navigations[0].locations[0].uri, uriB)
  assert.deepEqual(clone(navigations[0].locations[0].range), { startLineNumber: 3, startColumn: 2, endLineNumber: 3, endColumn: 5 })
  assert.ok(!h.sent().some(frame => frame.method === 'workspace/executeCommand'))
  h.a.set('updated')
  assert.equal(await h.client.activateCodeLens(lens), false)
  assert.equal((await provider.resolveCodeLens(h.a, lens)).command, undefined)
  const count = h.sent().length
  assert.equal((await provider.provideCodeLenses(model('foreign'))).lenses.length, 0)
  assert.equal(h.sent().length, count)
})

test('CodeLens and inlay hints honor refresh, cancellation and ready generation, with old listeners disposed', async t => {
  const caps = { codeLensProvider: { resolveProvider: true }, inlayHintProvider: { resolveProvider: true } }
  const h = await setup(t, {}, caps), events = []
  h.provider('CodeLens').onDidChange(() => events.push('lens'))
  h.provider('InlayHints').onDidChangeInlayHints(() => events.push('inlay'))
  h.sockets[0].receive({ id: 401, method: 'workspace/codeLens/refresh' })
  h.sockets[0].receive({ id: 402, method: 'workspace/inlayHint/refresh' })
  assert.deepEqual(events, ['lens', 'inlay'])
  assert.equal(h.sent().find(frame => frame.id === 402 && !frame.method).result, null)
  const cancelled = cancellation(), request = h.provider('CodeLens').provideCodeLenses(h.a, cancelled)
  cancelled.cancel(); assert.equal((await request).lenses.length, 0)
  const pending = h.provider('InlayHints').provideInlayHints(h.a, { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 })
  h.ready(caps)
  h.respond([{ position: { line: 0, character: 0 }, label: 'stale' }])
  assert.equal((await pending).hints.length, 0)
  h.sockets[0].receive({ id: 403, method: 'workspace/codeLens/refresh' })
  assert.deepEqual(events, ['lens', 'inlay'])
  h.ready({}); assert.equal(h.provider('CodeLens'), undefined); assert.equal(h.provider('InlayHints'), undefined)
})

test('inlay hints preserve label documentation and locations while suppressing commands and readonly edits', async t => {
  const h = await setup(t, {}, { inlayHintProvider: { resolveProvider: true } }), provider = h.provider('InlayHints')
  const raw = { position: { line: 0, character: 2 }, label: [{ value: 'param:', tooltip: { kind: 'markdown', value: '**name**' }, location: { uri: uriB, range: range() }, command: { command: 'arbitrary.run' } }], kind: 2, paddingRight: true, data: { handle: 'hint' }, textEdits: [{ range: range(), newText: 'name' }] }
  const pending = provider.provideInlayHints(h.a, { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 })
  assert.equal(h.sent().at(-1).method, 'textDocument/inlayHint')
  assert.deepEqual(h.sent().at(-1).params.range, range(0, 0, 0, 3))
  h.respond([raw, { position: { line: -1, character: 0 }, label: 'bad' }])
  const [hint] = (await pending).hints
  assert.deepEqual(clone(hint.position), { lineNumber: 1, column: 3 })
  assert.equal(hint.label[0].label, 'param:'); assert.equal(hint.label[0].tooltip.isTrusted, false)
  assert.equal(hint.label[0].location.uri.toString(), uriB); assert.equal(hint.label[0].command, undefined)
  assert.equal(hint.textEdits[0].text, 'name')
  const resolved = provider.resolveInlayHint(hint)
  assert.deepEqual(h.sent().at(-1).params.data, { handle: 'hint' })
  h.respond({ ...raw, tooltip: { kind: 'plaintext', value: 'plain <b>' } })
  assert.equal((await resolved).tooltip, 'plain <b>')
  h.client.canEdit = () => false
  const read = provider.provideInlayHints(h.a, { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 })
  h.respond([raw]); assert.equal((await read).hints[0].textEdits, undefined)
  const stale = provider.resolveInlayHint(hint)
  h.a.set('different'); h.respond(raw)
  const staleHint = await stale
  assert.equal(staleHint.textEdits, undefined); assert.equal(staleHint.label[0].location, undefined)
  const count = h.sent().length
  assert.equal((await provider.provideInlayHints(model('foreign'), range())).hints.length, 0)
  assert.equal(h.sent().length, count)
})

const semanticCapabilities = { semanticTokensProvider: { legend: { tokenTypes: ['class', 'method', 'parameter'], tokenModifiers: ['declaration', 'readonly'] }, full: { delta: true }, range: true } }

test('semantic providers use server legend/full and range requests for readonly owned drafts; foreign models make no request', async t => {
  const h = await setup(t, { canEdit: () => false }, semanticCapabilities)
  const full = h.provider('DocumentSemanticTokens'), ranged = h.provider('DocumentRangeSemanticTokens')
  assert.deepEqual(clone(full.getLegend()), semanticCapabilities.semanticTokensProvider.legend)
  const request = full.provideDocumentSemanticTokens(h.a, 'previous-result')
  assert.equal(h.sent().at(-1).method, 'textDocument/semanticTokens/full')
  assert.deepEqual(h.sent().at(-1).params, { textDocument: { uri: uriA } })
  h.respond({ data: [0, 0, 3, 1, 1], resultId: 'next' })
  const tokens = await request
  assert.equal(tokens.resultId, 'next'); assert.deepEqual(Array.from(tokens.data), [0, 0, 3, 1, 1])
  const rangeRequest = ranged.provideDocumentRangeSemanticTokens(h.a, { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 })
  assert.equal(h.sent().at(-1).method, 'textDocument/semanticTokens/range')
  assert.deepEqual(h.sent().at(-1).params.range, range(0, 0, 0, 3))
  h.respond({ data: [] }); assert.equal((await rangeRequest).data.length, 0)
  const count = h.sent().length
  assert.equal(await full.provideDocumentSemanticTokens(model('foreign')), null)
  assert.equal(h.sent().length, count); full.releaseDocumentSemanticTokens('next')
})

test('semantic highlighting rejects stale, cancelled and malformed responses without altering syntax models', async t => {
  const h = await setup(t, {}, semanticCapabilities), p = h.provider('DocumentSemanticTokens')
  const cancelled = cancellation(), pending = p.provideDocumentSemanticTokens(h.a, null, cancelled)
  cancelled.cancel(); assert.equal(await pending, null)
  assert.equal(h.sent().at(-1).method, '$/cancelRequest')
  const stale = p.provideDocumentSemanticTokens(h.a)
  h.a.set('new value'); h.respond({ data: [0, 0, 3, 0, 0] }); assert.equal(await stale, null)
  for (const data of [[0, 0], [0, -1, 2, 0, 0], [0, 0, 0, 0, 0], [0, 0, 3, 9, 0], [0, 0, 3, 0, 8], [1, 0, 3, 0, 0], [0, 8, 3, 0, 0], [0, 0, 3, 0.5, 0], [0, 0, 3, 0, 0, 0, 2, 3, 1, 0]]) {
    const request = p.provideDocumentSemanticTokens(h.a)
    h.respond({ data }); assert.equal(await request, null, `${data}`)
  }
  assert.equal(h.a.text, 'new value'); assert.equal(h.markers.length, 0)
})

test('semantic server refresh invalidates tokens and acknowledges request; reconnect and disposal clear old subscriptions', async t => {
  const h = await setup(t, {}, semanticCapabilities), events = []
  const subscription = h.provider('DocumentSemanticTokens').onDidChange(() => events.push('old'))
  h.sockets[0].receive({ id: 'refresh1', method: 'workspace/semanticTokens/refresh' })
  assert.deepEqual(events, ['old']); assert.deepEqual(h.sent().at(-1), { jsonrpc: '2.0', id: 'refresh1', result: null })
  h.ready({ semanticTokensProvider: { legend: semanticCapabilities.semanticTokensProvider.legend, range: true } })
  assert.equal(h.provider('DocumentSemanticTokens'), undefined)
  h.provider('DocumentRangeSemanticTokens').onDidChange(() => events.push('new'))
  h.sockets[0].receive({ id: 'refresh2', method: 'workspace/semanticTokens/refresh' })
  assert.deepEqual(events, ['old', 'new'])
  subscription.dispose(); h.client.dispose(); assert.equal(h.client.semanticTokenListeners.size, 0)
})

test('semantic providers remain absent for missing/invalid capability legends and retain syntax-only fallback', async t => {
  for (const legend of [null, { tokenTypes: [], tokenModifiers: [] }, { tokenTypes: ['class'], tokenModifiers: null }, { tokenTypes: [5], tokenModifiers: [] }]) {
    const h = await setup(t, {}, { semanticTokensProvider: { legend, full: true } })
    assert.equal(h.provider('DocumentSemanticTokens'), undefined)
    assert.equal(h.provider('DocumentRangeSemanticTokens'), undefined)
  }
})

test('requests synchronize all latest models before querying and preserve multiple opened document versions', async t => {
  const h = await setup(t)
  h.a.set('fooa'); h.b.set('draft dependency')
  const pending = h.client.symbols(uriA)
  const frames = h.sent().slice(-3)
  assert.deepEqual(frames.map(x => x.method), ['textDocument/didChange', 'textDocument/didChange', 'textDocument/documentSymbol'])
  assert.equal(frames[0].params.textDocument.uri, uriA); assert.equal(frames[1].params.contentChanges[0].text, 'draft dependency')
  h.respond([]); await pending
  assert.equal(h.client.documents.size, 2); assert.equal(h.client.documents.get(uriA).version, 2); assert.equal(h.client.documents.get(uriB).version, 2)
  const length = h.sent().length; h.client.changeDocument(uriA, 'fooa'); assert.equal(h.sent().length, length)
})

test('incremental servers receive a valid complete replacement range with UTF16/CRLF length and save capabilities respected', async t => {
  const h = await setup(t, {}, { textDocumentSync: { change: 2, save: { includeText: false } } })
  h.client.changeDocument(uriA, 'x\r\n😀'); h.client.changeDocument(uriA, 'y')
  const change = h.sent().filter(x => x.method === 'textDocument/didChange').at(-1)
  assert.deepEqual(change.params.contentChanges[0], { range: range(0, 0, 1, 2), rangeLength: 5, text: 'y' })
  h.a.set('y'); h.client.saveDocument(uriA, 'y'); assert.equal(Object.hasOwn(h.sent().at(-1).params, 'text'), false)
  h.ready({ textDocumentSync: { change: 1, save: false } }); const count = h.sent().length; h.client.saveDocument(uriA, 'y'); assert.equal(h.sent().length, count)
})

test('cancellation sends matching cancelRequest and removes pending/listener; pre-cancelled request sends nothing', async t => {
  const h = await setup(t), token = cancellation(), pending = h.client.request('textDocument/hover', {}, { token })
  const id = h.sent().at(-1).id; token.cancel(); await assert.rejects(pending, error => error.name === 'CancellationError')
  assert.deepEqual(h.sent().at(-1), { jsonrpc: '2.0', method: '$/cancelRequest', params: { id } }); assert.equal(h.client.pending.size, 0); assert.equal(token.disposed, 1)
  h.sockets[0].receive({ id, result: 'late' }); const count = h.sent().length
  await assert.rejects(h.client.request('ignored', {}, { token }), /取消/); assert.equal(h.sent().length, count)
})

test('timeout cancels server work; stale model/version responses never reach callers', async t => {
  const h = await setup(t)
  await assert.rejects(h.client.request('slow', {}, 5), /超时/); assert.equal(h.sent().at(-1).method, '$/cancelRequest'); assert.equal(h.client.pending.size, 0)
  const pending = h.client.symbols(uriA); h.a.set('newer'); h.respond([{ name: 'old', kind: 12, range: range() }]); await assert.rejects(pending, /文档已变化/)
  const next = h.client.symbols(uriA); h.client.closeDocument(uriA); h.client.openDocument({ uri: uriA, model: model('newer') }); h.respond([]); await assert.rejects(next, /文档已变化/)
})

test('hover translation can complete after 15 seconds while other document queries keep their original deadline', async t => {
  const timers = controlledTimers(), h = await setup(t, {}, allCapabilities, {}, timers)
  let settled = false
  const hover = h.client.hover(uriA, pos).finally(() => { settled = true })
  const hoverId = h.sent().at(-1).id
  const definition = h.client.definition(uriA, pos), definitionId = h.sent().at(-1).id
  const expired = assert.rejects(definition, /请求超时.*textDocument\/definition/)
  timers.advance(15000)
  await expired
  assert.equal(settled, false)
  assert.equal(h.client.pending.has(hoverId), true)
  assert.deepEqual(h.sent().at(-1), { jsonrpc: '2.0', method: '$/cancelRequest', params: { id: definitionId } })

  timers.advance(5000)
  h.sockets[0].receive({ id: hoverId, result: { contents: { kind: 'markdown', value: '完成文档中文翻译。' } } })
  assert.match((await hover).sections.at(-1).value, /完成文档中文翻译/)
  assert.equal(timers.size, 0)
})

test('30-second hover deadline still cancels remote work and preserves cancellation and stale-document guards', async t => {
  const timers = controlledTimers(), h = await setup(t, {}, allCapabilities, {}, timers)
  const hover = h.client.hover(uriA, pos), hoverId = h.sent().at(-1).id
  const expired = assert.rejects(hover, /请求超时.*textDocument\/hover/)
  timers.advance(29999)
  assert.equal(h.client.pending.has(hoverId), true)
  timers.advance(1)
  await expired
  assert.deepEqual(h.sent().at(-1), { jsonrpc: '2.0', method: '$/cancelRequest', params: { id: hoverId } })
  h.sockets[0].receive({ id: hoverId, result: { contents: 'late translation' } })
  assert.equal(h.client.pending.size, 0)

  const token = cancellation(), cancelled = h.client.hover(uriA, pos, token)
  const cancelledId = h.sent().at(-1).id
  const cancellationResult = assert.rejects(cancelled, error => error.name === 'CancellationError')
  timers.advance(20000)
  token.cancel()
  await cancellationResult
  assert.deepEqual(h.sent().at(-1), { jsonrpc: '2.0', method: '$/cancelRequest', params: { id: cancelledId } })
  assert.equal(token.disposed, 1)

  const stale = h.client.hover(uriA, pos)
  const staleResult = assert.rejects(stale, error => error.name === 'CancellationError')
  timers.advance(20000)
  h.a.set('newer document')
  h.respond({ contents: 'obsolete translated documentation' })
  await staleResult
  assert.equal(timers.size, 0)
})

test('versioned diagnostics reject old/unsynchronized drafts and retain tags/data/related information under distinct marker owners', async t => {
  const h = await setup(t), diagnostic = { range: range(), message: 'unused', severity: 2, code: 0, tags: [1], data: { fix: 'keep' }, relatedInformation: [{ location: { uri: uriB, range: range(1, 2, 1, 4) }, message: 'related' }] }
  h.client.changeDocument(uriA, 'changed'); h.a.set('changed')
  h.sockets[0].receive({ method: 'textDocument/publishDiagnostics', params: { uri: uriA, version: 1, diagnostics: [diagnostic] } }); assert.equal(h.diagnostics.length, 0)
  h.sockets[0].receive({ method: 'textDocument/publishDiagnostics', params: { uri: uriA, version: 2, diagnostics: [diagnostic] } })
  assert.equal(h.markers.at(-1).owner, 'code-agent-lsp:pyright'); assert.equal(h.markers.at(-1).items[0].code, '0'); assert.equal(h.markers.at(-1).items[0].relatedInformation[0].startColumn, 3)
  assert.deepEqual(clone(h.client.diagnostics.get(uriA)[0].data), { fix: 'keep' }); assert.deepEqual(clone(h.markers.at(-1).items[0].tags), [1])
  h.a.set('not synced'); h.sockets[0].receive({ method: 'textDocument/publishDiagnostics', params: { uri: uriA, diagnostics: [] } }); assert.equal(h.diagnostics.length, 1)
  h.client.closeDocument(uriA); assert.equal(h.a.markers.get('code-agent-lsp:pyright').length, 0)
})

test('primary and companion diagnostics coexist; closing one client clears only its server marker owner', async t => {
  const h = await setup(t), companion = new h.client.constructor({ projectId: 'p/one', lang: 'python', serverId: 'ruff', autoReconnect: false })
  t.after(() => companion.dispose()); companion.connect(); companion._handle({ method: 'code-agent/ready', params: { server: 'ruff', capabilities: {} } }); companion.openDocument({ uri: uriA, model: h.a })
  h.client._publish(uriA, [{ range: range(), message: 'type error', severity: 1 }], 1)
  companion._publish(uriA, [{ range: range(), message: 'lint warning', severity: 2 }], 1)
  assert.equal(h.a.markers.get('code-agent-lsp:pyright')[0].message, 'type error'); assert.equal(h.a.markers.get('code-agent-lsp:ruff')[0].message, 'lint warning')
  h.client.closeDocument(uriA); assert.equal(h.a.markers.get('code-agent-lsp:pyright').length, 0); assert.equal(h.a.markers.get('code-agent-lsp:ruff').length, 1)
})

test('automatic reconnect replays every opened latest draft at preserved versions and ignores old socket frames', async t => {
  const h = await setup(t, { autoReconnect: true, reconnectDelayMs: 5 }), first = h.sockets[0]
  h.client.changeDocument(uriA, 'saved draft'); h.a.set('saved draft'); first.close(); h.b.set('offline draft')
  await new Promise(resolve => setTimeout(resolve, 15)); assert.equal(h.sockets.length, 2)
  const second = h.sockets[1]; first.receive({ method: 'code-agent/ready', params: { server: 'stale', capabilities: {} } }); assert.equal(h.client.server, 'pyright')
  second.receive({ method: 'code-agent/ready', params: { server: 'pyright', capabilities: allCapabilities } })
  const replay = second.sent.filter(frame => frame.method === 'textDocument/didOpen')
  assert.deepEqual(replay.map(frame => [frame.params.textDocument.uri, frame.params.textDocument.version, frame.params.textDocument.text]), [[uriA, 2, 'saved draft'], [uriB, 2, 'offline draft']])
  assert.equal(h.commands.size, 1); assert.equal(h.client.ready, true)
})

test('new page resumes server-known document version before didOpen/change/save; real close permits a fresh version', async t => {
  const h = await setup(t)
  h.client.closeDocument(uriA)
  h.ready(allCapabilities, { document_versions: { [uriA]: 10, [uriB]: 1 } })
  h.client.openDocument({ uri: uriA, model: h.a })
  const opened = h.sent().filter(frame => frame.method === 'textDocument/didOpen' && frame.params.textDocument.uri === uriA).at(-1)
  assert.equal(opened.params.textDocument.version, 11); assert.equal(h.client.documents.get(uriA).version, 11)
  h.a.set('updated after refresh'); h.client.saveDocument(uriA, h.a.text)
  const change = h.sent().filter(frame => frame.method === 'textDocument/didChange' && frame.params.textDocument.uri === uriA).at(-1)
  assert.equal(change.params.textDocument.version, 12); assert.equal(h.sent().at(-1).method, 'textDocument/didSave'); assert.equal(h.sent().at(-1).params.text, h.a.text)
  h.client.closeDocument(uriA); h.client.openDocument({ uri: uriA, model: h.a }); assert.equal(h.client.documents.get(uriA).version, 1)
})

test('same-version reconnect preserves document version; higher server versions and changed offline text advance safely', async t => {
  const h = await setup(t)
  h.ready(allCapabilities, { document_versions: { [uriA]: 1, [uriB]: 1 } })
  assert.deepEqual(h.sent().filter(frame => frame.method === 'textDocument/didOpen').slice(-2).map(frame => frame.params.textDocument.version), [1, 1])
  h.a.set('offline newer'); h.ready(allCapabilities, { document_versions: { [uriA]: 10, [uriB]: 8 } })
  assert.equal(h.client.documents.get(uriA).version, 11); assert.equal(h.client.documents.get(uriB).version, 9)
  const replay = h.sent().filter(frame => frame.method === 'textDocument/didOpen').slice(-2)
  assert.deepEqual(replay.map(frame => [frame.params.textDocument.version, frame.params.textDocument.text]), [[11, 'offline newer'], [9, 'bar']])
  h.ready(allCapabilities, { document_versions: { [uriA]: 11, [uriB]: 9 } })
  assert.equal(h.client.documents.get(uriA).version, 11); assert.equal(h.client.documents.get(uriB).version, 9)
})

test('completion maps text edits, snippets, kinds, sorting, context, incomplete status and safe resolve/additional imports', async t => {
  const h = await setup(t), provider = h.provider('CompletionItem')
  const pending = provider.provideCompletionItems(h.a, pos, { triggerKind: 1, triggerCharacter: '.' })
  const raw = { label: 'func', kind: 3, labelDetails: { detail: '(x)', description: 'module' }, textEdit: { range: range(0, 0, 0, 3), newText: 'func(${1:x})' }, insertTextFormat: 2, sortText: '001', filterText: 'fu', preselect: true, data: { id: 8 }, command: { command: 'untrusted.run' } }
  const request = h.respond({ isIncomplete: true, items: [raw] }); const result = await pending, item = result.suggestions[0]
  assert.deepEqual(request.params.context, { triggerKind: 2, triggerCharacter: '.' }); assert.equal(result.incomplete, true); assert.equal(item.kind, 1)
  assert.equal(item.insertText, 'func(${1:x})'); assert.equal(item.insertTextRules, 4); assert.equal(item.range.endColumn, 4); assert.equal(item.filterText, 'fu'); assert.equal(item.label.description, 'module'); assert.equal(item.command, undefined)
  const resolve = provider.resolveCompletionItem(item); const resolveRequest = h.sent().at(-1); assert.deepEqual(resolveRequest.params.data, { id: 8 })
  h.respond({ ...raw, detail: 'resolved', documentation: { kind: 'markdown', value: '**doc**' }, additionalTextEdits: [{ range: range(0, 0, 0, 0), newText: 'import func\n' }] })
  const resolved = await resolve; assert.equal(resolved.detail, 'resolved'); assert.equal(resolved.additionalTextEdits[0].text, 'import func\n'); assert.equal(resolved.documentation.isTrusted, false)
})

test('completion supports InsertReplaceEdit and itemDefaults without losing data on resolve', async t => {
  const h = await setup(t), p = h.provider('CompletionItem'), pending = p.provideCompletionItems(h.a, pos, { triggerKind: 2 })
  h.respond({ itemDefaults: { editRange: { insert: range(0, 0, 0, 3), replace: range(0, 0, 0, 5) }, insertTextFormat: 2, commitCharacters: [')'], data: { shared: 1 } }, items: [{ label: 'function', textEditText: '${1:name}' }] })
  const item = (await pending).suggestions[0]; assert.equal(item.range.insert.endColumn, 4); assert.equal(item.range.replace.endColumn, 6); assert.equal(item.insertTextRules, 4); assert.deepEqual(clone(item.commitCharacters), [')'])
  const resolved = p.resolveCompletionItem(item); assert.deepEqual(h.sent().at(-1).params.data, { shared: 1 }); h.respond({ label: 'function', detail: 'default resolved' }); assert.equal((await resolved).detail, 'default resolved')
})

test('provider cancellation passes token to transport; stale completion resolve does not modify existing suggestion or request server', async t => {
  const h = await setup(t), p = h.provider('CompletionItem'), token = cancellation()
  const cancelled = p.provideCompletionItems(h.a, pos, {}, token); const id = h.sent().at(-1).id; token.cancel()
  assert.equal((await cancelled).suggestions.length, 0); assert.deepEqual(h.sent().at(-1).params, { id })
  const pending = p.provideCompletionItems(h.a, pos, {}); h.respond([{ label: 'foo', data: { id: 1 } }]); const item = (await pending).suggestions[0]
  h.a.set('new draft'); const count = h.sent().length; assert.equal(await p.resolveCompletionItem(item), item); assert.equal(h.sent().length, count)
})

test('hover/definition/references/signature help retain ranges, URI links, context and parameter documentation', async t => {
  const h = await setup(t)
  const hover = h.provider('Hover').provideHover(h.a, pos); h.respond({ contents: [{ language: 'python', value: 'def foo(x)' }], range: range() }); assert.equal((await hover).range.startColumn, 1)
  const definition = h.provider('Definition').provideDefinition(h.a, pos); h.respond([{ targetUri: uriB, targetRange: range(2, 0, 5, 1), targetSelectionRange: range(3, 1, 3, 4) }]); const locations = await definition; assert.equal(locations[0].uri.toString(), uriB); assert.equal(locations[0].range.startLineNumber, 4)
  const refs = h.provider('Reference').provideReferences(h.a, pos, { includeDeclaration: false }); assert.equal(h.sent().at(-1).params.context.includeDeclaration, false); h.respond([{ uri: uriB, range: range() }]); assert.equal((await refs).length, 1)
  const signature = h.provider('SignatureHelp').provideSignatureHelp(h.a, pos, undefined, { triggerKind: 2, triggerCharacter: '(' }); h.respond({ signatures: [{ label: 'foo(x)', parameters: [{ label: [4, 5], documentation: { kind: 'markdown', value: 'arg' } }] }] }); const help = await signature
  assert.equal(help.value.activeParameter, 0); assert.equal(help.value.signatures[0].parameters[0].documentation.isTrusted, false); help.dispose()
})

test('full hover view retains typed documentation and safe signatures for readonly drafts', async t => {
  const h = await setup(t, { canEdit: () => false })
  const doc = { kind: 'plaintext', value: '**原文**\n@param value <T>' }
  const signature = { language: 'java', value: 'String method(T value)' }
  h.a.set('new draft')
  const pending = h.client.hover(uriA, pos)
  assert.equal(h.sent().at(-2).method, 'textDocument/didChange')
  assert.equal(h.sent().at(-1).method, 'textDocument/hover')
  assert.deepEqual(h.sent().at(-1).params.position, { line: 0, character: 3 })
  h.respond({ contents: [signature, doc], range: range(0, 1, 0, 4) })
  const result = await pending
  assert.deepEqual(clone(result.sections.slice(-2)), [{ kind: 'code', language: 'java', value: signature.value }, doc])
  assert.match(result.sections[0].value, /语言：.*Python/)
  assert.equal(result.contents[1].isTrusted, false)
  assert.equal(result.contents[1].supportHtml, false)
  assert.equal(result.range.startColumn, 2)
  const stale = h.client.hover(uriA, pos)
  h.a.set('newer draft'); h.respond({ contents: 'old docs' })
  await assert.rejects(stale, /文档已变化/)
})

test('hover docs reject malformed responses and missing capabilities without server calls', async t => {
  const h = await setup(t, {}, {})
  const count = h.sent().length
  assert.equal(await h.client.hover(uriA, pos), null)
  assert.equal(h.sent().length, count)
  h.ready({ hoverProvider: true })
  for (const contents of [null, [], { value: 'missing kind' }, { kind: 'unknown', value: 'ignored' }]) {
    const pending = h.client.hover(uriA, pos); h.respond({ contents })
    assert.equal(await pending, null)
  }
})

test('native and full documentation share structured LSP details and retain translation restoration metadata in every language', async t => {
  for (const lang of ['python', 'typescript', 'javascript', 'go', 'rust', 'c', 'cpp']) {
    const h = await setup(t, { lang }, { hoverProvider: true })
    const response = { contents: [{ language: lang, value: 'foo(value)' }, { kind: 'markdown', value: '根据提供的值读取内容。' }],
      documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'ready' },
      _code_agent_original: { contents: 'Reads the supplied value.' }, range: range(0, 0, 0, 3) }
    const full = h.client.hover(uriA, pos); h.respond(response)
    const native = h.provider('Hover').provideHover(h.a, pos); h.respond(response)
    const fullResult = await full, nativeResult = await native
    assert.deepEqual(clone(nativeResult), clone(fullResult))
    assert.equal(fullResult.symbol.name, 'foo')
    assert.equal(fullResult.symbol.language, lang)
    assert.equal(fullResult.symbol.purpose, '根据提供的值读取内容。')
    assert.equal(fullResult.documentation_translation.status, 'ready')
    assert.deepEqual(clone(fullResult._code_agent_original), response._code_agent_original)
  }
})

test('background documentation updates target live URI/version without cancelling same-draft in-flight hovers', async t => {
  for (const lang of ['python', 'java']) {
    const timers = controlledTimers(), h = await setup(t, { lang }, { hoverProvider: true }, {}, timers)
    const pending = h.client.hover(uriA, pos)
    const pendingId = h.sent().at(-1).id
    h.sockets[0].receive({ method: 'code-agent/documentationUpdated', params: { uri: 'file:///outside', version: 1 } })
    h.sockets[0].receive({ method: 'code-agent/documentationUpdated', params: { uri: uriA, version: 99 } })
    assert.equal(h.documentationUpdates.length, 0)
    const prewarm = { ready: 4, total: 8 }
    for (let index = 0; index < 8; index++) {
      timers.advance(2000)
      h.sockets[0].receive({ method: 'code-agent/documentationUpdated', params: { uri: uriA, version: 1, status: 'running', prewarm } })
      assert.equal(h.client.pending.has(pendingId), true)
    }
    assert.deepEqual(clone(h.documentationUpdates[0]), { projectId: 'p/one', tree: 'task:one', uri: uriA, version: 1, serverId: '', status: 'running', prewarm })
    h.respond({ contents: '已完成当前悬停的中文说明。', documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'ready' } })
    assert.ok((await pending).sections.some(section => section.value.includes('已完成当前悬停')))
    assert.ok(!h.sent().some(frame => frame.method === '$/cancelRequest'))
    const refreshed = h.client.hover(uriA, pos)
    h.respond({ contents: '说明已更新为中文。', documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'ready' } })
    assert.ok((await refreshed).sections.some(section => section.value.includes('说明已更新为中文')))
    const changed = h.client.hover(uriA, pos)
    const stale = assert.rejects(changed, error => error.name === 'CancellationError')
    h.a.set('changed source')
    h.respond({ contents: 'old source documentation' })
    await stale
    h.client.closeDocument(uriA)
    h.sockets[0].receive({ method: 'code-agent/documentationUpdated', params: { uri: uriA, version: 1 } })
    assert.equal(h.documentationUpdates.length, 8)
    assert.equal(h.client.documentationVersions.has(uriA), false)
  }
})

test('Java uses one shared token hover source and local words remain available without server hover capability', async t => {
  const local = { sections: [{ kind: 'plaintext', value: '类别：修饰符' }], contents: [{ value: '类别：修饰符' }], source: 'Java 语法', localOnly: true }
  const h = await setup(t, { lang: 'java' }, {}, { getTokenHover: () => local })
  assert.equal(h.provider('Hover'), undefined, 'the global Java hover provider owns rendering')
  const source = h.hoverSources.at(-1).source
  assert.equal(source.matches(h.a), true)
  assert.equal(source.matches(model('public', 'java')), false)
  const count = h.sent().length
  assert.equal(await source.provide(h.a, pos), local)
  assert.equal(await h.client.hover(uriA, pos), local)
  assert.equal(h.sent().length, count, 'lexical help does not request hover or semantic tokens')
  h.ready({ hoverProvider: true })
  assert.equal(h.hoverSources[0].disposed, true)
  assert.equal(await h.hoverSources.at(-1).source.provide(h.a, pos), local)
  assert.equal(h.sent().filter(frame => frame.method === 'textDocument/hover').length, 0)
  const cancelled = cancellation(); cancelled.cancel()
  await assert.rejects(h.client.hover(uriA, pos, cancelled), /取消/)
  h.client.dispose()
  assert.equal(h.hoverSources.at(-1).disposed, true)
  assert.equal(source.matches(h.a), false)
})

test('Java hover passes complete server evidence through the same enhancer for native and full views', async t => {
  const local = { sections: [{ kind: 'plaintext', value: '类别：标识符' }], contents: [{ value: '类别：标识符' }], localOnly: false }
  const enhanced = { sections: [{ kind: 'plaintext', value: '类别：注解；属性：public' }], contents: [{ value: '类别：注解；属性：public' }], source: 'Java 语义与文档', localOnly: false }
  const observed = []
  const h = await setup(t, { lang: 'java' }, { hoverProvider: true }, { getTokenHover(model, position, result = null) {
    observed.push({ model, position, result }); return result ? enhanced : local
  } })
  const raw = { contents: [{ language: 'java', value: 'org.example.RequestParam' }, { kind: 'markdown', value: '**Complete** documentation' }], range: range(0, 0, 0, 3) }
  for (const query of [() => h.client.hover(uriA, pos), () => h.hoverSources.at(-1).source.provide(h.a, pos)]) {
    const pending = query()
    assert.equal(h.sent().at(-1).method, 'textDocument/hover')
    h.respond(raw)
    assert.equal(await pending, enhanced)
    assert.equal(observed.at(-1).model, h.a)
    assert.deepEqual(observed.at(-1).position, pos)
    assert.deepEqual(clone(observed.at(-1).result), raw)
  }
  const empty = h.client.hover(uriA, pos); h.respond(null)
  assert.equal(await empty, local, 'a missing Javadoc still keeps token help')
  assert.equal(h.sent().some(frame => frame.method.startsWith('textDocument/semanticTokens')), false)
})

test('real Java enhancer combines validated semantic kinds and modifiers with complete typed server documentation', async t => {
  const caps = { hoverProvider: true, semanticTokensProvider: { legend: { tokenTypes: ['interface'], tokenModifiers: ['public', 'abstract'] }, full: true } }
  const h = await setup(t, { lang: 'java' }, caps)
  const position = { lineNumber: 1, column: 2 }
  const tokens = h.provider('DocumentSemanticTokens').provideDocumentSemanticTokens(h.a)
  h.respond({ data: [0, 0, 3, 0, 3] }); await tokens
  const signature = { language: 'java', value: 'org.example.foo' }
  const doc = { kind: 'plaintext', value: '**literal Javadoc** <T>\n@param value argument' }
  const pending = h.hoverSources.at(-1).source.provide(h.a, position)
  h.respond({ contents: [signature, doc] })
  const result = await pending
  assert.match(result.sections[0].value, /接口/)
  assert.match(result.sections[0].value, /public/)
  assert.match(result.sections[0].value, /抽象/)
  assert.deepEqual(clone(result.sections.slice(-2)), [{ kind: 'code', language: 'java', value: signature.value }, doc])
  assert.equal(result.range.startColumn, 1)
  assert.equal(result.range.endColumn, 4)
  assert.equal(result.contents.every(item => item.isTrusted === false && item.supportHtml === false), true)
  assert.equal(h.sent().filter(frame => frame.method === 'textDocument/semanticTokens/full').length, 1)
  h.a.set('foo ')
  const changed = h.client.hover(uriA, position); h.respond(null)
  assert.match((await changed).sections[0].value, /标识符/)
  assert.doesNotMatch(getTokenHover(h.a, position).sections[0].value, /\*\*接口\*\*/)
})

test('Java falls back on server failure only for the current draft and never revives cancelled or replaced hovers', async t => {
  const local = { sections: [], contents: [], localOnly: false }
  const h = await setup(t, { lang: 'java' }, { hoverProvider: true }, { getTokenHover: () => local })
  const rejectLatest = (code = -32603) => {
    const request = h.sent().findLast(frame => frame.id !== undefined && frame.method)
    h.sockets.at(-1).receive({ id: request.id, error: { code, message: '服务暂时不可用' } })
  }
  const failed = h.client.hover(uriA, pos); rejectLatest()
  assert.equal(await failed, local)
  const stale = h.client.hover(uriA, pos); h.a.set('new draft'); rejectLatest()
  await assert.rejects(stale, /文档已变化/)
  const token = cancellation(), cancelled = h.client.hover(uriA, pos, token)
  token.cancel(); await assert.rejects(cancelled, /取消/)
  const serverCancelled = h.client.hover(uriA, pos); rejectLatest(-32800)
  await assert.rejects(serverCancelled, /取消/)
  const replaced = h.client.hover(uriA, pos)
  h.client.openDocument({ uri: uriA, model: model(h.a.text, 'java') }); rejectLatest()
  await assert.rejects(replaced, /文档已变化/)
})

test('validated semantic tokens feed hover cache and document, refresh, reconnect and disposal invalidate it', async t => {
  const h = await setup(t, { lang: 'java' }, semanticCapabilities)
  const provider = h.provider('DocumentSemanticTokens')
  const pending = provider.provideDocumentSemanticTokens(h.a)
  h.respond({ data: [0, 0, 3, 0, 1] }); const validated = await pending
  assert.equal(h.semanticRecords.length, 1)
  assert.equal(h.semanticRecords[0].result, validated)
  assert.equal(h.semanticRecords[0].model, h.a)
  assert.equal(h.semanticRecords[0].owner, h.client)
  assert.deepEqual(clone(h.semanticRecords[0].legend), semanticCapabilities.semanticTokensProvider.legend)
  h.client.changeDocument(uriA, 'next')
  assert.equal(h.semanticClears.at(-1).model, h.a)
  const refreshed = provider.provideDocumentSemanticTokens(h.a)
  const requestId = h.sent().at(-1).id
  h.sockets.at(-1).receive({ id: 'semantic-refresh', method: 'workspace/semanticTokens/refresh' })
  assert.equal(h.semanticClears.at(-1).model, undefined)
  h.sockets.at(-1).receive({ id: requestId, result: { data: [0, 0, 3, 0, 1] } })
  assert.equal(await refreshed, null)
  assert.equal(h.semanticRecords.length, 1, 'a response predating refresh cannot repopulate the cache')
  const invalid = provider.provideDocumentSemanticTokens(h.a); h.respond({ data: [0, 0, 3, 100, 0] })
  assert.equal(await invalid, null); assert.equal(h.semanticRecords.length, 1)
  const oldModel = h.a
  h.client.openDocument({ uri: uriA, model: model('foo', 'java') })
  assert.equal(h.semanticClears.at(-1).model, oldModel)
  h.client.closeDocument(uriA)
  assert.equal(h.semanticClears.at(-1).model.getLanguageId(), 'java')
  h.ready(semanticCapabilities)
  assert.equal(h.semanticClears.at(-1).model, undefined)
  h.sockets.at(-1).close()
  assert.equal(h.semanticClears.at(-1).model, undefined)
  h.client.dispose()
  assert.equal(h.semanticClears.at(-1).owner, h.client)
  assert.equal(h.semanticClears.at(-1).model, undefined)
})

test('format/range formatting return local undoable edits; readonly checks suppress edits while navigation stays available', async t => {
  let writable = true; const h = await setup(t, { canEdit: () => writable })
  const format = h.provider('DocumentFormattingEdit').provideDocumentFormattingEdits(h.a, { tabSize: 2, insertSpaces: true }); h.respond([{ range: range(), newText: 'formatted' }]); assert.equal((await format)[0].text, 'formatted'); assert.equal(h.a.text, 'foo'); assert.equal(h.edits.length, 0)
  const rangeFormat = h.provider('DocumentRangeFormattingEdit').provideDocumentRangeFormattingEdits(h.a, { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 }, { tabSize: 4 }); assert.equal(h.sent().at(-1).method, 'textDocument/rangeFormatting'); h.respond([]); await rangeFormat
  writable = false; const count = h.sent().length; assert.equal((await h.provider('CompletionItem').provideCompletionItems(h.a, pos, {})).suggestions.length, 0); assert.equal((await h.provider('DocumentFormattingEdit').provideDocumentFormattingEdits(h.a, {})).length, 0); assert.equal(h.sent().length, count)
  const hover = h.provider('Hover').provideHover(h.a, pos); h.respond({ contents: 'readable' }); assert.equal((await hover).contents.at(-1).value, 'readable')
  writable = true; const late = h.client.formatDocument(uriA); writable = false; h.respond([{ range: range(), newText: 'late' }]); await assert.rejects(late, /只读/)
})

test('symbols map hierarchical/flat results and workspace query remains a read request', async t => {
  const h = await setup(t), symbols = h.provider('DocumentSymbol').provideDocumentSymbols(h.a)
  h.respond([{ name: 'Class', kind: 5, range: range(0, 0, 3, 0), selectionRange: range(), children: [{ name: 'method', kind: 6, range: range(1, 0, 2, 0) }] }, { name: 'flat', kind: 12, location: { uri: uriA, range: range(4, 0, 4, 3) } }])
  const result = await symbols; assert.equal(result[0].kind, 4); assert.equal(result[0].children[0].selectionRange.startLineNumber, 2); assert.equal(result[1].range.startLineNumber, 5)
  const search = h.client.workspaceSymbols('Foo'); assert.equal(h.sent().at(-1).method, 'workspace/symbol'); assert.deepEqual(h.sent().at(-1).params, { query: 'Foo' }); h.respond([]); await search
})

test('workspace symbol responses and same-URI model replacements cannot return stale positions', async t => {
  const h = await setup(t), search = h.client.workspaceSymbols('Foo'); h.b.set('dependency changed'); h.respond([]); await assert.rejects(search, /工作区文档已变化/)
  const pending = h.client.symbols(uriA); h.client.openDocument({ uri: uriA, model: model('foo') }); h.respond([]); await assert.rejects(pending, /文档已变化/)
})

test('code action edits use preview hook with request-time all-document snapshots and never execute supplied commands', async t => {
  const h = await setup(t), p = h.provider('CodeAction'), pending = p.provideCodeActions(h.a, { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 }, { markers: [] })
  const rawEdit = { changes: { [uriA]: [{ range: range(), newText: 'x' }], [uriB]: [{ range: range(), newText: 'y' }] } }
  h.respond([{ title: 'Fix both', kind: 'quickfix', edit: rawEdit, command: { command: 'arbitrary.shell' } }, { title: 'Unsafe command', command: 'arbitrary.shell' }]); const actions = (await pending).actions
  assert.equal(actions[0].edit, undefined); assert.match(actions[1].disabled, /服务器命令/); h.b.set('later dependency edit')
  assert.equal(await h.commands.get(actions[0].command.id)(null, actions[0]), true); assert.equal(h.edits.length, 1)
  assert.equal(h.edits[0].snapshots[uriB], 'bar'); assert.equal(h.edits[0].versions[uriA], 1); assert.equal(h.edits[0].title, 'Fix both'); assert.equal(h.a.text, 'foo')
  assert.equal(await h.client.applyCodeAction(actions[1]), false); assert.equal(h.sent().some(frame => frame.method === 'workspace/executeCommand'), false)
  h.a.set('stale origin'); assert.equal(await h.client.applyCodeAction(actions[0]), false); assert.equal(h.edits.length, 1)
})

test('advertised Java organizeImports command returns a previewed WorkspaceEdit and never applies unreviewed changes', async t => {
  const h = await setup(t, {}, { codeActionProvider: true, executeCommandProvider: { commands: ['java.edit.organizeImports'] } }), provider = h.provider('CodeAction')
  const codeAction = { title: 'Organize imports', command: { command: 'java.edit.organizeImports', arguments: [uriA] } }
  const fetchAction = async raw => {
    const pending = provider.provideCodeActions(h.a, { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 }, {})
    h.respond([raw]); return (await pending).actions[0]
  }
  const action = await fetchAction(codeAction)
  assert.equal(action.disabled, undefined)
  const pending = h.client.applyCodeAction(action)
  assert.equal(h.sent().at(-1).method, 'workspace/executeCommand')
  assert.deepEqual(h.sent().at(-1).params, { command: 'java.edit.organizeImports', arguments: [uriA] })
  h.respond({ changes: { [uriA]: [{ range: range(), newText: 'imports' }] } })
  assert.equal(await pending, true); assert.equal(h.edits.length, 1)
  assert.equal(h.edits[0].source, 'organizeImports'); assert.equal(h.a.text, 'foo')
  const unsafe = await fetchAction({ title: 'Wrong target', command: { command: 'java.edit.organizeImports', arguments: [uriB] } })
  assert.ok(unsafe.disabled)
  const count = h.sent().length
  assert.equal(await h.client.applyCodeAction(unsafe), false); assert.equal(h.sent().length, count)
  const next = await fetchAction(codeAction), stale = h.client.applyCodeAction(next)
  h.b.set('background changed'); h.respond({ changes: { [uriA]: [{ range: range(), newText: 'old' }] } })
  assert.equal(await stale, false); assert.equal(h.edits.length, 1)
  h.ready({ codeActionProvider: true })
  const notAdvertised = await fetchAction(codeAction)
  assert.ok(notAdvertised.disabled)
})

test('resolve code actions preserves initial snapshots and direct API edit results carry the same review metadata', async t => {
  const h = await setup(t), actions = h.client.codeActions(uriA, range(), { diagnostics: [] })
  const edit = { documentChanges: [{ textDocument: { uri: uriA, version: 1 }, edits: [{ range: range(), newText: 'fixed' }] }] }
  h.respond([{ title: 'Direct API', edit }]); const raw = (await actions)[0]; assert.equal(await h.client.reviewEdit(raw.edit, { title: raw.title }), true); assert.equal(h.edits[0].snapshots[uriA], 'foo')
  const pending = h.provider('CodeAction').provideCodeActions(h.a, range(), { markers: [] }); h.respond([{ title: 'Deferred', kind: 'quickfix', data: { id: 1 } }]); const action = (await pending).actions[0]
  const resolve = h.client.applyCodeAction(action); assert.equal(h.sent().at(-1).method, 'codeAction/resolve'); h.respond({ title: 'Resolved deferred', edit }); assert.equal(await resolve, true); assert.equal(h.edits.at(-1).title, 'Resolved deferred')
  const direct = h.client.codeActions(uriA, range(), { diagnostics: [] }); h.respond([{ title: 'Direct deferred', data: { id: 2 } }]); const rawDeferred = (await direct)[0]
  const resolvedRaw = h.client.resolveCodeAction(rawDeferred); h.respond({ title: 'Resolved API', edit }); const apiAction = await resolvedRaw
  assert.equal(await h.client.reviewEdit(apiAction.edit, { title: apiAction.title }), true); assert.equal(h.edits.at(-1).snapshots[uriB], 'bar')
})

test('code actions forward original diagnostic data and reject readonly targets without reviewing any partial edit', async t => {
  const h = await setup(t, { canEdit: m => m?.writable !== false }), diagnostic = { range: range(), message: 'fixable', data: { secretFixId: 2 }, tags: [1] }
  h.client._publish(uriA, [diagnostic], 1)
  const pending = h.provider('CodeAction').provideCodeActions(h.a, range(), { markers: [] }); assert.deepEqual(h.sent().at(-1).params.context.diagnostics[0].data, { secretFixId: 2 })
  h.respond([{ title: 'Fix readonly dependency', edit: { changes: { [uriA]: [{ range: range(), newText: 'a' }], [uriB]: [{ range: range(), newText: 'b' }] } } }]); const action = (await pending).actions[0]
  h.b.writable = false; assert.equal(await h.client.applyCodeAction(action), false); assert.equal(h.edits.length, 0)
})

test('rename prepare targets exact ranges and rename returns empty Monaco edits after cross-file preview', async t => {
  const h = await setup(t), p = h.provider('Rename'), prepare = p.resolveRenameLocation(h.a, pos)
  h.respond({ range: range(0, 0, 0, 3), placeholder: 'foo' }); assert.equal((await prepare).text, 'foo')
  const pending = p.provideRenameEdits(h.a, pos, 'renamed'); assert.equal(h.sent().at(-1).params.newName, 'renamed'); h.respond({ changes: { [uriA]: [{ range: range(), newText: 'renamed' }], [uriB]: [{ range: range(), newText: 'renamed' }] } })
  assert.deepEqual(clone(await pending), { edits: [] }); assert.equal(h.edits.at(-1).source, 'rename'); assert.equal(h.edits.at(-1).snapshots[uriA], 'foo'); assert.equal(h.a.text, 'foo'); assert.equal(h.b.text, 'bar')
})

test('server requested applyEdit is only reviewed and acknowledged not applied; notification review includes current drafts', async t => {
  const h = await setup(t), edit = { changes: { [uriA]: [{ range: range(), newText: 'server' }] } }
  h.a.set('unsent draft'); h.sockets[0].receive({ id: 41, method: 'workspace/applyEdit', params: { label: 'Server proposal', edit } }); await tick()
  const response = h.sent().find(frame => frame.id === 41 && !frame.method); assert.equal(response.result.applied, false); assert.equal(h.edits[0].snapshots[uriA], 'unsent draft'); assert.equal(h.a.text, 'unsent draft')
  h.sockets[0].receive({ method: 'code-agent/reviewEdit', params: { request_id: 's2', label: 'Notification proposal', edit } }); await tick(); assert.equal(h.edits.at(-1).requestId, 's2')
})

test('dispose closes transport, cancels requests/ready waits, removes registrations/commands and leaves workspace-owned models alive', async t => {
  const h = await setup(t), pending = h.client.request('slow', {}), token = cancellation(), another = h.client.request('another', {}, { token })
  h.client.dispose(); await assert.rejects(pending, /释放/); await assert.rejects(another, /释放/); await assert.rejects(h.client.waitForReady(), /释放/)
  assert.equal(h.client.pending.size, 0); assert.equal(h.commands.size, 0); assert.equal(h.registrations.every(item => item.disposed), true); assert.equal(h.a.disposed, false); assert.equal(token.disposed, 1)
  assert.equal(h.sockets[0].readyState, 3); assert.equal(h.client.documents.size, 0)
})

test('disposing scheduled reconnect prevents a new transport and late callbacks cannot restore disposed clients', async t => {
  const h = await setup(t, { autoReconnect: true, reconnectDelayMs: 5 }), first = h.sockets[0]
  first.close(); h.client.dispose(); await new Promise(resolve => setTimeout(resolve, 15)); assert.equal(h.sockets.length, 1)
  first.receive({ method: 'code-agent/ready', params: { server: 'late', capabilities: allCapabilities } }); assert.equal(h.client.ready, false); assert.equal(h.client.documents.size, 0)
})

test('fatal startup/auth errors preserve actionable detail and do not repeatedly restart language services', async t => {
  const h = await setup(t, { autoReconnect: true, reconnectDelayMs: 5 }), first = h.sockets[0]
  first.receive({ method: 'code-agent/error', params: { message: 'Install pyright in this project environment' } }); first.readyState = 3; first.onclose({ code: 4500, reason: 'lsp start failed' })
  await new Promise(resolve => setTimeout(resolve, 15)); assert.equal(h.sockets.length, 1); assert.deepEqual(h.statuses.at(-1), ['error', 'Install pyright in this project environment'])
  h.client.connect(); assert.equal(h.sockets.length, 2)
})

// Incremental-sync regression coverage. Apply the emitted range independently
// so a small payload is accepted only when it reconstructs the complete draft.
function applyIncrementalChange(previous, change) {
  const offset = position => {
    const breaks = /\r\n|\r|\n/g
    let line = 0, lineStart = 0, match
    while (line < position.line && (match = breaks.exec(previous))) { line++; lineStart = breaks.lastIndex }
    assert.equal(line, position.line)
    const lineEnd = previous.slice(lineStart).search(/\r|\n/)
    assert.ok(position.character <= (lineEnd < 0 ? previous.length - lineStart : lineEnd), 'range remains within a line')
    return lineStart + position.character
  }
  const start = offset(change.range.start), end = offset(change.range.end)
  assert.equal(change.rangeLength, end - start)
  return previous.slice(0, start) + change.text + previous.slice(end)
}

test('incremental changes send only the modified span for insert/delete, multiline and UTF16/CRLF drafts', async t => {
  const h = await setup(t, {}, { textDocumentSync: { change: 2 } })
  const cases = [
    ['foo', 'foobar', range(0, 3, 0, 3), 'bar'],
    ['foo', 'barfoo', range(0, 0, 0, 0), 'bar'],
    ['fooBARbaz', 'foobaz', range(0, 3, 0, 6), ''],
    ['alpha\nomega', 'alpha\nbravo\nomega', range(1, 0, 1, 0), 'bravo\n'],
    ['alpha\r\nomega', 'alpha\nomega', range(0, 5, 1, 0), '\n'],
    ['a\r\nb', 'a\rb', range(0, 1, 1, 0), '\r'],
    ['a\rb', 'a\r\nb', range(0, 1, 1, 0), '\r\n'],
    ['a😀z', 'a😁z', range(0, 1, 0, 3), '😁'],
    ['a\ud83d\ude00z', 'a\ud83e\ude00z', range(0, 1, 0, 3), '\ud83e\ude00'],
    ['a😀b\nold', 'a😀b\nnew', range(1, 0, 1, 3), 'new'],
    ['a\rb', 'a\rBb', range(1, 0, 1, 0), 'B'],
    ['\r\nfoo', '\r\nfoo!', range(1, 3, 1, 3), '!'],
    ['', '😀\r\n你好', range(0, 0, 0, 0), '😀\r\n你好'],
    ['😀\r\n你好', '', range(0, 0, 1, 2), ''],
  ]
  for (const [previous, next, expectedRange, expectedText] of cases) {
    h.client.closeDocument(uriA); h.a.set(previous); h.client.openDocument({ uri: uriA, model: h.a })
    h.client.changeDocument(uriA, next)
    const changes = h.sent().at(-1).params.contentChanges
    assert.equal(changes.length, 1)
    assert.deepEqual(changes[0].range, expectedRange, `${JSON.stringify(previous)} -> ${JSON.stringify(next)}`)
    assert.equal(changes[0].text, expectedText)
    assert.equal(applyIncrementalChange(previous, changes[0]), next)
    assert.equal(h.client.documents.get(uriA).text, next)
    assert.equal(h.sent().at(-1).params.textDocument.version, 2)
  }
})

test('one-character edit in a large draft stays small; unchanged drafts send no frame or version', async t => {
  const h = await setup(t, {}, { textDocumentSync: 2 })
  const previous = 'class Item {\r\n'.repeat(13000) + 'return 1;\r\n}'
  h.client.closeDocument(uriA); h.a.set(previous); h.client.openDocument({ uri: uriA, model: h.a })
  const next = previous.replace('return 1;', 'return 2;')
  h.client.changeDocument(uriA, next)
  const frame = h.sent().at(-1), change = frame.params.contentChanges[0]
  assert.equal(change.text, '2'); assert.equal(change.rangeLength, 1)
  assert.deepEqual(change.range, range(13000, 7, 13000, 8))
  assert.equal(applyIncrementalChange(previous, change), next)
  assert.ok(JSON.stringify(frame).length < 400)
  const count = h.sent().length, version = h.client.documents.get(uriA).version
  h.client.changeDocument(uriA, next)
  assert.equal(h.sent().length, count); assert.equal(h.client.documents.get(uriA).version, version)
})

test('full-sync servers retain whole text and incremental offline edits replay as complete didOpen on reconnect', async t => {
  const h = await setup(t, {}, { textDocumentSync: 1 })
  h.client.changeDocument(uriA, 'fooa')
  assert.deepEqual(h.sent().at(-1).params.contentChanges, [{ text: 'fooa' }])
  h.ready({ textDocumentSync: 2 }); const first = h.sockets[0]
  first.close(); h.a.set('offline😀\r\ndraft'); h.client.changeDocument(uriA, h.a.text)
  const version = h.client.documents.get(uriA).version
  h.client.connect(); h.ready({ textDocumentSync: { change: 2 } })
  const replay = h.sent().find(frame => frame.method === 'textDocument/didOpen' && frame.params.textDocument.uri === uriA)
  assert.equal(replay.params.textDocument.text, h.a.text); assert.equal(replay.params.textDocument.version, version)
  h.a.set('offline😀\r\ndraft!'); h.client.changeDocument(uriA, h.a.text)
  assert.equal(applyIncrementalChange(replay.params.textDocument.text, h.sent().at(-1).params.contentChanges[0]), h.a.text)
})
