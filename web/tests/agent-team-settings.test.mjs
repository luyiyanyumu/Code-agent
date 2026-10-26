import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, nextTick, ref, watch } from 'vue'
import { usePreferences } from '../src/lib/preferences.js'
import { SETTINGS_SECTIONS, WORKSPACE_SHORTCUTS, searchSettings } from '../src/lib/settingsCatalog.js'

async function setup({ role = 'admin', failLoad = false, failSave = false, deferSave = false } = {}) {
  const calls = []
  let resolveSave
  const config = { max_depth: 3, max_parallel: 4 }
  const source = (await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')).match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const context = vm.createContext({
    defineAsyncComponent: loader => ({ loader }),
    computed, nextTick, ref, watch, usePreferences, SETTINGS_SECTIONS, WORKSPACE_SHORTCUTS, searchSettings, defineEmits: () => () => {}, defineProps: () => ({ user: { role } }), onMounted() {},
    useDialog: () => ({}),
    api: { settings: {
      agentTeam: async () => {
        calls.push({ method: 'GET' })
        if (failLoad) throw new Error('无法加载')
        return { ...config }
      },
      saveAgentTeam: async payload => {
        calls.push({ method: 'PUT', body: { ...payload } })
        if (failSave) throw new Error('保存失败')
        if (deferSave) await new Promise(resolve => { resolveSave = resolve })
        Object.assign(config, payload)
        return { ...config }
      },
    } },
  })
  vm.runInContext(`${source}\n;globalThis.subject = { teamConfig, teamForm, teamLoading, teamSaving, teamError, teamNotice, teamChanged, loadAgentTeam, saveAgentTeam }`, context)
  await context.subject.loadAgentTeam()
  return { s: context.subject, calls, config, resolve: () => resolveSave() }
}

test('agent team settings load independently and save only collaboration limits', async () => {
  const { s, calls, config } = await setup()
  assert.equal(s.teamChanged.value, false)
  s.teamForm.value.max_depth = 5
  s.teamForm.value.max_parallel = 8
  assert.equal(s.teamChanged.value, true)
  await s.saveAgentTeam()
  assert.deepEqual(config, { max_depth: 5, max_parallel: 8 })
  assert.deepEqual(calls[1], { method: 'PUT', body: { max_depth: 5, max_parallel: 8 } })
  assert.equal(s.teamNotice.value, '协作设置已保存')
  assert.equal(s.teamChanged.value, false)
})

test('invalid collaboration limits remain editable without any save request', async () => {
  const { s, calls } = await setup()
  for (const [field, value] of [['max_depth', ''], ['max_depth', 9], ['max_depth', 2.5], ['max_parallel', 17], ['max_parallel', 0], ['max_parallel', true]]) {
    s.teamForm.value = { max_depth: 3, max_parallel: 4, [field]: value }
    await s.saveAgentTeam()
    assert.match(s.teamError.value, /整数/)
    assert.equal(s.teamForm.value[field], value)
  }
  assert.equal(calls.filter(call => call.method === 'PUT').length, 0)
})

test('failed load leaves separate retry error and cannot overwrite unavailable settings', async () => {
  const { s, calls } = await setup({ failLoad: true })
  assert.equal(s.teamConfig.value, null)
  assert.equal(s.teamError.value, '无法加载')
  assert.equal(s.teamLoading.value, false)
  await s.saveAgentTeam()
  assert.equal(calls.length, 1)
})

test('failed save keeps draft limits and prior effective settings', async () => {
  const { s } = await setup({ failSave: true })
  s.teamForm.value.max_depth = 6
  await s.saveAgentTeam()
  assert.equal(s.teamConfig.value.max_depth, 3)
  assert.equal(s.teamForm.value.max_depth, 6)
  assert.equal(s.teamError.value, '保存失败')
  assert.equal(s.teamSaving.value, false)
  assert.equal(s.teamChanged.value, true)
})

test('duplicate saves and reloads are blocked during an in-flight save', async () => {
  const { s, calls, resolve } = await setup({ deferSave: true })
  s.teamForm.value.max_parallel = 2
  const saving = s.saveAgentTeam()
  assert.equal(s.teamSaving.value, true)
  await s.saveAgentTeam()
  await s.loadAgentTeam()
  assert.equal(calls.length, 2)
  resolve()
  await saving
  assert.equal(s.teamConfig.value.max_parallel, 2)
  assert.equal(s.teamSaving.value, false)
})

test('non-admin can load collaboration settings but cannot submit updates', async () => {
  const { s, calls } = await setup({ role: 'user' })
  s.teamForm.value.max_depth = 6
  await s.saveAgentTeam()
  assert.equal(calls.length, 1)
  assert.equal(s.teamConfig.value.max_depth, 3)
})
