<script setup>
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, getToken, setToken } from './api.js'
import DialogHost from './components/ui/DialogHost.vue'
import AgentLogo from './components/ui/AgentLogo.vue'
import { initializePreferences } from './lib/preferences.js'
import { SETTINGS_SECTIONS } from './lib/settingsCatalog.js'

const disposePreferences = initializePreferences()
const CodeView = defineAsyncComponent(() => import('./views/CodeView.vue'))
const SettingsView = defineAsyncComponent(() => import('./views/SettingsView.vue'))
const user = ref(null)
const booting = ref(true)
const bootError = ref('')

function savedPreference(key, fallback) {
  try { return localStorage.getItem(key) || fallback } catch { return fallback }
}
const resourceSections = new Set(['memory', 'knowledge', 'usage', 'automations', 'audit'])
const rememberedView = savedPreference('code-agent:view', 'code')
const rememberedSection = rememberedView === 'tools' ? 'extensions'
  : resourceSections.has(rememberedView) ? rememberedView
  : savedPreference('code-agent:settings-section', 'general')
const settingsInitialSection = ref(SETTINGS_SECTIONS.some(item => item.id === rememberedSection) ? rememberedSection : 'general')
const settingsInitialToolTab = ref('builtin')
// 启动始终进入代码工作区，旧资源页面只迁移为设置中的分类。
const view = ref('code')
const settingsVisited = ref(false)
const activeLabel = computed(() => view.value === 'settings' ? '设置' : '代码')
let returnFocus = null

function settingsSectionChanged(section) {
  if (!SETTINGS_SECTIONS.some(item => item.id === section)) return
  settingsInitialSection.value = section
  try { localStorage.setItem('code-agent:settings-section', section) } catch { /* 禁止存储时仍可切换分类。 */ }
}
function openSettings(section = '', tab = '') {
  if (typeof section === 'string' && section) settingsSectionChanged(section)
  if (['builtin', 'mcp', 'plugin', 'off', 'skill', 'expert'].includes(tab)) settingsInitialToolTab.value = tab
  if (view.value === 'code') returnFocus = globalThis.document?.activeElement || null
  settingsVisited.value = true
  view.value = 'settings'
}
async function closeSettings() {
  view.value = 'code'
  await nextTick()
  const target = returnFocus?.isConnected ? returnFocus : globalThis.document?.querySelector('[aria-label="打开设置"]')
  target?.focus?.({ preventScroll: true })
  returnFocus = null
}
function settingsNavigate(target) {
  const nextView = typeof target === 'string' ? target : target?.view
  if (nextView === 'code') { void closeSettings(); return }
  if (nextView === 'tools') { openSettings('extensions', target?.tab); return }
  if (resourceSections.has(nextView)) { openSettings(nextView); return }
  if (nextView === 'settings') openSettings()
}
function onSettingsShortcut(event) {
  if (event.key === ',' && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    openSettings()
  }
}
onMounted(() => {
  window.addEventListener('keydown', onSettingsShortcut)
  // 丢弃旧全局页面的启动状态，保留其迁移后的设置分类。
  try { localStorage.setItem('code-agent:view', 'code') } catch { /* 工作区仍可使用。 */ }
  settingsSectionChanged(settingsInitialSection.value)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onSettingsShortcut)
  disposePreferences()
})

async function restore() {
  booting.value = true
  bootError.value = ''
  try {
    try { user.value = await api.me() }
    catch (e) {
      if (e.status !== 401 || !getToken()) throw e
      setToken('')
      user.value = await api.me()
    }
  } catch (e) { bootError.value = e.message || '暂时无法连接服务，请稍后重试。' }
  finally { booting.value = false }
}
onMounted(restore)
</script>

<template>
  <div v-if="booting || bootError" class="boot" aria-live="polite">
    <div class="boot-card">
      <AgentLogo class="logo" :wordmark="false" :size="44" />
      <h1>{{ bootError ? '工作区暂时无法连接' : '正在打开工作区' }}</h1>
      <p>{{ bootError || '连接服务，准备你的工作空间…' }}</p>
      <button v-if="bootError" class="primary" @click="restore">重新连接</button>
      <span v-else class="boot-spinner" aria-hidden="true"></span>
    </div>
  </div>
  <div v-else class="shell">
    <a class="skip-link" href="#main-workspace">跳转到主要内容</a>
    <main id="main-workspace" class="content" :aria-label="activeLabel" tabindex="-1">
      <CodeView v-show="view === 'code'" :key="user.id || user.user_id" :user="user" :visible="view === 'code'" @open-settings="openSettings" />
      <SettingsView v-if="settingsVisited" v-show="view === 'settings'" :user="user" :visible="view === 'settings'" :initial-section="settingsInitialSection" :initial-tool-tab="settingsInitialToolTab" @close="closeSettings" @section-change="settingsSectionChanged" @navigate="settingsNavigate" />
    </main>
  </div>
  <DialogHost />
</template>

<style scoped>
.boot { display: grid; place-items: center; height: 100%; color: var(--muted); padding: var(--sp-6); }
.boot-card { text-align: center; max-width: 460px; }
.logo { margin: 0 auto var(--sp-6); }
.boot-card h1 { font-size: var(--fs-xl); color: var(--text); margin: 0 0 var(--sp-2); }
.boot-card p { margin: 0 0 var(--sp-6); overflow-wrap: anywhere; }
.boot-spinner { display: block; width: 22px; height: 22px; margin: 0 auto; border: 2px solid var(--border-strong); border-top-color: var(--accent); border-radius: 50%; animation: boot-spin .8s linear infinite; }
@keyframes boot-spin { to { transform: rotate(360deg); } }
.shell { height: 100%; display: flex; min-width: 0; min-height: 0; }
.content { flex: 1; outline: none; background: var(--bg); overflow: hidden; display: flex; flex-direction: column; min-width: 0; min-height: 0; position: relative; }
.skip-link { position: fixed; top: 8px; left: 12px; z-index: 1000; padding: 8px 14px; background: var(--panel); border-radius: 8px; box-shadow: var(--shadow-pop); transform: translateY(-160%); }
.skip-link:focus { transform: translateY(0); }
@media (prefers-reduced-motion: reduce) { .boot-spinner { animation: none; } }
</style>
