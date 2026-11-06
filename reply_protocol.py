"""Shared cleanup for internal reply metadata, before XML fallback or delivery."""
from __future__ import annotations

import re

# Metadata is not visible text. Removing only its XML shell leaks mood names.
FEELING_TAG_RE = re.compile(
    r"<feeling(?:\s[^<>]*?)?\s*/>|<feeling(?:\s[^<>]*?)?\s*>(.*?)</feeling\s*>",
    re.IGNORECASE | re.DOTALL,
)


def strip_feeling_tags(text: str) -> str:
    """Drop tag AND payload; never filter ordinary words such as 'curious'."""
    return FEELING_TAG_RE.sub("", text)


def extract_feeling(text: str) -> tuple[str, str]:
    """Consume all mood tags, but only an outside-msg tag may update mood."""
    ranges = [(m.start(), m.end()) for m in re.finditer(
        r"<msg(?:\s[^<>]*?)?\s*>.*?</msg\s*>", text, re.IGNORECASE | re.DOTALL,
    )]
    feeling = ""
    for match in FEELING_TAG_RE.finditer(text):
        value = (match.group(1) or "").strip().lower()
        if not feeling and re.fullmatch(r"\w+", value) and not any(
            start <= match.start() < end for start, end in ranges
        ):
            feeling = value
    return strip_feeling_tags(text).strip(), feeling
