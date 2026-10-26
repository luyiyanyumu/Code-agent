// Run: node --test web/tests/unified-search.test.mjs
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'

const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const flush = async () => { await nextTick(); await new Promise(setImmediate) }
async function component(initialMode = 'files') {
  const props = reactive({ open: true, projectId: 'alpha', initialMode }), events = [], calls = { files: [], search: [] }
  const scope = effectScope(), unmount = [], timers = new Map(); let timerId = 0
  const code = Object.fromEntries(Object.keys(calls).map(method => [method, (pid, options) => {
    const call = { ...deferred(), pid, options }; calls[method].push(call); return call.promise
  }]))
  const source = (await readFile(new URL('../src/components/code/QuickOpen.vue', import.meta.url), 'utf8'))
    .match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm, '')
  const context = vm.createContext({ computed, nextTick, ref, watch, api: { code },
    defineProps: () => props, defineEmits: () => (...args) => events.push(JSON.parse(JSON.stringify(args))),
    onBeforeUnmount: fn => unmount.push(fn), document: { activeElement: null },
    setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id }, clearTimeout: id => timers.delete(id),
  })
  scope.run(() => vm.runInContext(`${source}\n;globalThis.subject = { mode, query, files, data, busy, results, loadFiles, search, setMode, enter, inputEnter, move, close }`, context))
  await flush()
  return { s: context.subject, props, calls, events,
    dispose() { unmount.forEach(fn => fn()); scope.stop(); assert.equal(timers.size, 0) },
  }
}
const content = path => ({ matches: [{ path, line: 3, column: 2, text: 'needle' }] })

test('late file/content responses cannot replace results after mode, query, project or close changes', async t => {
  const view = await component(); t.after(() => view.dispose())
  const { s, calls, props, events } = view
  s.setMode('content'); s.query.value = 'first'; const first = s.search()
  calls.files[0].resolve({ files: [{ path: 'old-file.js' }] }); await flush()
  assert.equal(s.files.value.length, 0); assert.equal(s.busy.value, true)
  s.query.value = 'second'; const second = s.search()
  calls.search[1].resolve(content('second.js')); await second
  calls.search[0].resolve(content('first.js')); await first
  assert.equal(s.results.value[0].path, 'second.js')
  s.query.value = 'old-project'; const oldProject = s.search()
  props.projectId = 'beta'; await flush()
  assert.equal(calls.files[1].pid, 'beta')
  calls.files[1].resolve({ files: [{ path: 'beta.js' }] }); await flush()
  calls.search[2].resolve(content('alpha.js')); await oldProject
  assert.equal(s.data.value, null); assert.equal(s.results.value[0].path, 'beta.js')
  const closing = s.loadFiles()
  s.close(); calls.files[2].resolve({ files: [{ path: 'after-close.js' }] }); await closing
  assert.equal(s.results.value[0].path, 'beta.js'); assert.equal(s.busy.value, false)
  assert.deepEqual(events, [['mode-change', 'content'], ['close']])
  props.open = false; await flush()
  assert.equal(s.results.value.length, 0)
})

test('one query survives mode switches and selections emit file paths or exact content positions then close', async t => {
  const view = await component(); t.after(() => view.dispose())
  const { s, calls, props, events } = view
  calls.files[0].resolve({ files: [{ path: 'src/needle.js' }] }); await flush()
  s.query.value = 'needle'; s.setMode('content')
  assert.equal(s.query.value, 'needle')
  s.setMode('files'); calls.files[1].resolve({ files: [{ path: 'src/needle.js' }] }); await flush()
  assert.equal(s.query.value, 'needle'); s.enter()
  assert.deepEqual(events, [['mode-change', 'content'], ['mode-change', 'files'], ['open-file', { path: 'src/needle.js' }], ['close']])
  props.open = false; await flush(); props.initialMode = 'content'; props.open = true; await flush()
  s.query.value = 'needle'; const searching = s.search()
  calls.search[0].resolve({ matches: [
    { path: 'src/a.js', line: 4, column: 1, text: 'needle' },
    { path: 'src/b.js', line: 42, column: 7, text: 'needle' },
  ] }); await searching
  s.move(1); s.move(1); s.enter()
  assert.deepEqual(events.slice(4), [['mode-change', 'content'], ['open-file', { path: 'src/b.js', line: 42, column: 7 }], ['close']])
})

test('busy Enter sends one search and composing Enter never starts a request', async t => {
  const view = await component('content'); t.after(() => view.dispose())
  const { s, calls } = view; let prevented = 0
  const event = extra => ({ preventDefault() { prevented++ }, ...extra })
  s.query.value = 'needle'
  s.inputEnter(event({ isComposing: true })); s.inputEnter(event({ keyCode: 229 }))
  assert.equal(calls.search.length, 0); assert.equal(prevented, 0)
  s.inputEnter(event()); s.inputEnter(event()); s.inputEnter(event())
  assert.equal(calls.search.length, 1); assert.equal(s.busy.value, true)
  assert.equal(calls.search[0].options.query, 'needle')
  calls.search[0].resolve(content('current.js')); await flush()
  assert.equal(s.busy.value, false); assert.equal(s.results.value[0].path, 'current.js')
  s.inputEnter(event()); assert.equal(calls.search.length, 2)
  calls.search[1].resolve(content('again.js')); await flush()
})
