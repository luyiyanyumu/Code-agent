import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync, existsSync } from 'node:fs'
import * as vue from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import { api } from '../src/api.js'
import * as display from '../src/lib/businessMeaningDisplay.js'
import { appendBusinessMeaning, createBusinessMeaningSource, notifyBusinessMeaningChanged } from '../src/lib/businessMeaning.js'
import { kindMeta, layoutFeatureTree, layoutStructure } from '../src/lib/mapLayout.js'

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = async () => { await vue.nextTick(); for (let i = 0; i < 10; i++) await Promise.resolve() }
const source = readFileSync(new URL('../src/components/code/BusinessMeaning.vue', import.meta.url), 'utf8')
const entity = { id: 'sym:Service.java#list', kind: 'method', path: 'Service.java', name: 'list', tree: 'main' }
const record = (meaning = '查询可见文档', extra = {}) => ({ entity, business: { business_meaning: meaning, detail: '', inputs: [], outputs: [], rules: [], effects: [], feature_refs: [], status: 'current', source: 'human', version: 'a'.repeat(40), ...extra } })

async function setup(t, overrides = {}, initialProps = {}) {
  const original = api.code
  const calls = [], emitted = [], listeners = new Map()
  const stubs = { symbolBusiness: async () => record(), saveSymbolBusiness: async (_, body) => record(body.business_meaning, body), ...overrides }
  api.code = new Proxy(stubs, { get: (target, name) => (...args) => { calls.push({ name, args }); if (!target[name]) throw new Error(`Unstubbed API: ${String(name)}`); return target[name](...args) } })
  const props = vue.reactive({ projectId: 'project-a', entity: { ...entity }, ...initialProps })
  const hooks = { mount: [], unmount: [] }
  const bindings = { ...Object.fromEntries(Object.entries(vue).filter(([key]) => /^[\w$]+$/.test(key))), ...display, api, defineProps: () => props, defineEmits: () => (...args) => emitted.push(args),
    onMounted: fn => hooks.mount.push(fn), onBeforeUnmount: fn => hooks.unmount.push(fn),
    window: { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) } }
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import[^\n]+\r?\n/gm, '')
  const scope = vue.effectScope()
  const state = scope.run(() => new Function(...Object.keys(bindings), `${script}\nreturn { record, business, statusLabel, sourceLabel, ready, loading, saving, editing, error, notice, draft, load, edit, cancel, save, key, onBusinessChanged, entityId, rowGroups }`)(...Object.values(bindings)))
  hooks.mount.forEach(fn => fn())
  t.after(() => { hooks.unmount.forEach(fn => fn()); scope.stop(); api.code = original })
  await settle()
  return { state, props, calls, emitted, stubs, listeners, unmount: () => hooks.unmount.forEach(fn => fn()) }
}

test('domains map to the same module record, while presentation-only nodes have no business identity', () => {
  assert.equal(display.businessEntityId({ id: 'fdomain:backend/service' }), 'mod:backend/service')
  assert.equal(display.businessEntityId({ id: 'fdomain:根目录' }), 'mod:')
  assert.equal(display.businessEntityId({ id: 'sym:Service.java#list' }), entity.id)
  assert.equal(display.businessEntityId({ id: 'feature-root' }), '')
})

test('stale/missing business values cannot leak and a model candidate cannot claim confirmation', () => {
  for (const status of ['stale', 'missing', '__proto__']) {
    const value = display.displayBusiness(record('旧业务', { status, detail: '旧细节', inputs: [{ name: 'id', meaning: '旧参数' }], evidence: ['旧证据'] }).business)
    assert.equal(value.business_meaning, '')
    assert.equal(value.detail, '')
    assert.deepEqual(value.inputs, [])
    assert.deepEqual(value.evidence, [])
  }
  assert.equal(display.displayBusiness(record('候选', { status: 'current', source: 'model' }).business).status, 'unreviewed')
  assert.equal(display.displayBusiness(record('候选', { status: 'unreviewed', source: 'comment' }).business).status, 'unreviewed')
})

test('human confirmation preserves optional fields and sends the current code version', async t => {
  const fixture = await setup(t, { symbolBusiness: async () => record('源码候选', { status: 'unreviewed', source: 'comment', inputs: [{ name: 'categoryId', meaning: '分类编号，空表示全部' }] }) })
  const s = fixture.state
  assert.equal(s.sourceLabel.value, '源码功能描述候选')
  assert.equal(s.statusLabel.value, '待确认')
  s.edit()
  s.draft.value.business_meaning = '  查询用户可见文档  '
  s.draft.value.detail = '权限校验后按分类过滤'
  s.draft.value.outputs.push({ name: 'documents', meaning: '当前用户可见的文档列表' }, { name: '', meaning: '' })
  s.draft.value.rules = '空分类表示全部\n\n只返回有权限的文档'
  s.draft.value.effects = '记录访问日志'
  s.draft.value.feature_refs = '文档查询'
  await s.save()
  const save = fixture.calls.find(call => call.name === 'saveSymbolBusiness')
  assert.deepEqual(save.args, ['project-a', { id: entity.id, tree: 'main', version: 'a'.repeat(40), business_meaning: '查询用户可见文档', detail: '权限校验后按分类过滤', inputs: [{ name: 'categoryId', meaning: '分类编号，空表示全部' }], outputs: [{ name: 'documents', meaning: '当前用户可见的文档列表' }], rules: ['空分类表示全部', '只返回有权限的文档'], effects: ['记录访问日志'], feature_refs: ['文档查询'] }])
  assert.equal(s.editing.value, false)
  assert.equal(s.saving.value, false)
  assert.equal(fixture.emitted[0][0], 'saved')
})

test('empty summaries and rejected writes keep the human draft available for correction', async t => {
  const fixture = await setup(t, { saveSymbolBusiness: async () => { throw new Error('代码版本已变化，请刷新') } })
  const s = fixture.state
  s.edit(); s.draft.value.business_meaning = ' '
  await s.save()
  assert.equal(fixture.calls.some(call => call.name === 'saveSymbolBusiness'), false)
  assert.match(s.error.value, /请填写/)
  s.draft.value.business_meaning = '新的业务说明'
  await s.save()
  assert.equal(s.editing.value, true)
  assert.equal(s.draft.value.business_meaning, '新的业务说明')
  assert.match(s.error.value, /版本已变化/)
})

test('changing a project during load or save cannot display the previous project business', async t => {
  const oldRead = deferred(), oldSave = deferred()
  let reads = 0
  const fixture = await setup(t, { symbolBusiness: pid => pid === 'project-a' ? ++reads === 1 ? Promise.resolve(record()) : oldRead.promise : Promise.resolve(record('项目 B 的业务')), saveSymbolBusiness: () => oldSave.promise })
  const reading = fixture.state.load()
  fixture.state.edit(); fixture.state.draft.value.business_meaning = '项目 A 编辑稿'
  const saving = fixture.state.save()
  fixture.props.projectId = 'project-b'; fixture.props.entity.tree = 'work'
  await settle()
  oldRead.resolve(record('旧读取')); oldSave.resolve(record('旧保存'))
  await saving; await reading; await settle()
  assert.equal(fixture.state.business.value.business_meaning, '项目 B 的业务')
  assert.equal(fixture.state.editing.value, false)
  assert.equal(fixture.state.draft.value.business_meaning, '')
  assert.equal(fixture.emitted.length, 0)
})

test('a failed business read cannot submit an unversioned write and can be retried', async t => {
  const fixture = await setup(t, { symbolBusiness: async () => { throw new Error('读取失败') } })
  assert.equal(fixture.state.ready.value, false)
  assert.equal(fixture.state.record.value, null)
  fixture.state.edit(); fixture.state.draft.value.business_meaning = '读取失败时的内容'
  await fixture.state.save()
  assert.equal(fixture.calls.some(call => call.name === 'saveSymbolBusiness'), false)
  assert.match(fixture.state.error.value, /先成功读取/)
  fixture.stubs.symbolBusiness = async () => record()
  await fixture.state.load()
  assert.equal(fixture.state.ready.value, true)
})

test('newer reads win, a late read cannot overwrite a save, and double-submit writes once', async t => {
  const fixture = await setup(t)
  const first = deferred(), second = deferred(), written = deferred()
  let reads = 0
  fixture.stubs.symbolBusiness = () => ++reads === 1 ? first.promise : second.promise
  const old = fixture.state.load(), newer = fixture.state.load()
  second.resolve(record('新的读取')); await newer
  first.resolve(record('旧的读取')); await old
  assert.equal(fixture.state.business.value.business_meaning, '新的读取')
  const late = deferred()
  fixture.stubs.symbolBusiness = () => late.promise
  const read = fixture.state.load()
  fixture.stubs.saveSymbolBusiness = () => written.promise
  fixture.state.edit(); fixture.state.draft.value.business_meaning = '人工保存'
  const save = fixture.state.save(); await fixture.state.save()
  written.resolve(record('人工保存')); await save
  late.resolve(record('迟到读取')); await read
  assert.equal(fixture.state.business.value.business_meaning, '人工保存')
  assert.equal(fixture.calls.filter(call => call.name === 'saveSymbolBusiness').length, 1)
})

test('shared changes refresh the record without overwriting an open human draft, and listeners clean up', async t => {
  const fixture = await setup(t)
  fixture.state.edit(); fixture.state.draft.value.business_meaning = '未保存稿'
  fixture.stubs.symbolBusiness = async () => record('另一视图更新')
  const handler = fixture.listeners.get('code-agent:business-meaning-changed')
  handler({ detail: { projectId: 'different', id: entity.id, tree: 'main' } })
  assert.equal(fixture.calls.filter(call => call.name === 'symbolBusiness').length, 1)
  handler({ detail: { projectId: 'project-a', id: entity.id, tree: 'main' } })
  await settle()
  assert.equal(fixture.state.business.value.business_meaning, '另一视图更新')
  assert.equal(fixture.state.draft.value.business_meaning, '未保存稿')
  assert.match(fixture.state.notice.value, /编辑内容保留/)
  fixture.unmount()
  assert.equal(fixture.listeners.size, 0)
})

test('the real business template distinguishes candidates and safely renders all optional fields', async t => {
  const fixture = await setup(t, { symbolBusiness: async () => record('<script>候选</script>', { status: 'unreviewed', source: 'model', detail: '**字面说明**', inputs: [{ name: 'id', meaning: '空表示全部' }], outputs: [{ name: 'result', meaning: '查询结果' }], rules: ['只返回授权数据'], effects: ['记录日志'], feature_refs: ['文档查询'] }) })
  const descriptor = parse(source).descriptor
  const script = compileScript(descriptor, { id: 'business-template' })
  const template = compileTemplate({ source: descriptor.template.content, filename: 'BusinessMeaning.vue', id: 'business-template', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const bindings = { ...Object.fromEntries(Object.entries(vue).filter(([key]) => /^[\w$]+$/.test(key))) }
  const code = template.code.replace(/import \{([^}]+)\} from ["']vue["']/g, (_, imports) => { for (const entry of imports.split(',')) { const [name, alias = name] = entry.trim().split(/\s+as\s+/); bindings[alias] = vue[name] } return '' }).replace('export function render', 'return function render')
  const render = new Function(...Object.keys(bindings), code)(...Object.values(bindings))
  const setupState = vue.proxyRefs({ ...fixture.state, ...display, props: fixture.props })
  const app = vue.createSSRApp({ render: () => render({}, [], fixture.props, setupState, {}, {}) })
  const html = await renderToString(app)
  assert.match(html, /功能描述/); assert.match(html, /模型自动补充候选/); assert.match(html, /尚未人工确认/)
  assert.doesNotMatch(html, /业务含义|补描述/)
  assert.match(html, /&lt;script&gt;候选&lt;\/script&gt;/); assert.doesNotMatch(html, /<script>|已确认/)
  for (const text of ['输入与参数', '空表示全部', '输出与返回值', '查询结果', '只返回授权数据', '记录日志', '文档查询']) assert.ok(html.includes(text), text)
})

test('batch function updates stay scoped to one project/tree and preserve the active manual draft', async t => {
  const fixture = await setup(t)
  const handler = fixture.listeners.get('code-agent:business-meaning-changed')
  fixture.state.edit()
  fixture.state.draft.value.business_meaning = '人工未保存功能'
  fixture.state.draft.value.inputs = [{ name: 'categoryId', meaning: '人工参数约定' }]
  fixture.stubs.symbolBusiness = async () => record('自动生成的功能', { source: 'model', status: 'unreviewed' })
  for (const detail of [{ projectId: 'project-b', tree: 'main' }, { projectId: 'project-a', tree: 'work' }, { projectId: 'project-a', tree: 'main', id: 'sym:Other.java#list' }]) handler({ detail })
  assert.equal(fixture.calls.filter(call => call.name === 'symbolBusiness').length, 1)
  handler({ detail: { projectId: 'project-a', tree: 'main', automatic: true } })
  await settle()
  assert.equal(fixture.state.business.value.business_meaning, '自动生成的功能')
  assert.equal(fixture.state.business.value.status, 'unreviewed')
  assert.equal(fixture.state.editing.value, true)
  assert.equal(fixture.state.draft.value.business_meaning, '人工未保存功能')
  assert.deepEqual(fixture.state.draft.value.inputs, [{ name: 'categoryId', meaning: '人工参数约定' }])
  assert.match(fixture.state.notice.value, /功能描述已更新.*编辑内容保留/)
  await fixture.state.save()
  assert.equal(fixture.state.business.value.business_meaning, '人工未保存功能')
  assert.equal(fixture.state.business.value.source, 'human')
})

test('late automatic function refresh cannot replace a manual save', async t => {
  const fixture = await setup(t)
  const automatic = deferred()
  fixture.stubs.symbolBusiness = () => automatic.promise
  fixture.state.edit()
  fixture.state.draft.value.business_meaning = '人工确认功能'
  fixture.state.onBusinessChanged({ detail: { projectId: 'project-a', tree: 'main', id: '' } })
  await fixture.state.save()
  automatic.resolve(record('迟到的自动描述', { source: 'model', status: 'unreviewed' }))
  await settle()
  assert.equal(fixture.state.business.value.business_meaning, '人工确认功能')
  assert.equal(fixture.state.business.value.source, 'human')
})

test('function hover is named consistently, marked as a candidate, and shown exactly once', () => {
  const base = { sections: [{ kind: 'markdown', value: 'Java 类型说明' }] }
  const automatic = record('自动说明', { source: 'model', status: 'current' })
  const hover = appendBusinessMeaning(appendBusinessMeaning(base, automatic), record('更新后的自动说明', { source: 'model', status: 'unreviewed' }))
  const text = hover.sections.map(section => section.value).join('\n')
  assert.equal(hover.sections.length, 2)
  assert.equal((text.match(/功能描述/g) || []).length, 1)
  assert.match(text, /功能描述（待确认）.*更新后的自动说明/)
  assert.match(text, /模型自动补充候选/)
  assert.doesNotMatch(text, /业务含义|已确认/)
  for (const status of ['stale', 'missing']) {
    const value = appendBusinessMeaning(base, record('过时功能', { status })).businessSection.value
    assert.match(value, /自动更新|待自动补充/)
    assert.doesNotMatch(value, /过时功能/)
  }
})

test('automatic batch notifications invalidate the editor function cache and discard older pending reads', async t => {
  const pending = deferred()
  const model = { getLanguageId: () => 'java', getVersionId: () => 1, isDisposed: () => false }
  const context = { projectId: 'project-a', tree: 'main', path: entity.path, uri: 'file:///Service.java', textForUri: () => 'class Service {}' }
  let reads = 0
  const source = createBusinessMeaningSource({ contextForModel: () => context, targetForModel: () => ({ name: entity.name, declaration: { lineNumber: 1, column: 7 } }), resolve: () => ++reads === 1 ? pending.promise : Promise.resolve(record('最新功能', { status: 'unreviewed', source: 'model' })) })
  t.after(() => source.dispose())
  const base = { sections: [{ kind: 'markdown', value: 'Java 类' }] }
  const old = source.provide(model, { lineNumber: 1, column: 7 }, base)
  await settle()
  notifyBusinessMeaningChanged({ projectId: 'project-a', tree: 'main', automatic: true })
  const fresh = await source.provide(model, { lineNumber: 1, column: 7 }, base)
  assert.match(fresh.businessSection.value, /最新功能/)
  pending.resolve(record('旧功能')); assert.equal(await old, base)
  await source.provide(model, { lineNumber: 1, column: 7 }, base)
  assert.equal(reads, 2, 'the refreshed record is cached after the batch update')
})

test('refreshing a known cross-file function reuses its exact declaration without a new language request', async t => {
  let definitions = 0, reads = 0
  const payloads = []
  const model = { getLanguageId: () => 'java', getVersionId: () => 1, isDisposed: () => false }
  const context = { projectId: 'project-a', tree: 'main', path: 'Caller.java', uri: 'file:///Caller.java',
    client: { ready: true, capabilities: { definitionProvider: true }, definition: async () => { definitions++; return [{ uri: 'file:///Service.java', range: { start: { line: 3, character: 8 } } }] } },
    pathForUri: uri => uri === 'file:///Service.java' ? 'Service.java' : '', textForUri: () => 'class Service {}' }
  const source = createBusinessMeaningSource({ contextForModel: () => context, targetForModel: () => ({ name: 'list' }), resolve: async (_, payload) => { reads++; payloads.push(payload); return record(reads === 1 ? '' : '自动补充的功能', { source: 'model', status: reads === 1 ? 'missing' : 'unreviewed' }) } })
  t.after(() => source.dispose())
  const base = { sections: [{ kind: 'markdown', value: 'Java 方法' }] }, position = { lineNumber: 1, column: 3 }
  const initial = await source.provide(model, position, base)
  assert.equal(definitions, 1)
  notifyBusinessMeaningChanged({ projectId: 'project-a', tree: 'main', automatic: true })
  const refreshed = await source.provide(model, position, { ...base, businessLookup: initial.businessLookup, skipBusinessDefinition: true })
  assert.equal(definitions, 1)
  assert.equal(reads, 2)
  assert.deepEqual(payloads[1], payloads[0])
  assert.match(refreshed.businessSection.value, /自动补充的功能/)
})

test('a method function appears below its title and before technical details or long JDT docs', () => {
  const tokenSection = { kind: 'markdown', value: '`raw` · **方法**\n\n**作用：** 内联打开原文。\n\n**返回类型：** `ResponseEntity<Resource>`' }
  const signature = { kind: 'code', language: 'java', value: 'ResponseEntity<Resource> FileController.raw(@PathVariable Long id)' }
  const documentation = { kind: 'plaintext', value: '原始 Javadoc\n'.repeat(100) }
  const base = { tokenSection, sections: [tokenSection, signature, documentation] }
  const first = appendBusinessMeaning(base, record('按文件编号读取原文并以内联响应打开。'))
  const refreshed = appendBusinessMeaning(first, record('更新后的原文在线阅读功能。'))
  assert.equal(refreshed.sections.length, 3)
  assert.match(refreshed.sections[0].value, /^`raw` · \*\*方法\*\*\n\n\*\*功能描述：\*\* 更新后的原文在线阅读功能。/)
  assert.ok(refreshed.sections[0].value.indexOf('功能描述') < refreshed.sections[0].value.indexOf('作用：'))
  assert.deepEqual(refreshed.sections.slice(1), [signature, documentation])
  assert.equal((refreshed.contents.map(item => item.value).join('\n').match(/\*\*功能描述/g) || []).length, 1)
  assert.doesNotMatch(refreshed.sections[0].value, /按文件编号/)
  assert.deepEqual(base.sections, [tokenSection, signature, documentation])
})

test('an old backend resolve 404 visibly explains the missing function and a retry recovers', async t => {
  const model = { getLanguageId: () => 'java', getVersionId: () => 1, isDisposed: () => false }
  const context = { projectId: 'project-a', tree: 'main', path: 'FileController.java', uri: 'file:///FileController.java' }
  const tokenSection = { kind: 'markdown', value: '`raw` · **方法**\n\n**作用：** 内联打开原文。' }
  const base = { tokenSection, sections: [tokenSection] }
  let reads = 0
  const source = createBusinessMeaningSource({ contextForModel: () => context,
    targetForModel: () => ({ name: 'raw', declaration: { lineNumber: 130, column: 37 } }),
    resolve: async () => { if (++reads === 1) throw new Error('NotFound'); return record('原文在线阅读功能。') } })
  t.after(() => source.dispose())
  const failed = await source.provide(model, { lineNumber: 130, column: 37 }, base)
  assert.match(failed.sections[0].value, /功能描述.*接口不可用.*重启后端/)
  assert.match(failed.sections[0].value, /作用.*内联打开原文/)
  assert.equal(failed.business, null)
  const retry = await source.provide(model, { lineNumber: 130, column: 37 }, failed)
  assert.equal(reads, 2)
  assert.match(retry.sections[0].value, /功能描述.*原文在线阅读功能/)
  assert.doesNotMatch(retry.sections[0].value, /接口不可用/)
  assert.equal((retry.sections[0].value.match(/\*\*功能描述/g) || []).length, 1)
})

test('a timed-out project declaration keeps its Java details and exposes a retryable function status', async t => {
  const pending = deferred()
  const model = { getLanguageId: () => 'java', getVersionId: () => 1, isDisposed: () => false }
  const context = { projectId: 'project-a', tree: 'main', path: 'FileController.java', uri: 'file:///FileController.java' }
  const source = createBusinessMeaningSource({ contextForModel: () => context, timeoutMs: 5,
    targetForModel: () => ({ name: 'raw', declaration: { lineNumber: 130, column: 37 } }), resolve: () => pending.promise })
  t.after(() => source.dispose())
  const base = { sections: [{ kind: 'plaintext', value: '原始语言说明' }] }
  const result = await source.provide(model, { lineNumber: 130, column: 37 }, base)
  assert.match(result.businessSection.value, /功能描述.*读取超时.*重试/)
  assert.deepEqual(result.sections.at(-1), base.sections[0])
  assert.equal(result.business, null)
  pending.resolve(record('迟到说明'))
})

test('a failed or timed-out definition lookup reports its function status without guessing a binding', async t => {
  const model = { getLanguageId: () => 'java', getVersionId: () => 1, isDisposed: () => false }
  const base = { sections: [{ kind: 'plaintext', value: 'raw 方法调用' }] }
  for (const timeout of [false, true]) {
    const context = { projectId: 'project-a', tree: 'main', path: 'Caller.java', uri: 'file:///Caller.java',
      client: { ready: true, capabilities: { definitionProvider: true }, definition: async () => {
        if (timeout) return new Promise(() => {})
        throw new Error('offline')
      } } }
    let reads = 0
    const source = createBusinessMeaningSource({ contextForModel: () => context, timeoutMs: 5,
      targetForModel: () => ({ name: 'raw', category: '方法调用' }), resolve: () => { reads++; return record() } })
    t.after(() => source.dispose())
    const result = await source.provide(model, { lineNumber: 1, column: 3 }, base)
    assert.match(result.businessSection.value, timeout ? /功能描述.*定位.*超时.*重试/ : /功能描述.*无法定位.*重试/)
    assert.equal(result.business, null)
    assert.equal(result.businessLookup, undefined)
    assert.equal(reads, 0)
  }
})

test('automatic updates in another project or worktree do not cancel the active function read or cache', async t => {
  const pending = deferred()
  const model = { getLanguageId: () => 'java', getVersionId: () => 1, isDisposed: () => false }
  const context = { projectId: 'project-a', tree: 'main', path: 'Service.java', uri: 'file:///Service.java' }
  let reads = 0, signal
  const source = createBusinessMeaningSource({ contextForModel: () => context,
    targetForModel: () => ({ name: 'list', declaration: { lineNumber: 1, column: 7 } }),
    resolve: (_pid, _payload, options) => { reads++; signal = options.signal; return pending.promise } })
  t.after(() => source.dispose())
  const base = { sections: [{ kind: 'plaintext', value: '原始语言说明' }] }
  const reading = source.provide(model, { lineNumber: 1, column: 7 }, base)
  await settle()
  notifyBusinessMeaningChanged({ projectId: 'project-b', tree: 'main', automatic: true })
  notifyBusinessMeaningChanged({ projectId: 'project-a', tree: 'worktree', automatic: true })
  assert.equal(signal.aborted, false)
  pending.resolve(record('当前项目功能'))
  const result = await reading
  assert.match(result.businessSection.value, /当前项目功能/)
  await source.provide(model, { lineNumber: 1, column: 7 }, base)
  assert.equal(reads, 1)
})

test('Java entity kinds remain visible and categorized in the structure and feature projections', () => {
  const kinds = ['record', 'annotation', 'constructor', 'field', 'parameter', 'enum_member', 'record_component']
  const symbols = kinds.map((kind, i) => ({ id: `sym:Value.java#${i}`, name: kind, kind, line: i + 1, end_line: i + 1 }))
  const structure = { id: 'mod:', kind: 'dir', name: 'root', children: [{ id: 'file:Value.java', kind: 'file', path: 'Value.java', name: 'Value.java', symbols }] }
  const layout = layoutStructure({ structure }, { expanded: { 'mod:': true, 'file:Value.java': true } })
  for (const kind of kinds) { assert.notEqual(kindMeta(kind).label, '其他'); assert.ok(layout.nodes.some(node => node.kind === kind), kind) }
  const value = record().business
  const projected = layoutFeatureTree({ id: 'fdomain:backend', kind: 'domain', name: 'backend', business: value, doc: '后端模块', count: 3, own: 1, children: [] }).nodes[0]
  assert.equal(projected.business, value); assert.equal(projected.doc, '后端模块'); assert.equal(projected.count, 3)
})

test('the removed workspace documentation tab has no component or lazy activation path', () => {
  const code = readFileSync(new URL('../src/views/CodeView.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(code, /DocView|docActivated|centerView\s*===\s*['"]doc['"]|图即文档/)
  assert.equal(existsSync(new URL('../src/components/code/DocView.vue', import.meta.url)), false)
  assert.match(code, /思维导图/)
  assert.match(code, /编辑器/)
})
