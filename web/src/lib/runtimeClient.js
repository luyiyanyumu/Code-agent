// PTY 和运行输出共用递增序号；ready.next_seq 是服务端末尾，不能在历史回放前跳过去。
const sequence = value => Math.max(0, Math.trunc(Number(value) || 0))
export const isRunning = meta => ['running', 'starting', 'stopping'].includes(meta?.status)

export function createRuntimeCursor({ afterSeq = 0, onOutput = () => {}, onExit = () => {}, onGap = () => {} } = {}) {
  let lastSeq = sequence(afterSeq)
  function gap(firstSeq) {
    const first = sequence(firstSeq)
    if (first > lastSeq + 1) {
      onGap({ after_seq: lastSeq, first_seq: first })
      lastSeq = first - 1
    }
  }
  function accept(event) {
    if (!event || !['output', 'exit'].includes(event.type)) return false
    const seq = sequence(event.seq)
    if (!seq || seq <= lastSeq) return false
    gap(seq)
    lastSeq = seq
    if (event.type === 'output') onOutput(String(event.data ?? ''), event)
    else onExit(event.code ?? null, event)
    return true
  }
  return { accept, gap, get lastSeq() { return lastSeq } }
}

export function createTerminalStream({ open, onOutput, onExit = () => {}, onGap, onReady = () => {}, onState = () => {}, onError = () => {}, schedule = setTimeout, unschedule = clearTimeout } = {}) {
  let socket = null, timer = null, generation = 0, target = null, attempts = 0, ended = false, state = 'closed', size = null
  let cursor = createRuntimeCursor()
  const setState = next => { state = next; onState(next) }
  function release() {
    if (timer != null) { unschedule(timer); timer = null }
    const old = socket; socket = null
    if (old) {
      old.onopen = old.onmessage = old.onerror = old.onclose = null
      try { old.close() } catch { /* already released */ }
    }
  }
  function send(frame) {
    if (socket?.readyState !== 1 || ended) return false
    try { socket.send(JSON.stringify(frame)); return true } catch { return false }
  }
  function retry(mine) {
    if (mine !== generation || !target || ended || timer != null) return
    setState('reconnecting')
    timer = schedule(() => { timer = null; if (mine === generation) attach(mine) }, Math.min(8000, 500 * 2 ** Math.min(attempts++, 4)))
  }
  function attach(mine) {
    if (mine !== generation || !target || ended) return
    setState(attempts ? 'reconnecting' : 'connecting')
    let ws
    try { ws = open(target.projectId, target.id, cursor.lastSeq) }
    catch (e) { onError(e); retry(mine); return }
    socket = ws
    const current = () => mine === generation && socket === ws
    ws.onopen = () => {
      if (!current()) return
      attempts = 0; setState('open')
      if (size) send({ type: 'resize', ...size })
    }
    ws.onmessage = message => {
      if (!current()) return
      let event
      try { event = JSON.parse(message.data) } catch { return }
      if (event.type === 'ready') {
        onReady(event)
        if (event.truncated) cursor.gap(event.first_seq)
        // Completed terminals can still replay their last output before the socket closes.
        if (event.status && !isRunning(event)) { ended = true; setState('exited') }
      } else if (event.type === 'gap') cursor.gap(event.first_seq)
      else if (event.type === 'error') onError(new Error(event.message || event.detail || '终端连接错误'))
      else if (cursor.accept(event) && event.type === 'exit') { ended = true; setState('exited') }
    }
    ws.onerror = () => { if (current()) onError(new Error('终端连接暂时中断，正在重连')) }
    ws.onclose = event => {
      if (!current()) return
      socket = null
      if (ended) { setState('exited'); return }
      if ([1008, 4401, 4403, 4404, 4409].includes(event.code)) {
        ended = true; setState('closed'); onError(new Error(event.reason || '终端不可用，请刷新终端列表')); return
      }
      retry(mine)
    }
  }
  function connect(projectId, id, { afterSeq = 0 } = {}) {
    generation++; release(); attempts = 0; ended = false; size = null
    target = projectId && id ? { projectId, id } : null
    cursor = createRuntimeCursor({ afterSeq, onOutput, onGap, onExit })
    if (target) attach(generation)
    else setState('closed')
  }
  function disconnect() { generation++; target = null; release(); setState('closed') }
  return {
    connect, disconnect, dispose: disconnect,
    input: data => typeof data === 'string' && data.length ? send({ type: 'input', data }) : false,
    resize(cols, rows) {
      cols = sequence(cols); rows = sequence(rows)
      if (!cols || !rows) return false
      size = { cols, rows }; return send({ type: 'resize', ...size })
    },
    get lastSeq() { return cursor.lastSeq }, get state() { return state },
  }
}

// One request at a time for the selected run. Switching projects invalidates in-flight results.
export function createRunPoller({ load, onUpdate = () => {}, onOutput, onExit, onGap, onError = () => {}, schedule = setTimeout, unschedule = clearTimeout, intervalMs = 500 } = {}) {
  let target = null, generation = 0, timer = null, pending = null, cursor = createRuntimeCursor()
  function clearTimer() { if (timer != null) { unschedule(timer); timer = null } }
  function stop() { generation++; target = null; pending = null; clearTimer() }
  async function refresh() {
    if (!target) return
    const mine = generation, identity = target
    if (pending?.generation === mine) return pending.promise
    clearTimer()
    const request = { generation: mine, promise: null }
    pending = request
    request.promise = (async () => {
      let again = false
      try {
        const data = await load(identity.projectId, identity.id, cursor.lastSeq)
        if (mine !== generation || target !== identity) return
        if (data.truncated || sequence(data.first_seq) > cursor.lastSeq + 1) cursor.gap(data.first_seq)
        const events = Array.isArray(data.events) ? [...data.events].sort((a, b) => sequence(a.seq) - sequence(b.seq)) : []
        events.forEach(cursor.accept)
        onUpdate(data, identity)
        again = isRunning(data) || (events.length > 0 && cursor.lastSeq < sequence(data.next_seq))
      } catch (error) {
        if (mine !== generation || target !== identity) return
        onError(error); again = ![401, 403, 404].includes(error.status)
      } finally {
        if (pending === request) pending = null
        if (mine === generation && target === identity && again) timer = schedule(() => { timer = null; refresh() }, intervalMs)
      }
    })()
    return request.promise
  }
  function select(projectId, id, { afterSeq = 0 } = {}) {
    stop()
    if (!projectId || !id) return Promise.resolve()
    target = { projectId, id }
    const identity = target
    cursor = createRuntimeCursor({ afterSeq,
      onOutput: (text, event) => onOutput?.(text, event, identity),
      onExit: (code, event) => onExit?.(code, event, identity),
      onGap: gap => onGap?.(gap, identity),
    })
    return refresh()
  }
  return { select, refresh, stop, dispose: stop, get lastSeq() { return cursor.lastSeq } }
}

// Only project-relative references are navigable; terminal text itself never executes a link.
export function outputLocations(text) {
  const result = [], seen = new Set()
  const patterns = [/File\s+"([^"]+)",\s+line\s+(\d+)/g, /(?:^|[\s(])([\w.@/-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs|vue|json|go|rs|java|cs)):(\d+)(?::(\d+))?/gm]
  for (const pattern of patterns) for (const match of String(text || '').matchAll(pattern)) {
    const path = match[1].replaceAll('\\', '/')
    if (!path || path.startsWith('/') || /^[a-z]:/i.test(path) || path.split('/').includes('..')) continue
    const line = sequence(match[2]), column = Math.max(1, sequence(match[3]))
    const key = `${path}:${line}:${column}`
    if (line && !seen.has(key)) { seen.add(key); result.push({ path, line, column }) }
  }
  return result.slice(0, 30)
}
