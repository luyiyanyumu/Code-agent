// Only explicit, public operation explanations belong in tool cards.
// Provider thinking streams and other internal reasoning fields are ignored.
const text = (value, limit) => typeof value === 'string' ? value.trim().slice(0, limit) : ''
export function normalizeToolExplanation(value) {
  const record = value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  const purpose = text(record.purpose, 4000), basis = text(record.basis, 800)
  const effects = (Array.isArray(record.effects) ? record.effects : [record.effects])
    .map(effect => text(effect, 2000)).filter(Boolean).slice(0, 12)
  return { purpose, basis, effects, source: purpose || basis || effects.length ? 'agent' : 'missing' }
}
export function toolArguments(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value
  return Object.fromEntries(Object.entries(value).filter(([key]) => key !== '_approval'))
}
export function explanationForTool(payload) {
  // New records carry their explanation separately from executable arguments.
  // Existing records retain the same explicit declaration in reserved metadata.
  return normalizeToolExplanation(payload?.explanation ?? payload?.call?.arguments?._approval)
}
