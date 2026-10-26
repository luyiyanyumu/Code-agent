import assert from 'node:assert/strict'
import { test } from 'node:test'
import { effectScope } from 'vue'
import { createDialogService, dialogService, useDialog } from '../src/composables/useDialog.js'

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }

test('cancel never invokes a mutation and same-owner double clicks share one decision', async () => {
  const service = createDialogService(), owner = Symbol()
  let writes = 0
  const first = service.open({ onConfirm: () => writes++ }, owner)
  assert.equal(service.open({ onConfirm: () => writes++ }, owner), first)
  service.cancel()
  assert.equal(await first, null)
  assert.equal(service.active.value, null)
  assert.equal(writes, 0)
})

test('required whitespace and custom validation retain the dialog and original input', async () => {
  const service = createDialogService()
  let writes = 0
  const done = service.open({ input: { label: '工作区名称', required: true }, validate: value => value.includes('/') ? '名称不能包含斜杠' : '', onConfirm: () => writes++ })
  service.setValue('  ')
  assert.equal(await service.submit(), false)
  assert.equal(service.active.value.error, '请输入工作区名称')
  service.setValue('bad/name')
  assert.equal(await service.submit(), false)
  assert.equal(service.active.value.error, '名称不能包含斜杠')
  service.setValue('  项目讨论  ')
  assert.equal(service.active.value.error, '')
  assert.equal(await service.submit(), true)
  assert.deepEqual(await done, { value: '项目讨论', result: 0 })
  assert.equal(writes, 1)
})

test('failed API submissions retain input, release pending state and support one retry', async () => {
  const service = createDialogService()
  let attempts = 0
  const done = service.open({ input: { value: '工作区' }, onConfirm: async () => { if (++attempts === 1) throw new Error('连接暂时中断'); return 'created' } })
  assert.equal(await service.submit(), false)
  assert.equal(service.active.value.error, '连接暂时中断')
  assert.equal(service.active.value.value, '工作区')
  assert.equal(service.active.value.busy, false)
  assert.equal(await service.submit(), true)
  assert.deepEqual(await done, { value: '工作区', result: 'created' })
  assert.equal(attempts, 2)
})

test('pending requests cannot be canceled, submitted twice or edited', async () => {
  const service = createDialogService(), pending = deferred()
  let writes = 0
  const done = service.open({ input: { value: '保留' }, onConfirm: () => { writes++; return pending.promise } })
  const first = service.submit()
  assert.equal(service.active.value.busy, true)
  service.cancel(); service.setValue('不允许')
  assert.equal(service.active.value.value, '保留')
  assert.equal(await service.submit(), false)
  pending.resolve('ok')
  await first
  assert.deepEqual(await done, { value: '保留', result: 'ok' })
  assert.equal(writes, 1)
})

test('unmount removes queued decisions and late responses cannot alter the next modal', async () => {
  const service = createDialogService(), owner = Symbol(), pending = deferred()
  const first = service.open({ onConfirm: () => pending.promise }, owner)
  const next = service.open({ title: '另一操作' })
  const submitting = service.submit()
  service.dismissOwner(owner)
  assert.equal(await first, null)
  assert.equal(service.active.value.options.title, '另一操作')
  pending.reject(new Error('旧请求错误'))
  await submitting
  assert.equal(service.active.value.error, '')
  assert.equal(service.active.value.busy, false)
  service.cancel()
  assert.equal(await next, null)

  const active = service.open({ title: '当前' })
  const queued = service.open({ title: '已卸载' }, owner)
  service.dismissOwner(owner)
  assert.equal(await queued, null)
  service.cancel(); await active
  assert.equal(service.active.value, null)
})

test('useDialog cancels automatically when its Vue scope is disposed', async () => {
  const scope = effectScope()
  const dialog = scope.run(() => useDialog())
  const done = dialog.open({ title: '页面操作' })
  scope.stop()
  assert.equal(await done, null)
  assert.equal(await dialog.open({ title: '失效操作' }), null)
  assert.equal(dialogService.active.value, null)
})
