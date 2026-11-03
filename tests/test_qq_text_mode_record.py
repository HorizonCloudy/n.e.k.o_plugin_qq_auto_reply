import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply.pipeline_models import QQDeliveryPlan
from plugin.plugins.qq_auto_reply.reply_delivery_node import QQReplyDeliveryNode
from plugin.plugins.qq_auto_reply.reply_postprocess_node import QQReplyPostprocessNode


@pytest.mark.parametrize("target", ["group", "private"])
@pytest.mark.parametrize("text,expected", [("", "语音内容"), ("正文", "正文\n语音内容"), ("语音内容", "语音内容")])
def test_text_mode_converts_model_record_to_text(target, text, expected):
    synth = AsyncMock()
    client = SimpleNamespace(
        send_group_message=AsyncMock(return_value="mid"),
        send_group_message_segments=AsyncMock(return_value="mid"),
        send_message=AsyncMock(return_value="mid"),
        send_group_record=AsyncMock(), send_private_record=AsyncMock(),
    )
    plugin = SimpleNamespace(
        qq_client=client, voice_reply_service=SimpleNamespace(synthesize_reply_voice_file=synth),
        _get_reply_mode=lambda: "text", logger=Mock(), _emit_log=Mock(),
    )
    blocks = QQReplyPostprocessNode._parse_blocks(
        f"<msg><reply>123</reply><at>456</at><text>{text}</text><record>语音内容</record></msg>"
    )
    plan = QQDeliveryPlan(target_type=target, target_id="789", blocks=blocks,
                          fallback_to_text_on_voice_failure=False)
    result = asyncio.run(QQReplyDeliveryNode(plugin).deliver(plan))
    assert result.delivered
    if target == "group":
        client.send_group_message_segments.assert_awaited_once_with("789", [
            {"type": "reply", "data": {"id": "123"}},
            {"type": "at", "data": {"qq": "456"}},
            {"type": "text", "data": {"text": expected}},
        ])
    else:
        client.send_message.assert_awaited_once_with("789", "[CQ:reply,id=123][CQ:at,qq=456]" + expected)
    synth.assert_not_called()
    client.send_group_record.assert_not_called()
    client.send_private_record.assert_not_called()
    assert "语音" not in plugin.logger.info.call_args.args[0]


def test_direct_record_send_checks_current_mode_and_outbound_guard():
    synth = AsyncMock()
    client = SimpleNamespace(send_group_message=AsyncMock())
    plugin = SimpleNamespace(
        qq_client=client, voice_reply_service=SimpleNamespace(synthesize_reply_voice_file=synth),
        _get_reply_mode=lambda: "text", logger=Mock(), _emit_log=Mock(),
        outbound_guard_service=SimpleNamespace(check=Mock(return_value=SimpleNamespace(
            blocked=True, reason="blacklist", detail="blocked",
        ))),
    )
    blocks = QQReplyPostprocessNode._parse_blocks("<msg><record>语音内容</record></msg>")
    plan = QQDeliveryPlan(target_type="group", target_id="789", blocks=blocks)
    assert not asyncio.run(QQReplyDeliveryNode(plugin)._send_record(plan, blocks[0]))
    synth.assert_not_called()
    client.send_group_message.assert_not_called()
