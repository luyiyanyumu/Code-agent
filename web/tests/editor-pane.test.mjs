// 真正的 SFC 脚本 + 假 Monaco/LSP/API；不读写测试以外的文件、不连接后端。
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, markRaw, nextTick, reactive, ref, watch } from 'vue'
import { WorkspaceLanguageService } from '../src/lib/workspaceLanguageService.js'
import { getTokenHover, getJavaTokenTarget, registerTokenBusinessSource, withTokenBusiness, clearSemanticTokens, subscribeHoverDocumentationUpdates, notifyHoverDocumentationUpdated } from '../src/lib/tokenHover.js'
import { createBusinessMeaningSource, subscribeBusinessMeaning, notifyBusinessMeaningChanged } from '../src/lib/businessMeaning.js'
import { PREFERENCE_DEFAULTS, editorPreferenceOptions } from '../src/lib/preferences.js'

const clone = value => JSON.parse(JSON.stringify(value))
const settle = async () => { await nextTick(); await new Promise(setImmediate); await nextTick() }
const initialFile = overrides => ({ path: 'src/a.py', text: 'disk', diskText: 'disk', lang: 'python', uri: 'file:///E:/project/src/a.py', sha1: 'sha-a', ...overrides })
const viewState = (line, column, scrollTop) => ({ cursorState: [{ position: { lineNumber: line, column } }], viewState: { scrollTop, scrollLeft: 12 } })

async function loadScript(relative, bindings, exports) {
  let source = await readFile(new URL(relative, import.meta.url), 'utf8')
  if (relative.endsWith('.vue')) source = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  source = source.replace(/^import .*\r?\n/gm, '').replace(/^export default .*\r?\n/gm, '').replace(/^export /gm, '')
    .replace(/await import\('([^']+)'\)/g, 'await __modules["$1"]')
  const context = vm.createContext({ URL, ...bindings })
  vm.runInContext(`${source}\n;globalThis.subject = { ${exports} }`, context)
  return context.subject
}

function fakeMonaco(fixtureOptions = {}) {
  const models = new Map(), editors = [], openers = []
  class Range {
    constructor(startLineNumber, startColumn, endLineNumber, endColumn) {
      Object.assign(this, { startLineNumber, startColumn, endLineNumber, endColumn })
    }
  }
  const monaco = {
    Range, KeyMod: { CtrlCmd: 1 }, KeyCode: { KeyS: 2, F9: 9 },
    Uri: { parse: value => ({ toString: () => value }), from: ({ scheme, authority, path }) => ({ toString: () => `${scheme}://${authority}${path}` }) },
    editor: {
      MouseTargetType: { GUTTER_GLYPH_MARGIN: 2 },
      setModelLanguage(model, lang) { model.lang = lang },
      registerEditorOpener(opener) {
        const registration = { opener, disposed: false, dispose() { this.disposed = true } }
        openers.push(registration)
        return registration
      },
      create(host, options) {
        const editor = {
          host, options, model: options.model, disposed: false, calls: [], listeners: [],
          selection: new Range(1, 1, 1, 1), scrollTop: 0, scrollLeft: 0,
          getModel() { return this.model }, getValue() { return this.model.getValue() },
          getPosition() { return { lineNumber: this.selection.startLineNumber, column: this.selection.startColumn } },
          onDidChangeModelContent(fn) { this.content = fn },
          onDidChangeCursorSelection(fn) { this.listeners.push(fn); return { dispose: () => { this.listeners = this.listeners.filter(item => item !== fn) } } },
          onDidLayoutChange(fn) { this.layout = fn }, addAction() {},
          addCommand(key, fn) { (this.commands ||= new Map()).set(key, fn) }, focus() { this.focusCount = (this.focusCount || 0) + 1 },
          onMouseDown(fn) { this.mouseDown = fn; return { dispose: () => { this.mouseDown = null } } },
          createDecorationsCollection() {
            this.decorations = []
            return { set: items => { this.decorationWrites = (this.decorationWrites || 0) + 1; this.decorations = clone(items); if (fixtureOptions.reentrantDecorations) this.content?.() }, clear: () => { this.decorations = [] } }
          },
          updateOptions(options) { Object.assign(this.options, options) },
          pushUndoStop() { this.calls.push(['undo-stop']) },
          executeEdits(source, edits) {
            this.calls.push(['edit', source, clone(edits)])
            const offset = (line, column) => this.model.text.split('\n').slice(0, line - 1).reduce((n, value) => n + value.length + 1, 0) + column - 1
            const operations = edits.map(edit => ({ start: offset(edit.range.startLineNumber, edit.range.startColumn), end: offset(edit.range.endLineNumber, edit.range.endColumn), text: edit.text })).sort((a, b) => b.start - a.start)
            let text = this.model.text
            for (const edit of operations) text = text.slice(0, edit.start) + edit.text + text.slice(edit.end)
            this.model.setValue(text)
          },
          getAction(name) { return fixtureOptions.missingActions?.includes(name) ? null : { run: async () => { this.calls.push(['action', name]) } } },
          trigger(source, action, payload) { this.calls.push(['trigger', source, action, payload]) },
          getSelection() { return this.selection },
          setSelection(selection) { this.selection = selection; this.listeners.forEach(fn => fn({ selection })) },
          revealLineInCenter(line) { this.calls.push(['jump', line]); this.scrollTop = line * 22 },
          getVisibleRanges() { return [{ startLineNumber: 1, endLineNumber: 10000 }] },
          saveViewState() { return viewState(this.selection.startLineNumber, this.selection.startColumn, this.scrollTop) },
          restoreViewState(state) {
            this.calls.push(['restore', clone(state)])
            const position = state.cursorState[0].position
            this.selection = new Range(position.lineNumber, position.column, position.lineNumber, position.column)
            this.scrollTop = state.viewState.scrollTop
            this.scrollLeft = state.viewState.scrollLeft
          },
          dispose() { this.disposed = true; this.model.owner = null; this.listeners = [] },
        }
        options.model.owner = editor
        editors.push(editor)
        return editor
      },
    },
  }
  const module = {
    ensureMonaco: () => monaco, monacoLanguage: lang => lang,
    getModel(path, text, lang, fileUri, scope) {
      const uri = fileUri || `code-agent://project/${scope}/${path}`
      if (!models.has(uri)) {
        models.set(uri, {
          path, text, lang, uri: { toString: () => uri }, disposed: false, version: 1,
          options: { insertSpaces: fixtureOptions.modelInsertSpaces ?? true },
          getValue() { return this.text }, getLanguageId() { return this.lang }, getVersionId() { return this.version },
          isDisposed() { return this.disposed }, getOptions() { return { tabSize: 4, insertSpaces: true, ...this.options } },
          updateOptions(options) { this.options = { ...this.options, ...options } },
          setValue(text) { this.text = text; this.version++; this.owner?.content?.() },
          dispose() { this.disposed = true; this.disposeCount = (this.disposeCount || 0) + 1; models.delete(uri) },
        })
      }
      return models.get(uri)
    },
  }
  return { monaco, module, models, editors, openers }
}

async function setup(t, overrides = {}, options = {}) {
  const props = reactive({
    projectId: 'p', tree: 'main', sessionId: 's', documentKey: 'p/main/src/a.py',
    file: initialFile(), highlight: null, restoreViewState: null, readonly: false, saving: false,
    lspServer: { available: false }, documents: [], languageServers: [], breakpoints: [], debugLocation: null, ...overrides,
  })
  const fake = fakeMonaco(options), events = [], timers = new Map(), clients = []
  let timerId = 0, unmount, exposed
  class FakeLspClient {
    constructor(clientOptions) { this.options = clientOptions; this.documents = new Map(); this.calls = []; this.capabilities = options.capabilities?.[clientOptions.serverId || 'primary'] || options.capabilities || {}; clients.push(this) }
    connect() {} async waitForReady() {}
    openDocument(document) { this.documents.set(document.uri, document); this.calls.push(['open', document.uri, document.text]) }
    registerProviders() { this.calls.push(['providers']) }
    changeDocument(uri, text) { this.calls.push(['change', uri, text]) }
    saveDocument(uri, text) { this.calls.push(['save', uri, text]) }
    notify(method, params) { this.calls.push(['notify', method, params]) }
    closeDocument(uri) { this.documents.delete(uri); this.calls.push(['close', uri]) }
    dispose() { this.disposed = true; this.calls.push(['dispose']) }
    async formatDocument(uri, formatOptions) { this.calls.push(['format', uri, formatOptions]); return typeof options.format === 'function' ? options.format() : options.format || [] }
    async symbols(uri) { this.calls.push(['symbols', uri]); return options.symbols || [] }
    async workspaceSymbols(query) { this.calls.push(['workspace-symbols', query]); return options.workspaceSymbols || [] }
    async hover(uri, position, token) {
      this.calls.push(['hover', uri, position, token])
      return typeof options.hover === 'function' ? options.hover(token) : options.hover || null
    }
  }
  if (options.existingText != null) fake.module.getModel(props.file.path, options.existingText, props.file.lang, props.file.uri, `${props.projectId}/${props.tree}`)
  const scope = effectScope()
  const preferences = reactive({ ...PREFERENCE_DEFAULTS, ...options.preferences })
  const subject = await scope.run(() => loadScript('../src/components/code/EditorPane.vue', {
    computed, markRaw, ref, watch: (...args) => scope.run(() => watch(...args)), nextTick, WorkspaceLanguageService, getTokenHover,
    getJavaTokenTarget, registerTokenBusinessSource, withTokenBusiness, createBusinessMeaningSource, subscribeBusinessMeaning, subscribeHoverDocumentationUpdates,
    usePreferences: () => ({ preferences }), editorPreferenceOptions,
    defineProps: () => props, defineEmits: () => (name, payload) => events.push([name, clone(payload)]),
    defineExpose: methods => { exposed = methods }, onMounted() {}, onBeforeUnmount: fn => { unmount = fn },
    requestAnimationFrame: fn => fn(),
    setTimeout: function (fn, delay) { assert.equal(this, undefined, 'native browser timers cannot use the manager as their receiver'); timers.set(++timerId, { fn, delay }); return timerId },
    clearTimeout: function (id) { assert.equal(this, undefined, 'native browser timer cancellation needs its original receiver'); return timers.delete(id) },
    api: options.api || { code: new Proxy({}, { get: () => () => { throw new Error('API not allowed in editor regression') } }) },
    __modules: { '../../lib/monacoSetup.js': fake.module, '../../lib/lspClient.js': { LspClient: FakeLspClient } },
  }, 'mount, hostEl, dirty, error, lspState, plainFile, snapshot, releaseDocument, save, markSaved, relativeTargetPath, connectLsp, toggleBreakpoint, notifySaved, notifyResourceChanges, formatDocument, languageEditorAction, languageCapabilities, languageServerIds, symbolPanel, symbolMode, symbolQuery, symbolItems, toggleSymbolPanel, setSymbolMode, openLanguageSymbol, toggleLspPanel, showDocumentation, closeDocumentation, symbolDocumentation, showHierarchy, hierarchy, navigateHierarchy, showLensReferences, lensReferences'))
  subject.hostEl.value = { rendered: true }
  await subject.mount()
  await settle()
  let released = false
  const dispose = () => { if (!released) { unmount(); scope.stop(); released = true } }
  t.after(dispose)
  return { subject, props, events, fake, clients, timers, exposed, dispose, preferences }
}

test('editor preferences apply at mount and update cached models without changing content', async t => {
  const { fake, preferences, subject } = await setup(t, { file: initialFile({ text: '\tdisk', diskText: '\tdisk' }) },
    { modelInsertSpaces: false, preferences: { editorFontSize: 18, editorTabSize: 2 } })
  const editor = fake.editors[0]
  assert.equal(editor.options.fontSize, 18)
  assert.equal(editor.model.getOptions().tabSize, 2)
  assert.equal(editor.options.insertSpaces, true)
  assert.equal(editor.model.getOptions().insertSpaces, true, 'old tab-based models adopt space insertion')
  const before = subject.snapshot().text
  Object.assign(preferences, { editorFontSize: 20, editorTabSize: 8, editorWordWrap: 'on', editorMinimap: true,
    editorLineNumbers: 'relative', editorWhitespace: 'all', reducedMotion: true })
  await settle()
  assert.equal(editor.options.fontSize, 20)
  assert.equal(editor.options.lineHeight, 32)
  assert.equal(editor.options.wordWrap, 'on')
  assert.equal(editor.options.minimap.enabled, true)
  assert.equal(editor.options.lineNumbers, 'relative')
  assert.equal(editor.options.renderWhitespace, 'all')
  assert.equal(editor.options.smoothScrolling, false)
  assert.equal(editor.model.getOptions().tabSize, 8)
  assert.equal(editor.model.getOptions().insertSpaces, true)
  assert.equal(subject.snapshot().text, before)
  assert.equal(subject.dirty.value, false)
})

test('editor mounts the document draft, preserves its dirty baseline, and exposes snapshot', async t => {
  const { subject: s, fake, exposed } = await setup(t, { file: initialFile({ text: 'unsaved draft', dirty: true }) }, { existingText: 'stale disk model' })
  assert.equal(s.snapshot().text, 'unsaved draft')
  assert.equal(s.snapshot().key, 'p/main/src/a.py')
  assert.equal(s.snapshot().path, 'src/a.py')
  assert.equal(s.dirty.value, true)
  assert.equal(fake.editors[0].options.fontSize, 14)
  assert.equal(fake.editors[0].options.lineHeight, 22)
  assert.equal(fake.editors[0].options.readOnly, false)
  assert.equal(fake.editors[0].options['semanticHighlighting.enabled'], true)
  assert.equal(fake.editors[0].options.bracketPairColorization.enabled, true)
  assert.equal(fake.editors[0].options.guides.bracketPairs, 'active')
  assert.equal(typeof exposed.snapshot, 'function')
})

test('glyph margin and F9 toggle Python breakpoints without editing or dirtying the document', async t => {
  const { subject: s, fake, events, exposed } = await setup(t, { readonly: true })
  const editor = fake.editors[0]
  assert.equal(editor.options.glyphMargin, true)
  editor.mouseDown({ target: { type: 2, position: { lineNumber: 7 } } })
  editor.mouseDown({ target: { type: 6, position: { lineNumber: 8 } } })
  editor.setSelection(new fake.monaco.Range(12, 1, 12, 1))
  editor.commands.get(fake.monaco.KeyCode.F9)()
  assert.deepEqual(events.filter(([name]) => name === 'breakpoint').map(([, payload]) => payload), [
    { path: 'src/a.py', line: 7 }, { path: 'src/a.py', line: 12 },
  ])
  assert.equal(exposed.toggleBreakpoint(0), false)
  assert.equal(exposed.toggleBreakpoint(), true)
  assert.equal(s.snapshot().text, 'disk')
  assert.equal(s.dirty.value, false)
})

test('debug decorations follow models and clear paused highlight on continue without changing cursor', async t => {
  const { props, fake, subject: s } = await setup(t, {
    breakpoints: [{ path: 'src/a.py', line: 3 }, { path: 'src/b.py', line: 5 }],
    debugLocation: { path: 'src/a.py', line: 7 },
  })
  const first = fake.editors[0]
  assert.deepEqual(first.decorations.map(item => [item.range.startLineNumber, item.options.glyphMarginClassName]), [[3, 'ide-breakpoint-glyph'], [7, 'ide-debug-glyph']])
  first.setSelection(new fake.monaco.Range(22, 4, 22, 4))
  props.debugLocation = null
  await settle()
  assert.equal(first.decorations.length, 1)
  assert.equal(s.snapshot().selection.start_line, 22)
  props.file = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py' })
  props.documentKey = 'p/main/src/b.py'
  await settle()
  assert.equal(first.decorations.length, 0, 'disposed editor does not leave debug glyphs in its cached model')
  assert.deepEqual(fake.editors.at(-1).decorations.map(item => item.range.startLineNumber), [5])
  props.debugLocation = { path: 'src/a.py', line: 7 }
  await settle()
  assert.equal(fake.editors.at(-1).decorations.length, 1, 'paused line in another file is not painted here')
  props.file = initialFile()
  props.documentKey = 'p/main/src/a.py'
  await settle()
  assert.equal(fake.editors.at(-1).decorations.length, 2)
})

test('breakpoint toggling rejects unsupported files and the pending old model during a document switch', async t => {
  const { props, fake, subject: s, events } = await setup(t)
  props.documentKey = 'p/main/App.vue'
  props.file = initialFile({ path: 'App.vue', uri: 'file:///E:/project/App.vue', lang: 'vue' })
  assert.equal(s.toggleBreakpoint(3), false)
  await settle()
  fake.editors.at(-1).commands.get(fake.monaco.KeyCode.F9)()
  assert.equal(s.toggleBreakpoint(3), false)
  assert.equal(events.filter(([name]) => name === 'breakpoint').length, 0)
})

test('debug decoration view flush cannot recursively publish content or repeat the same model write', async t => {
  const { props, fake, events, subject: s } = await setup(t, { breakpoints: [{ path: 'src/a.py', line: 3 }] }, { reentrantDecorations: true })
  const editor = fake.editors[0], initialWrites = editor.decorationWrites
  editor.model.setValue('dirty replacement')
  assert.equal(editor.decorationWrites, initialWrites, 'content callback does not synchronously mutate decorations')
  await settle()
  assert.equal(editor.decorationWrites, initialWrites + 1)
  assert.equal(events.filter(([name]) => name === 'change').length, 1)
  editor.model.setValue('disk')
  await settle()
  assert.equal(s.dirty.value, false)
  assert.equal(editor.decorationWrites, initialWrites + 2)
  props.file = initialFile({ text: 'accepted external hunk', diskText: 'accepted external hunk' })
  await settle()
  assert.equal(s.snapshot().text, 'accepted external hunk')
  assert.equal(s.dirty.value, false)
  assert.equal(editor.decorationWrites, initialWrites + 3)
})

test('content and cursor events retain document identity and undo clears dirty against disk text', async t => {
  const { subject: s, fake, events } = await setup(t)
  const editor = fake.editors[0]
  editor.model.setValue('typed')
  assert.deepEqual(events.filter(([name]) => name === 'change').at(-1)[1], { key: 'p/main/src/a.py', path: 'src/a.py', text: 'typed' })
  assert.equal(s.dirty.value, true)
  editor.setSelection(new fake.monaco.Range(3, 2, 6, 9))
  assert.deepEqual(events.filter(([name]) => name === 'selection').at(-1)[1], {
    key: 'p/main/src/a.py', path: 'src/a.py', start_line: 3, end_line: 6, start_column: 2, end_column: 9,
  })
  editor.model.setValue('disk')
  assert.equal(s.dirty.value, false)
  assert.equal(s.snapshot().selection.end_column, 9)
})

test('switch flushes the old draft and view state while its workspace client stays alive', async t => {
  const first = initialFile()
  const { subject: s, props, fake, events, clients } = await setup(t, { file: first,
    lspServer: { available: true }, documents: [{ key: 'p/main/src/a.py', file: first, text: 'disk' }] })
  const old = fake.editors[0]
  old.model.setValue('typed')
  old.model.text = 'last byte before switch'
  old.setSelection(new fake.monaco.Range(12, 5, 12, 5))
  old.scrollTop = 242
  props.file = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py', text: 'b draft', diskText: 'b disk' })
  props.documentKey = 'p/main/src/b.py'
  props.documents.push({ key: 'p/main/src/b.py', file: props.file, text: 'b draft' })
  props.restoreViewState = viewState(31, 7, 530)
  await settle()
  assert.deepEqual(events.filter(([name]) => name === 'change').at(-1)[1], { key: 'p/main/src/a.py', path: 'src/a.py', text: 'last byte before switch' })
  const state = events.find(([name]) => name === 'view-state')[1]
  assert.equal(state.key, 'p/main/src/a.py')
  assert.equal(state.state.cursorState[0].position.lineNumber, 12)
  assert.equal(state.state.viewState.scrollTop, 242)
  assert.equal(clients.length, 1)
  assert.equal(clients[0].calls.some(([name]) => name === 'dispose' || name === 'close'), false)
  assert.equal(clients[0].calls.some(([name, uri, text]) => name === 'change' && uri === first.uri && text === 'last byte before switch'), true)
  assert.equal(clients[0].documents.has(first.uri), true)
  assert.equal(old.disposed, true)
  assert.equal(s.snapshot().text, 'b draft')
  assert.equal(s.snapshot().selection.key, 'p/main/src/b.py')
  assert.equal(s.snapshot().selection.start_line, 31)
  assert.equal(fake.editors.at(-1).scrollTop, 530)
  assert.equal(s.dirty.value, true)
})

test('returning to a document restores cursor and scroll; only a fresh jump token overrides restoration', async t => {
  const { subject: s, props, fake } = await setup(t, { highlight: { start: 2, end: 2, column: 3, token: 1 } })
  props.file = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py' })
  props.documentKey = 'p/main/src/b.py'
  props.restoreViewState = viewState(21, 6, 480)
  await settle()
  const editor = fake.editors.at(-1)
  assert.equal(s.snapshot().selection.start_line, 21)
  assert.equal(editor.scrollTop, 480)
  assert.equal(editor.calls.filter(([name]) => name === 'jump').length, 0)
  props.highlight = { start: 8, end: 8, column: 4, token: 2 }
  await settle()
  assert.equal(s.snapshot().selection.start_line, 8)
  assert.equal(s.snapshot().selection.start_column, 4)
  editor.setSelection(new fake.monaco.Range(29, 9, 29, 9))
  props.highlight = { start: 8, end: 8, column: 4, token: 2 }
  await settle()
  assert.equal(s.snapshot().selection.start_line, 29, 'parent rerender cannot pull cursor back to an old jump')
})

test('a fresh explicit jump takes priority over initial saved view state and keeps target column', async t => {
  const { subject: s, fake } = await setup(t, { restoreViewState: viewState(40, 2, 820), highlight: { start: 17, end: 17, column: 11, token: 4 } })
  assert.deepEqual(fake.editors[0].calls.map(([name]) => name), ['restore', 'jump'])
  assert.equal(s.snapshot().selection.start_line, 17)
  assert.equal(s.snapshot().selection.start_column, 11)
})

test('unmount flushes current content and cursor/scroll, clears timers, and unregisters editor opener', async t => {
  const { subject: s, fake, events, timers, dispose } = await setup(t)
  const editor = fake.editors[0]
  editor.model.setValue('typed')
  editor.model.text = 'final draft'
  editor.scrollTop = 390
  dispose()
  assert.equal(events.filter(([name]) => name === 'change').at(-1)[1].text, 'final draft')
  assert.equal(events.find(([name]) => name === 'view-state')[1].state.viewState.scrollTop, 390)
  assert.equal(timers.size, 0)
  assert.equal(fake.openers[0].disposed, true)
  assert.equal(editor.disposed, true)
  assert.equal([...fake.models.values()].every(model => model.disposed), true)
})

test('snapshot identity follows the mounted model before a pending parent document switch finishes', async t => {
  const { subject: s, props } = await setup(t)
  props.file = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py', text: 'b draft' })
  props.documentKey = 'p/main/src/b.py'
  assert.equal(s.snapshot().key, 'p/main/src/a.py')
  assert.equal(s.snapshot().path, 'src/a.py')
  assert.equal(s.snapshot().text, 'disk')
  await settle()
  assert.equal(s.snapshot().key, 'p/main/src/b.py')
})

test('closed document release waits for switching and disposes cached models once without touching the current model', async t => {
  const { subject: s, props, fake, dispose } = await setup(t)
  const firstModel = fake.editors[0].model
  const first = { key: 'p/main/src/a.py', path: 'src/a.py', file: { uri: 'file:///E:/project/src/a.py' } }
  assert.equal(s.releaseDocument(first), false, 'current model cannot be released')
  assert.equal(firstModel.disposed, false)
  props.file = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py' })
  props.documentKey = 'p/main/src/b.py'
  assert.equal(s.releaseDocument(first), false, 'queued Vue update has not detached the current model yet')
  await settle()
  const currentModel = fake.editors.at(-1).model
  assert.equal(s.releaseDocument(first), true)
  assert.equal(firstModel.disposed, true)
  assert.equal(firstModel.disposeCount, 1)
  assert.equal(currentModel.disposed, false)
  assert.equal(s.releaseDocument(first), false, 'closed model was removed from ownership cache')
  dispose()
  assert.equal(firstModel.disposeCount, 1, 'unmount never disposes a released model again')
  assert.equal(currentModel.disposeCount, 1)
})

test('closed document release also accepts backend file metadata without allocating a model', async t => {
  const { subject: s, props, fake } = await setup(t)
  props.file = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py' })
  props.documentKey = 'p/main/src/b.py'
  await settle()
  assert.equal(fake.models.size, 2)
  assert.equal(s.releaseDocument({ path: 'src/a.py', uri: 'file:///E:/project/src/a.py' }), true)
  assert.equal(fake.models.size, 1)
  assert.equal(s.releaseDocument({ path: 'src/not-opened.py', uri: 'file:///E:/project/src/not-opened.py' }), false)
  assert.equal(fake.models.size, 1)
})

test('closing an alias of the current URI detaches ownership without disposing the live model', async t => {
  const { subject: s, props, fake } = await setup(t)
  const sharedModel = fake.editors[0].model
  props.file = initialFile({ path: 'src/alias.py' })
  props.documentKey = 'p/main/src/alias.py'
  await settle()
  assert.equal(fake.editors.at(-1).model, sharedModel)
  assert.equal(s.releaseDocument({ key: 'p/main/src/a.py' }), true)
  assert.equal(sharedModel.disposed, false)
  props.file = initialFile({ path: 'src/c.py', uri: 'file:///E:/project/src/c.py' })
  props.documentKey = 'p/main/src/c.py'
  await settle()
  assert.equal(s.releaseDocument({ key: 'p/main/src/alias.py' }), true)
  assert.equal(sharedModel.disposeCount, 1)
})

test('save carries the captured document key and later typing survives its response', async t => {
  const { subject: s, props, fake, events } = await setup(t)
  const editor = fake.editors[0]
  editor.model.setValue('saved draft')
  s.save()
  assert.deepEqual(events.find(([name]) => name === 'save')[1], { key: 'p/main/src/a.py', path: 'src/a.py', text: 'saved draft', sha1: 'sha-a' })
  editor.model.setValue('saved draft plus later typing')
  s.markSaved('saved draft')
  props.file.text = 'saved draft'
  await settle()
  assert.equal(s.snapshot().text, 'saved draft plus later typing')
  assert.equal(s.dirty.value, true)
})

test('save cannot pair an old model with newly assigned file props before Vue switches the editor', async t => {
  const { subject: s, props, fake, events } = await setup(t)
  fake.editors[0].model.setValue('old document draft')
  props.file = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py', text: 'new document' })
  props.documentKey = 'p/main/src/b.py'
  s.save()
  assert.equal(events.some(([name]) => name === 'save'), false)
  await settle()
  assert.equal(s.snapshot().text, 'new document')
})

test('disk baseline updates independently of draft text clear the editor dirty marker', async t => {
  const { subject: s, props, fake } = await setup(t, { file: initialFile({ diskText: undefined, savedText: 'disk' }) })
  fake.editors[0].model.setValue('external save matches this draft')
  assert.equal(s.dirty.value, true)
  props.file.text = 'external save matches this draft'
  props.file.savedText = 'external save matches this draft'
  await settle()
  assert.equal(s.snapshot().text, 'external save matches this draft')
  assert.equal(s.dirty.value, false)
})

test('same document metadata replacement preserves its editor, cursor, selection, viewport and LSP connection', async t => {
  const { subject: s, props, fake, events, clients } = await setup(t, {
    file: initialFile({ diskText: undefined, savedText: 'disk' }),
    restoreViewState: viewState(1, 1, 0), lspServer: { available: true },
  })
  const editor = fake.editors[0], client = clients[0]
  editor.model.setValue('submitted draft')
  editor.setSelection(new fake.monaco.Range(12, 2, 15, 8))
  editor.scrollTop = 338
  const before = clone(s.snapshot())
  props.file = { ...props.file, text: 'submitted draft', savedText: 'submitted draft', sha1: 'new-sha', total_lines: 80, size: 1200, dirty: false }
  await settle()
  assert.equal(fake.editors.length, 1, 'metadata update must not recreate Monaco')
  assert.equal(fake.editors[0], editor)
  assert.equal(editor.disposed, false)
  assert.equal(clients.length, 1)
  assert.equal(client.disposed, undefined)
  assert.deepEqual(clone(s.snapshot()), before)
  assert.equal(s.dirty.value, false)
  assert.equal(events.some(([name]) => name === 'view-state'), false, 'metadata update must not flush/rebuild the current document')
})

test('save response metadata keeps later typing and selection context without restoring stale viewport', async t => {
  const { subject: s, props, fake, events } = await setup(t, {
    file: initialFile({ diskText: undefined, savedText: 'disk' }),
    restoreViewState: viewState(1, 1, 0), highlight: { start: 1, end: 1, token: 1 },
  })
  const editor = fake.editors[0]
  editor.model.setValue('submitted')
  s.save()
  editor.model.setValue('submitted plus later typing')
  editor.setSelection(new fake.monaco.Range(23, 4, 27, 11))
  editor.scrollTop = 510
  const selection = clone(s.snapshot().selection)
  props.file = { ...props.file, text: 'submitted plus later typing', savedText: 'submitted', sha1: 'saved-sha', dirty: true }
  props.highlight = { start: 1, end: 1, token: 1 }
  await settle()
  assert.equal(fake.editors.length, 1)
  assert.equal(s.snapshot().text, 'submitted plus later typing')
  assert.deepEqual(clone(s.snapshot().selection), selection)
  assert.equal(editor.scrollTop, 510)
  assert.equal(s.dirty.value, true)
  assert.deepEqual(events.filter(([name]) => name === 'selection').at(-1)[1], selection)
})

test('plain files stay quiet without a language server and readonly messaging describes manual permission', async t => {
  const { subject: s, clients } = await setup(t, { readonly: true, file: initialFile({ path: '.gitignore', lang: 'gitignore' }), lspServer: { available: true } })
  assert.equal(s.plainFile.value, true)
  assert.equal(s.lspState.value, 'idle')
  assert.equal(clients.length, 0)
  s.save()
  assert.match(s.error.value, /人工编辑/)
  assert.doesNotMatch(s.error.value, /AI|主干/)
})

test('encoding previews remain readonly with manual editing enabled and become editable only after a UTF-8 reload', async t => {
  const { subject: s, props, fake, clients, events } = await setup(t, {
    readonly: false, lspServer: { available: true },
    file: initialFile({ text: '中文日志', diskText: '中文日志', editable: false,
      readonly_reason: 'encoding', encoding: 'utf-8+gb18030',
      encoding_warning: '混合编码文件只读，保留原始字节', size: 236 * 1024 }),
  }, { capabilities: { documentFormattingProvider: true } })
  const editor = fake.editors[0]
  assert.equal(editor.options.readOnly, true)
  editor.model.setValue('attempted overwrite')
  s.save()
  assert.match(s.error.value, /混合编码.*只读/)
  assert.doesNotMatch(s.error.value, /人工编辑|过大/)
  assert.equal(events.some(([name]) => name === 'save'), false)
  assert.equal(await s.formatDocument(), false)
  assert.equal(clients.some(client => client.calls.some(([name]) => name === 'format')), false)
  props.file = { ...props.file, editable: true, readonly_reason: '', encoding: 'utf-8', encoding_warning: '' }
  await settle()
  assert.equal(editor.options.readOnly, false)
})

test('cross-file opener decodes backend file URI, accepts the active worktree, and creates no empty target model', async t => {
  const { subject: s, fake, events } = await setup(t, { tree: 'task:one', file: initialFile({ path: 'src/a.py', uri: 'file:///E:/worktrees/task%20one/src/a.py' }) })
  const editor = fake.editors[0], opener = fake.openers[0].opener
  assert.equal(opener.openCodeEditor(editor, { toString: () => 'file:///e%3A/worktrees/task%20one/src/%E4%B8%AD%E6%96%87%20file.py' }, { startLineNumber: 14, startColumn: 7 }), true)
  assert.deepEqual(events.filter(([name]) => name === 'open-file').at(-1)[1], { path: 'src/中文 file.py', line: 14, column: 7 })
  assert.equal(opener.openCodeEditor(editor, 'file:///E:/worktrees/task%20one/not-yet-loaded.py', { lineNumber: 9, column: 4 }), true)
  assert.equal(fake.models.size, 1, 'target content is fetched by parent before a real model can exist')
  const invalid = [
    'file:///E:/other-project/src/a.py', 'file:///E:/worktrees/task%20two/src/a.py',
    'file:///E:/worktrees/task%20one-sibling/src/a.py', 'file:///E:/worktrees/task%20one/src/../../outside.py',
    'https://example.invalid/a.py', 'file:///E:/worktrees/task%20one/src/a.py?tree=other',
    'file:///E:/worktrees/task%20one/src/a.py#fragment', 'file:///E:/worktrees/task%20one/%ZZ.py',
  ]
  const before = events.length
  for (const uri of invalid) {
    assert.equal(opener.openCodeEditor(editor, uri), false, uri)
    assert.equal(s.relativeTargetPath(uri), null, uri)
  }
  assert.equal(opener.openCodeEditor({}, 'file:///E:/worktrees/task%20one/src/b.py'), false)
  assert.equal(events.length, before)
})

test('fallback Monaco URI keeps project and tree boundaries and stale openers cannot open files', async t => {
  const { props, fake, events } = await setup(t, { file: initialFile({ uri: '' }) })
  const oldEditor = fake.editors[0], oldOpener = fake.openers[0].opener
  assert.equal(oldOpener.openCodeEditor(oldEditor, 'code-agent://project/p/main/src/b.py'), true)
  assert.equal(oldOpener.openCodeEditor(oldEditor, 'code-agent://project/other/main/src/b.py'), false)
  assert.equal(oldOpener.openCodeEditor(oldEditor, 'code-agent://project/p/task:other/src/b.py'), false)
  props.file = initialFile({ path: 'src/b.py', uri: '' })
  props.documentKey = 'p/main/src/b.py'
  const queued = events.length
  assert.equal(oldOpener.openCodeEditor(oldEditor, 'code-agent://project/p/main/src/c.py'), false, 'old opener is invalid as soon as props change, before Vue teardown')
  assert.equal(events.length, queued)
  await settle()
  const before = events.length
  assert.equal(oldOpener.openCodeEditor(oldEditor, 'code-agent://project/p/main/src/c.py'), false)
  assert.equal(events.length, before)
  assert.equal(fake.openers[0].disposed, true)
})

test('EditorPane connects LSP with the active tree and the draft rather than disk text', async t => {
  const { clients } = await setup(t, { tree: 'task:abc', lspServer: { available: true }, file: initialFile({ text: 'draft' }) })
  assert.equal(clients[0].options.tree, 'task:abc')
  assert.equal(clients[0].calls[0][2], 'draft')
})

test('LSP URI transport includes tree and LocationLink definition uses its selection range', async () => {
  const providers = {}, registrations = []
  class Range {
    constructor(startLineNumber, startColumn, endLineNumber, endColumn) { Object.assign(this, { startLineNumber, startColumn, endLineNumber, endColumn }) }
  }
  const register = name => (_lang, provider) => { providers[name] = provider; const disposable = { dispose() { this.disposed = true } }; registrations.push(disposable); return disposable }
  const monaco = {
    Range, Uri: { parse: uri => ({ toString: () => uri }) }, MarkerSeverity: {},
    languages: { CompletionItemKind: { Text: 0 }, registerCompletionItemProvider: register('completion'), registerHoverProvider: register('hover'), registerDefinitionProvider: register('definition'), registerReferenceProvider: register('references') },
    editor: { setModelMarkers() {} },
  }
  class Socket {
    static OPEN = 1
    constructor(url) { this.url = url; this.readyState = 1 }
    send() {} close() { this.readyState = 3 }
  }
  const { LspClient } = await loadScript('../src/lib/lspClient.js', { monaco, clearSemanticTokens, WebSocket: Socket, location: { protocol: 'http:', host: 'fixture.invalid' }, getToken: () => '', setTimeout, clearTimeout }, 'LspClient')
  const client = new LspClient({ projectId: 'p', tree: 'task:one', lang: 'python' })
  client.connect()
  assert.equal(new URL(client.ws.url).searchParams.get('tree'), 'task:one')
  const model = { getLanguageId: () => 'python', getValue: () => 'draft', uri: { toString: () => 'file:///E:/project/src/a.py' } }
  client.openDocument({ uri: 'file:///E:/project/src/a.py', text: 'draft', model })
  client._handle({ method: 'code-agent/ready', params: { capabilities: { completionProvider: {}, hoverProvider: true, definitionProvider: true, referencesProvider: true } } })
  client.registerProviders()
  assert.deepEqual(Object.keys(providers), ['completion', 'hover', 'definition', 'references'])
  client.request = async () => [{
    targetUri: 'file:///E:/project/src/b.py',
    targetRange: { start: { line: 1, character: 0 }, end: { line: 100, character: 0 } },
    targetSelectionRange: { start: { line: 14, character: 6 }, end: { line: 14, character: 10 } },
  }]
  const result = await providers.definition.provideDefinition(model, { lineNumber: 2, column: 3 })
  assert.equal(result[0].uri.toString(), 'file:///E:/project/src/b.py')
  assert.deepEqual(clone(result[0].range), { startLineNumber: 15, startColumn: 7, endLineNumber: 15, endColumn: 11 })
  client.dispose()
  assert.equal(registrations.every(item => item.disposed), true)
})

test('background drafts update cached models and both Python language services without changing the focused editor', async t => {
  const a = initialFile(), b = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py', text: 'b disk' })
  const { subject: s, props, clients, fake, timers } = await setup(t, {
    documents: [{ key: 'p/main/src/a.py', file: a, text: a.text }, { key: 'p/main/src/b.py', file: b, text: b.text }],
    languageServers: [{ id: 'pyright', languages: ['python'], state: 'installed' }, { id: 'ruff', languages: ['python'], state: 'installed' }],
  }, { capabilities: { pyright: { renameProvider: true }, ruff: { documentFormattingProvider: true } } })
  assert.equal(clients.length, 2)
  props.documents[1].text = 'accepted cross-file draft'
  await settle()
  assert.equal(fake.models.get(b.uri).getValue(), 'accepted cross-file draft')
  assert.equal(s.snapshot().path, a.path)
  for (const { fn } of [...timers.values()]) fn()
  for (const client of clients) assert.equal(client.calls.some(([name, uri, text]) => name === 'change' && uri === b.uri && text === 'accepted cross-file draft'), true)
  assert.equal(s.languageCapabilities.value.renameProvider, true)
  assert.equal(s.languageCapabilities.value.documentFormattingProvider, true)
})

test('a plain file tab retains language services for background code tabs', async t => {
  const a = initialFile()
  const { props, clients, subject: s } = await setup(t, { lspServer: { available: true }, documents: [{ key: 'p/main/src/a.py', file: a, text: a.text }] })
  const client = clients[0]
  props.file = initialFile({ path: 'README.md', uri: 'file:///E:/project/README.md', lang: 'plaintext', text: 'notes' })
  props.documentKey = 'p/main/README.md'
  props.lspServer = { available: false }
  await settle()
  assert.equal(client.disposed, undefined)
  assert.equal(client.documents.has(a.uri), true)
  assert.equal(s.lspState.value, 'idle')
  props.documents[0].text = 'background changed'
  await settle()
  s.notifySaved({ key: 'p/main/src/a.py', text: 'background changed' })
  assert.equal(client.calls.some(([name, uri, text]) => name === 'save' && uri === a.uri && text === 'background changed'), true)
})

test('server catalogs respect explicit disabled flags and dispose only the disabled companion', async t => {
  const { subject: s, props, clients } = await setup(t, { languageServers: [
    { id: 'pyright', languages: ['python'], state: 'installed' },
    { id: 'ruff', languages: ['python'], state: 'installed', enabled: false },
  ] })
  assert.deepEqual(clone(s.languageServerIds('python')), ['pyright'])
  assert.equal(clients.length, 1)
  props.languageServers[1].enabled = true
  await settle()
  assert.equal(clients.length, 2)
  props.languageServers[1].state = 'disabled'
  await settle()
  assert.equal(clients[1].disposed, true)
  assert.equal(clients[0].disposed, undefined)
})

test('refreshing code analysis enables newly installed servers and respects later catalog disable', async t => {
  const { subject: s, props, clients } = await setup(t, {
    languageServers: [{ id: 'pyright', name: 'Pyright', languages: ['python'], state: 'missing' }],
  }, { api: { code: { projectLsp: async () => ({ servers: [{ id: 'pyright', name: 'Pyright', languages: ['python'], state: 'installed' }] }) } } })
  assert.equal(clients.length, 0)
  await s.toggleLspPanel(true)
  assert.equal(clients.length, 1)
  assert.equal(s.lspState.value, 'ready')
  clients[0].options.onStatus('error', 'startup failed')
  await s.toggleLspPanel(true)
  assert.equal(clients.length, 1, 'retry preserves the document connection object')
  assert.equal(s.lspState.value, 'ready')
  props.languageServers[0].state = 'disabled'
  await settle()
  assert.equal(clients[0].disposed, true)
})

test('formatting changes the model between two undo boundaries and save notifications wait for disk success', async t => {
  const { subject: s, fake, clients, events } = await setup(t, { lspServer: { available: true } }, {
    capabilities: { documentFormattingProvider: true }, format: [{ range: { start: { line: 0, character: 0 }, end: { line: 0, character: 4 } }, newText: 'formatted' }],
  })
  assert.equal(await s.formatDocument(), true)
  assert.equal(s.snapshot().text, 'formatted')
  assert.equal(s.dirty.value, true)
  assert.deepEqual(fake.editors[0].calls.filter(([name]) => name === 'undo-stop' || name === 'edit').map(([name]) => name), ['undo-stop', 'edit', 'undo-stop'])
  s.save()
  assert.equal(events.some(([name]) => name === 'save'), true)
  assert.equal(clients[0].calls.some(([name]) => name === 'save'), false)
  s.notifySaved({ key: 'p/main/src/a.py', text: 'formatted' })
  assert.equal(clients[0].calls.some(([name, uri, text]) => name === 'save' && text === 'formatted'), true)
})

test('late formatting cannot overwrite later typing or a document changed to readonly', async t => {
  let resolve
  const pending = () => new Promise(done => { resolve = done })
  const { subject: s, props, fake } = await setup(t, { lspServer: { available: true } }, { capabilities: { documentFormattingProvider: true }, format: pending })
  const edits = [{ range: { start: { line: 0, character: 0 }, end: { line: 0, character: 4 } }, newText: 'stale format' }]
  const first = s.formatDocument()
  fake.editors[0].model.setValue('later typing')
  resolve(edits)
  assert.equal(await first, false)
  assert.equal(s.snapshot().text, 'later typing')
  const second = s.formatDocument()
  props.readonly = true
  resolve(edits)
  assert.equal(await second, false)
  assert.equal(s.snapshot().text, 'later typing')
  assert.equal(s.languageEditorAction('editor.action.rename', 'renameProvider'), false)
})

test('full documentation reads the current unsaved document in readonly mode without moving its view', async t => {
  const sections = [{ kind: 'code', value: 'class SessionService', language: 'java' }, { kind: 'markdown', value: '## 作用\n完整说明' }]
  const { subject: s, fake, clients, exposed, events } = await setup(t, {
    readonly: true, file: initialFile({ text: 'unsaved draft', dirty: true }), lspServer: { available: true },
  }, { capabilities: { hoverProvider: true }, hover: { sections } })
  const editor = fake.editors[0]
  editor.setSelection(new fake.monaco.Range(12, 7, 12, 7))
  editor.scrollTop = 480
  const before = clone(s.snapshot()), changeCount = events.filter(([name]) => name === 'change').length
  assert.equal(typeof exposed.showDocumentation, 'function')
  assert.equal(await s.showDocumentation(), true)
  assert.deepEqual(clone(s.symbolDocumentation.value), {
    title: 'a.py', path: 'src/a.py', line: 12, sections, loading: false, error: '',
  })
  const call = clients[0].calls.find(([name]) => name === 'hover')
  assert.equal(call[1], 'file:///E:/project/src/a.py')
  assert.deepEqual(clone(call[2]), { lineNumber: 12, column: 7 })
  assert.deepEqual(clone(s.snapshot()), before)
  assert.equal(editor.scrollTop, 480)
  assert.equal(events.filter(([name]) => name === 'change').length, changeCount)
  s.closeDocumentation()
  assert.equal(s.symbolDocumentation.value, null)
  assert.equal(editor.focusCount, 1)
  assert.deepEqual(clone(s.snapshot()), before)
})

test('background LSP documentation updates refresh the pinned full view without stealing focus or opening native hover', async t => {
  let text = 'Original documentation.'
  const { subject: s, fake, clients, dispose } = await setup(t, { lspServer: { available: true } }, {
    capabilities: { hoverProvider: true }, hover: () => ({ sections: [{ kind: 'plaintext', value: text }] }),
  })
  const editor = fake.editors[0]
  editor.setSelection(new fake.monaco.Range(1, 2, 1, 2))
  await s.showDocumentation()
  editor.setSelection(new fake.monaco.Range(1, 4, 1, 4)); editor.scrollTop = 360
  const before = clone(s.snapshot()), focus = editor.focusCount || 0
  const change = { projectId: 'p', tree: 'main', uri: 'file:///E:/project/src/a.py', version: 1 }
  for (const other of [{ ...change, projectId: 'other' }, { ...change, tree: 'task:other' }, { ...change, uri: 'file:///other.py' }, { ...change, version: 99 },
    { ...change, position: { line: 0, character: 20 } }]) {
    notifyHoverDocumentationUpdated(other)
  }
  await settle()
  assert.equal(clients[0].calls.filter(([name]) => name === 'hover').length, 1)
  text = '完整中文说明已准备就绪。'
  notifyHoverDocumentationUpdated(change)
  await settle()
  assert.equal(s.symbolDocumentation.value.sections[0].value, text)
  const latest = clients[0].calls.filter(([name]) => name === 'hover').at(-1)
  assert.deepEqual(clone(latest[2]), { lineNumber: 1, column: 2 })
  assert.deepEqual(clone(s.snapshot()), before)
  assert.equal(editor.focusCount || 0, focus)
  assert.equal(editor.scrollTop, 360)
  assert.ok(!editor.calls.some(call => call[0] === 'trigger' && call[2] === 'editor.action.showHover'))
  s.closeDocumentation(false)
  notifyHoverDocumentationUpdated(change)
  await settle()
  assert.equal(s.symbolDocumentation.value, null)
  assert.equal(clients[0].calls.filter(([name]) => name === 'hover').length, 2)
  dispose()
  notifyHoverDocumentationUpdated(change)
  await settle()
  assert.equal(clients[0].calls.filter(([name]) => name === 'hover').length, 2)
})

test('frequent background updates during a full-documentation read keep it running and coalesce one later refresh', async t => {
  const requests = [], tokens = []
  const { subject: s, clients } = await setup(t, { lspServer: { available: true } }, {
    capabilities: { hoverProvider: true }, hover: token => { tokens.push(token); return new Promise(resolve => requests.push(resolve)) },
  })
  const first = s.showDocumentation()
  for (let index = 0; index < 10; index++) {
    notifyHoverDocumentationUpdated({ projectId: 'p', tree: 'main', uri: 'file:///E:/project/src/a.py', version: 1 })
    assert.equal(tokens[0].isCancellationRequested, false)
  }
  assert.equal(clients[0].calls.filter(([name]) => name === 'hover').length, 1)
  requests[0]({ sections: [{ kind: 'plaintext', value: 'Old documentation.' }] })
  await first
  await settle()
  assert.equal(clients[0].calls.filter(([name]) => name === 'hover').length, 2)
  requests[1]({ sections: [{ kind: 'plaintext', value: '新的中文说明。' }] })
  await settle()
  assert.equal(s.symbolDocumentation.value.sections[0].value, '新的中文说明。')
})

test('Java complete explanation works without an installed language service and preserves the draft', async t => {
  const { subject: s, fake, clients, events } = await setup(t, {
    file: initialFile({ path: 'Example.java', lang: 'java', text: 'public class Example {}', uri: 'file:///E:/project/Example.java' }),
    documentKey: 'p/main/Example.java', readonly: true, lspServer: { available: false },
  })
  fake.editors[0].setSelection(new fake.monaco.Range(1, 3, 1, 3))
  const before = clone(s.snapshot())
  assert.equal(await s.showDocumentation(), true)
  assert.match(s.symbolDocumentation.value.sections[0].value, /修饰符/)
  assert.equal(s.symbolDocumentation.value.source, 'Java 代码说明')
  assert.equal(clients.length, 0)
  assert.deepEqual(clone(s.snapshot()), before)
  assert.equal(events.filter(([name]) => name === 'save').length, 0)
})

test('Java complete explanation reads the shared project record offline without changing or saving code', async t => {
  const text = 'class Example { Long categoryId; }', calls = []
  const file = initialFile({ path: 'Example.java', lang: 'java', text, diskText: text, uri: 'file:///E:/project/Example.java' })
  const { subject: s, fake, events } = await setup(t, { file, documentKey: 'p/main/Example.java', readonly: true, lspServer: { available: false } }, {
    api: { code: { resolveSymbolBusiness: async (pid, payload) => { calls.push({ pid, payload }); return {
      entity: { id: 'sym:Example.java#Example.categoryId', name: 'categoryId' },
      business: { business_meaning: '文档分类筛选标识', status: 'current', source: 'human' },
    } } } },
  })
  const column = text.indexOf('categoryId') + 1
  fake.editors[0].setSelection(new fake.monaco.Range(1, column, 1, column))
  const before = clone(s.snapshot())
  assert.equal(await s.showDocumentation(), true)
  assert.match(s.symbolDocumentation.value.sections.map(item => item.value).join('\n'), /功能描述.*文档分类筛选标识/)
  assert.equal(calls[0].payload.column, column)
  assert.equal(calls[0].payload.text, text)
  assert.deepEqual(clone(s.snapshot()), before)
  assert.equal(events.filter(([name]) => name === 'save' || name === 'change').length, 0)
})

test('automatic function updates refresh an open Java explanation at its pinned word without touching the draft or selection', async t => {
  const text = 'class Example { Long categoryId; }', calls = []
  let business = { status: 'missing', source: '', business_meaning: '' }
  const file = initialFile({ path: 'Example.java', lang: 'java', text, diskText: 'class Example {}', dirty: true, uri: 'file:///E:/project/Example.java' })
  const { subject: s, fake, events, clients } = await setup(t, { file, documentKey: 'p/main/Example.java', lspServer: { available: true } }, {
    capabilities: { hoverProvider: true }, hover: { sections: [{ kind: 'plaintext', value: '原始语言服务说明' }] },
    api: { code: { resolveSymbolBusiness: async (pid, payload) => { calls.push({ pid, payload }); return {
      entity: { id: 'sym:Example.java#Example.categoryId', name: 'categoryId' }, business: { ...business },
    } } } },
  })
  const editor = fake.editors[0], column = text.indexOf('categoryId') + 1
  editor.setSelection(new fake.monaco.Range(1, column, 1, column)); editor.scrollTop = 320
  assert.equal(await s.showDocumentation(), true)
  assert.match(s.symbolDocumentation.value.sections[0].value, /功能描述.*待自动补充/)
  const title = s.symbolDocumentation.value.title, line = s.symbolDocumentation.value.line
  editor.setSelection(new fake.monaco.Range(1, 2, 1, 2))
  const before = clone(s.snapshot()), changeCount = events.filter(([name]) => name === 'change' || name === 'save').length
  const focusCount = editor.focusCount || 0
  business = { status: 'unreviewed', source: 'model', business_meaning: '用于按文档分类筛选结果' }
  notifyBusinessMeaningChanged({ projectId: 'p', tree: 'main', automatic: true })
  await settle()
  assert.ok(s.symbolDocumentation.value, 'the explanation remains open')
  assert.equal(s.symbolDocumentation.value.title, title)
  assert.equal(s.symbolDocumentation.value.line, line)
  assert.equal(s.symbolDocumentation.value.sections.at(-1).value, '原始语言服务说明')
  assert.match(s.symbolDocumentation.value.sections[0].value, /功能描述（待确认）.*用于按文档分类筛选结果/)
  assert.equal((s.symbolDocumentation.value.sections.map(item => item.value).join('\n').match(/功能描述/g) || []).length, 1)
  assert.equal(calls.length, 2)
  assert.equal(calls[1].payload.column, column, 'refresh uses the pinned word, not the current cursor')
  assert.equal(clients[0].calls.filter(([name]) => name === 'hover').length, 1, 'refresh reuses the language explanation')
  assert.deepEqual(clone(s.snapshot()), before)
  assert.equal(s.dirty.value, true)
  assert.equal(editor.scrollTop, 320)
  assert.equal(editor.focusCount || 0, focusCount)
  assert.equal(events.filter(([name]) => name === 'change' || name === 'save').length, changeCount)
})

test('a pinned explanation shows failed function refreshes and recovers while retaining its language docs', async t => {
  const text = 'class Example { Long categoryId; }'
  let failed = false
  const file = initialFile({ path: 'Example.java', lang: 'java', text, diskText: text, uri: 'file:///E:/project/Example.java' })
  const { subject: s, fake } = await setup(t, { file, documentKey: 'p/main/Example.java', lspServer: { available: true } }, {
    capabilities: { hoverProvider: true }, hover: { sections: [{ kind: 'plaintext', value: '原始语言服务说明' }] },
    api: { code: { resolveSymbolBusiness: async () => {
      if (failed) throw new Error('NotFound')
      return { entity: { id: 'sym:Example.java#Example.categoryId' },
        business: { status: 'current', source: 'human', business_meaning: '当前分类功能' } }
    } } },
  })
  const column = text.indexOf('categoryId') + 1
  fake.editors[0].setSelection(new fake.monaco.Range(1, column, 1, column))
  await s.showDocumentation()
  failed = true
  notifyBusinessMeaningChanged({ projectId: 'p', tree: 'main', automatic: true })
  await settle()
  assert.match(s.symbolDocumentation.value.sections[0].value, /功能描述.*接口不可用.*重启后端/)
  assert.doesNotMatch(s.symbolDocumentation.value.sections[0].value, /当前分类功能/)
  assert.equal(s.symbolDocumentation.value.sections.at(-1).value, '原始语言服务说明')
  failed = false
  notifyBusinessMeaningChanged({ projectId: 'p', tree: 'main', automatic: true })
  await settle()
  assert.match(s.symbolDocumentation.value.sections[0].value, /功能描述.*当前分类功能/)
  assert.doesNotMatch(s.symbolDocumentation.value.sections[0].value, /接口不可用/)
})

test('open Java explanations ignore automatic changes from another project/tree or entity', async t => {
  const text = 'class Example { Long categoryId; }', calls = []
  const file = initialFile({ path: 'Example.java', lang: 'java', text, diskText: text, uri: 'file:///E:/project/Example.java' })
  const { subject: s, fake } = await setup(t, { file, documentKey: 'p/main/Example.java' }, {
    api: { code: { resolveSymbolBusiness: async (_, payload) => { calls.push(payload); return {
      entity: { id: 'sym:Example.java#Example.categoryId', name: 'categoryId' },
      business: { status: 'missing', business_meaning: '' },
    } } } },
  })
  fake.editors[0].setSelection(new fake.monaco.Range(1, text.indexOf('categoryId') + 1, 1, text.indexOf('categoryId') + 1))
  await s.showDocumentation()
  const before = clone(s.symbolDocumentation.value)
  for (const detail of [{ projectId: 'other', tree: 'main' }, { projectId: 'p', tree: 'work' }, { projectId: 'p', tree: 'main', id: 'sym:Example.java#other' }]) {
    notifyBusinessMeaningChanged(detail); await settle()
  }
  assert.equal(calls.length, 1)
  assert.deepEqual(clone(s.symbolDocumentation.value), before)
})

test('an optional automatic refresh failure replaces the function with an honest status and preserves Java details', async t => {
  let reads = 0
  const text = 'class Example { Long categoryId; }'
  const file = initialFile({ path: 'Example.java', lang: 'java', text, diskText: text, uri: 'file:///E:/project/Example.java' })
  const { subject: s, fake } = await setup(t, { file, documentKey: 'p/main/Example.java' }, {
    api: { code: { resolveSymbolBusiness: async () => {
      if (++reads > 1) throw new Error('临时读取失败')
      return { entity: { id: 'sym:Example.java#Example.categoryId' }, business: { status: 'current', source: 'human', business_meaning: '已确认的功能描述' } }
    } } },
  })
  fake.editors[0].setSelection(new fake.monaco.Range(1, text.indexOf('categoryId') + 1, 1, text.indexOf('categoryId') + 1))
  await s.showDocumentation()
  const before = clone(s.symbolDocumentation.value)
  notifyBusinessMeaningChanged({ projectId: 'p', tree: 'main', automatic: true }); await settle()
  assert.equal(reads, 2)
  const updated = clone(s.symbolDocumentation.value)
  assert.deepEqual({ ...updated, sections: [] }, { ...before, sections: [] })
  assert.match(updated.sections[0].value, /功能描述.*暂时无法读取.*重试/)
  assert.match(updated.sections[0].value, /作用.*保存类或实例的状态/)
  assert.doesNotMatch(updated.sections[0].value, /已确认的功能描述/)
})

test('late automatic explanation refresh cannot reopen a panel after a file change or unmount', async t => {
  const text = 'class Example { Long categoryId; }'
  for (const action of ['switch', 'unmount']) {
    let resolve, reads = 0
    const file = initialFile({ path: 'Example.java', lang: 'java', text, diskText: text, uri: 'file:///E:/project/Example.java' })
    const fixture = await setup(t, { file, documentKey: 'p/main/Example.java' }, {
      api: { code: { resolveSymbolBusiness: async () => ++reads === 1 ? {
        entity: { id: 'sym:Example.java#Example.categoryId' }, business: { status: 'missing', business_meaning: '' },
      } : new Promise(done => { resolve = done }) } },
    })
    fixture.fake.editors[0].setSelection(new fixture.fake.monaco.Range(1, text.indexOf('categoryId') + 1, 1, text.indexOf('categoryId') + 1))
    await fixture.subject.showDocumentation()
    notifyBusinessMeaningChanged({ projectId: 'p', tree: 'main', automatic: true }); await settle()
    assert.equal(reads, 2)
    if (action === 'switch') {
      fixture.props.file = initialFile({ path: 'Second.java', lang: 'java', uri: 'file:///E:/project/Second.java', text: 'class Second {}' })
      fixture.props.documentKey = 'p/main/Second.java'; await settle()
    } else fixture.dispose()
    resolve({ entity: { id: 'sym:Example.java#Example.categoryId' }, business: { status: 'unreviewed', source: 'model', business_meaning: '迟到的自动说明' } })
    await settle()
    assert.equal(fixture.subject.symbolDocumentation.value, null)
    fixture.dispose()
  }
})

test('closing a pending documentation request cancels it and a late result cannot reopen the panel', async t => {
  let resolve, token, cancelled = 0
  const { subject: s } = await setup(t, { lspServer: { available: true } }, {
    capabilities: { hoverProvider: true }, hover: value => {
      token = value
      value.onCancellationRequested(() => { cancelled++ })
      return new Promise(done => { resolve = done })
    },
  })
  const pending = s.showDocumentation()
  assert.equal(s.symbolDocumentation.value.loading, true)
  s.closeDocumentation()
  assert.equal(token.isCancellationRequested, true)
  assert.equal(cancelled, 1)
  resolve({ sections: [{ kind: 'plaintext', value: 'late response' }] })
  assert.equal(await pending, false)
  assert.equal(s.symbolDocumentation.value, null)
})

test('editing or switching files invalidates pending documentation without mixing old and new symbols', async t => {
  const requests = []
  const { subject: s, props, fake } = await setup(t, { lspServer: { available: true } }, {
    capabilities: { hoverProvider: true }, hover: token => new Promise(resolve => { requests.push({ token, resolve }) }),
  })
  const first = s.showDocumentation()
  fake.editors[0].model.setValue('new draft')
  assert.equal(s.symbolDocumentation.value, null)
  assert.equal(requests[0].token.isCancellationRequested, true)
  requests[0].resolve({ sections: [{ kind: 'plaintext', value: 'old draft docs' }] })
  assert.equal(await first, false)
  const second = s.showDocumentation()
  props.file = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py', text: 'other draft' })
  props.documentKey = 'p/main/src/b.py'
  await settle()
  assert.equal(s.symbolDocumentation.value, null)
  assert.equal(requests[1].token.isCancellationRequested, true)
  requests[1].resolve({ sections: [{ kind: 'plaintext', value: 'old file docs' }] })
  assert.equal(await second, false)
  assert.equal(s.symbolDocumentation.value, null)
  assert.equal(s.snapshot().text, 'other draft')
})

test('language tools focus the editor and trigger registered commands absent from getAction', async t => {
  const { subject: s, props, fake } = await setup(t, { lspServer: { available: true } }, {
    capabilities: { codeActionProvider: true, renameProvider: true }, missingActions: ['editor.action.quickFix'],
  })
  const editor = fake.editors[0]
  assert.equal(s.languageEditorAction('editor.action.quickFix', 'codeActionProvider'), true)
  assert.deepEqual(clone(editor.calls.at(-1)), ['trigger', 'code-agent.language', 'editor.action.quickFix', {}])
  assert.equal(editor.focusCount, 1)
  assert.equal(s.languageEditorAction('editor.action.rename', 'renameProvider'), true)
  assert.deepEqual(clone(editor.calls.at(-1)), ['action', 'editor.action.rename'])
  props.readonly = true
  assert.equal(s.languageEditorAction('editor.action.quickFix', 'codeActionProvider'), false)
  assert.equal(editor.focusCount, 2, 'readonly guard executes before focus or commands')
})

test('document and workspace symbols navigate within the active workspace and ignore outside targets', async t => {
  const { subject: s, events, clients } = await setup(t, { lspServer: { available: true } }, {
    capabilities: { documentSymbolProvider: true, workspaceSymbolProvider: true },
    symbols: [{ name: 'A', selectionRange: { start: { line: 2, character: 3 } }, children: [{ name: 'method', selectionRange: { start: { line: 4, character: 2 } } }] }],
    workspaceSymbols: [{ name: 'Other', location: { uri: 'file:///E:/project/src/b.py', range: { start: { line: 7, character: 1 } } } },
      { name: 'Outside', location: { uri: 'file:///E:/elsewhere/out.py', range: { start: { line: 0, character: 0 } } } }],
  })
  s.toggleSymbolPanel()
  await settle()
  assert.deepEqual(clone(s.symbolItems.value).map(item => [item.name, item.line, item.column, item.depth]), [['A', 3, 4, 0], ['method', 5, 3, 1]])
  s.setSymbolMode('workspace')
  await settle()
  assert.equal(s.symbolItems.value.length, 1)
  s.openLanguageSymbol(s.symbolItems.value[0])
  assert.deepEqual(events.filter(([name]) => name === 'language-symbol').at(-1)[1], { name: 'Other', path: 'src/b.py', line: 8, column: 2, depth: 0, projectId: 'p', tree: 'main' })
  assert.equal(clients[0].calls.some(([name]) => name === 'workspace-symbols'), true)
})

test('workspace edit hooks route preview payloads and dynamically deny edits after readonly changes', async t => {
  const background = initialFile({ path: 'src/b.py', uri: 'file:///E:/project/src/b.py', editable: false })
  const { props, fake, clients, events } = await setup(t, { lspServer: { available: true },
    documents: [{ key: 'p/main/src/b.py', file: background, text: 'background' }] })
  const options = clients[0].options, model = fake.editors[0].model
  assert.equal(options.canEdit(model), true)
  const backgroundModel = fake.models.get(background.uri)
  backgroundModel.uri = { toString: () => 'file:///E%3A/project/src/b.py' }
  assert.equal(options.canEdit(backgroundModel), false, 'background permissions use document ownership rather than URI spelling')
  props.documents[0].file.editable = true
  assert.equal(options.canEdit(backgroundModel), true)
  options.onWorkspaceEdit({ edit: { changes: {} }, title: 'Rename', uri: model.uri.toString(), versions: { [model.uri.toString()]: 1 } })
  assert.equal(events.filter(([name]) => name === 'language-edit').length, 1)
  props.readonly = true
  assert.equal(options.canEdit(model), false)
})

test('worktree switches dispose the prior scope and its documents before creating a new connection', async t => {
  const { props, clients } = await setup(t, { lspServer: { available: true } })
  const first = clients[0]
  props.tree = 'task:one'
  props.documentKey = 'p/task:one/src/a.py'
  props.file = initialFile({ uri: 'file:///E:/worktree/src/a.py' })
  await settle()
  assert.equal(first.disposed, true)
  assert.equal(first.documents.size, 0)
  assert.equal(clients.length, 2)
  assert.equal(clients[1].options.tree, 'task:one')
})

test('advanced navigation is available in readonly mode, filters foreign references and closes when edited', async t => {
  const { subject: s, fake, clients, events } = await setup(t, { lspServer: { available: true }, readonly: true }, { capabilities: { callHierarchyProvider: true, typeHierarchyProvider: true } })
  assert.equal(s.showHierarchy('call'), true)
  assert.equal(s.hierarchy.value.client, clients[0])
  assert.equal(s.hierarchy.value.kind, 'call')
  assert.equal(s.navigateHierarchy({ uri: 'file:///E:/outside/a.py', range: { start: { line: 2, character: 3 } } }), false)
  assert.equal(s.showLensReferences({ projectId: 'foreign', tree: 'main', locations: [] }), false)
  assert.equal(s.showLensReferences({ projectId: 'p', tree: 'main', title: '2 references', locations: [
    { uri: 'file:///E:/project/src/b.py', range: { startLineNumber: 5, startColumn: 3 } },
    { uri: 'file:///E:/outside/b.py', range: { startLineNumber: 2, startColumn: 1 } },
  ] }), true)
  assert.equal(s.lensReferences.value.items.length, 1)
  assert.equal(s.lensReferences.value.items[0].path, 'src/b.py')
  assert.equal(s.hierarchy.value, null)
  assert.equal(s.navigateHierarchy({ uri: 'file:///E:/project/src/b.py', range: { start: { line: 4, character: 2 } } }), true)
  assert.equal(events.at(-1)[1].line, 5)
  fake.editors[0].getModel().setValue('new draft')
  assert.equal(s.hierarchy.value, null); assert.equal(s.lensReferences.value, null)
})

test('resource notifications encode project paths and invert created/deleted states on rollback', async t => {
  const { subject: s, clients } = await setup(t, { lspServer: { available: true } })
  const batch = { files: [
    { path: 'src/新 #.py', before_exists: false, after_exists: true },
    { path: 'src/old.py', before_exists: true, after_exists: false },
    { path: 'src/a.py', before_exists: true, after_exists: true },
    { path: '../outside.py', before_exists: false, after_exists: true },
  ] }
  assert.equal(s.notifyResourceChanges(batch, false, 'E:\\项目 空间'), true)
  let changes = clients[0].calls.at(-1)[2].changes
  assert.deepEqual(clone(changes.map(row => row.type)), [1, 3, 2])
  assert.equal(decodeURIComponent(new URL(changes[0].uri).pathname), '/E:/项目 空间/src/新 #.py')
  s.notifyResourceChanges(batch, true, 'E:\\项目 空间')
  changes = clients[0].calls.at(-1)[2].changes
  assert.deepEqual(clone(changes.map(row => row.type)), [3, 1, 2])
})
