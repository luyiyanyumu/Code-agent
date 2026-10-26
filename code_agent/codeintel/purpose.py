"""符号的"作用说明"（P10）：回答"这个东西是干什么用的"。

为什么需要：地图和详情抽屉能回答"它是什么结构""改它会牵动谁""谁改过"，但**没有一句话
说它是干什么的**——点开 `logger` 只看到 `logger = logging.getLogger(__name__)` 和 5 个
引用文件，读者还得自己翻译。这个模块把"意义与作用"补齐。

产品纪律（与 `impact` / `logic` 一致，也是 archify 的 Truth before spectacle）：
**不发明语义**。输出只允许来自三类事实，且每条都能追到依据：

1. **代码里写着的**：赋值表达式、类型注解、签名、参数、返回值、docstring、装饰器、基类、
   `assert` / `raise` 这类契约；
2. **索引里量出来的**：同文件读写次数、跨文件真实使用点（候选文件里做词边界文本匹配）、
   被调用点、成员数、文件被导入次数；
3. 由 1/2 按固定模板拼出的自然语言（`headline` / `facts`）——**模板里没有任何模型想象**。

名字上的推断（`get_*` → 读取、`*_cache` → 缓存）只做"命名约定级别的翻译"，并且会在
`notes` 里标明是命名约定而不是行为证据。拿不准的（非 Python、解析失败、名字撞车）如实
降级并在 `reason` / `notes` 里说清楚。
"""

from __future__ import annotations

import ast
import html
import re
from typing import Any, Callable

#: 最多返回多少条事实（界面是一屏抽屉，太多没人看）
MAX_FACTS = 14
#: 跨文件使用点扫描的上限：候选文件数 / 单文件大小。候选集 = 导入了本模块的文件，
#: 所以不会退化成全仓扫描（和 logic.py 的调用方扫描同一套收敛思路）。
MAX_CANDIDATES = 20
MAX_CANDIDATE_BYTES = 512 * 1024
MAX_DOCUMENTATION_CHARS = 6_000

ROLE_LABEL = {
    "variable": "变量",
    "constant": "常量",
    "function": "函数",
    "method": "方法",
    "class": "类",
    "interface": "接口",
    "type": "类型",
    "enum": "枚举",
    "property": "属性",
    "component": "组件",
    "route": "接口端点",
    "hook": "钩子",
    "module": "模块",
}

#: 名字前缀 → 动作语义。只翻译"动作"，不承诺行为。
NAME_PREFIX = (
    ("get_", "读取"), ("fetch_", "拉取"), ("load_", "加载"), ("read_", "读取"),
    ("set_", "设置"), ("update_", "更新"), ("save_", "保存"), ("write_", "写入"),
    ("add_", "添加"), ("remove_", "移除"), ("delete_", "删除"), ("clear_", "清空"),
    ("is_", "判断是否"), ("has_", "判断是否存在"), ("can_", "判断能否"), ("should_", "判断是否应该"),
    ("to_", "转换成"), ("from_", "从输入构造"), ("parse_", "解析"), ("format_", "格式化"),
    ("build_", "构造"), ("make_", "创建"), ("create_", "创建"), ("run_", "执行"),
    ("start_", "启动"), ("stop_", "停止"), ("close_", "关闭"), ("init_", "初始化"),
    ("reset_", "重置"), ("handle_", "处理"), ("on_", "响应事件"), ("render_", "渲染"),
    ("draw_", "绘制"), ("compute_", "计算"), ("calc_", "计算"), ("check_", "校验"),
    ("validate_", "校验"), ("filter_", "过滤"), ("find_", "查找"), ("search_", "搜索"),
    ("list_", "列出"), ("count_", "统计"), ("apply_", "应用"), ("merge_", "合并"),
    ("send_", "发送"), ("emit_", "发出"), ("notify_", "通知"), ("log_", "记录日志"),
    ("scan_", "扫描"), ("walk_", "遍历"), ("index_", "建立索引"), ("resolve_", "解析"),
    ("ensure_", "确保（没有就建）"), ("refresh_", "刷新"), ("sync_", "同步"),
)

#: 名字里的角色词 → 它大概率是什么（只用于"作用"的自然语言，不用于任何判定）。
#: 匹配规则：**只看名字的最后一个词**（`session_store` 是"存储"，不是"会话"），
#: 凭据类例外——出现在任意位置都提醒，除非尾巴是计量词（`token_budget` 不是凭据）。
NAME_HINTS = (
    ("logger", "日志记录器"),
    ("log", "日志"),
    ("router", "路由集合"),
    ("cache", "缓存"),
    ("lock", "并发锁"),
    ("mutex", "并发锁"),
    ("conn", "连接"),
    ("connection", "连接"),
    ("pool", "连接 / 对象池"),
    ("session", "会话"),
    ("config", "配置"),
    ("settings", "配置项"),
    ("client", "客户端"),
    ("store", "存储 / 仓储"),
    ("repo", "仓储"),
    ("repository", "仓储"),
    ("registry", "注册表"),
    ("manager", "管理器"),
    ("handler", "处理器"),
    ("middleware", "中间件"),
    ("provider", "供应商适配"),
    ("state", "状态"),
    ("queue", "队列"),
    ("retry", "重试"),
    ("timeout", "超时"),
    ("limit", "上限"),
    ("budget", "预算上限"),
    ("default", "默认值"),
    ("snapshot", "快照"),
    ("index", "索引"),
    ("path", "路径"),
    ("dir", "目录"),
    ("directory", "目录"),
    ("file", "文件名"),
    ("root", "根目录"),
    ("mode", "模式"),
    ("flag", "开关"),
    ("counter", "计数"),
    ("count", "计数"),
    ("round", "轮次"),
    ("rounds", "轮次"),
    ("prompt", "提示词"),
    ("messages", "消息列表"),
    ("temp", "临时数据"),
    ("tmp", "临时数据"),
)

#: 尾巴是这些词时不提醒"凭据"：`token_budget` / `secret_count` 都不是凭据本身
_MEASURE_TAIL = {
    "budget", "limit", "count", "num", "len", "length", "chars", "size", "cap",
    "remaining", "rate", "usage", "total", "max", "min", "index", "flag",
}

_CREDENTIAL_WORDS = {"token", "apikey", "secret", "password", "passwd", "credential", "credentials"}

#: 构造类调用 → 它造出来的是什么（只列常识性、无歧义的）
CTOR_HINTS = {
    "getLogger": "日志记录器",
    "Logger": "日志记录器",
    "Lock": "线程锁",
    "RLock": "可重入锁",
    "Semaphore": "信号量",
    "Queue": "队列",
    "LifoQueue": "后进先出队列",
    "PriorityQueue": "优先队列",
    "defaultdict": "带默认值的字典",
    "OrderedDict": "有序字典",
    "Counter": "计数器",
    "deque": "双端队列",
    "Path": "文件系统路径",
    "PurePath": "路径",
    "ThreadPoolExecutor": "线程池",
    "ProcessPoolExecutor": "进程池",
    "Session": "会话",
    "AsyncSession": "异步会话",
    "Client": "客户端",
    "AsyncClient": "异步客户端",
    "Connection": "连接",
    "Engine": "引擎（连接池）",
    "Template": "模板",
    "Agent": "智能体实例",
    "set": "集合",
    "frozenset": "不可变集合",
    "dict": "字典",
    "list": "列表",
    "tuple": "元组",
    "namedtuple": "具名元组",
    "OrderedDict2": "有序字典",
}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=")


def _mention_pattern(name: str) -> re.Pattern[str]:
    r"""跨文件"用到这个名字"的匹配：裸名字 **或** `模块.名字`。

    为什么要第二种：模块级常量在别的文件里几乎总是写成 `config.TOKEN_BUDGET`，
    只认裸名字会一律报"没人在用"。同时要排除 `self.x` / `cls.x`——那是实例属性，
    不是这个模块级符号（`self.logger` 出现在别的文件里是最典型的假阳性）。

    正则里 `(?<![\w])` + `(?!self\b)` 这一对就是干这个用的：`self.NAME` 里 's' 处
    被 lookahead 挡掉，'e' 处又被 lookbehind 挡掉，于是整段不匹配。
    """
    esc = re.escape(name)
    return re.compile(
        rf"(?<![\w.])({esc})(?![\w])"
        rf"|(?<![\w])(?!(?:self|cls)\b)([A-Za-z_]\w*)\.{esc}(?![\w])"
    )


class PurposeError(Exception):
    """不可接受的目标（例如符号不存在）。"""


# ---------------------------------------------------------------------------
# 文件 / 目录：悬停在地图节点上要能回答"这个文件是干嘛的"
# ---------------------------------------------------------------------------
TEST_DIR_NAMES = {"tests", "test", "spec", "specs", "__tests__", "testing"}
TEST_CONFIG_NAMES = {"conftest.py", "pytest.ini", "tox.ini", "vitest.config.js", "jest.config.js"}


def _leading_comment(source: str, limit: int = 40) -> str:
    """非 Python 文件的"文件头注释"：开头连续的 // 或 /* … */ 取第一段。"""
    lines = source.split("\n")
    # Vue：优先 <script> 段里的开头注释
    if "<script" in source:
        idx = next((i for i, l in enumerate(lines) if "<script" in l), 0)
        lines = lines[idx + 1:]
    buf: list[str] = []
    started = False
    for raw in lines[:limit]:
        text = raw.strip()
        if not started and not text:
            continue
        if text.startswith("//") or text.startswith("*") or text.startswith("/*") or text.startswith("<!--"):
            started = True
            cleaned = text.lstrip("/*<!-").strip().strip("*/->").strip()
            if cleaned:
                buf.append(cleaned)
        else:
            break
    return " ".join(buf)[:400]


def _module_doc(source: str) -> str:
    """Python 文件/包的模块说明（`ast.get_docstring`）。"""
    try:
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001
        return ""
    return (ast.get_docstring(tree) or "").strip()


def _java_declaration_doc(source: str, line: int) -> tuple[str, int]:
    """读取紧邻声明的 Javadoc；import 后的类说明同样属于文件职责的依据。

    只接受文档与声明之间的空白或注解，不能把成员说明、许可证或其它类的说明
    错安到当前声明上。源码仍以索引行号为定位依据，不扩充启发式解析出的符号数。
    """
    lines = source.splitlines()
    if line <= 0 or line > len(lines):
        return "", 0
    before = "\n".join(lines[:line - 1])
    comments = list(re.finditer(r"/\*\*(.*?)\*/", before, flags=re.S))
    if not comments:
        return "", 0
    comment = comments[-1]
    gap = before[comment.end():]
    # Annotations may span several lines and contain arrays or nested annotations.
    pos = 0
    while pos < len(gap):
        if gap[pos].isspace():
            pos += 1
            continue
        annotation = re.match(r"@[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", gap[pos:])
        if not annotation:
            return "", 0
        pos += annotation.end()
        while pos < len(gap) and gap[pos].isspace():
            pos += 1
        if pos < len(gap) and gap[pos] == "(":
            depth, quote, escaped = 0, "", False
            while pos < len(gap):
                char = gap[pos]
                pos += 1
                if quote:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        quote = ""
                elif char in ('"', "'"):
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break
            if depth or quote:
                return "", 0

    text = "\n".join(re.sub(r"^\s*\* ?", "", part).rstrip() for part in comment.group(1).splitlines()).strip()
    # Keep prose and list structure, while showing common Javadoc markup as text.
    text = re.sub(r"\{@(?:code|literal)\s+([^}]*)\}", r"\1", text)
    text = re.sub(r"\{@link\s+([^}\s]+)(?:\s+([^}]+))?\}", lambda m: m.group(2) or m.group(1), text)
    text = re.sub(r"<\s*li(?:\s[^>]*)?>", "\n- ", text, flags=re.I)
    text = re.sub(r"<\s*/?(?:p|ul|ol|li)\s*>|<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"</?[A-Za-z][^>]*>", "", text))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:MAX_DOCUMENTATION_CHARS], before.count("\n", 0, comment.start()) + 1


def _java_file_doc(source: str, symbols: list[dict]) -> tuple[str, int]:
    types = sorted(
        (row for row in symbols if row.get("kind") in {"class", "interface", "enum", "type"}
         and not row.get("parent")),
        key=lambda row: int(row.get("line") or 0),
    )
    if not types:
        return "", 0
    # A file has one main top-level declaration. A later helper's documentation
    # must not become the whole file's purpose when the first type has no docs.
    return _java_type_doc(source, types[0])


def _java_type_doc(source: str, row: dict) -> tuple[str, int]:
    """核对类型名称后定位源码，避免旧索引行号或换行规范差异造成注释遗漏。"""
    from code_agent.codeintel.parsers.js_parser import _LineState, _clean_line

    name = str(row.get("name") or "")
    if not name:
        return "", 0
    declaration = re.compile(rf"\b(?:class|interface|enum|record)\s+{re.escape(name)}\b")
    state = _LineState()
    for line, raw in enumerate(source.splitlines(), start=1):
        cleaned = _clean_line(raw, state)
        if len(raw) - len(raw.lstrip(" \t")) <= 2 and declaration.search(cleaned):
            return _java_declaration_doc(source, line)
    return "", 0


def _kind_counts(rows: list[dict]) -> str:
    """符号构成：`函数 12 · 类 3 · 常量 5`。"""
    counter: dict[str, int] = {}
    for row in rows:
        counter[row["kind"]] = counter.get(row["kind"], 0) + 1
    parts = [
        f"{ROLE_LABEL.get(kind, kind)} {n}"
        for kind, n in sorted(counter.items(), key=lambda kv: -kv[1])
    ]
    return " · ".join(parts[:5])


def _is_test_path(rel: str) -> bool:
    parts = [p.lower() for p in rel.split("/")]
    if any(p in TEST_DIR_NAMES for p in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    return name.startswith("test_") or name.endswith("_test.py") or ".spec." in name or ".test." in name


def describe_file(index: Any, *, read_text: Callable[[str], str], rel: str) -> dict:
    """一个文件的"这是干嘛的"：作者写的模块说明 + 结构事实 + 谁在用它。"""
    from code_agent.codeintel.store import file_id

    rel = str(rel or "").replace("\\", "/")
    row = index.file_row(rel) or {}
    symbols = index.symbols_for(rel) if row else []
    lang = str(row.get("lang") or "")
    loc = int(row.get("loc") or 0)
    out: dict[str, Any] = {
        "available": False,
        "target": f"file:{rel}",
        "kind": "file",
        "role": "文件",
        "name": rel.rsplit("/", 1)[-1],
        "path": rel,
        "line": 1,
        "lang": lang,
        "facts": [],
        "notes": [],
        "headline": "",
        "basis": "符号索引 + 文件内容",
    }
    if not row:
        out["reason"] = f"索引里没有这个文件：{rel}（可能刚被删除）"
        return out

    doc = ""
    doc_line = 1
    doc_source = "文件头注释"
    source = ""
    try:
        source = read_text(rel)
    except Exception:  # noqa: BLE001
        source = ""
    if source:
        doc = _module_doc(source) if rel.endswith(".py") else ""
        if doc:
            doc_source = "模块文档串"
        if rel.endswith(".java"):
            doc, doc_line = _java_file_doc(source, symbols)
            if doc:
                doc_source = "类型 Javadoc"
        if not doc:
            doc = _leading_comment(source)
            doc_line = 1
            doc_source = "文件头注释"
    doc_first = _first_sentence(doc)
    facts: list[dict] = []
    facts.append({"label": "基本情况", "text": f"{lang or '未知语言'} · {loc} 行 · {len(symbols)} 个符号", "line": 1})
    if symbols:
        facts.append({"label": "符号构成", "text": _kind_counts(symbols), "line": 1})
    if doc_first:
        facts.append({"label": "作者写的说明", "text": doc_first, "line": doc_line})
        out["documentation"] = {"text": doc[:MAX_DOCUMENTATION_CHARS], "line": doc_line, "source": doc_source}
    importers = sorted({str(e["src"]).replace("file:", "") for e in index.edges(kind="imports", dst=file_id(rel))})
    if importers:
        where = "、".join(importers[:3])
        more = f" 等 {len(importers)} 个文件" if len(importers) > 3 else ""
        facts.append({"label": "谁在用它", "text": f"{where}{more} 导入了它", "line": 1})
    else:
        facts.append({"label": "谁在用它", "text": "索引里没有其它文件导入它", "line": 1})
    if row.get("entrypoint"):
        facts.append({"label": "入口", "text": "被识别为入口文件（__main__ / main() / 启动脚本）", "line": 1})
    if row.get("skipped"):
        facts.append({"label": "未索引", "text": f"未参与解析：{row['skipped']}", "line": 1})
    if _is_test_path(rel):
        facts.append({"label": "性质", "text": "测试文件：它描述的是行为契约，不是实现", "line": 1})
    if row.get("error"):
        facts.append({"label": "解析问题", "text": str(row["error"])[:120], "line": 1})

    headline = doc_first or f"{lang or '未知语言'} 文件：{loc} 行"
    if symbols:
        headline += f"；含 {len(symbols)} 个符号"
    if importers:
        headline += f"，被 {len(importers)} 个文件导入"
    out.update({"available": True, "headline": headline, "facts": _drop_repeats(facts, headline)[:MAX_FACTS]})
    out["notes"] = ["文件说明优先取作者写的模块文档串、类型 Javadoc 或文件头注释；其余是索引里量出来的结构事实"]
    if lang == "java":
        out["notes"].append("Java 结构来自启发式索引，可能没有列出成员；显式 import 之外的注入与运行时使用不计入导入数量")
    return out


def describe_dir(index: Any, *, read_text: Callable[[str], str], rel: str) -> dict:
    """一个目录的"这是干嘛的"：装了多少东西、主要是什么语言、有没有包说明。"""
    rel = str(rel or "").strip("/").replace("\\", "/")
    prefix = f"{rel}/" if rel else ""
    files = [
        row for row in index.file_rows().values()
        if (row["path"].startswith(prefix) if prefix else True) and not row.get("skipped")
    ]
    langs: dict[str, int] = {}
    for row in files:
        langs[str(row.get("lang") or "?")] = langs.get(str(row.get("lang") or "?"), 0) + 1
    direct_files = [row for row in files if row["path"][len(prefix):].count("/") == 0]
    subdirs = {
        row["path"][len(prefix):].split("/")[0]
        for row in files
        if "/" in row["path"][len(prefix):]
    }
    symbols = index.query_all(
        "SELECT kind, COUNT(*) n FROM symbols WHERE path LIKE ? GROUP BY kind", (f"{prefix}%",)
    )
    total_symbols = sum(int(r["n"]) for r in symbols)
    composition = " · ".join(
        f"{ROLE_LABEL.get(r['kind'], r['kind'])} {r['n']}"
        for r in sorted(symbols, key=lambda r: -int(r["n"]))[:5]
    )

    out: dict[str, Any] = {
        "available": True,
        "target": f"mod:{rel}",
        "kind": "dir",
        "role": "目录",
        "name": rel.rsplit("/", 1)[-1] if rel else "(项目根)",
        "path": rel,
        "line": 1,
        "facts": [],
        "notes": [],
        "basis": "符号索引（按路径前缀聚合）",
    }
    if not files:
        out["headline"] = f"空目录（索引里没有已解析的文件）"
        out["facts"] = [{"label": "基本情况", "text": "0 个文件", "line": 1}]
        out["notes"] = ["索引不包含被跳过的文件（密钥/二进制/超大）"]
        return out

    lang_bits = "、".join(f"{name} {n}" for name, n in sorted(langs.items(), key=lambda kv: -kv[1])[:3])
    facts = [
        {"label": "规模", "text": f"{len(files)} 个文件（直属 {len(direct_files)}）· {len(subdirs)} 个子目录 · {total_symbols} 个符号", "line": 1},
        {"label": "语言", "text": lang_bits, "line": 1},
    ]
    if composition:
        facts.append({"label": "符号构成", "text": composition, "line": 1})

    # 包说明：优先 __init__.py 的模块文档串（Python 包的"这是干嘛的"就写在那里）
    init_rel = f"{prefix}__init__.py"
    doc = ""
    try:
        if index.file_row(init_rel):
            doc = _module_doc(read_text(init_rel))
    except Exception:  # noqa: BLE001
        doc = ""
    if not doc:
        readme = next((row["path"] for row in direct_files if row["path"].lower().endswith("readme.md")), "")
        if readme:
            try:
                doc = " ".join(read_text(readme).split("\n\n")[0].split("\n")[1:]).strip() or ""
            except Exception:  # noqa: BLE001
                doc = ""
    if doc:
        facts.append({"label": "包说明", "text": _first_sentence(doc), "line": 1})

    if rel in TEST_DIR_NAMES or _is_test_path(f"{rel}/x"):
        facts.append({"label": "性质", "text": "测试目录：这里描述行为契约", "line": 1})

    headline = f"{len(files)} 个文件 · {total_symbols} 个符号"
    if lang_bits:
        headline += f"，主要是 {sorted(langs.items(), key=lambda kv: -kv[1])[0][0]}"
    if doc:
        headline = f"{_first_sentence(doc)}（{headline}）"
    out.update({"headline": headline, "facts": _drop_repeats(facts, headline)[:MAX_FACTS]})
    out["notes"] = ["目录说明优先取包内 `__init__.py` 的文档串，其次是目录里的 README"]
    return out


def describe_target(index: Any, *, read_text: Callable[[str], str], target: str) -> dict:
    """统一入口：符号 / 文件 / 目录 都能问"这是干嘛的"。

    为什么统一：地图上悬停一个节点，用户不区分"我悬停的是文件还是函数"——
    `file:<path>` 与 `mod:<dir>` 是地图节点自带的 id，直接复用，前端不用分支。
    """
    sid = str(target or "")
    if sid.startswith("file:"):
        return describe_file(index, read_text=read_text, rel=sid[len("file:"):])
    if sid.startswith("mod:"):
        return describe_dir(index, read_text=read_text, rel=sid[len("mod:"):])
    return describe(index, read_text=read_text, symbol_id=sid)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _words(name: str) -> list[str]:
    """`max_retry_count` / `maxRetryCount` → ['max','retry','count']。"""
    if not name:
        return []
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [w for w in re.split(r"[_\W]+", snake.lower()) if w]


def _camel(name: str) -> str:
    return "".join(w.capitalize() for w in _words(name)) or name


def _name_hint(name: str) -> str:
    words = _words(name)
    if not words:
        return ""
    if words[-1] not in _MEASURE_TAIL and any(w in _CREDENTIAL_WORDS for w in words):
        return "凭据（注意不要外泄）"
    last = words[-1]
    for key, text in NAME_HINTS:
        if last == key:
            return text
    return ""


def _action_hint(name: str) -> str:
    for prefix, text in NAME_PREFIX:
        if name.startswith(prefix):
            return text
    return ""


def _is_upper_const(name: str) -> bool:
    return bool(name) and name.upper() == name and any(c.isalpha() for c in name)


def _unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 —— 反解析失败不该让整个说明挂掉
        return ""


def _const_type(value: Any) -> str:
    if isinstance(value, bool):
        return "布尔字面量"
    if isinstance(value, str):
        return "字符串字面量"
    if isinstance(value, int):
        return "整数字面量"
    if isinstance(value, float):
        return "浮点字面量"
    if value is None:
        return "None"
    if isinstance(value, bytes):
        return "字节串字面量"
    return f"{type(value).__name__} 字面量"


def _value_hint(node: ast.AST | None) -> str:
    """赋值右边的表达式 → "它保存的是什么"。**只是表达式形态的翻译**。"""
    if node is None:
        return "（没有解析到赋值表达式）"
    if isinstance(node, ast.Constant):
        text = _unparse(node)
        shown = text if len(text) <= 40 else f"{text[:36]}…"
        return f"{_const_type(node.value)}（{shown}）"
    if isinstance(node, ast.JoinedStr):
        return "f-string 拼出的文本"
    if isinstance(node, ast.Lambda):
        return "匿名函数"
    if isinstance(node, ast.Dict):
        return "字典字面量"
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        kind = {ast.List: "列表", ast.Set: "集合", ast.Tuple: "元组"}[type(node)]
        return f"{kind}字面量（{len(node.elts)} 项）"
    if isinstance(node, (ast.DictComp, ast.ListComp, ast.SetComp)):
        return "推导式产生的容器"
    if isinstance(node, ast.GeneratorExp):
        return "生成器表达式"
    if isinstance(node, ast.Await):
        return f"await 的结果（{_unparse(node.value)}）"
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            hint = CTOR_HINTS.get(func.id)
            if hint:
                return f"{func.id}() 造出的{hint}"
            return f"{func.id}() 的返回值"
        if isinstance(func, ast.Attribute):
            hint = CTOR_HINTS.get(func.attr)
            tail = f"{_unparse(func)}(…)"
            if hint:
                return f"{tail} 造出的{hint}"
            return f"{tail} 的返回值"
        return "一次调用的返回值"
    if isinstance(node, ast.Name):
        return f"另一个值 `{node.id}` 的引用（别名/缓存）"
    if isinstance(node, ast.Attribute):
        return f"对象属性 `{_unparse(node)}`"
    if isinstance(node, ast.BinOp):
        return f"运算结果（`{_unparse(node)}`）"
    if isinstance(node, ast.Subscript):
        return f"下标/泛型取值（`{_unparse(node)}`）"
    if isinstance(node, ast.IfExp):
        return "条件表达式的结果"
    return f"表达式 `{_unparse(node)[:60]}`"


def _annot(node: ast.AST | None) -> str:
    return _unparse(node) if node is not None else ""


def _docstring(node: ast.AST) -> str:
    try:
        return (ast.get_docstring(node) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _first_sentence(text: str, limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    for sep in ("。", "．", ". ", "\n"):
        idx = flat.find(sep)
        if 0 < idx < limit:
            return flat[: idx + 1].strip()
    return flat[:limit]


def _drop_repeats(facts: list[dict], headline: str, labels: tuple[str, ...] = ("作者写的说明", "包说明")) -> list[dict]:
    """去掉与结论句重复的条目（只针对"作者写的说明/包说明"这类整句事实）。

    为什么**只**处理这两个标签：一开始写成"文本出现在 headline 里就删"，
    结果把 `返回：int` 这种短事实删了（`int` 正好是 headline 的子串），
    常量那条更冤——`保存的值：整数字面量（3）` 被整句吞掉，测试直接抓到。
    文档串那两句才是真重复（headline 就是它的前缀）。
    """
    head = " ".join((headline or "").split())
    out = []
    for fact in facts:
        text = " ".join(str(fact.get("text") or "").split())
        if fact.get("label") in labels and len(text) >= 8 and text in head:
            continue
        out.append(fact)
    return out


# ---------------------------------------------------------------------------
# AST 定位
# ---------------------------------------------------------------------------
def _scope_of(tree: ast.AST, lineno: int, name: str = "") -> tuple[str, str]:
    """(作用域, 所属名字)。模块级 / 类成员 / 函数局部 / 参数。

    两个刻意的细节（都被真机结果打回过）：
    1. 取**包含这一行的最深那个**定义：方法体里的一行同时被 ClassDef 和 FunctionDef 包含，
       按 `ast.walk` 顺序取最后一个会取错（walk 是广度优先，不是嵌套顺序）；
    2. 定义**自己那一行**不算"被自己包含"：`def transcripts_dir(...)` 的 lineno 就是函数自己的
       lineno，如果按 `start <= line <= end` 判，一个模块级函数会被说成"函数局部"。
    """
    owner = ""
    kind = "模块级"
    best = -1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and start == lineno and name:
            args = list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs)
            if any(getattr(a, "arg", "") == name and getattr(a, "lineno", 0) == lineno for a in args):
                kind, owner, best = "参数", node.name, start
            continue
        if start < lineno <= end and start > best:
            kind = "类成员" if isinstance(node, ast.ClassDef) else "函数局部"
            owner = node.name
            best = start
    return kind, owner


def _node_at(tree: ast.AST, row: dict) -> ast.AST | None:
    """按行号（+ 名字）在 AST 里定位这个符号。"""
    lineno = int(row.get("line") or 0)
    name = str(row.get("name") or "")
    kind = str(row.get("kind") or "")
    want = (ast.FunctionDef, ast.AsyncFunctionDef) if kind in ("function", "method", "hook", "route") else (
        (ast.ClassDef,) if kind in ("class", "interface", "component", "enum") else (ast.Assign, ast.AnnAssign, ast.AugAssign)
    )
    for node in ast.walk(tree):
        if not isinstance(node, want):
            continue
        if lineno and node.lineno != lineno:
            continue
        if name and name not in {_unparse(t) for t in getattr(node, "targets", [])} | {
            _unparse(getattr(node, "target", None))
        } | {getattr(node, "name", "")}:
            continue
        return node
    return None


def _targets(node: ast.AST) -> list[str]:
    out: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(getattr(child, "ctx", None), (ast.Store,)):
            out.append(child.id)
    return out


#: 写者 / 取值两条分析的限制：界面是一屏抽屉，超过就不列了
MAX_WRITERS = 8
MAX_VALUES = 12


def _literal_of(node: ast.AST | None) -> str:
    """常量字面量 → 展示文本（不是常量就返回空）。"""
    if isinstance(node, ast.Constant):
        return _unparse(node)
    return ""


def _container_values(node: ast.AST | None) -> list[str]:
    """容器字面量 → 元素值列表（字典取 key）。`("compact","expanded")` → ['compact','expanded']。"""
    out: list[str] = []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for elt in node.elts:
            text = _literal_of(elt)
            if text:
                out.append(text)
    elif isinstance(node, ast.Dict):
        for key in node.keys:
            text = _literal_of(key)
            if text:
                out.append(text)
    return out


def _annotation_values(annotation: ast.AST | None) -> list[str]:
    """注解里写死的取值范围：`Literal["a","b"]`、`bool`。其它注解不猜。"""
    if annotation is None:
        return []
    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        name = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else "")
        if name == "Literal":
            sl = annotation.slice
            elts = sl.elts if isinstance(sl, (ast.Tuple, ast.List)) else [sl]
            return [t for t in (_literal_of(e) for e in elts) if t]
    if isinstance(annotation, ast.Name) and annotation.id == "bool":
        return ["True", "False"]
    return []


#: 会被算作"改写这个变量"的方法名：`_seen.add(x)`、`_cache.update(d)` 也是改它
MUTATING_METHODS = {
    "add", "append", "extend", "update", "pop", "remove", "discard", "clear",
    "setdefault", "insert", "sort", "reverse", "add_all", "put", "push",
}


def _writers_in_file(tree: ast.AST, source: str, name: str, def_line: int) -> list[dict]:
    """谁在改它：AST 里所有写入这个变量的语句，带上所属函数。

    比"读写计数"多一层信息：计数只说"被改了 3 次"，这里能说"是 `_reset()` 和
    `load_env_file()` 改的"。模块级状态排查就靠这个。

    三种写入都要认（只认 `name = ...` 会漏掉最常见的那种）：
    1. `name = ...` / `name += ...` / `for name in ...` / `with ... as name`；
    2. `name[k] = v`（下标赋值）；
    3. `name.add(x)` / `name.update(d)`（可变对象的方法调用改写）——
       本仓的 `_LOADED_ENV_FILES.add(...)` 就是这一类。
    """
    lines = source.split("\n")
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        kind = ""
        lineno = getattr(node, "lineno", 0)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For,
                             ast.AsyncFor, ast.comprehension, ast.NamedExpr, ast.With, ast.AsyncWith)):
            if name not in _targets(node):
                continue
            kind = "自增/自减" if isinstance(node, ast.AugAssign) else "赋值"
            if isinstance(node, (ast.For, ast.AsyncFor)) or isinstance(node, ast.comprehension):
                kind = "循环变量"
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                kind = "上下文绑定"
        elif isinstance(node, ast.Subscript) and isinstance(getattr(node, "ctx", None), ast.Store):
            base = node.value
            if not (isinstance(base, ast.Name) and base.id == name):
                continue
            kind = "下标赋值"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if not (isinstance(base, ast.Name) and base.id == name):
                continue
            if node.func.attr not in MUTATING_METHODS:
                continue
            kind = f"调用 {node.func.attr}() 改写"
        else:
            continue
        owner = "模块级"
        if lineno:
            scope, owner_name = _scope_of(tree, lineno, name)
            if scope in ("函数局部", "参数") and owner_name:
                owner = f"{owner_name}()"
            elif scope == "类成员" and owner_name:
                owner = f"{owner_name} 类体"
            else:
                owner = scope
        key = (lineno, owner)
        if key in seen:
            continue
        seen.add(key)
        text = (lines[lineno - 1].strip() if 0 < lineno <= len(lines) else "")[:80]
        out.append({"line": lineno, "owner": owner, "kind": kind, "text": text, "def_line": def_line})
    out.sort(key=lambda w: w["line"])
    return out[:MAX_WRITERS]


def _cross_file_writers(
    index: Any,
    *,
    read_text: Callable[[str], str],
    rel: str,
    name: str,
    def_line: int,
) -> list[dict]:
    """别的文件里有没有改它（`模块.名字 = …`）。文本匹配，如实标注。"""
    from code_agent.codeintel.store import file_id

    write_pat = re.compile(
        rf"(?<![\w.])(?:[A-Za-z_]\w*\.)?{re.escape(name)}\s*(\+=|-=|\*=|/=|\|=|&=|:=|=(?!=))"
    )
    out: list[dict] = []
    for edge in index.edges(kind="imports", dst=file_id(rel))[: MAX_CANDIDATES]:
        cand = str(edge["src"]).replace("file:", "")
        if not cand or cand == rel:
            continue
        try:
            other = read_text(cand)
        except Exception:  # noqa: BLE001
            continue
        for idx, line in enumerate(other.split("\n"), start=1):
            if idx == def_line and cand == rel:
                continue
            if write_pat.search(line) and not line.strip().startswith("#"):
                out.append({"path": cand, "line": idx, "text": line.strip()[:80]})
                break
    return out[:MAX_WRITERS]


# ---------------------------------------------------------------------------
# 使用情况（同文件 + 跨文件候选）
# ---------------------------------------------------------------------------
def _same_file_usage_text(source: str, name: str, def_line: int) -> tuple[int, int, int]:
    """(读取次数, 赋值次数, 定义行) —— 词边界匹配，排除 `x.name` 这种属性访问。

    只给非 Python 用：没有 AST 时只能文本匹配，所以调用方会标明"文本匹配"。
    """
    pat = _mention_pattern(name)
    reads = writes = 0
    for idx, line in enumerate(source.split("\n"), start=1):
        if not pat.search(line):
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = _ASSIGN_LINE.match(stripped)
        if idx == def_line or (match and match.group(1) == name):
            writes += 1
        else:
            reads += 1
    return reads, writes, def_line


def _same_file_usage(tree: ast.AST, name: str) -> dict:
    """AST 口径的同文件使用情况。

    为什么不用正则数赋值：`RunConfig(max_rounds=...)` 里的 `max_rounds=` 也是
    "行首是名字 + 等号"，正则会把**关键字实参**数成一次重新赋值，于是 `max_rounds`
    被判成"不是常量"（真机跑出来过）。AST 能分清 `ctx=Store` 的赋值与关键字实参。

    `x.name` 这种属性访问单独记一笔：dataclass 字段的日常用法就是 `self.name`。
    """
    reads = writes = attr_reads = attr_writes = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            if isinstance(node.ctx, ast.Store):
                writes += 1
            elif isinstance(node.ctx, ast.Load):
                reads += 1
        elif isinstance(node, ast.Attribute) and node.attr == name:
            if isinstance(node.ctx, ast.Store):
                attr_writes += 1
            elif isinstance(node.ctx, ast.Load):
                attr_reads += 1
    # 定义处自己那一次赋值（`name = ...` / `name: T = ...`）算"赋值"而不是"读写"
    return {"reads": max(0, reads), "writes": writes, "attr_reads": attr_reads, "attr_writes": attr_writes}


def _cross_file_usage(
    index: Any,
    *,
    read_text: Callable[[str], str],
    rel: str,
    name: str,
    kind: str,
    def_line: int,
) -> dict:
    """在"导入了本模块的文件"里找真正用到它的地方。

    为什么不用索引里的 `referenced_by` 边：那些边只统计**签名里出现过**名字的
    class/function（变量不参与，且要求名字全仓唯一），所以对 `logger` 这种模块级变量
    只能给出"5 个文件导入过这个模块"——那是文件级事实，说成"被引用 5 次"会误导。
    这里退回到候选文件源码做词边界匹配，并如实标注是文本匹配。
    """
    from code_agent.codeintel.store import file_id

    candidates: list[str] = []
    for edge in index.edges(kind="imports", dst=file_id(rel)):
        src = str(edge["src"]).replace("file:", "")
        if src and src != rel and src not in candidates:
            candidates.append(src)
    candidates = candidates[:MAX_CANDIDATES]

    is_callable = kind in ("function", "method", "hook", "route")
    hits: list[dict] = []
    scanned = 0
    for cand in candidates:
        try:
            other = read_text(cand)
        except Exception:  # noqa: BLE001 —— 单个文件读不到不影响其余
            continue
        if len(other) > MAX_CANDIDATE_BYTES:
            continue
        scanned += 1
        pat = _mention_pattern(name)
        lines = [i for i, line in enumerate(other.split("\n"), start=1) if pat.search(line)]
        call_lines = [i for i in lines if is_callable and re.search(rf"{re.escape(name)}\s*\(", other.split("\n")[i - 1])]
        if lines:
            hits.append(
                {
                    "path": cand,
                    "count": len(lines),
                    "line": lines[0],
                    "call_sites": call_lines,
                }
            )
    return {
        "candidates": candidates,
        "scanned": scanned,
        "hits": sorted(hits, key=lambda h: (-h["count"], h["path"])),
        "kind": "call" if is_callable else "mention",
    }


# ---------------------------------------------------------------------------
# "它到底干什么"：字段 / 用在哪 / 谁守着它
# ---------------------------------------------------------------------------
def _source_line(read_text: Callable[[str], str], rel: str, line: int) -> str:
    """取某文件某一行的源码（读不到就空着，不猜）。"""
    try:
        lines = read_text(rel).split("\n")
    except Exception:  # noqa: BLE001
        return ""
    if 1 <= int(line) <= len(lines):
        return lines[int(line) - 1].strip()[:80]
    return ""


def _owner_name(index: Any, rel: str, line: int) -> str:
    """这一行属于哪个函数/方法（索引答不出来就留空——宁可不写，也不编一个归属）。"""
    try:
        sid = index.symbol_at(rel, int(line))
    except Exception:  # noqa: BLE001
        return ""
    if not sid:
        return ""
    row = index.symbol(str(sid)) or {}
    return str(row.get("name") or "")


#: 纯名字行（`ContractReport,` 这种 import 续行 / `__all__` 项）——最不像"使用"
_BARE_NAME_LINE = re.compile(r"^[A-Za-z_][\w.]*(\s*,\s*[A-Za-z_][\w.]*)*,?$")


def _best_usage_line(read_text: Callable[[str], str], rel: str, name: str) -> tuple[int, int]:
    """在候选文件里挑一行**真正的使用**：跳过 import，优先"构造/调用"那一行。

    返回 `(行号, 分数)`，分数越小越像"真的在用"：
    0 = 构造/调用（`WorkflowStep(...)`）、1 = 其它真实出现（注解、比较、赋值）、
    2 = 只是被列在 import / `__all__` 里、3 = import 行本身。

    为什么必须挑：命中集里的第一行往往是 `from x import Name`（甚至是括号换行后的
    `Name,` 续行）——那是"这里提到了它"，不是"这里用了它"。把 import 当用法贴出来，
    等于又回到"说了等于没说"。
    """
    try:
        lines = read_text(rel).split("\n")
    except Exception:  # noqa: BLE001
        return 0, 9
    pattern = _mention_pattern(name)
    mentions = [i for i, line in enumerate(lines, start=1) if pattern.search(line)]
    if not mentions:
        return 0, 9

    def score(index: int) -> int:
        text = lines[index - 1].strip()
        if re.search(rf"{re.escape(name)}\s*\(", text):
            return 0                      # 构造 / 调用：最具体
        if text.startswith(("import ", "from ")):
            return 3
        if _BARE_NAME_LINE.match(text):
            return 2                      # 只是被列在 import / __all__ 里
        return 1                          # 其它真实出现（注解、比较、赋值…）

    best = min(mentions, key=lambda i: (score(i), i))
    return best, score(best)


def _class_fields(index: Any, symbol_id: str) -> list[str]:
    """类体里的字段（`name: str` 这种原样一行）。

    为什么要单独列：数据类"是干什么的"往往就写在字段上——`WorkflowStep(name, prompt)`
    比"没有基类；含 0 个方法"有用得多。
    """
    rows = index.query_all(
        "SELECT name, signature FROM symbols WHERE parent=? AND kind IN "
        "('property','variable','constant') ORDER BY line",
        (symbol_id,),
    )
    out: list[str] = []
    for row in rows:
        name = str(row["name"] or "")
        if not name or name.startswith("__"):
            continue
        sig = " ".join(str(row["signature"] or "").split())
        if sig.startswith(("def ", "async def ")):
            # `@property def enabled(...)` 是**取值方法**，不是数据字段：
            # 混进"字段"里会让人以为这个类有一堆字段（真机上 `FailureBreaker` 就中招了）
            continue
        out.append(sig[:60] if sig else name)
    return out


def _usage_evidence(
    index: Any, *, read_text: Callable[[str], str], name: str, rel: str, cross: dict
) -> tuple[str, dict | None]:
    """它在真实代码里**被怎么用**：谁在哪一行用了它，并附上那一行源码。

    这是整张作用卡里最具体的一条：字段、继承、成员数都说明不了"什么时候用它"，
    而调用点/使用点是**看得到的证据**（本模块的底线：模板里不放想象）。

    候选文件 = 导入了本模块的文件 + **它自己所在的文件**（`WorkflowStep` 最自然的用法
    就在同一模块的 `parse_steps` 里，只在"外部文件"里找会得出"没人用它"的假结论）。
    挑行与排序：先看"是不是真的在用"（构造/调用 > 其它出现 > import），
    再看产品代码优先于测试代码、自己的文件排在最后。
    """
    from code_agent.codeintel.impact import is_test_path

    if not name:
        return "", None
    candidates: list[str] = [str(h.get("path") or "") for h in (cross.get("hits") or [])]
    candidates.append(str(rel or ""))
    best: tuple[int, int, int, str, int] | None = None
    for path in dict.fromkeys(p for p in candidates if p):
        line, score = _best_usage_line(read_text, path, name)
        if not line or score > 1:
            continue                      # import / __all__ 里出现不算"用在哪"
        rank = (score, 1 if is_test_path(path) else 0, 0 if path == rel else 1, path, line)
        if best is None or rank < best:
            best = rank
    if best is None:
        return "", None
    _score, _is_test, _is_self, path, line = best
    owner = _owner_name(index, path, line)
    snippet = _source_line(read_text, path, line)
    verb = "被调用" if cross.get("kind") == "call" else "被用到"
    where = f"{path}:{line}" + (f" 的 {owner}" if owner else "")
    text = f"{where}（`{snippet}`）" if snippet else where
    return f"在 {where} 里{verb}", {"label": "用在哪", "text": text, "line": line}


def _guard_tests(
    index: Any, *, read_text: Callable[[str], str], name: str, cross: dict
) -> dict | None:
    """守着它的测试：测试文件里提到它、且提到的那几行落在哪个测试函数体内。

    为什么不直接列"这个测试文件"：一个测试文件里有几十条测试，列出整份文件等于没说；
    要找的是**函数体内真的用了它**的那几条测试——它们往往一句话就写清了这段代码的意图。
    """
    from code_agent.codeintel.impact import is_test_path

    if not name:
        return None
    pattern = _mention_pattern(name)
    guards: list[str] = []
    for hit in cross.get("hits") or []:
        rel = str(hit.get("path") or "")
        if not is_test_path(rel):
            continue
        try:
            lines = read_text(rel).split("\n")
        except Exception:  # noqa: BLE001
            continue
        mention_lines = [i for i, line in enumerate(lines, start=1) if pattern.search(line)]
        if not mention_lines:
            continue
        for row in index.symbols_for(rel):
            if str(row.get("kind") or "") not in ("function", "method"):
                continue
            start = int(row.get("line") or 0)
            end = int(row.get("end_line") or start)
            if any(start <= ln <= max(end, start) for ln in mention_lines):
                guards.append(f"{row.get('name')}（{rel}:{start}）")
    uniq = list(dict.fromkeys(guards))
    if not uniq:
        return None
    return {"label": "守着它的测试", "text": "、".join(uniq[:3]), "line": 0}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def describe(index: Any, *, read_text: Callable[[str], str], symbol_id: str) -> dict:
    """一个符号的"意义与作用"。事实可追、命名推断标注来源、拿不准就降级。"""
    row = index.symbol(symbol_id)
    if row is None:
        raise PurposeError(f"符号不存在：{symbol_id}")
    rel = str(row.get("path") or "")
    kind = str(row.get("kind") or "unknown")
    name = str(row.get("name") or "")
    line = int(row.get("line") or 0)
    out: dict[str, Any] = {
        "available": False,
        "symbol_id": symbol_id,
        "name": name,
        "kind": kind,
        "role": ROLE_LABEL.get(kind, kind),
        "path": rel,
        "line": line,
        "end_line": int(row.get("end_line") or line),
        "confidence": row.get("confidence") or "high",
        "lang": "python" if rel.endswith(".py") else "other",
        "scope": "",
        "headline": "",
        "facts": [],
        "usage": {},
        "notes": [],
        "reason": "",
        "basis": "符号索引（结构事实）",
    }

    import json as _json

    def _list(key: str) -> list:
        raw = row.get(key)
        if isinstance(raw, str):
            try:
                return list(_json.loads(raw or "[]"))
            except Exception:  # noqa: BLE001
                return []
        return list(raw or [])

    doc = str(row.get("doc") or "").strip()
    facts: list[dict] = []

    # ---- 非 Python：只给结构事实 + 文本级使用，不做语义推断 ----
    if not rel.endswith(".py"):
        hint = _name_hint(name)
        signature = str(row.get("signature") or "")
        reads = writes = 0
        cross = {"hits": [], "candidates": [], "scanned": 0, "kind": "mention"}
        try:
            source = read_text(rel)
            if not doc and rel.endswith(".java"):
                if kind in {"class", "interface", "enum", "type"}:
                    doc, doc_line = _java_type_doc(source, row)
                else:
                    doc, doc_line = _java_declaration_doc(source, line)
                if doc:
                    out["documentation"] = {"text": doc, "line": doc_line, "source": "声明 Javadoc"}
            reads, writes, _ = _same_file_usage_text(source, name, line)
            cross = _cross_file_usage(
                index, read_text=read_text, rel=rel, name=name, kind=kind, def_line=line
            )
        except Exception:  # noqa: BLE001 —— 读不到就只给索引事实
            pass
        facts.append({"label": "位置", "text": f"{rel}:{line}", "line": line})
        if signature:
            facts.append({"label": "签名", "text": signature[:160], "line": line})
        if doc:
            facts.append({"label": "作者说明", "text": _first_sentence(doc), "line": line})
            if "documentation" not in out:
                out["documentation"] = {"text": doc[:MAX_DOCUMENTATION_CHARS], "line": line, "source": "符号文档"}
        if hint:
            facts.append({"label": "命名暗示", "text": f"{hint}（按命名约定推断，不是行为证据）", "line": line})
        if reads or writes:
            facts.append({"label": "本文件使用", "text": f"出现 {reads + writes} 处（文本匹配）", "line": line})
        if cross["hits"]:
            where = "、".join(f"{h['path']}（{h['count']} 处）" for h in cross["hits"][:4])
            facts.append({"label": "跨文件使用", "text": f"{len(cross['hits'])} 个文件：{where}", "line": cross["hits"][0]["line"]})
        headline = f"{out['role']} {name}"
        if hint:
            headline += f"（看名字是{hint}）"
        if doc:
            headline += f"：{_first_sentence(doc)}"
        elif signature:
            headline += f"：{signature[:100]}"
        if cross["hits"]:
            headline += f"；在 {len(cross['hits'])} 个文件里出现"
        out["available"] = True
        out["headline"] = headline
        out["facts"] = facts[:MAX_FACTS]
        out["usage"] = {
            "reads_same_file": reads,
            "writes_same_file": writes,
            "cross_files": [{"path": h["path"], "count": h["count"], "line": h["line"]} for h in cross["hits"]],
            "candidates": len(cross["candidates"]),
            "scanned": cross["scanned"],
            "cross_kind": cross["kind"],
        }
        out["notes"] = [
            "非 Python 文件：没有语句级 AST，只有签名/位置等结构事实 + 文本词边界匹配的使用点",
            "不做类型与用途推断——需要语义时请用「问 AI」或直接看实现",
        ]
        out["basis"] = "符号索引（启发式解析）+ 候选文件文本匹配"
        return out

    try:
        source = read_text(rel)
        tree = ast.parse(source)
    except Exception as exc:  # noqa: BLE001
        out["available"] = True
        out["headline"] = f"{out['role']} {name}（源码没读通，只能给结构事实）"
        out["facts"] = [{"label": "位置", "text": f"{rel}:{line}", "line": line}]
        out["reason"] = f"源码解析失败：{exc}"
        out["notes"] = ["解析失败时不猜用途，以文件内容为准"]
        return out

    node = _node_at(tree, row)
    scope, owner = _scope_of(tree, line, name)
    out["scope"] = scope
    same = _same_file_usage(tree, name)
    reads = same["reads"] + same["attr_reads"]
    writes = same["writes"]
    cross = _cross_file_usage(index, read_text=read_text, rel=rel, name=name, kind=kind, def_line=line)

    headline = ""
    action = _action_hint(name)
    hint = _name_hint(name)

    # ---------------- 变量 / 常量 ----------------
    if kind in ("variable", "constant", "property"):
        value = getattr(node, "value", None) if node is not None else None
        annotation = _annot(getattr(node, "annotation", None)) if node is not None else ""
        value_text = _value_hint(value)
        sig = str(row.get("signature") or "")
        if value is None and "=" in sig:
            value_text = f"`{sig.split('=', 1)[1].strip()}`"
        is_const = kind == "constant" or _is_upper_const(name)
        out["role"] = "常量" if is_const else "变量"
        facts.append({"label": "声明位置", "text": f"{scope}{' · ' + owner if owner else ''}", "line": line})
        facts.append({"label": "保存的值", "text": value_text, "line": line})
        if annotation:
            facts.append({"label": "类型注解", "text": annotation, "line": line})
        if hint:
            facts.append({"label": "命名暗示", "text": f"{hint}（按命名约定推断，不是行为证据）", "line": line})
        if is_const:
            facts.append({"label": "常量性", "text": "全大写命名：按约定是常量，不应被改写", "line": line})
        use_bits = [f"读取 {same['reads']} 处", f"赋值 {writes} 处"]
        if same["attr_reads"] or same["attr_writes"]:
            # dataclass / 实例字段的日常用法就是 `self.name` / `cls.name`
            use_bits.append(f"属性访问 读 {same['attr_reads']} / 写 {same['attr_writes']}")
        facts.append({"label": "本文件使用", "text": " · ".join(use_bits), "line": line})

        # 能取哪些值：注解里的 Literal / bool、容器字面量的元素、以及被重赋的常量。
        # 单个标量（`max_rounds = 3`）不算"取值范围"，只有真的存在多种取值或来自
        # 注解/容器时才说——否则抽屉里会出现"能取的值：3（1 种）"这种废话。
        values = _annotation_values(getattr(node, "annotation", None) if node is not None else None)
        from_container = False
        if not values and node is not None:
            values = _container_values(getattr(node, "value", None))
            from_container = bool(values)
        if not values:
            values = [
                _literal_of(getattr(other, "value", None))
                for other in ast.walk(tree)
                if isinstance(other, (ast.Assign, ast.AnnAssign))
                and name in _targets(other)
                and _literal_of(getattr(other, "value", None))
            ]
        values = [v for v in values if v]
        distinct = list(dict.fromkeys(values))
        if distinct and (from_container or len(distinct) > 1):
            facts.append({
                "label": "能取的值",
                "text": f"{'、'.join(distinct)}（{len(distinct)} 种，来自字面量/注解）",
                "line": line,
            })

        # 谁能改它：把"定义处那一次赋值"和"之后的改写"分开——模块级状态排查靠后者
        writers = _writers_in_file(tree, source, name, line)
        later = [w for w in writers if w["line"] != line]
        outside = _cross_file_writers(index, read_text=read_text, rel=rel, name=name, def_line=line)
        if later:
            def _fmt(w: dict) -> str:
                # 普通赋值不用标；`.add()` / 下标赋值要标出来——"改成什么样"本身就是信息
                suffix = "" if w["kind"] == "赋值" else f"，{w['kind']}"
                return f"{w['owner']}（{rel}:{w['line']}{suffix}）"

            where = "、".join(_fmt(w) for w in later[:4])
            extra = f" 等 {len(later)} 处" if len(later) > 4 else ""
            facts.append({"label": "谁能改它", "text": f"本文件：{where}{extra}", "line": later[0]["line"]})
        if outside:
            where = "、".join(f"{w['path']}:{w['line']}" for w in outside[:4])
            facts.append({"label": "跨文件写入", "text": f"{len(outside)} 个文件：{where}（文本匹配）", "line": outside[0]["line"]})
        if not later and not outside:
            facts.append({"label": "可变性", "text": "只有定义处那一次赋值，之后只读", "line": line})

        if cross["hits"]:
            where = "、".join(f"{h['path']}（{h['count']} 处）" for h in cross["hits"][:4])
            facts.append({"label": "跨文件使用", "text": f"{len(cross['hits'])} 个文件：{where}", "line": cross["hits"][0]["line"]})
        elif cross["scanned"]:
            facts.append(
                {
                    "label": "跨文件使用",
                    "text": f"导入了本模块的 {cross['scanned']} 个文件里没有直接出现 `{name}`（文本匹配，非类型解析）",
                    "line": 0,
                }
            )
        # 一句话说清楚：什么作用域、保存了什么、被谁用
        headline = f"{scope}{out['role']}：{value_text}"
        if hint and hint not in value_text:
            headline += f"，看名字是{hint}"
        if reads:
            headline += f"；本文件读取 {reads} 处"
        elif cross["hits"]:
            headline += "；本文件内没有其它引用（跨文件有使用）"
        else:
            headline += "；本文件里没有其它地方读它"
        if writes > 1:
            headline += f"，被重新赋值 {writes - 1} 次（不是常量）"

    # ---------------- 函数 / 方法 ----------------
    elif kind in ("function", "method", "hook", "route"):
        args = getattr(node, "args", None) if node is not None else None
        params = _list("params")
        returns = str(row.get("returns") or "")
        decorators = _list("decorators")
        annotated = 0
        if args is not None:
            all_args = list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs)
            annotated = sum(1 for a in all_args if a.annotation is not None)
        complexity = int(row.get("complexity") or 0)
        asserts = raises = 0
        if node is not None:
            for child in ast.walk(node):
                if isinstance(child, ast.Assert):
                    asserts += 1
                elif isinstance(child, ast.Raise):
                    raises += 1
        doc_first = _first_sentence(doc)
        facts.append({"label": "声明位置", "text": f"{scope}{' · ' + owner if owner else ''}", "line": line})
        if params:
            facts.append({"label": "入参", "text": f"{len(params)} 个：{'、'.join(params[:8])}" + ("…" if len(params) > 8 else ""), "line": line})
        if annotated:
            facts.append({"label": "类型标注", "text": f"{annotated} 个参数带类型注解", "line": line})
        facts.append({"label": "返回", "text": returns or "无注解（不猜）", "line": line})
        if decorators:
            facts.append({"label": "装饰器", "text": "、".join(f"@{d}" for d in decorators[:6]), "line": line})
        if doc_first:
            facts.append({"label": "作者写的说明", "text": doc_first, "line": line})
        # 最有信息量的两条放在前面：**真的在哪被调用**（贴源码行）+ 谁守着它。
        # 放在这儿是因为悬停提示只取前几条 facts——把"作用"沉到最后等于没有。
        usage_phrase, usage_fact = _usage_evidence(
            index, read_text=read_text, name=name, rel=rel, cross=cross
        )
        if usage_fact:
            facts.append(usage_fact)
        guard = _guard_tests(index, read_text=read_text, name=name, cross=cross)
        if guard:
            facts.append(guard)
        if action:
            facts.append({"label": "命名暗示", "text": f"`{name}` 按命名约定表示「{action}」（不是行为证据）", "line": line})
        facts.append({"label": "本文件中的出现", "text": f"同文件引用 {reads} 处（名字词边界匹配）", "line": line})
        if cross["hits"]:
            where = "、".join(f"{h['path']}:{h['line']}" for h in cross["hits"][:4])
            facts.append({"label": "跨文件调用点", "text": f"{len(cross['hits'])} 个文件：{where}", "line": cross["hits"][0]["line"]})
        elif cross["scanned"]:
            facts.append({"label": "跨文件调用点", "text": f"导入了本模块的 {cross['scanned']} 个文件里没有 `{name}(` 调用点", "line": 0})
        if asserts or raises:
            facts.append({"label": "自身契约", "text": f"{asserts} 处断言 · {raises} 处显式抛错", "line": line})
        if complexity:
            facts.append({"label": "分支复杂度", "text": str(complexity), "line": line})
        # 一句话：作者怎么写它的（docstring）优先，否则退到"命名约定 + 签名 + 调用点"
        if doc_first:
            headline = f"{doc_first}（{out['role']} {name}）"
        else:
            headline = f"{out['role']} {name}"
            if action:
                headline += f"：按命名约定是「{action}」"
        if params:
            headline += f"；入参 {len(params)} 个"
        if returns:
            headline += f"，返回 {returns}"
        if usage_phrase:
            headline += f"；{usage_phrase}"

    # ---------------- 类 / 接口 / 组件 ----------------
    elif kind in ("class", "interface", "component", "enum"):
        bases = _list("bases")
        members = [
            r for r in index.query_all("SELECT kind, name FROM symbols WHERE parent=? ORDER BY line", (symbol_id,))
        ]
        methods = [m for m in members if m["kind"] in ("method", "function")]
        props = [m for m in members if m["kind"] in ("property", "variable", "constant")]
        decorators = _list("decorators")
        doc_first = _first_sentence(doc)
        if doc_first:
            facts.append({"label": "作者写的说明", "text": doc_first, "line": line})
        # ---- 具体一点：字段 / 用在哪 / 谁守着它（旧版到这里只有"没有基类；含 0 个方法"）----
        fields = _class_fields(index, symbol_id)
        if fields:
            shown = " · ".join(f[:32] for f in fields[:4])
            if len(fields) > 4:
                shown += f" …（共 {len(fields)} 个字段）"
            facts.append({"label": "字段", "text": shown, "line": line})
        usage_phrase, usage_fact = _usage_evidence(
            index, read_text=read_text, name=name, rel=rel, cross=cross
        )
        if usage_fact:
            facts.append(usage_fact)
        guard = _guard_tests(index, read_text=read_text, name=name, cross=cross)
        if guard:
            facts.append(guard)
        facts.append({"label": "声明位置", "text": f"{scope} · {rel}", "line": line})
        if bases:
            facts.append({"label": "继承 / 混入", "text": "、".join(bases[:6]), "line": line})
        if decorators:
            facts.append({"label": "装饰器", "text": "、".join(f"@{d}" for d in decorators[:6]), "line": line})
        if members:
            facts.append({"label": "成员", "text": f"方法 {len(methods)} · 属性/常量 {len(props)}", "line": line})
        if cross["hits"]:
            where = "、".join(f"{h['path']}:{h['line']}" for h in cross["hits"][:4])
            facts.append({"label": "其它文件里的出现", "text": f"{len(cross['hits'])} 个文件：{where}", "line": cross["hits"][0]["line"]})
        # 一句话：作者说明 → 字段（数据类靠它就能读懂）→ 方法/继承 → 最后接上"用在哪"
        if doc_first:
            headline = f"{out['role']}：{doc_first}"
        elif fields:
            is_data = any("dataclass" in d or "attrs" in d or "NamedTuple" in d for d in decorators)
            field_names = "、".join(f.split(":")[0].split("=")[0].strip() for f in fields[:6])
            headline = f"{'数据类' if is_data else out['role']} {name}：字段 {field_names}"
        elif methods:
            headline = f"{out['role']} {name}：{len(methods)} 个方法"
            if bases:
                headline += f"，继承 {'、'.join(b.split('.')[-1] for b in bases[:3])}"
        elif bases:
            headline = f"{out['role']} {name}：继承 {'、'.join(b.split('.')[-1] for b in bases[:3])}"
        else:
            headline = f"{out['role']} {name}：既没有方法也没有字段（标记/占位类）"
        if usage_phrase:
            headline += f"；{usage_phrase}"

    # ---------------- 其它 kind ----------------
    else:
        facts.append({"label": "位置", "text": f"{rel}:{line}", "line": line})
        if _unparse(node):
            facts.append({"label": "源码", "text": _unparse(node)[:120], "line": line})
        headline = f"{out['role']} {name}"

    # ---- 使用情况汇总（界面直接用，不再二次推断）----
    out["usage"] = {
        "reads_same_file": reads,
        "writes_same_file": writes,
        "cross_files": [{"path": h["path"], "count": h["count"], "line": h["line"]} for h in cross["hits"]],
        "candidates": len(cross["candidates"]),
        "scanned": cross["scanned"],
        "cross_kind": cross["kind"],
    }

    # ---- 收口与诚实声明 ----
    out.update({"available": True, "headline": headline or f"{out['role']} {name}", "facts": _drop_repeats(facts, headline)[:MAX_FACTS]})
    notes = [f"依据：AST 解析 + 符号索引；作用域判定到「{scope}」"]
    if hint or action:
        notes.append("名字里的语义（前缀/角色词）属于命名约定推断，已逐条标注，不是行为证据")
    if cross["kind"] == "mention":
        notes.append(
            "跨文件使用是文本词边界匹配（认 `名字` 与 `模块.名字`，排除 `self.名字`/`cls.名字`），"
            "可能包含同名但属于别的作用域的用法"
        )
        notes.append(
            "候选文件 = 索引里指向本文件的 import 边；`from 包 import 子模块` 这种写法不会生成 import 边，"
            "因此可能漏掉使用方（`from code_agent.agent.loop import …` 这种全模块路径写法不受影响）"
        )
    notes.append("这里只回答「它是什么、拿来干什么」；会不会出错、影响到谁请看「可达与风险」")
    out["notes"] = notes
    out["basis"] = "AST（标准库）+ 符号索引 + 候选文件文本匹配"
    return out
