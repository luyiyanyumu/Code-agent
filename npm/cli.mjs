import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import net from 'node:net'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export const latestPackage = 'https://github.com/luyiyanyumu/Code-agent/releases/latest/download/code-agent.tgz'
const providers = new Set(['offline', 'deepseek', 'openai', 'openai_compat'])
const help = `Code-agent：本地 AI 编码工作台

用法：code-agent [start|install|update] [选项]
  start              启动工作台（默认）；首次自动安装 Python 后端
  install            只准备当前版本的运行环境
  update             下载最新发布包并准备环境，不中断已有服务
  --host <地址>      监听地址，默认 127.0.0.1
  --port <端口>      监听端口，默认 8765
  --provider <名称>  offline / deepseek / openai / openai_compat
  --home <目录>      持久数据目录，默认 ~/.code-agent
  --python <路径>    Python 3.11+ 可执行文件（路径含空格时加引号）
  --no-open          不自动打开浏览器
  -h, --help         查看帮助
  -v, --version      查看版本

需要 Node.js 20.11+、Python 3.11+。可启动后在设置中配置模型，或使用环境变量 / 数据目录 .env。
`

export function parseArgs(args) {
  const options = { command: 'start', host: '127.0.0.1', port: 8765, open: true }
  let commandSeen = false
  for (let index = 0; index < args.length; index++) {
    const arg = args[index]
    if (['-h', '--help'].includes(arg)) { options.help = true; continue }
    if (['-v', '--version'].includes(arg)) { options.version = true; continue }
    if (arg === '--no-open') { options.open = false; continue }
    if (['start', 'install', 'update'].includes(arg) && !commandSeen) {
      options.command = arg
      commandSeen = true
      continue
    }
    const [flag, ...inlineValue] = arg.split('=')
    if (!['--host', '--port', '--provider', '--home', '--python'].includes(flag)) {
      throw new Error(`未知参数：${arg}。使用 --help 查看用法。`)
    }
    const value = inlineValue.length ? inlineValue.join('=') : args[++index]
    if (!value || value.startsWith('--')) throw new Error(`${flag} 缺少参数值。`)
    options[flag.slice(2)] = value
  }
  if (!/^\d+$/.test(String(options.port)) || Number(options.port) < 1 || Number(options.port) > 65535) {
    throw new Error('--port 必须为 1 到 65535 的整数。')
  }
  options.port = Number(options.port)
  if (!/^[a-zA-Z0-9.:[\]_-]+$/.test(options.host)) throw new Error('--host 必须是有效的主机名或 IP 地址。')
  options.host = options.host.replace(/^\[([^\]]+)\]$/, '$1')
  if (options.provider && !providers.has(options.provider)) throw new Error(`不支持的模型供应商：${options.provider}`)
  return options
}

export function runtimePaths(home, release, platform = process.platform) {
  if (!/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(release.version) || !/^[a-f0-9]{64}$/.test(release.payloadHash)) {
    throw new Error('安装包的版本或后端校验信息不完整，请重新下载正式发布包。')
  }
  const directory = join(resolve(home), 'runtime', 'npm', `${release.version}-${release.payloadHash.slice(0, 16)}`)
  return {
    directory,
    python: platform === 'win32' ? join(directory, 'Scripts', 'python.exe') : join(directory, 'bin', 'python'),
    ready: join(directory, 'ready.json'),
    lock: `${directory}.lock`,
  }
}

/** No shell is involved, including on Windows; executable paths remain single arguments. */
export function runCommand(command, args, options = {}) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      shell: false,
      windowsHide: true,
      stdio: options.capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
    })
    let stdout = ''
    let stderr = ''
    let forwardedSignal
    let finished = false
    const forward = signal => {
      forwardedSignal = signal
      if (!finished) child.kill(signal)
    }
    const interrupt = () => forward('SIGINT')
    const terminate = () => forward('SIGTERM')
    process.on('SIGINT', interrupt)
    process.on('SIGTERM', terminate)
    const cleanup = () => {
      finished = true
      process.off('SIGINT', interrupt)
      process.off('SIGTERM', terminate)
    }
    if (options.capture) {
      child.stdout.on('data', data => { stdout = (stdout + data).slice(-65536) })
      child.stderr.on('data', data => { stderr = (stderr + data).slice(-65536) })
    }
    child.once('error', error => { cleanup(); reject(error) })
    child.once('close', (code, signal) => {
      cleanup()
      const stoppedBy = signal || forwardedSignal
      resolveRun({ code: stoppedBy === 'SIGINT' ? 130 : stoppedBy === 'SIGTERM' ? 143 : code ?? 1, stdout, stderr })
    })
  })
}

async function checkedRun(run, command, args, options, label) {
  const result = await run(command, args, options)
  if (result.code !== 0) {
    const error = new Error(`${label}失败（退出码 ${result.code}）。解决上述错误后重新运行即可继续安装。`)
    error.exitCode = result.code
    throw error
  }
  return result
}

export async function findPython({ explicit, env = process.env, platform = process.platform, run = runCommand, cwd = process.cwd() } = {}) {
  const selected = explicit || env.CODE_AGENT_PYTHON
  const candidates = selected ? [[selected, []]] : platform === 'win32'
    ? [['py', ['-3']], ['python3', []], ['python', []]]
    : [['python3', []], ['python', []]]
  for (const [command, prefix] of candidates) {
    try {
      const result = await run(command, [...prefix, '-c', 'import json,sys; print(json.dumps(list(sys.version_info[:3])))'], { capture: true, env, cwd })
      if ([130, 143].includes(result.code)) {
        const error = new Error('Python 检测已取消。')
        error.exitCode = result.code
        throw error
      }
      if (result.code !== 0) continue
      const version = JSON.parse(result.stdout.trim())
      if (version[0] === 3 && version[1] >= 11) return { command, prefix }
    } catch (error) {
      if (error.exitCode) throw error
      // Missing Windows launcher / python alias / older interpreters: try the next candidate.
    }
  }
  throw new Error(selected
    ? '指定的 Python 不可用或版本低于 3.11。请用 --python 指定 Python 3.11+ 可执行文件。'
    : '未找到 Python 3.11+。请安装 Python 后重试，或用 --python / CODE_AGENT_PYTHON 指定路径。')
}

async function runtimeReady(paths, release) {
  try {
    const ready = JSON.parse(await readFile(paths.ready, 'utf8'))
    return ready.version === release.version && ready.payloadHash === release.payloadHash && existsSync(paths.python)
  } catch { return false }
}

export async function ensureRuntime({ root = packageRoot, home, release, python, env = process.env, platform = process.platform, run = runCommand, log = console.log }) {
  const paths = runtimePaths(home, release, platform)
  if (await runtimeReady(paths, release)) return paths
  await mkdir(dirname(paths.directory), { recursive: true })
  try { await mkdir(paths.lock) } catch (error) {
    if (error.code === 'EEXIST') throw new Error(`此版本正在安装，请等待另一条安装命令完成后重试。若安装曾异常退出，确认无安装进程后删除此锁目录再重试：${paths.lock}`)
    throw error
  }
  try {
    // Another installer may have completed between the first check and lock acquisition.
    if (await runtimeReady(paths, release)) return paths
    await mkdir(paths.directory, { recursive: true })
    await rm(paths.ready, { force: true })
    const selected = await findPython({ explicit: python, env, platform, run, cwd: root })
    log(`[code-agent] 首次准备 ${release.version}，正在创建独立 Python 环境……`)
    await checkedRun(run, selected.command, [...selected.prefix, '-m', 'venv', paths.directory], { cwd: root, env }, '创建 Python 环境')
    await checkedRun(run, paths.python, ['-I', '-m', 'pip', '--disable-pip-version-check', 'install', '--no-input', '--upgrade', '--progress-bar', 'off', root], { cwd: root, env }, '安装后端依赖')
    await checkedRun(run, paths.python, ['-I', '-c', 'import code_agent.server'], { cwd: root, env }, '后端导入验证')
    if (!existsSync(paths.python)) throw new Error('Python 环境未生成可执行文件，请检查 Python venv 组件是否安装。')
    const temporary = `${paths.ready}.${process.pid}.tmp`
    await writeFile(temporary, JSON.stringify(release) + '\n', 'utf8')
    await rename(temporary, paths.ready)
    log(`[code-agent] ${release.version} 已就绪，数据目录：${home}`)
    return paths
  } finally {
    await rm(paths.lock, { recursive: true, force: true })
  }
}

export async function assertPortFree(host, port) {
  await new Promise((resolveCheck, reject) => {
    const server = net.createServer()
    server.once('error', error => {
      reject(new Error(error.code === 'EADDRINUSE'
        ? `端口 ${port} 已被占用。已有服务不会被停止；请使用原服务或通过 --port 指定其他端口。`
        : `无法监听 ${host}:${port}（${error.code || '未知错误'}）。请检查 --host 和 --port。`))
    })
    server.listen({ host, port, exclusive: true }, () => server.close(resolveCheck))
  })
}

export function browserUrl(host, port) {
  const connectHost = host === '0.0.0.0' ? '127.0.0.1' : host === '::' ? '::1' : host
  return `http://${connectHost.includes(':') ? `[${connectHost}]` : connectHost}:${port}`
}

async function openBrowser(url, { platform = process.platform, run = runCommand, log = console.log, env = process.env, cwd = process.cwd() } = {}) {
  const [command, args] = platform === 'win32'
    ? ['rundll32.exe', ['url.dll,FileProtocolHandler', url]]
    : platform === 'darwin' ? ['open', [url]] : ['xdg-open', [url]]
  try {
    const result = await run(command, args, { capture: true, env, cwd })
    if (result.code !== 0) log(`[code-agent] 请在浏览器中打开 ${url}`)
  } catch { log(`[code-agent] 请在浏览器中打开 ${url}`) }
}

async function openWhenHealthy(url, signal, options) {
  const deadline = Date.now() + 60000
  while (!signal.aborted && Date.now() < deadline) {
    try {
      const response = await fetch(`${url}/healthz`, { signal: AbortSignal.any([signal, AbortSignal.timeout(1500)]) })
      if (response.ok && !signal.aborted) { await openBrowser(url, options); return }
    } catch { /* startup not ready yet */ }
    if (!signal.aborted) await new Promise(resolveSleep => {
      const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolveSleep() }
      const timer = setTimeout(finish, 350)
      signal.addEventListener('abort', finish, { once: true })
    })
  }
}

export function findNpmCli(env = process.env, node = process.execPath) {
  const candidates = [
    env.npm_execpath,
    join(dirname(node), 'node_modules', 'npm', 'bin', 'npm-cli.js'),
    join(dirname(node), '..', 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js'),
    join(dirname(node), '..', 'share', 'nodejs', 'npm', 'bin', 'npm-cli.js'),
    ...(env.PATH || env.Path || '').split(process.platform === 'win32' ? ';' : ':').flatMap(path => [
      join(path, 'node_modules', 'npm', 'bin', 'npm-cli.js'),
      join(path, '..', 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js'),
      join(path, '..', 'share', 'nodejs', 'npm', 'bin', 'npm-cli.js'),
    ]),
  ]
  const found = candidates.find(path => path && /(?:^|[/\\])npm-cli\.js$/.test(path) && existsSync(path))
  if (!found) throw new Error('无法定位 npm-cli.js。请使用包含 npm 的 Node.js 安装，或通过 npx 启动更新。')
  return resolve(found)
}

export async function updateLatest(options, { run = runCommand, env = process.env, cwd = process.cwd(), npmCli, node = process.execPath, log = console.log } = {}) {
  const source = env.CODE_AGENT_UPDATE_PACKAGE || latestPackage
  if (!source.startsWith('https://') && !isAbsolute(source)) throw new Error('更新包必须是 HTTPS 地址或本地绝对路径。')
  const args = [npmCli || findNpmCli(env, node), 'exec', '--yes', '--prefer-online', '--package', source, '--', 'code-agent', 'install']
  if (options.home) args.push('--home', options.home)
  if (options.python) args.push('--python', options.python)
  log('[code-agent] 正在下载并准备最新版本；已有服务继续运行。')
  const result = await run(node, args, { cwd, env })
  if (result.code === 0) log('[code-agent] 最新运行环境已准备好。停止旧服务后，使用 npx --yes --prefer-online --package ' + source + ' code-agent 启动新版，并保留原来的 --home、--port 等启动选项。')
  return result.code
}

export async function main(args = process.argv.slice(2), dependencies = {}) {
  const root = dependencies.root || packageRoot
  const env = { ...(dependencies.env || process.env) }
  const cwd = dependencies.cwd || process.cwd()
  const run = dependencies.run || runCommand
  const log = dependencies.log || console.log
  const errorLog = dependencies.error || console.error
  const platform = dependencies.platform || process.platform
  try {
    const nodeVersion = (dependencies.nodeVersion || process.versions.node).split('.').map(Number)
    if (nodeVersion[0] < 20 || (nodeVersion[0] === 20 && nodeVersion[1] < 11)) throw new Error('需要 Node.js 20.11 或更新版本。')
    const options = parseArgs(args)
    // A relative executable is relative to the invoking project, not the temporary npx package.
    if (options.python && /[/\\]/.test(options.python) && !isAbsolute(options.python)) options.python = resolve(cwd, options.python)
    if (env.CODE_AGENT_PYTHON && /[/\\]/.test(env.CODE_AGENT_PYTHON) && !isAbsolute(env.CODE_AGENT_PYTHON)) env.CODE_AGENT_PYTHON = resolve(cwd, env.CODE_AGENT_PYTHON)
    const metadata = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'))
    if (options.help) { log(help); return 0 }
    if (options.version) { log(metadata.version); return 0 }
    if (options.command === 'update') return await updateLatest(options, { run, env, cwd, npmCli: dependencies.npmCli, log })
    const home = resolve(cwd, options.home || env.CODE_AGENT_HOME || join(homedir(), '.code-agent'))
    env.CODE_AGENT_HOME = home
    const checkPort = dependencies.checkPort || assertPortFree
    if (options.command === 'start') await checkPort(options.host, options.port)
    if (!existsSync(join(root, 'web', 'dist', 'index.html'))) throw new Error('此安装包缺少预构建界面，请重新下载正式发布包。')
    const release = JSON.parse(await readFile(join(root, 'npm', 'release.json'), 'utf8'))
    if (release.version !== metadata.version) throw new Error('安装包版本不一致，请重新下载正式发布包。')
    const runtime = await ensureRuntime({ root, home, release, python: options.python, env, platform, run, log })
    if (options.command === 'install') return 0
    await checkPort(options.host, options.port)
    env.CODE_AGENT_WEB_DIST = join(root, 'web', 'dist')
    if (!env.CODE_AGENT_ENV_FILE && existsSync(join(home, '.env'))) env.CODE_AGENT_ENV_FILE = join(home, '.env')
    const serverArgs = ['-I', '-m', 'code_agent.server', '--host', options.host, '--port', String(options.port)]
    if (options.provider) serverArgs.push('--provider', options.provider)
    const url = browserUrl(options.host, options.port)
    log(`[code-agent] 工作台：${url}，按 Ctrl+C 停止。`)
    const controller = new AbortController()
    const launch = options.open ? (dependencies.openWhenHealthy || openWhenHealthy)(url, controller.signal, { run, platform, env, cwd, log }) : Promise.resolve()
    try {
      return (await run(runtime.python, serverArgs, { cwd, env })).code
    } finally {
      controller.abort()
      await launch
    }
  } catch (error) {
    errorLog(`[code-agent] ${error.message}`)
    return Number.isInteger(error.exitCode) && error.exitCode > 0 && error.exitCode < 256 ? error.exitCode : 1
  }
}
