<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  group: { type: Object, required: true },
  draft: { type: Object, default: () => ({}) },
  submitting: { type: Boolean, default: false },
  error: { type: String, default: '' },
})
const emit = defineEmits(['update:draft', 'answer', 'dismiss'])
const localError = ref(''), confirmDismiss = ref(false)
const questions = computed(() => props.group.questions || [])
const index = computed(() => Math.min(Math.max(0, Number(props.draft.index) || 0), Math.max(0, questions.value.length - 1)))
const current = computed(() => questions.value[index.value] || null)
const pending = computed(() => props.group.status === 'pending')
const statusLabel = computed(() => ({ pending: '等待你回答', answered: '已回答', dismissed: '已放弃', timeout: '已超时', cancelled: '已取消', inactive: '提问已结束' })[props.group.status] || '提问记录')
const errorText = computed(() => props.error || localError.value)
function draftFor(id) { return props.draft.answers?.[id] || { selected: [], custom: '', skipped: false } }
function patchDraft(id, patch) {
  localError.value = ''
  emit('update:draft', { ...props.draft, answers: { ...(props.draft.answers || {}), [id]: { ...draftFor(id), ...patch } } })
}
function go(step) {
  localError.value = ''
  emit('update:draft', { ...props.draft, index: Math.min(Math.max(0, index.value + step), questions.value.length - 1) })
}
function pick(option) {
  if (!pending.value || props.submitting || !current.value) return
  const q = current.value, selected = draftFor(q.id).selected || []
  const multi = q.multi_select === true || q.multiSelect === true
  patchDraft(q.id, { selected: multi ? (selected.includes(option.label) ? selected.filter(value => value !== option.label) : [...selected, option.label]) : [option.label], custom: multi ? draftFor(q.id).custom : '', skipped: false })
}
function setCustom(text) {
  if (!pending.value || props.submitting || !current.value) return
  const q = current.value, multi = q.multi_select === true || q.multiSelect === true
  patchDraft(q.id, { custom: text, ...(multi ? {} : { selected: [] }), skipped: false })
}
function completed(draft) { return draft.skipped === true || (draft.selected || []).length > 0 || !!String(draft.custom || '').trim() }
function skip() {
  if (!current.value || !pending.value || props.submitting) return
  localError.value = ''
  emit('update:draft', { ...props.draft, index: Math.min(index.value + 1, questions.value.length - 1),
    answers: { ...(props.draft.answers || {}), [current.value.id]: { selected: [], custom: '', skipped: true } } })
}
function submit() {
  if (!pending.value || props.submitting) return
  const missing = questions.value.findIndex(q => !completed(draftFor(q.id)))
  if (missing >= 0) {
    emit('update:draft', { ...props.draft, index: missing })
    localError.value = '请选择选项、填写答案，或明确跳过这道问题。'
    return
  }
  const answers = questions.value.map(q => {
    const draft = draftFor(q.id), custom = String(draft.custom || '').trim()
    if (draft.skipped) return { id: q.id, selected: [] }
    const selected = custom && !(q.multi_select === true || q.multiSelect === true) ? [] : [...(draft.selected || [])]
    return { id: q.id, selected, ...(custom ? { custom } : {}) }
  })
  localError.value = ''
  emit('answer', answers)
}
function advance() {
  if (!current.value || !pending.value || props.submitting) return
  if (!completed(draftFor(current.value.id))) { localError.value = '请选择选项、填写答案，或明确跳过这道问题。'; return }
  if (index.value < questions.value.length - 1) go(1)
  else submit()
}
function recommended(label) {
  const text = String(label || ''), suffix = /\s*(?:\((?:recommended|推荐)\)|（(?:recommended|推荐)）)\s*$/i
  return { label: text.replace(suffix, ''), recommended: suffix.test(text) }
}
function historyAnswer(question) {
  const answer = (props.group.answers || []).find(value => value.id === question.id)
  if (!answer) return ''
  const values = [...(answer.selected || []), ...(answer.custom ? [answer.custom] : [])]
  return values.join('；') || '已跳过'
}
function unsentDraft(question) {
  if (!['inactive', 'timeout', 'cancelled'].includes(props.group.status)) return ''
  const draft = draftFor(question.id), values = [...(draft.selected || []), ...(draft.custom ? [draft.custom] : [])]
  return values.join('；')
}
const endExplanation = computed(() => ({ inactive: '这次提问已失效，回答没有送达。可以重新发消息继续。', timeout: '回答时限已结束，本组问题不能再提交。', cancelled: '这次提问已随执行停止，回答没有送达。', dismissed: '你已明确放弃本组问题。' })[props.group.status] || '')
watch(() => props.group.status, () => { localError.value = ''; confirmDismiss.value = false })
</script>

<template>
  <section v-if="pending && current" class="question-request" :data-question-id="group.id || group.question_id" :aria-busy="submitting" aria-label="智能体提问">
    <header><strong>等待你回答</strong><span>{{ index + 1 }} / {{ questions.length }}</span></header>
    <div class="question-body">
      <span v-if="current.header" class="question-header">{{ current.header }}</span>
      <h3>{{ current.question }}</h3>
      <p v-if="current.multi_select || current.multiSelect" class="question-hint">可多选</p>
      <div v-if="current.options?.length" class="question-options" role="group" :aria-label="current.question">
        <button v-for="(option, i) in current.options" :key="`${i}:${option.label}`" type="button" class="question-option" :class="{ selected: (draftFor(current.id).selected || []).includes(option.label) }" :aria-pressed="(draftFor(current.id).selected || []).includes(option.label)" :disabled="submitting" @click="pick(option)">
          <span class="option-index">{{ i + 1 }}</span><span class="option-copy"><span>{{ recommended(option.label).label }}<small v-if="recommended(option.label).recommended" class="recommended">推荐</small></span><small v-if="option.description">{{ option.description }}</small></span>
        </button>
      </div>
      <label v-if="current.allow_custom !== false" class="question-custom"><span>{{ current.options?.length ? '也可以填写自己的答案' : '你的答案' }}</span><textarea :value="draftFor(current.id).custom || ''" :disabled="submitting" rows="2" :aria-label="`回答：${current.question}`" placeholder="填写你的答案…" @input="setCustom($event.target.value)" /></label>
      <p v-if="draftFor(current.id).skipped" class="question-hint">这道问题已明确跳过，不会代你选择答案。</p>
      <p v-if="errorText" class="question-error" role="alert">{{ errorText }}</p>
      <p v-if="confirmDismiss" class="dismiss-confirm">放弃后智能体会收到本组问题未获回答的通知。</p>
    </div>
    <footer>
      <div class="question-pagination"><button type="button" :disabled="submitting || index === 0" aria-label="上一道问题" @click="go(-1)">上一题</button><button v-if="index < questions.length - 1" type="button" :disabled="submitting" @click="advance">下一题</button><button type="button" :disabled="submitting" @click="skip">跳过本题</button></div>
      <div class="question-actions"><template v-if="confirmDismiss"><button type="button" :disabled="submitting" @click="confirmDismiss = false">继续回答</button><button type="button" class="danger" :disabled="submitting" @click="emit('dismiss')">确认放弃整组</button></template><template v-else><button type="button" :disabled="submitting" @click="confirmDismiss = true">放弃整组</button><button type="button" class="primary" :disabled="submitting" @click="submit">{{ submitting ? '正在提交…' : '提交回答' }}</button></template></div>
    </footer>
  </section>
  <details v-else class="question-history" :data-question-id="group.id || group.question_id" :open="!!error">
    <summary><strong>{{ statusLabel }}</strong><span>{{ questions[0]?.question || '智能体提问' }}</span></summary>
    <p v-if="endExplanation">{{ endExplanation }}</p>
    <p v-if="error" class="question-error" role="alert">{{ error }}</p>
    <div v-for="question in questions" :key="question.id"><p>{{ question.question }}</p><p v-if="historyAnswer(question)" class="history-answer">{{ historyAnswer(question) }}</p><p v-else-if="unsentDraft(question)" class="unsent-draft">未送出的回答草稿：{{ unsentDraft(question) }}</p></div>
  </details>
</template>

<style scoped>
.question-request { min-width: 0; display: flex; flex-direction: column; border: 1px solid var(--accent-border, var(--border)); border-radius: 8px; background: var(--panel); color: var(--text); font-size: var(--fs-xs); }
.question-request header { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 12px; border-bottom: 1px solid var(--border); color: var(--accent); }.question-request header span { margin-left: auto; color: var(--muted); font-size: var(--fs-2xs); }
.question-body { padding: 12px; min-width: 0; }.question-header { color: var(--muted); font-size: var(--fs-2xs); }h3 { margin: 3px 0 10px; font-size: var(--fs-sm); font-weight: var(--fw-semibold); white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }p { margin: 8px 0 0; overflow-wrap: anywhere; }.question-hint { color: var(--muted); font-size: var(--fs-2xs); }
.question-options { display: flex; flex-direction: column; gap: 7px; }.question-option { display: flex; gap: 8px; align-items: flex-start; width: 100%; padding: 9px; min-height: 38px; text-align: left; color: var(--text); background: var(--panel-soft); border: 1px solid var(--border); border-radius: 6px; font-size: var(--fs-xs); }.question-option.selected { background: var(--accent-soft); border-color: var(--accent); }.option-index { flex: none; color: var(--muted); font-size: var(--fs-2xs); }.option-copy { display: flex; flex-direction: column; gap: 4px; min-width: 0; overflow-wrap: anywhere; }.option-copy > small { color: var(--muted); font-size: var(--fs-2xs); line-height: 1.6; }.recommended { margin-left: 7px; font-size: var(--fs-2xs); color: var(--accent); }
.question-custom { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; color: var(--muted); font-size: var(--fs-2xs); }.question-custom textarea { box-sizing: border-box; width: 100%; min-height: 58px; resize: vertical; border: 1px solid var(--border-strong); border-radius: 6px; padding: 8px; background: var(--panel); color: var(--text); font: inherit; font-size: var(--fs-xs); line-height: 1.7; }.question-error { color: var(--err); }.dismiss-confirm { color: var(--warn); }
footer { display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 12px; border-top: 1px solid var(--border); }footer button { min-height: 30px; padding: 5px 8px; font-size: var(--fs-2xs); }.question-pagination,.question-actions { display: flex; flex-wrap: wrap; gap: 6px; }.question-actions { margin-left: auto; }.question-request button:focus-visible,.question-custom textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.question-history { padding: 9px 10px; min-width: 0; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); background: var(--panel-soft); font-size: var(--fs-2xs); }.question-history summary { cursor: pointer; overflow-wrap: anywhere; line-height: 1.7; }.question-history summary strong { margin-right: 7px; }.question-history > div { margin-top: 10px; }.question-history p { white-space: pre-wrap; line-height: 1.7; }.history-answer { color: var(--text); }.unsent-draft { color: var(--muted); }
@media (max-width: 380px) { .question-actions { margin-left: 0; width: 100%; }.question-actions .primary { margin-left: auto; } }
</style>
