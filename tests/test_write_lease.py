"""写租约（write lease）：同一轮里写同一个文件的调用必须串行，不同文件照旧并行。

盯住三条不变量：

1. **同路径串行**：两次写同一个文件（同一轮或跨 `dispatch_all`）时，进入/退出工具的
   时间段必须首尾相接、不许交叠——否则就是无保护的读-改-写，后写者基于旧内容覆盖前者，
   补丁账本还会记下一个从未真实发生过的顺序。
2. **不同路径仍然并行**：租约按**路径**发放，不是一把全局互斥锁。这条写坏了，工具
   并行化（M14）就被悄悄退化成串行，性能账全砸。
3. **表不泄漏、异常也放手**：路径→锁的表在写者走完后必须回到空；工具抛异常时租约
   照退，下一个写者不会卡死。

两个判定手段，都不用"计数峰值"这种靠调度运气的写法：

- **时间区间交叠**：每个写者记录自己"在工具里"的区间，`_overlap` 量出交叠时长。
  区间长度有 `asyncio.sleep` 保底，所以"串行 => 交叠 <= 0"是稳的。
- **`_enter_together`**：把两个 `dispatch_all` 协程一起挂到同一个事件循环上，等到
  两个都真的开始了才放行。注意它必须在**取租约之前**同步——把闩放在工具体内会和
  租约互相等待（第二个调用根本进不了工具，闩永远凑不齐），那是测试自己造的死锁。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(session_id="s_lease", workspace=workspace)


def _touch_dir(tmp_path: Path, *dirs: str) -> None:
    """预先建好写者会落进去的目录。

    `write_file` / `edit_file` 都不负责建父目录，`WriteProbe` 也不建——否则它测的
    就不再是租约，而是"谁先 mkdir"。目录属于测试布置，统一在这里建。
    """
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)


async def _enter_together(*coros):
    """让两个 `dispatch_all` 真正同时在场，再一起放行。

    做法：每个包装协程先报到、让出一次调度（`sleep(0)`），所以两个包装协程都会在
    任何工具操作之前完成报到；返回的 future 也是此刻就建好的，不会出现"一个已经
    跑完了另一个才开始"的顺序执行假象。
    """
    arrived = 0
    gate = asyncio.Event()

    async def guarded(coro):
        nonlocal arrived
        arrived += 1
        if arrived >= 2:
            gate.set()
        else:
            await gate.wait()
        return await coro

    return await asyncio.gather(*(guarded(c) for c in coros))


class _Timeline:
    """记录每个写者"在工具里"的时间区间。"""

    def __init__(self) -> None:
        self.intervals: dict[str, tuple[float, float]] = {}

    def enter(self, tag: str) -> float:
        return asyncio.get_running_loop().time()

    def exit(self, tag: str, start: float) -> None:
        self.intervals[tag] = (start, asyncio.get_running_loop().time())


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两个区间的交叠时长（<= 0 表示没有同时在场）。"""
    return min(a[1], b[1]) - max(a[0], b[0])


class WriteProbe(Tool):
    """假的写工具：`writes_path = True`，每次执行都真的写一次文件。

    真写文件（追加 `tag:start` / `tag:end`）是为了让"交错"在**数据**上也留下痕迹：
    租约失效时不只是时间线交叠，文件内容也会变成 start,start,end,end。
    """

    name = "write_probe"
    description = "append markers to a file"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    writes_path = True

    def __init__(self, timeline: _Timeline, *, delay: float = 0.02) -> None:
        self.timeline = timeline
        self.delay = delay

    async def execute(self, arguments, ctx):
        tag = arguments["tag"]
        # 用 abspath（不解析符号链接）对齐注册表的键推导，见 registry._path_key：
        # 这样测试盯的是租约本身，而不是"resolve() 会不会把临时目录规范化成另一种写法"。
        # 没给 path 时落到各自的工作区文件：这类调用按契约**不该**被租约碰到，
        # 所以它必须能独立跑完（真交错才好判定）。
        raw = arguments.get("path") or f"unleased-{tag}.txt"
        target = Path(os.path.abspath(Path(ctx.workspace) / raw))
        start = self.timeline.enter(tag)
        try:
            body = target.read_text(encoding="utf-8") if target.exists() else ""
            target.write_text(body + f"{tag}:start\n", encoding="utf-8")
            await asyncio.sleep(self.delay)
            body = target.read_text(encoding="utf-8")
            target.write_text(body + f"{tag}:end\n", encoding="utf-8")
            return f"{tag} ok"
        finally:
            self.timeline.exit(tag, start)


class PlainProbe(Tool):
    """不声明 `writes_path` 的工具：即便参数里有 path，也必须完全并行。"""

    name = "plain_probe"
    description = "not a writer"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    def __init__(self, timeline: _Timeline, *, delay: float = 0.05) -> None:
        self.timeline = timeline
        self.delay = delay

    async def execute(self, arguments, ctx):
        tag = arguments["tag"]
        start = self.timeline.enter(tag)
        try:
            await asyncio.sleep(self.delay)
            return f"{tag} ok"
        finally:
            self.timeline.exit(tag, start)


def test_same_path_writers_never_interleave(tmp_path):
    """同一个文件的两个写者在同一轮里必须串行：标记不能交错。"""

    async def flow():
        timeline = _Timeline()
        reg = ToolRegistry()
        reg.register(WriteProbe(timeline))
        ctx = _ctx(tmp_path)
        _touch_dir(tmp_path, "src")
        results = await reg.dispatch_all(
            [("write_probe", {"path": "src/a.py", "tag": "A"}),
             ("write_probe", {"path": "src/a.py", "tag": "B"})],
            ctx,
        )
        assert all(isinstance(r, str) and r.endswith("ok") for r in results), results
        # 数据层面：两次写各自首尾相接，绝不出现 start,start,end,end。
        lines = (tmp_path / "src" / "a.py").read_text(encoding="utf-8").split()
        assert lines in (["A:start", "A:end", "B:start", "B:end"],
                         ["B:start", "B:end", "A:start", "A:end"]), lines
        # 时间层面：两个区间不相交。
        assert _overlap(timeline.intervals["A"], timeline.intervals["B"]) <= 0, timeline.intervals

    asyncio.run(flow())


def test_different_paths_still_run_concurrently(tmp_path):
    """不同文件绝不能被串行化：两个调用必须同时在场（租约不是全局互斥锁）。"""

    async def flow():
        timeline = _Timeline()
        reg = ToolRegistry()
        reg.register(WriteProbe(timeline, delay=0.08))
        ctx = _ctx(tmp_path)
        _touch_dir(tmp_path, "src")
        await _enter_together(
            reg.dispatch_all([("write_probe", {"path": "src/a.py", "tag": "A"})], ctx),
            reg.dispatch_all([("write_probe", {"path": "src/b.py", "tag": "B"})], ctx),
        )
        # 两个写者各自至少睡 80ms。串行执行时后者的进入时间必然晚于前者的退出时间，
        # 交叠 <= 0；只有真的并发（两个协程同时在 await 里）才有正的交叠时长。
        assert _overlap(timeline.intervals["A"], timeline.intervals["B"]) > 0, timeline.intervals

    asyncio.run(flow())


def test_non_write_tool_ignores_path(tmp_path):
    """不声明 writes_path 的工具：参数里有同一个 path 也不串行。"""

    async def flow():
        timeline = _Timeline()
        reg = ToolRegistry()
        reg.register(PlainProbe(timeline, delay=0.05))
        ctx = _ctx(tmp_path)
        await reg.dispatch_all(
            [("plain_probe", {"path": "src/a.py", "tag": "A"}),
             ("plain_probe", {"path": "src/a.py", "tag": "B"})],
            ctx,
        )
        assert _overlap(timeline.intervals["A"], timeline.intervals["B"]) > 0, timeline.intervals

    asyncio.run(flow())


def test_write_tool_without_path_is_not_serialized(tmp_path):
    """写了 writes_path 但没给 path：没有可归一的键，按原样并行。"""

    async def flow():
        timeline = _Timeline()
        reg = ToolRegistry()
        reg.register(WriteProbe(timeline, delay=0.05))
        ctx = _ctx(tmp_path)
        await _enter_together(
            reg.dispatch_all([("write_probe", {"tag": "A"})], ctx),
            reg.dispatch_all([("write_probe", {"tag": "B"})], ctx),
        )
        assert _overlap(timeline.intervals["A"], timeline.intervals["B"]) > 0, timeline.intervals

    asyncio.run(flow())


def test_path_forms_and_case_share_one_lease(tmp_path):
    """相对路径 / `./` 前缀 / 绝对路径 / 大小写差异必须落到同一把租约上。"""

    async def flow():
        _touch_dir(tmp_path, "src", "SRC")
        variants = [
            "src/a.py",                                      # 工作区相对
            "./src/a.py",                                    # 带 ./ 前缀
            str(tmp_path / "src" / "a.py"),                  # 绝对路径
            "SRC\\A.PY" if os.name == "nt" else "SRC/A.PY",  # 大小写/分隔符差异
        ]
        for left, right in ((0, 1), (0, 2), (0, 3)):
            timeline = _Timeline()
            reg = ToolRegistry()
            reg.register(WriteProbe(timeline))
            ctx = _ctx(tmp_path)
            await reg.dispatch_all(
                [("write_probe", {"path": variants[left], "tag": "A"}),
                 ("write_probe", {"path": variants[right], "tag": "B"})],
                ctx,
            )
            overlap = _overlap(timeline.intervals["A"], timeline.intervals["B"])
            assert overlap <= 0, (
                f"{variants[left]} 与 {variants[right]} 没落到同一把租约：{timeline.intervals}"
            )

    asyncio.run(flow())


def test_filtered_registry_shares_lease_with_inner(tmp_path):
    """M21 包装层必须借用 inner 的租约表：跨注册表视图写同一个文件也要串行。"""

    async def flow():
        timeline = _Timeline()
        reg = ToolRegistry()
        reg.register(WriteProbe(timeline))
        filtered = FilteredToolRegistry(reg, exclude=("other_tool",))
        ctx = _ctx(tmp_path)
        _touch_dir(tmp_path, "src")

        # 内层与包装层共用一张表（不要求"同一个对象"，只要求同一把锁）。
        assert filtered._lease_table() is reg._write_lease

        # 一次走内层、一次走包装层，写同一个文件。
        results = await _enter_together(
            reg.dispatch_all([("write_probe", {"path": "src/a.py", "tag": "A"})], ctx),
            filtered.dispatch_all([("write_probe", {"path": "src/a.py", "tag": "B"})], ctx),
        )
        assert all(r[0].endswith("ok") for r in results), results
        assert _overlap(timeline.intervals["A"], timeline.intervals["B"]) <= 0, timeline.intervals
        lines = (tmp_path / "src" / "a.py").read_text(encoding="utf-8").split()
        assert lines in (["A:start", "A:end", "B:start", "B:end"],
                         ["B:start", "B:end", "A:start", "A:end"]), lines

    asyncio.run(flow())


def test_lease_table_does_not_grow(tmp_path):
    """一批互不相同的路径写完后，路径→锁的表必须回到空（不泄漏、不长期增长）。"""

    async def flow():
        timeline = _Timeline()
        reg = ToolRegistry()
        reg.register(WriteProbe(timeline))
        ctx = _ctx(tmp_path)
        _touch_dir(tmp_path, "src")
        calls = [("write_probe", {"path": f"src/f{i}.py", "tag": f"t{i}"}) for i in range(12)]
        results = await reg.dispatch_all(calls, ctx)
        assert all(r.endswith("ok") for r in results), results
        assert reg._write_lease.size() == 0, "写租约表在写者走完后仍然留着条目"

        filtered = FilteredToolRegistry(reg)
        await filtered.dispatch_all(calls, ctx)
        assert reg._write_lease.size() == 0
        assert filtered._own_write_lease.size() == 0      # 兜底表一次都没被用到

    asyncio.run(flow())


def test_failing_writer_releases_lease(tmp_path):
    """工具抛异常时必须放开租约：同一个文件的下一个写者不能被卡死。"""

    class BoomTool(Tool):
        name = "boom_writer"
        description = "fails after touching the path"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
        writes_path = True

        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, arguments, ctx):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return "second ok"

    async def flow():
        boom = BoomTool()
        reg = ToolRegistry()
        reg.register(boom)
        ctx = _ctx(tmp_path)

        # 单个 dispatch_all 内：先失败的那个不能把后一个挡在租约外。
        results = await asyncio.wait_for(
            reg.dispatch_all(
                [("boom_writer", {"path": "src/a.py"}), ("boom_writer", {"path": "src/a.py"})],
                ctx,
            ),
            timeout=5.0,
        )
        assert results[0] == "[tool error] boom", results
        assert results[1] == "second ok", results
        assert reg._write_lease.size() == 0

        # 异常之后再来一轮：租约表干净，立刻能拿到锁。
        again = await asyncio.wait_for(
            reg.dispatch_all([("boom_writer", {"path": "src/a.py"})], ctx), timeout=5.0
        )
        assert again == ["second ok"], again
        assert reg._write_lease.size() == 0

    asyncio.run(flow())


def test_bad_path_value_never_breaks_the_turn(tmp_path):
    """path 是个连 Path() 都构造失败的值时，租约退回原始字符串，调用照常。"""

    class NastyPath:
        def __str__(self) -> str:
            return "src/a.py"

        def __fspath__(self):
            raise TypeError("no path for you")

    seen: list[str] = []

    class NastyWriter(Tool):
        name = "nasty_writer"
        description = "declares writes_path but gets a path-like object"
        parameters = {"type": "object", "properties": {"path": {}}}
        writes_path = True

        async def execute(self, arguments, ctx):
            seen.append(str(arguments["path"]))
            return "ok"

    async def flow():
        reg = ToolRegistry()
        reg.register(NastyWriter())
        ctx = _ctx(tmp_path)
        results = await reg.dispatch_all([("nasty_writer", {"path": NastyPath()})], ctx)
        assert results == ["ok"], results
        assert seen == ["src/a.py"]
        assert reg._write_lease.size() == 0

    asyncio.run(flow())


def test_dispatch_all_keeps_order_and_error_contract(tmp_path):
    """返回契约不变：长度/顺序与 calls 对齐，异常收敛成 `[tool error] ...`。"""

    class FailTool(Tool):
        name = "always_fail"
        description = "always fails"

        async def execute(self, arguments, ctx):
            raise RuntimeError("nope")

    class EchoTool(Tool):
        name = "echo"
        description = "echo text"

        async def execute(self, arguments, ctx):
            return arguments.get("text", "")

    async def flow():
        reg = ToolRegistry()
        reg.register(FailTool())
        reg.register(EchoTool())
        ctx = _ctx(tmp_path)
        results = await reg.dispatch_all(
            [("echo", {"text": "a"}), ("always_fail", {}), ("echo", {"text": "b"}),
             ("missing_tool", {"path": "src/a.py"})],
            ctx,
        )
        assert len(results) == 4
        assert results[0] == "a" and results[2] == "b"
        assert results[1].startswith("[tool error]")
        assert results[3].startswith("[tool error]") and "missing_tool" in results[3]
        assert reg._write_lease.size() == 0

    asyncio.run(flow())