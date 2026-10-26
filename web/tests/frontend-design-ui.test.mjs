import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as vue from 'vue'
import { api } from '../src/api.js'
import { serializeDesignCss, parseDesignCss } from '../src/lib/frontendDesign.js'
import { wheelZoom, zoomScrollPosition } from '../src/lib/designCanvas.js'

const copy = value => JSON.parse(JSON.stringify(value))
const settle = async () => { await vue.nextTick(); for (let n = 0; n < 24; n++) await Promise.resolve() }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { resolve, reject, promise } }
const stylesheet = { path: '.code-agent/frontend-design.css', text: '', sha1: 'css-v1' }
const preview = pid => ({ html: `<html><body><button id="save">${pid} 保存</button><button id="other">取消</button></body></html>`, base_url: `http://127.0.0.1:9000/${pid}/`, entry_path: 'index.html', entry_sha1: 'entry-v1', stylesheet: { ...stylesheet }, source_kind: 'html' })
const element = (selector = '#save') => ({ selector, tag: 'button', text: '保存', styles: { color: '#000000', 'font-size': '14px' }, rect: { x: 10, y: 20, top: 20, left: 10, right: 110, bottom: 50, width: 100, height: 30 } })

async function setup(t, overrides = {}, storage = new Map(), propOverrides = {}) {
  const original = api.code, calls = [], events = [], timers = new Map(), frames = new Map(), listeners = new Map(), posted = [], downloads = [], clipboard = [], storageWrites = [], cancelledFrames = []
  let mount, unmount, counter = 0, frameCounter = 0, stopped = false
  const props = vue.reactive({ projectId: 'a', readonly: false, visible: true, aiResult: null, dirtyPaths: [], caseInsensitive: false, ...propOverrides })
  const stubs = { designInfo: async () => ({ entries: [{ path: 'index.html', label: '主页' }], stylesheet }), designPreview: async pid => preview(pid), saveDesignStyles: async (_, data) => ({ path: stylesheet.path, text: data.css, sha1: 'css-v2', entry_sha1: 'entry-v2', linked: true }), ...overrides }
  api.code = new Proxy(stubs, { get: (target, name) => (...args) => { calls.push({ name, args: copy(args) }); if (!target[name]) throw new Error(`Unstubbed API: ${String(name)}`); return target[name](...args) } })
  const requestAnimationFrame = fn => { frames.set(++frameCounter, fn); return frameCounter }
  const cancelAnimationFrame = id => { cancelledFrames.push(id); frames.delete(id) }
  const browserWindow = { innerWidth: 1280, requestAnimationFrame, cancelAnimationFrame, navigator: { clipboard: { writeText: async text => { clipboard.push(text) } } }, addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: type => listeners.delete(type) }
  const bindings = {
    defineProps: () => props, defineEmits: () => (...args) => events.push(args),
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => { storageWrites.push({ key, value }); storage.set(key, value) }, removeItem: key => storage.delete(key) },
    setTimeout: (fn, delay) => { timers.set(++counter, { fn, delay }); return counter }, clearTimeout: id => timers.delete(id),
    requestAnimationFrame, cancelAnimationFrame,
    window: browserWindow,
  }
  const url = new URL('../src/components/code/FrontendDesign.vue', import.meta.url)
  let script = readFileSync(url, 'utf8').match(/<script setup>([\s\S]*?)<\/script>/)[1]
  const imports = []
  script = script.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*$/gm, (_, clause, specifier) => { imports.push({ clause: clause.trim(), specifier }); return '' })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? vue : specifier.endsWith('.vue') ? { default: {} } : await import(new URL(specifier, url))
    if (clause.startsWith('{')) for (const entry of clause.slice(1, -1).split(',').map(s => s.trim()).filter(Boolean)) { const [key, local = key] = entry.split(/\s+as\s+/); bindings[local] = namespace[key] }
    else bindings[clause] = namespace.default
  }
  bindings.onMounted = fn => { mount = fn }; bindings.onBeforeUnmount = fn => { unmount = fn }
  bindings.downloadDesignText = payload => { downloads.push(copy(payload)) }
  const scope = vue.effectScope()
  const state = scope.run(() => new Function(...Object.keys(bindings), `${script}\nreturn {active,contexts,iframeEl,group,panelOpen,dirty,dirtySource,undoAvailable,redoAvailable,passCount,failureCount,canvasStyle,aiDiff,statusLabel,entryRuntime,suggestedService,connectSuggestedService,suggestedApi,connectSuggestedApi,disconnectApi,loadInfo,loadPreview,activate,retry,receive,setMode,setDevice,setViewport,applyDeclarations,applyStyleDraft,resetSelected,resetRule,undo,redo,saveStyles,requestAi,receiveAi,applyAi,dismissAi,addCheck,addChecks,updateCheck,changeCheckState,setCheckState,removeCheck,persistLocal,discardAndReload,frameLoaded,navigatePreview,refreshLayers,selectLayer,clearCanvasSelection,resetProperty,changeProperty,canvasViewport,workspaceEl,viewportSize,zoomLevel,frameSize,setZoom,fitCanvas,focusSelection,onCanvasWheel,beginPan,movePan,endPan,onDesignKey,releaseSpace,spaceHeld,panning,layersOpen,panToolActive,selectionAncestors,toggleLayers,toggleProperties,savePreset,applyPreset,removePreset,exportCss,copyCss,exportChecks,syncInteraction,cancelTransform,receiveTransform,transformInProgress}`)(...Object.values(bindings)))
  const frameWindow = vue.markRaw({ postMessage: (message, target) => posted.push({ message: copy(message), target }) })
  state.iframeEl.value = { contentWindow: frameWindow }
  mount?.()
  function dispose() { if (stopped) return; stopped = true; unmount?.(); scope.stop() }
  t.after(() => { dispose(); api.code = original })
  const message = (type, data = {}, source = frameWindow) => state.receive({ source, data: { channel: state.active.value.channel, type, ...data } })
  const select = (selector = '#save') => { state.setMode('edit'); message('selected', { element: element(selector) }) }
  const flushFrame = async () => { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(fn => fn(16)); await settle(); return callbacks.length }
  return { state, props, calls, events, timers, frames, cancelledFrames, flushFrame, listeners, posted, storage, storageWrites, stubs, message, select, dispose, browserWindow, downloads, clipboard }
}

test('default loads actual project HTML in acceptance mode without any style writes or AI dispatch', async t => {
  const fixture = await setup(t); await settle()
  const state = fixture.state.active.value
  assert.equal(state.mode, 'acceptance')
  assert.equal(state.loadedEntryPath, 'index.html')
  assert.match(state.document, /a 保存/)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
  assert.equal(fixture.events.length, 0)
  assert.equal(fixture.state.dirty.value, false)
  fixture.message('ready')
  assert.equal(state.bridgeReady, true)
  assert.deepEqual(fixture.posted.slice(-2).map(item => item.message.type), ['mode', 'styles'])
})

test('a discovered project API suggestion needs an explicit connection and reaches only that preview configuration', async t => {
  const suggested = 'http://127.0.0.1:8080/api/'
  const fixture = await setup(t, {
    designInfo: async () => ({ entries: [{ path: 'index.html', api_suggested_url: suggested }], stylesheet }),
    designPreview: async (pid, payload) => ({ ...preview(pid), api_url: payload.api_url || '', api_proxy_url: payload.api_url ? 'http://127.0.0.1:9000/api/v1/code/frontend-design-api/unit-ticket/' : '' }),
  }); await settle()
  assert.equal(fixture.state.suggestedApi.value, suggested)
  assert.equal(fixture.state.active.value.apiUrl, '')
  assert.equal(Object.hasOwn(fixture.calls.find(call => call.name === 'designPreview').args[1], 'api_url'), false)
  await fixture.state.connectSuggestedApi()
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').at(-1).args[1].api_url, suggested)
  const config = JSON.parse(fixture.state.active.value.document.match(/\nconst config = ([^\n]+);\n/)[1])
  assert.equal(config.apiUrl, suggested)
  assert.equal(config.apiProxyUrl, 'http://127.0.0.1:9000/api/v1/code/frontend-design-api/unit-ticket/')
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('project API addresses persist without capability tickets and remain isolated across projects and reloads', async t => {
  const storage = new Map()
  const fixture = await setup(t, {}, storage); await settle()
  fixture.state.active.value.apiUrl = 'http://127.0.0.1:8080/api/'
  fixture.state.persistLocal(fixture.state.active.value)
  fixture.props.projectId = 'b'; await settle()
  assert.equal(fixture.state.active.value.apiUrl, '')
  assert.equal(Object.hasOwn(fixture.calls.filter(call => call.name === 'designPreview').at(-1).args[1], 'api_url'), false)
  fixture.dispose()
  const restored = await setup(t, {}, storage); await settle()
  assert.equal(restored.calls.find(call => call.name === 'designPreview').args[1].api_url, 'http://127.0.0.1:8080/api/')
  assert.equal([...storage.values()].join('').includes('frontend-design-api/'), false)
})

test('unsafe API suggestions are unavailable and a credential-bearing address is never saved to browser storage', async t => {
  const fixture = await setup(t, {
    designInfo: async () => ({ entries: [{ path: 'index.html', api_suggested_url: 'http://user:private@127.0.0.1:8080/api/' }], stylesheet }),
  }); await settle()
  assert.equal(fixture.state.suggestedApi.value, '')
  const prior = fixture.calls.length
  await fixture.state.connectSuggestedApi()
  assert.equal(fixture.calls.length, prior)
  fixture.state.active.value.apiUrl = 'http://user:private@127.0.0.1:8080/api/'
  fixture.state.persistLocal(fixture.state.active.value)
  assert.equal([...fixture.storage.values()].join('').includes('private'), false)
  for (const value of ['http://127.0.0.1:8765/api/', 'http://127.0.0.1:5175/api/', 'https://127.0.0.1:8080/api/', 'http://example.invalid/api/']) {
    fixture.state.active.value.entries[0].api_suggested_url = value
    assert.equal(fixture.state.suggestedApi.value, '')
  }
})

test('connecting or disconnecting a project API preserves an unsaved style draft until explicit reload', async t => {
  const fixture = await setup(t, {
    designInfo: async () => ({ entries: [{ path: 'index.html', api_suggested_url: 'http://127.0.0.1:8080/api/' }], stylesheet }),
  }); await settle(); fixture.select(); fixture.state.applyDeclarations({ color: '#2255aa' })
  const prior = fixture.calls.length, document = fixture.state.active.value.document
  await fixture.state.connectSuggestedApi()
  assert.equal(fixture.calls.length, prior)
  assert.equal(fixture.state.active.value.document, document)
  assert.match(fixture.state.active.value.notice, /未保存样式/)
  await fixture.state.disconnectApi()
  assert.equal(fixture.calls.length, prior)
  assert.equal(fixture.state.active.value.rules[0].declarations.color, '#2255aa')
})

test('hidden tab defers first read, and hiding preserves project draft and checks', async t => {
  const fixture = await setup(t, {}, new Map(), { visible: false }); await settle()
  assert.equal(fixture.calls.length, 0)
  fixture.props.visible = true; await settle()
  fixture.select(); fixture.state.applyDeclarations({ color: '#2255aa' })
  fixture.state.active.value.checkDraft = '点击保存会显示确认'; fixture.state.addCheck()
  const state = fixture.state.active.value
  fixture.props.visible = false; await settle()
  fixture.props.visible = true; await settle()
  assert.equal(fixture.state.active.value, state)
  assert.equal(state.rules[0].declarations.color, '#2255aa')
  assert.equal(state.checks.length, 1)
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 1)
})

test('project without HTML entry stays genuinely empty and does not send an invalid URL-only preview', async t => {
  const fixture = await setup(t, { designInfo: async () => ({ entries: [], stylesheet }) }); await settle()
  fixture.state.active.value.url = 'http://127.0.0.1:5173/'
  await fixture.state.loadPreview()
  assert.equal(fixture.state.active.value.preview, null)
  assert.match(fixture.state.active.value.notice, /HTML 入口/)
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 0)
})

test('cross-project late preview response cannot replace the current project or selection', async t => {
  const delayed = deferred()
  const fixture = await setup(t, { designPreview: pid => pid === 'a' ? delayed.promise : preview(pid) }); await settle()
  const old = fixture.state.active.value
  fixture.props.projectId = 'b'; await settle()
  fixture.select('#other')
  delayed.resolve(preview('a')); await settle()
  assert.equal(fixture.state.active.value.projectId, 'b')
  assert.equal(fixture.state.active.value.selected.selector, '#other')
  assert.match(fixture.state.active.value.document, /b 保存/)
  assert.match(old.document, /a 保存/)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
})

test('selection bridge requires the current iframe source and channel and edit mode', async t => {
  const fixture = await setup(t); await settle()
  fixture.message('selected', { element: element() })
  assert.equal(fixture.state.active.value.selected, null)
  fixture.state.setMode('edit')
  fixture.message('selected', { element: element() }, {})
  assert.equal(fixture.state.active.value.selected, null)
  fixture.state.receive({ source: fixture.state.iframeEl.value.contentWindow, data: { channel: 'frontend-wrong-channel', type: 'selected', element: element() } })
  assert.equal(fixture.state.active.value.selected, null)
  fixture.message('selected', { element: element() })
  assert.equal(fixture.state.active.value.selected.selector, '#save')
  fixture.message('selected', { element: null })
  assert.equal(fixture.state.active.value.selected, null)
})

test('manual style application is preview-only, has undo/redo, and reset removes only selected overrides', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.active.value.styleDraft.color = '#3355aa'
  fixture.state.applyStyleDraft()
  assert.equal(fixture.state.active.value.rules[0].declarations.color, '#3355aa')
  assert.equal(fixture.state.dirty.value, true)
  assert.equal(fixture.state.undoAvailable.value, true)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
  fixture.select('#other'); fixture.state.applyDeclarations({ 'font-size': '20px' })
  fixture.state.resetSelected()
  assert.equal(fixture.state.active.value.rules.length, 1)
  assert.equal(fixture.state.active.value.rules[0].selector, '#save')
  fixture.state.undo(); assert.equal(fixture.state.active.value.rules.length, 2)
  fixture.state.redo(); assert.equal(fixture.state.active.value.rules.length, 1)
  assert.ok(fixture.posted.some(item => item.message.type === 'styles'))
})

test('saving sends both hashes and updates entry/stylesheet names only after successful write', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.state.applyDeclarations({ color: '#3355aa' })
  await fixture.state.saveStyles(); await settle()
  const call = fixture.calls.find(call => call.name === 'saveDesignStyles')
  assert.equal(call.args[0], 'a')
  assert.equal(call.args[1].expected_sha1, 'css-v1')
  assert.equal(call.args[1].expected_entry_sha1, 'entry-v1')
  assert.equal(call.args[1].entry_path, 'index.html')
  assert.equal(parseDesignCss(call.args[1].css)[0].declarations.color, '#3355aa')
  assert.equal(fixture.state.dirty.value, false)
  assert.deepEqual(fixture.events.find(event => event[0] === 'saved')[1], { path: stylesheet.path, entry_path: 'index.html' })
})

test('successful save preserves the backend warning about ledger or index availability', async t => {
  const warning = '样式已保存，改动台账暂不可用。'
  const fixture = await setup(t, { saveDesignStyles: async (_, data) => ({ path: stylesheet.path, text: data.css, sha1: 'css-v2', entry_sha1: 'entry-v2', linked: true, warning }) }); await settle(); fixture.select(); fixture.state.applyDeclarations({ color: '#3355aa' })
  await fixture.state.saveStyles(); await settle()
  assert.equal(fixture.state.dirty.value, false)
  assert.match(fixture.state.active.value.notice, /改动台账暂不可用/)
  assert.equal(fixture.events.find(event => event[0] === 'saved')[1].warning, warning)
})

test('invalid manual CSS values are explained and cannot create misleading empty style history', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  assert.equal(fixture.state.applyDeclarations({ color: 'url(https://example.invalid/)' }), false)
  assert.equal(fixture.state.active.value.rules.length, 0)
  assert.equal(fixture.state.undoAvailable.value, false)
  assert.match(fixture.state.active.value.notice, /不支持/)
  assert.equal(fixture.state.applyDeclarations({ position: 'fixed' }), false)
  assert.equal(fixture.state.active.value.rules.length, 0)
})

test('409 preserves draft and blocks overwrite until explicit reload of the latest document', async t => {
  const fixture = await setup(t, { saveDesignStyles: async () => { throw Object.assign(new Error('Conflict'), { status: 409 }) } }); await settle(); fixture.select(); fixture.state.applyDeclarations({ color: '#3355aa' })
  await fixture.state.saveStyles(); await settle()
  assert.equal(fixture.state.active.value.conflict, true)
  assert.equal(fixture.state.dirty.value, true)
  assert.equal(fixture.state.active.value.rules[0].declarations.color, '#3355aa')
  assert.match(fixture.state.active.value.notice, /草稿已保留/)
  assert.ok(JSON.parse(fixture.storage.get('code-agent-frontend-design-v1:a')).draft)
  await fixture.state.saveStyles()
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 1)
  await fixture.state.discardAndReload(); await settle()
  assert.equal(fixture.state.active.value.conflict, false)
  assert.equal(fixture.state.dirty.value, false)
})

test('readonly and editor unsaved entry or stylesheet block project writes without losing previews', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.state.applyDeclarations({ color: '#3355aa' })
  fixture.props.readonly = true; await fixture.state.saveStyles()
  fixture.props.readonly = false; fixture.props.dirtyPaths = ['index.html']; await settle()
  assert.equal(fixture.state.dirtySource.value, true); await fixture.state.saveStyles()
  fixture.props.dirtyPaths = ['.code-agent\\frontend-design.css']; await settle()
  assert.equal(fixture.state.dirtySource.value, true); await fixture.state.saveStyles()
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
  assert.equal(fixture.state.active.value.rules[0].declarations.color, '#3355aa')
})

test('Windows entry and stylesheet dirty paths use case-insensitive comparison', async t => {
  const fixture = await setup(t, {}, new Map(), { caseInsensitive: true }); await settle(); fixture.select(); fixture.state.applyDeclarations({ color: '#3355aa' })
  fixture.props.dirtyPaths = ['INDEX.HTML']; await settle()
  assert.equal(fixture.state.dirtySource.value, true); await fixture.state.saveStyles()
  fixture.props.dirtyPaths = ['.CODE-AGENT\\FRONTEND-DESIGN.CSS']; await settle()
  assert.equal(fixture.state.dirtySource.value, true); await fixture.state.saveStyles()
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
  fixture.props.caseInsensitive = false; await settle()
  assert.equal(fixture.state.dirtySource.value, false)
})

test('foreign stylesheet is preserved and never overwritten by managed CSS save', async t => {
  const fixture = await setup(t, { designPreview: async pid => ({ ...preview(pid), stylesheet: { ...stylesheet, text: 'button { background-image: url(image.png); }' } }) }); await settle(); fixture.select(); fixture.state.applyDeclarations({ color: '#3355aa' })
  assert.equal(fixture.state.active.value.stylesheetUnsupported, true)
  await fixture.state.saveStyles()
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
})

test('AI design requires an explicit request and separate confirmed application with matching identity', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.active.value.aiInstruction = '改成蓝色大字'
  fixture.state.requestAi()
  const pending = fixture.state.active.value.pendingAi
  assert.equal(fixture.events.filter(event => event[0] === 'ask').length, 1)
  assert.match(fixture.events.find(event => event[0] === 'ask')[1], /改成蓝色大字/)
  const result = { kind: 'frontend-style', request_id: pending.requestId, selector: '#save', declarations: { color: '#3355aa', 'font-size': '20px' }, summary: '蓝色与大字' }
  fixture.state.receiveAi({ projectId: 'b', text: JSON.stringify(result) })
  assert.equal(fixture.state.active.value.aiProposal, null)
  fixture.state.receiveAi({ projectId: 'a', text: JSON.stringify({ ...result, request_id: 'another-request' }) })
  assert.equal(fixture.state.active.value.aiProposal, null)
  fixture.state.receiveAi({ projectId: 'a', text: JSON.stringify(result) })
  assert.equal(fixture.state.active.value.rules.length, 0, 'AI response does not apply itself')
  assert.equal(fixture.state.aiDiff.value.length, 2)
  fixture.state.applyAi()
  assert.equal(fixture.state.active.value.rules[0].declarations['font-size'], '20px')
  assert.equal(fixture.state.active.value.pendingAi, null)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
})

test('switching selection invalidates an AI proposal and cannot apply the previous element result', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.active.value.aiInstruction = '蓝色'; fixture.state.requestAi()
  const pending = fixture.state.active.value.pendingAi
  const result = { projectId: 'a', text: JSON.stringify({ kind: 'frontend-style', request_id: pending.requestId, selector: '#save', declarations: { color: '#3355aa' }, summary: '蓝色' }) }
  fixture.select('#other'); fixture.state.receiveAi(result); fixture.state.applyAi()
  assert.equal(fixture.state.active.value.aiProposal, null)
  assert.equal(fixture.state.active.value.rules.length, 0)
  assert.match(fixture.state.active.value.aiNotice, /先前选中/)
})

test('unsupported current AI style result gives a recoverable error instead of remaining waiting', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.active.value.aiInstruction = '更醒目'; fixture.state.requestAi()
  const pending = fixture.state.active.value.pendingAi
  const bad = { kind: 'frontend-style', request_id: pending.requestId, selector: '#save', declarations: { color: 'url(https://example.invalid/)' }, summary: '不安全值' }
  const waiting = fixture.state.active.value.aiNotice
  fixture.state.receiveAi({ projectId: 'b', text: JSON.stringify(bad) })
  assert.equal(fixture.state.active.value.aiNotice, waiting)
  fixture.state.receiveAi({ projectId: 'a', text: JSON.stringify({ ...bad, request_id: 'other-request' }) })
  assert.equal(fixture.state.active.value.aiNotice, waiting)
  fixture.state.receiveAi({ projectId: 'a', text: JSON.stringify(bad) })
  assert.match(fixture.state.active.value.aiNotice, /重新生成/)
  assert.equal(fixture.state.active.value.aiProposal, null)
  assert.equal(fixture.state.active.value.rules.length, 0)
  assert.equal(fixture.state.active.value.pendingAi.requestId, pending.requestId)
})

test('acceptance records are project-local and device switching performs no writes or model calls', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.active.value.checkDraft = '按钮点击后页面变更'; fixture.state.addCheck()
  const check = fixture.state.active.value.checks[0]
  fixture.state.setCheckState(check, 'passed'); check.note = '看到确认消息'; fixture.state.persistLocal(fixture.state.active.value)
  fixture.state.setDevice('phone')
  assert.equal(fixture.state.canvasStyle.value.width, '390px')
  assert.equal(fixture.state.passCount.value, 1)
  fixture.props.projectId = 'b'; await settle(); assert.equal(fixture.state.active.value.checks.length, 0)
  fixture.props.projectId = 'a'; await settle(); assert.equal(fixture.state.active.value.checks[0].note, '看到确认消息')
  assert.equal(fixture.events.filter(event => event[0] === 'ask').length, 0)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
})

test('restored local draft keeps revision conflict and never auto-saves', async t => {
  const storage = new Map([['code-agent-frontend-design-v1:a', JSON.stringify({ entryPath: 'index.html', draft: { css: serializeDesignCss([{ selector: '#save', declarations: { color: '#3355aa' } }]), baseSha1: 'outdated', entrySha1: 'entry-v1', entryPath: 'index.html', url: '' } })]])
  const fixture = await setup(t, {}, storage); await settle()
  assert.equal(fixture.state.active.value.rules[0].declarations.color, '#3355aa')
  assert.equal(fixture.state.active.value.conflict, true)
  assert.equal(fixture.state.dirty.value, true)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
})

test('dirty previews require explicit discard before reloading another source', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.state.applyDeclarations({ color: '#3355aa' })
  fixture.state.active.value.url = 'http://127.0.0.1:9001/'
  await fixture.state.loadPreview()
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 1)
  assert.match(fixture.state.active.value.notice, /未保存/)
  await fixture.state.discardAndReload(); await settle()
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 2)
  assert.equal(fixture.state.dirty.value, false)
})

test('entry discovery timeout identifies the project read and retry restores the preview', async t => {
  const pending = deferred()
  let reads = 0, signal
  const fixture = await setup(t, { designInfo: (_, options) => {
    signal = options.signal
    return ++reads === 1 ? pending.promise : { entries: [{ path: 'index.html', label: '主页' }], stylesheet }
  } }); await settle()
  const timer = [...fixture.timers.values()].find(item => item.delay === 12000)
  assert.ok(timer); timer.fn(); await settle()
  assert.equal(signal.aborted, true)
  assert.equal(fixture.state.active.value.loaded, false)
  assert.match(fixture.state.active.value.loadError, /读取项目页面入口超时/)
  assert.doesNotMatch(fixture.state.active.value.loadError, /开发服务/)
  await fixture.state.retry(); await settle()
  assert.equal(reads, 2)
  assert.equal(fixture.state.active.value.loadError, '')
  assert.equal(fixture.state.active.value.loadedEntryPath, 'index.html')
  assert.match(fixture.state.active.value.document, /a 保存/)
  pending.resolve({ entries: [{ path: 'stale.html' }], stylesheet }); await settle()
  assert.equal(fixture.state.active.value.loadedEntryPath, 'index.html')
})

test('remote preview timeout identifies the page service and retry recovers', async t => {
  const pending = deferred()
  let loads = 0, signal
  const storage = new Map([['code-agent-frontend-design-v1:a', JSON.stringify({ url: 'http://127.0.0.1:9001/' })]])
  const fixture = await setup(t, { designPreview: (pid, _, options) => {
    signal = options.signal
    return ++loads === 1 ? pending.promise : preview(pid)
  } }, storage); await settle()
  const timer = [...fixture.timers.values()].find(item => item.delay === 12000)
  assert.ok(timer); timer.fn(); await settle()
  assert.equal(signal.aborted, true)
  assert.equal(fixture.state.active.value.loaded, true)
  assert.match(fixture.state.active.value.loadError, /连接前端页面超时/)
  await fixture.state.retry(); await settle()
  assert.equal(loads, 2)
  assert.equal(fixture.state.active.value.loadError, '')
  assert.match(fixture.state.active.value.document, /a 保存/)
  pending.resolve(preview('stale')); await settle()
  assert.doesNotMatch(fixture.state.active.value.document, /stale 保存/)
})

test('timeout exposes retry and late response after unmount cannot update the canvas', async t => {
  const pending = deferred()
  const fixture = await setup(t, { designPreview: () => pending.promise }); await settle()
  assert.equal(fixture.state.active.value.loading, true)
  const timer = [...fixture.timers.values()].find(item => item.delay === 12000)
  assert.ok(timer); timer.fn(); await settle()
  assert.equal(fixture.state.active.value.loading, false)
  assert.match(fixture.state.active.value.loadError, /载入项目页面超时/)
  assert.doesNotMatch(fixture.state.active.value.loadError, /开发服务/)
  const old = fixture.state.active.value
  fixture.dispose(); pending.resolve(preview('too late')); await settle()
  assert.equal(old.preview, null)
  assert.equal(fixture.listeners.has('message'), false)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
})

const assetBase = 'http://127.0.0.1:8765/api/v1/code/frontend-design-assets/test-ticket_123/'
test('acceptance navigation reloads a same-ticket static HTML with fresh selection bridge', async t => {
  const fixture = await setup(t, { designPreview: async (pid, args) => ({ ...preview(pid), entry_path: args.entry_path, base_url: assetBase }) }); await settle()
  const channel = fixture.state.active.value.channel
  await fixture.state.navigatePreview(assetBase + 'pages/about.html'); await settle()
  const call = fixture.calls.filter(call => call.name === 'designPreview').at(-1)
  assert.equal(call.args[1].entry_path, 'pages/about.html')
  assert.equal(call.args[1].url, '')
  assert.equal(fixture.state.active.value.loadedEntryPath, 'pages/about.html')
  assert.ok(fixture.state.active.value.entries.some(entry => entry.path === 'pages/about.html'))
  assert.notEqual(fixture.state.active.value.channel, channel)
  assert.equal(fixture.state.active.value.bridgeReady, false)
})

test('navigation refuses external, other-ticket, non-HTML static routes and dirty preview', async t => {
  const fixture = await setup(t, { designPreview: async pid => ({ ...preview(pid), base_url: assetBase }) }); await settle()
  for (const href of ['https://example.invalid/index.html', 'about:blank', assetBase.replace('test-ticket_123', 'other-ticket') + 'index.html', assetBase + 'images/logo.png', assetBase + 'route']) {
    await fixture.state.navigatePreview(href)
    assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 1)
    assert.ok(fixture.state.active.value.notice)
  }
  fixture.select(); fixture.state.applyDeclarations({ color: '#3355aa' }); fixture.state.setMode('acceptance')
  await fixture.state.navigatePreview(assetBase + 'next.html')
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 1)
  assert.match(fixture.state.active.value.notice, /先保存/)
  assert.equal(fixture.state.active.value.entryPath, 'index.html')
})

test('dev-server navigation preserves the current HTML entry and maps only the ticket path to dev origin', async t => {
  const fixture = await setup(t, { designPreview: async pid => ({ ...preview(pid), source_kind: 'dev-server', base_url: assetBase + 'nested/' }) }); await settle()
  fixture.state.active.value.loadedUrl = 'http://localhost:9001/nested/'
  await fixture.state.navigatePreview(assetBase + 'profile?tab=settings#bio'); await settle()
  const call = fixture.calls.filter(call => call.name === 'designPreview').at(-1)
  assert.equal(call.args[1].entry_path, 'index.html')
  assert.equal(call.args[1].url, 'http://localhost:9001/profile?tab=settings#bio')
})

test('iframe load clears stale connection and pings; timeout reports failure until verified ready', async t => {
  const fixture = await setup(t); await settle(); fixture.message('ready')
  assert.equal(fixture.state.active.value.bridgeReady, true)
  fixture.select()
  const beforeLoad = fixture.posted.length
  fixture.state.frameLoaded({ target: fixture.state.iframeEl.value })
  assert.equal(fixture.state.active.value.bridgeReady, false)
  assert.equal(fixture.state.active.value.selected, null)
  assert.ok(fixture.posted.slice(beforeLoad).some(item => item.message.type === 'ping' && item.message.channel === fixture.state.active.value.channel))
  const timer = [...fixture.timers.values()].find(item => item.delay === 8000)
  assert.ok(timer); timer.fn()
  assert.match(fixture.state.active.value.bridgeError, /未连接/)
  assert.equal(fixture.state.statusLabel.value, '画布连接失败')
  fixture.message('ready')
  assert.equal(fixture.state.active.value.bridgeReady, true)
  assert.equal(fixture.state.active.value.bridgeError, '')
})

test('returning after a hidden load with missed ready re-pings the current frame and recovers connection', async t => {
  const pending = deferred()
  const fixture = await setup(t, { designPreview: () => pending.promise }); await settle()
  fixture.props.visible = false; await settle()
  pending.resolve(preview('a')); await settle()
  assert.equal(fixture.state.active.value.loaded, true)
  assert.ok(fixture.state.active.value.preview)
  assert.equal(fixture.state.active.value.bridgeReady, false)
  const beforeShow = fixture.posted.length
  fixture.props.visible = true; await settle()
  assert.ok(fixture.posted.slice(beforeShow).some(item => item.message.type === 'ping' && item.message.channel === fixture.state.active.value.channel))
  assert.ok([...fixture.timers.values()].some(timer => timer.delay === 8000))
  fixture.message('ready')
  assert.equal(fixture.state.active.value.bridgeReady, true)
  assert.equal([...fixture.timers.values()].some(timer => timer.delay === 8000), false)
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 1, 'returning reconnects without rereading or losing drafts')
})

test('page runtime errors survive frame load and ready handshake, then clear only on new preview', async t => {
  const fixture = await setup(t); await settle()
  fixture.message('error', { message: 'Vue source import failed: Unexpected token' })
  assert.match(fixture.state.active.value.pageError, /Vue source/)
  fixture.state.frameLoaded({ target: fixture.state.iframeEl.value })
  fixture.message('ready')
  assert.equal(fixture.state.active.value.bridgeReady, true)
  assert.equal(fixture.state.active.value.bridgeError, '')
  assert.match(fixture.state.active.value.pageError, /Vue source/)
  assert.equal(fixture.state.statusLabel.value, '页面运行错误')
  await fixture.state.loadPreview(); await settle()
  assert.equal(fixture.state.active.value.pageError, '')
})

test('component source keeps sandbox without same-origin and visible local acceptance purpose', () => {
  const source = readFileSync(new URL('../src/components/code/FrontendDesign.vue', import.meta.url), 'utf8')
  const acceptanceSource = readFileSync(new URL('../src/components/code/DesignAcceptance.vue', import.meta.url), 'utf8')
  assert.match(source, /sandbox="allow-scripts allow-forms allow-modals"/)
  assert.doesNotMatch(source, /allow-same-origin/)
  assert.match(source, /<DesignAcceptance\b/)
  assert.match(acceptanceSource, /验收记录保存在当前浏览器，按项目隔离/)
  assert.match(source, /放弃草稿并重载/)
})

const rawRuntime = { requires_dev_server: true, reason: '入口引用未编译 Vue 组件。', source_entry: 'frontend/src/main.js', working_dir: 'frontend', command: 'npm run dev', suggested_url: 'http://127.0.0.1:5174/' }
test('selected preview runtime replaces pending discovery metadata without leaking into another entry', async t => {
  const pendingRuntime = { requires_dev_server: null, preflight_pending: true, command: '', suggested_url: '' }
  const fixture = await setup(t, {
    designInfo: async () => ({ entries: [
      { path: 'frontend/index.html', runtime: pendingRuntime },
      { path: 'frontend/dist/index.html', runtime: pendingRuntime },
    ] }),
    designPreview: async (_, payload) => ({ ...preview('a'), entry_path: payload.entry_path,
      runtime: payload.entry_path === 'frontend/index.html' ? rawRuntime : { requires_dev_server: false },
      api_suggested_url: 'http://127.0.0.1:8080/api/',
    }),
  }); await settle()
  assert.equal(fixture.state.active.value.serviceRequired, true)
  assert.equal(fixture.state.entryRuntime.value.command, 'npm run dev')
  assert.equal(fixture.state.active.value.entries[0].runtime.requires_dev_server, true)
  assert.equal(fixture.state.suggestedService.value, 'http://127.0.0.1:5174/')
  assert.equal(fixture.state.suggestedApi.value, 'http://127.0.0.1:8080/api/')
  fixture.state.active.value.entryPath = 'frontend/dist/index.html'
  assert.equal(fixture.state.entryRuntime.value.requires_dev_server, null)
  assert.equal(fixture.state.suggestedService.value, '')
  await fixture.state.loadPreview(); await settle()
  assert.equal(fixture.state.active.value.serviceRequired, false)
  assert.equal(fixture.state.entryRuntime.value.requires_dev_server, false)
  assert.match(fixture.state.active.value.document, /a 保存/)
})

test('raw source entry shows service requirements without connecting or rendering an empty iframe', async t => {
  const fixture = await setup(t, {
    designInfo: async () => ({ entries: [{ path: 'frontend/index.html', runtime: rawRuntime }] }),
    designPreview: async () => ({ ...preview('a'), html: '', entry_path: 'frontend/index.html', runtime: rawRuntime }),
  }); await settle()
  assert.equal(fixture.state.active.value.preview, null)
  assert.equal(fixture.state.active.value.document, '')
  assert.equal(fixture.state.active.value.serviceRequired, true)
  assert.equal(fixture.state.statusLabel.value, '需要前端开发服务')
  assert.equal(fixture.state.entryRuntime.value.command, 'npm run dev')
  assert.equal(fixture.state.suggestedService.value, 'http://127.0.0.1:5174/')
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 1)
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview')[0].args[1].url, '')
  assert.equal(fixture.events.length, 0)
})

test('explicit service connection uses selected project and exits the source requirement state', async t => {
  const fixture = await setup(t, {
    designInfo: async () => ({ entries: [{ path: 'frontend/index.html', runtime: rawRuntime }] }),
    designPreview: async (_, payload) => ({ ...preview('a'), html: payload.url ? preview('a').html : '', runtime: rawRuntime, source_kind: payload.url ? 'dev-server' : 'static' }),
  }); await settle()
  await fixture.state.connectSuggestedService(); await settle()
  const call = fixture.calls.filter(call => call.name === 'designPreview').at(-1)
  assert.equal(call.args[0], 'a')
  assert.equal(call.args[1].entry_path, 'frontend/index.html')
  assert.equal(call.args[1].url, 'http://127.0.0.1:5174/')
  assert.equal(fixture.state.active.value.serviceRequired, false)
  assert.match(fixture.state.active.value.document, /a 保存/)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
})

test('suggested service rejects external, credential and application control URLs', async t => {
  const fixture = await setup(t, { designInfo: async () => ({ entries: [{ path: 'index.html', runtime: { ...rawRuntime } }] }) }); await settle()
  for (const url of ['https://127.0.0.1:5174', 'http://external.invalid:5174', 'http://user:secret@127.0.0.1:5174', 'http://127.0.0.1:8765', 'http://127.0.0.1:5175', 'http://127.0.0.1:5174/?key=secret']) {
    fixture.state.active.value.entries[0].runtime.suggested_url = url
    assert.equal(fixture.state.suggestedService.value, '')
    await fixture.state.connectSuggestedService()
  }
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 1)
})

test('switching a saved page to a source requirement clears obsolete styles and keeps acceptance records', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.applyDeclarations({ color: '#3355aa' }); await fixture.state.saveStyles(); await settle()
  fixture.state.active.value.checkDraft = '页面可操作'; fixture.state.addCheck()
  fixture.state.active.value.entries.push({ path: 'frontend/index.html', runtime: rawRuntime })
  fixture.stubs.designPreview = async () => ({ ...preview('a'), html: '', entry_path: 'frontend/index.html', runtime: rawRuntime })
  fixture.state.active.value.entryPath = 'frontend/index.html'
  await fixture.state.loadPreview(); await settle()
  assert.equal(fixture.state.active.value.serviceRequired, true)
  assert.equal(fixture.state.active.value.rules.length, 0)
  assert.equal(fixture.state.active.value.checks.length, 1)
  assert.equal(fixture.state.dirty.value, false)
  assert.equal(fixture.state.undoAvailable.value, false)
})

test('page errors retain bounded distinct failures and clear on successful reload', async t => {
  const fixture = await setup(t); await settle()
  for (let n = 0; n < 8; n++) fixture.message('error', { error_kind: 'resource', message: `脚本加载失败：assets/file${n}.js`, resource_path: `assets/file${n}.js`, resource_type: 'script' })
  fixture.message('error', { error_kind: 'resource', message: '脚本加载失败：assets/file7.js', resource_path: 'assets/file7.js', resource_type: 'script' })
  assert.equal(fixture.state.active.value.pageErrors.length, 5)
  assert.match(fixture.state.active.value.pageError, /file3.js/)
  fixture.state.frameLoaded(); fixture.message('ready')
  assert.equal(fixture.state.active.value.pageErrors.length, 5)
  await fixture.state.loadPreview(); await settle()
  assert.equal(fixture.state.active.value.pageErrors.length, 0)
  assert.equal(fixture.state.active.value.pageError, '')
})

test('runtime failures show the actual script position and keep separate locations', async t => {
  const fixture = await setup(t); await settle()
  fixture.message('error', { error_kind: 'runtime', message: 'SecurityError: 页面存储不可用', filename: 'assets/index.js', line: 8, column: 34 })
  assert.equal(fixture.state.active.value.pageError, 'SecurityError: 页面存储不可用（assets/index.js:8:34）')
  fixture.message('error', { error_kind: 'runtime', message: 'SecurityError: 页面存储不可用', filename: 'assets/index.js', line: 9, column: 12 })
  assert.equal(fixture.state.active.value.pageErrors.length, 2)
  assert.equal(fixture.state.active.value.pageErrors[1].location, 'assets/index.js:9:12')
})

test('built document resources stay in the dist directory while dev resources use the capability root', async t => {
  const parseEnvironment = () => JSON.parse(fixture.state.active.value.document.match(/\nconst config = ([^\n]+);\n/)[1])
  const fixture = await setup(t, {
    designInfo: async () => ({ entries: [{ path: 'frontend/dist/index.html' }] }),
    designPreview: async () => ({ ...preview('a'), source_kind: 'static', entry_path: 'frontend/dist/index.html', base_url: assetBase + 'frontend/dist/' }),
  }); await settle()
  assert.equal(parseEnvironment().resourceRootUrl, assetBase + 'frontend/dist/')
  assert.equal(parseEnvironment().initialRoute, '/')
  fixture.stubs.designPreview = async () => ({ ...preview('a'), source_kind: 'dev-server', entry_path: 'frontend/dist/index.html', base_url: assetBase + 'notes/' })
  fixture.state.active.value.url = 'http://127.0.0.1:9001/notes/42?tab=detail'
  await fixture.state.loadPreview(); await settle()
  assert.equal(parseEnvironment().resourceRootUrl, assetBase)
  assert.equal(parseEnvironment().initialRoute, '/notes/42?tab=detail')
})

test('a plain nested HTML document retains project-root resources instead of treating its folder as a build', async t => {
  const fixture = await setup(t, {
    designInfo: async () => ({ entries: [{ path: 'docs/index.html' }] }),
    designPreview: async () => ({ ...preview('a'), source_kind: 'static', entry_path: 'docs/index.html', base_url: assetBase + 'docs/', runtime: { working_dir: 'docs', command: '' } }),
  }); await settle()
  const config = JSON.parse(fixture.state.active.value.document.match(/\nconst config = ([^\n]+);\n/)[1])
  assert.equal(config.resourceRootUrl, assetBase)
})

const pageLayers = () => [
  { selector: 'body', parentSelector: '', tag: 'body', name: '页面', hidden: false, childCount: 1 },
  { selector: '#save', parentSelector: 'body', tag: 'button', name: '保存', hidden: false, childCount: 0 },
]
function attachCanvas(fixture, values = {}) {
  const viewport = vue.markRaw({ clientWidth: 500, clientHeight: 400, scrollLeft: 100, scrollTop: 60, getBoundingClientRect: () => ({ left: 20, top: 30 }), ...values })
  fixture.state.canvasViewport.value = viewport
  fixture.state.iframeEl.value.getBoundingClientRect = () => ({ left: 100, top: 90 })
  return viewport
}
function designKey(overrides = {}) {
  return { key: '', code: '', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, isComposing: false, target: { tagName: 'DIV' }, preventDefault() {}, ...overrides }
}
const nearly = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-8, `expected ${actual} to be ${expected}`)

test('edit mode requests real layers when the bridge becomes ready and tree selection waits for its inspected reply', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.setMode('edit')
  assert.equal(fixture.posted.some(item => item.message.type === 'get-layers'), false)
  fixture.message('ready')
  assert.deepEqual(fixture.posted.slice(-3).map(item => item.message.type), ['mode', 'styles', 'get-layers'])
  assert.equal(fixture.state.active.value.layersLoading, true)
  fixture.message('layers', { nodes: pageLayers(), selectedSelector: '', truncated: false })
  assert.deepEqual(copy(fixture.state.active.value.layers), pageLayers())
  assert.equal(fixture.state.active.value.layersLoading, false)
  fixture.state.active.value.canvasTool = 'pan'
  fixture.state.selectLayer('#save')
  assert.deepEqual(fixture.posted.at(-1).message, { channel: fixture.state.active.value.channel, type: 'inspect', selector: '#save', scrollIntoView: true })
  assert.equal(fixture.state.active.value.canvasTool, 'select')
  assert.equal(fixture.state.active.value.selected, null, 'the tree cannot invent computed styles')
  fixture.message('selected', { element: { ...element(), ancestors: [{ selector: 'body', tag: 'body', name: '页面' }] } })
  assert.equal(fixture.state.active.value.selected.selector, '#save')
  assert.deepEqual(copy(fixture.state.selectionAncestors.value), [{ selector: 'body', tag: 'body', name: '页面' }])
  fixture.state.clearCanvasSelection()
  assert.equal(fixture.state.active.value.selected, null)
  assert.deepEqual(copy(fixture.state.active.value.styleDraft), {})
  assert.equal(fixture.posted.at(-1).message.selector, '')
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('foreign, malformed and hidden layer or selection replies cannot replace current canvas state', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.message('ready')
  const payload = { nodes: pageLayers(), selectedSelector: '#save', truncated: false }
  fixture.message('layers', payload)
  const original = copy(fixture.state.active.value.layers)
  fixture.message('layers', { ...payload, nodes: [] }, {})
  fixture.message('layers', { ...payload, nodes: [], channel: 'foreign-channel' })
  fixture.message('layers', { ...payload, nodes: [{ ...pageLayers()[1], parentSelector: '#missing' }] })
  assert.deepEqual(copy(fixture.state.active.value.layers), original)
  fixture.props.visible = false; await settle()
  fixture.message('layers', { nodes: [], selectedSelector: '', truncated: false })
  fixture.message('selected', { element: element('#other') })
  fixture.message('shortcut', { action: 'clear' })
  assert.deepEqual(copy(fixture.state.active.value.layers), original)
  assert.equal(fixture.state.active.value.selected.selector, '#save')
  fixture.props.visible = true; await settle()
  fixture.message('layers', { nodes: [], selectedSelector: '', truncated: true })
  assert.deepEqual(copy(fixture.state.active.value.layers), [])
  assert.equal(fixture.state.active.value.layersTruncated, true)
})

test('single inspector changes, clears and resets preserve other fields and participate in undo without writes', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.changeProperty({ property: 'width', value: '120px' })
  fixture.state.changeProperty({ property: 'color', value: '#3355aa' })
  assert.deepEqual(copy(fixture.state.active.value.rules[0].declarations), { width: '120px', color: '#3355aa' })
  fixture.state.changeProperty({ property: 'width', value: '' })
  assert.deepEqual(copy(fixture.state.active.value.rules[0].declarations), { color: '#3355aa' })
  fixture.state.undo()
  assert.equal(fixture.state.active.value.rules[0].declarations.width, '120px')
  fixture.state.resetProperty('color')
  assert.deepEqual(copy(fixture.state.active.value.rules[0].declarations), { width: '120px' })
  fixture.state.resetProperty('width')
  assert.equal(fixture.state.active.value.rules.length, 0)
  fixture.state.undo()
  assert.deepEqual(copy(fixture.state.active.value.rules[0].declarations), { width: '120px' })
  const history = fixture.state.active.value.historyIndex
  assert.equal(fixture.state.resetProperty('position'), false)
  fixture.state.changeProperty({ property: 'position', value: 'fixed' })
  assert.equal(fixture.state.active.value.historyIndex, history)
  assert.equal(fixture.calls.filter(call => call.name === 'saveDesignStyles').length, 0)
})

test('canvas shortcuts leave inputs and editable ancestors untouched and support shifted Digit fit or focus keys', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  const viewport = attachCanvas(fixture); await settle()
  fixture.state.iframeEl.value.getBoundingClientRect = () => ({ left: 20 + 24 - viewport.scrollLeft, top: 30 + 24 + 30 * fixture.state.zoomLevel.value / 100 - viewport.scrollTop })
  let prevented = 0
  for (const target of [{ tagName: 'INPUT' }, { tagName: 'TEXTAREA' }, { tagName: 'SELECT' }, { tagName: 'SPAN', parentElement: { isContentEditable: true } }]) {
    for (const event of [{ key: 'h' }, { key: ' ' }, { key: 'z', ctrlKey: true }, { key: '!', code: 'Digit1', shiftKey: true }]) fixture.state.onDesignKey(designKey({ ...event, target, preventDefault: () => prevented++ }))
  }
  assert.equal(prevented, 0)
  assert.equal(fixture.state.active.value.canvasTool, 'select')
  assert.equal(fixture.state.spaceHeld.value, false)
  fixture.state.onDesignKey(designKey({ key: ' ', target: { tagName: 'BUTTON', closest: () => ({ tagName: 'BUTTON' }) }, preventDefault: () => prevented++ }))
  assert.equal(prevented, 0, 'Space retains native activation on focused toolbar controls')
  assert.equal(fixture.state.spaceHeld.value, false)
  fixture.state.setDevice('phone')
  fixture.state.onDesignKey(designKey({ key: '!', code: 'Digit1', shiftKey: true, preventDefault: () => prevented++ }))
  await settle()
  nearly(fixture.state.zoomLevel.value, 352 / 360 * 100)
  fixture.message('selected', { element: { ...element(), rect: { x: 300, y: 400, left: 300, top: 400, right: 400, bottom: 430, width: 100, height: 30 } } })
  fixture.state.onDesignKey(designKey({ key: '@', code: 'Digit2', shiftKey: true, preventDefault: () => prevented++ }))
  await settle()
  assert.equal(fixture.state.zoomLevel.value, 200)
  assert.equal(viewport.scrollLeft, 474)
  assert.equal(viewport.scrollTop, 714)
  assert.equal(prevented, 2)
  fixture.state.onDesignKey(designKey({ key: 'Escape' }))
  assert.equal(fixture.state.active.value.selected, null)
})

test('space and middle-button panning track only the active pointer and stop on release, hide or mode changes', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  const viewport = attachCanvas(fixture); await settle()
  const captured = []
  const pointer = overrides => ({ button: 0, pointerId: 7, clientX: 100, clientY: 80, currentTarget: { setPointerCapture: id => captured.push(id) }, preventDefault() {}, ...overrides })
  fixture.state.beginPan(pointer({}))
  assert.equal(fixture.state.panning.value, false)
  fixture.state.onDesignKey(designKey({ key: ' ' }))
  assert.equal(fixture.state.panToolActive.value, true)
  fixture.state.beginPan(pointer({}))
  assert.deepEqual(captured, [7])
  fixture.state.movePan(pointer({ pointerId: 8, clientX: 60, clientY: 30 }))
  assert.equal(viewport.scrollLeft, 100)
  fixture.state.movePan(pointer({ clientX: 60, clientY: 30 }))
  assert.equal(viewport.scrollLeft, 140)
  assert.equal(viewport.scrollTop, 110)
  fixture.state.releaseSpace({ type: 'keyup', key: ' ' })
  assert.equal(fixture.state.panning.value, false)
  assert.equal(fixture.state.spaceHeld.value, false)
  fixture.state.beginPan(pointer({ button: 1 }))
  assert.equal(fixture.state.panning.value, true)
  fixture.state.releaseSpace({ type: 'blur' })
  assert.equal(fixture.state.panning.value, false)
  fixture.state.beginPan(pointer({ button: 1 })); fixture.props.visible = false; await settle()
  assert.equal(fixture.state.panning.value, false)
  fixture.state.beginPan(pointer({ button: 1 }))
  assert.equal(fixture.state.panning.value, false, 'a hidden viewport cannot start a pan')
  fixture.props.visible = true; await settle()
  fixture.state.beginPan(pointer({ button: 1 })); fixture.state.setMode('acceptance')
  assert.equal(fixture.state.panning.value, false)
})

test('zoom keeps its pointer anchor, persists per project and pending scroll changes cannot affect a replacement project', async t => {
  const storage = new Map([['code-agent-frontend-design-v1:a', JSON.stringify({ version: 1, zoom: 100 })], ['code-agent-frontend-design-v1:b', JSON.stringify({ version: 1, zoom: 75 })]])
  const fixture = await setup(t, {}, storage); await settle()
  const viewport = attachCanvas(fixture); await settle()
  await fixture.state.setZoom(150, { x: 200, y: 100 })
  assert.equal(viewport.scrollLeft, 250)
  assert.equal(viewport.scrollTop, 140)
  assert.equal(JSON.parse(storage.get('code-agent-frontend-design-v1:a')).zoom, 150)
  viewport.scrollLeft = 100; viewport.scrollTop = 60
  const pending = fixture.state.setZoom(175, { x: 200, y: 100 })
  fixture.props.projectId = 'b'
  await settle(); await pending
  assert.equal(fixture.state.zoomLevel.value, 75)
  assert.equal(viewport.scrollLeft, 100)
  assert.equal(viewport.scrollTop, 60)
  fixture.props.projectId = 'a'; await settle()
  assert.equal(fixture.state.zoomLevel.value, 175)
  assert.equal(JSON.parse(storage.get('code-agent-frontend-design-v1:b')).zoom, 75)
})

test('continuous modified wheel preserves viewport anchors and iframe wheel normalizes units and frame coordinates', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.message('ready')
  const viewport = attachCanvas(fixture); await settle()
  let prevented = 0
  fixture.state.onCanvasWheel({ deltaY: -100, clientX: 220, clientY: 130, preventDefault: () => prevented++ })
  assert.equal(fixture.state.zoomLevel.value, 100)
  assert.equal(fixture.frames.size, 0)
  fixture.state.onCanvasWheel({ ctrlKey: true, deltaY: -0.5, clientX: 220, clientY: 130, preventDefault: () => prevented++ })
  assert.equal(fixture.state.zoomLevel.value, 100, 'wheel input is applied on the next animation frame')
  assert.equal(await fixture.flushFrame(), 1)
  assert.equal(prevented, 1)
  const zoom = wheelZoom(100, { deltaY: -0.5 })
  assert.ok(zoom > 100 && zoom < 101, 'a small trackpad delta must not jump by 25 percentage points')
  nearly(fixture.state.zoomLevel.value, zoom)
  nearly((viewport.scrollLeft + 200) / zoom, (100 + 200) / 100)
  nearly((viewport.scrollTop + 100) / zoom, (60 + 100) / 100)
  await fixture.state.setZoom(150)
  viewport.scrollLeft = 100; viewport.scrollTop = 60
  fixture.message('canvas-wheel', { deltaY: -2, deltaMode: 1, clientX: 40, clientY: 20 })
  assert.equal(await fixture.flushFrame(), 1)
  const iframeZoom = wheelZoom(150, { deltaY: -2, deltaMode: 1, pageHeight: 400 })
  nearly(fixture.state.zoomLevel.value, iframeZoom)
  const position = zoomScrollPosition({ scrollLeft: 100, scrollTop: 60, anchorX: 140, anchorY: 90, previousZoom: 150, nextZoom: iframeZoom })
  nearly(viewport.scrollLeft, position.scrollLeft)
  nearly(viewport.scrollTop, position.scrollTop)
  for (const value of [{ deltaY: 0, clientX: 40, clientY: 20 }, { deltaY: -120, clientX: -1, clientY: 20 }, { deltaY: -120, deltaMode: 3, clientX: 40, clientY: 20 }, { deltaY: -120, clientX: 40, clientY: 20, channel: 'foreign-channel' }]) fixture.message('canvas-wheel', value)
  fixture.message('canvas-wheel', { deltaY: -120, clientX: 40, clientY: 20 }, {})
  assert.equal(fixture.frames.size, 0)
  nearly(fixture.state.zoomLevel.value, iframeZoom)
  fixture.props.visible = false; await settle()
  fixture.message('canvas-wheel', { deltaY: -120, clientX: 40, clientY: 20 })
  assert.equal(fixture.frames.size, 0)
  nearly(fixture.state.zoomLevel.value, iframeZoom)
})

test('wheel bursts accumulate once per frame at the latest pointer and persist only after the idle debounce', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.message('ready')
  const viewport = attachCanvas(fixture); await settle()
  const state = fixture.state.active.value, initialHistory = copy(state.history), initialRules = copy(state.rules), writes = fixture.storageWrites.length, zoomChanges = []
  const stop = vue.watch(() => state.zoom, value => zoomChanges.push(value), { flush: 'sync' }); t.after(stop)
  const wheel = (deltaY, clientX = 220, clientY = 130) => fixture.state.onCanvasWheel({ metaKey: true, deltaY, clientX, clientY, preventDefault() {} })
  wheel(-0.25, 80, 70); wheel(-0.5, 160, 100); wheel(-0.75)
  assert.equal(fixture.frames.size, 1)
  assert.equal(zoomChanges.length, 0)
  assert.equal(await fixture.flushFrame(), 1)
  const firstZoom = wheelZoom(100, { deltaY: -1.5 })
  nearly(fixture.state.zoomLevel.value, firstZoom)
  nearly(viewport.scrollLeft, (100 + 200) * firstZoom / 100 - 200)
  nearly(viewport.scrollTop, (60 + 100) * firstZoom / 100 - 100)
  assert.equal(zoomChanges.length, 1, 'a burst makes one reactive zoom update for its frame')
  assert.equal(fixture.storageWrites.length, writes, 'wheel rendering does not synchronously serialize project state')
  wheel(-0.5); wheel(-0.5)
  assert.equal(fixture.frames.size, 1)
  assert.equal(await fixture.flushFrame(), 1)
  nearly(fixture.state.zoomLevel.value, wheelZoom(100, { deltaY: -2.5 }))
  assert.equal(zoomChanges.length, 2)
  const persistence = [...fixture.timers.entries()].filter(([, timer]) => timer.delay === 250)
  assert.equal(persistence.length, 1, 'new frames replace the previous idle timer')
  assert.equal(fixture.storageWrites.length, writes)
  fixture.timers.delete(persistence[0][0]); persistence[0][1].fn(); await settle()
  assert.equal(fixture.storageWrites.length, writes + 1)
  nearly(JSON.parse(fixture.storage.get('code-agent-frontend-design-v1:a')).zoom, fixture.state.zoomLevel.value)
  assert.deepEqual(copy(state.history), initialHistory); assert.deepEqual(copy(state.rules), initialRules)
  assert.equal(fixture.state.dirty.value, false)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('wheel anchoring also preserves the logical point when the stage recenters as it scales', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.message('ready')
  const viewport = attachCanvas(fixture)
  const stage = { getBoundingClientRect: () => ({ left: 20 + Math.max(24, (500 - 300 * fixture.state.zoomLevel.value / 100) / 2) - viewport.scrollLeft, top: 30 + 24 - viewport.scrollTop }) }
  viewport.querySelector = selector => selector === '.canvas-stage' ? stage : null
  await settle()
  const point = { x: (220 - stage.getBoundingClientRect().left) * 100 / fixture.state.zoomLevel.value, y: (130 - stage.getBoundingClientRect().top) * 100 / fixture.state.zoomLevel.value }
  fixture.state.onCanvasWheel({ ctrlKey: true, deltaY: -25, clientX: 220, clientY: 130, preventDefault() {} })
  await fixture.flushFrame()
  nearly((220 - stage.getBoundingClientRect().left) * 100 / fixture.state.zoomLevel.value, point.x)
  nearly((130 - stage.getBoundingClientRect().top) * 100 / fixture.state.zoomLevel.value, point.y)
})

test('obsolete queued wheel frames cannot move a hidden, replaced or unmounted canvas', async t => {
  for (const transition of ['project', 'page', 'mode', 'hidden', 'unmount']) await t.test(transition, async sub => {
    const fixture = await setup(sub); await settle(); fixture.select(); fixture.message('ready')
    const viewport = attachCanvas(fixture); await settle()
    fixture.state.onCanvasWheel({ ctrlKey: true, deltaY: -5, clientX: 220, clientY: 130, preventDefault() {} })
    const [id, callback] = [...fixture.frames.entries()][0]
    if (transition === 'project') { fixture.props.projectId = 'b'; await settle() }
    if (transition === 'page') await fixture.state.loadPreview()
    if (transition === 'mode') fixture.state.setMode('acceptance')
    if (transition === 'hidden') { fixture.props.visible = false; await settle() }
    if (transition === 'unmount') fixture.dispose()
    await settle()
    assert.equal(fixture.frames.size, 0, 'the lifecycle transition cancels its queued frame')
    assert.ok(fixture.cancelledFrames.includes(id))
    const writes = fixture.storageWrites.length
    callback(16); await settle()
    assert.equal(fixture.state.zoomLevel.value, 100)
    assert.equal(viewport.scrollLeft, 100); assert.equal(viewport.scrollTop, 60)
    assert.equal(fixture.storageWrites.length, writes, 'a callback already dequeued by the browser is still invalidated')
    assert.equal(fixture.frames.size, 0)
  })
})

test('wheel scroll completion and delayed persistence remain bound to the original project', async t => {
  const storage = new Map([['code-agent-frontend-design-v1:b', JSON.stringify({ version: 1, zoom: 75 })]])
  const fixture = await setup(t, {}, storage); await settle(); fixture.select(); fixture.message('ready')
  const viewport = attachCanvas(fixture); await settle()
  fixture.state.onCanvasWheel({ ctrlKey: true, deltaY: -5, clientX: 220, clientY: 130, preventDefault() {} })
  const callback = [...fixture.frames.values()][0]; fixture.frames.clear(); callback(16)
  const oldZoom = fixture.state.zoomLevel.value
  assert.ok(oldZoom > 100)
  const timer = [...fixture.timers.values()].find(timer => timer.delay === 250)
  assert.ok(timer)
  fixture.props.projectId = 'b'; await settle()
  assert.equal(fixture.state.zoomLevel.value, 75)
  assert.equal(viewport.scrollLeft, 100); assert.equal(viewport.scrollTop, 60)
  nearly(JSON.parse(storage.get('code-agent-frontend-design-v1:a')).zoom, oldZoom)
  const writes = fixture.storageWrites.length
  timer.fn(); await settle()
  assert.equal(fixture.storageWrites.length, writes, 'a stale idle callback cannot serialize the replacement project')
  assert.equal(JSON.parse(storage.get('code-agent-frontend-design-v1:b')).zoom, 75)
})

test('a cancelled wheel callback cannot consume a newly queued frame from the replacement project', async t => {
  const storage = new Map([['code-agent-frontend-design-v1:b', JSON.stringify({ version: 1, zoom: 75 })]])
  const fixture = await setup(t, {}, storage); await settle(); fixture.select(); fixture.message('ready')
  attachCanvas(fixture); await settle()
  const wheel = () => fixture.state.onCanvasWheel({ ctrlKey: true, deltaY: -1, clientX: 220, clientY: 130, preventDefault() {} })
  wheel()
  const obsoleteCallback = [...fixture.frames.values()][0]
  fixture.props.projectId = 'b'; await settle(); fixture.select(); fixture.message('ready'); await settle()
  wheel()
  assert.equal(fixture.frames.size, 1)
  obsoleteCallback(16); await settle()
  assert.equal(fixture.state.zoomLevel.value, 75, 'the obsolete callback must not read and apply the replacement project pending wheel')
  assert.equal(fixture.frames.size, 1)
  assert.equal(await fixture.flushFrame(), 1)
  nearly(fixture.state.zoomLevel.value, wheelZoom(75, { deltaY: -1 }))
})

test('validated iframe middle-button pan uses scaled coordinates and the parent pointer can continue the same gesture without style history', async t => {
  for (const zoom of [25, 100, 200]) await t.test(`${zoom}%`, async sub => {
    const fixture = await setup(sub, {}, new Map(), { readonly: true }); await settle(); fixture.select(); fixture.message('ready')
    await fixture.state.setZoom(zoom)
    const viewport = attachCanvas(fixture); await settle()
    const state = fixture.state.active.value, rules = copy(state.rules), history = copy(state.history), selected = copy(state.selected)
    const pan = (phase, extra = {}) => fixture.message('canvas-pan', { phase, pointerId: 5, clientX: 40, clientY: 20, ...extra })
    pan('start'); await settle()
    assert.equal(fixture.state.panning.value, true)
    assert.equal(interaction(fixture).enabled, false)
    assert.equal(interaction(fixture).panEnabled, true, 'disabling element editing must not abort the active canvas pan')
    pan('move', { pointerId: 6, clientX: 20, clientY: -10 })
    assert.equal(viewport.scrollLeft, 100); assert.equal(viewport.scrollTop, 60)
    pan('move', { clientX: 20, clientY: -10 })
    nearly(viewport.scrollLeft, 100 + 20 * zoom / 100)
    nearly(viewport.scrollTop, 60 + 30 * zoom / 100)
    fixture.state.movePan({ pointerId: 5, clientX: 100 + 40 * zoom / 100 - 30, clientY: 90 + 20 * zoom / 100 - 40, preventDefault() {} })
    nearly(viewport.scrollLeft, 130); nearly(viewport.scrollTop, 100)
    pan('end', { pointerId: 6 })
    assert.equal(fixture.state.panning.value, true)
    pan('end'); await settle()
    assert.equal(fixture.state.panning.value, false)
    nearly(viewport.scrollLeft, 130); nearly(viewport.scrollTop, 100)
    assert.equal(interaction(fixture).enabled, true)
    assert.deepEqual(copy(state.rules), rules); assert.deepEqual(copy(state.history), history)
    assert.deepEqual(copy(state.selected), selected)
    assert.equal(fixture.state.dirty.value, false)
    assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
  })
})

test('iframe pan rejects foreign or malformed messages and old gestures cannot move hidden or replacement pages', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.message('ready')
  const viewport = attachCanvas(fixture); await settle()
  const pan = (phase = 'start', extra = {}, source) => fixture.message('canvas-pan', { phase, pointerId: 5, clientX: 40, clientY: 20, ...extra }, source)
  for (const extra of [{ phase: 'unknown' }, { pointerId: -1 }, { pointerId: 0.5 }, { pointerId: '5' }, { clientX: NaN }, { clientY: '20' }, { clientX: 100001 }, { channel: 'old-channel' }]) pan('start', extra)
  pan('start', {}, {})
  pan('move', { clientX: -10, clientY: -20 }); pan('end')
  assert.equal(fixture.state.panning.value, false)
  assert.equal(viewport.scrollLeft, 100); assert.equal(viewport.scrollTop, 60)
  for (const flag of ['loading', 'saving']) {
    fixture.state.active.value[flag] = true; pan(); fixture.state.active.value[flag] = false
    assert.equal(fixture.state.panning.value, false)
  }
  fixture.state.active.value.bridgeReady = false; pan()
  assert.equal(fixture.state.panning.value, false)
  fixture.message('ready'); pan(); assert.equal(fixture.state.panning.value, true)
  fixture.props.visible = false; await settle()
  assert.equal(fixture.state.panning.value, false)
  pan('move', { clientX: -10, clientY: -20 }); pan()
  assert.equal(fixture.state.panning.value, false)
  assert.equal(viewport.scrollLeft, 100); assert.equal(viewport.scrollTop, 60)
  fixture.props.visible = true; await settle()
  fixture.state.setMode('acceptance'); pan()
  assert.equal(fixture.state.panning.value, false)
  fixture.state.setMode('edit'); pan()
  assert.equal(fixture.state.panning.value, true)
  const channel = fixture.state.active.value.channel
  fixture.props.projectId = 'b'; await settle()
  pan('move', { channel, clientX: -10, clientY: -20 }); pan('end', { channel })
  assert.equal(fixture.state.panning.value, false)
  assert.equal(viewport.scrollLeft, 100); assert.equal(viewport.scrollTop, 60)
  assert.equal(fixture.state.active.value.history.length, 1)
})

test('iframe pan end applies its latest coordinates before finishing while cancel never applies a final movement', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.message('ready')
  const viewport = attachCanvas(fixture); await settle()
  fixture.message('canvas-pan', { phase: 'start', pointerId: 5, clientX: 40, clientY: 20 })
  fixture.message('canvas-pan', { phase: 'end', pointerId: 5, clientX: 20, clientY: -10 })
  await settle()
  assert.equal(fixture.state.panning.value, false)
  assert.equal(viewport.scrollLeft, 120); assert.equal(viewport.scrollTop, 90)
  fixture.message('canvas-pan', { phase: 'start', pointerId: 6, clientX: 40, clientY: 20 })
  fixture.message('canvas-pan', { phase: 'cancel', pointerId: 6, clientX: -40, clientY: -20 })
  await settle()
  assert.equal(fixture.state.panning.value, false)
  assert.equal(viewport.scrollLeft, 120); assert.equal(viewport.scrollTop, 90)
  assert.equal(fixture.state.active.value.history.length, 1)
})

test('selected-position updates only the matching element rect and preserves inspector editing references', async t => {
  const fixture = await setup(t); await settle(); fixture.select(); fixture.message('ready')
  const state = fixture.state.active.value, selected = state.selected, styles = selected.styles, draft = state.styleDraft
  draft.width = 'calc(100% - 12px)'
  const rect = { x: 42, y: 80, left: 42, top: 80, right: 142, bottom: 110, width: 100, height: 30 }
  fixture.message('selected-position', { selector: '#save', rect })
  assert.equal(state.selected, selected); assert.equal(state.selected.styles, styles); assert.equal(state.styleDraft, draft)
  assert.deepEqual(copy(selected.rect), rect)
  assert.equal(draft.width, 'calc(100% - 12px)')
  for (const extra of [{ selector: '#other' }, { channel: 'old-channel' }, { rect: { ...rect, width: NaN } }]) fixture.message('selected-position', { selector: '#save', rect: { ...rect, left: 999 }, ...extra })
  fixture.message('selected-position', { selector: '#save', rect: { ...rect, left: 999 } }, {})
  fixture.props.visible = false; await settle()
  fixture.message('selected-position', { selector: '#save', rect: { ...rect, left: 999 } })
  assert.deepEqual(copy(selected.rect), rect)
  assert.equal(state.history.length, 1)
  assert.equal(fixture.state.dirty.value, false)
})

test('narrow layouts close both auxiliary panels and opening one drawer closes the other', async t => {
  const fixture = await setup(t); await settle()
  assert.equal(fixture.state.layersOpen.value, true)
  assert.equal(fixture.state.panelOpen.value, true)
  fixture.state.workspaceEl.value = vue.markRaw({ clientWidth: 700 })
  fixture.listeners.get('resize')()
  assert.equal(fixture.browserWindow.innerWidth, 1280, 'drawer layout uses the actual workspace rather than the full window')
  assert.equal(fixture.state.layersOpen.value, false)
  assert.equal(fixture.state.panelOpen.value, false)
  fixture.state.toggleLayers()
  assert.equal(fixture.state.layersOpen.value, true)
  assert.equal(fixture.state.panelOpen.value, false)
  fixture.state.toggleProperties()
  assert.equal(fixture.state.panelOpen.value, true)
  assert.equal(fixture.state.layersOpen.value, false)
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 1)
})

test('custom viewport dimensions are bounded integers, keep logical size across zoom and persist per project', async t => {
  const storage = new Map()
  const fixture = await setup(t, {}, storage); await settle()
  fixture.state.setViewport({ width: 1440, height: 900 })
  assert.equal(fixture.state.active.value.device, 'custom')
  assert.deepEqual(copy(fixture.state.frameSize.value), { width: 1440, height: 900 })
  const persisted = storage.get('code-agent-frontend-design-v1:a')
  for (const dimensions of [{ width: 239, height: 900 }, { width: 3841, height: 900 }, { width: 1440, height: 239 }, { width: 1440, height: 2161 }, { width: 1024.5, height: 768 }, { width: '1024', height: 768 }, { width: NaN, height: 768 }, null]) fixture.state.setViewport(dimensions)
  assert.deepEqual(copy(fixture.state.frameSize.value), { width: 1440, height: 900 })
  assert.equal(storage.get('code-agent-frontend-design-v1:a'), persisted)
  await fixture.state.setZoom(150)
  assert.equal(fixture.state.canvasStyle.value.width, '1440px')
  assert.equal(fixture.state.canvasStyle.value.height, '900px')
  assert.equal(fixture.state.canvasStyle.value.transform, 'scale(1.5)')
  fixture.props.projectId = 'b'; await settle()
  assert.notEqual(fixture.state.active.value.device, 'custom')
  fixture.state.setViewport({ width: 240, height: 240 })
  assert.deepEqual(copy(fixture.state.frameSize.value), { width: 240, height: 240 })
  fixture.props.projectId = 'a'; await settle()
  assert.deepEqual(copy(fixture.state.frameSize.value), { width: 1440, height: 900 })
  assert.equal(fixture.calls.filter(call => call.name === 'designPreview').length, 2)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
  fixture.dispose()
  const restored = await setup(t, {}, storage); await settle()
  assert.equal(restored.state.active.value.device, 'custom')
  assert.deepEqual(copy(restored.state.frameSize.value), { width: 1440, height: 900 })
})

test('change-list reset removes only its addressed rule in edit mode and participates in undo without saving', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.applyDeclarations({ color: '#2255aa' })
  fixture.select('#other'); fixture.state.applyDeclarations({ width: '80px' })
  fixture.state.resetRule('#save')
  assert.deepEqual(copy(fixture.state.active.value.rules), [{ selector: '#other', declarations: { width: '80px' } }])
  assert.equal(fixture.state.active.value.selected.selector, '#other')
  fixture.state.undo()
  assert.equal(fixture.state.active.value.rules.length, 2)
  const history = fixture.state.active.value.historyIndex
  fixture.state.resetRule('#missing')
  assert.equal(fixture.state.active.value.historyIndex, history)
  fixture.state.setMode('acceptance'); fixture.state.resetRule('#save')
  assert.equal(fixture.state.active.value.rules.length, 2)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('style presets snapshot only explicit overrides and merge into another selection through normal history', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.savePreset('只有计算样式')
  assert.deepEqual(copy(fixture.state.active.value.stylePresets), [], 'computed typography and colors are not persisted as overrides')
  fixture.state.applyDeclarations({ color: '#2255aa', width: '120px' })
  fixture.state.savePreset('  主要按钮  ')
  const preset = copy(fixture.state.active.value.stylePresets[0])
  assert.equal(preset.name, '主要按钮')
  assert.deepEqual(preset.declarations, { color: '#2255aa', width: '120px' })
  assert.equal(Object.hasOwn(preset.declarations, 'font-size'), false)
  fixture.state.applyDeclarations({ color: '#ff0000' })
  assert.deepEqual(copy(fixture.state.active.value.stylePresets[0]), preset, 'later edits cannot mutate the stored preset snapshot')
  fixture.select('#other'); fixture.state.applyDeclarations({ opacity: '.5' })
  fixture.state.applyPreset(preset.id)
  assert.deepEqual(copy(fixture.state.active.value.rules.find(rule => rule.selector === '#other').declarations), { opacity: '.5', color: '#2255aa', width: '120px' })
  fixture.state.undo()
  assert.deepEqual(copy(fixture.state.active.value.rules.find(rule => rule.selector === '#other').declarations), { opacity: '.5' })
  fixture.state.setMode('acceptance'); fixture.state.applyPreset(preset.id)
  assert.deepEqual(copy(fixture.state.active.value.rules.find(rule => rule.selector === '#other').declarations), { opacity: '.5' })
  assert.equal(fixture.events.some(event => event[0] === 'ask'), false)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('presets are project-local, survive reload, normalize persisted declarations and remove by owned id', async t => {
  const storage = new Map([['code-agent-frontend-design-v1:a', JSON.stringify({ stylePresets: [{ id: 'restored', name: '旧预设', declarations: { color: '#123456', position: 'fixed', width: 'url(https://example.invalid)' } }, { id: '<bad>', name: '无效', declarations: { color: 'red' } }] })]])
  const fixture = await setup(t, {}, storage); await settle(); fixture.select()
  assert.deepEqual(copy(fixture.state.active.value.stylePresets), [{ id: 'restored', name: '旧预设', declarations: { color: '#123456' } }])
  fixture.props.projectId = 'b'; await settle(); fixture.select()
  assert.deepEqual(copy(fixture.state.active.value.stylePresets), [])
  fixture.state.applyPreset('restored'); fixture.state.removePreset('restored')
  assert.equal(fixture.state.active.value.rules.length, 0)
  assert.equal(fixture.state.contexts.get('a').stylePresets.length, 1)
  fixture.props.projectId = 'a'; await settle()
  fixture.state.removePreset('restored')
  assert.deepEqual(copy(fixture.state.active.value.stylePresets), [])
  assert.deepEqual(JSON.parse(storage.get('code-agent-frontend-design-v1:a')).stylePresets, [])
  fixture.dispose()
  const restored = await setup(t, {}, storage); await settle()
  assert.deepEqual(copy(restored.state.active.value.stylePresets), [])
})

test('acceptance bulk additions deduplicate normalized titles and enforce the project limit', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.addCheck('  主按钮反馈  ')
  fixture.state.addChecks(['主按钮反馈', ' 表单错误提示 ', '表单错误提示', '', '   ', null, 12])
  assert.deepEqual(fixture.state.active.value.checks.map(check => check.title), ['主按钮反馈', '表单错误提示'])
  fixture.state.active.value.checkDraft = '旧版输入草稿'; fixture.state.addCheck()
  assert.equal(fixture.state.active.value.checks.at(-1).title, '旧版输入草稿')
  assert.equal(fixture.state.active.value.checkDraft, '')
  fixture.state.addChecks(Array.from({ length: 110 }, (_, index) => `验收项 ${index}`))
  assert.equal(fixture.state.active.value.checks.length, 100)
  assert.equal(new Set(fixture.state.active.value.checks.map(check => check.id)).size, 100)
  fixture.state.addCheck('超过上限')
  assert.equal(fixture.state.active.value.checks.length, 100)
  assert.equal(JSON.parse(fixture.storage.get('code-agent-frontend-design-v1:a')).checks.length, 100)
  assert.equal(fixture.events.some(event => event[0] === 'ask'), false)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('acceptance item updates and states resolve ids only inside the active project and bound persisted text', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.addCheck('需要检查')
  const firstId = fixture.state.active.value.checks[0].id
  fixture.state.updateCheck({ id: firstId, title: '新'.repeat(300), note: '记'.repeat(800) })
  assert.equal(fixture.state.active.value.checks[0].title.length, 240)
  assert.equal(fixture.state.active.value.checks[0].note.length, 600)
  fixture.state.changeCheckState({ id: firstId, state: 'passed' })
  assert.equal(fixture.state.passCount.value, 1)
  fixture.state.changeCheckState({ id: firstId, state: 'other' })
  assert.equal(fixture.state.active.value.checks[0].state, 'passed')
  const original = copy(fixture.state.active.value.checks[0])
  fixture.props.projectId = 'b'; await settle()
  fixture.state.updateCheck({ id: firstId, title: '不可跨项目修改', note: '不可跨项目修改' })
  fixture.state.changeCheckState({ id: firstId, state: 'failed' })
  assert.deepEqual(copy(fixture.state.contexts.get('a').checks[0]), original)
  fixture.state.addCheck('B 项目验收')
  const own = fixture.state.active.value.checks[0]
  fixture.state.changeCheckState({ id: own.id, state: 'failed' })
  fixture.state.updateCheck({ id: own.id, note: '仅更新备注' })
  assert.equal(own.title, 'B 项目验收')
  assert.equal(own.note, '仅更新备注')
  assert.equal(fixture.state.failureCount.value, 1)
})

test('CSS copy and export use only controlled rules, permit empty managed CSS and never save project files', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.exportCss()
  await fixture.state.copyCss()
  assert.equal(fixture.downloads.length, 1)
  assert.match(fixture.downloads[0].filename, /\.css$/)
  assert.deepEqual(parseDesignCss(fixture.downloads[0].text), [])
  assert.equal(fixture.clipboard[0], serializeDesignCss([]))
  fixture.select(); fixture.state.applyDeclarations({ color: '#2255aa' })
  fixture.state.exportCss(); await fixture.state.copyCss()
  const currentCss = serializeDesignCss(fixture.state.active.value.rules)
  assert.equal(fixture.downloads.at(-1).text, currentCss)
  assert.equal(fixture.clipboard.at(-1), currentCss)
  assert.equal(fixture.state.dirty.value, true)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
  assert.equal(fixture.events.some(event => event[0] === 'ask' || event[0] === 'saved'), false)
})

test('acceptance export includes current project records while clipboard failure remains recoverable', async t => {
  const fixture = await setup(t); await settle()
  fixture.state.addCheck('按钮 <反馈>')
  const own = fixture.state.active.value.checks[0]
  fixture.state.changeCheckState({ id: own.id, state: 'failed' })
  fixture.state.updateCheck({ id: own.id, note: '未出现预期提示' })
  fixture.state.exportChecks()
  assert.equal(fixture.downloads.length, 1)
  assert.match(fixture.downloads[0].filename, /\.md$/)
  assert.match(fixture.downloads[0].text, /index\.html/)
  assert.match(fixture.downloads[0].text, /失败/)
  assert.match(fixture.downloads[0].text, /未出现预期提示/)
  assert.equal(fixture.downloads[0].text.includes('<反馈>'), false)
  fixture.browserWindow.navigator.clipboard.writeText = async () => { throw new Error('clipboard denied') }
  await fixture.state.copyCss()
  assert.ok(fixture.state.active.value.notice)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

function aiReply(fixture, declarations = { color: '#2255aa', 'font-size': '20px' }) {
  const state = fixture.state.active.value
  return { projectId: state.projectId, text: JSON.stringify({ kind: 'frontend-style', request_id: state.pendingAi.requestId, selector: state.pendingAi.selector, declarations, summary: '预览提案' }) }
}

test('AI phase changes only after explicit requests or matching replies and times out without applying', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  const state = fixture.state.active.value
  assert.equal(state.aiPhase, 'idle')
  state.aiInstruction = '改善当前样式'; fixture.state.requestAi()
  assert.equal(state.aiPhase, 'waiting')
  fixture.state.requestAi()
  assert.equal(fixture.events.filter(event => event[0] === 'ask').length, 1, 'a waiting request cannot be dispatched twice')
  const timer = [...fixture.timers.entries()].find(([, value]) => value.delay === 180000)
  assert.ok(timer, 'AI requests have a bounded three-minute waiting timer')
  const reply = aiReply(fixture)
  fixture.state.receiveAi({ ...reply, projectId: 'b' })
  assert.equal(state.aiPhase, 'waiting')
  fixture.timers.delete(timer[0]); timer[1].fn(); await settle()
  assert.equal(state.aiPhase, 'idle')
  assert.match(state.aiNotice, /超时|未返回|未收到/)
  assert.equal(state.rules.length, 0)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('AI application accepts only an unchanged nonempty subset of the matching proposal and preserves other overrides', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  fixture.state.applyDeclarations({ width: '120px' })
  const state = fixture.state.active.value
  state.aiInstruction = '更醒目'; fixture.state.requestAi()
  assert.equal(state.aiPhase, 'waiting')
  fixture.state.receiveAi(aiReply(fixture))
  assert.equal(state.aiPhase, 'ready')
  assert.deepEqual(copy(state.rules[0].declarations), { width: '120px' })
  const history = state.historyIndex
  for (const subset of [{}, { color: '#ff0000' }, { color: '#2255aa', height: '20px' }, { color: '#2255aa', position: 'fixed' }]) fixture.state.applyAi(subset)
  assert.equal(state.historyIndex, history)
  assert.equal(state.aiPhase, 'ready')
  for (const identity of ['channel', 'projectId']) {
    const original = state.pendingAi[identity]
    state.pendingAi[identity] = 'foreign-identity'
    fixture.state.applyAi({ color: '#2255aa' })
    assert.equal(state.historyIndex, history, `subset application retains the ${identity} identity guard`)
    state.pendingAi[identity] = original
  }
  fixture.state.applyAi({ color: '#2255aa' })
  assert.deepEqual(copy(state.rules[0].declarations), { width: '120px', color: '#2255aa' })
  assert.equal(Object.hasOwn(state.rules[0].declarations, 'font-size'), false)
  assert.equal(state.pendingAi, null)
  assert.equal(state.aiPhase, 'idle')
  fixture.state.undo()
  assert.deepEqual(copy(state.rules[0].declarations), { width: '120px' })
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('AI dismissal clears identity and persisted pending state and an obsolete timeout cannot interrupt a newer request', async t => {
  const fixture = await setup(t); await settle(); fixture.select()
  const state = fixture.state.active.value
  state.aiInstruction = '第一个请求'; fixture.state.requestAi()
  const oldTimer = [...fixture.timers.values()].find(value => value.delay === 180000)
  const firstRequest = state.pendingAi.requestId
  fixture.state.dismissAi()
  state.aiInstruction = '第二个请求'; fixture.state.requestAi()
  assert.notEqual(state.pendingAi.requestId, firstRequest)
  const secondRequest = state.pendingAi.requestId
  oldTimer.fn()
  assert.equal(state.aiPhase, 'waiting')
  assert.equal(state.pendingAi.requestId, secondRequest)
  assert.ok([...fixture.timers.values()].some(value => value.delay === 180000), 'an obsolete callback cannot remove the newer request timer')
  fixture.state.receiveAi(aiReply(fixture))
  assert.equal(state.aiPhase, 'ready')
  fixture.state.dismissAi()
  assert.equal(state.pendingAi, null)
  assert.equal(state.aiProposal, null)
  assert.equal(state.aiPhase, 'idle')
  assert.equal(JSON.parse(fixture.storage.get('code-agent-frontend-design-v1:a')).pendingAi, null)
  oldTimer.fn()
  assert.equal(state.aiPhase, 'idle')
  assert.equal(state.rules.length, 0)
})

async function transformFixture(t, propOverrides = {}) {
  const fixture = await setup(t, {}, new Map(), propOverrides)
  await settle(); fixture.select(); fixture.message('ready'); await settle()
  return fixture
}
const transformMessage = (fixture, phase, gestureId = 'gesture-1', declarations, selector = '#save') => fixture.message('transform', { phase, gestureId, selector, ...(declarations ? { declarations } : {}) })
const transformResult = (fixture, gestureId = 'gesture-1') => fixture.posted.filter(item => item.message.type === 'transform-result' && item.message.gestureId === gestureId).at(-1)?.message
const interaction = fixture => fixture.posted.filter(item => item.message.type === 'interaction').at(-1)?.message

test('ready and mode, zoom and pan changes synchronize actual canvas interaction without gating readonly previews', async t => {
  const fixture = await setup(t, {}, new Map(), { readonly: true }); await settle()
  fixture.message('ready')
  assert.deepEqual(interaction(fixture), { channel: fixture.state.active.value.channel, type: 'interaction', enabled: false, panEnabled: false, zoom: 100 })
  fixture.select(); await settle()
  assert.equal(interaction(fixture).enabled, true)
  await fixture.state.setZoom(150); await settle()
  assert.equal(interaction(fixture).zoom, 150)
  assert.equal(interaction(fixture).enabled, true)
  fixture.state.active.value.canvasTool = 'pan'; await settle()
  assert.equal(interaction(fixture).enabled, false)
  assert.equal(interaction(fixture).panEnabled, true)
  fixture.state.active.value.canvasTool = 'select'; fixture.state.spaceHeld.value = true; await settle()
  assert.equal(interaction(fixture).enabled, false)
  assert.equal(interaction(fixture).panEnabled, true)
  fixture.state.spaceHeld.value = false; await settle()
  assert.equal(interaction(fixture).enabled, true)
  fixture.state.setMode('acceptance'); await settle()
  assert.equal(interaction(fixture).enabled, false)
})

test('transform start records a CSS and selection snapshot without rules, history, persistence or project writes', async t => {
  const fixture = await transformFixture(t), state = fixture.state.active.value
  fixture.state.applyDeclarations({ width: '120px' })
  const rules = copy(state.rules), history = copy(state.history), index = state.historyIndex, stored = fixture.storage.get('code-agent-frontend-design-v1:a')
  const selected = copy(state.selected)
  transformMessage(fixture, 'start')
  assert.equal(fixture.state.transformInProgress.value, true)
  assert.deepEqual(copy(state.transformGesture), { id: 'gesture-1', selector: '#save', channel: state.channel, baseCss: serializeDesignCss(rules), startSelection: selected })
  assert.deepEqual(copy(state.rules), rules)
  assert.deepEqual(copy(state.history), history)
  assert.equal(state.historyIndex, index)
  assert.equal(fixture.storage.get('code-agent-frontend-design-v1:a'), stored)
  fixture.message('selected', { element: { ...element(), styles: { width: '176px', color: '#000000', 'font-size': '14px' }, rect: { ...element().rect, width: 176, right: 186 } } })
  assert.equal(state.styleDraft.width, '176px', 'the live drag dimensions take precedence over existing 120px overrides')
  assert.deepEqual(copy(state.rules), rules)
  assert.equal(fixture.storage.get('code-agent-frontend-design-v1:a'), stored)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
  assert.equal(fixture.events.some(event => event[0] === 'ask'), false)
})

test('one transform commit creates one undo entry and posts canonical styles before its acceptance', async t => {
  const fixture = await transformFixture(t), state = fixture.state.active.value
  const before = copy(state.rules), history = state.history.length
  transformMessage(fixture, 'start')
  const marker = fixture.posted.length, declarations = { translate: '24px -10px', width: '140px', height: '44px' }
  transformMessage(fixture, 'commit', 'gesture-1', declarations)
  assert.equal(state.transformGesture, null)
  assert.equal(state.history.length, history + 1)
  assert.deepEqual(copy(state.rules), [{ selector: '#save', declarations }])
  assert.equal(fixture.state.dirty.value, true)
  const posted = fixture.posted.slice(marker).map(item => item.message)
  const ack = posted.findIndex(message => message.type === 'transform-result' && message.accepted)
  assert.ok(ack > 0)
  assert.ok(posted.slice(0, ack).some(message => message.type === 'styles' && message.css === serializeDesignCss(state.rules)))
  assert.equal(transformResult(fixture).accepted, true)
  assert.deepEqual(parseDesignCss(JSON.parse(fixture.storage.get('code-agent-frontend-design-v1:a')).draft.css), copy(state.rules))
  transformMessage(fixture, 'commit', 'gesture-1', declarations)
  assert.equal(transformResult(fixture).accepted, false, 'a replay cannot commit another undo step')
  assert.equal(state.history.length, history + 1)
  fixture.state.undo()
  assert.deepEqual(copy(state.rules), before)
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
})

test('an unchanged transform is acknowledged without making another undo step or discarding other properties', async t => {
  const fixture = await transformFixture(t), state = fixture.state.active.value
  fixture.state.applyDeclarations({ width: '140px', color: '#3355aa' })
  const history = state.history.length, stored = fixture.storage.get('code-agent-frontend-design-v1:a')
  transformMessage(fixture, 'start')
  transformMessage(fixture, 'commit', 'gesture-1', { width: '140px' })
  assert.equal(transformResult(fixture).accepted, true)
  assert.equal(state.transformGesture, null)
  assert.equal(state.history.length, history)
  assert.deepEqual(copy(state.rules[0].declarations), { width: '140px', color: '#3355aa' })
  assert.equal(fixture.storage.get('code-agent-frontend-design-v1:a'), stored)
})

test('cancel, Escape and undo or redo without available history roll back live selection without committing', async t => {
  const cancellations = {
    bridge: fixture => transformMessage(fixture, 'cancel'),
    Escape: fixture => fixture.state.onDesignKey(designKey({ key: 'Escape' })),
    undo: fixture => fixture.state.undo(),
    redo: fixture => fixture.state.redo(),
  }
  for (const [name, cancel] of Object.entries(cancellations)) await t.test(name, async sub => {
    const fixture = await transformFixture(sub), state = fixture.state.active.value, before = copy(state.selected)
    const stored = fixture.storage.get('code-agent-frontend-design-v1:a')
    transformMessage(fixture, 'start')
    fixture.message('selected', { element: { ...element(), styles: { width: '190px' } } })
    cancel(fixture)
    assert.equal(state.transformGesture, null)
    assert.deepEqual(copy(state.selected), before)
    assert.equal(state.styleDraft.width, '')
    assert.deepEqual(copy(state.rules), [])
    assert.equal(state.history.length, 1)
    assert.equal(fixture.storage.get('code-agent-frontend-design-v1:a'), stored)
    assert.equal(transformResult(fixture).accepted, false)
  })
})

test('selection, mode, project, visibility, loading, saving, tool and zoom changes cancel pending transforms', async t => {
  const changes = {
    selection: fixture => fixture.message('selected', { element: element('#other') }),
    clear: fixture => fixture.state.clearCanvasSelection(),
    mode: fixture => fixture.state.setMode('acceptance'),
    project: fixture => { fixture.props.projectId = 'b' },
    hidden: fixture => { fixture.props.visible = false },
    loading: fixture => { fixture.state.active.value.loading = true },
    saving: fixture => { fixture.state.active.value.saving = true },
    panTool: fixture => { fixture.state.active.value.canvasTool = 'pan' },
    space: fixture => { fixture.state.spaceHeld.value = true },
    zoom: fixture => fixture.state.setZoom(125),
    frameLoad: fixture => fixture.state.frameLoaded(),
  }
  for (const [name, change] of Object.entries(changes)) await t.test(name, async sub => {
    const fixture = await transformFixture(sub), state = fixture.state.active.value, channel = state.channel
    transformMessage(fixture, 'start')
    await change(fixture); await settle()
    assert.equal(state.transformGesture, null)
    assert.deepEqual(copy(state.rules), [])
    assert.equal(state.history.length, 1)
    assert.ok(fixture.posted.some(item => item.message.type === 'transform-result' && item.message.gestureId === 'gesture-1' && item.message.accepted === false && item.message.channel === channel))
    if (name === 'selection') assert.equal(state.selected.selector, '#other')
    if (name === 'project') assert.equal(fixture.state.active.value.projectId, 'b')
    assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
  })
})

test('starting a preview reload cancels the current transform before the new request resolves', async t => {
  const fixture = await transformFixture(t), state = fixture.state.active.value, pending = deferred()
  fixture.stubs.designPreview = () => pending.promise
  transformMessage(fixture, 'start')
  const loading = fixture.state.loadPreview()
  assert.equal(state.transformGesture, null)
  assert.equal(state.loading, true)
  assert.equal(transformResult(fixture).accepted, false)
  pending.resolve(preview('a')); await loading; await settle()
  assert.deepEqual(copy(state.rules), [])
  assert.equal(state.history.length, 1)
})

test('middle-button panning disables interaction and cancels a gesture even with the select tool active', async t => {
  const fixture = await transformFixture(t), state = fixture.state.active.value
  attachCanvas(fixture); await settle()
  transformMessage(fixture, 'start')
  fixture.state.beginPan({ button: 1, pointerId: 3, clientX: 100, clientY: 100, preventDefault() {}, currentTarget: { setPointerCapture() {} } })
  await settle()
  assert.equal(fixture.state.panning.value, true)
  assert.equal(state.transformGesture, null)
  assert.equal(interaction(fixture).enabled, false)
  transformMessage(fixture, 'start', 'during-pan')
  assert.equal(state.transformGesture, null)
  assert.equal(transformResult(fixture, 'during-pan').accepted, false)
  fixture.state.endPan(); await settle()
  assert.equal(interaction(fixture).enabled, true)
})

test('foreign sources and old channels are ignored while obsolete gesture IDs cannot replace a current transaction', async t => {
  const fixture = await transformFixture(t), state = fixture.state.active.value
  transformMessage(fixture, 'start')
  const gesture = state.transformGesture, marker = fixture.posted.length
  fixture.message('transform', { phase: 'commit', gestureId: 'gesture-1', selector: '#save', declarations: { width: '999px' } }, {})
  fixture.message('transform', { phase: 'commit', gestureId: 'gesture-1', selector: '#save', declarations: { width: '999px' }, channel: 'old-channel' })
  assert.equal(state.transformGesture, gesture)
  assert.equal(fixture.posted.length, marker)
  transformMessage(fixture, 'start', 'different-id')
  assert.equal(transformResult(fixture, 'different-id').accepted, false)
  transformMessage(fixture, 'commit', 'obsolete-id', { width: '999px' })
  assert.equal(transformResult(fixture, 'obsolete-id').accepted, false)
  assert.equal(state.transformGesture, gesture)
  transformMessage(fixture, 'cancel', 'obsolete-id')
  assert.equal(state.transformGesture, gesture)
  assert.deepEqual(copy(state.rules), [])
  transformMessage(fixture, 'commit', 'gesture-1', { width: '140px' })
  assert.equal(transformResult(fixture).accepted, true)
  assert.equal(state.rules[0].declarations.width, '140px')
})

test('malformed transform payloads cannot bypass declaration validation or alter an existing gesture', async t => {
  const fixture = await transformFixture(t), state = fixture.state.active.value
  transformMessage(fixture, 'start')
  const current = state.transformGesture, marker = fixture.posted.length
  for (const declarations of [{ color: '#ff0000' }, { width: '10%' }, { height: '0px' }, { translate: 'none' }, { translate: '1px 2px 3px' }, { width: ' 100px' }, {}]) {
    transformMessage(fixture, 'commit', 'gesture-1', declarations)
    assert.equal(state.transformGesture, current)
    assert.deepEqual(copy(state.rules), [])
  }
  fixture.message('transform', { phase: 'cancel', gestureId: 'gesture-1', selector: '#save', declarations: {} })
  assert.equal(state.transformGesture, current, 'cancellation payloads cannot carry mutation fields')
  assert.equal(fixture.posted.length, marker, 'invalid frame messages receive no actionable response')
  transformMessage(fixture, 'commit', 'gesture-1', { width: '140px' })
  assert.equal(transformResult(fixture).accepted, true)
})

test('CAS changes and the 256-rule limit reject commits and return canonical styles before rollback confirmation', async t => {
  for (const reason of ['changed-css', 'rule-limit']) await t.test(reason, async sub => {
    const fixture = await transformFixture(sub), state = fixture.state.active.value
    if (reason === 'rule-limit') state.rules = Array.from({ length: 256 }, (_, i) => ({ selector: `#node-${i}`, declarations: { color: '#000000' } }))
    transformMessage(fixture, 'start')
    if (reason === 'changed-css') state.rules = [{ selector: '#other', declarations: { color: '#3355aa' } }]
    const canonical = copy(state.rules), history = copy(state.history), marker = fixture.posted.length
    transformMessage(fixture, 'commit', 'gesture-1', { width: '140px' })
    assert.equal(state.transformGesture, null)
    assert.equal(transformResult(fixture).accepted, false)
    assert.deepEqual(copy(state.rules), canonical)
    assert.deepEqual(copy(state.history), history)
    const posted = fixture.posted.slice(marker).map(item => item.message)
    const lastAck = posted.findLastIndex(message => message.type === 'transform-result' && message.accepted === false)
    assert.ok(posted.slice(0, lastAck).some(message => message.type === 'styles' && message.css === serializeDesignCss(canonical)))
    if (reason === 'rule-limit') assert.match(state.notice, /256/)
  })
})

test('blocked start conditions cannot create a gesture and readonly commits remain preview-only', async t => {
  for (const condition of ['hidden', 'acceptance', 'loading', 'saving', 'pan', 'space', 'unready', 'wrong-selection']) await t.test(condition, async sub => {
    const fixture = await transformFixture(sub), state = fixture.state.active.value
    if (condition === 'hidden') fixture.props.visible = false
    if (condition === 'acceptance') fixture.state.setMode('acceptance')
    if (condition === 'loading') state.loading = true
    if (condition === 'saving') state.saving = true
    if (condition === 'pan') state.canvasTool = 'pan'
    if (condition === 'space') fixture.state.spaceHeld.value = true
    if (condition === 'unready') state.bridgeReady = false
    await settle()
    transformMessage(fixture, 'start', 'blocked-start', undefined, condition === 'wrong-selection' ? '#other' : '#save')
    assert.equal(state.transformGesture, null)
    assert.equal(transformResult(fixture, 'blocked-start').accepted, false)
    assert.deepEqual(copy(state.rules), [])
  })
  const fixture = await transformFixture(t, { readonly: true }), state = fixture.state.active.value
  transformMessage(fixture, 'start')
  transformMessage(fixture, 'commit', 'gesture-1', { translate: '10px 20px' })
  assert.equal(transformResult(fixture).accepted, true)
  assert.equal(fixture.state.dirty.value, true)
  await fixture.state.saveStyles()
  assert.equal(fixture.calls.some(call => call.name === 'saveDesignStyles'), false)
  assert.equal(state.rules[0].declarations.translate, '10px 20px')
})
