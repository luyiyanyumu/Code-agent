import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { debugAdapterMeta, debugLanguageForPath, javaDebugOptions } from '../src/lib/debugClient.js'

const clone = value => JSON.parse(JSON.stringify(value))
const settle = async () => { await nextTick(); await new Promise(setImmediate); await nextTick() }
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const state = overrides => ({ id: 'debug-1', project_id: 'p', path: 'main.py', status: 'paused', seq: 1, pause_seq: 1, frame_id: 10, stack: [{ id: 10, name: 'compute', path: 'main.py', line: 7, column: 1 }], variables: [{ name: 'nested', value: '{}', variablesReference: 5 }], scopes: [], console: [], console_seq: 0, breakpoints: [], ...overrides })

async function setup(t, client = {}, overrides = {}) {
  const props = reactive({ projectId: 'p', file: { path: 'main.py' }, breakpoints: [], visible: true, ...overrides })
  const events = [], timers = new Map(), calls = []
  let unmount, exposed, timerId = 0
  const debugClient = {
    meta: async () => ({ available: true, languages: ['python'] }), sessions: async () => ({ sessions: [] }),
    start: async (...args) => { calls.push(['start', ...args]); return state() },
    state: async (...args) => { calls.push(['state', ...args]); return state() },
    action: async (...args) => { calls.push(['action', ...args]); return state({ seq: 3, status: 'running', frame_id: null, stack: [], variables: [] }) },
    breakpoints: async (...args) => { calls.push(['breakpoints', ...args]); return state({ breakpoints: args[2] }) },
    variables: async () => ({ variables: [{ name: 'items', value: '[1,2]', variablesReference: 6 }] }),
    ...client,
  }
  const scope = effectScope()
  const source = (await readFile(new URL('../src/components/code/DebugPanel.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const context = vm.createContext({
    computed, ref, nextTick, watch: (...args) => scope.run(() => watch(...args)), debugClient, debugAdapterMeta, debugLanguageForPath, javaDebugOptions,
    defineProps: () => props, defineEmits: () => (name, payload) => events.push([name, clone(payload)]),
    defineExpose: methods => { exposed = methods }, onBeforeUnmount: fn => { unmount = fn },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId }, clearTimeout: id => timers.delete(id),
  })
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject = { meta, session, error, path, argsText, cwd, expanded, canStart, language, javaRequest, mainClass, projectName, attachHost, attachPort, stopOnEntry, adapter, selectedLanguage, applyState, start, requestStart, refresh, action, toggleVariable, syncBreakpoints, selectFrame, refreshEnvironment }`, context))
  await settle()
  t.after(() => { unmount(); scope.stop() })
  return { s: context.subject, props, events, timers, calls, exposed }
}

test('launch button requests parent save; only exposed start performs a real launch', async t => {
  const { s, events, calls, exposed } = await setup(t)
  s.argsText.value = '["--port","8000"]'
  s.requestStart()
  assert.deepEqual(events.find(([name]) => name === 'start-request')[1], { path: 'main.py', args: ['--port', '8000'], cwd: '' })
  assert.equal(calls.some(([name]) => name === 'start'), false)
  await exposed.start({ path: 'main.py', args: ['--port', '8000'] })
  await settle()
  assert.equal(calls.filter(([name]) => name === 'start').length, 1)
  assert.deepEqual(events.filter(([name]) => name === 'location').at(-1)[1], { path: 'main.py', line: 7, column: 1 })
  await s.action('continue')
  await settle()
  assert.equal(events.filter(([name]) => name === 'location').at(-1)[1], null)
})

test('first-mount F5 waits for initial capability loading and duplicate waiting starts launch once', async t => {
  const capability = deferred()
  const { s, calls, exposed } = await setup(t, { meta: () => capability.promise })
  const first = exposed.start({ path: 'main.py' })
  const duplicate = exposed.start({ path: 'main.py' })
  assert.equal(calls.some(([name]) => name === 'start'), false)
  capability.resolve({ available: true })
  await Promise.all([first, duplicate]); await settle()
  assert.equal(calls.filter(([name]) => name === 'start').length, 1)
  assert.equal(exposed.isPaused(), true)
  await exposed.continue(); await settle()
  assert.equal(s.session.value.status, 'running')
})

test('waiting initial start is cancelled when its project changes', async t => {
  const capability = deferred()
  const { props, calls, exposed, s } = await setup(t, { meta: project => project === 'p' ? capability.promise : Promise.resolve({ available: true }) })
  const waiting = exposed.start({ path: 'main.py' })
  props.projectId = 'q'; props.file = { path: 'other.py' }
  await settle()
  capability.resolve({ available: true })
  await waiting; await settle()
  assert.equal(calls.some(([name]) => name === 'start'), false)
  assert.equal(s.session.value, null)
  assert.equal(s.path.value, 'other.py')
})

test('a saved entry that becomes dirty while waiting for capability is rejected before launch', async t => {
  const capability = deferred()
  const { props, calls, exposed, s } = await setup(t, { meta: () => capability.promise })
  const waiting = exposed.start({ path: 'main.py' })
  props.file = { path: 'main.py', dirty: true, text: 'new draft typed during loading' }
  capability.resolve({ available: true })
  await waiting; await settle()
  assert.equal(calls.some(([name]) => name === 'start'), false)
  assert.match(s.error.value, /保存/)
})

test('unsupported entry and missing dependency never start a debug process', async t => {
  const { s, calls } = await setup(t, { meta: async () => ({ available: false, install_hint: 'install debugpy' }) }, { file: { path: 'App.vue' } })
  assert.equal(s.canStart.value, false)
  await s.start()
  assert.equal(s.error.value, 'install debugpy')
  assert.equal(calls.some(([name]) => name === 'start'), false)
})

test('late project metadata cannot reattach another project debug session', async t => {
  const old = deferred()
  const { s, props } = await setup(t, {
    meta: project => project === 'p' ? old.promise : Promise.resolve({ available: true, languages: ['python'] }),
    sessions: async project => ({ sessions: project === 'p' ? [state()] : [] }),
  })
  props.projectId = 'q'; props.file = { path: 'other.py' }
  await settle()
  old.resolve({ available: false, install_hint: 'stale' })
  await settle()
  assert.equal(s.meta.value.available, true)
  assert.equal(s.session.value, null)
  assert.equal(s.path.value, 'other.py')
})

test('older polling response cannot replace a newer action state or resurrect old variables', async t => {
  const response = deferred(), variable = deferred()
  const { s } = await setup(t, { state: () => response.promise, variables: () => variable.promise })
  s.applyState(state({ seq: 2 }))
  const expanding = s.toggleVariable({ key: 'locals/0:nested', variablesReference: 5 })
  const polling = s.refresh()
  await s.action('continue')
  response.resolve(state({ seq: 2 }))
  variable.resolve({ variables: [{ name: 'stale', value: '1' }] })
  await Promise.all([polling, expanding]); await settle()
  assert.equal(s.session.value.status, 'running')
  assert.deepEqual(clone(s.expanded.value), {})
})

test('breakpoint updates serialize latest props and merge only breakpoint fields', async t => {
  const first = deferred(), payloads = []
  const { s, props } = await setup(t, { breakpoints: async (project, id, points) => {
    payloads.push(clone(points)); if (payloads.length === 1) return first.promise
    return state({ seq: 1, breakpoints: points })
  } })
  s.applyState(state({ seq: 8 }))
  props.breakpoints = [{ path: 'main.py', line: 7 }]
  await settle()
  props.breakpoints = [{ path: 'main.py', line: 9 }]
  await settle()
  first.resolve(state({ seq: 1, status: 'running', breakpoints: [{ path: 'main.py', line: 7 }] }))
  await settle()
  assert.deepEqual(payloads, [[{ path: 'main.py', line: 7 }], [{ path: 'main.py', line: 9 }]])
  assert.equal(s.session.value.status, 'paused')
  assert.equal(s.session.value.seq, 8)
  assert.equal(s.session.value.breakpoints[0].line, 9)
})

test('console polling merges by sequence and frame choice emits relative location', async t => {
  const { s, events } = await setup(t, { state: async (project, id, options) => state({ seq: 5, frame_id: options.frameId, stack: [{ id: 11, name: '<module>', path: 'main.py', line: 11, column: 2 }], console_seq: 2, console: [{ seq: 2, output: 'second' }] }) })
  s.applyState(state({ console_seq: 1, console: [{ seq: 1, output: 'first' }] }))
  await s.selectFrame({ id: 11, path: 'main.py', line: 11, column: 2 })
  await settle()
  assert.deepEqual(clone(s.session.value.console).map(item => item.output), ['first', 'second'])
  assert.deepEqual(events.filter(([name]) => name === 'open-file').at(-1)[1], { path: 'main.py', line: 11, column: 2 })
  assert.deepEqual(events.filter(([name]) => name === 'location').at(-1)[1], { path: 'main.py', line: 11, column: 2 })
})

const javaMeta = { available: true, languages: ['python', 'java'], adapters: { python: { available: true }, java: { available: true, bundle_found: true, requests: ['launch', 'attach'] } } }
test('Java auto/F5 launch supports entry selection, options and Java-only breakpoints through the parent save request', async t => {
  const { s, events, calls, exposed } = await setup(t, { meta: async () => javaMeta, start: async (project, payload) => { calls.push(['start', project, payload]); return state({ path: payload.path, language: 'java', request: payload.java.request, main_class: payload.java.main_class }) } },
    { file: { path: 'src/Main.java' }, breakpoints: [{ path: 'src/Main.java', line: 8 }, { path: 'other.py', line: 4 }] })
  assert.equal(s.selectedLanguage.value, 'java'); assert.equal(s.canStart.value, true)
  s.mainClass.value = 'demo.Main'; s.projectName.value = 'demo'; s.stopOnEntry.value = false
  s.requestStart()
  const payload = events.find(([name]) => name === 'start-request')[1]
  assert.deepEqual(payload, { path: 'src/Main.java', args: [], cwd: '', language: 'java', java: { request: 'launch', main_class: 'demo.Main', project_name: 'demo', stop_on_entry: false } })
  await exposed.start(payload)
  assert.equal(calls.filter(([name]) => name === 'start').length, 1)
  assert.deepEqual(clone(calls.find(([name]) => name === 'start')[2].breakpoints), [{ path: 'src/Main.java', line: 8 }])
})

test('Java prerequisite stays unavailable when Python is available and reprobe does not erase launch configuration', async t => {
  let found = false
  const { s, calls } = await setup(t, { meta: async () => ({ available: true, languages: found ? ['python', 'java'] : ['python'], adapters: { python: { available: true }, java: { available: found, install_hint: 'Install Java Debug plugin' } } }) }, { file: { path: 'Main.java' } })
  assert.equal(s.canStart.value, false); assert.equal(s.adapter.value.available, false)
  await s.start()
  assert.match(s.error.value, /Install Java Debug plugin/); assert.equal(calls.length, 0)
  s.mainClass.value = 'demo.Main'; found = true
  await s.refreshEnvironment()
  assert.equal(s.canStart.value, true); assert.equal(s.mainClass.value, 'demo.Main')
})

test('Java attach is localhost-only with integer ports, skips draft save and retains attach semantics in breakpoint updates', async t => {
  const { s, props, calls } = await setup(t, { meta: async () => javaMeta, start: async (project, payload) => { calls.push(['start', project, payload]); return state({ language: 'java', request: 'attach', path: payload.path }) } }, { file: { path: 'Main.java', dirty: true }, breakpoints: [{ path: 'Main.java', line: 4 }, { path: 'other.py', line: 2 }] })
  s.javaRequest.value = 'attach'; s.attachHost.value = 'localhost'; s.attachPort.value = '5005'
  s.argsText.value = '{invalid launch-only args}'
  await s.start()
  const start = calls.find(([name]) => name === 'start')
  assert.equal(start[2].java.request, 'attach'); assert.equal(start[2].java.host, '127.0.0.1'); assert.equal(start[2].java.port, 5005)
  assert.equal(start[2].args.length, 0)
  props.breakpoints = [{ path: 'Main.java', line: 9 }, { path: 'other.py', line: 2 }]
  await settle()
  assert.deepEqual(clone(calls.filter(([name]) => name === 'breakpoints').at(-1)[3]), [{ path: 'Main.java', line: 9 }])
})

test('Java rejects invalid attach settings and explicit language mismatch without creating sessions', async t => {
  const { s, calls } = await setup(t, { meta: async () => javaMeta }, { file: { path: 'Main.java' } })
  s.javaRequest.value = 'attach'; s.attachPort.value = '0'
  await s.start(); assert.match(s.error.value, /端口/)
  s.attachPort.value = '5005'; s.attachHost.value = 'example.com'
  await s.start(); assert.match(s.error.value, /本机/)
  s.language.value = 'python'; await s.start()
  assert.match(s.error.value, /一致/); assert.equal(calls.some(([name]) => name === 'start'), false)
})

test('Java dirty launch remains blocked and recovered history keeps Java controls even with a Python current tab', async t => {
  const blocked = await setup(t, { meta: async () => javaMeta }, { file: { path: 'Main.java', dirty: true } })
  await blocked.s.start(); assert.match(blocked.s.error.value, /保存/); assert.equal(blocked.calls.some(([name]) => name === 'start'), false)
  const resumed = await setup(t, { meta: async () => javaMeta, sessions: async () => ({ sessions: [state({ path: 'Main.java', language: 'java', request: 'attach' })] }) })
  assert.equal(resumed.s.selectedLanguage.value, 'java'); assert.equal(resumed.s.javaRequest.value, 'attach')
  assert.equal(resumed.s.path.value, 'Main.java')
})

test('debug metadata and Java options never infer unsupported adapters or remote attach endpoints', () => {
  assert.equal(debugAdapterMeta({ available: true }, 'java').available, false)
  assert.equal(debugAdapterMeta({ available: true }, 'python').available, true)
  assert.equal(debugAdapterMeta({ available: true, languages: ['java'] }, 'python').available, false)
  assert.equal(debugLanguageForPath('src/Main.JAVA'), 'java')
  assert.equal(debugLanguageForPath('component.vue'), '')
  assert.deepEqual(javaDebugOptions({ request: 'attach', host: '::1', port: '65535' }), { request: 'attach', main_class: '', project_name: '', stop_on_entry: true, host: '::1', port: 65535 })
  for (const port of [true, 0, 65536, 3.5, '3.5', '1e3', '']) assert.throws(() => javaDebugOptions({ request: 'attach', port }))
  assert.throws(() => javaDebugOptions({ request: 'attach', host: 'https://localhost' }))
})

test('opening settings suspends debug polling and returning resumes its console cursor without stopping the session', async t => {
  const reads = []
  const h = await setup(t, {
    sessions: async () => ({ sessions: [state({ console_seq: 2, console: [{ seq: 2, output: 'before settings' }] })] }),
    state: async (project, id, options) => {
      reads.push([project, id, clone(options)])
      return state({ seq: 2, console_seq: 3, console: [{ seq: 2, output: 'duplicate' }, { seq: 3, output: 'while hidden' }] })
    },
  })
  h.s.argsText.value = '["draft argument"]'
  h.s.expanded.value = { 'locals/0:nested': [{ name: 'saved expansion', value: '7' }] }
  assert.equal(h.timers.size, 1)
  h.props.visible = false
  await settle()
  assert.equal(h.timers.size, 0)
  assert.equal(h.s.session.value.status, 'paused')
  assert.deepEqual(reads, [])
  assert.equal(h.calls.some(([name]) => ['action', 'start'].includes(name)), false)
  h.props.visible = true
  await settle()
  assert.deepEqual(reads, [['p', 'debug-1', { afterConsole: 2 }]])
  assert.deepEqual(clone(h.s.session.value.console).map(row => row.output), ['before settings', 'while hidden'])
  assert.equal(h.s.argsText.value, '["draft argument"]')
  assert.equal(h.s.expanded.value['locals/0:nested'][0].name, 'saved expansion')
  assert.equal(h.timers.size, 1)
  assert.equal(h.calls.some(([name]) => ['action', 'start'].includes(name)), false)
})

test('an in-flight debug poll completing while hidden keeps its state but cannot schedule another read', async t => {
  const pending = deferred(), reads = []
  const h = await setup(t, {
    sessions: async () => ({ sessions: [state({ console_seq: 1, console: [{ seq: 1, output: 'first' }] })] }),
    state: async (_project, _id, options) => {
      reads.push(clone(options))
      return reads.length === 1 ? pending.promise : state({ seq: 3, console_seq: 3, console: [{ seq: 3, output: 'third' }] })
    },
  })
  const [timerId, tick] = h.timers.entries().next().value
  h.timers.delete(timerId)
  const polling = tick()
  assert.equal(reads.length, 1)
  h.props.visible = false
  await settle()
  pending.resolve(state({ seq: 2, console_seq: 2, console: [{ seq: 2, output: 'second' }] }))
  await polling
  await settle()
  assert.equal(h.s.session.value.console_seq, 2)
  assert.equal(h.timers.size, 0, 'the finished timer must not rearm a hidden panel')
  assert.equal(reads.length, 1)
  h.props.visible = true
  await settle()
  assert.deepEqual(reads, [{ afterConsole: 1 }, { afterConsole: 2 }])
  assert.deepEqual(clone(h.s.session.value.console).map(row => row.output), ['first', 'second', 'third'])
  assert.equal(h.calls.some(([name]) => ['action', 'start'].includes(name)), false)
})

test('recovering debug history in a hidden workspace retains the session without periodic reads until it becomes visible', async t => {
  const h = await setup(t, { sessions: async () => ({ sessions: [state({ status: 'running', variables: [], stack: [], frame_id: null })] }) }, { visible: false })
  assert.equal(h.s.session.value.id, 'debug-1')
  assert.equal(h.s.session.value.status, 'running')
  assert.equal(h.timers.size, 0)
  assert.equal(h.calls.length, 0)
  h.props.visible = true
  await settle()
  assert.equal(h.calls.filter(([name]) => name === 'state').length, 1)
  assert.equal(h.timers.size, 1)
  assert.equal(h.calls.some(([name]) => ['action', 'start'].includes(name)), false)
})
