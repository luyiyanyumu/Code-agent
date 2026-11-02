/**
 * DeepSeek Harness host for Code-agent's Python services.
 *
 * The official AgentLoop owns every turn, step, natural stop and tool barrier.
 * This module only translates the public LLM/tool contracts across a local
 * JSONL pipe. No credentials, filesystem tools or desktop profiles are loaded.
 */
import { verifyCore } from './verify.mjs';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const build = await verifyCore();
const {
  Context, AgentRegistry, AgentLoop, LlmRuntime, LlmAdapter,
  createAssistantMessage, createDeveloperMessage, createSystemMessage,
  createToolResultMessage, createUserMessage, SessionStore, SessionProjections,
  SystemPrompt, ToolRuntime,
} = await import('./core.mjs');
const VERSION = build.upstream.version;
const provenance = { package: '@deepseek-ai/dsh-agent-loop', version: VERSION, mode: 'source-merge', commit: build.upstream.commit };
const textBlock = text => ({ type: 'text', text });
const clone = value => JSON.parse(JSON.stringify(value));
const textOf = content => typeof content === 'string' ? content :
  Array.isArray(content) ? content.filter(b => b.type === 'text').map(b => b.text).join('') : '';

/** Marshal durable native messages while preserving imported multimodal inputs. */
class Messages {
  originals = new Map();

  fromWire(wire) {
    const content = [textBlock(textOf(wire.content))];
    let message;
    if (wire.role === 'system') {
      message = createSystemMessage(textOf(wire.content));
    } else if (wire.role === 'assistant') {
      for (const call of wire.tool_calls ?? []) content.push({
        type: 'tool-call', id: call.id, name: call.function.name,
        arguments: typeof call.function.arguments === 'string' ? call.function.arguments : JSON.stringify(call.function.arguments ?? {}),
      });
      message = createAssistantMessage({ content, source: { provider: 'code-agent', model: 'configured' } });
    } else if (wire.role === 'tool') {
      message = createToolResultMessage({ callId: wire.tool_call_id, content, isError: false });
    } else if (wire.role === 'developer') {
      message = createDeveloperMessage({ content, source: { kind: 'code-agent-context' } });
    } else {
      message = createUserMessage({ content, source: { kind: 'user' } });
    }
    // IDs are immutable across native session snapshots. The opaque wire input
    // remains memory-only, preserving image_url blocks without disk persistence.
    this.originals.set(message.id, clone(wire));
    return message;
  }

  toWire(message) {
    if (this.originals.has(message.id)) return clone(this.originals.get(message.id));
    if (message.role === 'tool') {
      let content = textOf(message.content);
      // Upstream-generated failures have structured isError metadata. Python's
      // existing transcript/UI contract carries that state in the result text.
      if (message.isError && !content.startsWith('[tool error]') && !content.startsWith('[permission denied]')) {
        content = '[tool error] ' + content;
      }
      return { role: 'tool', tool_call_id: message.source.callId, content };
    }
    const wire = { role: message.role, content: textOf(message.content) };
    const calls = message.content.filter(block => block.type === 'tool-call');
    if (calls.length) wire.tool_calls = calls.map(call => ({
      id: call.id, type: 'function', function: { name: call.name, arguments: call.arguments },
    }));
    return wire;
  }

  append(session, wire, position = { turn: 0, step: 0 }) {
    const message = this.fromWire(wire);
    if (wire.role === 'system') session.append('system/message', { ...position, message }, { surfaceOp: 'append' });
    else if (wire.role === 'assistant') session.append('assistant/message', { ...position, message, stream: [] }, { surfaceOp: 'append' });
    else if (wire.role === 'tool') session.append('tool/result', { ...position, message }, { surfaceOp: 'append' });
    else if (wire.role === 'developer') session.append('developer/message', { ...position, message }, { surfaceOp: 'append' });
    else session.append('user/message', message, { surfaceOp: 'append' });
    return message;
  }
}

/** Run exactly one prompt through the unmodified upstream AgentLoop service. */
export async function runHarness(start, request, signal = new AbortController().signal) {
  signal.throwIfAborted();
  const ctx = new Context();
  const messages = new Messages();
  let handle;
  let calls = 0;
  let lastTurn = null;
  let failure;
  let boundary = false;
  let abortListener;
  const currentPosition = { turn: 1, step: 1 };
  try {
    for (const plugin of [SessionStore, SessionProjections, AgentRegistry, LlmRuntime]) {
      await ctx.plugin(plugin).await();
    }
    await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false, includeRuntimeContext: false }).await();
    await ctx.plugin(ToolRuntime, { mode: 'native' }).await();
    await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: start.maxParallelToolCalls ?? 10 }).await();

    const allMessages = clone(start.messages ?? []);
    const system = allMessages.filter(m => m.role === 'system').map(m => textOf(m.content)).join('\n\n') || start.systemPrompt || '';
    ctx.systemPrompt.section({ name: 'code-agent', order: 0, text: system, complete: true, interpolate: false });

    for (const row of start.tools ?? []) {
      const tool = row.function ?? row;
      ctx.tools.register({
        name: tool.name, description: tool.description ?? '', parameters: tool.parameters ?? { type: 'object' },
        isConcurrencySafe: () => (start.executionModes?.[tool.name] ?? row.executionMode) === 'parallel',
        output: { schema: { type: 'string' }, render: (_args, value) => [textBlock(value)] },
        execute: async (args, exec) => {
          const result = await request('tool', { call: { id: exec.callId, name: tool.name, arguments: args } }, exec.signal);
          return typeof result === 'string' ? result : JSON.stringify(result);
        },
      });
    }

    class PythonAdapter extends LlmAdapter {
      async *stream(options) {
        calls++;
        const result = await request('model', {
          messages: options.messages.map(m => messages.toWire(m)),
          tools: (options.tools ?? []).map(tool => ({ type: 'function', function: tool })),
        }, options.signal);
        options.signal?.throwIfAborted();
        lastTurn = result;
        for (const context of result.contextMessages ?? []) {
          if (context.role !== 'user' && context.role !== 'developer') throw new Error('contextMessages must contain user or developer messages');
          messages.append(handle.agent.session, context, currentPosition);
        }
        let index = 0;
        if (result.text) {
          yield { type: 'block-start', index, blockType: 'text' };
          yield { type: 'text-delta', index, text: result.text };
          yield { type: 'block-end', index, block: textBlock(result.text) };
          index++;
        }
        for (const call of result.tool_calls ?? []) {
          const block = { type: 'tool-call', id: call.id, name: call.name, arguments: JSON.stringify(call.arguments ?? {}) };
          yield { type: 'block-start', index, blockType: 'tool-call' };
          yield { type: 'tool-call-delta', index, id: call.id, name: call.name, argumentsDelta: block.arguments };
          yield { type: 'block-end', index, block };
          index++;
        }
        const usage = result.usage ?? {};
        yield { type: 'usage', usage: {
          inputTokens: Math.max(0, (usage.input_tokens ?? 0) - (usage.cached_tokens ?? 0)),
          outputTokens: usage.output_tokens ?? 0, cacheReadTokens: usage.cached_tokens ?? 0,
        } };
        const stop = result.stop_reason || 'end_turn';
        let reason;
        if (['length', 'max-tokens', 'max_tokens'].includes(stop)) reason = { kind: 'max-tokens' };
        else if (['stop', 'end_turn', 'tool_use', 'tool_calls', 'completed'].includes(stop)) {
          reason = { kind: (result.tool_calls?.length ?? 0) ? 'tool-calls' : 'stop' };
        } else reason = { kind: 'error', failure: { code: stop, message: result.text || `Provider stopped: ${stop}` } };
        yield { type: 'finish', reason };
      }
    }
    ctx.llm.registerAdapter(['code-agent'], new PythonAdapter());
    ctx.on('agent/error', payload => { failure = payload.error; });
    ctx.on('agent/pre-step', async (payload, next) => {
      currentPosition.turn = payload.turn;
      currentPosition.step = payload.step;
      if (start.maxTurns > 0 && calls >= start.maxTurns) {
        boundary = true;
        return { kind: 'reject' };
      }
      return next();
    });

    const history = allMessages.filter(m => m.role !== 'system');
    let input;
    if (history.length && history.at(-1).role === 'user') input = history.pop();
    else input = { role: 'user', content: start.prompt ?? '' };
    handle = await ctx.agents.create({
      sessionId: start.sessionId || 'code-agent-session',
      ...(start.cwd ? { meta: { cwd: start.cwd } } : {}),
      agentOptions: { provider: 'code-agent', model: 'configured' },
      setup: (_agentCtx, agent) => {
        if (system) messages.append(agent.session, { role: 'system', content: system });
        for (const message of history) messages.append(agent.session, message);
      },
    });
    abortListener = () => handle.agent.cancel({ kind: 'user' });
    signal.addEventListener('abort', abortListener, { once: true });
    signal.throwIfAborted();
    handle.agent.followup(messages.fromWire(input));
    await handle.agent.whenIdle();
    signal.throwIfAborted();
    // The upstream driver contains errors to restore an idle session. The host
    // must still fail its own request, including failures after successful steps.
    if (failure && (!lastTurn || ['stop', 'end_turn', 'tool_use', 'tool_calls', 'completed'].includes(lastTurn.stop_reason || 'end_turn'))) throw failure;
    const stop = boundary ? 'max_turns' : lastTurn?.stop_reason || (failure ? 'error' : 'end_turn');
    return {
      text: lastTurn?.text || '',
      stop_reason: ['stop', 'tool_use', 'tool_calls', 'completed'].includes(stop) ? 'end_turn' : stop,
      turns: calls, segment_boundary: boundary,
      messages: handle.agent.session.deriveMessages().map(message => messages.toWire(message)),
      ...(failure ? { error: String(failure.message ?? failure) } : {}),
      harness: provenance,
    };
  } finally {
    if (abortListener) signal.removeEventListener('abort', abortListener);
    await handle?.dispose();
    await ctx.fiber.dispose();
  }
}

function write(frame) { process.stdout.write(JSON.stringify(frame) + '\n'); }

/** Serve a single isolated run; stdout is exclusively this private protocol. */
export async function serve() {
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const pending = new Map();
  const abort = new AbortController();
  let sequence = 0;
  let running;
  const request = (method, payload, signal) => new Promise((resolve, reject) => {
    if (signal?.aborted || abort.signal.aborted) { reject(signal?.reason || abort.signal.reason); return; }
    const id = String(++sequence);
    const onAbort = () => { pending.delete(id); reject(signal.reason); };
    signal?.addEventListener('abort', onAbort, { once: true });
    pending.set(id, {
      resolve: result => { signal?.removeEventListener('abort', onAbort); resolve(result); },
      reject: error => { signal?.removeEventListener('abort', onAbort); reject(error); },
    });
    write({ type: 'request', id, method, ...payload });
  });
  const cancel = () => {
    abort.abort(new Error('Harness run canceled'));
    for (const call of pending.values()) call.reject(abort.signal.reason);
    pending.clear();
  };
  input.on('line', line => {
    let frame;
    try { frame = JSON.parse(line); } catch { write({ type: 'error', message: 'Invalid JSONL frame' }); cancel(); return; }
    if (frame.type === 'response') {
      const call = pending.get(String(frame.id));
      if (!call) return;
      pending.delete(String(frame.id));
      if (frame.error) call.reject(new Error(typeof frame.error === 'string' ? frame.error : frame.error.message || 'Python callback failed'));
      else call.resolve(frame.result);
    } else if (frame.type === 'cancel') cancel();
    else if (frame.type === 'start' && !running) {
      running = runHarness(frame, request, abort.signal).then(
        result => write({ type: 'done', ...result }),
        error => write({ type: 'error', message: String(error?.message || error), canceled: abort.signal.aborted }),
      ).finally(() => { input.close(); process.stdin.pause(); });
    } else { write({ type: 'error', message: 'Invalid protocol sequence' }); cancel(); }
  });
  input.on('close', () => { if (!running || pending.size) cancel(); });
  process.once('SIGTERM', cancel);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv.includes('--check')) write({ ...provenance, node: process.versions.node });
  else await serve();
}
