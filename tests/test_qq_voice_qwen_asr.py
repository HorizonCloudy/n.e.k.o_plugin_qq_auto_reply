# -*- coding: utf-8 -*-
"""Qwen 转写必须走 **OpenAI 兼容端点 + qwen3-asr-flash**，不是录音文件识别那套。

由来（2026-11-11 真机，使用者「我发了条语音，看看解析情况」）：

部署完双写日志之后，三个 STT 后端的死因终于各自说清楚了，Qwen 那条是：

```
[Voice] Qwen转录失败: 404 code=InvalidParameter msg=Model not exist.
```

两个毛病叠在一起：

* `sensevoice-v1` 在这个账号上直接「模型不存在」；
* 就算模型对，`/api/v1/services/audio/asr/transcription` 是**录音文件识别**，
  它的 `file_urls` 要的是**公网可访问**的地址 —— 我们手里只有内存里的字节，
  塞 `data:` URI 进去本来就不成立（同族的 `paraformer-v2` 更是直接
  `403 AccessDenied: current user api does not support synchronous calls`）。

换成 `compatible-mode/v1/chat/completions` 上的 `qwen3-asr-flash`：它**吃 base64
data URI**、同步返回正文。同一把 key 实测：

```
7862ebb….amr (4667 B)  -> "嗯。"      annotations: zh / neutral
be1b338….amr (28355 B) -> "Shit."    annotations: en / angry
```

这个文件钉住：端点、模型名、请求体形状、以及返回正文的取法。
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import httpx
from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin

#: `#!AMR\n` 魔数 —— 决定 `format=amr`。
AMR_BYTES = b"#!AMR\n" + b"\x00" * 32


class _Cfg:
    def __init__(self, core: dict, slots: dict | None = None) -> None:
        self._core = core
        self._slots = slots or {}

    def get_core_config(self) -> dict:
        return dict(self._core)

    def get_model_api_config(self, slot: str) -> dict:
        return dict(self._slots.get(slot) or {})


def _transcribe(monkeypatch, *, core, slots=None, audio: bytes = AMR_BYTES):
    """跑一遍 `_transcribe_voice`，返回 (结果文本, 捕获到的请求, 文件日志行)。"""
    captured: dict = {}
    file_lines: list[tuple[str, str]] = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "妈我放学回来了"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["headers"] = kwargs.get("headers")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    import utils.config_manager as config_manager_module

    monkeypatch.setattr(config_manager_module, "get_config_manager", lambda: _Cfg(core, slots))

    plugin = SimpleNamespace(
        _qq_settings={},
        logger=None,
        _emit_log=lambda level, msg: None,
        _voice_log=lambda level, msg: file_lines.append((level, str(msg))),
    )
    text = asyncio.run(
        QQAutoReplyPlugin._transcribe_voice(
            plugin, base64.b64encode(audio).decode(),
        )
    )
    return text, captured, file_lines


def test_qwen_uses_the_compatible_mode_audio_understanding_endpoint(monkeypatch):
    text, captured, _ = _transcribe(monkeypatch, core={"ASSIST_API_KEY_QWEN": "sk-test"})

    assert text == "妈我放学回来了"
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", (
        "又打回录音文件识别那套了 —— 那条路要公网 URL，内存里的字节进不去"
    )
    assert "services/audio/asr/transcription" not in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_qwen_payload_carries_a_base64_data_uri_and_amr_format(monkeypatch):
    _, captured, _ = _transcribe(monkeypatch, core={"ASSIST_API_KEY_QWEN": "sk-test"})

    body = captured["json"]
    assert body["model"] == "qwen3-asr-flash", "sensevoice-v1 在这个账号上是 Model not exist"
    content = body["messages"][0]["content"][0]
    assert content["type"] == "input_audio"
    assert content["input_audio"]["format"] == "amr"
    data = content["input_audio"]["data"]
    assert data.startswith("data:audio/amr;base64,"), data[:40]
    assert base64.b64decode(data.split(",", 1)[1]) == AMR_BYTES, "音频字节在编码过程中被改了"


def test_successful_transcription_leaves_a_file_log_line(monkeypatch):
    _, _, file_lines = _transcribe(monkeypatch, core={"ASSIST_API_KEY_QWEN": "sk-test"})

    assert any("Qwen转录完成" in msg for _, msg in file_lines), file_lines


def test_no_key_means_no_request(monkeypatch):
    text, captured, file_lines = _transcribe(monkeypatch, core={})

    assert text == ""
    assert captured == {}, "没有 key 还发了请求"
    assert any("所有STT后端均未产出结果" in msg for _, msg in file_lines), file_lines
