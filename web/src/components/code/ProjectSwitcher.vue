<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import ProjectIcon from './ProjectIcon.vue'

const props = defineProps({
  project: { type: Object, default: null }, projects: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false }, phase: { type: String, default: 'idle' },
  error: { type: String, default: '' },
})
const emit = defineEmits(['open-folder', 'new-project', 'manual-open', 'select', 'close-project', 'remove'])
const id = useId()
const root = ref(null), trigger = ref(null), menu = ref(null), searchInput = ref(null)
const menuOpen = ref(false), query = ref(''), confirming = ref(null), position = ref({})
const busy = computed(() => props.loading || !['', 'idle'].includes(props.phase))
const branch = computed(() => props.project?.branch || props.project?.git_branch || props.project?.git?.branch || '')
const filteredProjects = computed(() => {
  const text = query.value.trim().toLocaleLowerCase()
  return (props.projects || []).filter(project => !text || `${projectName(project)} ${project.root_path || ''}`.toLocaleLowerCase().includes(text))
})
const phaseText = computed(() => ({ selecting: '等待选择文件夹…', picking: '等待选择文件夹…',
  opening: '正在打开工作区…', creating: '正在创建项目…', switching: '正在切换工作区…', removing: '正在移除记录…',
}[props.phase] || '正在加载…'))
function projectName(project) { return project?.name || project?.root_path?.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '选择项目' }
function positionMenu() {
  const anchor = trigger.value?.getBoundingClientRect()
  if (!anchor || !menuOpen.value) return
  const width = Math.min(Math.max(anchor.width, 320), Math.max(0, window.innerWidth - 24))
  const left = Math.min(Math.max(12, anchor.left), Math.max(12, window.innerWidth - width - 12))
  const below = window.innerHeight - anchor.bottom - 18, above = anchor.top - 18
  position.value = below < 230 && above > below
    ? { left: `${left}px`, width: `${width}px`, bottom: `${window.innerHeight - anchor.top + 6}px`, maxHeight: `${Math.min(560, Math.max(80, above))}px` }
    : { left: `${left}px`, width: `${width}px`, top: `${anchor.bottom + 6}px`, maxHeight: `${Math.min(560, Math.max(80, below))}px` }
}
function closeMenu(restoreFocus = false) {
  menuOpen.value = false; confirming.value = null
  if (restoreFocus) nextTick(() => trigger.value?.focus())
}
function openMenu() {
  if (busy.value) return
  menuOpen.value = true; query.value = ''; confirming.value = null
  positionMenu()
  nextTick(() => { positionMenu(); searchInput.value?.focus() })
}
function toggleMenu() { if (menuOpen.value) closeMenu(); else openMenu() }
function choose(project) { if (!busy.value) { closeMenu(true); emit('select', project) } }
function command(action) {
  if (busy.value) return
  closeMenu(true)
  if (action === 'open-folder') emit(action, props.project?.root_path || '')
  else emit(action)
}
function requestRemove(project) {
  if (busy.value || !project) return
  confirming.value = project
  nextTick(() => menu.value?.querySelector('[data-confirm-remove]')?.focus())
}
function cancelRemove() { confirming.value = null; nextTick(() => searchInput.value?.focus()) }
function confirmRemove() {
  const project = confirming.value
  if (busy.value || !project || !props.projects.some(row => row.id === project.id)) return
  confirming.value = null; emit('remove', project)
  nextTick(() => searchInput.value?.focus())
}
function menuKeydown(event) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeMenu(true); return }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  if (['Home', 'End'].includes(event.key) && event.target?.tagName === 'INPUT') return
  const items = Array.from(menu.value?.querySelectorAll('button:not([disabled]), input:not([disabled])') || [])
  if (!items.length) return
  event.preventDefault()
  const current = items.indexOf(document.activeElement)
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
    : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
  items[index].focus()
}
function triggerKeydown(event) {
  if (['ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); openMenu() }
  else if (event.key === 'Escape') closeMenu(true)
}
function outside(event) { if (menuOpen.value && !root.value?.contains(event.target) && !menu.value?.contains(event.target)) closeMenu() }
function onScroll(event) { if (menuOpen.value && !menu.value?.contains(event.target)) positionMenu() }
watch(() => props.project?.id, () => closeMenu())
watch(query, () => { confirming.value = null })
onMounted(() => {
  document.addEventListener('pointerdown', outside, true)
  document.addEventListener('focusin', outside)
  window.addEventListener('resize', positionMenu)
  window.addEventListener('scroll', onScroll, true)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', outside, true)
  document.removeEventListener('focusin', outside)
  window.removeEventListener('resize', positionMenu)
  window.removeEventListener('scroll', onScroll, true)
})
</script>

<template>
  <div ref="root" class="project-switcher">
    <button ref="trigger" class="project-switcher-trigger" type="button" aria-label="切换项目" aria-haspopup="dialog" :aria-expanded="menuOpen" :aria-controls="`${id}-project-menu`" :data-project-id="project?.id" :disabled="busy" :title="project ? `${projectName(project)}\n${project.root_path}` : '打开或创建一个项目'" @click="toggleMenu" @keydown="triggerKeydown">
      <span class="current-icon"><ProjectIcon name="folder" /></span>
      <span class="current-copy"><strong>{{ projectName(project) }}</strong><span v-if="busy">{{ phaseText }}</span><span v-else-if="project" class="current-detail"><span class="current-path">{{ project.root_path }}</span><span v-if="branch" class="current-branch"><ProjectIcon name="branch" />{{ branch }}</span></span><span v-else>打开文件夹，开始工作</span></span>
      <ProjectIcon name="chevron" class="trigger-chevron" :class="{ expanded: menuOpen }" />
    </button>
    <p v-if="error && !menuOpen" class="switcher-error" role="alert">{{ error }}</p>

    <Teleport to="body">
      <div v-if="menuOpen" :id="`${id}-project-menu`" ref="menu" class="project-menu" :style="position" role="dialog" aria-label="项目和工作区" @keydown="menuKeydown">
        <div class="menu-commands">
          <button type="button" :disabled="busy" aria-keyshortcuts="Control+Shift+O" @click="command('open-folder')"><ProjectIcon name="folder-open" /><span>打开文件夹…</span><kbd aria-hidden="true">Ctrl+Shift+O</kbd></button>
          <button type="button" :disabled="busy" @click="command('new-project')"><ProjectIcon name="plus" /><span>新建项目</span></button>
          <button type="button" :disabled="busy" @click="command('manual-open')"><ProjectIcon name="folder" /><span>输入工作区路径…</span></button>
        </div>
        <div class="menu-recents">
          <div class="recent-heading"><span>最近项目</span><span>{{ projects.length }}</span></div>
          <label class="project-search"><ProjectIcon name="search" /><input ref="searchInput" v-model="query" :disabled="busy" aria-label="搜索最近项目" placeholder="搜索项目名称或路径" autocomplete="off" /></label>
          <div class="menu-project-list">
            <div v-for="recent in filteredProjects" :key="recent.id || recent.root_path" class="menu-project" :class="{ current: recent.id === project?.id }">
              <button class="project-select" type="button" :data-project-id="recent.id" :aria-label="`切换到 ${projectName(recent)}`" :aria-current="recent.id === project?.id ? 'true' : undefined" :disabled="busy" :title="`${projectName(recent)}\n${recent.root_path}${recent.available === false ? '\n目录不可用' : ''}`" @click="choose(recent)"><ProjectIcon name="folder" /><span><span class="project-name-line"><strong>{{ projectName(recent) }}</strong><em v-if="recent.available === false" class="unavailable-label">目录不可用</em></span><small>{{ recent.root_path }}</small></span><ProjectIcon v-if="recent.id === project?.id" name="check" class="project-check" /></button>
              <button class="remove-shortcut" type="button" :disabled="busy" :aria-label="`从最近记录移除 ${projectName(recent)}`" title="从最近记录移除，保留项目文件" @click="requestRemove(recent)">移除</button>
            </div>
            <p v-if="!filteredProjects.length" class="menu-empty">{{ query ? '没有匹配的项目' : '还没有最近项目' }}</p>
          </div>
        </div>
        <div v-if="confirming" class="remove-confirm" role="group" aria-label="确认移除项目记录"><strong :title="projectName(confirming)">移除「{{ projectName(confirming) }}」？</strong><p>仅移除最近记录，项目文件会保留。</p><div><button type="button" :disabled="busy" @click="cancelRemove">取消</button><button data-confirm-remove type="button" :disabled="busy" @click="confirmRemove">移除记录</button></div></div>
        <div v-if="project" class="workspace-commands"><button type="button" :disabled="busy" @click="command('close-project')"><ProjectIcon name="close" />关闭工作区</button><button type="button" :disabled="busy" @click="requestRemove(project)">从最近记录移除…</button></div>
        <p v-if="error" class="menu-error" role="alert">{{ error }}</p>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.project-switcher { width: 100%; min-width: 0; padding: 8px; }
.project-switcher-trigger { display: flex; align-items: center; gap: 10px; width: 100%; min-width: 0; padding: 9px 7px; border: 1px solid transparent; background: transparent; text-align: left; border-radius: 8px; }
.project-switcher-trigger:hover:not(:disabled), .project-switcher-trigger[aria-expanded="true"] { background: var(--panel); border-color: var(--border); }
.current-icon { display: grid; place-items: center; width: 34px; height: 34px; flex: none; color: var(--accent); background: var(--panel); border: 1px solid var(--border); border-radius: 7px; }
.current-icon .project-icon { width: 18px; height: 18px; }.current-copy { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 5px; }
.current-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-sm); font-weight: var(--fw-semibold); color: var(--text); }
.current-copy > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: var(--fs-2xs); }
.current-detail { display: flex; align-items: center; gap: 7px; min-width: 0; }.current-path { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.current-branch { display: inline-flex; align-items: center; gap: 2px; flex: 0 1 auto; max-width: 38%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.current-branch .project-icon { width: 11px; height: 11px; }.trigger-chevron { width: 15px; height: 15px; color: var(--muted); transition: transform .15s; }.trigger-chevron.expanded { transform: rotate(180deg); }
.project-menu { position: fixed; z-index: 1400; box-sizing: border-box; display: flex; flex-direction: column; overflow: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 9px; box-shadow: 0 12px 36px #20273520, 0 2px 6px #20273508; color: var(--text); padding: 6px; }
.menu-commands { padding: 2px; }.menu-commands button, .workspace-commands button { display: flex; width: 100%; align-items: center; gap: 9px; background: transparent; border: 0; text-align: left; padding: 9px 10px; font-size: var(--fs-sm); min-width: 0; min-height: 35px; border-radius: 5px; }
.menu-commands button:hover:not(:disabled), .workspace-commands button:hover:not(:disabled) { background: var(--panel-soft); }.menu-commands .project-icon { color: var(--text-2); width: 17px; height: 17px; }
.menu-commands button > span { flex: 1; }.menu-commands kbd { background: transparent; border: 0; font: inherit; color: var(--muted); font-size: var(--fs-2xs); padding: 0; }
.menu-recents { border-top: 1px solid var(--border); margin-top: 4px; padding: 12px 2px 5px; }.recent-heading { display: flex; align-items: center; justify-content: space-between; padding: 0 9px 8px; color: var(--muted); font-size: var(--fs-2xs); }.recent-heading > span:first-child { font-weight: var(--fw-semibold); }
.project-search { display: flex; align-items: center; gap: 6px; margin: 0 7px 7px; padding: 6px 8px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 6px; }.project-search .project-icon { width: 14px; height: 14px; color: var(--muted); }
.project-search input { flex: 1; min-width: 0; background: transparent; border: 0; border-radius: 0; padding: 0; min-height: 20px; outline: none; font-size: var(--fs-2xs); }.project-search:focus-within { border-color: var(--accent-border); box-shadow: var(--focus-ring); }
.menu-project-list { max-height: 230px; overflow: auto; }.menu-project { display: flex; align-items: center; border-radius: 6px; }.menu-project:hover { background: var(--panel-soft); }.menu-project.current { background: var(--accent-soft); }
.project-select { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; padding: 9px 8px; border: 0; background: transparent; text-align: left; border-radius: 6px; }.project-select:hover:not(:disabled) { background: transparent; }.project-select > .project-icon { color: var(--muted); width: 16px; height: 16px; }
.project-select > span { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 4px; }.project-select strong, .project-select small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.project-select strong { font-size: var(--fs-xs); font-weight: var(--fw-medium); }.project-select small { color: var(--muted); font-size: var(--fs-2xs); }.project-select > .project-check { color: var(--accent); width: 14px; height: 14px; }
.project-name-line { display: flex; align-items: center; gap: 5px; min-width: 0; }.project-name-line strong { flex: 1; min-width: 0; }.unavailable-label { flex: none; color: var(--warn); font-style: normal; font-size: var(--fs-2xs); white-space: nowrap; }
.remove-shortcut { font-size: var(--fs-2xs); padding: 5px 7px; border: 0; background: transparent; color: var(--muted); opacity: 0; margin-right: 3px; flex: none; }.menu-project:hover .remove-shortcut, .menu-project:focus-within .remove-shortcut { opacity: 1; }.remove-shortcut:hover:not(:disabled) { background: var(--panel); color: var(--text); }
.menu-empty { padding: 13px 10px; margin: 0; color: var(--muted); text-align: center; font-size: var(--fs-2xs); }.workspace-commands { border-top: 1px solid var(--border); margin-top: 5px; padding: 5px 2px 1px; }.workspace-commands .project-icon { width: 15px; height: 15px; color: var(--muted); }.workspace-commands button:last-child { padding-left: 34px; color: var(--muted); font-size: var(--fs-2xs); }
.remove-confirm { margin: 6px; padding: 10px; border: 1px solid var(--accent-border); background: var(--panel-soft); border-radius: 7px; }.remove-confirm > strong { display: block; font-size: var(--fs-xs); font-weight: var(--fw-medium); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.remove-confirm p { font-size: var(--fs-2xs); line-height: 1.5; color: var(--muted); margin: 5px 0 9px; }.remove-confirm > div { display: flex; justify-content: flex-end; gap: 6px; }.remove-confirm button { font-size: var(--fs-2xs); padding: 5px 9px; }.remove-confirm [data-confirm-remove] { color: var(--accent); border-color: var(--accent-border); }
.menu-error { margin: 6px; padding: 9px; font-size: var(--fs-2xs); line-height: 1.5; color: var(--err); background: var(--err-soft, #fff2f2); border-radius: 6px; overflow-wrap: anywhere; }
.switcher-error { margin: 3px 12px 8px; color: var(--err); font-size: var(--fs-2xs); line-height: 1.5; overflow-wrap: anywhere; }
@media (hover: none) { .remove-shortcut { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .trigger-chevron { transition: none; } }
@media (max-width: 400px) { .project-switcher-trigger { padding: 9px; gap: 8px; }.current-branch { display: none; } }
</style>
