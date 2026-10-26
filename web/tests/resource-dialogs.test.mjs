import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, ref } from 'vue'
import { createDialogService } from '../src/composables/useDialog.js'

async function setup(name, api, expose) {
  const source = (await readFile(new URL(`../src/views/${name}.vue`, import.meta.url), 'utf8'))
    .match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const dialog = createDialogService()
  const context = vm.createContext({
    computed, ref, api, onMounted() {}, defineProps: () => ({ user: { role: 'admin' } }), defineExpose() {},
    useDialog: () => dialog,
    window: { confirm: () => { throw new Error('Native dialog is forbidden') } },
  })
  vm.runInContext(`${source}\n;globalThis.subject = {${expose}}`, context)
  return { s: context.subject, dialog }
}

test('personal and session memory deletion require confirmation and cancellation makes no API mutation', async () => {
  const mutations = []
  const memory = { user: { entries: ['个人偏好'] }, sessions: [{ session_id: 's1', title: '项目 A', entries: ['会话背景'] }] }
  const { s, dialog } = await setup('MemoryView', { memory: async () => memory, deleteMemory: async (...args) => mutations.push(args) }, 'data, load, removeUserFact, removeSessionFact')
  await s.load()
  for (const start of [() => s.removeUserFact(0), () => s.removeSessionFact('s1', 0)]) {
    const pending = start()
    assert.equal(dialog.active.value.options.tone, 'danger')
    dialog.cancel()
    await pending
  }
  assert.deepEqual(mutations, [])
  const removal = s.removeSessionFact('s1', 0)
  assert.match(dialog.active.value.options.description, /项目 A/)
  assert.equal(dialog.active.value.options.details, '会话背景')
  await dialog.submit()
  await removal
  assert.deepEqual(mutations, [['workspace', 0, 's1']])
})

test('memory deletion rejects a stale index instead of removing its replacement', async () => {
  const mutations = []
  let memory = { user: { entries: ['原条目'] }, sessions: [] }
  const { s, dialog } = await setup('MemoryView', { memory: async () => memory, deleteMemory: async (...args) => mutations.push(args) }, 'data, load, removeUserFact')
  await s.load()
  const removal = s.removeUserFact(0)
  memory = { user: { entries: ['另一条目'] }, sessions: [] }
  assert.equal(await dialog.submit(), false)
  assert.match(dialog.active.value.error, /记忆列表已变化/)
  assert.deepEqual(mutations, [])
  assert.equal(s.data.value.user.entries[0], '另一条目')
  dialog.cancel()
  await removal
})

test('knowledge deletion retains the captured document and keeps failed submissions retryable', async () => {
  const mutations = []
  let fail = true
  const { s, dialog } = await setup('KnowledgeView', {
    knowledgeDelete: async (...args) => { mutations.push(args); if (fail) throw new Error('文档正在使用') },
    knowledge: async () => [], knowledgeStats: async () => ({}),
  }, 'removeDoc, busy')
  const doc = { id: 'doc-a', title: '方案 A', origin: 'llm' }
  const removal = s.removeDoc(doc)
  Object.assign(doc, { id: 'doc-b', title: '方案 B', origin: 'human' })
  assert.match(dialog.active.value.options.details, /方案 A/)
  assert.equal(await dialog.submit(), false)
  assert.equal(dialog.active.value.error, '文档正在使用')
  assert.equal(s.busy.value, '')
  fail = false
  assert.equal(await dialog.submit(), true)
  await removal
  assert.deepEqual(mutations, [['doc-a', 'llm'], ['doc-a', 'llm']])
})

test('pack, connector and plugin delete callbacks keep the original target while waiting', async () => {
  const mutations = []
  const catalog = { tools: [], can_edit: true }
  const { s, dialog } = await setup('ToolsView', {
    tools: async () => catalog,
    deletePack: async (...args) => { mutations.push(['pack', ...args]); return { moved_to: '.trash/item' } },
    deleteMcp: async name => mutations.push(['mcp', name]),
    deletePlugin: async name => mutations.push(['plugin', name]),
  }, 'removePack, removeMcp, removePlugin')
  for (const [method, value] of [['removePack', { name: 'plan', kind: 'skill' }], ['removeMcp', { name: 'files' }], ['removePlugin', { name: 'reports' }]]) {
    const removal = s[method](value)
    value.name = 'replacement'
    value.kind = 'expert'
    assert.equal(await dialog.submit(), true)
    await removal
  }
  assert.deepEqual(mutations, [['pack', 'skill', 'plan'], ['mcp', 'files'], ['plugin', 'reports']])
})

test('bulk disabling is cancelable and API errors remain in the confirmation', async () => {
  const mutations = []
  const { s, dialog } = await setup('ToolsView', {
    setToolsEnabled: async value => { mutations.push(value); throw new Error('权限不足') },
  }, 'catalog, setAllTools, busy')
  s.catalog.value = { can_edit: true, tools: [{ name: 'one' }] }
  const cancelled = s.setAllTools(false)
  assert.equal(dialog.active.value.options.tone, 'warning')
  dialog.cancel()
  await cancelled
  assert.deepEqual(mutations, [])
  const disabling = s.setAllTools(false)
  assert.equal(await dialog.submit(), false)
  assert.equal(dialog.active.value.error, '权限不足')
  assert.equal(s.busy.value, '')
  assert.deepEqual(mutations, [false])
  dialog.cancel()
  await disabling
})
