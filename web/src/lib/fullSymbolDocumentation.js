// Reading projection only: never rewrite the cached language-server receipts.
import { Marked } from 'marked'
import { normalizeHoverDocumentation } from './hoverDocumentation.js'
import { buildQuickDocumentation } from './quickDocumentation.js'
import { usefulAttributes } from './compactSymbolHover.js'
import { documentationTranslationPresentation } from './symbolHoverDocumentation.js'
import { BUSINESS_SOURCE_LABEL, displayBusiness } from './businessMeaningDisplay.js'

const parser = new Marked({ gfm: true, async: false })
const ownNote = /^\*\*(?:文档中文翻译|中文说明)：\*\*/
const shortcut = /^Ctrl\+Q 查看完整说明或原文。$/
const namespace = /^(?:com|org|net|io|[\w$]+(?:\.[\w$]+)+)$/
const validRows = rows => (Array.isArray(rows) ? rows : []).filter(row => row
  && ['code', 'markdown', 'plaintext'].includes(row.kind) && typeof row.value === 'string' && row.value.length)
  .map(row => ({ ...row }))
const sameSection = (left, right) => !!right && left.kind === right.kind && (left.language || '') === (right.language || '') && left.value === right.value
const compactText = value => String(value ?? '').replace(/\s+/g, ' ').trim()
const canonical = value => compactText(value).replace(/[。.!！?？;；,，:：]+$/u, '')
const chineseProse = value => {
  const content = typeof value === 'string' ? value : ''
  return content && (/\p{Script=Han}/u.test(content) || !/[a-z]/i.test(content)) ? content : ''
}
function plain(tokens) {
  return (tokens || []).map(token => token.tokens ? plain(token.tokens)
    : token.type === 'br' ? '\n' : typeof token.text === 'string' ? token.text : '').join('')
}
function paragraphs(value) {
  return parser.lexer(value).filter(token => token.type !== 'space')
}
function generatedHeader(value) {
  return /^`([^`\r\n]+)`\s*·\s*\*\*([^*\r\n]+)\*\*$/.exec(value.trim())
}
function legacyFacts(section) {
  if (section?.kind !== 'markdown') return null
  const blocks = paragraphs(section.value)
  const header = generatedHeader(blocks[0]?.raw || '')
  const labels = blocks.filter(block => block.type === 'paragraph').map(block => plain(block.tokens))
  const businessLabel = /^(?:业务信息|功能描述(?:（待确认）)?)：\s*([\s\S]*)$/
  if (!header && !labels.some(line => /^(?:识别依据|作用依据)：/.test(line) || businessLabel.test(line))) return null
  if (blocks.some(block => block.type !== 'paragraph' || !generatedHeader(block.raw || '')
    && !/^(?:名称|语言|类别|作用|返回类型|声明类型|类型|属性|识别依据|作用依据|功能依据|业务信息|功能描述(?:（待确认）)?|文档中文翻译|中文说明)：/.test(plain(block.tokens)))) return null
  const facts = header ? { name: header[1], category: header[2] } : {}
  for (const line of labels) {
    const meaning = businessLabel.exec(line)
    if (meaning) {
      facts.businessMeaning = /^(?:待自动补充|代码已变化|当前符号尚未与项目索引绑定|暂时无法读取|读取超时)/.test(meaning[1]) ? '' : meaning[1]
      facts.businessCandidate = /（待确认）/.test(line)
    }
    const match = /^(名称|类别|作用|返回类型|声明类型|类型|属性)：\s*([\s\S]*)$/.exec(line)
    if (!match) continue
    if (match[1] === '名称') facts.name = match[2]
    if (match[1] === '类别') facts.category = match[2].replace(/（语言服务[^）]*）$/, '')
    if (match[1] === '作用') facts.purpose = match[2]
    if (['返回类型', '声明类型', '类型'].includes(match[1])) { facts.type = match[2]; facts.typeLabel = match[1] }
    if (match[1] === '属性') facts.attributes = match[2].split('、')
  }
  return facts
}
function rawServerRows(document, base) {
  return base.filter(section => !sameSection(section, document.tokenSection) && !sameSection(section, document.symbolSection)
    && !sameSection(section, document.businessSection) && !legacyFacts(section)
    && !(section.kind === 'markdown' && paragraphs(section.value).length === 1
      && (ownNote.test(section.value.trim()) || shortcut.test(section.value.trim()))))
}

// Keep raw code.value byte-for-byte. A fenced declaration's framing newline is
// excluded, but its internal CRLF/spacing is retained instead of lexer-normalized.
function javaFieldDeclaration(section, symbol) {
  if (section.kind !== 'code' || String(section.language).toLowerCase() !== 'java'
    || !['类常量', '成员变量', '静态变量', '只读成员变量', '字段'].includes(symbol.category)
    || typeof symbol.name !== 'string' || !/^[\p{L}_$][\p{L}\p{N}_$]*$/u.test(symbol.name)) return null
  const name = symbol.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const signature = section.value.replace(/^(?:(?:\s*@(?:[\p{L}_$][\p{L}\p{N}_$]*\.)*[\p{L}_$][\p{L}\p{N}_$]*)\s*)+/u, '')
    .replace(/^(?:\s*(?:public|protected|private|static|final|transient|volatile)\s+)+/, '').trimStart()
  const match = new RegExp(`^([\\p{L}\\p{N}_$.[\\]<>?, &]+?)\\s+((?:[\\p{L}_$][\\p{L}\\p{N}_$]*\\.)+)${name}(?=\\s*(?:[=;]|$))`, 'u').exec(signature)
  if (!match || /^(?:return|new|throw|package|import|class|interface|record|enum|void)\b/.test(match[1])) return null
  // This is a constrained declaration check, not a type checker. In particular,
  // plain prose such as "This member package.Type.name" is never a Java type.
  let depth = 0, outside = ''
  for (const character of match[1]) {
    if (character === '<') depth++
    else if (character === '>') { if (--depth < 0) return null }
    else if (!depth) outside += character
  }
  if (depth || /[\p{L}\p{N}_$]\s+[\p{L}\p{N}_$]/u.test(outside)
    || !/^[\p{L}_$][\p{L}\p{N}_$]*(?:\s*\.\s*[\p{L}_$][\p{L}\p{N}_$]*)*(?:\s*\[\s*\])*\s*$/u.test(outside)) return null
  return { declaration: section.value, language: 'java', owner: match[2].slice(0, -1), remainder: '', section }
}
function declarationOf(section, symbol) {
  if (section.kind === 'plaintext') return null
  const metadata = buildQuickDocumentation({ sections: [section] })
  if (!metadata.declaration) return javaFieldDeclaration(section, symbol)
  if (section.kind === 'code') return { ...metadata, declaration: section.value, remainder: '', section }
  const match = /^(?:[ \t]*\r?\n)*(?:[ \t]*([\w$]+(?:\.[\w$]+)+)[ \t]*\r?\n(?:[ \t]*\r?\n)*)?[ \t]*(`{3,}|~{3,})([^\r\n]*)\r?\n([\s\S]*?)\r?\n[ \t]*\2[ \t]*(?:\r?\n|$)/.exec(section.value)
  if (!match) return null
  return { ...metadata, declaration: match[4], owner: metadata.owner || match[1] || '',
    remainder: section.value.slice(match[0].length), section }
}
function sourceLine(value) {
  if (/[\r\n]/.test(value)) return null
  const match = /^(来源|Source|Defined in|定义位置|识别依据|作用依据|功能依据|文档中文翻译|中文说明)\s*[:：]\s*([\s\S]*)$/i.exec(value)
  if (!match) return null
  const label = ({ source: '来源', 'defined in': '定义位置', '文档中文翻译': '翻译', '中文说明': '翻译' })[match[1].toLowerCase()] || match[1]
  return { label, value: match[2] }
}
function isContextSection(section) {
  if (section.kind === 'code') return namespace.test(section.value.trim())
  const blocks = section.kind === 'markdown' ? paragraphs(section.value) : [{ type: 'paragraph', text: section.value }]
  return blocks.length && blocks.every(block => block.type === 'paragraph' && (sourceLine(plain(block.tokens) || block.text)
    || namespace.test((plain(block.tokens) || block.text || '').trim())))
}
function leadingDeclaration(rows, symbol) {
  for (const section of rows) {
    if (isContextSection(section)) continue
    return declarationOf(section, symbol)
  }
  return null
}
function sectionKey(section) { return `${section.kind}\0${section.language || ''}\0${section.value}` }
function repeatsTruncatedPurpose(purpose, rows, declaration) {
  if (!purpose.endsWith('…')) return false
  const prefix = purpose.slice(0, -1)
  if (!prefix) return false
  for (const section of rows) {
    if (isContextSection(section)) continue
    const current = section === declaration?.section
      ? declaration.remainder ? { ...section, value: declaration.remainder } : null : section
    if (!current || current.kind !== 'markdown') continue
    for (const block of paragraphs(current.value)) {
      if (block.type !== 'paragraph') continue
      const content = compactText(plain(block.tokens))
      if (sourceLine(content) || namespace.test(content) || shortcut.test(content)) continue
      return content.length > prefix.length && content.startsWith(prefix)
    }
  }
  return false
}
function literal(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/[\\`*_{}\[\]()#+\-.!<>|~=:]/g, character => `&#${character.codePointAt(0)};`)
}

export function buildFullSymbolDocumentation(document, { original = false } = {}) {
  document = document && typeof document === 'object' ? document : {}
  const base = validRows(document.businessBaseSections || document.sections)
  const legacy = Object.assign({}, ...base.map(legacyFacts).filter(Boolean))
  const symbol = { ...legacy, ...(document.symbol && typeof document.symbol === 'object' ? document.symbol : {}) }
  const purpose = chineseProse(symbol.purpose)
  const business = document.business?.business && displayBusiness(document.business.business)
  const businessMeaning = business ? !['missing', 'stale'].includes(business.status) ? chineseProse(business.business_meaning) : '' : chineseProse(legacy.businessMeaning)
  const businessCandidate = !!businessMeaning && (business ? business.status === 'unreviewed' : !!legacy.businessCandidate)
  const serverRows = rawServerRows(document, base)
  const storedOriginal = validRows(document.originalSections)
  const restoredOriginal = normalizeHoverDocumentation(document._code_agent_original?.contents)
  const originalRows = storedOriginal.length ? storedOriginal : restoredOriginal.length ? restoredOriginal : serverRows
  const declaration = leadingDeclaration(serverRows, symbol)
  const actualDeclaration = original ? leadingDeclaration(rawServerRows(document, originalRows), symbol) || declaration : declaration
  const links = buildQuickDocumentation({ sections: original ? originalRows : [...base, ...validRows([document.businessSection])] }).links
  const provenance = [], provenanceSeen = new Set()
  function remember(label, value) {
    value = compactText(value)
    const key = `${label}\0${value}`
    if (value && !provenanceSeen.has(key)) { provenanceSeen.add(key); provenance.push({ label, value }) }
  }
  const translationState = documentationTranslationPresentation(document,
    { missing: !purpose && !!(document.untranslated || symbol.purpose && !chineseProse(symbol.purpose)) })
  for (const section of base) {
    if (section.kind !== 'markdown') continue
    for (const block of paragraphs(section.value)) {
      if (block.type !== 'paragraph') continue
      const line = sourceLine(plain(block.tokens))
      if (line) remember(line.label, line.value)
    }
  }
  if (document.source) remember('来源', document.source)
  if (document.businessReadError) remember('项目说明状态', document.businessReadError)
  if (businessMeaning && business && BUSINESS_SOURCE_LABEL[business.source]) remember('功能来源', BUSINESS_SOURCE_LABEL[business.source])
  const metadata = [], type = symbol.returnType || symbol.type
  if (type) metadata.push({ label: symbol.returnType || symbol.typeLabel === '返回类型' ? '返回类型' : '类型', value: String(type), code: true })
  const attributes = usefulAttributes(symbol)
  if (attributes.length) metadata.push({ label: '属性', value: attributes.join(' · ') })
  const output = { title: symbol.name || document.title || '完整说明', category: symbol.category || '',
    purpose: original || repeatsTruncatedPurpose(purpose, serverRows, declaration) ? '' : purpose,
    businessMeaning: original || canonical(businessMeaning) === canonical(purpose) ? '' : businessMeaning,
    businessCandidate, metadata: original ? [] : metadata, declaration: actualDeclaration?.declaration || '',
    language: actualDeclaration?.language || 'plaintext', owner: actualDeclaration?.owner || '', links,
    sections: [], provenance, translationState: original ? null : translationState,
    translationNotice: original ? '' : translationState.message, originalAvailable: originalRows.length > 0 }
  if (original) {
    output.sections = originalRows.map((section, index) => ({ ...section,
      declaration: section === actualDeclaration?.section, heading: index === 0 ? '语言服务原文' : '' }))
    return output
  }
  const seen = new Set()
  let contentContext = ''
  function append(section) {
    if (!section.value || !section.value.trim()) return
    const key = sectionKey(section) + (section.kind === 'code' ? `\0${contentContext}` : '')
    if (!seen.has(key)) { seen.add(key); output.sections.push(section) }
    if (section.kind !== 'code') contentContext = section.value
  }
  if (declaration) append({ kind: 'code', language: declaration.language, value: declaration.declaration, declaration: true, heading: '声明签名' })
  for (const section of serverRows) {
    let current = section
    if (section !== declaration?.section && declaration?.section.kind === 'markdown'
      && sameSection(section, declaration.section)) continue
    if (section === declaration?.section) {
      if (!declaration.remainder) continue
      current = { ...section, value: declaration.remainder }
    }
    if (current.kind === 'code') {
      if (namespace.test(current.value.trim())) { remember('命名空间', current.value); continue }
      append(current); continue
    }
    if (current.kind === 'plaintext') {
      const source = sourceLine(current.value.trim())
      if (source) { remember(source.label, source.value); continue }
      if (namespace.test(current.value.trim())) { remember('命名空间', current.value); continue }
      if (canonical(current.value) === canonical(purpose) || businessMeaning && canonical(current.value) === canonical(businessMeaning)) continue
      append(current); continue
    }
    const kept = parser.lexer(current.value).filter(block => {
      if (block.type !== 'paragraph') return true
      const content = plain(block.tokens).trim()
      if (shortcut.test(content)) return false
      const source = sourceLine(content)
      if (source) { remember(source.label, source.value); return false }
      if (namespace.test(content)) { remember('命名空间', content); return false }
      let hasReference = false
      parser.walkTokens([block], token => { if (['link', 'image'].includes(token.type)) hasReference = true })
      return hasReference || !(purpose && canonical(content) === canonical(purpose)
        || businessMeaning && canonical(content) === canonical(businessMeaning))
    }).map(block => block.raw || '').join('')
    append({ ...current, value: kept })
  }
  if (businessMeaning && business) {
    const details = []
    if (business.detail && canonical(business.detail) !== canonical(businessMeaning)) details.push(literal(business.detail))
    for (const [key, label] of [['inputs', '输入'], ['outputs', '输出'], ['rules', '功能规则'], ['effects', '副作用'], ['feature_refs', '所属功能']]) {
      const items = Array.isArray(business[key]) ? business[key] : []
      if (items.length) details.push(`**${label}**\n${items.map(item => `- ${typeof item === 'object' && item ? `${literal(item.name)}：${literal(item.meaning)}` : literal(item)}`).join('\n')}`)
    }
    if (details.length) append({ kind: 'markdown', value: details.join('\n\n'), heading: businessCandidate ? '功能详情（待确认）' : '功能详情' })
  }
  return output
}
