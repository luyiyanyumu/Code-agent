<script setup>
// 节点详情抽屉（M18 · P1 起步，P6 扩到"这个节点的一切"）。
//
// 点击导图元素后要能回答四个问题，缺一个用户就得自己去翻代码：
// 1. **它是什么**：结构、成员、反向依赖、实现片段（P1）；
// 2. **改它会惊动谁**：影响面 + 关联测试 + 风险分级（P6，每条理由可读）；
// 3. **当初为什么这么写**：决策线程（决定 / 已知风险 / 待办）；
// 4. **谁在什么时候改过**：改动溯源时间线（台账 + 任务 + 决策 + 提交）。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { kindMeta } from '../../lib/mapLayout.js'
import { api } from '../../api.js'
import { businessEntityId, displayBusiness, BUSINESS_STATUS_LABEL, matchesBusinessMeaningChange } from '../../lib/businessMeaningDisplay.js'
import BusinessMeaning from './BusinessMeaning.vue'

const props = defineProps({
  detail: { type: Object, required: true },
  projectId: { type: String, default: '' },
})
const emit = defineEmits(['close', 'open-file', 'ask', 'error'])

const showSource = ref(false)
const meta = computed(() => kindMeta(props.detail.kind))

//: 这个节点是不是"符号"节点。
//: 地图上有三类节点：`sym:*`（符号）/ `mod:*`（模块，按路径前缀聚合）/ 文件。
//: **只有符号节点才有"实现步骤 / 调用关系"**——对模块节点调 `symbol/logic` 会拿到
//: 「符号不存在」的 422，界面只能显示一句莫名其妙的告警（真机踩到）。
const isSymbolNode = computed(() => String(props.detail?.id || '').startsWith('sym:'))

//: 功能视图（M33）的**能力域**节点：一组功能的聚合，不是代码实体。
//: 可达/决策/溯源都是按符号或文件查的，对它发请求只会得到「符号不存在」。
const isDomainNode = computed(() => String(props.detail?.id || '').startsWith('fdomain:'))
const isFileNode = computed(() => String(props.detail?.id || '').startsWith('file:') || props.detail.kind === 'file')
let detailGeneration = 0
let disposed = false
let pendingLoads = 0
let lastTarget = ''
function detailKey() {
  return JSON.stringify([props.projectId, props.detail?.id, props.detail?.path, props.detail?.tree || 'main'])
}
function captureTarget() {
  return { generation: detailGeneration, key: detailKey(), projectId: props.projectId, detail: { ...props.detail } }
}
function targetCurrent(context) {
  return !disposed && context.generation === detailGeneration && context.key === detailKey()
}
function startLoading(context) {
  if (targetCurrent(context)) { pendingLoads++; loading.value = 'details' }
}
function finishLoading(context) {
  if (targetCurrent(context)) { pendingLoads--; if (!pendingLoads) loading.value = '' }
}
onBeforeUnmount(() => { disposed = true; detailGeneration++; window.removeEventListener('code-agent:business-meaning-changed', onBusinessChanged) })


//: 抽屉标题那一行。模块节点没有 path/line，以前会拼成 `undefined:undefined · NaN 行`。
const loc = computed(() => {
  const d = props.detail || {}
  if (d.path && d.line) return `${d.path}:${d.line}`
  if (d.path) return String(d.path)
  return String(d.name || d.module || d.id || '')
})

// ---- P8：实现步骤（先看"它怎么做的"，再看结构）----
const logic = ref(null)
const logicOpen = ref(true)

const STEP_CLS = {
  if: 'branch', elif: 'branch', else: 'branch', match: 'branch',
  for: 'loop', while: 'loop',
  return: 'out', raise: 'err',
  try: 'try', except: 'err', finally: 'try',
  call: 'call', await: 'call',
  docstring: 'dim', nested: 'nested',
}

async function loadLogic() {
  if (!isSymbolNode.value || !props.projectId || !props.detail.id) return
  const context = captureTarget()
  startLoading(context)
  try {
    const data = await api.code.symbolLogic(context.projectId, context.detail.id, context.detail.tree || 'main')
    if (targetCurrent(context)) logic.value = data
  } catch (e) {
    if (targetCurrent(context)) logic.value = { available: false, reason: e.message }
  } finally { finishLoading(context) }
}

// ---- P10：意义与作用（它是什么、拿来干什么、谁真的在用）----
// 为什么单独一段放在最上面：点开 `logger` 以前只有一个赋值表达式和几个引用文件，
// 读者得自己翻译"这是干啥的"。现在先给一句话结论，再列可追的依据。
const purpose = ref(null)
const purposeOpen = ref(true)
//: 边界声明默认收起：4 行免责声明会把"它到底是什么"挤出屏幕（360px 宽的抽屉）
const purposeNotes = ref(false)
let purposeSeq = 0

const purposeFacts = computed(() => (purpose.value?.available ? purpose.value.facts || [] : []))

const purposeDocumentation = computed(() => {
  const data = purpose.value
  if (data?.documentation?.text) return data.documentation
  // Generated descriptions belong to the shared function section above. Only
  // actual source documentation is rendered again as implementation detail.
  if (props.detail.doc) return { text: props.detail.doc, source: '作者说明', line: props.detail.line }
  return null
})

async function loadPurpose() {
  if (isDomainNode.value || !props.projectId || !props.detail.id) return
  const context = captureTarget()
  const mine = ++purposeSeq
  startLoading(context)
  try {
    const data = await api.code.symbolPurpose(context.projectId, context.detail.id, context.detail.tree || 'main')
    if (targetCurrent(context) && mine === purposeSeq) purpose.value = data
  } catch (e) {
    if (targetCurrent(context) && mine === purposeSeq) purpose.value = { available: false, reason: e.message }
  } finally { finishLoading(context) }
}

function onBusinessChanged(event) {
  const change = event.detail || {}
  if (!matchesBusinessMeaningChange(change, { projectId: props.projectId, tree: props.detail.tree, id: businessEntityId(props.detail) })) return
  void loadPurpose()
}
function businessSaved(record) {
  if (record.entity?.id !== businessEntityId(props.detail)) return
  if (purpose.value) purpose.value = { ...purpose.value, business: record.business }
}
onMounted(() => window.addEventListener('code-agent:business-meaning-changed', onBusinessChanged))

// ---- P6：可达 / 决策 / 溯源 ----
const tab = ref('logic')            // logic | impact | decisions | history
const impact = ref(null)
const behaviors = ref([])
// M32 契约层：这张节点所属文件的契约对照（形状/义务/证据/首个未满足项）
const contract = ref(null)
const decisions = ref([])
const decisionKinds = ref([])
const history = ref(null)
const loading = ref('')
const note = ref('')
const draft = ref({ kind: 'decision', text: '' })

const RISK_LABEL = { low: '低', medium: '中', high: '高', critical: '极高' }
const targetId = computed(() => props.detail.id || `file:${props.detail.path}`)

//: 契约角标的一行摘要：**只显示能一眼判断"要不要点开"的信息**
const contractLine = computed(() => {
  const c = contract.value
  if (!c) return ''
  // 模块节点（`mod:`）拿到的是一份"目录汇总"：字段与单文件不同，说人话即可
  if (c.is_dir) {
    if (!c.items?.length) return `${c.python_files} 个实现，都没有契约`
    const ok = c.items.filter((i) => i.ok).length
    return `${c.items.length}/${c.python_files} 个实现有契约 · 其中 ${ok} 个对得上`
  }
  if (!c.has_contract) return '这个文件没有契约'
  const parts = [
    `${c.obligation_count} 条义务`,
    `证据 ${c.evidence_count}（${Math.round((c.coverage || 0) * 100)}%）`,
  ]
  if (c.declared_gaps?.length) parts.push(`${c.declared_gaps.length} 条声明无独立证据`)
  return parts.join(' · ')
})

async function loadContract() {
  if (!props.projectId || !props.detail.path) return
  const context = captureTarget()
  startLoading(context)
  try {
    const data = await api.code.contracts(context.projectId, { path: context.detail.path })
    if (targetCurrent(context)) contract.value = data
  } catch (e) {
    if (targetCurrent(context)) contract.value = { has_contract: false, error: e.message }
  } finally { finishLoading(context) }
}

async function loadImpact() {
  if (!props.projectId) return
  const context = captureTarget()
  const file = String(context.detail.id || '').startsWith('file:') || context.detail.kind === 'file'
  startLoading(context)
  try {
    const [report, spec] = await Promise.all([
      api.code.impact(context.projectId, {
        symbol_ids: !file && context.detail.id ? [context.detail.id] : [],
        files: file || !context.detail.id ? [context.detail.path] : [],
        tree: context.detail.tree || 'main',
      }),
      api.code.behaviors(context.projectId, {
        target: context.detail.id || context.detail.path,
        tree: context.detail.tree || 'main', limit: 30,
      }),
    ])
    if (targetCurrent(context)) { impact.value = report; behaviors.value = spec.cards || [] }
  } catch (e) {
    if (targetCurrent(context)) impact.value = { error: e.message }
  } finally { finishLoading(context) }
}

async function loadDecisions() {
  if (!props.projectId) return
  const context = captureTarget()
  startLoading(context)
  try {
    const data = await api.code.decisions(context.projectId, {
      target: context.detail.id || `file:${context.detail.path}`, limit: 50, prefix: !context.detail.id,
    })
    if (targetCurrent(context)) { decisions.value = data.items || []; decisionKinds.value = data.kinds || [] }
  } catch (e) {
    if (targetCurrent(context)) emit('error', e.message)
  } finally { finishLoading(context) }
}

async function loadHistory() {
  if (!props.projectId) return
  const context = captureTarget()
  startLoading(context)
  try {
    const data = await api.code.provenance(context.projectId, {
      path: context.detail.path,
      symbolId: String(context.detail.id || '').startsWith('file:') ? '' : context.detail.id || '',
      tree: context.detail.tree || 'main',
    })
    if (targetCurrent(context)) history.value = data
  } catch (e) {
    if (targetCurrent(context)) emit('error', e.message)
  } finally { finishLoading(context) }
}

async function refresh() {
  if (disposed) return
  detailGeneration++
  const key = detailKey()
  if (key !== lastTarget) {
    draft.value = { kind: 'decision', text: '' }
    tab.value = 'logic'
    purposeOpen.value = true
    purposeNotes.value = false
    showSource.value = false
    lastTarget = key
  }
  note.value = ''
  pendingLoads = 0
  loading.value = ''
  purpose.value = logic.value = impact.value = contract.value = history.value = null
  behaviors.value = decisions.value = decisionKinds.value = []
  if (isDomainNode.value) return
  await Promise.all([loadPurpose(), loadLogic(), loadImpact(), loadContract(), loadDecisions(), loadHistory()])
}

async function saveDecision() {
  const text = draft.value.text.trim()
  if (!text) {
    note.value = '内容不能为空'
    return
  }
  const context = captureTarget()
  const kind = draft.value.kind
  try {
    await api.code.addDecision(context.projectId, {
      target: context.detail.id || `file:${context.detail.path}`,
      text,
      kind,
      session_id: context.detail.session_id || '',
    })
    if (!targetCurrent(context)) return
    draft.value.text = ''
    note.value = '已记入决策线程'
    await Promise.all([loadDecisions(), loadHistory()])
  } catch (e) {
    if (targetCurrent(context)) emit('error', e.message)
  }
}

async function removeDecision(item) {
  const context = captureTarget()
  try {
    await api.code.deleteDecision(context.projectId, item.id)
    if (!targetCurrent(context)) return
    await Promise.all([loadDecisions(), loadHistory()])
  } catch (e) {
    if (targetCurrent(context)) emit('error', e.message)
  }
}

const SOURCE_LABEL = { patch: '台账', task: '任务', batch: '并行车道', decision: '决策', commit: '提交' }

function jump(path, line, end_line) {
  emit('open-file', { path, line: line || 1, end_line: end_line || line || 1 })
}

function ask() {
  const d = props.detail
  const members = (d.members || []).map((m) => m.name).join('、')
  const importers = (d.importers || []).map((p) => String(p).replace(/^file:/, '')).join('、')
  const risk = impact.value?.risk
  const p = purpose.value
  const business = displayBusiness(p?.business)
  const sourceLine = d.line || purposeDocumentation.value?.line || 1
  const lines = [
    `请解释并给出改进建议：${d.kind === 'route' ? `${d.route_method || 'GET'} ${d.name}` : d.name}`,
    '',
    `[结构坐标] ${d.id}`,
    d.path ? `位置：${d.path}:${sourceLine}-${d.end_line || sourceLine}` : '',
    p?.available ? `作用（平台推导）：${p.headline}` : '',
    business.business_meaning ? `功能描述（${BUSINESS_STATUS_LABEL[business.status]}）：${business.business_meaning}` : '',
    purposeDocumentation.value?.text ? `详细说明（${purposeDocumentation.value.source || '作者说明'}）：\n${purposeDocumentation.value.text}` : '',
    (p?.facts || []).length ? `已知事实：\n- ${p.facts.slice(0, 8).map((f) => `${f.label}：${f.text}`).join('\n- ')}` : '',
    d.signature ? `签名：${d.signature}` : '',
    d.params?.length ? `参数：${d.params.join(', ')}` : '',
    d.returns ? `返回：${d.returns}` : '',
    d.bases?.length ? `继承：${d.bases.join(', ')}` : '',
    members ? `成员（${(d.members || []).length}）：${members}` : '',
    importers ? `被引用于：${importers}` : '',
    risk ? `影响面风险：${risk.level}（${risk.score}）· ${(risk.reasons || []).join('；')}` : '',
    (impact.value?.tests || []).length ? `关联测试：${impact.value.tests.join('、')}` : '',
    decisions.value.length
      ? `已知决定/风险：\n- ${decisions.value.slice(0, 5).map((i) => `${i.kind_label}：${i.text}`).join('\n- ')}`
      : '',
    '',
    '请先给结论，再给依据；如需读实现，用 read_file 精确读取上面的行区间。',
  ]
  emit('ask', lines.filter(Boolean).join('\n'))
}

watch(() => [props.projectId, props.detail?.id, props.detail?.path, props.detail?.tree], refresh, { immediate: true })
</script>

<template>
  <aside class="detail card">
    <header>
      <span class="kind" :style="{ color: meta.color }">{{ meta.label }}</span>
      <span class="name">{{ detail.name }}</span>
      <span class="spacer" />
      <button v-if="detail.path" class="ghost source-button" title="打开源码" aria-label="打开源码" @click="jump(detail.path, detail.line, detail.end_line)">↗</button>
      <button class="ghost" title="关闭" @click="emit('close')">×</button>
    </header>

    <div class="detail-scroll" tabindex="0" aria-label="节点详情内容">
    <div class="body">
      <div class="row mono loc" @click="jump(detail.path, detail.line, detail.end_line)">
        {{ loc }}<span v-if="detail.line" class="dim"> · {{ (detail.end_line || detail.line) - detail.line + 1 }} 行</span>
      </div>
      <div v-if="detail.confidence && detail.confidence !== 'high'" class="hint-warn">
        启发式解析（{{ detail.confidence }}）：结构与真实实现可能略有出入，以文件内容为准
      </div>
      <div v-if="detail.error" class="hint-err">{{ detail.error }}</div>

      <BusinessMeaning :project-id="projectId" :entity="detail" @saved="businessSaved" />

      <!-- 作用（P10）：先一句话说清"它是干什么的"，再列可追的依据。
           只给事实与标注过的命名推断；拿不准就说拿不准。 -->
      <section v-if="purpose" class="purpose">
        <div class="ph" @click="purposeOpen = !purposeOpen">
          <b>作用</b>
          <span class="spacer" />
          <span class="muted tiny">{{ purposeOpen ? '▾' : '▸' }}</span>
        </div>
        <template v-if="purposeOpen">
          <p v-if="purpose.available" class="head">{{ purpose.headline }}</p>
          <div v-else class="hint-warn">{{ purpose.reason || '暂时无法推导作用说明' }}</div>
          <section v-if="purposeDocumentation?.text" class="purpose-documentation" aria-label="详细说明">
            <div class="documentation-head"><b>详细说明</b><span>{{ purposeDocumentation.source || '作者说明' }}</span><button v-if="purposeDocumentation.line && detail.path" class="ghost" @click="jump(detail.path, purposeDocumentation.line, purposeDocumentation.line)">查看注释 :{{ purposeDocumentation.line }}</button></div>
            <p>{{ purposeDocumentation.text }}</p>
          </section>
          <ul v-if="purposeFacts.length" class="facts">
            <li v-for="(f, i) in purposeFacts" :key="i">
              <span class="fl">{{ f.label }}</span>
              <span class="ft" :class="{ jumpable: !!f.line }" @click="f.line && jump(purpose.path, f.line, f.line)">
                {{ f.text }}
              </span>
            </li>
          </ul>
          <div v-if="purpose.basis" class="basis">依据：{{ purpose.basis }}</div>
          <div v-if="(purpose.notes || []).length" class="basis toggle" @click="purposeNotes = !purposeNotes">
            边界与依据 {{ purposeNotes ? '▾' : '▸' }}
          </div>
          <template v-if="purposeNotes">
            <div v-for="(n, i) in purpose.notes || []" :key="`pn-${i}`" class="basis">· {{ n }}</div>
          </template>
        </template>
      </section>
      <div v-else-if="loading && !isDomainNode" class="description-loading" role="status">正在读取详细说明…</div>

      <p v-if="detail.signature" class="sig mono">{{ detail.signature }}</p>
      <p v-if="detail.doc && !purposeDocumentation" class="doc">{{ detail.doc }}</p>

      <div v-if="detail.bases?.length" class="tags">
        <span class="tag">继承 {{ detail.bases.join(', ') }}</span>
      </div>
      <div v-if="detail.params?.length" class="tags">
        <span v-for="p in detail.params" :key="p" class="tag mono">{{ p }}</span>
        <span v-if="detail.returns" class="tag">→ {{ detail.returns }}</span>
      </div>
      <div v-if="detail.decorators?.length" class="tags">
        <span v-for="d in detail.decorators" :key="d" class="tag mono">@{{ d }}</span>
      </div>
      <div v-if="detail.route_method" class="tags">
        <span class="tag route">{{ detail.route_method }} {{ detail.route_path }}</span>
      </div>
      <div v-if="detail.props?.length" class="props">
        <div class="sub">props</div>
        <div v-for="p in detail.props" :key="p.name" class="prop mono">
          {{ p.name }}<span class="dim">: {{ p.type || 'any' }}{{ p.required ? '' : ' ?' }}</span>
        </div>
      </div>
      <div v-if="detail.emits?.length" class="tags">
        <span v-for="e in detail.emits" :key="e" class="tag">emits {{ e }}</span>
      </div>

      <template v-if="detail.members?.length">
        <div class="sub">成员（{{ detail.members.length }}）</div>
        <ul class="list">
          <li v-for="m in detail.members" :key="m.id" @click="jump(m.path, m.line, m.end_line)">
            <span class="m-kind">{{ kindMeta(m.kind).short }}</span>
            <span class="m-name mono">{{ m.name }}</span>
            <span class="dim mono">:{{ m.line }}</span>
          </li>
        </ul>
      </template>

      <template v-if="detail.importers?.length">
        <div class="sub">被引用（{{ detail.importers.length }}）</div>
        <ul class="list">
          <li
            v-for="p in detail.importers"
            :key="p"
            @click="jump(String(p).replace(/^file:/, ''), 1, 1)"
          >
            <span class="m-kind">F</span>
            <span class="m-name mono">{{ String(p).replace(/^file:/, '') }}</span>
          </li>
        </ul>
      </template>

      <div v-if="detail.source">
        <div class="sub toggle" @click="showSource = !showSource">
          实现片段 {{ showSource ? '▾' : '▸' }}
        </div>
        <pre v-if="showSource" class="source mono">{{ detail.source }}</pre>
      </div>
    </div>

    <div class="tabs">
      <button :class="{ active: tab === 'logic' }" @click="tab = 'logic'">实现</button>
      <button :class="{ active: tab === 'impact' }" @click="tab = 'impact'">可达与风险</button>
      <button :class="{ active: tab === 'decisions' }" @click="tab = 'decisions'">
        决策{{ decisions.length ? ` ${decisions.length}` : '' }}
      </button>
      <button :class="{ active: tab === 'history' }" @click="tab = 'history'">溯源</button>
      <span class="spacer" />
      <button class="ghost" :disabled="!!loading" @click="refresh">↻</button>
    </div>

    <div class="panel" :class="{ 'panel-compact': isFileNode && tab === 'logic' }">
      <!-- 实现：这个函数按什么顺序做了什么事（P8；非 Python 如实不可用） -->
      <template v-if="tab === 'logic'">
        <!-- 模块节点没有"实现步骤"：它是一组文件的聚合，说清这一点比报一句
             「符号不存在」有用（后者会让人以为索引坏了）。 -->
        <div v-if="!isSymbolNode" class="hint-note">
          <template v-if="isDomainNode">
            这是<strong>能力域</strong>（功能视图里一组功能的聚合），不是代码实体，没有"实现步骤"。
            点开它下面某条<strong>功能</strong>就能看到这条功能是怎么实现的。
          </template>
          <template v-else-if="detail.feature?.kind === 'command'">
            这是<strong>命令入口</strong>（{{ detail.feature.label }} 的
            <span class="mono">__main__</span> 是裸调用、没有 main 函数），
            没有单独的步骤函数可看——它做的事就在这个文件里。
          </template>
          <template v-else-if="isFileNode">
            这是<strong>文件节点</strong>，上方展示完整说明与结构事实。展开文件下的类、函数或方法，可查看各自的实现步骤。
          </template>
          <template v-else>
            这是<strong>模块节点</strong>（一组文件的聚合），没有"实现步骤"。
            点开它下面的某个<strong>符号</strong>（函数 / 类 / 方法）才会看到步骤与调用关系。
          </template>
          <div v-if="contract?.is_dir && contract?.summary" class="muted tiny" style="margin-top:6px">
            {{ contract.summary }}
          </div>
        </div>
        <template v-else-if="logic?.available">
          <div class="counts">
            <span class="cchip">步骤 {{ logic.counts.steps }}</span>
            <span v-if="logic.counts.branches" class="cchip branch">分支 {{ logic.counts.branches }}</span>
            <span v-if="logic.counts.loops" class="cchip loop">循环 {{ logic.counts.loops }}</span>
            <span v-if="logic.counts.returns" class="cchip out">返回 {{ logic.counts.returns }}</span>
            <span v-if="logic.counts.raises" class="cchip err">抛错 {{ logic.counts.raises }}</span>
          </div>
          <ol class="steps">
            <li
              v-for="(s, i) in logic.steps"
              :key="`${s.line}-${i}`"
              :class="STEP_CLS[s.kind] || 'plain'"
              :style="{ paddingLeft: `${6 + s.depth * 14}px` }"
              @click="jump(logic.path, s.line, s.end_line || s.line)"
            >
              <span class="s-kind">{{ s.label }}</span>
              <span class="s-line mono">{{ s.line }}</span>
              <span class="s-text mono">{{ s.text || '（分支体）' }}</span>
            </li>
          </ol>
          <div v-if="logic.truncated" class="muted tiny">步骤过多，只显示前 {{ logic.steps.length }} 步</div>

          <div class="sub">它调用了谁（{{ logic.calls.length }}）</div>
          <ul v-if="logic.calls.length" class="list">
            <li v-for="c in logic.calls" :key="c.id" @click="jump(c.path, c.line, c.line)">
              <span class="m-kind">{{ kindMeta(c.kind).short }}</span>
              <span class="m-name mono">{{ c.name }}</span>
              <span class="muted tiny">{{ c.path }}</span>
            </li>
          </ul>
          <div v-else class="muted tiny">没有解析到索引里的调用（可能都是内置函数/库方法）</div>
          <div v-if="logic.unresolved_calls?.length" class="muted tiny">
            未解析（内置/第三方/方法调用）：{{ logic.unresolved_calls.join('、') }}
          </div>

          <div class="sub">被谁调用（{{ logic.callers.length }}）</div>
          <ul v-if="logic.callers.length" class="list">
            <li v-for="c in logic.callers" :key="c.path" @click="jump(c.path, 1, 1)">
              <span class="m-kind">F</span><span class="m-name mono">{{ c.path }}</span>
            </li>
          </ul>
          <div v-else class="muted tiny">索引里没有发现调用方</div>
          <div class="basis">{{ logic.note }}</div>
          <div class="basis">依据：{{ logic.basis }}</div>
        </template>
        <div v-else-if="logic" class="hint-warn">
          这个符号没有实现步骤：{{ logic.reason }}
        </div>
        <div v-else class="muted tiny">读取中…</div>
      </template>

      <!-- 静态可达（P6）：改这里会读到它的是谁（分数 + 理由 + 关联测试）。
           措辞刻意不是"影响/破坏面"：依据只有 import 与引用边（archify 的纪律：
           没有独立代码分析证据，就不要把图上的可达性说成运行时影响）。 -->
      <template v-else-if="tab === 'impact'">
        <div v-if="impact?.error" class="hint-err">{{ impact.error }}</div>
        <div v-else-if="impact?.risk" class="risk">
          <span class="badge" :class="impact.risk.level">
            {{ RISK_LABEL[impact.risk.level] || impact.risk.level }} · {{ impact.risk.score }}
          </span>
          <span class="muted tiny">
            静态可达 {{ impact.risk.signals.fan_in }} · 跨 {{ impact.risk.signals.files }} 文件
          </span>
        </div>
        <div v-if="impact?.basis" class="basis">依据：{{ impact.basis }}<template
          v-if="impact.not_runtime_impact">（不是运行时影响或故障预测）</template>
        </div>
        <ul v-if="impact?.risk?.reasons?.length" class="reasons">
          <li v-for="(r, i) in impact.risk.reasons" :key="i">{{ r }}</li>
        </ul>

        <!-- 契约（M32）：设计层在证据层上面——先说"这份文件被承诺了什么"，
             再说"谁守着它"。没有契约时也明说，因为"没有契约"本身就是信息。 -->
        <template v-if="contract">
          <div class="sub">契约</div>
          <div v-if="contract.error" class="hint-err">{{ contract.error }}</div>
          <!-- 模块节点：契约是"这个包下那么多实现各自的对照"，列出来才是信息 -->
          <div v-else-if="contract.is_dir" class="contract">
            <div class="muted tiny">{{ contractLine }}</div>
            <ul v-if="contract.items?.length" class="list">
              <li v-for="i in contract.items" :key="i.contract">
                <span class="m-kind">{{ i.ok ? '✓' : '✗' }}</span>
                <span class="m-name mono">{{ i.module }}</span>
                <span class="spacer" />
                <span class="muted tiny">
                  {{ i.coverage != null ? `证据 ${Math.round(i.coverage * 100)}%` : (i.note || '未对照') }}
                </span>
              </li>
            </ul>
            <div v-else class="muted tiny">
              这个包下的实现都没有 <span class="mono">.CONTRACT.md</span>：
              {{ contract.python_files }} 个文件里没有一份被写下来的形状/义务。
            </div>
          </div>
          <div v-else-if="!contract.has_contract" class="muted tiny">
            这个文件没有契约（同目录没有 <span class="mono">{{ detail.path }}</span> 对应的
            <span class="mono">.CONTRACT.md</span>）：形状/义务/数据/拓扑没有被写下来，
            也就没有东西可以对照。
          </div>
          <div v-else class="contract">
            <div class="th-head">
              <span class="tag" :class="contract.ok ? 'decision' : 'warn'">
                {{ contract.ok ? '契约一致' : '有缺口' }}
              </span>
              <span class="mono tiny">{{ contract.contract }}</span>
              <span class="spacer" />
              <span class="muted tiny">ρ {{ (contract.rho ?? 0).toFixed(2) }}</span>
            </div>
            <div class="muted tiny">{{ contractLine }}</div>
            <ul v-if="contract.checks?.length" class="list">
              <li v-for="c in contract.checks" :key="c.name">
                <span class="m-kind">{{ c.ok ? '✓' : '✗' }}</span>
                <span class="m-name">{{ c.name }}</span>
              </li>
            </ul>
            <!-- 只详述**第一个**未满足项：与门禁口径一致（摊开整张清单会让人挑好做的先做） -->
            <div v-if="contract.first_unsatisfied" class="hint-warn">
              第一个未满足项：{{ contract.first_unsatisfied }}
              <div v-if="(contract.checks || []).find((c) => c.name === contract.first_unsatisfied)?.detail"
                   class="muted tiny">
                {{ (contract.checks || []).find((c) => c.name === contract.first_unsatisfied).detail }}
              </div>
            </div>
            <div v-if="contract.declared_gaps?.length" class="muted tiny">
              契约明确声明无独立证据的义务（不是遗漏，但也别以为被验收了）：
              {{ contract.declared_gaps.join('、') }}
            </div>
          </div>
        </template>
        <template v-if="behaviors.length">
          <div class="sub">守着它的行为（{{ behaviors.length }}）</div>
          <ul class="thread">
            <li v-for="b in behaviors" :key="b.id">
              <div class="th-head">
                <span class="tag decision">测试</span>
                <span class="mono tiny" @click="jump(b.path, b.line, b.end_line)">{{ b.path }}:{{ b.line }}</span>
                <span class="spacer" />
                <span class="muted tiny">{{ b.asserts }} 断言</span>
              </div>
              <div class="th-text">{{ b.behavior }}</div>
              <div v-if="b.doc" class="muted tiny">{{ b.doc }}</div>
            </li>
          </ul>
        </template>
        <template v-else-if="impact && !impact.error">
          <div class="sub">守着它的行为</div>
          <div class="muted tiny">没有找到针对它的测试：这条行为目前没有可执行的断言在守。</div>
        </template>

        <template v-if="impact?.tests?.length">
          <div class="sub">关联测试文件（{{ impact.tests.length }}）</div>
          <ul class="list">
            <li v-for="t in impact.tests" :key="t" @click="jump(t, 1, 1)">
              <span class="m-kind">T</span><span class="m-name mono">{{ t }}</span>
            </li>
          </ul>
        </template>
        <div v-else-if="impact && !impact.error && !behaviors.length" class="muted tiny">
          没有找到关联测试：这次改动缺少可执行证据
        </div>
        <template v-if="impact?.reach?.files?.length">
          <div class="sub">静态可达到的文件（{{ impact.reach.files_total }}）</div>
          <ul class="list">
            <li v-for="f in impact.reach.files.slice(0, 20)" :key="f" @click="jump(f, 1, 1)">
              <span class="m-kind">F</span><span class="m-name mono">{{ f }}</span>
            </li>
          </ul>
        </template>
        <div v-if="impact?.reach?.modules?.length" class="muted tiny">
          跨模块：{{ impact.reach.modules.join('、') }}
        </div>
      </template>

      <!-- 决策线程：当初为什么这么定 -->
      <template v-else-if="tab === 'decisions'">
        <div class="draft">
          <select v-model="draft.kind">
            <option v-for="k in (decisionKinds.length ? decisionKinds : ['decision', 'risk', 'todo', 'note'])" :key="k" :value="k">
              {{ { decision: '决定', risk: '已知风险', todo: '待办', note: '备注' }[k] || k }}
            </option>
          </select>
          <textarea v-model="draft.text" rows="2" placeholder="当初为什么这么定？接受了什么风险？" />
          <div class="row">
            <button class="primary" @click="saveDecision">记下来</button>
            <span v-if="note" class="muted tiny">{{ note }}</span>
          </div>
        </div>
        <div v-if="!decisions.length" class="muted tiny">这条线程还是空的。</div>
        <ul class="thread">
          <li v-for="item in decisions" :key="item.id">
            <div class="th-head">
              <span class="tag" :class="item.kind">{{ item.kind_label }}</span>
              <span class="muted tiny">{{ item.author || '匿名' }} · {{ item.ts }}</span>
              <span class="spacer" />
              <button class="ghost tiny" title="删除" @click="removeDecision(item)">×</button>
            </div>
            <div class="th-text">{{ item.text }}</div>
          </li>
        </ul>
      </template>

      <!-- 溯源：谁在什么时候改过 -->
      <template v-else>
        <div v-if="history && !history.items?.length" class="muted tiny">还没有改动记录。</div>
        <ul class="timeline">
          <li v-for="(item, i) in history?.items || []" :key="i">
            <span class="src" :class="item.source">{{ SOURCE_LABEL[item.source] || item.source }}</span>
            <div class="tl-body">
              <div>{{ item.title }}</div>
              <div v-if="item.detail" class="muted tiny">{{ item.detail }}</div>
              <div class="muted tiny">
                {{ item.who || '—' }} · {{ item.ts }}
                <template v-if="item.reverted"> · 已被回滚</template>
              </div>
            </div>
          </li>
        </ul>
      </template>
    </div>

    </div>
    <footer>
      <button class="primary" @click="ask">问 AI</button>
      <button v-if="detail.path" class="ghost" @click="jump(detail.path, detail.line, detail.end_line)">打开源码</button>
    </footer>
  </aside>
</template>

<style scoped>
.detail {
  position: absolute; top: 10px; right: 10px; bottom: 10px; width: min(480px, calc(100% - 20px));
  display: flex; flex-direction: column; overflow: hidden; z-index: 6;
  box-shadow: var(--shadow-pop);
}
header {
  display: flex; flex: none; align-items: center; gap: var(--sp-2); min-height: 42px; padding: 8px 14px;
  border-bottom: 1px solid var(--border); font-size: var(--fs-sm);
}
.kind { font-size: var(--fs-xs); font-weight: var(--fw-bold); flex: none; }
.name { font-weight: var(--fw-semibold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spacer { flex: 1; }
.ghost { border: none; background: transparent; color: var(--muted); padding: var(--sp-0-5) var(--sp-1-5); }
.ghost:hover { background: var(--hover); }

.detail-scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.body { padding: var(--sp-2) var(--sp-2-5); font-size: var(--fs-sm); }
.row { display: flex; gap: var(--sp-1-5); }
.loc { cursor: pointer; color: var(--accent); font-size: var(--fs-xs); }
.loc { overflow-wrap: anywhere; }
.loc:hover { text-decoration: underline; }
.dim { color: var(--muted); }
.sig { margin: var(--sp-1-5) 0 0; font-size: var(--fs-xs); color: var(--text-2); word-break: break-all; }
.doc { margin: var(--sp-1-5) 0 0; color: var(--text-2); }
.tags { display: flex; flex-wrap: wrap; gap: var(--sp-1); margin-top: var(--sp-1-5); }
.tag { font-size: var(--fs-xs); padding: var(--sp-hair) var(--sp-1-5); border-radius: var(--r-sm); background: var(--hover); color: var(--text-2); }
.tag.route { background: #fee2e2; color: #b91c1c; }
.sub { margin: var(--sp-2-5) 0 var(--sp-1); font-size: var(--fs-xs); color: var(--muted); font-weight: var(--fw-semibold); }
.sub.toggle { cursor: pointer; }
.list { list-style: none; margin: 0; padding: 0; }
.list li {
  display: flex; gap: var(--sp-1-5); align-items: baseline; padding: 3px 5px; border-radius: var(--r-sm); cursor: pointer;
}
.list li:hover { background: var(--hover); }
.m-kind { font-size: var(--fs-2xs); color: var(--muted); font-weight: var(--fw-bold); width: 12px; flex: none; }
.m-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.props { margin-top: var(--sp-1-5); }
.prop { font-size: var(--fs-xs); }
.source {
  margin: var(--sp-1) 0 0; padding: var(--sp-2); background: #202735; color: #e5e9f1; border-radius: var(--r-sm);
  font-size: var(--fs-xs); max-height: 260px; overflow: auto; white-space: pre;
}
.hint-warn { margin-top: var(--sp-1-5); font-size: var(--fs-xs); color: var(--warn); background: var(--warn-soft); padding: 5px 7px; border-radius: var(--r-sm); }
/* 中性说明（不是故障、也不是警告）：模块节点没有实现步骤属于正常情况 */
.hint-note { margin-top: var(--sp-1-5); font-size: var(--fs-xs); color: var(--muted); background: var(--hover); padding: 7px 9px; border-radius: var(--r-sm); line-height: var(--lh-relaxed); }
.hint-err { margin-top: var(--sp-1-5); font-size: var(--fs-xs); color: var(--err); }

/* 作用（P10）：一句话结论 + 可追的依据。视觉上比结构信息更靠前、更醒目 */
.purpose {
  margin-top: var(--sp-2); padding: 7px var(--sp-2); border-radius: var(--r-sm);
  background: var(--panel-soft); border: 1px solid var(--border);
}
.purpose .ph { display: flex; align-items: center; gap: var(--sp-1-5); font-size: var(--fs-xs); cursor: pointer; }
.purpose .ph .spacer { flex: 1; }
.purpose .head { margin: var(--sp-1-5) 0 0; font-size: var(--fs-sm); color: #0f172a; line-height: var(--lh-normal); }
.purpose .facts { list-style: none; margin: var(--sp-1-5) 0 0; padding: 0; }
.purpose .facts li { display: flex; gap: var(--sp-1-5); font-size: var(--fs-xs); line-height: var(--lh-normal); }
.purpose .fl { flex: none; width: 58px; color: var(--muted); }
.purpose .ft { min-width: 0; color: var(--text-2); }
.purpose .ft.jumpable { cursor: pointer; }
.purpose .ft.jumpable:hover { text-decoration: underline; }
.purpose .basis { font-size: var(--fs-xs); }
.purpose .basis.toggle { cursor: pointer; color: var(--accent); }
.purpose-documentation { margin: 12px 0; padding: 11px 12px; border: 1px solid var(--border); border-radius: 7px; background: var(--panel); }
.documentation-head { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; font-size: 11px; }.documentation-head > span { color: var(--muted); font-size: 10px; }.documentation-head button { margin-left: auto; color: var(--accent); font-size: 10px; }
.purpose-documentation p { margin: 9px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; line-height: 1.8; color: var(--text-2); }.description-loading { margin-top: 12px; font-size: 12px; color: var(--muted); }
.purpose .ft { overflow-wrap: anywhere; }
.tabs { flex-wrap: wrap; }

.tabs { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; gap: var(--sp-1); padding: var(--sp-1) var(--sp-2); border-top: 1px solid var(--border); background: var(--panel); }
.tabs button { font-size: var(--fs-xs); padding: var(--sp-0-5) 7px; border-radius: var(--r-sm); border: 1px solid transparent; background: transparent; color: var(--muted); }
.tabs button.active { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-border); font-weight: var(--fw-semibold); }
.tabs .spacer { flex: 1; }
.panel { min-height: 100px; padding: var(--sp-1-5) var(--sp-2-5) var(--sp-2-5); font-size: var(--fs-xs); border-top: 1px solid var(--border); }
.panel-compact { min-height: 0; }
.risk { display: flex; align-items: center; gap: var(--sp-2); }
.badge { font-size: var(--fs-xs); font-weight: var(--fw-bold); padding: var(--sp-hair) 7px; border-radius: var(--r-sm); background: var(--hover); }
.badge.low { background: var(--ok-soft); color: var(--ok); }
.badge.medium { background: var(--warn-soft); color: var(--warn); }
.badge.high { background: #fff7ed; color: #c2410c; }
.badge.critical { background: var(--err-soft); color: var(--err); }
.reasons { margin: var(--sp-1-5) 0 0; padding-left: var(--sp-4); }
.reasons li { margin-bottom: var(--sp-0-5); color: var(--text-2); }
.basis { margin-top: var(--sp-1); font-size: var(--fs-xs); color: var(--muted); line-height: var(--lh-snug); }

/* 实现步骤（P8）：缩进=嵌套层级；颜色只标语义（分支/循环/返回/抛错） */
.counts { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: var(--sp-1-5); }
.cchip { font-size: var(--fs-xs); padding: var(--sp-hair) var(--sp-1-5); border-radius: var(--r-sm); background: var(--hover); color: var(--text-2); }
.cchip.branch { background: #eef2ff; color: #4338ca; }
.cchip.loop { background: #f5f3ff; color: #6d28d9; }
.cchip.out { background: var(--ok-soft); color: var(--ok); }
.cchip.err { background: var(--err-soft); color: var(--err); }
.steps { list-style: none; margin: 0 0 var(--sp-1-5); padding: 0; }
.steps li {
  display: flex; align-items: baseline; gap: var(--sp-1-5); padding: var(--sp-0-5) var(--sp-1-5); border-radius: var(--r-sm);
  cursor: pointer; border-left: 2px solid transparent;
}
.steps li:hover { background: #f5f8fc; }
.steps li.branch { border-left-color: #6366f1; }
.steps li.loop { border-left-color: #8b5cf6; }
.steps li.out { border-left-color: #10b981; }
.steps li.err { border-left-color: #ef4444; }
.steps li.try { border-left-color: #f59e0b; }
.steps li.call { border-left-color: #0ea5e9; }
.steps li.nested { border-left-color: var(--faint); }
.s-kind { flex: none; font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); background: var(--hover); color: var(--text-2); }
.s-line { flex: none; font-size: var(--fs-xs); color: var(--muted); width: 34px; text-align: right; }
.s-text { font-size: var(--fs-xs); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.draft { display: flex; flex-direction: column; gap: var(--sp-1); margin-bottom: var(--sp-2); }
.draft textarea { width: 100%; font-size: var(--fs-xs); }
.draft .row { display: flex; align-items: center; gap: var(--sp-1-5); }
.thread { list-style: none; margin: 0; padding: 0; }
.thread li { border-left: 2px solid var(--border); padding: var(--sp-0-5) 0 var(--sp-1-5) var(--sp-2); margin-bottom: var(--sp-1-5); }
.th-head { display: flex; align-items: center; gap: var(--sp-1-5); }
.th-text { color: var(--text); white-space: pre-wrap; }
.tag.decision { background: var(--accent-soft); color: var(--accent); }
.tag.risk { background: var(--err-soft); color: var(--err); }
.tag.todo { background: var(--warn-soft); color: var(--warn); }
/* M32 契约角标：一致用中性灰（不是成就，只是"对得上"），有缺口才用警示色 */
.tag.warn { background: var(--warn-soft); color: var(--warn); }
.contract .th-head { display: flex; align-items: center; gap: var(--sp-1-5); margin-top: var(--sp-1); }
.timeline { list-style: none; margin: 0; padding: 0; }
.timeline li { display: flex; gap: var(--sp-1-5); padding: 3px 0; }
.src { flex: none; font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); background: var(--hover); color: var(--muted); height: 16px; line-height: var(--h-chip); }
.src.patch { background: #eff6ff; color: var(--accent-hover); }
.src.task, .src.batch { background: var(--ok-soft); color: var(--ok); }
.src.decision { background: #f5f3ff; color: #6d28d9; }
.src.commit { background: var(--hover); color: var(--text-2); }
.tl-body { min-width: 0; }

footer {
  display: flex; flex: none; flex-wrap: wrap; gap: var(--sp-2); padding: var(--sp-2) var(--sp-2-5); border-top: 1px solid var(--border);
}
</style>
