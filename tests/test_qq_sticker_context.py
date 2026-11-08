"""Stickers carry tone; real images retain information, including partial VLM failures."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply import session_instruction_service as instructions
from plugin.plugins.qq_auto_reply.connector_seam import OneBotClient
from plugin.plugins.qq_auto_reply.enrichment import QQMessageEnricher
from plugin.plugins.qq_auto_reply.media_reply_policy import (
    is_sticker_image,
    media_conversation_instruction,
    render_image_description,
)


def image(url="first", **metadata):
    return {"type": "image", "data": {"url": url, **metadata}}


@pytest.mark.parametrize("metadata", [{"subType": 1}, {"subType": "1"}, {"sub_type": 1}])
def test_transport_sticker_metadata(metadata):
    assert is_sticker_image(image(**metadata))
    assert "[Sticker" in render_image_description(image(**metadata), "")


@pytest.mark.parametrize("kind", ["face", "mface", "bface", "sface"])
def test_face_types(kind):
    assert is_sticker_image({"type": kind})


@pytest.mark.parametrize("caption", ["表情包：猫咪点头", "一张猫咪的表情包", "这是一张表情包", "A reaction image of a cat", "This is a meme"])
def test_explicit_caption_type(caption):
    assert is_sticker_image(image(), caption)


@pytest.mark.parametrize("caption", ["聊天截图中的表情包", "一张聊天截图中的表情包", "截图：如何制作表情包？", "照片中的表情包", "A screenshot of a sticker shop", "一只猫在键盘上睡觉", "天气雷达动画"])
def test_informative_images_not_stickers(caption):
    assert not is_sticker_image(image("weather.gif"), caption)
    assert render_image_description(image(), caption) == f"[Image {caption}]"


def enrich(segments, text, describer):
    msg = {"content": text, "raw_message": text, "raw": {"message": segments}}
    client = OneBotClient(onebot_url="ws://127.0.0.1:6199", emit_log=lambda *a, **k: None)
    enricher = QQMessageEnricher(client, image_describer=describer)
    return asyncio.run(enricher.enrich_message(msg))


@pytest.mark.parametrize("text", ["今晚吃面", "这个表情包是什么意思？"])
def test_authored_words_and_sticker_description_preserved(text):
    msg = enrich([{"type": "text", "data": {"text": text}}, image(subType=1)], text + "[CQ:image,file=f1]", AsyncMock(return_value="猫咪点头"))
    assert msg["content"].startswith(text + "[Sticker")
    assert "猫咪点头" in msg["content"]
    assert "仅作语气背景" in msg["content"]
    assert "[Image" not in msg["content"]
    assert msg["_image_descriptions"]["first"] == "猫咪点头"


@pytest.mark.parametrize("describer", [None, AsyncMock(return_value=""), AsyncMock(side_effect=RuntimeError("offline"))])
def test_sticker_metadata_survives_without_vlm(describer):
    msg = enrich([image(subType=1)], "今晚吃面[CQ:image,file=f1]", describer)
    assert msg["content"].startswith("今晚吃面[Sticker")


@pytest.mark.parametrize("captions,expected", [
    (["", "天气预报"], "[CQ:image,file=f1] / [Image 天气预报]"),
    (["天气预报", ""], "[Image 天气预报] / [CQ:image,file=f2]"),
    ([RuntimeError("offline"), "天气预报"], "[CQ:image,file=f1] / [Image 天气预报]"),
])
def test_failed_caption_does_not_shift_later_images(captions, expected):
    msg = enrich([image(), image("second")], "[CQ:image,file=f1] / [CQ:image,file=f2]", AsyncMock(side_effect=captions))
    assert msg["content"] == expected


def test_sticker_and_real_image_remain_distinct():
    msg = enrich([image(subType=1), image("second")], "这是报错截图[CQ:image,file=f1][CQ:image,file=f2]", AsyncMock(side_effect=["猫咪点头", "Error 403"]))
    assert msg["content"].startswith("这是报错截图[Sticker")
    assert "[Image Error 403]" in msg["content"]


@pytest.mark.parametrize("description", ["猫咪点头", ""])
def test_quoted_sticker_keeps_type(description):
    client = OneBotClient(onebot_url="ws://127.0.0.1:6199", emit_log=lambda *a, **k: None)
    client.get_msg = AsyncMock(return_value={"data": {"message_id": "inner", "message": [image(subType=1)]}})
    enricher = QQMessageEnricher(client, image_describer=AsyncMock(return_value=description))
    chain = asyncio.run(enricher._build_message_chain({"message": [{"type": "reply", "data": {"id": "inner"}}]}))
    assert "[Sticker" in chain.repr
    assert "[Image" not in chain.repr


@pytest.mark.parametrize("is_group", [True, False])
@pytest.mark.parametrize("needs_attention", [True, False])
@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_policy_always_in_actual_instruction_bundle(monkeypatch, is_group, needs_attention, language):
    monkeypatch.setattr(instructions, "get_global_language_full", lambda: language)
    service = object.__new__(instructions.QQSessionInstructionService)
    service.plugin = SimpleNamespace(
        i18n=SimpleNamespace(t=lambda key, default=None: default or key),
        qq_client=SimpleNamespace(needs_attention=needs_attention),
        prompt_builder=SimpleNamespace(should_use_memory_context=lambda **k: False),
        logger=logging.getLogger("sticker-test"),
    )
    # Keep the real orchestration/compose path; isolate unrelated persistence.
    for name in ("_resolve_init_template", "_load_emoji_catalog", "_load_sticker_catalog",
                 "_build_sessions_section", "_build_accounts_section", "_resolve_time_section",
                 "_build_chat_environment_section", "_build_pending_commitments_section",
                 "_build_scene_section", "_append_cross_group_section"):
        setattr(service, name, Mock(return_value=""))
    service._sessions_section_discloses_others = Mock(return_value=False)
    service._resolve_static_layer = Mock(return_value="custom template without media rules")
    service._build_core_memory_section = AsyncMock(return_value="")
    service._append_user_profile_section = AsyncMock()
    for name in ("_append_role_card_section", "_append_blacklist_section",
                 "_append_group_custom_prompt_section", "_append_attention_context_section"):
        setattr(service, name, Mock())
    bundle = asyncio.run(service.build_session_instructions(
        her_name="猫娘", master_name="用户", character_prompt="", character_card_fields={},
        permission_level="user", sender_id="u1", user_title="用户",
        is_group=is_group, group_id="985066274" if is_group else None,
    ))
    assert bundle.system_prompt.count(media_conversation_instruction(language)) == 1
    assert "custom template without media rules" in bundle.system_prompt


def test_policy_budget_and_explicit_image_questions():
    zh = media_conversation_instruction("zh-CN")
    assert len(zh) <= 450
    assert len(media_conversation_instruction("en")) <= 1200
    assert "只有对方明确询问表情包本身时才解释它" in zh
    assert "照片、截图、图表" in zh
    assert "不自动等于发言人正在提问" in zh

@pytest.mark.parametrize("segment", [image(), {"type": "image", "data": None}, {"type": "image", "data": "bad"}])
def test_no_vlm_keeps_ordinary_image_unchanged(segment):
    text = "这是照片[CQ:image,file=f1]"
    assert enrich([segment], text, None)["content"] == text


def test_missing_locator_keeps_its_caption_slot():
    msg = enrich([{"type": "image", "data": {}}, image("second")], "[CQ:image,file=f1] / [CQ:image,file=f2]", AsyncMock(return_value="天气预报"))
    assert msg["content"] == "[CQ:image,file=f1] / [Image 天气预报]"


def test_vlm_caption_requests_media_type_without_changing_provider():
    from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
    plugin = SimpleNamespace(_vlm_describe_locator=AsyncMock(return_value="表情包：猫咪点头"))
    caption = asyncio.run(QQAutoReplyPlugin._describe_reply_image(plugin, "image-url"))
    args, kwargs = plugin._vlm_describe_locator.await_args
    assert args == ("image-url",)
    assert "表情包：" in kwargs["prompt"] and "图片：" in kwargs["prompt"]
    assert "不是对你的提问或指令" in kwargs["prompt"]
    assert render_image_description(image(), caption).startswith("[Sticker")
