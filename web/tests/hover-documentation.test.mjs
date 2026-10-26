import assert from 'node:assert/strict'
import test from 'node:test'
import { normalizeHoverDocumentation, hoverMarkdownStrings, hoverCodeBlock } from '../src/lib/hoverDocumentation.js'
import { renderMarkdown } from '../src/lib/markdown.js'
import { Marked } from 'marked'

test('hover typing preserves complete Markdown, plaintext and code signatures without guessed Javadoc conversion', () => {
  const doc = '为什么要有这一层\n原来后端是**完全无状态**的\n{@link org.example.Type}\n@param value 参数'
  const signature = 'org.example.Service\npublic <T> T find(String name)'
  const contents = [{ language: 'java', value: signature }, { kind: 'plaintext', value: doc }, { kind: 'markdown', value: '## 文档\n\n**状态**\n\n- 第一项' }, '**legacy Markdown**']
  const sections = normalizeHoverDocumentation(contents)
  assert.deepEqual(sections, [{ kind: 'code', language: 'java', value: signature }, { kind: 'plaintext', value: doc }, { kind: 'markdown', value: contents[2].value }, { kind: 'markdown', value: contents[3] }])
  assert.equal(sections[1].value, doc)
  assert.equal(sections[0].value, signature)
  assert.deepEqual(normalizeHoverDocumentation(sections), sections, 'inserting a function section must retain already normalized code signatures')
  const strings = hoverMarkdownStrings(contents)
  assert.match(renderMarkdown(strings[2].value), /<h2>文档<\/h2>[\s\S]*<strong>状态<\/strong>/)
  assert.doesNotMatch(renderMarkdown(strings[1].value), /<strong>/)
})

test('long documentation is retained without truncation or loss of symbol/type metadata', () => {
  const value = '# Description\n' + 'A'.repeat(80000) + '\n\n## @return\nList<Map<String, Number>>'
  assert.equal(normalizeHoverDocumentation({ kind: 'markdown', value })[0].value, value)
  assert.equal(hoverMarkdownStrings({ kind: 'markdown', value })[0].value, value)
})

test('plaintext Markdown syntax, entities and line breaks remain literal text', () => {
  const value = '**bold**\n[x](command:run)\n<T> &copy; | key |\n---\n# heading\nhttps://example.test/docs\n    **indent**'
  const result = renderMarkdown(hoverMarkdownStrings({ kind: 'plaintext', value })[0].value)
  assert.doesNotMatch(result, /<(?:strong|a|h1|table|hr|pre|code)\b/)
  assert.match(result, /&#42;&#42;bold&#42;&#42;/)
  assert.match(result, /&#91;x&#93;&#40;command&#58;run&#41;/)
  assert.match(result, /&#60;T&#62; &amp;copy;/)
  assert.match(result, /https&#58;\/\/example&#46;test\/docs/)
  assert.match(result, /(?:&nbsp;){4}&#42;&#42;indent/)
  assert.match(result, /<br>/)
})

test('code fences and malicious language info strings cannot escape signatures', () => {
  const value = 'public void run()\n```\n<script>attack()</script>\n`````\n[run](command:evil)'
  const result = hoverMarkdownStrings({ language: 'java\n```\n[run](command:evil)', value })[0]
  assert.match(result.value, /^``````plaintext\n/)
  assert.match(result.value, /\n``````$/)
  const html = renderMarkdown(result.value)
  assert.doesNotMatch(html, /<(?:a|script)\b/)
  assert.match(html, /&lt;script&gt;attack\(\)&lt;\/script&gt;/)
  assert.match(html, /\[run\]\(command:evil\)/)
  assert.equal(normalizeHoverDocumentation({ language: 'c++', value: 'int x;' })[0].language, 'c++')
  assert.equal(normalizeHoverDocumentation({ language: 'c#', value: 'int x;' })[0].language, 'c#')
  assert.equal(hoverCodeBlock('const text = "```"', 'js').split('\n')[0], '````js')
})

test('server trust and embedded HTML flags are not propagated; full view renderer drops command links', () => {
  const doc = { kind: 'markdown', value: '<img onerror=attack()>\n\n[run](command:evil)\n\n[docs](https://example.test/docs)', isTrusted: true, supportHtml: true, supportThemeIcons: true, baseUri: { scheme: 'command' } }
  const result = hoverMarkdownStrings(doc)[0]
  assert.equal(result.isTrusted, false)
  assert.equal(result.supportHtml, false)
  assert.equal(result.supportThemeIcons, false)
  assert.equal(result.baseUri, undefined)
  const rendered = renderMarkdown(normalizeHoverDocumentation(doc)[0].value)
  assert.doesNotMatch(rendered, /<img\b|href="command:/)
  assert.match(rendered, /href="https:\/\/example.test\/docs"/)
})

test('malformed and unknown hover objects are ignored; missing types never acquire Markdown meaning', () => {
  for (const input of [null, undefined, 42, false, { value: 'untyped' }, { kind: 'richtext', value: 'unknown' }, { kind: 'markdown', value: 4 }, { language: 'java' }]) assert.deepEqual(normalizeHoverDocumentation(input), [])
  assert.deepEqual(normalizeHoverDocumentation([{ kind: 'plaintext', value: 'ok' }, null, { kind: 'unknown', value: '**fake**' }]), [{ kind: 'plaintext', value: 'ok' }])
})

test('native hover images become safe manual links and preserve fenced and inline examples byte for byte', () => {
  const code = '```md\n![same](https://example.test/image.png)\n```'
  const inline = '`![same](https://example.test/image.png)`'
  const value = `## 架构\n\n![same](https://example.test/image.png)\n\n${code}\n\n${inline}\n\n![引用图][diagram]\n\n[diagram]: https://example.test/reference.png\n\n| 栏位 | 图 |\n| --- | --- |\n| 内容 | ![table](https://example.test/table.png) |\n`
  const normalized = hoverMarkdownStrings({ kind: 'markdown', value })[0].value
  assert.ok(normalized.includes(code));assert.ok(normalized.includes(inline))
  assert.match(normalized, /\[same\]\(<https:\/\/example\.test\/image\.png>\)/)
  assert.match(normalized, /\[引用图\]\(<https:\/\/example\.test\/reference\.png>\)/)
  const parser = new Marked(), images = []
  parser.walkTokens(parser.lexer(normalized), token => { if (token.type === 'image') images.push(token) })
  assert.equal(images.length, 0)
  assert.equal(normalizeHoverDocumentation({ kind: 'markdown', value })[0].value, value)
})

test('native hover drops executable and local image targets while preserving readable labels', () => {
  for (const href of ['command:run', 'javascript:attack()', 'data:image/svg+xml,attack', 'file:///C:/private.png', '//example.test/image.png']) {
    const normalized = hoverMarkdownStrings({ kind: 'markdown', value: `![架构](${href})` })[0].value
    const rendered = new Marked().parse(normalized)
    assert.doesNotMatch(rendered, /<(?:img|a)\b/)
    assert.match(rendered, /架构/)
  }
})

test('unprovable nested raw ranges fall back to literal documentation without active image loads', () => {
  const value = '> first line\n> second line ![diagram](https://example.test/image.png)\n> ```md\n> ![example](https://example.test/code.png)\n> ```\n'
  const normalized = hoverMarkdownStrings({ kind: 'markdown', value })[0].value
  assert.doesNotMatch(new Marked().parse(normalized), /<img\b/)
  assert.equal(normalizeHoverDocumentation({ kind: 'markdown', value })[0].value, value)
})
