// LSP hover accepts legacy Markdown strings, MarkedString code blocks and
// explicitly typed MarkupContent. Keep those distinctions for the full view:
// plaintext (including Javadoc-looking text) must never become active Markdown.
import { Marked } from 'marked'

const hoverParser = new Marked({ gfm: true, async: false })

export function normalizeHoverDocumentation(contents) {
  const items = Array.isArray(contents) ? contents : [contents]
  return items.flatMap(item => {
    if (typeof item === 'string') return item ? [{ kind: 'markdown', value: item }] : []
    if (!item || typeof item !== 'object' || typeof item.value !== 'string' || !item.value) return []
    if (item.kind === 'markdown' || item.kind === 'plaintext') return [{ kind: item.kind, value: item.value }]
    // Our shared hover pipeline also receives sections normalized by an earlier
    // stage. Preserve their code signatures when inserting function prose.
    if (item.kind !== undefined && item.kind !== 'code' || typeof item.language !== 'string') return []
    // A language identifier is an info string, not arbitrary Markdown. Values
    // such as `java\n```\n[run](command:...)` must not escape a code signature.
    const language = /^[a-zA-Z][a-zA-Z0-9_+#.-]{0,63}$/.test(item.language) ? item.language : 'plaintext'
    return [{ kind: 'code', language, value: item.value }]
  })
}

export function hoverCodeBlock(value, language = 'plaintext') {
  const safeLanguage = /^[a-zA-Z][a-zA-Z0-9_+#.-]{0,63}$/.test(language) ? language : 'plaintext'
  const longest = (value.match(/`+/g) || []).reduce((length, run) => Math.max(length, run.length), 0)
  const fence = '`'.repeat(Math.max(3, longest + 1))
  return `${fence}${safeLanguage}\n${value}\n${fence}`
}

function literalMarkdown(value) {
  // Entities are decoded after Markdown parsing. Unlike backslash escapes they
  // also keep leading indentation and bare URLs from acquiring code/link
  // semantics, and don't leak literal backslashes inside an indented block.
  return value.replace(/&/g, '&amp;')
    .replace(/[\\`*_{}\[\]()#+\-.!<>|~=:]/g, character => `&#${character.codePointAt(0)};`)
    .replace(/^[ \t]+/gm, spaces => spaces.replace(/ /g, '&nbsp;').replace(/\t/g, '&nbsp;'.repeat(4)))
    .replace(/\r\n|\r|\n/g, '  \n')
}

function imageLabel(token) {
  const label = literalMarkdown(token.text?.replace(/\s+/g, ' ').trim() || '图片')
  const href = typeof token.href === 'string' ? token.href.trim() : ''
  if (/^https?:\/\//i.test(href) && !/[\u0000-\u0020\u007f<>\\]/.test(href)) {
    try { if (['https:', 'http:'].includes(new URL(href).protocol)) return `[${label}](<${href}>)` } catch { /* Use a literal label. */ }
  }
  return label
}

// Monaco loads Markdown images even when isTrusted/supportHtml are false.
// Locate image tokens in their own Markdown containers rather than rewriting
// strings globally: identical image syntax inside code examples stays intact.
function withoutImageLoads(value) {
  const source = value.replace(/\r\n|\r/g, '\n'), tokens = hoverParser.lexer(source)
  const images = []
  hoverParser.walkTokens(tokens, token => { if (token.type === 'image') images.push(token) })
  if (!images.length) return value
  const replacements = []
  function visit(children, container, offset) {
    let cursor = 0
    for (const token of children) {
      if (typeof token.raw !== 'string' || !token.raw) continue
      const start = container.indexOf(token.raw, cursor)
      if (start < 0) continue
      cursor = start + token.raw.length
      if (token.type === 'image') {
        replacements.push({ start: offset + start, end: offset + cursor, value: imageLabel(token) })
      } else if (!['code', 'codespan', 'html'].includes(token.type)) {
        const nested = token.type === 'list' ? token.items : token.type === 'table'
          ? [...token.header, ...token.rows.flat()].flatMap(cell => cell.tokens || []) : token.tokens
        if (Array.isArray(nested)) visit(nested, token.raw, offset + start)
      }
    }
  }
  visit(tokens, source, 0)
  // Nested blockquote/list indentation can make the parser's raw text differ
  // from the source. If exact ranges cannot be proved, show the original as
  // literal text instead of leaving an active image or altering code samples.
  if (replacements.length !== images.length) return literalMarkdown(value)
  replacements.sort((left, right) => left.start - right.start)
  let result = '', cursor = 0
  for (const replacement of replacements) {
    if (replacement.start < cursor) return literalMarkdown(value)
    result += source.slice(cursor, replacement.start) + replacement.value
    cursor = replacement.end
  }
  return result + source.slice(cursor)
}

export function hoverMarkdownStrings(contents) {
  return normalizeHoverDocumentation(contents).map(section => ({
    value: section.kind === 'code' ? hoverCodeBlock(section.value, section.language)
      : section.kind === 'plaintext' ? literalMarkdown(section.value) : withoutImageLoads(section.value),
    // Never inherit a server's trust, baseUri, HTML or theme-icon flags. Both
    // command links and embedded HTML remain disabled by Monaco's renderer.
    isTrusted: false,
    supportHtml: false,
    supportThemeIcons: false,
  }))
}
