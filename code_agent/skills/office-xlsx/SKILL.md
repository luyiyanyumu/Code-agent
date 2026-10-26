---
name: office-xlsx
description: 生成 Excel（.xlsx）交付物：零依赖脚本先把 CSV 变 xlsx，需要格式/公式/多表时再用 openpyxl。
---

# 生成 Excel（.xlsx）

## 第一步永远是自检（不要跳过）

```bash
"<python>" -c "import openpyxl; print(openpyxl.__version__)"
```

`<python>` 用 `load_workspace_dependencies` 给出的**工作区解释器绝对路径**，别用
PATH 里的 `python`（可能是另一只、也可能是系统那只）。

- 成功 → 两条路都能走（下面 §3 的格式能力可用）；
- `ModuleNotFoundError: No module named 'openpyxl'` → **走 §2 的零依赖脚本**，
  或者明确告诉用户"需要 `pip install -e .[office]`（或 `pip install openpyxl`）"。
  **不要**在没有库的情况下硬写一段 `import openpyxl` 的代码然后猜它为什么失败。

## 2. 默认路径：零依赖脚本（推荐先走这条）

本技能自带 `scripts/write_xlsx.py`，**只用标准库**（xlsx 本质是装了几份 XML 的 zip）：

```bash
"<python>" scripts/write_xlsx.py --out reports/weekly.xlsx --csv /tmp/weekly.csv \
    --sheet Summary --widths 16,10,10
```

- 输入是 CSV（UTF-8，带不带 BOM 都行；分隔符自动嗅探，也可以 `--delimiter ;`）；
- 数字自动识别成数字单元格（Excel 里能直接求和）；`007`、`2026-10-07`、`3,200`
  这类**保持文本**——猜错数字比留成文本代价大得多；
- 第一行当表头；`--no-header` 表示没有表头；
- 需要造 CSV 时用 Python 的 `csv` 模块写（别手拼字符串，字段里可能有逗号/引号/换行）。

它**不做**：字体、颜色、边框、合并、公式、冻结窗格、图表、筛选。
要这些就装 openpyxl 走 §3。

## 3. 需要格式时：openpyxl

```python
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

wb = Workbook()
ws = wb.active
ws.title = "Summary"
ws.append(["项目", "工时", "占比"])
for cell in ws[1]:                       # 表头加粗（openpyxl 里没有"整行加粗"的快捷方式）
    cell.font = Font(bold=True)
    cell.fill = PatternFill("solid", fgColor="EEF2F7")
for name, hours, share in rows:
    ws.append([name, hours, share])
ws["C2"].number_format = "0.0%"
ws.column_dimensions["A"].width = 24
ws.freeze_panes = "A2"
wb.save("reports/weekly.xlsx")
```

注意：
- **数字要给数字类型**（`3` 不是 `"3"`），否则 Excel 不认它是数；
- 百分比用 `number_format`，不要自己拼 `"12.5%"` 字符串；
- 多表用 `wb.create_sheet("明细")`。

## 4. 交付

产物落到工作区之后，用 **`present`** 把它声明为交付物（只传路径，不传内容）：

```
present(files=[{"path": "reports/weekly.xlsx", "description": "本周工时汇总"}])
```

不要用 `deliver_file`：那个要你把文件内容当字符串传进来，二进制根本传不过去。

## 5. 诚实的边界

- 表很大（>10 万行）时，先分页或汇总再写；一次性写出几百 MB 的 xlsx 既慢又难交付。
- 需要**读取**已有 xlsx 时，零依赖脚本帮不上（它只写）；没装 openpyxl 就如实说明，
  或者让用户把源文件转成 CSV（`libreoffice --headless --convert-to csv`）。
- 写完之后**自己验证一下**：文件在不在、大小是否合理、能不能被 zipfile 打开。
  交付一个损坏的 xlsx 比不交付更糟。
