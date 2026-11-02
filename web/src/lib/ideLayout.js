export const IDE_LAYOUT_KEY = 'code-agent-ide-layout-v1'
export const defaultIdeLayout = Object.freeze({ left: true, right: true, bottom: false, bottomTab: 'problems', bottomHeight: 260 })
const tabs = new Set(['problems', 'terminal', 'run', 'output', 'tests', 'debug', 'integration'])
function normalize(value = {}) {
  const number = Number(value.bottomHeight)
  return { left: typeof value.left === 'boolean' ? value.left : true, right: typeof value.right === 'boolean' ? value.right : true,
    bottom: value.bottom === true, bottomTab: tabs.has(value.bottomTab) ? value.bottomTab : 'problems',
    bottomHeight: Number.isFinite(number) ? Math.max(160, Math.min(600, number)) : 260 }
}
export function readIdeLayout(storage) { try { return normalize(JSON.parse(storage.getItem(IDE_LAYOUT_KEY) || '{}')) } catch { return { ...defaultIdeLayout } } }
export function persistIdeLayout(storage, value) { try { storage.setItem(IDE_LAYOUT_KEY, JSON.stringify(normalize(value))); return true } catch { return false } }
