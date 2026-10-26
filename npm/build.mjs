import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, relative, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const metadata = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'))
const pythonProject = await readFile(join(root, 'pyproject.toml'), 'utf8')
const pythonVersion = /^version\s*=\s*"([^"]+)"/m.exec(pythonProject)?.[1]
if (metadata.version !== pythonVersion) {
  throw new Error(`npm/Python 版本必须一致：${metadata.version} / ${pythonVersion}`)
}

async function run(command, args) {
  await new Promise((resolveRun, reject) => {
    const child = spawn(command, args, { cwd: root, stdio: 'inherit', shell: false })
    child.on('error', reject)
    child.on('exit', code => code === 0 ? resolveRun() : reject(new Error(`构建失败（退出码 ${code}）`)))
  })
}

// Call Vite's JS entry directly; no .cmd shell or user-controlled command string.
const vite = join(root, 'web', 'node_modules', 'vite', 'bin', 'vite.js')
if (!existsSync(vite)) {
  throw new Error('缺少前端构建依赖。请先执行 npm ci --prefix web，再执行 npm pack。')
}
await run(process.execPath, [vite, 'build', join(root, 'web'), '--config', join(root, 'web', 'vite.config.js')])
if (!existsSync(join(root, 'web', 'dist', 'index.html'))) {
  throw new Error('缺少前端产物 web/dist/index.html，无法发布可启动的安装包。')
}

async function sourceFiles(directory) {
  const found = []
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name)
    if (entry.isSymbolicLink()) throw new Error(`发布源不能包含符号链接：${path}`)
    if (entry.isDirectory() && entry.name !== '__pycache__') found.push(...await sourceFiles(path))
    if (entry.isFile() && (entry.name.endsWith('.py') || (path.includes(`${join('code_agent', 'skills')}`) && entry.name.endsWith('.md')))) {
      found.push(path)
    }
  }
  return found
}
const files = [join(root, 'pyproject.toml'), ...await sourceFiles(join(root, 'code_agent'))]
files.sort()
const hash = createHash('sha256')
for (const path of files) {
  hash.update(relative(root, path).replaceAll('\\', '/'))
  hash.update('\0')
  hash.update(await readFile(path))
  hash.update('\0')
}
await writeFile(join(root, 'npm', 'release.json'), JSON.stringify({ version: metadata.version, payloadHash: hash.digest('hex') }, null, 2) + '\n')
console.log(`[code-agent] 安装包已构建：${metadata.version}（${files.length} 个后端源文件 + 预构建界面）`)
