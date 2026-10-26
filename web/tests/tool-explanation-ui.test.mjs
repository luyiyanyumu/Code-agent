import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as Vue from 'vue'
import { compileTemplate, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'

const source = readFileSync(new URL('../src/components/ToolExplanation.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\r?\n/gm, '')
const compiled = compileTemplate({ source: parse(source).descriptor.template.content, filename: 'ToolExplanation.vue', id: 'tool-explanation-review', compilerOptions: { hoistStatic: false } })
assert.deepEqual(compiled.errors, [])
const renderCode = compiled.code.replace(/^import \{([\s\S]*?)\} from ["']vue["']/m, (_, names) => `const {${names.replace(/\s+as\s+/g, ':')}} = Vue`).replace('export function render', 'function render')
const render = new Function('Vue', `${renderCode}\nreturn render`)(Vue)
function explanationCard(explanation, expanded = false) {
  const props = Vue.reactive({ explanation, expanded })
  const bindings = new Function('computed', 'defineProps', `${script}\nreturn { purpose, basis, effects, provided }`)(Vue.computed, () => props)
  const html = () => renderToString(Vue.createSSRApp({ setup: () => ({ ...props, ...bindings }), render }))
  return { props, bindings, html }
}

test('folded tool explanation shows only its explicit purpose and keeps the known facts and expectations collapsed', async () => {
  const card = explanationCard({ purpose: '核对浏览器依赖加载', basis: '资源请求先前超时', effects: ['只请求本地测试资源', '记录资源响应状态'], source: 'agent' })
  const html = await card.html()
  assert.match(html, /执行说明：/)
  assert.match(html, /核对浏览器依赖加载/)
  assert.doesNotMatch(html, /资源请求先前超时|只请求本地测试资源|记录资源响应状态|思路摘要/)
})

test('expanded tool explanation distinguishes purpose, known facts and expected effects from actual evidence', async () => {
  const card = explanationCard({ purpose: '核对浏览器依赖加载', basis: '资源请求先前超时', effects: ['只请求本地测试资源', '记录资源响应状态'], source: 'agent' }, true)
  const html = await card.html()
  for (const text of ['思路摘要', '智能体说明', '调用目的', '依据的已知事实', '预期影响', '资源请求先前超时', '只请求本地测试资源\n记录资源响应状态', '实际执行请核对工具状态和结果']) assert.ok(html.includes(text), text)
  assert.ok(html.indexOf('调用目的') < html.indexOf('依据的已知事实'))
  assert.ok(html.indexOf('依据的已知事实') < html.indexOf('预期影响'))
})

test('old records are explicitly missing and never derive purposes from model reasoning or command text', async () => {
  for (const value of [null, { source: 'missing' }, { purpose: ' ', basis: '', effects: [] }, { thinking: 'PRIVATE_THINKING', reasoning: 'PRIVATE_REASONING', command: 'delete important-file' }]) {
    const card = explanationCard(value, true)
    const html = await card.html()
    assert.match(html, /此记录未提供执行说明/)
    assert.doesNotMatch(html, /PRIVATE_THINKING|PRIVATE_REASONING|delete important-file|思路摘要/)
  }
})

test('partial explanation reports individual missing fields without inventing a purpose', async () => {
  const card = explanationCard({ basis: '失败日志中有缺失模块信息', effects: ['读取该模块的已安装版本'] })
  assert.match(await card.html(), /已提供依据或预期影响，展开查看。/)
  card.props.expanded = true
  const html = await card.html()
  assert.match(html, /未提供调用目的。/)
  assert.match(html, /失败日志中有缺失模块信息/)
})

test('each tool retains independent explanation fields and reacts to an updated call', async () => {
  const one = explanationCard({ purpose: '调用一', basis: '事实一', effects: ['影响一'] }, true)
  const two = explanationCard({ purpose: '调用二', basis: '事实二', effects: ['影响二'] }, true)
  const first = await one.html(), second = await two.html()
  assert.doesNotMatch(first, /调用二|事实二|影响二/)
  assert.doesNotMatch(second, /调用一|事实一|影响一/)
  one.props.explanation = { purpose: '补全的说明', basis: '新事实', effects: '保留换行\n第二行' }
  assert.match(await one.html(), /补全的说明/)
  assert.match(await one.html(), /保留换行\n第二行/)
  assert.doesNotMatch(await two.html(), /补全的说明|新事实/)
})

test('explanations are escaped plain text and arbitrary objects are not serialized into user explanations', async () => {
  const card = explanationCard({ purpose: '<img src=x onerror="attack()">', basis: '<script>attack()</script>', effects: ['<b>effect</b>', { hidden: 'OBJECT_SENTINEL' }, null, 7], thinking: 'PRIVATE_THINKING' }, true)
  const html = await card.html()
  assert.match(html, /&lt;img src=x onerror=&quot;attack\(\)&quot;&gt;/)
  assert.match(html, /&lt;script&gt;attack\(\)&lt;\/script&gt;/)
  assert.match(html, /&lt;b&gt;effect&lt;\/b&gt;/)
  assert.doesNotMatch(html, /<img|<script|<b>effect|OBJECT_SENTINEL|PRIVATE_THINKING/)
  const missing = explanationCard({ purpose: { content: 'OBJECT_PURPOSE' }, basis: 123, effects: [{ content: 'OBJECT_EFFECT' }] }, true)
  assert.match(await missing.html(), /此记录未提供执行说明/)
  assert.doesNotMatch(await missing.html(), /OBJECT_PURPOSE|OBJECT_EFFECT/)
})
