import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { payloadFingerprint, payloadFiles } from '../payload.mjs'

test('npm and standalone Harness ship merged sources without registry runtime dependencies', async () => {
  const root = JSON.parse(await readFile(new URL('../../package.json', import.meta.url), 'utf8'))
  const harness = JSON.parse(await readFile(new URL('../../code_agent/harness/node/package.json', import.meta.url), 'utf8'))
  assert.equal(root.engines.node, harness.engines.node)
  assert.deepEqual(root.dependencies ?? {}, {})
  assert.deepEqual(harness.dependencies ?? {}, {})
  assert.match(root.devDependencies.esbuild, /^\d+\.\d+\.\d+$/)
  assert.equal(root.devDependencies.zod, '4.4.3')
  assert(root.files.includes('code_agent/harness/node/vendor/**'))
  assert(root.files.includes('code_agent/harness/node/src/**'))
})

test('merged source, bundle, provenance and license changes invalidate cache; tests and dependencies do not', async t => {
  const root = await mkdtemp(join(tmpdir(), 'code-agent-payload-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const node = join(root, 'code_agent', 'harness', 'node')
  await mkdir(join(node, 'node_modules', 'example'), { recursive: true })
  await mkdir(join(node, 'tests'), { recursive: true })
  await mkdir(join(node, 'src'), { recursive: true })
  const vendor = join(node, 'vendor', 'deepseek-harness')
  await mkdir(join(vendor, 'packages', 'core', 'agent-loop', 'src'), { recursive: true })
  await mkdir(join(root, 'code_agent', '__pycache__'), { recursive: true })
  await writeFile(join(root, 'pyproject.toml'), '[project]\nversion="0.10.2"\n')
  await writeFile(join(root, 'code_agent', '__init__.py'), '')
  await writeFile(join(node, 'runner.mjs'), 'export const core = 1\n')
  await writeFile(join(node, 'package.json'), '{"private":true}\n')
  await writeFile(join(node, 'package-lock.json'), '{"lockfileVersion":3}\n')
  await writeFile(join(node, 'core.mjs'), 'export const core = 1\n')
  await writeFile(join(node, 'core-build.json'), '{"inputs":{}}\n')
  await writeFile(join(node, 'src', 'index.ts'), 'export const entry = 1\n')
  const source = join(vendor, 'packages', 'core', 'agent-loop', 'src', 'index.ts')
  await writeFile(source, 'export const loop = 1\n')
  await writeFile(join(vendor, 'LICENSE'), 'MIT license\n')
  const original = await payloadFingerprint(root)
  await writeFile(join(node, 'node_modules', 'example', 'unrelated.py'), 'not package source')
  await writeFile(join(root, 'code_agent', '__pycache__', 'unrelated.py'), 'not package source')
  await writeFile(join(node, 'tests', 'unrelated.py'), 'not package source')
  await writeFile(join(vendor, 'AGENTS.md'), 'not runtime source')
  assert.equal((await payloadFingerprint(root)).payloadHash, original.payloadHash)
  const names = (await payloadFiles(root)).map(path => path.slice(root.length).replaceAll('\\', '/'))
  assert.equal(names.length, 9)
  assert(names.some(name => name.endsWith('/runner.mjs')))
  await writeFile(join(node, 'runner.mjs'), 'export const core = 2\n')
  const changedRunner = await payloadFingerprint(root)
  assert.notEqual(changedRunner.payloadHash, original.payloadHash)
  await writeFile(join(node, 'package-lock.json'), '{"lockfileVersion":3,"dependencies":{"new":"1.0.0"}}\n')
  assert.notEqual((await payloadFingerprint(root)).payloadHash, changedRunner.payloadHash)
  for (const path of [source, join(node, 'core.mjs'), join(node, 'core-build.json'), join(vendor, 'LICENSE')]) {
    const before = (await payloadFingerprint(root)).payloadHash
    await writeFile(path, (await readFile(path, 'utf8')) + '\nchanged\n')
    assert.notEqual((await payloadFingerprint(root)).payloadHash, before, path)
  }
})
