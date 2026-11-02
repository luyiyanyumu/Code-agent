import { createHash } from 'node:crypto'
import { readdir, readFile } from 'node:fs/promises'
import { join, relative } from 'node:path'

/** Runtime sources only; local dependency caches never become release input. */
export async function payloadFiles(root, directory = join(root, 'code_agent')) {
  const found = []
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (['node_modules', '__pycache__'].includes(entry.name)) continue
    const path = join(directory, entry.name)
    const name = relative(root, path).replaceAll('\\', '/')
    if (name.startsWith('code_agent/harness/node/')
      && (entry.name === 'tests' || ['AGENTS.md', 'CLAUDE.md'].includes(entry.name))) continue
    if (entry.isSymbolicLink()) throw new Error(`发布源不能包含符号链接：${path}`)
    if (entry.isDirectory()) found.push(...await payloadFiles(root, path))
    if (entry.isFile() && (name.endsWith('.py')
      || name.startsWith('code_agent/skills/') && name.endsWith('.md')
      || /^code_agent\/harness\/node\/(?:[^/]+\.(?:mjs|json|md)|LICENSE[^/]*)$/.test(name)
      || name.startsWith('code_agent/harness/node/src/')
      || name.startsWith('code_agent/harness/node/vendor/'))) {
      found.push(path)
    }
  }
  return found
}

export async function payloadFingerprint(root) {
  const files = [join(root, 'pyproject.toml'), ...await payloadFiles(root)]
  files.sort()
  const hash = createHash('sha256')
  for (const path of files) {
    hash.update(relative(root, path).replaceAll('\\', '/'))
    hash.update('\0')
    hash.update(await readFile(path))
    hash.update('\0')
  }
  return { payloadHash: hash.digest('hex'), count: files.length }
}
