import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, ref } from 'vue'
import { createDialogService } from '../src/composables/useDialog.js'

const clone = value => JSON.parse(JSON.stringify(value))
function fixture() {
  return { scopes: [
    { id: 'user', kind: 'user', title: '个人记忆', revision: 2, records: [
      { id: 'u1', content: '优先给结论', enabled: true, pinned: false, category: 'preference', source: 'manual', updated_at: '2026-10-11T12:00:00Z' },
      { id: 'u2', content: '中文回复\n必要时解释术语', enabled: true, pinned: true, category: 'preference', source: 'agent', updated_at: '2026-10-10T12:00:00Z' },
    ] },
    { id: 'project:p1', kind: 'project', title: '学习平台', project_id: 'p1', revision: 3, records: [
      { id: 'p1', content: '统一使用 Java 21', enabled: false, pinned: false, category: 'convention', source: 'manual', updated_at: '2026-10-12T12:00:00Z' },
    ] },
    { id: 'session:s1', kind: 'session', title: '登录调试', session_id: 's1', revision: 0, records: [] },
  ], user: { entries: ['优先给结论', '中文回复\n必要时解释术语'] }, sessions: [] }
}
async function subject(overrides = {}) {
  let current = fixture()
  const calls = []
  const api = {
    memory: async () => clone(current),
    addMemory: async payload => { calls.push(['add', clone(payload)]) },
    updateMemory: async (...args) => { calls.push(['patch', ...clone(args)]) },
    deleteMemoryRecord: async (...args) => { calls.push(['delete', ...args]) },
    ...overrides,
  }
  const source = (await readFile(new URL('../src/views/MemoryView.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const dialog = createDialogService()
  const context = vm.createContext({ computed, ref, api, onMounted() {}, defineExpose() {}, useDialog: () => dialog })
  vm.runInContext(`${source}\n;globalThis.subject = { data, records, scopes, query, scopeFilter, statusFilter, visibleGroups, total, enabledCount, pinnedCount, matchingCount, newFact, draftOpen, draftScope, draftCategory, draftPinned, editing, busy, loading, loaded, error, notice, canSave, draftStale, latestEditRecord, load, reload, openAdd, startEdit, resetDraft, saveDraft, updateRecord, removeRecord, reviewLatest, cancelDraft, clearFilters }`, context)
  const s = context.subject
  await s.load()
  return { s, calls, dialog, setData(value) { current = value } }
}

test('scope, state and keyword filters compose and pinned records sort before newer unpinned records', async () => {
  const { s } = await subject()
  assert.equal(s.total.value, 3)
  assert.equal(s.enabledCount.value, 2)
  assert.equal(s.pinnedCount.value, 1)
  assert.deepEqual(Array.from(s.visibleGroups.value[0].records, record => record.id), ['u2', 'u1'])
  s.scopeFilter.value = 'project'; s.statusFilter.value = 'disabled'; s.query.value = '编码约定'
  assert.equal(s.matchingCount.value, 1)
  assert.equal(s.visibleGroups.value[0].records[0].content, '统一使用 Java 21')
  s.query.value = '学习平台'; assert.equal(s.matchingCount.value, 1)
  s.statusFilter.value = 'enabled'; assert.equal(s.matchingCount.value, 0)
  s.clearFilters(); assert.equal(s.matchingCount.value, 3)
})

test('a multiline draft saves to the chosen project with scope revision and metadata', async () => {
  const { s, calls } = await subject()
  s.openAdd('project:p1'); s.newFact.value = '  禁止覆盖用户改动\n提交前运行构建  '
  s.draftCategory.value = 'convention'; s.draftPinned.value = true
  await s.saveDraft()
  assert.deepEqual(calls, [['add', { scope: 'project', scope_id: 'project:p1', project_id: 'p1', content: '禁止覆盖用户改动\n提交前运行构建', category: 'convention', pinned: true, revision: 3 }]])
  assert.equal(s.newFact.value, ''); assert.equal(s.draftOpen.value, false)
})

test('adding a legacy session memory uses workspace scope instead of silently falling back to personal storage', async () => {
  const saved = { personal: [], session: [] }
  const { s } = await subject({
    memory: async () => ({ user: { entries: [] }, sessions: [{ session_id: 'old-session', title: '旧版任务', entries: ['已有会话背景'] }] }),
    addMemory: async payload => {
      // Reproduce the older backend: unsupported scopes default to personal.
      if (payload.scope === 'workspace' && payload.session_id === 'old-session') saved.session.push(payload.content)
      else saved.personal.push(payload.content)
    },
  })
  s.openAdd('session:old-session'); s.newFact.value = '仅此会话使用的任务背景'
  await s.saveDraft()
  assert.deepEqual(saved.session, ['仅此会话使用的任务背景'])
  assert.deepEqual(saved.personal, [])
})

test('memory draft refresh preserves edits and requires explicit review before using a newer scope version', async () => {
  const { s, calls, dialog, setData } = await subject()
  s.startEdit(s.records.value[0]); s.newFact.value = '我的编辑草稿'
  const next = fixture(); next.scopes[0].revision = 4; next.scopes[0].records[0].content = '另一操作保存的新内容'
  setData(next); await s.reload()
  assert.equal(s.newFact.value, '我的编辑草稿'); assert.equal(s.draftStale.value, true); assert.equal(s.canSave.value, false)
  await s.saveDraft(); assert.equal(calls.length, 0)
  const review = s.reviewLatest()
  assert.match(dialog.active.value.options.details, /另一操作保存的新内容/)
  assert.match(dialog.active.value.options.details, /我的编辑草稿/)
  dialog.cancel(); await review
  assert.equal(s.canSave.value, false)
  const accepted = s.reviewLatest(); await dialog.submit(); await accepted
  assert.equal(s.canSave.value, true)
  await s.saveDraft()
  assert.equal(calls[0][1], 'u1'); assert.equal(calls[0][2].revision, 4); assert.equal(calls[0][2].content, '我的编辑草稿')
})

test('a rejected edit keeps its draft and a deleted target cannot be silently recreated', async () => {
  const { s, setData } = await subject({ updateMemory: async () => { throw Object.assign(new Error('stale'), { status: 409 }) } })
  s.startEdit(s.records.value[0]); s.newFact.value = '保留草稿'
  await s.saveDraft()
  assert.equal(s.newFact.value, '保留草稿'); assert.equal(s.draftOpen.value, true); assert.equal(s.busy.value, '')
  assert.match(s.error.value, /已保留你的草稿/)
  const next = fixture(); next.scopes[0].records = []; next.scopes[0].revision = 6
  setData(next); await s.reload()
  assert.equal(s.latestEditRecord.value, undefined); assert.equal(s.canSave.value, false)
})

test('enable and pin operations use record identity and scope revision without editing content', async () => {
  const { s, calls } = await subject()
  await s.updateRecord(s.records.value[2], { enabled: true })
  await s.updateRecord(s.records.value[0], { pinned: true })
  assert.deepEqual(calls, [
    ['patch', 'p1', { scope: 'project', scope_id: 'project:p1', project_id: 'p1', revision: 3, enabled: true }],
    ['patch', 'u1', { scope: 'user', scope_id: 'user', revision: 2, pinned: true }],
  ])
})

test('record deletion is cancelable and keeps the captured ID and version when the list changes', async () => {
  const { s, calls, dialog } = await subject()
  const pending = s.removeRecord(s.records.value[0]); dialog.cancel(); await pending
  assert.deepEqual(calls, [])
  const removal = s.removeRecord(s.records.value[0])
  s.data.value = fixture(); s.data.value.scopes[0].revision = 99; s.data.value.scopes[0].records[0].id = 'replacement'
  await dialog.submit(); await removal
  assert.deepEqual(calls, [['delete', 'user', 'u1', 2]])
})

test('length validation and draft switching guard avoid silent data loss', async () => {
  const { s, dialog } = await subject()
  s.openAdd(); s.newFact.value = 'x'.repeat(1001)
  assert.equal(s.canSave.value, false)
  s.startEdit(s.records.value[0]); assert.equal(s.editing.value, null); assert.equal(s.newFact.value.length, 1001)
  const pending = s.cancelDraft(); dialog.cancel(); await pending
  assert.equal(s.newFact.value.length, 1001)
  const confirmed = s.cancelDraft(); await dialog.submit(); await confirmed
  assert.equal(s.newFact.value, ''); assert.equal(s.draftOpen.value, false)
})

test('failed reads retain loaded records and unsaved drafts for a later retry', async () => {
  let fail = false
  const { s } = await subject({ memory: async () => { if (fail) throw new Error('读取服务暂不可用'); return fixture() } })
  s.openAdd(); s.newFact.value = '重试后继续编辑'
  fail = true; await s.reload()
  assert.equal(s.total.value, 3); assert.equal(s.newFact.value, '重试后继续编辑')
  assert.equal(s.loading.value, false); assert.equal(s.loaded.value, true)
  assert.equal(s.error.value, '读取服务暂不可用')
  fail = false; await s.reload(); assert.equal(s.error.value, '')
})
