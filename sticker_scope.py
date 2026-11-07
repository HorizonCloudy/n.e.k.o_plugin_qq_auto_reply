"""Conversation boundaries for automatically collected stickers.

Manual uploads remain shared. Collected entries retain their original scope even
if collection policy later changes; enabling ``any`` is not retroactive consent.
"""
from typing import Any


def collection_scope(*, group_id: str, sender_id: str, policy: str) -> str:
    if policy == "any":
        return "global"
    if group_id:
        return f"group:{group_id}"
    if sender_id:
        return f"private:{sender_id}"
    return "restricted"


def entry_scope(entry: Any) -> str:
    if not isinstance(entry, dict):
        return "global"  # Legacy manually uploaded descriptions.
    if "scope" in entry:
        return str(entry["scope"] or "restricted")
    # Older auto-collected entries already have provenance; do not accidentally
    # turn them into global uploads during an upgrade.
    group = str(entry.get("group") or "")
    sender = str(entry.get("origin") or "")
    if group or sender:
        return collection_scope(group_id=group, sender_id=sender, policy="current")
    return "global"


def sticker_allowed(entry: Any, *, target_type: str = "", target_id: str = "") -> bool:
    if isinstance(entry, dict) and entry.get("sendable") is False:
        return False  # Ordinary collected photos are managed here, not offered as stickers.
    scope = entry_scope(entry)
    return scope == "global" or (
        target_type in {"group", "private"}
        and bool(target_id)
        and scope == f"{target_type}:{target_id}"
    )


def group_sticker_collection_allowed(plugin: Any, group_id: str) -> bool:
    """Group collection is opt-in; private collection retains its own policy."""
    if not str(group_id or "").strip():
        return True
    manager = getattr(plugin, "group_permission_mgr", None)
    try:
        settings = getattr(plugin, "_qq_settings", None) or {}
        if str(settings.get("sticker_collect_policy") or "").strip().lower() == "off":
            return False
        return manager.is_sticker_collection_enabled(group_id) is True
    except Exception:
        return False
