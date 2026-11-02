import net from 'node:net'
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const validPort = value => Number.isInteger(value) && value >= 1024 && value <= 65535

async function unusedPort(excluded) {
  for (let attempt = 0; attempt < 10; attempt++) {
    const port = await new Promise((done, reject) => {
      const server = net.createServer()
      server.once('error', reject)
      server.listen(0, '127.0.0.1', () => {
        const value = server.address().port
        server.close(error => error ? reject(error) : done(value))
      })
    })
    if (!excluded.has(port)) return port
  }
  throw new Error('无法为应用选择空闲端口，请稍后重试。')
}

/** Remember relocated service ports so closing/reopening cannot spawn duplicates. */
export async function prepareBundledServices(manager) {
  const file = join(manager.home, 'desktop-services.json')
  let remembered = {}
  try { remembered = JSON.parse(await readFile(file, 'utf8')) } catch {}
  for (const service of manager.services) {
    const variable = `CODE_AGENT_LAUNCHER_${service.id.toUpperCase()}_PORT`
    const explicit = manager.env?.[variable]
    let port = service.port
    if (explicit !== undefined) {
      if (!/^\d+$/.test(explicit) || !validPort(Number(explicit))) throw new Error(`${variable} 必须是有效的本机端口。`)
      port = Number(explicit)
    } else if (validPort(remembered[service.id])) port = remembered[service.id]
    service.port = port
    service.url = `http://127.0.0.1:${port}${service.id === 'frontend' ? '/' : ''}`
  }
  if (manager.services[0].port === manager.services[1].port) throw new Error('前后端不能使用相同的端口。')
  manager.invalidateIdentity()
  const statuses = await manager.status()
  const ports = new Set(manager.services.map(service => service.port))
  for (const service of manager.services) {
    if (statuses.find(item => item.id === service.id)?.state === 'foreign') {
      ports.delete(service.port)
      service.port = await unusedPort(ports)
      ports.add(service.port)
      service.url = `http://127.0.0.1:${service.port}${service.id === 'frontend' ? '/' : ''}`
    }
  }
  manager.invalidateIdentity()
  await mkdir(manager.home, { recursive: true })
  const temporary = `${file}.${process.pid}.tmp`
  await writeFile(temporary, JSON.stringify(Object.fromEntries(manager.services.map(service => [service.id, service.port]))) + '\n')
  await rename(temporary, file)
}
