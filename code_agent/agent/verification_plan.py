"""Small task-local verification scheduling from real execution receipts.

This module neither executes tests nor caches their results. Permission checks,
input freshness and the successful checked paths remain the caller's concern.
"""

from __future__ import annotations

from dataclasses import dataclass
import posixpath
import re
import shlex
from typing import Iterable

from code_agent.agent.code_question import _paths_and_intent


_TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)test_[^/]+$|"
                        r"(?:_test|\.test|\.spec)\.[^/]+$|(?:Test|Tests|Spec)\.[^/]+$", re.I)
_CREATE_TEST = re.compile(
    r"(?:新增|新建|创建|添加|增加|编写|补充|补齐|生成)[^。；;!?！？\n]{0,60}(?:回归测试|测试|test)|"
    r"\b(?:add|create|write|implement|generate)\b[^.;!?\n]{0,70}\b(?:regression\s+)?tests?\b", re.I,
)
_CREATE = re.compile(r"新增|新建|创建|添加|增加|编写|补充|补齐|生成|\b(?:add|create|write|implement|generate)\b", re.I)
_NEGATED = re.compile(r"不要|无需|不用|不必|禁止|暂不|不需要|别|\b(?:not|never|without|don['’]t|do\s+not)\b", re.I)
_FULL = re.compile(r"全量(?:测试|验证|验收|检查)?|完整(?:验收|测试|校验)|所有测试|全部测试|"
                   r"\b(?:full|complete|entire)\s+(?:test\s+)?(?:suite|tests?|verification|validation)\b", re.I)
_FINAL_COMMAND = re.compile(r"(?:完整|最终)(?:交付)?(?:验收|验证|测试)?命令\s*[:：]|"
                            r"(?:final|full|complete)\s+(?:(?:test|verification|validation)\s+)?command\s*[:：]", re.I)


def _path(raw: str, cwd: str) -> str:
    raw = raw.replace("\\", "/")
    absolute = raw.startswith("/") or bool(re.match(r"^[A-Za-z]:/", raw))
    value = posixpath.normpath(raw if absolute else posixpath.join(cwd, raw))
    return value.casefold() if re.match(r"^[A-Za-z]:/", value) else value


def _test_path(raw: str, workspace: str) -> bool:
    """Classify project paths, never pytest's test_* temporary ancestors."""
    root = _path(workspace, ".").rstrip("/")
    value = _path(raw, root or "/")
    if root and value.startswith(root + "/"):
        value = value[len(root) + 1:]
    elif value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        # For a separate external project its parent names are not evidence of
        # a test module; an explicit test filename remains recognizable.
        value = posixpath.basename(value)
    return bool(_TEST_PATH.search(value))


@dataclass(frozen=True)
class ValidationCommand:
    runner: str
    scope: tuple[str, ...]
    focused: bool
    command: str
    filtered: bool = False

    @property
    def full(self) -> bool:
        return not self.focused


def parse_validation_command(command: str, workspace=None) -> ValidationCommand | None:
    """Recognize plain common test runners, without evaluating shell syntax."""
    if not isinstance(command, str) or not command.strip() or any(c in command for c in "\r\n`$;|<>"):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="&")
        lexer.whitespace_split, lexer.commenters, lexer.escape = True, "", ""
        words = list(lexer)
    except ValueError:
        return None
    cwd = _path(str(workspace or "."), ".")
    if len(words) >= 4 and words[0].lower() in {"cd", "set-location"} and words[2] == "&&":
        cwd, words = _path(words[1], cwd), words[3:]
    if not words or any("&" in word for word in words):
        return None
    name = posixpath.basename(words[0].replace("\\", "/")).lower()
    rest = words[1:]
    if name in {"python", "python.exe", "python3"}:
        if rest[:2] != ["-m", "pytest"]:
            return None
        name, rest = "pytest", rest[2:]
    focused = False
    scopes: list[str] = []
    if name in {"pytest", "pytest.exe"}:
        index = 0
        while index < len(rest):
            word = rest[index]
            if word in {"--collect-only", "--co"}:
                return None
            if word in {"-k", "-m", "--keyword", "--markexpr"}:
                if index + 1 == len(rest):
                    return None
                focused, index = True, index + 2
                continue
            if word.startswith(("--keyword=", "--markexpr=")):
                focused, index = True, index + 1
                continue
            if word in {"-c", "-o", "--override-ini", "--rootdir"}:
                if index + 1 == len(rest):
                    return None
                index += 2
                continue
            if word in {"-q", "-v", "-vv", "-vvv", "-x", "-s", "-l", "--showlocals", "--disable-warnings",
                        "--strict-markers", "--strict-config"} or re.fullmatch(r"--(?:tb=\w+|maxfail=\d+|color=\w+)", word):
                index += 1
                continue
            if word.startswith("-"):
                return None
            if "::" in word:
                focused = True
            scopes.append(_path(word.split("::", 1)[0], cwd))
            index += 1
        # A single explicit test file is a useful focused regression check.
        filtered = focused
        focused = focused or (len(scopes) == 1 and bool(re.search(r"\.[\w]+$", scopes[0])))
        return ValidationCommand("pytest", tuple(dict.fromkeys(scopes or [cwd])), focused, command, filtered)
    if name in {"npm", "npm.cmd", "pnpm", "yarn", "bun"}:
        if rest not in (["test"], ["run", "test"], ["run", "test:unit"], ["run", "test:integration"]):
            return None
        return ValidationCommand(name, (cwd,), rest[-1] != "test", command)
    if name in {"mvn", "mvn.cmd", "mvnw", "mvnw.cmd", "mvnw.bat"}:
        # These flags only affect output/interaction. Test selectors, skip
        # properties and extra goals remain visible to the focused-run guard.
        rest = [word for word in rest if word not in {"-q", "--quiet", "-B", "--batch-mode",
                                                      "-ntp", "--no-transfer-progress"}]
    if name in {"cargo", "go", "dotnet", "mvn", "mvn.cmd", "mvnw", "mvnw.cmd", "mvnw.bat",
                "gradle", "gradlew", "gradlew.bat"}:
        if not rest or rest[0] != "test":
            return None
        if name == "go":
            if any(flag in rest for flag in ("-run", "-bench")):
                focused = True
            scopes = [_path(value.removesuffix("/..."), cwd) for value in rest[1:]
                      if not value.startswith("-") and value not in {"1", "0"}]
        elif len(rest) > 1:
            # Runner-specific selectors and options are not a full-suite proof.
            focused = True
        return ValidationCommand(name, tuple(scopes or [cwd]), focused, command, focused)
    return None


class VerificationPlan:
    """Pending requested tests and one current final-scope execution receipt."""

    def __init__(self, workspace, prompt: str):
        self.workspace = str(workspace or ".").replace("\\", "/")
        self.required_test_paths: tuple[str, ...] = ()
        requested: dict[str, None] = {}
        writable_tests: dict[str, None] = {}
        self.tests_requested = False
        # Split at prose boundaries while masking source-path punctuation first.
        _, masked = _paths_and_intent(prompt)
        normalized = list(prompt)
        for index, character in enumerate(masked):
            if character in ",;!?":
                normalized[index] = {",": "，", ";": "；", "!": "！", "?": "？"}[character]
            elif character == "." and (index + 1 == len(masked) or masked[index + 1].isspace()):
                normalized[index] = "。"
        _, masked = _paths_and_intent("".join(normalized))
        start = 0
        for boundary in [*re.finditer(r"[。；;!?！？\n]|[,，](?=\s*(?:参考|运行|执行|勿|不要|原有|existing|run|reference))", masked, re.I), None]:
            end = boundary.start() if boundary else len(prompt)
            clause, intent = prompt[start:end], masked[start:end]
            command_header = re.search(r"(?:命令|\bcommand)\s*[:：]", intent, re.I)
            if command_header:
                # Test invocation paths describe read/check scope, including
                # after phrases such as "before adding tests, baseline command".
                clause, intent = clause[:command_header.start()], intent[:command_header.start()]
            if re.search(r"可写范围|(?:只|仅)(?:能|允许)?修改|\bonly\s+(?:edit|modify|change)\b", intent, re.I):
                files, _ = _paths_and_intent(clause)
                writable_tests.update(dict.fromkeys(_path(path, self.workspace) for path in files.values()
                                                    if _test_path(path, self.workspace)))
            action = _CREATE.search(intent)
            prefix = re.split(r"[,，;；。.!?]", intent[:action.start()])[-1] if action else ""
            affirmative = bool(action and not _NEGATED.search(prefix))
            if affirmative:
                paths, _ = _paths_and_intent(clause)
                tests = [_path(path, self.workspace) for path in paths.values() if _test_path(path, self.workspace)]
                if tests or _CREATE_TEST.search(clause):
                    self.tests_requested = True
                    requested.update(dict.fromkeys(tests))
            start = boundary.end() if boundary else len(prompt)
        self.required_test_paths = tuple(requested or (writable_tests if self.tests_requested else {}))
        self.required_final_scope: tuple[str, ...] = ()
        self.requires_full_validation = self.tests_requested or any(
            not _NEGATED.search(re.split(r"[,，;；。.!?\n]", masked[:match.start()])[-1]) for match in _FULL.finditer(masked))
        lines = prompt.splitlines()
        for index, line in enumerate(lines):
            match = _FINAL_COMMAND.search(line)
            if match:
                command = line[match.end():].strip().strip("`")
                if not command:
                    command = next((next_line.strip().strip("`") for next_line in lines[index + 1:]
                                    if next_line.strip() and not next_line.lstrip().startswith("```")), "")
                parsed = parse_validation_command(command, self.workspace)
                if parsed is not None:
                    self.required_final_scope = parsed.scope
                    self.requires_full_validation = True
        self._changed: set[str] = set()
        self._baseline_seen = False
        self._full_current = False

    @property
    def pending_tests(self) -> tuple[str, ...]:
        pending = tuple(path for path in self.required_test_paths if path not in self._changed)
        if pending:
            return pending
        if self.tests_requested and not self.required_test_paths and not any(_test_path(path, self.workspace) for path in self._changed):
            return ("新增或补充用户要求的回归测试",)
        return ()

    @property
    def ready_to_finish(self) -> bool:
        return not self.pending_tests and (not self.requires_full_validation or self._full_current)

    @property
    def has_current_full_result(self) -> bool:
        return self._full_current

    def record_changes(self, paths: Iterable[str]) -> None:
        changed = {_path(str(path), self.workspace) for path in paths if path}
        if changed:
            self._changed.update(changed)
            self._full_current = False

    def before_validation(self, command: str, *, explicit_full: bool = False) -> str | None:
        parsed = parse_validation_command(command, self.workspace)
        declared_final = bool(parsed and self.required_final_scope and not parsed.filtered
                              and self._covers(parsed, self.required_final_scope))
        if parsed is None or (parsed.focused and not declared_final) or explicit_full:
            return None
        if self.pending_tests and (self._changed or self._baseline_seen):
            return ("用户要求的新增回归测试尚未实际交付：" + "、".join(self.pending_tests)
                    + "。先完成本批实现和测试，可定点运行失败用例；补齐后再运行最终完整验收。"
                    "此前实际执行的初始复现/基线无需重复。")
        return None

    def record_validation(self, command: str, result: str, *, verified_paths: Iterable[str] = ()) -> bool:
        """Caller supplies only actual executions, never model claims/cache hits."""
        parsed = parse_validation_command(command, self.workspace)
        outcome = re.match(r"^exit_code=(-?\d+)(?:\s|$)", str(result or ""))
        if parsed is None or outcome is None:
            return False
        self._baseline_seen = True
        if int(outcome[1]) != 0:
            self._full_current = False
            return False
        actual_checked = tuple(verified_paths)
        if not actual_checked or self.pending_tests or parsed.filtered or (parsed.focused and not self.required_final_scope):
            return False
        required_tests = self.required_test_paths or tuple(path for path in self._changed if _test_path(path, self.workspace))
        required = (*self.required_final_scope, *(required_tests if self.tests_requested else ()))
        covers = self._covers(parsed, required)
        if covers:
            self._full_current = True
            return True
        return False

    @staticmethod
    def _covers(parsed: ValidationCommand, required_paths: Iterable[str]) -> bool:
        return all(any(required == scope or required.startswith(scope.rstrip("/") + "/")
                       for scope in parsed.scope) for required in required_paths)

    def note(self, max_chars: int = 600) -> str:
        if self.pending_tests:
            text = ("待交付测试：" + "、".join(self.pending_tests) + "。先完成实现与这些测试，再定点检查失败项，最后完整验收；"
                    "实现开始后不要在新测试缺失时提前全量。")
        elif self.requires_full_validation and not self._full_current:
            text = "新增测试义务已补齐；依据实际失败先定点验证，交付前还需当前版本的明确完整验收范围通过。"
        elif self._full_current:
            text = "当前版本已有真实完整范围验证通过；直接交付，只有新需求或具体失败才继续新增改动。"
        else:
            text = "按改动完成必要的定点验证；用户明确要求的最终范围必须实际执行，不把定点结果称为全量通过。"
        return text[:max(0, max_chars)]
