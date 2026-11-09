"""Optional sentence boundaries for text bubbles, without rewriting content."""
from __future__ import annotations

import re
from dataclasses import replace

from .pipeline_models import QQMessageBlock

# Treat URLs, Markdown links and code as indivisible spans.
_PROTECTED = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]*`|!?\[[^\]\n]*\]\([^\n]*?\)|https?://[^\s<>\u3000。！？]+|\[CQ:[^\]]*\]")
_BOUNDARY = re.compile(r"[。！？!?]+[’”\"」』）)]*|(?<!\d)\.(?=\s|$)|\n+")


def split_sentences(text: str) -> list[str]:
    protected = [(m.start(), m.end()) for m in _PROTECTED.finditer(text)]
    cuts = [m.end() for m in _BOUNDARY.finditer(text)
            if not any(start <= m.start() < end for start, end in protected)]
    parts: list[str] = []
    start = 0
    for end in cuts:
        part = text[start:end].strip()
        if part:
            parts.append(part)
        start = end
    if text[start:].strip():
        parts.append(text[start:].strip())
    return parts or [text]


def split_text_blocks(blocks: list[QQMessageBlock]) -> list[QQMessageBlock]:
    result = []
    for block in blocks:
        # Preserve structured/voice messages as authored.
        if not block.text or block.record or block.sticker or block.poke or block.ark or block.keyboard:
            result.append(block)
            continue
        parts = split_sentences(block.text)
        if len(parts) == 1:
            result.append(block)
            continue
        for index, part in enumerate(parts):
            result.append(replace(
                block, text=part,
                at_user=block.at_user if index == 0 else "",
                reply_to=block.reply_to if index == 0 else "",
                emoji=block.emoji if index == len(parts) - 1 else "",
            ))
    return result
