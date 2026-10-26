import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'
import {
  STYLE_FIELDS, MAX_LAYERS, normalizeLayers, normalizeTransformDeclarations, normalizeStyleDeclarations, serializeDesignCss, parseDesignCss,
  isManagedDesignCss, createDesignDocument, createStyleRequest, parseStyleResult,
  validateBridgeMessage, sanitizeDesignResourcePath, sanitizeDesignErrorMessage, describeDesignRuntimeError,
} from '../src/lib/frontendDesign.js'

const rectangle = { x: 100, y: 80, width: 100, height: 32, left: 100, top: 80, right: 200, bottom: 112 }
const element = {
  selector: '#submit', tag: 'button', text: '保存', styles: { color: '#334155', 'font-size': '16px' }, rect: rectangle,
}

test('visual declarations accept useful typography/layout while refusing CSS code and network values', () => {
  const original = {
    color: 'rgb(12, 34, 56)', 'background-color': '#f8fafc', 'font-family': '"微软雅黑", sans-serif',
    'font-size': 'clamp(14px, 2vw, 24px)', width: 'calc(100% - 20px)', padding: '8px 16px',
    'line-height': 1.5, opacity: .8, display: 'flex', 'justify-content': 'space-between',
    'box-shadow': '0 8px 20px rgba(15, 23, 42, .12)',
    height: '20px; background: url(https://example.invalid)',
    border: 'expression(alert(1))', margin: 'var(--secret)', gap: 'attr(data-secret)',
    position: 'fixed', content: 'url(https://example.invalid)',
  }
  const normalized = normalizeStyleDeclarations(original)
  assert.equal(normalized.color, 'rgb(12, 34, 56)')
  assert.equal(normalized['font-family'], '"微软雅黑", sans-serif')
  assert.equal(normalized.width, 'calc(100% - 20px)')
  assert.equal(normalized['line-height'], '1.5')
  assert.equal(normalized.opacity, '0.8')
  for (const property of ['height', 'border', 'margin', 'gap', 'position', 'content']) assert.equal(Object.hasOwn(normalized, property), false)
  assert.equal(original.height.includes(';'), true)
  assert.deepEqual(normalizeStyleDeclarations(null), {})
  assert.deepEqual(normalizeStyleDeclarations([]), {})
  assert.deepEqual(normalizeStyleDeclarations({ opacity: 1.1, 'font-weight': 3000, color: '#abc</style>' }), {})
  assert.equal(new Set(STYLE_FIELDS.map(field => field.property)).size, 21)
})

test('managed CSS round trips with metadata tied to its visible declarations', () => {
  const rules = [
    { selector: '#submit', declarations: { 'border-radius': '8px', color: '#334155' } },
    { selector: 'main > section:nth-of-type(2) > button:nth-of-type(1)', declarations: { padding: '8px 16px', 'font-weight': '600' } },
    { selector: '#save\\:primary', declarations: { opacity: '.9' } },
  ]
  const css = serializeDesignCss(rules)
  const parsed = parseDesignCss(css)
  assert.equal(isManagedDesignCss(css), true)
  assert.equal(parsed.length, 3)
  assert.deepEqual(parsed[0], { selector: '#submit', declarations: { color: '#334155', 'border-radius': '8px' } })
  assert.equal(serializeDesignCss(parsed), css)
  assert.equal(isManagedDesignCss(css.replace('#334155 !important', '#ffffff !important')), false)
  assert.deepEqual(parseDesignCss(css + '@import "https://example.invalid";'), [])
  assert.throws(() => parseDesignCss('body {color:red}', { strict: true }), /保留原文件/)
  assert.deepEqual(parseDesignCss(''), [])
  assert.equal(isManagedDesignCss(serializeDesignCss([])), true)
})

test('selector injection, tampered metadata and inherited prototype fields cannot create styles', () => {
  const unsafe = [
    '#submit } body { color: red', '#submit, body', '#x/*comment*/', 'body:has(input)',
    'input[value="secret"]', '#x</style><script>alert(1)</script>', '@import url(x)',
  ]
  for (const selector of unsafe) assert.deepEqual(parseDesignCss(serializeDesignCss([{ selector, declarations: { color: 'red' } }])), [])
  const declarations = Object.assign(Object.create({ color: 'red' }), { padding: '8px', constructor: 'url(x)', __proto__: null })
  assert.deepEqual(normalizeStyleDeclarations(declarations), { padding: '8px' })
  const metadata = btoa(JSON.stringify({ version: 1, rules: [{ selector: '#submit', declarations: { color: 'red', content: 'url(x)' } }] }))
  assert.equal(isManagedDesignCss(`/* code-agent-frontend-design:v1 ${metadata} */\n#submit {color:red}\n`), false)
})

test('AI style protocol only parses complete JSON and rejects unsafe partial replacements', () => {
  const result = { kind: 'frontend-style', request_id: 'design-17', selector: '#submit', declarations: { color: '#fff', 'background-color': '#334155' }, summary: '调整主按钮配色' }
  assert.deepEqual(parseStyleResult(JSON.stringify(result)), result)
  assert.deepEqual(parseStyleResult('```json\n' + JSON.stringify(result) + '\n```'), result)
  assert.equal(parseStyleResult('```json\n' + JSON.stringify(result)), null)
  assert.equal(parseStyleResult(JSON.stringify(result).slice(0, -1)), null)
  assert.equal(parseStyleResult('建议如下：\n' + JSON.stringify(result)), null)
  assert.equal(parseStyleResult(JSON.stringify({ ...result, kind: 'command' })), null)
  assert.equal(parseStyleResult(JSON.stringify({ ...result, selector: '#submit;body' })), null)
  assert.equal(parseStyleResult(JSON.stringify({ ...result, declarations: { color: 'red', 'background-color': 'url(https://example.invalid)' } })), null)
  assert.equal(parseStyleResult(JSON.stringify({ ...result, declarations: { color: 'red', position: 'fixed' } })), null)
  const prompt = createStyleRequest({ projectId: 'p-1', requestId: 'design-17', element: { ...element, text: '</script>'.repeat(1000) }, instruction: '更简洁'.repeat(3000) })
  assert.equal(prompt.includes('不要运行工具或修改文件'), true)
  assert.equal(prompt.includes('design-17'), true)
  assert.equal(prompt.includes('<script>'), false)
  assert.ok(prompt.length < 7000)
  assert.throws(() => createStyleRequest({ requestId: 'design-17', element: { selector: '} body {' } }), /选择页面元素/)
})

test('bridge validation binds iframe source and fresh channel and bounds DOM samples', () => {
  const frame = {}
  const channel = 'preview-new-17'
  const payload = { type: 'selected', channel, element }
  const options = { iframeWindow: frame, channel }
  assert.deepEqual(validateBridgeMessage({ source: frame, data: payload }, options), payload)
  assert.equal(validateBridgeMessage({ source: {}, data: payload }, options), null)
  assert.equal(validateBridgeMessage({ source: frame, data: { ...payload, channel: 'preview-old-16' } }, options), null)
  assert.equal(validateBridgeMessage({ source: frame, data: { ...payload, type: 'styles' } }, options), null)
  assert.equal(validateBridgeMessage({ source: frame, data: { ...payload, element: { ...element, rect: { ...rectangle, width: Infinity } } } }, options), null)
  assert.equal(validateBridgeMessage({ source: frame, data: { ...payload, element: { ...element, selector: 'a}body{' } } }, options), null)
  assert.deepEqual(validateBridgeMessage({ source: frame, data: { ...payload, element: null } }, options), { type: 'selected', channel, element: null })
  const selected = validateBridgeMessage({ source: frame, data: { ...payload, element: { ...element, text: 'x'.repeat(5000), styles: { color: 'red', position: 'fixed' } } } }, options)
  assert.equal(selected.element.text.length, 500)
  assert.deepEqual(selected.element.styles, { color: 'red' })
})

test('layers and shortcut messages enforce bounded parent-first trees and selection breadcrumbs', () => {
  const frame = {}, channel = 'preview-layers-17'
  const validate = data => validateBridgeMessage({ source: frame, data: { channel, ...data } }, { iframeWindow: frame, channel })
  const root = { selector: 'body', parentSelector: '', tag: 'body', name: '页面', hidden: false, childCount: 1 }
  const child = { selector: '#submit', parentSelector: 'body', tag: 'button', name: 'x'.repeat(500), hidden: true, childCount: 0 }
  const message = validate({ type: 'layers', nodes: [root, child], selectedSelector: '#submit', truncated: false })
  assert.equal(message.nodes[1].name.length, 120)
  assert.equal(message.nodes[1].hidden, true)
  assert.equal(message.selectedSelector, '#submit')
  for (const nodes of [[child, root], [root, root], [{ ...root, childCount: -1 }], [{ ...root, hidden: 1 }], [{ ...root, selector: 'body;script' }], Array(MAX_LAYERS + 1).fill(root)]) {
    assert.equal(normalizeLayers(nodes), null)
    assert.equal(validate({ type: 'layers', nodes, selectedSelector: '', truncated: false }), null)
  }
  assert.equal(validate({ type: 'layers', nodes: [], selectedSelector: '', truncated: 'yes' }), null)
  const ancestors = [{ selector: 'body', tag: 'body', name: '页面' }]
  assert.deepEqual(validate({ type: 'selected', element: { ...element, ancestors } }).element.ancestors, ancestors)
  for (const invalid of [[...ancestors, ...ancestors], [{ selector: element.selector, tag: 'button' }], Array(25).fill(ancestors[0])]) {
    assert.equal(validate({ type: 'selected', element: { ...element, ancestors: invalid } }), null)
  }
  for (const action of ['undo', 'redo', 'clear', 'pan-start', 'pan-end', 'select-tool', 'pan-tool', 'fit', 'focus']) assert.deepEqual(validate({ type: 'shortcut', action }), { type: 'shortcut', channel, action })
  assert.equal(validate({ type: 'shortcut', action: 'delete' }), null)
  assert.equal(validateBridgeMessage({ source: {}, data: { channel, type: 'layers', nodes: [], selectedSelector: '', truncated: false } }, { iframeWindow: frame, channel }), null)
})

test('resource diagnostics keep readable paths while removing capabilities, hosts and URL data', () => {
  const baseUrl = 'http://127.0.0.1:5298/api/v1/code/frontend-design-assets/capability-secret/frontend/'
  const resource = baseUrl + 'src/main.js?api_key=secret-query#secret-fragment'
  assert.equal(sanitizeDesignResourcePath(resource), 'frontend/src/main.js')
  assert.equal(sanitizeDesignResourcePath('./src/main.js?token=secret', { baseUrl }), 'frontend/src/main.js')
  assert.equal(sanitizeDesignResourcePath('https://secret-user:secret-password@private.invalid/assets/theme.css?signature=secret'), 'assets/theme.css')
  assert.equal(sanitizeDesignResourcePath('/api/v1/code/frontend-design-assets/capability-secret'), '')
  assert.equal(sanitizeDesignResourcePath('/api/v1/code/frontend-design-assets/capability-secret/%E6%A0%B7%E5%BC%8F.css?token=secret'), '样式.css')
  assert.equal(sanitizeDesignResourcePath('/api%252Fv1%252Fcode%252Ffrontend-design-assets%252Fcapability-secret%252Fsrc%252Fmain.js?token=secret'), 'src/main.js')
  assert.equal(sanitizeDesignResourcePath('/api/v1/code/frontend-design-api/capability-secret/notes?page=1'), 'notes')
  const apiMessage = sanitizeDesignErrorMessage('Unable to GET /api/v1/code/frontend-design-api/capability-secret/notes?page=1 capability-secret', { baseUrl })
  assert.match(apiMessage, /Unable to GET notes/)
  assert.equal(apiMessage.includes('capability-secret'), false)
  assert.equal(sanitizeDesignResourcePath('/src/main.js%3Ftoken%3Dsecret'), 'src/main.js')
  assert.equal(sanitizeDesignResourcePath('/src/%2e%2e/fallback.js'), 'fallback.js')
  for (const address of ['data:text/plain,secret', 'blob:http://private.invalid/secret', 'file:///C:/Users/secret.js', 'javascript:secret', '/src/%00secret', '/https%3A%2F%2Fuser%3Asecret%40private.invalid/app.js']) assert.equal(sanitizeDesignResourcePath(address), '')
  assert.equal(sanitizeDesignResourcePath('a'.repeat(400) + '.js').length, 320)
  assert.equal(sanitizeDesignResourcePath('a'.repeat(16385)), '')
  const message = sanitizeDesignErrorMessage(`Unable to import ${resource}; fallback //secret-user:secret-password@private.invalid/style.css?token=secret-query; then src/fallback.js?token=secret-query#secret-fragment capability-secret`, { baseUrl })
  assert.match(message, /frontend\/src\/main\.js/)
  assert.match(message, /style\.css/)
  assert.match(message, /src\/fallback\.js/)
  for (const secret of ['capability-secret', 'secret-user', 'secret-password', 'private.invalid', '127.0.0.1', 'secret-query', 'secret-fragment', 'frontend-design-assets']) assert.equal(message.includes(secret), false)
  assert.equal(sanitizeDesignErrorMessage('x'.repeat(1000)).length, 500)
  assert.equal(sanitizeDesignErrorMessage('Failed data:text/plain,secret and blob:http://host.invalid/secret').includes('secret'), false)
})

test('error bridge protocol validates bounded diagnostic fields and preserves legacy errors', () => {
  const frame = {}, channel = 'preview-error-17'
  const options = { iframeWindow: frame, channel }
  const validate = data => validateBridgeMessage({ source: frame, data: { type: 'error', channel, ...data } }, options)
  assert.deepEqual(validate({ message: '旧错误' }), { type: 'error', channel, message: '旧错误' })
  const path = 'https://user:password@host.invalid/api/v1/code/frontend-design-assets/capability-secret/src/main.js?token=secret#key'
  assert.deepEqual(validate({ error_kind: 'runtime', message: `Cannot import ${path}`, filename: path, line: 12, column: 7 }), {
    type: 'error', channel, error_kind: 'runtime', message: 'Cannot import src/main.js', filename: 'src/main.js', line: 12, column: 7,
  })
  assert.deepEqual(validate({ error_kind: 'resource', resource_type: 'script', resource_path: path, message: '失败' }), {
    type: 'error', channel, error_kind: 'resource', resource_type: 'script', resource_path: 'src/main.js', message: '失败',
  })
  for (const invalid of [
    { error_kind: 'other' }, { error_kind: 3 }, { resource_type: 'cookie' }, { resource_type: ['script'] },
    { resource_path: 'x'.repeat(321) }, { resource_path: {} }, { filename: 1 }, { filename: 'x'.repeat(321) },
    { line: '12' }, { line: -1 }, { line: 1.5 }, { line: Infinity }, { line: 1000001 }, { column: null }, { column: -1 },
  ]) assert.equal(validate({ message: '失败', ...invalid }), null)
  assert.equal(validate({ message: null }).message, '页面预览发生错误。')
  assert.equal(validate({ error_kind: 'navigation', message: '导航失败', ignored: 'secret' }).ignored, undefined)
})

test('generic runtime events recover the actual exception name and cause without sending its stack', () => {
  const baseUrl = 'http://127.0.0.1:5298/api/v1/code/frontend-design-assets/capability-secret/frontend/'
  const address = 'https://secret-user:secret-password@private.invalid/api/v1/code/frontend-design-assets/capability-secret/frontend/assets/index.js?api_key=secret-query#secret-fragment'
  const error = new DOMException('Failed to read localStorage from Window: access is denied.', 'SecurityError')
  Object.defineProperty(error, 'stack', { value: `SecurityError: RAW-STACK-MUST-NOT-BE-SENT\n    at setup (${address}:4:19)\n    at other (${address}:88:99)` })
  const details = describeDesignRuntimeError({ message: 'Uncaught', error }, { baseUrl })
  assert.deepEqual(details, {
    message: 'SecurityError: Failed to read localStorage from Window: access is denied.', filename: 'frontend/assets/index.js', line: 4, column: 19,
  })
  for (const secret of ['RAW-STACK', 'capability-secret', 'private.invalid', 'secret-query', 'secret-fragment', 'secret-user', 'secret-password']) assert.equal(JSON.stringify(details).includes(secret), false)
  assert.equal(describeDesignRuntimeError({ message: 'Script error.', error }).message, details.message)
  assert.equal(describeDesignRuntimeError({ message: 'Uncaught TypeError: Cannot read properties of undefined', error: { name: 'TypeError', message: 'Cannot read properties of undefined' } }).message, 'TypeError: Cannot read properties of undefined')
  const provided = describeDesignRuntimeError({ message: 'Detailed original failure', error, filename: 'src/specific.js', line: 12, column: 7 }, { baseUrl })
  assert.equal(provided.message, 'SecurityError: Detailed original failure')
  assert.equal(provided.filename, 'frontend/src/specific.js')
  assert.equal(provided.line, 12)
  assert.equal(provided.column, 7)
})

test('runtime locations use only the first bounded resource frame and unreadable exceptions remain honest', () => {
  const error = { name: 'TypeError', message: 'Cannot read undefined', stack: 'TypeError: Cannot read undefined\n    at src/app.js:7:12\n    at src/other.js:8:13' }
  assert.deepEqual(describeDesignRuntimeError({ message: 'Uncaught', error }), { message: 'TypeError: Cannot read undefined', filename: 'src/app.js', line: 7, column: 12 })
  assert.deepEqual(describeDesignRuntimeError({ message: 'Uncaught', error, filename: 'src/app.js', line: 0, column: 0 }), { message: 'TypeError: Cannot read undefined', filename: 'src/app.js', line: 7, column: 12 })
  assert.equal(describeDesignRuntimeError({ error: { message: 'Promise rejected', stack: 'Promise rejected\nrender@https://private.invalid/src/widget.js?token=secret:3:9' } }).filename, 'src/widget.js')
  assert.deepEqual(describeDesignRuntimeError({ error: { message: 'Promise rejected', stack: 'render@https://private.invalid/src/widget.js?token=secret:3:9\nother@https://private.invalid/src/later.js:5:7' } }), {
    message: 'Promise rejected', filename: 'src/widget.js', line: 3, column: 9,
  })
  assert.equal(describeDesignRuntimeError({ error: { message: 'Blocked local file', stack: 'Error: Blocked local file\n    at run (file:///C:/Users/private/file.js:1:2)' } }).filename, '')
  const inaccessible = Object.fromEntries(['name', 'message', 'stack'].map(property => [property, '']))
  for (const property of ['name', 'message', 'stack']) Object.defineProperty(inaccessible, property, { get() { throw new Error('SECRET-GETTER') } })
  const unreadable = describeDesignRuntimeError({ message: 'Script error.', error: inaccessible, line: Infinity, column: 1000001 })
  assert.match(unreadable.message, /浏览器未提供详细原因/)
  assert.match(unreadable.message, /跨源/)
  assert.equal(unreadable.filename, '')
  assert.equal(JSON.stringify(unreadable).includes('SECRET-GETTER'), false)
  assert.equal(unreadable.line, undefined)
  assert.equal(unreadable.column, undefined)
  assert.equal(describeDesignRuntimeError({ error: { name: 'Error', message: 'x'.repeat(10000), stack: 'y'.repeat(10000) } }).message.length, 500)
  const beyondBound = describeDesignRuntimeError({ error: { message: 'failure', stack: 'failure\n' + 'x'.repeat(8192) + '\n    at https://private.invalid/late.js:1:2' } })
  assert.equal(beyondBound.filename, '')
})

test('srcdoc injects its base before resources without modifying script text or accepting JS URLs', () => {
  const html = '<!doctype html><html lang="zh"><head><base href="https://old.invalid/"><script>const content = "<base href=\'keep\'>";</script><link rel="stylesheet" href="style.css"></head><body><button>保存</button></body></html>'
  const output = createDesignDocument(html, {
    channel: 'new-channel-17', baseUrl: 'http://127.0.0.1:8765/preview/ticket/?p=a&b=c',
    css: serializeDesignCss([{ selector: '#submit', declarations: { color: 'red' } }]), mode: 'edit',
  })
  assert.equal(output.includes('https://old.invalid'), false)
  assert.equal(output.includes('const content = "<base href=\'keep\'>";'), true)
  assert.ok(output.indexOf('data-code-agent-design="base"') < output.indexOf('href="style.css"'))
  assert.equal(output.includes('?p=a&amp;b=c'), true)
  assert.equal(output.includes('color: red !important;'), true)
  assert.equal(output.includes('sandbox='), false)
  assert.throws(() => createDesignDocument(html, { channel: 'new-channel-17', baseUrl: 'javascript:alert(1)' }), /资源地址/)
  assert.throws(() => createDesignDocument(html, { channel: '' }), /通道/)
  const hostileChannel = createDesignDocument('<p>hello</p>', { channel: '</script><script>evil</script>' })
  assert.equal(hostileChannel.includes('</script><script>evil'), false)
  assert.equal(createDesignDocument('<html><body>hi</body></html>', { channel: 'new-channel-17' }).includes('<head>'), true)
  const inert = '<html><body><template><head><base href="https://inert.invalid/"></head></template><textarea>literal <head></textarea><svg><head></head></svg></body></html>'
  const inertOutput = createDesignDocument(inert, { channel: 'new-channel-17' })
  assert.ok(inertOutput.indexOf('data-code-agent-design="bridge"') < inertOutput.indexOf('<body>'))
  assert.ok(inertOutput.includes('<template><head><base href="https://inert.invalid/">'))
})

function bridgeFixture(mode = 'edit', makeDocument = createDesignDocument) {
  const received = []
  const consoleErrors = []
  const windowListeners = new Map()
  const documentListeners = new Map()
  const frames = []
  const observers = []
  const delayed = new Map()
  const metrics = { rectReads: 0, fragmentReads: 0, computedReads: 0, descendantScans: 0, selectorQueries: 0, styleWrites: 0, textWrites: 0, computedTargets: [] }
  function resetMetrics() { for (const key of Object.keys(metrics)) metrics[key] = Array.isArray(metrics[key]) ? [] : 0 }
  let timerSequence = 0
  function listen(registry, type, listener) {
    if (!registry.has(type)) registry.set(type, new Set())
    registry.get(type).add(listener)
  }
  class Element {
    constructor(tag, id = '') {
      this.nodeType = 1; this.localName = tag; this.id = id
      this.children = []; this.parentElement = null; this.isConnected = true
      this.attributes = new Map(); this._textContent = '保存'; this.values = { color: 'rgb(51, 65, 85)', 'font-size': '16px' }
      this.inline = new Map(); this.priorities = new Map(); this.controlled = {}; this.captured = new Set()
      this.style = { setProperty: (name, value, priority = '') => { metrics.styleWrites++; this.inline.set(name, value); this.priorities.set(name, priority) }, getPropertyValue: name => this.inline.get(name) || '', getPropertyPriority: name => this.priorities.get(name) || '', removeProperty: name => { metrics.styleWrites++; this.inline.delete(name); this.priorities.delete(name) } }
    }
    get textContent() { return this._textContent }
    set textContent(value) { metrics.textWrites++; this._textContent = value }
    appendChild(child) {
      if (child.parentElement) child.parentElement.children = child.parentElement.children.filter(existing => existing !== child)
      child.parentElement = this; this.children.push(child); return child
    }
    get lastElementChild() { return this.children.at(-1) || null }
    setAttribute(name, value) { this.attributes.set(name, value) }
    getAttribute(name) { return name === 'style' ? Array.from(this.inline).map(([key, value]) => `${key}:${value}!${this.priorities.get(key) || ''}`).join(';') || null : this.attributes.has(name) ? this.attributes.get(name) : null }
    get href() { return this.attributes.get('href') || '' }
    closest(selector) {
      for (let current = this; current; current = current.parentElement) {
        if (selector === '[data-code-agent-design]' && current.attributes.has('data-code-agent-design')) return current
        if (selector === 'a[href]' && current.localName === 'a' && current.attributes.has('href')) return current
      }
      return null
    }
    computed(property) { return this.priorities.get(property) === 'important' ? this.inline.get(property) : this.controlled[property] || this.inline.get(property) || this.values[property] || '' }
    getBoundingClientRect() { metrics.rectReads++; return this.measure ? this.measure() : { ...rectangle } }
    getClientRects() { metrics.fragmentReads++; return this.fragments || [this.measure ? this.measure() : { ...rectangle }] }
    getElementsByTagName() { metrics.descendantScans++; const result = []; const walk = node => { for (const child of node.children) { result.push(child); walk(child) } }; walk(this); return result }
    setPointerCapture(id) { this.captured.add(id) }
    hasPointerCapture(id) { return this.captured.has(id) }
    releasePointerCapture(id) { this.captured.delete(id) }
    scrollIntoView(options) { this.scrolled = options }
    remove() {
      if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this)
      this.isConnected = false; this.parentElement = null
    }
  }
  const root = new Element('html'), head = root.appendChild(new Element('head')), body = root.appendChild(new Element('body'))
  const style = head.appendChild(new Element('style', 'code-agent-design-style'))
  style.setAttribute('data-code-agent-design', 'style')
  const section1 = body.appendChild(new Element('section')), section2 = body.appendChild(new Element('section'))
  const first = section1.appendChild(new Element('button', 'duplicate')), second = section2.appendChild(new Element('button', 'duplicate'))
  const escaped = section2.appendChild(new Element('button', 'save:primary'))
  const all = () => { const result = []; const visit = current => { result.push(current); current.children.forEach(visit) }; visit(root); return result }
  function componentMatches(candidate, selector) {
    if (selector.startsWith('#')) {
      const id = selector.slice(1).replace(/\\([0-9a-f]{1,6}) ?|\\(.)/gi, (_, code, character) => code ? String.fromCodePoint(parseInt(code, 16)) : character)
      return candidate.id === id
    }
    const match = /^([a-z][a-z-]*):nth-of-type\((\d+)\)$/.exec(selector)
    return Boolean(match && candidate.localName === match[1] && (candidate.parentElement ? candidate.parentElement.children.filter(child => child.localName === candidate.localName).indexOf(candidate) + 1 : 1) === Number(match[2]))
  }
  function querySelectorAll(selector) {
    metrics.selectorQueries++
    const components = selector.split(' > ')
    return all().filter(candidate => {
      let current = candidate
      for (let index = components.length - 1; index >= 0; index--) {
        if (!current || !componentMatches(current, components[index])) return false
        current = current.parentElement
      }
      return true
    })
  }
  const document = {
    readyState: 'complete', body, head, documentElement: root, baseURI: 'http://127.0.0.1:5298/api/v1/code/frontend-design-assets/unit-ticket/static/',
    getElementById: id => all().find(candidate => candidate.id === id),
    createElement: tag => new Element(tag), querySelectorAll, querySelector: selector => querySelectorAll(selector)[0] || null,
    addEventListener: (type, listener) => listen(documentListeners, type, listener),
    removeEventListener: (type, listener) => documentListeners.get(type)?.delete(listener),
  }
  const parent = { postMessage: (value, origin) => received.push({ value: structuredClone(value), origin }) }
  const window = {
    parent, location: { hash: '' }, addEventListener: (type, listener) => listen(windowListeners, type, listener),
    URL, structuredClone, Event, DOMException,
    console: { error: (...args) => consoleErrors.push(args.length) },
    setTimeout: (callback, delay = 0) => { const id = ++timerSequence; if (delay >= 1000) delayed.set(id, callback); else frames.push(callback); return id },
    clearTimeout: id => delayed.delete(id),
    dispatchEvent: event => { for (const listener of windowListeners.get(event.type) || []) listener(event); return true },
    getComputedStyle: target => { metrics.computedReads++; metrics.computedTargets.push(target); return { getPropertyValue: property => target.computed(property) } },
    requestAnimationFrame: callback => frames.push(callback),
  }
  const html = makeDocument('<html><head></head><body></body></html>', { channel: 'preview-unit-17', mode, baseUrl: document.baseURI, resourceRootUrl: '' })
  const source = /<script data-code-agent-design="bridge">([\s\S]*?)<\/script>/.exec(html)[1]
  vm.runInNewContext(source, {
    document, window, URL, TextEncoder, TextDecoder, btoa, atob,
    MutationObserver: class { constructor(callback) { this.callback = callback; observers.push(this) } observe() {} },
  })
  function message(data, source = parent) { for (const listener of windowListeners.get('message') || []) listener({ source, data }) }
  function event(type, target = second, extra = {}) {
    const value = { type, target, prevented: false, defaultPrevented: false, stopped: false, preventDefault() { this.prevented = true; this.defaultPrevented = true }, stopImmediatePropagation() { this.stopped = true }, ...extra }
    for (const listener of documentListeners.get(type) || []) listener(value)
    return value
  }
  function flush() { let limit = 30; while (frames.length && --limit) frames.shift()(); assert.ok(limit > 0) }
  function windowEvent(type, data = {}) { for (const listener of windowListeners.get(type) || []) listener({ type, target: window, ...data }) }
  return { received, message, event, windowEvent, flush, body, head, style, first, second, escaped, observers, window, consoleErrors, Element, delayed, metrics, resetMetrics, channel: 'preview-unit-17' }
}

function geometryFixture(options = {}, makeDocument = createDesignDocument) {
  const fixture = bridgeFixture('edit', makeDocument), target = fixture.second
  const base = { left: 100, top: 80, width: 100, height: 60, ...options }
  const extraX = options.contentBox ? 24 : 0, extraY = options.contentBox ? 16 : 0
  Object.assign(target.values, { display: 'block', 'box-sizing': options.contentBox ? 'content-box' : 'border-box', translate: 'none', width: `${base.width - extraX}px`, height: `${base.height - extraY}px`, ...options.styles })
  if (options.contentBox) Object.assign(target.values, { 'padding-left': '10px', 'padding-right': '10px', 'border-left-width': '2px', 'border-right-width': '2px', 'padding-top': '6px', 'padding-bottom': '6px', 'border-top-width': '2px', 'border-bottom-width': '2px' })
  target.measure = () => {
    const width = Math.max(base.minWidth || 1, Math.min(base.maxWidth || Infinity, parseFloat(target.computed('width')) + extraX))
    const height = Math.max(base.minHeight || 1, Math.min(base.maxHeight || Infinity, parseFloat(target.computed('height')) + extraY))
    const translate = target.computed('translate'), parts = !translate || translate === 'none' ? [0, 0] : translate.split(/\s+/).map(parseFloat)
    const left = base.left + (base.centerX ? (base.width - width) / 2 : 0) + parts[0]
    const top = base.top + (base.centerY ? (base.height - height) / 2 : 0) + (parts[1] || 0)
    return { x: left, y: top, left, top, width, height, right: left + width, bottom: top + height }
  }
  const pointer = (type, x, y, targetNode = target, more = {}) => fixture.event(type, targetNode, { button: 0, pointerId: 7, clientX: x, clientY: y, ...more })
  const transforms = () => fixture.received.filter(item => item.value.type === 'transform').map(item => item.value)
  const handle = direction => fixture.body.getElementsByTagName('*').find(node => node.getAttribute('data-code-agent-handle') === direction)
  const acknowledge = (accepted = true) => {
    const commit = transforms().findLast(message => message.phase === 'commit')
    if (accepted) Object.assign(target.controlled, commit.declarations)
    fixture.message({ channel: fixture.channel, type: 'styles', rules: accepted ? [{ selector: commit.selector, declarations: commit.declarations }] : [] })
    fixture.message({ channel: fixture.channel, type: 'transform-result', gestureId: commit.gestureId, accepted })
    fixture.flush()
  }
  fixture.flush()
  return { ...fixture, target, pointer, transforms, handle, acknowledge }
}

test('production-minified parent helper keeps the serialized frame environment, CSS and diagnostics functional', async () => {
  const { build } = await import('esbuild')
  const bundled = await build({
    entryPoints: [fileURLToPath(new URL('../src/lib/frontendDesign.js', import.meta.url))],
    bundle: true, minify: true, format: 'esm', target: 'es2022', write: false, logLevel: 'silent',
  })
  const compiled = await import(`data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString('base64')}`)
  const fixture = bridgeFixture('edit', compiled.createDesignDocument)
  assert.equal(fixture.received[0].value.type, 'ready')
  fixture.window.localStorage.setItem('frame-only', 'ready')
  assert.equal(fixture.window.localStorage.getItem('frame-only'), 'ready')
  fixture.event('click')
  const selector = fixture.received.at(-1).value.element.selector
  fixture.message({ channel: fixture.channel, type: 'get-layers' })
  assert.equal(fixture.received.at(-1).value.type, 'layers')
  assert.ok(fixture.received.at(-1).value.nodes.length)
  fixture.message({ channel: fixture.channel, type: 'styles', rules: [{ selector, declarations: { color: '#123456', position: 'fixed' } }] })
  assert.match(fixture.style.textContent, /color: #123456 !important/)
  assert.equal(fixture.style.textContent.includes('position'), false)
  fixture.windowEvent('vite:preloadError', { payload: new TypeError('Unable to load /api/v1/code/frontend-design-assets/unit-ticket/static/assets/route.js?token=secret') })
  assert.match(fixture.received.at(-1).value.message, /TypeError: Unable to load static\/assets\/route.js/)
  for (const privateValue of ['unit-ticket', 'token=secret']) assert.equal(fixture.received.at(-1).value.message.includes(privateValue), false)
  const geometry = geometryFixture({}, compiled.createDesignDocument)
  geometry.pointer('pointerdown', 100, 80); geometry.pointer('pointerup', 120, 90)
  assert.deepEqual(geometry.transforms().at(-1).declarations, { translate: '20px 10px' }, 'production minification preserves the serialized geometry dependencies')
  geometry.acknowledge()
})

test('handled router and Vite lazy loader exceptions are visible without serializing console values', () => {
  const fixture = bridgeFixture('acceptance')
  const error = new TypeError('Unable to preload CSS at /api/v1/code/frontend-design-assets/unit-ticket/static/assets/route.css?token=secret')
  fixture.windowEvent('vite:preloadError', { payload: error })
  assert.match(fixture.received.at(-1).value.message, /TypeError: Unable to preload CSS at static\/assets\/route.css/)
  fixture.window.console.error('caught route failure', error, { credential: 'hidden' })
  assert.equal(fixture.consoleErrors.at(-1), 3)
  assert.equal(fixture.received.at(-1).value.message.includes('unit-ticket'), false)
  assert.equal(JSON.stringify(fixture.received).includes('hidden'), false)
  const count = fixture.received.length
  fixture.window.console.error('ordinary diagnostic', { credential: 'hidden' })
  assert.equal(fixture.received.length, count)
})

test('injected bridge reports each failed resource and separate scrubbed runtime errors', () => {
  const fixture = bridgeFixture('acceptance')
  const script = fixture.body.appendChild(new fixture.Element('script'))
  script.setAttribute('src', 'http://127.0.0.1:5298/api/v1/code/frontend-design-assets/unit-ticket/static/src/main.js?token=secret#secret-hash')
  fixture.windowEvent('error', { target: script })
  assert.deepEqual(fixture.received.at(-1).value, {
    channel: fixture.channel, type: 'error', error_kind: 'resource', resource_type: 'script', resource_path: 'static/src/main.js', message: '页面资源加载失败（脚本）：static/src/main.js。',
  })
  const stylesheet = fixture.head.appendChild(new fixture.Element('link'))
  stylesheet.setAttribute('href', './theme.css?key=secret')
  fixture.windowEvent('error', { target: stylesheet })
  assert.equal(fixture.received.at(-1).value.resource_type, 'stylesheet')
  assert.equal(fixture.received.at(-1).value.resource_path, 'static/theme.css')
  const image = fixture.body.appendChild(new fixture.Element('img'))
  image.currentSrc = 'https://user:password@private.invalid/images/logo.png?key=secret'
  fixture.windowEvent('error', { target: image })
  assert.equal(fixture.received.at(-1).value.resource_type, 'image')
  assert.equal(fixture.received.at(-1).value.resource_path, 'images/logo.png')
  const modulePreload = fixture.head.appendChild(new fixture.Element('link'))
  modulePreload.setAttribute('rel', 'modulepreload')
  modulePreload.setAttribute('href', './assets/lazy.js?token=secret')
  fixture.windowEvent('error', { target: modulePreload })
  assert.equal(fixture.received.at(-1).value.resource_type, 'script')
  assert.equal(fixture.received.at(-1).value.resource_path, 'static/assets/lazy.js')
  assert.match(fixture.received.at(-1).value.message, /脚本/)
  const scriptPreload = fixture.head.appendChild(new fixture.Element('link'))
  scriptPreload.setAttribute('rel', 'preload')
  scriptPreload.setAttribute('as', 'script')
  scriptPreload.setAttribute('href', './assets/boot.js?token=secret')
  fixture.windowEvent('error', { target: scriptPreload })
  assert.equal(fixture.received.at(-1).value.resource_type, 'script')
  assert.equal(fixture.received.filter(item => item.value.error_kind === 'resource').length, 5)
  const address = script.getAttribute('src')
  fixture.windowEvent('error', { message: `Failed to import ${address}`, filename: address, lineno: 17, colno: 9 })
  assert.deepEqual(fixture.received.at(-1).value, {
    channel: fixture.channel, type: 'error', error_kind: 'runtime', message: 'Failed to import static/src/main.js', filename: 'static/src/main.js', line: 17, column: 9,
  })
  fixture.windowEvent('unhandledrejection', { reason: { message: `Failed to fetch ${address} unit-ticket` } })
  assert.equal(fixture.received.at(-1).value.error_kind, 'runtime')
  const diagnostics = JSON.stringify(fixture.received.filter(item => item.value.type === 'error'))
  for (const secret of ['unit-ticket', 'secret', 'user:password', 'private.invalid', '127.0.0.1', 'frontend-design-assets']) assert.equal(diagnostics.includes(secret), false)
  const anchor = fixture.body.appendChild(new fixture.Element('a'))
  anchor.setAttribute('href', 'https://user:password@private.invalid/page.html?key=secret')
  fixture.event('click', anchor)
  assert.equal(fixture.received.at(-1).value.error_kind, 'navigation')
  assert.equal(fixture.received.at(-1).value.resource_path, 'page.html')
})

test('serialized iframe bridge preserves DOMException causes and promise rejection source positions', () => {
  const fixture = bridgeFixture('acceptance')
  const address = 'http://127.0.0.1:5298/api/v1/code/frontend-design-assets/unit-ticket/static/assets/index.js?token=secret'
  const error = new DOMException('The operation is insecure.', 'SecurityError')
  Object.defineProperty(error, 'stack', { value: `SecurityError: HIDDEN-RAW-STACK\n    at mount (${address}:1:554)` })
  fixture.windowEvent('error', { message: 'Uncaught', error })
  assert.deepEqual(fixture.received.at(-1).value, {
    channel: fixture.channel, type: 'error', error_kind: 'runtime', message: 'SecurityError: The operation is insecure.', filename: 'static/assets/index.js', line: 1, column: 554,
  })
  fixture.windowEvent('unhandledrejection', { reason: { name: 'TypeError', message: 'Cannot read properties of undefined', stack: `TypeError: HIDDEN-RAW-STACK\n    at render (${address}:4:17)` } })
  assert.deepEqual(fixture.received.at(-1).value, {
    channel: fixture.channel, type: 'error', error_kind: 'runtime', message: 'TypeError: Cannot read properties of undefined', filename: 'static/assets/index.js', line: 4, column: 17,
  })
  fixture.windowEvent('error', { message: 'Script error.' })
  assert.match(fixture.received.at(-1).value.message, /浏览器未提供详细原因/)
  for (const secret of ['HIDDEN-RAW-STACK', 'unit-ticket', 'secret', '127.0.0.1', 'frontend-design-assets']) assert.equal(JSON.stringify(fixture.received).includes(secret), false)
})

test('editable iframe picks unique IDs or structural ancestry and never picks injected DOM', () => {
  const fixture = bridgeFixture()
  assert.equal(fixture.received[0].value.type, 'ready')
  const click = fixture.event('click')
  assert.equal(click.prevented, true)
  assert.equal(click.stopped, true)
  let picked = fixture.received.at(-1).value
  assert.equal(picked.type, 'selected')
  assert.equal(picked.element.selector, 'section:nth-of-type(2) > button:nth-of-type(1)')
  assert.equal(picked.element.tag, 'button')
  const overlay = fixture.body.children.at(-1)
  assert.equal(overlay.attributes.get('data-code-agent-design'), 'selection')
  const before = fixture.received.length
  fixture.event('click', overlay)
  assert.equal(fixture.received.length, before)
  fixture.event('click', fixture.escaped)
  picked = fixture.received.at(-1).value
  assert.equal(picked.element.selector, '#save\\3a primary')
  assert.equal(picked.element.text, '保存')
  fixture.second.textContent = 'x'.repeat(1000)
  fixture.event('click')
  assert.equal(fixture.received.at(-1).value.element.text.length, 500)
})

test('layers expose bounded designable hierarchy and tree inspection scrolls with real breadcrumbs', () => {
  const fixture = bridgeFixture()
  fixture.first.hidden = true
  const excluded = fixture.body.appendChild(new fixture.Element('script'))
  excluded.appendChild(new fixture.Element('div', 'in-script'))
  fixture.flush()
  const layers = fixture.received.findLast(item => item.value.type === 'layers').value
  assert.equal(layers.truncated, false)
  assert.equal(layers.nodes.length, 6)
  assert.equal(layers.nodes[0].tag, 'body')
  assert.equal(layers.nodes[0].parentSelector, '')
  assert.equal(layers.nodes[0].childCount, 2)
  const first = layers.nodes.find(node => node.selector === 'section:nth-of-type(1) > button:nth-of-type(1)')
  assert.equal(first.hidden, true)
  assert.equal(first.parentSelector, 'section:nth-of-type(1)')
  assert.equal(layers.nodes.some(node => ['script', 'style', 'head'].includes(node.tag) || node.selector === '#in-script'), false)
  const row = layers.nodes.find(node => node.selector === '#save\\3a primary')
  fixture.message({ channel: 'wrong-channel', type: 'inspect', selector: row.selector, scrollIntoView: true })
  assert.equal(fixture.escaped.scrolled, undefined)
  fixture.message({ channel: fixture.channel, type: 'inspect', selector: row.selector, scrollIntoView: true })
  const picked = fixture.received.at(-1).value.element
  assert.deepEqual(structuredClone(fixture.escaped.scrolled), { block: 'nearest', inline: 'nearest', behavior: 'instant' })
  assert.deepEqual(picked.ancestors.map(item => item.tag), ['body', 'section'])
  fixture.message({ channel: fixture.channel, type: 'inspect', selector: picked.ancestors.at(-1).selector })
  assert.equal(fixture.received.at(-1).value.element.tag, 'section')
  fixture.message({ channel: fixture.channel, type: 'inspect', selector: 'head:nth-of-type(1)' })
  assert.equal(fixture.received.at(-1).value.element, null)
  fixture.message({ channel: fixture.channel, type: 'mode', mode: 'acceptance' })
  const before = fixture.received.length
  fixture.message({ channel: fixture.channel, type: 'get-layers' })
  fixture.flush()
  assert.equal(fixture.received.length, before)
})

test('layer snapshots cap wide documents and debounce external changes without overlay loops', () => {
  const fixture = bridgeFixture()
  fixture.flush()
  const count = () => fixture.received.filter(item => item.value.type === 'layers').length
  const initial = count()
  fixture.event('click')
  const overlay = fixture.body.children.at(-1)
  fixture.observers[0].callback([{ type: 'childList', target: fixture.body, addedNodes: [overlay], removedNodes: [] }])
  fixture.observers[0].callback([{ type: 'attributes', target: overlay }])
  fixture.flush()
  assert.equal(count(), initial)
  const added = fixture.body.appendChild(new fixture.Element('article', 'new-layer'))
  for (let index = 0; index < 10; index++) fixture.observers[0].callback([{ type: 'childList', target: fixture.body, addedNodes: [added], removedNodes: [] }])
  fixture.flush()
  assert.equal(count(), initial + 1)
  fixture.observers[0].callback([{ type: 'attributes', target: added }])
  fixture.flush()
  assert.equal(count(), initial + 1)
  fixture.first.values.display = 'none'
  fixture.observers[0].callback([{ type: 'attributes', target: fixture.first }])
  fixture.flush()
  assert.equal(count(), initial + 2)
  for (let index = 0; index < MAX_LAYERS + 20; index++) fixture.body.appendChild(new fixture.Element('div', `layer-${index}`))
  fixture.message({ channel: fixture.channel, type: 'get-layers' })
  const layers = fixture.received.at(-1).value
  assert.equal(layers.nodes.length, MAX_LAYERS)
  assert.equal(layers.truncated, true)
  assert.ok(normalizeLayers(layers.nodes))
  assert.equal(layers.nodes.some(node => node.selector === '#code-agent-design-style'), false)
})

test('iframe shortcuts forward selected edit commands and release space pan without hijacking inputs', () => {
  const fixture = bridgeFixture()
  const actions = () => fixture.received.filter(item => item.value.type === 'shortcut').map(item => item.value.action)
  fixture.event('keydown', fixture.second, { key: 'z', ctrlKey: true })
  assert.deepEqual(actions(), [])
  fixture.event('click')
  for (const extra of [{ key: 'z', ctrlKey: true }, { key: 'Z', metaKey: true, shiftKey: true }, { key: 'y', ctrlKey: true }]) {
    assert.equal(fixture.event('keydown', fixture.second, extra).prevented, true)
  }
  assert.deepEqual(actions(), ['undo', 'redo', 'redo'])
  for (const tag of ['input', 'textarea', 'select']) {
    const input = fixture.body.appendChild(new fixture.Element(tag))
    for (const extra of [{ key: 'z', ctrlKey: true }, { key: ' ', code: 'Space' }, { key: 'a' }]) assert.equal(fixture.event('keydown', input, extra).prevented, false)
  }
  const editable = fixture.body.appendChild(new fixture.Element('div'))
  editable.setAttribute('contenteditable', '')
  const nested = editable.appendChild(new fixture.Element('span'))
  assert.equal(fixture.event('keydown', nested, { key: 'z', ctrlKey: true }).prevented, false)
  assert.equal(fixture.event('keydown', fixture.second, { key: ' ', code: 'Space' }).prevented, true)
  fixture.event('keydown', fixture.second, { key: ' ', code: 'Space', repeat: true })
  fixture.event('keyup', fixture.second, { key: ' ', code: 'Space' })
  assert.deepEqual(actions().slice(-2), ['pan-start', 'pan-end'])
  fixture.event('keydown', fixture.second, { key: ' ', code: 'Space' })
  fixture.windowEvent('blur')
  fixture.windowEvent('blur')
  assert.deepEqual(actions().slice(-2), ['pan-start', 'pan-end'])
  fixture.event('keydown', fixture.second, { key: 'Escape' })
  assert.equal(actions().at(-1), 'clear')
  assert.equal(fixture.received.at(-1).value.element, null)
  fixture.event('keydown', fixture.second, { key: ' ', code: 'Space' })
  fixture.message({ channel: fixture.channel, type: 'mode', mode: 'acceptance' })
  assert.equal(actions().at(-1), 'pan-end')
  const before = actions().length
  assert.equal(fixture.event('keydown', fixture.second, { key: ' ', code: 'Space' }).prevented, false)
  assert.equal(fixture.event('keydown', fixture.second, { key: 'z', ctrlKey: true }).prevented, false)
  assert.equal(actions().length, before)
})

test('canvas wheel forwards only bounded modifier-wheel zoom in edit mode and preserves normal scrolling', () => {
  const fixture = bridgeFixture()
  const wheelMessages = () => fixture.received.filter(item => item.value.type === 'canvas-wheel')
  const options = { iframeWindow: fixture.window, channel: fixture.channel }
  const wheel = { deltaY: -120, clientX: 240.5, clientY: 160 }
  for (const modifier of [{ ctrlKey: true }, { metaKey: true }]) {
    const event = fixture.event('wheel', fixture.second, { ...wheel, ...modifier })
    assert.equal(event.prevented, true)
    assert.equal(event.stopped, true)
    const message = wheelMessages().at(-1).value
    assert.deepEqual(message, { channel: fixture.channel, type: 'canvas-wheel', deltaMode: 0, ...wheel })
    assert.deepEqual(validateBridgeMessage({ source: fixture.window, data: message }, options), message)
    assert.equal(validateBridgeMessage({ source: {}, data: message }, options), null)
  }
  assert.equal(fixture.event('wheel', fixture.second, wheel).prevented, false)
  for (const invalid of [{ deltaY: 0 }, { deltaY: Infinity }, { deltaY: 10001 }, { deltaY: '120' }, { deltaMode: -1 }, { deltaMode: 3 }, { deltaMode: '1' }, { clientX: NaN }, { clientX: -1 }, { clientY: 100001 }]) {
    const data = { ...wheel, ...invalid }
    assert.equal(fixture.event('wheel', fixture.second, { ...data, ctrlKey: true }).prevented, false)
    assert.equal(validateBridgeMessage({ source: fixture.window, data: { ...data, type: 'canvas-wheel', channel: fixture.channel } }, options), null)
  }
  assert.equal(wheelMessages().length, 2)
  for (const deltaMode of [1, 2]) {
    fixture.event('wheel', fixture.second, { ...wheel, deltaMode, ctrlKey: true })
    const message = wheelMessages().at(-1).value
    assert.equal(message.deltaMode, deltaMode)
    assert.deepEqual(validateBridgeMessage({ source: fixture.window, data: message }, options), message)
  }
  fixture.message({ channel: fixture.channel, type: 'mode', mode: 'acceptance' })
  assert.equal(fixture.event('wheel', fixture.second, { ...wheel, ctrlKey: true }).prevented, false)
  assert.equal(wheelMessages().length, 4)
})

test('full selection samples geometry once and unchanged overlays perform no redundant writes', () => {
  const fixture = bridgeFixture()
  fixture.flush()
  fixture.second.values.display = 'block'
  const child = fixture.second.appendChild(new fixture.Element('span'))
  fixture.resetMetrics()
  fixture.event('click')
  assert.equal(fixture.metrics.rectReads, 1)
  assert.equal(fixture.metrics.fragmentReads, 1)
  assert.equal(fixture.metrics.descendantScans, 1)
  assert.equal(fixture.metrics.computedTargets.filter(target => target === fixture.second).length, 1)
  assert.equal(fixture.metrics.computedTargets.filter(target => target === child).length, 1)
  const selected = fixture.received.at(-1).value.element
  assert.equal(selected.geometry.enabled, true)
  assert.equal(fixture.body.getElementsByTagName('*').filter(node => node.getAttribute('data-code-agent-handle')).length, 9)
  fixture.resetMetrics()
  fixture.event('click')
  assert.equal(fixture.metrics.rectReads, 1)
  assert.equal(fixture.metrics.styleWrites, 0)
  assert.equal(fixture.metrics.textWrites, 0)
  assert.deepEqual(fixture.received.at(-1).value.element, selected)
})

test('hand tool hides the inactive badge while retaining selection and restores the badge and geometry hints on select tool', () => {
  const fixture = geometryFixture()
  fixture.event('click')
  const overlay = fixture.body.getElementsByTagName('*').find(node => node.getAttribute('data-code-agent-design') === 'selection')
  const badge = overlay.children.find(node => node.getAttribute('data-code-agent-design') === 'geometry-label')
  assert.equal(badge.style.getPropertyValue('display'), 'block')
  fixture.event('keydown', fixture.target, { key: 'h', code: 'KeyH' })
  assert.equal(fixture.received.at(-1).value.action, 'pan-tool')
  fixture.message({ channel: fixture.channel, type: 'interaction', enabled: false, panEnabled: true }); fixture.flush()
  assert.equal(badge.style.getPropertyValue('display'), 'none', 'the inactive-tool notice cannot cover the page')
  assert.equal(overlay.isConnected, true)
  assert.equal(fixture.handle('se'), undefined)
  fixture.event('keydown', fixture.target, { key: 'v', code: 'KeyV' })
  assert.equal(fixture.received.at(-1).value.action, 'select-tool')
  fixture.message({ channel: fixture.channel, type: 'interaction', enabled: true, panEnabled: true }); fixture.flush()
  assert.equal(badge.style.getPropertyValue('display'), 'block')
  assert.match(badge.textContent, /100 × 60/)
  assert.ok(fixture.handle('se'))
  fixture.target.values.translate = '10% 0px'
  fixture.observers[0].callback([{ type: 'attributes', attributeName: 'class', target: fixture.target }]); fixture.flush()
  assert.equal(badge.style.getPropertyValue('display'), 'block', 'unsupported geometry still explains its limitation')
  assert.match(badge.textContent, /百分比/)
})

test('scroll bursts update only selected position with one rect read and retain full revalidation for resize and DOM changes', () => {
  const fixture = bridgeFixture()
  fixture.flush()
  let offset = 0
  fixture.second.measure = () => ({ ...rectangle, y: rectangle.y + offset, top: rectangle.top + offset, bottom: rectangle.bottom + offset })
  fixture.event('click')
  const full = fixture.received.at(-1).value.element
  fixture.resetMetrics()
  const before = fixture.received.length
  offset = -40
  for (let index = 0; index < 20; index++) fixture.windowEvent('scroll')
  fixture.flush()
  const position = fixture.received.at(-1).value
  assert.equal(fixture.received.length, before + 1)
  assert.deepEqual(position, { channel: fixture.channel, type: 'selected-position', selector: full.selector, rect: fixture.second.measure() })
  assert.equal(fixture.metrics.rectReads, 1)
  assert.equal(fixture.metrics.computedReads, 0)
  assert.equal(fixture.metrics.fragmentReads, 0)
  assert.equal(fixture.metrics.descendantScans, 0)
  assert.equal(fixture.metrics.selectorQueries, 0)
  assert.equal(fixture.metrics.styleWrites, 1, 'only the overlay top changes')
  assert.equal(fixture.metrics.textWrites, 0)
  fixture.resetMetrics()
  fixture.windowEvent('scroll'); fixture.flush()
  assert.equal(fixture.metrics.styleWrites, 0)
  assert.equal(fixture.metrics.textWrites, 0)
  fixture.second.values.transform = 'matrix(0, 1, -1, 0, 0, 0)'
  fixture.windowEvent('scroll'); fixture.windowEvent('resize'); fixture.flush()
  assert.equal(fixture.received.at(-1).value.type, 'selected')
  assert.equal(fixture.received.at(-1).value.element.geometry.enabled, false)
  fixture.second.values.transform = 'none'
  fixture.observers[0].callback([{ type: 'attributes', attributeName: 'class', target: fixture.second }]); fixture.flush()
  assert.equal(fixture.received.findLast(item => item.value.type === 'selected').value.element.geometry.enabled, true)
})

test('position envelopes validate selectors, complete bounded rects, channel and frame without carrying style fields', () => {
  const frame = {}, channel = 'position-unit-17'
  const data = { channel, type: 'selected-position', selector: '#submit', rect: rectangle, styles: { color: 'red' } }
  const options = { iframeWindow: frame, channel }
  assert.deepEqual(validateBridgeMessage({ source: frame, data }, options), { channel, type: 'selected-position', selector: '#submit', rect: rectangle })
  for (const invalid of [{ selector: '' }, { selector: '#submit{}' }, { selector: ' #submit' }, { rect: { ...rectangle, x: Infinity } }, { rect: { ...rectangle, width: -1 } }, { rect: { ...rectangle, right: 10000001 } }, { rect: { left: 0 } }]) assert.equal(validateBridgeMessage({ source: frame, data: { ...data, ...invalid } }, options), null)
  assert.equal(validateBridgeMessage({ source: {}, data }, options), null)
  assert.equal(validateBridgeMessage({ source: frame, data: { ...data, channel: 'other-channel' } }, options), null)
})

test('pointer and scroll work share a frame while gesture start still rejects newly unsafe geometry', () => {
  const fixture = geometryFixture()
  fixture.pointer('pointerdown', 100, 80)
  const before = fixture.received.filter(item => item.value.type === 'selected').length
  fixture.resetMetrics()
  for (const [x, y] of [[110, 85], [120, 90], [140, 100]]) fixture.pointer('pointermove', x, y)
  fixture.windowEvent('scroll')
  fixture.flush()
  assert.equal(fixture.target.style.getPropertyValue('translate'), '40px 20px')
  assert.equal(fixture.received.filter(item => item.value.type === 'selected').length, before + 1)
  assert.equal(fixture.metrics.rectReads, 1)
  assert.equal(fixture.metrics.fragmentReads, 0)
  assert.equal(fixture.metrics.computedReads, 1)
  fixture.pointer('pointercancel', 140, 100)
  fixture.target.values.transform = 'matrix(0, 1, -1, 0, 0, 0)'
  fixture.pointer('pointerdown', 100, 80); fixture.pointer('pointermove', 130, 90); fixture.pointer('pointerup', 130, 90); fixture.flush()
  assert.equal(fixture.transforms().filter(message => message.phase === 'commit').length, 0)
})

test('iframe middle pan coalesces bounded coordinates and yields capture to the parent shield', () => {
  const fixture = bridgeFixture()
  fixture.flush()
  const messages = () => fixture.received.filter(item => item.value.type === 'canvas-pan').map(item => item.value)
  const pointer = (type, extra = {}) => fixture.event(type, fixture.second, { button: 1, pointerId: 17, clientX: 240, clientY: 160, ...extra })
  assert.equal(pointer('pointerdown').prevented, true)
  assert.equal(fixture.second.captured.size, 0)
  assert.equal(fixture.received.some(item => item.value.type === 'selected'), false, 'middle pan does not change selection')
  pointer('pointermove', { clientX: 230 }); pointer('pointermove', { clientX: 200 }); pointer('pointermove', { clientX: -10, clientY: 150 })
  fixture.flush()
  assert.deepEqual(messages().map(message => message.phase), ['start', 'move'])
  assert.equal(messages().at(-1).clientX, -10)
  fixture.message({ channel: fixture.channel, type: 'interaction', enabled: false, panEnabled: true, zoom: 100 })
  pointer('pointermove', { clientX: -20 }); pointer('pointerup', { clientX: -30, clientY: 140 }); fixture.flush()
  assert.deepEqual(messages().map(message => message.phase), ['start', 'move', 'end'])
  assert.equal(messages().at(-1).clientX, -30)
  pointer('pointerup')
  assert.equal(messages().length, 3)
  const options = { iframeWindow: fixture.window, channel: fixture.channel }
  for (const data of messages()) assert.deepEqual(validateBridgeMessage({ source: fixture.window, data }, options), data)
  const valid = messages()[0]
  for (const invalid of [{ phase: 'commit' }, { pointerId: -1 }, { pointerId: 2147483648 }, { pointerId: 1.5 }, { pointerId: '17' }, { clientX: Infinity }, { clientX: -100001 }, { clientY: 100001 }]) assert.equal(validateBridgeMessage({ source: fixture.window, data: { ...valid, ...invalid } }, options), null)
  assert.equal(validateBridgeMessage({ source: {}, data: valid }, options), null)
  assert.equal(validateBridgeMessage({ source: fixture.window, data: { ...valid, channel: 'old-channel' } }, options), null)
})

test('window fallback preserves the last middle-pan coordinates when release precedes the scheduled move', () => {
  const fixture = bridgeFixture()
  fixture.flush()
  fixture.event('pointerdown', fixture.second, { button: 1, pointerId: 17, clientX: 240, clientY: 160 })
  const pointer = (type, clientX, clientY, buttons) => fixture.windowEvent(type, { pointerId: 17, clientX, clientY, buttons, preventDefault() {}, stopImmediatePropagation() {} })
  pointer('pointermove', 220, 150, 4)
  pointer('pointerup', 200, 140, 0)
  fixture.flush()
  const messages = fixture.received.filter(item => item.value.type === 'canvas-pan').map(item => item.value)
  assert.deepEqual(messages.map(message => message.phase), ['start', 'end'])
  assert.equal(messages.at(-1).clientX, 200)
  assert.equal(messages.at(-1).clientY, 140)
  assert.equal(messages.at(-1).pointerId, 17)
})

test('middle pan releases on parent handoff, escape, blur, disabled interaction and acceptance without stale move messages', () => {
  for (const finish of ['parent', 'escape', 'blur', 'disabled', 'acceptance', 'pointercancel']) {
    const fixture = bridgeFixture()
    fixture.flush()
    const pointer = (type, extra = {}) => fixture.event(type, fixture.second, { button: 1, pointerId: 17, clientX: 240, clientY: 160, ...extra })
    const messages = () => fixture.received.filter(item => item.value.type === 'canvas-pan').map(item => item.value)
    pointer('pointerdown'); pointer('pointermove', { clientX: 220 })
    if (finish === 'parent') {
      fixture.message({ channel: 'stale-channel', type: 'canvas-pan-cancel', pointerId: 17 })
      fixture.message({ channel: fixture.channel, type: 'canvas-pan-cancel', pointerId: 17 }, {})
      fixture.message({ channel: fixture.channel, type: 'canvas-pan-cancel', pointerId: 18 })
      fixture.flush()
      assert.equal(messages().at(-1).phase, 'move')
      fixture.message({ channel: fixture.channel, type: 'canvas-pan-cancel', pointerId: 17 })
    } else if (finish === 'escape') fixture.event('keydown', fixture.second, { key: 'Escape' })
    else if (finish === 'blur') fixture.windowEvent('blur')
    else if (finish === 'disabled') fixture.message({ channel: fixture.channel, type: 'interaction', enabled: false, panEnabled: false })
    else if (finish === 'acceptance') fixture.message({ channel: fixture.channel, type: 'mode', mode: 'acceptance' })
    else pointer('pointercancel')
    fixture.flush()
    assert.equal(messages().at(-1).phase, finish === 'parent' ? 'move' : 'cancel', finish)
    const count = messages().length
    pointer('pointermove'); pointer('pointerup'); fixture.windowEvent('blur'); fixture.flush()
    assert.equal(messages().length, count, `${finish}: no late move or duplicate end`)
    if (finish === 'acceptance') assert.equal(pointer('pointerdown').prevented, false)
    if (finish === 'disabled') { pointer('pointerdown'); assert.equal(messages().length, count) }
  }
})

test('iframe tool and fit shortcuts use physical shifted digits without triggering in inputs or modifier combinations', () => {
  const fixture = bridgeFixture()
  const actions = () => fixture.received.filter(item => item.value.type === 'shortcut').map(item => item.value.action)
  for (const extra of [{ key: 'v', code: 'KeyV' }, { key: 'h', code: 'KeyH' }, { key: '!', code: 'Digit1', shiftKey: true }]) {
    assert.equal(fixture.event('keydown', fixture.second, extra).prevented, true)
  }
  assert.deepEqual(actions(), ['select-tool', 'pan-tool', 'fit'])
  fixture.event('keydown', fixture.second, { key: '@', code: 'Digit2', shiftKey: true })
  assert.equal(actions().length, 3)
  fixture.event('click')
  fixture.event('keydown', fixture.second, { key: '@', code: 'Digit2', shiftKey: true })
  assert.equal(actions().at(-1), 'focus')
  const before = actions().length
  for (const extra of [{ key: 'v', ctrlKey: true }, { key: 'h', metaKey: true }, { key: 'v', altKey: true }, { key: '!', code: 'Digit1', ctrlKey: true, shiftKey: true }, { key: 'h', isComposing: true }]) {
    fixture.event('keydown', fixture.second, extra)
  }
  const input = fixture.body.appendChild(new fixture.Element('textarea'))
  for (const extra of [{ key: 'v' }, { key: 'h' }, { key: '!', code: 'Digit1', shiftKey: true }, { key: '@', code: 'Digit2', shiftKey: true }]) {
    assert.equal(fixture.event('keydown', input, extra).prevented, false)
  }
  assert.equal(actions().length, before)
  fixture.message({ channel: fixture.channel, type: 'mode', mode: 'acceptance' })
  assert.equal(fixture.event('keydown', fixture.second, { key: 'h' }).prevented, false)
  assert.equal(actions().length, before)
})

test('translate and geometry bridge accept only bounded pixel declarations and matched transform envelopes', () => {
  for (const translate of ['none', '0px', '-12px +5.5px', '.5px -.25px']) assert.equal(normalizeStyleDeclarations({ translate }).translate, translate)
  for (const translate of ['inherit', 'unset', '10%', '1rem', '1px 2px 3px', 'calc(1px + 2px)', 'translate(2px)', '1000001px', 'NaNpx']) assert.deepEqual(normalizeStyleDeclarations({ translate }), {})
  assert.deepEqual(normalizeTransformDeclarations({ translate: '-3px 4px', width: '20.5px' }), { translate: '-3px 4px', width: '20.5px' })
  for (const declarations of [{}, { translate: 'none' }, { width: '0px' }, { width: '-1px' }, { height: '20%' }, { translate: '1px', transform: 'none' }, { translate: ' 1px' }]) assert.equal(normalizeTransformDeclarations(declarations), null)
  const frame = {}, channel = 'transform-unit-17'
  const validate = data => validateBridgeMessage({ source: frame, data: { channel, ...data } }, { iframeWindow: frame, channel })
  const start = { type: 'transform', phase: 'start', gestureId: 'gesture-1', selector: '#save' }
  assert.deepEqual(validate(start), { channel, ...start })
  assert.equal(validate({ ...start, declarations: {} }), null)
  assert.equal(validate({ ...start, gestureId: '<script>' }), null)
  assert.equal(validate({ ...start, phase: 'move' }), null)
  assert.equal(validate({ ...start, phase: 'commit', declarations: { width: '20px', position: 'fixed' } }), null)
  assert.deepEqual(validate({ ...start, phase: 'commit', declarations: { translate: '3px 4px' } }).declarations, { translate: '3px 4px' })
})

test('one pointer press moves in iframe CSS pixels at every outer zoom, commits once and restores exact inline values after ack', () => {
  for (const zoom of [25, 75, 150, 200]) {
    const fixture = geometryFixture()
    fixture.target.style.setProperty('outline', '1px solid red', 'important')
    fixture.message({ channel: fixture.channel, type: 'interaction', enabled: true, zoom })
    fixture.pointer('pointerdown', 110, 90)
    assert.equal(fixture.transforms().length, 0)
    fixture.pointer('pointermove', 112, 91); fixture.flush()
    assert.equal(fixture.transforms().length, 0, 'a click-sized movement has no history transaction')
    fixture.pointer('pointermove', 130, 100); fixture.flush()
    assert.deepEqual(fixture.transforms().map(message => message.phase), ['start'])
    assert.equal(fixture.target.computed('translate'), '20px 10px', 'parent CSS zoom is not divided into inner-frame client deltas')
    assert.equal(fixture.handle('se').style.getPropertyValue('width'), `${9 * 100 / zoom}px`)
    const preview = fixture.received.findLast(item => item.value.type === 'selected').value.element
    assert.equal(preview.rect.left, 120)
    fixture.pointer('pointerup', 130, 100)
    fixture.pointer('pointerup', 130, 100)
    assert.deepEqual(fixture.transforms().map(message => message.phase), ['start', 'commit'])
    assert.deepEqual(fixture.transforms()[1].declarations, { translate: '20px 10px' })
    fixture.message({ channel: fixture.channel, type: 'transform-result', gestureId: 'old-gesture', accepted: true })
    assert.equal(fixture.target.style.getPropertyPriority('translate'), 'important')
    fixture.acknowledge()
    assert.equal(fixture.target.style.getPropertyValue('translate'), '')
    assert.equal(fixture.target.computed('translate'), '20px 10px')
    assert.equal(fixture.target.style.getPropertyValue('outline'), '1px solid red')
    assert.equal(fixture.target.style.getPropertyPriority('outline'), 'important')
    assert.equal(fixture.transforms().filter(message => message.phase === 'cancel').length, 0, 'parent commit styles do not cancel the pending transaction')
  }
})

test('all eight resize handles compensate real centered-layout movement and preserve their opposite edges', () => {
  for (const direction of ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']) {
    const fixture = geometryFixture({ centerX: true, centerY: true })
    fixture.event('click')
    const handle = fixture.handle(direction)
    assert.ok(handle, `visible ${direction} handle`)
    fixture.pointer('pointerdown', 100, 80, handle)
    fixture.pointer('pointermove', 120, 90, handle); fixture.flush()
    const rect = fixture.target.getBoundingClientRect()
    if (direction.includes('w')) assert.equal(rect.right, 200, `${direction} preserves right edge`)
    else assert.equal(rect.left, 100, `${direction} preserves left edge even when layout recenters width`)
    if (direction.includes('n')) assert.equal(rect.bottom, 140, `${direction} preserves bottom edge`)
    else assert.equal(rect.top, 80, `${direction} preserves top edge even when layout recenters height`)
    fixture.pointer('pointerup', 120, 90, handle)
    const commit = fixture.transforms().at(-1)
    assert.equal(commit.phase, 'commit')
    assert.equal(Object.hasOwn(commit.declarations, 'width'), /[ew]/.test(direction))
    assert.equal(Object.hasOwn(commit.declarations, 'height'), /[ns]/.test(direction))
    assert.equal(fixture.body.getElementsByTagName('*').filter(node => node.getAttribute('data-code-agent-handle')).length, 9)
    fixture.acknowledge()
  }
})

test('resize subtracts content-box decoration, commits constrained actual sizes and supports axis lock, ratio and center anchors', () => {
  const content = geometryFixture({ contentBox: true })
  content.event('click'); const se = content.handle('se')
  content.pointer('pointerdown', 100, 80, se); content.pointer('pointerup', 120, 90, se)
  assert.deepEqual(content.transforms().at(-1).declarations, { translate: '0px 0px', width: '96px', height: '54px' })
  const constrained = geometryFixture({ maxWidth: 110, centerX: true })
  constrained.event('click'); const east = constrained.handle('e')
  constrained.pointer('pointerdown', 100, 80, east); constrained.pointer('pointermove', 150, 80, east); constrained.flush()
  assert.equal(constrained.target.getBoundingClientRect().left, 100)
  assert.match(constrained.body.getElementsByTagName('*').find(node => node.getAttribute('data-code-agent-design') === 'geometry-label').textContent, /受布局约束/)
  constrained.pointer('pointerup', 150, 80, east)
  assert.equal(constrained.transforms().at(-1).declarations.width, '110px')
  const ratio = geometryFixture(); ratio.event('click')
  ratio.pointer('pointerdown', 100, 80, ratio.handle('se')); ratio.pointer('pointerup', 120, 110, ratio.handle('se'), { shiftKey: true })
  assert.equal(ratio.transforms().at(-1).declarations.width, '150px')
  assert.equal(ratio.transforms().at(-1).declarations.height, '90px')
  const center = geometryFixture(); center.event('click')
  center.pointer('pointerdown', 100, 80, center.handle('se')); center.pointer('pointerup', 110, 85, center.handle('se'), { altKey: true })
  assert.deepEqual(center.transforms().at(-1).declarations, { translate: '-10px -5px', width: '120px', height: '70px' })
  const axis = geometryFixture(); axis.pointer('pointerdown', 100, 80); axis.pointer('pointerup', 130, 90, axis.target, { shiftKey: true })
  assert.equal(axis.transforms().at(-1).declarations.translate, '30px 0px')
})

test('drag returning to origin cancels without fixed dimensions and every cancellation restores only its temporary properties', () => {
  for (const cancel of ['escape', 'pointercancel', 'lostpointercapture', 'blur', 'mode', 'interaction', 'inspect', 'styles', 'detached', 'selector']) {
    const fixture = geometryFixture()
    fixture.target.style.setProperty('translate', '4px 6px')
    fixture.target.style.setProperty('height', '60px')
    fixture.target.style.setProperty('outline', '2px dotted blue', 'important')
    fixture.pointer('pointerdown', 110, 90); fixture.pointer('pointermove', 130, 105); fixture.flush()
    if (cancel === 'escape') fixture.event('keydown', fixture.target, { key: 'Escape' })
    else if (['pointercancel', 'lostpointercapture'].includes(cancel)) fixture.pointer(cancel, 130, 105)
    else if (cancel === 'blur') fixture.windowEvent('blur')
    else if (cancel === 'mode') fixture.message({ channel: fixture.channel, type: 'mode', mode: 'acceptance' })
    else if (cancel === 'interaction') fixture.message({ channel: fixture.channel, type: 'interaction', enabled: false })
    else if (cancel === 'inspect') fixture.message({ channel: fixture.channel, type: 'inspect', selector: '' })
    else if (cancel === 'styles') fixture.message({ channel: fixture.channel, type: 'styles', rules: [] })
    else if (cancel === 'detached') { fixture.target.remove(); fixture.observers[0].callback([{ target: fixture.body }]) }
    else { fixture.target.id = 'new-identity'; fixture.pointer('pointermove', 140, 105); fixture.flush() }
    assert.deepEqual(fixture.transforms().map(message => message.phase), ['start', 'cancel'], cancel)
    assert.equal(fixture.target.style.getPropertyValue('translate'), '4px 6px', cancel)
    assert.equal(fixture.target.style.getPropertyPriority('translate'), '', cancel)
    assert.equal(fixture.target.style.getPropertyValue('height'), '60px', cancel)
    assert.equal(fixture.target.style.getPropertyValue('outline'), '2px dotted blue', cancel)
    assert.equal(fixture.target.style.getPropertyPriority('outline'), 'important', cancel)
  }
  const fixture = geometryFixture()
  fixture.pointer('pointerdown', 100, 80); fixture.pointer('pointermove', 120, 90); fixture.flush(); fixture.pointer('pointerup', 100, 80)
  assert.deepEqual(fixture.transforms().map(message => message.phase), ['start', 'cancel'])
  assert.equal(fixture.target.style.getPropertyValue('translate'), '')
  fixture.event('click'); const east = fixture.handle('e')
  fixture.pointer('pointerdown', 100, 80, east); fixture.pointer('pointermove', 120, 80, east); fixture.flush(); fixture.pointer('pointerup', 100, 80, east)
  assert.equal(fixture.transforms().filter(message => message.phase === 'commit').length, 0)
  assert.equal(fixture.target.style.getPropertyValue('width'), '')
})

test('pending rejection, missing acknowledgement and undo release temporary state without duplicate commits', () => {
  for (const finish of ['reject', 'timeout', 'blur']) {
    const fixture = geometryFixture()
    fixture.pointer('pointerdown', 100, 80); fixture.pointer('pointerup', 120, 90)
    if (finish === 'reject') fixture.acknowledge(false)
    else if (finish === 'timeout') [...fixture.delayed.values()].forEach(callback => callback())
    else fixture.windowEvent('blur')
    assert.equal(fixture.target.style.getPropertyValue('translate'), '', finish)
    assert.equal(fixture.transforms().filter(message => message.phase === 'commit').length, 1, finish)
    if (finish !== 'reject') assert.equal(fixture.transforms().at(-1).phase, 'cancel')
  }
  const fixture = geometryFixture()
  fixture.pointer('pointerdown', 100, 80); fixture.pointer('pointermove', 120, 90); fixture.flush()
  fixture.event('keydown', fixture.target, { key: 'z', ctrlKey: true })
  const messages = fixture.received.filter(item => ['transform', 'shortcut'].includes(item.value.type)).map(item => item.value)
  assert.equal(messages.at(-2).phase, 'cancel')
  assert.equal(messages.at(-1).action, 'undo')
  const refused = geometryFixture()
  refused.pointer('pointerdown', 100, 80); refused.pointer('pointermove', 120, 90); refused.flush()
  const start = refused.transforms().at(-1)
  refused.message({ channel: refused.channel, type: 'transform-result', gestureId: start.gestureId, accepted: false })
  assert.equal(refused.target.style.getPropertyValue('translate'), '', 'an immediately rejected start also releases its preview')
  refused.pointer('pointerup', 120, 90)
  assert.equal(refused.transforms().filter(message => message.phase === 'commit').length, 0)
})

test('cancellation and acknowledgement flush restored geometry before re-enabling author transitions', () => {
  for (const finish of ['escape', 'accept', 'reject']) {
    const fixture = geometryFixture()
    fixture.target.style.setProperty('transition-property', 'all')
    fixture.target.style.setProperty('transition-duration', '.2s')
    fixture.pointer('pointerdown', 100, 80); fixture.pointer('pointermove', 140, 100); fixture.flush()
    if (finish !== 'escape') fixture.pointer('pointerup', 140, 100)
    const log = [], style = fixture.target.style, set = style.setProperty, remove = style.removeProperty, measure = fixture.target.measure
    style.setProperty = (key, value, priority) => { log.push(['set', key, value, priority]); set(key, value, priority) }
    style.removeProperty = key => { log.push(['remove', key]); remove(key) }
    fixture.target.measure = () => { log.push(['layout', fixture.target.computed('transition-property'), fixture.target.computed('translate')]); return measure() }
    if (finish === 'escape') fixture.event('keydown', fixture.target, { key: 'Escape' })
    else fixture.acknowledge(finish === 'accept')
    const restoreTransition = log.findIndex(entry => entry[0] === 'set' && entry[1] === 'transition-property' && entry[2] === 'all')
    const restoredLayout = log.findIndex(entry => entry[0] === 'layout')
    assert.ok(restoredLayout > 0 && restoreTransition > restoredLayout, `${finish}: restored layout is resolved before author transitions return`)
    assert.equal(log[restoredLayout][1], 'none')
    assert.equal(log[restoredLayout][2], finish === 'accept' ? '40px 20px' : 'none')
    assert.equal(fixture.target.style.getPropertyValue('transition-property'), 'all')
    assert.equal(fixture.target.style.getPropertyPriority('transition-property'), '')
    assert.equal(fixture.target.style.getPropertyValue('transition-duration'), '.2s')
  }
})

test('unsafe geometry stays selectable with Chinese guidance and own gesture styles never refresh the layer tree', () => {
  for (const setup of [
    fixture => { fixture.target.values.translate = '10% 0px' },
    fixture => { fixture.target.values.transform = 'matrix(0, 1, -1, 0, 0, 0)' },
    fixture => { fixture.target.parentElement.values.transform = 'matrix(2, 0, 0, 2, 0, 0)' },
    fixture => { fixture.body.values.transform = 'matrix(1, 0, 0, 1, 5, 0)' },
    fixture => { fixture.target.style.setProperty('width', '100px', 'important') },
    fixture => { fixture.target.values.display = 'inline' },
    fixture => { fixture.target.fragments = [rectangle, rectangle] },
    fixture => { const child = fixture.target.appendChild(new fixture.Element('span')); child.values.position = 'fixed' },
  ]) {
    const fixture = geometryFixture(); setup(fixture); fixture.event('click')
    const selected = fixture.received.findLast(item => item.value.type === 'selected').value.element
    assert.equal(selected.geometry.enabled, false)
    assert.match(selected.geometry.hint, /[\u4e00-\u9fff]/)
    assert.equal(fixture.handle('se'), undefined)
    fixture.pointer('pointerdown', 100, 80); fixture.pointer('pointerup', 120, 90)
    assert.equal(fixture.transforms().length, 0)
  }
  const fixture = geometryFixture()
  const layers = () => fixture.received.filter(item => item.value.type === 'layers').length
  const count = layers()
  fixture.pointer('pointerdown', 100, 80); fixture.pointer('pointermove', 120, 90); fixture.flush()
  fixture.observers[0].callback([{ type: 'attributes', attributeName: 'style', target: fixture.target }])
  fixture.flush()
  assert.equal(layers(), count)
  assert.equal(fixture.transforms().at(-1).phase, 'start')
  fixture.target.style.setProperty('color', 'red')
  fixture.observers[0].callback([{ type: 'attributes', attributeName: 'style', target: fixture.target }])
  assert.equal(fixture.transforms().at(-1).phase, 'cancel', 'external style mutation cancels a gesture')
})

test('acceptance removes interception and selection, stale frames cannot change mode or styles', () => {
  const fixture = bridgeFixture()
  fixture.event('click')
  fixture.message({ channel: fixture.channel, type: 'mode', mode: 'acceptance' }, {})
  assert.equal(fixture.event('submit').prevented, true)
  fixture.message({ channel: 'preview-unit-old', type: 'mode', mode: 'acceptance' })
  assert.equal(fixture.event('submit').prevented, true)
  fixture.message({ channel: fixture.channel, type: 'mode', mode: 'acceptance' })
  assert.equal(fixture.event('click').prevented, false)
  assert.equal(fixture.event('submit').prevented, false)
  assert.equal(fixture.event('keydown').prevented, false)
  assert.equal(fixture.body.children.some(child => child.attributes.has('data-code-agent-design')), false)
  assert.equal(fixture.received.at(-1).value.element, null)
  fixture.message({ channel: fixture.channel, type: 'mode', mode: 'edit' })
  assert.equal(fixture.event('pointerdown').prevented, true)
  fixture.event('click')
  fixture.event('keydown', fixture.second, { key: 'Escape' })
  assert.equal(fixture.received.at(-1).value.element, null)
})

test('styles and explicit inspect honor the channel and sanitize rules; detached selections clear', () => {
  const fixture = bridgeFixture('acceptance')
  assert.equal(fixture.event('click').prevented, false)
  fixture.message({ channel: fixture.channel, type: 'inspect', selector: '#save\\:primary' })
  assert.equal(fixture.received.length, 1)
  fixture.message({ channel: fixture.channel, type: 'mode', mode: 'edit' })
  fixture.message({ channel: fixture.channel, type: 'inspect', selector: '#save\\:primary' })
  assert.equal(fixture.received.at(-1).value.element.selector, '#save\\3a primary')
  const rules = [{ selector: '#save\\:primary', declarations: { color: 'red', position: 'fixed', border: 'url(https://example.invalid)' } }]
  fixture.message({ channel: 'wrong-channel', type: 'styles', rules })
  assert.equal(fixture.style.textContent, '保存')
  fixture.message({ channel: fixture.channel, type: 'styles', rules })
  fixture.flush()
  assert.equal(fixture.style.textContent, '#save\\:primary {\n  color: red !important;\n}')
  fixture.message({ channel: fixture.channel, type: 'styles', css: serializeDesignCss([{ selector: '#save\\:primary', declarations: { color: 'blue' } }]) })
  fixture.flush()
  assert.equal(fixture.style.textContent.includes('blue !important'), true)
  fixture.escaped.remove()
  fixture.observers[0].callback([{ target: fixture.body }])
  fixture.flush()
  assert.equal(fixture.received.at(-1).value.element, null)
})

test('preview style stays after saved and dynamically injected CSS without repeated DOM moves', () => {
  const fixture = bridgeFixture()
  const savedLink = fixture.head.appendChild(new fixture.Element('link'))
  fixture.message({ channel: fixture.channel, type: 'styles', rules: [{ selector: '#save\\:primary', declarations: { color: 'blue' } }] })
  assert.equal(fixture.head.lastElementChild, fixture.style)
  assert.equal(fixture.head.children.filter(child => child === fixture.style).length, 1)
  const frameworkStyle = fixture.head.appendChild(new fixture.Element('style'))
  fixture.observers[0].callback([{ target: fixture.head }])
  assert.equal(fixture.head.lastElementChild, fixture.style)
  const children = fixture.head.children.slice()
  fixture.observers[0].callback([{ target: fixture.head }])
  assert.deepEqual(fixture.head.children, children)
  assert.ok(children.includes(savedLink) && children.includes(frameworkStyle))
})

test('acceptance links retain SPA and hash behavior while same-ticket page navigation reloads through the parent', () => {
  const fixture = bridgeFixture('acceptance')
  const anchor = fixture.body.appendChild(new fixture.Element('a'))
  const href = 'http://127.0.0.1:5298/api/v1/code/frontend-design-assets/unit-ticket/static/detail.html?tab=2'
  anchor.setAttribute('href', href)
  const navigation = fixture.event('click', anchor)
  assert.equal(navigation.prevented, true)
  assert.deepEqual(fixture.received.at(-1).value, { channel: fixture.channel, type: 'navigate', href })
  const before = fixture.received.length
  fixture.event('click', anchor, { defaultPrevented: true })
  assert.equal(fixture.received.length, before)
  anchor.setAttribute('href', '#heading')
  assert.equal(fixture.event('click', anchor).prevented, true)
  assert.equal(fixture.window.location.hash, '#heading')
  assert.equal(fixture.received.length, before)
  anchor.setAttribute('href', 'https://outside.invalid/detail.html')
  assert.equal(fixture.event('click', anchor).prevented, true)
  assert.equal(fixture.received.at(-1).value.type, 'error')
  fixture.message({ channel: 'old-preview-unit', type: 'ping' })
  assert.equal(fixture.received.at(-1).value.type, 'error')
  fixture.message({ channel: fixture.channel, type: 'ping' })
  assert.equal(fixture.received.at(-1).value.type, 'ready')
  assert.deepEqual(validateBridgeMessage({ source: fixture.window, data: { channel: fixture.channel, type: 'navigate', href } }, { iframeWindow: fixture.window, channel: fixture.channel }), { channel: fixture.channel, type: 'navigate', href })
  assert.equal(validateBridgeMessage({ source: fixture.window, data: { channel: fixture.channel, type: 'navigate', href: 'javascript:alert(1)' } }, { iframeWindow: fixture.window, channel: fixture.channel }), null)
})
