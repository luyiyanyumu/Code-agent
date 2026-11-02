"""Background JSON-RPC remains functional without Windows console windows."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from code_agent.extensions import jsonrpc_stdio
from code_agent.extensions.jsonrpc_stdio import JsonRpcError, JsonRpcStdio


SERVER = r'''
import ctypes,json,os,subprocess,sys,time
framing, stubborn, child_port = sys.argv[1:]
child = None
if stubborn == "yes":
    child_code = "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',"+child_port+")); s.listen(); time.sleep(60)"
    child = subprocess.Popen([sys.executable,"-u","-c",child_code],
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
sys.stderr.write("后台服务中文日志\n");sys.stderr.flush()
while True:
    if framing == "newline":
        line=sys.stdin.buffer.readline()
        if not line: break
        msg=json.loads(line)
    else:
        headers={}
        while True:
            line=sys.stdin.buffer.readline()
            if not line: break
            if line in (b"\r\n",b"\n"): break
            key,_,value=line.decode().partition(":")
            headers[key.lower()]=value.strip()
        if not line: break
        msg=json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    if msg.get("method")=="test/hang": continue
    if "id" not in msg: continue
    result={"text":"握手成功","pid":os.getpid(),"child_pid":child.pid if child else None,
        "console_hwnd":int(ctypes.windll.kernel32.GetConsoleWindow()) if os.name=="nt" else None}
    body=json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result},ensure_ascii=False).encode("utf-8")
    if framing == "content-length": sys.stdout.buffer.write(b"Content-Length: "+str(len(body)).encode()+b"\r\n\r\n")
    sys.stdout.buffer.write(body+(b"\n" if framing=="newline" else b""));sys.stdout.buffer.flush()
if stubborn == "yes": time.sleep(60)
'''


def write_server(tmp_path: Path) -> Path:
    path = tmp_path / "后台服务 with spaces.py"
    path.write_text(SERVER, encoding="utf-8")
    return path


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.mark.parametrize("framing", ["newline", "content-length"])
def test_background_stdio_preserves_protocol_logs_and_graceful_exit(tmp_path, monkeypatch, framing):
    script = write_server(tmp_path)
    calls = []
    launch = asyncio.create_subprocess_exec

    async def recording(*argv, **kwargs):
        calls.append((argv, kwargs))
        return await launch(*argv, **kwargs)

    monkeypatch.setattr(jsonrpc_stdio.asyncio, "create_subprocess_exec", recording)

    async def flow():
        client = JsonRpcStdio(sys.executable, ["-u", str(script), framing, "no", "0"], framing=framing)
        await client.start()
        process = client._proc
        try:
            result = await client.request("initialize", timeout=5)
            # Windows venv python.exe can be a redirector with a child PID.
            assert result["text"] == "握手成功" and result["pid"] > 0
            for _ in range(50):
                if "后台服务中文日志" in client.stderr_tail():
                    break
                await asyncio.sleep(.01)
            assert "后台服务中文日志" in client.stderr_tail()
        finally:
            await client.stop()
        assert process.returncode == 0
        assert client._proc is None and not client.alive and not client._pending

    asyncio.run(flow())
    assert len(calls) == 1, "A cooperative server must not need a taskkill process"
    flags = calls[0][1]["creationflags"]
    if os.name == "nt":
        assert flags & subprocess.CREATE_NO_WINDOW
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert flags == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree and console flags")
def test_real_background_stdio_child_has_no_allocated_console(tmp_path):
    script = write_server(tmp_path)

    async def flow():
        client = JsonRpcStdio(sys.executable, ["-u", str(script), "newline", "no", "0"])
        await client.start()
        try:
            result = await client.request("initialize", timeout=5)
            assert result["text"] == "握手成功"
            assert result["console_hwnd"] == 0, "The real interpreter must not own a console, including a hidden one"
        finally:
            await client.stop()

    asyncio.run(flow())


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree and console flags")
def test_timeout_shutdown_hides_taskkill_and_reaps_server_child(tmp_path, monkeypatch):
    script = write_server(tmp_path)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    calls = []
    launch = asyncio.create_subprocess_exec

    async def recording(*argv, **kwargs):
        calls.append((argv, kwargs))
        return await launch(*argv, **kwargs)

    monkeypatch.setattr(jsonrpc_stdio.asyncio, "create_subprocess_exec", recording)

    async def flow():
        client = JsonRpcStdio(sys.executable, ["-u", str(script), "content-length", "yes", str(port)], framing="content-length")
        await client.start()
        process = client._proc
        try:
            result = await client.request("initialize", timeout=5)
            assert result["child_pid"]
            for _ in range(100):
                if listening(port):
                    break
                await asyncio.sleep(.02)
            assert listening(port), "The real descendant must have started"
            with pytest.raises(JsonRpcError, match="请求超时"):
                await client.request("test/hang", timeout=.1)
            assert not client._pending
        finally:
            await client.stop(grace=3)
        assert process.returncode is not None and not client.alive
        assert not listening(port), "Shutdown must stop the owned descendant too"

    asyncio.run(flow())
    assert len(calls) == 2 and calls[1][0][0] == "taskkill"
    assert calls[0][1]["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert calls[0][1]["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    assert calls[1][1]["creationflags"] & subprocess.CREATE_NO_WINDOW
