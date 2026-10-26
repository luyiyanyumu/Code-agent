import assert from 'node:assert/strict'
import test from 'node:test'
import { describeSymbolHover } from '../src/lib/symbolHoverDocumentation.js'

function model(language, name = 'read') {
  return { getLanguageId: () => language, getValue: () => name,
    getWordAtPosition: () => ({ word: name, startColumn: 1, endColumn: name.length + 1 }) }
}

test('all supported languages share verified symbol details without losing complete LSP documentation or code', () => {
  for (const [language, signature] of [
    ['python', 'def read(value: str) -> str'], ['typescript', 'function read(value: string): string'],
    ['javascript', 'function read(value)'], ['go', 'func read(value string) string'],
    ['rust', 'pub fn read(value: &str) -> String'], ['c', 'char *read(const char *value)'],
    ['cpp', 'std::string read(const std::string &value)'], ['lua', 'function read(value)'],
  ]) {
    const document = '根据调用者提供的值读取内容。\n\n' + '完整的详细中文说明。'.repeat(500) + '\n\n```' + language + '\n' + signature + '\n```'
    const serverResult = { contents: [{ language, value: signature }, { kind: 'markdown', value: document }],
      documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'ready' },
      _code_agent_original: { contents: 'Original documentation.' } }
    const snapshot = JSON.stringify(serverResult)
    const hover = describeSymbolHover({ model: model(language), position: { lineNumber: 1, column: 2 }, serverResult,
      semanticToken: { type: 'function', modifiers: ['async', 'deprecated'] } })
    assert.equal(hover.symbol.name, 'read')
    assert.equal(hover.symbol.language, language)
    assert.equal(hover.symbol.category, '函数')
    assert.deepEqual(hover.symbol.attributes, ['异步', '已弃用'])
    assert.equal(hover.symbol.purpose, '根据调用者提供的值读取内容。')
    assert.deepEqual(hover.sections.slice(-2), [{ kind: 'code', language, value: signature }, serverResult.contents[1]])
    assert.equal(hover.sections.at(-1).value, document)
    assert.deepEqual(hover._code_agent_original, serverResult._code_agent_original)
    assert.equal(hover.documentation_translation.status, 'ready')
    assert.equal(JSON.stringify(serverResult), snapshot)
    assert.ok(hover.contents.every(section => section.isTrusted === false && section.supportHtml === false))
  }
})

test('unknown categories and signature-only responses never become invented behavior or return types', () => {
  const hover = describeSymbolHover({ model: model('some-language', 'getUser'), position: { lineNumber: 1, column: 2 },
    serverResult: { contents: { language: 'some-language', value: 'getUser(42)' } },
    semanticToken: { type: 'vendor-token', modifiers: ['vendor-flag'] } })
  assert.equal(hover.symbol.name, 'getUser')
  assert.equal(hover.symbol.category, undefined)
  assert.equal(hover.symbol.attributes, undefined)
  assert.equal(hover.symbol.purpose, undefined)
  assert.doesNotMatch(hover.sections[0].value, /作用：|返回类型|读取用户/)
  const noName = describeSymbolHover({ model: { ...model('python'), getWordAtPosition: () => ({ word: 'unproven', startColumn: 1, endColumn: 9 }) },
    position: { lineNumber: 1, column: 2 }, serverResult: { contents: 'Actual documentation.' } })
  assert.equal(noName.symbol.name, undefined)
})

test('pending and failed translation status is visible without claiming model translation succeeded', () => {
  for (const [status, label] of [['pending', '正在准备中文说明'], ['failed', '当前保留语言服务原文']]) {
    const hover = describeSymbolHover({ model: model('rust'), position: { lineNumber: 1, column: 2 },
      serverResult: { contents: { kind: 'plaintext', value: 'Returns a readable value.' },
        documentation_translation: { language: 'zh-CN', source: 'configured-model', status } } })
    assert.ok(hover.sections.some(section => section.value.includes(label)))
    assert.equal(hover.documentation_translation.status, status)
    assert.ok(!hover.sections.some(section => section.value.includes('模型翻译，')))
  }
})

test('explicit server labels provide categories when semantic tokens are unavailable', () => {
  for (const [label, signature, category] of [
    ['function', 'read(value: string): string', '函数'], ['method', 'Reader.read(value: string): Set<string>', '方法'],
    ['class', 'Reader<T>', '类'], ['variable', 'read: string', '变量'], ['parameter', 'read: string | undefined', '参数'],
  ]) {
    const contents = { kind: 'markdown', value: `(${label}) ${signature}\n\n服务器提供的真实说明。` }
    const hover = describeSymbolHover({ model: model('typescript'), position: { lineNumber: 1, column: 2 }, serverResult: { contents } })
    assert.equal(hover.symbol.category, category)
    assert.equal(hover.symbol.semanticType, undefined)
    assert.equal(hover.symbol.purpose, '服务器提供的真实说明。')
    assert.match(hover.sections[0].value, /语言服务器签名/)
    assert.deepEqual(hover.sections.at(-1), contents)
  }
})

test('leading typed signatures expose only explicit return annotations and preserve original code', () => {
  for (const [language, signature, returnType, category] of [
    ['python', 'def read(value: str = ")", callback: Callable[[str], int] = len) -> tuple[str, int]:', 'tuple[str, int]', '函数'],
    ['typescript', 'function read(callback: (value: string) => number): Promise<Map<string, number>>', 'Promise<Map<string, number>>', '函数'],
    ['javascript', 'function read(value): Set<string>', 'Set<string>', '函数'],
    ['rust', 'pub async fn read(value: &str) -> Result<Vec<String>, Error>', 'Result<Vec<String>, Error>', '函数'],
    ['go', 'func read(value string) (string, error)', '(string, error)', '函数'],
    ['go', 'func (reader *Reader) Read(value string) string', 'string', '方法'],
  ]) {
    const contents = { language, value: signature }
    const hover = describeSymbolHover({ model: model(language), position: { lineNumber: 1, column: 2 }, serverResult: { contents } })
    assert.equal(hover.symbol.category, category, signature)
    assert.equal(hover.symbol.returnType, returnType, signature)
    assert.equal(hover.symbol.purpose, undefined)
    assert.match(hover.sections[0].value, /\*\*返回类型：\*\*/)
    assert.deepEqual(hover.sections.at(-1), { kind: 'code', language, value: signature })
  }
  const fenced = '```typescript\n(method) Reader.read(value: string): Promise<Set<string>>\n```\n\n完整中文说明。'
  const hover = describeSymbolHover({ model: model('typescript'), position: { lineNumber: 1, column: 2 }, serverResult: { contents: fenced },
    semanticToken: { type: 'function' } })
  assert.equal(hover.symbol.category, '函数', 'real semantic tokens retain precedence over signature labels')
  assert.equal(hover.symbol.semanticType, 'function')
  assert.equal(hover.symbol.returnType, 'Promise<Set<string>>')
  assert.equal(hover.sections.at(-1).value, fenced)
})

test('prose, later examples, incomplete signatures and ambiguous annotations do not invent facts', () => {
  for (const [language, contents, category] of [
    ['python', { kind: 'plaintext', value: '(function) This description explains how functions work.' }, undefined],
    ['typescript', { kind: 'markdown', value: '(class) This is a description of Reader.' }, undefined],
    ['python', { kind: 'markdown', value: '说明调用方式。\n\n```python\ndef read() -> int\n```' }, undefined],
    ['go', { language: 'go', value: 'func read() (value string, err error)' }, '函数'],
    ['typescript', { language: 'typescript', value: 'function read() { return "value" }' }, '函数'],
    ['python', { language: 'python', value: 'def read(value: str -> int' }, undefined],
    ['python', { language: 'python', value: 'def read() -> returns a value' }, '函数'],
  ]) {
    const hover = describeSymbolHover({ model: model(language), position: { lineNumber: 1, column: 2 }, serverResult: { contents } })
    assert.equal(hover.symbol.category, category, contents.value)
    assert.equal(hover.symbol.returnType, undefined, contents.value)
    assert.doesNotMatch(hover.sections[0].value, /返回类型/)
  }
})
