const normalized = value => String(value ?? '').normalize('NFKC').trim().toLocaleLowerCase().replace(/\s+/g, ' ')
const words = value => Array.isArray(value) ? value.join(' ') : String(value ?? '')

/** Keep menu and command search on the same live actions and disabled states. */
export function flattenIdeCommands(menus, { includeDisabled = false } = {}) {
  const result = [], seen = new Set()
  for (const menu of Array.isArray(menus) ? menus : []) {
    if (!menu || !menu.id || !menu.label) continue
    for (const item of Array.isArray(menu.items) ? menu.items : []) {
      if (!item || item.separator || !item.id || !item.label || item.hidden || seen.has(item.id)) continue
      seen.add(item.id)
      if (!includeDisabled && item.disabled) continue
      result.push({ ...item, menuId: menu.id, menuLabel: menu.label })
    }
  }
  return result
}

export function searchIdeCommands(menus, query = '', { limit = 30 } = {}) {
  const text = normalized(query).replace(/^>\s*/, ''), tokens = text.split(' ').filter(Boolean)
  const rows = flattenIdeCommands(menus).map((command, index) => {
    const label = normalized(command.label), shortcut = normalized(command.shortcut).replace(/\s+/g, '')
    const haystack = normalized([command.menuLabel, command.label, command.shortcut, command.id, words(command.keywords)].join(' '))
    if (!tokens.every(token => haystack.includes(token) || shortcut.includes(token.replace(/\s+/g, '')))) return null
    const rank = !text ? 0 : label === text ? 4 : label.startsWith(text) ? 3 : label.includes(text) ? 2 : 1
    return { command, index, rank }
  }).filter(Boolean)
  return rows.sort((a, b) => b.rank - a.rank || a.index - b.index).slice(0, Math.max(0, Number(limit) || 0)).map(row => row.command)
}
