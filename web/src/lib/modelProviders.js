export const MODEL_PROVIDER_PRESETS = [
  { id: 'deepseek', name: 'DeepSeek', provider: 'deepseek', base_url: 'https://api.deepseek.com' },
  { id: 'openai', name: 'OpenAI', provider: 'openai', base_url: 'https://api.openai.com/v1' },
  { id: 'kimi', name: 'Kimi', provider: 'openai_compat', base_url: 'https://api.moonshot.cn/v1' },
  { id: 'ollama', name: 'Ollama', provider: 'openai_compat', base_url: 'http://127.0.0.1:11434/v1' },
  { id: 'lmstudio', name: 'LM Studio', provider: 'openai_compat', base_url: 'http://127.0.0.1:1234/v1' },
]
export const providerLabel = provider => ({ deepseek: 'DeepSeek', openai: 'OpenAI', openai_compat: 'OpenAI 兼容', offline: '离线模式' }[provider] || provider || '自定义 API')

export function localModelEndpoint(value) {
  let host
  try { host = new URL(value).hostname.toLowerCase().replace(/^\[|\]$/g, '') } catch { return false }
  const ipv4 = /^\d+\.\d+\.\d+\.\d+$/.test(host) && host.split('.').every(part => Number(part) <= 255)
  return ['localhost', '::1', '0.0.0.0', 'host.docker.internal'].includes(host) || /\.(local|localhost|internal)$/.test(host)
    || ipv4 && (/^(127\.|10\.|192\.168\.|169\.254\.)/.test(host) || /^172\.(1[6-9]|2\d|3[01])\./.test(host))
}

export function modelProviderDraft(source = {}) {
  const defaultModel = source.model_default || source.active_model || ''
  const models = (source.models || []).map(row => ({ ...row, id: String(row.id || ''), name: String(row.name || '') }))
  if (defaultModel && !models.some(row => row.id === defaultModel)) models.unshift({ id: defaultModel, name: '' })
  return { name: source.name || providerLabel(source.provider), provider: source.provider || 'openai_compat', base_url: source.base_url || '',
    api_key: '', model_default: defaultModel, models: models.length ? models : [{ id: '', name: '' }],
    context_window: source.context_window ?? '', max_output_tokens: source.max_output_tokens ?? '', proxy_mode: source.proxy_mode || 'system' }
}

export function modelCapacity(value, label) {
  if (value == null || String(value).trim() === '') return null
  const text = String(value).trim()
  if (!/^\d+$/.test(text) || !Number.isSafeInteger(Number(text)) || Number(text) < 1 || Number(text) > 10000000) throw new Error(`${label}请输入 1–10,000,000 之间的整数`)
  return Number(text)
}

export function modelProviderPayload(draft) {
  const name = String(draft.name || '').trim()
  if (!name) throw new Error('请填写提供商名称')
  const base_url = String(draft.base_url || '').trim()
  if (base_url) {
    let url
    try { url = new URL(base_url) } catch { throw new Error('服务地址需为完整的 HTTP 或 HTTPS URL') }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('服务地址需为不含账号、查询参数和片段的 HTTP 或 HTTPS URL')
  }
  const api_key = String(draft.api_key || '').trim()
  if (api_key && (!/^[\x21-\x7e]+$/.test(api_key) || /^['"]|['"]$/.test(api_key) || /^(?:export\s+)?[A-Z_][A-Z0-9_]*=/.test(api_key))) throw new Error('API 密钥只需填写密钥本身，不含引号、空格或环境变量名称')
  const seen = new Set()
  const models = draft.models.map((row, index) => {
    const id = String(row.id || '').trim(), modelName = String(row.name || '').trim()
    if (!id) throw new Error(`请填写第 ${index + 1} 个模型的 ID`)
    if (seen.has(id)) throw new Error(`模型 ID 重复：${id}`)
    seen.add(id)
    return { ...row, id, name: modelName || id }
  })
  if (!models.length) throw new Error('至少添加一个模型')
  if (models.length > 200) throw new Error('每个提供商最多保存 200 个模型')
  const model_default = String(draft.model_default || '').trim() || models[0].id
  if (!seen.has(model_default)) throw new Error('默认模型需在当前提供商的模型列表中')
  const context_window = modelCapacity(draft.context_window, '上下文窗口'), max_output_tokens = modelCapacity(draft.max_output_tokens, '最大输出 Token')
  if (context_window && max_output_tokens && max_output_tokens >= context_window) throw new Error('最大输出 Token 必须小于上下文窗口')
  return { name, provider: draft.provider, base_url, model_default, models, proxy_mode: draft.proxy_mode,
    context_window, max_output_tokens, ...(api_key ? { api_key } : {}) }
}

export function addDiscoveredModels(current, candidates, selected) {
  const ids = new Set(current.map(row => row.id.trim()).filter(Boolean))
  const rows = current.filter(row => row.id.trim() || row.name.trim()).map(row => ({ ...row }))
  for (const item of candidates) {
    const row = typeof item === 'string' ? { id: item, name: item } : item
    if (selected.has(row.id) && !ids.has(row.id)) { rows.push({ ...row }); ids.add(row.id) }
  }
  return rows.length ? rows : [{ id: '', name: '' }]
}
