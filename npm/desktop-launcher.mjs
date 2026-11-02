import { randomBytes } from 'node:crypto'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, writeFile, rename, readFile, rm } from 'node:fs/promises'
import { dirname, resolve, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createLauncher, openLocal } from './launcher.mjs'
import { prepareBundledServices } from './desktop-startup.mjs'
import { tailLog, redactLog } from './launcher-service.mjs'

export async function launchDesktopWindow({ root = resolve(dirname(fileURLToPath(import.meta.url)), '..'), home,
  env = process.env, spawnProcess = spawn } = {}) {
  root = resolve(root)
  const executable = resolve(root, existsSync(resolve(root, 'Code-agent.exe')) ? 'Code-agent.exe' : 'Code-agent Launcher.exe')
  if (!existsSync(executable)) throw new Error('未找到桌面程序，请先执行 npm run build:launcher。')
  const child = spawnProcess(executable, ['--root', root], {
    cwd: root, env: { ...env, CODE_AGENT_NODE: process.execPath, ...(home ? { CODE_AGENT_HOME: resolve(home) } : {}) },
    detached: true, windowsHide: false, shell: false, stdio: 'ignore',
  })
  await new Promise((done, reject) => { child.once('spawn', done); child.once('error', reject) })
  child.unref()
  return { native: true, pid: child.pid, executable }
}

export function parseDesktopArgs(args) {
  const options = {}
  const names = { '--root': 'root', '--handshake-file': 'handshakeFile', '--parent-pid': 'parentPid', '--nonce': 'nonce' }
  for (let index = 0; index < args.length; index++) {
    const name = names[args[index]], value = args[++index]
    if (!name || !value || value.startsWith('--') || options[name] !== undefined) throw new Error('桌面启动参数无效。')
    options[name] = value
  }
  if (!options.root || !options.handshakeFile || !/^\d+$/.test(options.parentPid || '') || Number(options.parentPid) < 1
    || !/^[a-f0-9-]{32,64}$/i.test(options.nonce || '')) throw new Error('桌面启动参数不完整。')
  options.root = resolve(options.root)
  options.handshakeFile = resolve(options.handshakeFile)
  options.parentPid = Number(options.parentPid)
  if (!Number.isSafeInteger(options.parentPid)) throw new Error('桌面窗口进程编号无效。')
  return options
}

/** The controller belongs only to its native host; service children stay detached. */
export async function createDesktopService(options) {
  const root = resolve(options.root)
  const nonce = options.nonce || randomBytes(24).toString('hex')
  if (!/^[a-f0-9-]{32,64}$/i.test(nonce)) throw new Error('桌面窗口凭据无效。')
  const events = []
  const managed = new Map()
  let desktopAction = false
  let launcher, interval, closeRequested = false, closing = false
  let watchdogPending = false
  let resolveClosed
  const closed = new Promise(done => { resolveClosed = done })
  const parentAlive = options.parentAlive || (() => {
    try { process.kill(options.parentPid, 0); return true } catch (error) { return error.code === 'EPERM' }
  })
  const authorized = request => request.headers['x-desktop-token'] === nonce

  async function identities() {
    if (typeof launcher.manager.identities === 'function') return launcher.manager.identities(true)
    return Object.fromEntries((await launcher.manager.status()).map(service => [service.id, { pid: service.pid, created: service.created, owned: service.state !== 'foreign' }]))
  }

  async function runAction(action, target) {
    if (desktopAction) throw Object.assign(new Error('已有应用操作正在进行，请稍候。'), { code: 409 })
    desktopAction = true
    let before = {}
    let actionStarted = false
    const tracksOwnership = action === 'start' || action === 'restart'
    try {
      if (tracksOwnership || action === 'update') before = await identities()
      const accepted = launcher.begin(action, target)
      if (accepted.error) throw Object.assign(new Error(accepted.error), { code: accepted.code })
      actionStarted = true
      const deadline = Date.now() + 180000
      while (accepted.operation.state === 'running') {
        if (Date.now() > deadline) throw new Error('应用操作尚未完成，请在应用状态中查看进度。')
        await new Promise(done => setTimeout(done, 75))
      }
      if (accepted.operation.state !== 'succeeded') throw new Error(accepted.operation.message)
    } finally {
      // Include a successfully started backend even if starting the frontend
      // failed later. Exact identity is checked again before normal app exit.
      try { if (actionStarted && tracksOwnership) {
        const after = await identities()
        for (const id of target === 'all' ? ['backend', 'frontend'] : [target]) {
          const next = after[id], prior = before[id]
          if (next?.owned && next.pid && (next.pid !== prior?.pid || next.created !== prior?.created)) managed.set(id, { ...next })
        }
      } } finally { desktopAction = false }
    }
    // Ownership capture above already refreshed the identity cache. Returning
    // status afterwards avoids a second adjacent OS snapshot and reports ready.
    return desktopStatus()
  }

  async function desktopStatus() {
    const state = await launcher.status()
    return { ...state, desktop: { ready: !desktopAction, managedServices: [...managed.keys()], reusedServices: state.services.filter(service => service.pid && !managed.has(service.id)).map(service => service.id) } }
  }

  async function exitApplication() {
    if (desktopAction || launcher.getOperation()?.state === 'running') throw Object.assign(new Error('应用操作正在进行，请完成后再退出。'), { code: 409 })
    desktopAction = true
    try {
      const current = await identities()
      const stopped = [], preserved = []
      for (const id of ['backend', 'frontend']) {
        const owned = managed.get(id), actual = current[id]
        if (!owned || !actual?.owned || actual.pid !== owned.pid || actual.created !== owned.created) { if (actual?.pid) preserved.push(id); continue }
        await launcher.manager.action('stop', id)
        managed.delete(id); stopped.push(id)
      }
      return { exiting: true, stopped, preserved }
    } finally { desktopAction = false }
  }

  async function rpc(request) {
    let text = ''
    for await (const chunk of request) { text += chunk.toString('utf8'); if (Buffer.byteLength(text) > 16384) throw new Error('应用请求过大。') }
    const { method, params = {} } = JSON.parse(text)
    if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('应用参数无效。')
    if (method === 'status') return desktopStatus()
    if (method === 'exit') return exitApplication()
    if (method === 'logs') {
      if (!['backend', 'frontend', 'launcher', 'update'].includes(params.service)) throw new Error('未知日志类型。')
      return tailLog(join(launcher.manager.logDir, `${params.service}.log`), params.lines)
    }
    if (method === 'action') {
      const allowed = { start: ['all', 'backend', 'frontend'], restart: ['all', 'backend', 'frontend'], open: ['logs'], 'check-update': ['all'], update: ['all'] }
      if (!allowed[params.action]?.includes(params.target)) throw new Error('不支持的应用操作。')
      return runAction(params.action, params.target)
    }
    throw new Error('未知应用请求。')
  }

  async function finishClose() {
    if (closing || !launcher || desktopAction || launcher.getOperation()?.state === 'running') return
    closing = true
    clearInterval(interval)
    await launcher.close()
    if (options.handshakeFile) {
      // Never remove a handshake left by a replacement native window.
      try {
        const current = JSON.parse(await readFile(options.handshakeFile, 'utf8'))
        if (current.nonce === nonce && current.pid === process.pid) await rm(options.handshakeFile, { force: true })
      } catch {}
    }
    resolveClosed()
  }

  async function close() { closeRequested = true; await finishClose(); return closed }

  launcher = await createLauncher({
    ...options, root, port: 0, desktop: true,
    open: async destination => {
      if (!/^https?:\/\//i.test(destination)) return (options.openFolder || openLocal)(destination)
      const url = new URL(destination)
      const permitted = launcher.manager.services.some(service => new URL(service.url).origin === url.origin)
      if (!permitted || url.hostname !== '127.0.0.1' || url.username || url.password || url.protocol !== 'http:') throw new Error('此地址不能在工作台窗口中打开。')
      const event = { type: 'open-window', url: url.href }
      if (events.length >= 32) events.shift()
      events.push(event)
      options.onOpenWindow?.(url.href)
    },
    handleRequest: async ({ request, response, url, respond }) => {
      if (!url.pathname.startsWith('/api/desktop/')) return false
      if (!authorized(request)) { respond(response, 403, { error: '桌面窗口凭据无效。' }); return true }
      if (request.method === 'POST' && url.pathname === '/api/desktop/rpc') {
        try { respond(response, 200, await rpc(request)) }
        catch (error) { respond(response, error.code === 409 ? 409 : 400, { error: redactLog(error.message) }) }
        return true
      }
      if (request.method === 'GET' && url.pathname === '/api/desktop/events') {
        respond(response, 200, { events: events.splice(0) }); return true
      }
      if (request.method === 'POST' && url.pathname === '/api/desktop/close') {
        respond(response, 200, { closing: true })
        closeRequested = true
        setImmediate(() => { finishClose().catch(() => {}) })
        return true
      }
      respond(response, 404, { error: '桌面接口不存在。' }); return true
    },
  })
  const bundled = launcher.manager.mode === 'bundled'
  if (bundled) {
    try { await prepareBundledServices(launcher.manager) }
    catch (error) { await launcher.close(); throw error }
  }
  if (options.handshakeFile) {
    const handshake = { pid: process.pid, url: launcher.url, root, nonce, serviceUrls: launcher.manager.services.map(service => service.url), workspaceUrl: launcher.manager.services.find(service => service.id === 'frontend').url }
    await mkdir(dirname(options.handshakeFile), { recursive: true })
    const temporary = `${options.handshakeFile}.${process.pid}.tmp`
    try {
      await writeFile(temporary, JSON.stringify(handshake), { encoding: 'utf8', mode: 0o600, flag: 'wx' })
      await rename(temporary, options.handshakeFile)
    } catch (error) { await launcher.close(); await rm(temporary, { force: true }); throw error }
  }
  interval = setInterval(async () => {
    if (watchdogPending || closing) return
    watchdogPending = true
    try {
      if (!await parentAlive()) closeRequested = true
      if (closeRequested) await finishClose()
    } finally { watchdogPending = false }
  }, options.pollIntervalMs || 1000)
  if (options.autoStart !== false) runAction('start', 'all').catch(() => {})
  return { ...launcher, nonce, close, closed }
}
