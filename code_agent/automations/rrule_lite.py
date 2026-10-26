"""rrule 子集解析（s23）：只支持教程覆盖的核心语法，够用且可测。

支持：FREQ=HOURLY|DAILY|WEEKLY|MONTHLY|YEARLY + INTERVAL=n + BYHOUR/BYMINUTE/BYDAY/BYMONTHDAY
不支持：COUNT/UNTIL/BYSETPOS 等高级特性（YAGNI）。

锚点语义：递推以 anchor（任务创建/首次启用时间）为起点，INTERVAL 相对锚点计数。
"""

from __future__ import annotations

from datetime import datetime, timedelta

_BYDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_MAX_SCAN = 800  # 候选扫描上限，防死循环


def parse_rrule(rrule: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for chunk in rrule.strip().split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()
    return parts


def next_occurrence(rrule: str, after: datetime, anchor: datetime) -> datetime | None:
    """返回 after 之后（严格大于）的下一次触发时间；无法解析返回 None。"""
    p = parse_rrule(rrule)
    freq = p.get("FREQ", "")
    if freq not in ("HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return None
    try:
        interval = max(1, int(p.get("INTERVAL", "1")))
    except ValueError:
        return None

    byhour = _int_list(p.get("BYHOUR"), default=[anchor.hour if freq == "HOURLY" else 9])
    byminute = _int_list(p.get("BYMINUTE"), default=[0])
    byday = [d for d in (p.get("BYDAY", "").split(",") if p.get("BYDAY") else []) if d in _BYDAY]
    bymonthday = _int_list(p.get("BYMONTHDAY"), default=[anchor.day])
    bymonth = _int_list(p.get("BYMONTH"), default=[anchor.month])

    candidate = _floor_minute(max(after, anchor))
    scanned = 0
    while scanned < _MAX_SCAN:
        nxt = _next_candidate(candidate, freq, interval, anchor, byhour, byminute, byday, bymonthday, bymonth)
        if nxt is None:
            return None
        scanned += 1
        if nxt > after:
            return nxt
        candidate = nxt
    return None


def _next_candidate(
    after: datetime, freq: str, interval: int, anchor: datetime,
    byhour: list[int], byminute: list[int], byday: list[str],
    bymonthday: list[int], bymonth: list[int],
) -> datetime | None:
    """给出严格大于 after 的下一个**满足条件**的候选；找不到返回 None。"""
    if freq == "HOURLY":
        # 从锚点整点起，每 interval 小时一个槽；分钟取 byminute
        anchor_hour = anchor.replace(minute=0, second=0, microsecond=0)
        delta_h = (after - anchor_hour).total_seconds() / 3600.0
        step = max(1, int(delta_h // interval) + 1) * interval
        base = anchor_hour + timedelta(hours=step)
        nxt = base.replace(minute=byminute[0] if len(byminute) == 1 else 0)
        if nxt <= after:
            nxt += timedelta(hours=interval)
        return nxt.replace(minute=(byminute[0] if len(byminute) == 1 else 0), second=0, microsecond=0)

    # 非 HOURLY：按天粒度扫描，检查当天是否满足 day-filter，再挑时刻
    day = (after + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(_MAX_SCAN):
        if _day_matches(day, freq, interval, anchor, byday, bymonthday, bymonth):
            for h in sorted(byhour):
                for m in sorted(byminute):
                    cand = day.replace(hour=h, minute=m, second=0, microsecond=0)
                    if cand > after:
                        return cand
        day += timedelta(days=1)
    return None


def _day_matches(
    day: datetime, freq: str, interval: int, anchor: datetime,
    byday: list[str], bymonthday: list[int], bymonth: list[int],
) -> bool:
    if freq == "DAILY":
        anchor_date = anchor.date()
        return (day.date() - anchor_date).days % interval == 0
    if freq == "WEEKLY":
        if not byday:
            byday = [k for k, v in _BYDAY.items() if v == anchor.weekday()]
        weeks_between = (day.date() - anchor.date()).days // 7
        if weeks_between % interval != 0:
            return False
        return day.weekday() in {_BYDAY[d] for d in byday if d in _BYDAY}
    if freq == "MONTHLY":
        months_between = (day.year - anchor.year) * 12 + (day.month - anchor.month)
        return months_between % interval == 0 and day.day in bymonthday
    if freq == "YEARLY":
        return day.month in bymonth and day.day in bymonthday
    return False


def _int_list(raw: str | None, default: list[int]) -> list[int]:
    if not raw:
        return default
    try:
        return [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        return default


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)
