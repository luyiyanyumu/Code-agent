"""文件观察策略：读过才能改，且读完之后没变过才准改。

对齐 DSH 的 `dsh-fs-observation-policy`。没有这条纪律时，模型可以在第 3 轮读到
文件内容，在第 10 轮把一个已经被**人**（或另一个会话 / 另一次运行的 AI）改过的
文件整篇覆盖回去——而且它真诚地以为自己知道文件里有什么。这不是理论风险：
code-agent 的 patch 台账"自动回滚"早就有一条同源的硬规则（只有当前内容仍等于该次
改动之后的内容才允许回滚，见 `codeintel/service.py`），只是那条只护回滚，
不护**写入**。

三种状态（与 DSH 同构）：

    未见 unseen     没读过 → 不许覆盖、不许编辑
    确认存在 present 读过并存下版本 → 改之前必须仍然是那个版本
    确认缺失 absent  读过、当时不存在 → 允许 write 创建它（`createIfAbsent`），
                     但不允许 edit；若此后文件出现了，说明有并发创建者，拒绝覆盖

版本记多细：**能记多细就记多细**。

- 全文件读（`read_file` 同时省略 offset/limit）→ 存 `(size, mtime_ns, sha1(全文))`，最强；
- 窗口读（给了 offset/limit）→ 只存 `(size, mtime_ns)`。窗口读没有全文可摘要，
  而为了一次校验把整文件再读一遍不划算；DSH 也明确接受这个强度
  （"授权依据是版本新鲜度，而非视图完整性"，任何窗口读都会授权对**未变**文件
  执行全文件覆盖）。

两个刻意的设计取舍：

1. **记录是会话级的、不持久化**。DSH 同样如此（dispose 即丢、绝不跨会话）。
   会话恢复（进程重启 / 重新打开）之后必须重新读一遍——这是**特性**不是缺陷：
   恢复后的"我以为我记得"恰恰是最危险的假设。
2. **`None` = 不启用**。策略在 DSH 里也是可插拔的，省略它写入仍然原子，只是不
   要求先读。真实链路一定传（`AgentLoop.run` 是唯一构造 `ToolContext` 的地方），
   只有程序化直调（测试、脚本）会拿到 None——那种场景没有"模型基于陈旧内容
   覆盖"的风险。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

#: 错误文案里必须出现"硬事实"（读没读过 / 变没变），而且必须给**恢复动作**。
#: 前两句照抄 DSH 的措辞——那是模型已经被训练过的句子，换"更好的写法"只会削弱
#: 识别率；后面括号里的中文只补"为什么"，不改变句子结构。
_NOT_READ = (
    'cannot modify "{path}": file has not been read — read the file, then retry'
    "（本会话还没读过这个文件，直接覆盖等于闭着眼睛改；先用 read_file 读它）"
)
_STALE = (
    'cannot modify "{path}": the file changed since it was read — re-read the file, then retry'
    "（读完之后文件被改过——可能是你、是人、也可能是另一个会话；按当前内容重读一遍再决定怎么改）"
)
_DELETED = (
    'cannot modify "{path}": the file was deleted since it was read — re-read it, then retry'
    "（你读到它时它还在，现在没了：重新读一遍，或用 write_file 把它建回来）"
)
_APPEARED = (
    'cannot modify "{path}": the file now exists but was observed as missing — read it, then retry'
    "（你读到「它不存在」，现在它出现了——有人刚创建了它，先读一遍再决定要不要动）"
)
_ABSENT_EDIT = (
    'cannot edit "{path}": the file was observed as missing but now exists — read it, then retry'
    "（你读到「它不存在」，现在它出现了——有人刚创建了它，先读一遍再决定要不要动）"
)


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


@dataclass(frozen=True)
class _Version:
    """读到过的版本。`digest` 为 None = 只记了 stat 指纹（窗口读）。"""

    size: int
    mtime_ns: int
    digest: str | None = None


class FsObservation:
    """一个会话的"读过什么版本"记录。按绝对路径（normcase）记账。"""

    def __init__(self) -> None:
        #: 绝对路径 → 读到时的版本（present）
        self._present: dict[str, _Version] = {}
        #: 读到过、当时不存在的路径（absent）
        self._absent: set[str] = set()

    # -- 记账 ---------------------------------------------------------------
    @staticmethod
    def _key(path: Path | str) -> str:
        try:
            # normcase 让 Windows 上 A.py / a.py 归一到同一个键：
            # 两条路径指向同一个文件，分开记账等于给绕过留口子。
            return os.path.normcase(str(Path(path).resolve()))
        except OSError:
            return str(path)

    def record_read(
        self, path: Path | str, *, raw: bytes | None = None, cheap: bool = False
    ) -> None:
        """记下读到的版本；路径不存在则记为"确认缺失"。

        **摘要一律按磁盘上的原始字节算**，不按解码后的文本——这条不是洁癖，
        是踩出来的：Windows 上 `write_text("a\\n")` 落盘是 CRLF，而 `read_text()`
        用通用换行又把 CRLF 读回 LF。如果一边按原始字节、一边按读回的文本算摘要，
        同一个没动过的文件会永远判成"读完之后被改过"，门禁直接把正常写入全拒了。

        参数：
          raw   调用方已经拿到的原始字节（如 read_file 的全文件读）——省一次重复读；
          cheap True = 只记 stat 指纹（窗口读没有全文，也不值得为校验再读一遍）。
        """
        target = Path(path)
        key = self._key(target)
        try:
            st = target.stat()
        except OSError:
            self._present.pop(key, None)
            self._absent.add(key)
            return

        digest: str | None = None
        if not cheap:
            data = raw
            if data is None:
                try:
                    data = target.read_bytes()
                except OSError:
                    data = None
            if data is not None:
                digest = _digest_bytes(data)

        self._absent.discard(key)
        self._present[key] = _Version(
            size=st.st_size, mtime_ns=st.st_mtime_ns, digest=digest
        )

    def record_absent(self, path: Path | str) -> None:
        """记下"读过、当时不存在"。"""
        key = self._key(path)
        self._present.pop(key, None)
        self._absent.add(key)

    def state(self, path: Path | str) -> str:
        """unseen / present / absent。"""
        key = self._key(path)
        if key in self._present:
            return "present"
        if key in self._absent:
            return "absent"
        return "unseen"

    def forget(self, path: Path | str) -> None:
        """丢掉某个路径的记录（回滚等把文件改回去的场景）。"""
        key = self._key(path)
        self._present.pop(key, None)
        self._absent.discard(key)

    # -- 门禁 ---------------------------------------------------------------
    def check_write(self, path: Path | str) -> str | None:
        """write 之前的检查。返回 None = 放行，否则返回给模型的错误说明。"""
        target = Path(path)
        exists = target.exists()
        state = self.state(target)

        if exists:
            if state == "present":
                return None if self._unchanged(target) else _STALE.format(path=path)
            if state == "absent":
                return _APPEARED.format(path=path)
            # unseen：没读过就要覆盖一个已存在的文件 → 拒
            # （DSH：未见 + 已存在 → 不许覆盖；未见 + 缺失 → createIfAbsent）
            return _NOT_READ.format(path=path)

        # 目标不存在
        if state == "present":
            return _DELETED.format(path=path)
        return None  # unseen / absent → 允许创建

    def check_edit(self, path: Path | str) -> str | None:
        """edit 之前的检查：必须先读过，且读完之后没变过。"""
        target = Path(path)
        state = self.state(target)
        if state == "unseen":
            return _NOT_READ.format(path=path)
        if state == "absent":
            return _ABSENT_EDIT.format(path=path)
        if not target.is_file():
            return _DELETED.format(path=path)
        return None if self._unchanged(target) else _STALE.format(path=path)

    def _unchanged(self, path: Path) -> bool:
        """当前文件是否仍是读到过的那个版本（按**原始字节**比，见 record_read 的说明）。"""
        expected = self._present.get(self._key(path))
        if expected is None:
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        if st.st_size != expected.size or st.st_mtime_ns != expected.mtime_ns:
            return False
        if expected.digest is None:
            return True  # 窗口读：只有 stat 指纹可比（DSH 接受这一强度）
        try:
            return _digest_bytes(path.read_bytes()) == expected.digest
        except OSError:
            return False
