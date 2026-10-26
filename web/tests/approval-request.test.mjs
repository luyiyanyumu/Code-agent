import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as Vue from 'vue'
import { computed, reactive } from 'vue'
import { compileTemplate, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import { sessionPhaseLabel } from '../src/lib/sessionPresentation.js'

const source = readFileSync(new URL('../src/components/ApprovalRequest.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\n/gm, '')

function reviewCard(componentSource, approval, extra = {}) {
  const props = reactive({ approval, submitting: false, readonly: true, ...extra })
  const events = []
  const setup = componentSource.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
  const bindings = new Function('computed', 'defineProps', 'defineEmits', `${setup}\nreturn { props, purpose, effects, command, argumentsText, emit }`)(computed, () => props, () => (name, value) => events.push([name, value]))
  const compiled = compileTemplate({ source: parse(componentSource).descriptor.template.content, filename: 'ApprovalRequest.vue', id: 'approval-review', compilerOptions: { hoistStatic: false } })
  assert.deepEqual(compiled.errors, [])
  const code = compiled.code.replace(/^import \{([\s\S]*?)\} from ["']vue["']/m, (_, names) => `const {${names.replace(/\s+as\s+/g, ':')}} = Vue`).replace('export function render', 'function render')
  const render = new Function('Vue', `${code}\nreturn render`)(Vue)
  const renderHtml = () => renderToString(Vue.createSSRApp({ setup: () => ({ ...props, ...bindings }), render }))
  return { props, bindings, events, renderHtml }
}

test('approval card preserves the entire multiline command and extra parameters for review', () => {
  const command = 'echo begin\n' + 'x'.repeat(12000) + '\necho end'
  const props = reactive({ approval: { arguments: { command, cwd: 'project folder', timeout: 120 } } })
  const result = new Function('computed', 'defineProps', 'defineEmits', `${script}\nreturn { command, argumentsText }`)(computed, () => props, () => () => {})
  assert.equal(result.command.value, command)
  assert.deepEqual(JSON.parse(result.argumentsText.value), { cwd: 'project folder', timeout: 120 })
  props.approval.arguments = JSON.stringify({ command, cwd: '项目目录', timeout: 120 })
  assert.equal(result.command.value, command)
  assert.deepEqual(JSON.parse(result.argumentsText.value), { cwd: '项目目录', timeout: 120 })
  for (const legacy of ["{'command': 'echo old'}", 'null', '[1,2]', '"quoted"']) {
    props.approval.arguments = legacy
    assert.equal(result.command.value, '')
    assert.equal(result.argumentsText.value, legacy)
  }
  props.approval.arguments = { path: 'src/main.py', content: 'full file content' }
  assert.equal(result.command.value, '')
  assert.deepEqual(JSON.parse(result.argumentsText.value), props.approval.arguments)
  assert.match(source, /overflow: auto/)
  assert.match(source, /会话仍保持只读；批准只授权本次操作/)
  assert.match(source, /仅批准本次/)
  assert.equal((source.match(/:disabled="submitting"/g) || []).length, 2)
  assert.equal(sessionPhaseLabel('waiting_approval'), '等待你批准')
})

for (const [app, location] of [
  ['coding', '../src/components/ApprovalRequest.vue'],
  ['independent chat', '../../chat-app/src/components/ApprovalRequest.vue'],
]) {
  const componentSource = readFileSync(new URL(location, import.meta.url), 'utf8')

  test(`${app} approval separates the explicit purpose, effects, and permission reason`, async () => {
    const review = reviewCard(componentSource, {
      tool: 'bash', purpose: ' 验证前端依赖加载是否超时\n核对最近启动日志。 ',
      effects: [' 将测试脚本超时改为 90 秒。 ', '只请求本地开发资源，不发送模型请求。'],
      reason: '当前会话为只读模式，需要单次授权。', arguments: { command: 'node offline-check.mjs\necho complete' },
    })
    const html = await review.renderHtml()
    assert.ok(html.indexOf('操作目的') < html.indexOf('命令</span>'))
    assert.match(html, /验证前端依赖加载是否超时\n核对最近启动日志。/)
    assert.match(html, /操作影响/)
    assert.match(html, /将测试脚本超时改为 90 秒。/)
    assert.match(html, /申请原因/)
    assert.match(html, /当前会话为只读模式，需要单次授权。/)
    assert.equal((html.match(/<small>智能体说明<\/small>/g) || []).length, 2)
    assert.match(html, /node offline-check.mjs\necho complete/)
    assert.deepEqual(review.events, [])
  })

  test(`${app} approval never guesses a purpose or effects from an unexplained command`, async () => {
    const review = reviewCard(componentSource, { tool: 'bash', arguments: { command: 'curl http://localhost:5174 && sed -i update.mjs' }, reason: '这次操作需要授权。' })
    const html = await review.renderHtml()
    assert.match(html, /尚未提供操作目的，请先让智能体说明后再决定是否批准。/)
    assert.match(html, /尚未说明，按下方命令与参数核对。/)
    assert.equal((html.match(/<small>智能体说明<\/small>/g) || []).length, 0)
    assert.match(html, /仅批准本次/)
    assert.match(html, /会话仍保持只读；批准只授权本次操作。/)
    assert.deepEqual(review.events, [])
  })

  test(`${app} approval treats explanatory fields as escaped text and updates their content`, async () => {
    const review = reviewCard(componentSource, { tool: 'bash', purpose: '<img src=x onerror=alert(1)>', effects: '<script>secret()</script>', arguments: '{}' })
    let html = await review.renderHtml()
    assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/)
    assert.match(html, /&lt;script&gt;secret\(\)&lt;\/script&gt;/)
    assert.doesNotMatch(html, /<img|<script>/)
    review.props.approval.purpose = '  后续重新核对静态检查  '
    review.props.approval.effects = [' 读取文件。 ', null, { command: 'not a description' }, '']
    assert.equal(review.bindings.purpose.value, '后续重新核对静态检查')
    assert.deepEqual(review.bindings.effects.value, ['读取文件。'])
    html = await review.renderHtml()
    assert.match(html, /后续重新核对静态检查/)
    assert.doesNotMatch(html, /not a description/)
    review.props.approval.purpose = { command: 'not a purpose' }
    review.props.approval.effects = null
    assert.equal(review.bindings.purpose.value, '')
    assert.deepEqual(review.bindings.effects.value, [])
  })

  test(`${app} approval keeps the complete long review scrollable while its one-time controls remain outside`, async () => {
    const longPurpose = '检查资源加载耗时。\n'.repeat(300)
    const review = reviewCard(componentSource, { tool: 'bash', purpose: longPurpose, arguments: { command: 'x'.repeat(12000) } }, { submitting: true })
    const html = await review.renderHtml()
    assert.match(html, /aria-busy="true"/)
    assert.equal((html.match(/ disabled/g) || []).length, 2)
    assert.ok(html.includes(longPurpose.trim()))
    assert.ok(html.includes('x'.repeat(12000)))
    assert.match(componentSource, /\.approval-body \{[^}]*overflow: auto/)
    assert.match(componentSource, /\.approval-request > header, \.approval-footer \{ flex: none/)
    assert.match(componentSource, /white-space: pre-wrap/)
    assert.match(componentSource, /background: var\(--accent-soft, var\(--panel-soft\)\)/)
    assert.doesNotMatch(componentSource, /v-html|\.\.\/.*ApprovalRequest/)
  })
}
