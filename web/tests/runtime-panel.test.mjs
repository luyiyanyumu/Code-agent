// Execute the real SFC setup with fake PTY/API/xterm; no process is launched.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { createRunPoller, createTerminalStream, isRunning, outputLocations } from '../src/lib/runtimeClient.js'

const deferred = () => { let resolve; const promise = new Promise(a => { resolve = a }); return { promise, resolve } }
const settle = async () => { await nextTick(); await new Promise(setImmediate); await nextTick() }
const meta = (id, overrides = {}) => ({ id, shell: 'powershell', cwd: '.', status: 'running', exit_code: null, ...overrides })
async function setup(t, options = {}) {
  const props = reactive({ projectId: 'p', file: { path: 'src/main.py', lang: 'python', dirty: false }, activeTab: 'terminal', visible: true, ...options.props })
  const calls = [], sockets = [], terms = [], observers = [], events = [], timers = new Map(); let timerId = 0, unmount, exposed
  const schedule = (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId }, unschedule = id => timers.delete(id)
  class FakeTerminal {
    constructor(options) { this.options = options; this.output = ''; this.disposables = []; this.cols = 80; this.rows = 24; terms.push(this) }
    loadAddon(addon) { addon.term = this; this.addon = addon }
    open(host) { this.host = host }
    write(text) { this.output += text }
    onData(fn) { this.data = fn; const d = { disposed: false, dispose() { this.disposed = true } }; this.disposables.push(d); return d }
    onResize(fn) { this.resized = fn; const d = { disposed: false, dispose() { this.disposed = true } }; this.disposables.push(d); return d }
    focus() { this.focused = true }
    dispose() { this.disposed = true; this.addon?.dispose() }
  }
  class FakeFit {
    fit() { this.fitCount = (this.fitCount || 0) + 1; if (this.term.cols !== 100) { this.term.cols = 100; this.term.rows = 20; this.term.resized?.({ cols: 100, rows: 20 }) } }
    dispose() { this.disposed = true }
  }
  class FakeResizeObserver {
    constructor(fn) { this.fn = fn; this.elements = new Set(); observers.push(this) }
    observe(el) { this.elements.add(el) } unobserve(el) { this.elements.delete(el) } disconnect() { this.disconnected = true; this.elements.clear() }
  }
  const defaults = {
    terminals: () => ({ available: true, backend: 'conpty', shells: [{ id: 'powershell', label: 'PowerShell', available: true }], terminals: options.terminals || [] }),
    runConfigs: () => ({ configs: [{ id: 'tests', kind: 'pytest', name: 'Python tests', available: true, cwd: '.', path: 'tests' }] }),
    runs: () => ({ runs: [] }),
    run: (_pid, id) => ({ id, kind: 'python', status: 'exited', exit_code: 0, events: [], next_seq: 0 }),
    createTerminal: () => meta('created'), closeTerminal: (_pid, id) => meta(id, { status: 'closed' }),
    createRun: (_pid, payload) => ({ id: 'run-created', ...payload, command: ['python', payload.path || ''], status: 'exited', exit_code: 0 }),
    stopRun: (_pid, id) => ({ id, status: 'stopped', exit_code: 1 }),
    ...options.api,
  }
  const code = Object.fromEntries(Object.entries(defaults).map(([name, fn]) => [name, (...args) => { calls.push([name, ...JSON.parse(JSON.stringify(args))]); return Promise.resolve(fn(...args)) }]))
  let source = (await readFile(new URL('../src/components/code/RuntimePanel.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1]
  source = source.replace(/^import .*\r?\n/gm, '').replace(/import\('([^']+)'\)/g, 'Promise.resolve(__modules["$1"])')
  const scope = effectScope()
  const context = vm.createContext({
    computed, ref, watch: (...args) => scope.run(() => watch(...args)), nextTick, ResizeObserver: FakeResizeObserver,
    defineProps: () => props, defineEmits: () => (name, payload) => events.push([name, payload]), defineExpose: value => { exposed = value }, onBeforeUnmount: fn => { unmount = fn },
    setTimeout: schedule, clearTimeout: unschedule, AbortController, api: { code },
    createRunPoller: opts => createRunPoller({ ...opts, schedule, unschedule }), createTerminalStream: opts => createTerminalStream({ ...opts, schedule, unschedule }), isRunning, outputLocations,
    openTerminalStream: (...args) => {
      const ws = { args, readyState: 0, sent: [], closeCount: 0, send(text) { this.sent.push(JSON.parse(text)) }, close() { this.closeCount++; this.readyState = 3 }, opened() { this.readyState = 1; this.onopen?.() }, frame(frame) { this.onmessage?.({ data: JSON.stringify(frame) }) } }
      sockets.push(ws); return ws
    },
    __modules: { '@xterm/xterm': options.terminalModule || { Terminal: FakeTerminal }, '@xterm/addon-fit': { FitAddon: FakeFit } },
  })
  scope.run(() => vm.runInContext(`${source}\n;globalThis.subject = { terminals, terminalId, terminalInfo, entries, hosts, runs, runId, configs, configId, configGroups, error, notice, outputs, output, kind, target, runCwd, runName, loading, loadStates, loadRequests, formTouched, creatingRun, ensureTerminal, setHost, fitSelected, closeTerminal, reconnect, runForm, startRun, stopRun, showTests, chooseConfig, loadSection, loadProject }`, context))
  const s = context.subject; await settle()
  let released = false
  const dispose = () => { if (!released) { released = true; unmount(); scope.stop() } }
  t.after(dispose)
  return { s, props, calls, sockets, terms, observers, events, timers, exposed, dispose, FakeTerminal, async host(id) { s.setHost(id, { clientWidth: 900, clientHeight: 200 }); await settle() } }
}

test('runtime discovery/showTests never execute; only manual typed run or terminal action creates a process', async t => {
  const { s, calls, exposed } = await setup(t)
  assert.deepEqual(calls.map(item => item[0]), ['terminals', 'runConfigs', 'runs'])
  exposed.showTests(); await settle(); assert.equal(s.kind.value, 'pytest'); assert.equal(calls.some(item => item[0] === 'createRun'), false)
  s.kind.value = 'npm'; s.target.value = 'test:unit'; s.runCwd.value = 'frontend'; await s.runForm()
  assert.deepEqual(calls.find(item => item[0] === 'createRun').slice(1), ['p', { kind: 'npm', script: 'test:unit', cwd: 'frontend' }])
  await exposed.createTerminal(); assert.equal(calls.filter(item => item[0] === 'createTerminal').length, 1)
})

test('current-file run protects dirty drafts and dispatches supported saved files only', async t => {
  const { s, props, calls, exposed } = await setup(t, { props: { file: { path: 'src/a.py', dirty: true } } })
  assert.equal(await exposed.runCurrent(), false); assert.match(s.error.value, /保存/); assert.equal(calls.some(item => item[0] === 'createRun'), false)
  props.file = { path: 'src/main.mjs', dirty: false }; assert.equal(await exposed.runCurrent(), true)
  assert.deepEqual(calls.find(item => item[0] === 'createRun').slice(1), ['p', { kind: 'node', path: 'src/main.mjs', name: 'src/main.mjs' }])
  props.file = { path: 'src/main.ts', dirty: false }; assert.equal(await exposed.runCurrent(), false)
})

test('delayed discovery results cannot overwrite the next project and no PTY starts while hidden', async t => {
  const first = deferred()
  const { s, props, calls, sockets } = await setup(t, { props: { activeTab: 'output' }, api: { terminals: pid => pid === 'p' ? first.promise : { available: true, shells: [], terminals: [meta('second')] } } })
  props.projectId = 'q'; await settle(); assert.equal(s.terminalId.value, 'second')
  first.resolve({ available: true, shells: [], terminals: [meta('first')] }); await settle()
  assert.equal(s.terminalId.value, 'second'); assert.equal(s.terminals.value.length, 1); assert.equal(sockets.length, 0)
  assert.equal(calls.some(item => item[0] === 'createTerminal'), false)
})

test('terminal switches preserve separate buffers, reconnect seq and release xterm listeners on unmount', async t => {
  const h = await setup(t, { terminals: [meta('one'), meta('two')] })
  const { s, sockets, terms, timers } = h
  await h.host('two'); await h.host('one'); s.terminalId.value = 'one'; await settle()
  const first = sockets.find(item => item.args[1] === 'one'); first.opened(); first.frame({ type: 'output', seq: 1, data: 'one output' })
  assert.equal(terms.find(term => term.output === 'one output').options.disableStdin, false)
  s.terminalId.value = 'two'; await settle(); assert.equal(first.closeCount, 1)
  const second = sockets.findLast(item => item.args[1] === 'two'); second.opened(); second.frame({ type: 'output', seq: 1, data: 'two output' })
  s.terminalId.value = 'one'; await settle(); const resumed = sockets.at(-1)
  assert.deepEqual(resumed.args, ['p', 'one', 1]); resumed.opened(); resumed.frame({ type: 'output', seq: 1, data: 'dup' }); resumed.frame({ type: 'output', seq: 2, data: ' next' })
  assert.equal(terms.find(term => term.output.startsWith('one output')).output, 'one output next'); assert.equal(terms.length, 2)
  h.dispose(); assert.equal(timers.size, 0); assert.equal(s.entries.size, 0)
  assert.ok(terms.every(term => term.disposed && term.disposables.every(item => item.disposed)))
  assert.equal(h.calls.some(item => item[0] === 'closeTerminal'), false)
})

test('lazy terminal import cannot mount an old scope after switching projects or disposal', async t => {
  const delayedModule = deferred()
  const h = await setup(t, { terminals: [meta('same')], terminalModule: delayedModule.promise })
  h.s.setHost('same', { clientWidth: 900, clientHeight: 200 }); await settle()
  h.props.projectId = 'other'; await settle(); h.dispose()
  delayedModule.resolve({ Terminal: h.FakeTerminal }); await settle()
  assert.equal(h.terms.length, 0); assert.equal(h.sockets.length, 0)
})

test('late process creation is ignored after project switch; in-flight stop cannot select an old run', async t => {
  const created = deferred(), stopped = deferred()
  const h = await setup(t, { props: { activeTab: 'run' }, api: { createRun: () => created.promise, stopRun: () => stopped.promise } })
  const pending = h.s.startRun({ kind: 'python', path: 'a.py' }); h.props.projectId = 'next'; await settle()
  created.resolve({ id: 'old', status: 'running' }); assert.equal(await pending, false); assert.equal(h.s.runs.value.some(item => item.id === 'old'), false)
  h.s.runs.value = [meta('run-a'), meta('run-b')]; h.s.runId.value = 'run-a'; await settle()
  // Poll fixture ended run-a; mark it running again to exercise a deliberate stop.
  h.s.runs.value = [meta('run-a'), meta('run-b')]
  const stopping = h.s.stopRun(); h.s.runId.value = 'run-b'; await settle()
  stopped.resolve({ id: 'run-a', status: 'stopped', exit_code: 1 }); await stopping
  assert.equal(h.s.runId.value, 'run-b'); assert.equal(h.s.runs.value.find(item => item.id === 'run-a').status, 'stopped')
})

test('terminal availability failure prevents creation and manual close disposes only that terminal', async t => {
  const h = await setup(t, { terminals: [meta('one'), meta('two')] }); await h.host(h.s.terminalId.value)
  const closed = h.s.terminalId.value; await h.s.closeTerminal(); await settle()
  assert.equal(h.s.terminals.value.some(item => item.id === closed), false); assert.equal(h.s.entries.has(closed), false)
  h.s.terminalInfo.value = { available: false, reason: 'missing' }; assert.equal(await h.exposed.createTerminal(), false)
  assert.equal(h.calls.some(item => item[0] === 'createTerminal'), false)
})

const projectConfig = (id, extra = {}) => ({ id, kind: 'maven', script: 'spring-boot:run', cwd: 'backend', source: 'project', name: 'Backend · Spring Boot', available: true, ...extra })

test('run configurations appear and can run while terminal and history discovery remain pending', async t => {
  const terminals = deferred(), history = deferred()
  const h = await setup(t, { props: { activeTab: 'run', file: { path: 'backend/src/AgentService.java' } }, api: {
    terminals: () => terminals.promise, runs: () => history.promise, runConfigs: () => ({ configs: [projectConfig('boot')] }),
  } })
  assert.equal(h.s.configs.value.length, 1)
  assert.equal(h.s.configId.value, 'boot')
  assert.equal(h.s.loading.value, false)
  assert.equal(h.s.loadStates.value.terminals.loading, true)
  assert.equal(h.s.loadStates.value.runs.loading, true)
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false, 'selecting a default is not execution')
  assert.equal(await h.s.runForm(), true)
  assert.deepEqual(h.calls.find(call => call[0] === 'createRun').slice(1), ['p', { kind: 'maven', script: 'spring-boot:run', cwd: 'backend', name: 'Backend · Spring Boot' }])
})

test('terminal readiness does not wait for run configurations or history', async t => {
  const configs = deferred(), runs = deferred()
  const h = await setup(t, { terminals: [meta('ready')], api: { runConfigs: () => configs.promise, runs: () => runs.promise } })
  assert.equal(h.s.terminalId.value, 'ready')
  assert.equal(h.s.loading.value, false)
  assert.equal(h.s.loadStates.value.configs.loading, true)
  assert.equal(await h.exposed.createTerminal(), true)
})

test('repeated refresh reuses each pending request rather than overlapping discovery', async t => {
  const response = deferred()
  const h = await setup(t, { api: { runConfigs: () => response.promise } })
  const first = h.s.loadSection('configs'), second = h.s.loadSection('configs')
  h.exposed.refresh(); h.exposed.refresh()
  assert.equal(first, second)
  assert.equal(h.calls.filter(call => call[0] === 'runConfigs').length, 1)
  response.resolve({ configs: [projectConfig('boot')] })
  assert.equal(await first, true)
  assert.equal(h.s.loadStates.value.configs.loading, false)
})

test('configuration timeout exposes retry, aborts the fetch, and cannot accept a late old response', async t => {
  const first = deferred(), signals = []; let attempt = 0
  const h = await setup(t, { props: { activeTab: 'run' }, api: { runConfigs: (_pid, options) => {
    signals.push(options.signal)
    return ++attempt === 1 ? first.promise : { configs: [projectConfig('retry')] }
  } } })
  const request = h.s.loadRequests.get('configs')
  assert.equal(h.timers.get(request.timer).delay, 15_000)
  h.timers.get(request.timer).fn()
  await settle()
  assert.equal(h.s.loadStates.value.configs.loading, false)
  assert.match(h.s.loadStates.value.configs.error, /读取超时.*重试/)
  assert.equal(signals[0].aborted, true)
  assert.equal(h.events.some(([name]) => name === 'error'), false, 'discovery has an independent section error')
  assert.equal(await h.s.loadSection('configs'), true)
  first.resolve({ configs: [projectConfig('late-old')] })
  await settle()
  assert.equal(h.s.configs.value[0].id, 'retry')
  assert.equal(h.s.loadStates.value.configs.error, '')
})

test('each failed discovery section can retry without clearing successful sections', async t => {
  let fail = true
  const h = await setup(t, { props: { activeTab: 'run' }, api: {
    terminals: () => { if (fail) throw new Error('terminal unavailable'); return { terminals: [meta('new')], shells: [] } },
    runConfigs: () => ({ configs: [projectConfig('boot')] }),
  } })
  assert.match(h.s.loadStates.value.terminals.error, /terminal unavailable/)
  assert.equal(h.s.configs.value[0].id, 'boot')
  assert.equal(h.s.loadStates.value.configs.error, '')
  fail = false
  await h.s.loadSection('terminals')
  assert.equal(h.s.terminalId.value, 'new')
  assert.equal(h.s.configs.value[0].id, 'boot')
  assert.equal(h.calls.filter(call => call[0] === 'runConfigs').length, 1)
})

test('project switch aborts pending discovery and does not expose stale results or errors', async t => {
  const first = deferred(), signals = []
  const h = await setup(t, { api: { runConfigs: (pid, options) => {
    if (pid === 'p') { signals.push(options.signal); return first.promise }
    return { configs: [projectConfig('next')] }
  } } })
  h.props.projectId = 'next'; await settle()
  assert.equal(signals[0].aborted, true)
  first.resolve({ configs: [projectConfig('previous')] }); await settle()
  assert.equal(h.s.configs.value[0].id, 'next')
  assert.equal(h.s.loadStates.value.configs.error, '')
})

test('unmount cancels all discovery timeouts and aborts pending fetches', async t => {
  const pending = deferred(), signals = []
  const capture = (_pid, options) => { signals.push(options.signal); return pending.promise }
  const h = await setup(t, { api: { terminals: capture, runConfigs: capture, runs: capture } })
  assert.equal(h.s.loadRequests.size, 3)
  h.dispose(); await settle()
  assert.equal(h.timers.size, 0)
  assert.equal(h.s.loadRequests.size, 0)
  assert.equal(signals.every(signal => signal.aborted), true)
  pending.resolve({ configs: [projectConfig('late')] }); await settle()
  assert.equal(h.s.configs.value.length, 0)
})

test('Java current-file entry selects the nearest module project config and requires a second deliberate run', async t => {
  const h = await setup(t, { props: { activeTab: 'run', file: { path: 'backend/service/src/main/java/AgentService.java', dirty: false } }, api: {
    runConfigs: () => ({ configs: [projectConfig('root', { cwd: '.', name: 'Root app' }), projectConfig('compile', { cwd: 'backend/service', script: 'compile', name: 'Compile service' }),
      projectConfig('service', { cwd: 'backend/service', kind: 'gradle', script: 'bootRun', name: 'Service app' })] }),
  } })
  assert.equal(await h.exposed.runCurrent(), false)
  assert.equal(h.s.configId.value, 'service')
  assert.equal(h.s.kind.value, 'gradle')
  assert.equal(h.s.target.value, 'bootRun')
  assert.equal(h.s.runCwd.value, 'backend/service')
  assert.match(h.s.notice.value, /Service app.*点击.*运行/)
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false)
  await h.s.runForm()
  assert.deepEqual(h.calls.find(call => call[0] === 'createRun').slice(1), ['p', { kind: 'gradle', script: 'bootRun', cwd: 'backend/service', name: 'Service app' }])
})

test('Java service without a project entry gives useful guidance and never dispatches a single-file runner', async t => {
  const h = await setup(t, { props: { activeTab: 'run', file: { path: 'backend/src/AgentService.java', dirty: false } }, api: { runConfigs: () => ({ configs: [] }) } })
  assert.equal(await h.exposed.runCurrent(), false)
  assert.match(h.s.notice.value, /Maven.*Gradle.*Service/)
  assert.equal(h.s.error.value, '')
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false)
})

test('TS project entry guides to npm while Python and JavaScript retain their existing direct runner', async t => {
  const h = await setup(t, { props: { activeTab: 'run', file: { path: 'frontend/src/app.ts', dirty: false } }, api: {
    runConfigs: () => ({ configs: [projectConfig('npm', { kind: 'npm', script: 'dev', cwd: 'frontend', name: 'Frontend dev' })] }),
  } })
  assert.equal(await h.exposed.runCurrent(), false)
  assert.equal(h.s.configId.value, 'npm')
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false)
  h.props.file = { path: 'frontend/src/app.js', dirty: false }
  assert.equal(await h.exposed.runCurrent(), true)
  assert.equal(h.calls.find(call => call[0] === 'createRun')[2].kind, 'node')
})

test('unavailable project entry exposes its reason and blocks process creation', async t => {
  const h = await setup(t, { props: { activeTab: 'run', file: { path: 'backend/AgentService.java', dirty: false } }, api: {
    runConfigs: () => ({ configs: [projectConfig('boot', { available: false, reason: 'Install Maven first' })] }),
  } })
  await h.exposed.runCurrent()
  assert.equal(h.s.configId.value, 'boot')
  assert.equal(await h.s.runForm(), false)
  assert.match(h.s.error.value, /Install Maven/)
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false)
})

test('Maven and Gradle goals use the script payload and their test configs are available in Tests', async t => {
  const h = await setup(t, { props: { activeTab: 'tests' }, api: {
    runConfigs: () => ({ configs: [projectConfig('compile', { script: 'compile' }), projectConfig('test', { script: 'test', name: 'Maven tests' })] }),
  } })
  assert.equal(h.s.kind.value, 'maven')
  assert.equal(h.s.target.value, 'test')
  await h.s.runForm()
  assert.deepEqual(h.calls.find(call => call[0] === 'createRun')[2], { kind: 'maven', script: 'test', cwd: 'backend', name: 'Maven tests' })
})

test('large file-entry lists stay in a single grouped selector with project entries first', async t => {
  const files = Array.from({ length: 100 }, (_, i) => ({ id: `js-${i}`, source: 'file', kind: 'node', path: `src/${i}.js`, name: `JS ${i}` }))
  const h = await setup(t, { api: { runConfigs: () => ({ configs: [...files, projectConfig('boot')] }) } })
  assert.equal(h.s.configGroups.value.length, 2)
  assert.equal(h.s.configGroups.value[0].items[0].id, 'boot')
  assert.equal(h.s.configGroups.value[1].items.length, 100)
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false)
})

test('late discovery preserves a manually edited command form', async t => {
  const pending = deferred()
  const h = await setup(t, { props: { activeTab: 'run' }, api: { runConfigs: () => pending.promise } })
  h.s.kind.value = 'command'; h.s.target.value = 'echo manual'; h.s.formTouched.value = true
  pending.resolve({ configs: [projectConfig('boot')] }); await settle()
  assert.equal(h.s.kind.value, 'command')
  assert.equal(h.s.target.value, 'echo manual')
})

test('mixed-repository Tests chooses the nearest Java module rather than root pytest', async t => {
  const h = await setup(t, { props: { activeTab: 'tests', file: { path: 'backend/service/src/main/java/AgentService.java' } }, api: {
    runConfigs: () => ({ configs: [
      { id: 'pytest', kind: 'pytest', cwd: '.', path: 'tests', name: 'Python tests', available: true },
      projectConfig('backend-tests', { script: 'test', name: 'Backend Maven tests' }),
      projectConfig('service-tests', { kind: 'gradle', script: 'test', cwd: 'backend/service', name: 'Service Gradle tests' }),
      projectConfig('npm-tests', { kind: 'npm', script: 'test', cwd: 'frontend', name: 'Frontend tests' }),
    ] }),
  } })
  assert.equal(h.s.configId.value, 'service-tests')
  assert.equal(h.s.kind.value, 'gradle')
  assert.equal(h.s.runCwd.value, 'backend/service')
  h.props.file = { path: 'scripts/check.py' }
  h.exposed.showTests()
  assert.equal(h.s.configId.value, 'pytest', 'root pytest is relevant for Python files')
  h.props.file = { path: 'frontend/src/app.ts' }
  h.exposed.showTests()
  assert.equal(h.s.configId.value, 'npm-tests')
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false)
})

test('unsupported-file guidance accepts same-path metadata rerenders during configuration loading', async t => {
  const pending = deferred()
  const h = await setup(t, { props: { activeTab: 'run', file: { path: 'backend/AgentService.java' } }, api: { runConfigs: () => pending.promise } })
  const first = h.exposed.runCurrent()
  h.props.file = { ...h.props.file, size: 1200 }
  pending.resolve({ configs: [projectConfig('boot')] })
  assert.equal(await first, false)
  assert.match(h.s.notice.value, /Spring Boot.*点击.*运行/, 'metadata replacement keeps the logical document identity')
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false)
})

test('unsupported-file guidance ignores a different file selected during configuration loading', async t => {
  const pending = deferred()
  const h = await setup(t, { props: { activeTab: 'run', file: { path: 'backend/AgentService.java' } }, api: { runConfigs: () => pending.promise } })
  const first = h.exposed.runCurrent()
  h.props.file = { path: 'frontend/app.ts' }
  pending.resolve({ configs: [projectConfig('boot')] })
  assert.equal(await first, false)
  assert.equal(h.s.notice.value, '')
  assert.equal(h.calls.some(call => call[0] === 'createRun'), false)
})

test('opening settings pauses run reads and returning resumes after the retained output cursor without duplicates', async t => {
  const h = await setup(t, { props: { activeTab: 'run' }, api: {
    runs: () => ({ runs: [meta('existing-run')] }),
    run: (_pid, id, afterSeq) => ({ id, status: 'running', next_seq: afterSeq ? 2 : 1,
      events: afterSeq ? [{ type: 'output', seq: 1, data: 'duplicate' }, { type: 'output', seq: 2, data: ' second' }]
        : [{ type: 'output', seq: 1, data: 'first' }] }),
  } })
  assert.equal(h.s.output.value, 'first')
  const buffer = h.s.outputs.get('existing-run')
  const queuedPoll = Array.from(h.timers.values()).find(timer => timer.delay === 500)
  assert.ok(queuedPoll)
  h.props.visible = false
  await settle()
  assert.equal(Array.from(h.timers.values()).some(timer => timer.delay === 500), false)
  const requestsBefore = h.calls.filter(([name]) => name === 'run').length
  queuedPoll.fn()
  await settle()
  assert.equal(h.calls.filter(([name]) => name === 'run').length, requestsBefore, 'a cleared callback cannot restart hidden polling')
  assert.equal(h.s.output.value, 'first')
  assert.equal(h.s.runs.value[0].status, 'running')
  assert.equal(h.calls.some(([name]) => ['stopRun', 'closeTerminal', 'createRun'].includes(name)), false)
  h.props.visible = true
  await settle()
  assert.deepEqual(h.calls.filter(([name]) => name === 'run').map(call => call.slice(1)), [['p', 'existing-run', 0], ['p', 'existing-run', 1]])
  assert.equal(h.s.output.value, 'first second')
  assert.equal(h.s.outputs.get('existing-run'), buffer)
  assert.equal(buffer.seq, 2)
})

test('a run response finishing after settings open cannot alter output or rearm the read timer', async t => {
  const pending = deferred()
  let reads = 0
  const h = await setup(t, { props: { activeTab: 'run' }, api: {
    runs: () => ({ runs: [meta('existing-run')] }),
    run: (_pid, id) => ++reads === 1 ? pending.promise : { id, status: 'running', next_seq: 1, events: [{ type: 'output', seq: 1, data: 'current' }] },
  } })
  assert.equal(reads, 1)
  h.props.visible = false
  await settle()
  pending.resolve({ id: 'existing-run', status: 'exited', exit_code: 8, next_seq: 1, events: [{ type: 'output', seq: 1, data: 'stale' }] })
  await settle()
  assert.equal(h.s.output.value, '')
  assert.equal(h.s.runs.value[0].status, 'running')
  assert.equal(Array.from(h.timers.values()).some(timer => timer.delay === 500), false)
  assert.equal(h.calls.some(([name]) => name === 'stopRun'), false)
  h.props.visible = true
  await settle()
  assert.equal(reads, 2)
  assert.equal(h.s.output.value, 'current')
})

test('run selection while settings are visible waits until the coding workspace returns', async t => {
  const h = await setup(t, { props: { activeTab: 'run', visible: false }, api: {
    runs: () => ({ runs: [meta('one'), meta('two')] }),
    run: (_pid, id) => ({ id, status: 'running', events: [{ type: 'output', seq: 1, data: id }], next_seq: 1 }),
  } })
  assert.equal(h.s.runId.value, 'one')
  assert.equal(h.calls.some(([name]) => name === 'run'), false)
  h.s.runId.value = 'two'
  await settle()
  assert.equal(h.calls.some(([name]) => name === 'run'), false)
  h.props.visible = true
  await settle()
  assert.deepEqual(h.calls.filter(([name]) => name === 'run').map(call => call.slice(1)), [['p', 'two', 0]])
  assert.equal(h.s.output.value, 'two')
  assert.equal(h.calls.some(([name]) => name === 'stopRun'), false)
})

test('hiding the coding workspace retains an active terminal connection and buffer without stopping its process', async t => {
  const h = await setup(t, { terminals: [meta('terminal-one')] })
  await h.host('terminal-one')
  const terminal = h.terms[0], socket = h.sockets[0], entry = h.s.entries.get('terminal-one')
  socket.opened()
  socket.frame({ type: 'output', seq: 1, data: 'before settings' })
  h.props.visible = false
  await settle()
  socket.frame({ type: 'output', seq: 2, data: ' while hidden' })
  assert.equal(terminal.output, 'before settings while hidden')
  assert.equal(terminal.disposed, undefined)
  assert.equal(socket.closeCount, 0)
  assert.equal(h.s.entries.get('terminal-one'), entry)
  h.props.visible = true
  await settle()
  assert.equal(h.terms.length, 1)
  assert.equal(h.sockets.length, 1)
  assert.equal(h.calls.some(([name]) => ['closeTerminal', 'stopRun', 'createTerminal'].includes(name)), false)
})
