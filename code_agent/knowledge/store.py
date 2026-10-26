"""知识库文档存储（M12/M14）：正式区 + LLM 隔离区双区。

一篇文档 = 一个 markdown 文件，frontmatter 存 title / tags / source，正文是知识内容。

双区设计（LLM wiki）：
- 正式区  knowledge/<doc_id>.md       人工维护的权威知识（origin=human）
- 隔离区  knowledge/_llm/<doc_id>.md  模型沉淀 / LLM 编译的内容（origin=llm）

模型可以自由往隔离区写（不进审批），但**不会污染正式库**——管理员在页面上
审核后「采纳」才把条目移入正式区。`_llm` 以 `_` 开头，正式区的 `glob("*.md")`
扫不到它，天然分区。
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHUNK_SIZE = 900  # 每块字符上限（约 300-500 token）

#: 内容来源分区
AREA_HUMAN = "human"
AREA_LLM = "llm"
_LLM_SUBDIR = "_llm"

_FRONTMATTER_RE = re.compile(r"\s*---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _parse_doc(text: str) -> tuple[dict[str, str], str]:
    """解析 frontmatter（key: value）+ 正文。无 frontmatter 则全文当正文。"""
    meta: dict[str, str] = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip()
        body = m.group(2)
    return meta, body.strip()


def _split_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _render(title: str, content: str, tags: list[str], source: str = "") -> str:
    fm = f"---\ntitle: {title.strip()}\n"
    tags_line = ",".join(t.strip() for t in tags if t.strip())
    if tags_line:
        fm += f"tags: {tags_line}\n"
    if source:
        fm += f"source: {source.strip()}\n"
    fm += "---\n\n"
    return fm + content.strip() + "\n"


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.strip()).strip("-").lower()
    return s[:40] or "doc"


def chunk_text(content: str, size: int = CHUNK_SIZE) -> list[str]:
    """按空行分段落，贪婪合并到接近 size 的块；超长段落硬切。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if buf and len(buf) + len(p) + 2 > size:
            chunks.append(buf)
            buf = p
        elif len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(p), size):
                chunks.append(p[i : i + size])
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks or ([content.strip()] if content.strip() else [])


class KnowledgeStore:
    """知识文档的文件 CRUD。文件名 = doc_id.md，id 用 token_hex。

    双区：origin=human 落在正式区 root/；origin=llm 落在隔离区 root/_llm/。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 分区寻址
    # ------------------------------------------------------------------
    def area_root(self, origin: str = AREA_HUMAN) -> Path:
        if origin == AREA_LLM:
            d = self.root / _LLM_SUBDIR
            d.mkdir(parents=True, exist_ok=True)
            return d
        return self.root

    def _path(self, doc_id: str, origin: str = AREA_HUMAN) -> Path:
        return self.area_root(origin) / f"{doc_id}.md"

    def _read(self, doc_id: str, origin: str = AREA_HUMAN) -> dict[str, Any] | None:
        path = self._path(doc_id, origin)
        if not path.is_file():
            return None
        meta, body = _parse_doc(path.read_text(encoding="utf-8"))
        return {
            "id": doc_id,
            "title": meta.get("title") or _slug(path.stem),
            "content": body,
            "tags": _split_tags(meta.get("tags")),
            "origin": origin,
            "source": meta.get("source", ""),
            "updated_at": _mtime_iso(path),
        }

    def _list_area(self, origin: str) -> list[dict[str, Any]]:
        root = self.area_root(origin)
        if not root.is_dir():
            return []
        docs: list[dict[str, Any]] = []
        for p in sorted(root.glob("*.md")):
            meta, _ = _parse_doc(p.read_text(encoding="utf-8"))
            docs.append(
                {
                    "id": p.stem,
                    "title": meta.get("title") or _slug(p.stem),
                    "tags": _split_tags(meta.get("tags")),
                    "origin": origin,
                    "source": meta.get("source", ""),
                    "updated_at": _mtime_iso(p),
                }
            )
        return docs

    def list(self, origin: str | None = None) -> list[dict[str, Any]]:
        """列文档。origin=None 返回两区全部（各自带 origin 标注）。"""
        if origin in (AREA_HUMAN, AREA_LLM):
            docs = self._list_area(origin)
        else:
            docs = self._list_area(AREA_HUMAN) + self._list_area(AREA_LLM)
        docs.sort(key=lambda d: d["updated_at"], reverse=True)
        return docs

    def get(self, doc_id: str, origin: str | None = None) -> dict[str, Any] | None:
        if origin in (AREA_HUMAN, AREA_LLM):
            return self._read(doc_id, origin)
        # 未指定：先正式区，再隔离区
        return self._read(doc_id, AREA_HUMAN) or self._read(doc_id, AREA_LLM)

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def save(
        self,
        doc_id: str,
        title: str,
        content: str,
        tags: list[str],
        origin: str = AREA_HUMAN,
        source: str = "",
    ) -> dict[str, Any]:
        self._path(doc_id, origin).write_text(
            _render(title, content, tags, source), encoding="utf-8"
        )
        return self._read(doc_id, origin)  # type: ignore[return-value]

    def create(
        self,
        title: str,
        content: str,
        tags: list[str],
        origin: str = AREA_HUMAN,
        source: str = "",
    ) -> dict[str, Any]:
        doc_id = f"kb_{secrets.token_hex(6)}"
        return self.save(doc_id, title, content, tags, origin, source)

    def delete(self, doc_id: str, origin: str | None = None) -> bool:
        if origin in (AREA_HUMAN, AREA_LLM):
            targets = [self._path(doc_id, origin)]
        else:
            targets = [self._path(doc_id, AREA_HUMAN), self._path(doc_id, AREA_LLM)]
        removed = False
        for path in targets:
            if path.is_file():
                path.unlink()
                removed = True
        return removed

    def move(self, doc_id: str, from_origin: str, to_origin: str) -> dict[str, Any] | None:
        """把条目从一个区移到另一个区（采纳：隔离区 -> 正式区）。"""
        src = self._path(doc_id, from_origin)
        if not src.is_file():
            return None
        dst = self._path(doc_id, to_origin)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
        return self._read(doc_id, to_origin)

    # ------------------------------------------------------------------
    # 导入（人工来源，落正式区）
    # ------------------------------------------------------------------
    def import_file(self, filename: str, content: str) -> dict[str, Any]:
        """导入单个文件：frontmatter 有 title 则优先，否则用文件名做标题。"""
        meta, body = _parse_doc(content)
        title = meta.get("title") or Path(filename).stem
        tags = _split_tags(meta.get("tags"))
        return self.create(title, body or content.strip(), tags, AREA_HUMAN)

    def import_dir(self, directory: str) -> dict[str, Any]:
        """扫描目录导入所有 .md/.txt，返回 {imported, skipped, docs}。"""
        d = Path(directory)
        if not d.is_dir():
            raise ValueError(f"目录不存在: {directory}")
        imported: list[str] = []
        skipped: list[str] = []
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() not in (".md", ".markdown", ".txt"):
                continue
            try:
                doc = self.import_file(p.name, p.read_text(encoding="utf-8"))
                imported.append(doc["id"])
            except Exception:
                skipped.append(str(p))
        return {"imported": len(imported), "skipped": skipped, "docs": imported}
