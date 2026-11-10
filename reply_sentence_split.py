"""Optional sentence boundaries and terminal full-stop cleanup for text bubbles."""
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
        # Quoted replies and structured/voice messages stay as authored.
        if not block.text or block.reply_to or block.record or block.sticker or block.poke or block.ark or block.keyboard:
            result.append(block)
            continue
        parts = split_sentences(block.text)
        if len(parts) == 1:
            result.append(block)
            continue
        for index, part in enumerate(parts):
            result.append(replace(
                block, text=_without_terminal_full_stop(part),
                at_user=block.at_user if index == 0 else "",
                reply_to=block.reply_to if index == 0 else "",
                emoji=block.emoji if index == len(parts) - 1 else "",
            ))
    return result


def _without_terminal_full_stop(text: str) -> str:
    # Keep closing quotes/brackets, protected content, decimals and ellipses.
    match = re.search(r"(?:。|(?<![\d.])\.)[’”\"」』）)]*$", text)
    if match is None or any(m.start() <= match.start() < m.end() for m in _PROTECTED.finditer(text)):
        return text
    cleaned = text[:match.start()] + text[match.start() + 1:]
    return cleaned if cleaned.strip() else text
