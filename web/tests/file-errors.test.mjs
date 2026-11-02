import assert from 'node:assert/strict'
import test from 'node:test'
import { api } from '../src/api.js'
import { isMissingProjectFile } from '../src/lib/fileErrors.js'

test('only explicit file absence is missing, never a missing route/project or another read failure', () => {
  for (const error of [{ status: 404, code: 'PROJECT_FILE_NOT_FOUND' }, { status: 422, message: '文件不存在：src/A.java' }, { status: 404, message: '文件不存在：src/A.java' }]) assert.equal(isMissingProjectFile(error), true)
  for (const error of [{ status: 500, message: 'disk read failed' }, { status: 403, message: 'permission denied' }, { status: 404, message: '项目不存在：p' }, { status: 404, message: '后端没有这个 /api/v1/code 路由' }, { status: 422, message: '不允许读取二进制文件' }, { status: 422, message: '非法路径' }]) assert.equal(isMissingProjectFile(error), false)
})

test('the request preserves typed resource error headers without rewriting them as a missing backend route', async t => {
  const original = globalThis.fetch
  t.after(() => { globalThis.fetch = original })
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: '文件不存在：src/A.java' }), { status: 404, headers: { 'X-Code-Agent-Error-Code': 'PROJECT_FILE_NOT_FOUND' } })
  await assert.rejects(api.code.file('p', 'src/A.java'), error => error.status === 404 && error.code === 'PROJECT_FILE_NOT_FOUND' && isMissingProjectFile(error) && !error.message.includes('旧版本'))
})
