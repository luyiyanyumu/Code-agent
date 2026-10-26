import assert from 'node:assert/strict'
import test from 'node:test'
import vm from 'node:vm'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { SETTINGS_SECTIONS } from '../src/lib/settingsCatalog.js'

// Render the actual App template with small stateful editor/settings fixtures.
// Their draft buffers let this regression detect accidental component remounts.
async function setup({ savedView, savedSection, storageBlocked = false } = {}) {
  const source = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: 'workspace-shell' })
  const template = compileTemplate({ source: descriptor.template.content, filename: 'App.vue', id: 'workspace-shell', compilerOptions: { bindingMetadata: compiled.bindings } })
  assert.deepEqual(template.errors, [])
  const script = descriptor.scriptSetup.content.replace(/^import .*\r?\n/gm, '')
  const storage = new Map(Object.entries({
    ...(savedView ? { 'code-agent:view': savedView } : {}),
    ...(savedSection ? { 'code-agent:settings-section': savedSection } : {}),
  }))
  const listeners = new Map(), instances = new Map(), unmounted = []
  let effectsDisposed = 0, focusCalls = 0
  const focusTarget = { isConnected: true, focus: () => { focusCalls++ } }
  const component = name => Vue.defineComponent({
    name,
    props: ['user', 'visible', 'initialSection', 'initialToolTab'],
    emits: ['open-settings', 'close', 'navigate', 'section-change'],
    setup(props, { emit }) {
      const draft = Vue.ref('')
      const instance = { draft, props, openSettings: () => emit('open-settings'), close: () => emit('close'), navigate: target => emit('navigate', target) }
      instances.set(name, [...(instances.get(name) || []), instance])
      Vue.onBeforeUnmount(() => unmounted.push(name))
      return () => Vue.h('div', { 'data-component': name }, draft.value)
    },
  })
  const context = vm.createContext({
    ...Vue,
    SETTINGS_SECTIONS,
    defineAsyncComponent: loader => component(loader.toString().match(/\/([\w]+)\.vue/)[1]),
    DialogHost: component('DialogHost'),
    AgentLogo: Vue.defineComponent({ name: 'AgentLogo', render: () => Vue.h('span', { 'data-component': 'AgentLogo' }) }),
    api: { me: async () => ({ id: 'fixture-owner', role: 'admin' }) }, getToken: () => '', setToken() {},
    usePreferences: () => ({ preferences: { defaultView: 'last' } }),
    initializePreferences: () => () => { effectsDisposed++ },
    localStorage: {
      getItem(key) { if (storageBlocked) throw new Error('Storage unavailable'); return storage.get(key) ?? null },
      setItem(key, value) { if (storageBlocked) throw new Error('Storage unavailable'); storage.set(key, value) },
    },
    window: { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name) } },
    document: { activeElement: focusTarget, querySelector: () => focusTarget },
  })
  let renderSource = template.code.replace(/import \{([^}]+)\} from ["']vue["']/g, (_, imports) => {
    for (const entry of imports.split(',')) {
      const [name, alias = name] = entry.trim().split(/\s+as\s+/)
      context[alias] = Vue[name]
    }
    return ''
  }).replace('export function render', 'function render')
  vm.runInContext(`${renderSource}\n;globalThis.renderApp = render`, context)
  const App = Vue.defineComponent({
    setup() {
      vm.runInContext(`${script}\n;globalThis.subject = { ${Object.keys(compiled.bindings).join(', ')} }`, context)
      return context.subject
    },
    render: context.renderApp,
  })
  const node = (type, text = '') => ({ type, text, children: [], props: {}, style: {}, parent: null })
  const renderer = Vue.createRenderer({
    createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
    insert(child, parent, anchor = null) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
      const index = anchor ? parent.children.indexOf(anchor) : -1
      parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
      child.parent = parent
    },
    remove(child) { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null },
    patchProp(element, key, old, value) { element.props[key] = value },
    setText: (element, text) => { element.text = text },
    setElementText(element, text) { element.text = text; element.children = [] },
    parentNode: element => element.parent,
    nextSibling: element => element.parent?.children[element.parent.children.indexOf(element) + 1] || null,
    setScopeId() {},
    insertStaticContent(text, parent, anchor) {
      const element = node('#static', text)
      const index = anchor ? parent.children.indexOf(anchor) : -1
      parent.children.splice(index < 0 ? parent.children.length : index, 0, element)
      element.parent = parent
      return [element, element]
    },
  })
  const root = node('root'), app = renderer.createApp(App)
  app.mount(root)
  await new Promise(resolve => setImmediate(resolve))
  await Vue.nextTick()
  return { s: context.subject, source, descriptor, storage, listeners, instances, unmounted, root,
    get focusCalls() { return focusCalls }, get effectsDisposed() { return effectsDisposed }, dispose: () => app.unmount() }
}

test('startup always shows the editor while old resource pages become remembered settings destinations', async () => {
  for (const [savedView, section] of [['memory', 'memory'], ['knowledge', 'knowledge'], ['usage', 'usage'], ['automations', 'automations'], ['audit', 'audit'], ['tools', 'extensions']]) {
    const fixture = await setup({ savedView })
    try {
      assert.equal(fixture.s.view.value, 'code', savedView)
      assert.equal(fixture.s.settingsInitialSection.value, section, savedView)
      assert.equal(fixture.instances.get('CodeView')?.length, 1)
      assert.equal(fixture.instances.has('SettingsView'), false, 'settings are deferred until explicitly opened')
      fixture.s.openSettings()
      await Vue.nextTick()
      assert.equal(fixture.instances.get('SettingsView')[0].props.initialSection, section)
    } finally { fixture.dispose() }
  }
})

test('editor toolbar opens settings, and returning/reopening retains both editor and settings drafts', async () => {
  const fixture = await setup()
  try {
    const editor = fixture.instances.get('CodeView')[0]
    editor.draft.value = 'unsaved Java edit'
    editor.openSettings()
    await Vue.nextTick()
    assert.equal(fixture.s.view.value, 'settings')
    assert.equal(editor.props.visible, false)
    const settings = fixture.instances.get('SettingsView')[0]
    settings.draft.value = 'unsaved model connection'
    settings.close()
    await Vue.nextTick()
    assert.equal(fixture.s.view.value, 'code')
    assert.equal(editor.props.visible, true)
    assert.equal(settings.props.visible, false)
    editor.openSettings()
    await Vue.nextTick()
    assert.equal(fixture.instances.get('CodeView').length, 1)
    assert.equal(fixture.instances.get('SettingsView').length, 1)
    assert.equal(editor.draft.value, 'unsaved Java edit')
    assert.equal(settings.draft.value, 'unsaved model connection')
    assert.deepEqual(fixture.unmounted, [])
    assert.ok(fixture.focusCalls > 0, 'closing settings returns keyboard focus to the editor toolbar')
  } finally { fixture.dispose() }
})

test('settings shortcut supports Control and Command, ignores composing/modified input, and cleans up on unmount', async () => {
  const fixture = await setup()
  let prevented = 0
  const event = overrides => ({ key: ',', ctrlKey: true, metaKey: false, altKey: false, shiftKey: false, isComposing: false, preventDefault: () => { prevented++ }, ...overrides })
  try {
    const shortcut = fixture.listeners.get('keydown')
    assert.equal(typeof shortcut, 'function')
    for (const overrides of [{ key: '.' }, { ctrlKey: false }, { altKey: true }, { shiftKey: true }, { isComposing: true }]) shortcut(event(overrides))
    assert.equal(fixture.s.view.value, 'code')
    assert.equal(prevented, 0)
    shortcut(event())
    await Vue.nextTick()
    assert.equal(fixture.s.view.value, 'settings')
    fixture.s.closeSettings()
    await Vue.nextTick()
    shortcut(event({ ctrlKey: false, metaKey: true }))
    await Vue.nextTick()
    assert.equal(fixture.s.view.value, 'settings')
    assert.equal(prevented, 2)
  } finally { fixture.dispose() }
  assert.equal(fixture.listeners.has('keydown'), false)
  assert.equal(fixture.effectsDisposed, 1)
})

test('settings links keep all resource destinations inside settings and ignore invalid section changes', async () => {
  const fixture = await setup({ savedView: 'invalid', savedSection: 'invalid' })
  try {
    assert.equal(fixture.s.view.value, 'code')
    assert.equal(fixture.s.settingsInitialSection.value, 'general')
    for (const section of ['memory', 'knowledge', 'usage', 'automations', 'audit']) {
      fixture.s.settingsNavigate({ view: section })
      await Vue.nextTick()
      assert.equal(fixture.s.view.value, 'settings')
      assert.equal(fixture.s.settingsInitialSection.value, section)
      assert.equal(fixture.storage.get('code-agent:settings-section'), section)
    }
    fixture.s.settingsNavigate({ view: 'tools', tab: 'mcp' })
    assert.equal(fixture.s.settingsInitialSection.value, 'extensions')
    assert.equal(fixture.s.settingsInitialToolTab.value, 'mcp')
    fixture.s.settingsSectionChanged('not-a-section')
    assert.equal(fixture.s.settingsInitialSection.value, 'extensions')
    fixture.s.settingsNavigate({ view: 'not-a-view' })
    assert.equal(fixture.s.view.value, 'settings')
    assert.equal(fixture.s.settingsInitialSection.value, 'extensions')
  } finally { fixture.dispose() }
})

test('unavailable browser storage still allows opening settings and returning to code', async () => {
  const fixture = await setup({ storageBlocked: true })
  try {
    assert.equal(fixture.s.view.value, 'code')
    fixture.s.openSettings('knowledge')
    await Vue.nextTick()
    assert.equal(fixture.s.view.value, 'settings')
    assert.equal(fixture.s.settingsInitialSection.value, 'knowledge')
    fixture.s.closeSettings()
    await Vue.nextTick()
    assert.equal(fixture.s.view.value, 'code')
  } finally { fixture.dispose() }
})

test('workspace shell renders only editor and settings, with no global sidebar or standalone resource views', async () => {
  const fixture = await setup()
  try {
    assert.doesNotMatch(fixture.descriptor.template.content, /<aside\b|aria-label="主导航"|NAV_GROUPS/)
    assert.doesNotMatch(fixture.descriptor.template.content, /<(Memory|Knowledge|Usage|Automations|Audit)View\b/)
    assert.doesNotMatch(fixture.descriptor.scriptSetup.content, /import\(['"].*\/(Memory|Knowledge|Usage|Automations|Audit)View\.vue/)
    assert.equal(fixture.instances.get('CodeView').length, 1)
  } finally { fixture.dispose() }
})
