import { notifyBusinessMeaningChanged } from './businessMeaning.js'

const count = value => Math.max(0, Number.isFinite(Number(value)) ? Math.floor(Number(value)) : 0)
const revisions = new Map()
const kindLabels = { module: '模块', file: '文件', class: '类', interface: '接口', method: '方法',
  function: '函数', field: '字段', property: '属性', variable: '变量', parameter: '参数', param: '参数',
  constructor: '构造方法', enum: '枚举', annotation: '注解', constant: '常量', type: '类型' }
const hasCount = (job, key) => job[key] !== undefined && job[key] !== null

/** Both map and editor report the same background job, without claiming unknown coverage is complete. */
export function functionDescriptionPresentation(job, { loading = false, error = '' } = {}) {
  if (error) return { label: '功能描述状态暂不可用', title: error, tone: 'error' }
  if (!job) return { label: loading ? '检查功能描述…' : '功能描述待检查', title: '自动检查并补全缺少或已过期的功能描述，三处视图共用同一份说明。', tone: 'unknown' }
  const total = count(job.total), done = count(job.done), written = count(job.written), failed = count(job.failed), skipped = count(job.skipped)
  const missing = count(job.missing), described = count(job.described), coverage = count(job.coverage_total)
  const inventory = Object.entries(job.target_counts || job.by_kind || {}).filter(([, value]) => count(value))
    .map(([kind, value]) => `${kindLabels[kind] || kind} ${count(value)}`).join('、')
  const changes = [hasCount(job, 'added') ? `新增 ${count(job.added)}` : '',
    hasCount(job, 'stale') ? `过期 ${count(job.stale)}` : '',
    hasCount(job, 'reused') ? `复用 ${count(job.reused)}` : ''].filter(Boolean).join('、')
  const inventoryKnown = job.coverage_known || total > 0 || job.state === 'running'
  const details = [inventoryKnown ? `本轮待补 ${total} 个代码实体，已处理 ${done} 个` : '本轮待补数量检查中',
    `本轮生成 ${written} 条，失败 ${failed} 条${hasCount(job, 'skipped') ? `，跳过 ${skipped} 条` : ''}`,
    changes, inventory ? `本轮类型：${inventory}` : '',
    job.coverage_known ? `项目有效描述 ${described}/${coverage} 条，待补 ${missing} 条` : '',
    '类、方法、字段、参数、文件和模块等分别计数；有效描述会复用，只补缺失或过期的代码实体。',
    job.error, job.note].filter(Boolean).join('；')
  if (job.state === 'queued') return { label: '功能描述自动检查中…', title: details, tone: 'running' }
  if (job.running || job.state === 'running') return { label: `自动补功能描述：本轮 ${done}/${total}`, title: details, tone: 'running' }
  if (job.state === 'error' || failed || job.error) return { label: `功能描述${failed ? `失败 ${failed} 条` : '补全失败'}${job.coverage_known ? ` · 待补 ${missing}` : ''}`, title: details, tone: 'error' }
  if (job.state === 'waiting') return { label: `功能描述等待补全${job.coverage_known ? ` · 待补 ${missing}` : ''}`, title: details, tone: 'missing' }
  if (!job.coverage_known) return { label: '功能描述待检查', title: details || '索引准备完成后自动检查功能描述。', tone: 'unknown' }
  if (!coverage) return { label: '暂无可补充的功能描述', title: details, tone: 'unknown' }
  if (missing) return { label: `功能描述待补 ${missing} 条`, title: details, tone: 'missing' }
  return { label: `功能描述已齐 ${described}/${coverage}`, title: details, tone: 'complete' }
}

/** One notification per shared journal revision, even when map and editor poll together. */
export function publishFunctionDescriptions(projectId, tree = 'main', job, extra = {}) {
  const revision = Number(job?.revision)
  if (!projectId || !Number.isSafeInteger(revision) || revision < 0) return false
  const key = `${projectId}\0${tree}`
  const epoch = String(job.revision_epoch || '')
  const previous = revisions.get(key)
  if (previous && previous.epoch === epoch && revision <= previous.revision) return false
  revisions.delete(key)
  revisions.set(key, { epoch, revision })
  if (revisions.size > 128) revisions.delete(revisions.keys().next().value)
  if (previous === undefined && revision === 0) return false
  notifyBusinessMeaningChanged({ ...extra, projectId, tree, id: '', revision, revision_epoch: epoch, automatic: true })
  return true
}
