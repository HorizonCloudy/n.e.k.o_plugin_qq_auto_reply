"""Shared normalization for QQ plugin tools and read-only context capabilities."""
from typing import Any


def normalize_plugin_tiers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        plugin_id = str(key or "").strip()
        tier = str(value or "").strip().lower()
        if plugin_id and tier in ("all", "admin"):
            clean[plugin_id] = tier
    return clean
