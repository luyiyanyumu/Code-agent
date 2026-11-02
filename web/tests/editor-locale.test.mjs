import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const officialUrl = new URL('../node_modules/monaco-editor/esm/vs/nls/lang/zh-cn.js', import.meta.url)
const official = readFileSync(officialUrl, 'utf8')
const messages = JSON.parse(official.match(/globalThis\._VSCODE_NLS_MESSAGES=(\[[\s\S]*\]);\s*globalThis\._VSCODE_NLS_LANGUAGE=/)[1])
// Node does not implement Vite's ?raw imports. The test substitutes the exact
// installed bytes, while exercising the unchanged bootstrap/merge code.
const source = readFileSync(new URL('../src/lib/editorLocale.js', import.meta.url), 'utf8')
  .replace("import localeSource from 'monaco-editor/nls/lang/zh-cn?raw'", `const localeSource = ${JSON.stringify(official)}`)
  .replace("'./completionLocale.js'", JSON.stringify(new URL('../src/lib/completionLocale.js', import.meta.url).href))
const previousMessages = globalThis._VSCODE_NLS_MESSAGES, previousLanguage = globalThis._VSCODE_NLS_LANGUAGE
const { installEditorLocale } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)
globalThis._VSCODE_NLS_MESSAGES = previousMessages
globalThis._VSCODE_NLS_LANGUAGE = previousLanguage

test('offline official table matches the pinned Monaco package and covers native menu actions', () => {
  const pkg = JSON.parse(readFileSync(new URL('../node_modules/monaco-editor/package.json', import.meta.url), 'utf8'))
  assert.equal(pkg.version, '0.56.0');assert.match(official,/Version: 0\.56\.0/);assert.ok(messages.length>2000)
  const target = {};installEditorLocale(target)
  for (const label of ['转到定义', '转到声明', '转到类型定义', '转到实现', '转到引用', '重命名符号', '更改所有匹配项', '整理格式', '整理选区格式', '代码导航...', '重构...', '源代码操作...', '剪切', '复制', '粘贴', '命令面板']) {
    assert.ok(target._VSCODE_NLS_MESSAGES.some(value => value.replace(/&&/g,'')===label), label)
  }
  assert.equal(target._VSCODE_NLS_LANGUAGE,'zh-cn')
})

test('toolbar aliases only affect verified 0.56 labels and keep existing translations authoritative', () => {
  const base=new URL('../node_modules/monaco-editor/esm/vs/editor/',import.meta.url)
  const formatting=readFileSync(new URL('contrib/format/browser/formatActions.js',base),'utf8')
  const symbols=readFileSync(new URL('common/standaloneStrings.js',base),'utf8')
  assert.match(formatting,/localize2\(1057, "Format Document"\)/);assert.match(formatting,/localize2\(1058, "Format Selection"\)/)
  assert.match(symbols,/localize\(827, "Go to Symbol\.\.\."\)/)
  const target={_VSCODE_NLS_MESSAGES:{1057:'自定义整理',827:'自定义导航'}};installEditorLocale(target)
  assert.equal(target._VSCODE_NLS_MESSAGES[1057],'自定义整理');assert.equal(target._VSCODE_NLS_MESSAGES[827],'自定义导航')
})

test('editor bootstrap preserves shipped completion wording and existing custom translations without mutating their table', () => {
  const existing=[];existing[5]='已有中文';existing[1543]='正在读取说明…'
  const target={_VSCODE_NLS_MESSAGES:existing};installEditorLocale(target)
  assert.equal(target._VSCODE_NLS_MESSAGES[5],'已有中文');assert.equal(target._VSCODE_NLS_MESSAGES[1542],'关闭');assert.equal(target._VSCODE_NLS_MESSAGES[1543],'正在读取说明…')
  assert.equal(existing[1542],undefined);const installed=target._VSCODE_NLS_MESSAGES;installEditorLocale(target);assert.equal(target._VSCODE_NLS_MESSAGES,installed)
  const objectTarget={_VSCODE_NLS_MESSAGES:{1542:'自定义关闭',1543:'自定义加载',3:'已有整词'}};installEditorLocale(objectTarget)
  assert.equal(objectTarget._VSCODE_NLS_MESSAGES[1542],'自定义关闭');assert.equal(objectTarget._VSCODE_NLS_MESSAGES[3],'已有整词')
})

test('the single shared Monaco entry loads locale before any standalone or contribution module', () => {
  const runtime=readFileSync(new URL('../src/lib/monacoRuntime.js',import.meta.url),'utf8')
  assert.match(runtime,/^import '\.\/editorLocale\.js'/)
  assert.ok(runtime.indexOf("import './editorLocale.js'")<runtime.indexOf("import 'monaco-editor/"))
  const lsp=readFileSync(new URL('../src/lib/lspClient.js',import.meta.url),'utf8')
  assert.match(lsp,/import \* as monaco from '\.\/monacoRuntime\.js'/)
})
