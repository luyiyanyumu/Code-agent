import assert from 'node:assert/strict'
import test from 'node:test'
import { createDesktopBridge, createWorkbenchLeaveGuard } from '../src/lib/desktopRuntime.js'

function setup() {
  const messages = [], listeners = new Set()
  const host = { __CODE_AGENT_DESKTOP__: { integrated: true }, chrome: { webview: {
    postMessage: message => messages.push(message),
    addEventListener: (_, listener) => listeners.add(listener), removeEventListener: (_, listener) => listeners.delete(listener),
  } } }
  const bridge = createDesktopBridge(host)
  return { host, bridge, messages, listeners, respond: value => Promise.all([...listeners].map(listener => listener({ data: value }))) }
}

test('desktop requests correlate real responses and never send native credentials', async t => {
  const s = setup(); t.after(() => s.bridge.dispose())
  assert.equal(s.host.__CODE_AGENT_DESKTOP__.closeReady, true)
  const first = s.bridge.request('status'), second = s.bridge.request('logs', { service: 'backend', lines: 200 })
  assert.equal(s.messages.length, 2)
  const [a, b] = s.messages
  assert.notEqual(a.id, b.id)
  assert.deepEqual(a.params, {})
  assert.equal(JSON.stringify(s.messages).includes('token'), false)
  await s.respond({ channel: 'other', id: a.id, ok: true, result: 'ignored' })
  await s.respond({ channel: a.channel, id: b.id, ok: true, result: { text: 'actual-log' } })
  await s.respond({ channel: a.channel, id: a.id, ok: true, result: { version: '1.0.0', services: [] } })
  assert.deepEqual(await second, { text: 'actual-log' })
  assert.equal((await first).version, '1.0.0')
})

test('request error, bounded timeout, unknown method and browser fallback are explicit', async t => {
  const s = setup(); t.after(() => s.bridge.dispose())
  const failed = s.bridge.request('action', { action: 'restart', target: 'backend' })
  await s.respond({ channel: 'code-agent-desktop', id: s.messages[0].id, ok: false, error: '后台任务仍在执行' })
  await assert.rejects(failed, /后台任务仍在执行/)
  await assert.rejects(s.bridge.request('status', {}, { timeout: 3 }), /超时/)
  const count = s.messages.length
  await assert.rejects(s.bridge.request('shell'), /不支持/)
  assert.equal(s.messages.length, count)
  const browser = createDesktopBridge({})
  assert.equal(browser.available(), false)
  await assert.rejects(browser.request('status'), /仅在.*桌面应用/)
  browser.dispose()
})

test('native close waits for guards, reports cancellation, and disposal removes listeners', async () => {
  const s = setup()
  let captures = 0
  const unregister = s.bridge.registerLeaveGuard(async reason => { captures++; assert.equal(reason, 'close'); return false })
  await s.respond({ channel: 'code-agent-desktop', event: 'before-close', id: 'native-close-1' })
  assert.equal(captures, 1)
  assert.deepEqual(s.messages.at(-1), { channel: 'code-agent-desktop', id: 'native-close-1', method: 'close-response', params: { allow: false } })
  unregister()
  await s.respond({ channel: 'code-agent-desktop', event: 'before-close', id: 'native-close-2' })
  assert.equal(s.messages.at(-1).params.allow, true)
  const pending = s.bridge.request('status')
  s.bridge.dispose()
  await assert.rejects(pending, /窗口已关闭/)
  assert.equal(s.listeners.size, 0)
  assert.equal(s.host.__CODE_AGENT_DESKTOP__.closeReady, false)
})

test('native close acknowledges a pending decision immediately and keeps final permission separate', async t => {
  const s = setup(); t.after(() => s.bridge.dispose())
  let decide, calls = 0
  s.bridge.registerLeaveGuard(() => { calls++; return new Promise(resolve => { decide = resolve }) })
  const request = { channel: 'code-agent-desktop', event: 'before-close', id: 'native-pending-decision' }
  const completion = s.respond(request)
  assert.deepEqual(s.messages, [{ channel: 'code-agent-desktop', id: request.id, method: 'close-pending', params: {} }])
  assert.equal(calls, 1)
  await s.respond(request)
  assert.equal(calls, 1)
  assert.equal(s.messages.length, 1)
  decide(false)
  await completion
  assert.deepEqual(s.messages.at(-1), { channel: 'code-agent-desktop', id: request.id, method: 'close-response', params: { allow: false } })
})

test('a failed asynchronous close guard acknowledges receipt but never grants permission', async t => {
  const s = setup(); t.after(() => s.bridge.dispose())
  s.bridge.registerLeaveGuard(async () => { throw new Error('draft recovery failed') })
  await s.respond({ channel: 'code-agent-desktop', event: 'before-close', id: 'native-failed-decision' })
  assert.deepEqual(s.messages.map(({ method, params }) => ({ method, params })), [
    { method: 'close-pending', params: {} }, { method: 'close-response', params: { allow: false } },
  ])
})

test('workbench captures and flushes before leave, only confirms dirty files, and reports failed recovery honestly', async () => {
  const calls = [], prompts = []
  let dirty = false, flushOk = true, allow = false
  const guard = createWorkbenchLeaveGuard({ capture: () => calls.push('capture'), flush: () => { calls.push('flush'); return flushOk }, hasUnsaved: () => dirty, storageError: () => '存储不可用', confirm: message => { prompts.push(message); return allow } })
  assert.equal(await guard('close'), true)
  assert.deepEqual(calls, ['capture', 'flush'])
  assert.equal(prompts.length, 0)
  dirty = true
  assert.equal(await guard('restart'), false)
  assert.match(prompts.at(-1), /没有保存到项目文件/)
  assert.match(prompts.at(-1), /重新加载工作台/)
  flushOk = false; allow = true
  assert.equal(await guard('close'), true)
  assert.match(prompts.at(-1), /未能保存.*丢失/)
  assert.match(prompts.at(-1), /存储不可用/)
})

test('integrated titlebar requires the native capability and receives native window state without polling', async t => {
  const s = setup(); t.after(() => s.bridge.dispose())
  assert.equal(s.bridge.integratedTitlebar(), false, 'older desktop hosts retain their own caption')
  s.host.__CODE_AGENT_DESKTOP__.integratedTitlebar = true
  assert.equal(s.bridge.integratedTitlebar(), true)
  const states = []
  const release = s.bridge.subscribeWindowState(state => states.push(state))
  await s.respond({ channel: 'other', event: 'window-state', state: { maximized: true, minimized: false, active: true } })
  await s.respond({ channel: 'code-agent-desktop', event: 'window-state', state: { maximized: 'true', minimized: false, active: true } })
  assert.equal(states.length, 0)
  await s.respond({ channel: 'code-agent-desktop', event: 'window-state', state: { maximized: true, minimized: false, active: true, integratedTitlebar: true } })
  assert.deepEqual(states.at(-1), { maximized: true, minimized: false, active: true, integratedTitlebar: true })
  assert.equal(s.messages.length, 0, 'native state changes do not create polling requests')
  const initial = []
  s.bridge.subscribeWindowState(state => initial.push(state))()
  assert.equal(initial[0].maximized, true, 'late subscribers see the last native state')
  release()
  await s.respond({ channel: 'code-agent-desktop', event: 'window-state', state: { maximized: false, minimized: false, active: false, integratedTitlebar: true } })
  assert.equal(states.length, 1)
})

test('window actions keep request correlation and close permission stays with the native before-close flow', async t => {
  const s = setup(); t.after(() => s.bridge.dispose())
  let guarded = 0
  s.bridge.registerLeaveGuard(() => { guarded++; return false })
  const state = s.bridge.request('window-state')
  const first = s.messages.at(-1)
  await s.respond({ channel: first.channel, id: first.id, ok: true, result: { maximized: false, minimized: false, active: true, integratedTitlebar: true } })
  assert.equal((await state).maximized, false)
  for (const method of ['window-minimize', 'window-toggle-maximize', 'window-close']) {
    const action = s.bridge.request(method)
    const sent = s.messages.at(-1)
    assert.equal(sent.method, method)
    await s.respond({ channel: sent.channel, id: sent.id, ok: true, result: { accepted: true } })
    assert.deepEqual(await action, { accepted: true })
  }
  assert.equal(guarded, 0, 'buttons do not bypass or pre-run the native close guard')
  await s.respond({ channel: 'code-agent-desktop', event: 'before-close', id: 'caption-close' })
  assert.equal(guarded, 1)
  assert.equal(s.messages.at(-1).method, 'close-response')
  assert.equal(s.messages.at(-1).params.allow, false)
})
