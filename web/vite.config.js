import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'node:fs'

// 开发期把 /api 代理到本地后端，生产由 nginx 反代（见根目录 nginx.conf）
// 目标可用 CODE_AGENT_API_TARGET 覆盖：联调/冒烟时后端常常不在 8765，
// 否则只能临时改这个文件（或再写一份配置），容易忘了改回来。
const API_TARGET = process.env.CODE_AGENT_API_TARGET || 'http://127.0.0.1:8765'
// Pre-bundle the same lightweight Monaco entries used by the workbench. Keep
// this derived from its static imports so adding an editor feature cannot cause
// Vite to discover a dependency and reload an unsaved workspace on first use.
const monacoEntries = [...readFileSync(new URL('./src/lib/monacoRuntime.js', import.meta.url), 'utf8')
  .matchAll(/(?:import|from)\s+['"](monaco-editor\/[^'"]+)['"]/g)].map(match => match[1])

export default defineConfig({
  plugins: [vue()],
  // monaco 是运行期按需加载的大依赖：先在 dev 期预构建，避免"第一次打开文件时
  // Vite 现场重新预构建 → 整页刷新"（那会打断用户当前的选择与编辑状态）
  optimizeDeps: {
    include: [...new Set([...monacoEntries, 'monaco-editor/languages/definitions/html/html'])],
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        ws: true, // WebSocket 升级
      },
      '/healthz': API_TARGET,
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 4096,
    rollupOptions: {
      output: {
        // Monaco 单独成 chunk：代码页是异步加载的，且这个 chunk 体积大、
        // 变更少——独立出来才能被浏览器长期缓存，而不是每次改业务代码都失效
        manualChunks(id) {
          if (id.includes('monaco-editor')) return 'monaco'
          return undefined
        },
      },
    },
  },
})
