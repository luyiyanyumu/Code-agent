<script setup>
// 目录选择对话框（M18）：按 Windows「选择文件夹」的样子做——左侧快速访问、
// 顶部前进/后退/上一级 + 面包屑、列表带修改时间、底部「文件夹:」+ 选择/取消。
//
// 为什么值得做成对话框而不是页面内嵌一小块：选工作区是**一次性、要看清目录**的动作，
// 内嵌列表在窄栏里只能看到几个名字（真机上用户因此以为"只能选 C 盘"）。
// 这里保留服务端列目录（浏览器拿不到文件系统），但把交互做成用户熟悉的样子。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../../api.js'

const props = defineProps({
  start: { type: String, default: '' },   // 初始目录（空 = 服务端给默认起点）
})
const emit = defineEmits(['pick', 'close'])

const data = ref(null)          // { path, parent, dirs, roots, places, breadcrumbs, hidden_count }
const history = ref([])         // 访问历史（← → 用）
const cursor = ref(-1)
const loading = ref(false)
const error = ref('')
const query = ref('')           // 列表过滤（只过滤当前目录，不递归）
const selected = ref('')        // 列表里选中项的路径
const showHidden = ref(false)
const pathInput = ref('')       // 底部「文件夹:」输入框
const listEl = ref(null)
//: 请求序号：只接受最后一次请求的结果（防止迟到的响应覆盖用户已经改过的状态）
let reqSeq = 0

const dirs = computed(() => {
  const q = query.value.trim().toLowerCase()
  const items = data.value?.dirs || []
  return q ? items.filter((d) => d.name.toLowerCase().includes(q)) : items
})
const canBack = computed(() => cursor.value > 0)
const canForward = computed(() => cursor.value >= 0 && cursor.value < history.value.length - 1)
const currentPath = computed(() => data.value?.path || '')

function fmtTime(seconds) {
  if (!seconds) return ''
  const d = new Date(seconds * 1000)
  if (isNaN(d.getTime())) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function load(target, { push = true } = {}) {
  // 迟到的响应一律丢弃：对话框一打开就立刻粘贴路径并回车的用户，
  // 会撞上"初始目录"这次请求晚回来，把输入框与选中项覆盖回主目录
  // （真机踩过：界面显示用户主目录，用户以为自己选的是 E:\projects\my-app）。
  const seq = ++reqSeq
  loading.value = true
  error.value = ''
  try {
    const payload = await api.code.fsList(target || '', { hidden: showHidden.value })
    if (seq !== reqSeq) return
    data.value = payload
    pathInput.value = payload.path || ''
    selected.value = ''
    if (push) {
      history.value = history.value.slice(0, cursor.value + 1).concat(payload.path)
      cursor.value = history.value.length - 1
    }
    if (listEl.value) listEl.value.scrollTop = 0
  } catch (e) {
    if (seq !== reqSeq) return
    error.value = e.message
  } finally {
    if (seq === reqSeq) loading.value = false
  }
}

function go(target) {
  return load(target)
}

function back() {
  if (!canBack.value) return
  cursor.value -= 1
  load(history.value[cursor.value], { push: false })
}

function forward() {
  if (!canForward.value) return
  cursor.value += 1
  load(history.value[cursor.value], { push: false })
}

function up() {
  if (data.value?.parent) load(data.value.parent)
}

async function toggleHidden() {
  showHidden.value = !showHidden.value
  await load(currentPath.value, { push: false })
}

/** 单击选中（底部「文件夹:」跟着变），双击进入——与原生对话框一致。 */
function onRowClick(item) {
  selected.value = item.path
  pathInput.value = item.path
}

function onRowDblClick(item) {
  go(item.path)
}

function confirm() {
  const chosen = (selected.value || pathInput.value || currentPath.value || '').trim()
  if (!chosen) return
  emit('pick', chosen)
}

function onKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
  }
}

onMounted(() => {
  load(props.start || '')
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
watch(showHidden, () => load(currentPath.value, { push: false }))
</script>

<template>
  <div class="dirmask" @click.self="emit('close')">
    <div class="dirdlg" role="dialog" aria-label="选择工作区目录">
      <header>
        <span class="title">选择工作区目录</span>
        <span class="spacer" />
        <button class="ghost" title="关闭" @click="emit('close')">×</button>
      </header>

      <div class="toolbar">
        <button class="icon" :disabled="!canBack" title="后退" @click="back">←</button>
        <button class="icon" :disabled="!canForward" title="前进" @click="forward">→</button>
        <button class="icon" :disabled="!data?.parent" title="上一级" @click="up">↑</button>

        <nav class="crumbs">
          <template v-for="(c, i) in data?.breadcrumbs || []" :key="c.path">
            <button class="crumb" :title="c.path" @click="go(c.path)">{{ c.name }}</button>
            <span v-if="i < (data.breadcrumbs.length - 1)" class="sep">›</span>
          </template>
        </nav>

        <button class="icon" title="刷新" :disabled="loading" @click="load(currentPath, { push: false })">⟳</button>
        <input v-model="query" class="find" placeholder="在当前目录中筛选…" />
      </div>

      <div class="body">
        <aside class="places">
          <div v-for="p in data?.places || []" :key="p.path" class="place" :class="{ on: p.path === currentPath }">
            <button :title="p.path" @click="go(p.path)">
              <span class="ic">{{ p.kind === 'projects' ? '🧰' : p.kind === 'recent' ? '🕘' : p.kind === 'home' ? '🏠' : '📁' }}</span>
              <span class="nm">{{ p.name }}</span>
            </button>
          </div>
          <div class="places-sep" />
          <div v-for="r in data?.roots || []" :key="`root-${r.path}`" class="place">
            <button :title="r.path" @click="go(r.path)">
              <span class="ic">💽</span><span class="nm">{{ r.name }}</span>
            </button>
          </div>
        </aside>

        <section class="listing">
          <div class="cols">
            <span class="c-name">名称</span>
            <span class="c-time">修改日期</span>
            <span class="c-kind">类型</span>
          </div>
          <div v-if="loading" class="muted pad">读取中…</div>
          <div v-else-if="error" class="err pad">{{ error }}</div>
          <div v-else-if="!dirs.length" class="muted pad">
            {{ query ? '没有匹配的子目录' : '没有子目录' }}
          </div>
          <ul v-else ref="listEl" class="rows">
            <li
              v-for="item in dirs"
              :key="item.path"
              :class="{ on: item.path === selected }"
              @click="onRowClick(item)"
              @dblclick="onRowDblClick(item)"
            >
              <span class="c-name">
                <span class="ic">📁</span>{{ item.name }}
                <span v-if="item.is_project" class="badge">工程</span>
              </span>
              <span class="c-time mono">{{ fmtTime(item.mtime) }}</span>
              <span class="c-kind muted">{{ item.is_project ? '代码工程' : '文件夹' }}</span>
            </li>
          </ul>
          <div class="foot-note">
            <span v-if="data?.is_project" class="ok">当前目录看起来是一个代码工程（含 .git / 清单文件）</span>
            <span class="spacer" />
            <span v-if="data?.hidden_count" class="muted tiny">已隐藏 {{ data.hidden_count }} 个系统/隐藏目录</span>
            <button class="ghost tiny" @click="toggleHidden">
              {{ showHidden ? '隐藏系统目录' : '显示全部' }}
            </button>
          </div>
        </section>
      </div>

      <footer>
        <label class="pathfield">
          <span>文件夹:</span>
          <input
            v-model="pathInput"
            class="mono"
            placeholder="可直接输入或粘贴路径后回车"
            @keyup.enter="go(pathInput.trim())"
          />
        </label>
        <button class="primary" :disabled="!pathInput.trim() && !currentPath" @click="confirm">选择此文件夹</button>
        <button @click="emit('close')">取消</button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.dirmask {
  position: fixed; inset: 0; z-index: 40; display: flex; align-items: center; justify-content: center;
  background: var(--overlay); backdrop-filter: blur(3px);
}
.dirdlg {
  width: min(920px, 94vw); height: min(620px, 86vh); display: flex; flex-direction: column;
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
  box-shadow: var(--shadow-pop); font-size: var(--fs-sm);
}
header { min-height: 46px; display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--border); }
.title { font-weight: var(--fw-semibold); font-size: var(--fs-sm); }
.spacer { flex: 1; }
.ghost { border: none; background: transparent; color: var(--muted); padding: var(--sp-0-5) var(--sp-1-5); }
.ghost:hover { background: var(--hover); }

.toolbar { display: flex; align-items: center; gap: var(--sp-1-5); padding: var(--sp-1-5) var(--sp-2-5); border-bottom: 1px solid var(--border); }
.icon {
  flex: none; width: 26px; height: 24px; border-radius: var(--r-sm); border: 1px solid var(--border);
  background: var(--panel); color: var(--text);
}
.icon:disabled { color: var(--border-strong); }
.crumbs { display: flex; align-items: center; gap: var(--sp-0-5); flex: 1; min-width: 0; overflow: hidden; }
.crumb {
  border: 1px solid transparent; background: transparent; padding: var(--sp-0-5) var(--sp-1-5); border-radius: var(--r-sm);
  max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.crumb:hover { background: var(--hover); }
.sep { color: var(--muted); }
.find { width: 180px; flex: none; font-size: var(--fs-xs); }

.body { flex: 1; display: flex; min-height: 0; }
.places { width: 190px; flex: none; border-right: 1px solid var(--border); overflow-y: auto; padding: var(--sp-1-5); background: var(--panel-soft); }
.place button {
  display: flex; align-items: center; gap: var(--sp-1-5); width: 100%; text-align: left;
  border: none; background: transparent; padding: 5px 7px; border-radius: var(--r-sm);
}
.place button:hover { background: #eef2f7; }
.place.on button { background: var(--accent-soft); color: var(--accent); font-weight: var(--fw-semibold); }
.ic { flex: none; }
.nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.places-sep { height: 1px; background: var(--border); margin: var(--sp-1-5) var(--sp-1); }

.listing { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.cols, .rows li { display: grid; grid-template-columns: 1fr 160px 96px; align-items: center; gap: var(--sp-2); }
.cols { padding: var(--sp-1-5) var(--sp-2-5); border-bottom: 1px solid var(--border); color: var(--muted); font-size: var(--fs-xs); }
.rows { list-style: none; margin: 0; padding: var(--sp-0-5) 0; overflow-y: auto; flex: 1; }
.rows li { padding: 5px var(--sp-2-5); cursor: default; }
.rows li:hover { background: #f5f8fc; }
.rows li.on { background: var(--accent-soft); }
.c-name { display: flex; align-items: center; gap: var(--sp-1-5); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.c-time { font-size: var(--fs-xs); color: var(--text-2); }
.badge { font-size: var(--fs-xs); padding: 0 5px; border-radius: var(--r-sm); background: var(--ok-soft); color: var(--ok); }
.pad { padding: var(--sp-3-5) var(--sp-3); }
.err { color: var(--err); }
.foot-note { display: flex; align-items: center; gap: var(--sp-2); padding: 5px var(--sp-2-5); border-top: 1px solid var(--border); font-size: var(--fs-xs); }
.foot-note .ok { color: var(--ok); }
.tiny { font-size: var(--fs-xs); }

footer { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-top: 1px solid var(--border); }
.pathfield { display: flex; align-items: center; gap: var(--sp-2); flex: 1; min-width: 0; }
.pathfield input { flex: 1; min-width: 0; font-size: var(--fs-sm); }
@media (max-width: 600px) { .dirdlg { width: calc(100vw - 24px); height: 85vh; }.places { width: 110px; }.cols, .rows li { grid-template-columns: minmax(0, 1fr); }.c-time, .cols > :not(:first-child) { display: none; }.toolbar { flex-wrap: wrap; }.find { width: 100%; }footer { flex-wrap: wrap; }.pathfield { flex-basis: 100%; } }
</style>
