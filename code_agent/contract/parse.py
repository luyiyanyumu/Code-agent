"""契约解析：把 `*.CONTRACT.md` 读成结构化契约。

格式来自阶段 0 实验里真正被用过的那两份（`E:\\csp-phase0\\exp-a|exp-b\\CONTRACT.md`），
只保留**够用**的部分，不做通用 markdown 解析：

```markdown
## 1. 形状（shape）
```python
def classify(prompt: str) -> Route: ...
```
## 2. 义务（obligations）
| id | 义务（必须成立的性质） | 证据 |
|---|---|---|
| OBL1 | 空输入返回默认路由，不抛异常 | test_classify_... |
## 3. 数据（设计者的决定）
## 4. 拓扑（topology）
```

三个刻意的取舍：

1. **义务表就是契约的本体**：`| OBLx | 义务 | 证据 |`，一行一条。解析失败的行**如实报出来**
   （`problems`），不静默丢弃——实验里"静默丢一条义务"正是最危险的那种错。
2. **形状段用代码块**：只从 `python` 代码块里抽 `def name(...)` / `class Name` / `NAME: T = ...`，
   目的不是拿到可执行的签名，而是拿到**契约声明了哪些符号**（与实现的形状快照比对）。
3. **数据段只登记"存在"**：数据（关键词表、路由表这类）属于设计者的决定，解析器不试图理解它，
   只记录"这一段非空"——因为它的作用是在检查报告里提醒"这些是人写的、别动"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 契约文件名约定：与被约束的实现同目录，`<实现文件名>.CONTRACT.md`
#: （例：`code_agent/agent/routing.CONTRACT.md` 约束 `routing.py`）
SUFFIX = ".CONTRACT.md"

#: 义务 id 的形状：OBL1 / OBL-0142 / OBL_7 —— 允许前缀式编号，取到就记
OBLIGATION_ID = re.compile(r"^\s*(OBL[-_]?\d+)\s*$", re.I)
#: 形状段里认的符号声明
SYMBOL_DECL = re.compile(
    r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(|^class\s+([A-Za-z_]\w*)\b|^([A-Z_][A-Z0-9_]*)\s*[:=]",
)
#: 证据列里的证据名：按非词字符切开后，取**含 test 的 token**
#: （认 `test_foo`、`tests/test_x.py::test_foo`；不认自然语言里的 "test"）
_EVIDENCE_TOKEN = re.compile(r"[A-Za-z_][\w]*")
_PY_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


@dataclass
class Obligation:
    """一条义务：id + 条文 + 证据。

    证据可以为空，但**必须是"明确声明的空"**：契约里写 `无独立证据` 之类才算数。
    "忘了写证据"与"声明了没有独立证据"是两回事——机器要能区分，否则闸门会逼人
    编一条假证据来凑过检查（阶段 0 实验里 10 条义务有 5 条没有证据名，
    全都由实现者自己标出来"这条我只能靠猜"，正说明这个区分必须存在）。
    """

    id: str
    text: str
    evidence: list[str] = field(default_factory=list)
    #: 契约**明确声明**这条义务没有独立证据（"由约束保证"/"未判定"/"—"）
    no_evidence_declared: bool = False

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence)

    @property
    def settled(self) -> bool:
        """这条义务是否"已了结"：有证据，或明确声明了没有。"""
        return self.has_evidence or self.no_evidence_declared

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "evidence": list(self.evidence),
            "has_evidence": self.has_evidence,
            "no_evidence_declared": self.no_evidence_declared,
            "settled": self.settled,
        }


@dataclass
class Contract:
    """一个模块的契约。"""

    module: str                    # 被约束的实现文件（相对路径）
    path: str                      # 契约文件本身（相对路径）
    symbols: list[str] = field(default_factory=list)      # 形状段声明的公开符号
    #: 形状段里的声明行（原样保留，交给 shape.shape_of_declarations 解析成形状快照）
    declarations: list[str] = field(default_factory=list)
    obligations: list[Obligation] = field(default_factory=list)
    data_blocks: int = 0           # 数据段的行数（>0 表示这份契约里有人决定的数据）
    topology: int = 0              # 拓扑段的行数
    problems: list[str] = field(default_factory=list)     # 解析期的如实记录

    @property
    def evidence_count(self) -> int:
        return sum(1 for o in self.obligations if o.has_evidence)

    @property
    def declared_no_evidence(self) -> list[str]:
        """明确声明"没有独立证据"的义务 id（它们是**已声明的缺口**，不是遗漏）。"""
        return [o.id for o in self.obligations if not o.has_evidence and o.no_evidence_declared]

    @property
    def silent_gaps(self) -> list[str]:
        """静默缺失证据的义务 id（既没证据、也没声明）——这一类才是要拦下来的。"""
        return [o.id for o in self.obligations if not o.settled]

    @property
    def coverage(self) -> float:
        if not self.obligations:
            return 0.0
        return round(self.evidence_count / len(self.obligations), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "path": self.path,
            "symbols": list(self.symbols),
            "declarations": list(self.declarations),
            "obligations": [o.to_dict() for o in self.obligations],
            "obligation_count": len(self.obligations),
            "evidence_count": self.evidence_count,
            "coverage": self.coverage,
            "declared_no_evidence": self.declared_no_evidence,
            "silent_gaps": self.silent_gaps,
            "data_blocks": self.data_blocks,
            "topology": self.topology,
            "problems": list(self.problems),
        }


def _sections(text: str) -> dict[str, str]:
    """按 `## ...` 标题切段，返回 {标题原文: 正文}。标题里的编号与括号都保留。"""
    out: dict[str, str] = {}
    current = ""
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                out[current] = "\n".join(buf)
            current = line[3:].strip()
            buf = []
        elif current:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf)
    return out


def _section(sections: dict[str, str], *keywords: str) -> str:
    """按关键词找段（标题里有中文也有英文，两种都认）。"""
    for title, body in sections.items():
        low = title.lower()
        if any(k.lower() in low for k in keywords):
            return body
    return ""


def _parse_shape(body: str) -> tuple[list[str], list[str], list[str]]:
    """从形状段抽符号声明。

    优先把**整个 python 代码块当成一个模块解析**（实验契约的形状段本来就是合法 Python：
    `def f(...) -> T: ...`、带字段的 `class X:`、`NAME: T = ...`），
    这样 `class` 的字段、多行注解都能拿到；解析不了才退化成逐行。

    为什么要有个"退化成逐行"：契约是人手写的 markdown，形状段可能夹着自然语言的占位
    （`def f(...): ...  # 见下文`），整块解析会失败——那时宁可少拿一点，也不要报"没有符号"。
    """
    symbols: list[str] = []
    declarations: list[str] = []
    problems: list[str] = []

    blocks = _PY_BLOCK.findall(body) or []
    whole = "\n".join(blocks).strip()
    if whole:
        try:
            import ast as _ast

            for node in _ast.parse(whole).body:
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    symbols.append(node.name)
                    declarations.append(
                        "def " + node.name + "(" + _ast.unparse(node.args) + ") -> "
                        + _ast.unparse(node.returns or _ast.Constant(None)) + ": ..."
                    )
                elif isinstance(node, _ast.ClassDef):
                    symbols.append(node.name)
                    base = _ast.unparse(node)
                    declarations.append(base)
                elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
                    symbols.append(node.target.id)
                    declarations.append(_ast.unparse(node))
                elif isinstance(node, _ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, _ast.Name):
                            symbols.append(tgt.id)
                            declarations.append(_ast.unparse(node))
            if symbols:
                return symbols, declarations, problems
        except SyntaxError:
            problems.append("形状段的 python 代码块不是合法 Python（已退化为逐行解析）")

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or not stripped:
            continue
        m = SYMBOL_DECL.match(stripped)
        if not m:
            continue
        name = next((g for g in m.groups() if g), "")
        if not name:
            continue
        declarations.append(stripped)
        if name in symbols:
            problems.append(f"形状段里重复声明了符号 {name}（已去重）")
            continue
        symbols.append(name)
    if not symbols:
        problems.append("形状段里没有解析到任何符号声明（检查是否有 ```python 代码块）")
    return symbols, declarations, problems


def _split_row(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


#: 证据列里"明确声明没有独立证据"的说法（写进契约才算数，见 Obligation 的说明）
_NO_EVIDENCE_MARKERS = (
    "无独立证据", "没有独立证据", "无证据", "未判定", "不判定", "由约束保证",
    "no evidence", "n/a", "none",
)


def _evidence_of(cell: str) -> list[str]:
    """从证据列里抽证据名（含 test 的 token）。"""
    out: list[str] = []
    for token in _EVIDENCE_TOKEN.findall(cell or ""):
        if "test" in token.lower() and token not in out:
            out.append(token)
    return out


def _declares_no_evidence(cell: str) -> bool:
    low = (cell or "").strip().lower()
    return any(m in low for m in _NO_EVIDENCE_MARKERS)


def _parse_obligations(body: str) -> tuple[list[Obligation], list[str]]:
    """解析义务表。表头之后每一行都要有 id；id 形状不对的行**如实报出来**。"""
    obligations: list[Obligation] = []
    problems: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _split_row(stripped)
        if len(cells) < 2:
            continue
        if set(cells[0]) <= set("-: "):          # 分隔行
            continue
        m = OBLIGATION_ID.match(cells[0])
        if not m:
            if cells[0] and not cells[0].startswith("id"):
                problems.append(f"义务表里有一行 id 形状不对（要求 OBL<数字>）：{cells[0][:40]!r}")
            continue
        oid = m.group(1).upper()
        if oid in seen:
            problems.append(f"义务 id 重复：{oid}")
            continue
        seen.add(oid)
        text = cells[1]
        cell = cells[2] if len(cells) > 2 else ""
        obligations.append(
            Obligation(
                id=oid,
                text=text[:400],
                evidence=_evidence_of(cell),
                no_evidence_declared=_declares_no_evidence(cell),
            )
        )
    if not obligations:
        problems.append("没有解析到任何义务（契约没有约束力）")
    return obligations, problems


def _count_body_lines(body: str) -> int:
    """数一数这一段的实质行数（跳过标题行、空行、标注引用）。"""
    n = 0
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith(">") or s.startswith("```"):
            continue
        n += 1
    return n


def parse_contract(path: Path, *, module: str = "") -> Contract:
    """读一个契约文件。`module` 不传时按文件名推（`routing.CONTRACT.md` → `routing.py`）。"""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if not module:
        stem = path.name[: -len(SUFFIX)] if path.name.endswith(SUFFIX) else path.stem
        module = f"{stem}.py"

    sections = _sections(text)
    symbols, declarations, p1 = _parse_shape(_section(sections, "形状", "shape"))
    obligations, p2 = _parse_obligations(_section(sections, "义务", "obligation"))
    data_body = _section(sections, "数据", "data")
    topo_body = _section(sections, "拓扑", "topolog")

    return Contract(
        module=module,
        path=str(path),
        symbols=symbols,
        declarations=declarations,
        obligations=obligations,
        data_blocks=_count_body_lines(data_body),
        topology=_count_body_lines(topo_body),
        problems=p1 + p2,
    )
