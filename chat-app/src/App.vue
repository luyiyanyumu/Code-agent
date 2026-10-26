<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import ChatView from './views/ChatView.vue'
import DialogHost from './components/ui/DialogHost.vue'
import UiIcon from './components/ui/UiIcon.vue'
import ConnectionSettings from './components/ConnectionSettings.vue'
import { api, setToken } from './api.js'
import { initializePreferences, usePreferences } from './lib/preferences.js'

const { preferences, updatePreference, storageError } = usePreferences()
const disposePreferences = initializePreferences()
const user = ref(null)
const booting = ref(true)
const bootError = ref('')
const authRequired = ref(false)
const tokenInput = ref('')
const showSettings = ref(false)
let disposed = false

async function restore() {
  booting.value = true
  bootError.value = ''
  authRequired.value = false
  try {
    const identity = await api.me()
    if (!disposed) user.value = identity
  } catch (error) {
    if (!disposed) {
      authRequired.value = error.status === 401
      bootError.value = error.message || '暂时无法连接对话服务，请确认后端已启动后重试。'
    }
  } finally {
    if (!disposed) booting.value = false
  }
}

function authenticate() {
  if (!tokenInput.value.trim()) return
  setToken(tokenInput.value.trim())
  tokenInput.value = ''
  restore()
}

function changePreference(key, event) {
  const control = event.target
  updatePreference(key, key === 'reducedMotion' ? control.checked : control.value)
  if (key === 'reducedMotion') control.checked = preferences[key]
  else control.value = preferences[key]
}

function modelSettingsChanged() {
  window.dispatchEvent(new CustomEvent('chat-agent:model-settings-changed'))
}

onMounted(restore)
onBeforeUnmount(() => { disposed = true; disposePreferences() })
</script>

<template>
  <div class="chat-app">
    <header class="app-header">
      <div class="app-brand"><span class="brand-icon" aria-hidden="true"><UiIcon name="chat" :size="19" /></span><strong>Chat-agent</strong><span class="brand-note">对话 · 思考 · 创作</span></div>
      <div class="header-actions">
        <button class="settings-trigger" :disabled="!user" @click="showSettings = true"><UiIcon name="server" :size="16" /><span>模型连接</span></button>
        <details class="preferences-menu">
        <summary><UiIcon name="settings" :size="16" /><span>偏好</span></summary>
        <div class="preferences-panel">
          <h2>对话偏好</h2>
          <label>外观<select aria-label="外观" :value="preferences.theme" @change="changePreference('theme', $event)"><option value="light">浅色</option><option value="dark">深色</option><option value="system">跟随系统</option></select></label>
          <label>发送快捷键<select aria-label="发送快捷键" :value="preferences.sendKey" @change="changePreference('sendKey', $event)"><option value="enter">Enter 发送</option><option value="ctrl-enter">Ctrl / ⌘ + Enter 发送</option></select></label>
          <label class="motion-preference"><input type="checkbox" :checked="preferences.reducedMotion" @change="changePreference('reducedMotion', $event)" />减少动态效果</label>
          <p>仅保存在当前浏览器的对话应用中。</p>
          <p v-if="storageError" class="error" role="alert">{{ storageError }}</p>
        </div>
        </details>
      </div>
    </header>
    <main class="app-content">
      <div v-if="booting || bootError" class="connection-page" aria-live="polite">
        <div class="connection-card">
          <UiIcon name="chat" :size="30" />
          <h1>{{ bootError ? '暂时无法连接对话服务' : '正在打开对话' }}</h1>
          <p>{{ bootError || '正在连接服务，准备你的会话…' }}</p>
          <form v-if="authRequired" @submit.prevent="authenticate">
            <label for="access-token">访问令牌</label>
            <input id="access-token" v-model="tokenInput" type="password" autocomplete="off" placeholder="输入后端提供的访问令牌" />
            <button class="primary" :disabled="!tokenInput.trim()">连接</button>
          </form>
          <button v-if="bootError" :class="{ primary: !authRequired }" @click="restore">重新连接</button>
        </div>
      </div>
      <ChatView v-else-if="user" :user="user" />
    </main>
    <DialogHost />
    <ConnectionSettings v-if="showSettings" @close="showSettings = false" @saved="modelSettingsChanged" />
  </div>
</template>

<style scoped>
.chat-app { height: 100%; display: flex; flex-direction: column; min-width: 320px; overflow: hidden; }
.app-header { flex: none; height: 60px; display: flex; align-items: center; justify-content: space-between; padding: 0 22px; border-bottom: 1px solid var(--border); background: var(--panel); gap: 16px; }
.app-brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand-icon { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 10px; color: var(--accent); background: var(--accent-soft); }
.app-brand strong { font-size: 16px; font-weight: 650; letter-spacing: -.02em; }
.brand-note { font-size: 12px; color: var(--muted); margin-left: 8px; }
.app-content { flex: 1; min-height: 0; overflow: hidden; }
.header-actions { display: flex; align-items: center; gap: 8px; }
.settings-trigger { display: flex; align-items: center; gap: 6px; padding: 6px 11px; font-size: 13px; border: 1px solid var(--border); border-radius: 8px; color: var(--text-2); }
.preferences-menu { position: relative; }
.preferences-menu summary { list-style: none; display: flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 8px; padding: 6px 11px; cursor: pointer; font-size: 13px; color: var(--text-2); }
.preferences-menu summary::-webkit-details-marker { display: none; }
.preferences-menu summary:hover { background: var(--hover); }
.preferences-panel { position: absolute; right: 0; top: calc(100% + 10px); z-index: 100; width: min(320px, calc(100vw - 24px)); padding: 18px; border-radius: 12px; border: 1px solid var(--border); box-shadow: var(--shadow-pop); background: var(--panel); }
.preferences-panel h2 { font-size: 15px; margin: 0 0 16px; }
.preferences-panel label { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 16px; font-size: 13px; }
.preferences-panel select { min-width: 155px; max-width: 190px; }
.preferences-panel .motion-preference { justify-content: flex-start; gap: 8px; }
.preferences-panel p { color: var(--muted); font-size: 12px; margin: 0; line-height: 1.7; }
.preferences-panel .error { color: var(--err); margin-top: 8px; }
.connection-page { height: 100%; display: grid; place-items: center; padding: 24px; }
.connection-card { width: min(460px, 100%); padding: 32px; border: 1px solid var(--border); border-radius: 16px; background: var(--panel); text-align: center; }
.connection-card > svg { color: var(--accent); }
.connection-card h1 { font-size: 20px; margin: 18px 0 10px; }
.connection-card p { color: var(--muted); line-height: 1.8; font-size: 13px; }
.connection-card button { margin-top: 12px; }
.connection-card form { display: flex; flex-direction: column; text-align: left; gap: 8px; margin: 20px 0 8px; }
.connection-card label { font-size: 13px; }
@media (max-width: 600px) { .app-header { height: 52px; padding: 0 12px; gap: 8px; }.brand-note { display: none; }.app-brand { gap: 6px; }.header-actions { gap: 4px; }.settings-trigger { padding: 6px 7px; }.connection-card { padding: 22px; } }
</style>
