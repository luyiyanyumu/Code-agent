import { computed, ref } from 'vue'
import { api } from '../api.js'

/** One owner for project access intents; UI owns presentation and removal confirmation. */
export function useProjectAccess(options = {}) {
  const client = options.client || api.code
  const storageKey = options.storageKey || 'code-agent:active-project'
  const onActivate = options.onActivate || (async () => {})
  const onClose = options.onClose || (() => {})
  const projects = ref([]), meta = ref(null), loading = ref(false), phase = ref('idle'), error = ref(''), activeId = ref('')
  const busy = computed(() => phase.value !== 'idle')
  const nativeAvailable = computed(() => meta.value?.native_picker?.available === true)
  let disposed = false, intent = 0, readVersion = 0, listRevision = 0, initializing = null, activating = null, storage = null, storageWarning = '', operationError = '', errorOwner = null, preference = null
  const message = (e, fallback) => e?.message || String(e || fallback)
  const current = ticket => !disposed && ticket === intent
  function showError(text, owner = 'action') { operationError = text; errorOwner = owner; error.value = [operationError, storageWarning].filter(Boolean).join(' ') }
  function storageError(action, e) {
    storageWarning = `最近工作区偏好${action}失败：${message(e, '本地存储不可用')}。当前会话仍可继续。`
    if (!disposed) error.value = [operationError, storageWarning].filter(Boolean).join(' ')
  }
  try { storage = Object.hasOwn(options, 'storage') ? options.storage : typeof localStorage === 'undefined' ? null : localStorage }
  catch (e) { preference = { exists: true, id: '' }; storageError('读取', e) }

  function clearError() { if (!disposed) { storageWarning = ''; operationError = ''; errorOwner = null; error.value = '' } }
  function begin(next, { clear = true } = {}) {
    const ticket = ++intent
    phase.value = next
    if (clear) showError('')
    return ticket
  }
  function finish(ticket) { if (current(ticket)) phase.value = 'idle' }
  function remembered() {
    if (preference) return preference
    try { const id = storage?.getItem(storageKey); preference = { exists: id != null, id: id == null ? '' : String(id) }; return preference }
    catch (e) { preference = { exists: true, id: '' }; storageError('读取', e); return preference }
  }
  function remember(id, ticket) {
    if (!current(ticket)) return
    // Existing empty value is an explicit close; absent key alone enables legacy first-project restore.
    preference = { exists: true, id }
    try { storage?.setItem(storageKey, id) }
    catch (e) { storageError('保存', e) }
  }
  function upsert(project, { promote = false } = {}) {
    if (!project?.id) throw new Error('服务未返回有效项目，请重新打开。')
    listRevision++
    const existing = projects.value.find(item => item.id === project.id)
    const updated = { ...existing, ...project }
    if (promote) projects.value = [updated, ...projects.value.filter(item => item.id !== project.id)]
    else projects.value = existing ? projects.value.map(item => item.id === project.id ? updated : item) : [...projects.value, updated]
  }
  async function enter(project, ticket, { verify = false } = {}) {
    if (!current(ticket)) return false
    if (verify) {
      const fresh = await client.project(project.id)
      if (!current(ticket)) return false
      if (!fresh?.id || fresh.id !== project.id) throw new Error('项目详情响应与所选工作区不匹配，请刷新后重试。')
      if (fresh.available === false) throw new Error('工作区目录当前不可用，请检查路径或从最近列表移除。')
      project = fresh
      upsert(project)
    }
    if (project.available === false) throw new Error('工作区目录当前不可用，请检查路径或从最近列表移除。')
    if (activeId.value === project.id && (!activating || activating.id === project.id)) {
      upsert(project, { promote: true })
      return true
    }
    // A callback may already have changed the rendered workspace before its async setup ends.
    // Returning to the committed ID then needs a new callback to invalidate the other workspace.
    const activation = { id: project.id, ticket }
    activating = activation
    try {
      const accepted = await onActivate(project)
      if (!current(ticket)) return false
      if (accepted === false) { showError('工作区未能打开，请重试。'); return false }
      upsert(project, { promote: true })
      activeId.value = project.id; remember(project.id, ticket)
      return true
    } finally { if (activating === activation) activating = null }
  }
  function read() {
    if (disposed) return { list: Promise.resolve(false), completion: Promise.resolve({ success: false, listSuccess: false }) }
    const version = ++readVersion, action = intent, revision = listRevision
    const validRead = () => !disposed && version === readVersion
    loading.value = true
    if (typeof errorOwner === 'number') showError('', version)
    const failures = ['', '']
    function failure(index, text) {
      if (!validRead()) return
      failures[index] = text
      if (current(action) && (errorOwner !== 'action' || !operationError)) showError(failures.filter(Boolean).join(' '), version)
    }
    const list = Promise.resolve().then(() => client.listProjects()).then(data => {
      if (!validRead()) return false
      const rows = Array.isArray(data) ? data : data?.projects
      if (Array.isArray(rows)) {
        // An older refresh must not undo registration/removal completed while it was pending.
        if (revision === listRevision) projects.value = rows.filter(item => item?.id)
        return true
      }
      failure(0, '项目列表响应无效，请刷新重试。'); return false
    }).catch(e => { failure(0, `项目列表读取失败：${message(e, '无法连接服务')}`); return false })
    const info = Promise.resolve().then(() => client.meta()).then(data => {
      if (!validRead()) return false
      if (data && typeof data === 'object') { meta.value = data; return true }
      failure(1, '项目环境读取失败：服务未返回有效环境信息'); return false
    }).catch(e => { failure(1, `项目环境读取失败：${message(e, '无法连接服务')}`); return false })
    const completion = Promise.all([list, info]).then(([listSuccess, metaSuccess]) => {
      if (!validRead()) return { success: false, listSuccess: false }
      loading.value = false
      return { success: listSuccess || metaSuccess, listSuccess }
    })
    return { list, completion }
  }
  async function refresh() { return (await read().completion).success }
  function init() {
    if (disposed) return Promise.resolve(false)
    if (initializing) return initializing
    const initialIntent = intent
    const task = (async () => {
      const reading = read(), listSuccess = await reading.list
      if (!current(initialIntent)) return false
      if (activeId.value || !listSuccess) {
        const result = await reading.completion
        return current(initialIntent) && result.success
      }
      const saved = remembered()
      const available = item => item.available !== false
      if (saved.exists && !saved.id) return true
      const project = saved.exists ? projects.value.find(item => item.id === saved.id && available(item)) : projects.value.find(available)
      if (saved.exists && !project) { showError([operationError, '上次工作区当前不可用，请选择其他工作区或重新打开目录。'].filter(Boolean).join(' ')); return false }
      if (!project) return true
      // Automatic restoration belongs to the existing init intent; user actions still invalidate it.
      const ticket = initialIntent
      phase.value = 'switching'
      try { return await enter(project, ticket, { verify: true }) }
      catch (e) { if (current(ticket)) showError(message(e, '工作区打开失败')); return false }
      finally { finish(ticket) }
    })()
    initializing = task
    task.finally(() => { if (initializing === task) initializing = null })
    return task
  }
  async function activate(projectOrId) {
    if (disposed) return false
    // Explicit selection supersedes a picker/register in flight, even when selecting the same tab.
    const ticket = begin('switching')
    const id = typeof projectOrId === 'string' ? projectOrId : projectOrId?.id
    const project = typeof projectOrId === 'string' ? projects.value.find(item => item.id === projectOrId) : projectOrId
    try {
      if (project?.available === false) throw new Error('工作区目录当前不可用，请检查路径或从最近列表移除。')
      if (id && id === activeId.value && (!activating || activating.id === id)) return true
      if (!project?.id) throw new Error('请选择一个最近工作区。')
      return await enter(project, ticket, { verify: true })
    } catch (e) { if (current(ticket)) showError(message(e, '工作区切换失败')); return false }
    finally { finish(ticket) }
  }
  async function register(nextPhase, invoke) {
    if (disposed || busy.value) return false
    const ticket = begin(nextPhase)
    try {
      const project = await invoke()
      if (!current(ticket)) return false
      upsert(project)
      return await enter(project, ticket)
    } catch (e) { if (current(ticket)) showError(message(e, '工作区打开失败')); return false }
    finally { finish(ticket) }
  }
  function openPath(value) {
    return register('opening', () => {
      const path = String(value || '').trim()
      if (!path) throw new Error('请输入工作区的完整路径。')
      return client.openProject(path)
    })
  }
  function createProject({ name = '', template = 'empty', gitInit = true, location = '' } = {}) {
    return register('creating', () => {
      const trimmed = String(name || '').trim()
      if (!trimmed) throw new Error('请输入新项目名称。')
      const parent = String(location || '').trim()
      return parent ? client.createProject(trimmed, template, gitInit, parent) : client.createProject(trimmed, template, gitInit)
    })
  }
  async function selectFolder(start, ticket) {
    if (!nativeAvailable.value) throw new Error(meta.value?.native_picker?.reason || '系统文件夹选择器不可用，请手动输入工作区路径。')
    const picked = await client.pickFolder(String(start || '').trim())
    if (!current(ticket) || picked?.cancelled) return null
    return typeof picked?.path === 'string' ? picked.path.trim() || null : null
  }
  async function pickLocation(start = '') {
    if (disposed || busy.value) return null
    const ticket = begin('selecting')
    try { const path = await selectFolder(start, ticket); return current(ticket) ? path : null }
    catch (e) { if (current(ticket)) showError(message(e, '项目位置选择失败')); return null }
    finally { finish(ticket) }
  }
  async function openFolder(start = '') {
    if (disposed || busy.value) return false
    const ticket = begin('selecting')
    try {
      const path = await selectFolder(start, ticket)
      if (!path || !current(ticket)) return false
      phase.value = 'opening'
      const project = await client.openProject(path)
      if (!current(ticket)) return false
      upsert(project)
      return await enter(project, ticket)
    } catch (e) { if (current(ticket)) showError(message(e, '文件夹选择失败')); return false }
    finally { finish(ticket) }
  }
  function closeCurrent(ticket) {
    const result = onClose()
    if (!current(ticket) || result === false) return false
    activeId.value = ''; remember('', ticket)
    return true
  }
  function close() {
    if (disposed) return false
    const ticket = begin('idle')
    try { return closeCurrent(ticket) }
    catch (e) { if (current(ticket)) showError(message(e, '工作区关闭失败')); return false }
  }
  async function removeRecent(projectOrId) {
    if (disposed || busy.value) return false
    const ticket = begin('removing'), id = typeof projectOrId === 'string' ? projectOrId : projectOrId?.id
    try {
      if (!id) throw new Error('请选择要从最近列表移除的工作区。')
      await client.removeProject(id, false)
      if (!current(ticket)) return false
      listRevision++; projects.value = projects.value.filter(item => item.id !== id)
      if (activeId.value === id) return closeCurrent(ticket)
      return true
    } catch (e) { if (current(ticket)) showError(message(e, '最近工作区移除失败')); return false }
    finally { finish(ticket) }
  }
  function deferRestore() {
    if (disposed) return false
    intent++; phase.value = 'idle'
    return true
  }
  function dispose() { disposed = true; intent++; readVersion++ }
  return { projects, meta, loading, phase, error, activeId, busy, nativeAvailable, init, refresh, openFolder, openPath, createProject, pickLocation, activate, removeRecent, close, clearError, deferRestore, dispose }
}
