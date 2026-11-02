// The editor hover is a reading preview. Full documentation, provenance and
// original server receipts remain on the document for Ctrl+Q and refreshes.
import { hoverMarkdownStrings, hoverCodeBlock } from './hoverDocumentation.js'
import { buildQuickDocumentation } from './quickDocumentation.js'
import { displayBusiness } from './businessMeaningDisplay.js'
import { documentationTranslationPresentation } from './symbolHoverDocumentation.js'

const text = (value, limit = 200) => {
  const characters = Array.from(String(value ?? '').replace(/\s+/g, ' ').trim())
  return characters.slice(0, limit).join('') + (characters.length > limit ? '…' : '')
}
const literal = value => String(value).replace(/&/g, '&amp;')
  .replace(/[\\`*_{}\[\]()#+\-.!<>|~=:]/g, character => `&#${character.codePointAt(0)};`)
function code(value) {
  const content = text(value, 240)
  const fence = '`'.repeat(1 + (content.match(/`+/g) || []).reduce((max, run) => Math.max(max, run.length), 0))
  const pad = /^[` ]|[` ]$/.test(content) ? ' ' : ''
  return `${fence}${pad}${content}${pad}${fence}`
}
const prose = value => {
  const compact = text(value)
  // Chinese previews never turn unready English prose into an invented summary.
  return compact && (/\p{Script=Han}/u.test(compact) || !/[a-z]/i.test(compact)) ? compact : ''
}
const identityOnly = new Set(['声明位置', '定义位置', '导入位置', '文档注释', '标准库符号', '泛型', '类型实参', '写入位置'])
export function usefulAttributes(symbol) {
  const category = symbol.category || ''
  return [...new Set((Array.isArray(symbol.attributes) ? symbol.attributes : []).filter(item => typeof item === 'string' && !identityOnly.has(item)
    && !(item === '静态' && /类常量|静态/.test(category))
    && !(['final', '只读'].includes(item) && /类常量|只读/.test(category)))
    .map(item => ({ 'public（公开）': '公开', 'private（私有）': '私有', 'protected（受保护）': '受保护' })[item] || item))]
}

function leadingDeclaration(document) {
  const base = document.businessBaseSections || document.sections || []
  for (const section of base) {
    if (!section || typeof section.value !== 'string') continue
    if (section.value === document.tokenSection?.value || section.value === document.symbolSection?.value) continue
    const value = section.value.trim()
    if (/^\*\*(?:文档中文翻译|中文说明)：\*\*/.test(value) || /^Ctrl\+Q 查看完整说明或原文。$/.test(value)) continue
    // A standalone package/owner is navigation context, not a declaration.
    if (/^[\w$]+(?:\.[\w$]+)*$/.test(value)) continue
    // Only the leading server section may describe this symbol. Once prose has
    // begun, later code examples must not be promoted to its declaration.
    if (value.length > 8192) return {}
    return buildQuickDocumentation({ sections: [section] })
  }
  return {}
}

export function compactSymbolHover(document) {
  if (!document?.symbol) return document
  const symbol = document.symbol, category = text(symbol.category, 64), name = text(symbol.name, 240)
  const heading = [name ? code(name) : '代码说明', category ? literal(category) : ''].filter(Boolean).join(' · ')
  const paragraphs = [`### ${heading}`]
  const purpose = prose(symbol.purpose)
  if (purpose) paragraphs.push(`**作用**　${literal(purpose)}`)
  const business = document.business?.business && displayBusiness(document.business.business)
  const meaning = business && !['missing', 'stale'].includes(business.status) && prose(business.business_meaning)
  if (meaning && meaning.replace(/[\s\p{P}]/gu, '') !== purpose.replace(/[\s\p{P}]/gu, '')) {
    paragraphs.push(`**功能${business.status === 'unreviewed' ? '（待确认）' : ''}**　${literal(meaning)}`)
  }
  const metadata = []
  const type = text(symbol.returnType || symbol.type, 240)
  if (type) metadata.push([symbol.returnType || symbol.typeLabel === '返回类型' ? '返回类型' : '类型', code(type).replace(/\|/g, '\\|')])
  const attributes = usefulAttributes(symbol)
  if (attributes.length) metadata.push(['属性', attributes.map(item => literal(text(item, 64))).join(' · ')])
  if (metadata.length) paragraphs.push(`| | |\n| :--- | :--- |\n${metadata.map(([key, value]) => `| ${key} | ${value} |`).join('\n')}`)
  const details = leadingDeclaration(document)
  if (details.declaration) {
    const lines = details.declaration.trim().split(/\r\n|\r|\n/)
    const snippet = lines.slice(0, 5).join('\n')
    const characters = Array.from(snippet)
    const shortened = characters.length > 560 || lines.length > 5
    paragraphs.push(hoverCodeBlock(characters.slice(0, 560).join('') + (shortened ? '\n…' : ''), details.language))
  }
  const translation = documentationTranslationPresentation(document,
    { missing: !purpose && !!(document.untranslated || symbol.purpose && !prose(symbol.purpose)) })
  const notice = translation.status === 'pending' ? '正在翻译中文说明，完成后自动更新'
    : translation.status === 'failed' ? '翻译失败，按 Ctrl+Q 可重试'
      : translation.status === 'untranslated' ? '尚未翻译，按 Ctrl+Q 可翻译' : ''
  paragraphs.push(`> ${notice ? literal(notice) + ' · ' : ''}完整说明 ${code('Ctrl+Q')}`)
  return { ...document, contents: hoverMarkdownStrings([{ kind: 'markdown', value: paragraphs.join('\n\n') }]) }
}
