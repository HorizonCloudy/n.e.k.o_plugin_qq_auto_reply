from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugin.plugins.qq_auto_reply.pipeline_models import QQMessageBlock, QQReplyOutcome, QQReplyRequest
from plugin.plugins.qq_auto_reply.reply_delivery_node import QQReplyDeliveryNode
from plugin.plugins.qq_auto_reply.reply_postprocess_node import QQReplyPostprocessNode


def plan(text, **kwargs):
    request = QQReplyRequest(message_text="hi", sender_id="12345", is_group=True,
                             group_id="98765", current_message_id="456", **kwargs)
    outcome = QQReplyOutcome(action="reply", blocks=[QQMessageBlock(text=text)])
    return QQReplyPostprocessNode(None).build_delivery_plan(request, outcome)


async def test_plain_mention_and_missing_quote_become_native_segments():
    delivery = plan("@小明 你好", member_labels={"12345": "小明"})
    block = delivery.blocks[0]
    assert block.text == "你好"
    assert block.at_user == "12345"
    assert block.reply_to == "456"
    send = AsyncMock(return_value="sent-id")
    plugin = SimpleNamespace(_get_reply_mode=lambda: "text",
                             qq_client=SimpleNamespace(send_group_message_segments=send))
    node = QQReplyDeliveryNode(plugin)
    node._outbound_allowed = lambda *args: True
    assert await node._send_text(delivery, block, node._compose_text(block))
    segments = send.call_args.args[1]
    assert segments[:2] == [{"type": "reply", "data": {"id": "456"}},
                            {"type": "at", "data": {"qq": "12345"}}]
    assert all("[CQ:" not in item["data"].get("text", "") for item in segments)


def test_ambiguous_unknown_or_partial_names_are_not_guessed():
    for text in ("@小明 你好", "@陌生人 你好", "@小明同学 你好", "邮件 a@小明.com"):
        delivery = plan(text, member_labels={"12345": "小明", "23456": "小明"})
        assert not delivery.blocks[0].at_user
        assert delivery.blocks[0].text == text


def test_synthetic_turn_does_not_invent_quote():
    assert not plan("总结", source_kind="summary").blocks[0].reply_to


def test_explicit_quote_preserved_without_mutating_draft():
    request = QQReplyRequest(message_text="hi", sender_id="12345", is_group=True,
                             group_id="98765", current_message_id="456")
    original = QQMessageBlock(text="回复", reply_to="789")
    outcome = QQReplyOutcome(action="reply", blocks=[original])
    delivery = QQReplyPostprocessNode(None).build_delivery_plan(request, outcome)
    assert delivery.blocks[0].reply_to == "789"
    assert delivery.blocks[0] is not original
