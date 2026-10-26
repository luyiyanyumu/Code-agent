"""工具注册表与分发（s02）。

- register: 注册工具实例
- schemas:  给模型看的 function 定义列表
- dispatch: 按名称执行（未注册/不存在抛 ToolError）
- 并发分发：dispatch_all 用 asyncio.gather 并行执行多个 ToolCall
- FilteredToolRegistry / apply_disabled：按"用户禁用的工具"或"会话开关"摘掉工具
  （摘掉 = 不进 schema + 调不动；见 M19 联网开关、M21 一键禁用）
- 写租约（write lease）：声明了 `Tool.writes_path` 的调用按**目标绝对路径**串行，
  同一轮里两个写同一个文件的调用不再互相覆盖（见 WriteLeaseTable）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.models.approval import executable_arguments


def _path_key(ctx: ToolContext, raw: str) -> str:
    """把 `arguments["path"]` 归一成租约键（**解析后的绝对路径**）。

    为什么必须归一：`src/a.py`、`./src/a.py`、`{workspace}/src/a.py` 和
    `{workspace}/SRC/A.PY`（Windows）指的是同一个文件，而模型完全可能在同一轮里
    用不同写法写它们；键不统一 = 租约形同虚设，这些调用会照旧并发踩同一份内容。

    为什么用 `abspath` 而不是 `resolve()`：内置工具的边界校验走的是
    `ws / p`（**不解析符号链接**，见 `_WorkspaceBound._resolved`），租约键必须和
    真实写入落点用同一把尺子——若 `link.py` 是指向别处的符号链接，`resolve()` 会算出
    另一个键，而工具实际写的仍是 `link.py`，两边就对不上了。`abspath` 同样消掉
    `.` / `..` / 相对前缀，覆盖了"同一个文件的不同写法"这个真正的风险面。

    防御性：路径连解析都失败（类型怪异、`Path` 构造抛错等）时退回原始字符串——
    租约的职责是**减少**竞争，绝不能因为一个坏路径把整轮工具调用搞崩。
    """
    try:
        p = Path(raw)
        if not p.is_absolute():
            p = Path(ctx.workspace) / p
        key = os.path.normcase(os.path.abspath(p))
    except Exception:
        return str(raw)
    return key or str(raw)


class WriteLeaseTable:
    """按路径发放写租约：同一路径串行，不同路径照旧并发。

    实现要点（每一条都是并发正确性的必要条件，别看着啰嗦就简化掉）：

    - 一个键 = 一把 `asyncio.Lock`。**取锁和登记引用必须在同一个不 await 的临界区里
      完成**（本类的全部方法都是同步方法，天然如此）；否则"取到锁"与"登记引用"之间被
      切走，另一个协程可能把条目摘掉，同一路径就出现了两把不同的锁，串行立刻失效。
    - 引用计数统计的是"持有 + 正在等待"的总数，只有归零才允许删条目。等待者也占一个
      引用，所以持锁者放手后条目不会消失，等待者醒来拿到的仍是同一把锁（不会出现
      "旧锁释放后重建一把新锁，与旧等待者并行"这种致命交错）。
    - 不需要 WeakValueDictionary 之类花活：计数归零即摘，表的大小恒等于"此刻正在写
      或排队等写的路径数"，天然有界（测试用 `size()` 盯住它回到 0）。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    def lease(self, key: str) -> "_PathLease":
        """为 `key` 取一份租约（异步上下文管理器）。"""
        return _PathLease(self, key)

    @contextlib.asynccontextmanager
    async def lease_all(self, keys: list[str]):
        """一次取**多把**租约（同一次调用要写多个路径时用，例如 `edit_many`）。

        **按固定顺序取**（调用方已排序）：两两用不同顺序取锁必然死锁——A 拿 k1 等 k2、
        B 拿 k2 等 k1，两个协程互相等一辈子。排序是多锁场景下唯一简单的死锁预防。
        """
        held: list[_PathLease] = []
        try:
            for key in keys:
                lease = self.lease(key)
                await lease.__aenter__()
                held.append(lease)
            yield
        finally:
            # 逆序释放（与获取相反）：语义上没有强制要求，但保持栈式配对更不容易写错
            for lease in reversed(held):
                with contextlib.suppress(Exception):
                    await lease.__aexit__(None, None, None)

    def size(self) -> int:
        """表里还留着的路径数（正常情况下时刻回落到 0）。"""
        return len(self._locks)

    def _acquire(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._refs[key] = self._refs.get(key, 0) + 1
        return lock

    def _release(self, key: str) -> None:
        left = self._refs.get(key, 1) - 1
        if left > 0:
            self._refs[key] = left
            return
        # 计数归零：最后一个持有/等待者已经放手，摘除是安全的——此刻没有人在跑这条
        # 路径，新来的即使拿到新建的锁，也不可能与谁并行。
        self._refs.pop(key, None)
        self._locks.pop(key, None)


class _PathLease:
    """一次写租约的持有句柄：`async with table.lease(key): ...`。"""

    __slots__ = ("_table", "_key", "_lock")

    def __init__(self, table: WriteLeaseTable, key: str) -> None:
        self._table = table
        self._key = key
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self) -> "_PathLease":
        lock = self._table._acquire(self._key)
        try:
            await lock.acquire()
        except BaseException:
            # acquire() 抛出取消时没有取得所有权；即使 locked() 为真，也只是
            # 别的写者仍持有它。asyncio 自己负责取消与唤醒的交接，我们只退等待引用。
            self._table._release(self._key)
            raise
        # 只在 acquire 正常返回后记录所有权。两者之间没有 await，取消不会把
        # "正在等别人的锁"误判成"自己已经拿到锁"。
        self._lock = lock
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        # 只释放这份租约实际取得的锁。释放锁和退引用之间没有 await；等待者自己
        # 仍持有引用，只有最后一个持有者/等待者离开后才会摘除表项。
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()
            self._table._release(self._key)
        return False


def _lease_keys(tool: Any, arguments: dict[str, Any], ctx: ToolContext) -> list[str]:
    """这次调用要取哪些租约键（归一化 + **排序**）。

    排序有两个作用：多把锁的死锁预防（见 `lease_all`），以及让"同一组路径"永远
    产生同一个键序列（否则两个调用可能以不同顺序取同一组锁 → 死锁）。
    """
    if tool is None:
        return []
    arguments = executable_arguments(arguments)
    getter = getattr(tool, "write_targets", None)
    if callable(getter):
        try:
            raws = getter(arguments) or []
        except Exception:
            return []
    elif getattr(tool, "writes_path", False):
        # 鸭子类型对象（扩展塞进来的代理）可能只有 writes_path，没有 write_targets
        raw = arguments.get("path") if isinstance(arguments, dict) else None
        raws = [raw] if raw else []
    else:
        return []
    return sorted({_path_key(ctx, str(item)) for item in raws if str(item).strip()})


async def _dispatch_one(
    registry: Any,
    execute: Callable[[str, dict[str, Any], ToolContext], Any],
    name: str,
    arguments: dict[str, Any],
    ctx: ToolContext,
    table: WriteLeaseTable,
) -> str:
    """执行一次调用：写同一路径时先在写租约上排队，其余原样直通。

    这里刻意不做"工具是否存在"的额外判断：`execute` 就是各注册表自己的执行入口
    （`ToolRegistry.dispatch` 会抛 ToolError，`FilteredToolRegistry.dispatch` 会对被
    摘掉的工具抛会话级错误），错误文案与调用 `dispatch` 完全一致。
    """
    keys = _lease_keys(registry.get(name), arguments, ctx)
    if not keys:
        # 非写工具 / 没给路径 / 未知工具：零额外开销，与今天一模一样地并行。
        return await execute(name, arguments, ctx)
    async with table.lease_all(keys):
        return await execute(name, arguments, ctx)


def _collect(results: list[Any]) -> list[str]:
    """把 gather 的结果收敛成 `list[str]`（顺序与 calls 一一对应，异常变错误串）。"""
    return [r if isinstance(r, str) else f"[tool error] {r}" for r in results]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        #: 写租约表。同一注册表（乃至同一进程里各会话共享的这一份）共用一张，
        #: FilteredToolRegistry 也直接借用它，见 FilteredToolRegistry._lease_table。
        self._write_lease = WriteLeaseTable()

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool.name 不能为空")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    async def dispatch(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
        # 单次调用不取租约：一把锁要有人抢才有意义，孤零零一次调用不可能和自己竞争。
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"tool not found: {name!r}")
        arguments = executable_arguments(arguments)
        guard = (ctx.extra or {}).get("team_write_guard")
        if guard is None:
            return await tool.execute(arguments, ctx)
        if not callable(guard):
            raise ToolError("团队文件责任校验不可用，已拒绝执行")
        # This is an execution boundary, independent of permission mode and
        # approvals. Filtered registries delegate here, so acquire the team's
        # reservation only once and leave existing path leases to the caller.
        async with guard(name, arguments, ctx, tool):
            return await tool.execute(arguments, ctx)

    async def dispatch_all(
        self, calls: list[tuple[str, dict[str, Any]]], ctx: ToolContext
    ) -> list[str]:
        """并发执行多个工具调用（s02 的并发工具机制）。

        同一轮里两个写同一个文件的调用（两次 edit_file，或 write_file + edit_file）
        若放任并发，就是无保护的读-改-写：后写者基于旧内容覆盖前者，补丁账本还会记下
        一个从未真实发生过的顺序。所以声明了 `writes_path`、又确实给了 `path` 的调用，
        按解析后的绝对路径**加一把写租约**（同路径串行）；其余调用一步不多走，照旧全速
        并行。返回值的长度/顺序/异常转字符串的契约一字未动。
        """
        table = self._write_lease
        results = await asyncio.gather(
            *(_dispatch_one(self, self.dispatch, name, args, ctx, table) for name, args in calls),
            return_exceptions=True,
        )
        return _collect(results)

    def write_lease(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> "contextlib.AbstractAsyncContextManager[None]":
        """给**调用方自己执行**的一次工具调用取写租约（同路径串行，其余空上下文）。

        为什么注册表要把这件事暴露出去，而不是只在 `dispatch_all` 里做：

        真正并发扇出工具调用的是 **agent loop**（`AgentLoop._execute_tool`），它走的是
        `dispatch` 而不是 `dispatch_all`——因为那里还要过权限、审批、审计、外部化，
        是项目里"安全链路上的唯一入口"。只把租约做在 `dispatch_all` 里，
        等于在一个**没人走的路口**装了红绿灯：看起来修好了，实际每次都放行。

        所以租约的取用点必须由调用方决定：loop 在 `async with` 里包住自己的
        `_execute_tool`，而这里只负责"算键、给锁"这一件事。
        非写工具、没给路径、或工具不存在时返回 `contextlib.nullcontext()`——零开销，
        且 `async with` 对两种上下文都成立。

        **一次写多个路径的工具**（`edit_many`）会拿到多把锁（`lease_all`，按排序取，
        防死锁）：只锁第一个路径的话，另一个文件就是裸奔的。
        """
        keys = _lease_keys(self._tools.get(name), arguments, ctx)
        if not keys:
            return contextlib.nullcontext()
        return self._write_lease.lease_all(keys)


def apply_disabled(registry: Any, disabled: Iterable[str] | None = None) -> Any:
    """按"用户禁用的工具"包一层注册表（M21）。

    传 None 表示从 config.json 读当前禁用清单。清单为空时原样返回——
    不套一层无谓的代理，扫描目录时也少一层。
    """
    if disabled is None:
        from code_agent.settings import disabled_tools

        disabled = disabled_tools()
    names = tuple(sorted({str(n).strip() for n in disabled if str(n).strip()}))
    if not names:
        return registry
    return FilteredToolRegistry(registry, exclude=names)


class FilteredToolRegistry:
    """按名字**排除**若干工具后的注册表视图（M19 联网开关用它）。

    为什么不是"注册/注销"：工具实例是共享的（多个会话一份 registry），
    注销会影响到别的会话；而"看不见的工具"比"调了报错的工具"更干净——
    关掉联网时模型不该在 schema 里看到 `web_search`，否则它会尝试调用并浪费一轮。
    未排除的工具一律直接透传，行为与 ToolRegistry 完全一致。
    """

    def __init__(self, inner: ToolRegistry, *, exclude: tuple[str, ...] = ()) -> None:
        self._inner = inner
        self._exclude = frozenset(exclude)
        #: 兜底表：只有 inner 给不出租约表（鸭子类型、被换掉的实现）时才会用到。
        #: 注意不能"每次 dispatch_all 新建一张"——那等于没有租约。
        self._own_write_lease = WriteLeaseTable()

    def _visible(self) -> list[Tool]:
        return [t for name, t in self._inner._tools.items() if name not in self._exclude]

    def get(self, name: str) -> Tool | None:
        if name in self._exclude:
            return None
        return self._inner.get(name)

    def names(self) -> list[str]:
        return [t.name for t in self._visible()]

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._visible()]

    def _lease_table(self) -> WriteLeaseTable:
        """写租约表**取自 inner**。

        包装层与内层注册表可能同时在被用（M21 只在这一层摘工具，别处仍拿原注册表），
        若这层自己再造一张表，两个视图就会各拿一把锁去写同一个文件，串行保证当场失效。
        所以共享 inner 的那一张；`_inner` 缺失、或它没有这张表（鸭子类型注册表）时，
        才退回自己的兜底表——那时包装层内部依然串行，只是无法与一个不存在的表共享。
        """
        inner = getattr(self, "_inner", None)
        table = getattr(inner, "_write_lease", None)
        if isinstance(table, WriteLeaseTable):
            return table
        return self._own_write_lease

    async def dispatch(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
        if name in self._exclude:
            raise ToolError(f"tool not available in this session: {name!r}")
        return await self._inner.dispatch(name, executable_arguments(arguments), ctx)

    async def dispatch_all(
        self, calls: list[tuple[str, dict[str, Any]]], ctx: ToolContext
    ) -> list[str]:
        table = self._lease_table()
        results = await asyncio.gather(
            *(_dispatch_one(self, self.dispatch, name, args, ctx, table) for name, args in calls),
            return_exceptions=True,
        )
        return _collect(results)

    def write_lease(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> "contextlib.AbstractAsyncContextManager[None]":
        """同 `ToolRegistry.write_lease`，但租约表取自 inner（见 `_lease_table`）。

        被本层摘掉的工具根本不会执行，不必取租约。inner 若也提供 `write_lease`
        （正常的 `ToolRegistry` 都提供），直接委托——这样包装层与内层永远共用
        同一把按路径的锁，而不是各拿一把去写同一个文件。
        """
        if name in self._exclude:
            return contextlib.nullcontext()
        inner_lease = getattr(self._inner, "write_lease", None)
        if callable(inner_lease):
            return inner_lease(name, arguments, ctx)
        keys = _lease_keys(self._inner.get(name), arguments, ctx)
        if not keys:
            return contextlib.nullcontext()
        return self._lease_table().lease_all(keys)
