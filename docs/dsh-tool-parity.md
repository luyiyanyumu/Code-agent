# DSH ↔ Code-agent 工具对齐报告

**目标**：以 DeepSeek Harness（DSH）的"内置工具"为参照，盘点 `code_agent` 已实现与未实现的工具能力，给出优先级化的"需要完善"清单。

**方法与证据边界**

- DSH 端：抽离 `app.asar` 中 `@deepseek-ai/dsh-tool-*` / `@deepseek-ai/dsh-*-policy` 包的 `README.zh.md`（22 份，落在 `E:\CH-agent\.tmp\dsh_pkgs\`）作为权威来源。DSH 版本 `@deepseek-ai/dsh-desktop-runtime@0.2.0-rc.2`（读自 `dsh/package.json`）。**未读 `lib/index.js`**，所以"实现细节"层面只覆盖 README 说明的范围。
- code-agent 端：`build_registry`（`code_agent/api/app.py:105-179`、`code_agent/cli.py:23-58`）+ 全部工具类源码 + `permissions/` / `sandbox/` / `context/` / `agent/` / `sessions/` / `codeintel/`。所有结论都带 `文件:行`。

---

## 1. 一句话结论

| 维度 | DSH 内置 | code_agent（M51 之后） | 差距性质 |
|---|---|---|---|
| 模型可见工具数 | 23 个 | API **33** 常驻（+2 条件 + 动态）；CLI **19** | CLI 仍比 Web 少（缺 `edit_file`/`glob`/`list_dir`/`grep` 等） |
| 安全/正确性 | FS 观察策略、原子写、并行写租约、协作式超时、按工具字节预算+spill、重复提醒 | 六条**都有**（M41~M47）；另加 `jobs`（M45）与持久 shell（M48） | — |
| 文件发现 / 媒体 | `glob`、`read_image` | **都有**（M43 / M51）。图片默认**不发**（`CODE_AGENT_VISION` 默认关，见 §3.2） | 只剩"默认关"这一个部署差异 |
| 交付语义 | `present`（只记路径，不复制内容） | `present`（M43）：把文件**搬进交付目录**，比 DSH 多一份副本——换来与既有 artifacts 下载接口一致 | 有意不同，已在 §5 注明 |
| Office/办公文档 | office 技能 + `office-to-pdf` + `libreoffice-kit` | 随包 office-xlsx/docx/pptx 技能（M49）+ **零依赖** xlsx 脚本 | **预览/转换那一段未做** |
| 多 agent / 长任务 | subagent、workflow、agent-team、`jobs`、goal、plan-mode | `jobs` ✅（M45 起 + M50 的人可见面）；**subagent / workflow / agent-team / goal / plan-mode 仍为零** | **仍是最大单项** |

---

## 1.5 实施进度（滚动更新）

| 波次 | 内容 | 状态 | 落点 |
|---|---|---|---|
| 第一波 | P0-a 七条既有缺陷（幽灵工具名 / skill_list 权限 / WriteFilePolicy 死路 / JSON 截断 / PATCH_INLINE_LIMIT / 逐工具超时 / 无策略工具审批口径） | ✅ 已完成 | README **M41** |
| 第二波 | 原子写 + FS 观察策略（read-before-write / version guard）+ 并行写租约 | ✅ 已完成 | README **M42** |
| 第三波 | `glob` ✅ / `present` ✅ / `load_workspace_dependencies` ✅ / `jobs` 后台任务 ✅ | ✅ 4/4 | README **M43** + **M45** |
| 第四波 | 持久 shell ✅ / spill（按工具字节预算）✅ / Office 链 ✅（**预览那一段未做**，见下）｜ 另：`repeat-reminder` ✅ | ✅ 4/4 | README **M46** + **M47** + **M48** + **M49** |
| 附带 | 顺手定位并修掉一条"整跑才红"的既有测试污染（见下） | ✅ | README **M44** |
| 第二批 | "最后一公里"：`jobs` 的人可见面（只读端点）✅ ／ `read_image` ✅ ／ `edit_many` ✅ | ✅ 3/3 | README **M50** + **M51** + **M52** |

### 四波总账（M41~M49）

| 里程碑 | 内容 | 一句话证据 |
|---|---|---|
| M41 | P0-a 七条既有缺陷 | 「工具」页从"22 个都需审批"变成"只有 `skill_install` 需审批"；幽灵工具名被守卫测试抓住 |
| M42 | 原子写 + FS 观察策略 + 并行写租约 | 越界写从"白点一次审批"改成 deny；陈旧写被拒；租约接线有独立守卫 |
| M43 | `glob` / `present` / `load_workspace_dependencies` | 三个"以前答不了/传不进来"的能力 |
| M44 | 既有测试污染（整跑才红）+ `provider_override` 静默失效 | 5 个失败 → 0；单测新增 4 条 |
| M45 | `jobs` 后台任务 | 长命令不再阻塞整轮；完成通知两条投递路 |
| M46 | 按工具的输出预算 + 诚实 spill | 中文按字节算；bash 两头留；省略位置说对 |
| M47 | 重复调用提醒 | 成功不清零；与熔断器共存有专门测试 |
| M48 | 持久 shell | `cd`/`export` 跨调用保留；超时如实说"状态没了" |
| M49 | 办公文档链（随包技能 + 零依赖 xlsx） | venv 无 Office 库也能出真 xlsx |

### 第二批总账（M50~M52）

| 里程碑 | 内容 | 一句话证据 |
|---|---|---|
| M50 | `jobs` 的人可见面（`GET /sessions/{sid}/jobs`） | 名册可读，**输出正文不在响应里**（有专门断言）；别人的会话 404 |
| M51 | `read_image` | 签名识别 + 零依赖尺寸解析；开着时**字节与磁盘逐字节一致**地进请求，关着时明说"像素没发出去" |
| M52 | `edit_many`（DSH 没有） | 整批校验失败 → **一处都不写**（有专门断言）；落盘中途失败 → 如实报"哪些写了哪些没写"；多路径租约由 `Tool.write_targets()` 覆盖 |

**还没做的**（不在四波 + 第二批的枚举里，想继续就是新的一批）：plan-mode、
goal、subagent/workflow/agent-team（多 agent，最大单项）、结构化输出、多编辑、
命名文档、`pwsh` 工具本身、租约键与观察表的路径归一统一、CLI registry 与 Web 对齐、
LibreOffice 转换/预览。

### P1 剩余（诚实清单）

第三波做完之后，**P1 里还没做的**是这几项（都在第 5 节里）：

- **`#12 read_image`**：模型对 PNG 设计稿 / 用户截图 / 报错截图仍然没有入口。
- **`#17 plan-mode`**（`exit_plan_mode`）：P4 的"Plan 意图层"是**任务级**结构化计划，
  不是会话级"先出计划、批准后才写"的切换。
- **`#15 goal`**：没有跨轮次的长期目标（DSH 的 `create_goal`/`update_goal` +
  自动续行 + `blocked` 需连续 3 round）。
- **`#16 subagent / workflow / agent-team`**：最大单项，code-agent 仍是单 agent 串行。

**已做完但值得知道的边界**：`jobs` 只做了"起 → 读 → 停 → 通知"这条主线。
DSH 还多两条本文没做——**前台命令超时后自动交接为后台任务**
（`still running after <timeout>ms; moved to background job <id>`），以及
**给人和前端看的任务面**：目前没有 `/api/v1/sessions/{id}/jobs` 端点、也没有任务面板，
所以**模型能用后台任务，人看不见它们**。（本轮刻意没有先加一个没有 UI 的端点——
半个功能比明说没做更容易让人误以为它齐了。）

### 已知限制 / 后续小修（本轮**没做**，如实记下）

- **两处路径键推导不一致**：写租约用 `os.path.abspath`（`tools/registry.py::_path_key`），
  文件观察表用 `Path.resolve()`（`tools/fs_observation.py::_key`）。
  对观察表而言 `resolve()` 是对的（它关心"底层是同一份内容吗"）；对租约而言，
  `abspath` 不消解符号链接，所以"同一个底层文件、两种写法（`link.py` 与 `real.py`）"
  会拿到两把不同的锁——那是租约想防的那类竞争的一个窄缝。窄缝的窄在于：要同时满足
  "模型用两种写法写同一个文件"且"其中一种经过符号链接"。修法是把租约键也改成
  `resolve()`（工具自身的边界校验本来就在 `resolve()`），顺带统一两处；
  本轮为了不改动已通过验证的 `registry.py` 而留下，作为下一波的小修。
- **`jobs` 后台任务**（第三波第 4 项）与**第四波全部**未做，见上文清单。

---

### 这几波里踩到并修掉的"假通过"（值得单独看）

0. **"整跑才红、单跑全绿"的既有测试污染**（第四波 M44，与工具层无关但同源）。全量
   `pytest` 有 5 条失败：`test_task_routing`×2（脚本化 provider **一次都没被调用**）、
   `test_todo_tools`×1（工具没落轨迹）、`test_ws`×2（审批事件永远不来，各 243 秒后
   `WebSocketDisconnect`）——而它们单独跑**全绿**。二分到
   `tests/test_rename_compat.py::test_legacy_env_migrated`：
   `config.migrate_legacy_env()` 把值**直接写进 `os.environ["CODE_AGENT_MODEL_DEFAULT"]`**，
   而 `monkeypatch.delenv(..., raising=False)` 面对**本就不存在**的变量不登记撤销，
   于是泄漏到整个进程；该变量一旦有值，`ModelRouter.resolve_default()` 的缓存键就从
   `_provider_default` 变成 `model:<名字>`，而 `provider_override` 存的正是前者 →
   **覆盖静默失效**。修法：覆盖改成独立短路 + 测试自己兜底清变量 +
   新增 `tests/test_model_routing_override.py`。**教训**：三条看起来互不相干的症状
   可以是同一个根因；"单独跑是绿的"不构成证据。
1. **写租约曾经是死代码**（第二波，由子任务交接时点出）。租约最初只做在
   `registry.dispatch_all`，而 agent loop 并发扇出走的是 `AgentLoop._execute_tool`
   （权限 → 审批 → 执行 → 审计 → 外部化），**不经过 `dispatch_all`**：租约自己 10 条测试
   全绿，真实链路一次都没串行过。修法是把取租约暴露成 `registry.write_lease(...)`，
   由 loop 包住自己的 `_execute_tool`，并加 `tests/test_write_lease_wiring.py`
   直接量一个 turn 里两次调用的时间区间（同路径不重叠 / 不同路径必须重叠）。
   教训：**"功能在自己的测试里通过"与"功能在真实链路上生效"是两件事**，
   接线本身需要一条独立的断言。
2. **观察版本摘要必须按磁盘原始字节算**（第二波）。Windows 上 `write_text` 落 CRLF、
   `read_text` 的通用换行读回 LF；按解码文本算摘要会把**没动过**的文件永远判成
   "读完之后被改过"，门禁把正常写入全拒。测试当场抓到。
3. **`present` 的符号链接检查一度是死分支**（第三波）。`_WorkspaceBound.resolve_read`
   内部会 `resolve()`，链接信息在那一步就没了，解析后再 `is_symlink()` 恒为假——
   测试当场 `DID NOT RAISE`。改成检查解析前的原始路径。
4. **`asyncio.wait` 的默认 `return_when` 是 `ALL_COMPLETED`，不是 `FIRST_COMPLETED`**（第三波
   `jobs`）。原代码只有**一个** future（`{wait_task}`），两者完全等价，所以默认值一直
   没暴露问题；我加了第二个 future（"外部杀停事件"）之后，语义变成"等进程结束
   **并且** 等人按杀停"——而后者永远不会自己完成，于是永久挂住。
   这类"**单元素集合掩盖了默认参数语义**"的坑，只有加第二个元素时才会现形；
   反过来也说明：给一段"只有一个元素"的并发代码加第二个元素时，必须重新读一遍
   它对集合语义的假设。
5. **断言被无关数据污染**（第四波，我自己的测试写错了）。`test_output_budget.py` 里
   我为了验证窗口长度，在**整段输出**里数填充字符 `A` —— 而落盘路径
   `C:\Users\…\AppData\…` 里恰好有一个大写 `A`，于是 `count("A")` 比窗口多 1，
   测试红了。看起来像产品 bug，其实是断言把"路径里的字符"也算进了"窗口内容"。
   教训：**断言要在它真正考察的那一段上做**（改成先切出 `--- 开头 N 字节 ---`
   与省略页脚之间的那一段再数）。同类的还有一条：`test_context.py` 里
   写死的 `< 1000` 上界对应的是当时固定的 400 字预览，预算变成可配之后
   **那个数字不再是产品常量**——继续写死它会变成"一改预算就红"的假门槛。
6. **把 GC 噪声当成确定性泄漏**（第四波，我自己差点写进报告）。做持久 shell 时发现
   `unclosed transport` 的 ResourceWarning，我写了个探针按"四条生命周期路径"数泄漏，
   得到 `1) 0  2) 0  3) 2  4) 2`——看着像"超时与 kill 两条路径都漏"。但**同一条路径的
   另一次运行报 0**：ResourceWarning 是 GC 时序决定的，而探针按"哪个 catch_warnings
   窗口里出现"归因，前一个用例的对象在后一个用例的窗口里被回收，就会记到后一个头上。
   我差点据此去改 `KILL_GRACE`。真正站得住的两步是：**①把 executor 的改动还原后
   同样复现**（证明"既有的"而不是"我引入的"）；**②只用确定性断言写测试**
   （`_release` 关不关 stdin、`_kill` 有没有回收 taskkill——用假对象与源码断言，
   不数真进程的句柄）。留下的结论因此分得清：修掉的是两处**确定的**句柄未回收，
   剩下的是一条**既有的、时序相关的**警告（单文件跑会出现，全量退出码仍是 0）。
7. **测试自己弄脏了要发布的源码树**（第四波 M49）。我给"随包技能"写测试时，一边用
   `importlib` 加载 `code_agent/skills/…/write_xlsx.py`（Python 顺手在里面生成了
   `__pycache__`），一边又在同一个目录里手工造 `__pycache__/junk.pyc` 再清理——
   结果真 `.pyc` 还在、`rmdir` 失败：`OSError: WinError 145`。**这条测试单独跑是绿的**
   （顺序不同、`.pyc` 还没生成），跑整组才红。两个改法都对症：加载脚本时
   `sys.dont_write_bytecode = True`；要造脏数据的测试**在临时副本上动手**。
   顺带发现的产品问题：`resource_files` 会把 `__pycache__/*.pyc` 列成"可读资源"
   （清单是给模型按需 read_file 的，列字节码等于引导它去读二进制）——
   以前技能里基本只有 `SKILL.md`，第一次真的带 `scripts/` 就现形了。
   （这三条与第一波修掉的 6 个"幽灵工具名"是同一种病：**看起来做了，其实没做**。）

第二波落地时踩到并修掉的一个真 bug（值得单独记）：**观察版本摘要必须按磁盘原始字节算**。
Windows 上 `Path.write_text("a\n")` 落盘是 CRLF，而 `Path.read_text()` 的通用换行又把
CRLF 读回 LF；若一边按原始字节、一边按解码文本算摘要，**一个完全没动过的文件会被
永远判成"读完之后被改过"**，门禁于是把正常写入全部拒掉。这是我自己的测试当场抓到的
（`tests/test_fs_observation.py`），不是推理出来的。

**最终验证结果（跑完才写的）**：全量 `pytest` = **1165 项、0 失败、exit 0**（78 个用例文件，
约 25~35 分钟；最慢单文件 `tests/test_code_parallel.py` 约 100 秒，其次是几个真起进程的
沙箱/持久 shell 用例）。
这个数字随功能增长：第三波结束时 1015 → `jobs` 后 1044 → M46/M47 后 1068
→ M48（持久 shell）后 1090 → M49（office 技能）后 1114 → **M50/M51 后 1150
→ M52（edit_many）后 1165**。
**最后一次全量是在冻结的最终代码状态上跑的**（M52 的收尾——`write_targets` 的多路径
租约支持——已在那次之前落地），所以"零失败"覆盖的就是交付状态本身。

第一次全量曾出现 **5 失败**，全部是同一个既有测试污染（见下面第 0 条），修掉之后连
`test_ws` 那两个 243 秒的审批超时也一起消失——**总时长从 20 分钟降到 16 分钟**
（之后每加一批真起进程/真写文件/真起子进程的用例就涨一点，这是真实开销）。

过程纪律（五条都是踩出来的）：

- 每一波用**定向测试**（每次 40~190 项）迭代，全量只在最后跑一次；
- 全量运行期间**绝不动 Python 文件**——第一次全量的 5 个失败里，有 3 个正是我在
  跑测试的同时并发编辑模块造成的瞬时不一致（自伤，不是产品缺陷）；
- 收尾小修之后**再跑一次全量**，而不是用"定向测试过了"代替；
- **不拿 GC 时序噪声当结论**：ResourceWarning 是"谁被回收"决定的，同一条代码路径
  不同运行会报 0 个或 2 个（我的泄漏探针就自相矛盾过）。要写成断言就用假对象或源码断言，
  要下结论就做**对照实验**（把改动还原后是否同样复现）；
- 环境教训：`tests/test_code_parallel.py` 是 git worktree 重型用例，
  **两个 pytest 进程在同一仓库并发跑它会互相抢 git 锁**，把 116 秒拖成十几分钟，
  看起来像死锁。我一度以为新加的写租约死锁了，实测租约文件单独跑 **1.53 秒通过**——
  "观察到卡住"和"代码有死锁"之间隔着一层必须验证的假设。

---

## 2. DSH 内置工具盘点

### 2.1 模型可见工具

| 工具名 | DSH 包 | 关键参数 | 关键语义 |
|---|---|---|---|
| `read` | `dsh-tool-fs` | `file_path`, `offset?`, `limit?` | 行号化 UTF-8；`offset` 从 1 起；`limit` ≤ `readLimit`(2000)；按 `readMaxBytes`(51200) 截断；≥ `readStreamMinSize`(10MB) 流式读 |
| `read_image` | `dsh-tool-fs` | `file_path` | PNG/JPEG/WebP/GIF；无扩展名按**文件签名**识别；服务端按路由能力自动缩放；仅当持久 `ctx.attachments` 可用且模型路由声明图像输入时注册 |
| `write` | `dsh-tool-fs` | `file_path`, `content` | **原子覆盖**；挂观察策略后：覆盖要求会话 read 过且文件自读后未变（`replaceIfVersion`），创建走 `createIfAbsent`；陈旧→`FS_STALE_VERSION` |
| `edit` | `dsh-tool-fs` | `file_path`, `old_string`, `new_string`, `replace_all?` | 字面量替换；唯一匹配（除 `replace_all`）；同策略要求先 read 且未变 |
| `glob` | `dsh-tool-fs-search` | `pattern`, `path?` | **ripgrep 后端**；含隐藏/忽略文件但排除 VCS 元数据；不含 `/` 的模式匹配任意深度基名（`*` 匹配整棵树）；按 mtime 排序；上限 100；溢出可选按顶层轮转采样；溢出写 spill |
| `grep` | `dsh-tool-fs-search` | `pattern`, `path?`, `include?` | ripgrep 正则；`include` 是**单正向 glob**（逗号列表/否定值前置拒绝）；上限 250；溢出写**格式化 spill 产物**；按文件分组 `Line N: <preview>` |
| `bash` | `dsh-tool-bash` | `command`, `run_in_background?`, `timeout?`, `sandbox_permissions?`, `justification?` | 每次全新 shell；`run_in_background` 立即返回 job id；**前台超时仍运行 → 自动交接为 job** 并提示 `[still running after <t>ms; moved to background job <id>]`；沙箱拒绝时可请求更宽权限重试**一次** |
| `bash` 持久 | `dsh-tool-bash-persistent` | 同上 | 同一 agent 跨调用保留 cwd、exported 变量、函数 |
| `pwsh` | `dsh-tool-pwsh` | 同 bash | Windows 首选；机制同 bash |
| `pwsh` 持久 | `dsh-tool-pwsh-persistent` | 同上 | 同上 |
| `web_search` | `dsh-tool-web` | `queries[1..4]` | 多查询并发、轮询合并、URL 去重、按 `searchMaxResults`(8) 截断；超时 30000ms；**任一查询失败即批式 abort**；结果渲染为 `- [<title>](url)` |
| `web_fetch` | `dsh-tool-web` | `url` | HTML→GFM markdown（先删活动/隐藏内容，词法嵌套守卫防不安全 HTML）；非 2xx **写在结果里而非抛错**；超时 30000ms；`fetchMaxOutputChars`(200000)；截断附固定恢复提示 |
| `todo_write` | `dsh-tool-todo` | `todos:[{content,status}]` | status ∈ pending/in_progress/completed；**整表替换**；必需项失败文案固定（`must be a non-empty string` / `duplicate content`）；可选 `allowParallelInProgress:false` |
| `skill` | `dsh-tool-skill` | `name` | 会话目录在**首次请求前**持久注入（`<available_skills>` 信封）；用户 `/name` 由 pre-step 注入同一条 `<skill_content>` 并告知模型不要重复加载；目录变化追加**完整替换**，空目录显式停用旧名 |
| `ask_user_question` | `dsh-tool-ask-user` | `questions[]`（id/question/header/options/multi_select） | 默认阻塞；`mode:timed`+`timeout` 可反悔（`{"pending":true,...}`）；回答案 `{answers:[{id,selected,custom?}]}`；存活子级禁调（`DELEGATED_CALLER`） |
| `present` | `dsh-tool-present` | `files:[{path, description?}]` | **只记坐标，不复制内容**；相对路径按 session cwd 解析，绝对路径可指向工作区外；缺失/目录/符号链接/拒绝访问都失败；每调用文件数有上限 |
| `subagent`（多实例） | `dsh-tool-subagent` + `-control` | 依提供方 | 提供方 `spawn`/`fork`/`acp`；`one-shot`（前台等结果）/ `continuable`（后台启动返回可续聊 id，`run_in_background` 默认 true）；可配 persona、工具权限、深度限制；**失败返回错误而非部分成功** |
| `list_subagent_models` | `dsh-tool-subagent`（条件） | — | `modelSelectionSettings:true` 时给子级选择 provider/model/reasoning_effort |
| `workflow` | `dsh-tool-workflow` | `script`(JS 编排), `run_in_background?` | 前台等全部结束；后台立即返回 `{kind:'background', jobId, runId}`；取消/异常→错误而非部分成功；注册为独立 `workflow` job |
| `agent-team`（实验） | `dsh-experimental-tool-agent-team` | `spawn_teammate` 等 | 覆盖 `subagent`/`subagent_fork` 与全局 child control；Team roster、消息、interrupt、wait、任务板 |
| `get_goal`/`create_goal`/`update_goal` | `dsh-tool-goal` | `objective`, `max_goal_rounds?`, `goal_id`, `revision`, `action` | 长期目标自动续行；action ∈ edit/pause/resume/complete/blocked；`blocked` 要求**同一 blocker 连续 3 round**；session resume/fork 后 active goal 转 **disarmed**，需显式 `resume` 重新装配 |
| `job_output`/`job_list`/`job_kill` | `dsh-tool-jobs` | `job_id`, `wait?`, `timeout_ms?`, `reason?` | 后台命令/PTY/**subagent** 全部进 `ctx.jobs`；读取非消费式；完成通知：繁忙 agent 注入 next-step inbox，空闲 agent 唤醒一轮（受 `maxConsecutiveWakes` 约束）；已被 `wait` 收走的结算不再通知 |
| `exit_plan_mode` | `dsh-plan-mode` | （计划正文） | 注册常驻、**仅 plan-mode 激活时可执行**；批准→静默 pending-exit；`Keep planning` 带反馈回炉；无交互通道或被重载时以拒绝失败 |
| `load_workspace_dependencies` | `dsh-tool-workspace-dependencies` | — | 返回随包 Python / Node.js / pnpm 绝对路径；Office skills 靠它取解释器 |
| `str_replace_editor` | `dsh-tool-str-replace-editor` | `command`(view/create/str_replace) | `edit` 的另一层封装，主要服务 PTC 呈现模式 |

### 2.2 工具级策略包（不暴露工具名，但改工具行为）

| 策略 | 作用 |
|---|---|
| `dsh-fs-observation-policy` | **read-before-write + version guard**：编辑前必须 read；自读后变更（含被删）→ `FS_STALE_VERSION`；读取缺失路径记 `absent`，后续 `write` 走 `createIfAbsent`；**观察状态不跨会话持久化**，恢复的会话必须重读 |
| `dsh-tool-call-timeout-policy` | 给声明了 `*TimeoutMs` 的工具挂**协作式**超时；只请求取消，忽略信号的工具不会被硬停 |
| `dsh-repeat-tool-reminder` | 第 `[3,5,8]`（可配）次重复同一工具同一调用时追加提醒；`include`/`exclude` 列表；被排除的调用**既不递增也不重置**计数器（记录类工具不能掩盖循环） |
| `dsh-output-retention` | `ItemRetainer`（按项 head/tail）+ `TextRetainer`（字节流 head/tail/headTail）；`truncated` 只表示**预算事实**，绝不并入"上游不完整"；`formatRetentionNotice` 标准化省略子句，恢复指引由工具自己写 |
| `dsh-spill` / `dsh-spill-policy` / `dsh-spill-local` | 溢出落盘；标准措辞：`(Omitted N bytes. Full formatted result stored at: …. Use read with offset/limit, or grep this path to search within it.)` |
| `dsh-compaction-tool-result-pruner` | 超阈值（标准 preset 8192 字符）的工具结果裁剪 |
| `dsh-permission-presets` | `workspace-write` / `danger-full-access` / `read-only` 三档 |
| `dsh-sandbox*` / `dsh-fs-sandbox` / `dsh-bash-sandbox` / `dsh-pwsh-sandbox` | 多后端沙箱（含 Windows ACL / AppContainer） |
| `dsh-user-approval` | 默认策略 `ask`；审批超时 fail closed |
| `dsh-experimental-auto-review` | 工具预执行自动裁决；拒绝以 `AUTO_REVIEW_DENIED` 标记 |
| `dsh-agent-tool-presentation` | 工具呈现模式 `native` / `ptc` / `both` |

---

## 3. code-agent 工具盘点

### 3.1 工具清单（含 CLI/API 分叉——这是第一个发现）

| 工具名 | 类 / 文件 | API | CLI | 关键语义 |
|---|---|:--:|:--:|---|
| `bash` | `tools_builtin.py:63` | ✅ | ✅ | 同步 `subprocess` + `asyncio.wait_for`；超时杀整棵进程树（Windows `taskkill /F /T`，POSIX `killpg`，`KILL_GRACE` 3s）；输出截 `MAX_OUTPUT`(64KiB)；cwd 限 ctx.workspace；**刻意不用 `shutil.which("bash")`**（WindowsApps 的 WSL stub 会劫持），只认 Git Bash 候选路径，找不到退回 cmd.exe 并在描述里如实说明；超时上限硬编码 300s。**M45 起**多了 `run_in_background`（挂成后台任务，无执行器超时）；**M48 起**另有 `bash_persistent`（跨调用保留 cwd / 导出变量 / 函数） |
| `read_file` | `tools_builtin.py:99` | ✅ | ✅ | 行号化；不给 `offset` → 读前 64KiB；给了 → 按行截 2000 行；路径白名单 = `workspace` / `tool_results_dir` / `skills_dir` |
| `write_file` | `tools_builtin.py:152` | ✅ | ✅ | **无版本检查**；越界直接抛 `ToolError`；`mkdir(parents=True)` + `write_text`（**非原子**） |
| `list_dir` | `code_tools.py:54` | ✅ | ❌ | 跳过 `.git/node_modules/.venv` 等；`depth` ≤ 4、`max_entries` ≤ 1000；**无 glob 模式** |
| `grep` | `code_tools.py:117` | ✅ | ❌ | 单文件 2MB 上限、二进制跳过；`glob` 文件名过滤 + `context`(≤5) + `max_results`(≤40)；**不自己裁**，溢出交给 M46 的按工具预算（`grep` = 12 KiB 留头 + 落盘指针） |
| `code_map` | `code_tools.py:199` | ✅ | ❌ | 符号索引分片（定位入口） |
| `code_features` | `code_tools.py:290` | ✅ | ❌ | 按"能干什么"查（功能导航） |
| `code_search` | `code_tools.py:429` | ✅ | ❌ | 融合检索（向量 + 关键词 + 图谱按需） |
| `code_graph` | `code_tools.py:509` | ✅ | ❌ | 调用图只读 |
| `edit_file` | `code_tools.py:580` | ✅ | ❌ | 同调内 read-modify-write（**非跨调用 version guard**）；默认唯一匹配；命中 0/多处报错并给候选行号；写完落 patch 台账（改前/改后全文 + sha1）；越界拒绝 |
| `save_memory` | `memory/tools_save.py:18` | ✅ | ✅ | 两层记忆写入 |
| `deliver_file` | `extensions/artifacts.py:51` | ✅ | ✅ | **必须传 `content`**（≤10MB），写 `artifacts/<session>/<ts>-<filename>` |
| `load_skill` | `extensions/packs.py:230` | ✅ | ✅ | `name = f"load_{kind}"`；加载正文并列出附带资源清单（模型再 `read_file` 读绝对路径） |
| `load_expert` | `extensions/packs.py:263` | ✅ | ✅ | 同上（Expert 包） |
| `web_search` | `web_tools.py:73` | ✅ | ✅ | DeepSeek 原生 `web_search_20250305`；`queries[1..4]`；单条摘要 600 字，总上限 12000；`wrap_untrusted` 包裹 |
| `web_fetch` | `web_tools.py:117` | ✅ | ✅ | SSRF 闸门（仅公网 http/https）；`max_chars` 夹在 500–60000；`wrap_untrusted` 包裹 |
| `skill_list` / `skill_search` / `skill_install` | `skill_tools.py:154/198/231` | ✅ | ✅ | 技能商店三件套（M20） |
| `acceptance_set` / `acceptance_check` | `acceptance_tools.py:28/82` | ✅ | ❌ | M29 有序验收；门禁只报第一个未满足项 + 进度 ρ=ℓ/n |
| `contract_check` | `contract_tools.py:31` | ✅ | ❌ | M32 契约骨架校验 |
| `feature_brief` | `brief_tools.py:26` | ✅ | ❌ | 符号"作用"补白（内容哈希缓存） |
| `todo_write` | `todo_tools.py:41` | ✅ | ❌ | M30 整表替换任务清单 |
| `ask_user_question` | `ask_tools.py:54` | ✅ | ❌ | M31 阻塞式提问；见 §6 优势 |
| `kb_search` / `kb_write` | `knowledge/tools.py:19/57` | ✅* | ❌ | *仅当传入 `KnowledgeBase`（`app.py:139-141`） |
| `mcp__<server>__<tool>` | `extensions/mcp_client.py:83` | 动态 | ❌ | 启动时连接；连不上不阻塞 |
| 插件代码工具 | `extensions/plugins.py` | 动态 | ❌ | **双开关** `CODE_AGENT_PLUGIN_CODE=1` **且** 清单 `codeTools=true` |
| `tool_search` | `tools/defer.py:18` | **❌** | ✅ | **延迟加载只存在于 CLI**：唯一调用方是 `cli.py:34-38`，且只 defer `write_file`。Web/API 的 `build_registry` 从不用 `DeferredToolRegistry` |

> **M43 新增的三个工具**（本表在它们之前写成，故未入表）：`glob`（`tools/builtin/code_tools.py`，API ✅ / CLI ❌，只读）、`present`（`extensions/artifacts.py`，API ✅ / CLI ✅，kind=allow）、`load_workspace_dependencies`（`tools/builtin/env_tools.py`，API ✅ / CLI ✅，只读）。
> **M45 新增的三个工具**：`job_output`（只读）、`job_list`（只读）、`job_kill`（kind=allow，只读档拒绝——它会杀进程）。另：`bash` 多了一个 `run_in_background` 参数（不是新工具，是同一个工具的第二个模式）。
> **M48 新增的工具**：`bash_persistent`（持久 shell；策略与 `bash` **同源** —— 它同样在执行命令，换个工具名不能换个策略）。
> 七个新工具都已进 `BUILTIN_TOOL_POLICIES` 登记表，因此「工具」页文案与只读档放行名单自动跟上。
> **M49 没有加工具**：办公链补的是**随包自带技能**（`code_agent/skills/`：office-xlsx / office-docx / office-pptx）+ 可选依赖声明，不是新工具——这正是 DSH 的做法（`dsh-skill-office` 是技能包，`dsh-tool-*` 里没有 office 工具）。模型的路径是 `load_skill("office-xlsx")` → 读自带脚本 → `bash` 跑它 → `present` 交付。
> **M51 加了一个工具**：`read_image`（只读）。它与 `read_file` 的关键差别是**是否把内容发给模型**——图片字节只有在 `CODE_AGENT_VISION` 设成 1 时才随下一次请求发出（默认关：默认 provider 是 deepseek，chat 接口不吃 `image_url` 块）；没开时工具只给文件事实并明说"像素没发出去"。
> **M50 加了一个端点**：`GET /api/v1/sessions/{sid}/jobs`（只读，**不含输出正文**）。
> **M52 加了一个工具**：`edit_many`（一次多处精确替换，**DSH 没有**）。它同时给租约补了**多路径**能力：`Tool.write_targets()` 让一次写多个文件的工具为每个路径各取一把锁（按排序取，防死锁）。

> **发现 1（一致性）**：CLI 的 `build_registry`（`cli.py:23-58`）只有 12 个工具，**缺** `list_dir` / `grep` / `glob` / `edit_file` / `code_*` / `acceptance_*` / `contract_check` / `feature_brief` / `todo_write` / `ask_user_question` / `kb_*`。也就是说 CLI 会话**没有精确编辑工具**，只能整文件 `write_file`——而 `write_file` 恰恰没有版本守护。（M42/M43 给 `write_file` 补上了原子写与观察门禁，但"CLI 没有 edit_file"这条仍未解决。）

### 3.2 机制层

| 模块 | 事实 |
|---|---|
| `permissions/hooks.py` | 三档 `read-only` / `workspace-write`(默认) / `full-access`；**`full-access` 连 bash 黑名单都绕过**（`:149-152`）；审批回调缺失或抛异常 → fail closed（`:189-199`）；审批超时 120s，提问超时 600s |
| `permissions/policy.py` | `BashPolicy` 黑名单 17 条 + 白名单前缀 → 其余 `need_approval`；`DefaultPolicy` = **一律 need_approval**（`:203-207`） |
| `sandbox/executor.py` | 输出截 64KiB；超时杀进程树；`CODE_AGENT_SANDBOX_MODE=docker` → `--network=none --memory=512m --pids-limit=256 --read-only` |
| `context/externalize.py` | **按工具的字节预算 + 窗口策略**（M46）：`TOOL_WINDOWS` 给每个工具配 `(开头字节, 结尾字节)`（`bash` 1024+15K 两头留、`grep`/`glob`/`code_*` 留头、`web_fetch` 6K+6K、`read_file` 32K、默认 16K 留头）；超预算落 `<tool_results_dir>/<session>/<call_id>_<sha1[:10]>.txt`（**完整原文**），返回"窗口 + 落盘路径 + 省略说明"（说清这是**预算**截断／省了多少字节／怎么捞全文）；切窗口按 **UTF-8 边界**，不留替换字符；`CODE_AGENT_EXTERNALIZE_THRESHOLD`（字节）显式设定可覆盖所有工具的窗口 |
| `context/compact.py` + `prompt.py` | 预算 = `max(4000, (window*0.5 - (system+tools)/2.2)*2.2)` 字符；未知模型 → 固定 `CODE_AGENT_HISTORY_BUDGET`(24000)；阶段 1 `prune_old_tool_results(keep_last=8, 旧→160 字符头, 新→封顶 2000)`；阶段 2 从尾部回填、丢孤儿 tool 消息；**纯确定性，无 LLM 摘要**（`compact.py:8` 的 LLM 槽位是注释里的"预留"） |
| `agent/loop.py` | 每轮处理**全部** tool_calls（无上限）；`CODE_AGENT_TOOL_PARALLEL!=0` 且 >1 个调用时 `asyncio.gather`，否则串行；**无逐工具超时、无重试** |
| `agent/breaker.py` | 指纹 = 工具 + 归一化参数（剥掉易变键）；**连续 5 次**同样失败触发（`CODE_AGENT_FAILURE_STREAK`）；任一成功清零；触发后 `stop_reason="repeated_failure"`；失败判定 = 结果文本以 `[tool error]` / `[permission denied]` 开头 |
| `sessions/checkpoints.py` + `rewind.py` | 检查点 = **轨迹 seq + patch 台账 id，不是文件快照**；rewind 取所有更晚检查点的 patch 并集，newest-first 逆向应用，**再**截断轨迹 |
| `codeintel/service.py:246-250` | 唯一的写入时版本防护：**回滚**时若 `sha1_text(当前) != after_sha1` 则拒绝 |
| `codeintel/patches.py` | 每次 `edit_file` / 编辑器保存 / 回滚本身都记改前改后全文 + sha1 + session id |

---

## 4. 横向对比

✅ 覆盖 / 🟡 部分覆盖 / ❌ 缺失

| DSH | code_agent | 差距说明 |
|---|---|---|
| `read` | ✅ `read_file` | 参数齐；路径硬边界更严 |
| `read_image` | ❌ | 无任何图片入口 |
| `write`（原子 + 观察策略） | 🟡 `write_file` | 无原子写、无版本守护 |
| `edit` | ✅ `edit_file` | 唯一匹配 + patch 台账；缺跨调用守护 |
| `glob` | 🟡 | 只有 `list_dir`(≤4 层) 与 `grep` 的 glob 过滤 |
| `grep` | ✅ `grep` | 参数更丰富；缺字节预算+spill |
| `bash` | ✅ `bash` | 进程树杀 + shell 如实声明；缺持久 shell、缺后台 job |
| `bash`/`pwsh` 持久 | 🟡 `bash_persistent` | M48 已做 bash 版（cwd/变量/函数跨调用保留）；pwsh 版**未做**（code-agent 连 `pwsh` 工具本身都还没有） |
| `pwsh` | 🟡 | 没有独立 PowerShell 工具 |
| `web_search` / `web_fetch` | ✅ | 注入防护更强（结构化 `wrap_untrusted`） |
| `todo_write` | ✅ | 同款 |
| `skill` | ✅ `load_skill` + 目录注入 | 入口更碎（4 个工具），但语义覆盖 |
| `ask_user_question` | ✅ | 字段几乎相同，细节更稳 |
| `present` | 🟡 `deliver_file` | 必须传全文 → 二进制/大文件/脚本产物不可行 |
| `subagent` / `workflow` / `agent-team` | ❌ | 完全缺失 |
| `get_goal` / `create_goal` / `update_goal` | ❌ | 内部**有**自动续跑（`manager.py:789-864`，上限 40 次续跑 / 1200 总轮），但**模型不可见、不可控** |
| `job_output` / `job_list` / `job_kill` | ❌ | bash 完全阻塞，无接管、无通知、无并发 |
| `exit_plan_mode` | ❌ | P4 的"Plan 意图层"是任务级结构化计划，不是会话级 plan-mode |
| `load_workspace_dependencies` | ❌ | 缺失 |
| `str_replace_editor`（PTC） | ❌ | 无 PTC |
| Office 技能链（`dsh-skill-office` + `dsh-office-to-pdf` + `libreoffice-kit`） | ❌ | 缺失 |

### DSH 策略 vs code_agent

| DSH 策略 | code_agent | 说明 |
|---|---|---|
| `fs-observation-policy` | ❌ | 最显眼的安全缺口 |
| `atomic-write` | ❌ | `write_text` 直写 |
| 并行写租约 | ❌ | `dispatch_all` 无文件级串行化 |
| `tool-call-timeout-policy` | 🟡 仅 bash | 其余工具无 deadline |
| `repeat-tool-reminder` | ✅ **已对齐**（M47，`agent/repeat_reminder.py`） | 阈值 `[3,5,8]`、`include`/`exclude`、被排除的调用既不递增也不重置；**成功不清零**（与熔断器互补：它管"成功但没用的重复"） |
| `output-retention` + spill | 🟡 | 全局字符阈值、无 per-tool 预算、无标准化恢复指引 |
| `compaction-tool-result-pruner` | 🟡 | 有确定性裁剪，无按工具策略的 pruner |
| 权限预设 / 沙箱 / 审批 | ✅ | M6/M15 落地 |
| `auto-review` | ❌ | 缺失 |
| PTC 呈现模式 | ❌ | 缺失 |

---

## 5. 需要完善清单

> **落地状态**：下面 P0-a 的 1~7 条 = 第一波（M41，✅ 已修，含守卫测试）；
> P0-b 的 8~10 条 = 第二波（M42，✅ 已修）；P1 的 #11/#13/#18 = 第三波（M43，✅ 已做）、
> #14 = M45（✅ 已做）；P2 的 #20/#21 = M46/M47（✅ 已做）、#19 = M48（✅ 已做）、
> #24 Office 链 = M49（✅ 已做，**预览/转换那一段未做**）。
> 第二批（"最后一公里"）：#12 `read_image` = M51（✅ 已做）、#22 多编辑 = M52（✅ 已做，
> 而且比 DSH 更进一步——DSH 靠 PTC 打包，没有独立的多编辑工具）、
> `jobs` 的人可见面 = M50（✅ 已做，原名册只有模型看得见）。
> 其余（#15 goal、#16 subagent-family、#17 plan-mode、#23/#25）**未做**。
> 清单本身保留原样，便于对照"当时判断的是什么"。
>
> **四波的"完成"指的是当初列进计划的那一项落地了**，不等于本报告所有缺口都补完：
> P1 还剩 read_image / plan-mode / goal / subagent 家族，P2 还剩结构化输出 / 多编辑 /
> 命名文档，另有若干"已知限制"（见 §1.5 末尾）。

### P0-a — 工具层已有的正确性缺陷（低成本、高收益，先修这些）

| # | 缺陷 | 证据 | 修法 |
|---|---|---|---|
| 1 | **权限白名单里有 7 个不存在的工具名**：`list_files`、`get_file_info`、`search_files`、`glob`、`list_artifacts`、`read_artifact`、`knowledge_search`（真名是 `kb_search`）。它们既是 `READ_ONLY_TOOLS` 成员又注册了 `ReadOnlyPolicy`，但没有任何类实现 | `permissions/hooks.py:50,51,52,54,59,60,61`；`hooks.py:118-119` | 删掉幽灵名，或按计划补齐 `glob` / `list_files` 等工具；`knowledge_search` 改名对齐 `kb_search` |
| 2 | **`skill_list` / `skill_search` 声明是"只读、放行"，实际走 `DefaultPolicy` = `need_approval`** | 声明：`tools/builtin/skill_tools.py:14-15`；未注册策略：`hooks.py:100-140` 无这两项；`DefaultPolicy` = need_approval：`policy.py:203-207` | 给二者注册 `ReadOnlyPolicy`（与 `code_map`/`code_graph` 同档） |
| 3 | **`WriteFilePolicy` 的"越界转审批"分支不可达** | 该分支在 `policy.py:99-102`，但 `write_file`/`edit_file` 先用 `_WorkspaceBound` 抛 `ToolError`（`tools_builtin.py:33-39`、`code_tools.py:620`），策略永远看不到越界参数 | 二选一：要么让工具不再硬拒、把越界交策略裁决（保留审批能力），要么承认"硬边界"并删掉死分支与相关文档 |
| 4 | **`code_*` 工具按字符切 JSON → 截断处产出非法 JSON** | `code_tools.py:253,287,354,426,506,577` 全是 `json.dumps(...)[:8000/12000]` | 改成"条目级裁剪 + 明确的 `truncated` 字段"（DSH `ItemRetainer` 的做法）；至少别切在 JSON 中间 |
| 5 | **`PATCH_INLINE_LIMIT` 定义了但全仓无人使用** | 定义 `code_tools.py:33`；全仓仅此一处 | 接上或删除 |
| 6 | **没有逐工具超时框架** | `loop.py` 无 deadline；只有 bash 有（`tools_builtin.py:96`，夹在 300s） | 参考 `dsh-tool-call-timeout-policy`：给 web_search/web_fetch 默认 30s，其余可配；实现为**协作式**（尊重 `CancelledError`，不硬杀） |
| 7 | `web_search`/`web_fetch`/`save_memory`/`deliver_file`/`load_skill`/`load_expert` 无专用策略 → 默认每次人工审批 | `hooks.py:100-140` 未覆盖；`policy.py:203-207` | 逐项确认意图：`web_*` 是否要"联网开关 + 审批"双层（M19 只做了开关） |

### P0-b — 安全/正确性（需要写代码）

8. **FS 观察策略：read-before-write + version guard**（对标 `dsh-fs-observation-policy`）
   - 现状：`write_file`（`tools_builtin.py:152`）无条件覆盖；`edit_file`（`code_tools.py:580`）单调内重读再写，靠 `old_string` 比对兜底，**跨调用不保护**。唯一写入时防护是**回滚**路径（`codeintel/service.py:246-250`）。
   - 最小可行：把"被 read 过的 (path, mtime, sha1)"观察表挂在 session 上；`write_file` 覆盖现有文件时要求观察存在且未变，否则报错。错误文案照抄 DSH 已被模型训练过的两句：
     - `cannot modify "<path>": file has not been read — read the file, then retry`
     - `… — re-read the file, then retry`（陈旧版）
   - 顺带：读取缺失路径记为 `absent`，允许随后的 `write` 以 `createIfAbsent` 语义创建（防覆盖并发创建者）。
   - 与已有资产一致：`patches.py` 的回滚硬规则本来就是同一思路，可抽出共用。
   - 测试缺口：`test_code_edit.py` / `test_registry.py` 无 stale-version 用例。

9. **写原子化**（对标 `dsh-atomic-write`）
   - `path.write_text(...)` 在崩溃/被杀时可能留半截文件。改成 `tempfile` + `os.replace`（Windows 同卷 `os.replace` 已是原子替换）。

10. **并行同文件写租约**（README M18 P5 已有设计）
    - `tools/registry.py:43` 与 `:105` 的 `dispatch_all = asyncio.gather(...)` 对同文件的两个写操作没有任何串行化。最小可行：按解析后的绝对路径加锁；粗粒度（同会话一把写锁）已足够。

### P1 — 能力放大

11. **`glob` 文件发现工具**——复用 `grep` 的 ripgrep 内层，`glob(pattern, path?)`，含隐藏/忽略文件、排除 VCS 元数据、按 mtime 排序、上限 100、溢出写 spill。同时可以把 P0-a#1 里那个幽灵 `glob` 白名单项变成真的。

12. **`read_image` 工具**——PNG/JPEG/WebP/GIF，按签名（而非扩展名）识别；服务端自动缩放；复用 `attachments` 通道回注模型。

13. **`present` 工具**——`present(files:[{path, description?}])`，只记坐标不复制。当前 `deliver_file` 要求 `content`，脚本生成的 xlsx/pdf/大图**根本传不进去**。注意 DSH 也承认：shell 沙箱私有 `/tmp` 里的产物要先写进 session 可访问位置。

14. **`jobs` 后台任务控制**——`run_in_background` + `job_output`/`job_list`/`job_kill` + 完成通知。这是 P1 里**性价比最高**的一项：bash 工具加一个 schema 字段 + 一张 `ctx.jobs` 注册表 + 一个 inbox 投递（可复用 M31 提问那套"挂起/送达"机制）。DSH 的细节值得抄：
    - 前台超时 → **自动交接**为后台 job（而不是杀掉），并回 `[still running after <t>ms; moved to background job <id>]`；
    - 完成通知：繁忙 agent 注入下一步，空闲 agent 唤醒一轮，且受 `maxConsecutiveWakes` 预算约束；
    - 已被 `wait` 收走的结算**不再通知**（避免"读过了还被叫醒"）。

15. **`goal` 多轮自动续跑**——`get_goal`/`update_goal` 两个工具（`create` 可并入 `update_goal` 或单列）。第 2 节表格里的 `revision` CAS、`blocked` 需连续 3 round、session resume 后 disarmed 需显式 resume —— 这三条是产品决策，建议照搬。当前 `manager.py:789-864` 的内部续跑可以成为它的执行底座（把"自动"变成"模型可控 + 用户可见"）。

16. **`subagent` / `workflow` / `agent-team`**——最大单项工程。建议分阶段：
    - **P1**：subagent 本地 in-process 提供方 + 上下文继承开关 + `run_in_background`；
    - **P2**：workflow（JS 编排 + fan-out + 注册为 job）；
    - **P3**：agent-team（roster / 消息 / interrupt / 任务板）。

17. **`exit_plan_mode`（会话级 plan-mode）**——注册 `exit_plan_mode` 工具 + session 级 `plan_mode` 标志；激活时用 `FilteredToolRegistry` 把写类工具从 schema 摘掉，并注入 `plan:policy` 提示段落；UI 批准后退出。P4 的"Plan 意图层"（任务级 goal/subtasks）与之**互补而非替代**。

18. **`load_workspace_dependencies`**——启动时各取一次 Python/Node/pnpm 绝对路径，缓存进 session；一个工具返回三条。模型不再靠猜或乱跑 shell 探测。

### P2 — 体验/扩展

19. **持久 shell**（bash/pwsh）——`cd` 与 exported 变量跨调用保留。落地要小心 Windows 管道/Proactor 行为，`sandbox/executor.py:8-19` 已经把坑写清楚了。

20. **`repeat-tool-reminder`**——与既有 breaker **互补**（breaker 管"失败重复"，reminder 管"动作重复"）。阈值 `[3,5,8]`；被 `exclude` 的调用既不递增也不重置计数（`todo_write` 应排除，否则记录动作会掩盖循环）。

21. **per-tool 字节预算 + spill**——把 `externalize.py` 的"全局 64KiB 字符"升级为按工具配置：`grep` 250 项、`glob` 100 路径、`bash` tail 8KiB、`web_fetch` headTail、`web_search` 8 来源。溢出写 spill，措辞照 DSH（含"用 read offset/limit 或 grep 该路径"的恢复指引）。

22. **多编辑 / 批量编辑工具**——当前 `edit_file` 每次一处（或 `replace_all`）。多处小改动要发 N 轮工具调用。DSH 也没有独立的多编辑工具（靠 PTC 打包），所以这项优先级低于上面。

23. **结构化输出**——`response_format` / `json_schema` 全仓无（grep `response_format|json_schema|structured` 只有散文与 dataclass 命中）。对 `contract_check` / `feature_brief` 这类"要求模型给固定形状"的场景有用。

24. **Office/文档链**——DSH 走的是"`load_workspace_dependencies` 给解释器 + `skill-office` 给 docx/pptx/xlsx 指令与脚本 + `libreoffice-kit` 做转换 + `office-to-pdf` 做预览"。code_agent 侧完全没有；若要支持，建议先做 `load_workspace_dependencies`（=#18）+ 一个 office skill，而不是自己写 docx/xlsx 代码。

25. **skill 命名收敛**——`load_skill` / `skill_list` / `skill_search` / `skill_install` 四件套 vs DSH 单个 `skill`。不必强行收敛（code_agent 的拆分有"技能商店"产品语义），但应在文档里明示分工，避免模型乱用。

---

## 6. code_agent 已强于 DSH 的部分（别在重构里弄丢）

1. **`ask_user_question` 的取消正确性**——`ask_tools.py:130` 显式 `raise` `CancelledError` 而不吞。注释写明真机踩过："吞掉会让用户按停止之后这一轮不停，直到耗完 600s 超时"。DSH 有同款语义，但这是极易写错的地方。
2. **`web_search`/`web_fetch` 的结构化注入防护**——`web_tools.py:67,164` 用 `wrap_untrusted` 把外部内容包成"数据块"；DSH 主要靠提示词措辞。结构化更稳。
3. **patch 台账 + 回滚硬规则**——`codeintel/service.py:246-250` 的"只有当前内容仍等于该次改动之后的内容才允许自动回滚"是 DSH 没有的，而且它正是 P0-b#8 观察策略的现成同源实现。
4. **失败熔断**（`breaker.py`）——DSH 只有 repeat-reminder，没有熔断。保留。
5. **路径硬边界 fail closed**——`_WorkspaceBound.resolve_write/resolve_read` 不依赖策略配置，比"依赖事件门闸"更难误配。
6. **`tool_search` 延迟加载**——`tools/defer.py` 的 `DeferredToolRegistry` + stub `_use_tool_search` 占位是 DSH 没有的。**但它只在 CLI 生效**（见 §3.1 发现 1），把它接到 API registry 上就能让 Web 会话也受益。
7. **代码智能工具族**——`code_map`/`code_features`/`code_search`/`code_graph`/`contract_check`/`acceptance_*`/`feature_brief` 是 DSH 完全没有的一套；这是 code_agent 的差异化资产。
8. **`edit_many`（M52）**——DSH 没有多编辑工具（它靠 PTC 把多步打包，见 `dsh-agent-tool-presentation`）。code-agent 的 `edit_many` 走**两段式**（整批内存推演 → 全通过才落盘），比"连发 N 次 `edit_file`"语义更严：校验失败一处都不写。这是少发 N-1 轮工具调用换来的，代价是"跨文件真事务做不到"必须如实说清（落盘中途失败会报哪些写了、哪些没写）。

---

## 7. 建议的落地顺序

```
第一波（P0-a，无架构改动，1 个 PR 内可完成）
  1 → 2 → 3 → 4 → 5 → 6/7                                    ✅ M41

第二波（P0-b，安全/正确性）
  #9 原子写  →  #8 FS 观察策略  →  #10 并行写租约              ✅ M42
  （先做原子写，因为观察策略的 replace 语义要建在原子写之上）

第三波（P1，性价比排序）
  #11 glob  →  #13 present  →  #18 workspace-deps                     ✅ M43
  #14 jobs                                                            ✅ M45
  #12 read_image                                                      ✅ M51
  #17 plan-mode  →  #15 goal
  →  #16 subagent/workflow/team                                      ⏳ 未做

第四波（P2）
  #20 repeat-reminder  →  #21 spill（按工具字节预算）                    ✅ M47 / M46
  #19 持久 shell                                                     ✅ M48
  #24 Office 链（技能 + 解释器；**预览/转换那一段未做**）                ✅ M49
  #22 多编辑（DSH 没有，属于额外能力）                                  ✅ M52
  #23 结构化输出 → #25 命名文档                                        ⏳ 未做
  （#6 逐工具超时框架已在第一波落地，不再是第四波的遗留项）
```

---

## 8. 未确认项（如需继续，先查这些）

1. **DSH `dsh-compaction-tool-result-pruner` 的默认阈值与裁剪算法**——README 只提到"标准 preset 8192 字符"，未给全局默认；要读 `src/index.ts`。
2. **`dsh-jobs-local` 的输出环保留量与 `pumpPollMs` 默认值**——决定后台 bash 的实时输出延迟。
3. **`dsh-tool-web` 的 `web_search` 是否返回摘要文本**——`dsh-web-search-deepseek/README.zh.md:65` 说"content 始终省略"，而 code_agent 的 `format_search` 会返回一段服务端摘要（`web_tools.py:47-49`）。语义可能不一致。
4. **PTC 模式的接入成本**——`dsh-agent-tool-presentation` README 自己都说 native 是首选；要不要跟进取决于 code_agent 的 schema token 成本实测。
5. **DSH `read` 的 `readMaxLineLength`(2000) 与 code_agent 的行长行为差异**——code_agent 只按 `max_bytes`/行数截断，超长单行会整行进上下文。

---

*本报告基于 DSH 0.2.0-rc.2 的随包 README 与 `E:\CH-agent` 工作区源码。DSH 端的实现细节以 `app.asar` 内 `lib/index.js` 为准（README 覆盖不到的部分已在 §8 列出）。*
