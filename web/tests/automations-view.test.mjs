import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, ref } from 'vue'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

async function setup() {
  const gates = { read: null, create: null, toggle: null }
  const calls = { read: 0, create: 0, toggle: 0 }
  let rows = [{ id: 'a1', name: 'Existing task', status: 'active' }]
  const api = {
    automations: async () => {
      calls.read++
      return gates.read ? gates.read.promise : rows.map(row => ({ ...row }))
    },
    createAutomation: async () => {
      calls.create++
      if (gates.create) await gates.create.promise
      rows = [...rows, { id: 'a2', name: 'New task', status: 'active' }]
    },
    setAutomationStatus: async (id, status) => {
      calls.toggle++
      if (gates.toggle) await gates.toggle.promise
      rows = rows.map(row => row.id === id ? { ...row, status } : row)
    },
  }
  const source = (await readFile(new URL('../src/views/AutomationsView.vue', import.meta.url), 'utf8'))
    .match(/<script setup>([\s\S]*?)<\/script>/)[1]
    .replace(/^import .*\r?\n/gm, '')
  const context = vm.createContext({ computed, ref, onMounted() {}, defineExpose() {}, api, Date })
  vm.runInContext(`${source}\n;globalThis.subject = { load, create, toggle, form, items, loading, creating, toggling, error }`, context)
  const subject = context.subject
  subject.form.value.name = 'New task'
  subject.form.value.prompt = 'Summarize the project'
  return { s: subject, gates, calls }
}

test('a pending list refresh blocks mutations until its result arrives', async () => {
  const { s, gates, calls } = await setup()
  gates.read = deferred()
  const refresh = s.load()
  assert.equal(s.loading.value, true)
  await s.create()
  await s.toggle({ id: 'a1', status: 'active' })
  await s.load()
  assert.deepEqual(calls, { read: 1, create: 0, toggle: 0 })

  gates.read.resolve([{ id: 'a1', status: 'active' }])
  await refresh
  gates.read = null
  assert.equal(s.loading.value, false)
  await s.create()
  assert.deepEqual(calls, { read: 2, create: 1, toggle: 0 })
  assert.equal(s.items.value.length, 2)
})

test('creation blocks manual refresh and competing writes but refreshes after success', async () => {
  const { s, gates, calls } = await setup()
  await s.load()
  gates.create = deferred()
  const creation = s.create()
  assert.equal(s.creating.value, true)
  await s.load()
  await s.create()
  await s.toggle({ id: 'a1', status: 'active' })
  assert.deepEqual(calls, { read: 1, create: 1, toggle: 0 })

  gates.create.resolve()
  await creation
  assert.deepEqual(calls, { read: 2, create: 1, toggle: 0 })
  assert.equal(s.items.value.length, 2)
  assert.equal(s.creating.value, false)
  assert.equal(s.loading.value, false)
})

test('a pending status change blocks other operations and reloads the updated status', async () => {
  const { s, gates, calls } = await setup()
  await s.load()
  gates.toggle = deferred()
  const pause = s.toggle(s.items.value[0])
  assert.equal(s.toggling.value, 'a1')
  await s.load()
  await s.create()
  await s.toggle(s.items.value[0])
  assert.deepEqual(calls, { read: 1, create: 0, toggle: 1 })

  gates.toggle.resolve()
  await pause
  assert.deepEqual(calls, { read: 2, create: 0, toggle: 1 })
  assert.equal(s.items.value[0].status, 'paused')
  assert.equal(s.toggling.value, '')
  assert.equal(s.loading.value, false)
})

test('failed requests release their operation guard so the user can retry', async () => {
  const { s, gates, calls } = await setup()
  gates.read = deferred()
  const refresh = s.load()
  gates.read.reject(new Error('List unavailable'))
  await refresh
  assert.equal(s.loading.value, false)
  assert.equal(s.error.value, 'List unavailable')
  gates.read = null
  await s.load()

  gates.create = deferred()
  const creation = s.create()
  gates.create.reject(new Error('Create unavailable'))
  await creation
  assert.equal(s.creating.value, false)
  assert.equal(s.form.value.name, 'New task')
  gates.create = null
  await s.create()
  assert.equal(s.items.value.length, 2)

  gates.toggle = deferred()
  const pause = s.toggle(s.items.value[0])
  gates.toggle.reject(new Error('Status unavailable'))
  await pause
  assert.equal(s.toggling.value, '')
  assert.equal(s.items.value[0].status, 'active')
  gates.toggle = null
  await s.toggle(s.items.value[0])
  assert.equal(s.items.value[0].status, 'paused')
  assert.deepEqual(calls, { read: 4, create: 2, toggle: 2 })
})
