"""Media understanding must not imply a group reply (no external API calls)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply.attention_gate_service import QQAttentionGateService
from plugin.plugins.qq_auto_reply.enrichment import QQMessageEnricher
from plugin.plugins.qq_auto_reply.media_reply_policy import authored_media_text
from plugin.plugins.qq_auto_reply.message_dispatcher import QQMessageDispatcher
from test_qq_auto_reply_attention_gate import _evaluate, _FakeAttention, _plugin
from test_qq_ban_reaction import _Recorder


def media(kind="image"):
    return {"type": kind, "data": {"url": "https://invalid.example/sticker.png"}}


def text(value):
    return {"type": "text", "data": {"text": value}}


@pytest.mark.parametrize("kind", ["image", "face", "mface", "bface", "sface"])
def test_media_segments_and_cq(kind):
    assert authored_media_text([text("  "), media(kind), {"type": "at"}]) == ""
    assert authored_media_text(f"[CQ:reply,id=7][CQ:{kind},id=1] ") == ""
    assert authored_media_text([media(kind), text("看看这个？")]) == "看看这个？"
    assert authored_media_text(f"[CQ:{kind},id=1]看看这个？") == "看看这个？"


@pytest.mark.parametrize("segments", [None, [], "普通文本", [text("你好")],
    [media(), {"type": "record"}], [media(), {"type": "file"}],
    [media(), {"type": "forward"}], [media(), {"type": []}],
    [media(), {"type": "text", "data": []}], [media(), "bad"],
    "[CQ:image,file=a][CQ:record,file=b]", "&#91;CQ:image,file=a&#93;"])
def test_unknown_or_complex_media_keeps_existing_behavior(segments):
    assert authored_media_text(segments) is None


def test_literal_escaped_cq_is_authored_text():
    assert authored_media_text("[CQ:image,file=a]&#91;CQ:at,qq=1&#93;&amp;") == "[CQ:at,qq=1]&"
    assert authored_media_text([media(), media("face"), text("问"), text("题？")]) == "问题？"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("admin", [True, False])
def test_passive_media_never_wakes_even_hot_group_admin_or_disabled_attention(enabled, admin):
    attention = _FakeAttention(scores={"g1": 99}, enabled=enabled)
    plugin = _plugin(attention)
    plugin.permission_mgr = SimpleNamespace(get_permission_level=lambda _: "admin" if admin else "normal")
    plugin._qq_settings["addressee_names"] = ["猫娘"]
    decision, _ = asyncio.run(_evaluate(plugin, segments=[media()], message_text="猫娘你怎么看？" * 50))
    assert (decision.action, decision.reason) == ("ignore", "passive_media_context")
    assert attention.calls == []


@pytest.mark.parametrize("directed", [{"is_at_bot": True}, {"is_reply_to_bot": True}, {"quoted_message_id": "sent"}])
def test_directed_media_allowed_without_force(directed):
    plugin = _plugin(_FakeAttention(scores={"g1": 0}))
    plugin.qq_client.sent_message_ids = {"sent": True}
    decision, _ = asyncio.run(_evaluate(plugin, segments=[media()], message_text="图片描述", **directed))
    assert decision.action == "reply"
    assert decision.force_reply is False


def test_actual_text_not_caption_drives_addressing_and_attention():
    plugin = _plugin(_FakeAttention(scores={"g1": 0}))
    plugin._qq_settings["addressee_names"] = ["猫娘"]
    plugin.attention_service.update_on_message = AsyncMock()
    decision, gate = asyncio.run(_evaluate(plugin, segments=[media(), text("随手分享")], message_text="猫娘你怎么看？" * 30))
    assert decision.action == "ignore"
    assert decision.reason.startswith("low_attention")
    assert plugin.attention_service.update_on_message.call_args.args[0]["content"] == "随手分享"
    decision, _ = asyncio.run(_evaluate(plugin, segments=[media(), text("猫娘这是什么？")], message_text="描述"))
    assert decision.reason == "named_bot" and decision.force_reply


def test_normal_group_relay_and_open_platform_unchanged():
    plugin = _plugin(_FakeAttention())
    plugin.group_permission_mgr = SimpleNamespace(get_group_level=lambda _: "normal")
    decision, _ = asyncio.run(_evaluate(plugin, segments=[media()]))
    assert decision.reason == "normal_group_passthrough"
    plugin.qq_client.needs_attention = False
    decision, _ = asyncio.run(_evaluate(plugin, segments=[media()], is_at_bot=True))
    assert decision.reason == "no_attention_needed" and decision.force_reply


def test_enriched_blacklist_still_applies():
    plugin = _plugin(_FakeAttention())
    plugin._qq_settings["backlog_labels"] = [{"id": "blacklist", "priority": -10, "keywords": ["过滤词"]}]
    decision, _ = asyncio.run(_evaluate(plugin, segments=[media(), text("分享")], message_text="图片包含过滤词"))
    assert decision.reason == "blacklist"


def test_repeated_passive_media_marked_reviewed_without_echo_or_generation():
    plugin = _plugin(_FakeAttention())
    plugin.attention_gate_service = QQAttentionGateService(plugin)
    plugin.backlog_store = SimpleNamespace(mark_message_reviewed=AsyncMock())
    plugin.repeat_echo_service = SimpleNamespace(maybe_echo=AsyncMock())
    dispatcher = QQMessageDispatcher(plugin)
    async def run():
        for i in range(10):
            await dispatcher.handle_group_message("g1", "u1", "解析描述", False,
                segments=[media()], current_message_id=str(i))
    asyncio.run(run())
    assert plugin.backlog_store.mark_message_reviewed.await_count == 10
    plugin.repeat_echo_service.maybe_echo.assert_not_awaited()
    assert plugin.attention_service.calls == []


def test_full_ingress_keeps_vlm_context_and_marks_passive_media_reviewed():
    plugin = _Recorder()
    plugin.attention_service = _FakeAttention()
    plugin.attention_gate_service = QQAttentionGateService(plugin)
    plugin.backlog_store = SimpleNamespace(mark_message_reviewed=AsyncMock())
    plugin.repeat_echo_service = SimpleNamespace(maybe_echo=AsyncMock())
    describe = AsyncMock(return_value="猫娘，你怎么看？")
    plugin.enricher = QQMessageEnricher(plugin.qq_client, image_describer=describe)
    message = {"message_type": "group", "group_id": "g1", "user_id": "u1", "message_id": "m1",
        "content": "[CQ:image,file=a]", "raw_message": "[CQ:image,file=a]", "message": [media()]}
    asyncio.run(QQMessageDispatcher(plugin).handle_message(message))
    describe.assert_awaited_once()
    assert "猫娘，你怎么看？" in plugin.backlog[0]["content"]
    plugin.backlog_store.mark_message_reviewed.assert_awaited_once_with("m1")
    plugin.repeat_echo_service.maybe_echo.assert_not_awaited()


def test_original_cq_captured_before_enrichment_overwrites_it():
    plugin = _Recorder()
    enricher = QQMessageEnricher(plugin.qq_client)
    async def enrich(message):
        message["content"] = message["raw_message"] = "猫娘你怎么看？"
        return message
    enricher.enrich_message = enrich
    plugin.enricher = enricher
    dispatcher = QQMessageDispatcher(plugin)
    dispatcher.handle_group_message = AsyncMock()
    asyncio.run(dispatcher.handle_message({"message_type": "group", "group_id": "g1", "user_id": "u1",
        "content": "[CQ:image,file=a]", "raw_message": "[CQ:image,file=a]"}))
    assert dispatcher.handle_group_message.call_args.kwargs["segments"] == "[CQ:image,file=a]"


def test_private_media_still_reaches_private_handler():
    plugin = _Recorder()
    dispatcher = QQMessageDispatcher(plugin)
    dispatcher.handle_private_message = AsyncMock()
    asyncio.run(dispatcher.handle_message({"message_type": "private", "user_id": "u1",
        "content": "[CQ:image,file=a]", "raw_message": "[CQ:image,file=a]"}))
    dispatcher.handle_private_message.assert_awaited_once()
