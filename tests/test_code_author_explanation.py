"""Recovered source documentation takes precedence over stored model briefs."""

from types import SimpleNamespace
from unittest.mock import Mock

from code_agent.codeintel.service import CodeIntelService


def test_recovered_javadoc_is_not_overwritten_by_a_model_brief():
    index = SimpleNamespace(symbol=lambda sid: {"name": "AgentService", "doc": ""})
    service = SimpleNamespace(briefs=Mock(side_effect=AssertionError("Author documentation must win")))
    explanation = {
        "headline": "智能体编排服务",
        "documentation": {"text": "聊天、润色与本地工具调用。", "line": 55, "source": "类型 Javadoc"},
        "facts": [{"label": "声明位置", "text": "AgentService.java:64"}],
    }
    CodeIntelService._inject_brief(service, {}, index, "sym:AgentService.java#AgentService", explanation)
    assert explanation["headline"] == "智能体编排服务"
    assert "brief" not in explanation
    service.briefs.assert_not_called()
