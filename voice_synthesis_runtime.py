"""Capture existing host TTS workers without a desktop session or a new host API.

No provider URL, credential selection, voice prefix or provider-specific client
belongs here. Routing/normalization/conversion are supplied by the installed host.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

from main_logic.core.tts_runtime import TtsRuntimeMixin
from main_logic.tts_client import (
    TTS_AUDIO_DONE_SENTINEL,
    TTS_PROVIDER_REGISTRY,
    TTS_SHUTDOWN_SENTINEL,
)

MAX_AUDIO_BYTES = 16 * 1024 * 1024
SYNTHESIS_TIMEOUT_SECONDS = 125.0
READY_TIMEOUT_SECONDS = 30.0
WORKER_JOIN_SECONDS = 2.0


class PluginSpeechError(RuntimeError):
    """Only fixed public codes may leave the adapter, never provider errors."""


class _HostVoiceRoute(TtsRuntimeMixin):
    def __init__(self, config_manager):
        self._config_manager = config_manager

    def _realtime_base_url(self):
        return str(self._config_manager.get_model_api_config("realtime").get("base_url") or "")

    async def prepare(self):
        _, name, _, characters, *_ = await self._config_manager.aget_character_data()
        if name not in characters:
            raise PluginSpeechError("character_unavailable")
        self.lanlan_name = name
        self.lanlan_basic_config = characters
        core = self._config_manager.get_core_config()
        realtime = self._config_manager.get_model_api_config("realtime")
        self.core_api_type = realtime.get("api_type", "") or core.get("CORE_API_TYPE", "")
        self._apply_voice_id_for_route()
        return self._resolve_tts_worker_spec()


class _AudioQueue(Queue):
    """Bound memory without blocking a provider callback during cancellation."""
    def __init__(self):
        super().__init__(maxsize=256)
        self.closed = Event()
        self.failed = Event()
        self.failure = ""
        self._budget_lock = Lock()
        self._audio_bytes = 0

    def put(self, item, block=True, timeout=None):
        if self.closed.is_set() or self.failed.is_set():
            return
        audio = item[2] if isinstance(item, tuple) and len(item) == 3 and item[0] == "__audio__" else item
        with self._budget_lock:
            if isinstance(audio, (bytes, bytearray, memoryview)):
                self._audio_bytes += audio.nbytes if isinstance(audio, memoryview) else len(audio)
            if self._audio_bytes > MAX_AUDIO_BYTES - 44:
                self.failure = "audio_too_large"
                self.failed.set()
                return
            try:
                super().put(item, block=False)
            except Full:
                self.failure = "output_overflow"
                self.failed.set()


async def _convert_audio(chunks):
    from main_logic.watch_together.audio import write_speech_wav_async
    with tempfile.TemporaryDirectory(prefix="qq-speech-") as directory:
        output = Path(directory) / "speech.wav"
        await write_speech_wav_async(chunks, output)
        if output.stat().st_size > MAX_AUDIO_BYTES:
            raise PluginSpeechError("audio_too_large")
        return await asyncio.to_thread(output.read_bytes)


class PluginSpeechRuntime:
    """One retained runtime: fail-fast admission, no provider retries/playback.

    A timed-out worker may not be forcibly killed safely. Retain its thread and
    reject followups until it actually exits instead of leaking more workers.
    """
    def __init__(self):
        self._busy = False
        self._thread = None

    async def synthesize(self, text):
        text = str(text or "").strip()
        if not text or len(text) > 2000:
            raise PluginSpeechError("invalid_text")
        if self._busy:
            raise PluginSpeechError("busy")
        if self._thread is not None and self._thread.is_alive():
            raise PluginSpeechError("worker_cleanup_pending")
        self._busy = True
        try:
            async with asyncio.timeout(SYNTHESIS_TIMEOUT_SECONDS):
                from utils.config_manager import get_config_manager
                route = _HostVoiceRoute(get_config_manager())
                worker, key, voice, provider, disabled, config = await route.prepare()
                if disabled:
                    raise PluginSpeechError("tts_disabled")
                if not route._tts_worker_supports_completion(worker, provider, config):
                    raise PluginSpeechError("completion_unavailable")
                meta = TTS_PROVIDER_REGISTRY.get(provider) if provider else None
                spoken = route._normalize_game_speech_preload_text(
                    text, normalize_spaces=not meta or meta.category != "ws_bistream",
                )
                if not spoken:
                    raise PluginSpeechError("empty_after_normalization")
                chunks = await self._capture(worker, key, voice, spoken)
                return await _convert_audio(chunks)
        except TimeoutError:
            raise PluginSpeechError("timeout") from None
        except PluginSpeechError:
            raise
        except Exception:
            # The worker, config reader and decoder may expose secrets in errors.
            raise PluginSpeechError("tts_unavailable") from None
        finally:
            self._busy = False

    async def _capture(self, worker, key, voice, text):
        requests = Queue()
        responses = _AudioQueue()
        speech_id = "qq-speech-" + uuid.uuid4().hex
        def run_worker():
            try:
                worker(requests, responses, key, voice)
            except Exception:
                # Do not let threading.excepthook print provider credentials.
                responses.put(("__error__", None))

        thread = Thread(target=run_worker, daemon=True)
        self._thread = thread
        chunks = []

        async def next_response(deadline=None):
            while True:
                # Yield even if a provider continuously emits non-audio events.
                await asyncio.sleep(0)
                if deadline is not None and time.monotonic() >= deadline:
                    raise PluginSpeechError("tts_ready_timeout")
                if responses.failed.is_set():
                    raise PluginSpeechError(responses.failure)
                try:
                    return responses.get_nowait()
                except Empty:
                    if not thread.is_alive():
                        raise PluginSpeechError("tts_incomplete")
                    await asyncio.sleep(0.01)

        try:
            thread.start()
            deadline = time.monotonic() + READY_TIMEOUT_SECONDS
            while True:
                message = await next_response(deadline)
                if isinstance(message, tuple) and len(message) == 2:
                    if message[0] == "__error__":
                        raise PluginSpeechError("tts_unavailable")
                    if message[0] == "__ready__":
                        if message[1] is not True:
                            raise PluginSpeechError("tts_unavailable")
                        break
            requests.put((speech_id, text))
            requests.put((None, None))
            while True:
                message = await next_response()
                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__audio__":
                    if str(message[1] or "") == speech_id and isinstance(message[2], (bytes, bytearray, memoryview)):
                        chunks.append(bytes(message[2]))
                elif isinstance(message, (bytes, bytearray, memoryview)):
                    chunks.append(bytes(message))
                elif isinstance(message, tuple) and len(message) == 2:
                    if message[0] in {"__error__", "__ready__"}:
                        raise PluginSpeechError("tts_unavailable")
                    if message[0] == TTS_AUDIO_DONE_SENTINEL and str(message[1] or "") == speech_id:
                        if not chunks:
                            raise PluginSpeechError("audio_unavailable")
                        return chunks
        finally:
            responses.closed.set()
            requests.put((TTS_SHUTDOWN_SENTINEL, None))
            if thread.ident is not None:
                await asyncio.to_thread(thread.join, WORKER_JOIN_SECONDS)
