import test from 'node:test'
import assert from 'node:assert/strict'
import { diffLanguageForPath, reviewDiffOptions } from '../src/lib/diffPresentation.js'

test('ledger language detection handles Windows paths, upper case and filenames without an extension', () => {
  assert.equal(diffLanguageForPath('C:\\项目\\DOCS\\功能说明.MD'), 'markdown')
  assert.equal(diffLanguageForPath('.github/workflows/release.yml'), 'yaml')
  assert.equal(diffLanguageForPath('backend/Example.java'), 'java')
  assert.equal(diffLanguageForPath('frontend/组件.vue'), 'vue')
  assert.equal(diffLanguageForPath('Dockerfile.release'), 'dockerfile')
  assert.equal(diffLanguageForPath('README'), 'plaintext')
  assert.equal(diffLanguageForPath('data.custom'), 'plaintext')
  assert.equal(diffLanguageForPath(null), 'plaintext')
})

test('ledger starts in readable wrapped inline mode and supports explicit side by side and no wrap', () => {
  const normal = reviewDiffOptions()
  assert.equal(normal.readOnly, true)
  assert.equal(normal.originalEditable, false)
  assert.equal(normal.renderSideBySide, false)
  assert.equal(normal.diffWordWrap, 'on')
  assert.match(normal.fontFamily, /Microsoft YaHei/)
  const selected = reviewDiffOptions({ sideBySide: true, wrap: false, fontSize: 22 })
  assert.equal(selected.renderSideBySide, true)
  assert.equal(selected.wordWrap, 'off')
  assert.equal(selected.diffWordWrap, 'off')
  assert.equal(selected.fontSize, 22)
  assert.ok(selected.lineHeight > 30)
})

test('normal Chinese punctuation is permitted while invisible and lookalike characters are not exempted', () => {
  const unicode = reviewDiffOptions().unicodeHighlight
  for (const character of '（）：；，。？！【】') assert.equal(unicode.allowedCharacters[character], true)
  for (const character of ['\u00a0', '\u200b', '\u202e', '\u3000', 'а', 'ο']) assert.equal(unicode.allowedCharacters[character], undefined)
  assert.equal(unicode.nonBasicASCII, false)
  assert.equal(unicode.invisibleCharacters, true)
  assert.equal(unicode.ambiguousCharacters, true)
  assert.equal(unicode.includeComments, true)
  assert.equal(unicode.includeStrings, true)
})

test('changing one diff option object cannot alter later views and invalid font sizes use the default', () => {
  const first = reviewDiffOptions()
  first.unicodeHighlight.allowedCharacters['\u00a0'] = true
  first.minimap.enabled = true
  assert.equal(reviewDiffOptions().unicodeHighlight.allowedCharacters['\u00a0'], undefined)
  assert.equal(reviewDiffOptions().minimap.enabled, false)
  for (const fontSize of [NaN, '18', -1, 11, 25, 16.5]) assert.equal(reviewDiffOptions({ fontSize }).fontSize, 14)
})
