import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { reactive, ref, nextTick } from 'vue'
import { api } from '../src/api.js'

const content = readFileSync(new URL('../src/components/ConnectionSettings.vue', import.meta.url), 'utf8')
const script = content.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"]\s*$/gm, '')

test('connection form keeps public profiles separate from private password and sends only chat routes', async t => {
  const calls = [], emitted = []
  const publicProfile = { id: 'saved', name: 'fixture', provider: 'openai-compatible', model: 'local', base_url: 'http://localhost/v1', has_key: true }
  const bindings = {
    reactive, ref, nextTick, onMounted() {}, defineEmits: () => (...args) => emitted.push(args),
    api: {
      modelProfiles: async () => [publicProfile],
      saveModelProfile: async body => { calls.push(['save', body]); return publicProfile },
      activateModelProfile: async id => calls.push(['activate', id]),
      deleteModelProfile: async id => calls.push(['delete', id]),
    },
  }
  const state = new Function(...Object.keys(bindings), script + '\nreturn { load, edit, save, activate, remove, draft, clearKey, activateAfterSave, profiles, status }')(...Object.values(bindings))
  await state.load()
  state.edit(publicProfile)
  assert.equal(state.draft.api_key, '', 'saved credentials are never loaded into the password field')
  state.draft.api_key = 'new-fixture-secret'
  await state.save()
  assert.equal(calls[0][1].api_key, 'new-fixture-secret')
  assert.deepEqual(calls[1], ['activate', 'saved'])
  assert.equal(state.draft.api_key, '', 'password field is cleared after save')
  assert.equal(emitted[0][0], 'saved')
  await state.remove(publicProfile)
  assert.equal(state.draft.id, '')

  const previous = globalThis.fetch
  globalThis.fetch = async (path, options) => { calls.push([path, options]); return { ok: true, status: 200, text: async () => '{}' } }
  t.after(() => { globalThis.fetch = previous })
  await api.saveModelProfile({ name: 'x' })
  assert.equal(calls.at(-1)[0], '/api/v1/model-profiles')
  await api.activateModelProfile('id with /')
  assert.equal(calls.at(-1)[0], '/api/v1/model-profiles/id%20with%20%2F/activate')
})

test('settings dialog is accessible and never initiates a model prompt or network connection test', () => {
  assert.match(content, /aria-labelledby="connection-settings-title"/)
  assert.match(content, /@cancel\.prevent/)
  assert.match(content, /type="password" autocomplete="new-password"/)
  assert.match(content, /保存设置不会请求模型服务/)
  assert.doesNotMatch(content, /api\.prompt|api\.createSession|fetch\(/)
})
