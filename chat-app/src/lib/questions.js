// 提问卡的推导与答案组装（M31）。
//
// 与任务清单同一套思路：**状态从轨迹推导**，不在前端留一份"谁在问什么"的影子状态。
// 轨迹里有两条事件：
//   question_request  （落盘）→ 问题与选项
//   question_answered （落盘）→ 状态 answered / dismissed / timeout / cancelled + 答案
// 「还有没有待回答的提问」= 最后一条 request 没有对应的 answered（按 question_id 配）。
// 这样刷新页面后那张卡还在（因为 request 落盘了），答完就自动消失，不需要额外的待办表。

const DEFAULT_OPTION_PREFIX = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']

/**
 * 推荐项约定（照 deepseek harness）：推荐项放第一个，label 末尾加 `(Recommended)`。
 * 这里把它**剥掉后**单独渲染成一个小徽标，但**答案值保持原样**——
 * 模型看到的必须是它自己写的那串字，前端不该偷偷改答案。
 * @returns {{label: string, recommended: boolean}}
 */
export function parseRecommendedLabel(label) {
  const text = String(label ?? '')
  const suffix = /\s*(?:\((?:recommended|推荐)\)|（(?:recommended|推荐)）)\s*$/i
  return suffix.test(text)
    ? { label: text.replace(suffix, ''), recommended: true }
    : { label: text, recommended: false }
}

/** 选项的字母序号（A/B/C…）：给人一眼能指代的编号。 */
export function optionPrefix(index) {
  return DEFAULT_OPTION_PREFIX[index] || String(index + 1)
}

/** 从事件流里取"当前还挂着的提问"；没有就返回 null。 */
export function pendingQuestion(events) {
  if (!Array.isArray(events) || !events.length) return null
  const answered = new Set()
  for (const ev of events) {
    if (ev?.type === 'question_answered' && ev.payload?.question_id) {
      answered.add(String(ev.payload.question_id))
    }
  }
  let latest = null
  for (const ev of events) {
    if (ev?.type !== 'question_request') continue
    const id = String(ev.payload?.question_id || '')
    if (!id || answered.has(id)) continue
    const questions = normalizeQuestions(ev.payload?.questions)
    if (!questions.length) continue
    latest = { id, questions, ts: ev.ts, timeout: Number(ev.payload?.timeout) || 0 }
  }
  return latest
}

/** 问题列表的边界清洗（服务端已经校验过，这里只防御脏轨迹）。 */
export function normalizeQuestions(raw) {
  if (!Array.isArray(raw)) return []
  const out = []
  for (const q of raw) {
    if (!q || typeof q !== 'object') continue
    const id = String(q.id ?? '').trim()
    const question = String(q.question ?? '').trim()
    if (!id || !question) continue
    const options = (Array.isArray(q.options) ? q.options : [])
      .map((o) => (typeof o === 'string' ? { label: o } : o))
      .filter((o) => o && String(o.label ?? '').trim())
      .map((o) => ({
        label: String(o.label).trim(),
        description: String(o.description ?? '').trim(),
      }))
    out.push({
      id,
      question,
      header: String(q.header ?? '').trim(),
      options,
      multiSelect: q.multi_select === true,
    })
  }
  return out
}

/**
 * 组装提交答案（照 DSH 的 `submitDrafts`，逐条对齐）：
 * - 跳过的题 → `selected: []`（**不带 custom**），模型据此知道"这题没答"；
 * - 单选且填了自定义 → `selected: []`（自定义**覆盖**选择，否则模型会以为两个都要）；
 * - 多选且填了自定义 → 两者都给（自定义是补充）；
 * - 未答且未跳过 → 返回 null 表示"还不能提交"（调用方据此拦下并提示）。
 * @returns {Array<{id: string, selected: string[], custom?: string}>|null}
 */
export function buildAnswers(questions, drafts) {
  const answers = []
  for (const q of questions) {
    const draft = drafts?.[q.id] || {}
    const selected = (draft.selected || []).filter(Boolean)
    const custom = String(draft.custom ?? '').trim()
    if (draft.skipped) {
      answers.push({ id: q.id, selected: [] })
      continue
    }
    if (selected.length === 0 && !custom) return null // 既没选也没填 → 不完整
    const one = { id: q.id }
    one.selected = custom && !q.multiSelect ? [] : selected
    if (custom) one.custom = custom
    answers.push(one)
  }
  return answers
}

/** 该题是否已经"有答案或已跳过"（决定"下一题/提交"能不能点）。 */
export function draftCompleted(draft) {
  if (!draft) return false
  if (draft.skipped) return true
  return (draft.selected || []).length > 0 || String(draft.custom ?? '').trim() !== ''
}

/** 一屏只显示一题：头部进度文案 "1 / 3"。 */
export function progressText(index, total) {
  return `${index + 1} / ${total}`
}
