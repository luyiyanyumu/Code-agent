import { reactive, ref, watch } from 'vue'

export const PREFERENCE_STORAGE_KEY = 'chat-agent:preferences'
export const PREFERENCE_DEFAULTS = Object.freeze({ theme: 'light', sendKey: 'enter', reducedMotion: false })
const choices = { theme: ['light', 'dark', 'system'], sendKey: ['enter', 'ctrl-enter'] }
const preferences = reactive({ ...PREFERENCE_DEFAULTS })
const storageError = ref('')
let loaded = false
let disposeEffects = null

function valid(key, value) {
  return key === 'reducedMotion' ? typeof value === 'boolean' : !!choices[key]?.includes(value)
}

function decode(raw) {
  if (raw == null) return { ...PREFERENCE_DEFAULTS }
  const saved = JSON.parse(raw)
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) throw new Error('invalid preferences')
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
    // An existing same-origin coding preference may seed the first visit. Never
    // write its key; subsequent changes belong only to this chat application.
    const next = decode(raw ?? storage.getItem('code-agent:preferences'))
    Object.assign(preferences, next)
    if (raw === null) storage.setItem(PREFERENCE_STORAGE_KEY, JSON.stringify(next))
  } catch {
    loaded = true
    storageError.value = '浏览器偏好无法读取或保存，本次对话仍可使用默认设置。'
  }
}

function updatePreference(key, value) {
  loadPreferences()
  if (!Object.hasOwn(PREFERENCE_DEFAULTS, key) || !valid(key, value)) return false
  const next = { ...preferences, [key]: value }
  try {
    globalThis.localStorage.setItem(PREFERENCE_STORAGE_KEY, JSON.stringify(next))
    Object.assign(preferences, next)
    storageError.value = ''
    return true
  } catch {
    storageError.value = '设置未保存：请检查浏览器的存储权限后重试。'
    return false
  }
}

export function usePreferences() {
  loadPreferences()
  return { preferences, updatePreference, storageError }
}

export function initializePreferences() {
  loadPreferences()
  if (disposeEffects) return disposeEffects
  const root = globalThis.document?.documentElement
  const browser = globalThis.window
  const media = browser?.matchMedia?.('(prefers-color-scheme: dark)')
  const applyAppearance = () => {
    const theme = preferences.theme === 'system' ? (media?.matches ? 'dark' : 'light') : preferences.theme
    root?.setAttribute('data-theme', theme)
    root?.setAttribute('data-reduced-motion', String(preferences.reducedMotion))
    if (root?.style) root.style.colorScheme = theme
  }
  const stop = watch(() => [preferences.theme, preferences.reducedMotion], applyAppearance, { immediate: true, flush: 'sync' })
  const sync = event => {
    if (event.key !== PREFERENCE_STORAGE_KEY && event.key !== null) return
    if (event.storageArea && event.storageArea !== globalThis.localStorage) return
    try { Object.assign(preferences, decode(event.newValue)); storageError.value = '' }
    catch { storageError.value = '其他窗口的偏好无法读取，当前设置已保留。' }
  }
  browser?.addEventListener('storage', sync)
  media?.addEventListener?.('change', applyAppearance)
  disposeEffects = () => {
    stop()
    browser?.removeEventListener('storage', sync)
    media?.removeEventListener?.('change', applyAppearance)
    disposeEffects = null
  }
  return disposeEffects
}

export function shouldSendMessage(event, sendKey, composing = false) {
  if (composing || event.isComposing || event.keyCode === 229 || event.key !== 'Enter'
    || event.shiftKey || event.altKey || event.repeat) return false
  return sendKey === 'ctrl-enter' ? !!(event.ctrlKey || event.metaKey) : !event.ctrlKey && !event.metaKey
}
