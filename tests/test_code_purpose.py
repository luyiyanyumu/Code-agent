"""代码模式 P10：符号的"意义与作用"说明。

点开 `logger` 以前只有 `logger = logging.getLogger(__name__)` 和 5 个引用文件，
读者得自己翻译。这个模块补一句话说清"它是什么、拿来干什么"。测试盯住四件事：

1. **作用域要判对**：模块级函数不能被说成"函数局部"（定义自己那一行不算"被自己包含"）；
2. **赋值 ≠ 关键字实参**：`RunConfig(max_rounds=...)` 不是"重新赋值"，否则常量会被
   误判成"不是常量"（真机跑出来过，所以用 AST 的 `ctx=Store` 数）；
3. **不发明语义**：命名推断必须逐条标注"按命名约定"，`token_budget` 不能被说成"凭据"；
4. **拿不准就降级**：非 Python 只给结构事实，解析失败要说明原因，而不是编一个漂亮解释。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from code_agent.codeintel import purpose
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.vcs import git_env


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

    def find_by_root(self, key: str) -> dict | None:
        return None

    def set_scan(self, pid: str, summary: str) -> None:
        pass


CONFIG_SRC = '''"""配置。"""

import logging
from typing import Literal

logger = logging.getLogger(__name__)

MAX_ROUNDS: int = 3                    # 轮次上限
TOKEN_BUDGET: int = 200_000            # 跨轮预算
DEFAULT_MODE = "compact"
LOOP_LABELS: dict[str, str] = {"compact": "紧凑", "expanded": "展开"}
_LITERAL_MODE: Literal["compact", "expanded"] = "compact"

_calls = 0
_seen: set[str] = set()
_index_of: dict[str, int] = {}


def note(name: str) -> None:
    _seen.add(name)


def drop(name: str) -> None:
    _seen.discard(name)


def remember(name: str, value: int) -> None:
    _index_of[name] = value


def bump() -> int:
    global _calls
    _calls += 1
    return _calls


def reset():
    global _calls
    _calls = 0


def transcripts_dir(root):
    """会话记录目录。"""
    return root / "transcripts"


def load(name: str, default: int = 1) -> int:
    return int(name) + default


class Budget:
    """一轮运行的预算。"""

    max_rounds: int = 3

    def from_env(self, env) -> "Budget":
        return Budget(max_rounds=int(env.get("R", self.max_rounds)))


class Step:
    """一个步骤。"""

    name: str
    prompt: str = ""

    @property
    def label(self) -> str:
        return self.name


class Draft:
    title: str
    body: str = ""
'''


OTHER_SRC = '''"""另一个模块，真的用到上面的东西。"""

from pkg.config import Draft, Step, TOKEN_BUDGET
import pkg.config as cfg


def use():
    return TOKEN_BUDGET + cfg.MAX_ROUNDS


def build():
    return Step(name="a", prompt="b")


def draft():
    return Draft(title="t", body="b")
'''

STEP_TEST_SRC = '''"""步骤的测试。"""

from pkg.config import Step


def test_step_label_uses_name():
    step = Step(name="hello", prompt="p")
    assert step.label == "hello"
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "config.py").write_text(CONFIG_SRC, encoding="utf-8")
    (root / "pkg" / "other.py").write_text(OTHER_SRC, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_steps.py").write_text(STEP_TEST_SRC, encoding="utf-8")
    (root / "web").mkdir()
    (root / "web" / "api.js").write_text("export function request(p) {\n  return fetch(p)\n}\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {"id": "p_p10", "name": "proj", "root_path": str(repo),
           "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin"}
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    return {"row": row, "intel": intel, "repo": repo}


def _purpose(env, sid: str) -> dict:
    return env["intel"].symbol_purpose(env["row"], sid)


def _fact(out: dict, label: str) -> str:
    for item in out["facts"]:
        if item["label"] == label:
            return item["text"]
    return ""


def test_module_level_function_is_not_reported_as_local(env):
    """`def transcripts_dir(...)` 的 lineno 就是函数自己的 lineno——不能被算成"函数局部"。"""
    out = _purpose(env, "sym:pkg/config.py#transcripts_dir")
    assert out["available"] is True
    assert out["scope"] == "模块级"
    assert _fact(out, "声明位置").startswith("模块级")
    # 有 docstring 时，"作用"直接引用作者原话
    assert "会话记录目录" in out["headline"]
    assert "函数" in out["headline"]


def test_method_scope_and_returns(env):
    out = _purpose(env, "sym:pkg/config.py#Budget.from_env")
    assert out["scope"] == "类成员"
    assert _fact(out, "声明位置").endswith("Budget")
    assert "Budget" in _fact(out, "返回")


def test_uppercase_module_constant_is_explained_as_constant(env):
    out = _purpose(env, "sym:pkg/config.py#MAX_ROUNDS")
    assert out["available"] is True
    assert out["role"] == "常量"
    assert "整数字面量" in _fact(out, "保存的值")
    assert "常量" in _fact(out, "常量性")


def test_keyword_argument_is_not_a_reassignment(env):
    """`Budget(max_rounds=...)` 里的 `max_rounds=` 不是重新赋值（正则数会数错）。"""
    out = _purpose(env, "sym:pkg/config.py#Budget.max_rounds")
    assert out["usage"]["writes_same_file"] == 1          # 只有类体里那一处
    assert "被重新赋值" not in out["headline"]


def test_name_hint_is_marked_as_convention_and_not_credential_for_budget(env):
    """命名推断必须标注来源；`TOKEN_BUDGET` 是预算，不是凭据。"""
    out = _purpose(env, "sym:pkg/config.py#TOKEN_BUDGET")
    hints = [f for f in out["facts"] if f["label"] == "命名暗示"]
    assert hints, "全大写 + budget 应该给出命名暗示"
    assert "命名约定" in hints[0]["text"]
    assert "凭据" not in out["headline"]
    assert "预算" in hints[0]["text"]


def test_cross_file_usage_finds_real_use_and_marks_text_matching(env):
    """跨文件使用是"候选文件里的词边界匹配"，找到的要给出文件与行号。"""
    out = _purpose(env, "sym:pkg/config.py#TOKEN_BUDGET")
    hits = out["usage"]["cross_files"]
    assert [h["path"] for h in hits] == ["pkg/other.py"]
    assert hits[0]["count"] >= 1
    assert any("词边界匹配" in n for n in out["notes"])
    assert "other.py" in _fact(out, "跨文件使用")


def test_cross_file_usage_understands_module_dot_name(env):
    """`cfg.MAX_ROUNDS` 这种"模块.名字"写法也算用到（只认裸名字会一律报"没人在用"）。"""
    out = _purpose(env, "sym:pkg/config.py#MAX_ROUNDS")
    assert [h["path"] for h in out["usage"]["cross_files"]] == ["pkg/other.py"]


def test_self_attribute_is_not_counted_as_module_symbol_use(env):
    """`self.max_rounds` 是实例属性，不是"别的文件用了这个模块级符号"。"""
    out = _purpose(env, "sym:pkg/config.py#Budget.max_rounds")
    assert all(h["path"] != "pkg/other.py" for h in out["usage"]["cross_files"])


def test_same_file_reads_are_counted_and_attribute_access_is_separate(env):
    """`self.x` / `cls.x` 是属性访问，单独记一笔；别混进普通读取里。"""
    out = _purpose(env, "sym:pkg/config.py#Budget.max_rounds")
    same = _fact(out, "本文件使用")
    assert "属性访问" in same
    assert out["usage"]["reads_same_file"] >= 1


def test_function_purpose_reports_callers_and_contract(env):
    out = _purpose(env, "sym:pkg/config.py#load")
    assert "入参 2 个" in out["headline"]
    assert "int" in _fact(out, "返回")
    assert any(f["label"] == "类型标注" for f in out["facts"])


def test_class_purpose_says_what_it_is_and_where_it_is_used(env):
    """类的"作用"不能只是"没有基类；含 0 个方法"——要说字段，还要指出**在哪被用**。"""
    out = _purpose(env, "sym:pkg/config.py#Step")
    # 作者写了说明就用作者的话（但用法照样要接上）
    assert "一个步骤" in out["headline"]
    assert _fact(out, "字段") == 'name: str · prompt: str = ""'
    usage = _fact(out, "用在哪")
    assert "pkg/other.py" in usage and "Step(" in usage
    assert "build" in usage, usage            # 归属到具体函数
    assert "import" not in usage, usage       # import 行不算用法
    assert "pkg/other.py" in out["headline"]


def test_undocumented_class_leads_with_its_fields(env):
    """没有 docstring 的类（真机上的 `WorkflowStep`）：字段就是它的含义，别再只说"没有基类"。"""
    out = _purpose(env, "sym:pkg/config.py#Draft")
    assert out["headline"].startswith("类 Draft：字段 title、body"), out["headline"]
    assert "pkg/other.py" in out["headline"]
    assert "没有基类" not in out["headline"]
    assert _fact(out, "字段") == 'title: str · body: str = ""'


def test_property_is_not_reported_as_a_field(env):
    """`@property def label(...)` 是取值方法，不是数据字段（真机上 `FailureBreaker` 中过招）。"""
    out = _purpose(env, "sym:pkg/config.py#Step")
    assert "label" not in _fact(out, "字段")
    assert "def " not in _fact(out, "字段")


def test_guard_tests_are_named_not_just_the_file(env):
    """守它的要写到**哪条测试**：只说"某测试文件提到过"等于没说。"""
    out = _purpose(env, "sym:pkg/config.py#Step")
    guard = _fact(out, "守着它的测试")
    assert "test_step_label_uses_name" in guard, guard
    assert "tests/test_steps.py" in guard, guard


def test_non_python_degrades_to_structure_facts_only(env):
    rows = env["intel"].index("p_p10").query_all("SELECT id FROM symbols WHERE path='web/api.js' LIMIT 1")
    out = _purpose(env, rows[0]["id"])
    assert out["available"] is True
    assert out["lang"] == "other"
    assert any("没有语句级语义" in n or "没有语句级 AST" in n for n in out["notes"])
    assert all("凭据" not in f["text"] for f in out["facts"])


def test_unknown_symbol_raises(env):
    with pytest.raises(purpose.PurposeError):
        _purpose(env, "sym:pkg/config.py#does_not_exist")


def test_unparsable_python_degrades_with_reason(env):
    """源码读不通时给结构事实 + 原因，不编解释。"""
    path = env["repo"] / "pkg" / "config.py"
    good = path.read_text(encoding="utf-8")
    try:
        path.write_text(good + "\ndef broken(:\n", encoding="utf-8")
        out = _purpose(env, "sym:pkg/config.py#MAX_ROUNDS")
    finally:
        path.write_text(good, encoding="utf-8")
    assert out["available"] is True
    assert out["reason"], "解析失败必须说明原因"
    assert any("不猜用途" in n for n in out["notes"])


def test_headline_always_present_and_facts_bounded(env):
    index = env["intel"].index("p_p10")
    for row in index.query_all("SELECT id FROM symbols LIMIT 40"):
        out = _purpose(env, row["id"])
        assert out["headline"], row["id"]
        assert len(out["facts"]) <= purpose.MAX_FACTS
        assert out["basis"]


# ---------------------------------------------------------------------------
# P12：文件 / 目录（地图上悬停要能回答"这个文件是干嘛的"）
# ---------------------------------------------------------------------------
def test_file_purpose_uses_module_docstring(env):
    out = _purpose(env, "file:pkg/config.py")
    assert out["available"] is True
    assert out["kind"] == "file"
    assert out["role"] == "文件"
    assert out["headline"].startswith("配置。")
    facts = {f["label"]: f["text"] for f in out["facts"]}
    assert "python" in facts["基本情况"] and "个符号" in facts["基本情况"]
    assert "函数" in facts["符号构成"]
    assert "导入了它" in facts["谁在用它"]


def test_file_purpose_flags_tests_and_unknown_files(env):
    out = _purpose(env, "file:pkg/config.py")
    assert "测试文件" not in {f["text"] for f in out["facts"]}
    # 不存在的文件：如实说，别编
    missing = _purpose(env, "file:pkg/nope.py")
    assert missing["available"] is False
    assert "索引里没有这个文件" in missing["reason"]


def test_dir_purpose_prefers_package_docstring(env):
    out = _purpose(env, "mod:pkg")
    assert out["available"] is True
    assert out["kind"] == "dir"
    facts = {f["label"]: f["text"] for f in out["facts"]}
    assert "个文件" in facts["规模"] and "个符号" in facts["规模"]
    assert "python" in facts["语言"]


def test_dir_purpose_handles_project_root_and_missing_dir(env):
    root = _purpose(env, "mod:")
    assert root["available"] is True
    assert "个文件" in root["headline"]
    empty = _purpose(env, "mod:nowhere")
    assert empty["available"] is True
    assert "空目录" in empty["headline"]


def test_describe_target_dispatches_by_prefix(env):
    """`file:` / `mod:` / 符号 id 走三条不同分支，前端不用分支。"""
    from code_agent.codeintel import purpose as mod

    index = env["intel"].index("p_p10")
    read = lambda rel: (env["repo"] / rel).read_text(encoding="utf-8")  # noqa: E731
    assert mod.describe_target(index, read_text=read, target="file:pkg/config.py")["kind"] == "file"
    assert mod.describe_target(index, read_text=read, target="mod:pkg")["kind"] == "dir"
    assert mod.describe_target(
        index, read_text=read, target="sym:pkg/config.py#logger"
    )["kind"] == "variable"


def test_writers_list_answers_who_can_change_it(env):
    """模块级状态最要紧的问题是"谁能改它"，不是"被改了几次"。"""
    out = _purpose(env, "sym:pkg/config.py#_calls")
    fact = _fact(out, "谁能改它")
    assert "bump()" in fact and "reset()" in fact
    assert "config.py:" in fact


def test_mutating_method_counts_as_a_writer(env):
    """`_seen.add(x)` / `_index_of[k] = v` 也是改它——只认 `name = ...` 会漏掉最常见的那种。"""
    seen = _purpose(env, "sym:pkg/config.py#_seen")
    fact = _fact(seen, "谁能改它")
    assert "note()" in fact and "drop()" in fact
    assert "add()" in fact and "discard()" in fact

    index = _purpose(env, "sym:pkg/config.py#_index_of")
    assert "下标赋值" in _fact(index, "谁能改它")
    assert "remember()" in _fact(index, "谁能改它")


def test_single_assignment_is_reported_as_read_only(env):
    out = _purpose(env, "sym:pkg/config.py#logger")
    assert "只有定义处那一次赋值" in _fact(out, "可变性")
    assert not _fact(out, "谁能改它")  # 定义自己那次赋值不算"谁在改它"


def test_value_set_from_container_literal(env):
    out = _purpose(env, "sym:pkg/config.py#LOOP_LABELS")
    values = _fact(out, "能取的值")
    assert "'compact'" in values and "'expanded'" in values
    assert "2 种" in values


def test_value_set_from_literal_annotation(env):
    out = _purpose(env, "sym:pkg/config.py#_LITERAL_MODE")
    assert "2 种" in _fact(out, "能取的值")


def test_scalar_value_is_not_printed_as_a_value_set(env):
    """`max_rounds = 3` 不是"取值范围"，别在抽屉里写"能取的值：3（1 种）"。"""
    out = _purpose(env, "sym:pkg/config.py#MAX_ROUNDS")
    assert not _fact(out, "能取的值")
