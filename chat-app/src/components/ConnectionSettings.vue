<script setup>
import { nextTick, onMounted, reactive, ref } from 'vue'
import { api } from '../api.js'
import UiIcon from './ui/UiIcon.vue'

const emit = defineEmits(['close', 'saved'])
const dialog = ref(null)
const profiles = ref([])
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const status = ref('')
const activateAfterSave = ref(true)
const clearKey = ref(false)
const draft = reactive({ id: '', name: '', provider: 'openai-compatible', base_url: '', model: '', api_key: '', supports_images: false })

async function load() {
  loading.value = true
  try { profiles.value = await api.modelProfiles() }
  catch (failure) { error.value = failure.message || '无法读取模型配置' }
  finally { loading.value = false }
}

function edit(profile = null) {
  Object.assign(draft, { id: profile?.id || '', name: profile?.name || '', provider: profile?.provider || 'openai-compatible', base_url: profile?.base_url || '', model: profile?.model || '', api_key: '', supports_images: !!profile?.supports_images })
  activateAfterSave.value = true
  clearKey.value = false
  error.value = ''
  status.value = ''
}

function providerChanged() {
  if (draft.provider === 'deepseek' && !draft.id) {
    draft.base_url = 'https://api.deepseek.com/v1'
    draft.model = 'deepseek-chat'
    if (!draft.name) draft.name = 'DeepSeek'
    draft.supports_images = false
  }
}

async function perform(action, message) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  status.value = ''
  try {
    await action()
    await load()
    status.value = message
    emit('saved')
  } catch (failure) { error.value = failure.message || '保存失败，请重试' }
  finally { busy.value = false }
}

async function save() {
  await perform(async () => {
    const saved = await api.saveModelProfile({ ...draft, ...(clearKey.value ? { clear_key: true } : {}) })
    if (activateAfterSave.value) await api.activateModelProfile(saved.id)
    edit(saved)
  }, activateAfterSave.value ? '配置已保存，下一条消息将使用这个模型。' : '配置已保存。')
}

async function activate(profile) {
  await perform(() => api.activateModelProfile(profile.id), profile.provider === 'offline' ? '已切换为本地演示，不会向模型服务发送消息。' : '模型已切换，下一条消息起生效。')
}

async function remove(profile) {
  await perform(async () => {
    await api.deleteModelProfile(profile.id)
    if (draft.id === profile.id) edit()
  }, '配置已删除。删除当前配置时会切换回本地演示。')
}

onMounted(async () => {
  await nextTick()
  dialog.value?.showModal()
  await load()
})
</script>

<template>
  <dialog ref="dialog" class="connection-settings" aria-labelledby="connection-settings-title" @cancel.prevent="emit('close')" @click="event => { if (event.target === dialog) emit('close') }">
    <header><div><h1 id="connection-settings-title">模型连接</h1><p>对话应用使用自己的配置、密钥与数据目录。</p></div><button class="icon-btn" aria-label="关闭模型连接设置" @click="emit('close')"><UiIcon name="close" /></button></header>
    <div class="settings-body">
      <section aria-label="已有模型配置">
        <h2>配置档案</h2>
        <p v-if="loading && !profiles.length" class="muted" aria-live="polite">正在读取配置…</p>
        <div v-for="profile in profiles" :key="profile.id" class="profile-row">
          <div class="profile-description"><strong>{{ profile.name }}</strong><span>{{ profile.model }}</span><small>{{ profile.provider === 'offline' ? '本地演示 · 无 AI · 不联网' : (profile.readonly ? '环境变量配置' : (profile.has_key ? '密钥已保存' : '未配置密钥，可用于无密钥的本地服务')) }}</small></div>
          <div class="profile-actions"><span v-if="profile.active" class="active-badge">当前</span><button v-else :disabled="busy" @click="activate(profile)">使用</button><template v-if="!profile.readonly"><button :disabled="busy" @click="edit(profile)">编辑</button><button :disabled="busy" :aria-label="`删除配置 ${profile.name}`" @click="remove(profile)">删除</button></template></div>
        </div>
        <button class="add-profile" :disabled="busy" @click="edit()"><UiIcon name="plus" :size="15" />新建模型配置</button>
      </section>
      <form @submit.prevent="save">
        <h2>{{ draft.id ? '编辑连接' : '新建连接' }}</h2>
        <label for="connection-name">配置名称<input id="connection-name" v-model="draft.name" required maxlength="100" placeholder="例如：我的对话模型" :disabled="busy" /></label>
        <label for="connection-provider">服务类型<select id="connection-provider" v-model="draft.provider" :disabled="busy" @change="providerChanged"><option value="openai-compatible">OpenAI 兼容服务</option><option value="deepseek">DeepSeek</option></select></label>
        <label for="connection-base">API 地址<input id="connection-base" v-model="draft.base_url" type="url" required placeholder="https://example.com/v1" :disabled="busy" /><small>填写 API 根地址，服务需支持 /chat/completions 流式接口。</small></label>
        <label for="connection-model">模型名称<input id="connection-model" v-model="draft.model" required maxlength="200" placeholder="服务提供的模型 ID" :disabled="busy" /></label>
        <label for="connection-key">API 密钥<input id="connection-key" v-model="draft.api_key" type="password" autocomplete="new-password" :placeholder="draft.id ? '留空保留已保存的密钥' : '本地无密钥服务可留空'" :disabled="busy || clearKey" /><small>密钥仅保存在此对话后端，不会显示在配置列表中。</small></label>
        <label v-if="draft.id" class="check-field"><input v-model="clearKey" type="checkbox" :disabled="busy" />删除已保存的密钥</label>
        <label class="check-field"><input v-model="draft.supports_images" type="checkbox" :disabled="busy" />此模型支持图片输入</label>
        <label class="check-field"><input v-model="activateAfterSave" type="checkbox" :disabled="busy" />保存后立即使用</label>
        <p class="muted">保存设置不会请求模型服务，消息会在发送时交给所选服务。</p>
        <button class="primary save-button" :disabled="busy || loading">{{ busy ? '正在保存…' : '保存连接' }}</button>
      </form>
      <p v-if="error" class="feedback error" role="alert">{{ error }}</p>
      <p v-if="status" class="feedback success" role="status">{{ status }}</p>
    </div>
  </dialog>
</template>

<style scoped>
.connection-settings { color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 0; width: min(700px, calc(100vw - 28px)); max-height: min(820px, calc(100dvh - 32px)); box-shadow: var(--shadow-pop); }
.connection-settings::backdrop { background: rgba(15, 23, 42, .35); }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 22px 24px 18px; border-bottom: 1px solid var(--border); }
h1 { margin: 0; font-size: 19px; }
header p, .muted { color: var(--muted); font-size: 12px; line-height: 1.7; margin: 8px 0 0; }
.settings-body { padding: 20px 24px 24px; overflow: auto; max-height: calc(100dvh - 160px); }
h2 { font-size: 14px; margin: 0 0 14px; }
.profile-row { display: flex; align-items: center; justify-content: space-between; gap: 14px; border: 1px solid var(--border); border-radius: 10px; padding: 12px; margin-bottom: 8px; }
.profile-description { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.profile-description strong { font-size: 13px; }
.profile-description span { font-size: 12px; overflow-wrap: anywhere; }
.profile-description small { color: var(--muted); font-size: 11px; }
.profile-actions { display: flex; align-items: center; flex-wrap: wrap; justify-content: flex-end; gap: 6px; flex-shrink: 0; }
.profile-actions button { padding: 4px 7px; font-size: 12px; }
.active-badge { color: var(--accent); background: var(--accent-soft); border-radius: 5px; padding: 3px 7px; font-size: 11px; }
.add-profile { display: flex; align-items: center; gap: 6px; font-size: 12px; margin-top: 10px; }
form { margin-top: 24px; border-top: 1px solid var(--border); padding-top: 20px; }
form label { display: flex; flex-direction: column; gap: 7px; font-size: 13px; margin-bottom: 14px; }
form label small { color: var(--muted); font-size: 11px; line-height: 1.6; }
form input, form select { width: 100%; box-sizing: border-box; }
form .check-field { flex-direction: row; align-items: center; gap: 8px; font-size: 12px; }
form input[type=checkbox] { width: auto; }
.save-button { margin-top: 16px; }
.feedback { font-size: 12px; margin: 16px 0 0; line-height: 1.7; }
.error { color: var(--err); }.success { color: var(--accent); }
@media(max-width: 480px) { header { padding: 18px 16px 14px; }.settings-body { padding: 16px; }.profile-row { align-items: flex-start; gap: 10px; }.profile-actions { max-width: 110px; } }
</style>
