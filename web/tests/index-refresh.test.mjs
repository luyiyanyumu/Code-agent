import assert from 'node:assert/strict'
import test from 'node:test'
import { useIndexRefresh } from '../src/composables/useIndexRefresh.js'

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const result = (state, generation = 1) => ({ index_refresh: { state, pending: ['queued', 'running'].includes(state), requested_generation: generation, completed_generation: state === 'done' ? generation : 0 } })
function setup(client, onDone = () => {}) {
  const timers = new Map(); let seq = 0
  return { timers, progress: useIndexRefresh({ client, onDone, setTimer: fn => { timers.set(++seq, fn); return seq }, clearTimer: id => timers.delete(id) }) }
}

test('save acknowledgement starts background progress without waiting for index HTTP', async () => {
  const reading = deferred(); let reads = 0, completed = 0
  const { progress, timers } = setup({ indexStatus: () => { reads++; return reading.promise } }, () => completed++)
  progress.accept('p', result('queued'))
  assert.equal(reads, 0); assert.equal(progress.state.value.pending, true)
  const poll = progress.poll()
  assert.equal(reads, 1); assert.equal(progress.state.value.pending, true)
  reading.resolve(result('done')); await poll
  assert.equal(progress.state.value.pending, false); assert.equal(completed, 1); assert.equal(timers.size, 0)
  progress.reset()
})

test('an old completion cannot hide a newer save or mutate a different project', async () => {
  const old = deferred(); let signal
  const { progress } = setup({ indexStatus: (_p, _t, options) => { signal = options.signal; return old.promise } })
  progress.accept('p', result('queued'))
  const poll = progress.poll()
  progress.accept('p', result('queued', 2))
  assert.equal(signal.aborted, true)
  old.resolve(result('done')); await poll
  assert.equal(progress.state.value.requested_generation, 2); assert.equal(progress.state.value.pending, true)
  progress.reset(); progress.accept('new', result('queued'))
  assert.equal(progress.state.value.requested_generation, 1)
  progress.reset()
})

test('a stale server snapshot is polled again until the saved generation finishes', async () => {
  const { progress, timers } = setup({ indexStatus: async () => result('done', 1) })
  progress.accept('p', result('queued', 2)); await progress.poll()
  assert.equal(progress.state.value.pending, true); assert.equal(timers.size, 1)
  progress.reset(); assert.equal(timers.size, 0)
})

test('index errors retry indexing independently; status request failures only retry reading', async () => {
  let scans = 0, reads = 0
  const { progress } = setup({
    scan: async () => { scans++; return result('queued', 2) },
    indexStatus: async () => { if (++reads === 1) throw new Error('offline'); return result('done', 2) },
  })
  progress.accept('p', result('error'))
  await progress.retry(); assert.equal(scans, 1); assert.equal(progress.state.value.pending, true)
  await progress.poll(); assert.match(progress.statusError.value, /不可用/)
  await progress.retry(); assert.equal(scans, 1); assert.equal(reads, 2); assert.equal(progress.state.value.state, 'done')
  progress.reset()
})

test('a status timeout is visible and changing projects discards the aborted response', async () => {
  let signal
  const { progress, timers } = setup({ indexStatus: (_p, _t, options) => new Promise((_yes, no) => {
    signal = options.signal; signal.addEventListener('abort', () => no(Object.assign(new Error('aborted'), { name: 'AbortError' })))
  }) })
  progress.accept('p', result('queued')); const poll = progress.poll()
  assert.equal(timers.size, 1); [...timers.values()][0](); await poll
  assert.equal(signal.aborted, true); assert.match(progress.statusError.value, /超时/)
  const second = progress.poll(); progress.reset(); await second
  assert.equal(progress.state.value, null); assert.equal(progress.statusError.value, ''); assert.equal(timers.size, 0)
})
