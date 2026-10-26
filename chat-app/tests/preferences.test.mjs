import test from 'node:test'
import assert from 'node:assert/strict'
import { shouldSendMessage } from '../src/lib/preferences.js'

test('send shortcuts respect platform modifier, multiline entry and IME confirmation', () => {
  const key = overrides => ({ key: 'Enter', ...overrides })
  assert.equal(shouldSendMessage(key(), 'enter'), true)
  assert.equal(shouldSendMessage(key(), 'ctrl-enter'), false)
  assert.equal(shouldSendMessage(key({ ctrlKey: true }), 'ctrl-enter'), true)
  assert.equal(shouldSendMessage(key({ metaKey: true }), 'ctrl-enter'), true)
  assert.equal(shouldSendMessage(key({ ctrlKey: true }), 'enter'), false)
  for (const extra of [{ isComposing: true }, { keyCode: 229 }, { shiftKey: true }, { altKey: true }, { repeat: true }]) {
    assert.equal(shouldSendMessage(key(extra), 'enter'), false)
    assert.equal(shouldSendMessage(key({ ctrlKey: true, ...extra }), 'ctrl-enter'), false)
  }
  assert.equal(shouldSendMessage(key(), 'enter', true), false)
})

test('first visit only copies compatible preferences and later changes do not touch coding settings', async t => {
  const oldStorage = globalThis.localStorage
  const oldValue = JSON.stringify({ theme: 'dark', sendKey: 'ctrl-enter', editorFontSize: 22, defaultView: 'code' })
  const values = new Map([['code-agent:preferences', oldValue]])
  globalThis.localStorage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
  t.after(() => { globalThis.localStorage = oldStorage })
  const module = await import('../src/lib/preferences.js?first-visit')
  const state = module.usePreferences()
  assert.deepEqual({ ...state.preferences }, { theme: 'dark', sendKey: 'ctrl-enter', reducedMotion: false })
  assert.equal(state.updatePreference('theme', 'light'), true)
  assert.equal(state.updatePreference('editorFontSize', 20), false)
  assert.equal(values.get('code-agent:preferences'), oldValue)
  assert.equal(JSON.parse(values.get('chat-agent:preferences')).theme, 'light')
  values.set('code-agent:preferences', JSON.stringify({ theme: 'dark', sendKey: 'enter' }))
  const reloaded = (await import('../src/lib/preferences.js?return-visit')).usePreferences()
  assert.equal(reloaded.preferences.theme, 'light')
  assert.equal(reloaded.preferences.sendKey, 'ctrl-enter')
})

test('blocked storage getter falls back without preventing the app from starting', async t => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('SecurityError') } })
  t.after(() => {
    if (original) Object.defineProperty(globalThis, 'localStorage', original)
    else delete globalThis.localStorage
  })
  const module = await import('../src/lib/preferences.js?blocked-storage')
  const state = module.usePreferences()
  const dispose = module.initializePreferences()
  assert.deepEqual({ ...state.preferences }, { theme: 'light', sendKey: 'enter', reducedMotion: false })
  assert.match(state.storageError.value, /无法读取或保存/)
  assert.equal(state.updatePreference('theme', 'dark'), false)
  dispose()
})
