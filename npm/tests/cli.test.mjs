import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import net from 'node:net'
import test from 'node:test'
import { assertPortFree, browserUrl, ensureRuntime, findPython, latestPackage, main, parseArgs, runCommand, runtimePaths, updateLatest } from '../cli.mjs'

const release = { version: '0.10.1', payloadHash: 'a'.repeat(64) }
async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'code agent npm test '))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const root = join(directory, 'package with spaces')
  const home = join(directory, 'persistent home')
  await mkdir(join(root, 'npm'), { recursive: true })
  await mkdir(join(root, 'web', 'dist'), { recursive: true })
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: release.version }))
  await writeFile(join(root, 'npm', 'release.json'), JSON.stringify(release))
  await writeFile(join(root, 'web', 'dist', 'index.html'), '<!doctype html>')
  return { directory, root, home }
}

function fakeInstaller(calls, paths, overrides = {}) {
  return async (command, args, options) => {
    calls.push({ command, args, options })
    if (args.includes('-c') && args.at(-1).includes('sys.version_info')) return { code: 0, stdout: '[3, 12, 5]\n' }
    if (args.includes('venv')) {
      await mkdir(dirname(paths.python), { recursive: true })
      await writeFile(paths.python, '')
    }
    if (args.includes('pip') && overrides.pipCode) return { code: overrides.pipCode }
    if (args.at(-1) === 'import code_agent.server' && overrides.importCode) return { code: overrides.importCode }
    return { code: 0 }
  }
}

test('arguments accept quoted paths and reject malformed or unknown options', () => {
  assert.deepEqual(parseArgs(['install', '--home', 'C:\\My Files\\Code Agent', '--python=C:\\Python Dir\\python.exe', '--port', '8080']), {
    command: 'install', host: '127.0.0.1', port: 8080, open: true,
    home: 'C:\\My Files\\Code Agent', python: 'C:\\Python Dir\\python.exe',
  })
  assert.equal(parseArgs(['--no-open']).open, false)
  assert.throws(() => parseArgs(['--port', '0']), /1 到 65535/)
  assert.throws(() => parseArgs(['--port', '1.5']), /1 到 65535/)
  assert.throws(() => parseArgs(['--port', '--home']), /缺少参数值/)
  assert.throws(() => parseArgs(['--host', 'localhost&evil']), /主机名/)
  assert.throws(() => parseArgs(['--provider', 'other']), /不支持/)
  assert.throws(() => parseArgs(['--force']), /未知参数/)
})

test('runtime cache is persistent and separates payloads, never an npx path', () => {
  const first = runtimePaths('/data/user home', release, 'linux')
  assert.match(first.directory.replaceAll('\\', '/'), /runtime\/npm\/0\.10\.1-aaaaaaaaaaaaaaaa$/)
  assert.equal(first.python, join(first.directory, 'bin', 'python'))
  const other = runtimePaths('/data/user home', { ...release, payloadHash: 'b'.repeat(64) }, 'win32')
  assert.notEqual(first.directory, other.directory)
  assert.equal(other.python, join(other.directory, 'Scripts', 'python.exe'))
  assert.throws(() => runtimePaths('/data', { version: '../unsafe', payloadHash: 'a'.repeat(64) }), /校验信息/)
})

test('install preserves paths with spaces, checks imports, then skips all work on reuse', async t => {
  const { root, home } = await fixture(t)
  const paths = runtimePaths(home, release, 'win32')
  const calls = []
  const options = { root, home, release, platform: 'win32', python: 'C:\\Python 3.12\\python.exe', env: {}, log() {}, run: fakeInstaller(calls, paths) }
  assert.deepEqual(await ensureRuntime(options), paths)
  assert.equal(calls[0].command, 'C:\\Python 3.12\\python.exe')
  assert.deepEqual(calls[1].args, ['-m', 'venv', paths.directory])
  assert.equal(calls[2].args.at(-1), root)
  assert.deepEqual(calls[2].args.slice(0, 5), ['-I', '-X', 'utf8', '-m', 'pip'])
  assert.equal(calls[2].options.cwd, root)
  assert.deepEqual(calls[3].args, ['-I', '-X', 'utf8', '-c', 'import code_agent.server'])
  assert.deepEqual(JSON.parse(await readFile(paths.ready, 'utf8')), release)
  assert.equal(existsSync(paths.lock), false)
  const before = calls.length
  await ensureRuntime(options)
  assert.equal(calls.length, before)
})

for (const [stage, overrides] of [['pip', { pipCode: 42 }], ['import', { importCode: 9 }]]) {
  test(`${stage} failure leaves no ready marker, releases lock, and a retry succeeds`, async t => {
    const { root, home } = await fixture(t)
    const paths = runtimePaths(home, release)
    const options = { root, home, release, env: {}, log() {}, run: fakeInstaller([], paths, overrides) }
    await assert.rejects(ensureRuntime(options), /失败/)
    assert.equal(existsSync(paths.ready), false)
    assert.equal(existsSync(paths.lock), false)
    await ensureRuntime({ ...options, run: fakeInstaller([], paths) })
    assert.equal(existsSync(paths.ready), true)
  })
}

test('concurrent installation refuses clearly without touching the active lock', async t => {
  const { root, home } = await fixture(t)
  const paths = runtimePaths(home, release)
  await mkdir(paths.lock, { recursive: true })
  await assert.rejects(ensureRuntime({ root, home, release, run() { throw new Error('must not run') } }), /正在安装/)
  assert.equal(existsSync(paths.lock), true)
})

test('Python selection refuses old versions and honors explicit paths without fallback', async () => {
  const calls = []
  await assert.rejects(findPython({ explicit: '/old python', run: async (command, args) => { calls.push([command, args]); return { code: 0, stdout: '[3,10,12]' } } }), /低于 3.11/)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], '/old python')
  const selected = await findPython({ platform: 'win32', env: {}, run: async command => {
    if (command === 'py') throw Object.assign(new Error('missing'), { code: 'ENOENT' })
    return { code: 0, stdout: '[3,11,0]' }
  } })
  assert.deepEqual(selected, { command: 'python3', prefix: [] })
  await assert.rejects(findPython({ env: {}, run: async () => ({ code: 130 }) }), error => error.exitCode === 130)
})

test('update uses npm JS entry, online package fetch, exact arguments, and child exit status', async () => {
  assert.equal(latestPackage, '@luyiyanyumu/code-agent@latest')
  let call
  const result = await updateLatest({ home: 'C:\\Data with spaces', python: 'C:\\Python & More\\python.exe' }, {
    node: '/node', npmCli: '/npm with spaces/npm-cli.js', env: {}, cwd: '/work', log() {},
    run: async (...args) => { call = args; return { code: 37 } },
  })
  assert.equal(result, 37)
  assert.deepEqual(call, ['/node', ['/npm with spaces/npm-cli.js', 'exec', '--yes', '--prefer-online', '--package', '@luyiyanyumu/code-agent@latest', '--', 'code-agent', 'install', '--home', 'C:\\Data with spaces', '--python', 'C:\\Python & More\\python.exe'], { cwd: '/work', env: {} }])
})

test('successful registry update displays the short npx command and preserves option reminder', async () => {
  const messages = []
  const calls = []
  assert.equal(await updateLatest({}, {
    npmCli: '/npm/npm-cli.js', env: { CODE_AGENT_UPDATE_PACKAGE: '@luyiyanyumu/code-agent@latest' }, log: message => messages.push(message),
    run: async (command, args) => { calls.push([command, args]); return { code: 0 } },
  }), 0)
  assert.deepEqual(calls[0][1].slice(1, 6), ['exec', '--yes', '--prefer-online', '--package', '@luyiyanyumu/code-agent@latest'])
  assert.match(messages.at(-1), /npx @luyiyanyumu\/code-agent@latest 启动新版/)
  assert.match(messages.at(-1), /--home、--port/)
  assert.doesNotMatch(messages.at(-1), /--package|github\.com/)
})

test('update source rejects arbitrary npm specs, flags, credentials, and malformed URLs before spawning', async () => {
  for (const source of [
    '--registry=https://evil.test', '-h', '@luyiyanyumu/code-agent@latest --help', 'other-package@latest',
    'npm:other-package@latest', 'code-agent@file:./untrusted', 'git+https://evil.test/repo.git',
    'http://evil.test/package.tgz', 'https://', 'https://user:secret@evil.test/package.tgz',
    'https://evil.test/package.tgz#fragment', 'https://evil.test/package.tgz --help',
    'https://evil.test/package.tgz\n--registry=bad', './relative-package.tgz', '@luyiyanyumu/code-agent@latest\u0000',
  ]) {
    await assert.rejects(updateLatest({}, {
      env: { CODE_AGENT_UPDATE_PACKAGE: source }, npmCli: '/npm/npm-cli.js', log() {},
      run() { assert.fail('invalid update source must not spawn a process') },
    }), /更新包必须是/)
  }
})

test('legacy HTTPS and local absolute tarball sources remain single npm arguments for smoke tests', async () => {
  for (const source of [
    'https://github.com/luyiyanyumu/Code-agent/releases/latest/download/code-agent.tgz',
    join(tmpdir(), 'code agent smoke', 'code-agent.tgz'),
  ]) {
    let args
    assert.equal(await updateLatest({}, {
      env: { CODE_AGENT_UPDATE_PACKAGE: source }, npmCli: '/npm/npm-cli.js', log() {},
      run: async (command, passed) => { args = passed; return { code: 0 } },
    }), 0)
    assert.equal(args[5], source)
    assert.deepEqual(args.slice(6), ['--', 'code-agent', 'install'])
  }
})

test('busy port check runs before dependency install and never starts a child', async t => {
  const { root, home } = await fixture(t)
  const messages = []
  const code = await main(['--home', home, '--no-open'], { root, env: {}, log() {}, error: message => messages.push(message), checkPort: async () => { throw new Error('端口 8765 已被占用') }, run() { assert.fail('must not launch') } })
  assert.equal(code, 1)
  assert.match(messages.join(), /端口 8765 已被占用/)
  assert.equal(existsSync(home), false)
})

test('real socket preflight reports occupation and leaves listener running', async t => {
  const listener = net.createServer()
  await new Promise(resolveListen => listener.listen(0, '127.0.0.1', resolveListen))
  t.after(() => new Promise(resolveClose => listener.close(resolveClose)))
  await assert.rejects(assertPortFree('127.0.0.1', listener.address().port), /已被占用/)
  assert.equal(listener.listening, true)
})

test('start uses installed runtime with caller cwd, persistent config, provider and exact exit code', async t => {
  const { root, home, directory } = await fixture(t)
  const paths = runtimePaths(home, release)
  await mkdir(dirname(paths.python), { recursive: true })
  await writeFile(paths.python, '')
  await writeFile(paths.ready, JSON.stringify(release))
  await writeFile(join(home, '.env'), 'DEEPSEEK_API_KEY=placeholder\n')
  const calls = []
  let checks = 0
  const code = await main(['--home', home, '--provider', 'offline', '--port', '9999', '--no-open'], {
    root, cwd: directory, env: {}, log() {}, checkPort: async () => { checks++ },
    run: async (command, args, options) => { calls.push({ command, args, options }); return { code: 17 } },
  })
  assert.equal(code, 17)
  assert.equal(checks, 2)
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0].args, ['-I', '-X', 'utf8', '-m', 'code_agent.server', '--host', '127.0.0.1', '--port', '9999', '--provider', 'offline'])
  assert.equal(calls[0].options.cwd, directory)
  assert.equal(calls[0].options.env.CODE_AGENT_HOME, home)
  assert.equal(calls[0].options.env.CODE_AGENT_WEB_DIST, join(root, 'web', 'dist'))
  assert.equal(calls[0].options.env.CODE_AGENT_ENV_FILE, join(home, '.env'))
})

test('help and version do not require a Python runtime or built assets', async t => {
  const { root } = await fixture(t)
  await rm(join(root, 'web'), { recursive: true })
  const messages = []
  for (const args of [['--help'], ['--version']]) assert.equal(await main(args, { root, log: message => messages.push(message), run() { assert.fail('must not run') } }), 0)
  assert.match(messages[0], /--no-open/)
  assert.equal(messages[1], release.version)
})

test('browser URLs connect through localhost for wildcard bind addresses', () => {
  assert.equal(browserUrl('0.0.0.0', 8765), 'http://127.0.0.1:8765')
  assert.equal(browserUrl('::', 8765), 'http://[::1]:8765')
  assert.equal(browserUrl('::1', 8765), 'http://[::1]:8765')
})

test('actual subprocess runner captures output and preserves unsuccessful exit status', async () => {
  const result = await runCommand(process.execPath, ['-e', 'process.stdout.write("path with spaces"); process.stderr.write("diagnostic"); process.exit(23)'], { capture: true })
  assert.deepEqual(result, { code: 23, stdout: 'path with spaces', stderr: 'diagnostic' })
})

test('relative Python paths resolve from the user project rather than npm cache', async t => {
  const { root, home, directory } = await fixture(t)
  const paths = runtimePaths(home, release)
  const calls = []
  assert.equal(await main(['install', '--home', home, '--python', './python folder/python'], {
    root, cwd: directory, env: {}, log() {}, run: fakeInstaller(calls, paths),
  }), 0)
  assert.equal(calls[0].command, join(directory, 'python folder', 'python'))
})

test('Ctrl+C-style cancellation during install releases lock and forwards exit code', async t => {
  const { root, home } = await fixture(t)
  const paths = runtimePaths(home, release)
  let attempts = 0
  assert.equal(await main(['install', '--home', home], {
    root, env: {}, log() {}, error() {},
    run: async () => ++attempts === 1 ? { code: 0, stdout: '[3,11,0]' } : { code: 130 },
  }), 130)
  assert.equal(existsSync(paths.lock), false)
  assert.equal(existsSync(paths.ready), false)
})

test('browser opening monitor is canceled and awaited when server exits', async t => {
  const { root, home } = await fixture(t)
  const paths = runtimePaths(home, release)
  await ensureRuntime({ root, home, release, env: {}, log() {}, run: fakeInstaller([], paths) })
  let stopped = false
  assert.equal(await main(['--home', home], {
    root, env: {}, log() {}, checkPort: async () => {}, run: async () => ({ code: 0 }),
    openWhenHealthy: async (url, signal) => {
      assert.equal(url, 'http://127.0.0.1:8765')
      await new Promise(resolveStop => signal.addEventListener('abort', resolveStop, { once: true }))
      stopped = true
    },
  }), 0)
  assert.equal(stopped, true)
})
