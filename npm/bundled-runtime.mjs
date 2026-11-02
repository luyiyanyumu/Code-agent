import { readFile, readdir, realpath, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, relative, resolve, sep, win32 } from 'node:path'
import { loadLanguageTools } from './language-runtime.mjs'

export const bundledRuntimeManifest = 'desktop-runtime.json'

export function bundledDataHome({ home, env = process.env } = {}) {
  return resolve(home || env.CODE_AGENT_HOME || join(env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local'), 'Code-agent', 'data'))
}

function inside(root, candidate) {
  const name = relative(root, candidate)
  return name !== '' && name !== '..' && !name.startsWith('..' + sep) && !isAbsolute(name)
}

async function runtimePath(root, realRoot, value, label, directory = false) {
  if (typeof value !== 'string' || !value || isAbsolute(value) || win32.isAbsolute(value)
    || value.split(/[/\\]/).some(part => part === '..') || /[\u0000-\u001f]/.test(value)) {
    throw new Error(`安装包的${label}路径无效，请重新运行安装程序。`)
  }
  const candidate = resolve(root, value)
  if (!inside(root, candidate)) throw new Error(`安装包的${label}路径超出应用目录。`)
  try {
    const actual = await realpath(candidate)
    if (!inside(realRoot, actual)) throw new Error('outside')
    const metadata = await stat(actual)
    if (directory ? !metadata.isDirectory() : !metadata.isFile()) throw new Error('wrong-kind')
  } catch {
    throw new Error(`安装包缺少有效的${label}，请重新运行安装程序。`)
  }
  return candidate
}

/** Bundled runtimes are relocatable files, never a venv tied to a build machine. */
export async function loadBundledRuntime(root) {
  root = resolve(root)
  let manifest, metadata
  try {
    manifest = JSON.parse((await readFile(join(root, bundledRuntimeManifest), 'utf8')).replace(/^\uFEFF/, ''))
    metadata = JSON.parse((await readFile(join(root, 'package.json'), 'utf8')).replace(/^\uFEFF/, ''))
  } catch { throw new Error('安装包运行环境清单无法读取，请重新运行安装程序。') }
  if (manifest.schemaVersion !== 1 || !/^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/.test(manifest.version)
    || metadata.version !== manifest.version) throw new Error('安装包版本或运行环境清单不一致，请重新运行安装程序。')
  const realRoot = await realpath(root)
  const node = await runtimePath(root, realRoot, manifest.node, 'Node.js')
  const python = await runtimePath(root, realRoot, manifest.python, 'Python')
  const webview2 = await runtimePath(root, realRoot, manifest.webview2, 'WebView2', true)
  const git = manifest.git === undefined ? undefined : await runtimePath(root, realRoot, manifest.git, 'Git')
  const languageTools = await loadLanguageTools(root, manifest)
  const javaFields = ['java', 'jdtls', 'lombok'].filter(name => manifest[name] !== undefined)
  let javaTools = {}
  if (javaFields.length) {
    if (javaFields.length !== 3) throw new Error('安装包的 Java 分析组件不完整，请重新运行安装程序。')
    const java = await runtimePath(root, realRoot, manifest.java, 'Java')
    const jdtls = await runtimePath(root, realRoot, manifest.jdtls, 'Java 语言服务', true)
    const lombok = await runtimePath(root, realRoot, manifest.lombok, 'Lombok 分析支持')
    for (const [path, label, directory] of [
      [join(dirname(manifest.java), 'javac.exe'), 'Java 编译器'],
      [join(dirname(dirname(manifest.java)), 'release'), 'Java 版本信息'],
      [join(dirname(dirname(manifest.java)), 'legal'), 'Java 许可声明', true],
      [join(manifest.jdtls, 'bin/jdtls'), 'Java 服务启动程序'],
      [join(manifest.jdtls, 'bin/jdtls.py'), 'Java 服务启动模块'],
      [join(manifest.jdtls, 'config_win/config.ini'), 'Java 服务配置'],
    ]) await runtimePath(root, realRoot, path, label, directory)
    const plugins = await runtimePath(root, realRoot, join(manifest.jdtls, 'plugins'), 'Java 服务依赖', true)
    const launcher = (await readdir(plugins)).find(name => /^org\.eclipse\.equinox\.launcher_[^/\\]+\.jar$/.test(name))
    if (!launcher) throw new Error('安装包缺少 Java 服务引擎，请重新运行安装程序。')
    await runtimePath(root, realRoot, join(manifest.jdtls, 'plugins', launcher), 'Java 服务引擎')
    javaTools = { java, jdtls, lombok }
  }
  await runtimePath(root, realRoot, join(manifest.webview2, 'msedgewebview2.exe'), 'WebView2 引擎')
  for (const [path, label] of [
    ['web/dist/index.html', '前端界面'], ['code_agent/server.py', '后端程序'],
    ['code_agent/harness/node/runner.mjs', '智能体内核'], ['scripts/backend_idle_check.py', '任务状态检查程序'],
    ['npm/launcher-preview.mjs', '前端运行程序'],
  ]) await runtimePath(root, realRoot, path, label)
  return { root, version: manifest.version, node, python, webview2, git, ...javaTools, ...(languageTools ? {languageTools} : {}), manifest }
}
