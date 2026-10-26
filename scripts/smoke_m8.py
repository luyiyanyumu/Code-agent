"""M8 端到端流式冒烟：本地假 OpenAI 兼容后端 -> Code-agent -> WebSocket 客户端。

为什么需要它：单元测试用 httpx.MockTransport 拦在进程内，验证不了
"真 HTTP + 真 SSE + 真 WS 推送" 这条完整链路。本脚本起一个真的 HTTP 服务
扮演 OpenAI 兼容后端，再起一个真的 Code-agent 进程指向它，最后用真的 WS
客户端收事件，断言：

  1. 流式：收到 >= 3 个 assistant_delta，拼接后与最终 done.text 完全一致
  2. 增量不入轨迹：assistant_delta 的 seq == 0，且 /events 回放里查无此类事件
  3. 工具链：流式 tool_calls 分片能被正确累积 -> 工具真的执行 -> 第二轮收口
  4. 重试退避：后端连续 2 次 503 后恢复，客户端仍拿到正常结果（且真的重试了 3 次）

用法：
    python scripts/smoke_m8.py

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

ANSWER = "Code-agent 流式链路已贯通：provider 增量 → 事件总线 → WebSocket → 前端打字机。"
#: 按 9 个字符切片，保证多片（CJK 按字符切不会截断字节）
ANSWER_PARTS = [ANSWER[i : i + 9] for i in range(0, len(ANSWER), 9)]


# ======================================================================
# 假 OpenAI 兼容后端
# ======================================================================
def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
    body = {
        "id": "chatcmpl-smoke",
        "object": "chat.completion.chunk",
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return _sse(body)


def _text_stream():
    for part in ANSWER_PARTS:
        yield _chunk({"content": part})
    yield _chunk({}, finish="stop", usage={"prompt_tokens": 41, "completion_tokens": 23})
    yield "data: [DONE]\n\n"


def _tool_stream():
    """tool_calls 故意拆成两片，验证 provider 的 index 累积逻辑。"""
    yield _chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_smoke_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "'},
                }
            ]
        }
    )
    yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'README.md"}'}}]})
    yield _chunk({}, finish="tool_calls", usage={"prompt_tokens": 55, "completion_tokens": 17})
    yield "data: [DONE]\n\n"


class FakeLLM:
    """记录请求次数，可注入连续失败，用于验证重试退避。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail_next = 0
        self.fail_status = 503

    def build(self):
        app = FastAPI()
        state = self

        @app.post("/__config")
        async def configure(body: dict) -> dict:
            state.fail_next = int(body.get("fail_next", 0))
            state.fail_status = int(body.get("fail_status", 503))
            return {"ok": True}

        @app.get("/__stats")
        async def stats() -> dict:
            return {"requests": len(state.requests)}

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            state.requests.append(body)

            if state.fail_next > 0:
                state.fail_next -= 1
                return JSONResponse(
                    {"error": {"message": "上游过载，请稍后重试"}},
                    status_code=state.fail_status,
                )

            messages = body.get("messages") or []
            hit_tool = any(
                m.get("role") == "user" and "读取 README" in (m.get("content") or "")
                for m in messages
            )
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            use_tool = hit_tool and not has_tool_result

            if not body.get("stream"):
                return JSONResponse(_non_stream(use_tool))

            stream = _tool_stream() if use_tool else _text_stream()
            return StreamingResponse(stream, media_type="text/event-stream")

        return app

    def _non_stream(self, use_tool: bool) -> dict:
        if use_tool:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_smoke_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": ANSWER}
            finish = "stop"
        return {
            "id": "chatcmpl-smoke",
            "object": "chat.completion",
            "model": "fake-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 41, "completion_tokens": 23},
        }


def start_fake_llm(port: int, llm: FakeLLM):
    config = uvicorn.Config(llm.build(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    return server


# ======================================================================
# 工具函数
# ======================================================================
class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f"  -- {detail}"
        print(line, flush=True)
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


def wait_http(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


# ======================================================================
# WS 场景
# ======================================================================
def _ws_connect():
    try:
        from websockets.asyncio.client import connect as ws_connect

        return ws_connect
    except ImportError:  # 兼容旧版 websockets
        from websockets import connect as ws_connect

        return ws_connect


async def run_ws(url: str, text: str, timeout: float = 45.0) -> list[dict]:
    ws_connect = _ws_connect()
    events: list[dict] = []
    async with ws_connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "prompt", "text": text}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") in ("done", "error"):
                break
    return events


def deltas_of(events: list[dict]) -> str:
    return "".join(e["payload"].get("text", "") for e in events if e.get("type") == "assistant_delta")


# ======================================================================
# 主流程
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(prog="smoke_m8")
    parser.add_argument("--keep", action="store_true", help="保留临时 CODE_AGENT_HOME 便于排查")
    args = parser.parse_args()

    port = free_port()
    llm_port = free_port()
    home = Path(tempfile.mkdtemp(prefix="code-agent-smoke-m8-"))

    llm = FakeLLM()
    start_fake_llm(llm_port, llm)
    if not wait_http(f"http://127.0.0.1:{llm_port}/__stats"):
        print("假 LLM 后端启动失败", file=sys.stderr)
        return 1

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
            "CODE_AGENT_LLM_RETRIES": "3",
            "CODE_AGENT_WS_IDLE_TIMEOUT": "30",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    log = (home / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "code_agent.server", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    report = Report()
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_http(f"{base}/healthz", timeout=45):
            print("Code-agent 启动失败，日志尾部：", file=sys.stderr)
            print((home / "server.log").read_text(encoding="utf-8")[-1500:], file=sys.stderr)
            return 1

        print("\n== M8 流式贯通冒烟 ==\n", flush=True)
        _, health = http_json(f"{base}/healthz")
        report.check("服务健康检查", isinstance(health, dict) and health.get("ok") is True, str(health))

        status, login = http_json(
            f"{base}/api/v1/auth/login", {"username": "admin", "password": "smoke-pass"}
        )
        token = (login or {}).get("token") if isinstance(login, dict) else None
        report.check("登录成功", status == 200 and bool(token))
        if not token:
            return report.summary()

        _, sess = http_json(f"{base}/api/v1/sessions", {"model_tier": "default"}, token)
        sid = (sess or {}).get("id")
        report.check("创建会话", bool(sid), f"session={sid}")

        ws_url = f"ws://127.0.0.1:{port}/api/v1/sessions/{sid}/stream?after_seq=0&token={token}"

        # ---- 场景 1：纯文本流式 ----
        ev1 = asyncio.run(run_ws(ws_url, "用一句话介绍你自己"))
        meta1 = next((e for e in ev1 if e.get("type") == "session_meta" and e["payload"].get("model")), None)
        report.check(
            "路由档位模型来自 provider 默认（未误用 deepseek 模型名）",
            bool(meta1) and meta1["payload"]["model"] == "fake-model",
            f"model={(meta1 or {}).get('payload', {}).get('model')}",
        )
        n_delta = sum(1 for e in ev1 if e.get("type") == "assistant_delta")
        err1 = next((e for e in ev1 if e.get("type") == "error"), None)
        if err1:
            print(f"  [DIAG] 服务端 error 事件：{err1['payload'].get('message')}", flush=True)
        joined = deltas_of(ev1)
        done = next((e for e in ev1 if e.get("type") == "done"), None)
        report.check("流式：收到多个 assistant_delta", n_delta >= 3, f"deltas={n_delta}")
        report.check("流式：增量拼接 == 最终回答", joined == ANSWER, f"joined={joined[:40]}...")
        report.check(
            "流式：done 文本与增量一致",
            done is not None and done["payload"]["text"] == ANSWER,
        )
        report.check(
            "流式：增量不落轨迹（seq=0）",
            all(e.get("seq") == 0 for e in ev1 if e.get("type") == "assistant_delta"),
        )

        _, replay = http_json(f"{base}/api/v1/sessions/{sid}/events?after_seq=0", token=token)
        replay = replay or []
        types = [e.get("type") for e in replay]
        report.check("回放：不含 assistant_delta（未持久化）", "assistant_delta" not in types, f"{types[:8]}")
        report.check("回放：含 done 与 assistant_message", "done" in types)

        # ---- 场景 2：流式 tool_calls + 真的执行工具 ----
        _, sess2 = http_json(f"{base}/api/v1/sessions", {"model_tier": "default"}, token)
        sid2 = (sess2 or {}).get("id")
        # 工具在工作区 <home>/workspaces/<session_id> 内解析相对路径，先把待读文件放好
        ws2 = home / "workspaces" / sid2
        ws2.mkdir(parents=True, exist_ok=True)
        (ws2 / "README.md").write_text("smoke workspace readme\n", encoding="utf-8")
        ws_url2 = f"ws://127.0.0.1:{port}/api/v1/sessions/{sid2}/stream?after_seq=0&token={token}"
        ev2 = asyncio.run(run_ws(ws_url2, "帮我读取 README"))
        t_types = [e.get("type") for e in ev2]
        tool_use = next((e for e in ev2 if e.get("type") == "tool_use"), None)
        tool_res = next((e for e in ev2 if e.get("type") == "tool_result"), None)
        report.check("工具链：流式分片拼出 tool_calls", tool_use is not None, f"{t_types}")
        report.check(
            "工具链：参数分片累积正确",
            bool(tool_use) and tool_use["payload"]["call"]["name"] == "read_file"
            and tool_use["payload"]["call"]["arguments"].get("path") == "README.md",
            json.dumps((tool_use or {}).get("payload", {}).get("call", {}), ensure_ascii=False)[:120],
        )
        report.check(
            "工具链：工具真实执行且读到内容",
            bool(tool_res) and "smoke workspace readme" in (tool_res["payload"].get("result") or ""),
        )
        report.check(
            "工具链：第二轮收口为 done",
            any(e.get("type") == "done" for e in ev2),
        )

        # ---- 场景 3：重试退避（真 503）----
        http_json(f"http://127.0.0.1:{llm_port}/__config", {"fail_next": 2, "fail_status": 503})
        before = len(llm.requests)
        _, sess3 = http_json(f"{base}/api/v1/sessions", {"model_tier": "default"}, token)
        sid3 = (sess3 or {}).get("id")
        ws_url3 = f"ws://127.0.0.1:{port}/api/v1/sessions/{sid3}/stream?after_seq=0&token={token}"
        t0 = time.time()
        ev3 = asyncio.run(run_ws(ws_url3, "介绍一下自己"))
        elapsed = time.time() - t0
        used = len(llm.requests) - before
        done3 = next((e for e in ev3 if e.get("type") == "done"), None)
        report.check("重试：503 后仍拿到正常结果", bool(done3) and done3["payload"]["text"] == ANSWER)
        report.check("重试：确实重发了请求（3 次）", used == 3, f"requests={used}")
        report.check("重试：发生退避等待", elapsed > 1.5, f"elapsed={elapsed:.2f}s")
        report.check("重试：无 error 事件泄漏给客户端", not any(e.get("type") == "error" for e in ev3))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)
        else:
            print(f"\n临时目录保留在：{home}")

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
