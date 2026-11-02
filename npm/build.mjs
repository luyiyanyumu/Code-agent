import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { payloadFingerprint } from './payload.mjs'

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

// Build the editable, merged Harness sources before computing release identity.
await run(process.execPath, [join(root, 'code_agent', 'harness', 'node', 'build.mjs')])

// Call Vite's JS entry directly; no .cmd shell or user-controlled command string.
const vite = join(root, 'web', 'node_modules', 'vite', 'bin', 'vite.js')
if (!existsSync(vite)) {
  throw new Error('缺少前端构建依赖。请先执行 npm ci --prefix web，再执行 npm pack。')
}
await run(process.execPath, [vite, 'build', join(root, 'web'), '--config', join(root, 'web', 'vite.config.js')])
if (!existsSync(join(root, 'web', 'dist', 'index.html'))) {
  throw new Error('缺少前端产物 web/dist/index.html，无法发布可启动的安装包。')
}

const { payloadHash, count } = await payloadFingerprint(root)
await writeFile(join(root, 'npm', 'release.json'), JSON.stringify({ version: metadata.version, payloadHash }, null, 2) + '\n')
console.log(`[code-agent] 安装包已构建：${metadata.version}（${count} 个后端与 Harness 源文件 + 预构建界面）`)
