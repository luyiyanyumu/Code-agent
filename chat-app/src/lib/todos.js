// 任务清单的"当前状态"推导（M30）。
//
// 原理：清单不是界面猜出来的，而是模型通过 `todo_write` 工具自己写的**整份快照**。
// 由此推出三条规则——缺任何一条，界面上的进度都是错的：
//
//   1. **只认最后一次成功写入**。工具语义是整份替换（没有局部更新），所以"最后一份"
//      就是当前真相；拿更早的那份或把它们合并起来都会显示一个不存在的中间态。
//   2. **只认本轮**（最后一条**真实**用户消息之后的那次写入）。清单回答的是"这一轮做到
//      第几步"，上一轮的任务不该挂在这一轮上。注意自动续跑/检查点提示也是
//      user_message 事件，但它们是**系统注入**的（auto），不是新的一轮——
//      拿它当分界线会把长任务中途的清单清掉。
//   3. **失败的写入不算数**。`[tool error]` / `[permission denied]` 表示那次清单没生效
//      （比如两条 in_progress 被工具拒了），界面若照样显示，就会和模型看到的对不上。

export const TODO_STATUSES = ['pending', 'in_progress', 'completed']

/** 状态 → 界面文案（顺序即分组顺序）。 */
export const TODO_LABEL = {
  pending: '待处理',
  in_progress: '进行中',
  completed: '已完成',
}

/** 状态 → 行首图标（用符号而不是 emoji：与工具行的视觉语言一致）。 */
export const TODO_ICON = {
  completed: '✓',
  in_progress: '◐',
  pending: '○',
}

const FAILED = /^\[(tool error|permission denied)\]/

/** 把工具参数规范成 [{content, status}]；形状不对就返回 null（当作没写过）。 */
function normalize(raw) {
  if (!Array.isArray(raw)) return null
  const out = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') return null
    const content = String(item.content ?? '').trim()
    const status = String(item.status ?? '').trim()
    if (!content || !TODO_STATUSES.includes(status)) return null
    out.push({ content, status })
  }
  return out
}

/** 一条 user_message 是不是"新的一轮"（自动续跑/检查点提示不算）。 */
export function isRealPrompt(ev) {
  if (!ev || ev.type !== 'user_message') return false
  const p = ev.payload || {}
  return !p.auto
}

/**
 * 从事件流里取"当前这一轮的任务清单"。
 * @returns {Array<{content: string, status: string}>|null} null = 现在没有清单
 */
export function currentTodos(events) {
  if (!Array.isArray(events) || !events.length) return null
  // 工具结果按 call_id 建索引：判断一次写入是否成功要看它喂回模型的那段文本
  const results = new Map()
  for (const ev of events) {
    if (ev?.type === 'tool_result' && ev.payload?.call_id) {
      results.set(ev.payload.call_id, String(ev.payload.result ?? ''))
    }
  }
  let from = 0
  for (let i = 0; i < events.length; i += 1) {
    if (isRealPrompt(events[i])) from = i + 1
  }
  let latest = null
  for (let i = from; i < events.length; i += 1) {
    const ev = events[i]
    if (ev?.type !== 'tool_use') continue
    const call = ev.payload?.call
    if (!call || call.name !== 'todo_write') continue
    const result = results.get(call.id)
    if (result === undefined) continue          // 还没回结果：不进"最后一份"
    if (FAILED.test(result)) continue           // 那次写入没生效
    const items = normalize(call.arguments?.todos)
    if (items === null) continue
    latest = items
  }
  return latest
}

/** 按状态计数（界面头部那句 "5 已完成 · 1 进行中 · 2 待处理"）。 */
export function todoCounts(items) {
  const counts = { pending: 0, in_progress: 0, completed: 0 }
  for (const item of items || []) {
    if (counts[item.status] !== undefined) counts[item.status] += 1
  }
  return counts
}

/** 头部摘要文案：**已完成在前**（先看进展），且只列非零项（0 待处理不值得占屏）。 */
export function todoHeadline(counts) {
  const parts = []
  for (const status of ['completed', 'in_progress', 'pending']) {
    if (counts[status]) parts.push(`${counts[status]} ${TODO_LABEL[status]}`)
  }
  return parts.join(' · ')
}
