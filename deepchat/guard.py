from __future__ import annotations

import re

#: A fragment is prompt text, not an instruction channel. These patterns are
#: the cheap first line against milestone/memory text trying to hijack the
#: persona. This is deliberately conservative: false positives only drop a
#: decorative fragment, while a false negative leaks an instruction.
_INJECTION_PATTERNS: tuple[str, ...] = (
    "忽略以上",
    "忽略之前",
    "忽略所有",
    "无视以上",
    "system prompt",
    "系统提示词",
    "系统提示",
    "你现在是",
    "从现在开始你是",
    "ignore previous",
    "ignore all previous",
    "jailbreak",
    "开发者模式",
    "developer mode",
    "pretend to be",
    "act as a",
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")

DEFAULT_MAX_LEN = 400


def sanitize_text(text: object, *, max_len: int = DEFAULT_MAX_LEN) -> str:
    """Normalize a fragment: strip control chars, collapse whitespace, cap length."""
    value = _CONTROL_CHARS.sub("", str(text or ""))
    value = _WHITESPACE.sub(" ", value).strip()
    if max_len > 0 and len(value) > max_len:
        value = value[:max_len].rstrip()
    return value


def is_safe_text(text: object) -> bool:
    """False when the text looks like an instruction-injection attempt."""
    lowered = sanitize_text(text, max_len=0).lower()
    return not any(pattern in lowered for pattern in _INJECTION_PATTERNS)


def sanitize_fragment(text: object, *, max_len: int = DEFAULT_MAX_LEN) -> str | None:
    """Return a safe fragment, or None when it should not reach the prompt."""
    value = sanitize_text(text, max_len=max_len)
    if not value or not is_safe_text(value):
        return None
    return value
