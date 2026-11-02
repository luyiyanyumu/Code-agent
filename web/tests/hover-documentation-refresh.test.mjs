import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createHoverDocumentationRefresh } from '../src/lib/hoverDocumentationRefresh.js'

function fixture() {
  const events = new Map(), calls = [], listeners = new Set()
  const event = callback => { listeners.add(callback); return { dispose: () => listeners.delete(callback) } }
  let version = 7
  const model = { getVersionId: () => version, isDisposed: () => false, getWordAtPosition: () => ({ word: 'readSession', startColumn: 5, endColumn: 16 }) }
  const anchor = { type: 1, range: { startLineNumber: 3, startColumn: 9, endLineNumber: 3, endColumn: 10 }, initialMousePosX: 160, initialMousePosY: 120 }
  const node = { isConnected: true, getClientRects: () => [{}], ownerDocument: { defaultView: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) } } }
  const wrapper = { isVisible: true, isFocused: false, getDomNode: () => node,
    _currentResult: { options: { anchor, source: 0, shouldFocus: false } }, hide() { calls.push('hide'); this._currentResult = null },
    _startShowingOrUpdateHover(...args) { calls.push(args) } }
  const controller = { isHoverVisible: true, _contentWidget: wrapper, _onHoverContentsChanged: { event } }
  const editor = { getContribution: id => { assert.equal(id, 'editor.contrib.contentHover'); return controller }, getModel: () => model, getDomNode: () => node }
  for (const name of ['onDidChangeModel', 'onDidChangeModelContent', 'onDidDispose']) editor[name] = callback => { events.set(name, callback); return { dispose: () => events.delete(name) } }
  const context = { projectId: 'fixture', tree: 'main', uri: 'file:///fixture.java', version: 2 }
  const refresh = createHoverDocumentationRefresh({ editor, contextForModel: () => context })
  const notify = overrides => refresh.refresh({ ...context, position: { line: 2, character: 4 }, status: 'ready', ...overrides })
  return { calls, model, anchor, node, wrapper, controller, editor, context, refresh, notify, events, listeners, content: () => [...listeners].forEach(callback => callback()), change: () => { version++; events.get('onDidChangeModelContent')?.() } }
}

test('refreshes only visible original mouse anchor immediately, without caret, focus or edits', () => {
  const f = fixture()
  assert.equal(f.refresh.supported, true)
  assert.equal(f.notify(), true)
  assert.deepEqual(f.calls, ['hide', [f.anchor, 1, 0, false, null]])
  assert.equal(f.notify(), false, 'a cleared old result cannot trigger another query')
})

test('stale identity, URI, project, tree, version and other-symbol notifications never query', () => {
  for (const overrides of [{ status: 'pending' }, { status: 'running' }, { status: 'prewarm' }, { uri: 'file:///other.java' }, { projectId: 'other' }, { tree: 'archive' }, { version: 3 }, { version: undefined }, { position: { line: 0, character: 4 } }, { position: { line: 2, character: 20 } }, { position: { line: -1, character: 4 } }]) {
    const f = fixture(); assert.equal(f.notify(overrides), false); assert.deepEqual(f.calls, [])
  }
  const f = fixture(); f.change(); f.content(); assert.equal(f.notify(), false); assert.deepEqual(f.calls, [])
})

test('terminal failed and legacy status-less notifications may refresh matching anchors', () => {
  for (const status of ['failed', undefined]) { const f = fixture(); assert.equal(f.notify({ status }), true) }
})

test('hidden, detached, focused, keyboard-focused and removed widgets fail closed', () => {
  for (const mutate of [f => f.wrapper.isVisible = false, f => f.controller.isHoverVisible = false,
    f => f.node.isConnected = false, f => f.node.getClientRects = () => [], f => f.wrapper.isFocused = true,
    f => f.wrapper._currentResult.options.shouldFocus = true, f => f.controller._contentWidget = null,
    f => f.wrapper._currentResult = { options: { anchor: f.anchor } }, f => f.editor.getModel = () => ({}),
    f => f.editor.getDomNode = () => ({ isConnected: false })]) {
    const f = fixture(); mutate(f); assert.equal(f.notify(), false); assert.deepEqual(f.calls, [])
  }
})

test('new rendered results capture their own version; disposal removes subscriptions', () => {
  const f = fixture(); f.change(); f.context.version++
  f.wrapper._currentResult = { options: { anchor: f.anchor, source: 0, shouldFocus: false } }; f.content()
  assert.equal(f.notify(), true)
  f.refresh.dispose(); f.refresh.dispose(); assert.equal(f.listeners.size, 0); assert.equal(f.events.size, 0)
  assert.equal(f.notify(), false)
})

test('missing Monaco private boundary is unsupported and never starts queries', () => {
  for (const editor of [undefined, { getContribution: () => null }, { getContribution: () => { throw new Error('disposed') } }]) {
    const adapter = createHoverDocumentationRefresh({ editor, contextForModel: () => ({}) })
    assert.equal(adapter.supported, false); assert.equal(adapter.refresh({}), false); adapter.dispose()
  }
  const f = fixture(); f.wrapper._startShowingOrUpdateHover = undefined
  assert.equal(f.notify(), false); assert.deepEqual(f.calls, [])
})

test('an early terminal update refreshes its own visible pending receipt once, never a later operation', async () => {
  const f = fixture(), original = f.wrapper._currentResult
  f.wrapper._currentResult = null; f.wrapper.isVisible = false; f.controller.isHoverVisible = false
  f.wrapper._hoverOperation = { options: original.options }
  assert.equal(f.notify(), false); assert.deepEqual(f.calls, [])
  original.hoverParts = [{ contents: [{ value: '正在翻译中文说明，完成后自动更新' }] }]
  f.wrapper._currentResult = original; f.wrapper.isVisible = true; f.controller.isHoverVisible = true
  f.content(); await Promise.resolve()
  assert.deepEqual(f.calls, ['hide', [f.anchor, 1, 0, false, null]])
  f.wrapper._currentResult = { options: original.options, hoverParts: [{ contents: [{ value: '中文说明完成' }] }] }
  f.content(); await Promise.resolve(); assert.equal(f.calls.length, 2, 'cached ready render cannot loop')
  const g = fixture(), other = g.wrapper._currentResult
  g.wrapper._currentResult = null;g.wrapper._hoverOperation = { options: other.options };g.notify()
  g.wrapper._currentResult = { options: { ...other.options }, hoverParts: original.hoverParts };g.content();await Promise.resolve()
  assert.deepEqual(g.calls, [], 'unrelated operation cannot consume a terminal update')
})

test('installed Monaco 0.56 still exposes the guarded native hover boundary', () => {
  const base = new URL('../node_modules/monaco-editor/esm/vs/editor/contrib/hover/browser/', import.meta.url)
  const controller = fs.readFileSync(new URL('contentHoverController.js', base), 'utf8')
  const wrapper = fs.readFileSync(new URL('contentHoverWidgetWrapper.js', base), 'utf8')
  assert.match(controller, /this\.ID = 'editor\.contrib\.contentHover'/)
  assert.match(controller, /this\._onHoverContentsChanged =/)
  assert.match(wrapper, /_startShowingOrUpdateHover\(anchor, mode, source, focus, mouseEvent\)/)
  assert.match(wrapper, /hide\(\)\s*\{\s*this\._hoverOperation\.cancel\(\);\s*this\._setCurrentResult\(null\)/)
})
