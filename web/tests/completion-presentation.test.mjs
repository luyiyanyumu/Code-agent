import test from 'node:test'
import assert from 'node:assert/strict'
import { completionLabel, completionPresentation } from '../src/lib/completionPresentation.js'
import { buildFullSymbolDocumentation } from '../src/lib/fullSymbolDocumentation.js'

test('completion details show one Chinese card and one signature without source or translation boilerplate', () => {
  const signature = 'Session readSession(String token, String tenant)'
  const item = { label: 'readSession', kind: 2, detail: signature,
    documentation: { kind: 'markdown', value: '# readSession\n\n```java\n' + signature + '\n```\n\n读取当前用户的会话。\n\n读取当前用户的会话。\n\n**文档中文翻译：** 模型翻译。\n\ncom\n\nSource: sky-server' },
    documentation_translation: { status: 'ready', language: 'zh-CN' }, data: { opaque: 1 }, insertText: 'readSession(${1:token})' }
  const before = JSON.stringify(item), preview = completionPresentation(item, 'java')
  assert.equal(preview.detail, signature, 'the native list still receives its type/signature')
  assert.equal((preview.documentation.value.match(/Session readSession\(String token, String tenant\)/g) || []).length, 1)
  assert.equal((preview.documentation.value.match(/读取当前用户的会话。/g) || []).length, 1)
  assert.match(preview.documentation.value, /^### `readSession` · 方法/)
  assert.doesNotMatch(preview.documentation.value, /Source:|sky-server|\ncom\n|文档中文翻译|模型翻译|待准备/)
  assert.equal(JSON.stringify(item), before)
})

test('untranslated, pending and failed prose is honest while code and identifiers stay original', () => {
  for (const [status, expected] of [[undefined, /中文说明尚未翻译/], ['untranslated', /中文说明尚未翻译/], ['pending', /正在翻译中文说明/], ['failed', /中文说明翻译失败/]]) {
    const preview = completionPresentation({ label: 'read', kind: 3, detail: 'Returns the current session.',
      documentation: 'Returns a session for this user.', documentation_translation: status && { status } }, 'python')
    assert.equal(preview.detail, '')
    assert.match(preview.documentation.value, expected)
    assert.doesNotMatch(preview.documentation.value, /Returns|模型翻译/)
    assert.match(preview.documentation.value, /`read` · 函数/)
  }
  const code = 'def read(token: str) -> dict[str, str]'
  const preview = completionPresentation({ label: 'read', kind: 3, detail: code, documentation: '返回会话信息。' }, 'python')
  assert.match(preview.documentation.value, /def read\(token: str\) -> dict\[str, str\]/)
  assert.doesNotMatch(preview.documentation.value, /待准备|准备中/)
  const refreshing = completionPresentation({ label: 'read', kind: 3, detail: code, documentation_translation: { status: 'pending', refresh_required: true } }, 'python')
  assert.match(refreshing.documentation.value, /稍后重新补全查看/)
})

test('long actual documentation, parameter tables, examples and readonly edits are not truncated or mutated', () => {
  const body = '读取会话。\n\n| 参数 | 含义 |\n|---|---|\n| token | 用户令牌 |\n\n' + '完整业务边界说明。'.repeat(2000) + '\n\n正文末尾TAIL\n\n```python\nprint(session)\nprint(session)\n```'
  const item = { label: 'read', kind: 3, documentation: { kind: 'markdown', value: body },
    textEdit: { newText: 'read(${1:token})', range: { start: { line: 0, character: 0 }, end: { line: 0, character: 2 } } }, additionalTextEdits: [{ newText: 'import read\n' }], command: { command: 'never.execute' }, data: { _code_agent_original: 'opaque' } }
  const before = JSON.stringify(item), preview = completionPresentation(item, 'python')
  assert.match(preview.documentation.value, /正文末尾TAIL/)
  assert.equal((preview.documentation.value.match(/完整业务边界说明。/g) || []).length, 2000)
  assert.match(preview.documentation.value, /\| token \| 用户令牌 \|/)
  assert.equal((preview.documentation.value.match(/print\(session\)/g) || []).length, 2)
  assert.equal(JSON.stringify(item), before)
})

test('prose labels lose clutter while names, parameter labels and disambiguating modules stay exact', () => {
  assert.deepEqual(completionLabel({ label: 'read', labelDetails: { detail: '(token: str)', description: 'package.session' } }), { label: 'read', detail: '(token: str)', description: 'package.session' })
  assert.deepEqual(completionLabel({ label: 'read', labelDetails: { detail: 'read', description: 'Returns a session for this user.' } }), { label: 'read', detail: undefined, description: undefined })
  assert.equal(completionLabel({ label: '中文方法' }), '中文方法')
  for (const context of ['com.sky.entity.Session', 'backend/utils/common', 'com.sky.entity']) {
    assert.equal(completionPresentation({ label: 'Session', kind: 7, detail: context }).detail, context, 'qualified types and paths are code context, not English prose')
    assert.equal(completionLabel({ label: 'Session', labelDetails: { description: context } }).description, context)
  }
  assert.equal(completionPresentation({ label: 'if', kind: 14 }).documentation, undefined, 'a plain keyword does not create a useless details panel')
})

test('both completion Markdown and literal plaintext preserve the existing trust boundary', () => {
  const item = { label: 'read` [run](command:evil)', kind: 3, documentation: { kind: 'markdown', value: '读取内容。\n\n<img src="https://bad.invalid">\n\n![示意](https://bad.invalid/a.png)\n\n`![literal](https://bad.invalid/code.png)`\n\n[运行](command:evil)' } }
  const preview = completionPresentation(item, 'python')
  assert.ok(preview.documentation.isTrusted === false && preview.documentation.supportHtml === false && preview.documentation.supportThemeIcons === false)
  assert.doesNotMatch(preview.documentation.value, /!\[示意\]/)
  assert.match(preview.documentation.value, /`!\[literal\]/)
  const plain = completionPresentation({ label: 'x', kind: 6, documentation: { kind: 'plaintext', value: '**中文原始文本**\n[运行](command:evil)' } })
  assert.doesNotMatch(plain.documentation.value, /\[运行\]\(command:evil\)/)
})

test('leading blank framing never prevents exact full-signature copy or causes a second declaration', () => {
  const code = ' public String read(String id)\r\n'
  const value = '\r\n \r\n```java\r\n' + code + '\r\n```\r\n\r\n读取说明。'
  const full = buildFullSymbolDocumentation({ sections: [{ kind: 'markdown', value }] })
  assert.equal(full.declaration, code)
  assert.equal(full.sections.filter(section => section.declaration).length, 1)
  assert.equal(full.sections.filter(section => section.value.includes('public String read')).length, 1)
})
