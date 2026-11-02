#!/usr/bin/env node
import { createInterface } from 'node:readline'
import { createDesktopService, parseDesktopArgs } from '../npm/desktop-launcher.mjs'

try {
  const options = parseDesktopArgs(process.argv.slice(2))
  const service = await createDesktopService({ ...options, onOpenWindow: url => {
    process.stdout.write(JSON.stringify({ event: 'open', url }) + '\n')
  } })
  const input = createInterface({ input: process.stdin, terminal: false })
  input.on('line', line => { if (line === 'shutdown') service.close().catch(() => {}) })
  input.on('close', () => { service.close().catch(() => {}) })
  await service.closed
  input.close()
} catch (error) {
  console.error(`Code-agent 桌面启动失败：${error.message}`)
  process.exitCode = 1
}
