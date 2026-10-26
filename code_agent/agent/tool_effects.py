"""Local file-change and command receipts, without executing or interpreting code."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re
import shlex
from typing import Iterable

from code_agent.agent.validation_commands import inline_python_validation_paths


def annotation_satisfied(workspace: Path | None, raw: str) -> bool:
    """Conservative local no-op check, used only after a read and real validation.

    Unknown syntax/languages return false. A model's assertion that it has
    finished is never used as an annotation-coverage receipt.
    """
    if workspace is None:
        return False
    try:
        root = workspace.resolve()
        path = Path(raw)
        path = (path if path.is_absolute() else root / path).resolve()
        path.relative_to(root)
        if path.stat().st_size > 1024 * 1024:
            return False
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeError):
        return False
    chinese = re.compile(r"[\u3400-\u9fff]")
    if path.suffix == ".py":
        try:
            functions = [node for node in ast.walk(ast.parse(text)) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            return bool(functions) and all(chinese.search(ast.get_docstring(node) or "") for node in functions)
        except SyntaxError:
            return False
    if path.suffix.lower() not in (".js", ".mjs", ".cjs", ".ts") or re.search(r"\bclass\s|=>", text):
        return False
    functions = list(re.finditer(r"(?m)^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+[\w$]+\s*\(", text))
    if not functions:
        return False
    for function in functions:
        prefix = text[:function.start()].rstrip()
        if prefix.endswith("*/"):
            start = prefix.rfind("/*")
            comment = prefix[start:] if start >= 0 else ""
        else:
            lines = []
            for line in reversed(prefix.splitlines()):
                if not line.lstrip().startswith("//"):
                    break
                lines.append(line)
            comment = "\n".join(lines)
        if not chinese.search(comment):
            return False
    return True


def file_versions(workspace: Path | None, paths: Iterable[str]) -> dict[str, bytes | None]:
    """Read bounded fingerprints for already-authorized workspace targets only."""
    if workspace is None:
        return {}
    root = workspace.resolve()
    versions: dict[str, bytes | None] = {}
    remaining = 16 * 1024 * 1024
    for raw in dict.fromkeys(paths):
        try:
            path = Path(raw)
            path = (path if path.is_absolute() else root / path).resolve()
            path.relative_to(root)
            cap = min(8 * 1024 * 1024, remaining)
            if path.stat().st_size > cap:
                continue
            with path.open("rb") as handle:
                data = handle.read(cap + 1)
            if len(data) <= cap:
                versions[str(path)] = hashlib.blake2b(data, digest_size=16).digest()
                remaining -= len(data)
        except FileNotFoundError:
            versions[str(path)] = None
        except (OSError, ValueError):
            continue
    return versions


def changed_files(before: dict[str, bytes | None], after: dict[str, bytes | None]) -> tuple[str, ...]:
    return tuple(path for path, version in before.items() if path in after and after[path] != version)


def verification_paths(command: str, result: str, workspace: Path | None,
                       candidates: Iterable[str]) -> tuple[str, ...]:
    """Recognize successful explicit test/check commands, not arbitrary exit 0.

    This is a receipt that a process ran successfully, not proof of what its
    tests cover. The caller additionally checks that it did not mutate targets.
    No command, substitution, script body or output text is ever evaluated.
    """
    if not isinstance(result, str) or not re.match(r"^exit_code=0(?:\s|$)", result):
        return ()
    if not workspace or not command:
        return ()
    root = workspace.resolve()
    cwd = root

    def canonical(raw: str, base: Path | None = None) -> str:
        value = raw.replace("\\", "/")
        if root.drive and re.match(r"^/[A-Za-z]/", value):
            value = value[1] + ":" + value[2:]
        path = Path(value)
        return str((path if path.is_absolute() else (base or cwd) / path).resolve())

    selected = tuple(dict.fromkeys(canonical(p, root) for p in candidates))
    if not selected:
        return ()
    if inline_paths := inline_python_validation_paths(command):
        try:
            covered = {canonical(path) for path in inline_paths}
            return tuple(path for path in selected if path in covered and Path(path).is_relative_to(root))
        except (OSError, ValueError):
            return ()
    if any(c in command for c in "\n\r`$;|<>"):
        return ()
    covered: set[str] = set()
    saw_check = False
    try:
        for part in command.split("&&"):
            lexer = shlex.shlex(part.strip(), posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            lexer.escape = ""
            words = list(lexer)
            if not words:
                return ()
            name = Path(words[0].replace("\\", "/")).name.lower()
            if name in ("cd", "set-location") and len(words) == 2:
                target_dir = Path(canonical(words[1]))
                try:
                    target_dir.relative_to(root)
                except ValueError:
                    return ()
                cwd = target_dir
                continue
            if name == "echo":
                continue
            scoped = {path for path in selected if Path(path).is_relative_to(cwd)}
            if name in ("node", "node.exe") and len(words) >= 3 and words[1] in ("--check", "-c"):
                if len(words) != 3:
                    return ()
                target = canonical(words[2])
                if target not in selected:
                    return ()
                covered.add(target)
            elif name in ("python", "python.exe", "python3") and words[1:3] == ["-m", "py_compile"]:
                paths = {canonical(word) for word in words[3:] if not word.startswith("-")}
                if not paths or not paths <= set(selected):
                    return ()
                covered.update(paths)
            elif name in ("node", "node.exe") and len(words) >= 2 and (
                words[1] == "--test" or re.search(r"(?:^|[/\\._-])(?:verify|check|test|tests|spec)[\w.-]*\.[cm]?js$", words[1], re.I)
            ):
                paths = [canonical(word) for word in words[2:] if not word.startswith("-")]
                matches = set(paths) & set(selected)
                covered.update(matches or scoped)
            elif (name in ("pytest", "pytest.exe")
                  or name in ("python", "python.exe", "python3") and words[1:3] == ["-m", "pytest"]
                  or name in ("npm", "npm.cmd", "pnpm", "yarn", "bun") and len(words) >= 2 and (
                      words[1] in ("test", "check", "typecheck", "build")
                      or words[1] == "run" and len(words) >= 3 and words[2] in ("test", "check", "typecheck", "lint", "build"))
                  or name in ("mvn", "mvn.cmd", "gradle", "gradlew", "gradlew.bat") and "test" in words[1:]
                  or name in ("cargo", "go", "dotnet") and words[1:2] == ["test"]):
                covered.update(scoped)
            else:
                return ()
            saw_check = True
    except (ValueError, OSError):
        return ()
    return tuple(path for path in selected if path in covered) if saw_check else ()
