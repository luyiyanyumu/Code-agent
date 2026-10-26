import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { INSPECTOR_GROUPS, normalizeInspectorInput, stepInspectorInput, hexColorForPicker } from '../src/lib/designInspectorFields.js'
import { STYLE_FIELDS, normalizeStyleDeclarations } from '../src/lib/frontendDesign.js'

async function component(t, name, initial, exports) {
  const source = await readFile(new URL(`../src/components/code/${name}.vue`, import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: name })
  const template = compileTemplate({ source: descriptor.template.content, filename: `${name}.vue`, id: name, compilerOptions: { bindingMetadata: compiled.bindings } })
  assert.deepEqual(template.errors, [], 'production template compiles')
  const props = Vue.reactive(initial), events = [], cleanup = [], mounted = []
  const bindings = { computed: Vue.computed, reactive: Vue.reactive, watch: Vue.watch, ref: Vue.ref, nextTick: Vue.nextTick, INSPECTOR_GROUPS, normalizeInspectorInput, stepInspectorInput, hexColorForPicker,
    useId: () => 'inspector-fixture', defineProps: () => props, defineEmits: () => (...args) => events.push(args),
    onBeforeUnmount: fn => cleanup.push(fn), onMounted: fn => mounted.push(fn),
  }
  const script = descriptor.scriptSetup.content.replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"]\s*;?\r?$/gm, '')
  const scope = Vue.effectScope()
  const state = scope.run(() => new Function(...Object.keys(bindings), `${script}\nreturn {${exports}}`)(...Object.values(bindings)))
  t.after(() => { cleanup.forEach(fn => fn()); scope.stop() })
  return { state, props, events, mount: () => mounted.forEach(fn => fn()) }
}

test('inspector groups cover every supported CSS field exactly once and their enum options are valid', () => {
  const fields = INSPECTOR_GROUPS.flatMap(group => group.fields)
  assert.equal(fields.length, 21)
  assert.deepEqual(fields.map(field => field.property).sort(), STYLE_FIELDS.map(field => field.property).sort())
  assert.equal(new Set(fields.map(field => field.property)).size, 21)
  assert.deepEqual(INSPECTOR_GROUPS.find(group => group.id === 'position').fields.map(field => field.property), ['translate'])
  assert.equal(fields.find(field => field.property === 'translate').placeholder, 'Xpx Ypx')
  for (const field of fields) for (const option of field.options) {
    assert.equal(normalizeStyleDeclarations({ [field.property]: option.value })[field.property], option.value)
    assert.ok(option.label)
  }
})

test('numeric dimensions acquire px while complex and unitless CSS values remain intact', () => {
  for (const property of ['width', 'height', 'padding', 'margin', 'gap', 'border-radius', 'font-size']) {
    assert.equal(normalizeInspectorInput(property, ' 12.5 '), '12.5px')
    assert.equal(normalizeInspectorInput(property, 0), '0px')
    for (const value of ['calc(100% - 2rem)', '1.5rem', '25%', 'auto', '8px 16px']) assert.equal(normalizeInspectorInput(property, value), value)
  }
  assert.equal(normalizeInspectorInput('margin', '-8'), '-8px')
  assert.equal(normalizeInspectorInput('font-weight', '600'), '600')
  assert.equal(normalizeInspectorInput('line-height', '1.5'), '1.5')
  assert.equal(normalizeInspectorInput('opacity', '1.2'), '1')
  assert.equal(normalizeInspectorInput('opacity', '-0.1'), '0')
  assert.equal(normalizeInspectorInput('opacity', '.25'), '0.25')
  assert.equal(normalizeInspectorInput('opacity', 'inherit'), 'inherit')
  assert.equal(normalizeInspectorInput('width', ''), '')
  assert.equal(hexColorForPicker('#AbC'), '#aabbcc')
  assert.equal(hexColorForPicker('#12AB34'), '#12ab34')
  assert.equal(hexColorForPicker('rgb(31,45,42)'), '#1f2d2a')
  assert.equal(hexColorForPicker('rgba(31, 45, 42, 0.4)'), '#1f2d2a')
  assert.equal(hexColorForPicker('rgb(31 45 42 / 40%)'), '#1f2d2a')
  assert.equal(hexColorForPicker('rgb(100% 50% 0%)'), '#ff8000')
  assert.equal(hexColorForPicker('rgb(300, -10, 42.4)'), '#ff002a')
  assert.equal(hexColorForPicker('rgb(invalid)'), '#000000')
  assert.equal(hexColorForPicker('rgba(0, 0, 0, .5)'), '#000000')
})

test('numeric stepping preserves units, handles precision and respects property boundaries', () => {
  assert.equal(stepInspectorInput('width', '120px', 1), '121px')
  assert.equal(stepInspectorInput('width', '120', -1, { shiftKey: true }), '110px')
  assert.equal(stepInspectorInput('font-size', '1.5rem', 1, { altKey: true }), '1.6rem')
  assert.equal(stepInspectorInput('height', '45%', 1), '46%')
  assert.equal(stepInspectorInput('margin', '0px', -1), '-1px')
  assert.equal(stepInspectorInput('padding', '0px', -1), '0px')
  assert.equal(stepInspectorInput('width', '.1px', 1, { altKey: true }), '0.2px')
  assert.equal(stepInspectorInput('line-height', '1.5', 1), '1.6')
  assert.equal(stepInspectorInput('line-height', '24px', -1), '23px')
  assert.equal(stepInspectorInput('font-weight', '400', 1), '500')
  assert.equal(stepInspectorInput('font-weight', '1000', 1), '1000')
  assert.equal(stepInspectorInput('opacity', '.99', 1, { shiftKey: true }), '1')
  assert.equal(stepInspectorInput('opacity', '0', -1), '0')
  for (const value of ['auto', 'calc(100% - 2rem)', 'var(--size)', '8px 16px', '', 'Infinity', 'url(test)', '1e309']) assert.equal(stepInspectorInput('width', value, 1), null)
  for (const property of ['color', 'border', 'translate', 'display']) assert.equal(stepInspectorInput(property, '1', 1), null)
  assert.equal(stepInspectorInput('opacity', '50%', 1), null)
  assert.equal(stepInspectorInput('width', '12px', 0), null)
  assert.equal(stepInspectorInput('width', `${'9'.repeat(308)}px`, 1), null)
})

test('inspector arrow stepping commits once and protects typing, disabled state and CSS expressions', async t => {
  const { state, props, events } = await component(t, 'DesignInspector', { selected: { selector: '#title', tag: 'h2', styles: { width: '120px', height: 'calc(100% - 1rem)' } }, draft: {}, overrides: {}, disabled: false }, 'values,inputKey')
  const key = (key, extra = {}) => ({ key, preventDefault() { this.prevented = true }, stopPropagation() { this.stopped = true }, ...extra })
  const up = key('ArrowUp', { shiftKey: true })
  state.inputKey(up, 'width')
  assert.equal(up.prevented, true); assert.equal(up.stopped, true)
  assert.deepEqual(events, [['change', { property: 'width', value: '130px' }]])
  props.draft = { width: '130px' }; await Vue.nextTick()
  assert.equal(state.values.width, '130px')
  for (const event of [key('ArrowDown', { ctrlKey: true }), key('ArrowUp', { metaKey: true }), key('ArrowUp', { isComposing: true })]) {
    state.inputKey(event, 'width'); assert.equal(event.prevented, undefined)
  }
  const expression = key('ArrowDown')
  state.inputKey(expression, 'height'); assert.equal(expression.prevented, undefined)
  props.disabled = true
  state.inputKey(key('ArrowDown'), 'width')
  assert.equal(events.length, 1)
})

test('translation inputs normalize one or two signed pixel axes without opening other transform or position values', () => {
  for (const [input, expected] of [['12 -8', '12px -8px'], ['-.5 +2', '-.5px +2px'], ['12', '12px'], ['0', '0px'], ['12px -8px', '12px -8px'], ['12 -8px', '12px -8px'], ['none', 'none']]) {
    const value = normalizeInspectorInput('translate', input)
    assert.equal(value, expected)
    assert.equal(normalizeStyleDeclarations({ translate: value }).translate, value)
  }
  for (const input of ['1 2 3', '1px 2px 3px', '10%', '1rem 2rem', 'calc(1px + 2px)', 'var(--offset)', 'translate(12px, 8px)', '12px; position: fixed', 'inherit', 'initial', 'unset', 'NaN', 'Infinity']) {
    assert.deepEqual(normalizeStyleDeclarations({ translate: normalizeInspectorInput('translate', input) }), {})
  }
  assert.deepEqual(normalizeStyleDeclarations({ position: 'absolute', transform: 'translate(12px, 8px)' }), {})
})

const inspectorExports = 'values, editing, pending, blocked, dimensions, commit, resetField, onFieldInput, inputKey, enumKnown'
const inspectorProps = () => ({ selected: { selector: '#button', tag: 'button', styles: { width: '10px', color: 'rgba(2, 3, 4, .5)' }, rect: { width: 90.2, height: 30.9 } }, draft: { width: '10px', color: 'rgba(2, 3, 4, .5)' }, overrides: { width: '10px' }, disabled: false })

test('a single inspector field commits normalized changes, avoids duplicate blur and resets just that override', async t => {
  const { state, props, events } = await component(t, 'DesignInspector', inspectorProps(), inspectorExports)
  assert.equal(state.dimensions.value, '90 × 31 px')
  assert.equal(state.values.color, 'rgba(2, 3, 4, .5)', 'native color picker conversion does not rewrite the original alpha text')
  assert.equal(hexColorForPicker(state.values.color), '#020304', 'computed rgba displays its RGB swatch without discarding text alpha')
  state.values.width = '24'; state.commit('width'); state.commit('width')
  assert.deepEqual(events, [['change', { property: 'width', value: '24px' }]])
  props.draft.width = '24px'; await Vue.nextTick()
  state.values.width = 'calc(100% - 1rem)'; state.commit('width')
  assert.deepEqual(events.at(-1), ['change', { property: 'width', value: 'calc(100% - 1rem)' }])
  state.resetField('width')
  assert.deepEqual(events.at(-1), ['reset', 'width'])
  const count = events.length
  state.resetField('color')
  assert.equal(events.length, count, 'resetting an inherited field does not clear another override')
  state.values.width = ''; state.commit('width')
  assert.deepEqual(events.at(-1), ['reset', 'width'], 'clearing an overridden value requests its removal')
})

test('inspector translation commits signed axes independently from element dimensions', async t => {
  const { state, props, events } = await component(t, 'DesignInspector', inspectorProps(), inspectorExports)
  props.draft.translate = 'none'; await Vue.nextTick()
  state.values.translate = '12 -8'; state.commit('translate')
  assert.deepEqual(events.at(-1), ['change', { property: 'translate', value: '12px -8px' }])
  assert.equal(state.values.width, '10px')
  props.draft.translate = '12px -8px'; await Vue.nextTick()
  props.overrides.translate = '12px -8px'; state.resetField('translate')
  assert.deepEqual(events.at(-1), ['reset', 'translate'])
})

test('external inspector updates preserve active typing, selection changes reset it and disabled fields cannot submit', async t => {
  const { state, props, events } = await component(t, 'DesignInspector', inspectorProps(), inspectorExports)
  state.editing.add('width'); state.values.width = '31'
  props.draft.width = '25px'; await Vue.nextTick()
  assert.equal(state.values.width, '31')
  props.selected = { selector: '#other', tag: 'div', styles: {} }; props.draft = { width: '80%' }
  await Vue.nextTick()
  assert.equal(state.values.width, '80%')
  props.disabled = true
  state.commit('width', '42'); state.resetField('width')
  assert.equal(events.length, 0)
  props.disabled = false; props.selected = null; await Vue.nextTick()
  state.commit('width', '42')
  assert.equal(events.length, 0)
})

test('typing after an Enter submission remains protected from its late same-element acknowledgement', async t => {
  const { state, props, events } = await component(t, 'DesignInspector', inspectorProps(), inspectorExports)
  state.values.width = '24'; state.commit('width')
  state.values.width = '25'; state.onFieldInput('width')
  props.draft.width = '24px'; props.selected.styles.width = '24px'
  await Vue.nextTick(); await Vue.nextTick()
  assert.equal(state.values.width, '25', 'the earlier Enter reply cannot replace continued typing')
  assert.equal(state.editing.has('width'), true)
  state.commit('width')
  assert.deepEqual(events.at(-1), ['change', { property: 'width', value: '25px' }])
})

test('clearing an inherited field or a rejected submission restores the effective incoming value', async t => {
  const { state, props, events } = await component(t, 'DesignInspector', inspectorProps(), inspectorExports)
  props.overrides = {}; await Vue.nextTick()
  state.values.width = ''; state.commit('width')
  assert.equal(state.values.width, '10px')
  assert.equal(events.length, 0, 'an inherited value has no override to remove')
  state.values.width = 'not-a-size'; state.commit('width')
  assert.deepEqual(events.at(-1), ['change', { property: 'width', value: 'not-a-size' }], 'the parent can show its existing validation error')
  await Vue.nextTick(); await Vue.nextTick()
  assert.equal(state.values.width, '10px', 'a rejected change must not appear to have applied')
  props.overrides = { width: '10px' }; await Vue.nextTick()
  state.values.width = ''; state.commit('width')
  assert.deepEqual(events.at(-1), ['reset', 'width'])
})

test('text editing retains native undo and Enter submits only the current field', async t => {
  const { state, events } = await component(t, 'DesignInspector', inspectorProps(), inspectorExports)
  let prevented = 0, stopped = 0
  const key = overrides => ({ key: 'z', ctrlKey: true, metaKey: false, altKey: false, preventDefault: () => prevented++, stopPropagation: () => stopped++, ...overrides })
  state.inputKey(key({}), 'width')
  state.inputKey(key({ key: 'Z', ctrlKey: false, metaKey: true }), 'width')
  assert.equal(prevented, 0, 'native input undo is not prevented')
  assert.equal(stopped, 2, 'global canvas undo cannot consume input undo')
  state.values.width = '18'
  state.inputKey(key({ key: 'Enter', ctrlKey: false }), 'width')
  assert.equal(prevented, 1)
  assert.deepEqual(events, [['change', { property: 'width', value: '18px' }]])
})

const layersExports = 'index, query, collapsed, rows, focusedSelector, ancestorSelectors, collapseAll, expandAll, toggle, select, treeKey, setRowRef'
const layerList = () => [
  { selector: '#app', parentSelector: '', tag: 'div', name: '页面' },
  { selector: '#main', parentSelector: '#app', tag: 'main', name: '内容' },
  { selector: '#save', parentSelector: '#main', tag: 'button', name: '保存' },
  { selector: '#cancel', parentSelector: '#app', tag: 'button', name: '取消', hidden: true },
]
const layerProps = () => ({ layers: layerList(), selectedSelector: '', loading: false, truncated: false, disabled: false })

test('layer search keeps ancestors and expands matches temporarily without changing stored collapse state', async t => {
  const { state } = await component(t, 'DesignLayers', layerProps(), layersExports)
  state.collapseAll()
  assert.deepEqual(state.rows.value.map(row => row.selector), ['#app'])
  state.query.value = '保存'
  assert.deepEqual(state.rows.value.map(row => row.selector), ['#app', '#main', '#save'])
  assert.equal(state.rows.value[2].depth, 2)
  assert.equal(state.rows.value[0].expanded, true)
  state.query.value = ''
  assert.deepEqual(state.rows.value.map(row => row.selector), ['#app'])
  state.expandAll()
  assert.equal(state.rows.value.length, 4)
})

test('selecting a canvas element reveals its ancestors and layer emissions honor the disabled state', async t => {
  const { state, props, events } = await component(t, 'DesignLayers', layerProps(), layersExports)
  state.collapseAll()
  state.query.value = '取消'
  props.selectedSelector = '#save'; await Vue.nextTick()
  assert.equal(state.query.value, '', 'a canvas selection outside the filter becomes visible')
  assert.ok(state.rows.value.some(row => row.selector === '#save'))
  assert.equal(state.focusedSelector.value, '#save')
  state.select(state.rows.value.find(row => row.selector === '#cancel'))
  assert.deepEqual(events, [['select', '#cancel']])
  props.disabled = true
  state.select(state.rows.value[0]); state.collapseAll()
  assert.equal(events.length, 1)
  assert.ok(state.rows.value.some(row => row.selector === '#save'))
})

test('external canvas selection scrolls its revealed row into view without taking focus or repeating on layer refresh', async t => {
  const { state, props } = await component(t, 'DesignLayers', layerProps(), layersExports)
  const scrolls = [], focuses = []
  state.setRowRef('#save', { scrollIntoView: options => scrolls.push(options), focus: () => focuses.push('#save') })
  state.collapseAll()
  props.selectedSelector = '#save'
  await Vue.nextTick(); await Vue.nextTick()
  assert.ok(state.rows.value.some(row => row.selector === '#save'))
  assert.deepEqual(scrolls, [{ block: 'nearest' }])
  assert.deepEqual(focuses, [], 'canvas selection retains focus in the canvas')
  props.layers = layerList()
  await Vue.nextTick(); await Vue.nextTick()
  assert.equal(scrolls.length, 1, 'refreshing unchanged selection does not disturb tree scrolling')
  assert.deepEqual(focuses, [])
  const reopened = await component(t, 'DesignLayers', { ...layerProps(), selectedSelector: '#save' }, layersExports)
  reopened.state.setRowRef('#save', { scrollIntoView: options => scrolls.push(options), focus: () => focuses.push('#save') })
  reopened.mount()
  await Vue.nextTick(); await Vue.nextTick()
  assert.equal(scrolls.length, 2, 'reopening the drawer reveals its existing selection after DOM mount')
  reopened.props.layers = layerList()
  await Vue.nextTick(); await Vue.nextTick()
  assert.equal(scrolls.length, 2, 'refreshing the reopened drawer does not scroll again')
  assert.deepEqual(focuses, [])
})

test('layer keyboard supports roving focus, branch navigation and explicit selection without hijacking shortcuts', async t => {
  const { state, events } = await component(t, 'DesignLayers', layerProps(), layersExports)
  const focused = []
  for (const row of state.rows.value) state.setRowRef(row.selector, { focus: () => focused.push(row.selector), scrollIntoView() {} })
  let prevented = 0
  const press = (key, row, extra = {}) => state.treeKey({ key, preventDefault: () => prevented++, stopPropagation() {}, ...extra }, row)
  press('ArrowDown', state.rows.value[0]); await Vue.nextTick()
  assert.equal(focused.at(-1), '#main')
  press('ArrowLeft', state.rows.value.find(row => row.selector === '#main'))
  assert.equal(state.rows.value.some(row => row.selector === '#save'), false)
  press('ArrowRight', state.rows.value.find(row => row.selector === '#main'))
  assert.equal(state.rows.value.some(row => row.selector === '#save'), true)
  press('End', state.rows.value[0]); await Vue.nextTick()
  assert.equal(focused.at(-1), '#cancel')
  press('Enter', state.rows.value.find(row => row.selector === '#save'))
  assert.deepEqual(events.at(-1), ['select', '#save'])
  const count = prevented
  press('ArrowDown', state.rows.value[0], { ctrlKey: true })
  assert.equal(prevented, count)
})

test('layer indexing caps work at 800 nodes and malformed cycles or duplicate selectors cannot hang traversal', async t => {
  const { state, props } = await component(t, 'DesignLayers', layerProps(), layersExports)
  props.layers = [{ selector: '#a', parentSelector: '#b', tag: 'div' }, { selector: '#b', parentSelector: '#a', tag: 'div' }, { selector: '#a', parentSelector: '', tag: 'div' }]
  await Vue.nextTick()
  assert.equal(state.index.value.nodes.length, 2)
  assert.equal(state.rows.value.length, 2)
  state.query.value = '#b'
  assert.equal(state.rows.value.length, 2)
  state.query.value = ''
  props.layers = Array.from({ length: 805 }, (_, i) => ({ selector: `#node-${i}`, parentSelector: i ? `#node-${i - 1}` : '', tag: 'div' }))
  await Vue.nextTick()
  assert.equal(state.index.value.nodes.length, 800)
  assert.ok(state.rows.value.length < 10, 'large trees start with shallow branches instead of mounting hundreds of rows')
  state.expandAll()
  assert.equal(state.rows.value.length, 800)
  state.collapseAll()
  assert.equal(state.rows.value.length, 1)
})

test('a large tree reveals the selected branch and keeps a deliberate expansion on refresh', async t => {
  const list = Array.from({ length: 210 }, (_, i) => ({ selector: `#layer-${i}`, parentSelector: i ? `#layer-${i - 1}` : '', tag: 'div' }))
  const { state, props } = await component(t, 'DesignLayers', { ...layerProps(), layers: list, selectedSelector: '#layer-5' }, layersExports)
  assert.ok(state.rows.value.some(row => row.selector === '#layer-5'))
  assert.ok(state.rows.value.length < 10)
  state.expandAll()
  props.layers = list.map(row => ({ ...row })); await Vue.nextTick()
  assert.equal(state.rows.value.length, 210, 'background refresh never undoes an explicit expand-all')
  state.query.value = '#layer-209'
  assert.ok(state.rows.value.some(row => row.selector === '#layer-209'), 'search reaches deeply collapsed branches')
  state.query.value = ''
  props.layers = []; await Vue.nextTick()
  props.layers = list; await Vue.nextTick()
  assert.ok(state.rows.value.length < 10, 'a new page can initialize its own compact tree')
})
