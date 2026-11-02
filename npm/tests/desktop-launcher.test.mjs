import assert from 'node:assert/strict'
import test from 'node:test'
import http from 'node:http'
import { EventEmitter } from 'node:events'
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createDesktopService, parseDesktopArgs, launchDesktopWindow } from '../desktop-launcher.mjs'

const nonce = '246d0024-9ec8-4c2b-90fd-121778992691'

test('native exe launch keeps spaced paths literal, preserves environment and detaches the visible window', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'code-agent native exe '))
  const root = join(directory, 'project with spaces & parentheses (原生)')
  await mkdir(root, { recursive: true })
  const executable = join(root, 'Code-agent Launcher.exe')
  await writeFile(executable, '')
  t.after(() => rm(directory, { recursive: true, force: true }))
  const env = { PATH: 'literal-test-path', CODE_AGENT_NODE: 'old-node', CODE_AGENT_HOME: 'existing-data-home', CUSTOM_FLAG: 'retained' }
  const original = { ...env }
  for (const home of [undefined, join(directory, 'profile data 中文')]) {
    const calls = []
    let unrefed = false
    const result = await launchDesktopWindow({ root, home, env, spawnProcess: (...args) => {
      calls.push(args)
      const child = Object.assign(new EventEmitter(), { pid: 4321, unref() { unrefed = true } })
      queueMicrotask(() => child.emit('spawn'))
      return child
    } })
    assert.equal(calls.length, 1)
    assert.equal(calls[0][0], executable)
    assert.deepEqual(calls[0][1], ['--root', root])
    assert.deepEqual(calls[0][2], { cwd: root, env: { ...original, CODE_AGENT_NODE: process.execPath,
      ...(home ? { CODE_AGENT_HOME: resolve(home) } : {}) },
    detached: true, windowsHide: false, shell: false, stdio: 'ignore' })
    assert.deepEqual(result, { native: true, pid: 4321, executable })
    assert.equal(unrefed, true)
    assert.deepEqual(env, original)
  }
})

test('missing native exe fails before spawning and identifies the build command', async t => {
  const root = await mkdtemp(join(tmpdir(), 'code-agent missing native exe '))
  t.after(() => rm(root, { recursive: true, force: true }))
  let spawned = false
  await assert.rejects(launchDesktopWindow({ root, spawnProcess: () => { spawned = true; throw new Error('must not spawn') } }), /npm run build:launcher/)
  assert.equal(spawned, false)
})

async function fixture(t, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'code-agent native launcher '))
  const root = join(directory, 'workspace with spaces')
  await mkdir(join(root, 'npm', 'launcher-ui'), { recursive: true })
  await mkdir(join(root, 'logs'), { recursive: true })
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: '0.10.2' }))
  await writeFile(join(root, 'npm', 'launcher-ui', 'index.html'), '<html><meta name="launcher-token" content="__LAUNCHER_TOKEN__"></html>')
  await writeFile(join(root, 'npm', 'launcher-ui', 'app.js'), 'console.log("desktop")')
  await writeFile(join(root, 'npm', 'launcher-ui', 'style.css'), 'body{}')
  const calls = [], folders = []
  const services = [
    { id: 'backend', state: 'healthy', url: 'http://127.0.0.1:8765' },
    { id: 'frontend', state: 'healthy', url: 'http://127.0.0.1:5175/' },
  ]
  const manager = { logDir: join(root, 'logs'), services, env: {},
    initialize: async () => {}, status: async () => structuredClone(services),
    action: async (...args) => { calls.push(args) }, ...options.manager }
  const handshakeFile = join(directory, 'private handshake.json')
  const service = await createDesktopService({ root, handshakeFile, parentPid: process.pid, nonce, manager,
    parentAlive: options.parentAlive || (() => true), pollIntervalMs: 20, autoStart: false,
    openFolder: async path => folders.push(path), ...options, manager })
  t.after(async () => {
    options.beforeClose?.()
    await service.close()
    await rm(directory, { recursive: true, force: true })
  })
  const html = await (await fetch(service.url)).text()
  const token = /content="([a-f0-9]{64})"/.exec(html)[1]
  const post = (action, target) => fetch(`${service.url}/api/action`, { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Launcher-Token': token }, body: JSON.stringify({ action, target }) })
  const events = key => fetch(`${service.url}/api/desktop/events`, { headers: { 'X-Desktop-Token': key ?? nonce } })
  return { root, directory, handshakeFile, manager, service, calls, folders, html, post, events }
}

async function until(condition, message = 'condition did not complete') {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await condition()) return
    await new Promise(done => setTimeout(done, 20))
  }
  assert.fail(message)
}

async function waitOperation(service) {
  let status
  await until(async () => {
    status = await (await fetch(`${service.url}/api/status`)).json()
    return status.operation?.state !== 'running'
  })
  return status.operation
}

async function isClosed(url) {
  try { await fetch(url, { signal: AbortSignal.timeout(250) }); return false } catch { return true }
}

test('desktop arguments preserve paths and validate owner PID, nonce and unknown input', () => {
  const root = resolve('workspace with spaces')
  const options = parseDesktopArgs(['--root', root, '--handshake-file', join(root, 'private.json'), '--parent-pid', '1234', '--nonce', nonce])
  assert.equal(options.root, root)
  assert.equal(options.parentPid, 1234)
  assert.equal(options.nonce, nonce)
  assert.equal(options.handshakeFile, join(root, 'private.json'))
  for (const pid of ['0', '-1', '1.5', '12x']) {
    assert.throws(() => parseDesktopArgs(['--root', root, '--handshake-file', join(root, 'private.json'), '--parent-pid', pid, '--nonce', nonce]))
  }
  assert.throws(() => parseDesktopArgs(['--root', root, '--handshake-file', join(root, 'private.json'), '--parent-pid', '123', '--nonce', 'invalid']))
  assert.throws(() => parseDesktopArgs(['--root']))
  assert.throws(() => parseDesktopArgs(['--unexpected', 'value']))
})

test('private desktop service writes a verified ephemeral handshake without exposing its nonce to the UI', async t => {
  const { root, handshakeFile, service, html } = await fixture(t)
  const handshake = JSON.parse(await readFile(handshakeFile, 'utf8'))
  assert.equal(handshake.pid, process.pid)
  assert.equal(handshake.root, root)
  assert.equal(handshake.url, service.url)
  assert.equal(handshake.nonce, nonce)
  assert.match(service.url, /^http:\/\/127\.0\.0\.1:\d+$/)
  assert.notEqual(new URL(service.url).port, '19387')
  assert.deepEqual(handshake.serviceUrls, ['http://127.0.0.1:8765', 'http://127.0.0.1:5175/'])
  assert.equal(handshake.workspaceUrl, 'http://127.0.0.1:5175/')
  assert.equal(html.includes('__LAUNCHER_TOKEN__'), false)
  assert.equal(html.includes(nonce), false)
  const status = await (await fetch(`${service.url}/api/status`)).text()
  assert.equal(status.includes(nonce), false)
})

test('desktop bridge rejects missing credentials, foreign origins and hostile Host headers', async t => {
  const { service, events } = await fixture(t)
  assert.equal((await events('')).status, 403)
  assert.equal((await events('wrong')).status, 403)
  assert.equal((await fetch(`${service.url}/api/desktop/close`, { method: 'POST' })).status, 403)
  assert.equal((await fetch(`${service.url}/api/desktop/events`, { headers: { 'X-Desktop-Token': nonce, Origin: 'https://untrusted.invalid' } })).status, 403)
  const status = await new Promise((done, reject) => {
    const request = http.get(`${service.url}/api/desktop/events`, { headers: { Host: 'untrusted.invalid', 'X-Desktop-Token': nonce } }, response => { response.resume(); done(response.statusCode) })
    request.on('error', reject)
  })
  assert.equal(status, 403)
  assert.equal((await fetch(service.url)).status, 200)
})

test('opening services queues native child windows and drains events without a browser launch', async t => {
  const { service, post, events, calls } = await fixture(t)
  for (const target of ['workbench', 'backend', 'frontend']) {
    assert.equal((await post('open', target)).status, 202)
    assert.equal((await waitOperation(service)).state, 'succeeded')
  }
  const payload = await (await events()).json()
  assert.deepEqual(payload.events.map(item => ({ type: item.type, url: item.url })), [
    { type: 'open-window', url: 'http://127.0.0.1:5175/' },
    { type: 'open-window', url: 'http://127.0.0.1:8765/docs' },
    { type: 'open-window', url: 'http://127.0.0.1:5175/' },
  ])
  assert.deepEqual((await (await events()).json()).events, [])
  assert.deepEqual(calls, [])
})

test('log folder uses only the explicit folder adapter and does not queue web navigation', async t => {
  const { service, root, post, events, folders } = await fixture(t)
  assert.equal((await post('open', 'logs')).status, 202)
  assert.equal((await waitOperation(service)).state, 'succeeded')
  assert.deepEqual(folders, [join(root, 'logs')])
  assert.deepEqual((await (await events()).json()).events, [])
})

test('native stdout adapter receives the exact permitted window URL', async t => {
  const opened = []
  const { manager, service, post, events } = await fixture(t, { onOpenWindow: url => opened.push(url) })
  const destination = 'http://127.0.0.1:5175/?from=launcher&note=%E4%B8%AD%E6%96%87#editor'
  manager.services[1].url = destination
  assert.equal((await post('open', 'workbench')).status, 202)
  assert.equal((await waitOperation(service)).state, 'succeeded')
  assert.deepEqual(opened, [destination])
  assert.deepEqual((await (await events()).json()).events, [{ type: 'open-window', url: destination }])
})

test('desktop opening rejects non-service targets and malicious service destinations', async t => {
  const opened = []
  const { manager, service, post, events } = await fixture(t, { onOpenWindow: url => opened.push(url) })
  for (const target of ['https://untrusted.invalid/', 'file:///C:/Windows/System32/cmd.exe', '../../logs']) {
    assert.equal((await post('open', target)).status, 400)
  }
  for (const destination of [
    'https://127.0.0.1:5175/',
    'http://user:password@127.0.0.1:5175/',
    'http://127.0.0.1:5175@untrusted.invalid/',
    'http://untrusted.invalid:5175/',
  ]) {
    manager.services[1].url = destination
    assert.equal((await post('open', 'workbench')).status, 202)
    assert.equal((await waitOperation(service)).state, 'failed', `unsafe native window was accepted: ${destination}`)
  }
  assert.deepEqual(opened, [])
  assert.deepEqual((await (await events()).json()).events, [])
})

test('closing the private desktop controller preserves managed frontend and backend services', async t => {
  const { service, calls } = await fixture(t)
  const response = await fetch(`${service.url}/api/desktop/close`, { method: 'POST', headers: { 'X-Desktop-Token': nonce } })
  assert.equal(response.status, 200)
  assert.equal((await response.json()).closing, true)
  await until(() => isClosed(service.url), 'desktop controller did not close')
  assert.deepEqual(calls, [])
})

test('parent exit waits for the current operation to finish and never stops services', async t => {
  let alive = true, release
  const blocked = new Promise(done => { release = done })
  const actions = []
  const { service, post } = await fixture(t, { parentAlive: () => alive, beforeClose: () => release(),
    manager: { action: async (...args) => { actions.push(args); await blocked } } })
  assert.equal((await post('start', 'backend')).status, 202)
  alive = false
  await new Promise(done => setTimeout(done, 80))
  assert.equal((await fetch(`${service.url}/api/status`)).status, 200)
  release()
  await until(() => isClosed(service.url), 'controller survived its parent after operation finished')
  assert.deepEqual(actions, [['start', 'backend']])
})

async function rpc(service, method, params = {}, key = nonce) {
  return fetch(`${service.url}/api/desktop/rpc`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Desktop-Token': key }, body: JSON.stringify({ method, params }) })
}

test('IDE RPC authenticates requests, restricts actions and redacts bounded logs', async t => {
  const { service, root } = await fixture(t)
  assert.equal((await rpc(service, 'status', {}, '')).status, 403)
  const status = await (await rpc(service, 'status')).json()
  assert.equal(status.version, '0.10.2')
  assert.equal(JSON.stringify(status).includes(nonce), false)
  for (const params of [{ action: 'stop', target: 'all' }, { action: 'open', target: 'https://untrusted.invalid' }])
    assert.equal((await rpc(service, 'action', params)).status, 400)
  assert.equal((await rpc(service, 'logs', { service: '../secret' })).status, 400)
  await writeFile(join(root, 'logs/backend.log'), 'api_key=secret-value\nready\n')
  const log = await (await rpc(service, 'logs', { service: 'backend', lines: 1 })).json()
  assert.equal(log.text, 'ready')
  assert.equal((await (await rpc(service, 'logs', { service: 'backend', lines: 5 })).json()).text.includes('secret-value'), false)
})

test('IDE exit preserves reused services and shuts down only identities started by this app', async t => {
  const { service, manager, calls } = await fixture(t)
  manager.services[0].pid = 10
  manager.services[1].pid = 20
  manager.identities = async () => Object.fromEntries(manager.services.map(item => [item.id, { owned: true, pid: item.pid, created: String(item.pid) }]))
  manager.action = async (action, target) => {
    calls.push([action, target])
    for (const item of manager.services.filter(item => target === 'all' || item.id === target)) {
      if (action === 'restart' || action === 'start') { item.pid += 100; item.state = 'healthy' }
      else { item.pid = null; item.state = 'stopped' }
    }
  }
  const first = await (await rpc(service, 'exit')).json()
  assert.deepEqual(first.preserved, ['backend', 'frontend'])
  assert.deepEqual(calls, [])
  assert.equal((await rpc(service, 'action', { action: 'restart', target: 'backend' })).status, 200)
  const state = await (await rpc(service, 'status')).json()
  assert.deepEqual(state.desktop.managedServices, ['backend'])
  const result = await (await rpc(service, 'exit')).json()
  assert.deepEqual(result.stopped, ['backend'])
  assert.deepEqual(result.preserved, ['frontend'])
  assert.deepEqual(calls, [['restart', 'backend'], ['stop', 'backend']])
  assert.equal((await fetch(`${service.url}/api/status`)).status, 200, 'native may close the controller only after the exit response')
})

test('IDE exit refuses a busy backend and never stops its frontend afterwards', async t => {
  const { service, manager, calls } = await fixture(t)
  let pid = 100
  manager.identities = async () => ({ backend: { owned: true, pid, created: String(pid) } })
  manager.action = async (action, target) => { calls.push([action, target]); if (action === 'restart') pid++; else throw new Error('仍有任务正在执行') }
  assert.equal((await rpc(service, 'action', { action: 'restart', target: 'backend' })).status, 200)
  const refused = await rpc(service, 'exit')
  assert.equal(refused.status, 400)
  assert.match((await refused.json()).error, /任务/)
  assert.deepEqual(calls, [['restart', 'backend'], ['stop', 'backend']])
})

test('IDE startup remains unsettled until service ownership has been captured', async t => {
  const { service, manager } = await fixture(t)
  let phase = 0, release
  const blocked = new Promise(done => { release = done })
  manager.identities = async () => { if (++phase === 2) await blocked; return { backend: { owned: true, pid: phase === 1 ? null : 123, created: 'new' } } }
  const action = rpc(service, 'action', { action: 'start', target: 'backend' })
  await until(() => phase >= 2)
  assert.equal((await (await rpc(service, 'status')).json()).desktop.ready, false)
  release(); assert.equal((await action).status, 200)
  const state = await (await rpc(service, 'status')).json()
  assert.equal(state.desktop.ready, true)
  assert.deepEqual(state.desktop.managedServices, ['backend'])
})

test('successful desktop actions capture ownership before returning the settled status', async t => {
  const { service, manager } = await fixture(t)
  let inspections = 0, current = 10, cache = null, statusCalls = 0
  manager.identities = async () => {
    inspections++
    cache = { backend: { owned: true, pid: current, created: String(current) } }
    return cache
  }
  manager.status = async () => {
    statusCalls++
    if (!cache) await manager.identities()
    return manager.services.map(item => ({ ...item, ...(item.id === 'backend' ? cache.backend : {}) }))
  }
  manager.action = async () => { current++; cache = null }
  const response = await rpc(service, 'action', { action: 'restart', target: 'backend' })
  assert.equal(response.status, 200)
  const state = await response.json()
  assert.equal(state.desktop.ready, true)
  assert.deepEqual(state.desktop.managedServices, ['backend'])
  assert.equal(state.services[0].pid, 11)
  assert.equal(inspections, 2, 'only pre-action and post-action ownership snapshots are needed')
  assert.equal(statusCalls, 1)
})

test('a failed pre-action identity check cannot adopt pre-existing services as owned', async t => {
  const { service, manager, calls } = await fixture(t)
  let inspections = 0
  manager.identities = async () => {
    if (++inspections === 1) throw new Error('identity unavailable')
    return { backend: { owned: true, pid: 10, created: 'existing' } }
  }
  assert.equal((await rpc(service, 'action', { action: 'start', target: 'backend' })).status, 400)
  assert.equal(inspections, 1, 'an action that never started must not capture ownership afterwards')
  const exited = await (await rpc(service, 'exit')).json()
  assert.deepEqual(exited.stopped, [])
  assert.deepEqual(exited.preserved, ['backend'])
  assert.deepEqual(calls, [])
})
