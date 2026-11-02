import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import config from '../vite.config.js'
import { assertDeferredEditor, inspectProductionBundle } from '../verify-production-bundle.mjs'

async function fixture(t, sources) {
  const root = await mkdtemp(path.join(tmpdir(), 'code-agent-bundle-'))
  t.after(() => {
    assert.ok(path.resolve(root).startsWith(path.join(path.resolve(tmpdir()), 'code-agent-bundle-')))
    return rm(root, { recursive: true, force: true })
  })
  const files = {
    'index.html': '<script type="module" src="/assets/index-test.js"></script><link rel="stylesheet" href="/assets/index-test.css">',
    'assets/index-test.js': 'import {preload} from "./vite-runtime-test.js";preload(()=>import("./CodeView-test.js"),["./monaco-test.js"]);',
    'assets/CodeView-test.js': 'import {preload} from "./vite-runtime-test.js";export const open=()=>preload(()=>import("./monacoSetup-test.js"));',
    'assets/vite-runtime-test.js': 'export const preload=fn=>fn();',
    'assets/monacoSetup-test.js': 'import "./monaco-test.js";',
    'assets/monaco-test.js': 'export const editor={};',
    'assets/monaco-test.css': '.monaco-editor{display:block}',
    'assets/index-test.css': 'body{margin:0}',
    ...Object.fromEntries(['editor', 'json', 'css', 'html', 'ts'].map(worker => [`assets/${worker}.worker-test.js`, 'self.onmessage=()=>{};'])),
    ...sources,
  }
  for (const [filename, source] of Object.entries(files)) {
    await mkdir(path.dirname(path.join(root, filename)), { recursive: true })
    await writeFile(path.join(root, filename), source)
  }
  return root
}

test('Vite assigns its shared preload helper independently from the editor', () => {
  const chunks = config.build.rollupOptions.output.manualChunks
  assert.equal(chunks('\0vite/preload-helper.js'), 'vite-runtime')
  assert.equal(chunks('E:/app/node_modules/monaco-editor/esm/vs/editor/editor.api.js'), 'monaco')
  assert.equal(chunks('E:\\app\\node_modules\\monaco-editor\\esm\\vs\\editor\\editor.api.js'), 'monaco')
  assert.equal(chunks('E:/app/src/components/code/EditorPane.vue'), undefined)
})

test('emitted dependency graph ignores dynamic imports and preload-table strings', async t => {
  const report = await inspectProductionBundle(await fixture(t))
  assertDeferredEditor(report)
  assert.deepEqual(report.entryClosure, ['assets/index-test.js', 'assets/vite-runtime-test.js'])
  assert.deepEqual(report.workbenchClosure, ['assets/CodeView-test.js', 'assets/vite-runtime-test.js'])
  assert.ok(!report.entryClosure.includes('assets/monacoSetup-test.js'))
})

test('emitted graph catches the original shared-helper regression through static reexports', async t => {
  const report = await inspectProductionBundle(await fixture(t, {
    'assets/vite-runtime-test.js': 'export {preload} from "./shared-test.js";',
    'assets/shared-test.js': 'export {preload} from "./monaco-test.js";',
    'assets/monaco-test.js': 'export const preload=fn=>fn();export const editor={};',
  }))
  assert.deepEqual(report.eagerMonaco, ['assets/monaco-test.js'])
  assert.throws(() => assertDeferredEditor(report), /Startup statically imports/)
})

test('HTML preload and stylesheet can eagerly load Monaco even when JS imports do not', async t => {
  const report = await inspectProductionBundle(await fixture(t, {
    'index.html': '<script type="module" src="/assets/index-test.js"></script><link rel="modulepreload" href="/assets/monaco-test.js"><link rel="stylesheet" href="/assets/monaco-test.css">',
  }))
  assert.deepEqual(report.eagerMonaco, ['assets/monaco-test.js', 'assets/monaco-test.css'])
  assert.throws(() => assertDeferredEditor(report), /Startup statically imports/)
})
