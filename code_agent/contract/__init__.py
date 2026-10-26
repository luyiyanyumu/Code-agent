"""契约层（M32）：把"契约骨架编程"从一份文档落成项目里的一等产物。

## 它解决什么

阶段 0 实验（`docs/contract-first-phase0-report.md`）量出来的事实：

- 4 个独立实现、全部一次通过全部测试、契约层（签名/数据/import）**一字未改** →
  **"实现可丢弃"在证据覆盖范围内成立**；
- 但 37 个案例里 **16 处文案漂移**测试一条都没抓到，16 个边界探测点里 **2 处分歧**
  落在契约自己**字面冲突**的地方 → **证据的边界就是契约的边界**；
- 10 条被判定的义务里 **8 条**被实现者标为"只能靠猜"，而三个实现在其中 14 个未定义边界上
  **行为完全一致** → 危险不是"实现不一致"，而是**"缺口不被任何闸门发现"**。

所以这一层只做三件事，每件都有实验证据支撑：

| 模块 | 做什么 | 对应实验证据 |
|---|---|---|
| `parse.py` | 把 `*.CONTRACT.md` 解析成结构化契约（形状 / 义务 / 数据 / 拓扑） | 两份实验契约就是按这个格式写的，够用 |
| `shape.py` | 抽出实现文件的"形状快照"（公开符号 / 签名 / dataclass 字段 / import） | 实验里的 `gate.py` 就是这个，4 次实现零违例靠它验 |
| `check.py` | 契约 ↔ 实现的一致性检查：形状是否被改、义务有没有证据、实现体有没有义务 | "证据的边界就是契约的边界" → 把缺口**变成可枚举的清单** |

`check.py` 的返回口径刻意与 M29 的验收清单一致：给**进度 ρ**，并且**只详述第一个未满足项**。

**刻意不做的事**：不发明 DSL、不接管实现文件的读写、不做 AST 全量比对。
契约写在 markdown 里（人写、AI 只读），实现仍然就是普通 Python——这一层的价值是
"让缺口可见"，不是"发明一门新语言"。
"""

from code_agent.contract.check import (
    ContractReport,
    contract_check,
    find_contract,
    list_contracts,
)
from code_agent.contract.parse import Contract, Obligation, parse_contract
from code_agent.contract.shape import ShapeError, shape_of_source

__all__ = [
    "Contract",
    "ContractReport",
    "Obligation",
    "ShapeError",
    "contract_check",
    "find_contract",
    "list_contracts",
    "parse_contract",
    "shape_of_source",
]
