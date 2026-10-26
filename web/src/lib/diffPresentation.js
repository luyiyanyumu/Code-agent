// The ledger uses the same Chinese punctuation policy as the main editor.
// Invisible characters and foreign lookalike letters still receive a marker.
export const CHINESE_UNICODE_HIGHLIGHT = Object.freeze({
  allowedCharacters: Object.freeze(Object.fromEntries(Array.from('（）：；，。？！、·“”‘’—–％／【】「」『』〔〕［］｛｝').map(character => [character, true]))),
  nonBasicASCII: false,
  ambiguousCharacters: true,
  invisibleCharacters: true,
  includeComments: true,
  includeStrings: true,
})

export const DIFF_FONT_FAMILY = '"Cascadia Code", Consolas, "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", monospace'

const languages = Object.freeze({
  md: 'markdown', markdown: 'markdown', mdx: 'markdown',
  yml: 'yaml', yaml: 'yaml', json: 'json', jsonc: 'json',
  py: 'python', pyi: 'python', java: 'java', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'typescript', mts: 'typescript', cts: 'typescript', tsx: 'typescript', vue: 'vue',
  html: 'html', htm: 'html', svelte: 'html', css: 'css', scss: 'scss', less: 'less',
  toml: 'ini', ini: 'ini', cfg: 'ini', properties: 'ini', sh: 'shell', bash: 'shell', zsh: 'shell',
  ps1: 'powershell', psm1: 'powershell', bat: 'bat', cmd: 'bat', sql: 'sql', xml: 'xml', svg: 'xml',
  go: 'go', rs: 'rust', c: 'c', h: 'cpp', cc: 'cpp', cpp: 'cpp', cxx: 'cpp', hpp: 'cpp', cs: 'csharp',
  php: 'php', rb: 'ruby', kt: 'kotlin', kts: 'kotlin', swift: 'swift', dart: 'dart', lua: 'lua', tf: 'hcl', hcl: 'hcl',
})

export function diffLanguageForPath(path) {
  const name = String(path || '').replaceAll('\\', '/').split('/').at(-1).toLowerCase()
  if (name === 'dockerfile' || name.startsWith('dockerfile.')) return 'dockerfile'
  if (name === '.bashrc' || name === '.zshrc') return 'shell'
  const extension = name.includes('.') ? name.split('.').at(-1) : ''
  return languages[extension] || 'plaintext'
}

export function reviewDiffOptions({ sideBySide = false, wrap = true, fontSize = 14 } = {}) {
  const size = Number.isInteger(fontSize) && fontSize >= 12 && fontSize <= 24 ? fontSize : 14
  return {
    readOnly: true,
    originalEditable: false,
    renderSideBySide: sideBySide,
    // Inline is the default. An explicit side-by-side choice must agree with
    // the selected control and its before/after column labels at every width.
    useInlineViewWhenSpaceIsLimited: false,
    diffWordWrap: wrap ? 'on' : 'off',
    wordWrap: wrap ? 'on' : 'off',
    // Monaco turns wrapping off on the hidden original in inline mode. Reset
    // that override when switching views so both visible columns wrap again.
    wordWrapOverride2: 'inherit',
    automaticLayout: true,
    minimap: { enabled: false },
    fontFamily: DIFF_FONT_FAMILY,
    fontSize: size,
    lineHeight: Math.round(size * 1.65),
    fontLigatures: false,
    lineNumbersMinChars: 3,
    glyphMargin: false,
    scrollBeyondLastLine: false,
    renderLineHighlight: 'none',
    padding: { top: 10, bottom: 10 },
    scrollbar: { verticalScrollbarSize: 9, horizontalScrollbarSize: 9 },
    unicodeHighlight: { ...CHINESE_UNICODE_HIGHLIGHT, allowedCharacters: { ...CHINESE_UNICODE_HIGHLIGHT.allowedCharacters } },
  }
}
