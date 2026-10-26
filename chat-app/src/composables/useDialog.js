import { getCurrentScope, onScopeDispose, shallowRef } from 'vue'

/** A single modal owner serializes decisions and keeps failed submissions editable. */
export function createDialogService() {
  const active = shallowRef(null)
  const queue = []
  let serial = 0

  function advance() { if (!active.value) active.value = queue.shift() || null }
  function finish(request, result) {
    if (request.settled) return
    request.settled = true
    if (active.value?.id === request.id) active.value = null
    else {
      const index = queue.findIndex(item => item.id === request.id)
      if (index >= 0) queue.splice(index, 1)
    }
    request.resolve(result)
    advance()
  }
  function update(request, changes) {
    if (!request.settled && active.value?.id === request.id) {
      Object.assign(request, changes)
      active.value = { ...request }
    }
  }
  function open(options, owner = Symbol('dialog')) {
    // Repeated clicks from the same component must never queue duplicate mutations.
    const existing = [active.value, ...queue].find(item => item?.owner === owner)
    if (existing) return existing.promise
    let resolve
    const promise = new Promise(done => { resolve = done })
    const request = {
      id: ++serial, owner, options: { ...options, input: options.input ? { ...options.input } : null },
      value: String(options.input?.value ?? ''), error: '', busy: false,
      settled: false, promise, resolve,
    }
    queue.push(request)
    advance()
    return promise
  }
  function setValue(value) {
    const request = active.value
    if (request && !request.busy) update(request, { value, error: '' })
  }
  function cancel() {
    const request = active.value
    if (request && !request.busy) finish(request, null)
  }
  function dismissOwner(owner) {
    // Unmount cancels queued decisions; an already authorized request may finish,
    // but its late response must not change a different modal.
    for (const request of [active.value, ...queue]) {
      if (request?.owner === owner) finish(request, null)
    }
  }
  async function submit() {
    const request = active.value
    if (!request || request.busy) return false
    const { options } = request
    const value = options.input?.trim === false ? request.value : request.value.trim()
    let error = ''
    try {
      if (options.input?.required && !value) error = `请输入${options.input.label || '内容'}`
      else if (options.input?.maxLength && value.length > options.input.maxLength) error = `最多输入 ${options.input.maxLength} 个字符`
      else error = options.validate?.(value) || ''
    } catch (e) { error = e?.message || '请检查输入内容' }
    if (error) { update(request, { error }); return false }
    update(request, { busy: true, error: '' })
    try {
      const result = await options.onConfirm?.(value)
      finish(request, { value, result })
      return true
    } catch (e) {
      update(request, { error: e?.message || '操作未完成，请稍后重试。', busy: false })
      return false
    }
  }
  return { active, open, setValue, cancel, submit, dismissOwner }
}

export const dialogService = createDialogService()

export function useDialog() {
  const owner = Symbol('dialog-owner')
  let disposed = false
  if (getCurrentScope()) onScopeDispose(() => { disposed = true; dialogService.dismissOwner(owner) })
  return { open: options => disposed ? Promise.resolve(null) : dialogService.open(options, owner) }
}
