// Java hover fallback. This is deliberately a lexical aid, not a Java type checker.
// Keep an index per model version in callers; offsets use JavaScript/LSP UTF-16 units.
import { resolveJavaBuiltinPurpose } from './javaBuiltinPurpose.js'
import { resolveJavaSymbolPurpose } from './javaSymbolPurpose.js'
const KEYWORDS = {
  abstract: ['修饰符', '声明抽象类或抽象方法；抽象方法由具体子类实现。'],
  public: ['访问修饰符', '允许其他可访问该类型的代码访问此声明。'],
  protected: ['访问修饰符', '允许同包代码及符合访问规则的子类访问。'],
  private: ['访问修饰符', '将访问范围限制在所属顶层类及其嵌套类型内部。'],
  static: ['修饰符', '成员属于类型本身；访问静态成员不需要创建该类型的实例。'],
  final: ['修饰符', '变量只能赋值一次；方法不可被重写；类不可被继承。'],
  native: ['修饰符', '声明由 Java 之外的本地代码实现的方法。'],
  synchronized: ['关键字', '用于同步方法或同步代码块，获取指定对象的监视器锁。'],
  transient: ['字段修饰符', '标记不参与默认 Java 对象序列化的实例字段。'],
  volatile: ['字段修饰符', '为字段读写提供线程间可见性与特定的内存顺序保证；不保证复合操作原子性。'],
  strictfp: ['修饰符', '历史上用于要求严格的浮点运算语义；现代 Java 已默认采用严格浮点语义。'],
  boolean: ['基本类型', '布尔类型，取值为 true 或 false。'],
  byte: ['基本类型', '8 位有符号整数，范围为 -128 至 127。'],
  short: ['基本类型', '16 位有符号整数，范围为 -32768 至 32767。'],
  int: ['基本类型', '32 位有符号整数。'],
  long: ['基本类型', '64 位有符号整数。'],
  char: ['基本类型', '16 位无符号 UTF-16 代码单元；一个字符可能需要两个 char 表示。'],
  float: ['基本类型', '32 位 IEEE 754 浮点数。'],
  double: ['基本类型', '64 位 IEEE 754 浮点数。'],
  void: ['返回类型关键字', '表示方法不返回值。'],
  class: ['类型声明关键字', '声明类；在 .class 表达式中用于取得类型对应的 Class 对象。'],
  interface: ['类型声明关键字', '声明接口；与 @ 组合时声明注解类型。'],
  enum: ['类型声明关键字', '声明具有一组命名常量的枚举类型。'],
  extends: ['继承关键字', '声明类的父类、接口的父接口，或类型参数的上界。'],
  implements: ['接口实现关键字', '声明类或枚举实现的接口。'],
  new: ['对象创建关键字', '创建类实例或数组；也用于构造器方法引用。'],
  this: ['当前实例关键字', '引用当前实例；this(...) 用于调用本类的其他构造器。'],
  super: ['父类型关键字', '访问父类成员或调用父类构造器；也用于泛型下界。'],
  instanceof: ['类型检查关键字', '检查对象是否符合指定类型，也可配合模式匹配绑定变量。'],
  package: ['包声明关键字', '声明当前编译单元所属的包。'],
  import: ['导入关键字', '导入类型或静态成员，以便使用简短名称。'],
  if: ['条件控制关键字', '条件为 true 时执行相应语句。'],
  else: ['条件控制关键字', '指定 if 条件不成立时执行的分支。'],
  switch: ['分支控制关键字', '根据值或模式选择分支；可作为语句或表达式使用。'],
  case: ['分支标签关键字', '定义 switch 的值匹配或模式匹配分支。'],
  default: ['关键字', '定义 switch 默认分支、接口默认方法，或注解成员的默认值。'],
  for: ['循环关键字', '执行计数循环或遍历数组、可迭代对象的增强 for 循环。'],
  while: ['循环关键字', '条件为 true 时继续循环。'],
  do: ['循环关键字', '与 while 组合形成先执行循环体、再检查条件的循环。'],
  break: ['控制流关键字', '退出循环、switch 语句或指定标签对应的语句。'],
  continue: ['控制流关键字', '跳过本轮循环的剩余语句，开始下一轮循环。'],
  return: ['控制流关键字', '结束当前方法或 lambda，并可返回一个值。'],
  throw: ['异常关键字', '抛出一个异常对象。'],
  throws: ['异常声明关键字', '在方法或构造器签名中声明可能抛出的异常类型。'],
  try: ['异常处理关键字', '建立异常处理代码块；也可声明需要自动关闭的资源。'],
  catch: ['异常处理关键字', '捕获并处理指定类型的异常。'],
  finally: ['异常处理关键字', '定义 try / catch 之后执行的清理代码块。'],
  assert: ['断言关键字', '启用断言时检查条件；失败时抛出 AssertionError。'],
  const: ['保留关键字', 'Java 保留此单词，但不使用它声明常量；常量通常使用 final。'],
  goto: ['保留关键字', 'Java 保留此单词，但没有 goto 跳转语句。'],
  _: ['保留标记', '单独的下划线不能用作普通命名标识符；较新的 Java 可在特定位置表示未命名变量或模式。'],
  true: ['布尔字面量', '表示 boolean 类型的真值。'],
  false: ['布尔字面量', '表示 boolean 类型的假值。'],
  null: ['空引用字面量', '表示没有指向对象的引用；不能直接赋给基本类型变量。'],
}

const CONTEXTUAL = {
  var: ['局部类型推断关键字', '由初始化表达式推断局部变量类型；也可用于 lambda 的参数声明。'],
  record: ['类型声明关键字', '声明记录类，其记录组件参与生成访问器、构造器、equals、hashCode 和 toString。'],
  sealed: ['类／接口修饰符', '限制哪些类型可以直接继承此类或实现此接口。'],
  'non-sealed': ['类／接口修饰符', '重新开放密封父类型的当前子类型，允许继续继承。'],
  permits: ['密封类型关键字', '列出允许直接继承密封类或实现密封接口的类型。'],
  yield: ['控制流关键字', '向当前 switch 表达式提供结果值。'],
  when: ['模式守卫关键字', '为 switch 的模式匹配分支添加布尔条件。'],
  module: ['模块声明关键字', '声明 Java 模块。'],
  open: ['模块修饰符', '将模块中所有包开放给其他模块进行深度反射。'],
  requires: ['模块依赖关键字', '声明当前模块依赖另一个模块。'],
  transitive: ['模块依赖修饰符', '使依赖当前模块的其他模块也能够读取所声明的依赖模块。'],
  exports: ['模块导出关键字', '将包的公开 API 导出给其他模块。'],
  opens: ['模块开放关键字', '将包开放给其他模块进行深度反射。'],
  to: ['模块限定关键字', '限定 exports 或 opens 的目标模块。'],
  uses: ['服务使用关键字', '声明当前模块使用某个服务接口。'],
  provides: ['服务提供关键字', '声明当前模块提供某个服务的实现。'],
  with: ['服务实现关键字', '列出 provides 指定服务的实现类型。'],
}

const SEMANTIC = {
  namespace: ['包／命名空间', '用于组织和限定类型名称。'],
  package: ['包', '用于组织和限定类型名称。'],
  type: ['类型', '此名称由语言服务器识别为类型。'],
  class: ['类', '定义对象的状态与行为，或作为类类型的引用。'],
  interface: ['接口', '定义类型需要遵循的契约。'],
  enum: ['枚举', '表示一组命名常量组成的类型。'],
  enumMember: ['枚举常量', '表示所属枚举类型的一个命名实例。'],
  record: ['记录类', '以记录组件描述数据的类。'],
  recordComponent: ['记录组件', '定义记录类的一项数据，并关联同名访问器。'],
  annotation: ['注解', '向声明或类型使用位置附加元数据。'],
  decorator: ['注解', '向声明或类型使用位置附加元数据。'],
  annotationMember: ['注解属性', '指定注解的一个成员值。'],
  typeParameter: ['类型参数', '在泛型声明中为待确定的类型命名。'],
  parameter: ['参数', '接收调用者传入的值。'],
  variable: ['变量', '以名称引用一个值或对象。'],
  property: ['字段', '保存类型或实例的状态。'],
  field: ['字段', '保存类型或实例的状态。'],
  method: ['方法', '表示可调用的行为。'],
  function: ['方法', '表示可调用的行为。'],
  constructor: ['构造器', '在创建实例时初始化对象。'],
  label: ['语句标签', '为语句命名，可供带标签的 break 或 continue 使用。'],
  keyword: ['关键字', '此名称由语言服务器识别为 Java 关键字。'],
  modifier: ['修饰符', '限定声明的访问权限或其他语义。'],
  string: ['字符串字面量', '表示文本值。'],
  number: ['数字字面量', '表示代码中直接写出的数值。'],
  comment: ['注释', '用于说明代码，不参与程序执行。'],
}

const MODIFIERS = {
  // JDT uses the LSP readonly bit for Java final, including final classes and
  // methods. It does not mean that the referenced object is immutable.
  declaration: '声明位置', definition: '定义位置', readonly: 'final', static: '静态', final: 'final',
  abstract: '抽象', deprecated: '已弃用', public: 'public（公开）', private: 'private（私有）',
  protected: 'protected（受保护）', native: '本地方法', generic: '泛型', typeArgument: '类型实参',
  importDeclaration: '导入位置', documentation: '文档注释', defaultLibrary: '标准库符号',
  async: '异步', modification: '写入位置', constructor: '构造器',
}
const PRIMITIVES = new Set(['boolean', 'byte', 'short', 'int', 'long', 'char', 'float', 'double'])
const DECL_MODIFIERS = new Set(['public', 'protected', 'private', 'static', 'abstract', 'final', 'native', 'synchronized', 'strictfp', 'transient', 'volatile', 'sealed', 'non-sealed', 'default'])
const identifierStart = /[\p{L}\p{Nl}\p{Sc}\p{Pc}]/u
const identifierPart = /[\p{L}\p{Nl}\p{Sc}\p{Pc}\p{Mn}\p{Mc}\p{Nd}\p{Cf}]/u
const isStart = ch => !!ch && identifierStart.test(ch)
const isPart = ch => !!ch && identifierPart.test(ch)
const escapeRegex = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const code = value => '`' + String(value).replace(/`/g, 'ˋ').replace(/[\r\n]+/g, ' ') + '`'

function scan(text) {
  const tokens = []
  let i = 0
  const add = (start, kind) => tokens.push({ start, end: i, word: text.slice(start, i), kind })
  while (i < text.length) {
    const ch = String.fromCodePoint(text.codePointAt(i))
    if (/\s/u.test(ch)) { i += ch.length; continue }
    const start = i
    if (text.startsWith('//', i)) {
      i += 2
      while (i < text.length && text[i] !== '\n' && text[i] !== '\r') i++
      add(start, 'comment'); continue
    }
    if (text.startsWith('/*', i)) {
      const end = text.indexOf('*/', i + 2)
      i = end < 0 ? text.length : end + 2
      add(start, text.startsWith('/**', start) ? 'documentation' : 'comment'); continue
    }
    if (text.startsWith('"""', i)) {
      i += 3
      while (i < text.length) {
        if (text[i] === '\\') { i += Math.min(2, text.length - i); continue }
        if (text.startsWith('"""', i)) { i += 3; break }
        i++
      }
      add(start, 'textBlock'); continue
    }
    if (ch === '"' || ch === "'") {
      i++
      while (i < text.length) {
        if (text[i] === '\\') { i += Math.min(2, text.length - i); continue }
        if (text[i] === ch) { i++; break }
        if (text[i] === '\n' || text[i] === '\r') break
        i++
      }
      add(start, ch === '"' ? 'string' : 'char'); continue
    }
    if (isStart(ch)) {
      i += ch.length
      while (i < text.length) {
        const part = String.fromCodePoint(text.codePointAt(i))
        if (!isPart(part)) break
        i += part.length
      }
      if (text.slice(start, i) === 'non' && text.startsWith('-sealed', i) && !isPart(text[i + 7])) i += 7
      add(start, 'identifier'); continue
    }
    if (/\d/.test(ch)) {
      const number = text.slice(i).match(/^(?:0[xX][\da-fA-F_]+(?:\.[\da-fA-F_]*)?(?:[pP][+-]?[\d_]+)?[fFdDlL]?|0[bB][01_]+[lL]?|[\d_]+(?:\.[\d_]*)?(?:[eE][+-]?[\d_]+)?[fFdDlL]?)/)?.[0]
      i += number?.length || 1
      add(start, 'number'); continue
    }
    const operator = ['...', '::', '->', '==', '!=', '&&', '||', '++', '--', '+=', '-=', '*=', '/='].find(op => text.startsWith(op, i))
    i += operator?.length || ch.length
    add(start, 'punctuation')
  }
  return tokens
}

function pairTokens(tokens) {
  const pairs = new Map(), stack = []
  const close = { ')': '(', ']': '[', '}': '{' }
  for (let i = 0; i < tokens.length; i++) {
    const word = tokens[i].word
    if (['(', '[', '{'].includes(word)) stack.push(i)
    else if (close[word]) {
      const j = stack.at(-1)
      if (tokens[j]?.word === close[word]) { pairs.set(j, i); pairs.set(i, j); stack.pop() }
    }
  }
  return pairs
}

function contextual(tokens, i, index) {
  const word = tokens[i].word, before = tokens[i - 1]?.word, after = tokens[i + 1]?.word
  if (!CONTEXTUAL[word] || before === '.' || before === '::' || before === '@') return false
  if (word === 'record') return tokens[i + 1]?.kind === 'identifier' && tokens[i + 2]?.word === '('
  if (word === 'var') return tokens[i + 1]?.kind === 'identifier' && ['=', ',', ')', ':', '['].includes(tokens[i + 2]?.word)
  if (word === 'sealed' || word === 'non-sealed') {
    let j = i + 1
    while (DECL_MODIFIERS.has(tokens[j]?.word)) j++
    return ['class', 'interface'].includes(tokens[j]?.word)
  }
  if (word === 'permits') {
    for (let j = i - 1; j >= 0 && !['{', '}', ';'].includes(tokens[j].word); j--) {
      if (['class', 'interface'].includes(tokens[j].word)) return true
    }
    return false
  }
  if (word === 'yield') return after !== '(' && after !== '=' && index.switchRanges.some(([a, b]) => i > a && i < b)
  if (word === 'when') {
    for (let j = i - 1; j >= 0 && !['{', '}', ';', '->', ':'].includes(tokens[j].word); j--) if (tokens[j].word === 'case') return true
    return false
  }
  if (word === 'module') return tokens.slice(i + 1, i + 10).some(t => t.word === '{') && (i === 0 || before === 'open' || before === ')')
  if (word === 'open') return after === 'module'
  if (!index.moduleRange || i <= index.moduleRange[0] || i >= index.moduleRange[1]) return false
  if (['requires', 'exports', 'opens', 'uses', 'provides'].includes(word)) return ['{', ';'].includes(before)
  if (word === 'transitive') return before === 'requires' || (before === 'static' && tokens[i - 2]?.word === 'requires')
  const statement = []
  for (let j = i - 1; j >= 0 && !['{', ';'].includes(tokens[j].word); j--) statement.push(tokens[j].word)
  return word === 'to' ? statement.includes('exports') || statement.includes('opens') : statement.includes('provides')
}

function typeBefore(index, i) {
  const { tokens, pairs } = index
  let j = i - 1
  if (tokens[j]?.word === '...') j--
  while (tokens[j]?.word === ']' && pairs.has(j)) j = pairs.get(j) - 1
  if (tokens[j]?.word === '>') {
    let depth = 1
    for (j--; j >= 0 && depth; j--) { if (tokens[j].word === '>') depth++; if (tokens[j].word === '<') depth-- }
    if (depth) return null
  }
  if (tokens[j]?.kind !== 'identifier' || (KEYWORDS[tokens[j].word] && !PRIMITIVES.has(tokens[j].word) && tokens[j].word !== 'void')) return null
  while (tokens[j - 1]?.word === '.' && tokens[j - 2]?.kind === 'identifier') j -= 2
  if (['.', 'new', 'return', 'throw', '@', 'instanceof', 'case'].includes(tokens[j - 1]?.word)) return null
  return { start: j, end: i - 1, text: index.text.slice(tokens[j].start, tokens[i - 1].end) }
}

function rangeLookup(ranges, length) {
  const sorted = [...ranges].sort((a, b) => a.start - b.start || b.end - a.end)
  const lookup = new Array(length), active = []
  let next = 0
  for (let i = 0; i < length; i++) {
    while (active.length && active.at(-1).end <= i) active.pop()
    while (next < sorted.length && sorted[next].start < i) {
      if (sorted[next].end > i) active.push(sorted[next])
      next++
    }
    lookup[i] = active.at(-1) || null
  }
  return lookup
}

function markType(index, type) {
  if (!type) return
  for (let j = type.start; j <= type.end; j++) {
    if (index.tokens[j].kind === 'identifier' && !KEYWORDS[index.tokens[j].word] && !index.roles.has(j)) {
      index.roles.set(j, { category: '类型引用', description: '此处作为类型使用；具体是类、接口还是其他类型需语义信息确认。', inferred: true })
    }
  }
}

function simpleStatementEnd(index, start) {
  const { tokens, pairs } = index
  if (tokens[start]?.word === '{') return pairs.get(start) ?? null
  // A nested control statement needs a real parser. Do not extend a lexical
  // variable binding through the rest of the method when its end is uncertain.
  if (['if', 'for', 'while', 'do', 'try', 'switch', 'synchronized'].includes(tokens[start]?.word) || tokens[start + 1]?.word === ':') return null
  for (let i = start; i < tokens.length; i++) {
    if (tokens[i].word === ';') return i
    if (tokens[i].word === '}') return null
    if (['(', '[', '{'].includes(tokens[i].word)) {
      if (!pairs.has(i)) return null
      i = pairs.get(i)
    }
  }
  return null
}

function buildRoles(index) {
  const { tokens, pairs, roles } = index
  // Annotation names and annotation member assignments.
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].word !== '@' || tokens[i + 1]?.word === 'interface') continue
    let j = i + 1
    if (tokens[j]?.kind !== 'identifier') continue
    while (tokens[j + 1]?.word === '.' && tokens[j + 2]?.kind === 'identifier') {
      roles.set(j, { category: '包／命名空间', description: '注解类型的限定名称。', inferred: true }); j += 2
    }
    const annotationName = tokens[j].word
    const annotationQualifiedName = tokens.slice(i + 1, j + 1).map(token => token.word).join('')
    roles.set(j, { category: '注解', description: '向声明或类型使用位置附加元数据。', inferred: true, annotationName, annotationQualifiedName })
    if (tokens[j + 1]?.word === '(' && pairs.has(j + 1)) {
      const end = pairs.get(j + 1)
      for (let k = j + 2; k < end; k++) {
        if (tokens[k].kind === 'identifier' && tokens[k + 1]?.word === '=' && ['(', ','].includes(tokens[k - 1]?.word)) {
          roles.set(k, { category: '注解属性', description: '为注解的这个成员指定值；具体含义由注解类型定义。', inferred: true, annotationName, annotationQualifiedName })
        }
      }
    }
  }
  for (let i = 0; i < tokens.length; i++) {
    const word = tokens[i].word
    if (!['class', 'interface', 'enum', 'record'].includes(word) || tokens[i - 1]?.word === '.' || tokens[i + 1]?.kind !== 'identifier') continue
    if (word === 'record' && !contextual(tokens, i, index)) continue
    const category = word === 'class' ? '类' : word === 'interface' ? (tokens[i - 1]?.word === '@' ? '注解类型' : '接口') : word === 'enum' ? '枚举' : '记录类'
    roles.set(i + 1, { category, description: `声明${category} ${code(tokens[i + 1].word)}。`, inferred: true, declaration: true, declarationTokenIndex: i + 1 })
    let j = i + 2
    while (j < tokens.length && !['{', ';'].includes(tokens[j].word)) {
      if (tokens[j].word === '(' && pairs.has(j)) {
        if (word === 'record') index.parameterRanges.push({ start: j, end: pairs.get(j), category: '记录组件' })
        j = pairs.get(j)
      }
      j++
    }
    if (tokens[j]?.word === '{') index.typeRanges.push({ start: j, end: pairs.get(j) ?? tokens.length, name: tokens[i + 1].word, category })
  }
  index.typeAt = rangeLookup(index.typeRanges, tokens.length)
  // A following body/semicolon plus a return type is useful evidence of a method declaration.
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].kind !== 'identifier' || KEYWORDS[tokens[i].word] || roles.has(i) || tokens[i + 1]?.word !== '(') continue
    const closing = pairs.get(i + 1)
    if (closing === undefined) continue
    const owner = index.typeAt[i]
    const type = typeBefore(index, i)
    let after = closing + 1
    while (tokens[after]?.word === '[' && tokens[after + 1]?.word === ']') after += 2
    if (tokens[after]?.word === 'throws') {
      after++
      while (after < tokens.length && !['{', ';', '=', '->'].includes(tokens[after].word)) after++
    }
    const constructor = owner?.name === tokens[i].word && ['{', '}', ';', ...DECL_MODIFIERS].includes(tokens[i - 1]?.word)
    if ((type || constructor) && ['{', ';', 'default'].includes(tokens[after]?.word)) {
      const end = tokens[after]?.word === '{' ? (pairs.get(after) ?? tokens.length) : after
      const annotationMember = owner?.category === '注解类型'
      roles.set(i, { category: constructor ? '构造器' : annotationMember ? '注解属性' : '方法', description: constructor ? '声明创建实例时执行的初始化过程。' : annotationMember ? '声明注解可接收的一个成员值；default 可指定默认值。' : '声明可被调用的方法。', type: constructor ? null : type?.text, typeLabel: annotationMember ? '属性类型' : '返回类型', inferred: true, declaration: true, declarationTokenIndex: i })
      markType(index, type)
      index.methodRanges.push({ start: i + 1, end, body: after, name: tokens[i].word })
      index.parameterRanges.push({ start: i + 1, end: closing, category: '参数', scopeEnd: end })
    } else if (tokens[i - 1]?.word === 'new') {
      roles.set(i, { category: '构造器调用', description: '创建此类型的实例；具体重载由参数与类型分析确定。', inferred: true })
    } else {
      roles.set(i, { category: '方法调用', description: '调用此名称的方法；所属类型、参数类型与具体重载需语义信息确认。', inferred: true })
    }
  }
  index.methodAt = rangeLookup(index.methodRanges, tokens.length)
  // Catch parameters and explicitly typed lambda parameters also have declaration syntax.
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].word === '(' && pairs.has(i)) {
      const close = pairs.get(i)
      if (tokens[i - 1]?.word === 'catch' || tokens[close + 1]?.word === '->') {
        const body = tokens[close + 1]?.word === '->' ? close + 2 : close + 1
        index.parameterRanges.push({ start: i, end: close, category: '参数', scopeEnd: pairs.get(body) ?? close })
      }
    }
  }
  index.parameterAt = rangeLookup(index.parameterRanges, tokens.length)
  const controlHeaders = []
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].word !== '(' || !['for', 'try'].includes(tokens[i - 1]?.word) || !pairs.has(i)) continue
    const end = pairs.get(i), scopeEnd = simpleStatementEnd(index, end + 1)
    controlHeaders.push({ start: i, end, scopeEnd, uncertainEnd: index.braceAt[i]?.end ?? tokens.length })
  }
  index.controlHeaderAt = rangeLookup(controlHeaders, tokens.length)
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].kind !== 'identifier' || KEYWORDS[tokens[i].word] || roles.has(i) || contextual(tokens, i, index)) continue
    if (!['=', ';', ',', ')', ':', '['].includes(tokens[i + 1]?.word)) continue
    const type = typeBefore(index, i)
    if (!type || type.text === 'void') continue
    const parameter = index.parameterAt[i]
    const method = index.methodAt[i]
    const owner = index.typeAt[i]
    const methodLocal = method && (!owner || method.start > owner.start)
    const category = parameter?.category || (methodLocal ? '局部变量' : owner ? '字段' : '变量')
    const description = category === '参数' ? '接收调用者传入的值。' : category === '记录组件' ? '声明记录类的一项数据，并关联同名访问器。' : category === '字段' ? '声明保存在类型或实例中的状态。' : '声明一个用于保存值或对象引用的变量。'
    const role = { category, description, type: type.text, typeLabel: '声明类型', inferred: true, declaration: true, declarationTokenIndex: i }
    roles.set(i, role); markType(index, type)
    let scopeStart = 0, scopeEnd = tokens.length
    const controlHeader = index.controlHeaderAt[i]
    if (parameter) { scopeStart = parameter.start; scopeEnd = parameter.scopeEnd ?? owner?.end ?? parameter.end }
    else if (controlHeader) { scopeStart = controlHeader.start; scopeEnd = controlHeader.scopeEnd ?? controlHeader.end }
    else if (index.braceAt[i]) { scopeStart = index.braceAt[i].start; scopeEnd = index.braceAt[i].end }
    const declaration = { i, name: tokens[i].word, role, scopeStart, scopeEnd, ownerStart: owner?.start }
    if (controlHeader?.scopeEnd === null) declaration.uncertainRange = [controlHeader.end, controlHeader.uncertainEnd]
    if (!index.declarations.has(declaration.name)) index.declarations.set(declaration.name, [])
    index.declarations.get(declaration.name).push(declaration)
  }
}

/** Build once for a document version; callers may retain several independent indexes. */
export function createJavaTokenIndex(text) {
  text = typeof text === 'string' ? text : ''
  const allTokens = scan(text)
  const tokens = allTokens.filter(token => !['comment', 'documentation', 'string', 'char', 'textBlock'].includes(token.kind))
  const index = { text, allTokens, tokens, pairs: pairTokens(tokens), roles: new Map(), declarations: new Map(), imports: new Set(), typeRanges: [], methodRanges: [], parameterRanges: [], switchRanges: [], moduleRange: null }
  const braceRanges = []
  for (const [start, end] of index.pairs) if (tokens[start]?.word === '{') braceRanges.push({ start, end })
  index.braceAt = rangeLookup(braceRanges, tokens.length)
  tokens.forEach((token, i) => { token.codeIndex = i })
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].word === 'import' && tokens[i + 1]?.word !== 'static') {
      let qualified = ''
      for (let j = i + 1; j < tokens.length && tokens[j].word !== ';'; j++) qualified += tokens[j].word
      index.imports.add(qualified)
    }
    if (tokens[i].word === 'switch' && tokens[i + 1]?.word === '(') {
      const close = index.pairs.get(i + 1)
      if (tokens[close + 1]?.word === '{') index.switchRanges.push([close + 1, index.pairs.get(close + 1) ?? tokens.length])
    }
    if (tokens[i].word === 'module' && (i === 0 || tokens[i - 1]?.word === 'open')) {
      const open = tokens.findIndex((token, j) => j > i && token.word === '{')
      if (open >= 0) index.moduleRange = [open, index.pairs.get(open) ?? tokens.length]
    }
  }
  buildRoles(index)
  return index
}

let previousIndex = null
function tokenAt(index, offset) {
  let low = 0, high = index.allTokens.length - 1
  while (low <= high) {
    const middle = (low + high) >> 1, token = index.allTokens[middle]
    if (offset < token.start) high = middle - 1
    else if (offset >= token.end) low = middle + 1
    else return token
  }
  return null
}

function hoverStrings(contents) {
  if (typeof contents === 'string') return [contents]
  if (Array.isArray(contents)) return contents.flatMap(hoverStrings)
  if (contents && typeof contents.value === 'string') return [contents.value]
  if (contents?.contents) return hoverStrings(contents.contents)
  return []
}

function hoverEvidence(contents, word) {
  const lines = hoverStrings(contents).flatMap(value => value.split(/\r\n?|\n/)).map(line => line.trim()).filter(line => line && !line.startsWith('```'))
  const escaped = escapeRegex(word)
  for (const line of lines) {
    const typeDeclaration = line.match(new RegExp(`^(?:(?:public|protected|private|abstract|static|final|sealed|non-sealed)\\s+)*(@interface|class|interface|enum|record)\\s+(?:[\\p{L}\\p{N}_$.]+\\.)?${escaped}(?=[\\s<{(]|$)`, 'u'))
    if (typeDeclaration) return { category: ({ class: '类', interface: '接口', enum: '枚举', record: '记录类', '@interface': '注解类型' })[typeDeclaration[1]], description: '语言服务器提供了此类型的声明信息。' }
  }
  // Markup prose such as "Returns value" is not a declaration. Only a Java
  // MarkedString or explicitly labelled fenced code supplies signature evidence.
  function javaBlocks(value) {
    if (Array.isArray(value)) return value.flatMap(javaBlocks)
    if (value && typeof value === 'object' && value.language?.toLowerCase() === 'java' && typeof value.value === 'string') return [value.value]
    if (value?.contents) return javaBlocks(value.contents)
    const markdown = typeof value === 'string' ? value : typeof value?.value === 'string' ? value.value : ''
    return [...markdown.matchAll(/(?:^|\n)[ \t]*```java[ \t]*\r?\n([\s\S]*?)(?:\r?\n)[ \t]*```/gi)].map(match => match[1])
  }
  const signatureLines = javaBlocks(contents).flatMap(value => value.split(/\r\n?|\n/)).map(line => line.trim()).filter(Boolean)
  for (const line of signatureLines) {
    const signature = line.replace(/^(?:(?:public|protected|private|static|final|abstract|native|synchronized|default|strictfp)\s+)+/, '')
    const method = signature.match(new RegExp(`^([\\p{L}\\p{N}_$.[\\]<>?, &]+?)\\s+(?:[\\p{L}\\p{N}_$.]+\\.)?${escaped}\\s*\\(`, 'u'))
    if (method && !/[=:]/.test(method[1]) && !/^\s*(?:return|Source)\b/.test(method[1])) return { category: '方法', description: '语言服务器提供了此方法的签名。', type: method[1].trim(), typeLabel: '返回类型' }
    const variable = signature.match(new RegExp(`^([\\p{L}\\p{N}_$.[\\]<>?, &]+?)\\s+((?:[\\p{L}\\p{N}_$.]+\\.)?)${escaped}(?:\\s*(?:[;=]|$)|\\s+-\\s+)`, 'u'))
    if (variable && !/[=:]/.test(variable[1]) && !/^\s*(?:return|Source)\b/.test(variable[1])) return { category: variable[2] ? '字段' : '变量', description: '语言服务器提供了此符号的类型。', type: variable[1].trim(), typeLabel: '类型' }
  }
  return null
}

function localRole(index, token) {
  const i = token.codeIndex
  if (index.roles.has(i)) return index.roles.get(i)
  const before = index.tokens[i - 1]?.word
  if (before !== '.' || index.tokens[i - 2]?.word === 'this') {
    const sameName = index.declarations.get(token.word) || []
    const explicitThis = before === '.'
    const uncertainBinding = !explicitThis && sameName.some(item => item.uncertainRange && i > item.uncertainRange[0] && i < item.uncertainRange[1])
    const qualifiedThis = explicitThis && index.tokens[i - 3]?.word === '.'
    const choices = uncertainBinding || qualifiedThis ? [] : sameName.filter(item => i > item.scopeStart && i < item.scopeEnd && (i >= item.i || item.role.category === '字段') && (!explicitThis || (item.role.category === '字段' && item.ownerStart === index.typeAt[i]?.start)))
    choices.sort((a, b) => b.scopeStart - a.scopeStart || b.i - a.i)
    if (choices[0]) return { ...choices[0].role, declaration: false, description: `引用当前作用域内声明的${choices[0].role.category}。` }
  }
  return { category: '标识符（语义尚未解析）', description: '这是一个代码名称；当前信息不足以确定它是变量、类型、方法或其他符号。' }
}

function compatibleBinding(local, category) {
  return local.category === category
    || category === '变量' && ['参数', '局部变量', '字段', '记录组件'].includes(local.category)
    || local.category === '注解属性' && category === '方法'
}

function syntaxPurpose(role) {
  const descriptions = {
    类型: '用于声明值的类型、创建对象或访问该类型的成员。',
    类型引用: '在此处指定声明、泛型或类型关系所使用的类型。',
    类: '封装对象的状态和行为，供创建实例或访问类成员使用。',
    接口: '定义实现类型需要遵循的方法契约。',
    枚举: '定义一组命名常量，限制可选取的值。',
    枚举常量: '表示所属枚举类型的一个命名实例。',
    记录类: '用记录组件表示一组数据，自动提供访问器和值比较等方法。',
    记录组件: '保存记录对象的一项数据，通过同名访问器读取。',
    注解: '为声明或类型使用位置附加元数据，供编译器或框架读取。',
    注解类型: '定义可附加到 Java 代码上的元数据及其属性。',
    注解属性: '配置所属注解的一个成员值，具体含义由注解类型定义。',
    类型参数: '在泛型声明中代表一个尚未确定的类型，使用时由类型实参约束。',
    参数: '接收调用者传入的值，供当前方法或构造器使用。',
    变量: '保存值或对象引用，供当前代码读取或赋值。',
    局部变量: '保存当前代码块中的临时数据，其名称仅在所属作用域内可用。',
    字段: '保存类或实例的状态，供所属类型的代码读取或更新。',
    方法: '封装可被调用的操作，接收参数并执行方法定义的逻辑。',
    方法调用: '执行目标方法，将实参传给它并取得结果或完成相应操作。',
    构造器: '创建实例时执行初始化，设置对象的初始状态。',
    构造器调用: '创建指定类型的实例并执行匹配的初始化过程。',
  }
  return descriptions[role.category]
    ? descriptions[role.category]
    : role.description
}

// Purpose summaries from source comments/server prose are text, never active Markdown.
function purposeText(value) {
  return String(value).replace(/&/g, '&amp;').replace(/[\\`*_{}\[\]()#+\-.!<>|~=:]/g, character => `&#${character.codePointAt(0)};`)
}

/** Return a small, untrusted Markdown supplement, or null outside lexical tokens. */
export function describeJavaToken({ text = '', offset, semanticToken = null, hoverContents = null, documentationTranslation = null, index = null } = {}) {
  if (typeof text !== 'string' || !Number.isInteger(offset) || offset < 0 || offset >= text.length) return null
  if (!index || index.text !== text) {
    if (!previousIndex || previousIndex.text !== text) previousIndex = createJavaTokenIndex(text)
    index = previousIndex
  }
  const token = tokenAt(index, offset)
  if (!token || token.kind === 'punctuation') return null
  let role, source = null, localOnly = false
  const lexical = {
    comment: ['注释', '用于解释代码，不参与 Java 语句的执行。'],
    documentation: ['文档注释', '用于生成 Javadoc，可描述类型、方法、参数和返回值。'],
    string: ['字符串字面量', '用双引号表示 String 类型的文本；内部单词不会作为 Java 标识符解析。'],
    textBlock: ['文本块字面量', '用三引号表示多行 String 文本；内部内容不会作为 Java 语句解析。'],
    char: ['字符字面量', '用单引号表示一个 char 类型的 UTF-16 代码单元。'],
    number: ['数字字面量', '代码中直接写出的数值；具体类型取决于数值形式及后缀。'],
  }
  if (lexical[token.kind]) {
    const [category, description] = lexical[token.kind]
    role = { category, description }
    localOnly = true
  } else if (KEYWORDS[token.word] || contextual(index.tokens, token.codeIndex, index)) {
    const [category, description] = KEYWORDS[token.word] || CONTEXTUAL[token.word]
    role = { category, description }
    localOnly = true
  } else {
    const local = localRole(index, token)
    const evidence = hoverEvidence(hoverContents, token.word)
    const modifiers = Array.isArray(semanticToken?.modifiers) ? semanticToken.modifiers : []
    const semantic = modifiers.includes('constructor') ? SEMANTIC.constructor : SEMANTIC[semanticToken?.type]
    if (semantic) {
      const [category, description] = semantic
      role = { category, description, type: evidence?.type || local.type, typeLabel: evidence?.typeLabel || local.typeLabel }
      if (compatibleBinding(local, category)) role.declarationTokenIndex = local.declarationTokenIndex
      source = '语言服务器语义信息'
      if (local.category === '注解属性' && ['property', 'field', 'method', 'function', 'variable'].includes(semanticToken?.type)) {
        role = { ...role, category: local.category, description: local.description, typeLabel: '属性类型' }
        source = '注解语法位置与语言服务器语义信息'
      }
    } else if (evidence) {
      // A bare variable signature cannot distinguish a parameter from a local; syntax can.
      role = { ...local, ...evidence, category: evidence.category === '变量' && ['参数', '局部变量', '字段', '记录组件'].includes(local.category) ? local.category : evidence.category }
      if (!compatibleBinding(local, evidence.category)) delete role.declarationTokenIndex
      if (local.category === '注解属性' && evidence.category === '方法') role = { ...role, category: '注解属性', typeLabel: '属性类型' }
      source = '语言服务器签名'
    } else role = local
    const attributes = modifiers.map(modifier => MODIFIERS[modifier]).filter(Boolean)
    if (attributes.length) role = { ...role, attributes: [...new Set(attributes)] }
  }
  const symbolPurpose = localOnly ? null : resolveJavaSymbolPurpose({ index, token, role, hoverContents, documentationTranslation })
  const builtinPurpose = localOnly ? null : resolveJavaBuiltinPurpose({ index, token, role, hoverContents })
  // Chinese library explanations take precedence over server prose; this file's own
  // documentation describes custom symbols, including similarly named declarations.
  const purpose = symbolPurpose?.source !== '语言服务器文档' && symbolPurpose
    || builtinPurpose || symbolPurpose
  if (builtinPurpose?.category && role.category === '标识符（语义尚未解析）') role = { ...role, category: builtinPurpose.category }
  const displayWord = ['comment', 'documentation', 'string', 'textBlock'].includes(token.kind) && token.word.length > 64 ? token.word.slice(0, 61) + '…' : token.word
  const lines = [`${code(displayWord)} · **${role.category}**`, `**作用：** ${purpose ? purposeText(purpose.text) : syntaxPurpose(role)}`]
  if (role.type) lines.push(`**${role.typeLabel || '类型'}：** ${code(role.type)}`)
  if (role.attributes?.length) lines.push(`**属性：** ${role.attributes.join('、')}`)
  else if (role.declaration) lines.push('**属性：** 声明位置')
  if (source) lines.push(`识别依据：${source}。`)
  else if (role.inferred) lines.push('识别依据：当前代码语法位置（尚未经过类型解析）。')
  if (purpose) lines.push(`作用依据：${purposeText(purpose.source)}${purpose.translation ? '（模型中文翻译）' : ''}。`)
  else if (!localOnly) lines.push('作用依据：Java 语法用途。')
  if (purpose?.url) lines.push(`[${code(token.word)} 官方说明](${purpose.url})`)
  const declaration = Number.isInteger(role.declarationTokenIndex) ? index.tokens[role.declarationTokenIndex] : null
  return { start: token.start, end: token.end, word: token.word, category: role.category, localOnly,
    ...(declaration ? { declarationOffset: declaration.start } : {}),
    purpose: purpose?.text || syntaxPurpose(role), contents: [{ kind: 'markdown', value: lines.join('\n\n') }] }
}
