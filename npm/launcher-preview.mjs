/** Dependency-free frontend host for the prebuilt npm distribution. */
import http from 'node:http'
import net from 'node:net'
import { readFile, stat } from 'node:fs/promises'
import { dirname, extname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'web', 'dist')
const args = process.argv.slice(2)
const port = Number(args[args.indexOf('--port') + 1] || 5175)
const backendPort = Number(args[args.indexOf('--backend-port') + 1] || 8765)
if (![port, backendPort].every(value => Number.isInteger(value) && value > 0 && value < 65536)) throw new Error('无效端口')
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' }
const server = http.createServer(async (request, response) => {
  if (request.url?.split('?')[0] === '/__code_agent_health') {
    response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
    response.end(JSON.stringify({ ok: true, service: 'code-agent-frontend' }))
    return
  }
  if (/^\/(api(?:\/|$)|healthz(?:\?|$))/.test(request.url)) {
    let upstreamResponse, clientDisconnected = false
    const proxy = http.request({ hostname: '127.0.0.1', port: backendPort, path: request.url, method: request.method, headers: request.headers }, upstream => {
      upstreamResponse = upstream
      if (clientDisconnected || response.destroyed) { upstream.destroy(); return }
      upstream.on('error', () => { if (!response.destroyed) response.destroy() })
      response.writeHead(upstream.statusCode, upstream.headers); upstream.pipe(response)
    })
    const disconnect = () => {
      if (response.writableFinished) return
      clientDisconnected = true
      upstreamResponse?.destroy()
      proxy.destroy()
    }
    // A completed POST body is not a disconnect. Observe the response side so
    // closing the WebView also cancels long-running picker requests upstream.
    request.on('aborted', disconnect)
    response.on('close', disconnect)
    proxy.on('error', () => {
      if (clientDisconnected || response.destroyed) return
      if (!response.headersSent) response.writeHead(502)
      response.end('后端尚未就绪')
    })
    request.pipe(proxy)
    return
  }
  try {
    const pathname = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname)
    let file = resolve(root, '.' + pathname)
    if (file !== root && !file.startsWith(root + sep)) { response.writeHead(403); response.end(); return }
    try { if (!(await stat(file)).isFile()) file = join(root, 'index.html') } catch { file = join(root, 'index.html') }
    const data = await readFile(file)
    response.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream', 'X-Content-Type-Options': 'nosniff' })
    response.end(data)
  } catch { response.writeHead(500); response.end('前端文件不可用，请检查安装包') }
})
server.on('upgrade', (request, socket, head) => {
  if (!request.url.startsWith('/api/')) { socket.destroy(); return }
  const upstream = net.connect(backendPort, '127.0.0.1', () => {
    upstream.write(`${request.method} ${request.url} HTTP/${request.httpVersion}\r\n` + Object.entries(request.headers).map(([key, value]) => `${key}: ${value}\r\n`).join('') + '\r\n')
    if (head.length) upstream.write(head)
    upstream.pipe(socket); socket.pipe(upstream)
  })
  upstream.on('error', () => socket.destroy())
  socket.on('error', () => upstream.destroy())
  socket.on('close', () => upstream.destroy())
})
server.listen(port, '127.0.0.1', () => console.log(`Code-agent 前端：http://127.0.0.1:${port}`))
