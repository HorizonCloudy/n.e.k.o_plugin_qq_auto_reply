"""Voice notes are standalone; reply/at metadata belongs to text messages."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply._vendor.connection_onebot.onebot_client import OneBotClient
from plugin.plugins.qq_auto_reply.voice_reply_service import QQVoiceReplyService


@pytest.mark.parametrize("scope,modifiers", [
    ("private", {}),
    ("private", {"reply_message_id": "456"}),
    ("group", {}),
    ("group", {"reply_message_id": "456"}),
    ("group", {"at_user_id": "789"}),
    ("group", {"reply_message_id": "456", "at_user_id": "789"}),
])
@pytest.mark.parametrize("receipt", ["voice-mid", None])
def test_record_sender_never_combines_voice_with_other_segments(
    monkeypatch, scope, modifiers, receipt,
):
    client = OneBotClient(onebot_url="ws://127.0.0.1:3001", direction="forward")
    send = AsyncMock(return_value=receipt)
    monkeypatch.setattr(client, f"send_{scope}_message_segments", send)

    result = asyncio.run(getattr(client, f"send_{scope}_record")(
        "123", "file:///voice.wav", **modifiers,
    ))

    assert result == receipt
    send.assert_awaited_once_with("123", [
        {"type": "record", "data": {"file": "file:///voice.wav"}},
    ])


@pytest.mark.parametrize("mode", ["voice", "both"])
@pytest.mark.parametrize("addressing", ["inline", "explicit"])
@pytest.mark.parametrize("receipt", ["voice-mid", None])
def test_group_voice_drops_modifiers_but_text_keeps_them(
    monkeypatch, mode, addressing, receipt,
):
    client = SimpleNamespace(
        supports_voice=True,
        send_group_record=AsyncMock(return_value=receipt),
        send_group_message_segments=AsyncMock(return_value="text-mid"),
    )
    service = QQVoiceReplyService(SimpleNamespace(
        qq_client=client,
        _validate_outbound_message=lambda text: text,
        _get_reply_mode=lambda: mode,
        logger=Mock(),
    ))
    synth = AsyncMock(return_value=("file:///voice.wav", "audio/wav"))
    monkeypatch.setattr(service, "synthesize_reply_voice_file", synth)
    text = "你好"
    modifiers = {}
    if addressing == "inline":
        text = "[CQ:reply,id=456][CQ:at,qq=789]" + text
    else:
        modifiers = {"reply_message_id": "456", "at_user_id": "789"}

    assert asyncio.run(service.deliver_group_reply(
        "123", text,
        voice_text="语音内容" if mode == "both" else "",
        fallback_to_text_on_voice_failure=True,
        **modifiers,
    ))

    synth.assert_awaited_once_with("语音内容" if mode == "both" else "你好")
    client.send_group_record.assert_awaited_once_with("123", "file:///voice.wav")
    if mode == "both" or receipt is None:
        client.send_group_message_segments.assert_awaited_once()
        args = client.send_group_message_segments.call_args.args
        assert args[0] == "123"
        assert args[1][:2] == [
            {"type": "reply", "data": {"id": "456"}},
            {"type": "at", "data": {"qq": "789"}},
        ]
        assert "".join(seg["data"]["text"] for seg in args[1][2:]).strip() == "你好"
    else:
        client.send_group_message_segments.assert_not_awaited()
