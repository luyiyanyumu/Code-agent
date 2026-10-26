"""M25：CH-agent → Code-agent 改名后的兼容与迁移（对外行为不变、数据不丢）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_agent import config


# ---------------------------------------------------------------------------
# 环境变量兼容：旧前缀自动搬新前缀，新名优先
# ---------------------------------------------------------------------------
def test_legacy_env_migrated(monkeypatch):
    """旧前缀自动搬成新前缀。

    **必须自己兜底清掉 `CODE_AGENT_MODEL_DEFAULT`**（M44 定位到的一次真实污染）：
    `migrate_legacy_env()` 是把值**直接写进 `os.environ`** 的，而 monkeypatch 只负责
    还原它自己动过的变量——这里 `delenv(..., raising=False)` 面对一个**本来就不存在**
    的变量不会登记撤销，于是那次写入会**泄漏到整个 pytest 进程**。
    后果不是"这条用例错"，而是后面所有依赖 `router.provider_override` 的用例
    静默跑到真 provider 上：`test_task_routing`（provider 一次没被调用）、
    `test_todo_tools`（工具没落轨迹）、`test_ws`（审批事件永远不来，243s 超时）。
    三条用例单独跑都是绿的，只有在整跑里才红——最难查的那一类。
    """
    monkeypatch.delenv("CODE_AGENT_MODEL_DEFAULT", raising=False)
    monkeypatch.setenv("CH_AGENT_MODEL_DEFAULT", "deepseek-v4-pro")
    try:
        moved = config.migrate_legacy_env()
        assert os.environ["CODE_AGENT_MODEL_DEFAULT"] == "deepseek-v4-pro"
        assert any("CH_AGENT_MODEL_DEFAULT" in m for m in moved)
    finally:
        os.environ.pop("CODE_AGENT_MODEL_DEFAULT", None)


def test_new_env_wins(monkeypatch):
    """新旧都设了 → 以新名为准（改名后用户显式配的新变量不能被旧变量盖掉）。"""
    monkeypatch.setenv("CH_AGENT_MODEL_DEFAULT", "old-value")
    monkeypatch.setenv("CODE_AGENT_MODEL_DEFAULT", "new-value")
    config.migrate_legacy_env()
    assert os.environ["CODE_AGENT_MODEL_DEFAULT"] == "new-value"


# ---------------------------------------------------------------------------
# 数据目录迁移：拷贝不删除、不覆盖已有、带标记只做一次
# ---------------------------------------------------------------------------
@pytest.fixture
def homes(tmp_path, monkeypatch):
    """造一个"旧目录 + 空的新目录"环境，并让 config 里的 home 指向它们。

    注意**不能**设 CODE_AGENT_HOME：那是"显式指定目录"，按设计就跳过迁移。
    这里要模拟的是"用户没设任何变量、用的默认 ~/.code-agent"，所以把 Path.home 指到 tmp。
    """
    root = tmp_path / "home"
    legacy = root / ".ch-agent"
    target = root / ".code-agent"
    legacy.mkdir(parents=True)
    (legacy / "ch_agent.db").write_text("SQLITE", encoding="utf-8")
    (legacy / "ch_agent.db-wal").write_text("WAL", encoding="utf-8")
    (legacy / "config.json").write_text('{"provider": "deepseek"}', encoding="utf-8")
    (legacy / "skills" / "demo").mkdir(parents=True)
    (legacy / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "_legacy_home", lambda: legacy)
    monkeypatch.delenv("CODE_AGENT_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: root))
    return legacy, target


def test_home_migration_copies_and_renames_db(homes):
    legacy, target = homes
    result = config.migrate_legacy_home()

    assert result == target
    # 数据都过来了
    assert (target / "code_agent.db").read_text(encoding="utf-8") == "SQLITE"
    assert (target / "code_agent.db-wal").read_text(encoding="utf-8") == "WAL"
    assert (target / "config.json").is_file()
    assert (target / "skills" / "demo" / "SKILL.md").is_file()
    # 旧库名不该留在新目录
    assert not (target / "ch_agent.db").exists()
    # **旧目录保留**（当备份，不删用户数据）
    assert (legacy / "ch_agent.db").is_file()
    assert (target / ".migrated-from-ch-agent").is_file()


def test_home_migration_is_idempotent(homes):
    _, target = homes
    assert config.migrate_legacy_home() is not None
    # 第二次调用：有标记 → 不再拷贝（也不会覆盖用户后来改的东西）
    (target / "config.json").write_text('{"provider": "changed"}', encoding="utf-8")
    assert config.migrate_legacy_home() is None
    assert (target / "config.json").read_text(encoding="utf-8") == '{"provider": "changed"}'


def test_home_migration_never_overwrites_existing(homes):
    legacy, target = homes
    target.mkdir(parents=True)
    (target / "code_agent.db").write_text("EXISTING", encoding="utf-8")
    assert config.migrate_legacy_home() is None
    assert (target / "code_agent.db").read_text(encoding="utf-8") == "EXISTING"


def test_home_migration_noop_without_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_legacy_home", lambda: tmp_path / ".ch-agent")  # 不存在
    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / ".code-agent"))
    assert config.migrate_legacy_home() is None


def test_home_migration_skips_explicit_home(homes, monkeypatch):
    """显式设了 CODE_AGENT_HOME（测试/临时实例）时不迁移。

    真机踩到：临时实例启动时把用户真实数据整份拷进了自己的 home，
    于是"临时环境"里冒出用户的历史会话——既浪费又让人误判。
    """
    legacy, target = homes
    monkeypatch.setenv("CODE_AGENT_HOME", str(target))
    assert config.migrate_legacy_home() is None
    assert not target.exists()
    assert (legacy / "ch_agent.db").is_file()        # 旧数据原地不动


# ---------------------------------------------------------------------------
# 品牌：代码里不该再有旧名字（这是"全改"的可验证定义）
# ---------------------------------------------------------------------------
def test_no_legacy_brand_in_source():
    """**源码**里残留旧品牌名 = 改名没做干净。

    只扫包内 Python 源码（README 是文档，里面本来就要写"从 CH-agent 改名过来"，
    还有仓库所在目录的真实路径，不参与这条断言）。允许的例外只有兼容层。
    """
    root = Path(__file__).resolve().parent.parent
    allow = {root / "code_agent" / "config.py"}
    bad: list[str] = []
    for path in (root / "code_agent").rglob("*.py"):
        if path in allow or "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("CH-agent", "ch_agent", "CH_AGENT_", "ch-agent"):
            if needle in text:
                bad.append(f"{path.relative_to(root)}: {needle}")
    assert not bad, bad[:10]
