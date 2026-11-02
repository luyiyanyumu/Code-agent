import assert from 'node:assert/strict'
import test from 'node:test'
import { EventEmitter } from 'node:events'
import { cp, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve, sep } from 'node:path'
import net from 'node:net'
import { bundledDataHome, loadBundledRuntime } from '../bundled-runtime.mjs'
import { LauncherServices } from '../launcher-service.mjs'
import { applyUpdate, checkUpdate, detectMode, installerReleasePage } from '../launcher-update.mjs'

const manifest = { schemaVersion: 1, version: '0.10.2', node: 'runtimes/node/node.exe', python: 'runtimes/python/python.exe', webview2: 'runtimes/webview2' }
const required = [manifest.node, manifest.python, `${manifest.webview2}/msedgewebview2.exe`,
  'web/dist/index.html', 'code_agent/server.py', 'code_agent/harness/node/runner.mjs',
  'scripts/backend_idle_check.py', 'npm/launcher-preview.mjs']

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'code agent bundled runtime '))
  t.after(async () => {
    assert.equal(dirname(directory), resolve(tmpdir()), 'cleanup must remain inside the named temporary directory')
    await rm(directory, { recursive: true, force: true })
  })
  const root = join(directory, '安装目录 & Code agent (完整版本)')
  await mkdir(root, { recursive: true })
  for (const path of required) {
    await mkdir(dirname(join(root, path)), { recursive: true })
    await writeFile(join(root, path), `bundled fixture ${path}`)
  }
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: manifest.version }))
  await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify(manifest))
  await writeFile(join(root, 'npm/release.json'), JSON.stringify({ version: manifest.version, payloadHash: 'a'.repeat(64) }))
  return { directory, root }
}

async function languageFixture(root, extra = {}) {
  const files=['runtimes/language-servers/tool/server.exe','runtimes/language-servers/tool/lib/stdlib.txt']
  for(const name of files){await mkdir(dirname(join(root,name)),{recursive:true});await writeFile(join(root,name),'verified tool fixture')}
  const server={id:'fixture',version:'1.2.3',command:files[0],args:[{path:dirname(files[1]),suffix:'/*'}],requiredFiles:[files[1]],
    env:{PATH:{paths:[dirname(files[0])],inherit:true},CACHE:{dataPath:'fixture/cache'}},...extra}
  await writeFile(join(root,'runtimes/language-tools.json'),JSON.stringify({schemaVersion:1,servers:[server]}))
  await writeFile(join(root,'desktop-runtime.json'),JSON.stringify({...manifest,languageTools:'runtimes/language-tools.json'}))
  return server
}

test('packaged language services validate relocatable executables, SDK files and private cache declarations',async t=>{
  const {root,directory}=await fixture(t)
  await languageFixture(root,{initializationOptions:{tsserver:{path:{path:'runtimes/language-servers/tool/server.exe'}}}})
  const original=await loadBundledRuntime(root)
  assert.deepEqual(original.languageTools.servers,['fixture'])
  assert.ok(original.languageTools.criticalFiles.includes('runtimes/language-servers/tool/lib/stdlib.txt'))
  const relocated=join(directory,'搬移后的安装目录')
  await cp(root,relocated,{recursive:true})
  const moved=await loadBundledRuntime(relocated)
  assert.equal(moved.languageTools.path,join(relocated,'runtimes/language-tools.json'))
  assert.equal(await statExists(join(relocated,'language-servers/fixture/cache')),false,'validation must not create cache directories')
})

async function statExists(path){try{await readFile(path);return true}catch(error){if(error.code==='ENOENT')return false;throw error}}

test('a declared server cannot pass with only its executable while its standard library is missing',async t=>{
  const {root}=await fixture(t)
  await languageFixture(root)
  await rm(join(root,'runtimes/language-servers/tool/lib/stdlib.txt'))
  await assert.rejects(loadBundledRuntime(root),/语言分析服务不完整/)
})

test('packaged language metadata rejects duplicate IDs and paths outside the runtime or cache boundary',async t=>{
  const {root}=await fixture(t)
  for(const extra of [
    {args:[{path:'../outside.exe'}]},
    {env:{CACHE:{dataPath:'../private'}}},
    {initializationOptions:{library:{path:'web/dist/index.html'}}},
    {env:{BROKEN:42}},
  ]){await languageFixture(root,extra);await assert.rejects(loadBundledRuntime(root),/语言分析服务不完整/)}
  const server=await languageFixture(root)
  await writeFile(join(root,'runtimes/language-tools.json'),JSON.stringify({schemaVersion:1,servers:[server,server]}))
  await assert.rejects(loadBundledRuntime(root),/重复/)
})

async function unusedPort() {
  const server = net.createServer()
  await new Promise((done, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', done) })
  const port = server.address().port
  await new Promise(done => server.close(done))
  return port
}

test('bundled manifest takes precedence over Git and uses explicit persistent data home', async t => {
  const { root, directory } = await fixture(t)
  await writeFile(join(root, '.git'), 'fixture: not a real checkout')
  assert.equal(await detectMode(root), 'bundled')
  assert.equal(bundledDataHome({ env: { LOCALAPPDATA: join(directory, 'Local Application Data') } }), join(directory, 'Local Application Data', 'Code-agent', 'data'))
  assert.equal(bundledDataHome({ env: { CODE_AGENT_HOME: join(directory, 'explicit home'), LOCALAPPDATA: join(directory, 'ignored') } }), join(directory, 'explicit home'))
  assert.equal(bundledDataHome({ home: join(directory, 'selected home'), env: { CODE_AGENT_HOME: 'ignored' } }), join(directory, 'selected home'))
})

test('runtime paths survive relocation and never retain the original project location', async t => {
  const { root, directory } = await fixture(t)
  const original = await loadBundledRuntime(root)
  const relocated = join(directory, '另一处安装目录 with spaces')
  await cp(root, relocated, { recursive: true })
  const actual = await loadBundledRuntime(relocated)
  for (const key of ['node', 'python', 'webview2']) {
    assert.ok(actual[key].startsWith(relocated + sep))
    assert.ok(!actual[key].startsWith(root + sep))
    assert.notEqual(actual[key], original[key])
  }
  assert.equal(actual.version, manifest.version)
})

test('invalid paths, mismatched version and incomplete runtime fail before any startup', async t => {
  const { root, directory } = await fixture(t)
  for (const replacement of [
    { node: '../outside/node.exe' }, { node: join(directory, 'outside.exe') },
    { python: 'C:\\build-machine\\python.exe' }, { webview2: '.' },
    { schemaVersion: 99 }, { version: '0.99.0' },
  ]) {
    await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify({ ...manifest, ...replacement }))
    await assert.rejects(loadBundledRuntime(root), /安装包/)
  }
  await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify(manifest))
  await rm(join(root, manifest.python))
  await assert.rejects(loadBundledRuntime(root), /Python/)
})

test('junctions cannot redirect runtime dependencies outside the install directory', async t => {
  const { root, directory } = await fixture(t)
  const external = join(directory, 'external runtime')
  await mkdir(external)
  await writeFile(join(external, 'node.exe'), 'outside executable')
  const localDirectory = join(root, 'runtimes', 'node')
  assert.ok(localDirectory.startsWith(root + sep))
  await rm(localDirectory, { recursive: true })
  await symlink(external, localDirectory, process.platform === 'win32' ? 'junction' : 'dir')
  await assert.rejects(loadBundledRuntime(root), /Node.js/)
})

test('bundled initialization does not invoke external tools and keeps data outside app files', async t => {
  const { root, directory } = await fixture(t)
  const env = { LOCALAPPDATA: join(directory, 'user application data'), CUSTOM_FLAG: 'preserved', Path: 'C:\\Windows\\System32' }
  const services = new LauncherServices({ root, mode: 'bundled', env, run() { assert.fail('bundled startup must not install dependencies') } })
  await services.initialize()
  assert.equal(services.python, join(root, manifest.python))
  assert.equal(services.home, join(directory, 'user application data', 'Code-agent', 'data'))
  assert.equal(services.env.CODE_AGENT_HOME, services.home)
  assert.equal(services.logDir, join(services.home, 'logs'))
  assert.equal(services.env.CODE_AGENT_NODE, join(root, manifest.node))
  assert.equal(services.env.CODE_AGENT_WEB_DIST, join(root, 'web', 'dist'))
  assert.equal(services.env.CODE_AGENT_HARNESS_NODE_DIR, join(root, 'code_agent', 'harness', 'node'))
  assert.equal(services.env.CUSTOM_FLAG, 'preserved')
  const pathEntries = services.env.PATH.split(process.platform === 'win32' ? ';' : ':')
  assert.deepEqual(pathEntries.slice(0, 2), [dirname(join(root, manifest.node)), dirname(join(root, manifest.python))])
  assert.equal(services.env.Path, undefined)
  assert.equal(env.Path, 'C:\\Windows\\System32')
  assert.equal(env.CODE_AGENT_HOME, undefined)
  assert.equal(await readFile(join(root, 'desktop-runtime.json'), 'utf8'), JSON.stringify(manifest))
})

test('bundled backend and frontend execute packaged runtimes with no pip, npm or PATH dependency', async t => {
  const { root, directory } = await fixture(t)
  const calls = []
  const services = new LauncherServices({ root, mode: 'bundled', home: join(directory, 'data'), env: { PATH: '' },
    backendPort: await unusedPort(), frontendPort: await unusedPort(),
    run() { assert.fail('no external install/build command may run') },
    spawnProcess(...args) {
      calls.push(args)
      const child = Object.assign(new EventEmitter(), { pid: 1234 + calls.length, exitCode: null, unref() {} })
      queueMicrotask(() => child.emit('spawn'))
      return child
    },
  })
  await services.initialize()
  services.status = async () => services.services.map(service => ({ ...service, state: 'stopped' }))
  services.probe = async () => ({ healthy: true })
  await services.start('backend')
  await services.start('frontend')
  assert.equal(calls[0][0], join(root, manifest.python))
  assert.deepEqual(calls[0][1], ['-I', '-B', '-X', 'utf8', '-m', 'code_agent.server', '--host', '127.0.0.1', '--port', String(services.services[0].port)])
  assert.equal(calls[0][2].env.CODE_AGENT_HOME, services.home)
  assert.equal(calls[0][2].env.CODE_AGENT_NODE, join(root, manifest.node))
  assert.equal(calls[1][0], join(root, manifest.node))
  assert.deepEqual(calls[1][1], [join(root, 'npm', 'launcher-preview.mjs'), '--port', String(services.services[1].port), '--backend-port', String(services.services[0].port)])
  assert.ok(calls.every(([, , options]) => options.cwd === root && options.shell === false && options.windowsHide && options.detached))
})

test('bundled idle guard reads the correct persistent database through packaged Python', async t => {
  const { root, directory } = await fixture(t)
  const calls = []
  const services = new LauncherServices({ root, mode: 'bundled', home: join(directory, 'data'), env: {}, run: async (...args) => { calls.push(args); return { code: 0 } } })
  await services.initialize()
  await services.assertIdle()
  assert.equal(calls[0][0], join(root, manifest.python))
  assert.deepEqual(calls[0][1], ['-I', '-B', '-X', 'utf8', join(root, 'scripts', 'backend_idle_check.py'), '--base-url', services.services[0].url, '--db', join(services.home, 'code_agent.db')])
})

test('optional bundled Git is validated and made available to project tool shells', async t => {
  const { root, directory } = await fixture(t)
  const git = 'runtimes/git/cmd/git.exe'
  await mkdir(dirname(join(root, git)), { recursive: true })
  await writeFile(join(root, git), 'packaged git')
  await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify({ ...manifest, git }))
  assert.equal((await loadBundledRuntime(root)).git, join(root, git))
  const services = new LauncherServices({ root, mode: 'bundled', env: { LOCALAPPDATA: join(directory, 'user data'), Path: 'C:\\Windows\\System32' } })
  await services.initialize()
  assert.deepEqual(services.env.PATH.split(process.platform === 'win32' ? ';' : ':').slice(0, 3), [dirname(join(root, manifest.node)), dirname(join(root, manifest.python)), dirname(join(root, git))])
  assert.equal(services.env.PYTHONDONTWRITEBYTECODE, '1')
  await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify({ ...manifest, git: '../external/git.exe' }))
  await assert.rejects(loadBundledRuntime(root), /Git/)
})

const javaManifest = { java: 'runtimes/java/bin/java.exe', jdtls: 'runtimes/jdtls', lombok: 'runtimes/lombok/lombok.jar' }
async function addJava(root) {
  for (const path of [javaManifest.java, javaManifest.lombok, 'runtimes/java/bin/javac.exe', 'runtimes/java/release',
    'runtimes/jdtls/bin/jdtls', 'runtimes/jdtls/bin/jdtls.py', 'runtimes/jdtls/config_win/config.ini',
    'runtimes/jdtls/plugins/org.eclipse.equinox.launcher_fixture.jar']) {
    await mkdir(dirname(join(root, path)), { recursive: true })
    await writeFile(join(root, path), 'fixture tool')
  }
  await mkdir(join(root, 'runtimes/java/legal'), { recursive: true })
  await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify({ ...manifest, ...javaManifest }))
}

test('optional Java analysis tools are complete and relocate without changing system Java', async t => {
  const { root, directory } = await fixture(t)
  await addJava(root)
  const relocated = join(directory, '移动后的 Java 工作台')
  await cp(root, relocated, { recursive: true })
  const runtime = await loadBundledRuntime(relocated)
  for (const name of ['java', 'jdtls', 'lombok']) assert.equal(runtime[name], join(relocated, javaManifest[name]))
  const env = { JAVA_HOME: 'user-selected-jdk', PATH: 'user-path', LOCALAPPDATA: join(directory, 'user-data') }
  const services = new LauncherServices({ root: relocated, mode: 'bundled', env })
  await services.initialize()
  assert.equal(services.env.JAVA_HOME, 'user-selected-jdk')
  assert.equal(env.PATH, 'user-path')
})

test('incomplete Java tools and missing service engine fail before starting any process', async t => {
  const { root } = await fixture(t)
  await addJava(root)
  for (const replacement of [{ java: undefined }, { jdtls: undefined }, { lombok: undefined },
    { java: '../outside/java.exe' }, { jdtls: 'C:\\external\\jdtls' }, { lombok: 'runtimes/lombok/missing.jar' }]) {
    await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify({ ...manifest, ...javaManifest, ...replacement }))
    await assert.rejects(loadBundledRuntime(root), /安装包/)
  }
  await writeFile(join(root, 'desktop-runtime.json'), JSON.stringify({ ...manifest, ...javaManifest }))
  await rm(join(root, 'runtimes/jdtls/plugins/org.eclipse.equinox.launcher_fixture.jar'))
  await assert.rejects(loadBundledRuntime(root), /服务引擎/)
})

test('Java dependency junctions cannot point outside the installed application', async t => {
  const { root, directory } = await fixture(t)
  await addJava(root)
  const external = join(directory, 'outside-java')
  await cp(join(root, 'runtimes/java'), external, { recursive: true })
  await rm(join(root, 'runtimes/java'), { recursive: true })
  await symlink(external, join(root, 'runtimes/java'), process.platform === 'win32' ? 'junction' : 'dir')
  await assert.rejects(loadBundledRuntime(root), /Java/)
})

test('bundled update checks official installer releases instead of npm or git', async t => {
  const { root } = await fixture(t)
  let request
  const report = await checkUpdate({ root, run() { assert.fail('bundled checking must not use git/npm') }, fetchImpl: async (...args) => {
    request = args
    return { ok: true, json: async () => ({ tag_name: 'v0.10.3', assets: [{ name: 'Code-agent-Setup-0.10.3.exe', browser_download_url: 'https://github.com/luyiyanyumu/Code-agent/releases/download/v0.10.3/Code-agent-Setup-0.10.3.exe' }] }) }
  } })
  assert.equal(request[0], 'https://api.github.com/repos/luyiyanyumu/Code-agent/releases/latest')
  assert.equal(request[1].redirect, 'error')
  assert.equal(report.mode, 'bundled')
  assert.equal(report.available, true)
  assert.equal(report.latest, '0.10.3')
  assert.equal(report.downloadPage, installerReleasePage)
  assert.match(report.downloadUrl, /^https:\/\/github.com\/luyiyanyumu\/Code-agent\/releases\/download\//)
})

test('missing releases, failed checks and foreign installer URLs do not invent an update', async t => {
  const { root } = await fixture(t)
  const absent = await checkUpdate({ root, fetchImpl: async () => ({ ok: false, status: 404 }) })
  assert.equal(absent.available, false)
  assert.equal(absent.error, null)
  assert.match(absent.message, /尚未发现/)
  const failed = await checkUpdate({ root, fetchImpl: async () => ({ ok: false, status: 403 }) })
  assert.match(failed.error, /暂不可用/)
  const foreign = await checkUpdate({ root, fetchImpl: async () => ({ ok: true, json: async () => ({ tag_name: 'v99.0.0', assets: [{ name: 'Setup.exe', browser_download_url: 'https://untrusted.invalid/Setup.exe' }] }) }) })
  assert.equal(foreign.available, false)
  assert.equal(foreign.downloadUrl, undefined)
})

test('bundled update action preserves runtimes and does not require npm, Python or Git installers', async t => {
  const { root } = await fixture(t)
  const result = await applyUpdate({ root, env: { PATH: '', CODE_AGENT_UPDATE_PACKAGE: 'untrusted@latest' }, run() { assert.fail('no installer subprocess may be invoked') } })
  assert.equal(result.updated, false)
  assert.equal(result.staged, false)
  assert.equal(result.downloadPage, installerReleasePage)
  assert.match(result.message, /新版安装包/)
  for (const path of required) assert.match(await readFile(join(root, path), 'utf8'), /bundled fixture/)
})
