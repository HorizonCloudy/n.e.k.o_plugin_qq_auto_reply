"""Group mute and burst protection. Blocked replies are discarded, never delayed."""
from __future__ import annotations

import asyncio
import time
from typing import Any


class GroupSendSafety:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.cutoff: dict[str, float] = {}
        self.muted: dict[str, bool] = {}
        self.muted_until: dict[str, float] = {}
        self.checked: dict[str, float] = {}
        self.recent: dict[str, list[float]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def blocked(self, group_id: str, *, created_at: float = 0,
                      force: bool = False) -> bool:
        gid = str(group_id)
        async with self.locks.setdefault(gid, asyncio.Lock()):
            now = time.time()
            client = getattr(self.plugin, "qq_client", None)
            # Capture notice expiry before is_group_muted removes expired entries.
            local_expiries = getattr(client, "_group_muted", None)
            if isinstance(local_expiries, dict) and local_expiries.get(gid):
                self.muted_until[gid] = float(local_expiries[gid])
            check = getattr(client, "is_group_muted", None)
            local_muted = callable(check) and check(gid) is True
            muted = local_muted
            if force or now - self.checked.get(gid, 0) >= 5:
                member = getattr(client, "get_group_member_info", None)
                login = getattr(client, "get_login_info", None)
                if callable(member) and callable(login):
                    try:
                        async with asyncio.timeout(5):
                            info = await login()
                            uid = info.get("user_id") if isinstance(info, dict) else None
                            if uid:
                                data = await member(gid, str(uid), no_cache=True)
                                if isinstance(data, dict):
                                    until = float(data.get("shut_up_timestamp") or 0)
                                    if until > 0:
                                        self.muted_until[gid] = max(
                                            until, self.muted_until.get(gid, 0) if local_muted else 0,
                                        )
                                    muted = muted or until > now
                    except Exception:
                        # Keep an active known mute, but don't extend its expiry.
                        until = self.muted_until.get(gid)
                        muted = muted or (self.muted.get(gid, False) and (until is None or until > now))
                self.checked[gid] = now
            else:
                until = self.muted_until.get(gid)
                muted = muted or (self.muted.get(gid, False) and (until is None or until > now))
            if muted or self.muted.get(gid, False):
                # A late observation of natural expiry must not discard fresh input.
                boundary = now if muted else min(now, self.muted_until.get(gid, now))
                self.cutoff[gid] = max(self.cutoff.get(gid, 0), boundary)
            if muted != self.muted.get(gid, False):
                emit = getattr(self.plugin, "_emit_log", None)
                if callable(emit):
                    emit("INFO", f"[Mute] 群{gid} {'禁言中' if muted else '已解禁'}，此前消息与待发回复不补发")
            self.muted[gid] = muted
            return muted or bool(created_at and created_at <= self.cutoff.get(gid, 0))

    def has_capacity(self, group_id: str) -> bool:
        """Non-consuming preflight; delivery still checks and consumes the quota."""
        now = time.time()
        rows = self.recent.setdefault(str(group_id), [])
        rows[:] = [stamp for stamp in rows if now - stamp < 60]
        return len(rows) < 3

    def admit(self, group_id: str) -> bool:
        """At most three reply plans per minute; no retry queue."""
        if not self.has_capacity(group_id):
            return False
        self.recent[str(group_id)].append(time.time())
        return True


def group_send_safety(plugin: Any) -> GroupSendSafety:
    service = getattr(plugin, "_group_send_safety", None)
    if not isinstance(service, GroupSendSafety):
        service = GroupSendSafety(plugin)
        plugin._group_send_safety = service
    return service
