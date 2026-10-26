<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, readFileBase64 } from '../api.js'
import { useDialog } from '../composables/useDialog.js'
import PageHeader from '../components/ui/PageHeader.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import UiIcon from '../components/ui/UiIcon.vue'

const props = defineProps({ user: { type: Object, default: null } })
const isAdmin = computed(() => props.user?.role === 'admin')

const docs = ref([])
const stats = ref({})
// 分区：human 正式区（人工权威）| llm 隔离区（模型沉淀 / LLM 编译，待审核）
const area = ref('human')
const error = ref('')
const notice = ref('')
const busy = ref('')
const dialog = useDialog()

const filteredDocs = computed(() => docs.value.filter((d) => (d.origin || 'human') === area.value))
const humanCount = computed(() => docs.value.filter((d) => (d.origin || 'human') === 'human').length)
const pendingCount = computed(() => docs.value.filter((d) => d.origin === 'llm').length)

// 搜索
const searchQ = ref('')
const searching = ref(false)
const searchResults = ref(null) // null=未搜索；[]=无结果

// 文档展开
const expanded = ref('')
const docContents = ref({})

// 新建 / 编辑
const editModal = ref(false)
const editId = ref('') // 空=新建
const editOrigin = ref('human') // 编辑条目的所在区
const editForm = ref({ title: '', tags: '', content: '' })
const saving = ref(false)

// 导入目录
const importDirModal = ref(false)
const importDir = ref('')
const importing = ref(false)

// 上传文件
const uploadModal = ref(false)
const uploadFiles = ref([])
const uploading = ref(false)
let reloadRequest = null

async function load() {
  error.value = ''
  try {
    ;[docs.value, stats.value] = await Promise.all([api.knowledge(), api.knowledgeStats()])
  } catch (e) {
    error.value = e.message
  }
}

// Re-entry updates server data while preserving edits, uploads and search state.
function reload() {
  if (busy.value || saving.value || importing.value || uploading.value) return Promise.resolve()
  if (reloadRequest) return reloadRequest
  reloadRequest = load().finally(() => { reloadRequest = null })
  return reloadRequest
}

async function doSearch() {
  const q = searchQ.value.trim()
  if (!q) return
  searching.value = true
  error.value = ''
  try {
    searchResults.value = await api.knowledgeSearch(q, 5)
  } catch (e) {
    error.value = e.message
  } finally {
    searching.value = false
  }
}

async function toggleExpand(d) {
  if (expanded.value === d.id) {
    expanded.value = ''
    return
  }
  expanded.value = d.id
  if (!(d.id in docContents.value)) {
    try {
      const full = await api.knowledgeGet(d.id, d.origin)
      docContents.value[d.id] = full.content || ''
    } catch {
      docContents.value[d.id] = '(加载失败)'
    }
  }
}

function openCreate() {
  editId.value = ''
  editForm.value = { title: '', tags: '', content: '' }
  editModal.value = true
}

async function openEdit(doc) {
  try {
    const full = await api.knowledgeGet(doc.id, doc.origin)
    editId.value = doc.id
    editOrigin.value = doc.origin || 'human'
    editForm.value = {
      title: full.title || '',
      tags: (full.tags || []).join(', '),
      content: full.content || '',
    }
    editModal.value = true
  } catch (e) {
    error.value = e.message
  }
}

async function submitEdit() {
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const payload = {
      title: editForm.value.title.trim(),
      content: editForm.value.content,
      tags: editForm.value.tags
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
    }
    if (editId.value) {
      if (editOrigin.value === 'llm') payload.origin = 'llm'
      await api.knowledgeUpdate(editId.value, payload)
    } else {
      await api.knowledgeCreate(payload)
    }
    notice.value = editId.value ? '文档已更新' : '文档已创建'
    editModal.value = false
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function removeDoc(doc) {
  const { id, title, origin } = doc
  return dialog.open({
    title: '删除知识文档',
    description: '文档将从知识库移除，之后的检索不再包含它。',
    details: `${title}\n${origin === 'llm' ? '待审核' : '正式文档'} · ${id}`,
    tone: 'danger',
    icon: 'book',
    confirmText: '删除文档',
    pendingText: '正在删除…',
    onConfirm: async () => {
      busy.value = id
      error.value = ''
      notice.value = ''
      try {
        await api.knowledgeDelete(id, origin)
        notice.value = `已删除《${title}》`
        await load()
      } finally {
        busy.value = ''
      }
    },
  })
}

async function promoteDoc(doc) {
  busy.value = doc.id
  error.value = ''
  notice.value = ''
  try {
    await api.knowledgePromote(doc.id)
    notice.value = `已采纳《${doc.title}》→ 正式区`
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

async function compileDoc(doc) {
  busy.value = doc.id
  error.value = ''
  notice.value = ''
  try {
    const r = await api.knowledgeCompile(doc.id)
    notice.value = `已编译出 ${r.pages} 个 wiki 页，进入隔离区待审核`
    area.value = 'llm'
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

async function submitImportDir() {
  importing.value = true
  error.value = ''
  notice.value = ''
  try {
    const r = await api.knowledgeImportDir(importDir.value.trim())
    notice.value = `已从目录导入 ${r.imported} 篇文档`
    importDirModal.value = false
    importDir.value = ''
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    importing.value = false
  }
}

function onUploadFiles(e) {
  uploadFiles.value = Array.from(e.target.files || []).filter((f) =>
    /\.(md|markdown|txt)$/i.test(f.name)
  )
}

async function submitUpload() {
  if (!uploadFiles.value.length) {
    error.value = '请先选择 .md / .txt 文件'
    return
  }
  uploading.value = true
  error.value = ''
  notice.value = ''
  try {
    const files = []
    for (const f of uploadFiles.value) {
      files.push({ filename: f.name, content_b64: await readFileBase64(f) })
    }
    const r = await api.knowledgeImportFiles(files)
    notice.value = `已导入 ${r.imported} 篇文档` + (r.errors?.length ? `（${r.errors.length} 个失败）` : '')
    uploadModal.value = false
    uploadFiles.value = []
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    uploading.value = false
  }
}

function fmt(ts) {
  return (ts || '').replace('T', ' ').slice(0, 16)
}

onMounted(reload)
defineExpose({ reload })
</script>

<template>
  <div class="page knowledge-page">
    <PageHeader title="知识库" description="整理团队知识，在对话中随时找到可靠的参考。" eyebrow="资源" icon="book">
      <template #actions>
        <button @click="load" title="刷新知识库" aria-label="刷新知识库"><UiIcon name="refresh" :size="15" /></button>
        <template v-if="isAdmin">
          <button @click="importDirModal = true">导入目录</button>
          <button @click="uploadModal = true"><UiIcon name="upload" :size="15" />上传文件</button>
          <button class="primary" @click="openCreate"><UiIcon name="plus" :size="15" />新建文档</button>
        </template>
      </template>
    </PageHeader>

    <p v-if="error" class="feedback err" role="alert">{{ error }}</p>
    <p v-if="notice" class="feedback ok" role="status">{{ notice }}</p>

    <div class="stats card" aria-label="知识库概览">
      <div class="stat">
        <div class="stat-value">{{ stats.docs ?? 0 }}</div>
        <div class="stat-label">文档数</div>
        <div class="muted stat-hint">可供检索的知识文档</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ stats.pending ?? 0 }}</div>
        <div class="stat-label">待审核</div>
        <div class="muted stat-hint">模型整理，等待确认</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ stats.chunks ?? 0 }}</div>
        <div class="stat-label">知识片段</div>
        <div class="muted stat-hint">帮助定位相关内容</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ stats.embedding ? '向量' : 'BM25' }}</div>
        <div class="stat-label">检索模式</div>
        <div class="muted stat-hint">{{ stats.embedding ? stats.model : '按关键词匹配内容' }}</div>
      </div>
    </div>

    <!-- 搜索 -->
    <section class="card block search-panel">
      <div class="section-heading"><h3>搜索知识</h3><span class="muted">用问题找到相关内容</span></div>
      <form class="search-row" @submit.prevent="doSearch">
        <UiIcon name="search" :size="18" />
        <input
          v-model="searchQ"
          aria-label="搜索知识库"
          placeholder="输入问题或关键词…"
        />
        <button type="submit" class="primary" :disabled="searching || !searchQ.trim()">
          {{ searching ? '检索中…' : '检索' }}
        </button>
      </form>

      <div v-if="searchResults !== null" class="results">
        <div v-for="(h, i) in searchResults" :key="h.doc_id + h.chunk" class="result-item">
          <div class="result-head">
            <span class="badge s-ok">#{{ i + 1 }}</span>
            <strong>{{ h.title }}</strong>
            <span class="muted mono" style="font-size:var(--fs-xs)">相关度 {{ h.score }}</span>
          </div>
          <p class="result-text">{{ h.text }}</p>
        </div>
        <EmptyState v-if="!searchResults.length" icon="search" title="没有找到相关内容" description="试试更简短的关键词，或补充一份相关文档。" />
      </div>
    </section>

    <!-- 文档列表（正式区 / 隔离区） -->
    <section class="card block documents-panel">
      <div class="list-head page-toolbar">
        <div class="tabs" aria-label="知识分区">
          <button class="chip" :class="{ active: area === 'human' }" :aria-pressed="area === 'human'" @click="area = 'human'">
            正式文档 <span>{{ humanCount }}</span>
          </button>
          <button class="chip" :class="{ active: area === 'llm' }" :aria-pressed="area === 'llm'" @click="area = 'llm'">
            待审核 <span>{{ pendingCount }}</span>
          </button>
        </div>
      </div>
      <p v-if="area === 'llm'" class="muted hint">
        这里是模型整理的内容。检查后点击「采纳到正式区」，即可作为正式知识保留。
      </p>

      <div class="items">
        <div v-for="d in filteredDocs" :key="d.origin + d.id" class="item">
          <button class="item-head" :aria-expanded="expanded === d.id" @click="toggleExpand(d)">
            <span class="document-icon"><UiIcon name="file" :size="18" /></span>
            <span class="document-title"><strong>{{ d.title }}</strong><span class="document-meta"><code class="mono name">{{ d.id }}</code><span>{{ fmt(d.updated_at) }}</span></span></span>
            <span class="document-tags"><span v-if="d.origin === 'llm'" class="badge s-staging">待审核</span><span v-for="t in d.tags" :key="t" class="tag">{{ t }}</span></span>
            <UiIcon name="chevron-down" :size="16" class="chev" :class="{ expanded: expanded === d.id }" />
          </button>
          <div v-if="expanded === d.id" class="detail">
            <div v-if="d.source" class="muted" style="font-size:var(--fs-xs); margin-bottom:6px">
              来源：{{ d.source }}
            </div>
            <pre class="mono content">{{ (expanded === d.id && docContents[d.id]) || '' }}</pre>
            <div v-if="isAdmin" class="detail-actions">
              <template v-if="d.origin === 'llm'">
                <button class="small primary" :disabled="busy === d.id" @click="promoteDoc(d)">
                  采纳到正式区
                </button>
                <button class="small" @click="openEdit(d)">编辑</button>
                <button class="small danger" :disabled="busy === d.id" @click="removeDoc(d)">删除</button>
              </template>
              <template v-else>
                <button
                  class="small"
                  :disabled="busy === d.id"
                  title="用 LLM 把本文编译成结构化 wiki 页（按主题拆页）"
                  @click="compileDoc(d)"
                >
                  LLM 编译
                </button>
                <button class="small" @click="openEdit(d)">编辑</button>
                <button class="small danger" :disabled="busy === d.id" @click="removeDoc(d)">删除</button>
              </template>
            </div>
          </div>
        </div>
        <EmptyState v-if="!filteredDocs.length" icon="book" :title="area === 'llm' ? '没有待审核的文档' : '建立你的第一份知识'" :description="area === 'llm' ? '模型整理或编译出的内容会出现在这里，等待你审核。' : (isAdmin ? '上传已有资料，或新建文档，逐步积累可复用的知识。' : '知识文档将在管理员添加后显示。')">
          <button v-if="area === 'human' && isAdmin" @click="uploadModal = true"><UiIcon name="upload" :size="15" />上传文件</button>
        </EmptyState>
      </div>
    </section>

    <!-- 弹窗：新建 / 编辑 -->
    <div v-if="editModal" class="modal-mask" @click.self="editModal = false">
      <div class="card modal wide" role="dialog" aria-modal="true" aria-labelledby="knowledge-edit-title">
        <h3 id="knowledge-edit-title">{{ editId ? '编辑文档' : '新建文档' }}</h3>
        <div class="form-grid">
          <label for="knowledge-title">标题</label>
          <input id="knowledge-title" v-model="editForm.title" placeholder="文档标题（默认取文件名）" />
          <label for="knowledge-tags">标签</label>
          <input id="knowledge-tags" v-model="editForm.tags" placeholder="用逗号分隔，如 产品, 售后" />
          <label for="knowledge-content">正文</label>
          <textarea id="knowledge-content" v-model="editForm.content" rows="14" placeholder="知识正文（Markdown），按空行分块"></textarea>
        </div>
        <div class="actions">
          <button @click="editModal = false">取消</button>
          <button class="primary" :disabled="saving" @click="submitEdit">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 弹窗：导入目录 -->
    <div v-if="importDirModal" class="modal-mask" @click.self="importDirModal = false">
      <div class="card modal" role="dialog" aria-modal="true" aria-label="从目录导入知识">
        <h3>从目录导入</h3>
        <p class="muted" style="font-size:var(--fs-sm)">
          扫描服务端磁盘上的目录，把其中所有 .md / .txt 读入知识库（frontmatter 的 title 优先，否则用文件名）。
        </p>
        <input v-model="importDir" aria-label="知识文档目录" placeholder="如 D:/docs 或 /data/wiki" />
        <div class="actions">
          <button @click="importDirModal = false">取消</button>
          <button class="primary" :disabled="importing || !importDir.trim()" @click="submitImportDir">
            {{ importing ? '导入中…' : '导入' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 弹窗：上传文件 -->
    <div v-if="uploadModal" class="modal-mask" @click.self="uploadModal = false">
      <div class="card modal" role="dialog" aria-modal="true" aria-label="上传知识文件">
        <h3>上传知识文件</h3>
        <p class="muted" style="font-size:var(--fs-sm)">选择 .md / .txt 文件（可多选），作为知识文档导入。</p>
        <input type="file" aria-label="选择知识文件" accept=".md,.markdown,.txt" multiple @change="onUploadFiles" />
        <p v-if="uploadFiles.length" class="muted" style="font-size:var(--fs-xs)">
          已选 {{ uploadFiles.length }} 个：{{ uploadFiles.map((f) => f.name).join('、') }}
        </p>
        <div class="actions">
          <button @click="uploadModal = false">取消</button>
          <button class="primary" :disabled="uploading" @click="submitUpload">
            {{ uploading ? '上传中…' : '上传并导入' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 28px; overflow-y: auto; background: var(--bg); }
.page > * { max-width: 1440px; margin-left: auto; margin-right: auto; }
h3 { margin: 0; font-size: 15px; font-weight: 650; }
.feedback { padding: 10px 13px; margin-bottom: 16px; border-radius: 7px; font-size: 12px; }
.err { color: var(--err); background: var(--err-soft, #fff1f2); }
.ok { color: var(--ok); background: var(--ok-soft, #edf9f3); }
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); padding: 20px 0; margin-bottom: 20px; }
.stat { padding: 0 24px; min-width: 0; }
.stat + .stat { border-left: 1px solid var(--border); }
.stat-value { font-size: 25px; font-weight: 650; line-height: 1.3; letter-spacing: -.7px; margin-bottom: 7px; }
.stat-label { font-size: 12px; color: var(--text); font-weight: 550; }
.stat-hint { font-size: 11px; margin-top: 5px; overflow-wrap: anywhere; }
.block { padding: 22px; margin-bottom: 20px; }
.section-heading { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.section-heading > span { font-size: 12px; }
.search-row { display: flex; align-items: center; gap: 12px; padding: 6px 7px 6px 14px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--panel); color: var(--muted); }
.search-row:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.search-row input { flex: 1; min-width: 0; border: 0; box-shadow: none; background: transparent; padding-left: 0; }
.search-row input:focus { box-shadow: none; }
.results { margin-top: 20px; display: flex; flex-direction: column; }
.result-item { padding: 16px 0; border-top: 1px solid var(--border); }
.result-head { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; font-size: 13px; }
.result-text { margin: 10px 0 0; font-size: 13px; line-height: 1.75; color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }
.documents-panel { padding-top: 0; }
.list-head { display: flex; align-items: center; margin: 0 -22px 8px; padding: 0 22px; border-bottom: 1px solid var(--border); }
.tabs { display: flex; gap: 24px; }
.chip { display: inline-flex; align-items: center; gap: 8px; border: 0; border-bottom: 2px solid transparent; background: transparent; border-radius: 0; padding: 17px 0 15px; font-size: 13px; color: var(--muted); cursor: pointer; }
.chip > span { background: var(--panel-soft); color: var(--muted); font-size: 11px; padding: 1px 6px; border-radius: 4px; }
.chip:hover { color: var(--text); }
.chip.active { border-bottom-color: var(--accent); color: var(--accent); font-weight: 600; }
.chip.active > span { background: var(--accent-soft); color: var(--accent); }
.hint { font-size: 12px; margin: 16px 0; line-height: 1.7; padding: 10px 12px; background: var(--panel-soft); border-radius: 6px; }
.items { display: flex; flex-direction: column; }
.item { border-bottom: 1px solid var(--border); }
.item:last-child { border-bottom: none; }
.item-head { width: 100%; display: flex; align-items: center; gap: 14px; padding: 17px 2px; cursor: pointer; border: 0; border-radius: 0; background: transparent; text-align: left; font-weight: 400; }
.item-head:hover { background: var(--panel-soft); }
.document-icon { display: flex; color: var(--accent); background: var(--accent-soft); padding: 9px; border-radius: 8px; flex: none; }
.document-title { display: grid; gap: 7px; flex: 1; min-width: 0; }
.document-title > strong { font-size: 13px; overflow-wrap: anywhere; }
.document-meta { display: flex; flex-wrap: wrap; gap: 12px; font-size: 11px; color: var(--muted); }
.document-meta .name { font-size: 11px; overflow-wrap: anywhere; }
.document-tags { display: flex; flex-wrap: wrap; gap: 5px; max-width: 32%; }
.chev { color: var(--muted); flex: none; transition: transform .15s; }
.chev.expanded { transform: rotate(180deg); }
.detail { padding: 0 0 18px 50px; }
.content { margin: 0; padding: 16px; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 7px; font-size: 12px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto; }
.detail-actions { display: flex; gap: 8px; margin-top: 12px; justify-content: flex-end; flex-wrap: wrap; }
.badge { font-size: 10px; padding: 3px 7px; border-radius: 5px; border: 1px solid var(--border); white-space: nowrap; }
.s-ok { background: #edf9f3; color: #187451; border-color: #d1eedd; }
.s-staging { background: #fff8e8; color: #9a6a16; border-color: #f1dfb5; }
.tag { font-size: 10px; padding: 3px 7px; border-radius: 5px; background: var(--panel-soft); color: var(--muted); max-width: 100%; overflow-wrap: anywhere; }
button.small { font-size: 12px; padding: 5px 10px; }
.modal-mask { position: fixed; inset: 0; background: var(--overlay); display: grid; place-items: center; z-index: 50; padding: 16px; max-width: none; }
.modal { width: min(100%, 480px); padding: 26px; max-height: 90vh; overflow-y: auto; }
.modal.wide { width: min(100%, 760px); }
.modal h3 { margin: 0 0 22px; font-size: 19px; }
.modal p { line-height: 1.7; }
.modal > input { width: 100%; }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--border); }
.form-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
.form-grid label { font-size: 12px; color: var(--text); margin-top: 10px; }
.form-grid textarea { font-family: ui-monospace, Consolas, monospace; font-size: 12px; resize: vertical; }
@media (max-width: 1050px) { .stat { padding: 0 16px; } .stat-hint { line-height: 1.5; } }
@media (max-width: 680px) {
  .page { padding: 18px 14px; }
  .block { padding: 16px; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 20px; }
  .stat:nth-child(3) { border-left: 0; }
  .stat-value { font-size: 23px; }
  .documents-panel { padding-top: 0; }
  .list-head { margin: 0 -16px 8px; padding: 0 16px; }
  .section-heading > span { display: none; }
  .document-tags { display: none; }
  .document-meta { gap: 5px; }
  .item-head { gap: 10px; }
  .document-icon { padding: 7px; }
  .detail { padding-left: 0; }
  .modal { padding: 20px; }
  .search-row { gap: 8px; padding-left: 10px; }
}
</style>
