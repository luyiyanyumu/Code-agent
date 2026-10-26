import assert from 'node:assert/strict'
import test from 'node:test'
import { applyTextEdits, assertPlanCurrent, prepareWorkspaceEdit, relativeWorkspaceUri } from '../src/lib/workspaceEdits.js'

const uri = path => `file:///E:/work/${path}`
const edit = (start, end, newText, line = 0) => ({ range: { start: { line, character: start }, end: { line, character: end } }, newText })
const document = (path = 'main.py', text = 'name = 1\n') => ({ key: path, path, text, file: { path, text: 'disk = 1\n', editable: true, uri: uri(path), sha1: 'baseline' } })
const prepare = options => prepareWorkspaceEdit({ rootPath: 'E:/work', readFile: async path => ({ path, text: 'name = 1\n', editable: true }), ...options })

test('workspace URIs support encoded Unicode, Windows casing, UNC and exact boundaries', () => {
  assert.equal(relativeWorkspaceUri('E:\\work', 'file:///e:/WORK/%E4%BB%A3%E7%A0%81.py'), '代码.py')
  assert.equal(relativeWorkspaceUri('\\\\server\\share\\repo', 'file://server/share/repo/main.py'), 'main.py')
  assert.equal(relativeWorkspaceUri('/home/person/repo', 'file:///home/person/repo/main.py'), 'main.py')
  for (const path of ['file:///E:/work-other/main.py', 'file:///E:/work/%2e%2e/private.py', 'file:///E:/work/%2e%2e%2fprivate.py', 'https://host/E:/work/main.py', 'file:///E:/work/main.py?x=1', 'file:///E:/work/main.py#x']) assert.throws(() => relativeWorkspaceUri('E:/work', path))
  assert.throws(() => relativeWorkspaceUri('/home/person/repo', 'file:///home/person/REPO/main.py'))
})

test('text edits preserve CRLF and UTF16 ranges, same-position insert order and adjacent edits', () => {
  assert.equal(applyTextEdits('😀name\r\nother\r\n', [edit(2, 6, 'new'), edit(0, 5, 'next', 1)]), '😀new\r\nnext\r\n')
  assert.equal(applyTextEdits('abc', [edit(1, 1, 'X'), edit(1, 1, 'Y')]), 'aXYbc')
  assert.equal(applyTextEdits('abc', [edit(0, 1, 'A'), edit(1, 2, 'B')]), 'ABc')
  assert.equal(applyTextEdits('abc\n', [edit(0, 0, 'last', 1)]), 'abc\nlast')
})

test('overlapping, reversed and out-of-bounds edits fail before application', () => {
  for (const edits of [[edit(0, 2, 'x'), edit(1, 3, 'y')], [edit(3, 1, 'x')], [edit(0, 4, 'x')], [edit(0, 0, 'x', 2)], [edit(-1, 0, 'x')]]) assert.throws(() => applyTextEdits('abc', edits))
})

test('previews edit current dirty drafts and read only unopened files without mutating either', async () => {
  const doc = document(), reads = []
  const rows = await prepare({ documents: [doc], edit: { changes: { [uri('main.py')]: [edit(0, 4, 'renamed')], [uri('helper.py')]: [edit(0, 4, 'renamed')] } }, readFile: async path => { reads.push(path); return { path, text: 'name = 1\n', editable: true, sha1: 'disk' } } })
  assert.deepEqual(reads, ['helper.py'])
  assert.equal(rows[0].before, doc.text); assert.equal(rows[0].file.sha1, 'baseline'); assert.equal(rows[0].after, 'renamed = 1\n')
  assert.equal(doc.text, 'name = 1\n'); assert.equal(doc.file.text, 'disk = 1\n')
  assert.equal(rows[1].key, ''); assert.equal(rows[1].file.sha1, 'disk')
})

test('versioned edits and request snapshots reject stale draft results and support Windows URI casing', async () => {
  const doc = document(), documentChanges = [{ textDocument: { uri: uri('main.py'), version: 3 }, edits: [edit(0, 4, 'new')] }]
  await assert.rejects(prepare({ documents: [doc], edit: { documentChanges }, versions: { [uri('main.py')]: 4 } }), /变化/)
  await assert.rejects(prepare({ documents: [doc], edit: { documentChanges }, versions: { [uri('main.py')]: 3 }, snapshots: { [uri('main.py')]: 'earlier' } }), /变化/)
  documentChanges[0].textDocument.uri = 'file:///e:/WORK/MAIN.py'
  const rows = await prepare({ documents: [doc], edit: { documentChanges }, versions: { [uri('main.py')]: 3 }, snapshots: { [uri('main.py')]: doc.text } })
  assert.equal(rows[0].key, doc.key)
})

test('unsafe resource operations, secret/partial documents and oversized batches are rejected', async () => {
  await assert.rejects(prepare({ edit: { documentChanges: [{ kind: 'delete', uri: uri('main.py'), options: { recursive: true } }] } }), /递归删除/)
  const changes = { [uri('main.py')]: [edit(0, 4, 'new')] }
  for (const flags of [{ secret: true }, { editable: false }, { truncated: true }, { start_line: 2 }, { end_line: 1, total_lines: 20 }]) await assert.rejects(prepare({ edit: { changes }, readFile: async path => ({ path, text: 'name = 1\n', ...flags }) }), /不可编辑|完整/)
  await assert.rejects(prepare({ edit: { changes }, maxChars: 1 }), /过大/)
  await assert.rejects(prepare({ edit: { changes: { ...changes, [uri('helper.py')]: changes[uri('main.py')] } }, maxFiles: 1 }), /超过/)
})

const missing = () => { throw Object.assign(new Error('missing'), { status: 404 }) }
const resourceRead = files => async path => Object.hasOwn(files, path) ? { path, text: files[path], editable: true, uri: uri(path) } : missing()

test('ordered resource edits preserve operation order and produce exact whole-batch preconditions', async () => {
  const rows = await prepare({ readFile: resourceRead({ 'main.py': 'name = 1\n', 'old.py': 'old\n' }), edit: { documentChanges: [
    { textDocument: { uri: uri('main.py'), version: null }, edits: [edit(0, 4, 'renamed')] },
    { kind: 'rename', oldUri: uri('main.py'), newUri: uri('next.py') },
    { textDocument: { uri: uri('next.py'), version: null }, edits: [edit(10, 11, '2')] },
    { kind: 'create', uri: uri('new.py') },
    { textDocument: { uri: uri('new.py'), version: null }, edits: [edit(0, 0, 'created\n')] },
    { kind: 'delete', uri: uri('old.py') },
  ] } })
  assert.equal(rows.resourceOperations, true)
  assert.deepEqual(rows.map(row => row.kind), ['edit', 'rename', 'edit', 'create', 'edit', 'delete'])
  assert.deepEqual(rows.transactionFiles, [
    { path: 'main.py', before_exists: true, before_text: 'name = 1\n', after_exists: false, after_text: '' },
    { path: 'next.py', before_exists: false, before_text: '', after_exists: true, after_text: 'renamed = 2\n', mode_from: 'main.py' },
    { path: 'new.py', before_exists: false, before_text: '', after_exists: true, after_text: 'created\n' },
    { path: 'old.py', before_exists: true, before_text: 'old\n', after_exists: false, after_text: '' },
  ])
})

test('resource refactors reject dirty snapshots and later drafts across both rename endpoints', async () => {
  const doc = { ...document(), dirty: true }
  const change = { documentChanges: [{ kind: 'rename', oldUri: uri('main.py'), newUri: uri('next.py') }] }
  await assert.rejects(prepare({ documents: [doc], readFile: resourceRead({}), edit: change }), /草稿/)
  doc.dirty = false; doc.file.text = doc.text; doc.savedText = doc.text
  const rows = await prepare({ documents: [doc], readFile: resourceRead({}), edit: change })
  assert.doesNotThrow(() => assertPlanCurrent(rows, [doc]))
  doc.dirty = true; assert.throws(() => assertPlanCurrent(rows, [doc]), /草稿/)
  doc.dirty = false
  const target = { path: 'next.py', text: 'later', dirty: true }
  assert.throws(() => assertPlanCurrent(rows, [doc, target]), /草稿/)
  await assert.rejects(prepare({ documents: [doc], readFile: resourceRead({}), edit: change, snapshots: { [uri('main.py')]: 'old request' } }), /变化/)
})

test('resource collision, outside targets, partial loading, ambiguous server actions and permission errors fail before mutation', async () => {
  const original = { 'main.py': 'name = 1\n', 'existing.py': '' }
  for (const change of [
    { kind: 'create', uri: uri('main.py') },
    { kind: 'rename', oldUri: uri('main.py'), newUri: uri('existing.py') },
    { kind: 'rename', oldUri: uri('main.py'), newUri: uri('main.py') },
    { kind: 'rename', oldUri: uri('missing.py'), newUri: uri('next.py') },
    { kind: 'rename', oldUri: uri('main.py'), newUri: 'file:///E:/outside.py' },
    { kind: 'delete', uri: uri('main.py'), options: { recursive: true } },
    { kind: 'create', uri: uri('new.py'), options: { overwrite: true } },
  ]) await assert.rejects(prepare({ edit: { documentChanges: [change] }, readFile: resourceRead(original) }))
  const deleteEdit = { documentChanges: [{ kind: 'delete', uri: uri('main.py') }] }
  await assert.rejects(prepare({ edit: deleteEdit, requireVersions: true }), /主动文件操作/)
  await assert.rejects(prepare({ edit: deleteEdit, readFile: async () => ({ text: 'partial', partial: true }) }), /完整/)
  await assert.rejects(prepare({ edit: { documentChanges: [{ kind: 'create', uri: uri('new.py') }] }, readFile: async () => { throw Object.assign(new Error('forbidden'), { status: 403 }) } }), /forbidden/)
  assert.deepEqual(original, { 'main.py': 'name = 1\n', 'existing.py': '' })
})

test('resource no-op flags follow LSP without relaxing overwrite or returning an empty plan', async () => {
  const rows = await prepare({ edit: { documentChanges: [
    { kind: 'create', uri: uri('main.py'), options: { ignoreIfExists: true } },
    { kind: 'delete', uri: uri('missing.py'), options: { ignoreIfNotExists: true } },
    { kind: 'create', uri: uri('empty.py') },
  ] }, readFile: resourceRead({ 'main.py': 'existing' }) })
  assert.deepEqual(rows.transactionFiles, [{ path: 'empty.py', before_exists: false, before_text: '', after_exists: true, after_text: '' }])
  const transient = await prepare({ edit: { documentChanges: [
    { kind: 'create', uri: uri('temporary.py') },
    { textDocument: { uri: uri('temporary.py'), version: null }, edits: [edit(0, 0, 'new\n')] },
    { kind: 'rename', oldUri: uri('temporary.py'), newUri: uri('final.py') },
  ] }, readFile: resourceRead({}) })
  assert.deepEqual(transient.transactionFiles, [{ path: 'final.py', before_exists: false, before_text: '', after_exists: true, after_text: 'new\n' }])
})

test('unsolicited server edits require a matching declared version rather than delivery snapshots', async () => {
  const doc = document('main.py', 'bar = 2\n'), target = uri('main.py')
  const modifications = [edit(0, 3, 'renamed')]
  await assert.rejects(prepare({ requireVersions: true, documents: [doc], edit: { changes: { [target]: modifications } }, snapshots: { [target]: doc.text } }), /缺少文档版本/)
  await assert.rejects(prepare({ requireVersions: true, edit: { changes: { [uri('helper.py')]: modifications } } }), /缺少文档版本/)
  const documentChanges = [{ textDocument: { uri: target, version: 4 }, edits: modifications }]
  await assert.rejects(prepare({ requireVersions: true, documents: [doc], edit: { documentChanges }, versions: { [target]: 5 } }), /变化/)
  const rows = await prepare({ requireVersions: true, documents: [doc], edit: { documentChanges }, versions: { [target]: 4 }, snapshots: { [target]: doc.text } })
  assert.equal(rows[0].after, 'renamed = 2\n')
})

test('a later bad target fails the whole preview without modifying valid drafts', async () => {
  const doc = document()
  await assert.rejects(prepare({ documents: [doc], edit: { changes: { [uri('main.py')]: [edit(0, 4, 'new')], [uri('missing.py')]: [edit(0, 4, 'new')] } }, readFile: async () => { throw new Error('missing') } }), /missing/)
  assert.equal(doc.text, 'name = 1\n')
})

test('batch apply and undo validation rejects later typing or closed targets before any mutation', async () => {
  const doc = document(), rows = await prepare({ documents: [doc], edit: { changes: { [uri('main.py')]: [edit(0, 4, 'new')] } } })
  assert.doesNotThrow(() => assertPlanCurrent(rows, [doc]))
  assert.throws(() => assertPlanCurrent(rows, []), /已关闭/)
  doc.text = 'later user input'; assert.throws(() => assertPlanCurrent(rows, [doc]), /变化/)
  doc.text = rows[0].after; assert.doesNotThrow(() => assertPlanCurrent(rows, [doc], 'undo'))
  assert.throws(() => assertPlanCurrent(rows, [], 'undo'), /已关闭/)
  doc.text = 'new later input'; assert.throws(() => assertPlanCurrent(rows, [doc], 'undo'), /变化/)
})
