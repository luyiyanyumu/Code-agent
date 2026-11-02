import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'

// Render the actual source assets at UI sizes and fractional Windows scaling.
const brand = fileURLToPath(new URL('./public/brand/', import.meta.url))
const output = path.resolve(process.argv[2] || fileURLToPath(new URL('../.tmp/brand-vector-browser', import.meta.url)))
await fs.mkdir(output, { recursive: true })
const svg = await fs.readFile(path.join(brand, 'code-agent-mark.svg'))
const png = await fs.readFile(path.join(brand, 'code-agent-mark.png'))
assert.equal(png.subarray(1, 4).toString(), 'PNG', 'Keep the native-compatible PNG')
assert.ok(svg.length < 2048, 'The vector mark should stay below 2KB')
const loaded = await import(process.env.PLAYWRIGHT_CORE_PATH || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js')
const { chromium } = loaded.default || loaded
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 940, height: 640 }, deviceScaleFactor: 1.5 })
  const svgUrl = `data:image/svg+xml;base64,${svg.toString('base64')}`, pngUrl = `data:image/png;base64,${png.toString('base64')}`
  const row = (label, source) => `<section><strong>${label}</strong>${[24, 44, 128].map(size => `<figure><img width="${size}" height="${size}" src="${source}" alt="${label} ${size}像素"><figcaption>${size}px</figcaption></figure>`).join('')}</section>`
  await page.setContent(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Code-agent 品牌矢量验收</title><style>body{margin:0;padding:24px;font:14px system-ui;background:#f4f6fa;color:#1e293b}h1{font-size:18px;margin:0 0 16px}.surfaces{display:grid;grid-template-columns:1fr 1fr;gap:16px}.surface{padding:20px;border-radius:8px;background:white}.dark{background:#181b24;color:#f1f5f9}section{min-height:220px;display:grid;grid-template-columns:56px 48px 68px 140px;gap:4px;align-items:center}strong{font-size:12px}figure{margin:0;text-align:center}img{display:block;margin:auto}figcaption{font-size:11px;opacity:.6;margin-top:10px}</style><h1>Code-agent · 原轮廓 / 矢量轮廓 · 150% Windows 缩放</h1><div class="surfaces">${['', 'dark'].map(theme => `<div class="surface ${theme}">${row('原 PNG', pngUrl)}${row('新 SVG', svgUrl)}</div>`).join('')}</div></html>`)
  await page.locator('img').last().waitFor()
  const assets = await page.locator('img').evaluateAll(images => images.map(image => ({ ready: image.complete && image.naturalWidth > 0, width: image.width, height: image.height })))
  assert.equal(assets.length, 12)
  assert.ok(assets.every(asset => asset.ready && asset.width === asset.height))
  const vector = await page.evaluate(source => {
    const document = new DOMParser().parseFromString(source, 'image/svg+xml')
    return { root: document.documentElement.localName, errors: document.querySelectorAll('parsererror').length, viewBox: document.documentElement.getAttribute('viewBox'), external: [...document.querySelectorAll('[href], [src], image, script, foreignObject')].length }
  }, svg.toString())
  assert.deepEqual(vector, { root: 'svg', errors: 0, viewBox: '0 0 64 64', external: 0 })
  await page.screenshot({ path: path.join(output, 'brand-24-44-128-dpr150.png') })
  const report = { passed: true, svgBytes: svg.length, pngBytes: png.length, pngSha256: createHash('sha256').update(png).digest('hex'), sourcePixelWidth: png.readUInt32BE(16), sourcePixelHeight: png.readUInt32BE(20), assets, vector, screenshot: 'brand-24-44-128-dpr150.png' }
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
} finally { await browser.close() }
