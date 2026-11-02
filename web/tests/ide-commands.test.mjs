import assert from 'node:assert/strict'
import test from 'node:test'
import { flattenIdeCommands, searchIdeCommands } from '../src/lib/ideCommands.js'

const menus = [
  { id: 'file', label: '文件', items: [
    { id: 'open', label: '打开项目', shortcut: 'Ctrl+O', keywords: ['folder', 'workspace'] },
    { separator: true }, { id: 'save', label: '保存文件', shortcut: 'Ctrl+S' },
    { id: 'save-all', label: '全部保存', shortcut: 'Ctrl+Shift+S' },
    { id: 'close', label: '关闭文件', disabled: true }, { id: 'secret', label: '隐藏功能', hidden: true },
  ] },
  { id: 'view', label: '视图', items: [{ id: 'sidebar', label: '文件侧栏', shortcut: 'Ctrl+B', checked: true }, { id: 'save', label: '重复保存' }] },
]

test('search and menu share enabled real commands, preserve checked state and never mutate definitions', () => {
  const before = structuredClone(menus), rows = flattenIdeCommands(menus)
  assert.deepEqual(rows.map(row => row.id), ['open', 'save', 'save-all', 'sidebar'])
  assert.equal(rows.at(-1).checked, true)
  assert.equal(rows.at(-1).menuLabel, '视图')
  assert.deepEqual(menus, before)
  assert.ok(flattenIdeCommands(menus, { includeDisabled: true }).some(row => row.id === 'close'))
  assert.equal(flattenIdeCommands(null).length, 0)
})

test('search ranks Chinese labels, matches English aliases and keyboard shortcuts, and respects each token', () => {
  assert.equal(searchIdeCommands(menus, '保存文件')[0].id, 'save')
  assert.deepEqual(searchIdeCommands(menus, '> 保存').map(row => row.id), ['save', 'save-all'])
  assert.equal(searchIdeCommands(menus, ' WORKSPACE ')[0].id, 'open')
  assert.equal(searchIdeCommands(menus, 'ctrl+shift+s')[0].id, 'save-all')
  assert.equal(searchIdeCommands(menus, '文件 保存')[0].id, 'save')
  assert.equal(searchIdeCommands(menus, '关闭').length, 0)
  assert.equal(searchIdeCommands(menus, '打开 secret-credential-value').length, 0)
})

test('result order stays stable for empty search and definitions update without stale disabled actions', () => {
  assert.deepEqual(searchIdeCommands(menus, ' ', { limit: 2 }).map(row => row.id), ['open', 'save'])
  assert.equal(searchIdeCommands(menus, '', { limit: 0 }).length, 0)
  const changed = structuredClone(menus); changed[0].items[2].disabled = true
  assert.deepEqual(searchIdeCommands(changed, '保存').map(row => row.id), ['save-all'])
  assert.deepEqual(searchIdeCommands(menus, '保存').map(row => row.id), ['save', 'save-all'])
})
