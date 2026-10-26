<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../../api.js'

const props = defineProps({ user: { type: Object, default: null } })
const isAdmin = computed(() => props.user?.role === 'admin')

const settings = ref([
  {
    id: 'permission', title: '默认执行权限',
    hint: '决定新会话能够直接执行哪些操作，以及何时申请授权。',
    load: () => api.permissionSettings(), save: mode => api.savePermissionSettings(mode),
    config: null, draft: '', loading: false, saving: false, error: '', notice: '',
  },
])

function checkedConfig(value) {
  if (!value || typeof value.mode !== 'string' || !Array.isArray(value.modes) ||
      !value.modes.length || value.modes.some(mode => !mode || typeof mode.id !== 'string' || !mode.id || typeof mode.label !== 'string') ||
      !value.modes.some(mode => mode.id === value.mode)) {
    throw new Error('服务返回的设置不完整，请重试')
  }
  return value
}

function changed(setting) {
  return !!setting.config && setting.draft !== setting.config.mode
}

function selectedDescription(setting) {
  return setting.config?.modes.find(mode => mode.id === setting.draft)?.desc || ''
}

function clearFeedback(setting) {
  setting.error = ''
  setting.notice = ''
}

async function loadSetting(setting) {
  if (setting.loading || setting.saving) return
  setting.loading = true
  clearFeedback(setting)
  try {
    const config = checkedConfig(await setting.load())
    setting.config = config
    setting.draft = config.mode
  } catch (error) {
    setting.error = error?.message || '无法加载设置，请重试'
  } finally {
    setting.loading = false
  }
}

async function saveSetting(setting) {
  if (!isAdmin.value || setting.loading || setting.saving || !changed(setting)) return
  clearFeedback(setting)
  if (!setting.config.modes.some(mode => mode.id === setting.draft)) {
    setting.error = '请选择可用的设置选项'
    return
  }
  setting.saving = true
  try {
    const config = checkedConfig(await setting.save(setting.draft))
    setting.config = config
    setting.draft = config.mode
    setting.notice = '已保存，新会话生效'
  } catch (error) {
    setting.error = error?.message || '保存失败，请重试'
  } finally {
    setting.saving = false
  }
}

onMounted(() => Promise.allSettled(settings.value.map(loadSetting)))
</script>

<template>
  <section class="execution-settings" aria-labelledby="execution-settings-title">
    <header class="group-header">
      <div>
        <h3 id="execution-settings-title">新会话默认设置</h3>
        <p>新建普通会话会采用这些默认值，已有会话保留自己的设置。</p>
      </div>
      <span class="scope-label">服务端设置</span>
    </header>

    <form v-for="setting in settings" :key="setting.id" class="setting-row" novalidate @submit.prevent="saveSetting(setting)">
      <div class="setting-copy">
        <label :for="`execution-${setting.id}`">{{ setting.title }}</label>
        <p :id="`execution-${setting.id}-hint`">{{ setting.hint }}</p>
      </div>
      <div class="setting-control">
        <div v-if="setting.loading" class="loading" role="status">正在加载…</div>
        <template v-else-if="setting.config">
          <div class="control-line">
            <select :id="`execution-${setting.id}`" v-model="setting.draft" :disabled="!isAdmin || setting.saving" :aria-describedby="`execution-${setting.id}-hint execution-${setting.id}-description`" @change="clearFeedback(setting)">
              <option v-for="mode in setting.config.modes" :key="mode.id" :value="mode.id">{{ mode.label }}</option>
            </select>
            <button v-if="isAdmin" class="save-button" type="submit" :disabled="!changed(setting) || setting.saving">{{ setting.saving ? '保存中…' : '保存' }}</button>
          </div>
          <p :id="`execution-${setting.id}-description`" class="mode-description">{{ selectedDescription(setting) }}</p>
          <p v-if="changed(setting) && !setting.saving && !setting.error" class="draft-state">有未保存的更改</p>
        </template>
        <div v-if="setting.error" class="feedback error" role="alert">
          <span>{{ setting.error }}</span>
          <button v-if="!setting.config" type="button" class="small" :disabled="setting.loading" @click="loadSetting(setting)">重试</button>
        </div>
        <p v-if="setting.notice" class="feedback success" role="status">{{ setting.notice }}</p>
      </div>
    </form>

    <div class="scope-notes">
      <p><strong>Lead 的能力</strong><span>由角色单独控制，可读取、指挥与审计。选择完全权限也不会为 Lead 增加 Bash 等执行工具。</span></p>
      <p><strong>编辑器只读</strong><span>只控制你在编辑器里的手动修改，与智能体的执行权限分别设置。</span></p>
      <p v-if="!isAdmin" class="admin-note">当前账号可查看设置；修改需要管理员权限。</p>
    </div>
  </section>
</template>

<style scoped>
.execution-settings { min-width: 0; }
.group-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding-bottom: 20px; }
.group-header h3 { margin: 0; color: var(--text); font-size: 15px; font-weight: 600; }
.group-header p { margin: 6px 0 0; color: var(--muted); font-size: 12px; line-height: 1.7; }
.scope-label { flex: none; padding: 3px 7px; border: 1px solid var(--border); border-radius: 5px; color: var(--muted); font-size: 10px; white-space: nowrap; }
.setting-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, .9fr); align-items: start; gap: 32px; padding: 22px 0; border-top: 1px solid var(--border); }
.setting-copy { min-width: 0; }
.setting-copy label { display: block; color: var(--text); font-size: 13px; font-weight: 500; line-height: 1.6; }
.setting-copy p { margin: 5px 0 0; color: var(--muted); font-size: 12px; line-height: 1.7; }
.setting-control { min-width: 0; }
.control-line { display: flex; gap: 8px; }
.control-line select { flex: 1; min-width: 0; min-height: 34px; width: 100%; background: var(--panel); font-size: 12px; }
.save-button { min-height: 34px; min-width: 56px; flex: none; font-size: 12px; }
.mode-description, .draft-state, .loading { margin: 7px 0 0; color: var(--muted); font-size: 11px; line-height: 1.7; overflow-wrap: anywhere; }
.loading { margin-top: 5px; }
.draft-state { color: var(--accent); }
.feedback { display: flex; align-items: center; gap: 8px; margin: 8px 0 0; font-size: 12px; line-height: 1.7; }
.feedback span { flex: 1; overflow-wrap: anywhere; }
.feedback.error { color: var(--err); }
.feedback.success { color: var(--ok); }
.scope-notes { padding: 16px 0 0; border-top: 1px solid var(--border); }
.scope-notes p { display: flex; align-items: baseline; gap: 12px; margin: 0 0 10px; font-size: 12px; line-height: 1.7; color: var(--muted); }
.scope-notes strong { flex: 0 0 86px; font-size: 12px; color: var(--text-2); font-weight: 500; }
.scope-notes p.admin-note { margin-top: 16px; }
@media (max-width: 680px) {
  .group-header { gap: 12px; }
  .setting-row { grid-template-columns: minmax(0, 1fr); gap: 12px; padding: 18px 0; }
  .scope-notes p { display: block; }
  .scope-notes strong { display: block; margin-bottom: 3px; }
}
</style>
