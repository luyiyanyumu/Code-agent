// Monaco 0.56's NLS lookup reads this bootstrap table at render time.
// Keep the two suggestion-widget fallbacks Chinese, even before docs resolve.
// The test verifies these indexes against the pinned package's actual source.
export function installCompletionLocale(target = globalThis) {
  const messages = target._VSCODE_NLS_MESSAGES || []
  const close = !messages[1542] || messages[1542] === 'Close'
  const loading = !messages[1543] || messages[1543] === 'Loading...'
  if (!close && !loading) return
  const localized = Array.isArray(messages) ? messages.slice() : { ...messages }
  if (close) localized[1542] = '关闭'
  if (loading) localized[1543] = '正在读取说明…'
  target._VSCODE_NLS_MESSAGES = localized
}
