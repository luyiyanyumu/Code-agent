import test from 'node:test'
import assert from 'node:assert/strict'
import { candidateService, createIntegrationObserver, integrationDeadline, integrationForm, integrationPayload, integrationStatus, jsonObject } from '../src/lib/integrationClient.js'

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settle = () => new Promise(setImmediate)
function clock() {
  const timers = new Map(); let id = 0
  return { timers, schedule: fn => { timers.set(++id, fn); return id }, unschedule: key => timers.delete(key), fire() { const [key, fn] = timers.entries().next().value; timers.delete(key); fn() } }
}

test('discovered Maven argv does not become a custom shell command; only supported role fields persist', () => {
  const form = integrationForm({ frontend_url: 'http://localhost:5173/', backend_url: 'http://localhost:8080/' })
  form.backend = candidateService({ id: 'maven', kind: 'maven', cwd: 'server', script: 'spring-boot:run', command: ['mvn', 'spring-boot:run'], name: 'Spring Boot', available: true }, form.backend)
  form.frontend = candidateService({ id: 'vite', kind: 'npm', cwd: 'web', script: 'dev', command: ['npm', 'run', 'dev'] }, form.frontend)
  form.backend.args_text = '["-DskipTests"]'; form.backend.env_text = '{"PORT":"8080"}'
  const value = integrationPayload(form)
  assert.equal(value.backend.command, ''); assert.equal(value.frontend.command, '')
  assert.deepEqual(value.backend.args, ['-DskipTests']); assert.deepEqual(value.backend.env, { PORT: '8080' })
  assert.deepEqual(Object.keys(value.backend).sort(), ['mode', 'kind', 'cwd', 'path', 'script', 'command', 'args', 'env'].sort())
  assert.equal(value.backend.cwd, 'server'); assert.equal(value.backend.script, 'spring-boot:run')
})

test('attach mode and explicit custom args/env round trip; malformed structures stop submission', () => {
  const form = integrationForm({ backend: { mode: 'attach' }, frontend: { kind: 'command', command: 'npm run dev', args: ['--host', '127.0.0.1'], env: { PORT: '5173' } } })
  assert.equal(integrationPayload(form).backend.mode, 'attach')
  assert.deepEqual(integrationPayload(form).frontend.args, ['--host', '127.0.0.1'])
  form.frontend.args_text = '[12]'; assert.throws(() => integrationPayload(form), /字符串/)
  form.frontend.args_text = '[]'; form.frontend.env_text = '{"PORT":5173}'; assert.throws(() => integrationPayload(form), /字符串键值/)
  form.frontend.env_text = '{}'; form.startup_timeout = -1; assert.throws(() => integrationPayload(form), /1–300/)
  assert.throws(() => jsonObject('[]', '请求头'), /JSON 对象/)
  assert.equal(integrationStatus('pending'), '等待中'); assert.equal(integrationStatus('unhealthy'), '连接异常')
})

test('snapshot observation is single flight and ignores a late response/error from another project', async () => {
  const c = clock(), pending = [], states = [], errors = []
  const observer = createIntegrationObserver({ ...c, load: (pid, options) => { const value = deferred(); pending.push({ ...value, pid, signal: options.signal }); return value.promise }, onState: (value, pid) => states.push([pid, value]), onError: value => errors.push(value) })
  const first = observer.select('a'); observer.refresh(); assert.equal(pending.length, 1)
  const second = observer.select('b'); observer.refresh(); assert.equal(pending.length, 2); assert.equal(pending[0].signal.aborted, true)
  pending[0].resolve({ session: { id: 'old' } }); await first; assert.deepEqual(states, [])
  pending[1].resolve({ session: { id: 'current' } }); await second; assert.deepEqual(states, [['b', { session: { id: 'current' } }]])
  assert.equal(c.timers.size, 1); c.fire(); await settle(); assert.equal(pending[2].pid, 'b')
  observer.stop(); pending[2].reject(new Error('late')); await settle(); assert.deepEqual(errors, []); assert.equal(c.timers.size, 0)
})

test('observation retries a failed read, while hiding cancels its scheduled polling', async () => {
  const c = clock(), states = [], errors = []; let count = 0
  const observer = createIntegrationObserver({ ...c, load: async () => { if (++count === 1) throw new Error('offline'); return { session: null } }, onState: value => states.push(value), onError: value => errors.push(value.message) })
  await observer.select('a'); assert.deepEqual(errors, ['offline']); assert.equal(c.timers.size, 1)
  c.fire(); await settle(); assert.deepEqual(states, [{ session: null }]); observer.dispose(); assert.equal(c.timers.size, 0)
})

test('a timed out write is never replayed, and a pre-cancelled action does not run', async () => {
  let writes = 0
  await assert.rejects(integrationDeadline(() => { writes++; return new Promise(() => {}) }, { timeoutMs: 5, message: 'result unknown' }), /result unknown/)
  assert.equal(writes, 1)
  const controller = new AbortController(); controller.abort()
  await assert.rejects(integrationDeadline(() => { writes++ }, { signal: controller.signal }), { name: 'AbortError' })
  assert.equal(writes, 1)
  const switching = new AbortController()
  const pending = integrationDeadline(() => { writes++ }, { signal: switching.signal }); switching.abort()
  await assert.rejects(pending, { name: 'AbortError' }); assert.equal(writes, 1)
})
