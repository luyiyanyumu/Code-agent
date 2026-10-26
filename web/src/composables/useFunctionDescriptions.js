import { computed, ref } from 'vue'
import { functionDescriptionPresentation, publishFunctionDescriptions } from '../lib/functionDescriptions.js'

/** Uses the workspace's existing polling clock. Optional description reads cannot hold up the editor. */
export function useFunctionDescriptions({ client, setTimer = setTimeout, clearTimer = clearTimeout, timeoutMs = 8000 }) {
  const state = ref(null), loading = ref(false), error = ref('')
  const presentation = computed(() => functionDescriptionPresentation(state.value, { loading: loading.value, error: error.value }))
  let scope = '', generation = 0, controller = null, pending = null
  function reset() {
    generation++
    controller?.abort()
    controller = pending = null
    scope = ''
    state.value = null
    loading.value = false
    error.value = ''
  }
  function refresh(projectId, tree = 'main') {
    if (!projectId) { reset(); return Promise.resolve() }
    const key = `${projectId}\0${tree}`
    if (scope !== key) { reset(); scope = key }
    if (pending) return pending
    const ticket = generation
    const abort = new AbortController()
    controller = abort
    loading.value = true
    let timer, cancel
    const cancelled = new Promise((_, reject) => {
      cancel = () => reject(abort.signal.reason || Object.assign(new Error('请求已取消'), { name: 'AbortError' }))
      abort.signal.addEventListener('abort', cancel, { once: true })
      timer = setTimer(() => abort.abort(Object.assign(new Error('功能描述状态读取超时'), { name: 'TimeoutError' })), timeoutMs)
    })
    pending = Promise.race([Promise.resolve().then(() => client.indexStatus(projectId, tree, { signal: abort.signal })), cancelled])
      .then(result => {
        if (ticket !== generation || scope !== key) return
        state.value = result?.briefs || null
        error.value = result?.briefs ? '' : '服务尚未返回功能描述进度，请重启后端后刷新。'
        if (result?.briefs) publishFunctionDescriptions(projectId, tree, result.briefs, { origin: 'code-view' })
      }).catch(cause => {
        if (ticket === generation && scope === key) error.value = cause.message || '功能描述状态读取失败'
      }).finally(() => {
        clearTimer(timer)
        abort.signal.removeEventListener('abort', cancel)
        if (ticket === generation && scope === key) { controller = pending = null; loading.value = false }
      })
    return pending
  }
  return { state, loading, error, presentation, refresh, reset }
}
