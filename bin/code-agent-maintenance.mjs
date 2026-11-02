#!/usr/bin/env node
// This file can be extracted alone by Setup and executed with the installed
// Node. Keep imports self-contained so older installed payloads can be stopped.
import { readFile, realpath, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, relative, resolve, sep, win32 } from 'node:path'
import { pathToFileURL } from 'node:url'

const validPort = value => Number.isInteger(value) && value >= 1024 && value <= 65535
const canonical = value => resolve(value).replace(/[\\/]+$/, '')
const samePath = (left, right, platform) => platform === 'win32'
  ? canonical(left).toLowerCase() === canonical(right).toLowerCase() : canonical(left) === canonical(right)
const inside = (root, path) => {
  const name = relative(root, path)
  return name !== '' && name !== '..' && !name.startsWith('..' + sep) && !isAbsolute(name)
}

export function parseMaintenanceArgs(args) {
  const options = {}
  for (let index = 0; index < args.length; index++) {
    const flag = args[index]
    if (!['--root', '--home', '--action'].includes(flag)) throw new Error('未知维护参数；仅支持 --root、--home 和 --action stop。')
    const key = flag.slice(2)
    if (Object.hasOwn(options, key)) throw new Error(`维护参数 ${flag} 不能重复。`)
    const value = args[++index]
    if (!value || value.startsWith('--') || /[\u0000-\u001f]/.test(value)) throw new Error(`维护参数 ${flag} 缺少有效值。`)
    options[key] = value
  }
  if (options.action !== 'stop') throw new Error('维护程序仅支持 --action stop。')
  if (!options.root || !isAbsolute(options.root)) throw new Error('--root 必须是已安装 Code-agent 的绝对目录。')
  if (options.home !== undefined && !isAbsolute(options.home)) throw new Error('--home 必须是用户数据的绝对目录。')
  return options
}

async function readJson(path, label, maximum = 32 * 1024 * 1024) {
  try {
    const metadata = await stat(path)
    if (!metadata.isFile() || metadata.size > maximum) throw new Error('invalid-file')
    return JSON.parse((await readFile(path, 'utf8')).replace(/^\uFEFF/, ''))
  } catch (error) {
    if (error.code === 'ENOENT') throw error
    throw new Error(`${label}无法读取，请重新运行安装程序修复。`)
  }
}

async function ownedFile(root, value, label, platform) {
  if (typeof value !== 'string' || !value || isAbsolute(value) || win32.isAbsolute(value)
    || value.split(/[/\\]/).some(part => part === '..') || /[\u0000-\u001f]/.test(value)) throw new Error(`${label}路径无效。`)
  const path = resolve(root, value)
  try {
    if (!inside(root, path) || !samePath(await realpath(path), path, platform) || !(await stat(path)).isFile()) throw new Error('invalid-path')
  } catch { throw new Error(`${label}不属于此安装目录或已经缺失，请重新安装。`) }
  return path
}

async function validateInstallation(selected, platform) {
  if (typeof selected !== 'string' || !isAbsolute(selected) || /[\u0000-\u001f]/.test(selected)) throw new Error('必须指定已安装 Code-agent 的绝对目录。')
  const root = resolve(selected)
  try {
    if (!samePath(await realpath(root), root, platform) || !(await stat(root)).isDirectory()) throw new Error('linked-root')
    await ownedFile(root, 'installation-manifest.json', '安装身份清单', platform)
  } catch { throw new Error('该目录不是有效的 Code-agent 安装目录，请选择原安装目录。') }
  const receipt = await readJson(join(root, 'installation-manifest.json'), '安装身份清单')
  if (receipt?.schemaVersion !== 1 || receipt.appId !== 'CodeAgent' || typeof receipt.installRoot !== 'string'
    || !isAbsolute(receipt.installRoot) || !samePath(receipt.installRoot, root, platform) || !Array.isArray(receipt.files)) {
    throw new Error('Code-agent 安装身份校验失败，未执行服务维护。')
  }
  for (const name of ['desktop-runtime.json', 'package.json', 'npm/launcher-service.mjs', 'npm/launcher-process.ps1']) {
    await ownedFile(root, name, '安装组件', platform)
    if (!receipt.files.includes(name)) throw new Error('安装组件不在此应用的文件清单内，已取消维护。')
  }
  const runtime = await readJson(join(root, 'desktop-runtime.json'), '运行环境清单', 1024 * 1024)
  const metadata = await readJson(join(root, 'package.json'), '应用版本清单', 1024 * 1024)
  if (runtime?.schemaVersion !== 1 || !/^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/.test(runtime.version)
    || receipt.version !== runtime.version || metadata.version !== runtime.version) throw new Error('安装版本清单不一致，已取消维护。')
  for (const key of ['node', 'python']) {
    await ownedFile(root, runtime[key], `内置 ${key}`, platform)
    if (!receipt.files.includes(runtime[key])) throw new Error('内置运行环境不在安装文件清单内，已取消维护。')
  }
  return root
}

async function rememberedPorts(home) {
  let saved
  try { saved = await readJson(join(home, 'desktop-services.json'), '服务端口记录', 4096) }
  catch (error) {
    if (error.code === 'ENOENT') return { backend: 8765, frontend: 5175 }
    throw new Error('服务端口记录损坏，请先打开 Code-agent 启动器查看服务状态后重试。')
  }
  if (!saved || !validPort(saved.backend) || !validPort(saved.frontend) || saved.backend === saved.frontend)
    throw new Error('保存的前后端端口无效，已保留所有服务；请先打开启动器查看状态。')
  return { backend: saved.backend, frontend: saved.frontend }
}

async function defaultManager(options) {
  const module = await import(pathToFileURL(join(options.root, 'npm', 'launcher-service.mjs')).href)
  if (typeof module.LauncherServices !== 'function') throw new Error('安装版服务管理组件无效，请重新安装。')
  return new module.LauncherServices(options)
}

const inspectScript = `
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$root = [IO.Path]::GetFullPath($env:CODE_AGENT_MAINTENANCE_ROOT).TrimEnd('\\')
$owner = [int]$env:CODE_AGENT_MAINTENANCE_PID
$found = @(Get-CimInstance Win32_Process -OperationTimeoutSec 5 -ErrorAction Stop | Where-Object {
    $_.ProcessId -ne $owner -and $_.ExecutablePath -and
    ([IO.Path]::GetFullPath($_.ExecutablePath)).StartsWith($root + '\\', [StringComparison]::OrdinalIgnoreCase)
} | ForEach-Object { [pscustomobject]@{ pid = [int]$_.ProcessId; name = [string]$_.Name } })
ConvertTo-Json -InputObject $found -Compress
`

/** Read-only scan. Never terminate a launcher, browser, terminal or project process. */
export async function inspectRemainingProcesses(manager, { root, pid = process.pid, platform = process.platform } = {}) {
  if (platform !== 'win32') throw new Error('此维护操作仅支持 Windows 安装版。')
  const result = await manager.run('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-EncodedCommand', Buffer.from(inspectScript, 'utf16le').toString('base64')], {
    cwd: root, env: { ...manager.env, CODE_AGENT_MAINTENANCE_ROOT: root, CODE_AGENT_MAINTENANCE_PID: String(pid) },
    timeout: 15000, capture: true,
  })
  if (result.code !== 0) throw new Error('无法确认安装目录中的进程已全部关闭，请关闭 Code-agent 窗口后重试。')
  let rows
  try { rows = JSON.parse(result.stdout.replace(/^\uFEFF/, '').trim()) } catch { throw new Error('无法读取安装目录的进程状态，请稍后重试。') }
  if (!Array.isArray(rows) || rows.some(row => !Number.isInteger(row.pid) || row.pid <= 0 || typeof row.name !== 'string'))
    throw new Error('安装目录进程检查结果无效，请稍后重试。')
  return rows
}

/** Only the two verified application services can be stopped by this entrypoint. */
export async function runMaintenance(options, adapters = {}) {
  if (options.action !== 'stop') throw new Error('维护程序仅支持停止应用服务。')
  const platform = adapters.platform || process.platform
  if (platform !== 'win32') throw new Error('此维护操作仅支持 Windows 安装版。')
  const root = await validateInstallation(options.root, platform)
  const env = adapters.env || process.env
  const selectedHome = options.home || env.CODE_AGENT_HOME || join(env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local'), 'Code-agent', 'data')
  if (!isAbsolute(selectedHome) || /[\u0000-\u001f]/.test(selectedHome)) throw new Error('用户数据目录必须是有效的绝对路径。')
  const home = resolve(selectedHome)
  const ports = await rememberedPorts(home)
  const manager = await (adapters.createManager || defaultManager)({ root, home, mode: 'bundled', platform, env,
    backendPort: ports.backend, frontendPort: ports.frontend, log: () => {} })
  await manager.initialize()
  const statuses = await manager.status()
  if (statuses.some(service => service.state === 'foreign')) throw new Error('保存的服务端口由其他项目占用，已保留其进程。请检查启动器中的端口与服务状态。')
  await manager.action('stop', 'all')
  const remaining = await (adapters.inspectRemaining || inspectRemainingProcesses)(manager, { root, platform })
  if (remaining.length) {
    const names = remaining.slice(0, 6).map(row => `${row.name}（PID ${row.pid}）`).join('、')
    throw new Error(`应用目录仍有进程运行：${names}。请关闭 Code-agent 启动器、相关终端或调试窗口后重试。`)
  }
  return { ok: true, message: 'Code-agent 前后端已安全停止，可以继续更新或卸载。' }
}

export function maintenanceMessage(error) {
  return String(error?.message || '维护操作失败，请查看启动器状态后重试。')
    .replace(/\b(Bearer\s+)[\w.+/=-]+/gi, '$1[已隐藏]')
    .replace(/\bsk-[a-zA-Z0-9_-]{12,}/g, '[已隐藏密钥]')
    .replace(/((?:api[_ .-]?key|access[_ .-]?token|authorization|password|secret)["']?\s*[=:]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)/gi, '$1[已隐藏]')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/g, '$1[已隐藏]@')
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const result = await runMaintenance(parseMaintenanceArgs(process.argv.slice(2)))
    process.stdout.write(JSON.stringify(result) + '\n')
  } catch (error) {
    const message = maintenanceMessage(error)
    process.stderr.write(`Code-agent 维护未完成：${message}\n`)
    process.stdout.write(JSON.stringify({ ok: false, message }) + '\n')
    process.exitCode = 1
  }
}
