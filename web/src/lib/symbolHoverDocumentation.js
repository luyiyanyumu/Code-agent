// Shared facts from an LSP hover. Only explicit server signatures, semantic
// tokens and documentation provide evidence; symbol names never imply purpose.
import { Marked } from 'marked'
import { hoverMarkdownStrings, normalizeHoverDocumentation } from './hoverDocumentation.js'

const parser = new Marked({ gfm: true, async: false })
const languages = { python: 'Python', javascript: 'JavaScript', typescript: 'TypeScript', go: 'Go', rust: 'Rust', c: 'C', cpp: 'C++', csharp: 'C#', java: 'Java', vue: 'Vue', ruby: 'Ruby', php: 'PHP', kotlin: 'Kotlin', swift: 'Swift' }
const categories = { namespace: '命名空间', type: '类型', class: '类', enum: '枚举', interface: '接口', struct: '结构体', typeParameter: '类型参数', parameter: '参数', variable: '变量', property: '属性', enumMember: '枚举成员', event: '事件', function: '函数', method: '方法', macro: '宏', decorator: '装饰器', keyword: '关键字', comment: '注释', string: '字符串', number: '数值', operator: '运算符' }
const modifiers = { declaration: '声明位置', definition: '定义位置', readonly: '只读', static: '静态', deprecated: '已弃用', abstract: '抽象', async: '异步', modification: '修改位置', documentation: '文档', defaultLibrary: '标准库' }
const literal = value => String(value).replace(/&/g, '&amp;').replace(/[\\`*_{}\[\]()#+\-.!<>|~=:]/g, character => `&#${character.codePointAt(0)};`)

export function documentationTranslationState(value) {
  if (!value || typeof value !== 'object') return null
  if (value.status === 'pending') return 'pending'
  if (value.status === 'failed') return 'failed'
  return value.language === 'zh-CN' && value.source === 'configured-model' && (!value.status || value.status === 'ready') ? 'ready' : null
}

export function documentationTranslationNote(value) {
  const state = documentationTranslationState(value)
  const text = state === 'ready' ? '**文档中文翻译：** 模型翻译，签名、类型与代码保持原文。'
    : state === 'pending' ? '**文档中文翻译：** 正在准备中文说明，完成后更新。'
      : state === 'failed' ? '**文档中文翻译：** 暂未完成，当前保留语言服务原文。' : ''
  return text ? [{ kind: 'markdown', value: text }] : []
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
  const purpose = prose(original, signature), semanticCategory = categories[semanticToken?.type]
  const category = semanticCategory || categories[signature.category]
  const attributes = [...new Set((semanticToken?.modifiers || []).map(name => modifiers[name]).filter(Boolean))]
  const lines = [word ? `**名称：** ${literal(word.word)}` : '', `**语言：** ${literal(languages[language] || language)}`]
  if (category) lines.push(`**类别：** ${category}（${semanticCategory ? '语言服务语义信息' : '语言服务器签名'}）`)
  if (attributes.length) lines.push(`**属性：** ${attributes.join('、')}`)
  if (signature.returnType) lines.push(`**返回类型：** ${literal(signature.returnType)}`)
  if (purpose) lines.push(`**作用：** ${literal(purpose)}`, '作用依据：语言服务器文档。')
  const detail = { kind: 'markdown', value: lines.filter(Boolean).join('\n\n') }
  const sections = [detail, ...documentationTranslationNote(serverResult.documentation_translation), ...original]
  const range = serverResult.range?.start && serverResult.range?.end ? {
    startLineNumber: serverResult.range.start.line + 1, startColumn: serverResult.range.start.character + 1,
    endLineNumber: serverResult.range.end.line + 1, endColumn: serverResult.range.end.character + 1,
  } : word ? { startLineNumber: position.lineNumber, startColumn: word.startColumn, endLineNumber: position.lineNumber, endColumn: word.endColumn } : null
  return { sections, contents: hoverMarkdownStrings(sections), source: '语言服务', ...(range ? { range } : {}),
    symbol: { ...(word ? { name: word.word } : {}), language, ...(category ? { category } : {}), ...(semanticCategory ? { semanticType: semanticToken.type } : {}), ...(signature.returnType ? { returnType: signature.returnType } : {}), ...(attributes.length ? { attributes } : {}), ...(purpose ? { purpose } : {}) },
    ...(serverResult.documentation_translation && typeof serverResult.documentation_translation === 'object'
      ? { documentation_translation: { ...serverResult.documentation_translation } } : {}),
    ...(Object.hasOwn(serverResult, '_code_agent_original') ? { _code_agent_original: serverResult._code_agent_original } : {}) }
}
