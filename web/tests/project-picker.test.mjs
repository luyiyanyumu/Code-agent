// Presentational project entry contracts: the shared controller owns all I/O.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, markRaw, nextTick, reactive, ref, watch } from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'

const settle = async () => { await nextTick(); await new Promise(setImmediate) }
const metadata = { native_picker: { available: true }, templates: ['empty', 'python', 'node'], projects_root: 'E:/projects' }
const recent = [{ id: 'a', name: 'alpha', root_path: 'E:/work/alpha' }, { id: 'b', name: '项目 beta', root_path: 'E:/work/beta' }]
let ids = 0
async function setup(t, name = 'ProjectPicker', overrides = {}) {
  const component = await readFile(new URL(`../src/components/code/${name}.vue`, import.meta.url), 'utf8')
  const { descriptor } = parse(component)
  const compiled = compileScript(descriptor, { id: name })
  const rendered = compileTemplate({ source: descriptor.template.content, filename: name, id: name, compilerOptions: { bindingMetadata: compiled.bindings } })
  assert.deepEqual(rendered.errors, [])
  const source = descriptor.scriptSetup.content.replace(/^import .*\r?\n/gm, '')
  const props = reactive({ meta: metadata, projects: recent, project: recent[0], loading: false, phase: 'idle', error: '', initialMode: 'open', compact: false, locationSelection: null, ...overrides })
  const events = [], mounted = [], unmounted = [], listeners = new Map(), windowListeners = new Map()
  const doc = { activeElement: null, addEventListener: (event, fn) => listeners.set(event, fn), removeEventListener: event => listeners.delete(event) }
  const win = { innerWidth: 1024, innerHeight: 768, addEventListener: (event, fn) => windowListeners.set(event, fn), removeEventListener: event => windowListeners.delete(event) }
  const context = vm.createContext({ computed, nextTick, reactive, ref, watch, document: doc, window: win,
    defineProps: () => props, defineEmits: () => (...args) => events.push(JSON.parse(JSON.stringify(args))),
    useId: () => `project-fixture-${++ids}`, onMounted: fn => mounted.push(fn), onBeforeUnmount: fn => unmounted.push(fn) })
  const exported = name === 'ProjectPicker'
    ? 'mode,path,name,template,gitInit,location,locationEdited,targetPath,setLocation,pickLocation,manualExpanded,pathInput,nameInput,openButton,busy,nativeAvailable,nativeReason,templates,manualVisible,canOpenPath,canCreate,phaseText,setMode,toggleManual,openFolder,openPath,createProject,selectProject'
    : 'root,trigger,menu,searchInput,menuOpen,query,confirming,position,busy,branch,filteredProjects,phaseText,positionMenu,openMenu,closeMenu,toggleMenu,choose,command,requestRemove,cancelRemove,confirmRemove,menuKeydown,triggerKeydown,outside'
  const scope = effectScope()
  scope.run(() => vm.runInContext(`${source}\nglobalThis.subject={${exported}}`, context))
  mounted.forEach(fn => fn()); await settle()
  t.after(() => { unmounted.forEach(fn => fn()); scope.stop() })
  const element = (attributes = {}) => { const node = { tagName: 'BUTTON', focused: 0, focus() { this.focused++; doc.activeElement = this }, contains: target => target === node, getBoundingClientRect: () => ({ left: 12, width: 300, top: 30, bottom: 90 }), ...attributes }; return markRaw(node) }
  return { s: context.subject, props, events, component, doc, win, listeners, windowListeners, element }
}

test('project entry components compile and use controller events without API calls or a custom directory browser', async t => {
  for (const name of ['ProjectPicker', 'ProjectSwitcher']) {
    const h = await setup(t, name)
    assert.doesNotMatch(h.component, /import.*api|api\.|fetch\(|DirPicker|fsList|loadMeta|pickFolder\(/)
    assert.deepEqual(h.events, [])
  }
})

test('native opening emits only the trimmed initial folder and leaves registration to the controller', async t => {
  const { s, events } = await setup(t)
  s.openFolder(); s.path.value = ' E:/existing '; s.openFolder()
  assert.deepEqual(events, [['open-folder', ''], ['open-folder', 'E:/existing']])
  assert.equal(s.path.value, ' E:/existing ')
  assert.equal(s.manualVisible.value, false)
})

test('manual opening is an independent trimmed-path action with empty input guarded', async t => {
  const { s, events } = await setup(t)
  s.openPath(); assert.deepEqual(events, [])
  s.path.value = ' E:/manual project '; s.openPath()
  assert.deepEqual(events, [['open-path', 'E:/manual project']])
})

test('manual input expands and focuses without launching a native window', async t => {
  const h = await setup(t), input = h.element({ tagName: 'INPUT' })
  h.s.pathInput.value = input
  h.s.toggleManual(); await settle()
  assert.equal(h.s.manualVisible.value, true); assert.equal(input.focused, 1)
  h.s.toggleManual(); assert.equal(h.s.manualVisible.value, false)
  assert.deepEqual(h.events, [])
})

test('remote and missing capabilities show manual entry and cannot emit native selection', async t => {
  const h = await setup(t, 'ProjectPicker', { meta: { native_picker: { available: false, reason: '远程服务请手填路径' }, templates: ['empty'] } })
  assert.equal(h.s.nativeAvailable.value, false); assert.equal(h.s.manualVisible.value, true)
  assert.equal(h.s.nativeReason.value, '远程服务请手填路径')
  h.s.openFolder(); assert.deepEqual(h.events, [])
  h.s.path.value = '/srv/project'; h.s.openPath()
  assert.deepEqual(h.events, [['open-path', '/srv/project']])
  assert.equal(h.s.template.value, 'empty')
})

test('create and open forms switch without side effects; create emits the complete presentation contract', async t => {
  const { s, events } = await setup(t)
  s.setMode('create'); s.createProject(); assert.deepEqual(events, [])
  s.name.value = ' demo '; s.template.value = 'node'; s.gitInit.value = false; s.createProject()
  assert.deepEqual(events, [['create', { name: 'demo', template: 'node', gitInit: false, location: 'E:/projects' }]])
  s.setMode('open'); assert.equal(s.mode.value, 'open'); assert.equal(s.name.value, ' demo ')
})

test('new initial mode and changed metadata are reflected by the reused entry component', async t => {
  const h = await setup(t, 'ProjectPicker', { initialMode: 'create', compact: true })
  assert.equal(h.s.mode.value, 'create')
  h.props.initialMode = 'open'; h.props.meta = { ...metadata, templates: ['empty'] }; await settle()
  assert.equal(h.s.mode.value, 'open'); assert.equal(h.s.template.value, 'empty')
  assert.deepEqual(h.events, [])
})

test('manual-open mode immediately expands the path field and focuses it in the reusable modal', async t => {
  const h = await setup(t, 'ProjectPicker', { initialMode: 'path', compact: true }), input = h.element({ tagName: 'INPUT' })
  h.s.pathInput.value = input
  assert.equal(h.s.mode.value, 'open'); assert.equal(h.s.manualVisible.value, true)
  h.props.initialMode = 'create'; await settle(); h.props.initialMode = 'path'; await settle()
  assert.equal(h.s.manualExpanded.value, true); assert.equal(h.doc.activeElement, input); assert.deepEqual(h.events, [])
})

test('loading and operation phases disable every presentation action', async t => {
  const { s, props, events } = await setup(t)
  s.path.value = 'E:/project'; s.name.value = 'demo'
  for (const phase of ['selecting', 'opening', 'creating', 'switching', 'removing']) {
    props.phase = phase
    s.openFolder(); s.openPath(); s.createProject(); s.selectProject(recent[0]); s.setMode('create')
    assert.equal(s.busy.value, true); assert.equal(s.canOpenPath.value, false); assert.equal(s.canCreate.value, false)
  }
  assert.deepEqual(events, []); assert.equal(s.mode.value, 'open')
  props.phase = 'idle'; props.loading = true; s.openFolder(); assert.deepEqual(events, [])
  props.loading = false; s.selectProject(recent[1]); assert.deepEqual(events, [['select', recent[1]]])
})

test('opening the project switcher is presentation-only and gives the search field focus', async t => {
  const h = await setup(t, 'ProjectSwitcher'), input = h.element({ tagName: 'INPUT' })
  h.s.searchInput.value = input
  h.s.openMenu(); await settle()
  assert.equal(h.s.menuOpen.value, true); assert.equal(input.focused, 1); assert.deepEqual(h.events, [])
  assert.match(h.component, /aria-label="切换项目"/); assert.match(h.component, /:aria-expanded="menuOpen"/)
  assert.match(h.component, /<Teleport to="body">/)
})

test('switcher search matches names and paths including Unicode', async t => {
  const h = await setup(t, 'ProjectSwitcher')
  h.s.query.value = ' BETA '; assert.deepEqual(h.s.filteredProjects.value.map(p => p.id), ['b'])
  h.s.query.value = '项目'; assert.deepEqual(h.s.filteredProjects.value.map(p => p.id), ['b'])
  h.s.query.value = 'work/alpha'; assert.deepEqual(h.s.filteredProjects.value.map(p => p.id), ['a'])
  h.s.query.value = 'absent'; assert.equal(h.s.filteredProjects.value.length, 0)
})

test('switcher commands have distinct controller events and project selection closes the menu', async t => {
  const h = await setup(t, 'ProjectSwitcher')
  for (const action of ['open-folder', 'new-project', 'manual-open', 'close-project']) { h.s.openMenu(); h.s.command(action); assert.equal(h.s.menuOpen.value, false) }
  h.s.openMenu(); h.s.choose(recent[1]); assert.equal(h.s.menuOpen.value, false)
  assert.deepEqual(h.events, [['open-folder', 'E:/work/alpha'], ['new-project'], ['manual-open'], ['close-project'], ['select', recent[1]]])
})

test('removing a recent record requires explicit second-step confirmation and never closes or deletes the workspace', async t => {
  const h = await setup(t, 'ProjectSwitcher')
  h.s.requestRemove(recent[1]); assert.equal(h.s.confirming.value.id, 'b'); assert.deepEqual(h.events, [])
  h.s.cancelRemove(); h.s.confirmRemove(); assert.deepEqual(h.events, [])
  h.s.requestRemove(recent[1]); h.s.confirmRemove()
  assert.deepEqual(h.events, [['remove', recent[1]]]); assert.equal(h.s.confirming.value, null)
  assert.match(h.component, /仅移除最近记录，项目文件会保留/)
})

test('changed search cancels removal confirmation and stale missing projects cannot be removed', async t => {
  const h = await setup(t, 'ProjectSwitcher')
  h.s.requestRemove(recent[1]); h.s.query.value = 'alpha'; await settle(); assert.equal(h.s.confirming.value, null)
  h.s.requestRemove(recent[1]); h.props.projects = [recent[0]]; h.s.confirmRemove(); assert.deepEqual(h.events, [])
})

test('busy switcher protects switching, native opening, creation, closing and removal', async t => {
  const h = await setup(t, 'ProjectSwitcher', { phase: 'opening' })
  h.s.openMenu(); h.s.command('open-folder'); h.s.command('new-project'); h.s.command('close-project'); h.s.choose(recent[1]); h.s.requestRemove(recent[1])
  assert.equal(h.s.menuOpen.value, false); assert.equal(h.s.confirming.value, null); assert.deepEqual(h.events, [])
})

test('Escape closes the menu and restores trigger focus; arrows cycle through enabled commands', async t => {
  const h = await setup(t, 'ProjectSwitcher'), first = h.element({ tagName: 'INPUT' }), second = h.element(), third = h.element(), trigger = h.element()
  h.s.trigger.value = trigger; h.s.menu.value = h.element({ querySelectorAll: () => [first, second, third] })
  h.s.openMenu(); first.focus()
  let prevented = 0, stopped = 0
  const key = name => ({ key: name, target: h.doc.activeElement, preventDefault: () => prevented++, stopPropagation: () => stopped++ })
  h.s.menuKeydown(key('ArrowDown')); assert.equal(h.doc.activeElement, second)
  h.s.menuKeydown(key('ArrowUp')); assert.equal(h.doc.activeElement, first)
  h.s.menuKeydown(key('ArrowUp')); assert.equal(h.doc.activeElement, third)
  h.s.menuKeydown(key('Escape')); await settle()
  assert.equal(h.s.menuOpen.value, false); assert.equal(h.doc.activeElement, trigger); assert.equal(prevented, 4); assert.equal(stopped, 1)
})

test('outside click or focus closes the menu while teleported menu and trigger interactions stay open', async t => {
  const h = await setup(t, 'ProjectSwitcher'), trigger = h.element(), inside = h.element()
  h.s.root.value = trigger; h.s.menu.value = inside; h.s.openMenu()
  h.listeners.get('pointerdown')({ target: inside }); assert.equal(h.s.menuOpen.value, true)
  h.listeners.get('focusin')({ target: trigger }); assert.equal(h.s.menuOpen.value, true)
  h.listeners.get('pointerdown')({ target: {} }); assert.equal(h.s.menuOpen.value, false)
})

test('teleported fixed menu fits the viewport on narrow screens and opens upward near the bottom', async t => {
  const h = await setup(t, 'ProjectSwitcher')
  h.s.trigger.value = h.element({ getBoundingClientRect: () => ({ left: 900, width: 340, top: 600, bottom: 650 }) })
  h.s.openMenu()
  assert.ok(parseFloat(h.s.position.value.left) + parseFloat(h.s.position.value.width) <= h.win.innerWidth - 12)
  assert.ok(h.s.position.value.bottom)
  h.win.innerWidth = 320; h.win.innerHeight = 600
  h.s.trigger.value = h.element({ getBoundingClientRect: () => ({ left: -50, width: 280, top: 40, bottom: 95 }) })
  h.s.positionMenu()
  assert.equal(h.s.position.value.left, '12px'); assert.equal(h.s.position.value.width, '296px'); assert.equal(h.s.position.value.top, '101px')
})

test('project identity change dismisses a stale menu and branch metadata can update without reopening', async t => {
  const h = await setup(t, 'ProjectSwitcher', { project: { ...recent[0], branch: 'main' } })
  assert.equal(h.s.branch.value, 'main'); h.s.openMenu()
  h.props.project = { ...recent[1], branch: 'feature/long-branch' }; await settle()
  assert.equal(h.s.menuOpen.value, false); assert.equal(h.s.branch.value, 'feature/long-branch'); assert.deepEqual(h.events, [])
})

test('shortcut metadata is announced independently of the native-open command name', async t => {
  const h = await setup(t, 'ProjectSwitcher')
  assert.match(h.component, /aria-keyshortcuts="Control\+Shift\+O"/)
  assert.match(h.component, /<kbd aria-hidden="true">Ctrl\+Shift\+O<\/kbd>/)
})

test('unavailable recent directories remain selectable and removable with a visible status marker', async t => {
  const unavailable = { ...recent[1], available: false }
  const entry = await setup(t, 'ProjectPicker', { projects: [unavailable] })
  entry.s.selectProject(unavailable); assert.deepEqual(entry.events, [['select', unavailable]])
  assert.match(entry.component, /project.available === false[\s\S]*class="unavailable-label">目录不可用/)
  const switcher = await setup(t, 'ProjectSwitcher', { projects: [unavailable] })
  switcher.s.choose(unavailable); switcher.s.requestRemove(unavailable); switcher.s.confirmRemove()
  assert.deepEqual(switcher.events, [['select', unavailable], ['remove', unavailable]])
  assert.match(switcher.component, /recent.available === false[\s\S]*class="unavailable-label">目录不可用/)
})

test('create location defaults from metadata, loads late and preserves manually edited parents across metadata refreshes', async t => {
  const h = await setup(t, 'ProjectPicker', { initialMode: 'create', meta: null })
  assert.equal(h.s.location.value, '')
  h.s.name.value = 'demo'; assert.equal(h.s.canCreate.value, false)
  h.props.meta = metadata; await settle(); assert.equal(h.s.location.value, 'E:/projects')
  h.s.setLocation(' E:/custom parent ')
  h.props.meta = { ...metadata, projects_root: 'E:/different-default' }; await settle()
  assert.equal(h.s.location.value, ' E:/custom parent ')
  h.s.createProject()
  assert.deepEqual(h.events, [['create', { name: 'demo', template: 'python', gitInit: true, location: 'E:/custom parent' }]])
})

test('browsing a create location only requests folder selection and cancellation preserves the complete form', async t => {
  const h = await setup(t, 'ProjectPicker', { initialMode: 'create' })
  h.s.name.value = 'keep-name'; h.s.template.value = 'node'; h.s.gitInit.value = false
  h.s.setLocation(' E:/keep-parent '); h.s.pickLocation()
  assert.deepEqual(h.events, [['pick-location', 'E:/keep-parent']])
  h.props.phase = 'selecting'; h.s.pickLocation(); h.s.createProject(); h.s.setMode('open')
  assert.equal(h.s.mode.value, 'create'); assert.equal(h.s.canCreate.value, false)
  h.props.locationSelection = null; h.props.phase = 'idle'; await settle()
  assert.equal(h.s.location.value, ' E:/keep-parent '); assert.equal(h.s.name.value, 'keep-name')
  assert.equal(h.s.template.value, 'node'); assert.equal(h.s.gitInit.value, false)
  assert.deepEqual(h.events, [['pick-location', 'E:/keep-parent']])
})

test('each native location selection updates the parent even when the chosen path repeats after a manual edit', async t => {
  const h = await setup(t, 'ProjectPicker', { initialMode: 'create' })
  h.s.name.value = 'keep-name'; h.s.template.value = 'node'; h.s.gitInit.value = false
  h.props.locationSelection = { path: 'E:\\chosen' }; await settle()
  assert.equal(h.s.location.value, 'E:\\chosen')
  h.s.setLocation('E:/manual-override')
  h.props.locationSelection = { path: 'E:\\chosen' }; await settle()
  assert.equal(h.s.location.value, 'E:\\chosen'); assert.equal(h.s.name.value, 'keep-name')
  assert.equal(h.s.template.value, 'node'); assert.equal(h.s.gitInit.value, false)
  assert.deepEqual(h.events, [])
})

test('final directory preview uses Windows drive/UNC or Unix separators and strips trailing separators once', async t => {
  const h = await setup(t, 'ProjectPicker', { initialMode: 'create' })
  h.s.name.value = ' demo '
  for (const [parent, expected] of [
    ['E:/projects//', 'E:\\projects\\demo'], ['C:\\', 'C:\\demo'],
    ['\\\\server\\share\\', '\\\\server\\share\\demo'],
    ['/srv/projects///', '/srv/projects/demo'], ['/', '/demo'],
    ['/srv/合法\\名称', '/srv/合法\\名称/demo'],
  ]) {
    h.s.setLocation(parent); assert.equal(h.s.targetPath.value, expected)
  }
  h.s.name.value = ''; assert.equal(h.s.targetPath.value, '')
})

test('empty parents cannot silently create in the default location and remote browsing remains manual', async t => {
  const h = await setup(t, 'ProjectPicker', { initialMode: 'create', meta: { ...metadata, native_picker: { available: false } } })
  h.s.name.value = 'demo'; h.s.setLocation('   ')
  assert.equal(h.s.canCreate.value, false); assert.equal(h.s.targetPath.value, '')
  h.s.createProject(); h.s.pickLocation(); assert.deepEqual(h.events, [])
  h.s.setLocation('/srv/custom'); assert.equal(h.s.canCreate.value, true)
  h.props.loading = true; h.s.createProject(); assert.deepEqual(h.events, [])
  h.props.loading = false; h.s.createProject()
  assert.deepEqual(h.events, [['create', { name: 'demo', template: 'python', gitInit: true, location: '/srv/custom' }]])
})
