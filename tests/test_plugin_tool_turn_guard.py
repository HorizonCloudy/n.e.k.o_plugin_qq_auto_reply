from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from plugin.plugins.qq_auto_reply.plugin_tool_turn_guard import (
    UNEXECUTED_SEARCH_REPLY,
    unexecuted_search_promise,
)
from plugin.plugins.qq_auto_reply.reply_generation_service import QQReplyGenerationService
from utils.llm_client import AIMessage


@pytest.mark.parametrize("text", [
    "<msg><text>好的，我现在搜索claude新模型的相关信息。</text></msg>",
    "我这就帮你搜索。", "我已经查到了。",
])
def test_detects_unexecuted_promises(text):
    assert unexecuted_search_promise(text)


@pytest.mark.parametrize("text", [
    "我现在不能搜索。", "我没有搜索。", "这次搜索失败了。", "你想搜索什么？",
    "我现在不知道搜索结果。", "搜索是获取最新信息的方法。",
])
def test_does_not_replace_honest_failures_or_ordinary_text(text):
    assert not unexecuted_search_promise(text)


def test_guard_replaces_outbound_and_only_current_history():
    plugin = SimpleNamespace(logger=Mock(), _emit_log=Mock())
    service = QQReplyGenerationService(plugin)
    old = AIMessage(content="我现在搜索旧主题。")
    current = AIMessage(content="好的，我现在搜索claude新模型的相关信息。")
    session = SimpleNamespace(_qq_tool_audit={"started": 0, "returned": 0},
                              _conversation_history=[old, current])
    chunks = [current.content]
    service._finish_tool_audit(session, "group:985066274", chunks, 1)
    assert chunks == [UNEXECUTED_SEARCH_REPLY]
    assert current.content == UNEXECUTED_SEARCH_REPLY
    assert old.content == "我现在搜索旧主题。"


def test_executed_search_keeps_answer():
    plugin = SimpleNamespace(logger=Mock(), _emit_log=Mock())
    session = SimpleNamespace(_qq_tool_audit={"started": 1, "returned": 1},
                              _conversation_history=[])
    chunks = ["我已经查到了。"]
    QQReplyGenerationService(plugin)._finish_tool_audit(session, "group:1", chunks, 0)
    assert chunks == ["我已经查到了。"]


def test_transport_diagnostic_never_logs_payload_or_credentials():
    plugin = SimpleNamespace(logger=Mock(), _emit_log=Mock())
    session = SimpleNamespace(_openai_tools_payload=lambda: [{"secret": "DO_NOT_LOG"}],
                              api_key="DO_NOT_LOG")
    QQReplyGenerationService(plugin)._log_tool_transport_state(session, "group:1")
    output = str(plugin.logger.mock_calls)
    assert "prepared_openai_tools=1" in output
    assert "DO_NOT_LOG" not in output
