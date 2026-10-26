"""Verify editing delivery in a disposable fixture; API calls require --live.

Default: deterministic offline provider, real permission/edit/shell dispatch.
--live: use the configured default model without writing any model settings.
Only a safe JSON summary is printed. Fixture and redacted transcript are saved.
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
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from code_agent.agent.loop import AgentLoop
from code_agent.models.events import EventType
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import Decision, PolicyResult
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import BashTool, ReadFileTool
from code_agent.tools.builtin.code_tools import EditFileTool, EditManyTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


SOURCE = "autostart.mjs"
COMMANDS = ("node --check autostart.mjs", "node ../verify.mjs")
FUNCTIONS = ("normalizeConfig", "readDescriptor", "ensureReady")
SOURCE_TEXT = """export function normalizeConfig(config = {}) {
  return {
    enabled: config.enabled !== false,
    timeout: Number.isFinite(config.timeout) ? Math.max(100, config.timeout) : 1500,
  };
}

export function readDescriptor(raw) {
  const descriptor = JSON.parse(raw);
  if (typeof descriptor.name !== 'string' || !descriptor.name.trim()) {
    throw new TypeError('name is required');
  }
  return { name: descriptor.name.trim(), argv: descriptor.argv ?? [] };
}

export function ensureReady(config) {
  const normalized = normalizeConfig(config);
  return normalized.enabled && normalized.timeout >= 100;
}
"""
COMMENTS = {
    "normalizeConfig": "规范化启用状态和超时，缺省值为启用及 1500 毫秒，最小超时为 100 毫秒。",
    "readDescriptor": "解析 JSON 描述符并校验名称，返回去除首尾空白的名称及默认参数数组。",
    "ensureReady": "复用配置规范化结果，检查服务是否启用且超时符合最低要求。",
}
VERIFIER = r"""import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as source from './workspace/autostart.mjs';
const text = readFileSync(new URL('./workspace/autostart.mjs', import.meta.url), 'utf8').replace(/\r\n/g, '\n');
for (const name of ['normalizeConfig', 'readDescriptor', 'ensureReady']) {
  const pattern = new RegExp('((?:[ \\t]*//[^\\n]*\\n|[ \\t]*/\\*[\\s\\S]*?\\*/[ \\t]*\\n)+)[ \\t]*export\\s+function\\s+' + name + '\\b');
  const comment = text.match(pattern)?.[1] ?? '';
  assert.match(comment, /[\u4e00-\u9fff]/u, `Missing Chinese function comment: ${name}`);
}
assert.deepEqual(source.normalizeConfig(), { enabled: true, timeout: 1500 });
assert.deepEqual(source.normalizeConfig({ enabled: false, timeout: 20 }), { enabled: false, timeout: 100 });
assert.deepEqual(source.normalizeConfig({ timeout: 300 }), { enabled: true, timeout: 300 });
assert.deepEqual(source.readDescriptor('{"name":" worker "}'), { name: 'worker', argv: [] });
assert.deepEqual(source.readDescriptor('{"name":"worker","argv":["--run"]}'), { name: 'worker', argv: ['--run'] });
assert.throws(() => source.readDescriptor('{"name":" "}'), TypeError);
assert.throws(() => source.readDescriptor('invalid'), SyntaxError);
assert.equal(source.ensureReady(), true);
assert.equal(source.ensureReady({ enabled: false }), false);
console.log('VERIFIED 3 Chinese function comments and 9 behavior assertions');
"""


class FixtureCommandPolicy:
    """Never grant shell access beyond the two immutable fixture checks."""

    def evaluate(self, arguments, ctx):
        if arguments.get("command", "").strip() in COMMANDS and not arguments.get("run_in_background"):
            return PolicyResult(Decision.need_approval, "仅允许本次临时 fixture 的 Node 检查")
        return PolicyResult(Decision.deny, "验收脚本仅接受列出的两条本机 Node 校验命令")


class SafeTranscript(TranscriptStore):
    def __init__(self, root, secrets=()):
        super().__init__(root)
        self.secrets = tuple(str(secret) for secret in secrets if secret)

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
            value = re.sub(r"https?://[^\s'\"<>]+", "[redacted-url]", value)
            return re.sub(r"(?i)(bearer\s+|\bsk-)[A-Za-z0-9_.-]+", "[redacted-token]", value)
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def append(self, session_id, type, payload):
        return super().append(session_id, type, self.redact(payload))


def make_fixture(root):
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / SOURCE).write_text(SOURCE_TEXT, encoding="utf-8")
    (root / "verify.mjs").write_text(VERIFIER, encoding="utf-8")
    (root / "autostart.before.mjs").write_text(SOURCE_TEXT, encoding="utf-8")
    return workspace


def offline_provider():
    call = OfflineProvider.tool_call_turn
    edits = [{"path": SOURCE, "old_string": f"export function {name}(",
              "new_string": f"// {COMMENTS[name]}\nexport function {name}("} for name in FUNCTIONS]
    explanation = {"purpose": "验证临时 fixture 的语法和行为",
                   "effects": ["仅运行指定本机 Node 校验脚本"]}
    return OfflineProvider(script=[
        call("read_file", {"path": SOURCE}),
        call("edit_many", {"edits": [{"path": SOURCE, "old_string": "missing function marker",
                                      "new_string": "not applied"}]}),
        OfflineProvider.text_turn("编辑失败，未能补充注释。"),
        call("edit_many", {"edits": edits}),
        *[call("bash", {"command": command, "_approval": explanation}) for command in COMMANDS],
        OfflineProvider.text_turn("中文注释已写入，语法与行为校验通过。"),
    ])


def node_check(workspace, command):
    try:
        proc = subprocess.run(command, cwd=workspace, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
        return {"exit_code": proc.returncode, "verified_marker": "VERIFIED 3 Chinese" in proc.stdout}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timeout": True}
    except OSError as exc:
        return {"exit_code": None, "exception_type": type(exc).__name__}


async def run(args, root):
    workspace = make_fixture(root)
    router = None
    provider = None
    secret_values = []
    # Resolve before isolating CODE_AGENT_HOME: live uses existing configuration,
    # while memory/results/transcripts must go only into this verification run.
    if args.live:
        from code_agent.agent.router import ModelRouter

        router = ModelRouter()
        provider = router.resolve_default()
        if router.provider_name == "offline":
            raise ValueError("Live verification requires a configured non-offline provider")
        secret_values = [getattr(provider, "api_key", ""), getattr(provider, "base_url", "")]
        provider.max_output_tokens = args.max_output_tokens
        if hasattr(provider, "max_retries"):
            provider.max_retries = 0
    else:
        provider = offline_provider()

    previous_home = os.environ.get("CODE_AGENT_HOME")
    os.environ["CODE_AGENT_HOME"] = str(root / "agent-home")
    transcript = SafeTranscript(root / "transcripts", secret_values)
    approvals = []

    async def approve_fixture_only(request):
        arguments = json.loads(request.command)
        approved = request.tool_name == "bash" and arguments.get("command", "").strip() in COMMANDS
        approvals.append({"tool": request.tool_name, "approved": approved})
        return approved

    registry = ToolRegistry()
    for tool in (ReadFileTool(), EditFileTool(), EditManyTool(), BashTool()):
        registry.register(tool)
    permissions = PermissionManager(workspace, approver=approve_fixture_only, mode="workspace-write")
    permissions.register_policy("bash", FixtureCommandPolicy())
    loop = AgentLoop(provider, registry, permissions, transcript, workspace=workspace,
                     max_turns=12, fs_observation=FsObservation(), memory_root=root / "memory")
    summary: dict[str, Any] = {"mode": "live" if args.live else "offline", "artifact_dir": str(root),
                               "provider": getattr(provider, "name", "unknown"),
                               "model": getattr(provider, "model", "offline-script"), "max_turns": 12}
    started = time.monotonic()
    result = None
    try:
        result = await asyncio.wait_for(loop.run(
            "给打开的这个文件补充中文注释并运行语法和行为验证。",
            context_blocks=[
                f"当前编辑器文件：{SOURCE}，工作区为独立临时 fixture。\n"
                "给每个导出函数前补充准确的中文注释，保持全部代码行为不变，实际写入文件。\n"
                f"修改后运行这两条现成检查命令：{COMMANDS[0]}；{COMMANDS[1]}。\n"
                "bash 的 command 必须分别完整匹配以上两条命令，不得拼接 echo、ls、管道、分号或 &&。"
                "每条 bash 调用必须提供 _approval.purpose 和非空 _approval.effects，说明本次 fixture 验证。\n"
                "验证脚本位于工作区外且不可修改。使用原生工具调用，复用已读内容，简短报告实际结果。",
            ],
        ), timeout=args.timeout)
        summary.update(stop_reason=result.stop_reason, turns=result.turns, usage=result.usage.model_dump(),
                       answer_chars=len(result.text or ""))
    except asyncio.TimeoutError:
        summary.update(stop_reason="verification_timeout", turns=None)
    except Exception as exc:
        # Exception strings can contain service URLs or response bodies.
        summary.update(stop_reason="verification_error", exception_type=type(exc).__name__)
    finally:
        if router is not None:
            await router.aclose()
        if previous_home is None:
            os.environ.pop("CODE_AGENT_HOME", None)
        else:
            os.environ["CODE_AGENT_HOME"] = previous_home

    source = (workspace / SOURCE).read_text(encoding="utf-8")
    events = transcript.replay(loop.session_id)
    uses = [event for event in events if event.type == EventType.tool_use]
    results = [event for event in events if event.type == EventType.tool_result]
    syntax = node_check(workspace, ["node", "--check", SOURCE])
    behavior = node_check(workspace, ["node", "../verify.mjs"])
    shell_results = [event.payload["result"] for event in results if event.payload.get("name") == "bash"]
    errors = [{"tool": event.payload.get("name"),
               "message": transcript.redact(event.payload.get("result", ""))[:240]}
              for event in results if event.payload.get("result", "").startswith(("[tool error]", "[permission denied]"))]
    function_count = len(re.findall(r"\bexport\s+function\s+", source))
    chinese_comments = len(re.findall(r"(?m)^\s*(?://[^\n]*[\u4e00-\u9fff][^\n]*|/\*[\s\S]*?[\u4e00-\u9fff][\s\S]*?\*/)", source))
    summary.update(
        elapsed_seconds=round(time.monotonic() - started, 2), session_id=loop.session_id,
        changed=source != SOURCE_TEXT, function_count=function_count, chinese_comment_count=chinese_comments,
        sha256=hashlib.sha256((workspace / SOURCE).read_bytes()).hexdigest(),
        tool_counts=dict(Counter(event.payload["call"]["name"] for event in uses)),
        tool_errors=errors, approvals=approvals, node_syntax=syntax, node_behavior=behavior,
        model_ran_node_validation=any("VERIFIED 3 Chinese" in text and "exit_code=0" in text
                                      for text in shell_results),
        transcript=str(transcript._path(loop.session_id)),
    )
    summary["passed"] = bool(
        result and result.stop_reason == "end_turn" and summary["changed"] and function_count == 3
        and syntax["exit_code"] == behavior["exit_code"] == 0 and behavior["verified_marker"]
        and summary["model_ran_node_validation"]
        and (root / "verify.mjs").read_text(encoding="utf-8") == VERIFIER
    )
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly call the configured default model")
    parser.add_argument("--output-dir", type=Path, default=REPO / "web" / "shots" / "editing-delivery")
    parser.add_argument("--max-output-tokens", type=int, choices=(4096, 8192), default=4096)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not 0 < args.timeout <= 150:
        parser.error("--timeout must be in (0, 150] so verification stays below three minutes")
    if shutil.which("node") is None:
        parser.error("Node.js is required for real syntax and behavior checks")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=args.output_dir)).resolve()
    try:
        summary = asyncio.run(run(args, root))
    except Exception as exc:
        summary = {"passed": False, "artifact_dir": str(root), "exception_type": type(exc).__name__}
        (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
