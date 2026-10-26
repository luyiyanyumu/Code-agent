// 思维导图的布局与增量补丁（纯函数，无 DOM 依赖）。
//
// 为什么单独拆出来：
// 1. 布局是"语义缩放"的落点——用户看到的是 目录 → 文件 → 符号 → 成员 的层，
//    不是把一张大图缩小；这份映射逻辑必须能单独推理与测试；
// 2. 增量补丁（applyDelta）直接决定"改动是否显性体现在导图上"，
//    它出错会表现成"地图和代码对不上"这种最难发现的问题。
//
// 两种投影：
//   structure：可折叠的缩进树（左→右），节点是 dir/file/symbol
//   deps     ：分层有向依赖图，节点是模块，边是 import 聚合

/** 符号 kind → 展示用中文与配色键（配色在组件里按 key 取 CSS 变量）。 */
export const KIND_META = {
  dir: { label: '目录', color: 'var(--muted)', short: 'DIR' },
  file: { label: '文件', color: '#6b7280', short: 'F' },
  module: { label: '模块', color: '#7c3aed', short: 'M' },
  // 功能视图（M33）的两种节点：能力域是聚合，功能是叶子（点开看实现流程）
  domain: { label: '能力域', color: '#7c3aed', short: '域' },
  feature: { label: '功能', color: '#0891b2', short: '功' },
  class: { label: '类', color: '#2563eb', short: 'C' },
  interface: { label: '接口', color: '#0891b2', short: 'I' },
  type: { label: '类型', color: '#0891b2', short: 'T' },
  enum: { label: '枚举', color: '#0891b2', short: 'E' },
  record: { label: '记录类', color: '#2563eb', short: 'REC' },
  annotation: { label: '注解', color: '#c026d3', short: '@' },
  component: { label: '组件', color: '#c026d3', short: 'V' },
  function: { label: '函数', color: '#16a34a', short: 'ƒ' },
  method: { label: '方法', color: '#65a30d', short: 'm' },
  constructor: { label: '构造方法', color: '#65a30d', short: 'new' },
  field: { label: '字段', color: '#65a30d', short: 'f' },
  parameter: { label: '参数', color: '#64748b', short: 'arg' },
  enum_member: { label: '枚举值', color: '#0891b2', short: 'EV' },
  record_component: { label: '记录组件', color: '#65a30d', short: 'rc' },
  property: { label: '属性', color: '#65a30d', short: 'p' },
  hook: { label: '钩子', color: '#d97706', short: 'h' },
  route: { label: '路由', color: '#dc2626', short: 'R' },
  variable: { label: '变量', color: '#94a3b8', short: 'v' },
  constant: { label: '常量', color: '#64748b', short: 'k' },
  unknown: { label: '其他', color: '#9ca3af', short: '?' },
}

export function kindMeta(kind) {
  return KIND_META[kind] || KIND_META.unknown
}

/** 置信度 → 视觉提示：启发式解析出来的节点不该被当成事实。 */
export function isHeuristic(node) {
  return node?.confidence === 'low' || node?.confidence === 'medium'
}

/** 从服务端 structure 里取出某条相对路径的祖先目录链（含自身目录）。 */
export function pathDirs(relPath) {
  const parts = String(relPath || '').split('/').filter(Boolean)
  const dirs = []
  for (let i = 0; i < parts.length - 1; i += 1) dirs.push(parts.slice(0, i + 1).join('/'))
  return dirs
}

/** 估算一段文本在 12px 字体下的像素宽度：SVG 没有自动布局，必须自己量。 */
export function textWidth(text) {
  let w = 0
  for (const ch of String(text ?? '')) {
    const code = ch.codePointAt(0)
    if (code > 0x2e80) w += 12                    // 中日韩全角
    else if (code >= 0x2000 && code <= 0x2bff) w += 9   // 角标里会用到的箭头/符号
    else w += 6.6
  }
  return w
}

/**
 * 把服务端的 structure（目录 → 文件 → 扁平符号）转成**经典树状思维导图**的行。
 *
 * 这一版是重写的，四条规则对应上一版真实踩到的四个问题：
 *
 * 1. **折叠的目录也要画出来**。旧版把"没展开的目录"整行丢掉，于是文件树里 `code_agent/`
 *    有 15 个子目录、地图上只剩下 5 个文件——用户第一反应就是"两边对不上"。
 *    折叠态用 `n 项` 角标表示：层级在、数量在，也避免一上来就画出几千行。
 * 2. **按深度分列**。旧版 x = 16 + depth*20（缩进 20px），而节点宽 100~360px——
 *    子节点直接画在父节点的框上，看起来就是"框叠框"。现在每列宽度取该列最宽的**行**，
 *    子列从父列右边界之后开始，永不相交。
 * 3. **父子垂直居中**（tidy tree）：父节点对齐到子节点区间的正中，连线才是思维导图的样子，
 *    而不是一堆左对齐的行。
 * 4. **角标由布局统一排版**。诊断/入口/未索引/证据/覆盖率这些角标，宽度算进节点宽度、
 *    从右往左依次摆放；旧版把它们写死在 `width-46` / `width-34` 上，两个角标一撞就糊在
 *    文件名上（真机截图里是 "backend.log索"）。
 *
 * 另外：`showSkipped=false` 时不画被跳过的文件（密钥/二进制/超大文件，本仓 319 个文件里
 * 有 92 个），但会在目录行上标 `未索引 n`——**不画 ≠ 悄悄藏起来**。
 */
export function layoutStructure(
  map,
  { expanded, kinds, query, showHeuristic = true, showSkipped = false, badgeOf, autoExpand = false } = {},
) {
  const ROW = 24
  const COL_GAP = 26
  //: 药丸高度比行距小 4px，行与行之间才有呼吸（连线也不会贴在文字上）
  const BOX_H = ROW - 4
  const PAD = 16
  const TOKEN_GAP = 6

  const q = (query || '').trim().toLowerCase()
  const kindSet = kinds && kinds.length ? new Set(kinds) : null
  const isExpanded = (id) => !!expanded[id]

  const rows = []
  const links = []
  let maxDepth = 0

  /** 搜索命中的节点要连同祖先一起显示：否则"搜到了但在折叠里"等于没搜到。 */
  function visibleByQuery(node) {
    if (!q) return true
    const label = `${node.name || ''} ${node.path || ''} ${node.signature || ''}`.toLowerCase()
    if (label.includes(q)) return true
    const kids = node.kind === 'dir' ? node.children || [] : node.symbols || []
    return kids.some((child) => visibleByQuery(child))
  }

  function symbolGroups(list) {
    const ids = new Set(list.map((s) => s.id))
    const out = new Map()
    for (const sym of list) {
      const parent = sym.parent && ids.has(sym.parent) ? sym.parent : ''
      if (!out.has(parent)) out.set(parent, [])
      out.get(parent).push(sym)
    }
    for (const arr of out.values()) arr.sort((a, b) => a.line - b.line || a.name.localeCompare(b.name))
    return out
  }

  function mkRow(raw) {
    const row = {
      id: raw.id,
      kind: raw.kind,
      name: raw.name,
      label: raw.label,
      path: raw.path || '',
      depth: raw.depth,
      line: raw.line,
      end_line: raw.end_line,
      confidence: raw.confidence,
      lang: raw.lang,
      loc: raw.loc,
      files: raw.files,
      skipped: raw.skipped || '',
      entrypoint: !!raw.entrypoint,
      symbolCount: raw.symbolCount || 0,
      skippedCount: raw.skippedCount || 0,
      childCount: raw.childCount || 0,
      open: !!raw.open,
      hasKids: !!raw.hasKids,
      matched: !!raw.matched,
      parent: raw.parent || '',
      children: [],
      tokens: [],
      x: 0,
      y: 0,
      width: 0,
      height: BOX_H,
      midY: BOX_H / 2,
    }
    rows.push(row)
    if (row.parent) links.push({ from: row.parent, to: row.id, kind: 'tree' })
    maxDepth = Math.max(maxDepth, row.depth)
    return row
  }

  /** 目录（含子目录）里有多少文件因为 secret/二进制/超大被跳过——折叠态也要说清楚。 */
  function countSkipped(node) {
    let n = 0
    for (const child of node.children || []) {
      if (child.kind === 'dir') n += countSkipped(child)
      else if (child.skipped) n += 1
    }
    return n
  }

  function addSymbols(list, parentRow, depth) {
    const groups = symbolGroups(list)
    const memo = new Map()
    const allowed = (sym) => !kindSet || kindSet.has(sym.kind)
    const selfMatch = (sym) =>
      `${sym.name} ${sym.qname} ${sym.signature || ''} ${sym.route_path || ''}`
        .toLowerCase()
        .includes(q)
    const descendantMatch = (sid) => {
      if (memo.has(sid)) return memo.get(sid)
      const ok = (groups.get(sid) || []).some(
        (child) => allowed(child) && (selfMatch(child) || descendantMatch(child.id)),
      )
      memo.set(sid, ok)
      return ok
    }
    const recurse = (parentId, parent, d) => {
      for (const sym of groups.get(parentId) || []) {
        if (!allowed(sym)) continue
        if (!showHeuristic && isHeuristic(sym) && !q) continue
        if (q && !selfMatch(sym) && !descendantMatch(sym.id)) continue
        const row = mkRow({
          id: sym.id,
          kind: sym.kind,
          name: sym.name,
          label: sym.kind === 'route' ? `${sym.route_method || 'GET'} ${sym.name}` : sym.name,
          path: parent.path,
          depth: d,
          parent: parent.id,
          line: sym.line,
          end_line: sym.end_line,
          confidence: sym.confidence,
          hasKids: (groups.get(sym.id) || []).length > 0,
          matched: q ? selfMatch(sym) : false,
        })
        parent.children.push(row)
        recurse(sym.id, row, d + 1)
      }
    }
    recurse('', parentRow, depth)
  }

  function addFile(node, depth, parentId) {
    const syms = node.symbols || []
    const row = mkRow({
      id: node.id,
      kind: 'file',
      name: node.name,
      label: node.name,
      path: node.path || '',
      depth,
      parent: parentId,
      lang: node.lang,
      loc: node.loc,
      skipped: node.skipped,
      entrypoint: node.entrypoint,
      symbolCount: syms.length,
      // 有类型过滤时默认展开，用户**显式**折叠过的（false）才收起——
      // 否则"只看变量"给到的还是一片折叠的文件，等于没说
      open: autoExpand ? expanded?.[node.id] !== false : isExpanded(node.id),
      hasKids: syms.length > 0,
    })
    if (row.open || q) addSymbols(syms, row, depth + 1)
    return row
  }

  function addDir(node, depth, parentId) {
    const kids = node.children || []
    const row = mkRow({
      id: node.id,
      kind: 'dir',
      name: node.name,
      label: `${node.name}/`,
      path: node.path || '',
      depth,
      parent: parentId,
      files: node.files,
      loc: node.loc,
      symbolCount: node.symbol_count || 0,
      childCount: kids.length,
      skippedCount: showSkipped ? 0 : countSkipped(node),
      // 同上：类型过滤生效时目录默认展开，让命中文件直接可见
      open: (autoExpand ? expanded?.[node.id] !== false : isExpanded(node.id)) || !!q,
      hasKids: kids.length > 0,
    })
    if (!row.open) return row
    const visible = kids.filter((child) => {
      if (child.kind === 'dir') return !q || visibleByQuery(child)
      if (!showSkipped && child.skipped) return false
      return !q || visibleByQuery(child)
    })
    // 全是被跳过的文件时不要留一个"点了没反应"的箭头
    row.hasKids = visible.length > 0
    for (const child of visible) {
      row.children.push(
        child.kind === 'dir' ? addDir(child, depth + 1, row.id) : addFile(child, depth + 1, row.id),
      )
    }
    return row
  }

  if (map?.structure) {
    const root = addDir(map.structure, 0, '')

    // 自底向上量高度，再自顶向下摆 y：父节点落在子节点区间的正中
    const measure = (row) => {
      if (!row.children.length) {
        row.subH = ROW
        return ROW
      }
      let h = 0
      for (const child of row.children) h += measure(child)
      row.subH = h
      return h
    }
    measure(root)

    const place = (row, top) => {
      if (!row.children.length) {
        row.cy = top + ROW / 2
      } else {
        let y = top
        for (const child of row.children) {
          place(child, y)
          y += child.subH
        }
        const first = row.children[0]
        const last = row.children[row.children.length - 1]
        row.cy = (first.cy + last.cy) / 2
      }
      row.y = row.cy - BOX_H / 2
    }
    place(root, PAD)

    // 按深度分列：列宽 = 该列最宽的行；角标宽度也算进去，否则角标会溢出到下一列
    const colW = []
    for (const row of rows) {
      const isDir = row.kind === 'dir'
      const chipW = isDir ? 30 : 16
      const caretW = row.hasKids ? 16 : 0
      row.tokens = ((badgeOf ? badgeOf(row) : []) || [])
        .filter(Boolean)
        .map((tok) => ({ ...tok, w: tok.dot ? 12 : textWidth(tok.text) + 10 }))
      const tokensW = row.tokens.reduce((sum, tok) => sum + tok.w + TOKEN_GAP, 0)
      row.chipW = chipW
      row.caretW = caretW
      row.contentW =
        10 + chipW + 4 + textWidth(row.label) + (tokensW ? 12 + tokensW : 0) + caretW + 8
      colW[row.depth] = Math.max(colW[row.depth] || 0, row.contentW)
    }

    const colX = []
    let acc = PAD
    for (let d = 0; d <= maxDepth; d += 1) {
      colX[d] = acc
      acc += (colW[d] || 60) + COL_GAP
    }

    for (const row of rows) {
      row.x = colX[row.depth]
      // 每行只有**自己内容那么宽**：窄父节点到子节点之间的连线才会拉开成弧线；
      // 若让每行都撑满列宽，所有连线都缩成 26px 的小钩子，看起来就是表格而不是思维导图。
      // 列起点仍由该列最宽的行决定，兄弟节点因此纵向对齐。
      row.width = row.contentW
      row.chipX = 10
      row.labelX = 10 + row.chipW + 4
      // 角标从右往左贴：先让出 caret 的位置，再一个个往左排，永不压到文件名
      let tx = row.width - 8 - row.caretW
      row.caretX = tx + 4
      for (let i = row.tokens.length - 1; i >= 0; i -= 1) {
        tx -= row.tokens[i].w
        row.tokens[i].x = tx
        tx -= TOKEN_GAP
      }
    }

    const totalW = maxDepth >= 0 ? colX[maxDepth] + (colW[maxDepth] || 60) + PAD : 420
    const totalH = PAD * 2 + root.subH
    return {
      nodes: rows,
      links,
      clusters: [],
      width: Math.max(420, totalW),
      height: Math.max(240, totalH),
      rowHeight: ROW,
      gap: COL_GAP,
      columns: maxDepth + 1,
      depthX: colX,
      //: 根节点的纵坐标。根在整棵树的垂直中线上，打开面板时要**以它为中心**，
      //: 否则视野停在树的顶部，看起来"根节点不见了"。
      rootY: root.cy,
    }
  }

  return {
    nodes: [],
    links: [],
    clusters: [],
    width: 420,
    height: 240,
    rowHeight: ROW,
    gap: COL_GAP,
    columns: 1,
    depthX: [PAD],
    rootY: 120,
  }
}

/**
 * 内部导入的有向投影：导入方在左，被依赖方在右；循环依赖作为同层分组。
 * 1/2/3 按父目录层级聚合，4 逐文件；浅层文件始终留在其真实父包中。
 * allNodes/allLinks 保留过滤前的关系供详情使用，nodes/links 是同一套过滤后的图。
 */
export function layoutModules(map, {
  query, moduleDepth = 3, includeIsolated = false,
  focusId = '', focusDirection = 'both', focusHops = 1, changedPaths = null, scopePaths = null,
  availableWidth = 0,
} = {}) {
  const q = String(query || '').trim().toLowerCase()
  const depth = Math.max(1, Math.min(4, Math.floor(Number(moduleDepth) || 3)))
  const files = collectFileNodes(map?.structure)
  const normalize = (path) => String(path || '').replace(/\\/g, '/').replace(/^\.\//, '')
  const modulePathOf = (path) => depth === 4 ? path : path.split('/').slice(0, -1).slice(0, depth).join('/')
  const byFileId = new Map()
  const fileToModule = new Map()
  const buckets = new Map()
  const metadata = new Map((map?.modules || []).map((node) => [node.path, node]))

  for (const file of files) {
    const path = normalize(file.path)
    if (!path || file.skipped || fileToModule.has(path)) continue
    const modulePath = modulePathOf(path)
    let node = buckets.get(modulePath)
    if (!node) {
      const fullLabel = modulePath || '(根目录)'
      const parts = modulePath.split('/').filter(Boolean)
      const name = parts.at(-1) || '(根目录)'
      const parentPath = parts.slice(0, -1).join('/')
      const width = Math.max(216, Math.min(272, dependencyTextWidth(name, 14) + 68))
      node = {
        id: `mod:${modulePath}`, kind: 'module', name,
        path: modulePath, fullLabel, label: dependencyLabel(name, width - 68, 14),
        parentPath, parentLabel: dependencyLabel(parentPath || '项目根目录', width - 26, 10),
        isFile: depth === 4, members: [], files: 0, loc: 0,
        purpose: metadata.get(modulePath)?.purpose || '',
        importerCount: 0, dependencyCount: 0, importCount: 0, importedByCount: 0,
        internalImportCount: 0, connectedFiles: 0, internalCycleCount: 0,
        cyclic: false, cycleId: '', matched: false, focusRole: '',
        x: 0, y: 0, w: width, h: 72, width, height: 72,
      }
      buckets.set(modulePath, node)
    }
    node.members.push(path)
    node.files += 1
    node.loc += Math.max(0, Number(file.loc) || 0)
    fileToModule.set(path, node.id)
    byFileId.set(file.id || `file:${path}`, path)
    byFileId.set(`file:${path}`, path)
  }

  const connectedFiles = new Set()
  const fileEdges = []
  const aggregate = new Map()
  let importCount = 0
  let ignoredEdgeCount = 0
  for (const edge of map?.edges?.imports || []) {
    const fromPath = byFileId.get(edge.from)
    const toPath = byFileId.get(edge.to)
    if (!fromPath || !toPath || fromPath === toPath) { ignoredEdgeCount += 1; continue }
    const count = Math.max(1, Math.floor(Number(edge.count) || 1))
    const from = fileToModule.get(fromPath), to = fileToModule.get(toPath)
    connectedFiles.add(fromPath)
    connectedFiles.add(toPath)
    fileEdges.push({ from: fromPath, to: toPath })
    importCount += count
    if (from === to) {
      buckets.get(modulePathOf(fromPath)).internalImportCount += count
      continue
    }
    // JSON tuple avoids collisions when a legal filename contains an arrow or delimiter.
    const key = JSON.stringify([from, to])
    if (!aggregate.has(key)) aggregate.set(key, { from, to, count: 0, kind: 'imports' })
    aggregate.get(key).count += count
  }
  const allLinks = [...aggregate.values()].sort(dependencyLinkOrder)
  const allNodes = [...buckets.values()].sort(dependencyNodeOrder)
  const nodeById = new Map(allNodes.map((node) => [node.id, node]))
  const adjacency = dependencyAdjacency(allNodes.map((node) => node.id), allLinks)
  for (const link of allLinks) {
    nodeById.get(link.from).importCount += link.count
    nodeById.get(link.to).importedByCount += link.count
  }
  for (const node of allNodes) {
    node.members.sort(dependencyTextOrder)
    node.connectedFiles = node.members.filter((path) => connectedFiles.has(path)).length
    node.importerCount = adjacency.up.get(node.id).size
    node.dependencyCount = adjacency.down.get(node.id).size
    node.matched = !!q && `${node.fullLabel}\n${node.members.join('\n')}`.toLowerCase().includes(q)
  }
  // 同一包内的导入不等于循环；真正的内部环以文件 SCC 判定。
  const fileGraph = dependencyAdjacency([...connectedFiles].sort(dependencyTextOrder), fileEdges)
  const fileCycles = dependencyComponents(fileGraph).filter((group) => group.length > 1)
  for (const cycle of fileCycles) {
    const modules = new Set(cycle.map((path) => fileToModule.get(path)))
    if (modules.size === 1) nodeById.get([...modules][0]).internalCycleCount += 1
  }
  const components = dependencyComponents(adjacency)
  for (const members of components) {
    const cyclic = members.length > 1 || nodeById.get(members[0]).internalCycleCount > 0
    for (const id of members) {
      nodeById.get(id).cyclic = cyclic
      nodeById.get(id).cycleId = cyclic ? `cycle:${members[0]}` : ''
    }
  }

  const isolated = allNodes.filter((node) => !node.connectedFiles)
  let visible = new Set(allNodes.filter((node) => includeIsolated || node.connectedFiles).map((node) => node.id))
  const keep = (ids) => { visible = new Set([...visible].filter((id) => ids.has(id))) }
  if (q) keep(dependencyNeighborhood(allNodes.filter((node) => node.matched).map((node) => node.id), adjacency, 'both', 1))
  if (Array.isArray(scopePaths)) {
    const scoped = new Set(scopePaths.map(normalize))
    const members = allNodes.filter((node) => node.members.some((path) => scoped.has(path)))
    keep(dependencyNeighborhood(members.map((node) => node.id), adjacency, 'both', 1))
  }
  if (Array.isArray(changedPaths)) {
    const changed = new Set(changedPaths.map(normalize))
    const affected = allNodes.filter((node) => node.members.some((path) => changed.has(path)))
    keep(dependencyNeighborhood(affected.map((node) => node.id), adjacency, 'both', 1))
  }
  const focusedId = nodeById.has(focusId) ? focusId
    : nodeById.has(`mod:${focusId}`) ? `mod:${focusId}` : fileToModule.get(normalize(String(focusId).replace(/^file:/, '')))
  if (focusId) {
    const hops = Math.max(1, Math.min(2, Math.floor(Number(focusHops) || 1)))
    const direction = ['up', 'down'].includes(focusDirection) ? focusDirection : 'both'
    keep(dependencyNeighborhood(focusedId ? [focusedId] : [], adjacency, direction, hops))
    const upstream = dependencyNeighborhood(focusedId ? [focusedId] : [], adjacency, 'up', hops)
    const downstream = dependencyNeighborhood(focusedId ? [focusedId] : [], adjacency, 'down', hops)
    for (const node of allNodes) {
      node.focusRole = node.id === focusedId ? 'self'
        : upstream.has(node.id) && downstream.has(node.id) ? 'both'
          : upstream.has(node.id) ? 'up' : downstream.has(node.id) ? 'down' : ''
    }
  }
  const nodes = allNodes.filter((node) => visible.has(node.id))
  const links = allLinks.filter((link) => visible.has(link.from) && visible.has(link.to)).map((link) => ({ ...link }))
  const geometry = positionDependencyModules(nodes, links, components, availableWidth)
  return {
    nodes, links, allNodes, allLinks, files, fileCycles, ...geometry,
    stats: {
      moduleDepth: depth, totalModuleCount: allNodes.length, shownModuleCount: nodes.length,
      connectedModuleCount: allNodes.length - isolated.length, isolatedCount: isolated.length,
      hiddenIsolatedCount: includeIsolated ? 0 : isolated.length,
      totalLinkCount: allLinks.length, shownLinkCount: links.length,
      importCount, internalImportCount: allNodes.reduce((sum, node) => sum + node.internalImportCount, 0),
      shownImportCount: links.reduce((sum, link) => sum + link.count, 0) + nodes.reduce((sum, node) => sum + node.internalImportCount, 0),
      shownInternalImportCount: nodes.reduce((sum, node) => sum + node.internalImportCount, 0),
      cycleCount: components.filter((group) => nodeById.get(group[0]).cyclic).length,
      shownCycleCount: geometry.clusters.length,
      ignoredEdgeCount, focusMissing: !!focusId && !focusedId,
    },
  }
}

const dependencyTextOrder = (a, b) => a < b ? -1 : a > b ? 1 : 0
const dependencyNodeOrder = (a, b) => dependencyTextOrder(a.id, b.id)
const dependencyLinkOrder = (a, b) => dependencyTextOrder(a.from, b.from) || dependencyTextOrder(a.to, b.to)

// Dependency cards use 14px semibold titles and 10px paths; the tree keeps its 12px estimate.
// A little extra room accommodates the wider semibold glyphs and font fallback.
function dependencyTextWidth(text, fontSize) {
  return textWidth(text) * fontSize / 12 * 1.06
}

function dependencyLabel(text, maxWidth, fontSize = 12) {
  if (dependencyTextWidth(text, fontSize) <= maxWidth) return text
  const letters = [...text]
  let keep = letters.length - 1
  while (keep > 2) {
    const left = Math.ceil(keep / 2), right = Math.floor(keep / 2)
    const label = `${letters.slice(0, left).join('')}…${letters.slice(-right).join('')}`
    if (dependencyTextWidth(label, fontSize) <= maxWidth) return label
    keep -= 1
  }
  return `${letters[0]}…${letters.at(-1)}`
}

/** Reuse a track only after its occupied interval ends; intersecting routes stay distinct. */
function dependencyRouteLanes(entries, intervalOf, field) {
  const intervals = entries.map((link) => ({ link, ...intervalOf(link) }))
    .sort((a, b) => a.start - b.start || a.end - b.end || dependencyLinkOrder(a.link, b.link))
  const ends = []
  for (const { link, start, end } of intervals) {
    let lane = ends.findIndex((lastEnd) => lastEnd < start)
    if (lane < 0) lane = ends.length
    ends[lane] = end
    link[field] = lane
  }
  return ends.length
}

/** Inputs end on the left; SCC back edges use a separate lower band on the right. */
function dependencyPorts(nodes, links, groupByNode) {
  const outgoing = new Map(nodes.map((node) => [node.id, []]))
  const incoming = new Map(nodes.map((node) => [node.id, []]))
  const cycleIncoming = new Map(nodes.map((node) => [node.id, []]))
  for (const link of links) {
    outgoing.get(link.from).push(link)
    const sameGroup = groupByNode.get(link.from) === groupByNode.get(link.to)
    const targetInputs = sameGroup ? cycleIncoming : incoming
    targetInputs.get(link.to).push(link)
  }
  const spread = (edges, side, low, high, field) => {
    edges.sort(dependencyLinkOrder)
    edges.forEach((link, index) => {
      link[field] = { side, offset: low + (high - low) * (index + 1) / (edges.length + 1) }
    })
  }
  for (const node of nodes) {
    const hasCycleInputs = cycleIncoming.get(node.id).length > 0
    spread(outgoing.get(node.id), 'right', 10, hasCycleInputs ? 34 : node.height - 10, 'sourcePort')
    spread(incoming.get(node.id), 'left', 10, node.height - 10, 'targetPort')
    spread(cycleIncoming.get(node.id), 'right', 42, node.height - 10, 'targetPort')
  }
}

function dependencyAdjacency(ids, links) {
  const up = new Map(ids.map((id) => [id, new Set()]))
  const down = new Map(ids.map((id) => [id, new Set()]))
  for (const link of links) {
    if (!down.has(link.from) || !up.has(link.to)) continue
    down.get(link.from).add(link.to)
    up.get(link.to).add(link.from)
  }
  return { up, down }
}

function dependencyNeighborhood(seeds, adjacency, direction, hops) {
  const result = new Set(seeds), visit = (neighbors, start) => {
    let layer = new Set(start)
    const seen = new Set(start)
    for (let step = 0; step < hops; step += 1) {
      const next = new Set()
      for (const id of layer) for (const neighbor of neighbors.get(id) || []) {
        result.add(neighbor)
        if (!seen.has(neighbor)) { seen.add(neighbor); next.add(neighbor) }
      }
      layer = next
    }
  }
  // Both means ancestors plus descendants, rather than unrelated siblings reached by reversing direction mid-walk.
  if (direction !== 'down') visit(adjacency.up, seeds)
  if (direction !== 'up') visit(adjacency.down, seeds)
  return result
}

/** Iterative Kosaraju avoids call-stack limits on long dependency chains. */
function dependencyComponents({ up, down }) {
  const visited = new Set(), ordered = []
  for (const root of [...down.keys()].sort(dependencyTextOrder)) {
    if (visited.has(root)) continue
    const stack = [[root, false]]
    while (stack.length) {
      const [id, finished] = stack.pop()
      if (finished) { ordered.push(id); continue }
      if (visited.has(id)) continue
      visited.add(id)
      stack.push([id, true])
      const neighbors = [...down.get(id)].sort(dependencyTextOrder).reverse()
      for (const next of neighbors) if (!visited.has(next)) stack.push([next, false])
    }
  }
  const assigned = new Set(), groups = []
  for (const root of ordered.reverse()) {
    if (assigned.has(root)) continue
    const members = [], stack = [root]
    assigned.add(root)
    while (stack.length) {
      const id = stack.pop()
      members.push(id)
      for (const next of up.get(id)) if (!assigned.has(next)) { assigned.add(next); stack.push(next) }
    }
    groups.push(members.sort(dependencyTextOrder))
  }
  return groups.sort((a, b) => dependencyTextOrder(a[0], b[0]))
}

function positionDependencyModules(nodes, links, components, availableWidth = 0) {
  const PAD = 24, HEADER = 40, COLUMN_GAP = 88, NODE_GAP = 22, GROUP_GAP = 22, REGION_GAP = 24
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const groups = components.map((ids) => ids.filter((id) => byId.has(id))).filter((ids) => ids.length)
    .map((ids) => ({
      id: ids[0], nodes: ids.map((id) => byId.get(id)), rank: 0,
      cyclic: ids.some((id) => byId.get(id).cyclic),
    }))
  const groupByNode = new Map(groups.flatMap((group) => group.nodes.map((node) => [node.id, group.id])))
  dependencyPorts(nodes, links, groupByNode)
  const byGroup = new Map(groups.map((group) => [group.id, group]))
  const groupLinks = links.map((link) => ({ from: groupByNode.get(link.from), to: groupByNode.get(link.to) }))
    .filter((link) => link.from !== link.to)
  const graph = dependencyAdjacency(groups.map((group) => group.id), groupLinks)
  const weak = [], visited = new Set(), standaloneGroups = []
  for (const group of groups) {
    if (visited.has(group.id)) continue
    // A package can contain many imports while having no relationship to another card.
    // These cards belong in the same compact grid as other disconnected cards, not
    // in individual full-width graph rows. A multi-card SCC still forms a graph.
    if (group.nodes.length === 1 && !graph.up.get(group.id).size && !graph.down.get(group.id).size) {
      standaloneGroups.push(group)
      visited.add(group.id)
      continue
    }
    const stack = [group.id], members = []
    visited.add(group.id)
    while (stack.length) {
      const id = stack.pop()
      members.push(byGroup.get(id))
      for (const next of [...graph.up.get(id), ...graph.down.get(id)]) {
        if (!visited.has(next)) { visited.add(next); stack.push(next) }
      }
    }
    weak.push(members.sort((a, b) => dependencyTextOrder(a.id, b.id)))
  }
  const clusters = [], regions = [], componentBounds = new Map()
  const regionLabel = (members) => {
    const paths = members.map((node) => node.path.split('/').filter(Boolean))
    const shared = [...(paths[0] || [])]
    while (shared.length && paths.some((parts) => shared.some((part, index) => parts[index] !== part))) shared.pop()
    return shared.join('/') || '依赖组'
  }
  const clusterFor = (group, x, y, width) => ({
    id: group.nodes[0].cycleId, kind: 'cycle',
    label: group.nodes.length > 1 ? `循环依赖 · ${group.nodes.length} 模块`
      : group.nodes[0].internalCycleCount ? '包内循环依赖' : '循环依赖（部分）',
    members: group.nodes.map((node) => node.id), x: x - 12, y: y - 6,
    width: width + 24, height: group.height + 12,
  })
  for (const members of weak) {
    const memberIds = new Set(members.map((group) => group.id))
    const localLinks = links.filter((link) => memberIds.has(groupByNode.get(link.from)))
    const indegree = new Map(members.map((group) => [group.id, graph.up.get(group.id).size]))
    const ready = members.filter((group) => !indegree.get(group.id))
    for (let index = 0; index < ready.length; index += 1) {
      const group = ready[index]
      for (const next of [...graph.down.get(group.id)].sort(dependencyTextOrder)) {
        const child = byGroup.get(next)
        child.rank = Math.max(child.rank, group.rank + 1)
        indegree.set(next, indegree.get(next) - 1)
        if (!indegree.get(next)) ready.push(child)
      }
    }
    const bands = []
    for (const group of members) {
      if (!bands[group.rank]) bands[group.rank] = []
      bands[group.rank].push(group)
      group.height = group.nodes.length * (72 + NODE_GAP) - NODE_GAP + (group.cyclic ? 36 : 0)
    }
    // Insert invisible routing slots into skipped layers. Their space participates
    // in ordering and measurement, so a long edge goes through free space between
    // cards instead of wrapping every shortcut around the top of the whole graph.
    const orderingLinks = []
    for (const link of localLinks) {
      const from = byGroup.get(groupByNode.get(link.from)), to = byGroup.get(groupByNode.get(link.to))
      if (from === to) continue
      let previous = from.id
      link.routeSlots = []
      for (let rank = from.rank + 1; rank < to.rank; rank += 1) {
        const slot = { id: `route:${JSON.stringify([link.from, link.to, rank])}`, nodes: [], rank, height: 16, virtual: true }
        bands[rank].push(slot)
        link.routeSlots.push(slot)
        orderingLinks.push({ from: previous, to: slot.id })
        previous = slot.id
      }
      orderingLinks.push({ from: previous, to: to.id })
    }
    const ordering = dependencyAdjacency(bands.flatMap((band) => band.map((group) => group.id)), orderingLinks)
    // Repeated barycenter ordering reduces crossings while ties remain deterministic.
    for (let pass = 0; pass < 6; pass += 1) {
      const order = new Map(bands.flatMap((band) => band.map((group, index) => [group.id, index])))
      const forward = pass % 2 === 0
      const ranks = bands.map((_, index) => index)
      if (!forward) ranks.reverse()
      for (const rank of ranks) {
        const score = (group) => {
          const adjacent = [...(forward ? ordering.up : ordering.down).get(group.id)]
          return adjacent.length ? adjacent.reduce((sum, id) => sum + order.get(id), 0) / adjacent.length : order.get(group.id)
        }
        bands[rank].sort((a, b) => score(a) - score(b) || dependencyTextOrder(a.id, b.id))
      }
    }
    const bandHeights = bands.map((band) => band.reduce((sum, group) => sum + group.height + GROUP_GAP, -GROUP_GAP))
    const maxHeight = Math.max(...bandHeights)
    const bandWidths = bands.map((band) => Math.max(216, ...band.flatMap((group) => group.nodes.map((node) => node.width))))
    // Keep SCC return tracks outside the cards and reserve their full width.
    const cycleLaneCounts = bands.map((band) => Math.max(0, ...band.map((group) => {
      const relativeY = new Map(group.nodes.map((node, index) => [node.id, index * (72 + NODE_GAP)]))
      const cycleLinks = localLinks.filter((link) => groupByNode.get(link.from) === group.id && groupByNode.get(link.to) === group.id)
      return dependencyRouteLanes(cycleLinks, (link) => {
        const fromY = relativeY.get(link.from) + link.sourcePort.offset
        const toY = relativeY.get(link.to) + link.targetPort.offset
        return { start: Math.min(fromY, toY), end: Math.max(fromY, toY) }
      }, 'cycleLane')
    })))
    const bandGaps = cycleLaneCounts.map((count) => Math.max(COLUMN_GAP, 64 + Math.max(0, count - 1) * 7))
    const bandX = []
    let nextX = PAD
    for (let rank = 0; rank < bands.length; rank += 1) {
      bandX[rank] = nextX
      nextX += bandWidths[rank] + bandGaps[rank]
    }
    const regionNodes = members.flatMap((group) => group.nodes)
    const sharedPath = regionLabel(regionNodes)
    const region = {
      id: `region:${members[0].id}`, kind: 'component',
      label: sharedPath.split('/').at(-1), path: sharedPath === '依赖组' ? '' : sharedPath, title: sharedPath,
      members: regionNodes.map((node) => node.id).sort(dependencyTextOrder), linkCount: localLinks.length,
      x: 0, y: 0, width: bandX.at(-1) + bandWidths.at(-1) + PAD,
      height: HEADER + maxHeight + PAD, links: localLinks, nodes: regionNodes, clusters: [],
    }
    for (let rank = 0; rank < bands.length; rank += 1) {
      let y = HEADER + (maxHeight - bandHeights[rank]) / 2
      for (const group of bands[rank]) {
        const startY = y
        if (group.virtual) {
          group.y = y + group.height / 2
          group.x = bandX[rank]
          group.width = bandWidths[rank]
        } else {
          if (group.cyclic) y += 24
          for (const node of group.nodes) {
            node.x = bandX[rank]
            node.y = y
            node.layer = rank
            node.regionId = region.id
            y += node.height + NODE_GAP
            componentBounds.set(node.id, { width: bandWidths[rank] })
          }
          if (group.cyclic) region.clusters.push(clusterFor(group, bandX[rank], startY, bandWidths[rank]))
        }
        y = startY + group.height + GROUP_GAP
      }
    }
    for (const link of localLinks) {
      const from = byId.get(link.from)
      if (groupByNode.get(link.from) === groupByNode.get(link.to)) {
        link.cycleRouteX = from.x + componentBounds.get(from.id).width + 32 + link.cycleLane * 7
        region.width = Math.max(region.width, link.cycleRouteX + PAD)
      }
      // Serializable route data is useful for geometry checks; slots never become
      // nodes or extra imports in the public graph.
      link.routePoints = (link.routeSlots || []).map((slot) => ({
        layer: slot.rank, x: slot.x, width: slot.width, y: slot.y,
      }))
      delete link.routeSlots
    }
    regions.push(region)
  }
  const requestedWidth = Number(availableWidth)
  const desiredWidth = Number.isFinite(requestedWidth) && requestedWidth > 0 ? Math.max(320, requestedWidth) : 1040
  let packingWidth = Math.max(desiredWidth, ...regions.map((region) => region.width))
  const regionOrder = (a, b) => (b.kind === 'component') - (a.kind === 'component')
    || b.height * b.width - a.height * a.width || dependencyTextOrder(a.id, b.id)
  if (standaloneGroups.length) {
    const maxCellWidth = Math.max(...standaloneGroups.map((group) => Math.max(...group.nodes.map((node) => node.width)) + (group.cyclic ? 24 : 0)))
    const maxColumns = Math.max(1, Math.min(4, standaloneGroups.length, Math.floor((packingWidth - PAD * 2 + REGION_GAP) / (maxCellWidth + REGION_GAP))))
    const gridShape = (columns) => {
      const rowHeights = []
      standaloneGroups.forEach((group, index) => {
        const row = Math.floor(index / columns)
        rowHeights[row] = Math.max(rowHeights[row] || 0, group.cyclic ? 120 : 72)
      })
      return {
        columns, rowHeights,
        width: PAD * 2 + columns * maxCellWidth + (columns - 1) * REGION_GAP,
        height: HEADER + rowHeights.reduce((sum, value) => sum + value, 0) + (rowHeights.length - 1) * NODE_GAP + PAD,
      }
    }
    // A narrow two-column grid often fits beside a medium component, whereas a
    // four-column grid would create another entire row. Try those few shapes
    // together with a small width allowance instead of making the left column
    // hundreds of pixels longer just to save one horizontal gutter.
    let chosen = null
    for (let columns = 1; columns <= maxColumns; columns += 1) {
      const shape = gridShape(columns)
      const candidates = new Set([Math.max(packingWidth, shape.width)])
      for (const region of regions) {
        const together = region.width + REGION_GAP + shape.width
        if (together <= packingWidth * 1.1) candidates.add(Math.max(packingWidth, together))
      }
      for (const target of candidates) {
        const blocks = [...regions, { ...shape, kind: 'standalone', id: 'region:standalone' }].sort(regionOrder)
        let cursorX = 0, cursorY = 0, rowHeight = 0, usedWidth = 0
        for (const block of blocks) {
          if (cursorX && cursorX + block.width > target) { cursorX = 0; cursorY += rowHeight + REGION_GAP; rowHeight = 0 }
          usedWidth = Math.max(usedWidth, cursorX + block.width)
          cursorX += block.width + REGION_GAP
          rowHeight = Math.max(rowHeight, block.height)
        }
        const score = cursorY + rowHeight + Math.max(0, usedWidth - packingWidth) * 1.5
        if (!chosen || score < chosen.score || (score === chosen.score && columns > chosen.columns)) chosen = { ...shape, score, packingWidth: target }
      }
    }
    const { columns, rowHeights } = chosen
    packingWidth = chosen.packingWidth
    const rowY = []
    let nextY = HEADER
    for (const rowHeight of rowHeights) { rowY.push(nextY); nextY += rowHeight + NODE_GAP }
    const region = {
      id: 'region:standalone', kind: 'standalone', label: '独立模块', path: '', title: '当前视图没有跨模块连线', linkCount: 0,
      members: standaloneGroups.flatMap((group) => group.nodes.map((node) => node.id)),
      x: 0, y: 0, width: chosen.width, height: chosen.height,
      links: [], nodes: standaloneGroups.flatMap((group) => group.nodes), clusters: [],
    }
    standaloneGroups.forEach((group, index) => {
      const node = group.nodes[0], x = PAD + (index % columns) * (maxCellWidth + REGION_GAP)
      const y = rowY[Math.floor(index / columns)]
      node.x = x + (group.cyclic ? 12 : 0)
      node.y = y + (group.cyclic ? 30 : 0)
      node.layer = 0
      node.regionId = region.id
      group.height = node.height + 36
      if (group.cyclic) region.clusters.push(clusterFor(group, node.x, y + 6, node.width))
    })
    regions.push(region)
  }
  // Lay out independent subgraphs as compact blocks. A stable shelf keeps small
  // groups side by side while a wide group retains enough space for real routes.
  regions.sort(regionOrder)
  let x = PAD, y = PAD, rowHeight = 0, width = 0, height = 0
  for (const region of regions) {
    if (x > PAD && x + region.width > packingWidth + PAD) { x = PAD; y += rowHeight + REGION_GAP; rowHeight = 0 }
    region.x = x
    region.y = y
    region.label = dependencyLabel(region.label, Math.max(80, region.width - 160), 12)
    for (const node of region.nodes) { node.x += x; node.y += y }
    for (const cluster of region.clusters) { cluster.x += x; cluster.y += y; clusters.push(cluster) }
    for (const link of region.links) {
      if (link.cycleRouteX !== undefined) link.cycleRouteX += x
      for (const point of link.routePoints) { point.x += x; point.y += y }
    }
    width = Math.max(width, x + region.width + PAD)
    height = Math.max(height, y + region.height + PAD)
    x += region.width + REGION_GAP
    rowHeight = Math.max(rowHeight, region.height)
    delete region.nodes
    delete region.links
    delete region.clusters
  }
  for (const link of links) {
    const from = byId.get(link.from), to = byId.get(link.to)
    link.x1 = from.x + from.width
    link.y1 = from.y + link.sourcePort.offset
    link.y2 = to.y + link.targetPort.offset
    if (groupByNode.get(link.from) === groupByNode.get(link.to)) {
      link.x2 = to.x + to.width
      const lane = link.cycleRouteX
      link.path = `M ${link.x1} ${link.y1} L ${lane} ${link.y1} L ${lane} ${link.y2} L ${link.x2} ${link.y2}`
    } else {
      link.x2 = to.x
      let cursorX = from.x + componentBounds.get(from.id).width + 22, cursorY = link.y1
      let path = `M ${link.x1} ${link.y1} L ${cursorX} ${cursorY}`
      const enter = (targetX, targetY) => {
        const midX = (cursorX + targetX) / 2
        path += ` C ${midX} ${cursorY}, ${midX} ${targetY}, ${targetX} ${targetY}`
        cursorX = targetX
        cursorY = targetY
      }
      for (const point of link.routePoints) {
        enter(point.x - 22, point.y)
        cursorX = point.x + point.width + 22
        path += ` L ${cursorX} ${cursorY}`
      }
      enter(link.x2 - 22, link.y2)
      link.path = `${path} L ${link.x2} ${link.y2}`
    }
  }
  return { clusters, regions, width: Math.max(420, width), height: Math.max(240, height) }
}

function collectFileNodes(structure) {
  const out = []
  const walk = (node) => {
    if (node.kind === 'file') out.push({ ...node })
    for (const child of node.children || []) walk(child)
  }
  if (structure) walk(structure)
  return out
}

/**
 * 把服务端 delta 贴到已有的地图上（局部更新）。
 *
 * 只有"能确定地贴对"的情况才贴：
 *   - 文件新增/删除 → 树的形状变了，返回 {patched:false} 让调用方整取；
 *   - 边有变化 / 截断 → 同上（边集需要重算）；
 *   - 否则按 path 找到文件节点，增/改/删它的符号列表。
 * 返回 {patched, changedFiles, added, updated, removed}，供 UI 做临时高亮。
 */
export function applyDelta(map, delta) {
  const result = {
    patched: false,
    changedFiles: [],
    added: new Set(),
    updated: new Set(),
    removed: new Set(),
  }
  if (!map || !delta) return result
  const kinds = new Set((delta.changes || []).map((c) => c.kind))
  if (delta.truncated || delta.edges_dirty || kinds.has('file_added') || kinds.has('file_removed')) {
    return result
  }

  const fileNodes = new Map()
  const walk = (node) => {
    if (node.kind === 'file') fileNodes.set(node.path, node)
    for (const child of node.children || []) walk(child)
  }
  if (map.structure) walk(map.structure)

  const added = delta.added || []
  const updated = delta.updated || []
  const removed = delta.removed || []
  const byPath = new Map()
  const push = (sym, bucket) => {
    if (!byPath.has(sym.path)) byPath.set(sym.path, [])
    byPath.get(sym.path).push(sym)
    bucket.add(sym.id)
  }
  for (const sym of added) push(sym, result.added)
  for (const sym of updated) push(sym, result.updated)
  for (const id of removed) {
    result.removed.add(id)
    const rel = String(id).replace(/^sym:/, '').split('#')[0]
    if (!byPath.has(rel)) byPath.set(rel, [])
  }

  // 先整体判定能不能贴：任一文件节点缺失就放弃（宁可整取，也不留下半对的地图）
  for (const rel of byPath.keys()) {
    if (!fileNodes.has(rel)) return { ...result, patched: false }
  }

  for (const [rel, incoming] of byPath.entries()) {
    const node = fileNodes.get(rel)
    const removedHere = new Set(
      removed.filter((id) => String(id).startsWith(`sym:${rel}#`))
    )
    const merged = new Map(
      (node.symbols || []).filter((s) => !removedHere.has(s.id)).map((s) => [s.id, s])
    )
    for (const sym of incoming) merged.set(sym.id, sym)
    node.symbols = [...merged.values()].sort((a, b) => a.line - b.line)
    result.changedFiles.push(rel)
  }

  result.patched = true
  return result
}

/** 在当前可见节点里按 id 找节点（供选中/居中用）。 */
export function findNode(layout, id) {
  return layout.nodes.find((n) => n.id === id) || null
}
/**
 * 功能导航树（第三张图）：能力域 → 功能 → 细分功能 → … → 原子步骤。
 *
 * 与结构/依赖投影的关系：同一套渲染，只换数据来源。
 * 结构图回答"代码长什么样"，依赖图回答"谁引用谁"，功能图回答"这个项目能干什么、
 * 这件事又是怎么拆开的"。
 *
 * 这里只做**排版**：树由组件按需装配（点开一个节点才去取它的下一层，可以一直拆下去）。
 * 布局本身与结构树同一套规则（按深度分列 + 父子垂直居中的 tidy tree），
 * 因为"层层展开的图"读起来必须是同一个形状。
 */
export function layoutFeatureTree(root, { rowHeight = 26, colGap = 56, pad = 18 } = {}) {
  const ROW = rowHeight
  const COL_GAP = colGap
  const BOX_H = ROW - 4
  const PAD = pad
  const TOKEN_GAP = 6
  const rows = []
  const links = []
  let maxDepth = 0

  function build(node, depth) {
    const row = {
      id: node.id,
      kind: node.kind,
      name: node.name || '',
      label: node.label || node.name || node.id,
      path: node.path || '',
      line: node.line,
      end_line: node.end_line,
      feature: node.feature,
      node: node.node,
      doc: node.doc || '',
      doc_path: node.doc_path || '',
      count: node.count || 0,
      own: node.own || 0,
      business: node.business,
      open: !!node.open,
      hasKids: !!node.hasKids,
      //: 不能继续展开时，用 caret 那一位表达状态：`·` 原子、`≡` 共用、`↺` 回到上层。
      //: 放在这里是因为"能不能继续往下走"本来就是 caret 要回答的问题，不必再占一颗药丸。
      marker: node.marker || '',
      markerTitle: node.markerTitle || '',
      childCount: node.childCount || 0,
      loading: !!node.loading,
      note: node.note || '',
      matched: !!node.matched,
      depth,
      children: [],
      tokens: (node.tokens || []).map((tok) => ({ ...tok })),
      x: 0,
      y: 0,
      width: 0,
      height: BOX_H,
      midY: BOX_H / 2,
    }
    rows.push(row)
    maxDepth = Math.max(maxDepth, depth)
    for (const child of node.children || []) {
      const childRow = build(child, depth + 1)
      row.children.push(childRow)
      links.push({ from: row.id, to: childRow.id, kind: 'tree' })
    }
    return row
  }

  const rootRow = build(root, 0)

  const measure = (row) => {
    if (!row.children.length) {
      row.subH = ROW
      return ROW
    }
    let h = 0
    for (const child of row.children) h += measure(child)
    row.subH = h
    return h
  }
  measure(rootRow)

  const place = (row, top) => {
    if (!row.children.length) {
      row.cy = top + ROW / 2
    } else {
      let y = top
      for (const child of row.children) {
        place(child, y)
        y += child.subH
      }
      const first = row.children[0]
      const last = row.children[row.children.length - 1]
      row.cy = (first.cy + last.cy) / 2
    }
    row.y = row.cy - BOX_H / 2
  }
  place(rootRow, PAD)

  // 按深度分列：列宽 = 该列最宽的行；角标宽度也算进去，否则角标会溢出到下一列
  const colW = []
  for (const row of rows) {
    const chipW = row.kind === 'domain' ? 22 : 18
    // caret 位既装"可展开的箭头"，也装"到头了"的记号——两种都要留同样宽度
    const caretW = row.hasKids || row.marker ? 16 : 0
    if (row.kind === 'domain' && !row.open && row.childCount) {
      row.tokens = [
        { text: `${row.childCount} 项`, cls: 'tok-count', title: '这里还是折叠的：点一下展开' },
      ]
    }
    row.tokens = (row.tokens || []).map((tok) => ({
      ...tok,
      w: tok.dot ? 12 : textWidth(tok.text) + 10,
    }))
    const tokensW = row.tokens.reduce((sum, tok) => sum + tok.w + TOKEN_GAP, 0)
    row.chipW = chipW
    row.caretW = caretW
    row.contentW =
      10 + chipW + 4 + textWidth(row.label) + (tokensW ? 12 + tokensW : 0) + caretW + 8
    colW[row.depth] = Math.max(colW[row.depth] || 0, row.contentW)
  }

  const colX = []
  let acc = PAD
  for (let d = 0; d <= maxDepth; d += 1) {
    colX[d] = acc
    acc += (colW[d] || 60) + COL_GAP
  }
  for (const row of rows) {
    row.x = colX[row.depth]
    row.width = row.contentW
    row.chipX = 10
    row.labelX = 10 + row.chipW + 4
    let tx = row.width - 8 - row.caretW
    row.caretX = tx + 4
    for (let i = row.tokens.length - 1; i >= 0; i -= 1) {
      tx -= row.tokens[i].w
      row.tokens[i].x = tx
      tx -= TOKEN_GAP
    }
  }

  const totalW = colX[maxDepth] + (colW[maxDepth] || 60) + PAD
  return {
    nodes: rows,
    links,
    clusters: [],
    width: Math.max(420, totalW),
    height: Math.max(240, PAD * 2 + rootRow.subH),
    rowHeight: ROW,
    gap: COL_GAP,
    columns: maxDepth + 1,
    depthX: colX,
    rootY: rootRow.cy,
  }
}

/** 功能来源 → 角标文字（与后端 `source` 字段一一对应）。 */
export const SOURCE_LABEL = {
  route: 'HTTP',
  command: '命令',
  tool: '工具',
  hook: '钩子',
  component: '组件',
  member: '成员',
  call: '调用',
  public: '公开',
  container: '容器',
}

/**
 * 证据三态 → **一个点**（`direct` 实心 · `file` 空心 · `none` 灰点）。
 *
 * 为什么只留一个点、不给文字：三种状态回答的是同一个问题（"谁在守着这条功能"），
 * 就该占同一个视觉通道、同一个宽度。旧版给"直接/文件级"配上文字药丸、给"没有"
 * 配另一种颜色，一行里和来源、原子、共用挤在一起，读的人分不清哪颗在答哪个问题。
 * 数量与文件名进悬停提示——状态条已经报了全局数量，单个节点不必再重复。
 */
export function evidenceTokens(node) {
  const state = node?.test_state
  const files = (node.tests || []).join('、')
  if (state === 'direct') {
    return [
      {
        dot: true,
        cls: 'tok-cov-direct',
        title: `有测试直接盯着（按词边界提到）：${files || '（文件名缺失）'}`,
      },
    ]
  }
  if (state === 'file') {
    return [
      {
        dot: true,
        cls: 'tok-cov-file',
        title: `${node.test_note || '只有文件级证据：测试 import 了这个模块，不保证盯着这一条'}：${files}`,
      },
    ]
  }
  return [
    {
      dot: true,
      cls: 'tok-cov-none',
      title: '没有找到测试：这条功能目前没有可执行的证据',
    },
  ]
}
