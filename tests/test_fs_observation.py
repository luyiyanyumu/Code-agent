"""文件观察策略 + 原子写（M41，对齐 `dsh-fs-observation-policy` / `dsh-atomic-write`）。

这一层要挡住的是一件很具体的事：模型在第 3 轮读到文件内容，在第 10 轮把一个已经
被**人**（或另一个会话）改过的文件整篇覆盖回去——而且它以为自己知道文件里有什么。
覆盖会静默丢掉别人的改动，且事后只能靠 patch 台账逐条比对才发现。

两类断言：

1. **门禁**（`FsObservation`）：未见不许改、见过但不一致不许改、确认缺失可创建、
   并发创建者不被覆盖。错误文案必须同时给出"硬事实"与"恢复动作"。
2. **原子性**（`atomic_write`）：写入不留临时文件、失败不破坏原文件、
   `O_EXCL` 创建不会静默覆盖已存在的文件。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.atomic_write import atomic_create, atomic_replace
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.code_tools import EditFileTool
from code_agent.tools.builtin.tools_builtin import ReadFileTool, WriteFileTool
from code_agent.tools.fs_observation import FsObservation


def _ctx(workspace, obs: FsObservation | None = None) -> ToolContext:
    return ToolContext(session_id="s_test", workspace=workspace, fs_observation=obs)


def _write(workspace, obs, path: str, content: str) -> str:
    return asyncio.run(
        WriteFileTool().execute({"path": path, "content": content}, _ctx(workspace, obs))
    )


def _read(workspace, obs, path: str, **kw) -> str:
    return asyncio.run(ReadFileTool().execute({"path": path, **kw}, _ctx(workspace, obs)))


def _edit(workspace, obs, path: str, old: str, new: str) -> str:
    return asyncio.run(
        EditFileTool(None).execute(
            {"path": path, "old_string": old, "new_string": new}, _ctx(workspace, obs)
        )
    )


# ----------------------------------------------------------------------
# 原子写
# ----------------------------------------------------------------------
def test_atomic_replace_creates_and_leaves_no_temp_file(tmp_path) -> None:
    target = tmp_path / "a.txt"
    atomic_replace(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    atomic_replace(target, "world\n")
    assert target.read_text(encoding="utf-8") == "world\n"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "a.txt"]
    assert leftovers == [], f"临时文件没清干净：{leftovers}"


def test_atomic_replace_keeps_existing_permissions(tmp_path) -> None:
    """改写不能顺手把权限位弄丢——`mkstemp` 建出来是 0600，直接 replace 会把
    0o755 的脚本变成 0600（POSIX 上就是这个断言在守；Windows 只认只读位，
    所以这里比对"改前 == 改后"而不是写死一个值）。"""
    import os
    import stat

    target = tmp_path / "run.sh"
    target.write_text("echo 1\n", encoding="utf-8")
    os.chmod(target, 0o755)
    before = stat.S_IMODE(target.stat().st_mode)
    atomic_replace(target, "echo 2\n")
    assert stat.S_IMODE(target.stat().st_mode) == before
    if os.name == "posix":
        assert before == 0o755


def test_atomic_create_refuses_to_clobber(tmp_path) -> None:
    """O_EXCL：已存在就返回 False，绝不覆盖（并发创建者的内容必须留着）。"""
    target = tmp_path / "a.txt"
    assert atomic_create(target, "first") is True
    assert atomic_create(target, "second") is False
    assert target.read_text(encoding="utf-8") == "first"


# ----------------------------------------------------------------------
# 门禁：write_file
# ----------------------------------------------------------------------
def test_write_refuses_to_overwrite_a_file_never_read(tmp_path) -> None:
    """**核心回归**：没读过就覆盖 → 拒，且文案给出"读一下再重试"。"""
    obs = FsObservation()
    target = tmp_path / "existing.py"
    target.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ToolError) as err:
        _write(tmp_path, obs, "existing.py", "x = 2\n")
    message = str(err.value)
    assert "file has not been read" in message
    assert "read the file, then retry" in message
    # 关键：原文件一个字节都没动
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_write_allows_creating_a_new_file(tmp_path) -> None:
    obs = FsObservation()
    _write(tmp_path, obs, "brand-new.py", "y = 1\n")
    assert (tmp_path / "brand-new.py").read_text(encoding="utf-8") == "y = 1\n"


def test_write_after_read_is_allowed(tmp_path) -> None:
    obs = FsObservation()
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _read(tmp_path, obs, "a.py")
    _write(tmp_path, obs, "a.py", "x = 2\n")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"


def test_write_is_refused_when_the_file_changed_after_the_read(tmp_path) -> None:
    """读完之后被人改过 → 拒。这是这个策略存在的唯一理由。"""
    obs = FsObservation()
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    _read(tmp_path, obs, "a.py")

    # 模拟"人 / 另一个会话"在两次调用之间改了它（mtime 也要走，才能触发比对）
    import os
    import time

    time.sleep(0.01)
    target.write_text("x = 1\n# 人加的注释\n", encoding="utf-8")
    os.utime(target, None)

    with pytest.raises(ToolError) as err:
        _write(tmp_path, obs, "a.py", "x = 2\n")
    assert "changed since it was read" in str(err.value)
    assert "人加的注释" in target.read_text(encoding="utf-8")   # 别人的改动活着


def test_read_of_missing_file_authorises_creation(tmp_path) -> None:
    """读过"它不存在"之后可以创建它（DSH 的 createIfAbsent）。"""
    obs = FsObservation()
    with pytest.raises(ToolError):
        _read(tmp_path, obs, "later.py")          # 读不存在的文件 → 报错但记下"确认缺失"
    assert obs.state(tmp_path / "later.py") == "absent"
    _write(tmp_path, obs, "later.py", "ok = 1\n")
    assert (tmp_path / "later.py").read_text(encoding="utf-8") == "ok = 1\n"


def test_write_refused_when_observed_missing_file_appeared(tmp_path) -> None:
    """读到"缺失"之后文件又出现 → 有并发创建者，不许覆盖。"""
    obs = FsObservation()
    with pytest.raises(ToolError):
        _read(tmp_path, obs, "race.py")
    (tmp_path / "race.py").write_text("别人的内容\n", encoding="utf-8")

    with pytest.raises(ToolError) as err:
        _write(tmp_path, obs, "race.py", "我的内容\n")
    assert "observed as missing" in str(err.value)
    assert (tmp_path / "race.py").read_text(encoding="utf-8") == "别人的内容\n"


def test_write_refused_when_observed_file_was_deleted(tmp_path) -> None:
    obs = FsObservation()
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    _read(tmp_path, obs, "gone.py")
    target.unlink()

    with pytest.raises(ToolError) as err:
        _write(tmp_path, obs, "gone.py", "x = 2\n")
    assert "deleted since it was read" in str(err.value)


def test_write_updates_the_observation(tmp_path) -> None:
    """写完立刻记账：同一轮里紧接着再写一次不该被自己的上一次改动拒掉。"""
    obs = FsObservation()
    (tmp_path / "a.py").write_text("1\n", encoding="utf-8")
    _read(tmp_path, obs, "a.py")
    _write(tmp_path, obs, "a.py", "2\n")
    _write(tmp_path, obs, "a.py", "3\n")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "3\n"


# ----------------------------------------------------------------------
# 门禁：edit_file
# ----------------------------------------------------------------------
def test_edit_requires_a_previous_read(tmp_path) -> None:
    obs = FsObservation()
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ToolError) as err:
        _edit(tmp_path, obs, "a.py", "x = 1", "x = 2")
    assert "file has not been read" in str(err.value)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_edit_after_read_succeeds(tmp_path) -> None:
    obs = FsObservation()
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _read(tmp_path, obs, "a.py")
    _edit(tmp_path, obs, "a.py", "x = 1", "x = 2")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"


def test_two_edits_in_a_row_succeed(tmp_path) -> None:
    """连续两次 edit 不能被"读完之后变了"自己绊倒（这个策略最容易写错的地方）。"""
    obs = FsObservation()
    (tmp_path / "a.py").write_text("a\nb\n", encoding="utf-8")
    _read(tmp_path, obs, "a.py")
    _edit(tmp_path, obs, "a.py", "a", "A")
    _edit(tmp_path, obs, "a.py", "b", "B")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "A\nB\n"


def test_edit_is_refused_when_changed_after_read(tmp_path) -> None:
    obs = FsObservation()
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    _read(tmp_path, obs, "a.py")
    import time

    time.sleep(0.01)
    target.write_text("x = 1\ny = 2\n", encoding="utf-8")

    with pytest.raises(ToolError) as err:
        _edit(tmp_path, obs, "a.py", "x = 1", "x = 9")
    assert "changed since it was read" in str(err.value)
    assert "y = 2" in target.read_text(encoding="utf-8")


def test_windowed_read_authorises_edit_while_unchanged(tmp_path) -> None:
    """窗口读（offset/limit）只记 stat 指纹，但没变就仍然授权编辑（DSH 同款口径）。"""
    obs = FsObservation()
    (tmp_path / "a.py").write_text("l1\nl2\nl3\n", encoding="utf-8")
    _read(tmp_path, obs, "a.py", offset=2, limit=1)
    _edit(tmp_path, obs, "a.py", "l3", "L3")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "l1\nl2\nL3\n"


def test_edit_on_missing_file_points_at_write_file(tmp_path) -> None:
    """文件根本不存在时，edit 给的是工具自己的提示（要新建就用 write_file）。"""
    obs = FsObservation()
    with pytest.raises(ToolError) as err:
        _edit(tmp_path, obs, "nope.py", "a", "b")
    assert "write_file" in str(err.value)


def test_edit_refused_when_observed_missing_file_appeared(tmp_path) -> None:
    """读到"缺失"之后文件又出现 → 我们没见过它的内容，不许改。"""
    obs = FsObservation()
    with pytest.raises(ToolError):
        _read(tmp_path, obs, "race.py")                  # 记下"确认缺失"
    (tmp_path / "race.py").write_text("别人的内容\n", encoding="utf-8")

    with pytest.raises(ToolError) as err:
        _edit(tmp_path, obs, "race.py", "别人的内容", "我的内容")
    assert "observed as missing" in str(err.value)
    assert (tmp_path / "race.py").read_text(encoding="utf-8") == "别人的内容\n"


# ----------------------------------------------------------------------
# 可插拔：没有 store 就不挂策略（程序化直调路径）
# ----------------------------------------------------------------------
def test_without_a_store_the_policy_is_not_applied(tmp_path) -> None:
    """`ctx.fs_observation is None` = 这条策略没挂（DSH 里它本身也是可插拔的）。

    但**原子写永远生效**——策略可关，正确性不可关。
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _write(tmp_path, None, "a.py", "x = 2\n")          # 没读也放行
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"


# ----------------------------------------------------------------------
# 端到端：loop 真的把 store 传下去了
# ----------------------------------------------------------------------
def test_agent_loop_enforces_the_policy_end_to_end(agent_home) -> None:
    """**这条是"接线"的守卫**：没有它，工具改对了但 loop 忘了传 store 也测不出来
    （两边都能过，只是策略静默失效）。

    判别方式：预先建好一个文件，然后让模型只调 `write_file` 覆盖它、从不读。
    接了线 → 被拒且文件不变；没接线 → 覆盖成功。
    """
    from code_agent.agent.loop import AgentLoop
    from code_agent.permissions.hooks import PermissionManager
    from code_agent.providers.offline import OfflineProvider
    from code_agent.storage.jsonl import TranscriptStore
    from code_agent.tools.registry import ToolRegistry

    workspace = agent_home / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "existing.py"
    target.write_text("original\n", encoding="utf-8")

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    results: list[str] = []
    provider = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn(
                "write_file", {"path": "existing.py", "content": "clobbered\n"}
            ),
            OfflineProvider.text_turn("好，我改完了"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=reg,
        permissions=PermissionManager(workspace_root=workspace),
        transcript=TranscriptStore(agent_home / "transcripts"),
        workspace=workspace,
        fs_observation=FsObservation(),
        on_event=lambda ev: results.append(str(ev.payload)) if ev.type.value == "tool_result" else None,
    )
    asyncio.run(loop.run("直接覆盖它"))

    assert target.read_text(encoding="utf-8") == "original\n", "策略没生效：文件被静默覆盖了"
    assert any("file has not been read" in r for r in results), results


def test_session_runtime_owns_one_observation_store() -> None:
    """观察记录挂在 runtime 上（不是 loop 上）：loop 每段重建，记录不能跟着重置。

    这里做源码级守卫——真正的行为代价（模型每段重读目标文件）在单测里很贵，
    而"接线接在哪一层"是这次改动最容易改错的地方。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "code_agent/sessions/manager.py").read_text(
        encoding="utf-8"
    )
    assert "self.fs_observation = FsObservation()" in src
    assert "fs_observation=self.fs_observation," in src
