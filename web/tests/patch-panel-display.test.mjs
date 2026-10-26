import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, markRaw, nextTick, reactive, ref, watch } from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { diffLanguageForPath, reviewDiffOptions } from '../src/lib/diffPresentation.js'

const component = await readFile(new URL('../src/components/code/PatchPanel.vue', import.meta.url), 'utf8')
const { descriptor } = parse(component)
const compiled = compileScript(descriptor, { id: 'patch-panel-display' })
const template = compileTemplate({ source: descriptor.template.content, filename: 'PatchPanel.vue', id: 'patch-panel-display', compilerOptions: { bindingMetadata: compiled.bindings } })
const source = descriptor.scriptSetup.content.replace(/^import .*\r?\n/gm, '').replace("import('../../lib/monacoSetup.js')", 'loadMonaco()')
const settle = async () => { await nextTick(); await new Promise(setImmediate) }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const detail = (id, path = 'docs/说明.md', extra = {}) => ({ id, path, inline: true, before_text: '原说明（更新前）', after_text: '新说明（更新后）', ...extra })

async function setup(t, overrides = {}) {
  const props = reactive({ projectId: 'project-a', refreshKey: 0 })
  const preferences = reactive({ editorFontSize: 14 })
  const events = [], unmounted = [], models = [], editors = [], calls = []
  const themeSubscriptions = []
  const monaco = { editor: {
    createModel(text, language) { const model = { text, language, disposed: 0, dispose() { this.disposed++ } }; models.push(model); return model },
    createDiffEditor(host, options) {
      const editor = { host, options, disposed: 0, model: null, updates: [], setModel(model) { this.model = model }, updateOptions(options) { this.updates.push(options); this.options = { ...this.options, ...options } }, dispose() { this.disposed++ } }
      editors.push(editor); return editor
    },
  } }
  const api = { code: {
    patches: async projectId => { calls.push(['list', projectId]); return { patches: [], counts: { total: 0, active: 0 } } },
    patchDetail: async (projectId, id) => { calls.push(['detail', projectId, id]); return detail(id) },
    revertPatch: async (projectId, id) => { calls.push(['revert', projectId, id]); return { path: 'docs/说明.md', version: 4 } },
    ...overrides.api,
  } }
  const context = vm.createContext({ computed, nextTick, ref, watch, api, diffLanguageForPath, reviewDiffOptions,
    usePreferences: () => ({ preferences }),
    loadMonaco: overrides.loadMonaco || (async () => ({ ensureMonaco: () => monaco, currentEditorTheme: () => 'code-agent-dark', followEditorTheme() { const subscription = { disposed: 0, dispose() { this.disposed++ } }; themeSubscriptions.push(subscription); return subscription } })),
    defineProps: () => props, defineEmits: () => (...args) => events.push(JSON.parse(JSON.stringify(args))), onBeforeUnmount: fn => unmounted.push(fn) })
  const scope = effectScope()
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject={patches,counts,loading,selected,detailLoading,detailError,sideBySide,wrap,busyId,note,diffHost,load,openDetail,closeDetail,revert,getEditor:()=>diffEditor,getModels:()=>diffModels}`, context))
  context.subject.diffHost.value = markRaw({ id: 'fixture-host' })
  let didUnmount = false
  const unmount = () => { if (didUnmount) return; didUnmount = true; unmounted.forEach(fn => fn()); scope.stop() }
  t.after(unmount)
  await settle()
  return { s: context.subject, props, preferences, events, models, editors, calls, monaco, themeSubscriptions, unmount }
}

test('ledger detail compiles with labeled before/after, view and wrapping controls', () => {
  assert.deepEqual(template.errors, [])
  assert.match(component, /修改前/)
  assert.match(component, /修改后/)
  assert.match(component, /行内对照/)
  assert.match(component, /并排对照/)
  assert.match(component, /自动换行/)
  assert.match(component, /aria-label="关闭差异详情"/)
})

test('detail starts as a wrapped inline read-only Markdown diff with the shared Chinese display policy', async t => {
  const h = await setup(t)
  await h.s.openDetail({ id: 'first', path: 'docs/说明.md' })
  assert.equal(h.editors.length, 1)
  assert.equal(h.editors[0].options.renderSideBySide, false)
  assert.equal(h.editors[0].options.wordWrap, 'on')
  assert.equal(h.editors[0].options.readOnly, true)
  assert.equal(h.models[0].language, 'markdown')
  assert.equal(h.models[0].text, '原说明（更新前）')
  assert.equal(h.models[1].text, '新说明（更新后）')
  assert.equal(h.editors[0].options.unicodeHighlight.allowedCharacters['（'], true)
  assert.equal(h.editors[0].options.unicodeHighlight.allowedCharacters['）'], true)
  assert.equal(h.editors[0].options.unicodeHighlight.invisibleCharacters, true)
  assert.equal(h.editors[0].options.unicodeHighlight.ambiguousCharacters, true)
  assert.equal(h.editors[0].options.theme, 'code-agent-dark')
  assert.equal(h.s.detailLoading.value, false)
  assert.deepEqual(h.events, [])
})

test('side-by-side and wrapping toggles reuse both models without discarding review state', async t => {
  const h = await setup(t)
  await h.s.openDetail({ id: 'first' })
  h.s.sideBySide.value = true; h.s.wrap.value = false; await settle()
  assert.equal(h.editors[0].options.renderSideBySide, true)
  assert.equal(h.editors[0].options.wordWrap, 'off')
  assert.equal(h.editors.length, 1)
  assert.equal(h.models.length, 2)
  assert.equal(h.models[0].disposed, 0)
})

test('switching records releases old models and applies the path language; close and unmount release all resources', async t => {
  const h = await setup(t, { api: { patchDetail: async (_projectId, id) => detail(id, id === 'first' ? '说明.md' : '.github/workflows/release.yml') } })
  await h.s.openDetail({ id: 'first' })
  await h.s.openDetail({ id: 'second' })
  assert.equal(h.models[0].disposed, 1); assert.equal(h.models[1].disposed, 1)
  assert.equal(h.models[2].language, 'yaml'); assert.equal(h.models[2].disposed, 0)
  h.s.closeDetail()
  assert.equal(h.s.selected.value, null)
  assert.equal(h.models[2].disposed, 1); assert.equal(h.models[3].disposed, 1)
  assert.equal(h.editors[0].disposed, 1)
  await h.s.openDetail({ id: 'third' })
  h.unmount()
  assert.equal(h.models.length, 6)
  assert.ok(h.models.every(model => model.disposed === 1))
  assert.ok(h.editors.every(editor => editor.disposed === 1))
  assert.ok(h.themeSubscriptions.every(subscription => subscription.disposed === 1))
})

test('saved editor font size updates the existing review editor without recreating models', async t => {
  const h = await setup(t)
  await h.s.openDetail({ id: 'first' })
  h.preferences.editorFontSize = 24; await settle()
  assert.equal(h.editors[0].options.fontSize, 24)
  assert.equal(h.editors[0].options.lineHeight, 40)
  assert.equal(h.models.length, 2)
})

test('a late older detail response cannot replace the most recently selected record', async t => {
  const old = deferred()
  const h = await setup(t, { api: { patchDetail: async (_projectId, id) => id === 'old' ? old.promise : detail(id) } })
  const first = h.s.openDetail({ id: 'old' })
  await h.s.openDetail({ id: 'new' })
  old.resolve(detail('old')); await first
  assert.equal(h.s.selected.value.id, 'new')
  assert.equal(h.models.length, 2)
  assert.equal(h.s.detailLoading.value, false)
})

test('closing while Monaco loads prevents delayed initialization and model leaks', async t => {
  const imported = deferred()
  const h = await setup(t, { loadMonaco: () => imported.promise })
  const pending = h.s.openDetail({ id: 'first' })
  await settle()
  h.s.closeDetail()
  imported.resolve({ ensureMonaco: () => h.monaco }); await pending
  assert.equal(h.s.selected.value, null)
  assert.equal(h.models.length, 0)
  assert.equal(h.editors.length, 0)
})

test('an older Monaco import cannot replace a newer rendered record or resurrect an unmounted panel', async t => {
  const oldImport = deferred()
  let imports = 0
  let h
  h = await setup(t, { api: { patchDetail: async (_projectId, id) => detail(id, `${id}.md`, { after_text: id }) }, loadMonaco: () => ++imports === 1 ? oldImport.promise : Promise.resolve({ ensureMonaco: () => h.monaco }) })
  const pending = h.s.openDetail({ id: 'old' }); await settle()
  await h.s.openDetail({ id: 'new' })
  assert.equal(h.models[1].text, 'new')
  assert.equal(h.models.length, 2)
  h.unmount()
  oldImport.resolve({ ensureMonaco: () => h.monaco }); await pending
  assert.equal(h.models.length, 2)
  assert.ok(h.models.every(model => model.disposed === 1))
  assert.equal(h.editors[0].disposed, 1)
})

test('switching projects rejects old list/detail responses and clears old review resources', async t => {
  const list = deferred(), old = deferred()
  const h = await setup(t, { api: {
    patches: async projectId => projectId === 'project-a' ? list.promise : { patches: [{ id: 'b', path: 'b.py' }], counts: { total: 1, active: 1 } },
    patchDetail: async (_projectId, id) => id === 'old' ? old.promise : detail(id),
  } })
  await h.s.openDetail({ id: 'first' })
  const pending = h.s.openDetail({ id: 'old' })
  h.props.projectId = 'project-b'
  await settle()
  list.resolve({ patches: [{ id: 'a' }], counts: { total: 99, active: 99 } }); old.resolve(detail('old')); await pending; await settle()
  assert.equal(h.s.selected.value, null)
  assert.equal(h.s.patches.value[0].id, 'b')
  assert.equal(h.s.counts.value.total, 1)
  assert.equal(h.s.loading.value, false)
  assert.ok(h.models.every(model => model.disposed === 1))
  assert.equal(h.editors[0].disposed, 1)
  assert.deepEqual(h.events, [])
})

test('empty complete text stays empty and fragment-only records remain explicitly labeled', async t => {
  const h = await setup(t, { api: { patchDetail: async () => detail('empty', 'empty.md', { before_text: '', after_text: '', old_string: 'old fragment', new_string: 'new fragment' }) } })
  await h.s.openDetail({ id: 'empty' })
  assert.equal(h.models[0].text, ''); assert.equal(h.models[1].text, '')
  assert.match(component, /仅显示改动片段/)
})

test('detail errors show the correct record and ignore late errors after closing', async t => {
  const late = deferred()
  const h = await setup(t, { api: { patchDetail: async (_projectId, id) => { if (id === 'late') return late.promise; throw new Error('mock detail failure') } } })
  await h.s.openDetail({ id: 'failed', path: 'failed.java' })
  assert.equal(h.s.detailError.value, 'mock detail failure')
  assert.equal(h.s.detailLoading.value, false)
  assert.deepEqual(h.events, [['error', 'mock detail failure']])
  const pending = h.s.openDetail({ id: 'late' }); h.s.closeDetail(); late.reject(new Error('stale error')); await pending
  assert.equal(h.s.detailError.value, '')
  assert.equal(h.events.length, 1)
})

test('mocked rollback retains the endpoint, changed event and stale-content refusal explanation', async t => {
  const h = await setup(t)
  await h.s.openDetail({ id: 'first' })
  await h.s.revert({ id: 'first' })
  assert.ok(h.calls.some(call => call.join('|') === 'revert|project-a|first'))
  assert.deepEqual(h.events, [['changed', { path: 'docs/说明.md', version: 4, projectId: 'project-a' }]])
  assert.equal(h.s.selected.value, null)
  assert.ok(h.models.every(model => model.disposed === 1))
  assert.match(h.s.note.value, /已回滚/)
  const failed = await setup(t, { api: { revertPatch: async () => { throw new Error('之后又被改过，不能回滚') } } })
  await failed.s.revert({ id: 'conflict' })
  assert.equal(failed.s.note.value, '之后又被改过，不能回滚')
  assert.deepEqual(failed.events, [])
})

test('completed rollback reports its captured project after a project switch without changing the new panel', async t => {
  const result = deferred()
  const h = await setup(t, { api: { revertPatch: async () => result.promise } })
  const pending = h.s.revert({ id: 'first' })
  h.props.projectId = 'project-b'; await settle()
  await h.s.openDetail({ id: 'new-project-record' })
  result.resolve({ path: 'first.md', version: 5 }); await pending
  assert.deepEqual(h.events, [['changed', { path: 'first.md', version: 5, projectId: 'project-a' }]])
  assert.equal(h.s.selected.value.id, 'new-project-record')
  assert.equal(h.s.note.value, '')
  assert.equal(h.models[0].disposed, 0)
})

test('rollback list refresh does not close a newly selected detail while the refresh was pending', async t => {
  const refreshed = deferred()
  let lists = 0
  const h = await setup(t, { api: { patches: async () => ++lists === 1 ? { patches: [], counts: { total: 0, active: 0 } } : refreshed.promise } })
  await h.s.openDetail({ id: 'old' })
  const pending = h.s.revert({ id: 'old' }); await settle()
  await h.s.openDetail({ id: 'new' })
  refreshed.resolve({ patches: [{ id: 'new' }], counts: { total: 1, active: 1 } }); await pending
  assert.equal(h.s.selected.value.id, 'new')
  assert.equal(h.models[2].disposed, 0)
  assert.equal(h.editors[0].disposed, 0)
})
