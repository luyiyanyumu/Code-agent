"""分块元数据与过滤条件（M34）：**先过滤，再排序**。

一条可检索的记录 = 文本 + 元数据。元数据不是装饰，它承担三件事：

1. **精度**：`origin` 区分正式区/隔离区（模型沉淀的草稿不该跟人工权威知识同权）；
   `path`/`symbol_id`/`contract_id` 让"只在这个文件/这个符号/这条契约范围内找"成为
   一次**精确过滤**，而不是靠向量碰运气。
2. **失效**：`hash` 是内容哈希。向量缓存按它存——内容改了就换 key，旧向量自动不再被用；
   不需要"记得去删缓存"这种纪律。
3. **解释**：检索结果要能说清"为什么它被召回"（字面命中？语义相近？图上有边？），
   否则模型自己也判断不了该不该采信。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

#: 区域：人工权威 / 模型沉淀 / 用户记忆 / 工作区记忆 / 代码
ORIGINS = ("human", "llm", "user", "workspace", "code")


def text_hash(text: str) -> str:
    """内容哈希（向量缓存的键）。与 codeintel.hashing.sha1_text 同一算法，避免两套口径。"""
    return hashlib.sha1(str(text or "").encode("utf-8", errors="replace")).hexdigest()


@dataclass
class Chunk:
    """一条可检索记录：文本 + 元数据。"""

    ref: str                                # 全局唯一引用：kb:human:doc#3 / mem:user / code:sym:...
    text: str
    kind: str = "kb"                        # kb | memory | code
    title: str = ""
    origin: str = "human"                   # 见 ORIGINS
    path: str = ""
    symbol_id: str = ""
    contract_id: str = ""
    tags: tuple[str, ...] = ()
    ts: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def hash(self) -> str:
        return text_hash(self.text)

    def meta(self) -> dict[str, Any]:
        """元数据视图（检索结果里回给调用方的那份）。

        `extra` 一并带上：知识库要靠它回传 `doc_id`/块序号（界面与"采纳"流程需要），
        而 SQL 路径下结果是从库里重建的——不带 extra 就会丢字段。
        """
        return {
            "ref": self.ref,
            "kind": self.kind,
            "title": self.title,
            "origin": self.origin,
            "path": self.path,
            "symbol_id": self.symbol_id,
            "contract_id": self.contract_id,
            "tags": list(self.tags),
            "ts": self.ts,
            "hash": self.hash,
            "extra": dict(self.extra or {}),
        }


@dataclass
class Filters:
    """结构化过滤条件。全部给定时是"与"关系；未给定的维度不过滤。

    为什么要有它：向量检索无法表达"只要正式区的、且属于这个符号的"——那是**集合运算**，
    用元数据做是精确的、可解释的、也是免费的（不用过一遍模型）。
    """

    kinds: tuple[str, ...] = ()
    origins: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    path_prefix: str = ""
    path_exact: str = ""
    symbol_id: str = ""
    contract_id: str = ""
    since: float = 0.0

    @property
    def empty(self) -> bool:
        return not any(
            [
                self.kinds,
                self.origins,
                self.tags,
                self.path_prefix,
                self.path_exact,
                self.symbol_id,
                self.contract_id,
                self.since,
            ]
        )

    def matches(self, chunk: Chunk) -> bool:
        if self.kinds and chunk.kind not in self.kinds:
            return False
        if self.origins and chunk.origin not in self.origins:
            return False
        if self.tags and not set(self.tags) & set(chunk.tags):
            return False                       # 标签是"或"：命中任一即可
        if self.path_exact and chunk.path.replace("\\", "/") != self.path_exact.replace("\\", "/"):
            return False
        if self.path_prefix:
            prefix = self.path_prefix.replace("\\", "/").rstrip("/") + "/"
            path = chunk.path.replace("\\", "/")
            if not (path == prefix[:-1] or path.startswith(prefix)):
                return False
        if self.symbol_id and chunk.symbol_id != self.symbol_id:
            return False
        if self.contract_id and chunk.contract_id != self.contract_id:
            return False
        if self.since and chunk.ts and chunk.ts < self.since:
            return False
        return True

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Filters":
        """从接口/工具参数构造（都是可选字段，缺省即不过滤）。"""
        raw = raw or {}

        def _tuple(key: str) -> tuple[str, ...]:
            value = raw.get(key)
            if value is None:
                return ()
            if isinstance(value, str):
                return tuple(v.strip() for v in value.split(",") if v.strip())
            if isinstance(value, (list, tuple)):
                return tuple(str(v).strip() for v in value if str(v).strip())
            return ()

        return cls(
            kinds=_tuple("kinds"),
            origins=_tuple("origins"),
            tags=_tuple("tags"),
            path_prefix=str(raw.get("path_prefix") or "").strip(),
            path_exact=str(raw.get("path") or "").strip(),
            symbol_id=str(raw.get("symbol_id") or "").strip(),
            contract_id=str(raw.get("contract_id") or "").strip(),
            since=float(raw.get("since") or 0),
        )


def apply_filters(chunks: list[Chunk], filters: Filters | None) -> list[Chunk]:
    """先裁剪候选集。返回新列表，不改原列表顺序。"""
    if filters is None or filters.empty:
        return list(chunks)
    return [c for c in chunks if filters.matches(c)]
