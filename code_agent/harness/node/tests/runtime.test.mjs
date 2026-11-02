import assert from 'node:assert/strict';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { runHarness } from '../runner.mjs';

const tool = name => ({ name, description: name, parameters: { type: 'object', properties: {} } });
const call = (id, name, args = {}) => ({ id, name, arguments: args });
const toolTurn = (...tool_calls) => ({ text: '', tool_calls, stop_reason: 'tool_use' });
const answer = text => ({ text, stop_reason: 'end_turn' });
const start = extra => ({ messages: [{ role: 'system', content: 'test system' }, { role: 'user', content: 'do work' }], ...extra });

test('official core executes a tool round and derives ordered history', async () => {
  const requests = [];
  const result = await runHarness(start({ tools: [tool('read')] }), async (method, payload) => {
    requests.push({ method, ...payload });
    if (method === 'tool') return 'real result';
    return requests.filter(r => r.method === 'model').length === 1 ? toolTurn(call('r1', 'read')) : answer('done');
  });
  assert.equal(result.harness.package, '@deepseek-ai/dsh-agent-loop');
  assert.equal(result.turns, 2);
  assert.equal(result.text, 'done');
  assert.equal(result.stop_reason, 'end_turn');
  assert.deepEqual(result.messages.map(m => m.role), ['system', 'user', 'assistant', 'tool', 'assistant']);
  assert.equal(requests.at(-1).messages.at(-1).content, 'real result');
  assert.equal(requests.at(-1).messages.at(-1).tool_call_id, 'r1');
});

test('imports earlier native tool history and preserves multimodal user input', async () => {
  const image = [{ type: 'text', text: 'inspect' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } }];
  const prior = [
    { role: 'system', content: 'sys' }, { role: 'user', content: 'earlier' },
    { role: 'assistant', content: '', tool_calls: [{ id: 'old', type: 'function', function: { name: 'read', arguments: '{}' } }] },
    { role: 'tool', tool_call_id: 'old', content: 'historical evidence' },
    { role: 'assistant', content: 'earlier answer' }, { role: 'user', content: image },
  ];
  const result = await runHarness(start({ messages: prior }), async (method, payload) => {
    assert.equal(method, 'model');
    assert.deepEqual(payload.messages, prior);
    return answer('image response');
  });
  assert.equal(result.turns, 1);
  assert.deepEqual(result.messages.slice(0, -1), prior);
});

test('parallel-safe reads overlap; exclusive writes are barriers; results keep call order', async () => {
  let active = 0;
  let peak = 0;
  const trace = [];
  let models = 0;
  const result = await runHarness(start({
    tools: [tool('read'), tool('write')], executionModes: { read: 'parallel', write: 'exclusive' }, maxParallelToolCalls: 2,
  }), async (method, payload) => {
    if (method === 'model') {
      if (++models === 1) return toolTurn(call('r1', 'read'), call('r2', 'read'), call('w', 'write'), call('r3', 'read'));
      assert.deepEqual(payload.messages.filter(m => m.role === 'tool').map(m => m.tool_call_id), ['r1', 'r2', 'w', 'r3']);
      return answer('done');
    }
    const id = payload.call.id;
    if (payload.call.name === 'write') assert.equal(active, 0);
    active++;
    peak = Math.max(peak, active);
    trace.push(`start:${id}`);
    await delay(id === 'r1' ? 25 : 5);
    trace.push(`end:${id}`);
    active--;
    return id;
  });
  assert.equal(result.stop_reason, 'end_turn');
  assert.equal(peak, 2);
  assert.ok(trace.indexOf('start:r2') < trace.indexOf('end:r1'));
  assert.ok(trace.indexOf('start:w') > trace.indexOf('end:r1'));
  assert.ok(trace.indexOf('start:w') > trace.indexOf('end:r2'));
  assert.ok(trace.indexOf('start:r3') > trace.indexOf('end:w'));
});

test('positive maxTurns stops before another model call with complete tool results', async () => {
  const result = await runHarness(start({ tools: [tool('read')], maxTurns: 2 }), async (method, payload) =>
    method === 'tool' ? payload.call.id : toolTurn(call(`read-${payload.messages.length}`, 'read')),
  );
  assert.equal(result.turns, 2);
  assert.equal(result.stop_reason, 'max_turns');
  assert.equal(result.segment_boundary, true);
  assert.equal(result.messages.at(-1).role, 'tool');
  assert.equal(result.messages.filter(m => m.role === 'tool').length, 2);
});

test('unknown tools get upstream error results without executing Python tools', async () => {
  let n = 0;
  const result = await runHarness(start({}), async (method, payload) => {
    assert.equal(method, 'model');
    if (++n === 1) return toolTurn(call('bad', 'unknown'));
    assert.equal(payload.messages.at(-1).role, 'tool');
    assert.ok(payload.messages.at(-1).content.startsWith('[tool error]'));
    assert.match(payload.messages.at(-1).content, /unknown|not found/i);
    return answer('recovered');
  });
  assert.equal(result.text, 'recovered');
});

test('ordinary tool callback errors remain recoverable model evidence', async () => {
  let n = 0;
  const result = await runHarness(start({ tools: [tool('read')] }), async (method, payload) => {
    if (method === 'tool') throw new Error('temporary tool failure');
    if (++n === 1) return toolTurn(call('bad', 'read'));
    assert.match(payload.messages.at(-1).content, /temporary tool failure/);
    return answer('error understood');
  });
  assert.equal(result.text, 'error understood');
});

test('provider exceptions after successful rounds fail instead of returning prior success', async () => {
  let n = 0;
  await assert.rejects(runHarness(start({ tools: [tool('read')] }), async method => {
    if (method === 'tool') return 'ok';
    if (++n === 1) return toolTurn(call('first', 'read'));
    throw new Error('provider unavailable');
  }), /provider unavailable/);
});

test('max_tokens and provider terminal states never dispatch partial tools', async () => {
  for (const reason of ['max_tokens', 'content_filter', 'error']) {
    let executions = 0;
    const result = await runHarness(start({ tools: [tool('write')] }), async method => {
      if (method === 'tool') executions++;
      return { ...toolTurn(call('partial', 'write')), stop_reason: reason };
    });
    assert.equal(executions, 0);
    assert.equal(result.stop_reason, reason);
    assert.equal(result.turns, 1);
  }
});

test('Python context additions join the native session before the assistant', async () => {
  let n = 0;
  const context = { role: 'user', content: 'a background job completed', _context_source: 'auto' };
  const image = { role: 'user', _context_source: 'image', content: [
    { type: 'text', text: '[read_image] screenshot' },
    { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } },
  ] };
  const result = await runHarness(start({ tools: [tool('read')] }), async (method, payload) => {
    if (method === 'tool') return 'ok';
    if (++n === 1) return { ...toolTurn(call('r', 'read')), contextMessages: [context, image] };
    assert.deepEqual(payload.messages[2], context);
    assert.deepEqual(payload.messages[3], image);
    return answer('done');
  });
  assert.deepEqual(result.messages[2], context);
  assert.deepEqual(result.messages[3], image);
});

test('canceling an in-flight model aborts its callback and unwinds official services', async () => {
  const controller = new AbortController();
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  const running = runHarness(start({}), async (_method, _payload, signal) => {
    started();
    await delay(10_000, undefined, { signal });
    return answer('never');
  }, controller.signal);
  await ready;
  controller.abort(new Error('test cancel'));
  await assert.rejects(running, /test cancel/);
});

test('JSONL child accepts callbacks and exits without stdout contamination', async () => {
  const runner = fileURLToPath(new URL('../runner.mjs', import.meta.url));
  const child = spawn(process.execPath, [runner], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  let buffer = '';
  const frames = [];
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', value => { stderr += value; });
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', value => {
    stdout += value;
    buffer += value;
    while (buffer.includes('\n')) {
      const index = buffer.indexOf('\n');
      const line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      const frame = JSON.parse(line);
      frames.push(frame);
      if (frame.type === 'request') child.stdin.write(JSON.stringify({ type: 'response', id: frame.id, result: answer('wire done') }) + '\n');
    }
  });
  const exit = new Promise(resolve => child.on('close', resolve));
  child.stdin.write(JSON.stringify({ type: 'start', ...start({}) }) + '\n');
  assert.equal(await exit, 0);
  assert.equal(stderr, '');
  assert.equal(frames.at(-1).type, 'done');
  assert.equal(frames.at(-1).text, 'wire done');
  assert.ok(stdout.endsWith('\n'));
});
