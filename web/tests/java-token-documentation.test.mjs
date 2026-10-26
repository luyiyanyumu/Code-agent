import assert from 'node:assert/strict'
import test from 'node:test'
import { createJavaTokenIndex, describeJavaToken } from '../src/lib/javaTokenDocumentation.js'

function at(text, word, options = {}) {
  const offset = options.offset ?? text.indexOf(word)
  assert.ok(offset >= 0, `fixture contains ${word}`)
  return describeJavaToken({ text, offset, ...options })
}
const markdown = result => result.contents[0].value

test('the screenshot method explains each keyword, type, annotation, attribute and parameter', () => {
  const text = 'public List<Document> list(@RequestParam(required = false) Long categoryId,\n @RequestParam(required = false) String kw,\n @RequestParam(required = false) Long folderId) { return service.list(categoryId, kw, folderId); }'
  const index = createJavaTokenIndex(text)
  const expected = { public: '访问修饰符', List: '类型引用', Document: '类型引用', list: '方法', RequestParam: '注解', required: '注解属性', false: '布尔字面量', Long: '类型引用', categoryId: '参数', String: '类型引用', kw: '参数', folderId: '参数', return: '控制流关键字', service: '标识符（语义尚未解析）' }
  for (const [word, category] of Object.entries(expected)) {
    const result = at(text, word, { index })
    assert.equal(result.category, category, word)
    assert.equal(result.word, word)
    assert.equal(text.slice(result.start, result.end), word)
    assert.match(markdown(result), /\*\*作用：\*\*\s+\S/, word)
  }
  assert.match(markdown(at(text, 'categoryId')), /声明类型.*`Long`/)
  assert.match(markdown(at(text, 'list')), /返回类型.*`List<Document>`/)
  assert.equal(at(text, 'categoryId', { offset: text.lastIndexOf('categoryId') }).category, '参数')
  assert.equal(at(text, 'list', { offset: text.lastIndexOf('list') }).category, '方法调用')
})

test('Spring raw resource method declarations retain their exact name binding with annotations, generics and JDT signatures', () => {
  for (const newline of ['\n', '\r\n']) {
    const text = ['class DocumentController {', '/** 返回原始文档资源。 */', '@GetMapping("/{id}/raw")',
      'public ResponseEntity<Resource> raw(', '  @PathVariable("id") Long id', ') throws IOException {',
      '  return service.raw(id);', '}', '}'].join(newline)
    const offset = text.indexOf(' raw(') + 1, index = createJavaTokenIndex(text)
    const hoverContents = [{ language: 'java', value: 'public ResponseEntity<Resource> DocumentController.raw(Long id) throws IOException' },
      { kind: 'markdown', value: '返回原始文档资源。' }]
    for (const semanticToken of [null, { type: 'method', modifiers: ['public', 'declaration'] }, { type: 'function', modifiers: ['public', 'declaration'] }]) {
      const result = describeJavaToken({ text, offset, index, semanticToken, hoverContents })
      assert.equal(result.category, '方法')
      assert.equal(result.localOnly, false)
      assert.equal(result.declarationOffset, offset)
      assert.match(markdown(result), /返回类型.*ResponseEntity<Resource>/)
    }
    assert.equal(at(text, 'raw', { offset: text.lastIndexOf('raw'), index }).declarationOffset, undefined, 'same-named calls must use language definition instead of borrowing a declaration')
  }
})

test('declared classes, interfaces, records, constructors, fields and locals use syntax evidence', () => {
  const text = 'interface Contract {} class Service implements Contract { private String name; public Service() {} public int run(final int count) { int total = count; return total; } } record Pair(String left, int right) {}'
  const expected = { Contract: '接口', Service: '类', name: '字段', run: '方法', count: '参数', total: '局部变量', Pair: '记录类', left: '记录组件', right: '记录组件' }
  for (const [word, category] of Object.entries(expected)) assert.equal(at(text, word).category, category, word)
  assert.equal(at(text, 'Service', { offset: text.indexOf('Service()') }).category, '构造器')
  assert.equal(at(text, 'total', { offset: text.lastIndexOf('total') }).category, '局部变量')
  assert.match(markdown(at(text, 'name')), /尚未经过类型解析/)
})

test('all reserved words and literals get local documentation', () => {
  const text = 'abstract public protected private static final native synchronized transient volatile strictfp boolean byte short int long char float double void class interface enum extends implements new this super instanceof package import if else switch case default for while do break continue return throw throws try catch finally assert const goto _ true false null'
  for (const word of text.split(' ')) {
    const result = at(text, word, { offset: text.split(' ').slice(0, text.split(' ').indexOf(word)).join(' ').length + (text.startsWith(word) ? 0 : 1) })
    assert.equal(result.localOnly, true, word)
    assert.doesNotMatch(result.category, /尚未解析/, word)
  }
})

test('contextual words are keywords only in their relevant grammar positions', () => {
  const text = 'sealed class Shape permits Circle {} non-sealed class Circle extends Shape {} record Point(int x) {} class X { void run() { var value = 1; obj.record(); obj.var(); obj.yield(); int record = 1; int yield = 2; } }'
  assert.equal(at(text, 'sealed').localOnly, true)
  assert.equal(at(text, 'non-sealed').localOnly, true)
  assert.equal(at(text, 'permits').localOnly, true)
  assert.equal(at(text, 'record').localOnly, true)
  assert.equal(at(text, 'var').localOnly, true)
  for (const word of ['record', 'var', 'yield']) {
    const result = at(text, word, { offset: text.indexOf(`obj.${word}`) + 4 })
    assert.equal(result.category, '方法调用', word)
    assert.equal(result.localOnly, false, word)
  }
  assert.equal(at(text, 'record', { offset: text.lastIndexOf('record') }).category, '局部变量')
  assert.equal(at(text, 'yield', { offset: text.lastIndexOf('yield') }).category, '局部变量')
  const switches = 'int v = switch (x) { case 1 -> { yield 42; } default -> 0; };'
  assert.equal(at(switches, 'yield').category, '控制流关键字')
})

test('module words have local documentation within module declarations', () => {
  const text = 'open module app.core { requires transitive lib.core; exports app.api to client.app; opens app.data; uses app.Service; provides app.Service with app.Impl; }'
  for (const word of ['open', 'module', 'requires', 'transitive', 'exports', 'to', 'opens', 'uses', 'provides', 'with']) assert.equal(at(text, word).localOnly, true, word)
  assert.equal(at('Object module = value;', 'module').category, '变量')
})

test('comments, escaped strings, characters and multiline text blocks do not become code words', () => {
  const cases = [
    ['// class Customer\nint x;', 'Customer', '注释'],
    ['/* public\nclass Foo {} */ int x;', 'public', '注释'],
    ['/** @param value the value */', 'value', '文档注释'],
    ['String x = "public \\"; class C {}', 'public', '字符串字面量'],
    ['String x = "class \\"Foo";', 'class', '字符串字面量'],
    ["char c = 'x';", 'x', '字符字面量'],
    ['String s = """\nclass Fake { public void run() {} }\n""";', 'Fake', '文本块字面量'],
  ]
  for (const [text, word, category] of cases) {
    const result = at(text, word, { semanticToken: { type: 'class' } })
    assert.equal(result.category, category, text)
    assert.equal(result.localOnly, true)
  }
})

test('authoritative JDT semantic categories and modifiers take precedence over inferred roles', () => {
  const text = 'Example item; item.run(); @RequestParam(required=false) String value;'
  assert.equal(at(text, 'Example', { semanticToken: { type: 'interface', modifiers: [] } }).category, '接口')
  const field = at(text, 'item', { semanticToken: { type: 'property', modifiers: ['private', 'static', 'readonly', 'declaration'] } })
  assert.equal(field.category, '字段')
  assert.match(markdown(field), /private（私有）.*静态.*final.*声明位置/)
  const finalClass = at(text, 'Example', { semanticToken: { type: 'class', modifiers: ['readonly', 'public'] } })
  assert.match(markdown(finalClass), /final.*public/)
  assert.doesNotMatch(markdown(finalClass), /只读|不可变/)
  for (const [type, category] of [['decorator', '注解'], ['annotation', '注解'], ['annotationMember', '注解属性'], ['record', '记录类'], ['recordComponent', '记录组件']]) {
    assert.equal(at(text, 'Example', { semanticToken: { type, modifiers: [] } }).category, category)
  }
  assert.equal(at(text, 'run', { semanticToken: { type: 'method', modifiers: ['constructor'] } }).category, '构造器')
  assert.equal(at(text, 'required', { semanticToken: { type: 'property', modifiers: [] } }).category, '注解属性')
  assert.equal(at(text, 'required', { semanticToken: { type: 'method', modifiers: [] } }).category, '注解属性')
  assert.equal(at('public int count;', 'public', { semanticToken: { type: 'variable' } }).category, '访问修饰符')
})

test('LSP signature evidence supplies real types without guessing from uppercase names', () => {
  const text = 'unknown; UPPERCASE; run(); count;'
  assert.equal(at(text, 'UPPERCASE').category, '标识符（语义尚未解析）')
  const result = at(text, 'run', { hoverContents: { kind: 'markdown', value: '```java\npublic java.util.List<String> example.Service.run(int count)\n```' } })
  assert.equal(result.category, '方法')
  assert.match(markdown(result), /`java.util.List<String>`/)
  assert.equal(at(text, 'count', { hoverContents: [{ language: 'java', value: 'private int example.Service.count' }] }).category, '字段')
  assert.equal(at(text, 'unknown', { hoverContents: 'public interface unknown' }).category, '接口')
  assert.equal(at(text, 'unknown', { hoverContents: 'This unknown method could be anything.' }).category, '标识符（语义尚未解析）')
})

test('member names are not resolved against unrelated same-named local variables', () => {
  const text = 'class A { String field; void run() { int value = 1; other.value; this.field; } }'
  assert.equal(at(text, 'value', { offset: text.indexOf('other.value') + 6 }).category, '标识符（语义尚未解析）')
  assert.equal(at(text, 'field', { offset: text.indexOf('this.field') + 5 }).category, '字段')
})

test('Unicode identifiers preserve UTF-16 ranges and punctuation/whitespace do not borrow a neighbor', () => {
  const text = 'class 用户 { int 𐐀值 = 1; int $数额; }'
  assert.equal(at(text, '用户').category, '类')
  const result = at(text, '𐐀值')
  assert.equal(result.word, '𐐀值')
  assert.equal(result.end - result.start, 3)
  assert.equal(describeJavaToken({ text, offset: result.start + 1 }).word, '𐐀值')
  assert.equal(at(text, '$数额').category, '字段')
  for (const offset of [-1, text.length, text.indexOf('{'), text.indexOf(' '), 0.5]) assert.equal(describeJavaToken({ text, offset }), null, String(offset))
  assert.equal(describeJavaToken({ text, offset: result.end }), null)
})

test('cached indexes stay document-specific and stale indexes are replaced', () => {
  const text = 'class Alpha {}', index = createJavaTokenIndex(text)
  assert.equal(at(text, 'Alpha', { index }).category, '类')
  assert.equal(at('interface Beta {}', 'Beta', { index }).category, '接口')
  assert.equal(at(text, 'Alpha', { index }).category, '类')
  assert.equal(describeJavaToken(), null)
})

test('Spring RequestParam help requires qualified evidence and preserves annotation-member category', () => {
  const usage = '@RequestParam(required = false) String query;'
  assert.doesNotMatch(markdown(at(usage, 'RequestParam')), /Spring Web/)
  assert.doesNotMatch(markdown(at(usage, 'required')), /默认为 true/)
  const imported = 'import org.springframework.web.bind.annotation.RequestParam; ' + usage
  assert.match(markdown(at(imported, 'required')), /boolean.*默认为 true/)
  assert.match(markdown(at(usage, 'RequestParam', { hoverContents: 'org.springframework.web.bind.annotation.RequestParam' })), /绑定到方法参数/)
  const resolved = at(usage, 'required', { hoverContents: { language: 'java', value: 'boolean org.springframework.web.bind.annotation.RequestParam.required()' } })
  assert.equal(resolved.category, '注解属性')
  assert.match(markdown(resolved), /属性类型.*`boolean`/)
  const unrelated = 'import example.RequestParam; ' + usage
  assert.doesNotMatch(markdown(at(unrelated, 'required')), /Spring Web/)
  const comment = '// import org.springframework.web.bind.annotation.RequestParam;\n' + usage
  assert.doesNotMatch(markdown(at(comment, 'required')), /Spring Web/)
})

test('scope indexes resolve shadowing and scale with many distinct declarations', () => {
  const text = 'class Many { ' + Array.from({ length: 1200 }, (_, i) => `int field${i};`).join('\n') + ' void run(int arg) { int local = 1; { String local = "x"; use(local); } use(local); } }'
  const index = createJavaTokenIndex(text)
  assert.equal(index.declarations.size, 1202)
  assert.equal(at(text, 'field1199', { index }).category, '字段')
  assert.match(markdown(at(text, 'local', { index, offset: text.indexOf('use(local)') + 4 })), /`String`/)
  assert.match(markdown(at(text, 'local', { index, offset: text.lastIndexOf('use(local)') + 4 })), /`int`/)
})

test('this-qualified names resolve only fields of the current type despite parameter shadowing', () => {
  const text = 'class A { String value; void run(int value) { use(this.value); use(value); } }'
  const field = at(text, 'value', { offset: text.indexOf('this.value') + 5 })
  assert.equal(field.category, '字段')
  assert.match(markdown(field), /`String`/)
  const parameter = at(text, 'value', { offset: text.lastIndexOf('use(value)') + 4 })
  assert.equal(parameter.category, '参数')
  assert.match(markdown(parameter), /`int`/)
})

test('for declarations stop at the loop statement and do not shadow later field references', () => {
  for (const body of ['{ use(value); }', 'use(value);']) {
    const text = `class A { String value; void run() { for (int value=0; value<2; value++) ${body} use(value); } }`
    const within = at(text, 'value', { offset: text.indexOf('use(value)') + 4 })
    assert.equal(within.category, '局部变量')
    assert.match(markdown(within), /`int`/)
    const outside = at(text, 'value', { offset: text.lastIndexOf('use(value)') + 4 })
    assert.equal(outside.category, '字段')
    assert.match(markdown(outside), /`String`/)
  }
  const uncertain = 'class A { String value; void run() { for (int value=0; value<2; value++) if (ready) use(value); use(value); } }'
  assert.equal(at(uncertain, 'value', { offset: uncertain.indexOf('use(value)') + 4 }).category, '标识符（语义尚未解析）')
})

test('try resources and catch parameters stay within their own bodies', () => {
  const text = 'class A { String value; void run() { try (Reader value = open()) { use(value); } catch (Exception value) { use(value); } finally { use(value); } use(value); } }'
  const uses = [...text.matchAll(/use\(value\)/g)].map(match => match.index + 4)
  const resource = at(text, 'value', { offset: uses[0] })
  assert.equal(resource.category, '局部变量')
  assert.match(markdown(resource), /`Reader`/)
  const caught = at(text, 'value', { offset: uses[1] })
  assert.equal(caught.category, '参数')
  assert.match(markdown(caught), /`Exception`/)
  for (const offset of uses.slice(2)) {
    const outside = at(text, 'value', { offset })
    assert.equal(outside.category, '字段')
    assert.match(markdown(outside), /`String`/)
  }
})

test('line comments end at CR, LF or CRLF without swallowing following Java code', () => {
  for (const newline of ['\r', '\n', '\r\n']) {
    const text = '// comment' + newline + 'class A { int field; }'
    assert.equal(at(text, 'comment').category, '注释')
    assert.equal(at(text, 'A').category, '类')
    assert.equal(at(text, 'field').category, '字段')
  }
})

test('plain hover prose is never treated as a variable type or method signature', () => {
  for (const hoverContents of [
    { kind: 'markdown', value: 'Returns value' },
    { kind: 'plaintext', value: 'The value' },
    'Returns value',
    { kind: 'markdown', value: 'A description of class value' },
    { kind: 'markdown', value: '```text\nReturns value\n```' },
  ]) {
    const result = at('value;', 'value', { hoverContents })
    assert.equal(result.category, '标识符（语义尚未解析）')
    assert.doesNotMatch(markdown(result), /\*\*类型：/)
  }
  const typed = at('value;', 'value', { hoverContents: { language: 'java', value: 'String value' } })
  assert.equal(typed.category, '变量')
  assert.match(markdown(typed), /`String`/)
  const fenced = at('value;', 'value', { hoverContents: { kind: 'markdown', value: 'Some documentation.\n\n```java\nString value\n```\n\nMore documentation.' } })
  assert.equal(fenced.category, '变量')
  assert.match(markdown(fenced), /`String`/)
})

test('local class fields belong to that class and annotation member declarations are attributes', () => {
  const text = 'class A { void m() { class Inner { int field; void n() { String local; } } } }'
  assert.equal(at(text, 'field').category, '字段')
  assert.equal(at(text, 'local').category, '局部变量')
  const annotation = '@interface Demo { boolean required() default false; String name(); }'
  assert.equal(at(annotation, 'Demo').category, '注解类型')
  const required = at(annotation, 'required')
  assert.equal(required.category, '注解属性')
  assert.match(markdown(required), /属性类型.*`boolean`/)
  assert.equal(at(annotation, 'name').category, '注解属性')
})

test('GetMapping in the screenshot import has a concrete HTTP purpose without losing its role', () => {
  const text = 'import org.springframework.web.bind.annotation.GetMapping; class Api { @GetMapping("/items") public void list() {} }'
  for (const offset of [text.indexOf('GetMapping'), text.lastIndexOf('GetMapping')]) {
    const result = at(text, 'GetMapping', { offset })
    assert.match(markdown(result), /\*\*作用：\*\*.*HTTP.*GET/)
    assert.doesNotMatch(result.purpose, /功能描述待补充/)
  }
  assert.doesNotMatch(markdown(at('import demo.GetMapping; @GetMapping class Api {}', 'GetMapping')), /HTTP.*GET/)
})

test('documented Java declarations and parameter references retain their purpose with semantic tokens', () => {
  const text = '/** 管理资料的读取接口。 */ class Api { /** 资料总数。 */ private int count; /** 查询资料。\n * @param id 资料的唯一编号。\n */ public int find(int id) { return id; } }'
  const samples = [
    ['Api', text.indexOf('Api'), { type: 'class' }, /管理资料的读取接口/],
    ['count', text.indexOf('count'), { type: 'property' }, /资料总数/],
    ['find', text.indexOf('find'), { type: 'method' }, /查询资料/],
    ['id', text.indexOf('int id') + 4, { type: 'parameter' }, /资料的唯一编号/],
    ['id', text.lastIndexOf('id'), { type: 'parameter' }, /资料的唯一编号/],
  ]
  for (const [word, offset, semanticToken, expected] of samples) {
    const result = at(text, word, { offset, semanticToken })
    assert.match(result.purpose, expected, word)
    assert.match(markdown(result), /作用依据：本文件 Javadoc/)
  }
  assert.doesNotMatch(at(text, 'Api', { semanticToken: { type: 'parameter' } }).purpose, /管理资料的读取接口/, 'contradictory semantic identity cannot borrow local type documentation')
})

test('signature-only hovers keep a syntax purpose and do not present a JAR origin as purpose', () => {
  const text = 'work();'
  const result = at(text, 'work', { hoverContents: [
    { language: 'java', value: 'public int demo.Service.work()' },
    { kind: 'markdown', value: 'Source: *demo-1.0.jar*' },
  ] })
  assert.equal(result.category, '方法')
  assert.match(result.purpose, /可被调用/)
  assert.doesNotMatch(result.purpose, /功能描述/)
  assert.doesNotMatch(result.purpose, /Source|demo-1.0.jar/)
})

test('translated nine-argument Set.of purpose retains generic result types and parameter names', () => {
  const text = 'import java.util.Set; class C { Set<String> values = Set.of("a", "b", "c", "d", "e", "f", "g", "h", "i"); }'
  const signature = 'public static Set<String> java.util.Set.of(String e1, String e2, String e3, String e4, String e5, String e6, String e7, String e8, String e9)'
  const hoverContents = [{ language: 'java', value: signature }, { kind: 'markdown', value: '返回包含九个元素的不可修改集合。\n\n参数 `e1` 到 `e9` 是集合元素。' }]
  const result = at(text, 'of', { hoverContents, documentationTranslation: { language: 'zh-CN', source: 'configured-model' } })
  assert.equal(result.purpose, '返回包含九个元素的不可修改集合。')
  assert.match(markdown(result), /返回类型.*`Set<String>`/)
  assert.match(markdown(result), /作用依据：语言服务器文档（模型中文翻译）/)
  assert.equal(text.slice(result.start, result.end), 'of')
  assert.equal(hoverContents[0].value, signature)
})

test('translated language-server prose does not displace verified Chinese library help', () => {
  const text = 'import org.springframework.web.bind.annotation.GetMapping;'
  const result = at(text, 'GetMapping', { hoverContents: [
    { language: 'java', value: 'org.springframework.web.bind.annotation.GetMapping' },
    { kind: 'markdown', value: '语言服务器返回的另一份中文说明。' },
  ], documentationTranslation: { language: 'zh-CN', source: 'configured-model' } })
  assert.match(result.purpose, /HTTP.*GET/)
  assert.match(markdown(result), /作用依据：官方 API 摘要/)
  assert.doesNotMatch(markdown(result), /作用依据：.*模型中文翻译/)
})
