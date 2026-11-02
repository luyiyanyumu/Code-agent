// This is a separate writable project preview, not the read-only design iframe.
// It is serialized into srcdoc, so all browser dependencies live in this scope.
export function installIntegrationPreviewEnvironment(win, config) {
  const URLCtor = win.URL, HeadersCtor = win.Headers, RequestCtor = win.Request
  const frontend = new URLCtor(config.frontend_url), backend = new URLCtor(config.backend_url)
  const base = new URLCtor(config.base_url), assets = new URLCtor(config.asset_prefix), proxy = new URLCtor(config.api_proxy_url)
  const apiPrefix = '/' + String(config.api_prefix || '/api').replace(/^\/+|\/+$/g, '')
  const denied = () => new Error('联调预览只允许当前项目页面和接口地址。')
  const supported = method => {
    const value = String(method || 'GET').toUpperCase()
    if (!['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'].includes(value)) throw denied()
    return value
  }
  const mapped = input => {
    const text = String(input)
    if (text.length > 16384 || /[\u0000-\u001f\u007f\\]/.test(text)) throw denied()
    const absolute = /^[a-z][a-z\d+.-]*:/i.test(text) || text.startsWith('//')
    const value = new URLCtor(text, frontend.href)
    if (!['http:', 'https:'].includes(value.protocol) || value.username || value.password) throw denied()
    if (value.origin === proxy.origin && value.pathname.startsWith(proxy.pathname)) return { url: value.href, api: true }
    if (value.origin === assets.origin && value.pathname.startsWith(assets.pathname)) return { url: value.href, api: false }
    let suffix, api = false
    if (absolute && value.origin === backend.origin) {
      const root = backend.pathname.endsWith('/') ? backend.pathname : backend.pathname + '/'
      if (value.pathname !== root.slice(0, -1) && !value.pathname.startsWith(root)) throw denied()
      suffix = '__backend__/' + (value.pathname === root.slice(0, -1) ? '' : value.pathname.slice(root.length))
      api = true
    } else if (value.origin === frontend.origin) {
      api = value.pathname === apiPrefix || value.pathname.startsWith(apiPrefix + '/')
      suffix = value.pathname.replace(/^\/+/, '')
    } else throw denied()
    const result = new URLCtor(suffix + value.search, (api ? proxy : assets).href)
    if (result.origin !== base.origin || !result.pathname.startsWith((api ? proxy : assets).pathname)) throw denied()
    return { url: result.href, api }
  }
  const notify = value => {
    try { win.parent.postMessage({ channel: config.channel, ...value }, '*') } catch { /* Parent may be closing. */ }
  }
  const envelope = (headers, api) => {
    const result = new HeadersCtor()
    if (api) result.set('X-Code-Agent-Project-Headers', encodeURIComponent(JSON.stringify(Object.fromEntries(headers))))
    for (const key of api ? ['content-type'] : ['accept', 'content-type', 'range']) if (headers.has(key)) result.set(key, headers.get(key))
    return result
  }
  const rawFetch = win.fetch
  if (typeof rawFetch === 'function') {
    Object.defineProperty(win, 'fetch', { configurable: true, writable: true, value: async function(input, options = {}) {
      const isRequest = typeof RequestCtor === 'function' && input instanceof RequestCtor
      const target = mapped(isRequest ? input.url : input)
      let draft = new RequestCtor(target.url, isRequest ? input : options)
      if (isRequest) draft = new RequestCtor(draft, options)
      supported(draft.method)
      const final = new RequestCtor(draft, { headers: envelope(new HeadersCtor(draft.headers), target.api), credentials: 'omit', redirect: 'error' })
      const started = Date.now()
      try {
        const response = await rawFetch.call(win, final)
        if (target.api) notify({ type: 'request', id: response.headers?.get('X-Code-Agent-Request-Id') || '', status: response.status, duration_ms: Date.now() - started })
        return response
      } catch (error) {
        if (target.api) notify({ type: 'request', error: String(error?.message || '接口请求失败').slice(0, 500) })
        throw error
      }
    } })
  }
  const xhr = win.XMLHttpRequest?.prototype
  if (xhr?.open && xhr?.send && xhr?.setRequestHeader) {
    const open = xhr.open, send = xhr.send, setHeader = xhr.setRequestHeader, states = new WeakMap()
    const credentials = Object.getOwnPropertyDescriptor(xhr, 'withCredentials')
    if (!credentials?.set || credentials.configurable === false) throw new Error('当前浏览器无法隔离联调请求凭据。')
    Object.defineProperty(xhr, 'withCredentials', { ...credentials, set() { credentials.set.call(this, false) } })
    Object.defineProperty(xhr, 'open', { configurable: true, writable: true, value: function(method, url, async = true, username, password) {
      if (username || password) throw new Error('请通过 Authorization 请求头显式传入项目认证。')
      const target = mapped(url)
      const result = open.call(this, supported(method), target.url, async)
      credentials.set.call(this, false)
      states.set(this, { ...target, headers: new HeadersCtor(), started: 0 })
      return result
    } })
    Object.defineProperty(xhr, 'setRequestHeader', { configurable: true, writable: true, value: function(name, value) {
      const state = states.get(this)
      if (!state) return setHeader.call(this, name, value)
      state.headers.append(name, value)
    } })
    Object.defineProperty(xhr, 'send', { configurable: true, writable: true, value: function(body) {
      const state = states.get(this)
      if (!state) throw new Error('请先打开联调接口请求。')
      for (const [key, value] of envelope(state.headers, state.api)) setHeader.call(this, key, value)
      credentials.set.call(this, false)
      state.started = Date.now()
      if (state.api) this.addEventListener('loadend', () => notify({ type: 'request', status: this.status, duration_ms: Date.now() - state.started }), { once: true })
      return send.call(this, body ?? null)
    } })
  }
  const memoryStorage = () => {
    const values = new Map(), maxBytes = 256 * 1024
    const bytes = text => new TextEncoder().encode(String(text)).length
    const methods = {
      getItem(key) { return values.get(String(key)) ?? null },
      setItem(key, value) {
        key = String(key); value = String(value)
        const total = [...values].reduce((sum, [k, v]) => sum + (k === key ? 0 : bytes(k) + bytes(v)), 0) + bytes(key) + bytes(value)
        if (total > maxBytes || (!values.has(key) && values.size >= 512)) throw new Error('预览临时存储已达到容量限制。')
        values.set(key, value)
      },
      removeItem(key) { values.delete(String(key)) }, clear() { values.clear() },
      key(index) { return [...values.keys()][Math.trunc(Number(index))] ?? null },
      get length() { return values.size },
    }
    return new Proxy(methods, {
      get(target, key) { if (Reflect.has(target, key) || typeof key === 'symbol') return Reflect.get(target, key); return values.get(String(key)) },
      set(target, key, value) { if (Reflect.has(target, key) || typeof key !== 'string') return false; methods.setItem(key, value); return true },
      deleteProperty(target, key) { if (Reflect.has(target, key)) return false; values.delete(String(key)); return true },
      ownKeys(target) { return [...new Set([...Reflect.ownKeys(target), ...values.keys()])] },
      getOwnPropertyDescriptor(target, key) { return Reflect.getOwnPropertyDescriptor(target, key) || (values.has(key) ? { configurable: true, enumerable: true, writable: true, value: values.get(key) } : undefined) },
    })
  }
  Object.defineProperty(win, 'localStorage', { configurable: true, value: memoryStorage() })
  Object.defineProperty(win, 'sessionStorage', { configurable: true, value: memoryStorage() })
  if (win.document) Object.defineProperty(win.document, 'cookie', { configurable: true, get() { return '' }, set() {} })
  // A hash-backed, bounded history lets SPA routers run in the opaque iframe.
  const initial = frontend.hash || '#' + (frontend.pathname + frontend.search || '/')
  const hashFor = raw => {
    if (raw == null || raw === '') return win.location.hash || initial
    const text = String(raw)
    if (text.length > 8192) throw denied()
    if (text.startsWith('#')) return text
    const url = new URLCtor(text, frontend.href)
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw denied()
    if (url.origin === frontend.origin) return url.hash || '#' + url.pathname + url.search
    if (url.origin === base.origin && url.pathname.startsWith(assets.pathname)) return url.hash || '#/' + url.pathname.slice(assets.pathname.length) + url.search
    throw denied()
  }
  const cloneState = value => {
    const result = win.structuredClone(value)
    let text
    try { text = JSON.stringify(result) } catch { throw new Error('预览历史状态无法序列化。') }
    if (String(text).length > 128 * 1024) throw new Error('预览历史状态过大。')
    return result
  }
  let entries = [{ hash: initial, state: null }], index = 0, traversal = 0
  const ownPops = new WeakSet()
  win.addEventListener('popstate', event => { if (!ownPops.has(event)) event.stopImmediatePropagation() }, true)
  const writeHash = hash => { if (win.location.hash !== hash) win.location.hash = hash }
  const move = delta => {
    const next = index + Math.trunc(Number(delta) || 0)
    if (next < 0 || next >= entries.length || next === index) return
    index = next; const entry = entries[index], turn = ++traversal; writeHash(entry.hash)
    win.setTimeout(() => {
      if (turn !== traversal || entries[index] !== entry) return
      const event = typeof win.PopStateEvent === 'function' ? new win.PopStateEvent('popstate', { state: cloneState(entry.state) }) : new win.Event('popstate')
      if (!('state' in event)) Object.defineProperty(event, 'state', { value: cloneState(entry.state) })
      ownPops.add(event); win.dispatchEvent(event)
    }, 0)
  }
  const history = { pushState(state, unused, url) {
    const item = { state: cloneState(state), hash: hashFor(url) }
    entries = entries.slice(0, index + 1); entries.push(item)
    if (entries.length > 256) entries.shift()
    index = entries.length - 1; traversal++; writeHash(item.hash)
  }, replaceState(state, unused, url) { entries[index] = { state: cloneState(state), hash: hashFor(url) }; traversal++; writeHash(entries[index].hash) },
  go: move, back() { move(-1) }, forward() { move(1) }, scrollRestoration: 'auto' }
  Object.defineProperties(history, { state: { get() { return cloneState(entries[index].state) } }, length: { get() { return entries.length } } })
  Object.defineProperty(win, 'history', { configurable: true, value: history })
  writeHash(initial)
  win.addEventListener('error', event => notify({ type: 'error', message: String(event.message || '页面资源未加载，请检查前端日志。').slice(0, 1000) }), true)
  win.addEventListener('unhandledrejection', event => notify({ type: 'error', message: String(event.reason?.message || event.reason || '页面运行失败。').slice(0, 1000) }))
  const ready = () => notify({ type: 'ready' })
  if (win.document?.readyState === 'loading') win.document.addEventListener('DOMContentLoaded', ready, { once: true })
  else ready()
  return { mapped, persistent: false, storage: 'memory' }
}

function prepareIntegrationHtml(input) {
  const remove = [], positions = { head: null, html: null }
  let offset = 0, inert = 0
  while (offset < input.length) {
    const start = input.indexOf('<', offset)
    if (start < 0) break
    if (input.startsWith('<!--', start)) { const end = input.indexOf('-->', start + 4); offset = end < 0 ? input.length : end + 3; continue }
    const tag = /^<\s*(\/?)\s*([a-z][a-z\d:-]*)\b/i.exec(input.slice(start))
    if (!tag) { offset = start + 1; continue }
    let end = start + tag[0].length, quote = ''
    for (; end < input.length; end++) { const c = input[end]; if (quote) { if (c === quote) quote = '' } else if (c === '"' || c === "'") quote = c; else if (c === '>') { end++; break } }
    const name = tag[2].toLowerCase()
    if (tag[1] && ['template', 'svg', 'math'].includes(name)) inert = Math.max(0, inert - 1)
    if (!tag[1]) {
      if (!inert && name === 'base') remove.push([start, end])
      if (!inert && Object.hasOwn(positions, name) && positions[name] === null) positions[name] = end
      if (['template', 'svg', 'math'].includes(name) && !/\/\s*>$/.test(input.slice(start, end))) inert++
      if (['script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noscript'].includes(name)) {
        const closing = new RegExp(`<\\s*/\\s*${name}\\s*>`, 'ig'); closing.lastIndex = end
        const found = closing.exec(input); offset = found ? closing.lastIndex : input.length; continue
      }
    }
    offset = end
  }
  let html = input
  for (const [start, end] of remove.slice().reverse()) html = html.slice(0, start) + html.slice(end)
  const adjust = index => index == null ? null : index - remove.filter(([, end]) => end <= index).reduce((sum, [start, end]) => sum + end - start, 0)
  return { html, head: adjust(positions.head), root: adjust(positions.html) }
}

export function createIntegrationPreviewDocument(preview, { channel } = {}) {
  if (typeof preview?.html !== 'string' || preview.html.length > 4 * 1024 * 1024) throw new Error('联调页面内容为空或过大。')
  if (typeof channel !== 'string' || !/^[a-z\d_-]{8,160}$/i.test(channel)) throw new Error('联调预览通道无效。')
  let base, assets, proxy, frontend, backend
  try {
    base = new URL(preview.base_url); assets = new URL(preview.asset_prefix); proxy = new URL(preview.api_proxy_url)
    frontend = new URL(preview.frontend_url); backend = new URL(preview.backend_url)
    for (const url of [base, assets, proxy, frontend, backend]) if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error()
    if (base.origin !== assets.origin || base.origin !== proxy.origin || !base.pathname.startsWith(assets.pathname)
      || !assets.pathname.startsWith('/api/v1/code/frontend-design-assets/') || !proxy.pathname.startsWith('/api/v1/code/integration-api/')
      || !assets.pathname.endsWith('/') || !proxy.pathname.endsWith('/')) throw new Error()
  } catch { throw new Error('联调预览资源地址无效，请刷新预览。') }
  const config = JSON.stringify({ ...preview, html: undefined, channel }).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029')
  base.hash = '/'
  const escape = text => text.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
  const script = `try{(${installIntegrationPreviewEnvironment.toString()})(window,${config});}catch(error){window.parent.postMessage({channel:${JSON.stringify(channel)},type:'error',message:String(error.message||error).slice(0,1000)},'*');}`
  const additions = `<base href="${escape(base.href)}"><script data-code-agent-integration>${script}</script>`
  const document = prepareIntegrationHtml(preview.html)
  if (document.head != null) return document.html.slice(0, document.head) + additions + document.html.slice(document.head)
  if (document.root != null) return document.html.slice(0, document.root) + `<head>${additions}</head>` + document.html.slice(document.root)
  return `<!doctype html><html><head>${additions}</head><body>${document.html}</body></html>`
}
