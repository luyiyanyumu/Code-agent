"""有序验收清单（借自 AgentFlow 的 verifier：有序判据 + 只报第一个未满足项）。

AgentFlow 的 verifier 每次只回答两件事：**证据是否充分**（STOP / CONTINUE）以及
**第一个未满足的判据是什么**。它不把"还差什么"摊成一张长清单交给模型——因为摊开
之后模型会开始挑好做的那一条做，顺序（也就是真正的依赖关系）就丢了。论文里那条
"只报第一个未满足项 + 进度前缀 ρ=ℓ/n" 就是我们这里的行为。

为什么清单由**模型自己**声明、而不是我们猜：
- 猜出来的验收项与任务语义无关，只会变成噪音；
- 让模型先把"做完的标准"写成 2~6 条有序短清单，再动手，这本身就是"计划要短"
  （论文里训练 planner 输出更短的 plan，我们不训练模型，就用**硬上限 + 有序**来约束）。

清单**不落工作区**（那是用户的仓库），放在 `~/.code-agent/acceptance/<session>.json`。

判据类型（都是可确定性判定的，不需要额外执行命令、也不需要额外模型调用）：
    tool:<name>              该工具在本次任务里成功执行过至少一次
    patch:<path>             该文件被成功改动过（edit_file / write_file / revert_patch）
    file_exists:<path>       文件存在于工作区
    file_contains:<path>::<text>   文件包含某段文本
    no_failures              本轮工具执行没有失败记录

刻意**不支持** `command:` 类型：那要在门禁里起子进程，而门禁拿不到审批通道
（审批是用户交互），要么绕过审批要么卡死。要跑命令就在正文里跑，让模型把
结果写进结论——门禁只认已经发生的事实。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 清单条数上限。**这是"计划要短"的机械保证**：写成十步的计划多半是猜的。
MAX_ITEMS = int(os.environ.get("CODE_AGENT_ACCEPTANCE_MAX", "6"))

CHECK_KINDS = ("tool", "patch", "file_exists", "file_contains", "no_failures")

#: 视为"改动了文件"的工具（用于 patch: 判据）
EDIT_TOOLS = ("edit_file", "write_file", "revert_patch")


def store_dir() -> Path:
    p = Path(os.environ.get("CODE_AGENT_HOME", str(Path.home() / ".code-agent"))) / "acceptance"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Item:
    """一条验收判据。label 给人的，check 给机器的。"""

    label: str
    check: str
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "check": self.check}


@dataclass
class Verdict:
    """一条判据的判定结果。"""

    item: Item
    ok: bool
    detail: str = ""


@dataclass
class Report:
    """一次评估的完整结果（含 ρ = ℓ/n 进度）。"""

    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def done(self) -> int:
        """已满足的条数 ℓ。"""
        return sum(1 for v in self.verdicts if v.ok)

    @property
    def passed(self) -> bool:
        return self.total > 0 and self.done == self.total

    @property
    def first_unsatisfied(self) -> Verdict | None:
        for v in self.verdicts:
            if not v.ok:
                return v
        return None

    @property
    def first_unsatisfied_at(self) -> int:
        """第一条未满足项的下标（0-based）；全绿时返回 -1。

        用下标而不是 `verdicts.index(first)`：index() 比的是相等性，两条完全
        一样的判据会指到前一条上——位置报错会让模型改错东西。
        """
        for i, v in enumerate(self.verdicts):
            if not v.ok:
                return i
        return -1

    @property
    def rho(self) -> float:
        return 0.0 if not self.total else round(self.done / self.total, 3)

    def to_dict(self) -> dict[str, Any]:
        first = self.first_unsatisfied
        return {
            "total": self.total,
            "done": self.done,
            "rho": self.rho,
            "passed": self.passed,
            "first_unsatisfied": first.item.label if first else "",
            "items": [
                {"label": v.item.label, "check": v.item.check, "ok": v.ok, "detail": v.detail}
                for v in self.verdicts
            ],
        }

    # ------------------------------------------------------------------
    def summary(self) -> str:
        """给人看的一屏字：进度 + 逐条勾选 + **只详述第一个未满足项**。

        已满足的项照列（它们是"做到哪儿了"的证据，也是模型不该回退的锚点），
        未满足的项里只有第一个带原因——这就是 AgentFlow verifier 的口径：
        一次只让模型面对一个障碍，顺序才守得住。
        """
        if not self.total:
            return ""
        lines = [f"[验收清单 · {self.done}/{self.total} 已满足（ρ={self.rho:.2f}）]"]
        first = self.first_unsatisfied
        pending_shown = 0
        for v in self.verdicts:
            if v.ok:
                lines.append(f"✅ {v.item.label}")
                continue
            pending_shown += 1
            lines.append(f"❌ {v.item.label}")
            if v.detail and pending_shown == 1:
                lines.append(f"   ← {v.detail}")
        if first is not None and pending_shown > 1:
            lines.append(f"（{pending_shown - 1} 条更靠后的未满足项先不展开：修完上面那条再往下走）")
        return "\n".join(lines)

    def digest(self) -> str:
        """回环 prompt 用的"只修第一个未满足项"指令。"""
        first = self.first_unsatisfied
        if first is None:
            return self.summary()
        # 位置取**第一条未满足项的下标 +1**，不是 done+1：满足的不一定是前缀
        # （第 2 项满足、第 1 项没满足是常见情形），按 done+1 报会指错一条。
        pos = self.first_unsatisfied_at + 1
        return "\n".join(
            [
                self.summary(),
                "",
                f"**当前只做第 {pos} 项：{first.item.label}**",
                f"判据：`{first.item.check}`",
                "",
                "其余未满足项先不要动：清单是有顺序的，跳过前面那条会让后面的证据失效。",
                "做完这一项立刻重跑同一个验证，用结果说话；做不到就如实说明卡在哪里。",
            ]
        )


class AcceptanceList:
    """会话级的有序验收清单（可保存 / 读取 / 清空）。"""

    def __init__(self, items: list[Item], *, note: str = "", created_at: str = ""):
        self.items = items[:MAX_ITEMS]
        self.note = note
        self.created_at = created_at or _now()

    # ------------------------------------------------------------------
    @staticmethod
    def path_for(session_id: str) -> Path:
        return store_dir() / f"{session_id}.json"

    @classmethod
    def load(cls, session_id: str) -> "AcceptanceList | None":
        """读清单。坏文件当"没有清单"处理（绝不因为一个 JSON 坏了就拦住主流程）。"""
        path = cls.path_for(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("验收清单解析失败（当作没有清单）：%s", path)
            return None
        items = [
            Item(label=str(i.get("label") or ""), check=str(i.get("check") or ""), id=str(i.get("id") or ""))
            for i in (data.get("items") or [])
            if isinstance(i, dict) and str(i.get("check") or "").strip()
        ]
        if not items:
            return None
        return cls(items, note=str(data.get("note") or ""), created_at=str(data.get("created_at") or ""))

    def save(self, session_id: str) -> Path:
        path = self.path_for(session_id)
        data = {
            "session_id": session_id,
            "created_at": self.created_at,
            "updated_at": _now(),
            "note": self.note,
            "items": [i.to_dict() for i in self.items],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("验收清单落盘失败（不影响主流程）")
        return path

    @staticmethod
    def clear(session_id: str) -> bool:
        path = AcceptanceList.path_for(session_id)
        if not path.is_file():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    @classmethod
    def parse(cls, raw: Any, *, note: str = "") -> "AcceptanceList":
        """清洗模型/接口给的清单：只留合法判据，并**保持给定顺序**。

        条数超上限时截断而不是报错：模型给了 8 条，前 6 条照样能用于门禁，
        拒收整份清单反而会让这一步白跑。
        """
        items: list[Item] = []
        for entry in raw if isinstance(raw, list) else []:
            if isinstance(entry, str):
                entry = {"label": entry, "check": entry}
            if not isinstance(entry, dict):
                continue
            check = str(entry.get("check") or "").strip()
            label = str(entry.get("label") or check).strip()[:120]
            if not valid_check(check):
                continue
            items.append(Item(label=label, check=check, id=str(entry.get("id") or "")))
            if len(items) >= MAX_ITEMS:
                break
        return cls(items, note=str(note or "")[:400])

    # ------------------------------------------------------------------
    def evaluate(self, evidence: "Evidence") -> Report:
        """按顺序逐条判定。

        注意：**顺序本身是判据的一部分**，但判定不做"短路"——全判一遍才能给出
        ρ=ℓ/n 的真实进度（前一条没满足不代表后一条的事实判定不出来）。
        """
        return Report([_verdict(item, evidence) for item in self.items])


# ---------------------------------------------------------------------------
# 证据：全部来自**已经发生的事实**（轨迹 + 文件系统），不额外执行任何东西
# ---------------------------------------------------------------------------
@dataclass
class Evidence:
    ok_tools: set[str] = field(default_factory=set)
    patched: set[str] = field(default_factory=set)
    workspace: Path | None = None
    failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok_tools": sorted(self.ok_tools),
            "patched": sorted(self.patched),
            "failures": self.failures,
        }


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def collect_evidence(
    events: Iterable[Any],
    *,
    workspace: Path | None = None,
    failures: int = 0,
) -> Evidence:
    """从轨迹事件里收集证据：哪些工具成功过、哪些文件被改过。

    `events` 是本会话的全部事件（TranscriptStore.replay 的结果）。只看
    `tool_use` / `tool_result` 配对：失败、延后或明确未执行的结果均不算成功。
    旧轨迹没有执行元数据时仍识别原有失败前缀与 `[tool deferred]`。
    """
    results: dict[str, tuple[str, bool]] = {}
    ev_list = list(events)
    for ev in ev_list:
        payload = getattr(ev, "payload", None) or {}
        if getattr(ev, "type", None) == "tool_result" or (
            isinstance(payload, dict) and payload.get("call_id") and payload.get("result") is not None
        ):
            call_id = str(payload.get("call_id") or "")
            if call_id:
                results[call_id] = (str(payload.get("result") or ""),
                                    payload.get("deferred") is True or payload.get("executed") is False)

    evidence = Evidence(workspace=workspace, failures=int(failures or 0))
    for ev in ev_list:
        payload = getattr(ev, "payload", None) or {}
        call = payload.get("call") if isinstance(payload, dict) else None
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        result, unexecuted = results.get(str(call.get("id") or ""), ("", False))
        failed = result.startswith("[tool error]") or result.startswith("[permission denied]")
        if not result or failed or unexecuted or result.lstrip().startswith("[tool deferred]"):
            continue
        if name:
            evidence.ok_tools.add(name)
        if name in EDIT_TOOLS:
            rel = _norm_path(str(args.get("path") or args.get("rel") or ""))
            if rel:
                evidence.patched.add(rel)
    return evidence


def failure_count(events: Iterable[Any], *, since_seq: int = 0) -> int:
    """轨迹里的工具失败条数（`[tool error]` / `[permission denied]`）。

    `no_failures` 根据实际工具轨迹判定，不能把缺少计数误当成没有失败。
    验收工具与会话视图使用同一口径：只看已经回给模型的工具结果，
    与 collect_evidence 同源，不增加执行或自动重试。
    """
    total = 0
    for ev in events:
        if int(getattr(ev, "seq", 0) or 0) <= since_seq:
            continue
        payload = getattr(ev, "payload", None) or {}
        if not isinstance(payload, dict) or not payload.get("call_id"):
            continue
        result = payload.get("result")
        if isinstance(result, str) and (
            result.startswith("[tool error]") or result.startswith("[permission denied]")
        ):
            total += 1
    return total


def since_last_prompt(events: Iterable[Any]) -> int:
    """「用户这一轮」的起点 seq：最后一条**真实**用户消息。

    后台通知等系统消息（payload 带 auto）不算新的用户任务，
    否则每轮都会把起点越推越后。返回 0 表示从头算（还没有过用户消息）。
    没有它的话，"本轮还剩多少失败"会退化成"整个会话的历史失败"——
    一条判据只要某轮失败过就永远不通过。
    """
    last = 0
    for ev in events:
        if getattr(ev, "type", None) != "user_message":
            continue
        payload = getattr(ev, "payload", None) or {}
        if not isinstance(payload, dict) or payload.get("auto"):
            continue
        last = max(last, int(getattr(ev, "seq", 0) or 0))
    return last


def _verdict(item: Item, ev: Evidence) -> Verdict:
    check = item.check.strip()
    kind, _, arg = check.partition(":")
    kind = kind.strip()
    arg = arg.strip()

    if kind == "no_failures":
        ok = ev.failures == 0
        return Verdict(item, ok, "" if ok else f"还有 {ev.failures} 类未解决的失败")

    if kind == "tool":
        ok = arg in ev.ok_tools
        return Verdict(item, ok, "" if ok else f"还没有成功执行过 `{arg}`")

    if kind == "patch":
        want = _norm_path(arg)
        ok = any(p == want or p.endswith("/" + want) or want.endswith("/" + p) for p in ev.patched)
        extra = "" if not ev.patched else "（已改过：" + "、".join(sorted(ev.patched)[:4]) + "）"
        return Verdict(item, ok, "" if ok else f"还没有成功改动 `{arg}`{extra}")

    if kind == "file_exists":
        path = _resolve(ev.workspace, arg)
        ok = path is not None and path.is_file()
        return Verdict(item, ok, "" if ok else f"文件不存在：{arg}")

    if kind == "file_contains":
        path_text, sep, needle = arg.partition("::")
        if not sep or not needle:
            return Verdict(item, False, f"判据写法不合法（应为 file_contains:路径::文本）：{check}")
        path = _resolve(ev.workspace, path_text)
        if path is None or not path.is_file():
            return Verdict(item, False, f"文件不存在：{path_text}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return Verdict(item, False, f"读不了文件：{exc}")
        ok = needle in text
        return Verdict(item, ok, "" if ok else f"`{path_text}` 里还没有 `{needle[:60]}`")

    return Verdict(item, False, f"不认识的判据类型：{kind}（支持 {'/'.join(CHECK_KINDS)}）")


def valid_check(check: str) -> bool:
    """判据合法性（写入前就校验，别等到门禁才发现清单是坏的）。"""
    kind, _, arg = (check or "").strip().partition(":")
    if kind.strip() not in CHECK_KINDS:
        return False
    if kind.strip() == "no_failures":
        return True
    if not arg.strip():
        return False
    if kind.strip() == "file_contains":
        path_text, sep, needle = arg.partition("::")
        return bool(sep and path_text.strip() and needle.strip())
    return True


def _resolve(workspace: Path | None, rel: str) -> Path | None:
    """把判据里的路径解析到工作区内（不允许越界，避免清单被用来探测工作区外）。"""
    if not rel:
        return None
    base = Path(workspace) if workspace else Path.cwd()
    target = (base / _norm_path(rel)).resolve()
    try:
        target.relative_to(Path(base).resolve())
    except ValueError:
        return None
    return target
