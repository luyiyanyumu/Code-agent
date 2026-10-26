"""契约 ↔ 实现的一致性检查：把"缺口"变成可枚举的清单。

阶段 0 实验的核心结论是**证据的边界就是契约的边界**——16 处文案漂移测试抓不到、
8/10 条义务有歧义但没有任何闸门报警。所以这一层要做的事很具体：

1. **形状**：实现有没有偷偷改签名/字段/import（实验里 4 次零违例，靠的就是这个检查）；
2. **义务的证据覆盖率**：哪几条义务没有证据（ρ = 有证据的义务 / 义务总数）；
3. **无主的实现**：实现体非空、但契约里没有对应义务声明的函数（"未授权代码"）；
4. **契约自身的问题**：解析期记下的 `problems`（id 形状不对、义务表为空……）。

返回口径与 M29 的验收清单一致：给**进度 ρ**，**只详述第一个未满足项**——
实验里那条"只报第一个"的理由在契约上同样成立：摊开整张清单会让人挑好做的先做。

**刻意不做**：不判断义务写得对不对、不跑证据。契约检查只回答"契约与实现是否对得上"，
"义务是否真的成立"是证据（测试/断言）的事，两者不能互相代替。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_agent.contract.parse import SUFFIX, Contract, parse_contract
from code_agent.contract.shape import ShapeError, shape_of_declarations, shape_of_file

logger = logging.getLogger(__name__)

#: 一个模块最多扫多少条实现函数（防超大文件把检查拖慢）
MAX_FUNCTIONS = 400
#: 证据定位时最多扫描多少测试文件、单个文件多大
MAX_EVIDENCE_FILES = 800
MAX_EVIDENCE_BYTES = 512 * 1024

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.M)
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tmp", "dist", "build", ".pytest_cache", ".venv-janus"}


def repo_root_of(module_path: Path) -> Path:
    """从实现文件往上找仓库根：最近的、含 `tests/` 的祖先目录。

    找不到就退化成模块所在目录——**不猜**，也不去读 .git（工作树/副本里可能没有）。
    """
    module_path = Path(module_path).resolve()
    for parent in module_path.parents:
        if (parent / "tests").is_dir():
            return parent
    return module_path.parent


def evidence_index(root: Path) -> set[str]:
    """扫 `tests/**` 收集测试函数名（只认 `def test_*`），用于"证据是否可定位"。"""
    found: set[str] = set()
    root = Path(root) / "tests"
    if not root.is_dir():
        return found
    count = 0
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_EVIDENCE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.update(_TEST_DEF.findall(text))
        count += 1
        if count >= MAX_EVIDENCE_FILES:
            break
    return found


@dataclass
class ContractReport:
    """一次契约检查的结果。"""

    module: str
    contract: str = ""
    ok: bool = False
    shape_ok: bool = True
    obligation_count: int = 0
    evidence_count: int = 0
    coverage: float = 0.0
    #: 逐条检查项：(名称, 是否通过, 说明)。顺序即优先级。
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    unimplemented: list[str] = field(default_factory=list)
    unowned: list[str] = field(default_factory=list)
    #: 契约**明确声明**"没有独立证据"的义务 id（已声明的缺口，不拦截但要看得见）
    declared_gaps: list[str] = field(default_factory=list)
    #: 契约里写了、但在 `tests/` 里找不到的证据名
    missing_evidence: list[str] = field(default_factory=list)
    data_blocks: int = 0
    topology: int = 0

    @property
    def done(self) -> int:
        return sum(1 for _, ok, _ in self.checks if ok)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def rho(self) -> float:
        return 0.0 if not self.total else round(self.done / self.total, 3)

    @property
    def first_unsatisfied(self) -> tuple[str, str] | None:
        for name, ok, detail in self.checks:
            if not ok:
                return name, detail
        return None

    def to_dict(self) -> dict[str, Any]:
        first = self.first_unsatisfied
        return {
            "module": self.module,
            "contract": self.contract,
            "ok": self.ok,
            "shape_ok": self.shape_ok,
            "done": self.done,
            "total": self.total,
            "rho": self.rho,
            "obligation_count": self.obligation_count,
            "evidence_count": self.evidence_count,
            "coverage": self.coverage,
            "first_unsatisfied": first[0] if first else "",
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in self.checks],
            "problems": list(self.problems),
            "unimplemented": list(self.unimplemented),
            "unowned": list(self.unowned),
            "declared_gaps": list(self.declared_gaps),
            "missing_evidence": list(self.missing_evidence),
            "data_blocks": self.data_blocks,
            "topology": self.topology,
        }

    def summary(self) -> str:
        """给人/模型看的一屏字：进度 + 逐项 + **只详述第一个未满足项**。"""
        head = f"[契约检查 · {self.module} · {self.done}/{self.total} 通过（ρ={self.rho:.2f}）]"
        lines = [head]
        first = self.first_unsatisfied
        pending = 0
        for name, ok, detail in self.checks:
            lines.append(("✅ " if ok else "❌ ") + name)
            if not ok:
                pending += 1
                if ok is False and (name, detail) == first and detail:
                    lines.append("   ← " + detail)
        if self.obligation_count:
            lines.append(
                f"义务 {self.obligation_count} 条，有证据 {self.evidence_count} 条"
                f"（覆盖率 {self.coverage:.2f}）"
            )
        if self.declared_gaps:
            lines.append(
                f"契约明确声明无独立证据的义务（**看这里，别以为它们被验收了**）："
                f"{', '.join(self.declared_gaps)}"
            )
        if self.unowned:
            lines.append(f"无主的实现（契约未声明）：{', '.join(self.unowned[:6])}")
        if self.unimplemented:
            lines.append(f"契约声明了但还没实现：{', '.join(self.unimplemented[:6])}")
        if pending > 1:
            lines.append(f"（{pending - 1} 条更靠后的未满足项先不展开：修完上面那条再往下走）")
        return "\n".join(lines)


def find_contract(module_path: Path) -> Path | None:
    """按约定找模块的契约文件：同目录、同主名、`<主名>.CONTRACT.md`。

    例：`code_agent/agent/routing.py` → `code_agent/agent/routing.CONTRACT.md`。
    （**不是** `routing.py.CONTRACT.md`——文件名里再带一次 `.py` 只会在报告里读起来别扭。）
    """
    module_path = Path(module_path)
    candidate = module_path.with_suffix(SUFFIX)
    if candidate.is_file():
        return candidate
    # 刻意不认"同目录同名 .md"：把一份普通说明文档误当成契约，会让检查形同虚设。
    return None


def list_contracts(root: Path, *, limit: int = 200) -> list[Path]:
    """列出一个项目里所有契约文件（跳过 .venv/node_modules 这类目录）。"""
    root = Path(root)
    skip = {".git", ".venv", "node_modules", "__pycache__", ".tmp", "dist", "build", ".pytest_cache"}
    out: list[Path] = []
    for path in root.rglob(f"*{SUFFIX}"):
        if any(part in skip for part in path.parts):
            continue
        out.append(path)
        if len(out) >= limit:
            break
    return sorted(out)


def contract_check(
    module_path: Path,
    *,
    contract_path: Path | None = None,
    module_label: str = "",
) -> ContractReport:
    """检查一个实现文件与它的契约是否对得上。"""
    module_path = Path(module_path)
    label = module_label or module_path.name
    contract_file = Path(contract_path) if contract_path else find_contract(module_path)
    if contract_file is None or not contract_file.is_file():
        return ContractReport(
            module=label,
            ok=False,
            checks=[(
                "契约存在",
                False,
                f"没有找到 `{module_path.name}{SUFFIX}`（同目录）。"
                "没有契约 = 没有任何约束，这份实现不受契约层管辖。",
            )],
        )

    contract: Contract = parse_contract(contract_file, module=module_path.name)
    report = ContractReport(
        module=label,
        contract=str(contract_file),
        obligation_count=len(contract.obligations),
        evidence_count=contract.evidence_count,
        coverage=contract.coverage,
        problems=list(contract.problems),
        data_blocks=contract.data_blocks,
        topology=contract.topology,
    )

    # 1) 实现能不能解析
    try:
        actual = shape_of_file(module_path)
    except ShapeError as exc:
        report.checks.append(("实现可解析", False, str(exc)))
        report.shape_ok = False
        report.ok = False
        return report
    report.checks.append(("实现可解析", True, ""))

    # 2) 契约声明的符号是否都在实现里（缺 = 契约说了但没实现）
    declared = set(contract.symbols)
    present = set(actual.symbols)
    missing = sorted(declared - present)
    report.unimplemented = missing
    report.checks.append((
        "契约声明的符号都在实现里",
        not missing,
        f"契约声明了但实现里没有：{', '.join(missing)}" if missing else "",
    ))

    # 3) **形状闸门（R1/R2）**：把契约形状段的声明解析成快照，与实现的快照逐符号比。
    #    这一步就是阶段 0 实验里那个 gate.py：实验里 4 次独立实现它全部 PASS（契约层一字未改）。
    declared_shape = shape_of_declarations(contract.declarations)
    shape_problems = declared_shape.diff(actual)
    report.shape_ok = not shape_problems
    report.checks.append((
        "形状未被改动（R1/R2）",
        not shape_problems,
        "；".join(shape_problems) if shape_problems else "",
    ))

    # 4) 无主的实现：函数体非空，但契约里既没声明这个符号、也没有任何义务提到它
    owners = " ".join(f"{o.id} {o.text} {' '.join(o.evidence)}" for o in contract.obligations)
    unowned = sorted(
        name for name in actual.implemented
        if name.split(".")[-1] not in declared and name not in owners and name.split(".")[-1] not in owners
    )[:20]
    report.unowned = unowned
    report.checks.append((
        "没有无主的实现（R3）",
        not unowned,
        f"实现了但契约未声明/未提及：{', '.join(unowned)}" if unowned else "",
    ))

    # 5) 义务结项：**每条义务要么有证据、要么明确声明"没有独立证据"**。
    #    这个区分是刻意的：机器不能逼人编一条假证据来凑过检查（阶段 0 实验里
    #    10 条义务有 5 条没有证据名，实现者全都自己标出了"这条只能靠猜"）。
    #    要拦的是**静默缺口**，不是"诚实的缺口"。
    silent = contract.silent_gaps
    declared_gap = contract.declared_no_evidence
    detail = ""
    if silent:
        detail = f"既没有证据、也没有声明缺口的义务：{', '.join(silent)}"
    report.checks.append((
        "每条义务都有证据或明确声明无证据（R4）",
        not silent,
        detail,
    ))
    report.declared_gaps = declared_gap

    # 6) 证据**可定位**：契约里写的证据名必须真的能在 `tests/` 里找到。
    #    这是 R6 的弱化版（不做哈希钉死，只保证"证据还在"）：它拦不住"把断言改弱"，
    #    但拦得住最常见的两种退化——证据被删掉、证据被改名。
    named = sorted({e for o in contract.obligations for e in o.evidence})
    missing_evidence: list[str] = []
    if named:
        have = evidence_index(repo_root_of(module_path))
        missing_evidence = [e for e in named if e not in have]
        if not have:
            report.checks.append((
                "证据可定位（R6·弱化版）",
                True,
                "没有找到 tests/ 目录，跳过证据定位（不代表证据存在）",
            ))
        else:
            report.checks.append((
                "证据可定位（R6·弱化版）",
                not missing_evidence,
                f"契约里写了但 tests/ 里找不到：{', '.join(missing_evidence)}" if missing_evidence else "",
            ))
    report.missing_evidence = missing_evidence

    # 6) 契约自身的问题（解析期记录）——只作为最后一项，不覆盖前面的结论
    report.checks.append((
        "契约自身无解析问题",
        not contract.problems,
        "；".join(contract.problems) if contract.problems else "",
    ))

    report.ok = all(ok for _, ok, _ in report.checks)
    return report
