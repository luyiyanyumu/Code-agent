// Real Vite launcher. The first HTTP request proves backend-before-frontend ordering.
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readFile, writeFile } from 'node:fs/promises'
const root = dirname(fileURLToPath(import.meta.url))
const backend = process.env.BACKEND_URL
const health = await fetch(new URL('/health', backend))
if (!health.ok || !(await health.json()).ok) throw Error('Backend was not ready before frontend startup')
await writeFile(join(root, 'startup-order.json'), JSON.stringify({ backendReady: true, at: Date.now(), pid: process.pid }))
console.log('frontend/dev.mjs:9 backend-ready-before-frontend confirmed')
const { createServer } = await import(process.env.VITE_MODULE)
const server = await createServer({
  configFile: false, root,
  server: { host: '127.0.0.1', port: Number(process.env.PORT), strictPort: true, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{ name: 'fixture-static-document', configureServer(vite) {
    // No @vite/client/HMR websocket: production preview uses explicit refresh.
    vite.middlewares.use(async (request, response, next) => {
      if (new URL(request.url, 'http://fixture').pathname !== '/') return next()
      const html = (await readFile(join(root, 'index.html'), 'utf8')).replace('__BACKEND_JSON__', JSON.stringify(new URL(backend).origin))
      response.setHeader('Content-Type', 'text/html; charset=utf-8'); response.end(html)
    })
  } }],
})
await server.listen()
console.log(`frontend/dev.mjs:25 frontend listening port=${process.env.PORT}`)
