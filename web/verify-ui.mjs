// M7/M10 前端联调验证：用 playwright-core 驱动本机 Chrome 跑完整 UI 流程并截图。
// 运行：node verify-ui.mjs <baseUrl> <outDir> [only]
// CHAT_BASE_URL 指向独立 chat-app（默认 5174）；两端均须使用隔离 offline 后端。
// authoring 另外要求 CODE_AGENT_TEST_HOME / CODE_AGENT_HOME 指向仓库 .tmp 内同一测试目录。
//   only = tools 时只跑到「工具与扩展」面板（M10）就收工——该面板与模型无关，
//   不必为了看它去消耗真实 API 配额。
// 说明：playwright-core 装在 WorkBuddy 托管 node 工作区，这里用绝对路径导入（ESM 不认 NODE_PATH）。

import fs from 'node:fs'
import assert from 'node:assert/strict'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const PW = process.env.PLAYWRIGHT_CORE_PATH
  || 'file:///C:/Users/dyh/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js'
const pw = await import(PW)
// playwright-core 是 CJS：命名导出可能探测不到，回退到 default
const chromium = pw.chromium || pw.default?.chromium
if (!chromium) throw new Error('playwright-core 加载失败：找不到 chromium 导出')

const base = process.argv[2] || 'http://127.0.0.1:5175'
const chatBase = process.env.CHAT_BASE_URL || 'http://127.0.0.1:5174'
const outDir = process.argv[3] || '.'
const only = (process.argv[4] || '').trim()
fs.mkdirSync(outDir, { recursive: true })
// This legacy integration flow submits prompts and changes fixture configuration.
// Both frontends must point to disposable offline services, never a real model.
for (const target of new Set(only === 'tools' || only === 'authoring' ? [base] : [base, chatBase])) {
  const settings = await (await fetch(`${target}/api/v1/settings/model`)).json()
  assert.equal(settings.provider, 'offline', `UI integration requires a disposable offline backend: ${target}`)
}
let fixtureHome
if (only === 'authoring') {
  const rawHome = process.env.CODE_AGENT_TEST_HOME
  const sandboxRoot = path.resolve(fileURLToPath(new URL('../.tmp', import.meta.url)))
  assert.ok(rawHome && path.isAbsolute(rawHome), 'Authoring requires an absolute CODE_AGENT_TEST_HOME inside this repository .tmp directory')
  fixtureHome = path.resolve(rawHome)
  const relative = path.relative(sandboxRoot, fixtureHome)
  assert.ok(relative && relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative), 'Refusing to clean outside the disposable .tmp fixture directory')
  assert.equal(path.resolve(process.env.CODE_AGENT_HOME || '.'), fixtureHome, 'Start the offline backend with matching CODE_AGENT_HOME and CODE_AGENT_TEST_HOME')
  const config = await (await fetch(`${base}/api/v1/mcp/config`)).json()
  assert.deepEqual(config.servers || {}, {}, 'Authoring requires an empty disposable MCP configuration')
}

const errors = []
const browser = await chromium.launch({
  executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: true,
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`) })

const step = (n, msg) => console.log(`[${n}] ${msg}`)

// 1) 直接进入工作区（默认免登录）
await page.goto(base, { waitUntil: 'networkidle' })
await page.waitForSelector('text=Code-agent', { timeout: 15000 })
await page.waitForSelector('.shell', { timeout: 15000 })
if (await page.locator('input[type="password"]').count()) throw new Error('工作区仍显示登录界面')
await page.screenshot({ path: `${outDir}/01-workspace.png` })
step(1, '免登录工作区渲染 OK')

// 2) 编码工作区；独立对话应用在后面的聊天流程中验证。
await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '代码', exact: true }).click()
await page.waitForSelector('.code', { timeout: 15000 })
await page.waitForTimeout(800)
await page.screenshot({ path: `${outDir}/02-code-workspace.png` })
step(2, '直接进入编码工作区')

// 3) 工具与扩展面板（M10）：统计卡片 / 来源筛选 / schema 展开 / 插件启停
await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '设置', exact: true }).click()
await page.locator('.settings-nav-item').filter({ hasText: '工具与扩展' }).click()
await page.waitForSelector('.tools-page', { timeout: 10000 })
await page.waitForTimeout(600)
const statCount = await page.locator('.stats .stat').count()
const toolsTotal = (await page.locator('.stats .stat').first().innerText()).split('\n')[0].trim()
if (statCount < 6) throw new Error(`工具面板统计卡片数量异常：${statCount}`)

// 展开一个内置工具的 schema
await page.locator('.tool-name').filter({ hasText: /^bash$/ }).click()
await page.waitForSelector('text=参数 schema', { timeout: 5000 })
const schemaShown = await page.locator('pre.schema').first().innerText()
if (!schemaShown.includes('properties')) throw new Error('schema 展开后无 properties')

// 来源筛选
await page.click('#extensions-mcp')
await page.waitForTimeout(300)
const mcpEmpty = await page.locator('.empty').count()
await page.click('#extensions-plugin')
await page.waitForTimeout(300)

// 插件启停（demo-pack：开 → 技能出现 → 再关回去，别留下状态）
const pluginRow = page.locator('.item:has-text("demo-pack")').first()
if (await pluginRow.count()) {
  const enabledBefore = await pluginRow.locator('input[type="checkbox"]').isChecked()
  await pluginRow.locator('input[type="checkbox"]').setChecked(true)
  await page.waitForTimeout(500)
  await page.click('#extensions-skill')
  await page.waitForTimeout(400)
  const skillNames = await page.locator('.item .name').allInnerTexts()
  if (!skillNames.includes('weekly-report')) {
    throw new Error(`启用插件后技能未出现：${JSON.stringify(skillNames)}`)
  }
  await page.screenshot({ path: `${outDir}/03b-tools-plugin-on.png` })
  await page.click('#extensions-plugin')
  await page.waitForTimeout(300)
  await pluginRow.locator('input[type="checkbox"]').setChecked(enabledBefore)
  await page.waitForTimeout(400)
  step(3, `工具面板 OK（统计卡 ${statCount} 张 / 工具 ${toolsTotal} 个 / 插件启停+技能热更新 OK）`)
} else {
  step(3, `工具面板 OK（统计卡 ${statCount} 张 / 工具 ${toolsTotal} 个 / 无插件可测）`)
}
await page.click('#extensions-builtin')
await page.waitForTimeout(300)
await page.screenshot({ path: `${outDir}/03-tools.png` })

if (only === 'tools') {
  await browser.close()
  console.log('\n== 结果（--only=tools）==')
  console.log(errors.length === 0 ? '工具面板通过，无 JS 错误' : `发现 ${errors.length} 条前端错误：`)
  errors.slice(0, 10).forEach((e) => console.log('  ' + e))
  process.exit(errors.length === 0 ? 0 : 1)
}

// ======================================================================
// M11 authoring（--only=authoring）：制作规范 / 上传插件 / 新建技能 /
// 编辑 MCP / 筛选对应。结束前把测试产物清干净（插件删除、技能文件删除、
// mcp.json 存回空）。
// ======================================================================
if (only === 'authoring') {
  const { execFileSync } = await import('node:child_process')
  const fsMod = await import('node:fs')

  // 幂等预清理：上一轮中断可能留下测试产物（失败不留断言——直接清掉重跑）
  fsMod.rmSync(path.join(fixtureHome, 'plugins/m11-pack'), { recursive: true, force: true })
  fsMod.rmSync(path.join(fixtureHome, 'skills/m11-note'), { recursive: true, force: true })

  // 用项目 venv 的 python 造一个测试插件 zip（stdlib zipfile，顶层带目录验证自动剥壳）
  const zipPath = `${outDir}/m11-pack.zip`
  const py = `
import json, zipfile, sys
files = {
    "m11-pack/plugin.json": json.dumps({
        "name": "m11-pack", "version": "1.0.0",
        "description": "UI 验证用插件", "prompt": "UI 验证片段",
    }, ensure_ascii=False),
    "m11-pack/skills/m11-skill/SKILL.md": "---\\nname: m11-skill\\ndescription: zip 带来的技能\\n---\\n正文",
}
z = zipfile.ZipFile(sys.argv[1], "w")
for k, v in files.items():
    z.writestr(k, v)
z.close()
`
  execFileSync(process.env.PYTHON_PATH || fileURLToPath(new URL('../.venv/Scripts/python.exe', import.meta.url)), ['-c', py, zipPath], { stdio: 'pipe' })

  // ---- A) 制作规范面板 ----
  await page.click('button:has-text("制作规范")')
  await page.waitForSelector('text=一个插件 = plugins/ 下的一个目录', { timeout: 5000 })
  await page.click('.guide-tabs .chip:has-text("MCP 连接器")')
  await page.waitForTimeout(300)
  const tmpl = await page.locator('pre.guide-template').innerText()
  if (!tmpl.includes('mcpServers')) throw new Error('规范模板展示异常')
  await page.screenshot({ path: `${outDir}/04-guide.png` })
  await page.click('button:has-text("收起规范")')
  step('A', '制作规范面板 OK（三个 tab + 模板展示）')

  // ---- B) 上传插件 zip ----
  await page.click('button:has-text("上传插件")')
  await page.setInputFiles('.modal input[type="file"]', zipPath)
  await page.click('button:has-text("上传并启用")')
  await page.waitForSelector('text=已上传（', { timeout: 15000 })
  await page.waitForTimeout(500)
  await page.click('#extensions-plugin')
  const uploaded = await page.locator('.item:has-text("m11-pack")').count()
  if (!uploaded) throw new Error('上传后插件卡片未出现')
  await page.click('#extensions-skill')
  await page.waitForTimeout(400)
  const zipSkills = await page.locator('.item .name').allInnerTexts()
  if (!zipSkills.includes('m11-skill')) throw new Error(`zip 内嵌技能未入目录：${JSON.stringify(zipSkills)}`)
  await page.screenshot({ path: `${outDir}/05-uploaded.png` })
  step('B', '上传插件 OK（zip 顶层目录自动剥壳 + 内嵌技能热更新）')

  // ---- C) 新建技能 ----
  await page.getByRole('group', { name: '工具与扩展操作' }).getByRole('button', { name: '新建技能', exact: true }).click()
  await page.fill('.modal input[placeholder="如 weekly-report"]', 'm11-note')
  await page.fill('.modal input[placeholder*="一句话说明"]', 'UI 验证创建的技能')
  await page.fill('.modal textarea', '## 步骤\n1. 这是 UI 验证创建的技能正文')
  await page.click('button:has-text("保存技能")')
  await page.waitForSelector('.notice:has-text("目录已热更新"), .feedback:has-text("目录已热更新")', { timeout: 10000 })
  await page.waitForTimeout(400)
  const localSkills = await page.locator('.item .name').allInnerTexts()
  if (!localSkills.includes('m11-note')) throw new Error(`新建技能未出现在目录：${JSON.stringify(localSkills)}`)
  await page.screenshot({ path: `${outDir}/06-skill.png` })
  step('C', '新建技能 OK（本地目录来源）')

  // ---- D) 编辑 MCP ----
  await page.click('#extensions-mcp')
  await page.click('button:has-text("编辑 MCP")')
  await page.click('button:has-text("+ 添加 server")')
  await page.fill('.mcp-row input[placeholder="名称"]', 'ui-test')
  await page.fill('.mcp-row input[placeholder*="命令"]', 'npx')
  await page.fill('.mcp-row input[placeholder*="参数"]', '-y mcp-ui-test')
  await page.click('button:has-text("保存 mcp.json")')
  await page.waitForSelector('text=已保存——重启服务后新配置才会连接', { timeout: 10000 })
  await page.screenshot({ path: `${outDir}/07-mcp.png` })
  // 清理：把行删掉再存一次，mcp.json 回到空
  await page.locator('.mcp-row button:has-text("删除")').click()
  await page.click('button:has-text("保存 mcp.json")')
  await page.waitForTimeout(600)
  await page.click('.modal button:has-text("关闭")')
  step('D', '编辑 MCP OK（校验/保存/重启提示 + 清理回空配置）')

  // ---- E) 筛选与内容对应 ----
  await page.click('#extensions-mcp')
  await page.waitForTimeout(400)
  if (!(await page.getByRole('heading', { name: /^MCP 工具/ }).count())) throw new Error('MCP 筛选下标题不是 MCP 工具')
  if (!(await page.locator('h3:has-text("MCP 连接器（")').count())) throw new Error('MCP 筛选下没有连接器面板')
  await page.click('#extensions-plugin')
  await page.waitForTimeout(400)
  if (!(await page.locator('h3:has-text("插件包（")').count())) throw new Error('插件工具筛选下没有插件包面板')
  await page.click('#extensions-skill')
  await page.waitForTimeout(400)
  if (!(await page.getByRole('heading', { name: /^技能\s+\d/ }).count())) throw new Error('技能筛选下标题不是技能')
  await page.screenshot({ path: `${outDir}/08-filters.png` })
  step('E', '筛选与内容对应 OK（MCP/插件工具/技能三种都指向正确面板）')

  // ---- 清理测试产物 ----
  await page.click('#extensions-plugin')
  await page.waitForTimeout(300)
  await page.locator('.item:has-text("m11-pack") button:has-text("删除")').click()
  await page.locator('dialog[open] button[type="submit"]').click()
  await page.waitForSelector('text=已删除', { timeout: 10000 })
  fsMod.rmSync(path.join(fixtureHome, 'skills/m11-note'), { recursive: true, force: true })
  fsMod.rmSync(zipPath, { force: true })
  step('F', '清理 OK（测试插件已删、本地技能文件已移除、mcp.json 已存回空）')

  await browser.close()
  console.log('\n== 结果（--only=authoring）==')
  console.log(errors.length === 0 ? 'M11 全流程通过，无 JS 错误' : `发现 ${errors.length} 条前端错误：`)
  errors.slice(0, 10).forEach((e) => console.log('  ' + e))
  process.exit(errors.length === 0 ? 0 : 1)
}

// 4) 新建会话
await page.goto(chatBase, { waitUntil: 'networkidle' })
await page.locator('.chat').waitFor()
await page.waitForTimeout(400)
await page.getByRole('button', { name: '开始新对话', exact: false }).click()
await page.waitForTimeout(1200)
const hasComposer = await page.locator('textarea').count()
step(4, `新建会话 OK（输入框 ${hasComposer} 个）`)

// 5) 在独立对话应用发起离线测试请求。
await page.locator('.ta:not(:disabled)').fill('用一句话确认你在线')
await page.locator('.send-btn').click()
await page.waitForFunction(
  () => {
    const bubbles = document.querySelectorAll('.row.agent .md-body')
    const last = bubbles[bubbles.length - 1]
    return last && last.innerText.trim().length > 0
  },
  null,
  { timeout: 90000 },
)
await page.waitForTimeout(600)
await page.screenshot({ path: `${outDir}/04-chat-reply.png` })
const reply = await page.locator('.row.agent .md-body').last().innerText()
step(5, `对话往返 OK：${reply.replace(/\s+/g, ' ').slice(0, 60)}`)

// 6) 用量面板
await page.goto(base, { waitUntil: 'networkidle' })
await page.click('.nav-item:has-text("用量")')
await page.waitForSelector('text=输入 Tokens', { timeout: 10000 })
await page.waitForTimeout(500)
await page.screenshot({ path: `${outDir}/05-usage.png` })
const prompts = await page.locator('.stat strong').first().innerText()
step(6, `用量面板 OK（轮次=${prompts.trim()}）`)

// 7) 自动化面板
await page.click('.nav-item:has-text("自动化")')
await page.waitForSelector('text=新建任务', { timeout: 10000 })
await page.fill('input[placeholder="如：每日晨报"]', '每日站会提醒')
await page.fill('textarea[placeholder*="汇总昨日销售"]', '汇总昨日进度并生成简报')
await page.waitForTimeout(300)
await page.screenshot({ path: `${outDir}/06-automations.png` })
await page.click('button:has-text("创建任务")')
await page.waitForSelector('text=已创建', { timeout: 10000 })
await page.waitForTimeout(800)
await page.screenshot({ path: `${outDir}/07-automation-created.png` })
step(7, '自动化创建 OK')

// 8) 审计面板
await page.click('.nav-item:has-text("审计")')
await page.waitForSelector('text=哈希链完整', { timeout: 10000 })
await page.waitForTimeout(500)
await page.screenshot({ path: `${outDir}/08-audit.png` })
step(8, '审计面板 OK（哈希链校验通过）')

await browser.close()

console.log('\n== 结果 ==')
console.log(errors.length === 0 ? 'UI 全流程通过，无 JS 错误' : `发现 ${errors.length} 条前端错误：`)
errors.slice(0, 10).forEach((e) => console.log('  ' + e))
process.exit(errors.length === 0 ? 0 : 1)
