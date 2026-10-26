import assert from 'node:assert/strict'
import test from 'node:test'
import { useProjectAccess } from '../src/composables/useProjectAccess.js'
import { api } from '../src/api.js'

const a = { id: 'a', name: 'Alpha', root_path: 'E:/alpha', available: true }
const b = { id: 'b', name: 'Beta', root_path: 'E:/beta', available: true }
const missing = { id: 'missing', name: 'Missing', root_path: 'E:/gone', available: false }
const capability = { native_picker: { available: true, platform: 'win32' }, templates: ['empty'] }
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settle = () => new Promise(setImmediate)
function memoryStorage(values = {}) {
  const data = new Map(Object.entries(values)), writes = []
  return { data, writes, getItem: key => data.has(key) ? data.get(key) : null, setItem(key, value) { data.set(key, value); writes.push([key, value]) }, removeItem(key) { data.delete(key); writes.push([key, null]) } }
}
function setup(t, options = {}) {
  const storage = Object.hasOwn(options, 'storage') ? options.storage : memoryStorage()
  const calls = [], activated = [], closed = []
  const methods = {
    listProjects: async () => [a, b], meta: async () => capability,
    project: async id => ({ ...([a, b, missing].find(project => project.id === id) || { id, available: true }) }),
    pickFolder: async () => ({ path: b.root_path, cancelled: false }),
    openProject: async path => ({ ...b, root_path: path }),
    createProject: async (name, template, gitInit, location) => ({ ...b, name, template, gitInit, ...(location ? { root_path: `${location}/${name}` } : {}) }),
    removeProject: async () => ({}), ...options.client,
  }
  const client = Object.fromEntries(Object.keys(methods).map(name => [name, (...args) => { calls.push([name, ...args]); return methods[name](...args) }]))
  const access = useProjectAccess({ storageKey: options.storageKey || 'project-test', storage, client,
    onActivate: async project => { activated.push(project.id); return options.onActivate?.(project) },
    onClose: () => { closed.push(true); return options.onClose?.() },
  })
  t.after(access.dispose)
  return { w: access, storage, client: methods, calls, activated, closed }
}
const closedStorage = () => memoryStorage({ 'project-test': '' })

test('initialization restores remembered available project and persists only after activation succeeds', async t => {
  const ready = deferred(), h = setup(t, { storage: memoryStorage({ 'project-test': 'b' }), client: { listProjects: async () => [missing, a, b] }, onActivate: () => ready.promise })
  const pending = h.w.init(); await settle()
  assert.deepEqual(h.activated, ['b']); assert.equal(h.w.activeId.value, ''); assert.equal(h.w.phase.value, 'switching'); assert.equal(h.storage.writes.length, 0)
  ready.resolve(); assert.equal(await pending, true); assert.equal(h.w.activeId.value, 'b'); assert.equal(h.storage.data.get('project-test'), 'b'); assert.equal(h.w.busy.value, false)
})

test('absent preference restores first available; explicit closed preference restores nothing', async t => {
  const legacy = setup(t, { client: { listProjects: async () => [missing, b, a] } }); await legacy.w.init()
  assert.deepEqual(legacy.activated, ['b'])
  const closed = setup(t, { storage: closedStorage() }); await closed.w.init()
  assert.deepEqual(closed.activated, []); assert.equal(closed.w.activeId.value, ''); assert.equal(closed.w.projects.value.length, 2)
})

test('stored unavailable or removed identity cannot activate a different workspace implicitly', async t => {
  const h = setup(t, { storage: memoryStorage({ 'project-test': 'missing' }), client: { listProjects: async () => [missing, a] } })
  assert.equal(await h.w.init(), false); assert.deepEqual(h.activated, []); assert.match(h.w.error.value, /上次工作区.*不可用/)
  assert.equal(await h.w.activate(missing), false); assert.deepEqual(h.activated, []); assert.equal(h.w.activeId.value, '')
  await h.w.activate(a); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.storage.data.get('project-test'), 'a')
})

test('explicit unavailable selection preserves current workspace and preference', async t => {
  const h = setup(t); await h.w.init(); const writes = h.storage.writes.length
  assert.equal(await h.w.activate(missing), false); assert.equal(h.w.activeId.value, 'a'); assert.deepEqual(h.activated, ['a']); assert.equal(h.storage.writes.length, writes)
  assert.match(h.w.error.value, /目录当前不可用/); assert.equal(h.w.phase.value, 'idle')
})

test('cached available recent project must pass fresh availability/404 validation before replacing current workspace', async t => {
  for (const failure of ['unavailable', 'not-found']) {
    const h = setup(t); await h.w.init()
    const writes = h.storage.writes.length, before = h.w.projects.value.map(project => project.id)
    h.client.project = async id => {
      assert.equal(id, 'b')
      if (failure === 'not-found') throw Object.assign(new Error('project 404 not found'), { status: 404 })
      return { ...b, available: false }
    }
    assert.equal(await h.w.activate(b), false)
    assert.equal(h.w.activeId.value, 'a'); assert.deepEqual(h.activated, ['a']); assert.equal(h.closed.length, 0)
    assert.deepEqual(h.w.projects.value.map(project => project.id), before); assert.equal(h.storage.writes.length, writes); assert.equal(h.w.phase.value, 'idle')
    assert.match(h.w.error.value, failure === 'unavailable' ? /目录当前不可用/ : /404/)
  }
  const restore = setup(t, { storage: memoryStorage({ 'project-test': 'b' }), client: { project: async () => ({ ...b, available: false }) } })
  assert.equal(await restore.w.init(), false); assert.deepEqual(restore.activated, []); assert.equal(restore.w.activeId.value, ''); assert.equal(restore.storage.writes.length, 0)
})

test('late fresh validation cannot replace workspace, update recents, error or preference after close/another activation', async t => {
  for (const action of ['close', 'activate']) {
    const probed = deferred(), h = setup(t); await h.w.init()
    h.client.project = id => id === 'b' ? probed.promise : Promise.resolve({ ...a })
    const pending = h.w.activate(b); await settle(); assert.equal(h.w.phase.value, 'switching'); assert.deepEqual(h.activated, ['a'])
    if (action === 'close') h.w.close()
    else assert.equal(await h.w.activate('a'), true)
    const writes = h.storage.writes.length
    probed.resolve({ ...b, name: 'Late Beta', git_branch: 'late' }); assert.equal(await pending, false)
    assert.equal(h.w.activeId.value, action === 'close' ? '' : 'a'); assert.deepEqual(h.activated, ['a']); assert.equal(h.storage.writes.length, writes)
    assert.equal(h.w.projects.value.find(project => project.id === 'b').name, 'Beta'); assert.equal(h.w.error.value, ''); assert.equal(h.w.phase.value, 'idle')
  }
  const probed = deferred(), restore = setup(t, { client: { project: () => probed.promise } })
  const pending = restore.w.init(); await settle(); restore.w.close(); probed.reject(new Error('late restore 404'))
  assert.equal(await pending, false); assert.deepEqual(restore.activated, []); assert.equal(restore.w.error.value, ''); assert.equal(restore.storage.data.get('project-test'), '')
})

test('fresh detail identity must match requested project before activation or metadata update', async t => {
  const h = setup(t); await h.w.init(); h.client.project = async () => ({ ...a, name: 'Wrong identity' })
  assert.equal(await h.w.activate(b), false); assert.equal(h.w.activeId.value, 'a'); assert.deepEqual(h.activated, ['a'])
  assert.equal(h.w.projects.value[0].name, 'Alpha'); assert.equal(h.storage.data.get('project-test'), 'a'); assert.match(h.w.error.value, /不匹配/)
})

test('fresh recent metadata is delivered once and MRU promotion waits for accepted latest activation', async t => {
  const ready = deferred(); let betaDecision = ready.promise; const seen = []
  const h = setup(t, { onActivate: project => { seen.push(project); return project.id === 'b' ? betaDecision : undefined } }); await h.w.init()
  h.client.project = async id => ({ ...(id === 'a' ? a : b), git_branch: 'fresh-main', git_dirty: true })
  const pending = h.w.activate(b); await settle()
  assert.equal(seen.at(-1).git_branch, 'fresh-main'); assert.equal(h.w.projects.value[1].git_dirty, true)
  assert.deepEqual(h.w.projects.value.map(project => project.id), ['a', 'b']); assert.equal(h.w.activeId.value, 'a')
  ready.resolve(false); assert.equal(await pending, false); assert.deepEqual(h.w.projects.value.map(project => project.id), ['a', 'b']); assert.equal(h.storage.data.get('project-test'), 'a')
  betaDecision = true; assert.equal(await h.w.activate(b), true)
  assert.deepEqual(h.w.projects.value.map(project => project.id), ['b', 'a']); assert.equal(h.w.projects.value[0].git_branch, 'fresh-main'); assert.equal(h.storage.data.get('project-test'), 'b')
  assert.equal(h.calls.filter(([name, id]) => name === 'project' && id === 'b').length, 2)
})

test('recent restoration/selection probes once each, while successful native/path/create use fresh registration without extra detail request', async t => {
  const h = setup(t); await h.w.init()
  assert.deepEqual(h.calls.filter(([name]) => name === 'project'), [['project', 'a']])
  await h.w.activate(b); assert.deepEqual(h.calls.filter(([name]) => name === 'project'), [['project', 'a'], ['project', 'b']])
  await h.w.openPath('E:/fresh-path'); await h.w.openFolder(); await h.w.createProject({ name: 'Fresh Create' })
  assert.equal(h.calls.filter(([name]) => name === 'project').length, 2)
  assert.equal(h.calls.filter(([name]) => name === 'openProject').length, 2); assert.equal(h.calls.filter(([name]) => name === 'createProject').length, 1)
  assert.equal(h.w.activeId.value, 'b'); assert.equal(h.w.projects.value[0].name, 'Fresh Create')
})

test('native cancellation retains current workspace/recent list, creates no registration and does not close', async t => {
  const h = setup(t, { client: { pickFolder: async () => ({ cancelled: true, path: '' }) } }); await h.w.init()
  const before = h.storage.writes.length
  assert.equal(await h.w.openFolder(' E:/start '), false)
  assert.deepEqual(h.calls.find(([name]) => name === 'pickFolder'), ['pickFolder', 'E:/start'])
  assert.equal(h.calls.some(([name]) => name === 'openProject'), false); assert.deepEqual(h.activated, ['a']); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.storage.writes.length, before); assert.equal(h.closed.length, 0); assert.equal(h.w.error.value, '')
})

test('native flow stays busy through selecting, registration and activation; duplicate opens/create are blocked', async t => {
  const picked = deferred(), registered = deferred(), activated = deferred()
  const h = setup(t, { storage: closedStorage(), client: { pickFolder: () => picked.promise, openProject: () => registered.promise }, onActivate: () => activated.promise }); await h.w.init()
  const pending = h.w.openFolder(); assert.equal(h.w.phase.value, 'selecting')
  assert.equal(await h.w.openFolder(), false); assert.equal(await h.w.openPath(a.root_path), false); assert.equal(await h.w.createProject({ name: 'blocked' }), false)
  picked.resolve({ path: ' E:/new ', cancelled: false }); await settle(); assert.equal(h.w.phase.value, 'opening'); assert.equal(h.w.busy.value, true)
  registered.resolve({ ...b, root_path: 'E:/new' }); await settle(); assert.deepEqual(h.activated, ['b']); assert.equal(h.w.activeId.value, '')
  activated.resolve(); assert.equal(await pending, true); assert.equal(h.w.activeId.value, 'b'); assert.equal(h.w.phase.value, 'idle')
  assert.equal(h.calls.filter(([name]) => name === 'pickFolder').length, 1); assert.equal(h.calls.filter(([name]) => name === 'openProject').length, 1)
})

test('successful registration upserts/activates immediately without re-listing, even if list service fails', async t => {
  const h = setup(t, { storage: closedStorage(), client: { listProjects: async () => { throw new Error('list offline') } } }); await h.w.init()
  assert.equal(await h.w.openPath(' E:/new-path '), true); assert.equal(h.w.activeId.value, 'b'); assert.equal(h.w.projects.value[0].root_path, 'E:/new-path')
  assert.equal(h.calls.filter(([name]) => name === 'listProjects').length, 1); assert.equal(h.w.error.value, '')
  await h.w.createProject({ name: ' Demo ', template: 'python', gitInit: false })
  assert.deepEqual(h.calls.at(-1), ['createProject', 'Demo', 'python', false]); assert.equal(h.w.projects.value.length, 1)
})

test('create passes trimmed optional parent location and keeps legacy three-argument calls for omitted/blank location', async t => {
  const h = setup(t, { storage: closedStorage() }); await h.w.init()
  assert.equal(await h.w.createProject({ name: ' Custom ', template: 'node', gitInit: false, location: ' D:/工作区/团队 ' }), true)
  assert.deepEqual(h.calls.at(-1), ['createProject', 'Custom', 'node', false, 'D:/工作区/团队'])
  assert.equal(h.w.projects.value[0].root_path, 'D:/工作区/团队/Custom'); assert.equal(h.w.activeId.value, 'b')
  await h.w.createProject({ name: 'Default', template: 'empty', gitInit: true })
  assert.deepEqual(h.calls.at(-1), ['createProject', 'Default', 'empty', true])
  await h.w.createProject({ name: 'Blank', location: '   ' })
  assert.deepEqual(h.calls.at(-1), ['createProject', 'Blank', 'empty', true])
})

test('create API transmits an explicit parent location and omits empty location so backend default remains available', async t => {
  const oldFetch = globalThis.fetch, oldStorage = globalThis.localStorage, calls = []
  t.after(() => { globalThis.fetch = oldFetch; globalThis.localStorage = oldStorage })
  globalThis.localStorage = { getItem: () => null }
  globalThis.fetch = async (path, options) => { calls.push({ path, ...options }); return { ok: true, status: 200, text: async () => JSON.stringify(b) } }
  await api.code.createProject('Custom', 'python', false, ' D:/Projects ')
  await api.code.createProject('Default'); await api.code.createProject('Blank', 'node', true, '  ')
  assert.deepEqual(calls.map(call => [call.method, call.path]), Array(3).fill(['POST', '/api/v1/code/projects']))
  assert.deepEqual(JSON.parse(calls[0].body), { mode: 'create', name: 'Custom', template: 'python', git_init: false, location: 'D:/Projects' })
  assert.deepEqual(JSON.parse(calls[1].body), { mode: 'create', name: 'Default', template: 'empty', git_init: true })
  assert.deepEqual(JSON.parse(calls[2].body), { mode: 'create', name: 'Blank', template: 'node', git_init: true })
})

test('selecting a creation location only returns parent path without registering, activating or changing workspace preference/recents', async t => {
  const h = setup(t, { client: { pickFolder: async () => ({ path: ' D:/Parents ', cancelled: false }) } }); await h.w.init()
  const calls = h.calls.length, writes = h.storage.writes.length, before = h.w.projects.value.map(project => ({ ...project }))
  assert.equal(await h.w.pickLocation(' E:/CurrentParent '), 'D:/Parents')
  assert.deepEqual(h.calls.slice(calls), [['pickFolder', 'E:/CurrentParent']]); assert.deepEqual(h.activated, ['a']); assert.equal(h.closed.length, 0)
  assert.equal(h.w.activeId.value, 'a'); assert.deepEqual(h.w.projects.value, before); assert.equal(h.storage.writes.length, writes); assert.equal(h.w.phase.value, 'idle')
})

test('location cancellation returns null without clearing active workspace or entering a registration flow', async t => {
  const h = setup(t, { client: { pickFolder: async () => ({ path: '', cancelled: true }) } }); await h.w.init()
  const calls = h.calls.length, writes = h.storage.writes.length
  assert.equal(await h.w.pickLocation('D:/ExistingInput'), null); assert.deepEqual(h.calls.slice(calls), [['pickFolder', 'D:/ExistingInput']])
  assert.equal(h.w.activeId.value, 'a'); assert.deepEqual(h.activated, ['a']); assert.equal(h.storage.writes.length, writes); assert.equal(h.w.error.value, ''); assert.equal(h.w.projects.value.length, 2)
})

test('location picker blocks duplicate selecting/open/create until selection completes', async t => {
  const picked = deferred(), h = setup(t, { client: { pickFolder: () => picked.promise } }); await h.w.init()
  const calls = h.calls.length, pending = h.w.pickLocation(); assert.equal(h.w.phase.value, 'selecting'); assert.equal(h.w.busy.value, true)
  assert.equal(await h.w.pickLocation(), null); assert.equal(await h.w.openFolder(), false); assert.equal(await h.w.openPath('D:/Other'), false); assert.equal(await h.w.createProject({ name: 'Blocked' }), false)
  assert.deepEqual(h.calls.slice(calls), [['pickFolder', '']]); picked.resolve({ path: 'D:/Chosen', cancelled: false })
  assert.equal(await pending, 'D:/Chosen'); assert.equal(h.w.phase.value, 'idle'); assert.equal(h.w.busy.value, false)
})

test('unavailable/failing location picker returns null and useful error, then a successful retry clears that error', async t => {
  const h = setup(t, { client: { meta: async () => ({ native_picker: { available: false, reason: 'Remote picker unavailable' } }) } }); await h.w.init()
  assert.equal(await h.w.pickLocation(), null); assert.equal(h.calls.some(([name]) => name === 'pickFolder'), false); assert.equal(h.w.error.value, 'Remote picker unavailable')
  h.client.meta = async () => capability; await h.w.refresh()
  h.client.pickFolder = async () => { throw new Error('native dialog failed') }
  assert.equal(await h.w.pickLocation(), null); assert.equal(h.w.error.value, 'native dialog failed'); assert.equal(h.w.phase.value, 'idle'); assert.equal(h.w.activeId.value, 'a')
  h.client.pickFolder = async () => ({ path: 'D:/Retry', cancelled: false })
  assert.equal(await h.w.pickLocation(), 'D:/Retry'); assert.equal(h.w.error.value, ''); assert.deepEqual(h.activated, ['a'])
})

test('late location selection is ignored after activate, close, a new project flow or disposal', async t => {
  for (const action of ['activate', 'close', 'new-project', 'dispose']) {
    const picked = deferred(), h = setup(t, { client: { pickFolder: () => picked.promise } }); await h.w.init()
    const pending = h.w.pickLocation(); await settle()
    if (action === 'activate') await h.w.activate(b)
    else if (action === 'close') h.w.close()
    else if (action === 'new-project') { h.w.deferRestore(); await h.w.createProject({ name: 'Newer', location: 'D:/NewerParent' }) }
    else h.w.dispose()
    const active = h.w.activeId.value, phase = h.w.phase.value, writes = h.storage.writes.length, recent = h.w.projects.value.map(project => ({ ...project })), activated = [...h.activated]
    if (action === 'close') picked.reject(new Error('obsolete picker failure'))
    else picked.resolve({ path: 'D:/Obsolete', cancelled: false })
    assert.equal(await pending, null); assert.equal(h.w.activeId.value, active); assert.equal(h.w.phase.value, phase); assert.deepEqual(h.activated, activated)
    assert.deepEqual(h.w.projects.value, recent); assert.equal(h.storage.writes.length, writes); assert.equal(h.w.error.value, '')
  }
})

test('explicit activate supersedes a pending native result before it can register another folder', async t => {
  const picked = deferred(), h = setup(t, { client: { pickFolder: () => picked.promise } }); await h.w.init()
  const pending = h.w.openFolder(); await h.w.activate(b)
  picked.resolve({ path: 'E:/late-native', cancelled: false }); assert.equal(await pending, false)
  assert.equal(h.calls.some(([name]) => name === 'openProject'), false); assert.deepEqual(h.activated, ['a', 'b']); assert.equal(h.w.activeId.value, 'b'); assert.equal(h.storage.data.get('project-test'), 'b')
})

test('same-active selection invalidates pending registration without resetting drafts or callback', async t => {
  const registered = deferred(), h = setup(t, { client: { openProject: () => registered.promise } }); await h.w.init()
  const pending = h.w.openPath(b.root_path); assert.equal(await h.w.activate('a'), true)
  registered.resolve({ id: 'late', root_path: 'E:/late', available: true }); assert.equal(await pending, false)
  assert.deepEqual(h.activated, ['a']); assert.equal(h.w.projects.value.some(project => project.id === 'late'), false); assert.equal(h.w.activeId.value, 'a')
})

test('returning to committed project while another activation is pending restores rendered workspace and invalidates late setup', async t => {
  const betaReady = deferred(); let rendered = '', version = 0
  const h = setup(t, { onActivate: async project => {
    const ownVersion = ++version; rendered = project.id
    if (project.id === 'b') await betaReady.promise
    return ownVersion === version
  } }); await h.w.init()
  const pending = h.w.activate(b); await settle(); assert.equal(rendered, 'b'); assert.equal(h.w.activeId.value, 'a')
  assert.equal(await h.w.activate('a'), true); assert.equal(rendered, 'a'); assert.deepEqual(h.activated, ['a', 'b', 'a'])
  betaReady.resolve(); assert.equal(await pending, false)
  assert.equal(h.w.activeId.value, 'a'); assert.equal(h.storage.data.get('project-test'), 'a'); assert.equal(h.w.phase.value, 'idle'); assert.equal(h.w.error.value, '')
})

test('close cancels pending creation and stores a closed marker without deleting recents', async t => {
  const created = deferred(), h = setup(t, { client: { createProject: () => created.promise } }); await h.w.init()
  const pending = h.w.createProject({ name: 'late' }); assert.equal(h.w.close(), true)
  created.resolve({ id: 'late', available: true }); assert.equal(await pending, false)
  assert.equal(h.w.activeId.value, ''); assert.equal(h.storage.data.get('project-test'), ''); assert.equal(h.w.projects.value.length, 2); assert.equal(h.closed.length, 1)
  const reloaded = setup(t, { storage: h.storage }); await reloaded.w.init(); assert.deepEqual(reloaded.activated, []); assert.equal(reloaded.w.activeId.value, '')
})

test('close during onActivate keeps registered project recent but rejects late activation/preference writes', async t => {
  const ready = deferred(), h = setup(t, { onActivate: project => project.id === 'b' ? ready.promise : undefined }); await h.w.init()
  const pending = h.w.openPath(b.root_path); await settle(); assert.deepEqual(h.activated, ['a', 'b']); assert.equal(h.w.activeId.value, 'a')
  h.w.close(); ready.resolve(); assert.equal(await pending, false)
  assert.equal(h.w.activeId.value, ''); assert.equal(h.storage.data.get('project-test'), ''); assert.equal(h.w.projects.value.some(project => project.id === 'b'), true)
})

test('out-of-order activation callback cannot overwrite a newer pending phase, ID, error or preference', async t => {
  const first = deferred(), second = deferred(), h = setup(t, { storage: closedStorage(), onActivate: project => project.id === 'a' ? first.promise : second.promise }); await h.w.init()
  const one = h.w.activate(a); await settle(); const two = h.w.activate(b); await settle()
  first.reject(new Error('old activation failed')); assert.equal(await one, false); assert.equal(h.w.phase.value, 'switching'); assert.equal(h.w.error.value, ''); assert.equal(h.w.activeId.value, '')
  second.resolve(); assert.equal(await two, true); assert.equal(h.w.activeId.value, 'b'); assert.equal(h.storage.data.get('project-test'), 'b'); assert.equal(h.w.phase.value, 'idle')
})

test('callback veto preserves current active preference and registration failure retains list', async t => {
  const h = setup(t, { onActivate: project => project.id === 'b' ? false : undefined }); await h.w.init()
  assert.equal(await h.w.openPath(b.root_path), false); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.storage.data.get('project-test'), 'a'); assert.match(h.w.error.value, /未能打开/)
  h.client.openProject = async () => { throw new Error('folder denied') }
  const before = h.w.projects.value.map(project => project.id); assert.equal(await h.w.openPath('E:/denied'), false)
  assert.deepEqual(h.w.projects.value.map(project => project.id), before); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.w.error.value, 'folder denied')
})

test('refresh never switches or closes current workspace, and independent list/meta failures preserve successful data', async t => {
  const h = setup(t); await h.w.init()
  h.client.listProjects = async () => [b]; h.client.meta = async () => { throw new Error('meta offline') }
  assert.equal(await h.w.refresh(), true); assert.deepEqual(h.w.projects.value.map(project => project.id), ['b']); assert.equal(h.w.activeId.value, 'a'); assert.deepEqual(h.activated, ['a']); assert.equal(h.closed.length, 0); assert.equal(h.w.nativeAvailable.value, true); assert.match(h.w.error.value, /meta offline/)
  h.client.listProjects = async () => { throw new Error('list offline') }; h.client.meta = async () => ({ native_picker: { available: false, reason: 'remote host' } })
  assert.equal(await h.w.refresh(), true); assert.deepEqual(h.w.projects.value.map(project => project.id), ['b']); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.w.nativeAvailable.value, false); assert.match(h.w.error.value, /list offline/)
  assert.equal(await h.w.openFolder(), false); assert.equal(h.w.error.value, 'remote host'); assert.equal(h.w.phase.value, 'idle')
})

test('slow metadata cannot block list restoration; slow list cannot hide a ready native capability', async t => {
  const metaWait = deferred(), h = setup(t, { client: { meta: () => metaWait.promise } })
  assert.equal(await h.w.init(), true); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.w.loading.value, true)
  metaWait.reject(new Error('late meta failure')); await settle(); assert.equal(h.w.loading.value, false); assert.match(h.w.error.value, /late meta failure/)
  const listWait = deferred(), other = setup(t, { storage: closedStorage(), client: { listProjects: () => listWait.promise } })
  const pending = other.w.init(); await settle(); assert.equal(other.w.nativeAvailable.value, true); assert.equal(other.w.projects.value.length, 0)
  listWait.resolve([a]); await pending; assert.equal(other.w.projects.value.length, 1); assert.deepEqual(other.activated, [])
})

test('a background read completing successfully cannot erase a real activation failure', async t => {
  const metaWait = deferred(), h = setup(t, { client: { meta: () => metaWait.promise }, onActivate: () => { throw new Error('workspace restore failed') } })
  assert.equal(await h.w.init(), false); assert.equal(h.w.error.value, 'workspace restore failed')
  metaWait.resolve(capability); await settle(); assert.equal(h.w.error.value, 'workspace restore failed'); assert.equal(h.w.activeId.value, '')
})

test('initialization waiting on environment metadata returns stale false after explicit close', async t => {
  const metaWait = deferred(), h = setup(t, { client: { listProjects: async () => { throw new Error('list unavailable') }, meta: () => metaWait.promise } })
  const pending = h.w.init(); await settle(); h.w.close(); metaWait.resolve(capability)
  assert.equal(await pending, false); assert.equal(h.w.activeId.value, ''); assert.equal(h.storage.data.get('project-test'), ''); assert.equal(h.w.error.value, ''); assert.deepEqual(h.activated, [])
})

test('pending refresh cannot drop a new registration or resurrect a removed recent entry', async t => {
  const h = setup(t); await h.w.init(); const oldList = deferred()
  h.client.listProjects = () => oldList.promise; const refresh = h.w.refresh()
  h.client.openProject = async () => ({ id: 'new', name: 'New', available: true }); await h.w.openPath('E:/new')
  oldList.resolve([a, b]); await refresh; assert.equal(h.w.projects.value.some(project => project.id === 'new'), true); assert.equal(h.w.activeId.value, 'new')
  const oldRemovalList = deferred(); h.client.listProjects = () => oldRemovalList.promise; const staleRefresh = h.w.refresh()
  assert.equal(await h.w.removeRecent(a), true); oldRemovalList.resolve([a, b, { id: 'new', available: true }]); await staleRefresh
  assert.equal(h.w.projects.value.some(project => project.id === 'a'), false); assert.equal(h.w.activeId.value, 'new')
})

test('overlapping refreshes commit only latest list/meta and do not let old completion clear loading', async t => {
  const lists = [deferred(), deferred()], metas = [deferred(), deferred()]; let listIndex = 0, metaIndex = 0
  const h = setup(t, { client: { listProjects: () => lists[listIndex++].promise, meta: () => metas[metaIndex++].promise } })
  const old = h.w.refresh(), next = h.w.refresh(); await settle()
  lists[0].resolve([a]); metas[0].resolve({ native_picker: { available: false } }); await old
  assert.equal(h.w.projects.value.length, 0); assert.equal(h.w.loading.value, true)
  metas[1].resolve(capability); await settle(); assert.equal(h.w.nativeAvailable.value, true)
  lists[1].resolve([b]); assert.equal(await next, true); assert.deepEqual(h.w.projects.value.map(project => project.id), ['b']); assert.equal(h.w.loading.value, false)
})

test('opening a modal defers in-flight initial restoration without clearing current/recent state', async t => {
  const listWait = deferred(), h = setup(t, { client: { listProjects: () => listWait.promise } })
  const pending = h.w.init(); h.w.deferRestore(); listWait.resolve([a, b]); assert.equal(await pending, false)
  assert.deepEqual(h.activated, []); assert.equal(h.w.projects.value.length, 2); assert.equal(h.w.activeId.value, ''); assert.equal(h.storage.writes.length, 0)
  await h.w.activate(a); h.w.deferRestore(); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.storage.data.get('project-test'), 'a')
})

test('removal deletes only registration; removing active closes once and never chooses another project', async t => {
  const h = setup(t); await h.w.init(); assert.equal(await h.w.removeRecent('b'), true)
  assert.deepEqual(h.calls.at(-1), ['removeProject', 'b', false]); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.closed.length, 0)
  assert.equal(await h.w.removeRecent(a), true); assert.equal(h.w.activeId.value, ''); assert.equal(h.closed.length, 1); assert.equal(h.storage.data.get('project-test'), ''); assert.deepEqual(h.activated, ['a'])
})

test('failed removal preserves list/active; delayed removal after another selection cannot close that workspace', async t => {
  const removed = deferred(), h = setup(t, { client: { removeProject: async () => { throw new Error('delete registration failed') } } }); await h.w.init()
  assert.equal(await h.w.removeRecent(a), false); assert.equal(h.w.activeId.value, 'a'); assert.equal(h.w.projects.value.length, 2); assert.equal(h.closed.length, 0); assert.match(h.w.error.value, /delete registration failed/)
  h.client.removeProject = () => removed.promise; const pending = h.w.removeRecent(a); await h.w.activate(b)
  removed.resolve({}); assert.equal(await pending, false); assert.equal(h.w.activeId.value, 'b'); assert.equal(h.closed.length, 0)
})

test('storage scopes isolate preferences; explicit close is also retained when persistence is disabled', async t => {
  const storage = memoryStorage({ first: 'a', second: 'b' })
  const first = setup(t, { storage, storageKey: 'first' }), second = setup(t, { storage, storageKey: 'second' }); await first.w.init(); await second.w.init()
  first.w.close(); assert.equal(storage.data.get('first'), ''); assert.equal(storage.data.get('second'), 'b'); assert.equal(second.w.activeId.value, 'b')
  const memoryOnly = setup(t, { storage: null }); await memoryOnly.w.init(); memoryOnly.w.close(); await memoryOnly.w.init(); assert.deepEqual(memoryOnly.activated, ['a']); assert.equal(memoryOnly.w.activeId.value, '')
})

test('storage read/write exceptions are visible and cannot prevent manual activation or inflate errors', async t => {
  const storage = { getItem() { throw new Error('read denied') }, setItem() { throw new Error('write denied') } }
  const h = setup(t, { storage }); await h.w.init(); assert.deepEqual(h.activated, []); assert.match(h.w.error.value, /偏好读取失败/)
  assert.equal(await h.w.activate(a), true); assert.equal(h.w.activeId.value, 'a'); assert.match(h.w.error.value, /偏好保存失败/)
  await h.w.activate(b); assert.equal(h.w.activeId.value, 'b'); assert.equal((h.w.error.value.match(/偏好保存失败/g) || []).length, 1)
  h.w.clearError(); assert.equal(h.w.error.value, ''); h.w.close(); assert.equal(h.w.activeId.value, ''); assert.match(h.w.error.value, /偏好保存失败/)
})

test('dispose invalidates metadata, registration and activation callbacks without further state/storage writes', async t => {
  const info = deferred(), list = deferred(), h = setup(t, { client: { meta: () => info.promise, listProjects: () => list.promise } })
  const read = h.w.refresh(); h.w.dispose(); info.resolve(capability); list.resolve([a]); assert.equal(await read, false)
  assert.equal(h.w.meta.value, null); assert.equal(h.w.projects.value.length, 0); assert.equal(await h.w.init(), false); assert.equal(h.w.close(), false)
  const register = deferred(), other = setup(t, { client: { openProject: () => register.promise } }); await other.w.init()
  const opened = other.w.openPath(b.root_path); other.w.dispose(); register.resolve({ id: 'late' }); assert.equal(await opened, false); assert.equal(other.w.activeId.value, 'a'); assert.deepEqual(other.activated, ['a'])
  const callback = deferred(), pending = setup(t, { storage: closedStorage(), onActivate: () => callback.promise }); await pending.w.init()
  const activation = pending.w.activate(a); await settle(); pending.w.dispose(); callback.resolve(); assert.equal(await activation, false); assert.equal(pending.w.activeId.value, ''); assert.equal(pending.storage.writes.length, 0)
})

test('empty paths/names and unavailable picker fail usefully without API mutation; retry can clear error', async t => {
  const h = setup(t, { storage: closedStorage(), client: { meta: async () => ({ native_picker: { available: false, reason: '远端仅支持手动路径' } }) } }); await h.w.init()
  assert.equal(await h.w.openPath('  '), false); assert.match(h.w.error.value, /完整路径/)
  assert.equal(await h.w.createProject({ name: ' ' }), false); assert.match(h.w.error.value, /项目名称/)
  assert.equal(await h.w.openFolder(), false); assert.equal(h.w.error.value, '远端仅支持手动路径')
  assert.equal(h.calls.some(([name]) => ['openProject', 'createProject', 'pickFolder'].includes(name)), false)
  assert.equal(await h.w.openPath(a.root_path), true); assert.equal(h.w.error.value, '')
})
