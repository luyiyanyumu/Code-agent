import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const target = process.env.CHAT_AGENT_API_TARGET || 'http://127.0.0.1:8766'
const proxy = {
  '/api': { target, changeOrigin: true, ws: true },
  '/healthz': { target, changeOrigin: true },
}

export default defineConfig({
  plugins: [vue()],
  server: { host: '127.0.0.1', port: 5174, strictPort: true, proxy },
  preview: { host: '127.0.0.1', port: 4174, strictPort: true, proxy },
  build: { outDir: 'dist' },
})
