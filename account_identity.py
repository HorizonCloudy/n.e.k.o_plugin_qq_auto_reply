"""Minimal read-only identity shared with other QQ plugins; no settings/tokens."""
from typing import Any

from .qq_open_platform_v2 import is_official_bot


async def account_identity(plugin: Any) -> dict:
    payload = {
        "schema_version": 1, "provider": "qq_auto_reply", "platform": "qq",
        "account_scope": "onebot_user", "available": False, "self_id": "",
    }
    client = getattr(plugin, "qq_client", None)
    # Official bot IDs are a different namespace and must never match a QQ UIN.
    if client is None or is_official_bot(client):
        return payload
    login = await plugin.runtime_service.fetch_login_status_payload()
    ident = str(login.get("self_id") or "").strip()
    if login.get("status") == "online" and ident.isascii() and ident.isdecimal() and int(ident) > 0:
        payload.update(available=True, self_id=ident)
    return payload
