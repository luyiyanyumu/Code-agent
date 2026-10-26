"""重复调用提醒（对齐 DSH 的 `dsh-repeat-tool-reminder`）。

与 `breaker.py`（失败熔断）**互补**，不是同一件事：

    breaker  同一个调用连续**失败**到阈值 → **停下**交回人（终止判据）
    reminder 同一个调用连续重复到阈值   → **提醒一句**，继续跑（不打断）

为什么需要后者：熔断器只看失败，而"原地打转"里最贵的一种是**成功但没用**的重复——
`grep` 同一个词、`read_file` 同一个区间、`job_output` 同一个任务、`list_dir` 同一个目录，
每次都有输出、每次都"成功"，于是熔断器永远不响，而模型在慢慢把轮次烧光。
提醒把这件事摆到它面前：**这是第 3 次了，前两次给出的信息如果你已经用上了，
就该换个动作；如果你在等什么，明说在等什么。**

四条从 DSH 那里抄来的、都是踩过的细节：

1. **计数与成功无关**。成功不清零——"重复"本身就是信号，跟这次成不成功没关系。
   （这是与熔断器最关键的区别；照抄熔断器的"成功即清零"会让提醒永远不触发。）
2. **别的调用插进来就清零**（"连续"语义）。`grep X → read_file → grep X` 不算
   连续两次 `grep X`——中间那步很可能正是它在推进的证据。
3. **被排除的工具既不递增也不重置**。这是 DSH 明确记下的一条：像 `todo_write`
   这类记录型工具会穿插在循环之间，如果让它"重置计数"，循环就被它掩盖了。
4. **每个阈值只提醒一次**（3 → 5 → 8），不是每次重复都刷屏。刷屏的提醒等于噪音，
   而噪音会被模型无视——那就白加了。

阈值与开关：`CODE_AGENT_REPEAT_THRESHOLDS`（默认 `3,5,8`；设 `0` 关闭）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from code_agent.agent.breaker import fingerprint

logger = logging.getLogger(__name__)

#: 触发提醒的重复次数（可配）。空/0 = 关闭。
DEFAULT_THRESHOLDS = (3, 5, 8)

#: 默认不参与计数的工具：记录型/自省型，本来就会被反复调用，且穿插在循环之间。
#: **排除 = 既不递增也不重置**（见模块开头的第 3 条）。
DEFAULT_EXCLUDE = frozenset(
    {
        "todo_write",     # 整表替换式记录，穿插在每一步之间
        "acceptance_set",  # 验收清单同理
        "ask_user_question",  # 问问题是"停住等人"，不是空转
    }
)


def _parse_thresholds(raw: str | None) -> tuple[int, ...]:
    """解析阈值配置。**写错了回退默认，不静默关闭。**

    - 未设 / 空串 → 默认（空串在实践中就是"没设"）
    - `0`        → 关闭（这是唯一的关闭方式，写在文档里）
    - 合法列表   → 用它
    - 一个数都解析不出来 → **回退默认 + 警告**；不回退成"关闭"，
      否则一个手滑（比如写成 `3,5,8次`）会把这道保护网悄悄摘掉，而没人会发现。
    """
    if raw is None:
        return DEFAULT_THRESHOLDS
    text = raw.strip()
    if not text:
        return DEFAULT_THRESHOLDS
    if text == "0":
        return ()
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0:
            out.append(value)
    if not out:
        logger.warning(
            "CODE_AGENT_REPEAT_THRESHOLDS=%r 解析不出任何正整数，回退默认 %s",
            raw,
            DEFAULT_THRESHOLDS,
        )
        return DEFAULT_THRESHOLDS
    return tuple(sorted(set(out)))


class RepeatReminder:
    """连续重复计数（跨段/跨轮共用同一个实例，与熔断器一起挂在一条 prompt 上）。"""

    def __init__(
        self,
        thresholds: tuple[int, ...] | None = None,
        *,
        exclude: frozenset[str] | None = None,
        include: frozenset[str] | None = None,
    ) -> None:
        self.thresholds = (
            _parse_thresholds(os.environ.get("CODE_AGENT_REPEAT_THRESHOLDS"))
            if thresholds is None
            else tuple(sorted(t for t in thresholds if t > 0))
        )
        self.exclude = DEFAULT_EXCLUDE if exclude is None else frozenset(exclude)
        #: None = 不限制；给了就只跟踪这些工具（DSH 的 `include`）
        self.include = None if include is None else frozenset(include)
        self._streaks: dict[str, int] = {}
        #: 指纹 -> 已经提醒过的阈值（每个阈值只提醒一次）
        self._notified: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.thresholds)

    def observe(self, tool: str, arguments: dict[str, Any] | None) -> str | None:
        """记一次调用。跨过一个阈值就返回提醒文本，否则 None。"""
        if not self.enabled:
            return None
        if tool in self.exclude:
            return None  # 既不递增也不重置（DSH 同款）
        if self.include is not None and tool not in self.include:
            return None

        fp = fingerprint(tool, arguments)
        for other in list(self._streaks):
            if other != fp:
                self._streaks[other] = 0
        count = self._streaks.get(fp, 0) + 1
        self._streaks[fp] = count

        already = self._notified.get(fp, 0)
        for threshold in self.thresholds:
            if count == threshold and threshold > already:
                self._notified[fp] = threshold
                return self.reminder_text(tool, count)
        return None

    def reminder_text(self, tool: str, count: int) -> str:
        return (
            f"[提示] 同一个调用（`{tool}`，参数完全一致）已经连续第 {count} 次。"
            "如果前几次给出的信息你已经用上了，就换个动作；"
            "如果你在等某个结果，明说在等什么、或用能给出新信息的调用；"
            "如果只是把同一份内容再读一遍，那不会产生新信息。"
        )
