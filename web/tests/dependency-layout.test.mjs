// Pure dependency-layout regressions. Run: node --test web/tests/dependency-layout.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { layoutModules, textWidth } from '../src/lib/mapLayout.js'

const file = (path, extra = {}) => ({ id: `file:${path}`, kind: 'file', path, name: path.split('/').pop(), loc: 10, lang: 'python', skipped: '', ...extra })
const edge = (from, to, count = 1) => ({ from: `file:${from}`, to: `file:${to}`, count, kind: 'imports' })
const mapOf = (paths, imports = []) => ({
  structure: { kind: 'dir', path: '', children: paths.map((path) => typeof path === 'string' ? file(path) : path) },
  modules: [], edges: { imports },
})
const pathsOf = (layout) => layout.nodes.map((node) => node.path).sort()
const nodeOf = (layout, path) => layout.nodes.find((node) => node.path === path)

function assertGeometry(layout) {
  const ids = new Set(layout.nodes.map((node) => node.id))
  const byId = new Map(layout.nodes.map((node) => [node.id, node]))
  for (const node of layout.nodes) {
    assert.ok([node.x, node.y, node.width, node.height].every(Number.isFinite), node.path)
    assert.ok(node.x >= 0 && node.y >= 0)
    assert.ok(node.width > 0 && node.height > 0)
    assert.equal(node.w, node.width)
    assert.equal(node.h, node.height)
    assert.ok(node.width >= 216 && node.width <= 272)
    assert.equal(node.height, 72)
    assert.ok(node.x + node.width <= layout.width)
    assert.ok(node.y + node.height <= layout.height)
    assert.ok(textWidth(node.label) * 14 / 12 <= node.width - 68)
    assert.ok(textWidth(node.parentLabel) * 10 / 12 <= node.width - 26)
  }
  for (let a = 0; a < layout.nodes.length; a += 1) for (let b = a + 1; b < layout.nodes.length; b += 1) {
    const first = layout.nodes[a], second = layout.nodes[b]
    const overlaps = first.x < second.x + second.width && second.x < first.x + first.width
      && first.y < second.y + second.height && second.y < first.y + first.height
    assert.equal(overlaps, false, `${first.path} overlaps ${second.path}`)
  }
  for (const link of layout.links) {
    assert.ok(ids.has(link.from) && ids.has(link.to))
    assert.ok([link.x1, link.y1, link.x2, link.y2].every(Number.isFinite))
    assert.match(link.path, /^M /)
    assert.equal(/NaN|undefined|Infinity/.test(link.path), false)
    const from = byId.get(link.from), to = byId.get(link.to)
    assert.equal(link.sourcePort.side, 'right')
    assert.equal(link.x1, from.x + from.width)
    assert.equal(link.x2, link.targetPort.side === 'right' ? to.x + to.width : to.x)
    assert.ok(link.y1 > from.y && link.y1 < from.y + from.height)
    assert.ok(link.y2 > to.y && link.y2 < to.y + to.height)
    assert.equal(link.path.endsWith(` ${link.x2} ${link.y2}`), true)
  }
}

function assertDistinctRouteTracks(layout) {
  const points = layout.links.flatMap((link) => (link.routePoints || []).map((point) => ({ ...point, link })))
  for (let a = 0; a < points.length; a += 1) for (let b = a + 1; b < points.length; b += 1) {
    const first = points[a], second = points[b]
    if (first.x === second.x && first.width === second.width) {
      assert.notEqual(first.y, second.y, `${first.link.from} / ${second.link.from} share an occupied routing slot`)
    }
  }
  const cyclic = layout.links.filter((link) => link.cycleRouteX !== undefined)
  for (let a = 0; a < cyclic.length; a += 1) for (let b = a + 1; b < cyclic.length; b += 1) {
    const first = cyclic[a], second = cyclic[b]
    const overlap = Math.min(first.y1, first.y2) <= Math.max(second.y1, second.y2)
      && Math.min(second.y1, second.y2) <= Math.max(first.y1, first.y2)
    if (overlap) assert.notEqual(first.cycleRouteX, second.cycleRouteX, `${first.from} / ${second.from} share a cycle track`)
  }
}

function assertRegions(layout) {
  const byId = new Map(layout.nodes.map((node) => [node.id, node]))
  const seen = new Set()
  for (const region of layout.regions) {
    assert.ok(region.x >= 0 && region.y >= 0)
    assert.ok(region.x + region.width <= layout.width)
    assert.ok(region.y + region.height <= layout.height)
    for (const id of region.members) {
      assert.ok(!seen.has(id), `${id} appears in more than one region`)
      seen.add(id)
      const node = byId.get(id)
      assert.equal(node.regionId, region.id)
      assert.ok(node.x >= region.x && node.x + node.width <= region.x + region.width)
      assert.ok(node.y >= region.y + 36 && node.y + node.height <= region.y + region.height)
    }
  }
  assert.equal(seen.size, layout.nodes.length)
  for (let a = 0; a < layout.regions.length; a += 1) for (let b = a + 1; b < layout.regions.length; b += 1) {
    const first = layout.regions[a], second = layout.regions[b]
    assert.ok(first.x + first.width <= second.x || second.x + second.width <= first.x
      || first.y + first.height <= second.y || second.y + second.height <= first.y,
    `${first.id} overlaps ${second.id}`)
  }
}

/** Sample the actual SVG segments, including curves in the column gaps. */
function assertRoutesAvoidCards(layout) {
  for (const link of layout.links) {
    const segments = [...link.path.matchAll(/([MLC])\s+([^MLC]+)/g)]
    let current = null
    for (const [, command, coordinates] of segments) {
      const values = coordinates.trim().split(/[\s,]+/).map(Number)
      const end = { x: values.at(-2), y: values.at(-1) }
      if (current && command !== 'M') {
        for (let step = 0; step <= 24; step += 1) {
          const t = step / 24, s = 1 - t
          const point = command === 'C'
            ? { x: s ** 3 * current.x + 3 * s ** 2 * t * values[0] + 3 * s * t ** 2 * values[2] + t ** 3 * end.x,
                y: s ** 3 * current.y + 3 * s ** 2 * t * values[1] + 3 * s * t ** 2 * values[3] + t ** 3 * end.y }
            : { x: current.x + (end.x - current.x) * t, y: current.y + (end.y - current.y) * t }
          for (const node of layout.nodes) {
            assert.ok(!(point.x > node.x + 0.01 && point.x < node.x + node.width - 0.01
              && point.y > node.y + 0.01 && point.y < node.y + node.height - 0.01),
            `${link.from} → ${link.to} crosses ${node.id}`)
          }
        }
      }
      current = end
    }
  }
}

test('default view keeps actual dependencies and excludes unrelated docs/logs/skipped files', () => {
  const map = mapOf(['app/main.py', 'lib/core.py', 'logs/backend.log', 'docs/guide.md', file('secret/key.py', { skipped: 'secret' })], [
    edge('app/main.py', 'lib/core.py', 2), edge('app/main.py', 'missing/outside.py'),
    edge('app/main.py', 'secret/key.py'), { from: 'file:app/main.py', to: 'external:requests', count: 1 },
  ])
  const graph = layoutModules(map)
  assert.deepEqual(pathsOf(graph), ['app', 'lib'])
  assert.equal(graph.links[0].count, 2)
  assert.equal(graph.stats.ignoredEdgeCount, 3)
  assert.equal(graph.allNodes.length, 4)
  assert.equal(nodeOf(graph, 'app').loc, 10)
  assert.equal(graph.files.find((node) => node.path === 'secret/key.py').skipped, 'secret')
  assert.equal(graph.files[0].lang, 'python')
  assertGeometry(graph)
  const withIsolated = layoutModules(map, { includeIsolated: true })
  assert.deepEqual(pathsOf(withIsolated), ['app', 'docs', 'lib', 'logs'])
  assertGeometry(withIsolated)
})

test('parent-package aggregation retains root and shallow files without duplicating members', () => {
  const map = mapOf(['main.py', 'helper.py', 'pkg/top.py', 'pkg/service/a.py', 'pkg/service/b.py', 'pkg/service/deep/c.py'], [
    edge('main.py', 'pkg/top.py'), edge('pkg/top.py', 'pkg/service/a.py', 2),
    edge('pkg/service/a.py', 'pkg/service/b.py', 3), edge('pkg/service/b.py', 'pkg/service/deep/c.py'),
  ])
  const graph = layoutModules(map, { moduleDepth: 3 })
  assert.deepEqual(pathsOf(graph), ['', 'pkg', 'pkg/service', 'pkg/service/deep'])
  assert.deepEqual(nodeOf(graph, '').members, ['helper.py', 'main.py'])
  assert.deepEqual(nodeOf(graph, 'pkg').members, ['pkg/top.py'])
  assert.deepEqual(nodeOf(graph, 'pkg/service').members, ['pkg/service/a.py', 'pkg/service/b.py'])
  assert.equal(nodeOf(graph, 'pkg/service').internalImportCount, 3)
  assert.equal(graph.stats.importCount, 7)
  assert.equal(graph.links.length, 3)
  assertGeometry(graph)
})

test('coarse packages with only internal imports stay visible and count weighted imports', () => {
  const map = mapOf(['frontend/src/views/one.vue', 'frontend/src/components/widget.vue', 'docs/note.md'], [
    edge('frontend/src/views/one.vue', 'frontend/src/components/widget.vue', 2),
    edge('frontend/src/views/one.vue', 'frontend/src/components/widget.vue', 3),
  ])
  const graph = layoutModules(map, { moduleDepth: 2 })
  assert.deepEqual(pathsOf(graph), ['frontend/src'])
  assert.equal(nodeOf(graph, 'frontend/src').internalImportCount, 5)
  assert.equal(nodeOf(graph, 'frontend/src').cyclic, false)
  assert.equal(graph.links.length, 0)
  assert.equal(graph.stats.internalImportCount, 5)
  const files = layoutModules(map, { moduleDepth: 4 })
  assert.equal(files.nodes.length, 2)
  assert.equal(files.links[0].count, 5)
  assert.ok(files.nodes.every((node) => node.isFile && node.members[0] === node.path))
  assert.equal(files.stats.internalImportCount, 0)
  assertGeometry(files)
})

test('cross-package cycles form one group and their dependencies follow to the right', () => {
  const map = mapOf(['a/main.py', 'b/main.py', 'c/main.py'], [
    edge('a/main.py', 'b/main.py'), edge('b/main.py', 'a/main.py'), edge('b/main.py', 'c/main.py'),
  ])
  const graph = layoutModules(map)
  assert.equal(graph.clusters.length, 1)
  assert.deepEqual(graph.clusters[0].members, ['mod:a', 'mod:b'])
  assert.equal(nodeOf(graph, 'a').x, nodeOf(graph, 'b').x)
  assert.ok(nodeOf(graph, 'c').x > nodeOf(graph, 'b').x)
  assert.equal(nodeOf(graph, 'a').cycleId, nodeOf(graph, 'b').cycleId)
  assert.equal(nodeOf(graph, 'c').cyclic, false)
  assertGeometry(graph)
})

test('file cycles inside a package are identified without inventing cross-module self links', () => {
  const graph = layoutModules(mapOf(['pkg/a.py', 'pkg/b.py'], [edge('pkg/a.py', 'pkg/b.py', 2), edge('pkg/b.py', 'pkg/a.py')]))
  assert.equal(graph.nodes.length, 1)
  assert.equal(graph.nodes[0].internalCycleCount, 1)
  assert.equal(graph.nodes[0].cyclic, true)
  assert.equal(graph.nodes[0].internalImportCount, 3)
  assert.deepEqual(graph.fileCycles, [['pkg/a.py', 'pkg/b.py']])
  assert.equal(graph.links.length, 0)
  assertGeometry(graph)
})

const chainMap = () => mapOf(['a/start.py', 'b/middle.py', 'c/work.py', 'd/end.py', 'sibling/s.py', 'other/z.py'], [
  edge('a/start.py', 'b/middle.py', 2), edge('b/middle.py', 'c/work.py'), edge('c/work.py', 'd/end.py'),
  edge('sibling/s.py', 'a/start.py'), edge('other/z.py', 'd/end.py'),
])

test('query matches full member paths and retains both one-hop neighbors', () => {
  const graph = layoutModules(chainMap(), { query: 'middle.py' })
  assert.deepEqual(pathsOf(graph), ['a', 'b', 'c'])
  assert.equal(nodeOf(graph, 'b').matched, true)
  assert.equal(nodeOf(graph, 'a').matched, false)
  assert.equal(graph.links.length, 2)
  assert.equal(graph.allNodes.length, 6)
  assert.equal(graph.allLinks.length, 5)
  assert.equal(nodeOf(graph, 'b').importedByCount, 2)
  assertGeometry(graph)
})

test('focus respects direction and hops without adding siblings of dependencies', () => {
  assert.deepEqual(pathsOf(layoutModules(chainMap(), { focusId: 'mod:b', focusDirection: 'down', focusHops: 2 })), ['b', 'c', 'd'])
  assert.deepEqual(pathsOf(layoutModules(chainMap(), { focusId: 'b', focusDirection: 'up', focusHops: 2 })), ['a', 'b', 'sibling'])
  assert.deepEqual(pathsOf(layoutModules(chainMap(), { focusId: 'file:b/middle.py', focusDirection: 'both', focusHops: 2 })), ['a', 'b', 'c', 'd', 'sibling'])
  const graph = layoutModules(chainMap(), { focusId: 'mod:b', focusHops: 1 })
  assert.deepEqual(pathsOf(graph), ['a', 'b', 'c'])
  assert.equal(nodeOf(graph, 'b').focusRole, 'self')
  assert.equal(nodeOf(graph, 'a').focusRole, 'up')
  assert.equal(nodeOf(graph, 'c').focusRole, 'down')
  assertGeometry(graph)
  const missing = layoutModules(chainMap(), { focusId: 'mod:missing' })
  assert.equal(missing.nodes.length, 0)
  assert.equal(missing.links.length, 0)
  assert.equal(missing.stats.focusMissing, true)
})

test('changed paths filter only when explicitly supplied and preserve one-hop context', () => {
  assert.equal(layoutModules(chainMap(), { changedPaths: null }).nodes.length, 6)
  assert.deepEqual(pathsOf(layoutModules(chainMap(), { changedPaths: ['b/middle.py'] })), ['a', 'b', 'c'])
  assert.equal(layoutModules(chainMap(), { changedPaths: [] }).nodes.length, 0)
  assertGeometry(layoutModules(chainMap(), { changedPaths: ['b/middle.py'], query: 'work.py' }))
})

test('an exact root-package file scope does not expand to the entire repository', () => {
  const map = mapOf(['main.py', 'helper.py', 'lib/core.py', 'other/app.py', 'other/service.py'], [
    edge('main.py', 'helper.py'), edge('helper.py', 'lib/core.py'), edge('other/app.py', 'other/service.py'),
  ])
  const root = nodeOf(layoutModules(map, { moduleDepth: 3 }), '')
  const graph = layoutModules(map, { moduleDepth: 4, scopePaths: root.members })
  assert.deepEqual(pathsOf(graph), ['helper.py', 'lib/core.py', 'main.py'])
  assert.equal(graph.links.length, 2)
  assert.equal(graph.allNodes.length, 5)
  assert.equal(graph.allLinks.length, 3)
  assertGeometry(graph)
})

test('exact package scope excludes similarly prefixed siblings and intersects other filters', () => {
  const map = mapOf(['app/api/main.py', 'app/api-backup/main.py', 'lib/core.py', 'downstream/end.py'], [
    edge('app/api/main.py', 'lib/core.py'), edge('app/api-backup/main.py', 'lib/core.py'),
    edge('lib/core.py', 'downstream/end.py'),
  ])
  const scoped = nodeOf(layoutModules(map), 'app/api').members
  const graph = layoutModules(map, { moduleDepth: 4, scopePaths: scoped })
  assert.deepEqual(pathsOf(graph), ['app/api/main.py', 'lib/core.py'])
  assert.equal(graph.links.length, 1)
  const combined = layoutModules(map, {
    moduleDepth: 4, scopePaths: scoped, query: 'core.py',
    changedPaths: ['lib/core.py'], focusId: 'file:lib/core.py', focusDirection: 'up', focusHops: 2,
  })
  assert.deepEqual(pathsOf(combined), ['app/api/main.py', 'lib/core.py'])
  assert.equal(layoutModules(map, { scopePaths: [] }).nodes.length, 0)
  assert.equal(layoutModules(map, { scopePaths: null }).nodes.length, 4)
  assertGeometry(graph)
  assertGeometry(combined)
})

test('layout is stable under reordered input and does not mutate source data', () => {
  const map = chainMap(), before = JSON.stringify(map)
  const first = layoutModules(map)
  const reordered = { ...map, structure: { ...map.structure, children: [...map.structure.children].reverse() }, edges: { imports: [...map.edges.imports].reverse() } }
  const second = layoutModules(reordered)
  assert.deepEqual(first.nodes, second.nodes)
  assert.deepEqual(first.links, second.links)
  assert.equal(JSON.stringify(map), before)
})

test('large full graphs are never truncated, have no overlaps, and stay available through allNodes', () => {
  const paths = Array.from({ length: 420 }, (_, index) => `packages/pkg-${String(index).padStart(3, '0')}/file.py`)
  const imports = paths.slice(1).map((path) => edge(paths[0], path))
  const graph = layoutModules(mapOf(paths, imports), { moduleDepth: 4 })
  assert.equal(graph.nodes.length, 420)
  assert.equal(graph.links.length, 419)
  assertGeometry(graph)
  const local = layoutModules(mapOf(paths, imports), { moduleDepth: 4, focusId: `mod:${paths[200]}` })
  assert.equal(local.nodes.length, 2)
  assert.equal(local.allNodes.length, 420)
  assert.equal(local.allLinks.length, 419)
  assertGeometry(local)
})

test('empty edges keep metadata available and explicit isolated view lays all modules out', () => {
  const map = mapOf(['main.py', 'docs/readme.md', 'pkg/helpers.py'])
  const graph = layoutModules(map)
  assert.equal(graph.nodes.length, 0)
  assert.equal(graph.allNodes.length, 3)
  assert.equal(graph.links.length, 0)
  assert.equal(graph.stats.isolatedCount, 3)
  const isolated = layoutModules(map, { includeIsolated: true })
  assert.equal(isolated.nodes.length, 3)
  assertGeometry(isolated)
  assertGeometry(layoutModules(null))
})

test('compact cards use the last path segment without losing full path or parent context', () => {
  const parent = 'frontend/src/很长的父目录说明'.repeat(4)
  const longName = '这是一个需要省略但仍保留首尾信息的非常长的组件名称.vue'
  const graph = layoutModules(mapOf(['main.py', `${parent}/Panel.vue`, `${parent}/${longName}`], [
    edge('main.py', `${parent}/Panel.vue`), edge(`${parent}/Panel.vue`, `${parent}/${longName}`),
  ]), { moduleDepth: 4 })
  const panel = nodeOf(graph, `${parent}/Panel.vue`)
  assert.equal(panel.name, 'Panel.vue')
  assert.equal(panel.label, 'Panel.vue')
  assert.equal(panel.width, 216)
  assert.equal(panel.parentPath, parent)
  assert.equal(panel.fullLabel, `${parent}/Panel.vue`)
  assert.notEqual(panel.parentLabel, parent)
  assert.match(panel.parentLabel, /…/)
  const long = nodeOf(graph, `${parent}/${longName}`)
  assert.equal(long.name, longName)
  assert.equal(long.width, 272)
  assert.match(long.label, /…/)
  assert.equal(nodeOf(graph, 'main.py').parentPath, '')
  assert.equal(nodeOf(graph, 'main.py').parentLabel, '项目根目录')
  assertGeometry(graph)
  const coarse = layoutModules(mapOf(['main.py', 'pkg/service/main.py'], [edge('main.py', 'pkg/service/main.py')]))
  assert.equal(nodeOf(coarse, '').label, '(根目录)')
  assert.equal(nodeOf(coarse, 'pkg/service').label, 'service')
  assert.equal(nodeOf(coarse, 'pkg/service').parentLabel, 'pkg')
})

test('fan-in and fan-out get distinct ports on the correct card edges', () => {
  const sources = Array.from({ length: 18 }, (_, index) => `source-${index}/main.py`)
  const targets = Array.from({ length: 18 }, (_, index) => `target-${index}/main.py`)
  const graph = layoutModules(mapOf([...sources, 'hub/main.py', ...targets], [
    ...sources.map((path) => edge(path, 'hub/main.py')),
    ...targets.map((path) => edge('hub/main.py', path)),
  ]))
  const inbound = graph.links.filter((link) => link.to === 'mod:hub')
  const outbound = graph.links.filter((link) => link.from === 'mod:hub')
  assert.equal(new Set(inbound.map((link) => link.y2)).size, 18)
  assert.equal(new Set(outbound.map((link) => link.y1)).size, 18)
  assert.ok(inbound.every((link) => link.targetPort.side === 'left'))
  assert.ok(outbound.every((link) => link.sourcePort.side === 'right'))
  assert.equal(graph.links.length, 36)
  assertGeometry(graph)
})

test('14px card titles reserve space for larger Chinese and English glyphs', () => {
  const names = [
    '这是超过标题宽度需要省略的中文名称.vue',
    'OriginalDocumentPreviewContainer.vue',
    'AgentService.java',
  ]
  const parent = 'frontend/src/components/很长的目录说明'
  const paths = names.map((name) => `${parent}/${name}`)
  const graph = layoutModules(mapOf(paths, [edge(paths[0], paths[1]), edge(paths[1], paths[2])]), { moduleDepth: 4 })
  for (const node of graph.nodes) {
    assert.ok(textWidth(node.label) * 14 / 12 * 1.06 <= node.width - 68)
    assert.ok(textWidth(node.parentLabel) * 10 / 12 * 1.06 <= node.width - 26)
    assert.equal(node.name, names.find((name) => node.path.endsWith(`/${name}`)))
    assert.equal(node.parentPath, parent)
  }
  assert.match(nodeOf(graph, paths[0]).label, /…/)
  assert.match(nodeOf(graph, paths[1]).label, /…/)
  assert.equal(nodeOf(graph, paths[2]).label, names[2])
  assertGeometry(graph)
})

test('more than twelve overlapping cross-layer edges get free routing slots without a top fence', () => {
  const chain = Array.from({ length: 6 }, (_, index) => `chain-${index}/main.py`)
  const shortcuts = Array.from({ length: 20 }, (_, index) => `entry-${index}/main.py`)
  const graph = layoutModules(mapOf([...chain, ...shortcuts], [
    ...chain.slice(1).map((path, index) => edge(chain[index], path)),
    ...shortcuts.flatMap((path) => [edge(path, chain[1]), edge(path, chain[5])]),
  ]))
  const long = graph.links.filter((link) => link.routePoints.length)
  assert.equal(long.length, 20)
  for (const layer of [1, 2, 3, 4]) {
    assert.equal(new Set(long.flatMap((link) => link.routePoints.filter((point) => point.layer === layer).map((point) => point.y))).size, 20)
  }
  assert.equal(graph.links.length, 45)
  assert.ok(long.every((link) => link.routeY === undefined))
  assertDistinctRouteTracks(graph)
  assertRoutesAvoidCards(graph)
  assertRegions(graph)
  assertGeometry(graph)
})

test('disjoint shortcut spans only reserve slots in the layers they cross', () => {
  const paths = Array.from({ length: 7 }, (_, index) => `chain-${index}/main.py`)
  const graph = layoutModules(mapOf(paths, [
    ...paths.slice(1).map((path, index) => edge(paths[index], path)),
    edge(paths[0], paths[2]), edge(paths[3], paths[5]),
  ]))
  const long = graph.links.filter((link) => link.routePoints.length)
  assert.equal(long.length, 2)
  assert.equal(long[0].routePoints.length, 1)
  assert.equal(long[1].routePoints.length, 1)
  assert.equal(long[0].routePoints[0].layer, 1)
  assert.equal(long[1].routePoints[0].layer, 4)
  assert.equal(graph.links.length, 8)
  assertDistinctRouteTracks(graph)
  assertRoutesAvoidCards(graph)
  assertGeometry(graph)
})

test('dense SCC routes expand column gaps and keep incoming cycle ports separate', () => {
  const paths = Array.from({ length: 6 }, (_, index) => `cycle-${index}/main.py`)
  const imports = paths.flatMap((from) => paths.filter((to) => to !== from).map((to) => edge(from, to)))
  const graph = layoutModules(mapOf([...paths, 'destination/main.py'], [...imports, edge(paths[0], 'destination/main.py')]))
  const cycles = graph.links.filter((link) => link.cycleRouteX !== undefined)
  assert.equal(cycles.length, 30)
  assert.ok(new Set(cycles.map((link) => link.cycleRouteX)).size > 8)
  for (const path of paths) {
    const node = nodeOf(graph, path.split('/')[0])
    const outputs = cycles.filter((link) => link.from === node.id)
    const inputs = cycles.filter((link) => link.to === node.id)
    assert.ok(outputs.every((link) => link.sourcePort.offset < 34))
    assert.ok(inputs.every((link) => link.targetPort.side === 'right' && link.targetPort.offset > 42))
    assert.equal(new Set([...outputs.map((link) => link.y1), ...inputs.map((link) => link.y2)]).size, 10)
  }
  const nextColumn = nodeOf(graph, 'destination')
  assert.ok(Math.max(...cycles.map((link) => link.cycleRouteX)) < nextColumn.x - 22)
  assert.equal(graph.clusters.length, 1)
  assertDistinctRouteTracks(graph)
  assertGeometry(graph)
  const reversed = layoutModules(mapOf(['destination/main.py', ...paths.toReversed()], [edge(paths[0], 'destination/main.py'), ...imports.toReversed()]))
  assert.deepEqual(reversed.nodes, graph.nodes)
  assert.deepEqual(reversed.links, graph.links)
})

test('cross-layer routes remain stable under filtering and reversed input', () => {
  const paths = Array.from({ length: 8 }, (_, index) => `chain-${index}/main.py`)
  const imports = [
    ...paths.slice(1).map((path, index) => edge(paths[index], path)),
    edge(paths[0], paths[3], 2), edge(paths[0], paths[5], 3), edge(paths[1], paths[7], 4),
    edge(paths[3], paths[6], 5), edge(paths[4], paths[7], 6),
  ]
  const map = mapOf(paths, imports)
  const before = JSON.stringify(map)
  const options = { moduleDepth: 4, focusId: `file:${paths[3]}`, focusHops: 2 }
  const graph = layoutModules(map, options)
  const reversed = layoutModules(mapOf(paths.toReversed(), imports.toReversed()), options)
  assert.deepEqual(reversed.nodes, graph.nodes)
  assert.deepEqual(reversed.links, graph.links)
  assert.equal(JSON.stringify(map), before)
  assert.equal(graph.allNodes.length, 8)
  assert.equal(graph.allLinks.length, 12)
  assert.ok(graph.links.some((link) => link.routePoints.length))
  assertDistinctRouteTracks(graph)
  assertRoutesAvoidCards(graph)
  assertRegions(graph)
  assertGeometry(graph)
})

test('packages with only internal imports share a compact grid while retaining every weighted relation', () => {
  const paths = Array.from({ length: 12 }, (_, index) => [`internal-${index}/a.py`, `internal-${index}/b.py`])
  const map = mapOf(paths.flat(), paths.map(([from, to]) => edge(from, to, 3)))
  const graph = layoutModules(map)
  assert.equal(graph.nodes.length, 12)
  assert.equal(graph.links.length, 0)
  assert.equal(graph.allNodes.length, 12)
  assert.equal(graph.stats.importCount, 36)
  assert.equal(graph.stats.shownInternalImportCount, 36)
  assert.equal(graph.stats.hiddenIsolatedCount, 0)
  assert.ok(graph.nodes.every((node) => node.files === 2 && node.internalImportCount === 3 && node.connectedFiles === 2))
  assert.equal(graph.regions.length, 1)
  assert.equal(graph.regions[0].kind, 'standalone')
  assert.ok(new Set(graph.nodes.map((node) => node.x)).size >= 3)
  assert.ok(graph.height < 500, `internal-only packages must not form a tall left column: ${graph.height}`)
  assertRegions(graph)
  assertGeometry(graph)
})

test('internal cycles remain visible and count correctly inside a disconnected-card grid', () => {
  const paths = ['cycle/a.py', 'cycle/b.py', 'normal/a.py', 'normal/b.py', 'docs/guide.md']
  const map = mapOf(paths, [edge(paths[0], paths[1]), edge(paths[1], paths[0]), edge(paths[2], paths[3])])
  const graph = layoutModules(map, { includeIsolated: true })
  assert.equal(graph.nodes.length, 3)
  assert.equal(graph.links.length, 0)
  assert.equal(graph.stats.shownCycleCount, 1)
  assert.equal(graph.stats.cycleCount, 1)
  assert.equal(graph.stats.isolatedCount, 1)
  assert.equal(nodeOf(graph, 'cycle').internalCycleCount, 1)
  assert.deepEqual(graph.fileCycles, [['cycle/a.py', 'cycle/b.py']])
  assert.equal(graph.clusters[0].label, '包内循环依赖')
  const region = graph.regions[0], cluster = graph.clusters[0]
  assert.ok(cluster.x >= region.x && cluster.x + cluster.width <= region.x + region.width)
  assert.ok(cluster.y >= region.y + 36 && cluster.y + cluster.height <= region.y + region.height)
  assertRegions(graph)
  assertGeometry(graph)
})

test('separate connected components use available horizontal space and never share a region or overlap', () => {
  const pairs = Array.from({ length: 12 }, (_, index) => [`group-${index}/entry/a.py`, `group-${index}/library/b.py`])
  const map = mapOf(pairs.flat(), pairs.map(([from, to]) => edge(from, to)))
  const graph = layoutModules(map, { availableWidth: 1280 })
  assert.equal(graph.nodes.length, 24)
  assert.equal(graph.links.length, 12)
  assert.equal(graph.regions.length, 12)
  assert.equal(graph.stats.shownCycleCount, 0)
  assert.ok(new Set(graph.regions.map((region) => region.x)).size > 1)
  assert.ok(new Set(graph.regions.map((region) => region.y)).size < 12)
  assert.ok(graph.height < 1200)
  for (const link of graph.links) assert.equal(graph.nodes.find((node) => node.id === link.from).regionId, graph.nodes.find((node) => node.id === link.to).regionId)
  const reordered = layoutModules(mapOf(pairs.flat().reverse(), pairs.map(([from, to]) => edge(from, to)).reverse()), { availableWidth: 1280 })
  assert.deepEqual(reordered.nodes, graph.nodes)
  assert.deepEqual(reordered.links, graph.links)
  assert.deepEqual(reordered.regions, graph.regions)
  assertRegions(graph)
  assertRoutesAvoidCards(graph)
  assertGeometry(graph)
})

test('the screenshot-style mix of two graphs and eight internal-only cards stays compact', () => {
  const front = [
    'frontend/src/main.js', 'frontend/src/views/Page.vue', 'output/preview/main.js',
    'frontend/src/router/routes.js', 'frontend/src/layout/Layout.vue',
    'frontend/src/components/Panel.vue', 'frontend/src/utils/helpers.js',
    'frontend/src/api/client.js', 'frontend/src/composables/useData.js',
  ]
  const frontImports = [
    edge(front[0], front[3]), edge(front[3], front[4]), edge(front[4], front[5]),
    edge(front[5], front[6]), edge(front[6], front[5]), edge(front[5], front[8]), edge(front[6], front[7]),
    edge(front[0], front[5]), edge(front[0], front[6]), edge(front[0], front[7]),
    edge(front[1], front[5]), edge(front[1], front[6]), edge(front[1], front[7]), edge(front[2], front[6]),
  ]
  const back = ['tests/a/main.py', 'tests/b/main.py', 'tests/c/main.py', 'tests/d/main.py', 'tools/bin/run.py', 'tools/common/helper.py', 'backend/src/main/App.java']
  const backImports = [edge(back[0], back[4]), edge(back[1], back[5]), edge(back[2], back[5]), edge(back[3], back[5]), edge(back[4], back[6]), edge(back[5], back[6])]
  const locals = Array.from({ length: 8 }, (_, index) => [`snapshot-${index}/a.js`, `snapshot-${index}/b.js`])
  const graph = layoutModules(mapOf([...front, ...back, ...locals.flat()], [...frontImports, ...backImports, ...locals.map(([from, to]) => edge(from, to))]), { availableWidth: 1100 })
  assert.equal(graph.nodes.length, 24)
  assert.equal(graph.links.length, 20)
  assert.equal(graph.regions.length, 3)
  assert.equal(graph.regions.find((region) => region.kind === 'standalone').members.length, 8)
  assert.ok(graph.height < 1050, `the screenshot arrangement became too tall: ${graph.height}`)
  assert.ok(graph.width < 1800)
  assertRegions(graph)
  assertDistinctRouteTracks(graph)
  assertRoutesAvoidCards(graph)
  assertGeometry(graph)
})

test('a narrower canvas reflows the disconnected grid without hiding data or shrinking cards', () => {
  const pairs = Array.from({ length: 16 }, (_, index) => [`package-${index}/a.py`, `package-${index}/b.py`])
  const map = mapOf(pairs.flat(), pairs.map(([from, to]) => edge(from, to)))
  const narrow = layoutModules(map, { availableWidth: 560 })
  const wide = layoutModules(map, { availableWidth: 1400 })
  assert.equal(narrow.nodes.length, wide.nodes.length)
  assert.equal(narrow.stats.importCount, wide.stats.importCount)
  assert.ok(narrow.height > wide.height)
  assert.ok(narrow.width < 600)
  for (const node of narrow.nodes) assert.equal(node.width, nodeOf(wide, node.path).width)
  assertRegions(narrow)
  assertRegions(wide)
  assertGeometry(narrow)
  assertGeometry(wide)
})

test('region headings stay short and preserve the complete shared path separately', () => {
  const parent = `workspace/${'a-very-long-common-package-name-'.repeat(12)}`
  const paths = [`${parent}/Entry.py`, `${parent}/Service.py`]
  const graph = layoutModules(mapOf(paths, [edge(paths[0], paths[1])]), { moduleDepth: 4 })
  assert.equal(graph.regions.length, 1)
  const region = graph.regions[0]
  assert.equal(region.path, parent)
  assert.equal(region.title, parent)
  assert.ok(textWidth(region.label) * 1.06 <= region.width - 160)
  assert.match(region.label, /…/)
  assertRegions(graph)
  assertGeometry(graph)
})
