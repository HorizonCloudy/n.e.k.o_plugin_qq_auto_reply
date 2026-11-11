# -*- coding: utf-8 -*-
"""语音消息转不出字时，正文里**只剩 `[语音]` 标记**，不再漏整条 CQ 码。

由来（使用者 2026-11-11：「检查日志，stt 好像没有正常起效」）：

真机 backlog 里每一条语音的正文都是

```
[语音] [CQ:record,file=6dcb3055….amr,path=D:\\qq\\temp\\Tencent Files\\…\\Ori\\6dcb3055….amr,
        url=https://multimedia.nt.qq.com.cn/download?appid=1403&fileid=…&rkey=CAESqAE…&format=amr]
```

也就是说：**标记后面紧跟 CQ 码 = 一个字都没转出来**（转写成功的形态是
``[语音] <正文> …``）。而那句 CQ 码里装的是 NapCat 的本机临时路径加一条**带
rkey 的签名直链**，对模型没有任何意义，却把一条语音消息的正文顶到 600+ 字符
（真机 ``发送消息到 AI (会话: private:820040531, length: 604)``）。

这个文件钉住：

1. ``_drop_record_cq`` 只剥 record 段，人话一个字不动；
2. 转写失败 / 未绑定 → 正文收敛成 ``[语音]``，且**文件日志留一条 WARNING**；
3. 转写成功 → ``[语音] <正文>``，record 段同样剥掉；
4. ``[语音]`` 不算"一句可复读的文本"（否则群友轮流发语音时她会把标记发出去）。
"""

from __future__ import annotations

import asyncio

from plugin.plugins.qq_auto_reply.enrichment import QQMessageEnricher
from plugin.plugins.qq_auto_reply.repeat_echo_service import QQRepeatEchoService

RECORD_CQ = (
    "[CQ:record,file=v.amr,"
    "path=D:\\qq\\temp\\Tencent Files\\3281414178\\nt_qq\\nt_data\\Ptt\\2026-11\\Ori\\v.amr,"
    "url=https://multimedia.nt.qq.com.cn/download?appid=1403&fileid=ABC"
    "&rkey=SECRETRKEY&format=amr&file_size=3023]"
)


class _Client:
    """只提供 ``get_record`` 的连接器桩（不碰网络）。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def get_record(self, file_id: str, output_format: str = "mp3") -> dict:
        return self._payload


def _message(cq: str = RECORD_CQ, text: str = "") -> dict:
    content = f"{text}{cq}".strip()
    return {
        "message_type": "private",
        "user_id": "820040531",
        "raw_message": content,
        "content": content,
    }


def _run(tmp_path, *, transcriber, cq=RECORD_CQ, text="", payload_extra=None, url=""):
    """跑一遍 ``_fetch_record_content``，返回 (message, 文件日志行)。"""
    audio = tmp_path / "v.amr"
    audio.write_bytes(b"#!AMR\n\x00\x00\x00fake-audio")
    payload = {"data": {"url": url, "file": str(audio)}}
    if payload_extra is not None:
        payload = payload_extra

    file_lines: list[tuple[str, str]] = []
    panel_lines: list[tuple[str, str]] = []

    class _Logger:
        def debug(self, msg):
            file_lines.append(("DEBUG", str(msg)))

        def info(self, msg):
            file_lines.append(("INFO", str(msg)))

        def warning(self, msg):
            file_lines.append(("WARN", str(msg)))

        def error(self, msg):
            file_lines.append(("ERROR", str(msg)))

        def exception(self, msg):        # pragma: no cover - 只在真出错时走
            file_lines.append(("WARN", str(msg)))

    enricher = QQMessageEnricher(
        _Client(payload),
        voice_transcriber=transcriber,
        logger=_Logger(),
        emit_log=lambda level, msg: panel_lines.append((level, str(msg))),
    )
    message = _message(cq=cq, text=text)
    asyncio.run(enricher._fetch_record_content(message, ["v.amr"]))
    return message, file_lines, panel_lines


async def _no_text(*_a, **_k) -> str:
    return ""


# ── 1. 只剥 record 段，人话不动 ─────────────────────────────────────

def test_drop_record_cq_removes_only_the_record_segment():
    assert QQMessageEnricher._drop_record_cq(RECORD_CQ) == ""
    assert QQMessageEnricher._drop_record_cq("听这个 " + RECORD_CQ) == "听这个"
    assert QQMessageEnricher._drop_record_cq("") == ""
    # 其它段与普通文字都不该被动到
    assert QQMessageEnricher._drop_record_cq("[CQ:at,qq=1] 你好") == "[CQ:at,qq=1] 你好"


# ── 2. 转写失败：正文收敛 + 留痕 ────────────────────────────────────

def test_failed_transcription_leaves_only_the_marker(tmp_path):
    message, file_lines, _ = _run(tmp_path, transcriber=_no_text)

    assert message["content"] == "[语音]", message["content"]
    assert message["raw_message"] == "[语音]"
    assert "CQ:record" not in message["content"]
    assert "rkey" not in message["content"], "签名直链漏进正文了"
    assert "D:\\qq\\temp" not in message["content"], "本机临时路径漏进正文了"

    warnings = [msg for level, msg in file_lines if level == "WARN"]
    assert any("语音未转写" in msg for msg in warnings), (
        "转写没出字却没在文件日志留痕 —— 又一次只能靠正文变短去猜：\n"
        + "\n".join(f"{lvl}: {msg}" for lvl, msg in file_lines)
    )


def test_unbound_transcriber_also_leaves_only_the_marker(tmp_path):
    message, file_lines, _ = _run(tmp_path, transcriber=None)

    assert message["content"] == "[语音]"
    warnings = [msg for level, msg in file_lines if level == "WARN"]
    assert any("transcriber=未绑定" in msg for msg in warnings), warnings


def test_failed_transcription_keeps_the_authored_text(tmp_path):
    """语音附带文字时，那段文字必须留着（她还在说别的话）。"""
    message, _, _ = _run(tmp_path, transcriber=_no_text, text="听这个 ")

    assert message["content"] == "[语音] 听这个", message["content"]


def test_missing_audio_says_so_and_still_marks_the_message(tmp_path):
    message, file_lines, _ = _run(
        tmp_path, transcriber=_no_text, payload_extra={"data": {"url": "", "file": ""}},
    )

    assert message["content"] == "[语音]"
    warnings = [msg for level, msg in file_lines if level == "WARN"]
    assert any("无法获取语音数据" in msg for msg in warnings), warnings


def test_a_non_http_url_is_never_handed_to_the_transcriber(tmp_path):
    """NapCat 有时在 `url` 字段里给本机路径。

    真机 10:21 的样子：`url=有` 但传下去只换来
    `UnsupportedProtocol: Request URL is missing an 'http://' or 'https://' protocol`，
    白跑一次、多两条吓人的 WARNING —— 而同一份音频已经由 `file` 路径读进内存了。
    """
    calls: list[dict] = []

    async def _record(audio_base64: str = "", **kwargs) -> str:
        calls.append({"audio_base64": audio_base64, **kwargs})
        return "妈我放学回来了"

    _run(
        tmp_path,
        transcriber=_record,
        url="D:\\qq\\temp\\Tencent Files\\x\\v.amr",
    )

    assert calls, "转录器压根没被调用"
    assert all("audio_url" not in kwargs for kwargs in calls), (
        f"非 http 的 url 被传给了转录器：{calls}"
    )


# ── 3. 转写成功：标记 + 正文，record 段照样剥掉 ─────────────────────

def test_successful_transcription_replaces_the_record_segment(tmp_path):
    async def _ok(*_a, **_k) -> str:
        return "妈我放学回来了"

    message, file_lines, _ = _run(tmp_path, transcriber=_ok, text="听这个 ")

    assert message["content"] == "[语音] 妈我放学回来了 听这个", message["content"]
    assert "CQ:record" not in message["content"]
    infos = [msg for level, msg in file_lines if level == "INFO"]
    assert any("语音转文字完成" in msg for msg in infos), infos


# ── 4. `[语音]` 不是可复读的文本 ────────────────────────────────────

def test_marker_is_not_repeatable_text():
    """群友轮流发语音时，她不能把内部标记当"同一句话"跟着发出去。"""
    assert QQRepeatEchoService.is_repeatable_text("[语音]") is False
    assert QQRepeatEchoService.is_repeatable_text("[语音] 妈我放学回来了") is False
    # 正常的人话不受影响
    assert QQRepeatEchoService.is_repeatable_text("一江大气喵") is True
