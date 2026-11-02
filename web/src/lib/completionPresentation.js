import { Marked } from 'marked'
import { hoverCodeBlock, hoverMarkdownStrings, normalizeHoverDocumentation } from './hoverDocumentation.js'
import { buildFullSymbolDocumentation } from './fullSymbolDocumentation.js'

const parser = new Marked({ gfm: true, async: false })
const kinds = ['文本', '方法', '函数', '构造器', '成员变量', '变量', '类', '接口', '模块', '属性', '单位', '值', '枚举', '关键字', '代码片段', '颜色', '文件', '引用', '文件夹', '枚举常量', '常量', '结构体', '事件', '运算符', '类型参数']
const literal = value => String(value ?? '').replace(/&/g, '&amp;').replace(/[\\`*_{}\[\]()#+\-.!<>|~=:]/g, character => `&#${character.codePointAt(0)};`)
const hasChinese = value => /\p{Script=Han}/u.test(value)
const visible = tokens => (tokens || []).map(token => token.tokens ? visible(token.tokens) : token.type === 'br' ? '\n' : token.text || '').join('')
const metadata = value => /^(?:来源|Source|Defined in|定义位置|识别依据|作用依据|功能依据|文档中文翻译|中文说明)\s*[:：]/i.test(value)
  || /^Ctrl\+Q 查看完整说明或原文。$/.test(value) || /^(?:com|org|net|io)(?:\.[\w$]+)*$/.test(value)
const canonicalCode = value => String(value).replace(/\r\n|\r/g, '\n').trim()
const inlineCodeOnly = tokens => tokens?.length && tokens.every(token => token.type === 'codespan' || token.type === 'text' && !token.text.trim())

function proseDetail(value) {
  if (typeof value === 'string' && /^(?:\.{0,2}[\\/])?[\p{L}\p{N}_$-]+(?:[.\\/:][\p{L}\p{N}_$-]+)+(?:\.\*)?$/u.test(value.trim())) return false
  if (!value || /^(?:(?:public|protected|private|static|final|abstract|readonly|export|declare|async|const)\s+|(?:class|interface|struct|enum|def|function|fn|type)\b)/.test(value)
    || /[`{};<>\[\]]|->|=>|::|\w\s*\(/.test(value)) return false
  return hasChinese(value) ? /[。！？]|^(?:返回|创建|表示|获取|设置|用于|提供|读取|保存|检查)/.test(value)
    : /^(?:Returns?|Creates?|Represents?|Gets?|Sets?|Provides?|Contains?|Computes?|Converts?|Checks?|The|This|Use|Whether)\b/i.test(value)
      || (value.match(/\b[a-z]{2,}\b/g) || []).length >= 3
}

function readingContents(documentation) {
  let untranslated = false
  const sections = normalizeHoverDocumentation(documentation).flatMap(section => {
    if (section.kind === 'code') return [section]
    if (section.kind === 'plaintext') {
      if (metadata(section.value.trim())) return []
      if (hasChinese(section.value) || !/[a-z]/i.test(section.value)) return [section]
      untranslated = true; return []
    }
    const kept = parser.lexer(section.value).filter(block => {
      if (block.type === 'space' || block.type === 'code') return true
      const content = visible(block.tokens).trim() || block.text || ''
      if (block.type === 'paragraph' && metadata(content)) return false
      if (hasChinese(block.raw) || !/[a-z]/i.test(block.raw) || block.type === 'paragraph' && inlineCodeOnly(block.tokens)) return true
      untranslated = true; return false
    }).map(block => block.raw || '').join('')
    return kept.trim() ? [{ ...section, value: kept }] : []
  })
  return { sections, untranslated }
}

function firstPurpose(sections) {
  for (const section of sections) {
    if (section.kind === 'code') continue
    if (section.kind === 'plaintext') return hasChinese(section.value) ? section.value : ''
    const first = parser.lexer(section.value).find(block => !['space', 'code'].includes(block.type))
    if (!first) continue
    if (first.type !== 'paragraph' || first.tokens?.some(token => !['text', 'escape'].includes(token.type))) return ''
    return hasChinese(first.text) ? first.text : ''
  }
  return ''
}

// Display fields only. The caller retains the unmodified server item for
// resolve, edits, snippets, sorting, completion defaults and context tickets.
export function completionPresentation(item, language = 'plaintext') {
  const name = String(item.label || '').replace(/[\r\n]+/g, ' ')
  const label = name.length > 140 ? name.slice(0, 140) + '…' : name
  const detail = typeof item.detail === 'string' ? item.detail : ''
  const isProse = proseDetail(detail), codeDetail = !isProse && detail.trim() ? detail : ''
  const projection = readingContents(item.documentation)
  const purpose = firstPurpose(projection.sections) || (isProse && hasChinese(detail) ? detail : '')
  const reading = buildFullSymbolDocumentation({ symbol: { name, language, category: kinds[item.kind - 1] || '补全建议', purpose }, sections: projection.sections })
  const body = []
  if (reading.purpose) body.push(`**作用**　${literal(reading.purpose)}`)
  if (codeDetail) body.push(hoverCodeBlock(codeDetail, language))
  for (const section of reading.sections) {
    if (section.kind === 'code' && codeDetail && canonicalCode(section.value) === canonicalCode(codeDetail)) continue
    if (section.kind === 'plaintext') body.push(literal(section.value).replace(/\r\n|\r|\n/g, '  \n'))
    else body.push(section.kind === 'code' ? hoverCodeBlock(section.value, section.language || language) : section.value)
  }
  const untranslated = projection.untranslated || isProse && !hasChinese(detail)
  const status = item.documentation_translation?.status
  const notice = status === 'failed' ? '中文说明翻译失败'
    : status === 'pending' ? item.documentation_translation?.refresh_required ? '正在翻译中文说明，稍后重新补全查看' : '正在翻译中文说明'
      : untranslated && !body.some(hasChinese) ? '中文说明尚未翻译' : ''
  if (!body.length && !notice) return { detail: isProse ? '' : detail, documentation: undefined }
  const heading = `### \`${label.replace(/`/g, 'ˋ')}\` · ${kinds[item.kind - 1] || '补全建议'}`
  const markdown = [heading, ...body, ...(notice ? [`> ${notice}`] : [])].join('\n\n')
  return { detail: isProse ? '' : detail, documentation: hoverMarkdownStrings([{ kind: 'markdown', value: markdown }])[0] }
}

export function completionLabel(item) {
  if (!item.labelDetails) return item.label || item.insertText || ''
  const extra = item.labelDetails
  const duplicate = value => typeof value === 'string' && value.trim() === String(item.label || '').trim()
  return { label: item.label || '', detail: duplicate(extra.detail) || proseDetail(extra.detail) && !hasChinese(extra.detail) ? undefined : extra.detail,
    description: duplicate(extra.description) || proseDetail(extra.description) && !hasChinese(extra.description) ? undefined : extra.description }
}
