"""Reply-style bundles. Loading old settings never applies a bundle implicitly."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import settings_schema

PRESET_KEY = "reply_behavior_preset"
# Deliberately exclude consent, proactive contact, connection and outbound safety.
MANAGED_KEYS = tuple(spec.key for spec in (
    *settings_schema.ATTENTION, *settings_schema.ATTENTION_NEW,
    *settings_schema.PACING,
)) + ("dormancy_enabled", "dormancy_idle_seconds", "dormancy_wake_score",
      "group_buffer_enabled", "private_buffer_enabled")


def preset_values(name: str) -> dict[str, Any]:
    values = {key: deepcopy(settings_schema.BY_KEY[key].default) for key in MANAGED_KEYS}
    if name == "fast":
        values.update(
            group_buffer_enabled=False, private_buffer_enabled=False,
            buffer_delay_mean_seconds=0.0, buffer_delay_private_seconds=0.0,
            buffer_collect_window_seconds=0.0, buffer_collect_window_private_seconds=0.0,
            dormancy_enabled=False, dormancy_idle_seconds=0,
            attention_fall_rate=0.0, attention_consume_ratio=0.0,
            attention_feedback_silent_penalty=0.0, reply_burst_max_replies=12,
        )
    elif name == "quiet":
        values.update(
            buffer_collect_window_seconds=10.0, buffer_collect_window_private_seconds=3.0,
            buffer_delay_mean_seconds=2.0, buffer_delay_private_seconds=1.0,
            reply_burst_window_seconds=180, reply_burst_max_replies=2,
            attention_base_rise_rate=0.01, attention_fall_rate=0.003,
            attention_consume_ratio=0.025, attention_heat_warm_gap_seconds=600,
            attention_feedback_silent_penalty=0.1, dormancy_idle_seconds=7200,
            attention_lock_seconds=180,
        )
    elif name != "balanced":
        raise ValueError(f"Unknown reply behavior preset: {name}")
    return values


def effective_preset(settings: dict[str, Any]) -> str:
    """Preserve legacy tuning; a marker alone is never authority to reset values."""
    name = settings.get(PRESET_KEY, "balanced")
    if name not in ("fast", "balanced", "quiet"):
        return "custom"
    values = preset_values(name)
    return name if all(
        settings.get(key, settings_schema.BY_KEY[key].default) == value
        for key, value in values.items()
    ) else "custom"


def expand_save(kwargs: dict[str, Any]) -> dict[str, Any]:
    result = dict(kwargs)
    name = result.get(PRESET_KEY)
    if name is not None:
        if name not in ("fast", "balanced", "quiet", "custom"):
            raise ValueError(f"Unknown reply behavior preset: {name}")
        if name != "custom":
            # Hidden form fields can be stale; the explicit bundle wins.
            result.update(preset_values(name))
    return result


def ui_metadata() -> dict[str, Any]:
    return {
        "values": {name: preset_values(name) for name in ("fast", "balanced", "quiet")},
        "controls": {
            key: {"id": spec.ui.id, "kind": spec.kind}
            for key in MANAGED_KEYS
            if (spec := settings_schema.BY_KEY[key]).ui is not None
        },
    }
