"""Validation and local-time windows for explicitly configured private greetings."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

MAX_GREETING_SLOTS = 12
GREETING_MODES = ("greeting", "search", "video")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def normalize_greeting_slots(value: Any) -> list[dict[str, Any]]:
    """Reject the entire invalid edit; never silently save a partial schedule."""
    if not isinstance(value, list) or len(value) > MAX_GREETING_SLOTS:
        raise ValueError("定时问候时间段必须是列表，最多 12 个")
    result = []
    ids = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("定时问候时间段格式不正确")
        sid = row.get("id")
        if not isinstance(sid, str) or not _ID.fullmatch(sid) or sid in ids:
            raise ValueError("定时问候时间段 ID 不合法或重复")
        ids.add(sid)
        start, end = row.get("start"), row.get("end")
        if not all(isinstance(v, str) and _TIME.fullmatch(v) for v in (start, end)):
            raise ValueError("时间段的起止时间必须为 HH:MM（24 小时制）")
        if start == end:
            raise ValueError("时间段的起止时间不能相同；结束早于开始表示跨零点")
        enabled, mode = row.get("enabled", True), row.get("mode", "greeting")
        if not isinstance(enabled, bool) or mode not in GREETING_MODES:
            raise ValueError("时间段开关或问候模式不合法")
        sample, topic = row.get("sample", ""), row.get("topic", "")
        if not isinstance(sample, str) or len(sample) > 500:
            raise ValueError("样例问候最多 500 字")
        if not isinstance(topic, str) or len(topic) > 300:
            raise ValueError("搜索方向最多 300 字")
        result.append(dict(id=sid, enabled=enabled, start=start, end=end,
                           mode=mode, sample=sample.strip(), topic=topic.strip()))
    return result


def slot_window(slot: dict[str, Any], now: float) -> tuple[datetime, datetime]:
    """Return today's window, or the previous day's still-active overnight window.

    The start date is also the deduplication date, including after midnight.
    The end is exclusive. Local wall times, not fixed 24-hour seconds, handle DST.
    """
    local = datetime.fromtimestamp(now)
    sh, sm = map(int, slot["start"].split(":"))
    eh, em = map(int, slot["end"].split(":"))
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        if local < end:
            start -= timedelta(days=1)
        else:
            end += timedelta(days=1)
    return start, end
