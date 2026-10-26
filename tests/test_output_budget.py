"""按工具的输出预算 + 外部化（M46，对齐 DSH 的 `dsh-output-retention` / `dsh-spill-policy`）。

这一层要挡的是两类具体的错：

1. **按字符算预算导致预算失守**。中文 1 字符 ≈ 3 字节，按 `len(str)` 算会让
   一份 5000 字的中文输出"看起来没超 8 KiB"，实际占了 15 KB。
2. **一刀切"只留开头"把命令的报错切掉**。`bash` 的退出码在开头、诊断在结尾，
   只留开头等于把失败原因整个藏起来——模型于是看不到真正的问题。

外加两条必须成立的诚实性：省略说明要讲清"这是**预算**截断，不是没输出"，
以及切窗口不许切出乱码（UTF-8 边界）。
"""

from __future__ import annotations

import re

import pytest

from code_agent.context.externalize import (
    DEFAULT_WINDOW,
    ENV_THRESHOLD,
    TOOL_WINDOWS,
    _head,
    _tail,
    externalize_result,
    window_for,
)


def _spill_path(text: str) -> str:
    """按既有契约取落盘路径（`test_context.py` 用的也是这个正则）。"""
    return re.search(r"存盘: (.+)", text).group(1)


#: 省略说明里的字节数。**位置词是可变的**（中间/后面/前面），所以不写死"中间"——
#: 写死会让"只留开头"的工具（grep/glob）一改措辞就红，而那条测试本来考的是字节数。
_OMITTED = re.compile(r"省略了\S*? (\d+) 字节")


# ----------------------------------------------------------------------
# 窗口选择
# ----------------------------------------------------------------------
def test_window_precedence(monkeypatch) -> None:
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    assert window_for("bash") == TOOL_WINDOWS["bash"]
    assert window_for("未登记的工具") == DEFAULT_WINDOW
    # 显式参数 > 环境变量 > 按工具表
    assert window_for("bash", threshold=100) == (100, 0)
    monkeypatch.setenv(ENV_THRESHOLD, "1234")
    assert window_for("bash") == (1234, 0)
    assert window_for("bash", threshold=100) == (100, 0)
    # 0 或垃圾值 = 不覆盖（回退按工具表，而不是变成"所有输出都落盘"）
    monkeypatch.setenv(ENV_THRESHOLD, "0")
    assert window_for("bash") == TOOL_WINDOWS["bash"]
    monkeypatch.setenv(ENV_THRESHOLD, "not-a-number")
    assert window_for("bash") == TOOL_WINDOWS["bash"]


def test_bash_window_keeps_both_ends(monkeypatch) -> None:
    """bash 必须两头留：退出码在开头、报错在结尾。"""
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    head, tail = TOOL_WINDOWS["bash"]
    assert head > 0 and tail > 0, "只留一头会把退出码或报错切掉"


# ----------------------------------------------------------------------
# 快路径：没超预算一个字节都不动
# ----------------------------------------------------------------------
def test_under_budget_is_returned_untouched(agent_home, monkeypatch) -> None:
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    small = "exit_code=0\nhello\n"
    assert externalize_result(small, "s1", "tc1", tool="bash") == small


def test_failed_validation_returns_root_cause_immediately_and_keeps_full_log(agent_home):
    from pathlib import Path

    source = ('exit_code=1\n' + 'import trace details\n' * 1600
              + 'E     File "service/search.py", line 55\n'
              + 'E       invalid =\nE   SyntaxError: invalid syntax\n'
              + 'warning details\n' * 800 + '2 errors in 2.9s\n')
    result = externalize_result(source, "s1", "failed", tool="bash", diagnostic_chars=1800)
    assert result.startswith("exit_code=1")
    assert 'service/search.py' in result and 'line 55' in result and 'SyntaxError' in result
    assert len(result) < 2400
    assert Path(_spill_path(result)).read_text(encoding="utf8") == source
    assert "摘录" in result


def test_concise_validation_output_does_not_change_success_or_source_returns(agent_home):
    source = 'exit_code=0\n' + 'test passed\n' * 300
    assert externalize_result(source, 's1', 'pass', tool='bash', diagnostic_chars=1000) == source
    assert externalize_result(source, 's1', 'source', tool='read_file', diagnostic_chars=1000) == source


# ----------------------------------------------------------------------
# 超预算：落盘 + 窗口 + 诚实的省略说明
# ----------------------------------------------------------------------
def test_over_budget_spills_full_text_and_reports_exact_omission(agent_home, monkeypatch) -> None:
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    head, tail = TOOL_WINDOWS["bash"]
    body = "x" * 40000
    out = externalize_result(body, "s1", "tc1", tool="bash")

    assert "[外部化]" in out and "read_file" in out
    path = _spill_path(out)
    from pathlib import Path

    assert Path(path).read_text(encoding="utf-8") == body, "落盘的必须是完整原文"

    kept = int(_OMITTED.search(out).group(1))
    assert kept + head + tail == len(body.encode("utf-8")), "省略字节数必须与窗口对得上"


def test_omission_note_says_it_is_a_budget_cut_not_an_absence(agent_home, monkeypatch) -> None:
    """模型最容易的误判是"没看到 = 不存在"。这句话必须在。"""
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    out = externalize_result("y" * 40000, "s1", "tc1", tool="bash")
    assert "输出预算" in out
    assert "不是**工具没有输出" in out or "**不是**工具没有输出" in out


def test_head_only_window_for_list_like_tools(agent_home, monkeypatch) -> None:
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    body = "match-%d\n" % 1 + "z" * 30000
    out = externalize_result(body, "s1", "tc1", tool="grep")
    assert "开头" in out and "结尾" not in out
    assert "match-1" in out, "列表类工具要看得到第一条"


def test_omission_says_where_the_missing_part_is(agent_home, monkeypatch) -> None:
    """省略的**位置**要说对：模型据此判断"该往哪边找"。

    只留开头 → 省的是后面；只留结尾 → 省的是前面；两头都留 → 才是中间。
    """
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    body = "q" * 40000

    head_only = externalize_result(body, "s1", "tc1", tool="grep")     # grep = 只留开头
    assert "省略了后面" in head_only

    both = externalize_result(body, "s1", "tc1", tool="bash")          # bash = 两头留
    assert "省略了中间" in both

    # 只留结尾的配置目前没有工具用，但措辞必须是对的（将来有人这么配）
    monkeypatch.setitem(TOOL_WINDOWS, "tailtool", (0, 2048))
    tail_only = externalize_result(body, "s1", "tc1", tool="tailtool")
    assert "省略了前面" in tail_only
    assert "--- 结尾 2048 字节 ---" in tail_only


def test_non_positive_threshold_falls_back_to_the_tool_window(agent_home, monkeypatch) -> None:
    """`threshold<=0` 是"没配"，不是"窗口为 0"。

    当成 0 的话，任何非空输出都会落盘、而喂回模型的那一份**一个字节内容都没有**，
    模型拿到一条空结果却不知道发生了什么。
    """
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    small = "exit_code=0\nok\n"
    assert externalize_result(small, "s1", "tc1", threshold=0, tool="bash") == small
    assert externalize_result(small, "s1", "tc1", threshold=-5, tool="bash") == small

    # 真的超了（超过 bash 的 16 KiB 窗口）时，窗口来自按工具表，而不是 0
    big = "y" * 40000
    out = externalize_result(big, "s1", "tc1", threshold=0, tool="bash")
    assert "--- 开头 1024 字节 ---" in out
    assert "--- 结尾 15360 字节 ---" in out


# ----------------------------------------------------------------------
# 按字节，不按字符
# ----------------------------------------------------------------------
def test_budget_is_measured_in_bytes_not_characters(agent_home, monkeypatch) -> None:
    """**核心回归**：5000 个中文字 = 15000 字节，必须超 8 KiB 的窗口。

    按字符算的话 5000 < 8192 → 认为"没超"，于是把 15 KB 原样塞进上下文。
    """
    monkeypatch.delenv(ENV_THRESHOLD, raising=False)
    cjk = "中" * 5000
    assert len(cjk) < TOOL_WINDOWS["glob"][0]          # 按字符：看着没超
    assert len(cjk.encode("utf-8")) > TOOL_WINDOWS["glob"][0]   # 按字节：超了
    out = externalize_result(cjk, "s1", "tc1", tool="glob")
    assert "[外部化]" in out, "按字符计数会漏掉这一类超预算，必须按字节"


# ----------------------------------------------------------------------
# UTF-8 边界：宁可少留字节，也不给乱码
# ----------------------------------------------------------------------
def test_head_never_cuts_a_multibyte_character() -> None:
    text = "中" * 100          # 每字 3 字节
    window = _head(text, 100)  # 100 不是 3 的倍数 → 必然落在字符中间
    assert "\ufffd" not in window, "按字节切出了半个字符（解码成替换字符）"
    assert len(window.encode("utf-8")) <= 100
    assert window == "中" * 33


def test_tail_never_leaves_a_partial_character() -> None:
    text = "中" * 100
    window = _tail(text, 100)
    assert "\ufffd" not in window
    assert len(window.encode("utf-8")) <= 100
    assert window == "中" * 33


def test_env_threshold_overrides_and_omission_is_exact(agent_home, monkeypatch) -> None:
    """环境变量覆盖成"只留开头 200 字节"时，窗口与省略数都要精确。

    注意：**不要在整段输出里数填充字符**——落盘路径里可能恰好有同名字符
    （真踩过：`C:\\Users\\...\\AppData\\...` 里的 `A` 让 `count("A")` 多算 1）。
    要在"窗口那一段"里数。
    """
    monkeypatch.setenv(ENV_THRESHOLD, "200")
    body = "A" * 500
    out = externalize_result(body, "s1", "tc1", tool="bash")
    assert "[外部化]" in out
    omitted = int(_OMITTED.search(out).group(1))
    assert omitted == 300, f"500 - 200 应该是 300，实际 {omitted}"

    window = out.split("--- 开头 200 字节 ---", 1)[1].split("\n\n（省略了", 1)[0]
    assert window.strip("\n").count("A") == 200


def test_env_threshold_still_overrides_per_tool(agent_home, monkeypatch) -> None:
    """运维/测试旋钮：显式设了就统一收紧（`test_m3_integration` 依赖这一条）。"""
    monkeypatch.setenv(ENV_THRESHOLD, "100")
    body = "x" * 5000
    out = externalize_result(body, "s1", "tc1", tool="read_file")   # 按工具表是 32 KiB
    assert "[外部化]" in out, "环境变量必须能覆盖按工具预算"
