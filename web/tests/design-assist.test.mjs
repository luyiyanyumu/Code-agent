import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { DESIGN_INSTRUCTION_TEMPLATES, normalizeDesignPresetName, normalizeDesignPresets, createDesignPreset } from '../src/lib/designPresets.js'

const settle = async () => { await Vue.nextTick(); await new Promise(setImmediate); await Vue.nextTick() }
const descendants = node => [node, ...node.children.flatMap(descendants)]
const text = node => node.text + node.children.map(text).join('')
const button = (root, label) => descendants(root).find(node => node.type === 'button' && text(node) === label)
const labeled = (root, label) => descendants(root).find(node => node.props['aria-label'] === label)
const proposal = () => ({ selector: '#button', summary: '增强对比并放大', declarations: { color: '#334455', width: '120px' } })
const differences = () => [{ property: 'color', before: '#999999', after: '#334455' }, { property: 'width', before: '80px', after: '120px' }]

// Compile and mount the entire production template with Vue's real renderer.
// Host nodes replace only the DOM; there is no browser, API, model or storage.
async function mount(t, initial = {}) {
  const url = new URL('../src/components/code/DesignAssist.vue', import.meta.url)
  const { descriptor, errors } = parse(await readFile(url, 'utf8'))
  assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: 'design-assist', inlineTemplate: true })
  const bindings = {}, imports = []
  let script = compiled.content.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?\r?$/gm, (_, clause, specifier) => { imports.push({ clause: clause.trim(), specifier }); return '' })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? Vue : await import(new URL(specifier, url))
    assert.ok(clause.startsWith('{'), 'only explicit local helpers and Vue are imported')
    for (const entry of clause.slice(1, -1).split(',').map(value => value.trim()).filter(Boolean)) {
      const [key, local = key] = entry.split(/\s+as\s+/)
      bindings[local] = namespace[key]
    }
  }
  script = script.replace('export default', 'return')
  const Subject = new Function(...Object.keys(bindings), script)(...Object.values(bindings))
  const node = (type, value = '') => Vue.markRaw({ type, text: value, props: {}, children: [], parent: null })
  const remove = child => { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null }
  const insert = (child, parent, anchor = null) => { remove(child); const index = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent }
  const renderer = Vue.createRenderer({
    createElement: type => node(type), createText: value => node('#text', value), createComment: value => node('#comment', value), insert, remove,
    parentNode: child => child.parent, nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] ?? null,
    patchProp: (element, key, previous, value) => { element.props[key] = value }, setText: (element, value) => { element.text = value },
    setElementText: (element, value) => { element.text = value; element.children = [] }, setScopeId() {},
    insertStaticContent(value, parent, anchor) { const element = node('#static', value); insert(element, parent, anchor); return [element, element] },
  })
  const events = [], warnings = [], props = Vue.reactive({ selected: { selector: '#button' }, instruction: '', proposal: null, diff: [], notice: '', busy: false, disabled: false, canSavePreset: false, presets: [], ...initial })
  const handlers = Object.fromEntries(['request', 'apply', 'dismiss', 'save-preset', 'apply-preset', 'remove-preset'].map(event => [`on${event[0].toUpperCase()}${event.slice(1)}`, value => events.push([event, ...(value === undefined ? [] : [value])])]))
  handlers['onUpdate:instruction'] = value => { events.push(['update:instruction', value]); props.instruction = value }
  const root = node('root'), app = renderer.createApp({ render: () => Vue.h(Subject, { ...props, ...handlers }) })
  app.config.warnHandler = message => warnings.push(message)
  app.mount(root)
  t.after(() => { app.unmount(); assert.deepEqual(warnings, []) })
  return { root, props, events }
}

test('project presets normalize names, bound input and retain only detached safe visual declarations', () => {
  const input = [
    { id: 'primary:1', name: '  主要\n  按钮 ', declarations: { color: '#3355aa', width: '12px', position: 'fixed', 'background-color': 'url(https://invalid.example)' } },
    { id: 'primary:1', name: '重复', declarations: { color: 'red' } },
    { id: 'empty', name: '空声明', declarations: { position: 'fixed' } },
    { id: 'bad/id', name: '无效标识', declarations: { color: 'red' } },
    { id: 'blank', name: ' ', declarations: { color: 'red' } },
  ]
  const snapshot = JSON.stringify(input), result = normalizeDesignPresets(input)
  assert.deepEqual(result, [{ id: 'primary:1', name: '主要 按钮', declarations: { color: '#3355aa', width: '12px' } }])
  assert.equal(JSON.stringify(input), snapshot)
  result[0].declarations.color = 'blue'
  assert.equal(input[0].declarations.color, '#3355aa')
  assert.equal(normalizeDesignPresets(Array.from({ length: 25 }, (_, i) => ({ id: `p-${i}`, name: `样式${i}`, declarations: { opacity: 0 } }))).length, 20)
  assert.equal(Array.from(normalizeDesignPresetName('😀'.repeat(50))).length, 40)
  for (const invalid of [null, {}, 'styles', 0]) assert.deepEqual(normalizeDesignPresets(invalid), [])
  assert.equal(createDesignPreset({ id: 'safe', name: '样式', declarations: { color: 'url(x)' } }), null)
  assert.deepEqual(createDesignPreset({ id: 'safe', name: ' 卡片 ', declarations: { padding: '12px' } }), { id: 'safe', name: '卡片', declarations: { padding: '12px' } })
})

test('shortcut templates fill the real textarea without requesting or applying AI automatically', async t => {
  const view = await mount(t)
  for (const template of DESIGN_INSTRUCTION_TEMPLATES) {
    button(view.root, template.label).props.onClick()
    await settle()
    assert.equal(labeled(view.root, 'AI 样式设计要求').props.value, template.instruction)
  }
  assert.equal(view.events.length, 4)
  assert.ok(view.events.every(([event]) => event === 'update:instruction'))
  labeled(view.root, 'AI 样式设计要求').props.onInput({ target: { value: '改成蓝色圆角按钮' } })
  await settle()
  button(view.root, '生成样式建议').props.onClick()
  assert.deepEqual(view.events.at(-1), ['request'])
  assert.equal(view.props.instruction, '改成蓝色圆角按钮')
})

test('candidate rows default to checked, render before/after and apply only chosen canonical proposal fields', async t => {
  const candidate = proposal(), snapshot = JSON.stringify(candidate)
  const view = await mount(t, { proposal: candidate, diff: [...differences(), { property: 'position', before: 'static', after: 'fixed' }, { property: 'color', before: 'red', after: 'malicious replacement' }] })
  assert.equal(labeled(view.root, '应用文字颜色').props.checked, true)
  assert.equal(labeled(view.root, '应用宽度').props.checked, true)
  assert.ok(text(view.root).includes('#999999'))
  assert.ok(text(view.root).includes('→ #334455'))
  assert.equal(labeled(view.root, '应用position'), undefined)
  assert.equal(text(view.root).includes('malicious replacement'), false)
  labeled(view.root, '应用宽度').props.onChange({ target: { checked: false } })
  await settle()
  button(view.root, '应用选中属性（1）').props.onClick()
  assert.deepEqual(view.events, [['apply', { color: '#334455' }]])
  assert.equal(JSON.stringify(candidate), snapshot)
  labeled(view.root, '应用文字颜色').props.onChange({ target: { checked: false } })
  await settle()
  const apply = button(view.root, '应用选中属性（0）')
  assert.equal(apply.props.disabled, true)
  apply.props.onClick()
  assert.equal(view.events.length, 1)
  button(view.root, '取消候选').props.onClick()
  assert.deepEqual(view.events.at(-1), ['dismiss'])
})

test('checkbox choices survive notices and reset for a new proposal while stale selected targets cannot apply', async t => {
  const view = await mount(t, { proposal: proposal(), diff: differences() })
  labeled(view.root, '应用宽度').props.onChange({ target: { checked: false } })
  view.props.notice = '候选已准备好'
  await settle()
  assert.equal(labeled(view.root, '应用宽度').props.checked, false)
  view.props.proposal = proposal()
  await settle()
  assert.equal(labeled(view.root, '应用宽度').props.checked, true)
  view.props.selected = { selector: '#other' }
  await settle()
  assert.equal(button(view.root, '应用选中属性（2）').props.disabled, true)
  button(view.root, '应用选中属性（2）').props.onClick()
  assert.equal(view.events.length, 0)
})

test('project style cards emit validated save/apply/remove intents without writing storage or computed styles', async t => {
  const view = await mount(t, { canSavePreset: true, presets: [{ id: 'card-1', name: '卡片', declarations: { padding: '12px', position: 'fixed' } }] })
  labeled(view.root, '本项目样式名称').props.onInput({ target: { value: '  主按钮  ' } })
  await settle()
  button(view.root, '保存覆盖').props.onClick()
  labeled(view.root, '应用样式 卡片').props.onClick()
  labeled(view.root, '删除样式 卡片').props.onClick()
  assert.deepEqual(view.events, [['save-preset', '主按钮'], ['apply-preset', 'card-1'], ['remove-preset', 'card-1']])
  assert.ok(text(view.root).includes('1 项属性'))
  view.props.presets = Array.from({ length: 20 }, (_, index) => ({ id: `p-${index}`, name: `样式${index}`, declarations: { color: 'red' } }))
  await settle()
  assert.equal(button(view.root, '保存覆盖').props.disabled, true)
  button(view.root, '保存覆盖').props.onClick()
  assert.equal(view.events.length, 3)
})

test('busy, disabled and no-selection states block requests, candidate application and preset actions', async t => {
  for (const restriction of [{ busy: true }, { disabled: true }, { selected: null }]) {
    const view = await mount(t, { instruction: '改为蓝色', proposal: proposal(), diff: differences(), presets: [{ id: 'p1', name: '按钮', declarations: { color: 'blue' } }], ...restriction })
    const request = button(view.root, restriction.busy ? '正在生成样式…' : '生成样式建议')
    assert.equal(request.props.disabled, true)
    request.props.onClick()
    button(view.root, '提高对比').props.onClick()
    labeled(view.root, 'AI 样式设计要求').props.onInput({ target: { value: '错误修改' } })
    button(view.root, '应用选中属性（2）').props.onClick()
    labeled(view.root, '应用样式 按钮').props.onClick()
    labeled(view.root, '删除样式 按钮').props.onClick()
    assert.deepEqual(view.events, [])
  }
})

test('waiting requests expose cancel and saving requires explicit current-element overrides', async t => {
  const view = await mount(t, { busy: true, instruction: '改为蓝色' })
  assert.equal(button(view.root, '取消等待').props.disabled, false)
  button(view.root, '取消等待').props.onClick()
  assert.deepEqual(view.events, [['dismiss']])
  view.props.busy = false
  await settle()
  assert.equal(button(view.root, '取消等待'), undefined)
  labeled(view.root, '本项目样式名称').props.onInput({ target: { value: '主按钮' } })
  await settle()
  assert.equal(button(view.root, '保存覆盖').props.disabled, true)
  assert.ok(text(view.root).includes('先在设计属性中修改当前元素'))
  button(view.root, '保存覆盖').props.onClick()
  assert.equal(view.events.length, 1)
  view.props.canSavePreset = true
  await settle()
  button(view.root, '保存覆盖').props.onClick()
  assert.deepEqual(view.events.at(-1), ['save-preset', '主按钮'])
})
