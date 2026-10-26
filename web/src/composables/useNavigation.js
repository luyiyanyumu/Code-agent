import { computed, reactive, ref } from 'vue'

// 历史只记录显式打开/跳转；离开前更新当前位置，返回时恢复真实光标。
export function useNavigation(limit = 100) {
  const scope = ref('')
  const scopes = reactive({})
  const state = computed(() => scopes[scope.value] || { entries: [], cursor: -1, revision: 0 })
  const normalize = point => point?.path ? { path: point.path, line: Math.max(1, Number(point.line) || 1), column: Math.max(1, Number(point.column) || 1) } : null
  const equal = (a, b) => a?.path === b?.path && a?.line === b?.line && a?.column === b?.column
  function activate(id) {
    scope.value = id || ''
    if (id && !scopes[id]) scopes[id] = { entries: [], cursor: -1, revision: 0 }
  }
  function update(point) {
    const p = normalize(point), s = state.value
    if (p && s.cursor >= 0 && s.entries[s.cursor]?.path === p.path) s.entries[s.cursor] = p
  }
  function visit(point, current) {
    if (!scope.value) return
    const p = normalize(point), s = state.value
    if (!p) return
    update(current)
    if (!s.entries.length && normalize(current) && !equal(p, normalize(current))) { s.entries.push(normalize(current)); s.cursor = 0 }
    if (equal(s.entries[s.cursor], p)) return
    s.entries.splice(s.cursor + 1)
    s.entries.push(p)
    if (s.entries.length > limit) s.entries.splice(0, s.entries.length - limit)
    s.cursor = s.entries.length - 1
    s.revision += 1
  }
  function prepare(direction, current) {
    update(current)
    const s = state.value, index = s.cursor + direction
    if (index < 0 || index >= s.entries.length) return null
    return { point: { ...s.entries[index] }, index, revision: s.revision, scope: scope.value }
  }
  function commit(ticket) {
    const s = state.value
    if (ticket.scope !== scope.value || ticket.revision !== s.revision) return false
    s.cursor = ticket.index
    s.revision += 1
    return true
  }
  return { activate, visit, update, prepare, commit, state, canBack: computed(() => state.value.cursor > 0), canForward: computed(() => state.value.cursor + 1 < state.value.entries.length) }
}
