import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { applyUpdate, checkUpdate, detectMode } from '../launcher-update.mjs'

async function fixture(t, { source = true } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'code agent launcher update '))
  t.after(() => rm(root, { recursive: true, force: true }))
  await writeFile(join(root, 'package.json'), JSON.stringify({ name: '@luyiyanyumu/code-agent', version: '0.10.2' }))
  if (source) await writeFile(join(root, '.git'), 'gitdir: /worktree/example')
  const python = join(root, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin', process.platform === 'win32' ? 'python.exe' : 'python')
  await mkdir(dirname(python), { recursive: true })
  await writeFile(python, '')
  return { root, python }
}

function runner(root, { dirty = '', counts = '0\t2', remote = 'https://github.com/luyiyanyumu/Code-agent.git', onCall, fetchCode = 0, mergeCode = 0 } = {}) {
  const calls = []
  return {
    calls,
    run: async (executable, args, options) => {
      calls.push({ executable, args, options })
      const result = await onCall?.(executable, args, options, calls)
      if (result) return result
      if (executable !== 'git') return { code: 0, stdout: 'build OK' }
      const signature = args.join(' ')
      const stdout = {
        'status --porcelain --untracked-files=all': dirty,
        'rev-parse --show-toplevel': root,
        'symbolic-ref --quiet --short HEAD': 'master',
        'config --get branch.master.remote': 'origin',
        'remote get-url origin': remote,
        'rev-parse --abbrev-ref --symbolic-full-name @{upstream}': 'origin/master',
        'rev-parse HEAD': 'a'.repeat(40),
        'rev-list --left-right --count HEAD...@{upstream}': counts,
      }[signature] || ''
      return { code: args[0] === 'fetch' ? fetchCode : args[0] === 'merge' ? mergeCode : 0, stdout }
    },
  }
}

test('mode detection recognizes .git files without confusing installed pyproject with a checkout', async t => {
  const source = await fixture(t)
  const installed = await fixture(t, { source: false })
  await writeFile(join(installed.root, 'pyproject.toml'), '[project]')
  assert.equal(await detectMode(source.root), 'source')
  assert.equal(await detectMode(installed.root), 'installed')
})

test('dirty source including untracked files is rejected before fetch, installation or writes', async t => {
  const { root } = await fixture(t)
  await writeFile(join(root, 'local notes.txt'), 'keep my changes')
  const fake = runner(root, { dirty: '?? local notes.txt' })
  await assert.rejects(applyUpdate({ root, run: fake.run }), /未提交或未跟踪/)
  assert.deepEqual(fake.calls.map(call => call.args), [['status', '--porcelain', '--untracked-files=all']])
  assert.equal(await readFile(join(root, 'local notes.txt'), 'utf8'), 'keep my changes')
})

test('source check fetches only official tracking refs and reports same-version commits accurately', async t => {
  const { root } = await fixture(t)
  const fake = runner(root, { dirty: ' M local.py', counts: '0 3' })
  const result = await checkUpdate({ root, run: fake.run, fetchImpl() { assert.fail('source checking does not use registry version') } })
  assert.equal(result.current, '0.10.2')
  assert.equal(result.available, true)
  assert.equal(result.source.behind, 3)
  assert.equal(result.source.dirty, true)
  assert.match(result.message, /保留/)
  assert.deepEqual(fake.calls.find(call => call.args[0] === 'fetch').args, ['fetch', '--no-tags', 'origin'])
  assert.equal(fake.calls.some(call => ['merge', 'reset', 'clean', 'stash'].includes(call.args[0])), false)
})

test('unrecognized remote and credential-bearing URL are refused without fetch or secret output', async t => {
  const { root } = await fixture(t)
  for (const remote of ['https://example.test/Code-agent.git', 'https://user:secret@github.com/luyiyanyumu/Code-agent.git']) {
    const fake = runner(root, { remote })
    const result = await checkUpdate({ root, run: fake.run })
    assert.match(result.error, /不是 Code-agent 官方/)
    assert.doesNotMatch(JSON.stringify(result), /secret|example\.test/)
    assert.equal(fake.calls.some(call => call.args[0] === 'fetch'), false)
  }
})

test('source rechecks dirty state after fetch and never merges newly edited files', async t => {
  const { root } = await fixture(t)
  let status = 0
  const fake = runner(root, { onCall: async (exe, args) => {
    if (args[0] === 'status') return { code: 0, stdout: ++status === 1 ? '' : ' M user-edit.py' }
  } })
  await assert.rejects(applyUpdate({ root, run: fake.run }), /检查期间出现了本地改动/)
  assert.equal(status, 2)
  assert.equal(fake.calls.some(call => call.args[0] === 'merge'), false)
})

test('diverged source refuses automatic merge rather than resetting or stashing', async t => {
  const { root } = await fixture(t)
  const fake = runner(root, { counts: '1 2' })
  await assert.rejects(applyUpdate({ root, run: fake.run }), /已分叉/)
  assert.equal(fake.calls.some(call => ['merge', 'reset', 'clean', 'stash'].includes(call.args[0])), false)
})

test('clean source uses only fast-forward then rebuilds frontend, backend and Harness', async t => {
  const { root, python } = await fixture(t)
  const fake = runner(root)
  const logs = []
  const result = await applyUpdate({ root, run: fake.run, python, npmCli: '/npm path/npm-cli.js', node: '/node path/node', log: line => logs.push(line) })
  assert.equal(result.updated, true)
  assert.equal(result.restartRequired, true)
  assert.deepEqual(fake.calls.find(call => call.args[0] === 'merge').args, ['merge', '--ff-only', '--no-edit', '@{upstream}'])
  assert.deepEqual(fake.calls.filter(call => call.executable === '/node path/node').map(call => call.args), [
    ['/npm path/npm-cli.js', 'ci', '--no-audit', '--no-fund'],
    ['/npm path/npm-cli.js', 'ci', '--prefix', 'web', '--no-audit', '--no-fund'],
    [join(root, 'npm', 'build.mjs')],
  ])
  assert.equal(fake.calls.some(call => call.executable === python && call.args.includes('pip') && call.args.at(-1) === root), true)
  assert.equal(fake.calls.some(call => call.args.includes('code_agent.harness')), true)
  assert.equal(fake.calls.some(call => call.args.at(-1) === 'import code_agent.server'), true)
  assert.equal(fake.calls.every(call => call.options.capture && call.options.cwd === root), true)
  assert.equal(fake.calls.some(call => ['reset', 'clean', 'stash'].includes(call.args[0])), false)
  assert.match(logs.join('\n'), /构建内核和前端界面/)
})

test('ff-only failure is surfaced and does not install or use destructive fallback', async t => {
  const { root } = await fixture(t)
  const fake = runner(root, { mergeCode: 1 })
  await assert.rejects(applyUpdate({ root, run: fake.run, npmCli: '/npm/npm-cli.js' }), /快进更新源码失败/)
  assert.equal(fake.calls.every(call => call.executable === 'git'), true)
  assert.equal(fake.calls.some(call => ['reset', 'clean', 'stash'].includes(call.args[0])), false)
})

test('unchanged source skips all rebuilds and installed-latest checks do not downgrade', async t => {
  const { root } = await fixture(t)
  const fake = runner(root, { counts: '2 0' })
  const result = await applyUpdate({ root, run: fake.run })
  assert.equal(result.updated, false)
  assert.equal(result.restartRequired, false)
  assert.equal(fake.calls.every(call => call.executable === 'git'), true)
  const installed = await fixture(t, { source: false })
  const same = await checkUpdate({ root: installed.root, fetchImpl: async () => ({ ok: true, json: async () => ({ name: '@luyiyanyumu/code-agent', version: '0.10.1' }) }) })
  assert.equal(same.available, false)
})

test('registry query uses fixed HTTPS URL, rejects redirects and reports failure honestly', async t => {
  const { root } = await fixture(t, { source: false })
  let request
  const result = await checkUpdate({ root, fetchImpl: async (...args) => { request = args; return { ok: false } } })
  assert.equal(request[0], 'https://registry.npmjs.org/@luyiyanyumu%2fcode-agent/latest')
  assert.equal(request[1].redirect, 'error')
  assert.equal(result.available, false)
  assert.equal(result.latest, null)
  assert.match(result.error, /暂不可用/)
  const invalid = await checkUpdate({ root, fetchImpl: async () => ({ ok: true, json: async () => ({ name: 'some-other-package', version: '99.0.0' }) }) })
  assert.match(invalid.error, /包信息不完整/)
})

test('npm mode stages official latest with literal path arguments and does not claim live upgrade', async t => {
  const { root } = await fixture(t, { source: false })
  const fake = runner(root)
  const logs = []
  const home = join(root, 'persistent data & keep')
  const result = await applyUpdate({ root, home, python: 'C:\\Python & More\\python.exe', npmCli: '/npm path/npm-cli.js', node: '/node path/node', env: { CODE_AGENT_UPDATE_PACKAGE: 'evil@latest' }, run: fake.run, log: line => logs.push(line) })
  assert.deepEqual(fake.calls[0].args, ['/npm path/npm-cli.js', 'exec', '--yes', '--prefer-online', '--package', '@luyiyanyumu/code-agent@latest', '--', 'code-agent', 'install', '--home', home, '--python', 'C:\\Python & More\\python.exe'])
  assert.equal(result.staged, true)
  assert.equal(result.updated, false)
  assert.match(result.message, /当前启动器仍属于原安装包/)
  assert.match(result.message, /当前服务继续运行/)
  assert.equal(fake.calls.length, 1)
})

test('build failure keeps updated source and redacts tokens from bounded diagnostic output', async t => {
  const { root } = await fixture(t)
  const secret = 'super-private-api-value'
  const logs = []
  const fake = runner(root, { onCall: async (exe, args) => {
    if (exe !== 'git') return { code: 1, stderr: `token=${secret} https://user:password@host.test/package\n${'x'.repeat(20000)}\nBearer bearer-secret ${secret}` }
  } })
  await assert.rejects(applyUpdate({ root, run: fake.run, npmCli: '/npm/npm-cli.js', env: { OPENAI_API_KEY: secret }, log: line => logs.push(line) }), /源码已快进更新，但构建尚未完成/)
  assert.doesNotMatch(logs.join('\n'), /super-private-api-value|bearer-secret|user:password/)
  assert.ok(Math.max(...logs.map(line => line.length)) <= 16000)
  assert.equal(fake.calls.some(call => ['reset', 'clean', 'stash'].includes(call.args[0])), false)
})

test('installed prerelease comparison recognizes stable and numeric newer versions', async t => {
  const { root } = await fixture(t, { source: false })
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: '1.0.0-rc.2' }))
  for (const [version, available] of [['1.0.0-rc.10', true], ['1.0.0', true], ['1.0.0-rc.1', false]]) {
    const result = await checkUpdate({ root, fetchImpl: async () => ({ ok: true, json: async () => ({ name: '@luyiyanyumu/code-agent', version }) }) })
    assert.equal(result.available, available, version)
  }
})
