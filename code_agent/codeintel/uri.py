"""文件 URI（M18 · P3）：LSP 按 URI 匹配文档，编码差异会直接表现为"文档找不到"。

规则只有一条：**由服务端生成 URI 并下发给前端，前端原样回传**。
这样客户端与语言服务器两侧看到的字符串完全一致，不受平台、空格、中文目录影响。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


def file_uri(path: Path | str) -> str:
    """绝对路径 → file:// URI（保留 `/` 与盘符冒号，其余百分号编码）。"""
    resolved = Path(path).resolve()
    text = str(resolved).replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text          # Windows 盘符：E:/x → /E:/x，拼出 file:///E:/x
    return "file://" + quote(text, safe="/:")


def uri_to_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    raw = unquote(parsed.path)
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]              # /E:/x → E:/x
    return Path(raw)


def normalize_uri(uri: str) -> str:
    """把 URI 归一成同一形态，用于**跨端匹配**。

    为什么必须归一：语言服务器回传的 URI 往往与我们发出去的不完全一致——
    真机上 pyright 回的是 `file:///e%3A/Path/...`（小写盘符 + 编码冒号），
    而我们发的是 `file:///E:/Path/...`。字符串直接比较必然不相等，
    结果是诊断既挂不到文档上、也过不了路径校验，被静默丢掉。

    归一 = 解析成路径 → `resolve()`（Windows 上会还原真实大小写）→ 重新生成 URI。
    """
    path = uri_to_path(uri)
    if path is None:
        return uri
    try:
        return file_uri(path)
    except OSError:
        return uri


def uri_to_rel(uri: str, root: Path | str) -> str | None:
    """URI 在项目内的相对路径；越界或非 file 协议返回 None。

    大小写与分隔符按平台归一比较（Windows 下 `e:/x` 与 `E:/x` 是同一个文件，
    但 `Path.relative_to` 是大小写敏感的）。
    """
    path = uri_to_path(uri)
    if path is None:
        return None
    try:
        target = path.resolve()
    except OSError:
        return None
    base = Path(root).resolve()
    try:
        return str(target.relative_to(base)).replace("\\", "/")
    except ValueError:
        pass
    # 退化路径：按 normcase 前缀比较（Windows 上 `e:/x` 与 `E:/x` 是同一文件，
    # 但 relative_to 大小写敏感；normcase 只做大小写/分隔符归一，长度不变）
    base_s = os.path.normcase(str(base)).rstrip("\\/")
    target_s = os.path.normcase(str(target))
    for sep in (os.sep, "/"):
        if target_s.startswith(base_s + sep):
            return str(target)[len(base_s) + 1 :].replace("\\", "/")
    return None


def same_file(uri_a: str, uri_b: str) -> bool:
    return normalize_uri(uri_a) == normalize_uri(uri_b)
