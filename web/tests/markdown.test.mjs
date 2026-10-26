import assert from 'node:assert/strict'
import test from 'node:test'
import { renderMarkdown } from '../src/lib/markdown.js'

test('code answers render headings, emphasis, lists, tables and literal code', () => {
  const result = renderMarkdown('### 调整建议\n\n**保存**后更新索引。\n\n1. 写入文件\n2. 后台索引\n\n| 文件 | 状态 |\n| --- | --- |\n| app.py | 已保存 |\n\n```html\n<script>alert(1)</script>\n```')
  assert.match(result, /<h3>调整建议<\/h3>/)
  assert.match(result, /<strong>保存<\/strong>/)
  assert.match(result, /<ol>[\s\S]*<li>写入文件<\/li>/)
  assert.match(result, /<table>[\s\S]*<td>已保存<\/td>/)
  assert.match(result, /<code class="language-html">&lt;script&gt;alert\(1\)&lt;\/script&gt;/)
  assert.doesNotMatch(result, /<script>/)
})

test('raw HTML and attribute injection remain visible text, never executable elements', () => {
  const result = renderMarkdown('<script>window.attack = 1</script>\n\n<img src=x onerror=alert(1)>\n\n<svg><a xlink:href="javascript:alert(1)">click</a></svg>\n\n<iframe srcdoc="<script>alert(1)</script>"></iframe>')
  assert.doesNotMatch(result, /<(?:script|img|svg|iframe|a)\b/i)
  assert.match(result, /&lt;img/)
  assert.match(result, /&lt;script&gt;/)
})

test('unsafe and obfuscated link schemes cannot become active links', () => {
  for (const href of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'java&#x73;cript:alert(1)', 'data:text/html,test', 'vbscript:msgbox(1)', 'file:///C:/secrets', '//external.test/path']) {
    const result = renderMarkdown(`[example](${href})`)
    assert.doesNotMatch(result, /<a\b/, href)
    assert.match(result, /example/)
  }
})

test('valid links are escaped and external images never create automatic resource loads', () => {
  const result = renderMarkdown('[文档](https://example.test/docs?a=1&b=2 "标题")\n\n![架构](https://example.test/diagram.png)\n\n[邮件](mailto:test@example.test)')
  assert.match(result, /href="https:\/\/example.test\/docs\?a=1&amp;b=2"/)
  assert.match(result, /target="_blank" rel="noopener noreferrer"/)
  assert.match(result, /href="https:\/\/example.test\/diagram.png"[^>]*>架构<\/a>/)
  assert.match(result, /href="mailto:test@example.test"/)
  assert.doesNotMatch(result, /<(?:img|video|audio|iframe)\b|\bsrc=/)
})

test('incomplete streaming markdown stays renderable without raw HTML', () => {
  for (const text of ['', null, '### 开始\n\n```js\nconst value = "<script>"', '**正在分析', '[link](javascript:']) {
    const result = renderMarkdown(text)
    assert.equal(typeof result, 'string')
    assert.doesNotMatch(result, /<script\b/)
  }
})
