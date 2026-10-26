// Graph editing history. View navigation and server save metadata stay current.
const GRAPH_FIELDS = ['schema_version', 'name', 'nodes', 'edges']
const encoder = new TextEncoder()

function optionInteger(options, key, fallback) {
  const value = options[key] ?? fallback
  if (!Number.isSafeInteger(value) || value < 0) throw new RangeError(`${key} must be a non-negative integer`)
  return value
}

function canonicalJson(value) {
  if (Array.isArray(value)) return value.map(canonicalJson)
  if (value && typeof value === 'object') {
    const result = {}
    for (const key of Object.keys(value).sort()) result[key] = canonicalJson(value[key])
    return result
  }
  return value
}

function snapshot(document) {
  if (!document || typeof document !== 'object' || Array.isArray(document)) throw new TypeError('A workflow document is required')
  const graph = {}
  for (const field of GRAPH_FIELDS) graph[field] = document[field]
  // JSON also accepts Vue reactive documents and incomplete form drafts.
  const json = JSON.stringify(canonicalJson(graph))
  return Object.freeze({ json, bytes: encoder.encode(json).byteLength })
}

function mergeDocument(document, entry) {
  // Clone the metadata too: callers can safely mutate the returned viewport.
  return { ...JSON.parse(JSON.stringify(document)), ...JSON.parse(entry.json) }
}

function endGroup(history) {
  history.lastGroup = null
  history.lastTime = null
}

function trimHistory(history) {
  history.bytes = [...history.past, ...history.future].reduce((sum, entry) => sum + entry.bytes, 0)
  while (history.past.length + history.future.length > history.maxEntries || history.bytes > history.maxBytes) {
    // Keep the nearest undo/redo steps. Future is a stack, so its first item is
    // the furthest redo target. Removing old entries never creates a gap.
    const removed = history.past.length ? history.past.shift() : history.future.shift()
    if (!removed) break
    history.bytes -= removed.bytes
  }
}

/**
 * maxEntries/maxBytes bound recoverable undo and redo snapshots together.
 * One additional current baseline is retained for the next graph edit.
 */
export function createWorkflowHistory(document, options = {}) {
  const history = {
    past: [], present: snapshot(document), future: [], bytes: 0,
    maxEntries: optionInteger(options, 'maxEntries', 50),
    maxBytes: optionInteger(options, 'maxBytes', 4 * 1024 * 1024),
    mergeWindowMs: optionInteger(options, 'mergeWindowMs', 600),
    lastGroup: null, lastTime: null,
  }
  return history
}

/** Reset the baseline after loading another document; return whether it changed. */
export function resetWorkflowHistory(history, document) {
  const next = snapshot(document)
  const changed = next.json !== history.present.json || Boolean(history.past.length || history.future.length)
  history.past = []
  history.present = next
  history.future = []
  history.bytes = 0
  endGroup(history)
  return changed
}

/**
 * Record the document AFTER an edit. A non-empty group joins adjacent text
 * edits inside mergeWindowMs; null/omitted group records an independent edit.
 */
export function recordWorkflowHistory(history, document, options = {}) {
  const next = snapshot(document)
  const group = typeof options.group === 'string' && options.group.length ? options.group : null
  const time = options.time ?? Date.now()
  if (!Number.isFinite(time)) throw new RangeError('time must be finite')
  if (next.json === history.present.json) {
    if (group === null || group !== history.lastGroup) endGroup(history)
    return false
  }
  const merge = group !== null && group === history.lastGroup && history.lastTime !== null
    && time >= history.lastTime && time - history.lastTime <= history.mergeWindowMs
  if (!merge) history.past.push(history.present)
  history.present = next
  history.future = []
  history.lastGroup = group
  history.lastTime = group === null ? null : time
  trimHistory(history)
  return true
}

export function canUndoWorkflow(history) { return Boolean(history?.past?.length) }
export function canRedoWorkflow(history) { return Boolean(history?.future?.length) }

/** Return a detached document or null if there is no step to undo. */
export function undoWorkflowHistory(history, currentDocument) {
  // An unrecorded form edit is still an edit; preserve it as the redo target.
  if (snapshot(currentDocument).json !== history.present.json) recordWorkflowHistory(history, currentDocument)
  endGroup(history)
  if (!canUndoWorkflow(history)) return null
  history.future.push(history.present)
  history.present = history.past.pop()
  trimHistory(history)
  return mergeDocument(currentDocument, history.present)
}

/** Return a detached document or null if there is no step to redo. */
export function redoWorkflowHistory(history, currentDocument) {
  if (snapshot(currentDocument).json !== history.present.json) {
    // A fresh edit creates a branch and invalidates all redo targets.
    recordWorkflowHistory(history, currentDocument)
    return null
  }
  endGroup(history)
  if (!canRedoWorkflow(history)) return null
  history.past.push(history.present)
  history.present = history.future.pop()
  trimHistory(history)
  return mergeDocument(currentDocument, history.present)
}
