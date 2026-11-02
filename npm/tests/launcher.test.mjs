import assert from 'node:assert/strict'
import test from 'node:test'
import http from 'node:http'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createLauncher, launchControlPanel } from '../launcher.mjs'
import { main, parseArgs } from '../cli.mjs'

async function fixture(t, overrides = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'code-agent launcher '))
  const root = join(directory, 'package with spaces')
  await mkdir(join(root, 'npm', 'launcher-ui'), { recursive: true })
  await mkdir(join(root, 'logs'), { recursive: true })
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: '0.10.2' }))
  await writeFile(join(root, 'npm', 'launcher-ui', 'index.html'), '<html><meta name="launcher-token" content="__LAUNCHER_TOKEN__"></html>')
  await writeFile(join(root, 'npm', 'launcher-ui', 'app.js'), 'console.log("launcher")')
  await writeFile(join(root, 'npm', 'launcher-ui', 'style.css'), 'body{}')
  const calls = []
  const services = [
    { id: 'backend', state: 'healthy', url: 'http://127.0.0.1:8765' },
    { id: 'frontend', state: 'healthy', url: 'http://127.0.0.1:5175/' },
  ]
  const manager = { logDir: join(root, 'logs'), services, env: {}, initialize: async () => {},
    status: async () => structuredClone(services), action: async (...args) => { calls.push(args) }, ...overrides }
  const launcher = await createLauncher({ root, mode: 'source', port: 0, manager, open: async url => { calls.push(['open', url]) } })
  t.after(async () => { await launcher.close(); await rm(directory, { recursive: true, force: true }) })
  const html = await (await fetch(launcher.url)).text()
  const token = /content="([a-f0-9]{64})"/.exec(html)[1]
  const post = (body, headers = {}) => fetch(`${launcher.url}/api/action`, { method: 'POST', headers: {
    'Content-Type': 'application/json', 'X-Launcher-Token': token, ...headers,
  }, body: JSON.stringify(body) })
  const finished = async () => {
    for (let index = 0; index < 50; index++) {
      const status = await (await fetch(`${launcher.url}/api/status`)).json()
      if (status.operation?.state !== 'running') return status.operation
      await new Promise(done => setTimeout(done, 10))
    }
    throw new Error('operation did not finish')
  }
  return { root, manager, services, calls, launcher, html, token, post, finished }
}

test('standalone page embeds per-process credential, CSP and real status', async t => {
  const { launcher, html } = await fixture(t)
  assert.ok(!html.includes('__LAUNCHER_TOKEN__'))
  const response = await fetch(launcher.url)
  assert.match(response.headers.get('content-security-policy'), /frame-ancestors 'none'/)
  const status = await (await fetch(`${launcher.url}/api/status`)).json()
  assert.equal(status.services[0].state, 'healthy')
  assert.equal(status.version, '0.10.2')
  assert.equal((await fetch(`${launcher.url}/app.js`)).status, 200)
  assert.equal((await fetch(`${launcher.url}/missing`)).status, 404)
})

test('mutations reject missing tokens, hostile origins, malformed JSON and invalid targets', async t => {
  const { post, launcher, token, calls } = await fixture(t)
  assert.equal((await post({ action: 'stop', target: 'all' }, { 'X-Launcher-Token': '' })).status, 403)
  assert.equal((await post({ action: 'stop', target: 'all' }, { Origin: 'https://untrusted.invalid' })).status, 403)
  assert.equal((await post({ action: 'shell', target: 'all' })).status, 400)
  assert.equal((await post({ action: 'stop', target: '../../' })).status, 400)
  assert.equal((await fetch(`${launcher.url}/api/action`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Launcher-Token': token }, body: '{' })).status, 400)
  const hostileHost = await new Promise((done, reject) => {
    const request = http.get(`${launcher.url}/api/status`, { headers: { Host: 'malicious.invalid' } }, response => { response.resume(); done(response.statusCode) })
    request.on('error', reject)
  })
  assert.equal(hostileHost, 403)
  assert.equal(calls.length, 0)
})

test('service actions serialize and expose terminal outcomes without blocking health requests', async t => {
  let release
  const blocked = new Promise(done => { release = done })
  const { post, launcher, finished } = await fixture(t, { action: async () => blocked })
  const accepted = await post({ action: 'restart', target: 'backend' })
  assert.equal(accepted.status, 202)
  assert.equal((await accepted.json()).state, 'running')
  assert.equal((await post({ action: 'stop', target: 'frontend' })).status, 409)
  assert.equal((await (await fetch(`${launcher.url}/api/status`)).json()).operation.state, 'running')
  release()
  const terminal = await finished()
  assert.equal(terminal.state, 'succeeded')
  assert.match(terminal.message, /后端已重启/)
})

test('busy guard failure stays visible and does not claim success', async t => {
  const { post, finished, launcher } = await fixture(t, { action: async () => { throw new Error('任务运行中，暂不能停止') } })
  await post({ action: 'stop', target: 'all' })
  const terminal = await finished()
  assert.equal(terminal.state, 'failed')
  assert.match(terminal.message, /任务运行中/)
  const log = await (await fetch(`${launcher.url}/api/logs?service=launcher`)).json()
  assert.match(log.text, /任务运行中/)
})

test('logs whitelist paths, handle absent files, and never treat text as markup', async t => {
  const { root, launcher } = await fixture(t)
  assert.equal((await fetch(`${launcher.url}/api/logs?service=..%2F.env`)).status, 400)
  const empty = await (await fetch(`${launcher.url}/api/logs?service=backend`)).json()
  assert.equal(empty.text, '')
  await writeFile(join(root, 'logs', 'backend.log'), '<script>alert(1)</script>\nAPI_KEY=secret-value\n')
  const log = await (await fetch(`${launcher.url}/api/logs?service=backend`)).json()
  assert.match(log.text, /<script>/)
  assert.ok(!log.text.includes('secret-value'))
})

test('source update refuses while either service runs; open uses the correct destination', async t => {
  const { post, finished, calls } = await fixture(t)
  await post({ action: 'update', target: 'all' })
  assert.match((await finished()).message, /先停止前后端/)
  await post({ action: 'open', target: 'workbench' })
  assert.equal((await finished()).state, 'succeeded')
  assert.deepEqual(calls.at(-1), ['open', 'http://127.0.0.1:5175/'])
})

test('same-directory launcher is reused rather than spawning or disturbing services', async t => {
  const { root, launcher, calls } = await fixture(t)
  const result = await launchControlPanel({ root, launcherPort: new URL(launcher.url).port, openBrowser: async url => calls.push(['browser', url]) })
  assert.equal(result.reused, true)
  assert.equal(result.url, launcher.url)
  assert.deepEqual(calls, [['browser', launcher.url]])
})

test('CLI launcher branch does not install or start backend and preserves explicit ports', async t => {
  const { root } = await fixture(t)
  let selected
  const code = await main(['launcher', '--no-open', '--port', '18765', '--launcher-port', '19388'], {
    root, log: () => {}, run: () => { throw new Error('backend install must not run') },
    launchControlPanel: async options => { selected = options; return { url: 'http://127.0.0.1:19388' } },
  })
  assert.equal(code, 0)
  assert.equal(selected.launcherPort, 19388)
  assert.equal(selected.backendPort, 18765)
  assert.equal(selected.open, false)
  assert.throws(() => parseArgs(['launcher', '--launcher-port', 'bad']), /端口|整数/)
})
