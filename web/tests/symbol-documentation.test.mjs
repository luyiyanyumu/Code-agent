import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import { renderMarkdown } from '../src/lib/markdown.js'
import { hoverCodeBlock } from '../src/lib/hoverDocumentation.js'
import { buildFullSymbolDocumentation } from '../src/lib/fullSymbolDocumentation.js'

const source = await readFile(new URL('../src/components/code/SymbolDocumentation.vue', import.meta.url), 'utf8')
const compiled = compileScript(parse(source).descriptor, { id: 'symbol-documentation-test', inlineTemplate: true }).content
const code = compiled.replace(/^import\s+\{([^}]+)\}\s+from\s+['"]vue['"];?\r?\n/gm, (_, names) =>
  `const { ${names.replace(/\b(\w+)\s+as\s+(\w+)/g, '$1: $2')} } = vue;\n`)
  .replace(/^import[^\n]+\r?\n/gm, '')
  .replace('export default', 'return')
const component = new Function('vue', 'renderMarkdown', 'hoverCodeBlock', 'buildFullSymbolDocumentation', code)(Vue, renderMarkdown, hoverCodeBlock, buildFullSymbolDocumentation)

function render(document = {}) { return renderToString(Vue.createSSRApp(component, { document })) }

test('complete documentation keeps source, query location and full signature/body in their original order', async () => {
  const longText = `说明开始\n${'完整内容 '.repeat(1200)}\n说明结束`
  const html = await render({
    title: 'AgentSessionService', path: 'service/AgentSessionService.java', line: 64,
    sections: [
      { kind: 'code', language: 'java', value: 'public class AgentSessionService {}' },
      { kind: 'markdown', value: '### 为什么要有这一层\n\n**事件**保留完整记录。' },
      { kind: 'plaintext', value: longText },
    ],
  })
  assert.match(html, /AgentSessionService/)
  assert.match(html, /语言服务/)
  assert.match(html, /查询位置/)
  assert.match(html, /service\/AgentSessionService\.java:64/)
  assert.match(html, /<strong>事件<\/strong>/)
  assert.match(html, /说明开始/)
  assert.match(html, /说明结束/)
  assert.equal((html.match(/完整内容/g) || []).length, 1200)
  assert.ok(html.indexOf('public class') < html.indexOf('为什么要有这一层'))
  assert.ok(html.indexOf('为什么要有这一层') < html.indexOf('说明开始'))
  assert.match(html, /data-section-kind="code"/)
  assert.match(html, /aria-busy="false"/)
})

test('plaintext remains literal, including Markdown, HTML and command links', async () => {
  const html = await render({ title: '<script>title()</script>', path: '<img src=x>', line: 7,
    sections: [{ kind: 'plaintext', value: '**字面文本**\n<h3>标题</h3>\n[运行](command:run)\n<script>attack()</script>' }] })
  assert.match(html, /\*\*字面文本\*\*/)
  assert.match(html, /&lt;h3&gt;标题&lt;\/h3&gt;/)
  assert.match(html, /\[运行\]\(command:run\)/)
  assert.match(html, /&lt;script&gt;attack\(\)&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<(?:script|img|h3|strong|a)\b/)
  assert.match(html, /&lt;script&gt;title\(\)&lt;\/script&gt;/)
})

test('Markdown cannot activate raw HTML, remote images or unsafe links', async () => {
  const html = await render({ sections: [{ kind: 'markdown', value:
    '<script>attack()</script>\n\n<img src="https://example.test/tracker" onerror="attack()">\n\n'
    + '[运行](command:run)\n\n[危险](javascript:alert(1))\n\n![图片](https://example.test/image.png)\n\n'
    + '[文档](https://example.test/docs)' }] })
  assert.doesNotMatch(html, /<(?:script|img|iframe)\b/)
  assert.doesNotMatch(html, /href="(?:command|javascript|data):/)
  assert.match(html, /&lt;script&gt;attack\(\)&lt;\/script&gt;/)
  assert.match(html, /href="https:\/\/example\.test\/image\.png"/)
  assert.match(html, /href="https:\/\/example\.test\/docs"/)
  assert.match(html, /rel="noopener noreferrer"/)
})

test('code signatures cannot escape their fence or inject a language info string', async () => {
  const value = 'class Example {}\n```\n[执行](command:run)\n<script>attack()</script>\n````'
  const html = await render({ sections: [{ kind: 'code', language: 'java\n```\n[执行](command:run)', value }] })
  assert.match(html, /language-plaintext/)
  assert.match(html, /\[执行\]\(command:run\)/)
  assert.match(html, /&lt;script&gt;attack\(\)&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<(?:a|script)\b/)
  assert.equal((html.match(/<pre>/g) || []).length, 1)
  assert.equal((html.match(/<code/g) || []).length, 1)
})

test('loading, error and empty states replace stale content and escape errors', async () => {
  const sections = [{ kind: 'markdown', value: '**旧说明**' }]
  const loading = await render({ title: 'currentSymbol', loading: true, error: '旧错误', sections })
  assert.match(loading, /role="status"/)
  assert.match(loading, /aria-busy="true"/)
  assert.match(loading, /读取说明…/)
  assert.match(loading, /documentation-title">currentSymbol/)
  assert.doesNotMatch(loading, /旧说明|旧错误/)
  const error = await render({ error: '<img src=x onerror=attack()>', sections })
  assert.match(error, /role="alert"/)
  assert.match(error, /&lt;img src=x onerror=attack\(\)&gt;/)
  assert.doesNotMatch(error, /<img\b|旧说明/)
  const empty = await render({ sections: [{ kind: 'html', value: '<script>attack()</script>' }, null, { kind: 'plaintext', value: '' }] })
  assert.match(empty, /未返回说明/)
  assert.doesNotMatch(empty, /attack/)
  assert.match(await render(null), /未返回说明/)
})

test('closing by button or Escape emits close without changing the document', () => {
  const calls = [], document = { title: '示例', sections: [] }
  const renderView = component.setup({ document }, { expose() {}, emit: event => calls.push(event) })
  const view = renderView({}, [])
  const header = view.children.find(child => child?.props?.class === 'documentation-header')
  const close = header.children.find(child => child?.type === 'button')
  close.props.onClick()
  view.props.onKeydown({ key: 'Escape', stopPropagation() {} })
  assert.deepEqual(calls, ['close', 'close'])
  assert.deepEqual(document, { title: '示例', sections: [] })
})

test('translation lifecycle is visible even without prose and only retryable terminal states expose an action', async () => {
  for (const [status, label, action] of [['untranslated', '未翻译', '翻译为中文'], ['pending', '翻译中', null], ['failed', '翻译失败', '重试翻译']]) {
    const html = await render({ sections: [], documentation_translation: { status } })
    assert.match(html, new RegExp(label))
    assert.doesNotMatch(html, /尚未就绪|未返回说明/)
    assert.equal(html.includes('documentation-translation-retry'), !!action)
    if (action) assert.match(html, new RegExp(action))
    assert.equal(html.includes('aria-busy="true"'), status === 'pending')
  }
  for (const value of [{ status: 'ready' }, { status: 'failed', retryable: false }]) {
    const html = await render({ documentation_translation: value })
    assert.doesNotMatch(html, /documentation-translation-retry/)
  }
  const noTranslation = await render({ sections: [{ kind: 'code', language: 'java', value: 'public @interface EnableTransactionManagement' }],
    documentation_translation: { status: 'untranslated', required: false, retryable: false } })
  assert.doesNotMatch(noTranslation, /documentation-notice|未翻译|翻译为中文/)
  assert.match(noTranslation, /EnableTransactionManagement/)
  assert.doesNotMatch(await render({ detached: true, documentation_translation: { status: 'failed' } }), /documentation-translation-retry/)
  const detached = await render({ detached: true, documentation_translation: { status: 'pending' } })
  assert.match(detached, /文件已切换或修改/)
  assert.doesNotMatch(detached, /正在翻译|aria-busy="true"|documentation-translation-retry/)
})

test('retry is an explicit event and model errors cannot inject provider secrets or markup', async () => {
  const calls = [], document = { documentation_translation: { status: 'failed', error_code: 'timeout', message: 'secret-key <script>attack()</script>' } }
  const html = await render(document)
  assert.match(html, /翻译超时/)
  assert.doesNotMatch(html, /secret-key|attack/)
  const renderView = component.setup({ document }, { expose() {}, emit: event => calls.push(event) })
  const find = node => node && typeof node === 'object' && (node.props?.class === 'documentation-translation-retry' ? node : Array.isArray(node.children) ? node.children.map(find).find(Boolean) : null)
  const button = find(renderView({}, []))
  assert.ok(button)
  button.props.onClick()
  assert.deepEqual(calls, ['retry-translation'])
  assert.equal(document.documentation_translation.status, 'failed')
})

test('quick documentation exposes the real owner, declaration copy and safe reference without dropping Chinese enhancements', async () => {
  const html = await render({ title: 'RouteMapping', sections: [
    { kind: 'markdown', value: '**作用：** 将请求映射到方法。\n\n**返回类型：** `void`\n\n识别依据：语言服务。' },
    { kind: 'markdown', value: '**业务信息：** 员工管理。\n\n**文档中文翻译：** 模型翻译。' },
    { kind: 'code', language: 'java', value: 'org.example.web\npublic @interface RouteMapping' },
    { kind: 'markdown', value: '完整中文描述。\n\n[框架参考](https://example.test/reference)' },
  ] })
  assert.doesNotMatch(html, /快速文档/)
  assert.match(html, /documentation-provenance[\s\S]*org\.example\.web/)
  assert.match(html, /复制签名/)
  assert.match(html, /固定此说明/)
  assert.match(html, /href="https:\/\/example\.test\/reference"[^>]*target="_blank"[^>]*rel="noopener noreferrer"/)
  for (const text of ['作用', '返回类型', '识别依据', '功能', '翻译', '完整中文描述']) assert.match(html, new RegExp(text))
  assert.ok(html.indexOf('将请求映射') < html.indexOf('员工管理'))
  assert.ok(html.indexOf('员工管理') < html.indexOf('public @interface'))
})

test('pin action emits without changing the source document', () => {
  const calls = [], document = { title: 'Type', sections: [{ kind: 'code', language: 'java', value: 'public class Type {}' }] }
  const renderView = component.setup({ document, pinned: false }, { expose() {}, emit: event => calls.push(event) })
  const view = renderView({}, [])
  const toolbar = view.children.find(child => child?.props?.class === 'documentation-actions')
  const pin = toolbar.children.find(child => child?.type === 'button' && child.props.title === '固定此说明')
  assert.equal(pin.props.disabled, false)
  pin.props.onClick()
  assert.deepEqual(calls, ['toggle-pin'])
  assert.equal(document.sections[0].value, 'public class Type {}')
})

test('the reading card shows one overview and one full signature, with metadata behind a closed disclosure', async () => {
  const tokenSection = { kind: 'markdown', value: '`load` · **方法**\n\n**作用：** 读取当前会话。\n\n识别依据：语言服务语义信息。' }
  const declaration = 'public Map<String, String> demo.Session.load(String token)'
  const html = await render({ title: 'load', path: 'Session.java', line: 12, source: '语言服务',
    symbol: { name: 'load', category: '方法', purpose: '读取当前会话。', returnType: 'Map<String, String>', attributes: ['public（公开）', '声明位置'] },
    tokenSection, sections: [tokenSection, { kind: 'code', language: 'java', value: declaration },
      { kind: 'markdown', value: '读取当前会话。\n\n### 参数\n\n- `token`：登录凭据。\n\n### 返回值\n\n返回完整会话信息。\n\n来源：session-module' },
      { kind: 'markdown', value: '**文档中文翻译：** 模型翻译。' },
      { kind: 'markdown', value: 'Ctrl+Q 查看完整说明或原文。' }],
  })
  assert.equal((html.match(/读取当前会话。/g) || []).length, 1)
  assert.equal((html.match(/public Map&lt;String, String&gt; demo.Session.load\(String token\)/g) || []).length, 1)
  assert.match(html, /load<\/span><span class="documentation-category">· 方法/)
  assert.match(html, /登录凭据/)
  assert.match(html, /返回完整会话信息/)
  assert.match(html, /<details class="documentation-provenance"><summary>来源与参考/)
  assert.doesNotMatch(html, /<details[^>]*\bopen\b/)
  const main = html.slice(0, html.indexOf('<details class="documentation-provenance"'))
  assert.doesNotMatch(main, /识别依据|session-module|模型翻译|Ctrl\+Q/)
})

test('English original is opt-in and both language views retain the same safe rendering contract', async () => {
  const document = { title: 'FindUser', path: 'api/user.ts', line: 2,
    sections: [{ kind: 'markdown', value: '**作用：** 查找用户。\n\n**业务信息：** 用户查询。' }],
    originalSections: [{ kind: 'markdown', value: 'Finds a user.\n\n<script>attack()</script>\n\n[运行](command:run)' }],
  }
  const html = await render(document)
  assert.match(html, /中文说明/)
  assert.match(html, /原文/)
  assert.match(html, /查找用户/)
  assert.doesNotMatch(html, /Finds a user|attack/)
  const renderView = component.setup({ document }, { expose() {}, emit() {} })
  const initial = renderView({}, [])
  const actions = initial.children.find(child => child?.props?.class === 'documentation-actions')
  const languageGroup = actions.children.find(child => child?.props?.class === 'documentation-languages')
  languageGroup.children[1].props.onClick()
  const originalView = renderView({}, [])
  const body = originalView.children.find(child => child?.props?.class === 'documentation-body')
  const encoded = JSON.stringify(body, (key, value) => ['component', 'appContext', 'ctx'].includes(key) ? undefined : value)
  assert.match(encoded, /Finds a user/)
  assert.match(encoded, /&lt;script&gt;/)
  assert.doesNotMatch(encoded, /href=\\"command:/)
  assert.equal(document.sections[0].value, '**作用：** 查找用户。\n\n**业务信息：** 用户查询。')
})

test('copy writes the exact declaration, keeping custom prose out of source code', async () => {
  const declaration = 'public static String findUser(String name)', copied = []
  const descriptor = Object.getOwnPropertyDescriptor(globalThis.navigator, 'clipboard')
  Object.defineProperty(globalThis.navigator, 'clipboard', { configurable: true, value: { writeText: async text => copied.push(text) } })
  try {
    const document = { title: 'findUser', sections: [
      { kind: 'markdown', value: '**业务信息：** 根据名称查询用户。' },
      { kind: 'code', language: 'java', value: declaration },
    ] }
    const renderView = component.setup({ document }, { expose() {}, emit() {} })
    const view = renderView({}, [])
    const actions = view.children.find(child => child?.props?.class === 'documentation-actions')
    const copy = actions.children.find(child => child?.props?.title === '复制语言服务返回的声明签名')
    await copy.props.onClick()
    assert.deepEqual(copied, [declaration])
    const footer = renderView({}, []).children.find(child => child?.props?.class === 'documentation-footer')
    assert.equal(footer.children[0].children, '签名已复制')
  } finally {
    if (descriptor) Object.defineProperty(globalThis.navigator, 'clipboard', descriptor)
    else delete globalThis.navigator.clipboard
  }
})
