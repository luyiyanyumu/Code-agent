"""批量补功能描述（M33）：按批取源码 → 一次问模型 → 写进描述存储。

为什么是"一次问一批、**不带任何工具**"，而不是让 agent 拿着工具一个个写：

1. **快**：几百个符号一个个"读文件→写一条"要几百轮工具往返；一批 12 个只要十几次调用。
2. **安全**：这条路径没有任何写工具——模型只能产出文本，**写库由服务端做**。
   所以它可以在任何权限档下跑，不需要"为了补描述给 agent 开仓库写权限"。
3. **可核对**：每条描述都和它的符号、源码片段、内容哈希一起落库；
   离线、超时、模型乱答都如实计数，不假装补全了。

调用方（服务层）负责：找缺描述的符号、注入 provider、把结果落到 BriefStore。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

#: 一批问几个符号。太少浪费往返，太多模型会漏答（实测 12 个稳定，20 个开始丢条目）
BATCH = 12
#: 每个符号最多带多少行源码。类/函数体一般几十行，够判断"干什么"，又不至于把 prompt 撑爆
MAX_SOURCE_LINES = 60
#: 一条描述的字数上限（存库那边会再拦一次）
MAX_BRIEF_CHARS = 200


class TargetChanged(ValueError):
    """A generated explanation no longer belongs to the current source."""

SYSTEM_PROMPT = """你在给一份代码导图补"这个符号是干什么的"。

读者是同事：他要在导图上扫一眼就知道这段代码管什么，然后决定要不要点进去看。

要求：
1. 写「它干什么 / 它是什么」，一句话，不超过 30 个字；
2. 不要复述名字（"处理 xxx" 这种等于没说），不要写结构描述（"该类没有基类"）；
3. 只依据给出的源码片段判断；拿不准就写得保守、具体到能验证的层面；
   方法描述实际行为；参数、字段或变量描述它承载的数据和用途；类、文件或模块描述职责；
   不要把语法类型说明当作功能，也不要凭名称猜业务规则；
4. 只输出 JSON，不要解释、不要 markdown 代码块：
{"briefs":[{"n":1,"text":"..."}]}
其中 n 是下面条目的编号。"""

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_JSON_ARRAY = re.compile(r"\[.*\]", re.S)


def _extract_items(raw: str) -> list[Any] | None:
    """从回答里抠出条目列表：`{"briefs":[...]}` / `[{...}]` / 单个 `{"text":...}` 都认。

    为什么要这么宽：模型偶尔会把最外层包一层、或者只回答一条就不给数组；
    为格式差异丢掉整批内容是纯损失（真正的判据是"这条描述有没有内容"）。
    """
    for pattern in (_JSON_OBJECT, _JSON_ARRAY):
        match = pattern.search(raw)
        if not match:
            continue
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("briefs") or data.get("items") or data.get("result")
            if isinstance(inner, list):
                return inner
            if data.get("text") or data.get("brief"):
                return [data]
    return None


@dataclass
class Target:
    """待补的一个符号（源码片段已经取好，调用模型时不需要再读文件）。"""

    id: str
    name: str
    kind: str
    path: str
    line: int
    signature: str = ""
    source: str = ""
    content_hash: str = ""
    body_hash: str = ""
    domain: str = ""

    def prompt_item(self, index: int) -> str:
        head = f"{index}. {self.name}（{self.kind}，{self.path or '.'}:{self.line}）"
        if self.signature:
            head += f"\n   签名：{self.signature[:160]}"
        if self.source:
            head += "\n   源码：\n" + "\n".join(f"   {ln}" for ln in self.source.split("\n"))
        return head


def source_segment(
    read_text: Callable[[str], str], row: dict, *, max_lines: int = MAX_SOURCE_LINES
) -> str:
    """取一个符号的源码片段（声明到它结束，最多 max_lines 行）。

    为什么用行区间而不是"整个文件"：一个文件里几十个符号，全塞进去既贵又容易被带偏；
    片段足够回答"它干什么"，也让模型知道别越过这段范围去猜。
    """
    rel = str(row.get("path") or "")
    start = int(row.get("line") or 0)
    end = int(row.get("end_line") or 0)
    if not rel or start <= 0:
        return ""
    try:
        text = read_text(rel)
        from code_agent.codeintel.business import extra_of

        context = extra_of(row).get("business_context_range")
        if isinstance(context, dict) and isinstance(context.get("start"), int) and isinstance(context.get("end"), int):
            if 0 <= context["start"] < context["end"] <= len(text):
                return "\n".join(text[context["start"]:context["end"]].splitlines()[:max_lines]).rstrip()
        lines = text.split("\n")
    except Exception:  # noqa: BLE001
        return ""
    if start > len(lines):
        return ""
    stop = min(end if end >= start else start + max_lines, start + max_lines - 1, len(lines))
    return "\n".join(lines[start - 1 : stop]).rstrip()


def build_messages(targets: list[Target], *, strict: bool = False) -> list[dict[str, Any]]:
    """一批符号 → 一轮对话。`strict=True` 用于解析失败后的重试（把要求再说死一点）。"""
    body = "\n\n".join(t.prompt_item(i) for i, t in enumerate(targets, start=1))
    system = SYSTEM_PROMPT
    if strict:
        system += "\n\n注意：上一次的回答无法解析。这次**只**输出一行 JSON 对象，不要任何其它字符。"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"给下面 {len(targets)} 个符号各写一句话：\n\n{body}"},
    ]


def parse_briefs(text: str, count: int) -> dict[int, str]:
    """从模型回答里抠出 {编号: 描述}。

    解析失败返回空字典（调用方据此重试一次，再失败就**如实记失败**而不是编内容）。
    """
    raw = str(text or "").strip()
    if not raw:
        return {}
    items = _extract_items(raw)
    if items is None:
        return {}
    out: dict[int, str] = {}
    for pos, item in enumerate(items, start=1):
        if isinstance(item, str):
            number, description = pos, item
        elif isinstance(item, dict):
            try:
                number = int(item.get("n") or item.get("index") or pos)
            except (TypeError, ValueError):
                number = pos
            description = str(item.get("text") or item.get("brief") or "")
        else:
            continue
        clean = " ".join(description.split())
        if not clean or not (1 <= number <= count):
            continue
        out[number] = clean[:MAX_BRIEF_CHARS]
    return out


@dataclass
class FillResult:
    total: int = 0
    written: int = 0
    failed: int = 0
    skipped: int = 0
    batches: int = 0
    errors: list[str] = field(default_factory=list)
    model: str = ""
    aborted: str = ""
    source_changed: bool = False

    def view(self) -> dict:
        return {
            "total": self.total,
            "written": self.written,
            "failed": self.failed,
            "skipped": self.skipped,
            "batches": self.batches,
            "errors": self.errors[:4],
            "model": self.model,
            "aborted": self.aborted,
            "source_changed": self.source_changed,
        }


async def fill(
    targets: list[Target],
    *,
    provider: Any,
    write: Callable[[Target, str], bool | None],
    batch: int = BATCH,
    on_progress: Callable[[dict], None] | None = None,
    threaded_write: bool = False,
    timeout: float = 45.0,
    should_continue: Callable[[], bool] | None = None,
) -> FillResult:
    """逐批补描述。`write(target, text)` 由调用方落库（带哈希），这里只管取内容。

    连续两批失败就停：多半是模型/网络不可用，继续跑只会白烧时间和额度。
    单批失败会重试一次（把 JSON 要求再说死一点），仍失败就记进 `errors` 并继续下一批。
    """
    result = FillResult(total=len(targets), model=str(getattr(provider, "model", "") or ""))
    if provider is None:
        result.aborted = "没有可用的模型"
        return result
    if not targets:
        return result

    consecutive = 0
    for start in range(0, len(targets), max(1, batch)):
        if result.source_changed or (should_continue is not None and not should_continue()):
            # Finish the in-flight batch, then replace the remaining inventory
            # from current code instead of calling the model on stale entities.
            result.source_changed = True
            result.aborted = "源码已变化，等待增量更新"
            break
        chunk = targets[start : start + max(1, batch)]
        result.batches += 1
        got: dict[int, str] = {}
        for attempt in (False, True):
            try:
                turn = await asyncio.wait_for(provider.chat(build_messages(chunk, strict=attempt), []), timeout=timeout)
            except Exception as exc:  # noqa: BLE001 —— 网络/额度问题：记下来，别把整轮炸掉
                result.errors.append(f"第 {result.batches} 批调用失败：{exc}"[:200])
                turn = None
            got = parse_briefs(getattr(turn, "text", "") or "", len(chunk)) if turn else {}
            if got:
                break
            if not attempt:
                result.errors.append(f"第 {result.batches} 批回答解析不了，重试一次")
        if not got:
            result.failed += len(chunk)
            consecutive += 1
            if consecutive >= 2:
                result.aborted = "连续两批拿不到可用回答，已停下"
                break
            continue
        consecutive = 0
        for number, text in got.items():
            target = chunk[number - 1]
            try:
                accepted = await asyncio.to_thread(write, target, text) if threaded_write else write(target, text)
                if accepted is False:
                    result.skipped += 1
                else:
                    result.written += 1
            except TargetChanged:
                result.skipped += 1
                result.source_changed = True
                result.aborted = "源码已变化，等待增量更新"
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                result.errors.append(f"{target.id} 落库失败：{exc}"[:200])
        result.failed += len(chunk) - len(got)       # 模型漏答的那几条也算失败
        if on_progress:
            on_progress(
                {
                    "total": result.total,
                    "done": min(result.written + result.failed + result.skipped, result.total),
                    "written": result.written,
                    "failed": result.failed,
                    "skipped": result.skipped,
                }
            )
    if should_continue is not None and not should_continue():
        result.source_changed = True
        result.aborted = "源码已变化，等待增量更新"
    return result
