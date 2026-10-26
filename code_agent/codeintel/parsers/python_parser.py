"""Python 解析器（M18 · P1）：用标准库 `ast`，不猜、不糊。

产出的是"符号"而不是"文本块"——地图与模型上下文都靠它：
- class（含 bases/decorators/成员方法/属性）
- function / async function（参数名、返回值、复杂度、docstring）
- 模块级 variable / constant（全大写视为常量）
- route（FastAPI/Flask 风格装饰器 → `GET /api/v1/x`），这是"接口"在地图上的可见形态
- import（解析成文件级依赖边的原始材料，跨文件解析在 graph.py）

边界与取舍：
- 解析失败**不让扫描挂掉**：异常记进 errors，该文件退化为"只有文件节点"。
- 复杂度是分支计数（近似），只用于"这个函数是不是该拆"的量级判断，不参与任何判定。
- 嵌套函数/类最多下钻 3 层：再深基本是闭包工厂，画进地图只增加噪声。
"""

from __future__ import annotations

import ast
from typing import Any

#: 装饰器方法名 → 路由 HTTP 方法（小写匹配）
_ROUTE_METHODS = frozenset({"get", "post", "put", "delete", "patch", "head", "options", "websocket", "route"})

#: 计入复杂度的分支类节点
_BRANCH_NODES = (
    ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
    ast.Try, ast.ExceptHandler, ast.BoolOp, ast.comprehension, ast.Match,
)

_MAX_DEPTH = 3
_DOC_LIMIT = 400
_SIG_LIMIT = 200


def _first_paragraph(doc: str | None) -> str:
    if not doc:
        return ""
    text = doc.strip()
    para = text.split("\n\n", 1)[0]
    para = " ".join(part.strip() for part in para.splitlines())
    return para[:_DOC_LIMIT]


def _decorator_name(node: ast.AST) -> str:
    try:
        if isinstance(node, ast.Call):
            return _decorator_name(node.func)
        return ast.unparse(node) if hasattr(ast, "unparse") else ""
    except Exception:  # ast.unparse 对极端语法可能抛错，装饰器只是展示信息
        return ""


def _complexity(node: ast.AST) -> int:
    return sum(1 for child in ast.walk(node) if isinstance(child, _BRANCH_NODES))


def _format_args(args: ast.arguments) -> tuple[str, list[str]]:
    """返回 (可读签名片段, 参数名列表)。默认值/注解尽力而为，不追求 100% 还原。"""
    names: list[str] = []
    parts: list[str] = []

    def render(arg: ast.arg, default: ast.AST | None = None) -> str:
        names.append(arg.arg)
        piece = arg.arg
        if arg.annotation is not None:
            try:
                piece += f": {ast.unparse(arg.annotation)}"
            except Exception:
                pass
        if default is not None:
            try:
                piece += f"={ast.unparse(default)[:40]}"
            except Exception:
                piece += "=…"
        return piece

    positional = list(args.posonlyargs) + list(args.args)
    defaults = list(args.defaults)
    pad = len(positional) - len(defaults)
    for idx, arg in enumerate(positional):
        parts.append(render(arg, defaults[idx - pad] if idx >= pad else None))
    if args.vararg is not None:
        names.append(args.vararg.arg)
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(render(arg, default))
    if args.kwarg is not None:
        names.append(args.kwarg.arg)
        parts.append("**" + args.kwarg.arg)
    return ", ".join(parts), names


def _returns(node: ast.AST) -> str:
    ann = getattr(node, "returns", None)
    if ann is None:
        return ""
    try:
        return ast.unparse(ann)[:80]
    except Exception:
        return ""


def _route_info(node: ast.AST) -> tuple[str, str] | None:
    """识别路由装饰器，返回 (method, path)。识别不出返回 None。"""
    for dec in getattr(node, "decorator_list", []):
        func = dec.func if isinstance(dec, ast.Call) else dec
        if not isinstance(func, ast.Attribute):
            continue
        method = func.attr.lower()
        if method not in _ROUTE_METHODS:
            continue
        path = ""
        if isinstance(dec, ast.Call) and dec.args:
            first = dec.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                path = first.value
        if method == "route":
            # Flask：@app.route("/x", methods=["POST"])
            http = "GET"
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        vals = [
                            el.value
                            for el in kw.value.elts
                            if isinstance(el, ast.Constant) and isinstance(el.value, str)
                        ]
                        if vals:
                            http = vals[0].upper()
            method = http
        elif method != "websocket":
            method = method.upper()
        if not path:
            continue
        return method.upper() if method == "WEBSOCKET" else method, path
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel_path: str, source: str) -> None:
        self.rel_path = rel_path
        self.source_lines = source.splitlines()
        self.symbols: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.entrypoint = False

    # -- 工具 --
    def _line_src(self, node: ast.AST) -> str:
        idx = getattr(node, "lineno", 0) - 1
        if 0 <= idx < len(self.source_lines):
            return self.source_lines[idx].strip()[:_SIG_LIMIT]
        return ""

    def _add(
        self, node: ast.AST, kind: str, name: str, parent: str = "", *, qname: str | None = None, **extra: Any
    ) -> str:
        """登记一个符号。qname 必须走关键字参数——它决定 id，被 **extra 吞掉就会退化成 name
        （方法会丢掉类名前缀、路由会拿路径当限定名，符号 id 直接失稳）。"""
        full = qname or name
        sid = f"sym:{self.rel_path}#{full}"
        self.symbols.append(
            {
                "id": sid,
                "kind": kind,
                "name": name,
                "qname": full,
                "line": getattr(node, "lineno", 0),
                "end_line": getattr(node, "end_lineno", None) or getattr(node, "lineno", 0),
                "parent": parent,
                "confidence": "high",
                **extra,
            }
        )
        return sid

    # -- 导入 --
    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.imports.append({"module": alias.name, "level": 0, "names": [], "raw": f"import {alias.name}"})
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        names = [alias.name for alias in node.names]
        self.imports.append(
            {
                "module": node.module or "",
                "level": node.level or 0,
                "names": names,
                "raw": f"from {'.' * (node.level or 0)}{node.module or ''} import {', '.join(names)}"[:160],
            }
        )
        self.generic_visit(node)

    # -- 类 --
    def visit_ClassDef(self, node: ast.ClassDef, depth: int = 0) -> None:
        qname = node.name
        bases = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                pass
        sid = self._add(
            node,
            kind="class",
            name=node.name,
            qname=qname,
            signature=f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}",
            doc=_first_paragraph(ast.get_docstring(node)),
            bases=bases,
            decorators=[_decorator_name(d) for d in node.decorator_list],
            complexity=_complexity(node),
            params=[],
            returns="",
        )
        if depth >= _MAX_DEPTH:
            return
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit_FunctionDef(child, depth=depth + 1, parent=sid, owner=qname, in_class=True)
            elif isinstance(child, ast.ClassDef):
                # 嵌套类：qname 用父类点号串起来
                self._nested_class(child, parent=sid, prefix=qname, depth=depth + 1)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                self._class_attr(child, parent=sid, owner=qname)

    def _nested_class(self, node: ast.ClassDef, parent: str, prefix: str, depth: int) -> None:
        bases = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                pass
        sid = self._add(
            node,
            kind="class",
            name=node.name,
            qname=f"{prefix}.{node.name}",
            parent=parent,
            signature=f"class {node.name}",
            doc=_first_paragraph(ast.get_docstring(node)),
            bases=bases,
            decorators=[_decorator_name(d) for d in node.decorator_list],
            complexity=_complexity(node),
            params=[],
            returns="",
        )
        if depth >= _MAX_DEPTH:
            return
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit_FunctionDef(child, depth=depth + 1, parent=sid, owner=f"{prefix}.{node.name}", in_class=True)

    def _class_attr(self, node: ast.Assign | ast.AnnAssign, parent: str, owner: str) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            kind = "constant" if target.id.isupper() else "variable"
            self._add(
                node,
                kind=kind,
                name=target.id,
                qname=f"{owner}.{target.id}",
                parent=parent,
                signature=self._line_src(node),
                doc="",
                params=[],
                returns="",
                bases=[],
                decorators=[],
                complexity=0,
            )

    # -- 函数 / 方法 --
    def visit_FunctionDef(  # noqa: N802
        self,
        node: ast.FunctionDef,
        depth: int = 0,
        parent: str = "",
        owner: str = "",
        in_class: bool = False,
    ) -> None:
        is_async = isinstance(node, ast.AsyncFunctionDef)
        qname = f"{owner}.{node.name}" if owner else node.name
        arg_sig, params = _format_args(node.args)
        returns = _returns(node)
        prefix = "async def" if is_async else "def"
        signature = f"{prefix} {node.name}({arg_sig})" + (f" -> {returns}" if returns else "")
        route = _route_info(node)
        # 类里的是方法，函数里的嵌套函数是闭包（kind=function，只是 parent 指向外层）
        kind = "method" if in_class else "function"
        extra: dict[str, Any] = dict(
            signature=signature[:_SIG_LIMIT],
            doc=_first_paragraph(ast.get_docstring(node)),
            params=params,
            returns=returns,
            decorators=[_decorator_name(d) for d in node.decorator_list],
            bases=[],
            complexity=_complexity(node),
        )
        if route is not None:
            # 路由是"接口"，单独一种 kind：地图上要能一眼看出对外暴露了什么
            method, path = route
            sid = self._add(
                node,
                kind="route",
                name=path,
                qname=qname,
                parent=parent,
                route_method=method,
                route_path=path,
                **extra,
            )
        else:
            if any(
                isinstance(d, ast.Name) and d.id == "property"
                or isinstance(d, ast.Attribute) and d.attr in ("setter", "getter", "deleter")
                for d in node.decorator_list
            ):
                kind = "property"
            sid = self._add(node, kind=kind, name=node.name, qname=qname, parent=parent, **extra)

        if depth >= _MAX_DEPTH:
            return
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 嵌套函数是闭包，不是方法
                self.visit_FunctionDef(child, depth=depth + 1, parent=sid, owner=qname)
            elif isinstance(child, ast.ClassDef):
                self._nested_class(child, parent=sid, prefix=qname, depth=depth + 1)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.visit_FunctionDef(node)

    # -- 模块级变量 --
    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            if isinstance(target, ast.Name):
                kind = "constant" if target.id.isupper() else "variable"
                self._add(
                    node, kind=kind, name=target.id,
                    signature=self._line_src(node), doc="", params=[], returns="",
                    bases=[], decorators=[], complexity=0,
                )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if isinstance(node.target, ast.Name):
            kind = "constant" if node.target.id.isupper() else "variable"
            self._add(
                node, kind=kind, name=node.target.id,
                signature=self._line_src(node), doc="", params=[], returns="",
                bases=[], decorators=[], complexity=0,
            )
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        """识别 `if __name__ == "__main__"`：这是"程序入口"的静态证据。"""
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
        ):
            for comp in test.comparators:
                if isinstance(comp, ast.Constant) and comp.value == "__main__":
                    self.entrypoint = True
        self.generic_visit(node)


def parse(rel_path: str, source: str) -> dict[str, Any]:
    """解析单个 Python 文件。返回归一化结果（symbols/imports/entrypoint/error）。"""
    result: dict[str, Any] = {
        "symbols": [], "imports": [], "entrypoint": False, "error": "", "module_doc": "",
    }
    try:
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    # 模块 docstring 单独给出：地图上的"模块用途"要的是"这个包/模块是干嘛的"，
    # 而不是"里面最大的那个类是干嘛的"
    result["module_doc"] = _first_paragraph(ast.get_docstring(tree))
    visitor = _Visitor(rel_path, source)
    try:
        visitor.visit(tree)
    except Exception as exc:  # 解析器自身出错不能让整个扫描失败
        result["error"] = f"visitor: {type(exc).__name__}: {exc}"
    result["symbols"] = visitor.symbols
    result["imports"] = visitor.imports
    result["entrypoint"] = visitor.entrypoint
    return result
