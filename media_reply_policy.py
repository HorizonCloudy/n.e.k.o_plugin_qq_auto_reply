"""Separate authored reply intent from image/sticker descriptions (group only)."""
from __future__ import annotations

import re
from typing import Any

_MEDIA_TYPES = frozenset({"image", "face", "mface", "bface", "sface"})
_CONTEXT_TYPES = frozenset({"at", "reply"})
_CQ = re.compile(r"\[CQ:([a-zA-Z_]+)(?:,[^\]]*)?\]")


def authored_media_text(segments: Any) -> str | None:
    """Return authored text for simple media messages; None means unknown/not media.

    Only original platform segments count. Never infer intent from VLM captions,
    image URLs/metadata or quoted messages. Voice/files/forwards retain their
    existing enrichment semantics rather than silently losing transcribed text.
    """
    found = False
    texts: list[str] = []
    if isinstance(segments, str):
        matches = list(_CQ.finditer(segments))
        kinds = {m.group(1) for m in matches}
        if not kinds.intersection(_MEDIA_TYPES) or kinds - _MEDIA_TYPES - _CONTEXT_TYPES:
            return None
        # Decode OneBot escapes only AFTER parsing, so literal escaped CQ text
        # cannot turn into an actual image or mention segment.
        return (_CQ.sub("", segments).replace("&#91;", "[")
                .replace("&#93;", "]").replace("&#44;", ",")
                .replace("&amp;", "&").strip())
    if not isinstance(segments, list):
        return None
    for segment in segments:
        if not isinstance(segment, dict):
            return None
        kind = segment.get("type")
        if not isinstance(kind, str):
            return None
        if kind in _MEDIA_TYPES:
            found = True
        elif kind == "text":
            data = segment.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                return None
            texts.append(data["text"])
        elif kind not in _CONTEXT_TYPES:
            return None
    return "".join(texts).strip() if found else None
