import { normalizeStyleDeclarations } from './frontendDesign.js'

export const MAX_DESIGN_PRESETS = 20
export const MAX_DESIGN_PRESET_NAME = 40
export const DESIGN_INSTRUCTION_TEMPLATES = Object.freeze([
  { id: 'contrast', label: '提高对比', instruction: '提高当前元素的文字与背景对比，让重点更清晰，保留现有内容和结构。' },
  { id: 'compact', label: '紧凑布局', instruction: '让当前元素更紧凑，适当减少内外边距和间隙，保持文字清晰、内容不被裁切。' },
  { id: 'card', label: '卡片样式', instruction: '将当前元素调整为简洁卡片：增加适当留白、圆角和轻微阴影，保持与页面配色协调。' },
  { id: 'mobile', label: '移动友好', instruction: '让当前元素更适合手机浏览：避免固定大宽度，改善留白与文字可读性，保证操作区域便于点击。' },
].map(item => Object.freeze(item)))

export function normalizeDesignPresetName(value) {
  return typeof value === 'string' ? Array.from(value.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/gu, ' ').trim()).slice(0, MAX_DESIGN_PRESET_NAME).join('') : ''
}

/** Bound persisted input and retain only nonempty, safe visual declarations. */
export function normalizeDesignPresets(input) {
  if (!Array.isArray(input)) return []
  const output = [], seen = new Set()
  for (const item of input.slice(0, MAX_DESIGN_PRESETS)) {
    if (!item || typeof item !== 'object' || typeof item.id !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9:_-]{0,127}$/.test(item.id) || seen.has(item.id)) continue
    const name = normalizeDesignPresetName(item.name), declarations = normalizeStyleDeclarations(item.declarations)
    if (!name || !Object.keys(declarations).length) continue
    seen.add(item.id)
    output.push({ id: item.id, name, declarations })
  }
  return output
}

/** The parent supplies identity and owns project storage/history. */
export function createDesignPreset({ id, name, declarations } = {}) {
  return normalizeDesignPresets([{ id, name, declarations }])[0] || null
}
