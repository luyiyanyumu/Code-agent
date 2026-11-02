import test from 'node:test'
import assert from 'node:assert/strict'
import { marked } from 'marked'
import { compactSymbolHover } from '../src/lib/compactSymbolHover.js'
import { getTokenHover } from '../src/lib/tokenHover.js'
import { appendBusinessMeaning } from '../src/lib/businessMeaning.js'

const value = document => document.contents.map(item => item.value).join('\n')
const model = (source, language = 'java') => ({ getValue: () => source, getVersionId: () => 1,
  getLanguageId: () => language, getWordAtPosition: language === 'java' ? undefined : () => ({ word: source, startColumn: 1, endColumn: source.length + 1 }) })

test('Java preview removes auxiliary rows while preserving complete translated and original receipts', () => {
  const source = 'class Flags { /** 创建时间 setter 的方法名称。 */ public static final String SET_CREATE_TIME = "setCreateTime"; }'
  const full = getTokenHover(model(source), { lineNumber: 1, column: source.indexOf('SET_CREATE_TIME') + 2 }, {
    contents: [{ language: 'java', value: 'com' }, { kind: 'markdown', value: '来源：sky-server' }],
    documentation_translation: { language: 'zh-CN', source: 'configured-model', status: 'ready' },
    _code_agent_original: { contents: 'Original receipt.' },
  })
  const snapshot = JSON.stringify(full), compact = compactSymbolHover(full)
  assert.equal(compact.contents.length, 1)
  assert.match(value(compact), /^### `SET_CREATE_TIME` · 类常量/)
  assert.match(value(compact), /创建时间 setter 的方法名称/)
  assert.match(value(compact), /\| 类型 \| `String`/)
  assert.match(value(compact), /\| 属性 \| 公开 \|/)
  assert.doesNotMatch(value(compact), /识别依据|作用依据|文档中文翻译|模型翻译|sky-server|\ncom\n|声明位置|中文说明尚未就绪/)
  assert.match(value(compact), /> 完整说明 `Ctrl\+Q`$/)
  assert.equal(compact.sections, full.sections)
  assert.equal(compact.originalSections, full.originalSections)
  assert.equal(compact._code_agent_original, full._code_agent_original)
  assert.equal(compact.tokenSection, full.tokenSection)
  assert.equal(JSON.stringify(full), snapshot)
})

test('non-Java previews preserve explicit union return types and only the leading declaration', () => {
  const full = getTokenHover(model('read', 'typescript'), { lineNumber: 1, column: 2 }, {
    contents: [{ language: 'typescript', value: 'function read(value: string): Promise<string | null>' },
      { kind: 'markdown', value: '读取指定内容。\n\n完整的参数与错误说明。' }],
  })
  const compact = compactSymbolHover(full), html = marked.parse(value(compact))
  assert.match(value(compact), /read` · 函数/)
  assert.match(value(compact), /\| 返回类型 \| `Promise<string \\\| null>`/)
  assert.match(value(compact), /```typescript\nfunction read/)
  assert.match(html, /<code>Promise&lt;string \| null&gt;<\/code>/)
  assert.equal(compact.sections, full.sections)
  const proseFirst = getTokenHover(model('read', 'typescript'), { lineNumber: 1, column: 2 }, {
    contents: [{ kind: 'markdown', value: '下面是一个示例。' }, { language: 'typescript', value: 'function read(): number' }],
  })
  assert.doesNotMatch(value(compactSymbolHover(proseFirst)), /```|返回类型/)
})

test('translation states stay brief and never leak unready English prose', () => {
  for (const [status, expected] of [['pending', /正在翻译中文说明/], ['failed', /翻译失败.*重试/], ['untranslated', /尚未翻译/], [undefined, /尚未翻译/]]) {
    const full = getTokenHover(model('read', 'python'), { lineNumber: 1, column: 2 }, {
      contents: [{ language: 'python', value: 'def read() -> str:' }, 'Returns the complete contents of the file.'],
      ...(status ? { documentation_translation: { language: 'zh-CN', source: 'configured-model', status } } : {}),
    })
    const compact = compactSymbolHover(full)
    assert.match(value(compact), expected)
    assert.doesNotMatch(value(compact), /Returns the complete|文档中文翻译|模型翻译/)
    assert.ok(full.originalSections.some(section => section.value.includes('Returns the complete')))
  }
})

test('real project meaning is distinct, candidate-marked, deduplicated and never stale', () => {
  const source = 'class A { /** 保存当前编号。 */ int id; }'
  const full = getTokenHover(model(source), { lineNumber: 1, column: source.indexOf('id;') + 1 })
  const candidate = appendBusinessMeaning(full, { entity: { id: 'id', name: 'id' }, business: {
    business_meaning: '标识当前登录用户。', status: 'unreviewed', source: 'model', detail: '完整业务补充信息。',
  } })
  const compact = compactSymbolHover(candidate)
  assert.match(value(compact), /功能（待确认）/)
  assert.match(value(compact), /标识当前登录用户/)
  assert.doesNotMatch(value(compact), /完整业务补充信息|功能依据/)
  assert.equal(compact.businessBaseSections, candidate.businessBaseSections)
  assert.equal(compact.businessSection, candidate.businessSection)
  const duplicate = appendBusinessMeaning(full, { entity: { id: 'id' }, business: {
    business_meaning: '保存当前编号！', status: 'current', source: 'human',
  } })
  assert.doesNotMatch(value(compactSymbolHover(duplicate)), /\*\*功能/)
  const stale = appendBusinessMeaning(full, { entity: { id: 'id' }, business: {
    business_meaning: '旧的错误说明', status: 'stale', source: 'model',
  } })
  assert.doesNotMatch(value(compactSymbolHover(stale)), /旧的错误说明|待自动|待更新/)
})

test('long prose and signatures have an explicit preview boundary while full code stays untouched', () => {
  const signature = 'function read(' + Array.from({ length: 80 }, (_, i) => `argument${i}: string`).join(', ') + '): string'
  const full = getTokenHover(model('read', 'typescript'), { lineNumber: 1, column: 2 }, {
    contents: [{ language: 'typescript', value: signature }, { kind: 'markdown', value: '详细中文说明。'.repeat(200) }],
  })
  const compact = compactSymbolHover(full)
  assert.ok(value(compact).length < 1200)
  assert.match(value(compact), /…/)
  assert.equal(compact.sections, full.sections)
  assert.ok(full.sections.some(section => section.value === signature))
})

test('untrusted names, summaries, business and signatures cannot activate HTML, links or images', () => {
  const summary = { kind: 'markdown', value: '**名称：** item' }
  const document = { symbol: { name: 'item` [run](command:evil)', category: '变量', purpose: '<img src="https://bad.invalid"> 中文说明 ![图](https://bad.invalid/a.png)' },
    symbolSection: summary, sections: [summary, { kind: 'code', language: 'typescript', value: 'const item = "</script>"' }] }
  const compact = compactSymbolHover(document), rendered = marked.parse(value(compact))
  assert.doesNotMatch(rendered, /<img\b|<script\b|href="command:|src="https:/)
  assert.ok(compact.contents.every(item => item.isTrusted === false && item.supportHtml === false && item.supportThemeIcons === false))
  assert.equal(compact.sections, document.sections)
  assert.equal(compactSymbolHover(null), null)
  const legacy = { contents: [{ value: 'legacy' }] }
  assert.equal(compactSymbolHover(legacy), legacy)
})
