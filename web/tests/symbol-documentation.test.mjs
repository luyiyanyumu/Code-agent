import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import { renderMarkdown } from '../src/lib/markdown.js'
import { hoverCodeBlock } from '../src/lib/hoverDocumentation.js'

const source = await readFile(new URL('../src/components/code/SymbolDocumentation.vue', import.meta.url), 'utf8')
const compiled = compileScript(parse(source).descriptor, { id: 'symbol-documentation-test', inlineTemplate: true }).content
const code = compiled.replace(/^import\s+\{([^}]+)\}\s+from\s+['"]vue['"];?\r?\n/gm, (_, names) =>
  `const { ${names.replace(/\b(\w+)\s+as\s+(\w+)/g, '$1: $2')} } = vue;\n`)
  .replace(/^import[^\n]+\r?\n/gm, '')
  .replace('export default', 'return')
const component = new Function('vue', 'renderMarkdown', 'hoverCodeBlock', code)(Vue, renderMarkdown, hoverCodeBlock)

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
  const loading = await render({ loading: true, error: '旧错误', sections })
  assert.match(loading, /role="status"/)
  assert.match(loading, /aria-busy="true"/)
  assert.match(loading, /读取说明…/)
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
