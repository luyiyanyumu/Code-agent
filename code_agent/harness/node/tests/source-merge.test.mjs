import assert from 'node:assert/strict';
import test from 'node:test';
import { cp, mkdtemp, readFile, writeFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { pathToFileURL } from 'node:url';
import { buildCore } from '../build.mjs';
import { runtimeDirectory, verifyCore } from '../verify.mjs';

const exec = promisify(execFile);

async function copyRuntime(t) {
  const temporary = await mkdtemp(join(tmpdir(), 'code-agent-source-merge-'));
  const directory = join(temporary, 'harness');
  t.after(async () => {
    // Never delete outside the exact OS temporary directory we created.
    assert(resolve(temporary).startsWith(resolve(tmpdir()) + sep));
    await rm(temporary, { recursive: true, force: true });
  });
  await cp(runtimeDirectory, directory, {
    recursive: true,
    filter: name => !name.split(/[\\/]/).some(part => part === 'node_modules' || part === 'tests'),
  });
  return directory;
}

async function runCopied(directory, body) {
  const script = `import assert from 'node:assert/strict';
    import {runHarness} from ${JSON.stringify(pathToFileURL(join(directory, 'runner.mjs')).href)};
    ${body}`;
  // No NODE_PATH, npm home, inherited NODE_OPTIONS, or repo cwd can supply a
  // hidden installed Harness. Only the copied sources and bundle are available.
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => ['SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'].includes(key.toUpperCase())));
  return exec(process.execPath, ['--input-type=module', '--eval', script], { cwd: directory, env, timeout: 15000 });
}

test('copied source release executes native model/tool rounds without node_modules or npm', async t => {
  const directory = await copyRuntime(t);
  await assert.rejects(stat(join(directory, 'node_modules')), { code: 'ENOENT' });
  const { stdout } = await runCopied(directory, `
    let models = 0, tools = 0;
    const result = await runHarness({
      messages:[{role:'user',content:'read then finish'}],
      tools:[{name:'read',description:'read',parameters:{type:'object',properties:{}}}],
    }, async (method, frame) => {
      if (method === 'tool') { tools++; assert.equal(frame.call.name,'read'); return 'copied runtime evidence'; }
      if (++models === 1) return {tool_calls:[{id:'read-1',name:'read',arguments:{}}],stop_reason:'tool_use'};
      assert.equal(frame.messages.at(-1).content,'copied runtime evidence');
      return {text:'delivered',stop_reason:'end_turn'};
    });
    assert.equal(models,2); assert.equal(tools,1);
    assert.equal(result.text,'delivered'); assert.equal(result.stop_reason,'end_turn');
    assert.equal(result.harness.mode,'source-merge');
    console.log(JSON.stringify(result.harness));
  `);
  assert.equal(JSON.parse(stdout).commit, '1600d8a1e140dcde43a1a0941940a0480712c011');
});

test('editing merged TypeScript invalidates startup and rebuilding changes real loop behavior', async t => {
  const directory = await copyRuntime(t);
  const source = join(directory, 'vendor/deepseek-harness/packages/core/tools/src/index.ts');
  const original = await readFile(source, 'utf8');
  assert(original.includes('`unknown tool "${toolName}"`'));
  await writeFile(source, original.replace('`unknown tool "${toolName}"`', '`LOCAL_SOURCE_PROOF "${toolName}"`'));
  await assert.rejects(verifyCore(directory), /source changed:/);
  await assert.rejects(runCopied(directory, ''), /source\/build mismatch/);
  const built = await buildCore(directory);
  assert(built.modules.includes('vendor/deepseek-harness/packages/core/tools/src/index.ts'));
  const { stdout } = await runCopied(directory, `
    let models=0;
    const result=await runHarness({messages:[{role:'user',content:'inspect unknown tool'}]},async(method,frame)=>{
      assert.equal(method,'model');
      if(++models===1) return {tool_calls:[{id:'missing-1',name:'not_registered',arguments:{}}],stop_reason:'tool_use'};
      assert.match(frame.messages.at(-1).content,/LOCAL_SOURCE_PROOF/);
      return {text:'local source executed',stop_reason:'end_turn'};
    });
    assert.equal(models,2); assert.equal(result.stop_reason,'end_turn');
    console.log(result.text);
  `);
  assert.equal(stdout.trim(), 'local source executed');
  // The release sources themselves have never been modified by the proof.
  assert.equal(await readFile(join(runtimeDirectory, 'vendor/deepseek-harness/packages/core/tools/src/index.ts'), 'utf8'), original);
});

test('source inventory additions and undeclared official imports fail closed', async t => {
  const directory = await copyRuntime(t);
  await writeFile(join(directory, 'src/extra.ts'), 'export const added = true;\n');
  await assert.rejects(verifyCore(directory), /inventory changed/);
  const entry = join(directory, 'src/index.ts');
  await writeFile(entry, (await readFile(entry, 'utf8')) + "\nexport { missing } from '@deepseek-ai/unmerged-package';\n");
  await assert.rejects(buildCore(directory), /Unmerged official dependency/);
});
