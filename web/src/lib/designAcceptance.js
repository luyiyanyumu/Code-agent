export const ACCEPTANCE_LIMIT = 100
export const ACCEPTANCE_TEMPLATES = Object.freeze([
  { id: 'interaction', label: '交互', titles: ['主要按钮可点击，并展示预期反馈', '页面链接与导航能到达正确位置', '弹窗支持打开、关闭及键盘操作'] },
  { id: 'form', label: '表单', titles: ['必填项为空时显示明确提示', '输入不合法数据时阻止提交并提示原因', '有效数据提交后展示明确结果'] },
  { id: 'responsive', label: '响应式', titles: ['手机宽度下文字与控件无横向溢出', '平板宽度下布局与内容顺序正确', '桌面宽度下重点操作清晰可见'] },
].map(template => Object.freeze({ ...template, titles: Object.freeze(template.titles) })))

const states = new Set(['pending', 'passed', 'failed'])
function validChecks(checks) {
  return Array.isArray(checks) ? checks.filter(check => check && typeof check.id === 'string' && typeof check.title === 'string') : []
}
const checkState = check => states.has(check.state) ? check.state : 'pending'

export function acceptanceSummary(checks) {
  const summary = { total: 0, pending: 0, passed: 0, failed: 0, completed: 0, progress: 0 }
  for (const check of validChecks(checks)) { summary.total++; summary[checkState(check)]++ }
  summary.completed = summary.passed + summary.failed
  summary.progress = summary.total ? Math.round(summary.passed * 100 / summary.total) : 0
  return summary
}

export function filterAcceptanceChecks(checks, { state = 'all', query = '' } = {}) {
  const search = String(query || '').trim().toLocaleLowerCase()
  return validChecks(checks).filter(check => (!states.has(state) || checkState(check) === state)
    && (!search || `${check.title} ${String(check.note || '')}`.toLocaleLowerCase().includes(search)))
}

export function acceptanceTemplateTitles(templateId, checks) {
  const existing = validChecks(checks), remaining = Math.max(0, ACCEPTANCE_LIMIT - existing.length)
  const titles = new Set(existing.map(check => check.title.trim().toLocaleLowerCase()))
  return (ACCEPTANCE_TEMPLATES.find(template => template.id === templateId)?.titles || [])
    .filter(title => !titles.has(title.toLocaleLowerCase())).slice(0, remaining)
}

function markdownText(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\\/g, '\\\\').replace(/([|`*_[\]#!~])/g, '\\$1').replace(/\r?\n/g, '<br>')
}
export function formatAcceptanceReport({ checks = [], entryPath = '', device = 'fluid' } = {}) {
  const summary = acceptanceSummary(checks)
  const labels = { pending: '待验收', passed: '通过', failed: '失败' }
  const deviceLabel = { fluid: '自适应', tablet: '平板（768 px）', phone: '手机（390 px）' }[device] || device
  const rows = validChecks(checks).map(check => `| ${labels[checkState(check)]} | ${markdownText(check.title.slice(0, 240))} | ${markdownText(String(check.note || '').slice(0, 600))} |`)
  return ['# 前端页面验收报告', '', `- 页面：${markdownText(entryPath || '未选择页面')}`, `- 预览宽度：${markdownText(deviceLabel)}`,
    `- 总计 ${summary.total} 项；通过 ${summary.passed} 项，失败 ${summary.failed} 项，待验收 ${summary.pending} 项。`,
    `- 通过进度：${summary.progress}%`, '', '| 状态 | 验收项 | 备注 |', '| --- | --- | --- |', ...rows, '',
    '验收结果为人工记录，页面、数据或预览宽度变化后请重新验证。', ''].join('\n')
}
