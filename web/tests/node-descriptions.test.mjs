import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as vue from 'vue'
import { api } from '../src/api.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const settle = async () => {
  await vue.nextTick()
  for (let n = 0; n < 8; n++) await Promise.resolve()
}
const documentation = { text: '处理用户需求并调用工具。\n- 读取会话上下文\n- 校验工具权限\n- 记录执行结果', line: 55, source: '类文档注释' }
const description = (name = 'AgentService') => ({
  available: true, headline: `${name}：处理用户需求并调用工具。`, path: `${name}.java`,
  documentation,
  facts: Array.from({ length: 7 }, (_, n) => ({ label: `依据 ${n + 1}`, text: `可追踪事实 ${n + 1}`, line: n + 1 })),
  notes: [], basis: '作者说明 + 源码索引',
})
const mapFixture = () => ({
  change_seq: 1, structure: { id: 'mod:', kind: 'dir', name: 'fixture', path: '', children: [] },
  modules: [], edges: { imports: [] }, stats: {}, scan: {}, warnings: [],
})

async function setup(t, component, overrides = {}, initialProps = {}) {
  const originalCodeApi = api.code
  const calls = [], events = [], timers = new Map()
  let timerId = 0, mount, unmount
  const props = vue.reactive({ ...(component === 'MindMap'
    ? { projectId: 'a', tree: 'main', diagnostics: null, overlay: null }
    : { projectId: 'a', detail: { id: 'file:AgentService.java', kind: 'file', path: 'AgentService.java', name: 'AgentService.java', tree: 'main' } }), ...initialProps })
  const stubs = {
    map: async () => mapFixture(), featureBriefs: async () => ({ missing: { total: 0 } }),
    indexStatus: async () => ({ change_seq: 1, scanning: false }), features: async () => ({ domains: [] }),
    symbolPurpose: async () => description(), symbol: async (_, id) => ({ id, kind: 'class', name: 'AgentService', path: 'AgentService.java', line: 64 }),
    symbolLogic: async () => ({ available: true, steps: [], calls: [] }),
    impact: async () => ({ risk: { level: 'low', score: 0, reasons: [] }, tests: [] }),
    behaviors: async () => ({ cards: [] }), contracts: async () => ({ has_contract: false }),
    decisions: async () => ({ items: [], kinds: [] }), provenance: async () => ({ items: [] }),
    ...overrides,
  }
  // Unknown calls fail locally rather than reaching a project or a real model.
  api.code = new Proxy(stubs, {
    get(target, name) {
      return (...args) => {
        calls.push({ name, args })
        if (!target[name]) throw new Error(`Unstubbed API: ${String(name)}`)
        return target[name](...args)
      }
    },
  })
  const bindings = {
    defineProps: () => props, defineEmits: () => (...args) => events.push(args), defineExpose() {},
    setInterval: (fn, delay) => { timers.set(++timerId, { fn, delay, interval: true }); return timerId },
    clearInterval: id => timers.delete(id),
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId },
    clearTimeout: id => timers.delete(id),
    window: { addEventListener() {}, removeEventListener() {} },
  }
  const url = new URL(`../src/components/code/${component}.vue`, import.meta.url)
  let script = readFileSync(url, 'utf8').match(/<script setup>([\s\S]*?)<\/script>/)[1]
  const imports = []
  script = script.replace(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*$/gm, (_, clause, specifier) => {
    imports.push({ clause: clause.trim(), specifier }); return ''
  })
  for (const { clause, specifier } of imports) {
    const namespace = specifier === 'vue' ? vue : specifier.endsWith('.vue') ? { default: {} } : await import(new URL(specifier, url))
    if (clause.startsWith('{')) {
      for (const entry of clause.slice(1, -1).split(',').map(s => s.trim()).filter(Boolean)) {
        const [key, local = key] = entry.split(/\s+as\s+/)
        bindings[local] = namespace[key]
      }
    } else bindings[clause] = namespace.default
  }
  bindings.onMounted = fn => { mount = fn }
  bindings.onBeforeUnmount = fn => { unmount = fn }
  const expose = component === 'MindMap'
    ? '({ hover, purposeCache, onNodeHover, onNodeLeave, holdNodeTip, tipPos, svgEl, onNodeClick, detail, view, loadMap, onBusinessChanged })'
    : '({ purpose, purposeDocumentation, logic, impact, contract, history, decisions, refresh, loadPurpose, jump, onBusinessChanged })'
  const scope = vue.effectScope()
  const state = scope.run(() => new Function(...Object.keys(bindings), `${script}\nreturn ${expose}`)(...Object.values(bindings)))
  t.after(() => { unmount?.(); scope.stop(); api.code = originalCodeApi })
  return {
    state, props, calls, events, stubs, timers,
    mount: () => mount?.(), unmount: () => unmount?.(),
    hoverTimer: async () => {
      const entry = [...timers.entries()].find(([, timer]) => timer.delay === 140 && !timer.interval)
      assert.ok(entry, 'hover debounce timer is scheduled')
      timers.delete(entry[0]); return entry[1].fn()
    },
  }
}

const fileNode = { id: 'file:AgentService.java', kind: 'file', name: 'AgentService.java', label: 'AgentService.java', path: 'AgentService.java', loc: 3101, hasKids: true }
const pointer = { clientX: 25, clientY: 25 }

test('file selection opens its explanation without navigating away from the map', async t => {
  const { state: s, events, calls } = await setup(t, 'MindMap')
  await s.onNodeClick(fileNode)
  assert.equal(s.detail.value.id, fileNode.id)
  assert.equal(s.detail.value.path, fileNode.path)
  assert.equal(s.detail.value.kind, 'file')
  assert.equal(events.some(([name]) => name === 'open-file'), false)
  assert.equal(calls.some(call => call.name === 'symbol'), false, 'a file is not a symbol')
})

test('hover preserves full documentation and facts, and successful results are reused', async t => {
  const fixture = await setup(t, 'MindMap')
  await fixture.state.onNodeHover(fileNode, pointer)
  await fixture.hoverTimer()
  assert.equal(fixture.state.hover.value.documentation.text, documentation.text)
  assert.equal(fixture.state.hover.value.documentation.line, 55)
  assert.equal(fixture.state.hover.value.documentation.source, documentation.source)
  assert.equal(fixture.state.hover.value.headline, description().headline)
  assert.ok(fixture.state.hover.value.facts.some(fact => fact.text === '可追踪事实 7'))
  fixture.state.onNodeLeave(true)
  await fixture.state.onNodeHover(fileNode, pointer)
  assert.equal(fixture.state.hover.value.loading, false)
  assert.equal(fixture.calls.filter(call => call.name === 'symbolPurpose').length, 1)
})

test('failed hover explanations are retried on the next hover rather than cached', async t => {
  const fixture = await setup(t, 'MindMap', { symbolPurpose: async () => { throw new Error('暂时无法读取') } })
  await fixture.state.onNodeHover(fileNode, pointer); await fixture.hoverTimer()
  assert.equal(fixture.state.hover.value.error, '暂时无法读取')
  assert.equal(fixture.state.purposeCache.has(fileNode.id), false)
  fixture.stubs.symbolPurpose = async () => description()
  fixture.state.onNodeLeave(true)
  await fixture.state.onNodeHover(fileNode, pointer); await fixture.hoverTimer()
  assert.equal(fixture.state.hover.value.error, '')
  assert.equal(fixture.calls.filter(call => call.name === 'symbolPurpose').length, 2)
})

test('entering the tooltip cancels its delayed close', async t => {
  const fixture = await setup(t, 'MindMap')
  await fixture.state.onNodeHover(fileNode, pointer); await fixture.hoverTimer()
  fixture.state.onNodeLeave()
  assert.ok([...fixture.timers.values()].some(timer => timer.delay === 180))
  fixture.state.holdNodeTip()
  assert.equal([...fixture.timers.values()].some(timer => timer.delay === 180), false)
  assert.equal(fixture.state.hover.value.id, fileNode.id)
})

test('a long tooltip remains above or below its trigger in a narrow canvas', async t => {
  const { state: s } = await setup(t, 'MindMap')
  s.svgEl.value = { closest: () => ({ getBoundingClientRect: () => ({ left: 0, top: 0, width: 390, height: 580 }) }) }
  const point = { clientX: 280, clientY: 310 }
  const tip = s.tipPos(point)
  assert.ok(tip.y + tip.maxHeight <= point.clientY - 6 || tip.y >= point.clientY + 6, 'the scrollable tooltip must not cover the pointer target')
  assert.ok(tip.y >= 8 && tip.y + tip.maxHeight <= 572)
})

test('same-path nodes from a new project or tree do not reuse the old explanation', async t => {
  const fixture = await setup(t, 'MindMap', { symbolPurpose: async (pid, _, tree) => description(`${pid}:${tree}`) })
  await fixture.state.onNodeHover(fileNode, pointer); await fixture.hoverTimer()
  fixture.props.projectId = 'b'; fixture.props.tree = 'work'
  await settle()
  await fixture.state.onNodeHover(fileNode, pointer); await fixture.hoverTimer()
  assert.equal(fixture.state.hover.value.headline, description('b:work').headline)
  assert.deepEqual(fixture.calls.filter(call => call.name === 'symbolPurpose').map(call => [call.args[0], call.args[2]]), [['a', 'main'], ['b', 'work']])
})

test('leaving or unmounting while a hover request is pending never reopens the tooltip', async t => {
  const pending = deferred()
  const fixture = await setup(t, 'MindMap', { symbolPurpose: () => pending.promise })
  await fixture.state.onNodeHover(fileNode, pointer)
  const loading = fixture.hoverTimer()
  fixture.state.onNodeLeave(true); fixture.unmount()
  pending.resolve(description()); await loading
  assert.equal(fixture.state.hover.value, null)
  assert.equal(fixture.state.purposeCache.size, 0)
})

test('a business save prevents an old hover response from restoring its invalidated description cache', async t => {
  const pending = deferred()
  const fixture = await setup(t, 'MindMap', { symbolPurpose: () => pending.promise })
  await fixture.state.onNodeHover(fileNode, pointer)
  const hovering = fixture.hoverTimer()
  await fixture.state.onBusinessChanged({ detail: { projectId: 'a', tree: 'main', id: fileNode.id } })
  pending.resolve(description('旧说明')); await hovering
  assert.equal(fixture.state.hover.value, null)
  assert.equal(fixture.state.purposeCache.size, 0)
})

test('module and domain names open details while their caret keeps the independent expansion action', async t => {
  const fixture = await setup(t, 'MindMap')
  await fixture.state.onNodeClick({ id: 'fdomain:backend', kind: 'domain', name: 'backend', hasKids: true })
  assert.equal(fixture.state.detail.value.id, 'fdomain:backend')
  assert.equal(fixture.calls.some(call => call.name === 'symbol'), false)
  await fixture.state.onNodeClick({ id: 'mod:backend', kind: 'dir', path: 'backend', name: 'backend', hasKids: true })
  assert.equal(fixture.state.detail.value.id, 'mod:backend')
})

test('file detail reads documentation and sends file paths for impact instead of symbol IDs', async t => {
  const fixture = await setup(t, 'NodeDetail')
  await settle()
  assert.equal(fixture.state.purpose.value.documentation.text, documentation.text)
  assert.equal(fixture.calls.some(call => call.name === 'symbolLogic'), false)
  const impact = fixture.calls.find(call => call.name === 'impact')
  assert.deepEqual(impact.args[1].symbol_ids, [])
  assert.deepEqual(impact.args[1].files, ['AgentService.java'])
  fixture.state.jump('AgentService.java', documentation.line, documentation.line)
  assert.deepEqual(fixture.events.at(-1), ['open-file', { path: 'AgentService.java', line: 55, end_line: 55 }])
})

test('a business change refreshes purpose and an earlier same-node read cannot replace it', async t => {
  const fixture = await setup(t, 'NodeDetail')
  await settle()
  const earlier = deferred(), changed = deferred()
  let reads = 0
  fixture.stubs.symbolPurpose = () => ++reads === 1 ? earlier.promise : changed.promise
  const loading = fixture.state.loadPurpose()
  fixture.state.onBusinessChanged({ detail: { projectId: 'a', tree: 'main', id: fileNode.id } })
  changed.resolve({ ...description('新说明'), business: { source: 'human', status: 'current', business_meaning: '共享人工说明' } })
  await settle()
  earlier.resolve(description('旧说明')); await loading
  assert.equal(fixture.state.purpose.value.headline, description('新说明').headline)
  assert.equal(fixture.state.purpose.value.business.business_meaning, '共享人工说明')
})

test('automatic description batches refresh open details only in their project/tree', async t => {
  const fixture = await setup(t, 'NodeDetail')
  await settle()
  fixture.stubs.symbolPurpose = async () => ({ ...description('自动更新'), business: { business_meaning: '共享功能', status: 'unreviewed', source: 'model' } })
  for (const detail of [{ projectId: 'b', tree: 'main' }, { projectId: 'a', tree: 'work' }, { projectId: 'a', tree: 'main', id: 'file:Other.java' }]) fixture.state.onBusinessChanged({ detail })
  assert.equal(fixture.calls.filter(call => call.name === 'symbolPurpose').length, 1)
  fixture.state.onBusinessChanged({ detail: { projectId: 'a', tree: 'main', id: '', automatic: true } })
  await settle()
  assert.equal(fixture.state.purpose.value.headline, description('自动更新').headline)
  assert.equal(fixture.state.purpose.value.business.business_meaning, '共享功能')
  assert.equal(fixture.props.detail.id, fileNode.id, 'the current drawer stays open')
})

test('generated function briefs are shown by the shared section rather than duplicated as source documentation', async t => {
  const fixture = await setup(t, 'NodeDetail', { symbolPurpose: async () => ({ ...description(), documentation: null, brief: { text: '同一份自动功能描述' }, business: { business_meaning: '同一份自动功能描述', status: 'unreviewed', source: 'model' } }) })
  await settle()
  assert.equal(fixture.state.purposeDocumentation.value, null)
  fixture.state.purpose.value = { ...fixture.state.purpose.value, documentation }
  assert.equal(fixture.state.purposeDocumentation.value.text, documentation.text, 'real author documentation is retained')
})

test('the newer detail keeps its explanation when an older node request finishes late', async t => {
  const pending = deferred()
  const fixture = await setup(t, 'NodeDetail', {
    symbolPurpose: (_, id) => id === fileNode.id ? pending.promise : Promise.resolve(description('Second')),
  })
  fixture.props.detail = { id: 'file:Second.java', kind: 'file', name: 'Second.java', path: 'Second.java', tree: 'main' }
  await settle()
  assert.equal(fixture.state.purpose.value.headline, description('Second').headline)
  pending.resolve(description('Old')); await settle()
  assert.equal(fixture.state.purpose.value.headline, description('Second').headline)
})

test('switching the tree with the same node ID reloads its explanation and ignores the prior tree', async t => {
  const pending = deferred()
  const fixture = await setup(t, 'NodeDetail', {
    symbolPurpose: (_, id, tree) => tree === 'main' ? pending.promise : Promise.resolve(description('Worktree')),
  })
  fixture.props.detail.tree = 'work'; await settle()
  assert.equal(fixture.state.purpose.value.headline, description('Worktree').headline)
  pending.resolve(description('Main')); await settle()
  assert.equal(fixture.state.purpose.value.headline, description('Worktree').headline)
})

test('unmounted detail ignores pending purpose responses', async t => {
  const pending = deferred()
  const fixture = await setup(t, 'NodeDetail', { symbolPurpose: () => pending.promise })
  fixture.unmount()
  const before = fixture.state.purpose.value
  pending.resolve(description('Late')); await settle()
  assert.equal(fixture.state.purpose.value, before)
})

test('all detail sections reject responses from the previous node', async t => {
  const pending = Object.fromEntries(['purpose', 'logic', 'impact', 'behaviors', 'contract', 'decisions', 'history'].map(key => [key, deferred()]))
  const second = {
    purpose: description('Second'), logic: { available: true, name: 'Second', steps: [] },
    impact: { fixture: 'Second', risk: { level: 'low', score: 0, reasons: [] }, tests: [] },
    behaviors: { cards: [] }, contract: { fixture: 'Second', has_contract: false },
    decisions: { items: [{ id: 'Second' }], kinds: [] }, history: { fixture: 'Second', items: [] },
  }
  const choose = (key, old) => old ? pending[key].promise : Promise.resolve(second[key])
  const fixture = await setup(t, 'NodeDetail', {
    symbolPurpose: (_, id) => choose('purpose', id === 'sym:first'),
    symbolLogic: (_, id) => choose('logic', id === 'sym:first'),
    impact: (_, options) => choose('impact', options.symbol_ids.includes('sym:first')),
    behaviors: (_, options) => choose('behaviors', options.target === 'sym:first'),
    contracts: (_, options) => choose('contract', options.path === 'First.java'),
    decisions: (_, options) => choose('decisions', options.target === 'sym:first'),
    provenance: (_, options) => choose('history', options.path === 'First.java'),
  }, { detail: { id: 'sym:first', kind: 'class', name: 'First', path: 'First.java', tree: 'main' } })
  fixture.props.detail = { id: 'sym:second', kind: 'class', name: 'Second', path: 'Second.java', tree: 'main' }
  await settle()
  for (const [key, item] of Object.entries(pending)) item.resolve({ ...second[key], fixture: 'First', headline: 'First', name: 'First', items: [{ id: 'First' }] })
  await settle()
  assert.equal(fixture.state.purpose.value.headline, second.purpose.headline)
  assert.equal(fixture.state.logic.value.name, 'Second')
  assert.equal(fixture.state.impact.value.fixture, 'Second')
  assert.equal(fixture.state.contract.value.fixture, 'Second')
  assert.equal(fixture.state.history.value.fixture, 'Second')
  assert.equal(fixture.state.decisions.value[0].id, 'Second')
})

test('detail explanation errors can be retried without closing the node', async t => {
  const fixture = await setup(t, 'NodeDetail', { symbolPurpose: async () => { throw new Error('读取说明失败') } })
  await settle()
  assert.equal(fixture.state.purpose.value.available, false)
  assert.equal(fixture.state.purpose.value.reason, '读取说明失败')
  fixture.stubs.symbolPurpose = async () => description()
  await fixture.state.refresh(); await settle()
  assert.equal(fixture.state.purpose.value.documentation.text, documentation.text)
})
