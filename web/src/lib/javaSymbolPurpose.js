// A purpose must come from documentation attached to the actual declaration,
// or from the language server's documentation for the hovered symbol. Names
// alone are never evidence of business behavior.
import { Marked } from 'marked'
import { normalizeHoverDocumentation } from './hoverDocumentation.js'

const parser = new Marked({ gfm: true, async: false })
const MAX_PURPOSE_LENGTH = 360
const MAX_COMMENT_DISTANCE = 16000
const declarationCategories = new Set(['类', '接口', '枚举', '记录类', '注解类型', '方法', '构造器', '注解属性', '字段', '变量', '局部变量'])
const variableCategories = new Set(['参数', '局部变量', '字段', '记录组件', '变量'])
const symbolCategories = new Set([...declarationCategories, '参数', '记录组件', '类型引用', '类型', '方法调用', '构造器调用', '枚举常量', '类型参数', '注解', '标识符（语义尚未解析）'])
const caches = new WeakMap()

function decodeEntities(value) {
  const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }
  return value.replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (entity, name) => {
    if (name[0] !== '#') return named[name.toLowerCase()] ?? entity
    const point = name[1]?.toLowerCase() === 'x' ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10)
    return point > 0 && point <= 0x10ffff && !(point >= 0xd800 && point <= 0xdfff) ? String.fromCodePoint(point) : ''
  })
}

function inlineText(tokens) {
  return (tokens || []).map(token => {
    if (token.type === 'image' || token.type === 'html') return ''
    if (Array.isArray(token.tokens)) return inlineText(token.tokens)
    if (token.type === 'br') return ' '
    return typeof token.text === 'string' ? token.text : ''
  }).join('')
}

function plainText(value) {
  // Javadoc inline references supply their display label, without generating a
  // link. HTML and Markdown media/actions never become part of this summary.
  const source = value.replace(/\{@(?:link|linkplain)\s+(\S+)(?:\s+([^}]*))?\}/g, (_, target, label) => label?.trim() || target)
    .replace(/\{@(?:code|literal)\s+([^}]*)\}/g, (_, code) => code.replace(/</g, '＜').replace(/>/g, '＞'))
    .replace(/<(script|style|iframe|object)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '')
    .replace(/<[^>]+>/g, '')
  const paragraph = parser.lexer(source).find(token => token.type === 'paragraph')
  const text = inlineText(paragraph?.tokens || [])
  // Decode only after stripping source tags. Re-escape angle brackets so an
  // entity-encoded tag cannot turn into HTML when the summary is merged.
  return decodeEntities(text).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '')
    .replace(/\s+/g, ' ').trim().replace(/</g, '＜').replace(/>/g, '＞')
}

function bounded(value) {
  const text = plainText(value)
  if (!text) return null
  const characters = Array.from(text)
  return characters.length > MAX_PURPOSE_LENGTH ? characters.slice(0, MAX_PURPOSE_LENGTH - 1).join('') + '…' : text
}

function commentBody(comment) {
  return comment.word.replace(/^\/\*\*?/, '').replace(/\*\/$/, '')
    .split(/\r\n|\r|\n/).map(line => line.replace(/^\s*\* ?/, '').replace(/^\s*\/\/ ?/, '')).join('\n').trim()
}

function nearestComment(index, token) {
  const all = index.allTokens || []
  let low = 0, high = all.length
  while (low < high) {
    const middle = (low + high) >> 1
    if (all[middle].start < token.start) low = middle + 1
    else high = middle
  }
  for (let i = low - 1; i >= 0 && token.start - all[i].end <= MAX_COMMENT_DISTANCE; i--) {
    if (all[i].kind === 'documentation' || all[i].kind === 'comment') return all[i]
  }
  return null
}

function isAttached(index, comment, declarationIndex, category) {
  const target = index.tokens[declarationIndex]
  // A trailing comment documents the preceding statement, not the next one.
  const lineStart = Math.max(index.text.lastIndexOf('\n', comment.start - 1), index.text.lastIndexOf('\r', comment.start - 1)) + 1
  if (comment.kind !== 'documentation' && index.text.slice(lineStart, comment.start).trim()) return false
  const between = index.text.slice(comment.end, target.start)
  if (/\n\s*\n\s*\n/.test(between.replace(/\r\n|\r/g, '\n'))) return false
  const prefix = []
  let start = declarationIndex
  while (start > 0 && index.tokens[start - 1].start >= comment.end) start--
  let angles = 0
  for (let i = start; i < declarationIndex; i++) {
    const token = index.tokens[i], word = token.word
    if (index.roles?.get(i)?.declaration) return false
    if (word === '@' && index.tokens[i + 1]?.word !== 'interface') {
      let j = i + 1
      if (index.tokens[j]?.kind !== 'identifier') return false
      while (index.tokens[j + 1]?.word === '.' && index.tokens[j + 2]?.kind === 'identifier') j += 2
      if (index.tokens[j + 1]?.word === '(') {
        const end = index.pairs?.get(j + 1)
        if (!Number.isInteger(end) || end >= declarationIndex) return false
        j = end
      }
      i = j
      continue
    }
    if (word === '<') angles++
    if (word === '>') angles--
    if (angles < 0 || [';', '{', '}', '=', '->', '(', ')', ':'].includes(word) || (word === ',' && !angles)) return false
    if (token.kind !== 'identifier' && !['.', '[', ']', '<', '>', ',', '?', '&', '@'].includes(word)) return false
    prefix.push(word)
  }
  if (angles) return false
  const typeWord = ({ 类: 'class', 接口: 'interface', 枚举: 'enum', 记录类: 'record', 注解类型: 'interface' })[category]
  return typeWord ? prefix.at(-1) === typeWord : category === '构造器' || prefix.length > 0
}

function attachedDocumentation(index, declarationIndex) {
  if (!Number.isInteger(declarationIndex) || declarationIndex < 0) return null
  const token = index.tokens?.[declarationIndex], role = index.roles?.get(declarationIndex)
  if (!token || !role?.declaration || !declarationCategories.has(role.category)) return null
  let cache = caches.get(index)
  if (!cache) { cache = new Map(); caches.set(index, cache) }
  if (cache.has(declarationIndex)) return cache.get(declarationIndex)
  const comment = nearestComment(index, token)
  let document = null
  if (comment && isAttached(index, comment, declarationIndex, role.category)) {
    const body = commentBody(comment)
    if (comment.kind === 'documentation') document = { body, source: '本文件 Javadoc' }
    else {
      const purpose = body.match(/^(?:作用|用途|功能|purpose)\s*[:：]\s*([\s\S]+)$/i)
      if (purpose) document = { body: purpose[1], source: '本文件说明注释' }
    }
  }
  cache.set(declarationIndex, document)
  return document
}

function parameterPurpose(index, declarationIndex, token, role) {
  const parameter = index.parameterAt?.[declarationIndex]
  if (!parameter || parameter.category !== role.category) return null
  const ownerIndex = parameter.start - 1
  const ownerRole = index.roles?.get(ownerIndex)
  const ownerCategories = role.category === '记录组件' ? ['记录类'] : ['方法', '构造器', '注解属性']
  if (!ownerRole?.declaration || !ownerCategories.includes(ownerRole.category)) return null
  const document = attachedDocumentation(index, ownerIndex)
  if (!document || document.source !== '本文件 Javadoc') return null
  const name = index.tokens[declarationIndex]?.word
  if (!name || name !== token.word) return null
  const lines = document.body.split(/\r\n|\r|\n/)
  let value = null
  for (const line of lines) {
    const param = line.match(/^\s*@param\s+(\S+)\s*(.*)$/)
    if (param) {
      if (value !== null) break
      if (param[1] === name) value = param[2]
    } else if (/^\s*@\w+/.test(line)) {
      if (value !== null) break
    } else if (value !== null) value += '\n' + line
  }
  const text = value === null ? null : bounded(value)
  return text ? { text, source: document.source + ' @param' } : null
}

function localPurpose(index, token, role) {
  const declarationIndex = role?.declarationTokenIndex
  if (!index || !Number.isInteger(declarationIndex) || index.tokens?.[declarationIndex]?.word !== token.word) return null
  const declarationRole = index.roles?.get(declarationIndex)
  // A server's generic "variable" category can describe a parameter or field.
  // Use the proven declaration category only to read its documentation, keeping
  // the caller's authoritative display category intact.
  const boundRole = role.category === '变量' && variableCategories.has(declarationRole?.category)
    ? { ...role, category: declarationRole.category } : role
  if (declarationRole?.category !== boundRole.category) return null
  if (boundRole.category === '参数' || boundRole.category === '记录组件') return parameterPurpose(index, declarationIndex, token, boundRole)
  const document = attachedDocumentation(index, declarationIndex)
  if (!document) return null
  const description = document.body.split(/^\s*@\w+/m)[0]
  const text = bounded(description)
  return text ? { text, source: document.source } : null
}

function metadataOrSignature(line) {
  const value = plainText(line)
  if (!value) return true
  if (/^(?:source|来源|出处|since|自|author|作者|version|版本|see also|see|specified by|overrides)\s*[:：]/i.test(value)) return true
  if (/^(?:import|package)\s+|^@(?:[\p{L}\p{N}\p{Sc}\p{Pc}.]+)(?:\s|\(|$)/u.test(value)) return true
  if (/^(?:(?:public|protected|private|static|abstract|final|native|synchronized|strictfp|default|sealed|non-sealed)\s+)+/.test(value)) return true
  if (/^(?:class|interface|enum|record|@interface)\s+/.test(value)) return true
  if (/^[\p{L}\p{Nl}\p{Sc}\p{Pc}][\p{L}\p{Nl}\p{Sc}\p{Pc}\p{Mn}\p{Mc}\p{Nd}\p{Cf}]*(?:\.[\p{L}\p{Nl}\p{Sc}\p{Pc}][\p{L}\p{Nl}\p{Sc}\p{Pc}\p{Mn}\p{Mc}\p{Nd}\p{Cf}]*)*(?:\(\))?\s*;?$/u.test(value)) return true
  if (/^[\p{L}\p{N}\p{Sc}\p{Pc}.\[\]＜＞?,]+\s+[\p{L}\p{N}\p{Sc}\p{Pc}.]+(?:\([^)]*\))?\s*;?$/u.test(value)) return true
  if (/^[\p{L}\p{N}\p{Sc}\p{Pc}.＜＞?\[\], ]+\([^)]*\)(?:\s+throws\s+[\p{L}\p{N}\p{Sc}\p{Pc}., ]+)?\s*;?$/u.test(value)) return true
  if (/^(?:[\p{L}\p{N}\p{Sc}\p{Pc}.＜＞?\[\],]+\s+)+[\p{L}\p{N}\p{Sc}\p{Pc}]+\s*=/u.test(value) || /^[{}]|[;{}]\s*$/.test(value)) return true
  if (/^[\p{L}\p{N}\p{Sc}\p{Pc}./\\: -]+\.(?:jar|class|java|zip)$/iu.test(value)) return true
  return false
}

export function isChineseDocumentationTranslation(value) {
  return value?.language === 'zh-CN' && value?.source === 'configured-model'
}

function languageServerPurpose(contents, documentationTranslation) {
  for (const section of normalizeHoverDocumentation(contents)) {
    if (section.kind === 'code') continue
    const candidates = parser.lexer(section.value).filter(token => token.type === 'paragraph').map(token => token.text)
    for (const candidate of candidates) {
      const lines = candidate.split(/\r\n|\r|\n/).filter(line => !metadataOrSignature(line))
      const text = bounded(lines.join(' '))
      if (text) return { text, source: '语言服务器文档',
        ...(isChineseDocumentationTranslation(documentationTranslation)
          ? { translation: { language: 'zh-CN', source: 'configured-model' } } : {}) }
    }
  }
  return null
}

export function resolveJavaSymbolPurpose({ index, token, role, hoverContents, documentationTranslation } = {}) {
  if (!token || token.kind !== 'identifier' || !symbolCategories.has(role?.category)) return null
  const local = localPurpose(index, token, role)
  const englishLocal = local && /[A-Za-z]{2,}/.test(local.text) && !/\p{Script=Han}/u.test(local.text)
  // Author-written Chinese keeps its priority. An attached English comment can
  // otherwise mask the translated documentation for this exact hovered symbol.
  if (local && (!englishLocal || !isChineseDocumentationTranslation(documentationTranslation))) return local
  const server = languageServerPurpose(hoverContents, documentationTranslation)
  if (englishLocal && !/\p{Script=Han}/u.test(server?.text || '')) return local
  return server || local
}
