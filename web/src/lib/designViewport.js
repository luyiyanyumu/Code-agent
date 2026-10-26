export const DESIGN_VIEWPORT_PRESETS = Object.freeze([
  { id: 'desktop', label: '桌面', width: 1440, height: 900 },
  { id: 'laptop', label: '笔记本', width: 1280, height: 800 },
  { id: 'tablet', label: '平板', width: 768, height: 1024 },
  { id: 'phone', label: '手机', width: 390, height: 844 },
])

export function normalizeDesignViewport(value) {
  if (!value || typeof value !== 'object') return null
  const { width, height } = value
  if (![width, height].every(number => typeof number === 'number' && Number.isInteger(number))) return null
  if (width < 240 || width > 3840 || height < 240 || height > 2160) return null
  return { width, height }
}
