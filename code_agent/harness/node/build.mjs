/** Build the local TypeScript merge. --check needs no compiler or npm packages. */
import { readFile, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { builtinModules, createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { runtimeDirectory, sha256, sourceHashes, verifyCore, safePath } from './verify.mjs';

// This resolver belongs to the build script, never to a running agent. Keeping
// the directory injectable lets tests rebuild copied source outside the repo.
const require = createRequire(import.meta.url);
export async function buildCore(directory = runtimeDirectory) {
  const before = await sourceHashes(directory);
  const { build, version: esbuildVersion } = await import('esbuild');
  const upstream = JSON.parse(await readFile(join(directory, 'UPSTREAM.json'), 'utf8'));
  const zodPackage = require.resolve('zod/package.json');
  const zod = JSON.parse(await readFile(zodPackage, 'utf8'));
  if (esbuildVersion !== '0.25.12' || zod.version !== '4.4.3') throw new Error('Build requires esbuild 0.25.12 and zod 4.4.3; run npm ci.');
  const source = join(directory, 'vendor', 'deepseek-harness');
  const attributionFile = join(source, 'packages/llm/llm/src/attribution.ts');
  const result = await build({
    absWorkingDir: directory, entryPoints: ['src/index.ts'], outfile: 'core.mjs',
    platform: 'node', format: 'esm', target: 'node22.14', bundle: true,
    treeShaking: true, legalComments: 'inline', minify: false,
    metafile: true, write: false, logLevel: 'silent',
    banner: { js: '// Generated from local editable DeepSeek Harness sources. See UPSTREAM.json and THIRD_PARTY_NOTICES.md.\n// Rebuild: npm run build:harness' },
    plugins: [{
      name: 'merged-harness-sources',
      setup(builder) {
        builder.onResolve({ filter: /^@deepseek-ai\// }, args => {
          const name = args.path.split('/').slice(0, 2).join('/');
          const entry = upstream.packages[name];
          if (!entry || args.path !== name) throw new Error(`Unmerged official dependency: ${args.path}`);
          return { path: safePath(source, entry.path + '/' + entry.entry) };
        });
        builder.onResolve({ filter: /^zod(?:\/.*)?$/ }, args => {
          if (args.path !== 'zod') throw new Error(`Unexpected Zod entry: ${args.path}`);
          return { path: join(dirname(zodPackage), 'index.js') };
        });
        builder.onLoad({ filter: /[/\\]llm[/\\]llm[/\\]src[/\\]attribution\.ts$/ }, async args => {
          if (args.path !== attributionFile) throw new Error('Unexpected attribution source');
          const text = await readFile(args.path, 'utf8');
          const expression = "createRequire(import.meta.url)('../package.json')";
          if (text.split(expression).length !== 2) throw new Error('Upstream attribution version expression changed');
          const manifest = JSON.parse(await readFile(join(dirname(dirname(args.path)), 'package.json'), 'utf8'));
          return { contents: text.replace(expression, JSON.stringify({ version: manifest.version })), loader: 'ts' };
        });
      },
    }],
  });
  const builtins = new Set(builtinModules.flatMap(name => [name, 'node:' + name]));
  for (const output of Object.values(result.metafile.outputs)) {
    for (const item of output.imports) {
      if (item.external && !builtins.has(item.path)) throw new Error(`Unbundled runtime dependency: ${item.path}`);
    }
  }
  const bundle = result.outputFiles[0].contents;
  const inputs = await sourceHashes(directory);
  // Never certify a bundle from an earlier revision using hashes read after a
  // concurrent edit. The next build can consume the complete new revision.
  if (JSON.stringify(before) !== JSON.stringify(inputs)) throw new Error('Harness source changed during compilation; retry the build.');
  const manifest = {
    format: 1,
    upstream: { repository: upstream.repository, tag: upstream.tag, commit: upstream.commit, version: upstream.version },
    inputs,
    output: { path: 'core.mjs', sha256: sha256(bundle) },
    buildDependencies: { esbuild: esbuildVersion, zod: zod.version },
    // Local module paths prove the compiler used the source merge, not registry
    // packages. Generic Zod entries are recorded without machine-local paths.
    modules: Object.keys(result.metafile.inputs).map(name => name.includes('node_modules') ? 'zod/' + name.split(/node_modules[/\\]zod[/\\]/).at(-1) : name.replaceAll('\\', '/')).sort(),
  };
  await writeFile(join(directory, 'core.mjs'), bundle);
  await writeFile(join(directory, 'core-build.json'), JSON.stringify(manifest, null, 2) + '\n');
  await verifyCore(directory);
  return manifest;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const manifest = process.argv.includes('--check') ? await verifyCore() : await buildCore();
  console.log(JSON.stringify({ mode: 'source-merge', upstream: manifest.upstream, sourceFiles: Object.keys(manifest.inputs).length, sha256: manifest.output.sha256 }));
}
