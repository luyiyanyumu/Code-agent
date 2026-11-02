import { spawn } from 'node:child_process'
import { existsSync, openSync, closeSync } from 'node:fs'
import { mkdir, open, readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import net from 'node:net'
import { ensureRuntime, runtimePaths } from './cli.mjs'
import { bundledDataHome, loadBundledRuntime } from './bundled-runtime.mjs'

const moduleRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const delay = ms => new Promise(done => setTimeout(done, ms))

export function redactLog(value) {
  return String(value)
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\b(Bearer\s+)[\w.+/=-]+/gi, '$1[已隐藏]')
    .replace(/\bsk-[a-zA-Z0-9_-]{12,}/g, '[已隐藏密钥]')
    .replace(/("(?:api[_ .-]?key|access[_ .-]?token|authorization|password|secret)"\s*:\s*")[^"\r\n]*(")/gi, '$1[已隐藏]$2')
    .replace(/((?:api[_ .-]?key|access[_ .-]?token|authorization|password|secret)\s*[=:]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)/gi, '$1[已隐藏]')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/g, '$1[已隐藏]@')
}

/** Read a bounded tail, never the entire (potentially multi-GB) service log. */
export async function tailLog(path, lines = 300) {
  const count = Math.min(1000, Math.max(1, Number(lines) || 300))
  let handle
  try {
    handle = await open(path, 'r')
    const { size } = await handle.stat()
    const length = Math.min(size, 256 * 1024)
    const buffer = Buffer.alloc(length)
    await handle.read(buffer, 0, length, size - length)
    let text = buffer.toString('utf8')
    if (size > length) text = text.slice(text.indexOf('\n') + 1)
    const rows = text.split(/\r?\n/)
    if (rows.at(-1) === '') rows.pop()
    return { path, text: redactLog(rows.slice(-count).join('\n')) }
  } catch (error) {
    if (error.code === 'ENOENT') return { path, text: '' }
    throw error
  } finally { await handle?.close() }
}

export function execute(command, args, options = {}) {
  return new Promise((done, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd, env: options.env || process.env, shell: false,
      windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = '', stderr = ''
    const collect = key => data => {
      const value = data.toString('utf8')
      if (key === 'stdout') stdout = (stdout + value).slice(-262144)
      else stderr = (stderr + value).slice(-262144)
      options.onOutput?.(redactLog(value))
    }
    child.stdout.on('data', collect('stdout'))
    child.stderr.on('data', collect('stderr'))
    const timer = setTimeout(() => { child.kill(); reject(new Error('操作超时，请查看日志后重试。')) }, options.timeout || 120000)
    child.once('error', error => { clearTimeout(timer); reject(error) })
    child.once('close', code => { clearTimeout(timer); done({ code: code ?? 1, stdout, stderr }) })
  })
}

export async function portOccupied(port) {
  return new Promise(done => {
    const socket = net.connect({ host: '127.0.0.1', port })
    const finish = value => { socket.destroy(); done(value) }
    socket.setTimeout(500, () => finish(false))
    socket.once('connect', () => finish(true))
    socket.once('error', () => finish(false))
  })
}

export class LauncherServices {
  constructor({ root = moduleRoot, home, mode = 'source', backendPort = 8765, frontendPort = 5175,
    run = execute, platform = process.platform, env = process.env, log = () => {}, spawnProcess = spawn, logDir } = {}) {
    this.root = resolve(root)
    this.mode = mode
    this.home = mode === 'bundled' ? bundledDataHome({ home, env }) : resolve(home || env.CODE_AGENT_HOME || join(homedir(), '.code-agent'))
    this.env = { ...env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' }
    this.platform = platform
    this.run = run
    this.log = log
    this.spawnProcess = spawnProcess
    this.logDir = logDir || (mode === 'source' ? join(this.root, 'logs') : join(this.home, 'logs'))
    this.services = [
      { id: 'backend', name: '后端服务', port: backendPort, url: `http://127.0.0.1:${backendPort}`, logKey: 'backend' },
      { id: 'frontend', name: '前端工作台', port: frontendPort, url: `http://127.0.0.1:${frontendPort}/`, logKey: 'frontend' },
    ]
    this.children = new Map()
    this.states = new Map()
    this.identityCache = null
    this.identityVersion = 0
    this.identityFlight = null
    this.identitySequence = 0
  }

  async initialize() {
    await mkdir(this.logDir, { recursive: true })
    const release = JSON.parse(await readFile(join(this.root, 'npm', 'release.json'), 'utf8'))
    if (this.mode === 'bundled') {
      this.bundledRuntime = await loadBundledRuntime(this.root)
      this.python = this.bundledRuntime.python
      this.env.CODE_AGENT_HOME = this.home
      this.env.CODE_AGENT_NODE = this.bundledRuntime.node
      this.env.CODE_AGENT_HARNESS_NODE_DIR = join(this.root, 'code_agent', 'harness', 'node')
      this.env.CODE_AGENT_WEB_DIST = join(this.root, 'web', 'dist')
      this.env.PYTHONDONTWRITEBYTECODE = '1'
      const inheritedPath = Object.entries(this.env).find(([name]) => name.toUpperCase() === 'PATH')?.[1]
      for (const name of Object.keys(this.env)) if (name.toUpperCase() === 'PATH') delete this.env[name]
      this.env.PATH = [dirname(this.bundledRuntime.node), dirname(this.python), this.bundledRuntime.git && dirname(this.bundledRuntime.git), inheritedPath].filter(Boolean).join(this.platform === 'win32' ? ';' : ':')
      if (!this.env.CODE_AGENT_ENV_FILE && existsSync(join(this.home, '.env'))) this.env.CODE_AGENT_ENV_FILE = join(this.home, '.env')
    } else {
      this.python = this.mode === 'source'
        ? join(this.root, '.venv', this.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
        : runtimePaths(this.home, release, this.platform).python
    }
    this.release = release
  }

  async identities(fresh = false) {
    if (!fresh && this.identityCache && Date.now() - this.identityCache.at < 10000) return this.identityCache.value
    const version = this.identityVersion
    // Read-only status consumers may share a live snapshot. A fresh inspection
    // for stop/exit must still start after that operation requests it.
    if (!fresh && this.identityFlight?.version === version) return this.identityFlight.promise
    const sequence = ++this.identitySequence
    const promise = (async () => {
      let value = {}
      if (this.platform === 'win32') {
        const result = await this.windowsProcess('Inspect')
        if (result.code !== 0) throw new Error('无法读取服务进程身份；停止操作已禁用。')
        value = JSON.parse(result.stdout.replace(/^\uFEFF/, '').trim())
      } else {
        // Only children started by this launcher can be managed on other platforms.
        for (const service of this.services) {
          const child = this.children.get(service.id)
          let alive = false
          if (child) { try { process.kill(child.pid, 0); alive = true } catch {} }
          value[service.id] = { pid: alive ? child.pid : null, owned: alive, created: alive ? child.created : null }
        }
      }
      if (version === this.identityVersion && sequence === this.identitySequence) this.identityCache = { at: Date.now(), value }
      return value
    })()
    const flight = { version, promise }
    this.identityFlight = flight
    try { return await promise }
    finally { if (this.identityFlight === flight) this.identityFlight = null }
  }

  invalidateIdentity() { this.identityVersion++; this.identityCache = null }

  async windowsProcess(action, id, identity) {
    const helper = join(this.root, 'scripts', 'launcher_process.py')
    // The stdlib helper reads Windows TCP/process tables directly. Older npm
    // payloads can still use their PowerShell helper; an error from the new
    // helper must never retry through another process-stopping path.
    const direct = existsSync(helper)
    const command = direct ? this.python : 'powershell.exe'
    const args = direct ? ['-I', '-S', '-B', '-X', 'utf8', helper]
      : ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', join(this.root, 'npm', 'launcher-process.ps1')]
    args.push('-Action', action, '-Root', this.root, '-BackendPython', this.python,
      '-BackendPort', String(this.services[0].port), '-FrontendPort', String(this.services[1].port))
    if (action === 'Stop') args.push('-Service', id, '-ExpectedPid', String(identity.pid), '-ExpectedCreated', String(identity.created))
    return this.run(command, args, { cwd: this.root, env: this.env, timeout: 15000, capture: true })
  }

  async healthResponse(url) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const response = await fetch(url, { redirect: 'manual', signal: AbortSignal.timeout(3500) })
        return { response, text: await response.text() }
      } catch (error) {
        if (attempt === 1) throw error
        // A reused local socket may close across a restart/idle timeout.
        // Retry this read once; HTTP failures themselves remain failures.
        await delay(75)
      }
    }
  }

  async probe(service) {
    const url = service.id === 'backend' ? `${service.url}/healthz` : `${service.url}__code_agent_health`
    let { response, text } = await this.healthResponse(url)
    if (service.id === 'frontend' && response.status === 404) {
      const fallback = await this.healthResponse(service.url)
      response = fallback.response
      text = fallback.text
    }
    let data = {}
    try { data = JSON.parse(text) } catch {}
    const healthy = response.ok && (service.id === 'backend'
      ? data.ok === true && typeof data.version === 'string'
      : data.ok === true && data.service === 'code-agent-frontend' || /<!doctype html|<html/i.test(text) && /code.agent/i.test(text))
    return { healthy, detail: healthy ? '服务响应正常' : `健康检查未通过（HTTP ${response.status}）` }
  }

  async status() {
    const version = this.identityVersion
    let identities = {}, inspectionError = ''
    try { identities = await this.identities() } catch (error) { inspectionError = error.message }
    const services = await Promise.all(this.services.map(async service => {
      const identity = identities[service.id] || {}
      const started = Date.now()
      let healthy = false, detail = ''
      try {
        const probe = await this.probe(service)
        healthy = probe.healthy
        detail = probe.detail
      } catch { detail = '尚未启动或暂时无法连接' }
      const occupied = Boolean(identity.pid) || healthy || await portOccupied(service.port)
      let state = !occupied ? 'stopped' : identity.owned ? (healthy ? 'healthy' : 'unhealthy') : 'foreign'
      if (occupied && !identity.owned) detail = inspectionError || '端口上的进程不属于此项目，无法通过本启动器停止'
      if (this.states.has(service.id)) { state = this.states.get(service.id); detail = state === 'starting' ? '正在启动，等待健康检查' : '正在停止服务' }
      return { ...service, healthUrl: service.id === 'backend' ? `${service.url}/healthz` : `${service.url}__code_agent_health`,
        state, pid: identity.pid || null, latencyMs: healthy ? Date.now() - started : null, detail }
    }))
    // A slow Windows snapshot started before a lifecycle action must not
    // overwrite the newly started/stopped service with a stale foreign state.
    return version !== this.identityVersion ? this.status() : services
  }

  async start(id) {
    const service = this.services.find(item => item.id === id)
    const current = (await this.status()).find(item => item.id === id)
    if (current.state === 'healthy') return
    if (current.state !== 'stopped') throw new Error(`${service.name}端口已占用，请先查看状态和日志。`)
    this.states.set(id, 'starting')
    try {
      let command, args, cwd = this.root, env = { ...this.env }
      if (id === 'backend') {
        if (this.mode === 'installed') {
          const runtime = await ensureRuntime({ root: this.root, home: this.home, release: this.release, env,
            platform: this.platform, run: this.run, log: this.log })
          this.python = runtime.python
          env.CODE_AGENT_HOME = this.home
          env.CODE_AGENT_WEB_DIST = join(this.root, 'web', 'dist')
          env.CODE_AGENT_HARNESS_NODE_DIR = join(this.root, 'code_agent', 'harness', 'node')
          env.CODE_AGENT_NODE = process.execPath
          if (!env.CODE_AGENT_ENV_FILE && existsSync(join(this.home, '.env'))) env.CODE_AGENT_ENV_FILE = join(this.home, '.env')
        }
        if (!existsSync(this.python)) throw new Error(this.mode === 'bundled'
          ? '安装包的 Python 运行组件缺失，请重新运行安装程序进行修复。'
          : '未找到 Python 虚拟环境，请先按 README 安装项目依赖。')
        command = this.python
        args = [...(this.mode === 'bundled' ? ['-I', '-B', '-X', 'utf8'] : this.mode === 'installed' ? ['-I', '-X', 'utf8'] : []), '-m', 'code_agent.server', '--host', '127.0.0.1', '--port', String(service.port)]
      } else {
        command = this.mode === 'bundled' ? this.bundledRuntime.node : process.execPath
        if (this.mode === 'source') {
          const vite = join(this.root, 'web', 'node_modules', 'vite', 'bin', 'vite.js')
          if (!existsSync(vite)) throw new Error('未找到前端依赖，请先执行 npm ci --prefix web。')
          cwd = join(this.root, 'web')
          args = [vite, '--host', '127.0.0.1', '--port', String(service.port), '--strictPort']
          env.CODE_AGENT_API_TARGET = this.services[0].url
        } else args = [join(this.root, 'npm', 'launcher-preview.mjs'), '--port', String(service.port), '--backend-port', String(this.services[0].port)]
      }
      // Recheck directly before creating a child, so a foreign listener is never replaced.
      if (await portOccupied(service.port)) throw new Error(`端口 ${service.port} 刚被占用，已取消启动。`)
      const output = openSync(join(this.logDir, `${id}.log`), 'a')
      let child
      try {
        child = this.spawnProcess(command, args, { cwd, env, detached: true, windowsHide: true, shell: false, stdio: ['ignore', output, output] })
        await new Promise((done, reject) => { child.once('spawn', done); child.once('error', reject) })
      } finally { closeSync(output) }
      this.children.set(id, { pid: child.pid, created: Date.now(), child })
      child.unref()
      this.log(`${service.name}已创建进程 PID ${child.pid}`)
      for (let attempt = 0; attempt < 100; attempt++) {
        if (child.exitCode !== null) throw new Error(`${service.name}启动失败（退出码 ${child.exitCode}），请查看日志。`)
        try {
          if ((await this.probe(service)).healthy) {
            this.invalidateIdentity()
            return
          }
        } catch {}
        await delay(500)
      }
      throw new Error(`${service.name}已启动进程，但未在时限内就绪，请查看日志。`)
    } finally { this.states.delete(id); this.invalidateIdentity() }
  }

  async assertIdle() {
    const service = this.services[0]
    const result = await this.run(this.python, [...(this.mode === 'bundled' ? ['-I', '-B', '-X', 'utf8'] : []), join(this.root, 'scripts', 'backend_idle_check.py'), '--base-url', service.url,
      ...(this.mode !== 'source' ? ['--db', join(this.home, 'code_agent.db')] : [])],
    { cwd: this.root, env: this.env, timeout: 90000, capture: true })
    if (result.code === 12) throw new Error('后端仍有任务、问题或审批等待处理，暂不能停止；完成当前任务后重试。')
    if (result.code !== 0) throw new Error('无法确认后端任务已空闲，已保留服务。请先查看健康状态和日志。')
  }

  async stop(id) {
    const service = this.services.find(item => item.id === id)
    let identity = (await this.identities(true))[id]
    if (!identity?.pid) {
      if (await portOccupied(service.port)) throw new Error('无法识别端口上的进程，已保留服务。')
      return
    }
    if (!identity.owned) throw new Error('该端口属于其他项目，已拒绝停止。')
    if (id === 'backend') { await this.assertIdle(); await this.assertIdle() }
    this.states.set(id, 'stopping')
    try {
      if (this.platform === 'win32') {
        const result = await this.windowsProcess('Stop', id, identity)
        if (result.code !== 0) throw new Error('服务进程已变化或不能安全停止，已取消操作。')
      } else {
        const owned = this.children.get(id)
        if (!owned || owned.pid !== identity.pid) throw new Error('无法确认进程身份，已取消停止。')
        process.kill(identity.pid, 'SIGTERM')
      }
      this.invalidateIdentity()
      for (let attempt = 0; attempt < 40; attempt++) {
        if (!await portOccupied(service.port)) { this.children.delete(id); this.log(`${service.name}已停止`); return }
        await delay(250)
      }
      throw new Error('服务端口尚未关闭，已取消后续启动。')
    } finally { this.states.delete(id); this.invalidateIdentity() }
  }

  async action(action, target) {
    const ids = target === 'all' ? ['backend', 'frontend'] : [target]
    if (!ids.every(id => this.services.some(service => service.id === id))) throw new Error('未知服务。')
    if (!['start', 'stop', 'restart'].includes(action)) throw new Error('未知服务操作。')
    if (action === 'start' && target === 'all') {
      // Both listeners can become healthy independently. Wait for both branches
      // so ownership capture also sees a successful sibling after one fails.
      const results = await Promise.allSettled(ids.map(id => this.start(id)))
      const failed = results.find(result => result.status === 'rejected')
      if (failed) throw failed.reason
      return
    }
    // Backend goes first: a busy backend leaves both services running for all-service actions.
    for (const id of ids) {
      if (action !== 'start') await this.stop(id)
      if (action !== 'stop') await this.start(id)
    }
  }
}
