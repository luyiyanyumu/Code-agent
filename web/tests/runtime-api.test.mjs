import test from 'node:test'
import assert from 'node:assert/strict'
import { api, openTerminalStream, request } from '../src/api.js'

test('runtime APIs encode project/run IDs, use explicit payloads, and preserve request contract', async t => {
  const oldFetch = globalThis.fetch, oldStorage = globalThis.localStorage, calls = []
  t.after(() => { globalThis.fetch = oldFetch; globalThis.localStorage = oldStorage })
  globalThis.localStorage = { getItem: () => null }
  globalThis.fetch = async (path, options) => { calls.push({ path, ...options }); return { ok: true, status: 200, text: async () => '{"id":"test"}' } }
  await api.code.terminals('a/b'); await api.code.createTerminal('a/b', { shell: 'powershell', cwd: 'src', cols: 100, rows: 30 }); await api.code.closeTerminal('a/b', 't/id')
  await api.code.runConfigs('a/b'); await api.code.runs('a/b'); await api.code.createRun('a/b', { kind: 'python', path: 'main.py' }); await api.code.run('a/b', 'r/id', 8); await api.code.stopRun('a/b', 'r/id')
  assert.deepEqual(calls.map(item => [item.method, item.path]), [
    ['GET', '/api/v1/code/projects/a%2Fb/terminals'], ['POST', '/api/v1/code/projects/a%2Fb/terminals'], ['DELETE', '/api/v1/code/projects/a%2Fb/terminals/t%2Fid'],
    ['GET', '/api/v1/code/projects/a%2Fb/run-configs'], ['GET', '/api/v1/code/projects/a%2Fb/runs'], ['POST', '/api/v1/code/projects/a%2Fb/runs'], ['GET', '/api/v1/code/projects/a%2Fb/runs/r%2Fid?after_seq=8'], ['POST', '/api/v1/code/projects/a%2Fb/runs/r%2Fid/stop'],
  ])
  assert.deepEqual(JSON.parse(calls[1].body), { shell: 'powershell', cwd: 'src', cols: 100, rows: 30 }); assert.equal(calls[0].headers.Authorization, undefined)
  assert.equal(typeof request, 'function')
})

test('terminal websocket URLs use authenticated same origin, escaped identifiers and last seen seq', t => {
  const oldLocation = globalThis.location, oldSocket = globalThis.WebSocket, oldStorage = globalThis.localStorage
  t.after(() => { globalThis.location = oldLocation; globalThis.WebSocket = oldSocket; globalThis.localStorage = oldStorage })
  globalThis.location = { protocol: 'https:', host: 'localhost:5175' }
  globalThis.localStorage = { getItem: () => 'fixture-token' }
  globalThis.WebSocket = class { constructor(url) { this.url = url } }
  const url = new URL(openTerminalStream('p/a', 't b', 3.9).url)
  assert.equal(url.protocol, 'wss:'); assert.equal(url.pathname, '/api/v1/code/projects/p%2Fa/terminals/t%20b/stream'); assert.equal(url.searchParams.get('after_seq'), '3'); assert.equal(url.searchParams.get('token'), 'fixture-token')
  globalThis.localStorage = { getItem: () => null }; assert.equal(new URL(openTerminalStream('p', 't', -1).url).searchParams.has('token'), false)
})

test('runtime discovery forwards cancellation and preserves AbortError', async t => {
  const oldFetch = globalThis.fetch, oldStorage = globalThis.localStorage
  t.after(() => { globalThis.fetch = oldFetch; globalThis.localStorage = oldStorage })
  globalThis.localStorage = { getItem: () => null }
  const controller = new AbortController(), calls = []
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options })
    if (options.signal?.aborted) throw options.signal.reason
    return { ok: true, status: 200, text: async () => '{}' }
  }
  for (const read of [api.code.terminals, api.code.runConfigs, api.code.runs]) await read('p/a', { signal: controller.signal })
  assert.equal(calls.length, 3)
  assert.ok(calls.every(({ options }) => options.signal === controller.signal && options.method === 'GET' && options.body === undefined))
  controller.abort()
  for (const read of [api.code.terminals, api.code.runConfigs, api.code.runs]) await assert.rejects(read('p/a', { signal: controller.signal }), error => error === controller.signal.reason && error.name === 'AbortError')
})
