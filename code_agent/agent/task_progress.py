"""Task-local delivery discipline; never grants permissions or claims tool success.

The loop supplies disk-change and validation receipts.  Reading a fresh file,
changing shell spelling, or a successful process exit is not a delivery receipt.
Annotation jobs have deterministic scope. Feature and bug-fix jobs use a small
working set while retaining room to follow dependencies and create new files.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
import posixpath
import re
from typing import Any, Iterable

from code_agent.agent.code_question import _paths_and_intent, requires_mutation
from code_agent.agent.execution_progress import ExecutionProgress
from code_agent.agent.verification_plan import VerificationPlan
from code_agent.agent.validation_commands import inline_python_validation_paths
from code_agent.tools.read_spec import has_read_window, read_window


_ANNOTATION = re.compile(r"注释|docstrings?|\bcomments?\b", re.I)
_ANNOTATION_PRODUCT_WORK = re.compile(
    r"(?:修复|修好|优化|实现|开发|重构)[^，。；！？\n]{0,35}"
    r"(?:注释[^，。；！？\n]{0,12}(?:功能|服务|工具|缓存|请求|接口|面板|bug|错误)|"
    r"(?:功能|服务|工具|缓存|请求|接口|面板)[^，。；！？\n]{0,12}注释)|"
    r"\b(?:fix|implement|develop|refactor)\b[^,;.!?\n]{0,50}\b(?:comment|docstring)[- ]"
    r"(?:tool|cache|service|feature|request|panel)\b", re.I,
)
_CODE_WORK = re.compile(
    r"修复|修好|修\s*bug|开发|实现|重构|优化|调整|修改|更新|删除|"
    r"(?:添加|新增|增加|补充)[^，。；！？\n]{0,20}(?:功能|能力|接口|支持|测试|配置)|"
    r"\b(?:fix|implement|develop|refactor|optimi[sz]e|modify|update|delete|remove)\b", re.I,
)
_BROAD = re.compile(
    r"全项目|整个项目|全仓库|整个仓库|所有文件|全部文件|所有代码|全部代码|"
    r"整个目录|所有模块|全部模块|每个文件|多(?:个)?文件|批量(?:处理|修改|注释)?(?:多个|所有|全部)?文件|"
    r"\b(?:all\s+(?:source\s+)?files|entire\s+(?:project|repo|directory)|"
    r"whole\s+(?:project|repo)|multiple\s+files|across\s+(?:the\s+)?(?:project|repo))\b", re.I,
)
_WHOLE_SCOPE = re.compile(r"全项目|整个项目|全仓库|整个仓库|整个目录|"
                          r"\b(?:entire|whole)\s+(?:project|repo|directory)\b|"
                          r"\bacross\s+(?:the\s+)?(?:project|repo)\b", re.I)
_FAILED = re.compile(r"^\s*(?:\[tool error\]|\[permission denied\]|error:|读取失败|文件不存在)", re.I)
_SHELL = {"bash", "bash_persistent", "shell", "exec_command"}
_WRITES = {"edit_file", "edit_many", "write_file", "replace_in_file", "apply_patch"}
_DELEGATION = {"delegate_agent", "followup_agent", "send_agent_message"}
_INVESTIGATION = {"grep", "glob", "list_dir", "code_map", "code_search", "git_read"}
_VALIDATION_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)\s*(?:node\s+(?:--check\b|--test\b|"
    r"(?:[^\s'\"]*[/\\])?(?:verify|check|validate|validator|test)[\w.-]*\.(?:mjs|cjs|js)\b)|"
    r"python(?:3)?\s+(?:-m\s+(?:pytest|unittest|py_compile|compileall)\b|"
    r"(?:[^\s'\"]*[/\\])?(?:verify|check|validate|validator|test)[\w.-]*\.py\b)|"
    r"pytest\b|npm(?:\.cmd)?\s+(?:test\b|run\s+(?:test|check|lint|build|typecheck)\b)|"
    r"(?:mvn|mvnw|gradle|gradlew)\s+(?:test|check|compile|build)\b|"
    r"cargo\s+(?:test|check|build)\b|go\s+(?:test|vet|build)\b|git\s+diff\s+--check\b)", re.I,
)
_FUNCTION_NAME = re.compile(
    r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(|"
    r"\b(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(|"
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^\n]*?\)|\w+)\s*=>|"
    r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract)\s+)*"
    r"[A-Za-z_$][\w$<>\[\],.?]*\s+([A-Za-z_$][\w$]*)\s*\(", re.M,
)
_ALL_FUNCTIONS = re.compile(
    r"(?:全部|所有|每个|每一个|各个|逐个)[^，。；！？\n]{0,16}(?:函数|方法)|"
    r"(?:函数|方法)[^，。；！？\n]{0,8}(?:全部|所有|逐个)|"
    r"\b(?:all|each|every)\b[^,;.!?\n]{0,32}\b(?:functions?|methods?)\b", re.I,
)
_JS_NONCODE = re.compile(
    r"/\*[\s\S]*?(?:\*/|\Z)|//[^\r\n]*|"
    r'"(?:\\.|[^"\\])*"|' + r"'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`",
)
_JS_NAMED_FUNCTION = re.compile(
    r"^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?(?:async[ \t]+)?"
    r"function(?:[ \t]*\*[ \t]*|[ \t]+)([A-Za-z_$][\w$]*)[ \t]*\(", re.M,
)
_CHINESE = re.compile(r"[\u3400-\u9fff]")
_DELEGATION_GLOBAL = re.compile(
    r"全项目|整个项目|全仓库|整个仓库|所有文件|全部文件|所有代码|全部代码|"
    r"整个目录|所有模块|全部模块|每个文件|"
    r"\b(?:all\s+(?:source\s+)?files|entire\s+(?:project|repo|directory)|"
    r"whole\s+(?:project|repo)|across\s+(?:the\s+)?(?:project|repo))\b", re.I,
)
_READ_COMMAND = re.compile(
    r"(?:^|[\s;&|])(?:cat|sed|awk|head|tail|nl|less|more|type|Get-Content|gc|rg|grep|findstr)\b|"
    r"\b(?:read_text|read_bytes|readFileSync|readFile|readlines|ReadAllText|ReadAllLines)\s*\(|"
    r"\bopen\s*\([^\n]*\)\s*\.\s*read\s*\(", re.I,
)
_WRITE_COMMAND = re.compile(
    r"\b(?:write_text|write_bytes|writeFileSync|writeFile|Set-Content|Add-Content|Out-File)\b|"
    r"\bWriteAll(?:Text|Lines|Bytes)\b|\bopen\s*\([^\n]*[,\s]['\"](?:w|a|x)[bt+]*['\"]|"
    r"\bsed\s+-[^\s]*i\b|(?:^|[;&|]\s*)(?:echo|printf)\b[^\n]*>\s*[^&]", re.I,
)
_SOURCE_PATH = re.compile(
    r"(?:[A-Za-z]:[/\\])?[\w./\\@+~-]+\.(?:py|pyi|java|js|mjs|cjs|ts|tsx|jsx|vue|svelte|"
    r"go|rs|c|h|cc|cpp|hpp|cs|kt|kts|swift|rb|php|sh|ps1|sql|html|css|scss|lua|dart)(?![\w.])", re.I,
)
_SOURCE_SUFFIX = re.compile(r"\.(?:py|pyi|java|js|mjs|cjs|ts|tsx|jsx|vue|svelte|go|rs|c|h|cc|cpp|hpp|cs|"
                            r"kt|kts|swift|rb|php|sh|ps1|sql|html|css|scss|lua|dart)$", re.I)
_CLAUSES = re.compile(r"[，。；！？;!\n]|然后|并且|同时|并(?:使用|用|运行|执行)", re.I)
_REFERENCE_ONLY = re.compile(r"参考|参照|对照|验证|校验|测试|检查|运行|执行|\b(?:verify|validate|check|test|reference|run)\b", re.I)
_REFERENCE_PREFIX = re.compile(
    r"(?:参考|参照|对照|例如|验证(?:脚本|命令)?|校验(?:脚本)?|测试(?:脚本)?|运行|执行|命令)\s*[:：]?\s*$|"
    r"\b(?:node|python(?:3)?|pytest|bash|pwsh|powershell|verify|validate|reference)\s+(?:-[\w-]+\s+)*$", re.I,
)
_NEGATIVE_SCOPE = re.compile(
    r"(?:不要|不用|不必|无需|别|不能|禁止|暂不|不要改|不改|不修改|不)[^，。；！？,;\n]{0,16}$|"
    r"\b(?:not|never|don['’]t|do\s+not)\b[^,;.!?\n]{0,30}$", re.I,
)
_EXCLUSIVE_CHANGE = re.compile(
    r"(?:只|仅|仅仅|限定)(?:能|允许|在|对)?(?:修改|改动|改|编辑|写入|更新)|"
    r"\b(?:only\s+(?:edit|modify|change|update|write)|(?:edit|modify|change|update)\s+only)\b", re.I,
)
_GENERIC_FILE_PATH = re.compile(
    r"(?:[A-Za-z]:[/\\])?[\w./\\@+~-]+\.(?:py|pyi|java|js|mjs|cjs|ts|tsx|jsx|vue|svelte|"
    r"go|rs|c|h|cc|cpp|hpp|cs|kt|kts|swift|rb|php|sh|ps1|sql|html|css|scss|lua|dart|"
    r"json|toml|ya?ml|ini|cfg|xml|md|txt)(?![\w.])", re.I,
)
_EXTRA_MUTATION_CAPABILITY = re.compile(
    r"技能|插件|专家|展示|预览|截图|演示|(?:生成|绘制|查看|附上|输出)[^，。；！？\n]{0,12}(?:图片|图表|架构图|可视化|PNG|SVG|PDF|PPT)|"
    r"联网|上网|(?:浏览|抓取|搜索|查找|检索|查阅|访问)[^，。；！？\n]{0,18}(?:网页|互联网|在线|官方|https?://)|"
    r"多智能体|子智能体|子代理|多\s*agent|子\s*agent|委派|协调|协作|分工|并行|跨模块|架构重构|整体迁移|"
    r"持久\s*shell|提交代码|创建PR|创建\s*PR|提案|只给方案|"
    r"\b(?:skills?|plugins?|experts?|present|preview|render|visuali[sz]e|browse|internet|"
    r"web\s+search|delegate|handoff|coordinate|multi[- ]?agent|parallel|"
    r"across\s+modules|persistent\s+shell|git\s+commit|pull\s+request|proposal)\b", re.I,
)
_FOCUSED_MUTATION_CORE = {"read_file", "write_file", "edit_file", "edit_many", "grep", "glob", "list_dir", "bash"}
_FOCUSED_MUTATION_AUX = {
    "ask_user_question", "load_workspace_dependencies",
    # Existing lead/child capabilities remain available when their role already
    # exposes them; schema focusing must not take away live coordination.
    "team_list", "delegate_agent", "send_agent_message", "followup_agent",
    "interrupt_agent", "wait_agents", "stop_agent", "read_agent_execution",
}


def _affirmative_scope(pattern: re.Pattern[str], intent: str) -> bool:
    return any(not _NEGATIVE_SCOPE.search(intent[:match.start()]) for match in pattern.finditer(intent))


def _primary_code_work(intent: str) -> bool:
    for clause in re.split(r"[，。；！？,;.!?\n]|然后|并且|同时|以及|并|\band\b", intent, flags=re.I):
        if not _affirmative_scope(_CODE_WORK, clause):
            continue
        # Editing/translating the comment itself remains an annotation job.
        # A separate implementation/fix clause, or work on the comment feature,
        # must keep ordinary development and its dependency/test flexibility.
        if _ANNOTATION.search(clause) and not _ANNOTATION_PRODUCT_WORK.search(clause):
            continue
        return True
    return False


def _source_path(path: str) -> bool:
    return bool(_SOURCE_SUFFIX.search(path) and not any(c in path for c in "\x00\r\n"))


def _annotation_targets(prompt: str) -> list[str]:
    """Exclude reference/check commands without inventing a repository-wide plan."""
    targets: list[str] = []
    for clause in _CLAUSES.split(prompt):
        paths, intent = _paths_and_intent(clause)
        if _REFERENCE_ONLY.search(intent) and not _ANNOTATION.search(intent):
            continue
        for path in paths.values():
            position = clause.find(path)
            if position >= 0 and _REFERENCE_PREFIX.search(clause[:position]):
                continue
            if path not in targets:
                targets.append(path)
    return targets


def _exclusive_write_targets(prompt: str, intent: str) -> list[str]:
    """Extract the affirmative file list belonging to an explicit write limit.

    ``intent`` masks paths without changing offsets. Sentence punctuation in a
    quoted/spaced filename, its suffix, or a Windows drive cannot end the list.
    Later read-only references and test-command arguments are not write targets.
    This is task discipline only; normal tool permissions still own authorization.
    """
    # The shared router intentionally accepts a narrower set of bare-path
    # terminators. Normalize prose commas/terminal periods at equal width here
    # so "only edit a.py, b.py." works without changing quoted filenames.
    normalized = list(prompt)
    for index, character in enumerate(intent):
        if character in ",;!?":
            normalized[index] = {",": "，", ";": "；", "!": "！", "?": "？"}[character]
        elif character == "." and (index + 1 == len(intent) or intent[index + 1].isspace()):
            normalized[index] = "。"
    _, intent = _paths_and_intent("".join(normalized))
    unrestricted = re.compile(
        r"(?:不|无需|不用)(?:限制|限定)(?:本次)?(?:修改|编辑|写入|改动)?(?:文件|路径|范围)|"
        r"(?:文件|修改|写入|编辑)?范围(?:不受限|不限)|"
        r"\b(?:no|without)\s+(?:file|path|write|scope)\s+restrictions?\b|"
        r"\b(?:scope|file\s+scope|write\s+scope)\s+is\s+unrestricted\b|"
        r"\bnot\s+restricted\s+to\b", re.I,
    )
    if unrestricted.search(intent):
        return []
    sentence_end = re.compile(r"[。；;！？!?\n.]")
    clauses = re.compile(r"[,，]|然后|并且|同时|以及|并|\b(?:and|then)\b", re.I)
    readonly = re.compile(r"只读|不可(?:修改|改动|编辑|写入)|不要(?:修改|改动|编辑)|"
                          r"\bread[- ]only\b|\bimmutable\b|\b(?:leave|keep).{0,30}\bunchanged\b", re.I)
    addition = re.compile(r"新增|新建|创建|添加|编写|补充|\b(?:add|create|write|implement)\b", re.I)
    targets: dict[str, str] = {}
    for match in _EXCLUSIVE_CHANGE.finditer(intent):
        if _NEGATIVE_SCOPE.search(intent[:match.start()]):
            continue
        start = match.end()
        stop = sentence_end.search(intent, start)
        end = stop.start() if stop else len(intent)
        cursor = start
        boundaries = [*clauses.finditer(intent, start, end)]
        for boundary in [*boundaries, None]:
            segment_end = boundary.start() if boundary else end
            words = intent[cursor:segment_end]
            # "create tests/new.py as a regression test" remains a write;
            # "run pytest tests/original.py" is a separate read/check action.
            if readonly.search(words) or (_REFERENCE_ONLY.search(words) and not addition.search(words)):
                break
            paths, _ = _paths_and_intent(prompt[cursor:segment_end])
            targets.update(paths)
            cursor = boundary.end() if boundary else end
    return list(targets.values())


class TaskProgress:
    """A bounded per-prompt work queue, advanced only by trusted receipts."""

    BATCH_SIZE = 3
    DEPENDENCY_READS = 2
    ANNOTATION_WARN_TURNS = 3
    ANNOTATION_STOP_TURNS = 10
    MUTATION_WARN_TURNS = 12
    MUTATION_STOP_TURNS = 30
    MUTATION_FOCUS_TURNS = 6
    MUTATION_READ_BATCH = 8
    MUTATION_RECOVERY_QUERIES = 3
    MAX_TRACKED_FILES = 512

    def __init__(self, workspace: Path | str | None, prompt: str, code_context: Any = None):
        self.workspace = str(workspace or "").replace("\\", "/")
        self._windows = bool(re.match(r"^[A-Za-z]:", self.workspace))
        self.enabled = requires_mutation(prompt)
        explicit, intent = _paths_and_intent(prompt)
        self.annotation = (self.enabled and _affirmative_scope(_ANNOTATION, intent)
                           and not _ANNOTATION_PRODUCT_WORK.search(intent) and not _primary_code_work(intent))
        self.verification_plan = VerificationPlan(workspace, prompt)
        limited_paths = _exclusive_write_targets(prompt, intent) if not self.annotation else []
        self.restricted_scope = bool(limited_paths)
        self.function_todo_hint = self.annotation and _affirmative_scope(_ALL_FUNCTIONS, intent)
        self.compact_editing = False
        self.broad = _affirmative_scope(_BROAD, intent)
        paths = (_annotation_targets(prompt) if self.annotation else limited_paths if self.restricted_scope else list(explicit.values()))
        if _affirmative_scope(_WHOLE_SCOPE, intent) and not self.restricted_scope:
            paths = []
        if not paths and not self.broad:
            if isinstance(code_context, dict):
                path = code_context.get("path") or code_context.get("file_path")
            else:
                path = code_context if isinstance(code_context, str) else None
            if path:
                paths = [str(path)]
        self.targets: OrderedDict[str, str] = OrderedDict((self._path(p), p) for p in paths)
        self._focused_mutation_tools = (
            self.enabled and not self.annotation and (not self.broad or self.restricted_scope)
            and 1 <= len(self.targets) <= self.BATCH_SIZE
            and all(_GENERIC_FILE_PATH.fullmatch(path) for path in self.targets.values())
            and not _affirmative_scope(_EXTRA_MUTATION_CAPABILITY, intent)
        )
        # An empty narrow scope anchors to the first source read, rather than
        # letting every new read silently enlarge the user's task.
        self._anchored = bool(paths) or self.broad
        self._inferred_target: str | None = None
        self._active: OrderedDict[str, str] = OrderedDict()
        self._discovered: OrderedDict[str, str] = OrderedDict()
        self._dependencies: set[str] = set()
        self._read: set[str] = set()
        self._changed: set[str] = set()
        self._verified: set[str] = set()
        self._validation_receipts: set[str] = set()
        self._noop: set[str] = set()
        self._delivered: set[str] = set()
        self._reservations: set[str] = set()
        self._read_windows: dict[str, dict[tuple[int, int], int]] = {}
        self._full_reads: dict[str, int] = {}
        self._complete_reads: set[str] = set()
        self._line_coverage: dict[str, set[int]] = {}
        self._declaration_lines: dict[str, dict[str, int]] = {}
        self._fresh_windows: dict[str, int] = {}
        self._implementation_locked = False
        self._seen_functions: dict[str, list[str]] = {}
        self._commented_functions: dict[str, set[str]] = {}
        self._function_docs: dict[str, dict[str, str]] = {}
        self._function_todo_untracked: set[str] = set()
        self._edit_receipts: OrderedDict[str, list[str]] = OrderedDict()
        self._validation_failures: OrderedDict[str, str] = OrderedDict()
        self._recovery_reads: dict[str, int] = {}
        self._without_delivery = 0
        self._batch_delivered = False
        self._last_block = ""
        self._working_reads: OrderedDict[str, str] = OrderedDict()
        self._query_counts: dict[tuple[str, str], int] = {}
        self._recovery_queries = 0
        self._focused_queries = 0
        self._change_generation: dict[str, int] = {}
        self._failure_versions: dict[str, tuple[int, str]] = {}
        self._edit_recovery_pending: set[str] = set()
        self._recovery_related: set[str] = set()
        self._compacted_read_ids: set[str] = set()
        self._context_restore_reads: dict[str, int] = {}
        self._context_restore_windows: dict[str, set[int]] = {}

    @property
    def focused_mutation_tools(self) -> bool:
        """Whether a concrete small development request uses its focused menu.

        This changes only advertised schemas, never the role's registry or the
        permission policy. Explicit wider/capability requests keep the full menu.
        """
        return self._focused_mutation_tools

    def _path(self, raw: str) -> str:
        path = str(raw or "").replace("\\", "/")
        if not (path.startswith("/") or re.match(r"^[A-Za-z]:/", path)):
            path = posixpath.join(self.workspace, path)
        path = posixpath.normpath(path)
        return path.casefold() if self._windows or re.match(r"^[A-Za-z]:/", path) else path

    def tool_schemas(self, schemas: list[dict[str, Any]], compact: bool = False) -> list[dict[str, Any]]:
        """Shrink model-side edit payloads without changing shared tool limits.

        Large exact replacements duplicate function bodies in model output and
        can hit output limits before even one edit is delivered. Ordinary
        development uses short exact anchors while retaining room for complete
        new implementations; annotations can usually anchor on a declaration.
        The concrete
        tools keep their original limits, permissions and runtime validation.
        """
        if not self.annotation:
            return self._mutation_schemas(schemas, compact=compact) if self.enabled else schemas
        copied = deepcopy(schemas)
        if self.phase == "implement":
            copied = [entry for entry in copied if not (
                isinstance(entry, dict) and (
                    (entry.get("function") if isinstance(entry.get("function"), dict) else entry).get("name") in _INVESTIGATION
                    or str((entry.get("function") if isinstance(entry.get("function"), dict) else entry).get("name", "")).startswith("git_")
                ))]
        small = bool(compact or self.compact_editing)
        max_items, old_length, new_length = (1, 1000, 2500) if small else (5, 2000, 4000)
        guidance = (
            " Annotation task: for native edit_file/edit_many, replace only the unique function declaration and insert "
            "a short Chinese JSDoc/docstring immediately before it (inside a Python function "
            "for a docstring). Do not copy the function body into native replacement arguments. "
            f"Use at most {max_items} small replacement(s) per native call. For repetitive annotation work, "
            "an inline Node/Python editing script explicitly limited to the <=3 already-read batch files is allowed; "
            "preserve behavior and actually validate. No project-wide traversal or dynamic unknown targets."
        )
        for entry in copied:
            if not isinstance(entry, dict):
                continue
            schema = entry.get("function") if isinstance(entry.get("function"), dict) else entry
            name = schema.get("name")
            if self.phase == "implement" and name == "read_file":
                schema["description"] = str(schema.get("description") or "") + (
                    " Implementation phase: do not reread fully visible files. Only unread active-batch files, "
                    "new uncovered windows of at most 120 lines, or one <=60-line error recovery are allowed."
                )
            if self.phase == "implement" and name in _SHELL:
                schema["description"] = str(schema.get("description") or "") + (
                    " Implementation phase: only actual bounded edits or explicit syntax/test validation. "
                    "Inline batch-edit scripts may read/write ONLY the <=3 explicit already-read target paths. "
                    "Do not run project traversal, source-only readers, directory discovery or arbitrary shell probes."
                )
            shell_parameters = schema.get("parameters")
            if (name in _SHELL and isinstance(shell_parameters, dict)
                    and "_approval" in shell_parameters.get("required", [])):
                schema["description"] = str(schema.get("description") or "") + (
                    ' Include the already-required public explanation metadata in the SAME call: '
                    '"_approval":{"purpose":"本次验证目的","effects":["将检查的具体文件及预期结果"]}. '
                    "Describe this actual command; this metadata does not grant permission or bypass approval."
                )
            if name not in {"edit_file", "edit_many"}:
                continue
            schema["description"] = str(schema.get("description") or "") + guidance
            parameters = schema.get("parameters")
            if not isinstance(parameters, dict):
                continue
            properties = parameters.get("properties")
            if not isinstance(properties, dict):
                continue
            if name == "edit_many":
                edits = properties.get("edits")
                if not isinstance(edits, dict):
                    continue
                existing = edits.get("maxItems")
                edits["maxItems"] = min(max_items, existing) if isinstance(existing, int) else max_items
                edits["description"] = str(edits.get("description") or "") + guidance
                items = edits.get("items")
                properties = items.get("properties") if isinstance(items, dict) else None
                if not isinstance(properties, dict):
                    continue
            for field, limit in (("old_string", old_length), ("new_string", new_length)):
                value = properties.get(field)
                if isinstance(value, dict):
                    existing = value.get("maxLength")
                    value["maxLength"] = min(limit, existing) if isinstance(existing, int) else limit
                    value["description"] = str(value.get("description") or "") + (
                        " Keep only the unique declaration anchor; omit function bodies."
                    )
        return copied

    def _mutation_schemas(self, schemas: list[dict[str, Any]], *, compact: bool = False) -> list[dict[str, Any]]:
        small = bool(compact or self.compact_editing)
        copied = deepcopy(schemas)
        focused = self.focused_mutation_tools
        available_names = {str((entry.get("function") if isinstance(entry.get("function"), dict) else entry).get("name") or "")
                           for entry in copied if isinstance(entry, dict)}
        # A restricted/custom role may expose only proposal tools or a different
        # native editor/shell. Preserve that existing execution route too.
        alternatives = ({"apply_patch", "replace_in_file"} if not available_names & {"edit_file", "edit_many", "write_file"} else set())
        if not available_names & _WRITES:
            alternatives.add("propose_edit")
        if "bash" not in available_names:
            alternatives.add("bash_persistent")
        complete_current_result = self.ready_to_finish and self.verification_plan.has_current_full_result
        investigation_available = (not complete_current_result
                                   and (self.phase == "investigate" or self._recovery_queries > 0
                                        or self.phase == "implement" and self._focused_queries < 2))
        if focused:
            def advertised(entry: Any) -> bool:
                if not isinstance(entry, dict):
                    return False
                schema = entry.get("function") if isinstance(entry.get("function"), dict) else entry
                name = str(schema.get("name") or "")
                if name in _INVESTIGATION and not investigation_available:
                    return False
                return name in _FOCUSED_MUTATION_CORE or name in _FOCUSED_MUTATION_AUX or name in alternatives or name.startswith("job_")

            copied = [entry for entry in copied if advertised(entry)]
        local_edit_guidance = (
            " Prefer precise local replacements in existing files; do not regenerate unchanged function bodies "
            "or whole files. Preserve complete new implementations; batch independent sites in edit_many."
        )
        for entry in copied:
            if not isinstance(entry, dict):
                continue
            schema = entry.get("function") if isinstance(entry.get("function"), dict) else entry
            name = schema.get("name")
            hint = {
                "read_file": " For a bounded read, pass offset and/or limit (limit alone starts at line 1). Use actual grep path:line hits for the read window instead of guessing offsets. Locate required entry points before editing; batch independent file reads in one turn and reuse current-version source already delivered.",
                "edit_many": " Complete related entry point, implementation and regression changes in one small batch before the full validation. Batch independent exact edits in this call instead of one model round per site.",
                "bash": " Inspect source with read_file/grep/glob/list_dir. Use shell for actual builds/tests/edits. Reuse real failure diagnostics; do not rerun an unchanged failure merely to change output formatting.",
                "bash_persistent": " Inspect source with native file tools. Use shell for actual builds/tests/edits; reuse the latest real diagnostic before rerunning an unchanged failing version.",
            }.get(name, "")
            if hint:
                schema["description"] = str(schema.get("description") or "") + hint
            if name in {"edit_file", "edit_many", "replace_in_file", "apply_patch"}:
                schema["description"] = str(schema.get("description") or "") + local_edit_guidance
            elif name == "write_file":
                schema["description"] = str(schema.get("description") or "") + (
                    " New files may contain the complete necessary implementation or tests. For existing files, "
                    "prefer edit_file/edit_many for local changes; use a complete replacement only when the "
                    "task actually requires rewriting that file. Do not regenerate unchanged surrounding code."
                )
            if name in {"edit_file", "edit_many"}:
                parameters = schema.get("parameters")
                properties = parameters.get("properties") if isinstance(parameters, dict) else None
                if name == "edit_many" and isinstance(properties, dict):
                    edits = properties.get("edits")
                    if isinstance(edits, dict):
                        items = edits.get("items")
                        properties = items.get("properties") if isinstance(items, dict) else None
                    else:
                        properties = None
                if isinstance(properties, dict):
                    old = properties.get("old_string")
                    if isinstance(old, dict):
                        cap = old.get("maxLength")
                        old["maxLength"] = min(4000, cap) if isinstance(cap, int) else 4000
                        old["description"] = str(old.get("description") or "") + (
                            " Shortest unique exact current-version span, with only matching context. "
                            "Separate distant changes in one edit_many call."
                        )
                    new = properties.get("new_string")
                    if isinstance(new, dict):
                        # A short matching anchor must not truncate a necessary
                        # new algorithm, handler, component, or regression test.
                        new["description"] = str(new.get("description") or "") + (
                            " Complete necessary replacement for this span; retain the anchor when inserting "
                            "and omit unchanged surrounding code."
                        )
            if focused and self.phase in {"implement", "recover"} and name in _INVESTIGATION:
                schema["description"] = str(schema.get("description") or "") + (
                    " Implementation/recovery: only a specific file or directory path; "
                    "use the remaining targeted lookup allowance, not a whole-project investigation."
                )
        if not small:
            return copied
        guidance = (
            " Recover from the output limit with one small, complete edit at a time. "
            "Use a unique exact anchor and preserve necessary function bodies and valid syntax. "
            "Do not repeat unchanged surrounding code. New implementation/test/config files may use write_file; "
            "keep each new module bounded and continue in later calls if needed. Then run the relevant real validation."
        )
        for entry in copied:
            if not isinstance(entry, dict):
                continue
            schema = entry.get("function") if isinstance(entry.get("function"), dict) else entry
            name = schema.get("name")
            if name not in {"edit_file", "edit_many", "write_file", "apply_patch"}:
                continue
            schema["description"] = str(schema.get("description") or "") + guidance
            parameters = schema.get("parameters", {})
            properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
            if name == "edit_many" and isinstance(properties, dict):
                edits = properties.get("edits")
                if not isinstance(edits, dict):
                    continue
                cap = edits.get("maxItems")
                edits["maxItems"] = min(1, cap) if isinstance(cap, int) else 1
                item = edits.get("items", {})
                properties = item.get("properties", {}) if isinstance(item, dict) else {}
            if not isinstance(properties, dict):
                continue
            for field, limit in (("old_string", 3000), ("new_string", 5000), ("content", 7000)):
                value = properties.get(field)
                if isinstance(value, dict):
                    cap = value.get("maxLength")
                    value["maxLength"] = min(limit, cap) if isinstance(cap, int) else limit
        return copied

    @staticmethod
    def _window(args: dict[str, Any]) -> tuple[int, int]:
        try:
            return read_window(args) or (1, 200)
        except (ValueError, TypeError):
            # Schema validation owns malformed arguments; this policy should not
            # crash before the normal tool validation can explain them.
            return 1, 200

    @staticmethod
    def _paths(name: str, args: dict[str, Any]) -> list[str]:
        if name == "edit_many":
            edits = args.get("edits")
            if not isinstance(edits, list):
                return []
            return [str(item.get("path")) for item in edits
                    if isinstance(item, dict) and item.get("path")]
        if name == "apply_patch":
            patch = str(args.get("patch") or args.get("input") or "")
            return re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.M)
        path = args.get("path") or args.get("file_path")
        return [str(path)] if path else []

    def _deny(self, message: str) -> str:
        self._last_block = message
        return "[任务推进] " + message

    def _scope_ok(self, path: str) -> bool:
        root = self._path(".").rstrip("/")
        within = path == root or path.startswith(root + "/")
        return bool(self.broad and not self.targets and within or path in self.targets)

    def _reserve(self, paths: list[str], *, reading: bool) -> str | None:
        keys = list(dict.fromkeys(self._path(p) for p in paths))
        first_source = next((p for p in paths if _source_path(p)), None)
        if not self._anchored and first_source:
            anchor = self._path(first_source)
            self.targets[anchor] = first_source
            self._anchored = True
            self._inferred_target = anchor
        targets, dependencies = [], []
        for key in keys:
            metadata = self.broad and not self.targets and not _source_path(key)
            if self._scope_ok(key) and not (reading and metadata):
                targets.append(key)
            elif reading:
                dependencies.append(key)
            else:
                return self._deny("修改超出本次目标范围；只修改 " + self._labels(self.targets) + "。")
        if len(self._dependencies | set(dependencies)) > self.DEPENDENCY_READS:
            return self._deny("依赖调查已达上限，先完成当前目标的修改和验证，不继续扩大读取范围。")
        new = [key for key in targets if key not in self._active and (not reading or key not in self._delivered)]
        if len(self._active) + len(new) > self.BATCH_SIZE:
            return self._deny("本批最多 3 个文件；先修改并验证当前批次，再读取下一批。当前批次："
                              + self._labels(self._active))
        # Reserve before execution, so parallel calls cannot all pass an empty
        # active queue and each open their own fourth/fifth file.
        for raw, key in zip(paths, (self._path(p) for p in paths)):
            if key in new:
                self._active[key] = raw
                self._reservations.add(key)
        self._dependencies.update(dependencies)
        return None

    def _mutation_scope_ok(self, key: str) -> bool:
        root = self._path(".").rstrip("/")
        if self.restricted_scope:
            return key in self.targets
        return key in self.targets or key == root or key.startswith(root + "/")

    def _project_read(self, key: str) -> bool:
        """Tool-result artifacts remain evidence, not project work targets."""
        root = self._path(".").rstrip("/")
        return key in self.targets or key == root or key.startswith(root + "/")

    def _reserve_mutation_writes(self, paths: list[str]) -> str | None:
        keys = list(dict.fromkeys(self._path(path) for path in paths))
        if any(not self._mutation_scope_ok(key) for key in keys):
            scope = self._note_labels(self.targets) if self.restricted_scope else "当前项目及用户明确指定的文件"
            return self._deny("修改超出本次目标范围；只修改 " + scope + "。")
        new = [key for key in keys if key not in self._active]
        recovering = (self.phase == "recover" and all(key in self._read or key in self._recovery_related for key in new))
        maximum = self.BATCH_SIZE + 2 if recovering else self.BATCH_SIZE
        if len(self._active) + len(new) > maximum:
            return self._deny("本批最多修改 3 个文件；先验证本批已修改文件，再推进后续实现或测试文件。当前批次："
                              + self._note_labels(self._active))
        for path in paths:
            self._active.setdefault(self._path(path), path)
        return None

    def _new_file_write(self, name: str, args: dict[str, Any], path: str) -> bool:
        if name == "apply_patch":
            patch = str(args.get("patch") or args.get("input") or "")
            return path in re.findall(r"^\*\*\* Add File: (.+)$", patch, re.M)
        if name != "write_file":
            return False
        # Normal file tools still own creation/overwrite validation. This check
        # merely avoids demanding an impossible read of a not-yet-created file.
        try:
            return not Path(self._path(path)).exists()
        except OSError:
            return False

    def _before_mutation_tool(self, name: str, args: dict[str, Any]) -> str | None:
        paths = self._paths(name, args)
        if name in _INVESTIGATION or name.startswith("git_"):
            query = (name, repr(sorted(args.items())))
            if self._query_counts.get(query, 0) >= 2:
                return self._deny("同一查询已有两次结果；复用现有定位结果，继续实现或按具体错误定点排查。")
            if self.phase in {"implement", "recover"}:
                raw = str(args.get("path") or args.get("file_path") or "")
                scoped = raw not in {"", ".", "./", self.workspace} and "**" not in raw
                if self._recovery_queries and scoped:
                    self._recovery_queries -= 1
                elif self.phase == "implement" and scoped and self._focused_queries < 2:
                    self._focused_queries += 1
                else:
                    return self._deny("本批已有实现上下文；先修改并验证，避免重新遍历全仓。实际错误恢复可作有限的目录/文件定点定位。")
            return None
        if name in _DELEGATION:
            child_prompt = str(args.get("message") if name == "send_agent_message" else args.get("prompt") or "")
            child_paths = _annotation_targets(child_prompt)
            if child_paths and (self.restricted_scope or self.phase in {"implement", "recover"}):
                return self._reserve_mutation_writes(child_paths)
            if self.phase in {"implement", "recover"} and not child_paths:
                return self._deny("实施中的子任务请明确列出本批文件和具体待交付结果，不重新委派整个项目调查。")
            return None
        if name in _SHELL:
            command = str(args.get("command") or args.get("cmd") or "")
            writing = bool(_WRITE_COMMAND.search(command))
            if _READ_COMMAND.search(command) and not writing and not inline_python_validation_paths(command):
                return self._deny("请用 read_file/grep 读取和定位源码，复用已读窗口；Shell 可执行实际修改、构建和测试。")
            if writing:
                shell_paths = list(dict.fromkeys(_GENERIC_FILE_PATH.findall(command)))
                if not shell_paths:
                    return self._deny("Shell 修改需要显式列出本批目标文件；动态未知目标请改用原生编辑工具。")
                if reason := self._reserve_mutation_writes(shell_paths):
                    return reason
                if any(self._path(path) not in self._read and Path(self._path(path)).exists() for path in shell_paths):
                    return self._deny("先读取本批已有文件再执行脚本写入；新建实现、测试和配置文件可以直接创建。")
            # Unknown shell is not presumed a write or a validation receipt.
            # It may legitimately install/build/run a project, but cannot wash
            # the no-delivery counter; only the loop's disk receipts can do so.
            return None
        if name == "read_file" and paths:
            key = self._path(paths[0])
            window = self._window(args)
            recovering = False
            if key in self._read:
                restoring = self._consume_context_restore(key, args)
                if restoring:
                    recovering = True
                counts = self._read_windows.get(key, {})
                repeated = counts.get(window, 0) >= 2 or (window[0] == 1 and window[1] > 120 and self._full_reads.get(key, 0) >= 2)
                failure_version = self._failure_versions.get(key)
                current_failure = (failure_version is not None
                                   and failure_version[0] == self._change_generation.get(key, 0))
                unseen_changed_window = (key in self._changed and key not in self._complete_reads
                                         and self._allow_mutation_window(key, args))
                # A failed baseline remains unverified after an edit, but its
                # exhausted read allowance belongs to the old source version.
                # Once the failure's one reread is used, a genuinely unseen
                # current-version tail still fits the two-window edit budget.
                if (self.phase == "recover" and current_failure
                        and not (unseen_changed_window and not self._recovery_reads.get(key, 0))):
                    repeated = True
                if self.phase in {"implement", "recover"} and key in self._complete_reads:
                    repeated = True
                if repeated and not recovering:
                    if (has_read_window(args) and window[1] <= 120
                            and self._recovery_reads.get(key, 0)):
                        self._recovery_reads[key] -= 1
                        recovering = True
                    else:
                        return self._deny("这个源码窗口已经读取；保留已有实现上下文，实际编辑或测试失败仅补读 1 次不超过 120 行的相关窗口。")
                if (self.phase in {"implement", "recover"} and not recovering and key not in self._complete_reads
                        and not self._allow_mutation_window(key, args)):
                    return self._deny("实施阶段只补读真正未覆盖的小窗口，避免反复读取同一源码；按当前实现继续修改或验证。")
                if (self.phase in {"implement", "recover"} and key in self._changed
                        and (not recovering or unseen_changed_window)):
                    if self._fresh_windows.get(key, 0) >= 2:
                        return self._deny("当前新版本已补读两个必要窗口；复用本批上下文完成实现和验证，不重新展开全文。")
                    self._fresh_windows[key] = self._fresh_windows.get(key, 0) + 1
            elif (self._project_read(key) and key not in self._working_reads
                    and len(self._working_reads) >= self.MUTATION_READ_BATCH):
                error_dependency = (self.phase == "recover" and key in self._recovery_related
                                    and len(self._working_reads) < self.MUTATION_READ_BATCH + self.MUTATION_RECOVERY_QUERIES)
                if not error_dependency:
                    return self._deny("本批已读取 8 个实现/依赖/测试文件；先据此修改并验证。真实错误可额外读取最多3个错误/定点查询明确涉及的依赖，避免全仓调查。")
            if self._project_read(key):
                self._working_reads.setdefault(key, paths[0])
            return None
        if name in _WRITES and paths:
            if any(self._path(path) not in self._read and not self._new_file_write(name, args, path) for path in paths):
                return self._deny("先读取本批已有文件再精确编辑；新建文件可使用 write_file 或 apply_patch Add File。")
            return self._reserve_mutation_writes(paths)
        return None

    def _allow_mutation_window(self, key: str, args: dict[str, Any]) -> bool:
        if not has_read_window(args):
            return False
        first, limit = self._window(args)
        covered = self._line_coverage.get(key, set())
        if not covered:
            # Unnumbered partial tool output has no reliable coverage map.
            return limit <= 120 and self._read_windows.get(key, {}).get((first, limit), 0) < 2
        requested = set(range(first, first + limit))
        return limit <= 120 and bool(requested - covered) and len(requested & covered) <= 20

    def _consume_context_restore(self, key: str, args: dict[str, Any]) -> bool:
        """Consume one bounded, distinct restore window without advancing work."""
        remaining = self._context_restore_reads.get(key, 0)
        first, limit = self._window(args)
        if not remaining or not has_read_window(args) or limit > 120:
            return False
        requested = set(range(first, first + limit))
        restored = self._context_restore_windows.setdefault(key, set())
        # Small boundary overlap preserves exact anchors (e.g. 1-120 followed
        # by 119-170); repeating a head window cannot consume the tail credit.
        if not requested - restored or len(requested & restored) > 20:
            return False
        restored.update(requested)
        if remaining > 1:
            self._context_restore_reads[key] = remaining - 1
        else:
            self._context_restore_reads.pop(key, None)
        return True

    def before_tool(self, name: str, args: dict[str, Any]) -> str | None:
        if not self.enabled:
            return None
        if not self.annotation:
            return self._before_mutation_tool(name, args if isinstance(args, dict) else {})
        args = args if isinstance(args, dict) else {}
        if self.phase == "implement" and (name in _INVESTIGATION or name.startswith("git_")):
            return self._deny("本批已进入实施阶段，保持至本批验证完成；停止搜索和目录调查，继续补齐本批全部要求后再验证。")
        if name in _DELEGATION:
            child_prompt = str(args.get("message") if name == "send_agent_message" else args.get("prompt") or "")
            _, child_intent = _paths_and_intent(child_prompt)
            if _affirmative_scope(_DELEGATION_GLOBAL, child_intent):
                return self._deny("注释子任务必须限定当前批次的具体文件，不能把全项目或未界定范围再次委派出去。")
            child_paths = _annotation_targets(child_prompt)
            if not child_paths:
                return self._deny("注释子任务必须在 prompt/message 中显式列出本批目标文件路径；仅说继续或处理整个项目没有边界。")
            if reason := self._reserve(child_paths, reading=False):
                return reason
            return None
        if name in _SHELL:
            command = str(args.get("command") or args.get("cmd") or "")
            writing = bool(_WRITE_COMMAND.search(command))
            inline_validation = bool(inline_python_validation_paths(command))
            validating = bool(_VALIDATION_COMMAND.search(command)) or inline_validation
            if _READ_COMMAND.search(command) and not writing and not inline_validation:
                return self._deny("注释任务请用 read_file 读取当前批次，避免用 Shell 反复展开源码；Shell 仍可执行实际修改或验证。")
            if writing:
                paths = list(dict.fromkeys(_SOURCE_PATH.findall(command)))
                if not paths:
                    return self._deny("Shell 修改无法确定目标路径；请使用 edit_file/edit_many 修改已读取的本批文件。")
                if reason := self._reserve(paths, reading=False):
                    return reason
                if any(self._path(p) not in self._read for p in paths):
                    return self._deny("先用 read_file 读取本批待修改文件，再执行写入。")
            elif self.phase == "implement" and not validating:
                return self._deny("实施阶段只允许实际编辑或明确的测试/语法验证命令；未知 Shell、ls/目录调查不会推进本次任务。")
            return None
        paths = self._paths(name, args)
        if name == "read_file" and paths:
            key = self._path(paths[0])
            recovering = False
            window = self._window(args)
            if (key not in self._active and key not in self._delivered and self._scope_ok(key)
                    and len(self._active) >= self.BATCH_SIZE):
                return self._deny("本批最多 3 个文件；先修改并验证当前批次，再读取下一批。当前批次：" + self._labels(self._active))
            restoring = key in self._read and self._consume_context_restore(key, args)
            if restoring:
                recovering = True
            if window[0] == 1 and window[1] > 60 and self._full_reads.get(key, 0) >= 2 and not recovering:
                return self._deny("这个文件全文已经读取两次；保留现有源码上下文，按未完成函数继续编辑，错误恢复只读必要的小窗口。")
            if self.phase == "implement":
                unread_batch = (key not in self._read and self._scope_ok(key)
                                and (key in self._active or len(self._active) < self.BATCH_SIZE))
                if not unread_batch and not recovering:
                    first, limit = window
                    if (has_read_window(args) and key in self._read and limit <= 60
                            and self._recovery_reads.get(key)):
                        self._recovery_reads[key] -= 1
                        recovering = True
                    elif self._allow_new_window(key, args):
                        self._fresh_windows[key] = self._fresh_windows.get(key, 0) + 1
                    else:
                        return self._deny("实施阶段不再重读已知源码；完整文件继续补齐注释。部分读取只允许当前批次真正未覆盖的 <=120 行窗口；实际错误仅 1 次 <=60 行恢复。")
            if reason := self._reserve(paths, reading=True):
                return reason
            counts = self._read_windows.setdefault(key, {})
            if counts.get(window, 0) >= 2 and not recovering:
                return self._deny("这个源码窗口已经读取两次；请根据现有内容实施修改，匹配失败时只重读必要的小窗口。")
            # Count successful reads in observe(), so a denied/missing file does
            # not consume a read allowance or a permanent batch slot.
            return None
        if name in _WRITES and paths:
            if reason := self._reserve(paths, reading=False):
                return reason
            if any(self._path(p) not in self._read for p in paths):
                return self._deny("先用 read_file 读取本批待修改文件，再执行写入。")
        return None

    def _allow_new_window(self, key: str, args: dict[str, Any]) -> bool:
        if (key not in self._active or key in self._complete_reads or not has_read_window(args)
                or self._fresh_windows.get(key, 0) >= 2):
            return False
        first, limit = self._window(args)
        covered = self._line_coverage.get(key, set())
        if limit > 120 or not covered:
            return False
        requested = set(range(first, first + limit))
        overlap = len(requested & covered)
        first_gap = 1
        while first_gap in covered:
            first_gap += 1
        return bool(requested - covered) and overlap <= 20 and first <= first_gap + 20

    def _capture_read(self, key: str, args: dict[str, Any], result: str) -> None:
        whole = not has_read_window(args)
        try:
            byte_cap = int(args.get("max_bytes") or 65536)
        except (TypeError, ValueError):
            byte_cap = 65536
        clipped = whole and (byte_cap <= 0 or len(result.encode("utf-8", errors="replace")) >= byte_cap)
        lines, partial = ExecutionProgress._delivered(result, possibly_clipped=clipped)
        numbered = []
        for line in lines:
            match = re.match(r"^\s*(\d+)\t(.*)$", line)
            if match and not whole:
                numbered.append((int(match[1]), match[2]))
        if whole:
            numbered = list(enumerate(lines, 1))
            if not partial:
                self._complete_reads.add(key)
        covered = self._line_coverage.setdefault(key, set())
        declarations = self._declaration_lines.setdefault(key, {})
        for number, source in numbered:
            covered.add(number)
            for match in _FUNCTION_NAME.finditer(source):
                function = next((value for value in match.groups() if value), "")
                if function:
                    declarations[function] = number
        if whole or numbered and numbered[0][0] == 1:
            self._capture_function_todos(key, "\n".join(source for _, source in numbered), complete=whole and not partial)
        if not whole and numbered and not partial and len(numbered) < self._window(args)[1]:
            last = numbered[-1][0]
            if all(number in covered for number in range(1, last + 1)):
                self._complete_reads.add(key)

    def _shift_read_coverage(self, name: str, args: dict[str, Any], key: str) -> None:
        edits = args.get("edits", []) if name == "edit_many" else [args] if name == "edit_file" else []
        if not isinstance(edits, list):
            return
        for edit in edits:
            if not isinstance(edit, dict) or self._path(str(edit.get("path") or "")) != key:
                continue
            old, new = str(edit.get("old_string") or ""), str(edit.get("new_string") or "")
            delta = new.count("\n") - old.count("\n")
            if not delta:
                continue
            match = _FUNCTION_NAME.search(old)
            function = next((value for value in match.groups() if value), "") if match else ""
            positions = self._declaration_lines.get(key, {})
            if function not in positions:
                continue
            first = positions[function] - old[:match.start()].count("\n")
            old_end = first + old.count("\n")
            covered = self._line_coverage.get(key, set())
            self._line_coverage[key] = ({line if line < first else line + delta for line in covered if line < first or line > old_end}
                                       | set(range(first, first + new.count("\n") + 1)))
            self._declaration_lines[key] = {fn: line if line < first else line + delta
                                           for fn, line in positions.items() if line < first or line > old_end}
            for declaration in _FUNCTION_NAME.finditer(new):
                fn = next((value for value in declaration.groups() if value), "")
                if fn:
                    self._declaration_lines[key][fn] = first + new[:declaration.start()].count("\n")

    def observe(self, name: str, args: dict[str, Any], result: Any, *,
                changed_paths: Iterable[str] = (), verified_paths: Iterable[str] = ()) -> None:
        if not self.enabled:
            return
        changed_paths = tuple(changed_paths)
        if not self.annotation:
            self.verification_plan.record_changes(changed_paths)
        failed = bool(_FAILED.match(str(result or "")))
        if failed and name in _WRITES:
            for raw in self._paths(name, args):
                key = self._path(raw)
                if key in self._read:
                    if self.annotation:
                        self._recovery_reads[key] = 1
                    else:
                        self._edit_recovery_pending.add(key)
                        self._grant_mutation_recovery(key, "edit:" + str(result or "")[:180])
                if not self.annotation and key not in self._changed:
                    self._active.pop(key, None)
        if not self.annotation and not failed and (name in _INVESTIGATION or name.startswith("git_")):
            query = (name, repr(sorted(args.items())))
            self._query_counts[query] = self._query_counts.get(query, 0) + 1
            while len(self._query_counts) > 128:
                self._query_counts.pop(next(iter(self._query_counts)))
            if self.phase == "recover":
                self._recovery_related.update(self._path(path) for path in _GENERIC_FILE_PATH.findall(str(result or "")))
        if self.annotation and self.broad and not self.targets and not failed:
            self._discover(name, args, str(result or ""))
        if name == "read_file":
            for raw in self._paths(name, args):
                key = self._path(raw)
                if failed:
                    if key not in self._read:
                        self._working_reads.pop(key, None)
                    if key in self._reservations and key not in self._read:
                        self._active.pop(key, None)
                    self._reservations.discard(key)
                    if key == self._inferred_target and not self._read and not self._active:
                        self.targets.pop(key, None)
                        self._inferred_target = None
                        self._anchored = False
                    continue
                self._read.add(key)
                self._reservations.discard(key)
                window = self._window(args)
                counts = self._read_windows.setdefault(key, {})
                counts[window] = counts.get(window, 0) + 1
                if not self._project_read(key):
                    # Reuse the bounded read ledger's insertion order for the
                    # single external evidence slot; failures never reach here.
                    self._read_windows.pop(key)
                    self._read_windows[key] = counts
                if window[0] == 1 and window[1] > 60:
                    self._full_reads[key] = self._full_reads.get(key, 0) + 1
                self._capture_read(key, args, str(result or ""))
        for raw in changed_paths:
            key = self._path(str(raw))
            if (self._scope_ok(key) if self.annotation else self._mutation_scope_ok(key)):
                self._active.setdefault(key, str(raw))
                self._implementation_locked = True
            self._change_generation[key] = self._change_generation.get(key, 0) + 1
            self._context_restore_reads.pop(key, None)
            self._context_restore_windows.pop(key, None)
            self._edit_recovery_pending.discard(key)
            if not self.annotation:
                self._recovery_reads.pop(key, None)
            if self.annotation:
                # Annotation's exact native edits retain shifted declaration
                # positions for its function todo ledger. Product changes can
                # replace arbitrary bodies, so their old coverage is stale.
                self._shift_read_coverage(name, args, key)
            else:
                self._complete_reads.discard(key)
                self._read_windows.pop(key, None)
                self._full_reads.pop(key, None)
                self._line_coverage.pop(key, None)
                self._declaration_lines.pop(key, None)
            self._fresh_windows[key] = 0
            if name == "write_file" and self._path(str(args.get("path") or "")) == key:
                # A successful native write has already delivered the complete
                # new source to the model. Do not demand a read of its own file.
                self._read.add(key)
                self._capture_read(key, {}, str(args.get("content") or ""))
            self._changed.add(key)
            self._verified.discard(key)
            self._validation_receipts.discard(key)
            self._noop.discard(key)
            self._delivered.discard(key)
            if self.annotation:
                self._validation_failures.pop(key, None)
            self._remember_edits(name, args, key)
            self._update_function_todos(name, args, key)
            if name not in {"edit_file", "edit_many"}:
                # Scripts/children have disk receipts but no per-function
                # native replacement arguments. Unknown is not "all missing".
                self._function_todo_untracked.add(key)
            self._batch_delivered = True
            self._last_block = ""
        for raw in verified_paths:
            key = self._path(str(raw))
            fresh_validation = key not in self._validation_receipts
            if key in self._read or key in self._changed:
                self._validation_receipts.add(key)
                if not self.annotation:
                    # A baseline can fail on an unchanged implementation or
                    # dependency. A subsequent real check clears that failure
                    # without inventing an edit or marking read-only code done.
                    self._validation_failures.pop(key, None)
                    self._recovery_reads.pop(key, None)
            if key in self._changed:
                first_verification = key not in self._verified
                self._verified.add(key)
                self._delivered.add(key)
                self._validation_failures.pop(key, None)
                self._recovery_reads.pop(key, None)
                if first_verification or fresh_validation:
                    self._batch_delivered = True
                    self._last_block = ""
        # Only a whole completed batch opens slots. A single edited file must
        # not hide the other two unread/unmodified files in the same batch.
        if self._active and all(key in self._verified for key in self._active):
            self._active.clear()
            self._implementation_locked = False
            if not self.annotation:
                self._working_reads.clear()
                self._focused_queries = 0
                self._recovery_queries = 0
                self._recovery_related.clear()
        while len(self._read_windows) > self.MAX_TRACKED_FILES:
            self._read_windows.pop(next(iter(self._read_windows)))

    def _remember_edits(self, name: str, args: dict[str, Any], key: str) -> None:
        edits = args.get("edits", []) if name == "edit_many" else [args] if name == "edit_file" else []
        if not isinstance(edits, list):
            return
        names = self._edit_receipts.setdefault(key, [])
        for edit in edits:
            if not isinstance(edit, dict) or self._path(str(edit.get("path") or "")) != key:
                continue
            for match in _FUNCTION_NAME.finditer(str(edit.get("old_string") or "") + "\n" + str(edit.get("new_string") or "")):
                function = next((value for value in match.groups() if value), "")
                if function and function not in names:
                    names.append(function)
        del names[:-12]
        self._edit_receipts.move_to_end(key)
        while len(self._edit_receipts) > 6:
            self._edit_receipts.popitem(last=False)

    @staticmethod
    def _js_declarations(source: str) -> list[tuple[str, int]]:
        # Mask rather than delete non-code so declaration offsets still point
        # into the actual source. This intentionally supports only ordinary JS
        # named declaration syntax; calls and function expressions are omitted.
        masked = _JS_NONCODE.sub(lambda match: re.sub(r"[^\r\n]", " ", match[0]), source)
        return [(match[1], match.start()) for match in _JS_NAMED_FUNCTION.finditer(masked)]

    def _capture_function_todos(self, key: str, source: str, *, complete: bool = False) -> None:
        if not self.function_todo_hint or not re.search(r"\.(?:js|mjs|cjs|jsx|ts|tsx)$", key, re.I):
            return
        seen = self._seen_functions.setdefault(key, [])
        declarations = self._js_declarations(source)
        for function, _ in declarations:
            if function not in seen and len(seen) < 512:
                seen.append(function)
        if complete and key in self._function_todo_untracked:
            done = self._commented_functions.setdefault(key, set())
            docs = self._function_docs.setdefault(key, {})
            for function, position in declarations:
                doc = self._preceding_chinese_doc(source, position)
                if doc:
                    done.add(function)
                    docs[function] = doc[:4096]
                else:
                    done.discard(function)
                    docs.pop(function, None)
            self._function_todo_untracked.discard(key)

    @staticmethod
    def _preceding_chinese_doc(source: str, declaration: int) -> str:
        prefix = source[:declaration].rstrip()
        if prefix.endswith("*/"):
            start = prefix.rfind("/*")
            if start >= 0 and "*/" not in prefix[start:-2] and _CHINESE.search(prefix[start:]):
                return prefix[start:]
        comments = []
        for line in reversed(prefix.splitlines()):
            if not line.lstrip().startswith("//"):
                break
            comments.append(line)
        comment = "\n".join(reversed(comments))
        return comment if _CHINESE.search(comment) else ""

    def _update_function_todos(self, name: str, args: dict[str, Any], key: str) -> None:
        if not self.function_todo_hint or key not in self._seen_functions:
            return
        edits = args.get("edits", []) if name == "edit_many" else [args] if name == "edit_file" else []
        if not isinstance(edits, list):
            return
        done = self._commented_functions.setdefault(key, set())
        docs = self._function_docs.setdefault(key, {})
        for edit in edits:
            if not isinstance(edit, dict) or self._path(str(edit.get("path") or "")) != key:
                continue
            old, new = str(edit.get("old_string") or ""), str(edit.get("new_string") or "")
            for function, doc in list(docs.items()):
                if doc and doc in old and doc not in new:
                    done.discard(function)
                    docs.pop(function, None)
            for function, position in self._js_declarations(new):
                if function not in self._seen_functions[key]:
                    continue
                doc = self._preceding_chinese_doc(new, position)
                if doc:
                    done.add(function)
                    docs[function] = doc[:4096]
                else:
                    # A fresh declaration replacement without its recorded
                    # Chinese doc is not evidence that annotation is complete.
                    done.discard(function)
                    docs.pop(function, None)

    def _function_todos(self) -> dict[str, list[str]]:
        return {path: [function for function in self._seen_functions.get(path, [])
                       if function not in self._commented_functions.get(path, set())]
                for path in self._active if self._seen_functions.get(path) and path not in self._function_todo_untracked}

    def record_validation_failure(self, paths: Iterable[str], summary: str = "") -> None:
        """The loop calls this only after an actual failed validation process."""
        brief = str(summary or "").replace("\r\n", "\n").strip()[:1000]
        if not self.annotation:
            self._recovery_related.update(self._path(path) for path in _GENERIC_FILE_PATH.findall(str(summary or "")))
        for raw in paths:
            key = self._path(str(raw))
            if key in self._read or key in self._changed:
                self._validation_failures[key] = brief
                self._validation_failures.move_to_end(key)
                self._validation_receipts.discard(key)
                self._verified.discard(key)
                self._delivered.discard(key)
                if self.annotation:
                    self._recovery_reads[key] = 1
                else:
                    if key in self._changed:
                        self._active.setdefault(key, str(raw))
                    self._grant_mutation_recovery(key, "validation:" + brief)

    def _grant_mutation_recovery(self, key: str, failure: str) -> None:
        # A changed error message on the same source version is not a fresh
        # investigation allowance. A real edit creates the next version.
        version = (self._change_generation.get(key, 0), failure.partition(":")[0])
        if self._failure_versions.get(key) != version:
            self._failure_versions[key] = version
            self._recovery_reads[key] = 1
            self._recovery_queries = self.MUTATION_RECOVERY_QUERIES

    @property
    def phase(self) -> str:
        if not self.annotation:
            if self._validation_failures or self._edit_recovery_pending:
                return "recover"
            if self._implementation_locked or (self._read and self._without_delivery >= self.MUTATION_FOCUS_TURNS):
                return "implement"
            return "investigate"
        if (self.annotation and self._active and (self._implementation_locked
                or self._without_delivery >= self.ANNOTATION_WARN_TURNS and any(key in self._read for key in self._active))):
            return "implement"
        return "read_edit_verify" if self.annotation else "investigate"

    def record_noop(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Accept a caller's locally checked 'already satisfied' decision.

        The caller, not model prose, must establish semantic satisfaction. This
        ledger additionally requires a successful native source read and a real
        validation receipt for the unchanged version; a tool's exit text alone
        supplies neither. Call after observe(..., verified_paths=...).
        """
        accepted = []
        for raw in paths:
            key = self._path(str(raw))
            if (key in self._read and key not in self._changed and key in self._validation_receipts
                    and (self._scope_ok(key) if self.annotation else self._mutation_scope_ok(key))):
                first_noop = key not in self._noop or key not in self._delivered
                self._noop.add(key)
                self._verified.add(key)
                self._delivered.add(key)
                if first_noop:
                    self._batch_delivered = True
                    self._last_block = ""
                accepted.append(key)
        if self._active and all(key in self._verified for key in self._active):
            self._active.clear()
            self._implementation_locked = False
        return tuple(accepted)

    def _discover(self, name: str, args: dict[str, Any], result: str) -> None:
        paths: list[str] = []
        if name == "read_file":
            paths = self._paths(name, args)
        elif name == "glob":
            # Builtin glob emits workspace-relative paths, one per body line.
            paths = [line.strip() for line in result.splitlines()[1:]
                     if _source_path(line.strip())]
        elif name == "list_dir":
            # Builtin list_dir nests names by two spaces and includes sizes.
            base = str(args.get("path") or ".").replace("\\", "/")
            directories: list[str] = []
            for line in result.splitlines()[1:]:
                stripped = line.lstrip(" ")
                depth = (len(line) - len(stripped)) // 2
                directories = directories[:depth]
                if stripped.endswith("/"):
                    directories.append(stripped.rstrip("/"))
                    continue
                match = re.fullmatch(r"(.+?)\s+\(\d+B\)", stripped)
                if match:
                    paths.append(posixpath.join(base, *directories, match[1]))
        for path in paths:
            if (_source_path(path) and self._scope_ok(self._path(path))
                    and len(self._discovered) < self.MAX_TRACKED_FILES):
                self._discovered.setdefault(self._path(path), path)

    def finish_turn(self) -> None:
        if not self.enabled:
            return
        if self._batch_delivered:
            self._without_delivery = 0
        else:
            self._without_delivery += 1
        self._batch_delivered = False
        if (self.annotation and self._active and self._without_delivery >= self.ANNOTATION_WARN_TURNS
                and any(key in self._read for key in self._active)):
            self._implementation_locked = True

    @property
    def needs_action(self) -> bool:
        threshold = self.ANNOTATION_WARN_TURNS if self.annotation else self.MUTATION_WARN_TURNS
        return (self.enabled and not self.ready_to_finish
                and (self._without_delivery >= threshold or bool(self._last_block)))

    @property
    def should_stop(self) -> bool:
        threshold = self.ANNOTATION_STOP_TURNS if self.annotation else self.MUTATION_STOP_TURNS
        return self.enabled and not self.ready_to_finish and self._without_delivery >= threshold

    @property
    def blocked(self) -> bool:
        return self.should_stop

    @property
    def stalled(self) -> bool:
        return self.needs_action

    @property
    def ready_to_finish(self) -> bool:
        if not self.enabled:
            return True
        if not self.annotation:
            return (bool(self._changed or self._noop) and self._changed <= self._verified and not self._active
                    and not self._validation_failures and not self._edit_recovery_pending
                    and self.verification_plan.ready_to_finish)
        return (bool(self._changed or self._noop) and self._changed <= self._verified and not self._active
                and (not self.targets or set(self.targets) <= self._delivered)
                and set(self._discovered) <= self._delivered)

    def record_validation_command(self, command: str, result: str, checked_paths: Iterable[str] = ()) -> bool:
        """Record a real execution's full-scope receipt once per changed version.

        Checked paths may include already-verified files. The loop determines
        freshness independently; a focused or repeated full run is not new work.
        """
        if not self.enabled or self.annotation:
            return False
        previous = self.verification_plan.has_current_full_result
        accepted = self.verification_plan.record_validation(command, result, verified_paths=checked_paths)
        fresh = accepted and not previous
        if fresh:
            self._batch_delivered = True
        return fresh

    @property
    def turns_without_delivery(self) -> int:
        return self._without_delivery

    @property
    def monitored_paths(self) -> tuple[str, ...]:
        """Paths a loop may snapshot, without treating snapshots as reads."""
        return tuple(dict.fromkeys((*self.targets, *self._active, *self._changed, *self._working_reads)))

    @property
    def validation_candidates(self) -> tuple[str, ...]:
        """Include read-only baseline inputs without requiring their mutation."""
        baseline = self._active if self.annotation else self._working_reads
        return tuple(path for path in dict.fromkeys((*self.changed_unverified_paths, *self._active, *baseline,
                                                     *self._validation_failures))
                     if path not in self._verified and (path in self._read or path in self._changed)
                     and (path in self._changed or path not in self._validation_receipts))

    @property
    def validation_error_context(self) -> str:
        """Bounded process evidence; callers must wrap this as untrusted data.

        Keep the diagnostic separate from trusted task notes. A failed group
        check may record the same error against several paths; include its
        body only once so a traceback's useful middle survives context fitting.
        """
        groups: OrderedDict[str, list[str]] = OrderedDict()
        for path, diagnostic in reversed(self._validation_failures.items()):
            if not diagnostic:
                continue
            if diagnostic not in groups:
                if len(groups) >= 3:
                    continue
                groups[diagnostic] = []
            groups[diagnostic].append(path)
        excerpts = [f"验证错误来源：{self._note_labels(paths)}\n{diagnostic}"
                    for diagnostic, paths in groups.items()]
        return "\n\n".join(excerpts)[:1500]

    @property
    def context_read_targets(self) -> tuple[str, ...]:
        """Three project sources plus one already-read external reference.

        The reference is evidence only: selecting it here never adds a write
        target, a dependency requirement, or a permission to read its path.
        """
        available = [path for path in dict.fromkeys((*self._active, *self.targets, *self._working_reads))
                     if path in self._read and path not in self._delivered and self._project_read(path)]
        active = [path for path in available if path in self._active]
        implementation = [path for path in available if path not in active and _source_path(path)
                          and not re.search(r"(?:^|/)(?:tests?/|test_[^/]*$)|(?:_test|\.test|\.spec)\.", path, re.I)]
        remaining = [path for path in available if path not in active and path not in implementation]
        return tuple((*active, *implementation, *remaining)[:self.BATCH_SIZE]) + self.context_reference_targets

    @property
    def context_reference_targets(self) -> tuple[str, ...]:
        """A single most-recent successfully read, non-project evidence path."""
        latest = next((path for path in reversed(self._read_windows)
                       if path in self._read and not self._project_read(path)
                       and "/tool_results/" not in path.replace("\\", "/")), None)
        return (latest,) if latest is not None else ()

    def mark_compacted_reads(self, call_id: str, paths: Iterable[str] | str, *, complete: bool | None = None) -> None:
        """Allow bounded rehydration when the loop actually omits old evidence.

        The identity is the original read call, not a model claim. Repeated
        fitting of that same result cannot grant another allowance. This only
        affects at most two distinct <=120-line reads for a lost complete file
        longer than one window, otherwise one read; coverage and validation
        state and no-delivery budgets remain unchanged.
        """
        identity = str(call_id or "")
        if (not self.enabled or not identity or identity in self._compacted_read_ids
                or len(self._compacted_read_ids) >= self.MAX_TRACKED_FILES * 8):
            return
        self._compacted_read_ids.add(identity)
        for raw in [paths] if isinstance(paths, str) else paths:
            key = self._path(str(raw))
            if key in self._read and (self._project_read(key) or key in self.context_reference_targets):
                long_complete = (complete is not False and key in self._complete_reads
                                 and max(self._line_coverage.get(key, (0,)), default=0) > 120)
                self._context_restore_reads[key] = 2 if long_complete else 1
                self._context_restore_windows[key] = set()

    @property
    def changed_unverified_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._changed - self._verified))

    def state(self) -> dict[str, Any]:
        """Small JSON-safe execution metadata; no source or hidden tool output."""
        return {"enabled": self.enabled, "annotation": self.annotation, "broad": self.broad,
                "restricted_scope": self.restricted_scope,
                "compact_editing": self.compact_editing,
                "phase": self.phase,
                "function_todo_hint": self.function_todo_hint,
                "function_todos": self._function_todos(),
                "complete_reads": sorted(self._complete_reads),
                "recent_edits": {path: list(names) for path, names in self._edit_receipts.items() if names},
                "validation_failures": dict(self._validation_failures),
                "targets": list(self.targets), "active": list(self._active),
                "working_reads": list(self._working_reads),
                "discovered": list(self._discovered),
                "pending": self._pending_paths(),
                "changed": sorted(self._changed), "verified": sorted(self._verified),
                "noop": sorted(self._noop),
                "delivered": sorted(self._delivered), "turns_without_delivery": self._without_delivery,
                "needs_action": self.needs_action, "should_stop": self.should_stop,
                "ready_to_finish": self.ready_to_finish}

    @property
    def signature(self) -> tuple[Any, ...]:
        return (tuple(self._active), tuple(sorted(self._changed)), tuple(sorted(self._verified)),
                tuple(self._discovered), tuple(sorted(self._noop)), self._without_delivery, self._last_block)

    @staticmethod
    def _labels(paths: Iterable[str]) -> str:
        labels = [str(p) for p in paths]
        return "、".join(labels[:6]) + (f" 等 {len(labels)} 个文件" if len(labels) > 6 else "") or "尚未选定"

    def _pending_paths(self) -> list[str]:
        return [path for path in dict.fromkeys((*self.targets, *self._discovered)) if path not in self._delivered]

    def _note_labels(self, paths: Iterable[str]) -> str:
        root = self._path(".").rstrip("/") + "/"
        return self._labels(path[len(root):] if path.startswith(root) else path for path in paths)

    def note(self, max_chars: int = 1800) -> str:
        if not self.enabled:
            return ""
        # Scheduling/action state must survive the loop's 1500-character budget.
        # Function-name receipts are useful evidence, but cannot precede the
        # queue or spend the room needed to say what must happen next.
        lines = ["[任务推进] 以实际磁盘修改和对应验证结果作为交付；新文件读取和 Shell exit 0 不算交付。"]
        if not self.annotation:
            lines.append(self.verification_plan.note(max_chars=500))
        optional: list[str] = []
        if self.annotation:
            lines.append(f"当前批次：{self._note_labels(self._active)}；已修改 {len(self._changed)}，已验证 {len(self._verified)}。")
            if self.function_todo_hint:
                todos = self._function_todos()
                pieces = [f"{self._note_labels([path])}：{len(names)} 个（{'、'.join(names[:6])}"
                          + ("等" if len(names) > 6 else "") + "）" for path, names in todos.items() if names]
                if pieces:
                    lines.append("本批已见声明中尚无完成记录的函数：" + "；".join(pieces)[:380] + "。先一起补齐同文件剩余函数再验；此为编辑记录提示，最终以真实验证为准。")
            pending = self._pending_paths()
            lines.append(f"剩余目标 {len(pending)} 个：{self._note_labels(pending[:3])}。")
            next_batch = [p for p in pending if p not in self._active][:self.BATCH_SIZE]
            if next_batch:
                lines.append(f"下一批候选：{self._note_labels(next_batch)}。当前批次修改并验证后再打开候选，不重读已交付文件。")
            if self._validation_failures:
                failures = list(self._validation_failures)
                lines.append(f"验证失败待修正：{self._note_labels(failures)}。先定点修正失败点，并一起补齐同文件其余缺口，再验证；不要只补报错的首个函数。")
                # Raw process output already exists in the untrusted tool
                # result. Do not promote even a short excerpt into this trusted
                # runtime note, where source comments could become directives.
            untouched = [p for p in self._active if p not in self._changed and p not in self._noop]
            unverified = [p for p in self._active if p in self._changed and p not in self._verified]
            if untouched and not self._validation_failures:
                lines.append(f"本批尚未修改：{self._note_labels(untouched)}。先完成这些文件的注释，再验证本批。")
            if self._validation_failures:
                lines.append("下一步定点修正最近验证失败点，并一起补齐同文件全部缺失注释，再验证。")
            elif untouched:
                lines.append("下一步直接修改本批尚未修改的文件，不提前反复验证未完成批次。")
            elif unverified:
                lines.append("下一步先补齐本批全部要求和全部函数注释，再验证；已修改文件数不代表完成，当前分批编辑未完则继续 edit_many。")
            elif next_batch:
                lines.append("下一步读取下一批候选，最多 3 个文件，立即读、改、验证。")
            elif self.ready_to_finish:
                lines.append("下一步如实交付已修改/已验证结果，无需重新读取已交付文件。")
            else:
                lines.append("下一步直接实施当前目标的修改，不再扩大调查；编辑失败则修正精确匹配或拆批重试。")
            if self.phase == "implement":
                lines.append("当前阶段：实施锁定至本批验证完成，成功写入不会解锁调查。停止搜索和已知源码复读，部分文件只读真正未覆盖小窗口；错误恢复仅 1 次、最多 60 行。")
            if self.compact_editing:
                lines.append("上轮编辑参数过长：本轮每次只提交 1 处短替换，继续使用实际编辑工具完成本批，避免输出大段源码。")
            scope = (f"用户明确指定的 {len(self.targets)} 个文件" if self.targets
                     else "用户授权的全项目范围" if self.broad else "首个待注释文件")
            optional.extend([f"注释范围：{scope}。不扩展修改到依赖或其他模块。",
                             f"每批最多 {self.BATCH_SIZE} 个文件，按本批读取→修改→验证推进，再进入下一批。",
                             "原生 edit_file/edit_many 只用唯一声明作短锚点，不复制全函数体。重复工作可用内联 Node/Python 脚本，仅显式列出已读本批≤3文件，批量添加对应注释并实际验证代码/行为不变；不能遍历全项目或动态扩大目标。",
                             "目录清单只用于排队，不要整仓读取；委派子任务也要显式列出本批文件范围。"])
            if self._noop:
                optional.append(f"经本机检查及实际验证，无需改动 {len(self._noop)} 个文件；最终答复需明确区分无需修改和实际修改。")
            # Completed-file receipts stay in state()/history; only active
            # files need a compact reminder after context compression.
            receipt_parts = []
            for path in self._active:
                names = self._edit_receipts.get(path, [])
                if names:
                    short = "、".join(names[-6:])
                    if len(short) > 160:
                        short = short[:157] + "…"
                    receipt_parts.append(f"{self._note_labels([path])}: {short}（累计 {len(names)} 个）")
            if receipt_parts:
                optional.append("最近已实际写入的函数注释：" + "；".join(receipt_parts)[:420] + "。不要重新调查或重复插入。")
        else:
            changed = [path for path in self._active if path in self._changed and path not in self._verified]
            lines.append(f"当前工作集已读 {len(self._working_reads)} 个文件：{self._note_labels(self._working_reads)}；"
                         f"当前修改批次：{self._note_labels(self._active)}；累计已修改 {len(self._changed)}，已验证 {len(self._verified)}。")
            if self.restricted_scope:
                lines.append("用户限定只修改：" + self._note_labels(self.targets) + "；可按需要有界读取依赖，不扩展写范围。")
            else:
                lines.append("功能/修复范围按当前需求与代码证据推进；可新增实现、回归测试和配置文件，参考文件无需全部修改。")
            if self.phase == "recover":
                failures = dict.fromkeys((*self._validation_failures, *self._edit_recovery_pending))
                lines.append("当前阶段：错误恢复；待修正：" + self._note_labels(failures)
                             + f"。依据上次真实错误定位根因，允许 {self._recovery_queries} 次定点查询及每个相关文件 1 次≤120行补读。"
                             "错误明确涉及的新依赖可额外读取≤3个、修改≤2个，修正后统一重跑失败验收，不从全仓重新调查。")
            elif changed:
                lines.append("当前阶段：实施；本批待验证：" + self._note_labels(changed)
                             + "。补齐这项需求的实现与回归测试，再运行相关验收；测试通过后再打开后续修改批次。")
            elif self.ready_to_finish:
                lines.append("当前阶段：交付；实际修改版本已完成验证。直接交付行为变化与验证结果，不重读已完成文件。")
            elif self.phase == "implement":
                lines.append("当前阶段：实施；已有足够调查上下文，选取最多3个相关文件直接实现并验证；不再遍历全仓，必要依赖只作有限定点定位。")
            else:
                lines.append("当前阶段：定位；先读相关入口、实现与测试，定位具体行为或复现错误；读过的依赖无需改动，工作集最多8个文件，修改批次最多3个。")
            pending = [path for path in self.targets if path not in self._delivered and path not in self._active]
            if pending:
                lines.append("用户提到的后续关注文件：" + self._note_labels(pending[:3]) + "；按需求决定是否修改，不机械地改动所有参考文件。")
            if self.compact_editing:
                lines.append("上轮输出过长：每次只提交1处完整短替换，保留必要函数体与合法语法；新增实现/测试文件可用write_file分模块完成，再实际验证。")
            receipts = [f"{self._note_labels([path])}: {'、'.join(names[-5:])}" for path, names in self._edit_receipts.items()
                        if names and path in self._active]
            if receipts:
                optional.append("最近实际修改记录：" + "；".join(receipts)[:420] + "。复用这些结果，不重复调查。")
            optional.append("仅实际磁盘变更和新版本成功验证重置空转计数；新读文件、未知Shell、重复验证均不会重置。")
        if self._context_restore_reads:
            lines.append("部分已读源码已被上下文压缩：可对 " + self._note_labels(self._context_restore_reads)
                         + " 恢复显式offset、≤120行的必要窗口；丢失的完整长文件最多2个不同窗口补齐头尾，"
                         "其它片段1个；禁止重复全文，不重置任务进度。")
        if self._without_delivery:
            lines.append(f"连续 {self._without_delivery} 轮没有新增修改或有效验证。")
        if self.needs_action and not self.annotation and self.phase != "recover":
            lines.append("下一步直接实施当前目标的修改，不再扩大调查；编辑失败则修正精确匹配或拆批重试。")
        if self._last_block:
            lines.append("上次限制：" + self._last_block[:160])
        if self.should_stop:
            lines.append("持续无交付已达到上限；停止空转，如实报告未完成项和具体阻塞，不得声称任务完成。")
        result = "\n".join(lines)[:max(0, max_chars)]
        for line in optional:
            room = max_chars - len(result) - 1
            if room <= 0:
                break
            # A clipped receipt is useful; the high-priority queue/action lines
            # above are never clipped in order to make room for receipts.
            result += "\n" + line[:room]
        return result
