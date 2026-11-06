"""Per-turn group speaker identity, separate from group/tool/memory permissions."""
from __future__ import annotations

import json
from typing import Any


def speaker_permission(plugin: Any, sender_id: str) -> str:
    """Only the configured user permission manager can establish identity."""
    if not sender_id:
        return "none"
    try:
        manager = getattr(plugin, "permission_mgr", None)
        return str(manager.get_permission_level(sender_id) or "none") if manager else "none"
    except Exception:
        return "none"


def build_group_identity_section(
    plugin: Any, *, sender_id: str, permission_at_receipt: str | None,
    synthetic: bool,
) -> str:
    # Proactive/merged/system turns may use an admin ID as a placeholder. It
    # does not establish who authored the messages inside that turn.
    header = "## 本轮群聊身份（系统核验，仅适用于本轮）\n"
    boundary = (
        "身份依据为发送者账号与插件管理员名单，仅对应本条消息的发送者。"
    )
    if synthetic:
        return header + "这是系统发起或合并的群聊轮次，没有可据此确认的单一真人发言者。" + boundary
    sender = json.dumps(str(sender_id), ensure_ascii=True)
    current = speaker_permission(plugin, sender_id)
    if permission_at_receipt == "admin" and current == "admin":
        identity = (
            f"本条群消息的发送者（账号标识：{sender}）就是主人/管理员本人。"
        )
    else:
        identity = (
            f"本条群消息的真实发送者（账号标识：{sender}）未被核验为主人/管理员本人。"
        )
    return header + identity + boundary
