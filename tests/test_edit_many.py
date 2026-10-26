"""`edit_many`（M52）：一次多处精确替换。

**DSH 没有这个工具**，是这边多出来的能力——理由是"改一个跨文件的字段重命名"用
`edit_file` 要发 N 轮工具调用，每一轮都要重新装配上下文、都要一次模型往返。

它的核心是一个取舍：**两段式**。先在内存里按顺序推演全部改动，全部通过才落盘。
这个文件里一半的断言都在钉这件事——因为"做一处算一处"的失败形态最难查：
调用方收到一条错误，却不知道已经改掉了哪些。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.code_tools import MAX_EDITS_PER_CALL, EditManyTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


class _FakeService:
    """只记台账调用，不碰真实代码情报栈（这里要测的是"每文件一条"）。"""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def record_patch(self, **kwargs):
        self.records.append(kwargs)
        return {"id": f"p{len(self.records)}"}


def _ctx(workspace: Path, obs: FsObservation | None = None) -> ToolContext:
    return ToolContext(session_id="s", workspace=workspace, fs_observation=obs)


def _edit_many(workspace, edits, *, service=None, obs=None) -> str:
    return asyncio.run(EditManyTool(service).execute({"edits": edits}, _ctx(workspace, obs)))


def _touch(workspace: Path, name: str, text: str) -> Path:
    path = workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# 基本
# ----------------------------------------------------------------------
def test_applies_edits_across_files(tmp_path) -> None:
    _touch(tmp_path, "a.py", "x = 1\ny = 2\n")
    _touch(tmp_path, "b.py", "z = 3\n")

    out = _edit_many(
        tmp_path,
        [
            {"path": "a.py", "old_string": "x = 1", "new_string": "x = 10"},
            {"path": "a.py", "old_string": "y = 2", "new_string": "y = 20"},
            {"path": "b.py", "old_string": "z = 3", "new_string": "z = 30"},
        ],
    )
    assert "已修改 2 个文件" in out
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 10\ny = 20\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "z = 30\n"


def test_same_file_edits_apply_in_order(tmp_path) -> None:
    """同一文件的多处**按顺序叠加**：第二处看到的是第一处改过之后的内容。"""
    _touch(tmp_path, "a.py", "alpha\n")
    _edit_many(
        tmp_path,
        [
            {"path": "a.py", "old_string": "alpha", "new_string": "beta"},
            {"path": "a.py", "old_string": "beta", "new_string": "gamma"},
        ],
    )
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "gamma\n"


def test_replace_all_inside_the_batch(tmp_path) -> None:
    _touch(tmp_path, "a.py", "n = 1\nm = 1\n")
    _edit_many(
        tmp_path,
        [{"path": "a.py", "old_string": "1", "new_string": "2", "replace_all": True}],
    )
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "n = 2\nm = 2\n"


# ----------------------------------------------------------------------
# 两段式：任何一处不合法 → 一处都不写
# ----------------------------------------------------------------------
def test_one_invalid_edit_writes_nothing(tmp_path) -> None:
    """**核心**：整批校验失败时不做任何改动。

    "做一处算一处"的失败形态最难查：调用方收到错误却不知道已经改掉了哪些，
    于是要么重试（把已改的再改一遍 → old_string 找不到），要么以为全没改。
    """
    a = _touch(tmp_path, "a.py", "x = 1\n")
    b = _touch(tmp_path, "b.py", "y = 2\n")

    with pytest.raises(ToolError) as err:
        _edit_many(
            tmp_path,
            [
                {"path": "a.py", "old_string": "x = 1", "new_string": "x = 99"},
                {"path": "b.py", "old_string": "不存在的内容", "new_string": "z"},
            ],
        )
    message = str(err.value)
    assert "没有做任何改动" in message
    assert "第 2 处" in message, message
    assert a.read_text(encoding="utf-8") == "x = 1\n", "第一处不该被写进去"
    assert b.read_text(encoding="utf-8") == "y = 2\n"


def test_ambiguous_edit_blocks_the_whole_batch(tmp_path) -> None:
    _touch(tmp_path, "a.py", "dup\ndup\n")
    with pytest.raises(ToolError) as err:
        _edit_many(tmp_path, [{"path": "a.py", "old_string": "dup", "new_string": "x"}])
    assert "命中 2 处" in str(err.value)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "dup\ndup\n"


def test_missing_file_blocks_the_whole_batch(tmp_path) -> None:
    _touch(tmp_path, "a.py", "x\n")
    with pytest.raises(ToolError) as err:
        _edit_many(
            tmp_path,
            [
                {"path": "a.py", "old_string": "x", "new_string": "y"},
                {"path": "nope.py", "old_string": "a", "new_string": "b"},
            ],
        )
    assert "文件不存在" in str(err.value)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x\n"


def test_escape_is_refused(tmp_path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret\n", encoding="utf-8")
    with pytest.raises(ToolError) as err:
        _edit_many(tmp_path, [{"path": "../outside.py", "old_string": "secret", "new_string": "x"}])
    assert "越出工作区" in str(err.value) or "拒绝" in str(err.value)
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_read_before_write_gate_applies(tmp_path) -> None:
    """没先读就改 → 拒（同一套 FS 观察策略，换工具不能换策略）。"""
    path = _touch(tmp_path, "a.py", "x = 1\n")
    obs = FsObservation()
    with pytest.raises(ToolError) as err:
        _edit_many(tmp_path, [{"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}], obs=obs)
    assert "file has not been read" in str(err.value)
    assert path.read_text(encoding="utf-8") == "x = 1\n"

    # 读过之后就能改；而且改完观察要更新（同文件多处不会被自己判成陈旧）
    obs.record_read(path, raw=path.read_bytes())
    out = _edit_many(
        tmp_path,
        [
            {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
            {"path": "a.py", "old_string": "x = 2", "new_string": "x = 3"},
        ],
        obs=obs,
    )
    assert "已修改 1 个文件" in out
    assert path.read_text(encoding="utf-8") == "x = 3\n"


def test_validation_errors_are_all_reported(tmp_path) -> None:
    """多处不合法时一次全报（而不是"改一处报一处"，省掉模型来回试）。"""
    _touch(tmp_path, "a.py", "x\n")
    with pytest.raises(ToolError) as err:
        _edit_many(
            tmp_path,
            [
                {"path": "a.py", "old_string": "x", "new_string": "x"},        # 相同
                {"path": "b.py", "old_string": "a", "new_string": "b"},        # 缺文件
                {"path": "a.py", "old_string": "zzz", "new_string": "y"},      # 找不到
            ],
        )
    message = str(err.value)
    assert "3 处不合法" in message
    assert "第 1 处" in message and "第 2 处" in message and "第 3 处" in message


# ----------------------------------------------------------------------
# 限额与台账
# ----------------------------------------------------------------------
def test_cap_on_the_number_of_edits(tmp_path) -> None:
    _touch(tmp_path, "a.py", "x\n")
    edits = [
        {"path": "a.py", "old_string": str(i), "new_string": str(i)} for i in range(MAX_EDITS_PER_CALL + 1)
    ]
    with pytest.raises(ToolError) as err:
        _edit_many(tmp_path, edits)
    assert "最多" in str(err.value)


def test_empty_batch_is_refused(tmp_path) -> None:
    with pytest.raises(ToolError):
        _edit_many(tmp_path, [])


def test_one_ledger_entry_per_file(tmp_path) -> None:
    """每文件一条台账（不是每处一条）：回滚的粒度是"文件"，一处一条会让回滚变复杂。"""
    _touch(tmp_path, "a.py", "x = 1\ny = 2\n")
    _touch(tmp_path, "b.py", "z = 3\n")
    service = _FakeService()
    _edit_many(
        tmp_path,
        [
            {"path": "a.py", "old_string": "x = 1", "new_string": "x = 9"},
            {"path": "a.py", "old_string": "y = 2", "new_string": "y = 8"},
            {"path": "b.py", "old_string": "z = 3", "new_string": "z = 7"},
        ],
        service=service,
    )
    assert len(service.records) == 2
    rels = sorted(r["rel"] for r in service.records)
    assert rels == ["a.py", "b.py"]
    a_record = next(r for r in service.records if r["rel"] == "a.py")
    assert a_record["before"] == "x = 1\ny = 2\n"
    assert a_record["after"] == "x = 9\ny = 8\n", "台账里必须是整文件的前后，不是单处"


def test_partial_write_failure_is_reported_honestly(tmp_path, monkeypatch) -> None:
    """落盘中途失败：**如实报**哪些写了、哪些没写，绝不声称成功。

    跨文件真事务做不到——假装"要么全写要么全不写"就是撒谎。所以这里只保证
    "校验阶段的不合法绝不写盘"，落盘阶段的失败必须说清楚。
    """
    import code_agent.atomic_write as atomic

    _touch(tmp_path, "a.py", "x = 1\n")
    _touch(tmp_path, "b.py", "y = 2\n")

    real = atomic.atomic_replace
    calls = {"n": 0}

    def flaky(path, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("磁盘满了（模拟）")
        return real(path, text)

    monkeypatch.setattr(atomic, "atomic_replace", flaky)
    out = _edit_many(
        tmp_path,
        [
            {"path": "a.py", "old_string": "x = 1", "new_string": "x = 9"},
            {"path": "b.py", "old_string": "y = 2", "new_string": "y = 8"},
        ],
    )
    assert "部分失败" in out
    assert "磁盘满了" in out
    assert "不会自动回滚" in out
    assert "已修改" not in out, "部分失败时不许出现成功措辞"
    # 第一个文件确实写了（订单不确定，但必须如实报），第二个保持原样
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "y = 2\n"


# ----------------------------------------------------------------------
# 写租约：声明了就要为**每个**路径取锁
# ----------------------------------------------------------------------
def test_write_targets_covers_every_path() -> None:
    tool = EditManyTool(None)
    targets = tool.write_targets(
        {"edits": [{"path": "a.py"}, {"path": "b/c.py"}, {"path": "a.py"}]}
    )
    assert targets == ["a.py", "b/c.py", "a.py"], "要覆盖全部路径（去重交给注册表）"

    # 默认实现在这个工具上是**空的**（它没有 arguments['path']）——这正是必须覆盖的理由
    assert tool.write_targets({"edits": []}) == []
    assert tool.write_targets({}) == []


def test_lease_takes_a_lock_per_path(tmp_path) -> None:
    """两把锁真的都取了：`edit_many` 与 `edit_file` 并发改不同文件时才不会互相绕过。"""
    registry = ToolRegistry()
    tool = EditManyTool(None)
    registry.register(tool)
    ctx = _ctx(tmp_path)
    args = {"edits": [{"path": "a.py"}, {"path": "b.py"}]}

    async def main():
        assert registry._write_lease.size() == 0
        async with registry.write_lease("edit_many", args, ctx):
            assert registry._write_lease.size() == 2, "两个目标路径必须各有一把锁"
        assert registry._write_lease.size() == 0, "退出后锁表要回落到 0"

    asyncio.run(main())
