# Chat-agent

独立的通用对话应用。本目录包含完整 Vue 前端和 FastAPI 后端，可单独复制为仓库、安装、运行和发布。源码采用 [Apache-2.0](LICENSE)，第三方依赖保留各自许可证。

默认是明确标注的**本地演示，不调用 AI**。真实对话需要在顶部「模型连接」中添加自己的服务地址、模型名和密钥，再点击「使用」。支持 OpenAI 兼容协议和 DeepSeek；图像输入需模型支持并在配置中启用。

## 本地启动

需要 Python 3.11+、Node.js 20.19+ 或 22.12+。命令均在本目录执行。Windows PowerShell，第一个终端：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m chat_agent.server
```

macOS / Linux，第一个终端：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m chat_agent.server
```

第二个终端：

```sh
npm ci
npm run dev
```

打开 [http://127.0.0.1:5174](http://127.0.0.1:5174)。对话后端默认 **8766**，前端代理同时转发 REST 和 WebSocket。编码工作台不需要同时运行。

后端使用 `--host`、`--port`、`--home` 更改地址或数据目录。Vite 使用 `CHAT_AGENT_API_TARGET` 改变代理目标。例如 PowerShell：

```powershell
$env:CHAT_AGENT_API_TARGET = 'http://127.0.0.1:8876'
npm run dev
```

## 构建和部署

```sh
npm run build
python -m chat_agent.server --static-dir dist
```

此时后端直接提供前端、API 和 WebSocket，打开 [http://127.0.0.1:8766](http://127.0.0.1:8766)。不需要 Node 开发服务或另外配置代理。`dist/licenses/` 保留源码许可证和浏览器依赖声明。

Docker：先设置自己的 `CHAT_AGENT_ACCESS_TOKEN`，再运行 `docker compose up --build -d`。Compose 默认只映射本机 8766 端口，数据保存在独立卷中。进入应用后输入此令牌。远程部署应使用 HTTPS 和明确的可信浏览器来源。

## 独立配置和数据

只读取 `CHAT_AGENT_*`，不自动发现上级 `.env`，不读取编码应用凭据。`.env.example` 是模板，需由 shell、容器或进程管理器加载，服务不会自动读取它。

| 配置 | 默认值 / 用途 |
|---|---|
| `CHAT_AGENT_HOME` | 用户目录下 `.chat-agent`；留空也使用默认目录 |
| `CHAT_AGENT_HOST` / `CHAT_AGENT_PORT` | `127.0.0.1` / `8766` |
| `CHAT_AGENT_PROVIDER` | `offline`；可选 `openai-compatible`、`deepseek` |
| `CHAT_AGENT_API_BASE` / `CHAT_AGENT_API_KEY` / `CHAT_AGENT_MODEL` | 环境变量模型配置，公开 API 不返回密钥值 |
| `CHAT_AGENT_ACCESS_TOKEN` | 本机可选；监听非本机地址时必填 |
| `CHAT_AGENT_ALLOWED_ORIGINS` | 逗号分隔的额外可信浏览器来源 |
| `CHAT_AGENT_DEFAULT_PERMISSION` | `workspace-write`；可选 `read-only` |
| `CHAT_AGENT_MAX_UPLOAD_BYTES` | `10485760`，单个附件和产物 10 MiB |
| `CHAT_AGENT_MAX_ROUNDS` | `8`，每次请求的模型与工具循环上限 |
| `CHAT_AGENT_REQUEST_TIMEOUT` | `90` 秒，每次模型响应的时间上限 |

```text
.chat-agent/
  chat.sqlite3             会话、事件、检查点和分组
  models.json              模型档案和私有密钥
  workspaces/<session>/    本会话上传的附件
  artifacts/<session>/     本会话生成的文件
```

本地服务使用单一所有者，不是多租户认证平台；不同用户运行不同实例。浏览器偏好和令牌也使用独立存储键，不修改编码工作台偏好。

## 能力与边界

- 会话分组、搜索、改名、复制、归档和删除。
- 流式回复、思考内容、用量统计、断线事件补齐、停止、重启后的未完成状态清理。
- 附件文本阅读、可选模型图像输入、任务清单、提问卡和 UTF-8 文件交付。
- 「逐次审批」只授权本次文件生成；「生成文件」允许直接交付。文件操作始终局限于当前会话。
- 对话回退删除所选消息及后续对话，附件和已生成文件保留；复制会话也复制文件。
- 当前未提供终端、编码团队、项目索引、联网检索、插件执行或 PDF/Office 解析。不支持的二进制文档会明确标记为未向模型提供内容。

会话、模型、存储、权限和工具在本项目内重建，不导入编码后端。API 文档位于 `/docs`。打开应用只读取配置和列表，不自行建立会话或发送请求。

## 导入旧聊天记录

启动不会自动读取旧库。一次性复制命令仅导入普通对话，排除项目 Lead 和子智能体：

```powershell
.\.venv\Scripts\python.exe -m chat_agent.migration --source-home "$env:USERPROFILE\.code-agent"
```

旧目录名为 `.ch-agent` 时改用该路径。源库只读打开，原记录不删除、不修改。不复制模型配置或密钥。重复运行跳过已导入会话。源库有多个所有者时必须用 `--source-user <id>` 指定一个。`--destination-home <path>` 可用于测试导入。文件链接、过大的文件和损坏事件会跳过，并在结果中计数。

导入前备份源目录，导入期间停止向旧对话写入，以便数据库和 JSONL 轨迹保持一致。导入的编码工具历史只是数据，不获得执行能力。

## 验证与开源

```sh
python -m pytest
npm test
npm run build
python -m pip wheel . --no-deps --wheel-dir build/wheels
python scripts/source-release.py --output chat-agent-0.1.0-source.zip
```

测试使用临时数据、离线模型或 HTTP 模拟，不使用个人记录或真实模型账户。此目录成为独立仓库后，`.github/workflows/verify.yml` 可直接使用。参见 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

仅发布源码和许可证，不包含 `.venv`、`node_modules`、`.env`、数据库、密钥和个人记录。项目尚未推送或公开发布。
