<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, request } from '../../api.js'
import { useDialog } from '../../composables/useDialog.js'
import UiIcon from '../ui/UiIcon.vue'
import { addDiscoveredModels, localModelEndpoint, MODEL_PROVIDER_PRESETS, modelProviderDraft, modelProviderPayload, providerLabel } from '../../lib/modelProviders.js'

const props = defineProps({ readonly: Boolean, visible: { type: Boolean, default: true }, focusAnchor: { type: String, default: '' } })
const rootEl = ref(null), cfg = ref(null), profiles = ref([]), loading = ref(false), loaded = ref(false)
const error = ref(''), notice = ref(''), editError = ref(''), pending = ref(''), expanded = ref('')
const pendingActivation = ref('')
const drafts = reactive(new Map()), advanced = reactive(new Map())
const adding = ref(false), addMode = ref('third'), preset = ref('deepseek')
const discovered = ref([]), discoverOpen = ref(false), discoverSearch = ref(''), selected = ref(new Set()), probeError = ref(''), probeButton = ref(null), discoverInput = ref(null)
const dialog = useDialog()
let disposed = false, loadFlight = null, probeGeneration = 0

const activeProfile = computed(() => profiles.value.find(row => row.active))
const rows = computed(() => [
  ...(!activeProfile.value && cfg.value ? [{ ...cfg.value, id: '__current', name: `${providerLabel(cfg.value.provider)} 当前连接`, active: true, current: true }] : []),
  ...profiles.value,
])
const currentSource = computed(() => rows.value.find(row => row.id === expanded.value))
const draft = computed(() => drafts.get(expanded.value))
const sourceKeySet = computed(() => !!currentSource.value?.api_key_set)
const localDraft = computed(() => localModelEndpoint(draft.value?.base_url || ''))
const busy = computed(() => !!pending.value)
const addKey = computed(() => `__new-${addMode.value}`)
const candidates = computed(() => discovered.value.filter(row => `${row.id} ${row.name || ''}`.toLowerCase().includes(discoverSearch.value.trim().toLowerCase())))
const currentIds = computed(() => new Set((draft.value?.models || []).map(row => row.id.trim()).filter(Boolean)))
const defaultModels = computed(() => (draft.value?.models || []).filter(row => row.id.trim()))
const activeName = computed(() => activeProfile.value?.name || (cfg.value ? providerLabel(cfg.value.provider) : '尚未配置'))
const activeModel = computed(() => cfg.value?.active_model || cfg.value?.model_default || '未选择模型')

async function reload() {
  if (loadFlight) return loadFlight
  loading.value = true; error.value = ''
  loadFlight = Promise.all([api.modelSettings(), request('GET', '/api/v1/model-profiles')]).then(([settings, saved]) => {
    if (disposed) return
    cfg.value = settings; profiles.value = saved || []
    if (!loaded.value) {
      loaded.value = true
      const target = rows.value.find(row => row.active) || rows.value[0]
      if (target) openRow(target)
    }
  }).catch(cause => { if (!disposed) error.value = cause.message || '无法读取模型提供商' })
    .finally(() => { loading.value = false; loadFlight = null })
  return loadFlight
}

function resetDiscovery() { probeGeneration++; discovered.value = []; discoverOpen.value = false; discoverSearch.value = ''; selected.value = new Set(); probeError.value = '' }
function openRow(row) {
  if (busy.value) return
  if (expanded.value === row.id) { expanded.value = ''; return }
  adding.value = false
  if (!drafts.has(row.id)) drafts.set(row.id, modelProviderDraft(row))
  expanded.value = row.id; editError.value = ''; resetDiscovery()
}
function startAdd() {
  if (busy.value) return
  adding.value = !adding.value
  if (!adding.value) { expanded.value = ''; return }
  openAddMode(addMode.value)
}
function openAddMode(mode) {
  if (busy.value) return
  addMode.value = mode
  const key = `__new-${mode}`
  if (!drafts.has(key)) {
    drafts.set(key, modelProviderDraft(mode === 'third' ? MODEL_PROVIDER_PRESETS.find(row => row.id === preset.value) : { name: '', provider: 'openai_compat' }))
    advanced.set(key, true)
  }
  expanded.value = key; editError.value = ''; resetDiscovery()
}
function changePreset() {
  const previous = drafts.get('__new-third')
  const next = modelProviderDraft(MODEL_PROVIDER_PRESETS.find(row => row.id === preset.value))
  // Keep a typed credential only when staying on the same route family.
  if (previous?.provider === next.provider && previous.base_url === next.base_url) next.api_key = previous.api_key
  drafts.set('__new-third', next); resetDiscovery()
}
function cancelEdit() {
  if (busy.value) return
  if (draft.value) draft.value.api_key = ''
  drafts.delete(expanded.value); expanded.value = ''; adding.value = false; editError.value = ''; resetDiscovery()
}
function modelIdChanged(index, value) {
  const previous = draft.value.models[index].id
  draft.value.models[index].id = value
  if (!draft.value.model_default || draft.value.model_default === previous) draft.value.model_default = value
}
function removeModel(index) {
  const removed = draft.value.models.splice(index, 1)[0]
  if (draft.value.model_default === removed.id) draft.value.model_default = draft.value.models[0]?.id || ''
}
function setSelected(id, checked) { const next = new Set(selected.value); checked ? next.add(id) : next.delete(id); selected.value = next }
function selectVisible() { const next = new Set(selected.value); candidates.value.forEach(row => { if (!currentIds.value.has(row.id)) next.add(row.id) }); selected.value = next }
async function closeDiscovery() { discoverOpen.value = false; await nextTick(); probeButton.value?.focus() }
function adoptSelected() {
  draft.value.models = addDiscoveredModels(draft.value.models, discovered.value, selected.value)
  if (!draft.value.model_default) draft.value.model_default = draft.value.models[0]?.id || ''
  closeDiscovery()
}

async function probeModels() {
  if (busy.value || !draft.value || props.readonly) return
  probeError.value = ''; discovered.value = []; discoverOpen.value = false
  const key = expanded.value, ticket = ++probeGeneration
  const base_url = draft.value.base_url.trim() || ({ deepseek: 'https://api.deepseek.com', openai: 'https://api.openai.com/v1' }[draft.value.provider] || '')
  try {
    const endpoint = new URL(base_url)
    if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw Error('请先填写有效的 HTTP 或 HTTPS 服务地址')
    if (!localModelEndpoint(base_url) && !draft.value.api_key.trim() && !sourceKeySet.value) throw Error('请填写 API 密钥，或选择已有密钥的提供商')
  } catch (cause) { probeError.value = cause.message || '服务地址无效'; return }
  pending.value = 'probe'
  const signature = JSON.stringify([draft.value.provider, draft.value.base_url, draft.value.api_key, draft.value.proxy_mode])
  try {
    const body = { base_url, proxy_mode: draft.value.proxy_mode,
      ...(draft.value.api_key.trim() ? { api_key: draft.value.api_key.trim() } : {}),
      ...(currentSource.value?.current ? { use_current: true } : currentSource.value ? { profile_id: currentSource.value.id } : {}) }
    const result = await request('POST', '/api/v1/model-profiles/probe', body)
    if (disposed || ticket !== probeGeneration || expanded.value !== key || signature !== JSON.stringify([draft.value.provider, draft.value.base_url, draft.value.api_key, draft.value.proxy_mode])) return
    discovered.value = [...new Map((result.models || []).map(row => { const model = typeof row === 'string' ? { id: row, name: row } : row; return [model.id, model] })).values()]
    selected.value = new Set(); discoverSearch.value = ''; discoverOpen.value = true
    if (!discovered.value.length) probeError.value = '服务返回的模型列表为空，可手动添加模型 ID。'
    await nextTick(); discoverInput.value?.focus()
  } catch (cause) { if (!disposed && ticket === probeGeneration && expanded.value === key) probeError.value = cause.message || '获取模型失败' }
  finally { if (ticket === probeGeneration) pending.value = '' }
}

async function save(options = {}) {
  if (busy.value || !draft.value || props.readonly) return
  editError.value = ''; notice.value = ''
  let payload
  try { payload = modelProviderPayload(draft.value) } catch (cause) { editError.value = cause.message; return }
  const key = expanded.value, source = currentSource.value
  pending.value = 'save'
  try {
    const saved = await request(source && !source.current ? 'PUT' : 'POST', source && !source.current ? `/api/v1/model-profiles/${encodeURIComponent(source.id)}` : '/api/v1/model-profiles',
      { ...payload, ...(source?.current ? { use_current: true } : {}) })
    // A formerly unprofiled current connection is adopted explicitly without
    // reading its credential. Retain the saved ID if activation needs a retry.
    if (source?.current) {
      profiles.value.push(saved); drafts.set(saved.id, { ...draft.value, api_key: '' }); drafts.delete(key); expanded.value = saved.id
      pendingActivation.value = saved.id
    } else draft.value.api_key = ''
    if (source?.current || options.activate === true || pendingActivation.value === saved.id) {
      await request('POST', `/api/v1/model-profiles/${encodeURIComponent(saved.id)}/activate`)
      pendingActivation.value = ''
    }
    drafts.delete(key); drafts.delete(saved.id); expanded.value = ''; adding.value = false; resetDiscovery()
    await reload()
    notice.value = saved.active || source?.current || options.activate === true ? '提供商已保存，全局默认配置已更新。已单独选择的会话模型保持不变。' : '提供商已保存，可在对话输入框选择其中的模型。'
  } catch (cause) { editError.value = pendingActivation.value && pendingActivation.value === expanded.value ? `连接已保存，设为全局默认失败。请重试应用：${cause.message || '连接配置未能应用'}` : cause.message || '保存提供商失败，草稿已保留。' }
  finally { pending.value = '' }
}
async function makeDefault(row) {
  if (busy.value || props.readonly) return
  pending.value = 'activate'; error.value = ''; notice.value = ''
  try {
    await request('POST', `/api/v1/model-profiles/${encodeURIComponent(row.id)}/activate`)
    if (pendingActivation.value === row.id) pendingActivation.value = ''
    await reload(); notice.value = `已将「${row.name}」设为全局默认，供跟随默认的会话和后台功能描述使用。`
  } catch (cause) { error.value = cause.message || '默认提供商切换失败' }
  finally { pending.value = '' }
}
async function deleteProvider(row) {
  if (busy.value || props.readonly) return
  const { id, name } = row
  return dialog.open({ title: '删除模型提供商', description: '删除保存的提供商和模型列表；单独选择它的会话将提示不可用。当前全局默认连接如已生效会保留。', details: name,
    icon: 'server', tone: 'danger', confirmText: '删除提供商', pendingText: '正在删除…', onConfirm: async () => {
      pending.value = 'delete'
      try { await request('DELETE', `/api/v1/model-profiles/${encodeURIComponent(id)}`); drafts.delete(id); if (expanded.value === id) expanded.value = ''; if (pendingActivation.value === id) pendingActivation.value = ''; await reload(); notice.value = `已删除「${name}」。` }
      finally { pending.value = '' }
    } })
}
async function focusField(anchor) {
  if (!anchor || !loaded.value) return
  const row = rows.value.find(item => item.active) || rows.value[0]
  if (row && expanded.value !== row.id) openRow(row)
  if (row) advanced.set(row.id, true)
  await nextTick()
  const target = rootEl.value?.querySelector(`[id="${anchor}"]`)
  target?.scrollIntoView?.({ block: 'center' }); target?.focus?.({ preventScroll: true })
}
watch(() => props.focusAnchor, anchor => focusField(anchor))
watch(() => props.visible, (visible, previous) => { if (visible && previous === false) reload() })
onMounted(async () => { await reload(); await focusField(props.focusAnchor) })
onBeforeUnmount(() => { disposed = true; probeGeneration++; drafts.forEach(item => { item.api_key = '' }) })
defineExpose({ reload, focusField })
</script>

<template>
  <div ref="rootEl" class="model-providers">
    <div class="model-default-summary"><span class="default-icon"><UiIcon name="server" :size="18" /></span><div><strong>全局默认 · {{ activeName }}</strong><span class="mono">{{ activeModel }}</span></div><span class="default-label">默认会话 · 后台描述</span></div>
    <p class="model-scope">这里管理连接和模型列表。对话输入框可单独选择模型；跟随默认的会话和后台描述使用全局默认，已单独选择的模型保持不变。</p>
    <div v-if="error" class="model-feedback error" role="alert"><span>{{ error }}</span><button class="small" :disabled="busy || loading" @click="reload">重试</button></div>
    <p v-if="notice" class="model-feedback success" role="status"><UiIcon name="check" :size="15" />{{ notice }}</p>
    <div v-if="loading && !loaded" class="model-loading" role="status">正在读取模型提供商…</div>
    <div id="saved-profiles" class="provider-list">
      <article v-for="row in rows" :key="row.id" class="provider-row" :class="{ expanded: expanded === row.id }" :data-provider-id="row.id">
        <button class="provider-row-trigger" :disabled="busy" :aria-expanded="expanded === row.id" :aria-controls="`provider-editor-${row.id}`" @click="openRow(row)">
          <span class="provider-mark"><UiIcon name="server" :size="18" /></span><span class="provider-title"><strong>{{ row.name }}</strong><span>{{ providerLabel(row.provider) }} · {{ row.models?.length || (row.model_default || row.active_model ? 1 : 0) }} 个模型</span></span>
          <span v-if="row.active" class="global-badge">全局默认</span><span class="provider-key-status" :class="{ configured: row.api_key_set || row.local || row.provider === 'offline' }"><span class="key-dot" />{{ row.provider === 'offline' ? '离线' : row.api_key_set ? '密钥已配置' : row.local || localModelEndpoint(row.base_url) ? '本地连接' : '待配置' }}</span><UiIcon :name="expanded === row.id ? 'chevron-down' : 'chevron-right'" :size="16" />
        </button>
        <div v-if="expanded === row.id" :id="`provider-editor-${row.id}`" class="provider-editor-host"><!-- shared editor teleports here --></div>
        <div v-if="expanded !== row.id && !readonly" class="provider-row-actions"><button v-if="!row.active" class="ghost small" :disabled="busy" @click="makeDefault(row)">设为全局默认</button></div>
      </article>
    </div>
    <div v-if="!readonly" class="provider-add"><button class="add-provider" :disabled="busy" :aria-expanded="adding" @click="startAdd"><UiIcon :name="adding ? 'close' : 'plus'" :size="17" />{{ adding ? '关闭新增提供商' : '添加模型提供商' }}</button></div>
    <section v-if="adding" class="new-provider-card" aria-label="添加模型提供商">
      <div class="add-methods" role="tablist" aria-label="添加方式"><button id="third-provider-tab" role="tab" :aria-selected="addMode === 'third'" aria-controls="new-provider-editor" :disabled="busy" @click="openAddMode('third')">第三方模型提供商</button><button id="custom-provider-tab" role="tab" :aria-selected="addMode === 'custom'" aria-controls="new-provider-editor" :disabled="busy" @click="openAddMode('custom')">自定义模型 API</button></div>
      <p class="add-method-hint">{{ addMode === 'third' ? '选择已有提供商，配置 API 密钥和可用模型。' : '连接中转站、自部署服务或其他兼容 OpenAI Chat Completions 的 API。' }}</p>
      <label v-if="addMode === 'third'" class="preset-field">提供商<select v-model="preset" :disabled="busy" @change="changePreset"><option v-for="item in MODEL_PROVIDER_PRESETS" :key="item.id" :value="item.id">{{ item.name }}</option></select></label>
      <div id="new-provider-editor" role="tabpanel" :aria-labelledby="addMode === 'third' ? 'third-provider-tab' : 'custom-provider-tab'"></div>
    </section>
    <Teleport v-if="draft && (adding || currentSource)" :to="adding ? '#new-provider-editor' : `#provider-editor-${expanded}`" defer>
      <form class="provider-editor" novalidate @submit.prevent="save">
        <fieldset :disabled="busy || readonly">
          <div v-if="adding" class="provider-field"><label for="provider-name">提供商名称</label><input id="provider-name" v-model="draft.name" maxlength="40" autocomplete="off" placeholder="例如：团队模型网关" /></div>
          <div v-if="draft.provider !== 'offline'" class="provider-field credential-field"><label for="provider-api-key">API 密钥 <span v-if="sourceKeySet" class="stored-key">已配置</span></label><input id="provider-api-key" v-model="draft.api_key" type="password" autocomplete="new-password" name="code-agent-provider-api-key" autocapitalize="off" spellcheck="false" :placeholder="sourceKeySet ? '留空保留已保存的密钥' : localDraft ? '本地服务可留空' : '输入 API 密钥'" /><p>密钥只写入服务端，页面不会读取或回显已保存的值。</p></div>
          <p v-else class="provider-hint">离线模式不调用模型服务，也无需 API 密钥。</p>
          <details class="custom-provider-settings" :open="advanced.get(expanded)" @toggle="advanced.set(expanded, $event.target.open)"><summary>自定义设置 <span>服务地址、模型列表与容量</span></summary>
            <div class="provider-advanced">
              <div v-if="!adding" class="provider-field"><label for="provider-name">提供商名称</label><input id="provider-name" v-model="draft.name" maxlength="40" autocomplete="off" /></div>
              <div id="model-provider" class="provider-protocol" tabindex="-1"><span>API 协议</span><strong>{{ draft.provider === 'offline' ? '离线演示' : 'OpenAI Chat Completions' }}</strong><span>{{ providerLabel(draft.provider) }}</span></div>
              <div class="provider-field"><label for="provider-base-url">服务地址 <span>Base URL</span></label><input id="provider-base-url" v-model="draft.base_url" type="url" autocomplete="off" autocapitalize="off" spellcheck="false" :placeholder="draft.provider === 'deepseek' ? 'https://api.deepseek.com' : 'https://gateway.example/v1'" /></div>
              <div class="catalog-heading"><strong>模型选项 <span>{{ draft.models.length }}</span></strong><button ref="probeButton" type="button" class="small" :disabled="busy || readonly || draft.provider === 'offline'" @click="probeModels"><UiIcon name="refresh" :size="14" />{{ pending === 'probe' ? '正在获取…' : '获取可用模型' }}</button></div>
              <p v-if="probeError" class="provider-error" role="alert">{{ probeError }}</p>
              <section v-if="discoverOpen" class="discovered-models" role="region" aria-label="可用模型" @keydown.esc.stop.prevent="closeDiscovery">
                <header><strong>选择要添加的模型</strong><button type="button" class="ghost small" aria-label="关闭可用模型" @click="closeDiscovery"><UiIcon name="close" :size="15" /></button></header>
                <input ref="discoverInput" v-model="discoverSearch" aria-label="搜索可用模型" type="search" placeholder="搜索模型 ID 或名称" />
                <div class="discovery-actions"><button type="button" class="ghost small" @click="selectVisible">全选当前结果</button><button type="button" class="ghost small" @click="selected = new Set()">取消全选</button><span>已选 {{ selected.size }}</span></div>
                <div class="discovery-list"><label v-for="model in candidates" :key="model.id" :title="model.name || model.id"><input type="checkbox" :checked="selected.has(model.id) || currentIds.has(model.id)" :disabled="currentIds.has(model.id)" @change="setSelected(model.id, $event.target.checked)" /><code>{{ model.id }}</code><span v-if="currentIds.has(model.id)">已添加</span></label><p v-if="!candidates.length" class="provider-hint">没有匹配的模型</p></div>
                <footer><button type="button" class="primary small" :disabled="!selected.size" @click="adoptSelected">添加所选{{ selected.size ? `（${selected.size}）` : '' }}</button></footer>
              </section>
              <div class="editable-models"><div v-for="(model,index) in draft.models" :key="index" class="editable-model-row"><div class="provider-field"><label :for="`model-id-${index}`">模型 ID</label><input :id="`model-id-${index}`" class="mono" :value="model.id" :aria-label="`模型 ID ${index + 1}`" autocomplete="off" spellcheck="false" placeholder="原始模型 ID" @input="modelIdChanged(index, $event.target.value)" /></div><div class="provider-field"><label :for="`model-name-${index}`">显示名称 <span>可选</span></label><input :id="`model-name-${index}`" v-model="model.name" :aria-label="`模型显示名称 ${index + 1}`" autocomplete="off" placeholder="留空使用 ID" /></div><button type="button" class="ghost small remove-model" :aria-label="`移除模型 ${index + 1}`" @click="removeModel(index)"><UiIcon name="trash" :size="16" /></button></div></div>
              <button class="ghost small add-model-row" type="button" @click="draft.models.push({id:'',name:''})"><UiIcon name="plus" :size="15" />手动添加模型</button>
              <div class="provider-field"><label for="default-model">此提供商的默认模型</label><select id="default-model" v-model="draft.model_default"><option value="" disabled>选择默认模型</option><option v-for="model in defaultModels" :key="model.id" :value="model.id">{{ model.name || model.id }}</option></select><p>设为全局默认提供商时，新会话和后台功能描述使用此模型。</p></div>
              <div class="capacity-heading">连接容量覆盖 <span>留空由模型自动设置</span></div><div class="provider-capacity"><div class="provider-field"><label for="context-window">上下文窗口 <span>Token</span></label><input id="context-window" v-model="draft.context_window" inputmode="numeric" autocomplete="off" placeholder="自动" /></div><div class="provider-field"><label for="max-output-tokens">最大输出 Token</label><input id="max-output-tokens" v-model="draft.max_output_tokens" inputmode="numeric" autocomplete="off" placeholder="自动" /></div></div><p class="provider-hint">容量覆盖对该连接生效，最大输出需小于上下文窗口。</p>
              <div class="provider-field"><label for="provider-proxy-mode">网络代理</label><select id="provider-proxy-mode" v-model="draft.proxy_mode"><option value="system">跟随系统</option><option value="direct">直接连接</option></select></div>
            </div>
          </details>
        </fieldset>
        <p v-if="editError" class="provider-error" role="alert">{{ editError }}</p>
        <footer v-if="!readonly" class="provider-editor-actions"><button v-if="currentSource && !currentSource.current" type="button" class="ghost small delete-provider" :disabled="busy" @click="deleteProvider(currentSource)"><UiIcon name="trash" :size="14" />删除提供商</button><button v-if="currentSource && !currentSource.active && !currentSource.current" type="button" class="ghost small" :disabled="busy" @click="save({activate:true})">应用并设为全局默认</button><span class="spacer" /><button type="button" class="ghost small" :disabled="busy" @click="cancelEdit">取消</button><button class="primary small" type="submit" :disabled="busy">{{ pending === 'save' ? '正在应用…' : '应用' }}</button></footer>
      </form>
    </Teleport>
    <p v-if="readonly" class="provider-hint">当前仅可查看模型连接，管理员可编辑提供商和模型。</p>
    <p class="provider-footnote">“密钥已配置”表示服务端保存了凭据；连接状态以实际请求结果为准。</p>
  </div>
</template>

<style scoped>
.model-providers { min-width: 0; }.model-default-summary { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; min-width: 0; }.default-icon { display: grid; place-items: center; width: 36px; height: 36px; flex: none; background: var(--accent-soft); color: var(--accent); border-radius: 8px; }.model-default-summary > div { display: flex; flex: 1; flex-direction: column; gap: 4px; min-width: 0; }.model-default-summary strong { font-size: 13px; font-weight: 600; }.model-default-summary .mono { font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; }.default-label { font-size: 10px; color: var(--muted); white-space: nowrap; }
.model-scope,.provider-footnote { margin: 0 0 22px; font-size: 12px; line-height: 1.7; color: var(--muted); }.provider-footnote { margin-top: 18px; font-size: 11px; }.provider-list { border-top: 1px solid var(--border); }.provider-row { border-bottom: 1px solid var(--border); }.provider-row.expanded { background: var(--panel-soft); border: 1px solid var(--border); border-radius: 9px; margin: 7px 0; overflow: hidden; }.provider-row-trigger { display: flex; align-items: center; width: 100%; min-height: 74px; gap: 12px; background: transparent; padding: 15px 12px; border: 0; border-radius: 0; text-align: left; white-space: normal; }.provider-mark { color: var(--muted); display: grid; place-items: center; width: 28px; flex: none; }.provider-title { display: flex; flex: 1; flex-direction: column; gap: 5px; min-width: 0; }.provider-title strong { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }.provider-title > span { font-size: 11px; color: var(--muted); }.global-badge { font-size: 10px; color: var(--accent); background: var(--accent-soft); padding: 3px 6px; border-radius: 4px; white-space: nowrap; }.provider-key-status { font-size: 11px; display: inline-flex; align-items: center; gap: 6px; color: var(--muted); white-space: nowrap; }.key-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--warn); }.configured .key-dot { background: var(--ok); }.provider-row-actions { display: flex; justify-content: flex-end; margin: -8px 32px 9px 0; }.provider-row-actions:empty { display: none; }.provider-row-actions button { font-size: 10px; }
.provider-editor { padding: 0 20px 18px 52px; }.provider-editor fieldset { border: 0; margin: 0; padding: 0; min-width: 0; }.provider-field { display: flex; flex-direction: column; gap: 7px; min-width: 0; }.provider-field > label { font-size: 12px; color: var(--text-2); font-weight: 500; }.provider-field > label > span,.provider-field p { color: var(--muted); font-size: 10px; font-weight: 400; }.provider-field label .stored-key { color: var(--ok); margin-left: 8px; }.provider-field input,.provider-field select { width: 100%; min-width: 0; min-height: 36px; padding: 7px 10px; font-size: 12px; background: var(--panel); }.provider-field p { margin: 0; line-height: 1.6; }.credential-field { margin-bottom: 18px; }.custom-provider-settings { margin-top: 17px; }.custom-provider-settings > summary { cursor: pointer; font-size: 12px; color: var(--text-2); padding: 8px 0; }.custom-provider-settings > summary > span { font-size: 10px; color: var(--muted); margin-left: 8px; }.provider-advanced { display: flex; flex-direction: column; gap: 17px; padding-top: 14px; }.provider-protocol { display: flex; gap: 10px; flex-wrap: wrap; font-size: 11px; color: var(--muted); }.provider-protocol strong { font-weight: 500; color: var(--text-2); }.catalog-heading { display: flex; align-items: center; gap: 10px; justify-content: space-between; border-top: 1px solid var(--border); padding-top: 18px; font-size: 12px; }.catalog-heading strong { font-weight: 500; }.catalog-heading strong span { font-size: 10px; color: var(--muted); margin-left: 5px; }.editable-models { display: flex; flex-direction: column; gap: 13px; }.editable-model-row { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr) auto; gap: 10px; align-items: end; }.editable-model-row .remove-model { height: 36px; color: var(--muted); padding: 0 6px; }.add-model-row { align-self: flex-start; color: var(--accent); }.provider-capacity { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 14px; }.capacity-heading { margin-top: 2px; border-top: 1px solid var(--border); padding-top: 17px; font-size: 12px; }.capacity-heading > span { color: var(--muted); font-size: 10px; margin-left: 10px; }.provider-editor-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 19px; padding-top: 15px; border-top: 1px solid var(--border); }.spacer { flex: 1; }.provider-editor-actions .delete-provider { color: var(--muted); }.small { font-size: 11px; min-height: 30px; }.provider-hint { margin: 0; font-size: 11px; color: var(--muted); line-height: 1.7; }.provider-error { color: var(--err); font-size: 12px; line-height: 1.7; margin: 0; overflow-wrap: anywhere; }.model-feedback { display: flex; align-items: center; gap: 8px; padding: 11px 13px; margin-bottom: 16px; border-radius: 7px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }.model-feedback > span { flex: 1; }.model-feedback.error { color: var(--err); background: var(--err-soft); }.model-feedback.success { color: var(--ok); background: var(--ok-soft); }.model-loading { padding: 28px 0; color: var(--muted); font-size: 12px; }
.provider-add { margin-top: 18px; }.add-provider { min-height: 34px; padding: 5px 10px; font-size: 12px; color: var(--text-2); }.new-provider-card { margin-top: 14px; padding: 18px 20px; border: 1px solid var(--border); border-radius: 9px; background: var(--panel-soft); }.new-provider-card .provider-editor { padding: 0; }.add-methods { display: flex; gap: 4px; padding: 3px; border: 1px solid var(--border); border-radius: 7px; background: var(--panel); }.add-methods button { flex: 1; min-height: 32px; padding: 5px 8px; font-size: 12px; border: 0; background: transparent; }.add-methods button[aria-selected=true] { color: var(--accent); background: var(--accent-soft); }.add-method-hint { font-size: 11px; color: var(--muted); line-height: 1.7; margin: 12px 0 17px; }.preset-field { display: flex; align-items: center; gap: 16px; margin-bottom: 18px; font-size: 12px; }.preset-field select { flex: 1; min-width: 0; font-size: 12px; }
.discovered-models { border: 1px solid var(--border-strong); border-radius: 8px; background: var(--panel); padding: 12px; }.discovered-models header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; font-size: 12px; }.discovered-models > input { min-width: 0; width: 100%; font-size: 12px; }.discovery-actions { display: flex; align-items: center; gap: 5px; margin: 7px 0; }.discovery-actions > span { margin-left: auto; color: var(--muted); font-size: 10px; }.discovery-actions button { font-size: 10px; padding: 3px 4px; }.discovery-list { max-height: 220px; overflow: auto; }.discovery-list label { display: flex; align-items: center; gap: 8px; padding: 7px 2px; min-width: 0; font-size: 12px; }.discovery-list code { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.discovery-list label > span { font-size: 10px; margin-left: auto; color: var(--muted); flex: none; }.discovered-models footer { display: flex; justify-content: flex-end; padding-top: 10px; border-top: 1px solid var(--border); margin-top: 8px; }
@media(max-width:600px) { .provider-row-trigger { gap: 8px; padding: 14px 10px; }.provider-editor { padding: 0 13px 16px; }.provider-mark { display: none; }.provider-key-status { font-size: 10px; }.provider-title strong { font-size: 12px; }.global-badge { font-size: 9px; }.default-label { display: none; }.custom-provider-settings > summary > span { display: block; margin: 5px 0 0 15px; }.editable-model-row { grid-template-columns: minmax(0,1fr) auto; }.editable-model-row > .provider-field:first-child { grid-column: 1; }.editable-model-row > .provider-field:nth-child(2) { grid-column: 1; }.editable-model-row > .remove-model { grid-column: 2; grid-row: 1 / 3; align-self: center; }.new-provider-card { padding: 15px 12px; }.add-methods button { white-space: normal; font-size: 11px; }.provider-editor-actions .spacer { flex-basis: 100%; }.model-default-summary { align-items: flex-start; }.catalog-heading { flex-wrap: wrap; }.provider-capacity { gap: 10px; }.capacity-heading > span { display: block; margin: 5px 0 0; } }
@media(max-width:360px) { .provider-capacity { grid-template-columns: minmax(0,1fr); }.global-badge { display: none; }.discovery-actions { gap: 0; }.provider-key-status { max-width: 80px; } }
</style>
