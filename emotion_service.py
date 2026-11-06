"""The reply context's single emotion outlet, with explicit scope precedence."""
from __future__ import annotations

import asyncio
from typing import Any

from .deepchat.emotion import emotion_fragment


async def build_emotion_section(
    plugin: Any, *, is_group: bool, group_id: str | None, sender_id: str,
    synthetic: bool = False,
) -> str:
    # Group state always wins in a group, even when calm/unavailable. Never
    # fall back to a member's private state, including synthetic group turns.
    if is_group:
        attention = getattr(plugin, "attention_service", None)
        if not group_id or attention is None or not attention._enabled():
            return ""
        state = attention.get_state(str(group_id))
        return emotion_fragment(getattr(state, "emotion", "calm"), is_group=True)
    deepchat = getattr(plugin, "deepchat_service", None)
    if synthetic or not sender_id or deepchat is None or not deepchat.enabled or not deepchat.fragments_enabled:
        return ""

    def read_private() -> str:
        # Lazy decay writes too: share the writeback lock, off the event loop.
        with deepchat._writeback_lock:
            if not deepchat.enabled or not deepchat.fragments_enabled:
                return ""
            state = deepchat.mood.get(deepchat._mood_key(sender_id))
            return emotion_fragment(state.current, is_group=False)

    return await asyncio.to_thread(read_private)
