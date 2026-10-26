const DEFAULT_ZOOM = 100
const MIN_ZOOM = 25
const MAX_ZOOM = 200
const ZOOM_STEP = 25

/** Percentages remain fractional; missing/non-finite values use 100%. */
export function clampZoom(percent = DEFAULT_ZOOM) {
  return Number.isFinite(percent) ? Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, percent)) : DEFAULT_ZOOM
}

/** Move by 25 percentage points. Only the directions +1 and -1 act. */
export function stepZoom(current, direction) {
  const zoom = clampZoom(current)
  return direction === 1 || direction === -1 ? clampZoom(zoom + direction * ZOOM_STEP) : zoom
}

/** Wheel/pinch deltas use pixels, lines (16px), or the current viewport height. */
export function wheelZoom(current, { deltaY, deltaMode = 0, pageHeight = 800 } = {}) {
  const zoom = clampZoom(current)
  if (!Number.isFinite(deltaY) || deltaY === 0 || ![0, 1, 2].includes(deltaMode)) return zoom
  const page = Number.isFinite(pageHeight) && pageHeight > 0 ? pageHeight : 800
  const pixels = deltaY * (deltaMode === 1 ? 16 : deltaMode === 2 ? page : 1)
  return clampZoom(zoom * Math.exp(Math.max(-20, Math.min(20, -pixels * .002))))
}

/** Fit both axes, with padding on each side; invalid dimensions use 100%. */
export function fitCanvasZoom({ viewportWidth, viewportHeight, frameWidth, frameHeight, padding = 48 } = {}) {
  if (![viewportWidth, viewportHeight, frameWidth, frameHeight].every(value => Number.isFinite(value) && value > 0)) return DEFAULT_ZOOM
  const inset = Number.isFinite(padding) ? Math.max(0, padding) : 48
  const width = Math.max(0, viewportWidth - inset * 2), height = Math.max(0, viewportHeight - inset * 2)
  const ratio = Math.min(width / frameWidth, height / frameHeight)
  // Clamp before converting to percent so extreme valid dimensions cannot
  // overflow into an invalid percentage and accidentally select the default.
  return Math.min(MAX_ZOOM / 100, Math.max(MIN_ZOOM / 100, ratio)) * 100
}

function coordinate(value) {
  return Number.isFinite(value) ? Math.min(Number.MAX_SAFE_INTEGER, Math.max(0, value)) : 0
}

/** Keep the same content point beneath a viewport-relative pointer anchor. */
export function zoomScrollPosition({ scrollLeft = 0, scrollTop = 0, anchorX = 0, anchorY = 0, previousZoom, nextZoom } = {}) {
  const ratio = clampZoom(nextZoom) / clampZoom(previousZoom)
  const x = coordinate(anchorX), y = coordinate(anchorY)
  return {
    scrollLeft: coordinate((coordinate(scrollLeft) + x) * ratio - x),
    scrollTop: coordinate((coordinate(scrollTop) + y) * ratio - y),
  }
}

/** Avoid canvas shortcuts while a form control or editable ancestor owns input. */
export function isDesignTypingTarget(target) {
  const visited = new Set()
  for (let node = target; node && (typeof node === 'object' || typeof node === 'function') && !visited.has(node);
    node = node.parentElement || node.parentNode || node.host) {
    visited.add(node)
    if (typeof node.tagName === 'string' && /^(input|textarea|select)$/i.test(node.tagName)) return true
    if (node.isContentEditable === true) return true
    const editable = typeof node.getAttribute === 'function' ? node.getAttribute('contenteditable') : null
    if (typeof editable === 'string' && ['', 'true', 'plaintext-only'].includes(editable.trim().toLowerCase())) return true
  }
  return false
}
