# Windows 完整安装包

Windows 安装包包含 Code-agent 桌面应用、前后端和所需运行环境。安装完成后，通过桌面或开始菜单中的 **Code-agent** 快捷方式直接进入 IDE 工作台，不需要另外打开启动器、浏览器或源码目录，也不需要预先安装 Node.js、Python、Git 或 WebView2。

当前生成的安装包为 `dist/installers/Code-agent-Setup-0.10.2-win-x64.exe`。这是本地构建产物，尚未上传 GitHub Releases 或发布到 npm；公共 npm 的 `@latest` 仍是之前发布的版本。

## 安装与启动

1. 双击 `Code-agent-Setup-0.10.2-win-x64.exe`。
2. 选择安装位置，点击“开始安装”。默认仅为当前用户安装，无需管理员权限。
3. 安装完成后点击“打开 Code-agent”，或使用桌面、开始菜单中的 **Code-agent**。应用会在后台准备服务，然后直接显示编辑器、项目和 AI 助手。
4. 在工作台的“设置 → 模型与连接”中填写自己的模型服务。安装包不包含任何个人模型密钥、会话、数据库或项目文件。

系统支持 **Windows 10 / 11 x64**，使用系统 .NET Framework 4.8。前端、后端和运行环境均来自安装目录；安装后启动不会再运行 npm 或 pip 下载应用依赖。语言服务器、标准库与分析所需运行环境随安装包提供，打开对应文件后自动连接，缓存保存在用户数据目录。项目自身的第三方依赖、目标平台 SDK、指定版本构建工具和调试适配器仍按项目要求准备。

| 内容 | 安装包中的位置 |
| --- | --- |
| 桌面应用 | `Code-agent.exe` |
| 预构建前端 | `web/dist/` |
| Python 后端、技能与智能体内核 | `code_agent/` |
| Node.js、npm 与 npx | `runtimes/node/` |
| Python 与已安装的后端依赖 | `runtimes/python/` |
| WebView2 固定运行时 | `runtimes/webview2/` |
| Git | `runtimes/git/` |
| Java 21（代码分析） | `runtimes/java/` |
| Eclipse JDT 语言服务 | `runtimes/jdtls/` |
| Lombok IDE 适配层 | `runtimes/lombok/` |
| 其他语言服务、标准库及配套运行环境 | `runtimes/language-servers/` |
| 可迁移的服务命令与初始化配置 | `runtimes/language-tools.json` |

## 内置代码分析

安装包覆盖现有目录中的全部 **28 个服务**，其中 Python 可选择 Pyright、pylsp 或 Ruff。默认按文件语言选择服务，无需修改系统 PATH 或手动填写命令。

| 语言 / 文件 | 内置服务 |
| --- | --- |
| Java | Eclipse JDT，配套 Java 21 与 Lombok IDE agent |
| Python | Pyright、python-lsp-server、Ruff |
| JavaScript / TypeScript、Vue | TypeScript Language Server、Vue Language Server |
| HTML、CSS、JSON、YAML | VS Code HTML / CSS / JSON 服务、YAML Language Server |
| JavaScript / TypeScript 代码检查 | ESLint，支持异步诊断刷新 |
| Bash、Dockerfile、SQL | Bash Language Server、Dockerfile Language Server、SQL Language Server；另含 ShellCheck 与 shfmt |
| C / C++、Go、Rust、C# | clangd、gopls、rust-analyzer、csharp-ls，配套标准库及 SDK |
| Dart、Kotlin、Lua、Ruby | Dart Language Server、Kotlin Language Server、LuaLS、Solargraph |
| Swift、PHP、Zig | SourceKit-LSP、PHPantom、ZLS |
| Terraform、Markdown、TOML | Terraform LS、Marksman、Taplo |

服务通过应用相对路径加载；升级或更换安装位置后仍可用。显式配置的自定义服务优先于内置命令。离线验证使用独立用户目录与仅含 Windows 系统目录的 PATH，检查真实悬停、文档符号和错误诊断；支持实时诊断的服务另检查修复后诊断清除。Rust 的编译检查按其保存文件的工作方式验证。

代码分析环境与项目构建环境分别管理。内置服务能够分析标准库和本地源码；例如 Java 项目的 Maven / Gradle 依赖、Python 项目的专用虚拟环境、前端项目的 npm 依赖和 Swift 的平台构建工具，仍由项目声明。安装包不会修改项目依赖版本或自动运行项目安装脚本。

工作台底部“应用状态”和设置中的“关于与诊断”提供运行状态、后台重启、日志及检查更新。正常退出先确认未保存内容，再停止本次应用启动的服务；复用的已有服务会保留。任务仍在运行时可取消退出，或明确选择关闭窗口并保留后台运行。

## 程序与用户数据

默认程序位置为 `%LOCALAPPDATA%\Programs\Code-agent`；用户数据独立保存在 `%LOCALAPPDATA%\Code-agent\data`。运行日志位于数据目录的 `logs/`，会话、模型配置和工作区数据不会写入应用安装目录。内嵌窗口缓存位于 `%LOCALAPPDATA%\Code-agent\Launcher`。

安装目录包含启动器、程序文件及运行环境，安装后不再读取构建机器的虚拟环境或项目路径。Python 通过相对路径加载包；构建时生成的 Python 命令包装脚本不进入安装包。

## 更新与卸载

安装版检查更新时查询官方 GitHub Releases。发布新版后，按提示下载并运行新版 Windows 安装包；安装版不会调用 npm 更新自己。当前安装包尚未公开发布，检查失败或没有安装包资源时会显示实际原因。

升级前退出 Code-agent。若之前明确保留了后台服务，需先安全停止它们。安装器仅替换有 Code-agent 安装清单的应用目录，不强行结束进程；旧安装目录会作为备份保留。用户数据目录保持不变。

检测到旧版窗口或后台占用时，安装器会显示“停止后台并重试”。此操作请求应用正常退出，使用安装包内置的维护脚本和旧安装的运行环境，核对进程归属及任务状态后停止服务，再继续安装。未保存内容、执行中任务或无法确认归属的进程会阻止继续，不会强制结束。

可通过 Windows“已安装的应用”或安装目录中的 `Code-agent Uninstall.exe` 卸载。卸载只移除安装清单中的应用文件、所属快捷方式和卸载注册信息，保留会话、配置、项目及其他用户文件。

## 从源码构建

构建机器需准备项目开发依赖、Node.js、Python 和 .NET Framework 编译器；Ruby 原生扩展还需要 MSYS2 UCRT64 的 GCC 与 make，可通过 `build-windows-payload.ps1 -RubyBuildTools <MSYS2 目录>` 指定。这些是构建机依赖，不要求安装包用户安装。首次构建需要联网下载经过校验的运行环境。终端中执行：

```powershell
npm run build:installer
```

构建入口为 `scripts/build-windows-release.ps1`，依次生成独立运行目录并封装安装包。语言准备入口为 `scripts/prepare-windows-languages.ps1`，在构建时下载固定版本并验证校验值，按 Node / Python、原生服务、SDK、平台工具分组准备，再合并服务注册表。产物写入 `dist/installers/`，同时生成 SHA-256 校验文件和构建元数据。下载缓存和打包临时文件位于 `.tmp/`，不进入安装包。

内置 Python 的应用根目录必须排在 `Lib\site-packages` 前，避免依赖目录中的旧应用 wheel 遮蔽新源码。`python313._pth` 使用 Windows 相对路径 `..\..`；构建验证会检查 `code_agent`、服务、Harness 和文件夹选择器模块的实际 `__file__`，而不只检查能否导入。

已有完整 staging 目录时，可单独封装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-windows-installer.ps1 -PayloadDirectory "E:/build/code-agent-payload"
```

安装器支持隔离验证参数 `--silent --dir <绝对目录> --no-launch --result-file <结果文件>`。卸载器支持 `--silent --result-file <结果文件>`；卸载通过临时 helper 完成，自动化应等待结果文件写入后再检查目录。

仅调整安装向导界面时，可传入 `-ReusePayloadArchive <之前构建的 payload.zip>`。构建会验证归档与 staging 目录的文件清单和逐文件 SHA-256，再重新编译安装器，避免重复压缩运行环境。归档版本或内容不匹配时会拒绝复用。

完整语言包达到 1 GiB 后，构建器自动将 ZIP 附加到安装程序，并写入带 SHA-256 的 64 位边界信息，避开 PE 资源的大小限制。安装器按流校验和解压，仍只分发一个 EXE。较小的安装包继续兼容原有内嵌资源格式。

界面检查可执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-installer-ui.ps1`。该检查仅渲染真实 WinForms 控件，覆盖 100%、150%、200% 的布局以及准备、安装中、完成、失败四种状态，不执行安装操作；截图和布局报告位于 `.tmp/installer-ui-verification/`。不同缩放的布局由测试夹具模拟，不会修改 Windows 显示设置。

异步交互检查可执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-installer-async.ps1`。它在真实窗口事件循环中启动测试任务，覆盖大量进度、失败恢复、重试和迟到进度，报告位于 `.tmp/installer-async-verification/`；不写入安装目录或注册表。安装向导合并后台进度，每 100 毫秒读取最新状态，并以后台任务的实际结果切换完成或失败，避免解压数据块的密集通知阻塞收尾。

真实 GUI 安装检查使用 `scripts/verify-installer-installation.ps1 -Installer <最终安装包> -Target <全新隔离目录> -OutputDirectory <验证输出目录>`。Target 必须是 OutputDirectory 的同级新目录。测试直接加载最终安装包程序集，通过真实窗口按钮与后台任务安装完整载荷；注册表仅在测试进程内重定向，快捷方式写入验证目录，不停止现有服务。它会检查完成页、按钮、安装清单和登记，并保留截图、报告及安装文件。该检查不会启动测试安装版，也不会自动卸载或清理它。

## 已完成的安装验证

实际安装到含中文和空格的独立目录，清除外部开发与模型环境变量、仅保留系统 PATH 后，内置 Python、Node、npm、Git 均可运行。前后端自动启动，真实 WebSocket 代理和离线 Harness 完成 Shell 工具调用；分别重启和停止通过。

原生窗口实际加载安装目录中的 Fixed WebView2，直接进入前端工作台，工作台子窗口数量为零。载荷清单和逐文件校验值记录在安装包元数据中，快捷方式指向 `Code-agent.exe`；运行状态、原生日志桥、后端重启及正常退出停止本次服务均已验证。既有安装升级与卸载验证覆盖用户数据保留。安装包排除了构建机器的 Python 命令包装器，不包含个人配置。

复现完整流程：`node scripts/verify_windows_installer.mjs <安装包路径> <验证输出目录>`。该验证使用隔离数据目录和离线模型，不消耗模型 API。相关 npm 回归与 Windows 进程归属检查也已通过。

仅验证已安装的桌面应用：`node scripts/verify_desktop_ide.mjs <安装目录> <验证输出目录>`。它使用隔离数据和空闲端口，在真实内嵌工作台中读取状态与日志、重启后端并正常退出，保存截图和报告；不会操作其他项目的服务。

关闭回归：`node scripts/verify_desktop_close_with_picker.mjs <安装目录> <验证输出目录>`。它通过真实代理和 API 检查请求断开后选择器回收，再在选择器仍打开时退出应用，核对主窗口未被禁用、选择器与本次服务全部消失。`scripts/verify-desktop-close.ps1` 另覆盖脚本或 HTTP 永不返回、取消后重试和重复点击关闭等故障场景。

正常退出耗时：`node scripts/verify_desktop_shutdown.mjs <安装目录> <报告目录> 3`。记录三次实际原生进程退出时间，并独立核验控制器、后端与前端进程和监听端口均已清理。退出保护保持不变，Windows 进程检查改用内置标准库直接读取系统接口。

模型与导图验收：`node scripts/verify_desktop_models.mjs <安装目录> <报告目录>`。在隔离离线项目中通过真实桌面界面切换会话模型，核对全局配置保持不变、重载后选择仍在、`/model` 不发送提示词，检查提供方编辑与导图恢复，最后正常退出。此流程不使用用户的密钥、项目或会话，也不调用付费模型。
