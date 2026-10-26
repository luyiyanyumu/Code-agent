import assert from 'node:assert/strict'
import test from 'node:test'
// Initialize Vue before tests install the minimal document preference fixture.
import 'vue'

let serial = 0
async function setup(t, raw = null) {
  const descriptors = Object.fromEntries(['localStorage', 'window', 'document'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const values = new Map(raw == null ? [] : [['code-agent:preferences', raw]])
  const listeners = new Map(), mediaListeners = new Set(), attributes = new Map()
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
  const media = { matches: false, addEventListener: (_, fn) => mediaListeners.add(fn), removeEventListener: (_, fn) => mediaListeners.delete(fn) }
  const root = { setAttribute: (key, value) => attributes.set(key, value), style: {} }
  Object.assign(globalThis, { localStorage: storage, window: {
    addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name), matchMedia: () => media,
  }, document: { documentElement: root } })
  const module = await import(`../src/lib/preferences.js?test=${++serial}`)
  const dispose = module.initializePreferences()
  t.after(() => {
    dispose()
    for (const [key, descriptor] of Object.entries(descriptors)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  })
  return { ...module, ...module.usePreferences(), values, listeners, storage, media, mediaListeners, attributes, root, dispose }
}

test('preferences load validated values, persist updates and reset only requested fields', async t => {
  const s = await setup(t, JSON.stringify({ theme: 'dark', editorFontSize: 20, editorTabSize: 3, bash: true }))
  assert.equal(s.preferences.theme, 'dark')
  assert.equal(s.preferences.editorFontSize, 20)
  assert.equal(s.preferences.editorTabSize, 4)
  assert.equal(s.preferences.bash, undefined)
  assert.equal(s.attributes.get('data-theme'), 'dark')
  assert.equal(s.updatePreference('sendKey', 'ctrl-enter'), true)
  assert.equal(JSON.parse(s.values.get(s.PREFERENCE_STORAGE_KEY)).sendKey, 'ctrl-enter')
  assert.equal(s.usePreferences().preferences, s.preferences, 'same-page consumers share reactive state')
  assert.equal(s.resetPreferences(['theme']), true)
  assert.equal(s.preferences.theme, 'light')
  assert.equal(s.preferences.sendKey, 'ctrl-enter')
  assert.equal(s.attributes.get('data-theme'), 'light')
})

test('invalid values and failed persistence never apply unpersisted controls', async t => {
  const s = await setup(t)
  for (const [key, value] of [['editorFontSize', 25], ['editorFontSize', 14.5], ['editorMinimap', 'false'], ['permissionMode', 'full-access'], ['theme', '__proto__'], ['defaultView', 'chat']]) {
    assert.equal(s.updatePreference(key, value), false)
  }
  assert.equal(s.preferences.editorFontSize, 14)
  s.storage.setItem = () => { throw new Error('quota') }
  assert.equal(s.updatePreference('theme', 'dark'), false)
  assert.match(s.storageError.value, /保存失败/)
  assert.equal(s.preferences.theme, 'light')
  assert.equal(s.attributes.get('data-theme'), 'light')
  assert.equal(s.resetPreferences(), false)
})

test('chat startup preference migrates and persists code without losing shared preferences', async t => {
  const s = await setup(t, JSON.stringify({ defaultView: 'chat', theme: 'dark', sendKey: 'ctrl-enter', editorFontSize: 18 }))
  assert.equal(s.preferences.defaultView, 'code')
  assert.equal(s.preferences.theme, 'dark')
  assert.equal(s.preferences.sendKey, 'ctrl-enter')
  assert.equal(s.preferences.editorFontSize, 18)
  const persisted = JSON.parse(s.values.get(s.PREFERENCE_STORAGE_KEY))
  assert.equal(persisted.defaultView, 'code')
  assert.equal(persisted.theme, 'dark')
  assert.equal(persisted.sendKey, 'ctrl-enter')
  assert.equal(s.updatePreference('defaultView', 'last'), false)
  assert.equal(s.resetPreferences(['defaultView']), true)
  assert.equal(s.preferences.defaultView, 'code')
})

test('chat startup migration still opens code when browser storage cannot save the migration', async t => {
  const s = await setup(t)
  s.values.set(s.PREFERENCE_STORAGE_KEY, JSON.stringify({ defaultView: 'chat', theme: 'dark' }))
  s.storage.setItem = () => { throw new Error('quota') }
  const other = await import(`../src/lib/preferences.js?test=${++serial}`)
  const migrated = other.usePreferences()
  assert.equal(migrated.preferences.defaultView, 'code')
  assert.equal(migrated.preferences.theme, 'dark')
  assert.match(migrated.storageError.value, /无法保存启动偏好/)
})

test('system theme follows OS updates, explicit theme stays fixed and cleanup removes listeners', async t => {
  const s = await setup(t)
  s.updatePreference('theme', 'system')
  assert.equal(s.attributes.get('data-theme'), 'light')
  s.media.matches = true
  s.mediaListeners.forEach(fn => fn())
  assert.equal(s.attributes.get('data-theme'), 'dark')
  s.updatePreference('theme', 'light')
  s.mediaListeners.forEach(fn => fn())
  assert.equal(s.attributes.get('data-theme'), 'light')
  s.updatePreference('reducedMotion', true)
  assert.equal(s.attributes.get('data-reduced-motion'), 'true')
  s.dispose()
  assert.equal(s.mediaListeners.size, 0)
  assert.equal(s.listeners.size, 0)
})

test('cross-tab changes and clearing preferences sync without write-back loops', async t => {
  const s = await setup(t)
  let writes = 0
  s.storage.setItem = () => { writes++ }
  const sync = s.listeners.get('storage')
  sync({ key: s.PREFERENCE_STORAGE_KEY, storageArea: s.storage, newValue: JSON.stringify({ theme: 'dark', editorWordWrap: 'on' }) })
  assert.equal(s.preferences.editorWordWrap, 'on')
  assert.equal(s.attributes.get('data-theme'), 'dark')
  assert.equal(writes, 0)
  sync({ key: s.PREFERENCE_STORAGE_KEY, newValue: '{broken' })
  assert.equal(s.preferences.theme, 'dark')
  assert.match(s.storageError.value, /其他窗口/)
  sync({ key: 'other', newValue: null })
  assert.equal(s.preferences.theme, 'dark')
  sync({ key: s.PREFERENCE_STORAGE_KEY, newValue: null })
  assert.equal(s.preferences.theme, 'light')
  sync({ key: null, newValue: null })
  assert.equal(writes, 0)
})

test('corrupt and denied browser storage have readable errors instead of crashing startup', async t => {
  const s = await setup(t, '{bad')
  assert.equal(s.preferences.theme, 'light')
  assert.match(s.storageError.value, /无法读取/)
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('SecurityError') } })
  const other = await import(`../src/lib/preferences.js?test=${++serial}`)
  assert.doesNotThrow(() => other.usePreferences())
  assert.match(other.usePreferences().storageError.value, /无法读取/)
})

test('send key handles Ctrl and Command, and never sends IME/newline/repeat keys', async t => {
  const { shouldSendMessage: send } = await setup(t)
  assert.equal(send({ key: 'Enter' }, 'enter'), true)
  assert.equal(send({ key: 'Enter' }, 'ctrl-enter'), false)
  for (const modifier of ['ctrlKey', 'metaKey']) assert.equal(send({ key: 'Enter', [modifier]: true }, 'ctrl-enter'), true)
  for (const event of [{ key: 'Enter', shiftKey: true }, { key: 'Enter', altKey: true }, { key: 'Enter', isComposing: true },
    { key: 'Enter', keyCode: 229 }, { key: 'Enter', repeat: true }, { key: 'a' }]) {
    assert.equal(send(event, 'enter'), false)
    assert.equal(send({ ...event, ctrlKey: true }, 'ctrl-enter'), false)
  }
  assert.equal(send({ key: 'Enter' }, 'enter', true), false)
})

test('removed last-page startup migrates to code and preserves editor preferences', async t => {
  const s = await setup(t, JSON.stringify({ defaultView: 'last', theme: 'dark', editorFontSize: 20 }))
  assert.equal(s.preferences.defaultView, 'code')
  assert.equal(s.preferences.theme, 'dark')
  assert.equal(s.preferences.editorFontSize, 20)
  assert.equal(JSON.parse(s.values.get(s.PREFERENCE_STORAGE_KEY)).defaultView, 'code')
  assert.equal(s.updatePreference('defaultView', 'last'), false)
})
