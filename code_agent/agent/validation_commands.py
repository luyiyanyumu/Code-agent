"""Conservative shared classification of plain inline Python file checks."""

from __future__ import annotations

import ast
import posixpath
import shlex


def inline_python_validation_paths(command: str) -> tuple[str, ...]:
    """Return literal files consumed by direct content assertions/syntax checks.

    This does not execute code or grant shell permission. A caller may use
    these paths as receipts only after a successful, non-mutating execution.
    Source dumps and unrelated ``assert True`` statements are not validation.
    """
    if any(character in command for character in "\r\n`$"):
        return ()
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="&|;<>")
        lexer.whitespace_split, lexer.commenters, lexer.escape = True, "", ""
        words = list(lexer)
        if (len(words) != 3 or words[1] != "-c"
                or posixpath.basename(words[0].replace("\\", "/")).lower()
                not in {"python", "python.exe", "python3"}):
            return ()
        module = ast.parse(words[2])
    except (SyntaxError, ValueError, RecursionError):
        return ()
    path_names, ast_names, parse_names = set(), set(), set()
    reserved = {"print", "len", "open", "compile"}
    for statement in module.body:
        if isinstance(statement, ast.Import):
            if any(alias.name != "ast" or alias.asname in reserved for alias in statement.names):
                return ()
            ast_names.update(alias.asname or alias.name for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            if (statement.level or statement.module not in {"pathlib", "ast"}
                    or any(alias.name != ("Path" if statement.module == "pathlib" else "parse")
                           or alias.asname in reserved for alias in statement.names)):
                return ()
            if statement.module == "pathlib":
                path_names.update(alias.asname or alias.name for alias in statement.names)
            else:
                parse_names.update(alias.asname or alias.name for alias in statement.names)

    def literal_path(call: ast.AST) -> bool:
        if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
                or call.func.id not in path_names | {"open"} or not call.args
                or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str)):
            return False
        if any(character in call.args[0].value for character in "\x00\r\n"):
            return False
        if call.func.id in path_names:
            return len(call.args) == 1 and not call.keywords
        modes = [*call.args[1:2], *(kw.value for kw in call.keywords if kw.arg == "mode")]
        return all(isinstance(mode, ast.Constant) and mode.value in {"r", "rt", "rb"} for mode in modes)

    validated_paths: list[str] = []

    def checked_expression(expression: ast.AST) -> bool:
        reads = []
        for node in ast.walk(expression):
            if isinstance(node, (ast.BoolOp, ast.IfExp, ast.Lambda, ast.comprehension, ast.NamedExpr)):
                return False
            if not isinstance(node, ast.Call):
                continue
            if literal_path(node) or isinstance(node.func, ast.Name) and node.func.id == "len":
                continue
            if (isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "read_bytes", "read", "readlines"}
                    and literal_path(node.func.value) and not node.args):
                reads.append(node.func.value.args[0].value)
                continue
            # No arbitrary sinks (especially print/eval) inside a checked read.
            return False
        validated_paths.extend(reads)
        return bool(reads)

    for statement in module.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(statement, ast.Assert) and isinstance(statement.test, ast.Compare) and statement.msg is None:
            if not checked_expression(statement.test):
                return ()
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            syntax_check = (isinstance(call.func, ast.Name) and call.func.id in parse_names | {"compile"}
                            or isinstance(call.func, ast.Attribute) and call.func.attr == "parse"
                            and isinstance(call.func.value, ast.Name) and call.func.value.id in ast_names)
            if syntax_check and call.args and checked_expression(call.args[0]):
                # Extra arguments are labels/modes, not additional file readers.
                if not all(isinstance(argument, ast.Constant) for argument in call.args[1:]) or call.keywords:
                    return ()
                continue
            if (isinstance(call.func, ast.Name) and call.func.id == "print" and not call.keywords
                    and all(isinstance(argument, ast.Constant) for argument in call.args)):
                continue
        return ()
    return tuple(dict.fromkeys(validated_paths))

