"""M36：档案列表里不再有自动播种的「当前配置」那一行。

它当年是"升级后行为不变"的兼容措施：用当前生效配置播种一条档案。但它代表的是
"此刻生效的配置"，而那东西本来就在设置页上方单独编辑；它又是预设不可删，
用户想从列表里去也去不掉。现在不播种了，老数据里的它会被**清掉**（不是藏起来）。
"""
from __future__ import annotations

import json

import pytest

from code_agent import settings


@pytest.fixture()
def home(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    return agent_home


def _write_profiles(home, data: dict) -> None:
    (home / "providers.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_no_seeded_profile_on_a_fresh_install(home) -> None:
    """全新安装：档案列表是空的（原来会凭空多出一行「当前配置」）。"""
    assert settings.list_profiles() == []


def test_legacy_seed_is_removed_from_the_stored_file(home) -> None:
    """老数据里的 mp_seed 必须被清掉并落盘，而不是每次读的时候藏一下。"""
    _write_profiles(home, {
        "active": "mp_seed",
        "profiles": [
            {"id": "mp_seed", "name": "当前配置", "preset": True, "provider": "deepseek",
             "base_url": "", "model_default": "deepseek-chat", "api_key": "sk-x"},
            {"id": "mp_a", "name": "本地 Ollama", "provider": "openai_compat",
             "base_url": "http://127.0.0.1:11434/v1", "model_default": "qwen3:8b", "api_key": "",
             "model_lite": "qwen3:8b", "model_craft": "qwen3:8b"},
        ],
    })

    names = [p["name"] for p in settings.list_profiles()]
    assert names == ["本地 Ollama"], names

    stored = json.loads((home / "providers.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in stored["profiles"]] == ["mp_a"], stored
    assert stored["active"] == "", "生效标记指向被删掉的假档案时应清空"
    # 去档位后遗留的字段也要跟着清掉（不然文件里还写着三档，让人以为功能还在）
    assert "model_lite" not in stored["profiles"][0]
    assert "model_craft" not in stored["profiles"][0]


def test_seed_helper_recognises_both_shapes(home) -> None:
    """按 id 或按"preset + 名字"两种形态都要认出来（老版本写法可能不一致）。"""
    assert settings._is_legacy_seed({"id": "mp_seed", "name": "别的名字"})
    assert settings._is_legacy_seed({"id": "mp_x", "name": "当前配置", "preset": True})
    assert not settings._is_legacy_seed({"id": "mp_x", "name": "当前配置"})  # 没有 preset 标记 → 用户自己建的，保留
    assert not settings._is_legacy_seed({"id": "mp_x", "name": "DeepSeek"})


def test_user_profile_can_be_deleted_even_when_active(home) -> None:
    """删掉"生效中"那条：只清生效标记，不动 config.json 里的当前配置。"""
    settings.save({"provider": "deepseek", "model_default": "keep-me"})
    view = settings.save_profile({
        "name": "我的档案", "provider": "deepseek", "model_default": "another-model",
    })
    settings.activate_profile(view["id"])
    assert settings.model_config()["model_default"] == "another-model"

    settings.delete_profile(view["id"])
    assert settings.list_profiles() == []
    # 档案删了，但"当前生效配置"没被顺手动过
    assert settings.model_config()["model_default"] == "another-model"


def test_builtin_templates_are_not_profiles(home) -> None:
    """内置模板只是「+ 按钮」的模板：不在列表里，用它可以建出普通档案。"""
    assert settings.list_profiles() == []
    assert settings.BUILTIN_PRESETS, "模板表不该为空"
    tpl = next(t for t in settings.BUILTIN_PRESETS if t["name"] == "DeepSeek")
    view = settings.save_profile(dict(tpl))
    assert view["name"] == "DeepSeek"
    assert "preset" not in view, "视图里不再有 preset 字段（不存在不可删除的档案这回事了）"
    settings.delete_profile(view["id"])
    assert settings.list_profiles() == []
