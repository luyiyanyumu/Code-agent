// 会话流连接的生命周期独立于 Vue：切会话/卸载后，旧帧与旧重连不能再写状态。
const TERMINAL_CLOSES = {
  4401: '会话连接未获授权，请刷新页面后重试',
  4404: '会话不存在，请选择其他会话',
  4409: '会话已关闭，请选择其他会话',
}

export function createSessionStream({
  open, afterSeq, onEvent, onState, onError,
  schedule = setTimeout, unschedule = clearTimeout,
}) {
  let socket = null
  let generation = 0
  let reconnectTimer = null
  let heartbeatTimer = null

  function clearTimers() {
    if (reconnectTimer !== null) unschedule(reconnectTimer)
    if (heartbeatTimer !== null) unschedule(heartbeatTimer)
    reconnectTimer = heartbeatTimer = null
  }

  function close() {
    generation += 1
    clearTimers()
    if (socket) {
      const old = socket
      socket = null
      old.onopen = old.onmessage = old.onclose = old.onerror = null
      old.close()
    }
    onState('idle')
  }

  function connect(id) {
    close()
    if (!id) return
    const mine = generation

    function retry() {
      reconnectTimer = schedule(() => {
        reconnectTimer = null
        if (mine === generation) start()
      }, 2000)
    }

    function start() {
      if (mine !== generation) return
      onState('connecting')
      let next
      try {
        next = open(id, afterSeq())
      } catch (error) {
        onState('closed')
        onError(error.message || '无法建立会话连接')
        retry()
        return
      }
      socket = next
      const live = () => mine === generation && socket === next
      function heartbeat() {
        heartbeatTimer = schedule(() => {
          heartbeatTimer = null
          if (!live() || next.readyState !== 1) return
          try { next.send(JSON.stringify({ type: 'ping' })) } catch { next.close(); return }
          heartbeat()
        }, 30000)
      }
      next.onopen = () => {
        if (!live()) return
        onState('open')
        heartbeat()
      }
      next.onmessage = (message) => {
        if (!live()) return
        let event
        try { event = JSON.parse(message.data) } catch { return }
        if (event && typeof event === 'object' && typeof event.type === 'string') onEvent(event)
      }
      next.onclose = (event) => {
        if (!live()) return
        socket = null
        clearTimers()
        onState('closed')
        const reason = TERMINAL_CLOSES[event?.code]
        if (reason) onError(reason)
        else retry()
      }
    }
    start()
  }

  return { connect, close, get socket() { return socket } }
}
