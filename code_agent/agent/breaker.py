"""可选的重复失败熔断（借自 AgentRewind 的"终止判据"）。

默认关闭硬熔断，沿 DSH 使用重复提醒，让模型根据失败结果调整方法。
显式配置正数 CODE_AGENT_FAILURE_STREAK 时，以下硬停止语义才生效。

借鉴的结论很直白：一个失败的尝试被 **原样重试 N 次** 之后，第 N+1 次成功的概率
极低，继续跑只是在烧 token 与时间。AgentRewind 的判据是"连续 5 次提交里第一个
未满足的判据相同"就终止；这里把"提交"换成"工具调用"，判据换成**失败指纹**：

    指纹 = 工具名 + 归一化参数（键排序、路径分隔符统一、空白折叠）

为什么按"连续"而不是"累计"：同一个失败夹在别的失败之间（比如 a 失败 → b 失败 →
a 又失败）是两条不同的排错路线，不该合成一次熔断；而**同一个工具、同一份参数、
连着失败 5 次**是清清楚楚的原地打转。任一次成功都会清掉该工具的连续计数。

熔断不是"任务失败"：它停下并如实说明"我在这里原地打转了，请你介入"，这比
再跑 55 轮假装在推进诚实得多，也省得多。可用 `CODE_AGENT_FAILURE_STREAK` 调阈值
（设 0 关闭）。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# Repetition is advisory by default, as in DSH. Operators can opt into a hard
# breaker with a positive threshold; permission denials remain enforced per call.
DEFAULT_STREAK = max(0, int(os.environ.get("CODE_AGENT_FAILURE_STREAK", "0")))

#: 参数里这些键是易变的（每次调用都不同），不参与指纹——否则同一条命令换个时间戳
#: 就变成"新失败"，熔断永远不触发。
VOLATILE_KEYS = frozenset({"timeout", "timeout_ms", "ts", "timestamp", "nonce", "id"})

_WS = re.compile(r"\s+")


def normalize_value(value: Any) -> Any:
    """归一化参数值：路径分隔符统一为 /、空白折叠、数字统一成字符串形式。

    数字统一成字符串是有意的：`{"start": 10}` 与 `{"start": 10.0}` 是同一次调用，
    归一化后必须得到同一个指纹。
    """
    if isinstance(value, str):
        text = value.replace("\\", "/")
        text = _WS.sub(" ", text).strip()
        return text
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # 整数与整数形式的浮点要归一成同一个字符串：`10` 与 `10.0` 是同一次调用
        # （模型有时把行号写成 10.0），否则每次都会变成"新失败"。
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, dict):
        return {str(k): normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_value(v) for v in value]
    return value


def fingerprint(tool: str, arguments: dict[str, Any] | None) -> str:
    """失败指纹：工具名 + 归一化参数（键排序，去掉易变键）。

    参数为空时退化成工具名本身——"同一个工具反复失败（不管参数）"也是一种
    原地打转，值得被同一个指纹抓住。
    """
    args = {k: v for k, v in (arguments or {}).items() if k not in VOLATILE_KEYS}
    try:
        body = json.dumps(normalize_value(args), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):          # 参数里有不可序列化的东西
        body = str(args)
    return f"{tool}{body}" if body and body != "{}" else tool


class FailureBreaker:
    """跨执行段存活的连续失败计数器，同一条用户消息共用一个实例。"""

    def __init__(self, limit: int | None = None):
        self.limit = DEFAULT_STREAK if limit is None else max(0, int(limit))
        #: 指纹 -> 连续失败次数
        self.streaks: dict[str, int] = {}
        #: 指纹 -> 累计失败次数（熔断文案里用来说明"总共试了多少次"）
        self.totals: dict[str, int] = {}
        #: 指纹 -> (工具名, 最后一次失败原因)
        self.details: dict[str, tuple[str, str]] = {}
        self._tripped: str = ""

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def tripped(self) -> str:
        """熔断的指纹；'' 表示未熔断。"""
        return self._tripped

    def succeeded(self, tool: str) -> None:
        """某个工具成功了 → 清掉它的连续失败计数（累计数保留）。

        为什么要清"连续"：成功说明这条路通了，之前的失败已经不是"原地打转"的证据。
        """
        if not self.streaks:
            return
        for fp in [f for f in self.streaks if f == tool or f.startswith(tool + "{")]:
            self.streaks.pop(fp, None)

    def observe(self, fp: str, *, tool: str, reason: str) -> bool:
        """记一次失败。返回 True 表示本次达到阈值（熔断）。"""
        self.totals[fp] = self.totals.get(fp, 0) + 1
        self.details[fp] = (tool, reason)
        if not self.enabled:
            return False
        # 同一个失败必须"连着"发生：别的失败插进来就重置它的连续计数
        for other in list(self.streaks):
            if other != fp:
                self.streaks[other] = 0
        streak = self.streaks.get(fp, 0) + 1
        self.streaks[fp] = streak
        if streak >= self.limit and not self._tripped:
            self._tripped = fp
            return True
        return False

    # ------------------------------------------------------------------
    def report(self) -> str:
        """熔断说明（既是停止原因，也是给人看的一屏字）。

        说清三件事：卡在哪个调用上、试了几次、接下来该谁做什么。不写"任务失败"
        ——它没失败，是**停下等人**。
        """
        fp = self._tripped
        tool, reason = self.details.get(fp, ("", ""))
        streak = self.streaks.get(fp, 0)
        total = self.totals.get(fp, 0)
        lines = [
            f"[熔断] 同一个失败连续 {streak} 次（累计 {total} 次），已停下等你介入。",
            "",
            f"· 卡住的调用：`{tool}`" + (f"（指纹 {short_fp(fp)}）" if fp != tool else ""),
        ]
        if reason:
            lines.append(f"· 最近的失败原因：{reason[:400]}")
        lines += [
            "",
            "继续重试同一个调用几乎没有成功率，所以不再自动推进（省下的轮次留给有效改动）。",
            "建议：核对上一条原因里的路径/权限/命令是否正确，改掉真正的障碍后再发一条消息；"
            "如果这一步本来就不该做，直接告诉我改做什么。",
        ]
        return "\n".join(lines)


def short_fp(fp: str, limit: int = 120) -> str:
    """指纹的展示形式：太长就截断（参数可能很长）。"""
    return fp if len(fp) <= limit else fp[: limit - 1] + "…"
