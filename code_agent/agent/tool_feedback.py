"""Bounded observations of rejected calls, never permission decisions.

The caller must present ``context`` as untrusted tool-result data. Nothing here
executes a command, reads a path, caches an authorization, or blocks a new call.
Create a fresh instance for each user run; normal permissions remain authoritative.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import posixpath
import re
import shlex
from typing import Any, Iterable


_SHELL = {"bash", "bash_persistent", "shell", "exec_command"}
_FAILED = re.compile(r"^\s*(?:\[permission denied\]|\[tool error\])", re.I)
_BOUNDARY = re.compile(
    r"\[task scope\]|拒绝|不在允许范围|越出工作区|超出.*范围|禁止|只允许|仅允许|"
    r"(?:outside|out of|outside of) (?:the )?(?:workspace|allowed|permitted)|"
    r"(?:path|permission|access).{0,35}(?:denied|not allowed)|\bforbidden\b", re.I,
)
_SECRET = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret|"
    r"authorization)\b[\"']?\s*(?:=|:|\s)\s*)(?:Bearer\s+)?"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)|"
    r"\bBearer\s+[^\s,;]+|\bsk-[A-Za-z0-9_-]{8,}|https?://[^/\s]+@"
)
_PATH_TOKEN = re.compile(r"(?<![\w])(?:[A-Za-z]:[/\\]|\.\.?[/\\])[^\s\"'()<>|;&]+|"
                         r"(?<![\w])(?:[\w.-]+[/\\])+[\w.-]+")
_SAFE_PATH = re.compile(r"^(?:[A-Za-z]:)?[\w ./\\:@+-]+$", re.UNICODE)
_SAFE_PYTEST_FLAGS = {"-q", "-v", "-vv", "-x", "--disable-warnings", "--strict-markers",
                      "--strict-config", "--tb=short", "--tb=line", "--tb=no", "--tb=long"}


def _clean(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", _SECRET.sub("[redacted]", value)).strip()[:limit]


@dataclass
class _Refusal:
    tool: str
    target: str
    category: str
    reason: str
    count: int = 1


class ToolFeedback:
    """Remember recent actual refusals without treating them as permanent policy."""

    MAX_ENTRIES = 8
    MAX_ENTRY_CHARS = 200

    def __init__(self, workspace: Path | str | None, *, available_tools: Iterable[str] = ()):
        self.workspace = str(workspace or "").replace("\\", "/").rstrip("/")
        self.available_tools = frozenset(available_tools)
        self._windows = bool(re.match(r"^[A-Za-z]:/", self.workspace))
        self._refusals: OrderedDict[tuple[str, str, str], _Refusal] = OrderedDict()
        self._validation_example = ""

    def _path(self, raw: str) -> str:
        raw = raw.replace("\\", "/")
        absolute = raw.startswith("/") or bool(re.match(r"^[A-Za-z]:/", raw))
        path = posixpath.normpath(raw if absolute else posixpath.join(self.workspace, raw))
        if self._windows or re.match(r"^[A-Za-z]:/", path):
            path = path.casefold()
        return path

    def _display_target(self, target: str) -> str:
        base = self.workspace.casefold() if self._windows else self.workspace
        if base and target.startswith(base + "/"):
            target = target[len(base) + 1:]
        target = _clean(target, 1000)
        return target if len(target) <= 75 else "…" + target[-74:]

    def _targets(self, name: str, args: dict[str, Any]) -> tuple[str, ...]:
        paths = [args.get("path") or args.get("file_path")]
        if name == "edit_many" and isinstance(args.get("edits"), list):
            paths = [edit.get("path") for edit in args["edits"] if isinstance(edit, dict)]
        targets = tuple(dict.fromkeys(self._path(str(p)) for p in paths if isinstance(p, str) and p))
        if targets:
            return targets[:8]
        if name in _SHELL:
            command = str(args.get("command") or args.get("cmd") or "")
            # Literal paths help retain repeated shell refusals across spelling
            # changes; opaque code/commands are represented only by a digest.
            matches = _PATH_TOKEN.findall(command)
            if matches:
                return (self._path(matches[-1]),)
            digest = hashlib.blake2b(command.encode("utf-8"), digest_size=6).hexdigest()
            return ("command:" + digest,)
        return ("(unspecified target)",)

    @staticmethod
    def _category(reason: str) -> str:
        if re.search(r"不在允许范围|越出工作区|outside.*(?:workspace|allowed)|路径.*拒绝", reason, re.I):
            return "path_boundary"
        if re.search(r"已经读取|重复.*读|只补读|same.*read|already.*read", reason, re.I):
            return "read_reuse"
        if re.search(r"read_file[/、或 ]+grep|原生.*(?:读取|定位)|native.*(?:read|search)", reason, re.I):
            return "native_read"
        if re.search(r"审批|approval", reason, re.I):
            return "approval"
        if re.search(r"命令|脚本|重定向|串接|command|script|redirect|pipeline", reason, re.I):
            return "command_shape"
        return "call_boundary"

    def observe(self, name: str, args: dict[str, Any], result: Any) -> None:
        """Consume only completed tool outcomes; no caller assertion grants access."""
        if not isinstance(result, str) or not isinstance(args, dict):
            return
        failed = bool(_FAILED.match(result))
        targets = self._targets(name, args)
        if failed:
            permission = bool(re.match(r"^\s*\[permission denied\]", result, re.I))
            if not permission and not _BOUNDARY.search(result):
                return
            reason = _clean(_FAILED.sub("", result, count=1), 160)
            category = self._category(reason)
            for target in targets:
                key = (name, target, category)
                old = self._refusals.get(key)
                self._refusals[key] = _Refusal(name, target, category, reason, (old.count + 1) if old else 1)
                self._refusals.move_to_end(key)
            while len(self._refusals) > self.MAX_ENTRIES:
                self._refusals.popitem(last=False)
            return
        if not result.strip() or re.match(r"^\s*(?:error:|读取失败|文件不存在)", result, re.I):
            return
        # A nonzero/unknown shell exit does not resolve an earlier restriction.
        if name in _SHELL and not re.match(r"^exit_code=0(?:\s|$)", result):
            return
        for key in list(self._refusals):
            if key[0] == name and key[1] in targets:
                self._refusals.pop(key)
        if name in _SHELL:
            example = self._safe_validation_example(str(args.get("command") or args.get("cmd") or ""))
            if example:
                self._validation_example = example

    @staticmethod
    def _safe_validation_example(command: str) -> str:
        """Admit only plain known test invocations, never arbitrary shell output."""
        if not command or len(command) > 400 or any(ch in command for ch in "\r\n`$;|<>&"):
            return ""
        if _SECRET.search(command) or "://" in command:
            return ""
        try:
            lexer = shlex.shlex(command, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            lexer.escape = ""
            words = list(lexer)
        except ValueError:
            return ""
        if not words or not _SAFE_PATH.fullmatch(words[0]):
            return ""
        name = posixpath.basename(words[0].replace("\\", "/")).lower()
        if name in {"python", "python.exe", "python3"} and words[1:3] == ["-m", "pytest"]:
            arguments = words[3:]
        elif name in {"pytest", "pytest.exe"}:
            arguments = words[1:]
        elif name in {"npm", "npm.cmd", "pnpm", "yarn", "bun"}:
            return command if words[1:] in (["test"], ["check"], ["typecheck"], ["build"],
                                            ["run", "test"], ["run", "check"], ["run", "typecheck"],
                                            ["run", "lint"], ["run", "build"]) else ""
        else:
            return ""
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word in _SAFE_PYTEST_FLAGS:
                index += 1
                continue
            if word == "-c" and index + 1 < len(arguments) and _SAFE_PATH.fullmatch(arguments[index + 1]):
                index += 2
                continue
            if word == "-o" and index + 1 < len(arguments) and arguments[index + 1] == "addopts=":
                index += 2
                continue
            if word.startswith("-") or not _SAFE_PATH.fullmatch(word) or "=" in word:
                return ""
            index += 1
        return command

    def context(self, max_chars: int = 700) -> str:
        """Return bounded low-trust observations; the loop supplies the trust wrapper."""
        if max_chars <= 0:
            return ""
        lines = []
        older = []
        native_hint = ""
        if self._refusals:
            lines.append("最近实际拒绝记录（仅记录当次结果，权限状态可能变化）：")
            for refusal in reversed(tuple(self._refusals.values())):
                target = self._display_target(refusal.target)
                line = f"{refusal.tool} {target} [{refusal.category}] x{refusal.count}: {refusal.reason}"
                older.append(line[:self.MAX_ENTRY_CHARS])
            lines.append(older.pop(0))
            categories = {entry.category for entry in self._refusals.values()}
            if categories & {"native_read", "read_reuse"}:
                hints = []
                if "read_file" in self.available_tools:
                    hints.append("read_file(path, offset, limit)")
                if "native_read" in categories and "grep" in self.available_tools:
                    hints.append("grep(pattern, path)")
                if hints:
                    native_hint = "当前可用原生参数：" + "；".join(hints) + "；仍受本次实际范围与额度限制。"
        if self._validation_example:
            example = "此前实际执行成功的验证调用（不代表本次已获许可）：" + self._validation_example
            # Keep an exact runnable example, rather than a command clipped by
            # old refusals. It remains an observation, never a new permission.
            if len("\n".join([*lines, example])) <= max_chars:
                lines.append(example)
        if native_hint:
            lines.append(native_hint)
        lines.extend(older)
        return "\n".join(lines)[:max_chars]
