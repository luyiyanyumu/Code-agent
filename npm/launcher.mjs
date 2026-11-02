import http from 'node:http'
import { randomBytes } from 'node:crypto'
import { appendFile, readFile, mkdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { LauncherServices, execute, tailLog, redactLog } from './launcher-service.mjs'
import { detectMode, checkUpdate, applyUpdate } from './launcher-update.mjs'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const types = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8' }

export async function openLocal(target, run = execute, platform = process.platform) {
  const [command, args] = platform === 'win32'
    ? target.startsWith('http') ? ['rundll32.exe', ['url.dll,FileProtocolHandler', target]] : ['explorer.exe', [target]]
    : platform === 'darwin' ? ['open', [target]] : ['xdg-open', [target]]
  // Windows Explorer can return 1 after handing a folder to its existing process.
  const result = await run(command, args, { timeout: 15000, capture: true })
  if (result.code !== 0 && !(platform === 'win32' && !target.startsWith('http'))) throw new Error('无法自动打开，请复制显示的地址手动打开。')
}

export async function createLauncher(options = {}) {
  const root = resolve(options.root || packageRoot)
  const mode = options.mode || await detectMode(root)
  const version = JSON.parse(await readFile(join(root, 'package.json'), 'utf8')).version
  let logQueue = Promise.resolve()
  const manager = options.manager || new LauncherServices({ ...options, root, mode, log: value => log(value) })
  await manager.initialize()
  const logPaths = Object.fromEntries(['backend', 'frontend', 'launcher', 'update'].map(key => [key, join(manager.logDir, `${key}.log`)]))
  await mkdir(manager.logDir, { recursive: true })
  function log(value, key = 'launcher') {
    logQueue = logQueue.then(() => appendFile(logPaths[key], `[${new Date().toISOString()}] ${redactLog(value)}\n`, 'utf8')).catch(() => {})
  }
  const token = randomBytes(32).toString('hex')
  let operation = null, statusFlight = null, lastStatus = null, latest = { current: version, latest: null, available: false, checkedAt: null, error: null }
  let launcherUrl
  const allowedActions = new Map([
    ['start', ['all', 'backend', 'frontend']], ['stop', ['all', 'backend', 'frontend']], ['restart', ['all', 'backend', 'frontend']],
    ['open', ['workbench', 'backend', 'frontend', 'logs']], ['check-update', ['all']], ['update', ['all']],
  ])

  async function status() {
    if (!statusFlight) {
      statusFlight = manager.status().then(services => { lastStatus = services; return services }).finally(() => { statusFlight = null })
    }
    const services = await statusFlight
    return { root, version, mode, services, operation, update: latest, launcher: { url: launcherUrl } }
  }

  async function perform(action, target) {
    if (action === 'open') {
      const destination = target === 'logs' ? manager.logDir
        : target === 'backend' ? `${manager.services[0].url}/docs`
        : manager.services.find(service => service.id === (target === 'workbench' ? 'frontend' : target)).url
      if (target !== 'logs') {
        const services = lastStatus || await manager.status()
        const id = target === 'workbench' ? 'frontend' : target
        if (services.find(item => item.id === id)?.state !== 'healthy') throw new Error('服务尚未就绪，请先启动服务再打开。')
      }
      await (typeof options.open === 'function' ? options.open : options.openBrowser || openLocal)(destination)
      return target === 'logs' ? '已打开日志目录' : '已打开服务页面'
    }
    if (action === 'check-update') {
      latest = await (options.checkUpdate || checkUpdate)({ root, mode, env: manager.env, run: options.updateRun || execute })
      if (latest.error) throw new Error(latest.error)
      return latest.message || (latest.available ? `发现可用更新：${latest.latest || '源码上游提交'}` : '当前已是最新版本')
    }
    if (action === 'update') {
      const run = (command, args, opts) => (options.updateRun || execute)(command, args, {
        ...opts, timeout: 15 * 60 * 1000, onOutput: text => log(text, 'update'),
      })
      // No source dependency rebuild is allowed while its services are using the files.
      if (mode === 'source') {
        const services = await manager.status()
        if (services.some(item => item.state !== 'stopped')) throw new Error('源码更新需要先停止前后端。请先点击“全部停止”，再安装更新。')
      }
      const result = await (options.applyUpdate || applyUpdate)({ root, mode, run, env: manager.env,
        home: manager.home, python: manager.python, log: text => log(text, 'update') })
      log(result.message, 'update')
      latest = { ...latest, available: result.staged ? latest.available : false }
      return result.message
    }
    await manager.action(action, target)
    return `${target === 'all' ? '前后端' : target === 'backend' ? '后端' : '前端'}${{ start: '已启动', stop: '已停止', restart: '已重启' }[action]}`
  }

  function begin(action, target) {
    if (!allowedActions.get(action)?.includes(target)) return { error: '不支持的操作或目标。', code: 400 }
    if (operation?.state === 'running') return { error: '已有操作正在进行，请等待完成。', code: 409 }
    operation = { id: randomBytes(8).toString('hex'), action, target, state: 'running', message: '正在处理，请稍候…', startedAt: new Date().toISOString(), finishedAt: null }
    const selected = operation
    log(`开始 ${action} ${target}`)
    Promise.resolve().then(() => perform(action, target)).then(message => {
      Object.assign(selected, { state: 'succeeded', message })
    }, error => {
      Object.assign(selected, { state: 'failed', message: redactLog(error.message || '操作失败，请查看日志。') })
    }).finally(() => {
      selected.finishedAt = new Date().toISOString()
      log(`${selected.state} ${action} ${target}: ${selected.message}`)
    })
    return { operation: selected, code: 202 }
  }

  function respond(response, code, body, type = 'application/json; charset=utf-8') {
    response.writeHead(code, { 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
      'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" })
    response.end(type.startsWith('application/json') ? JSON.stringify(body) : body)
  }

  const server = http.createServer(async (request, response) => {
    try {
      // Host allowlisting also prevents a malicious DNS name rebinding to loopback.
      if (request.headers.host !== new URL(launcherUrl).host) return respond(response, 403, { error: '仅允许本机启动器访问。' })
      if (request.headers.origin && request.headers.origin !== launcherUrl) return respond(response, 403, { error: '请求来源不受信任。' })
      const url = new URL(request.url, launcherUrl)
      if (options.handleRequest && await options.handleRequest({ request, response, url, respond })) return
      if (request.method === 'GET') {
        if (url.pathname === '/api/identity') return respond(response, 200, { app: 'code-agent-launcher', root })
        if (url.pathname === '/api/status') return respond(response, 200, await status())
        if (url.pathname === '/api/logs') {
          const key = url.searchParams.get('service') || 'backend'
          if (!Object.hasOwn(logPaths, key)) return respond(response, 400, { error: '未知日志类型。' })
          await logQueue
          return respond(response, 200, await tailLog(logPaths[key], url.searchParams.get('lines')))
        }
        const staticFiles = { '/': ['index.html', '.html'], '/style.css': ['style.css', '.css'], '/app.js': ['app.js', '.js'] }
        const resource = staticFiles[url.pathname]
        if (!resource) return respond(response, 404, { error: '页面不存在。' })
        let body = await readFile(join(root, 'npm', 'launcher-ui', resource[0]), 'utf8')
        if (resource[1] === '.html') {
          body = body.replace('__LAUNCHER_TOKEN__', token)
          if (options.desktop) body = body.replace('<html lang="zh-CN">', '<html lang="zh-CN" data-desktop="true">')
        }
        return respond(response, 200, body, types[resource[1]])
      }
      if (request.method !== 'POST' || url.pathname !== '/api/action') return respond(response, 405, { error: '不支持的请求。' })
      if (request.headers['x-launcher-token'] !== token) return respond(response, 403, { error: '启动器凭据失效，请刷新页面。' })
      if (!String(request.headers['content-type']).startsWith('application/json')) return respond(response, 415, { error: '需要 JSON 请求。' })
      let data = '', tooLarge = false
      for await (const chunk of request) { data += chunk; if (data.length > 4096) { tooLarge = true; break } }
      if (tooLarge) return respond(response, 413, { error: '请求过大。' })
      let payload
      try { payload = JSON.parse(data) } catch { return respond(response, 400, { error: '请求格式错误。' }) }
      if (!payload || typeof payload !== 'object') return respond(response, 400, { error: '请求格式错误。' })
      const result = begin(payload.action, payload.target || 'all')
      return respond(response, result.code, result.error ? { error: result.error } : result.operation)
    } catch (error) {
      log(`请求失败：${error.message}`)
      respond(response, 500, { error: redactLog(error.message || '启动器处理失败，请查看日志。') })
    }
  })
  await new Promise((done, reject) => {
    server.once('error', reject)
    server.listen(options.port ?? 19387, '127.0.0.1', done)
  })
  launcherUrl = `http://127.0.0.1:${server.address().port}`
  log(`启动器已就绪：${launcherUrl}`)
  return { server, url: launcherUrl, manager, status, begin, getOperation: () => operation, close: () => new Promise(done => server.close(done)) }
}

export async function launchControlPanel(options = {}) {
  const root = resolve(options.root || packageRoot)
  const port = options.launcherPort || 19387
  const existing = `http://127.0.0.1:${port}`
  try {
    const response = await fetch(`${existing}/api/identity`, { signal: AbortSignal.timeout(1200), redirect: 'error' })
    const identity = await response.json()
    if (identity.app === 'code-agent-launcher' && resolve(identity.root) === root) {
      if (options.open !== false) await (options.openBrowser || openLocal)(existing)
      return { url: existing, reused: true }
    }
  } catch {}
  const launcher = await createLauncher({ ...options, root, port })
  if (options.open !== false) await (options.openBrowser || openLocal)(launcher.url)
  return launcher
}
