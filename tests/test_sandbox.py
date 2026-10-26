"""Shell 沙盒执行器单测：超时、输出上限、退出码。"""

import asyncio
import platform
import sys

import pytest

from code_agent.sandbox.executor import run_shell

#: 长任务命令：**必须平台无关**。
#: 早先用 `sleep 10`，在没装 Git Bash 的 Windows 上会被 cmd.exe 判为"不是内部命令"
#: 并立刻退出——结果测试变成在验"命令不存在"，而不是在验"超时会杀进程树"。
LONG_COMMAND = f'"{sys.executable}" -c "import time; time.sleep(10)"'


def test_timeout_kills_process(agent_home):
    result = asyncio.run(run_shell(LONG_COMMAND, timeout=1.0))
    assert "超时" in result


def test_exit_code_and_output(agent_home):
    result = asyncio.run(run_shell("echo hello-sandbox"))
    assert "hello-sandbox" in result
    assert "exit_code=0" in result


def test_failing_command_reports_code(agent_home):
    # M20：Windows 上装了 Git Bash 时 `bash` 工具**真的走 bash**（否则 `curl | head`
    # 这类命令会撞 `'head' is not recognized`）。所以命令语法要跟着实际 shell 走。
    from code_agent.sandbox.executor import find_bash

    if platform.system() == "Windows" and find_bash() is None:
        cmd = "cmd /c exit 3"
    else:
        cmd = "exit 3"
    result = asyncio.run(run_shell(cmd))
    assert "exit_code=3" in result


def test_output_truncation(agent_home):
    # 生成超过 64KB 的输出
    if platform.system() == "Windows":
        cmd = "python -c \"print('x' * 100000)\""
    else:
        cmd = "yes x | head -c 100000"
    result = asyncio.run(run_shell(cmd, max_output=1000))
    assert "已截断" in result
    assert len(result) < 2000
