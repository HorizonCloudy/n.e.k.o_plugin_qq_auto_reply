"""One-way private -> group reads; storage and private reads remain isolated.

A per-turn capability is minted before awaits, never from model arguments.
It names only the actual speaker, and doubles as a delivery-time consent key.
"""
from __future__ import annotations

from typing import Any

from .group_identity import speaker_permission
from .memory_bridge import QQMemoryQueryResult

TOKEN_PREFIX = "group-private-memory:"


def capture_private_memory_token(
    plugin: Any, *, sender_id: str, permission_at_receipt: str | None,
    member_at_receipt: bool, participant_at_receipt: bool, synthetic: bool,
) -> str:
    sender = str(sender_id or "").strip()
    if not sender or synthetic or not member_at_receipt:
        return ""
    if permission_at_receipt == "admin":
        mode = "legacy"
    elif permission_at_receipt in ("normal", "trusted") and participant_at_receipt:
        mode = "participant"
    else:
        return ""
    token = f"{TOKEN_PREFIX}{mode}:{sender}"
    return token if private_memory_token_live(plugin, token) else ""


def private_memory_token_live(plugin: Any, token: str) -> bool:
    if not token.startswith(TOKEN_PREFIX):
        return False
    mode, sep, sender = token[len(TOKEN_PREFIX):].partition(":")
    if not sep or not sender or mode not in ("legacy", "participant"):
        return False
    settings = getattr(plugin, "_qq_settings", {}) or {}
    if not (settings.get("group_memory_enabled") and settings.get("group_member_memory_enabled")):
        return False
    permission = speaker_permission(plugin, sender)
    if mode == "legacy":
        return permission == "admin"
    return permission in ("normal", "trusted", "admin") and bool(
        settings.get("private_participant_memory_enabled")
    )


def _subjects(plugin: Any, token: str) -> list[dict[str, str]] | None:
    mode, _, sender = token[len(TOKEN_PREFIX):].partition(":")
    return None if mode == "legacy" else [plugin.memory_bridge.participant_subject(sender)]


async def fetch_private_bootstrap(plugin: Any, her_name: str, token: str) -> str:
    if not private_memory_token_live(plugin, token):
        return ""
    try:
        subjects = _subjects(plugin, token)
        if subjects is None:
            text = await plugin.memory_bridge.fetch_bootstrap_memory(her_name)
        else:
            text = await plugin.memory_bridge.fetch_scoped_bootstrap_memory(
                her_name, subjects=subjects,
            )
        return text if private_memory_token_live(plugin, token) else ""
    except Exception as exc:
        plugin.logger.warning("群聊读取当前发言人的私聊核心记忆失败: %s", exc)
        return ""


async def query_private_memory(
    plugin: Any, her_name: str, token: str, query: str, *, time_spec: str, timeout: float,
) -> QQMemoryQueryResult:
    if not private_memory_token_live(plugin, token):
        return QQMemoryQueryResult()
    try:
        result = await plugin.memory_bridge.query_relevant_memory(
            her_name, query, subjects=_subjects(plugin, token),
            time_spec=time_spec, timeout=timeout,
        )
        return result if private_memory_token_live(plugin, token) else QQMemoryQueryResult()
    except Exception as exc:
        plugin.logger.warning("群聊召回当前发言人的私聊记忆失败: %s", exc)
        return QQMemoryQueryResult()


def render_private_memory(text: str) -> str:
    return "【当前发言人的私聊记忆（只读背景，不代表本群已发生或已说过）】\n" + text
