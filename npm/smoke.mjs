// Real tarball -> npm exec -> isolated Python runtime -> HTTP -> update -> restart.
import assert from 'node:assert/strict'
import { createServer } from 'node:net'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, readdir, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const args = process.argv.slice(2)
function argument(name) { const index = args.indexOf(name); return index < 0 ? null : args[index + 1] }
const tarball = argument('--tarball') && resolve(argument('--tarball'))
const previous = argument('--previous') ? resolve(argument('--previous')) : tarball
if (!tarball || !existsSync(tarball) || !existsSync(previous)) throw new Error('用法：npm run test:package -- --tarball <npm-pack.tgz> [--previous <older.tgz>]，指定的包必须已存在。')
const npmCli = process.env.npm_execpath || join(dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js')
if (!existsSync(npmCli) || !/\.[cm]?js$/.test(npmCli)) throw new Error('请使用 npm run test:package 运行此检查。')
const scratch = await mkdtemp(join(tmpdir(), 'code-agent-npx smoke '))
const dataHome = join(scratch, 'persistent data')
await mkdir(dataHome)
const env = {
  ...process.env,
  npm_config_cache: join(scratch, 'npm cache'),
  npm_config_audit: 'false',
  npm_config_fund: 'false',
  CODE_AGENT_HOME: dataHome,
  CODE_AGENT_AUTH_ENABLED: 'false',
  CODE_AGENT_SKIP_MIGRATION: '1',
  CODE_AGENT_UPDATE_PACKAGE: tarball,
  CODE_AGENT_ENV_FILE: join(scratch, '.env'),
}
await writeFile(env.CODE_AGENT_ENV_FILE, 'CODE_AGENT_PROVIDER=offline\n')
const logs = []
function launch(source, commandArgs) {
  const child = spawn(process.execPath, [npmCli, 'exec', '--yes', '--package', source, '--', 'code-agent', ...commandArgs], {
    cwd: scratch, env, shell: false, stdio: ['ignore', 'pipe', 'pipe'], detached: process.platform !== 'win32',
  })
  child.stdout.on('data', data => logs.push(data.toString()))
  child.stderr.on('data', data => logs.push(data.toString()))
  return child
}
async function complete(source, commandArgs) {
  const child = launch(source, commandArgs)
  await new Promise((resolveExit, reject) => {
    const timeout = setTimeout(() => { stop(child).finally(() => reject(new Error('npx 命令执行超时'))) }, 300_000)
    child.once('error', error => { clearTimeout(timeout); reject(error) })
    child.once('exit', code => { clearTimeout(timeout); code === 0 ? resolveExit() : reject(new Error(`npx ${commandArgs.join(' ')} 退出码 ${code}`)) })
  })
}
async function stop(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return
  if (process.platform === 'win32') {
    // This PID is the test-owned npm process; /T includes only its own descendants.
    await new Promise(resolveStop => {
      const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { shell: false, stdio: 'ignore' })
      killer.once('error', resolveStop)
      killer.once('exit', resolveStop)
    })
  } else {
    try { process.kill(-child.pid, 'SIGTERM') } catch (error) { if (error.code !== 'ESRCH') throw error }
  }
  if (child.exitCode === null && child.signalCode === null) {
    await Promise.race([new Promise(resolveStop => child.once('exit', resolveStop)), delay(10_000)])
  }
}
async function freePort() {
  const server = createServer()
  await new Promise((resolveListen, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolveListen) })
  const port = server.address().port
  await new Promise(resolveClose => server.close(resolveClose))
  return port
}
async function ready(url, child) {
  const deadline = Date.now() + 90_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error('服务在就绪前退出')
    try { const result = await fetch(`${url}/healthz`, { signal: AbortSignal.timeout(1500) }); if (result.ok && (await result.json()).ok) return } catch { /* wait for actual startup */ }
    await delay(250)
  }
  throw new Error('服务未在 90 秒内就绪')
}
async function request(url, path, options = {}) {
  const response = await fetch(url + path, { ...options, signal: AbortSignal.timeout(30_000), headers: { 'Content-Type': 'application/json', ...options.headers } })
  assert.equal(response.status, 200, `${path}: ${await response.clone().text()}`)
  return response.json()
}
const homeArgs = ['--home', dataHome]
let running
let firstSession
let firstEvents
try {
  console.log(`[package-smoke] 隔离目录：${scratch}`)
  console.log('[package-smoke] 通过 npm exec 安装打包后的程序和 Python 依赖…')
  await complete(previous, ['install', ...homeArgs])
  const sentinel = join(dataHome, 'keep-user-settings.txt')
  await writeFile(sentinel, 'user data survives package updates\n')
  const firstRuntimes = (await readdir(join(dataHome, 'runtime', 'npm'))).filter(name => !name.endsWith('.lock'))
  console.log('[package-smoke] 通过 npm exec 启动服务，验证界面、API 与真实离线工具执行…')
  let port = await freePort()
  let url = `http://127.0.0.1:${port}`
  running = launch(previous, ['start', '--no-open', '--provider', 'offline', '--port', String(port), ...homeArgs])
  await ready(url, running)
  const html = await (await fetch(url)).text()
  assert.match(html, /<div id="app">/)
  const asset = /<script[^>]+src="([^"]+)"/.exec(html)?.[1]
  assert.ok(asset, '包内缺少构建后的脚本')
  const script = await fetch(new URL(asset, url))
  assert.equal(script.status, 200)
  assert.match(script.headers.get('content-type'), /javascript/)
  assert.equal((await fetch(`${url}/api/v1/not-a-real-route`)).status, 404)
  assert.equal((await fetch(`${url}/assets/not-a-real-file.js`)).status, 404)
  const api = await request(url, '/openapi.json')
  assert.equal(Object.keys(api.paths).some(path => path.includes('workflow')), false)
  const session = await request(url, '/api/v1/sessions', { method: 'POST', body: '{}' })
  firstSession = session.id
  const reply = await request(url, `/api/v1/sessions/${firstSession}/prompt`, { method: 'POST', body: JSON.stringify({ text: '跑个 echo' }) })
  assert.equal(reply.stop_reason, 'end_turn')
  assert.match(reply.text, /hello-from-code-agent/)
  firstEvents = await request(url, `/api/v1/sessions/${firstSession}/events`)
  assert.ok(firstEvents.some(event => event.type === 'tool_result'), '必须有真实工具结果')
  await stop(running)
  running = null
  console.log('[package-smoke] 执行 update，再用新版重启，验证会话与执行记录保留…')
  await complete(previous, ['update', ...homeArgs])
  assert.equal(await readFile(sentinel, 'utf8'), 'user data survives package updates\n')
  const updatedRuntimes = (await readdir(join(dataHome, 'runtime', 'npm'))).filter(name => !name.endsWith('.lock'))
  if (previous !== tarball) assert.ok(updatedRuntimes.some(name => !firstRuntimes.includes(name)), '更新必须创建新版运行环境')
  port = await freePort()
  url = `http://127.0.0.1:${port}`
  running = launch(tarball, ['start', '--no-open', '--provider', 'offline', '--port', String(port), ...homeArgs])
  await ready(url, running)
  assert.ok((await request(url, '/api/v1/sessions')).some(session => session.id === firstSession))
  const events = await request(url, `/api/v1/sessions/${firstSession}/events`)
  assert.deepEqual(events, firstEvents)
  const report = { ok: true, tarball, previous, scratch, firstSession, eventCount: events.length, runtimesBefore: firstRuntimes, runtimesAfter: updatedRuntimes }
  await writeFile(join(scratch, 'report.json'), JSON.stringify(report, null, 2))
  console.log(`[package-smoke] 通过：真实安装/启动/工具执行/更新/重启，${events.length} 条会话记录保持一致。`)
} catch (error) {
  console.error(logs.join('').slice(-18000))
  throw error
} finally {
  await stop(running)
  await writeFile(join(scratch, 'smoke.log'), logs.join(''))
}
