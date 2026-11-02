"""Run the locally built, source-merged Harness driver.

Only framed model/tool requests cross stdin/stdout. The child has no provider
credentials, desktop profile, shell tools or network model adapter. Source and
its built runtime ship together; preparation never installs npm dependencies.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Awaitable, Callable

_FRAME_LIMIT = 32 * 1024 * 1024
_CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_prepared: dict[tuple, tuple[str, Path]] = {}


class HarnessError(RuntimeError):
    """The merged driver could not initialize or complete its protocol."""


def _child_env() -> dict[str, str]:
    # The model and tool adapters live in Python. Do not forward service keys,
    # npm tokens, proxy credentials, NODE_OPTIONS or desktop discovery tokens.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
               "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _node() -> str:
    configured = os.environ.get("CODE_AGENT_NODE")
    node = configured or shutil.which("node")
    if not node or (configured and not Path(configured).is_absolute()):
        raise HarnessError("DeepSeek Harness 需要 Node.js 22.14+；请安装后重试。")
    return str(node)


async def _command(*args: str, cwd: Path | None = None, timeout: float = 300) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=_child_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        creationflags=_CREATE_FLAGS,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        return process.returncode or 0, output.decode("utf-8", errors="replace")[-12000:]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def _validate_node(version: str):
    try:
        major, minor = (int(value) for value in version.strip().lstrip("v").split(".")[:2])
    except ValueError as exc:
        raise HarnessError("无法识别 Node.js 版本，官方 Harness 需要 Node.js 22.14+。") from exc
    if major < 22 or major == 22 and minor < 14:
        raise HarnessError("官方 DeepSeek Harness 需要 Node.js 22.14+，请升级 Node.js。")


def _source_snapshot(source: Path) -> tuple:
    """Track the complete shipped source tree, including newly added files.

    The Node verifier owns content hashes and build provenance. These stat keys
    only avoid launching it again for an unchanged runtime in this process.
    """
    required = {"runner.mjs", "core.mjs", "core-build.json", "package.json"}
    try:
        entries = {}

        def scan(directory, prefix=""):
            # DirEntry reuses directory-enumeration metadata on Windows. Avoid
            # resolving and stat'ing every path several times on each prompt.
            with os.scandir(directory) as items:
                for entry in items:
                    if not prefix and entry.name in {"node_modules", "tests"}:
                        continue
                    if not prefix and not (
                        entry.name in {"src", "vendor"} or entry.name.startswith("LICENSE")
                        or entry.name.endswith((".mjs", ".json", ".md"))
                    ):
                        continue
                    name = prefix + entry.name
                    if entry.is_symlink():
                        raise ValueError("source assets must not be symbolic links: " + name)
                    if entry.is_dir(follow_symlinks=False):
                        scan(entry.path, name + "/")
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        entries[name] = (stat.st_mtime_ns, stat.st_size)

        scan(source)
        if not required.issubset(entries):
            raise HarnessError("安装包缺少合并后的 Harness 源码或构建产物；源码目录请运行 npm run build:harness，安装包请重新安装。")
        manifest = json.loads((source / "core-build.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("invalid build manifest")
        inputs = manifest["inputs"]
        if manifest.get("format") != 1 or not isinstance(inputs, dict) or not inputs:
            raise ValueError("unsupported build manifest")
        for name in inputs:
            if not name or "\\" in name or Path(name).drive or name.startswith("/") or any(
                part in {"", ".", ".."} for part in name.split("/")
            ):
                raise ValueError("invalid source path")
            if name not in entries:
                raise ValueError("missing source: " + name)
        return tuple((name, *metadata) for name, metadata in sorted(entries.items()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HarnessError("Harness 源码清单不完整或无效，请重新构建或重新安装：" + str(exc)) from exc


async def prepare_runtime() -> tuple[str, Path]:
    node = _node()
    configured = os.environ.get("CODE_AGENT_HARNESS_NODE_DIR")
    source = Path(configured) if configured else Path(__file__).parent / "node"
    if configured and not source.is_absolute():
        raise HarnessError("CODE_AGENT_HARNESS_NODE_DIR 必须是绝对路径。")
    source = source.resolve()
    snapshot = _source_snapshot(source)
    node_stat = Path(node).stat()
    key = (node, str(source), node_stat.st_mtime_ns, node_stat.st_size, snapshot)
    if key in _prepared:
        return _prepared[key]
    code, output = await _command(node, str(source / "runner.mjs"), "--check", timeout=20)
    if code:
        raise HarnessError("合并后的 Harness 内核校验失败；请运行 npm run build:harness 或重新安装：" + output)
    try:
        _validate_node(json.loads(output)["node"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError("Harness 内核返回了无效的准备结果。") from exc
    result = (node, source / "runner.mjs")
    _prepared.clear()
    _prepared[key] = result
    return result


async def run_harness(
    start: dict[str, Any],
    request: Callable[[str, dict[str, Any]], Awaitable[Any]],
) -> dict[str, Any]:
    """Service official driver callbacks; this transport makes no agent decisions."""
    node, runner = await prepare_runtime()
    process = await asyncio.create_subprocess_exec(
        node, str(runner), env=_child_env(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_FRAME_LIMIT, creationflags=_CREATE_FLAGS,
    )
    assert process.stdin and process.stdout and process.stderr
    pending: set[asyncio.Task] = set()
    write_lock = asyncio.Lock()
    errors: list[str] = []
    terminal: dict[str, Any] | None = None
    completed = False

    async def read_stderr():
        while chunk := await process.stderr.read(4096):
            errors.append(chunk.decode("utf-8", errors="replace"))
            if sum(map(len, errors)) > 12000:
                del errors[:-2]

    async def send(frame):
        body = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        if len(body) > _FRAME_LIMIT:
            raise HarnessError("Harness 协议帧超出大小限制。")
        async with write_lock:
            process.stdin.write(body)
            await process.stdin.drain()

    callback_failure = asyncio.get_running_loop().create_future()

    async def serve(frame):
        try:
            result = await request(frame["method"], frame)
            await send({"type": "response", "id": frame["id"], "result": result})
        except BaseException as exc:
            # Budget/lifecycle hooks may use BaseException, and a provider may
            # independently cancel its request. Wake the transport in both cases
            # rather than leaving the official driver waiting for a lost reply.
            if not callback_failure.done():
                callback_failure.set_result(exc)

    stderr_task = asyncio.create_task(read_stderr())
    try:
        await send({**start, "type": "start"})
        while True:
            read_task = asyncio.create_task(process.stdout.readline())
            try:
                ready, _ = await asyncio.wait(
                    (read_task, callback_failure), return_when=asyncio.FIRST_COMPLETED,
                )
                if callback_failure in ready:
                    raise callback_failure.result()
                line = read_task.result()
            finally:
                if not read_task.done():
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
            if not line:
                break
            try:
                frame = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise HarnessError("官方 Harness 返回了无效协议帧。") from exc
            if not isinstance(frame, dict):
                raise HarnessError("官方 Harness 协议帧不是对象。")
            kind = frame.get("type")
            if kind == "request" and frame.get("method") in ("model", "tool") and isinstance(frame.get("id"), str):
                task = asyncio.create_task(serve(frame))
                pending.add(task)
                task.add_done_callback(pending.discard)
            elif kind == "done":
                terminal = frame
                break
            elif kind == "error":
                if callback_failure.done():
                    raise callback_failure.result()
                raise HarnessError(str(frame.get("message") or frame.get("error") or "Harness 执行失败"))
            else:
                raise HarnessError("官方 Harness 返回了不支持的协议消息。")
        if terminal is None:
            if callback_failure.done():
                raise callback_failure.result()
            raise HarnessError("官方 Harness 未返回终态：" + "".join(errors)[-2000:])
        if callback_failure.done():
            raise callback_failure.result()
        completed = True
        return terminal
    finally:
        for task in list(pending):
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # EOF cooperatively disposes the Cordis scope; kill is the bounded
        # fallback for an unresponsive child. Python tool tasks are drained first.
        if not process.stdin.is_closing():
            process.stdin.close()
        forced = False
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                forced = True
                process.kill()
                await process.wait()
        await stderr_task
        if completed and (forced or process.returncode != 0):
            raise HarnessError("Harness 未正常退出，执行结果未确认：" + "".join(errors)[-2000:])
