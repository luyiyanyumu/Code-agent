"""功能导航（M33）：把项目拆成 能力域 → 功能 → 细分功能 → … → 原子步骤。

与第一版"入口清单"的区别（那版是错的）：入口清单只回答"对外暴露了什么"，
回答不了"这个项目能干什么、这件事又是怎么拆开的"。功能导航要有**层次**：

    能力域（code_agent/agent）
      └ 功能（路由决策：按需求挑提示词与工具）
          └ 细分功能（关键词打分）
          └ 细分功能（装载技能说明）
              └ …一直拆到没有下一层为止

三层分别从哪来（全部取自索引，不编）：

- **能力域** = 二级目录（`code_agent/agent`、`web/src/components`…），
  一句话说明取该目录里最像"总说明"的模块文档首句；
- **功能** = 域内"能被触发/被调用"的公开符号：路由、工具类、命令入口、前端钩子，
  以及**有 docstring 的公开函数/类**。功能名直接用作者写给人的那句话
  （`SessionRuntime.submit` → "提交一次需求并跑到停"）；没有 doc 就退回符号名，
  并且**把没有 doc 这件事标出来**——名字好不好读，本来就是这个仓库的质量指标；
- **细分功能** = 点开一个节点时才按它自己拆（`children()`）：
  - 类/组件 → 它的成员方法；方法再按它调用的东西继续拆；
  - 函数/方法/路由/钩子 → 它调用到的函数（**按源码里出现的顺序**，这就是流程）；
  - 拆不下去的 = **原子步骤**，这时直接说"它在「实现」页里有 N 步 / M 个分支"，
    而不是留一个空箭头让人反复点；
  - 走回自己 = 循环；在别处已经拆过 = 共用（数据流里的环与共享节点不该被复制成一片森林）。

证据口径不变：三态（`direct` / `file` / `none`），不把文件级说成函数级。
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Callable

from code_agent.codeintel import spec

#: 一个能力域最多列多少条功能（超了如实说"还有 n 条"）
MAX_FEATURES_PER_DOMAIN = 80
#: 最多列多少个能力域
#: 最多列多少个能力域节点（嵌套后是"目录节点总数"，不是顶层个数）。
#: 超了就停在这一层并如实标注——宁可少列，不能悄悄丢掉一整支。
MAX_DOMAINS = 300
#: 一次细分最多返回多少个子功能
MAX_CHILDREN = 60
#: 参与装配的符号上限
MAX_SYMBOLS = 20000
#: 每条功能最多挂几个测试文件
MAX_TESTS_PER_FEATURE = 5
#: 读测试文件做归属时的上限
MAX_TEST_FILES = 400

#: 能进功能树的符号类型（变量/常量不进：它们是数据，不是"能干什么"）
NAV_KINDS = frozenset(
    {"route", "hook", "component", "class", "interface", "enum", "record", "annotation", "function", "method", "constructor"}
)
#: 天生就是"功能"的类型（有它就够，不要求 docstring）
ENTRY_KINDS = frozenset({"route", "hook", "component"})
#: 可以继续往下拆的类型
CALLABLE_KINDS = frozenset({"route", "hook", "function", "method", "constructor"})
CONTAINER_KINDS = frozenset({"class", "interface", "enum", "record", "annotation", "component"})

#: 功能的来源标签：界面上说清"这条功能是怎么被触发的"
ENTRY_LABEL = {
    "route": "HTTP",
    "hook": "钩子",
    "component": "组件",
    "tool": "工具",
    "command": "命令",
    "container": "容器",
    "call": "调用",
    "member": "成员",
    "public": "公开",
}

_ROUTE_VERSION = re.compile(r"^v\d+$")
_HANDLER = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)")
#: 装配型函数：`mount_code_api` / `create_app` / `build_registry` 只是把东西挂起来，
#: 它不是一个"功能层次"。真机踩到：`/api/v1/code` 的 65 个路由全躲在 `mount_code_api`
#: 里，功能导航第一屏因此只剩 6 条——把装配层当透明的，功能才是功能。
_ASSEMBLY = re.compile(r"^(mount|create|build|register|setup|install|wire|make|attach)_?")
#: 域说明的来源优先级：包自己的 `__init__` 最像"这个域是干什么的"
_INIT_NAMES = ("__init__.py", "index.js", "index.ts", "mod.rs")


def handler_of(signature: str) -> str:
    """从签名里取处理函数名：路由的 `name` 被 URL 占了，函数名只剩这一处。"""
    match = _HANDLER.match(str(signature or "").strip())
    return match.group(1) if match else ""


def doc_label(doc: str, *, limit: int = 40) -> str:
    """docstring 首句当功能名：作者写给人的那句话，比符号名好读得多。

    `失败指纹：工具名 + 归一化参数` → `失败指纹`：冒号前那半句才是"这块叫什么"，
    后面是细节，留着会把导航栏撑成一整行。冒号前太短（<4 字）就不切，避免切出"见"这种。
    """
    text = " ".join(str(doc or "").split())
    if not text:
        return ""
    for stop in ("。", "；", ". ", "\n"):
        head = text.split(stop)[0]
        if head:
            text = head
            break
    for sep in ("：", ":"):
        if sep in text:
            head = text.split(sep)[0].strip()
            if len(head) >= 4:
                text = head
            break
    text = text.strip("。.；;：: ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _json_list(raw: Any) -> list:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _is_test_dir(path: str) -> bool:
    """目录本身是不是测试目录（`is_test_path` 认的是文件，这里补一层）。"""
    rel = str(path or "").strip("/")
    if not rel:
        return False
    return spec.is_test_path(f"{rel}/x.py")


def parent_chain(rel: str) -> list[str]:
    """文件路径 → 它的各级父目录，从顶层往下（末位就是它直接所在的目录）。

        `code_agent/agent/breaker.py` → ["code_agent", "code_agent/agent"]
        `code_agent/settings.py`      → ["code_agent"]
        `setup.py`                    → []          （根目录下的文件）

    为什么返回整条链而不是"前两段"：能力域要**跟着包的嵌套走**。
    旧版取前两段会让 `code_agent` 与 `code_agent/agent` 在同一屏并列，
    父包和子包看起来像兄弟——读的人得自己去猜它们的关系。
    """
    parts = [p for p in str(rel or "").split("/") if p]
    if len(parts) < 2:
        return []
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _read_lines(read_text: Callable[[str], str], rel: str) -> list[str]:
    """读测试文件的行（读不到不当成失败：证据算不出来 ≠ 功能不存在）。"""
    try:
        text = read_text(rel)
    except Exception:  # noqa: BLE001
        return []
    if len(text) > spec.MAX_FILE_BYTES:
        text = text[: spec.MAX_FILE_BYTES]
    return text.split("\n")


def protection_index(
    index: Any, *, read_text: Callable[[str], str]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """一次性算出"谁被测试守着"：返回 (符号 id → 测试文件, 文件 → 测试文件)。

    为什么一次算完：`spec.behaviors(target=...)` 每次都重扫全部符号 + 重读全部测试文件，
    逐条功能调用一次会退化成 O(功能数 × 仓库大小)。这里只扫一遍，整张图共用
    （服务层按索引 change_seq 缓存，见 `CodeIntelService._protection`）。

    路由要单独补处理函数名：路由符号的 `name` 被 URL 占了（`/api/v1/things/{id}`），
    而测试文件里写的是 `get_thing`——不补这一步，**所有路由都拿不到直接证据**。
    """
    rows = index.all_symbols(limit=spec.MAX_COVERAGE_SYMBOLS)
    tests = [row for row in rows if spec.is_test_symbol(row)]
    if not tests:
        return {}, {}
    names = spec.name_index(rows)
    for row in rows:
        if str(row.get("kind") or "") != "route":
            continue
        handler = handler_of(row.get("signature") or "")
        if handler:
            names.setdefault(handler, []).append(str(row["id"]))
    test_files = sorted({str(row["path"]) for row in tests})[:MAX_TEST_FILES]

    by_symbol: dict[str, set[str]] = {}
    by_file: dict[str, set[str]] = {}
    for rel in test_files:
        lines = _read_lines(read_text, rel)
        if not lines:
            continue
        text = "\n".join(lines)
        for sid in spec.mentioned_symbols(text, names):
            by_symbol.setdefault(str(sid), set()).add(rel)
        for target in spec.imported_files(index, rel):
            by_file.setdefault(str(target), set()).add(rel)
    return (
        {k: sorted(v) for k, v in by_symbol.items()},
        {k: sorted(v) for k, v in by_file.items()},
    )


def _evidence_for(
    symbol_id: str,
    rel: str,
    by_symbol: dict[str, list[str]],
    by_file: dict[str, list[str]],
) -> dict:
    """一条功能的证据，三态（与覆盖率口径一致，不把文件级说成函数级）。

    - `direct`：测试文件**按词边界提到**这个符号 → 能说"这条被测试盯着"；
    - `file`：只有"测试 import 了这个模块"这一层 → 只能说文件级，不能替它背书；
    - `none`：都没找到 → 如实标 0 测试。
    """
    direct = by_symbol.get(str(symbol_id)) or []
    if direct:
        return {"state": "direct", "tests": direct[:MAX_TESTS_PER_FEATURE], "note": ""}
    weak = by_file.get(str(rel).strip("/")) or []
    if weak:
        return {
            "state": "file",
            "tests": weak[:MAX_TESTS_PER_FEATURE],
            "note": "只有文件级证据（测试 import 了这个模块），不保证盯着这一条",
        }
    return {"state": "none", "tests": [], "note": "没有找到测试"}


def _base_node(row: dict, *, source: str, evidence: dict, briefs: dict[str, dict] | None = None) -> dict:
    """索引行 → 功能节点（界面只需要这一种形状，层数由 children 决定）。

    功能名按"作者优先"取：作者写了 docstring 就用作者的原话；没写才看有没有
    **模型补的描述**（`briefs`，按内容哈希校验过）；都没有就退回符号名，并如实标
    `has_doc=False`。`label_from` 说明这句话是谁写的——界面上必须能一眼分辨，
    绝不把模型补的和作者写混在一起。
    """
    kind = str(row.get("kind") or "function")
    name = str(row.get("name") or "")
    doc = str(row.get("doc") or "")
    doc_first = doc_label(doc)
    from code_agent.codeintel.business import business_for

    business = business_for(row, (briefs or {}).get(str(row.get("id") or "")))
    brief: dict | None = None
    if not doc_first and briefs:
        candidate = briefs.get(str(row.get("id") or ""))
        if candidate and _brief_is_current(candidate, row):
            brief = {
                "text": str(candidate.get("text") or ""),
                "model": str(candidate.get("model") or ""),
                "ts": str(candidate.get("ts") or ""),
                "source": business["source"],
                "status": business["status"],
            }
    if business["source"] == "human" and business["status"] == "current":
        label, label_from = doc_label(business["business_meaning"]) or name, "business"
    elif doc_first:
        label, label_from = doc_first, "doc"
    elif brief:
        label, label_from = doc_label(brief["text"]) or name, "brief"
    else:
        label, label_from = name, "name"
    return {
        "id": str(row.get("id") or ""),
        "kind": kind,
        "name": name,
        "label": label,
        "label_from": label_from,
        "has_doc": bool(doc.strip()),
        "brief": brief,
        "business": business,
        # 缺描述 = 作者没写、模型也没补：这就是"待补"清单的口径
        "brief_missing": not doc_first and brief is None and business["status"] != "current",
        "doc": doc[:240],
        "path": str(row.get("path") or ""),
        "line": int(row.get("line") or 0),
        "end_line": int(row.get("end_line") or 0),
        "signature": str(row.get("signature") or ""),
        "parent": str(row.get("parent") or ""),
        "source": source,
        "source_label": ENTRY_LABEL.get(source, source),
        "tests": evidence["tests"],
        "test_state": evidence["state"],
        "test_note": evidence["note"],
    }


def _brief_is_current(brief: dict, row: dict) -> bool:
    """这条描述还算数吗：符号的**声明与实现**都没变才算数。

    两个哈希都要对：`content_hash` 跟签名/声明走，`body_hash` 跟实现走。
    只比前者会漏掉最要紧的一种改动——"描述的是它干什么"，而干什么写在函数体里。
    代码一改哈希就变 → 旧描述自动失效（不是删掉，而是不再采用），
    这样"模型的猜测"永远不会盖在改过的代码上。
    """
    stored_c = str(brief.get("content_hash") or "")
    stored_b = str(brief.get("body_hash") or "")
    if not (stored_c or stored_b):
        return False
    current_c = str(row.get("content_hash") or "")
    current_b = str(row.get("body_hash") or "")
    if stored_c and stored_c != current_c:
        return False
    if stored_b and stored_b != current_b:
        return False
    return True


def _file_node(rel: str, evidence: dict, *, child_count: int | None = None) -> dict:
    """没有 `main` 的命令入口：功能挂在文件上（点开看这个文件里定义了哪些函数）。"""
    return {
        "id": f"file:{rel}",
        "kind": "command",
        "name": rel,
        "label": rel,
        "has_doc": False,
        "doc": "",
        "path": rel,
        "line": 1,
        "end_line": 0,
        "signature": "",
        "parent": "",
        "source": "command",
        "source_label": ENTRY_LABEL["command"],
        "child_count": child_count,
        "children_known": child_count is not None,
        "has_children": bool(child_count),
        "tests": evidence["tests"],
        "test_state": evidence["state"],
        "test_note": evidence["note"],
    }


def _members(index: Any, row: dict) -> list[dict]:
    """类的下一层：成员方法/属性（与 `children_of` 同一口径，含私有成员但排除双下划线）。"""
    members = [
        r
        for r in index.symbols_for(str(row.get("path") or ""))
        if str(r.get("parent") or "") == str(row["id"])
        and str(r.get("kind") or "") in ("method", "function", "property", "constructor", "field", "enum_member", "record_component", "class", "interface", "enum", "record", "annotation")
        and not str(r.get("name") or "").startswith("__")
    ]
    members.sort(key=lambda r: int(r.get("line") or 0))
    return members


def _resolve_calls(names: list[str], by_name: dict[str, list[str]]) -> list[str]:
    """调用名 → 符号 id（首次出现顺序；同名多处定义不认，宁可漏不可错）。

    与 `logic.function_logic` 里那段解析逐条对应（同一套"全局唯一名"规则）：
    不这样的话，界面上的 `▸` 和点开后的结果就不是同一件事。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        short = str(raw).split(".")[-1]
        if not short or short in seen:
            continue
        seen.add(short)
        ids = by_name.get(short) or []
        if len(ids) == 1 and ids[0] not in out:
            out.append(ids[0])
    return out


def call_children(
    index: Any, *, read_text: Callable[[str], str], symbol_ids: list[str]
) -> dict[str, list[str]]:
    """这批符号**实际能往下拆的**子节点 id（源码顺序；一个文件只解析一次）。

    为什么要事先算出来：界面上的 `▸` 是对用户的一句承诺——"这一层下面还有东西"。
    旧版对任何函数都乐观地画 `▸`，点开才发现是空的，于是"它是不是原子"必须先点一下
    才知道，这正是最不清晰的地方。这里一次把话说死：**有下一层才给 `▸`**。

    口径与 `logic.function_logic` 的"它调用了谁"**同源**：收集调用名直接复用
    `logic._steps_for_body`（同一份步骤规则，含分支/循环里的调用，嵌套函数定义内部不算），
    解析按"全局唯一名"。所以 `▸` 承诺的东西 == 点开后拿到的东西。

    类的下一层是成员（索引里已经有，不必读源码）。
    """
    from code_agent.codeintel import logic

    want = [str(s) for s in symbol_ids if s]
    if not want:
        return {}
    rows = {str(r["id"]): r for r in index.symbols_by_ids(want)}
    by_file: dict[str, list[dict]] = {}
    for sid in want:
        row = rows.get(sid)
        if row is not None:
            by_file.setdefault(str(row.get("path") or ""), []).append(row)

    by_name: dict[str, list[str]] = {}
    for other in index.all_symbols():
        name = str(other.get("name") or "")
        if name:
            by_name.setdefault(name, []).append(str(other["id"]))

    out: dict[str, list[str]] = {}
    for rel, targets in by_file.items():
        need_source = any(str(r.get("kind")) not in CONTAINER_KINDS for r in targets)
        tree = None
        lines: list[str] = []
        if need_source:
            try:
                text = read_text(rel)
                tree = ast.parse(text)
                lines = text.split("\n")
            except Exception:  # noqa: BLE001 —— 读不到/解析不了就当作"没有下一层"
                tree = None
        by_line: dict[int, ast.AST] = {}
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    by_line[int(getattr(node, "lineno", 0) or 0)] = node
        for row in targets:
            sid = str(row["id"])
            if str(row.get("kind")) in CONTAINER_KINDS:
                out[sid] = [str(m["id"]) for m in _members(index, row)]
                continue
            node = by_line.get(int(row.get("line") or 0))
            if node is None or isinstance(node, ast.ClassDef):
                out[sid] = []
                continue
            steps: list[dict] = []
            logic._steps_for_body(
                list(getattr(node, "body", []) or []),
                lines,
                depth=0,
                steps=steps,
                branch_counter=[0],
            )
            names = [n for step in steps for n in (step.get("calls") or [])]
            out[sid] = _resolve_calls(names, by_name)
    return out


class _Nav:
    """功能树的内存装配（符号 → 父子关系 → 顶层功能）。"""

    def __init__(self, index: Any, *, read_text: Callable[[str], str]) -> None:
        self.index = index
        self.rows = index.all_symbols(limit=MAX_SYMBOLS)
        self.by_id = {str(r["id"]): r for r in self.rows}
        self.kids: dict[str, list[dict]] = {}
        for row in self.rows:
            parent = str(row.get("parent") or "")
            if parent and parent in self.by_id:
                self.kids.setdefault(parent, []).append(row)
        self.members = index.query_all(
            "SELECT parent, COUNT(*) AS n FROM symbols WHERE parent != '' GROUP BY parent"
        )
        self.member_count = {str(r["parent"]): int(r["n"]) for r in self.members}
        self.tools = self._tool_classes()
        self._keep: dict[str, bool] = {}

    def _tool_classes(self) -> set[str]:
        out: set[str] = set()
        for row in self.rows:
            if str(row.get("kind")) != "class":
                continue
            bases = [str(b).split(".")[-1] for b in _json_list(row.get("bases"))]
            if any(b.endswith("Tool") for b in bases):
                out.add(str(row["id"]))
        return out

    def ancestors(self, row: dict) -> list[str]:
        out: list[str] = []
        cur = str(row.get("parent") or "")
        guard = 0
        while cur and cur in self.by_id and guard < 40:
            out.append(cur)
            cur = str(self.by_id[cur].get("parent") or "")
            guard += 1
        return out

    def eligible(self, row: dict) -> bool:
        """能不能出现在功能树里：公开、类型对、不是测试。"""
        name = str(row.get("name") or "")
        if not name or name.startswith("_"):
            return False
        if str(row.get("kind") or "") not in NAV_KINDS:
            return False
        return not spec.is_test_path(str(row.get("path") or ""))

    def is_feature(self, row: dict) -> bool:
        """这条东西本身就是一个"功能"。

        规则（按用户对这个界面的要求定的，不是按代码结构定的）：
        - 入口类（路由/钩子/组件/工具类）当然算；
        - **类/组件一律算**：一个类就是"一块能力"（`AgentLoop` = agent 循环），
          名字优先用 doc 首句，没 doc 就用类名并标「无说明」；
        - 普通函数/方法要有 docstring 才算——几百个无说明的私有辅助函数不是功能。
        """
        if not self.eligible(row):
            return False
        sid = str(row["id"])
        kind = str(row.get("kind") or "")
        if sid in self.tools or kind in ENTRY_KINDS or kind in CONTAINER_KINDS:
            return True
        return bool(str(row.get("doc") or "").strip())

    def transparent(self, row: dict) -> bool:
        """装配型容器：提到域上的那一层，不算功能层次。

        只对"无 doc 的公开装配函数"生效（`mount_code_api`、`create_app`…）：
        它们的作用就是"把下面这些东西挂起来"，把它当一层会让人多点一次鼠标。
        """
        if self.is_feature(row) or not self.eligible(row):
            return False
        if str(row.get("kind") or "") not in ("function", "method"):
            return False
        return bool(_ASSEMBLY.match(str(row.get("name") or "")))

    def keep(self, row: dict) -> bool:
        """它自己，或者它下面某个后代，是功能吗（自底向上算一次）。"""
        sid = str(row["id"])
        if sid in self._keep:
            return self._keep[sid]
        self._keep[sid] = False          # 环保护
        if not self.eligible(row):
            self._keep[sid] = False
            return False
        value = self.is_feature(row) or any(
            self.keep(child) for child in self.kids.get(sid, []) if self.eligible(child)
        )
        self._keep[sid] = value
        return value

    def is_top(self, row: dict) -> bool:
        """顶层功能：自己算功能，且**没有"算功能且不透明"的祖先**。"""
        if not self.keep(row) or self.transparent(row):
            return False
        for parent_id in self.ancestors(row):
            parent = self.by_id[parent_id]
            if self.keep(parent) and not self.transparent(parent):
                return False
        return True

    def child_count(self, row: dict) -> int | None:
        """已知的下层数量：类看**成员**（与 `children_of` 同一套过滤），其余要靠 AST。"""
        sid = str(row["id"])
        if str(row.get("kind")) in CONTAINER_KINDS:
            return len(_members(self.index, row))
        return None

    def node(
        self,
        row: dict,
        *,
        source: str,
        by_symbol: dict,
        by_file: dict,
        child_ids: list[str] | None = None,
        briefs: dict[str, dict] | None = None,
    ) -> dict:
        """一条功能节点。

        `child_ids` 是**事先算准**的下一层（`call_children`）。它决定界面上画 `▸` 还是
        `·`：只有真有下一层才给箭头，不再靠"函数大概能拆"这种猜测。
        """
        node = _base_node(
            row,
            source="tool" if str(row["id"]) in self.tools else source,
            evidence=_evidence_for(str(row["id"]), str(row.get("path") or ""), by_symbol, by_file),
            briefs=briefs,
        )
        if node["source"] == "tool":
            node["kind"] = "tool"
        count = len(child_ids) if child_ids is not None else self.child_count(row)
        node["child_count"] = count
        node["children_known"] = count is not None
        # 未知就**不给箭头**：宁可让人以为它是叶子，也不要许一个点开是空的承诺
        node["has_children"] = bool(count)
        return node


def _domain_docs(index: Any) -> dict[str, dict]:
    """每个能力域的一句话说明 + 来自哪个文件。

    为什么带来源：目录自己没有 docstring，能给的只是"这个目录里某个文件的开头那句"。
    优先取包自己的 `__init__.py`，没有就取目录里最大的那个文件的模块文档——
    并把它来自哪个文件一起给出，界面上才不会被当成"官方描述"。
    """
    best: dict[str, tuple[int, str, str]] = {}
    for row in index.query_all("SELECT path, module_doc, loc FROM files"):
        rel = str(row["path"] or "")
        text = " ".join(str(row["module_doc"] or "").split())
        if not rel or not text:
            continue
        chain = parent_chain(rel)
        if not chain:
            continue                      # 根目录下的文件没有"目录自我描述"可取
        domain = chain[-1]
        name = rel.rsplit("/", 1)[-1]
        score = (2 if name in _INIT_NAMES else 1) * 10_000_000 + int(row.get("loc") or 0)
        current = best.get(domain)
        if current is None or score > current[0]:
            best[domain] = (score, doc_label(text, limit=60), rel)
    return {d: {"doc": v[1], "doc_path": v[2]} for d, v in best.items()}


def _insert_feature(tree: dict, chain: list[str], feature: dict) -> None:
    """把一条功能挂到它所在目录的节点上（沿路补齐祖先目录）。

    `chain` 是**累计路径**（`code_agent` → `code_agent/agent`），树里的键却是**单段**
    （`code_agent` → `agent`）——两者混用会把路径拼成 `code_agent/code_agent/agent`。
    所以这里逐段取最后一段：路径的拼接只由 `_serialize_domain` 负责。

    同一个 id 只留一条：命令入口会覆盖它自己的公开函数口径，而不是多出一条重复项。
    """
    node = tree
    for full in chain:
        seg = full.rsplit("/", 1)[-1]
        node = node["children"].setdefault(seg, {"children": {}, "features": []})
    node["features"] = [f for f in node["features"] if f["id"] != feature["id"]]
    node["features"].append(feature)


def _serialize_domain(
    path: str,
    node: dict,
    docs: dict[str, dict],
    per_domain: int,
    stats: dict,
) -> dict | None:
    """目录节点 → 能力域（含子域），按包的真实层级嵌套。"""
    children: list[dict] = []
    for seg in sorted(node["children"]):
        child_path = f"{path}/{seg}" if path else seg
        built = _serialize_domain(child_path, node["children"][seg], docs, per_domain, stats)
        if built is not None:
            children.append(built)

    own = node["features"]
    # 顺序：入口类在前，然后按文件与行号（读起来就是代码里的顺序）
    own.sort(key=_feature_order)
    taken = own[: max(1, int(per_domain))]
    subtree = len(own) + sum(c["count"] for c in children)
    if not subtree:
        return None
    meta = docs.get(path) or {}
    stats["domains"] = stats.get("domains", 0) + 1
    stats["found"] = stats.get("found", 0) + len(own)
    payload = {
        "id": f"fdomain:{path or '根目录'}",
        "name": path or "根目录",
        "label": path.rsplit("/", 1)[-1] if path else "根目录",
        "doc": meta.get("doc", ""),
        "doc_path": meta.get("doc_path", ""),
        # count = 这一支下共有多少条功能（含子域），own = 直接挂在这个目录下的
        "count": subtree,
        "own": len(own),
        "shown": len(taken),
        "truncated": len(own) > len(taken),
        "direct_tests": len([n for n in own if n["test_state"] == "direct"]),
        "file_tests": len([n for n in own if n["test_state"] == "file"]),
        "features": taken,
        "children": children,
    }
    stats["budget"] = stats.get("budget", 0) - 1
    if stats["budget"] <= 0:
        # 目录节点太多：如实说"到这里为止"，而不是悄悄丢掉一整支
        payload["children"] = []
        payload["note"] = "能力域太多，这一支的更深层没有列出"
    return payload


def _feature_order(node: dict) -> tuple:
    """功能排序：入口类在前，然后按文件与行号（读起来就是代码里的顺序）。"""
    return (
        0 if node["source"] in ("route", "command", "tool", "hook", "component") else 1,
        node["path"],
        node["line"],
        node["label"],
    )


def build_nav(
    index: Any,
    *,
    read_text: Callable[[str], str],
    per_domain: int = MAX_FEATURES_PER_DOMAIN,
    max_domains: int = MAX_DOMAINS,
    by_symbol: dict[str, list[str]] | None = None,
    by_file: dict[str, list[str]] | None = None,
    briefs: dict[str, dict] | None = None,
) -> dict:
    """功能导航的第一屏：能力域（按包的嵌套层级）→ 功能。

    更深的功能层由 `children_of` 按需给。
    `briefs` 是"模型补的功能描述"（按内容哈希校验过的，见 `briefs.py`）：
    只补**作者没写 docstring**的符号，`label_from` 会如实标出这句话是谁写的。
    """
    if by_symbol is None or by_file is None:
        by_symbol, by_file = protection_index(index, read_text=read_text)
    nav = _Nav(index, read_text=read_text)
    docs = _domain_docs(index)

    tree: dict = {"children": {}, "features": []}
    sources: dict[str, int] = {}

    def attach(rel: str, node: dict) -> bool:
        chain = parent_chain(rel)
        if any(_is_test_dir(d) for d in chain):
            return False                  # 测试不是"项目能干什么"
        _insert_feature(tree, chain, node)
        sources[node["source"]] = sources.get(node["source"], 0) + 1
        return True

    # 1. 顶层功能：符号树里"自己算功能、祖先不算"的公开符号
    tops = [row for row in nav.rows if nav.is_top(row)]
    # **一次算准"谁还有下一层"**：界面上的箭头是承诺，不能靠猜（见 call_children）
    child_map = call_children(
        index, read_text=read_text, symbol_ids=[str(row["id"]) for row in tops]
    )
    for row in tops:
        source = "public"
        if str(row["id"]) in nav.tools:
            source = "tool"
        elif str(row.get("kind")) in ENTRY_KINDS:
            source = str(row.get("kind"))
        elif not nav.is_feature(row):
            source = "container"
        attach(
            str(row.get("path") or ""),
            nav.node(
                row,
                source=source,
                by_symbol=by_symbol,
                by_file=by_file,
                child_ids=child_map.get(str(row["id"])),
                briefs=briefs,
            ),
        )

    # 2. 命令入口：`__main__` 文件也是"能干什么"（它不在符号树里）
    for row in index.query_all("SELECT path FROM files WHERE entrypoint=1 ORDER BY path"):
        rel = str(row["path"])
        entry = None
        for sym in index.symbols_for(rel):
            if str(sym.get("kind")) == "function" and str(sym.get("name")) == "main":
                entry = sym
                break
        if entry is not None:
            node = nav.node(
                entry,
                source="command",
                by_symbol=by_symbol,
                by_file=by_file,
                child_ids=child_map.get(str(entry["id"])),
                briefs=briefs,
            )
        else:
            top_level = [
                r
                for r in index.symbols_for(rel)
                if not str(r.get("parent") or "")
                and str(r.get("kind")) in CALLABLE_KINDS | CONTAINER_KINDS
            ]
            node = _file_node(
                rel,
                _evidence_for(f"file:{rel}", rel, by_symbol, by_file),
                child_count=len(top_level),
            )
        attach(rel, node)

    stats: dict = {"budget": int(max_domains), "domains": 0, "found": 0}
    domains: list[dict] = []
    for seg in sorted(tree["children"]):
        built = _serialize_domain(seg, tree["children"][seg], docs, per_domain, stats)
        if built is not None:
            domains.append(built)
    root_own = tree["features"]
    if root_own:
        root_own.sort(key=_feature_order)
        taken = root_own[: max(1, int(per_domain))]
        stats["domains"] += 1
        stats["found"] += len(root_own)
        domains.append(
            {
                "id": "fdomain:根目录",
                "name": "根目录",
                "label": "根目录",
                "doc": "",
                "doc_path": "",
                "count": len(root_own),
                "own": len(root_own),
                "shown": len(taken),
                "truncated": len(root_own) > len(taken),
                "direct_tests": len([n for n in root_own if n["test_state"] == "direct"]),
                "file_tests": len([n for n in root_own if n["test_state"] == "file"]),
                "features": taken,
                "children": [],
            }
        )

    def walk(node: dict, into: list[dict]) -> None:
        into.extend(node["features"])
        for child in node["children"]:
            walk(child, into)

    flat: list[dict] = []
    for domain in domains:
        walk(domain, flat)
    return {
        "source": "auto",
        "domains": domains,
        "totals": {
            "domains": stats["domains"],
            "features": len(flat),
            "found": stats["found"],
            "direct_tests": len([n for n in flat if n["test_state"] == "direct"]),
            "file_tests": len([n for n in flat if n["test_state"] == "file"]),
            "no_tests": len([n for n in flat if n["test_state"] == "none"]),
            "sources": sources,
        },
        "truncated": stats["found"] > len(flat),
        "method": (
            "能力域按**包的嵌套层级**（code_agent → agent/api/codeintel…，跟着真实目录走）；"
            "功能 = 入口（路由/命令/工具/钩子）+ 有 docstring 的公开符号 + 类，"
            "功能名优先用 docstring 首句；细分功能按「类成员 + 它调用的函数（源码顺序）」逐层拆开"
        ),
        "note": (
            "点一个功能就往下一层拆，拆到没有下层为止；"
            "没找到测试的功能标 0 测试，没写 docstring 的功能标「无说明」——缺证据、缺说明都是信息"
        ),
    }


def children_of(
    index: Any,
    *,
    read_text: Callable[[str], str],
    node_id: str,
    visited: tuple[str, ...] = (),
    limit: int = MAX_CHILDREN,
    by_symbol: dict[str, list[str]] | None = None,
    by_file: dict[str, list[str]] | None = None,
    briefs: dict[str, dict] | None = None,
) -> dict:
    """一个节点的细分功能（点开才调用，所以可以一直拆到原子步骤）。

    `visited` 是当前路径上的祖先：出现在里面的子节点标 `cyclic`，不再展开——
    静态调用图里有环（`submit → loop → submit`），不挡的话前端会无限拆下去。
    """
    from code_agent.codeintel import logic

    sid = str(node_id or "")
    by_symbol = by_symbol or {}
    by_file = by_file or {}
    ancestors = set(str(v) for v in visited)

    if sid.startswith("file:"):
        rel = sid[len("file:") :]
        rows = [
            r
            for r in index.symbols_for(rel)
            if not str(r.get("parent") or "")
            and str(r.get("kind")) in CALLABLE_KINDS | CONTAINER_KINDS
        ]
        nodes = _with_exact_children(
            index,
            read_text=read_text,
            rows=rows,
            source="call",
            by_symbol=by_symbol,
            by_file=by_file,
            briefs=briefs,
        )
        return _payload(sid, nodes[:limit], kind="command", visited=ancestors, total=len(nodes))

    row = index.symbol(sid)
    if row is None:
        raise ValueError(f"符号不存在：{sid}")
    kind = str(row.get("kind") or "")

    if kind in CONTAINER_KINDS:
        members = _members(index, row)
        nodes = _with_exact_children(
            index,
            read_text=read_text,
            rows=members,
            source="member",
            by_symbol=by_symbol,
            by_file=by_file,
            briefs=briefs,
        )
        return _payload(sid, nodes[:limit], kind=kind, visited=ancestors, total=len(nodes))

    if kind in CALLABLE_KINDS:
        # 孩子从**同一个来源**算（call_children）：界面上的箭头就是按它画的，
        # 点开拿到的必须是同一份，否则箭头又开始骗人
        child_ids = call_children(index, read_text=read_text, symbol_ids=[sid]).get(sid, [])
        child_rows = [r for r in index.symbols_by_ids(child_ids) if r is not None]
        order = {cid: i for i, cid in enumerate(child_ids)}
        child_rows.sort(key=lambda r: order.get(str(r["id"]), 999))
        nodes = _with_exact_children(
            index,
            read_text=read_text,
            rows=child_rows,
            source="call",
            by_symbol=by_symbol,
            by_file=by_file,
            briefs=briefs,
        )
        payload = _payload(sid, nodes[:limit], kind=kind, visited=ancestors, total=len(nodes))
        if not nodes:
            # 到头了才需要解释"为什么到头了"（顺带给出它的步骤数）——省一次解析
            info = logic.function_logic(index, read_text=read_text, symbol_id=sid)
            counts = (info.get("counts") or {}) if info.get("available") else {}
            payload["summary"] = {
                "steps": counts.get("steps", 0),
                "branches": counts.get("branches", 0),
                "loops": counts.get("loops", 0),
                "returns": counts.get("returns", 0),
                "raises": counts.get("raises", 0),
                "calls": 0,
            }
            if counts.get("steps"):
                payload["note"] = (
                    f"到这一层已经是原子步骤了：它自己做了 {counts.get('steps', 0)} 步"
                    f"（分支 {counts.get('branches', 0)} · 返回 {counts.get('returns', 0)}），"
                    "「实现」页里按源码顺序列着每一步"
                )
        return payload

    return _payload(
        sid, [], kind=kind, visited=ancestors, total=0,
        reason=f"它是 {kind}——没有更下一层的功能（数据不是功能）",
    )


def _with_exact_children(
    index: Any,
    *,
    read_text: Callable[[str], str],
    rows: list[dict],
    source: str,
    by_symbol: dict[str, list[str]],
    by_file: dict[str, list[str]],
    briefs: dict[str, dict] | None = None,
) -> list[dict]:
    """把一行行符号变成节点，并**事先算准**它们的下一层（`▸` 才不会骗人）。

    一次把整层的"有没有下一层"算完：点开一层时顺手把儿子们也标上，
    否则第二层又会退回"点了才知道"。
    """
    ids = [str(r["id"]) for r in rows]
    counts = call_children(index, read_text=read_text, symbol_ids=ids)
    nodes: list[dict] = []
    for row in rows:
        sid = str(row["id"])
        kids = counts.get(sid)
        node = {
            **_base_node(
                row,
                source=source,
                evidence=_evidence_for(sid, str(row.get("path") or ""), by_symbol, by_file),
                briefs=briefs,
            ),
            "child_count": len(kids) if kids is not None else None,
            "children_known": kids is not None,
            "has_children": bool(kids),
        }
        nodes.append(node)
    return nodes


def _payload(
    node_id: str,
    nodes: list[dict],
    *,
    kind: str,
    visited: set[str],
    total: int,
    reason: str = "",
) -> dict:
    """细分功能的返回：孩子 + 一句话说明"为什么到此为止"。"""
    leaf = not nodes
    note = reason
    if leaf and not note:
        note = "到这一层已经是原子步骤了：「实现」页里列出它每一步做了什么（按源码顺序）"
    for node in nodes:
        if node.get("id") in visited:
            # 环上出现过的节点不再展开：箭头收掉，否则用户会顺着环点下去
            node["cyclic"] = True
            node["has_children"] = False
            node["child_count"] = 0
    return {
        "id": node_id,
        "kind": kind,
        "children": nodes,
        "leaf": leaf,
        "note": note,
        "total": total,
        "truncated": total > len(nodes),
    }
