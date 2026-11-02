"""Replay a real Java annotation task in an isolated copy of its actual project.

Preparation never calls a model. --live requires --run-dir pointing at a prepared
copy and uses the existing configured default model without changing settings.
Only native production tools edit the target; verification never edits it.
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
sys.path.insert(0, str(REPO))
SOURCE = "backend/src/main/java/org/dyh/learnhub/ai/AgentChatProgress.java"
DEFAULT_PROJECT = Path("C:/Users/dyh/WorkBuddy/2026-09-07-15-46-17/learn-hub")
DEFAULT_TRANSCRIPT = Path.home() / ".code-agent/transcripts/s_aa7ea443353c1664.jsonl"
COMPILE = "cd backend && mvn -o -q compile -DskipTests"
JAVA21 = Path("C:/Users/dyh/dev/tools/jdk-21.0.12.1+1")
READ_NAMES = {"read_file", "grep", "glob", "list_dir", "code_map", "code_search", "code_symbols", "git_diff", "git_status"}
WRITE_NAMES = {"edit_file", "edit_many", "write_file"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_manifest(workspace):
    return {p.relative_to(workspace).as_posix(): sha(p) for p in sorted((workspace / "backend/src/main/java").rglob("*.java"))} | {"backend/pom.xml": sha(workspace / "backend/pom.xml")}


def setup_java():
    if (JAVA21 / "bin/java.exe").is_file():
        os.environ["JAVA_HOME"] = str(JAVA21)
        os.environ["PATH"] = str(JAVA21 / "bin") + os.pathsep + os.environ.get("PATH", "")


def compile_project(workspace, output):
    from code_agent.sandbox.executor import find_bash
    started = time.monotonic()
    command = [find_bash(), "-c", COMPILE]
    try:
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=180)
        output.write_text(result.stdout + result.stderr, encoding="utf-8")
        return {"exit_code": result.returncode, "seconds": round(time.monotonic() - started, 3), "log": output.name}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timeout": True, "seconds": round(time.monotonic() - started, 3)}


def original_task(transcript):
    events = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
    user = next(row for row in events if row["type"] == "user_message")
    result = next(row for row in events if row["seq"] == 6 and row["type"] == "tool_result")
    done = next(row for row in events if row["type"] == "done")
    return result["payload"]["result"], user["payload"], {
        "session_id": transcript.stem, "source_chars": len(result["payload"]["result"]),
        "turns": done["payload"]["turns"], "tool_calls": sum(row["type"] == "tool_use" for row in events),
        "seconds": round(done["ts"] - user["ts"], 3),
    }


def prepare(project, transcript, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("annotation-%Y%m%d-%H%M%S-"), dir=output_dir)).resolve()
    workspace = root / "workspace"
    (workspace / "backend/src/main/java").mkdir(parents=True)
    for path in (project / "backend/src/main/java").rglob("*.java"):
        destination = workspace / path.relative_to(project)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    shutil.copy2(project / "backend/pom.xml", workspace / "backend/pom.xml")
    original, task, baseline = original_task(transcript)
    actual_before = source_manifest(project)
    (root / "original.java").write_text(original, encoding="utf-8", newline="")
    (workspace / SOURCE).write_text(original, encoding="utf-8", newline="")
    initial = source_manifest(workspace)
    report = {"root": str(root), "project": str(project), "source": SOURCE,
              "original_task": task, "recorded_baseline": baseline,
              "initial": initial, "original_project_manifest": actual_before,
              "copied_java_files": len(initial) - 1,
              "baseline_compile": compile_project(workspace, root / "baseline-compile.log")}
    (root / "prepared.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_dir": str(root), "copied_java_files": report["copied_java_files"], "baseline_compile": report["baseline_compile"]}, ensure_ascii=False))
    return 0 if report["baseline_compile"]["exit_code"] == 0 else 1


def java_tokens(text):
    # Decode Unicode escapes before lexical classification (Java performs this
    # before recognizing comments). Literal quoted strings remain exact tokens.
    text = re.sub(r"\\u+([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)
    pattern = re.compile(r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"""[\s\S]*?"""|"(?:\\[\s\S]|[^"\\])*"|\'(?:\\[\s\S]|[^\'\\])*\'|[\w$]+|>>>=|>>>|>>=|<<=|>>|<<|->|::|==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|[^\s]')
    return [m[0] for m in pattern.finditer(text) if not re.match(r"\s|//|/\*", m[0])]


def chinese_coverage(original, current):
    declaration = re.compile(r"^    (?:(?:public|private|protected|static|final|synchronized|abstract)\s+)*(?:[\w$][\w$<>,\[\].? ]*\s+)?([\w$]+)\s*\(", re.M)
    expected = [(m[1], original[m.start():original.find("\n", m.start())]) for m in declaration.finditer(original)]
    missing = []
    for name, line in expected:
        index = current.find(line)
        prefix = current[:index].rstrip() if index >= 0 else ""
        comment = prefix[prefix.rfind("/*"):] if prefix.endswith("*/") else (re.search(r"(?:^|\n)((?:[ \t]*//[^\n]*\n?)+)$", prefix) or [None, ""])[1]
        if not re.search(r"[\u4e00-\u9fff]", comment):
            missing.append(name)
    class_prefix = current[:current.find("final class AgentChatProgress")].rstrip()
    class_comment = class_prefix[class_prefix.rfind("/*"):] if class_prefix.endswith("*/") else ""
    return {"methods": len(expected), "missing_methods": missing, "class_has_chinese": bool(re.search(r"[\u4e00-\u9fff]", class_comment))}


def safe_read_command(command, workspace):
    """A narrow, parsed shell-read allowlist, not an arbitrary-shell sandbox."""
    if any(character in command for character in (";", "|", "&", ">", "<", "`", "$", "\n", "\r")):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if parts == ["pwd"]:
        return True
    if not parts:
        return False
    name, args = parts[0], parts[1:]
    if name == "sed" and len(args) == 3 and args[0] == "-n" and re.fullmatch(r"\d+(?:,\d+)?p", args[1]):
        paths = args[2:]
    elif name in {"head", "tail"} and len(args) == 3 and args[0] == "-n" and re.fullmatch(r"\+?\d+", args[1]):
        paths = args[2:]
    elif name == "wc" and len(args) == 2 and args[0] in {"-l", "-c"}:
        paths = args[1:]
    elif name in {"cat", "ls"} and 1 <= len(args) <= 2 and (len(args) == 1 or args[0] in {"-n", "-l", "-a", "-la"}):
        paths = args[-1:]
    else:
        return False
    try:
        return all(raw and not raw.startswith("-") and (workspace / raw).resolve().is_relative_to(workspace.resolve()) for raw in paths)
    except (OSError, ValueError):
        return False


async def run_live(root, timeout, max_turns):
    from scripts.verify_project_development_delivery import ProjectRecordingProvider, resolve_probe_provider
    from scripts.verify_editing_delivery import SafeTranscript
    from code_agent.api.app import build_registry
    from code_agent.agent.project_team import ProjectTeam
    from code_agent.codeintel.service import CodeIntelService
    from code_agent.permissions.policy import Decision, PolicyResult
    from code_agent.sessions.manager import SessionManager
    from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo
    from code_agent.tools.builtin.agent_team_tools import register_agent_team_tools
    from code_agent.tools.registry import ToolRegistry

    prepared = json.loads((root / "prepared.json").read_text(encoding="utf-8"))
    workspace = root / "workspace"
    if prepared["baseline_compile"]["exit_code"] != 0 or source_manifest(workspace) != prepared["initial"]:
        raise ValueError("A successful unchanged preparation is required; prepare a fresh run")
    router, delegate, model = resolve_probe_provider(workspace, live=True)
    delegate.max_retries = 0
    provider = ProjectRecordingProvider(delegate, max_turns=max_turns)
    router.provider_override = provider
    previous_home = os.environ.get("CODE_AGENT_HOME")
    os.environ["CODE_AGENT_HOME"] = str(root / "agent-home")
    transcript = SafeTranscript(root / "transcripts", [getattr(delegate, "api_key", ""), getattr(delegate, "base_url", "")])
    original = (root / "original.java").read_text(encoding="utf-8")
    source_receipts = []
    requests = []

    def request(turn, messages, tools):
        # Store no messages/reasoning, only whether source data actually reaches
        # the provider intact. Raw tool events are separately redacted.
        contents = [str(m.get("content") or "") for m in messages]
        observed = []
        for call_id, text in source_receipts:
            observed.append({"call_id": call_id, "chars": len(text), "verbatim": any(text in value for value in contents),
                             "middle_visible": any(text[len(text)//3:len(text)*2//3] in value for value in contents)})
        requests.append({"turn": turn, "source_receipts": observed,
                         "delegate_agent_available": any(t["name"] == "delegate_agent" for t in tools)})
        print(json.dumps({"phase": "model", "turn": turn, "source_verbatim": any(r["verbatim"] for r in observed)}), flush=True)
    provider.on_request = request

    class ScopePolicy:
        def evaluate(self, arguments, ctx):
            entries = arguments.get("edits", [arguments])
            allowed = bool(entries) and all((Path(ctx.workspace) / e.get("path", "")).resolve() == (workspace / SOURCE).resolve() for e in entries)
            return PolicyResult(Decision.allow if allowed else Decision.deny, "仅允许修改原任务目标文件")

    class ShellPolicy:
        def evaluate(self, arguments, ctx):
            command = str(arguments.get("command", "")).strip()
            allowed = (command == COMPILE or safe_read_command(command, workspace)) and not arguments.get("run_in_background")
            # Only literal native read commands and the exact offline build;
            # arbitrary scripts cannot bypass the single-target edit boundary.
            return PolicyResult(Decision.allow if allowed else Decision.deny,
                                f"本验证环境 Shell 仅允许前台命令：{COMPILE}，或单条 sed/head/tail/cat/wc/ls/pwd 只读命令；复杂查询请用原生只读工具")

    database = Database(root / "agent-home/verification.db")
    user = UserRepo(database).create("annotation-validation", "isolated-local-validation")
    projects = ProjectRepo(database)
    project = projects.create(user["id"], "Real annotation reproduction", str(workspace), str(workspace).lower())
    projects.set_flags(project["id"], allow_direct_write=True)
    project = projects.get(project["id"])
    intel = CodeIntelService(projects)
    await intel.scan(project, full=True)
    production = build_registry(code_intel=intel)
    registry = ToolRegistry()
    for name in READ_NAMES | WRITE_NAMES | {"bash"}:
        if tool := production.get(name):
            registry.register(tool)
    sessions = SessionRepo(database)
    manager = SessionManager(transcript, sessions, ApprovalRepo(database), router, registry,
                             workspaces_root=root / "workspaces", project_repo=projects, code_intel=intel,
                             extra_policies={**{name: ScopePolicy() for name in WRITE_NAMES}, "bash": ShellPolicy()},
                             system_suffix=f"当前是隔离验证副本。只修改 {SOURCE} 的注释，不改逻辑；其他文件只读。验证命令为 `{COMPILE}`，前台执行，timeout=180；工具返回编译真实退出码，不需要额外 echo。")
    team = ProjectTeam(manager)
    manager.agent_team = team
    register_agent_team_tools(registry, team)
    meta = manager.create_project_session(user["id"], project["id"], permission_mode="workspace-write")
    runtime = manager.get_runtime(meta["id"], user["id"])
    started = time.monotonic()
    steps = []
    pending = {}
    before_version = sha(workspace / SOURCE)
    model_verified_version = None
    first_write = None
    broadcast = runtime.broadcast

    def observe(event):
        nonlocal before_version, first_write, model_verified_version
        p = event.payload
        if event.type.value == "tool_use":
            pending[p["call"]["id"]] = p["call"]
        if event.type.value == "tool_result":
            call = pending.get(p["call_id"], {})
            version = sha(workspace / SOURCE)
            changed = version != before_version
            if changed and first_write is None:
                first_write = {"turn": provider.calls, "previous_tools": len(steps), "seconds": round(time.monotonic()-started, 3)}
            text = str(p.get("result") or "")
            args = call.get("arguments", {})
            if call.get("name") == "read_file" and args.get("path") == SOURCE and not args.get("offset") and not args.get("limit") and not text.startswith("[tool error]"):
                source_receipts.append((p["call_id"], text))
            if call.get("name") == "bash" and args.get("command", "").strip() == COMPILE and text.startswith("exit_code=0"):
                model_verified_version = version
            steps.append({"turn": provider.calls, "tool": call.get("name"), "changed": changed,
                          "error": text[:300] if text.startswith(("[tool error]", "[error]", "[permission denied]")) else None,
                          "output_chars": len(text), "version": version})
            before_version = version
        broadcast(event)
    runtime.broadcast = observe
    result = error = None
    try:
        result = await asyncio.wait_for(runtime.submit(prepared["original_task"]["text"], code_context=prepared["original_task"]["code_context"]), timeout)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": transcript.redact(str(exc))}
    finally:
        agent_seconds = round(time.monotonic() - started, 3)
        await team.aclose()
        for active in manager._runtimes.values():
            active.shutdown()
        intel.close()
        database.close()
        await router.aclose()
        if previous_home is None:
            os.environ.pop("CODE_AGENT_HOME", None)
        else:
            os.environ["CODE_AGENT_HOME"] = previous_home
    current = (workspace / SOURCE).read_text(encoding="utf-8")
    final = compile_project(workspace, root / "final-compile.log")
    manifest = source_manifest(workspace)
    changed_paths = sorted(path for path, version in manifest.items() if prepared["initial"].get(path) != version)
    coverage = chinese_coverage(original, current)
    tokens_equal = java_tokens(original) == java_tokens(current)
    original_intact = source_manifest(Path(prepared["project"])) == prepared["original_project_manifest"]
    report = {"mode": "live", **model, "run_dir": str(root), "session_id": meta["id"],
              "recorded_baseline": prepared["recorded_baseline"], "turns": provider.calls,
              "agent_seconds": agent_seconds, "first_write": first_write, "tool_calls": len(steps),
              "tool_counts": dict(Counter(s["tool"] for s in steps)), "source_projection": requests,
              "tool_failures": [s for s in steps if s["error"]],
              "stop_reason": result.stop_reason if result else None, "error": error,
              "usage": result.usage.model_dump() if result else None,
              "tokens_unchanged": tokens_equal, "coverage": coverage, "changed_paths": changed_paths,
              "original_project_unchanged": original_intact, "final_compile": final,
              "model_compiled_final_version": model_verified_version == sha(workspace / SOURCE)}
    report["passed"] = bool(result and result.stop_reason == "end_turn" and not error and final["exit_code"] == 0
                            and changed_paths == [SOURCE] and original_intact and tokens_equal
                            and not coverage["missing_methods"] and coverage["class_has_chinese"])
    (root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "tool-steps.json").write_text(json.dumps(steps, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"source_projection", "usage"}}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--transcript", type=Path, default=DEFAULT_TRANSCRIPT)
    parser.add_argument("--output-dir", type=Path, default=REPO / ".tmp/harness-source-fix")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args()
    setup_java()
    if args.live:
        if not args.run_dir:
            parser.error("--live requires --run-dir")
        return asyncio.run(run_live(args.run_dir.resolve(), args.timeout, args.max_turns))
    return prepare(args.project.resolve(), args.transcript, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
