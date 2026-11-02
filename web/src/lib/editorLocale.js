// The installed Monaco package includes its matching, offline Microsoft locale.
// Read its data rather than executing its global assignment: that assignment
// would discard Code-agent's existing completion text and host translations.
import localeSource from 'monaco-editor/nls/lang/zh-cn?raw'
import { installCompletionLocale } from './completionLocale.js'

const literal = localeSource.match(/globalThis\._VSCODE_NLS_MESSAGES\s*=\s*(\[[\s\S]*\]);\s*globalThis\._VSCODE_NLS_LANGUAGE\s*=/)?.[1]
const chineseMessages = literal ? JSON.parse(literal) : []
const installed = new WeakMap()

export function installEditorLocale(target = globalThis) {
  if (installed.has(target) && installed.get(target) === target._VSCODE_NLS_MESSAGES) return
  // Preserve the two previously shipped wording choices before filling in the
  // rest of Monaco's built-in UI, including actions registered during imports.
  installCompletionLocale(target)
  const existing = target._VSCODE_NLS_MESSAGES, messages = chineseMessages.slice()
  // Keep the names already used by Code-agent's editor toolbar. These indexes
  // are guarded against the installed 0.56 contribution source in the tests.
  messages[1057] = '整理格式'
  messages[1058] = '整理选区格式'
  messages[827] = '代码导航...'
  for (const [key, value] of Object.entries(existing || {})) {
    if (value != null && value !== '') messages[key] = value
  }
  target._VSCODE_NLS_MESSAGES = messages
  target._VSCODE_NLS_LANGUAGE = 'zh-cn'
  installed.set(target, messages)
}

// Must run before monacoRuntime imports any contribution: many command/menu
// labels call localize() while their module is evaluated, before editor.create.
installEditorLocale()
