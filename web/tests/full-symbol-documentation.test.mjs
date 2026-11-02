import test from 'node:test'
import assert from 'node:assert/strict'
import { buildFullSymbolDocumentation } from '../src/lib/fullSymbolDocumentation.js'
import { getTokenHover } from '../src/lib/tokenHover.js'
import { appendBusinessMeaning } from '../src/lib/businessMeaning.js'

const typed = (value, language = 'java') => ({ kind: 'code', language, value })
const markdown = value => ({ kind: 'markdown', value })
const model = text => ({ getValue: () => text, getLanguageId: () => 'java', getVersionId: () => 1 })
const joined = result => result.sections.map(section => section.value).join('\n')

test('complete Java reading shows facts once, moves provenance, and keeps complete parameter and example content', () => {
  const source = 'class Service { /** 返回当前用户的会话。 */ public String load(String token) { return token; } }'
  const signature = 'public String demo.Service.load(String token)'
  const body = '返回当前用户的会话。\n\n**参数**\n\n- `token`：返回当前用户的会话。\n\n**返回值**\n\n完整会话字符串。\n\n```java\nString value = service.load(token);\n```'
  const full = getTokenHover(model(source), { lineNumber: 1, column: source.indexOf('load(') + 2 }, {
    contents: [typed(signature), markdown(body), markdown('Source: *sky-server*'), typed('com')],
    documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'ready' },
    _code_agent_original: { contents: [typed(signature), markdown('Returns the current session.')] },
  })
  const before = JSON.stringify(full), result = buildFullSymbolDocumentation(full)
  assert.equal(result.title, 'load')
  assert.equal(result.category, '方法')
  assert.equal(result.purpose, '返回当前用户的会话。')
  assert.deepEqual(result.metadata, [{ label: '返回类型', value: 'String', code: true }])
  assert.equal(result.declaration, signature)
  assert.equal(result.sections.filter(section => section.declaration).length, 1)
  assert.equal(result.sections[0].value, signature)
  assert.match(joined(result), /\*\*参数\*\*|\*\*返回值\*\*|完整会话字符串/)
  assert.match(joined(result), /String value = service.load\(token\)/)
  assert.equal((joined(result).match(/返回当前用户的会话/g) || []).length, 1, 'parameter-specific repetition remains while repeated standalone summary is removed')
  assert.doesNotMatch(joined(result), /识别依据|作用依据|文档中文翻译|Ctrl\+Q|sky-server|^com$/m)
  assert.ok(result.provenance.some(item => item.label === '来源' && item.value === 'sky-server'))
  assert.ok(result.provenance.some(item => item.label === '命名空间' && item.value === 'com'))
  assert.equal(JSON.stringify(full), before)
})

test('only complete same-kind same-language duplicate sections disappear; parameter rows and code lines remain untouched', () => {
  const signature = typed('public String load(String token)')
  const repeated = markdown('参数 `first` 与参数 `second` 都是输入。\n\n- `first`：允许空值。\n- `second`：允许空值。')
  const example = typed('value();\nvalue();')
  const result = buildFullSymbolDocumentation({ sections: [signature, signature, repeated, repeated, example, example, typed(example.value, 'typescript')] })
  assert.equal(result.sections.filter(section => section.value === signature.value).length, 1)
  assert.equal(result.sections.filter(section => section.value === repeated.value).length, 1)
  assert.equal(result.sections.filter(section => section.value === example.value).length, 2, 'different code languages are distinct content')
  assert.equal((joined(result).match(/允许空值/g) || []).length, 2)
  assert.equal((joined(result).match(/value\(\);/g) || []).length, 4)
  const contextual = buildFullSymbolDocumentation({ sections: [markdown('### 成功示例'), example, markdown('### 重试示例'), example] })
  assert.equal(contextual.sections.filter(section => section.kind === 'code').length, 2, 'identical code under different narrative headings keeps its context')
})

test('signature copy preserves exact leading whitespace, CRLF, annotations and generic spelling', () => {
  const signature = '  @Deprecated\r\npublic static <T> java.util.List<T> demo.Service.load(\r\n  String token, T defaultValue\r\n)  '
  const result = buildFullSymbolDocumentation({ sections: [typed(signature)] })
  assert.equal(result.declaration, signature)
  assert.equal(result.sections[0].value, signature)
  assert.equal(result.sections[0].heading, '声明签名')
})

test('a leading fenced declaration is separated once while internal CRLF and following prose are retained', () => {
  const declaration = 'def load(\r\n    token: str,\r\n) -> str:'
  const value = '```python\r\n' + declaration + '\r\n```\r\n\r\n参数 `token` 用于查找会话。\r\n\r\n```python\r\nload("demo")\r\n```'
  const result = buildFullSymbolDocumentation({ sections: [markdown(value)] })
  assert.equal(result.declaration, declaration)
  assert.equal(result.sections[0].value, declaration)
  assert.match(joined(result), /参数 `token` 用于查找会话/)
  assert.match(joined(result), /load\("demo"\)/)
  assert.equal(result.sections.filter(section => section.declaration).length, 1)
})

test('later same-name examples and arbitrary plaintext cannot become the copied declaration', () => {
  for (const first of [markdown('以下仅为使用示例。'), { kind: 'plaintext', value: 'public class Fake {}' }]) {
    const result = buildFullSymbolDocumentation({ sections: [first, typed('public String load(String token)')] })
    assert.equal(result.declaration, '')
    assert.equal(result.sections.filter(section => section.declaration).length, 0)
    assert.match(joined(result), /public String load/)
    assert.equal(result.sections[0].kind, first.kind)
  }
})

test('long prose, lists, return and throws sections survive without preview limits', () => {
  const long = '正文开始\n\n' + '完整业务规则与边界条件。'.repeat(10000) + '\n\n正文结束'
  const extras = '### 参数\n\n| 参数 | 说明 |\n|---|---|\n| id | 当前用户编号 |\n\n### 返回值\n\n会话对象。\n\n### 抛出\n\n- `IOException`：读取失败。'
  const result = buildFullSymbolDocumentation({ sections: [typed('public String load(String token)'), markdown(long), markdown(extras)] })
  assert.ok(joined(result).includes(long))
  assert.ok(joined(result).includes(extras))
  assert.equal((joined(result).match(/完整业务规则与边界条件/g) || []).length, 10000)
})

test('original view preserves receipt order, duplicate sections, code and literal plaintext byte-for-byte', () => {
  const raw = [markdown('Source: *server*'), typed('public String load(\r\nString token\r\n)'),
    { kind: 'plaintext', value: '**literal**\n<script>unsafe()</script>' }, markdown('Parameters remain complete.'), markdown('Parameters remain complete.')]
  const document = { title: 'load', symbol: { name: 'load', category: '方法', purpose: '中文概述。', type: 'String' },
    sections: [typed('public String load(String token)'), markdown('中文详情。')], originalSections: raw }
  const result = buildFullSymbolDocumentation(document, { original: true })
  assert.deepEqual(result.sections.map(({ kind, language, value }) => ({ kind, ...(language ? { language } : {}), value })), raw)
  assert.equal(result.purpose, '')
  assert.equal(result.businessMeaning, '')
  assert.deepEqual(result.metadata, [])
  assert.equal(result.translationNotice, '')
  assert.equal(result.originalAvailable, true)
})

test('when no stored original exists, original fallback omits application summaries and retains all server rows', () => {
  const summary = markdown('`load` · **方法**\n\n**作用：** 查询会话。\n\n识别依据：语言服务器签名。')
  const raw = [typed('public String load()'), markdown('Source: server\n\n参数与示例完整保留。')]
  const document = { tokenSection: summary, symbol: { name: 'load', purpose: '查询会话。' }, sections: [summary,
    markdown('**文档中文翻译：** 模型翻译，签名、类型与代码保持原文。'), markdown('Ctrl+Q 查看完整说明或原文。'), ...raw] }
  const result = buildFullSymbolDocumentation(document, { original: true })
  assert.deepEqual(result.sections.map(({ kind, language, value }) => ({ kind, ...(language ? { language } : {}), value })), raw)
})

test('business detail stays complete and candidate-marked, without repeating the same meaning or exposing stale values', () => {
  const summary = markdown('`load` · **方法**\n\n**作用：** 查询会话。')
  const full = { tokenSection: summary, symbol: { name: 'load', category: '方法', purpose: '查询会话。' }, sections: [summary, typed('public String load()')] }
  const record = { entity: { id: 'load' }, business: { business_meaning: '查询会话！', detail: '按租户隔离，保留完整规则。',
    inputs: [{ name: 'token', meaning: '访问令牌' }], outputs: [{ name: 'session', meaning: '会话对象' }], rules: ['验证权限'], effects: ['更新访问时间'],
    feature_refs: ['账户功能'], status: 'current', source: 'model' } }
  const document = appendBusinessMeaning(full, record), result = buildFullSymbolDocumentation(document)
  assert.equal(result.businessMeaning, '')
  assert.equal(result.businessCandidate, true)
  assert.match(joined(result), /按租户隔离|访问令牌|会话对象|验证权限|更新访问时间|账户功能/)
  assert.equal(result.sections.filter(section => section.heading === '功能详情（待确认）').length, 1)
  const stale = buildFullSymbolDocumentation(appendBusinessMeaning(full, { ...record, business: { ...record.business, status: 'stale' } }))
  assert.doesNotMatch(joined(stale), /访问令牌|更新访问时间/)
})

test('only metadata paragraphs move to provenance; nested, coded and multi-line source text stays literal', () => {
  const body = '来源：server\n\n**文档中文翻译：** 模型翻译。\n\nCtrl+Q 查看完整说明或原文。\n\n### 参数\n\n- 来源：参数中真实文本。\n\n```text\n来源：代码示例中真实文本。\n```'
  const plainSource = { kind: 'plaintext', value: 'Source: server\n参数：这一行不能被挪走。' }
  const result = buildFullSymbolDocumentation({ sections: [markdown(body), plainSource] })
  assert.match(joined(result), /参数中真实文本|代码示例中真实文本|这一行不能被挪走/)
  assert.doesNotMatch(joined(result), /Ctrl\+Q|模型翻译/)
  assert.ok(result.provenance.some(item => item.label === '来源' && item.value === 'server'))
})

test('legacy summaries remain readable, translation state honest, unsafe prose inert and inputs unchanged', () => {
  const legacy = markdown('`load` · **方法**\n\n**作用：** 查询会话。\n\n**返回类型：** `String`\n\n**属性：** public（公开）、声明位置\n\n识别依据：语言服务器签名。')
  const raw = [legacy, typed('public String load()'), markdown('[危险](command:run)\n\n[参考](https://example.test/docs)\n\n<script>attack()</script>')]
  const document = { sections: raw, documentation_translation: { status: 'failed' } }, before = JSON.stringify(document)
  const result = buildFullSymbolDocumentation(document)
  assert.equal(result.title, 'load')
  assert.equal(result.category, '方法')
  assert.equal(result.purpose, '查询会话。')
  assert.deepEqual(result.metadata, [{ label: '返回类型', value: 'String', code: true }, { label: '属性', value: '公开' }])
  assert.match(result.translationNotice, /翻译失败/)
  assert.deepEqual(result.links, [{ href: 'https://example.test/docs', label: '参考' }])
  assert.ok(joined(result).includes('<script>attack()</script>'), 'projection preserves original text; component safe renderer handles it')
  assert.equal(JSON.stringify(document), before)
  assert.equal(buildFullSymbolDocumentation({ ...document, symbol: { name: 'load' } }).purpose, '查询会话。', 'partial structural metadata does not erase useful legacy facts')
  for (const input of [null, undefined, {}, { sections: [null, { kind: 'html', value: '<script>' }] }]) assert.deepEqual(buildFullSymbolDocumentation(input).sections, [])
})

test('known legacy business-only metadata never blocks a real following signature, while real narrative still does', () => {
  const signature = 'public static String findUser(String name)'
  const result = buildFullSymbolDocumentation({ title: 'findUser', sections: [markdown('**业务信息：** 根据名称查询用户。'), typed(signature)] })
  assert.equal(result.declaration, signature)
  assert.equal(result.businessMeaning, '根据名称查询用户。')
  assert.equal(result.sections.filter(section => section.declaration).length, 1)
  const annotation = 'org.example.web\npublic @interface RouteMapping'
  const legacy = buildFullSymbolDocumentation({ sections: [
    markdown('**作用：** 根据请求路径查找控制器。\n\n**返回类型：** `void`\n\n识别依据：语言服务语义信息。'),
    markdown('**业务信息：** 管理用户接口。\n\n**文档中文翻译：** 已翻译。'), typed(annotation),
  ] })
  assert.equal(legacy.declaration, annotation)
  assert.equal(legacy.owner, 'org.example.web')
  assert.equal(legacy.businessMeaning, '管理用户接口。')
  assert.equal(legacy.purpose, '根据请求路径查找控制器。')
  const candidate = buildFullSymbolDocumentation({ sections: [markdown('**功能描述（待确认）：** 根据名称查询用户。'), typed(signature)] })
  assert.equal(candidate.declaration, signature)
  assert.equal(candidate.businessCandidate, true)
  const narrative = buildFullSymbolDocumentation({ sections: [markdown('**业务信息：** 这是调用示例。\n\n接下来展示示例代码，并非此符号声明。'), typed(signature)] })
  assert.equal(narrative.declaration, '')
  assert.match(joined(narrative), /并非此符号声明/)
})

test('qualified Java class constants and member fields expose their actual complete typed declaration', () => {
  const signature = '  public static final java.util.Map<String, java.util.List<String>> com.example.Flags.VALUE = null  '
  const document = { symbol: { name: 'VALUE', category: '类常量', language: 'java', purpose: '共享配置。' }, sections: [typed(signature)] }
  const result = buildFullSymbolDocumentation(document)
  assert.equal(result.declaration, signature)
  assert.equal(result.owner, 'com.example.Flags')
  assert.equal(result.sections[0].value, signature)
  assert.equal(result.sections[0].declaration, true)
  const member = 'private String com.example.State.VALUE;'
  assert.equal(buildFullSymbolDocumentation({ ...document, symbol: { ...document.symbol, category: '成员变量' }, sections: [typed(member)] }).declaration, member)
  for (const invalid of ['com.example.Flags.VALUE = "usage";', 'read(com.example.Flags.VALUE);', 'return String com.example.Flags.VALUE;', 'This member com.example.Flags.VALUE']) {
    assert.equal(buildFullSymbolDocumentation({ ...document, sections: [typed(invalid)] }).declaration, '', invalid)
  }
  assert.equal(buildFullSymbolDocumentation({ ...document, symbol: { ...document.symbol, name: 'OTHER' } }).declaration, '')
  assert.equal(buildFullSymbolDocumentation({ ...document, sections: [markdown('下面仅是一个字段示例。'), typed(signature)] }).declaration, '')
})

test('only the selected declaration is lifted out; the same code in a later example keeps its language and context', () => {
  const signature = 'public String load(String token)'
  const result = buildFullSymbolDocumentation({ sections: [typed(signature), markdown('### 声明示例\n\n下面展示相同声明的另一用途。'), typed(signature), typed(signature, 'typescript')] })
  assert.equal(result.sections.filter(section => section.value === signature).length, 3)
  assert.equal(result.sections.filter(section => section.declaration).length, 1)
  assert.equal(result.sections.at(-1).language, 'typescript')
  assert.ok(result.sections.findIndex(section => section.value.includes('### 声明示例')) < result.sections.findIndex((section, index) => index > 0 && section.value === signature))
  const receipt = markdown('```java\n' + signature + '\n```\n\n参数说明。')
  const duplicate = buildFullSymbolDocumentation({ sections: [receipt, receipt] })
  assert.equal((joined(duplicate).match(/参数说明/g) || []).length, 1)
  assert.equal(duplicate.sections.filter(section => section.declaration).length, 1)
})

test('untranslated local Javadoc reports the same honest state as hover and retains its complete original', () => {
  const source = 'class A { /** Reads the current user profile. */ String read() { return ""; } }'
  const full = getTokenHover(model(source), { lineNumber: 1, column: source.indexOf('read()') + 2 })
  const reading = buildFullSymbolDocumentation(full)
  assert.equal(reading.purpose, '')
  assert.match(reading.translationNotice, /尚未翻译/)
  assert.doesNotMatch(joined(reading), /Reads the current/)
  const original = buildFullSymbolDocumentation(full, { original: true })
  assert.match(joined(original), /Reads the current user profile/)
  assert.deepEqual(original.sections.map(({ kind, language, value }) => ({ kind, ...(language ? { language } : {}), value })), full.originalSections)
})

test('a truncated generic purpose defers to its complete first prose paragraph without losing links, operators or the long tail', () => {
  const intro = '读取会话，对于 a != b 的情况，'
  const continuation = '保留完整业务规则与所有边界条件。'.repeat(100)
  const visible = intro + continuation + '正文长尾 SENTINEL_FULL_BODY。'
  const body = '读取[会话](https://example.test/session)，对于 `a != b` 的情况，' + continuation + '正文长尾 SENTINEL_FULL_BODY。'
  const preview = Array.from(visible).slice(0, 360).join('') + '…'
  const signature = typed('function load(): string', 'typescript')
  const document = { symbol: { name: 'load', category: '函数', language: 'typescript', purpose: preview }, sections: [signature, markdown(body)] }
  const result = buildFullSymbolDocumentation(document)
  assert.equal(result.purpose, '')
  assert.ok(joined(result).includes(body))
  assert.match(joined(result), /`a != b`|SENTINEL_FULL_BODY/)
  assert.ok(result.links.some(link => link.href === 'https://example.test/session'))
  const completePurpose = preview.slice(0, -1)
  assert.equal(buildFullSymbolDocumentation({ ...document, symbol: { ...document.symbol, purpose: completePurpose } }).purpose, completePurpose, 'a real independent description without truncation is not removed merely because it shares a prefix')
  const different = '不同规则。' + preview
  assert.equal(buildFullSymbolDocumentation({ ...document, symbol: { ...document.symbol, purpose: different } }).purpose, different)
  for (const section of [markdown('- ' + visible), markdown('| 说明 |\n|---|\n| ' + visible + ' |'), typed(visible, 'text')]) {
    assert.equal(buildFullSymbolDocumentation({ ...document, sections: [signature, section] }).purpose, preview, 'list/table/code content cannot suppress the overview')
  }
})
