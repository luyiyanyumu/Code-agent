import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { createIntegrationPreviewDocument, installIntegrationPreviewEnvironment } from '../src/lib/integrationPreview.js'

const preview = overrides => ({ html: '<!doctype html><html><head><title>Demo</title></head><body>App</body></html>', frontend_url: 'http://127.0.0.1:5173/app/', backend_url: 'http://127.0.0.1:8080/base/', base_url: 'http://127.0.0.1:19387/api/v1/code/frontend-design-assets/testticket/app/', asset_prefix: 'http://127.0.0.1:19387/api/v1/code/frontend-design-assets/testticket/', api_proxy_url: 'http://127.0.0.1:19387/api/v1/code/integration-api/testtoken/', api_prefix: '/api', session_id: 'test-session', ...overrides })
const fixture = config => {
  const calls = [], messages = [], listeners = new Map(), timers = []
  class Xhr {
    constructor() { this.sentHeaders = {}; this.events = new Map(); this.status = 201 }
    open(method, url, async) { this.opened = { method, url, async } }
    setRequestHeader(key, value) { this.sentHeaders[key] = value }
    send(body) { this.body = body; this.events.get('loadend')?.() }
    addEventListener(name, fn) { this.events.set(name, fn) }
  }
  Object.defineProperty(Xhr.prototype, 'withCredentials', { configurable: true, get() { return this.credentials || false }, set(value) { this.credentials = value } })
  const win = { URL, Headers, Request, Event, structuredClone, XMLHttpRequest: Xhr, document: { readyState: 'complete' }, location: { hash: '' },
    fetch: async input => { calls.push(input); return new Response('{"ok":true}', { status: 201, headers: { 'X-Code-Agent-Request-Id': 'request-1' } }) },
    parent: { postMessage: value => messages.push(value) }, setTimeout: fn => timers.push(fn),
    addEventListener(name, fn) { const list = listeners.get(name) || []; list.push(fn); listeners.set(name, list) },
    dispatchEvent(event) { for (const fn of listeners.get(event.type) || []) fn(event); return true },
  }
  Object.defineProperties(win, { localStorage: { configurable: true, get() { throw new Error('parent storage accessed') } }, sessionStorage: { configurable: true, get() { throw new Error('parent storage accessed') } } })
  const environment = installIntegrationPreviewEnvironment(win, { ...preview(config), channel: 'integration-test' })
  return { win, environment, calls, messages, timers }
}

test('srcdoc installs the separate bridge before application code and preserves code/comment/inert base literals', () => {
  const html = '<html><head><!-- <base href="comment"> --><base href="bad"><script>const literal="<base href=\'string\'>"</script><template><base href="inert"></template></head><body>App</body></html>'
  const result = createIntegrationPreviewDocument(preview({ html }), { channel: 'integration-test' })
  assert.ok(result.indexOf('data-code-agent-integration') < result.indexOf('const literal'))
  assert.ok(!result.includes('<base href="bad">')); assert.ok(result.includes('<base href="comment">')); assert.ok(result.includes('<base href="inert">')); assert.ok(result.includes("<base href='string'>"))
  assert.ok(result.includes('testticket/app/#/')); assert.ok(!result.includes('allow-same-origin'))
  const script = result.match(/<script data-code-agent-integration>([\s\S]*?)<\/script>/)[1]
  new vm.Script(script)
})

test('srcdoc validates capability resource addresses and escapes script-closing configuration data', () => {
  assert.throws(() => createIntegrationPreviewDocument(preview(), { channel: 'bad' }), /通道/)
  assert.throws(() => createIntegrationPreviewDocument(preview({ api_proxy_url: 'http://outside.test/api/v1/code/integration-api/t/' }), { channel: 'integration-test' }), /资源地址/)
  assert.throws(() => createIntegrationPreviewDocument(preview({ base_url: 'file:///tmp/page' }), { channel: 'integration-test' }), /资源地址/)
  const result = createIntegrationPreviewDocument(preview({ session_id: '</script><script>alert(1)</script>' }), { channel: 'integration-test' })
  assert.ok(!result.includes('</script><script>alert(1)')); assert.ok(result.includes('\\u003c/script>'))
})

test('fetch POST preserves body, sends only explicit project headers in an encoded envelope and omits platform credentials', async () => {
  const { win, calls, messages } = fixture()
  const response = await win.fetch('/api/users?sort=name', { method: 'POST', credentials: 'include', redirect: 'follow', headers: { Authorization: 'Bearer project-secret', 'Content-Type': 'application/json', 'X-Name': 'demo' }, body: '{"name":"中文"}' })
  const sent = calls[0]
  assert.equal(sent.url, preview().api_proxy_url + 'api/users?sort=name'); assert.equal(sent.method, 'POST')
  assert.equal(await sent.text(), '{"name":"中文"}'); assert.equal(sent.credentials, 'omit'); assert.equal(sent.redirect, 'error')
  assert.equal(sent.headers.get('authorization'), null); assert.equal(sent.headers.get('cookie'), null)
  const project = JSON.parse(decodeURIComponent(sent.headers.get('X-Code-Agent-Project-Headers')))
  assert.equal(project.authorization, 'Bearer project-secret'); assert.equal(project['content-type'], 'application/json')
  assert.equal(response.status, 201); assert.equal(messages.find(item => item.type === 'request').id, 'request-1')
})

test('fetch Request input keeps its body and override headers; backend base prefixes map exactly once', async () => {
  const { win, calls, environment } = fixture()
  const input = new Request('http://127.0.0.1:8080/base/users', { method: 'PATCH', headers: { Authorization: 'Bearer explicit' }, body: 'changed' })
  await win.fetch(input, { headers: { 'Content-Type': 'text/plain', 'X-Trace': 'trace-1' } })
  assert.equal(calls[0].url, preview().api_proxy_url + '__backend__/users'); assert.equal(await calls[0].text(), 'changed')
  assert.equal(JSON.parse(decodeURIComponent(calls[0].headers.get('X-Code-Agent-Project-Headers')))['x-trace'], 'trace-1')
  assert.equal(environment.mapped('http://127.0.0.1:8080/base').url, preview().api_proxy_url + '__backend__/')
  assert.throws(() => environment.mapped('http://127.0.0.1:8080/other'), /当前项目/)
})

test('foreign URLs, credentials, backslash escapes and unsupported methods do not reach fetch', async () => {
  const { win, calls, environment } = fixture()
  for (const path of ['https://outside.test/api', '//outside.test/api', 'http://user:pass@127.0.0.1:8080/base/users', '/api\\outside', 'javascript:alert(1)']) assert.throws(() => environment.mapped(path), /当前项目/)
  await assert.rejects(win.fetch('/api/users', { method: 'TRACE' }))
  assert.equal(calls.length, 0)
  assert.equal(environment.mapped('/assets/main.js').url, preview().asset_prefix + 'assets/main.js')
})

test('XHR bridges CRUD headers/body and prevents withCredentials from enabling browser cookies', () => {
  const { win, messages } = fixture(), xhr = new win.XMLHttpRequest()
  xhr.open('PUT', '/api/users/7'); xhr.withCredentials = true
  xhr.setRequestHeader('Authorization', 'Bearer project-token'); xhr.setRequestHeader('Content-Type', 'application/json'); xhr.send('{"active":true}')
  assert.equal(xhr.opened.method, 'PUT'); assert.equal(xhr.opened.url, preview().api_proxy_url + 'api/users/7')
  assert.equal(xhr.body, '{"active":true}'); assert.equal(xhr.withCredentials, false); assert.equal(xhr.sentHeaders.authorization, undefined)
  assert.equal(JSON.parse(decodeURIComponent(xhr.sentHeaders['x-code-agent-project-headers'])).authorization, 'Bearer project-token')
  assert.ok(messages.some(item => item.type === 'request' && item.status === 201))
  assert.throws(() => xhr.open('GET', '/api/users', true, 'user', 'password'), /Authorization/)
})

test('preview storage/cookies stay in memory and route history cannot leave the project capability', () => {
  const one = fixture(), two = fixture()
  one.win.localStorage.setItem('token', 'project-token'); one.win.sessionStorage.login = 'demo'
  assert.equal(one.win.localStorage.getItem('token'), 'project-token'); assert.equal(one.win.sessionStorage.getItem('login'), 'demo')
  assert.equal(two.win.localStorage.getItem('token'), null); assert.equal(one.win.document.cookie, '')
  one.win.document.cookie = 'auth=secret'; assert.equal(one.win.document.cookie, '')
  assert.throws(() => one.win.localStorage.setItem('large', 'x'.repeat(300_000)), /容量/)
  one.win.history.pushState({ page: 2 }, '', '#/users'); assert.equal(one.win.location.hash, '#/users'); assert.equal(one.win.history.length, 2)
  const state = one.win.history.state; state.page = 100; assert.equal(one.win.history.state.page, 2)
  one.win.history.replaceState({ page: 3 }, '', '/app/users?page=3'); assert.equal(one.win.location.hash, '#/app/users?page=3')
  assert.throws(() => one.win.history.pushState({}, '', 'https://outside.test/path'), /当前项目/)
  one.win.history.back(); one.timers.forEach(fn => fn()); assert.equal(one.win.history.state, null)
})
