import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const settle = async () => { await nextTick(); await new Promise(setImmediate); await nextTick() }
const proposal = (id = 'r1') => ({ id, path: 'a.py', state: 'pending', counts: { total: 2, pending: 2, applied: 0, rejected: 0 }, hunks: [] })
async function setup(t, overrides = {}) {
  const props = reactive({ projectId: 'p', visible: true, refreshKey: 0, dirtyPaths: [], readonly: false, caseInsensitive: false })
  const calls = [], events = [], scope = effectScope()
  const changeReviews = { list: async () => ({ proposals: [proposal()] }), detail: async (_p, id) => proposal(id), decide: async (...args) => { calls.push(args); return { changed: args[2] !== 'reject', path: 'a.py', proposal: { ...proposal(), state: args[2] === 'rollback' ? 'reverted' : 'partial' } } }, ...overrides }
  let unmount
  const source = (await readFile(new URL('../src/components/code/ChangesPanel.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const ctx = vm.createContext({ computed, ref, watch: (...args) => scope.run(() => watch(...args)), changeReviews, defineProps: () => props, defineEmits: () => (name, payload) => events.push([name, payload]), onBeforeUnmount: fn => { unmount = fn }, setInterval: () => 1, clearInterval: () => {}, document: { visibilityState: 'visible' } })
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject = { selected, proposals, batches, selectedBatch, busy, error, load, choose, decide, chooseBatch, decideBatch }`, ctx))
  await settle(); t.after(() => { unmount(); scope.stop() })
  return { s: ctx.subject, props, calls, events }
}
test('dirty drafts and manual readonly prevent apply and rollback but allow rejecting', async t => {
  const { s, props, calls } = await setup(t)
  props.dirtyPaths = ['a.py']; await s.decide('apply'); await s.decide('rollback')
  assert.equal(calls.length, 0)
  await s.decide('reject', ['h1']); assert.equal(calls.length, 1)
  props.dirtyPaths = []; props.readonly = true; await s.decide('apply'); assert.equal(calls.length, 1)
})
test('review emits disk refresh after applying and reports server conflicts while preserving detail', async t => {
  const { s, events } = await setup(t)
  await s.decide('apply', ['h2'])
  assert.equal(events.find(([name]) => name === 'changed')[1].path, 'a.py')
  const failing = await setup(t, { decide: async () => { throw new Error('disk conflict') } })
  await failing.s.decide('apply'); assert.equal(failing.s.error.value, 'disk conflict')
  assert.equal(failing.s.selected.value.id, 'r1')
})
test('project changes invalidate delayed decisions and old proposal details', async t => {
  const pending = deferred()
  const { s, props, events } = await setup(t, { decide: () => pending.promise })
  const applying = s.decide('apply')
  props.projectId = 'q'; await settle()
  pending.resolve({ changed: true, path: 'a.py', proposal: proposal('old') }); await applying
  assert.equal(events.find(([name]) => name === 'changed')[1].projectId, 'p')
  assert.equal(s.selected.value.id, 'r1')
  assert.equal(s.busy.value, false)
})
test('newer list and detail responses win over earlier refreshes', async t => {
  const oldList = deferred(), oldDetail = deferred()
  let reads = 0
  const { s } = await setup(t, { list: () => ++reads === 1 ? oldList.promise : Promise.resolve({ proposals: [proposal('new')] }), detail: (_p, id) => id === 'old' ? oldDetail.promise : Promise.resolve(proposal(id)) })
  await s.load()
  oldList.resolve({ proposals: [proposal('old')] }); await settle()
  assert.equal(s.proposals.value[0].id, 'new')
  const choosing = s.choose(proposal('old')); await s.choose(proposal('new'))
  oldDetail.resolve(proposal('old')); await choosing
  assert.equal(s.selected.value.id, 'new')
})

const refactor = (state = 'pending') => ({ id: 'wr1', title: 'Rename class', state, paths: ['old.java', 'new.java'], file_count: 2, files: [] })
const batchServices = () => ({ listBatches: async () => ({ batches: [refactor()] }), detailBatch: async () => refactor() })

test('resource review restores persisted batches and protects every dirty endpoint', async t => {
  const decisions = []
  const { s, props } = await setup(t, { ...batchServices(), decideBatch: async (...args) => { decisions.push(args); return { changed: false, batch: refactor('rejected') } } })
  assert.equal(s.batches.value[0].id, 'wr1')
  await s.chooseBatch(refactor()); assert.equal(s.selected.value, null)
  props.caseInsensitive = true; props.dirtyPaths = ['NEW.java']; await s.decideBatch('apply'); assert.equal(decisions.length, 0)
  await s.decideBatch('reject'); assert.equal(decisions.length, 1)
  await s.chooseBatch(refactor()); props.dirtyPaths = []; props.readonly = true
  await s.decideBatch('apply'); assert.equal(decisions.length, 1)
})

test('draft identity follows workspace filesystem casing for text proposals and resource batches', async t => {
  const decisions = []
  const { s, props, calls } = await setup(t, { ...batchServices(), decideBatch: async (...args) => { decisions.push(args); return { changed: false, batch: refactor('rejected') } } })
  props.dirtyPaths = ['A.PY']; props.caseInsensitive = true
  await s.decide('apply'); assert.equal(calls.length, 0)
  props.caseInsensitive = false; await s.decide('apply'); assert.equal(calls.length, 1)
  await s.chooseBatch(refactor()); props.dirtyPaths = ['NEW.java']; props.caseInsensitive = false
  await s.decideBatch('apply'); assert.equal(decisions.length, 1)
})

test('batch apply emits every affected path and rollback is only offered for applied state', async t => {
  let current = refactor(), actions = []
  const { s, events } = await setup(t, {
    listBatches: async () => ({ batches: [current] }), detailBatch: async () => current,
    decideBatch: async (_pid, _id, action) => { actions.push(action); current = refactor(action === 'apply' ? 'applied' : 'reverted'); return { changed: true, paths: current.paths, batch: current } },
  })
  await s.chooseBatch(current)
  await s.decideBatch('rollback'); assert.deepEqual(actions, [])
  await s.decideBatch('apply'); assert.equal(s.selectedBatch.value.state, 'applied')
  assert.deepEqual(Array.from(events.find(([name]) => name === 'changed')[1].paths), ['old.java', 'new.java'])
  await s.decideBatch('rollback'); assert.deepEqual(actions, ['apply', 'rollback'])
})

test('late batch detail cannot replace a newer text proposal and late writes retain originating project', async t => {
  const delayed = deferred(), applying = deferred()
  const { s, props, events } = await setup(t, { ...batchServices(), detailBatch: (_pid, id) => id === 'old' ? delayed.promise : Promise.resolve(refactor()), decideBatch: () => applying.promise })
  const loading = s.chooseBatch({ id: 'old' }); await s.choose(proposal('new'))
  delayed.resolve(refactor()); await loading
  assert.equal(s.selected.value.id, 'new'); assert.equal(s.selectedBatch.value, null)
  await s.chooseBatch(refactor())
  const decision = s.decideBatch('apply'); props.projectId = 'q'; await settle()
  applying.resolve({ changed: true, paths: ['old.java', 'new.java'], batch: refactor('applied') }); await decision
  assert.equal(events.find(([name]) => name === 'changed')[1].projectId, 'p')
  assert.equal(s.busy.value, false)
})
