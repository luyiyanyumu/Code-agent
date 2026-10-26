import { STYLE_FIELDS } from './frontendDesign.js'

const labels = new Map(STYLE_FIELDS.map(field => [field.property, field.label]))
const options = entries => entries.map(([value, label]) => ({ value, label }))
const enumOptions = {
  display: options([['block', '块级'], ['inline', '行内'], ['inline-block', '行内块'], ['flex', '弹性布局'], ['inline-flex', '行内弹性'], ['grid', '网格'], ['inline-grid', '行内网格'], ['none', '隐藏'], ['contents', '仅内容']]),
  'flex-direction': options([['row', '横向'], ['column', '纵向'], ['row-reverse', '横向反转'], ['column-reverse', '纵向反转']]),
  'justify-content': options([['normal', '默认'], ['flex-start', '起点'], ['center', '居中'], ['flex-end', '终点'], ['space-between', '两端对齐'], ['space-around', '环绕间距'], ['space-evenly', '等间距']]),
  'align-items': options([['normal', '默认'], ['stretch', '拉伸'], ['flex-start', '起点'], ['center', '居中'], ['flex-end', '终点'], ['baseline', '基线对齐']]),
  'text-align': options([['start', '起点'], ['left', '左对齐'], ['center', '居中'], ['right', '右对齐'], ['end', '终点'], ['justify', '两端对齐']]),
}
const field = property => Object.freeze({ property, label: labels.get(property), control: enumOptions[property] ? 'select' : ['color', 'background-color'].includes(property) ? 'color' : property === 'opacity' ? 'opacity' : 'text', options: enumOptions[property] || [], placeholder: property === 'translate' ? 'Xpx Ypx' : undefined })
export const INSPECTOR_GROUPS = Object.freeze([
  ['position', '位置', ['translate']],
  ['size', '尺寸', ['width', 'height']],
  ['layout', '布局', ['display', 'flex-direction', 'justify-content', 'align-items']],
  ['spacing', '间距', ['padding', 'margin', 'gap']],
  ['text', '文字', ['color', 'font-size', 'font-weight', 'font-family', 'line-height', 'text-align']],
  ['fill', '填充', ['background-color']],
  ['effects', '边框与效果', ['border', 'border-radius', 'box-shadow', 'opacity']],
].map(([id, label, properties]) => Object.freeze({ id, label, fields: Object.freeze(properties.map(field)) })))

const pixelFields = new Set(['width', 'height', 'padding', 'margin', 'gap', 'border-radius', 'font-size'])
const numberPattern = /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)$/
export function normalizeInspectorInput(property, input) {
  const value = String(input ?? '').trim()
  if (property === 'translate' && value) {
    const axes = value.split(/\s+/)
    if (axes.length <= 2 && axes.every(axis => numberPattern.test(axis) || /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)px$/.test(axis))) return axes.map(axis => numberPattern.test(axis) ? `${axis}px` : axis).join(' ')
    return value
  }
  if (!value || !numberPattern.test(value)) return value
  if (property === 'opacity') return String(Math.max(0, Math.min(1, Number(value))))
  return pixelFields.has(property) ? `${value}px` : value
}

/** Step a single numeric value without rewriting CSS expressions or shorthands. */
export function stepInspectorInput(property, input, direction, { shiftKey = false, altKey = false } = {}) {
  if (![1, -1].includes(direction) || !(pixelFields.has(property) || ['font-weight', 'line-height', 'opacity'].includes(property))) return null
  const match = /^([-+]?(?:\d+(?:\.\d*)?|\.\d+))(px|rem|em|%|vh|vw|ch|ex)?$/i.exec(String(input ?? '').trim())
  if (!match || (['font-weight', 'opacity'].includes(property) && match[2])) return null
  const unit = (match[2] || '').toLowerCase()
  const base = property === 'opacity' ? .01 : property === 'font-weight' ? 100 : property === 'line-height' && !unit ? .1 : 1
  const amount = altKey ? (property === 'font-weight' ? 1 : base / 10) : shiftKey ? base * 10 : base
  let value = Number(match[1]) + amount * direction
  if (!Number.isFinite(value) || Math.abs(value) > 1e6) return null
  if (property === 'opacity') value = Math.max(0, Math.min(1, value))
  else if (property === 'font-weight') value = Math.max(1, Math.min(1000, Math.round(value)))
  else if (property !== 'margin') value = Math.max(0, value)
  value = Math.round(value * 1e6) / 1e6
  return normalizeInspectorInput(property, `${value}${unit}`)
}

export function hexColorForPicker(input) {
  const value = String(input || '').trim().toLowerCase()
  if (/^#[\da-f]{6}$/.test(value)) return value
  if (/^#[\da-f]{3}$/.test(value)) return `#${[...value.slice(1)].map(character => character + character).join('')}`
  const rgb = /^rgba?\(([^()]*)\)$/.exec(value)
  if (rgb) {
    const body = rgb[1].trim()
    const channels = body.includes(',') ? body.split(',').slice(0, 3) : body.split('/')[0].trim().split(/\s+/)
    if (channels.length === 3) {
      const numeric = channels.map(channel => {
        const text = channel.trim()
        if (!/^[-+]?(?:\d+(?:\.\d*)?|\.\d+)%?$/.test(text)) return NaN
        return Math.round(Math.max(0, Math.min(255, text.endsWith('%') ? Number(text.slice(0, -1)) * 255 / 100 : Number(text))))
      })
      if (numeric.every(Number.isFinite)) return `#${numeric.map(channel => channel.toString(16).padStart(2, '0')).join('')}`
    }
  }
  return '#000000'
}
