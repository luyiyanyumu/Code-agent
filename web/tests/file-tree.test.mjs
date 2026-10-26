import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import test from 'node:test'
import { computed, ref } from 'vue'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function tree(props, fetchTree) {
  const unmount = []
  const source = (await readFile(new URL('../src/components/code/FileTree.vue', import.meta.url), 'utf8'))
    .match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const context = vm.createContext({ computed, ref, defineProps: () => props, defineEmits: () => () => {},
    defineExpose() {}, onMounted() {}, watch() {}, onBeforeUnmount: fn => unmount.push(fn),
    api: { code: { tree: fetchTree } },
  })
  vm.runInContext(source + '\n;globalThis.subject = { refresh, loadDir, children, loading, error }', context)
  return { ...context.subject, dispose: () => unmount.forEach(fn => fn()) }
}

test('switching projects discards a late directory response and keeps replacement loading state', async () => {
  const a = deferred(), b = deferred(), props = { projectId: 'a' }
  const subject = await tree(props, pid => pid === 'a' ? a.promise : b.promise)
  try {
    const first = subject.refresh()
    props.projectId = 'b'
    const second = subject.refresh()
    a.resolve({ files: [{ path: 'old.txt' }] })
    await first
    assert.equal(subject.children.value[''], undefined)
    assert.equal(subject.loading.value[''], true)
    b.resolve({ files: [{ path: 'current.txt' }] })
    await second
    assert.equal(subject.children.value[''].files[0].path, 'current.txt')
    assert.equal(subject.loading.value[''], false)
  } finally { subject.dispose() }
})

test('repeated expansion shares the pending request and unmount ignores its response', async () => {
  const pending = deferred(), calls = []
  const subject = await tree({ projectId: 'a' }, (...args) => { calls.push(args); return pending.promise })
  const first = subject.loadDir('src'), second = subject.loadDir('src')
  assert.equal(first, second)
  await Promise.resolve()
  assert.equal(calls.length, 1)
  subject.dispose()
  pending.resolve({ files: [{ path: 'src/late.py' }] })
  await first
  assert.equal(subject.children.value.src, undefined)
})

test('manual refresh keeps an older failure from replacing the new tree', async () => {
  const old = deferred(), fresh = deferred()
  let calls = 0
  const subject = await tree({ projectId: 'a' }, () => ++calls === 1 ? old.promise : fresh.promise)
  try {
    const first = subject.refresh()
    await Promise.resolve()
    const second = subject.refresh()
    fresh.resolve({ dirs: [{ path: 'new-dir' }] })
    await second
    old.reject(new Error('obsolete failure'))
    await first
    assert.equal(subject.error.value, '')
    assert.equal(subject.children.value[''].dirs[0].path, 'new-dir')
  } finally { subject.dispose() }
})
