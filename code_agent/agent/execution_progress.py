"""Per-prompt, bounded evidence ledger for successful-but-unproductive loops.

This is deliberately not a shell interpreter or an execution/verification receipt.
Only delivered file lines and stable read-only observations can exhaust progress.
Inconclusive execution, waits and external calls pause that accounting without
erasing it; callers can explicitly report confirmed effects or verification.
No files are opened by this module.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
import hashlib
import ntpath
from pathlib import Path
import posixpath
import re
import shlex

from code_agent.context.safety import wrap_untrusted
from code_agent.tools.read_spec import has_read_window


_NUMBERED = re.compile(r"^\s*(\d+)(?:\t|: ?|\| ?)(.*)$")
_READ_HEADER = re.compile(r"^.+ 第 \d+-\d+ 行（每行前缀是真实行号）$")
_MATCH = re.compile(r"^(.+?):(\d+)(?::|-)(.*)$")
_MATCH_HEADER = re.compile(r"^(.+?):(\d+)$")
_FAILURE = re.compile(
    r"^(?:\[tool error\]|\[permission denied\]|error:|denied\b|permission denied\b|"
    r"文件不存在[:：]|读取失败[:：])", re.I,
)
_CUT = re.compile(r"(?:\[外部化\]|\[.*(?:truncated|omitted).*\]|"
                  r"\(.*(?:truncated|omitted).*\)|（省略了|--- 结尾 \d+ 字节 ---)", re.I)
_READ_TOOLS = {"read_file", "grep", "glob", "list_dir", "code_search", "code_map"}
_SHELL_TOOLS = {"bash", "bash_persistent"}
_WAIT_TOOLS = {
    "ask_user", "ask_question", "request_user_input", "request_approval",
    "job_output", "job_wait", "wait_agent", "wait_agents", "wait",
    "read_agent_execution", "agent_team_list", "team_list",
}
_WRITE_TOOLS = {"write_file", "edit_file", "edit_many", "apply_patch", "replace_in_file"}


def _digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", errors="replace"), digest_size=16).digest()


@dataclass
class _FileEvidence:
    # Hashes are sufficient for novelty; only a few short source excerpts survive.
    lines: OrderedDict[int, bytes] = field(default_factory=OrderedDict)
    samples: OrderedDict[int, str] = field(default_factory=OrderedDict)


class ExecutionProgress:
    """Old reads add stalls; inconclusive batches preserve them, never reset them."""

    MAX_FILES = 48
    MAX_LINES = 8192
    MAX_LINES_PER_FILE = 2048
    MAX_OBSERVATIONS = 192
    MAX_RESULT_CHARS = 65536
    MAX_SAMPLE_CHARS = 140
    MAX_SAMPLES_PER_FILE = 6

    def __init__(self, workspace: Path | None = None):
        self.workspace = str(workspace or "").replace("\\", "/")
        self._windows = bool(re.match(r"^[a-zA-Z]:", self.workspace))
        self._files: OrderedDict[str, _FileEvidence] = OrderedDict()
        self._observations: OrderedDict[bytes, None] = OrderedDict()
        self._has_observations = False
        self._stalled_turns = 0
        self._batch_seen = False
        self._batch_progress = False
        self._batch_uncertain = False

    @property
    def has_observations(self) -> bool:
        return self._has_observations

    @property
    def should_finalize(self) -> bool:
        return self._stalled_turns >= 6

    @property
    def reminder_due(self) -> bool:
        return self._stalled_turns in (3, 5)

    @property
    def stalled_turns(self) -> int:
        return self._stalled_turns

    def record_effect(self, paths: Iterable[str | Path] = ()) -> None:
        """Report a confirmed effect or verification result for this batch.

        The caller must obtain that evidence through normal execution; an unknown
        shell command or its zero exit status alone is not a confirmed effect.
        Optional changed paths invalidate only their old source coverage. No
        permission, file access or success claim is created by this bookkeeping.
        """
        self._has_observations = True
        self._batch_seen = True
        self._batch_progress = True
        for path in paths:
            if path:
                self._files.pop(self._path(str(path)), None)

    def _path(self, value: str, cwd: str = "") -> str:
        raw = str(value).replace("\\", "/")
        base = cwd or self.workspace
        absolute = raw.startswith("/") or bool(re.match(r"^[a-zA-Z]:/", raw))
        joined = raw if absolute else posixpath.join(base, raw)
        result = posixpath.normpath(joined)
        if self._windows or re.match(r"^[a-zA-Z]:/", result):
            result = result.casefold()
        return result[:1200]

    def _remember(self, text: str) -> bool:
        key = _digest(text)
        fresh = key not in self._observations
        self._observations[key] = None
        self._observations.move_to_end(key)
        while len(self._observations) > self.MAX_OBSERVATIONS:
            self._observations.popitem(last=False)
        return fresh

    def _record_lines(self, path: str, lines: list[tuple[int, str]]) -> bool:
        if not lines:
            return False
        evidence = self._files.setdefault(path, _FileEvidence())
        self._files.move_to_end(path)
        changed = False
        for number, body in lines:
            if number < 1:
                continue
            # ReadFileTool's numbered windows use rstrip(), while a plain cat /
            # sed preserves trailing blanks. These are the same source evidence,
            # rather than a new file version every time the reader is switched.
            digest = _digest(body.rstrip())
            if evidence.lines.get(number) != digest:
                changed = True
                evidence.samples[number] = body[:self.MAX_SAMPLE_CHARS]
                evidence.samples.move_to_end(number)
            evidence.lines[number] = digest
            evidence.lines.move_to_end(number)
            while len(evidence.lines) > self.MAX_LINES_PER_FILE:
                old, _ = evidence.lines.popitem(last=False)
                evidence.samples.pop(old, None)
            while len(evidence.samples) > self.MAX_SAMPLES_PER_FILE:
                evidence.samples.popitem(last=False)
        while len(self._files) > self.MAX_FILES:
            self._files.popitem(last=False)
        # A second global bound also applies when many files have large windows.
        total = sum(len(item.lines) for item in self._files.values())
        while total > self.MAX_LINES and self._files:
            _, oldest = self._files.popitem(last=False)
            total -= len(oldest.lines)
        return changed

    @staticmethod
    def _delivered(text: str, *, possibly_clipped: bool = False) -> tuple[list[str], bool]:
        """Take complete, visible head lines, never a hidden externalized tail."""
        external = text.startswith("[外部化]")
        if external:
            head = re.search(r"^--- 开头 \d+ 字节 ---\n", text, re.M)
            if not head:
                return [], True
            text = text[head.end():]
        limited = len(text) > ExecutionProgress.MAX_RESULT_CHARS
        text = text[:ExecutionProgress.MAX_RESULT_CHARS]
        lines = text.split("\n")
        if lines and not lines[-1] and text.endswith("\n"):
            lines.pop()
        partial = external or limited or possibly_clipped
        for idx, line in enumerate(lines):
            if _CUT.search(line):
                lines = lines[:idx]
                partial = True
                break
        # Budget clipping might leave the last source line unfinished. A dropped
        # complete line is harmless; counting a partial line as read is not.
        if partial and lines:
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                lines.pop()
        return lines, partial

    @staticmethod
    def _shell(command: str) -> tuple[str, str, int | None, bool] | None:
        """Recognize a tiny, inert grammar; reject everything else as unknown.

        Return (cwd, sole source path, first line, numbered output). An unknown first line denotes
        a search, whose actual file:line output must supply its own coordinates.
        The sole supported pipe is a numbered file reader followed by a literal
        sed line window. No other pipeline, redirection, substitution or awk /
        sed program is accepted merely because its name resembles a reader.
        """
        if not command or any(char in command for char in ("\n", "\r", "`")):
            return None
        # A single-quoted awk program can contain literal $0. The same bytes in
        # double quotes would be expanded by the shell and cannot be interpreted
        # as the recognized program. Never evaluate any command to decide this.
        quote = ""
        escaped = False
        for char in command:
            if escaped:
                escaped = False
                continue
            if char == "\\" and quote != "'":
                escaped = True
            elif char == quote:
                quote = ""
            elif not quote and char in ("'", '"'):
                quote = char
            elif char == "$" and quote != "'":
                return None
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            return None
        cwd = ""
        if len(tokens) >= 4 and tokens[0].casefold() in ("cd", "set-location") and tokens[2] == "&&":
            cwd, tokens = tokens[1], tokens[3:]
        # These commands preserve real source line numbers. Accept the complete
        # two-stage grammar, not a generic "starts with cat/nl" whitelist.
        if (len(tokens) == 7 and tokens[3] == "|" and tokens[4:6] == ["sed", "-n"]
                and ((tokens[0] == "cat" and tokens[1] in ("-n", "--number"))
                     or (tokens[0] == "nl" and tokens[1] == "-ba"))
                and not tokens[2].startswith("-")
                and re.fullmatch(r"[1-9]\d*(?:,[1-9]\d*)?p", tokens[6])):
            first = int(tokens[6].rstrip("p").split(",", 1)[0])
            return cwd, tokens[2], first, True
        if not tokens or any(t and set(t) <= set(";&|<>()") for t in tokens):
            return None
        name = ntpath.basename(tokens[0]).casefold()
        # Only literal executable names, not /tmp/cat (an arbitrary executable).
        if tokens[0].casefold() != name:
            return None
        args = tokens[1:]
        if name == "cat" and len(args) == 1 and not args[0].startswith("-"):
            return cwd, args[0], 1, False
        if name == "cat" and len(args) == 2 and args[0] in ("-n", "--number") and not args[1].startswith("-"):
            return cwd, args[1], 1, True
        if name == "nl" and len(args) == 2 and args[0] == "-ba" and not args[1].startswith("-"):
            return cwd, args[1], 1, True
        if name == "sed" and len(args) == 3 and args[0] == "-n":
            window = re.fullmatch(r"(\d+)(?:,(\d+))?p", args[1])
            if window and not args[2].startswith("-"):
                return cwd, args[2], int(window[1]), False
        if name == "awk" and len(args) == 2:
            window = re.fullmatch(r"NR\s*>=\s*(\d+)\s*&&\s*NR\s*<=\s*\d+\s*\{\s*print\s*;?\s*\}", args[0])
            if window and not args[1].startswith("-"):
                return cwd, args[1], int(window[1]), False
            numbered = re.fullmatch(
                r'NR\s*>=\s*([1-9]\d*)\s*&&\s*NR\s*<=\s*[1-9]\d*\s*'
                r'\{\s*printf\s+"%d\\t%s\\n"\s*,\s*NR\s*,\s*\$0\s*;?\s*\}', args[0],
            )
            if numbered and not args[1].startswith("-"):
                return cwd, args[1], int(numbered[1]), True
        if name in ("get-content", "gc"):
            if len(args) == 1 and not args[0].startswith("-"):
                return cwd, args[0], 1, False
            if len(args) == 2 and args[0].casefold() in ("-path", "-literalpath"):
                return cwd, args[1], 1, False
        if name in ("rg", "grep"):
            # No option that runs a preprocessor, loads a pattern file or writes
            # to a path; only simple literal pattern + optional source path.
            values = []
            idx = 0
            while idx < len(args):
                arg = args[idx]
                if arg in ("-n", "--line-number", "-i", "--ignore-case", "-F", "-E", "-H", "--no-heading", "--"):
                    pass
                elif arg in ("-A", "-B", "-C", "-m", "--max-count") and idx + 1 < len(args) and args[idx + 1].isdigit():
                    idx += 1
                elif arg.startswith("-"):
                    return None
                else:
                    values.append(arg)
                idx += 1
            if 1 <= len(values) <= 2:
                return cwd, values[1] if len(values) == 2 else "", None, True
        return None

    def _search_lines(self, lines: list[str], cwd: str, sole_path: str = "") -> tuple[bool, bool]:
        changed = False
        observed = False
        current = ""
        for line in lines:
            match = _MATCH.match(line)
            if match:
                path = self._path(match[1], cwd)
                changed |= self._record_lines(path, [(int(match[2]), match[3])])
                observed = True
                current = path
                continue
            header = _MATCH_HEADER.match(line)
            if header:
                current = self._path(header[1], cwd)
                continue
            numbered = _NUMBERED.match(line)
            if numbered and (current or sole_path):
                path = current or self._path(sole_path, cwd)
                changed |= self._record_lines(path, [(int(numbered[1]), numbered[2])])
                observed = True
        return changed, observed

    def observe(self, tool: str, arguments: dict, result: str) -> None:
        """Observe precisely what the model received, never the pre-spill output."""
        self._has_observations = True
        self._batch_seen = True
        tool = str(tool)
        text = str(result).replace("\r\n", "\n")
        if tool in _WAIT_TOOLS or any(part in tool for part in ("approval", "question")):
            self._batch_uncertain = True
            return
        if tool in _WRITE_TOOLS:
            # A rejected edit did not change the source. Keep its read evidence
            # so recovery can fix the call without starting source discovery over.
            if not _FAILURE.match(text) and not text.startswith("[edit_many] **部分失败**"):
                targets = ([item.get("path") for item in arguments.get("edits", []) if isinstance(item, dict)]
                           if tool == "edit_many" and isinstance(arguments.get("edits"), list)
                           else [arguments.get("path") or arguments.get("file_path")])
                self.record_effect(targets)
            else:
                self._batch_uncertain = True
            return
        shell = self._shell(str(arguments.get("command") or "")) if tool in _SHELL_TOOLS else None
        if tool not in _READ_TOOLS and shell is None:
            self._batch_uncertain = True
            return
        if _FAILURE.match(text):
            # Changing an error's wording is neither new source evidence nor
            # execution progress. Reliable read failures cannot erase old stalls.
            return
        clipped_read = False
        if tool == "read_file" and not has_read_window(arguments):
            try:
                byte_limit = int(arguments.get("max_bytes") or 65536)
            except (TypeError, ValueError):
                byte_limit = 65536
            # The built-in whole-file reader slices bytes without a truncation
            # suffix. Reaching its cap does not prove the last line was complete.
            clipped_read = byte_limit > 0 and len(text.encode("utf-8", errors="replace")) >= byte_limit
        lines, partial = self._delivered(text, possibly_clipped=clipped_read)
        if not lines:
            # A pointer with no visible bytes cannot establish what was read.
            self._batch_uncertain = True
            return
        if tool == "read_file":
            path = self._path(str(arguments.get("path") or ""))
            if text == "(空文件)" or text.startswith("（第 ") and "行起没有内容；文件共约 " in text:
                self._batch_progress |= self._remember("empty-read:" + path + "\n" + text)
                return
            numbered = has_read_window(arguments) and bool(_READ_HEADER.match(lines[0]))
            if numbered:
                lines = lines[1:]
            elif has_read_window(arguments):
                numbered = all(_NUMBERED.match(line) for line in lines)
                if not numbered:
                    self._batch_progress |= self._remember("read:" + path + "\n" + "\n".join(lines))
                    return
            if numbered:
                delivered = [(int(match[1]), match[2]) for line in lines if (match := _NUMBERED.match(line))]
            else:
                delivered = list(enumerate(lines, 1))
            self._batch_progress |= self._record_lines(path, delivered)
            if not delivered:
                self._batch_progress |= self._remember("read:" + path + "\n" + "\n".join(lines))
            return
        if shell is not None:
            cwd, path, start, numbered = shell
            cwd = self._path(cwd) if cwd else self.workspace
            # Persistent shells without an explicit cwd have unknown directory
            # state. They are not interchangeable with workspace-relative reads.
            if tool == "bash_persistent" and not shell[0]:
                self._batch_uncertain = True
                return
            if lines and lines[0].startswith("exit_code="):
                # executor.run_shell appends exactly this suffix for its local
                # output cap. Unknown suffixes and killed processes remain
                # uncertain; only a normal successful read can establish lines.
                code = re.fullmatch(r"exit_code=(-?\d+)( \(输出超过 \d+ 字节，已截断\))?", lines.pop(0))
                if not code or code[1] != "0":
                    self._batch_uncertain = True
                    return
                if code[2] and not partial:
                    while lines and not lines[-1].strip():
                        lines.pop()
                    if lines:
                        lines.pop()
                    partial = True
            else:
                # Only genuine shell success receipts are reliable readers.
                self._batch_uncertain = True
                return
            if lines and lines[0] == "[stdout]":
                lines.pop(0)
            if partial and not lines:
                self._batch_uncertain = True
                return
            if lines == ["(无输出)"]:
                self._batch_progress |= self._remember("empty-read:" + self._path(path, cwd))
                return
            if start is not None:
                # Source line numbers only come from the recognized read grammar
                # or the actual numbered result; requested limits do not count.
                delivered = ([(int(m[1]), m[2]) for line in lines if (m := _NUMBERED.match(line))]
                             if numbered else [(start + idx, line) for idx, line in enumerate(lines)])
                self._batch_progress |= self._record_lines(self._path(path, cwd), delivered)
                if not delivered:
                    self._batch_progress |= self._remember("empty-read:" + self._path(path, cwd))
                return
            changed, observed = self._search_lines(lines, cwd, path)
        elif tool == "grep":
            changed, observed = self._search_lines(lines, self.workspace)
        else:
            changed, observed = False, False
        if observed:
            self._batch_progress |= changed
        else:
            # Content-based, rather than argument-based, novelty catches query
            # spelling changes that return the very same search/list result.
            self._batch_progress |= self._remember(tool + "\n" + "\n".join(lines))

    def finish_turn(self) -> None:
        if self._batch_seen:
            if self._batch_progress:
                self._stalled_turns = 0
            elif not self._batch_uncertain:
                self._stalled_turns += 1
        self._batch_seen = self._batch_progress = self._batch_uncertain = False

    @staticmethod
    def _intervals(numbers: list[int]) -> str:
        parts = []
        for number in sorted(numbers):
            if parts and number == parts[-1][1] + 1:
                parts[-1][1] = number
            else:
                parts.append([number, number])
        return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in parts[:12]) + (" …" if len(parts) > 12 else "")

    def context(self, max_chars: int = 2200, *, execution: bool = False) -> str:
        limit = max(0, int(max_chars))
        if not limit or not self.has_observations:
            return ""
        intro = (
            f"本轮执行进展：已累计 {self._stalled_turns} 轮只读调用未增加证据（不确定执行不计入）。"
            + ("复用已读源码继续实施和验证；确实缺少某项信息才补查对应范围。" if execution else
               "复用已读证据回答；确实缺少某项信息才补查对应范围。") +
            "以下仅记录工具曾返回的片段，可能过时或省略，未覆盖处仍未知；"
            "未确认的部分应说明，不是执行成功或验证通过的证明。\n"
        )
        if len(intro) >= limit:
            return intro[:limit]
        # Preserve both wrapper boundaries even with a very small caller budget.
        wrapper_cost = len(wrap_untrusted("", "本次已读证据"))
        available = limit - len(intro) - wrapper_cost
        if available < 24:
            return intro.rstrip()[:limit]
        rows = []
        used = 0
        for path, entry in reversed(self._files.items()):
            row = f"{path}；已见行 {self._intervals(list(entry.lines))}"
            for number, snippet in list(entry.samples.items())[-3:]:
                row += f"\n  {number}: {snippet}"
            # Data may itself contain boundary markers: quote those markers so
            # a source line cannot manufacture an apparent end to this block.
            row = row.replace("【数据块", "【引用的数据块")
            if used + len(row) + 1 > available:
                row = row[:max(0, available - used - 1)]
            if row:
                rows.append(row)
                used += len(row) + 1
            if used >= available - 1:
                break
        if not rows:
            rows = ["暂无可定位的源文件片段；失败或外化指针不表示已读源码。"[:available]]
        return intro + wrap_untrusted("\n".join(rows), "本次已读证据")
