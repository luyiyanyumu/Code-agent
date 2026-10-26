import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'

const settle = async () => { await Vue.nextTick(); await new Promise(setImmediate); await Vue.nextTick() }
const descendants = node => [node, ...node.children.flatMap(descendants)]
const text = node => node.text + node.children.map(text).join('')
const toolbarNode = root => descendants(root).find(node => node.props['aria-label'] === '页面操作')
const button = (root, label) => descendants(root).find(node => node.type === 'button' && text(node) === label)
const hasClass = (node, name) => node.props?.some(prop => prop.type === 6 && prop.name === 'class' && prop.value?.content.split(/\s+/).includes(name))
function astPath(node, predicate, parents = []) {
  if (predicate(node)) return [...parents, node]
  for (const child of node.children || []) {
    const found = astPath(child, predicate, [...parents, node])
    if (found) return found
  }
}

// Compile the production script and its actual inline toolbar subtree. The canvas
// is covered elsewhere; this fixture needs no browser DOM, iframe or live API.
async function setup(t) {
  const url = new URL('../src/components/code/FrontendDesign.vue', import.meta.url)
  const { descriptor, errors } = parse(await readFile(url, 'utf8'))
  assert.deepEqual(errors, [])
  const path = astPath(descriptor.template.ast, node => hasClass(node, 'design-toolbar'))
  assert.ok(path, 'production template contains its page operation group')
  const sourceBar = path.at(-2), toolbar = path.at(-1)
  assert.ok(hasClass(sourceBar, 'source-bar'), 'page operations share the source/address row')
  assert.equal(path.some(node => node.tag === 'Teleport'), false, 'page operations stay inline')
  const siblings = sourceBar.children.filter(node => node.type === 1)
  assert.ok(hasClass(siblings[0], 'entry-label'))
  assert.ok(hasClass(siblings[1], 'url-label'))
  assert.equal(siblings[2].tag, 'button')
  assert.ok(hasClass(siblings[3], 'device-switch'))
  assert.equal(siblings[4], toolbar, 'page operations follow address, load and preview-width controls')
  const fixtureDescriptor = { ...descriptor, template: { ...descriptor.template, content: toolbar.loc.source, ast: undefined } }
  const compiled = compileScript(fixtureDescriptor, { id: 'frontend-design-toolbar', inlineTemplate: true })
  const calls = [], warnings = [], apps = [], storage = new Map()
  const fakeApi = { code: new Proxy({}, { get: (_, name) => async projectId => {
    calls.push({ name, projectId })
    assert.equal(name, 'designInfo', 'toolbar fixture must never request preview, writes or model work')
    return { entries: [] }
  } }) }
  const bindings = {
    window: { addEventListener() {}, removeEventListener() {} },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    setTimeout, clearTimeout, AbortController,
  }
  const imports = []
  let script = compiled.content.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?\r?$/gm, (_, clause, specifier) => {
    imports.push({ clause: clause.trim(), specifier }); return ''
  })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? Vue : specifier.endsWith('/api.js') ? { api: fakeApi }
      : specifier.endsWith('.vue') ? { default: Vue.defineComponent({ render: () => Vue.h('svg') }) }
        : await import(new URL(specifier, url))
    if (clause.startsWith('{')) for (const entry of clause.slice(1, -1).split(',').map(value => value.trim()).filter(Boolean)) {
      const [key, local = key] = entry.split(/\s+as\s+/); bindings[local] = namespace[key]
    }
    else bindings[clause] = namespace.default
  }
  script = script.replace('export default', 'return')
  const Subject = new Function(...Object.keys(bindings), script)(...Object.values(bindings))
  const node = (type, value = '') => Vue.markRaw({ type, text: value, props: {}, style: { display: '' }, children: [], parent: null })
  const remove = child => {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
    child.parent = null
  }
  const insert = (child, parent, anchor = null) => {
    remove(child)
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent
  }
  const renderer = Vue.createRenderer({
    createElement: type => node(type), createText: value => node('#text', value), createComment: value => node('#comment', value),
    insert, remove, parentNode: child => child.parent,
    nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] ?? null,
    patchProp: (element, key, previous, value) => { element.props[key] = value },
    setText: (element, value) => { element.text = value },
    setElementText: (element, value) => { element.text = value; element.children = [] }, setScopeId() {},
    insertStaticContent(value, parent, anchor) { const element = node('#static', value); insert(element, parent, anchor); return [element, element] },
  })
  function mount({ projectId = 'toolbar-project', visible = true } = {}) {
    const props = Vue.reactive({ projectId, visible })
    const root = node('root'), app = renderer.createApp({ render: () => Vue.h(Subject, props) })
    app.config.warnHandler = message => warnings.push(message)
    app.mount(root)
    let stopped = false
    const unmount = () => { if (!stopped) { stopped = true; app.unmount() } }
    apps.push(unmount)
    return { props, root, unmount }
  }
  t.after(() => { apps.forEach(unmount => unmount()); assert.deepEqual(warnings, []) })
  return { mount, calls }
}

test('page operations follow the source controls inline and mode buttons update production state', async t => {
  const fixture = await setup(t), view = fixture.mount()
  await settle()
  assert.ok(toolbarNode(view.root))
  assert.equal(button(view.root, '验收').props['aria-pressed'], true)
  assert.equal(button(view.root, '编辑').props['aria-pressed'], false)
  button(view.root, '编辑').props.onClick()
  await settle()
  assert.equal(button(view.root, '编辑').props['aria-pressed'], true)
  assert.equal(button(view.root, '验收').props['aria-pressed'], false)
  assert.ok(button(view.root, '保存样式').props.disabled, 'no fake preview enables a save')
  assert.deepEqual(fixture.calls, [{ name: 'designInfo', projectId: 'toolbar-project' }])
})

test('hiding the inline toolbar actually hides its DOM and showing it preserves mode and status', async t => {
  const fixture = await setup(t), view = fixture.mount()
  await settle()
  button(view.root, '编辑').props.onClick()
  await settle()
  const toolbar = toolbarNode(view.root), status = descendants(toolbar).find(node => node.props.role === 'status')
  const originalStatus = text(status)
  view.props.visible = false
  await settle()
  assert.equal(toolbar.style.display, 'none')
  view.props.visible = true
  await settle()
  assert.equal(toolbarNode(view.root), toolbar, 'visibility does not remount controls')
  assert.equal(toolbar.style.display, '')
  assert.equal(button(view.root, '编辑').props['aria-pressed'], true)
  assert.equal(text(status), originalStatus)
  assert.equal(fixture.calls.length, 1, 'showing the existing toolbar does not reload the project')
})

test('two instances keep independent inline state and unmounting removes only their own toolbar', async t => {
  const fixture = await setup(t)
  const first = fixture.mount({ projectId: 'first' }), second = fixture.mount({ projectId: 'second' })
  await settle()
  assert.notEqual(toolbarNode(first.root), toolbarNode(second.root))
  button(first.root, '编辑').props.onClick()
  await settle()
  assert.equal(button(first.root, '编辑').props['aria-pressed'], true)
  assert.equal(button(second.root, '验收').props['aria-pressed'], true)
  first.unmount()
  await settle()
  assert.equal(first.root.children.length, 0, 'unmount removes the inline toolbar')
  assert.ok(toolbarNode(second.root), 'another workspace retains its controls')
  second.unmount()
  await settle()
  assert.equal(second.root.children.length, 0)
})
