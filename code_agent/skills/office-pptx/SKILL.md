---
name: office-pptx
description: 生成 PowerPoint（.pptx）交付物：先用 python-pptx；库不在时如实说明并给替代交付格式，不要假装能生成。
---

# 生成 PowerPoint（.pptx）

## 第一步：自检（决定能不能做，不要跳过）

```bash
"<python>" -c "import pptx; print('ok')"
```

`<python>` 用 `load_workspace_dependencies` 给出的**工作区解释器绝对路径**。

### 库不在时怎么办（**必须明说**）

`ModuleNotFoundError: No module named 'pptx'` 时：

1. 告诉用户需要 `pip install -e .[office]`（或 `pip install python-pptx`），装完继续；
2. 或者换格式交付：写成 Markdown/HTML 大纲用 `present` 交付，并说清原因。

不要手写一个"看起来像 pptx"的 zip：损坏文件比不给更糟（用户打不开，还会以为是平台坏了）。

## 2. 常规路径（python-pptx）

```python
from pptx import Presentation
from pptx.util import Inches, Pt

prs = Presentation()                        # 默认模板：16:9 视版本而定，别假设尺寸
layout = prs.slide_layouts[1]               # 1 = 标题+内容
slide = prs.slides.add_slide(layout)
slide.shapes.title.text = "季度复盘"
slide.placeholders[1].text = "结论先行\n· 指标 A ↑12%\n· 指标 B 持平"

for title, bullets in pages:
    s = prs.slides.add_slide(layout)
    s.shapes.title.text = title
    tf = s.placeholders[1].text_frame
    tf.text = bullets[0]                    # 第一行用 .text，其余用 add_paragraph
    for line in bullets[1:]:
        p = tf.add_paragraph()
        p.text = line
        p.level = 0
prs.save("reports/quarterly.pptx")
```

要点：
- **版式（layout）决定占位符**：`slide_layouts[1]` 有标题+内容；`[6]` 是纯标题。
  往不存在的占位符里写会 `IndexError`——拿不准就先打印
  `[(i, l.name) for i, l in enumerate(prs.slide_layouts)]`；
- 文本框/表格/图片要显式给位置与尺寸（`Inches`/`Pt`），没有"自动排版"；
- 中文字体同样要显式设（`run.font.name` + 东亚字体），否则可能显示成方块；
- 每页字数要克制：一页放不下的内容拆页，比硬塞更可读。

## 3. 交付

```
present(files=[{"path": "reports/quarterly.pptx", "description": "季度复盘 8 页"}])
```

用 `present`（只传路径）。

## 4. 诚实的边界

- 平台**没有** LibreOffice 转换/预览服务：不要承诺"生成后能在线预览"；
- 动画、切换、母版设计、SmartArt python-pptx 基本做不了；
- 图表要么用 `add_chart`（原生但配置项多），要么先在别处把图画成 PNG 再插进来；
- 写完自己验：文件存在、`zipfile.ZipFile(path).namelist()` 里有 `ppt/presentation.xml`。
