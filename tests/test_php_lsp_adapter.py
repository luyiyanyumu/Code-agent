"""PHP-specific syntax augmentation does not disturb the native LSP protocol."""
import importlib.util
import io
from pathlib import Path
import sys
import threading


spec = importlib.util.spec_from_file_location("php_lsp_adapter", Path(__file__).resolve().parents[1] / "scripts/language-php-launch.py")
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


class Timer:
    def __init__(self, *args, **kwargs):
        self.cancelled = False
    def start(self):
        pass
    def cancel(self):
        self.cancelled = True


def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter.threading, "Timer", Timer)
    output = io.BytesIO()
    bridge = adapter.PhpBridge(tmp_path / "native.exe", tmp_path / "php.exe", output=output)
    uri = (tmp_path / "demo.php").as_uri()
    return bridge, output, uri


def event(bridge, uri, method, version=1, text="<?php\n$result = ;\n"):
    document = {"uri": uri, "version": version, "text": text}
    params = {"textDocument": document}
    if method == "didChange":
        params["contentChanges"] = [{"text": text}]
    bridge.synchronize({"method": "textDocument/" + method, "params": params})


def messages(output):
    return adapter._Decoder("content-length").feed(output.getvalue())


def test_compiler_syntax_error_uses_lsp_utf16_range():
    result = adapter.syntax_diagnostics("Parse error: syntax error, unexpected token \";\" in Standard input code on line 2\nErrors parsing Standard input code", "<?php\n$变量 = '😀' ;\n")
    assert len(result) == 1 and result[0]["severity"] == 1
    assert result[0]["source"] == "php-lint"
    assert result[0]["range"]["start"]["line"] == 1
    assert result[0]["range"]["end"]["character"] == len("$变量 = '😀' ;".encode("utf-16-le")) // 2
    assert adapter.syntax_diagnostics("No syntax errors detected in Standard input code", "<?php") == []


def test_native_empty_diagnostics_retain_current_compiler_error(tmp_path, monkeypatch):
    bridge, output, uri = fixture(tmp_path, monkeypatch)
    event(bridge, uri, "didOpen")
    document = bridge.documents[adapter.normalize_uri(uri)]
    document.lint_version = 1
    document.lint = adapter.syntax_diagnostics("Parse error: unexpected ; in Standard input code on line 2", document.text)
    bridge.from_native({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}})
    assert messages(output)[-1]["params"]["diagnostics"][0]["source"] == "php-lint"
    assert messages(output)[-1]["params"]["version"] == 1


def test_native_syntax_error_prevents_duplicate_compiler_error(tmp_path, monkeypatch):
    bridge, output, uri = fixture(tmp_path, monkeypatch)
    event(bridge, uri, "didOpen")
    document = bridge.documents[adapter.normalize_uri(uri)]
    document.lint_version, document.lint = 1, [{"source": "php-lint", "code": "syntax_error"}]
    native = [{"source": "phpantom", "code": "syntax_error", "message": "precise native error"}]
    bridge.from_native({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": native}})
    assert messages(output)[-1]["params"]["diagnostics"] == native


def test_incremental_changes_cancel_old_timer_and_close_cancels_latest(tmp_path, monkeypatch):
    bridge, output, uri = fixture(tmp_path, monkeypatch)
    event(bridge, uri, "didOpen", text="<?php\n$result = 1;\n")
    old = bridge.documents[adapter.normalize_uri(uri)]
    bridge.synchronize({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 2},
        "contentChanges": [{"range": {"start": {"line": 1, "character": 10}, "end": {"line": 1, "character": 11}}, "text": "2"}]}})
    current = bridge.documents[adapter.normalize_uri(uri)]
    assert old.timer.cancelled and current.version == 2 and current.text == "<?php\n$result = 2;\n"
    bridge.from_native({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "version": 1, "diagnostics": [{"message": "stale"}]}})
    assert not messages(output)
    event(bridge, uri, "didClose")
    assert current.timer.cancelled and not bridge.documents


def test_stale_compiler_process_cannot_publish_after_document_changes(tmp_path, monkeypatch):
    bridge, output, uri = fixture(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    class Process:
        returncode = 255
        def __init__(self, *args, **kwargs):
            assert args[0][1:] == ["-n", "-d", "display_errors=1", "-d", "log_errors=0", "-l"]
        def communicate(self, value=None, timeout=None):
            started.set()
            assert release.wait(2)
            return b"Parse error: unexpected ; in Standard input code on line 2", None
    monkeypatch.setattr(adapter.subprocess, "Popen", Process)
    event(bridge, uri, "didOpen")
    key = adapter.normalize_uri(uri)
    pending = threading.Thread(target=bridge.lint, args=(key, bridge.documents[key]))
    pending.start()
    assert started.wait(2)
    event(bridge, uri, "didChange", 2, "<?php\n$result = 1;\n")
    release.set()
    pending.join(2)
    assert not pending.is_alive() and not messages(output) and not bridge.active_lints


def test_non_diagnostic_native_messages_are_preserved(tmp_path, monkeypatch):
    bridge, output, _ = fixture(tmp_path, monkeypatch)
    response = {"jsonrpc": "2.0", "id": 47, "result": {"contents": {"kind": "markdown", "value": "真实函数签名"}}}
    bridge.from_native(response)
    assert messages(output) == [response]
