import test from 'node:test'
import assert from 'node:assert/strict'
import { createJavaTokenIndex } from '../src/lib/javaTokenDocumentation.js'
import { resolveJavaBuiltinPurpose } from '../src/lib/javaBuiltinPurpose.js'

const WEB = 'org.springframework.web.bind.annotation'
function help(text, word, { occurrence = -1, hoverContents, role } = {}) {
  const index = createJavaTokenIndex(text)
  const choices = index.tokens.filter(token => token.word === word)
  const token = choices.at(occurrence)
  return resolveJavaBuiltinPurpose({ index, token, role: role || index.roles.get(token?.codeIndex), hoverContents })
}

test('Spring mappings explain their specific HTTP method at import and annotation positions', () => {
  for (const [name, verb] of [['GetMapping', 'GET'], ['PostMapping', 'POST'], ['PutMapping', 'PUT'], ['DeleteMapping', 'DELETE'], ['PatchMapping', 'PATCH']]) {
    const text = `import ${WEB}.${name}; class Demo { @${name}("/users") void users() {} }`
    for (const occurrence of [0, 1]) {
      const result = help(text, name, { occurrence })
      assert.match(result.text, new RegExp(`HTTP ${verb}`))
      assert.equal(result.category, '注解')
      assert.equal(result.url, `https://docs.spring.io/spring-framework/docs/6.2.x/javadoc-api/org/springframework/web/bind/annotation/${name}.html`)
    }
  }
})

test('common Spring web and injection annotations have concrete purpose summaries', () => {
  for (const [name, pattern] of [
    ['RequestMapping', /HTTP 方法/], ['RequestParam', /请求参数/], ['PathVariable', /路径模板/],
    ['RequestBody', /请求体/], ['ResponseBody', /响应体/], ['RestController', /控制器/],
    ['RequestHeader', /请求头/], ['ResponseStatus', /状态码/],
  ]) assert.match(help(`@${WEB}.${name} class Demo {}`, name).text, pattern)
  for (const [name, pkg, pattern] of [
    ['Autowired', 'org.springframework.beans.factory.annotation', /注入依赖/],
    ['Component', 'org.springframework.stereotype', /组件扫描/],
    ['Service', 'org.springframework.stereotype', /服务层组件/],
  ]) assert.match(help(`import ${pkg}.${name}; @${name} class Demo {}`, name).text, pattern)
})

test('attribute purpose is tied to its verified annotation owner', () => {
  const result = help(`import ${WEB}.RequestParam; class Demo { void users(@RequestParam(required = false) Long id) {} }`, 'required')
  assert.equal(result.category, '注解属性')
  assert.match(result.text, /Spring Web：boolean/)
  assert.match(result.text, /默认为 true/)
  assert.match(result.text, /允许请求不提供该参数/)
  assert.match(help(`@${WEB}.GetMapping(produces = "application/json") class Demo {}`, 'produces').text, /Accept/)
  assert.match(help(`@${WEB}.ResponseStatus(code = HttpStatus.CREATED) class Demo {}`, 'code').text, /状态码/)
  assert.equal(help('class Demo { boolean required = true; }', 'required'), null)
  assert.equal(help('@local.RequestParam(required = false) class Demo {}', 'required'), null)
  assert.equal(help('class Demo { @RequestParam(required = false) void users(Long id) {} }', 'required'), null)
})

test('bare and complete Java type signatures supply identity, prose mentions do not', () => {
  const text = '@GetMapping class Demo {}'
  for (const hoverContents of [
    `${WEB}.GetMapping`,
    { language: 'java', value: `public @interface ${WEB}.GetMapping` },
    { kind: 'markdown', value: `\`\`\`java\npublic @interface ${WEB}.GetMapping\n\`\`\`` },
  ]) assert.match(help(text, 'GetMapping', { hoverContents }).text, /HTTP GET/)
  for (const hoverContents of [
    `See ${WEB}.GetMapping for details.`,
    { kind: 'markdown', value: `This class resembles \`${WEB}.GetMapping\`.` },
    { language: 'text', value: `public @interface ${WEB}.GetMapping` },
    { language: 'java', value: `// example of ${WEB}.GetMapping\npublic @interface ${WEB}.GetMapping` },
  ]) assert.equal(help(text, 'GetMapping', { hoverContents }), null)
  const attribute = '@RequestParam(required = false) class Demo {}'
  assert.match(help(attribute, 'required', { hoverContents: { language: 'java', value: `public abstract boolean ${WEB}.RequestParam.required()` } }).text, /非必填/)
  assert.equal(help(attribute, 'required', { hoverContents: `Returns ${WEB}.RequestParam.required()` }), null)
})

test('explicit custom identities, duplicate imports, local types and generic parameters avoid builtin claims', () => {
  for (const text of [
    'import custom.GetMapping; @GetMapping class Demo {}',
    `import ${WEB}.GetMapping; import custom.GetMapping; @GetMapping class Demo {}`,
    `import ${WEB}.GetMapping; class GetMapping {} @GetMapping class Demo {}`,
    `import ${WEB}.GetMapping; @interface GetMapping {} @GetMapping class Demo {}`,
    `import ${WEB}.GetMapping; class Demo<GetMapping> { @GetMapping void users() {} }`,
    `import ${WEB}.GetMapping; class Demo { public <GetMapping> void users() { GetMapping value; } }`,
  ]) assert.equal(help(text, 'GetMapping'), null, text)
  assert.equal(help('@GetMapping class Demo {}', 'GetMapping', { hoverContents: { language: 'java', value: 'public @interface custom.GetMapping' } }), null)
  assert.equal(help('@GetMapping class Demo {}', 'GetMapping', { hoverContents: [`${WEB}.GetMapping`, 'custom.GetMapping'] }), null)
  assert.equal(help('import java.util.List; class Demo<List> { List items; }', 'List'), null)
  assert.equal(help('class String {} class Demo { String text; }', 'String'), null)
})

test('wildcard imports are accepted only when unambiguous', () => {
  assert.match(help(`import ${WEB}.*; @GetMapping class Demo {}`, 'GetMapping').text, /HTTP GET/)
  assert.equal(help(`import ${WEB}.*; import custom.*; @GetMapping class Demo {}`, 'GetMapping'), null)
  assert.equal(help('import custom.*; @GetMapping class Demo {}', 'GetMapping'), null)
  assert.equal(help('@GetMapping class Demo {}', 'GetMapping'), null)
  assert.equal(help('class Demo { List items; }', 'List'), null)
})

test('Java standard types and annotations show their specific purpose', () => {
  for (const [name, pattern] of [['String', /不可变/], ['Long', /64 位/], ['Integer', /32 位/], ['Boolean', /true 或 false/], ['Object', /根类/]]) {
    const result = help(`class Demo { ${name} value; }`, name)
    assert.match(result.text, pattern)
    assert.equal(result.category, '类')
    assert.match(result.source, /隐式导入.*推定/)
    assert.match(result.url, /docs\.oracle\.com/)
  }
  for (const [name, pattern, category] of [['List', /顺序/, '接口'], ['Map', /每个键/, '接口'], ['Set', /重复元素/, '接口'], ['Optional', /缺失/, '类']]) {
    const result = help(`import java.util.${name}; class Demo { ${name}<String> value; }`, name)
    assert.match(result.text, pattern)
    assert.equal(result.category, category)
  }
  for (const name of ['Override', 'Deprecated', 'SuppressWarnings', 'FunctionalInterface', 'SafeVarargs']) assert.equal(help(`@${name} class Demo {}`, name).category, '注解')
  for (const name of ['Target', 'Retention', 'Documented', 'Inherited', 'Repeatable']) assert.equal(help(`@java.lang.annotation.${name} class Demo {}`, name).category, '注解')
})

test('variables, methods, comments, static imports and unknown symbols do not inherit API purpose', () => {
  assert.equal(help('class Demo { int String; }', 'String'), null)
  assert.equal(help(`import ${WEB}.GetMapping; class Demo { void GetMapping() {} }`, 'GetMapping'), null)
  assert.equal(help(`import static ${WEB}.GetMapping; class Demo {}`, 'GetMapping'), null)
  assert.equal(help('class Demo { UnknownType item; }', 'UnknownType'), null)
  assert.equal(help(`import ${WEB}.GetMapping; class Demo { Object value() { return GetMapping; } }`, 'GetMapping'), null)
  assert.equal(resolveJavaBuiltinPurpose(), null)
  assert.equal(resolveJavaBuiltinPurpose({ index: createJavaTokenIndex('// GetMapping'), token: { word: 'GetMapping', kind: 'comment' } }), null)
})

test('cached evidence stays specific to a document version and retains repeated hover results', () => {
  const index = createJavaTokenIndex('import java.util.List; class Demo { List<String> items; }')
  const token = index.tokens.filter(token => token.word === 'List').at(-1)
  const input = { index, token, role: index.roles.get(token.codeIndex) }
  const initial = resolveJavaBuiltinPurpose(input)
  for (let i = 0; i < 20; i++) assert.deepEqual(resolveJavaBuiltinPurpose(input), initial)
  assert.equal(help('import java.util.List; class Demo<List> { List items; }', 'List'), null)
  assert.deepEqual(resolveJavaBuiltinPurpose(input), initial)
})
