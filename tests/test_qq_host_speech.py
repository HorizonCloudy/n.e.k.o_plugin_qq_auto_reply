"""Prefer host HTTP; reuse host workers only when the endpoint is absent."""

import asyncio
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote, urlparse

import httpx
import pytest
from plugin.plugins.qq_auto_reply import voice_reply_service as vrs


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 48)
    return output.getvalue()


def service(tmp_path):
    return vrs.QQVoiceReplyService(
        SimpleNamespace(
            data_path=lambda: tmp_path,
            logger=SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
        )
    )


def test_delegates_text_only_and_saves_host_audio(tmp_path, monkeypatch):
    audio = wav_bytes()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/api/speech/synthesize"
        assert json.loads(request.content) == {"text": "你好 xxy"}
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
            instance = service(tmp_path)
            # Even a missing/gsv/xxy voice is not the plugin's responsibility.
            instance.get_current_voice_id = AsyncMock(side_effect=AssertionError("must not select a voice"))
            uri, mime = await instance.synthesize_reply_voice_file("  你好 xxy  ")
            assert mime == "audio/wav"
            path = unquote(urlparse(uri).path)
            if path.startswith("/") and len(path) > 2 and path[2] == ":":
                path = path[1:]
            assert Path(path).read_bytes() == audio
            instance.get_current_voice_id.assert_not_called()

    asyncio.run(run())
    assert len(calls) == 1
    assert not hasattr(vrs.QQVoiceReplyService, "_synthesize_local_tts")


@pytest.mark.parametrize(
    "status, message",
    [
        (429, "忙"),
        (503, "HTTP 503"),
        (504, "超时"),
        (403, "HTTP 403"),
        (307, "HTTP 307"),
    ],
)
def test_host_errors_do_not_trigger_another_provider(tmp_path, monkeypatch, status, message):
    calls = []
    monkeypatch.setattr(vrs.QQVoiceReplyService, "_synthesize_with_host_workers", AsyncMock(side_effect=AssertionError("must not retry")))

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret-upstream-api-key", headers={"location": "http://example.com"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
            with pytest.raises(RuntimeError, match=message) as exc:
                await service(tmp_path).synthesize_reply_voice_file("你好")
            assert "secret" not in str(exc.value)

    asyncio.run(run())
    assert len(calls) == 1
    assert not (tmp_path / "voice_cache").exists()


@pytest.mark.parametrize("content, mime", [(b"", "audio/wav"), (b"bad", "audio/wav"), (b"{}", "application/json")])
def test_rejects_bad_audio(tmp_path, monkeypatch, content, mime):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=content, headers={"content-type": mime}),
            )
        ) as client:
            monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
            with pytest.raises(RuntimeError, match="WAV"):
                await service(tmp_path).synthesize_reply_voice_file("你好")

    asyncio.run(run())
    assert not (tmp_path / "voice_cache").exists()


def test_bounds_audio_size(tmp_path, monkeypatch):
    monkeypatch.setattr(vrs, "MAX_HOST_AUDIO_BYTES", 16)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=wav_bytes(), headers={"content-type": "audio/wav"}),
            )
        ) as client:
            monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
            with pytest.raises(RuntimeError, match="过大"):
                await service(tmp_path).synthesize_reply_voice_audio("你好")

    asyncio.run(run())


@pytest.mark.parametrize("error, message", [(httpx.ConnectError, "无法连接"), (httpx.ReadTimeout, "超时")])
def test_network_errors_are_readable(tmp_path, monkeypatch, error, message):
    def handler(request):
        raise error("private URL", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
            with pytest.raises(RuntimeError, match=message):
                await service(tmp_path).synthesize_reply_voice_audio("你好")

    asyncio.run(run())


@pytest.mark.parametrize("text", ["", "   ", "字" * 2001])
def test_invalid_text_never_calls_host(tmp_path, monkeypatch, text):
    monkeypatch.setattr(vrs, "get_internal_http_client", lambda: pytest.fail("must not call host"))
    with pytest.raises(RuntimeError):
        asyncio.run(service(tmp_path).synthesize_reply_voice_audio(text))


def test_private_record_uses_host_generated_file(tmp_path, monkeypatch):
    instance = service(tmp_path)
    instance.plugin.qq_client = SimpleNamespace(supports_voice=True, send_private_record=AsyncMock(return_value="mid"))
    instance.plugin._get_reply_mode = lambda: "voice"
    instance.plugin._validate_outbound_message = lambda text: text

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=wav_bytes(), headers={"content-type": "audio/wav"}),
            )
        ) as client:
            monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
            assert await instance.deliver_private_reply("123", "你好", fallback_to_text_on_voice_failure=True)
        args = instance.plugin.qq_client.send_private_record.call_args.args
        assert args[0] == "123"
        assert args[1].startswith("file://")

    asyncio.run(run())


@pytest.mark.parametrize("status", [404, 405])
def test_missing_endpoint_reuses_host_workers(tmp_path, monkeypatch, status):
    async def run():
        instance = service(tmp_path)
        bridge = AsyncMock(return_value=wav_bytes())
        monkeypatch.setattr(instance, "_synthesize_with_host_workers", bridge)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(status, text="missing"),
        )) as client:
            monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
            audio, mime = await instance.synthesize_reply_voice_audio("hello")
        assert audio == wav_bytes()
        assert mime == "audio/wav"
        bridge.assert_awaited_once_with("hello")
    asyncio.run(run())


def test_bridge_errors_are_readable_and_sanitized(tmp_path):
    from plugin.plugins.qq_auto_reply.voice_synthesis_runtime import PluginSpeechError
    instance = service(tmp_path)
    instance._speech_runtime = SimpleNamespace(synthesize=AsyncMock(
        side_effect=PluginSpeechError("secret-upstream-key"),
    ))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(instance._synthesize_with_host_workers("hello"))
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("oversized", [False, True])
def test_bridge_output_is_validated_before_caching(tmp_path, monkeypatch, oversized):
    async def run():
        instance = service(tmp_path)
        payload = wav_bytes() if oversized else b"not audio"
        if oversized:
            monkeypatch.setattr(vrs, "MAX_HOST_AUDIO_BYTES", 16)
        monkeypatch.setattr(instance, "_synthesize_with_host_workers", AsyncMock(return_value=payload))
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(404),
        )) as client:
            monkeypatch.setattr(vrs, "get_internal_http_client", lambda: client)
            with pytest.raises(RuntimeError, match="过大" if oversized else "无效 WAV"):
                await instance.synthesize_reply_voice_file("hello")
        assert not (tmp_path / "voice_cache").exists()
    asyncio.run(run())
