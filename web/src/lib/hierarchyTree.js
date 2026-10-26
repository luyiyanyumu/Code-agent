// Tree-only logic stays separate from the editor: server handles remain opaque,
// repeated symbols stop at the ancestor boundary, and huge responses stay bounded.
let sequence = 0
export const HIERARCHY_MAX_DEPTH = 24
export const HIERARCHY_MAX_CHILDREN = 200
export const HIERARCHY_MAX_VISIBLE = 1000

export function hierarchyItemKey(item) {
  const start = item?.selectionRange?.start || item?.range?.start || {}
  return `${item?.uri || ''}\0${start.line ?? -1}:${start.character ?? -1}\0${item?.name || ''}`
}

export function hierarchyNode(item, ancestors = [], ranges = [], rangeUri = item?.uri) {
  const key = hierarchyItemKey(item)
  return { id: ++sequence, key, item, ranges, rangeUri, ancestors, depth: ancestors.length,
    cycle: ancestors.includes(key), expanded: false, loaded: false, loading: false,
    children: [], error: '', omitted: 0 }
}

export function attachHierarchyChildren(node, rows = []) {
  const valid = Array.isArray(rows) ? rows.filter(row => row?.item) : []
  node.children = valid.slice(0, HIERARCHY_MAX_CHILDREN).map(row => hierarchyNode(row.item, [...node.ancestors, node.key], row.ranges || [], row.rangeUri || row.item.uri))
  node.omitted = Math.max(0, valid.length - node.children.length)
  node.loaded = true
}

export function flattenHierarchy(nodes = []) {
  const result = []
  const visit = list => {
    for (const node of list) {
      if (result.length >= HIERARCHY_MAX_VISIBLE) return
      result.push(node)
      if (node.expanded && !node.cycle && node.depth < HIERARCHY_MAX_DEPTH) visit(node.children)
    }
  }
  visit(nodes)
  return result
}

export function hierarchyCanExpand(node) {
  return !node.cycle && node.depth < HIERARCHY_MAX_DEPTH && (!node.loaded || node.children.length > 0)
}

export function hierarchyCancellation() {
  const listeners = new Set()
  return { isCancellationRequested: false,
    onCancellationRequested(listener) {
      listeners.add(listener)
      return { dispose: () => listeners.delete(listener) }
    },
    cancel() {
      if (this.isCancellationRequested) return
      this.isCancellationRequested = true
      for (const listener of [...listeners]) listener()
      listeners.clear()
    },
  }
}
