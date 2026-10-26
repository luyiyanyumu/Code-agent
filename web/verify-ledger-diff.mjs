// Isolated ledger fixture: every API is mocked, no project, model request or rollback runs.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = pw.default || pw
const base = process.argv[2] || 'http://127.0.0.1:5193', origin = new URL(base).origin
const output = path.resolve(process.argv[3] || 'web/shots/ledger-diff')
await fs.mkdir(output, { recursive: true })
const source = await (await fetch(`${base}/src/components/code/PatchPanel.vue`)).text()
const vuePath = source.match(/from ["']([^"']*vue\.js[^"']*)["']/)?.[1]
assert.ok(vuePath, 'An isolated Vite server must be running')
const md = '# 前端界面优化方案（UI 与流程 · 2026-10-10）\n\n范围：`frontend/`（Vue 3 + Vite + Element Plus）。只描述方案与规范。\n\n## 功能与验收\n\n' + '中文说明需要自然换行，完整显示功能、证据、处理过程与最终结果。'.repeat(12) + '\n\n- **功能**：代码审查\n- `src/main.js`：入口文件\n'
const patches = [
  { id: 'markdown', path: 'docs/ui-design-2026-10-10.md', tool: 'edit_file', ts: '2026-10-12T05:43:42Z', before_len: 700, after_len: 735 },
  { id: 'unicode', path: 'src/unicode-warning.js', tool: 'editor_save', ts: '2026-10-12T05:43:40Z', before_len: 60, after_len: 70 },
  { id: 'slow', path: 'docs/slow-response.md', tool: 'edit_file', ts: '2026-10-12T05:43:37Z', before_len: 10, after_len: 25 },
  { id: 'yaml', path: '.github/workflows/a-very-long-directory-name-with-production-review-checks/release.yml', tool: 'edit_file', ts: '2026-10-12T05:43:34Z', before_len: 80, after_len: 100 },
]
const details = {
  markdown: { ...patches[0], inline: true, before_text: md.replace('最终结果', '验证结果'), after_text: md },
  unicode: { ...patches[1], inline: true, before_text: 'const pаth = "previous";\nconst hidden = "a\u200bb";\nconst bidi = "a\u202eb";\nconst spaced\u3000= 3;\n// 普通中文（括号）\n', after_text: 'const pаth = "x";\nconst hidden = "a\u200bb";\nconst bidi = "a\u202eb";\nconst spaced\u3000= 3;\n// 普通中文（括号）\n' },
  slow: { ...patches[2], inline: true, before_text: '# 旧文档\n', after_text: '# 延迟结果不应覆盖当前记录\n' },
  yaml: { ...patches[3], inline: true, before_text: 'name: Release\non: push\n', after_text: 'name: Release\non: push\njobs:\n  build:\n    runs-on: windows-latest\n' },
}
const checks = [], errors = [], errorStacks = [], unexpected = [], mutations = []
let releaseDetail, detailStarted = false, holdDetail = false
let releaseList, listStarted = false, holdList = false
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const context = await browser.newContext({ viewport: { width: 1120, height: 930 }, serviceWorkers: 'block' })
await context.route('**/*', async route => {
  const req = route.request(), url = new URL(req.url()), p = decodeURIComponent(url.pathname)
  if (url.origin !== origin) { unexpected.push(`${req.method()} ${url.origin}${p}`); return route.abort() }
  const fulfill = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) })
  if (p.startsWith('/api/')) {
    if (req.method() !== 'GET') { mutations.push(`${req.method()} ${p}`); return route.abort() }
    const match = p.match(/^\/api\/v1\/code\/projects\/([^/]+)\/patches(?:\/([^/]+))?$/)
    if (!match) { unexpected.push(`${req.method()} ${p}`); return route.abort() }
    if (!match[2]) {
      const value = { patches: match[1] === 'p1' ? patches : [], counts: { total: match[1] === 'p1' ? patches.length : 0, active: match[1] === 'p1' ? patches.length : 0 } }
      if (holdList && match[1] === 'p1') { listStarted = true; await new Promise(resolve => { releaseList = resolve }) }
      try { return await fulfill(value) } catch { return }
    }
    if (holdDetail && match[2] === 'slow') { detailStarted = true; await new Promise(resolve => { releaseDetail = resolve }) }
    try { return await fulfill(details[match[2]]) } catch { return }
  }
  if (p === '/__ledger-diff-fixture') return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body,#app{height:100%!important;overflow:hidden!important}#fixture{height:100%;display:flex;flex-direction:column;max-width:1080px;margin:0 auto;border:1px solid var(--border)}#fixture>nav{padding:8px;display:flex;gap:8px;flex-wrap:wrap;background:var(--panel)}#fixture>.patches{flex:1;min-height:0}#events{display:none}</style></head><body><div id="app"></div><script type="module">import{createApp,h,ref}from'${vuePath}';import Panel from'/src/components/code/PatchPanel.vue';import{ensureMonaco}from'/src/lib/monacoSetup.js';import{initializePreferences,usePreferences}from'/src/lib/preferences.js';import'/src/style.css';initializePreferences();const pid=ref('p1'),mounted=ref(true),events=ref([]),preferences=usePreferences(),monaco=ensureMonaco();window.fixture={pid,mounted,events,preferences,monaco};createApp({render:()=>h('main',{id:'fixture'},[h('nav',[h('button',{id:'project',onClick:()=>{pid.value=pid.value==='p1'?'p2':'p1'}},'切换项目'),h('button',{id:'mounted',onClick:()=>{mounted.value=!mounted.value}},'切换挂载'),h('button',{id:'dark',onClick:()=>preferences.updatePreference('theme',preferences.preferences.theme==='dark'?'light':'dark')},'切换主题'),h('button',{id:'font',onClick:()=>preferences.updatePreference('editorFontSize',24)},'大字体')]),mounted.value?h(Panel,{projectId:pid.value,onError:error=>events.value.push({type:'error',error}),onOpenFile:event=>events.value.push({type:'file',...event})}):null,h('pre',{id:'events'},JSON.stringify(events.value))])}).mount('#app')</script></body></html>` })
  return route.continue()
})
const page = await context.newPage()
page.setDefaultTimeout(15000)
page.on('pageerror', error => { errors.push(error.message); errorStacks.push(error.stack) })
const editorState = () => page.evaluate(() => {
  const monaco = window.fixture.monaco, diff = monaco.editor.getDiffEditors()[0], modified = diff?.getModifiedEditor(), original = diff?.getOriginalEditor()
  const option = name => modified?.getOption(monaco.editor.EditorOption[name])
  const warnings = model => model?.getAllDecorations().filter(item => item.options.description === 'unicode-highlight').map(item => ({ line: item.range.startLineNumber, text: model.getValueInRange(item.range) })) || []
  return { models: monaco.editor.getModels().length, diffEditors: monaco.editor.getDiffEditors().length, language: diff?.getModel()?.modified.getLanguageId(), content: diff?.getModel()?.modified.getValue(), fontFamily: option('fontFamily'), fontSize: option('fontSize'), readOnly: option('readOnly'), wordWrap: option('wordWrap'), wrapping: option('wrappingInfo')?.isViewportWrapping, originalWrapping: original?.getOption(monaco.editor.EditorOption.wrappingInfo).isViewportWrapping, originalReadOnly: original?.getOption(monaco.editor.EditorOption.readOnly), unicode: option('unicodeHighlighting'), originalWidth: original?.getLayoutInfo().width, modifiedWidth: modified?.getLayoutInfo().width, warnings: warnings(diff?.getModel()?.modified), originalWarnings: warnings(diff?.getModel()?.original), theme: document.documentElement.getAttribute('data-theme'), tokens: diff ? monaco.editor.tokenize(diff.getModel().modified.getValue(), diff.getModel().modified.getLanguageId()).flat().map(token => token.type) : [] }
})
const select = id => page.locator('.list li').filter({ hasText: patches.find(item => item.id === id).path }).click()
const waitModel = content => page.waitForFunction(content => window.fixture.monaco.editor.getDiffEditors()[0]?.getModel()?.modified.getValue().includes(content), content)
const closeDetail = () => page.getByRole('button', { name: '关闭差异详情', exact: true }).click()
let failure
try {
  await page.goto(`${base}/__ledger-diff-fixture`)
  await page.getByText(patches[0].path, { exact: true }).waitFor()
  assert.equal((await editorState()).models, 0)
  await select('markdown')
  await waitModel('前端界面优化方案')
  await page.waitForFunction(() => (window.fixture.monaco.editor.getDiffEditors()[0]?.getLineChanges()?.length || 0) > 0)
  let state = await editorState()
  assert.equal(state.models, 2); assert.equal(state.diffEditors, 1); assert.equal(state.language, 'markdown')
  assert.equal(state.wordWrap, 'on'); assert.equal(state.wrapping, true); assert.equal(state.readOnly, true)
  assert.ok(state.fontFamily.includes('Microsoft YaHei') || state.fontFamily.includes('PingFang'))
  assert.ok(state.tokens.some(token => token.includes('keyword') || token.includes('string') || token.includes('emphasis')), JSON.stringify(state.tokens))
  assert.ok(state.unicode.allowedCharacters['（'] && state.unicode.allowedCharacters['）'])
  assert.equal(state.unicode.invisibleCharacters, true); assert.equal(state.unicode.ambiguousCharacters, true)
  await page.waitForFunction(() => !window.fixture.monaco.editor.getDiffEditors()[0].getModel().modified.getAllDecorations().some(item => item.options.description==='unicode-highlight' && ['（','）'].includes(window.fixture.monaco.editor.getDiffEditors()[0].getModel().modified.getValueInRange(item.range))))
  checks.push('Markdown syntax, normal Chinese punctuation, CJK sans-serif font and default wrapped inline review')
  await page.screenshot({ path: path.join(output, 'ledger-inline-light.png'), fullPage: true })
  await page.getByRole('button', { name: '并排对照', exact: true }).click()
  await page.waitForFunction(() => window.fixture.monaco.editor.getDiffEditors()[0].getOriginalEditor().getLayoutInfo().width > 200)
  state = await editorState()
  assert.equal(state.originalWrapping, true); assert.equal(state.wrapping, true); assert.equal(state.originalReadOnly, true)
  const wrap = page.getByRole('checkbox', { name: '自动换行', exact: true })
  await wrap.uncheck()
  state = await editorState()
  assert.equal(state.originalWrapping, false); assert.equal(state.wrapping, false)
  await wrap.check()
  state = await editorState()
  assert.equal(state.originalWrapping, true); assert.equal(state.wrapping, true)
  await page.screenshot({ path: path.join(output, 'ledger-side-by-side.png'), fullPage: true })
  await page.getByRole('button', { name: '行内对照', exact: true }).click()
  await wrap.uncheck()
  assert.equal((await editorState()).wordWrap, 'off')
  await wrap.check()
  assert.equal((await editorState()).wordWrap, 'on')
  checks.push('Side-by-side/inline switching and wrap control update the same read-only editor')
  await select('unicode')
  await waitModel('pаth')
  await page.waitForFunction(() => {
    const model=window.fixture.monaco.editor.getDiffEditors()[0]?.getModel()?.modified
    const suspicious=model?.getAllDecorations().filter(item=>item.options.description==='unicode-highlight').map(item=>model.getValueInRange(item.range)) || []
    return ['а','\u200b','\u202e','\u3000'].every(character=>suspicious.some(text=>text.includes(character)))
  })
  state = await editorState()
  assert.equal(state.models, 2); assert.equal(state.language, 'javascript')
  assert.ok(!state.warnings.some(item => item.text.includes('（') || item.text.includes('）')))
  await page.getByRole('button', { name: '并排对照', exact: true }).click()
  await page.waitForFunction(() => {
    const model=window.fixture.monaco.editor.getDiffEditors()[0]?.getModel()?.original
    const suspicious=model?.getAllDecorations().filter(item=>item.options.description==='unicode-highlight').map(item=>model.getValueInRange(item.range)) || []
    return ['а','\u200b','\u202e','\u3000'].every(character=>suspicious.some(text=>text.includes(character)))
  })
  assert.ok(!(await editorState()).originalWarnings.some(item => item.text.includes('（') || item.text.includes('）')))
  await page.getByRole('button', { name: '行内对照', exact: true }).click()
  checks.push('Zero-width/bidirectional/full-width space characters and Cyrillic homographs remain highlighted while Chinese parentheses do not')
  await page.screenshot({ path: path.join(output, 'ledger-unicode-safety.png'), fullPage: true })
  for (let index = 0; index < 4; index++) {
    await select(index % 2 ? 'unicode' : 'markdown'); await waitModel(index % 2 ? 'pаth' : '前端界面优化方案')
    assert.equal((await editorState()).models, 2)
  }
  await closeDetail()
  await page.waitForFunction(() => window.fixture.monaco.editor.getModels().length === 0 && window.fixture.monaco.editor.getDiffEditors().length === 0)
  checks.push('Repeated selection keeps exactly two models and closing detail releases models plus editor')
  await select('markdown'); await waitModel('前端界面优化方案')
  holdDetail = true; detailStarted = false
  await select('slow')
  await page.waitForFunction(() => !!window.fixture, null)
  for (let index=0; !detailStarted && index<30; index++) await page.waitForTimeout(20)
  assert.equal(detailStarted, true)
  await select('unicode'); await waitModel('pаth')
  releaseDetail(); holdDetail = false
  await page.waitForTimeout(160)
  assert.ok((await editorState()).content.includes('pаth'))
  checks.push('A slow previous detail response cannot replace a newer selection')
  holdDetail = true; detailStarted = false
  await select('slow')
  for (let index=0; !detailStarted && index<30; index++) await page.waitForTimeout(20)
  assert.equal(detailStarted, true)
  // Close an existing editor while a new detail request is pending.
  if (await page.getByRole('button', { name: '关闭差异详情', exact: true }).count()) await closeDetail()
  else await page.locator('#mounted').click()
  releaseDetail(); holdDetail = false
  await page.waitForTimeout(160)
  assert.equal((await editorState()).models, 0)
  if (!await page.locator('.patches').count()) await page.locator('#mounted').click()
  checks.push('Closing/unmounting during a pending request cannot recreate stale diff models')
  await select('markdown'); await waitModel('前端界面优化方案')
  await page.locator('#font').click()
  await page.waitForFunction(() => window.fixture.monaco.editor.getDiffEditors()[0].getModifiedEditor().getOption(window.fixture.monaco.editor.EditorOption.fontSize) === 24)
  await page.locator('#dark').click()
  await page.waitForFunction(() => {
    const editor=window.fixture.monaco.editor.getDiffEditors()[0]?.getModifiedEditor()
    const background=editor?.getDomNode()?.querySelector('.monaco-editor-background')
    return background && getComputedStyle(background).backgroundColor === 'rgb(27, 35, 48)'
  })
  await page.screenshot({ path: path.join(output, 'ledger-dark-large-font.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: path.join(output, 'ledger-narrow-dark.png'), fullPage: true })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  assert.equal((await editorState()).wordWrap, 'on')
  await page.getByRole('button', { name: '并排对照', exact: true }).click()
  await page.waitForFunction(() => window.fixture.monaco.editor.getDiffEditors()[0].getOriginalEditor().getLayoutInfo().width > 50)
  assert.equal((await editorState()).originalWrapping, true)
  assert.equal(await page.getByRole('button', { name: '并排对照', exact: true }).getAttribute('aria-pressed'), 'true')
  await page.getByRole('button', { name: '行内对照', exact: true }).click()
  await select('yaml'); await waitModel('runs-on')
  assert.equal((await editorState()).language, 'yaml')
  await page.screenshot({ path: path.join(output, 'ledger-narrow-long-path.png'), fullPage: true })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  checks.push('Theme and editor font preferences update live; 390px layout wraps prose and fits long paths')
  await page.getByRole('button', { name: '打开文件', exact: true }).click()
  assert.deepEqual(await page.evaluate(() => window.fixture.events.value.at(-1)), { type: 'file', path: patches[3].path, line: 1 })
  holdList = true; listStarted = false
  await page.getByRole('button', { name: '刷新面板', exact: true }).click()
  for (let index=0; !listStarted && index<30; index++) await page.waitForTimeout(20)
  assert.equal(listStarted, true)
  await page.locator('#project').click()
  await page.waitForFunction(() => document.querySelectorAll('.list li').length === 0 && window.fixture.monaco.editor.getModels().length === 0)
  releaseList(); holdList = false
  await page.waitForTimeout(180)
  assert.equal(await page.locator('.list li').count(), 0)
  assert.equal((await editorState()).models, 0)
  checks.push('Project change rejects stale lists, clears old detail models, and file navigation emits only an event')
  await page.locator('#project').click()
  await select('markdown'); await waitModel('前端界面优化方案')
  await page.locator('#mounted').click()
  await page.waitForFunction(() => window.fixture.monaco.editor.getModels().length === 0)
  checks.push('Component unmount releases all diff resources')
  assert.deepEqual(unexpected, []); assert.deepEqual(mutations, []); assert.deepEqual(errors, [])
  assert.deepEqual(await page.evaluate(() => window.fixture.events.value.filter(event => event.type === 'error')), [])
} catch (error) { failure = error; await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }) }
finally {
  releaseDetail?.(); releaseList?.()
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ ok: !failure, checks, errors, errorStacks, unexpected, mutations, failure: failure?.message }, null, 2))
  if (!failure) await fs.rm(path.join(output, 'failure.png'), { force: true })
  await browser.close()
}
if (failure) throw failure
console.log(JSON.stringify({ ok: true, checks: checks.length, output }))
