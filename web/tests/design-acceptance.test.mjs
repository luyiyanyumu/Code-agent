import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { ACCEPTANCE_TEMPLATES, acceptanceSummary, filterAcceptanceChecks, acceptanceTemplateTitles, formatAcceptanceReport } from '../src/lib/designAcceptance.js'

const checks = () => [
  { id: 'one', title: '提交表单', state: 'pending', note: '必填项提示' },
  { id: 'two', title: '打开导航', state: 'passed', note: 'Route 正确' },
  { id: 'three', title: '手机布局', state: 'failed', note: '按钮溢出' },
]
const settle = async () => { await Vue.nextTick(); await new Promise(setImmediate); await Vue.nextTick() }
const all = root => [root, ...root.children.flatMap(all)]
const text = root => root.text + root.children.map(text).join('')
const find = (root, predicate) => all(root).find(predicate)
const label = (root, value) => find(root, node => node.props['aria-label'] === value)
const buttons = root => all(root).filter(node => node.type === 'button')
const click = node => { assert.ok(node, 'button exists'); node.props.onClick?.({ target: node, preventDefault() {}, stopPropagation() {} }) }
function type(node, value) {
  assert.ok(node, 'input exists'); node.value = value
  node.dispatchEvent({ type: 'input', target: node })
}

test('summary and filters preserve existing check records and search titles or notes', () => {
  const input = checks(), before = JSON.stringify(input)
  assert.deepEqual(acceptanceSummary(input), { total: 3, pending: 1, passed: 1, failed: 1, completed: 2, progress: 33 })
  assert.equal(filterAcceptanceChecks(input, { state: 'failed' })[0], input[2])
  assert.deepEqual(filterAcceptanceChecks(input, { query: 'route' }).map(check => check.id), ['two'])
  assert.equal(filterAcceptanceChecks(input, { state: 'pending', query: '溢出' }).length, 0)
  assert.equal(JSON.stringify(input), before)
  assert.equal(acceptanceSummary([]).progress, 0)
})

test('template batches skip existing titles and respect the shared hundred-check limit', () => {
  assert.equal(ACCEPTANCE_TEMPLATES.length, 3)
  const input = Array.from({ length: 99 }, (_, i) => ({ id: String(i), title: `检查${i}`, state: 'pending', note: '' }))
  input[0].title = ACCEPTANCE_TEMPLATES[0].titles[0]
  const titles = acceptanceTemplateTitles('interaction', input)
  assert.equal(titles.length, 1)
  assert.equal(titles[0], ACCEPTANCE_TEMPLATES[0].titles[1])
  assert.equal(input.length, 99)
  assert.deepEqual(acceptanceTemplateTitles('unknown', input), [])
  assert.deepEqual(acceptanceTemplateTitles('form', [...input, { id: '100', title: '最后一项', state: 'pending' }]), [])
})

test('Markdown reports include actual results and safe multiline notes without changing stored records', () => {
  const input = [{ id: 'one', title: '保存 | [按钮]', state: 'failed', note: '<script>alert(1)</script>\n实际结果：失败' }]
  const before = JSON.stringify(input)
  const report = formatAcceptanceReport({ checks: input, entryPath: 'frontend/index.html', device: 'phone' })
  assert.match(report, /frontend\/index\.html/)
  assert.match(report, /手机（390 px）/)
  assert.match(report, /失败 1 项/)
  assert.ok(report.includes('保存 \\| \\[按钮\\]'))
  assert.ok(report.includes('&lt;script&gt;'))
  assert.ok(report.includes('<br>实际结果：失败'))
  assert.equal(report.includes('<script>'), false)
  assert.equal(JSON.stringify(input), before)
  assert.match(formatAcceptanceReport(), /总计 0 项/)
})

// Real production SFC, Vue renderer and v-model directives; no browser or API.
async function setup(t, initial = {}) {
  const url = new URL('../src/components/code/DesignAcceptance.vue', import.meta.url)
  const { descriptor, errors } = parse(await readFile(url, 'utf8'))
  assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: 'design-acceptance', inlineTemplate: true })
  const bindings = {}, imports = []
  let script = compiled.content.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?\r?$/gm, (_, clause, specifier) => { imports.push({ clause: clause.trim(), specifier }); return '' })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? Vue : specifier.endsWith('.vue') ? { default: Vue.defineComponent({ render: () => Vue.h('svg') }) } : await import(new URL(specifier, url))
    if (clause.startsWith('{')) for (const entry of clause.slice(1, -1).split(',').map(value => value.trim()).filter(Boolean)) {
      const [name, local = name] = entry.split(/\s+as\s+/); bindings[local] = namespace[name]
    }
    else bindings[clause] = namespace.default
  }
  script = script.replace('export default', 'return')
  const Subject = new Function(...Object.keys(bindings), script)(...Object.values(bindings))
  const node = (kind, value = '') => {
    const listeners = new Map()
    return Vue.markRaw({ type: kind, tagName: kind.toUpperCase(), text: value, value: '', props: {}, style: {}, children: [], parent: null,
      getRootNode: () => ({ activeElement: null }),
      addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) || []), fn]) }, removeEventListener(type, fn) { listeners.set(type, (listeners.get(type) || []).filter(item => item !== fn)) },
      dispatchEvent(event) {
        for (const listener of listeners.get(event.type) || []) listener(event)
        const handler = this.props[`on${event.type[0].toUpperCase()}${event.type.slice(1)}`]
        for (const listener of Array.isArray(handler) ? handler : handler ? [handler] : []) listener(event)
      },
    })
  }
  const remove = child => { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null }
  const insert = (child, parent, anchor = null) => { remove(child); const index = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent }
  const renderer = Vue.createRenderer({
    createElement: kind => node(kind), createText: value => node('#text', value), createComment: value => node('#comment', value), insert, remove,
    parentNode: child => child.parent, nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
    patchProp(element, key, previous, value) { element.props[key] = value; if (key === 'type') element.type = value },
    setText: (element, value) => { element.text = value }, setElementText: (element, value) => { element.text = value; element.children = [] }, setScopeId() {},
    insertStaticContent(value, parent, anchor) { const element = node('#static', value); insert(element, parent, anchor); return [element, element] },
  })
  const props = Vue.reactive({ checks: checks(), disabled: false, ...initial }), events = [], warnings = []
  const handlers = Object.fromEntries(['Add', 'AddBatch', 'Update', 'State', 'Remove', 'ExportReport'].map(name => [`on${name}`, (...args) => events.push([name, ...args])]))
  const root = node('root'), app = renderer.createApp({ render: () => Vue.h(Subject, { ...props, ...handlers }) })
  app.config.warnHandler = message => warnings.push(message)
  const originalGlobals = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot }
  globalThis.document = { activeElement: null }
  globalThis.Document = class FixtureDocument {}
  globalThis.ShadowRoot = class FixtureShadowRoot {}
  app.mount(root)
  t.after(() => { app.unmount(); for (const [key, value] of Object.entries(originalGlobals)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value } assert.deepEqual(warnings, []) })
  await settle()
  return { root, props, events }
}

test('the rendered component filters by state and note search and emits state or export actions', async t => {
  const view = await setup(t)
  const filters = label(view.root, '筛选验收项')
  click(buttons(filters).find(node => text(node).startsWith('失败')))
  await settle()
  assert.equal(all(view.root).filter(node => node.type === 'article').length, 1)
  assert.ok(label(view.root, '验收项标题：手机布局'))
  click(buttons(filters).find(node => text(node).startsWith('全部')))
  type(label(view.root, '搜索验收项'), 'route'); await settle()
  assert.ok(label(view.root, '验收项标题：打开导航'))
  assert.equal(all(view.root).filter(node => node.type === 'article').length, 1)
  const states = label(view.root, '打开导航的验收状态')
  click(buttons(states).find(node => text(node) === '失败'))
  assert.deepEqual(view.events.at(-1), ['State', { id: 'two', state: 'failed' }])
  click(label(view.root, '导出 Markdown 验收报告'))
  assert.deepEqual(view.events.at(-1), ['ExportReport'])
})

test('rendered editing emits bounded single-field changes and leaves props and native input undo intact', async t => {
  const frozen = Object.freeze(checks().map(check => Object.freeze(check)))
  const view = await setup(t, { checks: frozen })
  const title = label(view.root, '验收项标题：提交表单')
  type(title, ' 新标题 ')
  let prevented = 0, stopped = 0
  title.props.onKeydown({ key: 'Enter', preventDefault: () => prevented++, stopPropagation: () => stopped++ })
  assert.deepEqual(view.events.at(-1), ['Update', { id: 'one', title: '新标题' }])
  await settle()
  const note = label(view.root, '提交表单的验收备注')
  type(note, '备注'.repeat(400))
  note.props.onBlur()
  assert.equal(view.events.at(-1)[1].note.length, 600)
  note.props.onKeydown({ key: 'z', ctrlKey: true, preventDefault: () => prevented++, stopPropagation: () => stopped++ })
  assert.equal(prevented, 1, 'input undo never prevents its browser default')
  assert.equal(stopped, 2)
  click(label(view.root, '删除验收项：提交表单'))
  assert.deepEqual(view.events.at(-1), ['Remove', 'one'])
  assert.equal(frozen[0].title, '提交表单')
  assert.equal(frozen[0].note, '必填项提示')
})

test('empty titles revert and continuing after Enter is not replaced by an older parent acknowledgement', async t => {
  const view = await setup(t)
  const title = label(view.root, '验收项标题：提交表单')
  type(title, '   '); title.props.onBlur(); await settle()
  assert.equal(title.value, '提交表单')
  assert.equal(view.events.length, 0)
  type(title, '新标题')
  title.props.onKeydown({ key: 'Enter', preventDefault() {}, stopPropagation() {} })
  type(title, '继续输入')
  view.props.checks = view.props.checks.map(check => check.id === 'one' ? { ...check, title: '新标题' } : check)
  await settle()
  assert.equal(title.value, '继续输入')
  title.props.onBlur()
  assert.deepEqual(view.events.at(-1), ['Update', { id: 'one', title: '继续输入' }])
  type(title, '标'.repeat(300)); title.props.onBlur()
  assert.equal(view.events.at(-1)[1].title.length, 240)
})

test('single and template additions enforce capacity and disabled mode cannot mutate records', async t => {
  const view = await setup(t, { checks: [] })
  const input = label(view.root, '新增验收项')
  type(input, 'X'.repeat(300))
  find(view.root, node => node.type === 'form').props.onSubmit({ preventDefault() {} })
  assert.deepEqual(view.events.at(-1), ['Add', 'X'.repeat(240)])
  find(view.root, node => node.type === 'form').props.onSubmit({ preventDefault() {} })
  assert.equal(view.events.length, 1, 'a second submit in the same render tick cannot overfill capacity')
  await settle()
  const nearlyFull = Array.from({ length: 99 }, (_, i) => ({ id: String(i), title: `验收${i}`, state: 'pending', note: '' }))
  view.props.checks = nearlyFull; await settle()
  click(buttons(view.root).find(node => text(node).startsWith('交互')))
  assert.equal(view.events.at(-1)[0], 'AddBatch')
  assert.equal(view.events.at(-1)[1].length, 1)
  view.props.checks = [...nearlyFull, { id: 'last', title: '最后一项', state: 'pending', note: '' }]
  await settle()
  assert.equal(label(view.root, '添加验收项').props.disabled, true)
  const before = view.events.length
  click(buttons(view.root).find(node => text(node).startsWith('表单')))
  assert.equal(view.events.length, before)
  view.props.disabled = true; await settle()
  click(label(view.root, '删除验收项：验收0'))
  click(label(view.root, '导出 Markdown 验收报告'))
  assert.equal(view.events.length, before)
  assert.equal(label(view.root, '验收项标题：验收0').props.disabled, true)
})
