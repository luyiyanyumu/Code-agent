import assert from 'node:assert/strict'
import test from 'node:test'
import { describeSymbolHover, chineseDocumentationProjection } from '../src/lib/symbolHoverDocumentation.js'

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

test('service modifiers refine binding names without inventing language-specific constants or local scope', () => {
  for (const language of ['typescript', 'python', 'rust', 'cpp', 'csharp', 'go']) {
    for (const [type, flags, category] of [
      ['variable', [], '变量'], ['variable', ['readonly'], '只读变量'],
      ['variable', ['static'], '静态变量'], ['variable', ['static', 'readonly'], '只读静态变量'],
      ['field', [], '成员变量'], ['field', ['readonly'], '只读成员变量'],
      ['field', ['static'], '静态变量'], ['field', ['readonly', 'static'], '只读静态变量'],
      ['property', [], '属性'], ['property', ['readonly'], '只读属性'],
      ['property', ['static'], '静态属性'], ['property', ['readonly', 'static'], '只读静态属性'],
      ['parameter', ['readonly'], '参数'], ['enumMember', [], '枚举常量'],
    ]) {
      const serverResult = { contents: { language, value: 'SET_CREATE_TIME' } }
      const semanticToken = { type, modifiers: flags }
      const before = JSON.stringify({ serverResult, semanticToken })
      const hover = describeSymbolHover({ model: model(language, 'SET_CREATE_TIME'), position: { lineNumber: 1, column: 2 },
        serverResult, semanticToken })
      assert.equal(hover.symbol.category, category, `${language}: ${type}/${flags}`)
      assert.equal(hover.symbol.semanticType, type)
      assert.match(hover.sections[0].value, /语言服务语义信息/)
      assert.doesNotMatch(hover.sections[0].value, /类常量|局部变量/)
      assert.deepEqual(hover.sections.at(-1), { kind: 'code', language, value: 'SET_CREATE_TIME' })
      if (flags.includes('readonly')) assert.ok(hover.symbol.attributes.includes('只读'))
      if (flags.includes('static')) assert.ok(hover.symbol.attributes.includes('静态'))
      assert.equal(JSON.stringify({ serverResult, semanticToken }), before)
    }
  }
})

test('uppercase names, readonly examples and unrelated modifiers cannot change a binding category', () => {
  const contents = { kind: 'markdown', value: '(variable) SET_CREATE_TIME: string\n\n中文说明。\n\n```typescript\nclass Example { static readonly value = 1 }\n```' }
  const hover = describeSymbolHover({ model: model('typescript', 'SET_CREATE_TIME'), position: { lineNumber: 1, column: 2 },
    serverResult: { contents }, semanticToken: { type: 'variable', modifiers: ['defaultLibrary', 'declaration', 'deprecated'] } })
  assert.equal(hover.symbol.category, '变量')
  assert.deepEqual(hover.symbol.attributes, ['标准库', '声明位置', '已弃用'])
  assert.equal(hover.sections.at(-1).value, contents.value)
  assert.doesNotMatch(hover.sections[0].value, /常量|静态变量|只读变量|局部变量/)
  const unknown = describeSymbolHover({ model: model('typescript'), position: { lineNumber: 1, column: 2 },
    serverResult: { contents: '说明。' }, semanticToken: { type: 'vendor-binding', modifiers: ['static', 'readonly'] } })
  assert.equal(unknown.symbol.category, undefined)
  assert.equal(unknown.symbol.semanticType, undefined)
})

test('pending and failed translation status is visible without claiming model translation succeeded', () => {
  for (const [status, label] of [['pending', '正在翻译中文说明'], ['failed', '翻译失败']]) {
    const hover = describeSymbolHover({ model: model('rust'), position: { lineNumber: 1, column: 2 },
      serverResult: { contents: { kind: 'plaintext', value: 'Returns a readable value.' },
        documentation_translation: { language: 'zh-CN', source: 'configured-model', status } } })
    assert.ok(hover.sections.some(section => section.value.includes(label)))
    assert.equal(hover.documentation_translation.status, status)
    assert.ok(!hover.sections.some(section => section.value.includes('模型翻译，')))
    assert.ok(!hover.sections.some(section => section.value.includes('Returns a readable value.')))
    assert.ok(hover.originalSections.some(section => section.value === 'Returns a readable value.'))
  }
})

test('Chinese projection retains code examples and mixed technical Chinese while preserving all unready prose', () => {
  const contents = [
    { language: 'typescript', value: 'function read(value: string): Promise<string>' },
    { kind: 'markdown', value: '使用 Promise 返回读取结果。\n\nReturns the result asynchronously.\n\n```typescript\nconst value = await read("English identifier");\n```\n\n参数 `value` 是输入字符串。' },
    { kind: 'plaintext', value: '<strong>This is literal original documentation.</strong>' },
  ]
  const before = JSON.stringify(contents)
  const result = chineseDocumentationProjection({ contents }, 'typescript')
  assert.match(result.sections.map(section => section.value).join('\n'), /使用 Promise|参数 `value`/)
  assert.match(result.sections.map(section => section.value).join('\n'), /English identifier/)
  assert.doesNotMatch(result.sections.map(section => section.value).join('\n'), /Returns the result asynchronously|literal original documentation/)
  assert.equal(result.untranslated, true)
  assert.deepEqual(result.originalSections, [{ kind: 'code', language: 'typescript', value: contents[0].value }, ...contents.slice(1)])
  assert.equal(JSON.stringify(contents), before)
})

test('unavailable translated prose never fabricates Chinese purpose or successful translation status', () => {
  const hover = describeSymbolHover({ model: model('python'), position: { lineNumber: 1, column: 2 },
    serverResult: { contents: [{ language: 'python', value: 'def read() -> str' }, 'Returns a readable value.'] } })
  assert.equal(hover.symbol.purpose, undefined)
  assert.equal(hover.symbol.returnType, 'str')
  assert.equal(hover.documentation_translation, undefined)
  assert.ok(hover.sections.some(section => section.value.includes('尚未翻译')))
  assert.ok(hover.originalSections.some(section => section.value === 'Returns a readable value.'))
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
