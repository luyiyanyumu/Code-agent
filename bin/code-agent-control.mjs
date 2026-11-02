#!/usr/bin/env node
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { LauncherServices } from '../npm/launcher-service.mjs'
import { detectMode } from '../npm/launcher-update.mjs'

try {
  const [action = 'stop', target = 'all'] = process.argv.slice(2)
  if (!['start', 'stop', 'restart'].includes(action) || !['all', 'backend', 'frontend'].includes(target)) throw new Error('用法：code-agent-control.mjs start|stop|restart all|backend|frontend')
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
  const services = new LauncherServices({ root, mode: await detectMode(root), log: console.log })
  await services.initialize()
  await services.action(action, target)
  console.log('服务操作已完成。')
} catch (error) { console.error(error.message); process.exitCode = 1 }
