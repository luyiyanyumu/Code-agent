import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import { renderToString } from '@vue/server-renderer'
import { compileScript, parse } from '@vue/compiler-sfc'
import { buildDesignChanges } from '../src/lib/designChanges.js'
import { STYLE_FIELDS, serializeDesignCss, parseDesignCss, isManagedDesignCss } from '../src/lib/frontendDesign.js'

const savedRules = () => [
  { selector: '#button', declarations: { color: 'red', width: '100px', padding: '8px' } },
  { selector: '#removed', declarations: { opacity: '.5' } },
  { selector: '#same', declarations: { gap: '12px' } },
]
const currentRules = () => [
  { selector: '#button', declarations: { color: 'blue', height: '20px', padding: '8px' } },
  { selector: '#new', declarations: { 'background-color': 'rgba(1, 2, 3, .4)' } },
  { selector: '#same', declarations: { gap: '12px' } },
]

test('managed design diff counts added, modified and removed selectors and exact property values', () => {
  const current = currentRules(), baseline = serializeDesignCss(savedRules())
  const result = buildDesignChanges(current, baseline)
  assert.equal(result.elementCount, 3)
  assert.equal(result.propertyCount, 5)
  assert.equal(result.exportable, true)
  assert.equal(result.currentCss, serializeDesignCss(current))
  assert.deepEqual(result.changes.map(({ selector, status, hasCurrent }) => ({ selector, status, hasCurrent })), [
    { selector: '#button', status: 'modified', hasCurrent: true },
    { selector: '#new', status: 'added', hasCurrent: true },
    { selector: '#removed', status: 'removed', hasCurrent: false },
  ])
  assert.deepEqual(result.changes[0].properties, [
    { property: 'color', label: '文字颜色', before: 'red', after: 'blue', status: 'modified' },
    { property: 'width', label: '宽度', before: '100px', after: null, status: 'removed' },
    { property: 'height', label: '高度', before: null, after: '20px', status: 'added' },
  ])
  assert.equal(result.changes[1].properties[0].after, 'rgba(1, 2, 3, .4)')
  assert.deepEqual(result.changes[2].properties[0], { property: 'opacity', label: '透明度', before: '.5', after: null, status: 'removed' })
})

test('unchanged values, ordering and duplicate-selector cascade create no false differences', () => {
  const saved = [
    { selector: '#item', declarations: { color: 'red', width: '10px' } },
    { selector: '#item', declarations: { color: 'blue' } },
    { selector: '#other', declarations: { margin: 'calc(100% - 1rem)' } },
  ]
  const current = [{ selector: '#other', declarations: { margin: 'calc(100% - 1rem)' } }, { selector: '#item', declarations: { width: '10px', color: 'blue' } }]
  const result = buildDesignChanges(current, serializeDesignCss(saved))
  assert.deepEqual(result.changes, [])
  assert.equal(result.elementCount, 0)
  assert.equal(result.propertyCount, 0)
})

test('removing every rule remains an exportable managed stylesheet and reports every removed property', () => {
  const result = buildDesignChanges([], serializeDesignCss(savedRules()))
  assert.equal(result.changes.length, 3)
  assert.equal(result.propertyCount, 5)
  assert.ok(result.changes.every(change => change.status === 'removed' && !change.hasCurrent && change.properties.every(property => property.after === null)))
  assert.equal(result.exportable, true)
  assert.equal(isManagedDesignCss(result.currentCss), true)
  assert.deepEqual(parseDesignCss(result.currentCss), [])
  assert.equal(buildDesignChanges(undefined, '').currentCss, serializeDesignCss([]))
})

test('diff input uses the existing selector and property whitelist and never mutates controlled rules', () => {
  const input = [
    { selector: '#safe', declarations: { color: 'url(https://invalid.test/x)', width: '20px', position: 'fixed' } },
    { selector: '#unsafe { color:red }', declarations: { color: 'red' } },
  ]
  const original = structuredClone(input)
  input.forEach(rule => { Object.freeze(rule.declarations); Object.freeze(rule) }); Object.freeze(input)
  const result = buildDesignChanges(input, 'body { color:red; }')
  assert.deepEqual(input, original)
  assert.deepEqual(parseDesignCss(result.currentCss), [{ selector: '#safe', declarations: { width: '20px' } }])
  assert.deepEqual(result.changes[0].properties, [{ property: 'width', label: '宽度', before: null, after: '20px', status: 'added' }])
  result.changes[0].properties[0].after = '99px'
  assert.deepEqual(input, original)
})

test('oversized stylesheet input cannot be offered as a successful export', () => {
  const declarations = Object.fromEntries(STYLE_FIELDS.map(({ property }) => [property, property === 'font-family' || property === 'color' || property === 'background-color' ? 'a'.repeat(256) : '0 '.repeat(128).trim()]))
  const rules = Array.from({ length: 256 }, (_, index) => ({ selector: `#node-${index}`, declarations }))
  const result = buildDesignChanges(rules)
  assert.equal(result.exportable, false)
  assert.deepEqual(result.changes, [])
  assert.equal(isManagedDesignCss(result.currentCss), true)
})

async function fixture(initial) {
  const url = new URL('../src/components/code/DesignChanges.vue', import.meta.url)
  const source = await readFile(url, 'utf8')
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: 'design-changes-test', inlineTemplate: true })
  const bindings = {}
  let script = compiled.content.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?\r?$/gm, (_, clause, specifier) => {
    const namespace = specifier === 'vue' ? Vue : specifier.endsWith('.vue') ? { default: { render: () => Vue.h('svg') } } : { buildDesignChanges }
    if (clause.trim().startsWith('{')) {
      for (const entry of clause.trim().slice(1, -1).split(',').map(item => item.trim()).filter(Boolean)) {
        const [name, local = name] = entry.split(/\s+as\s+/)
        bindings[local] = namespace[name]
      }
    } else bindings[clause.trim()] = namespace.default
    return ''
  })
  script = script.replace('export default', 'return')
  const definition = new Function(...Object.keys(bindings), script)(...Object.values(bindings))
  const props = Vue.reactive({ rules: [], savedCss: '', disabled: false, ...initial }), events = []
  const render = definition.setup(props, { expose() {}, emit: (...args) => events.push(args) })
  const tree = () => render({}, [])
  const html = () => renderToString(Vue.createSSRApp(definition, props))
  return { props, events, tree, html, source }
}

function nodes(root, predicate) {
  const result = []
  function visit(node) {
    if (!node || typeof node !== 'object') return
    if (predicate(node)) result.push(node)
    if (Array.isArray(node.children)) node.children.forEach(visit)
  }
  visit(root)
  return result
}
function text(node) { return typeof node.children === 'string' ? node.children : Array.isArray(node.children) ? node.children.map(child => typeof child === 'string' ? child : text(child)).join('') : '' }
const button = (tree, label) => nodes(tree, node => node.type === 'button' && text(node).includes(label))[0]

test('compiled component emits only controlled actions, including select for removed rules and guarded reset', async () => {
  const view = await fixture({ rules: currentRules(), savedCss: serializeDesignCss(savedRules()) })
  let tree = view.tree()
  const selectors = nodes(tree, node => node.type === 'button' && node.props?.class === 'change-selector')
  selectors.forEach(node => node.props.onClick())
  button(tree, '重置').props.onClick()
  button(tree, '复制 CSS').props.onClick()
  button(tree, '导出 CSS').props.onClick()
  assert.deepEqual(view.events, [['select', '#button'], ['select', '#new'], ['select', '#removed'], ['reset', '#button'], ['copy-css'], ['export-css']])
  assert.equal(nodes(tree, node => node.type === 'button' && text(node).includes('重置')).length, 2, 'removed selector has no current override to reset')
  view.props.disabled = true
  tree = view.tree()
  const allButtons = nodes(tree, node => node.type === 'button')
  assert.ok(allButtons.every(node => node.props.disabled === true))
  const previous = view.events.length
  allButtons.forEach(node => node.props.onClick())
  assert.equal(view.events.length, previous, 'programmatic calls obey disabled guards too')
})

test('empty component explains the saved state and still emits zero-argument copy and export', async () => {
  const view = await fixture({ rules: [], savedCss: serializeDesignCss([]) })
  const tree = view.tree(), html = await view.html()
  assert.match(html, /没有尚未保存的改动/)
  assert.match(html, /0 个元素 · 0 项属性改动/)
  assert.equal(button(tree, '复制 CSS').props.disabled, false)
  assert.equal(button(tree, '导出 CSS').props.disabled, false)
  button(tree, '复制 CSS').props.onClick(); button(tree, '导出 CSS').props.onClick()
  assert.deepEqual(view.events, [['copy-css'], ['export-css']])
})

test('Vue renders selectors and raw CSS values as text and rejects unsafe HTML or CSS inputs', async () => {
  const rules = [
    { selector: '#card\\&details', declarations: { color: 'red', 'font-family': '"微软雅黑", sans-serif' } },
    { selector: '<img src=x onerror=alert(1)>', declarations: { color: 'red' } },
    { selector: '#bad', declarations: { width: '20px; background:url(https://invalid.test)' } },
  ]
  const view = await fixture({ rules })
  const html = await view.html()
  assert.match(html, /#card\\&amp;details/)
  assert.match(html, /&quot;微软雅黑&quot;, sans-serif/)
  assert.match(html, /文字颜色/)
  assert.equal(html.includes('<img'), false)
  assert.equal(html.includes('onerror'), false)
  assert.equal(html.includes('invalid.test'), false)
  assert.equal(/v-html|innerHTML|navigator\.clipboard|createObjectURL/.test(view.source), false)
})
