import assert from 'node:assert/strict'
import test from 'node:test'
import { ideActions, ideActionState, ideKeybinding } from '../src/lib/ideActions.js'

const action = id => ideActions.find(item => item.id === id)
test('read-only Java still exposes navigation, parameter info and Chinese documentation', () => {
  const state = { language: 'java', readonly: true, capabilities: { definitionProvider: true, signatureHelpProvider: true, renameProvider: true, completionProvider: {} } }
  for (const id of ['documentation', 'definition', 'parameters']) assert.equal(ideActionState(action(id), state).disabled, false)
  for (const id of ['complete', 'rename']) assert.equal(ideActionState(action(id), state).reason, '当前文件只读')
})
test('built-in languages retain real capabilities without advertising unsupported Java actions', () => {
  assert.equal(ideActionState(action('complete'), { language: 'typescript' }).disabled, false)
  assert.equal(ideActionState(action('definition'), { language: 'java' }).disabled, true)
  assert.equal(ideActionState(action('symbols'), { language: 'python', capabilities: { workspaceSymbolProvider: true } }).disabled, false)
  assert.equal(ideActionState(action('implementation'), { language: 'javascript' }).disabled, true)
})
test('editor shortcuts leave Ctrl+P and Ctrl+B for the workspace', () => {
  const monaco = { KeyMod: { CtrlCmd: 2048, Alt: 512, Shift: 1024 }, KeyCode: { KeyB: 32, KeyP: 46, KeyQ: 48, F12: 70, Space: 10 } }
  assert.equal(ideKeybinding(action('definition'), monaco), 70)
  assert.equal(ideKeybinding(action('parameters'), monaco), 3082)
  for (const item of ideActions) assert.ok(![2080, 2094].includes(ideKeybinding(item, monaco)), `${item.id} masks a workspace shortcut`)
  assert.equal(ideKeybinding(action('implementation'), monaco), 2592)
  assert.equal(ideKeybinding(action('documentation'), monaco), 2096)
})
