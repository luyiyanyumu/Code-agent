// Component-only DOM/lifecycle checks. Real dual-service acceptance lives in
// scripts/verify_integration_delivery.mjs; no project/API fixture is started here.
import assert from 'node:assert/strict'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'

const web = dirname(fileURLToPath(import.meta.url)), root = dirname(web)
const out = resolve(process.argv[2] || join(root, '.tmp', 'integration-component-dom'))
await mkdir(out, { recursive: true })
const css = []
const compiled = await build({ write: false, bundle: true, format: 'iife', platform: 'browser', absWorkingDir: web,
  stdin: { resolveDir: web, sourcefile: 'integration-component-entry.js', contents: `import {createApp,h,ref} from 'vue';import Panel from './src/components/code/IntegrationPanel.vue';window.__panelProps={projectId:ref('one'),visible:ref(true)};window.__app=createApp({setup(){return()=>h(Panel,{projectId:window.__panelProps.projectId.value,visible:window.__panelProps.visible.value})}}).mount('#app');` },
  define: { __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
  plugins: [{ name: 'integration-component-fixture', setup(tool) {
    tool.onResolve({ filter: /api\.js$/ }, () => ({ path: 'mock-api', namespace: 'mock' }))
    tool.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({ contents: 'export const api=window.__integrationApi', loader: 'js' }))
    tool.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor, errors } = parse(await readFile(path, 'utf8'), { filename: path }); assert.deepEqual(errors, [])
      const script = compileScript(descriptor, { id: 'component-dom', inlineTemplate: true })
      for (const style of descriptor.styles) { const result = compileStyle({ source: style.content, filename: path, id: 'component-dom' }); assert.deepEqual(result.errors, []); css.push(result.code) }
      return { contents: script.content, loader: 'js', resolveDir: dirname(path) }
    })
  } }],
})
const pw = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const browser = await (pw.default || pw).chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } }), errors = [], checks = []
page.on('pageerror', error => errors.push(error.message))
try {
  await page.setContent('<main id="app"></main>')
  await page.addStyleTag({ content: (await readFile(join(web, 'src/style.css'), 'utf8')) + css.join('\n') + '\n#app{height:540px;width:100%;max-width:1100px;margin:20px auto}' })
  await page.evaluate(() => {
    const config = { name: '组件边界验收', frontend: { mode: 'attach' }, backend: { mode: 'attach' }, frontend_url: 'http://127.0.0.1:5173/', backend_url: 'http://127.0.0.1:8080/', api_prefix: '/api', startup_timeout: 60 }
    window.__calls = []; window.__snapshot = null; window.__previewFails = false; window.__deferSave = false
    const call = (name, pid) => window.__calls.push([name, pid])
    window.__integrationApi = { code: {
      integrationConfig: async pid => { call('config', pid); return { config, candidates: { frontend: [], backend: [] } } },
      integration: async pid => { call('snapshot', pid); return { session: window.__snapshot } },
      startIntegration: async pid => { call('start', pid); window.__snapshot = { id: 'session-1', status: 'starting', services: {} }; return { session: window.__snapshot } },
      stopIntegration: async pid => { call('stop', pid); window.__snapshot = { ...window.__snapshot, status: 'stopped' }; return { session: window.__snapshot } },
      restartIntegration: async pid => { call('restart', pid); window.__snapshot = { id: 'session-2', status: 'starting', services: {} }; return { session: window.__snapshot } },
      integrationPreview: async pid => { call('preview', pid); if (window.__previewFails) throw Error('前端页面暂时不可用'); return { html: '<html><head></head><body>组件预览</body></html>', base_url: 'http://127.0.0.1:19387/api/v1/code/frontend-design-assets/component/', asset_prefix: 'http://127.0.0.1:19387/api/v1/code/frontend-design-assets/component/', api_proxy_url: 'http://127.0.0.1:19387/api/v1/code/integration-api/component/', frontend_url: config.frontend_url, backend_url: config.backend_url, api_prefix: '/api' } },
      saveIntegrationConfig: async (pid, value) => { call('save', pid); if (window.__deferSave) await new Promise(done => { window.__finishSave = done }); return { config: value } },
      integrationRequests: async pid => { call('history', pid); return { requests: [] } },
      run: async () => ({ status: 'exited', events: [] }),
    } }
  })
  await page.addScriptTag({ content: compiled.outputFiles[0].text })
  const panel = page.getByRole('region', { name: '前后端联调', exact: true })
  await panel.getByRole('button', { name: '启动联调', exact: true }).waitFor()
  await page.waitForFunction(() => window.__calls.some(([name]) => name === 'snapshot'))
  await page.evaluate(() => { window.__panelProps.visible.value = false })
  const hiddenCount = await page.evaluate(() => window.__calls.length)
  await page.waitForTimeout(1200); assert.equal(await page.evaluate(() => window.__calls.length), hiddenCount)
  checks.push('Hidden component pauses session and log observation without stopping services')
  await page.evaluate(() => { window.__panelProps.visible.value = true })
  await panel.getByRole('button', { name: '启动联调', exact: true }).click()
  await panel.getByRole('tab', { name: '页面预览', exact: true }).waitFor()
  assert.equal(await page.evaluate(() => window.__calls.filter(([name]) => name === 'preview').length), 0)
  await page.evaluate(() => { window.__snapshot = { ...window.__snapshot, status: 'running' } })
  await panel.getByTitle('前后端联调页面预览', { exact: true }).waitFor()
  assert.equal(await page.evaluate(() => window.__calls.filter(([name]) => name === 'preview').length), 1)
  await page.waitForTimeout(1200); assert.equal(await page.evaluate(() => window.__calls.filter(([name]) => name === 'preview').length), 1)
  checks.push('202 starting to running loads the first preview automatically once')
  await panel.getByRole('button', { name: '整体重启', exact: true }).click()
  await page.evaluate(() => { window.__previewFails = true; window.__snapshot = { ...window.__snapshot, status: 'running' } })
  await panel.getByText('前端页面暂时不可用', { exact: false }).waitFor()
  const attempts = await page.evaluate(() => window.__calls.filter(([name]) => name === 'preview').length)
  await page.waitForTimeout(1200); assert.equal(await page.evaluate(() => window.__calls.filter(([name]) => name === 'preview').length), attempts)
  await page.evaluate(() => { window.__previewFails = false })
  await panel.getByRole('button', { name: '刷新预览', exact: true }).click()
  await panel.getByTitle('前后端联调页面预览', { exact: true }).waitFor()
  checks.push('Preview failures preserve the error and require an explicit recoverable retry')
  await panel.getByRole('tab', { name: '配置', exact: true }).click()
  await panel.getByLabel('联调配置名称', { exact: true }).fill('已提交草稿')
  await page.evaluate(() => { window.__deferSave = true })
  await panel.getByRole('button', { name: '保存配置', exact: true }).click()
  await page.waitForFunction(() => typeof window.__finishSave === 'function')
  await panel.getByLabel('联调配置名称', { exact: true }).fill('保存中继续输入的草稿')
  await page.evaluate(() => window.__finishSave())
  await panel.getByText('保存期间的新增修改仍保留在草稿中', { exact: false }).waitFor()
  assert.equal(await panel.getByLabel('联调配置名称', { exact: true }).inputValue(), '保存中继续输入的草稿')
  checks.push('Saving preserves edits entered while the save response is pending')
  await panel.getByRole('tab', { name: '日志', exact: true }).click(); await page.keyboard.press('ArrowRight')
  assert.equal(await panel.getByRole('tab', { name: '配置', exact: true }).getAttribute('aria-selected'), 'true')
  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await page.screenshot({ path: join(out, `component-config-${width}.png`) })
    const metrics = await panel.evaluate(element => ({ width: element.clientWidth, scroll: element.scrollWidth }))
    assert.ok(metrics.scroll <= metrics.width + 1, `Panel horizontal overflow at ${width}: ${JSON.stringify(metrics)}`)
  }
  checks.push('Configuration tab keyboard navigation and 1440/768/390 panel containment pass')
  await page.evaluate(() => { window.__panelProps.projectId.value = '' })
  await panel.getByText('打开项目，开始前后端联调', { exact: true }).waitFor()
  assert.equal(await panel.locator('iframe').count(), 0)
  assert.equal(await panel.getByRole('button', { name: '启动联调', exact: true }).isDisabled(), true)
  checks.push('No-project state releases the iframe and disables service actions')
  assert.deepEqual(errors, [])
  await writeFile(join(out, 'report.json'), JSON.stringify({ checks, errors, success: true }, null, 2))
  console.log(JSON.stringify({ checks, screenshots: out, success: true }, null, 2))
} finally { await browser.close() }
