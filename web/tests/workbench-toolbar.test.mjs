import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'

const settle = async () => { await Vue.nextTick(); await new Promise(setImmediate); await Vue.nextTick() }
const hasClass = (node, name) => node?.props?.some(prop => prop.type === 6 && prop.name === 'class' && prop.value?.content.split(/\s+/).includes(name))
function astPath(node, predicate, parents = []) {
  if (predicate(node)) return [...parents, node]
  for (const child of node.children || []) { const found = astPath(child, predicate, [...parents, node]); if (found) return found }
}
const all = root => [root, ...root.children.flatMap(all)]
const text = root => (root.text + root.children.map(text).join('')).trim()
const domClass = (node, name) => typeof node.props.class === 'string' && node.props.class.split(/\s+/).includes(name)
const byClass = (root, name) => all(root).find(node => domClass(node, name))
const byLabel = (root, name) => all(root).find(node => node.props['aria-label'] === name)
const button = (root, name) => all(root).find(node => node.type === 'button' && text(node) === name)
const click = node => { assert.ok(node, 'the control is rendered'); assert.notEqual(node.props.disabled, true, 'the control is enabled'); node.props.onClick({ preventDefault() {}, stopPropagation() {} }) }

// Compile actual handlers and the actual toolbar/status/bottom-panel subtrees.
// Deferred startup is omitted; all API access remains a failing offline stub.
async function setup(t) {
  const url = new URL('../src/views/CodeView.vue', import.meta.url)
  const exposed = 'project,file,centerView,manualReadonly,rightTab,problemsVisible,bottomTab,runtimeActivated,debugActivated,notice,indexRefresh,indexStatusError,onWorkbenchKey,showBottom,debugRef,editorRef,quickOpen,quickMode'
  const source = (await readFile(url, 'utf8')).replace('</script>', `\ndefineExpose({ toolbarTestState: () => ({${exposed}}) })\n</script>`)
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  const path = name => astPath(descriptor.template.ast, node => hasClass(node, name))
  const top = path('workbench-bar')?.at(-1), editor = path('editor-workbench-tools'), status = path('workspace-notice-bar')?.at(-1), bottom = path('bottom-panel')?.at(-1)
  assert.ok(top && editor && status && bottom, 'all production toolbar regions exist')
  assert.ok(astPath(top, node => hasClass(node, 'workspace-view-switcher')), 'mode switching belongs to the global top bar')
  assert.ok(editor.slice(0, -1).some(node => node.tag === 'main' && hasClass(node, 'center')), 'editor tools belong inside the editor center')
  assert.equal(path('workspace-view-bar'), undefined, 'no extra unconditional mode row remains')
  assert.ok(status.props.some(prop => prop.type === 7 && prop.name === 'if'), 'empty status never occupies its own row')
  const consumers = ['EditorPane', 'FrontendDesign', 'ChangesPanel'].map(name => {
    const node = astPath(descriptor.template.ast, item => item.tag === name)?.at(-1)
    assert.ok(node?.props.some(prop => prop.type === 7 && prop.name === 'bind' && prop.arg?.content === 'readonly' && /\bmanualReadonly\b/.test(prop.exp?.content)), `${name} consumes the shared permission`)
    return node.loc.source
  }).join('')
  const content = `<section>${top.loc.source}${status.loc.source}<main>${editor.at(-1).loc.source}${consumers}${bottom.loc.source}</main></section>`
  const fixtureDescriptor = { ...descriptor, template: { ...descriptor.template, content, ast: undefined } }
  const compiled = compileScript(fixtureDescriptor, { id: 'workbench-toolbar', inlineTemplate: true })
  const calls = [], warnings = [], storage = new Map(), mounts = new Map(), events = []
  const browserStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  const fakeApi = { code: new Proxy({}, { get: (_, name) => async () => { calls.push(String(name)); throw new Error(`Unexpected toolbar API: ${String(name)}`) } }) }
  const widget = name => Vue.defineComponent({ name, props: ['visible', 'readonly'], setup(props) {
    mounts.set(name, (mounts.get(name) || 0) + 1)
    return () => Vue.h(name === 'UiIcon' ? 'svg' : 'div', { 'data-widget': name, 'data-visible': props.visible, 'data-readonly': props.readonly }, name === 'AgentLogo' ? 'Code-agent' : '')
  } })
  let timerId = 0
  const bindings = { window: { innerWidth: 1280, addEventListener() {}, removeEventListener() {} }, document: { visibilityState: 'visible', activeElement: null, querySelector: () => null }, localStorage: browserStorage,
    setTimeout: () => ++timerId, clearTimeout() {}, setInterval: () => ++timerId, clearInterval() {},
  }
  const imports = []
  let script = compiled.content.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?\r?$/gm, (_, clause, specifier) => { imports.push({ clause: clause.trim(), specifier }); return '' })
  for (const { clause, specifier } of imports) {
    let namespace
    if (specifier === 'vue') namespace = { ...Vue, onMounted() {}, defineAsyncComponent: loader => widget(loader.toString().match(/([\w]+)\.vue/)[1]) }
    else if (specifier.endsWith('/api.js')) namespace = { api: fakeApi }
    else if (specifier.endsWith('.vue')) namespace = { default: widget(specifier.split('/').at(-1).replace('.vue', '')) }
    else if (specifier.endsWith('/useDialog.js')) namespace = { useDialog: () => ({ open: async () => null }) }
    else {
      const imported = await import(new URL(specifier, url))
      namespace = imported
      if (specifier.endsWith('/useWorkbench.js')) namespace = { ...imported, useWorkbench: options => imported.useWorkbench({ ...options, storage: browserStorage }) }
      if (specifier.endsWith('/useProjectAccess.js')) namespace = { ...imported, useProjectAccess: options => imported.useProjectAccess({ ...options, storage: browserStorage }) }
    }
    if (clause.startsWith('{')) for (const entry of clause.slice(1, -1).split(',').map(value => value.trim()).filter(Boolean)) {
      const [key, local = key] = entry.split(/\s+as\s+/); bindings[local] = namespace[key]
    }
    else bindings[clause] = namespace.default
  }
  script = script.replace('export default', 'return')
  const Subject = new Function(...Object.keys(bindings), script)(...Object.values(bindings))
  const node = (type, value = '') => Vue.markRaw({ type, text: value, props: {}, style: { display: '' }, children: [], parent: null })
  const remove = child => { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null }
  const insert = (child, parent, anchor = null) => { remove(child); const index = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent }
  const renderer = Vue.createRenderer({ createElement: type => node(type), createText: value => node('#text', value), createComment: value => node('#comment', value), insert, remove,
    parentNode: child => child.parent, nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
    patchProp: (element, key, previous, value) => { element.props[key] = value }, setText: (element, value) => { element.text = value }, setElementText: (element, value) => { element.text = value; element.children = [] }, setScopeId() {},
    insertStaticContent(value, parent, anchor) { const element = node('#static', value); insert(element, parent, anchor); return [element, element] },
  })
  const root = node('root'), app = renderer.createApp(Subject, { user: { id: 'toolbar-owner' }, visible: true, onOpenSettings: () => events.push('settings') })
  app.config.warnHandler = message => warnings.push(message)
  const vm = app.mount(root), state = vm.toolbarTestState()
  t.after(() => { app.unmount(); assert.deepEqual(calls, [], 'toolbar tests never call a real backend'); assert.deepEqual(warnings, []) })
  state.project.value = { id: 'toolbar-project', name: '工作区', root_path: '/fixture/project', available: true }
  await settle()
  return { root, state, mounts, events }
}

test('global toolbar owns the unique mode selector while editor actions stay in the editor toolbar', async t => {
  const { root, state, events } = await setup(t)
  const top = byClass(root, 'workbench-bar'), modes = byLabel(top, '工作区视图'), tools = byLabel(root, '编辑器工具')
  assert.equal(all(root).filter(node => node.props['aria-label'] === '工作区视图').length, 1)
  assert.ok(all(top).some(node => node.props['data-widget'] === 'AgentLogo'))
  for (const label of ['导航后退', '导航前进']) { assert.equal(byLabel(top, label), undefined); assert.ok(byLabel(tools, label)) }
  for (const label of ['终端', '运行', '调试']) { assert.equal(button(top, label), undefined); assert.ok(button(tools, label)) }
  assert.ok(byClass(top, 'editing-trigger'))
  assert.equal(byLabel(tools, '允许编辑'), undefined)
  for (const [name, mode] of [['思维导图', 'map'], ['前端设计', 'design'], ['编辑器', 'file']]) {
    click(button(modes, name)); await settle()
    assert.equal(state.centerView.value, mode)
    const selected = all(modes).filter(node => node.type === 'button' && node.props['aria-pressed'] === true)
    assert.equal(selected.length, 1)
    assert.equal(text(selected[0]), name)
    assert.equal(tools.style.display, mode === 'file' ? '' : 'none')
  }
  click(byLabel(top, '打开设置'))
  assert.deepEqual(events, ['settings'])
})

test('editor actions remain functional and the bottom runtime is hidden across modes without remounting', async t => {
  const { root, state, mounts } = await setup(t)
  const tools = byLabel(root, '编辑器工具'), modes = byLabel(root, '工作区视图')
  click(byLabel(byClass(root, 'workbench-bar'), '允许编辑')); await settle()
  assert.equal(state.manualReadonly.value, true)
  assert.equal(byLabel(byClass(root, 'workbench-bar'), '编辑只读').props['aria-pressed'], true)
  click(all(tools).find(node => node.type === 'button' && text(node).startsWith('问题'))); await settle()
  assert.equal(state.problemsVisible.value, true)
  assert.equal(state.bottomTab.value, 'problems')
  assert.equal(all(root).find(node => node.props['data-widget'] === 'ProblemsPanel').props['data-visible'], true)
  click(button(tools, '终端')); await settle()
  const panel = byClass(root, 'bottom-panel'), runtime = all(root).find(node => node.props['data-widget'] === 'RuntimePanel')
  assert.equal(state.bottomTab.value, 'terminal')
  assert.equal(runtime.props['data-visible'], true)
  for (const mode of ['思维导图', '前端设计']) {
    click(button(modes, mode)); await settle()
    assert.equal(panel.style.display, 'none')
    assert.equal(runtime.props['data-visible'], false)
    assert.equal(all(root).find(node => node.props['data-widget'] === 'ProblemsPanel').props['data-visible'], false)
    assert.equal(mounts.get('RuntimePanel'), 1)
    assert.equal(state.manualReadonly.value, true)
  }
  click(button(modes, '编辑器')); await settle()
  assert.equal(panel.style.display, '')
  assert.equal(all(root).find(node => node.props['data-widget'] === 'RuntimePanel'), runtime)
  assert.equal(runtime.props['data-visible'], true)
  click(byLabel(root, '关闭问题面板')); await settle()
  assert.equal(runtime.props['data-visible'], false)
  assert.equal(mounts.get('RuntimePanel'), 1)
})

test('one global permission toggle works from map and design and updates every shared readonly consumer', async t => {
  const { root, state } = await setup(t)
  state.file.value = { path: 'main.py', text: '', missing: false, needsReload: false }; await settle()
  const top = byClass(root, 'workbench-bar'), modes = byLabel(top, '工作区视图')
  const consumer = name => all(root).find(node => node.props['data-widget'] === name)
  assert.equal(all(root).filter(node => domClass(node, 'editing-trigger')).length, 1)
  assert.equal(consumer('EditorPane').props['data-readonly'], false)
  click(button(modes, '思维导图')); await settle()
  click(byLabel(top, '允许编辑')); await settle()
  assert.equal(state.manualReadonly.value, true)
  assert.equal(consumer('EditorPane').props['data-readonly'], true)
  click(button(modes, '前端设计')); await settle()
  assert.equal(consumer('FrontendDesign').props['data-readonly'], true)
  click(byLabel(top, '编辑只读')); await settle()
  assert.equal(state.manualReadonly.value, false)
  assert.equal(consumer('FrontendDesign').props['data-readonly'], false)
  assert.equal(consumer('EditorPane').props['data-readonly'], false)
  state.rightTab.value = 'changes'; await settle()
  assert.equal(consumer('ChangesPanel').props['data-readonly'], false)
  click(byLabel(top, '允许编辑')); await settle()
  for (const name of ['EditorPane', 'FrontendDesign', 'ChangesPanel']) assert.equal(consumer(name).props['data-readonly'], true)
  assert.equal(byClass(root, 'editor-workbench-tools').style.display, 'none')
})

test('status notices render only when needed and disappear after completion or clearing', async t => {
  const { root, state } = await setup(t)
  assert.equal(byLabel(root, '工作区状态'), undefined)
  state.notice.value = '已准备好输入'; await settle()
  assert.ok(text(byLabel(root, '工作区状态')).includes('已准备好输入'))
  state.notice.value = ''; state.indexRefresh.value = { pending: true, state: 'running' }; await settle()
  assert.ok(text(byLabel(root, '工作区状态')).includes('索引更新中'))
  state.indexRefresh.value = { pending: false, state: 'done' }; await settle()
  assert.equal(byLabel(root, '工作区状态'), undefined)
  state.indexRefresh.value = { pending: false, state: 'error', error: '索引未完成' }; await settle()
  assert.ok(text(byLabel(root, '工作区状态')).includes('索引更新失败'))
  assert.ok(button(byLabel(root, '工作区状态'), '重试'))
  state.indexRefresh.value = null
  state.indexStatusError.value = '索引进度暂时不可用'; await settle()
  assert.ok(text(byLabel(root, '工作区状态')).includes('索引进度暂时不可用'))
  assert.ok(button(byLabel(root, '工作区状态'), '重试'))
  state.indexStatusError.value = ''; await settle()
  assert.equal(byLabel(root, '工作区状态'), undefined)
})

test('unified search shortcuts switch modes and the modal blocks background editor execution', async t => {
  const { state } = await setup(t)
  let prevented = 0, continued = 0, breakpoint = 0
  const key = overrides => ({ key: '', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
    preventDefault: () => prevented++, ...overrides })
  state.debugRef.value = { isPaused: () => true, continue: () => continued++ }
  state.editorRef.value = { toggleBreakpoint: () => breakpoint++ }
  state.onWorkbenchKey(key({ key: 'p', ctrlKey: true }))
  assert.equal(state.quickOpen.value, true)
  assert.equal(state.quickMode.value, 'files')
  state.onWorkbenchKey(key({ key: 'F', ctrlKey: true, shiftKey: true }))
  assert.equal(state.quickMode.value, 'content')
  for (const event of [{ key: 'F5' }, { key: 'F5', ctrlKey: true }, { key: 'F9' }]) state.onWorkbenchKey(key(event))
  assert.equal(continued, 0)
  assert.equal(breakpoint, 0)
  assert.equal(state.problemsVisible.value, false)
  state.onWorkbenchKey(key({ key: 'p', metaKey: true }))
  assert.equal(state.quickMode.value, 'files')
  assert.equal(prevented, 3)
})

test('editor shortcuts ignore other modes and showBottom explicitly returns to the editor', async t => {
  const { root, state } = await setup(t)
  let prevented = 0, continued = 0, breakpoint = 0
  const key = overrides => ({ key: '', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, defaultPrevented: false, preventDefault: () => prevented++, ...overrides })
  state.editorRef.value = { toggleBreakpoint: () => breakpoint++ }
  state.debugRef.value = { isPaused: () => true, continue: () => continued++ }
  for (const mode of ['map', 'design']) {
    state.centerView.value = mode; await settle()
    for (const event of [{ key: 'F5' }, { key: 'F5', ctrlKey: true }, { key: 'F9' }, { key: 'ArrowLeft', altKey: true }, { key: 'ArrowRight', altKey: true }]) state.onWorkbenchKey(key(event))
    assert.equal(state.centerView.value, mode)
  }
  assert.equal(prevented, 0)
  assert.equal(continued, 0)
  assert.equal(breakpoint, 0)
  state.showBottom('terminal'); await settle()
  assert.equal(state.centerView.value, 'file')
  assert.equal(state.bottomTab.value, 'terminal')
  assert.equal(byClass(root, 'bottom-panel').style.display, '')
  state.editorRef.value = { toggleBreakpoint: () => breakpoint++ }
  state.onWorkbenchKey(key({ key: 'F9' }))
  state.onWorkbenchKey(key({ key: 'ArrowLeft', altKey: true }))
  state.onWorkbenchKey(key({ key: 'ArrowRight', altKey: true }))
  assert.equal(breakpoint, 1)
  assert.equal(prevented, 3)
  state.debugRef.value = { isPaused: () => true, continue: () => continued++ }
  state.onWorkbenchKey(key({ key: 'F5' })); await settle()
  assert.equal(continued, 1)
  assert.equal(state.bottomTab.value, 'debug')
  const debug = all(root).find(node => node.props['data-widget'] === 'DebugPanel')
  assert.equal(debug.props['data-visible'], true)
  state.centerView.value = 'map'; await settle()
  assert.equal(debug.props['data-visible'], false)
})
