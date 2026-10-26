"""代码模式 P8：实现逻辑抽取（步骤 + 调用关系）。

用户反馈"地图只有框架、没有实现逻辑"——这个模块补的就是那一段。测试盯住：

1. **步骤是真的按顺序**，嵌套层级保留（if 体内的语句要更靠内，不能拍平）；
2. **调用关系能解析到索引里的符号**（拼出调用链），解析不了的要如实列在 unresolved；
3. **只对 Python 做**：JS/Vue 必须返回 available=False + 原因，而不是编一个漂亮的假流程图；
4. **不是运行时调用栈**：载荷里自带这句话，避免读者误读。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from code_agent.codeintel import logic
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


SERVICE_SRC = '''"""示例业务。"""

from pkg.helper import normalize


def _private_step(value):
    return value


def fetch_user(user_id):
    """取用户：先校验，再查缓存，最后回源。"""
    if not user_id:
        raise ValueError("user_id 必填")
    cached = _private_step(user_id)
    for attempt in range(3):
        row = lookup(cached)
        if row is not None:
            return normalize(row)
    return None


def lookup(user_id):
    return {"id": user_id}
'''

HELPER_SRC = '''"""工具。"""


def normalize(row):
    return dict(row)
'''

WEB_SRC = """export function request(method, path) {
  const headers = {}
  if (method === 'POST') headers['Content-Type'] = 'application/json'
  return fetch(path, { method, headers })
}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "service.py").write_text(SERVICE_SRC, encoding="utf-8")
    (root / "pkg" / "helper.py").write_text(HELPER_SRC, encoding="utf-8")
    (root / "web").mkdir()
    (root / "web" / "api.js").write_text(WEB_SRC, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {"id": "p_p8", "name": "proj", "root_path": str(repo),
           "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin"}
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    return {"row": row, "intel": intel, "repo": repo}


def test_steps_keep_order_and_nesting(env):
    out = env["intel"].function_logic(env["row"], "sym:pkg/service.py#fetch_user")
    assert out["available"] is True
    kinds = [s["kind"] for s in out["steps"]]
    # 文档串 → 条件 → 抛错 → 赋值 → 循环 → 循环体…
    assert kinds[1] == "if" and kinds[2] == "raise"
    assert "for" in kinds and kinds.index("if") < kinds.index("for")
    body = [s for s in out["steps"] if s["depth"] > 1]
    assert body, "循环体/条件体应该有更深的缩进"
    assert all(s["depth"] <= logic.MAX_DEPTH for s in out["steps"])
    # 每步都能定位到源码行
    assert all(s["line"] > 0 for s in out["steps"])
    assert out["counts"]["loops"] == 1 and out["counts"]["raises"] == 1
    assert out["counts"]["returns"] == 2


def test_calls_resolve_to_index_symbols_and_chain(env):
    """它调用了谁：解析到索引里的符号（调用链），并保留无法解析的名字。"""
    out = env["intel"].function_logic(env["row"], "sym:pkg/service.py#fetch_user")
    names = {c["name"]: c for c in out["calls"]}
    assert "normalize" in names and names["normalize"]["path"] == "pkg/helper.py"
    assert "lookup" in names and names["lookup"]["path"] == "pkg/service.py"
    # 私有函数也参与「它调用了谁」的解析（调用链不该因为下划线就断）
    assert "_private_step" in names
    assert out["unresolved_calls"], "内置/方法调用要如实列为未解析"
    assert "range" in out["unresolved_calls"]
    assert out["not_runtime_trace"] is True
    assert "静态步骤" in out["note"]


def test_callers_come_from_reference_edges(env):
    out = env["intel"].function_logic(env["row"], "sym:pkg/service.py#lookup")
    assert out["available"] is True
    assert out["callers"], "索引里的引用边应该能给出调用方文件"
    assert all(c["confidence"] in ("medium", "high") for c in out["callers"])


def test_non_python_is_brace_scanned_and_labelled(env):
    """JS/Vue 现在走"按行 + 括号层级"的启发式（P11），但必须自报家门。

    这条曾经断言 JS 一律 available=False——P11 之后那不再是真的：前端 617 个可调用
    符号以前在「实现」页全是空的。现在要求变成"给出步骤 + 明确标注不是 AST"。
    """
    out = env["intel"].function_logic(env["row"], "sym:web/api.js#request")
    assert out["available"] is True
    assert out["parser"] == "brace-scan"
    assert out["confidence"] == "medium"
    assert "非 AST" in out["basis"] and "启发式" in out["note"]
    assert out["steps"], "应该扫出语句"


def test_language_without_any_scanner_degrades(env, tmp_path):
    """既不是 Python 也不是大括号语言 → 如实不可用，并说清原因。"""
    from code_agent.codeintel import logic_brace

    assert logic_brace.supports("pkg/thing.rb") is False
    out = logic_brace.brace_logic(
        env["intel"].index("p_p8"),
        read_text=lambda rel: "",
        symbol_id="sym:pkg/service.py#fetch_user",
    )
    # Python 不在大括号语言集合里，brace_logic 自己也要拒绝（避免被误调用）
    assert out["available"] is False
    assert "没有可用的步骤解析" in out["reason"]


def test_missing_or_broken_target_is_explicit(env):
    with pytest.raises(logic.LogicError):
        env["intel"].function_logic(env["row"], "sym:pkg/service.py#nope")

    # 符号存在但文件语法坏了：如实说"当前不是合法 Python"，而不是抛栈
    (env["repo"] / "pkg" / "service.py").write_text("def broken(:\n", encoding="utf-8")
    out = env["intel"].function_logic(env["row"], "sym:pkg/service.py#fetch_user")
    assert out["available"] is False and "合法 Python" in out["reason"]


def test_steps_are_capped(env, tmp_path, agent_home, monkeypatch):
    """几千行的巨型函数不能把响应撑爆：只给前 N 步并标注截断。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    root = tmp_path / "big"
    (root / "pkg").mkdir(parents=True)
    body = ["def huge():", "    total = 0"]
    for i in range(logic.MAX_STEPS + 50):
        body.append(f"    total += {i}")
    body.append("    return total")
    (root / "pkg" / "big.py").write_text("\n".join(body) + "\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    row = {"id": "p_big8", "name": "big", "root_path": str(root),
           "root_key": str(root).lower(), "user_id": "u"}
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    out = intel.function_logic(row, "sym:pkg/big.py#huge")
    assert out["available"] is True
    assert len(out["steps"]) == logic.MAX_STEPS
    assert out["truncated"] is True


def test_api_endpoint_returns_steps(env):
    """走 API 也要能拿到（界面点节点就调这个）。"""
    from fastapi.testclient import TestClient

    from code_agent.api.app import create_app

    client = TestClient(create_app(provider_name="offline"))
    with client:
        token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        pid = client.post(
            "/api/v1/code/projects", json={"mode": "open", "path": str(env["repo"])}, headers=headers
        ).json()["id"]
        client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=headers)

        body = client.get(
            f"/api/v1/code/projects/{pid}/symbol/logic",
            params={"id": "sym:pkg/service.py#fetch_user"},
            headers=headers,
        ).json()
        assert body["available"] is True and body["counts"]["loops"] == 1
        assert any(c["name"] == "lookup" for c in body["calls"])

        # 缺 id → 422；不存在的符号 → 422（领域错误，不是 500）
        assert client.get(
            f"/api/v1/code/projects/{pid}/symbol/logic", headers=headers
        ).status_code == 422
        assert client.get(
            f"/api/v1/code/projects/{pid}/symbol/logic",
            params={"id": "sym:pkg/service.py#nope"},
            headers=headers,
        ).status_code == 422
