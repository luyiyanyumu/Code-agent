import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, ref } from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'

const file = new URL('../src/components/settings/ExecutionSettings.vue', import.meta.url)
const component = await readFile(file, 'utf8')

async function setup({ role = 'admin', failedLoads = [], failSave = false, deferSave = false, invalidConfig = false } = {}) {
  const calls = []
  const failures = new Set(failedLoads)
  const configs = {
    permission: { mode: 'workspace-write', modes: [
      { id: 'read-only', label: '只读', desc: '读取并为写入申请授权' },
      { id: 'workspace-write', label: '工作区写入', desc: '工作区内读写' },
      { id: 'full-access', label: '完全权限', desc: '免审批' },
    ] },
  }
  let resolveSave
  let mounted
  async function load(id) {
    calls.push({ method: 'GET', id })
    if (failures.has(id)) throw new Error('加载失败')
    return invalidConfig ? { mode: 'unknown', modes: [] } : structuredClone(configs[id])
  }
  async function save(id, mode) {
    calls.push({ method: 'PUT', id, mode })
    if (failSave) throw new Error('保存失败')
    if (deferSave) await new Promise(resolve => { resolveSave = resolve })
    configs[id].mode = mode
    return structuredClone(configs[id])
  }
  const source = component.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const context = vm.createContext({
    computed, ref, defineProps: () => ({ user: { role } }), onMounted(callback) { mounted = callback },
    api: {
      permissionSettings: () => load('permission'), savePermissionSettings: mode => save('permission', mode),
    },
  })
  vm.runInContext(`${source}\n;globalThis.subject = { settings, isAdmin, changed, selectedDescription, clearFeedback, loadSetting, saveSetting }`, context)
  await mounted()
  return { s: context.subject, calls, configs, failures, resolveSave: () => resolveSave() }
}

test('execution settings only load and save permission defaults without requesting removed execution modes', async () => {
  const { s, calls, configs } = await setup()
  assert.deepEqual(calls, [{ method: 'GET', id: 'permission' }])
  assert.equal(s.settings.value.length, 1)
  const [permission] = s.settings.value
  assert.equal(s.changed(permission), false)
  permission.draft = 'read-only'
  assert.equal(s.selectedDescription(permission), '读取并为写入申请授权')
  assert.equal(s.changed(permission), true)
  await s.saveSetting(permission)
  assert.deepEqual(calls[1], { method: 'PUT', id: 'permission', mode: 'read-only' })
  assert.equal(configs.permission.mode, 'read-only')
  assert.equal(s.changed(permission), false)
  assert.equal(permission.notice, '已保存，新会话生效')
})

test('load failures remain local to their row and can be retried without writing defaults', async () => {
  const { s, calls, failures } = await setup({ failedLoads: ['permission'] })
  const [permission] = s.settings.value
  assert.equal(permission.config, null)
  assert.equal(permission.error, '加载失败')
  permission.draft = 'full-access'
  await s.saveSetting(permission)
  assert.equal(calls.some(call => call.method === 'PUT'), false)
  failures.clear()
  await s.loadSetting(permission)
  assert.equal(permission.config.mode, 'workspace-write')
  assert.equal(permission.draft, 'workspace-write')
  assert.equal(permission.error, '')
})

test('failed save keeps the editable draft and previously effective default', async () => {
  const { s, configs } = await setup({ failSave: true })
  const permission = s.settings.value[0]
  permission.draft = 'full-access'
  await s.saveSetting(permission)
  assert.equal(permission.error, '保存失败')
  assert.equal(permission.draft, 'full-access')
  assert.equal(permission.config.mode, 'workspace-write')
  assert.equal(configs.permission.mode, 'workspace-write')
  assert.equal(permission.saving, false)
  assert.equal(s.changed(permission), true)
})

test('unchanged, unknown and non-admin updates do not call write endpoints', async () => {
  const { s, calls } = await setup()
  const permission = s.settings.value[0]
  await s.saveSetting(permission)
  permission.draft = 'invented-mode'
  await s.saveSetting(permission)
  assert.equal(permission.error, '请选择可用的设置选项')
  assert.equal(calls.some(call => call.method === 'PUT'), false)
  const readOnly = await setup({ role: 'user' })
  assert.equal(readOnly.s.isAdmin.value, false)
  for (const setting of readOnly.s.settings.value) {
    setting.draft = setting.config.modes[0].id === setting.draft ? setting.config.modes[1].id : setting.config.modes[0].id
    await readOnly.s.saveSetting(setting)
  }
  assert.equal(readOnly.calls.length, 1)
})

test('duplicate save and reload cannot overwrite an in-flight update', async () => {
  const { s, calls, resolveSave } = await setup({ deferSave: true })
  const permission = s.settings.value[0]
  permission.draft = 'read-only'
  const saving = s.saveSetting(permission)
  assert.equal(permission.saving, true)
  await s.saveSetting(permission)
  await s.loadSetting(permission)
  assert.equal(calls.length, 2)
  resolveSave()
  await saving
  assert.equal(permission.config.mode, 'read-only')
  assert.equal(permission.saving, false)
})

test('malformed server settings never become selectable or saveable defaults', async () => {
  const { s, calls } = await setup({ invalidConfig: true })
  for (const setting of s.settings.value) {
    assert.equal(setting.config, null)
    assert.match(setting.error, /设置不完整/)
    setting.draft = 'workspace-write'
    await s.saveSetting(setting)
  }
  assert.equal(calls.length, 1)
})

test('execution settings compile with labelled controls and separate scope explanations', () => {
  const { descriptor, errors } = parse(component, { filename: 'ExecutionSettings.vue' })
  assert.equal(errors.length, 0)
  const script = compileScript(descriptor, { id: 'execution-settings' })
  const template = compileTemplate({ source: descriptor.template.content, filename: 'ExecutionSettings.vue', id: 'execution-settings', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  assert.match(descriptor.template.content, /已有会话保留自己的设置/)
  assert.match(descriptor.template.content, /由角色单独控制/)
  assert.match(descriptor.template.content, /手动修改，与智能体的执行权限分别设置/)
  assert.doesNotMatch(component, /CAGE|loop-mode|默认任务推进方式/i)
})
