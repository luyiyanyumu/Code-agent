#!/usr/bin/env node
import { launchControlPanel } from '../npm/launcher.mjs'
import { launchDesktopWindow } from '../npm/desktop-launcher.mjs'

try {
  const [major, minor] = process.versions.node.split('.').map(Number)
  if (major < 22 || (major === 22 && minor < 14)) throw new Error('需要 Node.js 22.14 或更新版本。')
  const options = {}
  let web = false
  for (let index = 2; index < process.argv.length; index++) {
    const argument = process.argv[index]
    if (argument === '--no-open') options.open = false
    else if (argument === '--web') web = true
    else if (['--root', '--home', '--launcher-port'].includes(argument)) {
      const value = process.argv[++index]
      if (!value || value.startsWith('--')) throw new Error(`${argument} 缺少参数。`)
      options[argument === '--launcher-port' ? 'launcherPort' : argument.slice(2)] = value
    } else throw new Error(`未知参数：${argument}`)
  }
  if (options.launcherPort) {
    if (!/^\d+$/.test(options.launcherPort) || Number(options.launcherPort) < 1 || Number(options.launcherPort) > 65535) throw new Error('启动器端口必须在 1 到 65535 之间。')
    options.launcherPort = Number(options.launcherPort)
  }
  if (process.platform === 'win32' && options.open !== false && !web) {
    const result = await launchDesktopWindow(options)
    console.log(`Code-agent 桌面启动器已打开：${result.executable}`)
  } else {
    const result = await launchControlPanel(options)
    console.log(`Code-agent 启动器：${result.url}`)
  }
} catch (error) {
  console.error(`Code-agent 启动器：${error.message}`)
  process.exitCode = 1
}
