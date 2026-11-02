import { existsSync } from 'node:fs'
import { readFile, realpath } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { findNpmCli, latestPackage, runCommand } from './cli.mjs'
import { bundledRuntimeManifest } from './bundled-runtime.mjs'

const registryUrl = 'https://registry.npmjs.org/@luyiyanyumu%2fcode-agent/latest'
const versionPattern = /^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/
const officialRemote = /^(?:https:\/\/github\.com\/luyiyanyumu\/Code-agent(?:\.git)?\/?|git@github\.com:luyiyanyumu\/Code-agent(?:\.git)?|ssh:\/\/git@github\.com\/luyiyanyumu\/Code-agent(?:\.git)?)$/i
export const installerReleasePage = 'https://github.com/luyiyanyumu/Code-agent/releases/latest'
const installerReleaseApi = 'https://api.github.com/repos/luyiyanyumu/Code-agent/releases/latest'

/** A worktree's .git is a file. Installed packages also contain pyproject.toml. */
export async function detectMode(root) {
  if (existsSync(join(root, bundledRuntimeManifest))) return 'bundled'
  return existsSync(join(root, '.git')) ? 'source' : 'installed'
}

function redact(value, env) {
  let text = String(value || '')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1[已隐藏]@')
    .replace(/(Bearer\s+)[\w.+/=-]+/gi, '$1[已隐藏]')
    .replace(/((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+/gi, '$1[已隐藏]')
  for (const [name, secret] of Object.entries(env || {})) {
    if (/(?:KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)/i.test(name) && typeof secret === 'string' && secret.length >= 6) {
      text = text.split(secret).join('[已隐藏]')
    }
  }
  return text.slice(-16000)
}

async function command(context, executable, args, label, { output = false } = {}) {
  if (output) context.log(`${label}……`)
  let result
  try {
    result = await context.run(executable, args, { cwd: context.root, env: context.env, capture: true })
  } catch {
    throw new Error(`${label}失败：无法执行所需程序，请检查 Git、Node.js 和 Python 环境。`)
  }
  if (output) {
    const detail = redact([result.stdout, result.stderr].filter(Boolean).join('\n'), context.env)
    if (detail) context.log(detail)
  }
  if (result.code !== 0) throw new Error(`${label}失败（退出码 ${result.code ?? '未知'}），请查看更新日志后重试。`)
  return String(result.stdout || '').trim()
}

async function contextFor(options) {
  const root = resolve(options.root)
  const metadata = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'))
  return {
    ...options, root, current: metadata.version,
    mode: options.mode || await detectMode(root),
    run: options.run || runCommand, env: { ...(options.env || process.env) },
    log: options.log || (() => {}),
  }
}

function pathEqual(left, right) {
  return process.platform === 'win32' ? left.toLowerCase() === right.toLowerCase() : left === right
}

async function sourceIdentity(context) {
  const top = await command(context, 'git', ['rev-parse', '--show-toplevel'], '定位源码仓库')
  if (!pathEqual(await realpath(top), await realpath(context.root))) throw new Error('启动器目录不是仓库根目录，已拒绝更新。')
  const branch = await command(context, 'git', ['symbolic-ref', '--quiet', '--short', 'HEAD'], '检查当前分支')
  const remote = await command(context, 'git', ['config', '--get', `branch.${branch}.remote`], '检查跟踪远端')
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(remote)) throw new Error('当前分支没有可更新的远端，请先配置官方仓库的跟踪分支。')
  const remoteUrl = await command(context, 'git', ['remote', 'get-url', remote], '核对更新来源')
  if (!officialRemote.test(remoteUrl)) throw new Error('更新来源不是 Code-agent 官方 GitHub 仓库，已拒绝自动更新。')
  const upstream = await command(context, 'git', ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}'], '检查跟踪分支')
  return { branch, remote, upstream }
}

async function sourceState(context, identity) {
  const dirty = Boolean(await command(context, 'git', ['status', '--porcelain', '--untracked-files=all'], '检查本地改动'))
  const head = await command(context, 'git', ['rev-parse', 'HEAD'], '读取当前提交')
  const counts = await command(context, 'git', ['rev-list', '--left-right', '--count', 'HEAD...@{upstream}'], '比较远端提交')
  const match = /^(\d+)\s+(\d+)$/.exec(counts)
  if (!match) throw new Error('无法比较本地与远端提交，未进行更新。')
  return { ...identity, head, dirty, ahead: Number(match[1]), behind: Number(match[2]) }
}

function isNewer(latest, current) {
  if (!versionPattern.test(current)) return latest !== current
  const parse = value => { const [base, ...extra] = value.split('-'); return { base: base.split('.').map(Number), pre: extra.join('-') } }
  const next = parse(latest), now = parse(current)
  for (let index = 0; index < 3; index++) if (next.base[index] !== now.base[index]) return next.base[index] > now.base[index]
  if (!next.pre || !now.pre) return Boolean(!next.pre && now.pre)
  const left = next.pre.split('.'), right = now.pre.split('.')
  for (let index = 0; index < Math.max(left.length, right.length); index++) {
    if (left[index] === undefined || right[index] === undefined) return left[index] !== undefined
    if (left[index] === right[index]) continue
    const ln = /^\d+$/.test(left[index]), rn = /^\d+$/.test(right[index])
    if (ln && rn) return Number(left[index]) > Number(right[index])
    if (ln !== rn) return !ln
    return left[index] > right[index]
  }
  return false
}

async function registryVersion(fetchImpl) {
  const response = await fetchImpl(registryUrl, { signal: AbortSignal.timeout(12000), redirect: 'error', headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error('npm 版本查询暂不可用，请稍后重试。')
  const metadata = await response.json()
  if (metadata.name !== '@luyiyanyumu/code-agent' || !versionPattern.test(metadata.version)) throw new Error('npm 返回的包信息不完整，未接受此版本。')
  return metadata.version
}

async function installerVersion(fetchImpl, report) {
  const response = await fetchImpl(installerReleaseApi, {
    signal: AbortSignal.timeout(12000), redirect: 'error',
    headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'Code-agent-Desktop-Updater' },
  })
  report.downloadPage = installerReleasePage
  if (response.status === 404) {
    report.message = '尚未发现已发布的 Windows 安装包；可在官方发布页查看后续版本。'
    return
  }
  if (!response.ok) throw new Error('安装包版本查询暂不可用，请稍后重试。')
  const release = await response.json()
  const version = String(release.tag_name || '').replace(/^v/, '')
  if (!versionPattern.test(version) || release.draft || release.prerelease) throw new Error('官方发布信息不完整，未接受此安装包版本。')
  const asset = (Array.isArray(release.assets) ? release.assets : []).find(asset => {
    if (!asset || !/(?:setup|install).*\.exe$|\.msi$/i.test(asset.name || '')) return false
    try {
      const url = new URL(asset.browser_download_url)
      return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password
        && !url.hash && url.pathname.startsWith('/luyiyanyumu/Code-agent/releases/download/')
    } catch { return false }
  })
  report.latest = version
  report.available = Boolean(asset) && isNewer(version, report.current)
  if (asset) report.downloadUrl = asset.browser_download_url
  report.message = !asset ? '官方最新发布暂未提供 Windows 安装包。'
    : report.available ? `发现新版 ${version}，请下载并运行新版安装包，原有会话和配置会保留。` : '当前已是最新安装包版本。'
}

/** Explicit checks may fetch refs; they never touch working files or start builds. */
export async function checkUpdate(options) {
  const context = await contextFor(options)
  const report = { current: context.current, latest: null, available: false, checkedAt: new Date().toISOString(), error: null, mode: context.mode }
  try {
    if (context.mode === 'bundled') {
      await installerVersion(options.fetchImpl || fetch, report)
    } else if (context.mode === 'source') {
      const identity = await sourceIdentity(context)
      await command(context, 'git', ['fetch', '--no-tags', identity.remote], '获取官方远端版本')
      report.source = await sourceState(context, identity)
      report.available = report.source.behind > 0
      report.latest = report.source.behind > 0 ? `远端领先 ${report.source.behind} 个提交` : context.current
      report.message = report.source.dirty
        ? '存在本地未提交改动，更新前需要先处理；启动器会保留这些改动。'
        : report.source.ahead > 0 && report.source.behind > 0
          ? '本地与远端分支已分叉，自动更新将拒绝合并，请手动处理。'
          : report.available ? `发现 ${report.source.behind} 个新提交。` : '当前分支已包含远端最新提交。'
    } else {
      report.latest = await registryVersion(options.fetchImpl || fetch)
      report.available = isNewer(report.latest, context.current)
      report.message = report.available ? `发现新版 ${report.latest}。` : '当前已是最新发布版本。'
    }
  } catch (error) {
    report.error = redact(error.message, context.env)
    report.message = report.error
  }
  return report
}

/** Never resets, stashes or overwrites a dirty checkout. Installed updates only stage a new runtime. */
export async function applyUpdate(options) {
  const context = await contextFor(options)
  if (context.mode === 'bundled') {
    return { mode: context.mode, updated: false, staged: false, restartRequired: false, downloadPage: installerReleasePage,
      message: `安装版请下载并运行新版安装包，原有会话与配置会保留：${installerReleasePage}` }
  }
  if (context.mode === 'installed') {
    // Ignore CODE_AGENT_UPDATE_PACKAGE here: the control panel only installs the official package.
    const npmCli = options.npmCli || findNpmCli(context.env, options.node || process.execPath)
    const args = [npmCli, 'exec', '--yes', '--prefer-online', '--package', latestPackage, '--', 'code-agent', 'install', '--home', resolve(options.home || context.env.CODE_AGENT_HOME || join(homedir(), '.code-agent'))]
    if (options.python) args.push('--python', options.python)
    await command(context, options.node || process.execPath, args, '下载并准备最新正式版本', { output: true })
    return { mode: context.mode, updated: false, staged: true, restartRequired: true, message: '最新正式版运行环境已准备好，当前服务继续运行。停止旧服务后，请使用 npx @luyiyanyumu/code-agent@latest 启动新版；当前启动器仍属于原安装包。' }
  }
  // This is deliberately before fetch, dependency install or any filesystem mutation.
  if (await command(context, 'git', ['status', '--porcelain', '--untracked-files=all'], '检查本地改动')) {
    throw new Error('源码目录存在未提交或未跟踪的文件，已保留全部改动并停止更新。请先自行提交或处理改动后重试。')
  }
  const identity = await sourceIdentity(context)
  await command(context, 'git', ['fetch', '--no-tags', identity.remote], '获取官方远端版本', { output: true })
  const state = await sourceState(context, identity)
  if (state.dirty) throw new Error('检查期间出现了本地改动，已停止更新并保留文件。')
  if (state.ahead && state.behind) throw new Error('本地与远端分支已分叉，已拒绝自动合并。请手动处理后重试。')
  if (!state.behind) return { mode: context.mode, updated: false, staged: false, restartRequired: false, message: '当前分支已包含远端最新提交，无需更新。' }
  const npmCli = options.npmCli || findNpmCli(context.env, options.node || process.execPath)
  const node = options.node || process.execPath
  const python = options.python || (process.platform === 'win32' ? join(context.root, '.venv', 'Scripts', 'python.exe') : join(context.root, '.venv', 'bin', 'python'))
  if (!existsSync(python)) throw new Error('源码 Python 环境缺失，尚未更新文件。请先创建 .venv 并安装项目依赖。')
  await command(context, 'git', ['merge', '--ff-only', '--no-edit', '@{upstream}'], '快进更新源码', { output: true })
  // No dependency or source cleanup on failure: the log tells the user which stage to retry.
  try {
    await command(context, node, [npmCli, 'ci', '--no-audit', '--no-fund'], '安装内核构建依赖', { output: true })
    await command(context, node, [npmCli, 'ci', '--prefix', 'web', '--no-audit', '--no-fund'], '安装前端构建依赖', { output: true })
    await command(context, python, ['-X', 'utf8', '-m', 'pip', '--disable-pip-version-check', 'install', '--no-input', '-e', context.root], '更新后端依赖', { output: true })
    await command(context, node, [join(context.root, 'npm', 'build.mjs')], '构建内核和前端界面', { output: true })
    await command(context, python, ['-X', 'utf8', '-m', 'code_agent.harness', '--prepare'], '验证内核运行环境', { output: true })
    await command(context, python, ['-X', 'utf8', '-c', 'import code_agent.server'], '验证后端导入', { output: true })
  } catch (error) {
    throw new Error(`源码已快进更新，但构建尚未完成。${error.message} 当前服务未被自动停止，请查看日志修复后重建。`)
  }
  const metadata = JSON.parse(await readFile(join(context.root, 'package.json'), 'utf8'))
  return { mode: context.mode, updated: true, staged: false, current: metadata.version, restartRequired: true, message: '源码、依赖和前端界面已更新并通过导入验证。请在任务空闲时重启前后端生效。' }
}
