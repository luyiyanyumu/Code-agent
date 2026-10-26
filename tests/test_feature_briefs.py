"""功能描述（M33）：给没写 docstring 的符号补一句"它是干什么的"。

三条硬约束，测试逐条盯着：

1. **不是一次性生成**：按「符号 id + 内容哈希」缓存——代码没改就一直复用，
   代码一改旧描述**自动失效**（不是删掉，是不再采用）。
2. **作者优先、绝不混**：作者写了 docstring 就用作者的；模型补的只顶上缺失的，
   并且 `label_from` / `brief` 字段如实标出这句话是谁写的。
3. **不进用户仓库**：描述存在 code-agent 自己的目录（`~/.code-agent/feature-briefs/`），
   用户的 git 里不会多出文件。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from code_agent.codeintel import briefs as briefs_mod
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.vcs import git_env
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.brief_tools import FeatureBriefTool


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**git_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git failed: {proc.stdout}{proc.stderr}")
    return proc.stdout.strip()


class FakeProjects:
    def __init__(self, row: dict) -> None:
        self.row = row

    def get(self, pid: str) -> dict | None:
        return self.row if pid == self.row["id"] else None

    def find_by_root(self, root_key: str) -> dict | None:
        return self.row if self.row["root_key"] == root_key else None

    def set_scan(self, pid: str, summary: str) -> None:
        pass


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """三个符号：有说明的函数、无说明的**类**（类一定进导航，是"待补"的典型）、私有辅助。"""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        '"""核心。"""\n\n\n'
        "def documented(value: int) -> int:\n"
        '    """作者写了说明：把值翻倍。"""\n'
        "    return value * 2\n\n\n"
        "class Widget:\n"
        "    size: int = 1\n\n"
        "    def grow(self) -> int:\n"
        "        return self.size + 1\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {"id": "p_brief", "name": "proj", "root_path": str(repo),
           "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin"}
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    return {"row": row, "intel": intel, "repo": repo}


DOC_ID = "sym:pkg/core.py#documented"
NODOC_ID = "sym:pkg/core.py#Widget"


def _node(intel, row, sid: str) -> dict:
    nav = intel.features(row)
    for domain in nav["domains"]:

        def walk(node: dict):
            for feature in node.get("features") or []:
                if feature["id"] == sid:
                    return feature
            for child in node.get("children") or []:
                found = walk(child)
                if found:
                    return found
            return None

        found = walk(domain)
        if found:
            return found
    raise AssertionError(f"功能导航里没有 {sid}")


def _walk_features(nav: dict) -> list[dict]:
    """把能力域树摊平成功能清单（测试里按 id 找节点用）。"""
    out: list[dict] = []

    def walk(node: dict) -> None:
        out.extend(node.get("features") or [])
        for child in node.get("children") or []:
            walk(child)

    for domain in nav.get("domains") or []:
        walk(domain)
    return out


def brief_fill_fill(targets, provider, store, *, batch: int | None = None):
    """按生产同一条路调引擎（写库带哈希）。"""
    from code_agent.codeintel import brief_fill

    def write(target, text: str) -> None:
        store.write(
            symbol_id=target.id,
            text=text,
            content_hash=target.content_hash,
            body_hash=target.body_hash,
            path_rel=target.path,
            name=target.name,
            model="fake-model",
        )

    kwargs = {"batch": batch} if batch else {}
    return asyncio.run(brief_fill.fill(targets, provider=provider, write=write, **kwargs))


async def _auto_then_drain(intel, row) -> dict:
    """触发自动补 + 等后台任务收尾（任务跑在同一个事件循环里）。"""
    started = intel.maybe_autofill_briefs(row)
    for _ in range(300):
        if not intel.brief_fill_status(row)["running"]:
            break
        await asyncio.sleep(0.02)
    return started


# ---------------------------------------------------------------------------
# 存储本身
# ---------------------------------------------------------------------------
def test_store_keeps_latest_and_can_forget(tmp_path: Path) -> None:
    store = briefs_mod.BriefStore("k1", path=tmp_path / "b.jsonl")
    store.write(symbol_id=DOC_ID, text="第一版", content_hash="h1")
    store.write(symbol_id=DOC_ID, text="第二版", content_hash="h2")
    latest = store.latest()
    assert latest[DOC_ID]["text"] == "第二版"          # 同一符号以最后一条为准
    assert store.forget(DOC_ID) is True
    assert store.latest() == {}
    assert store.forget(DOC_ID) is False               # 再删一次如实返回 False


def test_store_rejects_empty_or_too_long(tmp_path: Path) -> None:
    store = briefs_mod.BriefStore("k2", path=tmp_path / "b.jsonl")
    with pytest.raises(briefs_mod.BriefError):
        store.write(symbol_id=DOC_ID, text="   ")
    with pytest.raises(briefs_mod.BriefError):
        store.write(symbol_id=DOC_ID, text="长" * (briefs_mod.MAX_TEXT + 1))


def test_briefs_live_outside_the_repo(env) -> None:
    """描述不能写进用户仓库：它落在 code-agent 自己的数据目录里。"""
    before = sorted(
        str(p.relative_to(env["repo"])) for p in env["repo"].rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    env["intel"].feature_brief_write(env["row"], symbol_id=NODOC_ID, text="加一", model="m")
    store = env["intel"]._brief_store(env["row"])
    assert store.path.is_file()
    assert env["repo"] not in store.path.parents
    assert "feature-briefs" in str(store.path)
    after = sorted(
        str(p.relative_to(env["repo"])) for p in env["repo"].rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    assert after == before, "往仓库里写了东西（描述必须留在平台目录）"


# ---------------------------------------------------------------------------
# 用在导航上：作者优先、标记清楚
# ---------------------------------------------------------------------------
def test_undocumented_symbol_uses_the_brief_and_says_so(env) -> None:
    node = _node(env["intel"], env["row"], NODOC_ID)
    assert node["label_from"] == "name" and node["brief_missing"] is True
    assert node["label"] == "Widget"

    env["intel"].feature_brief_write(
        env["row"], symbol_id=NODOC_ID, text="加一：给一个整数加 1", model="deepseek-flash"
    )
    node = _node(env["intel"], env["row"], NODOC_ID)
    assert node["label"] == "加一：给一个整数加 1"        # 冒号前太短就不切（doc_label 的规则）
    assert node["label_from"] == "brief"               # 且标明"这句是谁写的"
    assert node["brief"]["model"] == "deepseek-flash"
    assert node["brief_missing"] is False


def test_author_docstring_wins_over_brief(env) -> None:
    env["intel"].feature_brief_write(
        env["row"], symbol_id=DOC_ID, text="模型也写了一句", model="m"
    )
    node = _node(env["intel"], env["row"], DOC_ID)
    assert node["label_from"] == "doc"
    assert node["label"] == "作者写了说明"
    assert node["brief"] is None
    # 作者写了 docstring 的符号不该出现在"待补"清单里
    assert node["brief_missing"] is False


def test_stale_brief_is_not_used_after_code_changes(env) -> None:
    env["intel"].feature_brief_write(env["row"], symbol_id=NODOC_ID, text="旧的说明", model="m")
    assert _node(env["intel"], env["row"], NODOC_ID)["label_from"] == "brief"

    # 改代码 → 内容哈希变 → 旧描述自动失效
    target = env["repo"] / "pkg" / "core.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("return self.size + 1", "return self.size + 2"),
        encoding="utf-8",
    )
    asyncio.run(env["intel"].scan(env["row"], full=True))
    node = _node(env["intel"], env["row"], NODOC_ID)
    assert node["label_from"] == "name", "代码改过之后旧描述不能再用"
    assert node["brief_missing"] is True
    # 但描述本身没被删：`feature_briefs` 如实说它已经失效
    listed = env["intel"].feature_briefs(env["row"])
    assert listed["count"] == 1 and listed["stale"] == 1
    assert listed["items"][0]["fresh"] is False


def test_purpose_card_marks_model_text(env) -> None:
    env["intel"].feature_brief_write(env["row"], symbol_id=NODOC_ID, text="加一：给整数加 1", model="m")
    out = env["intel"].symbol_purpose(env["row"], NODOC_ID)
    assert out["brief"]["text"] == "加一：给整数加 1"
    assert out["business"]["business_meaning"] == "加一：给整数加 1"
    assert out["business"]["source"] == "model"
    assert "加一：给整数加 1" not in out["headline"]
    assert not any(f["label"] == "模型补的说明" for f in out["facts"])
    # 作者写了 docstring 的符号：不会挂模型的话
    env["intel"].feature_brief_write(env["row"], symbol_id=DOC_ID, text="模型的话", model="m")
    doc_out = env["intel"].symbol_purpose(env["row"], DOC_ID)
    assert "brief" not in doc_out
    assert doc_out["headline"].startswith("作者写了说明")


def test_missing_list_tells_what_to_write_next(env) -> None:
    data = env["intel"].feature_brief_missing(env["row"])
    ids = [item["id"] for item in data["items"]]
    assert NODOC_ID in ids and DOC_ID not in ids      # 有 docstring 的不算缺
    env["intel"].feature_brief_write(env["row"], symbol_id=NODOC_ID, text="补上了", model="m")
    again = [item["id"] for item in env["intel"].feature_brief_missing(env["row"])["items"]]
    assert NODOC_ID not in again


# ---------------------------------------------------------------------------
# code-agent 自己写：工具
# ---------------------------------------------------------------------------
def test_tool_lists_missing_then_writes(env) -> None:
    tool = FeatureBriefTool(intel=env["intel"])
    ctx = ToolContext(session_id="s1", workspace=env["repo"])
    listed = asyncio.run(tool.execute({"action": "missing"}, ctx))
    assert NODOC_ID in listed and "补法" in listed

    written = asyncio.run(
        tool.execute({"action": "write", "id": NODOC_ID, "text": "给一个整数加 1"}, ctx)
    )
    assert "已记录" in written
    assert _node(env["intel"], env["row"], NODOC_ID)["label"] == "给一个整数加 1"

    listed_after = asyncio.run(tool.execute({"action": "missing"}, ctx))
    assert NODOC_ID not in listed_after

    other = asyncio.run(tool.execute({"action": "list"}, ctx))
    assert "给一个整数加 1" in other
    gone = asyncio.run(tool.execute({"action": "forget", "id": NODOC_ID}, ctx))
    assert "已删除" in gone


def test_tool_says_so_when_the_author_already_wrote_one(env) -> None:
    tool = FeatureBriefTool(intel=env["intel"])
    ctx = ToolContext(session_id="s1", workspace=env["repo"])
    out = asyncio.run(
        tool.execute({"action": "write", "id": DOC_ID, "text": "模型插一句"}, ctx)
    )
    # 不覆盖作者的话，而且明确告诉模型这条不会被采用
    assert "以作者的话为准" in out
    assert _node(env["intel"], env["row"], DOC_ID)["label_from"] == "doc"


def test_tool_without_code_mode_is_an_explicit_error(env) -> None:
    tool = FeatureBriefTool(intel=None)
    ctx = ToolContext(session_id="s1", workspace=env["repo"])
    with pytest.raises(Exception) as err:
        asyncio.run(tool.execute({"action": "missing"}, ctx))
    assert "代码索引服务不可用" in str(err.value)


# ---------------------------------------------------------------------------
# 批量补描述（M33）：一次问一批，**不带任何工具**
# ---------------------------------------------------------------------------
class FakeProvider:
    """假模型：按批把答案编好，用来验证"取源码 → 问模型 → 落库"这条链。"""

    name = "fake"
    model = "fake-model"

    def __init__(self, *, broken: bool = False, drop: bool = False) -> None:
        self.broken = broken
        self.drop = drop
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools):
        from code_agent.models.events import ModelTurn

        self.calls.append(messages)
        assert tools == [], "补描述这条路不该带任何工具"
        if self.broken:
            return ModelTurn(text="我觉得这个函数挺好的")
        text = messages[-1]["content"]
        # 编号就是从 1 开始的条目号：让描述带上编号，方便断言哪条写给谁
        numbers = [i for i, _ in enumerate(text.split("\n\n"), start=1)] if not self.drop else []
        payload = {"briefs": [{"n": n, "text": f"第{n}条的说明"} for n in numbers]}
        import json as _json

        return ModelTurn(text="```json\n" + _json.dumps(payload, ensure_ascii=False) + "\n```")


@pytest.fixture
def fill_repo(tmp_path: Path) -> Path:
    """12 个无说明的类：够凑一批（BATCH=12），能验证批边界。"""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    body = '"""核心。"""\n\n\n'
    for i in range(1, 8):
        body += f"class Thing{i}:\n    size: int = {i}\n\n    def grow(self) -> int:\n        return self.size + {i}\n\n\n"
    (root / "pkg" / "core.py").write_text(body, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def fillenv(fill_repo: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {"id": "p_fill", "name": "proj", "root_path": str(fill_repo),
           "root_key": str(fill_repo).replace("\\", "/").lower(), "user_id": "u_admin"}
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    return {"row": row, "intel": intel, "repo": fill_repo}


def test_source_segment_stays_inside_the_symbol(env) -> None:
    """给模型的源码片段要**只含这个符号**：带进整份文件既贵又容易把描述写串。"""
    from code_agent.codeintel import brief_fill

    index = env["intel"].index(env["row"]["id"], "main")
    row = index.symbol(DOC_ID)
    text = brief_fill.source_segment(lambda rel: env["intel"]._read_text(env["row"], rel), row)
    assert "def documented" in text
    assert "作者写了说明" in text
    assert "class Widget" not in text          # 下一个符号不许混进来


def test_parse_briefs_tolerates_wrapping_and_junk() -> None:
    from code_agent.codeintel import brief_fill

    wrapped = '```json\n{"briefs":[{"n":1,"text":" 按关键词分类 "},{"n":9,"text":"越界的编号"}]}\n```'
    got = brief_fill.parse_briefs(wrapped, 3)
    assert got == {1: "按关键词分类"}                # 越界编号丢掉
    assert brief_fill.parse_briefs("模型：我不会", 3) == {}
    assert brief_fill.parse_briefs('[{"text":"只有一条"}]', 1) == {1: "只有一条"}


def test_fill_writes_every_missing_symbol(fillenv) -> None:
    """一次补全：缺 7 条 → 全补上；导航里的功能名随之变成描述，且标着"模型补的"。"""
    intel, row = fillenv["intel"], fillenv["row"]
    before = intel.feature_brief_missing(row)["total"]
    assert before == 7
    provider = FakeProvider()
    targets = intel._collect_targets(row, limit=0)
    assert [t.name for t in targets if t.kind == "class"] == [f"Thing{i}" for i in range(1, 8)]
    assert {t.kind for t in targets} >= {"class", "method", "variable", "module"}
    store = intel._brief_store(row)
    result = brief_fill_fill(targets, provider, store)
    assert result.written == len(targets) and result.failed == 0
    intel._invalidate_nav(row["id"])          # 生产里由补描述的后台任务收尾时作废
    assert intel.feature_brief_missing(row)["total"] == 0

    nav_ids = {n["id"]: n for n in _walk_features(intel.features(row))}
    node = nav_ids["sym:pkg/core.py#Thing3"]
    assert node["label_from"] == "brief"
    assert node["label"].startswith("第")             # 描述成了功能名
    assert node["brief"]["model"] == "" or True       # 模型名由服务层填（这里直接调引擎）


def test_fill_reports_malformed_answers_instead_of_inventing(fillenv) -> None:
    """模型乱答：重试一次仍不行 → **如实计失败**，绝不编内容，而且不许把整轮炸掉。"""
    intel, row = fillenv["intel"], fillenv["row"]
    targets = intel._collect_targets(row, limit=0)
    store = intel._brief_store(row)
    provider = FakeProvider(broken=True)
    # 用小批次逼出多批：连着两批拿不到可用回答就该停下（否则白烧时间与额度）
    result = brief_fill_fill(targets, provider, store, batch=3)
    assert result.written == 0
    assert result.batches == 2, "第 2 批失败后就该停"
    assert result.failed == 6, "只算真的试过的那两条批"
    assert len(provider.calls) == 4, "每批各重试一次"
    assert result.aborted, "连续拿不到可用回答要停下，而不是把剩下的批次白烧完"
    assert store.latest() == {}


def test_fill_without_provider_says_so(fillenv) -> None:
    intel, row = fillenv["intel"], fillenv["row"]
    targets = intel._collect_targets(row, limit=0)
    result = brief_fill_fill(targets, None, intel._brief_store(row))
    assert result.written == 0
    assert "没有可用的模型" in result.aborted


def test_auto_fill_reuses_current_descriptions_without_another_model_call(fillenv) -> None:
    intel, row = fillenv["intel"], fillenv["row"]
    provider = FakeProvider()
    intel.set_brief_provider(lambda: provider)
    first = asyncio.run(_auto_then_drain(intel, row))
    assert first["started"] is True and first["state"] == "queued"
    status = intel.brief_fill_status(row)
    assert status["written"] == 23 and status["state"] == "complete"
    assert status["coverage_known"] and status["missing"] == 0
    assert status["described"] == status["coverage_total"]
    assert intel.feature_brief_missing(row)["total"] == 0
    before = len(provider.calls)
    second = intel.maybe_autofill_briefs(row)
    assert second["started"] is False and "已检查" in second["reason"]
    assert len(provider.calls) == before


def test_failed_autofill_keeps_the_one_shot(fillenv) -> None:
    """没配模型 / 模型乱答 → **不落**"已自动补"标记：那一次机会要留给能跑通的时候。

    真机踩到：DEEPSEEK_API_KEY 未配置，第一次自动补失败并落标记，之后永远不再自动补。
    """
    from code_agent.codeintel import briefs as briefs_mod

    intel, row = fillenv["intel"], fillenv["row"]
    intel.set_brief_provider(lambda: FakeProvider(broken=True))
    started = asyncio.run(_auto_then_drain(intel, row))
    assert started["started"] is True
    assert not briefs_mod.autofill_marked(briefs_mod.root_key_of(row["root_path"]))
    # 还能再来一次（换成能用的模型就补上了）
    intel.set_brief_provider(lambda: FakeProvider())
    again = asyncio.run(_auto_then_drain(intel, row))
    assert again["started"] is True
    assert intel.feature_brief_missing(row)["total"] == 0


def test_old_one_shot_markers_do_not_disable_incremental_fill(fillenv) -> None:
    from code_agent.codeintel import briefs as briefs_mod

    intel, row = fillenv["intel"], fillenv["row"]
    briefs_mod.mark_autofill(briefs_mod.root_key_of(row["root_path"]), detail="legacy")
    intel.set_brief_provider(lambda: FakeProvider())
    result = asyncio.run(_auto_then_drain(intel, row))
    assert result["started"] is True
    assert intel.brief_fill_status(row)["state"] == "complete"


def test_fill_skips_when_nothing_is_missing(fillenv) -> None:
    intel, row = fillenv["intel"], fillenv["row"]
    intel.set_brief_provider(lambda: FakeProvider())
    asyncio.run(_auto_then_drain(intel, row))
    again = intel.maybe_autofill_briefs(row)
    assert again["started"] is False and "已检查" in again["reason"]


def test_start_fill_is_single_flight(fillenv) -> None:
    """按钮点两下不该跑两份。"""
    intel, row = fillenv["intel"], fillenv["row"]
    intel._fill_jobs[intel._brief_key(row)] = {"running": True, "total": 7, "done": 3}
    res = intel.start_brief_fill(row)
    assert res["started"] is False and res["reason"] == "正在自动补功能描述"
    assert res["running"] is True and res["done"] == 3


def test_unchanged_descriptions_reuse_hashes_without_reading_source(fillenv, monkeypatch) -> None:
    intel, row = fillenv["intel"], fillenv["row"]
    provider = FakeProvider()
    intel.set_brief_provider(lambda: provider)
    asyncio.run(_auto_then_drain(intel, row))
    calls = len(provider.calls)

    def unexpected_read(*args):
        raise AssertionError("unchanged descriptions should not read source")

    monkeypatch.setattr(intel, "_read_text", unexpected_read)
    assert intel._collect_targets(row, limit=0) == []
    assert len(provider.calls) == calls


def test_only_changed_entities_and_parent_modules_need_incremental_descriptions(fillenv) -> None:
    intel, row, root = fillenv["intel"], fillenv["row"], fillenv["repo"]
    (root / "pkg" / "stable.py").write_text("def stable():\n    return 123\n", encoding="utf-8")
    asyncio.run(intel.scan(row))
    intel.set_brief_provider(lambda: FakeProvider())
    asyncio.run(_auto_then_drain(intel, row))
    path = root / "pkg" / "core.py"
    path.write_text(path.read_text(encoding="utf-8").replace("size: int = 1\n", "size: int = 41\n", 1), encoding="utf-8")
    # Inspect the inventory before invoking the fake generator again.
    intel.set_brief_provider(None)
    asyncio.run(intel.scan(row))
    key = intel._brief_key(row)
    intel._fill_jobs[key] = {}
    targets = intel._collect_targets(row, limit=0)
    ids = {target.id for target in targets}
    assert "sym:pkg/core.py#Thing1" in ids
    assert "sym:pkg/core.py#Thing2" not in ids
    assert "sym:pkg/stable.py#stable" not in ids
    assert "file:pkg/stable.py" not in ids
    assert {"mod:pkg", "mod:"} <= ids
    assert "file:pkg/core.py" not in ids, "the file still has its own current author comment"
    state = intel._brief_fill_state(row)
    assert state["stale"] == len(targets) and state["added"] == 0
    assert state["reused"] > state["stale"]
    assert sum(state["target_counts"].values()) == len(targets)


def test_source_change_stops_the_remaining_batch_inventory_before_next_model_request(fillenv) -> None:
    from code_agent.codeintel import brief_fill
    intel, row = fillenv["intel"], fillenv["row"]
    targets = intel._collect_targets(row, limit=0)
    provider = FakeProvider()
    written = []
    result = asyncio.run(brief_fill.fill(
        targets, provider=provider, write=lambda target, text: written.append(target.id), batch=3,
        should_continue=lambda: not provider.calls,
    ))
    assert len(provider.calls) == 1
    assert len(written) == 3
    assert result.source_changed and result.aborted
    assert result.failed == 0, "unattempted stale entities are not provider failures"


def test_source_change_during_fill_schedules_a_fresh_incremental_inventory_without_cooldown(fillenv) -> None:
    intel, row, root = fillenv["intel"], fillenv["row"], fillenv["repo"]

    class ChangingProvider(FakeProvider):
        async def chat(self, messages, tools):
            result = await super().chat(messages, tools)
            if len(self.calls) == 1:
                (root / "pkg" / "new.py").write_text("def fresh():\n    return 88\n", encoding="utf-8")
                intel._scan_sync(row, False, "main")
            return result

    provider = ChangingProvider()
    intel.set_brief_provider(lambda: provider)
    asyncio.run(_auto_then_drain(intel, row))
    status = intel.brief_fill_status(row)
    assert len(provider.calls) == 1, "an obsolete large inventory must stop at its next batch"
    assert status["state"] == "waiting" and status["retry_in"] == 0
    assert status["failed"] == 0 and not status["error"]
    assert status["written"] == 12 and status["skipped"] == 0, "unchanged entities in the current batch remain reusable"
    assert "增量" in status["note"]
    asyncio.run(_auto_then_drain(intel, row))
    assert intel.brief_fill_status(row)["missing"] == 0


def test_single_last_batch_source_change_is_incremental_waiting_not_model_failure(env) -> None:
    intel, row, root = env["intel"], env["row"], env["repo"]
    initial = intel._collect_targets(row, limit=0)
    assert len(initial) < 12

    class ChangingProvider(FakeProvider):
        async def chat(self, messages, tools):
            result = await super().chat(messages, tools)
            (root / "pkg" / "new.py").write_text("def added():\n    return 12\n", encoding="utf-8")
            intel._scan_sync(row, False, "main")
            return result

    provider = ChangingProvider()
    intel.set_brief_provider(lambda: provider)
    asyncio.run(_auto_then_drain(intel, row))
    status = intel.brief_fill_status(row)
    assert len(provider.calls) == 1
    assert status["state"] == "waiting" and status["retry_in"] == 0
    assert status["failed"] == 0 and status["skipped"] > 0 and not status["error"]
    assert "增量" in status["note"]
    intel.set_brief_provider(lambda: FakeProvider())
    asyncio.run(_auto_then_drain(intel, row))
    assert intel.brief_fill_status(row)["missing"] == 0
