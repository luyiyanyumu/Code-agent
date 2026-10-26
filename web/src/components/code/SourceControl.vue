<script setup>
import UiIcon from '../ui/UiIcon.vue'
// 源代码管理面板（M18 · P4）：分支 / 改动 / 提交 / 拉取推送。
//
// 三条产品约束：
// 1. **危险操作没有按钮**：强推、reset --hard、历史重写由后端硬拒（403），
//    界面上也不提供入口——"点了报错"比"没有按钮"更容易让人误以为能用。
// 2. **网络动作要批注**：pull 只走 `--ff-only`（否则冲突处理被塞进一个按钮里），
//    切分支前工作区必须干净（脏工作区切分支会把改动带过去，是常见事故）。
// 3. **失败要能看到原文**：git 的输出直接展示，不翻译成含糊的"操作失败"。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../api.js'
import { useDialog } from '../../composables/useDialog.js'

const props = defineProps({
  projectId: { type: String, required: true },
})
const emit = defineEmits(['error', 'changed'])
const dialog = useDialog()
let disposed = false
let projectEpoch = 0
let loadRequest = 0
onBeforeUnmount(() => { disposed = true })

const status = ref(null)
const branches = ref([])
const loading = ref(false)
const busy = ref('')
const note = ref('')
const message = ref('')
const selected = ref([])     // 勾选的改动文件（空 = 全部）

const changes = computed(() => status.value?.changes || [])
const isRepo = computed(() => !!status.value?.is_repo)

async function load() {
  const pid = props.projectId, epoch = projectEpoch, request = ++loadRequest
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch && loadRequest === request
  loading.value = true
  try {
    const result = await api.code.git(pid)
    if (!current()) return
    status.value = result
    const list = result?.is_repo ? (await api.code.gitBranches(pid)).branches || [] : []
    if (current()) branches.value = list
  } catch (e) {
    if (current()) emit('error', e.message)
  } finally {
    if (current()) loading.value = false
  }
}

function toggle(path) {
  selected.value = selected.value.includes(path)
    ? selected.value.filter((p) => p !== path)
    : [...selected.value, path]
}

async function commit() {
  const text = message.value.trim()
  if (!text) {
    note.value = '提交信息不能为空'
    return
  }
  busy.value = 'commit'
  note.value = ''
  try {
    const result = await api.code.gitCommit(
      props.projectId, text, selected.value.length ? selected.value : null
    )
    message.value = ''
    selected.value = []
    note.value = '已提交'
    status.value = result
    await load()
    emit('changed', result)
  } catch (e) {
    emit('error', e.message)
  } finally {
    busy.value = ''
  }
}

async function run(action, extra = {}, confirmText = '') {
  if (busy.value || disposed) return
  const pid = props.projectId, epoch = projectEpoch, branch = status.value?.branch
  const payload = { ...extra }
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch
  const perform = async () => {
    if (!current() || status.value?.branch !== branch) throw new Error('当前项目或分支已变化，请关闭弹窗后重新操作。')
    busy.value = action
    note.value = ''
    const result = await api.code.gitAction(pid, action, payload)
    if (!result.ok) throw new Error(result.output || 'Git 操作未成功，请处理后重试。')
    if (current()) {
      note.value = `${action === 'pull' ? '拉取' : action === 'push' ? '推送' : action === 'stash' ? '暂存' : '元数据更新'}完成`
      status.value = result
      emit('changed', result)
    }
    return result
  }
  busy.value = `confirm:${action}`
  try {
    if (confirmText) return await dialog.open({
      title: action === 'push' ? '推送当前分支？' : action === 'pull' ? '拉取远端更新？' : '暂存当前改动？',
      description: confirmText,
      details: `分支：${branch || '未命名'}\n远端：${status.value?.remote || 'origin'}`,
      tone: 'warning', icon: 'git-branch',
      confirmText: action === 'push' ? '推送分支' : action === 'pull' ? '拉取更新' : '暂存改动',
      pendingText: '正在执行 Git 操作…', onConfirm: perform,
    })
    return await perform()
  } catch (e) { if (current()) emit('error', e.message) }
  finally { if (current()) busy.value = '' }
}

// 下拉框用 v-model：`:value` 会在 option 渲染前写进 select，导致分支名显示为空。
const branchModel = computed({
  get: () => status.value?.branch || '',
  set: (value) => { checkout(value) },
})

async function checkout(branch) {
  if (!branch || branch === status.value?.branch || busy.value || disposed) return
  if (status.value?.dirty) { note.value = '请先提交或暂存当前改动，再切换分支。'; return }
  const pid = props.projectId, epoch = projectEpoch, previous = status.value?.branch
  const current = () => !disposed && props.projectId === pid && projectEpoch === epoch
  busy.value = `confirm:checkout:${branch}`
  try {
    return await dialog.open({
      title: '切换项目分支？', description: '工作区文件将切换到目标分支；当前工作区必须保持干净。',
      details: `${previous || '当前分支'} → ${branch}`, icon: 'git-branch', tone: 'warning',
      confirmText: '切换分支', pendingText: '正在切换…',
      onConfirm: async () => {
        if (!current() || status.value?.branch !== previous) throw new Error('当前项目或分支已变化，请关闭弹窗后重新操作。')
        if (status.value?.dirty) throw new Error('工作区出现新改动，请先提交或暂存后再切换。')
        busy.value = `checkout:${branch}`
        const result = await api.code.gitCheckout(pid, branch)
        if (!current()) return result
        status.value = result
        note.value = `已切到 ${branch}`
        await load()
        if (current()) emit('changed', result)
        return result
      },
    })
  } finally { if (current()) busy.value = '' }
}

watch(() => props.projectId, () => { projectEpoch++; selected.value = []; status.value = null; busy.value = ''; load() })
load()
defineExpose({ load })
</script>

<template>
  <div class="scm">
    <div class="head">
      <UiIcon name="git-branch" :size="15" class="panel-icon" /><span class="title">源代码管理</span>
      <span class="spacer" />
      <button class="ghost" title="刷新" aria-label="刷新面板" @click="load"><UiIcon name="refresh" :size="14" /></button>
    </div>

    <div v-if="loading && !status" class="muted pad">读取中…</div>
    <div v-else-if="!isRepo" class="muted pad">
      当前项目不是 git 仓库：{{ status?.error || '无法读取仓库状态' }}。
      在「任务」面板里创建任务时可选自动初始化。
    </div>

    <template v-else>
      <div class="branchbar">
        <span class="branch mono" title="当前分支">{{ status?.branch || '（未知分支）' }}</span>
        <select v-model="branchModel" :disabled="!!busy" aria-label="切换分支" title="切换分支（工作区必须干净）">
          <option v-for="b in branches" :key="b.name" :value="b.name">{{ b.name }}</option>
        </select>
        <span v-if="status?.ahead || status?.behind" class="muted tiny">
          ↑{{ status?.ahead || 0 }} ↓{{ status?.behind || 0 }}
        </span>
        <span v-if="status?.dirty" class="dirty">未提交</span>
      </div>
      <div class="remote muted tiny">{{ status?.remote || '（未配置远端）' }}</div>

      <div class="actions">
        <button class="ghost small" :disabled="!!busy" @click="run('fetch')">拉取元数据</button>
        <button
          class="ghost small" :disabled="!!busy"
          title="只允许快进合并"
          @click="run('pull', {}, '从远端拉取（--ff-only）？有本地改动时可能失败，请先提交或暂存。')"
        >拉取</button>
        <button
          class="ghost small" :disabled="!!busy"
          @click="run('push', { branch: status?.branch }, `推送当前分支 ${status?.branch} 到 origin？`)"
        >推送</button>
        <button
          class="ghost small" :disabled="!!busy"
          title="把本地改动暂存起来"
          @click="run('stash', {}, '暂存当前改动？')"
        >暂存</button>
      </div>

      <div v-if="note" class="note">{{ note }}</div>

      <ul class="changes">
        <li v-for="c in changes" :key="c.path" @click="toggle(c.path)">
          <input type="checkbox" :checked="selected.includes(c.path)" @click.stop="toggle(c.path)" />
          <span class="cstatus">{{ c.status || 'M' }}</span>
          <span class="mono ellipsis" :title="c.path">{{ c.path }}</span>
        </li>
      </ul>
      <div v-if="!changes.length" class="muted pad">工作区干净</div>

      <div class="commit">
        <textarea v-model="message" rows="2" placeholder="提交信息（未勾选文件时提交全部改动）" />
        <div class="row">
          <span class="muted tiny">
            {{ selected.length ? `已选 ${selected.length} 个文件` : '全部改动' }}
          </span>
          <span class="spacer" />
          <button class="primary" :disabled="!!busy || !changes.length" @click="commit">
            {{ busy === 'commit' ? '提交中…' : '提交' }}
          </button>
        </div>
      </div>

      <p class="muted tiny foot">
        平台不提供强推 / reset --hard / 历史重写：这些操作不可逆，请在终端里自行执行。
      </p>
    </template>
  </div>
</template>

<style scoped>
.scm { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; }
.head { display: flex; align-items: center; gap: 8px; min-height: 42px; flex: none; padding: 7px 14px; border-bottom: 1px solid var(--border); font-size: var(--fs-sm); }
.title { font-weight: var(--fw-semibold); }.panel-icon { flex: none; color: var(--muted); }
.spacer { flex: 1; }
.ghost { display: inline-flex; align-items: center; justify-content: center; min-height: 26px; border: none; background: transparent; color: var(--muted); padding: 3px 6px; }
.ghost:hover { background: var(--hover); }
.ghost.small { font-size: var(--fs-xs); }
.pad { padding: var(--sp-2-5); font-size: var(--fs-xs); }

.branchbar { min-height: 40px; display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1-5) var(--sp-2-5); }
.branch { flex: none; font-size: var(--fs-xs); font-weight: var(--fw-semibold); }
.branchbar select { flex: 1; min-width: 0; font-size: var(--fs-xs); padding: 3px var(--sp-1-5); }
.dirty { font-size: var(--fs-xs); padding: var(--sp-hair) 5px; border-radius: var(--r-sm); background: var(--warn-soft); color: var(--warn); }
.remote { padding: 0 var(--sp-2-5) var(--sp-1-5); font-size: var(--fs-xs); word-break: break-all; }
.actions { display: flex; flex-wrap: wrap; gap: var(--sp-1); padding: 0 var(--sp-2) var(--sp-2); border-bottom: 1px solid var(--border); }
.note { padding: var(--sp-1-5) var(--sp-2-5); font-size: var(--fs-xs); color: var(--accent); background: var(--accent-soft); }

.changes { list-style: none; margin: 0; padding: var(--sp-1); overflow-y: auto; flex: 1; }
.changes li { min-height: 29px; display: flex; align-items: center; gap: var(--sp-1-5); padding: 3px var(--sp-1-5); border-radius: var(--r-sm); font-size: var(--fs-xs); cursor: pointer; }
.changes li:hover { background: var(--bg); }
.cstatus { font-size: var(--fs-xs); color: var(--warn); width: 16px; flex: none; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.commit { background: var(--panel-soft); border-top: 1px solid var(--border); padding: var(--sp-2) var(--sp-2-5); }
.commit textarea { width: 100%; font-size: var(--fs-sm); }
.commit .row { display: flex; align-items: center; margin-top: var(--sp-1-5); }
.tiny { font-size: var(--fs-xs); }
.foot { padding: 0 var(--sp-2-5) var(--sp-2); margin: 0; }
</style>
