import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { createDialogService } from '../src/composables/useDialog.js'

const task = (id = 'task-a') => ({ id, title: `Task ${id}`, status: 'review', review: { changed_files: 1, gate: { violations: [] } } })
const batch = (id = 'batch-a') => ({ id, title: `Batch ${id}`, status: 'review', lanes: [{ id: 'lane-1' }] })
const git = { is_repo: true, branch: 'main', remote: 'origin', dirty: false, changes: [] }
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { resolve, promise } }

async function setup(t, name, overrides = {}) {
  const props = reactive({ projectId: 'project-a', writable: false })
  const scope = effectScope(), service = createDialogService(), owner = Symbol(), events = [], calls = [], unmount = []
  const apiCode = {
    tasks: async () => [task()], task: async (_pid, id) => task(id),
    batches: async () => [batch()], batch: async (_pid, id) => batch(id),
    git: async () => ({ ...git }), gitBranches: async () => ({ branches: [{ name: 'main' }, { name: 'feature' }] }),
    ...overrides,
  }
  for (const name of ['mergeTask', 'discardTask', 'mergeBatch', 'discardBatch', 'gitAction', 'gitCheckout']) {
    const original = apiCode[name]
    apiCode[name] = async (...args) => { calls.push([name, ...args]); return original ? original(...args) : { ok: true, files: ['main.py'], merged: ['lane-1'], ...git } }
  }
  const raw = await readFile(new URL(`../src/components/code/${name}.vue`, import.meta.url), 'utf8')
  const source = raw.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const names = name === 'TaskPanel' ? 'selected,busy,note,mergeAll,applyFile,discard' : name === 'ParallelPanel' ? 'selected,busy,note,mergeQueue,discard' : 'selected,status,busy,note,run,checkout'
  const context = vm.createContext({ computed, ref, watch: (...args) => scope.run(() => watch(...args)), onBeforeUnmount: fn => unmount.push(fn), defineProps: () => props, defineEmits: () => (...args) => events.push(args), defineExpose() {}, api: { code: apiCode }, useDialog: () => ({ open: options => service.open(options, owner) }), window: { confirm() { throw new Error('native confirm forbidden') }, prompt() { throw new Error('native prompt forbidden') } } })
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject = {${names}}`, context))
  await nextTick(); await nextTick()
  const s = context.subject
  if (name === 'TaskPanel') s.selected.value = task()
  if (name === 'ParallelPanel') s.selected.value = batch()
  const dispose = () => { unmount.forEach(fn => fn()); service.dismissOwner(owner); scope.stop() }
  t.after(dispose)
  return { s, props, service, calls, events, dispose }
}

test('task merge and deletion cancel without mutation; errors stay in the same dialog for retry', async t => {
  let attempts = 0
  const { s, service, calls } = await setup(t, 'TaskPanel', { discardTask: async () => { if (++attempts === 1) throw new Error('workspace is locked'); return { ok: true } } })
  const merge = s.mergeAll()
  assert.equal(service.active.value.options.title, '合并任务到主干？')
  assert.equal(calls.length, 0); service.cancel(); await merge
  const discarding = s.discard()
  assert.equal(service.active.value.options.tone, 'danger')
  assert.match(service.active.value.options.details, /Task task-a/)
  assert.equal(await service.submit(), false)
  assert.equal(service.active.value.error, 'workspace is locked')
  assert.equal(s.selected.value.id, 'task-a')
  assert.equal(await service.submit(), true); await discarding
  assert.equal(service.active.value, null); assert.equal(s.selected.value, null)
  assert.deepEqual(calls.map(call => call.slice(0, 3)), [['discardTask', 'project-a', 'task-a'], ['discardTask', 'project-a', 'task-a']])
})

test('forced task merge requires an explicit reason and trims it before the request', async t => {
  const { s, service, calls } = await setup(t, 'TaskPanel')
  s.selected.value.review.gate.violations = [{ severity: 'error', message: 'dependency cycle' }]
  const pending = s.mergeAll()
  assert.equal(service.active.value.options.confirmText, '强制合并')
  assert.match(service.active.value.options.details, /dependency cycle/)
  service.setValue('  '); assert.equal(await service.submit(), false); assert.equal(calls.length, 0)
  service.setValue('  compatibility migration  '); assert.equal(await service.submit(), true); await pending
  assert.equal(calls[0][3].force, true); assert.equal(calls[0][3].reason, 'compatibility migration')
})

test('single-file apply discloses cleanup and cannot silently bypass architecture gates', async t => {
  const { s, service, calls } = await setup(t, 'TaskPanel')
  s.selected.value.review.gate.violations = [{ severity: 'error', message: 'protected module' }]
  const pending = s.applyFile('src/one.py')
  assert.match(service.active.value.options.description, /其余任务改动将被放弃/)
  assert.match(service.active.value.options.details, /src\/one.py/)
  assert.equal(await service.submit(), false); assert.equal(calls.length, 0)
  service.setValue('Reviewed this isolated change'); await service.submit(); await pending
  assert.equal(calls[0][3].files[0], 'src/one.py'); assert.equal(calls[0][3].reason, 'Reviewed this isolated change')
})

for (const component of ['TaskPanel', 'ParallelPanel']) {
  test(`${component}: changing selection or project before confirmation cannot mutate a new target`, async t => {
    const { s, service, calls, props } = await setup(t, component)
    const pending = s.discard()
    s.selected.value = component === 'TaskPanel' ? task('task-b') : batch('batch-b')
    assert.equal(await service.submit(), false); assert.equal(calls.length, 0)
    assert.match(service.active.value.error, /已变化/)
    props.projectId = 'project-b'; await nextTick()
    assert.equal(await service.submit(), false); assert.equal(calls.length, 0)
    service.cancel(); await pending
  })
  test(`${component}: unmount cancels a pending deletion`, async t => {
    const { s, service, calls, dispose } = await setup(t, component)
    const pending = s.discard(); dispose(); await pending
    assert.equal(service.active.value, null); assert.equal(calls.length, 0)
  })
}

test('parallel queue partial failure stays visible for retry and preserves the captured batch', async t => {
  let attempts = 0
  const { s, service, calls } = await setup(t, 'ParallelPanel', { mergeBatch: async () => ++attempts === 1 ? { ok: false, merged: [], log: [{ lane: 'lane-1', error: 'fixture conflict' }], batch: batch() } : { ok: true, merged: ['lane-1'], batch: batch() } })
  const pending = s.mergeQueue()
  assert.match(service.active.value.options.description, /已经完成的车道会保留/)
  assert.equal(await service.submit(), false); assert.match(service.active.value.error, /fixture conflict/)
  assert.equal(await service.submit(), true); await pending
  assert.deepEqual(calls.map(call => call.slice(0, 3)), [['mergeBatch', 'project-a', 'batch-a'], ['mergeBatch', 'project-a', 'batch-a']])
})

test('late deletion result cannot clear or refresh a different project selection', async t => {
  const pendingResult = deferred()
  const { s, service, props, events } = await setup(t, 'TaskPanel', { discardTask: () => pendingResult.promise })
  const pending = s.discard(), submit = service.submit()
  props.projectId = 'project-b'; await nextTick(); s.selected.value = task('task-b')
  pendingResult.resolve({ ok: true }); await submit; await pending
  assert.equal(s.selected.value.id, 'task-b'); assert.equal(events.length, 0)
})

test('Git push cancellation is inert and a failed command remains retryable', async t => {
  let attempts = 0
  const { s, service, calls } = await setup(t, 'SourceControl', { gitAction: async () => ++attempts === 1 ? { ok: false, output: 'remote declined fixture' } : { ok: true, ...git } })
  let pending = s.run('push', { branch: 'main' }, '推送 main 到 origin？')
  assert.match(service.active.value.options.details, /main/)
  service.cancel(); await pending; assert.equal(calls.length, 0)
  pending = s.run('push', { branch: 'main' }, '推送 main 到 origin？')
  assert.equal(await service.submit(), false); assert.match(service.active.value.error, /remote declined fixture/)
  assert.equal(await service.submit(), true); await pending
  assert.equal(calls[0][3].branch, 'main'); assert.equal(service.active.value, null)
})

test('Git branch changes while a dialog is open cannot redirect the request', async t => {
  const { s, service, calls } = await setup(t, 'SourceControl')
  const pending = s.run('pull', {}, '只允许快进拉取。')
  s.status.value = { ...git, branch: 'other' }
  assert.equal(await service.submit(), false); assert.match(service.active.value.error, /分支已变化/)
  assert.equal(calls.length, 0); service.cancel(); await pending
})

test('branch checkout waits for confirmation and rejects new working tree changes', async t => {
  const { s, service, calls } = await setup(t, 'SourceControl')
  const pending = s.checkout('feature')
  assert.match(service.active.value.options.details, /main → feature/)
  assert.equal(calls.length, 0)
  s.status.value.dirty = true
  assert.equal(await service.submit(), false); assert.equal(calls.length, 0)
  assert.match(service.active.value.error, /新改动/)
  s.status.value.dirty = false; await service.submit(); await pending
  assert.deepEqual(calls[0], ['gitCheckout', 'project-a', 'feature'])
})

test('IDE mutations no longer call browser-native prompt or confirm', async () => {
  for (const path of ['../src/views/CodeView.vue', ...['TaskPanel', 'ParallelPanel', 'SourceControl'].map(name => `../src/components/code/${name}.vue`)]) {
    const source = await readFile(new URL(path, import.meta.url), 'utf8')
    assert.doesNotMatch(source, /window\.(?:prompt|confirm)\s*\(/)
  }
})
