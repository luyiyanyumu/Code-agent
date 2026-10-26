"""M10 端到端冒烟：插件包机制 + 统一工具入口。

为什么需要它：单元测试用 TestClient 在进程内跑，验证不了这几件事——
  * 服务**运行中**往 plugins/ 落盘一个包，reload 后真的生效（含热推到已有会话）
  * 插件声明的 prompt 片段与内嵌技能目录，真的出现在**发给模型的请求**里
  * 插件声明的 allowTools，真的让一个默认要审批的工具直接执行
  * 停用插件后，技能 / prompt / 放行名单真的全部撤回（回到审批）
  * 代码式工具的双重开关在真实进程里成立（默认跳过 → 设 CODE_AGENT_PLUGIN_CODE=1 才加载）
  * 插件声明的 MCP server 配错了，不会拖垮启动，目录里如实显示"配了没连上"

做法：起一个真的 HTTP 服务扮演 OpenAI 兼容后端（记录每次请求的完整 messages），
再起真 Code-agent 进程指向它，用真 WS 收事件、真 REST 审批。分两段跑：

  Phase A（不设 CODE_AGENT_PLUGIN_CODE）——声明式能力全链路 + 代码工具默认关闭
  Phase B（CODE_AGENT_PLUGIN_CODE=1 重启同 home）——代码工具真加载、真执行 + MCP 配错不崩

用法：
    python scripts/smoke_m10.py [--keep]

退出码：0 全通；1 有失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[1]

ANSWER_PLUGIN = "结论：插件链路已贯通。依据：技能目录与 prompt 片段都已进入本次请求。"
PLUGIN_TOOL_RESULT = "hi from code plugin"

#: 刻意不在 bash 白名单里（python/ls/cat 等才放行），用来验证"默认要审批"
BASH_COMMAND = "mkdir -p from_plugin"


# ======================================================================
# 假 OpenAI 兼容后端
# ======================================================================
def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _chunk(delta: dict, finish: str | None = None) -> str:
    return _sse(
        {
            "id": "chatcmpl-m10",
            "object": "chat.completion.chunk",
            "model": "fake-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


def _text_stream(text: str):
    parts = [text[i : i + 9] for i in range(0, len(text), 9)]
    for part in parts:
        yield _chunk({"content": part})
    yield _chunk({}, finish="stop")
    yield "data: [DONE]\n\n"


def _call_stream(name: str, arguments: dict, call_id: str):
    yield _chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }
            ]
        }
    )
    yield _chunk({}, finish="tool_calls")
    yield "data: [DONE]\n\n"


class FakeLLM:
    """按用户话术选脚本，并记录每次请求的完整 messages（要验 system prompt）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def build(self):
        app = FastAPI()
        state = self

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            state.requests.append(body)

            messages = body.get("messages") or []
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            user_text = next(
                (m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"),
                "",
            )
            last = messages[-1] if messages else {}
            # 工具已执行完 -> 收口成文本
            pending_tool = last.get("role") == "tool"

            name, args = "", {}
            if not has_tool_result and not pending_tool:
                if "跑个命令" in user_text:
                    name, args = "bash", {"command": BASH_COMMAND}
                elif "用插件工具" in user_text:
                    name, args = "plugin_hello", {}

            if not body.get("stream"):
                if name:
                    return JSONResponse(
                        {
                            "id": "chatcmpl-m10",
                            "object": "chat.completion",
                            "model": "fake-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": None,
                                        "tool_calls": [
                                            {
                                                "id": f"call_{name}",
                                                "type": "function",
                                                "function": {
                                                    "name": name,
                                                    "arguments": json.dumps(args, ensure_ascii=False),
                                                },
                                            }
                                        ],
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ],
                        }
                    )
                return JSONResponse(
                    {
                        "id": "chatcmpl-m10",
                        "object": "chat.completion",
                        "model": "fake-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": ANSWER_PLUGIN},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )

            stream = _call_stream(name, args, f"call_{name}") if name else _text_stream(ANSWER_PLUGIN)
            return StreamingResponse(stream, media_type="text/event-stream")

        return app

    def last_system(self) -> str:
        for body in reversed(self.requests):
            for msg in body.get("messages") or []:
                if msg.get("role") == "system":
                    return msg.get("content") or ""
        return ""


def start_fake_llm(port: int, llm: FakeLLM):
    server = uvicorn.Server(
        uvicorn.Config(llm.build(), host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    return server


# ======================================================================
# 通用工具
# ======================================================================
class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)
        if ok:
            self.passed += 1
        else:
            self.failed += 1

    def summary(self) -> int:
        print(f"\n结果：{self.passed} 通过 / {self.failed} 失败", flush=True)
        return 0 if self.failed == 0 else 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, body: dict | None = None, token: str | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw[:300]}


def wait_http(url: str, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _ws_connect():
    try:
        from websockets.asyncio.client import connect as ws_connect

        return ws_connect
    except ImportError:  # 兼容旧版 websockets
        from websockets import connect as ws_connect

        return ws_connect


async def run_ws(
    base: str,
    token: str,
    sid: str,
    text: str,
    port: int,
    decide: bool | None = None,
    timeout: float = 60.0,
) -> list[dict]:
    """跑一轮对话，只返回**本轮**产生的事件。

    两个坑（前端同样要处理，这里对齐它的做法）：
    * after_seq 取一个极大值：本脚本只关心当前这轮，若从 0 补历史，上一轮的 done
      会让"见到 done 即收工"的客户端提前退出。
    * approval_request 会有两条：loop 写进轨迹的那条 payload 是 {tool, arguments, reason}，
      运行时扇出的那条才带 approval_id（REST 决策要用）。前端按 approval_id 过滤，
      这里也只对带 approval_id 的那条做决策。
    """
    ws_connect = _ws_connect()
    url = (
        f"ws://127.0.0.1:{port}/api/v1/sessions/{sid}/stream"
        f"?after_seq={10**9}&token={token}"
    )
    events: list[dict] = []
    async with ws_connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "prompt", "text": text}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            ev = json.loads(raw)
            events.append(ev)
            payload = ev.get("payload") or {}
            if (
                ev.get("type") == "approval_request"
                and decide is not None
                and payload.get("approval_id")
            ):
                status, _ = http_json(
                    f"{base}/api/v1/approvals/{payload['approval_id']}/decide",
                    {"approved": decide},
                    token,
                )
                if status != 200:
                    print(f"  [WARN] 审批决策失败 http={status}", flush=True)
            if ev.get("type") in ("done", "error"):
                break
    return events


def ev(events: list[dict], kind: str) -> dict | None:
    return next((e for e in events if e.get("type") == kind), None)


def names(items: list[dict]) -> list[str]:
    return [i["name"] for i in items]


# ======================================================================
# 造插件
# ======================================================================
def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_skill(root: Path, name: str, description: str, body: str = "正文") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8"
    )


def drop_fin_pack(home: Path) -> Path:
    """运行中落盘的插件：内嵌技能 + prompt 片段 + bash 放行 + 一个配错的 MCP server。"""
    d = home / "plugins" / "fin-pack"
    write_json(
        d / "plugin.json",
        {
            "name": "fin-pack",
            "version": "1.0.0",
            "description": "冒烟用：内嵌技能 + prompt + bash 放行 + 配错的 MCP",
            "author": "smoke",
            "allowTools": ["bash"],
            "prompt": "【插件 fin-pack】回答先给结论，再给依据。",
            "mcpServers": {"bad-mcp": {"command": "code-agent-not-a-real-binary-xyz", "args": []}},
        },
    )
    write_skill(d / "skills", "demo", "演示技能（冒烟用）")
    return d


def drop_code_pack(home: Path) -> Path:
    """代码式工具插件：声明 codeTools=true，但能否加载看全局开关。"""
    d = home / "plugins" / "code-pack"
    write_json(
        d / "plugin.json",
        {
            "name": "code-pack",
            "version": "1.0.0",
            "description": "冒烟用：代码式工具（双重开关）",
            "codeTools": True,
        },
    )
    (d / "tools.py").write_text(
        "from code_agent.tools.base import Tool\n"
        "class HelloTool(Tool):\n"
        "    name = 'plugin_hello'\n"
        "    description = '插件代码工具'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def execute(self, arguments, ctx):\n"
        f"        return {PLUGIN_TOOL_RESULT!r}\n"
        "def build_tools():\n"
        "    return [HelloTool()]\n",
        encoding="utf-8",
    )
    return d


# ======================================================================
# 起/停服务
# ======================================================================
def start_server(home: Path, llm_port: int, code_tools: bool) -> tuple[subprocess.Popen, int, Path]:
    port = free_port()
    env = os.environ.copy()
    env.update(
        {
            "CODE_AGENT_HOME": str(home),
            "CODE_AGENT_PROVIDER": "openai_compat",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{llm_port}/v1",
            "OPENAI_API_KEY": "smoke-key",
            "OPENAI_MODEL": "fake-model",
            "CODE_AGENT_ADMIN_USER": "admin",
            "CODE_AGENT_ADMIN_PASSWORD": "smoke-pass",
            "CODE_AGENT_SECRET": "smoke-secret-not-for-production-use-0123456789",
            "CODE_AGENT_WS_IDLE_TIMEOUT": "60",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    env.pop("CODE_AGENT_PLUGIN_CODE", None)
    if code_tools:
        env["CODE_AGENT_PLUGIN_CODE"] = "1"

    log_path = home / ("server-code.log" if code_tools else "server-plain.log")
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "code_agent.server", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, port, log_path


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def login(base: str) -> str | None:
    status, data = http_json(
        f"{base}/api/v1/auth/login", {"username": "admin", "password": "smoke-pass"}
    )
    return (data or {}).get("token") if status == 200 and isinstance(data, dict) else None


def new_session(base: str, token: str) -> str:
    _, sess = http_json(f"{base}/api/v1/sessions", {"model_tier": "default"}, token)
    return (sess or {}).get("id", "")


# ======================================================================
# Phase A：声明式能力（不设代码开关）
# ======================================================================
def phase_a(report: Report, home: Path, base: str, port: int, llm: FakeLLM) -> str | None:
    print("\n== Phase A：插件包（声明式能力）+ 统一入口 ==\n", flush=True)
    token = login(base)
    report.check("A1 登录成功", bool(token))
    if not token:
        return None

    sid = new_session(base, token)
    report.check("A2 建会话（用于验证热更新推到已有会话）", bool(sid), f"session={sid}")

    _, before = http_json(f"{base}/api/v1/tools", token=token)
    report.check(
        "A3 初始目录：无技能、无插件、代码工具默认跳过",
        names((before or {}).get("packs", [])) == []
        and names((before or {}).get("plugins", [])) == ["code-pack"]
        and (before or {})["code_tools"]["status"].get("code-pack", "").startswith("已跳过"),
        json.dumps((before or {}).get("code_tools", {}), ensure_ascii=False),
    )

    # ---- 运行中落盘 + reload ----
    drop_fin_pack(home)
    status, reloaded = http_json(f"{base}/api/v1/plugins/reload", {}, token)
    reloaded = reloaded or {}
    report.check(
        "A4 运行中装包：reload 报告 added / skills / 热更新会话数",
        status == 200
        and reloaded.get("added") == ["fin-pack"]
        and "demo" in reloaded.get("skills", [])
        and reloaded.get("active_sessions_updated", 0) >= 1,
        json.dumps(reloaded, ensure_ascii=False)[:180],
    )
    report.check(
        "A5 插件声明的 MCP server 需重启才生效（如实上报）",
        reloaded.get("mcp_restart_required") is True,
    )

    _, cat = http_json(f"{base}/api/v1/tools", token=token)
    cat = cat or {}
    pack = next((p for p in cat.get("packs", []) if p["name"] == "demo"), None)
    plugin = next((p for p in cat.get("plugins", []) if p["name"] == "fin-pack"), None)
    report.check(
        "A6 技能目录带上来源标记 plugin:fin-pack",
        bool(pack) and pack["origin"] == "plugin:fin-pack" and pack["kind"] == "skill",
        json.dumps(pack, ensure_ascii=False),
    )
    report.check(
        "A7 插件清单可读（版本 / 内嵌技能 / 放行名单 / prompt 标记）",
        bool(plugin)
        and plugin["version"] == "1.0.0"
        and plugin["skills_found"] == ["demo"]
        and plugin["allow_tools"] == ["bash"]
        and plugin["has_prompt"] is True,
        json.dumps(plugin, ensure_ascii=False)[:200],
    )
    bash_item = next((t for t in cat.get("tools", []) if t["name"] == "bash"), None)
    report.check(
        "A8 目录里 bash 显示为已放行（放行名单生效的证据）",
        bool(bash_item) and bash_item["policy"] == "allow",
        json.dumps(bash_item, ensure_ascii=False)[:140],
    )
    report.check(
        "A9 代码工具仍未被加载（plugin_hello 不在工具列表）",
        "plugin_hello" not in names(cat.get("tools", [])),
    )

    # ---- 插件 prompt + 内嵌技能真的进了请求 ----
    ev1 = asyncio.run(run_ws(base, token, sid, "介绍一下你自己", port))
    system = llm.last_system()
    report.check(
        "A10 插件 prompt 片段进入已存在会话的 system prompt",
        "回答先给结论" in system,
        f"system 长度={len(system)}",
    )
    report.check(
        "A11 内嵌技能目录进入 system prompt（M4 静默失效 bug 的端到端回归）",
        "demo" in system and "演示技能（冒烟用）" in system,
    )
    report.check("A12 对话正常收口", ev(ev1, "done") is not None, f"{[e.get('type') for e in ev1]}")

    # ---- 放行名单让工具直接执行 ----
    ev2 = asyncio.run(run_ws(base, token, sid, "帮我跑个命令", port))
    tool_res = ev(ev2, "tool_result")
    report.check(
        "A13 放行名单生效：bash 直接执行，无审批打断",
        tool_res is not None and not any(e.get("type") == "approval_request" for e in ev2),
        f"{[e.get('type') for e in ev2]}",
    )
    report.check(
        "A14 命令真的执行了（工作区出现 from_plugin 目录）",
        (home / "workspaces" / sid / "from_plugin").is_dir(),
    )

    # ---- 停用插件：能力全部撤回 ----
    status, toggled = http_json(
        f"{base}/api/v1/plugins/fin-pack/toggle", {"enabled": False}, token
    )
    toggled = toggled or {}
    report.check(
        "A15 停用插件：技能目录 / prompt 全部撤回",
        status == 200
        and "demo" not in toggled.get("skills", [])
        and "demo" not in (toggled.get("plugin", {}) or {}).get("skills_found", []),
        json.dumps(toggled, ensure_ascii=False)[:180],
    )
    on_disk = json.loads((home / "plugins" / "fin-pack" / "plugin.json").read_text(encoding="utf-8"))
    report.check("A16 启停落盘（plugin.json enabled=false）", on_disk.get("enabled") is False)

    _, cat2 = http_json(f"{base}/api/v1/tools", token=token)
    bash_after = next((t for t in (cat2 or {}).get("tools", []) if t["name"] == "bash"), None)
    report.check(
        "A17 停用后 bash 回到默认策略（需审批）",
        bool(bash_after) and bash_after["policy"] == "policy",
        json.dumps(bash_after or {}, ensure_ascii=False)[:140],
    )

    # ---- 反证：没有放行时确实要审批 ----
    sid2 = new_session(base, token)
    ev3 = asyncio.run(run_ws(base, token, sid2, "帮我跑个命令", port, decide=True))
    report.check(
        "A18 反证：未放行时出现审批请求，批准后工具才执行",
        ev(ev3, "approval_request") is not None and ev(ev3, "tool_result") is not None,
        f"{[e.get('type') for e in ev3]}",
    )
    system3 = llm.last_system()
    report.check("A19 停用后插件 prompt 不再进 system prompt", "回答先给结论" not in system3)

    # 恢复启用，供 Phase B 使用
    http_json(f"{base}/api/v1/plugins/fin-pack/toggle", {"enabled": True}, token)
    return token


# ======================================================================
# Phase B：代码式工具双开关 + MCP 配错不崩
# ======================================================================
def phase_b(report: Report, home: Path, base: str, port: int, llm: FakeLLM) -> None:
    print("\n== Phase B：代码工具（CODE_AGENT_PLUGIN_CODE=1）+ MCP 容错 ==\n", flush=True)
    token = login(base)
    report.check("B1 登录成功", bool(token))
    if not token:
        return

    _, cat = http_json(f"{base}/api/v1/tools", token=token)
    cat = cat or {}
    status = cat.get("code_tools", {}).get("status", {})
    report.check(
        "B2 双开关齐备 -> 代码工具已加载",
        status.get("code-pack") == "已加载",
        json.dumps(status, ensure_ascii=False),
    )

    hello = next((t for t in cat.get("tools", []) if t["name"] == "plugin_hello"), None)
    report.check(
        "B3 代码工具进入统一目录：来源 plugin、origin 精确到包名、默认需审批",
        bool(hello)
        and hello["source"] == "plugin"
        and hello["origin"] == "plugin:code-pack"
        and hello["policy"] == "approval",
        json.dumps(hello, ensure_ascii=False)[:180],
    )

    mcp_items = {m["name"]: m for m in cat.get("mcp", [])}
    bad = mcp_items.get("bad-mcp")
    report.check(
        "B4 插件声明的 MCP server 配错：不崩、如实显示未连上",
        bool(bad) and bad["connected"] is False and bool(bad["error"]),
        json.dumps(bad, ensure_ascii=False)[:180],
    )
    report.check(
        "B5 MCP server 来源标记为 plugin:fin-pack",
        bool(bad) and bad["origin"] == "plugin:fin-pack",
    )

    sid = new_session(base, token)
    ev1 = asyncio.run(run_ws(base, token, sid, "用插件工具", port, decide=True))
    tool_use = ev(ev1, "tool_use")
    tool_res = ev(ev1, "tool_result")
    report.check(
        "B6 插件代码工具被真实调用（走到审批 -> 执行）",
        bool(tool_use)
        and tool_use["payload"]["call"]["name"] == "plugin_hello"
        and ev(ev1, "approval_request") is not None,
        f"{[e.get('type') for e in ev1]}",
    )
    report.check(
        "B7 插件代码真的跑出了结果",
        bool(tool_res) and PLUGIN_TOOL_RESULT in (tool_res["payload"].get("result") or ""),
        json.dumps((tool_res or {}).get("payload", {}), ensure_ascii=False)[:160],
    )


# ======================================================================
# 主流程
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(prog="smoke_m10")
    parser.add_argument("--keep", action="store_true", help="保留临时 CODE_AGENT_HOME 便于排查")
    args = parser.parse_args()

    home = Path(tempfile.mkdtemp(prefix="code-agent-smoke-m10-"))
    llm_port = free_port()
    llm = FakeLLM()
    start_fake_llm(llm_port, llm)
    if not wait_http(f"http://127.0.0.1:{llm_port}/docs"):
        print("假 LLM 后端启动失败", file=sys.stderr)
        return 1

    # code-pack 在启动前就位：Phase A 应看到它被"跳过"，Phase B 应看到"已加载"
    drop_code_pack(home)

    report = Report()
    try:
        # ---------------- Phase A ----------------
        proc, port, log_path = start_server(home, llm_port, code_tools=False)
        try:
            base = f"http://127.0.0.1:{port}"
            if not wait_http(f"{base}/healthz"):
                print("Code-agent 启动失败，日志尾部：", file=sys.stderr)
                print(log_path.read_text(encoding="utf-8")[-2000:], file=sys.stderr)
                return 1
            _, health = http_json(f"{base}/healthz")
            report.check("A0 服务健康检查", isinstance(health, dict) and health.get("ok") is True)
            phase_a(report, home, base, port, llm)
        finally:
            stop_server(proc)
            print(f"  [INFO] Phase A 服务已停止（日志 {log_path.name}）", flush=True)

        # ---------------- Phase B：同 home，开代码开关重启 ----------------
        proc2, port2, log2 = start_server(home, llm_port, code_tools=True)
        try:
            base2 = f"http://127.0.0.1:{port2}"
            if not wait_http(f"{base2}/healthz"):
                print("Code-agent 重启失败，日志尾部：", file=sys.stderr)
                print(log2.read_text(encoding="utf-8")[-2000:], file=sys.stderr)
                return report.summary()
            phase_b(report, home, base2, port2, llm)
        finally:
            stop_server(proc2)
            print(f"  [INFO] Phase B 服务已停止（日志 {log2.name}）", flush=True)
    finally:
        if args.keep:
            print(f"\n临时目录保留在：{home}")
        else:
            shutil.rmtree(home, ignore_errors=True)

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
