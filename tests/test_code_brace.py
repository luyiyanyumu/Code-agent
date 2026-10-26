"""P11：非 Python（大括号语言 / Vue）的语句级实现步骤。

本仓 617 个可调用符号在 .vue/.js 里——以前它们在「实现」页一律是"只有 Python 有
语句级解析"。这个模块用"按行 + 花括号层级 + 行首关键字"的启发式补上，测试盯住：

1. **层级要对**：if 体内的语句 depth 必须比 if 大；
2. **字符串与注释里的花括号不能干扰层级**（`const s = "{ }"` / `// }`）；
3. **多行表达式不被切碎**（跨行的调用链只算一条语句）；
4. **Vue 用 .vue 的绝对行号**（`<script setup>` 段里的函数，行号要对得上文件）；
5. **必须自报家门**：confidence=medium + note/basis 写清是启发式，不是 AST。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from code_agent.codeintel import logic
from code_agent.codeintel import logic_brace
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


SERVICE_JS = """// 前端服务层：这里的花括号只用来测试"字符串里的括号不算层级"
import { helper } from './helper.js'

export function request(method, path) {
  const headers = { 'X-Trick': '}{ not a brace }' }   // 注释里的 } 也不算
  const text = `template ${method} braces: { }`
  if (!path) {
    throw new Error('path required')
  }
  for (const attempt of [1, 2, 3]) {
    const row = helper(path,
      text,
      headers)
    if (row && row.ok) {
      return row
    }
  }
  try {
    return helper.fallback(text)
  } catch (err) {
    console.log(err)
  }
  return null
}
"""

HELPER_JS = """export function helper(target, text, headers) {
  return { ok: !!target }
}
"""

APP_VUE = """<template>
  <div>{{ label }}</div>
</template>

<script setup>
import { ref } from 'vue'

const label = ref('x')

function bump(step) {
  if (step > 0) {
    label.value = step
  } else {
    label.value = 0
  }
  return label.value
}
</script>
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "service.js").write_text(SERVICE_JS, encoding="utf-8")
    (root / "src" / "helper.js").write_text(HELPER_JS, encoding="utf-8")
    (root / "src" / "App.vue").write_text(APP_VUE, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {"id": "p_p11", "name": "proj", "root_path": str(repo),
           "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin"}
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    return {"row": row, "intel": intel, "repo": repo}


def _steps(env, sid: str) -> dict:
    return env["intel"].function_logic(env["row"], sid)


def test_supports_brace_languages():
    for rel in ("a/b.js", "a/b.ts", "a/b.vue", "a/b.java", "a/b.go"):
        assert logic_brace.supports(rel), rel
    for rel in ("a/b.py", "a/b.rb", "a/b.md"):
        assert not logic_brace.supports(rel), rel


def test_js_steps_order_and_nesting(env):
    out = _steps(env, "sym:src/service.js#request")
    assert out["available"] is True
    assert out["parser"] == "brace-scan"
    kinds = [s["kind"] for s in out["steps"]]
    # 赋值 → 条件 → 抛错 → 循环 → 赋值/返回 → try → 调用 → 返回
    assert kinds[0] == "assign", kinds
    assert "if" in kinds and "raise" in kinds and "for" in kinds and "try" in kinds
    assert kinds.count("return") >= 2, kinds
    depth_if = [s["depth"] for s in out["steps"] if s["kind"] == "if"]
    depth_throw = [s["depth"] for s in out["steps"] if s["kind"] == "raise"]
    assert depth_throw and depth_if and depth_throw[0] > depth_if[0], "if 体内的语句层级要更深"


def test_strings_and_comments_do_not_break_brace_depth(env):
    """`'{'`、`// }`、模板串里的 `{ }` 都不能影响层级——否则层级会越算越乱。"""
    out = _steps(env, "sym:src/service.js#request")
    depths = [s["depth"] for s in out["steps"]]
    # 真实结构最深是"for 里的 if 里的 return" = 2（函数体第一层为 0）；
    # 字符串/注释若被算进层级会明显更深
    assert max(depths) == 2, f"层级被字符串/注释带偏了：{depths}"
    assert min(depths) >= 0
    # 最后一条 return 应该回到函数体顶层（depth 0）
    assert out["steps"][-1]["kind"] == "return"
    assert out["steps"][-1]["depth"] == 0


def test_multiline_expression_is_one_step(env):
    """跨行的 `helper(path,\\n text,\\n headers)` 只能算一条语句。"""
    out = _steps(env, "sym:src/service.js#request")
    helper_steps = [s for s in out["steps"] if "helper(path" in s["text"]]
    assert len(helper_steps) == 1
    # 续行的 `text,` / `headers)` 不能被当成语句
    assert all("headers)" not in s["text"] for s in out["steps"])


def test_chain_continuation_merges_into_previous_step(env, tmp_path: Path):
    """链式调用 / 多行运算符的续行要并进上一条，不能一行一条步骤。"""
    path = env["repo"] / "src" / "chain.js"
    path.write_text(
        "export function build(md) {\n"
        "  let html = md\n"
        "    .replace(/a/g, 'A')\n"
        "    .replace(/b/g, 'B')\n"
        "    .replace(/c/g, 'C')\n"
        "  return html\n"
        "}\n",
        encoding="utf-8",
    )
    asyncio.run(env["intel"].scan(env["row"], full=True))
    out = _steps(env, "sym:src/chain.js#build")
    kinds = [s["kind"] for s in out["steps"]]
    assert kinds == ["assign", "return"], kinds
    assert ".replace(/c/g, 'C')" in out["steps"][0]["text"], out["steps"][0]["text"]


def test_calls_resolved_to_index_symbols(env):
    out = _steps(env, "sym:src/service.js#request")
    names = [c["name"] for c in out["calls"]]
    assert "helper" in names
    hit = next(c for c in out["calls"] if c["name"] == "helper")
    assert hit["path"] == "src/helper.js"
    assert "fallback" in out["unresolved_calls"]


def test_vue_function_uses_absolute_file_lines(env):
    """Vue 的符号行号是 .vue 的绝对行号：实现页跳过去要落在正确的行上。"""
    rows = env["intel"].index("p_p11").query_all(
        "SELECT id, line FROM symbols WHERE path='src/App.vue' AND name='bump'"
    )
    assert rows, "Vue 里的函数没有被索引"
    out = _steps(env, rows[0]["id"])
    assert out["available"] is True
    assert out["path"] == "src/App.vue"
    assert out["steps"], "Vue 里的函数也应该有步骤"
    # 步骤行号必须落在文件范围内，且第 1 步是 if（第 2 步是 else）
    total = len(APP_VUE.split("\n"))
    assert all(1 <= s["line"] <= total for s in out["steps"])
    kinds = [s["kind"] for s in out["steps"]]
    assert kinds[0] == "if", kinds
    assert "else" in kinds, kinds


def test_python_still_uses_ast(env, tmp_path: Path):
    """Python 那条路径不能被这次改动影响。"""
    path = env["repo"] / "src" / "py_mod.py"
    path.write_text("def add(a, b):\n    if a:\n        return b\n    return a + b\n", encoding="utf-8")
    asyncio.run(env["intel"].scan(env["row"], full=True))
    out = _steps(env, "sym:src/py_mod.py#add")
    assert out["available"] is True
    assert "parser" not in out  # Python 路径不标 brace-scan
    assert [s["kind"] for s in out["steps"]][0] == "if"


def test_non_brace_language_degrades(env):
    out = logic_brace.supports("src/thing.rb")
    assert out is False


def test_confidence_and_disclosure(env):
    """启发式必须自报家门：medium + note + basis，否则读者会当成精确解析。"""
    out = _steps(env, "sym:src/service.js#request")
    assert out["confidence"] == "medium"
    assert "启发式" in out["note"] or "扫描" in out["note"]
    assert "非 AST" in out["basis"]
    assert "_depth_of" not in json_dumps(out)


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
