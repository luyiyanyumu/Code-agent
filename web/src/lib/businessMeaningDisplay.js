export const BUSINESS_SOURCE_LABEL = { human: '人工功能描述', comment: '源码功能描述候选', model: '模型自动补充候选' }
export const BUSINESS_STATUS_LABEL = { current: '已确认', unreviewed: '待确认', stale: '待更新', missing: '待自动补充' }

// An omitted identity means a completed batch may have updated any record in
// this project/tree. Keep narrower single-record saves scoped to their entity.
export function matchesBusinessMeaningChange(change, { projectId, tree = 'main', id }) {
  return !!id && change?.projectId === projectId && (change.tree || 'main') === (tree || 'main')
    && (!change.id || change.id === id)
}

/** A domain and its corresponding module share the same business record. */
export function businessEntityId(detail) {
  const id = String(detail?.id || '')
  if (id.startsWith('fdomain:')) {
    const path = id.slice(8)
    return `mod:${path === '根目录' ? '' : path}`
  }
  return /^(sym:|file:|mod:)/.test(id) ? id : ''
}

export function displayBusiness(value) {
  const business = value && typeof value === 'object' ? value : {}
  const status = Object.hasOwn(BUSINESS_STATUS_LABEL, business.status) ? business.status : 'missing'
  // A candidate can only become confirmed through an explicit human save.
  const displayStatus = status === 'current' && business.source !== 'human' ? 'unreviewed' : status
  if (displayStatus === 'missing' || displayStatus === 'stale') {
    return { ...business, status: displayStatus, business_meaning: '', detail: '', inputs: [], outputs: [], rules: [], effects: [], feature_refs: [], evidence: [] }
  }
  return { ...business, status: displayStatus }
}
