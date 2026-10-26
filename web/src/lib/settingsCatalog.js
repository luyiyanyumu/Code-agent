export const SETTINGS_SECTIONS = [
  { id: 'general', label: '通用', icon: 'settings', description: '让工作区适应你的日常使用习惯。', scope: '此浏览器 · 自动保存' },
  { id: 'appearance', label: '外观', icon: 'layers', description: '选择舒适的主题，让界面与编辑器保持一致。', scope: '此浏览器 · 立即生效' },
  { id: 'editor', label: '编辑器', icon: 'code', description: '调整代码的显示方式与缩进偏好。', scope: '此浏览器 · 立即生效' },
  { id: 'models', label: '模型与连接', icon: 'server', description: '管理模型服务、连接档案与上下文容量。', scope: '服务端 · 保存后生效' },
  { id: 'agents', label: '智能体协作', icon: 'git-branch', description: '控制团队规模、任务拆分层级和执行预算。', scope: '服务端 · 所有项目' },
  { id: 'permissions', label: '执行与权限', icon: 'shield', description: '设置新会话的执行方式，了解工具与角色的权限边界。', scope: '服务端 · 新会话默认值' },
  { id: 'extensions', label: '工具与扩展', icon: 'tools', description: '管理工具能力、MCP 服务器、插件、技能与专家。', scope: '服务端' },
  { id: 'memory', label: '记忆', icon: 'memory', description: '管理个人偏好与会话记忆，为代码助手补充工作背景。', scope: '资源 · 服务端' },
  { id: 'knowledge', label: '知识库', icon: 'knowledge', description: '搜索、导入与整理知识文档，审核模型整理的参考内容。', scope: '资源 · 服务端' },
  { id: 'usage', label: '用量', icon: 'usage', description: '查看请求次数与模型消耗，了解代码助手的使用情况。', scope: '管理 · 服务端' },
  { id: 'automations', label: '自动化', icon: 'automations', description: '安排一次性或周期任务，管理任务计划与运行状态。', scope: '管理 · 服务端' },
  { id: 'audit', label: '审计', icon: 'audit', description: '查看工具执行记录，校验操作历史的完整性。', scope: '管理 · 仅管理员' },
  { id: 'shortcuts', label: '快捷键', icon: 'terminal', description: '快速查阅工作区中可用的键盘操作。', scope: '快捷键参考' },
  { id: 'system', label: '关于与诊断', icon: 'server', description: '查看服务连接状态和设置的存储范围。', scope: '当前服务' },
]

// Search metadata contains labels only: never index connection values or credentials.
export const SETTINGS_ITEMS = [
  ['general', 'default-view', '起始页面', '启动 代码 工作区 首页 设置 资源 管理'],
  ['general', 'send-key', '发送消息的按键', '代码助手 Enter Ctrl Command 回车 换行 输入'],
  ['appearance', 'theme', '主题', '浅色 深色 跟随系统 light dark system'],
  ['appearance', 'reduced-motion', '减少动态效果', '动画 过渡 无障碍'],
  ['editor', 'editor-font-size', '代码字号', '字体 大小 font size'],
  ['editor', 'editor-tab-size', '缩进宽度', '空格 Tab 缩进'],
  ['editor', 'editor-word-wrap', '自动换行', '长行 word wrap'],
  ['editor', 'editor-minimap', '代码缩略图', '小地图 minimap'],
  ['editor', 'editor-line-numbers', '行号', '相对 显示 隐藏'],
  ['editor', 'editor-whitespace', '空白字符', '空格 制表符 whitespace'],
  ['models', 'model-provider', '模型服务提供方', 'DeepSeek OpenAI 兼容 离线'],
  ['models', 'provider-base-url', '服务地址', 'Base URL API 连接 本地 Ollama LM Studio vLLM'],
  ['models', 'provider-api-key', 'API 密钥', 'Key token 凭据'],
  ['models', 'default-model', '默认模型', 'Model ID 获取 探测'],
  ['models', 'context-window', '上下文窗口', '容量 Token context'],
  ['models', 'max-output-tokens', '最大输出 Token', '容量 限制 output'],
  ['models', 'provider-proxy-mode', '网络代理', '系统 直连 proxy'],
  ['models', 'saved-profiles', '已保存配置', '档案 添加 编辑 启用 删除 预设'],
  ['agents', 'agent-team-depth', '最大递归深度', '团队 子智能体 层级 Lead'],
  ['agents', 'agent-team-parallel', '并行子智能体上限', '团队 并发 性能 调度'],
  ['agents', 'agent-budget-task-tokens', '单次执行 Token 上限', '预算 子任务 消耗 input output tokens 成本'],
  ['agents', 'agent-budget-task-seconds', '单次执行时间上限', '预算 子任务 超时 秒 审批 排队 等待'],
  ['agents', 'agent-budget-team-tokens', '团队总 Token 上限', '预算 总量 Lead 递归 子智能体 消耗 tokens 成本'],
  ['agents', 'agent-budget-team-seconds', '团队总时间上限', '预算 总时间 Lead 递归 超时 秒 等待'],
  ['permissions', 'execution-permission', '默认执行权限', '只读 工作区可写 完整访问 审批 申请 bash Lead'],
  ['extensions', 'extensions-builtin', '内置工具', '启用 禁用 审批 bash git read'],
  ['extensions', 'extensions-mcp', 'MCP 服务器', '连接 外部 服务'],
  ['extensions', 'extensions-plugin', '插件', '安装 启用 扩展'],
  ['extensions', 'extensions-off', '已禁用工具', '停用 恢复 启用 审批'],
  ['extensions', 'extensions-skill', '技能', '创建 上传 提示词 skill'],
  ['extensions', 'extensions-expert', '专家', '启用 删除 专业能力 expert'],
  ['memory', 'memory-settings', '个人记忆与会话记忆', '添加 删除 保存 偏好 工作背景 信息 资源'],
  ['knowledge', 'knowledge-settings', '搜索与管理知识文档', '知识库 搜索 参考 标签 正式区 待审核 资源'],
  ['knowledge', 'knowledge-settings', '导入知识', '目录 上传 文件 新建 编辑 文档 审核'],
  ['usage', 'usage-settings', '模型用量概览', '请求 对话 轮次 消耗 Tokens 输入 输出 统计 明细 管理'],
  ['automations', 'automations-settings', '自动化任务', '计划 周期 定时 一次性 每天 调度 暂停 恢复 管理'],
  ['automations', 'automations-settings', '创建自动化任务', '名称 提示词 定时 RRULE 重复 计划 时间'],
  ['audit', 'audit-settings', '执行审计日志', '工具 操作 参数 结果 成功 失败 用户 搜索 管理'],
  ['audit', 'audit-settings', '审计完整性校验', '哈希链 hash chain 记录 管理员 校验'],
  ['shortcuts', 'shortcut-list', '键盘快捷键', 'Ctrl 保存 搜索 运行 调试 快速打开'],
  ['system', 'service-status', '服务状态', '版本 连接 健康 检查 诊断'],
  ['system', 'settings-storage', '设置存储与生效范围', '本地 浏览器 服务端 隐私 API Key'],
].map(([section, anchor, label, keywords]) => ({ section, anchor, label, keywords }))

export function searchSettings(query) {
  const terms = String(query || '').trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return []
  return SETTINGS_ITEMS.filter(item => {
    const section = SETTINGS_SECTIONS.find(s => s.id === item.section)
    const text = `${section.label} ${item.label} ${item.keywords}`.toLocaleLowerCase()
    return terms.every(term => text.includes(term))
  })
}

export const WORKSPACE_SHORTCUTS = [
  { label: '打开设置', keys: 'Ctrl + ,', scope: '全局' },
  { label: '快速打开文件', keys: 'Ctrl + P', scope: '代码工作区' },
  { label: '搜索项目内容', keys: 'Ctrl + Shift + F', scope: '代码工作区' },
  { label: '打开项目文件夹', keys: 'Ctrl + Shift + O', scope: '代码工作区' },
  { label: '显示 / 隐藏文件侧栏', keys: 'Ctrl + B', scope: '代码工作区' },
  { label: '保存当前文件', keys: 'Ctrl + S', scope: '编辑器' },
  { label: '导航后退 / 前进', keys: 'Alt + ← / →', scope: '代码工作区' },
  { label: '运行当前文件', keys: 'Ctrl + F5', scope: '代码工作区' },
  { label: '开始 / 继续调试', keys: 'F5', scope: '代码工作区' },
  { label: '切换断点', keys: 'F9', scope: '编辑器' },
]
