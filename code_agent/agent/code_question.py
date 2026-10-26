"""Conservative routing and evidence conversion for single-source explanations.

These helpers do not read files, execute tools, or grant permissions.  Matching a
question only identifies a narrow explanation request; the caller must still
obtain source through the normal permission-checked tools.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import ntpath
import posixpath
import re
from typing import Any

from code_agent.agent.text_tool_output import is_text_tool_call
from code_agent.context.safety import data_message


_EXT = r"(?:java|py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|cpp|cc|cxx|c|h|hpp|cs|vue|kt|kts|swift|rb|php|scala|sh|sql|html|css|scss|svelte|ex|exs|erl|clj|dart|lua|r|m)"
_SOURCE_END = re.compile(r"\." + _EXT + r"$", re.I)
_QUOTED = re.compile(r"(?P<quote>[`\"'])(?P<path>[^\n\r]*?)(?P=quote)")
# An unquoted path is an explicit ASCII token.  Chinese instructions commonly
# touch both ends ("解释src/main.py的逻辑"); they must remain available for intent
# checks instead of becoming part of the filename.  Unicode/spaced paths remain
# supported when quoted.  The leading boundary also prevents matching a URL's
# path after its scheme separator or a suffix inside a longer filename.
_BARE = re.compile(
    r"(?<![A-Za-z0-9_./\\@+~$%=:~-])"
    r"(?:[A-Za-z]:[\\/])?[A-Za-z0-9_./\\@+~$%=-]+?\."
    + _EXT + r"(?=$|[\s`\"'<>|，。；：！？、（）(){}\[\]:]|[\u3400-\u9fff])",
    re.I,
)
_EXPLAIN = re.compile(r"解释|说明|讲解|分析|\b(?:explain|describe)\b", re.I)
_MUTATION_WORDS = (
    r"修改|修复|重构|优化|实现|新增|添加|增加|加入|新建|创建|编写|补充|补齐|补全|调整|"
    r"删除|改成|改为|改写|更新|完善|替换|迁移|"
    r"改(?:动)?(?:代码|文件|逻辑|实现)|修(?:好|一下|吧)|改(?:好|一下|吧)|解决|"
    r"写(?:个|一个|一份)[^，。；！？\n]{0,12}(?:脚本|程序|文件|测试|函数|模块)|"
    r"\b(?:edit|modify|fix|refactor|optimi[sz]e|implement|add|delete|remove|rewrite|"
    r"update|replace|migrate)\b"
)
_MUTATIONS = re.compile(_MUTATION_WORDS, re.I)
_OPERATIONS = re.compile(
    _MUTATION_WORDS + r"|"
    r"提交|发布|部署|安装|启动|重启|执行|"
    r"运行(?!逻辑|流程|机制|原理|时(?:行为|逻辑|问题))|"
    r"\b(?:commit|publish|deploy|install|start|restart|execute|run|test)\b",
    re.I,
)
# Annotation requests often use verbs absent from ordinary edit requests:
# "补上解释性注释" and "中文注释解释每个函数" both require a write.
_ANNOTATION_WRITES = re.compile(
    r"(?:补(?:充|上|全|齐)?|加(?:上|入)?|写(?:入|上)?|撰写|生成|翻译)"
    r"[^，。；！？\n]{0,24}(?:注释|\b(?:comments?|docstrings?)\b)|"
    r"(?:用|使用|以)(?:中文|汉语|详细|逐行|解释性|说明性)*注释(?:来)?(?:解释|说明|讲解|标注)|"
    r"(?:中文|汉语|详细|逐行|解释性|说明性)注释(?:来)?(?:解释|说明|讲解|标注)(?!了|的|过|什么)|"
    r"\b(?:write|insert|generate|translate)\b[^,;.!?\n]{0,40}\b(?:comments?|docstrings?)\b|"
    r"\b(?:use\s+(?:Chinese\s+)?comments?\s+to\s+explain|comment\s+(?:each|every|all)\b)",
    re.I,
)
# These verbs also occur in descriptions ("this class supports ...", "build
# status"). Require a request-shaped prefix and exclude informational topics
# instead of treating every mention as a requested implementation.
_DEVELOPMENT_WRITES = re.compile(
    r"支持|开发|修掉|修\s*bugs?(?![A-Za-z])|做(?:一个|一下|出来|好|个)?|"
    r"\b(?:build|create|develop|repair|resolve|support|handle|make)\b", re.I,
)
_DEVELOPMENT_REQUEST = re.compile(
    r"^\s*(?:(?:请|麻烦|帮我|帮我们|给我|替我|直接|现在|继续|再|也|先|"
    r"可以|能不能|可不可以|能否|能|要|需要|希望|想要)\s*)*$|"
    r"(?:帮我|帮我们|希望|需要|想要|让|使|把|将|为)[^，。；！？\n]{0,48}$|"
    r"(?:^|\s)(?:please|(?:can|could|would|will)\s+you(?:\s+please)?|"
    r"(?:i|we)\s+(?:want|need)(?:\s+you)?\s+to)\s*$|[:：]\s*$",
    re.I,
)
_DEVELOPMENT_TOPIC = re.compile(
    r"^\s*(?:的)?(?:什么|哪些|哪种|流程|状态|团队|计划|方案|建议|方法|方式|思路|含义|区别)|"
    r"^\s+(?:status|process|team|ticket|plan|approach|means)\b|"
    r"(?:是什么|有什么用|什么含义|什么作用|是干什么)", re.I,
)
_INTENT_CLAUSES = re.compile(
    r"[，。；！？,;.!?\n]|但是|然后|而是|并且|同时|但|并|\b(?:but|then|and)\b", re.I,
)
_NEGATED_ACTION = re.compile(
    r"(?:不要|不用|不必|无需|不需要|禁止|别|不能|不允许|暂不|先不|不)[^，。；！？\n]{0,16}$|"
    r"\b(?:do\s+not|don['’]t|never|without|no|not)\b[^,;.!?\n]{0,30}$", re.I,
)
_ACTION_QUESTION = re.compile(
    r"(?:如何|怎么|怎样|为何|为什么|解释|说明|讲解|分析)[^，。；！？\n]{0,24}$|"
    r"\b(?:how|why|explain|describe)\b[^,;.!?\n]{0,50}$", re.I,
)
_ACTION_TOPIC = re.compile(r"^\s*(?:的)?(?:方案|建议|方法|方式|思路|含义|是什么)|^\s+(?:plan|approach|means)\b", re.I)
_OTHER_WORK = re.compile(
    r"继续|接着|上轮|上一轮|上次任务|之前的任务|其他文件|其它文件|所有文件|"
    r"全项目|整个项目|全仓库|整个仓库|审查|审计|"
    r"(?:自动|稍后|定时|每天).{0,8}(?:通知|提醒|检查)|"
    r"\b(?:continue|resume|review|audit|notify|remind|schedule|entire\s+(?:project|repo))\b",
    re.I,
)
_CALLERS = re.compile(
    r"调用关系|调用链|调用者|调用方|被谁调用|谁调用|哪些.{0,12}调用|"
    r"\b(?:callers?|call\s+(?:graph|chain|relationships?)|references)\b", re.I,
)
_DETAILED = re.compile(
    r"详细|逐行|全面|深入|每一行|所有方法|全部函数|"
    r"\b(?:in\s+detail|line\s+by\s+line|thorough(?:ly)?)\b", re.I,
)


@dataclass(frozen=True)
class CodeQuestion:
    path: str
    include_callers: bool = False
    detailed: bool = False


def _source_path(candidate: str) -> bool:
    return bool(
        candidate
        and _SOURCE_END.search(candidate)
        and "://" not in candidate
        and not any(c in candidate for c in "\x00\n\r<>|")
    )


def _path_key(path: str) -> str:
    if re.match(r"^[A-Za-z]:[\\/]", path) or "\\" in path:
        return ntpath.normcase(ntpath.normpath(path))
    return posixpath.normpath(path)


def _paths_and_intent(prompt: str) -> tuple[dict[str, str], str]:
    masked = list(prompt)
    paths: dict[str, str] = {}
    for match in _QUOTED.finditer(prompt):
        path = match["path"].strip()
        if _source_path(path):
            paths.setdefault(_path_key(path), path)
            masked[match.start():match.end()] = " " * (match.end() - match.start())
    remainder = "".join(masked)
    for match in _BARE.finditer(remainder):
        path = match.group()
        if _source_path(path):
            paths.setdefault(_path_key(path), path)
            masked[match.start():match.end()] = " " * (match.end() - match.start())
    return paths, "".join(masked)


def requires_mutation(prompt: str) -> bool:
    """Recognize an affirmative request to change code/files, without any I/O.

    This routing hint is deliberately separate from execution evidence and
    permissions. Mentioning an edit, asking how it works, or forbidding an edit
    does not require a write. Actual changes still use permission-checked tools.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    _, intent = _paths_and_intent(prompt)
    for clause in _INTENT_CLAUSES.split(intent):
        for pattern in (_MUTATIONS, _ANNOTATION_WRITES, _DEVELOPMENT_WRITES):
            for action in pattern.finditer(clause):
                prefix = re.sub(r"能不能|可不可以", "可以", clause[:action.start()])
                if _NEGATED_ACTION.search(prefix) or _ACTION_QUESTION.search(prefix):
                    continue
                if _ACTION_TOPIC.search(clause[action.end():]):
                    continue
                if pattern is _DEVELOPMENT_WRITES and (
                    not _DEVELOPMENT_REQUEST.search(prefix)
                    or _DEVELOPMENT_TOPIC.search(clause[action.end():])
                ):
                    continue
                return True
    return False


def match_code_question(prompt: str) -> CodeQuestion | None:
    """Match an explicit, independent explanation of exactly one source file.

    Filenames are masked before intent detection: ``fix.py`` does not request a
    fix, and ``explain.py`` does not by itself request an explanation.  Ambiguous
    or compound work falls back to the normal agent loop.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    paths, intent = _paths_and_intent(prompt)
    if (len(paths) != 1 or not _EXPLAIN.search(intent)
            or _OPERATIONS.search(intent) or _OTHER_WORK.search(intent) or requires_mutation(prompt)):
        return None
    return CodeQuestion(next(iter(paths.values())), bool(_CALLERS.search(intent)),
                        bool(_DETAILED.search(intent)))


def _text_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    return [deepcopy(part) for part in content
            if not isinstance(part, dict) or part.get("type") not in ("image_url", "input_image", "image")]


def _images(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [deepcopy(part) for part in content
            if isinstance(part, dict) and part.get("type") in ("image_url", "input_image", "image")]


def _is_protocol_text(content: Any) -> bool:
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content
                          if isinstance(part, dict) and part.get("type") in ("text", "input_text"))
    return is_text_tool_call(content)


def make_answer_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn prior tool batches into attributed untrusted data for a tool-free answer.

    Native assistant/tool protocol can encourage compatible providers to emit
    more tool syntax even when tools are disabled.  Keep its evidence as ordinary
    data instead, preserving complete call/result groups and missing results.
    Original trusted system/user instructions and ordinary conversation remain
    in their original roles; no input object is modified.
    """
    converted: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []

    def flush() -> None:
        if not batch:
            return
        records = []
        pictures = []
        for message in batch:
            role = message.get("role")
            pictures.extend(_images(message.get("content")))
            if role == "tool":
                records.append({"kind": "tool_result", "call_id": message.get("tool_call_id"),
                                "result": _text_content(message.get("content"))})
            else:
                record: dict[str, Any] = {"kind": "assistant_tool_request",
                                          "text": _text_content(message.get("content"))}
                if message.get("tool_calls"):
                    record["calls"] = deepcopy(message["tool_calls"])
                if message.get("function_call"):
                    record["legacy_call"] = deepcopy(message["function_call"])
                records.append(record)
        evidence = data_message(
            "以下是先前工具请求及已返回结果的记录；请求本身不证明已执行，缺失的结果不代表成功。\n"
            + json.dumps(records, ensure_ascii=False, default=str),
            label="已有工具证据（调用 ID 对应请求与结果）",
        )
        if pictures:
            evidence["content"] = [{"type": "text", "text": evidence["content"]}, *pictures]
        converted.append(evidence)
        batch.clear()

    for original in messages:
        message = {key: deepcopy(value) for key, value in original.items()
                   if not key.startswith("_native")}
        role = message.get("role")
        is_request = role == "assistant" and bool(
            message.get("tool_calls") or message.get("function_call") or _is_protocol_text(message.get("content"))
        )
        if role == "tool":
            batch.append(message)
        elif is_request:
            flush()
            batch.append(message)
        else:
            flush()
            # These fields are protocol, never independent user/system commands.
            for key in ("tool_calls", "function_call", "tool_call_id"):
                message.pop(key, None)
            converted.append(message)
    flush()
    return converted
