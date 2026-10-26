import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { workItemActive } from '../src/lib/workItems.js'

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = async () => { await nextTick(); for (let n = 0; n < 10; n++) await Promise.resolve() }
async function setup(t, name = 'TaskPanel', overrides = {}) {
  const props = reactive({ projectId: 'p1', selectedId: 'one', detailOnly: true }), calls = [], events = [], cleanup = []
  const scope = effectScope()
  const service = {
    task: async (pid, id) => ({ id, title: pid, status: 'review', plan: {}, review: { changed_files: 1 } }),
    batch: async (pid, id) => ({ id, title: pid, status: 'review', plan: {}, lanes: [], progress: {} }),
    tasks: () => { throw new Error('Detail views must not read the old task list') },
    batches: () => { throw new Error('Detail views must not read the old parallel list') },
    verifyTask: async () => ({ ok: true, results: [] }),
    approveTask: async () => ({ id: 'one', status: 'review' }),
    updateTaskPlan: async () => {}, updateBatchPlan: async () => {},
    ...overrides,
  }
  const api = { code: new Proxy(service, { get: (target, key) => (...args) => { calls.push([key, ...args]); return target[key](...args) } }) }
  const file = await readFile(new URL(`../src/components/code/${name}.vue`, import.meta.url), 'utf8')
  const source = file.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const context = vm.createContext({ computed, ref, watch: (...args) => scope.run(() => watch(...args)), onBeforeUnmount: fn => cleanup.push(fn), defineProps: () => props,
    defineEmits: () => (...args) => events.push(args), defineExpose() {}, api, workItemActive, useDialog: () => ({ open() {} }) })
  const exports = name === 'TaskPanel' ? 'selected,busy,note,canMerge,canApprove,workspaceAvailable,executionActive,verify,approve,planDraft,planError,savePlan,load' : 'selected,busy,note,canMerge,canApprove,executionActive,verifyLane,planDraft,planError,savePlan,load'
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject={${exports}}`, context))
  await settle()
  const dispose = () => { cleanup.forEach(fn => fn()); scope.stop() }
  t.after(dispose)
  return { s: context.subject, props, calls, events, dispose }
}

test('unified detail views fetch only the requested task or batch rather than a second independent list', async t => {
  const task = await setup(t), batch = await setup(t, 'ParallelPanel')
  assert.deepEqual(task.calls.map(call => call[0]), ['task'])
  assert.deepEqual(batch.calls.map(call => call[0]), ['batch'])
})
test('task completion cannot enable acceptance while a recursive child is still running', async t => {
  const f = await setup(t)
  f.s.selected.value = { id: 'one', status: 'review', active: true, agent: { state: 'completed' }, agents: [{ state: 'running' }] }
  assert.equal(f.s.executionActive.value, true); assert.equal(f.s.canMerge.value, false)
  f.s.selected.value.active = false; f.s.selected.value.agents = []
  assert.equal(f.s.executionActive.value, false); assert.equal(f.s.canMerge.value, true)
})
test('parallel integration waits for every active recursive lane worker, while deferred lanes allow predecessors to merge', async t => {
  const f = await setup(t, 'ParallelPanel')
  f.s.selected.value = { id: 'one', status: 'review', lanes: [{ status: 'review', active: true, agents: [{ state: 'queued' }] }] }
  assert.equal(f.s.executionActive.value, true); assert.equal(f.s.canMerge.value, false)
  f.s.selected.value.lanes = [{ status: 'review', agent: { state: 'completed' } }, { status: 'deferred' }]
  assert.equal(f.s.executionActive.value, false); assert.equal(f.s.canMerge.value, true)
})
test('opening a cleaned-up task retains its historical review without switching into the removed tree', async t => {
  const f = await setup(t, 'TaskPanel', { task: async (_pid, id) => ({ id, status: 'merged', workspace_available: false, review: { changed_files: 1 } }) })
  assert.equal(f.s.workspaceAvailable.value, false)
  assert.equal(f.events.some(event => event[0] === 'open-tree'), false)
})
test('late verification results from an old project cannot update the new selection or emit completion', async t => {
  const pending = deferred(), f = await setup(t, 'TaskPanel', { verifyTask: () => pending.promise })
  f.events.length = 0
  const result = f.s.verify()
  f.props.projectId = 'p2'; await settle()
  pending.resolve({ ok: true, results: [{ command: 'old command' }] }); await result
  assert.equal(f.s.selected.value.title, 'p2'); assert.equal(f.s.note.value, '')
  assert.equal(f.events.some(event => event[0] === 'changed'), false)
})
test('late approval failure after unmount does not replace the current project error banner', async t => {
  const pending = deferred(), f = await setup(t, 'TaskPanel', { approveTask: () => pending.promise })
  f.events.length = 0
  const result = f.s.approve(); f.dispose()
  pending.reject(new Error('old task failure')); await result
  assert.equal(f.events.some(event => event[0] === 'error'), false)
})
test('plan editing validates JSON objects and preserves the captured project and task identity', async t => {
  const f = await setup(t)
  f.s.planDraft.value = '[]'; await f.s.savePlan()
  assert.equal(f.s.planError.value, '计划需要是 JSON 对象。')
  assert.equal(f.calls.some(call => call[0] === 'updateTaskPlan'), false)
  f.s.planDraft.value = '{"goal":"updated","subtasks":[]}'
  await f.s.savePlan()
  const update = f.calls.find(call => call[0] === 'updateTaskPlan')
  assert.equal(update[1], 'p1'); assert.equal(update[2], 'one'); assert.equal(update[3].goal, 'updated')
})
