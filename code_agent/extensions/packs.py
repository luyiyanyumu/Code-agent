"""Skills 系统（s16）：技能先列目录，用到再展开。

机制与 ToolSearch 同源（延迟加载思想）：
- 技能 = 目录/SKILL.md（frontmatter: name/description + 正文指令）
- system prompt 只注入"目录"（name + 一句话描述，几十 token）
- 模型需要时调 load_skill(name)，正文才进上下文

M10：支持**多个来源目录**。插件包内嵌的 skills/ 作为额外来源并入同一份目录，
所以 load_skill 对插件技能一视同仁；每个技能记录 origin（local / plugin:xxx），
供「工具入口」页面展示来源。同名技能本地优先，插件不能覆盖平台自带技能。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_agent.tools.base import Tool, ToolContext


@dataclass(frozen=True)
class PackSource:
    """一个技能/专家来源目录。allow 为空表示该来源下的全部都启用。"""

    root: Path
    origin: str = "local"
    allow: tuple[str, ...] = field(default_factory=tuple)


def _parse_doc(path: Path) -> tuple[dict[str, str], str]:
    """解析 SKILL.md/EXPERT.md：--- frontmatter（key: value）+ 正文。"""
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip()
        body = m.group(2)
    return meta, body.strip()


def fallback_description(body: str, limit: int = 200) -> str:
    """frontmatter 没写 description 时，从正文里找一句能当描述的。

    社区技能（SkillHub 上很多）**只有正文、没有 frontmatter**——真机上装完
    `weekly-report-gen` 后目录里显示成 `- weekly-report-gen: `（空的），
    模型于是完全不知道这技能是干什么的，等于白装。这里按"最可能那句"兜底：
    先找 `## 描述` / `## Description` 小节，再退到第一行去掉 `#` 的标题。
    """
    lines = [l.strip() for l in (body or "").splitlines()]
    for i, line in enumerate(lines):
        low = line.lower().lstrip("# ").strip()
        if low in ("描述", "说明", "description", "summary", "简介"):
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith("#"):
                    return nxt[:limit]
    for line in lines:
        text = line.lstrip("# ").strip()
        if text and not text.startswith(("---", "```", "|")):
            return text[:limit]
    return ""


def iter_pack_docs(root: Path, doc_name: str = "SKILL.md"):
    """扫出一个来源目录里所有技能/专家文档，**支持两种布局**：

    - `skills/<slug>/SKILL.md`（平台自建 / 页面上传的，一直如此）
    - `skills/<命名空间>/<slug>/SKILL.md`（SkillHub 装的社区技能：`@clawhub_xxx/skill/`）

    只认"这一层有 SKILL.md 就是技能，否则当命名空间目录、再下一层"：
    这样不会把技能内部的资源子目录误当成技能，也不需要递归整个目录树。
    **点开头的目录一律跳过**（`.trash` 是删除回收站——上次删掉的技能不该又冒出来）。
    """
    if not root.exists():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        direct = child / doc_name
        if direct.is_file():
            yield direct
            continue
        for nested in sorted(child.glob(f"*/{doc_name}")):
            if nested.is_file():
                yield nested


class MarkdownPack:
    """SKILL.md / EXPERT.md 共用的目录+懒加载机制。"""

    def __init__(
        self,
        root: Path,
        doc_name: str,
        kind: str,
        sources: list[PackSource] | None = None,
    ):
        self.root = Path(root)
        self.doc_name = doc_name
        self.kind = kind
        # 主目录永远排第一：同名技能本地优先，插件覆盖不了平台自带技能
        self._sources: list[PackSource] = [PackSource(self.root, "local")] + list(sources or [])
        self._packs: dict[str, tuple[dict[str, str], str]] = {}
        self._origins: dict[str, str] = {}
        self._dirs: dict[str, Path] = {}
        #: 被用户停用的 pack 名（M22）：不进目录、不进 catalog_text、load 不到
        self._disabled: set[str] = set()
        self._scan()

    def set_disabled(self, names: list[str] | set[str] | None) -> None:
        """设置"停用清单"（停用的技能/专家：模型看不到、也 load 不到）。

        与工具一键禁用同一语义：**看得见 = 用得上**。但**列表里仍然列出来**（带 enabled=false），
        否则页面上点一下它就从列表消失、再也没法启用回来。
        """
        self._disabled = {str(n).strip() for n in (names or []) if str(n).strip()}
        self._scan()

    def is_disabled(self, name: str) -> bool:
        """停用判断同时认 frontmatter 的 name 与目录名（两者常不一样）。"""
        if name in self._disabled:
            return True
        path = self._dirs.get(name)
        return bool(path and path.name in self._disabled)

    def _scan(self) -> None:
        self._packs.clear()
        self._origins.clear()
        self._dirs.clear()
        for src in self._sources:
            if not src.root.exists():
                continue
            for doc in iter_pack_docs(src.root, self.doc_name):
                meta, body = _parse_doc(doc)
                if not meta.get("description"):
                    # 没有 frontmatter 描述的社区技能：从正文兜一句，否则目录里是空白
                    fb = fallback_description(body)
                    if fb:
                        meta = {**meta, "description": fb}
                name = meta.get("name") or doc.parent.name
                if src.allow and name not in src.allow:
                    continue
                if name in self._packs:
                    continue
                self._packs[name] = (meta, body)
                self._origins[name] = src.origin
                self._dirs[name] = doc.parent

    def path_of(self, name: str) -> Path | None:
        """这个技能/专家的目录（页面上"删除"要移走的就是它）。"""
        return self._dirs.get(name)

    def names(self) -> list[str]:
        """**可用**的名字（停用的不算）——system prompt 目录与 load 都按这个口径。"""
        return [n for n in self._packs if not self.is_disabled(n)]

    def set_sources(self, sources: list[PackSource] | None = None) -> None:
        """替换额外来源目录并重扫（M10：插件启停后热更新）。

        复用同一实例是有意的——app.state.skills 等引用要保持有效，
        不能换对象，否则外部持有的引用会指向旧目录。
        """
        self._sources = [PackSource(self.root, "local")] + list(sources or [])
        self._scan()

    def rescan(self) -> None:
        """来源不变，只重扫磁盘（M11：页面上传技能后调用）。"""
        self._scan()

    def list(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        """清单（默认**含**停用的，带 enabled 标记）：页面要能看到停用的东西并再启用回来。

        system prompt 的目录走 `catalog_text()`，那里只取启用的。
        """
        out: list[dict[str, Any]] = []
        for name, (meta, _) in self._packs.items():
            enabled = not self.is_disabled(name)
            if not enabled and not include_disabled:
                continue
            out.append(
                {
                    "name": name,
                    "description": meta.get("description", ""),
                    "origin": self._origins.get(name, "local"),
                    "enabled": enabled,
                }
            )
        return out

    def origin(self, name: str) -> str:
        return self._origins.get(name, "")

    def load(self, name: str) -> str | None:
        """取正文。**停用的读不到**（与"模型看不到"保持一致，fail closed）。"""
        if self.is_disabled(name):
            return None
        pack = self._packs.get(name)
        return pack[1] if pack else None

    def resource_dir(self, name: str) -> Path | None:
        """技能目录路径（M12：附带脚本 / 参考资料 / 资源文件挂在这里）。"""
        return self._dirs.get(name)

    def resource_files(self, name: str) -> list[str]:
        """技能目录下除 doc_name 外的**可读资源**相对路径列表（排序）。

        跳过 `__pycache__` 与点开头的文件/目录：这份清单是给模型"按需 read_file"
        用的，把 `write_xlsx.cpython-313.pyc` 或 `.DS_Store` 列进去，等于引导它
        去读一个二进制垃圾文件。

        **为什么现在才修**：以前技能里基本只有 SKILL.md，附带资源是 M12 才有的能力；
        随包自带的 office 技能第一次真的带 `scripts/`，一跑就发现清单里多了个 `.pyc`
        （脚本被编译过一次就会长出来，用户自己的技能一样会踩到）。
        """
        d = self._dirs.get(name)
        if d is None or not d.is_dir():
            return []
        out: list[str] = []
        for path in d.rglob("*"):
            if not path.is_file() or path.name == self.doc_name:
                continue
            parts = path.relative_to(d).parts
            if any(part.startswith(".") or part == "__pycache__" for part in parts):
                continue
            out.append(str(path.relative_to(d)).replace("\\", "/"))
        return sorted(out)

    def catalog_text(self) -> str:
        """注入 system prompt 的目录（轻量，只占几十 token）。**停用的不进目录。**"""
        items = [i for i in self.list() if i["enabled"]]
        if not items:
            return ""
        lines = "\n".join(f"- {i['name']}: {i['description']}" for i in items)
        return f"可用{self.kind}（用 load_{self.kind} 工具加载完整内容）：\n{lines}"


class LoadPackTool(Tool):
    def __init__(self, pack: MarkdownPack):
        self._pack = pack
        self.name = f"load_{pack.kind}"
        kind_zh = {"skill": "技能", "expert": "专家"}.get(pack.kind, pack.kind)
        self.description = f"加载一个{kind_zh}的完整指令内容（先看 system prompt 里的目录，再按名字加载）"
        self.description_zh = self.description
        self.parameters = {
            "type": "object",
            "properties": {"name": {"type": "string", "description": f"{kind_zh}名称"}},
            "required": ["name"],
        }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        name = (arguments.get("name") or "").strip()
        body = self._pack.load(name)
        if body is None:
            available = ", ".join(self._pack.names()) or "(无)"
            return f"[load_{self._pack.kind}] 未找到 {name!r}。可用: {available}"
        # M12：技能可附带脚本 / 参考资料 / 资源文件——列出清单，模型按需 read_file
        resources = self._pack.resource_files(name)
        if resources:
            d = self._pack.resource_dir(name)
            lines = "\n".join(f"- {r}（绝对路径：{d / r}）" for r in resources)
            body = f"{body}\n\n[附带资源]（按需用 read_file 读取绝对路径）\n{lines}"
        return body


class SkillsRegistry(MarkdownPack):
    def __init__(self, root: Path, sources: list[PackSource] | None = None):
        super().__init__(root, "SKILL.md", "skill", sources)


class ExpertsRegistry(MarkdownPack):
    def __init__(self, root: Path, sources: list[PackSource] | None = None):
        super().__init__(root, "EXPERT.md", "expert", sources)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
