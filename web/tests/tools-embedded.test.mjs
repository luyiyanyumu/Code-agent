import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'

const source = await readFile(new URL('../src/views/ToolsView.vue', import.meta.url), 'utf8')
const descriptor = parse(source).descriptor
const compiled = compileScript(descriptor, { id: 'embedded-tools-test', inlineTemplate: true }).content
const code = compiled.replace(/^import\s+\{([^}]+)\}\s+from\s+['"]vue['"];?\r?\n/gm, (_, names) =>
  `const { ${names.replace(/\b(\w+)\s+as\s+(\w+)/g, '$1: $2')} } = vue;\n`)
  .replace(/^import[^\n]+\r?\n/gm, '')
  .replace('export default', 'return')
const PageHeader = { props: ['title'], setup: props => () => Vue.h('h1', props.title) }
const UiIcon = { setup: () => () => Vue.h('span') }
const EmptyState = { setup: () => () => Vue.h('span') }
const component = new Function('vue', 'useDialog', 'PageHeader', 'UiIcon', 'EmptyState', code)(Vue, () => ({}), PageHeader, UiIcon, EmptyState)

function setup(initialTab = 'builtin') {
  let exposed
  const context = vm.createContext({
    computed: Vue.computed, ref: Vue.ref, onMounted() {}, useDialog: () => ({}),
    defineProps: () => ({ embedded: true, initialTab }), defineExpose: value => { exposed = value },
  })
  const script = descriptor.scriptSetup.content.replace(/^import .*\r?\n/gm, '')
  vm.runInContext(`${script}\n;globalThis.subject = {filter,catalogQuery,expanded,pluginFile,pluginOverwrite,mcpRows,skillForm,pluginModal,mcpModal}`, context)
  return { s: context.subject, exposed }
}

test('embedded tool settings render every category and header action without a duplicate page heading', async () => {
  const embedded = await renderToString(Vue.createSSRApp(component, { embedded: true }))
  const standalone = await renderToString(Vue.createSSRApp(component))
  assert.doesNotMatch(embedded, /<h1/)
  assert.match(standalone, /<h1\b[^>]*>工具与扩展<\/h1>/)
  assert.match(embedded, /tools-embedded/)
  for (const label of ['内置工具', 'MCP', '插件工具', '已禁用', '技能', '专家', '制作规范', '重新扫描', '上传插件', '新建技能']) {
    assert.ok(embedded.includes(label), label)
  }
})

test('settings search can open all categories while preserving open editor drafts', () => {
  const { s, exposed } = setup('mcp')
  assert.equal(s.filter.value, 'mcp')
  s.pluginFile.value = { name: 'draft.zip' }
  s.pluginOverwrite.value = true
  s.pluginModal.value = true
  s.mcpModal.value = true
  s.mcpRows.value = [{ name: 'draft-server', command: 'node', argsText: 'draft.js' }]
  s.skillForm.value = { name: 'draft-skill', description: '尚未保存', content: 'draft', resources: [] }
  const drafts = [s.pluginFile.value, s.mcpRows.value, s.skillForm.value]
  for (const category of ['builtin', 'mcp', 'plugin', 'off', 'skill', 'expert']) {
    s.catalogQuery.value = 'previous search'
    s.expanded.value = 'previous tool'
    assert.equal(exposed.openCategory(category), true)
    assert.equal(s.filter.value, category)
    assert.equal(s.catalogQuery.value, '')
    assert.equal(s.expanded.value, '')
    assert.equal(s.pluginFile.value, drafts[0])
    assert.equal(s.mcpRows.value, drafts[1])
    assert.equal(s.skillForm.value, drafts[2])
    assert.equal(s.pluginOverwrite.value, true)
    assert.equal(s.pluginModal.value, true)
    assert.equal(s.mcpModal.value, true)
  }
})

test('unknown category requests cannot clear a current search or select an absent panel', () => {
  const { s, exposed } = setup('unknown')
  assert.equal(s.filter.value, 'builtin')
  s.catalogQuery.value = 'keep search'
  s.expanded.value = 'keep expanded'
  assert.equal(exposed.openCategory('unknown'), false)
  assert.equal(s.filter.value, 'builtin')
  assert.equal(s.catalogQuery.value, 'keep search')
  assert.equal(s.expanded.value, 'keep expanded')
})
