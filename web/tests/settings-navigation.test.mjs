import assert from 'node:assert/strict'
import test from 'node:test'
import vm from 'node:vm'
import { readFile } from 'node:fs/promises'
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { SETTINGS_SECTIONS, SETTINGS_ITEMS, WORKSPACE_SHORTCUTS, searchSettings } from '../src/lib/settingsCatalog.js'
import { PREFERENCE_DEFAULTS } from '../src/lib/preferences.js'

async function setup({ preferenceSaves = true, initialSection } = {}) {
  const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const requests = [], visited = [], changes = []
  const props = reactive({ user: { role: 'admin' }, initialSection, visible: true })
  const context = vm.createContext({ computed, nextTick, ref, watch, SETTINGS_SECTIONS, WORKSPACE_SHORTCUTS, searchSettings,
    defineAsyncComponent: loader => ({ loader }),
    defineProps: () => props, defineEmits: () => (...args) => visited.push(args),
    useDialog: () => ({}), onMounted() {},
    usePreferences: () => ({ preferences: { ...PREFERENCE_DEFAULTS }, storageError: ref(''), updatePreference: (...args) => { changes.push(args); return preferenceSaves }, resetPreferences: keys => { changes.push(keys); return true } }),
    api: { tools: async () => { requests.push('tools'); return { tools: [], mcp: [], plugins: [], packs: [] } }, health: async () => { requests.push('health'); return { ok: true, version: 'fixture' } } },
  })
  vm.runInContext(`${script}\n;globalThis.s = { activeSection, searchQuery, selectSection, contentPane, form, teamForm, setPreference, preferenceNotice, extensionsVisited, toolsPanel, resourceViews, visitedResources, resourcePanels }`, context)
  return { s: context.s, requests, visited, changes, source, props }
}

test('settings search finds real fields by category, Chinese labels and English keywords', () => {
  assert.equal(searchSettings('  ').length, 0)
  assert.equal(searchSettings('editorFontSize secret-credential-value').length, 0)
  assert.equal(searchSettings('字体')[0].anchor, 'editor-font-size')
  assert.deepEqual(searchSettings(' CAGE '), [])
  assert.deepEqual(searchSettings('默认循环模式'), [])
  assert.equal(searchSettings('权限 只读')[0].anchor, 'execution-permission')
  assert.equal(searchSettings('base URL')[0].anchor, 'provider-base-url')
  assert.ok(searchSettings('模型 Token').length >= 2)
  assert.equal(new Set(SETTINGS_SECTIONS.map(s => s.id)).size, 14)
  for (const [query, section] of [['记忆', 'memory'], ['知识库', 'knowledge'], ['用量', 'usage'], ['自动化', 'automations'], ['审计', 'audit']]) {
    assert.ok(searchSettings(query).some(result => result.section === section && result.anchor === `${section}-settings`), query)
  }
})

test('switching categories and search results preserves unsaved model and collaboration drafts', async () => {
  const { s, requests } = await setup()
  s.form.value.model_default = 'unsaved-model'
  s.form.value.api_key = 'test-only-secret'
  s.teamForm.value.max_parallel = 7
  s.searchQuery.value = '权限'
  const focus = [], scroll = []
  s.contentPane.value = { scrollTop: 70, querySelector: selector => ({ scrollIntoView: options => scroll.push(options), matches: () => true, focus: () => focus.push(selector) }) }
  await s.selectSection('permissions', 'execution-permission')
  assert.equal(s.activeSection.value, 'permissions')
  assert.equal(s.searchQuery.value, '')
  assert.deepEqual(focus, ['[id="execution-permission"]'])
  await s.selectSection('models')
  assert.equal(s.form.value.api_key, 'test-only-secret')
  assert.equal(s.form.value.model_default, 'unsaved-model')
  assert.equal(s.teamForm.value.max_parallel, 7)
  assert.deepEqual(requests, [])
  await s.selectSection('invalid')
  assert.equal(s.activeSection.value, 'models')
})

test('tools mount on demand, remain mounted across categories, and search selects the requested tool category in place', async () => {
  const { s, requests, visited } = await setup()
  assert.equal(s.extensionsVisited.value, false)
  const categories = []
  s.toolsPanel.value = { openCategory: tab => categories.push(tab) }
  await s.selectSection('extensions'); await nextTick()
  assert.equal(s.extensionsVisited.value, true)
  await s.selectSection('general'); await s.selectSection('extensions')
  assert.equal(s.extensionsVisited.value, true)
  assert.deepEqual(categories, [], 'ordinary category switching preserves the current tool category and query')
  for (const tab of ['builtin', 'mcp', 'plugin', 'off', 'skill', 'expert']) {
    await s.selectSection('extensions', `extensions-${tab}`)
  }
  assert.deepEqual(categories, ['builtin', 'mcp', 'plugin', 'off', 'skill', 'expert'])
  assert.ok(visited.every(([event]) => event === 'section-change'), 'tool search never navigates out of settings')
  await s.selectSection('system'); await nextTick()
  assert.deepEqual(requests, ['health'], 'embedded ToolsView owns its catalog request; no duplicate parent request')
})

test('remembered or legacy tools page opens tools immediately, while invalid settings categories fall back to general', async () => {
  const { s } = await setup({ initialSection: 'extensions' })
  assert.equal(s.activeSection.value, 'extensions')
  assert.equal(s.extensionsVisited.value, true)
  assert.equal((await setup({ initialSection: 'invalid' })).s.activeSection.value, 'general')
})

test('resource searches open their settings category without navigation, and category changes preserve drafts', async () => {
  const { s, requests, visited } = await setup()
  assert.equal(s.visitedResources.value.size, 0, 'resource screens remain unloaded until first visited')
  s.form.value.model_default = 'unsaved-model'
  s.form.value.api_key = 'fixture-secret'
  s.teamForm.value.max_parallel = 6
  const focused = []
  s.contentPane.value = { scrollTop: 25, querySelector: selector => ({ scrollIntoView() {}, matches: () => true, focus: () => focused.push(selector) }) }
  const resourceSections = ['memory', 'knowledge', 'usage', 'automations', 'audit']
  for (const section of resourceSections) {
    const destination = SETTINGS_ITEMS.find(item => item.section === section)
    s.searchQuery.value = destination.label
    await s.selectSection(destination.section, destination.anchor)
    assert.equal(s.activeSection.value, section)
    assert.ok(s.visitedResources.value.has(section))
    assert.equal(s.searchQuery.value, '')
    assert.equal(s.contentPane.value.scrollTop, 0)
  }
  await s.selectSection('models')
  assert.equal(s.form.value.model_default, 'unsaved-model')
  assert.equal(s.form.value.api_key, 'fixture-secret')
  assert.equal(s.teamForm.value.max_parallel, 6)
  assert.deepEqual(Array.from(s.visitedResources.value), resourceSections)
  assert.deepEqual(focused, resourceSections.map(section => `[id="${section}-settings"]`))
  assert.ok(visited.every(([event]) => event === 'section-change'), 'resource search never navigates out of settings')
  assert.deepEqual(requests, [], 'each resource view owns its requests; opening a section issues no duplicate parent fetch')
})

test('a remembered resource section is prepared on first settings mount without loading the other resources', async () => {
  for (const section of ['memory', 'knowledge', 'usage', 'automations', 'audit']) {
    const { s } = await setup({ initialSection: section })
    assert.equal(s.activeSection.value, section)
    assert.deepEqual(Array.from(s.visitedResources.value), [section])
    assert.equal(s.extensionsVisited.value, false)
  }
})

test('returning to a retained resource refreshes it once, including when settings reopen on a different resource', async () => {
  const { s, props } = await setup()
  const reloads = []
  await s.selectSection('memory')
  assert.deepEqual(reloads, [], 'a new resource owns its first load')
  s.resourcePanels.memory = { reload: async () => reloads.push('memory') }
  s.resourcePanels.usage = { reload: async () => reloads.push('usage') }
  await s.selectSection('general')
  await s.selectSection('memory')
  assert.deepEqual(reloads, ['memory'])
  props.visible = false
  await nextTick()
  await s.selectSection('usage')
  assert.deepEqual(reloads, ['memory'], 'hidden settings must not fetch resource data')
  props.visible = true
  await nextTick(); await nextTick()
  assert.deepEqual(reloads, ['memory', 'usage'])
  props.visible = false
  await nextTick()
  props.initialSection = 'memory'
  props.visible = true
  await nextTick(); await nextTick()
  assert.equal(s.activeSection.value, 'memory')
  assert.deepEqual(reloads, ['memory', 'usage', 'memory'], 'changing section while reopening does not cause duplicate refresh')
})

test('rapid resource switching and repeated activation cannot run stale or overlapping refreshes', async () => {
  const { s } = await setup()
  const reloads = []
  let complete
  s.resourcePanels.memory = { reload: () => { reloads.push('memory'); return new Promise(resolve => { complete = resolve }) } }
  s.resourcePanels.usage = { reload: async () => reloads.push('usage') }
  const oldSection = s.selectSection('usage')
  const newSection = s.selectSection('memory')
  await Promise.all([oldSection, newSection])
  assert.deepEqual(reloads, ['memory'], 'a queued refresh for a section no longer visible is skipped')
  await s.selectSection('memory')
  assert.deepEqual(reloads, ['memory'], 'an outstanding reload is not duplicated')
  complete()
  await nextTick(); await nextTick()
  await s.selectSection('general'); await s.selectSection('memory')
  assert.deepEqual(reloads, ['memory', 'memory'], 'a completed reload does not suppress later visits')
  complete()
})

test('settings view compiles and every search destination points to an existing control', async () => {
  const { source, s } = await setup()
  const execution = await readFile(new URL('../src/components/settings/ExecutionSettings.vue', import.meta.url), 'utf8')
  const tools = await readFile(new URL('../src/views/ToolsView.vue', import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'settings-navigation' })
  const result = compileTemplate({ source: descriptor.template.content, filename: 'SettingsView.vue', id: 'settings-navigation', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(result.errors, [])
  for (const item of SETTINGS_ITEMS) {
    if (item.section === 'extensions') {
      assert.ok(tools.includes("'extensions-' + f.key"))
      assert.ok(tools.includes(`key: '${item.anchor.replace('extensions-', '')}'`))
    }
    else if (item.section === 'permissions') {
      assert.ok(execution.includes('`execution-${setting.id}`'))
      assert.ok(execution.includes(`id: '${item.anchor.replace('execution-', '')}'`))
    } else if (['memory', 'knowledge', 'usage', 'automations', 'audit'].includes(item.section)) {
      assert.equal(item.anchor, `${item.section}-settings`)
      assert.ok(source.includes(':id="`${resourceSection}-settings`"'))
      assert.ok(Object.hasOwn(s.resourceViews, item.section))
    } else assert.ok(source.includes(`id="${item.anchor}"`), item.anchor)
  }
  assert.doesNotMatch(source, /emit\('navigate'/, 'settings destinations stay inside the settings panel')
  assert.match(source, /v-if="visitedResources\.has\(resourceSection\)"/)
  assert.match(source, /v-show="activeSection === resourceSection"/)
  assert.match(source, /aria-label="返回代码工作区"/)
})


test('failed browser preference persistence rolls the visible input back to its effective value', async () => {
  const { s } = await setup({ preferenceSaves: false })
  const toggle = { type: 'checkbox', checked: true }
  s.setPreference('editorMinimap', true, { target: toggle })
  assert.equal(toggle.checked, false)
  const select = { type: 'select-one', value: '24' }
  s.setPreference('editorFontSize', 24, { target: select })
  assert.equal(select.value, '14')
  assert.equal(s.preferenceNotice.value, '')
})
