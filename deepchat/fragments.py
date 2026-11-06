from __future__ import annotations


def append_fragments(system_prompt: str, *fragments: str) -> str:
    """Append non-empty prompt fragments without disturbing existing text."""
    parts = [system_prompt.strip()] if system_prompt and system_prompt.strip() else []
    for fragment in fragments:
        text = (fragment or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)
