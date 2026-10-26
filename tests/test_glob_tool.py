"""`glob` 工具（M43）：按路径模式发现文件。

为什么值得单独测模式编译器：模型给的模式必须按**可预期**的规则解释。`fnmatch` 的
`*` 会跨 `/`、`PurePath.match` 对 `**` 的行为各版本还在变，两者都不能直接用；
这里折算成正则，那么"折算对不对"就必须被钉住——否则工具会静默地多给或少给文件，
而模型无从察觉（它只会以为"这棵树里就这些"）。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.code_tools import GlobTool, _glob_to_regex


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(session_id="s_test", workspace=workspace)


def _glob(workspace: Path, **kwargs) -> str:
    return asyncio.run(GlobTool().execute(kwargs, _ctx(workspace)))


# ----------------------------------------------------------------------
# 模式编译
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        # 不含 `/` 的模式匹配任意深度的基名（DSH 同款：`*` 就是整棵树）
        ("*.py", "a.py", True),
        ("*.py", "src/deep/a.py", True),
        ("*.py", "a.pyc", False),
        # `**` 匹配零个或多个段
        ("**/*.py", "a.py", True),
        ("**/*.py", "src/a/b.py", True),
        ("src/**/*.ts", "src/b.ts", True),
        ("src/**/*.ts", "src/a/b.ts", True),
        ("src/**/*.ts", "other/a.ts", False),
        # `*` 不跨 `/`
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/a/b.py", False),
        # `?` 单字符
        ("a?.py", "ab.py", True),
        ("a?.py", "abc.py", False),
        # 字符类
        ("[ab]*.py", "apple.py", True),
        ("[ab]*.py", "cherry.py", False),
        ("[!a]*.py", "apple.py", False),
        ("[!a]*.py", "bpple.py", True),
        # 前缀 `./` 与反斜杠都按 POSIX 归一
        ("./src/*.py", "src/a.py", True),
        ("src\\*.py", "src/a.py", True),
        # 只给 `**` = 整棵树
        ("**", "a/b.py", True),
    ],
)
def test_glob_pattern_semantics(pattern: str, path: str, expected: bool) -> None:
    matched = bool(_glob_to_regex(pattern).match(path))
    assert matched is expected, f"{pattern!r} 对 {path!r} 的判定应为 {expected}"


def test_glob_is_case_insensitive_on_windows() -> None:
    """Windows 上大小写不敏感（与文件系统一致）；POSIX 上敏感。"""
    regex = _glob_to_regex("*.PY")
    assert bool(regex.match("a.py")) is (os.name == "nt")


# ----------------------------------------------------------------------
# 工具行为
# ----------------------------------------------------------------------
def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "src" / "deep" / "b.py").write_text("b\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("r\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "c.py").write_text("c\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "d.py").write_text("d\n", encoding="utf-8")
    return tmp_path


def test_glob_finds_files_at_any_depth(tmp_path) -> None:
    _repo(tmp_path)
    out = _glob(tmp_path, pattern="*.py")
    assert "src/a.py" in out
    assert "src/deep/b.py" in out
    assert "README.md" not in out


def test_glob_skips_ignored_and_hidden_dirs(tmp_path) -> None:
    """忽略规则与 grep/list_dir 同一套——这是刻意的设计，不是遗漏。

    三个发现工具对同一棵树必须给同一份答案，否则模型会看到互相矛盾的两份真相。
    """
    _repo(tmp_path)
    out = _glob(tmp_path, pattern="**/*.py")
    assert "node_modules/pkg/c.py" not in out
    assert ".git/d.py" not in out


def test_glob_respects_the_path_scope(tmp_path) -> None:
    _repo(tmp_path)
    out = _glob(tmp_path, pattern="*.py", path="src")
    assert "src/a.py" in out
    assert "README.md" not in out


def test_glob_sorts_newest_modified_first(tmp_path) -> None:
    _repo(tmp_path)
    import time

    time.sleep(0.01)
    newest = tmp_path / "src" / "newest_marker.py"
    newest.write_text("n\n", encoding="utf-8")
    out = _glob(tmp_path, pattern="*.py")
    first = out.splitlines()[1]           # 第 0 行是表头
    assert first == "src/newest_marker.py", out


def test_glob_cap_states_it_is_a_cap_not_an_absence(tmp_path) -> None:
    """达到上限时必须说清"这是条数上限"，并给收窄建议。

    这条不是文案洁癖：模型看到一份短清单，最容易的误判就是"这棵树里只有这些"，
    于是漏掉它本该读的文件。
    """
    _repo(tmp_path)
    for i in range(4):
        (tmp_path / f"extra{i}.py").write_text("x\n", encoding="utf-8")
    out = _glob(tmp_path, pattern="*.py", max_results=2)
    assert "只列前 2 个" in out
    assert "条数上限" in out
    assert "不是文件不存在" in out
    assert len([ln for ln in out.splitlines() if ln.endswith(".py")]) == 2


def test_glob_reports_no_matches_with_a_next_step(tmp_path) -> None:
    _repo(tmp_path)
    out = _glob(tmp_path, pattern="**/*.rs")
    assert "没有匹配的文件" in out
    assert "list_dir" in out


def test_glob_rejects_empty_pattern(tmp_path) -> None:
    with pytest.raises(ToolError):
        _glob(tmp_path, pattern="   ")


def test_glob_rejects_missing_path(tmp_path) -> None:
    with pytest.raises(ToolError):
        _glob(tmp_path, pattern="*.py", path="nope")


def test_glob_rejects_escape(tmp_path) -> None:
    """工作区之外不许搜——与 read/write 同一套硬边界。"""
    _repo(tmp_path)
    with pytest.raises(ToolError):
        _glob(tmp_path, pattern="*.py", path="../")


def test_glob_is_registered_and_readonly() -> None:
    """注册 + 只读策略：注册表里在，权限表里是只读放行。

    （后者其实由 tests/test_permission_policy_consistency.py 的全表断言覆盖，
    这里再钉一次是为了让"新工具忘了进策略表"在它自己的测试里就暴露。）
    """
    from code_agent.api.app import build_registry
    from code_agent.permissions.hooks import PermissionManager
    from code_agent.permissions.policy import Decision

    assert "glob" in set(build_registry().names())

    tmp = Path(os.environ.get("TEMP", "."))
    pm = PermissionManager(workspace_root=tmp, mode="read-only")
    res = asyncio.run(pm.evaluate("glob", {"pattern": "*.py"}, _ctx(tmp)))
    assert res.decision is Decision.allow, res.reason
