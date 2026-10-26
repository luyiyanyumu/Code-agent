"""原子写文件（对齐 DSH 的 `dsh-atomic-write`）。

`path.write_text(...)` 是"先截断、再写"：进程在中间没了（超时被杀、断电、用户点停止）
就留下半个文件——而工具刚刚才把原文读进来，结果是**把一个好文件改坏**，
比"这次没写成"严重得多。

这里改成同目录临时文件 + `os.replace`：读者要么看到旧全文，要么看到新全文。
Windows 上 `os.replace` 走 `MoveFileEx(REPLACE_EXISTING)`，同卷替换是原子的；
临时文件因此必须建在**目标同目录**（换卷就不是原子的了）。

两个入口对应 DSH 的两种写入意图：

    atomic_replace(path, text)   覆盖（replaceIfVersion / 无条件覆盖都走它）
    atomic_create(path, text)    只在**不存在**时创建（O_EXCL）——即 DSH 的
                                 `createIfAbsent`，并发创建者不会被静默覆盖

编码/换行与原来的 `Path.write_text(encoding="utf-8")` 保持一致（文本模式、默认
换行转换）：这次改动只解决"原子性"，不顺手改文件字节，免得把 diff 搅浑。
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path


def atomic_replace(path: Path | str, text: str) -> None:
    """把 text 原子地写成 path 的内容（覆盖已存在的文件）。

    保留目标原有的权限位：`mkstemp` 建出来的是 0600，如果直接 replace，
    一个可执行脚本会在改写后丢掉 +x——那是"顺手改坏了别的东西"。
    """
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    mode: int | None = None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        mode = None  # 目标还不存在：用 mkstemp 的默认权限

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())          # 先确保内容落盘，再替换目录项
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        # 失败时绝不留下临时文件（它会出现在目录列表里，看起来像个真文件）
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_create(path: Path | str, text: str) -> bool:
    """只在目标**不存在**时创建它。

    返回 True = 创建成功；False = 已经存在（并发创建者赢了这一局）。

    为什么不用 `exists()` 再 `write`：那是典型的 TOCTOU——检查与写入之间别人
    可能刚好创建了同名文件，于是我们把它覆盖掉。`O_EXCL` 把"检查 + 创建"合成
    一个原子操作，由内核判定谁赢。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    return True
