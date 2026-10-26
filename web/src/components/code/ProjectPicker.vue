<script setup>
import { computed, nextTick, onMounted, ref, useId, watch } from 'vue'
import ProjectIcon from './ProjectIcon.vue'

const props = defineProps({
  meta: { type: Object, default: null }, projects: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false }, phase: { type: String, default: 'idle' },
  error: { type: String, default: '' }, initialMode: { type: String, default: 'open' },
  compact: { type: Boolean, default: false },
  locationSelection: { type: Object, default: null },
})
const emit = defineEmits(['open-folder', 'open-path', 'pick-location', 'create', 'select', 'close'])
const id = useId()
const mode = ref(props.initialMode === 'create' ? 'create' : 'open')
const path = ref(''), name = ref(''), template = ref('python'), gitInit = ref(true)
const location = ref(props.meta?.projects_root || ''), locationEdited = ref(false)
const manualExpanded = ref(props.initialMode === 'path')
const pathInput = ref(null), nameInput = ref(null), openButton = ref(null)
const busy = computed(() => props.loading || !['', 'idle'].includes(props.phase))
const nativeAvailable = computed(() => props.meta?.native_picker?.available === true)
const nativeReason = computed(() => props.meta?.native_picker?.reason || '输入服务所在电脑上的项目文件夹路径。')
const templates = computed(() => props.meta?.templates?.length ? props.meta.templates : ['empty', 'python', 'node'])
const manualVisible = computed(() => manualExpanded.value || !nativeAvailable.value)
const canOpenPath = computed(() => !!path.value.trim() && !busy.value)
const canCreate = computed(() => !!name.value.trim() && !!location.value.trim() && !busy.value)
const targetPath = computed(() => {
  const parent = location.value.trim(), child = name.value.trim()
  if (!parent || !child) return ''
  const windows = /^[a-z]:[\\/]/i.test(parent) || /^\\\\/.test(parent)
  const base = windows ? parent.replace(/\//g, '\\').replace(/\\+$/, '') : parent.replace(/\/+$/, '')
  return `${base}${windows ? '\\' : '/'}${child}`
})
const phaseText = computed(() => ({
  picking: '正在等待系统文件夹窗口，请选择项目目录。', selecting: '正在等待系统文件夹窗口，请选择项目目录。',
  opening: '正在打开工作区…', creating: '正在创建项目…', switching: '正在切换工作区…',
  removing: '正在更新最近项目…', loading: '正在加载工作区…',
}[props.phase] || (props.loading ? '正在加载工作区…' : '正在处理…')))

watch(() => props.initialMode, value => { mode.value = value === 'create' ? 'create' : 'open'; manualExpanded.value = value === 'path'; focusForm() })
watch(templates, values => { if (!values.includes(template.value)) template.value = values.includes('python') ? 'python' : values[0] }, { immediate: true })
watch(() => props.meta?.projects_root, value => { if (!locationEdited.value) location.value = value || '' })
// Watch the complete object: choosing the same path again must replace a manually edited value.
watch(() => props.locationSelection, selection => {
  if (typeof selection?.path === 'string' && selection.path.trim()) {
    location.value = selection.path.trim(); locationEdited.value = true
  }
}, { immediate: true })

function focusForm() { nextTick(() => (mode.value === 'create' ? nameInput.value : manualVisible.value ? pathInput.value : openButton.value)?.focus()) }
function setMode(value) { if (!busy.value) { mode.value = value; focusForm() } }
function toggleManual() { manualExpanded.value = !manualExpanded.value; if (manualExpanded.value) nextTick(() => pathInput.value?.focus()) }
function openFolder() { if (!busy.value && nativeAvailable.value) emit('open-folder', path.value.trim()) }
function openPath() { if (canOpenPath.value) emit('open-path', path.value.trim()) }
function setLocation(value) { location.value = value; locationEdited.value = true }
function pickLocation() { if (!busy.value && nativeAvailable.value) emit('pick-location', location.value.trim()) }
function createProject() { if (canCreate.value) emit('create', { name: name.value.trim(), template: template.value, gitInit: gitInit.value, location: location.value.trim() }) }
function selectProject(project) { if (!busy.value) emit('select', project) }
function templateLabel(value) { return ({ empty: '空项目', python: 'Python 项目', node: 'Node.js 项目' })[value] || value }
function projectName(project) { return project.name || project.root_path?.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '未命名项目' }
onMounted(() => { if (props.compact) focusForm() })
</script>

<template>
  <section class="workspace-entry" :class="{ compact, creating: mode === 'create' }" :aria-busy="busy">
    <header class="entry-header">
      <span class="workspace-mark"><ProjectIcon name="workspace" /></span>
      <div class="entry-heading">
        <span v-if="!compact" class="eyebrow">代码工作区</span>
        <h2>{{ mode === 'create' ? '创建新项目' : compact ? '打开工作区' : '打开你的工作区' }}</h2>
        <p>{{ mode === 'create' ? '选择模板，在本机创建一个新的项目。' : '选择项目文件夹，开始浏览、运行和协作。' }}</p>
      </div>
    </header>

    <div v-if="mode === 'open'" class="entry-content">
      <div v-if="nativeAvailable" class="entry-actions">
        <button ref="openButton" class="primary open-folder" type="button" :disabled="busy" @click="openFolder"><ProjectIcon name="folder-open" />打开文件夹</button>
        <button class="new-project" type="button" :disabled="busy" @click="setMode('create')"><ProjectIcon name="plus" />新建项目</button>
      </div>
      <button v-if="nativeAvailable" class="manual-toggle" type="button" :disabled="busy" :aria-expanded="manualExpanded" :aria-controls="`${id}-manual-path`" @click="toggleManual">输入工作区路径<ProjectIcon name="chevron" :class="{ rotated: manualExpanded }" /></button>
      <form v-if="manualVisible" :id="`${id}-manual-path`" class="path-form" @submit.prevent="openPath">
        <label :for="`${id}-path`">工作区路径</label>
        <div class="path-row">
          <input :id="`${id}-path`" ref="pathInput" v-model="path" :disabled="busy" placeholder="例如 E:\projects\my-app" autocomplete="off" spellcheck="false" />
          <button :class="{ primary: !nativeAvailable }" :disabled="!canOpenPath" type="submit">打开工作区</button>
        </div>
        <p v-if="!nativeAvailable && meta" class="helper">{{ nativeReason }}</p>
      </form>
      <div v-if="!nativeAvailable" class="manual-actions"><button class="new-project" type="button" :disabled="busy" @click="setMode('create')"><ProjectIcon name="plus" />新建项目</button></div>
      <div v-if="projects.length" class="recent-projects">
        <div class="section-heading"><span>最近打开</span><span>{{ projects.length }}</span></div>
        <div class="recent-list">
          <button v-for="project in projects" :key="project.id || project.root_path" class="recent-project" type="button" :disabled="busy" :title="`${projectName(project)}\n${project.root_path}${project.available === false ? '\n目录不可用' : ''}`" @click="selectProject(project)">
            <span class="recent-icon"><ProjectIcon name="folder" /></span>
            <span class="project-copy"><span class="project-name-line"><strong>{{ projectName(project) }}</strong><em v-if="project.available === false" class="unavailable-label">目录不可用</em></span><span>{{ project.root_path }}</span></span>
            <span class="recent-open">打开</span>
          </button>
        </div>
      </div>
      <p v-else-if="nativeAvailable && !busy" class="empty-note">在系统文件夹窗口中选择现有项目，即可开始。</p>
    </div>

    <form v-else class="create-form" @submit.prevent="createProject">
      <button class="back-action" type="button" :disabled="busy" @click="setMode('open')"><ProjectIcon name="back" />返回打开工作区</button>
      <label class="field" :for="`${id}-name`"><span>项目名称</span><input :id="`${id}-name`" ref="nameInput" v-model="name" :disabled="busy" placeholder="例如 demo-service" autocomplete="off" required /></label>
      <label class="field" :for="`${id}-template`"><span>项目模板</span><select :id="`${id}-template`" v-model="template" :disabled="busy"><option v-for="value in templates" :key="value" :value="value">{{ templateLabel(value) }}</option></select></label>
      <div class="field location-field">
        <label :for="`${id}-location`">项目位置</label>
        <div class="location-row"><input :id="`${id}-location`" :value="location" :disabled="busy" placeholder="选择项目的父文件夹" autocomplete="off" spellcheck="false" required @input="setLocation($event.target.value)" /><button v-if="nativeAvailable" type="button" :disabled="busy" @click="pickLocation"><ProjectIcon name="folder-open" />浏览…</button></div>
        <p class="location-hint">新项目会作为子文件夹创建在此位置。</p>
      </div>
      <label class="git-option"><input v-model="gitInit" type="checkbox" :disabled="busy" /><span>初始化 Git 仓库<small>建立初始提交，方便后续版本管理。</small></span></label>
      <div v-if="targetPath" class="creation-location"><span>最终目录</span><code :title="targetPath">{{ targetPath }}</code></div>
      <button class="primary create-button" type="submit" :disabled="!canCreate"><ProjectIcon name="plus" />创建项目</button>
    </form>
    <p v-if="busy" class="operation-status" role="status"><span class="spinner" />{{ phaseText }}</p>
    <p v-if="error" class="entry-error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.workspace-entry { width: min(calc(100% - 64px), 680px); max-height: calc(100% - 64px); flex: 0 1 auto; overflow: auto; margin: auto; padding: 36px; box-sizing: border-box; color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 6px 24px #20273506; }
.entry-header { display: flex; align-items: center; gap: 16px; margin-bottom: 28px; }
.workspace-mark { display: grid; place-items: center; width: 54px; height: 54px; flex: none; border-radius: 12px; background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent-border); }
.workspace-mark .project-icon { width: 28px; height: 28px; }
.entry-heading { min-width: 0; }
.eyebrow { display: block; margin-bottom: 5px; color: var(--accent); font-size: var(--fs-2xs); font-weight: var(--fw-semibold); letter-spacing: .06em; }
.entry-heading h2 { font-size: var(--fs-2xl); font-weight: var(--fw-semibold); letter-spacing: -.025em; margin: 0; line-height: 1.35; }
.entry-heading p { color: var(--muted); margin: 8px 0 0; font-size: var(--fs-sm); line-height: 1.6; }
.entry-actions { display: flex; align-items: center; gap: 10px; }
.entry-actions button, .new-project, .create-button { display: inline-flex; gap: 8px; align-items: center; justify-content: center; min-height: 38px; padding: 9px 16px; border-radius: 6px; font-size: var(--fs-sm); }
.open-folder { box-shadow: 0 2px 4px #355dfb18; }
.manual-toggle { display: inline-flex; align-items: center; gap: 4px; color: var(--muted); border: 0; padding: 0; margin-top: 19px; min-height: 28px; background: transparent; font-size: var(--fs-xs); }
.manual-toggle:hover:not(:disabled), .back-action:hover:not(:disabled) { color: var(--accent); background: transparent; }
.manual-toggle .project-icon { width: 14px; height: 14px; transition: transform .15s; }
.rotated { transform: rotate(180deg); }
.path-form { margin-top: 12px; }
.path-form > label, .field > span, .field > label { color: var(--text-2); font-size: var(--fs-xs); font-weight: var(--fw-medium); }
.path-row { display: flex; gap: 8px; margin-top: 7px; }
.path-row input { min-width: 0; flex: 1; font-size: var(--fs-sm); }
.path-row button { flex: none; font-size: var(--fs-xs); white-space: nowrap; }
.helper, .empty-note { color: var(--muted); font-size: var(--fs-xs); line-height: 1.6; margin: 10px 0 0; }
.empty-note { margin-top: 27px; }.manual-actions { margin-top: 14px; }
.recent-projects { margin-top: 30px; padding-top: 22px; border-top: 1px solid var(--border); }
.section-heading { display: flex; justify-content: space-between; color: var(--muted); font-size: var(--fs-2xs); margin-bottom: 10px; }
.section-heading > span:first-child { font-weight: var(--fw-semibold); letter-spacing: .02em; }
.recent-list { max-height: 260px; overflow: auto; margin: 0 -10px; }
.recent-project { display: flex; align-items: center; gap: 12px; width: 100%; min-width: 0; text-align: left; border: 0; border-radius: 6px; background: transparent; padding: 12px 10px; }
.recent-project:hover:not(:disabled) { background: var(--panel-soft); }.recent-icon { display: grid; place-items: center; width: 32px; height: 32px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 6px; color: var(--muted); flex: none; }
.project-copy { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.project-copy strong { font-size: var(--fs-sm); font-weight: var(--fw-medium); }.project-copy > span { color: var(--muted); font-size: var(--fs-2xs); }
.project-copy > * { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-name-line { display: flex; align-items: center; gap: 7px; min-width: 0; }.project-name-line strong { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; color: var(--text); }.unavailable-label { flex: none; color: var(--warn); font-style: normal; font-size: var(--fs-2xs); }
.recent-open { flex: none; color: var(--accent); font-size: var(--fs-2xs); opacity: 0; }
.recent-project:hover .recent-open, .recent-project:focus-visible .recent-open { opacity: 1; }
.create-form { display: flex; flex-direction: column; gap: 18px; }
.back-action { display: inline-flex; align-items: center; gap: 6px; align-self: flex-start; padding: 0; min-height: 26px; background: transparent; border: 0; font-size: var(--fs-xs); color: var(--muted); }
.back-action .project-icon { width: 15px; height: 15px; }.field { display: flex; flex-direction: column; gap: 7px; min-width: 0; }
.field input, .field select { min-height: 38px; font-size: var(--fs-sm); width: 100%; box-sizing: border-box; }.creating .entry-header { margin-bottom: 22px; }.creating .back-action { margin-top: -3px; margin-bottom: 1px; }
.location-row { display: flex; gap: var(--sp-2); min-width: 0; }.location-row input { flex: 1; min-width: 0; }.location-row button { display: inline-flex; align-items: center; gap: var(--sp-1-5); flex: none; white-space: nowrap; font-size: var(--fs-xs); }
.location-row .project-icon { width: 16px; height: 16px; }.location-hint { color: var(--muted); font-size: var(--fs-2xs); line-height: var(--lh-snug); margin: 0; }
.git-option { display: flex; gap: 8px; color: var(--text-2); font-size: var(--fs-xs); line-height: 1.5; }.git-option input { margin-top: 3px; flex: none; }
.git-option small { display: block; color: var(--muted); font-size: var(--fs-2xs); margin-top: 2px; }
.creation-location { display: flex; flex-direction: column; gap: 5px; color: var(--muted); font-size: var(--fs-2xs); padding: 12px 14px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 6px; }
.creation-location code { color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-2xs); }
.create-button { align-self: flex-start; margin-top: 2px; }
.operation-status { display: flex; align-items: center; gap: 8px; margin: 18px 0 0; color: var(--muted); font-size: var(--fs-xs); line-height: 1.5; }
.spinner { width: 13px; height: 13px; flex: none; border: 2px solid var(--accent-border); border-top-color: var(--accent); border-radius: 50%; animation: entry-spin .8s linear infinite; }
.entry-error { padding: 10px 12px; border-radius: 6px; color: var(--err); background: var(--err-soft, #fff2f2); font-size: var(--fs-xs); line-height: 1.6; margin: 16px 0 0; overflow-wrap: anywhere; }
.compact { width: 100%; max-height: none; padding: 30px; margin: 0; border: 0; border-radius: 0; box-shadow: none; }.compact .entry-header { gap: 12px; margin-bottom: 21px; }
.compact .workspace-mark { width: 40px; height: 40px; border-radius: 11px; }.compact .workspace-mark .project-icon { width: 24px; height: 24px; }
.compact .entry-heading h2 { font-size: var(--fs-lg); }.compact .entry-heading p { margin-top: 4px; font-size: var(--fs-xs); }
.compact .recent-projects { margin-top: 23px; padding-top: 17px; }.compact .recent-list { max-height: 200px; }
@keyframes entry-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinner { animation: none; }.manual-toggle .project-icon { transition: none; } }
@media (max-width: 600px) { .workspace-entry { width: calc(100% - 28px); max-height: calc(100% - 28px); padding: 24px 20px; }.entry-header { gap: 13px; }.workspace-mark { width: 48px; height: 48px; border-radius: 13px; }.workspace-mark .project-icon { width: 27px; height: 27px; }.entry-heading h2 { font-size: var(--fs-xl); }.compact { width: 100%; max-height: none; padding: 24px 20px; }.entry-actions { flex-wrap: wrap; } }
@media (max-width: 380px) { .path-row { flex-direction: column; }.path-row button { align-self: flex-start; }.entry-heading h2 { font-size: var(--fs-lg); }.workspace-mark { width: 40px; height: 40px; }.recent-open { display: none; } }
</style>
