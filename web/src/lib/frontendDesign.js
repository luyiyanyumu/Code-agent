import { installDesignEnvironment } from './frontendDesignEnvironment.js'
import { createFrontendDesignRuntime } from './frontendDesignRuntime.js'

const helpers = createFrontendDesignRuntime()
export const {
  STYLE_FIELDS, MAX_LAYERS, normalizeLayers, normalizeTransformDeclarations, normalizeStyleDeclarations, serializeDesignCss, parseDesignCss,
  isManagedDesignCss, createStyleRequest, parseStyleResult, validateBridgeMessage,
  sanitizeDesignResourcePath, sanitizeDesignErrorMessage, describeDesignRuntimeError,
} = helpers

function escapeAttribute(value) {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** Locate actual HTML tags without changing strings in scripts or comments. */
function prepareHtml(input) {
  const removals = []
  let headEnd = null
  let htmlEnd = null
  let position = 0
  let inertDepth = 0
  let bodySeen = false
  while (position < input.length) {
    const open = input.indexOf('<', position)
    if (open < 0) break
    if (input.startsWith('<!--', open)) {
      const close = input.indexOf('-->', open + 4)
      position = close < 0 ? input.length : close + 3
      continue
    }
    const tag = /^<\s*(\/?)\s*([a-z][a-z0-9:-]*)\b/i.exec(input.slice(open))
    if (!tag) { position = open + 1; continue }
    let end = open + tag[0].length
    let quote = ''
    for (; end < input.length; end++) {
      const character = input[end]
      if (quote) { if (character === quote) quote = '' }
      else if (character === '"' || character === "'") quote = character
      else if (character === '>') { end++; break }
    }
    const name = tag[2].toLowerCase()
    if (tag[1] && ['template', 'svg', 'math'].includes(name)) inertDepth = Math.max(0, inertDepth - 1)
    if (!tag[1]) {
      if (!inertDepth) {
        if (name === 'body') bodySeen = true
        if (name === 'head' && headEnd === null && !bodySeen) headEnd = end
        if (name === 'html' && htmlEnd === null && !bodySeen) htmlEnd = end
        if (name === 'base') removals.push([open, end])
      }
      if (['template', 'svg', 'math'].includes(name) && !/\/\s*>$/.test(input.slice(open, end))) inertDepth++
      if (name === 'plaintext') { position = input.length; continue }
      if (['script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript'].includes(name)) {
        const close = new RegExp(`<\\s*/\\s*${name}\\s*>`, 'ig')
        close.lastIndex = end
        const closing = close.exec(input)
        position = closing ? close.lastIndex : input.length
        continue
      }
    }
    position = end
  }
  let html = input
  for (const [start, end] of removals.slice().reverse()) html = html.slice(0, start) + html.slice(end)
  const offset = index => index === null ? null : index - removals.filter(([, end]) => end <= index).reduce((sum, [start, end]) => sum + end - start, 0)
  return { html, headEnd: offset(headEnd), htmlEnd: offset(htmlEnd) }
}

export function createDesignDocument(html, { channel, baseUrl = '', css = '', mode = 'acceptance', initialRoute = '/', resourceRootUrl = baseUrl, apiUrl = '', apiProxyUrl = '' } = {}) {
  if (typeof html !== 'string' || html.length > 4 * 1024 * 1024) throw new Error('页面内容为空或过大。')
  if (typeof channel !== 'string' || channel.length < 8 || channel.length > 160) throw new Error('页面预览通道无效。')
  let base = ''
  if (baseUrl) {
    try {
      const url = new URL(baseUrl)
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || baseUrl.length > 8192) throw new Error()
      // Vue Router's browser history supports a hash-bearing base. In srcdoc
      // this keeps app routes independent of the opaque document's pathname.
      url.hash = '/'
      base = `<base href="${escapeAttribute(url.href)}" data-code-agent-design="base">`
    } catch { throw new Error('页面资源地址无效。') }
  }
  const initialCss = helpers.rulesBody(parseDesignCss(css))
  const config = JSON.stringify({ channel, mode: mode === 'edit' ? 'edit' : 'acceptance', baseUrl, initialRoute, resourceRootUrl, apiUrl, apiProxyUrl }).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029')
  // Serialize a single lexical scope rather than named helpers independently.
  // A minified build renames local bindings, and toString() must preserve both
  // their declarations and all references in the same function expression.
  const runtime = `(() => {\nconst config = ${config};\n(${createFrontendDesignRuntime.toString()})(config, (${installDesignEnvironment.toString()}));\n})();`
  const additions = `${base}<style id="code-agent-design-style" data-code-agent-design="style">${initialCss}</style><script data-code-agent-design="bridge">${runtime}</script>`
  const prepared = prepareHtml(html)
  if (prepared.headEnd !== null) return prepared.html.slice(0, prepared.headEnd) + additions + prepared.html.slice(prepared.headEnd)
  if (prepared.htmlEnd !== null) return prepared.html.slice(0, prepared.htmlEnd) + `<head>${additions}</head>` + prepared.html.slice(prepared.htmlEnd)
  return `<!doctype html><html><head>${additions}</head><body>${prepared.html}</body></html>`
}
