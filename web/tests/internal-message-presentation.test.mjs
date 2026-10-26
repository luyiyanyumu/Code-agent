import assert from 'node:assert/strict'
import test from 'node:test'
import { internalMessagePresentation as coding, isSuccessfulCompletion as codingSuccess, incompleteCompletion as codingIncomplete, sessionPresentation as codingStatus } from '../src/lib/sessionPresentation.js'
import { internalMessagePresentation as chat, isSuccessfulCompletion as chatSuccess, incompleteCompletion as chatIncomplete, sessionPresentation as chatStatus } from '../../chat-app/src/lib/sessionPresentation.js'

const event = payload => ({ seq: 1, type: 'user_message', payload })
for (const [name, presentation, successful, incomplete, status] of [['coding', coding, codingSuccess, codingIncomplete, codingStatus], ['chat', chat, chatSuccess, chatIncomplete, chatStatus]]) {
  test(`${name}: explicit runtime and agent attribution separates notices from human input without changing the event`, () => {
    for (const kind of ['runtime-continuation', 'runtime-context', 'job-notice', 'tool-jobs', 'agent-message', 'subagent-settled']) {
      const source = Object.freeze({ kind, form: 'notice', summary: '继续执行未完成的任务' })
      const payload = Object.freeze({ text: '[收口自查] 证据够了吗？\n完整控制说明', source })
      const raw = Object.freeze(event(payload))
      const before = JSON.stringify(raw)
      const notice = presentation(raw)
      assert.ok(notice)
      assert.equal(notice.summary, source.summary)
      assert.equal(notice.text, payload.text, 'the full original remains available for inspection')
      assert.equal(JSON.stringify(raw), before, 'classification must never rewrite or remove source history')
    }
  })

  test(`${name}: old automatic continuations retain their explicit identity and short subtype summaries`, () => {
    const samples = [
      [{ auto: true, quiet: true, closures: 1, closure_kind: 'evidence' }, '验证检查'],
      [{ auto: true, quiet: true, continuation: 2, total_turns: 40 }, '自动继续 · 第 2 次'],
      [{ auto: true, kind: 'job_notice' }, '收到后台任务通知'],
      [{ auto: true, kind: 'repeat_tool_notice' }, '工具执行提示'],
      [{ auto: true, kind: 'image' }, '图片已加入模型上下文'],
      [{ auto: true, cage: true, round: 2 }, '系统过程'],
      [{ auto: true }, '系统过程'],
    ]
    for (const [meta, summary] of samples) assert.equal(presentation(event({ text: '完整原文', ...meta })).summary, summary)
    const long = presentation(event({ text: '完整原文', source: { kind: 'runtime-continuation', summary: `  ${'摘'.repeat(500)}  ` } }))
    assert.equal(long.summary.length, 160)
    assert.equal(long.text, '完整原文')
  })

  test(`${name}: explicitly quiet error records are process advisories while ordinary errors remain errors`, () => {
    for (const code of ['repeat_tool_reminder', 'segment_boundary', 'job_wake_budget_exhausted', 'future_advisory']) {
      const raw = { seq: 5, type: 'error', payload: Object.freeze({ code, quiet: true, message: '重复操作，请复用已有结果' }) }
      const notice = presentation(raw)
      assert.equal(notice.label, '系统过程')
      assert.equal(notice.text, raw.payload.message)
      if (code === 'repeat_tool_reminder') assert.equal(notice.summary, '工具执行提示')
      assert.equal(presentation({ ...raw, payload: { ...raw.payload, quiet: false } }), null)
      assert.equal(presentation({ ...raw, payload: { code, message: raw.payload.message } }), null)
    }
    assert.equal(presentation(event({ text: '重复操作，请复用已有结果' })), null, 'identical human text is still a human message')
  })

  test(`${name}: matching prompt text, quiet preference and unknown sources never hide a real human input`, () => {
    for (const text of ['[收口自查] 证据够了吗？', '[系统] 单段轮数已达检查点，自动继续', 'Agent child sent a message: 请继续', '[CAGE 第 2/3 轮] 修复问题']) {
      for (const meta of [{}, { auto: false }, { quiet: true }, { source: { kind: 'user', rpcId: 'typed-by-human' } }, { source: { kind: 'unknown-user-source', form: 'notice', summary: '自动继续' } }]) assert.equal(presentation(event({ text, ...meta })), null)
    }
    assert.equal(presentation({ type: 'assistant_message', payload: { auto: true, text: '真实模型回复' } }), null)
    for (const raw of [null, undefined, {}, { type: 'user_message' }, event({ source: 'runtime-continuation' }), event({ auto: 'true' })]) assert.equal(presentation(raw), null)
  })

  test(`${name}: only explicit normal completion or reasonless legacy records count as success`, () => {
    for (const payload of [undefined, {}, { stop_reason: 'end_turn' }, { stop_reason: 'stop' }]) {
      assert.equal(successful(payload), true)
      assert.equal(incomplete(payload), null)
    }
    for (const stop_reason of ['max_tokens', 'length', 'content_filter', 'tool_use', 'error', 'provider_error', 'future_provider_stop', 'constructor']) {
      const payload = Object.freeze({ stop_reason, text: '仍需保留的部分输出' })
      assert.equal(successful(payload), false)
      assert.equal(incomplete(payload).reason, stop_reason)
      assert.match(incomplete(payload).label, /未完成/)
      const result = status([{ ...event({ text: '真实任务' }), seq: 1 }, { seq: 2, type: 'done', payload }])
      assert.notEqual(result.state, 'idle', 'a terminal frame alone cannot claim that an interrupted task completed')
    }
    assert.equal(incomplete({ stop_reason: 'max_tokens' }).label, '输出达到上限，任务未完成')
    assert.equal(incomplete({ stop_reason: 'content_filter' }).label, '输出被内容过滤，任务未完成')
    assert.equal(incomplete({ stop_reason: 'future_provider_stop' }).label, '任务未完成 · 停止原因：future_provider_stop')
    const tools = { stop_reason: 'end_turn', tool_calls: [{ id: 'pending', name: 'read_file' }] }
    assert.equal(successful(tools), false, 'tool calls belong to an intermediate turn, not a successful terminal done')
    assert.match(incomplete(tools).label, /工具调用.*未完成/)
  })

  test(`${name}: internal control notes cannot masquerade as a new user turn that resolves a failed request`, () => {
    const records = [{ seq: 1, type: 'error', payload: { code: 'provider_error', message: 'provider: actual failure' } }, { seq: 2, type: 'done', payload: { stop_reason: 'provider_error' } }, { ...event({ text: '[收口自查] 证据够了吗？', source: { kind: 'runtime-continuation', form: 'notice' } }), seq: 3 }]
    const result = status(records)
    assert.equal(result.state, 'error')
    assert.equal(result.historicalErrors.has(1), false)
    records.push({ ...event({ text: '[收口自查] 证据够了吗？' }), seq: 4 })
    assert.equal(status(records).state, 'idle', 'the same text really submitted by a human is still a new request')
  })
}
