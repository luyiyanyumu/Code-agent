"""Offline fixtures for legacy/mixed editor reads; original bytes stay intact."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from code_agent.codeintel import projects as P
from code_agent.codeintel.hashing import sha1_text
from code_agent.codeintel.projects import ProjectService
from code_agent.codeintel.text_encoding import decode_text, read_text_range


@pytest.fixture
def reader(tmp_path):
    return ProjectService(None), {"root_path": str(tmp_path)}, tmp_path


def _read(reader, raw: bytes, *, name="fixture.log", start=None, end=None):
    service, project, root = reader
    target = root / name
    target.write_bytes(raw)
    result = service.read_file(project, name, start, end)
    assert target.read_bytes() == raw
    return result


def test_valid_utf8_remains_editable_with_existing_newlines_and_hash(reader):
    source = "中文和🌟\r\n第二行\r末行\n合法替换字符：\ufffd"
    result = _read(reader, source.encode("utf-8"))
    expected = source.replace("\r\n", "\n").replace("\r", "\n")
    assert result["text"] == expected
    assert result["sha1"] == sha1_text(expected)
    assert result["encoding"] == "utf-8" and result["editable"] is True
    assert result["readonly_reason"] == result["encoding_warning"] == ""


@pytest.mark.parametrize("name", ["fixture.log", "fixture.txt", "fixture.java"])
def test_legacy_chinese_is_displayed_readonly(reader, name):
    raw = "第一行：旧编码\r\n第二行\n".encode("gb18030")
    result = _read(reader, raw, name=name)
    assert result["text"] == "第一行：旧编码\n第二行\n"
    assert result["encoding"] == "gb18030" and result["editable"] is False
    assert result["readonly_reason"] == "encoding" and "只读" in result["encoding_warning"]


def test_mixed_log_preserves_utf8_and_recovers_legacy_lines(reader):
    raw = ("正常中文和🌟\r\n".encode("utf-8") + "历史记录：旧编码\r\n".encode("gb18030")
           + b"ASCII\n" + "结束中文\r".encode("utf-8"))
    result = _read(reader, raw)
    assert result["text"] == "正常中文和🌟\n历史记录：旧编码\nASCII\n结束中文\n"
    assert "\ufffd" not in result["text"]
    assert result["encoding"] == "utf-8+gb18030"
    assert result["editable"] is False and result["readonly_reason"] == "encoding"


@pytest.mark.parametrize("name,encoding", [("fixture.log", "utf-8+gb18030"), ("fixture.txt", "gb18030")])
def test_range_uses_the_same_decoding_and_whole_file_readonly_metadata(reader, name, encoding):
    raw = ("正常中文\r\n".encode("utf-8") + b"ASCII\r"
           + "旧编码记录\r\n".encode("gb18030") + "结束\n".encode("utf-8")) if name.endswith(".log") else "正常中文\r\nASCII\r旧编码记录\r\n结束\n".encode("gb18030")
    result = _read(reader, raw, name=name, start=2, end=3)
    assert result["text"] == "ASCII\n旧编码记录"
    assert result["start_line"] == 2 and result["end_line"] == 3
    assert result["encoding"] == encoding and result["editable"] is False
    # A selected ASCII line cannot hide legacy data outside the returned range.
    service, project, _ = reader
    ascii_only = service.read_file(project, name, 2, 2)
    assert ascii_only["text"] == "ASCII" and ascii_only["editable"] is False
    assert ascii_only["encoding"] == encoding


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030"])
def test_streaming_range_handles_multibyte_characters_at_probe_chunk_boundary(tmp_path, encoding):
    raw = b"a" * 65535 + "中文\r\n第二行\r第三行\n".encode(encoding)
    target = tmp_path / "fixture.txt"
    target.write_bytes(raw)
    decoded, count = read_text_range(target, 1, 3, inspection_limit=128 * 1024)
    assert decoded.text == "a" * 65535 + "中文\n第二行\n第三行"
    assert decoded.encoding == encoding and count == 3


@pytest.mark.parametrize("name", ["fixture.log", "fixture.txt"])
def test_unknown_encoding_has_explicit_readonly_warning(reader, name):
    result = _read(reader, b"ascii\ninvalid \xff\n", name=name)
    assert result["encoding"] == "unknown" and result["editable"] is False
    assert result["readonly_reason"] == "encoding"
    assert "无法" in result["encoding_warning"] and "\ufffd" in result["text"]


def test_range_remains_streamed_and_capped(reader, monkeypatch):
    service, project, root = reader
    target = root / "fixture.log"
    raw = "中文\n".encode("utf-8") * 8
    target.write_bytes(raw)
    original_digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(P, "MAX_RANGE_LINES", 2)
    monkeypatch.setattr(P, "MAX_HASH_BYTES", 32)

    def forbid_whole_read(self):
        raise AssertionError("range reads must not materialize the whole file")

    monkeypatch.setattr(Path, "read_bytes", forbid_whole_read)
    result = service.read_file(project, target.name, 2, 7)
    assert result["text"] == "中文\n中文" and result["end_line"] == 3
    assert result["total_lines"] is None and result["sha1"] == ""
    assert result["encoding"] == "unknown" and result["editable"] is False
    assert "未确认全文件" in result["encoding_warning"]
    with target.open("rb") as stream:
        assert hashlib.sha256(stream.read()).hexdigest() == original_digest


def test_binary_guard_still_rejects_nul(reader):
    with pytest.raises(P.P.ProjectPathError, match="二进制"):
        _read(reader, b"legacy\x00bytes", name="fixture.log")


def test_full_utf8_decoding_does_not_treat_an_ambiguous_line_as_gb18030():
    source = "正常中文，必须保留 UTF-8 优先。\n"
    assert decode_text(source.encode("utf-8"), is_log=True).text == source
