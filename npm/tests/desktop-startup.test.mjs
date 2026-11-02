import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { prepareBundledServices } from '../desktop-startup.mjs'

async function fixture(t) {
  const home = await mkdtemp(join(tmpdir(), 'code-agent bundled ports '))
  t.after(() => rm(home, { recursive: true, force: true }))
  return { home, env: {}, services: [{ id: 'backend', port: 18765 }, { id: 'frontend', port: 15185 }],
    invalidateIdentity() {}, async status() { return this.services.map(service => ({ ...service, state: 'healthy' })) } }
}

test('reopening an installed app reuses remembered service ports', async t => {
  const manager = await fixture(t)
  await writeFile(join(manager.home, 'desktop-services.json'), JSON.stringify({ backend: 23451, frontend: 23452 }))
  await prepareBundledServices(manager)
  assert.deepEqual(manager.services.map(service => service.port), [23451, 23452])
  assert.equal(manager.services[0].url, 'http://127.0.0.1:23451')
  assert.equal(manager.services[1].url, 'http://127.0.0.1:23452/')
})

test('foreign ports are replaced and persisted without stopping any other project', async t => {
  const manager = await fixture(t)
  manager.status = async () => manager.services.map(service => ({ ...service, state: 'foreign' }))
  await prepareBundledServices(manager)
  assert.notEqual(manager.services[0].port, 18765)
  assert.notEqual(manager.services[1].port, 15185)
  assert.notEqual(manager.services[0].port, manager.services[1].port)
  assert.deepEqual(JSON.parse(await readFile(join(manager.home, 'desktop-services.json'), 'utf8')),
    Object.fromEntries(manager.services.map(service => [service.id, service.port])))
})

test('explicit isolated ports are validated and take precedence over remembered ports', async t => {
  const manager = await fixture(t)
  manager.env.CODE_AGENT_LAUNCHER_BACKEND_PORT = '24001'
  manager.env.CODE_AGENT_LAUNCHER_FRONTEND_PORT = '24002'
  await prepareBundledServices(manager)
  assert.deepEqual(manager.services.map(service => service.port), [24001, 24002])
  manager.env.CODE_AGENT_LAUNCHER_BACKEND_PORT = '0'
  await assert.rejects(prepareBundledServices(manager), /有效/)
  manager.env.CODE_AGENT_LAUNCHER_BACKEND_PORT = '24002'
  await assert.rejects(prepareBundledServices(manager), /相同/)
})
