import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import net from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { inspectRemainingProcesses, maintenanceMessage, parseMaintenanceArgs, runMaintenance } from '../../bin/code-agent-maintenance.mjs'
import { LauncherServices } from '../launcher-service.mjs'

const cli = fileURLToPath(new URL('../../bin/code-agent-maintenance.mjs', import.meta.url))
const runtime = { schemaVersion: 1, version: '0.10.2', node: 'runtimes/node/node.exe', python: 'runtimes/python/python.exe', webview2: 'runtimes/webview2' }
const required = [runtime.node, runtime.python, `${runtime.webview2}/msedgewebview2.exe`,
  'npm/launcher-service.mjs', 'npm/launcher-process.ps1', 'npm/launcher-preview.mjs', 'scripts/backend_idle_check.py',
  'web/dist/index.html', 'code_agent/server.py', 'code_agent/harness/node/runner.mjs']

async function unusedPort() {
  const server = net.createServer()
  await new Promise((done, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', done) })
  const port = server.address().port
  await new Promise(done => server.close(done))
  return port
}

async function fixture(t, { guard = 0, foreign, stopCode = 0 } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'code agent maintenance '))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const root = join(directory, '安装目录 & literal (root)'), home = join(directory, '用户数据 & home')
  await mkdir(home, { recursive: true })
  for (const name of required) {
    await mkdir(dirname(join(root, name)), { recursive: true })
    await writeFile(join(root, name), 'fixture; never executed')
  }
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: runtime.version }))
  await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify(runtime))
  const receipt = { schemaVersion: 1, appId: 'CodeAgent', version: runtime.version, installRoot: root,
    files: ['package.json', 'desktop-runtime.json', ...required] }
  await writeFile(join(root, 'installation-manifest.json'), JSON.stringify(receipt))
  await writeFile(join(root, 'npm/release.json'), JSON.stringify({ version: runtime.version, payloadHash: 'a'.repeat(64) }))
  const ports = { backend: await unusedPort(), frontend: await unusedPort() }
  await writeFile(join(home, 'desktop-services.json'), JSON.stringify(ports, null, 2) + '\n')
  const identities = {
    backend: { pid: 222, created: '2026-10-29T01:02:03.0000000Z', owned: foreign !== 'backend' },
    frontend: { pid: 333, created: '2026-10-29T01:02:04.0000000Z', owned: foreign !== 'frontend' },
  }
  const calls = [], created = []
  let scanned = 0
  t.mock.method(globalThis, 'fetch', async url => new Response(JSON.stringify(String(url).includes('/healthz')
    ? { ok: true, version: runtime.version } : { ok: true, service: 'code-agent-frontend' })))
  const adapters = { platform: 'win32', env: { LOCALAPPDATA: join(directory, 'Local AppData'), CODE_AGENT_LAUNCHER_BACKEND_PORT: '14444', CODE_AGENT_LAUNCHER_FRONTEND_PORT: '15555' },
    createManager(options) {
      created.push(options)
      return new LauncherServices({ ...options, spawnProcess() { assert.fail('maintenance cannot start a process') }, run: async (command, args, options) => {
        calls.push({ command, args, options })
        if (command.endsWith('python.exe')) return { code: guard, stdout: '{}' }
        assert.equal(command, 'powershell.exe')
        const action = args[args.indexOf('-Action') + 1]
        if (action === 'Inspect') return { code: 0, stdout: JSON.stringify(identities) }
        assert.equal(action, 'Stop')
        return { code: stopCode, stdout: '{}' }
      } })
    },
    async inspectRemaining() { scanned++; return [] },
  }
  return { directory, root, home, receipt, ports, calls, created, adapters, scanCount: () => scanned }
}

const operation = call => call.command.endsWith('python.exe') ? 'Guard' : call.args[call.args.indexOf('-Action') + 1]

test('maintenance stops through two idle guards and forwards saved ports and exact process identities', async t => {
  const f = await fixture(t)
  const portsFile = await readFile(join(f.home, 'desktop-services.json'), 'utf8')
  const result = await runMaintenance({ root: f.root, home: f.home, action: 'stop' }, f.adapters)
  assert.equal(result.ok, true)
  assert.equal(f.created[0].mode, 'bundled')
  assert.equal(f.created[0].backendPort, f.ports.backend)
  assert.equal(f.created[0].frontendPort, f.ports.frontend)
  assert.equal(await readFile(join(f.home, 'desktop-services.json'), 'utf8'), portsFile)
  assert.deepEqual(f.calls.map(operation), ['Inspect', 'Inspect', 'Guard', 'Guard', 'Stop', 'Inspect', 'Stop'])
  for (const call of f.calls.filter(item => operation(item) === 'Stop')) {
    const service = call.args[call.args.indexOf('-Service') + 1]
    assert.equal(call.args[call.args.indexOf('-ExpectedPid') + 1], service === 'backend' ? '222' : '333')
    assert.equal(call.args[call.args.indexOf('-ExpectedCreated') + 1], service === 'backend' ? '2026-10-29T01:02:03.0000000Z' : '2026-10-29T01:02:04.0000000Z')
    assert.equal(call.args[call.args.indexOf('-Root') + 1], f.root)
    assert.equal(call.args[call.args.indexOf('-BackendPort') + 1], String(f.ports.backend))
    assert.equal(call.args[call.args.indexOf('-FrontendPort') + 1], String(f.ports.frontend))
    assert.equal(call.options.cwd, f.root)
  }
  for (const call of f.calls.filter(item => operation(item) === 'Guard')) {
    assert.deepEqual(call.args.slice(0, 4), ['-I', '-B', '-X', 'utf8'])
    assert.equal(call.args[call.args.indexOf('--db') + 1], join(f.home, 'code_agent.db'))
  }
  assert.equal(f.scanCount(), 1)
})

test('a busy backend prevents every stop and preserves both services', async t => {
  const f = await fixture(t, { guard: 12 })
  await assert.rejects(runMaintenance({ root: f.root, home: f.home, action: 'stop' }, f.adapters), /后端仍有任务/)
  assert.deepEqual(f.calls.map(operation), ['Inspect', 'Inspect', 'Guard'])
  assert.equal(f.scanCount(), 0)
})

for (const foreign of ['backend', 'frontend']) test(`a foreign ${foreign} is rejected before stopping either application service`, async t => {
  const f = await fixture(t, { foreign })
  await assert.rejects(runMaintenance({ root: f.root, home: f.home, action: 'stop' }, f.adapters), /其他项目/)
  assert.deepEqual(f.calls.map(operation), ['Inspect'])
  assert.equal(f.scanCount(), 0)
})

test('a changed process identity cancels maintenance without a force-stop fallback', async t => {
  const f = await fixture(t, { stopCode: 1 })
  await assert.rejects(runMaintenance({ root: f.root, home: f.home, action: 'stop' }, f.adapters), /不能安全停止/)
  assert.deepEqual(f.calls.map(operation), ['Inspect', 'Inspect', 'Guard', 'Guard', 'Stop'])
  assert.equal(f.scanCount(), 0)
})

test('remaining launchers and bundled browsers are reported for the user to close', async t => {
  const f = await fixture(t)
  f.adapters.inspectRemaining = async () => [{ pid: 456, name: 'msedgewebview2.exe' }, { pid: 789, name: 'node.exe' }]
  await assert.rejects(runMaintenance({ root: f.root, home: f.home, action: 'stop' }, f.adapters), /msedgewebview2\.exe（PID 456）.*请关闭 Code-agent/)
  assert.equal(f.calls.filter(call => operation(call) === 'Stop').length, 2)
})

test('corrupted or ambiguous saved ports fail without reading process identities', async t => {
  const f = await fixture(t)
  for (const saved of [{ backend: '25380', frontend: 25381 }, { backend: 0, frontend: 25381 }, { backend: 25380, frontend: 25380 }]) {
    await writeFile(join(f.home, 'desktop-services.json'), JSON.stringify(saved))
    await assert.rejects(runMaintenance({ root: f.root, home: f.home, action: 'stop' }, f.adapters), /端口无效/)
  }
  await writeFile(join(f.home, 'desktop-services.json'), '{not-json')
  await assert.rejects(runMaintenance({ root: f.root, home: f.home, action: 'stop' }, f.adapters), /端口记录损坏/)
  assert.deepEqual(f.calls, [])
  assert.deepEqual(f.created, [])
})

test('invalid installation receipts cannot authorize maintenance of an arbitrary directory', async t => {
  const f = await fixture(t)
  for (const replacement of [{ appId: 'other-app' }, { installRoot: f.directory }, { version: '9.9.9' }, { files: [] }]) {
    await writeFile(join(f.root, 'installation-manifest.json'), JSON.stringify({ ...f.receipt, ...replacement }))
    await assert.rejects(runMaintenance({ root: f.root, home: f.home, action: 'stop' }, f.adapters), /安装|清单/)
  }
  assert.deepEqual(f.calls, [])
  assert.deepEqual(f.created, [])
})

test('maintenance uses the normal bundled data home and never creates or relocates remembered ports', async t => {
  const f = await fixture(t)
  const seen = []
  f.adapters.createManager = options => {
    seen.push(options)
    return { async initialize() {}, async status() { return [] }, async action(action, target) { assert.deepEqual([action, target], ['stop', 'all']) } }
  }
  const localHome = join(f.adapters.env.LOCALAPPDATA, 'Code-agent', 'data')
  await mkdir(localHome, { recursive: true })
  const saved = JSON.stringify({ backend: 26540, frontend: 26541 }) + '\n'
  await writeFile(join(localHome, 'desktop-services.json'), saved)
  await runMaintenance({ root: f.root, action: 'stop' }, f.adapters)
  assert.equal(seen[0].home, localHome)
  assert.deepEqual([seen[0].backendPort, seen[0].frontendPort], [26540, 26541])
  assert.equal(await readFile(join(localHome, 'desktop-services.json'), 'utf8'), saved)
  f.adapters.env.CODE_AGENT_HOME = join(f.directory, 'explicit env home')
  await runMaintenance({ root: f.root, action: 'stop' }, f.adapters)
  assert.equal(seen[1].home, f.adapters.env.CODE_AGENT_HOME)
  assert.deepEqual([seen[1].backendPort, seen[1].frontendPort], [8765, 5175])
  await assert.rejects(readFile(join(seen[1].home, 'desktop-services.json')), { code: 'ENOENT' })
})

test('read-only final scan uses literal environment paths and excludes only its own process', async t => {
  const f = await fixture(t)
  let captured
  const manager = { env: { PATH: 'preserved' }, run: async (...args) => { captured = args; return { code: 0, stdout: '[{"pid":456,"name":"node.exe"}]' } } }
  assert.deepEqual(await inspectRemainingProcesses(manager, { root: f.root, platform: 'win32', pid: 123 }), [{ pid: 456, name: 'node.exe' }])
  assert.equal(captured[0], 'powershell.exe')
  const script = Buffer.from(captured[1].at(-1), 'base64').toString('utf16le')
  assert.match(script, /Get-CimInstance Win32_Process/)
  assert.doesNotMatch(script, /Stop-Process|taskkill|\.Kill\(|Terminate|&\s/)
  assert.ok(!script.includes(f.root))
  assert.equal(captured[2].env.CODE_AGENT_MAINTENANCE_ROOT, f.root)
  assert.equal(captured[2].env.CODE_AGENT_MAINTENANCE_PID, '123')
  assert.equal(captured[2].cwd, f.root)
})

test('CLI accepts only absolute installation and data paths and stop action', () => {
  const root = resolve('literal root & no shell')
  assert.deepEqual(parseMaintenanceArgs(['--root', root, '--action', 'stop']), { root, action: 'stop' })
  for (const args of [['--root', root, '--action', 'kill'], ['--root', root, '--action', 'stop', '--force'],
    ['--root', 'relative', '--action', 'stop'], ['--root', root, '--home', 'relative', '--action', 'stop'],
    ['--root', root, '--root', root, '--action', 'stop']]) assert.throws(() => parseMaintenanceArgs(args))
})

test('maintenance messages redact credentials before stdout or stderr', () => {
  const result = maintenanceMessage(new Error('api_key=private-fixture password="private-password" Bearer private-bearer https://alice:private-url@example.test sk-private_fixture_key_12345'))
  assert.doesNotMatch(result, /private-fixture|private-password|private-bearer|private-url|private_fixture_key/)
})

test('standalone extracted CLI dynamically uses the old installation service module', { skip: process.platform !== 'win32' }, async t => {
  const f = await fixture(t)
  const extracted = join(f.directory, 'extracted maintenance with spaces.mjs')
  await copyFile(cli, extracted)
  const audit = join(f.directory, 'old-manager-audit.json')
  await writeFile(join(f.root, 'npm/launcher-service.mjs'), `import {writeFile} from 'node:fs/promises';
export class LauncherServices {
 constructor(options) {this.options=options;this.env=options.env;this.calls=[];}
 async initialize(){this.calls.push('initialize');}
 async status(){return [{id:'backend',state:'stopped'},{id:'frontend',state:'stopped'}];}
 async action(action,target){this.calls.push([action,target]);}
 async run(){await writeFile(${JSON.stringify(audit)},JSON.stringify({root:this.options.root,home:this.options.home,backendPort:this.options.backendPort,frontendPort:this.options.frontendPort,calls:this.calls}));return {code:0,stdout:'[]'};}
}`)
  const outcome = await new Promise((done, reject) => {
    const child = spawn(process.execPath, [extracted, '--root', f.root, '--home', f.home, '--action', 'stop'], { shell: false, windowsHide: true })
    let stdout = '', stderr = ''
    child.stdout.on('data', data => { stdout += data })
    child.stderr.on('data', data => { stderr += data })
    child.once('error', reject); child.once('close', code => done({ code, stdout, stderr }))
  })
  assert.equal(outcome.code, 0, outcome.stderr)
  assert.equal(outcome.stdout.trim().split('\n').length, 1)
  assert.equal(JSON.parse(outcome.stdout).ok, true)
  const saved = JSON.parse(await readFile(audit, 'utf8'))
  assert.deepEqual(saved.calls, ['initialize', ['stop', 'all']])
  assert.equal(saved.root, f.root)
  assert.equal(saved.backendPort, f.ports.backend)
  assert.equal(saved.frontendPort, f.ports.frontend)
})
