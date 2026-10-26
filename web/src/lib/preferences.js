import { reactive, ref, watch } from 'vue'

export const PREFERENCE_STORAGE_KEY = 'code-agent:preferences'
export const PREFERENCE_DEFAULTS = Object.freeze({
  theme: 'light',
  editorFontSize: 14,
  editorTabSize: 4,
  editorWordWrap: 'off',
  editorMinimap: false,
  editorLineNumbers: 'on',
  editorWhitespace: 'none',
  sendKey: 'enter',
  defaultView: 'code',
  reducedMotion: false,
})

const choices = {
  theme: ['light', 'dark', 'system'], editorTabSize: [2, 4, 8],
  editorWordWrap: ['on', 'off'], editorLineNumbers: ['on', 'off', 'relative'],
  editorWhitespace: ['none', 'boundary', 'all'], sendKey: ['enter', 'ctrl-enter'],
  defaultView: ['code'],
}
const preferences = reactive({ ...PREFERENCE_DEFAULTS })
const storageError = ref('')
let loaded = false
let disposeEffects = null

function valid(key, value) {
  if (!Object.hasOwn(PREFERENCE_DEFAULTS, key)) return false
  if (key === 'editorFontSize') return Number.isInteger(value) && value >= 12 && value <= 24
  if (key === 'editorMinimap' || key === 'reducedMotion') return typeof value === 'boolean'
  return choices[key]?.includes(value) || false
}

function decode(raw) {
  if (raw == null) return { ...PREFERENCE_DEFAULTS }
  const saved = JSON.parse(raw)
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) throw new Error('invalid preferences')
  // Ignore unknown/invalid fields so a stale or manually edited record cannot
  // pass arbitrary options to Monaco or change execution permissions.
  return Object.fromEntries(Object.entries(PREFERENCE_DEFAULTS).map(([key, fallback]) =>
    [key, valid(key, saved[key]) ? saved[key] : fallback]))
}

function loadPreferences() {
  if (loaded) return
  try {
    const storage = globalThis.localStorage
    if (!storage) return
    loaded = true
    const raw = storage.getItem(PREFERENCE_STORAGE_KEY)
    const next = decode(raw)
    Object.assign(preferences, next)
    // The coding workspace is the only main page. Migrate removed startup
    // choices without resetting unrelated editor and appearance preferences.
    if (raw != null && JSON.parse(raw)?.defaultView !== next.defaultView) {
      try { storage.setItem(PREFERENCE_STORAGE_KEY, JSON.stringify(next)) }
      catch { storageError.value = '已改为代码工作区，但无法保存启动偏好。请检查浏览器的存储权限。' }
    }
  } catch {
    loaded = true
    storageError.value = '无法读取浏览器偏好，已使用默认值。请检查浏览器的存储权限。'
  }
}

function save(next) {
  try {
    globalThis.localStorage.setItem(PREFERENCE_STORAGE_KEY, JSON.stringify(next))
    Object.assign(preferences, next)
    storageError.value = ''
    return true
  } catch {
    storageError.value = '保存失败：浏览器存储不可用或空间不足。设置尚未更改，请检查后重试。'
    return false
  }
}

function updatePreference(key, value) {
  loadPreferences()
  if (!valid(key, value)) {
    storageError.value = '设置值无效，请选择支持的选项。'
    return false
  }
  return save({ ...preferences, [key]: value })
}

function resetPreferences(keys = Object.keys(PREFERENCE_DEFAULTS)) {
  loadPreferences()
  if (!Array.isArray(keys) || keys.some(key => !Object.hasOwn(PREFERENCE_DEFAULTS, key))) {
    storageError.value = '无法恢复未知设置。'
    return false
  }
  return save({ ...preferences, ...Object.fromEntries(keys.map(key => [key, PREFERENCE_DEFAULTS[key]])) })
}

export function usePreferences() {
  loadPreferences()
  return { preferences, updatePreference, resetPreferences, storageError }
}

/** Install once at the application boundary; components share one reactive record. */
export function initializePreferences() {
  loadPreferences()
  if (disposeEffects) return disposeEffects
  const root = globalThis.document?.documentElement
  const browser = globalThis.window
  const media = browser?.matchMedia?.('(prefers-color-scheme: dark)')
  function applyAppearance() {
    const theme = preferences.theme === 'system' ? (media?.matches ? 'dark' : 'light') : preferences.theme
    root?.setAttribute('data-theme', theme)
    root?.setAttribute('data-reduced-motion', String(preferences.reducedMotion))
    if (root?.style) root.style.colorScheme = theme
  }
  const stop = watch(() => [preferences.theme, preferences.reducedMotion], applyAppearance, { immediate: true, flush: 'sync' })
  const sync = event => {
    if (event.key !== PREFERENCE_STORAGE_KEY && event.key !== null) return
    try {
      if (event.storageArea && event.storageArea !== globalThis.localStorage) return
      Object.assign(preferences, decode(event.newValue))
      storageError.value = ''
    } catch {
      storageError.value = '其他窗口的偏好数据无法读取，当前设置已保留。'
    }
  }
  browser?.addEventListener('storage', sync)
  if (media?.addEventListener) media.addEventListener('change', applyAppearance)
  else media?.addListener?.(applyAppearance)
  disposeEffects = () => {
    stop()
    browser?.removeEventListener('storage', sync)
    if (media?.removeEventListener) media.removeEventListener('change', applyAppearance)
    else media?.removeListener?.(applyAppearance)
    disposeEffects = null
  }
  return disposeEffects
}

export function shouldSendMessage(event, sendKey, composing = false) {
  if (composing || event.isComposing || event.keyCode === 229 || event.key !== 'Enter'
    || event.shiftKey || event.altKey || event.repeat) return false
  return sendKey === 'ctrl-enter' ? !!(event.ctrlKey || event.metaKey) : !event.ctrlKey && !event.metaKey
}

export function editorPreferenceOptions(value) {
  return {
    fontSize: value.editorFontSize,
    lineHeight: Math.round(value.editorFontSize * 1.6),
    tabSize: value.editorTabSize,
    insertSpaces: true,
    detectIndentation: false,
    wordWrap: value.editorWordWrap,
    minimap: { enabled: value.editorMinimap },
    lineNumbers: value.editorLineNumbers,
    renderWhitespace: value.editorWhitespace,
    smoothScrolling: !value.reducedMotion,
  }
}
