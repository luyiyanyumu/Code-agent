import { ref } from 'vue'

/** Index progress never participates in the editor's disk-save acknowledgement. */
export function useIndexRefresh({ client, onDone = () => {}, setTimer = setTimeout, clearTimer = clearTimeout }) {
  const state = ref(null), statusError = ref(''), retrying = ref(false)
  let projectId = '', revision = 0, timer = null, controller = null

  function cancel() {
    revision++
    if (timer !== null) clearTimer(timer)
    timer = null
    controller?.abort()
    controller = null
  }
  function reset() { cancel(); projectId = ''; state.value = null; statusError.value = ''; retrying.value = false }
  function schedule() { if (state.value?.pending) timer = setTimer(() => { timer = null; void poll() }, 1000) }
  function apply(result) {
    const next = result?.index_refresh
    if (!next) { statusError.value = '暂时无法获取索引进度'; return false }
    if ((next.requested_generation || 0) < (state.value?.requested_generation || 0)) return false
    const wasPending = state.value?.pending
    state.value = next
    statusError.value = ''
    if (wasPending && next.state === 'done') onDone(result)
    return true
  }
  function accept(pid, result) {
    if (!result?.index_refresh) return
    if (projectId !== pid) reset()
    projectId = pid
    cancel()
    retrying.value = false
    apply(result)
    schedule()
  }
  async function request(scan = false) {
    cancel()
    const ticket = revision, pid = projectId
    if (!pid) return
    const abort = new AbortController()
    controller = abort
    const timeout = setTimer(() => abort.abort(), scan ? 30000 : 10000)
    try {
      const result = scan
        ? await client.scan(pid, { tree: 'main', signal: abort.signal })
        : await client.indexStatus(pid, 'main', { signal: abort.signal })
      if (ticket !== revision) return
      apply(result)
      if (!statusError.value) schedule()
    } catch (error) {
      if (ticket === revision) statusError.value = error.name === 'AbortError' ? '索引进度读取超时' : '索引进度暂时不可用'
    } finally {
      clearTimer(timeout)
      if (ticket === revision) { controller = null; retrying.value = false }
    }
  }
  function poll() { return request() }
  function retry() {
    if (retrying.value || !projectId) return
    retrying.value = true
    statusError.value = ''
    return request(state.value?.state === 'error')
  }
  return { state, statusError, retrying, accept, poll, retry, reset }
}
