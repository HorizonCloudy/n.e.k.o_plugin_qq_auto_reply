"""Optional mixin for forever_companion 1.4.7; never loads or changes a shard.

Copy into its mixins/ and add QQEmotionSnapshotMixin to ForeverCompanionPlugin.
This is an internal RPC, not an LLM tool. See docs/companion-context.md.
"""

from __future__ import annotations

import time

from plugin.sdk.plugin import Ok, plugin_entry

_ACTIONS = frozenset(
    {
        "ebb_tide",
        "sea_fog",
        "shallow_reef",
        "storm_surge",
        "seek_harbor",
        "ripple",
        "warm_current",
        "spring_tide",
        "rising_tide",
    }
)


class QQEmotionSnapshotMixin:
    @plugin_entry(
        id="get_emotion_snapshot",
        name="只读情绪快照",
        description="内部只读接口：只导出已加载角色的动作枚举，不导出原因/日记。",
        input_schema={"type": "object", "properties": {"lanlan": {"type": "string"}}, "required": ["lanlan"]},
        metadata={"agent_hidden": True},
    )
    async def get_emotion_snapshot(self, lanlan: str = "", **_):
        now = time.time()
        unavailable = {"schema_version": 1, "lanlan": lanlan, "available": False}
        if not isinstance(lanlan, str) or not lanlan or len(lanlan) > 128:
            return Ok(unavailable)
        if not getattr(self, "_state_trusted", False) or not self._store_ready:
            return Ok(unavailable)
        shard = self._shards.get(lanlan)
        if shard is None or not shard.loaded or shard.lanlan != lanlan:
            return Ok(unavailable)
        # _mood_enabled(shard) is a pure capability evaluation in 1.4.7.
        # Never call _get_shard/_ensure_shard/_current_affect/_supervise_once.
        if not self._mood_enabled(shard):
            return Ok(unavailable)
        state = shard.mood
        action, expires = state.action, state.expires_at
        if not isinstance(action, str) or action not in _ACTIONS:
            return Ok(unavailable)
        if type(expires) not in (int, float) or not 0 <= expires <= 1e12:
            return Ok(unavailable)
        if expires > 0 and now >= expires:
            return Ok(unavailable)
        return Ok(
            {
                "schema_version": 1,
                "lanlan": lanlan,
                "available": True,
                "enabled": True,
                "sampled_at": now,
                "action": action,
                "expires_at": expires,
            }
        )
