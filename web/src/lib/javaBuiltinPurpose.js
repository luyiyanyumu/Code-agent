// Curated summaries of the linked official APIs. Resolution is local and
// conservative: a simple name alone is never enough for framework symbols.
const SPRING_API = 'https://docs.spring.io/spring-framework/docs/6.2.x/javadoc-api/'
const JAVA_API = 'https://docs.oracle.com/en/java/javase/21/docs/api/java.base/'
const WEB = 'org.springframework.web.bind.annotation'
const catalog = new Map()
function add(name, text, category, attributes = {}) {
  catalog.set(name, { text, category, attributes, url: (name.startsWith('java.') ? JAVA_API : SPRING_API) + name.replaceAll('.', '/') + '.html' })
}

const mappingAttributes = {
  value: '指定匹配的请求路径，与 path 互为别名，例如 "/users"。',
  path: '指定匹配的请求路径，与 value 互为别名；方法上的路径会结合类上的路径。',
  name: '为请求映射指定名称，便于引用此映射；它不是 URL 路径。',
  params: '限定请求参数条件，例如 "active=true" 或 "!active"；满足这些条件才匹配。',
  headers: '限定 HTTP 请求头条件，满足这些条件才匹配。',
  consumes: '限定处理方法接受的媒体类型，按请求的 Content-Type 匹配。',
  produces: '声明可返回的媒体类型，通常结合请求的 Accept 进行匹配。',
}
for (const [name, method, example] of [
  ['GetMapping', 'GET', '例如 @GetMapping("/users") 将 GET /users 交给此方法。'],
  ['PostMapping', 'POST', '常用于接收提交的数据。'],
  ['PutMapping', 'PUT', '用于声明处理 PUT 请求的接口。'],
  ['DeleteMapping', 'DELETE', '用于声明处理 DELETE 请求的接口。'],
  ['PatchMapping', 'PATCH', '用于声明处理 PATCH 请求的接口。'],
]) add(`${WEB}.${name}`, `Spring Web：将指定路径的 HTTP ${method} 请求映射到处理方法；相当于 @RequestMapping(method = RequestMethod.${method})。${example}`, '注解', mappingAttributes)
add(`${WEB}.RequestMapping`, 'Spring Web：按路径、HTTP 方法、请求参数、请求头和媒体类型把请求映射到控制器方法。类上可设置共同路径，方法上设置具体接口；未限定 method 时可匹配多种 HTTP 方法。', '注解', { ...mappingAttributes, method: '限定匹配的 HTTP 请求方法，例如 RequestMethod.GET；未指定时不作此限制。' })
add(`${WEB}.RequestParam`, 'Spring Web：将 HTTP 请求参数绑定到方法参数。Spring MVC 中可接收查询参数、表单参数等；name / value 指定参数名。required 默认为 true，设为 false 允许省略；defaultValue 提供缺失或空值时的默认值，并隐式设为非必填。', '注解', {
  value: 'String 类型的注解属性，指定要绑定的 HTTP 请求参数名称，与 name 互为别名。',
  name: 'String 类型的注解属性，指定要绑定的 HTTP 请求参数名称，与 value 互为别名。',
  required: 'boolean 类型的注解属性，默认为 true。设置 required = false 允许请求不提供该参数；提供 defaultValue 也会使它成为非必填。',
  defaultValue: 'String 类型的注解属性，在请求参数缺失或为空值时提供默认值；设置后会隐式将 required 设为 false。',
})
add(`${WEB}.PathVariable`, 'Spring Web：把路径模板中的变量绑定到方法参数，例如 "/users/{id}" 中的 id。name / value 指定变量名，required 默认为 true。', '注解', {
  value: '指定要绑定的路径变量名称，与 name 互为别名。',
  name: '指定要绑定的路径变量名称，与 value 互为别名。',
  required: 'boolean 类型的注解属性，默认为 true；设为 false 时允许路径变量缺失。',
})
add(`${WEB}.RequestBody`, 'Spring Web：通过消息转换器将 HTTP 请求体转换为方法参数，例如把 JSON 转为 Java 对象；required 默认为 true，表示需要请求体。', '注解', { required: 'boolean 类型的注解属性，默认为 true；设为 false 时允许请求体缺失，并可传入 null。' })
add(`${WEB}.ResponseBody`, 'Spring Web：把方法返回值写入 HTTP 响应体，通过消息转换器输出；返回值用于响应内容，而不是作为视图名称。用于类型时适用于该类型的处理方法。', '注解')
add(`${WEB}.RestController`, 'Spring Web：把类标记为控制器，同时使请求处理方法默认按 @ResponseBody 输出响应体；相当于组合 @Controller 和 @ResponseBody。', '注解', { value: '指定建议使用的 Spring 组件名称；它不是请求路径。' })
add(`${WEB}.RequestHeader`, 'Spring Web：将 HTTP 请求头绑定到方法参数，例如读取 Authorization。name / value 指定请求头名，required 默认为 true；defaultValue 可提供默认值并使其非必填。', '注解', {
  value: '指定要读取的 HTTP 请求头名称，与 name 互为别名。',
  name: '指定要读取的 HTTP 请求头名称，与 value 互为别名。',
  required: 'boolean 类型的注解属性，默认为 true；设为 false 时允许请求头缺失，提供 defaultValue 也会使其非必填。',
  defaultValue: '指定请求头缺失时的默认值，同时隐式将 required 设为 false。',
})
add(`${WEB}.ResponseStatus`, 'Spring Web：为处理方法或异常类型声明应返回的 HTTP 状态码，例如 HttpStatus.CREATED；不会覆盖 ResponseEntity 等已指定的状态。', '注解', {
  value: '指定 HTTP 响应状态码，与 code 互为别名；默认是 INTERNAL_SERVER_ERROR。',
  code: '指定 HTTP 响应状态码，与 value 互为别名；默认是 INTERNAL_SERVER_ERROR。',
  reason: '指定错误原因；非空时使用 Servlet 容器的 sendError，通常会生成错误页并忽略处理方法的返回值。',
})
add('org.springframework.beans.factory.annotation.Autowired', 'Spring：标记由容器注入依赖的构造器、字段或方法；通常根据依赖类型匹配 Bean。required 默认为 true。', '注解', { required: 'boolean 类型的注解属性，默认为 true；指定该依赖是否必须满足，设为 false 可声明可选注入。' })
for (const [name, text] of [
  ['Component', 'Spring：把类标记为组件，使其成为组件扫描发现并注册为 Bean 的候选；value 可指定组件名称。'],
  ['Service', 'Spring：把类标记为服务层组件，是 @Component 的专用形式，可通过组件扫描注册为 Bean；通常承载业务操作。'],
]) add(`org.springframework.stereotype.${name}`, text, '注解', { value: '指定建议使用的 Spring Bean 名称；默认交由容器命名。' })

for (const [name, text] of [
  ['String', 'Java 标准库：表示不可变的文本字符串。创建后内容不能更改，拼接或替换等操作不会修改原字符串。'],
  ['Long', 'Java 标准库：long 的包装类，用对象表示 64 位整数；可用于泛型集合，也可以为 null。提供整数解析、比较和转换方法。'],
  ['Integer', 'Java 标准库：int 的包装类，用对象表示 32 位整数；可用于泛型集合，也可以为 null。提供整数解析、比较和转换方法。'],
  ['Boolean', 'Java 标准库：boolean 的包装类，用对象表示 true 或 false；对象引用也可以为 null，拆箱前需保证不是 null。'],
  ['Object', 'Java 标准库：Java 类继承层次的根类，提供 equals、hashCode、toString 等所有对象共有的基础方法。'],
]) add(`java.lang.${name}`, text, '类')
for (const [name, text, category] of [
  ['List', 'Java 标准库：表示有顺序的元素序列，可按从 0 开始的下标访问；通常允许重复元素。具体实现决定是否可修改、是否接受 null。', '接口'],
  ['Map', 'Java 标准库：保存键到值的映射，每个键最多对应一个值。可根据键查询值；具体实现决定顺序、可修改性和 null 支持。', '接口'],
  ['Set', 'Java 标准库：表示不包含重复元素的集合；重复判定遵循集合实现的约定。顺序、可修改性和 null 支持取决于具体实现。', '接口'],
  ['Optional', 'Java 标准库：表示可能存在、也可能缺失的非 null 值，常用于方法返回值；通过 isPresent、map、orElse 等操作显式处理缺失。', '类'],
]) add(`java.util.${name}`, text, category)
for (const [name, text, attributes] of [
  ['Override', 'Java：表明方法意图重写父类型的方法；编译器会检查重写关系，名称或签名不匹配时报告错误。'],
  ['Deprecated', 'Java：标记不建议继续使用的 API，使编译器和工具提示弃用；since 可说明开始弃用的版本，forRemoval 表示是否计划移除。', { since: '记录此 API 开始弃用的版本，默认是空字符串。', forRemoval: 'boolean 类型的注解属性，默认为 false；为 true 表示此 API 计划在未来版本移除。' }],
  ['SuppressWarnings', 'Java：抑制被注解声明及其内部指定类别的编译器警告，例如 "unchecked"；不会修复对应问题或关闭编译错误。', { value: '指定要抑制的编译器警告名称，例如 "unchecked"，可填写多个名称。' }],
  ['FunctionalInterface', 'Java：声明接口应符合函数式接口规则，编译器会检查；符合规则的接口可以用于 lambda 或方法引用。'],
  ['SafeVarargs', 'Java：声明方法或构造器不会对可变参数执行潜在不安全操作，并抑制相关未检查警告；这是一项由开发者负责的安全承诺。'],
]) add(`java.lang.${name}`, text, '注解', attributes)
for (const [name, text, attributes] of [
  ['Target', 'Java：限定自定义注解可以使用的位置，例如类型、方法、字段或参数。', { value: '指定允许此注解使用的 ElementType 位置，可填写多个位置。' }],
  ['Retention', 'Java：指定自定义注解保留到源码、类文件还是运行时；未声明时默认保留到类文件。', { value: '指定 RetentionPolicy 保留策略；RUNTIME 表示运行时仍可通过反射读取。' }],
  ['Documented', 'Java：要求文档工具将使用了此注解的声明中的注解信息包含在生成的文档中。'],
  ['Inherited', 'Java：让查询类上的注解时可向父类查找此注解；不使接口注解或方法上的注解自动继承。'],
  ['Repeatable', 'Java：允许同一种注解在一个位置重复使用；value 指定用于保存这些重复注解的容器注解类型。', { value: '指定承载重复注解的容器注解类型。' }],
]) add(`java.lang.annotation.${name}`, text, '注解', attributes)

const simpleNames = new Map([...catalog].map(([name]) => [name.split('.').at(-1), name]))
const qualifiedNamePattern = /^[\p{L}_$][\p{L}\p{N}_$]*(?:\.[\p{L}_$][\p{L}\p{N}_$]*)+$/u
const signatureModifiers = /^(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|default)\s+)*/
function hoverParts(value) {
  if (Array.isArray(value)) return value.flatMap(hoverParts)
  if (value?.contents) return hoverParts(value.contents)
  if (value?.language?.toLowerCase() === 'java' && typeof value.value === 'string') return [{ java: true, text: value.value }]
  const text = typeof value === 'string' ? value : typeof value?.value === 'string' ? value.value : ''
  const parts = [...text.matchAll(/(?:^|\n)[ \t]*```java[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```/gi)].map(match => ({ java: true, text: match[1] }))
  // A complete standalone qualified type name is the JDT type hover format.
  const bare = text.trim().replace(/^`([^`]+)`$/, '$1')
  if (qualifiedNamePattern.test(bare)) parts.push({ java: false, text: bare })
  return parts
}
function hoverIdentity(contents, name, member) {
  const found = new Set()
  for (const part of hoverParts(contents)) {
    // Restrict identity evidence to the signature, not subsequent prose or examples.
    const line = part.text.split(/\r?\n/).map(value => value.trim()).find(Boolean) || ''
    const signature = line.replace(signatureModifiers, '')
    if (qualifiedNamePattern.test(signature) && signature.split('.').at(-1) === name && !member) found.add(signature)
    if (!part.java) continue
    const type = signature.match(/^(?:@interface|class|interface|enum|record)\s+([\p{L}\p{N}_$.]+)(?=[\s<{(]|$)/u)?.[1]
    if (type?.split('.').at(-1) === name && qualifiedNamePattern.test(type) && !member) found.add(type)
    if (member) {
      const method = signature.match(/^[\p{L}\p{N}_$.[\]<>?, &]+?\s+([\p{L}\p{N}_$.]+)\s*\(/u)?.[1]
      const owner = method?.slice(0, -(member.length + 1))
      if (method?.endsWith(`.${member}`) && owner?.split('.').at(-1) === name && qualifiedNamePattern.test(owner)) found.add(owner)
    }
  }
  return found
}
function qualifiedAt(index, token) {
  const tokens = index.tokens || [], i = token.codeIndex
  let start = i
  while (tokens[start - 1]?.word === '.' && tokens[start - 2]?.kind === 'identifier') start -= 2
  return tokens.slice(start, i + 1).map(value => value.word).join('')
}
function importAt(index, token) {
  const tokens = index.tokens || []
  let start = token.codeIndex
  while (start >= 0 && ![';', '{', '}'].includes(tokens[start]?.word)) {
    if (tokens[start]?.word === 'import') {
      if (tokens[start + 1]?.word === 'static') return { static: true }
      let end = token.codeIndex
      while (end < tokens.length && tokens[end].word !== ';') end++
      return { name: tokens.slice(start + 1, end).map(value => value.word).join('') }
    }
    start--
  }
  return null
}
const indexCache = new WeakMap()
function indexedEvidence(index) {
  if (indexCache.has(index)) return indexCache.get(index)
  const shadowed = new Set(index.typeRanges?.map(type => type.name) || [])
  for (const role of index.roles?.values() || []) {
    if (role.declaration && ['类', '接口', '注解类型', '枚举', '记录类', '类型参数'].includes(role.category)) shadowed.add(index.tokens?.[role.declarationTokenIndex]?.word)
  }
  // Type parameters are not yet fully indexed. A matching parameter declaration
  // in a type/method header is enough to withhold a guessed library identity.
  const tokens = index.tokens || []
  tokens.forEach((token, i) => {
    if (!simpleNames.has(token.word) || !['<', ','].includes(tokens[i - 1]?.word) || !['>', ',', 'extends'].includes(tokens[i + 1]?.word)) return
    let j = i - 1
    while (j >= 0 && !['<', ';', '{', '}', '='].includes(tokens[j].word)) j--
    if (tokens[j]?.word === '<' && (['class', 'interface', 'record'].includes(tokens[j - 2]?.word) || ['public', 'protected', 'private', 'static', 'final', '{', '}', ';'].includes(tokens[j - 1]?.word))) shadowed.add(token.word)
  })
  const exactImports = new Map(), wildcards = []
  for (const name of index.imports || []) {
    if (name.endsWith('.*')) wildcards.push(name)
    else {
      const simple = name.split('.').at(-1)
      if (!exactImports.has(simple)) exactImports.set(simple, [])
      exactImports.get(simple).push(name)
    }
  }
  const result = { shadowed, exactImports, wildcards }
  indexCache.set(index, result)
  return result
}
function resolveIdentity(index, token, name, annotation, contents, member) {
  const imported = importAt(index, token)
  if (imported?.static) return null
  if (imported?.name) return imported.name.endsWith(`.${name}`) ? { name: imported.name, evidence: '完整导入名称' } : null
  const qualified = annotation?.annotationQualifiedName || qualifiedAt(index, token)
  if (qualified.includes('.')) return { name: qualified, evidence: '源码全限定名称' }
  const { shadowed, exactImports, wildcards } = indexedEvidence(index)
  if (shadowed.has(name)) return null
  const exact = exactImports.get(name) || []
  if (exact.length > 1) return null
  const hover = hoverIdentity(contents, name, member)
  if (hover.size > 1) return null
  if (hover.size === 1) {
    const resolved = [...hover][0]
    if (exact.length && exact[0] !== resolved) return null
    return { name: resolved, evidence: '语言服务器完整签名' }
  }
  if (exact.length === 1) return { name: exact[0], evidence: '明确导入' }
  const builtin = simpleNames.get(name)
  if (wildcards.length > 1) return null
  if (wildcards.length === 1) {
    if (builtin?.slice(0, builtin.lastIndexOf('.')) === wildcards[0].slice(0, -2)) return { name: builtin, evidence: '单一通配导入（词法推定）' }
    return null
  }
  return builtin?.startsWith('java.lang.') && !builtin.startsWith('java.lang.annotation.') ? { name: builtin, evidence: 'java.lang 隐式导入（词法推定）' } : null
}

/** Return verified local API purpose help, or null when the identity is unclear. */
export function resolveJavaBuiltinPurpose({ index, token, role, hoverContents } = {}) {
  if (!index || !token || token.kind !== 'identifier') return null
  const lexical = index.roles?.get(token.codeIndex)
  const annotation = lexical?.annotationName ? lexical : null
  const member = annotation && token.word !== annotation.annotationName ? token.word : null
  const name = annotation?.annotationName || token.word
  if (!simpleNames.has(name)) return null
  // Do not describe a variable or method merely because it shares a type name.
  if (!annotation && !importAt(index, token) && /变量|字段|参数|组件|方法|属性|构造器/.test(role?.category || lexical?.category || '') && !['构造器调用'].includes(role?.category || lexical?.category)) return null
  const imported = importAt(index, token)
  const currentCategory = role?.category || lexical?.category || ''
  if (!annotation && !imported && !['类型引用', '类型', '类', '接口', '枚举', '记录类', '注解', '注解类型', '构造器调用'].includes(currentCategory)) {
    const tokens = index.tokens || [], i = token.codeIndex
    const typeSyntax = tokens[i + 1]?.word === '.' && tokens[i + 2]?.word === 'class' || tokens[i + 1]?.word === '::' || ['new', 'instanceof', 'extends', 'implements'].includes(tokens[i - 1]?.word)
    if (!typeSyntax && !hoverIdentity(hoverContents, name).size) return null
  }
  const identity = resolveIdentity(index, token, name, annotation, hoverContents, member)
  const entry = identity && catalog.get(identity.name)
  if (!entry) return null
  const text = member ? entry.attributes[member] : entry.text
  if (!text) return null
  return {
    text: member ? `${identity.name.startsWith('org.springframework.web.') ? 'Spring Web' : identity.name.startsWith('org.springframework.') ? 'Spring' : 'Java'}：${text}` : text,
    source: `官方 API 摘要；${identity.evidence}`,
    url: entry.url,
    category: member ? '注解属性' : entry.category,
  }
}
