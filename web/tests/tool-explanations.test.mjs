import assert from 'node:assert/strict'
import test from 'node:test'
import { buildToolCards } from '../src/composables/useAgentSession.js'
import { explanationForTool, normalizeToolExplanation, toolArguments } from '../src/lib/toolExplanations.js'

const publicExplanation = { purpose: '检查组件能否加载。', basis: '前一次请求已报告超时。', effects: ['访问本地测试页面。'], source: 'agent' }
const use = (id, explanation, args = { command: 'fixture command' }) => ({ type: 'tool_use', payload: { call: { id, name: 'bash', arguments: args }, ...(explanation === undefined ? {} : { explanation }) } })
const result = (id, value) => ({ type: 'tool_result', payload: { call_id: id, result: value } })

test('tool cards show the explicit public explanation and retain executable arguments separately', () => {
  const args = { command: 'fixture command\nsecond fixture line', cwd: '项目目录', _approval: { purpose: '旧的说明' } }
  const cards = buildToolCards([use('first', publicExplanation, args), result('first', 'fixture output')])
  assert.deepEqual(cards[0].explanation, publicExplanation)
  assert.deepEqual(cards[0].args, { command: args.command, cwd: args.cwd })
  assert.equal(cards[0].result, 'fixture output')
  assert.equal(cards[0].state, 'ok')
  assert.equal(args._approval.purpose, '旧的说明', 'normalizing does not rewrite persisted events')
})

test('parallel tools pair explanations and real results by their own call identities', () => {
  const first = { ...publicExplanation, purpose: '检查页面' }, second = { ...publicExplanation, purpose: '读取校验输出' }
  const cards = buildToolCards([use('first', first), use('second', second), result('second', '[tool error] fixture failure'), result('first', 'fixture success')])
  assert.equal(cards[0].explanation.purpose, '检查页面')
  assert.equal(cards[0].state, 'ok')
  assert.equal(cards[1].explanation.purpose, '读取校验输出')
  assert.equal(cards[1].state, 'error')
})

test('replay and session replacement rebuild declared explanations without borrowing another session', () => {
  const saved = [use('same', publicExplanation), result('same', '[permission denied] fixture refusal')]
  const replay = JSON.parse(JSON.stringify(saved))
  assert.deepEqual(buildToolCards(saved), buildToolCards(replay))
  assert.equal(buildToolCards(replay)[0].state, 'denied')
  const other = buildToolCards([use('same', { purpose: '另一会话的操作' })])[0]
  assert.equal(other.explanation.purpose, '另一会话的操作')
  assert.equal(other.explanation.basis, '')
  assert.equal(other.result, '')
})

test('legacy reserved explanations are reused, while absent intent is honestly missing', () => {
  const legacy = buildToolCards([use('old', undefined, { path: 'fixture.txt', _approval: publicExplanation })])[0]
  assert.deepEqual(legacy.explanation, publicExplanation)
  assert.deepEqual(legacy.args, { path: 'fixture.txt' })
  const missing = buildToolCards([use('missing', undefined, { command: 'sed -i fixture; curl localhost' })])[0]
  assert.deepEqual(missing.explanation, { purpose: '', basis: '', effects: [], source: 'missing' })
  assert.equal(missing.state, 'running')
})

test('internal provider reasoning cannot become a tool explanation, including an injected thinking field', () => {
  const privateText = 'PRIVATE_REASONING_SENTINEL'
  const event = use('one', { ...publicExplanation, thinking: privateText, reasoning_content: privateText })
  event.payload.thinking = privateText
  const cards = buildToolCards([{ type: 'thinking_delta', payload: { text: privateText } }, event])
  assert.equal(JSON.stringify(cards).includes(privateText), false)
  assert.deepEqual(cards[0].explanation, publicExplanation)
  assert.equal(explanationForTool({ thinking: privateText, call: { arguments: {} } }).source, 'missing')
})

test('invalid explanation fields never invent intent and explicit empty new metadata overrides old declarations', () => {
  assert.deepEqual(normalizeToolExplanation({ purpose: {}, basis: 123, effects: ['  ', null, ' 本地检查 '] }), { purpose: '', basis: '', effects: ['本地检查'], source: 'agent' })
  assert.equal(explanationForTool({ explanation: {}, call: { arguments: { _approval: publicExplanation } } }).source, 'missing')
  assert.equal(normalizeToolExplanation(null).source, 'missing')
  assert.equal(normalizeToolExplanation([]).source, 'missing')
  assert.deepEqual(toolArguments({ name: 'fixture', _approval: publicExplanation }), { name: 'fixture' })
})
