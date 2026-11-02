import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { mkdir, readFile, writeFile, stat } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execute } from '../npm/launcher-service.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const installer = resolve(process.argv[2] || join(root, 'dist/installers/Code-agent-Setup-0.10.2-win-x64.exe'))
const output = resolve(process.argv[3] || join(root, '.tmp', `installer-verification-${Date.now()}`))
const installation = join(output, '应用 App')
const home = join(output, 'user-data')
const unrelated = join(output, 'unrelated-cwd')
await mkdir(unrelated, { recursive: true })
const report = { passed: false, installer, installation, home, phases: [] }
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(?:CODE_AGENT_|CH_AGENT_|OPENAI_|DEEPSEEK_|PYTHON|VIRTUAL_ENV|NODE_PATH|NODE_OPTIONS|npm_)/i.test(key)))
env.PATH = [join(process.env.SystemRoot, 'System32'), process.env.SystemRoot, join(process.env.SystemRoot, 'System32/WindowsPowerShell/v1.0')].join(';')
env.CODE_AGENT_HOME = home
env.CODE_AGENT_PROVIDER = 'offline'
env.CODE_AGENT_AUTH_ENABLED = 'false'
const sleep = ms => new Promise(done => setTimeout(done, ms))
const jsonFile = async path => JSON.parse((await readFile(path, 'utf8')).replace(/^\uFEFF/, ''))
async function waitFor(callback, label, timeout = 120000) {
  const deadline = Date.now() + timeout
  let error
  while (Date.now() < deadline) { try { const value = await callback(); if (value) return value } catch (failure) { error = failure }; await sleep(300) }
  throw new Error(`${label} timed out${error ? `: ${error.message}` : ''}`)
}
function phase(name, data = {}) { report.phases.push({ name, ...data }); console.log(JSON.stringify(report.phases.at(-1))) }
async function checked(command, args, options = {}) {
  const result = await execute(command, args, { cwd: unrelated, env, timeout: 300000, ...options })
  assert.equal(result.code, 0, result.stderr || result.stdout || `${command} failed`)
  return result
}
let controller, controllerOutput = '', controllerError = '', base, token
async function action(action, target) {
  const response = await fetch(`${base}/api/action`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Launcher-Token': token }, body: JSON.stringify({ action, target }) })
  assert.equal(response.status, 202, await response.clone().text())
  return waitFor(async () => {
    const state = await (await fetch(`${base}/api/status`)).json()
    if (state.operation?.state === 'running') return null
    assert.equal(state.operation?.state, 'succeeded', state.operation?.message)
    return state
  }, `${action} ${target}`)
}

try {
  const registryProbe = await execute('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', "if (Test-Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\CodeAgent') { exit 12 }"], { env, cwd: unrelated })
  assert.equal(registryProbe.code, 0, 'An existing installed Code-agent must not be overwritten by this verification')
  await checked(installer, ['--silent', '--dir', installation, '--no-launch', '--result-file', join(output, 'install-result.json')])
  const installed = await jsonFile(join(output, 'install-result.json'))
  assert.equal(installed.ok, true, JSON.stringify(installed))
  const manifest = await jsonFile(join(installation, 'installation-manifest.json'))
  assert.equal(manifest.appId, 'CodeAgent')
  assert.ok(manifest.files.length > 1000)
  phase('install', { result: installed, files: manifest.files.length })

  const python = join(installation, 'runtimes/python/python.exe')
  const node = join(installation, 'runtimes/node/node.exe')
  const pythonProbe = join(output, 'inspect-python.py')
  await writeFile(pythonProbe, `import json,sys\nimport code_agent.server,fastapi,winpty,docx,pptx,debugpy\nprint(json.dumps({"python":sys.executable,"paths":sys.path,"backend":code_agent.server.__file__,"fastapi":fastapi.__file__,"winpty":winpty.__file__,"office":docx.__file__}))\n`)
  const inspected = JSON.parse((await checked(python, ['-I', '-B', '-X', 'utf8', pythonProbe])).stdout.trim())
  assert.ok(inspected.backend.toLowerCase().startsWith(installation.toLowerCase()))
  assert.ok(inspected.paths.every(path => path.toLowerCase().startsWith(installation.toLowerCase())))
  await checked(node, [join(installation, 'runtimes/node/node_modules/npm/bin/npm-cli.js'), '--version'])
  await checked(join(installation, 'runtimes/git/cmd/git.exe'), ['--version'])
  phase('bundled-dependencies', { cleanPath: env.PATH, ...inspected })

  const handshake = join(output, 'controller.json'), nonce = randomBytes(24).toString('hex')
  controller = spawn(node, [join(installation, 'bin/code-agent-desktop-service.mjs'), '--root', installation,
    '--handshake-file', handshake, '--parent-pid', String(process.pid), '--nonce', nonce],
  { env, cwd: unrelated, windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] })
  controller.stdout.on('data', data => { controllerOutput = (controllerOutput + data).slice(-16000) })
  controller.stderr.on('data', data => { controllerError = (controllerError + data).slice(-16000) })
  const identity = await waitFor(async () => { if (controller.exitCode !== null) throw new Error(controllerError); const data = await jsonFile(handshake); return data.nonce === nonce ? data : null }, 'desktop handshake')
  base = identity.url
  token = /name="launcher-token" content="([a-f0-9]+)"/.exec(await (await fetch(base)).text())[1]
  let ready = await waitFor(async () => { const state = await (await fetch(`${base}/api/status`)).json(); if (state.operation?.state === 'failed') throw new Error(state.operation.message); return state.services.every(service => service.state === 'healthy') ? state : null }, 'automatic startup')
  assert.equal(ready.mode, 'bundled')
  assert.notEqual(ready.services[0].pid, ready.services[1].pid)
  phase('automatic-start', { services: ready.services.map(({id,url,pid,state}) => ({id,url,pid,state})), mode: ready.mode })

  const backend = ready.services.find(service => service.id === 'backend').url
  const frontend = ready.services.find(service => service.id === 'frontend').url
  assert.equal((await (await fetch(`${backend}/api/v1/settings/model`)).json()).provider, 'offline')
  assert.ok((await (await fetch(frontend)).text()).includes('Code-agent'))
  assert.equal((await (await fetch(`${frontend}healthz`)).json()).ok, true)
  const session = await (await fetch(`${backend}/api/v1/sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ permission_mode: 'workspace-write' }) })).json()
  assert.ok(session.id)
  const events = await new Promise((done, reject) => {
    const socket = new WebSocket(`${frontend.replace('http:', 'ws:')}api/v1/sessions/${session.id}/stream?after_seq=0`)
    const rows = [], timer = setTimeout(() => { socket.close(); reject(new Error('offline Harness task timed out')) }, 90000)
    let sent = false
    socket.onmessage = message => {
      const event = JSON.parse(String(message.data)); rows.push(event)
      if (event.type === 'session_meta' && !sent) { sent = true; socket.send(JSON.stringify({ type: 'prompt', text: '安装包离线验证', web: false })) }
      if (event.type === 'done') { clearTimeout(timer); socket.close(); done(rows) }
      if (event.type === 'error' || event.type === 'approval_request') { clearTimeout(timer); socket.close(); reject(new Error(`unexpected ${event.type}: ${JSON.stringify(event.payload)}`)) }
    }
    socket.onerror = error => { clearTimeout(timer); reject(error) }
  })
  assert.ok(events.some(event => event.type === 'tool_use'))
  assert.ok(events.some(event => event.type === 'tool_result'))
  assert.match(events.at(-1).payload.text, /hello-from-code-agent/)
  phase('offline-harness-task', { eventTypes: events.map(event => event.type), stopReason: events.at(-1).payload.stop_reason, externalModelCalls: 0 })

  const before = ready.services.map(service => service.pid)
  ready = await action('restart', 'backend')
  assert.notEqual(ready.services[0].pid, before[0]); assert.equal(ready.services[1].pid, before[1])
  ready = await action('restart', 'frontend')
  assert.notEqual(ready.services[1].pid, before[1])
  await checked(installer, ['--silent', '--dir', installation, '--no-launch', '--result-file', join(output, 'busy-upgrade.json')]).then(() => { throw new Error('busy upgrade must refuse') }, async error => {
    const refused = await jsonFile(join(output, 'busy-upgrade.json')); assert.equal(refused.ok, false); phase('busy-upgrade-preserved', { message: refused.error || refused.message })
  })
  ready = await action('stop', 'all')
  assert.ok(ready.services.every(service => service.state === 'stopped'))
  controller.stdin.write('shutdown\n')
  await waitFor(() => controller.exitCode !== null, 'controller close')
  controller = null; base = null
  phase('restart-and-stop')

  const guiReport = join(output, 'native.json')
  const gui = spawn(join(installation, 'Code-agent.exe'), ['--diagnostics-file', guiReport, '--diagnostics-exit-after-ms', '18000'],
    { cwd: unrelated, env, windowsHide: false, shell: false, stdio: 'ignore' })
  const nativeReady = await waitFor(async () => { const data = await jsonFile(guiReport); if (data.state === 'failed') throw new Error(data.error); return data.state === 'ready' ? data : null }, 'bundled WebView2 window')
  assert.equal(nativeReady.bundledRuntime, true)
  assert.ok(nativeReady.nodeExecutable.toLowerCase().startsWith(installation.toLowerCase()))
  assert.ok(nativeReady.webViewRuntimeDirectory.toLowerCase().startsWith(installation.toLowerCase()))
  await waitFor(() => gui.exitCode !== null, 'native diagnostic close', 90000)
  const ports = await jsonFile(join(home, 'desktop-services.json'))
  assert.equal(nativeReady.integratedWorkbench, true)
  assert.equal(nativeReady.workspaceWindows, 0)
  assert.equal(new URL(nativeReady.mainViewUrl).origin, new URL(nativeReady.workspaceUrl).origin)
  await assert.rejects(fetch(`http://127.0.0.1:${ports.backend}/healthz`, { signal: AbortSignal.timeout(1000) }))
  await assert.rejects(fetch(`http://127.0.0.1:${ports.frontend}/healthz`, { signal: AbortSignal.timeout(1000) }))
  phase('native-window', { ...nativeReady, ownedServicesStoppedOnClose: true })

  // Reopen the controller without auto-start for a final guarded service stop.
  const cleanupModule = join(output, 'stop-installed.mjs')
  await writeFile(cleanupModule, `import {LauncherServices} from ${JSON.stringify(new URL('../npm/launcher-service.mjs', import.meta.url).href)};const m=new LauncherServices({root:${JSON.stringify(installation)},mode:'bundled',home:${JSON.stringify(home)},env:process.env,backendPort:${ports.backend},frontendPort:${ports.frontend}});await m.initialize();await m.action('stop','all');\n`)
  await checked(node, [cleanupModule])
  await writeFile(join(home, 'keep-personal-data.txt'), 'must survive uninstall\n')
  await checked(installer, ['--silent', '--dir', installation, '--no-launch', '--result-file', join(output, 'upgrade-result.json')])
  assert.equal((await jsonFile(join(output, 'upgrade-result.json'))).ok, true)
  phase('upgrade')
  await checked(join(installation, 'Code-agent Uninstall.exe'), ['--silent', '--result-file', join(output, 'uninstall-result.json')])
  const uninstalled = await waitFor(async () => { const value = await jsonFile(join(output, 'uninstall-result.json')); return value }, 'uninstall completion')
  assert.equal(uninstalled.ok, true, JSON.stringify(uninstalled))
  assert.equal(await readFile(join(home, 'keep-personal-data.txt'), 'utf8'), 'must survive uninstall\n')
  await assert.rejects(stat(join(installation, 'Code-agent Launcher.exe')), { code: 'ENOENT' })
  phase('uninstall-data-preserved', { result: uninstalled })
  report.passed = true
} catch (error) {
  report.error = error.stack || String(error)
  throw error
} finally {
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n')
  await writeFile(join(output, 'controller-output.log'), controllerOutput + '\n' + controllerError)
  // On a failed verification leave installed files for diagnosis; never force-kill services.
  if (controller && base && token) await action('stop', 'all').catch(() => {})
  controller?.stdin.write('shutdown\n')
}
