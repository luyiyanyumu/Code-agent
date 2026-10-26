/** Prepare language-server edits against exact draft snapshots, without disk writes. */
export function relativeWorkspaceUri(rootPath, uri) {
  let target
  try { target = new URL(uri) } catch { throw new Error('语言服务返回了无效文件地址。') }
  if (target.protocol !== 'file:' || target.search || target.hash || target.username || target.password) throw new Error('只支持工作区内的文件修改。')
  let pathname
  try { pathname = decodeURIComponent(target.pathname) } catch { throw new Error('文件地址编码无效。') }
  if (target.hostname && target.hostname.toLowerCase() !== 'localhost') pathname = `//${target.hostname}${pathname}`
  else if (/^\/[a-z]:\//i.test(pathname)) pathname = pathname.slice(1)
  const root = String(rootPath || '').replace(/\\/g, '/').replace(/\/+$/, '')
  const path = pathname.replace(/\\/g, '/')
  const windows = /^[a-z]:\//i.test(root) || root.startsWith('//')
  const comparable = value => windows ? value.toLowerCase() : value
  if (!root || !comparable(path).startsWith(`${comparable(root)}/`)) throw new Error('修改目标超出当前工作区。')
  const relative = path.slice(root.length + 1)
  if (!relative || relative.split('/').some(part => !part || part === '.' || part === '..') || /[\u0000-\u001f]/.test(relative)) throw new Error('修改目标路径无效。')
  return relative
}

function offsetAt(text, position) {
  const line = position?.line, character = position?.character
  if (!Number.isInteger(line) || !Number.isInteger(character) || line < 0 || character < 0) throw new Error('修改范围无效，请重新请求代码操作。')
  let start = 0
  for (let index = 0; index < line; index++) {
    const next = text.indexOf('\n', start)
    if (next < 0) throw new Error('修改范围超出文件内容，请重新请求代码操作。')
    start = next + 1
  }
  const newline = text.indexOf('\n', start)
  let end = newline < 0 ? text.length : newline
  if (end > start && text[end - 1] === '\r') end--
  if (character > end - start) throw new Error('修改范围超出当前行，请重新请求代码操作。')
  return start + character
}

export function applyTextEdits(text, edits) {
  if (typeof text !== 'string' || !Array.isArray(edits)) throw new Error('语言服务返回了无效修改。')
  const ranges = edits.map((edit, index) => {
    if (typeof edit?.newText !== 'string') throw new Error('语言服务返回了无效修改内容。')
    const start = offsetAt(text, edit.range?.start), end = offsetAt(text, edit.range?.end)
    if (end < start) throw new Error('修改范围起止顺序无效。')
    return { start, end, text: edit.newText, index }
  }).sort((a, b) => a.start - b.start || a.end - b.end || a.index - b.index)
  let cursor = 0, result = ''
  for (const edit of ranges) {
    if (edit.start < cursor) throw new Error('修改范围相互重叠，无法安全应用。')
    result += text.slice(cursor, edit.start) + edit.text
    cursor = edit.end
  }
  return result + text.slice(cursor)
}

export async function prepareWorkspaceEdit({ edit, rootPath, documents = [], readFile, versions = {}, snapshots = {}, requireVersions = false, maxFiles = 50, maxChars = 2_000_000 }) {
  if (!edit || typeof edit !== 'object' || Array.isArray(edit)) throw new Error('语言服务未返回有效修改。')
  if (Array.isArray(edit.documentChanges) && edit.documentChanges.some(change => change?.kind)) return prepareResourceEdit({ edit, rootPath, documents, readFile, versions, snapshots, requireVersions, maxFiles, maxChars })
  const groups = new Map()
  const windows = /^[a-z]:[\\/]/i.test(rootPath) || String(rootPath).startsWith('\\\\') || String(rootPath).startsWith('//')
  const pathKey = path => windows ? path.toLowerCase() : path
  const add = (uri, edits, version = null) => {
    const path = relativeWorkspaceUri(rootPath, uri)
    if (!Array.isArray(edits)) throw new Error('语言服务返回了无效修改列表。')
    if (requireVersions && version == null) throw new Error('服务器主动修改缺少文档版本，请重新执行快速修复或重命名。')
    const key = pathKey(path)
    let group = groups.get(key)
    if (!group) { group = { uri, path, edits: [], version }; groups.set(key, group) }
    else if (version != null && group.version != null && version !== group.version) throw new Error('同一文件的修改版本不一致。')
    if (version != null) {
      if (!Number.isInteger(version)) throw new Error('修改版本无效。')
      group.version = version
    }
    group.edits.push(...edits)
  }
  if (edit.changes != null) {
    if (typeof edit.changes !== 'object' || Array.isArray(edit.changes)) throw new Error('语言服务返回了无效修改列表。')
    for (const [uri, edits] of Object.entries(edit.changes)) add(uri, edits)
  }
  if (edit.documentChanges != null) {
    if (!Array.isArray(edit.documentChanges)) throw new Error('语言服务返回了无效修改列表。')
    for (const change of edit.documentChanges) {
      if (change?.kind || !change?.textDocument) throw new Error('此操作包含创建、移动或删除文件，目前请通过文件操作完成。')
      add(change.textDocument.uri, change.edits, change.textDocument.version)
    }
  }
  if (!groups.size) throw new Error('此操作没有需要应用的代码修改。')
  if (groups.size > maxFiles) throw new Error(`一次修改超过 ${maxFiles} 个文件，请缩小操作范围。`)
  let size = 0
  const rows = []
  for (const group of groups.values()) {
    const document = documents.find(doc => doc.file?.uri === group.uri || pathKey(doc.path) === pathKey(group.path))
    const originalUri = document?.file?.uri || group.uri
    if (group.version != null && (versions[group.uri] ?? versions[originalUri]) !== group.version) throw new Error('代码已变化，请重新请求代码操作。')
    if (document?.needsReload || document?.file?.missing || document?.file?.editable === false || document?.file?.secret) throw new Error(`${group.path} 当前不可编辑，请先刷新文件。`)
    const file = document?.file || await readFile(group.path)
    if (!file || file.secret || file.editable === false || file.truncated || file.partial || (file.start_line != null && file.start_line !== 1) || (file.end_line != null && file.total_lines != null && file.end_line < file.total_lines)) throw new Error(`${group.path} 未完整载入或不可编辑。`)
    if (file.path && pathKey(String(file.path).replace(/\\/g, '/')) !== pathKey(group.path)) throw new Error('读取的文件与修改目标不一致。')
    const before = document ? document.text : file.text
    if (typeof before !== 'string') throw new Error(`${group.path} 未完整载入。`)
    const snapshotUri = Object.hasOwn(snapshots, group.uri) ? group.uri : originalUri
    if (Object.hasOwn(snapshots, snapshotUri) && before !== snapshots[snapshotUri]) throw new Error('代码在操作期间已变化，请重新请求代码操作。')
    const after = applyTextEdits(before, group.edits)
    size += before.length + after.length
    if (size > maxChars) throw new Error('修改内容过大，请缩小操作范围。')
    if (before !== after) rows.push({ path: group.path, uri: group.uri, key: document?.key || '', caseInsensitive: windows, file: { ...file, path: group.path, text: before }, before, after })
  }
  if (!rows.length) throw new Error('代码内容无变化。')
  return rows
}

/** Validate all targets before changing any draft; later user edits cannot be overwritten. */
export function findPlanDocument(row, documents) {
  const comparable = value => row.caseInsensitive ? value.toLowerCase() : value
  return documents.find(doc => row.key ? doc.key === row.key : comparable(doc.path) === comparable(row.path))
}

export function assertPlanCurrent(rows, documents, direction = 'apply') {
  if (rows.transactionFiles) {
    for (const row of rows.transactionFiles) {
      const comparable = value => rows.caseInsensitive ? value.toLowerCase() : value
      const document = documents.find(doc => comparable(doc.path) === comparable(row.path))
      if (document && (document.dirty || document.needsReload || document.file?.missing || document.file?.editable === false || document.file?.secret || document.text !== (direction === 'undo' ? row.after_text : row.before_text))) throw new Error(`${row.path} 有未保存或更新的草稿，已保留当前内容；请先保存再重构。`)
    }
    return
  }
  for (const row of rows) {
    const document = findPlanDocument(row, documents)
    if (!document && (direction === 'undo' || row.key)) throw new Error(`${row.path} 已关闭，请重新请求代码操作。`)
    if (document && (document.needsReload || document.file?.missing || document.file?.editable === false || document.file?.secret || document.text !== (direction === 'undo' ? row.after : row.before))) throw new Error(`${row.path} 又有变化，已保留当前草稿；请重新操作。`)
  }
}

/** Resource refactors are a reviewed disk transaction, never a sequence of draft saves. */
async function prepareResourceEdit({ edit, rootPath, documents, readFile, versions, snapshots, requireVersions, maxFiles, maxChars }) {
  if (requireVersions) throw new Error('服务器主动文件操作缺少可靠的版本，请显式发起重构并审查。')
  if (edit.changes && Object.keys(edit.changes).length) throw new Error('文件操作需要有序 documentChanges，不能混合无序 changes。')
  const windows = /^[a-z]:[\\/]/i.test(rootPath) || String(rootPath).startsWith('\\\\') || String(rootPath).startsWith('//')
  const keyOf = path => windows ? path.toLowerCase() : path
  const states = new Map(), rows = []
  const load = async uri => {
    const path = relativeWorkspaceUri(rootPath, uri), key = keyOf(path)
    if (states.has(key)) return states.get(key)
    if (states.size >= maxFiles) throw new Error(`一次修改超过 ${maxFiles} 个文件，请缩小操作范围。`)
    const document = documents.find(doc => keyOf(doc.path) === key)
    if (document && (document.dirty || document.needsReload || document.file?.missing || document.file?.editable === false || document.file?.secret)) throw new Error(`${path} 有未保存或更新的草稿，请先保存再重构。`)
    let file
    try { file = document?.file || await readFile(path) } catch (error) { if (error?.status !== 404) throw error }
    if (file && (file.secret || file.editable === false || file.truncated || file.partial || file.start_line > 1 || (file.end_line != null && file.total_lines != null && file.end_line < file.total_lines))) throw new Error(`${path} 未完整载入或不可编辑。`)
    if (file?.path && keyOf(String(file.path).replace(/\\/g, '/')) !== key) throw new Error('读取的文件与修改目标不一致。')
    const before = document ? document.text : file?.text
    if (file && typeof before !== 'string') throw new Error(`${path} 未完整载入。`)
    if (document?.savedText != null && document.text !== document.savedText) throw new Error(`${path} 有未保存草稿，请先保存再重构。`)
    const originalUri = document?.file?.uri || uri
    const snapshotUri = Object.hasOwn(snapshots, uri) ? uri : originalUri
    if (Object.hasOwn(snapshots, snapshotUri) && snapshots[snapshotUri] !== before) throw new Error('代码在操作期间已变化，请重新请求代码操作。')
    const state = { path, uri, key: document?.key || '', file, beforeExists: !!file, before: before || '', exists: !!file, text: before || '' }
    states.set(key, state)
    return state
  }
  for (const change of edit.documentChanges) {
    if (change?.kind === 'create') {
      if (change.options?.overwrite) throw new Error('新建文件不允许覆盖现有文件。')
      const state = await load(change.uri)
      if (state.exists) { if (change.options?.ignoreIfExists) continue; throw new Error(`${state.path} 已存在，不能覆盖。`) }
      state.exists = true; state.text = ''
      rows.push({ kind: 'create', path: state.path, uri: change.uri, before: '', after: '', caseInsensitive: windows })
    } else if (change?.kind === 'rename') {
      if (change.options?.overwrite) throw new Error('移动文件不允许覆盖现有文件。')
      const source = await load(change.oldUri), target = await load(change.newUri)
      if (keyOf(source.path) === keyOf(target.path)) throw new Error('请使用不同的新文件路径；仅改变大小写的移动暂不支持。')
      if (!source.exists) throw new Error(`${source.path} 不存在，不能移动。`)
      if (target.exists) { if (change.options?.ignoreIfExists) continue; throw new Error(`${target.path} 已存在，不能覆盖。`) }
      const text = source.text
      source.exists = false; source.text = ''; target.exists = true; target.text = text; target.modeFrom = source.modeFrom || (source.beforeExists ? source.path : undefined)
      rows.push({ kind: 'rename', path: target.path, oldPath: source.path, uri: change.newUri, before: text, after: text, caseInsensitive: windows })
    } else if (change?.kind === 'delete') {
      if (change.options?.recursive) throw new Error('重构不支持递归删除目录。')
      const state = await load(change.uri)
      if (!state.exists) { if (change.options?.ignoreIfNotExists) continue; throw new Error(`${state.path} 不存在，不能删除。`) }
      const text = state.text; state.exists = false; state.text = ''
      rows.push({ kind: 'delete', path: state.path, uri: change.uri, before: text, after: '', caseInsensitive: windows })
    } else if (change?.textDocument) {
      const state = await load(change.textDocument.uri)
      if (!state.exists) throw new Error(`${state.path} 不存在，请先创建文件。`)
      const version = change.textDocument.version
      const originalUri = state.file?.uri || change.textDocument.uri
      if (version != null && (!Number.isInteger(version) || (versions[change.textDocument.uri] ?? versions[originalUri]) !== version)) throw new Error('代码已变化，请重新请求代码操作。')
      const before = state.text, after = applyTextEdits(before, change.edits)
      state.text = after
      if (before !== after) rows.push({ kind: 'edit', path: state.path, uri: change.textDocument.uri, before, after, caseInsensitive: windows })
    } else throw new Error('语言服务返回了未知文件操作。')
  }
  let size = 0
  const transactionFiles = [...states.values()].filter(state => state.beforeExists !== state.exists || state.before !== state.text).map(state => {
    size += state.before.length + state.text.length
    if (size > maxChars) throw new Error('修改内容过大，请缩小操作范围。')
    return { path: state.path, before_exists: state.beforeExists, before_text: state.before, after_exists: state.exists, after_text: state.text, ...(state.modeFrom ? { mode_from: state.modeFrom } : {}) }
  })
  if (!transactionFiles.length) throw new Error('代码内容与文件位置无变化。')
  rows.transactionFiles = transactionFiles
  rows.resourceOperations = true
  rows.caseInsensitive = windows
  return rows
}
