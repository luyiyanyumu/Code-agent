"""Bounded Git inspection without exposing a shell or arbitrary Git arguments."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from code_agent.tools.base import Tool, ToolContext, ToolError

MAX_OUTPUT_BYTES = 64 * 1024
QUERY_TIMEOUT_SECONDS = 20
_REVISION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}\Z")
_FORMAT = "%H%nAuthor: %an%nDate: %aI%n%n%B"


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _git_executable(root: Path) -> tuple[str, str]:
    """Search explicit absolute PATH entries; never execute a workspace wrapper."""
    directories: list[str] = []
    for item in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(item.strip('"'))
        if not item or not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if not _inside(resolved, root):
            directories.append(str(resolved))
    name = "git.exe" if os.name == "nt" else "git"
    for directory in directories:
        candidate = Path(directory) / name
        try:
            resolved = candidate.resolve()
            if _inside(resolved, root) or not resolved.is_file():
                continue
            if os.name != "nt" and not os.access(resolved, os.X_OK):
                continue
            return str(resolved), os.pathsep.join(directories)
        except (OSError, RuntimeError):
            continue
    raise ToolError("Git executable not found outside the workspace; install Git on the host.")


def _environment(safe_path: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update({
        "PATH": safe_path,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_PAGER": "",
        "LC_ALL": "C",
    })
    return env


_CONFIG = [
    "--no-pager", "--no-replace-objects",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.preloadindex=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", f"core.attributesFile={os.devnull}",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "submodule.recurse=false",
    "-c", "protocol.allow=never",
    "-c", "color.ui=false",
    "-c", "log.showSignature=false",
]


async def _run_git(
    executable: str, root: Path, args: list[str], env: dict[str, str],
) -> tuple[int, str, bool]:
    process = await asyncio.create_subprocess_exec(
        executable, *_CONFIG, *args, cwd=str(root), env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output = bytearray()
    truncated = False
    try:
        assert process.stdout is not None
        while chunk := await process.stdout.read(8192):
            remaining = MAX_OUTPUT_BYTES - len(output)
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
                break
        if truncated:
            await _stop_process(process)
        else:
            await process.wait()
    finally:
        if process.returncode is None:
            await _stop_process(process)
    return process.returncode, output.decode("utf-8", errors="replace"), truncated


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    # Drain after terminating: Process.wait alone can hang when its pipe buffer
    # is full, even after the child exits. Never accumulate discarded output.
    if process.stdout is not None:
        while await process.stdout.read(8192):
            pass
    await process.wait()


class GitReadTool(Tool):
    name = "git_read"
    description = (
        "Read Git status, diffs and commit history in the current workspace without a shell. "
        "Operations: status; diff (scope unstaged/staged/head); log (limit 1-50); "
        "show (one commit, defaults to HEAD). Optional paths are literal workspace-relative paths. "
        "revision accepts a branch/tag/commit name, not Git expressions or command arguments. "
        "This tool cannot stage, commit, fetch, switch branches, run hooks or execute arbitrary commands. "
        "Use read_file/list_dir/grep for source and logs; delegate builds and tests to an execution agent."
    )
    description_zh = "只读核查当前项目的 Git 状态、差异和提交历史，不启动 shell，不修改索引或工作区。"
    timeout = QUERY_TIMEOUT_SECONDS + 5
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["status", "diff", "log", "show"]},
            "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "scope": {"type": "string", "enum": ["unstaged", "staged", "head"]},
            "revision": {"type": "string", "description": "Commit hash, branch or tag; log/show only."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["operation"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        if not isinstance(arguments, dict) or set(arguments) - set(self.parameters["properties"]):
            raise ToolError("Only the documented git_read parameters are accepted.")
        operation = arguments.get("operation")
        if operation not in ("status", "diff", "log", "show"):
            raise ToolError("operation must be status, diff, log or show; write operations are unavailable.")
        if "scope" in arguments and operation != "diff":
            raise ToolError("scope is only supported for diff.")
        if "revision" in arguments and operation not in ("log", "show"):
            raise ToolError("revision is only supported for log/show.")
        if "limit" in arguments and operation != "log":
            raise ToolError("limit is only supported for log.")
        scope = arguments.get("scope", "unstaged")
        if scope not in ("unstaged", "staged", "head"):
            raise ToolError("scope must be unstaged, staged or head.")
        limit = arguments.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ToolError("limit must be an integer between 1 and 50.")
        revision = arguments.get("revision", "HEAD")
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ToolError("revision must be a plain commit hash, branch or tag name.")

        root = Path(ctx.workspace).resolve()
        paths = arguments.get("paths", [])
        if not isinstance(paths, list) or len(paths) > 20:
            raise ToolError("paths must contain at most 20 literal workspace-relative paths.")
        safe_paths = []
        for raw in paths:
            if not isinstance(raw, str) or not raw or "\x00" in raw:
                raise ToolError("Each path must be a nonempty string without NUL bytes.")
            path = Path(raw)
            if path.is_absolute() or path.drive or raw.startswith(("/", "\\")):
                raise ToolError("Git paths must be relative to the workspace.")
            try:
                target = (root / path).resolve()
                relative = target.relative_to(root)
            except (ValueError, OSError, RuntimeError):
                raise ToolError(f"Git path is outside the workspace: {raw}") from None
            if any(part.lower() == ".git" for part in relative.parts):
                raise ToolError("Git metadata paths are not supported.")
            safe_paths.append(relative.as_posix())

        executable, safe_path = _git_executable(root)
        env = _environment(safe_path)
        try:
            async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
                return await self._query(executable, root, env, operation, safe_paths, scope, revision, limit)
        except TimeoutError:
            raise ToolError(f"Git inspection timed out after {QUERY_TIMEOUT_SECONDS} seconds; narrow paths.") from None
        except OSError as exc:
            raise ToolError(f"Git inspection could not start: {exc}") from None

    async def _query(
        self, executable: str, root: Path, env: dict[str, str], operation: str,
        paths: list[str], scope: str, revision: str, limit: int,
    ) -> str:
        code, output, truncated = await _run_git(executable, root, ["rev-parse", "--show-toplevel"], env)
        if code or truncated:
            raise ToolError("The current workspace is not a readable Git worktree.")
        if Path(output.strip()).resolve() != root:
            raise ToolError("Git repository root must match the workspace; inspecting a parent repository is denied.")

        # Even `diff --no-ext-diff --no-textconv` may run clean/process filters.
        # Neutralize all configured filters, including ones from local includes.
        code, output, truncated = await _run_git(
            executable, root, ["config", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process|required)$"], env,
        )
        if truncated or code not in (0, 1):
            raise ToolError("Cannot safely inspect repository filter configuration.")
        keys = [key for key in output.split("\x00") if key]
        if len(keys) > 128:
            raise ToolError("Too many Git filter settings for bounded read-only inspection.")
        config: list[str] = []
        for key in keys:
            if not re.fullmatch(r"filter\..+\.(clean|process|required)", key):
                raise ToolError("Unsupported Git filter configuration.")
            config.extend(["-c", f"{key}={'false' if key.endswith('.required') else ''}"])

        if operation in ("log", "show"):
            code, output, truncated = await _run_git(
                executable, root, [*config, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"], env,
            )
            if code or truncated or not re.fullmatch(r"[0-9a-f]{40,64}\s*", output):
                raise ToolError("revision does not identify a readable commit in this repository.")
            revision = output.strip()

        if operation == "status":
            args = ["status", "--porcelain=v1", "--branch", "--untracked-files=normal", "--ignore-submodules=all"]
        elif operation == "diff":
            args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--ignore-submodules=all", "--unified=3"]
            if scope == "staged":
                args.append("--cached")
            elif scope == "head":
                args.append("HEAD")
        elif operation == "log":
            args = ["log", "--no-decorate", "--no-show-signature", f"--format={_FORMAT}", f"--max-count={limit}", revision]
        else:
            args = ["show", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--ignore-submodules=all", "--no-show-signature", f"--format={_FORMAT}", revision]
        code, output, truncated = await _run_git(executable, root, [*config, *args, "--", *paths], env)
        if code and not truncated:
            raise ToolError(f"Git {operation} failed (exit {code}): {output.strip()}")
        result = {"operation": operation, "workspace": str(root), "output": output, "truncated": truncated}
        if truncated:
            result["hint"] = f"Output exceeded {MAX_OUTPUT_BYTES} bytes and the query was stopped. Narrow paths or lower log limit."
        return json.dumps(result, ensure_ascii=False)
