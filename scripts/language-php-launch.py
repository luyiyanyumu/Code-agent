"""PHP LSP transport: PHPantom semantics plus bundled PHP's syntax-only lint.

PHPantom 0.11.1 can omit parse diagnostics for a file with no declarations.
The PHP compiler's -l mode parses stdin without evaluating the program. This
adapter retains native LSP messages and augments only diagnostic notifications.
All pipe readers use daemon threads, avoiding cancelled Windows Proactor reads.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading

for ancestor in Path(__file__).resolve().parents:
    if (ancestor / "code_agent").is_dir():
        sys.path.insert(0, str(ancestor))
        break
from code_agent.extensions.jsonrpc_stdio import _Decoder, encode_message
from code_agent.codeintel.documentation_prewarm import apply_changes
from code_agent.codeintel.uri import normalize_uri


@dataclass
class Document:
    uri: str
    version: int
    text: str
    native: list[dict] = field(default_factory=list)
    lint: list[dict] = field(default_factory=list)
    lint_version: int | None = None
    timer: threading.Timer | None = None


def syntax_diagnostics(output: str, text: str) -> list[dict]:
    match = re.search(r"(?:PHP )?(?:Parse|Fatal) error:\s*(.*?)\s+in Standard input code on line (\d+)", output, re.S)
    if not match:
        return []
    lines = text.splitlines()
    line = max(0, int(match.group(2)) - 1)
    length = len(lines[line].encode("utf-16-le")) // 2 if line < len(lines) else 0
    return [{"range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": length}},
             "severity": 1, "code": "syntax_error", "source": "php-lint", "message": match.group(1).strip()}]


class PhpBridge:
    def __init__(self, native: Path, php: Path, *, output=None):
        self.native_path, self.php = native, php
        self.output = output or sys.stdout.buffer
        self.lock, self.output_lock, self.native_lock = threading.RLock(), threading.Lock(), threading.Lock()
        self.documents: dict[str, Document] = {}
        self.process = None
        self.active_lints: set[subprocess.Popen] = set()
        self.lint_slots = threading.Semaphore(2)
        self.stopping = False

    def emit(self, message: dict) -> None:
        with self.output_lock:
            self.output.write(encode_message(message, "content-length"))
            self.output.flush()

    def publish(self, document: Document) -> None:
        native = document.native
        lint = document.lint if document.lint_version == document.version else []
        # Keep PHPantom's precise ranges when its parser already reports the error.
        if any(str(item.get("code")) == "syntax_error" for item in native):
            lint = []
        self.emit({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {
            "uri": document.uri, "version": document.version, "diagnostics": [*native, *lint]}})

    def from_native(self, message: dict) -> None:
        if message.get("method") == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            with self.lock:
                document = self.documents.get(normalize_uri(params.get("uri", "")))
                if document is not None:
                    version = params.get("version")
                    if version is not None and version != document.version:
                        return
                    document.native = params.get("diagnostics") or []
                    self.publish(document)
                    return
        self.emit(message)

    def synchronize(self, message: dict) -> None:
        method, params = message.get("method"), message.get("params") or {}
        document = params.get("textDocument") or {}
        uri = document.get("uri")
        if not isinstance(uri, str):
            return
        key = normalize_uri(uri)
        with self.lock:
            if method == "textDocument/didClose":
                previous = self.documents.pop(key, None)
                if previous and previous.timer:
                    previous.timer.cancel()
                return
            previous = self.documents.get(key)
            if method == "textDocument/didOpen":
                text, version = document.get("text"), document.get("version")
            elif method == "textDocument/didChange" and previous:
                text = apply_changes(previous.text, params.get("contentChanges") or [])
                version = document.get("version")
            else:
                return
            if not isinstance(text, str) or not isinstance(version, int):
                return
            if previous and version <= previous.version:
                return
            if previous and previous.timer:
                previous.timer.cancel()
            state = Document(uri, version, text)
            self.documents[key] = state
            timer = threading.Timer(.08, self.lint, args=(key, state))
            timer.daemon = True
            state.timer = timer
            timer.start()

    def lint(self, key: str, state: Document) -> None:
        with self.lint_slots:
            with self.lock:
                if self.stopping or self.documents.get(key) is not state:
                    return
                process = subprocess.Popen([str(self.php), "-n", "-d", "display_errors=1", "-d", "log_errors=0", "-l"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.active_lints.add(process)
            try:
                try:
                    output, _ = process.communicate(state.text.encode("utf-8"), timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    return
                diagnostics = syntax_diagnostics(output.decode("utf-8", "replace"), state.text)
                if process.returncode and not diagnostics:
                    self.emit({"jsonrpc": "2.0", "method": "window/logMessage", "params": {
                        "type": 1, "message": "内置 PHP 语法检查未完成：" + output.decode("utf-8", "replace")[:500]}})
                    return
                with self.lock:
                    if not self.stopping and self.documents.get(key) is state:
                        state.lint, state.lint_version = diagnostics, state.version
                        self.publish(state)
            finally:
                with self.lock:
                    self.active_lints.discard(process)

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            for state in self.documents.values():
                if state.timer:
                    state.timer.cancel()
            processes = [*self.active_lints, *([self.process] if self.process else [])]
            for process in processes:
                if process.poll() is None:
                    process.kill()
        for process in processes:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def run(self) -> int:
        self.process = subprocess.Popen([str(self.native_path), "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr.buffer, bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        events = queue.Queue()

        def read(stream, native=False):
            decoder = _Decoder("content-length")
            read_chunk = getattr(stream, "read1", stream.read)
            try:
                while chunk := read_chunk(65536):
                    for message in decoder.feed(chunk):
                        if native:
                            self.from_native(message)
                        else:
                            events.put(message)
            finally:
                events.put(None)

        for stream, native in ((self.process.stdout, True), (sys.stdin.buffer, False)):
            threading.Thread(target=read, args=(stream, native), daemon=True).start()
        try:
            while True:
                message = events.get()
                if message is None:
                    break
                self.synchronize(message)
                with self.native_lock:
                    self.process.stdin.write(encode_message(message, "content-length"))
                    self.process.stdin.flush()
            return 0
        finally:
            self.stop()


if __name__ == "__main__":
    directory = Path(__file__).resolve().parent
    raise SystemExit(PhpBridge(directory / "phpantom_lsp.exe", directory.parent / "php/php.exe").run())
