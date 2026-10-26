"""形状快照：抽出"契约层"（签名 / 数据 / import），**不比函数体**。

这是阶段 0 实验里那个 `gate.py` 的产品化版本。实验事实：

- 4 次独立实现，形状闸门**全部 PASS**（契约层一字未改），而函数体全是新写的 →
  闸门既能拦住违约，又不妨碍"实现可丢弃"；
- 闸门的自检也做过：改签名 → FAIL、加 import → FAIL、原样 → PASS。

所以这里只做三件事：
1. 抽模块级**公开符号**（函数/类/常量）及其形状字符串；
2. 抽 **dataclass / 类的字段**（字段名、类型、默认值、顺序）；
3. 抽 **import 集合**。

刻意不比：函数体、注释、docstring、语句顺序。**函数体正是被允许改变的东西。**
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any


class ShapeError(ValueError):
    """形状抽取失败（语法错等）。调用方据此如实报错，不要当"形状一致"。"""


@dataclass
class Shape:
    """一个 Python 文件的形状快照。"""

    symbols: dict[str, str] = field(default_factory=dict)   # 名字 -> 形状串
    imports: set[str] = field(default_factory=set)
    #: 有函数体（非空、非 `raise NotImplementedError`/`pass`/`...`）的符号名
    implemented: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": dict(sorted(self.symbols.items())),
            "imports": sorted(self.imports),
            "implemented": sorted(self.implemented),
        }

    def diff(self, other: "Shape") -> list[str]:
        """与另一个形状比对，返回差异说明（空列表 = 形状一致）。

        import 只在**契约声明了 import** 时才比对：契约的形状段通常只列签名与数据，
        不列 import；那种情况下拿"实现里的 import"去对比"空集合"只会产出假报警。
        """
        problems: list[str] = []
        extra = set(other.symbols) - set(self.symbols)
        missing = set(self.symbols) - set(other.symbols)
        if extra:
            problems.append(f"新增了公开符号（契约没声明）：{sorted(extra)}")
        if missing:
            problems.append(f"缺少契约声明的公开符号：{sorted(missing)}")
        for name in sorted(set(self.symbols) & set(other.symbols)):
            if self.symbols[name] != other.symbols[name]:
                problems.append(
                    f"形状被改动：{name}\n    契约声明: {self.symbols[name]}\n    实现实际: {other.symbols[name]}"
                )
        if self.imports:
            added = set(other.imports) - set(self.imports)
            if added:
                problems.append(f"新增 import（契约未允许）：{sorted(added)}")
        return problems


def _empty_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """函数体是不是"还没实现"（只由 pass / ... / raise NotImplementedError 组成）。"""
    body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                                          and isinstance(n.value.value, str))]
    if not body:
        return True
    if all(isinstance(n, ast.Pass) for n in body):
        return True
    if len(body) == 1:
        one = body[0]
        if isinstance(one, ast.Expr) and isinstance(one.value, ast.Constant) and one.value.value is Ellipsis:
            return True
        if isinstance(one, ast.Raise):
            exc = one.exc
            name = ""
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name):
                name = exc.id
            elif isinstance(exc, ast.Attribute):
                name = exc.attr
            if name == "NotImplementedError":
                return True
    return False


def _absorb(tree: ast.Module, shape: Shape) -> None:
    """把一棵 AST 里的模块级声明吸收进形状快照（`shape_of_source` 与声明解析共用）。

    **常量/模块级变量只比"存在"，不比形状串**（统一记成 `const`）。理由：常量的**值**
    是设计者的数据（契约 §3），它在实现里怎么写（带不带注解、值是字面量还是表达式）
    属于实现风格；阶段 0 实验里 `DEFAULT_ROUTE: Route = ROUTES["edit"]` 与
    `DEFAULT_ROUTE = ROUTES["edit"]` 就被这个口径误判过。值的漂移由人在 §3 复核，
    机器只保证"契约声明的常量还在"。
    """
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                shape.imports.add(f"{getattr(node, 'module', None) or ''}:{alias.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            shape.symbols[node.name] = (
                "func(" + ast.unparse(node.args) + ") -> " + ast.unparse(node.returns or ast.Constant(None))
            )
            if not _empty_body(node):
                shape.implemented.add(node.name)
        elif isinstance(node, ast.ClassDef):
            members: list[str] = []
            for sub in node.body:
                if isinstance(sub, ast.AnnAssign):
                    members.append(ast.unparse(sub))
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.append(f"{sub.name}(" + ast.unparse(sub.args) + ")")
                    if not _empty_body(sub):
                        shape.implemented.add(f"{node.name}.{sub.name}")
            bases = ",".join(ast.unparse(b) for b in node.bases)
            shape.symbols[node.name] = f"class[{bases}]{{" + "; ".join(members) + "}"
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    shape.symbols[tgt.id] = "const"
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            shape.symbols[node.target.id] = "const"


def _parse_declaration(text: str) -> ast.Module | None:
    """把一条契约声明解析成 AST。三种写法都认：

    - `class Foo:` + 缩进字段（直接能解析）
    - `def f(a: int) -> str: ...`（行内 `...` 已经是合法函数体）
    - `def f(a: int) -> str:`（没有函数体 → 补一个 `pass`）

    解析不了返回 None，由调用方跳过——**不抛异常**：契约是人手写的 markdown，
    形状段里出现自然语言占位是常态，不该因此让整次检查失败。
    """
    candidates = [text, text + "\n    pass", re.sub(r":\s*\.\.\.\s*$", ": pass", text)]
    for source in candidates:
        try:
            return ast.parse(source)
        except SyntaxError:
            continue
    return None


def shape_of_declarations(lines: list[str]) -> Shape:
    """把**契约形状段里的声明行**解析成形状快照，用来跟真实实现比对。

    逐条解析（同一条声明可能是多行，比如带字段的 `class`），所以它只能拿到"声明"、
    拿不到实现——这正是这份快照的用途。

    已知口径（刻意保留，与阶段 0 实验里的闸门一致）：
    - **装饰器不参与比对**（`@dataclass(frozen=True)` 在解析前会被剥掉）；
    - **模块级常量只比"存在"**，不比值（值属于契约 §3 的数据，见 `_absorb` 的说明）。
    """
    shape = Shape()
    for raw in lines:
        # 剥掉装饰器行：`ast.unparse(ClassDef)` 会把 `@dataclass(...)` 一起带出来，
        # 而装饰器既不该让声明解析失败，也不在比对范围内。
        text = "\n".join(
            ln for ln in raw.strip().splitlines() if not ln.lstrip().startswith("@")
        ).strip()
        if not text or text.startswith("#"):
            continue
        tree = _parse_declaration(text)
        if tree is None:
            continue          # 声明行解析不了就跳过；形状的缺失由 check 报告出来
        _absorb(tree, shape)
    return shape


def shape_of_source(source: str) -> Shape:
    """从源码文本抽形状。语法错抛 `ShapeError`（**不返回空形状**——那会被当"通过"）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ShapeError(f"解析失败：{exc.msg}（第 {exc.lineno} 行）") from exc
    shape = Shape()
    _absorb(tree, shape)
    return shape


def shape_of_file(path: Any) -> Shape:
    from pathlib import Path

    return shape_of_source(Path(path).read_text(encoding="utf-8", errors="replace"))
