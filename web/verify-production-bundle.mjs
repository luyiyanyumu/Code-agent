import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseAst } from 'rollup/parseAst'

const entryAttribute = (tag, attribute) => tag.match(new RegExp(`\\b${attribute}=["']([^"']+)["']`, 'i'))?.[1]
const isMonaco = filename => /(?:^|\/)monaco(?:Setup)?[-.]/i.test(filename)
const cleanURL = value => value.split(/[?#]/)[0]

/** Inspect emitted imports rather than searching strings: lazy preloading tables
 * legitimately mention Monaco and must not count as an eager dependency. */
export async function inspectProductionBundle(directory) {
  const root = path.resolve(directory), html = await fs.readFile(path.join(root, 'index.html'), 'utf8')
  const files = new Map()
  async function collect(folder) {
    for (const item of await fs.readdir(folder, { withFileTypes: true })) {
      const full = path.join(folder, item.name)
      if (item.isDirectory()) await collect(full)
      else files.set(path.relative(root, full).replaceAll('\\', '/'), (await fs.stat(full)).size)
    }
  }
  await collect(root)
  function resolveAsset(specifier, importer = 'index.html') {
    const clean = cleanURL(specifier)
    assert.ok(!/^(?:[a-z]+:)?\/\//i.test(clean), `Unexpected external production dependency: ${specifier}`)
    const resolved = clean.startsWith('/') ? clean.slice(1) : path.posix.normalize(path.posix.join(path.posix.dirname(importer), clean))
    assert.ok(!resolved.startsWith('../') && files.has(resolved), `Missing production asset: ${importer} → ${specifier}`)
    return resolved
  }
  const tags = html.match(/<(?:script|link)\b[^>]*>/gi) || []
  const entries = tags.filter(tag => /^<script/i.test(tag) && entryAttribute(tag, 'type') === 'module').map(tag => resolveAsset(entryAttribute(tag, 'src')))
  const preloads = tags.filter(tag => entryAttribute(tag, 'rel') === 'modulepreload').map(tag => resolveAsset(entryAttribute(tag, 'href')))
  const stylesheets = tags.filter(tag => entryAttribute(tag, 'rel') === 'stylesheet').map(tag => resolveAsset(entryAttribute(tag, 'href')))
  assert.ok(entries.length, 'Production HTML must have a module entry')
  const codeViews = [...files.keys()].filter(filename => /(?:^|\/)CodeView-[^/]+\.js$/.test(filename))
  assert.ok(codeViews.length, 'The production workbench chunk must be retained')
  const dependencies = new Map()
  async function walk(filename, visited) {
    if (visited.has(filename)) return
    visited.add(filename)
    if (!/\.[cm]?js$/.test(filename)) return
    if (!dependencies.has(filename)) {
      const source = await fs.readFile(path.join(root, filename), 'utf8')
      const imports = parseAst(source).body
        .filter(node => ['ImportDeclaration', 'ExportAllDeclaration', 'ExportNamedDeclaration'].includes(node.type) && node.source)
        .map(node => resolveAsset(node.source.value, filename))
      dependencies.set(filename, imports)
    }
    for (const dependency of dependencies.get(filename)) await walk(dependency, visited)
  }
  const entryClosure = new Set(), workbenchClosure = new Set()
  for (const filename of [...entries, ...preloads]) await walk(filename, entryClosure)
  for (const filename of codeViews) await walk(filename, workbenchClosure)
  const startup = new Set([...entryClosure, ...workbenchClosure, ...stylesheets])
  const eagerMonaco = [...startup].filter(isMonaco)
  const monacoAssets = [...files.keys()].filter(isMonaco)
  const sum = values => values.reduce((bytes, filename) => bytes + files.get(filename), 0)
  return {
    directory: root, entries, preloads, stylesheets,
    entryClosure: [...entryClosure], workbenchClosure: [...workbenchClosure], eagerMonaco,
    startupJavaScriptBytes: sum([...startup].filter(filename => filename.endsWith('.js'))),
    // Workbench CSS is attached to its dynamic import by Vite, so only report
    // HTML-linked styles here. Browser resource evidence covers all UI styles.
    htmlStylesheetBytes: sum(stylesheets),
    monacoAssets, monacoBytes: sum(monacoAssets),
    workers: [...files.keys()].filter(filename => /(?:^|\/)(?:editor|json|css|html|ts)\.worker-[^/]+\.js$/.test(filename)),
  }
}

export function assertDeferredEditor(report) {
  assert.deepEqual(report.eagerMonaco, [], `Startup statically imports the deferred editor: ${report.eagerMonaco.join(', ')}`)
  assert.ok(report.monacoAssets.some(filename => /\.js$/.test(filename)), 'Monaco JavaScript must still be bundled')
  assert.ok(report.monacoAssets.some(filename => /\.css$/.test(filename)), 'Monaco styles must still be bundled')
  for (const worker of ['editor', 'json', 'css', 'html', 'ts']) {
    assert.ok(report.workers.some(filename => path.posix.basename(filename).startsWith(`${worker}.worker-`)), `${worker} worker must still be bundled locally`)
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const report = await inspectProductionBundle(process.argv[2] || fileURLToPath(new URL('./dist', import.meta.url)))
  console.log(JSON.stringify(report, null, 2))
  assertDeferredEditor(report)
}
