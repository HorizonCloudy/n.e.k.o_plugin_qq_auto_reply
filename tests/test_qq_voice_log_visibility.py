# -*- coding: utf-8 -*-
"""看门狗：语音 / STT 的判定必须进**文件日志**，不能只进 UI 面板的内存环形缓冲。

由来（使用者 2026-11-11：「检查日志，stt 好像没有正常起效」）：

日志文件里**一条 ``[Voice]`` 都没有** —— 不是"跑了但失败得安静"，而是整条通道
压根没往文件里写过字。原因是 ``_emit_log`` 只把一行塞进内存 ring 并推 SSE
（见 ``__init__._emit`` 的说明），而 ring 在插件重载时就清空。于是：

* 「本地 STT 有没有配」「OpenAI 返回 401 了没有」「Qwen 的 key 是不是不对」
  「三个后端全军覆没」这四类判定，事后**一个字都查不到**；
* 唯一能反推的证据是 backlog 里残留的 ``[CQ:record,…]``：正文写成
  ``"[语音] [CQ:record,…]"``（标记后面**紧跟** CQ 码 = 没转出字），配上
  ``发送消息到 AI … length: 604`` 才勉强拼出结论。

同一个教训此前踩过两次，写法都是"两路都写"：``reply_buffer_service._log``
（缓冲区静默丢弃已生成的回复，见 ``test_qq_buffer_log_visibility``）与
``plugin_tool_service.emit_bridge_log``（工具桥到底挂没挂）。语音这一路照抄。

这个文件钉住三件事：

1. ``emit_voice_log`` **两路都写**，任何一路失败都不影响另一路；
2. INFO/WARNING/ERROR 的 ``[Voice]`` 日志**不许**再走 memory-only 的 ``_emit_log``
   （DEBUG 例外：文件日志级别是 INFO，写过去也看不见）；
3. 那几个判定性文案必须还在源码里（删掉就红）。
"""

from __future__ import annotations

import pathlib
import re

from plugin.plugins.qq_auto_reply.enrichment import QQMessageEnricher, emit_voice_log

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
INIT_SOURCE = (PLUGIN / "__init__.py").read_text(encoding="utf-8")
ENRICH_SOURCE = (PLUGIN / "enrichment.py").read_text(encoding="utf-8")


class _Recorder:
    """一个把两路日志分别记下来的桩插件。"""

    def __init__(self) -> None:
        self.file_lines: list[tuple[str, str]] = []
        self.panel_lines: list[tuple[str, str]] = []

        class _Logger:
            def __init__(self, sink):
                self._sink = sink

            def _write(self, level, msg):
                self._sink.append((level, str(msg)))

            def debug(self, msg):
                self._write("DEBUG", msg)

            def info(self, msg):
                self._write("INFO", msg)

            def warning(self, msg):
                self._write("WARN", msg)

            def error(self, msg):
                self._write("ERROR", msg)

        self.logger = _Logger(self.file_lines)
        self.emit_log = lambda level, msg: self.panel_lines.append((level, str(msg)))


def _enricher(rec: _Recorder) -> QQMessageEnricher:
    return QQMessageEnricher(
        object(), emit_log=rec.emit_log, logger=rec.logger,
    )


# ── 1. 两路都写 ────────────────────────────────────────────────────

def test_log_writes_to_both_sinks():
    rec = _Recorder()
    emit_voice_log(rec.emit_log, rec.logger, "WARNING", "[Voice] 所有STT后端均未产出结果")

    assert rec.file_lines == [("WARN", "[Voice] 所有STT后端均未产出结果")], (
        "文件日志没收到 —— 这正是本次要修的那个盲区"
    )
    assert rec.panel_lines == [("WARNING", "[Voice] 所有STT后端均未产出结果")], (
        "面板那路不该被砍掉"
    )


def test_levels_map_to_the_logger_methods():
    rec = _Recorder()
    for level in ("INFO", "WARN", "WARNING", "ERROR"):
        emit_voice_log(rec.emit_log, rec.logger, level, f"{level} 测试")
    assert [lvl for lvl, _ in rec.file_lines] == ["INFO", "WARN", "WARN", "ERROR"]


def test_debug_stays_out_of_the_file_log():
    """DEBUG 照旧只进 ring：文件日志级别是 INFO，写过去也看不见。"""
    rec = _Recorder()
    emit_voice_log(rec.emit_log, rec.logger, "DEBUG", "[Voice] get_record完成: bytes=0")

    assert rec.file_lines == [], "DEBUG 被写进文件日志了 —— 级别映射多余"
    assert rec.panel_lines, "DEBUG 连面板都没收到"


def test_a_broken_logger_cannot_break_the_panel():
    """任何一路失败都不能影响另一路（日志本身绝不该影响消息管线）。"""
    rec = _Recorder()

    def _boom(msg):
        raise RuntimeError("文件日志坏了")

    rec.logger.warning = _boom
    emit_voice_log(rec.emit_log, rec.logger, "WARNING", "[Voice] 测试")

    assert rec.panel_lines, "文件日志抛异常时面板那路也没写 —— 两路必须互相独立"


def test_a_missing_logger_is_tolerated():
    """轻量宿主 / 单测桩可能没有 logger。"""
    panel: list[tuple[str, str]] = []
    emit_voice_log(lambda lvl, msg: panel.append((lvl, str(msg))), None, "WARNING", "[Voice] 无 logger")
    assert panel, "没有 logger 时连面板那路都没写"


def test_a_missing_emit_log_is_tolerated():
    """反过来也一样：ring 那路缺席时文件那路要照写。"""
    rec = _Recorder()
    emit_voice_log(None, rec.logger, "WARNING", "[Voice] 无 ring")
    assert rec.file_lines, "没有 _emit_log 时文件那路也没写"


# ── 2. 增强器的语音日志同样双写 ─────────────────────────────────────

def test_enricher_voice_log_writes_both():
    rec = _Recorder()
    _enricher(rec)._voice_log("WARNING", "[Voice] 语音未转写，只留 [语音] 标记")

    assert rec.file_lines and rec.file_lines[0][0] == "WARN"
    assert rec.panel_lines


# ── 3. 源码级：判定性日志不许再 memory-only ────────────────────────

_MEMORY_ONLY = re.compile(r'_emit_log\(\s*"(?:INFO|WARNING|WARN|ERROR)"')


def _memory_only_voice_lines(source: str) -> list[str]:
    return [
        line.strip()
        for line in source.splitlines()
        if "[Voice]" in line and _MEMORY_ONLY.search(line)
    ]


def test_init_voice_logs_no_longer_go_to_memory_only():
    """``_transcribe_voice`` 的判定专用 ``_voice_log``。"""
    offenders = _memory_only_voice_lines(INIT_SOURCE)
    assert not offenders, (
        "这些 [Voice] 日志仍然只进内存环形缓冲（重启即失、文件里查不到）：\n"
        + "\n".join(offenders)
    )


def test_enrichment_voice_logs_no_longer_go_to_memory_only():
    offenders = _memory_only_voice_lines(ENRICH_SOURCE)
    assert not offenders, (
        "这些 [Voice] 日志仍然只进内存环形缓冲（重启即失、文件里查不到）：\n"
        + "\n".join(offenders)
    )


def test_every_stt_exit_leaves_a_line():
    """``_transcribe_voice`` 的每个出口都要有 INFO/WARNING/ERROR 留痕。

    真机代价：一个字节都没留的时候，只能靠"prompt 里只剩 ``[语音]``"反推，
    再拿两条 401 去猜是哪一层咽掉的。这些文案删掉就等于把那条通道又闭上了。
    """
    for marker in (
        "[Voice] 无音频数据",              # 拿不到音频
        "[Voice] 本地STT完成",             # 本地成功
        "[Voice] 本地STT返回",             # 本地返回非 200 / 空正文
        "[Voice] 本地STT异常",             # 本地抛异常
        "[Voice] OpenAI转录完成",
        "[Voice] OpenAI转录返回",
        "[Voice] OpenAI转录异常",
        "[Voice] Qwen转录完成",
        "[Voice] Qwen转录失败",
        "[Voice] Qwen转录异常",
        "[Voice] 所有STT后端均未产出结果",  # 三个都没出字（配置问题的唯一线索）
        "[Voice] _transcribe_voice顶层异常",
    ):
        assert marker in INIT_SOURCE, f"这条留痕不见了: {marker}"


def test_enrichment_leaves_a_line_on_a_failed_transcription():
    """转写没出字时必须留一条 WARNING —— 否则"语音没转出来"在文件里没有痕迹。"""
    assert "[Voice] 语音未转写" in ENRICH_SOURCE
    assert "[Voice] 语音转文字完成" in ENRICH_SOURCE
    # 60s 外层超时会把内层协程取消掉（内层 per-file 的 except 接不住），
    # 这条以前是 DEBUG，等于"语音消失"完全不留痕。
    assert "[Voice] 语音转录超时或失败" in ENRICH_SOURCE
