import assert from 'node:assert/strict'
import test from 'node:test'
import { effectScope } from 'vue'
import { api } from '../src/api.js'
import { useAgentSession } from '../src/composables/useAgentSession.js'

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = () => new Promise(setImmediate)
function setup(t) {
  const oldApi = { ...api }, globals = Object.fromEntries(['window', 'localStorage', 'WebSocket', 'location'].map(key => [key, globalThis[key]]))
  const storage = new Map(), sockets = []
  const localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  class Socket {
    static OPEN = 1
    constructor() { this.readyState = 0; sockets.push(this) }
    close() { this.readyState = 3 }
    emit(data) { this.onmessage?.({ data: JSON.stringify(data) }) }
  }
  Object.assign(globalThis, { window: { localStorage, removeEventListener() {} }, localStorage, WebSocket: Socket, location: { protocol: 'http:', host: 'fixture.invalid' } })
  Object.assign(api, { events: async () => [], pendingApproval: async () => null, pendingQuestions: async () => ({ questions: [] }), checkpoints: async () => ({ busy: false }),
    webCapability: async () => ({ available: true }), setSessionWeb: async (_, enabled) => ({ web_enabled: enabled }) })
  const scope = effectScope(), s = scope.run(useAgentSession)
  t.after(() => { scope.stop(); Object.assign(api, oldApi); Object.assign(globalThis, globals) })
  return { s, storage, sockets }
}

test('unavailable capability is refreshed on click and only confirmed on/off states reach local storage', async t => {
  const { s, storage } = setup(t), posts = []
  let available = false
  api.webCapability = async () => ({ available, reason: available ? '' : '请配置模型后重试。' })
  api.setSessionWeb = async (id, enabled) => { posts.push([id, enabled]); return { web_enabled: enabled } }
  await s.load('one'); await settle()
  assert.equal(await s.setWeb(true), false)
  assert.equal(s.web.value, false); assert.match(s.webError.value, /配置/); assert.equal(posts.length, 0)
  available = true
  assert.equal(await s.setWeb(true), true)
  assert.equal(s.web.value, true); assert.equal(storage.get('code-agent:web:one'), '1')
  assert.equal(await s.setWeb(false), true)
  assert.equal(s.web.value, false); assert.equal(storage.get('code-agent:web:one'), '0')
  assert.deepEqual(posts, [['one', true], ['one', false]])
})

test('a transient capability error is retryable and an unavailable service never prevents turning off', async t => {
  const { s } = setup(t)
  api.webCapability = async () => { throw new Error('temporary') }
  await s.load('one'); await settle()
  assert.equal(s.webInfo.value.available, false)
  api.webCapability = async () => ({ available: true })
  assert.equal(await s.setWeb(true), true)
  api.webCapability = async () => { throw new Error('must not query on disable') }
  assert.equal(await s.setWeb(false), true)
  assert.equal(s.web.value, false)
})

test('pending writes reject duplicate clicks and failed writes preserve the previous confirmed state', async t => {
  const { s, storage } = setup(t)
  await s.load('one'); await settle()
  const gate = deferred(); let calls = 0
  api.setSessionWeb = () => { calls++; return gate.promise }
  const pending = s.setWeb(true); await settle()
  assert.equal(s.webPending.value, true); assert.equal(s.web.value, false); assert.equal(storage.size, 0)
  assert.equal(await s.setWeb(true), false); assert.equal(calls, 1)
  gate.reject(new Error('无法保存设置'))
  assert.equal(await pending, false)
  assert.equal(s.webPending.value, false); assert.equal(s.web.value, false); assert.equal(storage.size, 0)
  api.setSessionWeb = async () => ({ web_enabled: 1 })
  assert.equal(await s.setWeb(true), true)
  api.setSessionWeb = async () => { throw new Error('无法关闭') }
  assert.equal(await s.setWeb(false), false)
  assert.equal(s.web.value, true); assert.equal(storage.get('code-agent:web:one'), '1')
})

test('old capability and toggle responses cannot change the replacement session', async t => {
  const { s, storage } = setup(t), staleCapability = deferred(), staleWrite = deferred()
  let calls = 0
  api.webCapability = () => ++calls === 1 ? staleCapability.promise : Promise.resolve({ available: true })
  await s.load('old'); await s.load('one'); await settle()
  staleCapability.resolve({ available: false, reason: '旧错误' }); await settle()
  assert.equal(s.webInfo.value.available, true)
  api.setSessionWeb = () => staleWrite.promise
  const pending = s.setWeb(true); await settle()
  await s.load('new'); await settle()
  staleWrite.resolve({ web_enabled: true })
  assert.equal(await pending, false)
  assert.equal(s.web.value, false); assert.equal(s.webError.value, ''); assert.equal(storage.has('code-agent:web:new'), false)
})

test('backend session state overrides an old browser preference and malformed confirmations never turn the switch on', async t => {
  const { s, storage, sockets } = setup(t)
  storage.set('code-agent:web:one', '1')
  await s.load('one'); await settle()
  sockets.at(-1).emit({ seq: 0, type: 'session_meta', payload: { web_enabled: false } })
  assert.equal(s.web.value, false)
  api.setSessionWeb = async () => ({ web_enabled: 'false' })
  assert.equal(await s.setWeb(true), false)
  assert.equal(s.web.value, false); assert.match(s.webError.value, /返回异常/)
})
