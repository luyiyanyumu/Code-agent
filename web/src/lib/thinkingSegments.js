// 思考分段（M20）：把"一路 += 的思考缓冲"改成"每轮一段、挂到轮到它收口的事件上"。
//
// 这是个纯函数，从 ChatView 里抽出来就能单测——真问题（重复）恰恰出在这段状态机里：
// 一次 25 轮的工具循环会把 25 段推理粘成一大坨，相邻两段又常在重复同一套计划。

/**
 * @param {{payload?:object, type:string, ts:number}} ev 事件（就地改写 payload）
 * @param {{buffer:string, startedTs:number}} sink 缓冲（会被本函数改写）
 * @returns {boolean} 是否发生了挂载
 */
export function attachThinking(ev, sink) {
  const attachOn = ev.type === 'tool_use' || ev.type === 'assistant_message' || ev.type === 'done'
  if (!sink.buffer || !attachOn) return false
  ev.payload = {
    ...(ev.payload || {}),
    thinking: sink.buffer,
    thinking_ms: sink.startedTs && ev.ts >= sink.startedTs ? ev.ts - sink.startedTs : 0,
  }
  sink.buffer = ''
  sink.startedTs = 0
  return true
}

/** 单段思考的字符上限：一轮推理再长也不该把内存/渲染拖垮（超了留尾部）。 */
export const MAX_THINK_CHARS = 20000

export function pushThinking(sink, text, ts) {
  sink.buffer += text || ''
  if (sink.buffer.length > MAX_THINK_CHARS) sink.buffer = sink.buffer.slice(-MAX_THINK_CHARS)
  if (!sink.startedTs && ts) sink.startedTs = ts
}
