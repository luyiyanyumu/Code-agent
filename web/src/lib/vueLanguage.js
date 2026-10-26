// Extend Monaco's HTML state machine with Vue SFC embedded languages. These are
// lexical grammar rules, not guesses about whether an identifier is a class.
export function vueLanguage(html) {
  const root = [...html.tokenizer.root]
  root.splice(-1, 1,
    [/\{\{/, { token: 'delimiter', next: '@vueInterpolation', nextEmbedded: 'javascript' }],
    [/[^<{]+/], [/[{]/, ''])
  const afterLang = (tag, fallback) => [
    [/=/, { token: 'delimiter', switchTo: `@${tag}AfterLangEquals` }],
    [/[ \t\r\n]+/],
    [/>/, { token: 'delimiter', next: `@${tag}Embedded`, nextEmbedded: fallback }],
    [/./, { token: '@rematch', switchTo: `@${tag}` }],
  ]
  const langValue = (tag, names, fallback) => [
    ...names.map(([pattern, language]) => [pattern, { token: 'attribute.value', switchTo: `@${tag}WithCustomType.${language}` }]),
    [/"[^"]*"|'[^']*'/, { token: 'attribute.value', switchTo: `@${tag}WithCustomType.${fallback}` }],
    [/[ \t\r\n]+/],
    [/./, { token: '@rematch', switchTo: `@${tag}` }],
  ]
  return {
    ...html, tokenPostfix: '.vue',
    tokenizer: {
      ...html.tokenizer, root,
      otherTag: [[/(?:v-[\w-]+|[:@#][\w.-]+)(?:\.[\w-]+)*/, 'attribute.name.directive'], ...html.tokenizer.otherTag],
      script: [[/lang(?=\s*=)/, { token: 'attribute.name', switchTo: '@scriptAfterLang' }], ...html.tokenizer.script],
      style: [[/lang(?=\s*=)/, { token: 'attribute.name', switchTo: '@styleAfterLang' }], ...html.tokenizer.style],
      scriptAfterLang: afterLang('script', 'javascript'),
      scriptAfterLangEquals: langValue('script', [[/["'](?:ts|typescript)["']/, 'typescript'], [/["'](?:js|javascript)["']/, 'javascript']], 'javascript'),
      styleAfterLang: afterLang('style', 'css'),
      styleAfterLangEquals: langValue('style', [[/["']scss["']/, 'scss'], [/["']less["']/, 'less'], [/["']css["']/, 'css']], 'css'),
      vueInterpolation: [
        [/\}\}/, { token: 'delimiter', next: '@pop', nextEmbedded: '@pop' }],
        [/[^}]+/, ''], [/[}]/, ''],
      ],
    },
  }
}
