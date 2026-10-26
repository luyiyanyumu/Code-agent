"""M9 测试：.env 加载器（零依赖解析 + 优先级语义）。

背景：.env.example 一直让用户「复制为 .env 后填写」，但进程从未读过它，
配置只能靠手工 set 环境变量。这里锁住补齐后的行为。
"""

from __future__ import annotations

import os

import pytest

from code_agent.config import _parse_env_line, load_env_file


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "line,expected",
    [
        ("KEY=value", ("KEY", "value")),
        ("  KEY = value  ", ("KEY", "value")),
        ("KEY=", ("KEY", "")),
        ('KEY="quoted value"', ("KEY", "quoted value")),
        ("KEY='single'", ("KEY", "single")),
        ("export KEY=value", ("KEY", "value")),
        ("KEY=value=with=equals", ("KEY", "value=with=equals")),
        ("KEY=has spaces inside", ("KEY", "has spaces inside")),
        ("# comment", None),
        ("", None),
        ("   ", None),
        ("NO_EQUALS_SIGN", None),
        ("=novalue", None),
    ],
)
def test_parse_env_line(line: str, expected):
    assert _parse_env_line(line) == expected


def test_parse_keeps_hash_inside_value():
    """值里的 # 不该被当注释吃掉（密钥/口令常含特殊字符）。"""
    assert _parse_env_line("KEY=abc#def") == ("KEY", "abc#def")


# ----------------------------------------------------------------------
# 加载与优先级
# ----------------------------------------------------------------------
@pytest.fixture
def env_file(tmp_path):
    def _make(text: str):
        p = tmp_path / "test.env"
        p.write_text(text, encoding="utf-8")
        return p

    return _make


def test_loads_values_into_environ(env_file, monkeypatch):
    monkeypatch.delenv("CODE_AGENT_TEST_A", raising=False)
    monkeypatch.delenv("CODE_AGENT_TEST_B", raising=False)
    path = env_file(
        "\n".join(
            [
                "# 顶部注释",
                "CODE_AGENT_TEST_A=alpha",
                "",
                "export CODE_AGENT_TEST_B='beta value'",
            ]
        )
    )
    assert load_env_file(path) == path
    assert os.environ["CODE_AGENT_TEST_A"] == "alpha"
    assert os.environ["CODE_AGENT_TEST_B"] == "beta value"


def test_real_env_wins_over_env_file(env_file, monkeypatch):
    """核心语义：.env 只填空位，绝不覆盖已注入的环境变量。

    这保证容器/CI 注入的密钥永远优先于仓库里的本地兜底文件。
    """
    monkeypatch.setenv("CODE_AGENT_TEST_C", "from-real-env")
    path = env_file("CODE_AGENT_TEST_C=from-dotenv")
    load_env_file(path)
    assert os.environ["CODE_AGENT_TEST_C"] == "from-real-env"


def test_override_flag_forces_env_file(env_file, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_TEST_D", "from-real-env")
    path = env_file("CODE_AGENT_TEST_D=from-dotenv")
    load_env_file(path, override=True)
    assert os.environ["CODE_AGENT_TEST_D"] == "from-dotenv"


def test_missing_file_returns_none(tmp_path):
    assert load_env_file(tmp_path / "nope.env") is None


def test_idempotent_second_call(env_file, monkeypatch):
    monkeypatch.delenv("CODE_AGENT_TEST_E", raising=False)
    path = env_file("CODE_AGENT_TEST_E=first")
    assert load_env_file(path) == path
    assert os.environ["CODE_AGENT_TEST_E"] == "first"
    # 复用同一路径不会重复解析（模块级 _LOADED_ENV_FILES 去重）
    assert load_env_file(path) == path


def test_env_file_var_points_to_custom_path(env_file, monkeypatch):
    monkeypatch.delenv("CODE_AGENT_TEST_F", raising=False)
    path = env_file("CODE_AGENT_TEST_F=custom")
    monkeypatch.setenv("CODE_AGENT_ENV_FILE", str(path))
    assert load_env_file() == path
    assert os.environ["CODE_AGENT_TEST_F"] == "custom"


# ----------------------------------------------------------------------
# 与项目约定的联动
# ----------------------------------------------------------------------
def test_repo_env_example_is_parseable(tmp_path):
    """.env.example 里不能有解析不出来的行（用户会直接复制它）。"""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    assert example.is_file(), "缺少 .env.example"
    parsed = 0
    for raw in example.read_text(encoding="utf-8").splitlines():
        item = _parse_env_line(raw)
        if item is None:
            continue
        key, _ = item
        assert key.replace("_", "").isalnum(), f"可疑的键名：{key!r}"
        parsed += 1
    assert parsed >= 10  # 别让「注释掉整份文件」蒙混过关
