import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, nextTick, ref, watch } from 'vue'
import { usePreferences } from '../src/lib/preferences.js'
import { SETTINGS_SECTIONS, WORKSPACE_SHORTCUTS, searchSettings } from '../src/lib/settingsCatalog.js'

const defaults = { task_tokens: 0, task_seconds: 0, team_tokens: 0, team_seconds: 0 }
async function setup({ role = 'admin', failLoad = false, failSave = false, deferSave = false } = {}) {
  const calls = [], config = { ...defaults }
  let resolveSave
  const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const context = vm.createContext({ computed, nextTick, ref, watch, usePreferences, SETTINGS_SECTIONS, WORKSPACE_SHORTCUTS, searchSettings,
    defineAsyncComponent: loader => ({ loader }), defineEmits: () => () => {}, defineProps: () => ({ user: { role } }), onMounted() {}, useDialog: () => ({}),
    api: { settings: {
      agentTeamBudgets: async () => { calls.push({ method: 'GET' }); if (failLoad) throw new Error('预算读取失败'); return { ...config } },
      saveAgentTeamBudgets: async payload => {
        calls.push({ method: 'PUT', body: { ...payload } })
        if (failSave) throw new Error('预算保存失败')
        if (deferSave) await new Promise(resolve => { resolveSave = resolve })
        Object.assign(config, payload)
        return { ...config }
      },
    } },
  })
  vm.runInContext(`${script}\nglobalThis.subject = { teamConfig, teamForm, teamError, teamBudgetConfig, teamBudgetForm, teamBudgetLoading, teamBudgetSaving, teamBudgetError, teamBudgetNotice, teamBudgetChanged, loadAgentTeamBudgets, saveAgentTeamBudgets }`, context)
  await context.subject.loadAgentTeamBudgets()
  return { s: context.subject, calls, config, source, resolve: () => resolveSave() }
}

test('budget defaults disable all caps and saving sends only four budget fields', async () => {
  const { s, calls, config } = await setup()
  assert.deepEqual({ ...s.teamBudgetConfig.value }, defaults)
  assert.equal(s.teamBudgetChanged.value, false)
  s.teamForm.value.max_parallel = 8
  s.teamBudgetForm.value.task_tokens = 50000
  s.teamBudgetForm.value.task_seconds = 300
  s.teamBudgetForm.value.team_tokens = 200000
  s.teamBudgetForm.value.max_parallel = 9
  assert.equal(s.teamBudgetChanged.value, true)
  await s.saveAgentTeamBudgets()
  assert.deepEqual(config, { task_tokens: 50000, task_seconds: 300, team_tokens: 200000, team_seconds: 0 })
  assert.deepEqual(calls[1].body, config)
  assert.equal(s.teamForm.value.max_parallel, 8, 'saving budgets preserves collaboration draft')
  assert.equal(s.teamConfig.value, null)
  assert.equal(s.teamBudgetNotice.value, '任务预算已保存')
  assert.equal(s.teamBudgetChanged.value, false)
})

test('invalid or excessive budgets remain editable and never submit a write', async () => {
  const { s, calls } = await setup()
  for (const [field, value] of [['task_tokens', ''], ['task_tokens', true], ['task_tokens', -1], ['task_tokens', 1.5], ['task_tokens', NaN], ['task_tokens', 10000001], ['task_seconds', 86401], ['team_tokens', 100000001], ['team_seconds', 604801]]) {
    s.teamBudgetForm.value = { ...defaults, [field]: value }
    await s.saveAgentTeamBudgets()
    assert.match(s.teamBudgetError.value, /整数.*0 表示不限制/)
    assert.equal(s.teamBudgetForm.value[field], value)
  }
  assert.equal(calls.filter(call => call.method === 'PUT').length, 0)
})

test('budget failure does not mask collaboration settings or erase the retryable draft', async () => {
  const failedLoad = await setup({ failLoad: true })
  assert.equal(failedLoad.s.teamBudgetConfig.value, null)
  assert.equal(failedLoad.s.teamBudgetError.value, '预算读取失败')
  assert.equal(failedLoad.s.teamError.value, '')
  await failedLoad.s.saveAgentTeamBudgets()
  assert.equal(failedLoad.calls.length, 1)
  const { s } = await setup({ failSave: true })
  s.teamBudgetForm.value.team_seconds = 3600
  await s.saveAgentTeamBudgets()
  assert.equal(s.teamBudgetForm.value.team_seconds, 3600)
  assert.equal(s.teamBudgetConfig.value.team_seconds, 0)
  assert.equal(s.teamBudgetError.value, '预算保存失败')
  assert.equal(s.teamBudgetChanged.value, true)
  assert.equal(s.teamBudgetSaving.value, false)
})

test('pending budget save blocks duplicates and reloads until it settles', async () => {
  const { s, calls, resolve } = await setup({ deferSave: true })
  s.teamBudgetForm.value.task_tokens = 100
  const pending = s.saveAgentTeamBudgets()
  assert.equal(s.teamBudgetSaving.value, true)
  await s.saveAgentTeamBudgets(); await s.loadAgentTeamBudgets()
  assert.equal(calls.length, 2)
  resolve(); await pending
  assert.equal(s.teamBudgetConfig.value.task_tokens, 100)
  assert.equal(s.teamBudgetSaving.value, false)
})

test('non-admin reads budgets while save is guarded and settings search reaches actual controls', async () => {
  const { s, calls, source } = await setup({ role: 'user' })
  s.teamBudgetForm.value.team_tokens = 100
  await s.saveAgentTeamBudgets()
  assert.equal(calls.length, 1)
  assert.equal(s.teamBudgetConfig.value.team_tokens, 0)
  assert.match(source, /:disabled="teamBudgetSaving \|\| !isAdmin"/)
  assert.match(source, /包括排队、审批和等待子任务/)
  assert.match(source, /正在进行的模型请求可能超过上限/)
  assert.equal(searchSettings('预算 Token').length, 2)
  for (const result of searchSettings('预算')) assert.ok(source.includes(`id="${result.anchor}"`), result.anchor)
})
