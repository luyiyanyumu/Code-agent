// One command catalog drives menus and editor shortcuts without masking workspace shortcuts.
export const ideActions = [
  { id: 'complete', label: '代码补全', action: 'editor.action.triggerSuggest', capability: 'completionProvider', group: 'code', shortcut: 'Ctrl+Space', keys: ['CtrlCmd', 'Space'], writes: true },
  { id: 'parameters', label: '参数信息', action: 'editor.action.triggerParameterHints', capability: 'signatureHelpProvider', group: 'code', shortcut: 'Ctrl+Shift+Space', keys: ['CtrlCmd', 'Shift', 'Space'] },
  { id: 'documentation', label: '快速文档', group: 'navigation', shortcut: 'Ctrl+Q', keys: ['CtrlCmd', 'KeyQ'] },
  { id: 'definition', label: '跳转到定义', action: 'editor.action.revealDefinition', capability: 'definitionProvider', group: 'navigation', shortcut: 'F12', keys: ['F12'] },
  { id: 'peek', label: '预览定义', action: 'editor.action.peekDefinition', capability: 'definitionProvider', group: 'navigation', shortcut: 'Ctrl+Shift+I', keys: ['CtrlCmd', 'Shift', 'KeyI'] },
  { id: 'implementation', label: '跳转到实现', action: 'editor.action.goToImplementation', capability: 'implementationProvider', group: 'navigation', shortcut: 'Ctrl+Alt+B', keys: ['CtrlCmd', 'Alt', 'KeyB'] },
  { id: 'type', label: '跳转到类型', action: 'editor.action.goToTypeDefinition', capability: 'typeDefinitionProvider', group: 'navigation' },
  { id: 'references', label: '查找引用', action: 'editor.action.goToReferences', capability: 'referencesProvider', group: 'navigation', shortcut: 'Alt+F7', keys: ['Alt', 'F7'] },
  { id: 'symbols', label: '文件结构', capability: 'documentSymbolProvider', group: 'navigation', shortcut: 'Ctrl+F12', keys: ['CtrlCmd', 'F12'] },
  { id: 'callHierarchy', label: '调用层级', capability: 'callHierarchyProvider', group: 'navigation' },
  { id: 'typeHierarchy', label: '类型层级', capability: 'typeHierarchyProvider', group: 'navigation' },
  { id: 'rename', label: '重命名符号', action: 'editor.action.rename', capability: 'renameProvider', group: 'refactor', shortcut: 'Shift+F6', keys: ['Shift', 'F6'], writes: true },
  { id: 'quickFix', label: '快速修复', action: 'editor.action.quickFix', capability: 'codeActionProvider', group: 'refactor', shortcut: 'Alt+Enter', keys: ['Alt', 'Enter'], writes: true },
  { id: 'refactor', label: '重构选中代码', action: 'editor.action.refactor', capability: 'codeActionProvider', group: 'refactor', shortcut: 'Ctrl+Alt+Shift+T', keys: ['CtrlCmd', 'Alt', 'Shift', 'KeyT'], writes: true },
  { id: 'imports', label: '整理导入', action: 'editor.action.organizeImports', capability: 'codeActionProvider', group: 'refactor', shortcut: 'Ctrl+Alt+O', keys: ['CtrlCmd', 'Alt', 'KeyO'], writes: true },
  { id: 'format', label: '整理格式', action: 'editor.action.formatDocument', capability: 'documentFormattingProvider', group: 'code', shortcut: 'Ctrl+Alt+L', keys: ['CtrlCmd', 'Alt', 'KeyL'], writes: true },
]

const builtin = {
  javascript: ['completionProvider', 'signatureHelpProvider', 'hoverProvider', 'definitionProvider', 'referencesProvider', 'documentSymbolProvider', 'renameProvider', 'documentFormattingProvider', 'codeActionProvider'],
  typescript: ['completionProvider', 'signatureHelpProvider', 'hoverProvider', 'definitionProvider', 'referencesProvider', 'documentSymbolProvider', 'renameProvider', 'documentFormattingProvider', 'codeActionProvider'],
  json: ['completionProvider', 'hoverProvider', 'documentSymbolProvider', 'documentFormattingProvider'],
  html: ['completionProvider', 'hoverProvider', 'documentSymbolProvider', 'documentFormattingProvider', 'renameProvider'],
  css: ['completionProvider', 'hoverProvider', 'documentSymbolProvider', 'documentFormattingProvider', 'definitionProvider', 'referencesProvider', 'renameProvider', 'codeActionProvider'],
}
export function ideCapability(capabilities, language, capability) {
  return !!capabilities?.[capability] || !!builtin[language]?.includes(capability)
}
export function ideActionState(item, { capabilities = {}, language = '', readonly = false, busy = false, ready = true } = {}) {
  const supported = item.id === 'documentation' ? language === 'java' || ideCapability(capabilities, language, 'hoverProvider')
    : item.id === 'symbols' ? ideCapability(capabilities, language, 'documentSymbolProvider') || !!capabilities.workspaceSymbolProvider
      : ideCapability(capabilities, language, item.capability)
  const reason = !ready ? '请先打开代码文件' : !supported ? '当前语言服务暂不支持此操作'
    : item.writes && readonly ? '当前文件只读' : busy ? '请等待当前操作完成' : ''
  return { ...item, disabled: !!reason, reason }
}
export function ideKeybinding(item, monaco) {
  return item.keys?.reduce((value, key) => value | (monaco.KeyMod?.[key] ?? monaco.KeyCode?.[key] ?? 0), 0)
}
