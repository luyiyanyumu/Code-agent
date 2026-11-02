// Shared facts from an LSP hover. Only explicit server signatures, semantic
// tokens and documentation provide evidence; symbol names never imply purpose.
import { Marked } from 'marked'
import { hoverMarkdownStrings, normalizeHoverDocumentation } from './hoverDocumentation.js'

const parser = new Marked({ gfm: true, async: false })
const languages = { python: 'Python', javascript: 'JavaScript', typescript: 'TypeScript', go: 'Go', rust: 'Rust', c: 'C', cpp: 'C++', csharp: 'C#', java: 'Java', vue: 'Vue', ruby: 'Ruby', php: 'PHP', kotlin: 'Kotlin', swift: 'Swift' }
const categories = { namespace: '命名空间', type: '类型', class: '类', enum: '枚举', interface: '接口', struct: '结构体', typeParameter: '类型参数', parameter: '参数', variable: '变量', field: '成员变量', property: '属性', enumMember: '枚举常量', event: '事件', function: '函数', method: '方法', macro: '宏', decorator: '装饰器', keyword: '关键字', comment: '注释', string: '字符串', number: '数值', operator: '运算符' }
const modifiers = { declaration: '声明位置', definition: '定义位置', readonly: '只读', static: '静态', deprecated: '已弃用', abstract: '抽象', async: '异步', modification: '修改位置', documentation: '文档', defaultLibrary: '标准库' }
const literal = value => String(value).replace(/&/g, '&amp;').replace(/[\\`*_{}\[\]()#+\-.!<>|~=:]/g, character => `&#${character.codePointAt(0)};`)

function semanticCategoryOf(token) {
  const category = categories[token?.type]
  if (!['variable', 'field', 'property'].includes(token?.type)) return category
  // These modifiers do not establish a language's const semantics or whether a
  // variable is local. Keep the service's distinction between fields/properties.
  const flags = Array.isArray(token.modifiers) ? token.modifiers : []
  const base = flags.includes('static') ? token.type === 'property' ? '静态属性' : '静态变量' : category
  return flags.includes('readonly') ? `只读${base}` : base
}

export function documentationTranslationState(value) {
  if (!value || typeof value !== 'object') return null
  if (value.status === 'pending') return 'pending'
  if (value.status === 'failed') return 'failed'
  if (value.status === 'untranslated') return 'untranslated'
  return value.language === 'zh-CN' && value.source === 'configured-model' && (!value.status || value.status === 'ready') ? 'ready' : null
}

const translationErrors = {
  provider_unavailable: '请在模型设置中配置可用模型后重试。',
  queue_full: '翻译队列繁忙，请稍后重试。',
  timeout: '翻译超时，请重试。',
  invalid_response: '翻译结果未通过校验，请重试。',
  provider_error: '翻译请求未成功，请重试。',
  input_too_large: '说明内容过长，暂时无法翻译。',
  request_failed: '连接中断，请重试。',
}
export function documentationTranslationPresentation(document, { missing = false } = {}) {
  const metadata = document?.documentation_translation
  const supplied = documentationTranslationState(metadata)
  let status = metadata?.required === false ? null
    : supplied === 'ready' && missing ? 'untranslated' : supplied || (missing ? 'untranslated' : null)
  const detached = document?.detached && ['pending', 'failed', 'untranslated'].includes(status)
  if (detached) status = 'untranslated'
  const label = ({ untranslated: '未翻译', pending: '翻译中', failed: '翻译失败', ready: '已翻译' })[status] || ''
  const message = detached ? '文件已切换或修改，请重新打开该符号的说明以更新翻译。'
    : status === 'pending' ? '正在翻译中文说明，完成后自动更新。'
    : status === 'failed' ? translationErrors[metadata?.error_code] || '中文说明翻译失败，可重试或查看原文。'
      : status === 'untranslated' ? '这段说明尚未翻译。' : ''
  return { status, label, message,
    canRetry: !detached && ['untranslated', 'failed'].includes(status) && metadata?.retryable !== false,
    action: status === 'failed' ? '重试翻译' : '翻译为中文' }
}

export function documentationTranslationNote(value, { untranslated = false } = {}) {
  const state = documentationTranslationState(value)
  const presentation = documentationTranslationPresentation({ documentation_translation: value }, { missing: untranslated })
  const text = state === 'ready' && !untranslated ? '**文档中文翻译：** 模型翻译，签名、类型与代码保持原文。'
    : presentation.message ? `**中文说明：** ${presentation.message}` : ''
  return text ? [{ kind: 'markdown', value: text }] : []
}

const hasChinese = value => /\p{Script=Han}/u.test(value)

// Chinese is the default reading view, not a claim that unavailable prose has
// been translated. Keep original server receipts separately for an opt-in view.
// Code examples/signatures are always retained, including English identifiers.
export function chineseDocumentationProjection(serverResult, language = '') {
  const original = normalizeHoverDocumentation(serverResult?.contents)
  let untranslated = false
  const sections = original.flatMap(section => {
    if (section.kind === 'code') return [section]
    if (section.kind === 'plaintext') {
      if (hasChinese(section.value) || !/[a-z]/i.test(section.value)) return [section]
      untranslated = true
      return []
    }
    const signature = signatureFacts([section], language)
    const blocks = parser.lexer(section.value)
    let omitted = false
    const kept = blocks.map(block => {
      const raw = block.raw || ''
      if (block.type === 'space' || block.type === 'code' || hasChinese(raw) || !/[a-z]/i.test(raw)) return raw
      if (signature.text && block.type === 'paragraph' && textOf(block.tokens).trim() === signature.text) return raw
      // These are provenance labels, not prose translations. Preserve literal
      // package/file names while presenting the established labels in Chinese.
      const source = /^(\s*)(Source|Defined in|Since|Author|Version)\s*:\s*/i.exec(raw)
      if (source) {
        const label = { source: '来源', 'defined in': '定义位置', since: '起始版本', author: '作者', version: '版本' }[source[2].toLowerCase()]
        return raw.replace(source[0], `${source[1]}${label}：`)
      }
      omitted = untranslated = true
      return ''
    }).join('').trim()
    if (!omitted && kept === section.value.trim()) return [section]
    return kept ? [{ ...section, value: kept }] : []
  })
  const restored = normalizeHoverDocumentation(serverResult?._code_agent_original?.contents)
  const originalSections = restored.length ? restored : JSON.stringify(sections) !== JSON.stringify(original) ? original : []
  return { sections, originalSections, untranslated }
}

function textOf(tokens) {
  return (tokens || []).map(token => {
    if (['html', 'image', 'code'].includes(token.type)) return ''
    if (token.tokens) return textOf(token.tokens)
    return typeof token.text === 'string' ? token.text : token.type === 'br' ? ' ' : ''
  }).join('')
}

function closingParenthesis(text, start, language) {
  let depth = 0, quote = ''
  for (let index = start; index < text.length; index++) {
    const character = text[index]
    if (quote) {
      if (character === '\\') index++
      else if (character === quote) quote = ''
    } else if (character === '"' || character === '`' || character === "'" && language !== 'rust') quote = character
    else if (character === '(') depth++
    else if (character === ')' && --depth === 0) return index
  }
  return -1
}

// Deliberately omit complex/ambiguous types, named Go results and function
// bodies. This is a display aid for explicit annotations, not a type checker.
function explicitType(value) {
  const type = value.trim()
  if (!type || type.length > 512 || /[\r\n]/.test(type) || !/^[\w$.,:<>\[\]()&*|?!'" \t]+$/.test(type) || /[\w$]\s+[\w$]/.test(type)) return ''
  const stack = [], pairs = { '>': '<', ']': '[', ')': '(' }
  for (const character of type) {
    if ('<[('.includes(character)) stack.push(character)
    else if (Object.hasOwn(pairs, character) && stack.pop() !== pairs[character]) return ''
  }
  return stack.length ? '' : type
}

function signatureFacts(contents, modelLanguage) {
  // Inspect the leading server block only. Later code samples describe usage,
  // and must never become the hovered symbol's category or return annotation.
  const section = contents.find(item => item.value.trim())
  if (!section) return {}
  const block = section.kind === 'code' ? null : parser.lexer(section.value).find(item => item.type !== 'space')
  const text = (section.kind === 'code' ? section.value : block?.type === 'code' ? block.text
    : block?.type === 'paragraph' ? textOf(block.tokens) : '').trim()
  if (!text || text.length > 8192) return {}
  const language = section.language || (block?.type === 'code' ? block.lang?.split(/\s/)[0] : '') || modelLanguage
  const label = /^\((function|method|class|variable|parameter|property)\)\s+/.exec(text)
  const body = label ? text.slice(label[0].length) : text
  let head = language === 'python' ? /^(?:async\s+)?def\s+[\w.]+\s*\(/.exec(body)
    : ['typescript', 'javascript', 'typescriptreact', 'javascriptreact', 'ts', 'js'].includes(language) ? /^(?:(?:export|declare|default|async)\s+)*function(?:\s*\*)?\s+[\w$]+(?:<[^<>()]*>)?\s*\(/.exec(body)
      : language === 'rust' ? /^(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const)\s+)*fn\s+\w+(?:<[^<>()]*>)?\s*\(/.exec(body)
        : language === 'go' ? /^func\s+(?:\([^)]*\)\s*)?\w+(?:\[[^\]]*\])?\s*\(/.exec(body) : null
  if (!head && label && ['function', 'method'].includes(label[1])) head = /^[\w.$]+(?:<[^<>()]*>)?\s*\(/.exec(body)
  if (!head) {
    const typedValue = /^(?:[\w$]+)\s*:\s*(.+)$/.exec(body)
    const labeledType = label?.[1] === 'class' && /^[\w.$]+(?:<[^<>()]*>|\[[^\[\]()]*\])?$/.test(body)
    return label && (labeledType || ['variable', 'parameter', 'property'].includes(label[1]) && typedValue && explicitType(typedValue[1]))
      ? { category: label[1], text } : {}
  }
  if (label && !['function', 'method'].includes(label[1])) return {}
  const close = closingParenthesis(body, head[0].lastIndexOf('('), language)
  if (close < 0) return {}
  const tail = body.slice(close + 1).trim()
  const annotation = language === 'python' ? /^->\s*(.+?)\s*:?$/.exec(tail)
    : ['typescript', 'javascript', 'typescriptreact', 'javascriptreact', 'ts', 'js'].includes(language) ? /^:\s*(.+?)\s*;?$/.exec(tail)
      : language === 'rust' ? /^->\s*(.+?)\s*;?$/.exec(tail)
        : language === 'go' && tail ? [tail, tail] : null
  const returnType = annotation ? explicitType(annotation[1]) : ''
  return { category: label?.[1] || (language === 'go' && /^func\s+\(/.test(body) ? 'method' : 'function'), text, ...(returnType ? { returnType } : {}) }
}

function prose(contents, signature) {
  for (const section of contents) {
    if (section.kind === 'code') continue
    for (const block of parser.lexer(section.value)) {
      if (block.type !== 'paragraph') continue
      if (signature.text && textOf(block.tokens).trim() === signature.text) continue
      const lines = textOf(block.tokens).split(/\r\n|\r|\n/).map(line => line.trim()).filter(line => line
        && !/^(?:source|来源|出处|defined in|defined at|since|author|version|see also|parameters?|returns?|参数|返回值?)\s*[:：]/i.test(line)
        && !/^(?:import|package|namespace|class|interface|struct|enum|type|def|async def|fn|pub fn|func|function)\s+/.test(line)
        && !/^[\w.$]+(?:\([^)]*\))?\s*;?$/.test(line))
      const text = lines.join(' ').replace(/\s+/g, ' ').trim()
      if (text) return Array.from(text).slice(0, 360).join('') + (Array.from(text).length > 360 ? '…' : '')
    }
  }
  return ''
}

function wordAt(model, position) {
  const word = model.getWordAtPosition?.(position)
  if (!word || typeof word.word !== 'string' || !word.word || word.word.length > 256
    || !Number.isInteger(word.startColumn) || !Number.isInteger(word.endColumn)) return null
  const line = model.getLineContent?.(position.lineNumber) ?? model.getValue?.().split(/\r\n|\r|\n/)[position.lineNumber - 1]
  return typeof line === 'string' && line.slice(word.startColumn - 1, word.endColumn - 1) === word.word ? word : null
}

export function describeSymbolHover({ model, position, serverResult, semanticToken = null } = {}) {
  if (!model || model.isDisposed?.() || !serverResult || !position) return null
  const original = normalizeHoverDocumentation(serverResult.contents)
  if (!original.length) return null
  const language = model.getLanguageId?.() || 'plaintext', word = wordAt(model, position)
  const signature = signatureFacts(original, language)
  const projection = chineseDocumentationProjection(serverResult, language)
  const purpose = prose(projection.sections, signature), semanticCategory = semanticCategoryOf(semanticToken)
  const category = semanticCategory || categories[signature.category]
  const attributes = [...new Set((semanticToken?.modifiers || []).map(name => modifiers[name]).filter(Boolean))]
  const lines = [word ? `**名称：** ${literal(word.word)}` : '', `**语言：** ${literal(languages[language] || language)}`]
  if (category) lines.push(`**类别：** ${category}（${semanticCategory ? '语言服务语义信息' : '语言服务器签名'}）`)
  if (attributes.length) lines.push(`**属性：** ${attributes.join('、')}`)
  if (signature.returnType) lines.push(`**返回类型：** ${literal(signature.returnType)}`)
  if (purpose) lines.push(`**作用：** ${literal(purpose)}`, '作用依据：语言服务器文档。')
  const detail = { kind: 'markdown', value: lines.filter(Boolean).join('\n\n') }
  const sections = [detail, ...documentationTranslationNote(serverResult.documentation_translation, projection),
    ...(projection.originalSections.length ? [{ kind: 'markdown', value: 'Ctrl+Q 查看完整说明或原文。' }] : []), ...projection.sections]
  const range = serverResult.range?.start && serverResult.range?.end ? {
    startLineNumber: serverResult.range.start.line + 1, startColumn: serverResult.range.start.character + 1,
    endLineNumber: serverResult.range.end.line + 1, endColumn: serverResult.range.end.character + 1,
  } : word ? { startLineNumber: position.lineNumber, startColumn: word.startColumn, endLineNumber: position.lineNumber, endColumn: word.endColumn } : null
  return { sections, symbolSection: detail, untranslated: projection.untranslated, originalSections: projection.originalSections, contents: hoverMarkdownStrings(sections), source: '语言服务', ...(range ? { range } : {}),
    symbol: { ...(word ? { name: word.word } : {}), language, ...(category ? { category } : {}), ...(semanticCategory ? { semanticType: semanticToken.type } : {}), ...(signature.returnType ? { returnType: signature.returnType } : {}), ...(attributes.length ? { attributes } : {}), ...(purpose ? { purpose } : {}) },
    ...(serverResult.documentation_translation && typeof serverResult.documentation_translation === 'object'
      ? { documentation_translation: { ...serverResult.documentation_translation } } : {}),
    ...(Object.hasOwn(serverResult, '_code_agent_original') ? { _code_agent_original: serverResult._code_agent_original } : {}) }
}
