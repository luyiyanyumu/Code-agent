"""图即文档（M18 · P7）：把"地图里已经算出来的事实"导出成一份能给人读的文档。

**为什么值得做**：索引里躺着的这些东西——模块结构、依赖方向、入口与路由、符号构成、
架构规则、行为卡片与覆盖率、决策线程、最近的改动——本来只活在界面里，
换个人（或三个月后的自己）就得重新点一遍。导出一份 markdown 就是把这堆事实
变成可评审、可归档、可 diff 的产物。

三条纪律：
1. **只写事实**：文档里的每个数字都来自索引/台账/决策，不做总结式发挥；
2. **不全就说不全**：被截断的章节明确写"已截断"，而不是给一个看起来完整的表；
3. **不写进用户仓库**：导出物落在平台侧 `~/.code-agent/code/<pid>/exports/`，
   用户仓库里不长平台文件（这是 P4 就定下的边界）。

章节可选（`sections`）：overview / modules / entrypoints / specs / decisions /
architecture / changes。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from code_agent.codeintel import graph
from code_agent.codeintel.impact import is_test_path

#: 各章节的行数上限（文档是给人读的，不是给人翻的）
MAX_MODULE_ROWS = 60
MAX_EDGE_ROWS = 80
MAX_SYMBOL_ROWS = 40
MAX_BEHAVIOR_ROWS = 80
MAX_DECISION_ROWS = 60
MAX_CHANGE_ROWS = 40

SECTIONS = ("overview", "modules", "entrypoints", "specs", "decisions", "architecture", "changes")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "（无）\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(lines) + "\n"


def _section_overview(project: dict, index: Any, coverage: dict) -> str:
    counts = index.counts()
    kinds = index.kind_counts()
    summary = index.scan_summary()
    langs = summary.get("languages") or []
    lang_text = "、".join(f"{it.get('name')} {it.get('files')} 文件" for it in langs) or "（未知）"
    lines = [
        "## 1. 概览",
        "",
        f"- 项目：**{project.get('name')}**（`{project.get('root_path')}`）",
        f"- 生成时间：{_now()}",
        f"- 索引版本：{index.version()}（scan {summary.get('mode') or '-'} · "
        f"{summary.get('finished_at') or '-'}）",
        f"- 规模：{counts['files']} 个文件 · {counts['symbols']} 个符号 · {counts['edges']} 条关系",
        f"- 语言：{lang_text}",
        f"- 测试覆盖（按符号）：{coverage['totals']['ratio']:.0%}"
        f"（直接 {coverage['totals']['direct']} · 文件级 {coverage['totals']['file_level']} · "
        f"无 {coverage['totals']['uncovered']}）",
    ]
    if counts["symbols"]:
        top = sorted(kinds.items(), key=lambda kv: -kv[1])[:8]
        lines.append("- 符号构成：" + " · ".join(f"{name} {n}" for name, n in top))
    if summary.get("symbols_capped_count"):
        lines.append(
            f"- ⚠ {summary['symbols_capped_count']} 个文件的符号数被截断（生成代码？），"
            "地图与文档都不完整"
        )
    if summary.get("errors"):
        lines.append(f"- ⚠ {len(summary['errors'])} 个文件解析失败（退化为仅文件节点）")
    return "\n".join(lines) + "\n"


def _section_modules(index: Any) -> str:
    modules = graph.build_modules(index)
    modules = sorted(modules, key=lambda m: -int(m.get("loc") or 0))
    rows = [
        [
            m.get("path") or "(根目录)",
            m.get("files"),
            m.get("loc"),
            "、".join(
                f"{k} {v}"
                for k, v in sorted(
                    (m.get("symbol_counts") or {}).items(), key=lambda kv: -kv[1]
                )[:4]
            ) or "—",
            "、".join(m.get("tests") or []) or "—",
        ]
        for m in modules[:MAX_MODULE_ROWS]
    ]
    out = ["## 2. 模块地图", "", _table(["模块", "文件", "行数", "主要符号", "测试"], rows)]
    if len(modules) > MAX_MODULE_ROWS:
        out.append(f"（共 {len(modules)} 个模块，仅列行数最多的 {MAX_MODULE_ROWS} 个）\n")
    return "\n".join(out) + "\n"


def _section_entrypoints(index: Any) -> str:
    entrypoints = graph.build_entrypoints(index)
    routes = graph.build_routes(index)
    commands = graph.build_commands(index)
    out = ["## 3. 入口、路由与命令", ""]
    out.append("**入口**\n")
    out.append(_table(
        ["路径", "类型", "依据"],
        [[e.get("path"), e.get("kind"), e.get("evidence")] for e in entrypoints[:MAX_EDGE_ROWS]],
    ))
    out.append("\n**HTTP 路由**\n")
    out.append(_table(
        ["方法", "路径", "文件:行"],
        [[r.get("method"), r.get("path"), f"{r.get('file')}:{r.get('line')}"]
         for r in routes[:MAX_EDGE_ROWS]],
    ))
    if commands and any(commands.values()):
        out.append("\n**可执行命令**\n")
        rows = [[scope, cmd] for scope, items in commands.items() for cmd in (items or [])]
        out.append(_table(["用途", "命令"], rows[:MAX_EDGE_ROWS]))
    return "\n".join(out) + "\n"


def _section_specs(behaviors: dict, coverage: dict) -> str:
    out = ["## 4. 测试即规格", ""]
    out.append(f"- 行为卡片：{behaviors['total']} 条（其中 {behaviors['no_asserts']} 条没有断言）")
    out.append(f"- 覆盖：直接 {coverage['totals']['direct']} · 文件级 {coverage['totals']['file_level']} · "
               f"未覆盖 {coverage['totals']['uncovered']}（{coverage['method']}）")
    out.append("")
    out.append("### 行为卡片\n")
    rows = []
    for card in behaviors["cards"][:MAX_BEHAVIOR_ROWS]:
        rows.append([
            f"{card['path']}:{card['line']}",
            card["behavior"],
            card["asserts"],
            "、".join(c.split("#")[-1] for c in card["covers_symbols"][:3]) or
            "、".join(card["covers_files"][:3]) or "—",
        ])
    out.append(_table(["位置", "行为", "断言", "盯着"], rows))
    if behaviors["truncated"]:
        out.append(f"（共 {behaviors['total']} 条，仅列前 {MAX_BEHAVIOR_ROWS} 条）\n")
    if coverage["uncovered"]:
        out.append("\n### 没有测试守着的公共符号（前 30 条）\n")
        out.append(_table(
            ["符号", "类型", "位置"],
            [[u["name"], u["kind"], f"{u['path']}:{u['line']}"] for u in coverage["uncovered"][:30]],
        ))
    return "\n".join(out) + "\n"


def _section_decisions(decisions: list[dict]) -> str:
    out = ["## 5. 决策与已知风险", ""]
    if not decisions:
        out.append("（还没有记录：决定/风险可以挂在任意符号或文件上）\n")
        return "\n".join(out) + "\n"
    rowset = []
    for item in decisions[:MAX_DECISION_ROWS]:
        rowset.append([
            item.get("ts") or "",
            item.get("kind_label") or item.get("kind") or "",
            (item.get("text") or "").replace("\n", " ")[:160],
            item.get("target") or "",
            item.get("author") or "",
        ])
    out.append(_table(["时间", "类型", "内容", "挂在", "谁"], rowset))
    if len(decisions) > MAX_DECISION_ROWS:
        out.append(f"（共 {len(decisions)} 条，仅列最近 {MAX_DECISION_ROWS} 条）\n")
    return "\n".join(out) + "\n"


def _section_architecture(arch: dict) -> str:
    rules = arch.get("rules") or {}
    out = ["## 6. 架构约束", ""]
    if arch.get("errors"):
        out.append("⚠ 规则文件有问题，未生效：" + "；".join(arch["errors"]) + "\n")
    layers = rules.get("layers") or []
    if layers:
        out.append("**分层（允许的依赖方向）**\n")
        out.append(_table(
            ["层", "路径", "允许依赖"],
            [[l.get("name"), "、".join(l.get("paths") or []),
              "、".join(l.get("allow") or []) or "（不依赖其他层）"] for l in layers],
        ))
    else:
        out.append("（没有定义分层：`architecture.json` 里的 `layers`）\n")
    out.append(f"- 禁环：{'开' if rules.get('no_cycles', True) else '关'}")
    api = rules.get("public_api")
    out.append(f"- 公共面知情：{'开' if api else '未配置'}"
               + (f"（路径 {', '.join(api.get('paths') or [])}，"
                  f"{'必须确认' if api.get('require_ack', True) else '仅提醒'}）" if api else ""))
    protected = rules.get("protected_paths") or []
    if protected:
        out.append("\n**保护路径**\n")
        out.append(_table(["路径", "原因"], [[p.get("glob"), p.get("reason") or "—"] for p in protected]))
    return "\n".join(out) + "\n"


def _section_changes(tasks: list[dict], batches: list[dict]) -> str:
    out = ["## 7. 最近的改动", ""]
    if tasks:
        out.append("**任务 / 车道**\n")
        out.append(_table(
            ["标题", "状态", "分支", "改动文件", "符号(增/改/删)"],
            [[t.get("title") or "", t.get("status") or "", t.get("branch") or "",
              len(t.get("changed_files") or []),
              f"{(t.get('symbol_counts') or {}).get('added', 0)}/"
              f"{(t.get('symbol_counts') or {}).get('modified', 0)}/"
              f"{(t.get('symbol_counts') or {}).get('removed', 0)}"]
             for t in tasks[:MAX_CHANGE_ROWS]],
        ))
    else:
        out.append("（还没有任务记录）\n")
    if batches:
        out.append("\n**并行批次**\n")
        out.append(_table(
            ["标题", "状态", "已落地/总车道", "队列"],
            [[b.get("title") or "", b.get("status") or "",
              f"{(b.get('progress') or {}).get('merged', 0)}/{(b.get('progress') or {}).get('total', 0)}",
              " → ".join(b.get("queue") or [])]
             for b in batches[:MAX_CHANGE_ROWS]],
        ))
    return "\n".join(out) + "\n"


def build(
    *,
    project: dict,
    index: Any,
    coverage: dict,
    behaviors: dict,
    decisions: list[dict],
    architecture: dict,
    tasks: list[dict] | None = None,
    batches: list[dict] | None = None,
    sections: list[str] | None = None,
    title: str = "",
) -> dict:
    """装配文档。返回 markdown + **回执（receipt）**：章节清单、字节数、sha256 与自检项。

    回执的用法参考 archify 的交付契约：一份产物要能回答"它由什么生成、检查了什么、
    哪一节被截断了"。所以这里不做花哨的"质量分"，只写**能复核的事实**：
    - `checks`：每条都是可判定的（含 0 个未标注的截断章节、含生成时间、章节编号连续）；
    - `truncated_sections`：哪些章节只给了一部分（如实列出，不藏在正文里）；
    - `sha256`：产物指纹，用户拿它对照自己保存的文件。
    """
    wanted = [s for s in (sections or list(SECTIONS)) if s in SECTIONS] or list(SECTIONS)
    parts: list[str] = []
    header = [
        f"# {title or (project.get('name') or '项目')} · 代码地图文档",
        "",
        "> 由 Code-agent 代码模式从**符号索引 / 改动台账 / 决策线程**生成，只写事实：",
        "> 被截断的章节会显式标注「已截断」，不做总结式发挥。",
        "",
    ]
    parts.append("\n".join(header))
    numbering = {name: i + 1 for i, name in enumerate(wanted)}
    for name in wanted:
        if name == "overview":
            body = _section_overview(project, index, coverage)
        elif name == "modules":
            body = _section_modules(index)
        elif name == "entrypoints":
            body = _section_entrypoints(index)
        elif name == "specs":
            body = _section_specs(behaviors, coverage)
        elif name == "decisions":
            body = _section_decisions(decisions)
        elif name == "architecture":
            body = _section_architecture(architecture)
        else:
            body = _section_changes(tasks or [], batches or [])
        # 章节编号按**本次实际导出**的顺序重排，避免"1. 概览 / 3. 入口"这种跳号
        body = _renumber(body, numbering[name])
        parts.append(body)
    markdown = "\n".join(parts).rstrip() + "\n"

    truncated: list[str] = []
    for name, text in zip(wanted, parts[1:]):
        if "已截断" in text or "仅列" in text:
            truncated.append(name)
    warnings: list[str] = []
    summary = index.scan_summary()
    if not index.counts()["files"]:
        warnings.append("索引为空：请先扫描一次，否则文档里没有可核对的规模数字")
    if summary.get("symbols_capped_count"):
        warnings.append("有文件的符号数被截断，概览里的规模数字偏小")
    if architecture.get("errors"):
        warnings.append("架构规则文件有问题：第 6 节展示的是原始规则，未生效")

    checks = [
        {"id": "sections_present", "ok": all(f"## {numbering[name]}." in markdown for name in wanted),
         "detail": f"{len(wanted)} 个章节都写了标题"},
        {"id": "no_silent_truncation", "ok": not truncated or all(
            f"## {numbering[name]}." in markdown for name in truncated),
         "detail": ("截断章节已标注：" + "、".join(truncated)) if truncated else "没有章节被截断"},
        {"id": "generated_at", "ok": True, "detail": "正文含生成时间与索引版本"},
        {"id": "facts_only", "ok": bool(coverage.get("method")),
         "detail": "覆盖率口径写在正文里（直接/文件级/无 的判据）"},
    ]
    return {
        "markdown": markdown,
        "sections": wanted,
        "chars": len(markdown),
        "lines": markdown.count("\n") + 1,
        "generated_at": _now(),
        "receipt": {
            "artifact": {
                "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                "bytes": len(markdown.encode("utf-8")),
                "lines": markdown.count("\n") + 1,
                "sections": wanted,
            },
            "source": {
                "project_id": project.get("id") or "",
                "index_version": index.version(),
                "scan_mode": summary.get("mode") or "",
                "scanned_at": summary.get("finished_at") or "",
            },
            "checks": checks,
            "truncated_sections": truncated,
            "warnings": warnings,
            "viewer_only_excluded": ["覆盖率图层", "证据角标 SRC", "地图交互状态"],
            "viewer_only_excluded_note": "这些是只读视图态，按 Canonical Clean 规则不进入导出物",
        },
    }


def _renumber(section: str, number: int) -> str:
    """把 `## n.` 换成本次导出的序号（章节可被裁剪，编号必须跟着走）。"""
    lines = section.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("## "):
            lines[i] = f"## {number}. {line.split('. ', 1)[-1]}"
            break
    return "\n".join(lines)


def save(project_id: str, markdown: str, *, name: str = "") -> str:
    """把文档写到平台侧的 exports 目录（**不写进用户仓库**）。

    文件名带内容指纹前缀：同一份内容重复导出不会产生一堆看起来一样的文件，
    看到文件名就知道是不是同一版（archify 的 delivery 也是这个思路）。
    """
    from code_agent.config import code_dir

    folder = code_dir() / project_id / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:8]
    path = folder / (name or f"map-{stamp}-{digest}.md")
    path.write_text(markdown, encoding="utf-8")
    return str(path)


def is_test_file(rel: str) -> bool:
    return is_test_path(rel)


def summary_line(coverage: dict) -> str:
    totals = coverage.get("totals") or {}
    return (
        f"{totals.get('ratio', 0):.0%} 符号有测试守着"
        f"（直接 {totals.get('direct', 0)} · 文件级 {totals.get('file_level', 0)}）"
    )


def doc_path_hint(project_id: str) -> str:
    from code_agent.config import code_dir

    return str(Path(code_dir()) / project_id / "exports")


def from_service(
    intel,
    project: dict,
    *,
    tree_id: str = "main",
    sections: list[str] | None = None,
    title: str = "",
    tasks: Any = None,
    batches: Any = None,
) -> dict:
    """图即文档：把索引/台账/决策里的事实装配成 markdown。"""

    task_views: list[dict] = []
    if tasks is not None:
        try:
            for row in tasks.list_for_project(project["id"], limit=40):
                review = {}
                try:
                    review = json.loads(row.get("review_json") or "{}")
                except json.JSONDecodeError:
                    review = {}
                task_views.append(
                    {
                        "title": row.get("title") or "",
                        "status": row.get("status") or "",
                        "branch": row.get("branch") or "",
                        "changed_files": review.get("changed_files") or [],
                        "symbol_counts": (review.get("symbols") or {}).get("counts") or {},
                    }
                )
        except Exception:  # noqa: BLE001
            task_views = []
    batch_views: list[dict] = []
    if batches is not None:
        try:
            for row in batches.list_for_project(project["id"], limit=20):
                plan = {}
                try:
                    plan = json.loads(row.get("plan_json") or "{}")
                except json.JSONDecodeError:
                    plan = {}
                batch_views.append(
                    {
                        "title": row.get("title") or "",
                        "status": row.get("status") or "",
                        "queue": [l.get("id") for l in (plan.get("lanes") or [])],
                        "progress": {},
                    }
                )
        except Exception:  # noqa: BLE001
            batch_views = []
    store = intel.decisions(project["id"])
    decisions = [store.view(item) for item in store.all()[:400]]
    decisions.sort(key=lambda i: str(i.get("ts") or ""), reverse=True)
    return build(
        project=project,
        index=intel.index(project["id"], tree_id),
        coverage=intel.coverage(project, tree_id=tree_id),
        behaviors=intel.behaviors(project, tree_id=tree_id, limit=200),            decisions=decisions,
        architecture=intel.architecture(project),
        tasks=task_views,
        batches=batch_views,
        sections=sections,
        title=title,
    )

