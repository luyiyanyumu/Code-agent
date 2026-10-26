---
name: office-docx
description: 生成 Word（.docx）交付物：先用 python-docx；库不在时如实说明并给出替代交付格式，不要假装能生成。
---

# 生成 Word（.docx）

## 第一步：自检（决定能不能做，不要跳过）

```bash
"<python>" -c "import docx; print(docx.__version__ if hasattr(docx,'__version__') else 'ok')"
```

`<python>` 用 `load_workspace_dependencies` 给出的**工作区解释器绝对路径**。

### 库不在时怎么办（**必须明说，不要硬写代码**）

`ModuleNotFoundError: No module named 'docx'` 时，只有两个诚实选项：

1. 告诉用户需要装：`pip install -e .[office]`（或 `pip install python-docx`）——
   装完再继续；
2. **换交付格式**：把内容写成 Markdown（`.md`）或 HTML（`.html`）用 `present` 交付，
   并在结论里说清"没有 python-docx，所以给的是 .md；需要 .docx 请先装 python-docx"。

**不要**生成一段 `import docx` 的代码然后拿 `ImportError` 去猜别的原因，也不要
手写一个"看起来像 docx"的 zip——那是个损坏文件，用户打不开，比不给更糟。

## 2. 常规路径（python-docx）

```python
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()
doc.add_heading("季度复盘", level=1)
doc.add_paragraph("结论先行：……")
for title, body in sections:
    doc.add_heading(title, level=2)
    doc.add_paragraph(body)
table = doc.add_table(rows=1, cols=3)
table.style = "Table Grid"                  # 不设 style 的话表格没有边框
hdr = table.rows[0].cells
hdr[0].text, hdr[1].text, hdr[2].text = "项目", "工时", "结论"
for row in rows:
    cells = table.add_row().cells
    cells[0].text, cells[1].text, cells[2].text = row
doc.add_page_break()
doc.save("reports/quarterly.docx")
```

要点：
- **中文字体**要显式设，否则某些环境下会退回一个没有中文字形的字体、显示成方块：
  `doc.styles["Normal"].font.name = "微软雅黑"`（并用 `rPr.rFonts` 设东亚字体）；
- 表格必须给 `style`（`Table Grid`）才有边框；
- 插入图片用 `doc.add_picture(path, width=Cm(14))`，路径必须是真实存在的文件；
- 页眉页脚/页码在 python-docx 里要手动组合 XML，属于"做不到就如实说"的范围。

## 3. 交付

```
present(files=[{"path": "reports/quarterly.docx", "description": "季度复盘"}])
```

用 `present`（只传路径）。`deliver_file` 要传文件内容字符串，二进制传不过去。

## 4. 诚实的边界

- 本平台**没有** LibreOffice 转换服务：不要试图在服务端把 docx 转 PDF/图片预览；
  用户拿到文件自己打开。
- 复杂排版（多级编号、目录域、交叉引用、修订）python-docx 做不了或很脆——
  能做多少说多少，别承诺。
- 写完之后自己验一下：文件存在、大小合理、`zipfile.ZipFile(path).namelist()` 能列出
  `word/document.xml`。
