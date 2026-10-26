import { computed, reactive, ref, shallowReactive } from 'vue'

const normalizePath = (path) => String(path || '').replace(/\\/g, '/').replace(/^\.\//, '')
const scopeKey = (projectId, tree) => JSON.stringify([String(projectId || ''), String(tree || 'main')])

/** Project, worktree, and exact relative path together identify an editor document. */
export function documentKey(projectId, tree = 'main', path = '') {
  return JSON.stringify([String(projectId || ''), String(tree || 'main'), normalizePath(path)])
}

/**
 * In-memory document registry, with debounced recovery snapshots.
 * documents contains the current scope; other scopes remain in the registry.
 * Restored tabs have needsReload=true: reread their disk files before editing,
 * then merge with openFile/updateExternal so a recovered draft stays intact.
 */
export function useWorkbench(options = {}) {
  const storageKey = options.storageKey || 'code-agent-workbench-v1'
  const debounceMs = Math.max(0, Number(options.debounceMs ?? 400) || 0)
  const maxDraftChars = Math.max(1, Number(options.maxDraftChars ?? 2_000_000) || 2_000_000)
  const documents = ref([])
  const activeKey = ref('')
  const storageError = ref('')
  const registry = shallowReactive(new Map())
  const activeByScope = new Map()
  let projectId = ''
  let tree = 'main'
  let timer = null
  let disposed = false
  let storage = null
  let recoveryUnreadable = false

  const activeDocument = computed(() => documents.value.find((document) => document.key === activeKey.value) || null)
  const allDirty = computed(() => [...registry.values()].filter((document) => document.dirty))
  const hasUnsaved = computed(() => allDirty.value.length > 0)

  const reportStorage = (action, error) => {
    storageError.value = `草稿${action}失败：${error?.message || String(error || '存储不可用')}。当前编辑仍保留在会话中。`
  }
  try {
    storage = Object.hasOwn(options, 'storage') ? options.storage
      : typeof localStorage === 'undefined' ? null : localStorage
  } catch (error) { reportStorage('存储', error) }

  function refreshDocuments() {
    documents.value = [...registry.values()].filter((document) => document.projectId === projectId && document.tree === tree)
    const scope = scopeKey(projectId, tree)
    const remembered = activeByScope.get(scope)
    activeKey.value = documents.value.some((document) => document.key === remembered)
      ? remembered : documents.value[0]?.key || ''
    if (activeKey.value) activeByScope.set(scope, activeKey.value)
    else activeByScope.delete(scope)
  }

  function schedule() {
    if (disposed) return
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(() => { timer = null; flush() }, debounceMs)
  }

  function baseDocument(file, pid, treeId) {
    const path = normalizePath(file.path)
    const text = String(file.text ?? '')
    return reactive({
      key: documentKey(pid, treeId, path), projectId: pid, tree: treeId, path,
      file: { ...file, path }, text, savedText: text, sha1: file.sha1 || '',
      dirty: false, viewState: null, selection: null, conflict: null, needsReload: false,
    })
  }

  function adoptDisk(document, file) {
    const text = String(file.text ?? '')
    document.file = { ...file, path: document.path }
    document.sha1 = file.sha1 || ''
    document.savedText = text
    document.text = text
    document.dirty = false
    document.conflict = null
    document.needsReload = false
  }

  function activateProject(pid, treeId = 'main') {
    projectId = String(pid || '')
    tree = String(treeId || 'main')
    refreshDocuments()
    schedule()
    return documents.value
  }

  function activate(key) {
    const document = registry.get(key)
    if (!document) return false
    if (document.projectId !== projectId || document.tree !== tree) {
      projectId = document.projectId
      tree = document.tree
    }
    refreshDocuments()
    activeKey.value = document.key
    activeByScope.set(scopeKey(projectId, tree), document.key)
    schedule()
    return true
  }

  function openFile(file, opts = {}) {
    const pid = String(opts.projectId ?? projectId)
    const treeId = String(opts.tree || (pid === projectId ? tree : 'main'))
    const path = normalizePath(file?.path)
    if (!pid || !path) throw new Error('打开文件需要项目 ID 和文件路径')
    const key = documentKey(pid, treeId, path)
    let document = registry.get(key)
    if (document) updateExternal(key, file)
    else {
      document = baseDocument(file, pid, treeId)
      registry.set(key, document)
    }
    if (opts.activate !== false) activate(key)
    else if (pid === projectId && treeId === tree) refreshDocuments()
    schedule()
    return document
  }

  function update(key, patch = {}) {
    const document = registry.get(key)
    if (!document) return null
    if (Object.hasOwn(patch, 'text')) {
      document.text = String(patch.text ?? '')
      // The original hash survives restoration; savedText is unknown until disk is reread.
      document.dirty = document.savedText === null || document.text !== document.savedText
      if (!document.dirty) document.conflict = null
      schedule()
    }
    if (Object.hasOwn(patch, 'viewState')) document.viewState = patch.viewState
    if (Object.hasOwn(patch, 'selection')) document.selection = patch.selection
    return document
  }

  function markSaved(key, result = {}) {
    const document = registry.get(key)
    if (!document) return null
    // Call with the submitted text: a later edit must never become the saved baseline.
    const submitted = String(Object.hasOwn(result, 'text') ? result.text ?? '' : document.text)
    const sha1 = result.sha1 ?? document.sha1
    const { changed: _changed, version: _version, text: _text, ...metadata } = result
    document.savedText = submitted
    document.sha1 = sha1
    document.file = { ...document.file, ...metadata, path: document.path, text: submitted, sha1 }
    if (result.lines !== undefined) document.file.total_lines = result.lines
    document.dirty = document.text !== submitted
    document.conflict = null
    document.needsReload = false
    schedule()
    return document
  }

  function updateExternal(key, file) {
    const document = registry.get(key)
    if (!document || !file) return null
    const diskText = String(file.text ?? '')
    const sameHash = !!file.sha1 && !!document.sha1 && file.sha1 === document.sha1
    const sameBaseline = document.savedText !== null && diskText === document.savedText
    if (!document.dirty || diskText === document.text) adoptDisk(document, file)
    else if (sameHash || sameBaseline) {
      // A restored draft still sits over the same disk version; establish its text baseline.
      document.file = { ...file, path: document.path }
      document.savedText = diskText
      document.sha1 = file.sha1 || document.sha1
      document.dirty = document.text !== diskText
      document.conflict = null
      document.needsReload = false
    } else {
      document.conflict = {
        kind: 'external', expectedSha1: document.sha1,
        actualSha1: file.sha1 || '', file: { ...file, path: document.path },
      }
      document.needsReload = false
    }
    schedule()
    return document
  }

  function close(key, { discard = false } = {}) {
    const document = registry.get(key)
    if (!document) return true
    if (document.dirty && !discard) return false
    const scope = scopeKey(document.projectId, document.tree)
    const peers = [...registry.values()].filter((item) => scopeKey(item.projectId, item.tree) === scope)
    const index = peers.findIndex((item) => item.key === key)
    registry.delete(key)
    if (activeByScope.get(scope) === key) {
      const next = peers[index + 1] || peers[index - 1]
      if (next) activeByScope.set(scope, next.key)
      else activeByScope.delete(scope)
    }
    refreshDocuments()
    // Closing a saved/discarded tab should also remove its old recovery draft immediately.
    flush()
    return true
  }

  function flush() {
    if (timer !== null) { clearTimeout(timer); timer = null }
    if (!storage) {
      if (!storageError.value) reportStorage('存储', new Error('浏览器本地存储不可用'))
      return false
    }
    // Preserve an unreadable existing snapshot for manual recovery instead of replacing it.
    if (recoveryUnreadable) return false
    try {
      const scopes = new Map()
      for (const document of registry.values()) {
        if (document.dirty && document.text.length > maxDraftChars) {
          storageError.value = `草稿「${document.path}」超过 ${maxDraftChars.toLocaleString()} 字符，未写入恢复快照。当前编辑仍保留在会话中，请先保存文件。`
          return false
        }
        const scope = scopeKey(document.projectId, document.tree)
        if (!scopes.has(scope)) scopes.set(scope, {
          projectId: document.projectId, tree: document.tree,
          activeKey: activeByScope.get(scope) || '', documents: [],
        })
        const file = document.file
        const item = {
          path: document.path, sha1: document.sha1, dirty: document.dirty,
          file: { path: document.path, lang: file.lang, uri: file.uri, editable: file.editable, secret: file.secret },
          viewState: document.viewState, selection: document.selection,
        }
        if (document.dirty) item.text = document.text
        scopes.get(scope).documents.push(item)
      }
      if (!scopes.size) storage.removeItem(storageKey)
      else storage.setItem(storageKey, JSON.stringify({ version: 1, scopes: [...scopes.values()] }))
      storageError.value = ''
      return true
    } catch (error) {
      reportStorage('保存', error)
      return false
    }
  }

  function restore() {
    if (!storage) return
    try {
      const raw = storage.getItem(storageKey)
      if (!raw) return
      const saved = JSON.parse(raw)
      if (saved?.version !== 1 || !Array.isArray(saved.scopes)) throw new Error('恢复快照格式不受支持')
      const restoredDocuments = new Map()
      const restoredActive = new Map()
      for (const scope of saved.scopes) {
        if (!scope?.projectId || !Array.isArray(scope.documents)) throw new Error('恢复快照缺少项目或标签数据')
        const pid = String(scope.projectId), treeId = String(scope.tree || 'main')
        for (const item of scope.documents) {
          if (!item?.path || (item.dirty && typeof item.text !== 'string')) throw new Error('恢复快照缺少文件或草稿内容')
          const document = baseDocument({ ...item.file, path: item.path, sha1: item.sha1 || '' }, pid, treeId)
          document.text = item.dirty ? item.text : ''
          document.savedText = null
          document.dirty = !!item.dirty
          document.needsReload = true
          document.viewState = item.viewState && typeof item.viewState === 'object' ? item.viewState : null
          document.selection = item.selection && typeof item.selection === 'object' ? item.selection : null
          restoredDocuments.set(document.key, document)
        }
        if (scope.activeKey) restoredActive.set(scopeKey(pid, treeId), scope.activeKey)
      }
      for (const [key, document] of restoredDocuments) registry.set(key, document)
      for (const [scope, key] of restoredActive) activeByScope.set(scope, key)
    } catch (error) {
      recoveryUnreadable = true
      reportStorage('恢复', error)
    }
  }

  function dispose() {
    if (disposed) return
    flush()
    disposed = true
  }

  restore()
  return { documents, activeKey, activeDocument, allDirty, hasUnsaved, storageError,
    activateProject, openFile, activate, update, markSaved, updateExternal, close, flush, dispose }
}
