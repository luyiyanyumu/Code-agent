"""JS / TS / JSX / TSX / Vue 解析器（M18 · P1）：启发式，但**不假装精确**。

为什么不用 ast：Python 的标准库给了 `ast`，JS 生态没有零依赖的解析器；
引入 babel/tree-sitter 会把"离线可用、零新依赖"这条底线破掉。所以这里用
"顶层声明扫描 + 花括号配对"的启发式，并**给每个符号打 confidence 标记**：

- high   ：语法明确的声明（class / function / interface / type / enum / import）
- medium ：箭头函数、对象式组件、Vue 宏（defineProps/defineEmits）推断出来的
- low    ：仅靠正则捞到的成员/变量

地图上低置信度节点会以不同样式呈现，模型上下文里也会标注——
**不把启发式结果当事实**，比"看起来什么都解析出来了"重要得多。

已知不做的：TS 类型体操、装饰器语义、动态 import 目标、JSX 内联组件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: 顶层声明模式（顺序即优先级）
_RE_CLASS = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")
_RE_INTERFACE = re.compile(r"^(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")
_RE_TYPE = re.compile(r"^(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*=")
_RE_ENUM = re.compile(r"^(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")
_RE_FUNC = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")
_RE_CONST_FUNC = re.compile(
    r"^(?:export\s+)?(?:default\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?function\b"
)
_RE_ARROW = re.compile(
    r"^(?:export\s+)?(?:default\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
    r"(?:async\s+)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
_RE_HOOK = re.compile(
    r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
    r"(ref|shallowRef|reactive|computed|readonly|toRefs|withDefaults|defineStore|use[A-Z]\w*)\s*[(<]"
)
_RE_VAR = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)")
_RE_DEFAULT_OBJ = re.compile(r"^(?:export\s+)?default\s*\{")
_RE_CLASS_MEMBER = re.compile(
    r"^\s*(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?([A-Za-z_$][\w$]*)\s*\([^;{]*\)\s*(?::[^{=>]+)?\s*\{"
)
_RE_IMPORT_FROM = re.compile(r"""^\s*(?:import|export)\s[^'"]*from\s*['"]([^'"]+)['"]""")
_RE_IMPORT_BARE = re.compile(r"""^\s*import\s*['"]([^'"]+)['"]""")
_RE_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
_RE_PROPS_TYPE = re.compile(r"defineProps\s*<\s*\{(.*?)\}\s*>", re.S)
#: 对象式 defineProps 的 props 里可能嵌套 `{ type: String, required: true }`，
#: 所以不能用 `[^}]*` 截到第一个右花括号（那样只会拿到 "title: {" 半截）
_RE_PROPS_OBJ = re.compile(r"defineProps\s*\(\s*(\{.*?\})\s*\)", re.S)
_RE_EMITS = re.compile(r"defineEmits\s*[<(]([^>)]*)[>)]", re.S)
_RE_EMIT_NAME = re.compile(r"""['"]([\w:-]+)['"]""")
_RE_SFC_BLOCK = re.compile(r"<script([^>]*)>(.*?)</script>", re.S)


@dataclass
class _LineState:
    """跨行清理状态。**必须跨行保留**：

    - 块注释天然跨行；
    - 模板字符串（反引号）可以跨行，而 `${…}` 里就有花括号——不跨行保留引号状态，
      一个多行的 `` `${`` 就会让深度计数一路走负，后续函数体内的声明会被误判成
      顶层符号（真机上就是这么把 api.js 的 `proto/url` 画到地图顶层的）。

    单引号/双引号字符串不允许裸换行，所以它们到行尾强制复位：
    否则注释里一个孤立的撇号会"毒掉"后面整个文件。
    """

    block: bool = False
    quote: str = ""


def _clean_line(line: str, state: _LineState, *, keep_strings: bool = False) -> str:
    """去掉注释与字符串内容后再数花括号：字符串/注释里的 `{` 不能算进深度。"""
    out: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        two = line[i : i + 2]
        if state.block:
            if two == "*/":
                state.block = False
                i += 2
            else:
                i += 1
            continue
        if state.quote:
            if ch == "\\":
                if keep_strings:
                    out.append(line[i : i + 2])
                i += 2
                continue
            if keep_strings:
                out.append(ch)
            if ch == state.quote:
                state.quote = ""
            i += 1
            continue
        if two == "//":
            break
        if two == "/*":
            state.block = True
            i += 2
            continue
        if ch in "\"'`":
            state.quote = ch
            if keep_strings:
                out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    if state.quote and state.quote != "`":
        state.quote = ""
    return "".join(out)


def _find_block_end(lines: list[str], start_idx: int) -> int:
    """从 start_idx（0-based）起找花括号闭合行（1-based）。找不到返回起始行。

    自带独立状态：**只在"确定不位于字符串/注释内部"的行上调它**（顶层声明就是
    这种行）。这样它不会污染调用方的跨行状态——主扫描循环的 depth 依赖那个状态，
    被就地推进一次就会错位（这类 bug 表现为"某些函数内变量跑到了地图顶层"）。
    """
    state = _LineState()
    depth = 0
    seen_open = False
    for idx in range(start_idx, len(lines)):
        code = _clean_line(lines[idx], state)
        opens = code.count("{")
        closes = code.count("}")
        if opens:
            seen_open = True
        depth += opens - closes
        if seen_open and depth <= 0:
            return idx + 1
        if idx == start_idx and not seen_open:
            return start_idx + 1
    return len(lines)


def _signature_of(line: str) -> str:
    text = line.strip()
    return text[:200]


def _classify_var(name: str) -> str:
    return "constant" if name.isupper() else "variable"


def _scan(rel_path: str, lines: list[str], line_offset: int, container: tuple[str, str] | None) -> tuple[list[dict], list[dict]]:
    """扫描顶层声明。container=(parent_id, owner_prefix) 时用于 Vue 组件内部。"""
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    state = _LineState()
    import_state = _LineState()
    pending_import: list[str] = []
    depth = 0
    parent_id = container[0] if container else ""
    owner = container[1] if container else ""
    # 非 Vue 的 JS 文件用 `export default {` 时，用文件名兜底当组件名
    stem = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    for idx, raw in enumerate(lines):
        line_no = line_offset + idx + 1
        code = _clean_line(raw, state)
        import_line = _clean_line(raw, import_state, keep_strings=True).strip()
        stripped = code.strip()

        if not stripped:
            continue

        # 顶层导入/依赖：必须在**原始行**上匹配——_clean_line 会把字符串内容
        # 一起清掉（为了正确数花括号），于是 `from 'vue'` 里的 'vue' 也一并消失
        # 字符串保留、注释去掉；命名导入常写成多行，必须按整条声明匹配。
        raw_stripped = import_line
        import_candidate = " ".join([*pending_import, import_line])
        found_import = False
        for regex in (_RE_IMPORT_FROM, _RE_IMPORT_BARE):
            m = regex.match(import_candidate)
            if m:
                imports.append({"module": m.group(1), "level": 0, "names": [], "raw": import_candidate[:160]})
                found_import = True
                break
        if found_import:
            pending_import = []
        elif pending_import:
            pending_import.append(import_line)
            if len(pending_import) > 30 or ";" in stripped:
                pending_import = []
        elif re.match(r"^(?:import\b(?!\s*[.(])|export\s+(?:\*|\{))", stripped):
            pending_import = [import_line]
        for m in _RE_REQUIRE.finditer(raw_stripped):
            imports.append({"module": m.group(1), "level": 0, "names": [], "raw": f"require('{m.group(1)}')"})

        # 非顶层（在块里）不认声明：块内声明属于实现细节，画进地图只增噪声
        if depth != 0:
            depth += code.count("{") - code.count("}")
            continue

        end_line = _find_block_end(lines, idx)

        def add(kind: str, name: str, confidence: str, **extra: Any) -> str:
            full = f"{owner}.{name}" if owner else name
            sid = f"sym:{rel_path}#{full}"
            symbols.append(
                {
                    "id": sid,
                    "kind": kind,
                    "name": name,
                    "qname": full,
                    "line": line_no,
                    "end_line": line_offset + end_line,
                    "parent": parent_id,
                    "confidence": confidence,
                    "signature": _signature_of(stripped),
                    "doc": "",
                    "params": [],
                    "returns": "",
                    "decorators": [],
                    "bases": [],
                    "complexity": 0,
                    **extra,
                }
            )
            return sid

        m = _RE_CLASS.match(stripped)
        if m:
            name = m.group(1)
            bases: list[str] = []
            ext = re.search(r"extends\s+([A-Za-z_$][\w$.]*)", stripped)
            if ext:
                bases.append(ext.group(1))
            sid = add("class", name, "high", bases=bases)
            try:
                symbols.extend(
                    _scan_class_members(rel_path, lines, idx, end_line, sid, name, line_offset)
                )
            except Exception:
                pass
            depth += code.count("{") - code.count("}")
            continue

        m = _RE_INTERFACE.match(stripped)
        if m:
            add("interface", m.group(1), "high")
            depth += code.count("{") - code.count("}")
            continue

        m = _RE_TYPE.match(stripped)
        if m:
            add("type", m.group(1), "high")
            depth += code.count("{") - code.count("}")
            continue

        m = _RE_ENUM.match(stripped)
        if m:
            add("enum", m.group(1), "high")
            depth += code.count("{") - code.count("}")
            continue

        m = _RE_FUNC.match(stripped)
        if m:
            add("function", m.group(1), "high")
            depth += code.count("{") - code.count("}")
            continue

        m = _RE_CONST_FUNC.match(stripped)
        if m:
            add("function", m.group(1), "high")
            depth += code.count("{") - code.count("}")
            continue

        m = _RE_ARROW.match(stripped)
        if m:
            add("function", m.group(1), "medium")
            depth += code.count("{") - code.count("}")
            continue

        m = _RE_HOOK.match(stripped)
        if m:
            add("hook", m.group(1), "medium", signature=_signature_of(stripped), hook=m.group(2))
            depth += code.count("{") - code.count("}")
            continue

        if _RE_DEFAULT_OBJ.match(stripped):
            add("component", stem, "medium", signature="export default { … }（选项式组件）")
            depth += code.count("{") - code.count("}")
            continue

        m = _RE_VAR.match(stripped)
        if m:
            add(_classify_var(m.group(1)), m.group(1), "medium")
            depth += code.count("{") - code.count("}")
            continue

        depth += code.count("{") - code.count("}")

    return symbols, imports


def _scan_class_members(
    rel_path: str,
    lines: list[str],
    class_idx: int,
    class_end: int,
    class_id: str,
    class_name: str,
    line_offset: int = 0,
) -> list[dict[str, Any]]:
    """在类体内捞方法（启发式：`name(...) {` 形态）。"""
    members: list[dict[str, Any]] = []
    state = _LineState()
    for idx in range(class_idx + 1, min(class_end, len(lines))):
        code = _clean_line(lines[idx], state)
        m = _RE_CLASS_MEMBER.match(code)
        if not m:
            continue
        name = m.group(1)
        if name in ("if", "for", "while", "switch", "catch", "return"):
            continue
        members.append(
            {
                "id": f"sym:{rel_path}#{class_name}.{name}",
                "kind": "method",
                "name": name,
                "qname": f"{class_name}.{name}",
                "line": line_offset + idx + 1,
                "end_line": line_offset + _find_block_end(lines, idx),
                "parent": class_id,
                "confidence": "medium",
                "signature": code.strip()[:200],
                "doc": "",
                "params": [],
                "returns": "",
                "decorators": [],
                "bases": [],
                "complexity": 0,
            }
        )
    return members


def _extract_sfc_script(text: str) -> tuple[str, int, str]:
    """取 <script setup>（优先）或 <script> 段，返回 (脚本内容, 行偏移, lang 属性)。"""
    blocks = list(_RE_SFC_BLOCK.finditer(text))
    if not blocks:
        return "", 0, ""
    setup = next((b for b in blocks if "setup" in b.group(1)), None)
    m = setup or blocks[0]
    attrs = m.group(1) or ""
    lang_match = re.search(r"lang\s*=\s*['\"](\w+)['\"]", attrs)
    script = m.group(2) or ""
    offset = text[: m.start(2)].count("\n")
    return script, offset, (lang_match.group(1) if lang_match else "")


def _parse_props(script: str) -> list[dict[str, Any]]:
    props: list[dict[str, Any]] = []
    seen: set[str] = set()
    m = _RE_PROPS_TYPE.search(script)
    if m:
        for piece in re.split(r"[;\n,]", m.group(1)):
            mm = re.match(r"\s*([A-Za-z_$][\w$]*)\s*(\??)\s*:\s*(.+)", piece)
            if not mm:
                continue
            name, optional, type_text = mm.group(1), mm.group(2), mm.group(3).strip()
            if name in seen:
                continue
            seen.add(name)
            props.append({"name": name, "type": type_text[:60], "required": optional != "?"})
    if not props:
        m = _RE_PROPS_OBJ.search(script)
        if m:
            body = m.group(1)
            for mm in re.finditer(r"([A-Za-z_$][\w$]*)\s*:\s*\{([^{}]*)\}", body):
                name, inner = mm.group(1), mm.group(2)
                if name in seen:
                    continue
                seen.add(name)
                type_m = re.search(r"type\s*:\s*([A-Za-z_$][\w$.]*)", inner)
                props.append(
                    {
                        "name": name,
                        "type": type_m.group(1) if type_m else "",
                        "required": "required:true" in inner.replace(" ", ""),
                    }
                )
            # 简写形式：name: String / count: Number
            for mm in re.finditer(r"([A-Za-z_$][\w$]*)\s*:\s*([A-Z][\w$.]*)", body):
                name, type_text = mm.group(1), mm.group(2)
                if name in seen or name in ("type", "required", "default", "validator"):
                    continue
                seen.add(name)
                props.append({"name": name, "type": type_text, "required": False})
    return props


def _parse_emits(script: str) -> list[str]:
    m = _RE_EMITS.search(script)
    if not m:
        return []
    names = _RE_EMIT_NAME.findall(m.group(1))
    return list(dict.fromkeys(names))[:30]


def parse_vue(rel_path: str, text: str) -> dict[str, Any]:
    """Vue SFC：组件本身是一个符号，脚本里的声明作为它的成员。"""
    result: dict[str, Any] = {"symbols": [], "imports": [], "entrypoint": False, "error": "", "confidence": "medium"}
    component = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    total = text.count("\n") + 1
    script, offset, _lang = _extract_sfc_script(text)
    cid = f"sym:{rel_path}#{component}"
    result["symbols"].append(
        {
            "id": cid,
            "kind": "component",
            "name": component,
            "qname": component,
            "line": 1,
            "end_line": total,
            "parent": "",
            "confidence": "high",
            "signature": "<template> + <script setup> 组件",
            "doc": "",
            "params": [],
            "returns": "",
            "decorators": [],
            "bases": [],
            "complexity": 0,
            "props": _parse_props(script) if script else [],
            "emits": _parse_emits(script) if script else [],
        }
    )
    if not script:
        return result
    lines = script.split("\n")
    inner_symbols, imports = _scan(rel_path, lines, offset, (cid, component))
    result["symbols"].extend(inner_symbols)
    result["imports"] = imports
    return result


def parse(rel_path: str, text: str) -> dict[str, Any]:
    """解析 JS/TS/Vue 文件。启发式结果带 confidence，不谎报精确度。"""
    if rel_path.lower().endswith(".vue"):
        return parse_vue(rel_path, text)
    lines = text.split("\n")
    symbols, imports = _scan(rel_path, lines, 0, None)
    is_ts = rel_path.lower().endswith((".ts", ".tsx"))
    return {
        "symbols": symbols,
        "imports": imports,
        "entrypoint": False,
        "error": "",
        "confidence": "high" if is_ts else "medium",
    }
