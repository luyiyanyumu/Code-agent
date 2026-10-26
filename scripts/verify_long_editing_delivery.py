"""Large annotation delivery probe using SessionRuntime and production tools.

No API request is made without --live. Both modes edit a private 12-module
fixture through real tools and run an immutable, independent Node verifier.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from code_agent.models.events import EventType, ModelTurn, ToolCall
from code_agent.permissions.policy import BashPolicy, Decision, PolicyResult
from code_agent.providers.base import Provider
from code_agent.providers.offline import OfflineProvider
from scripts.verify_editing_delivery import SafeTranscript


MODULES = 12
FUNCTIONS_PER_MODULE = 10
BATCH_SIZE = 3
TARGETS = tuple(f"src/module_{number:02}.mjs" for number in range(MODULES))
READ_TOOLS = {"read_file", "grep", "glob", "list_dir", "code_map", "code_search", "code_graph", "code_features"}
WRITE_TOOLS = {"edit_file", "edit_many", "write_file"}
MARKER = "LONG_VERIFY "


def function_name(module, number):
    return f"aggregate{module:02}_{number:02}"


def module_source(module):
    previous = function_name(module - 1, 0)
    dependency = f"import {{ {previous} as precedingAggregate }} from './module_{module - 1:02}.mjs';\n\n" if module else ""
    bodies = []
    for number in range(FUNCTIONS_PER_MODULE):
        bias = f"precedingAggregate([{module}, {number}], {{ limit: 2 }}).average" if module else "0"
        bodies.append(f"""export function {function_name(module, number)}(values, options = {{}}) {{
  if (!Array.isArray(values)) {{
    throw new TypeError('values must be an array');
  }}
  const limit = Number.isInteger(options.limit)
    ? Math.max(0, options.limit)
    : values.length;
  const finite = values.filter((value) => Number.isFinite(value));
  const selected = finite.slice(0, limit);
  if (selected.length === 0) {{
    return {{ count: 0, total: 0, average: 0, adjusted: 0 }};
  }}
  const weight = {module + number + 1};
  const bias = {bias};
  const scaled = selected.map((value, index) => value * weight + index);
  const total = scaled.reduce((sum, value) => sum + value, 0);
  const precision = Number.isInteger(options.precision)
    ? Math.min(3, Math.max(0, options.precision))
    : 2;
  const average = Number((total / scaled.length).toFixed(precision));
  const adjusted = average + bias;
  return {{ count: selected.length, total, average, adjusted }};
}}
""")
    return dependency + "\n".join(bodies)


def comment_for(module, number):
    dependency = "叠加前一模块计算的偏移" if module else "首模块偏移为零"
    return (
        "/**\n"
        f" * 对有限数值按权重 {module + number + 1} 及索引求加权统计，{dependency}。\n"
        " * @param {number[]} values 输入数组；非数组抛出 TypeError，非有限数值被过滤。\n"
        " * @param {object} options 配置；options.limit 取非负整数，缺省为输入长度。\n"
        " * options.precision 限制为 0 至 3 位小数，缺省为 2。\n"
        " * @returns {object} 返回 count 数量、total 总和、average 均值和 adjusted 偏移结果。\n"
        " * 空数组或 limit 为零时各返回字段为零；不修改输入。\n"
        " */\n"
    )


VERIFIER = r"""import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
const root = path.dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(readFileSync(path.join(root, 'manifest.json'), 'utf8'));
const requested = process.argv.slice(2);
const files = requested.length && requested[0] !== '--all' ? requested : manifest.targets;
let assertions = 0, functions = 0, comments = 0;
const errors = [];
const verified = [];
function tokens(raw) {
  const source = raw.replace(/\r\n/g, '\n');
  const pattern = /\s+|\/\/[^\n]*|\/\*[\s\S]*?\*\/|'(?:\\[\s\S]|[^'\\])*'|"(?:\\[\s\S]|[^"\\])*"|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[A-Za-z_$][\w$]*|===|!==|=>|==|!=|<=|>=|&&|\|\||\?\?|\?\.|\+\+|--|\*\*|[^\s]/gy;
  const result = [];
  let match;
  while ((match = pattern.exec(source)) !== null) {
    const token = match[0];
    if (!/^\s|^\/\/|^\/\*/.test(token)) result.push(token);
  }
  return result;
}
for (const relative of files) {
  try {
    assert.ok(manifest.targets.includes(relative), 'File is outside annotation scope');
    const currentPath = path.join(root, 'workspace', relative);
    const baselinePath = path.join(root, 'baseline', relative);
    const raw = readFileSync(currentPath, 'utf8').replace(/\r\n/g, '\n');
    const baseline = readFileSync(baselinePath, 'utf8');
    assert.deepEqual(tokens(raw), tokens(baseline), 'Runtime tokens changed');
    const syntax = spawnSync(process.execPath, ['--check', currentPath], { encoding: 'utf8', timeout: 10000 });
    assert.equal(syntax.status, 0, 'Node syntax check failed');
    const current = await import(pathToFileURL(currentPath).href);
    const expected = await import(pathToFileURL(baselinePath).href);
    const names = manifest.functions[relative];
    for (const name of names) {
      functions++;
      const declaration = raw.indexOf('export function ' + name + '(');
      const prefix = raw.slice(0, declaration).trimEnd();
      const comment = prefix.endsWith('*/') ? prefix.slice(prefix.lastIndexOf('/*'))
        : (prefix.match(/(?:^|\n)((?:[ \t]*\/\/[^\n]*\n?)+)$/)?.[1] ?? '');
      assert.match(comment, /[\u4e00-\u9fff]/u, 'Missing Chinese comment: ' + name);
      for (const term of ['values', 'options', 'limit', 'precision', 'count', 'total', 'average', 'adjusted']) {
        assert.ok(comment.includes(term), `Missing parameter/return field ${term}: ${name}`);
      }
      assert.match(comment, /返回|@returns?/u, 'Missing return description: ' + name);
      assert.match(comment, /空/u, 'Missing empty-input boundary: ' + name);
      assert.match(comment, /TypeError|抛出|异常/u, 'Missing invalid-array boundary: ' + name);
      assert.match(comment, /有限|过滤/u, 'Missing finite-number handling: ' + name);
      assert.match(comment, /权重|加权|系数|倍/u, 'Missing weighting description: ' + name);
      const prose = comment.replace(/\r?\n\s*\*?/g, ' ');
      assert.match(prose, /(?:precision|精度|小数)[^。；]{0,180}(?:0|０|零)[^。；]{0,80}(?:3|３|三)/u,
        'Missing precision boundary: ' + name);
      comments++;
      const cases = [
        [[], {}], [[1, 2, 3], {}], [[-3, 0, 5], {}], [[1.25, 2.5, 9.75], { precision: 3 }],
        [[NaN, 1, Infinity, 2, -Infinity], {}], [[1, '3', null, 4], {}],
        [[1, 2, 3], { limit: 0 }], [[1, 2, 3], { limit: -9 }], [[1, 2, 3], { limit: 1 }],
        [[1, 2, 3], { limit: 100 }], [[1.13, 2.28], { precision: -2 }],
        [[1.13, 2.28], { precision: 8 }], [[1, 2, 3], { limit: '2', precision: '1' }],
        [[0, 0, 0], {}],
      ];
      for (const [values, options] of cases) {
        const before = [...values];
        assert.deepEqual(current[name](values, options), expected[name](values, options), 'Behavior changed: ' + name);
        assert.deepEqual(values, before, 'Input was mutated: ' + name);
        assertions += 2;
      }
      for (const invalid of [null, 'wrong', 42, {}]) {
        assert.throws(() => current[name](invalid), TypeError, 'Missing array validation: ' + name);
        assertions++;
      }
    }
    verified.push(relative);
  } catch (error) {
    errors.push({ path: relative, message: String(error.message).slice(0, 260) });
  }
}
const output = { ok: errors.length === 0, paths: files, verified, files: files.length, functions, comments,
  assertions, token_unchanged: errors.every((error) => !error.message.includes('tokens')), errors };
console.log('LONG_VERIFY ' + JSON.stringify(output));
process.exitCode = output.ok ? 0 : 1;
"""


def make_fixture(root):
    workspace = root / "workspace"
    (workspace / "src").mkdir(parents=True)
    (root / "baseline" / "src").mkdir(parents=True)
    functions = {}
    for module, relative in enumerate(TARGETS):
        text = module_source(module)
        (workspace / relative).write_text(text, encoding="utf-8")
        (root / "baseline" / relative).write_text(text, encoding="utf-8")
        functions[relative] = [function_name(module, number) for number in range(FUNCTIONS_PER_MODULE)]
    decoy = workspace / "unrelated-project"
    decoy.mkdir()
    (decoy / "autostart.mjs").write_text("export const unrelated = 'do not edit or inspect';\n", encoding="utf-8")
    (workspace / "README.md").write_text(
        "# Long annotation fixture\nOnly the twelve src/module_XX.mjs files are task targets.\n"
        "The unrelated-project directory is outside task scope.\n"
        "Run node ../verify_long.mjs followed by up to three target paths after each batch.\n",
        encoding="utf-8",
    )
    (root / "other-project").mkdir()
    (root / "other-project" / "module_00.mjs").write_text("export const other = 42;\n", encoding="utf-8")
    manifest = {"targets": list(TARGETS), "functions": functions, "batch_size": BATCH_SIZE,
                "minimum_behavior_assertions": 300, "runtime_tokens_must_not_change": True}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (root / "verify_long.mjs").write_text(VERIFIER, encoding="utf-8")
    return workspace, manifest


class FixtureShellPolicy:
    """Restrict this probe to scoped reads and immutable local Node validation.

    Real BashPolicy still handles each admitted command's normal approvals.
    Simple && / ; composition is permitted; no eval, expansion or redirection.
    """

    def evaluate(self, arguments, ctx):
        command = str(arguments.get("command") or "").strip()
        if re.match(r"^node\s+(?:-e|--eval)\b", command):
            if arguments.get("run_in_background"):
                return PolicyResult(Decision.deny, "fixture 脚本不得后台执行")
            return self._evaluate_edit_script(command, ctx)
        if arguments.get("run_in_background") or re.search(r"[`$<>|\r\n]", command):
            return PolicyResult(Decision.deny, "验收工作区不允许动态命令、后台、重定向或管道")
        segments = re.split(r"\s*(?:&&|;)\s*", command)
        decisions = []
        for segment in segments:
            try:
                parts = shlex.split(segment)
            except ValueError:
                return PolicyResult(Decision.deny, "无效 fixture 命令")
            if not parts:
                return PolicyResult(Decision.deny, "空 fixture 命令")
            executable = parts[0].lower()
            safe = False
            if executable == "node" and len(parts) >= 2:
                if parts[1] == "../verify_long.mjs":
                    safe = not parts[2:] or parts[2:] == ["--all"] or all(path in TARGETS for path in parts[2:])
                elif parts[1] == "--check":
                    safe = len(parts) == 3 and parts[2] in TARGETS
            elif executable == "echo":
                safe = not any(character in segment for character in "&()")
            elif executable == "pwd":
                safe = len(parts) == 1
            elif executable in {"ls", "dir"}:
                safe = all(part.startswith("-") or part in {"src", "src/", "."} for part in parts[1:])
            elif executable in {"cat", "type", "head", "tail", "wc"}:
                safe = bool(parts[1:]) and all(part in TARGETS or part == "README.md" or part.startswith("-")
                                             or part.isdecimal() for part in parts[1:])
            if not safe:
                return PolicyResult(Decision.deny, "命令超出本次 fixture 范围；使用原生读改工具或本地校验入口")
            decisions.append(BashPolicy().evaluate({"command": segment}, ctx))
        if any(item.decision == Decision.deny for item in decisions):
            return next(item for item in decisions if item.decision == Decision.deny)
        if any(item.decision == Decision.need_approval for item in decisions):
            return PolicyResult(Decision.need_approval, "本次 fixture 本机验证命令，范围已校验")
        return PolicyResult(Decision.allow, "本次 fixture 内只读命令")

    @staticmethod
    def _evaluate_edit_script(command, ctx):
        """Admit simple explicit-file annotation scripts, not arbitrary Node code.

        This is a fixture-only narrowing of BashPolicy. Production permissions,
        read-before-write and active-batch checks still run before dispatch.
        """
        prefix = re.match(r"^node\s+(?:-e|--eval)\s+", command)
        tail = command[prefix.end():].strip() if prefix else ""
        if len(tail) < 2 or tail[0] not in "\"'" or "`" in tail or "$" in tail:
            return PolicyResult(Decision.deny, "node 脚本必须由一个完整引号包裹，不使用 Shell 展开")
        quote, index = tail[0], 1
        while index < len(tail):
            if tail[index] == "\\" and quote == '"':
                index += 2
                continue
            if tail[index] == quote:
                break
            index += 1
        if index != len(tail) - 1:
            return PolicyResult(Decision.deny, "脚本编辑必须单独调用，不拼接其它 Shell 命令")
        try:
            parts = shlex.split(command)
        except ValueError:
            return PolicyResult(Decision.deny, "无效脚本引号")
        if len(parts) != 3:
            return PolicyResult(Decision.deny, "node 脚本只接受一个代码参数")
        code = parts[2]
        denied = re.search(
            r"\.\.[/\\]|[A-Za-z]:[/\\]|baseline|verify_long|manifest\.json|unrelated-project|other-project|"
            r"\b(?:process|child_process|spawn|exec|execFile|fork|fetch|http|https|net|tls|dgram|WebSocket|"
            r"import|eval|global|globalThis|module|constructor|prototype|__proto__|Reflect|Proxy|"
            r"readdir|readdirSync|opendir|glob|symlink|unlink|rmSync|mkdir|chdir)\b", code, re.I,
        )
        if denied or re.search(r"\bFunction\b", code):
            return PolicyResult(Decision.deny, "脚本包含越界路径、目录遍历、动态执行、进程或网络操作")
        module_call = re.compile(r"\brequire\s*\(\s*(['\"])(?:node:)?fs\1\s*\)")
        if len(module_call.findall(code)) != 1 or re.search(r"\brequire\b", module_call.sub("FS_MODULE", code)):
            return PolicyResult(Decision.deny, "脚本只能加载一次 fs 模块")
        strings = re.findall(r"(['\"])((?:\\.|(?!\1).)*?)\1", code)
        paths = list(dict.fromkeys(value for _quote, value in strings if value in TARGETS))
        if not 1 <= len(paths) <= BATCH_SIZE:
            return PolicyResult(Decision.deny, "脚本必须显式列出本批 1 至 3 个目标文件")
        for _quote, value in strings:
            if re.search(r"(?:^|[/\\])(?:src|workspace)[/\\]|\.(?:mjs|cjs|js|json|py|txt)(?:$|['\"])", value) and value not in TARGETS:
                return PolicyResult(Decision.deny, "脚本引用了本批目标以外的文件路径")
        binding = re.search(r"\b(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*require\s*\(", code)
        if not binding:
            return PolicyResult(Decision.deny, "请使用 const fs = require(\"node:fs\") 绑定文件模块")
        fs_name = re.escape(binding[1])
        accesses = re.findall(r"\b" + fs_name + r"\s*\.\s*([A-Za-z_]\w*)\s*(\(?)", code)
        if not accesses or any(name not in {"readFileSync", "writeFileSync"} or opening != "("
                               for name, opening in accesses) or re.search(r"\b" + fs_name + r"\s*\[", code):
            return PolicyResult(Decision.deny, "fs 只允许直接调用 readFileSync/writeFileSync")
        arrays = {}
        for match in re.finditer(r"\bconst\s+([A-Za-z_]\w*)\s*=\s*(\[[^\]]*\])", code):
            try:
                values = json.loads(match[2])
            except ValueError:
                values = re.findall(r"['\"]([^'\"]+)['\"]", match[2])
                remainder = re.sub(r"['\"][^'\"]*['\"]", "", match[2][1:-1])
                if remainder.strip(" ,\t\r\n"):
                    continue
            if values and all(value in paths for value in values):
                arrays[match[1]] = values
        if any(re.search(r"\b" + re.escape(name) + r"\s*(?:\.\s*(?:push|pop|shift|unshift|splice|fill|copyWithin|length)\b|\[[^\]]*\]\s*=)", code)
               for name in arrays):
            return PolicyResult(Decision.deny, "显式目标数组不得动态扩展或改写")
        iterators = {match[1] for match in re.finditer(r"for\s*\(\s*const\s+([A-Za-z_]\w*)\s+of\s+([A-Za-z_]\w*)\s*\)", code)
                     if match[2] in arrays}
        for match in re.finditer(r"\b" + fs_name + r"\s*\.\s*(?:readFileSync|writeFileSync)\s*\(\s*([^,\n]+)", code):
            argument = match[1].strip()
            literal = argument[1:-1] if len(argument) >= 2 and argument[0] in "\"'" and argument[-1] == argument[0] else None
            if literal in paths or argument in iterators:
                continue
            if any(re.fullmatch(re.escape(name) + r"\[\d+\]", argument) for name in arrays):
                continue
            return PolicyResult(Decision.deny, "文件读写参数必须为显式目标路径或本批 const 文件循环变量")
        return BashPolicy().evaluate({"command": command}, ctx)


class RecordingProvider(Provider):
    def __init__(self, delegate, max_turns=80):
        self.delegate = delegate
        self.calls = 0
        self.max_turns = max_turns
        self.request_summaries = []
        self.on_request = None
        self.supports_streaming = delegate.supports_streaming
        self.name = delegate.name

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def begin(self, messages, tools):
        if self.calls >= self.max_turns:
            raise RuntimeError("Verification model-turn budget exhausted")
        self.calls += 1
        self.request_summaries.append({"turn": self.calls, "message_chars": sum(len(str(m.get("content", ""))) for m in messages),
                                       "tool_count": len(tools), "tool_names": [tool["name"] for tool in tools]})
        if self.on_request is not None:
            self.on_request(self.calls, messages, tools)

    async def chat(self, messages, tools):
        self.begin(messages, tools)
        return await self.delegate.chat(messages, tools)

    async def stream(self, messages, tools):
        self.begin(messages, tools)
        async for chunk in self.delegate.stream(messages, tools):
            yield chunk


def fixture_script_command(paths):
    """A compact example with explicit paths; actual writes remain tool-driven."""
    header = (
        "/** values 为输入数组，非数组抛出 TypeError，过滤非有限数。options.limit 为非负截取数量；"
        "options.precision 精度范围 0 至 3。按本函数权重统计并叠加前模块偏移，"
        "返回 count 数量、total 总和、average 均值、adjusted 偏移结果；空数组各字段为零。 */\n"
    )
    code = (
        'const fs = require("node:fs"); const files = ' + json.dumps(list(paths)) + '; '
        'const header = ' + json.dumps(header, ensure_ascii=False) + '; '
        'for (const file of files) { const source = fs.readFileSync(file, "utf8"); '
        'const next = source.replace(/^export function /gm, (declaration) => header + declaration); '
        'fs.writeFileSync(file, next, "utf8"); }'
    )
    return "node -e " + shlex.quote(code)


def offline_provider(*, scripted_edits=False):
    script = []
    for first in range(0, MODULES, BATCH_SIZE):
        script.append(ModelTurn(tool_calls=[ToolCall(name="read_file", arguments={"path": TARGETS[module]})
                                           for module in range(first, first + BATCH_SIZE)], stop_reason="tool_use"))
        # Match the annotation schema's small per-request payload budget.
        edits = []
        for module in range(first, first + BATCH_SIZE):
            for number in range(FUNCTIONS_PER_MODULE):
                declaration = f"export function {function_name(module, number)}("
                edits.append({"path": TARGETS[module], "old_string": declaration,
                              "new_string": comment_for(module, number) + declaration})
        if scripted_edits:
            script.append(OfflineProvider.tool_call_turn("bash", {
                "command": fixture_script_command(TARGETS[first:first + BATCH_SIZE]),
                "_approval": {"purpose": "为已读本批目标批量添加中文注释", "effects": ["只写入本批三个显式目标"]},
            }))
        else:
            for offset in range(0, len(edits), 5):
                script.append(OfflineProvider.tool_call_turn("edit_many", {"edits": edits[offset:offset + 5]}))
        script.append(OfflineProvider.tool_call_turn("bash", {
            "command": "node ../verify_long.mjs " + " ".join(TARGETS[first:first + BATCH_SIZE]),
            "_approval": {"purpose": f"验证第 {first // BATCH_SIZE + 1} 批真实注释交付",
                          "effects": ["校验本批源码 token、中文注释覆盖及输入输出行为"]},
        }))
    script.append(OfflineProvider.tool_call_turn("bash", {
        "command": "node ../verify_long.mjs --all",
        "_approval": {"purpose": "运行最终全部目标的交付验证", "effects": ["只读校验 12 模块与 120 函数"]},
    }))
    script.append(OfflineProvider.text_turn("已按四批为全部目标添加中文注释，逐批和全量验证通过。"))
    return OfflineProvider(script=script)


def parse_verifications(text):
    values = []
    for line in str(text).splitlines():
        if line.startswith(MARKER):
            try:
                values.append(json.loads(line[len(MARKER):]))
            except ValueError:
                pass
    return values


def external_verify(workspace, paths=()):
    try:
        process = subprocess.run(["node", "../verify_long.mjs", *paths], cwd=workspace, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=40)
        summaries = parse_verifications(process.stdout)
        return {"exit_code": process.returncode, "result": summaries[-1] if summaries else None}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timeout": True, "result": None}


class DeliveryObserver:
    def __init__(self, workspace, provider):
        self.workspace = workspace
        self.provider = provider
        self.before = self.snapshot()
        self.initial = dict(self.before)
        self.steps = []
        self.arguments = {}

    def snapshot(self):
        return {str(path.relative_to(self.workspace)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.workspace.rglob("*") if path.is_file()}

    def on_event(self, event):
        if event.type == EventType.tool_use:
            call = event.payload["call"]
            self.arguments[call["id"]] = call
        elif event.type == EventType.tool_result:
            after = self.snapshot()
            changed = sorted(path for path in set(after) | set(self.before) if after.get(path) != self.before.get(path))
            call = self.arguments.get(event.payload["call_id"], {})
            result = event.payload.get("result", "")
            self.steps.append({"seq": event.seq, "turn": self.provider.calls, "tool": event.payload.get("name"),
                               "call_id": event.payload["call_id"], "changed": changed,
                               "error": result.startswith(("[tool error]", "[permission denied]")),
                               "block_kind": ("task_scope" if "任务推进" in result and result.startswith("[tool error]")
                                              else "permission" if result.startswith("[permission denied]")
                                              else "tool_error" if result.startswith("[tool error]") else None),
                               "verifications": parse_verifications(result), "arguments": call.get("arguments", {}),
                               "target_versions": {path: after.get(path) for path in TARGETS}})
            self.before = after

    def summary(self):
        writes = [step for step in self.steps if step["changed"]]
        longest = current = 0
        by_turn = {}
        for step in self.steps:
            by_turn.setdefault(step["turn"], []).append(step)
        for steps in by_turn.values():
            if any(step["changed"] for step in steps):
                current = 0
            elif any(step["tool"] in READ_TOOLS or step["tool"] in {"bash", "bash_persistent"} for step in steps):
                current += 1
                longest = max(longest, current)
        out_of_scope = []
        blocked_attempts = []
        for step in self.steps:
            arguments = step["arguments"]
            paths = [arguments.get("path", "")]
            paths += [edit.get("path", "") for edit in arguments.get("edits", []) if isinstance(edit, dict)]
            for path in paths:
                if path and ("unrelated-project" in path or "other-project" in path or path.startswith("..")):
                    access = {"turn": step["turn"], "tool": step["tool"], "path": path}
                    if step["error"]:
                        blocked_attempts.append({**access, "block_kind": step.get("block_kind") or "tool_error"})
                    else:
                        out_of_scope.append(access)
        verified_batches = [value for step in self.steps for value in step["verifications"]
                            if value.get("ok") and 1 <= value.get("files", 0) <= BATCH_SIZE]
        active = set()
        verified_versions = {}
        batch_order_violations = []
        for step in self.steps:
            raw = step["arguments"].get("path", "")
            try:
                path = Path(raw)
                relative = str(path.resolve().relative_to(self.workspace.resolve())).replace("\\", "/") if path.is_absolute() else str(path).replace("\\", "/").removeprefix("./")
            except (ValueError, OSError):
                relative = raw
            # Count task source reads and actual writes. Discovery metadata and
            # final all-module checks do not manufacture an extra active batch.
            if step["tool"] == "read_file" and not step["error"] and relative in TARGETS:
                version = step["target_versions"].get(relative)
                if version is None or verified_versions.get(relative) != version:
                    active.add(relative)
            active.update(path for path in step["changed"] if path in TARGETS)
            if len(active) > BATCH_SIZE:
                batch_order_violations.append({"turn": step["turn"], "active_files": sorted(active)})
            for value in step["verifications"]:
                if value.get("ok"):
                    for path in value.get("verified", []):
                        verified_versions[path] = step["target_versions"].get(path)
                        active.discard(path)
        model_verified_current = sorted(path for path, version in verified_versions.items()
                                        if version == self.before.get(path) and path in TARGETS)
        target_changes = sorted({path for step in writes for path in step["changed"] if path in TARGETS})
        outside_changes = sorted({path for step in writes for path in step["changed"] if path not in TARGETS})
        return {"tool_counts": dict(Counter(step["tool"] for step in self.steps)),
                "native_read_calls": sum(step["tool"] == "read_file" for step in self.steps),
                "shell_calls": sum(step["tool"] in {"bash", "bash_persistent"} for step in self.steps),
                "first_write_turn": min((step["turn"] for step in writes), default=None),
                "longest_readonly_turn_streak": longest, "changed_targets": target_changes,
                "out_of_scope_accesses": out_of_scope, "out_of_scope_changes": outside_changes,
                "blocked_attempts": blocked_attempts,
                "successful_batch_verifications": verified_batches,
                "model_verified_paths": model_verified_current,
                "batch_order_violations": batch_order_violations, "unverified_active_paths": sorted(active),
                "tool_error_count": sum(step["error"] for step in self.steps)}


def task_prompt():
    return (
        "请给当前工程以下 12 个源码文件中的全部导出函数补充准确、详细的中文 JSDoc 注释，实际写入并完成验证：\n"
        + "\n".join(TARGETS)
        + "\n共 120 个函数。只增加注释，保持运行时代码 token 和行为不变，不重构。"
        "逐函数说明 values、options.limit、options.precision 参数、返回对象 count/total/average/adjusted 各字段、"
        "权重与跨模块偏移、空数组/过滤非有限数值/非数组 TypeError 边界和 0 至 3 位精度。\n"
        "请每批处理至多 3 个文件：读取本批、实际修改、校验本批通过，再进入下一批；无需先通读 12 个文件。"
        "复用读取内容，每次编辑遵守当前工具 schema 的小批次上限，使用短且唯一命中的替换片段，"
        "保持完整剩余目标清单，不要只完成首批。\n"
        "编辑可选原生小批次替换，或 node -e/--eval 脚本批改已读的本批最多三个显式目标；"
        "简单重复注释可用紧凑准确模板，不必重复大段通用叙述。脚本单独调用、由一个 Shell 引号包裹，"
        "使用 const fs = require(\"node:fs\")，const files = [本批显式路径] 和 for (const file of files)，"
        "仅 fs.readFileSync/writeFileSync 读写这些 file，不作目录遍历、网络、进程启动或动态执行，"
        "不要使用反引号或 Shell 变量展开。脚本写入与独立验证必须分成两次工具调用。\n"
        "现成独立验证入口在工作区外，不可改：node ../verify_long.mjs 后带本批目标路径（最多三个），"
        "例如 node ../verify_long.mjs src/module_00.mjs src/module_01.mjs src/module_02.mjs。"
        "入口会实际做 Node 语法、注释覆盖、源码 token 不变及行为断言；全量结束运行 node ../verify_long.mjs --all。"
        "允许本地校验命令以 && 或 ; 组合，也可单独 node --check 目标文件。"
        "每次需审批调用提供 _approval 的 purpose 和 effects。\n"
        "仅处理上述列出的 12 个源码文件，不读取或修改 unrelated-project、other-project 或其它诱饵目录。"
        "完成后简短报告实际修改规模及逐批、全量验证结果。"
    )


async def run_probe(root, *, live=False, timeout=900, max_turns=80, scripted_edits=False):
    workspace, manifest = make_fixture(root)
    # Resolve the configured provider before isolating platform storage.
    from code_agent.agent.router import ModelRouter

    router = ModelRouter() if live else ModelRouter(provider_name="offline")
    delegate = router.resolve_default() if live else offline_provider(scripted_edits=scripted_edits)
    if live and router.provider_name == "offline":
        raise ValueError("Live mode requires a configured non-offline provider")
    if live:
        delegate.max_output_tokens = 8192
        if hasattr(delegate, "max_retries"):
            delegate.max_retries = 1
    provider = RecordingProvider(delegate, max_turns=max_turns)
    router.provider_override = provider
    secret_values = [getattr(delegate, "api_key", ""), getattr(delegate, "base_url", "")]
    previous_home = os.environ.get("CODE_AGENT_HOME")
    os.environ["CODE_AGENT_HOME"] = str(root / "agent-home")
    transcript = SafeTranscript(root / "transcripts", secret_values)
    (root / "requests").mkdir()

    def record_request(turn, messages, tools):
        payload = transcript.redact({"turn": turn, "messages": messages, "tools": tools})
        (root / "requests" / f"turn-{turn:03}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    provider.on_request = record_request
    database = runtime = intel = None
    result = None
    started = time.monotonic()
    summary = {"mode": "live" if live else "offline", "artifact_dir": str(root),
               "model": getattr(delegate, "model", "offline-script"), "max_turns": max_turns,
               "timeout_seconds": timeout, "target_files": MODULES, "target_functions": MODULES * FUNCTIONS_PER_MODULE,
               "initial_source_lines": sum(len(module_source(module).splitlines()) for module in range(MODULES))}
    observer = DeliveryObserver(workspace, provider)
    try:
        from code_agent.api.app import build_registry
        from code_agent.codeintel.service import CodeIntelService
        from code_agent.sessions.manager import SessionRuntime
        from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo

        database = Database(root / "agent-home" / "long-validation.db")
        user = UserRepo(database).create("long-validation-user", "local-fixture-only")
        projects = ProjectRepo(database)
        project = projects.create(user["id"], "Annotation delivery benchmark", str(workspace), str(workspace).lower())
        intel = CodeIntelService(projects)
        await intel.scan(project, full=True)
        registry = build_registry(code_intel=intel)
        sessions = SessionRepo(database)
        meta = sessions.create(user["id"], title="Long annotation validation", project_id=project["id"],
                               tree_id="main", kind="chat", permission_mode="workspace-write")
        approvals = []
        runtime = SessionRuntime(
            meta["id"], user["id"], transcript, sessions, ApprovalRepo(database), router, registry, workspace,
            extra_policies={"bash": FixtureShellPolicy(), "bash_persistent": FixtureShellPolicy()},
            permission_mode="workspace-write", memory_root=root / "memory", session_memory_root=root / "session-memory",
        )
        runtime.code_context_provider = lambda: intel.session_context(project)
        runtime._code_context_provider_fn = lambda selected: intel.message_context(project, selected)
        runtime.code_project, runtime.code_intel = project, intel

        async def approve_fixture(request):
            arguments = json.loads(request.command)
            from code_agent.tools.base import ToolContext

            policy = FixtureShellPolicy().evaluate(arguments, ToolContext(meta["id"], workspace))
            approved = request.tool_name in {"bash", "bash_persistent"} and policy.decision != Decision.deny
            approvals.append({"tool": request.tool_name, "approved": approved})
            return approved

        runtime._server_approver = approve_fixture
        original_broadcast = runtime.broadcast

        def observe_and_broadcast(event):
            observer.on_event(event)
            original_broadcast(event)

        runtime.broadcast = observe_and_broadcast
        original_build = runtime._build_loop

        def bounded_build(**overrides):
            remaining = max(1, max_turns - provider.calls)
            configured = int(overrides.get("max_turns") or remaining)
            overrides["max_turns"] = min(remaining, configured)
            return original_build(**overrides)

        runtime._build_loop = bounded_build
        summary["production_registry_tools"] = registry.names()
        result = await asyncio.wait_for(runtime.submit(task_prompt(), code_context={"path": TARGETS[0]}), timeout=timeout)
        summary.update(stop_reason=result.stop_reason, turns=provider.calls, usage=result.usage.model_dump(),
                       answer_chars=len(result.text or ""), approvals=approvals)
    except asyncio.TimeoutError:
        summary.update(stop_reason="verification_timeout", turns=provider.calls)
    except Exception as exc:
        summary.update(stop_reason="verification_error", exception_type=type(exc).__name__, turns=provider.calls)
    finally:
        if runtime is not None:
            runtime.shutdown()
        if intel is not None:
            intel.close()
        if database is not None:
            database.close()
        await router.aclose()
        if previous_home is None:
            os.environ.pop("CODE_AGENT_HOME", None)
        else:
            os.environ["CODE_AGENT_HOME"] = previous_home
    verification = external_verify(workspace)
    summary.update(observer.summary(), external_verification=verification,
                   elapsed_seconds=round(time.monotonic() - started, 2),
                   session_id=getattr(runtime, "session_id", None),
                   request_summaries=provider.request_summaries)
    final = verification.get("result") or {}
    immutable = (root / "verify_long.mjs").read_text(encoding="utf-8") == VERIFIER
    baseline_unchanged = all((root / "baseline" / relative).read_text(encoding="utf-8") == module_source(index)
                             for index, relative in enumerate(TARGETS))
    summary["independent_verifier_unchanged"] = immutable and baseline_unchanged
    summary["passed"] = bool(
        result and result.stop_reason == "end_turn" and verification["exit_code"] == 0 and final.get("ok")
        and final.get("functions") == MODULES * FUNCTIONS_PER_MODULE and final.get("comments") == MODULES * FUNCTIONS_PER_MODULE
        and final.get("assertions", 0) >= 300 and set(summary["model_verified_paths"]) == set(TARGETS)
        and set(summary["changed_targets"]) == set(TARGETS) and not summary["out_of_scope_changes"]
        and not summary["out_of_scope_accesses"] and immutable and baseline_unchanged
        and not summary["batch_order_violations"] and not summary["unverified_active_paths"]
    )
    (root / "model-requests.json").write_text(json.dumps(provider.request_summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "tool-steps.json").write_text(json.dumps(transcript.redact(observer.steps), ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=REPO / "web" / "shots" / "long-editing-delivery")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--max-turns", type=int, default=80)
    args = parser.parse_args()
    if not 0 < args.timeout <= 900 or not 1 <= args.max_turns <= 80:
        parser.error("timeout must be <=900 seconds and max-turns <=80")
    if shutil.which("node") is None:
        parser.error("Node.js is required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=args.output_dir)).resolve()
    try:
        summary = asyncio.run(run_probe(root, live=args.live, timeout=args.timeout, max_turns=args.max_turns))
    except Exception as exc:
        summary = {"passed": False, "artifact_dir": str(root), "exception_type": type(exc).__name__}
        (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    # Detailed requests and safe tool results remain local; avoid raw replies.
    printable = {key: value for key, value in summary.items() if key not in {"request_summaries", "production_registry_tools"}}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
