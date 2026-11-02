import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { installCompletionLocale } from '../src/lib/completionLocale.js'

test('suggestion loading and close labels use verified pinned NLS entries without disturbing existing localization', () => {
  const source = readFileSync(new URL('../node_modules/monaco-editor/esm/vs/editor/contrib/suggest/browser/suggestWidgetDetails.js', import.meta.url), 'utf8')
  assert.match(source, /localize\(1542, "Close"\)/)
  assert.match(source, /localize\(1543, "Loading\.\.\."\)/)
  const target = {}, other = []
  other[5] = '已有翻译'; target._VSCODE_NLS_MESSAGES = other
  installCompletionLocale(target)
  assert.equal(target._VSCODE_NLS_MESSAGES[1542], '关闭')
  assert.equal(target._VSCODE_NLS_MESSAGES[1543], '正在读取说明…')
  assert.equal(target._VSCODE_NLS_MESSAGES[5], '已有翻译')
  assert.equal(other[1542], undefined)
  const localized = target._VSCODE_NLS_MESSAGES
  installCompletionLocale(target)
  assert.equal(target._VSCODE_NLS_MESSAGES, localized)
  const fallback = { _VSCODE_NLS_MESSAGES: { 1542: 'Close', 1543: 'Loading...', 2: 'untouched' } }
  installCompletionLocale(fallback)
  assert.equal(fallback._VSCODE_NLS_MESSAGES[1543], '正在读取说明…')
  assert.equal(fallback._VSCODE_NLS_MESSAGES[2], 'untouched')
})
