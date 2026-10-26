import { computed, onScopeDispose, ref, watch } from 'vue'

// Dialogue navigation is independent of the editor's canonical Lead identity.
// Closing a subscription or selecting a tab never stops a server-side task.
export function useProjectConversations({ projectId, tree, leadSessionId, userId, visible = () => true, client, storage, onDefaultChanged, pollMs = 15000 }) {
  const items = ref([]), selectedId = ref(''), defaultId = ref(''), loading = ref(false), creating = ref(false), deleting = ref(''), error = ref('')
  let generation = 0, request = 0, mutation = 0, selection = 0, timer, disposed = false, preferred = ''
  const removedIds = new Set()
  const store = storage || (() => { try { return globalThis.localStorage } catch { return null } })()
  const key = () => `code-agent-conversation-v1:${encodeURIComponent(userId?.() || 'local')}:${encodeURIComponent(projectId() || '')}`
  const current = (pid, version) => !disposed && projectId() === pid && generation === version
  const canonicalId = computed(() => defaultId.value || (!removedIds.has(leadSessionId()) ? leadSessionId() : '') || '')
  const conversations = computed(() => {
    const lead = items.value.find(item => item.id === canonicalId.value)
    return [...(canonicalId.value ? [{ kind: 'main', title: 'Lead', ...lead, id: canonicalId.value, is_default: true }] : []), ...items.value.filter(item => item.kind === 'chat' && item.id !== canonicalId.value && !removedIds.has(item.id))]
  })
  const selected = computed(() => conversations.value.find(item => item.id === selectedId.value) || conversations.value[0] || null)
  const isChat = computed(() => tree() === 'main' && selected.value?.kind === 'chat')
  const activeRootId = computed(() => isChat.value ? selected.value.id : canonicalId.value)
  function persist() {
    preferred = selected.value?.id || canonicalId.value
    try { if (projectId()) store?.setItem(key(), preferred) } catch { /* In-memory navigation still works when storage is unavailable. */ }
  }
  function stopTimer() { clearTimeout(timer); timer = null }
  function schedule() {
    stopTimer()
    if (!disposed && projectId() && leadSessionId() && tree() === 'main' && visible()) timer = setTimeout(() => { timer = null; void refresh() }, pollMs)
  }
  async function refresh() {
    if (disposed || !projectId() || !leadSessionId() || tree() !== 'main') return false
    const pid = projectId(), version = generation, mine = ++request, revision = mutation
    loading.value = true
    try {
      const result = await client.conversations(pid)
      if (!current(pid, version) || mine !== request || revision !== mutation) return false
      if (!Array.isArray(result?.conversations)) throw new Error('会话列表格式不正确')
      const previousDefault = canonicalId.value
      const nextDefault = result.default_session_id || leadSessionId()
      if (removedIds.has(nextDefault)) return false
      defaultId.value = nextDefault
      const seen = new Set()
      items.value = result.conversations.filter(item => item?.id && !removedIds.has(item.id) && !seen.has(item.id) && (seen.add(item.id), true))
      const wanted = selectedId.value || preferred
      selectedId.value = conversations.value.some(item => item.id === wanted) ? wanted : canonicalId.value
      error.value = ''
      persist()
      if (nextDefault !== previousDefault) {
        const nextMeta = items.value.find(item => item.id === nextDefault)
        if (nextMeta) onDefaultChanged?.({ projectId: pid, deletedSessionId: previousDefault, defaultSessionId: nextDefault, defaultConversation: nextMeta })
      }
      return true
    } catch (cause) {
      if (current(pid, version) && mine === request) error.value = cause.message || '读取对话失败'
      return false
    } finally {
      if (current(pid, version) && mine === request) { loading.value = false; schedule() }
    }
  }
  function select(id) {
    if (tree() !== 'main' || !conversations.value.some(item => item.id === id)) return false
    selectedId.value = id
    selection++
    persist()
    return true
  }
  function returnLead() {
    selectedId.value = canonicalId.value
    selection++
    if (tree() === 'main') persist()
  }
  async function create() {
    if (disposed || !projectId() || !leadSessionId() || tree() !== 'main' || creating.value || deleting.value) return null
    const pid = projectId(), version = generation, navigation = selection
    const source = activeRootId.value || leadSessionId()
    creating.value = true
    error.value = ''
    try {
      const result = await client.createConversation(pid, { from_session_id: source })
      if (!current(pid, version)) return null
      if (!result?.id || result.kind !== 'chat') throw new Error('新对话未返回有效的会话标识')
      mutation++
      items.value = [...items.value.filter(item => item.id !== result.id), result]
      // An explicit tab change while POST was pending takes precedence.
      if (navigation === selection && tree() === 'main') { selectedId.value = result.id; persist() }
      return result
    } catch (cause) {
      if (current(pid, version)) error.value = cause.message || '新建对话失败'
      return null
    } finally {
      if (current(pid, version)) { creating.value = false; schedule() }
    }
  }
  async function remove(id) {
    const item = conversations.value.find(item => item.id === id)
    if (disposed || !projectId() || tree() !== 'main' || creating.value || deleting.value || !item) return false
    const pid = projectId(), version = generation
    deleting.value = id
    error.value = ''
    try {
      const result = await client.deleteConversation(pid, id)
      if (!current(pid, version)) return false
      if (!result?.deleted || result.session_id !== id) throw new Error('未能确认对话已删除，请刷新列表核对')
      const deletingDefault = id === canonicalId.value
      const nextDefault = result.default_session_id || (deletingDefault ? '' : canonicalId.value)
      const nextMeta = result.default_conversation || items.value.find(item => item.id === nextDefault)
      if (!nextDefault || nextDefault === id || (deletingDefault && (!nextMeta || nextMeta.id !== nextDefault))) throw new Error('服务端未返回有效的默认对话，请刷新列表核对')
      mutation++
      removedIds.add(id)
      defaultId.value = nextDefault
      items.value = items.value.filter(item => item.id !== id)
      if (nextMeta) items.value = [...items.value.filter(item => item.id !== nextMeta.id), nextMeta]
      if (selectedId.value === id) {
        selectedId.value = conversations.value.some(item => item.id === result.fallback_session_id) ? result.fallback_session_id : canonicalId.value
        selection++
        persist()
      }
      if (deletingDefault) onDefaultChanged?.({ projectId: pid, deletedSessionId: id, defaultSessionId: nextDefault, defaultConversation: nextMeta })
      return true
    } catch (cause) {
      if (current(pid, version)) error.value = cause.message || '删除对话失败'
      return false
    } finally { if (current(pid, version)) { deleting.value = ''; schedule() } }
  }
  function update(id, patch) { items.value = items.value.map(item => item.id === id ? { ...item, ...patch } : item) }
  watch([projectId, () => userId?.() || 'local'], () => {
    generation++; request++; mutation++; selection++; stopTimer()
    removedIds.clear()
    items.value = []; selectedId.value = ''; defaultId.value = ''; loading.value = false; creating.value = false; deleting.value = ''; error.value = ''
    try { preferred = projectId() ? store?.getItem(key()) || '' : '' } catch { preferred = '' }
    if (visible()) void refresh()
  }, { immediate: true, flush: 'sync' })
  watch([leadSessionId, tree, visible], () => {
    stopTimer()
    if (!leadSessionId()) {
      request++; defaultId.value = ''; items.value = []; selectedId.value = ''; loading.value = false
      return
    }
    if (visible() && tree() === 'main') void refresh()
  }, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; generation++; request++; stopTimer() })
  return { conversations, selected, selectedId, canonicalId, activeRootId, isChat, loading, creating, deleting, error, refresh, select, returnLead, create, remove, update }
}
