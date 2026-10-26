import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, ref } from 'vue'

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

async function resource(name, expose, api) {
  const source = (await readFile(new URL(`../src/views/${name}.vue`, import.meta.url), 'utf8'))
    .match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  let mounted, publicMethods
  const context = vm.createContext({
    computed, ref, api, Date, Promise,
    defineProps: () => ({ user: { role: 'admin' } }),
    useDialog: () => ({}),
    onMounted: callback => { mounted = callback },
    defineExpose: methods => { publicMethods = methods },
  })
  vm.runInContext(`${source}\n;globalThis.subject = {${expose}}`, context)
  return { state: context.subject, mounted, publicMethods }
}

const cases = [
  {
    name: 'MemoryView', expose: 'data, newFact, expandedSession, busy', method: 'memory',
    initial: { user: { entries: ['old memory'] }, sessions: [] },
    latest: { user: { entries: ['new memory'] }, sessions: [] },
    extraApi: {},
    prepare(s) { s.newFact.value = 'unsaved memory'; s.expandedSession.value = 'session-draft' },
    verify(s) {
      assert.equal(s.newFact.value, 'unsaved memory')
      assert.equal(s.expandedSession.value, 'session-draft')
      assert.equal(s.data.value.user.entries[0], 'new memory')
    },
  },
  {
    name: 'KnowledgeView', expose: 'docs, editForm, editModal, searchQ, searchResults, area, saving, uploading, importing, busy', method: 'knowledge',
    initial: [{ id: 'old-doc' }], latest: [{ id: 'new-doc' }], extraApi: { knowledgeStats: async () => ({}) },
    prepare(s) {
      s.editForm.value = { title: 'Draft title', tags: 'draft', content: 'Unsaved document' }
      s.editModal.value = true; s.searchQ.value = 'draft query'; s.searchResults.value = [{ id: 'search-result' }]; s.area.value = 'llm'
    },
    verify(s) {
      assert.equal(s.editForm.value.content, 'Unsaved document'); assert.equal(s.editModal.value, true)
      assert.equal(s.searchQ.value, 'draft query'); assert.equal(s.searchResults.value[0].id, 'search-result'); assert.equal(s.area.value, 'llm')
      assert.equal(s.docs.value[0].id, 'new-doc')
    },
  },
  {
    name: 'UsageView', expose: 'usage', method: 'usage', initial: { prompts: 1 }, latest: { prompts: 2 }, extraApi: {},
    prepare() {}, verify(s) { assert.equal(s.usage.value.prompts, 2) },
  },
  {
    name: 'AutomationsView', expose: 'items, form, statusFilter, customSchedule', method: 'automations',
    initial: [{ id: 'old-task', status: 'active' }], latest: [{ id: 'new-task', status: 'paused' }], extraApi: {},
    prepare(s) { s.form.value.name = 'Unsaved task'; s.form.value.prompt = 'Draft prompt'; s.statusFilter.value = 'paused'; s.customSchedule.value = true },
    verify(s) {
      assert.equal(s.form.value.name, 'Unsaved task'); assert.equal(s.form.value.prompt, 'Draft prompt')
      assert.equal(s.statusFilter.value, 'paused'); assert.equal(s.customSchedule.value, true)
      assert.equal(s.items.value[0].id, 'new-task')
    },
  },
  {
    name: 'AuditView', expose: 'entries, query, outcome', method: 'audit',
    initial: [{ seq: 1 }], latest: [{ seq: 2 }], extraApi: { auditVerify: async () => ({ ok: true }) },
    prepare(s) { s.query.value = 'draft search'; s.outcome.value = 'err' },
    verify(s) { assert.equal(s.query.value, 'draft search'); assert.equal(s.outcome.value, 'err'); assert.equal(s.entries.value[0].seq, 2) },
  },
]

for (const item of cases) {
  test(`${item.name} refreshes preserved settings data without losing drafts or duplicating its pending initial read`, async () => {
    const gate = deferred()
    let requests = 0, data = item.initial
    const api = { ...item.extraApi, [item.method]: async () => { requests++; if (requests === 1) await gate.promise; return data } }
    const { state, mounted, publicMethods } = await resource(item.name, item.expose, api)
    assert.equal(typeof publicMethods.reload, 'function')
    const initialRead = mounted()
    const sameRead = publicMethods.reload()
    assert.equal(requests, 1)
    gate.resolve()
    await Promise.all([initialRead, sameRead])
    item.prepare(state)
    data = item.latest
    await publicMethods.reload()
    assert.equal(requests, 2)
    item.verify(state)
  })
}

test('memory and knowledge re-entry reads skip active mutations and can refresh afterward', async () => {
  for (const item of cases.slice(0, 2)) {
    let requests = 0
    const api = { ...item.extraApi, [item.method]: async () => { requests++; return item.initial } }
    const { state, mounted, publicMethods } = await resource(item.name, item.expose, api)
    await mounted()
    const flags = item.name === 'MemoryView' ? ['busy'] : ['busy', 'saving', 'uploading', 'importing']
    for (const flag of flags) {
      state[flag].value = flag === 'busy' ? 'operation' : true
      await publicMethods.reload()
      assert.equal(requests, 1)
      state[flag].value = flag === 'busy' ? '' : false
    }
    await publicMethods.reload()
    assert.equal(requests, 2)
  }
})
