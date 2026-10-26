// Offline regressions for workspace races and editor/LSP cleanup.
// Run: node --test web/tests/code-workspace.test.mjs
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { documentKey, useWorkbench } from '../src/composables/useWorkbench.js'
import { useNavigation } from '../src/composables/useNavigation.js'
import { useProjectAccess } from '../src/composables/useProjectAccess.js'
import { useIndexRefresh } from '../src/composables/useIndexRefresh.js'
import { useFunctionDescriptions } from '../src/composables/useFunctionDescriptions.js'
import { prepareWorkspaceEdit, assertPlanCurrent, findPlanDocument } from '../src/lib/workspaceEdits.js'
import { WorkspaceLanguageService } from '../src/lib/workspaceLanguageService.js'
import { createDialogService } from '../src/composables/useDialog.js'
import { useProjectAgentTeam } from '../src/lib/agentTeam.js'
import { useProjectConversations } from '../src/composables/useProjectConversations.js'
import { clearSemanticTokens, getJavaTokenTarget, registerTokenBusinessSource, subscribeHoverDocumentationUpdates, withTokenBusiness } from '../src/lib/tokenHover.js'
import { createBusinessMeaningSource, subscribeBusinessMeaning } from '../src/lib/businessMeaning.js'
import { usePreferences, editorPreferenceOptions, shouldSendMessage } from '../src/lib/preferences.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function loadScript(path, bindings, exports) {
  let source = await readFile(new URL(path, import.meta.url), 'utf8')
  if (path.endsWith('.vue')) source = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  source = source.replace(/^import\s+(?!\()[\s\S]*?\s+from\s+['"][^'"]+['"]\s*;?\r?$/gm, '')
    .replace(/^import\s+['"][^'"]+['"]\s*;?\r?$/gm, '')
    .replace(/^export\s*\{[\s\S]*?\}\s*(?:from\s+['"][^'"]+['"])?\s*;?\r?$/gm, '')
    .replace(/^export default .*\r?\n/gm, '').replace(/^export /gm, '')
    .replace(/await import\('([^']+)'\)/g, 'await __modules["$1"]')
  const context = vm.createContext({ setTimeout, clearTimeout, setInterval, clearInterval, ...bindings })
  vm.runInContext(`${source}\n;globalThis.subject = { ${exports} }`, context)
  return { subject: context.subject, context }
}

async function component(path, props, api, exports, extra = {}) {
  const unmount = []
  const storage = new Map()
  const browserStorage = { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) }
  const loaded = await loadScript(path, {
    computed, usePreferences, editorPreferenceOptions, shouldSendMessage, defineAsyncComponent: loader => ({ loader }), nextTick, ref, watch, defineProps: () => props, defineEmits: () => () => {},
    defineExpose: () => {}, onMounted: () => {}, onBeforeUnmount: (fn) => unmount.push(fn),
    api, localStorage: browserStorage, documentKey, useNavigation, WorkspaceLanguageService, prepareWorkspaceEdit, assertPlanCurrent, findPlanDocument, useProjectAccess: (options) => useProjectAccess({ ...options, storage: browserStorage }), useWorkbench: (options) => useWorkbench({ ...options, storage: browserStorage }),
    getJavaTokenTarget, registerTokenBusinessSource, subscribeHoverDocumentationUpdates, withTokenBusiness, createBusinessMeaningSource, subscribeBusinessMeaning,
    useIndexRefresh, useFunctionDescriptions, useDialog: () => ({ open: async () => null }), document: { visibilityState: 'visible' },
    useProjectAgentTeam: options => { const scope = effectScope(); unmount.push(() => scope.stop()); return scope.run(() => useProjectAgentTeam(options)) },
    useProjectConversations: options => { const scope = effectScope(); unmount.push(() => scope.stop()); return scope.run(() => useProjectConversations(options)) },
    window: { confirm: () => false, addEventListener() {}, removeEventListener() {} },
    ...extra,
  }, exports)
  return { ...loaded, dispose: async () => { unmount.forEach((fn) => fn()); await nextTick() } }
}

const project = (id) => ({ id, name: id, available: true, root_path: `/projects/${id}` })
const apiFor = (overrides = {}) => ({ code: {
  agentTeam: async (id, tree, conversationId) => ({ lead: { session_id: conversationId || `session-${id}`, role: 'lead', state: 'idle' }, agents: [], limits: { max_depth: 3, max_parallel: 2 }, running: 0, queued: 0 }),
  project: async (id) => project(id), projectSession: async (id) => ({ id: `session-${id}`, permission_mode: 'read-only' }),
  conversations: async id => ({ default_session_id: `session-${id}`, conversations: [{ id: `session-${id}`, kind: 'lead' }] }),
  projectLsp: async () => ({ servers: [] }), diagnostics: async () => ({ counts: { error: 0, warning: 0 } }),
  indexStatus: async () => ({ briefs: { state: 'idle', revision: 0, coverage_known: false } }),
  ...overrides,
} })
const viewExports = 'project, session, file, fileBusy, lspServers, diagnostics, activeTree, selectProject, openFile, refreshDiagnostics, saveFile, promptOpenProject, projectAccess, pickerOpen, openProjectFolder, pickProjectLocation, projectLocationSelection, dismissProjectForm, workbench, documents, editorRef, captureEditor, onEditorChange, closeDocument, closingDocument, finishClose, conflictDialog, resolveConflict, navigation, navigateHistory, onEditorSelection, runCurrent, runDebug, runtimeRef, debugRef, banner, onPatchesChanged, onLanguageEdit, applyLanguageEdit, undoLanguageEdit, languageEditPreview, languageEditUndo'

test('opening an editor project checks automatic functionality progress without opening a map', async () => {
  const reads = [], blocked = deferred()
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor({
    indexStatus: (pid, tree, options) => { reads.push({ pid, tree, signal: options.signal }); return blocked.promise },
  }), viewExports + ', functionDescriptions, centerView, mapActivated')
  try {
    await view.subject.selectProject(project('editor-auto'))
    await new Promise(setImmediate)
    assert.equal(view.subject.centerView.value, 'file')
    assert.equal(view.subject.mapActivated.value, false)
    assert.equal(reads.length, 1)
    assert.equal(reads[0].tree, 'main')
    assert.equal(view.subject.project.value.id, 'editor-auto')
    assert.equal(view.subject.functionDescriptions.loading.value, true)
    blocked.resolve({ briefs: { state: 'waiting', revision: 0, coverage_known: true, coverage_total: 4, described: 2, missing: 2, note: '没有可用的模型' } })
    await new Promise(setImmediate)
    assert.match(view.subject.functionDescriptions.presentation.value.label, /待补 2/)
    assert.equal(view.subject.banner.value, '')
  } finally { await view.dispose() }
})

test('map AI handoff opens the assistant and prepares a prompt without sending', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor(), viewExports + ', onAsk, rightVisible, rightTab, mobilePanel, askPrompt, notice')
  try {
    const s = view.subject
    s.rightVisible.value = false
    s.rightTab.value = 'git'
    s.onAsk('需求确认：输出需求清单')
    assert.equal(s.rightVisible.value, true)
    assert.equal(s.rightTab.value, 'ai')
    assert.equal(s.mobilePanel.value, 'assistant')
    assert.equal(s.askPrompt.value, '需求确认：输出需求清单')
    assert.match(s.notice.value, /编辑后发送/)
    const source = await readFile(new URL('../src/views/CodeView.vue', import.meta.url), 'utf8')
    assert.doesNotMatch(source, /workflow|Workflow|pendingAgentNodeId|clear-node-intent/)
  } finally { await view.dispose() }
})

test('frontend design opens after the mindmap and frees its canvas without losing workbench panel preferences', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor(), viewExports + ', openFrontendDesign, designActivated, centerView, leftVisible, rightVisible, leftShown, rightShown, gridStyle')
  try {
    const s = view.subject
    await s.selectProject(project('design-layout')); await nextTick()
    s.openFrontendDesign(); await nextTick()
    assert.equal(s.centerView.value, 'design')
    assert.equal(s.designActivated.value, true)
    assert.equal(s.leftShown.value, false)
    assert.equal(s.rightShown.value, false)
    assert.match(s.gridStyle.value.gridTemplateColumns, /^0px .* 0px$/)
    assert.equal(s.leftVisible.value, true)
    assert.equal(s.rightVisible.value, true)
    s.centerView.value = 'file'; await nextTick()
    assert.equal(s.leftShown.value, true)
    assert.equal(s.rightShown.value, true)
    const source = await readFile(new URL('../src/views/CodeView.vue', import.meta.url), 'utf8')
    assert.ok(source.indexOf('>思维导图</button>') < source.indexOf('>前端设计</button>'))
    assert.match(source, /FrontendDesign[\s\S]*?:dirty-paths="dirtyPaths"/)
  } finally { await view.dispose() }
})

test('selecting the current design mode returns to its canvas without closing its assistant or changing panel preferences', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor(), viewExports + ', openFrontendDesign, showAssistantPanel, designActivated, designAssistantVisible, centerView, mobilePanel, leftVisible, rightVisible, leftShown, rightShown')
  try {
    const s = view.subject
    await s.selectProject(project('design-repeat')); await nextTick()
    s.leftVisible.value = false
    s.rightVisible.value = false
    let snapshots = 0
    s.editorRef.value = { snapshot: () => { snapshots++; return null } }
    s.openFrontendDesign(); await nextTick()
    s.showAssistantPanel(); await nextTick()
    assert.equal(s.designAssistantVisible.value, true)
    assert.equal(s.rightShown.value, true)
    assert.equal(s.mobilePanel.value, 'assistant')
    const originalLeft = s.leftVisible.value, originalRight = s.rightVisible.value
    s.openFrontendDesign(); await nextTick()
    assert.equal(s.centerView.value, 'design')
    assert.equal(s.designActivated.value, true)
    assert.equal(s.mobilePanel.value, 'center')
    assert.equal(s.designAssistantVisible.value, true)
    assert.equal(s.rightShown.value, true)
    assert.equal(s.leftShown.value, false)
    assert.equal(s.leftVisible.value, originalLeft)
    assert.equal(s.rightVisible.value, originalRight)
    assert.equal(snapshots, 1, 'selecting the current canvas does not recapture the hidden editor')
    s.centerView.value = 'file'; await nextTick()
    assert.equal(s.leftShown.value, originalLeft)
    assert.equal(s.rightShown.value, originalRight)
  } finally { await view.dispose() }
})

test('design mode ignores an empty or explicitly unavailable project without changing the workspace', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor(), viewExports + ', openFrontendDesign, designActivated, designAssistantVisible, centerView, mobilePanel, leftVisible, rightVisible')
  try {
    const s = view.subject
    s.leftVisible.value = false
    s.mobilePanel.value = 'projects'
    s.openFrontendDesign(); await nextTick()
    assert.equal(s.centerView.value, 'file')
    assert.equal(s.designActivated.value, false)
    assert.equal(s.mobilePanel.value, 'projects')
    s.project.value = { ...project('missing'), available: false }
    s.openFrontendDesign(); await nextTick()
    assert.equal(s.centerView.value, 'file')
    assert.equal(s.designActivated.value, false)
    assert.equal(s.designAssistantVisible.value, false)
    assert.equal(s.mobilePanel.value, 'projects')
    assert.equal(s.leftVisible.value, false)
    assert.equal(s.rightVisible.value, true)
  } finally { await view.dispose() }
})

test('design handoff stays on its canvas, prepares the existing Lead composer, and rejects another session result', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor(), viewExports + ', onDesignAsk, onDesignResult, openFrontendDesign, designAiResult, designActivated, designAssistantVisible, centerView, askPrompt, rightShown')
  try {
    const s = view.subject
    await s.selectProject(project('design-main')); await nextTick()
    s.openFrontendDesign()
    s.onDesignAsk('只设计当前选中标题的样式，请返回 frontend-style JSON')
    assert.equal(s.centerView.value, 'design')
    assert.equal(s.rightShown.value, true)
    assert.equal(s.designAssistantVisible.value, true)
    assert.match(s.askPrompt.value, /frontend-style JSON/)
    s.onDesignResult({ sessionId: 'other-session', text: 'wrong' })
    assert.equal(s.designAiResult.value, null)
    s.onDesignResult({ sessionId: 'session-design-main', text: 'matched', seq: 'a10' })
    assert.equal(s.designAiResult.value.text, 'matched')
    assert.equal(s.designAiResult.value.projectId, 'design-main')
    await s.selectProject(project('replacement')); await nextTick()
    assert.equal(s.designAiResult.value, null)
    assert.equal(s.designActivated.value, false)
  } finally { await view.dispose() }
})

test('map questions stay on the root dialogue and never select a worker composer', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor(), viewExports + ', onAsk, agentTeam')
  try {
    const s = view.subject
    await s.selectProject(project('current')); await nextTick()
    s.agentTeam.team.value = { lead: { session_id: 'session-current', role: 'lead' }, agents: [{ session_id: 'worker-current', parent_session_id: 'session-current', role: 'node', node_id: 'requirements', state: 'idle' }] }
    assert.equal(s.agentTeam.select('worker-current'), false)
    s.onAsk('解释所选代码及其依赖')
    assert.equal(s.agentTeam.sessionId.value, 'session-current')
    assert.equal(s.session.value.id, 'session-current', 'save/editor identity remains permanent Lead')
  } finally { await view.dispose() }
})

const fileApi = (overrides = {}) => apiFor({
  file: async (_pid, path) => ({ path, text: `${path}: disk`, sha1: `sha-${path}`, lang: 'text' }),
  ...overrides,
})

test('new project dialogue preserves dirty editor identity, keeps map questions on its current root, and stays outside task trees', async () => {
  const creates = [], view = await component('../src/views/CodeView.vue', { user: { id: 'dialogue-owner' } }, fileApi({
    conversations: async pid => ({ default_session_id: `session-${pid}`, conversations: [{ id: `session-${pid}`, kind: 'code' }, ...creates.filter(([createdPid]) => createdPid === pid).map(() => ({ id: `${pid}-chat`, title: '新对话1', kind: 'chat' }))] }),
    createConversation: async (pid, payload) => { creates.push([pid, payload]); return { id: `${pid}-chat`, title: '新对话1', kind: 'chat', permission_mode: 'read-only', effective_permission: 'read-only' } },
  }), viewExports + ', conversations, createConversation, selectConversation, agentTeam, aiSessionId, onAsk')
  try {
    const s = view.subject
    await s.selectProject(project('dialogue')); await nextTick(); await new Promise(setImmediate)
    await s.openFile({ path: 'draft.py' }); s.onEditorChange({ key: s.file.value.key, text: 'unsaved editor content' })
    const key = s.workbench.activeKey.value
    s.agentTeam.team.value = { lead: { session_id: 'session-dialogue' }, agents: [{ session_id: 'dialogue-worker', role: 'node' }] }
    assert.equal(s.agentTeam.select('dialogue-worker'), false)
    await s.createConversation(); await nextTick()
    assert.deepEqual(JSON.parse(JSON.stringify(creates)), [['dialogue', { from_session_id: 'session-dialogue' }]])
    assert.equal(s.aiSessionId.value, 'dialogue-chat')
    assert.equal(s.session.value.id, 'session-dialogue')
    assert.equal(s.workbench.activeKey.value, key)
    assert.equal(s.documents.value.find(doc => doc.key === key).text, 'unsaved editor content')
    assert.equal(s.documents.value.find(doc => doc.key === key).dirty, true)
    s.onAsk('解释当前地图节点')
    assert.equal(s.aiSessionId.value, 'dialogue-chat')
    s.selectConversation('dialogue-chat'); assert.equal(s.aiSessionId.value, 'dialogue-chat')
    s.activeTree.value = 'task:123'; await nextTick()
    assert.equal(s.conversations.isChat.value, false)
    await s.createConversation(); assert.equal(creates.length, 1)
    s.activeTree.value = 'main'; await nextTick()
    assert.equal(s.conversations.isChat.value, true)
    assert.equal(s.session.value.id, 'session-dialogue')
  } finally { await view.dispose() }
})

test('project dialogue write permission changes target its own session and keep canonical Lead permission unchanged', async () => {
  const permissions = [], leadCalls = []
  let mode = 'read-only'
  const client = apiFor({
    project: async pid => ({ ...project(pid), allow_direct_write: true }),
    projectSession: async (pid, options) => { leadCalls.push([pid, options]); return { id: `session-${pid}`, permission_mode: 'read-only' } },
    conversations: async pid => ({ default_session_id: `session-${pid}`, conversations: [{ id: `session-${pid}`, kind: 'code' }, { id: `${pid}-chat`, title: '新对话1', kind: 'chat', permission_mode: mode, effective_permission: mode }] }),
  })
  client.setSessionPermission = async (id, next) => { permissions.push([id, next]); mode = next; return { id, permission_mode: next } }
  const view = await component('../src/views/CodeView.vue', { user: { id: 'permission-owner' } }, client, viewExports + ', conversations, selectConversation, toggleWritable, executionWritable, writable')
  try {
    const s = view.subject
    await s.selectProject({ ...project('permission'), allow_direct_write: true }); await nextTick(); await new Promise(setImmediate)
    s.selectConversation('permission-chat'); assert.equal(s.executionWritable.value, false)
    await s.toggleWritable(); await nextTick(); await new Promise(setImmediate)
    assert.deepEqual(permissions, [['permission-chat', 'workspace-write']])
    assert.equal(s.executionWritable.value, true)
    assert.equal(s.writable.value, false)
    assert.equal(s.session.value.permission_mode, 'read-only')
    assert.equal(leadCalls.length, 1, 'chat permission never calls canonical projectSession')
    await s.toggleWritable(); await nextTick()
    assert.deepEqual(permissions.at(-1), ['permission-chat', 'read-only'])
    assert.equal(s.session.value.id, 'session-permission')
  } finally { await view.dispose() }
})

test('deletion confirmation covers the default too, preserves drafts on failure, and stale project cannot confirm', async () => {
  const service = createDialogService(), removed = [], metas = [{ id: 'delete-chat', kind: 'chat', title: '新对话 1' }, { id: 'other-chat', kind: 'chat', title: '其他对话' }]
  let fail = true
  const view = await component('../src/views/CodeView.vue', { user: { id: 'delete-owner' } }, fileApi({
    conversations: async pid => ({ default_session_id: `session-${pid}`, conversations: [{ id: `session-${pid}`, kind: 'code' }, ...metas] }),
    deleteConversation: async (pid, id) => { removed.push([pid, id]); if (fail) throw Error('删除暂时失败'); return { deleted: true, session_id: id, fallback_session_id: `session-${pid}` } },
  }), viewExports + ', conversations, selectConversation, deleteConversation, aiSessionId', { useDialog: () => service })
  try {
    const s = view.subject
    await s.selectProject(project('delete')); await nextTick(); await new Promise(setImmediate)
    await s.openFile({ path: 'draft.py' }); s.onEditorChange({ key: s.file.value.key, text: 'keep unsaved content' })
    s.selectConversation('delete-chat')
    const defaultCanceled = s.deleteConversation('session-delete'); await nextTick()
    assert.match(service.active.value.options.description, /删除默认对话后.*空白对话/)
    service.cancel(); await defaultCanceled; assert.deepEqual(removed, [])
    const canceled = s.deleteConversation('delete-chat'); await nextTick()
    assert.match(service.active.value.options.description, /项目文件.*草稿.*其他对话/)
    assert.deepEqual(removed, [])
    service.cancel(); await canceled; assert.equal(s.aiSessionId.value, 'delete-chat')
    const pending = s.deleteConversation('delete-chat'); await nextTick()
    await service.submit(); assert.match(service.active.value.error, /删除暂时失败/)
    assert.equal(s.aiSessionId.value, 'delete-chat'); assert.equal(s.file.value.text, 'keep unsaved content')
    fail = false; await service.submit(); await pending
    assert.equal(s.aiSessionId.value, 'session-delete'); assert.equal(s.file.value.text, 'keep unsaved content')
    assert.equal(s.session.value.id, 'session-delete')
    await s.conversations.refresh(); s.selectConversation('other-chat')
    const stale = s.deleteConversation('other-chat'); await nextTick()
    await s.selectProject(project('replacement')); await nextTick()
    const before = removed.length
    await service.submit(); assert.equal(removed.length, before)
    assert.match(service.active.value.error, /当前项目已变化/)
    service.cancel(); await stale
  } finally { service.cancel(); await view.dispose() }
})

test('confirmed default deletion updates the workspace root without changing dirty editor models or promoting chat permissions', async () => {
  for (const promoteChat of [true, false]) {
    const service = createDialogService(), deletion = deferred(), reads = [], roots = [], removed = []
    let defaultId = 'session-default', rows = [{ id: defaultId, kind: 'main', title: 'Lead', permission_mode: 'read-only' }]
    const next = { id: promoteChat ? 'existing-chat' : 'fresh-root', kind: promoteChat ? 'chat' : 'main', title: promoteChat ? '继续开发接口' : '新对话', permission_mode: 'workspace-write', effective_permission: 'workspace-write', busy: promoteChat }
    if (promoteChat) rows.push(next)
    const view = await component('../src/views/CodeView.vue', { user: { id: `default-delete-${promoteChat}` } }, fileApi({
      project: async pid => ({ ...project(pid), session_id: defaultId, allow_direct_write: true }),
      conversations: async () => ({ default_session_id: defaultId, conversations: rows }),
      projectSession: async pid => { roots.push(pid); return rows.find(row => row.id === defaultId) },
      file: async (pid, path) => { reads.push([pid, path]); return { path, text: 'disk content', sha1: 'before' } },
      deleteConversation: async (pid, id) => { removed.push([pid, id]); await deletion.promise; defaultId = next.id; rows = [next]; return { deleted: true, session_id: id, fallback_session_id: next.id, default_session_id: next.id, default_conversation: next } },
    }), viewExports + ', conversations, selectConversation, deleteConversation, aiSessionId, executionWritable', { useDialog: () => service })
    try {
      const s = view.subject
      await s.selectProject({ ...project('default'), session_id: 'session-default', allow_direct_write: true }); await nextTick(); await new Promise(setImmediate)
      await s.openFile({ path: 'dirty.java' }); s.onEditorChange({ key: s.file.value.key, text: 'unsaved java draft' })
      const key = s.workbench.activeKey.value, model = s.documents.value.find(doc => doc.key === key), beforeReads = reads.length
      const confirm = s.deleteConversation('session-default'); await nextTick()
      assert.match(service.active.value.options.description, /默认对话.*空白对话/)
      const submitting = service.submit(); assert.deepEqual(removed, [['default', 'session-default']])
      deletion.resolve(); assert.equal(await submitting, true, service.active.value?.error); await confirm; await nextTick(); await new Promise(setImmediate)
      assert.equal(s.session.value.id, next.id)
      assert.equal(s.session.value.kind, next.kind)
      assert.equal(s.project.value.session_id, next.id)
      assert.equal(s.aiSessionId.value, next.id)
      assert.equal(s.conversations.selected.value.title, next.title)
      assert.equal(s.conversations.isChat.value, promoteChat)
      assert.equal(s.executionWritable.value, true)
      assert.equal(s.workbench.activeKey.value, key)
      assert.equal(s.documents.value.find(doc => doc.key === key), model, 'deleting a conversation does not recreate the editor document')
      assert.equal(model.text, 'unsaved java draft'); assert.equal(model.dirty, true)
      assert.equal(s.file.value.text, 'unsaved java draft')
      assert.equal(reads.length, beforeReads, 'the replacement only synchronizes metadata; no file reload')
      assert.equal(roots.length, 1, 'does not request a new canonical Lead when the server already supplied the default')
      assert.equal(s.conversations.conversations.value.some(row => row.id === 'session-default'), false)
    } finally { service.cancel(); await view.dispose() }
  }
})

test('enabling AI writes uses a cancellable dialog and keeps API errors available for retry', async () => {
  const service = createDialogService(), calls = []
  let failures = 1
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor({
    updateSettings: async (pid, body) => { calls.push(['settings', pid, body]); return { ...project(pid), allow_direct_write: true } },
    projectSession: async (pid, body) => {
      if (body?.permission_mode === 'workspace-write') { calls.push(['write', pid]); if (failures--) throw new Error('permission update failed') }
      return { id: `session-${pid}`, permission_mode: body?.permission_mode || 'read-only' }
    },
  }), viewExports + ', toggleWritable, sessionBusy', { useDialog: () => service })
  try {
    const s = view.subject
    await s.selectProject(project('current'))
    let pending = s.toggleWritable()
    assert.equal(service.active.value.options.confirmText, '允许 AI 修改')
    service.cancel(); await pending; assert.equal(calls.length, 0); assert.equal(s.sessionBusy.value, false)
    pending = s.toggleWritable()
    assert.equal(await service.submit(), false); assert.equal(service.active.value.error, 'permission update failed')
    assert.equal(s.session.value.permission_mode, 'read-only')
    assert.equal(await service.submit(), true); await pending
    assert.equal(calls.filter(row => row[0] === 'settings').length, 1)
    assert.equal(s.session.value.permission_mode, 'workspace-write')
  } finally { service.cancel(); await view.dispose() }
})

for (const mode of ['workspace-write', 'full-access']) {
  test(`${mode} displays writable and toggles to read-only without changing manual editing`, async () => {
    const writes = []
    const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor({
      projectSession: async (pid, body) => {
        if (body) writes.push(body.permission_mode)
        return { id: `session-${pid}`, permission_mode: body?.permission_mode || mode }
      },
      updateSettings: async () => assert.fail('turning off execution writes must not enable project writes'),
    }), viewExports + ', toggleWritable, writable, manualReadonly', {
      useDialog: () => ({ open: async () => assert.fail('reducing execution permission must not request write access') }),
    })
    try {
      const s = view.subject
      await s.selectProject(project('current'))
      s.manualReadonly.value = false
      assert.equal(s.writable.value, true)
      await s.toggleWritable()
      assert.deepEqual(writes, ['read-only'])
      assert.equal(s.writable.value, false)
      assert.equal(s.manualReadonly.value, false)
    } finally { await view.dispose() }
  })
}

test('switching projects before approving AI access cannot grant the replacement project', async () => {
  const service = createDialogService(), grants = []
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor({ updateSettings: async (...args) => { grants.push(args); return project(args[0]) } }), viewExports + ', toggleWritable', { useDialog: () => service })
  try {
    const s = view.subject
    await s.selectProject(project('original'))
    const pending = s.toggleWritable()
    await s.selectProject(project('replacement'))
    assert.equal(await service.submit(), false)
    assert.match(service.active.value.error, /项目已变化/)
    assert.equal(grants.length, 0); service.cancel(); await pending
  } finally { service.cancel(); await view.dispose() }
})

test('a second external change refreshes the conflict while an overwrite is pending and cannot discard the draft', async () => {
  const writing = deferred()
  let reads = 0, writes = 0
  const conflict = Object.assign(new Error('changed externally'), { status: 409 })
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({
    file: async (_pid, path) => ({ path, text: `external-${++reads}`, sha1: `disk-${reads}`, editable: true }),
    saveFile: async () => { if (++writes === 1) throw conflict; return writing.promise },
  }), viewExports + ', saving, notice')
  try {
    const s = view.subject
    await s.selectProject(project('current')); await s.openFile({ path: 'main.py' })
    const key = s.file.value.key
    s.onEditorChange({ key, text: 'keep my draft' })
    assert.equal(await s.saveFile({ key, path: 'main.py', text: 'keep my draft', sha1: 'disk-1' }), false)
    assert.equal(s.conflictDialog.value.fresh.text, 'external-2')
    const overwriting = s.resolveConflict('overwrite')
    assert.equal(s.saving.value, true); assert.equal(s.notice.value, '')
    await s.resolveConflict('disk')
    assert.equal(s.file.value.text, 'keep my draft')
    writing.reject(conflict); await overwriting
    assert.equal(s.conflictDialog.value.fresh.text, 'external-3')
    assert.equal(s.file.value.text, 'keep my draft'); assert.equal(s.file.value.dirty, true)
    assert.equal(s.saving.value, false)
    await s.resolveConflict('keep')
    assert.equal(s.conflictDialog.value, null); assert.match(s.notice.value, /已保留草稿/)
  } finally { await view.dispose() }
})

test('a ledger failure reports committed disk content without keeping the editor in saving state', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({
    saveFile: async () => ({ ok: true, changed: true, sha1: 'saved', patch_error: 'fixture ledger locked' }),
  }), viewExports + ', saving, notice')
  try {
    const s = view.subject
    await s.selectProject(project('current')); await s.openFile({ path: 'main.py' })
    const key = s.file.value.key
    s.onEditorChange({ key, text: 'committed' })
    assert.equal(await s.saveFile({ key, path: 'main.py', text: 'committed', sha1: s.file.value.sha1 }), true)
    assert.equal(s.saving.value, false); assert.equal(s.file.value.dirty, false)
    assert.match(s.notice.value, /已保存/); assert.match(s.banner.value, /文件已保存.*台账记录失败/)
  } finally { await view.dispose() }
})

test('language edits preview dirty and unopened files, apply only to drafts and undo as one batch', async () => {
  let saves = 0
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({
    file: async (_pid, path) => ({ path, text: 'name = 1\n', sha1: 'disk', uri: `file:///projects/current/${path}`, editable: true }),
    saveFile: async () => { saves++; return {} },
  }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('current')); await s.openFile({ path: 'main.py' })
    s.onEditorChange({ key: s.file.value.key, text: 'name = 2\n' })
    const range = { start: { line: 0, character: 0 }, end: { line: 0, character: 4 } }
    const edit = { changes: { 'file:///projects/current/main.py': [{ range, newText: 'renamed' }], 'file:///projects/current/helper.py': [{ range, newText: 'renamed' }] } }
    assert.equal(await s.onLanguageEdit({ edit, title: 'rename', projectId: 'current', tree: 'main' }), true)
    assert.equal(s.documents.value.length, 1); assert.equal(s.file.value.text, 'name = 2\n')
    assert.equal(await s.applyLanguageEdit(), true)
    assert.equal(s.documents.value.length, 2); assert.equal(s.file.value.text, 'renamed = 2\n')
    assert.equal(s.documents.value.find(doc => doc.path === 'helper.py').text, 'renamed = 1\n')
    assert.equal(saves, 0)
    assert.equal(await s.undoLanguageEdit(), true)
    assert.equal(s.file.value.text, 'name = 2\n')
    assert.equal(s.documents.value.find(doc => doc.path === 'helper.py').dirty, false)
    assert.equal(s.documents.value.find(doc => doc.path === 'main.py').dirty, true)
    assert.equal(saves, 0)
  } finally { await view.dispose() }
})

test('typing during a preview and changing projects prevent language edits from replacing any draft', async () => {
  const read = deferred()
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({
    file: async (_pid, path) => path === 'helper.py' ? read.promise : { path, text: 'name = 1\n', uri: `file:///projects/current/${path}`, editable: true },
  }), viewExports)
  try {
    const s = view.subject, range = { start: { line: 0, character: 0 }, end: { line: 0, character: 4 } }
    await s.selectProject(project('current')); await s.openFile({ path: 'main.py' })
    assert.equal(await s.onLanguageEdit({ edit: { changes: { 'file:///projects/current/main.py': [{ range, newText: 'new' }] } } }), true)
    s.onEditorChange({ key: s.file.value.key, text: 'later input' })
    assert.equal(await s.applyLanguageEdit(), false); assert.equal(s.file.value.text, 'later input')
    const preparing = s.onLanguageEdit({ edit: { changes: { 'file:///projects/current/helper.py': [{ range, newText: 'new' }] } } })
    await s.selectProject(project('other'))
    read.resolve({ path: 'helper.py', text: 'name = 1\n', editable: true })
    assert.equal(await preparing, false); assert.equal(s.languageEditPreview.value, null)
  } finally { await view.dispose() }
})

test('choosing a creation location preserves the current editor and applies repeated selections', async () => {
  const responses = [{ path: 'E:/新位置', cancelled: false }, { path: null, cancelled: true }, { path: 'E:/新位置', cancelled: false }]
  const starts = []
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({
    pickFolder: async start => { starts.push(start); return responses.shift() },
    openProject: () => { throw new Error('location selection must not open a project') },
    createProject: () => { throw new Error('location selection must not create a project') },
  }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('current')); await s.openFile({ path: 'draft.py' })
    s.onEditorChange({ key: s.file.value.key, text: 'keep location draft' })
    s.projectAccess.meta.value = { native_picker: { available: true } }
    s.promptOpenProject('create')
    assert.equal(await s.pickProjectLocation('E:/initial'), true)
    const first = s.projectLocationSelection.value
    assert.equal(first.path, 'E:/新位置')
    assert.equal(await s.pickProjectLocation('E:/typed'), false)
    assert.equal(s.projectLocationSelection.value, first)
    assert.equal(await s.pickProjectLocation('E:/typed'), true)
    assert.equal(s.projectLocationSelection.value.path, first.path)
    assert.notEqual(s.projectLocationSelection.value, first)
    assert.deepEqual(starts, ['E:/initial', 'E:/typed', 'E:/typed'])
    assert.equal(s.pickerOpen.value, true)
    assert.equal(s.project.value.id, 'current')
    assert.equal(s.file.value.text, 'keep location draft')
    assert.equal(s.documents.value[0].dirty, true)
    s.dismissProjectForm()
    assert.equal(s.projectLocationSelection.value, null)
  } finally { await view.dispose() }
})

test('native project cancellation and registration failure keep the current editor and unsaved draft', async () => {
  const chosen = deferred()
  let registrations = 0
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({
    pickFolder: () => chosen.promise,
    openProject: async () => { registrations++; throw new Error('folder is no longer available') },
  }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('current')); await s.openFile({ path: 'draft.py' })
    s.onEditorChange({ key: s.file.value.key, text: 'keep this unsaved draft' })
    s.projectAccess.meta.value = { native_picker: { available: true } }
    const selection = s.openProjectFolder()
    assert.equal(s.project.value.id, 'current')
    assert.equal(s.file.value.text, 'keep this unsaved draft')
    chosen.resolve({ path: null, cancelled: true })
    assert.equal(await selection, false)
    assert.equal(registrations, 0)
    assert.equal(s.project.value.id, 'current')
    assert.equal(await s.projectAccess.openPath('/projects/missing'), false)
    assert.equal(s.project.value.id, 'current')
    assert.equal(s.file.value.text, 'keep this unsaved draft')
    assert.equal(s.documents.value[0].dirty, true)
  } finally { await view.dispose() }
})

test('workspace history restores actual outgoing cursor and preserves dirty tabs', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi(), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('p'))
    await s.openFile({ path: 'a.py' })
    const a = s.file.value.key
    s.onEditorSelection({ key: a, path: 'a.py', start_line: 9, end_line: 9, start_column: 4, end_column: 4 })
    s.onEditorChange({ key: a, text: 'draft A' })
    await s.openFile({ path: 'b.py', line: 6, column: 2 })
    await s.navigateHistory(-1)
    assert.equal(s.file.value.path, 'a.py')
    assert.equal(s.file.value.text, 'draft A')
    assert.equal(s.navigation.state.value.entries[s.navigation.state.value.cursor].column, 4)
    await s.navigateHistory(1)
    assert.equal(s.file.value.path, 'b.py')
    assert.equal(s.navigation.canForward.value, false)
  } finally { await view.dispose() }
})

test('run and debug save the submitted draft first and conflicts prevent execution', async () => {
  const saves = [], launches = []
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({ saveFile: async (...args) => { saves.push(args); return { sha1: 'saved', changed: true } } }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('p')); await s.openFile({ path: 'a.py' })
    s.onEditorChange({ key: s.file.value.key, text: 'draft A' })
    s.runtimeRef.value = { runCurrent: () => launches.push('run') }
    await s.runCurrent()
    assert.equal(saves[0][2], 'draft A')
    assert.deepEqual(launches, ['run'])
    s.debugRef.value = { start: options => launches.push(options.path) }
    await s.runDebug()
    assert.deepEqual(launches, ['run', 'a.py'])
    s.onEditorChange({ key: s.file.value.key, text: 'new draft' })
    view.context.api.code.saveFile = async () => { throw Object.assign(new Error('disk changed'), { status: 409 }) }
    await s.runDebug()
    assert.equal(launches.length, 2)
    assert.ok(s.conflictDialog.value)
  } finally { await view.dispose() }
})

test('switching projects while saving cannot start a process in the next project', async () => {
  const pending = deferred(), launches = []
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({ saveFile: () => pending.promise }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('p')); await s.openFile({ path: 'a.py' })
    s.onEditorChange({ key: s.file.value.key, text: 'draft' })
    s.runtimeRef.value = { runCurrent: () => launches.push('run') }
    const running = s.runCurrent()
    await s.selectProject(project('q')); await s.openFile({ path: 'q.py' })
    pending.resolve({ sha1: 'saved', changed: true }); await running
    assert.equal(launches.length, 0)
    assert.equal(s.file.value.path, 'q.py')
  } finally { await view.dispose() }
})

test('Java debug is enabled and attach preserves drafts while launch saves them', async () => {
  const saves = [], launches = []
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({ saveFile: async (...args) => { saves.push(args); return { sha1: 'saved-java', changed: true } } }), viewExports + ', debugSupported')
  try {
    const s = view.subject
    await s.selectProject(project('p')); await s.openFile({ path: 'Main.java' })
    assert.equal(s.debugSupported.value, true)
    s.onEditorChange({ key: s.file.value.key, text: 'unsaved Java draft' })
    s.debugRef.value = { start: options => launches.push(options) }
    await s.runDebug({ language: 'java', java: { request: 'attach', port: 5005 } })
    assert.equal(saves.length, 0)
    assert.equal(s.file.value.dirty, true)
    assert.equal(launches[0].java.request, 'attach')
    await s.runDebug({ language: 'java', java: { request: 'launch' } })
    assert.equal(saves[0][2], 'unsaved Java draft')
    assert.equal(launches[1].path, 'Main.java')
    await s.openFile({ path: 'file.txt' })
    assert.equal(s.debugSupported.value, false)
    await s.openFile({ path: 'main.py' })
    assert.equal(s.debugSupported.value, true)
  } finally { await view.dispose() }
})

test('Escape keeps a resource review visible while its batch is being applied', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi(), viewExports + ', languageEditBusy, onWorkbenchKey')
  try {
    const s = view.subject
    s.languageEditPreview.value = { batch: { id: 'applying' } }
    s.languageEditBusy.value = true
    const ev = { key: 'Escape', preventDefault() { this.defaultPrevented = true } }
    s.onWorkbenchKey(ev)
    assert.equal(ev.defaultPrevented, true)
    assert.ok(s.languageEditPreview.value)
    s.languageEditBusy.value = false
    s.onWorkbenchKey({ key: 'Escape', preventDefault() {} })
    assert.equal(s.languageEditPreview.value, null)
  } finally { await view.dispose() }
})

test('review changes refresh background tabs while keeping their later drafts', async () => {
  let disk = 'old disk'
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({ file: async (_pid, path) => ({ path, text: disk, sha1: disk }) }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('p')); await s.openFile({ path: 'a.py' })
    const a = s.file.value.key
    await s.openFile({ path: 'b.py' })
    s.onPatchesChanged({ projectId: 'other-project', path: 'a.py' }); await new Promise(setImmediate)
    assert.equal(s.documents.value.find(d => d.key === a).text, 'old disk')
    disk = 'applied proposal'
    s.onPatchesChanged({ path: 'a.py' }); await new Promise(setImmediate)
    assert.equal(s.documents.value.find(d => d.key === a).text, 'applied proposal')
    s.onEditorChange({ key: a, text: 'later draft' })
    disk = 'rolled back'
    s.onPatchesChanged({ path: 'a.py' }); await new Promise(setImmediate)
    assert.equal(s.documents.value.find(d => d.key === a).text, 'later draft')
    assert.ok(s.documents.value.find(d => d.key === a).conflict)
  } finally { await view.dispose() }
})

test('capturing a model during rapid tab switches updates its own document', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi(), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('p'))
    await s.openFile({ path: 'a.txt' })
    const a = s.file.value.key
    await s.openFile({ path: 'b.txt' })
    s.editorRef.value = { snapshot: () => ({ key: a, text: 'draft from old model' }) }
    s.captureEditor()
    assert.equal(s.documents.value.find(doc => doc.key === a).text, 'draft from old model')
    assert.equal(s.workbench.activeDocument.value.text, 'b.txt: disk')
    s.editorRef.value = null
  } finally { await view.dispose() }
})

test('an unloaded clean recovery placeholder never becomes an empty draft', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi(), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('p'))
    await s.openFile({ path: 'a.txt' })
    const doc = s.workbench.activeDocument.value
    Object.assign(doc, { savedText: null, text: '', dirty: false, needsReload: true })
    s.editorRef.value = { snapshot: () => ({ key: doc.key, text: '' }) }
    s.captureEditor()
    assert.equal(doc.dirty, false)
    s.workbench.updateExternal(doc.key, { path: doc.path, text: 'a.txt: disk', sha1: 'sha-a.txt' })
    assert.equal(doc.text, 'a.txt: disk')
    s.editorRef.value = null
  } finally { await view.dispose() }
})

test('save A then switch B and back A refreshes its baseline while keeping later typing', async () => {
  const saved = deferred()
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({ saveFile: () => saved.promise }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('p'))
    await s.openFile({ path: 'a.txt' })
    const key = s.file.value.key
    s.onEditorChange({ key, text: 'submitted' })
    const saving = s.saveFile({ key, path: 'a.txt', text: 'submitted', sha1: 'sha-a.txt' })
    s.onEditorChange({ key, text: 'submitted plus later typing' })
    await s.openFile({ path: 'b.txt' })
    await s.openFile({ path: 'a.txt' })
    saved.resolve({ changed: true, sha1: 'new-sha', lines: 1, version: 3 })
    assert.equal(await saving, true)
    assert.equal(s.file.value.text, 'submitted plus later typing')
    assert.equal(s.file.value.sha1, 'new-sha')
    assert.equal(s.file.value.savedText, 'submitted')
    assert.equal(s.file.value.dirty, true)
  } finally { await view.dispose() }
})

test('a no-change save also refreshes the active hash and clears its dirty flag', async () => {
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({
    saveFile: async () => ({ changed: false, sha1: 'current-sha', lines: 1 }),
  }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('p'))
    await s.openFile({ path: 'a.txt' })
    const key = s.file.value.key
    s.onEditorChange({ key, text: 'same as disk now' })
    assert.equal(await s.saveFile({ key, path: 'a.txt', text: 'same as disk now', sha1: 'old' }), true)
    assert.equal(s.file.value.sha1, 'current-sha')
    assert.equal(s.file.value.dirty, false)
  } finally { await view.dispose() }
})

test('an old save-and-close completion cannot dismiss a new project close dialog', async () => {
  const saved = deferred()
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({ saveFile: () => saved.promise }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('old'))
    await s.openFile({ path: 'a.txt' })
    s.onEditorChange({ key: s.file.value.key, text: 'old draft' })
    s.closeDocument(s.file.value.key)
    const closing = s.finishClose('save')
    await s.selectProject(project('new'))
    await s.openFile({ path: 'b.txt' })
    s.onEditorChange({ key: s.file.value.key, text: 'new draft' })
    s.closeDocument(s.file.value.key)
    const newDialog = s.closingDocument.value
    saved.resolve({ changed: true, sha1: 'saved-old', lines: 1, version: 2 })
    await closing
    assert.equal(s.closingDocument.value, newDialog)
    assert.equal(s.file.value.text, 'new draft')
    assert.equal(s.documents.value.length, 1)
  } finally { await view.dispose() }
})

test('an old conflict overwrite cannot dismiss a new project conflict dialog', async () => {
  const saved = deferred()
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({ saveFile: () => saved.promise }), viewExports)
  try {
    const s = view.subject
    await s.selectProject(project('old'))
    await s.openFile({ path: 'a.txt' })
    const key = s.file.value.key
    s.onEditorChange({ key, text: 'old draft' })
    s.conflictDialog.value = { key, path: 'a.txt', text: 'old draft', fresh: { sha1: 'new-disk' } }
    const resolving = s.resolveConflict('overwrite')
    await s.selectProject(project('new'))
    await s.openFile({ path: 'b.txt' })
    s.conflictDialog.value = { key: s.file.value.key, path: 'b.txt', fresh: { sha1: 'b-disk' } }
    const newDialog = s.conflictDialog.value
    saved.resolve({ changed: true, sha1: 'saved-old', lines: 1, version: 2 })
    await resolving
    assert.equal(s.conflictDialog.value, newDialog)
  } finally { await view.dispose() }
})

test('late project/session responses cannot overwrite the newly selected project', async () => {
  const oldSession = deferred(), oldLsp = deferred()
  const api = apiFor({
    projectSession: (id) => id === 'old' ? oldSession.promise : Promise.resolve({ id: 'session-new' }),
    projectLsp: (id) => id === 'old' ? oldLsp.promise : Promise.resolve({ servers: [{ name: 'new-lsp' }] }),
  })
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, api, viewExports)
  try {
    const pending = view.subject.selectProject(project('old'))
    view.subject.activeTree.value = 'task:old'
    await view.subject.selectProject(project('new'))
    oldSession.resolve({ id: 'session-old' })
    oldLsp.resolve({ servers: [{ name: 'old-lsp' }] })
    await pending
    assert.equal(view.subject.project.value.id, 'new')
    assert.equal(view.subject.session.value.id, 'session-new')
    assert.equal(view.subject.lspServers.value[0].name, 'new-lsp')
    assert.equal(view.subject.activeTree.value, 'main')
  } finally { await view.dispose() }
})

test('workspace activation completes while Lead, language probing and recovery remain pending', async () => {
  const lead = deferred(), lsp = deferred(), disk = deferred(), counts = deferred()
  let teamReads = 0, diagnosticsReads = 0
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor({
    projectSession: () => lead.promise, projectLsp: () => lsp.promise,
    file: () => disk.promise, diagnostics: () => { diagnosticsReads++; return counts.promise },
    agentTeam: async id => { teamReads++; return { lead: { session_id: `session-${id}`, role: 'lead' }, agents: [] } },
  }), viewExports + ', sessionBusy')
  try {
    const s = view.subject
    const draft = s.workbench.openFile({ path: 'visible.py', text: 'original', sha1: 'disk', editable: true }, { projectId: 'p' })
    s.workbench.update(draft.key, { text: 'my recovered draft' }); draft.needsReload = true
    assert.equal(await s.selectProject(project('p')), true)
    await nextTick()
    assert.equal(s.projectAccess.phase.value, 'idle')
    assert.equal(s.projectAccess.activeId.value, 'p')
    assert.equal(s.sessionBusy.value, true)
    assert.equal(s.file.value.text, 'my recovered draft')
    assert.equal(s.file.value.needsReload, true)
    assert.equal(teamReads, 0, 'roster waits for canonical Lead instead of racing its creation')
    assert.equal(diagnosticsReads, 1, 'diagnostics start independently of the slow LSP probe')
    disk.resolve({ path: 'visible.py', text: 'original', sha1: 'disk', editable: true })
    counts.resolve({ counts: { error: 2, warning: 1 } })
    lead.resolve({ id: 'session-p', permission_mode: 'read-only' })
    lsp.resolve({ servers: [{ name: 'ready' }] })
    await new Promise(setImmediate)
    assert.equal(s.file.value.needsReload, false)
    assert.equal(s.file.value.text, 'my recovered draft')
    assert.equal(s.session.value.id, 'session-p')
    assert.equal(s.diagnostics.value.counts.error, 2)
    assert.equal(teamReads, 1)
  } finally { await view.dispose() }
})

test('recovery reads active tab first, limits background concurrency, and shares manual open reads', async () => {
  const reads = [], pending = new Map()
  let running = 0, peak = 0
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor({
    file: (_pid, path) => {
      reads.push(path); running++; peak = Math.max(peak, running)
      const call = deferred(); pending.set(path, call)
      return call.promise.finally(() => { running-- })
    },
  }), viewExports)
  try {
    const s = view.subject
    for (const path of ['background-a.py', 'background-b.py', 'background-c.py', 'active.py']) {
      const doc = s.workbench.openFile({ path, text: `${path} original`, sha1: path }, { projectId: 'p' })
      s.workbench.update(doc.key, { text: `${path} draft` }); doc.needsReload = true
    }
    await s.selectProject(project('p')); await new Promise(setImmediate)
    assert.deepEqual(reads, ['active.py'])
    const explicit = s.openFile({ path: 'active.py' })
    await new Promise(setImmediate)
    assert.deepEqual(reads, ['active.py'], 'opening a recovering file reuses the disk request')
    const resolve = path => pending.get(path).resolve({ path, text: `${path} original`, sha1: path })
    resolve('active.py'); await explicit; await new Promise(setImmediate)
    assert.equal(s.file.value.text, 'active.py draft')
    assert.deepEqual(reads, ['active.py', 'background-a.py', 'background-b.py'])
    assert.equal(peak, 2)
    resolve('background-a.py'); await new Promise(setImmediate)
    assert.equal(reads.at(-1), 'background-c.py')
    resolve('background-b.py'); resolve('background-c.py'); await new Promise(setImmediate)
    assert.equal(s.documents.value.every(doc => !doc.needsReload && doc.dirty), true)
    assert.equal(peak, 2)
  } finally { await view.dispose() }
})

test('leaving during active-tab recovery does not start background reads or alter a replacement draft', async () => {
  const disk = deferred(), reads = []
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, fileApi({
    file: (pid, path) => { reads.push([pid, path]); return pid === 'old' ? disk.promise : Promise.resolve({ path, text: 'replacement' }) },
  }), viewExports)
  try {
    const s = view.subject
    for (const path of ['background.py', 'active.py']) {
      const doc = s.workbench.openFile({ path, text: 'original' }, { projectId: 'old' })
      s.workbench.update(doc.key, { text: 'recovered draft' }); doc.needsReload = true
    }
    await s.selectProject(project('old')); await new Promise(setImmediate)
    await s.selectProject(project('new')); await s.openFile({ path: 'new.py' })
    s.onEditorChange({ key: s.file.value.key, text: 'replacement draft' })
    disk.resolve({ path: 'active.py', text: 'late disk' }); await new Promise(setImmediate)
    assert.equal(reads.some(([pid, path]) => pid === 'old' && path === 'background.py'), false)
    assert.equal(s.file.value.text, 'replacement draft')
    assert.equal(s.project.value.id, 'new')
  } finally { await view.dispose() }
})

test('hidden cached workspace pauses recovery, team polling and shortcuts without reopening its Lead', async () => {
  const props = reactive({ user: { id: 1 }, visible: true }), disk = deferred(), reads = []
  let leadReads = 0, lspReads = 0, teamReads = 0, diagnosticReads = 0
  const view = await component('../src/views/CodeView.vue', props, fileApi({
    projectSession: async id => { leadReads++; return { id: `session-${id}` } },
    projectLsp: async () => { lspReads++; return { servers: [] } },
    diagnostics: async () => { diagnosticReads++; return { counts: { error: 0, warning: 0 } } },
    agentTeam: async id => { teamReads++; return { lead: { session_id: `session-${id}`, role: 'lead' }, agents: [] } },
    file: (_pid, path) => { reads.push(path); return path === 'active.py' ? disk.promise : Promise.resolve({ path, text: 'original', sha1: 'disk' }) },
  }), viewExports + ', agentTeam, onWorkbenchKey, quickOpen, rightVisible, rightTab, changesVisible')
  try {
    const s = view.subject
    s.rightTab.value = 'changes'
    assert.equal(s.changesVisible.value, true)
    s.rightVisible.value = false
    assert.equal(s.changesVisible.value, false, 'collapsed review panel pauses its reads')
    s.rightVisible.value = true
    for (const path of ['background.py', 'active.py']) {
      const doc = s.workbench.openFile({ path, text: 'original', sha1: 'disk' }, { projectId: 'p' })
      s.workbench.update(doc.key, { text: 'keep draft' }); doc.needsReload = true
    }
    await s.selectProject(project('p')); await new Promise(setImmediate)
    props.visible = false; await nextTick()
    assert.equal(s.changesVisible.value, false, 'opening settings pauses change review reads')
    const beforeTeams = teamReads, beforeDiagnostics = diagnosticReads
    assert.equal(await s.agentTeam.refresh(), false)
    s.onWorkbenchKey({ key: 'p', ctrlKey: true, preventDefault: () => assert.fail('hidden workspace captured Ctrl+P') })
    assert.equal(s.quickOpen.value, false)
    disk.resolve({ path: 'active.py', text: 'original', sha1: 'disk' }); await new Promise(setImmediate)
    assert.deepEqual(reads, ['active.py'])
    assert.equal(teamReads, beforeTeams)
    assert.equal(diagnosticReads, beforeDiagnostics)
    props.visible = true; await new Promise(setImmediate)
    assert.equal(s.changesVisible.value, true, 'returning to the editor resumes change review reads')
    assert.deepEqual(reads, ['active.py', 'background.py'])
    assert.equal(s.file.value.text, 'keep draft')
    assert.equal(s.session.value.id, 'session-p')
    assert.equal(leadReads, 1)
    assert.equal(lspReads, 1)
    assert.equal(diagnosticReads, beforeDiagnostics + 1)
  } finally { await view.dispose() }
})

test('a workspace activated while hidden defers its language probing until visible', async () => {
  const props = reactive({ user: { id: 1 }, visible: false })
  let lspReads = 0
  const view = await component('../src/views/CodeView.vue', props, apiFor({ projectLsp: async () => { lspReads++; return { servers: [] } } }), viewExports)
  try {
    await view.subject.selectProject(project('p')); await new Promise(setImmediate)
    assert.equal(lspReads, 0)
    props.visible = true; await new Promise(setImmediate)
    assert.equal(lspReads, 1)
    props.visible = false; await nextTick(); props.visible = true; await new Promise(setImmediate)
    assert.equal(lspReads, 1, 'module navigation retains completed probe data')
  } finally { await view.dispose() }
})

test('the last opened file wins; opening the project form keeps the workspace and closing invalidates pending reads', async () => {
  const first = deferred(), second = deferred(), leaving = deferred()
  const requests = [first, second, leaving]
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, apiFor({
    file: () => requests.shift().promise,
  }), viewExports)
  try {
    await view.subject.selectProject(project('p'))
    const readFirst = view.subject.openFile({ path: 'a.js' })
    const readSecond = view.subject.openFile({ path: 'b.js' })
    second.resolve({ path: 'b.js', text: 'second' })
    await readSecond
    first.resolve({ path: 'a.js', text: 'first' })
    await readFirst
    assert.equal(view.subject.file.value.path, 'b.js')
    view.subject.promptOpenProject()
    assert.equal(view.subject.project.value.id, 'p')
    assert.equal(view.subject.file.value.path, 'b.js')
    assert.equal(view.subject.pickerOpen.value, true)
    view.subject.dismissProjectForm()
    const readLeaving = view.subject.openFile({ path: 'late.js' })
    view.subject.projectAccess.close()
    leaving.resolve({ path: 'late.js', text: 'late' })
    await readLeaving
    assert.equal(view.subject.project.value, null)
    assert.equal(view.subject.file.value, null)
    assert.equal(view.subject.fileBusy.value, false)
  } finally { await view.dispose() }
})

test('diagnostics do not overlap and a save cannot replace a different current file', async () => {
  const diagnostic = deferred(), saved = deferred()
  let diagCalls = 0
  const api = apiFor({ saveFile: () => saved.promise })
  const view = await component('../src/views/CodeView.vue', { user: { id: 1 } }, api, viewExports)
  try {
    await view.subject.selectProject(project('p'))
    api.code.diagnostics = () => { diagCalls += 1; return diagnostic.promise }
    const poll = view.subject.refreshDiagnostics()
    await view.subject.refreshDiagnostics()
    assert.equal(diagCalls, 1)
    diagnostic.resolve({ counts: { error: 3, warning: 0 } })
    await poll
    assert.equal(view.subject.diagnostics.value.counts.error, 3)
    view.subject.file.value = { path: 'a.js', text: 'original' }
    const save = view.subject.saveFile({ path: 'a.js', text: 'saved', sha1: 'old' })
    view.subject.file.value = { path: 'b.js', text: 'other' }
    saved.resolve({ changed: true, sha1: 'new', lines: 1, version: 2 })
    await save
    assert.equal(view.subject.file.value.path, 'b.js')
    assert.equal(view.subject.file.value.text, 'other')
  } finally { await view.dispose() }
})

class Socket {
  static OPEN = 1
  constructor(url) { this.url = url; this.readyState = Socket.OPEN; this.messages = [] }
  send(text) { this.messages.push(JSON.parse(text)) }
  close() { this.readyState = 3; this.closed = true; this.onclose?.() }
}

async function clientClass() {
  const markers = []
  const monaco = {
    MarkerSeverity: { Error: 8, Warning: 4, Info: 2, Hint: 1 },
    editor: { setModelMarkers: (model, owner, items) => markers.push({ model, owner, items }) },
  }
  const loaded = await loadScript('../src/lib/lspClient.js', {
    monaco, clearSemanticTokens, WebSocket: Socket, location: { protocol: 'http:', host: 'localhost' }, getToken: () => 'test-token',
  }, 'LspClient')
  return { ...loaded.subject, markers }
}

test('LSP accepts empty capabilities and restores ready diagnostics when the model opens', async () => {
  const { LspClient, markers } = await clientClass()
  const client = new LspClient({ projectId: 'p', lang: 'python' })
  client.connect()
  assert.match(client.ws.url, /token=test-token/)
  const ready = client.waitForReady()
  client._handle({ method: 'code-agent/ready', params: { capabilities: {}, diagnostics: {
    'file:///a.py': [{ message: 'existing error', severity: 1 }],
  } } })
  await ready
  client.openDocument({ uri: 'file:///a.py', text: 'x', model: { getValue: () => 'x' } })
  assert.equal(client.ready, true)
  assert.equal(markers.at(-1).items[0].message, 'existing error')
  client.dispose()
})

test('disposing or closing LSP rejects pending requests immediately and clears timers', async () => {
  const { LspClient } = await clientClass()
  const client = new LspClient({ projectId: 'p', lang: 'python' })
  client.connect()
  const waiting = assert.rejects(client.waitForReady(60000), /已释放/)
  const request = assert.rejects(client.request('textDocument/hover', {}, 60000), /已释放/)
  const socket = client.ws
  client.dispose()
  await Promise.all([waiting, request])
  assert.equal(socket.closed, true)
  assert.equal(client.pending.size, 0)
  assert.equal(client.readyWaiters.size, 0)
  const closed = new LspClient({ projectId: 'p', lang: 'python' })
  closed.connect()
  const disconnected = assert.rejects(closed.request('textDocument/hover', {}, 60000), /已关闭/)
  closed.ws.close()
  await disconnected
  closed.dispose()
})

test('editor cleanup closes a client still waiting for ready and never opens its stale document', async () => {
  const { LspClient } = await clientClass()
  const props = reactive({ projectId: 'p', tree: 'main', file: { path: 'a.py', uri: 'file:///a.py', lang: 'python', text: 'x' } })
  const pane = await component('../src/components/code/EditorPane.vue', props, apiFor(), 'connectLsp, shutdownLsp, dirty, markSaved', {
    __modules: { '../../lib/lspClient.js': { LspClient } },
  })
  try {
    const model = { getValue: () => 'x', uri: { toString: () => 'file:///a.py' }, dispose() {} }
    pane.context.__editor = { getModel: () => model, dispose() {} }
    vm.runInContext("editor = __editor; activeDocument = {key: 'p/main/a.py',projectId:'p',tree:'main',uri:'file:///a.py',path:'a.py'}", pane.context)
    const connecting = pane.subject.connectLsp()
    for (let index = 0; index < 15 && !vm.runInContext('lsp', pane.context); index++) await Promise.resolve()
    const client = vm.runInContext('lsp', pane.context)
    const socket = client.ws
    pane.subject.shutdownLsp()
    await connecting
    assert.equal(socket.closed, true)
    assert.equal(client.documents.size, 0)
    assert.equal(client.readyWaiters.size, 0)
  } finally { await pane.dispose() }
})

test('a save response preserves text entered while the request was in flight', async () => {
  const props = reactive({ projectId: 'p', tree: 'main', file: { path: 'a.py', text: 'before' } })
  const pane = await component('../src/components/code/EditorPane.vue', props, apiFor(), 'dirty, markSaved')
  try {
    const model = { getValue: () => 'saved plus later edits' }
    pane.context.__editor = { getValue: model.getValue, getModel: () => model, dispose() {} }
    vm.runInContext('editor = __editor', pane.context)
    pane.subject.markSaved('saved')
    props.file.text = 'saved'
    await nextTick()
    assert.equal(pane.subject.dirty.value, true)
    assert.equal(model.getValue(), 'saved plus later edits')
  } finally { await pane.dispose() }
})

test('failed AI sends restore the draft without replacing a newer draft or a new session', async () => {
  const props = reactive({ sessionId: 'session-p', pendingPrompt: '', codeContext: null })
  let delivery = deferred()
  const state = {
    load: async () => {}, send: () => delivery.promise,
    timeline: ref([]), streaming: ref(''), busy: ref(false), web: ref(false), historyLoading: ref(false), historyError: ref(''),
  }
  const pane = await component('../src/components/code/AiPanel.vue', props, apiFor(), 'input, send', {
    nextTick, useAgentSession: () => state, buildToolCards: () => [],
  })
  try {
    pane.subject.input.value = 'first draft'
    const first = pane.subject.send()
    assert.equal(pane.subject.input.value, '')
    delivery.resolve(false)
    await first
    assert.equal(pane.subject.input.value, 'first draft')
    delivery = deferred()
    const second = pane.subject.send()
    pane.subject.input.value = 'newer draft'
    delivery.resolve(false)
    await second
    assert.equal(pane.subject.input.value, 'newer draft')
    delivery = deferred()
    const third = pane.subject.send()
    props.sessionId = 'another-session'
    delivery.resolve(false)
    await third
    assert.equal(pane.subject.input.value, '')
  } finally { await pane.dispose() }
})

test('same relative paths use separate Monaco models across projects and share absolute identity correctly', async () => {
  const models = new Map()
  const monaco = {
    Uri: { parse: (uri) => uri, from: ({ scheme, authority, path }) => `${scheme}://${authority}${path}` },
    editor: {
      getModel: (uri) => models.get(uri),
      createModel: (text, lang, uri) => { const model = { text, lang, uri }; models.set(uri, model); return model },
    },
  }
  const { subject } = await loadScript('../src/lib/monacoSetup.js', { monaco }, 'getModel, modelUri')
  const first = subject.getModel('src/index.js', 'one', 'javascript', '', 'project-one/main')
  const second = subject.getModel('src/index.js', 'two', 'javascript', '', 'project-two/main')
  assert.notEqual(first, second)
  assert.equal(subject.getModel('src/index.js', 'ignored', 'javascript', '', 'project-one/main'), first)
  assert.notEqual(
    subject.getModel('src/index.js', 'one', 'javascript', 'file:///project-one/src/index.js'),
    subject.getModel('src/index.js', 'two', 'javascript', 'file:///project-two/src/index.js'),
  )
})

function editorModule(created) {
  return {
    monacoLanguage: (lang) => lang,
    getModel: (path, text, lang) => ({
      path, getValue: () => text, setValue: (value) => { text = value },
      getLanguageId: () => lang, dispose() {},
    }),
    ensureMonaco: () => ({
      KeyMod: { CtrlCmd: 1 }, KeyCode: { KeyS: 1 },
      editor: { create: (host, { model }) => {
        created.push({ host, model })
        return { getModel: () => model, onDidChangeModelContent() {}, addCommand() {}, addAction() {}, onDidLayoutChange() {}, dispose() {} }
      } },
    }),
  }
}

test('the first file waits for its rendered host before creating Monaco', async () => {
  const props = reactive({ projectId: 'p', tree: 'main', file: null, lspServer: { available: false } })
  const created = []
  let render = null
  const pane = await component('../src/components/code/EditorPane.vue', props, apiFor(), 'mount, hostEl, error', {
    nextTick: () => render ? render.promise : Promise.resolve(),
    __modules: { '../../lib/monacoSetup.js': editorModule(created) },
  })
  try {
    await pane.subject.mount()
    assert.equal(created.length, 0)
    render = deferred()
    props.file = { path: 'first.py', text: 'first', lang: 'python' }
    await nextTick()
    assert.equal(created.length, 0)
    pane.subject.hostEl.value = { rendered: true }
    render.resolve()
    await new Promise(setImmediate)
    assert.equal(pane.subject.error.value, '')
    assert.equal(created.length, 1)
    assert.equal(created[0].model.path, 'first.py')
  } finally { await pane.dispose() }
})

test('switching files during lazy import mounts only the newest file', async () => {
  const props = reactive({ projectId: 'p', tree: 'main', file: { path: 'old.py', text: 'old', lang: 'python' }, lspServer: { available: false } })
  const created = [], imported = deferred()
  const pane = await component('../src/components/code/EditorPane.vue', props, apiFor(), 'mount, hostEl, error', {
    __modules: { '../../lib/monacoSetup.js': imported.promise },
  })
  try {
    pane.subject.hostEl.value = { rendered: true }
    const mounting = pane.subject.mount()
    await new Promise(setImmediate)
    props.file = { path: 'new.py', text: 'new', lang: 'python' }
    await nextTick()
    imported.resolve(editorModule(created))
    await mounting
    await new Promise(setImmediate)
    assert.equal(pane.subject.error.value, '')
    assert.equal(created.length, 1)
    assert.equal(created[0].model.path, 'new.py')
  } finally { await pane.dispose() }
})
