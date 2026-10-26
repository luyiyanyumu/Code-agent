"""内置工具：bash / read_file / write_file。

M6 安全加固：read/write 做路径边界校验（纵深防御）。
- write：硬性限制在会话工作区内（越界直接拒，不依赖策略）
- read ：允许工作区 + 系统内部 tool_results 目录（外部化产物的二次拉取，
  属系统产物非用户数据；用 tool_results_dir 判定）
即使某条权限策略漏配，工具本身也不放行越出上述白名单的读写。

M41 增补两件事（都对齐 DSH，见 docs/dsh-tool-parity.md）：

1. **写入原子化**（`code_agent/atomic_write.py`）：原来的 `path.write_text` 是
   "先截断再写"，中途挂掉会把一个好文件留成半截；现在走"同目录临时文件 +
   `os.replace`"，读者要么看到旧全文、要么看到新全文。新建走 `O_EXCL`，
   并发创建者不会被静默覆盖。
2. **读写观察**（`code_agent/tools/fs_observation.py`，对齐
   `dsh-fs-observation-policy`）：`read_file` 记下"读到的版本"，`write_file`
   覆盖已有文件前要求"本会话读过、且读完之后没变过"。`ctx.fs_observation`
   为 None 表示没挂这条策略，此时只保留原子写（策略本身在 DSH 里也是可插拔的）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_agent.config import skills_dir, tool_results_dir
from code_agent.sandbox.executor import run_shell
from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.tools.read_spec import read_window


class _WorkspaceBound:
    """路径硬边界校验（resolve 后判定，防符号链接/.. 穿越）。"""

    @staticmethod
    def _resolved(workspace: Path, raw: str) -> Path:
        if not raw:
            raise ToolError("path 不能为空")
        ws = Path(workspace).resolve()
        p = Path(raw)
        target = p if p.is_absolute() else (ws / p)
        return ws, target.resolve()

    @staticmethod
    def resolve_write(workspace: Path, raw: str) -> Path:
        ws, resolved = _WorkspaceBound._resolved(workspace, raw)
        try:
            resolved.relative_to(ws)
        except ValueError:
            raise ToolError(f"写入路径越出工作区（拒绝）: {resolved}") from None
        return resolved

    @staticmethod
    def resolve_read(workspace: Path, raw: str) -> Path:
        ws, resolved = _WorkspaceBound._resolved(workspace, raw)
        # 白名单：工作区 / 系统 tool_results（外部化二次拉取）/ 技能目录（只读，附带资源）
        allowed = [ws]
        try:
            allowed.append(Path(tool_results_dir()).resolve())
        except Exception:
            pass
        try:
            allowed.append(Path(skills_dir()).resolve())
        except Exception:
            pass
        for base in allowed:
            try:
                resolved.relative_to(base)
                return resolved
            except ValueError:
                continue
        raise ToolError(f"读取路径不在允许范围（拒绝）: {resolved}")


class BashTool(Tool):
    name = "bash"

    def __init__(self) -> None:
        # 描述里如实写明**实际用哪个 shell**：Windows 上是 Git Bash（装了就真的走 bash），
        # 没装则退回 cmd.exe——那种情况下 head/grep 这类写法必然失败，得提前告诉模型。
        from code_agent.sandbox.executor import shell_kind

        kind = shell_kind()
        self.description = (
            f"Execute a shell command in the session workspace (shell: {kind}). "
            "Subject to permission policy (deny patterns / approval). "
            "For long-running commands pass run_in_background=true to get a job id back "
            "immediately; then read it with job_output and stop it with job_kill."
        )
        self.description_zh = (
            f"在会话工作区执行 shell 命令（实际 shell：{kind}）。受权限策略约束（黑名单拦截 / 人工审批）。"
            "长命令用 run_in_background=true 立刻拿到 job id，再用 job_output 读、job_kill 停。"
        )

    description = "Execute a shell command in the session workspace."
    description_zh = "在会话工作区执行 shell 命令。受权限策略约束（黑名单拦截 / 人工审批）。"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {"type": "number", "description": "Optional timeout seconds (default 30, max 300)"},
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "Start the command as a background job and return its id immediately "
                    "(default false). Use it for long commands (installs, builds, test suites). "
                    "Background jobs have NO executor timeout — stop them with job_kill. "
                    "You will be notified in-session when the job finishes; do not poll it."
                ),
            },
        },
        "required": ["command"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        command = (arguments.get("command") or "").strip()
        if not command:
            raise ToolError("command 不能为空")
        if arguments.get("run_in_background"):
            return self._start_background(command, ctx)
        timeout = float(arguments.get("timeout") or 30.0)
        return await run_shell(command, cwd=ctx.workspace, timeout=min(timeout, 300.0))

    def _start_background(self, command: str, ctx: ToolContext) -> str:
        """把命令挂到会话的后台任务表上，立刻返回 id（M45）。

        两条纪律写在这里而不是靠调用方自觉：
        - **没有任务表就如实报错**，不假装接下了（同 M31 提问通道：没有通道就说清楚）；
        - **后台任务不设执行器超时**（对齐 DSH）：杀了它就是 `job_kill`，
          超时保护在这里没有意义——用户要的正是"它慢慢跑，我去干别的"。
        """
        import asyncio

        registry = (ctx.extra or {}).get("jobs")
        if registry is None:
            return (
                "[bash] 当前环境没有后台任务表（嵌入式 / 未接 SessionRuntime），"
                "run_in_background 不可用：请去掉这个参数前台执行，"
                "或把命令拆小、或加大 timeout（上限 300 秒）。"
            )

        kill_event = asyncio.Event()

        async def runner(job):
            from code_agent.jobs.registry import JobStopped

            def pump(chunk: bytes) -> None:
                job.append(chunk)

            text = await run_shell(
                command,
                cwd=ctx.workspace,
                timeout=None,            # 后台任务不设执行器超时
                on_chunk=pump,
                kill_event=kill_event,
            )
            if kill_event.is_set():
                # 必须显式报告"我是被停的"：run_shell 杀完进程是**正常返回**的，
                # 任务层若只看"有没有抛异常"，被杀停的任务会结算成 done ——
                # 模型于是读到一条"成功"的假象，而它明明被停了。
                raise JobStopped(text)
            return text

        job = registry.start(
            session_id=ctx.session_id,
            kind="bash",
            label=command[:120],
            runner=runner,
            kill=kill_event.set,         # 杀停 = 杀进程树（不是 cancel 协程，见 executor 文件头）
        )
        return (
            f"started background job {job.id}\n"
            f"命令：{command[:200]}\n"
            f"读输出：job_output(job_id=\"{job.id}\")（wait=true 可等它跑完，最多 300 秒）；"
            f"不需要了就 job_kill。\n"
            f"**不要轮询它**：任务结束时你会在这个会话里收到一条完成通知。"
        )


class BashPersistentTool(Tool):
    """持久 shell（M48，对齐 DSH 的 `dsh-tool-bash-persistent`）。

    与一次性 `bash` 的唯一区别：**同一个会话的多次调用共享一只 shell**，所以
    `cd` / `export` / 定义函数会留到下一次调用。用户要装一次环境跑五条命令时，
    不必再把它们拼成一条 `cd x && export A=B && …`（那种字符串很快就会变成
    没法读、也没法改的一坨）。

    刻意做成**独立工具**而不是 `bash` 的一个参数：两者的状态语义完全不同
    （一个每次全新、一个跨调用累积），写在同一段描述里，模型很容易在"我以为
    还在那个目录"上栽跟头。独立工具的描述可以把这件事说清楚。
    """

    name = "bash_persistent"
    description = (
        "Run a shell command in a PERSISTENT shell for this session: the working directory, "
        "exported environment variables and shell functions persist across calls. "
        "Use it for multi-step shell work (`cd`, `export`, then run). "
        "A call that times out KILLS the shell and its state is lost — the next call starts a "
        "fresh one. Requires bash (not available when the machine only has cmd.exe). "
        "Do not read from stdin in the command."
    )
    description_zh = (
        "在一个**持久** shell 里执行命令：工作目录、导出的环境变量、函数会在本会话的后续调用里保留。"
        "适合多步 shell 作业（先 cd / export 再跑）。"
        "超时会**杀掉这只 shell、状态一并消失**，下一次调用是全新的 shell。"
        "需要 bash（只有 cmd.exe 的机器上不可用）；命令里不要从 stdin 读。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run in the persistent shell"},
            "timeout": {"type": "number", "description": "Optional timeout seconds (default 30, max 300)"},
        },
        "required": ["command"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        command = (arguments.get("command") or "").strip()
        if not command:
            raise ToolError("command 不能为空")
        shells = (ctx.extra or {}).get("shells")
        if shells is None:
            return (
                "[bash_persistent] 当前环境没有持久 shell（嵌入式 / 未接 SessionRuntime）："
                "请用一次性 `bash`，并把每条命令需要的 cd / export 写在同一条命令里。"
            )
        timeout = min(float(arguments.get("timeout") or 30.0), 300.0)
        return await shells.run(command, cwd=ctx.workspace, timeout=timeout)


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file. Paths are relative to the workspace root, "
        "or inside the system tool_results dir (for externalized output). "
        "Pass offset/limit to read an exact line range (map coordinates are line-based). "
        "A limit without offset reads the first lines; omit both for the whole file."
    )
    description_zh = (
        "读取文本文件（带行号）。路径相对工作区根目录，或位于系统 tool_results 目录。"
        "用 offset/limit 精确读行区间——地图给的坐标就是行号。仅提供 limit 从第1行读；两者都省略才读全文。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (relative to workspace)"},
            "offset": {"type": "integer", "description": "Start line (1-based, default 1 when limit given). Omit both offset and limit for the whole file."},
            "limit": {"type": "integer", "description": "How many lines to read (default 200 when offset given); without offset, read the first N lines."},
            "max_bytes": {"type": "integer", "description": "Max bytes to read (default 65536)"},
        },
        "required": ["path"],
    }

    @staticmethod
    def _source_stat(path: Path) -> tuple[int, int] | None:
        try:
            observed = path.stat()
        except OSError:
            return None
        return observed.st_mtime_ns, observed.st_size

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        raw = arguments.get("path") or ""
        path = _WorkspaceBound.resolve_read(ctx.workspace, raw)
        if not path.is_file():
            # 记"确认缺失"（DSH 同款语义）：这让随后的一次 write 可以带防护地把它
            # 建出来（createIfAbsent），同时仍挡住覆盖**并发创建者**刚写进去的文件。
            if ctx.fs_observation is not None:
                ctx.fs_observation.record_absent(path)
            raise ToolError(f"文件不存在: {raw}")

        window = read_window(arguments)
        if window is None:
            from code_agent.tools.read_receipts import build_read_result

            max_bytes = int(arguments.get("max_bytes") or 65536)
            stat_before = self._source_stat(path)
            data = path.read_bytes()[:max_bytes]
            stat_after = self._source_stat(path)
            text = data.decode("utf-8", errors="replace")
            # 读到的是（截断过的）全文：记账时带上内容摘要，这是最强的观察强度。
            # 摘要按**原始字节**算（不是解码后的文本）——Windows 上 CRLF/LF 的
            # 通用换行翻译会让"文本摘要"自己跟自己不一致，正常写入会被误判成陈旧。
            if ctx.fs_observation is not None:
                if len(data) < max_bytes:
                    ctx.fs_observation.record_read(path, raw=data)
                else:
                    # 被 max_bytes 截断了 → 退化成 stat 指纹，免得把残缺摘要当全文
                    ctx.fs_observation.record_read(path, cheap=True)
            return build_read_result(
                text if text else "(空文件)", path=str(raw), raw=data,
                max_bytes=max_bytes, stat_before=stat_before, stat_after=stat_after,
            )

        from code_agent.tools.read_receipts import build_window_read_result

        first, limit = window
        total = 0
        lines: list[str] = []
        original_lines: list[str] = []
        stat_before = self._source_stat(path)
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, start=1):
                total = idx
                if idx < first:
                    continue
                if idx >= first + limit:
                    break
                lines.append(f"{idx}\t{line.rstrip()}")
                original_lines.append(line)
        stat_after = self._source_stat(path)
        # 窗口读没有全文可摘要 → 只记 stat 指纹（DSH 明确接受这一强度：
        # "授权依据是版本新鲜度，而非视图完整性"）。
        if ctx.fs_observation is not None:
            ctx.fs_observation.record_read(path, cheap=True)
        if not lines:
            return f"（第 {first} 行起没有内容；文件共约 {total} 行）"
        head = f"{path.name} 第 {first}-{first + len(lines) - 1} 行（每行前缀是真实行号）"
        return build_window_read_result(
            head + "\n" + "\n".join(lines), path=str(raw), text="".join(original_lines),
            offset=first, stat_before=stat_before, stat_after=stat_after,
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write text to a file (create or overwrite). "
        "Paths outside the workspace are rejected. "
        "Overwriting an existing file requires that this session has READ it first and that "
        "it has not changed since — otherwise the write is refused; read the file, then retry."
    )
    description_zh = (
        "写入文本文件（新建或覆盖）。工作区之外的路径会被直接拒绝。"
        "覆盖已有文件前必须先在本会话读过它、且读完之后没被改过——否则拒绝写入。"
    )
    #: 参与同一轮内的写租约（见 tools/registry.py）：两个写同一文件的调用必须串行。
    writes_path = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (relative to workspace)"},
            "content": {"type": "string", "description": "Text content to write"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        from code_agent.atomic_write import atomic_create, atomic_replace

        raw = arguments.get("path") or ""
        path = _WorkspaceBound.resolve_write(ctx.workspace, raw)
        content = arguments.get("content") or ""

        # 观察门禁（对齐 dsh-fs-observation-policy）：覆盖别人写过的文件之前，
        # 本会话必须先读过它、且它自那之后没变过。ctx.fs_observation 为 None 表示
        # 没挂这条策略（程序化直调），此时跳过——但下面的原子写**永远**生效。
        obs = ctx.fs_observation
        if obs is not None:
            refusal = obs.check_write(path)
            if refusal:
                raise ToolError(refusal)

        if path.exists():
            atomic_replace(path, content)
        elif not atomic_create(path, content):
            # O_EXCL 判定"这次创建没抢到"：说明有并发创建者刚写进去。
            # 绝不覆盖它——覆盖掉的是**别人**的工作，而且我们没见过那份内容。
            if obs is not None:
                obs.record_absent(path)
            raise ToolError(
                f"写入失败: {raw} 在本次调用期间被别的会话/进程创建了（拒绝覆盖）。"
                "先用 read_file 读它，再决定怎么改。"
            )

        if obs is not None:
            # 写完读一遍磁盘，记下**真实落盘版本**（不是我们以为写进去的那串）：
            # 文本模式会做换行转换，用内存里的字符串算摘要会和磁盘对不上。
            obs.record_read(path)
        return f"已写入 {path}（{len(content)} 字符）"
