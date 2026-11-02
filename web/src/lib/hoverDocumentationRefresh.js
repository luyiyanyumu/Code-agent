// Monaco 0.56 has no HoverProvider.onDidChange. Keep this small adapter at the
// native-widget boundary; language/provider code must not depend on internals.
const CONTRIBUTION = 'editor.contrib.contentHover'

function present(node) {
  if (!node?.isConnected || !node.getClientRects?.().length) return false
  const style = node.ownerDocument?.defaultView?.getComputedStyle?.(node)
  return style?.visibility !== 'hidden' && style?.display !== 'none'
}

function visible(editor, controller, wrapper) {
  return !!controller?.isHoverVisible && !!wrapper?.isVisible
    && present(editor.getDomNode?.()) && present(wrapper.getDomNode?.())
}

function sameContext(a, b) {
  return a && b && a.projectId === b.projectId && (a.tree || 'main') === (b.tree || 'main')
    && a.uri === b.uri && Number.isInteger(a.version) && a.version === b.version
}

function sameSymbol(model, range, position) {
  if (!position) return true // Some older servers only invalidate a document.
  if (!Number.isInteger(position.line) || position.line < 0 || !Number.isInteger(position.character) || position.character < 0) return false
  const line = position.line + 1, column = position.character + 1
  if (line !== range.startLineNumber) return false
  const word = model.getWordAtPosition?.({ lineNumber: range.startLineNumber, column: range.startColumn })
  return word ? column >= word.startColumn && column < word.endColumn
    : column >= range.startColumn && column <= range.endColumn
}

/**
 * Refresh an already visible mouse hover at its original anchor, never at the
 * caret. contextForModel returns {projectId, tree, uri, version}, where version
 * is the LSP document revision (not necessarily Monaco's model revision).
 *
 * The two private members below are verified against installed Monaco 0.56:
 * ContentHoverController._onHoverContentsChanged / _contentWidget and
 * ContentHoverWidgetWrapper._currentResult / _startShowingOrUpdateHover.
 * Feature-detect all of them and fail closed on other Monaco versions.
 */
export function createHoverDocumentationRefresh({ editor, contextForModel } = {}) {
  const subscriptions = [], stale = new WeakSet()
  let disposed = false, snapshot = null, deferred = null, controller
  try { controller = editor?.getContribution?.(CONTRIBUTION) } catch { /* An unmounted editor is inert. */ }
  const event = controller?._onHoverContentsChanged?.event
  const supported = !!editor && typeof contextForModel === 'function' && typeof event === 'function'
  const invalidate = () => {
    const result = controller?._contentWidget?._currentResult
    if (result && typeof result === 'object') stale.add(result)
    snapshot = null
    deferred = null
  }
  const capture = () => {
    const wrapper = controller?._contentWidget, result = wrapper?._currentResult, model = editor?.getModel?.()
    if (disposed || !visible(editor, controller, wrapper) || !result || stale.has(result) || !model || model.isDisposed?.()) return
    const anchor = result.options?.anchor, context = contextForModel(model)
    if (anchor?.type !== 1 || !anchor.range || !context?.uri || !Number.isInteger(context.version)) return
    snapshot = { wrapper, result, anchor, model, modelVersion: model.getVersionId?.(), context: { ...context } }
    if (deferred && deferred.model === model && deferred.modelVersion === snapshot.modelVersion
      && deferred.options === result.options && deferred.expires > Date.now()) {
      const update = deferred.change
      deferred = null
      // A fast translation can finish before Monaco renders its pending
      // receipt. Consume that one terminal notification only when this same
      // in-flight operation actually becomes a visible pending card.
      const pending = result.hoverParts?.some(part => part.contents?.some(content =>
        /正在翻译中文说明|中文说明准备中|尚未翻译/.test(content?.value || '')))
      if (pending) queueMicrotask(() => {
        if (snapshot?.result === result) refreshCurrent(update)
      })
    }
  }
  const refreshCurrent = change => {
    if (!snapshot) return false
    const { wrapper, result, anchor, model, modelVersion, context } = snapshot
    if (model.isDisposed?.() || model !== editor.getModel?.() || model.getVersionId?.() !== modelVersion
      || wrapper !== controller._contentWidget || wrapper._currentResult !== result || stale.has(result)
      || !visible(editor, controller, wrapper) || wrapper.isFocused || result.options?.shouldFocus
      || !sameContext(context, contextForModel(model)) || !sameContext(context, change)
      || !sameSymbol(model, anchor.range, change.position)
      || typeof wrapper.hide !== 'function' || typeof wrapper._startShowingOrUpdateHover !== 'function') return false
    // hide() cancels computation and clears both operation.options and the
    // old result: starting again at an identical anchor otherwise short-circuits.
    const source = result.options.source ?? 0
    snapshot = null
    wrapper.hide()
    wrapper._startShowingOrUpdateHover(anchor, 1 /* Immediate */, source, false, null)
    return true
  }
  if (supported) {
    subscriptions.push(event(capture))
    for (const method of ['onDidChangeModel', 'onDidChangeModelContent', 'onDidDispose']) {
      if (typeof editor[method] === 'function') subscriptions.push(editor[method](invalidate))
    }
    capture()
  }
  return {
    supported,
    refresh(change) {
      if (disposed || !supported || !change
        || (change.status != null && change.status !== 'ready' && change.status !== 'failed')) return false
      if (refreshCurrent(change)) return true
      // Do not resurrect hidden hovers. Only remember a notification for an
      // already running native computation; hide/cancel clears its options.
      const model = editor.getModel?.(), wrapper = controller._contentWidget
      const options = wrapper?._hoverOperation?.options, anchor = options?.anchor
      if (!wrapper?._currentResult) snapshot = null
      if (!snapshot && !wrapper?._currentResult && model && !model.isDisposed?.() && present(editor.getDomNode?.())
        && anchor?.type === 1 && anchor.range && !options.shouldFocus && !wrapper.isFocused
        && sameContext(contextForModel(model), change) && sameSymbol(model, anchor.range, change.position)) {
        deferred = { change: { ...change }, model, modelVersion: model.getVersionId?.(), options, expires: Date.now() + 5000 }
      }
      return false
    },
    dispose() {
      if (disposed) return
      disposed = true
      snapshot = null
      deferred = null
      for (const subscription of subscriptions) subscription?.dispose?.()
      subscriptions.length = 0
    },
  }
}
