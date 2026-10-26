/**
 * A deliberately isolated browser environment for the opaque design iframe.
 * This function is serialized into srcdoc; keep every dependency inside it.
 * Nothing here reads a parent window, real browser storage, or navigates a page.
 */
export function installDesignEnvironment(win, { baseUrl = '', initialRoute = '/', resourceRootUrl = '', apiProxyUrl = '', apiUrl = '' } = {}) {
  const MAX_STORAGE_ENTRIES = 512
  const MAX_STORAGE_BYTES = 256 * 1024
  const MAX_HISTORY_ENTRIES = 256
  const MAX_STATE_BYTES = 256 * 1024
  const MAX_ROUTE_LENGTH = 4096
  const createError = (name, message) => {
    if (typeof win.DOMException === 'function') return new win.DOMException(message, name)
    const error = new Error(message)
    error.name = name
    return error
  }
  // Count UTF-8 bytes, including lone surrogates as replacement characters.
  const byteLength = value => {
    let bytes = 0
    for (const character of value) {
      const point = character.codePointAt(0)
      bytes += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4
    }
    return bytes
  }
  const createStorage = () => {
    const values = new Map()
    let bytes = 0
    const set = (rawKey, rawValue) => {
      const key = String(rawKey)
      const value = String(rawValue)
      const prior = values.get(key)
      const nextBytes = bytes - (values.has(key) ? byteLength(key) + byteLength(prior) : 0) + byteLength(key) + byteLength(value)
      if (nextBytes > MAX_STORAGE_BYTES || (!values.has(key) && values.size >= MAX_STORAGE_ENTRIES)) {
        throw createError('QuotaExceededError', '预览临时存储已达到容量限制。')
      }
      values.set(key, value)
      bytes = nextBytes
    }
    const remove = rawKey => {
      const key = String(rawKey)
      if (values.has(key)) bytes -= byteLength(key) + byteLength(values.get(key))
      values.delete(key)
    }
    const target = Object.create(null)
    Object.defineProperties(target, {
      getItem: { value(key) { return values.get(String(key)) ?? null }, configurable: true },
      setItem: { value: set, configurable: true },
      removeItem: { value: remove, configurable: true },
      clear: { value() { values.clear(); bytes = 0 }, configurable: true },
      key: { value(index) {
        const number = Number(index)
        const position = Number.isNaN(number) ? 0 : Math.trunc(number)
        if (position < 0 || !Number.isFinite(position) || position >= values.size) return null
        return [...values.keys()][position]
      }, configurable: true },
      length: { get() { return values.size }, configurable: true },
      toString: { value() { return '[object Storage]' }, configurable: true },
      [Symbol.toStringTag]: { value: 'Storage', configurable: true },
    })
    return new Proxy(target, {
      get(object, property, receiver) {
        if (Reflect.has(object, property) || typeof property === 'symbol') return Reflect.get(object, property, receiver)
        return values.get(String(property))
      },
      set(object, property, value) {
        if (typeof property === 'symbol' || Reflect.has(object, property)) return false
        set(property, value)
        return true
      },
      deleteProperty(object, property) {
        if (Reflect.has(object, property)) return false
        remove(property)
        return true
      },
      has(object, property) { return Reflect.has(object, property) || values.has(String(property)) },
      ownKeys(object) { return [...new Set([...Reflect.ownKeys(object), ...values.keys()])] },
      getOwnPropertyDescriptor(object, property) {
        const own = Reflect.getOwnPropertyDescriptor(object, property)
        if (own) return own
        if (typeof property === 'string' && values.has(property)) return { value: values.get(property), enumerable: true, configurable: true, writable: true }
        return undefined
      },
      defineProperty(object, property, descriptor) {
        if (Reflect.has(object, property) || typeof property !== 'string' || descriptor.configurable === false || !Object.hasOwn(descriptor, 'value')) return false
        set(property, descriptor.value)
        return true
      },
      preventExtensions() { return false },
      setPrototypeOf(object, prototype) { return prototype === null },
    })
  }
  const URLCtor = win.URL
  let base
  try {
    base = new URLCtor(baseUrl)
    if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || !base.pathname.endsWith('/')) throw new Error()
    base.hash = ''
    base.search = ''
  } catch {
    throw createError('SecurityError', '预览环境需要有效的受控资源目录。')
  }
  if (typeof win.structuredClone !== 'function' || typeof win.dispatchEvent !== 'function' || !win.location) {
    throw createError('NotSupportedError', '当前浏览器无法安装隔离预览环境。')
  }
  if (typeof initialRoute !== 'string' || !initialRoute.startsWith('/') || initialRoute.startsWith('//') || initialRoute.length > MAX_ROUTE_LENGTH - 1 || /[\u0000-\u001f\u007f]/.test(initialRoute)) {
    throw createError('SecurityError', '预览初始路由不受支持。')
  }
  let resourceRoot = null
  if (resourceRootUrl) {
    try {
      resourceRoot = new URLCtor(resourceRootUrl)
      const capability = base.pathname.match(/^(.*\/api\/v1\/code\/frontend-design-assets\/[^/]+\/)/)?.[1]
        || base.pathname.match(/^(\/preview\/[^/]+\/)/)?.[1] || base.pathname
      if (resourceRoot.origin !== base.origin || resourceRoot.username || resourceRoot.password || !resourceRoot.pathname.endsWith('/') || !resourceRoot.pathname.startsWith(capability) || !base.pathname.startsWith(resourceRoot.pathname) || resourceRoot.search || /%2f|%5c|%00/i.test(resourceRoot.pathname)) throw new Error()
      resourceRoot.hash = ''
    } catch {
      throw createError('SecurityError', '预览动态资源目录不在当前受控项目内。')
    }
  }
  let apiTarget = null
  let apiProxy = null
  const assetCapability = base.pathname.match(/^(.*\/api\/v1\/code\/frontend-design-assets\/([^/]+)\/)/)
  if (apiProxyUrl || apiUrl) {
    try {
      if (apiProxyUrl) {
        apiProxy = new URLCtor(apiProxyUrl)
        const expected = '/api/v1/code/frontend-design-api/' + assetCapability?.[2] + '/'
        if (!assetCapability || apiProxy.origin !== base.origin || apiProxy.pathname !== expected || apiProxy.username || apiProxy.password || apiProxy.search || apiProxy.hash) throw new Error()
      }
      if (apiUrl) {
        apiTarget = new URLCtor(apiUrl)
        const host = apiTarget.hostname.toLowerCase()
        if (apiTarget.protocol !== 'http:' || !(/^(?:127\.(?:\d{1,3}\.){2}\d{1,3}|localhost|\[::1\])$/.test(host)) || apiTarget.username || apiTarget.password || apiTarget.search || apiTarget.hash || !apiTarget.pathname.startsWith('/api/') || !apiTarget.pathname.endsWith('/') || apiTarget.port === base.port) throw new Error()
      }
    } catch {
      throw createError('SecurityError', '项目接口配置必须使用同一预览的受控接口及本机项目服务。')
    }
  }
  const installApiEnvironment = () => {
    const virtualDocument = base.origin + '/'
    if (win.document) {
      // Axios may read cookies for XSRF even when the caller requested browser
      // credentials. The opaque preview has no real cookies or login session.
      Object.defineProperty(win.document, 'cookie', { configurable: true, get() { return '' }, set() {} })
    }
    class PreviewURL extends URLCtor {
      constructor(input, documentBase) {
        if (arguments.length < 2) super(input)
        else super(input, /^(?:about:(?:srcdoc|blank)(?:[?#].*)?|null)$/.test(String(documentBase)) ? virtualDocument : documentBase)
      }
    }
    Object.defineProperty(win, 'URL', { value: PreviewURL, configurable: true, writable: true })
    // Application interceptors commonly annotate error.message. These request
    // errors must remain writable; Storage/History still use DOMException.
    const apiError = (name, message) => {
      const error = new Error(message)
      error.name = name
      return error
    }
    const missing = () => apiError('InvalidStateError', '项目接口尚未连接，请在前端设计中填写项目后端 API 地址并连接。')
    const denied = () => apiError('SecurityError', '预览仅允许当前项目的受控只读接口，不允许管理接口、外部服务或敏感路径。')
    const readonly = () => apiError('NotAllowedError', '验收当前仅支持 GET/HEAD 只读接口；写入操作需要另行授权。')
    const validatePath = path => {
      if (path.length > 4096 || path.includes('//') || /[\u0000-\u001f\u007f\\]/.test(path)) throw denied()
      let parts
      try {
        parts = path.split('/').filter(Boolean).map(part => {
          let decoded = part
          for (let step = 0; step < 5; step++) {
            const next = decodeURIComponent(decoded)
            if (next === decoded) break
            decoded = next
          }
          if (decoded.includes('%')) throw denied()
          return decoded
        })
      } catch { throw denied() }
      if (parts.some(part => part.startsWith('.') || /[/\\:?#\u0000-\u001f\u007f]|%2e|%2f|%5c|%00/i.test(part) || /^(?:env|environment|@fs|@id|__open-in-editor|__vite_ping)$/i.test(part) || /credential|secret|private-key/i.test(part))) throw denied()
    }
    const endpoint = rawInput => {
      const input = String(rawInput)
      if (input.length > 8192 || /[\u0000-\u001f\u007f\\]/.test(input)) throw denied()
      const relative = !/^[a-z][a-z0-9+.-]*:/i.test(input) && !input.startsWith('/')
      const relativePath = relative ? input.split(/[?#]/, 1)[0].replace(/^(?:\.\/)+/, '') : ''
      const originalPath = relative ? relativePath : input.startsWith('/') && !input.startsWith('//') ? input.split(/[?#]/, 1)[0]
        : input.match(/^(?:https?:)?\/\/[^/?#]+([^?#]*)/i)?.[1] || ''
      validatePath(originalPath)
      let url
      const projectApi = /^\/api(?:\/|\?|$)/.test(input) || (relative && /^api(?:\/|$)/.test(relativePath))
      try { url = new URLCtor(input, projectApi ? virtualDocument : base.href) } catch { throw denied() }
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash) throw denied()
      // The modulepreload polyfill may fetch its already rewritten asset URL.
      const assetPrefix = assetCapability?.[1] || resourceRoot?.pathname || base.pathname
      if (url.origin === base.origin && url.pathname.startsWith(assetPrefix)) return { url: url.href, asset: true }
      let suffix
      if (url.origin === base.origin && /^\/api(?:\/|$)/.test(url.pathname)) {
        if (apiProxy && url.pathname.startsWith(apiProxy.pathname)) suffix = url.pathname.slice(apiProxy.pathname.length)
        else suffix = url.pathname.slice('/api'.length).replace(/^\//, '')
      } else if (apiTarget && url.origin === apiTarget.origin && url.pathname.startsWith(apiTarget.pathname)) suffix = url.pathname.slice(apiTarget.pathname.length)
      else throw denied()
      validatePath(suffix)
      if (suffix.startsWith('/') || /^v1\/(?:code|sessions?|memory|agents?|projects?|tasks?|parallel|automations?|config|settings|providers?|approvals?|questions|workspaces?|tools|knowledge)(?:\/|$)/i.test(suffix)) throw denied()
      const keys = [...url.searchParams.keys()]
      if (keys.length > 64 || keys.some(key => /authorization|cookie|password|credential|secret|token|apikey|sessionid/i.test(key.replace(/[^a-z0-9]/gi, '')))) throw denied()
      if (!apiTarget || !apiProxy) throw missing()
      const mapped = new URLCtor(suffix + url.search, apiProxy.href)
      if (mapped.origin !== apiProxy.origin || !mapped.pathname.startsWith(apiProxy.pathname)) throw denied()
      return { url: mapped.href, asset: false }
    }
    const method = value => {
      const result = String(value || 'GET').toUpperCase()
      if (!['GET', 'HEAD'].includes(result)) throw readonly()
      return result
    }
    const credentialHeader = name => /^(?:cookie2?|authorization|proxy-authorization|x-api-key|x-auth-token|x-access-token)$/i.test(String(name))
    const NativeRequest = win.Request
    if (typeof NativeRequest === 'function') {
      class PreviewRequest extends NativeRequest {
        constructor(input, options) {
          // Request's native relative resolution also uses about:srcdoc. Only
          // the explicit project API path gets a virtual document fallback.
          if (typeof input === 'string' && /^\/api(?:\/|\?|$)/.test(input)) {
            validatePath(input.split(/[?#]/, 1)[0])
            super(new URLCtor(input, virtualDocument).href, options)
          } else super(input, options)
        }
      }
      Object.defineProperty(win, 'Request', { value: PreviewRequest, configurable: true, writable: true })
    }
    const nativeFetch = win.fetch
    if (typeof nativeFetch === 'function') {
      Object.defineProperty(win, 'fetch', { configurable: true, writable: true, value: async function(input, options = {}) {
        const isRequest = typeof NativeRequest === 'function' && input instanceof NativeRequest
        const requestMethod = method(options.method ?? (isRequest ? input.method : 'GET'))
        const target = endpoint(isRequest ? input.url : input)
        const headers = new win.Headers(options.headers ?? (isRequest ? input.headers : undefined))
        for (const name of [...headers.keys()]) if (credentialHeader(name)) headers.delete(name)
        const parameters = {
          method: requestMethod, headers, credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer',
          signal: options.signal !== undefined ? options.signal : (isRequest ? input.signal : undefined),
          mode: options.mode ?? (isRequest ? input.mode : 'cors'),
          cache: options.cache ?? (isRequest ? input.cache : 'default'),
          integrity: options.integrity ?? (isRequest ? input.integrity : ''),
          keepalive: options.keepalive ?? (isRequest ? input.keepalive : false),
        }
        if (options.body !== undefined && options.body !== null) throw readonly()
        return typeof NativeRequest === 'function'
          ? nativeFetch.call(win, new NativeRequest(target.url, parameters))
          : nativeFetch.call(win, target.url, parameters)
      } })
    }
    const prototype = win.XMLHttpRequest?.prototype
    if (prototype && typeof prototype.open === 'function') {
      const nativeOpen = prototype.open
      const nativeHeader = prototype.setRequestHeader
      const nativeSend = prototype.send
      const credentials = Object.getOwnPropertyDescriptor(prototype, 'withCredentials')
      if (!credentials?.set || !credentials.configurable) throw createError('NotSupportedError', '浏览器未允许安装只读项目接口连接。')
      Object.defineProperty(prototype, 'withCredentials', { ...credentials, set() { credentials.set.call(this, false) } })
      Object.defineProperty(prototype, 'open', { configurable: true, writable: true, value: function(verb, url, asynchronous = true, username, password) {
        const requestMethod = method(verb)
        if ((username != null && String(username)) || (password != null && String(password))) throw denied()
        const target = endpoint(url)
        const result = nativeOpen.call(this, requestMethod, target.url, asynchronous)
        credentials.set.call(this, false)
        return result
      } })
      if (typeof nativeHeader === 'function') Object.defineProperty(prototype, 'setRequestHeader', { configurable: true, writable: true, value: function(name, value) {
        if (!credentialHeader(name)) return nativeHeader.call(this, name, value)
      } })
      if (typeof nativeSend === 'function') Object.defineProperty(prototype, 'send', { configurable: true, writable: true, value: function(body) {
        if (body != null) throw readonly()
        credentials.set.call(this, false)
        return nativeSend.call(this, null)
      } })
    }
  }
  const installResourceMapping = () => {
    if (!resourceRoot) return
    const nativeSetAttribute = win.Element?.prototype?.setAttribute
    if (typeof nativeSetAttribute !== 'function') throw createError('NotSupportedError', '当前浏览器无法安装预览动态资源映射。')
    const moduleSuffix = /\.(?:js|mjs|cjs)$/i
    const imageSuffix = /\.(?:png|jpe?g|gif|webp|avif|svg|ico|bmp)$/i
    const fontSuffix = /\.(?:woff2?|ttf|otf|eot)$/i
    const rejected = () => createError('SecurityError', '动态资源不是当前项目内允许预览的前端文件。')
    const category = (element, attribute, changed = {}) => {
      const tag = String(element.localName || element.tagName || '').toLowerCase()
      if (tag === 'script' && attribute === 'src') return 'script'
      if (tag === 'img' && (attribute === 'src' || attribute === 'srcset')) return 'image'
      if (tag !== 'link' || attribute !== 'href') return ''
      const relations = String(changed.rel ?? element.getAttribute('rel') ?? '').toLowerCase().split(/\s+/)
      if (relations.includes('stylesheet')) return 'style'
      if (relations.includes('modulepreload')) return 'script'
      if (relations.includes('preload')) {
        const as = String(changed.as ?? element.getAttribute('as') ?? '').toLowerCase()
        if (['style', 'script', 'image', 'font'].includes(as)) return as
      }
      return ''
    }
    const mapUrl = (raw, kind) => {
      const value = String(raw)
      let input = value.trim()
      if (value.length > 8192 || /[\u0000-\u001f\u007f\\]/.test(value)) throw rejected()
      if (!input) return value
      // Existing rewritten capability URLs do not receive a second prefix.
      if (/^[a-z][a-z0-9+.-]*:/i.test(input) || input.startsWith('//')) {
        if (kind === 'image' && /^(?:data:image\/|blob:)/i.test(input)) return value
        let url
        try { url = new URLCtor(input, base.href) } catch { throw rejected() }
        if (!['http:', 'https:'].includes(url.protocol) || url.origin !== resourceRoot.origin || url.username || url.password) throw rejected()
        if (url.pathname.startsWith(resourceRoot.pathname)) return value
        // Vite's import.meta.resolve turns '/assets/chunk.js' into an absolute
        // URL on the preview server. Its origin alone never grants permission:
        // revalidate the project resource path below before mapping it.
        const originalPath = input.match(/^(?:https?:)?\/\/[^/?#]+([^?#]*)/i)?.[1] || ''
        if (originalPath.split('/').some(part => {
          try { return decodeURIComponent(part).startsWith('.') } catch { return true }
        })) throw rejected()
        input = url.pathname + url.search + url.hash
      }
      // Relative URLs already resolve through the document's controlled <base>.
      if (!input.startsWith('/')) return value
      const rawPath = input.split(/[?#]/, 1)[0]
      let parts
      try { parts = rawPath.slice(1).split('/').map(part => decodeURIComponent(part)) } catch { throw rejected() }
      if (!parts.length || parts.some(part => !part || part.startsWith('.') || /[\u0000-\u001f\u007f/\\]|%2e|%2f|%5c|%00/i.test(part))) throw rejected()
      const filename = parts.at(-1)
      const isModule = moduleSuffix.test(filename) || (/\.vue$/i.test(filename) && /[?&](?:vue|import)(?:[=&]|$)/.test(input))
      if (parts.some((part, position) => ['credentials', 'secrets', '@fs', '__open-in-editor', '__vite_ping'].includes(part.toLowerCase()) || (part.toLowerCase() === 'api' && !(isModule && position > 0 && parts[position - 1].toLowerCase() === 'src'))) || /credential|secret|private-key/i.test(filename)) throw rejected()
      const allowed = kind === 'script' ? isModule : kind === 'style' ? /\.css$/i.test(filename) : kind === 'image' ? imageSuffix.test(filename) : kind === 'font' && fontSuffix.test(filename)
      if (!allowed) throw rejected()
      const mapped = new URLCtor(input.slice(1), resourceRoot.href)
      for (const key of mapped.searchParams.keys()) if (/^(?:token|access[_-]?token|api[_-]?key|authorization|auth|password|passwd|secret|credential|signature)$/i.test(key)) throw rejected()
      if (!mapped.pathname.startsWith(resourceRoot.pathname)) throw rejected()
      return mapped.href
    }
    const mapValue = (element, attribute, raw, changed) => {
      const kind = category(element, attribute, changed)
      if (!kind) return raw
      if (attribute !== 'srcset') return mapUrl(raw, kind)
      const value = String(raw)
      if (!value.trim()) return value
      if (value.length > 8192) throw rejected()
      // A single data image needs no path mapping; its commas are not separators.
      if (/^\s*data:image\//i.test(value) && !/\s+\d+(?:w|x)\s*,/.test(value)) return value
      const candidates = value.split(',')
      if (candidates.length > 32) throw rejected()
      return candidates.map(candidate => {
        const match = candidate.trim().match(/^(\S+)(?:\s+(\d+w|(?:\d+(?:\.\d+)?|\.\d+)x))?$/)
        if (!match) throw rejected()
        return mapUrl(match[1], 'image') + (match[2] ? ' ' + match[2] : '')
      }).join(', ')
    }
    const updatePendingLink = (element, attribute, value) => {
      if (String(element.localName || element.tagName || '').toLowerCase() !== 'link' || !['rel', 'as'].includes(attribute)) return
      const href = element.getAttribute('href')
      if (href === null) return
      const mapped = mapValue(element, 'href', href, { [attribute]: value })
      if (mapped !== href) nativeSetAttribute.call(element, 'href', mapped)
    }
    for (const [name, properties] of [['HTMLScriptElement', ['src']], ['HTMLLinkElement', ['href', 'rel', 'as']], ['HTMLImageElement', ['src', 'srcset']]]) {
      const prototype = win[name]?.prototype
      if (!prototype) throw createError('NotSupportedError', '当前浏览器缺少预览资源元素接口。')
      for (const property of properties) {
        const descriptor = Object.getOwnPropertyDescriptor(prototype, property)
        if (!descriptor?.set || !descriptor.configurable) throw createError('NotSupportedError', '浏览器未允许映射预览资源属性。')
        Object.defineProperty(prototype, property, { ...descriptor, set(value) {
          updatePendingLink(this, property, value)
          descriptor.set.call(this, mapValue(this, property, value))
        } })
      }
    }
    const descriptor = Object.getOwnPropertyDescriptor(win.Element.prototype, 'setAttribute')
    Object.defineProperty(win.Element.prototype, 'setAttribute', { ...descriptor, value(name, value) {
      const attribute = String(name).toLowerCase()
      updatePendingLink(this, attribute, value)
      return nativeSetAttribute.call(this, name, mapValue(this, attribute, value))
    } })
  }
  const routeHash = value => {
    if (typeof value !== 'string' || value.length > MAX_ROUTE_LENGTH || /[\u0000-\u001f\u007f]/.test(value)) {
      throw createError('SecurityError', '预览路由地址不受支持。')
    }
    const hash = value === '' || value === '#' ? '#/' : value
    if (!hash.startsWith('#')) throw createError('SecurityError', '预览只支持页面内的 hash 路由。')
    return hash
  }
  const parseRoute = rawUrl => {
    if (rawUrl === undefined || rawUrl === null || rawUrl === '') return routeHash(String(win.location.hash || '#/'))
    const input = String(rawUrl)
    if (input.startsWith('#')) return routeHash(input)
    if (input.length > 8192 || /[\u0000-\u001f\u007f]/.test(input) || !input.includes('#')) {
      throw createError('SecurityError', '预览只支持受控页面内的 hash 路由。')
    }
    let url
    try { url = new URLCtor(input, base.href) } catch { throw createError('SecurityError', '预览路由地址不受支持。') }
    if (url.origin !== base.origin || url.username || url.password || !['http:', 'https:'].includes(url.protocol) || !url.pathname.startsWith(base.pathname) || /%2f|%5c|%00/i.test(url.pathname)) {
      throw createError('SecurityError', '预览无法跳转到当前资源目录以外。')
    }
    return routeHash(url.hash)
  }
  const cloneState = value => {
    let result
    try { result = win.structuredClone(value) } catch { throw createError('DataCloneError', '预览历史状态不能克隆。') }
    let size = 0
    let count = 0
    const seen = new Set()
    const pending = [result]
    while (pending.length) {
      const item = pending.pop()
      if (typeof item === 'string') size += byteLength(item)
      else if (item && typeof item === 'object' && !seen.has(item)) {
        seen.add(item)
        if (++count > 10000) throw createError('QuotaExceededError', '预览历史状态已达到容量限制。')
        if (ArrayBuffer.isView(item) || item instanceof ArrayBuffer) size += item.byteLength
        else if (item instanceof Map) {
          for (const [key, entry] of item) pending.push(key, entry)
        } else if (item instanceof Set) {
          for (const entry of item) pending.push(entry)
        } else {
          for (const key of Object.keys(item)) { size += byteLength(key); pending.push(item[key]) }
          if (typeof item.size === 'number' && Number.isFinite(item.size)) size += item.size
        }
      } else size += 8
      if (size > MAX_STATE_BYTES) throw createError('QuotaExceededError', '预览历史状态已达到容量限制。')
    }
    return result
  }
  let entries = [{ hash: routeHash(String(win.location.hash || '#' + initialRoute)), state: null }]
  let index = 0
  let traversal = 0
  let scrollRestoration = 'auto'
  const ownPopStates = new WeakSet()
  const writeHash = hash => {
    if (String(win.location.hash) !== hash) win.location.hash = hash
    if (String(win.location.hash) !== hash) throw createError('NotSupportedError', '浏览器未允许更新预览 hash 路由。')
  }
  const pop = entry => {
    const state = cloneState(entry.state)
    let event
    if (typeof win.PopStateEvent === 'function') event = new win.PopStateEvent('popstate', { state })
    else {
      event = new win.Event('popstate')
      Object.defineProperty(event, 'state', { value: state })
    }
    ownPopStates.add(event)
    win.dispatchEvent(event)
  }
  const history = Object.create(null)
  const move = delta => {
    const number = Number(delta)
    const target = index + (Number.isNaN(number) ? 0 : Math.trunc(number))
    if (!Number.isFinite(target) || target === index || target < 0 || target >= entries.length) return
    const entry = entries[target]
    writeHash(entry.hash)
    index = target
    const turn = ++traversal
    // Native History traversal is asynchronous. Only our own popstate is exposed;
    // a fragment assignment may also emit a native event on some browsers.
    win.setTimeout(() => { if (turn === traversal && entries[index] === entry) pop(entry) }, 0)
  }
  Object.defineProperties(history, {
    state: { get() { return cloneState(entries[index].state) }, enumerable: false },
    length: { get() { return entries.length }, enumerable: false },
    scrollRestoration: { get() { return scrollRestoration }, set(value) { if (value === 'auto' || value === 'manual') scrollRestoration = value } },
    pushState: { value(state, unused, url) {
      const hash = parseRoute(url)
      const value = cloneState(state)
      writeHash(hash)
      entries = entries.slice(0, index + 1)
      entries.push({ hash, state: value })
      if (entries.length > MAX_HISTORY_ENTRIES) entries.shift()
      index = entries.length - 1
      traversal++
    } },
    replaceState: { value(state, unused, url) {
      const hash = parseRoute(url)
      const value = cloneState(state)
      writeHash(hash)
      entries[index] = { hash, state: value }
      traversal++
    } },
    go: { value: move },
    back: { value() { move(-1) } },
    forward: { value() { move(1) } },
    [Symbol.toStringTag]: { value: 'History' },
  })
  try {
    // Defining own properties avoids invoking the native opaque-origin getters.
    Object.defineProperty(win, 'localStorage', { value: createStorage(), configurable: true, enumerable: true })
    Object.defineProperty(win, 'sessionStorage', { value: createStorage(), configurable: true, enumerable: true })
    Object.defineProperty(win, 'history', { value: history, configurable: true, enumerable: true })
    win.addEventListener('popstate', event => {
      if (!ownPopStates.has(event)) event.stopImmediatePropagation()
    }, true)
    writeHash(entries[0].hash)
    installResourceMapping()
    installApiEnvironment()
  } catch {
    throw createError('NotSupportedError', '浏览器未允许安装隔离预览存储、路由或资源映射。')
  }
  return { storage: 'memory', history: 'hash', persistent: false }
}
