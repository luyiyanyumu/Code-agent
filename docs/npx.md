# npx 安装与更新

Code-agent 通过 [npm · @luyiyanyumu/code-agent](https://www.npmjs.com/package/@luyiyanyumu/code-agent) 提供包含已构建前端的发行包。`npx` 下载启动器，启动器自动创建独立 Python 环境并安装后端；使用时不需要源码仓库，也不需要单独构建前端。

## 环境要求

- Node.js **20.11+**，包含 `npm` 和 `npx`。
- Python **3.11+**，可创建 `venv` 并使用 `pip` 安装依赖。
- 首次安装与更新需要联网下载发行包和 Python 依赖。
- Git、Java、语言服务器与调试器按项目功能需要另行安装。

## 启动

在终端执行：

```bash
npx @luyiyanyumu/code-agent@latest
```

命令默认执行 `start`。首次运行自动准备后端环境；后续复用已完成的安装。服务启动后默认打开 `http://127.0.0.1:8765`，前端和后端使用同一个端口。保持终端运行，按 `Ctrl+C` 停止服务。

首次下载时，npm 可能询问是否安装包，输入 `y` 继续；也可在 `npx` 后加 `--yes` 跳过这项 npm 确认。

在工作台的 **设置 → 模型与连接** 中配置模型，也可以设置 README 中的模型环境变量，或在应用数据目录的 `.env` 中保存配置。没有密钥时，可先运行离线演示：

```bash
npx @luyiyanyumu/code-agent@latest --provider offline
```

离线模式使用演示提供方，不调用真实 AI 服务。

## 下载与更新

只下载并准备最新版，不启动服务：

```bash
npx @luyiyanyumu/code-agent@latest install
```

应用的 `update` 子命令也会调用 npm，从 npm registry 的 `latest` 版本下载并准备运行环境：

```bash
npx @luyiyanyumu/code-agent@latest update
```

下载完成后，在旧服务终端按 `Ctrl+C`，再启动新版：

```bash
npx @luyiyanyumu/code-agent@latest
```

更新不会自动中断正在运行的任务。npm 管理发行包的下载缓存；如需优先检查在线内容，可在 `npx` 后添加 `--prefer-online`。详情见 [npm exec 官方说明](https://docs.npmjs.com/cli/commands/npm-exec/)。

## 自定义启动

```bash
# 修改端口，不自动打开浏览器
npx @luyiyanyumu/code-agent@latest start --port 8877 --no-open

# 指定 Python 与应用数据目录；路径有空格时添加引号
npx @luyiyanyumu/code-agent@latest start --python "C:/Python311/python.exe" --home "D:/Code-agent-data"

# 查看帮助与版本
npx @luyiyanyumu/code-agent@latest --help
npx @luyiyanyumu/code-agent@latest --version
```

| 命令或选项 | 用途 |
| --- | --- |
| `start` | 准备运行环境并启动工作台；省略子命令时默认执行 |
| `install` | 仅准备当前下载版本的运行环境 |
| `update` | 从 npm registry 获取 `latest` 版本并执行安装 |
| `--no-open` | 启动后不自动打开浏览器 |
| `--host` | 指定监听地址，默认 `127.0.0.1` |
| `--port` | 指定端口，默认 `8765` |
| `--provider` | 指定模型提供方，如 `offline`、`deepseek`、`openai_compat` |
| `--home` | 指定应用数据目录 |
| `--python` | 指定创建虚拟环境所用的 Python 可执行文件 |
| `--help` / `--version` | 查看使用说明或发行包版本 |

默认监听本机地址。需要对外访问时，应先按 README 配置认证与访问边界，再调整 `--host`。

## 程序与数据目录

应用数据默认保存在 `~/.code-agent`，也可通过 `--home` 或 `CODE_AGENT_HOME` 指定其他目录。

如果系统上有多个 Python，可通过 `--python` 或 `CODE_AGENT_PYTHON` 选择解释器。

- npm 管理发行包的下载缓存。
- 启动器在应用数据目录的 `runtime/npm/<版本>-<内容标识>` 下创建 Python 虚拟环境，按版本隔离后端依赖。
- 会话、项目记录、配置和密钥继续保存在应用数据目录，升级程序不会覆盖用户数据。

发行包提供 Code-agent 主工作台；仓库中的独立 `chat-app` 仍按自己的 README 安装与启动。

## 维护者发布

完成测试与打包后，用拥有 `@luyiyanyumu/code-agent` 发布权限的 npm 账户登录，并将已验证的 scoped 发行包公开发布：

```bash
npm login
npm publish <发行包路径> --access public
```

首次发布及后续发布按 npm 账户要求完成人工登录、双因素认证或发布确认。每次发布应使用新的版本号，并确认 npm 的 `latest` 标签指向目标版本。

未来如需通过 CI 自动发布，可在 npm 包设置中配置受信任发布者（trusted publishing），再连接对应仓库与发布流程；只有账号侧完成配置后才能启用。当前不以已配置自动发布为前提。

### GitHub Release 备用分发

仍可从 GitHub Release 下载同一发行包：

```bash
npx --yes --prefer-online https://github.com/luyiyanyumu/Code-agent/releases/latest/download/code-agent.tgz
```

此地址由维护者同步上传发行包，实际版本以对应 Release 为准；常规使用优先选择 npm 短命令。
