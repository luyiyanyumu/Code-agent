import { hoverMarkdownStrings, normalizeHoverDocumentation } from './hoverDocumentation.js'
import { BUSINESS_SOURCE_LABEL, displayBusiness } from './businessMeaningDisplay.js'

const listeners = new Set()
export function notifyBusinessMeaningChanged(detail) {
  for (const callback of listeners) callback(detail)
  if (typeof window !== 'undefined' && typeof CustomEvent !== 'undefined') {
    window.dispatchEvent(new CustomEvent('code-agent:business-meaning-changed', { detail }))
  }
}
export function subscribeBusinessMeaning(callback) {
  listeners.add(callback)
  return () => listeners.delete(callback)
}

// Project prose is displayed literally: it must not become Markdown links,
// images, HTML, or editor commands inside a hover.
const literal = value => String(value ?? '').slice(0, 6000)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/[\\`*_{}\[\]()#+.!|~-]/g, '\\$&')

function withFunctionSection(hover, businessSection, extra = {}) {
  // Keep the original language sections so repeated reads replace the function
  // text once. Put it below the word's title, before signatures and long docs.
  const base = hover.businessBaseSections || hover.sections || []
  const tokenSection = hover.tokenSection
  const index = tokenSection && base.findIndex(section => section.kind === tokenSection.kind && section.value === tokenSection.value)
  let all
  if (Number.isInteger(index) && index >= 0 && tokenSection.kind === 'markdown') {
    const split = tokenSection.value.indexOf('\n\n')
    const title = split < 0 ? tokenSection.value : tokenSection.value.slice(0, split)
    const explanation = split < 0 ? '' : tokenSection.value.slice(split + 2)
    all = base.map((section, i) => i === index
      ? { kind: 'markdown', value: [title, businessSection.value, explanation].filter(Boolean).join('\n\n') } : section)
  } else all = [businessSection, ...base]
  return { ...hover, ...extra, sections: normalizeHoverDocumentation(all), contents: hoverMarkdownStrings(all),
    businessBaseSections: base, businessSection }
}

export function appendBusinessMeaning(hover, record) {
  if (!hover || !record?.entity || !record.business) return hover
  const business = displayBusiness(record.business)
  const status = business.status || 'missing'
  const source = BUSINESS_SOURCE_LABEL[business.source] || '项目资料'
  const lines = []
  if (status === 'stale') lines.push('**功能描述：** 代码已变化，等待自动更新。')
  else if (status === 'missing' || !business.business_meaning) lines.push('**功能描述：** 待自动补充。')
  else {
    lines.push(`**功能描述${status === 'unreviewed' ? '（待确认）' : ''}：** ${literal(business.business_meaning)}`)
    if (business.detail) lines.push(literal(business.detail))
    for (const [key, label] of [['inputs', '输入'], ['outputs', '输出']]) {
      const items = (Array.isArray(business[key]) ? business[key] : []).slice(0, 80)
      if (items.length) lines.push(`**${label}：**\n${items.map(item => `- ${literal(item.name)}：${literal(item.meaning)}`).join('\n')}`)
    }
    for (const [key, label] of [['rules', '功能规则'], ['effects', '副作用'], ['feature_refs', '所属功能']]) {
      const items = (Array.isArray(business[key]) ? business[key] : []).slice(0, 80)
      if (items.length) lines.push(`**${label}：**\n${items.map(item => `- ${literal(item)}`).join('\n')}`)
    }
    lines.push(`功能依据：${source}。`)
  }
  const businessSection = { kind: 'markdown', value: lines.join('\n\n') }
  return withFunctionSection(hover, businessSection, { business: record, businessReadError: '' })
}

function unavailableFunction(hover, message, lookup) {
  return withFunctionSection(hover, { kind: 'markdown', value: `**功能描述：** ${message}` },
    { business: null, businessReadError: message, ...(lookup ? { businessLookup: lookup } : {}) })
}

function oneDeclaration(result) {
  const locations = Array.isArray(result) ? result : result ? [result] : []
  const unique = new Map()
  for (const item of locations) {
    const uri = item?.targetUri || item?.uri
    const range = item?.targetSelectionRange || item?.range
    const start = range?.start
    if (typeof uri !== 'string' || !Number.isInteger(start?.line) || !Number.isInteger(start?.character)
      || start.line < 0 || start.character < 0) continue
    unique.set(`${uri}\0${start.line}\0${start.character}`, { uri, line: start.line + 1, column: start.character + 1 })
  }
  return unique.size === 1 ? [...unique.values()][0] : null
}

// One cache per model revision. Hovering a word reuses a pending/read record;
// typing, changing projects, or saving a business explanation invalidates it.
export function createBusinessMeaningSource({ contextForModel, targetForModel, resolve, timeoutMs = 1800, ttlMs = 30000, now = Date.now }) {
  let caches = new WeakMap(), disposed = false, generation = 0
  const controllers = new Set(), scopeGenerations = new Map()
  const invalidate = change => {
    if (change?.projectId) {
      const key = `${change.projectId}\0${change.tree || 'main'}`
      scopeGenerations.set(key, (scopeGenerations.get(key) || 0) + 1)
      for (const pending of controllers) if (pending.scope === key) {
        pending.controller.abort(); controllers.delete(pending)
      }
    } else {
      generation++; caches = new WeakMap(); scopeGenerations.clear()
      for (const pending of controllers) pending.controller.abort()
      controllers.clear()
    }
  }
  const unsubscribe = subscribeBusinessMeaning(invalidate)
  return {
    matches(model) { return !disposed && model?.getLanguageId?.() === 'java' && !!contextForModel(model)?.projectId },
    async provide(model, position, hover, token) {
      const context = contextForModel(model), target = targetForModel(model, position, hover)
      if (disposed || !context?.projectId || !target || token?.isCancellationRequested) return hover
      const projectScope = `${context.projectId}\0${context.tree || 'main'}`
      const version = model.getVersionId?.(), epoch = generation, scopeEpoch = scopeGenerations.get(projectScope) || 0
      const scope = `${context.projectId}\0${context.tree || 'main'}\0${context.path}\0${context.uri}`
      const current = () => !disposed && epoch === generation && scopeEpoch === (scopeGenerations.get(projectScope) || 0)
        && !token?.isCancellationRequested && !model.isDisposed?.()
        && model.getVersionId?.() === version && scope === (() => {
          const value = contextForModel(model)
          return value ? `${value.projectId}\0${value.tree || 'main'}\0${value.path}\0${value.uri}` : ''
        })()
      let cache = caches.get(model)
      if (!cache || cache.version !== version || cache.scope !== scope || cache.scopeEpoch !== scopeEpoch) {
        cache = { version, scope, scopeEpoch, entries: new Map() }; caches.set(model, cache)
      }
      const key = target.declaration ? `${target.declaration.lineNumber}:${target.declaration.column}:${target.name}`
        : `${position.lineNumber}:${position.column}:${target.name}`
      let entry = cache.entries.get(key)
      if (!entry || now() - entry.created > ttlMs) {
        const controller = new AbortController()
        const pending = { controller, scope: projectScope }
        controllers.add(pending)
        let lookup, requested = false, definitionRequested = false
        const task = async () => {
          let declaration
          const previousLookup = hover?.businessLookup
          if (previousLookup?.projectId === context.projectId && previousLookup.tree === (context.tree || 'main') && previousLookup.name === target.name
            && typeof previousLookup.path === 'string' && typeof previousLookup.uri === 'string'
            && Number.isInteger(previousLookup.line) && previousLookup.line > 0 && Number.isInteger(previousLookup.column) && previousLookup.column > 0) {
            declaration = { path: previousLookup.path, uri: previousLookup.uri, line: previousLookup.line, column: previousLookup.column }
          } else if (target.declaration) declaration = { path: context.path, uri: context.uri,
            line: target.declaration.lineNumber, column: target.declaration.column }
          else if (!hover?.skipBusinessDefinition && context.client?.ready && context.client.capabilities?.definitionProvider) {
            definitionRequested = true
            const result = await context.client.definition(context.uri, position, token)
            if (!current()) return null
            const location = oneDeclaration(result)
            const path = location && context.pathForUri(location.uri)
            if (path) declaration = { ...location, path }
          }
          if (!declaration || !current() || controller.signal.aborted) return null
          const text = context.textForUri?.(declaration.uri)
          const payload = { tree: context.tree || 'main', path: declaration.path, line: declaration.line,
            column: declaration.column, name: target.name, ...(typeof text === 'string' ? { text } : {}) }
          lookup = { ...declaration, projectId: context.projectId, tree: context.tree || 'main', name: target.name }
          requested = true
          const record = await resolve(context.projectId, payload, { signal: controller.signal })
          return record?.entity && record.business ? { record, lookup }
            : { error: '当前符号尚未与项目索引绑定，等待解析。', lookup }
        }
        // A stalled optional lookup must not leave the hover waiting forever.
        let timer
        const promise = Promise.race([Promise.resolve().then(task).catch(error => requested
          ? { error: /404|NotFound|Not Found|后端没有这个.*路由/i.test(error?.message || '')
            ? '功能描述接口不可用，请重启后端后重试。' : '暂时无法读取，重新悬停可重试。', lookup }
          : definitionRequested ? { error: '暂时无法定位此符号的项目声明，重新悬停可重试。' } : null),
          new Promise(done => { timer = setTimeout(() => { controller.abort(); done(requested || target.declaration
            ? { error: '读取超时，重新悬停可重试。', lookup }
            : definitionRequested ? { error: '定位项目声明超时，重新悬停可重试。' } : null) }, timeoutMs) })])
          .finally(() => { clearTimeout(timer); controllers.delete(pending) })
        entry = { created: now(), promise }; cache.entries.set(key, entry)
        if (cache.entries.size > 128) cache.entries.delete(cache.entries.keys().next().value)
      }
      const result = await entry.promise
      if (!result || result.error || !current()) cache.entries.delete(key)
      if (!current() || !result) return hover
      return result.error ? unavailableFunction(hover, result.error, result.lookup)
        : { ...appendBusinessMeaning(hover, result.record), businessLookup: result.lookup }
    },
    dispose() { disposed = true; invalidate(); unsubscribe() },
  }
}
