"""Fake LSP server（测试夹具）：Content-Length 帧的极简语言服务器。

用途：在**没有安装任何真实语言服务器**的环境里，确定性地验证我们的 LSP 代理：
握手、文档同步、诊断落地与地图角标、请求/通知双向转发、服务端主动请求的响应。

刻意设的"陷阱"（用来验证客户端实现是否完整）：
- 启动后先发一个 `window/workDoneProgress/create` **请求**，并等它的响应；
  客户端若不响应，诊断就永远不会发出（测试会因此失败，而不是静默通过）。
- 诊断内容由文档文本决定：含 `TODO_ERR` 的行报 error、含 `unused` 的行报 warning，
  另外每篇文档恒定报一条 info——便于断言"计数与归属"。
- 收到 shutdown 后回结果，收到 exit 后退出。
"""

from __future__ import annotations

import json
import sys


def read_message() -> dict | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("ascii", errors="replace").strip()
        if ":" in text:
            key, _, value = text.partition(":")
            headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    body = sys.stdin.buffer.read(length)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def send(payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()


def send_request(rid: int, method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def diagnostics_for(uri: str, text: str) -> list[dict]:
    items: list[dict] = []
    for idx, line in enumerate(text.split("\n")):
        if "TODO_ERR" in line:
            items.append(
                {
                    "range": {
                        "start": {"line": idx, "character": 0},
                        "end": {"line": idx, "character": len(line)},
                    },
                    "severity": 1,
                    "message": f"fake: 这一行有错误（第 {idx + 1} 行）",
                    "source": "fake-lsp",
                    "code": "E100",
                }
            )
        if "unused" in line:
            items.append(
                {
                    "range": {
                        "start": {"line": idx, "character": 0},
                        "end": {"line": idx, "character": len(line)},
                    },
                    "severity": 2,
                    "message": "fake: 疑似未使用的变量",
                    "source": "fake-lsp",
                    "code": "W200",
                }
            )
    items.append(
        {
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
            "severity": 3,
            "message": "fake: 文档信息",
            "source": "fake-lsp",
        }
    )
    return items


def main() -> None:
    documents: dict[str, str] = {}
    progress_ready = False
    progress_rid = 9001
    send_request(progress_rid, "window/workDoneProgress/create", {"token": "fake-index"})

    while True:
        message = read_message()
        if message is None:
            return
        method = message.get("method")
        rid = message.get("id")

        if rid == progress_rid and "result" in message:
            progress_ready = True
            continue
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "capabilities": {
                            "textDocumentSync": 1,
                            "completionProvider": {"triggerCharacters": ["."]},
                            "hoverProvider": True,
                            "definitionProvider": True,
                        },
                        "serverInfo": {"name": "fake-lsp", "version": "1.0"},
                    },
                }
            )
        elif method == "initialized":
            continue
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "exit":
            return
        elif method == "textDocument/didOpen":
            doc = message["params"]["textDocument"]
            documents[doc["uri"]] = doc.get("text", "")
            # 等客户端的请求响应；拿不到也发（但把 fact 写进 stderr 便于诊断）
            if not progress_ready:
                sys.stderr.write("fake-lsp: 客户端未响应 progress 请求\n")
                sys.stderr.flush()
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": doc["uri"], "diagnostics": diagnostics_for(doc["uri"], doc["text"])},
                }
            )
        elif method == "textDocument/didChange":
            doc = message["params"]["textDocument"]
            text = ""
            for change in message["params"].get("contentChanges") or []:
                text = change.get("text", text)
            documents[doc["uri"]] = text
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": doc["uri"], "diagnostics": diagnostics_for(doc["uri"], text)},
                }
            )
        elif method == "textDocument/didClose":
            uri = message["params"]["textDocument"]["uri"]
            documents.pop(uri, None)
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": uri, "diagnostics": []},
                }
            )
        elif method == "textDocument/didSave":
            continue
        elif method == "textDocument/completion":
            uri = message["params"]["textDocument"]["uri"]
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "isIncomplete": False,
                        "items": [
                            {
                                "label": "fake_symbol",
                                "kind": 3,
                                "detail": "来自 fake-lsp",
                                "documentation": {"kind": "markdown", "value": "**假补全项**"},
                                "insertText": "fake_symbol",
                            }
                        ],
                        "uri": uri,
                    },
                }
            )
        elif method == "textDocument/hover":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "contents": {"kind": "markdown", "value": "**fake hover**：这里是一个符号"},
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 5},
                        },
                    },
                }
            )
        elif method == "textDocument/definition":
            uri = message["params"]["textDocument"]["uri"]
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "uri": uri,
                        "range": {
                            "start": {"line": 2, "character": 0},
                            "end": {"line": 2, "character": 4},
                        },
                    },
                }
            )
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "result": None})


if __name__ == "__main__":
    main()
