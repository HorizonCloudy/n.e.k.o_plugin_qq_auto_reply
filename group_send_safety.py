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
        self.checked: dict[str, float] = {}
        self.recent: dict[str, list[float]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def blocked(self, group_id: str, *, created_at: float = 0,
                      force: bool = False) -> bool:
        gid = str(group_id)
        async with self.locks.setdefault(gid, asyncio.Lock()):
            now = time.time()
            client = getattr(self.plugin, "qq_client", None)
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
                                    muted = muted or float(data.get("shut_up_timestamp") or 0) > now
                    except Exception:
                        # A failed refresh must not clear a previously known mute.
                        muted = muted or self.muted.get(gid, False)
                self.checked[gid] = now
            else:
                muted = muted or self.muted.get(gid, False)
            if muted or self.muted.get(gid, False):
                # Includes the first observation of unmute: old inputs and drafts
                # must not become eligible just because the ban has ended.
                self.cutoff[gid] = now
            if muted != self.muted.get(gid, False):
                emit = getattr(self.plugin, "_emit_log", None)
                if callable(emit):
                    emit("INFO", f"[Mute] 群{gid} {'禁言中' if muted else '已解禁'}，此前消息与待发回复不补发")
            self.muted[gid] = muted
            return muted or bool(created_at and created_at <= self.cutoff.get(gid, 0))

    def admit(self, group_id: str) -> bool:
        """At most three reply plans per minute; no retry queue."""
        now = time.time()
        rows = self.recent.setdefault(str(group_id), [])
        rows[:] = [stamp for stamp in rows if now - stamp < 60]
        if len(rows) >= 3:
            return False
        rows.append(now)
        return True


def group_send_safety(plugin: Any) -> GroupSendSafety:
    service = getattr(plugin, "_group_send_safety", None)
    if not isinstance(service, GroupSendSafety):
        service = GroupSendSafety(plugin)
        plugin._group_send_safety = service
    return service
