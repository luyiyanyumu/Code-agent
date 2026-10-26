"""随包自带的 Office 技能（M49，对齐 DSH 的 `dsh-skill-office` 思路）。

DSH 的办公链是"`load_workspace_dependencies` 给解释器 + `skill-office` 给指令与脚本 +
`libreoffice-kit` 做转换"。code-agent 这边**不自己写 docx/xlsx 代码**（那是重复造轮子，
而且维护成本会摊在平台身上）：把**指令 + 可跑脚本**做成随包技能。

这个文件钉住三件事，每件都对应一种"看起来做了其实没做"：

1. **技能真的被发现**（目录进 system prompt、`load_skill` 读得到、附带资源列得对）；
2. **xlsx 那条路真的开箱可用**——venv 里一个 Office 库都没有，所以随包脚本必须
   **只用标准库**，而且要真的跑得通、产出的文件真的是合法 xlsx（不是"看起来像"的 zip）；
3. **缺库时如实停下**——docx/pptx 的正文必须写明安装命令与替代交付格式，
   不许硬写一段 `import` 然后拿 ImportError 去猜别的原因。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from code_agent.config import builtin_skills_dir, skills_dir
from code_agent.extensions import PackSource, SkillsRegistry

SKILL_NAMES = ("office-xlsx", "office-docx", "office-pptx")

#: 随包脚本**只允许用标准库**——它的全部价值就是"零依赖开箱可用"。
STDLIB_OK = {
    "argparse", "csv", "io", "json", "re", "sys", "zipfile",
    "xml.sax.saxutils", "pathlib", "__future__",
}


def _registry(extra_local: Path | None = None) -> SkillsRegistry:
    root = extra_local or skills_dir()
    return SkillsRegistry(root, [PackSource(builtin_skills_dir(), "builtin")])


def _script_path() -> Path:
    return builtin_skills_dir() / "office-xlsx" / "scripts" / "write_xlsx.py"


def _load_script():
    """把随包脚本当模块加载（测函数级行为，不必起子进程）。

    加载时**关掉字节码写入**：否则 `import` 会在随包技能目录里长出一个
    `scripts/__pycache__`——测试改动了要发布的源码树，是测试自己的脏。
    """
    spec = importlib.util.spec_from_file_location("ca_write_xlsx", _script_path())
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


# ----------------------------------------------------------------------
# 1. 技能文件本身
# ----------------------------------------------------------------------
def test_shipped_skills_exist_with_name_and_description() -> None:
    """frontmatter 里必须有 name + description。

    没写 description 的技能在目录里显示成 `- xxx: `（空的），模型于是完全不知道
    它是干什么的——`packs.fallback_description` 那段注释记的就是这个真机问题。
    """
    for name in SKILL_NAMES:
        doc = builtin_skills_dir() / name / "SKILL.md"
        assert doc.is_file(), f"随包技能缺文件：{doc}"
        text = doc.read_text(encoding="utf-8")
        meta = dict(
            line.split(":", 1)
            for line in re.match(r"\s*---\s*\n(.*?)\n---", text, re.DOTALL).group(1).splitlines()
            if ":" in line
        )
        assert meta.get("name", "").strip() == name
        assert len(meta.get("description", "").strip()) > 10, f"{name} 的 description 太短/缺失"


def test_shipped_xlsx_script_uses_only_the_standard_library() -> None:
    """xlsx 脚本是"零依赖"承诺的载体：它一旦 import 第三方库，承诺当场失效。"""
    tree = ast.parse(_script_path().read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    outside = {m for m in imported if m not in STDLIB_OK and not m.startswith("xml.")}
    assert not outside, f"随包脚本引入了标准库之外的模块：{sorted(outside)}"


def test_shipped_scripts_are_syntactically_valid() -> None:
    """随包脚本必须能编译——坏脚本不该被发布出去。"""
    for path in builtin_skills_dir().rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_docx_and_pptx_skills_state_the_install_command_and_a_fallback() -> None:
    """缺库时必须"停下并说清"：安装命令 + 替代交付格式。

    这条是**文本断言**，脆一点但值得：它守的是"诚实"本身——
    改正文时如果把这段删了，技能就退化成"硬写 import 然后猜为什么失败"。
    """
    for name in ("office-docx", "office-pptx"):
        text = (builtin_skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
        assert 'pip install -e ".[office]"' in text or "pip install -e .[office]" in text, name
        assert "ModuleNotFoundError" in text, f"{name} 没写清「缺库时怎么判断」"
        assert "不要" in text, f"{name} 没写「不要怎么做」"


# ----------------------------------------------------------------------
# 2. 注册表集成
# ----------------------------------------------------------------------
def test_builtin_skills_are_discovered_with_origin(agent_home) -> None:
    reg = _registry()
    assert set(SKILL_NAMES) <= set(reg.names())
    for name in SKILL_NAMES:
        assert reg.origin(name) == "builtin"
        assert reg.load(name), f"{name} 读不到正文"


def test_catalog_lists_builtin_skills_with_their_descriptions(agent_home) -> None:
    catalog = _registry().catalog_text()
    for name in SKILL_NAMES:
        assert name in catalog
    assert "xlsx" in catalog and "Word" in catalog and "PowerPoint" in catalog


def test_resource_listing_covers_the_shipped_script(agent_home) -> None:
    """随包技能的脚本要被列成"可读资源"，模型才知道它存在、可以去读。"""
    resources = _registry().resource_files("office-xlsx")
    assert "scripts/write_xlsx.py" in resources, resources
    assert not any("__pycache__" in r or r.endswith(".pyc") for r in resources), resources


def test_resource_listing_skips_pycache_and_dotfiles(tmp_path) -> None:
    """`__pycache__` 与点开头的文件不进清单。

    清单是给模型"按需 read_file"用的；列一个 `.pyc` 或 `.DS_Store` 等于引导它去读
    二进制垃圾。实测就是这么发现的：脚本被编译过一次，清单里立刻多出一项。

    这里**复制一份到临时目录**再动手：测试绝不改动仓库里的随包技能
    （第一版就踩了——它在随包目录里建 `__pycache__`，清理时还因为里面有真 `.pyc`
    而 `rmdir` 失败）。
    """
    copy = tmp_path / "skills" / "office-xlsx"
    shutil.copytree(builtin_skills_dir() / "office-xlsx", copy)
    cache = copy / "scripts" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "write_xlsx.cpython-313.pyc").write_bytes(b"\x00")
    (copy / ".DS_Store").write_bytes(b"\x00")

    resources = SkillsRegistry(tmp_path / "skills").resource_files("office-xlsx")
    assert "scripts/write_xlsx.py" in resources
    assert not any(
        "__pycache__" in r or r.endswith(".pyc") or Path(r).name.startswith(".")
        for r in resources
    ), resources


def test_local_skill_overrides_the_builtin_one(agent_home) -> None:
    """用户目录优先：同名技能由用户的那份说了算（自带的是兜底，不是最高权）。"""
    local = skills_dir() / "office-xlsx"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text(
        "---\nname: office-xlsx\ndescription: 我自己写的 xlsx 技能\n---\n\n正文：听我的。\n",
        encoding="utf-8",
    )
    reg = _registry()
    assert reg.origin("office-xlsx") == "local"
    assert "听我的" in (reg.load("office-xlsx") or "")


def test_builtin_skill_can_be_disabled(agent_home) -> None:
    """停用走 `disabled_packs`（用户删不掉安装目录里的文件，停用才是正确动作）。"""
    reg = _registry()
    reg.set_disabled({"office-xlsx"})
    assert "office-xlsx" not in reg.catalog_text()
    assert reg.load("office-xlsx") is None, "停用后必须读不到（fail closed）"


# ----------------------------------------------------------------------
# 3. xlsx 脚本真的能用
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3", 3),
        ("3.5", 3.5),
        ("-12", -12),
        ("007", "007"),          # 编号不是数字
        ("0012.5", "0012.5"),
        ("2026-10-07", "2026-10-07"),
        ("3,200", "3,200"),
        ("nan", "nan"),
        ("", ""),
        ("张三", "张三"),
    ],
)
def test_number_inference_is_conservative(raw: str, expected) -> None:
    """数字推断保守优先：猜错数字（把编号/日期变数）比留成文本代价大得多。"""
    assert _load_script().coerce(raw) == expected


def test_script_produces_a_real_xlsx_with_correct_cell_types(tmp_path) -> None:
    """**核心**：真跑一遍脚本，并检查产出的 zip 结构与单元格类型。"""
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "项目,工时,编号,备注\n"
        "支付重构,12.5,007,张三负责\n"
        "登录优化,8,0012,无\n",
        encoding="utf-8",
    )
    out = tmp_path / "report.xlsx"
    proc = subprocess.run(
        [
            sys.executable, str(_script_path()),
            "--out", str(out), "--csv", str(csv_path), "--sheet", "Summary", "--widths", "16,8,10",
        ],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True and payload["rows"] == 3

    # 是个合法 zip，且关键部件都在
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        for part in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                     "xl/worksheets/sheet1.xml"):
            assert part in names, names
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        workbook = zf.read("xl/workbook.xml").decode("utf-8")

    assert 'name="Summary"' in workbook
    assert "<cols>" in sheet and 'customWidth="1"' in sheet

    # 数字写成数字单元格（Excel 里能直接求和）；编号与文本保持文本
    assert '<c r="B2"><v>12.5</v></c>' in sheet
    assert '<c r="B3"><v>8</v></c>' in sheet
    assert 't="inlineStr"><is><t xml:space="preserve">007</t>' in sheet
    assert "0012" in sheet and 't="inlineStr"' in sheet
    assert "张三负责" in sheet


def test_script_reports_rows_and_handles_headerless_input(tmp_path) -> None:
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("a,1\nb,2\n", encoding="utf-8")
    out = tmp_path / "o.xlsx"
    proc = subprocess.run(
        [sys.executable, str(_script_path()), "--out", str(out), "--csv", str(csv_path),
         "--no-header"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    with zipfile.ZipFile(out) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    # 没有表头 → 顶上补一行空行，数据从第 2 行开始
    assert '<c r="A2" t="inlineStr"><is><t xml:space="preserve">a</t>' in sheet


# ----------------------------------------------------------------------
# 4. 接线：Web 与 CLI 都要带上这个来源
# ----------------------------------------------------------------------
def test_web_and_cli_wire_the_builtin_source() -> None:
    """源码级守卫：两个 registry 都要把"随包技能"作为来源。

    只挂一半的后果很具体：目录里列着 office-xlsx，`load_skill` 却读不到
    （或反过来，CLI 里模型根本不知道有这个技能）。
    """
    root = Path(__file__).resolve().parents[1]
    app_src = (root / "code_agent/api/app.py").read_text(encoding="utf-8")
    cli_src = (root / "code_agent/cli.py").read_text(encoding="utf-8")

    assert 'PackSource(builtin_skills_dir(), "builtin")' in app_src
    assert cli_src.count('PackSource(builtin_skills_dir(), "builtin")') >= 2, (
        "CLI 的 load_skill 与 system prompt 目录必须来自同一个来源集合"
    )
    assert "skills.catalog_text()" in cli_src, "CLI 没把技能目录注入 system prompt"


def test_packaging_declares_the_skills_as_package_data() -> None:
    """技能要随包发布：`pip install` 出来的包里没有 SKILL.md 的话，
    源码 checkout 里跑得好好的、装完就静默失效（典型的"只在打包后坏"）。"""
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "package-data" in pyproject and "skills/**/*.md" in pyproject
    assert "office" in pyproject and "python-docx" in pyproject and "python-pptx" in pyproject
