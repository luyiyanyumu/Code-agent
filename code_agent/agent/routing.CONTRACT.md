# 契约：`agent/routing.py`

> 本文件是 `code_agent/agent/routing.py` 的契约：**形状 / 义务 / 数据 / 拓扑在这里，
> 实现体在 .py 里**。人写这份文件，AI 只写函数体。
>
> 来源：阶段 0 实验（`docs/contract-first-phase0-report.md`）里那份契约，
> 按实测结论做了两处收紧——**证据要能机器定位**、**没有独立证据的义务要明确声明**。
>
> 检查：`contract_check` 工具，或
> `python -c "from pathlib import Path; from code_agent.contract import contract_check; print(contract_check(Path('code_agent/agent/routing.py')).summary())"`

## 1. 形状（shape）

```python
@dataclass(frozen=True)
class Route:
    kind: str
    label: str
    tools: tuple[str, ...]
    hint: str

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (...)
ROUTES: dict[str, Route] = {...}
DEFAULT_ROUTE: Route = ROUTES["edit"]
MAX_HINT_TOOLS: int = 6

def classify(prompt: str) -> Route: ...
def route_hint(route: Route, available: Iterable[str] | None = None, *, role: str = "") -> str: ...
```

- 不得新增公开符号；不得改动上述签名（含参数名、默认值、返回类型）。
- 允许的 import：`__future__` / `re` / `dataclasses` / `typing`。
- 模块级常量只校验**存在**，值属于 §3 的数据（人在那里复核）。

`role` 是可选的仅限关键字参数，默认空字符串保留普通路由提示。
当 `role="lead"` 且路由为 `edit` 或 `verify` 时，提示强调简单任务直接实施、验证和交付，
复杂任务或责任范围独立的子任务才按需委派；已有充分验证证据时不机械重跑。
该参数只调整提示，不改变可用工具过滤、权限、只读规划或工作树边界。
对应行为证据：`test_lead_routes_recommend_available_execution_tools_before_delegation`、
`test_lead_route_does_not_invent_tools_when_execution_is_filtered`。

## 2. 义务（obligations）

| id | 义务（必须成立的性质） | 证据 |
|---|---|---|
| OBL1 | `classify` 对空输入（`""`、`None`、纯空白）返回 `DEFAULT_ROUTE`，**不抛异常** | test_classify_picks_the_more_specific_task_type |
| OBL2 | 匹配按 `RULES` 的**顺序**短路：第一个命中的规则决定结果（"含动作词又含提问词"取靠前的动作类） | test_classify_picks_the_more_specific_task_type |
| OBL3 | 提问词规则带 `^\s*` 锚点，只在句首生效；句中出现的提问词不得覆盖更靠前的规则 | test_classify_picks_the_more_specific_task_type |
| OBL4 | `route_hint` 在给定 `available` 时**只列其中的工具**，顺序沿用 `route.tools` | test_route_hint_lists_only_available_tools |
| OBL5 | `route_hint` 列出的工具数不超过 `MAX_HINT_TOOLS` | 无独立证据（由 §3 的数据保证：现有 5 条路由的工具数都 ≤6，超限才会显现） |
| OBL6 | `available` 为空集合时输出**不含任何工具名**，形如 `[工具路由｜<label>] <hint>` | test_route_hint_lists_only_available_tools |
| OBL7 | 输出必须以 `[工具路由｜` + `route.label` + `]` 开头 | test_route_hint_lists_only_available_tools |
| OBL8 | 提示正文里**不得出现工具名**（名字只出现在推荐列表里） | test_answer_tasks_get_no_tool_list |
| OBL9 | `available=None` 时不过滤，列出 `route.tools`（截到 `MAX_HINT_TOOLS`）；结果真的进了 system prompt | test_route_hint_reaches_the_system_prompt_without_removing_tools |
| OBL10 | 纯函数：不修改入参、无可观察副作用（不读盘、不联网、无全局状态） | 无独立证据（静态约束，未判定） |

**输出行的文案属于义务**：`[工具路由｜<label>] <推荐列表>。<hint>`——列表用顿号连接、
以中文句号收尾。阶段 0 实验里就是因为这条没写，重生成版本把文案换成了
"推荐工具：A, B。"，而 5 个测试全绿、一条都没抓到（`docs/contract-first-phase0-report.md` §1）。

## 3. 数据（设计者的决定，原样落地）

`RULES`（顺序即优先级，越具体的动作越靠前）：

| 优先级 | kind | 关键词（大小写不敏感） |
|---|---|---|
| 1 | `edit` | 改 / 修改 / 修复 / 实现 / 新增 / 删除 / 重构 / 优化 / 补上 / 加上 / fix / refactor / implement / update / rewrite / patch / rename |
| 2 | `verify` | 测试 / 跑一下 / 验证 / 回归 / 复现 / 失败 / 报错 / 为什么挂 / test / verify / reproduce / debug |
| 3 | `research` | 搜索 / 查一下 / 最新 / 资料 / 调研 / 联网 / 官网 / 文档站 / search / look up / browse / latest |
| 4 | `locate` | 在哪 / 哪里定义 / 谁调用 / 影响面 / 架构 / 地图 / 梳理 / 定位 / 找一下 / 结构 / 调用链 / where / which file / who calls / impact / map / locate / explore |
| 5 | `answer` | **句首锚定** `^\s*`：解释 / 说明 / 为什么 / 是什么 / 怎么理解 / 举例 / 对比 / 区别 / what / why / how / explain |

`ROUTES`（每个 kind 的中文名、推荐工具、提示语）：

| kind | label | tools（顺序即展示顺序） | hint |
|---|---|---|---|
| `edit` | 改代码 | code_map, grep, read_file, edit_file, write_file, bash | 利用已有坐标和上下文完成最小必要修改；缺少事实时再查找，随后验证受影响的行为并交付。 |
| `verify` | 验证/排错 | bash, read_file, grep, list_dir | 选择与问题最相关的验证或复现，按真实输出定位并解决问题；已有当前版本有效结果时复用，只有新改动、失败或未解决疑点才补充验证。 |
| `research` | 联网调研 | web_search, web_fetch, read_file, deliver_file | 先搜后读：搜索拿候选，抓取只读真正要引用的那几篇；结论要能对上来源，不确定的写不确定。 |
| `locate` | 定位/看结构 | code_map, grep, list_dir, read_file | 优先使用已有文件和坐标；需要结构或入口线索时再搜索或查地图，足够回答问题即可，避免无关的整仓库调查。 |
| `answer` | 问答/解释 | （空） | 这类问题通常不需要调工具：直接答，不知道的部分就说不知道，不要为了显得在干活而翻仓库。 |

`DEFAULT_ROUTE = ROUTES["edit"]`——分类不出来时用它，**不返回 None**，避免调用点分叉。

## 4. 拓扑（topology）

| 边 | 关系 |
|---|---|
| `SessionRuntime._route_for_prompt(prompt)` → `classify` | 每条用户消息分类一次，结果暂存在运行时上 |
| `SessionRuntime._loop_context()` → `route_hint(route, registry.names(), role=当前会话角色)` | 渲染成一行写进 system prompt 末尾；`available` 必须是**当轮真实可用**的工具名，角色只调整执行与协调提示 |
| `route_hint` → `Route.tools` | 只读 |
| `classify` → `RULES` / `ROUTES` | 只读 |

**失败模式**：本模块不得抛异常（分类失败要退化成默认路由，而不是让整轮对话挂掉）；
`route_hint` 遇到 `available` 里不存在的工具名不做任何报错，只是不列它。
