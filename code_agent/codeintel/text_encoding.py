"""Read legacy text without converting or overwriting its original bytes."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator


_LINE_END = re.compile(rb"\r\n|\n|\r")


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    warning: str = ""

    def metadata(self) -> dict:
        warnings = {
            "gb18030": "已按 GB18030 解码并只读显示，原文件未修改。",
            "utf-8+gb18030": "检测到 UTF-8 与 GB18030 混合编码，已逐行解码并只读显示，原文件未修改。",
            "unknown": "无法可靠识别文件编码，已只读显示，部分字符可能无法恢复。",
        }
        return {
            "encoding": self.encoding,
            "readonly_reason": "" if self.encoding == "utf-8" else "encoding",
            "encoding_warning": self.warning or warnings.get(self.encoding, ""),
        }


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _binary_lines(stream: BinaryIO) -> Iterator[bytes]:
    """Universal newlines without splitting UTF-8/GB18030 characters at IO chunks."""
    for chunk in stream:
        start = 0
        for match in _LINE_END.finditer(chunk):
            yield chunk[start:match.end()]
            start = match.end()
        if start < len(chunk):
            yield chunk[start:]


class _LogDecoder:
    def __init__(self) -> None:
        self.utf8 = False
        self.legacy = False
        self.unknown = False

    def decode(self, data: bytes) -> str:
        try:
            text = data.decode("utf-8")
            self.utf8 |= any(byte >= 128 for byte in data)
            return text
        except UnicodeDecodeError:
            self.legacy = True
            try:
                return data.decode("gb18030")
            except UnicodeDecodeError:
                self.unknown = True
                return data.decode("utf-8", errors="replace")

    @property
    def encoding(self) -> str:
        if self.unknown:
            return "unknown"
        if self.legacy:
            return "utf-8+gb18030" if self.utf8 else "gb18030"
        return "utf-8"


def decode_text(data: bytes, *, is_log: bool = False) -> DecodedText:
    """UTF-8 always wins; mixed logs fall back independently at each line."""
    try:
        return DecodedText(_normalize_newlines(data.decode("utf-8")), "utf-8")
    except UnicodeDecodeError:
        pass
    if is_log:
        import io

        decoder = _LogDecoder()
        text = "".join(decoder.decode(line) for line in _binary_lines(io.BytesIO(data)))
        return DecodedText(_normalize_newlines(text), decoder.encoding)
    try:
        return DecodedText(_normalize_newlines(data.decode("gb18030")), "gb18030")
    except UnicodeDecodeError:
        return DecodedText(_normalize_newlines(data.decode("utf-8", errors="replace")), "unknown")


def _probe_encoding(path: Path, *, limit: int) -> str:
    """Incremental validation keeps multibyte characters across read boundaries."""
    decoders = {name: codecs.getincrementaldecoder(name)(errors="strict") for name in ("utf-8", "gb18030")}
    with path.open("rb") as stream:
        remaining = limit
        while remaining > 0:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            for name, decoder in list(decoders.items()):
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    del decoders[name]
        if stream.read(1):
            return "unknown"
    for name, decoder in list(decoders.items()):
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            del decoders[name]
    return next(iter(decoders), "unknown")


def read_text_range(path: Path, first: int, last: int, *, inspection_limit: int) -> tuple[DecodedText, int]:
    """Keep only requested lines; inspect bounded files for reliable encoding metadata."""
    complete = path.stat().st_size <= inspection_limit
    encoding = _probe_encoding(path, limit=inspection_limit) if complete else "unknown"
    log_decoder = _LogDecoder() if path.suffix.lower() == ".log" and encoding != "utf-8" else None
    lines: list[str] = []
    if log_decoder is not None or not complete:
        decoder = log_decoder or _LogDecoder()
        with path.open("rb") as stream:
            for idx, raw in enumerate(_binary_lines(stream), start=1):
                # Small logs must inspect later lines too: an ASCII range can
                # belong to a mixed file and must never become editable.
                if idx > last and not complete:
                    break
                text = decoder.decode(raw)
                if first <= idx <= last:
                    lines.append(text.rstrip("\n").rstrip("\r"))
        encoding = decoder.encoding if complete else "unknown"
    else:
        codec = encoding if encoding != "unknown" else "utf-8"
        with path.open("r", encoding=codec, errors="replace") as stream:
            for idx, line in enumerate(stream, start=1):
                if idx > last:
                    break
                if idx >= first:
                    lines.append(line.rstrip("\n").rstrip("\r"))
    warning = "" if complete else "仅检查了当前读取范围，未确认全文件编码；已只读显示，原文件未修改。"
    return DecodedText("\n".join(lines), encoding, warning), len(lines)
