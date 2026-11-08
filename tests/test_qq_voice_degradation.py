"""Host speech failure still degrades to QQ text without hiding programming errors."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply.pipeline_models import QQDeliveryPlan, QQMessageBlock
from plugin.plugins.qq_auto_reply.reply_delivery_node import QQReplyDeliveryNode
from plugin.plugins.qq_auto_reply.voice_reply_service import QQVoiceReplyService


@pytest.mark.parametrize("failure", ["synthesis", "unconfirmed", "unsupported"])
def test_voice_fallback_expands_cq_modifiers(monkeypatch, failure):
    from unittest.mock import AsyncMock

    client = SimpleNamespace(
        supports_voice=failure != "unsupported",
        send_group_message_segments=AsyncMock(return_value="mid"),
        send_group_record=AsyncMock(return_value=None),
    )
    service = QQVoiceReplyService(
        SimpleNamespace(
            qq_client=client,
            _validate_outbound_message=lambda text: text,
            _get_reply_mode=lambda: "voice",
            logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        )
    )
    synth = AsyncMock(return_value=("file:///voice.wav", "audio/wav"))
    if failure == "synthesis":
        synth.side_effect = RuntimeError("TTS failed")
    monkeypatch.setattr(service, "synthesize_reply_voice_file", synth)
    assert asyncio.run(
        service.deliver_group_reply(
            "123",
            "[CQ:reply,id=456][CQ:at,qq=789]你好[CQ:face,id=14]",
            fallback_to_text_on_voice_failure=True,
        )
    )
    assert client.send_group_message_segments.call_args.args[1] == [
        {"type": "reply", "data": {"id": "456"}},
        {"type": "at", "data": {"qq": "789"}},
        {"type": "text", "data": {"text": "你好"}},
        {"type": "face", "data": {"id": "14"}},
    ]
    if failure == "unsupported":
        synth.assert_not_called()
    else:
        synth.assert_awaited_once_with("你好")
    if failure == "unconfirmed":
        client.send_group_record.assert_awaited_once_with("123", "file:///voice.wav")


class _Client:
    def __init__(self):
        self.sent_text: list[tuple[str, str]] = []

    async def send_message(self, target_id, text, **kw):
        self.sent_text.append((str(target_id), str(text)))
        return "mid"

    async def send_group_message(self, group_id, text, **kw):
        self.sent_text.append((str(group_id), str(text)))
        return "mid"


def _node(client, synth):
    logs: list[tuple[str, str, dict]] = []

    def _record(level):
        def _log(message, *args, **kwargs):
            logs.append((level, str(message), kwargs))

        return _log

    plugin = SimpleNamespace(
        qq_client=client,
        _get_reply_mode=lambda: "both",
        voice_reply_service=SimpleNamespace(synthesize_reply_voice_file=synth),
        logger=SimpleNamespace(info=_record("info"), warning=_record("warning")),
        _emit_log=lambda *a, **k: None,
    )
    return QQReplyDeliveryNode(plugin), logs


def _voice_plan() -> QQDeliveryPlan:
    return QQDeliveryPlan(
        target_type="private",
        target_id="820040531",
        blocks=[QQMessageBlock(record="用语音说这句话")],
        fallback_to_text_on_voice_failure=True,
    )


def test_a_config_failure_logs_one_line_and_still_sends_the_text():
    client = _Client()

    async def _synth(_text):
        raise RuntimeError("本体语音合成失败（HTTP 503）")

    node, logs = _node(client, _synth)

    delivered = asyncio.run(node._send_record(_voice_plan(), QQMessageBlock(record="用语音说这句话")))

    assert delivered is True, "回退的文字发出去并确认了，就该算已投递"
    assert client.sent_text == [("820040531", "用语音说这句话")]
    warning = next(entry for entry in logs if entry[0] == "warning")
    assert "HTTP 503" in warning[1], "原因要进日志，不能只剩一句『语音发送失败』"
    assert "exc_info" not in warning[2], "配置性失败不该打整段 traceback"
    assert any(level == "info" and "改发文字" in text for level, text, _ in logs), (
        "`[Send] … 已发送（语音）` 那个标签不会跟着变，所以要补一句说明实际发的是文字"
    )


def test_an_unexpected_error_still_gets_a_traceback():
    """别把真正的 bug 也降噪掉：非 RuntimeError 照旧带栈。"""
    client = _Client()

    async def _synth(_text):
        raise ValueError("something we did not expect")

    node, logs = _node(client, _synth)

    delivered = asyncio.run(node._send_record(_voice_plan(), QQMessageBlock(record="用语音说这句话")))

    assert delivered is True, "意外异常也走同一条回退（文字照样要发出去）"
    warning = next(entry for entry in logs if entry[0] == "warning")
    assert warning[2].get("exc_info") is True
