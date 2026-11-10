"""Offline worker/routing contracts; no provider request or QQ send is made."""
import asyncio
import io
import wave
from threading import Event
from unittest.mock import AsyncMock

import pytest
from main_logic import tts_client
from main_logic.core import tts_runtime
from plugin.plugins.qq_auto_reply import voice_synthesis_runtime as speech
from utils import config_manager


class Config:
    def __init__(self, voice="gsv:voice"):
        self.voice = voice
        self.core = {"CORE_API_TYPE": "qwen", "GPTSOVITS_ENABLED": True,
                     "ENABLE_CUSTOM_API": True}

    async def aget_character_data(self):
        return "master", "cat", {}, {"cat": {"voice_id": self.voice}}, {}, {}, {}, {}, {}

    def get_core_config(self):
        return self.core

    def get_model_api_config(self, kind):
        if kind == "realtime":
            return {"api_type": "qwen", "base_url": ""}
        return {"api_key": "slot-key", "is_custom": True, "base_url": "ws://localhost/tts"}

    def voice_id_exists_in_any_storage(self, voice):
        return False

    def load_json_config(self, name, default):
        return self.core

    def get_cosyvoice_clone_runtime(self, provider):
        return {"api_key": "cosy-key"}


@pytest.fixture
def config(monkeypatch):
    cm = Config()
    monkeypatch.setattr(config_manager, "get_config_manager", lambda: cm)
    monkeypatch.setattr(tts_client, "get_config_manager", lambda: cm)
    return cm


@pytest.fixture
def converter(monkeypatch):
    async def convert(chunks):
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            wav.writeframes(b"".join(chunks))
        return out.getvalue()
    monkeypatch.setattr(speech, "_convert_audio", convert)


def worker(calls, *, done=True, raw=False, payload=b"\x00\x00" * 480):
    def run(requests, responses, key, voice):
        calls.append((key, voice))
        responses.put(("__ready__", True))
        while True:
            sid, text = requests.get()
            if sid == speech.TTS_SHUTDOWN_SENTINEL:
                return
            if sid is not None:
                responses.put(payload if raw else ("__audio__", sid, payload))
                if done:
                    responses.put((speech.TTS_AUDIO_DONE_SENTINEL, sid))
    return run


def install_worker(monkeypatch, fn, provider="gptsovits", key=None):
    monkeypatch.setattr(tts_runtime._core_facade, "get_tts_worker", lambda **kw: (fn, key, provider))


@pytest.mark.asyncio
@pytest.mark.parametrize("voice,expected", [
    ("gsv:voice", "gsv:voice"), ("plain", "plain"), ("", ""),
    ({"source": "local", "provider": "gptsovits", "ref": "speaker"}, "gsv:speaker"),
])
async def test_real_gsv_dispatch(monkeypatch, config, converter, voice, expected):
    from main_logic.tts_client.workers import gptsovits
    config.voice = voice
    calls = []
    monkeypatch.setattr(gptsovits, "gptsovits_tts_worker", worker(calls))
    runtime = speech.PluginSpeechRuntime()
    data = await runtime.synthesize("hello")
    assert calls == [("slot-key", expected)]
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getnframes() == 480
    assert not runtime._thread.is_alive()
    assert not runtime._busy


@pytest.mark.asyncio
async def test_real_cosyvoice_dispatch(monkeypatch, config, converter):
    from main_logic.tts_client.workers import cosyvoice
    config.core["GPTSOVITS_ENABLED"] = False
    config.voice = "enrolled-voice"
    monkeypatch.setattr(tts_client, "_get_voice_meta", lambda voice: {"provider": "cosyvoice"})
    calls = []
    monkeypatch.setattr(cosyvoice, "cosyvoice_vc_tts_worker", worker(calls))
    await speech.PluginSpeechRuntime().synthesize("hello")
    assert calls == [("cosy-key", "enrolled-voice")]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["custom", "vllm_omni"])
async def test_real_configured_api_dispatch(monkeypatch, config, converter, provider):
    from main_logic.tts_client.workers import openai, vllm_omni
    config.voice = "api-voice"
    config.core.update({"GPTSOVITS_ENABLED": False, "ttsModelProvider": provider,
                        "ttsModelUrl": "https://tts.example.test/v1",
                        "ttsModelId": "model", "ttsVoiceId": "api-voice",
                        "ttsModelApiKey": "api-key"})
    calls = []
    base_worker = worker(calls)
    def configured_worker(requests, responses, key, voice_id, **kwargs):
        return base_worker(requests, responses, key, voice_id)
    monkeypatch.setattr(tts_client, "_get_voice_meta", lambda voice: None)
    monkeypatch.setattr(openai, "openai_tts_worker", configured_worker)
    monkeypatch.setattr(vllm_omni, "vllm_omni_tts_worker", configured_worker)
    await speech.PluginSpeechRuntime().synthesize("hello")
    assert calls == [("api-key", "api-voice")]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,key,expected", [
    ("qwen", "", ""), ("vllm_omni", None, ""),
    ("gptsovits", None, "slot-key"), ("cosyvoice", "voice-key", "voice-key"),
    ("minimax", "voice-key", "voice-key"), ("elevenlabs", "voice-key", "voice-key"),
    ("gemini", "voice-key", "voice-key"), ("custom", "api-key", "api-key"),
    ("future_provider", "future-key", "future-key"),
])
async def test_common_worker_protocol_and_host_key_policy(monkeypatch, config, converter, provider, key, expected):
    calls = []
    install_worker(monkeypatch, worker(calls, raw=True), provider, key)
    runtime = speech.PluginSpeechRuntime()
    assert (await runtime.synthesize("hello"))[:4] == b"RIFF"
    assert calls == [(expected, "gsv:voice")]


@pytest.mark.asyncio
async def test_disabled_and_unsupported_do_not_start(monkeypatch, config):
    monkeypatch.setattr(speech, "Thread", lambda **kw: pytest.fail("must not start"))
    config.core["DISABLE_TTS"] = True
    with pytest.raises(speech.PluginSpeechError, match="tts_disabled"):
        await speech.PluginSpeechRuntime().synthesize("hello")
    config.core["DISABLE_TTS"] = False
    install_worker(monkeypatch, tts_client.dummy_tts_worker, None)
    with pytest.raises(speech.PluginSpeechError, match="completion_unavailable"):
        await speech.PluginSpeechRuntime().synthesize("hello")


@pytest.mark.asyncio
async def test_incomplete_audio_times_out_and_releases_worker(monkeypatch, config):
    install_worker(monkeypatch, worker([], done=False))
    monkeypatch.setattr(speech, "SYNTHESIS_TIMEOUT_SECONDS", .08)
    monkeypatch.setattr(speech, "_convert_audio", AsyncMock(side_effect=AssertionError("partial audio")))
    runtime = speech.PluginSpeechRuntime()
    with pytest.raises(speech.PluginSpeechError, match="timeout"):
        await runtime.synthesize("hello")
    assert not runtime._busy
    assert not runtime._thread.is_alive()


@pytest.mark.asyncio
async def test_busy_cancellation_and_followup(monkeypatch, config, converter):
    calls = []
    install_worker(monkeypatch, worker(calls, done=False))
    runtime = speech.PluginSpeechRuntime()
    pending = asyncio.create_task(runtime.synthesize("hello"))
    while not calls:
        await asyncio.sleep(.005)
    with pytest.raises(speech.PluginSpeechError, match="busy"):
        await runtime.synthesize("second")
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not runtime._thread.is_alive()
    assert not runtime._busy
    install_worker(monkeypatch, worker([]))
    assert (await runtime.synthesize("next"))[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_unresponsive_retired_worker_blocks_followup(monkeypatch, config):
    release = Event()
    def stuck(requests, responses, key, voice):
        release.wait(5)
    install_worker(monkeypatch, stuck)
    monkeypatch.setattr(speech, "READY_TIMEOUT_SECONDS", .03)
    monkeypatch.setattr(speech, "WORKER_JOIN_SECONDS", .01)
    runtime = speech.PluginSpeechRuntime()
    try:
        with pytest.raises(speech.PluginSpeechError, match="tts_ready_timeout"):
            await runtime.synthesize("hello")
        with pytest.raises(speech.PluginSpeechError, match="worker_cleanup_pending"):
            await runtime.synthesize("next")
    finally:
        release.set()
        await asyncio.to_thread(runtime._thread.join, 1)


@pytest.mark.asyncio
async def test_audio_limit_and_provider_errors_are_sanitized(monkeypatch, config):
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 100)
    install_worker(monkeypatch, worker([], payload=b"x" * 200))
    with pytest.raises(speech.PluginSpeechError, match="audio_too_large"):
        await speech.PluginSpeechRuntime().synthesize("hello")
    def failed(requests, responses, key, voice):
        responses.put(("__error__", {"api_key": "secret"}))
    install_worker(monkeypatch, failed)
    with pytest.raises(speech.PluginSpeechError) as exc:
        await speech.PluginSpeechRuntime().synthesize("hello")
    assert str(exc.value) == "tts_unavailable"


def test_output_queue_never_blocks_and_closes():
    q = speech._AudioQueue()
    for _ in range(300):
        q.put(("__ready__", True))
    assert q.failed.is_set()
    assert q.qsize() == 256
    q = speech._AudioQueue()
    q.closed.set()
    q.put(b"ignored")
    assert q.empty()


@pytest.mark.asyncio
async def test_wrong_speech_id_cannot_complete(monkeypatch, config):
    def wrong(requests, responses, key, voice):
        responses.put(("__ready__", True))
        sid, _ = requests.get()
        responses.put(("__audio__", "other", b"\x00\x00"))
        responses.put((speech.TTS_AUDIO_DONE_SENTINEL, "other"))
    install_worker(monkeypatch, wrong)
    with pytest.raises(speech.PluginSpeechError, match="tts_incomplete"):
        await speech.PluginSpeechRuntime().synthesize("hello")


@pytest.mark.asyncio
async def test_worker_exception_is_sanitized(monkeypatch, config, capsys):
    def failed(*args):
        raise RuntimeError("private-api-key")
    install_worker(monkeypatch, failed)
    with pytest.raises(speech.PluginSpeechError, match="tts_unavailable"):
        await speech.PluginSpeechRuntime().synthesize("hello")
    captured = capsys.readouterr()
    assert "private-api-key" not in captured.out + captured.err


@pytest.mark.asyncio
async def test_real_host_pcm_converter_above_game_cache_limit():
    payload = b"\x00\x00" * (1024 * 1024 + 480)
    data = await speech._convert_audio([payload])
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getframerate() == 48000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.readframes(wav.getnframes()) == payload


@pytest.mark.asyncio
async def test_missing_http_to_real_bridge_to_private_record(monkeypatch, config, tmp_path):
    from types import SimpleNamespace

    import httpx
    from plugin.plugins.qq_auto_reply import voice_reply_service as vrs

    calls = []
    install_worker(monkeypatch, worker(calls))
    plugin = SimpleNamespace(
        data_path=lambda: tmp_path,
        logger=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
        qq_client=SimpleNamespace(supports_voice=True, send_private_record=AsyncMock(return_value="mid")),
        _get_reply_mode=lambda: "voice",
        _validate_outbound_message=lambda text: text,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
        monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
        service = vrs.QQVoiceReplyService(plugin)
        assert await service.deliver_private_reply("123", "hello", fallback_to_text_on_voice_failure=False)
    plugin.qq_client.send_private_record.assert_awaited_once()
    assert plugin.qq_client.send_private_record.call_args.args[1].startswith("file://")
    files = list((tmp_path / "voice_cache").glob("*.wav"))
    assert len(files) == 1
    with wave.open(str(files[0])) as wav:
        assert wav.getnframes() == 480
    assert len(calls) == 1
