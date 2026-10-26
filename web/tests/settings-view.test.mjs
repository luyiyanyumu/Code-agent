import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, nextTick, ref, watch } from 'vue'
import { usePreferences } from '../src/lib/preferences.js'
import { SETTINGS_SECTIONS, WORKSPACE_SHORTCUTS, searchSettings } from '../src/lib/settingsCatalog.js'
import { createDialogService } from '../src/composables/useDialog.js'

async function setup(overrides = {}, { deferProbe = false, role = 'admin' } = {}) {
  const calls = []
  const pendingProbes = []
  const dialog = createDialogService()
  const failures = new Map()
  const cfg = { provider: 'deepseek', base_url: 'https://api.deepseek.com', model_default: 'deepseek-flash', api_key_set: true, proxy_mode: 'system', ...overrides }
  const profile = { id: 'ds', name: 'DeepSeek', provider: 'deepseek', base_url: cfg.base_url, model_default: cfg.model_default, api_key_set: true, proxy_mode: 'direct', context_window: cfg.context_window, max_output_tokens: cfg.max_output_tokens, models: [] }
  const source = (await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const context = vm.createContext({
    defineAsyncComponent: loader => ({ loader }),
    computed, nextTick, ref, watch, usePreferences, SETTINGS_SECTIONS, WORKSPACE_SHORTCUTS, searchSettings, defineEmits: () => () => {}, defineProps: () => ({ user: { role } }), onMounted() {},
    window: { scrollTo() {}, confirm: () => { throw new Error('Native dialog is forbidden') } }, setTimeout: () => 0,
    useDialog: () => dialog,
    getToken: () => 'fixture-token',
    api: { modelSettings: async () => cfg, saveModelSettings: async payload => { calls.push({ method: 'PUT', path: '/api/v1/settings/model', body: payload }); Object.assign(cfg, payload) } },
    fetch: async (path, options) => {
      const body = options.body ? JSON.parse(options.body) : undefined
      calls.push({ method: options.method, path, body })
      if (failures.has(path)) return { ok: false, json: async () => ({ detail: failures.get(path) }) }
      if (path.endsWith('/probe') && deferProbe) return new Promise(resolve => pendingProbes.push(() => resolve({ ok: true, json: async () => ({ models: ['stale-model'] }) })))
      const data = path === '/api/v1/model-profiles' && options.method === 'GET' ? [profile]
        : path.endsWith('/probe') ? { models: ['deepseek-flash'] }
          : path.endsWith('/models') ? { items: body.items }
            : { ...profile, ...body, active: false }
      return { ok: true, json: async () => data }
    },
  })
  vm.runInContext(`${source}\n;globalThis.subject = { form, cfg, profiles, load, loadProfiles, fillForm, submit, probeModels, beginEditProfile, beginAddPreset, deleteProfile, error, saving, modelOptions, probed, storedKey }`, context)
  await context.subject.load(); await context.subject.loadProfiles()
  return { s: context.subject, calls, cfg, profile, dialog, failures, pendingProbes }
}

test('legacy settings default to system and current-mode save includes direct without credentials', async () => {
  const { s, calls } = await setup({ proxy_mode: undefined })
  assert.equal(s.form.value.proxy_mode, 'system')
  s.form.value.proxy_mode = 'direct'
  await s.submit()
  const save = calls.find(call => call.path === '/api/v1/settings/model')
  assert.equal(save.body.proxy_mode, 'direct')
  assert.equal(Object.hasOwn(save.body, 'api_key'), false)
})

test('editing a direct profile preserves mode through save and new presets default to system', async () => {
  const { s, calls, profile } = await setup()
  s.beginEditProfile(profile)
  assert.equal(s.form.value.proxy_mode, 'direct')
  await s.submit()
  assert.equal(calls.find(call => call.method === 'PUT' && call.path === '/api/v1/model-profiles/ds').body.proxy_mode, 'direct')
  s.beginAddPreset('DeepSeek')
  assert.equal(s.form.value.proxy_mode, 'system')
})

test('form probes use the selected mode and saved current credentials', async () => {
  const { s, calls } = await setup()
  s.form.value.proxy_mode = 'direct'
  await s.probeModels()
  const probe = calls.find(call => call.path.endsWith('/probe'))
  assert.equal(probe.body.proxy_mode, 'direct')
  assert.equal(probe.body.use_current, true)
  assert.equal(probe.body.api_key, '')
})

test('existing profile probes use its saved credentials without asking for key input', async () => {
  const { s, calls, profile } = await setup()
  s.beginEditProfile(profile)
  await s.probeModels()
  const probe = calls.find(call => call.path.endsWith('/probe'))
  assert.equal(probe.body.profile_id, 'ds')
  assert.equal(probe.body.proxy_mode, 'direct')
  assert.equal(probe.body.api_key, '')
})

test('single-model edit saves both capacities and preserves stored catalog and credentials', async () => {
  const { s, calls, profile } = await setup({ context_window: 131072, max_output_tokens: 8192 })
  profile.models = [{ id: 'deepseek-flash', name: 'Flash' }, { id: 'deepseek-pro', name: 'Pro' }]
  s.beginEditProfile(profile)
  s.form.value.model_default = 'deepseek-pro'
  s.form.value.context_window = ' 262144 '
  s.form.value.max_output_tokens = '16384'
  await s.submit()
  const save = calls.find(call => call.path === '/api/v1/model-profiles/ds')
  assert.equal(save.body.model_default, 'deepseek-pro')
  assert.equal(save.body.context_window, 262144)
  assert.equal(save.body.max_output_tokens, 16384)
  assert.equal(save.body.proxy_mode, 'direct')
  assert.equal(Object.hasOwn(save.body, 'api_key'), false)
  assert.equal(Object.hasOwn(save.body, 'models'), false)
  assert.equal(profile.models.length, 2)
})

test('profile delete cancellation makes no mutation and confirmed delete keeps the original target', async () => {
  const { s, calls, profile, dialog } = await setup()
  const cancelled = s.deleteProfile(profile)
  dialog.cancel()
  await cancelled
  assert.equal(calls.some(call => call.method === 'DELETE'), false)
  const deletion = s.deleteProfile(profile)
  profile.id = 'other'
  profile.name = 'Other profile'
  assert.equal(dialog.active.value.options.details, 'DeepSeek')
  assert.equal(await dialog.submit(), true)
  await deletion
  assert.equal(calls.find(call => call.method === 'DELETE').path, '/api/v1/model-profiles/ds')
})

test('clearing configured limits submits null to restore auto and presets do not inherit limits', async () => {
  const { s, calls } = await setup({ context_window: 131072, max_output_tokens: 8192 })
  assert.equal(s.form.value.context_window, 131072)
  s.form.value.context_window = ''
  s.form.value.max_output_tokens = ' '
  await s.submit()
  const save = calls.find(call => call.path === '/api/v1/settings/model')
  assert.equal(save.body.context_window, null)
  assert.equal(save.body.max_output_tokens, null)
  s.beginAddPreset('本地 Ollama')
  assert.equal(s.form.value.context_window, '')
  assert.equal(s.form.value.max_output_tokens, '')
  assert.equal(s.form.value.model_default, '')
})

test('probed options do not overwrite the selected model or configured capacities', async () => {
  const { s, profile } = await setup({ context_window: 131072, max_output_tokens: 8192 })
  profile.models = [{ name: 'Pro', id: 'deepseek-pro' }, { id: 'deepseek-flash' }]
  s.beginEditProfile(profile)
  await s.probeModels()
  assert.equal(s.form.value.model_default, 'deepseek-flash')
  assert.equal(s.form.value.context_window, 131072)
  assert.equal(s.form.value.max_output_tokens, 8192)
  assert.deepEqual(Array.from(s.modelOptions.value), ['deepseek-flash', 'deepseek-pro'])
})

test('invalid capacity values and output reservation are rejected before any save', async () => {
  for (const [window, output] of [['0', '8'], ['1.5', '1'], ['10000001', '8'], ['131072', '-1'], ['131072', '2e3'], ['131072', '131072'], ['4096', '8192'], [true, '8']]) {
    const { s, calls } = await setup()
    s.form.value.context_window = window
    s.form.value.max_output_tokens = output
    await s.submit()
    assert.equal(calls.some(call => call.method === 'PUT'), false, `${window}/${output}`)
    assert.ok(s.error.value)
    assert.equal(s.saving.value, false)
  }
})

test('current configuration reload cannot overwrite an opened profile draft', async () => {
  const { s, profile } = await setup()
  s.beginEditProfile(profile)
  s.form.value.context_window = '65536'
  s.form.value.model_default = 'custom-model'
  await s.load()
  assert.equal(s.form.value.context_window, '65536')
  assert.equal(s.form.value.model_default, 'custom-model')
  profile.api_key_set = false
  assert.equal(s.storedKey.value, false)
})

test('failed profile save leaves entered limits available for retry', async () => {
  const { s, calls, profile, failures } = await setup()
  s.beginEditProfile(profile)
  s.form.value.context_window = '65536'
  s.form.value.max_output_tokens = '4096'
  failures.set('/api/v1/model-profiles/ds', '无法保存配置')
  await s.submit()
  assert.equal(s.error.value, '无法保存配置')
  assert.equal(s.form.value.context_window, '65536')
  failures.clear()
  await s.submit()
  assert.equal(calls.filter(call => call.method === 'PUT').length, 2)
})

test('switching configurations or changing endpoint cannot populate stale model options', async () => {
  for (const switchForm of [s => s.beginAddPreset('本地 Ollama'), s => { s.form.value.base_url = 'http://127.0.0.1:11434/v1' }]) {
    const { s, pendingProbes } = await setup({}, { deferProbe: true })
    const probe = s.probeModels()
    assert.equal(pendingProbes.length, 1)
    switchForm(s)
    pendingProbes[0]()
    await probe
    assert.deepEqual(Array.from(s.probed.value), [])
    assert.equal(s.error.value, '')
  }
})


test('read-only model fields can be inspected but never submitted by a non-admin', async () => {
  const { s, calls } = await setup({}, { role: 'user' })
  assert.equal(s.form.value.model_default, 'deepseek-flash')
  s.form.value.model_default = 'must-not-save'
  await s.submit()
  assert.equal(calls.some(call => call.method === 'PUT' || call.method === 'POST'), false)
})
