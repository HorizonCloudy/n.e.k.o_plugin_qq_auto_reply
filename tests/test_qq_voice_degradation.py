# -*- coding: utf-8 -*-
"""语音发不出去时：**别被角色的 voice_id 挡住本地那条路，失败原因要说人话**。

真机背景（2026-09-29，使用者报告）：

> 「主要是 gsv 环境，会出现这个报错，但是接入百炼就好了」

那条报错是 `当前猫娘未配置 voice_id，无法发送语音`。根因不是"配置错了"，而是
``synthesize_reply_voice_file`` 在**入口**就要求角色必须有 voice_id —— 它挡住的是排在最前面
的**本地那条路**：``_synthesize_local_tts``（自建 SoVITS/CosyVoice，音色取自
``tts_custom.voice_name``，缺 voice_id 时用 ``"default"``，本来跑得通）。于是 GSV/自建 TTS 的
用户永远发不出语音；换百炼能好，只是因为百炼正好在插件的合成链上、而且选音色时把 voice_id
写上了。

要求现在留在真正需要它的地方：云端各分支自己有守卫（DashScope 那条见下）。本文件钉四件事：

1. 没有 voice_id 时，本地那条**照样能合成**（回归守卫 —— 这条以前是红的）；
2. 没有 voice_id 且本地不可用时，使用当前线路默认音色，而不是服务名或空音色；
3. 撞到宿主的 ``gsv:`` 音色要**当场说清插件没有这一路**，而不是拿它去问百炼；
4. 配置性失败只打**一句人话**（不再整段 traceback），且回退文字时补一句说明；
   真正的意外（非 RuntimeError）照旧带栈，不许把 bug 也降噪掉。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import utils.config_manager as host_config_manager
from plugin.plugins.qq_auto_reply import voice_reply_service as vrs
from plugin.plugins.qq_auto_reply.pipeline_models import QQDeliveryPlan, QQMessageBlock
from plugin.plugins.qq_auto_reply.reply_delivery_node import QQReplyDeliveryNode
from plugin.plugins.qq_auto_reply.voice_reply_service import QQVoiceReplyService

WAV_BYTES = b"RIFF....WAVEfake-audio"


def _mock_free_tts(monkeypatch):
    import base64
    import io
    import json
    import wave

    from main_logic.tts_client.workers import free

    monkeypatch.setattr(free, "_adjust_free_tts_url", lambda url: url)
    wav = io.BytesIO()
    with wave.open(wav, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x00\x00" * 24)
    events = iter([
        {"type": "tts.connection.done", "data": {"session_id": "test"}},
        {"type": "tts.response.audio.delta", "data": {"audio": base64.b64encode(wav.getvalue()).decode()}},
        {"type": "tts.response.done"},
    ])
    sent = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def recv(self):
            return json.dumps(next(events))

        async def send(self, message):
            sent.append(json.loads(message))

    def connect(url, **kwargs):
        assert url == "wss://www.lanlan.tech/tts"
        return Socket()

    monkeypatch.setattr(vrs.websockets, "connect", connect)
    return wav.getvalue(), sent


def test_legacy_free_voice_missing_from_ui_catalog_uses_free_tts(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, voice_id="qingchunshaonv")
    monkeypatch.setattr(vrs, "get_free_voices", lambda: {"new": "new-voice"})
    wav, sent = _mock_free_tts(monkeypatch)
    audio, mime = asyncio.run(service.synthesize_reply_voice_audio("你好"))
    assert audio == wav
    assert mime == "audio/wav"
    assert sent[0]["data"]["voice_id"] == "qingchunshaonv"


@pytest.mark.parametrize("failure", ["synthesis", "unconfirmed", "unsupported"])
def test_voice_fallback_expands_cq_modifiers(monkeypatch, failure):
    from unittest.mock import AsyncMock

    client = SimpleNamespace(
        supports_voice=failure != "unsupported",
        send_group_message_segments=AsyncMock(return_value="mid"),
        send_group_record=AsyncMock(return_value=None),
    )
    service = QQVoiceReplyService(SimpleNamespace(
        qq_client=client,
        _validate_outbound_message=lambda text: text,
        _get_reply_mode=lambda: "voice",
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    ))
    synth = AsyncMock(return_value=("file:///voice.wav", "audio/wav"))
    if failure == "synthesis":
        synth.side_effect = RuntimeError("TTS failed")
    monkeypatch.setattr(service, "synthesize_reply_voice_file", synth)
    assert asyncio.run(service.deliver_group_reply(
        "123", "[CQ:reply,id=456][CQ:at,qq=789]你好[CQ:face,id=14]",
        fallback_to_text_on_voice_failure=True,
    ))
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


class _FakeConfigManager:
    """只提供合成链在"走到守卫之前"会碰到的那些方法。"""

    def __init__(self, *, api_key: str = "key", voices=None, tts_custom=None):
        self._api_key = api_key
        self._voices = voices or {}
        self._tts_custom = tts_custom or {}

    async def aensure_region_resolved(self):
        self.region_resolved = True
        return True

    def get_voices_for_current_api(self):
        return self._voices

    def get_model_api_config(self, name):
        cfg = {"api_key": self._api_key, "base_url": ""}
        if name == "tts_custom":
            cfg.update(self._tts_custom)
        return cfg

    async def aget_core_config(self):
        return {}

    def get_cosyvoice_clone_runtime(self, provider):
        return {}

    def get_tts_api_key(self, provider):
        return ""


def _service(tmp_path, monkeypatch, *, voice_id: str, local_result=None):
    """最小可跑的 QQVoiceReplyService：把宿主 config_manager 与本地那条 TTS 都换掉。"""
    monkeypatch.setattr(vrs, "get_active_realtime_native_provider_for_ui", None)
    fake_cm = _FakeConfigManager()
    monkeypatch.setattr(host_config_manager, "get_config_manager", lambda: fake_cm)

    service = QQVoiceReplyService(SimpleNamespace(
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        data_path=lambda: tmp_path,
    ))

    async def _local(_text):
        return local_result

    async def _vid():
        return voice_id

    monkeypatch.setattr(service, "_synthesize_local_tts", _local)
    monkeypatch.setattr(service, "get_current_voice_id", _vid)
    return service


# ── 1 & 2：入口那道闸 ─────────────────────────────────────────────────

def test_the_local_line_works_without_a_character_voice(tmp_path, monkeypatch):
    """GSV/自建 TTS：角色没有 voice_id 也该能合成 —— 以前这里直接抛。"""
    service = _service(tmp_path, monkeypatch, voice_id="", local_result=(WAV_BYTES, "audio/wav"))

    def unexpected_default():
        pytest.fail("本地合成成功时不应读取云端默认音色")

    monkeypatch.setattr(vrs, "get_new_character_default_voice_id", unexpected_default)
    uri, mime = asyncio.run(service.synthesize_reply_voice_file("你好呀"))

    assert mime == "audio/wav"
    assert uri.startswith("file://")
    assert (tmp_path / "voice_cache").is_dir(), "音频文件要落进 voice_cache"


@pytest.mark.parametrize("voice_id", ["", "  ", None])
@pytest.mark.parametrize("native_provider", [None, "free", "free_intl"])
@pytest.mark.parametrize("catalog", [
    {"other": "other-voice", "cuteGirl": "region-default"},
    {"first": "first-voice"},
    {},
])
def test_missing_voice_uses_host_default(tmp_path, monkeypatch, voice_id, native_provider, catalog):
    from utils import api_config_loader

    service = _service(tmp_path, monkeypatch, voice_id=voice_id)
    cm = host_config_manager.get_config_manager()
    monkeypatch.setattr(vrs, "get_active_realtime_native_provider_for_ui", lambda cm: native_provider)

    def free_voices():
        assert cm.region_resolved, "必须先完成区域判定，再选择默认音色"
        return catalog

    monkeypatch.setattr(api_config_loader, "get_free_voices", free_voices)
    monkeypatch.setattr(vrs, "get_free_voices", free_voices)
    wav, sent = _mock_free_tts(monkeypatch)
    uri, mime = asyncio.run(service.synthesize_reply_voice_file("你好呀"))

    assert uri.startswith("file://")
    assert mime == "audio/wav"
    assert next((tmp_path / "voice_cache").glob("*.wav")).read_bytes() == wav
    assert sent[0]["data"]["voice_id"] == api_config_loader.get_new_character_default_voice_id()
    assert sent[0]["data"]["voice_id"] not in ("free", "free_intl", "")


@pytest.mark.parametrize("catalog", [{"cuteGirl": "legacy-default"}, {}])
def test_missing_voice_on_older_host_uses_free_default(tmp_path, monkeypatch, catalog):
    from utils.tts.providers.stepfun import STEPFUN_TTS_DEFAULT_VOICE

    service = _service(tmp_path, monkeypatch, voice_id="")
    monkeypatch.setattr(vrs, "get_new_character_default_voice_id", None)
    monkeypatch.setattr(vrs, "get_free_voices", lambda: catalog)
    wav, sent = _mock_free_tts(monkeypatch)
    assert asyncio.run(service.synthesize_reply_voice_audio("你好")) == (wav, "audio/wav")
    assert sent[0]["data"]["voice_id"] == catalog.get("cuteGirl", STEPFUN_TTS_DEFAULT_VOICE)


def test_gemini_missing_voice_uses_native_default_with_core_key(tmp_path, monkeypatch):
    import sys
    from unittest.mock import AsyncMock

    service = _service(tmp_path, monkeypatch, voice_id="")
    cm = host_config_manager.get_config_manager()
    cm._api_key = ""  # Gemini 使用 CORE_API_KEY，不要求免费线路的 AUDIO_API_KEY。
    monkeypatch.setattr(cm, "aget_core_config", AsyncMock(return_value={"CORE_API_KEY": "native-key"}))
    monkeypatch.setattr(vrs, "get_active_realtime_native_provider_for_ui", lambda cm: "gemini")
    synth = AsyncMock(return_value=WAV_BYTES)
    monkeypatch.setitem(sys.modules, "main_routers.characters_router", SimpleNamespace(
        _synthesize_gemini_native_voice_preview=synth,
    ))
    expected_voice, _ = vrs.normalize_gemini_tts_voice("")
    assert asyncio.run(service.synthesize_reply_voice_audio("你好")) == (WAV_BYTES, "audio/wav")
    synth.assert_awaited_once_with(
        voice_id=expected_voice, preview_line="你好", audio_api_key="native-key",
    )


def test_default_voice_still_requires_api_key(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, voice_id="")
    host_config_manager.get_config_manager()._api_key = ""
    monkeypatch.setattr(vrs, "get_new_character_default_voice_id", lambda: "default-voice")
    with pytest.raises(RuntimeError, match="TTS_AUDIO_API_KEY_MISSING"):
        asyncio.run(service.synthesize_reply_voice_audio("你好"))


def test_a_gsv_voice_says_the_plugin_has_no_such_line(tmp_path, monkeypatch):
    """宿主的 ``gsv:`` 音色：插件这条链上没有它，别拿它去问百炼。"""
    service = _service(tmp_path, monkeypatch, voice_id="gsv:my_voice", local_result=None)

    with pytest.raises(RuntimeError) as err:
        asyncio.run(service.synthesize_reply_voice_file("你好呀"))

    message = str(err.value)
    assert "GPT-SoVITS" in message and vrs.GSV_VOICE_PREFIX in message
    assert "百炼" in message, "要给出可行的替代（换插件支持的音色）"


# ── 4：投递这一层的日志 ───────────────────────────────────────────────

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
        raise RuntimeError("当前猫娘未配置 voice_id，而这条 TTS 线路需要音色")

    node, logs = _node(client, _synth)

    delivered = asyncio.run(node._send_record(_voice_plan(), QQMessageBlock(record="用语音说这句话")))

    assert delivered is True, "回退的文字发出去并确认了，就该算已投递"
    assert client.sent_text == [("820040531", "用语音说这句话")]
    warning = next(entry for entry in logs if entry[0] == "warning")
    assert "voice_id" in warning[1], "原因要进日志，不能只剩一句『语音发送失败』"
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
