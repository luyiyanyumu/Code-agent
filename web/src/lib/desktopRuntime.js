const CHANNEL = 'code-agent-desktop'
const WINDOW_METHODS = new Set(['window-state', 'window-minimize', 'window-toggle-maximize', 'window-close', 'window-drag'])
const METHODS = new Set(['status', 'action', 'logs', 'exit', ...WINDOW_METHODS])

export function createDesktopBridge(host = globalThis.window) {
  const pending = new Map(), guards = new Set(), closing = new Set(), windowListeners = new Set()
  let counter = 0, listening = false
  let windowState = null
  const available = () => Boolean(host?.__CODE_AGENT_DESKTOP__?.integrated && host?.chrome?.webview?.postMessage)
  const integratedTitlebar = () => available() && host.__CODE_AGENT_DESKTOP__.integratedTitlebar === true
  const post = value => host.chrome.webview.postMessage({ channel: CHANNEL, ...value })
  function acceptWindowState(value) {
    if (!value || typeof value.maximized !== 'boolean' || typeof value.minimized !== 'boolean' || typeof value.active !== 'boolean') return
    windowState = { maximized: value.maximized, minimized: value.minimized, active: value.active, integratedTitlebar: value.integratedTitlebar === true }
    for (const listener of windowListeners) listener(windowState)
  }
  async function prepareLeave(reason = 'close') {
    try { for (const guard of [...guards]) if (await guard(reason) === false) return false; return true }
    catch { return false }
  }
  async function message(event) {
    let data = event?.data
    try { if (typeof data === 'string') data = JSON.parse(data) } catch { return }
    if (data?.channel !== CHANNEL) return
    if (data.event === 'window-state') { acceptWindowState(data.state); return }
    if (typeof data.id !== 'string') return
    if (data.event === 'before-close') {
      if (closing.has(data.id)) return
      closing.add(data.id)
      try {
        post({ id: data.id, method: 'close-pending', params: {} })
        post({ id: data.id, method: 'close-response', params: { allow: await prepareLeave('close') } })
      }
      finally { closing.delete(data.id) }
      return
    }
    const request = pending.get(data.id)
    if (!request) return
    pending.delete(data.id); clearTimeout(request.timer)
    if (data.ok === true) {
      if (WINDOW_METHODS.has(request.method)) acceptWindowState(data.result)
      request.resolve(data.result)
    }
    else request.reject(new Error(data.error || '应用操作未完成，请稍后重试。'))
  }
  function connect() {
    if (!available()) return false
    if (!listening) { host.chrome.webview.addEventListener('message', message); listening = true }
    host.__CODE_AGENT_DESKTOP__.closeReady = true
    return true
  }
  function request(method, params = {}, options = {}) {
    if (!METHODS.has(method)) return Promise.reject(new Error('不支持的应用操作。'))
    if (!connect()) return Promise.reject(new Error('此功能仅在 Code-agent 桌面应用中可用。'))
    const id = `web-${Date.now().toString(36)}-${++counter}`
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('应用操作超时。请检查运行状态和日志，避免重复提交。')) }, options.timeout ?? (method === 'action' || method === 'exit' ? 185000 : 20000))
      pending.set(id, { resolve, reject, timer, method })
      try { post({ id, method, params }) }
      catch (error) { clearTimeout(timer); pending.delete(id); reject(error) }
    })
  }
  function dispose() {
    if (listening) host.chrome.webview.removeEventListener('message', message)
    if (host?.__CODE_AGENT_DESKTOP__) host.__CODE_AGENT_DESKTOP__.closeReady = false
    listening = false
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('应用窗口已关闭。')) }
    pending.clear(); guards.clear(); windowListeners.clear(); windowState = null
  }
  connect()
  return { available, integratedTitlebar, connect, request, prepareLeave, dispose,
    subscribeWindowState(listener) { windowListeners.add(listener); if (windowState) listener(windowState); return () => windowListeners.delete(listener) },
    registerLeaveGuard(guard) { guards.add(guard); return () => guards.delete(guard) } }
}

export function createWorkbenchLeaveGuard({ capture, flush, hasUnsaved, storageError, confirm }) {
  return async reason => {
    capture()
    const recovered = flush()
    if (!hasUnsaved()) return true
    const action = reason === 'restart' ? '重新加载工作台' : '关闭应用'
    const detail = recovered ? '本地恢复草稿已保留，但这些修改还没有保存到项目文件。'
      : `恢复草稿未能保存，继续可能丢失编辑。${storageError?.() || ''}`
    return Boolean(await confirm(`还有未保存的文件。\n\n${detail}\n\n仍要${action}吗？`))
  }
}

export const desktopRuntime = createDesktopBridge()
