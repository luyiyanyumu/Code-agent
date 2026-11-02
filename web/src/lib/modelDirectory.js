// The catalog is supplied by the server. No guessed model IDs or reasoning levels.
export function rankedModels(groups = [], query = '') {
  const needle = String(query).trim().toLocaleLowerCase()
  function score(name) {
    const text = String(name).toLocaleLowerCase()
    if (!needle) return 0
    let cursor = 0, cost = 0
    for (const letter of needle) {
      const index = text.indexOf(letter, cursor)
      if (index < 0) return Infinity
      cost += index - cursor; cursor = index + 1
    }
    return (text.startsWith(needle) ? 0 : 1000) + cost
  }
  return groups.map(group => ({ ...group, models: (group.models || [])
    .map((model, order) => ({ ...model, order, score: score(model.name || model.id) }))
    .filter(model => Number.isFinite(model.score))
    .sort((a, b) => a.score - b.score || a.order - b.order) }))
    .filter(group => group.models.length)
}

export function selectionName(directory) {
  const current = directory?.current
  if (!current?.model) return '选择模型'
  const group = directory.groups?.find(item => item.id === current.provider)
  const choice = group?.models?.find(item => item.id === current.model)
  return choice?.name || current.model
}

export function reasoningSelection(directory) {
  const current = directory?.current
  const model = directory?.groups?.find(group => group.id === current?.provider)
    ?.models?.find(model => model.id === current?.model)
  const capability = model?.reasoning
  if (!capability?.efforts?.length) return null
  const choices = capability.defaultEffort == null
    ? [{id: '', name: '服务商默认'}, ...capability.efforts]
    : capability.efforts
  const id = current.reasoningEffort ?? capability.defaultEffort ?? ''
  return {id, choices, name: choices.find(choice => choice.id === id)?.name || id}
}
