import assert from 'node:assert/strict'
import test from 'node:test'
import { attachHierarchyChildren, flattenHierarchy, hierarchyCanExpand, hierarchyCancellation, hierarchyItemKey, hierarchyNode, HIERARCHY_MAX_CHILDREN, HIERARCHY_MAX_DEPTH, HIERARCHY_MAX_VISIBLE } from '../src/lib/hierarchyTree.js'

const item = (name = 'root') => ({ uri: `file:///project/${name}.java`, name, selectionRange: { start: { line: 2, character: 0 } } })
test('hierarchy keeps distinct locations and stops recursive cycles at an ancestor', () => {
  const root = hierarchyNode(item())
  attachHierarchyChildren(root, [{ item: item('child'), ranges: [{ start: { line: 4 } }] }])
  root.expanded = true
  const child = root.children[0]
  child.expanded = true
  attachHierarchyChildren(child, [{ item: item() }])
  assert.equal(child.children[0].cycle, true)
  assert.equal(hierarchyCanExpand(child.children[0]), false)
  assert.equal(flattenHierarchy([root]).length, 3)
  assert.equal(child.ranges.length, 1)
  assert.notEqual(hierarchyItemKey(item()), hierarchyItemKey({ ...item(), selectionRange: { start: { line: 3, character: 0 } } }))
})
test('hierarchy bounds very large responses, rendering and deep traversal without discarding the source item', () => {
  const raw = item(), root = hierarchyNode(raw)
  attachHierarchyChildren(root, Array.from({ length: HIERARCHY_MAX_CHILDREN + 17 }, (_, i) => ({ item: item(`child-${i}`) })))
  assert.equal(root.item, raw)
  assert.equal(root.children.length, HIERARCHY_MAX_CHILDREN); assert.equal(root.omitted, 17)
  assert.equal(hierarchyCanExpand(hierarchyNode(item(), Array(HIERARCHY_MAX_DEPTH).fill('ancestor'))), false)
  const roots = Array.from({ length: HIERARCHY_MAX_VISIBLE + 3 }, (_, i) => hierarchyNode(item(`${i}`)))
  assert.equal(flattenHierarchy(roots).length, HIERARCHY_MAX_VISIBLE)
})
test('hierarchy cancellation triggers only active subscriptions and is idempotent', () => {
  const token = hierarchyCancellation(), calls = []
  const old = token.onCancellationRequested(() => calls.push('old'))
  token.onCancellationRequested(() => calls.push('active'))
  old.dispose(); token.cancel(); token.cancel()
  assert.deepEqual(calls, ['active']); assert.equal(token.isCancellationRequested, true)
})
