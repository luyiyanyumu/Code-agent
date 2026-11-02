// Display metadata only. The enhanced Chinese hover sections remain complete
// and in server/application order; parsing must never replace their content.
import { Marked } from 'marked'

const parser = new Marked({ gfm: true, async: false })
const qualifiedName = /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$/
const safeLanguage = value => typeof value === 'string' && /^[a-zA-Z][a-zA-Z0-9_+#.-]{0,63}$/.test(value) ? value : 'plaintext'

export function documentationHref(value) {
  if (typeof value !== 'string' || /[\u0000-\u0020\u007f<>\\]/.test(value) || !/^https?:\/\//i.test(value)) return ''
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}

function signatureCandidate(section) {
  if (section.kind === 'plaintext') return null
  if (section.kind === 'code') return { value: section.value, language: safeLanguage(section.language) }
  const tokens = parser.lexer(section.value).filter(token => token.type !== 'space')
  // Only a leading declaration can describe this symbol. A later example in
  // prose is not the hovered declaration, even if it happens to contain code.
  const first = tokens[0]
  if (first?.type === 'code') return { value: first.text, language: safeLanguage(first.lang?.split(/\s/)[0]) }
  if (first?.type === 'paragraph' && qualifiedName.test(first.text.trim()) && tokens[1]?.type === 'code') {
    return { value: tokens[1].text, language: safeLanguage(tokens[1].lang?.split(/\s/)[0]), owner: first.text.trim() }
  }
  return null
}

function isDeclaration(value) {
  const text = value.trim()
  if (!text || text.length > 24000) return false
  return /(?:^|\n)\s*(?:(?:export|declare|default|public|private|protected|abstract|static|final|sealed|async|unsafe|pub|readonly|open|internal)\s+)*(?:@interface|class|interface|enum|record|struct|type|namespace|def|function|fn|func|val|var|const|let)\s+[\w$]/.test(text)
    || /^\((?:method|function|class|property|parameter|variable|alias)\)\s+/.test(text)
    || /(?:^|\n)\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native|default)\s+)+(?:<[^\n]+>\s+)?[^\n;{}]+\s+[\w.$]+\s*\(/.test(text)
    || /(?:^|\n)\s*[\w$]+(?:\.[\w$]+)*(?:<[^\n;{}]+>|\[\])?\s+[\w.$]+\s*\([^;{}]*\)\s*(?:throws\s+[^;{}]+)?\s*;?$/.test(text)
}

function ownerOf(value) {
  const lines = value.trim().split(/\r\n|\r|\n/)
  const packageLine = lines.map(line => /^\s*package\s+([\w$.]+)\s*;?\s*$/.exec(line)).find(Boolean)
  if (packageLine && qualifiedName.test(packageLine[1])) return packageLine[1]
  if (lines.length > 1 && qualifiedName.test(lines[0].trim())) return lines[0].trim()
  const method = /\s([\w$]+(?:\.[\w$]+){2,})\s*\(/.exec(value)
  return method ? method[1].slice(0, method[1].lastIndexOf('.')) : ''
}

export function buildQuickDocumentation(document) {
  const rows = Array.isArray(document?.sections) ? document.sections : []
  const sections = rows.filter(row => row && ['code', 'markdown', 'plaintext'].includes(row.kind)
    && typeof row.value === 'string' && row.value.length).map(row => ({ ...row }))
  let declaration = '', language = 'plaintext', owner = '', declarationIndex = -1
  for (const [index, section] of sections.entries()) {
    const candidate = signatureCandidate(section)
    if (!candidate || !isDeclaration(candidate.value)) continue
    declaration = candidate.value
    language = candidate.language
    owner = candidate.owner || ownerOf(candidate.value)
    declarationIndex = index
    break
  }
  const links = []
  const seen = new Set()
  for (const section of sections) {
    if (section.kind !== 'markdown') continue
    parser.walkTokens(parser.lexer(section.value), token => {
      if (token.type !== 'link') return
      const href = documentationHref(token.href)
      if (!href || seen.has(href)) return
      seen.add(href)
      links.push({ href, label: String(token.text || '参考文档') })
    })
  }
  return { declaration, language, owner, links, sections: sections.map((section, index) => ({
    ...section,
    declaration: index === declarationIndex,
    heading: index === 0 || (section.kind === 'code') !== (sections[index - 1].kind === 'code')
      ? section.kind === 'code' ? '声明签名' : '说明' : '',
  })) }
}
