// A shared palette for lexical tokens and the standard LSP semantic classes.
// Monaco standalone matches semantic types with the same token rules.
const palettes = {
  light: { foreground: '263244', comment: '66758A', keyword: '7B3EAC', control: 'A43E77', string: '22734B', number: 'AD4B17', type: '087F8C', function: '8A5700', variable: '263244', property: '245EA8', parameter: '9B4E27', constant: '945113', decorator: '8C42A0', operator: '506176', tag: '245EA8', attribute: '8A5700', invalid: 'C42B35' },
  dark: { foreground: 'D5DFEC', comment: '899BB4', keyword: 'CFA4F7', control: 'F2A0C7', string: 'A8D5A2', number: 'EAAF85', type: '71C8CF', function: 'E8C47F', variable: 'D5DFEC', property: '8AB8F2', parameter: 'EDB28D', constant: 'ECC082', decorator: 'D5A4E6', operator: 'ABB9CE', tag: '8AB8F2', attribute: 'E8C47F', invalid: 'FF8D99' },
}

export function editorTheme(mode = 'light') {
  const dark = mode === 'dark', p = palettes[dark ? 'dark' : 'light']
  const rules = []
  const color = (tokens, foreground, fontStyle = '') => {
    for (const token of tokens) rules.push({ token, foreground, fontStyle })
  }
  color([''], p.foreground)
  color(['comment'], p.comment, 'italic')
  color(['comment.doc', 'comment.documentation'], p.comment)
  color(['keyword', 'storage', 'storage.type'], p.keyword)
  color(['keyword.control', 'keyword.return', 'keyword.if', 'keyword.else', 'keyword.for', 'keyword.while', 'keyword.switch', 'keyword.case', 'keyword.break', 'keyword.continue', 'keyword.throw', 'keyword.try', 'keyword.catch', 'keyword.finally'], p.control)
  color(['string', 'string.escape', 'attribute.value'], p.string)
  color(['number'], p.number)
  color(['type', 'type.identifier', 'class', 'interface', 'struct', 'enum', 'typeParameter', 'namespace'], p.type)
  color(['function', 'method', 'support.function', 'predefined'], p.function)
  color(['identifier', 'variable'], p.variable)
  color(['property', 'event'], p.property)
  color(['parameter'], p.parameter)
  color(['constant', 'enumMember', 'variable.readonly', 'property.readonly'], p.constant)
  color(['annotation', 'decorator', 'macro', 'metatag', 'attribute.name.directive'], p.decorator)
  color(['operator', 'delimiter'], p.operator)
  color(['tag'], p.tag)
  color(['attribute.name'], p.attribute)
  color(['regexp'], p.string)
  color(['invalid', 'string.invalid', 'string.escape.invalid'], p.invalid, 'underline')
  return {
    base: dark ? 'vs-dark' : 'vs', inherit: true, rules,
    colors: {
      'editor.background': dark ? '#1B2330' : '#FFFFFF',
      'editor.foreground': `#${p.foreground}`,
      'editorGutter.background': dark ? '#1B2330' : '#FFFFFF',
      'editorLineNumber.foreground': dark ? '#8292AA' : '#8593A6',
      'editorLineNumber.activeForeground': dark ? '#D5DFEC' : '#334A6C',
      'editor.lineHighlightBackground': dark ? '#253247' : '#F3F6FC',
      'editor.selectionBackground': dark ? '#364F73' : '#D9E6FD',
      'editor.inactiveSelectionBackground': dark ? '#2A3A51' : '#E7EDF6',
      'editorCursor.foreground': dark ? '#B6CAEF' : '#355DFB',
      'editorIndentGuide.background1': dark ? '#344253' : '#E5EAF1',
      'editorIndentGuide.activeBackground1': dark ? '#71849F' : '#AAB8CB',
      'editorBracketHighlight.foreground1': dark ? '#E8C47F' : '#8A5700',
      'editorBracketHighlight.foreground2': dark ? '#CFA4F7' : '#7B3EAC',
      'editorBracketHighlight.foreground3': dark ? '#71C8CF' : '#087F8C',
      'editorBracketHighlight.unexpectedBracket.foreground': `#${p.invalid}`,
    },
  }
}

export function currentEditorTheme(root = globalThis.document?.documentElement) {
  const dark = root?.getAttribute?.('data-theme') === 'dark' || root?.classList?.contains('dark')
  return dark ? 'code-agent-dark' : 'code-agent-light'
}

export function followEditorTheme(monaco, root = globalThis.document?.documentElement) {
  const update = () => monaco.editor.setTheme(currentEditorTheme(root))
  update()
  if (!root || typeof globalThis.MutationObserver !== 'function') return { dispose() {} }
  const observer = new MutationObserver(update)
  observer.observe(root, { attributes: true, attributeFilter: ['data-theme', 'class'] })
  return { dispose() { observer.disconnect() } }
}
