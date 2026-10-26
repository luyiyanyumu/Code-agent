import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { dirname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { computed, ref, watch, nextTick, effectScope } from 'vue'
import { marked } from 'marked'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { api, getToken, setToken, openStream, readFileBase64 } from '../src/api.js'
import { generalChatSessions } from '../src/lib/chatSessions.js'
import { createSessionStream } from '../src/lib/sessionStream.js'
import { sessionPhaseLabel, internalMessagePresentation, isSuccessfulCompletion, incompleteCompletion } from '../src/lib/sessionPresentation.js'
import { attachThinking, pushThinking } from '../src/lib/thinkingSegments.js'
import * as todos from '../src/lib/todos.js'
import * as questions from '../src/lib/questions.js'
import { shouldSendMessage } from '../src/lib/preferences.js'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const source = file => readFileSync(resolve(root, file), 'utf8')
function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry =>
    entry.isDirectory() ? walk(resolve(directory, entry.name)) : [resolve(directory, entry.name)])
}
function script(file) {
  return source(file).match(/<script setup>([\s\S]*?)<\/script>/)[1]
    .replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"]\s*$/gm, '')
}

test('standalone source dependency closure stays inside this directory and excludes coding runtime', () => {
  const files = walk(resolve(root, 'src')).filter(file => /\.(vue|js)$/.test(file))
  for (const file of files) {
    const text = readFileSync(file, 'utf8')
    for (const [, specifier] of text.matchAll(/(?:from\s*|import\s*\(?\s*)['"]([^'"]+)['"]/g)) {
      if (!specifier.startsWith('.')) {
        assert.ok(['vue', 'marked'].includes(specifier), `${file}: unexpected dependency ${specifier}`)
        continue
      }
      const target = resolve(dirname(file), specifier)
      assert.ok(target.startsWith(root + sep), `source escaped standalone folder: ${target}`)
      assert.ok(existsSync(target), `missing imported file: ${target}`)
    }
    assert.doesNotMatch(text, /(?:from|import\s*\()\s*['"][^'"]*(?:monaco|xterm|CodeView|AiPanel)/)
  }
  const pkg = JSON.parse(source('package.json'))
  const lock = JSON.parse(source('package-lock.json'))
  assert.deepEqual(Object.keys(pkg.dependencies).sort(), ['marked', 'vue'])
  assert.equal(lock.packages[''].name, pkg.name)
  assert.ok(!Object.keys(lock.packages).some(key => /monaco|xterm/.test(key)))
  assert.ok(!Object.hasOwn(api, 'code'))
  for (const [, name] of source('src/views/ChatView.vue').matchAll(/\bapi\.([A-Za-z]+)\(/g)) {
    assert.equal(typeof api[name], 'function', `ChatView API ${name} must exist`)
  }
})

test('every standalone Vue component compiles', () => {
  for (const filename of walk(resolve(root, 'src')).filter(file => file.endsWith('.vue'))) {
    const { descriptor, errors } = parse(readFileSync(filename, 'utf8'), { filename })
    assert.deepEqual(errors, [], filename)
    const compiled = compileScript(descriptor, { id: filename })
    const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: compiled.bindings } })
    assert.deepEqual(template.errors, [], filename)
  }
})

test('general sessions retain old and archived history while excluding project teams', () => {
  const history = [{ id: 'old' }, { id: 'new', project_id: '' }, { id: 'null', project_id: null }, { id: 'archived', status: 'archived' }, { id: 'lead', project_id: 'project', role: 'lead' }, { id: 'child', project_id: 'project', role: 'worker' }]
  assert.deepEqual(generalChatSessions(history), history.slice(0, 4))
  assert.equal(history.length, 6, 'filtering never changes server history')
  assert.deepEqual(generalChatSessions(null), [])
})

test('standalone sessions have no execution mode API or composer controls and ignore stale creation options', async t => {
  const chat = source('src/views/ChatView.vue')
  assert.doesNotMatch(chat, /loopModes|loopMenu|loop-mode|loop_mode|newLoop|curLoop|循环模式|CAGE/i)
  for (const removed of ['setSessionLoopMode', 'loopModeSettings', 'saveLoopModeSettings']) assert.equal(removed in api, false)
  const saved = { localStorage: globalThis.localStorage, fetch: globalThis.fetch }
  const calls = []
  globalThis.localStorage = { getItem: () => null }
  globalThis.fetch = async (path, options) => {
    calls.push({ path, method: options.method, body: JSON.parse(options.body) })
    return new Response('{}')
  }
  t.after(() => Object.assign(globalThis, saved))
  await api.createSession({ folder: 'work', permissionMode: 'workspace-write', loopMode: 'cage' })
  assert.deepEqual(calls, [{ path: '/api/v1/sessions', method: 'POST', body: { folder: 'work', permission_mode: 'workspace-write' } }])
})

test('chat startup only reads metadata without removed mode settings; both refresh paths exclude project sessions', async t => {
  const oldWindow = globalThis.window
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  const history = [{ id: 'ordinary' }, { id: 'project', project_id: 'p' }]
  const calls = []
  const metadata = { listSessions: history, listWorkspaces: [], modelSettings: {}, permissionSettings: {} }
  const mockApi = new Proxy({}, { get(_, key) { return async () => {
    calls.push(key)
    assert.ok(Object.hasOwn(metadata, key), `unexpected startup side effect: ${String(key)}`)
    return metadata[key]
  } } })
  const mounts = []
  let unmount
  const scope = effectScope()
  const bindings = {
    computed, ref, watch, nextTick, marked, api: mockApi, getToken, readFileBase64,
    openStream: () => { throw new Error('startup must not open a session stream') },
    usePreferences: () => ({ preferences: { sendKey: 'enter' } }), shouldSendMessage,
    createSessionStream, sessionPhaseLabel, internalMessagePresentation, isSuccessfulCompletion, incompleteCompletion, attachThinking, pushThinking, generalChatSessions, ...todos, ...questions,
    useDialog: () => ({ open() { throw new Error('startup must not request a dialog') } }),
    defineProps: () => ({ user: { role: 'admin' } }),
    onMounted: fn => mounts.push(fn), onBeforeUnmount: fn => { unmount = fn },
  }
  t.after(() => { unmount?.(); scope.stop(); globalThis.window = oldWindow })
  const state = scope.run(() => new Function(...Object.keys(bindings), script('src/views/ChatView.vue') + '\nreturn { sessions, current, refreshSessionsAfterChange }')(...Object.values(bindings)))
  for (const mount of mounts) await mount()
  assert.deepEqual(calls, ['listSessions', 'listWorkspaces', 'modelSettings', 'permissionSettings'])
  assert.deepEqual(state.sessions.value.map(item => item.id), ['ordinary'])
  assert.equal(state.current.value, null)
  await state.refreshSessionsAfterChange()
  assert.deepEqual(state.sessions.value.map(item => item.id), ['ordinary'])
})

test('app startup exposes connection error and successful retry without creating a session', async () => {
  let fail = true, reads = 0, mount
  const bindings = {
    ref, api: { me: async () => { reads++; if (fail) throw new Error('offline'); return { id: 'user' } } }, setToken,
    usePreferences: () => ({ preferences: {}, updatePreference() {}, storageError: ref('') }),
    initializePreferences: () => () => {}, onMounted: fn => { mount = fn }, onBeforeUnmount() {},
  }
  const state = new Function(...Object.keys(bindings), script('src/App.vue') + '\nreturn { restore, user, bootError, booting }')(...Object.values(bindings))
  await mount()
  assert.equal(state.bootError.value, 'offline')
  assert.equal(state.booting.value, false)
  fail = false
  await state.restore()
  assert.equal(state.user.value.id, 'user')
  assert.equal(state.bootError.value, '')
  assert.equal(reads, 2)
})

test('standalone authentication never changes or revives coding tokens and request uses chat identity', async t => {
  const saved = { localStorage: globalThis.localStorage, fetch: globalThis.fetch }
  const values = new Map([['code-agent-token', 'coding-example'], ['ch-agent-token', 'legacy-example']])
  const requests = []
  globalThis.localStorage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
  globalThis.fetch = async (path, options) => { requests.push({ path, options }); return { ok: true, status: 200, text: async () => '{"id":"chat-user"}' } }
  t.after(() => Object.assign(globalThis, saved))
  assert.equal(getToken(), '')
  setToken('chat-example')
  assert.equal((await api.me()).id, 'chat-user')
  assert.equal(requests[0].options.headers.Authorization, 'Bearer chat-example')
  assert.equal(requests[0].path, '/api/v1/auth/me')
  setToken('')
  assert.equal(getToken(), '', 'clearing chat identity cannot resurrect a coding token')
  assert.equal(values.get('code-agent-token'), 'coding-example')
  assert.equal(values.get('ch-agent-token'), 'legacy-example')
})

test('standalone websocket reconnect replays from last sequence and cannot reopen a previous session', t => {
  const saved = { localStorage: globalThis.localStorage, WebSocket: globalThis.WebSocket, location: globalThis.location }
  const sockets = [], timers = new Map(), events = [], states = []
  let sequence = 4, timerId = 0
  globalThis.localStorage = { getItem: () => 'example token' }
  globalThis.location = { protocol: 'https:', host: 'chat.example' }
  globalThis.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 1; sockets.push(this) }
    send() {}
    close() { this.readyState = 3 }
  }
  const stream = createSessionStream({
    open: openStream, afterSeq: () => sequence, onEvent: event => { events.push(event); sequence = event.seq },
    onState: state => states.push(state), onError: () => {},
    schedule: fn => { timers.set(++timerId, fn); return timerId }, unschedule: id => timers.delete(id),
  })
  t.after(() => { stream.close(); Object.assign(globalThis, saved) })
  stream.connect('first')
  assert.match(sockets[0].url, /^wss:\/\/chat\.example\/api\/v1\/sessions\/first\/stream\?token=example%20token&after_seq=4$/)
  sockets[0].onopen()
  sockets[0].onmessage({ data: JSON.stringify({ type: 'text_delta', seq: 5 }) })
  sockets[0].onclose({ code: 1006 })
  assert.equal(timers.size, 1)
  const retry = [...timers.values()][0]
  retry()
  assert.match(sockets[1].url, /after_seq=5$/)
  const oldFrame = sockets[1].onmessage
  stream.connect('second')
  oldFrame({ data: JSON.stringify({ type: 'done', seq: 99 }) })
  retry()
  assert.equal(sockets.length, 3, 'old retry cannot create another socket after switching')
  assert.equal(events.length, 1)
  assert.match(sockets[2].url, /sessions\/second\/stream/)
  stream.close()
  assert.equal(states.at(-1), 'idle')
})
