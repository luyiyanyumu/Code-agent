import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { installDesignEnvironment } from '../src/lib/frontendDesignEnvironment.js'

const baseUrl = 'http://127.0.0.1:8765/api/v1/code/frontend-design-assets/temporary-ticket/frontend/dist/'
class PreviewPopStateEvent extends Event {
  constructor(type, options) { super(type); this.state = options.state }
}
function windowFixture() {
  const events = []
  const listeners = new Map()
  const pending = []
  let hash = ''
  let nativeReads = 0
  const win = {
    URL, DOMException, structuredClone, Event, PopStateEvent: PreviewPopStateEvent,
    setTimeout(callback) { pending.push(callback) },
    addEventListener(type, listener, capture) {
      const handlers = listeners.get(type) || []
      handlers.push({ listener, capture: capture === true })
      handlers.sort((a, b) => Number(b.capture) - Number(a.capture))
      listeners.set(type, handlers)
    },
    dispatchEvent(event) {
      events.push({ type: event.type, hash, state: event.state })
      let stopped = false
      event.stopImmediatePropagation = () => { stopped = true }
      for (const { listener } of listeners.get(event.type) || []) {
        listener(event)
        if (stopped) break
      }
      return true
    },
    location: {
      pathname: 'srcdoc', href: 'about:srcdoc', origin: 'null',
      get hash() { return hash },
      set hash(value) {
        const before = hash
        hash = String(value)
        if (before !== hash) pending.push(() => win.dispatchEvent(new Event('hashchange')))
      },
      assign() { throw new Error('Real navigation must not be used') },
      replace() { throw new Error('Real navigation must not be used') },
    },
  }
  for (const name of ['localStorage', 'sessionStorage', 'history']) {
    Object.defineProperty(win, name, { configurable: true, get() { nativeReads++; throw new DOMException('opaque', 'SecurityError') } })
  }
  return {
    win, events,
    flush() { while (pending.length) pending.shift()() },
    reads() { return nativeReads },
  }
}

function withDom(fixture) {
  const assignments = []
  class Element {
    constructor(tag) { this.localName = tag; this.attributes = new Map() }
    getAttribute(name) { return this.attributes.get(String(name).toLowerCase()) ?? null }
    setAttribute(name, value) {
      const key = String(name).toLowerCase()
      const text = String(value)
      assignments.push({ tag: this.localName, attribute: key, value: text })
      this.attributes.set(key, text)
    }
  }
  const rawSetAttribute = Element.prototype.setAttribute
  class HTMLScriptElement extends Element { constructor() { super('script') } }
  class HTMLLinkElement extends Element { constructor() { super('link') } }
  class HTMLImageElement extends Element { constructor() { super('img') } }
  for (const [constructor, fields] of [[HTMLScriptElement, ['src']], [HTMLLinkElement, ['href', 'rel', 'as']], [HTMLImageElement, ['src', 'srcset']]]) {
    for (const field of fields) Object.defineProperty(constructor.prototype, field, {
      configurable: true, enumerable: true,
      get() { return this.getAttribute(field) ?? '' },
      set(value) { rawSetAttribute.call(this, field, value) },
    })
  }
  Object.assign(fixture.win, { Element, HTMLScriptElement, HTMLLinkElement, HTMLImageElement })
  return { ...fixture, assignments, Element, HTMLScriptElement, HTMLLinkElement, HTMLImageElement }
}

const apiProxyUrl = 'http://127.0.0.1:8765/api/v1/code/frontend-design-api/temporary-ticket/'
const apiUrl = 'http://127.0.0.1:18080/api/'
function withApi(fixture) {
  const fetchCalls = []
  const xhrCalls = []
  class XMLHttpRequest {
    constructor() { this.headers = new Map(); this._credentials = false }
    open(method, url, asynchronous) { xhrCalls.push({ method, url, asynchronous }); this.opened = true }
    setRequestHeader(name, value) { this.headers.set(String(name).toLowerCase(), String(value)) }
    send(body) { this.body = body; this.sent = true }
  }
  Object.defineProperty(XMLHttpRequest.prototype, 'withCredentials', { configurable: true,
    get() { return this._credentials }, set(value) { this._credentials = Boolean(value) },
  })
  Object.assign(fixture.win, {
    Request, Headers, XMLHttpRequest,
    fetch: async (request, options) => { fetchCalls.push({ request, options }); return { ok: true, json: async () => ({ notes: [] }) } },
  })
  return { ...fixture, fetchCalls, xhrCalls }
}

test('opaque-origin getters are not read and independent fresh storage implements Storage', () => {
  const fixture = windowFixture()
  const info = installDesignEnvironment(fixture.win, { baseUrl })
  const { localStorage: local, sessionStorage: session } = fixture.win
  assert.deepEqual(info, { storage: 'memory', history: 'hash', persistent: false })
  assert.equal(fixture.reads(), 0)
  assert.equal(local.getItem('missing'), null)
  local.setItem(12, true)
  local.setItem(null, undefined)
  local.setItem('empty', '')
  assert.equal(local.length, 3)
  assert.equal(local.key(0), '12')
  assert.equal(local.key(1), 'null')
  assert.equal(local.key(-1), null)
  assert.equal(local.key(Infinity), null)
  assert.equal(local.getItem(12), 'true')
  assert.equal(local.getItem('null'), 'undefined')
  assert.equal(local.getItem('empty'), '')
  assert.equal(session.getItem(12), null)
  session.setItem(12, 'separate')
  local.removeItem(12)
  assert.equal(session.getItem(12), 'separate')
  local.clear()
  assert.equal(local.length, 0)
  assert.equal(Object.prototype.toString.call(local), '[object Storage]')
  assert.equal(installDesignEnvironment(windowFixture().win, { baseUrl }).persistent, false)
})

test('storage property reads, writes, deletion and enumeration never pollute prototypes', () => {
  const { win } = windowFixture()
  installDesignEnvironment(win, { baseUrl })
  const store = win.localStorage
  store.foo = 42
  store.__proto__ = 'safe'
  store.constructor = 'still a value'
  assert.equal(store.foo, '42')
  assert.equal(store.getItem('foo'), '42')
  assert.equal(store.__proto__, 'safe')
  assert.equal(store.constructor, 'still a value')
  assert.equal(Object.getPrototypeOf(store), null)
  assert.deepEqual(Object.keys(store), ['foo', '__proto__', 'constructor'])
  assert.equal('foo' in store, true)
  assert.equal('absent' in store, false)
  delete store.foo
  assert.equal(store.foo, undefined)
  assert.equal(store.getItem('foo'), null)
  assert.deepEqual(Object.keys(store), ['__proto__', 'constructor'])
  assert.equal(Object.prototype.safe, undefined)
  store.setItem('getItem', 'kept through method')
  assert.equal(typeof store.getItem, 'function')
  assert.equal(store.getItem('getItem'), 'kept through method')
})

test('storage counts unicode bytes and enforces atomic entry and replacement quotas', () => {
  const { win } = windowFixture()
  installDesignEnvironment(win, { baseUrl })
  const store = win.localStorage
  store.setItem('x', '原值')
  assert.throws(() => store.setItem('x', '汉'.repeat(90000)), error => error.name === 'QuotaExceededError')
  assert.equal(store.getItem('x'), '原值')
  assert.throws(() => store.setItem('emoji', '😀'.repeat(70000)), error => error.name === 'QuotaExceededError')
  assert.equal(store.getItem('emoji'), null)
  store.clear()
  for (let index = 0; index < 512; index++) store.setItem('key' + index, 'small')
  assert.throws(() => store.setItem('extra', 'no'), error => error.name === 'QuotaExceededError')
  store.setItem('key0', 'replacement')
  assert.equal(store.length, 512)
  store.removeItem('key1')
  store.setItem('extra', 'yes')
  assert.equal(store.length, 512)
})

test('hash history clones its state and push/replace never emit popstate or navigate', () => {
  const fixture = windowFixture()
  const { win } = fixture
  installDesignEnvironment(win, { baseUrl })
  fixture.flush()
  fixture.events.length = 0
  const state = { position: 1, nested: { name: 'original' }, date: new Date(0), list: new Set(['a']) }
  win.history.replaceState({ position: 0 }, '', baseUrl + '#/')
  win.history.pushState(state, '', baseUrl + '#/items?q=one')
  state.nested.name = 'mutated'
  const fetched = win.history.state
  assert.equal(fetched.nested.name, 'original')
  assert.equal(fetched.date.getTime(), 0)
  assert.deepEqual([...fetched.list], ['a'])
  fetched.nested.name = 'getter mutation'
  assert.equal(win.history.state.nested.name, 'original')
  assert.equal(win.location.hash, '#/items?q=one')
  assert.equal(win.history.length, 2)
  fixture.flush()
  assert.deepEqual(fixture.events.map(event => event.type), ['hashchange'])
  win.history.replaceState({ changed: true }, '', '#/items/2')
  fixture.flush()
  assert.equal(win.history.length, 2)
  assert.deepEqual(win.history.state, { changed: true })
  assert.equal(fixture.events.some(event => event.type === 'popstate'), false)
  win.history.scrollRestoration = 'manual'
  win.history.scrollRestoration = 'invalid'
  assert.equal(win.history.scrollRestoration, 'manual')
  assert.equal(fixture.reads(), 0)
})

test('hash history rejects external URLs, APIs, scripts and escape paths without state changes or ticket leaks', () => {
  const fixture = windowFixture()
  installDesignEnvironment(fixture.win, { baseUrl })
  const bad = [
    'https://external.invalid/#/items', '/api/v1/sessions#/items', 'javascript:alert(1)#/',
    'data:text/html,hi#/', baseUrl + '../other/#/', baseUrl + '%2foutside#/',
    baseUrl + 'index.html', '/items', '//evil.invalid/#/items', 'http://user:password@127.0.0.1:8765' + new URL(baseUrl).pathname + '#/',
  ]
  for (const value of bad) {
    assert.throws(() => fixture.win.history.pushState({ invalid: true }, '', value), error => {
      assert.equal(error.name, 'SecurityError')
      assert.equal(error.message.includes('temporary-ticket'), false)
      assert.equal(error.message.includes('password'), false)
      return true
    })
    assert.equal(fixture.win.location.hash, '#/')
    assert.equal(fixture.win.history.length, 1)
    assert.equal(fixture.win.history.state, null)
  }
  assert.throws(() => fixture.win.history.pushState({ callback() {} }, '', '#/valid'), error => error.name === 'DataCloneError')
  assert.throws(() => fixture.win.history.pushState({ big: '汉'.repeat(90000) }, '', '#/valid'), error => error.name === 'QuotaExceededError')
  assert.equal(fixture.win.location.hash, '#/')
  assert.equal(fixture.win.history.length, 1)
})

test('back/forward emits cloned popstate after correct hash and a fresh push discards forward entries', () => {
  const fixture = windowFixture()
  const { win } = fixture
  installDesignEnvironment(win, { baseUrl })
  win.history.replaceState({ position: 0 }, '', '#/')
  win.history.pushState({ position: 1 }, '', '#/first')
  win.history.pushState({ position: 2 }, '', '#/second')
  fixture.flush()
  const pops = []
  win.addEventListener('popstate', event => pops.push({ hash: win.location.hash, state: event.state }))
  win.history.back()
  assert.equal(pops.length, 0)
  assert.equal(win.location.hash, '#/first')
  fixture.flush()
  assert.deepEqual(pops, [{ hash: '#/first', state: { position: 1 } }])
  win.history.forward()
  fixture.flush()
  assert.deepEqual(pops[1], { hash: '#/second', state: { position: 2 } })
  win.history.go(-2)
  fixture.flush()
  assert.deepEqual(pops[2], { hash: '#/', state: { position: 0 } })
  win.history.pushState({ position: 1 }, '', '#/replacement')
  assert.equal(win.history.length, 2)
  win.history.forward()
  win.history.go(0)
  win.history.go(Infinity)
  fixture.flush()
  assert.equal(pops.length, 3)
  assert.equal(win.location.hash, '#/replacement')
  win.dispatchEvent(new PreviewPopStateEvent('popstate', { state: { native: true } }))
  assert.equal(pops.length, 3)
})

test('serialized installation has no module closure and reports unsupported browsers truthfully', () => {
  const fixture = windowFixture()
  const context = { win: fixture.win, options: { baseUrl }, ArrayBuffer, Map, Set, WeakSet, Symbol, Object, Reflect, Error }
  vm.runInNewContext('(' + installDesignEnvironment.toString() + ')(win, options)', context)
  fixture.win.localStorage.setItem('standalone', 'works')
  assert.equal(fixture.win.localStorage.getItem('standalone'), 'works')
  assert.equal(fixture.win.location.hash, '#/')
  assert.throws(() => installDesignEnvironment(windowFixture().win, { baseUrl: 'javascript:secret-ticket' }), error => error.name === 'SecurityError' && !error.message.includes('secret-ticket'))
  const unsupported = windowFixture().win
  Object.defineProperty(unsupported, 'localStorage', { configurable: false, get() { throw new Error('never read') } })
  assert.throws(() => installDesignEnvironment(unsupported, { baseUrl }), error => error.name === 'NotSupportedError')
})

test('initial route supports local pathname/query but rejects external or oversized initialization', () => {
  const fixture = windowFixture()
  installDesignEnvironment(fixture.win, { baseUrl, initialRoute: '/library?folder=3' })
  assert.equal(fixture.win.location.hash, '#/library?folder=3')
  for (const initialRoute of ['//outside.invalid/path', 'https://outside.invalid/path', 'javascript:code()', 'relative', '/bad\nroute', '/' + 'x'.repeat(4096)]) {
    assert.throws(() => installDesignEnvironment(windowFixture().win, { baseUrl, initialRoute }), error => error.name === 'SecurityError')
  }
})

test('same-route traversal preserves cyclic cloned states and history capacity stays bounded', () => {
  const fixture = windowFixture()
  const { win } = fixture
  installDesignEnvironment(win, { baseUrl })
  const circular = { position: 0 }
  circular.self = circular
  win.history.replaceState(circular, '', '#/same')
  win.history.pushState({ position: 1 }, '', '#/same')
  const pops = []
  win.addEventListener('popstate', event => pops.push(event.state))
  win.history.back()
  fixture.flush()
  assert.equal(pops.length, 1)
  assert.equal(pops[0].self, pops[0])
  assert.equal(pops[0].position, 0)
  for (let position = 1; position <= 300; position++) win.history.pushState({ position }, '', '#/entry-' + position)
  assert.equal(win.history.length, 256)
  win.history.go(-255)
  fixture.flush()
  assert.equal(win.history.state.position, 45)
  assert.equal(win.location.hash, '#/entry-45')
  win.history.back()
  fixture.flush()
  assert.equal(win.history.state.position, 45)
})

test('dynamic resource setters map root-relative assets before a native property or attribute can request them', () => {
  const fixture = withDom(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl, resourceRootUrl: baseUrl })
  const script = new fixture.HTMLScriptElement()
  script.src = '/assets/Dashboard.js?v=123'
  assert.equal(script.src, baseUrl + 'assets/Dashboard.js?v=123')
  const sheet = new fixture.HTMLLinkElement()
  sheet.rel = 'stylesheet'
  sheet.href = '/assets/Dashboard.css'
  assert.equal(sheet.href, baseUrl + 'assets/Dashboard.css')
  const preload = new fixture.HTMLLinkElement()
  preload.setAttribute('rel', 'modulepreload')
  preload.setAttribute('href', '/assets/StateView.mjs')
  assert.equal(preload.href, baseUrl + 'assets/StateView.mjs')
  const image = new fixture.HTMLImageElement()
  image.src = '/images/logo.svg#icon'
  image.setAttribute('srcset', '/images/logo.webp 1x, /images/logo@2x.webp 2x, ./local.webp 3x')
  assert.equal(image.src, baseUrl + 'images/logo.svg#icon')
  assert.equal(image.srcset, baseUrl + 'images/logo.webp 1x, ' + baseUrl + 'images/logo@2x.webp 2x, ./local.webp 3x')
  assert.equal(fixture.assignments.some(item => ['src', 'href', 'srcset'].includes(item.attribute) && item.value.startsWith('/')), false)
})

test('resource root is a validated ancestor inside the same capability and already rewritten URLs remain unchanged', () => {
  const resourceRootUrl = 'http://127.0.0.1:8765/api/v1/code/frontend-design-assets/temporary-ticket/'
  const fixture = withDom(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl, resourceRootUrl })
  const script = new fixture.HTMLScriptElement()
  script.src = '/src/api/demo.js'
  assert.equal(script.src, resourceRootUrl + 'src/api/demo.js')
  const mapped = resourceRootUrl + 'assets/lazy.js'
  script.setAttribute('src', mapped)
  assert.equal(script.src, mapped)
  script.src = './relative.js'
  assert.equal(script.src, './relative.js')
  for (const root of [
    'https://outside.invalid/', 'http://127.0.0.1:8765/',
    'http://127.0.0.1:8765/api/v1/code/frontend-design-assets/another-ticket/',
    resourceRootUrl + 'another-subdirectory/', resourceRootUrl + '?token=credential',
  ]) assert.throws(() => installDesignEnvironment(withDom(windowFixture()).win, { baseUrl, resourceRootUrl: root }), error => error.name === 'SecurityError' && !error.message.includes('temporary-ticket'))
})

test('dynamic resource mapping denies APIs, hidden files, secrets, traversal and external hosts without native assignments', () => {
  const fixture = withDom(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl, resourceRootUrl: baseUrl })
  const script = new fixture.HTMLScriptElement()
  const invalid = [
    '/api/action.js', '/secrets/private.js', '/credentials/user.js', '/assets/.private.js', '/assets/secret.js',
    '/assets/../outside.js', '/assets/%2e%2e/outside.js', '/assets/%252e%252e/outside.js', '/assets/./outside.js', '/assets/%2e/outside.js',
    '/assets/x%2fy.js', '/assets/x%5cy.js', '/assets/x.js\n', '/assets/data.json', '/assets/file.js?token=credential',
    'https://outside.invalid/lazy.js', '//outside.invalid/lazy.js',
    'http://user:password@127.0.0.1:8765' + new URL(baseUrl).pathname + 'lazy.js',
    'javascript:alert(1)', 'data:text/javascript,alert(1)',
  ]
  for (const value of invalid) {
    assert.throws(() => { script.src = value }, error => error.name === 'SecurityError' && !error.message.includes('credential') && !error.message.includes('temporary-ticket'))
    assert.throws(() => script.setAttribute('src', value), error => error.name === 'SecurityError')
  }
  assert.deepEqual(fixture.assignments, [])
})

test('late link rel/as assignments map pending href before enabling preload without changing anchors/forms/iframes or issuing requests', () => {
  const fixture = withDom(windowFixture())
  let fetchCalls = 0
  const fetch = async () => { fetchCalls++; return true }
  fixture.win.fetch = fetch
  installDesignEnvironment(fixture.win, { baseUrl, resourceRootUrl: baseUrl })
  const link = new fixture.HTMLLinkElement()
  link.href = '/assets/lazy.css'
  fixture.assignments.length = 0
  link.rel = 'stylesheet'
  assert.deepEqual(fixture.assignments, [
    { tag: 'link', attribute: 'href', value: baseUrl + 'assets/lazy.css' },
    { tag: 'link', attribute: 'rel', value: 'stylesheet' },
  ])
  const font = new fixture.HTMLLinkElement()
  font.setAttribute('rel', 'preload')
  font.href = '/fonts/ui.woff2'
  font.setAttribute('as', 'font')
  assert.equal(font.href, baseUrl + 'fonts/ui.woff2')
  for (const [tag, attribute] of [['a', 'href'], ['form', 'action'], ['iframe', 'src'], ['div', 'data-src']]) {
    const element = new fixture.Element(tag)
    element.setAttribute(attribute, '/assets/keep.css')
    assert.equal(element.getAttribute(attribute), '/assets/keep.css')
  }
  const image = new fixture.HTMLImageElement()
  image.src = 'data:image/svg+xml,<svg></svg>'
  assert.equal(image.src, 'data:image/svg+xml,<svg></svg>')
  assert.equal(typeof fixture.win.fetch, 'function')
  assert.equal(fetchCalls, 0)
})

test('Vite import.meta.resolve absolute assets are still validated and mapped before native loading', () => {
  const fixture = withDom(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl, resourceRootUrl: baseUrl })
  // This is the value import.meta.resolve('/assets/...') returns from a module
  // served through the resource proxy, rather than Vite 6's raw '/assets/...'.
  const origin = new URL(baseUrl).origin
  const script = new fixture.HTMLScriptElement()
  script.src = origin + '/assets/StateView.js?v=compiled#module'
  assert.equal(script.src, baseUrl + 'assets/StateView.js?v=compiled#module')
  const sheet = new fixture.HTMLLinkElement()
  sheet.rel = 'stylesheet'
  sheet.setAttribute('href', origin + '/assets/Dashboard.css')
  assert.equal(sheet.href, baseUrl + 'assets/Dashboard.css')
  const preload = new fixture.HTMLLinkElement()
  preload.rel = 'modulepreload'
  preload.href = '//' + new URL(baseUrl).host + '/assets/Dashboard.mjs'
  assert.equal(preload.href, baseUrl + 'assets/Dashboard.mjs')
  assert.equal(fixture.assignments.filter(item => ['src', 'href'].includes(item.attribute)).every(item => item.value.startsWith(baseUrl)), true)
  const before = fixture.assignments.length
  for (const path of [
    '/api/action.js', '/api/v1/code/frontend-design-assets/other-ticket/assets/leaked.js',
    '/assets/.private.js', '/secrets/plain.js', '/assets/secret.js', '/assets/file.js?api_key=credential',
    '/assets/../outside.js', '/assets/%2e%2e/outside.js', '/assets/%252e%252e/outside.js',
    '/assets/x%2fy.js', '/assets/x%5cy.js', '/assets/./outside.js', '/assets/%2e/outside.js', '/assets/data.json',
  ]) {
    assert.throws(() => { script.src = origin + path }, error => error.name === 'SecurityError')
    assert.throws(() => script.setAttribute('src', '//' + new URL(baseUrl).host + path), error => error.name === 'SecurityError')
  }
  assert.throws(() => { sheet.href = 'https://outside.invalid/assets/page.css' }, error => error.name === 'SecurityError')
  assert.equal(fixture.assignments.length, before)
})

test('URL only repairs explicit opaque document bases and preserves native errors and static methods', () => {
  const fixture = withApi(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl })
  const PreviewURL = fixture.win.URL
  for (const documentBase of ['about:srcdoc', 'about:srcdoc#/', 'about:blank', 'about:blank?x=1', 'null', null]) {
    assert.equal(new PreviewURL('/api/notes', documentBase).href, 'http://127.0.0.1:8765/api/notes')
  }
  assert.equal(new PreviewURL('./note', 'https://example.invalid/root/').href, 'https://example.invalid/root/note')
  assert.throws(() => new PreviewURL('/api/notes'), TypeError)
  assert.throws(() => new PreviewURL('/api/notes', 'invalid-document-base'), TypeError)
  assert.equal(PreviewURL.canParse('https://example.invalid/'), URL.canParse('https://example.invalid/'))
  assert.equal(PreviewURL.canParse('/api/notes', 'invalid'), URL.canParse('/api/notes', 'invalid'))
  assert.equal(PreviewURL.createObjectURL, URL.createObjectURL)
  assert.equal(PreviewURL.revokeObjectURL, URL.revokeObjectURL)
  const blob = PreviewURL.createObjectURL(new Blob(['isolated']))
  assert.ok(blob.startsWith('blob:'))
  PreviewURL.revokeObjectURL(blob)
})

test('unconfigured API fails clearly before native requests while relative Request construction succeeds', async () => {
  const fixture = withApi(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl })
  const request = new fixture.win.Request('/api/notes?limit=5')
  assert.equal(request.url, 'http://127.0.0.1:8765/api/notes?limit=5')
  for (const input of ['/api/notes', request, new fixture.win.URL('/api/notes', 'about:srcdoc')]) {
    await assert.rejects(fixture.win.fetch(input), error => error.name === 'InvalidStateError' && error.message.includes('项目接口尚未连接'))
  }
  const xhr = new fixture.win.XMLHttpRequest()
  assert.throws(() => xhr.open('GET', '/api/notes'), error => error.name === 'InvalidStateError' && error.message.includes('项目后端 API 地址'))
  assert.deepEqual(fixture.fetchCalls, [])
  assert.deepEqual(fixture.xhrCalls, [])
})

test('fetch maps virtual and configured backend API URLs, preserving read metadata but dropping credentials', async () => {
  const fixture = withApi(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl, apiProxyUrl, apiUrl })
  await fixture.win.fetch('/api/notes?limit=5&folder=2')
  assert.equal(fixture.fetchCalls[0].request.url, apiProxyUrl + 'notes?limit=5&folder=2')
  assert.equal(fixture.fetchCalls[0].request.credentials, 'omit')
  await fixture.win.fetch(apiUrl + 'notes/3', { method: 'HEAD' })
  assert.equal(fixture.fetchCalls[1].request.url, apiProxyUrl + 'notes/3')
  assert.equal(fixture.fetchCalls[1].request.method, 'HEAD')
  const controller = new AbortController()
  const request = new fixture.win.Request('/api/notes?limit=1', {
    signal: controller.signal, credentials: 'include', cache: 'no-store',
    headers: { Accept: 'application/json', 'X-Request-Trace': 'read-check', Authorization: 'must-not-forward', Cookie: 'must-not-forward', 'X-Api-Key': 'must-not-forward' },
  })
  await fixture.win.fetch(request)
  const forwarded = fixture.fetchCalls[2].request
  assert.equal(forwarded.url, apiProxyUrl + 'notes?limit=1')
  assert.equal(forwarded.headers.get('accept'), 'application/json')
  assert.equal(forwarded.headers.get('x-request-trace'), 'read-check')
  for (const name of ['authorization', 'cookie', 'x-api-key']) assert.equal(forwarded.headers.has(name), false)
  assert.equal(forwarded.credentials, 'omit')
  assert.equal(forwarded.cache, 'no-store')
  assert.equal(forwarded.redirect, 'error')
  assert.equal(forwarded.referrerPolicy, 'no-referrer')
  controller.abort()
  assert.equal(forwarded.signal.aborted, true)
  await fixture.win.fetch(apiProxyUrl + 'notes')
  assert.equal(fixture.fetchCalls[3].request.url, apiProxyUrl + 'notes')
})

test('XHR routes GET/HEAD to the same API capability and cannot send browser credentials or bodies', () => {
  const fixture = withApi(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl, apiProxyUrl, apiUrl })
  const xhr = new fixture.win.XMLHttpRequest()
  xhr.withCredentials = true
  xhr.open('GET', new fixture.win.URL('/api/notes?limit=2', 'about:srcdoc').href)
  xhr.setRequestHeader('Accept', 'application/json')
  xhr.setRequestHeader('Authorization', 'must-not-forward')
  xhr.setRequestHeader('Cookie', 'must-not-forward')
  xhr.withCredentials = true
  xhr.send(null)
  assert.equal(xhr.withCredentials, false)
  assert.deepEqual([...xhr.headers], [['accept', 'application/json']])
  assert.equal(xhr.body, null)
  assert.deepEqual(fixture.xhrCalls, [{ method: 'GET', url: apiProxyUrl + 'notes?limit=2', asynchronous: true }])
  assert.throws(() => xhr.send('body'), error => error.name === 'NotAllowedError' && error.message.includes('只读'))
  assert.throws(() => xhr.open('GET', '/api/notes', true, 'user', 'password'), error => error.name === 'SecurityError')
  const head = new fixture.win.XMLHttpRequest()
  head.open('head', apiUrl + 'notes/3', false)
  assert.deepEqual(fixture.xhrCalls[1], { method: 'HEAD', url: apiProxyUrl + 'notes/3', asynchronous: false })
})

test('API writes, editor management, sensitive paths, traversal, foreign hosts and other tickets never reach native APIs', async () => {
  const fixture = withApi(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl, apiProxyUrl, apiUrl })
  const xhr = new fixture.win.XMLHttpRequest()
  for (const verb of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    await assert.rejects(fixture.win.fetch('/api/notes', { method: verb }), error => error.name === 'NotAllowedError' && error.message.includes('另行授权'))
    assert.throws(() => xhr.open(verb, '/api/notes'), error => error.name === 'NotAllowedError')
  }
  for (const input of [
    '/api/notes/../private', '/api/notes/%2e%2e/private', '/api/notes/%252e%252e/private', '/api/notes/%25252e%25252e/private', '/api/notes%2fprivate', '/api//notes',
    '/api/.env', '/api/credentials', '/api/%2563redentials', '/api/secrets', '/api/env', '/api/notes/private-key.pem', '/api/http:outside',
    '/api/v1/code/projects', '/api/v1/sessions', '/api/notes?access_token=credential', '/api/notes?session_id=credential',
    'http://outside.invalid/api/notes', 'http://127.0.0.1:18081/api/notes',
    'http://user:password@127.0.0.1:18080/api/notes',
    'http://127.0.0.1:8765/api/v1/code/frontend-design-api/other-ticket/notes',
    'http://127.0.0.1:8765/api/v1/code/frontend-design-assets/other-ticket/file.js',
  ]) {
    await assert.rejects(fixture.win.fetch(input), error => error.name === 'SecurityError' && !error.message.includes('temporary-ticket'))
    assert.throws(() => xhr.open('GET', input), error => error.name === 'SecurityError')
  }
  assert.throws(() => new fixture.win.Request('/api/notes/../private'), error => error.name === 'SecurityError')
  assert.deepEqual(fixture.fetchCalls, [])
  assert.deepEqual(fixture.xhrCalls, [])
})

test('API configuration binds its asset ticket and leaves asset loading within existing protection', async () => {
  for (const parameters of [
    { apiProxyUrl: apiProxyUrl.replace('temporary-ticket', 'other-ticket'), apiUrl },
    { apiProxyUrl: apiProxyUrl.replace('127.0.0.1', 'outside.invalid'), apiUrl },
    { apiProxyUrl, apiUrl: 'http://outside.invalid/api/' },
    { apiProxyUrl, apiUrl: 'http://user:password@127.0.0.1:18080/api/' },
    { apiProxyUrl, apiUrl: 'http://127.0.0.1:8765/api/' },
  ]) assert.throws(() => installDesignEnvironment(withApi(windowFixture()).win, { baseUrl, ...parameters }), error => error.name === 'SecurityError')
  const fixture = withApi(withDom(windowFixture()))
  installDesignEnvironment(fixture.win, { baseUrl, apiProxyUrl, apiUrl, resourceRootUrl: baseUrl })
  const script = new fixture.HTMLScriptElement()
  script.src = '/assets/StateView.js'
  assert.equal(script.src, baseUrl + 'assets/StateView.js')
  await fixture.win.fetch(script.src)
  assert.equal(fixture.fetchCalls[0].request.url, baseUrl + 'assets/StateView.js')
  assert.equal(fixture.fetchCalls[0].request.credentials, 'omit')
})

test('minified environment remains self-contained for opaque URL, fetch, storage and history', async () => {
  const { transform } = await import('esbuild')
  const source = (await transform('var install = ' + installDesignEnvironment.toString(), { minify: true, target: 'es2022' })).code
  const fixture = withApi(windowFixture())
  vm.runInNewContext(source + ';install(win,options);', {
    win: fixture.win, options: { baseUrl, apiProxyUrl, apiUrl }, ArrayBuffer, Map, Set, WeakSet, Symbol, Object, Reflect, Error,
  })
  assert.equal(new fixture.win.URL('/api/notes', 'about:srcdoc').href, 'http://127.0.0.1:8765/api/notes')
  await fixture.win.fetch(new fixture.win.Request('/api/notes'))
  assert.equal(fixture.fetchCalls[0].request.url, apiProxyUrl + 'notes')
  fixture.win.localStorage.example = 'temporary'
  assert.equal(fixture.win.localStorage.example, 'temporary')
  fixture.win.history.pushState({ position: 1 }, '', '#/notes')
  assert.equal(fixture.win.location.hash, '#/notes')
})

test('opaque XSRF cookie reads return an empty facade without reading or writing native cookies', () => {
  const fixture = withApi(windowFixture())
  let nativeReads = 0
  let nativeWrites = 0
  fixture.win.document = Object.create({})
  Object.defineProperty(Object.getPrototypeOf(fixture.win.document), 'cookie', {
    configurable: true,
    get() { nativeReads++; throw new DOMException('opaque document', 'SecurityError') },
    set() { nativeWrites++; throw new DOMException('opaque document', 'SecurityError') },
  })
  installDesignEnvironment(fixture.win, { baseUrl, apiProxyUrl, apiUrl })
  assert.equal(fixture.win.document.cookie, '')
  fixture.win.document.cookie = 'XSRF-TOKEN=must-not-persist'
  assert.equal(fixture.win.document.cookie, '')
  assert.equal(nativeReads, 0)
  assert.equal(nativeWrites, 0)
})

test('relative fetch and XHR resources stay under the controlled asset base without requiring API configuration', async () => {
  const fixture = withApi(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl })
  await fixture.win.fetch('./data.json?format=public')
  await fixture.win.fetch('data.json')
  await fixture.win.fetch('././assets/page-data.json')
  assert.deepEqual(fixture.fetchCalls.map(call => call.request.url), [
    baseUrl + 'data.json?format=public', baseUrl + 'data.json', baseUrl + 'assets/page-data.json',
  ])
  const xhr = new fixture.win.XMLHttpRequest()
  xhr.open('GET', 'data.json')
  xhr.open('HEAD', './data.json?format=public')
  assert.deepEqual(fixture.xhrCalls.map(call => call.url), [baseUrl + 'data.json', baseUrl + 'data.json?format=public'])
  for (const invalid of ['../data.json', './assets/../data.json', './.private.json', './credentials.json', '%2e%2e/data.json', './%25252e%25252e/data.json']) {
    await assert.rejects(fixture.win.fetch(invalid), error => error.name === 'SecurityError')
    assert.throws(() => xhr.open('GET', invalid), error => error.name === 'SecurityError')
  }
  await assert.rejects(fixture.win.fetch('api/notes'), error => error.name === 'InvalidStateError')
  assert.throws(() => xhr.open('GET', './api/notes'), error => error.name === 'InvalidStateError')
  assert.equal(fixture.fetchCalls.length, 3)
  assert.equal(fixture.xhrCalls.length, 2)
})

test('fetch signal null explicitly replaces an aborted Request signal while undefined preserves it', async () => {
  const fixture = withApi(windowFixture())
  installDesignEnvironment(fixture.win, { baseUrl, apiProxyUrl, apiUrl })
  const controller = new AbortController()
  const request = new fixture.win.Request('/api/notes', { signal: controller.signal })
  controller.abort()
  await fixture.win.fetch(request, { signal: null })
  assert.equal(fixture.fetchCalls[0].request.signal.aborted, false)
  await fixture.win.fetch(request, { signal: undefined })
  assert.equal(fixture.fetchCalls[1].request.signal.aborted, true)
})

test('API errors allow interceptors to annotate message while preserving their original explanation and names', async () => {
  const missing = withApi(windowFixture())
  installDesignEnvironment(missing.win, { baseUrl })
  const connected = withApi(windowFixture())
  installDesignEnvironment(connected.win, { baseUrl, apiProxyUrl, apiUrl })
  const failures = [
    [missing.win.fetch('/api/notes'), 'InvalidStateError', '项目接口尚未连接'],
    [connected.win.fetch('https://outside.invalid/api/notes'), 'SecurityError', '受控只读接口'],
    [connected.win.fetch('/api/notes', { method: 'POST' }), 'NotAllowedError', '写入操作需要另行授权'],
  ]
  for (const [promise, name, purpose] of failures) {
    const error = await promise.catch(error => error)
    assert.equal(error instanceof Error, true)
    assert.equal(error instanceof DOMException, false)
    assert.equal(error.name, name)
    const original = error.message
    assert.ok(original.includes(purpose))
    error.message = '项目请求提示：' + original
    assert.equal(error.message, '项目请求提示：' + original)
    assert.equal(error.name, name)
  }
  assert.throws(() => connected.win.localStorage.setItem('big', '汉'.repeat(90000)), error => error instanceof DOMException && error.name === 'QuotaExceededError')
  assert.throws(() => connected.win.history.pushState({}, '', 'https://outside.invalid/#/'), error => error instanceof DOMException && error.name === 'SecurityError')
})
