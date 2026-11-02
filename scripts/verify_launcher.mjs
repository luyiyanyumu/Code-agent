/** Real, offline FastAPI + Vite lifecycle; separate ports/home from user's workbench. */
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createLauncher } from '../npm/launcher.mjs'
import { LauncherServices, portOccupied } from '../npm/launcher-service.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = resolve(root, '.tmp/launcher-verification/real-runtime')
await mkdir(output, { recursive: true })
const backendPort = 18765, frontendPort = 15185
assert.equal(await portOccupied(backendPort), false, 'isolated backend port must be free')
assert.equal(await portOccupied(frontendPort), false, 'isolated frontend port must be free')
const manager = new LauncherServices({ root, mode: 'source', backendPort, frontendPort,
  env: { ...process.env, CODE_AGENT_HOME: join(output, 'home'), CODE_AGENT_PROVIDER: 'offline' }, logDir: join(output, 'logs'), log: console.log })
const opened = []
const launcher = await createLauncher({ root, mode: 'source', manager, port: 0, open: async target => opened.push(target) })
const html = await (await fetch(launcher.url)).text()
const token = /name="launcher-token" content="([a-f0-9]+)"/.exec(html)[1]
const steps = []
async function action(action, target) {
  const started = Date.now()
  const response = await fetch(`${launcher.url}/api/action`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Launcher-Token': token }, body: JSON.stringify({ action, target }) })
  assert.equal(response.status, 202)
  for (let attempt = 0; attempt < 180; attempt++) {
    const data = await (await fetch(`${launcher.url}/api/status`)).json()
    if (data.operation?.state !== 'running') {
      steps.push({ action, target, state: data.operation.state, message: data.operation.message, elapsedMs: Date.now() - started, services: data.services.map(({id,state,pid}) => ({id,state,pid})) })
      console.log(JSON.stringify(steps.at(-1)))
      assert.equal(data.operation.state, 'succeeded', data.operation.message)
      return data
    }
    await new Promise(done => setTimeout(done, 500))
  }
  throw new Error(`${action} ${target} timed out`)
}
let passed = false
try {
  let data = await action('start', 'all')
  assert.ok(data.services.every(item => item.state === 'healthy'))
  const beforeBackend = data.services[0].pid, beforeFrontend = data.services[1].pid
  const health = await (await fetch(`http://127.0.0.1:${backendPort}/healthz`)).json()
  assert.equal(health.ok, true)
  const proxied = await (await fetch(`http://127.0.0.1:${frontendPort}/healthz`)).json()
  assert.equal(proxied.ok, true)
  await action('open', 'workbench')
  await action('open', 'logs')
  data = await action('restart', 'backend')
  assert.notEqual(data.services[0].pid, beforeBackend)
  assert.equal(data.services[1].pid, beforeFrontend, 'backend restart must preserve frontend')
  const backendAfter = data.services[0].pid
  data = await action('restart', 'frontend')
  assert.notEqual(data.services[1].pid, beforeFrontend)
  assert.equal(data.services[0].pid, backendAfter, 'frontend restart must preserve backend')
  data = await action('stop', 'all')
  assert.ok(data.services.every(item => item.state === 'stopped'))
  // The control panel stays available even after both controlled processes stop.
  assert.equal((await fetch(launcher.url)).status, 200)
  data = await action('start', 'all')
  assert.ok(data.services.every(item => item.state === 'healthy'))
  data = await action('stop', 'all')
  assert.ok(data.services.every(item => item.state === 'stopped'))
  const backendLog = await (await fetch(`${launcher.url}/api/logs?service=backend`)).json()
  assert.match(backendLog.text, /Application startup complete/)
  const frontendLog = await (await fetch(`${launcher.url}/api/logs?service=frontend`)).json()
  assert.match(frontendLog.text, /VITE/)
  assert.equal(opened.length, 2)
  passed = true
} finally {
  await writeFile(join(output, 'report.json'), JSON.stringify({ passed, steps, opened, externalModelCalls: 0 }, null, 2) + '\n')
  // Clean up only this verifier's source processes and only through the same guard.
  for (const id of ['backend', 'frontend']) {
    if (await portOccupied(id === 'backend' ? backendPort : frontendPort)) await manager.stop(id).catch(error => console.error(`cleanup ${id}: ${error.message}`))
  }
  await launcher.close()
}
