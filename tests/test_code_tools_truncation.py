"""code_tools 载荷截断：超预算只丢条目，**绝不吐半截 JSON**。

这里盯的是一个真实翻车方式：旧实现是 `json.dumps(payload)[:12000]`——按字符剁，
剁在字符串/括号中间，模型拿到的工具结果语法都不合法，一个字段都读不出来，
还不知道"自己看到的是残缺的"。测试要求：

1. 任何预算下 `json.loads(结果)` 都必须成功（核心回归）；
2. 没超预算时输出与未截断的 dump **逐字节相同**，且不出现 `truncated` 键；
3. 超预算时 `truncated` 如实给出丢了几条（kept/omitted 自洽）与恢复动作；
4. 连一条都放不下、或载荷里根本没有可裁剪的列表时，仍返回合法 JSON 且不崩；
5. 上游自己的 `truncated` 事实（code_graph 的"每跳已截断"）不能被覆盖掉。
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from code_agent.codeintel.service import CodeIntelService
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.code_tools import CodeMapTool, _dump_capped

HINT = "缩小范围再查：把 limit 调小"


# ---------------------------------------------------------------------------
# 载荷工厂：条目够多/够长，才会真的顶出预算
# ---------------------------------------------------------------------------
def _item(i: int) -> dict:
    return {
        "id": f"sym:pkg/mod_{i}.py#handler_{i}",
        "kind": "function",
        "name": f"handler_{i}",
        "qname": f"pkg.mod_{i}.handler_{i}",
        "path": f"pkg/mod_{i}.py",
        "line": i * 3 + 1,
        "end_line": i * 3 + 9,
        "signature": f"def handler_{i}(request, *, option_{i}=None) -> dict[str, Any]",
    }


def _payload(count: int, *, list_key: str = "symbols") -> dict:
    return {
        "project": {"name": "proj", "root": "E:/proj"},
        "counts": {"symbols": count},
        list_key: [_item(i) for i in range(count)],
    }


# ---------------------------------------------------------------------------
# 1. 核心回归：旧切片吐非法 JSON，新实现永远可解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("budget", [8000, 12000])
def test_overflow_is_always_parseable_and_reports_omissions(budget: int):
    """超预算时结果必须可解析，且 kept/omitted 自洽、保留的是前缀。"""
    payload = _payload(140)
    untrimmed = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(untrimmed) > budget

    # 旧实现的产物：按字符切，切在半截 token 上 —— 语法都不合法
    with pytest.raises(json.JSONDecodeError):
        json.loads(untrimmed[:budget])

    out = _dump_capped(payload, list_key="symbols", budget=budget, hint=HINT)
    parsed = json.loads(out)  # 新实现：任何预算下都得能解析
    assert len(out) <= budget

    note = parsed["truncated"]
    assert note["kept"] + note["omitted"] == 140
    assert note["kept"] >= 1, "还有余量就不该把列表清空"
    assert note["omitted"] >= 1
    assert len(parsed["symbols"]) == note["kept"]
    assert parsed["symbols"] == payload["symbols"][: note["kept"]], "丢的必须是尾部，不许重排"
    # 诚实约定：说清预算事实 + 恢复动作，且不暗示上游数据缺失
    assert str(budget) in note["hint"] and "预算" in note["hint"]
    assert HINT in note["hint"]


def test_over_budget_does_not_mutate_caller_payload():
    """裁剪不能就地改调用方的 payload（同一个 dict 可能还要用于别处）。"""
    payload = _payload(140)
    before = json.dumps(payload, ensure_ascii=False, indent=2)
    _dump_capped(payload, list_key="symbols", budget=8000, hint=HINT)
    assert json.dumps(payload, ensure_ascii=False, indent=2) == before


# ---------------------------------------------------------------------------
# 2. 未超预算：逐字节等于旧输出，且不加任何键
# ---------------------------------------------------------------------------
def test_under_budget_is_byte_identical_and_silent():
    payload = _payload(3)
    expected = json.dumps(payload, ensure_ascii=False, indent=2)
    out = _dump_capped(payload, list_key="symbols", budget=8000, hint=HINT)
    assert out == expected
    assert "truncated" not in json.loads(out)


def test_under_budget_keeps_upstream_truncated_flag_untouched():
    """code_graph 自带的 truncated 布尔量在没超预算时必须原样留着。"""
    payload = {"start": "sym:a.py#f", "truncated": True, "layers": [{"depth": 1, "steps": []}]}
    out = _dump_capped(payload, list_key="layers", budget=8000, hint=HINT)
    assert json.loads(out)["truncated"] is True


# ---------------------------------------------------------------------------
# 3. 退化情形：连一条都放不下 / 非列表部分自己就超预算
# ---------------------------------------------------------------------------
def test_single_oversized_item_still_valid_json():
    """一条都塞不进预算：列表退化为空 + 说明，结果仍是合法 JSON。"""
    payload = _payload(6)
    for item in payload["symbols"]:
        item["signature"] = "x" * 700  # 单条就 ~700 字符，budget=800 时最多放 0 条

    out = _dump_capped(payload, list_key="symbols", budget=800, hint=HINT)
    parsed = json.loads(out)
    assert parsed["symbols"] == []
    note = parsed["truncated"]
    assert note["kept"] == 0 and note["omitted"] == 6
    assert "预算" in note["hint"] and HINT in note["hint"]


def test_non_list_part_alone_over_budget_returns_valid_json():
    """非列表字段自己就超预算：宁可超预算，也返回**完整合法** JSON，并如实说明。"""
    payload = {"symbol": {"signature": "y" * 9000}, "members": [_item(0), _item(1)]}
    out = _dump_capped(payload, list_key="members", budget=1000, hint=HINT)
    parsed = json.loads(out)
    assert parsed["symbol"]["signature"] == "y" * 9000  # 非列表字段不乱动
    assert parsed["members"] == []
    assert len(out) > 1000, "确实放不下就诚实超预算，不许切一半"
    assert "非列表字段" in parsed["truncated"]["hint"]


def test_missing_or_non_list_key_falls_back_without_crashing():
    """list_key 缺失或不是列表：不裁剪、不崩，返回合法 JSON + 说明。"""
    payload = {"symbols": "不是列表", "note": "z" * 900}

    out = _dump_capped(payload, list_key="symbols", budget=100, hint=HINT)
    parsed = json.loads(out)
    assert parsed["symbols"] == "不是列表"
    assert parsed["truncated"]["kept"] == 0 and parsed["truncated"]["omitted"] == 0
    assert "不是列表" in parsed["truncated"]["hint"]

    out2 = _dump_capped(payload, list_key="nope", budget=100, hint=HINT)
    assert json.loads(out2)["truncated"]["kept"] == 0


def test_upstream_truncated_flag_is_kept_in_note_when_trimming():
    """真要丢层时，code_graph 自己的"每跳已截断"布尔量不能被我们覆盖掉。"""
    payload = {
        "start": "sym:a.py#f",
        "truncated": True,
        "layers": [
            {"depth": d, "count": 1, "steps": [{"depth": d, "path": "a.py", "line": d, "name": "n" * 80}]}
            for d in range(1, 40)
        ],
    }
    out = _dump_capped(payload, list_key="layers", budget=1200, hint=HINT)
    note = json.loads(out)["truncated"]
    assert note["upstream"] is True
    assert note["kept"] + note["omitted"] == len(payload["layers"])
    assert note["omitted"] >= 1


# ---------------------------------------------------------------------------
# 4. 六个调用点：源码里不许再出现"序列化后按字符切片"
# ---------------------------------------------------------------------------
def test_no_character_slicing_of_json_remains_in_source():
    """用 AST 精确判定：模块里不许再有 `json.dumps(...)[:N]` 这种按字符剁的写法。"""
    source = (
        Path(__file__).resolve().parents[1]
        / "code_agent" / "tools" / "builtin" / "code_tools.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def is_dumps(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dumps"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json"
        )

    sliced = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript) and is_dumps(node.value)
    ]
    assert sliced == [], f"这些行还在序列化后按字符切片（会吐非法 JSON）：{sliced}"

    call_sites = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_dump_capped"
    ]
    assert len(call_sites) == 6, f"六个载荷点都要走 _dump_capped，实际：{call_sites}"


# ---------------------------------------------------------------------------
# 5. 端到端：真的走一次 code_map，超预算的输出必须能被 json.loads 解析
# ---------------------------------------------------------------------------
class FakeProjects:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.summaries: dict[str, dict] = {}

    def set_scan(self, project_id: str, summary_json: str) -> None:
        self.summaries[project_id] = json.loads(summary_json)

    def get(self, project_id: str) -> dict | None:
        return self.rows.get(project_id)

    def find_by_root(self, root_key: str) -> dict | None:
        for row in self.rows.values():
            if row["root_key"] == root_key:
                return row
        return None


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个符号足够多的项目：code_map 的 12000 字符预算一定会被顶爆。"""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text('"""示例包。"""\n', encoding="utf-8")
    body = ['"""一大批符号：用来把 code_map 的载荷顶出预算。"""', ""]
    for i in range(150):
        body += [
            "",
            f"def handler_{i}(request, *, option_{i}=None) -> dict:",
            f'    """处理第 {i} 类请求：校验入参后交给下游。"""',
            "    return {}",
        ]
    (root / "pkg" / "handlers.py").write_text("\n".join(body) + "\n", encoding="utf-8")
    return root


@pytest.fixture
def service(repo: Path, agent_home, monkeypatch) -> CodeIntelService:
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    fake = FakeProjects()
    row = {
        "id": "p_trunc", "name": "proj", "root_path": str(repo),
        "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    fake.rows["p_trunc"] = row
    svc = CodeIntelService(fake)
    asyncio.run(svc.scan(row, full=True))
    return svc


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(session_id="s_trunc", workspace=workspace, user_id="u_admin")


def test_code_map_over_budget_returns_parseable_payload(service, repo: Path):
    out = asyncio.run(CodeMapTool(service).execute({"limit": 400}, _ctx(repo)))
    parsed = json.loads(out)  # 旧实现在这里就炸了
    assert len(out) <= 12000
    note = parsed["truncated"]
    total = len(service.index("p_trunc").all_symbols(limit=400))
    assert total > note["kept"], "这个项目本来就该超预算，否则这条测试没在测东西"
    assert note["kept"] + note["omitted"] == total
    assert len(parsed["symbols"]) == note["kept"]
    assert "limit" in note["hint"], "说明里要给出恢复动作"
