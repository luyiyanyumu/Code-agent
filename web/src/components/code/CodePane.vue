<script setup>
// 文件查看/编辑位（P0：只读 + 行号 + 行区间高亮）。
//
// P2 这里会换成 Monaco（含 LSP、diff、保存乐观锁）。**刻意先做只读版本**：
// 编辑器的引入牵扯 worker 打包、URI 对齐、大文件守卫，先把"能看、能定位、
// 能把选区当上下文提给 AI"这条链路跑通，再换内核，避免同时引入两个变量。
import { computed, ref } from 'vue'

const props = defineProps({
  file: { type: Object, default: null },   // { path, text, lang, ... }
  highlight: { type: Object, default: null }, // { start, end }
})

const wrapEl = ref(null)

const lines = computed(() => {
  if (!props.file) return []
  const raw = props.file.text ?? ''
  return raw.split('\n')
})

function inHighlight(idx) {
  const h = props.highlight
  if (!h || !h.start) return false
  const line = (props.file?.start_line || 1) + idx
  return line >= h.start && line <= (h.end || h.start)
}

const gutterWidth = computed(() => String(props.file?.total_lines || lines.value.length || 1).length)

function copyPath() {
  if (!props.file) return
  const text = props.file.path
  if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {})
}
</script>

<template>
  <div class="pane">
    <div v-if="!file" class="empty muted">
      <div>从左侧选择一个文件查看内容。</div>
      <div class="empty-sub">项目地图（结构 / 依赖 / 实时改动）将在 P1 接入此处。</div>
    </div>

    <template v-else>
      <div class="bar">
        <span class="path mono" :title="file.path">{{ file.path }}</span>
        <span class="chip">{{ file.lang }}</span>
        <span v-if="file.secret" class="chip warn" title="疑似含密钥文件">疑似密钥</span>
        <span class="spacer" />
        <span class="muted meta">
          {{ file.total_lines ?? lines.length }} 行 · {{ Math.max(1, Math.round((file.size || 0) / 1024)) }}KB
          <template v-if="file.truncated"> · 已截断</template>
        </span>
        <button class="ghost" title="复制路径" @click="copyPath">⧉</button>
      </div>

      <div ref="wrapEl" class="code">
        <div v-for="(line, idx) in lines" :key="idx" class="line" :class="{ on: inHighlight(idx) }">
          <span class="gutter" :style="{ minWidth: `${gutterWidth}ch` }">
            {{ (file.start_line || 1) + idx }}
          </span>
          <span class="text mono">{{ line || ' ' }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-height: 0; height: 100%; }
.empty {
  display: flex; flex-direction: column; gap: var(--sp-1-5); align-items: center; justify-content: center;
  height: 100%; font-size: var(--fs-sm);
}
.empty-sub { font-size: var(--fs-xs); color: var(--muted); }

.bar {
  display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1-5) var(--sp-2-5);
  border-bottom: 1px solid var(--border); font-size: var(--fs-sm);
}
.path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spacer { flex: 1; }
.chip {
  flex: none; font-size: var(--fs-xs); padding: var(--sp-hair) var(--sp-1-5); border-radius: var(--r-sm);
  background: var(--hover); color: var(--muted);
}
.chip.warn { background: #fef3c7; color: var(--warn); }
.meta { flex: none; font-size: var(--fs-xs); }
.ghost { border: none; background: transparent; padding: var(--sp-0-5) var(--sp-1-5); color: var(--muted); flex: none; }
.ghost:hover { background: var(--hover); }

.code { overflow: auto; flex: 1; padding: var(--sp-1-5) 0 var(--sp-6); background: var(--panel); }
.line { display: flex; font-size: var(--fs-sm); line-height: var(--lh-normal); white-space: pre; }
.line.on { background: var(--accent-soft); }
.gutter {
  flex: none; text-align: right; padding: 0 var(--sp-2-5) 0 var(--sp-3); color: var(--muted);
  user-select: none; border-right: 1px solid #f1f3f5; margin-right: var(--sp-2-5);
}
.text { padding-right: var(--sp-6); }
</style>
