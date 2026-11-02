/** Verify the merged source and its shipped build without npm or build tools. */
import { createHash } from 'node:crypto';
import { readFile, readdir, lstat } from 'node:fs/promises';
import { dirname, join, resolve, relative, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';

export const runtimeDirectory = dirname(fileURLToPath(import.meta.url));
export const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const generated = new Set(['core.mjs', 'core-build.json']);

export function safePath(directory, name) {
  if (typeof name !== 'string' || !name || name.includes('\\') || name.split('/').some(p => !p || p === '.' || p === '..') || isAbsolute(name)) {
    throw new Error(`Invalid Harness build path: ${name}`);
  }
  const path = resolve(directory, name);
  const check = relative(resolve(directory), path);
  if (isAbsolute(check) || check.startsWith('..')) throw new Error(`Invalid Harness build path: ${name}`);
  return path;
}

export async function sourceFiles(directory = runtimeDirectory) {
  const names = [];
  async function walk(folder, prefix = '') {
    for (const entry of await readdir(folder, { withFileTypes: true })) {
      const name = prefix + entry.name;
      if (!prefix && (generated.has(entry.name) || ['node_modules', 'tests'].includes(entry.name))) continue;
      const included = prefix || ['src', 'vendor'].includes(entry.name) || /\.(mjs|json|md)$/.test(entry.name) || entry.name.startsWith('LICENSE');
      if (!included) continue;
      if (entry.isSymbolicLink()) throw new Error(`Harness source cannot be a symbolic link: ${name}`);
      if (entry.isDirectory()) await walk(join(folder, entry.name), name + '/');
      else if (entry.isFile()) names.push(name);
    }
  }
  await walk(directory);
  return names.sort();
}

export async function sourceHashes(directory = runtimeDirectory) {
  return Object.fromEntries(await Promise.all((await sourceFiles(directory)).map(async name => [name, sha256(await readFile(join(directory, name)))])));
}

export async function verifyCore(directory = runtimeDirectory) {
  let manifest;
  try {
    manifest = JSON.parse(await readFile(join(directory, 'core-build.json'), 'utf8'));
    if (manifest.format !== 1 || manifest.output?.path !== 'core.mjs' || !manifest.inputs || !manifest.upstream?.commit) throw new Error('invalid build manifest');
    const actual = await sourceHashes(directory);
    const names = Object.keys(manifest.inputs).sort();
    if (JSON.stringify(names) !== JSON.stringify(Object.keys(actual).sort())) throw new Error('source file inventory changed');
    for (const name of names) {
      safePath(directory, name);
      if (actual[name] !== manifest.inputs[name]) throw new Error(`source changed: ${name}`);
    }
    const bundle = safePath(directory, manifest.output.path);
    if (!(await lstat(bundle)).isFile() || sha256(await readFile(bundle)) !== manifest.output.sha256) throw new Error('compiled core changed');
  } catch (error) {
    throw new Error(`Harness source/build mismatch: ${error.message}. Run npm ci then npm run build:harness in the Code-agent source checkout.`, { cause: error });
  }
  return manifest;
}
