import test from 'node:test'
import assert from 'node:assert/strict'
import { createRunPoller, createRuntimeCursor, createTerminalStream, outputLocations } from '../src/lib/runtimeClient.js'

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settle = async () => { await new Promise(setImmediate) }
function clock() {
  const timers = new Map(); let id = 0
  return { timers, schedule: (fn, delay) => { timers.set(++id, { fn, delay }); return id }, unschedule: id => timers.delete(id), fire() { const [id, timer] = timers.entries().next().value; timers.delete(id); timer.fn() } }
}
function socket() {
  return { readyState: 0, sent: [], closeCount: 0,
    send(data) { this.sent.push(JSON.parse(data)) }, close() { this.closeCount++; this.readyState = 3 },
    opened() { this.readyState = 1; this.onopen?.() },
    frame(data) { this.onmessage?.({ data: JSON.stringify(data) }) },
    ended(code = 1006) { this.readyState = 3; this.onclose?.({ code }) },
  }
}

test('runtime cursor deduplicates output/exit, reports a missing range once, and ignores malformed frames', () => {
  const output = [], exits = [], gaps = []
  const cursor = createRuntimeCursor({ onOutput: text => output.push(text), onExit: code => exits.push(code), onGap: gap => gaps.push(gap) })
  cursor.accept({ type: 'output', seq: 1, data: 'a' })
  cursor.accept({ type: 'output', seq: 1, data: 'duplicate' })
  cursor.accept({ type: 'output', seq: 4, data: 'b' })
  cursor.accept({ type: 'ready', seq: 100 }); cursor.accept({ type: 'output', seq: 0, data: 'bad' })
  cursor.accept({ type: 'exit', seq: 5, code: 3 }); cursor.accept({ type: 'exit', seq: 5, code: 3 })
  assert.deepEqual(output, ['a', 'b']); assert.deepEqual(exits, [3]); assert.equal(cursor.lastSeq, 5)
  assert.deepEqual(gaps, [{ after_seq: 1, first_seq: 4 }])
})

test('terminal replays ready history, reconnects from last seen seq, and never queues offline input', () => {
  const sockets = [], calls = [], output = [], c = clock()
  const client = createTerminalStream({ ...c, open: (...args) => { calls.push(args); const ws = socket(); sockets.push(ws); return ws }, onOutput: text => output.push(text) })
  client.connect('p', 't'); assert.equal(client.input('danger\r'), false)
  client.resize(120, 35); client.resize(125, 38); sockets[0].opened()
  assert.deepEqual(sockets[0].sent, [{ type: 'resize', cols: 125, rows: 38 }])
  sockets[0].frame({ type: 'ready', status: 'running', next_seq: 2, first_seq: 1 })
  sockets[0].frame({ type: 'output', data: 'first', seq: 1 }); sockets[0].frame({ type: 'output', data: 'second', seq: 2 })
  assert.deepEqual(output, ['first', 'second']); assert.equal(client.input('echo safe\r'), true)
  const staleMessage = sockets[0].onmessage
  sockets[0].ended(); assert.equal(client.state, 'reconnecting'); assert.equal(client.input('no replay\r'), false)
  c.fire(); assert.deepEqual(calls[1], ['p', 't', 2]); sockets[1].opened()
  staleMessage({ data: JSON.stringify({ type: 'output', seq: 100, data: 'stale' }) })
  sockets[1].frame({ type: 'output', seq: 2, data: 'duplicate' }); sockets[1].frame({ type: 'output', seq: 3, data: 'third' })
  assert.deepEqual(output, ['first', 'second', 'third'])
  assert.deepEqual(sockets[1].sent, [{ type: 'resize', cols: 125, rows: 38 }])
  client.dispose(); assert.equal(c.timers.size, 0); assert.equal(sockets[1].closeCount, 1)
})

test('terminal switching/disposal cancels reconnect and ignores captured callbacks from the old scope', () => {
  const sockets = [], output = [], c = clock()
  const client = createTerminalStream({ ...c, open: () => { const ws = socket(); sockets.push(ws); return ws }, onOutput: text => output.push(text) })
  client.connect('a', 'one'); sockets[0].opened(); const staleMessage = sockets[0].onmessage, staleClose = sockets[0].onclose
  sockets[0].ended(); assert.equal(c.timers.size, 1)
  client.connect('b', 'two'); assert.equal(c.timers.size, 0)
  staleMessage({ data: JSON.stringify({ type: 'output', seq: 1, data: 'old' }) }); staleClose({ code: 1006 })
  assert.deepEqual(output, []); assert.equal(c.timers.size, 0)
  sockets[1].opened(); sockets[1].ended(4404)
  assert.equal(c.timers.size, 0); assert.equal(client.state, 'closed'); client.dispose()
})

test('completed terminal ready still replays outputs but exit forbids input and reconnection', () => {
  const ws = socket(), output = [], exits = [], gaps = [], c = clock()
  const client = createTerminalStream({ ...c, open: () => ws, onOutput: text => output.push(text), onExit: code => exits.push(code), onGap: gap => gaps.push(gap) })
  client.connect('p', 't'); ws.opened()
  ws.frame({ type: 'ready', status: 'exited', first_seq: 9, next_seq: 10, truncated: true })
  ws.frame({ type: 'output', seq: 9, data: 'tail' }); ws.frame({ type: 'exit', seq: 10, code: 0 }); ws.ended()
  assert.deepEqual(output, ['tail']); assert.deepEqual(exits, [0]); assert.equal(gaps.length, 1)
  assert.equal(client.lastSeq, 10); assert.equal(client.input('x'), false); assert.equal(c.timers.size, 0)
})

test('run polling sorts/deduplicates events, uses emitted seq rather than ready head, stops after exit', async () => {
  const loads = [], output = [], updates = [], c = clock()
  const pages = [
    { status: 'running', next_seq: 3, first_seq: 1, events: [{ type: 'output', seq: 2, data: 'b' }, { type: 'output', seq: 1, data: 'a' }] },
    { status: 'exited', next_seq: 3, first_seq: 1, exit_code: 0, events: [{ type: 'output', seq: 2, data: 'dup' }, { type: 'exit', seq: 3, code: 0 }] },
  ]
  const poller = createRunPoller({ ...c, load: (...args) => { loads.push(args); return Promise.resolve(pages.shift()) }, onOutput: (text, event, identity) => output.push([text, identity.id]), onUpdate: meta => updates.push(meta) })
  await poller.select('p', 'r'); assert.equal(poller.lastSeq, 2); assert.equal(c.timers.size, 1)
  c.fire(); await settle()
  assert.deepEqual(loads, [['p', 'r', 0], ['p', 'r', 2]]); assert.deepEqual(output, [['a', 'r'], ['b', 'r']])
  assert.equal(poller.lastSeq, 3); assert.equal(updates.length, 2); assert.equal(c.timers.size, 0); poller.dispose()
})

test('run selection isolates delayed projects and requests remain single flight', async () => {
  const pending = [], output = [], updates = [], c = clock()
  const poller = createRunPoller({ ...c, load: (...args) => { const item = deferred(); pending.push({ ...item, args }); return item.promise }, onOutput: text => output.push(text), onUpdate: meta => updates.push(meta) })
  const old = poller.select('a', 'one'); poller.refresh(); assert.equal(pending.length, 1)
  const next = poller.select('b', 'two'); poller.refresh(); assert.equal(pending.length, 2)
  pending[0].resolve({ status: 'running', next_seq: 1, events: [{ type: 'output', seq: 1, data: 'old' }] }); await old
  assert.equal(c.timers.size, 0); poller.refresh(); assert.equal(pending.length, 2)
  pending[1].resolve({ status: 'running', next_seq: 1, events: [{ type: 'output', seq: 1, data: 'new' }] }); await next
  assert.deepEqual(output, ['new']); assert.equal(updates.length, 1); assert.equal(c.timers.size, 1)
  poller.dispose(); assert.equal(c.timers.size, 0)
})

test('run disposal ignores in-flight output and resource errors stop polling', async () => {
  const wait = deferred(), output = [], errors = [], c = clock()
  const poller = createRunPoller({ ...c, load: () => wait.promise, onOutput: text => output.push(text), onError: e => errors.push(e) })
  const promise = poller.select('p', 'r'); poller.dispose()
  wait.resolve({ status: 'running', events: [{ type: 'output', seq: 1, data: 'late' }] }); await promise
  assert.deepEqual(output, []); assert.equal(c.timers.size, 0)
  const unavailable = createRunPoller({ ...c, load: async () => { throw Object.assign(new Error('gone'), { status: 404 }) }, onError: e => errors.push(e) })
  await unavailable.select('p', 'gone'); assert.equal(errors.length, 1); assert.equal(c.timers.size, 0); unavailable.dispose()
})

test('output locations accept relative traceback/test positions, reject absolute or parent paths', () => {
  const refs = outputLocations('File "src/main.py", line 10\ntests/test_main.py:12:3\nsrc/main.py:10\nFile "C:\\private\\x.py", line 20\n../secrets.py:1\n/root/file.py:2')
  assert.deepEqual(refs, [{ path: 'src/main.py', line: 10, column: 1 }, { path: 'tests/test_main.py', line: 12, column: 3 }])
})
