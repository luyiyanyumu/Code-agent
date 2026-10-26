// Language-specific explanations share one hover surface with the server's
// documentation. Java adds lexical help; all other languages use LSP facts.
import { createJavaTokenIndex, describeJavaToken } from './javaTokenDocumentation.js'
import { isChineseDocumentationTranslation } from './javaSymbolPurpose.js'
import { hoverMarkdownStrings, normalizeHoverDocumentation } from './hoverDocumentation.js'
import { describeSymbolHover, documentationTranslationState, documentationTranslationNote } from './symbolHoverDocumentation.js'

const indexes = new WeakMap()
const semanticCache = new WeakMap()
const ownerModels = new WeakMap()
const sources = new Set()
const businessSources = new Set()
const installations = new WeakMap()
const documentationListeners = new Set()

export function subscribeHoverDocumentationUpdates(listener) {
  documentationListeners.add(listener)
  return () => documentationListeners.delete(listener)
}

export function notifyHoverDocumentationUpdated(change) {
  for (const listener of [...documentationListeners]) {
    try { listener(change) } catch { /* One closed view must not block others. */ }
  }
}

function revision(model) { return model.getVersionId?.() ?? model.getValue() }

function indexed(model) {
  const version = revision(model)
  let entry = indexes.get(model)
  if (!entry || entry.version !== version) {
    const text = model.getValue()
    const lines = [0]
    for (const match of text.matchAll(/\r\n|\r|\n/g)) lines.push(match.index + match[0].length)
    entry = { version, text, lines, index: createJavaTokenIndex(text) }
    indexes.set(model, entry)
  }
  return entry
}

function positionAt(entry, offset) {
  let low = 0, high = entry.lines.length
  while (low + 1 < high) {
    const middle = (low + high) >>> 1
    if (entry.lines[middle] <= offset) low = middle
    else high = middle
  }
  return { lineNumber: low + 1, column: offset - entry.lines[low] + 1 }
}

function javaTokenLocation(model, position, entry) {
  const line = entry.lines[position.lineNumber - 1]
  const offset = model.getOffsetAt?.(position) ?? (line ?? entry.text.length) + position.column - 1
  const word = model.getWordAtPosition?.(position)
  // Toolbar/caret queries can sit at the identifier's exclusive end while
  // Monaco and the server still identify that word. Match that verified word,
  // but never turn whitespace into a hover for the preceding identifier.
  if (Number.isInteger(line) && word?.endColumn === position.column && Number.isInteger(word.startColumn)
    && word.startColumn < word.endColumn && /^[\p{L}\p{Nl}\p{Sc}\p{Pc}][\p{L}\p{Nl}\p{Sc}\p{Pc}\p{Mn}\p{Mc}\p{Nd}\p{Cf}]*$/u.test(word.word)
    && entry.text.slice(line + word.startColumn - 1, offset) === word.word && !/\s/.test(entry.text[offset] || '')) {
    return { offset: offset - 1, position: { ...position, column: position.column - 1 } }
  }
  return { offset, position }
}

// Accept only the already validated token stream from the LSP client. Keeping
// its revision prevents an old class/variable classification surviving edits.
export function rememberSemanticTokens(model, result, legend, owner) {
  if (!model || !result?.data || !owner) return
  const tokens = []
  let line = 0, column = 0
  for (let i = 0; i < result.data.length; i += 5) {
    line += result.data[i]
    column = result.data[i] ? result.data[i + 1] : column + result.data[i + 1]
    tokens.push({ line, column, length: result.data[i + 2], type: result.data[i + 3], bits: result.data[i + 4] })
  }
  let owners = semanticCache.get(model)
  if (!owners) semanticCache.set(model, owners = new Map())
  owners.set(owner, { version: revision(model), tokens, legend })
  let models = ownerModels.get(owner)
  if (!models) ownerModels.set(owner, models = new Set())
  models.add(model)
}

export function clearSemanticTokens(owner, model) {
  const models = ownerModels.get(owner)
  for (const item of model ? [model] : models || []) {
    semanticCache.get(item)?.delete(owner)
    models?.delete(item)
  }
  if (!models?.size) ownerModels.delete(owner)
}

function semanticAt(model, position) {
  const line = position.lineNumber - 1, column = position.column - 1
  for (const entry of semanticCache.get(model)?.values() || []) {
    if (entry.version !== revision(model)) continue
    let low = 0, high = entry.tokens.length
    while (low < high) {
      const middle = (low + high) >>> 1, token = entry.tokens[middle]
      if (token.line < line || token.line === line && token.column <= column) low = middle + 1
      else high = middle
    }
    const token = entry.tokens[low - 1]
    if (token?.line === line && token.column <= column && column < token.column + token.length) {
      return { type: entry.legend.tokenTypes[token.type], modifiers: entry.legend.tokenModifiers.filter((_name, bit) => token.bits & 2 ** bit) }
    }
  }
  return null
}

export function getTokenHover(model, position, serverResult = null) {
  if (!model || model.isDisposed?.()) return null
  if (model.getLanguageId?.() !== 'java') return describeSymbolHover({ model, position, serverResult, semanticToken: semanticAt(model, position) })
  const raw = serverResult?.contents == null ? [] : Array.isArray(serverResult.contents) ? serverResult.contents : [serverResult.contents]
  const documentationTranslation = documentationTranslationState(serverResult?.documentation_translation) === 'ready' && isChineseDocumentationTranslation(serverResult?.documentation_translation)
    ? { language: 'zh-CN', source: 'configured-model' } : null
  let description = null, range
  if (model.getLanguageId?.() === 'java') {
    const entry = indexed(model)
    const location = javaTokenLocation(model, position, entry)
    description = describeJavaToken({ text: entry.text, offset: location.offset, index: entry.index, semanticToken: semanticAt(model, location.position), hoverContents: serverResult?.contents, documentationTranslation })
    if (description) {
      const start = positionAt(entry, description.start), end = positionAt(entry, description.end)
      range = { startLineNumber: start.lineNumber, startColumn: start.column, endLineNumber: end.lineNumber, endColumn: end.column }
    }
  }
  const translationNote = raw.length ? documentationTranslationNote(serverResult?.documentation_translation) : []
  const allContents = [...(description?.contents || []), ...translationNote, ...raw]
  const sections = normalizeHoverDocumentation(allContents)
  if (!sections.length) return null
  const serverRange = serverResult?.range
  return {
    sections, contents: hoverMarkdownStrings(allContents),
    // The project function description can be inserted below this word's
    // category while leaving the original language-service sections intact.
    tokenSection: description?.contents?.[0],
    ...(documentationTranslation ? { documentationTranslation } : {}),
    ...(serverResult?.documentation_translation && typeof serverResult.documentation_translation === 'object'
      ? { documentation_translation: { ...serverResult.documentation_translation } } : {}),
    ...(serverResult && Object.hasOwn(serverResult, '_code_agent_original') ? { _code_agent_original: serverResult._code_agent_original } : {}),
    ...(range ? { range } : serverRange ? { range: { startLineNumber: serverRange.start.line + 1, startColumn: serverRange.start.character + 1, endLineNumber: serverRange.end.line + 1, endColumn: serverRange.end.character + 1 } } : {}),
    source: description ? raw.length ? 'Java 语法与语言服务' : 'Java 代码说明' : '语言服务',
    localOnly: !!description?.localOnly,
  }
}

export function registerTokenHoverSource(source) {
  sources.add(source)
  return { dispose() { sources.delete(source) } }
}

// A binding is a declaration position, never a word-name lookup. The Java
// adapter only publishes it for a compatible declaration or scoped reference.
export function getJavaTokenTarget(model, position, hover) {
  if (!model || model.isDisposed?.() || model.getLanguageId?.() !== 'java') return null
  const entry = indexed(model)
  const location = javaTokenLocation(model, position, entry)
  const item = describeJavaToken({ text: entry.text, offset: location.offset, index: entry.index, semanticToken: semanticAt(model, location.position), hoverContents: hover?.sections,
    documentationTranslation: hover?.documentationTranslation })
  if (!item || item.localOnly || !/^[\p{L}\p{Nl}\p{Sc}\p{Pc}][\p{L}\p{Nl}\p{Sc}\p{Pc}\p{Mn}\p{Mc}\p{Nd}\p{Cf}]*$/u.test(item.word)) return null
  return { name: item.word, category: item.category,
    ...(Number.isInteger(item.declarationOffset) ? { declaration: positionAt(entry, item.declarationOffset) } : {}) }
}

export function registerTokenBusinessSource(source) {
  businessSources.add(source)
  return { dispose() { businessSources.delete(source) } }
}

export async function withTokenBusiness(model, position, hover, token) {
  if (!hover || hover.localOnly || token?.isCancellationRequested || model?.isDisposed?.()) return hover
  const version = revision(model)
  const source = [...businessSources].find(candidate => candidate.matches(model))
  if (!source) return hover
  let result = hover
  try { result = await source.provide(model, position, hover, token) || hover }
  catch { /* The language explanation remains available without the project service. */ }
  return token?.isCancellationRequested || model.isDisposed?.() || revision(model) !== version ? null : result
}

export function installTokenHoverProvider(monaco) {
  if (installations.has(monaco)) return installations.get(monaco)
  const provider = monaco.languages.registerHoverProvider('java', {
    async provideHover(model, position, token) {
      if (token?.isCancellationRequested || model.isDisposed?.()) return null
      const version = revision(model), local = getTokenHover(model, position)
      if (local?.localOnly) return local
      const source = [...sources].find(candidate => candidate.matches(model))
      let result = local
      if (source) {
        try { result = await source.provide(model, position, token) || local }
        catch (error) {
          if (error?.name === 'CancellationError' || error?.code === -32800) return null
          // An unavailable server still leaves a local explanation.
        }
      }
      if (token?.isCancellationRequested || model.isDisposed?.() || revision(model) !== version) return null
      return withTokenBusiness(model, position, result, token)
    },
  })
  installations.set(monaco, provider)
  return provider
}
