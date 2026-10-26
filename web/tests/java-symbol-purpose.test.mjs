import assert from 'node:assert/strict'
import test from 'node:test'
import { createJavaTokenIndex } from '../src/lib/javaTokenDocumentation.js'
import { resolveJavaSymbolPurpose } from '../src/lib/javaSymbolPurpose.js'

function fixture(text, word, occurrence = 0) {
  const index = createJavaTokenIndex(text)
  const token = index.tokens.filter(item => item.word === word)[occurrence]
  assert.ok(token, `fixture contains ${word} at occurrence ${occurrence}`)
  const role = index.roles.get(token.codeIndex)
  return { index, token, role }
}

test('class, interface, method, constructor and field purposes come from their own Javadoc', () => {
  const cases = [
    ['/** 保存上传文件并提供下载入口。 */ public class Storage {}', 'Storage', '保存上传文件并提供下载入口。'],
    ['/** 约定可重复读取文档的能力。 */ public interface Reader {}', 'Reader', '约定可重复读取文档的能力。'],
    ['class C { /** 按照已有筛选条件返回文档。 */ public String find(int id) { return ""; } }', 'find', '按照已有筛选条件返回文档。'],
    ['class C { /** 初始化缓存所需的依赖。 */ public C() {} }', 'C', '初始化缓存所需的依赖。', 1],
    ['class C { /** 保存当前页面的查询条件。 */ private String query; }', 'query', '保存当前页面的查询条件。'],
  ]
  for (const [text, word, expected, occurrence = 0] of cases) {
    assert.deepEqual(resolveJavaSymbolPurpose(fixture(text, word, occurrence)), { text: expected, source: '本文件 Javadoc' })
  }
})

test('declaration Javadoc stays attached across annotations, generics and access modifiers', () => {
  const text = 'class C { /** 返回所有可用的结果。 */ @SuppressWarnings({"unchecked", "rawtypes"}) @Deprecated public static final <T> java.util.List<T> read() { return null; } }'
  assert.equal(resolveJavaSymbolPurpose(fixture(text, 'read')).text, '返回所有可用的结果。')
  assert.equal(resolveJavaSymbolPurpose(fixture('/** 声明供调用者设置的标记。 */ public @interface Mark {}', 'Mark')).text, '声明供调用者设置的标记。')
})

test('method parameter uses only its owner method exact @param tag, including multiline descriptions', () => {
  const text = `class C {
    /** 根据给定分类查询文档。
     * @param categoryId 指定需要查询的分类编号，
     *                   为空时查询全部分类。
     * @param kw 用于筛选标题的文本。
     * @return 匹配的文档列表。
     */
    public String list(Long categoryId, String kw) { return kw; }
  }`
  assert.deepEqual(resolveJavaSymbolPurpose(fixture(text, 'categoryId')), { text: '指定需要查询的分类编号， 为空时查询全部分类。', source: '本文件 Javadoc @param' })
  assert.equal(resolveJavaSymbolPurpose(fixture(text, 'kw')).text, '用于筛选标题的文本。')
  assert.equal(resolveJavaSymbolPurpose(fixture(text, 'list')).text, '根据给定分类查询文档。')
})

test('record components take exact @param descriptions from the record declaration', () => {
  const text = '/** 表示显示坐标。\n * @param x 横向位置。\n * @param y 纵向位置。\n */ public record Point(int x, int y) {}'
  assert.deepEqual(resolveJavaSymbolPurpose(fixture(text, 'x')), { text: '横向位置。', source: '本文件 Javadoc @param' })
  assert.equal(resolveJavaSymbolPurpose(fixture(text, 'y')).text, '纵向位置。')
})

test('same named parameters never borrow docs from another method or a generic type parameter', () => {
  const text = `class C {
    /** @param value 写入文本的内容。 */ void write(String value) {}
    /** @param <value> 泛型值的类型。 */ <value> void generic(String value) {}
    void other(String value) {}
  }`
  const index = createJavaTokenIndex(text)
  const declarations = index.declarations.get('value')
  assert.equal(declarations.length, 3)
  const results = declarations.map(declaration => resolveJavaSymbolPurpose({ index, token: index.tokens[declaration.i], role: declaration.role }))
  assert.equal(results[0].text, '写入文本的内容。')
  assert.equal(results[1], null)
  assert.equal(results[2], null)
})

test('catch and lambda parameters never borrow the surrounding method @param documentation', () => {
  const text = 'class C { /** @param value 文档中的方法参数。 */ void run(String value) { try {} catch (Exception value) {} consume((String value) -> value); } }'
  const index = createJavaTokenIndex(text)
  const declarations = index.declarations.get('value')
  assert.equal(declarations.length, 3)
  for (const declaration of declarations.slice(1)) assert.equal(resolveJavaSymbolPurpose({ index, token: index.tokens[declaration.i], role: declaration.role }), null)
})

test('resolved field references preserve declaration identity under shadowing and across unrelated receivers', () => {
  const text = 'class C { /** 持有当前分类的显示名称。 */ String value; void run(String value) { use(value); use(this.value); use(other.value); } }'
  const index = createJavaTokenIndex(text)
  const [field, parameter] = index.declarations.get('value')
  const references = index.tokens.filter(token => token.word === 'value').slice(2)
  assert.equal(resolveJavaSymbolPurpose({ index, token: references[0], role: { ...parameter.role, declaration: false } }), null)
  assert.equal(resolveJavaSymbolPurpose({ index, token: references[1], role: { ...field.role, declaration: false } }).text, '持有当前分类的显示名称。')
  assert.equal(resolveJavaSymbolPurpose({ index, token: references[2], role: { category: '字段' } }), null)
})

test('a bare method call cannot borrow a same-named declaration purpose', () => {
  const text = 'class C { /** 校验登录令牌。 */ void check(String input) {} void run() { other.check(1); check("a"); } }'
  const index = createJavaTokenIndex(text)
  const tokens = index.tokens.filter(token => token.word === 'check')
  for (const token of tokens.slice(1)) assert.equal(resolveJavaSymbolPurpose({ index, token, role: index.roles.get(token.codeIndex) }), null)
})

test('unrelated comments and declaration boundaries do not attach to the next symbol', () => {
  const cases = [
    ['class C { /** 持有之前的值。 */ String previous; String value; }', 'value'],
    ['class C { /** 计算之前的值。 */ void previous() {} String value; }', 'value'],
    ['class C { // 这里提到 value，但并非声明文档。\n String value; }', 'value'],
    ['class C { String previous; // 作用：保存之前的值。\n String value; }', 'value'],
    ['class C { /** 文件末尾说明。 */ } class Next {}', 'Next'],
    ['/** 遥远的旧说明。 */\n\n\n class Next {}', 'Next'],
  ]
  for (const [text, word] of cases) assert.equal(resolveJavaSymbolPurpose(fixture(text, word)), null, text)
})

test('ordinary adjacent comments require an explicit purpose label', () => {
  const cases = [
    ['class C {\n // 作用：保存最后一次成功结果。\n String value; }', '保存最后一次成功结果。'],
    ['class C {\n /* 用途：标记当前请求是否需要缓存。 */\n boolean value; }', '标记当前请求是否需要缓存。'],
    ['class C {\n // purpose: Holds the last successful result.\n String value; }', 'Holds the last successful result.'],
  ]
  for (const [text, expected] of cases) assert.deepEqual(resolveJavaSymbolPurpose(fixture(text, 'value')), { text: expected, source: '本文件说明注释' })
})

test('tag-only Javadoc without a matching parameter purpose returns no fabricated summary', () => {
  const text = 'class C { /** @return 已保存的值。\n * @since 1.0\n */ String get(String value) { return value; } }'
  assert.equal(resolveJavaSymbolPurpose(fixture(text, 'get')), null)
  assert.equal(resolveJavaSymbolPurpose(fixture(text, 'value')), null)
})

test('language server prose supplies purpose while signatures, FQNs and jar provenance do not', () => {
  const input = fixture('@GetMapping class C {}', 'GetMapping')
  const hoverContents = [
    { language: 'java', value: 'public @interface org.springframework.web.bind.annotation.GetMapping' },
    { kind: 'markdown', value: 'org.springframework.web.bind.annotation.GetMapping\n\nMaps HTTP GET requests onto specific handler methods.\n\nSource: *spring-web-6.2.19.jar*' },
  ]
  assert.deepEqual(resolveJavaSymbolPurpose({ ...input, hoverContents }), { text: 'Maps HTTP GET requests onto specific handler methods.', source: '语言服务器文档' })
  for (const hoverContents of [
    'org.springframework.web.bind.annotation.GetMapping\n\nSource: *spring-web-6.2.19.jar*',
    { language: 'java', value: 'public String example.C.value' },
    { kind: 'plaintext', value: 'public String example.C.get(int id)\nSource: C.java' },
    { kind: 'markdown', value: '```java\nString value = read();\n// Describes only an example.\n```\n\nSource: C.java' },
    { kind: 'plaintext', value: '```java\nString value = read();\n// Describes only an example.\n```' },
    { kind: 'plaintext', value: 'String 示例.用户.名称' },
    { kind: 'plaintext', value: '@自定义注解' },
    'spring-web-6.2.19.jar',
  ]) assert.equal(resolveJavaSymbolPurpose({ ...input, hoverContents }), null)
})

test('translated symbol documentation can replace an attached English Javadoc without changing author Chinese', () => {
  const english = 'Returns an unmodifiable set containing nine elements.'
  const text = `interface Set<E> { /** ${english} */ static <E> Set<E> of(E e1, E e2, E e3, E e4, E e5, E e6, E e7, E e8, E e9) { return null; } }`
  const input = fixture(text, 'of')
  const signature = 'public static <E> Set<E> java.util.Set.of(E e1, E e2, E e3, E e4, E e5, E e6, E e7, E e8, E e9)'
  const hoverContents = [{ language: 'java', value: signature }, { kind: 'markdown', value: '返回包含九个元素的不可修改集合。' }]
  const documentationTranslation = { language: 'zh-CN', source: 'configured-model' }
  const translated = resolveJavaSymbolPurpose({ ...input, hoverContents, documentationTranslation })
  assert.deepEqual(translated, { text: '返回包含九个元素的不可修改集合。', source: '语言服务器文档', translation: documentationTranslation })
  assert.equal(hoverContents[0].value, signature)

  for (const metadata of [undefined, { language: 'en', source: 'configured-model' }, { language: 'zh-CN', source: 'unknown' }]) {
    assert.deepEqual(resolveJavaSymbolPurpose({ ...input, hoverContents, documentationTranslation: metadata }), { text: english, source: '本文件 Javadoc' })
  }
  const chinese = fixture(text.replace(english, '返回当前业务允许使用的九个项目。'), 'of')
  assert.deepEqual(resolveJavaSymbolPurpose({ ...chinese, hoverContents, documentationTranslation }), { text: '返回当前业务允许使用的九个项目。', source: '本文件 Javadoc' })
  assert.deepEqual(resolveJavaSymbolPurpose({ ...input, hoverContents: english, documentationTranslation }), { text: english, source: '本文件 Javadoc' }, 'a success marker alone cannot replace the author with untranslated prose')
})

test('translated parameter descriptions replace only their exact attached English parameter purpose', () => {
  const input = fixture('class C { /** @param value the value to store */ void store(String value) {} }', 'value')
  const result = resolveJavaSymbolPurpose({ ...input,
    hoverContents: { kind: 'plaintext', value: '需要保存的值。' },
    documentationTranslation: { language: 'zh-CN', source: 'configured-model' } })
  assert.equal(result.text, '需要保存的值。')
  assert.equal(result.translation.source, 'configured-model')
})

test('a signature and provenance in the same paragraph are removed before selecting server prose', () => {
  const input = fixture('class C { String value; }', 'value')
  assert.equal(resolveJavaSymbolPurpose({ ...input, hoverContents: { kind: 'markdown', value: 'String example.C.value\nHolds the last acknowledged response.\nSource: C.java' } }).text, 'Holds the last acknowledged response.')
})

test('local attached purpose takes precedence over server prose, without using role metadata from a different category', () => {
  const input = fixture('class C { /** 保存已确认的响应。 */ String value; }', 'value')
  const hoverContents = 'A different response described by the server.'
  assert.equal(resolveJavaSymbolPurpose({ ...input, hoverContents }).source, '本文件 Javadoc')
  assert.equal(resolveJavaSymbolPurpose({ ...input, role: { ...input.role, category: '参数' }, hoverContents }).source, '语言服务器文档')
})

test('a generic server variable category retains parameter or field purposes but cannot borrow class docs', () => {
  const parameter = fixture('class C { /** @param value 用来筛选标题的文本。 */ void run(String value) {} }', 'value')
  const role = { ...parameter.role, category: '变量' }
  assert.deepEqual(resolveJavaSymbolPurpose({ ...parameter, role }), { text: '用来筛选标题的文本。', source: '本文件 Javadoc @param' })
  assert.equal(role.category, '变量')
  const field = fixture('class C { /** 保存当前分类编号。 */ int value; }', 'value')
  assert.equal(resolveJavaSymbolPurpose({ ...field, role: { ...field.role, category: '变量' } }).text, '保存当前分类编号。')
  const type = fixture('/** 管理已有资料。 */ class C {}', 'C')
  assert.equal(resolveJavaSymbolPurpose({ ...type, role: { ...type.role, category: '参数' } }), null)
  assert.equal(resolveJavaSymbolPurpose({ ...type, role: { ...type.role, category: '变量' } }), null)
})

test('literals, keywords and modifiers cannot acquire arbitrary server prose', () => {
  const index = createJavaTokenIndex('class C { int value = 3; String text = "example"; unresolved; }')
  for (const [token, role] of [
    [index.tokens.find(token => token.word === 'int'), { category: '基本类型' }],
    [index.tokens.find(token => token.word === 'class'), { category: '类型声明关键字' }],
    [index.tokens.find(token => token.word === '3'), { category: '数字字面量' }],
    [index.allTokens.find(token => token.kind === 'string'), { category: '字符串字面量' }],
  ]) assert.equal(resolveJavaSymbolPurpose({ index, token, role, hoverContents: 'Performs a very important business operation.' }), null)
  assert.equal(resolveJavaSymbolPurpose(), null)
})

test('an unresolved identifier can use actual server documentation without inferring its category', () => {
  const index = createJavaTokenIndex('unresolved;')
  const token = index.tokens[0]
  const role = { category: '标识符（语义尚未解析）' }
  assert.deepEqual(resolveJavaSymbolPurpose({ index, token, role, hoverContents: 'Holds the last acknowledged response.' }), { text: 'Holds the last acknowledged response.', source: '语言服务器文档' })
  assert.equal(role.category, '标识符（语义尚未解析）')
  assert.equal(resolveJavaSymbolPurpose({ index, token, role, hoverContents: 'example.Response\n\nSource: response-1.0.jar' }), null)
})

test('summaries retain Javadoc display text without active HTML, images, or command links', () => {
  const local = fixture('/** 对 {@link java.util.List 列表} 中的 {@code List<String>} 元素执行 <b>校验</b>。[检查](command:run) ![hidden](https://example.com/pixel.png) <script>bad()</script> */ class C {}', 'C')
  const purpose = resolveJavaSymbolPurpose(local)
  assert.equal(purpose.text, '对 列表 中的 List＜String＞ 元素执行 校验。检查')
  assert.doesNotMatch(purpose.text, /command:|https:|<|!\[|script|bad\(\)/)
  const remote = resolveJavaSymbolPurpose({ ...local, role: { category: '类' }, hoverContents: { kind: 'markdown', value: 'Provides [lookup](command:run) for ![tracking](https://example.com/pixel.png) records. <img src="https://example.com/x">' } })
  assert.equal(remote.text, 'Provides lookup for records.')
})

test('purpose summaries are bounded by Unicode characters and are cached per document index', () => {
  const first = fixture('/** ' + '🙂'.repeat(800) + ' */ class C {}', 'C')
  const result = resolveJavaSymbolPurpose(first)
  assert.equal(Array.from(result.text).length, 360)
  assert.equal(result.text.endsWith('…'), true)
  assert.equal(resolveJavaSymbolPurpose(first).text, result.text)
  const other = fixture('/** 独立文档中的不同用途。 */ class C {}', 'C')
  assert.equal(resolveJavaSymbolPurpose(other).text, '独立文档中的不同用途。')
})
