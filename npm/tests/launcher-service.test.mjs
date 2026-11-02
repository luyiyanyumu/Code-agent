import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import net from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { LauncherServices, redactLog, tailLog } from '../launcher-service.mjs'

const release = { version: '0.10.2', payloadHash: 'a'.repeat(64) }
const identities = {
  backend: { pid: 222, created: '2026-10-01T01:02:03.0000000Z', owned: true },
  frontend: { pid: 333, created: '2026-10-01T01:02:04.0000000Z', owned: true },
}

async function unusedPort() {
  const server = net.createServer()
  await new Promise((done, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', done) })
  const port = server.address().port
  await new Promise(done => server.close(done))
  return port
}

async function fixture(t, { mode = 'source', snapshot = identities, guardCodes = [], stopCode = 0, direct = false } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'code agent launcher services '))
  t.after(() => rm(root, { recursive: true, force: true }))
  await mkdir(join(root, 'npm'), { recursive: true })
  if (direct) {
    await mkdir(join(root, 'scripts'), { recursive: true })
    await writeFile(join(root, 'scripts', 'launcher_process.py'), '# mocked Windows API helper\n')
  }
  await writeFile(join(root, 'npm', 'release.json'), JSON.stringify(release))
  const calls = [], spawns = [], logs = []
  let guardIndex = 0
  const service = new LauncherServices({
    root, home: join(root, 'persistent home'), mode, platform: 'win32', env: { EXAMPLE: 'kept' },
    backendPort: await unusedPort(), frontendPort: await unusedPort(),
    run: async (command, args, options) => {
      calls.push({ command, args, options })
      if (command === 'powershell.exe' || args.includes(join(root, 'scripts', 'launcher_process.py'))) {
        const action = args[args.indexOf('-Action') + 1]
        return action === 'Inspect'
          ? { code: 0, stdout: JSON.stringify(snapshot) }
          : { code: stopCode, stdout: JSON.stringify(stopCode ? { error: 'identity changed' } : { stopped: true }) }
      }
      assert.equal(command, service.python)
      assert.equal(args[0], join(root, 'scripts', 'backend_idle_check.py'))
      return { code: guardCodes[guardIndex++] ?? 0, stdout: '{"idle":true}' }
    },
    spawnProcess: (...args) => { spawns.push(args); assert.fail('This test must not create a real service process') },
    log: value => logs.push(value),
  })
  await service.initialize()
  return { root, service, calls, spawns, logs }
}

const actionOf = call => call.args.includes('-Action') ? call.args[call.args.indexOf('-Action') + 1] : 'Guard'

test('direct Windows inspection and stop keep fresh identity checks and both backend idle checks', async t => {
  const { root, service, calls } = await fixture(t, { direct: true })
  await service.stop('backend')
  assert.deepEqual(calls.map(actionOf), ['Inspect', 'Guard', 'Guard', 'Stop'])
  const processCalls = calls.filter(call => actionOf(call) !== 'Guard')
  for (const call of processCalls) {
    assert.equal(call.command, service.python)
    assert.deepEqual(call.args.slice(0, 6), ['-I', '-S', '-B', '-X', 'utf8', join(root, 'scripts', 'launcher_process.py')])
    assert.equal(call.options.timeout, 15000)
    assert.equal(call.args[call.args.indexOf('-BackendPort') + 1], String(service.services[0].port))
  }
  const stop = processCalls.at(-1)
  assert.equal(stop.args[stop.args.indexOf('-ExpectedPid') + 1], String(identities.backend.pid))
  assert.equal(stop.args[stop.args.indexOf('-ExpectedCreated') + 1], identities.backend.created)
  assert.equal(calls.some(call => call.command === 'powershell.exe'), false)
})

test('direct process-helper failure never falls back to another stopping path', async t => {
  const { service, calls } = await fixture(t, { direct: true, stopCode: 1 })
  await assert.rejects(service.stop('backend'), /不能安全停止/)
  assert.deepEqual(calls.map(actionOf), ['Inspect', 'Guard', 'Guard', 'Stop'])
  assert.equal(calls.some(call => call.command === 'powershell.exe'), false)
})

test('direct inspection failure cannot stop services using an earlier cached identity', async t => {
  const { service, calls } = await fixture(t, { direct: true })
  await service.identities()
  const run = service.run
  service.run = async (...args) => { await run(...args); return { code: 1, stdout: '{"error":"unavailable"}' } }
  await assert.rejects(service.stop('backend'), /无法读取服务进程身份/)
  assert.deepEqual(calls.map(actionOf), ['Inspect', 'Inspect'])
})

test('Windows backend stop requires two idle checks and forwards the exact verified process identity', async t => {
  const { root, service, calls } = await fixture(t)
  await service.stop('backend')
  assert.deepEqual(calls.map(actionOf), ['Inspect', 'Guard', 'Guard', 'Stop'])
  const stop = calls.at(-1)
  assert.equal(stop.args[stop.args.indexOf('-Service') + 1], 'backend')
  assert.equal(stop.args[stop.args.indexOf('-ExpectedPid') + 1], '222')
  assert.equal(stop.args[stop.args.indexOf('-ExpectedCreated') + 1], identities.backend.created)
  assert.equal(stop.args[stop.args.indexOf('-BackendPython') + 1], service.python)
  assert.equal(stop.args[stop.args.indexOf('-Root') + 1], root)
  for (const call of calls.filter(call => actionOf(call) === 'Guard')) {
    assert.equal(call.options.cwd, root)
    assert.equal(call.options.env.PYTHONIOENCODING, 'utf-8')
    assert.equal(call.options.env.EXAMPLE, 'kept')
    assert.equal(call.options.capture, true)
  }
  assert.equal(service.states.size, 0)
})

test('frontend stop targets only frontend and does not run the backend task guard', async t => {
  const { service, calls } = await fixture(t)
  await service.stop('frontend')
  assert.deepEqual(calls.map(actionOf), ['Inspect', 'Stop'])
  const stop = calls.at(-1)
  assert.equal(stop.args[stop.args.indexOf('-Service') + 1], 'frontend')
  assert.equal(stop.args[stop.args.indexOf('-ExpectedPid') + 1], '333')
  assert.equal(stop.args[stop.args.indexOf('-ExpectedCreated') + 1], identities.frontend.created)
})

for (const action of ['stop', 'restart']) {
  test(`a busy backend preserves both services during ${action}-all`, async t => {
    const { service, calls, spawns } = await fixture(t, { guardCodes: [12] })
    const started = []
    service.start = async id => started.push(id)
    await assert.rejects(service.action(action, 'all'), /后端仍有任务/)
    assert.deepEqual(calls.map(actionOf), ['Inspect', 'Guard'])
    assert.equal(calls.some(call => call.args.includes('frontend')), false)
    assert.deepEqual(started, [])
    assert.deepEqual(spawns, [])
    assert.equal(service.states.size, 0)
  })
}

test('new work appearing during the second idle check prevents every stop', async t => {
  const { service, calls } = await fixture(t, { guardCodes: [0, 12] })
  await assert.rejects(service.stop('backend'), /后端仍有任务/)
  assert.deepEqual(calls.map(actionOf), ['Inspect', 'Guard', 'Guard'])
  assert.equal(service.states.size, 0)
})

test('unknown idle state fails closed without issuing a stop', async t => {
  const { service, calls } = await fixture(t, { guardCodes: [13] })
  await assert.rejects(service.stop('backend'), /无法确认后端任务已空闲/)
  assert.deepEqual(calls.map(actionOf), ['Inspect', 'Guard'])
})

test('installed backend idle inspection uses its persistent home database', async t => {
  const { service, calls } = await fixture(t, { mode: 'installed' })
  await service.stop('backend')
  for (const call of calls.filter(call => actionOf(call) === 'Guard')) {
    assert.equal(call.args[call.args.indexOf('--db') + 1], join(service.home, 'code_agent.db'))
  }
})

test('foreign service identities are refused before idle inspection or stopping', async t => {
  const { service, calls } = await fixture(t, { snapshot: { ...identities, backend: { ...identities.backend, owned: false } } })
  await assert.rejects(service.stop('backend'), /其他项目/)
  assert.deepEqual(calls.map(actionOf), ['Inspect'])
})

for (const healthy of [true, false]) {
  test(`a foreign ${healthy ? 'healthy' : 'unhealthy'} listener cannot be replaced on start`, async t => {
    const foreign = { backend: { ...identities.backend, owned: false }, frontend: { ...identities.frontend, owned: false } }
    const { service, calls, spawns } = await fixture(t, { snapshot: foreign })
    t.mock.method(globalThis, 'fetch', async url => new Response(
      String(url).includes('/healthz') ? JSON.stringify({ ok: healthy, version: '0.10.2' }) : '<!doctype html><title>Code-agent</title>',
      { status: healthy ? 200 : 503, headers: { 'content-type': 'application/json' } },
    ))
    const status = await service.status()
    assert.equal(status[0].state, 'foreign')
    assert.equal(status[1].state, 'foreign')
    await assert.rejects(service.start('backend'), /端口已占用/)
    await assert.rejects(service.start('frontend'), /端口已占用/)
    assert.equal(calls.every(call => actionOf(call) === 'Inspect'), true)
    assert.deepEqual(spawns, [])
  })
}

test('a failed process stop cancels restart and clears transient stopping state', async t => {
  const { service, calls } = await fixture(t, { stopCode: 1 })
  const started = []
  service.start = async id => started.push(id)
  await assert.rejects(service.action('restart', 'all'), /不能安全停止/)
  assert.deepEqual(calls.map(actionOf), ['Inspect', 'Guard', 'Guard', 'Stop'])
  assert.deepEqual(started, [])
  assert.equal(service.states.size, 0)
  assert.equal(service.identityCache, null)
})

test('healthy owned services are reused instead of spawning a duplicate', async t => {
  const { service, spawns } = await fixture(t)
  t.mock.method(globalThis, 'fetch', async url => new Response(String(url).includes('/healthz')
    ? JSON.stringify({ ok: true, version: '0.10.2' }) : '<!doctype html><title>Code-agent</title>'))
  await service.action('start', 'all')
  assert.deepEqual(spawns, [])
})

test('source start uses literal executable arguments and a hidden detached child', async t => {
  const { root, service, spawns } = await fixture(t)
  await mkdir(dirname(service.python), { recursive: true })
  await writeFile(service.python, 'placeholder, never executed')
  service.status = async () => service.services.map(item => ({ ...item, state: 'stopped' }))
  t.mock.method(globalThis, 'fetch', async () => new Response('{"ok":true,"version":"0.10.2"}'))
  let unrefed = false
  service.spawnProcess = (...args) => {
    spawns.push(args)
    const child = new EventEmitter()
    Object.assign(child, { pid: 444, exitCode: null, unref() { unrefed = true } })
    queueMicrotask(() => child.emit('spawn'))
    return child
  }
  await service.start('backend')
  assert.equal(spawns.length, 1)
  assert.equal(spawns[0][0], service.python)
  assert.deepEqual(spawns[0][1], ['-m', 'code_agent.server', '--host', '127.0.0.1', '--port', String(service.services[0].port)])
  assert.equal(spawns[0][2].cwd, root)
  assert.equal(spawns[0][2].shell, false)
  assert.equal(spawns[0][2].windowsHide, true)
  assert.equal(spawns[0][2].detached, true)
  assert.equal(unrefed, true)
  assert.equal(service.states.size, 0)
  assert.equal(service.children.get('backend').pid, 444)
})

test('unknown actions and targets are rejected without querying or stopping services', async t => {
  const { service, calls } = await fixture(t)
  await assert.rejects(service.action('stop', 'arbitrary-pid'), /未知服务/)
  await assert.rejects(service.action('kill', 'backend'), /未知服务操作/)
  assert.deepEqual(calls, [])
})

test('missing logs return an honest empty result and bounded tails retain only requested lines', async t => {
  const { root } = await fixture(t)
  const path = join(root, 'log with spaces.log')
  assert.deepEqual(await tailLog(path), { path, text: '' })
  await writeFile(path, 'line-1\nline-2\nline-3\nline-4\n')
  const result = await tailLog(path, 2)
  assert.deepEqual(result.text.trimEnd().split('\n'), ['line-3', 'line-4'])
  assert.equal(await readFile(path, 'utf8'), 'line-1\nline-2\nline-3\nline-4\n')
})

test('log reads cap bytes and lines while redacting secrets in the returned tail', async t => {
  const { root } = await fixture(t)
  const path = join(root, 'large.log')
  await writeFile(path, 'old text\n'.repeat(80000) + 'api_key=unit-test-private-value\nlast-line')
  const result = await tailLog(path, 1000000)
  assert.ok(Buffer.byteLength(result.text, 'utf8') <= 256 * 1024)
  assert.ok(result.text.split('\n').length <= 1000)
  assert.match(result.text, /last-line$/)
  assert.doesNotMatch(result.text, /unit-test-private-value/)
  assert.match(result.text, /已隐藏/)
})

test('log redaction strips terminal control colors, header credentials, env secrets and credential URLs', () => {
  const value = '\u001b[31merror\u001b[0m\nBearer unit-test-bearer\nOPENAI_API_KEY=unit-test-key\npassword="unit-test-pass"\nhttps://alice:unit-test-password@example.test/path\nsk-unit_test_private_key_12345'
  const result = redactLog(value)
  assert.doesNotMatch(result, /\u001b|unit-test-bearer|unit-test-key|unit-test-pass|unit-test-password|unit_test_private_key_12345/)
  assert.match(result, /error/)
  assert.match(result, /example\.test\/path/)
})

test('quoted JSON credential fields are redacted before logs reach the browser', () => {
  const result = redactLog('{"api_key":"unit-test-json-key","access_token": "unit-test-json-token", "password": "unit-test-json-password"}')
  assert.doesNotMatch(result, /unit-test-json-/)
})

test('an old in-flight Windows inspection cannot replace the cache after lifecycle invalidation', { timeout: 2000 }, async t => {
  const { service } = await fixture(t)
  const previous = { ...identities, frontend: { pid: null, created: null, owned: false } }
  const current = { ...identities, frontend: { ...identities.frontend, pid: 444, created: '2026-10-01T01:03:00.0000000Z' } }
  let releaseOld, inspections = 0
  const oldSnapshotReady = new Promise(done => { releaseOld = done })
  service.run = async (command, args) => {
    assert.equal(command, 'powershell.exe')
    assert.equal(args[args.indexOf('-Action') + 1], 'Inspect')
    const snapshot = ++inspections === 1 ? await oldSnapshotReady : current
    return { code: 0, stdout: JSON.stringify(snapshot) }
  }
  const oldRequest = service.identities()
  assert.equal(inspections, 1)
  service.invalidateIdentity()
  assert.deepEqual(await service.identities(), current)
  assert.equal(service.identityCache.value.frontend.pid, 444)
  releaseOld(previous)
  assert.deepEqual(await oldRequest, previous)
  assert.deepEqual(service.identityCache.value, current)
  assert.deepEqual(await service.identities(), current)
  assert.equal(inspections, 2, 'the latest cache is reused after the stale request finishes')
})

test('frontend health retries one stale socket and remains independent of the backend', async t => {
  const { service } = await fixture(t)
  const requests = []
  t.mock.method(globalThis, 'fetch', async url => {
    requests.push(String(url))
    if (requests.length === 1) throw new TypeError('fetch failed: reused socket closed')
    return new Response(JSON.stringify({ ok: true, service: 'code-agent-frontend' }))
  })
  const result = await service.probe(service.services[1])
  assert.equal(result.healthy, true)
  assert.equal(requests.length, 2)
  assert.ok(requests.every(url => url.endsWith('/__code_agent_health')))
})

test('HTTP frontend failures remain unhealthy without retry or backend fallback', async t => {
  const { service } = await fixture(t)
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return new Response('not ready', { status: 503 }) })
  const result = await service.probe(service.services[1])
  assert.equal(result.healthy, false)
  assert.equal(requests, 1)
  assert.match(result.detail, /503/)
})

test('status retries a slow pre-start snapshot after lifecycle identity invalidation', { timeout: 2000 }, async t => {
  const { service } = await fixture(t)
  const previous = { ...identities, frontend: { pid: null, created: null, owned: false } }
  const current = { ...identities, frontend: { ...identities.frontend, pid: 444, created: '2026-10-01T01:03:00.0000000Z' } }
  let releaseOld, inspections = 0
  const oldSnapshotReady = new Promise(done => { releaseOld = done })
  service.run = async () => ({ code: 0, stdout: JSON.stringify(++inspections === 1 ? await oldSnapshotReady : current) })
  t.mock.method(globalThis, 'fetch', async url => new Response(String(url).includes('/healthz')
    ? JSON.stringify({ ok: true, version: '0.10.2' }) : '<!doctype html><title>Code-agent</title>'))
  const pendingStatus = service.status()
  assert.equal(inspections, 1)
  // Model a start completing while the original Windows process query is in flight.
  // No service is spawned or stopped in this regression.
  service.invalidateIdentity()
  releaseOld(previous)
  const status = await pendingStatus
  assert.equal(inspections, 2)
  assert.equal(status.find(item => item.id === 'frontend').pid, 444)
  assert.deepEqual(status.map(item => item.state), ['healthy', 'healthy'])
  assert.deepEqual(service.identityCache.value, current)
})

test('concurrent read-only identity consumers share one current snapshot and retry failures', async t => {
  const { service } = await fixture(t)
  let inspections = 0, release
  const blocked = new Promise(done => { release = done })
  service.run = async () => { inspections++; await blocked; return { code: 0, stdout: JSON.stringify(identities) } }
  const waiting = Array.from({ length: 8 }, () => service.identities())
  assert.equal(inspections, 1)
  release()
  assert.deepEqual(await Promise.all(waiting), Array.from({ length: 8 }, () => identities))
  assert.equal(service.identityFlight, null)
  service.invalidateIdentity()
  service.run = async () => { inspections++; throw new Error('inspection unavailable') }
  await assert.rejects(service.identities(), /unavailable/)
  assert.equal(service.identityFlight, null)
  service.run = async () => { inspections++; return { code: 0, stdout: JSON.stringify(identities) } }
  assert.deepEqual(await service.identities(), identities)
  assert.equal(inspections, 3)
})

test('a required fresh identity check bypasses a pending read and cannot be overwritten by it', async t => {
  const { service } = await fixture(t)
  const previous = { backend: { ...identities.backend, pid: 111 }, frontend: identities.frontend }
  let inspections = 0, releaseOld
  const blocked = new Promise(done => { releaseOld = done })
  service.run = async () => ({ code: 0, stdout: JSON.stringify(++inspections === 1 ? await blocked : identities) })
  const old = service.identities()
  assert.deepEqual(await service.identities(true), identities)
  assert.equal(inspections, 2, 'a fresh lifecycle check always issues a new OS inspection')
  releaseOld(previous)
  assert.deepEqual(await old, previous)
  assert.deepEqual(service.identityCache.value, identities)
  await service.identities(true)
  assert.equal(inspections, 3, 'fresh checks bypass even a recently verified cache')
})

test('start-all starts both listeners concurrently and settles partial success before returning failure', async t => {
  const { service } = await fixture(t)
  const started = [], completed = []
  let release
  const frontendReady = new Promise(done => { release = done })
  service.start = async id => {
    started.push(id)
    if (id === 'backend') throw new Error('backend unavailable')
    await frontendReady; completed.push(id)
  }
  let finished = false
  const action = service.action('start', 'all').finally(() => { finished = true })
  const rejected = assert.rejects(action, /backend unavailable/)
  await new Promise(done => setImmediate(done))
  assert.deepEqual(started, ['backend', 'frontend'])
  assert.equal(finished, false, 'partial success must remain tracked until the sibling has settled')
  release(); await rejected
  assert.deepEqual(completed, ['frontend'])
})
