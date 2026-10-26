import assert from 'node:assert/strict'
import test from 'node:test'
import { editorTheme, currentEditorTheme, followEditorTheme } from '../src/lib/editorTheme.js'
import { vueLanguage } from '../src/lib/vueLanguage.js'

const luminance = hex => {
  const components = hex.match(/../g).map(value => parseInt(value, 16) / 255)
    .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return components[0] * .2126 + components[1] * .7152 + components[2] * .0722
}

test('IDE palettes distinguish real syntax and semantic classes with readable light/dark contrast', () => {
  const categories = ['keyword', 'string', 'number', 'comment', 'class', 'function', 'property', 'parameter', 'decorator']
  for (const mode of ['light', 'dark']) {
    const theme = editorTheme(mode), lookup = new Map(theme.rules.map(rule => [rule.token, rule.foreground]))
    assert.equal(theme.base, mode === 'light' ? 'vs' : 'vs-dark')
    assert.equal(new Set(categories.map(token => lookup.get(token))).size, categories.length)
    const background = luminance(theme.colors['editor.background'].slice(1))
    for (const rule of theme.rules) {
      const foreground = luminance(rule.foreground)
      const contrast = (Math.max(background, foreground) + .05) / (Math.min(background, foreground) + .05)
      assert.ok(contrast >= 4.5, `${mode}: ${rule.token || 'foreground'} contrast ${contrast.toFixed(2)}`)
    }
    assert.ok(lookup.has('variable.readonly')); assert.ok(lookup.has('annotation'))
    assert.ok(theme.colors['editorBracketHighlight.foreground3'])
  }
})

test('theme follows explicit application appearance, defaults light, and removes observer on dispose', () => {
  let mode = '', classes = new Set(), disconnected = false, listener, observed
  const root = { getAttribute: () => mode, classList: { contains: value => classes.has(value) } }
  const themes = [], original = globalThis.MutationObserver
  globalThis.MutationObserver = class {
    constructor(fn) { listener = fn }
    observe(...args) { observed = args }
    disconnect() { disconnected = true }
  }
  try {
    assert.equal(currentEditorTheme(root), 'code-agent-light')
    const subscription = followEditorTheme({ editor: { setTheme: name => themes.push(name) } }, root)
    mode = 'dark'; listener(); mode = 'light'; listener(); classes.add('dark'); listener()
    assert.deepEqual(themes, ['code-agent-light', 'code-agent-dark', 'code-agent-light', 'code-agent-dark'])
    assert.deepEqual(observed, [root, { attributes: true, attributeFilter: ['data-theme', 'class'] }])
    subscription.dispose(); assert.equal(disconnected, true)
  } finally { globalThis.MutationObserver = original }
})

test('Vue retains HTML grammar and independently embeds template JS, script TS/JS and style SCSS/LESS', () => {
  const html = { tokenPostfix: '.html', tokenizer: {
    root: [[/<!--/, 'comment'], [/[^<]+/]], otherTag: [[/"[^"]*"/, 'attribute.value']],
    script: [[/>/, { token: 'delimiter', nextEmbedded: 'javascript' }]],
    style: [[/>/, { token: 'delimiter', nextEmbedded: 'css' }]],
    scriptWithCustomType: [[/>/, { token: 'delimiter', nextEmbedded: '$S2' }]],
  } }
  const grammar = vueLanguage(html)
  assert.equal(grammar.tokenPostfix, '.vue'); assert.equal(html.tokenPostfix, '.html')
  assert.equal(grammar.tokenizer.root[0], html.tokenizer.root[0])
  assert.equal(grammar.tokenizer.root.find(rule => rule[0].test('{{')).at(1).nextEmbedded, 'javascript')
  assert.equal(grammar.tokenizer.vueInterpolation[0][1].nextEmbedded, '@pop')
  assert.equal(grammar.tokenizer.otherTag[0][0].test('v-model'), true)
  assert.equal(grammar.tokenizer.otherTag[0][0].test('@click.stop'), true)
  const state = grammar.tokenizer.scriptAfterLangEquals
  assert.equal(state.find(rule => rule[0].test('"ts"'))[1].switchTo, '@scriptWithCustomType.typescript')
  assert.equal(state.find(rule => rule[0].test("'javascript'"))[1].switchTo, '@scriptWithCustomType.javascript')
  const style = grammar.tokenizer.styleAfterLangEquals
  assert.equal(style.find(rule => rule[0].test('"scss"'))[1].switchTo, '@styleWithCustomType.scss')
  assert.equal(style.find(rule => rule[0].test("'less'"))[1].switchTo, '@styleWithCustomType.less')
  assert.equal(grammar.tokenizer.scriptWithCustomType, html.tokenizer.scriptWithCustomType)
  assert.equal(html.tokenizer.root.length, 2)
})
