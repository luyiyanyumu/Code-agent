import assert from 'node:assert/strict'
import test from 'node:test'
import { getTokenHover, getJavaTokenTarget, rememberSemanticTokens, clearSemanticTokens, registerTokenHoverSource, registerTokenBusinessSource, installTokenHoverProvider } from '../src/lib/tokenHover.js'
import { createBusinessMeaningSource } from '../src/lib/businessMeaning.js'

function model(text, language = 'java') {
  return { text, version: 1, disposed: false,
    getValue() { return this.text }, getLanguageId() { return language }, getVersionId() { return this.version },
    isDisposed() { return this.disposed }, set(text) { this.text = text; this.version++ },
  }
}
const value = hover => hover?.sections.map(section => section.value).join('\n') || ''
const pos = (column = 1, lineNumber = 1) => ({ lineNumber, column })
function provider() {
  const registrations = []
  const monaco = { languages: { registerHoverProvider(language, instance) {
    registrations.push({ language, instance }); return { dispose() {} }
  } } }
  installTokenHoverProvider(monaco); installTokenHoverProvider(monaco)
  assert.equal(registrations.length, 1)
  assert.equal(registrations[0].language, 'java')
  return registrations[0].instance
}

test('Java words have a local hover and precise ranges, without altering other languages', () => {
  const file = model('public class Example {}\r\n  private int count;')
  assert.match(value(getTokenHover(file, pos(3))), /修饰符/)
  const field = getTokenHover(file, pos(5, 2))
  assert.equal(field.range.startLineNumber, 2)
  assert.equal(field.range.startColumn, 3)
  assert.equal(field.range.endColumn, 10)
  assert.equal(getTokenHover(model('public', 'python'), pos()), null)
  assert.equal(getTokenHover(file, pos(7)), null, 'Whitespace must not explain the previous word')
})

test('current semantic tokens classify references and modifiers; edits and disposal invalidate them', () => {
  const file = model('item'), owner = {}
  const legend = { tokenTypes: ['interface'], tokenModifiers: ['public'] }
  rememberSemanticTokens(file, { data: Uint32Array.of(0, 0, 4, 0, 1) }, legend, owner)
  assert.match(value(getTokenHover(file, pos(2))), /接口/)
  file.set('other')
  assert.doesNotMatch(value(getTokenHover(file, pos(2))), /\*\*接口\*\*/)
  rememberSemanticTokens(file, { data: Uint32Array.of(0, 0, 5, 0, 1) }, legend, owner)
  clearSemanticTokens(owner, file)
  assert.match(value(getTokenHover(file, pos(2))), /标识符/)
  rememberSemanticTokens(file, { data: Uint32Array.of(0, 0, 5, 0, 0) }, legend, owner)
  clearSemanticTokens(owner)
  assert.match(value(getTokenHover(file, pos(2))), /标识符/)
  file.disposed = true
  assert.equal(getTokenHover(file, pos()), null)
})

test('validated current semantic tokens enrich all LSP languages and disappear after document changes', () => {
  for (const language of ['python', 'typescript', 'javascript', 'go', 'rust', 'c', 'cpp']) {
    const file = { ...model('read', language), getWordAtPosition: () => ({ word: 'read', startColumn: 1, endColumn: 5 }) }
    const owner = {}, response = { contents: '读取已提供的内容。' }
    rememberSemanticTokens(file, { data: [0, 0, 4, 0, 1] }, { tokenTypes: ['function'], tokenModifiers: ['async'] }, owner)
    const current = getTokenHover(file, pos(2), response)
    assert.equal(current.symbol.category, '函数')
    assert.deepEqual(current.symbol.attributes, ['异步'])
    file.set('newer')
    assert.equal(getTokenHover(file, pos(2), response).symbol.category, undefined)
    clearSemanticTokens(owner)
  }
})

test('pending Java translation keeps original purpose honest until a ready translated result arrives', () => {
  const file = model('class C { /** Returns the supplied value. */ String read() {} }')
  const position = pos(file.text.indexOf('read()') + 1)
  const pending = getTokenHover(file, position, { contents: 'Returns the supplied value.',
    documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'pending' } })
  assert.match(value(pending), /正在准备中文说明/)
  assert.doesNotMatch(value(pending), /模型中文翻译|模型翻译，/)
  assert.match(pending.tokenSection.value, /Returns the supplied value/)
  const ready = getTokenHover(file, position, { contents: '返回传入的值。',
    documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'ready' } })
  assert.match(ready.tokenSection.value, /返回传入的值/)
  assert.doesNotMatch(ready.tokenSection.value, /Returns the supplied value/)
})

test('separate models and owners never share semantic classifications', () => {
  const first = model('Thing'), second = model('Thing'), one = {}, two = {}
  const legend = { tokenTypes: ['class', 'interface'], tokenModifiers: [] }
  rememberSemanticTokens(first, { data: [0, 0, 5, 0, 0] }, legend, one)
  rememberSemanticTokens(second, { data: [0, 0, 5, 1, 0] }, legend, two)
  assert.match(value(getTokenHover(first, pos(2))), /\*\*类\*\*/)
  assert.match(value(getTokenHover(second, pos(2))), /接口/)
  clearSemanticTokens(one)
  assert.match(value(getTokenHover(first, pos(2))), /标识符/)
  assert.match(value(getTokenHover(second, pos(2))), /接口/)
  clearSemanticTokens(two)
})

test('Java classification precedes original typed documentation and retains safe rendering', () => {
  const file = model('@RequestParam(required = false) Long categoryId')
  const contents = [{ language: 'java', value: 'org.springframework.web.bind.annotation.RequestParam' },
    { kind: 'markdown', value: 'Source: *spring-web-6.2.19.jar*\n\n[run](command:evil)' },
    { kind: 'plaintext', value: '**literal**' }]
  const hover = getTokenHover(file, pos(4), { contents })
  assert.match(hover.sections[0].value, /注解/)
  assert.deepEqual(hover.sections.slice(1), [{ kind: 'code', language: 'java', value: contents[0].value }, contents[1], contents[2]])
  assert.ok(hover.contents.every(section => section.isTrusted === false && section.supportHtml === false))
  assert.equal(hover.source, 'Java 语法与语言服务')
})

test('GetMapping purpose is prepended to the original language signature and JAR source', () => {
  const file = model('import org.springframework.web.bind.annotation.GetMapping;')
  const contents = [{ language: 'java', value: 'org.springframework.web.bind.annotation.GetMapping' },
    { kind: 'markdown', value: 'Source: *spring-web-6.2.19.jar*' }]
  const hover = getTokenHover(file, pos(file.text.indexOf('GetMapping') + 1), { contents })
  assert.match(hover.sections[0].value, /\*\*作用：\*\*.*HTTP.*GET/)
  assert.deepEqual(hover.sections.slice(1), [{ kind: 'code', language: 'java', value: contents[0].value }, contents[1]])
  assert.ok(hover.contents.every(section => section.isTrusted === false && section.supportHtml === false))
})

test('translated Set.of hover labels model provenance and preserves its complete signature, examples and range', () => {
  const text = 'Set<String> result = Set.of("a", "b", "c", "d", "e", "f", "g", "h", "i");'
  const file = model(text), position = pos(text.indexOf('of(') + 1)
  const signature = 'public static Set<String> java.util.Set.of(String e1, String e2, String e3, String e4, String e5, String e6, String e7, String e8, String e9)'
  const example = 'Set<String> result = Set.of("a", "b", "c", "d", "e", "f", "g", "h", "i");'
  const documentation = '返回包含九个元素的不可修改集合。\n\n参数 `e1` 到 `e9` 是集合元素，返回类型为 `Set<String>`。\n\n```java\n' + example + '\n```'
  const contents = [{ language: 'java', value: signature }, { kind: 'markdown', value: documentation }]
  const server = { contents, documentation_translation: { language: 'zh-CN', source: 'configured-model' } }
  const snapshot = JSON.stringify(server)
  const hover = getTokenHover(file, position, server)
  assert.match(hover.tokenSection.value, /作用：\*\* 返回包含九个元素的不可修改集合/)
  assert.match(value(hover), /文档中文翻译：.*模型翻译/)
  assert.deepEqual(hover.sections.slice(-2), [{ kind: 'code', language: 'java', value: signature }, contents[1]])
  assert.equal(hover.range.startColumn, position.column)
  assert.equal(hover.range.endColumn, position.column + 2)
  assert.deepEqual(hover.documentationTranslation, server.documentation_translation)
  assert.equal(JSON.stringify(server), snapshot, 'the shared hover renderer must not rewrite server evidence')
  assert.ok(hover.contents.every(section => section.isTrusted === false && section.supportHtml === false))
  assert.ok(hover.contents.at(-1).value.includes('```java\n' + example + '\n```'))
})

test('Java caret at a verified identifier end retains complete details without requiring an LSP range', () => {
  const text = 'return Objects.requireNonNull(value);', start = text.indexOf('requireNonNull'), word = 'requireNonNull'
  const file = { ...model(text), getWordAtPosition: () => ({ word, startColumn: start + 1, endColumn: start + word.length + 1 }) }
  const position = pos(start + word.length + 1), owner = {}
  rememberSemanticTokens(file, { data: [0, start, word.length, 0, 1] }, { tokenTypes: ['method'], tokenModifiers: ['static'] }, owner)
  const server = { contents: [{ language: 'java', value: 'public static <T> T java.util.Objects.requireNonNull(T obj)' },
    { kind: 'markdown', value: '检查传入对象引用是否为空，非空时返回该引用。' }],
    documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'ready' } }
  const hover = getTokenHover(file, position, server)
  assert.equal(hover.source, 'Java 语法与语言服务')
  assert.match(hover.tokenSection.value, /`requireNonNull` · \*\*方法\*\*/)
  assert.match(hover.tokenSection.value, /作用：.*检查传入对象引用/s)
  assert.match(hover.tokenSection.value, /返回类型：.*<T> T/s)
  assert.match(hover.tokenSection.value, /静态/)
  assert.deepEqual(hover.range, { startLineNumber: 1, startColumn: start + 1, endLineNumber: 1, endColumn: position.column })
  assert.equal(getJavaTokenTarget(file, position, hover).name, word)
  assert.deepEqual(hover.sections.slice(-2), [{ kind: 'code', language: 'java', value: server.contents[0].value }, server.contents[1]])
  clearSemanticTokens(owner)
  const whitespace = { ...model('read next'), getWordAtPosition: () => ({ word: 'read', startColumn: 1, endColumn: 5 }) }
  assert.equal(getTokenHover(whitespace, pos(5)), null)
  const wrongWord = { ...file, getWordAtPosition: () => ({ word: 'wrong', startColumn: start + 1, endColumn: position.column }) }
  assert.equal(getTokenHover(wrongWord, position, server).tokenSection, undefined)
  const eof = { ...model('requireNonNull'), getWordAtPosition: () => ({ word, startColumn: 1, endColumn: word.length + 1 }) }
  assert.match(getTokenHover(eof, pos(word.length + 1), server).tokenSection.value, /返回类型/)
})

test('translation provenance is opt-in for legacy Java responses and is shared by other languages', () => {
  const contents = [{ language: 'java', value: 'Set<String> java.util.Set.of(String e1)' }, { kind: 'markdown', value: 'Returns an unmodifiable set.' }]
  const legacy = getTokenHover(model('Set.of("a");'), pos(5), { contents })
  assert.doesNotMatch(value(legacy), /文档中文翻译/)
  assert.equal(legacy.documentationTranslation, undefined)
  const other = getTokenHover(model('of', 'python'), pos(), { contents,
    documentation_translation: { language: 'zh-CN', source: 'configured-model' } })
  assert.deepEqual(other.sections.slice(-2), [{ kind: 'code', language: 'java', value: contents[0].value }, contents[1]])
  assert.match(value(other), /文档中文翻译/)
  assert.equal(other.documentation_translation.language, 'zh-CN')
  assert.equal(other.documentationTranslation, undefined)
})

test('raw resource method hover reaches the shared function lookup at its declaration without a definition request', async t => {
  const text = ['class DocumentController {', '/** 返回原始文档资源。 */', '@GetMapping("/{id}/raw")',
    'public ResponseEntity<Resource> raw(@PathVariable Long id) throws IOException {',
    '  return service.raw(id);', '}', '}'].join('\r\n')
  const file = model(text), offset = text.indexOf(' raw(') + 1
  const before = text.slice(0, offset).split('\r\n'), position = pos(before.at(-1).length + 1, before.length)
  const owner = {}, calls = []
  rememberSemanticTokens(file, { data: [position.lineNumber - 1, position.column - 1, 3, 0, 3] },
    { tokenTypes: ['method'], tokenModifiers: ['public', 'declaration'] }, owner)
  const languageResult = { contents: [{ language: 'java', value: 'public ResponseEntity<Resource> DocumentController.raw(Long id) throws IOException' },
    { kind: 'markdown', value: '返回原始文档资源。' }] }
  const original = getTokenHover(file, position, languageResult)
  assert.equal(original.tokenSection.kind, 'markdown')
  assert.equal(original.tokenSection.value, original.sections[0].value)
  assert.match(original.tokenSection.value, /^`raw` · \*\*方法\*\*/)
  assert.deepEqual(getJavaTokenTarget(file, position, original).declaration, position)
  const source = createBusinessMeaningSource({ contextForModel: candidate => candidate === file ? {
    projectId: 'p', tree: 'main', path: 'DocumentController.java', uri: 'file:///DocumentController.java',
    textForUri: () => text, client: { ready: true, capabilities: { definitionProvider: true }, definition() { throw new Error('the declaration must not need a definition request') } },
  } : null, targetForModel: getJavaTokenTarget, resolve: async (projectId, payload) => {
    calls.push({ projectId, payload })
    return { entity: { id: 'sym:DocumentController.java#raw', name: 'raw' },
      business: { business_meaning: '按文档编号读取原始文件资源', status: 'unreviewed', source: 'model' } }
  } })
  const businessRegistration = registerTokenBusinessSource(source)
  const languageRegistration = registerTokenHoverSource({ matches: candidate => candidate === file, provide: async () => original })
  t.after(() => { languageRegistration.dispose(); businessRegistration.dispose(); source.dispose(); clearSemanticTokens(owner) })
  const hover = await provider().provideHover(file, position)
  assert.match(value(hover), /功能描述（待确认）.*按文档编号读取原始文件资源/)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].projectId, 'p')
  assert.equal(calls[0].payload.name, 'raw')
  assert.equal(calls[0].payload.line, position.lineNumber)
  assert.equal(calls[0].payload.column, position.column)
  assert.equal(calls[0].payload.text, text)
})

test('keywords work offline without contacting registered language sources', async t => {
  const file = model('public class Example {}'), hoverProvider = provider()
  let calls = 0
  const source = registerTokenHoverSource({ matches: () => true, provide() { calls++; throw new Error('offline') } })
  t.after(() => source.dispose())
  assert.match(value(await hoverProvider.provideHover(file, pos(2))), /修饰符/)
  assert.equal(calls, 0)
  assert.match(value(await hoverProvider.provideHover(file, pos(15))), /类/)
  assert.equal(calls, 1)
})

test('in-flight responses cannot describe a replaced draft or a cancelled hover', async t => {
  const file = model('unresolved'), hoverProvider = provider()
  let deliver
  const source = registerTokenHoverSource({ matches: candidate => candidate === file, provide: () => new Promise(resolve => { deliver = resolve }) })
  t.after(() => source.dispose())
  const pending = hoverProvider.provideHover(file, pos(3))
  file.set('changed')
  deliver({ sections: [{ kind: 'plaintext', value: 'STALE' }] })
  assert.equal(await pending, null)
  const token = { isCancellationRequested: false }, cancelled = hoverProvider.provideHover(file, pos(3), token)
  token.isCancellationRequested = true
  deliver(null)
  assert.equal(await cancelled, null)
})
