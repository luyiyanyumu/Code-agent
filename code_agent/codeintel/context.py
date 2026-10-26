"""代码模式上下文（M18 · P2）：给模型的"作业规程"与"结构坐标"。

分两层，各自解决一个问题：

1. **会话级（system prompt 附加）**：作业规程 + 项目地图 digest。
   规程是行为约束（先看地图再读代码、只做最小替换、改完给文件:行号、跑验证命令），
   digest 是定位入口。它们在一次会话里只需给一次，且随索引版本更新而刷新。
2. **消息级（model-facing prompt 附加）**：用户此刻在看的**精确坐标**——
   文件、行区间、符号签名、成员、被谁引用。这部分由服务端从索引里查出来，
   不是前端拼的字符串，所以模型看到的是事实。

刻意的边界：**展示文本不变**。用户输入什么，界面与轨迹里就还是什么；
附加块只进"发给模型的 prompt"，并在 user_message 事件里留一份 `code_context`
供回放时看清"当时带了什么上下文"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_agent.codeintel.store import SymbolIndex

#: 发给模型的会话级附加（规程）——保持精简，它每轮都在上下文里
#:
#: 工具与子任务只在有助于推进当前任务时使用。参考本地 DSH 的
#: tool-subagent/lib/index.js 中按上下文分派独立工作的工具说明；
#: 不把地图读取、固定重试次数或额外验收清单变成每项工作的前置流程。
PROTOCOL = """[代码模式｜你是这个项目的 code 助手]（必须遵守）

身份与目标：完成用户在当前项目中的实际任务。已有源码、地图和运行结果是事实依据，但可能不完整或过时；
按需补足影响行动的事实，不用对同类项目的印象替代实际情况。用户已授权修改且当前权限允许时直接实施、验证和交付；
只有用户要求提案或当前权限仅允许提案时才提供可审查提案。

0. 工具按需使用：只调用当前工具表中真实可用的工具。能用已有准确上下文完成的事直接推进，不为显得在工作而额外查询。
1. 定位：已知文件或坐标时直接读相关内容；确实缺少入口、结构或功能线索时再使用搜索、地图或功能导航。
   工具输出足够支持下一步即可行动，不把每项任务都变成整仓库调查，也不凭记忆猜路径。
2. 阅读：优先读取与问题有关的区间；必要时补足相邻实现或完整小文件，以理解行为为准，避免无关的大段读取。
3. 改动：用户已授权修改且当前权限允许时，直接用当前可用的编辑工具实施最小精确改动（替换内容必须唯一命中），
   完成相关验证后交付，不要求用户额外说一遍“直接写入”。用户要求提案或权限仅允许提案时才用提案流程，
   说明如何审查应用；只读规划阶段遵守下述规划边界。
4. 交付：用 `文件:行号` 解释实际差异、原因与验证结果。未应用提案不能说文件已修改。
5. 验证：改动落盘后运行与本次修改相关的最短测试/构建命令，遵守当前执行权限与单次审批；
   已有当前版本、相关范围的有效验证结果可以复用。提案未落盘时只能说明建议验证什么。
6. 边界：只允许改当前授权项目与工作树内的文件，不越界、不改依赖目录（node_modules/.venv 等）。
   只读规划阶段仅阅读事实并产出计划，不修改文件、提交代码或执行会写入项目的命令；不绕过权限或单次审批。
7. 不确定：索引和地图是定位线索；低置信度或与当前代码不一致时，核对与本次行动相关的源码。
8. 按诊断修复：利用真实报错定位原因，修改解决任务所必需的位置，并验证受影响的行为；不要顺手扩大为无关重构。
9. 持续推进：遇到失败先分析输出、修正假设或更换方法，不因固定尝试次数就交回用户，也不重复已确认无效的操作。
   遇到真实的权限、输入、依赖或外部阻碍时，在授权范围内尝试可行路径；无法继续时说明具体阻碍与未完成项，不虚报完成。
10. 证据与措辞：区分"我改了"、"测试通过"、"我肉眼看过"；没有运行的命令不许说通过，
   没有看过的界面不许说好看。静态依赖关系读作"静态可达"，不要说成运行时影响或
   破坏面。给结论时带上依据（哪个文件:行、哪条命令、什么输出）。
11. 计划要短：简单任务直接完成最小实现和相关验证，不强制创建验收清单或增加验收工具调用。
   复杂任务先明确简短、有序的完成标准；需要记录时仅使用当前可用的工具，不列无关步骤或反复重写计划。
12. 收口：核对本次要求、实际改动与验证结果；已有当前版本的充分证据时直接交付。
   没有新改动、失败或未解决疑点时不重复验证，也不为收口额外建立审计交接。
   缺少相关运行验证时补一次最短验证；确无可执行验证时，说明具体原因和未验证范围。"""


LEAD_PROTOCOL = """[代码模式｜项目 Lead 的执行与协作规程]（必须遵守）

身份与目标：你既协调任务，也亲自执行。依据当前项目的源码、地图与实际执行结果完成用户目标，不用对同类项目的印象替代事实。
1. 先定位再动手：使用已有上下文定位；需要补充事实时再查地图、搜索或读取目标区间，不重复读取已经充分的证据。
   地图可能滞后；低置信度结构须回源码核对，静态可达不等于运行通过。
2. 直接实施优先：简单、边界明确、前后连续的任务由你直接完成修改、相关验证和交付，不为每个步骤另建节点或转交执行者。
   只使用当前工具表中可用的工具，遵守权限与单次审批。授权允许直接修改时实施最小改动；
   用户要求提案或仅允许提案时提供可审查提案，未应用前不能说文件已修改。
3. 验证与收口：修改后执行与本次改动相关的最短验证，确认真实工作目录、退出码与关键输出，再说明实际结果。
   已有当前版本的充分验证结果时无需机械重跑；没有新改动、失败或未解决疑点时直接交付，不增加重复取证和审计交接。
   没有执行的命令不能说通过；验证不可用时说明具体阻碍和未验证范围。
4. 按需协作：复杂任务或存在可独立推进、责任范围不同的子任务时再委派，明确目标、范围、输入、产出和验证标准。
   连续依赖或共享修改点优先自己完成或复用已有执行者；不要把本可直接完成的工作交回用户。
5. 复核后代结果：需要复核时按准确 session_id、activation_id 读取该次执行的实际改动和必要运行证据，区分节点报告与真实输出。
   证据充分即可复用；不足时直接补充必要检查，确需独立验证才委派，不为读取证据额外派工。
6. 计划与边界：简单任务简述做完的标准后推进；复杂任务使用简短、有序的验收标准，避免长计划与无关步骤。
   只处理当前授权项目与工作树，不改依赖目录或顺手重构。只读规划阶段仅阅读事实并产出计划，
   不修改文件、提交代码或执行会写入项目的命令，后代同样遵守规划权限。
7. 交付与阻碍：说明实际改动、文件:行、验证结果及尚未解决的问题；连续失败没有进展时核实阻碍并调整方法，
   保留真实未完成项，不将源码推断或完成通知当作验收通过。"""


def role_session_suffix(text: str, role: str) -> str:
    """Replace only our generated worker protocol, preserving the shared map."""
    if role == "lead" and text.startswith(PROTOCOL):
        return LEAD_PROTOCOL + text[len(PROTOCOL):]
    return text


def session_suffix(index: SymbolIndex | None, project: dict[str, Any], *, max_chars: int = 5000) -> str:
    """会话级附加：作业规程 + 项目地图 digest。"""
    if index is None:
        return PROTOCOL
    from code_agent.codeintel import graph

    digest = graph.build_digest(index, project, max_chars=max_chars)
    return f"{PROTOCOL}\n\n{digest}\n\n（地图可能滞后于最新改动：需要最新结构时随时调用 code_map。）"


def _fmt_ids(ids: list[str]) -> str:
    return "、".join(str(i).replace("file:", "") for i in ids[:8]) if ids else ""


def message_block(service: Any, project: dict[str, Any], code_context: dict[str, Any], *, tree_id: str = "main") -> str:
    """消息级附加：把用户当前盯着的坐标渲染成事实块。

    入参 code_context 的形状（前端只传 id 与区间，事实由服务端补）：
    ```json
    {"path": "code_agent/sessions/manager.py", "start": 212, "end": 268,
     "symbol_id": "sym:.../#SessionRuntime.submit", "node_id": "mod:code_agent/sessions"}
    ```
    """
    if not isinstance(code_context, dict) or not code_context:
        return ""
    index = service.index(project["id"], tree_id)
    lines: list[str] = ["[代码模式｜当前上下文]（服务端从符号索引解析）"]

    path = str(code_context.get("path") or "")
    start = int(code_context.get("start") or 0)
    end = int(code_context.get("end") or 0)
    if path:
        loc = f"{path}:{start or 1}" + (f"-{end}" if end and end != start else "")
        lines.append(f"打开的文件：{loc}")
        file_row = index.file_row(path)
        if file_row:
            lines.append(f"  语言 {file_row['lang']} · {file_row['loc']} 行 · 解析器 {file_row['strategy'] or '-'}")

    symbol_id = str(code_context.get("symbol_id") or "").strip()
    if symbol_id:
        row = index.symbol(symbol_id)
        if row is None:
            lines.append(f"选中符号：{symbol_id}（索引中已不存在，可能刚被改动）")
        else:
            from code_agent.codeintel import graph

            detail = graph.symbol_detail(row)
            lines.append(
                f"选中符号：{detail['kind']} {detail['qname']}（{detail['path']}:{detail['line']}-{detail['end_line']}）"
            )
            if detail["signature"]:
                lines.append(f"  签名：{detail['signature']}")
            if detail["doc"]:
                lines.append(f"  说明：{detail['doc'][:200]}")
            if detail["params"]:
                lines.append(f"  参数：{', '.join(detail['params'])}")
            if detail["returns"]:
                lines.append(f"  返回：{detail['returns']}")
            members = [
                f"{c['kind']} {c['name']}:{c['line']}"
                for c in index.query_all(
                    "SELECT kind, name, line FROM symbols WHERE parent=? ORDER BY line LIMIT 12", (symbol_id,)
                )
            ]
            if members:
                lines.append(f"  成员（{len(members)}）：{'、'.join(members)}")
            importers = sorted(
                {
                    str(e["src"]).replace("file:", "")
                    for e in index.edges(kind="referenced_by", dst=symbol_id)
                }
                | {
                    str(e["src"]).replace("file:", "")
                    for e in index.edges(kind="imports", dst=f"file:{row['path']}")
                }
            )
            lines.append(f"  被引用于：{_fmt_ids(importers) or '（暂无）'}")

    for key, label in (("test_files", "关联测试"), ("importers", "调用方")):
        values = code_context.get(key)
        if isinstance(values, list) and values:
            lines.append(f"{label}：{_fmt_ids([str(v) for v in values])}")

    tree_hint = code_context.get("map_hint")
    if isinstance(tree_hint, str) and tree_hint.strip():
        lines.append(f"地图提示：{tree_hint.strip()[:400]}")

    if len(lines) == 1:
        return ""
    lines.append("（以上为事实性上下文，可直接使用；需要更多细节用 code_map / read_file。）")
    return "\n".join(lines)


def workspace_root_of(project: dict[str, Any]) -> Path:
    return Path(project["root_path"])


def dump_context(code_context: dict[str, Any] | None) -> str:
    """把 code_context 存进事件用的紧凑字符串（回放可见，但不撑爆轨迹）。"""
    if not code_context:
        return ""
    return json.dumps(code_context, ensure_ascii=False)[:1000]
