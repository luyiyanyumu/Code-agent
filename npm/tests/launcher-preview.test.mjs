import assert from 'node:assert/strict'
import test from 'node:test'
import http from 'node:http'
import net from 'node:net'
import { createHash } from 'node:crypto'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { readFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const delay = milliseconds => new Promise(done => setTimeout(done, milliseconds))

async function freePort() {
  const reservation = net.createServer()
  await new Promise(done => reservation.listen(0, '127.0.0.1', done))
  const port = reservation.address().port
  await new Promise(done => reservation.close(done))
  return port
}

test('installed frontend preview serves prebuilt files and proxies a separate backend', { timeout: 20000 }, async t => {
  // Read the actual built distribution. Nothing in the source tree is rewritten.
  const indexHtml = await readFile(join(root, 'web/dist/index.html'), 'utf8')
  const assetPaths = [...indexHtml.matchAll(/(?:src|href)="(\/assets\/[^"?]+\.(?:js|css))"/g)].map(match => match[1])
  assert.ok(assetPaths.some(asset => asset.endsWith('.js')), 'build web/dist before running preview integration tests')
  assert.ok(assetPaths.some(asset => asset.endsWith('.css')))
  const sockets = new Set()
  let waitingStarted, waitingClosed
  const backend = http.createServer(async (request, response) => {
    let body = ''
    for await (const chunk of request) body += chunk
    response.setHeader('Content-Type', 'application/json')
    response.setHeader('X-Preview-Fixture', 'upstream')
    if (request.url === '/api/v1/picker-wait') {
      response.once('close', () => waitingClosed?.())
      waitingStarted?.()
      return
    }
    if (request.url.startsWith('/healthz')) {
      response.end(JSON.stringify({ ok: true, version: 'preview-fixture' }))
      return
    }
    response.statusCode = request.method === 'POST' ? 201 : 200
    response.end(JSON.stringify({ method: request.method, path: request.url, body, marker: request.headers['x-client-marker'] }))
  })
  backend.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)) })
  backend.on('upgrade', (request, socket, head) => {
    const accept = createHash('sha1').update(request.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64')
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`)
    const reply = chunk => {
      // Check that a masked client text frame passed through unchanged.
      if (chunk.equals(Buffer.from([0x81, 0x82, 1, 2, 3, 4, 0x71, 0x6b]))) socket.write(Buffer.from([0x81, 4, 0x70, 0x6f, 0x6e, 0x67]))
    }
    if (head.length) reply(head)
    socket.on('data', reply)
  })
  let child, childError, exited = false, diagnostics = ''
  t.after(async () => {
    if (child && !exited) {
      const finished = once(child, 'exit')
      child.kill()
      await finished
    }
    for (const socket of sockets) socket.destroy()
    backend.closeAllConnections()
    if (backend.listening) await new Promise(done => backend.close(done))
  })
  await new Promise(done => backend.listen(0, '127.0.0.1', done))
  const port = await freePort()
  const url = `http://127.0.0.1:${port}`
  child = spawn(process.execPath, [join(root, 'npm/launcher-preview.mjs'), '--port', String(port), '--backend-port', String(backend.address().port)], {
    cwd: root, windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.on('error', error => { childError = error })
  child.on('exit', () => { exited = true })
  for (const stream of [child.stdout, child.stderr]) stream.on('data', chunk => { diagnostics = (diagnostics + chunk).slice(-16000) })
  let ready = false
  for (let attempt = 0; attempt < 150; attempt++) {
    if (childError || exited) throw childError || new Error(`preview exited before readiness: ${diagnostics}`)
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(500) })
      ready = response.ok && (await response.text()) === indexHtml
      if (ready) break
    } catch {}
    await delay(25)
  }
  assert.ok(ready, `preview did not become ready: ${diagnostics}`)

  await t.test('index HTML and SPA routes use the real prebuilt entrypoint', async () => {
    for (const route of ['/', '/code/project/example?view=files']) {
      const response = await fetch(url + route)
      assert.equal(response.status, 200)
      assert.match(response.headers.get('content-type'), /^text\/html; charset=utf-8$/)
      assert.equal(response.headers.get('x-content-type-options'), 'nosniff')
      assert.equal(await response.text(), indexHtml)
    }
  })
  await t.test('JavaScript and CSS assets preserve their MIME types and bytes', async () => {
    for (const extension of ['js', 'css']) {
      const asset = assetPaths.find(value => value.endsWith(`.${extension}`))
      const response = await fetch(url + asset)
      assert.equal(response.status, 200)
      assert.match(response.headers.get('content-type'), extension === 'js' ? /^text\/javascript/ : /^text\/css/)
      assert.deepEqual(Buffer.from(await response.arrayBuffer()), await readFile(join(root, 'web/dist', asset)))
    }
  })
  await t.test('GET API requests forward query parameters and response headers', async () => {
    const response = await fetch(`${url}/api/v1/fixture?q=hello%20world&limit=2`, { headers: { 'X-Client-Marker': 'get-marker' } })
    assert.equal(response.status, 200)
    assert.equal(response.headers.get('x-preview-fixture'), 'upstream')
    assert.deepEqual(await response.json(), { method: 'GET', path: '/api/v1/fixture?q=hello%20world&limit=2', body: '', marker: 'get-marker' })
  })
  await t.test('POST API requests forward the body and upstream status', async () => {
    const body = JSON.stringify({ prompt: '中文请求', enabled: true })
    const response = await fetch(`${url}/api/v1/fixture`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Client-Marker': 'post-marker' }, body })
    assert.equal(response.status, 201)
    assert.deepEqual(await response.json(), { method: 'POST', path: '/api/v1/fixture', body, marker: 'post-marker' })
  })
  await t.test('health endpoint forwards backend health rather than SPA fallback', async () => {
    const response = await fetch(`${url}/healthz?probe=launcher`)
    assert.equal(response.status, 200)
    assert.deepEqual(await response.json(), { ok: true, version: 'preview-fixture' })
  })
  await t.test('frontend identity health is available independently of backend health', async () => {
    const response = await fetch(`${url}/__code_agent_health`)
    assert.equal(response.status, 200)
    assert.match(response.headers.get('content-type'), /^application\/json/)
    assert.deepEqual(await response.json(), { ok: true, service: 'code-agent-frontend' })
  })
  await t.test('WebSocket upgrades and frames tunnel through the frontend', async () => {
    const socket = net.connect(port, '127.0.0.1')
    try {
      const frame = await new Promise((done, reject) => {
        let received = Buffer.alloc(0), sent = false
        socket.setTimeout(3000, () => reject(new Error('WebSocket proxy timed out')))
        socket.on('error', reject)
        socket.on('data', chunk => {
          received = Buffer.concat([received, chunk])
          const split = received.indexOf('\r\n\r\n')
          if (split < 0) return
          if (!sent) {
            assert.match(received.subarray(0, split).toString(), /^HTTP\/1\.1 101 Switching Protocols/)
            sent = true
            socket.write(Buffer.from([0x81, 0x82, 1, 2, 3, 4, 0x71, 0x6b]))
          }
          if (received.length >= split + 10) done(received.subarray(split + 4))
        })
        socket.on('connect', () => socket.write('GET /api/v1/session/ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n'))
      })
      assert.deepEqual(frame, Buffer.from([0x81, 4, 0x70, 0x6f, 0x6e, 0x67]))
    } finally { socket.destroy() }
  })
  await t.test('closing a client response cancels its pending backend picker request', async () => {
    const started = new Promise(done => { waitingStarted = done })
    const closed = new Promise(done => { waitingClosed = done })
    const caller = http.request(`${url}/api/v1/picker-wait`, { method: 'POST', headers: { 'Content-Type': 'application/json' } })
    caller.on('error', () => {})
    caller.end('{}')
    try {
      await Promise.race([started, delay(3000).then(() => { throw new Error('pending request did not reach backend') })])
      // The finished request body alone must leave the long-running response alive.
      let prematurelyClosed = false
      closed.then(() => { prematurelyClosed = true })
      await delay(100)
      assert.equal(prematurelyClosed, false)
      caller.destroy()
      await Promise.race([closed, delay(3000).then(() => { throw new Error('client disconnect was not propagated upstream') })])
      assert.equal((await fetch(`${url}/__code_agent_health`)).status, 200)
    } finally { caller.destroy(); waitingStarted = null; waitingClosed = null }
  })
  await t.test('a stopped backend produces 502 while the frontend remains available', async () => {
    for (const socket of sockets) socket.destroy()
    backend.closeAllConnections()
    await new Promise(done => backend.close(done))
    for (const route of ['/healthz', '/api/v1/fixture']) {
      const response = await fetch(url + route, { signal: AbortSignal.timeout(3000) })
      assert.equal(response.status, 502)
      assert.match(await response.text(), /后端尚未就绪/)
    }
    const frontend = await fetch(url)
    assert.equal(frontend.status, 200)
    assert.equal(await frontend.text(), indexHtml)
    const frontendHealth = await fetch(`${url}/__code_agent_health`)
    assert.equal(frontendHealth.status, 200)
    assert.match(frontendHealth.headers.get('content-type'), /^application\/json/)
    assert.deepEqual(await frontendHealth.json(), { ok: true, service: 'code-agent-frontend' })
  })
})
