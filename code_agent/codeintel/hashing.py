"""哈希口径（M18）：**一处定义，处处一致**。

踩过的坑：乐观锁的"期望 sha1"由 `read_file` 给出（按原始字节算），而保存时
用 `read_text()` 再哈希（Windows 下 CRLF 被归一化）——两者永远不相等，
表现为"每次保存都 409 冲突"。所以文件哈希只走本模块，并且明确区分两种口径：

- **文本哈希** `sha1_file_text`：按 UTF-8 解码后（`errors="replace"`）再哈希。
  用途：编辑器乐观锁、修改台账的改前/改后指纹。
  理由：编辑器就是按**文本**编辑的，换行符差异不该让锁误报冲突。
- **字节哈希** `sha1_file_bytes`：按原始字节哈希。
  用途：索引的增量指纹（它要能发现"换行符被改""编码被改"这类真实改动）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: 大文件只哈希前 4MB + 文件大小：既避免把 50MB 日志整读一遍，
#: 也保证"内容变了必然不等"（同尺寸+同前缀的碰撞对一把锁来说可以接受）
LARGE_FILE_LIMIT = 4 * 1024 * 1024


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def sha1_file_bytes(path: Path | str, *, limit: int = LARGE_FILE_LIMIT) -> str:
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha1()
    read = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk or read >= limit:
                break
            digest.update(chunk)
            read += len(chunk)
    if size > limit:
        digest.update(f"|size={size}".encode("utf-8"))
    return digest.hexdigest()


def sha1_file_text(path: Path | str) -> str:
    path = Path(path)
    size = path.stat().st_size
    if size <= LARGE_FILE_LIMIT:
        return sha1_text(path.read_text(encoding="utf-8", errors="replace"))
    text = path.read_text(encoding="utf-8", errors="replace")
    return sha1_text(text[: LARGE_FILE_LIMIT // 2] + f"|size={size}")
