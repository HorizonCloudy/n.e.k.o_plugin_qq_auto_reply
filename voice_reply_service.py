from __future__ import annotations

import asyncio
import io
import random
import re
import time
import wave
from pathlib import Path
from typing import Any

import httpx
from utils.config_manager import get_reserved
from utils.http.internal_client import get_internal_http_client

try:
    from utils.voice_config import read_legacy_voice_id
except (ImportError, ModuleNotFoundError):
    read_legacy_voice_id = None

# Voice selection and provider protocols always come from the host. Older
# packaged hosts lack the HTTP endpoint, so capture their existing workers locally.
MAX_HOST_AUDIO_BYTES = 16 * 1024 * 1024
HOST_SPEECH_TIMEOUT_SECONDS = 140.0


class _HostSpeechEndpointMissing(RuntimeError):
    pass


class QQVoiceReplyService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._speech_runtime = None

    def get_voice_output_dir(self) -> Path:
        return self.plugin.data_path() / "voice_cache"

    async def cleanup_voice_output_dir(self, *, max_age_seconds: int = 1800) -> None:
        voice_dir = self.get_voice_output_dir()
        if not voice_dir.exists():
            return
        cutoff = time.time() - max_age_seconds
        for pattern in ("*.wav", "*.mp3"):
            for path in voice_dir.glob(pattern):
                try:
                    if path.stat().st_mtime < cutoff:
                        await asyncio.to_thread(path.unlink)
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    self.plugin.logger.warning(f"清理过期 QQ 语音缓存失败: {path} err={exc}")

    async def get_current_voice_id(self) -> str:
        """兼容旧调用方的查询接口；语音合成不读取或使用这里的结果。"""
        try:
            from utils.config_manager import get_config_manager
            config_manager = get_config_manager()
            characters = await config_manager.aload_characters()
            if not isinstance(characters, dict):
                return ""
            current_name = str(characters.get("当前猫娘") or "").strip()
            catgirls = characters.get("猫娘") or {}
            current_character = catgirls.get(current_name) if isinstance(catgirls, dict) else None
            if not isinstance(current_character, dict):
                return ""
            return read_legacy_voice_id(get_reserved(current_character, "voice_id", default="", legacy_keys=("voice_id",))) if read_legacy_voice_id else ""
        except Exception as exc:
            self.plugin.logger.warning(f"读取当前猫娘 voice_id 失败: {exc}")
            return ""

    async def synthesize_reply_voice_audio(self, text: str) -> tuple[bytes, str]:
        """Use the host endpoint, or its existing workers on endpoint absence only."""
        from config import MAIN_SERVER_PORT

        text = str(text or "").strip()
        if not text:
            raise RuntimeError("语音合成文本不能为空")
        if len(text) > 2000:
            raise RuntimeError("语音合成文本过长（最多 2000 字符）")
        url = f"http://127.0.0.1:{int(MAIN_SERVER_PORT)}/api/speech/synthesize"
        try:
            async with asyncio.timeout(HOST_SPEECH_TIMEOUT_SECONDS):
                client = get_internal_http_client()
                async with client.stream(
                    "POST", url, json={"text": text},
                    timeout=httpx.Timeout(135.0, connect=5.0), follow_redirects=False,
                ) as response:
                    if response.status_code in {404, 405}:
                        raise _HostSpeechEndpointMissing()
                    if response.status_code == 429:
                        raise RuntimeError("本体语音合成忙，请稍后重试")
                    if response.status_code == 504:
                        raise RuntimeError("本体语音合成超时")
                    if response.status_code != 200:
                        raise RuntimeError(f"本体语音合成失败（HTTP {response.status_code}），请检查本体 TTS 设置及日志")
                    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if mime != "audio/wav":
                        raise RuntimeError("本体语音接口未返回 WAV 音频")
                    audio = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(audio) + len(chunk) > MAX_HOST_AUDIO_BYTES:
                            raise RuntimeError("本体返回的语音文件过大")
                        audio.extend(chunk)
        except _HostSpeechEndpointMissing:
            audio = await self._synthesize_with_host_workers(text)
        except (TimeoutError, httpx.TimeoutException):
            raise RuntimeError("请求本体语音合成超时") from None
        except httpx.HTTPError:
            raise RuntimeError("无法连接本体语音接口，请确认本体已启动") from None
        # Reject empty/malformed responses before creating a QQ record file.
        if len(audio) > MAX_HOST_AUDIO_BYTES:
            raise RuntimeError("本体返回的语音文件过大")
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                if wav.getnframes() <= 0 or not wav.readframes(1):
                    raise ValueError("empty WAV")
        except (wave.Error, EOFError, ValueError):
            raise RuntimeError("本体语音接口返回了空音频或无效 WAV") from None
        return bytes(audio), "audio/wav"

    async def _synthesize_with_host_workers(self, text: str) -> bytes:
        """Compatibility bridge, not an alternate provider or a desktop session."""
        try:
            from .voice_synthesis_runtime import PluginSpeechError, PluginSpeechRuntime
        except ImportError:
            raise RuntimeError("当前本体缺少可复用的 TTS 组件，无法在插件侧合成语音") from None
        if self._speech_runtime is None:
            self._speech_runtime = PluginSpeechRuntime()
        self.plugin.logger.info("[QQ Voice] 本体无语音 HTTP 接口，复用已安装本体的 TTS 调度合成")
        try:
            return await self._speech_runtime.synthesize(text)
        except PluginSpeechError as exc:
            code = str(exc)
            messages = {
                "busy": "语音合成忙，请稍后重试",
                "worker_cleanup_pending": "上一条语音仍在清理，请稍后重试",
                "tts_disabled": "本体配置已禁用 TTS",
                "completion_unavailable": "当前 TTS 服务不支持独立生成完整语音",
                "character_unavailable": "无法读取当前角色的音色配置",
                "timeout": "语音合成超时",
                "tts_ready_timeout": "语音服务启动超时",
                "audio_too_large": "语音文件过大",
                "output_overflow": "语音输出超过缓冲上限",
                "empty_after_normalization": "没有可朗读的语音文本",
                "tts_incomplete": "语音服务未返回完整音频",
                "audio_unavailable": "语音服务未返回音频",
            }
            public_code = code if code in messages else "tts_unavailable"
            self.plugin.logger.warning("[QQ Voice] 本体 TTS 组件合成失败: %s", public_code)
            raise RuntimeError(messages.get(code, "语音合成失败，请检查本体 TTS 设置及日志")) from None

    async def synthesize_reply_voice_file(self, text: str) -> tuple[str, str]:
        """保存本体生成的音频，返回 QQ 发送所需的 ``(file:// URI, MIME)``。"""
        normalized_text = str(text or "").strip()
        if not normalized_text:
            raise RuntimeError("语音合成文本不能为空")
        await self.cleanup_voice_output_dir()
        audio_bytes, mime_type = await self.synthesize_reply_voice_audio(normalized_text)
        if not audio_bytes:
            raise RuntimeError("语音合成未返回音频数据")
        voice_dir = self.get_voice_output_dir()
        await asyncio.to_thread(voice_dir.mkdir, parents=True, exist_ok=True)
        suffix = ".mp3" if "mpeg" in mime_type else ".wav"
        output_path = voice_dir / f"qq_reply_{int(time.time() * 1000)}_{random.randint(1000, 9999)}{suffix}"
        await asyncio.to_thread(output_path.write_bytes, audio_bytes)
        return output_path.resolve().as_uri(), mime_type

    def _client_supports_voice(self) -> bool:
        """当前通道能不能收语音。

        开放平台的 `supports_voice` 是 False，而这里以前**从不问它**：`voice` 模式下
        每次都先 `synthesize_reply_voice_file()`（真跑一次 TTS、落一个音频文件），
        然后 `send_*_record` 在那边是空桩、返回 None，于是判成"未确认"再回退文本 ——
        功能上没错，但每次白烧一次语音合成。

        宁可少发一句也不肯多烧一次 TTS，所以合成之前就问。
        拿不到这个属性的连接**按支持处理**（保持旧行为：只有明确说不支持才跳过）。
        """
        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            return False
        return bool(getattr(client, "supports_voice", True))

    async def _private_text_fallback(
        self, target_qq: str, normalized_text: str, *, mode: str, fallback: bool,
    ) -> bool:
        """通道发不出语音时，私聊这条该怎么收场。

        * ``both``：文字本来就是这条回复的一部分（今天也是"先发文字、语音失败保留文字"），
          照发；
        * ``voice``：只有 ``fallback`` 允许时才拿文字顶上；不允许的调用方（转达 / 主动发言）
          要的是"语音没发出去就是没发出去"，这里返回 False 让它如实报未确认，
          而不是擅自补一条那几位调用方明确不要的文字。
        """
        if mode != "both" and not fallback:
            self.plugin.logger.info("当前通道不支持语音，且未允许回退文本，跳过私聊回复")
            return False
        if not normalized_text:
            return False
        return self._confirm_send(
            await self.plugin.qq_client.send_message(target_qq, normalized_text)
        )

    async def _group_text_fallback(
        self, group_id: str, text_segments: list[dict[str, Any]], normalized_text: str, *,
        mode: str, fallback: bool, keyboard: str = "",
    ) -> bool:
        """群聊版的同上（判据一致，只是发送接口不同：segments + keyboard）。"""
        if mode != "both" and not fallback:
            self.plugin.logger.info("当前通道不支持语音，且未允许回退文本，跳过群聊回复")
            return False
        if not normalized_text:
            return False
        return self._confirm_send(
            await self.plugin.qq_client.send_group_message_segments(
                group_id, text_segments, keyboard=keyboard,
            ),
        )

    @staticmethod
    def _confirm_send(result) -> bool:
        """Falsy result == the send was never confirmed.

        Both clients report a receipt for every sender now (Open Platform
        message id; NapCat echo round-trip), so there is no "no channel"
        case left to special-case — and no per-call flag to forget on a
        fallback path."""
        return bool(result)

    async def deliver_private_reply(self, target_qq: str, text: str, *, voice_text: str = "", fallback_to_text_on_voice_failure: bool) -> bool:
        normalized_text = self.plugin._validate_outbound_message(text)
        mode = self.plugin._get_reply_mode()
        if mode == "text":
            return self._confirm_send(
                await self.plugin.qq_client.send_message(target_qq, normalized_text)
            )
        if not self._client_supports_voice():
            # 这个通道发不出语音：直接按上面那套判据落文字，不做那次注定要丢的合成。
            return await self._private_text_fallback(
                target_qq, normalized_text, mode=mode, fallback=fallback_to_text_on_voice_failure,
            )
        # both 模式：LLM 自主决定 → 有 <record> 则语音，否则纯文字
        if mode == "both":
            voice_content = (voice_text or "").strip()
            if voice_content:
                # LLM 指定了语音内容
                text_ok = False
                if normalized_text:
                    text_ok = self._confirm_send(
                        await self.plugin.qq_client.send_message(target_qq, normalized_text)
                    )
                try:
                    file_uri, _ = await self.synthesize_reply_voice_file(voice_content)
                    return self._confirm_send(
                        await self.plugin.qq_client.send_private_record(target_qq, file_uri),
                    ) or text_ok
                except Exception:
                    self.plugin.logger.warning("QQ both-语音私聊发送失败，已保留文本", exc_info=True)
                    return text_ok
            else:
                # 无 <record> 标签 → 纯文字
                return self._confirm_send(
                    await self.plugin.qq_client.send_message(target_qq, normalized_text)
                )
        try:
            file_uri, _ = await self.synthesize_reply_voice_file(normalized_text)
            voice_ok = self._confirm_send(
                await self.plugin.qq_client.send_private_record(target_qq, file_uri),
            )
            if voice_ok:
                return True
            if mode == "voice" and fallback_to_text_on_voice_failure:
                # 开放平台失败吞异常返回 None（不 raise）：未确认同样要走
                # 文本回退，不能把"没发出去"当结论直接返回。
                self.plugin.logger.warning("QQ 纯语音私聊发送未确认，回退文本")
                return self._confirm_send(
                    await self.plugin.qq_client.send_message(target_qq, normalized_text)
                )
            return False
        except Exception:
            if mode == "voice" and fallback_to_text_on_voice_failure:
                self.plugin.logger.warning("QQ 纯语音私聊发送失败，回退文本", exc_info=True)
                return self._confirm_send(
                    await self.plugin.qq_client.send_message(target_qq, normalized_text)
                )
            if mode == "both" and normalized_text:
                self.plugin.logger.warning("QQ 复合私聊中的语音发送失败，已保留文本", exc_info=True)
                return False
            raise

    async def deliver_group_reply(self, group_id: str, text: str, *, reply_message_id: str = "", at_user_id: str = "", keyboard: str = "", voice_text: str = "", fallback_to_text_on_voice_failure: bool) -> bool:
        import time

        from .group_send_safety import group_send_safety
        safety = group_send_safety(self.plugin)
        started_at = time.time()
        if await safety.blocked(group_id, created_at=started_at, force=True):
            return False
        normalized_text = self.plugin._validate_outbound_message(text)
        mode = self.plugin._get_reply_mode()
        text_segments: list[dict[str, Any]] = []
        if str(reply_message_id or "").strip():
            text_segments.append({"type": "reply", "data": {"id": str(reply_message_id)}})
        if str(at_user_id or "").strip():
            text_segments.append({"type": "at", "data": {"qq": str(at_user_id)}})
        # 上游可能传入 _compose_text 生成的 CQ 字符串；OneBot 不会解析
        # text 段中的 CQ 码。只展开插件生成的修饰，正文保留为 text。
        raw_text = f" {normalized_text}" if at_user_id else normalized_text
        offset = 0
        for match in re.finditer(r"\[CQ:(reply,id|at,qq|face,id)=([^\],]+)\]", raw_text):
            if match.start() > offset:
                text_segments.append({"type": "text", "data": {"text": raw_text[offset:match.start()]}})
            kind, key = match.group(1).split(",")
            value = match.group(2).replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")
            segment = {"type": kind, "data": {key: value}}
            if segment not in text_segments:
                text_segments.append(segment)
            if kind == "reply" and not reply_message_id:
                reply_message_id = value
            elif kind == "at" and not at_user_id:
                at_user_id = value
            offset = match.end()
        if offset < len(raw_text):
            text_segments.append({"type": "text", "data": {"text": raw_text[offset:]}})
        speech_text = "".join(seg["data"]["text"] for seg in text_segments if seg["type"] == "text").strip()
        if mode == "text":
            return self._confirm_send(
                await self.plugin.qq_client.send_group_message_segments(group_id, text_segments, keyboard=keyboard),
            )
        if not self._client_supports_voice():
            # 与私聊那条同源：通道发不出语音时，别先合成一遍再判失败。
            return await self._group_text_fallback(
                group_id, text_segments, normalized_text,
                mode=mode, fallback=fallback_to_text_on_voice_failure, keyboard=keyboard,
            )
        # 语音只发独立 record；引用和 @ 留在 text_segments，供文字及失败回退使用。
        # both 模式：LLM 自主决定 → 有 <record> 则语音，否则纯文字
        if mode == "both":
            voice_content = (voice_text or "").strip()
            if voice_content:
                text_ok = False
                if normalized_text:
                    text_ok = self._confirm_send(
                        await self.plugin.qq_client.send_group_message_segments(group_id, text_segments, keyboard=keyboard),
                    )
                try:
                    file_uri, _ = await self.synthesize_reply_voice_file(voice_content)
                    if await safety.blocked(group_id, created_at=started_at, force=True):
                        return text_ok
                    return self._confirm_send(
                        await self.plugin.qq_client.send_group_record(group_id, file_uri),
                    ) or text_ok
                except Exception:
                    self.plugin.logger.warning("QQ both-语音群聊发送失败，已保留文本", exc_info=True)
                    return text_ok
            else:
                return self._confirm_send(
                    await self.plugin.qq_client.send_group_message_segments(group_id, text_segments, keyboard=keyboard),
                )
        try:
            file_uri, _ = await self.synthesize_reply_voice_file(speech_text)
            if await safety.blocked(group_id, created_at=started_at, force=True):
                return False
            voice_ok = self._confirm_send(
                await self.plugin.qq_client.send_group_record(group_id, file_uri),
            )
            if voice_ok:
                return True
            if mode == "voice" and fallback_to_text_on_voice_failure:
                if await safety.blocked(group_id, created_at=started_at, force=True):
                    return False
                # 对偶私聊路径：未确认（开放平台吞异常返回 None）也回退文本。
                # 回退的这条同样要带 keyboard——否则语音一失败，用户拿到的
                # 是一句"想看哪个？"却一个按钮都没有。
                self.plugin.logger.warning("QQ 纯语音群聊发送未确认，回退文本")
                return self._confirm_send(
                    await self.plugin.qq_client.send_group_message_segments(
                        group_id, text_segments, keyboard=keyboard,
                    ),
                )
            return False
        except Exception:
            if mode == "voice" and fallback_to_text_on_voice_failure:
                if await safety.blocked(group_id, created_at=started_at, force=True):
                    return False
                self.plugin.logger.warning("QQ 纯语音群聊发送失败，回退文本", exc_info=True)
                return self._confirm_send(
                    await self.plugin.qq_client.send_group_message_segments(
                        group_id, text_segments, keyboard=keyboard,
                    ),
                )
            if mode == "both":
                self.plugin.logger.warning("QQ 复合群聊中的语音发送失败，已保留文本", exc_info=True)
                return False
            raise
