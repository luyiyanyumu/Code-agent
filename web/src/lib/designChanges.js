import { STYLE_FIELDS, serializeDesignCss, parseDesignCss } from './frontendDesign.js'

function declarationsBySelector(rules) {
  const result = new Map()
  for (const rule of rules) {
    // Repeated selectors have the same cascade order as the exported CSS.
    result.set(rule.selector, { ...result.get(rule.selector), ...rule.declarations })
  }
  return result
}

/** Compare only the visual rules accepted by the managed-CSS serializer. */
export function buildDesignChanges(rules, savedCss = '') {
  let currentCss
  try { currentCss = serializeDesignCss(rules) }
  catch { return { changes: [], elementCount: 0, propertyCount: 0, currentCss: serializeDesignCss([]), exportable: false } }
  const currentRules = parseDesignCss(currentCss)
  const current = declarationsBySelector(currentRules)
  const saved = declarationsBySelector(parseDesignCss(savedCss))
  const changes = []
  let propertyCount = 0
  for (const selector of new Set([...current.keys(), ...saved.keys()])) {
    const before = saved.get(selector), after = current.get(selector)
    const properties = []
    for (const { property, label } of STYLE_FIELDS) {
      const previous = before?.[property] ?? null, next = after?.[property] ?? null
      if (previous === next) continue
      properties.push({ property, label, before: previous, after: next, status: previous === null ? 'added' : next === null ? 'removed' : 'modified' })
    }
    if (!properties.length) continue
    propertyCount += properties.length
    changes.push({ selector, status: !before ? 'added' : !after ? 'removed' : 'modified', hasCurrent: Boolean(after), properties })
  }
  return { changes, elementCount: changes.length, propertyCount, currentCss, exportable: true }
}
