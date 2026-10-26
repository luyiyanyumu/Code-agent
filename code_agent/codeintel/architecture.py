"""架构建模与约束门禁（M18 · P6）。

**为什么要有硬门禁**：风险分级只是提示——"被 12 处引用"不该禁止你改公共函数。
但有几类事故是"改完能跑、事后很难收"的：反向依赖（UI 直接读数据库）、
模块成环（两个包互相 import）、悄悄改掉对外契约。这些**可以被判定**，
所以它们可以被拦下来，而且拦下来时能说清是踩了哪条规则。

规则文件放在平台侧 `~/.code-agent/code/<pid>/architecture.json`（**不写进用户仓库**：
它是"我们的约定"，不是项目产物；也可以随时导出/复制到项目里去）。

规则形状：
```json
{
  "layers": [
    {"name": "ui", "paths": ["web/src/**"], "allow": ["service"]},
    {"name": "service", "paths": ["code_agent/api/**"], "allow": ["core"]},
    {"name": "core", "paths": ["code_agent/codeintel/**"], "allow": []}
  ],
  "no_cycles": true,
  "public_api": {"paths": ["code_agent/api/**"], "require_ack": true},
  "protected_paths": [{"glob": "code_agent/storage/db.py", "reason": "schema 与迁移集中在这"}]
}
```

判定分寸（三条，都是为了"别把门禁做成噪音"）：
1. **未被任何层覆盖的文件不参与层次检查**（新目录不会因为没登记就报错）；
2. 层依赖只查**方向**：`allow` 里没有的层被依赖即 error；自身不算违反；
3. 成环只报**与本次改动有关的环**（整仓的环是历史包袱，不该在每次提交时刷屏）。

`require_ack: false` 的公共面改动降级为 warn（有人就是想让公共 API 自由演化）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_agent.codeintel import graph
from code_agent.codeintel.impact import is_public_api
from code_agent.codeintel.paths import ProjectPathError, normalize_rel

#: 规则文件（平台侧，按项目）
RULES_NAME = "architecture.json"
#: 一次判定最多报多少条（超出的记为 dropped，如实上报）
MAX_VIOLATIONS = 60


class ArchitectureError(Exception):
    """规则本身有问题（写坏了就不该生效）。"""


def rules_path(project_id: str) -> Path:
    from code_agent.config import code_dir

    return code_dir() / project_id / RULES_NAME


def _glob_match(pattern: str, rel: str) -> bool:
    """路径匹配：支持 `dir/**` / `**/x.py` / `*.py` 这些常见写法。

    用 `fnmatch` 的语义 + 一段特判：`dir/**` 要能匹配 `dir/a/b/c.py` 与 `dir/a.py`，
    而 fnmatch 的 `*` 会跨 `/`（正好符合用户直觉，不额外发明语法）。
    """
    import fnmatch

    pattern = (pattern or "").replace("\\", "/").strip("/")
    rel = (rel or "").replace("\\", "/").strip("/")
    if not pattern:
        return False
    if pattern.endswith("/**"):
        base = pattern[:-3]
        return rel == base or rel.startswith(base + "/")
    if fnmatch.fnmatchcase(rel, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatchcase(rel, pattern[3:]) or fnmatch.fnmatchcase(
            rel.rsplit("/", 1)[-1], pattern[3:]
        )
    return False


def validate_rules(rules: dict) -> list[str]:
    """规则自检：返回问题清单（空 = 可用）。宁可不生效，也不要按坏规则拦人。"""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["规则必须是一个 JSON 对象"]
    layers = rules.get("layers") or []
    if not isinstance(layers, list):
        errors.append("layers 必须是数组")
        layers = []
    names: list[str] = []
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict):
            errors.append(f"layers[{i}] 必须是对象")
            continue
        name = str(layer.get("name") or "").strip()
        if not name:
            errors.append(f"layers[{i}] 缺少 name")
        elif name in names:
            errors.append(f"层名重复：{name}")
        else:
            names.append(name)
        paths = layer.get("paths") or []
        if not isinstance(paths, list) or not paths:
            errors.append(f"layers[{i}]（{name or i}）缺少 paths")
        else:
            for raw in paths:
                try:
                    normalize_rel(str(raw).replace("**", "x").replace("*", "x"))
                except ProjectPathError:
                    errors.append(f"layers[{i}] 的路径不允许包含 ..：{raw}")
        allow = layer.get("allow") or []
        if not isinstance(allow, list):
            errors.append(f"layers[{i}] 的 allow 必须是数组")
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict):
            continue
        for target in layer.get("allow") or []:
            if str(target) not in names:
                errors.append(f"layers[{i}] 的 allow 指向不存在的层：{target}")
    api = rules.get("public_api")
    if api is not None and not isinstance(api, dict):
        errors.append("public_api 必须是对象")
    for i, item in enumerate(rules.get("protected_paths") or []):
        if not isinstance(item, dict) or not str(item.get("glob") or "").strip():
            errors.append(f"protected_paths[{i}] 需要 glob")
            continue
        if ".." in str(item["glob"]):
            errors.append(f"protected_paths[{i}] 的 glob 不允许包含 ..")
    if "no_cycles" in rules and not isinstance(rules["no_cycles"], bool):
        errors.append("no_cycles 必须是布尔值")
    return errors


def load_rules(project_id: str) -> dict:
    """读规则。文件不存在 = 空规则（不拦任何东西），坏 JSON = 报错（不静默放过）。"""
    path = rules_path(project_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ArchitectureError(f"架构规则不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ArchitectureError("架构规则必须是 JSON 对象")
    return data


def save_rules(project_id: str, rules: dict) -> dict:
    errors = validate_rules(rules)
    if errors:
        raise ArchitectureError("；".join(errors[:6]))
    path = rules_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    return rules


def layer_of(rules: dict, rel: str) -> str:
    """文件属于哪一层（先声明先匹配；没命中返回空串 = 不受层次约束）。"""
    for layer in rules.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        for pattern in layer.get("paths") or []:
            if _glob_match(str(pattern), rel):
                return str(layer.get("name") or "")
    return ""


def _allow_map(rules: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for layer in rules.get("layers") or []:
        if isinstance(layer, dict):
            out[str(layer.get("name") or "")] = [str(a) for a in (layer.get("allow") or [])]
    return out


def layer_edges(
    index: Any, rules: dict, *, files: list[str] | None = None
) -> list[dict]:
    """被改动文件产生的跨层依赖（只看**改动文件**发出的边）。

    只看改动侧：`allow` 是"我们约定这个方向可以"，历史代码里已有的反向依赖
    不该在改别的文件的 PR 里被翻出来——那会让人学会忽略门禁。
    """
    if not rules.get("layers"):
        return []
    allow = _allow_map(rules)
    targets = {str(f).replace("\\", "/").strip("/") for f in (files or [])}
    out: list[dict] = []
    for rel in sorted(targets):
        src_layer = layer_of(rules, rel)
        if not src_layer:
            continue
        for edge in index.edges(kind="imports", src=f"file:{rel}"):
            dst = str(edge["dst"]).replace("file:", "")
            if dst == rel:
                continue
            dst_layer = layer_of(rules, dst)
            if not dst_layer or dst_layer == src_layer:
                continue
            if dst_layer in (allow.get(src_layer) or []):
                continue
            out.append(
                {
                    "rule": "layer",
                    "severity": "error",
                    "path": rel,
                    "to": dst,
                    "from_layer": src_layer,
                    "to_layer": dst_layer,
                    "message": f"{src_layer} 不允许依赖 {dst_layer}（当前允许：{'、'.join(allow.get(src_layer) or []) or '无'}）",
                }
            )
    return out


def find_cycles(index: Any, rules: dict, *, files: list[str] | None = None, limit: int = 6) -> list[dict]:
    """模块级依赖环：只报**与改动文件相关**的环。

    模块 = 目录（`graph.module_of`）。整仓的历史环太多，每次提交都列出来只会让人
    把门禁当噪音；这里只从改动文件所在模块出发找环。
    """
    if not rules.get("no_cycles", True) or not files:
        return []
    graph_map: dict[str, set[str]] = {}
    for edge in index.edges(kind="imports"):
        src = str(edge["src"]).replace("file:", "")
        dst = str(edge["dst"]).replace("file:", "")
        if not src or not dst:
            continue
        a, b = graph.module_of(src), graph.module_of(dst)
        if a == b:
            continue
        graph_map.setdefault(a, set()).add(b)

    starts = sorted({graph.module_of(str(f)) for f in files if f})
    found: list[dict] = []
    seen_cycles: set[tuple[str, ...]] = set()
    for start in starts:
        stack: list[str] = [start]

        def _walk(node: str) -> None:
            if len(found) >= limit:
                return
            for nxt in sorted(graph_map.get(node, ())):
                if nxt == start and len(stack) > 1:
                    cycle = tuple(stack + [start])
                    key = tuple(sorted(set(cycle)))
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        found.append(
                            {
                                "rule": "cycle",
                                "severity": "error",
                                "modules": list(cycle),
                                "message": "模块成环：" + " → ".join(cycle),
                            }
                        )
                    continue
                if nxt in stack or len(stack) > 6:
                    continue
                stack.append(nxt)
                _walk(nxt)
                stack.pop()

        _walk(start)
    return found


def public_api_changes(
    index: Any, rules: dict, *, symbols: list[dict]
) -> list[dict]:
    """公共面变更：规则里圈定的路径 + 对外承诺的符号。"""
    api = rules.get("public_api")
    if not api:
        return []
    severity = "error" if api.get("require_ack", True) else "warn"
    paths = [str(p) for p in (api.get("paths") or [])]
    out: list[dict] = []
    for row in symbols:
        rel = str(row.get("path") or "")
        if paths and not any(_glob_match(p, rel) for p in paths):
            continue
        public, why = is_public_api(row)
        if not public:
            continue
        out.append(
            {
                "rule": "public_api",
                "severity": severity,
                "path": rel,
                "symbol_id": row.get("id") or "",
                "symbol": row.get("name") or "",
                "message": f"改动公共面：{row.get('kind')} {row.get('qname') or row.get('name')}（{why}）",
            }
        )
    return out


def protected_hits(rules: dict, *, files: list[str]) -> list[dict]:
    out: list[dict] = []
    for item in rules.get("protected_paths") or []:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("glob") or "")
        for rel in files or []:
            if _glob_match(pattern, rel):
                out.append(
                    {
                        "rule": "protected",
                        "severity": "error",
                        "path": rel,
                        "glob": pattern,
                        "reason": str(item.get("reason") or ""),
                        "message": f"{rel} 是保护路径（{item.get('reason') or '规则要求谨慎'}）",
                    }
                )
    return out


def evaluate(
    index: Any,
    rules: dict,
    *,
    files: list[str],
    changed_symbols: list[dict] | None = None,
    protected: bool = True,
) -> dict:
    """跑一次门禁。返回 violations（带 severity 与理由）+ ok。"""
    rules = rules or {}
    files = [str(f).replace("\\", "/").strip("/") for f in (files or []) if str(f).strip()]
    violations: list[dict] = []
    violations += layer_edges(index, rules, files=files)
    if rules.get("no_cycles", True):
        violations += find_cycles(index, rules, files=files)
    violations += public_api_changes(index, rules, symbols=list(changed_symbols or []))
    if protected:
        violations += protected_hits(rules, files=files)
    errors = [v for v in violations if v.get("severity") == "error"]
    warns = [v for v in violations if v.get("severity") != "error"]
    return {
        "configured": bool(rules.get("layers") or rules.get("public_api") or rules.get("protected_paths")),
        "ok": not errors,
        "violations": violations[:MAX_VIOLATIONS],
        "dropped": max(0, len(violations) - MAX_VIOLATIONS),
        "counts": {"error": len(errors), "warn": len(warns)},
        "checked": {"files": len(files), "symbols": len(changed_symbols or [])},
    }


def protected_by_rules(rules: dict, files: list[str]) -> list[dict]:
    """给风险分级复用的"保护路径"命中（只取 glob + reason）。"""
    out: list[dict] = []
    for item in rules.get("protected_paths") or []:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("glob") or "")
        for rel in files or []:
            if _glob_match(pattern, rel):
                out.append({"glob": pattern, "reason": str(item.get("reason") or "")})
    return out
